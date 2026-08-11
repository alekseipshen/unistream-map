#!/usr/bin/env python3
"""Собирает data/branches.js и data/metro.js для сайта.

Запуск с обновлением из API:   python3 build_data.py --fetch
Запуск из локальных JSON:      python3 build_data.py
"""
import json
import math
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE.parent / "data"

# Отделения, которые курирует Игорь: номер -> id в системе Юнистрим
BRANCH_IDS = json.load(open(HERE / "branch-ids.json"))


def fetch_json(url, lang="ru"):
    req = urllib.request.Request(url, headers={
        "Accept-Language": lang,
        "User-Agent": "Mozilla/5.0 (branch-map builder)",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def load_branches(fetch):
    if fetch:
        out = {}
        for num, bid in BRANCH_IDS.items():
            out[num] = fetch_json(f"https://unistream.ru/api/poses/exchange/{bid}")
            print(f"  {num} -> {bid} ok", file=sys.stderr)
            time.sleep(1.5)
        json.dump(out, open(HERE / "branches-full.json", "w"),
                  ensure_ascii=False, indent=1)
        return out
    return json.load(open(HERE / "branches-full.json"))


def load_metro(fetch):
    if fetch:
        d = fetch_json("https://api.hh.ru/metro/1")
        json.dump(d, open(HERE / "metro-hh.json", "w"), ensure_ascii=False, indent=1)
        return d
    return json.load(open(HERE / "metro-hh.json"))


def clean_address(addr):
    """Убирает почтовый индекс и упоминание Москвы из начала адреса."""
    a = addr.strip()
    a = re.sub(r"^\d{6}\s*,\s*", "", a)
    a = re.sub(r"^(г\.?\s*Москва|Москва\s*г\.?|МОСКВА\s*Г\.?)\s*,\s*", "", a, flags=re.I)
    return a.strip()


def dist_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def build_metro(raw):
    """Группирует станции по названию; одноимённые станции дальше 900 м
    друг от друга остаются отдельными записями с уточнением линии."""
    by_name = {}
    for line in raw["lines"]:
        lname = line["name"]
        color = "#" + line["hex_color"]
        for st in line["stations"]:
            by_name.setdefault(st["name"], []).append(
                {"lat": st["lat"], "lng": st["lng"], "line": lname, "color": color})

    stations = []
    for name, pts in by_name.items():
        clusters = []
        for p in pts:
            placed = False
            for c in clusters:
                if dist_m(p["lat"], p["lng"], c[0]["lat"], c[0]["lng"]) < 900:
                    c.append(p)
                    placed = True
                    break
            if not placed:
                clusters.append([p])
        for c in clusters:
            label = name if len(clusters) == 1 else f"{name} ({c[0]['line']})"
            stations.append({
                "n": label,
                "lat": round(sum(p["lat"] for p in c) / len(c), 6),
                "lng": round(sum(p["lng"] for p in c) / len(c), 6),
                "l": [{"n": p["line"], "c": p["color"]} for p in c],
            })
    stations.sort(key=lambda s: s["n"])
    return stations


RING_LINES = {"Кольцевая", "МЦК", "Большая кольцевая линия"}

# У hh.ru у трёх станций битые координаты (lng=lat или мусор) — правим вручную
COORD_FIXES = {
    "Фабричная": (55.5735, 38.2065),
    "Раменское": (55.5646, 38.2250),
    "Чухлинка": (55.7324, 37.7639),
    "Лобня": (56.0135, 37.4849),      # у hh точка в 12 км западнее станции
    "Сколково": (55.7003, 37.3428),   # у hh точка кампуса, а не ж/д платформы
}


def fix_coords(raw):
    for line in raw["lines"]:
        for s in line["stations"]:
            if s["name"] in COORD_FIXES:
                s["lat"], s["lng"] = COORD_FIXES[s["name"]]


def path_len(pts):
    return sum(dist_m(pts[i]["lat"], pts[i]["lng"], pts[i + 1]["lat"], pts[i + 1]["lng"])
               for i in range(len(pts) - 1))


def reorder_path(sts):
    """Жадный ближайший сосед с каждого старта + 2-opt. Чинит битый order у hh."""
    best = None
    for start in range(len(sts)):
        rest = list(sts)
        path = [rest.pop(start)]
        while rest:
            i = min(range(len(rest)),
                    key=lambda j: dist_m(path[-1]["lat"], path[-1]["lng"],
                                         rest[j]["lat"], rest[j]["lng"]))
            path.append(rest.pop(i))
        length = path_len(path)
        if best is None or length < best[0]:
            best = (length, path)
    path = best[1]
    improved = True
    while improved:
        improved = False
        for i in range(len(path) - 2):
            for j in range(i + 2, len(path)):
                cand = path[:i + 1] + path[i + 1:j + 1][::-1] + path[j + 1:]
                if path_len(cand) < path_len(path) - 1:
                    path, improved = cand, True
    return path


def build_metro_lines(raw):
    """Линии метро для отрисовки на карте: цвет + станции по порядку.

    order у hh.ru у нескольких линий сломан (сегменты по 40 км через весь город),
    поэтому строим и жадный маршрут; берём вариант с меньшей общей длиной."""
    lines = []
    for line in raw["lines"]:
        sts = sorted(line["stations"], key=lambda s: s["order"])
        if line["name"] not in RING_LINES and len(sts) > 2:
            alt = reorder_path(sts)
            if path_len(alt) < path_len(sts) * 0.97:
                sts = alt
        lines.append({
            "n": line["name"],
            "c": "#" + line["hex_color"],
            "ring": line["name"] in RING_LINES,
            "s": [{"n": s["name"], "lat": s["lat"], "lng": s["lng"]} for s in sts],
        })
    return lines


def build_branches(raw):
    out = []
    for num in sorted(raw):
        b = raw[num]
        rates = [
            {"cur": r["currency"], "buy": r["buyRate"], "sell": r["sellRate"],
             "at": r.get("lastUpdated")}
            for r in (b.get("exchangeRates") or [])
        ]
        out.append({
            "num": num,
            "id": b["id"],
            "address": clean_address(b.get("address") or ""),
            "notes": (b.get("addressNotes") or "").strip(),
            "lat": b["location"]["lat"],
            "lon": b["location"]["lon"],
            "phone": b.get("phone") or "",
            "metro": [m["name"] for m in (b.get("metro") or [])],
            "hours": [
                {"d": h["weekday"], "o": h["open"], "c": h["close"]}
                for h in b["workingSchedule"]["workingHours"]
            ],
            "hoursNotes": (b["workingSchedule"].get("workingHoursNotes") or "").strip(),
            "currencies": b.get("currencies") or [],
            "rates": rates,
        })
    return out


def main():
    fetch = "--fetch" in sys.argv
    raw_metro = load_metro(fetch)
    fix_coords(raw_metro)
    branches = build_branches(load_branches(fetch))
    metro = build_metro(raw_metro)
    metro_lines = build_metro_lines(raw_metro)
    OUT.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(OUT / "branches.js", "w") as f:
        f.write(f"// generated {stamp}\nconst BRANCHES = ")
        json.dump(branches, f, ensure_ascii=False, separators=(",", ":"))
        f.write(f";\nconst DATA_BUILT_AT = {json.dumps(stamp)};\n")
    with open(OUT / "metro.js", "w") as f:
        f.write(f"// generated {stamp}\nconst METRO = ")
        json.dump(metro, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\nconst METRO_LINES = ")
        json.dump(metro_lines, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    print(f"branches: {len(branches)}, stations: {len(metro)}, lines: {len(metro_lines)}")


if __name__ == "__main__":
    main()
