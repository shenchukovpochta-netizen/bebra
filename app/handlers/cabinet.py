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
from datetime import date
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, Command
from aiogram.types import CallbackQuery, FSInputFile, Message

from .. import i18n, logic, texts
from .. import keyboards as kb
from ..config import Config
from ..crm import logic as crm_logic
from ..crm import notices, notify, service
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
    return logic.esc(texts.SUPPORT_CONTACT_URL)


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
    else:
        rental_block = i18n.t(lang, "CAB_NO_RENTAL")
    if s["due"]:
        hint = i18n.t(lang, "CAB_HINT_DEBT").format(amount=crm_logic.money(s["due"]))
    else:
        hint = i18n.t(lang, "CAB_HINT_OK")
    return i18n.t(lang, "CAB_HOME").format(
        name=logic.esc(client.get("full_name") or ""),
        balance=crm_logic.money(balance), rental=rental_block, hint=hint)


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
    await send(await home_text(crm, client, lang), reply_markup=kb.cabinet(lang))


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
                           reply_markup=kb.cabinet(lang))


async def pay_link(cfg: Config, client: dict, amount: Any) -> str:
    """Ссылка на оплату: с чеком 54-ФЗ, если эквайринг Точки настроен.

    Чек пробивает банк - это его эквайринг принимает деньги. Не настроен
    или банк не ответил - остаётся обычная ссылка СБП из настроек: без
    оплаты клиент не уедет, а чек можно выдать и потом.
    """
    if not (cfg.tochka_token and cfg.tochka_customer_code) or not amount or amount <= 0:
        return cfg.pay_url
    contract = str(client.get("contract_no") or "").strip()
    purpose = f"Аренда велосипеда{', договор ' + contract if contract else ''}"
    try:
        got = await tochka.TochkaClient(
            token=cfg.tochka_token, customer_code=cfg.tochka_customer_code,
            account_id=cfg.tochka_account_id).payment_link(
                amount=crm_logic.to_money(amount), purpose=purpose,
                client_phone=client.get("phone"))
    except Exception:                                   # noqa: BLE001
        log.warning("ссылка с чеком не получена, отдаём обычную", exc_info=True)
        return cfg.pay_url
    return got.get("link") or cfg.pay_url


@router.callback_query(F.data == "cab:pay")
async def cb_pay(callback: CallbackQuery, bot: Bot, cfg: Config, user: dict,
                 crm: Any = None) -> None:
    client = await _client_for_callback(callback, bot, crm, user)
    if client is None:
        return
    await callback.answer()
    lang = i18n.user_lang(user)
    balance = await crm.client_balance(client["id"])
    rental = await crm.active_rental_of(client["id"])
    summary = crm_logic.rental_summary(rental, balance, today=date.today())
    amount = crm_logic.topup_hint(summary)
    url = await pay_link(cfg, client, amount)
    await bot.send_message(
        user["tg_id"],
        i18n.t(lang, "CAB_PAY").format(
            amount=crm_logic.money(amount), pay_url=logic.esc(url)),
        reply_markup=kb.cab_pay(url, lang))


def claim_card(claim: dict, balance: Any) -> str:
    return texts.CAB_CLAIM_CARD.format(
        claim_id=claim["id"], fio=logic.esc(claim.get("full_name")),
        tg_id=claim.get("tg_id") or "—", phone=logic.esc(claim.get("phone")),
        balance=crm_logic.money(balance),
        amount=crm_logic.money(claim.get("amount_hint")))


@router.callback_query(F.data == "cab:paid")
async def cb_paid(callback: CallbackQuery, bot: Bot, cfg: Config, user: dict,
                  crm: Any = None) -> None:
    """«Я оплатил(а)»: заявка оператору. Сумму называет оператор, сверив
    поступление; клиентская кнопка - только сигнал проверить."""
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
    hint = crm_logic.topup_hint(summary)
    claim_id = await crm.create_claim(client["id"], hint or None)
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
    settings = crm_logic.ref_settings(await crm.settings())
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
    text = i18n.t(lang, "CAB_FRIENDS").format(
        code=code, link=crm_logic.ref_link(await _bot_username(bot), code),
        bonus=crm_logic.money(settings["bonus"]), stats=stats)
    await bot.send_message(user["tg_id"], text, reply_markup=kb.cab_back(lang))


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
        await _mark_card(bot, claim, texts.CAB_CLAIM_REJECTED_MARK.format(who=who))
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
    if crm is None or len(parts) != 3 or not parts[2].isdigit():
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
    if not await notices.allowed(crm, "order_answer"):
        return
    mark = "✅ согласовал" if agree else "✖️ отказался"
    text = (f"🔧 Клиент {mark}: наряд {order.get('no')}\n"
            f"{client.get('full_name') or '—'} · "
            f"{crm_logic.money(order.get('estimate'))}")
    try:
        await bot.send_message(cfg.contract_chat_id, text)
    except Exception as err:                             # noqa: BLE001
        log.warning("ответ на смету не доставлен в чат: %s", err)
        await notices.record(crm, "order_answer", status="failed",
                             client_id=client["id"], detail=str(err))
        return
    await notices.record(crm, "order_answer", status="sent",
                         client_id=client["id"])


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
        await bot.send_message(user["tg_id"], texts.CAB_REVIEW_NONE,
                               reply_markup=kb.cabinet(lang))
        return
    bonus = crm_logic.to_money(settings["review_bonus"])
    tail = (f"\n\nЗа опубликованный отзыв начислим "
            f"<b>{crm_logic.money(bonus)}</b> баллами — "
            f"покажите его менеджеру." if bonus > 0 else "")
    await bot.send_message(user["tg_id"], texts.CAB_REVIEW.format(bonus=tail),
                           reply_markup=kb.review_sites(links, lang))
