"""Точка входа веб-панели: python -m app.web

Свой процесс рядом с ботом: у панели свой порт и свой жизненный цикл,
падение одного не задевает другого. База общая (схема crm), бот -
только для уведомлений клиентам, и без токена панель тоже работает.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import uvicorn

from ..crm.db import CrmDB
from ..db import Database
from .app import create_app, ensure_admin
from .config import WebConfig

log = logging.getLogger("crm.web")
SCHEMA = Path(__file__).resolve().parent.parent.parent / "schema.sql"


async def build():
    cfg = WebConfig.load()
    db = await Database.connect(cfg.pg)
    # Схема применяется и здесь: панель могут поднять раньше бота, а таблицы
    # crm нужны ей с первой секунды. Скрипт идемпотентен.
    await db.apply_schema(SCHEMA)
    crm = CrmDB(db.pool)
    generated = await ensure_admin(crm, cfg)
    if generated:
        log.warning("создан администратор %s с паролем: %s  - смените его в панели "
                    "(Сотрудники) или задайте secrets/crm_admin_password",
                    cfg.admin_login, generated)
    bot = None
    if cfg.bot_token:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    else:
        log.warning("BOT_TOKEN не задан: уведомления клиентам из панели отключены")
    return create_app(crm=crm, db=db, cfg=cfg, bot=bot), cfg, db, bot


async def run() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                        stream=sys.stdout)
    app, cfg, db, bot = await build()
    log.info("панель CRM слушает порт %s", cfg.port)
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=cfg.port,
                                           log_level="info", proxy_headers=True))
    try:
        await server.serve()
    finally:
        if bot is not None:
            await bot.session.close()
        await db.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
