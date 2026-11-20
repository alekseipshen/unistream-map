#!/usr/bin/env python3
"""Клиент кабинета cash.rbc.ru («Рынок наличной валюты» РБК) — повторяет вызовы самого кабинета.

Как устроен кабинет (разобрано по его private.js, 19.11.2026):
- вход: POST /cash/json/cash_login/ (login, passwd) → cookie cash_session, живёт ~20 ч;
- «пакет» = набор котировок отделения, который сейчас показан на РБК. Пока пакет запущен,
  кнопка «Сохранить» заблокирована, поэтому цикл правки: stop_packet → save_packet → start_packet.
  Время автостопа (конец рабочего дня по режиму кассы) РБК выставляет сам при запуске;
- save_packet: bank_id + rates = строки «валюта_покупка_продажа_сумма_комиссия__порядок» через «;».
  В ответ приходит НОВЫЙ packet_id — запускать надо уже его;
- сохранять можно только в рабочие часы кассы («Bank can save package only in working hours»).

Публичная карточка без входа: /cash/json/cash_bank_info/?bank_id=<id> (CORS закрыт —
браузер с нашего сайта её прочитать не может, поэтому сверка идёт на сервере).
"""
import html
import json
import random
import re
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path

BASE = "https://cash.rbc.ru"
PRIVATE = BASE + "/cash/private/"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
ENV = Path(__file__).resolve().parent.parent / ".env"


class RbcError(Exception):
    pass


def _creds():
    vals = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals["RBC_LOGIN"], vals["RBC_PASSWORD"]


def public_info(bank_id, timeout=20):
    """Что сейчас видно на РБК: {code: {amount: (buy, sell)}}, имя, время публикации."""
    req = urllib.request.Request(f"{BASE}/cash/json/cash_bank_info/?bank_id={bank_id}",
                                 headers={"User-Agent": UA, "Accept": "application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=timeout))
    bank = d.get("bank") or {}
    quotes = {}
    for rows in (bank.get("quotations") or {}).values():
        for r in rows:
            pair = r.get("short_name", "")
            if not pair.endswith("/RUB"):
                continue
            quotes.setdefault(pair[:3], {})[float(r.get("amount") or 1)] = (r.get("buy"), r.get("sell"))
    info = bank.get("info") or {}
    return {"quotes": quotes, "name": info.get("bank_name"), "active": info.get("is_active"),
            # режим работы кассы, по которому РБК пускает пакет: [{week_day 1=пн, time_start, time_end}]
            "work": bank.get("work") or [],
            "published": bank.get("dt_last_published") or d.get("dt_last_published"),
            "raw_keys": sorted(bank.keys())}


class Cabinet:
    def __init__(self, timeout=30):
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.op.addheaders = [("User-Agent", UA), ("X-Requested-With", "XMLHttpRequest"),
                              ("Referer", PRIVATE + "cabinet")]

    def _get(self, url, params=None):
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self.op.open(url, timeout=self.timeout).read().decode("utf-8", "replace")

    def _post(self, url, data):
        body = urllib.parse.urlencode(data).encode()
        return self.op.open(url, data=body, timeout=self.timeout).read().decode("utf-8", "replace")

    def login(self):
        login, pw = _creds()
        resp = self._post(BASE + "/cash/json/cash_login/", {"login": login, "passwd": pw})
        if not any(c.name == "cash_session" for c in self.jar):
            raise RbcError(f"вход в кабинет РБК не удался: {resp[:200]}")
        return self

    def package(self, bank_id):
        """Текущий пакет отделения: запущен ли, packet_id, время автостопа, строки котировок."""
        t = self._get(f"{PRIVATE}package_info_html_fragment/{bank_id}/")
        running = "<script>mask_controls_when_package_starts_success();</script>" in t
        m = re.search(r'stop_time">[^<]*<strong>([^<]*)</strong>', t)
        stop_time = m.group(1).strip() if m else None
        rows, packet_id = [], None
        for tr in re.findall(r'<tr[^>]*id="rates"[^>]*>(.*?)</tr>', t, re.S):
            sel = re.search(r'<select id="currency"[^>]*>(.*?)</select>', tr, re.S)
            cur = re.search(r'<option[^>]*value="(\d+)"[^>]*selected[^>]*>([^<]+)<', sel.group(1)) or \
                re.search(r'<option[^>]*selected[^>]*value="(\d+)"[^>]*>([^<]+)<', sel.group(1)) if sel else None
            if not cur:
                continue
            pid = re.search(r'id="package_id" value="(\d+)"', tr)
            packet_id = packet_id or (pid.group(1) if pid else None)

            def val(name):
                mm = re.search(rf'id="{name}" value="([^"]*)"', tr)
                return mm.group(1).replace(",", ".") if mm else ""
            com = re.search(r'<select id="commission">(.*?)</select>', tr, re.S)
            commission = "1" if com and re.search(r'value="1"\s*selected', com.group(1)) else "0"
            rows.append({"currency_id": cur.group(1), "pair": html.unescape(cur.group(2)).strip(),
                         "bid": val("bid"), "ask": val("ask"), "summa": val("summa"),
                         "commission": commission})
        return {"running": running, "packet_id": packet_id, "stop_time": stop_time, "rows": rows}

    def stop(self, bank_id, packet_id):
        r = json.loads(self._get(PRIVATE + "stop_packet", {"packet_id": packet_id, "bank_id": bank_id}))
        if "time" not in r:
            raise RbcError(f"stop_packet: {r}")
        return r

    def start(self, bank_id, packet_id):
        raw = self._get(PRIVATE + "start_packet", {"packet_id": packet_id, "bank_id": bank_id})
        try:
            r = json.loads(raw)
        except ValueError:
            raise RbcError(f"start_packet: не JSON: {raw[:200]}")
        if "time" not in r:
            raise RbcError(f"start_packet: {r.get('error', r)}")
        return r

    def save(self, bank_id, rows):
        rates = ";".join("_".join([r["currency_id"], r["bid"], r["ask"], r["summa"],
                                   r["commission"], "", str(i)]) for i, r in enumerate(rows))
        r = json.loads(self._post(PRIVATE + "save_packet",
                                  {"bank_id": bank_id, "rates": rates, "r": random.random()}))
        if r.get("result") != "ok":
            raise RbcError(f"save_packet: {r.get('error', r)}")
        return r["packet_id"]
