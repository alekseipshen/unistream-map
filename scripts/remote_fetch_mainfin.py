#!/usr/bin/env python3
"""Курсы наличной валюты по банкам Москвы с mainfin.ru. Печатает JSON в stdout.

Нужен для банков, которых нет на banki.ru (ВТБ, Газпромбанк, РСХБ, Совкомбанк,
Райффайзен, Русский Стандарт, Фора-Банк и др.). Это курс банка ПО ГОРОДУ,
а не по конкретному офису — на сайте помечаем это отдельно.

ВАЖНО: запускать только с российского IP (RuVDS) — с иностранного mainfin отдаёт 403.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request

URL = "https://mainfin.ru/currency/moskva"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def fetch(attempts=3):
    for i in range(attempts):
        try:
            req = urllib.request.Request(URL, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError) as e:
            if i == attempts - 1:
                raise
            print(f"retry {i + 1}: {e}", file=sys.stderr)
            time.sleep(5 * (i + 1))


def parse(html):
    banks = []
    for alias, body in re.findall(r'<tr[^>]*data-bank-alias="([a-z0-9_-]+)"[^>]*>(.*?)</tr>',
                                  html, re.S):
        name = re.search(r'alt="([^"]+)"', body)
        rates = {}
        for m in re.finditer(r'id="' + re.escape(alias) + r'_(buy|sell)_(\w+)"[^>]*'
                             r'data-curse-val="([\d.]+)"', body):
            val = float(m.group(3))
            if val > 0:  # 0.00 = банк валюту не меняет
                rates.setdefault(m.group(2).upper(), {})[m.group(1)] = val
        rates = {c: v for c, v in rates.items() if v.get("buy") and v.get("sell")}
        if not rates:
            continue
        when = re.findall(r"(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})", body)
        banks.append({
            "alias": alias,
            "name": (name.group(1) if name else alias).strip(),
            "rates": rates,
            "at": when[0] if when else None,
        })
    return banks


def main():
    banks = parse(fetch())
    print(f"mainfin: {len(banks)} банков с курсами", file=sys.stderr)
    json.dump({"banks": banks}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
