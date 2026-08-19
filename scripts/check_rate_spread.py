#!/usr/bin/env python3
"""Проверяет, одинаковый ли у банка курс во всех отделениях Москвы.

Берёт офисные курсы banki.ru (там курс каждого офиса отдельно) и считает по банку
разброс: сколько разных значений, минимум/максимум, спред в рублях.
Дополнительно сверяет городской курс mainfin.ru с офисными — видно, что именно
показывает агрегатор: типовой курс банка или лучшее предложение одного офиса.

  python3 check_rate_spread.py           # USD
  python3 check_rate_spread.py EUR       # другая валюта
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from collect_bank_rates import norm  # noqa: E402  — то же сопоставление имён, что и в сборщике
SSH = ["ssh", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", "ruvds", "python3 -"]


def remote(script_name):
    p = subprocess.run(SSH, input=(HERE / script_name).read_bytes(),
                       capture_output=True, timeout=600)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[:300])
    return json.loads(p.stdout.decode())


def main():
    cur = (sys.argv[1] if len(sys.argv) > 1 else "USD").upper()
    offices = remote("remote_fetch_banki.py")["offices"]
    banks = remote("remote_fetch_mainfin.py")["banks"]
    city = {norm(b["name"]): b["rates"].get(cur) for b in banks}

    by_bank = {}
    for o in offices:
        r = o["rates"].get(cur)
        if not r or not r.get("buy"):
            continue
        by_bank.setdefault(o["bank"], []).append((r["buy"], r["sell"], o["name"]))

    print(f"Разброс курса {cur} внутри банка (источник: офисные курсы banki.ru, Москва)\n")
    print(f"{'банк':22s} {'офисов':>6s} {'разных':>6s} {'покупка min–max':>20s} {'спред':>7s}  вывод")
    rows = sorted(by_bank.items(), key=lambda kv: -len(kv[1]))
    for bank, vals in rows:
        if len(vals) < 2:
            continue
        buys = [v[0] for v in vals]
        uniq = len(set(buys))
        lo, hi = min(buys), max(buys)
        spread = hi - lo
        verdict = ("одинаковый везде" if uniq == 1 else
                   "почти одинаковый" if spread <= 0.2 else
                   "РАЗНЫЙ по офисам")
        print(f"{bank[:22]:22s} {len(vals):6d} {uniq:6d} {lo:9.2f}–{hi:<9.2f} {spread:7.2f}  {verdict}")

    print(f"\nСверка городского курса mainfin.ru с офисными (покупка {cur}):\n")
    print(f"{'банк':22s} {'город':>8s} {'офисы min–max':>20s}  совпадение")
    for bank, vals in rows:
        c = city.get(norm(bank))
        if not c:
            continue
        buys = [v[0] for v in vals]
        lo, hi = min(buys), max(buys)
        inside = lo - 0.01 <= c["buy"] <= hi + 0.01
        mark = "внутри диапазона" if inside else ("ВЫШЕ всех офисов" if c["buy"] > hi else "НИЖЕ всех офисов")
        print(f"{bank[:22]:22s} {c['buy']:8.2f} {lo:9.2f}–{hi:<9.2f}  {mark}")


if __name__ == "__main__":
    main()
