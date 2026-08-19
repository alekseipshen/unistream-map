#!/usr/bin/env python3
"""Геокодирование адресов отделений через Nominatim с постоянным кэшем.

У mainfin есть адреса отделений, но нет координат. Nominatim просит не чаще
1 запроса в секунду, поэтому результат кэшируется в scripts/state/geocode.json —
при обычном прогоне сборщика новых адресов почти не бывает.

  python3 geocode.py --fill    # разово прогреть кэш по всем адресам mainfin
"""
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "state" / "geocode.json"
UA = {"User-Agent": "unistream-branch-map/1.0 (internal tool; al.pshen@gmail.com)"}
# рамка Москвы: за её пределами результат считаем промахом
BOX = (36.9, 56.05, 38.0, 55.35)  # west, north, east, south


def load_cache():
    if CACHE.exists():
        return json.loads(CACHE.read_text())
    return {}


def save_cache(c):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(c, ensure_ascii=False, indent=0))


# Nominatim не понимает канцелярские сокращения — разворачиваем
ABBR = [
    (r"\bпр-?д\b\.?", "проезд"), (r"\bпр-?кт\b\.?", "проспект"), (r"\bпр-?т\b\.?", "проспект"),
    (r"\bш\.", "шоссе"), (r"\bпер\.", "переулок"), (r"\bпл\.", "площадь"),
    (r"\bб-?р\b\.?", "бульвар"), (r"\bнаб\.", "набережная"), (r"\bул\.", "улица"),
    (r"\bпроспа?\b", "проспект"), (r"\bкорп\.?\s*", "к"), (r"\bстр\.?\s*", "с"),
]


def variants(addr):
    """Несколько вариантов записи адреса — от точного к упрощённому."""
    a = re.sub(r"^\s*(г\.?\s*)?Москва\s*,?\s*", "", addr.strip(), flags=re.I)
    a = re.sub(r"\s+", " ", a)
    for pat, rep in ABBR:
        a = re.sub(pat, rep, a, flags=re.I)
    a = re.sub(r"\bд\.\s*", "", a)                       # «д. 5» → «5»
    a = re.sub(r"\s+(ТК|ТЦ|БЦ|ТР|ТРЦ)\b.*$", "", a, flags=re.I)  # хвосты вроде «ТЦ Филион»
    out = [a]
    no_build = re.sub(r"[,\s]*(с|к)\s*\d+[А-Яа-я]?\s*$", "", a)  # без строения/корпуса
    if no_build != a:
        out.append(no_build)
    m = re.match(r"^(.*?),\s*(\d+)", no_build)           # только улица + номер дома
    if m and m.group(0) != no_build:
        out.append(f"{m.group(1)}, {m.group(2)}")
    return [f"Москва, {v.strip(' ,')}" for v in out]


def geocode_one(addr):
    for q_addr in variants(addr):
        q = urllib.parse.urlencode({
            "q": q_addr, "format": "json", "limit": 1, "countrycodes": "ru",
            "viewbox": ",".join(map(str, BOX)), "bounded": 1,
        })
        req = urllib.request.Request(f"https://nominatim.openstreetmap.org/search?{q}", headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        if d:
            lat, lon = float(d[0]["lat"]), float(d[0]["lon"])
            if BOX[3] <= lat <= BOX[1] and BOX[0] <= lon <= BOX[2]:
                return {"lat": round(lat, 6), "lon": round(lon, 6)}
        time.sleep(1.1)
    return None


def reverse_many(points, limit=None, pause=1.1):
    """points: [(key, lat, lon)] → {key: "улица, дом"}. Для точек OSM без адреса."""
    cache = load_cache()
    new = 0
    for key, lat, lon in points:
        k = f"rev:{lat:.5f},{lon:.5f}"
        if k in cache:
            continue
        if limit is not None and new >= limit:
            break
        try:
            q = urllib.parse.urlencode({"lat": lat, "lon": lon, "format": "json",
                                        "zoom": 18, "addressdetails": 1})
            req = urllib.request.Request(
                f"https://nominatim.openstreetmap.org/reverse?{q}", headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            a = d.get("address", {})
            road = a.get("road") or a.get("pedestrian") or a.get("suburb") or ""
            house = a.get("house_number") or ""
            cache[k] = (f"{road}, {house}".strip(" ,") or None)
        except Exception as e:
            print(f"  reverse: {lat},{lon} — {e}", file=sys.stderr)
            cache[k] = None
        new += 1
        time.sleep(pause)
    if new:
        save_cache(cache)
        print(f"  обратным геокодированием получено адресов: {new}", file=sys.stderr)
    return {key: cache.get(f"rev:{lat:.5f},{lon:.5f}") for key, lat, lon in points}


def geocode_many(addresses, limit=None, pause=1.1):
    """Возвращает {адрес: {lat, lon} | None}. limit — сколько НОВЫХ адресов брать за раз."""
    cache = load_cache()
    new = 0
    for a in addresses:
        if a in cache:
            continue
        if limit is not None and new >= limit:
            break
        try:
            cache[a] = geocode_one(a)
        except Exception as e:
            print(f"  геокодер: {a[:40]} — {e}", file=sys.stderr)
            cache[a] = None
        new += 1
        time.sleep(pause)
    if new:
        save_cache(cache)
        print(f"  геокодировано новых адресов: {new}", file=sys.stderr)
    return {a: cache.get(a) for a in addresses}


def main():
    if "--fill" not in sys.argv:
        print(__doc__)
        return
    if "--retry-failed" in sys.argv:   # адреса, не распознанные прошлой версией разбора
        c = load_cache()
        dropped = [k for k, v in c.items() if not v]
        for k in dropped:
            del c[k]
        save_cache(c)
        print(f"сброшено неудачных адресов: {len(dropped)}", file=sys.stderr)
    p = subprocess.run(["ssh", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", "ruvds", "python3 -"],
                       input=(HERE / "remote_fetch_mainfin.py").read_bytes(),
                       capture_output=True, timeout=300)
    offices = json.loads(p.stdout.decode())["offices"]
    addrs = sorted({o["address"] for o in offices})
    print(f"адресов к геокодированию: {len(addrs)}", file=sys.stderr)
    res = geocode_many(addrs)
    ok = sum(1 for v in res.values() if v)
    print(f"успешно: {ok} из {len(addrs)}", file=sys.stderr)


if __name__ == "__main__":
    main()
