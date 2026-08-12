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
from aiogram.exceptions import TelegramAPIError
from aiogram.types import MenuButtonWebApp, WebAppInfo

from . import tasks
from .config import Config
from .db import Database
from .fleet import seed as fleet_seed
from .fleet.api import start_api
from .fleet.db import FleetDB
from .handlers import contract, faq, fleet, menu, moderation, registration
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
    root = Path(__file__).resolve().parent.parent
    await db.apply_schema(root / "schema.sql")
    # Парк и брони - поверх того же пула. Схему применяет только этот бот:
    # у MAX своя база, а техника одна, и учёт у неё должен быть один.
    fleet_db = FleetDB(db.pool)
    await fleet_db.apply_schema(root / "fleet_schema.sql")
    log.info("схемы применены")
    try:
        # Посев и бэкфилл - не повод не запуститься: учёт догоняет жизнь,
        # а не блокирует прокат. Сломанная схема выше - повод: без таблиц
        # не работают ни хуки, ни команды.
        await fleet_seed.ensure_seed(fleet_db)
    except Exception:                                   # noqa: BLE001
        log.exception("посев/бэкфилл парка не удался - продолжаю без него")

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    log.info("бот @%s готов", me.username)

    dp = Dispatcher()
    dp.update.outer_middleware(PipelineMiddleware(db, cfg, vault, fleet_db))
    # Порядок важен: модерация раньше регистрации, иначе клик админа
    # провалится в пользовательский сценарий. Договор - до регистрации,
    # чтобы «Подписываю» не поймала ловушка шага. menu - последним.
    # Команды парка - первыми: в личке утверждающего они иначе дошли бы
    # до ловушки меню и получили клиентский ответ.
    dp.include_router(fleet.router)
    dp.include_router(moderation.router)
    dp.include_router(contract.router)
    dp.include_router(registration.router)
    # Частые вопросы - до меню: у меню последним обработчиком стоит ловушка
    # на любое сообщение, и ветка вопросов до него бы не дожила.
    dp.include_router(faq.router)
    dp.include_router(menu.router)

    retention = asyncio.create_task(tasks.retention_loop(db, cfg))
    holds = asyncio.create_task(tasks.fleet_loop(fleet_db))
    # Витрина парка и Mini App - только при заданном порте. Боту и токен,
    # и служебный чат нужны для карточек «бронь из приложения».
    api_runner = None
    if cfg.api_port:
        api_runner = await start_api(
            fleet_db, cfg.api_port, bot=bot,
            admin_chat_id=cfg.contract_chat_id, bot_token=cfg.bot_token,
            crm_token=cfg.crm_token, admins=cfg.admins)
    # Кнопка меню «🚲 Бронь» во всех личных чатах: появляется, как только
    # владелец опубликовал Mini App по HTTPS и заполнил MINIAPP_URL.
    if cfg.miniapp_url:
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="🚲 Бронь", web_app=WebAppInfo(url=cfg.miniapp_url)))
        except TelegramAPIError:
            log.exception("кнопка Mini App не установилась - проверьте "
                          "MINIAPP_URL (нужен публичный HTTPS-адрес)")

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
        holds.cancel()
        await asyncio.gather(retention, holds, return_exceptions=True)
        await tasks.drain()
        if api_runner is not None:
            await api_runner.cleanup()
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
