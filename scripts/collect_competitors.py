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
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RADIUS_M = 1000
SSH_HOST = "ruvds"
OUR_BANK_MARKERS = ("юнистрим", "unistream")


def log(msg):
    print(msg, file=sys.stderr)


def dist_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def load_branches():
    src = (ROOT / "data" / "branches.js").read_text()
    return json.loads(re.search(r"const BRANCHES = (\[.*?\]);", src, re.S).group(1))


def fetch_offices():
    """Запускает remote_fetch_banki.py на RuVDS, возвращает список офисов."""
    script = (HERE / "remote_fetch_banki.py").read_bytes()
    p = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", SSH_HOST, "python3 -"],
        input=script, capture_output=True, timeout=600)
    if p.returncode != 0:
        raise RuntimeError(f"ssh/fetch failed: {p.stderr.decode()[:400]}")
    for line in p.stderr.decode().strip().splitlines():
        log("  banki.ru " + line)
    return json.loads(p.stdout.decode())["offices"]


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
        git("add", "data/competitors.js")
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
    git("add", "data/competitors.js")
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
    commit_and_push()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ОШИБКА: {e}")
        if "--dry-run" not in sys.argv:
            alert(f"Unistream map: сборщик курсов конкурентов упал.\n{type(e).__name__}: {e}")
        sys.exit(1)
