"""Операции CRM, общие для веб-панели и Telegram-бота.

Одна реализация на оба входа: зачисление платежа из панели и по кнопке
в служебном чате обязано вести к одинаковым записям в журнале. Здесь
только база; уведомления клиенту - в notify.py, чтобы веб-процесс без
токена бота мог работать так же, как бот.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from . import logic


class ServiceError(Exception):
    """Понятное человеку сообщение об отказе: показывается как есть."""


async def credit_claim(crm: Any, claim: dict, amount: Decimal, *, by: str,
                       method: str = "sbp") -> int | None:
    """Зачислить заявку клиента. None - её уже закрыл кто-то другой.

    Закрытие заявки и платёж - одна транзакция в базе: второй оператор
    (двойной тап, панель и Telegram одновременно) не пишет в журнал
    ничего, и сумма платежей в отчётах не раздувается.
    """
    return await crm.credit_claim(
        claim["id"], client_id=claim["client_id"], amount=abs(amount), method=method,
        note=f"Пополнение по заявке #{claim['id']}", created_by=by)


async def reject_claim(crm: Any, claim: dict, *, by: str) -> bool:
    return await crm.resolve_claim(claim["id"], status="rejected", resolved_by=by)


async def add_entry(crm: Any, client: dict, *, kind: str, amount: Decimal,
                    method: str | None, note: str | None, by: str,
                    rental_id: int | None = None) -> int:
    """Ручная запись в журнал из панели: знак ставится по виду записи."""
    if kind not in logic.KINDS or kind == "charge":
        raise ServiceError("Начисления делает биллинг; для ручной суммы "
                           "используйте корректировку или штраф.")
    return await crm.add_ledger(
        client_id=client["id"], rental_id=rental_id, kind=kind,
        amount=logic.signed_amount(kind, amount), method=method, note=note,
        created_by=by)


async def open_rental(crm: Any, *, client: dict, bike: dict | None, tariff: dict,
                      started_on: date, contract_no: str | None, by: str,
                      billing: str = "auto") -> int:
    """Оформить аренду и начислить первый период.

    Аренда с датой начала в будущем не начисляется заранее: первый период
    спишет дневной проход в свой день - иначе клиент видел бы долг за
    велосипед, которого ещё не получил.
    """
    if client.get("status") != "active":
        raise ServiceError("Клиент заблокирован или в чёрном списке.")
    if bike is not None and bike.get("status") != "available":
        raise ServiceError(
            f"Велосипед {bike.get('code')} сейчас "
            f"«{logic.BIKE_STATUSES.get(bike.get('status'), bike.get('status'))}».")
    if await crm.active_rental_of(client["id"]) is not None:
        raise ServiceError("У клиента уже идёт аренда - сначала закройте её.")
    try:
        rental_id = await crm.create_rental(
            client_id=client["id"], bike_id=bike["id"] if bike else None,
            tariff_id=tariff.get("id"), tariff_name=tariff["name"],
            period_days=int(tariff["period_days"]), price=logic.to_money(tariff["price"]),
            billing=billing, started_on=started_on, contract_no=contract_no,
            created_by=by)
    except Exception as exc:                            # noqa: BLE001
        # Уникальные индексы на активную аренду: гонка двух операторов.
        if "unique" in type(exc).__name__.lower():
            raise ServiceError("Аренда уже оформлена другим оператором.") from exc
        raise
    if billing == "auto":
        await charge_due(crm, rental={"id": rental_id, "client_id": client["id"],
                                      "billed_until": started_on,
                                      "period_days": int(tariff["period_days"]),
                                      "price": logic.to_money(tariff["price"]),
                                      "tariff_name": tariff["name"],
                                      "billing": "auto", "status": "active"},
                         today=date.today())
    return rental_id


async def charge_due(crm: Any, *, rental: dict, today: date) -> int:
    """Начислить аренде все периоды по сегодня. Возвращает число начислений."""
    if rental.get("billing") != "auto" or rental.get("status") != "active":
        return 0
    done = 0
    price = logic.to_money(rental["price"])
    for period_from, period_to in logic.due_periods(rental["billed_until"],
                                                    int(rental["period_days"]),
                                                    today=today):
        ok = await crm.charge_period(
            rental["id"], rental["client_id"], period_from=period_from,
            period_to=period_to, amount=-price,
            note=f"{rental.get('tariff_name') or 'Аренда'}: "
                 f"{logic.period_label(period_from, period_to)}")
        if ok:
            done += 1
    return done


async def charge_all(crm: Any, *, today: date) -> int:
    total = 0
    for rental in await crm.active_rentals():
        total += await charge_due(crm, rental=rental, today=today)
    return total


async def close_rental(crm: Any, rental: dict, *, closed_on: date, note: str | None,
                       bike_status: str = "available", by: str | None = None) -> None:
    if bike_status not in logic.BIKE_MANUAL_STATUSES:
        raise ServiceError("Недопустимый статус велосипеда.")
    if not await crm.close_rental(rental["id"], closed_on=closed_on, note=note,
                                  bike_status=bike_status, closed_by=by):
        raise ServiceError("Аренда уже закрыта.")


async def change_tariff(crm: Any, rental: dict, tariff: dict, *, billing: str) -> None:
    """Сменить тариф с ближайшего неначисленного периода."""
    if billing not in logic.BILLING:
        raise ServiceError("Недопустимый режим начисления.")
    await crm.update_rental(rental["id"], tariff_id=tariff.get("id"),
                            tariff_name=tariff["name"],
                            period_days=int(tariff["period_days"]),
                            price=logic.to_money(tariff["price"]), billing=billing)

