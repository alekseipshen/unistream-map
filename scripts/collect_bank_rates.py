#!/usr/bin/env python3
"""Курсы банков по городу (mainfin.ru) для точек на карте, которых нет на banki.ru.

banki.ru даёт курс по конкретному офису, но публикуют туда всего ~20 банков.
mainfin.ru покрывает ~50 банков (ВТБ, Газпромбанк, РСХБ, Совкомбанк, Райффайзен,
ПСБ, Русский Стандарт, Фора-Банк, Авангард, Уралсиб и др.), но курсом ПО ГОРОДУ.
На сайте такие курсы помечаются отдельно — в конкретном офисе может отличаться.

  python3 collect_bank_rates.py [--dry-run]
"""
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parent
SSH_HOST = "ruvds"
RADIUS_M = 1000
OUR_BANK_MARKERS = ("юнистрим", "unistream")

# Ключи — уже нормализованные имена из OSM, значения — нормализованные имена в источнике.
# Нужны там, где названия расходятся сильнее, чем справляется norm().
ALIASES = {
    "промсвязь": "псб",
    "уральскийреконструкцииииразвития": "убрир",
    "уральскийреконструкциииразвития": "убрир",
    "тинькофф": "т",
    "сберроссии": "сбер",
    "юнистрим": None,   # свои точки конкурентами не считаем
}


def log(m):
    print(m, file=sys.stderr)


def norm(s):
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"\b(пао|ао|оао|зао|ооо|акб|кб|банк[а-я]*)\b", " ", s)
    s = re.sub(r"банк|bank", "", s)
    return re.sub(r"[^a-zа-я0-9]+", "", s)


def load_js_array(name, const):
    src = (ROOT / "data" / name).read_text()
    return json.loads(re.search(rf"const {const} = (\[.*?\]);", src, re.S).group(1))


def remote(script_name, timeout=600):
    script = (HERE / script_name).read_bytes()
    p = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", SSH_HOST, "python3 -"],
        input=script, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"ssh/{script_name} failed: {p.stderr.decode()[:300]}")
    return json.loads(p.stdout.decode())


def fetch_mainfin():
    d = remote("remote_fetch_mainfin.py", timeout=300)
    log(f"  mainfin: {len(d['banks'])} банков, {len(d.get('offices', []))} отделений")
    return d["banks"], d.get("offices", [])


def dist_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def build_mainfin_offices(offices, banks, branches, known):
    """Отделения mainfin с адресами → координаты через геокодер → те, что в 1 км от нас.

    У mainfin курсы именно по отделению (у 9 банков они между офисами различаются),
    поэтому это полноценные офисные данные, а не оценка по городу."""
    import geocode
    names = {b["alias"]: b["name"] for b in banks}
    addrs = sorted({o["address"] for o in offices})
    coords = geocode.geocode_many(addrs, limit=40)  # новые адреса добираем порциями

    out, seen = [], set()
    for o in offices:
        c = coords.get(o["address"])
        if not c:
            continue
        key = (o["alias"], round(c["lat"], 5), round(c["lon"], 5))
        if key in seen:      # mainfin повторяет один и тот же офис несколько раз
            continue
        seen.add(key)
        near = sorted(({"num": b["num"], "d": round(dist_m(b["lat"], b["lon"], c["lat"], c["lon"]))}
                       for b in branches
                       if dist_m(b["lat"], b["lon"], c["lat"], c["lon"]) <= RADIUS_M),
                      key=lambda x: x["d"])
        if not near:
            continue
        bank = names.get(o["alias"], o["alias"])
        if any(m in bank.lower() for m in OUR_BANK_MARKERS):
            continue
        # тот же офис уже есть с banki.ru (там больше валют) — не дублируем
        if any(norm(k["bank"]) == norm(bank)
               and dist_m(k["lat"], k["lon"], c["lat"], c["lon"]) < 150 for k in known):
            continue
        out.append({
            "bank": bank, "address": o["address"], "lat": c["lat"], "lon": c["lon"],
            "rates": o["rates"], "at": o["at"], "near": near,
        })
    out.sort(key=lambda x: (x["near"][0]["num"], x["near"][0]["d"]))
    return out


def office_rates_by_bank():
    """Курсы из banki.ru, сгруппированные по банку: там курс каждого офиса отдельно.

    Если у банка во всех офисах Москвы курс совпадает — берём его вместо городского
    из mainfin: это фактические данные, а не оценка. Заодно избавляет от ситуации,
    когда рядом две точки одного банка показывают разные цифры из разных источников."""
    offices = remote("remote_fetch_banki.py")["offices"]
    grouped = {}
    for o in offices:
        for cur, r in o["rates"].items():
            if r.get("buy") and r.get("sell"):
                grouped.setdefault(norm(o["bank"]), {}).setdefault(cur, []).append(
                    (r["buy"], r["sell"]))
    uniform = {}
    for bank, curs in grouped.items():
        rates = {}
        for cur, vals in curs.items():
            if len(vals) >= 2 and len(set(vals)) == 1:  # во всех офисах одно и то же
                rates[cur] = {"buy": vals[0][0], "sell": vals[0][1]}
        if rates:
            uniform[bank] = rates
    return uniform


def main():
    dry = "--dry-run" in sys.argv
    banks, mf_offices = fetch_mainfin()
    if len(banks) < 20:
        raise RuntimeError(f"подозрительно мало банков: {len(banks)}")
    by_norm = {}
    for b in banks:
        by_norm.setdefault(norm(b["name"]), b)

    poi = load_js_array("poi.js", "POI")
    mapping, unmatched = {}, {}
    for p in poi:
        n = p["n"]
        if n in mapping or n in unmatched:
            continue
        key = norm(n)
        if key in ALIASES:
            if ALIASES[key] is None:  # явно исключён
                continue
            key = ALIASES[key]
        b = by_norm.get(key)
        if b:
            mapping[n] = b["alias"]
        else:
            unmatched[n] = True

    covered = sum(1 for p in poi if p["n"] in mapping)
    log(f"банков в источнике: {len(banks)} | точек с курсом: {covered} из {len(poi)}"
        f" ({covered * 100 // max(len(poi), 1)}%)")

    uniform = office_rates_by_bank()
    used, overridden = {}, []
    for b in banks:
        if b["alias"] not in set(mapping.values()):
            continue
        entry = {"name": b["name"], "rates": dict(b["rates"]), "at": b["at"]}
        same = uniform.get(norm(b["name"]))
        if same:
            entry["rates"].update(same)   # факт с banki.ru важнее оценки по городу
            entry["same"] = True
            overridden.append(b["name"])
        used[b["alias"]] = entry
    if overridden:
        log("курс подтверждён офисными данными banki.ru: " + ", ".join(sorted(overridden)))

    branches = load_js_array("branches.js", "BRANCHES")
    known = load_js_array("competitors.js", "COMPETITORS")
    mf = build_mainfin_offices(mf_offices, banks, branches, known)
    per = {}
    for o in mf:
        for n in o["near"]:
            per[n["num"]] = per.get(n["num"], 0) + 1
    log(f"отделений mainfin в радиусе 1 км: {len(mf)} "
        f"({', '.join(f'{k}:{v}' for k, v in sorted(per.items()))})")

    if dry:
        log("не совпало: " + ", ".join(sorted(unmatched)[:20]))
        log("--dry-run: файл не записан")
        return

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (ROOT / "data" / "bankrates.js").write_text(
        f"// generated {stamp}\nconst BANK_RATES = "
        + json.dumps(used, ensure_ascii=False, separators=(",", ":"))
        + ";\nconst POI_BANK = "
        + json.dumps(mapping, ensure_ascii=False, separators=(",", ":"))
        + f";\nconst BANK_RATES_AT = {json.dumps(stamp)};\n")
    (ROOT / "data" / "offices_mainfin.js").write_text(
        f"// generated {stamp}\nconst OFFICES_MF = "
        + json.dumps(mf, ensure_ascii=False, separators=(",", ":")) + ";\n")

    status = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--",
                             "data/bankrates.js"], capture_output=True, text=True).stdout
    if not status.strip():
        log("без изменений")
        return
    body = subprocess.run(["git", "-C", str(ROOT), "diff", "--", "data/bankrates.js"],
                          capture_output=True, text=True).stdout
    meaningful = [l for l in body.splitlines()
                  if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))
                  and "generated" not in l and "BANK_RATES_AT" not in l]
    if body and not meaningful:
        subprocess.run(["git", "-C", str(ROOT), "checkout", "--", "data/bankrates.js"])
        log("изменился только таймстемп — откатили")
        return
    subprocess.run(["git", "-C", str(ROOT), "add", "data/bankrates.js"])
    subprocess.run(["git", "-C", str(ROOT), "-c", "user.name=Alex Pshenichnikov",
                    "-c", "user.email=al.pshen@gmail.com", "commit", "-q",
                    "-m", "Auto: refresh city-level bank rates"])
    subprocess.run(["git", "-C", str(ROOT), "push", "-q"])
    log("запушено")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ОШИБКА: {e}")
        if "--dry-run" not in sys.argv:
            sys.path.insert(0, str(HERE))
            from collect_competitors import alert
            alert(f"Unistream map: сборщик курсов банков (mainfin) упал.\n{type(e).__name__}: {e}")
        sys.exit(1)
