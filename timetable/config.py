"""Настройки бота расписания.

Токен читается либо из переменной окружения, либо из файла по пути в
SCHEDULE_BOT_TOKEN_FILE - как и в основном боте репозитория: значение в
окружении видно в `docker inspect` и в дампе процесса, файл секрета - нет.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    token: str

    @staticmethod
    def load() -> Config:
        return Config(token=_token())


def _token() -> str:
    path = os.environ.get("SCHEDULE_BOT_TOKEN_FILE")
    if path:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(
                f"не прочитать файл токена SCHEDULE_BOT_TOKEN_FILE={path}: {exc}"
            ) from exc
    else:
        value = (os.environ.get("SCHEDULE_BOT_TOKEN") or "").strip()
    if not value:
        raise RuntimeError(
            "не задан токен бота: положите его в SCHEDULE_BOT_TOKEN "
            "или укажите файл в SCHEDULE_BOT_TOKEN_FILE"
        )
    # Токен BotAPI всегда вида <id>:<секрет>. Проверка на старте вместо
    # невнятного Unauthorized при первом же запросе к Telegram.
    if ":" not in value:
        raise RuntimeError("токен бота не похож на токен Telegram: нет двоеточия")
    return value
