"""Оценка аренды после сдачи: нажатие кнопки и комментарий к низкой оценке.

Вопрос задаёт круг в процессе бота (app/crm/feedback.py), здесь - ответ.
Диалог без состояния: оценка едет в callback кнопки, а комментарий - это
ответ на сообщение-просьбу, которое бот запомнил у оценки (prompt_msg).
Клиент при этом может быть в любом шаге своего сценария: состояние
bot.users оценка не трогает и не ломает.

Роутер подключается раньше кабинета и меню, но ловит только своё: кнопки
fb:… и ответ на свою просьбу. Остальное идёт дальше как раньше.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from .. import i18n, logic, texts
from .. import keyboards as kb
from ..crm import logic as crm_logic
from ..crm import service

log = logging.getLogger(__name__)

router = Router(name="feedback")


class FeedbackReply(BaseFilter):
    """Ответ клиента в личке на просьбу о комментарии к его же оценке.

    Сверка по базе, а не по тексту отвечаемого сообщения: текст можно
    подделать пересылкой, а пара «сообщение бота + клиент» - нет.
    """

    async def __call__(self, message: Message, crm: Any = None) -> bool | dict:
        replied = message.reply_to_message
        if (crm is None or replied is None or message.from_user is None
                or message.chat.type != "private"):
            return False
        if not (replied.from_user and replied.from_user.is_bot):
            return False
        try:
            row = await crm.feedback_by_prompt("tg", str(replied.message_id),
                                               message.from_user.id)
        except Exception:                               # noqa: BLE001
            log.exception("оценка по ответу клиента %s не прочитана",
                          message.from_user.id)
            return False
        return {"feedback": row} if row else False


@router.callback_query(F.data.startswith("fb:"))
async def cb_feedback(callback: CallbackQuery, bot: Bot, crm: Any = None) -> None:
    parsed = crm_logic.parse_feedback_callback(callback.data)
    if parsed is None or crm is None:
        await callback.answer(texts.FEEDBACK_STALE, show_alert=True)
        return
    rental_id, score = parsed
    try:
        row = await service.rate_rental(crm, rental_id, score, channel="tg",
                                        user_id=callback.from_user.id)
    except service.ServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(texts.FEEDBACK_THANKS_TOAST)
    # Кнопки снимаем: второе нажатие всё равно не пройдёт, а висящие
    # цифры выглядят так, будто оценку не приняли.
    message = getattr(callback, "message", None)
    if isinstance(message, Message):
        try:
            await message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError:
            pass
    chat = callback.from_user.id
    try:
        if not crm_logic.feedback_low(score):
            await bot.send_message(chat, texts.FEEDBACK_THANKS)
            return
        sent = await bot.send_message(chat, texts.FEEDBACK_ASK_COMMENT,
                                      reply_markup=kb.feedback_comment())
    except TelegramAPIError:
        log.warning("ответ на оценку клиенту %s не доставлен", chat)
        return
    await crm.set_feedback_prompt(row["id"], str(sent.message_id))


@router.message(FeedbackReply())
async def st_feedback_comment(message: Message, feedback: dict,
                              crm: Any = None, user: dict | None = None) -> None:
    # Просьба пришла с ForceReply, а Telegram считает его клавиатурой чата:
    # меню («Поддержка», «Кабинет») с тех пор скрыто. Ответ возвращает его -
    # иначе недовольный клиент остался бы без кнопок до /start. Только
    # в меню: посреди другого шага у клиента своя клавиатура.
    lang = i18n.user_lang(user)
    menu = (kb.main_menu(lang) if (user or {}).get("state") == logic.APPROVED
            else None)
    try:
        await service.comment_rental(crm, feedback, message.text or message.caption)
    except service.ServiceError as exc:
        await message.answer(str(exc), reply_markup=menu)
        return
    await message.answer(texts.FEEDBACK_COMMENT_THANKS, reply_markup=menu)
