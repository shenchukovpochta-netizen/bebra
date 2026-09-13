"""Конфигурация веб-панели CRM. Читается теми же помощниками, что у бота:
секреты через *_FILE, пустая переменная = не задана."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import _env, _int, _secret


@dataclass(frozen=True)
class WebConfig:
    pg: dict[str, Any]
    # Ключ подписи cookie сессии. Отдельный docker secret: утечка ключа -
    # это вход в панель без пароля.
    secret: str
    admin_login: str
    # Пароль первого администратора. Нужен один раз - при пустой таблице
    # crm.staff; дальше пароли живут в базе, и переменную можно убрать.
    admin_password: str
    # Токен бота - только для уведомлений клиентам (зачисление, аренда).
    # Панель работает и без него: уведомления тогда просто не уходят.
    bot_token: str
    storage_dir: Path
    port: int
    # За сколько дней до платежа считать аренду «на днях» в дашборде -
    # то же число, что у напоминаний бота.
    remind_before_days: int
    # Имя проката в шапке панели.
    title: str = "МАЙБАЙК"
    # Панель стоит за Caddy (задан CRM_DOMAIN): адрес клиента брать из
    # X-Forwarded-For, иначе все входы выглядят как один адрес прокси.
    trust_proxy: bool = False

    @classmethod
    def load(cls) -> WebConfig:
        return cls(
            pg={
                "user": _env("POSTGRES_USER", "mybike"),
                "password": _secret("POSTGRES_PASSWORD"),
                "database": _env("POSTGRES_DB", "mybike"),
                "host": _env("POSTGRES_HOST", "postgres"),
                "port": _int("POSTGRES_PORT", "5432"),
            },
            secret=_secret("CRM_SECRET"),
            admin_login=_env("CRM_ADMIN_LOGIN", "admin"),
            admin_password=_secret("CRM_ADMIN_PASSWORD", required=False),
            bot_token=_secret("BOT_TOKEN", required=False),
            storage_dir=Path(_env("STORAGE_DIR", "/files/kyc")),
            port=_int("CRM_PORT", "8080"),
            remind_before_days=_int("REMIND_BEFORE_DAYS", "2"),
            title=_env("CRM_TITLE", "МАЙБАЙК"),
            trust_proxy=bool(_env("CRM_DOMAIN", "")),
        )
