#!/usr/bin/env python3
"""Запуск скрипта на RuVDS по ssh — единственная точка, где мы туда ходим.

RuVDS живёт на 2 ядрах и 4 ГБ, там же Coolify, n8n и Matrix-стек. Когда память
кончается, sshd не успевает отдать баннер за 20 секунд, и мы получали
«Connection timed out during banner exchange» → падал весь прогон и уходил алерт.
Поэтому: длинный ConnectTimeout, несколько попыток с паузами и понятная ошибка,
если сервер действительно недоступен.
"""
import json
import random
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SSH_HOST = "ruvds"
BACKOFF = (30, 90, 180)          # ssh-блипы на перегруженном сервере длятся минутами
CONNECT_TIMEOUT = 90             # сколько ждём баннер sshd


def _ssh_cmd(remote_args):
    return ["ssh",
            "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
            "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=8",
            SSH_HOST, remote_args]


def run(script_name, args="", timeout=600, log=None):
    """Прогоняет scripts/<script_name> на RuVDS, возвращает распарсенный JSON."""
    log = log or (lambda m: print(m, file=sys.stderr))
    payload = (HERE / script_name).read_bytes()
    remote = f"python3 - {args}".strip()
    last = ""
    for i, pause in enumerate(BACKOFF + (None,)):
        p = subprocess.run(_ssh_cmd(remote), input=payload,
                           capture_output=True, timeout=timeout)
        if p.returncode == 0:
            try:
                return json.loads(p.stdout.decode())
            except json.JSONDecodeError as e:
                last = f"ответ не JSON: {e}; начало: {p.stdout[:120]!r}"
        else:
            last = p.stderr.decode().strip().splitlines()[-1] if p.stderr else f"код {p.returncode}"
        if pause is None:
            break
        log(f"  ssh {script_name} {args}: попытка {i + 1} не прошла ({last[:90]}), "
            f"повтор через {pause}с")
        time.sleep(pause + random.uniform(0, 10))
    raise RuntimeError(f"RuVDS недоступен после {len(BACKOFF) + 1} попыток: {last[:200]}")
