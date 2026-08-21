"""Точка входа.

Long polling, а не вебхук: серверу не нужен ни публичный адрес, ни сертификат,
ни открытый входящий порт, и исчезает главная боль вебхуков - один webhook
на один токен, из-за которого тестовый запуск глушит боевого бота.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from . import tasks
from .config import Config
from .db import Database
from .handlers import contract, faq, menu, moderation, registration
from .middlewares import PipelineMiddleware
from .services.contract import load_template
from .services.crypto import Vault

log = logging.getLogger("mybike")


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    cfg = Config.load()
    vault = Vault.from_raw(cfg.pdn_key)
    # Шаблоны читаются на старте, хотя нужны только при выдаче договора:
    # опечатка в пути обнаружилась бы иначе в момент, когда пользователю уже
    # сказано «заявка одобрена», а договора нет. Согласие проверяется наравне
    # с договором: без него issue() не соберёт пакет документов.
    load_template(cfg.contract_template)
    load_template(cfg.soglasie_template)
    log.info("шаблоны договора и согласия на месте: %s, %s",
             cfg.contract_template, cfg.soglasie_template)
    if not cfg.pdn_policy_file.exists():
        log.warning("файл политики ПДн %s не найден - шаг ознакомления "
                    "будет работать текстом, без вложения", cfg.pdn_policy_file)
    # Ссылку оплаты бот отдаёт кнопкой, а Telegram отвергает сообщение
    # целиком из-за кнопки с битым url. Проверяем на старте, а не в момент,
    # когда клиент уже подписал договор и ждёт реквизиты.
    if not cfg.pay_url.startswith(("http://", "https://")):
        log.warning("PAY_URL=%r не похож на ссылку - кнопки «Оплатить» "
                    "не будет, останется только текст", cfg.pay_url)

    db = await Database.connect(cfg.pg)
    await db.apply_schema(Path(__file__).resolve().parent.parent / "schema.sql")
    log.info("схема применена")

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    log.info("бот @%s готов", me.username)

    dp = Dispatcher()
    dp.update.outer_middleware(PipelineMiddleware(db, cfg, vault))
    # Порядок важен: модерация раньше регистрации, иначе клик админа
    # провалится в пользовательский сценарий. Договор - до регистрации,
    # чтобы «Подписываю» не поймала ловушка шага. menu - последним.
    dp.include_router(moderation.router)
    dp.include_router(contract.router)
    dp.include_router(registration.router)
    # Частые вопросы - до меню: у меню последним обработчиком стоит ловушка
    # на любое сообщение, и ветка вопросов до него бы не дожила.
    dp.include_router(faq.router)
    dp.include_router(menu.router)

    retention = asyncio.create_task(tasks.retention_loop(db, cfg))
    reminders = asyncio.create_task(
        tasks.reminders_loop(bot, db, cfg, vault))

    # docker stop шлёт SIGTERM. Без обработчика процесс умирает мгновенно:
    # фоновые задачи (сохранение скана, хэш) обрываются на полуслове,
    # а finally не выполняется.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            # через tasks.spawn, а не голый create_task: иначе задачу остановки
            # может собрать сборщик мусора, и завершение зависнет
            loop.add_signal_handler(sig, lambda: tasks.spawn(dp.stop_polling()))
        except NotImplementedError:
            pass          # Windows: сигналы через add_signal_handler не заводятся

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        log.info("останавливаюсь")
        retention.cancel()
        reminders.cancel()
        await asyncio.gather(retention, reminders, return_exceptions=True)
        await tasks.drain()
        await bot.session.close()
        await db.close()
        log.info("остановлен")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
