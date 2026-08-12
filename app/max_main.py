"""Точка входа бота для MAX. Запускается отдельным контейнером:

    docker compose --profile max up -d

Telegram-бот при этом не затрагивается: у MAX-бота свой токен, свой канал,
свои чаты и СВОЯ база mybike_max в том же Postgres - идентификаторы
пользователей двух мессенджеров живут в разных пространствах, и общая
таблица перепутала бы людей.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

import asyncpg

from . import tasks
from .config import Config, _env, _int, _secret
from .db import Database
from .max.client import MaxClient
from .max.handlers import Ctx
from .max.runner import poll_forever
from .services.contract import load_template
from .services.crypto import Vault

log = logging.getLogger("mybike-max")

# id сообщений в MAX (mid) - строки, а не числа. Общая схема объявляет
# колонки карточек bigint под Telegram; здесь они расширяются до text.
# Идемпотентно: у text-колонки повторный alter ничего не меняет.
MID_MIGRATION = """
alter table bot.users alter column mod_message_id type text
  using mod_message_id::text;
alter table bot.users alter column support_message_id type text
  using support_message_id::text;
"""


def load_config() -> Config:
    """Конфигурация MAX-бота из MAX_*-переменных.

    Поля те же, что у Telegram-версии: обработчики и ретеншен общие,
    им незачем знать, из какого мессенджера пришли значения.
    """
    return Config(
        bot_token=_secret("MAX_BOT_TOKEN"),
        channel_id=_int("MAX_CHANNEL_ID", required=True),
        admin_chat_id=_int("MAX_ADMIN_CHAT_ID", required=True),
        admins=tuple(int(x) for x in
                     _env("MAX_ADMINS", required=True).replace(",", " ").split()),
        pg={
            "user": _env("POSTGRES_USER", "mybike"),
            "password": _secret("POSTGRES_PASSWORD"),
            "database": _env("MAX_POSTGRES_DB", "mybike_max"),
            "host": _env("POSTGRES_HOST", "postgres"),
            "port": _int("POSTGRES_PORT", "5432"),
        },
        storage_dir=Path(_env("MAX_STORAGE_DIR", "/files/kyc-max")),
        pdn_key=_secret("PDN_KEY"),
        contract_chat_id=_int("MAX_CONTRACT_CHAT_ID",
                              _env("MAX_ADMIN_CHAT_ID", required=True)),
        fix_chat_id=_int("MAX_FIX_CHAT_ID",
                         _env("MAX_ADMIN_CHAT_ID", required=True)),
        fix_topic_id=None,        # тем в MAX нет
        contract_template=Path(
            _env("CONTRACT_TEMPLATE", "/srv/app/contract_template.docx")),
        channel_url=_env("MAX_CHANNEL_URL", "https://max.ru"),
        oferta_url=_env("OFERTA_URL", required=True),
        oferta_version=_env("OFERTA_VERSION", "2026-01-15"),
        pdn_url=_env("PDN_URL"),
        pdn_version=_env("PDN_VERSION", _env("OFERTA_VERSION", "2026-01-15")),
        video_url=_env("VIDEO_URL", "https://youtu.be/CyZzskq8o0o"),
        purge_approved_days=_int("PURGE_APPROVED_DAYS", "90"),
        purge_rejected_days=_int("PURGE_REJECTED_DAYS", "3"),
        updates_log_days=_int("UPDATES_LOG_DAYS", "7"),
        # Напоминания о сроке живут в Telegram-боте: у MAX старый поток
        # без актов и сроков. Поля нужны, чтобы Config собрался.
        remind_before_days=_int("REMIND_BEFORE_DAYS", "2"),
        remind_hour_utc=_int("REMIND_HOUR_UTC", "7"),
        rate_soft=_int("RATE_SOFT", "40"),
        rate_hard=_int("RATE_HARD", "50"),
        # Различимый префикс: у MAX своя последовательность, и без него
        # два договора из разных мессенджеров получили бы одинаковый номер.
        contract_prefix=_env("MAX_CONTRACT_PREFIX", "АВМ"),
    )


async def ensure_database(pg: dict) -> None:
    """Создаёт базу mybike_max, если её ещё нет.

    Официальный образ Postgres создаёт только одну базу из окружения,
    а init-скрипты не выполняются на уже инициализированном томе -
    на живом сервере добавить вторую базу больше некому.
    """
    admin = dict(pg, database=_env("POSTGRES_DB", "mybike"))
    conn = await asyncpg.connect(**admin)
    try:
        exists = await conn.fetchval(
            "select 1 from pg_database where datname = $1", pg["database"])
        if not exists:
            # имя проверено: только буквы/цифры/подчёркивание
            if not pg["database"].replace("_", "").isalnum():
                raise RuntimeError(f"подозрительное имя базы: {pg['database']!r}")
            await conn.execute(f'create database "{pg["database"]}"')
            log.info("создана база %s", pg["database"])
    finally:
        await conn.close()


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    cfg = load_config()
    vault = Vault.from_raw(cfg.pdn_key)
    load_template(cfg.contract_template)
    log.info("шаблон договора на месте: %s", cfg.contract_template)

    await ensure_database(cfg.pg)
    db = await Database.connect(cfg.pg)
    await db.apply_schema(Path(__file__).resolve().parent.parent / "schema.sql")
    await db.pool.execute(MID_MIGRATION)
    log.info("схема применена (база %s)", cfg.pg["database"])

    cl = MaxClient(_secret("MAX_BOT_TOKEN"),
                   base=_env("MAX_API_BASE", "https://botapi.max.ru"))
    me = await cl.me()
    log.info("бот MAX @%s готов", me.get("username") or me.get("name"))

    ctx = Ctx(cl, db, cfg, vault)
    retention = asyncio.create_task(tasks.retention_loop(db, cfg))
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    # Опрос - отдельной задачей: long polling висит в сети до 30 секунд,
    # и просто выставленный флаг остановки дождался бы конца запроса -
    # docker stop успел бы прислать SIGKILL. Отмена задачи рвёт запрос сразу.
    poller = asyncio.create_task(poll_forever(ctx, stop))
    try:
        await stop.wait()
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)
    finally:
        log.info("останавливаюсь")
        retention.cancel()
        await asyncio.gather(retention, return_exceptions=True)
        await tasks.drain()
        await cl.close()
        await db.close()
        log.info("остановлен")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
