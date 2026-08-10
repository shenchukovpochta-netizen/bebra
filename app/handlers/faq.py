"""Ветка частых вопросов: меню тем и автоответы.

Две точки входа. Первая - кнопка «Частые вопросы» в меню: человек выбирает
тему и сразу получает ответ, менеджера при этом не тревожат вовсе. Вторая -
свободный текст в режиме вопроса (app/handlers/menu.py): бот распознаёт тему
и отвечает мгновенно, а вопрос всё равно уходит человеку с меткой темы.

Сами ответы и факты - в app/faq.py. Здесь только доставка: что отправить,
кого перевести в режим вопроса и что записать в журнал.
"""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message

from .. import faq
from .. import keyboards as kb
from .. import logic, texts
from ..db import Database
from ..filters import StateIs

log = logging.getLogger(__name__)
router = Router(name="faq")

BTN_FAQ = faq.MENU_BUTTON


def reply_for(intent: faq.Intent, data: dict) -> str:
    """Ответ по теме с учётом того, кто спрашивает и который час.

    Действующему арендатору «сколько стоит» отвечается продлением, новому -
    тарифами; вне графика к ответам с приглашением приехать добавляется
    приписка про часы работы.
    """
    return faq.answer(intent, now=datetime.now(), renter=faq.is_renter(data),
                      plan=faq.plan_of(data))


# Меню тем доступно только из основного меню: в режиме вопроса кнопка,
# набранная текстом, означает «передумал спрашивать» и обрабатывается
# в menu.st_support - иначе человек молча остался бы в режиме вопроса.
@router.message(StateIs(logic.APPROVED), F.text == BTN_FAQ)
async def faq_menu(message: Message) -> None:
    await message.answer(texts.FAQ_MENU,
                         reply_markup=kb.faq_topics(faq.MENU_TOPICS))


@router.callback_query(StateIs(logic.APPROVED, logic.WAIT_SUPPORT),
                       F.data.startswith("faq:"))
async def faq_topic(callback: CallbackQuery, bot: Bot, db: Database,
                    user: dict) -> None:
    """Ответ по выбранной теме.

    Ответ уходит через bot по tg_id, а не через callback.message: список тем
    живёт в чате сутками, и у старого сообщения Telegram отдаёт недоступный
    объект без метода answer.
    """
    code = (callback.data or "").split(":", 1)[-1]
    intent = faq.BY_CODE.get(code)
    # Красные линии темой в меню не показываются: по ним бот не говорит
    # ничего. Кнопка с такой темой может прийти только из подделанного
    # callback - отвечаем как на устаревшую.
    if intent is None or intent.red or not intent.menu:
        await callback.answer(texts.FAQ_TOPIC_GONE, show_alert=True)
        return
    await callback.answer()

    tg_id = user["tg_id"]
    row = await db.get_user(tg_id)
    data = dict(row) if row else dict(user)
    await bot.send_message(tg_id, reply_for(intent, data))
    await db.log_event(tg_id, "faq_answered", {"code": intent.code})

    if not intent.handoff:
        return
    # Теме нужен человек: заряженные АКБ, забор велосипеда, возврат, выкуп.
    # Переводим в режим вопроса сразу, иначе следующее сообщение человека
    # («заберите с Баумана 1») провалится в ловушку меню.
    if user["state"] == logic.WAIT_SUPPORT or await db.patch(
            tg_id, expected_state=logic.APPROVED, state=logic.WAIT_SUPPORT):
        await bot.send_message(tg_id, texts.FAQ_HANDOFF,
                               reply_markup=kb.support_cancel())
