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
from . import company, logic

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
        url=bot_logic.esc(company.support_url()))
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


async def promo_applied(bot: Any, db: Any, crm: Any, client: dict, promo: dict,
                        amount: Any, *, period_index: int = 0) -> bool:
    """Клиенту: акция сработала, баллы на балансе. Текст акции - свой
    из строки или из шаблона; деньги уже начислены, недоставленное
    сообщение их не отменяет."""
    if not client.get("tg_id") or bot is None:
        return False
    lang = await _lang(db, client["tg_id"])
    balance = await crm.client_balance(client["id"])
    # {name} - имя, как в рассылках: по фамилии звучит как повестка.
    own = logic.promo_text(promo, discount=amount, period_index=period_index,
                           name=logic.first_name(client.get("full_name")),
                           balance=balance)
    text = i18n.t(lang, "CAB_PROMO_APPLIED").format(
        title=bot_logic.esc(promo.get("title") or ""), amount=logic.money(amount),
        balance=logic.money(balance),
        text=("\n\n" + bot_logic.esc(own)) if own else "")
    return await _send(bot, client["tg_id"], text, kb.cabinet_entry(lang))


async def booking_cancelled(bot: Any, db: Any, client: dict, booking: dict,
                            note: str | None = None) -> bool:
    """Клиенту: оператор снял заявку на аренду, и почему."""
    if not client.get("tg_id") or bot is None:
        return False
    lang = await _lang(db, client["tg_id"])
    text = i18n.t(lang, "CAB_BOOK_REMOVED").format(
        line=bot_logic.esc(logic.booking_line(booking)),
        note=("\n" + bot_logic.esc(note)) if note else "",
        url=bot_logic.esc(company.support_url()))
    return await _send(bot, client["tg_id"], text, kb.cabinet_entry(lang))


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


async def sign_code(bot: Any, request: dict, code: str) -> bool:
    """Код подтверждения клиенту в Telegram.

    Не в боте - вернётся False, и оператор продиктует код голосом: без
    этого клиент без Telegram подписать ничего не смог бы.
    """
    if not request.get("tg_id") or bot is None:
        return False
    return await _send(bot, request["tg_id"],
                       texts.SIGN_CODE.format(code=code,
                                              minutes=logic.SIGN_CODE_MINUTES))


async def pay_link(bot: Any, db: Any, order: dict) -> bool:
    """Клиенту: ссылка на оплату счёта.

    Нет в боте или ссылки нет - False, и оператор передаёт её сам.
    Молча «отправлено» показывать нельзя: клиент стоит рядом и ждёт.
    """
    del db
    link = str(order.get("link") or "")
    if not link or not order.get("tg_id") or bot is None:
        return False
    text = texts.PAY_LINK.format(
        no=order.get("no") or "", amount=logic.money(order.get("amount")),
        purpose=bot_logic.esc(order.get("purpose")), link=bot_logic.esc(link))
    return await _send(bot, order["tg_id"], text)


async def maintenance_invite(bot: Any, rental: dict) -> bool:
    """Клиенту: пора на бесплатное ТО."""
    if not rental.get("tg_id") or bot is None:
        return False
    started = rental.get("started_on")
    days = (date.today() - started).days if started else 0
    bike = rental.get("bike_code")
    text = texts.SERVICE_INVITE.format(
        bike=f"Велосипед № {bot_logic.esc(bike)}" if bike else "Велосипед",
        days=days)
    return await _send(bot, rental["tg_id"], text)


async def review_ask(bot: Any, rental: dict,
                     links: list[dict] | None = None) -> bool:
    """Клиенту: просьба оставить отзыв.

    Площадок нет - просим написать менеджеру: кнопка в никуда хуже, чем
    её отсутствие, а повод сказать спасибо остаётся.
    """
    if not rental.get("tg_id") or bot is None:
        return False
    started = rental.get("started_on")
    days = (date.today() - started).days if started else 0
    sites = links or []
    tail = ("\n\n" + "\n".join(f"• {bot_logic.esc(s['title'])}: {bot_logic.esc(s['url'])}"
                                 for s in sites)
            if sites else texts.REVIEW_ASK_PLAIN)
    return await _send(bot, rental["tg_id"],
                       texts.REVIEW_ASK.format(days=days, links=tail))


async def autocharge_ok(bot: Any, client: dict, amount: Any,
                        card: dict | None, until: Any = None) -> bool:
    """Клиенту: с карты списали. Молча списывать нельзя - это выглядит
    как мошенничество, даже когда клиент сам согласился."""
    if not client.get("tg_id") or bot is None:
        return False
    text = texts.AUTOCHARGE_OK.format(
        mask=logic.card_mask((card or {}).get("mask")) or "----",
        amount=logic.money(amount),
        until=until.strftime("%d.%m.%Y") if until else "—")
    return await _send(bot, client["tg_id"], text)


async def autocharge_fail(bot: Any, client: dict, amount: Any,
                          card: dict | None, reason: str = "") -> bool:
    """Клиенту: списать не вышло. Причину даём словами банка, но без
    кодов: «insufficient_funds» курьеру ничего не объясняет."""
    if not client.get("tg_id") or bot is None:
        return False
    human = ("Скорее всего, на карте не хватило денег."
             if not reason else bot_logic.esc(reason)[:200])
    text = texts.AUTOCHARGE_FAIL.format(
        mask=logic.card_mask((card or {}).get("mask")) or "----",
        amount=logic.money(amount), reason=human)
    return await _send(bot, client["tg_id"], text)


async def estimate(bot: Any, client: dict, order: dict, items: list[dict],
                   total: Any) -> bool:
    """Клиенту: смета с кнопками «согласен» / «не надо».

    Нет в боте - False, и оператор согласует вживую: без этого клиент
    без Telegram не мог бы ответить вовсе.
    """
    if not client.get("tg_id") or bot is None:
        return False
    what = (f"Велосипед № {bot_logic.esc(order.get('bike_code'))}"
            if order.get("bike_id")
            else bot_logic.esc(order.get("object_note") or "Ваша техника"))
    text = texts.ESTIMATE.format(
        no=order.get("no") or "", object=what,
        lines=bot_logic.esc(logic.estimate_lines(items)),
        total=logic.money(total))
    return await _send(bot, client["tg_id"], text,
                       kb.estimate_answer(int(order["id"])))
