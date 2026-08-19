#!/usr/bin/env python3
"""История курсов конкурентов: как часто банк реально переставляет курс.

`refreshDate` у banki.ru не годится — он обновляется при каждом опросе, даже если
цифры те же. Поэтому сравниваем значения между запусками сборщика и считаем
фактические изменения.

Состояние: scripts/state/rate_history.json (вне git, растёт медленно).
Результат: data/volatility.js — сколько раз в день банк меняет курс.

  python3 rate_history.py --backfill   # добрать точки из истории git по competitors.js
  python3 rate_history.py --stats      # показать сводку
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE = HERE / "state" / "rate_history.json"
KEEP_DAYS = 30
CURRENCIES = ("USD", "EUR")  # по остальным изменения редки, статистика была бы шумной


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"keys": {}, "first_seen": None}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")))


def record(entries, ts, state=None):
    """entries: [(key, bank, currency, buy, sell)]. Возвращает число изменений."""
    state = state if state is not None else load_state()
    state["first_seen"] = min(state.get("first_seen") or ts, ts)
    changed = 0
    for key, bank, cur, buy, sell in entries:
        k = f"{key}|{cur}"
        rec = state["keys"].setdefault(k, {"bank": bank, "last": None, "changes": [], "samples": 0})
        rec["bank"] = bank
        rec.setdefault("samples", 0)
        val = [buy, sell]
        if rec["last"] is not None:
            rec["samples"] += 1          # считаем только сравнимые пары замеров
            if rec["last"] != val:
                rec["changes"].append(ts)
                changed += 1
        rec["last"] = val
    # чистим хвост, чтобы файл не рос бесконечно
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    for rec in state["keys"].values():
        rec["changes"] = [c for c in rec["changes"] if c >= cutoff][-500:]
    return state, changed


def entries_from_competitors(comps):
    out = []
    for c in comps:
        for cur, r in c["rates"].items():
            if cur in CURRENCIES and r.get("buy"):
                out.append((f"o{c['id']}", c["bank"], cur, r["buy"], r["sell"]))
    return out


def stats(state):
    """По банку: в какой доле наших замеров курс оказывался новым.

    Замеры идут раз в 2 часа, поэтому «сколько раз в день» мы честно измерить не можем —
    внутри интервала банк мог переставить курс несколько раз. Доля замеров с изменением
    отвечает на практический вопрос: успевает ли курс устареть между заходами."""
    first = state.get("first_seen")
    if not first:
        return {}, 0
    days = max((datetime.now(timezone.utc) - datetime.fromisoformat(first)).total_seconds() / 86400, 0.01)
    per_bank = {}
    for rec in state["keys"].values():
        b = per_bank.setdefault(rec["bank"], {"changes": 0, "samples": 0, "last": None})
        b["changes"] += len(rec["changes"])
        b["samples"] += rec.get("samples", 0)
        if rec["changes"]:
            b["last"] = max(b["last"] or "", rec["changes"][-1])
    out = {}
    for bank, b in per_bank.items():
        if not b["samples"]:
            continue
        out[bank] = {
            "share": round(b["changes"] / b["samples"], 2),
            "samples": b["samples"],
            "last": b["last"],
        }
    return out, days


def write_volatility(state):
    st, days = stats(state)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (ROOT / "data" / "volatility.js").write_text(
        f"// generated {stamp}, окно наблюдения {days:.1f} дн.\nconst VOLATILITY = "
        + json.dumps(st, ensure_ascii=False, separators=(",", ":"))
        + f";\nconst VOLATILITY_DAYS = {days:.2f};\n")
    return st, days


def backfill():
    """Добираем точки из git-истории data/competitors.js — там уже есть снимки."""
    log = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H %cI", "--",
                          "data/competitors.js"], capture_output=True, text=True).stdout
    commits = [l.split() for l in log.strip().splitlines()][::-1]  # от старых к новым
    state = load_state()
    total = 0
    for sha, ts in commits:
        blob = subprocess.run(["git", "-C", str(ROOT), "show", f"{sha}:data/competitors.js"],
                              capture_output=True, text=True).stdout
        m = re.search(r"const COMPETITORS = (\[.*?\]);", blob, re.S)
        if not m:
            continue
        state, ch = record(entries_from_competitors(json.loads(m.group(1))), ts, state)
        total += ch
        print(f"  {ts[:16]} — изменений: {ch}", file=sys.stderr)
    save_state(state)
    print(f"добрано точек: {len(commits)}, изменений: {total}", file=sys.stderr)
    return state


def main():
    if "--backfill" in sys.argv:
        state = backfill()
    else:
        state = load_state()
    st, days = write_volatility(state)
    if "--stats" in sys.argv or "--backfill" in sys.argv:
        print(f"\nокно наблюдения: {days:.2f} дн., банков: {len(st)}\n")
        for bank, v in sorted(st.items(), key=lambda kv: -kv[1]["share"]):
            print(f"  {bank[:24]:26s} менялся в {v['share'] * 100:3.0f}% замеров "
                  f"(сравнений: {v['samples']:4d})   последнее: {(v['last'] or '—')[:16]}")


if __name__ == "__main__":
    main()
