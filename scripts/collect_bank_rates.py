#!/usr/bin/env python3
"""Курсы банков по городу (mainfin.ru) для точек на карте, которых нет на banki.ru.

banki.ru даёт курс по конкретному офису, но публикуют туда всего ~20 банков.
mainfin.ru покрывает ~50 банков (ВТБ, Газпромбанк, РСХБ, Совкомбанк, Райффайзен,
ПСБ, Русский Стандарт, Фора-Банк, Авангард, Уралсиб и др.), но курсом ПО ГОРОДУ.
На сайте такие курсы помечаются отдельно — в конкретном офисе может отличаться.

  python3 collect_bank_rates.py [--dry-run]
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SSH_HOST = "ruvds"

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


def fetch_banks():
    script = (HERE / "remote_fetch_mainfin.py").read_bytes()
    p = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", SSH_HOST, "python3 -"],
        input=script, capture_output=True, timeout=300)
    if p.returncode != 0:
        raise RuntimeError(f"ssh/mainfin failed: {p.stderr.decode()[:300]}")
    log("  " + p.stderr.decode().strip())
    return json.loads(p.stdout.decode())["banks"]


def main():
    dry = "--dry-run" in sys.argv
    banks = fetch_banks()
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
    used = {b["alias"]: {"name": b["name"], "rates": b["rates"], "at": b["at"]}
            for b in banks if b["alias"] in set(mapping.values())}

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
