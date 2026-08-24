#!/usr/bin/env python3
"""Собирает конкурентов в радиусе 1 км от наших отделений и их курсы.

Курсы тянутся с banki.ru, который доступен только с российского IP, поэтому
запрос выполняется на RuVDS через ssh (см. remote_fetch_banki.py).

  python3 collect_competitors.py            # собрать, записать data/competitors.js, закоммитить
  python3 collect_competitors.py --dry-run  # только показать, что получилось
"""
import json
import math
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RADIUS_M = 1000
SSH_HOST = "ruvds"
OUR_BANK_MARKERS = ("юнистрим", "unistream")

# Один прогон сервиса запускает два сборщика подряд, и обоим нужны одни и те же
# офисы banki.ru — раньше каждый ходил за ними сам, то есть 20 запросов к
# источнику каждые 30 минут вместо 10. Именно на этом banki.ru начинал рвать
# соединение. Сборщики идут друг за другом за секунды, данные заведомо те же.
CACHE = HERE / "state" / "banki_offices.json"
CACHE_TTL = 900


def log(msg):
    print(msg, file=sys.stderr)


def dist_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def load_branches(with_duty=True):
    """Основные 16 плюс дежурные — конкуренты считаются вокруг всех сразу."""
    src = (ROOT / "data" / "branches.js").read_text()
    out = json.loads(re.search(r"const BRANCHES = (\[.*?\]);", src, re.S).group(1))
    duty = ROOT / "data" / "branches_duty.js"
    if with_duty and duty.exists():
        out += json.loads(re.search(r"const BRANCHES_DUTY = (\[.*?\]);",
                                    duty.read_text(), re.S).group(1))
    return out


def cached_offices(max_age=CACHE_TTL):
    """Офисы из свежего кэша или None. Общая точка для обоих сборщиков."""
    try:
        age = time.time() - CACHE.stat().st_mtime
        if age <= max_age:
            return json.loads(CACHE.read_text())["offices"], age
    except (OSError, ValueError, KeyError):
        pass          # кэша нет или он битый — просто сходим на источник
    return None, None


def fetch_offices():
    """Запускает remote_fetch_banki.py на RuVDS, возвращает список офисов."""
    script = (HERE / "remote_fetch_banki.py").read_bytes()
    p = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", SSH_HOST, "python3 -"],
        input=script, capture_output=True, timeout=600)
    if p.returncode != 0:
        # хвост, а не начало: в начале идёт построчный прогресс по валютам,
        # и он вытеснял из алерта саму ошибку
        raise RuntimeError(f"ssh/fetch failed: ...{p.stderr.decode()[-400:]}")
    for line in p.stderr.decode().strip().splitlines():
        log("  banki.ru " + line)
    offices = json.loads(p.stdout.decode())["offices"]
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"offices": offices}, ensure_ascii=False))
    except OSError as e:
        log(f"  кэш не записан ({e}) — не критично, следующий сборщик сходит сам")
    return offices


def build(offices, branches):
    out = []
    for o in offices:
        if any(m in o["bank"].lower() for m in OUR_BANK_MARKERS):
            continue  # свои точки конкурентами не считаем
        near = []
        for b in branches:
            d = dist_m(b["lat"], b["lon"], o["lat"], o["lon"])
            if d <= RADIUS_M:
                near.append({"num": b["num"], "d": round(d)})
        if not near:
            continue
        near.sort(key=lambda x: x["d"])
        rates = {c: {"buy": r["buy"], "sell": r["sell"]}
                 for c, r in o["rates"].items() if r.get("buy") or r.get("sell")}
        if not rates:
            continue
        stamps = [r["at"] for r in o["rates"].values() if r.get("at")]
        out.append({
            "id": o["id"],
            "bank": o["bank"],
            "name": o["name"],
            "address": o["address"],
            "metro": o["metro"],
            "lat": o["lat"],
            "lon": o["lon"],
            "rates": rates,
            "at": max(stamps) if stamps else None,
            "near": near,
        })
    out.sort(key=lambda x: (x["near"][0]["num"], x["near"][0]["d"]))
    return out


def write_js(competitors):
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path = ROOT / "data" / "competitors.js"
    body = json.dumps(competitors, ensure_ascii=False, separators=(",", ":"))
    path.write_text(f"// generated {stamp}\nconst COMPETITORS = {body};\n"
                    f"const COMPETITORS_AT = {json.dumps(stamp)};\n")
    return path


def git(*args, check=True):
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True, check=check)


def commit_and_push():
    status = git("status", "--porcelain", "--", "data/competitors.js", check=False).stdout
    if not status.strip():
        log("данные не изменились — коммит не нужен")
        return False
    if status.lstrip().startswith("??"):  # файл ещё не в индексе — коммитим как есть
        git("add", "data/competitors.js", "data/volatility.js")
        git("-c", "user.name=Alex Pshenichnikov", "-c", "user.email=al.pshen@gmail.com",
            "commit", "-q", "-m", "Auto: refresh competitor rates")
        git("push", "-q")
        log("запушено (новый файл)")
        return True
    # сравниваем без строки-таймстемпа, чтобы не коммитить одинаковые данные
    body = git("diff", "--", "data/competitors.js", check=False).stdout
    meaningful = [l for l in body.splitlines()
                  if l.startswith(("+", "-")) and "generated" not in l
                  and "COMPETITORS_AT" not in l and not l.startswith(("+++", "---"))]
    if not meaningful:
        git("checkout", "--", "data/competitors.js", check=False)
        log("изменился только таймстемп — откатили")
        return False
    git("add", "data/competitors.js", "data/volatility.js")
    git("-c", "user.name=Alex Pshenichnikov", "-c", "user.email=al.pshen@gmail.com",
        "commit", "-q", "-m", "Auto: refresh competitor rates")
    git("push", "-q")
    log("запушено")
    return True


def alert(text):
    """Пишет в топик Alert командного центра — иначе сбой сборщика останется незамеченным."""
    sender = Path("/root/My-Digital-Brain/scripts/weekly-loop/tg_send.py")
    if not sender.exists():
        return
    subprocess.run([sys.executable, str(sender), "--chat", "-1003488817834",
                    "--thread", "7113", "--text", text],
                   capture_output=True, timeout=60)


def main():
    dry = "--dry-run" in sys.argv
    branches = load_branches()
    offices = fetch_offices()
    if len(offices) < 100:  # обычно ~470; резкое падение = banki.ru сменил API или блок
        raise RuntimeError(f"подозрительно мало офисов: {len(offices)}")
    log(f"офисов с курсами в Москве: {len(offices)}")
    comps = build(offices, branches)
    per = {}
    for c in comps:
        for n in c["near"]:
            per[n["num"]] = per.get(n["num"], 0) + 1
    log(f"конкурентов в радиусе {RADIUS_M} м: {len(comps)}")
    log("по отделениям: " + ", ".join(f"{k}:{per.get(k, 0)}" for k in sorted(b['num'] for b in branches)))
    if dry:
        log("--dry-run: файл не записан")
        return
    write_js(comps)

    # история изменений: сколько раз банк реально переставил курс между нашими замерами
    sys.path.insert(0, str(HERE))
    import rate_history as rh
    state, changed = rh.record(rh.entries_from_competitors(comps),
                               datetime.now(timezone.utc).isoformat(timespec="seconds"))
    rh.save_state(state)
    st, days = rh.write_volatility(state)
    log(f"история: изменений с прошлого замера {changed}, окно {days:.1f} дн., банков {len(st)}")

    if "--no-commit" not in sys.argv:   # под systemd публикует publish.py — одним коммитом
        commit_and_push()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ОШИБКА: {e}")
        if "--dry-run" not in sys.argv:
            alert(f"Unistream map: сборщик курсов конкурентов упал.\n{type(e).__name__}: {e}")
        sys.exit(1)
