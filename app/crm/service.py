"""Операции CRM, общие для веб-панели и Telegram-бота.

Одна реализация на оба входа: зачисление платежа из панели и по кнопке
в служебном чате обязано вести к одинаковым записям в журнале. Здесь
только база; уведомления клиенту - в notify.py, чтобы веб-процесс без
токена бота мог работать так же, как бот.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
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
                      billing: str = "auto", mileage: int | None = None) -> int:
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
            created_by=by, mileage_start=mileage)
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
                       bike_status: str = "available", by: str | None = None,
                       mileage: int | None = None) -> None:
    if bike_status not in logic.BIKE_MANUAL_STATUSES:
        raise ServiceError("Недопустимый статус велосипеда.")
    if not await crm.close_rental(rental["id"], closed_on=closed_on, note=note,
                                  bike_status=bike_status, closed_by=by,
                                  mileage_end=mileage):
        raise ServiceError("Аренда уже закрыта.")


async def change_tariff(crm: Any, rental: dict, tariff: dict, *, billing: str) -> None:
    """Сменить тариф с ближайшего неначисленного периода."""
    if billing not in logic.BILLING:
        raise ServiceError("Недопустимый режим начисления.")
    await crm.update_rental(rental["id"], tariff_id=tariff.get("id"),
                            tariff_name=tariff["name"],
                            period_days=int(tariff["period_days"]),
                            price=logic.to_money(tariff["price"]), billing=billing)



# ─────────────────────────── сервис: наряды ───────────────────────────

async def open_order(crm: Any, *, bike: dict | None, payer: str,
                     client: dict | None, complaint: str | None,
                     object_note: str | None, tech_id: int | None,
                     estimate: Decimal, by: str) -> int:
    """Открыть наряд и увести велосипед в ремонт.

    Статус велосипеда меняется здесь же: наряд открыт, а велосипед числится
    свободным - это тот самый рассинхрон, из-за которого его выдают клиенту
    прямо из мастерской.
    """
    if payer not in logic.PAYERS:
        raise ServiceError("Неизвестный плательщик наряда.")
    if payer == "client" and client is None and not (object_note or "").strip():
        raise ServiceError("Клиентский ремонт: укажите клиента или что за объект.")
    if bike is None and not (object_note or "").strip():
        raise ServiceError("Укажите велосипед из парка или что за объект.")
    if bike is not None and await crm.open_order_of(bike["id"]) is not None:
        raise ServiceError("По этому велосипеду уже открыт наряд.")
    try:
        order_id = await crm.create_work_order(
            bike_id=bike["id"] if bike else None, payer=payer,
            client_id=client["id"] if client else None, complaint=complaint,
            object_note=object_note, tech_id=tech_id,
            estimate=logic.to_money(estimate), created_by=by)
    except Exception as exc:                            # noqa: BLE001
        # Уникальный индекс на открытый наряд:два оператора нажали разом.
        if "unique" in type(exc).__name__.lower():
            raise ServiceError("По этому велосипеду уже открыт наряд.") from exc
        raise
    # Свой велосипед уходит в ремонт; в аренде он остаётся у клиента -
    # снимать аренду наряд не вправе, это делает возврат.
    if bike is not None and bike.get("status") == "available":
        await crm.update_bike(bike["id"], status="repair", by=by)
    return order_id


async def close_order(crm: Any, order: dict, *, by: str,
                      bike_status: str = "available") -> dict:
    """Закрыть наряд: посчитать итоги, записать ремонт в журнал велосипеда
    и вернуть велосипед в парк.

    Ремонт своего парка по-прежнему пишется в bike_log и repair_items -
    отчёт «что ломается» собран по ним, и наряд его не подменяет, а
    наполняет. Деньги клиентского ремонта остаются на наряде и в crm.ledger
    не попадают: журнал - это аренда, и средний чек считается по нему.
    """
    if not logic.order_is_open(order):
        raise ServiceError("Наряд уже закрыт.")
    items = await crm.order_items(order["id"])
    totals = logic.order_totals(items)
    log_id = None
    # Пишем через тот же create_repair, что и ручной ремонт: шапка и позиции
    # одной транзакцией, отчёт «что ломается» собирается по ним и наряда
    # не знает вовсе.
    nodes = [{"node": i["node"],
              "parts_cost": logic.to_money(i.get("parts_cost") or 0) * int(i.get("qty") or 1),
              "labor_cost": logic.to_money(i.get("labor_cost") or 0) * int(i.get("qty") or 1),
              "note": i.get("title")}
             for i in items if i.get("node")]
    if order.get("bike_id") and nodes:
        log_id = await crm.create_repair(
            order["bike_id"], items=nodes,
            note="Наряд " + str(order.get("no") or ""), created_by=by)
    await crm.update_work_order(order["id"], status="done",
                                total=totals["total"], cost=totals["cost"],
                                closed_at=datetime.now(UTC), log_id=log_id)
    if order.get("bike_id"):
        bike = await crm.bike(order["bike_id"])
        # Из аренды велосипед наряд не забирает и не возвращает: там его
        # судьбу решает закрытие аренды.
        if bike and bike.get("status") in ("repair", "maintenance"):
            await crm.update_bike(order["bike_id"], status=bike_status, by=by)
    return totals
