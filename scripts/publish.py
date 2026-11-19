#!/usr/bin/env python3
"""Один коммит на прогон сборщиков: публикует все изменившиеся data/*.js.

Раньше каждый сборщик коммитил своё, из-за чего файл offices_mainfin.js вообще
не попадал в коммит (его никто не добавлял), а GitHub Pages пересобирался дважды.
Теперь сборщики только пишут файлы, а публикация — здесь.

Файлы, где изменилась лишь строка с временем генерации, откатываются: незачем
гонять пересборку сайта, если курсы те же.
"""
import fcntl
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = "data"
STAMP_MARKERS = ("generated", "_AT =", "VOLATILITY_DAYS")


def git(*args, check=False):
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True, check=check)


def main(message="Auto: refresh rates"):
    # публикуют двое: прогон сборщиков (раз в 30 мин) и rbc_sync.py (раз в 5 мин) —
    # без замка их git add/commit/push могут переплестись
    lock = open(ROOT / "scripts" / "state" / "publish.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    changed = [l[3:].strip().strip('"') for l in
               git("status", "--porcelain", "--", DATA).stdout.splitlines()]
    if not changed:
        print("данные не изменились")
        return

    meaningful = []
    for f in changed:
        diff = git("diff", "--", f).stdout
        if not diff:                      # новый файл — публикуем как есть
            meaningful.append(f)
            continue
        body = [l for l in diff.splitlines()
                if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))
                and not any(m in l for m in STAMP_MARKERS)]
        if body:
            meaningful.append(f)
        else:
            git("checkout", "--", f)

    if not meaningful:
        print("изменились только отметки времени — публикация не нужна")
        return

    git("add", *meaningful)
    git("-c", "user.name=Alex Pshenichnikov", "-c", "user.email=al.pshen@gmail.com",
        "commit", "-q", "-m", message)
    p = git("push", "-q")
    if p.returncode != 0:
        print(f"push не прошёл: {p.stderr[:200]}", file=sys.stderr)
        sys.exit(1)
    print("опубликовано: " + ", ".join(Path(f).name for f in meaningful))


if __name__ == "__main__":
    main()
