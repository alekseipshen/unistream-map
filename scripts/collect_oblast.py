#!/usr/bin/env python3
"""Конкуренты вокруг дежурных отделений в Московской области.

Московский датасет banki.ru область не покрывает, поэтому по каждому городу
области берём страницу mainfin.ru — там банки и их отделения с адресами.
Города без своей страницы на mainfin (Мытищи, Люберцы, Пушкино, Видное и др.)
редиректят на Москву — такие пропускаем, иначе в карточки уедут чужие курсы.

  python3 collect_oblast.py [--dry-run]
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
OUR_MARKERS = ("юнистрим", "unistream")

# Проверено вручную: у этих городов на mainfin есть своя страница с курсами.
# Остальные (Мытищи, Люберцы, Пушкино, Видное, Дедовск, Котельники, Андреевка,
# Юдино, Ленинский район, Совхоз им. Ленина) редиректят на Москву — их не берём.
CITY_SLUGS = {
    "Балашиха": "balashiha", "Домодедово": "domodedovo", "Жуковский": "zhukovskiy",
    "Истра": "istra", "Королев": "korolev", "Красногорск": "krasnogorsk",
    "Наро-Фоминск": "naro-fominsk", "Ногинск": "noginsk", "Одинцово": "odincovo",
    "Подольск": "podolsk", "Раменское": "ramenskoe", "Реутов": "reutov",
    "Солнечногорск": "solnechnogorsk", "Химки": "himki", "Щелково": "schelkovo",
}


def log(m):
    print(m, file=sys.stderr)


def dist_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def load_js_array(name, const):
    src = (ROOT / "data" / name).read_text()
    return json.loads(re.search(rf"const {const} = (\[.*?\]);", src, re.S).group(1))


def fetch_cities(slugs):
    """Все города одним ssh-заходом: на RuVDS с 2 ядрами пачка сессий — лишняя нагрузка."""
    import remote as ssh
    d = ssh.run("remote_fetch_mainfin.py", args=",".join(slugs), timeout=1200, log=log)
    return d.get("cities", {})


def main():
    dry = "--dry-run" in sys.argv
    import geocode
    duty = load_js_array("branches_duty.js", "BRANCHES_DUTY")
    by_city = {}
    for b in duty:
        if b.get("city") and b["city"] != "Москва":
            by_city.setdefault(b["city"], []).append(b)

    skipped = sorted(set(by_city) - set(CITY_SLUGS))
    wanted = {c: CITY_SLUGS[c] for c in by_city if c in CITY_SLUGS}
    fetched = fetch_cities(sorted(wanted.values()))

    out = []
    for city, branches in sorted(by_city.items()):
        slug = CITY_SLUGS.get(city)
        if not slug:
            continue
        d = fetched.get(slug)
        if not d:
            log(f"  {city} ({slug}): данных нет, пропускаем")
            continue
        names = {b["alias"]: b["name"] for b in d["banks"]}
        offices = d.get("offices", [])
        coords = geocode.geocode_many(sorted({o["address"] for o in offices}), limit=60)
        added, seen = 0, set()
        for o in offices:
            c = coords.get(o["address"])
            if not c:
                continue
            key = (o["alias"], round(c["lat"], 5), round(c["lon"], 5))
            if key in seen:
                continue
            seen.add(key)
            bank = names.get(o["alias"], o["alias"])
            if any(m in bank.lower() for m in OUR_MARKERS):
                continue
            near = sorted(({"num": b["num"], "d": round(dist_m(b["lat"], b["lon"], c["lat"], c["lon"]))}
                           for b in branches
                           if dist_m(b["lat"], b["lon"], c["lat"], c["lon"]) <= RADIUS_M),
                          key=lambda x: x["d"])
            if not near:
                continue
            out.append({"bank": bank, "address": o["address"], "city": city,
                        "lat": c["lat"], "lon": c["lon"], "rates": o["rates"],
                        "at": o["at"], "near": near})
            added += 1
        log(f"  {city} ({slug}): банков {len(d['banks'])}, отделений {len(offices)}, "
            f"в 1 км от наших — {added}")

    out.sort(key=lambda x: (x["near"][0]["num"], x["near"][0]["d"]))
    per = {}
    for o in out:
        for n in o["near"]:
            per[n["num"]] = per.get(n["num"], 0) + 1
    log(f"итого конкурентов по области: {len(out)} у {len(per)} отделений")
    if skipped:
        log("нет страницы на mainfin (пропущены): " + ", ".join(skipped))
    if dry:
        log("--dry-run: файл не записан")
        return

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (ROOT / "data" / "competitors_oblast.js").write_text(
        f"// generated {stamp}\nconst COMPETITORS_OBLAST = "
        + json.dumps(out, ensure_ascii=False, separators=(",", ":")) + ";\n")
    subprocess.run([sys.executable, str(HERE / "publish.py")])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ОШИБКА: {e}")
        if "--dry-run" not in sys.argv:
            from collect_competitors import alert
            alert(f"Unistream map: сборщик конкурентов по области упал.\n{type(e).__name__}: {e}")
        sys.exit(1)
