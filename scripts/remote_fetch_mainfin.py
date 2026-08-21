#!/usr/bin/env python3
"""Курсы наличной валюты по банкам Москвы с mainfin.ru. Печатает JSON в stdout.

Нужен для банков, которых нет на banki.ru (ВТБ, Газпромбанк, РСХБ, Совкомбанк,
Райффайзен, Русский Стандарт, Фора-Банк и др.). Это курс банка ПО ГОРОДУ,
а не по конкретному офису — на сайте помечаем это отдельно.

ВАЖНО: запускать только с российского IP (RuVDS) — с иностранного mainfin отдаёт 403.
"""
import http.cookiejar
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request

CITY = sys.argv[1] if len(sys.argv) > 1 else "moskva"
URL = f"https://mainfin.ru/currency/{CITY}"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Города без своей страницы редиректят на Москву — принимать это нельзя,
    иначе московские курсы уедут в карточки подмосковных отделений."""
    def redirect_request(self, *a, **kw):
        return None


# Анти-бот mainfin периодически отдаёт 403 пачкой на несколько минут: паузы по
# 5-10 с не помогают, через четверть часа тот же запрос проходит. Редирект и 404
# повторять нельзя — это не сбой, а «у города нет своей страницы» (см. NoRedirect),
# и на прогоне по области такой retry только жёг бы время.
RETRY_STATUS = {403, 408, 429, 500, 502, 503, 504}
BACKOFF = (8, 20, 45)


def fetch():
    # Куки живут между попытками: челлендж ставит cookie, со следующего захода
    # с ней страница обычно отдаётся сразу.
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(NoRedirect,
                                         urllib.request.HTTPCookieProcessor(jar))
    for i, pause in enumerate(BACKOFF + (None,)):
        try:
            req = urllib.request.Request(URL, headers=HEADERS)
            with opener.open(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:   # подкласс URLError — ловим первым
            if pause is None or e.code not in RETRY_STATUS:
                raise
            print(f"retry {i + 1} через {pause}s: HTTP {e.code}", file=sys.stderr)
        except (urllib.error.URLError, TimeoutError) as e:
            if pause is None:
                raise
            print(f"retry {i + 1} через {pause}s: {e}", file=sys.stderr)
        time.sleep(pause + random.uniform(0, 3))


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


def parse_offices(html):
    """Строки конкретных отделений: адрес + курсы + время.

    Разметка: <tr class="row toggle-tbl ..."> со ссылкой /bank/{alias}/map/... и
    ячейками <td class="USD curr_hid">покупка</td><td class="USD ...">продажа</td>."""
    offices = []
    for row in re.findall(r"<tr[^>]*class=\"[^\"]*toggle-tbl[^\"]*\"[^>]*>(.*?)</tr>", html, re.S):
        link = re.search(r'href="/bank/([a-z0-9_-]+)/map/[^"]*"[^>]*>([^<]+)</a>', row)
        if not link:
            continue
        alias, address = link.group(1), link.group(2).strip()
        rates = {}
        for cur in ("USD", "EUR", "CNY"):
            vals = re.findall(rf'<td[^>]*class="[^"]*\b{cur}\b[^"]*"[^>]*>\s*([\d.]+)\s*</td>', row)
            if len(vals) >= 2 and float(vals[0]) > 0 and float(vals[1]) > 0:
                rates[cur] = {"buy": float(vals[0]), "sell": float(vals[1])}
        if not rates:
            continue
        when = re.search(r"(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})", row)
        offices.append({
            "alias": alias,
            "address": address,
            "rates": rates,
            "at": when.group(1) if when else None,
        })
    return offices


def main():
    html = fetch()
    banks = parse(html)
    offices = parse_offices(html)
    print(f"mainfin: {len(banks)} банков, {len(offices)} отделений с курсами", file=sys.stderr)
    json.dump({"banks": banks, "offices": offices}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
