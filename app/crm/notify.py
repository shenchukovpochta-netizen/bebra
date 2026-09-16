"""Уведомления клиенту в Telegram о событиях CRM.

Используется и ботом, и веб-панелью (у той свой экземпляр Bot только
для отправки). Любая ошибка доставки - предупреждение в логе, не
исключение: зачисление уже состоялось, и откатывать его из-за того,
что клиент заблокировал бота, нельзя.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from .. import i18n, texts
from .. import keyboards as kb
from .. import logic as bot_logic
from . import logic

log = logging.getLogger(__name__)


async def _lang(db: Any, tg_id: int) -> str:
    """Язык клиента - из bot.users; клиент без регистрации в боте - русский."""
    try:
        row = await db.get_user(tg_id)
    except Exception:                                    # noqa: BLE001
        return "ru"
    return i18n.user_lang(dict(row) if row else None)


async def _send(bot: Any, tg_id: int, text: str, markup: Any = None) -> bool:
    try:
        await bot.send_message(tg_id, text, reply_markup=markup)
        return True
    except Exception as exc:                             # noqa: BLE001
        log.warning("уведомление клиенту %s не доставлено: %s", tg_id, exc)
        return False


def rental_line(lang: str, summary: dict) -> str:
    if not summary.get("active"):
        return ""
    return i18n.t(lang, "CAB_PAID_UNTIL").format(
        until=summary["covered_until"].strftime("%d.%m.%Y"))


async def payment_credited(bot: Any, db: Any, crm: Any, client: dict,
                           amount: Any, *, today: date | None = None) -> bool:
    if not client.get("tg_id") or bot is None:
        return False
    lang = await _lang(db, client["tg_id"])
    balance = await crm.client_balance(client["id"])
    rental = await crm.active_rental_of(client["id"])
    summary = logic.rental_summary(rental, balance, today=today or date.today())
    text = i18n.t(lang, "CAB_PAID_CONFIRMED").format(
        amount=logic.money(amount), balance=logic.money(balance),
        rental=rental_line(lang, summary)).rstrip()
    return await _send(bot, client["tg_id"], text, kb.cabinet_entry(lang))


async def payment_rejected(bot: Any, db: Any, client: dict) -> bool:
    if not client.get("tg_id") or bot is None:
        return False
    lang = await _lang(db, client["tg_id"])
    text = i18n.t(lang, "CAB_PAID_REJECTED").format(
        url=bot_logic.esc(texts.SUPPORT_CONTACT_URL))
    return await _send(bot, client["tg_id"], text)


async def rental_opened(bot: Any, db: Any, crm: Any, client: dict, rental: dict,
                        *, today: date | None = None) -> bool:
    if not client.get("tg_id") or bot is None:
        return False
    lang = await _lang(db, client["tg_id"])
    balance = await crm.client_balance(client["id"])
    summary = logic.rental_summary(rental, balance, today=today or date.today())
    text = i18n.t(lang, "CAB_RENTAL_OPENED").format(
        bike=bot_logic.esc(summary.get("bike") or "—"),
        tariff=bot_logic.esc(rental.get("tariff_name") or ""),
        price=logic.money(rental["price"]), days=rental["period_days"],
        until=(summary["covered_until"].strftime("%d.%m.%Y")
               if summary.get("covered_until") else "—"),
        balance=logic.money(balance))
    return await _send(bot, client["tg_id"], text, kb.cabinet_entry(lang))


async def rental_closed(bot: Any, db: Any, crm: Any, client: dict, rental: dict) -> bool:
    if not client.get("tg_id") or bot is None:
        return False
    lang = await _lang(db, client["tg_id"])
    balance = await crm.client_balance(client["id"])
    bike = " ".join(x for x in (rental.get("bike_model"), rental.get("bike_code")) if x)
    text = i18n.t(lang, "CAB_RENTAL_CLOSED").format(
        bike=bot_logic.esc(bike or "—"), balance=logic.money(balance))
    return await _send(bot, client["tg_id"], text)


async def referral_bonus(bot: Any, db: Any, agent: dict, friend: dict,
                         amount: Any) -> bool:
    """Агенту: друг заплатил, бонус на балансе. Деньги уже начислены -
    недоставленное сообщение их не отменяет."""
    if not agent.get("tg_id") or bot is None:
        return False
    lang = await _lang(db, agent["tg_id"])
    text = i18n.t(lang, "CAB_REF_BONUS").format(
        name=bot_logic.esc(friend.get("full_name") or ""), amount=logic.money(amount))
    return await _send(bot, agent["tg_id"], text, kb.cabinet_entry(lang))


async def order_assigned(bot: Any, order: dict, tech: dict) -> bool:
    """Технику: на тебя назначен наряд. Язык русский: сотрудники местные,
    а языковые пакеты - для клиентов."""
    if not tech.get("tg_id") or bot is None:
        return False
    what = (f"Велосипед № {order.get('bike_code')} {order.get('bike_model') or ''}".strip()
            if order.get("bike_id") else (order.get("object_note") or "Объект не указан"))
    text = texts.STAFF_ORDER_ASSIGNED.format(
        no=order.get("no") or "", object=bot_logic.esc(what),
        complaint=bot_logic.esc(order.get("complaint") or "—"),
        status=logic.ORDER_STATUSES.get(order.get("status"), order.get("status") or ""))
    return await _send(bot, tech["tg_id"], text)


async def repair_ready(bot: Any, client: dict, order: dict, total: Any) -> bool:
    """Клиенту: его техника из наряда готова и сколько это стоило.

    Только по клиентским нарядам: свой парк чинится молча, там ждать
    нечего и некому.
    """
    if not client.get("tg_id") or bot is None:
        return False
    what = (f"Велосипед № {order.get('bike_code')}" if order.get("bike_id")
            else (order.get("object_note") or "Ваша техника"))
    text = texts.REPAIR_READY.format(
        no=order.get("no") or "", object=bot_logic.esc(what),
        total=logic.money(total))
    return await _send(bot, client["tg_id"], text)
