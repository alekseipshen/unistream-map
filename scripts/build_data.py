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
    branches = build_branches(load_branches(fetch))
    metro = build_metro(load_metro(fetch))
    OUT.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(OUT / "branches.js", "w") as f:
        f.write(f"// generated {stamp}\nconst BRANCHES = ")
        json.dump(branches, f, ensure_ascii=False, separators=(",", ":"))
        f.write(f";\nconst DATA_BUILT_AT = {json.dumps(stamp)};\n")
    with open(OUT / "metro.js", "w") as f:
        f.write(f"// generated {stamp}\nconst METRO = ")
        json.dump(metro, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    print(f"branches: {len(branches)}, stations: {len(metro)}")


if __name__ == "__main__":
    main()
