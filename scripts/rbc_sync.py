#!/usr/bin/env python3
"""Каждые 5 минут: история наших курсов + сверка с РБК + автообновление РБК (пилот: №196).

1. История. Тянем курсы всех наших отделений (16 своих + дежурные) из API Юнистрима и
   дописываем в CSV только изменения (новый lastUpdated или новые цифры). Это сырьё для
   будущего алгоритма «подсказать курс»: когда и на сколько Игорь переставлял курсы.
   CSV лежит в основном (приватном) репо и уезжает на GitHub ночным снапшотом.
   Курсы конкурентов в тот же момент — это git-история data/competitors.js.

2. Сверка с РБК. Для каждого отделения из rbc-ids.json читаем публичную карточку РБК
   (без входа) и пишем снимок в data/rbc_status.js. Сравнение «наш курс против РБК» делает
   уже браузер: свой живой курс он берёт из API Юнистрима сам, так что даже если этот
   скрипт умер, страница это увидит по устаревшему RBC_HEARTBEAT.

3. Автообновление. Для отделений из SYNC (пока только 196): если курсы в пакете кабинета
   расходятся с Юнистримом, а пакет запущен, — stop → save → start, затем перечитываем пакет.
   Строки «от 10 001» ставим на 10 коп. лучше основного курса (покупка +0.10, продажа −0.10),
   как их ставил Игорь.

Ключи: --dry-run (ничего не пишет и не сохраняет в РБК), --no-publish (не коммитит сайт).
"""
import csv
import json
import ssl
import sys
import time
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import rbc  # noqa: E402

SYNC = {"196",                       # пилот 19.11; 20.11 папа попросил ещё 11 касс
        "023", "027", "044", "098", "189", "198", "207", "211", "227", "253", "281", "286"}
TIER_STEP = 0.10                     # строки «от 10 001»: на 10 коп. лучше основного курса
MAX_JUMP = 0.08                      # больше 8% за раз — не верим, не пишем, алертим
MIN_WRITE_GAP_S = 180                # не чаще раза в 3 мин на отделение
MANUAL_STOP_GRACE_S = 600            # пакет остановили руками — 10 мин не вмешиваемся
HEARTBEAT_EVERY_S = 25 * 60          # чтобы не гонять пересборку Pages каждые 5 минут
HISTORY = Path("/root/My-Digital-Brain/Projects/unistream-history/our_rates.csv")
SYNC_LOG = Path("/root/My-Digital-Brain/Projects/unistream-history/rbc_sync_log.csv")
STATE = HERE / "state" / "rbc_sync_state.json"
STATUS_JS = ROOT / "data" / "rbc_status.json"
CA = HERE / "certs" / "russian_trusted.pem"   # unistream.ru на сертификате Минцифры
MSK = timezone(timedelta(hours=3))
DRY = "--dry-run" in sys.argv


def log(m):
    print(f"[{datetime.now(MSK):%H:%M:%S}] {m}", file=sys.stderr, flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- Юнистрим ----------
_ctx = ssl.create_default_context(cafile=str(CA))


def uni_rates(pos_id):
    req = urllib.request.Request(f"https://unistream.ru/api/poses/exchange/{pos_id}",
                                 headers={"User-Agent": rbc.UA, "Accept": "application/json",
                                          "Accept-Language": "ru"})
    last_err = None
    for attempt in range(3):
        try:
            d = json.load(urllib.request.urlopen(req, context=_ctx, timeout=20))
            return {r["currency"]: r for r in d.get("exchangeRates") or [] if r.get("currency")}
        except Exception as e:          # noqa: BLE001 — сеть/HTTP/JSON: повторяем
            last_err = e
            time.sleep(2 + attempt * 3)
    raise last_err


def all_branches():
    core = json.loads((HERE / "branch-ids.json").read_text())
    duty = json.loads((HERE / "branch-ids-duty.json").read_text())
    out = {k: (v, "duty") for k, v in duty.items()}
    out.update({k: (v, "core") for k, v in core.items()})
    return out


def fetch_all(branches):
    res, errs = {}, {}

    def one(item):
        num, (pos, _) = item
        try:
            return num, uni_rates(pos), None
        except Exception as e:          # noqa: BLE001
            return num, None, f"{type(e).__name__}: {e}"
    with ThreadPoolExecutor(8) as ex:
        for num, rates, err in ex.map(one, branches.items()):
            if rates is not None:
                res[num] = rates
            else:
                errs[num] = err
    return res, errs


# ---------- история ----------
HIST_FIELDS = ["observed_at_utc", "branch", "kind", "pos_id", "currency", "buy", "sell",
               "last_updated", "prev_buy", "prev_sell", "prev_last_updated"]


def record_history(state, branches, rates):
    last = state.setdefault("last_rates", {})
    rows, obs = [], now_iso()
    for num, cur_map in rates.items():
        pos, kind = branches[num]
        for cur, r in cur_map.items():
            key = f"{num}:{cur}"
            new = [r.get("buyRate"), r.get("sellRate"), r.get("lastUpdated")]
            old = last.get(key)
            if old != new:
                rows.append({"observed_at_utc": obs, "branch": num, "kind": kind, "pos_id": pos,
                             "currency": cur, "buy": new[0], "sell": new[1], "last_updated": new[2],
                             "prev_buy": old[0] if old else "", "prev_sell": old[1] if old else "",
                             "prev_last_updated": old[2] if old else ""})
                last[key] = new
    if rows and not DRY:
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        fresh = not HISTORY.exists()
        with HISTORY.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=HIST_FIELDS)
            if fresh:
                w.writeheader()
            w.writerows(rows)
    return len(rows)


# ---------- РБК: снимок для сайта ----------
def rbc_snapshot(ids):
    snap = {}

    def one(item):
        num, bid = item
        try:
            p = rbc.public_info(bid)
            base = {c: list(v.get(1.0) or v[min(v)]) for c, v in p["quotes"].items()}
            return num, {"rbc_id": bid, "active": p["active"], "q": base}
        except Exception as e:          # noqa: BLE001
            return num, {"rbc_id": bid, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    with ThreadPoolExecutor(6) as ex:
        for num, v in ex.map(one, ids.items()):
            snap[num] = v
    return snap


def write_status(state, snap, sync_info):
    """Переписываем файл только при смене цифр или раз в ~25 минут (пульс)."""
    payload = {"branches": snap, "sync": sync_info}
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    hb = state.get("heartbeat_at")
    hb_old = (not hb) or (datetime.now(timezone.utc) - datetime.fromisoformat(hb)).total_seconds() > HEARTBEAT_EVERY_S
    if body == state.get("status_body") and not hb_old:
        return False
    state["status_body"] = body
    state["heartbeat_at"] = now_iso()
    if DRY:
        return True
    # JSON, а не .js: страницу держат открытой часами, и она перечитывает снимок сама
    # (fetch с того же github.io, CORS не мешает); .js с const повторно не подключишь
    STATUS_JS.write_text(json.dumps({"heartbeat": state["heartbeat_at"], **payload},
                                    ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                         encoding="utf-8")
    return True


# ---------- РБК: автообновление ----------
def fmt(x):
    return f"{float(x):.2f}"


def desired_rows(rows, our, allowed=None):
    """Строки пакета с нашими курсами. Возвращает (new_rows, changes, problems).
    allowed — валюты, которые можно переписывать (None = все)."""
    new, changes, problems = [], [], []
    for r in rows:
        r2 = dict(r)
        code = r["pair"][:3] if r["pair"].endswith("/RUB") else None
        u = our.get(code) if code and (allowed is None or code in allowed) else None
        if u and u.get("buyRate") and u.get("sellRate"):
            buy, sell = float(u["buyRate"]), float(u["sellRate"])
            # строки для крупных сумм («от 3 001» у 189/198, «от 10 001» у остальных) — на 10 коп. лучше
            if float(r["summa"] or 1) > 1000:
                buy, sell = round(buy + TIER_STEP, 2), round(sell - TIER_STEP, 2)
            if buy >= sell:
                problems.append(f"{code} от {r['summa']}: покупка {buy} ≥ продажи {sell}")
            else:
                old_b, old_s = float(r["bid"] or 0), float(r["ask"] or 0)
                for o, n in ((old_b, buy), (old_s, sell)):
                    if o and abs(n - o) / o > MAX_JUMP:
                        problems.append(f"{code}: скачок {o} → {n} больше {int(MAX_JUMP * 100)}%")
                if fmt(old_b) != fmt(buy) or fmt(old_s) != fmt(sell):
                    changes.append(f"{code}{'' if float(r['summa'] or 1) <= 1000 else ' от ' + str(int(float(r['summa'])))}: "
                                   f"{fmt(old_b)}/{fmt(old_s)} → {fmt(buy)}/{fmt(sell)}")
                r2["bid"], r2["ask"] = fmt(buy), fmt(sell)
        new.append(r2)
    return new, changes, problems


def _fp(rows):
    return "|".join(f"{r['currency_id']}:{r['summa']}:{fmt(r['bid'] or 0)}:{fmt(r['ask'] or 0)}" for r in rows)


def uni_changed_at(our):
    ts = [datetime.fromisoformat(r["lastUpdated"]) for r in our.values() if r.get("lastUpdated")]
    return max(ts) if ts else None


def sync_branch(cab, num, rbc_id, our, state):
    st = state.setdefault("sync", {}).setdefault(num, {})
    pk = cab.package(rbc_id)
    info = {"running": pk["running"], "stop_time": pk["stop_time"],
            "last_write": st.get("last_write"), "last_result": st.get("last_result")}
    st["running_seen"] = pk["running"]
    if not pk["rows"]:
        info["note"] = "пакет пустой"
        return info

    # Когда пакет в кабинете менялся в последний раз. Урок 19.11.2026 14:28: курс на РБК
    # поставили РАНЬШЕ, чем он появился на сайте Юнистрима, и «перезапиши, раз отличается»
    # откатило свежий курс на старый. Поэтому пишем, только если курс на сайте сменился
    # ПОЗЖЕ последней правки пакета. Первый взгляд на пакет = «правили только что».
    fp = _fp(pk["rows"])
    if st.get("cab_fp") != fp:
        changed_at = now_iso()
        if "cab_fp" not in st and not pk["running"]:
            # первый взгляд на остановленный пакет: правили его не позже остановки. Иначе
            # «правили только что» не пустило бы сегодняшний курс, и утренний запуск
            # выставил бы вчерашние цифры
            try:
                changed_at = (datetime.strptime(pk["stop_time"], "%d.%m.%Y %H:%M").replace(tzinfo=MSK)
                              .astimezone(timezone.utc).isoformat(timespec="seconds"))
            except (TypeError, ValueError):
                pass
        st["cab_fp"], st["cab_changed_at"] = fp, changed_at
    cab_at = datetime.fromisoformat(st["cab_changed_at"])
    info["cab_changed_at"] = st["cab_changed_at"]
    # по каждой валюте: переписываем только те, что сменились на сайте позже правки пакета
    allowed = {c for c, r in our.items()
               if r.get("lastUpdated") and datetime.fromisoformat(r["lastUpdated"]) > cab_at}

    all_changes = desired_rows(pk["rows"], our)[1]
    new, changes, problems = desired_rows(pk["rows"], our, allowed)
    info["pending"] = all_changes
    if len(changes) < len(all_changes):
        info["manual"] = True
        info["note"] = "на РБК курс правили позже, чем на сайте Юнистрима: такие валюты не трогаем"
        log(f"№{num}: РБК правился позже сайта ({cab_at:%H:%M} UTC), не трогаю: "
            + "; ".join(c for c in all_changes if c not in changes))
    if problems and changes:
        info["last_result"] = st["last_result"] = "отказ: " + "; ".join(problems)
        raise rbc.RbcError(f"№{num}: не записываю в РБК — " + "; ".join(problems))

    if not pk["running"]:
        # Вечером РБК сам останавливает пакет по режиму работы кассы, а утром его НИКТО не
        # запускает (20.11.2026: в 10:20 у всех 15 отделений на РБК было пусто). Поэтому
        # утром запускаем сами — но только когда на сайте уже стоит СЕГОДНЯШНИЙ курс,
        # иначе выставили бы на день вчерашний.
        today = datetime.now(MSK).date()
        if not any(r.get("lastUpdated") and datetime.fromisoformat(r["lastUpdated"]).astimezone(MSK).date() == today
                   for r in our.values()):
            info["note"] = "пакет остановлен, сегодняшнего курса на сайте ещё нет"
            return info
        # у касс разный режим работы (253 с 9:30, 196 с 10:00, 011 по пт-сб до 23:00), и РБК
        # пускает пакет только в эти часы — включаем с открытия кассы, после закрытия не трогаем
        hours = open_hours(rbc_id)
        if hours is None:
            info["note"] = "касса по режиму РБК сейчас закрыта"
            return info
        # пакет только что остановил человек (правит руками в кабинете) — не мешаем
        stopped = None
        try:
            stopped = datetime.strptime(pk["stop_time"], "%d.%m.%Y %H:%M").replace(tzinfo=MSK)
        except (TypeError, ValueError):
            pass
        if stopped and timedelta(0) <= datetime.now(MSK) - stopped < timedelta(seconds=MANUAL_STOP_GRACE_S):
            info["note"] = f"пакет остановили вручную в {stopped:%H:%M}, ждём {MANUAL_STOP_GRACE_S // 60} мин"
            log(f"№{num}: пакет остановлен вручную в {stopped:%H:%M}, жду")
            return info
        return publish_packet(cab, num, rbc_id, pk, new if changes else pk["rows"], changes, allowed,
                              our, st, info, was_running=False)

    if not all_changes:
        info["in_sync"] = True
        return info
    if not changes:
        return info
    lw = st.get("last_write")
    if lw and (datetime.now(timezone.utc) - datetime.fromisoformat(lw)).total_seconds() < MIN_WRITE_GAP_S:
        info["note"] = "недавно писали, ждём публикации"
        return info
    return publish_packet(cab, num, rbc_id, pk, new, changes, allowed, our, st, info, was_running=True)


def open_hours(rbc_id, now=None):
    """(open, close) сегодня по режиму кассы на РБК, если касса сейчас открыта, иначе None.
    За 5 минут до закрытия уже не включаем: РБК всё равно снимет пакет."""
    now = now or datetime.now(MSK)
    try:
        work = rbc.public_info(rbc_id)["work"]
    except Exception:                   # noqa: BLE001 — нет данных: пусть решит сам РБК
        return (now.time(), now.time())
    for w in work:
        if int(w.get("week_day", 0)) != now.isoweekday():
            continue
        try:
            o = datetime.strptime(w["time_start"], "%H:%M").time()
            c = datetime.strptime(w["time_end"], "%H:%M").time()
        except (KeyError, ValueError):
            continue
        close_soon = (datetime.combine(now.date(), c) - timedelta(minutes=5)).time()
        if o <= now.time() < close_soon:
            return (o, c)
    return None


def publish_packet(cab, num, rbc_id, pk, rows, changes, allowed, our, st, info, was_running):
    what = "; ".join(changes) if changes else "курсы те же"
    log(f"№{num}: {'обновляю' if was_running else 'запускаю пакет (был остановлен)'} РБК: {what}")
    if DRY:
        info["note"] = "dry-run"
        return info

    old_packet = pk["packet_id"]
    if was_running:
        cab.stop(rbc_id, old_packet)
    started = False
    try:
        new_packet = cab.save(rbc_id, rows) if changes else old_packet
        r = cab.start(rbc_id, new_packet)
        started = True
    except rbc.RbcError as e:
        if "working hours" in str(e) and not was_running:
            # касса по режиму РБК ещё/уже закрыта — просто попробуем в следующий прогон
            info["note"] = "РБК не даёт запустить: касса вне рабочих часов"
            log(f"№{num}: РБК: касса вне рабочих часов, запущу позже")
            return info
        raise
    finally:
        if not started and was_running:
            # сохранить не вышло — возвращаем прежний пакет в эфир, чтобы касса не пропала с РБК
            try:
                cab.start(rbc_id, old_packet)
                log(f"№{num}: прежний пакет запущен обратно")
            except Exception as e:      # noqa: BLE001
                log(f"№{num}: НЕ удалось перезапустить прежний пакет: {e}")
    after = cab.package(rbc_id)
    left = desired_rows(after["rows"], our, allowed)[1]
    ok = after["running"] and not left
    st["last_write"] = now_iso()
    # «правка пакета» = момент курса, который мы перенесли, а не время записи: API Юнистрима
    # показывает новый курс с опозданием ~15 мин (19.11: поставлен в 14:18, в API к 14:33),
    # и курс, поставленный за минуту до нашей записи, иначе считался бы «старше РБК»
    stamps = [datetime.fromisoformat(our[c]["lastUpdated"]) for c in allowed if our[c].get("lastUpdated")]
    st["cab_fp"] = _fp(after["rows"])
    if stamps:
        st["cab_changed_at"] = max(stamps).astimezone(timezone.utc).isoformat(timespec="seconds")
    st["last_result"] = ("ok: " if ok else "не сошлось: ") + ("" if was_running else "запуск пакета; ") + what
    info.update(last_write=st["last_write"], last_result=st["last_result"], note=None,
                running=after["running"], stop_time=r.get("time"), in_sync=ok, pending=left)
    SYNC_LOG.parent.mkdir(parents=True, exist_ok=True)
    fresh = not SYNC_LOG.exists()
    with SYNC_LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if fresh:
            w.writerow(["at_utc", "branch", "rbc_id", "result", "changes"])
        w.writerow([st["last_write"], num, rbc_id, "ok" if ok else "mismatch",
                    ("" if was_running else "start; ") + what])
    if not ok:
        raise rbc.RbcError(f"№{num}: после записи в РБК пакет не совпал: running={after['running']}, осталось {left}")
    log(f"№{num}: РБК обновлён, пакет запущен до {r.get('time')}")
    return info


# ---------- main ----------
def alert(text):
    if DRY:
        log("ALERT (dry): " + text)
        return
    from collect_competitors import alert as _alert
    _alert(text)


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def main():
    state = load_state()
    branches = all_branches()
    rates, errs = fetch_all(branches)
    log(f"Юнистрим: курсы {len(rates)} отделений из {len(branches)}" + (f", ошибки: {len(errs)}" if errs else ""))
    if len(rates) < len(branches) * 0.5:
        raise RuntimeError(f"API Юнистрима ответил только по {len(rates)} отделениям из {len(branches)}: "
                           + next(iter(errs.values()), ""))
    n = record_history(state, branches, rates)
    log(f"история: новых строк {n}")

    ids = json.loads((HERE / "rbc-ids.json").read_text())
    core_ids = {k: v for k, v in ids.items() if branches.get(k, (0, ""))[1] == "core"}

    sync_info, failures = {}, []
    cab = None
    for num in sorted(SYNC):
        if num not in rates or num not in ids:
            failures.append(f"№{num}: нет курса Юнистрима или id РБК")
            continue
        try:
            cab = cab or rbc.Cabinet().login()
            sync_info[num] = sync_branch(cab, num, ids[num], rates[num], state)
        except Exception as e:          # noqa: BLE001
            failures.append(f"{type(e).__name__}: {e}")
            sync_info[num] = {"error": f"{type(e).__name__}: {str(e)[:200]}",
                              "last_write": state.get("sync", {}).get(num, {}).get("last_write")}
            log(traceback.format_exc()[-600:])

    # снимок после записи: РБК публикует раз в минуту, так что свежая запись может
    # проявиться только в следующем прогоне — браузер даёт на это 15 минут
    snap = rbc_snapshot(core_ids)
    log(f"РБК: снимок {sum('q' in v for v in snap.values())} из {len(snap)}")
    # когда курсы на РБК поменялись (по нашим замерам) — браузер сравнит со временем
    # смены курса на сайте: «РБК отстал» или «РБК правили позже сайта»
    pub = state.setdefault("pub", {})
    for num, v in snap.items():
        if "q" not in v:
            continue
        fp = json.dumps(v["q"], sort_keys=True)
        if num not in pub:
            pub[num] = {"fp": fp, "since": None}          # первый замер: момент смены неизвестен
        elif pub[num]["fp"] != fp:
            pub[num] = {"fp": fp, "since": now_iso()}
        v["changed_at"] = pub[num]["since"]
    changed = write_status(state, snap, sync_info)
    if not DRY:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, ensure_ascii=False))
    if changed and "--no-publish" not in sys.argv and not DRY:
        import publish
        publish.main(message="Auto: RBC status")
    if failures:
        alert("Unistream map: автообновление РБК — " + " | ".join(failures)[:600])
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:              # noqa: BLE001
        log(traceback.format_exc()[-800:])
        alert(f"Unistream map: rbc_sync упал: {type(e).__name__}: {str(e)[-300:]}")
        sys.exit(1)
