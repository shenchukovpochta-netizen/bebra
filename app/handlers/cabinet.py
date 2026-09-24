"""Личный кабинет клиента в Telegram: баланс, тариф, «оплачено до»,
история операций, договор, пополнение баланса.

Данные - из CRM (схема crm), связь с bot.users - по tg_id и телефону.
Клиент, заведённый в панели руками до регистрации в боте, привязывает
аккаунт, поделившись контактом; зарегистрированный в боте - привязывается
сам по телефону из анкеты.

Роутер подключается ПЕРВЫМ: /cabinet должен открываться с любого шага
анкеты, а ответ оператора суммой на карточку заявки - не доехать до общего
обработчика реплаев модерации. Поэтому каждый обработчик здесь отфильтрован
узко: команда, своя кнопка, свои callback'и, реплай именно на карточку заявки.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, Command
from aiogram.types import CallbackQuery, FSInputFile, Message

from .. import i18n, logic, texts
from .. import keyboards as kb
from ..config import Config
from ..crm import company, notices, notify, paying, service
from ..crm import logic as crm_logic
from ..crm import sync as crm_sync
from ..db import Database
from ..filters import ServiceChatReply, StateIs, is_operator
from ..services import tochka

log = logging.getLogger(__name__)
router = Router(name="cabinet")

# Состояния, из которых кнопка меню означает «передумал»: как в menu_shortcut,
# иначе следующее сообщение человека уехало бы вопросом в поддержку.
_DIALOG_STATES = (logic.WAIT_SUPPORT, logic.WAIT_CLOSE_REASON)

KIND_KEY = {
    "payment": "CAB_KIND_PAYMENT", "charge": "CAB_KIND_CHARGE",
    "fine": "CAB_KIND_FINE", "refund": "CAB_KIND_REFUND",
    "adjust": "CAB_KIND_ADJUST", "bonus": "CAB_KIND_BONUS",
}


# ─────────────────────────── клиент по пользователю ───────────────────────────

async def resolve_client(crm: Any, user: dict) -> dict | None:
    """Карточка клиента для пользователя бота, с автопривязкой по телефону.

    Зарегистрированный в боте человек получает карточку без вопросов:
    телефон у него уже есть. Без телефона (не дошёл до анкеты) - None,
    и кабинет попросит поделиться контактом.
    """
    client = await crm.client_by_tg(user["tg_id"])
    if client is not None:
        return client
    if not user.get("phone"):
        return None
    return await crm_sync.client_from_bot(crm, user)


def _file_exists(path: str) -> bool:
    """Синхронная проверка файла: договор лежит на локальном диске, и ради
    одного stat тянуть anyio незачем."""
    return os.path.exists(path)


def _support_url() -> str:
    return logic.esc(company.support_url())


async def home_text(crm: Any, client: dict, lang: str, *, today: date | None = None) -> str:
    balance = await crm.client_balance(client["id"])
    rental = await crm.active_rental_of(client["id"])
    s = crm_logic.rental_summary(rental, balance, today=today or date.today())
    if s["active"]:
        if s["overdue"]:
            left = i18n.t(lang, "CAB_OVERDUE").format(
                days=-s["days_left"], debt=crm_logic.money(s["due"]))
        elif s["days_left"] == 0:
            left = i18n.t(lang, "CAB_LEFT_TODAY")
        else:
            left = i18n.t(lang, "CAB_LEFT_DAYS").format(days=s["days_left"])
        rental_block = i18n.t(lang, "CAB_RENTAL").format(
            bike=logic.esc(s["bike"] or "—"), tariff=logic.esc(s["tariff_name"]),
            price=crm_logic.money(s["price"]), days=s["period_days"],
            until=s["covered_until"].strftime("%d.%m.%Y"), left=left)
        # Что клиент сказал про срок - видно и ему: иначе он нажмёт
        # «продлю» второй раз, не понимая, услышали ли его.
        intent = crm_logic.intent_state(rental, s, today=today or date.today())
        if intent.get("intent") in ("renew", "return"):
            rental_block += "\n" + i18n.t(
                lang, "CAB_INTENT_LINE_RENEW" if intent["intent"] == "renew"
                else "CAB_INTENT_LINE_RETURN")
    else:
        rental_block = i18n.t(lang, "CAB_NO_RENTAL")
        booking = await crm.open_booking_of(client["id"])
        if booking is not None:
            rental_block += "\n" + i18n.t(lang, "CAB_BOOK_LINE").format(
                line=logic.esc(crm_logic.booking_line(booking)))
    if s["due"]:
        hint = i18n.t(lang, "CAB_HINT_DEBT").format(amount=crm_logic.money(s["due"]))
    else:
        hint = i18n.t(lang, "CAB_HINT_OK")
    return i18n.t(lang, "CAB_HOME").format(
        name=logic.esc(client.get("full_name") or ""),
        balance=crm_logic.money(balance), rental=rental_block, hint=hint)


async def home_markup(crm: Any, client: dict, lang: str) -> Any:
    """Клавиатура кабинета: «продлю / сдаю» при идущей аренде, иначе
    заявка на велосипед или снятие поданной."""
    active = await crm.active_rental_of(client["id"]) is not None
    booking = (not active) and await crm.open_booking_of(client["id"]) is not None
    return kb.cabinet(lang, active=active, booking=booking)


async def show_home(send: Any, crm: Any, user: dict, client: dict | None) -> None:
    """Экран кабинета или приглашение привязать номер."""
    lang = i18n.user_lang(user)
    if client is None:
        await send(i18n.t(lang, "CAB_LINK_PROMPT"), reply_markup=kb.share_contact(lang))
        return
    if client.get("status") != "active":
        await send(i18n.t(lang, "CAB_BLOCKED").format(url=_support_url()),
                   reply_markup=kb.main_menu(lang))
        return
    await send(await home_text(crm, client, lang),
               reply_markup=await home_markup(crm, client, lang))


async def _open(message: Message, db: Database, crm: Any, user: dict) -> None:
    lang = i18n.user_lang(user)
    if crm is None:
        await message.answer(i18n.t(lang, "CAB_UNAVAILABLE"))
        return
    state = user.get("state")
    if state in _DIALOG_STATES:
        await db.patch(user["tg_id"], expected_state=state, state=logic.APPROVED)
    await show_home(message.answer, crm, user, await resolve_client(crm, user))


# user приходит из middleware, но у апдейтов из служебного чата (ответ
# оператора на карточку, команда парка) его нет: они идут мимо
# пользовательского конвейера. Роутер стоит первым, поэтому такие
# обработчики обязаны переживать user=None, а не падать на TypeError -
# упавший апдейт остаётся в processing и переигрывается впустую.

@router.message(Command("cabinet"))
async def cmd_cabinet(message: Message, db: Database, user: dict | None = None,
                      crm: Any = None) -> None:
    if user is not None:
        await _open(message, db, crm, user)


@router.message(F.text.in_(i18n.variants("BTN_CABINET")))
async def btn_cabinet(message: Message, db: Database, user: dict | None = None,
                      crm: Any = None) -> None:
    if user is not None:
        await _open(message, db, crm, user)


# ─────────────────────────── привязка по контакту ───────────────────────────

@router.message(F.contact, ~StateIs(logic.WAIT_CONTACT))
async def link_by_contact(message: Message, db: Database, user: dict | None = None,
                          crm: Any = None) -> None:
    """Контакт вне шага анкеты - это привязка кабинета.

    Шаг анкеты WAIT_CONTACT исключён фильтром: там контакт - ответ
    на вопрос регистрации, и его ждёт свой обработчик. Контакт ответом
    в служебном чате (оператор переслал номер клиента коллеге) - не наш.
    """
    if user is None:
        return
    lang = i18n.user_lang(user)
    if crm is None:
        await message.answer(i18n.t(lang, "CAB_UNAVAILABLE"))
        return
    contact = message.contact
    if not logic.contact_belongs_to_sender(contact.user_id, message.from_user.id):
        await message.answer(i18n.t(lang, "CONTACT_FOREIGN"),
                             reply_markup=kb.share_contact(lang))
        return
    phone = logic.normalize_phone(contact.phone_number)
    client = await crm.client_by_tg(user["tg_id"])
    if client is None and phone:
        client = await crm.client_by_phone(phone)
        if client is not None and client.get("tg_id") not in (None, user["tg_id"]):
            client = None            # номер за другим аккаунтом - как не найден
        elif client is not None:
            if not await crm.link_client_tg(client["id"], user["tg_id"],
                                            user.get("username")):
                client = None
    if client is None:
        await message.answer(i18n.t(lang, "CAB_NOT_FOUND").format(url=_support_url()),
                             reply_markup=kb.main_menu(lang)
                             if user.get("state") == logic.APPROVED else kb.remove())
        return
    await message.answer(i18n.t(lang, "CAB_LINKED").format(
        name=logic.esc(client.get("full_name") or "")),
        reply_markup=kb.main_menu(lang) if user.get("state") == logic.APPROVED
        else kb.remove())
    await show_home(message.answer, crm, user, client)


# ─────────────────────────── экраны кабинета ───────────────────────────

async def _client_for_callback(callback: CallbackQuery, bot: Bot, crm: Any,
                               user: dict) -> dict | None:
    """Клиент для callback-кнопки; None уже отвечен пользователю."""
    lang = i18n.user_lang(user)
    if crm is None:
        await callback.answer(i18n.t(lang, "CAB_UNAVAILABLE"), show_alert=True)
        return None
    client = await resolve_client(crm, user)
    if client is None:
        await callback.answer()
        await bot.send_message(user["tg_id"], i18n.t(lang, "CAB_LINK_PROMPT"),
                               reply_markup=kb.share_contact(lang))
        return None
    if client.get("status") != "active":
        await callback.answer()
        await bot.send_message(user["tg_id"],
                               i18n.t(lang, "CAB_BLOCKED").format(url=_support_url()))
        return None
    return client


@router.callback_query(F.data == "cab:home")
async def cb_home(callback: CallbackQuery, bot: Bot, user: dict,
                  crm: Any = None) -> None:
    client = await _client_for_callback(callback, bot, crm, user)
    if client is None:
        return
    await callback.answer()
    lang = i18n.user_lang(user)
    # Новым сообщением, а не правкой старого: у старого сообщения Telegram
    # может отдать недоступный объект, а кабинет открывают и из напоминаний.
    await bot.send_message(user["tg_id"], await home_text(crm, client, lang),
                           reply_markup=await home_markup(crm, client, lang))


def acquiring_for(cfg: Config) -> Any:
    """Эквайринг Точки, если он настроен. Тумблер владельца в панели
    проверяет вызывающий: он читает настройки, а этот помощник - нет."""
    if not (cfg.tochka_token and cfg.tochka_customer_code):
        return None
    return tochka.TochkaClient(token=cfg.tochka_token,
                               customer_code=cfg.tochka_customer_code,
                               account_id=cfg.tochka_account_id)


async def acquiring_live(cfg: Config, crm: Any) -> Any:
    """Эквайринг, настроенный И не выключенный владельцем в панели."""
    if not crm_logic.acquiring_enabled(await crm.settings()):
        return None
    return acquiring_for(cfg)


async def pay_link(cfg: Config, client: dict, amount: Any) -> str:
    """Ссылка на оплату без счёта: с чеком 54-ФЗ, если эквайринг настроен.

    Остаётся для напоминаний и старого пути: сам кабинет теперь выставляет
    счёт (см. cb_pay_option) - его оплату банк подтверждает сам.
    """
    acquiring = acquiring_for(cfg)
    if acquiring is None or not amount or amount <= 0:
        return cfg.pay_url
    contract = str(client.get("contract_no") or "").strip()
    purpose = f"Аренда велосипеда{', договор ' + contract if contract else ''}"
    try:
        got = await acquiring.payment_link(
            amount=crm_logic.to_money(amount), purpose=purpose,
            client_phone=client.get("phone"))
    except Exception:                                   # noqa: BLE001
        log.warning("ссылка с чеком не получена, отдаём обычную", exc_info=True)
        return cfg.pay_url
    return got.get("link") or cfg.pay_url


def _pay_options(summary: dict, lang: str) -> list[dict]:
    """Кнопки сумм с подписями: логика считает, кабинет подписывает."""
    options = crm_logic.topup_options(summary)
    for option in options:
        option["label_amount"] = crm_logic.money(option["amount"])
        option["days"] = summary.get("period_days") or 0
    return options


@router.callback_query(F.data == "cab:pay")
async def cb_pay(callback: CallbackQuery, bot: Bot, cfg: Config, user: dict,
                 crm: Any = None) -> None:
    """Экран пополнения: долг и периоды вперёд кнопками.

    Сумму клиент выбирает, а не вписывает: аренда платится периодами,
    и произвольная цифра лишь путала бы «оплачено до».
    """
    client = await _client_for_callback(callback, bot, crm, user)
    if client is None:
        return
    await callback.answer()
    lang = i18n.user_lang(user)
    balance = await crm.client_balance(client["id"])
    rental = await crm.active_rental_of(client["id"])
    summary = crm_logic.rental_summary(rental, balance, today=date.today())
    options = _pay_options(summary, lang)
    if not options:
        await bot.send_message(user["tg_id"],
                               i18n.t(lang, "CAB_PAY_NOTHING").format(url=_support_url()),
                               reply_markup=kb.cab_back(lang))
        return
    hint = (i18n.t(lang, "CAB_HINT_DEBT").format(amount=crm_logic.money(summary["due"]))
            if summary.get("due") else i18n.t(lang, "CAB_HINT_OK"))
    await bot.send_message(
        user["tg_id"],
        i18n.t(lang, "CAB_PAY_PICK").format(balance=crm_logic.money(balance), hint=hint),
        reply_markup=kb.cab_pay_options(options, lang))


@router.callback_query(F.data.regexp(r"^cab:pay:(debt|p\d)$"))
async def cb_pay_option(callback: CallbackQuery, bot: Bot, cfg: Config, user: dict,
                        crm: Any = None) -> None:
    """Сумма выбрана: счёт эквайринга со ссылкой банка, иначе СБП.

    Со счётом оплату подтверждает банк, и оператор к ней не прикасается:
    опрос счетов зачислит деньги сам. Без эквайринга - прежний путь:
    ссылка СБП и «я оплатил(а)», заявку разбирает оператор.
    """
    client = await _client_for_callback(callback, bot, crm, user)
    if client is None:
        return
    lang = i18n.user_lang(user)
    code = str(callback.data or "").rsplit(":", 1)[-1]
    balance = await crm.client_balance(client["id"])
    rental = await crm.active_rental_of(client["id"])
    summary = crm_logic.rental_summary(rental, balance, today=date.today())
    amount = crm_logic.topup_amount(summary, code)
    if amount is None:
        await callback.answer(i18n.t(lang, "CAB_PAY_STALE"), show_alert=True)
        return
    await callback.answer()
    acquiring = await acquiring_live(cfg, crm)
    if acquiring is not None:
        order = await service.create_pay_order(
            crm, client=client, rental=rental, amount=amount, by="кабинет",
            acquiring=acquiring)
        link = str(order.get("link") or "")
        if order.get("status") == "sent" and link:
            await bot.send_message(
                user["tg_id"],
                i18n.t(lang, "CAB_PAY_ORDER").format(
                    no=logic.esc(order["no"]), amount=crm_logic.money(amount),
                    link=logic.esc(link)),
                reply_markup=kb.cab_pay_order(link, int(order["id"]), lang))
            return
        # Банк не выдал ссылку - счёт остался с текстом отказа для
        # оператора, а клиенту нужен хоть какой-то способ заплатить.
    url = cfg.pay_url
    await bot.send_message(
        user["tg_id"],
        i18n.t(lang, "CAB_PAY").format(
            amount=crm_logic.money(amount), pay_url=logic.esc(url)),
        reply_markup=kb.cab_pay(url, lang, code=code))


@router.callback_query(F.data.regexp(r"^cab:paycheck:\d+$"))
async def cb_paycheck(callback: CallbackQuery, bot: Bot, db: Database, cfg: Config,
                      user: dict, crm: Any = None) -> None:
    """«Проверить оплату»: спросить банк сейчас, не дожидаясь опроса.
    Клиент стоит у оператора и ждёт зелёной отметки."""
    client = await _client_for_callback(callback, bot, crm, user)
    if client is None:
        return
    lang = i18n.user_lang(user)
    order = await crm.pay_order(int(str(callback.data).rsplit(":", 1)[-1]))
    if order is None or int(order["client_id"]) != int(client["id"]):
        await callback.answer()
        return
    if order["status"] in crm_logic.PAY_OPEN:
        state = await service.check_pay_order(crm, order,
                                              acquiring=await acquiring_live(cfg, crm))
        if state == "paid":
            # Тот же путь, что у минутного опроса: карточка команде, бонус
            # агенту; зачисление клиент видит прямо здесь, ответом.
            fresh = await crm.pay_order(order["id"]) or order
            await paying.tell_paid(bot, db, crm, cfg, fresh)
        order = await crm.pay_order(order["id"]) or order
    if order["status"] == "paid":
        await callback.answer()
        balance = await crm.client_balance(client["id"])
        await bot.send_message(
            user["tg_id"],
            i18n.t(lang, "CAB_PAY_DONE").format(
                no=logic.esc(order["no"]), amount=crm_logic.money(order["amount"]),
                balance=crm_logic.money(balance)),
            reply_markup=kb.cabinet_entry(lang))
        return
    if order["status"] in crm_logic.PAY_OPEN:
        await callback.answer(i18n.t(lang, "CAB_PAY_PENDING"), show_alert=True)
        return
    await callback.answer()
    await bot.send_message(user["tg_id"],
                           i18n.t(lang, "CAB_PAY_FAILED").format(no=logic.esc(order["no"])),
                           reply_markup=kb.cab_back(lang))


@router.callback_query(F.data.regexp(r"^cab:intent:(renew|return)$"))
async def cb_intent(callback: CallbackQuery, bot: Bot, user: dict,
                    crm: Any = None) -> None:
    """«Продлю» / «Сдаю»: то же намерение, что оператор ставит в панели,
    только сказано самим клиентом. Кормит прогноз освобождения."""
    client = await _client_for_callback(callback, bot, crm, user)
    if client is None:
        return
    lang = i18n.user_lang(user)
    rental = await crm.active_rental_of(client["id"])
    if rental is None:
        await callback.answer(i18n.t(lang, "CAB_NO_RENTAL"), show_alert=True)
        return
    intent = str(callback.data).rsplit(":", 1)[-1]
    balance = await crm.client_balance(client["id"])
    summary = crm_logic.rental_summary(rental, balance, today=date.today())
    await crm.update_rental(rental["id"], intent=intent,
                            intent_until=summary.get("covered_until"),
                            intent_by="клиент", intent_at=datetime.now(UTC),
                            snooze_until=None)
    await callback.answer()
    until = summary.get("covered_until")
    text = (i18n.t(lang, "CAB_INTENT_RENEW") if intent == "renew"
            else i18n.t(lang, "CAB_INTENT_RETURN").format(
                until=until.strftime("%d.%m.%Y") if until else "—"))
    await bot.send_message(user["tg_id"], text)
    await bot.send_message(user["tg_id"], await home_text(crm, client, lang),
                           reply_markup=await home_markup(crm, client, lang))


def claim_card(claim: dict, balance: Any) -> str:
    return texts.CAB_CLAIM_CARD.format(
        claim_id=claim["id"], fio=logic.esc(claim.get("full_name")),
        tg_id=claim.get("tg_id") or "—", phone=logic.esc(claim.get("phone")),
        balance=crm_logic.money(balance),
        amount=crm_logic.money(claim.get("amount_hint")))


@router.callback_query(F.data.regexp(r"^cab:paid(:(debt|p\d))?$"))
async def cb_paid(callback: CallbackQuery, bot: Bot, cfg: Config, user: dict,
                  crm: Any = None) -> None:
    """«Я оплатил(а)»: заявка оператору. Сумму называет оператор, сверив
    поступление; клиентская кнопка - только сигнал проверить. Код суммы
    в callback - подсказка, сколько ждать."""
    client = await _client_for_callback(callback, bot, crm, user)
    if client is None:
        return
    lang = i18n.user_lang(user)
    if await crm.pending_claim_of(client["id"]) is not None:
        await callback.answer(i18n.t(lang, "CAB_CLAIM_EXISTS"), show_alert=True)
        return
    balance = await crm.client_balance(client["id"])
    rental = await crm.active_rental_of(client["id"])
    summary = crm_logic.rental_summary(rental, balance, today=date.today())
    parts = str(callback.data or "").split(":")
    hint = (crm_logic.topup_amount(summary, parts[2]) if len(parts) > 2 else None) \
        or crm_logic.topup_hint(summary)
    # None - заявка уже открыта: второе нажатие пришло параллельно и
    # упёрлось в уникальный индекс. Для человека это то же самое, что
    # увидеть «заявка уже есть», а оператору вторая карточка на один
    # платёж означала бы риск зачислить его дважды.
    claim_id = await crm.create_claim(client["id"], hint or None)
    if claim_id is None:
        await callback.answer(i18n.t(lang, "CAB_CLAIM_EXISTS"), show_alert=True)
        return
    claim = await crm.claim(claim_id) or {"id": claim_id, **client, "amount_hint": hint}
    await callback.answer(i18n.t(lang, "CAB_CLAIM_TOAST"))
    try:
        sent = await bot.send_message(
            cfg.contract_chat_id, claim_card(claim, balance),
            reply_markup=kb.claim_confirm(claim_id, crm_logic.money(hint)))
        await crm.set_claim_card(claim_id, sent.chat.id, sent.message_id)
    except TelegramAPIError:
        # Заявка остаётся в базе: её видно в панели, оператор зачислит оттуда.
        log.exception("карточка заявки #%s не доставлена в служебный чат", claim_id)
    await bot.send_message(user["tg_id"], i18n.t(lang, "CAB_CLAIM_SENT"),
                           reply_markup=kb.cab_back(lang))


class HasPendingClaim(BaseFilter):
    """Фото или файл от клиента с открытой заявкой - это чек к ней."""

    async def __call__(self, message: Message, user: dict | None = None,
                       crm: Any = None) -> bool | dict:
        if crm is None or not user:
            return False
        client = await crm.client_by_tg(user["tg_id"])
        if client is None:
            return False
        claim = await crm.pending_claim_of(client["id"])
        if claim is None:
            return False
        return {"claim": claim}


@router.message(StateIs(logic.APPROVED), F.photo | F.document, HasPendingClaim())
async def receipt(message: Message, bot: Bot, cfg: Config, user: dict, claim: dict,
                  crm: Any = None) -> None:
    """Чек к заявке: пересылается оператору ответом на карточку заявки,
    в базе остаётся file_id - панель покажет, что чек был."""
    is_photo = bool(message.photo)
    file_id = message.photo[-1].file_id if is_photo else message.document.file_id
    await crm.set_claim_receipt(claim["id"], file_id, is_photo)
    caption = texts.CAB_CLAIM_RECEIPT.format(claim_id=claim["id"],
                                             fio=logic.esc(claim.get("full_name")))
    send = bot.send_photo if is_photo else bot.send_document
    reply_to = (claim.get("card_message_id")
                if claim.get("card_chat_id") == cfg.contract_chat_id else None)
    try:
        await send(cfg.contract_chat_id, file_id, caption=caption,
                   reply_to_message_id=reply_to)
    except TelegramAPIError:
        try:
            await send(cfg.contract_chat_id, file_id, caption=caption)
        except TelegramAPIError:
            log.exception("чек к заявке #%s не доставлен", claim["id"])
    lang = i18n.user_lang(user)
    await message.answer(i18n.t(lang, "CAB_RECEIPT_ATTACHED"), reply_markup=kb.cab_back(lang))


@router.callback_query(F.data == "cab:history")
async def cb_history(callback: CallbackQuery, bot: Bot, user: dict,
                     crm: Any = None) -> None:
    client = await _client_for_callback(callback, bot, crm, user)
    if client is None:
        return
    await callback.answer()
    lang = i18n.user_lang(user)
    rows = await crm.ledger_of(client["id"], limit=15)
    if not rows:
        text = i18n.t(lang, "CAB_HISTORY_EMPTY")
    else:
        lines = []
        for r in rows:
            when = r["created_at"].strftime("%d.%m.%Y") if r.get("created_at") else ""
            label = i18n.t(lang, KIND_KEY.get(r["kind"], "CAB_KIND_ADJUST"))
            period = crm_logic.period_label(r.get("period_from"), r.get("period_to"))
            tail = f" ({period})" if period else ""
            lines.append(f"{when}  <b>{crm_logic.money_signed(r['amount'])}</b> — "
                         f"{label}{tail}")
        text = i18n.t(lang, "CAB_HISTORY").format(rows="\n".join(lines))
    await bot.send_message(user["tg_id"], text, reply_markup=kb.cab_back(lang))


@router.callback_query(F.data == "cab:contract")
async def cb_contract(callback: CallbackQuery, bot: Bot, db: Database, user: dict,
                      crm: Any = None) -> None:
    """Договор - файл из bot.users: он подписан в боте и лежит на диске
    до ретеншена. В CRM только номер."""
    client = await _client_for_callback(callback, bot, crm, user)
    if client is None:
        return
    await callback.answer()
    lang = i18n.user_lang(user)
    row = await db.get_user(user["tg_id"])
    data = dict(row) if row else {}
    path = data.get("contract_path")
    if (data.get("contract_status") != logic.CT_SIGNED or not path
            or not _file_exists(path)):
        await bot.send_message(user["tg_id"], i18n.t(lang, "CAB_CONTRACT_NONE"),
                               reply_markup=kb.cab_back(lang))
        return
    number = data.get("contract_no") or client.get("contract_no") or ""
    await bot.send_document(
        user["tg_id"], FSInputFile(path, filename=Path(path).name),
        caption=i18n.t(lang, "CAB_CONTRACT_CAPTION").format(number=logic.esc(number)),
        reply_markup=kb.cab_back(lang))


async def _bot_username(bot: Bot) -> str:
    """Имя бота для ссылки-приглашения. Недоступно - покажем один код."""
    try:
        return (await bot.get_me()).username or ""
    except Exception as exc:                             # noqa: BLE001
        log.warning("имя бота не получено: %s", exc)
        return ""


@router.callback_query(F.data == "cab:friends")
async def cb_friends(callback: CallbackQuery, bot: Bot, user: dict,
                     crm: Any = None) -> None:
    """«Мои друзья»: код приглашения, ссылка и что по ней происходит.

    Код выдаётся здесь же, при первом открытии экрана: заранее раздавать
    его всем незачем - большинство клиентов этот экран не откроют.
    """
    client = await _client_for_callback(callback, bot, crm, user)
    if client is None:
        return
    await callback.answer()
    lang = i18n.user_lang(user)
    settings = crm_logic.bonus_settings(await crm.settings())
    if not settings["enabled"]:
        await bot.send_message(user["tg_id"], i18n.t(lang, "CAB_FRIENDS_OFF"),
                               reply_markup=kb.cab_back(lang))
        return
    try:
        code = await service.ref_code_of(crm, client)
    except service.ServiceError as exc:
        await bot.send_message(user["tg_id"], str(exc), reply_markup=kb.cab_back(lang))
        return
    rows = await crm.referrals(agent_id=client["id"], limit=1000)
    funnel = crm_logic.ref_funnel(rows)
    stats = i18n.t(lang, "CAB_FRIENDS_STATS").format(
        click=funnel["click"], signed=funnel["signed"], rented=funnel["rented"],
        paid=funnel["paid"], bonus=crm_logic.money(funnel["bonus"])) \
        if rows else i18n.t(lang, "CAB_FRIENDS_EMPTY")
    # Сумма агента не задана - «0 ₽ на баланс» обещать нельзя: другой
    # текст отправляет к менеджеру, а не выдумывает число.
    key = "CAB_FRIENDS" if settings["bonus"] > 0 else "CAB_FRIENDS_NO_SUM"
    text = i18n.t(lang, key).format(
        code=code, link=crm_logic.ref_link(await _bot_username(bot), code),
        bonus=crm_logic.money(settings["bonus"]), stats=stats)
    await bot.send_message(user["tg_id"], text, reply_markup=kb.cab_back(lang))


# ─────────────────────── заявка на аренду ───────────────────────
#
# Мастер в четыре нажатия: модель -> срок -> точка -> день. Выбор едет
# в callback следующего шага (cab:book:d:<модель>:<тариф>:<точка>:<день>),
# состояния у диалога нет: клиент может отвлечься на сутки и нажать
# старую кнопку - она отработает по свежим данным или скажет, что
# устарела.

async def _book_model(crm: Any, model_id: int) -> dict | None:
    return next((m for m in await crm.bike_models(active_only=True)
                 if int(m["id"]) == model_id), None)


async def _book_tariffs(crm: Any, model: dict) -> list[dict]:
    aliases = crm_logic.model_aliases(await crm.bike_models())
    return crm_logic.tariff_tiles(crm_logic.tariffs_for_model(
        await crm.tariffs(active_only=True), model["title"], aliases=aliases))


def _when_rows(lang: str, tail: str, *, today: date) -> list[tuple[str, str]]:
    keys = ("CAB_BOOK_TODAY", "CAB_BOOK_TOMORROW", "CAB_BOOK_DAY2")
    days = [today + timedelta(days=n) for n in range(len(keys))]
    return [(i18n.t(lang, key).format(date=day.strftime("%d.%m")),
             f"cab:book:d:{tail}:{day:%Y%m%d}") for day, key in zip(days, keys, strict=True)]


@router.callback_query(F.data == "cab:book")
async def cb_book(callback: CallbackQuery, bot: Bot, user: dict,
                  crm: Any = None) -> None:
    client = await _client_for_callback(callback, bot, crm, user)
    if client is None:
        return
    lang = i18n.user_lang(user)
    if await crm.active_rental_of(client["id"]) is not None:
        await callback.answer(i18n.t(lang, "CAB_BOOK_HAS_RENTAL"), show_alert=True)
        return
    await callback.answer()
    booking = await crm.open_booking_of(client["id"])
    if booking is not None:
        await bot.send_message(
            user["tg_id"],
            i18n.t(lang, "CAB_BOOK_EXISTS").format(
                line=logic.esc(crm_logic.booking_line(booking))),
            reply_markup=kb.cab_booking(lang))
        return
    models = crm_logic.booking_models(
        await crm.bike_models(active_only=True), await crm.bikes(limit=10000),
        aliases=crm_logic.model_aliases(await crm.bike_models()))
    if not models:
        await bot.send_message(user["tg_id"],
                               i18n.t(lang, "CAB_BOOK_NO_MODELS").format(url=_support_url()),
                               reply_markup=kb.cab_back(lang))
        return
    rows = [(i18n.t(lang, "CAB_BOOK_OPT_MODEL" if m["free"] else "CAB_BOOK_OPT_MODEL_NONE")
             .format(title=m["title"], free=m["free"]), f"cab:book:m:{m['id']}")
            for m in models]
    await bot.send_message(user["tg_id"], i18n.t(lang, "CAB_BOOK_MODEL"),
                           reply_markup=kb.cab_choice(rows, lang))


@router.callback_query(F.data.regexp(r"^cab:book:m:\d+$"))
async def cb_book_model(callback: CallbackQuery, bot: Bot, user: dict,
                        crm: Any = None) -> None:
    client = await _client_for_callback(callback, bot, crm, user)
    if client is None:
        return
    lang = i18n.user_lang(user)
    model = await _book_model(crm, int(str(callback.data).rsplit(":", 1)[-1]))
    if model is None:
        await callback.answer(i18n.t(lang, "CAB_BOOK_STALE"), show_alert=True)
        return
    await callback.answer()
    tariffs = await _book_tariffs(crm, model)
    if not tariffs:
        await bot.send_message(user["tg_id"],
                               i18n.t(lang, "CAB_BOOK_NO_TARIFF").format(url=_support_url()),
                               reply_markup=kb.cab_back(lang))
        return
    rows = [(i18n.t(lang, "CAB_BOOK_OPT_TARIFF").format(
                name=t["name"], price=crm_logic.money(t["price"])),
             f"cab:book:t:{model['id']}:{t['id']}") for t in tariffs]
    await bot.send_message(user["tg_id"], i18n.t(lang, "CAB_BOOK_TARIFF"),
                           reply_markup=kb.cab_choice(rows, lang))


@router.callback_query(F.data.regexp(r"^cab:book:t:\d+:\d+$"))
async def cb_book_tariff(callback: CallbackQuery, bot: Bot, user: dict,
                         crm: Any = None) -> None:
    client = await _client_for_callback(callback, bot, crm, user)
    if client is None:
        return
    lang = i18n.user_lang(user)
    _, _, _, model_id, tariff_id = str(callback.data).split(":")
    await callback.answer()
    locations = await crm.locations(active_only=True)
    if len(locations) > 1:
        rows = [(str(loc.get("public_title") or loc["name"]),
                 f"cab:book:l:{model_id}:{tariff_id}:{loc['id']}") for loc in locations]
        await bot.send_message(user["tg_id"], i18n.t(lang, "CAB_BOOK_POINT"),
                               reply_markup=kb.cab_choice(rows, lang))
        return
    # Одна точка (или ни одной в справочнике) - выбирать нечего.
    loc_id = locations[0]["id"] if locations else 0
    await bot.send_message(
        user["tg_id"], i18n.t(lang, "CAB_BOOK_WHEN"),
        reply_markup=kb.cab_choice(
            _when_rows(lang, f"{model_id}:{tariff_id}:{loc_id}", today=date.today()), lang))


@router.callback_query(F.data.regexp(r"^cab:book:l:\d+:\d+:\d+$"))
async def cb_book_point(callback: CallbackQuery, bot: Bot, user: dict,
                        crm: Any = None) -> None:
    client = await _client_for_callback(callback, bot, crm, user)
    if client is None:
        return
    lang = i18n.user_lang(user)
    tail = str(callback.data).split(":", 3)[-1]
    await callback.answer()
    await bot.send_message(
        user["tg_id"], i18n.t(lang, "CAB_BOOK_WHEN"),
        reply_markup=kb.cab_choice(_when_rows(lang, tail, today=date.today()), lang))


@router.callback_query(F.data.regexp(r"^cab:book:d:\d+:\d+:\d+:\d+$"))
async def cb_book_when(callback: CallbackQuery, bot: Bot, cfg: Config, user: dict,
                       crm: Any = None) -> None:
    """Последний шаг: заявка записана, команде - карточка."""
    client = await _client_for_callback(callback, bot, crm, user)
    if client is None:
        return
    lang = i18n.user_lang(user)
    _, _, _, model_id, tariff_id, loc_id, day = str(callback.data).split(":")
    model = await _book_model(crm, int(model_id))
    tariff = await crm.tariff(int(tariff_id))
    wanted = crm_logic.booking_when(day, today=date.today())
    if model is None or tariff is None or wanted is None:
        await callback.answer(i18n.t(lang, "CAB_BOOK_STALE"), show_alert=True)
        return
    location = next((loc for loc in await crm.locations(active_only=True)
                     if int(loc["id"]) == int(loc_id)), None)
    try:
        booking = await service.create_booking(
            crm, client=client, model=model["title"], tariff=tariff,
            location=location, wanted_on=wanted)
    except service.ServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()
    line = crm_logic.booking_line(booking)
    await bot.send_message(user["tg_id"],
                           i18n.t(lang, "CAB_BOOK_DONE").format(line=logic.esc(line)),
                           reply_markup=kb.cabinet_entry(lang))
    await notices.send_team(
        crm, bot, "booking_new",
        texts.BOOKING_CARD.format(fio=logic.esc(client.get("full_name") or ""),
                                  phone=logic.esc(client.get("phone") or ""),
                                  line=logic.esc(line)),
        cfg.contract_chat_id, client_id=client["id"])


@router.callback_query(F.data == "cab:book:cancel")
async def cb_book_cancel(callback: CallbackQuery, bot: Bot, user: dict,
                         crm: Any = None) -> None:
    client = await _client_for_callback(callback, bot, crm, user)
    if client is None:
        return
    lang = i18n.user_lang(user)
    booking = await crm.open_booking_of(client["id"])
    if booking is not None:
        try:
            await service.cancel_booking(crm, booking, by="клиент")
        except service.ServiceError:
            pass
    await callback.answer()
    await bot.send_message(user["tg_id"], i18n.t(lang, "CAB_BOOK_CANCELLED"))
    await bot.send_message(user["tg_id"], await home_text(crm, client, lang),
                           reply_markup=await home_markup(crm, client, lang))


# ─────────────────────────── операторская часть ───────────────────────────

def _is_admin(user_id: int, cfg: Config) -> bool:
    return is_operator(cfg, user_id)


async def _mark_card(bot: Bot, claim: dict, mark: str) -> None:
    if not (claim.get("card_chat_id") and claim.get("card_message_id")):
        return
    try:
        balance = claim.get("balance_before", 0)
        await bot.edit_message_text(
            chat_id=claim["card_chat_id"], message_id=claim["card_message_id"],
            text=claim_card(claim, balance) + f"\n\n{mark}", reply_markup=None)
    except TelegramAPIError:
        pass


async def credit(bot: Bot, db: Database, crm: Any, claim: dict, amount: Any, *,
                 who: str) -> Any:
    """Зачислить заявку и сообщить клиенту. Возвращает новый баланс или None."""
    balance_before = await crm.client_balance(claim["client_id"])
    ledger_id = await service.credit_claim(crm, claim, amount, by=f"tg:{who}")
    if ledger_id is None:
        return None
    await _mark_card(bot, {**claim, "balance_before": balance_before},
                     texts.CAB_CLAIM_DONE_MARK.format(amount=crm_logic.money(amount),
                                                      who=who))
    client = await crm.client(claim["client_id"]) or claim
    paid = {**client, "id": claim["client_id"]}
    await notices.send_client(
        crm, "pay_credited", claim["client_id"],
        lambda: notify.payment_credited(bot, db, crm, paid, amount))
    # Бонус агенту, если этого клиента привёл друг. Сбой программы не
    # должен откатывать зачисление - оно уже состоялось.
    try:
        bonus = await service.ref_paid(crm, {**client, "id": claim["client_id"]},
                                       amount, by=f"tg:{who}")
    except Exception:                                    # noqa: BLE001
        log.exception("реферальный бонус за клиента %s не начислен",
                      claim["client_id"])
        bonus = None
    if bonus:
        await notify.referral_bonus(bot, db, bonus["agent"], client, bonus["bonus"])
    return await crm.client_balance(claim["client_id"])


@router.callback_query(F.data.regexp(r"^crmpay:\d+:(ok|no)$"))
async def cb_claim(callback: CallbackQuery, bot: Bot, db: Database, cfg: Config,
                   crm: Any = None) -> None:
    if not _is_admin(callback.from_user.id, cfg):
        await callback.answer(texts.MOD_NO_RIGHTS, show_alert=True)
        return
    if crm is None:
        await callback.answer(texts.CAB_UNAVAILABLE, show_alert=True)
        return
    _, claim_id, action = callback.data.split(":")
    claim = await crm.claim(int(claim_id))
    if claim is None or claim.get("status") != "pending":
        await callback.answer(texts.CAB_CLAIM_NOT_PENDING, show_alert=True)
        return
    who = callback.from_user.username or str(callback.from_user.id)
    if action == "no":
        if not await service.reject_claim(crm, claim, by=f"tg:{who}"):
            await callback.answer(texts.CAB_CLAIM_NOT_PENDING, show_alert=True)
            return
        await callback.answer(texts.CAB_CLAIM_REJECTED_TOAST)
        # Баланс в карточку передаётся явно: в самой заявке его нет, и
        # отказ переписывал карточку нулём - у должника это читалось
        # как «ничего не должен».
        balance = await crm.client_balance(claim["client_id"])
        await _mark_card(bot, {**claim, "balance_before": balance},
                         texts.CAB_CLAIM_REJECTED_MARK.format(who=who))
        await notify.payment_rejected(bot, db, claim)
        return
    amount = crm_logic.to_money(claim.get("amount_hint") or 0)
    if amount <= 0:
        await callback.answer(texts.CAB_CLAIM_NO_AMOUNT, show_alert=True)
        return
    balance = await credit(bot, db, crm, claim, amount, who=who)
    if balance is None:
        await callback.answer(texts.CAB_CLAIM_NOT_PENDING, show_alert=True)
        return
    await callback.answer(texts.CAB_CLAIM_CREDITED.format(
        amount=crm_logic.money(amount), balance=crm_logic.money(balance)))


class ClaimReply(BaseFilter):
    """Ответ оператора на карточку заявки о зачислении - и только на неё.

    Ищется по (chat_id, message_id) карточки, как и остальные карточки
    бота. Не карточка - фильтр молчит, и сообщение уходит в модерацию.
    """

    async def __call__(self, message: Message, crm: Any = None) -> bool | dict:
        if crm is None or message.reply_to_message is None:
            return False
        claim = await crm.claim_by_card(message.chat.id, message.reply_to_message.message_id)
        if claim is None:
            return False
        return {"claim": claim}


@router.message(ServiceChatReply(), ClaimReply())
async def claim_amount_reply(message: Message, bot: Bot, db: Database, cfg: Config,
                             claim: dict, crm: Any = None) -> None:
    """Оператор ответил на карточку числом: зачислить именно эту сумму."""
    if not _is_admin(message.from_user.id, cfg):
        return
    if claim.get("status") != "pending":
        await message.reply(texts.CAB_CLAIM_NOT_PENDING)
        return
    check = crm_logic.check_amount(message.text or message.caption)
    if not check.ok:
        await message.reply(texts.CAB_CLAIM_AMOUNT_BAD)
        return
    who = message.from_user.username or str(message.from_user.id)
    balance = await credit(bot, db, crm, claim, check.value, who=who)
    if balance is None:
        await message.reply(texts.CAB_CLAIM_NOT_PENDING)
        return
    await message.reply(texts.CAB_CLAIM_CREDITED.format(
        amount=crm_logic.money(check.value), balance=crm_logic.money(balance)))


@router.callback_query(F.data.startswith("est:"))
async def cb_estimate(callback: CallbackQuery, bot: Bot, cfg: Config, user: dict,
                      crm: Any = None) -> None:
    """Ответ клиента на смету: согласен или нет.

    Проверяем, что наряд именно его: callback_data подделывается легко,
    а чужой наряд отменить или согласовать клиент не должен.
    """
    parts = str(callback.data or "").split(":")
    if crm is None or len(parts) not in (3, 4) or not parts[2].isdigit():
        await callback.answer()
        return
    agree = parts[1] == "ok"
    order = await crm.work_order(int(parts[2]))
    client = await crm.client_by_tg(user["tg_id"]) if user.get("tg_id") else None
    if (order is None or client is None
            or order.get("client_id") != client["id"]):
        await callback.answer("Наряд не найден", show_alert=True)
        return
    if order.get("status") != "approve":
        await callback.answer("По этому наряду уже решили", show_alert=True)
        return
    # Кнопка под старой сметой: сумма в ней не та, что сейчас в наряде.
    # Кнопки без суммы (до этой правки) тоже не принимаются - по ним не
    # понять, какую смету человек видел.
    if len(parts) != 4 or parts[3] != str(crm_logic.cents(order.get("estimate"))):
        await callback.answer("Смета изменилась. Ответьте на последнее сообщение "
                              "со сметой или позвоните менеджеру.", show_alert=True)
        return
    try:
        await service.answer_estimate(crm, order, agree=agree, by="клиент")
    except service.ServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("Принято")
    lang = i18n.user_lang(user)
    del lang                      # текст ответа одинаков на всех языках
    await bot.send_message(
        user["tg_id"],
        (texts.ESTIMATE_OK if agree else texts.ESTIMATE_NO).format(
            no=order.get("no") or ""))
    # Технику держит наряд, и техник ждёт именно этого ответа.
    await _tell_estimate_answer(bot, crm, cfg, order, client, agree=agree)


async def _tell_estimate_answer(bot: Bot, crm: Any, cfg: Config, order: dict,
                                client: dict, *, agree: bool) -> None:
    mark = "✅ согласовал" if agree else "✖️ отказался"
    text = (f"🔧 Клиент {mark}: наряд {order.get('no')}\n"
            f"{client.get('full_name') or '—'} · "
            f"{crm_logic.money(order.get('estimate'))}")
    # Получателя этого уведомления владелец назначает в панели - обычно
    # техника, который и ждёт ответа. Прямой send в служебный чат его
    # выбор игнорировал.
    await notices.send_team(crm, bot, "order_answer", text,
                            cfg.contract_chat_id, client_id=client["id"])


@router.callback_query(F.data == "cab:review")
async def cb_review(callback: CallbackQuery, bot: Bot, user: dict,
                    crm: Any = None) -> None:
    """Экран отзыва: кнопки площадок из настроек.

    Бонус называем только когда он задан: обещать клиенту сумму, которой
    владелец не назначал, нельзя.
    """
    client = await _client_for_callback(callback, bot, crm, user)
    if client is None:
        return
    await callback.answer()
    lang = i18n.user_lang(user)
    settings = crm_logic.bonus_settings(await crm.settings())
    links = crm_logic.review_links(await crm.settings())
    if not links:
        # Клавиатура кабинета - та же, что на главном экране: с арендой
        # это «продлю / сдаю», а не «забронировать велосипед».
        await bot.send_message(user["tg_id"], texts.CAB_REVIEW_NONE,
                               reply_markup=await home_markup(crm, client, lang))
        return
    bonus = crm_logic.to_money(settings["review_bonus"])
    tail = (f"\n\nЗа опубликованный отзыв начислим "
            f"<b>{crm_logic.money(bonus)}</b> баллами — "
            f"покажите его менеджеру." if bonus > 0 else "")
    await bot.send_message(user["tg_id"], texts.CAB_REVIEW.format(bonus=tail),
                           reply_markup=kb.review_sites(links, lang))
