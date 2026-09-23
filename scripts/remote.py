#!/usr/bin/env python3
"""Запуск скрипта на RuVDS по ssh — единственная точка, где мы туда ходим.

RuVDS живёт на 2 ядрах и 4 ГБ, там же Coolify, n8n и Matrix-стек. Когда память
кончается, sshd не успевает отдать баннер за 20 секунд, и мы получали
«Connection timed out during banner exchange» → падал весь прогон и уходил алерт.
Поэтому: длинный ConnectTimeout, несколько попыток с паузами и понятная ошибка,
если сервер действительно недоступен.

Второй сорт сбоя (23.09.2026): ssh соединился нормально, а завис сам источник на
той стороне. Такое приходило как subprocess.TimeoutExpired — и летело МИМО цикла
повторов, потому что цикл разбирал только ненулевой returncode. Хуже того, при
локальном таймауте мы убивали только свой ssh, а python3 на RuVDS продолжал
долбить источник ещё десятки минут, и следующий прогон упирался в тот же лимит и
падал так же (четыре падения подряд, два часа без свежих курсов). Поэтому теперь:
дедлайн ставится и на той стороне (UNISTREAM_DEADLINE читают сами скрипты, timeout(1)
страхует сверху), а TimeoutExpired обрабатывается как обычный сбой — с общим
потолком по времени, чтобы повторы не растянули прогон на полчаса.
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
REMOTE_GRACE = 45                # насколько дедлайн на той стороне короче нашего
TOTAL_FACTOR = 2                 # все попытки вместе — не дольше timeout * TOTAL_FACTOR


def _ssh_cmd(remote_args):
    return ["ssh",
            "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
            "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=8",
            SSH_HOST, remote_args]


def _tail(stderr):
    """Последняя осмысленная строка stderr удалённого скрипта — в ней причина."""
    lines = [l for l in stderr.decode("utf-8", "replace").splitlines() if l.strip()]
    return lines[-1].strip() if lines else ""


def run(script_name, args="", timeout=600, log=None):
    """Прогоняет scripts/<script_name> на RuVDS, возвращает распарсенный JSON.

    timeout — потолок на ОДНУ попытку; на все попытки вместе — timeout * TOTAL_FACTOR.
    Короткий сбой (сервер не пустил за секунды) успевает пройти всю лестницу
    повторов, а одно долгое зависание источника прогон не удваивает: сборщик
    ходит раз в 30 минут, и опоздавший прогон хуже пропущенного.
    """
    log = log or (lambda m: print(m, file=sys.stderr))
    payload = (HERE / script_name).read_bytes()
    # Три рубежа по возрастанию: скрипт сам отдаёт собранное по UNISTREAM_DEADLINE,
    # timeout(1) добивает его на 15 с позже, наш ssh ждёт ещё REMOTE_GRACE.
    # Порядок важен: убитый снаружи прогон возвращает НОЛЬ данных, свой дедлайн — хвост.
    deadline = max(30, timeout - REMOTE_GRACE)
    kill_at = min(deadline + 15, max(deadline, timeout - 5))   # timeout(1) строго раньше нашего
    remote = (f"UNISTREAM_DEADLINE={deadline} timeout -k 10 {kill_at} "
              f"python3 - {args}").strip()
    started = time.monotonic()
    last = ""
    for i, pause in enumerate(BACKOFF + (None,)):
        try:
            p = subprocess.run(_ssh_cmd(remote), input=payload,
                               capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            # сюда попадаем, только если завис сам канал: скрипт на той стороне
            # обязан был закончиться раньше по своему дедлайну
            last = f"ssh молчал {timeout}с — завис канал до RuVDS"
        else:
            if p.returncode == 0:
                try:
                    return json.loads(p.stdout.decode())
                except json.JSONDecodeError as e:
                    last = f"ответ не JSON: {e}; начало: {p.stdout[:120]!r}"
            elif p.returncode in (124, 137):   # сработал timeout(1) на той стороне
                last = f"не уложился в {deadline}с на RuVDS: {_tail(p.stderr)}"
            else:
                last = _tail(p.stderr) or f"код {p.returncode}"
        if pause is None:
            break
        spent = time.monotonic() - started
        if spent + pause + timeout > timeout * TOTAL_FACTOR:
            log(f"  ssh {script_name}: потрачено {int(spent)}с из "
                f"{int(timeout * TOTAL_FACTOR)}с — повторять уже некогда ({last[:90]})")
            break
        log(f"  ssh {script_name} {args}: попытка {i + 1} не прошла ({last[:90]}), "
            f"повтор через {pause}с")
        time.sleep(pause + random.uniform(0, 10))
    raise RuntimeError(f"{script_name}: {last[:200]}")
