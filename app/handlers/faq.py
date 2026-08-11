"""Ветка частых вопросов: выбор языка, меню тем и автоответы.

Ветка доступна В ЛЮБОМ состоянии, включая «до регистрации»: ночному лиду
нужны адрес и тарифы сейчас, а не после анкеты. Поэтому вход - и кнопкой
под приветствием (callback faq:open), и кнопкой основного меню.

Первый вопрос ветки - язык: выбор запоминается в faq_lang, дальше темы
и ответы идут на нём. Сменить можно кнопкой «🌐» в списке тем. Язык
действует только в ветке кнопок: распознавание свободного текста
в поддержке - по русским триггерам, и автоответ там русский.

Сами ответы и факты - в app/faq.py, переводы - в app/faq_i18n.py.
"""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message

from .. import faq
from .. import faq_i18n as i18n
from .. import keyboards as kb
from .. import logic, texts
from ..config import Config
from ..db import Database
from ..filters import StateIs

log = logging.getLogger(__name__)
router = Router(name="faq")

BTN_FAQ = faq.MENU_BUTTON


def _lang_of(user: dict) -> str:
    lang = user.get("faq_lang") or ""
    return lang if lang in i18n.LANGS else ""


def _registered(user: dict) -> bool:
    """Пользователь в меню: ему доступны «Поддержка» и режим вопроса."""
    return user.get("state") in (logic.APPROVED, logic.WAIT_SUPPORT)


def home(user: dict) -> tuple[str, object]:
    """(текст, клавиатура) главного экрана ветки.

    Язык ещё не выбран - первым вопросом идёт выбор языка; выбран -
    сразу темы на нём.
    """
    lang = _lang_of(user)
    if not lang:
        return i18n.pick_prompt(), kb.faq_langs()
    return (faq.menu_text(lang, registered=_registered(user)),
            kb.faq_topics(faq.MENU_TOPICS, lang))


def reply_for(intent: faq.Intent, data: dict, cfg: Config,
              lang: str = "ru") -> str:
    """Ответ по теме с учётом того, кто спрашивает и который час."""
    return faq.answer(intent, now=datetime.now(), renter=faq.is_renter(data),
                      plan=faq.plan_of(data), pay_url=cfg.pay_url, lang=lang)


# Кнопка меню видна только зарегистрированным (у остальных нет клавиатуры
# меню), а вот callbacks ниже работают в любом состоянии.
@router.message(StateIs(logic.APPROVED), F.text == BTN_FAQ)
async def faq_menu(message: Message, user: dict) -> None:
    text, markup = home(user)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("faqlang:"))
async def faq_set_lang(callback: CallbackQuery, bot: Bot, db: Database,
                       user: dict | None = None) -> None:
    """Выбор языка: запомнить и сразу показать темы на нём."""
    if user is None:        # callback из служебного чата - не наша ветка
        await callback.answer()
        return
    code = (callback.data or "").split(":", 1)[-1]
    if code not in i18n.LANGS:
        await callback.answer(texts.FAQ_TOPIC_GONE, show_alert=True)
        return
    await callback.answer()
    await db.patch(user["tg_id"], faq_lang=code)
    await db.log_event(user["tg_id"], "faq_lang_set", {"lang": code})
    fresh = {**user, "faq_lang": code}
    await bot.send_message(user["tg_id"],
                           faq.menu_text(code, registered=_registered(fresh)),
                           reply_markup=kb.faq_topics(faq.MENU_TOPICS, code))


@router.callback_query(F.data.startswith("faq:"))
async def faq_topic(callback: CallbackQuery, bot: Bot, db: Database,
                    cfg: Config, user: dict | None = None) -> None:
    """Ответ по выбранной теме - в любом состоянии.

    Ответ уходит через bot по tg_id, а не через callback.message: список тем
    живёт в чате сутками, и у старого сообщения Telegram отдаёт недоступный
    объект без метода answer.
    """
    if user is None:
        await callback.answer()
        return
    code = (callback.data or "").split(":", 1)[-1]
    tg_id = user["tg_id"]

    if code in ("open", "lang"):
        await callback.answer()
        shown = user if code == "open" else {**user, "faq_lang": ""}
        text, markup = home(shown)
        await bot.send_message(tg_id, text, reply_markup=markup)
        return

    intent = faq.BY_CODE.get(code)
    # Красные линии темой в меню не показываются: по ним бот не говорит
    # ничего. Кнопка с такой темой может прийти только из подделанного
    # callback - отвечаем как на устаревшую.
    if intent is None or intent.red or not intent.menu:
        await callback.answer(texts.FAQ_TOPIC_GONE, show_alert=True)
        return
    await callback.answer()

    row = await db.get_user(tg_id)
    data = dict(row) if row else dict(user)
    lang = _lang_of(data) or "ru"
    await bot.send_message(tg_id, reply_for(intent, data, cfg, lang))
    await db.log_event(tg_id, "faq_answered", {"code": intent.code,
                                               "lang": lang})

    if not intent.handoff:
        return
    if not _registered(data):
        # До регистрации режима вопроса нет - и трогать состояние анкеты
        # нельзя: человек стоит посреди неё. Вместо этого - прямой контакт.
        t = i18n.T.get(lang, {})
        await bot.send_message(tg_id, t.get("contact",
                                            texts.FAQ_GUEST_CONTACT))
        return
    # Теме нужен человек: заряженные АКБ, забор велосипеда, возврат, выкуп.
    # Переводим в режим вопроса сразу, иначе следующее сообщение человека
    # («заберите с Баумана 1») провалится в ловушку меню.
    if data["state"] == logic.WAIT_SUPPORT or await db.patch(
            tg_id, expected_state=logic.APPROVED, state=logic.WAIT_SUPPORT):
        t = i18n.T.get(lang, {})
        await bot.send_message(tg_id, t.get("handoff", texts.FAQ_HANDOFF),
                               reply_markup=kb.support_cancel())
