"""Меню после регистрации: тарифы, вопрос в поддержку, ловушка на остальное."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from .. import logic, texts
from ..config import Config
from ..db import Database
from ..filters import StateIs

log = logging.getLogger(__name__)
router = Router(name="menu")


@router.message(F.text == "🚲 Арендовать")
async def rent(message: Message) -> None:
    # Аренда оформляется людьми, а не ботом: показываем тарифы и куда писать.
    await message.answer(texts.TARIFFS)


@router.message(F.text == "📋 Мои поездки")
async def trips(message: Message) -> None:
    await message.answer("Поездок пока нет.")


@router.message(F.text == "💰 Тарифы")
async def tariffs(message: Message) -> None:
    await message.answer(texts.TARIFFS)


@router.message(F.text == "🆘 Поддержка")
async def support(message: Message, db: Database, user: dict) -> None:
    """Вход в диалог с поддержкой.

    Отдельное состояние обязательно: без него следующее сообщение человека
    провалилось бы в ловушку меню, и вопрос ушёл бы в никуда. Кнопка
    доступна только из меню, то есть в состоянии approved, - guard
    закрывает двойное нажатие и гонки.
    """
    if not await db.patch(user["tg_id"], expected_state=logic.APPROVED,
                          state=logic.WAIT_SUPPORT):
        await message.answer(texts.MENU_PROMPT, reply_markup=kb.main_menu())
        return
    await message.answer(texts.SUPPORT_PROMPT, reply_markup=kb.support_cancel())


@router.message(StateIs(logic.WAIT_SUPPORT), F.text)
async def st_support(message: Message, bot: Bot, db: Database, cfg: Config,
                     user: dict) -> None:
    if message.text.strip().lower() == "отмена":
        await db.patch(user["tg_id"], expected_state=logic.WAIT_SUPPORT,
                       state=logic.APPROVED)
        await message.answer(texts.SUPPORT_CANCELLED, reply_markup=kb.main_menu())
        return

    question = logic.support_question(message.text)
    if not question.ok:
        await message.answer(question.error)
        return

    # Карточка уходит в чат модерации ДО ответа пользователю: если она
    # не дошла (бот выкинут из чата), человек должен узнать об этом сразу,
    # а не ждать ответа, которого никто не увидит.
    try:
        sent = await bot.send_message(
            cfg.admin_chat_id,
            texts.SUPPORT_CARD.format(
                fio=logic.esc(user.get("full_name") or "без имени"),
                username=logic.esc(user.get("username") or "—"),
                tg_id=user["tg_id"],
                question=logic.esc(question.value),
            ),
        )
    except TelegramAPIError:
        log.exception("вопрос в поддержку от %s не доставлен", user["tg_id"])
        await db.patch(user["tg_id"], expected_state=logic.WAIT_SUPPORT,
                       state=logic.APPROVED)
        await message.answer(texts.SUPPORT_FAILED, reply_markup=kb.main_menu())
        return

    await db.patch(user["tg_id"], expected_state=logic.WAIT_SUPPORT,
                   state=logic.APPROVED,
                   support_chat_id=sent.chat.id, support_message_id=sent.message_id)
    await db.log_event(user["tg_id"], "support_question")
    await message.answer(texts.SUPPORT_SENT, reply_markup=kb.main_menu())


@router.message(StateIs(logic.WAIT_SUPPORT))
async def st_support_wrong(message: Message) -> None:
    await message.answer(texts.SUPPORT_AS_TEXT)


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
