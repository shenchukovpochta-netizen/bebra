"""Операции CRM, общие для веб-панели и Telegram-бота.

Одна реализация на оба входа: зачисление платежа из панели и по кнопке
в служебном чате обязано вести к одинаковым записям в журнале. Здесь
только база; уведомления клиенту - в notify.py, чтобы веб-процесс без
токена бота мог работать так же, как бот.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from . import logic

log = logging.getLogger(__name__)


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
    # Шаг воронки приглашений. Учёт не вправе сорвать выдачу велосипеда,
    # поэтому ошибка здесь только в логе.
    try:
        await ref_rented(crm, client)
    except Exception:                                    # noqa: BLE001
        log.exception("реферальная программа: аренда клиента %s не отмечена",
                      client.get("id"))
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


async def start_stock_take(crm: Any, *, scope: str, location: str | None,
                           note: str | None, by: str) -> int:
    """Открыть ведомость пересчёта со снимком ожидаемого парка."""
    if scope not in logic.TAKE_SCOPES:
        raise ServiceError("Неизвестная область пересчёта.")
    if scope == "location" and not (location or "").strip():
        raise ServiceError("Пересчёт по точке: выберите точку.")
    if await crm.open_stock_take() is not None:
        raise ServiceError("Пересчёт уже идёт. Закройте его, прежде чем начинать новый.")
    bikes = await crm.bikes(limit=10000)
    expected = logic.expected_bikes(bikes, scope=scope, location=location)
    if not expected:
        raise ServiceError("Считать нечего: на этой точке нет техники, "
                           "которую ждут на месте.")
    try:
        return await crm.create_stock_take(
            scope=scope, location=location if scope == "location" else None,
            note=note, bike_ids=[int(b["id"]) for b in expected], created_by=by)
    except Exception as exc:                            # noqa: BLE001
        if "unique" in type(exc).__name__.lower():
            raise ServiceError("Пересчёт уже идёт.") from exc
        raise


async def take_add_found(crm: Any, take: dict, code: str) -> dict:
    """Отметить велосипед по номеру на раме: так считают с телефона в руках.

    Три исхода, и все три нужны: ждали и нашли, не ждали, но нашли
    (числится у клиента, на другой точке или вовсе потерян), и номера
    такого в парке нет.
    """
    bike = await crm.bike_by_code(code)
    if bike is None:
        item_id = await crm.add_take_item(take["id"], bike_id=None, code=code,
                                          state="extra", note="Нет такого в парке")
        return {"item_id": item_id, "state": "extra", "bike": None,
                "message": f"{code}: такого номера в парке нет. Записал лишним."}
    item = await crm.take_item_of_bike(take["id"], int(bike["id"]))
    if item is None:
        note = f"Числится: {logic.BIKE_STATUSES.get(bike.get('status'), '—')}"
        item_id = await crm.add_take_item(take["id"], bike_id=int(bike["id"]),
                                          code=bike["code"], state="extra", note=note)
        return {"item_id": item_id, "state": "extra", "bike": bike,
                "message": f"{bike['code']} не ждали здесь: {note.lower()}. "
                           "Записал лишним."}
    if item["state"] in ("found", "extra"):
        # Лишний остаётся лишним: в «ожидалось» его не ждали, и отметка
        # «на месте» надула бы «нашли столько-то из стольких-то».
        said = "уже отмечен" if item["state"] == "found" else "уже записан лишним"
        return {"item_id": int(item["id"]), "state": item["state"], "bike": bike,
                "message": f"{bike['code']} {said}."}
    await crm.set_take_item(take["id"], int(item["id"]), state="found")
    return {"item_id": int(item["id"]), "state": "found", "bike": bike,
            "message": f"{bike['code']} на месте."}


async def finish_stock_take(crm: Any, take: dict, *, by: str,
                            lose_missing: bool = False,
                            return_found: bool = True) -> dict:
    """Закрыть ведомость и применить её результат к парку.

    Ведомость сама по себе ничего не меняет: пропустить один велосипед
    глазами легко, а статус «Утерян» потом никто не снимет. Поэтому
    недостачу в потери переводят отдельной галочкой, а вот найденный
    потерянный возвращается в парк сразу - он физически стоит на точке.
    """
    if not logic.take_is_open(take):
        raise ServiceError("Пересчёт уже закрыт.")
    items = await crm.take_items(take["id"])
    counts = logic.take_counts(items)
    # Неотмеченное станет недостачей при закрытии - учитываем это заранее.
    counts = {**counts, "missing": counts["missing"] + counts["expected"], "expected": 0}
    missing_ids = await crm.close_stock_take(
        take["id"], counts=counts, closed_at=datetime.now(UTC))
    lost, returned = 0, 0
    if lose_missing:
        for bike_id in missing_ids:
            bike = await crm.bike(bike_id)
            # В аренде велосипед потеряться не может: он у клиента, и это
            # разговор с клиентом, а не отметка в ведомости.
            if bike and bike.get("status") in logic.TAKE_EXPECTED_STATUSES:
                await crm.update_bike(bike_id, status="lost", by=by)
                lost += 1
    if return_found:
        for item in items:
            if item.get("state") != "extra" or not item.get("bike_id"):
                continue
            bike = await crm.bike(int(item["bike_id"]))
            if bike and bike.get("status") == "lost":
                await crm.update_bike(int(bike["id"]), status="available", by=by)
                returned += 1
    return {**counts, "lost": lost, "returned": returned}


async def ref_code_of(crm: Any, client: dict) -> str:
    """Код приглашения клиента: выдаётся при первом показе в кабинете.

    Заранее коды не раздаются: у большинства клиентов этот экран никто
    не откроет, а занятые коды мешали бы подбирать короткие.
    """
    if client.get("ref_code"):
        return str(client["ref_code"])
    for _ in range(10):
        code = logic.make_ref_code()
        if await crm.client_by_ref_code(code) is not None:
            continue
        if await crm.set_ref_code(client["id"], code):
            client["ref_code"] = code
            return code
    raise ServiceError("Не удалось выдать код приглашения, попробуйте ещё раз.")


async def ref_click(crm: Any, code: str, tg_id: int) -> dict | None:
    """Переход по ссылке-приглашению. None - записывать нечего.

    Пустой ответ - это норма: код чужой, свой собственный, человек уже
    закреплён за другим агентом или уже наш клиент. Приглашать своих
    клиентов заново программа не даёт - иначе бонус платился бы за тех,
    кто и так катается.
    """
    code = logic.clean_ref_code(code)
    if not code:
        return None
    agent = await crm.client_by_ref_code(code)
    if agent is None or agent.get("status") != "active":
        return None
    if agent.get("tg_id") and int(agent["tg_id"]) == int(tg_id):
        return None
    if await crm.client_by_tg(tg_id) is not None:
        return None
    if await crm.referral_of_tg(tg_id) is not None:
        return None
    ref_id = await crm.add_referral(agent_id=agent["id"], tg_id=tg_id)
    if ref_id is None:
        return None
    return await crm.referral_of_tg(tg_id)


async def ref_signed(crm: Any, client: dict) -> dict | None:
    """Друг завёл карточку: связать её с переходом.

    Зовётся при появлении клиента с Telegram - из бота и из панели. Без
    этого воронка обрывалась бы на «перешёл», а бонус платить было бы
    не за кого.
    """
    tg_id = client.get("tg_id")
    if not tg_id:
        return None
    ref = await crm.referral_of_tg(int(tg_id))
    if ref is None or ref["agent_id"] == client["id"]:
        return None
    if ref.get("client_id") is None:
        await crm.update_referral(ref["id"], client_id=client["id"],
                                  status="signed", signed_at=datetime.now(UTC))
        patch: dict[str, Any] = {"invited_by": ref["agent_id"],
                                 "invited_at": datetime.now(UTC)}
        # Канал привлечения известен без вопросов: клиента привёл друг.
        if not client.get("channel"):
            patch["channel"] = "referral"
        await crm.update_client(client["id"], **patch)
    return await crm.referral_of_tg(int(tg_id))


async def ref_rented(crm: Any, client: dict) -> dict | None:
    """Друг взял велосипед. Шаг воронки, деньгами ещё не пахнет."""
    ref = await crm.referral_of_client(client["id"])
    if ref is None or logic.ref_status_at_least(ref["status"], "rented"):
        return ref
    await crm.update_referral(ref["id"], status="rented", rented_at=datetime.now(UTC))
    return await crm.referral_of_client(client["id"])


async def ref_paid(crm: Any, client: dict, amount: Decimal, *,
                   by: str = "referral") -> dict | None:
    """Друг заплатил: начислить бонус агенту. None - платить не за что.

    Бонус платится один раз и только с платежа не меньше порога: иначе
    хватило бы перевести сто рублей с собственной карты на карту знакомого
    и получить бонус.
    """
    ref = await crm.referral_of_client(client["id"])
    if ref is None or ref["status"] == "paid":
        return None
    settings = logic.ref_settings(await crm.settings())
    if not settings["enabled"] or settings["bonus"] <= 0:
        return None
    if logic.to_money(amount) < settings["min_payment"]:
        return None
    agent = await crm.client(ref["agent_id"])
    if agent is None or agent.get("status") != "active":
        return None
    note = f"Бонус за друга: {client.get('full_name') or client['id']}"
    ledger_id = await crm.pay_referral_bonus(
        ref["id"], agent_id=agent["id"], amount=settings["bonus"], note=note,
        created_by=by)
    if ledger_id is None:
        return None
    return {**(await crm.referral_of_client(client["id"]) or {}),
            "agent": agent, "bonus": settings["bonus"]}
