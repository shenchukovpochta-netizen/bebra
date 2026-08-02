"""Меню после регистрации. Заглушки под аренду - здесь начинается ваша бизнес-логика."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from .. import logic, texts
from ..db import Database

log = logging.getLogger(__name__)
router = Router(name="menu")


@router.message(F.text == "🚲 Арендовать")
async def rent(message: Message) -> None:
    await message.answer("Аренда пока в разработке.")


@router.message(F.text == "📋 Мои поездки")
async def trips(message: Message) -> None:
    await message.answer("Поездок пока нет.")


@router.message(F.text == "💰 Тарифы")
async def tariffs(message: Message) -> None:
    await message.answer("Тарифы уточняются.")


@router.message(F.text == "🆘 Поддержка")
async def support(message: Message) -> None:
    await message.answer("Напишите нам: @support")


# Ловушка последней очереди: пользователь зарегистрирован, но прислал что-то,
# на что нет обработчика. Без неё сообщение уходит в тишину.
@router.message()
async def fallback(message: Message, db: Database, user: dict) -> None:
    # Состояние из прошлой версии бота: обработчика для него уже нет, и без
    # этой ветки человек навсегда застрял бы посреди регистрации, получая
    # приглашение в меню. Возвращаем в начало анкеты.
    if not logic.is_known_state(user.get("state")):
        log.warning("неизвестное состояние %r у %s - сбрасываю в начало",
                    user.get("state"), user["tg_id"])
        await db.patch(user["tg_id"], state=logic.WAIT_FIO)
        await message.answer(texts.WELCOME, reply_markup=kb.remove())
        return
    await message.answer(texts.MENU_PROMPT, reply_markup=kb.main_menu())


# Кнопка из старого сообщения в состоянии, где её уже не ждут. Без ответа
# на callback Telegram крутит часики у пользователя до таймаута.
@router.callback_query()
async def stale_callback(callback: CallbackQuery) -> None:
    await callback.answer("Кнопка устарела, отправьте /start")
