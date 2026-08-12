"""Меню после регистрации: тарифы, вопрос в поддержку, ловушка на остальное."""

from __future__ import annotations

import json
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from .. import faq, i18n
from .. import keyboards as kb
from .. import logic, texts
from ..config import Config
from ..db import Database, utcnow
from ..filters import StateIs
from .faq import home, reply_for

log = logging.getLogger(__name__)
router = Router(name="menu")


# Тексты кнопок меню. Набранная в диалоге кнопка означает «передумал,
# хочу вот это», а не текст вопроса или причину сдачи - поэтому оба
# состояния с ожиданием текста сверяются с этим набором.
# Подписи кнопок переводятся, поэтому обработчики сверяются не с одной
# русской строкой, а со всеми языковыми вариантами (i18n.variants) -
# reply-кнопка приходит обычным текстом на языке клиента.
BTN_RENT, BTN_TRIPS = "🚲 Арендовать", "📋 Мои аренды"
BTN_TARIFFS, BTN_SUPPORT = "💰 Тарифы", "🆘 Поддержка"


async def menu_shortcut(message: Message, bot: Bot, db: Database, cfg: Config,
                        user: dict, text: str, *, state: str) -> bool:
    """Кнопка меню, набранная посреди диалога: выйти и сделать, что просят.

    True - сообщение было кнопкой (или «Отмена») и уже обработано.
    Без этого человек молча оставался в режиме, и следующее его сообщение
    уезжало не туда: вопросом в поддержку или причиной закрытия аренды.
    """
    key = i18n.button_key(text)
    if key is None and text.lower() == "отмена":
        key = "BTN_CANCEL"      # русское «отмена» набирают и без кнопки
    if key in (None, "BTN_SHARE_CONTACT", "BTN_SAME_ADDRESS"):
        return False            # кнопки анкеты - не выход из диалога
    await db.patch(user["tg_id"], expected_state=state, state=logic.APPROVED)
    fresh = {**user, "state": logic.APPROVED}
    lang = i18n.user_lang(user)
    if key == "BTN_RENT":
        await start_rent(message, bot, db, cfg, fresh)
    elif key == "BTN_TARIFFS":
        await message.answer(i18n.t(lang, "TARIFFS"),
                             reply_markup=kb.main_menu(lang))
    elif key == "BTN_TRIPS":
        await message.answer(await rentals_text(db, fresh),
                             reply_markup=kb.main_menu(lang))
    elif key == "BTN_SUPPORT":
        await start_support(message, db, fresh)
    elif key == "BTN_CLOSE_RENT":
        await start_close(message, db, fresh)
    elif key == "BTN_FAQ":
        # Двумя сообщениями: клавиатуру меню и список тем в одном
        # сообщении Telegram не отдаёт - разметка там только одна.
        await message.answer(i18n.t(lang, "SUPPORT_CANCELLED"),
                             reply_markup=kb.main_menu(lang))
        faq_text, faq_kb = home(fresh)
        await message.answer(faq_text, reply_markup=faq_kb)
    else:
        await message.answer(i18n.t(lang, "SUPPORT_CANCELLED"),
                             reply_markup=kb.main_menu(lang))
    return True


# Обработчик состояния поддержки регистрируется РАНЬШЕ кнопок меню: иначе
# кнопка, набранная текстом посреди вопроса, уходила бы в обработчик кнопки,
# человек молча оставался в режиме вопроса - и его следующее сообщение
# неожиданно уезжало карточкой в чат модерации.
@router.message(StateIs(logic.WAIT_SUPPORT), F.text)
async def st_support(message: Message, bot: Bot, db: Database, cfg: Config,
                     user: dict) -> None:
    text = message.text.strip()
    lang = i18n.user_lang(user)
    if i18n.button_key(text) == "BTN_SUPPORT":
        # Уже в режиме вопроса - просто напоминаем, чего ждём.
        await message.answer(i18n.t(lang, "SUPPORT_PROMPT"),
                             reply_markup=kb.support_cancel(lang))
        return
    if await menu_shortcut(message, bot, db, cfg, user, text,
                           state=logic.WAIT_SUPPORT):
        return

    question = logic.support_question(message.text)
    if not question.ok:
        await message.answer(i18n.err(user.get("lang"), question.error))
        return

    # Тема вопроса: по ней бот отвечает сам и по ней же руководитель
    # разбирает поток карточек. Вопрос уходит человеку в любом случае -
    # автоответ может промахнуться, а потерянный вопрос клиент не простит.
    intent = faq.match(question.value)

    # Карточка уходит в чат модерации ДО ответа пользователю: если она
    # не дошла (бот выкинут из чата), человек должен узнать об этом сразу,
    # а не ждать ответа, которого никто не увидит.
    try:
        sent = await bot.send_message(
            cfg.admin_chat_id,
            texts.SUPPORT_CARD.format(
                topic=_topic_line(intent),
                fio=logic.esc(user.get("full_name") or "без имени"),
                handle=("@" + logic.esc(user["username"])
                        if user.get("username") else "без username"),
                tg_id=user["tg_id"],
                question=logic.esc(question.value),
            ),
        )
    except TelegramAPIError:
        log.exception("вопрос в поддержку от %s не доставлен", user["tg_id"])
        await db.patch(user["tg_id"], expected_state=logic.WAIT_SUPPORT,
                       state=logic.APPROVED)
        await message.answer(i18n.t(lang, "SUPPORT_FAILED"),
                             reply_markup=kb.main_menu(lang))
        return

    await db.patch(user["tg_id"], expected_state=logic.WAIT_SUPPORT,
                   state=logic.APPROVED,
                   support_chat_id=sent.chat.id, support_message_id=sent.message_id)
    await db.log_event(user["tg_id"], "support_question",
                       {"intent": intent.code if intent else None})

    if intent is None:
        await message.answer(i18n.t(lang, "SUPPORT_SENT"),
                             reply_markup=kb.main_menu(lang))
        return
    # Красная линия (долг, угон, суд, скидка, 18-): бот отдаёт нейтральную
    # фразу и молчит по существу. Ответ на такую тему - это либо цена,
    # которую бот не вправе назначать, либо юридический риск.
    await message.answer(reply_for(intent, user, cfg),
                         reply_markup=kb.main_menu(lang))
    if not intent.red:
        await message.answer(i18n.t(lang, "SUPPORT_SENT_ANSWERED"))


def _topic_line(intent: faq.Intent | None) -> str:
    """Шапка карточки: тема, метка и отвечал ли бот."""
    if intent is None:
        return texts.SUPPORT_TOPIC_NONE
    if intent.red:
        return texts.SUPPORT_TOPIC_RED.format(title=intent.title,
                                              label=intent.label)
    label = f" · метка <b>{intent.label}</b>" if intent.label else ""
    return texts.SUPPORT_TOPIC_BOT.format(title=intent.title, label=label)


@router.message(StateIs(logic.WAIT_SUPPORT))
async def st_support_wrong(message: Message, user: dict) -> None:
    await message.answer(i18n.t(user.get("lang"), "SUPPORT_AS_TEXT"))


# ─────────────────── повторная аренда по запросу клиента ───────────────────

async def start_rent(message: Message, bot: Bot, db: Database, cfg: Config,
                     user: dict) -> None:
    """«Арендовать»: у действующего клиента - заявка на повторную выдачу.

    Первую аренду оформляет регистрация. Эта ветка - для клиента
    с подписанным договором и без велосипеда на руках: заявка уходит
    оператору, тот отвечает на неё данными выдачи (вин-номера, комплект,
    срок, оплата), дальше обычная цепочка «оплата -> Акт приёма-передачи».
    """
    lang = i18n.user_lang(user)
    if (user.get("status") != logic.ST_APPROVED
            or user.get("contract_status") != logic.CT_SIGNED):
        # Регистрация не пройдена до конца - новую аренду начинать не с чего.
        await message.answer(i18n.t(lang, "TARIFFS"))
        return
    if logic.rental_is_active(user):
        given = logic.issue_context(user.get("issue_data"))
        await message.answer(
            i18n.t(lang, "RENT_ALREADY_ACTIVE").format(
                bike=logic.esc(given["bike_model"]),
                term=logic.esc(given["rent_term"])),
            reply_markup=kb.main_menu(lang))
        return

    tg_id = user["tg_id"]
    # Заявка уходит ДО ответа клиенту: не дошла - он должен узнать сразу,
    # а не ждать оператора, которого никто не позвал.
    try:
        sent = await bot.send_message(
            cfg.contract_chat_id,
            texts.RENT_REQUEST_CARD.format(
                fio=logic.esc(user.get("full_name") or "без имени"),
                handle=("@" + logic.esc(user["username"])
                        if user.get("username") else "без username"),
                tg_id=tg_id, number=logic.esc(user.get("contract_no") or ""),
                form=logic.ISSUE_FORM_TEMPLATE))
    except TelegramAPIError:
        log.exception("заявка на аренду от %s не доставлена", tg_id)
        await message.answer(i18n.t(lang, "RENT_REQUEST_FAILED"),
                             reply_markup=kb.main_menu(lang))
        return
    # Карточка заявки перевязывает ответ оператора на себя: форма выдачи
    # придёт ей, а не приглашению первой выдачи месячной давности.
    await db.patch(tg_id, issue_chat_id=sent.chat.id,
                   issue_message_id=sent.message_id)
    await db.log_event(tg_id, "rent_requested")
    await message.answer(i18n.t(lang, "RENT_REQUEST_SENT"),
                         reply_markup=kb.main_menu(lang))


# ─────────────────── закрытие аренды по запросу клиента ───────────────────

async def start_close(message: Message, db: Database, user: dict) -> None:
    """«Закрыть аренду»: спрашиваем причину и уходим в отдельное состояние.

    Причина нужна не из любопытства - она обязательная строка отчёта
    о закрытии, и спросить её у человека дешевле, чем выпытывать потом.
    """
    lang = i18n.user_lang(user)
    if not logic.rental_is_active(user):
        await message.answer(i18n.t(lang, "CLOSE_NO_RENTAL"),
                             reply_markup=kb.main_menu(lang))
        return
    if not await db.patch(user["tg_id"], expected_state=logic.APPROVED,
                          state=logic.WAIT_CLOSE_REASON):
        await message.answer(i18n.t(lang, "MENU_PROMPT"),
                             reply_markup=kb.main_menu(lang))
        return
    await message.answer(i18n.t(lang, "CLOSE_ASK_REASON"),
                         reply_markup=kb.support_cancel(lang))


@router.message(StateIs(logic.WAIT_CLOSE_REASON), F.text)
async def st_close_reason(message: Message, bot: Bot, db: Database, cfg: Config,
                          user: dict) -> None:
    """Причина от клиента -> запрос оператору с формой закрытия."""
    text = message.text.strip()
    if await menu_shortcut(message, bot, db, cfg, user, text,
                           state=logic.WAIT_CLOSE_REASON):
        return
    reason = logic.close_reason(text)
    if not reason.ok:
        await message.answer(i18n.err(user.get("lang"), reason.error))
        return

    tg_id = user["tg_id"]
    number = user.get("contract_no") or ""
    # Запрос уходит ДО ответа клиенту: не дошёл - человек должен узнать
    # сразу, а не ждать оператора, которого никто не позвал.
    try:
        sent = await bot.send_message(
            cfg.contract_chat_id,
            texts.CLOSE_CARD.format(
                fio=logic.esc(user.get("full_name") or "без имени"), tg_id=tg_id,
                number=logic.esc(number), reason=logic.esc(reason.value),
                form=logic.close_form_template()))
    except TelegramAPIError:
        log.exception("запрос на закрытие от %s не доставлен", tg_id)
        await db.patch(tg_id, expected_state=logic.WAIT_CLOSE_REASON,
                       state=logic.APPROVED)
        await message.answer(i18n.t(user.get("lang"), "CLOSE_REQUEST_FAILED"),
                             reply_markup=kb.main_menu(i18n.user_lang(user)))
        return

    # Карточка запроса перевязывает на себя ответ оператора: форму он
    # пришлёт именно ей, а не приглашению возврата месячной давности.
    await db.patch(tg_id, expected_state=logic.WAIT_CLOSE_REASON,
                   state=logic.APPROVED, close_reason=reason.value,
                   close_requested_at=utcnow(),
                   return_chat_id=sent.chat.id, return_message_id=sent.message_id)
    await db.log_event(tg_id, "close_requested")
    lang = i18n.user_lang(user)
    await message.answer(i18n.t(lang, "CLOSE_REQUESTED"),
                         reply_markup=kb.main_menu(lang))


@router.message(StateIs(logic.WAIT_CLOSE_REASON))
async def st_close_reason_wrong(message: Message, user: dict) -> None:
    lang = i18n.user_lang(user)
    await message.answer(i18n.t(lang, "CLOSE_ASK_REASON"),
                         reply_markup=kb.support_cancel(lang))


# ─────────────────────────── кнопки меню ───────────────────────────

@router.message(F.text.in_(i18n.variants("BTN_TARIFFS")))
async def tariffs(message: Message, user: dict) -> None:
    await message.answer(i18n.t(user.get("lang"), "TARIFFS"))


@router.message(F.text.in_(i18n.variants("BTN_RENT")))
async def rent(message: Message, bot: Bot, db: Database, cfg: Config,
               user: dict) -> None:
    await start_rent(message, bot, db, cfg, user)


async def rentals_text(db: Database, user: dict) -> str:
    lang = i18n.user_lang(user)
    """История аренд: текущая сверху, закрытые - из журнала событий."""
    lines = []
    if logic.rental_is_active(user):
        given = logic.issue_context(user.get("issue_data"))
        lines.append(i18n.t(lang, "TRIPS_ACTIVE").format(
            number=logic.esc(user.get("contract_no") or "—"),
            bike=logic.esc(given["bike_model"]), term=logic.esc(given["rent_term"])))
    for row in await db.rentals_of(user["tg_id"]):
        payload = row["payload"] or {}
        if isinstance(payload, str):        # база без кодека jsonb
            payload = json.loads(payload)
        lines.append(i18n.t(lang, "TRIPS_CLOSED").format(
            number=logic.esc(payload.get("number") or "—"),
            bike=logic.esc(payload.get("bike") or "—"),
            term=logic.esc(payload.get("term") or "—"),
            closed_at=logic.esc(payload.get("closed_at") or "—")))
    if not lines:
        return i18n.t(lang, "TRIPS_EMPTY")
    return i18n.t(lang, "TRIPS_HEADER") + "\n".join(lines)


@router.message(F.text.in_(i18n.variants("BTN_CLOSE_RENT")))
async def close_request(message: Message, db: Database, user: dict) -> None:
    await start_close(message, db, user)


@router.message(F.text.in_(i18n.variants("BTN_TRIPS")))
async def trips(message: Message, db: Database, user: dict) -> None:
    await message.answer(await rentals_text(db, user))


async def start_support(message: Message, db: Database, user: dict) -> None:
    """Вход в диалог с поддержкой.

    Отдельное состояние обязательно: без него следующее сообщение человека
    провалилось бы в ловушку меню, и вопрос ушёл бы в никуда.
    """
    lang = i18n.user_lang(user)
    if not await db.patch(user["tg_id"], expected_state=logic.APPROVED,
                          state=logic.WAIT_SUPPORT):
        await message.answer(i18n.t(lang, "MENU_PROMPT"),
                             reply_markup=kb.main_menu(lang))
        return
    await message.answer(i18n.t(lang, "SUPPORT_PROMPT"),
                         reply_markup=kb.support_cancel(lang))


@router.message(F.text.in_(i18n.variants("BTN_SUPPORT")))
async def support(message: Message, db: Database, user: dict) -> None:
    # Сюда доходят только состояния, не перехваченные ранними роутерами, -
    # то есть approved; guard в start_support закрывает гонку двойного нажатия.
    await start_support(message, db, user)




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
        await message.answer(i18n.t(user.get("lang"), "WELCOME"),
                             reply_markup=kb.remove())
        return
    lang = i18n.user_lang(user)
    await message.answer(i18n.t(lang, "MENU_PROMPT"),
                         reply_markup=kb.main_menu(lang))


# Кнопка из старого сообщения в состоянии, где её уже не ждут. Без ответа
# на callback Telegram крутит часики у пользователя до таймаута.
@router.callback_query()
async def stale_callback(callback: CallbackQuery) -> None:
    await callback.answer("Кнопка устарела, отправьте /start")
