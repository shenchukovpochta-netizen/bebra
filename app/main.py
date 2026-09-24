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
from aiogram.types import BotCommand

from . import tasks
from .config import Config
from .crm import banking, mailing, paying, tracking
from .crm.db import CrmDB
from .db import Database
from .handlers import cabinet, contract, faq, fleet, menu, moderation, ops, registration
from .handlers import staff as staff_h
from .max.client import MaxClient
from .middlewares import PipelineMiddleware
from .services.contract import load_template
from .services.crypto import Vault
from .services.starline import StarlineClient
from .services.tochka import TochkaClient

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
    # CRM живёт в той же базе (схема crm) и на том же пуле: кабинет клиента
    # и синхронизация бот -> CRM работают без отдельного подключения.
    crm = CrmDB(db.pool)

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    log.info("бот @%s готов", me.username)

    dp = Dispatcher()
    dp.update.outer_middleware(PipelineMiddleware(db, cfg, vault, crm))
    # Порядок важен: модерация раньше регистрации, иначе клик админа
    # провалится в пользовательский сценарий. Договор - до регистрации,
    # чтобы «Подписываю» не поймала ловушка шага. menu - последним.
    # Кабинет - первым: /cabinet должен открываться из любого шага анкеты,
    # а ответ оператора суммой на карточку заявки - не доехать до общего
    # обработчика реплаев модерации.
    # Рабочая группа точек - самой первой: её роутер забирает любое
    # сообщение из своих тем, и до меню с его ловушкой они не доходят.
    dp.include_router(ops.router)
    dp.include_router(cabinet.router)
    dp.include_router(staff_h.router)
    # Парк из служебного чата - до модерации: ответ на карточку велосипеда
    # иначе перехватил бы разбор ответов на карточки заявок.
    dp.include_router(fleet.router)
    dp.include_router(moderation.router)
    dp.include_router(contract.router)
    dp.include_router(registration.router)
    # Частые вопросы - до меню: у меню последним обработчиком стоит ловушка
    # на любое сообщение, и ветка вопросов до него бы не дожила.
    dp.include_router(faq.router)
    dp.include_router(menu.router)

    retention = asyncio.create_task(tasks.retention_loop(db, cfg))
    reminders = asyncio.create_task(
        tasks.reminders_loop(bot, db, cfg, vault, crm))
    # Опрос трекеров - отдельной задачей: у него свой период (минуты),
    # а не суточный, как у напоминаний. Без настроек StarLine задача
    # завершается сразу и ничего не делает.
    starline = StarlineClient(app_id=cfg.starline_app_id,
                              app_secret=cfg.starline_app_secret,
                              login=cfg.starline_login,
                              password=cfg.starline_password)
    tracking_task = asyncio.create_task(
        tracking.tracking_loop(bot, crm, cfg, starline,
                               interval=cfg.starline_poll_seconds))
    # Выписка банка - своим кругом: клиент, оплативший утром, не должен
    # ждать выдачи до вечернего прохода.
    tochka = TochkaClient(token=cfg.tochka_token,
                          account_id=cfg.tochka_account_id,
                          customer_code=cfg.tochka_customer_code)
    banking_task = asyncio.create_task(
        banking.banking_loop(bot, crm, cfg, tochka,
                             interval=cfg.tochka_poll_seconds, db=db))
    # Счета эквайринга - своим кругом, коротким: оператор ждёт отметки
    # «оплачено», чтобы выдать велосипед, и полчаса ожидания на точке -
    # это очередь. Здесь же суточное автосписание.
    paying_task = asyncio.create_task(paying.paying_loop(bot, crm, cfg, tochka, db=db))
    # Рассылки: тот же текст уходит в Telegram и, если подключён бот MAX,
    # в MAX. Токен MAX здесь необязателен - без него MAX-клиентам
    # сообщения помечаются пропущенными, а не теряются молча.
    max_client = MaxClient(cfg.max_bot_token) if cfg.max_bot_token else None
    mailing_task = asyncio.create_task(
        mailing.mailing_loop(bot, crm, cfg, max_client=max_client))
    # Команда /cabinet в меню бота (кнопка «Меню» слева от поля ввода).
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Начать / меню"),
            BotCommand(command="cabinet", description="🚲 Мой кабинет"),
        ])
    except TelegramAPIError:
        log.warning("не удалось задать список команд бота")

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
        tracking_task.cancel()
        banking_task.cancel()
        paying_task.cancel()
        mailing_task.cancel()
        await asyncio.gather(retention, reminders, tracking_task, banking_task,
                             paying_task, mailing_task, return_exceptions=True)
        if max_client is not None:
            await max_client.close()
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
