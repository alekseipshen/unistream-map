#!/usr/bin/env python3
"""Банки и обменники рядом с нашими отделениями по данным OpenStreetMap.

Это точки БЕЗ курсов — они дополняют banki.ru (там всего ~20 банков публикуют
курсы). Нужны, чтобы папа видел, кто вообще стоит рядом. Данные меняются редко,
запускать раз в месяц-два: python3 collect_osm_poi.py
"""
import json
import math
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RADIUS_M = 1000
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
SELECTORS = ["[amenity=bank]", "[amenity=bureau_de_change]", "[office=financial]"]
# считаем дублем точки banki.ru, если ближе 70 м и банк тот же
DEDUPE_M = 70


def dist_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def norm_bank(s):
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"(банк|bank|пао|ао|оао|зао|«|»|\"|'|\(|\)|\.|,|-)", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load_js_array(name, const):
    src = (ROOT / "data" / name).read_text()
    return json.loads(re.search(rf"const {const} = (\[.*?\]);", src, re.S).group(1))
def load_all_branches():
    """Основные 16 плюс дежурные — конкуренты считаются вокруг всех сразу."""
    out = load_js_array("branches.js", "BRANCHES")
    if (ROOT / "data" / "branches_duty.js").exists():
        out += load_js_array("branches_duty.js", "BRANCHES_DUTY")
    return out


def fetch_osm(branches):
    parts = []
    for b in branches:
        for sel in SELECTORS:
            parts.append(f'nwr(around:{RADIUS_M},{b["lat"]},{b["lon"]}){sel};')
    q = "[out:json][timeout:180];(" + "".join(parts) + ");out center tags;"
    data = urllib.parse.urlencode({"data": q}).encode()
    last = None
    for mirror in OVERPASS_MIRRORS:
        try:
            req = urllib.request.Request(mirror, data=data,
                                         headers={"User-Agent": "unistream-map/1.0"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)["elements"]
        except Exception as e:  # зеркала регулярно отдают 429/504
            last = e
            print(f"  {mirror.split('/')[2]}: {e}", file=sys.stderr)
    raise RuntimeError(f"все зеркала Overpass недоступны: {last}")


def main():
    branches = load_all_branches()
    try:
        comps = load_js_array("competitors.js", "COMPETITORS")
    except FileNotFoundError:
        comps = []
    els = fetch_osm(branches)
    print(f"OSM вернул: {len(els)}", file=sys.stderr)

    out, seen = [], set()
    for e in els:
        lat = e.get("lat") or (e.get("center") or {}).get("lat")
        lon = e.get("lon") or (e.get("center") or {}).get("lon")
        t = e.get("tags") or {}
        name = t.get("name") or t.get("operator") or ""
        if not lat or not lon or not name:
            continue
        key = (norm_bank(name), round(lat, 4), round(lon, 4))
        if key in seen:
            continue
        seen.add(key)
        # уже есть в banki.ru с курсами — не дублируем серой точкой
        if any(dist_m(lat, lon, c["lat"], c["lon"]) < DEDUPE_M
               and norm_bank(c["bank"])[:6] == norm_bank(name)[:6] for c in comps):
            continue
        near = sorted(
            ({"num": b["num"], "d": round(dist_m(b["lat"], b["lon"], lat, lon))}
             for b in branches if dist_m(b["lat"], b["lon"], lat, lon) <= RADIUS_M),
            key=lambda x: x["d"])
        if not near:
            continue
        out.append({
            "n": name,
            "kind": "exchange" if t.get("amenity") == "bureau_de_change" else "bank",
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "addr": " ".join(x for x in [t.get("addr:street"), t.get("addr:housenumber")] if x),
            "near": near,
        })

    # у большинства точек OSM адреса нет — добираем обратным геокодированием (кэш на диске)
    sys.path.insert(0, str(HERE))
    import geocode
    missing = [(i, p["lat"], p["lon"]) for i, p in enumerate(out) if not p["addr"]]
    if missing:
        print(f"без адреса: {len(missing)}, запрашиваем…", file=sys.stderr)
        found = geocode.reverse_many(missing, limit=400)  # ~1 запрос в секунду, не больше часа
        for i, addr in found.items():
            if addr:
                out[i]["addr"] = addr
    print(f"с адресом: {sum(1 for p in out if p['addr'])} из {len(out)}", file=sys.stderr)

    out.sort(key=lambda x: (x["near"][0]["num"], x["near"][0]["d"]))
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (ROOT / "data" / "poi.js").write_text(
        f"// generated {stamp}\nconst POI = "
        + json.dumps(out, ensure_ascii=False, separators=(",", ":"))
        + f";\nconst POI_AT = {json.dumps(stamp)};\n")
    print(f"точек без курсов: {len(out)} (обменников: "
          f"{sum(1 for x in out if x['kind'] == 'exchange')})", file=sys.stderr)


if __name__ == "__main__":
    main()
