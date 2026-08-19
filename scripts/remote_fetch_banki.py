#!/usr/bin/env python3
"""Тянет курсы обменных пунктов Москвы с banki.ru. Печатает JSON в stdout.

ВАЖНО: запускать только с российского IP (RuVDS) — banki.ru отдаёт иностранным
адресам anti-bot заглушку вместо данных. Обязателен заголовок X-Requested-With,
иначе endpoint отвечает 404.
"""
import json
import sys
import time
import urllib.error
import urllib.request

# ISO-код -> цифровой код валюты в API banki.ru
CURRENCIES = {
    "USD": 840, "EUR": 978, "CNY": 156, "AED": 784,
    "GBP": 826, "CHF": 756, "TRY": 949, "KZT": 398,
    "AMD": 51, "ILS": 376,
}
REGION = "moskva"
URL = ("https://www.banki.ru/products/currencyNodejsApi/getExchangesCoordinates/"
       "?currencyId={cid}&regionUrl={region}&currencyCode={code}"
       "&sortAttribute=buy&order=desc&amount=&perPage=1000&page=1")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.banki.ru/products/currency/map/moskva/",
    "Accept": "application/json",
}


def fetch(code, cid, attempts=3):
    url = URL.format(cid=cid, region=REGION, code=code)
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            if i == attempts - 1:
                print(f"WARN {code}: {e}", file=sys.stderr)
                return None
            time.sleep(5 * (i + 1))


def main():
    offices = {}
    for code, cid in CURRENCIES.items():
        d = fetch(code, cid)
        if not d:
            continue
        for o in d.get("list", []):
            c = o.get("coordinates") or {}
            if not c.get("latitude"):
                continue
            oid = o["id"]
            rec = offices.setdefault(oid, {
                "id": oid,
                "bank": o.get("bankName") or "",
                "bankId": o.get("bankId"),
                "name": o.get("name") or "",
                "address": c.get("address") or "",
                "metro": c.get("metroStation") or "",
                "lat": c["latitude"],
                "lon": c["longitude"],
                "rates": {},
            })
            ex = o.get("exchange") or {}
            if ex.get("buy") or ex.get("sale"):
                rec["rates"][code] = {
                    "buy": ex.get("buy"),
                    "sell": ex.get("sale"),
                    "at": ex.get("refreshDate"),
                }
        print(f"{code}: {d.get('totalItems')} offices", file=sys.stderr)
        time.sleep(2)
    json.dump({"offices": list(offices.values())}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
