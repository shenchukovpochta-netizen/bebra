"""Операции CRM, общие для веб-панели и Telegram-бота.

Одна реализация на оба входа: зачисление платежа из панели и по кнопке
в служебном чате обязано вести к одинаковым записям в журнале. Здесь
только база; уведомления клиенту - в notify.py, чтобы веб-процесс без
токена бота мог работать так же, как бот.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from . import esign, logic, notices, notify

log = logging.getLogger(__name__)


class ServiceError(Exception):
    """Понятное человеку сообщение об отказе: показывается как есть."""


async def cash_shift_id(crm: Any, method: str | None, by: str | None) -> int | None:
    """Смена, в которую лягут эти наличные. Безнал в ящик не попадает.

    Точек две, и смены на них открыты одновременно: по одному окну
    времени наличный платёж попадал в обе смены сразу, и на второй
    точке закрытие писало недостачу на ту же сумму как факт. Смену
    выбираем по тому, кто принял деньги, - и запоминаем в записи.
    """
    if method != "cash":
        return None
    shift = await crm.cash_shift_for(by)
    return int(shift["id"]) if shift else None


async def credit_claim(crm: Any, claim: dict, amount: Decimal, *, by: str,
                       method: str = "sbp") -> int | None:
    """Зачислить заявку клиента. None - её уже закрыл кто-то другой.

    Закрытие заявки и платёж - одна транзакция в базе: второй оператор
    (двойной тап, панель и Telegram одновременно) не пишет в журнал
    ничего, и сумма платежей в отчётах не раздувается.
    """
    return await crm.credit_claim(
        claim["id"], client_id=claim["client_id"], amount=abs(amount), method=method,
        note=f"Пополнение по заявке #{claim['id']}", created_by=by,
        shift_id=await cash_shift_id(crm, method, by))


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
        created_by=by, shift_id=await cash_shift_id(crm, method, by))


async def open_rental(crm: Any, *, client: dict, bike: dict | None, tariff: dict,
                      started_on: date, contract_no: str | None, by: str,
                      billing: str = "auto", mileage: int | None = None,
                      extras: Sequence[Mapping[str, Any]] = (),
                      promo_code: str | None = None,
                      applied: list[dict] | None = None) -> int:
    """Оформить аренду и начислить первый период.

    Аренда с датой начала в будущем не начисляется заранее: первый период
    спишет дневной проход в свой день - иначе клиент видел бы долг за
    велосипед, которого ещё не получил.

    `extras` - платные позиции сверх велосипеда (доп. аккумулятор). Они
    заводятся до первого начисления, потому что начисляется цена периода
    целиком: завести их после значило бы подарить клиенту первый период
    второй батареи.

    `promo_code` ложится на аренду: у выдачи с датой в будущем первый
    период начислит дневной проход, и код обязан дожить до него.
    Сработавшие акции собираются в `applied` - для уведомления клиенту.
    """
    if client.get("status") != "active":
        raise ServiceError("Клиент заблокирован или в чёрном списке.")
    if bike is not None and bike.get("status") != "available":
        raise ServiceError(
            f"Велосипед {bike.get('code')} сейчас "
            f"«{logic.BIKE_STATUSES.get(bike.get('status'), bike.get('status'))}».")
    if await crm.active_rental_of(client["id"]) is not None:
        raise ServiceError("У клиента уже идёт аренда - сначала закройте её.")
    base = logic.to_money(tariff["price"])
    price = logic.period_price(base, extras)
    try:
        rental_id = await crm.create_rental(
            client_id=client["id"], bike_id=bike["id"] if bike else None,
            tariff_id=tariff.get("id"), tariff_name=tariff["name"],
            period_days=int(tariff["period_days"]), price=price, base_price=base,
            billing=billing, started_on=started_on, contract_no=contract_no,
            created_by=by, mileage_start=mileage,
            promo_code=logic.clean_promo_code(promo_code) or None)
    except Exception as exc:                            # noqa: BLE001
        # Уникальные индексы на активную аренду: гонка двух операторов.
        if "unique" in type(exc).__name__.lower():
            raise ServiceError("Аренда уже оформлена другим оператором.") from exc
        raise
    # Журнал перемещений: с этой строки начинается история того, что
    # у клиента на руках. Без неё замена не знала бы, что снимать.
    if bike is not None:
        await crm.add_rental_bike(rental_id, bike_id=bike["id"], issued_on=started_on,
                                  mileage_start=mileage, reason="Выдача", created_by=by)
    for extra in extras:
        await crm.add_rental_extra(
            rental_id, kind=str(extra.get("kind") or "battery"),
            title=str(extra["title"]), price=logic.to_money(extra.get("price")),
            battery_id=extra.get("battery_id"), by=by)
    if billing == "auto":
        await charge_due(crm, rental={"id": rental_id, "client_id": client["id"],
                                      "billed_until": started_on,
                                      "period_days": int(tariff["period_days"]),
                                      "price": price,
                                      "tariff_name": tariff["name"],
                                      "billing": "auto", "status": "active",
                                      "promo_code": logic.clean_promo_code(promo_code)},
                         today=date.today(), applied=applied)
    # Шаг воронки приглашений. Учёт не вправе сорвать выдачу велосипеда,
    # поэтому ошибка здесь только в логе.
    try:
        await ref_rented(crm, client)
    except Exception:                                    # noqa: BLE001
        log.exception("реферальная программа: аренда клиента %s не отмечена",
                      client.get("id"))
    return rental_id


async def charge_due(crm: Any, *, rental: dict, today: date,
                     applied: list[dict] | None = None) -> int:
    """Начислить аренде все периоды по сегодня. Возвращает число начислений.

    После каждого начисления - проверка акций: скидка на период ложится
    баллами следом за самим начислением. `applied` собирает сработавшие
    акции для уведомлений: у сервиса бота нет, шлёт вызывающий.
    """
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
        if not ok:
            continue
        done += 1
        # Акция не вправе сорвать начисление: сбой здесь - только в логе.
        try:
            got = await apply_promo(crm, rental=rental, period_from=period_from,
                                    price=price, today=today)
        except Exception:                                # noqa: BLE001
            log.exception("акции: период %s аренды %s не проверен",
                          period_from, rental.get("id"))
            got = None
        if got is not None and applied is not None:
            applied.append(got)
    return done


async def charge_all(crm: Any, *, today: date,
                     applied: list[dict] | None = None) -> int:
    total = 0
    for rental in await crm.active_rentals():
        total += await charge_due(crm, rental=rental, today=today, applied=applied)
    return total


async def apply_promo(crm: Any, *, rental: dict, period_from: date, price: Decimal,
                      today: date, by: str = "promo") -> dict | None:
    """Скидка по акции на только что начисленный период. None - не подошла.

    Одна на период, выгоднейшая для клиента. Баллы, а не платёж: они
    меняют баланс, но средний чек не трогают. Повтор того же периода
    упирается в уникальный индекс и возвращает None: второй проход
    начислений скидку не удваивает.
    """
    promos = await crm.promos(active_only=True)
    if not promos:
        return None
    history = logic.rental_history(await crm.client_rentals(rental["client_id"]),
                                   rental["id"])
    ctx = {
        "period_index": await crm.rental_charge_count(rental["id"]),
        "today": today, "code": rental.get("promo_code"),
        "client_uses": await crm.promo_client_uses(rental["client_id"]),
        **history,
    }
    picked = logic.pick_promo(promos, ctx, price)
    if picked is None:
        return None
    promo, discount = picked
    try:
        bonus_id = await crm.grant_bonus(
            client_id=rental["client_id"], kind="promo", amount=discount,
            note=f"Акция «{promo['title']}»: {logic.period_label(period_from, None)}",
            by=by, promo_id=promo["id"], rental_id=rental["id"],
            period_from=period_from)
    except Exception as exc:                            # noqa: BLE001
        if "unique" in type(exc).__name__.lower():
            return None
        raise
    if bonus_id is None:
        return None
    return {"client_id": rental["client_id"], "rental_id": rental["id"],
            "promo": promo, "amount": discount, "period_index": ctx["period_index"],
            "bonus_id": bonus_id}


async def check_promo_code(crm: Any, code: Any, *, today: date) -> dict | None:
    """Промокод с выдачи: действующая акция или ServiceError с причиной.
    Пустой код - None, выдача без акции."""
    code = logic.clean_promo_code(code)
    if not code:
        return None
    promo = await crm.promo_by_code(code)
    if promo is None:
        raise ServiceError(f"Промокод {code} не найден или выключен.")
    if not logic.promo_alive(promo, today=today):
        raise ServiceError(f"Промокод {code} уже не действует: вышел срок "
                           "или выбран предел применений.")
    return promo


async def close_rental(crm: Any, rental: dict, *, closed_on: date, note: str | None,
                       bike_status: str = "available", by: str | None = None,
                       mileage: int | None = None,
                       battery_status: str = "available") -> None:
    if bike_status not in logic.BIKE_MANUAL_STATUSES:
        raise ServiceError("Недопустимый статус велосипеда.")
    if not await crm.close_rental(rental["id"], closed_on=closed_on, note=note,
                                  bike_status=bike_status, closed_by=by,
                                  mileage_end=mileage):
        raise ServiceError("Аренда уже закрыта.")
    # Батареи возвращаются вместе с велосипедом: оставить их «у клиента»
    # значит потерять две штуки на каждой закрытой аренде.
    try:
        await crm.return_batteries(rental["id"], status=battery_status, by=by or "")
    except Exception:                                    # noqa: BLE001
        log.exception("батареи аренды %s не приняты обратно", rental["id"])


async def change_tariff(crm: Any, rental: dict, tariff: dict, *, billing: str) -> None:
    """Сменить тариф с ближайшего неначисленного периода.

    Цена периода - это велосипед плюс живые позиции (доп. аккумулятор), и
    смена тарифа обязана пересобрать её так же, как выдача. Раньше сюда
    писалась голая цена тарифа: доп. аккумулятор переставал начисляться,
    а снятие позиции возвращало аренду к цене СТАРОГО тарифа, потому что
    `base_price` оставался прежним.
    """
    if billing not in logic.BILLING:
        raise ServiceError("Недопустимый режим начисления.")
    base = logic.to_money(tariff["price"])
    extras = await crm.rental_extras(rental["id"], live_only=True)
    await crm.update_rental(rental["id"], tariff_id=tariff.get("id"),
                            tariff_name=tariff["name"],
                            period_days=int(tariff["period_days"]),
                            price=logic.period_price(base, extras),
                            base_price=base, billing=billing)



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
    # Пишем ту же пару bike_log + repair_items, что и ручной ремонт: отчёт
    # «что ломается» собран по ним и о нарядах не знает вовсе. Шапка нужна
    # и тогда, когда узел не выбран ни в одной строке: иначе себестоимость
    # ремонта пропадала из месячного отчёта и из окупаемости по моделям.
    nodes = [{"node": i["node"],
              "parts_cost": logic.to_money(i.get("parts_cost") or 0) * logic.item_qty(i),
              "labor_cost": logic.to_money(i.get("labor_cost") or 0) * logic.item_qty(i),
              "note": i.get("title")}
             for i in items if i.get("node")]
    repair = None
    if order.get("bike_id") and (nodes or totals["cost"] > 0):
        repair = {"bike_id": order["bike_id"], "items": nodes,
                  "cost": totals["cost"],
                  "note": "Наряд " + str(order.get("no") or ""), "created_by": by}
    # Закрытие - заявкой: два нажатия «Закрыть наряд» писали в журнал
    # велосипеда два ремонта с одинаковыми позициями.
    done = await crm.close_work_order(
        order["id"], total=totals["total"], cost=totals["cost"],
        closed_at=datetime.now(UTC), repair=repair)
    if done is None:
        raise ServiceError("Наряд уже закрыт.")
    if order.get("bike_id"):
        bike = await crm.bike(order["bike_id"])
        # Из аренды велосипед наряд не забирает и не возвращает: там его
        # судьбу решает закрытие аренды.
        if bike and bike.get("status") in ("repair", "maintenance"):
            await crm.update_bike(order["bike_id"], status=bike_status, by=by)
    return totals


async def start_stock_take(crm: Any, *, scope: str, location: str | None,
                           note: str | None, by: str, what: str = "bikes") -> int:
    """Открыть ведомость пересчёта со снимком ожидаемого парка."""
    if scope not in logic.TAKE_SCOPES:
        raise ServiceError("Неизвестная область пересчёта.")
    if what not in logic.TAKE_WHAT:
        raise ServiceError("Неизвестно, что считаем.")
    if scope == "location" and not (location or "").strip():
        raise ServiceError("Пересчёт по точке: выберите точку.")
    if await crm.open_stock_take() is not None:
        raise ServiceError("Пересчёт уже идёт. Закройте его, прежде чем начинать новый.")
    bikes: list[dict] = []
    cells: list[dict] = []
    if what in ("all", "bikes"):
        bikes = logic.expected_bikes(await crm.bikes(limit=10000),
                                     scope=scope, location=location)
    if what in ("all", "batteries"):
        cells = logic.expected_batteries(await crm.batteries(limit=10000),
                                         scope=scope, location=location)
    if not bikes and not cells:
        raise ServiceError("Считать нечего: на этой точке нет техники, "
                           "которую ждут на месте.")
    try:
        return await crm.create_stock_take(
            scope=scope, location=location if scope == "location" else None,
            note=note, what=what, bike_ids=[int(b["id"]) for b in bikes],
            battery_ids=[int(b["id"]) for b in cells], created_by=by)
    except Exception as exc:                            # noqa: BLE001
        if "unique" in type(exc).__name__.lower():
            raise ServiceError("Пересчёт уже идёт.") from exc
        raise


async def take_add_found(crm: Any, take: dict, code: str) -> dict:
    """Отметить технику по номеру: так считают с телефона в руках.

    Номер может оказаться и велосипедным, и батарейным - человек обходит
    точку один раз и вводит подряд то, что видит. Что из них чей номер,
    разбирается здесь, а не в голове у считающего.

    Три исхода, и все три нужны: ждали и нашли, не ждали, но нашли
    (числится у клиента, на другой точке или вовсе потерян), и номера
    такого в парке нет.
    """
    what = str(take.get("what") or "bikes")
    bike = await crm.bike_by_code(code) if what in ("all", "bikes") else None
    battery = None
    if bike is None and what in ("all", "batteries"):
        battery = await crm.battery_by_code(code)
    if bike is None and battery is None:
        item_id = await crm.add_take_item(take["id"], bike_id=None, code=code,
                                          state="extra", note="Нет такого в парке")
        return {"item_id": item_id, "state": "extra", "bike": None,
                "message": f"{code}: такого номера в парке нет. Записал лишним."}
    if battery is not None:
        return await _take_mark(crm, take, battery, kind="battery")
    return await _take_mark(crm, take, bike, kind="bike")


async def _take_mark(crm: Any, take: dict, thing: dict, *, kind: str) -> dict:
    """Общая отметка строки ведомости для велосипеда и для батареи."""
    if kind == "battery":
        item = await crm.take_item_of_battery(take["id"], int(thing["id"]))
        titles, ids = logic.BATTERY_STATUSES, {"battery_id": int(thing["id"]),
                                               "bike_id": None}
    else:
        item = await crm.take_item_of_bike(take["id"], int(thing["id"]))
        titles, ids = logic.BIKE_STATUSES, {"bike_id": int(thing["id"]),
                                            "battery_id": None}
    if item is None:
        note = f"Числится: {titles.get(thing.get('status'), '—')}"
        item_id = await crm.add_take_item(take["id"], code=thing["code"],
                                          state="extra", note=note, **ids)
        return {"item_id": item_id, "state": "extra", "bike": thing, "kind": kind,
                "message": f"{thing['code']} не ждали здесь: {note.lower()}. "
                           "Записал лишним."}
    if item["state"] in ("found", "extra"):
        # Лишний остаётся лишним: в «ожидалось» его не ждали, и отметка
        # «на месте» надула бы «нашли столько-то из стольких-то».
        said = "уже отмечен" if item["state"] == "found" else "уже записан лишним"
        return {"item_id": int(item["id"]), "state": item["state"], "bike": thing,
                "kind": kind, "message": f"{thing['code']} {said}."}
    await crm.set_take_item(take["id"], int(item["id"]), state="found")
    return {"item_id": int(item["id"]), "state": "found", "bike": thing,
            "kind": kind, "message": f"{thing['code']} на месте."}


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
    missing = await crm.close_stock_take(
        take["id"], counts=counts, closed_at=datetime.now(UTC))
    lost, returned = 0, 0
    if lose_missing:
        for bike_id in missing["bikes"]:
            bike = await crm.bike(bike_id)
            # В аренде велосипед потеряться не может: он у клиента, и это
            # разговор с клиентом, а не отметка в ведомости.
            if bike and bike.get("status") in logic.TAKE_EXPECTED_STATUSES:
                await crm.update_bike(bike_id, status="lost", by=by)
                lost += 1
        for battery_id in missing["batteries"]:
            battery = await crm.battery(battery_id)
            if battery and battery.get("status") in \
                    logic.TAKE_EXPECTED_BATTERY_STATUSES:
                await crm.update_battery(battery_id, status="lost", by=by)
                lost += 1
    if return_found:
        for item in items:
            if item.get("state") != "extra":
                continue
            if item.get("bike_id"):
                bike = await crm.bike(int(item["bike_id"]))
                if bike and bike.get("status") == "lost":
                    await crm.update_bike(int(bike["id"]), status="available", by=by)
                    returned += 1
            elif item.get("battery_id"):
                battery = await crm.battery(int(item["battery_id"]))
                if battery and battery.get("status") == "lost":
                    await crm.update_battery(int(battery["id"]), status="available",
                                             by=by)
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
    settings = logic.bonus_settings(await crm.settings())
    if not settings["enabled"] or settings["bonus"] <= 0:
        return None
    if logic.to_money(amount) < settings["min_payment"]:
        return None
    if settings["new_only"] and not logic.is_new_friend(client, ref):
        # «Приведи друга» - про новых людей. Иначе это «перезаведи
        # соседа»: карточка старого клиента заведена раньше перехода.
        log.info("бонус за клиента %s не начислен: он был в базе раньше "
                 "перехода по ссылке", client.get("id"))
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
    # Повод рядом с записью: «за что начислили» журнал не хранит.
    try:
        await crm.record_bonus(client_id=agent["id"], kind="referral",
                               amount=settings["bonus"], ledger_id=ledger_id,
                               ref_id=ref["id"], note=note, by=by)
    except Exception:                                    # noqa: BLE001
        log.warning("повод бонуса агенту %s не записан", agent["id"],
                    exc_info=True)
    friend_bonus = settings["friend_bonus"]
    if friend_bonus > 0:
        # Другу - тоже, и один раз: частичный уникальный индекс не даст
        # начислить второй, сколько бы раз ни звали.
        try:
            await crm.grant_bonus(
                client_id=client["id"], kind="friend", amount=friend_bonus,
                ref_id=ref["id"], by=by,
                note=f"Бонус по приглашению от {agent.get('full_name') or agent['id']}")
        except Exception:                                # noqa: BLE001
            log.info("бонус другу %s уже начислялся", client.get("id"))
    return {**(await crm.referral_of_client(client["id"]) or {}),
            "agent": agent, "bonus": settings["bonus"],
            "friend_bonus": friend_bonus}


async def grant_review_bonus(crm: Any, client: dict, *, by: str,
                             note: str | None = None) -> Decimal | None:
    """Бонус за опубликованный отзыв. Начисляет человек, посмотрев скриншот.

    Автоматически проверить, что отзыв написан и опубликован, нечем:
    у площадок нет ни API, ни обязанности нам отвечать. Поэтому кнопка
    у оператора, а не правило в коде - и один раз на клиента.
    """
    settings = logic.bonus_settings(await crm.settings())
    amount = settings["review_bonus"]
    if amount <= 0:
        raise ServiceError("Бонус за отзыв не задан: поставьте сумму "
                           "в настройках отзывов.")
    if await crm.bonus_of(client["id"], "review") is not None:
        raise ServiceError("Бонус за отзыв этому клиенту уже начисляли.")
    got = await crm.grant_bonus(client_id=client["id"], kind="review",
                                amount=amount, by=by,
                                note=note or "Бонус за опубликованный отзыв")
    return amount if got is not None else None


async def grant_manual_bonus(crm: Any, client: dict, amount: Decimal, *,
                             note: str, by: str) -> Decimal:
    """Баллы руками: акция, извинение, договорённость.

    Это не платёж: баллы меняют баланс, но в средний чек не идут -
    иначе одно из трёх чисел парка начало бы врать.
    """
    amount = logic.to_money(amount)
    if amount <= 0:
        raise ServiceError("Сумма баллов должна быть больше нуля.")
    await crm.grant_bonus(client_id=client["id"], kind="manual", amount=amount,
                          note=note or "Начислено руками", by=by)
    return amount


async def receive_parts(crm: Any, *, supplier_id: int | None, lines: list[dict],
                        note: str | None, by: str) -> int:
    """Приход на склад. Пересчёт средней себестоимости - внутри, в базе."""
    clean = [line for line in lines if int(line.get("qty") or 0) > 0]
    if not clean:
        raise ServiceError("Приход пуст: укажите хотя бы одну позицию с количеством.")
    return await crm.create_part_doc(kind="receipt", supplier_id=supplier_id,
                                     lines=clean, note=note, created_by=by)


async def write_off_parts(crm: Any, *, lines: list[dict], note: str | None,
                          by: str) -> int:
    """Списание со склада: брак, потеря, износ. Причина обязательна -
    списание без причины через месяц никто не объяснит."""
    clean = [line for line in lines if int(line.get("qty") or 0) > 0]
    if not clean:
        raise ServiceError("Списание пусто: укажите позицию и количество.")
    if not (note or "").strip():
        raise ServiceError("Укажите причину списания.")
    for line in clean:
        part = await crm.part(int(line["part_id"]))
        if part is None:
            raise ServiceError("Такой позиции на складе нет.")
        stock = await crm.part_stock(part["id"])
        if int(line["qty"]) > stock:
            raise ServiceError(
                f"«{part['title']}»: на складе {stock}, списать больше нельзя.")
        line["cost"] = part["cost"]
    return await crm.create_part_doc(kind="write_off", supplier_id=None, lines=clean,
                                     note=note, created_by=by)


async def issue_part_to_order(crm: Any, order: dict, part: dict, qty: int, *,
                              by: str) -> dict:
    """Списать запчасть со склада в наряд и вернуть строку наряда.

    Ради этого склад и заводился: до него себестоимость ремонта писали
    руками, и она ничего не значила. Здесь она берётся со склада, а остаток
    на полке уменьшается тем же действием.
    """
    qty = int(qty)
    if qty <= 0:
        raise ServiceError("Количество: должно быть больше нуля.")
    if not logic.order_is_open(order):
        raise ServiceError("Наряд закрыт - списывать в него нечего.")
    stock = await crm.part_stock(part["id"])
    if qty > stock:
        raise ServiceError(f"«{part['title']}»: на складе {stock}. "
                           "Закажите запчасть или спишите меньше.")
    cost = logic.to_money(part.get("cost") or 0)
    move_id = await crm.add_part_move(
        part_id=part["id"], kind="order", qty=-qty, cost=cost,
        order_id=order["id"], created_by=by,
        note=f"Наряд {order.get('no') or ''}".strip())
    item_id = await crm.add_order_item(
        order["id"], title=part["title"], node=part.get("node"), work_type_id=None,
        qty=qty, price=logic.to_money(part.get("price") or 0), parts_cost=cost,
        labor_cost=Decimal(0), note="Со склада", move_id=move_id)
    return {"item_id": item_id, "cost": cost, "qty": qty,
            "stock_left": stock - qty}


async def count_part(crm: Any, part: dict, fact: int, *, by: str,
                     note: str | None = None) -> dict:
    """Пересчёт позиции: привести остаток к факту на полке.

    Пишется движением, а не правкой остатка: расхождение - это документ
    с датой и автором, а не тихое исправление числа.
    """
    fact = int(fact)
    if fact < 0:
        raise ServiceError("Факт: не может быть отрицательным.")
    stock = await crm.part_stock(part["id"])
    delta = fact - stock
    if delta == 0:
        return {"delta": 0, "stock": stock}
    await crm.add_part_move(
        part_id=part["id"], kind="count", qty=delta,
        cost=logic.to_money(part.get("cost") or 0), created_by=by,
        note=note or ("Излишек по пересчёту" if delta > 0 else "Недостача по пересчёту"))
    return {"delta": delta, "stock": fact}


async def collect_part_needs(crm: Any, *, by: str) -> dict:
    """Собрать потребности склада в заказ: нехватка и наряды, ждущие запчасть.

    Заказ один и собирается дополнением: нажали второй раз - добавились
    только новые строки, уже внесённые руками не задваиваются.
    """
    rows = logic.part_rows(await crm.parts(active_only=True), await crm.stock_map())
    needs = logic.part_needs(rows, await crm.waiting_orders_parts())
    order = await crm.open_part_order()
    if order is None:
        order_id = await crm.create_part_order(supplier_id=None, note=None, created_by=by)
        order = await crm.part_order(order_id)
    added = 0
    by_id = {int(r["id"]): r for r in rows}
    for need in needs:
        part_id = need.get("part_id")
        if not part_id:
            continue
        part = by_id.get(int(part_id))
        price = logic.to_money((part or {}).get("cost") or 0)
        if await crm.add_part_order_item(
                order["id"], part_id=int(part_id), qty=max(int(need["qty"]), 1),
                price=price, source=need["source"],
                work_order_id=need.get("work_order_id")) is not None:
            added += 1
    return {"order": await crm.part_order(order["id"]), "added": added,
            "needs": len(needs)}


async def receive_part_order(crm: Any, order: dict, *, by: str) -> int:
    """Приёмка заказа: строки становятся приходом на склад."""
    if order["status"] == "received":
        raise ServiceError("Заказ уже принят.")
    if order["status"] == "cancelled":
        raise ServiceError("Заказ отменён.")
    items = await crm.part_order_items(order["id"])
    if not items:
        raise ServiceError("В заказе нет строк.")
    # Сперва занять заказ, потом приходовать: двойной клик по «Принять»
    # делал два прихода ПРХ и удваивал остаток на полке.
    if not await crm.claim_part_order(order["id"],
                                      total=logic.order_total(items),
                                      closed_at=datetime.now(UTC)):
        raise ServiceError("Заказ уже принят.")
    try:
        doc_id = await receive_parts(
            crm, supplier_id=order.get("supplier_id"),
            lines=[{"part_id": i["part_id"], "qty": i["qty"], "price": i["price"]}
                   for i in items],
            note=f"Заказ {order.get('no') or ''}".strip(), by=by)
    except Exception:
        # Приход не получился - заказ снова в работе, иначе он остался бы
        # «принят» без единого движения на складе.
        await crm.release_part_order(order["id"], status=order["status"])
        raise
    await crm.update_part_order(order["id"], doc_id=doc_id)
    return doc_id


async def orders_waiting_for(crm: Any, items: Iterable[dict]) -> list[dict]:
    """Наряды, которые стояли в «ждёт запчасть» ради этой поставки.

    Строка заказа помнит наряд, из-за которого её заказали
    (`part_order_items.work_order_id`), поэтому по приёмке сразу видно,
    кому идти. Без этого техник узнаёт о приходе, только когда сам
    заглянет на склад.
    """
    seen, out = set(), []
    for item in items:
        order_id = item.get("work_order_id")
        if not order_id or order_id in seen:
            continue
        seen.add(order_id)
        order = await crm.work_order(int(order_id))
        if order is not None and order.get("status") == "waiting":
            out.append(order)
    return out


async def swap_bike(crm: Any, rental: dict, new_bike: dict, *, reason: str,
                    mileage_old: int | None = None, mileage_new: int | None = None,
                    old_status: str | None = None, by: str) -> dict:
    """Заменить велосипед внутри аренды.

    Деньги, даты и договор остаются те же - меняется только то, что у
    клиента на руках. До замены приходилось закрывать аренду и открывать
    новую, и тогда расходились и оплаченный период, и номер договора.
    """
    if rental.get("status") != "active":
        raise ServiceError("Аренда закрыта - менять в ней нечего.")
    if reason not in logic.SWAP_REASONS:
        raise ServiceError("Укажите причину замены.")
    old_id = rental.get("bike_id")
    # Сначала «тот же велосипед»: он и правда «в аренде», но говорить об
    # этом оператору, который просто не сменил выбор, бесполезно.
    if old_id and int(old_id) == int(new_bike["id"]):
        raise ServiceError("Это тот же велосипед.")
    if new_bike.get("status") != "available":
        raise ServiceError(
            f"Велосипед {new_bike.get('code')} сейчас "
            f"«{logic.BIKE_STATUSES.get(new_bike.get('status'), new_bike.get('status'))}».")
    status = old_status or logic.SWAP_BIKE_STATUS.get(reason, "available")
    if status not in logic.BIKE_MANUAL_STATUSES:
        raise ServiceError("Недопустимый статус снятого велосипеда.")
    ok = await crm.swap_rental_bike(
        rental["id"], old_bike_id=old_id, new_bike_id=new_bike["id"],
        old_status=status, mileage_old=mileage_old, mileage_new=mileage_new,
        reason=logic.SWAP_REASONS[reason], today=date.today(), by=by)
    if not ok:
        raise ServiceError("Аренда изменилась, пока вы заполняли форму. "
                           "Откройте её заново.")
    return {"old_bike_id": old_id, "new_bike_id": new_bike["id"],
            "old_status": status}


async def start_search(crm: Any, rental: dict, *, note: str | None, by: str) -> None:
    """Объявить велосипед в розыск: клиент не платит и не отвечает.

    Аренда остаётся идущей, а начисления - начисляться: розыск не прощает
    долг, он поднимает флаг, чтобы велосипед не растворился в списке
    должников.
    """
    if rental.get("status") != "active":
        raise ServiceError("Аренда закрыта.")
    if logic.in_search(rental):
        raise ServiceError("Эта аренда уже в розыске.")
    await crm.update_rental(rental["id"], search_at=datetime.now(UTC),
                            search_by=by, search_note=note)


async def stop_search(crm: Any, rental: dict, *, by: str) -> None:
    """Снять розыск: клиент нашёлся, велосипед вернули."""
    del by
    if not logic.in_search(rental):
        raise ServiceError("Эта аренда не в розыске.")
    await crm.update_rental(rental["id"], search_at=None, search_by=None,
                            search_note=None)


async def declare_theft(crm: Any, rental: dict, *, note: str | None, by: str) -> None:
    """Признать технику потерянной: аренда закрывается, велосипед - lost.

    Батареи уходят туда же. Раньше эта кнопка звала базу напрямую, минуя
    возврат батарей, и выданные аккумуляторы оставались «у клиента»
    навсегда: велосипед числился потерянным, а две батареи при нём -
    живыми и свободными к выдаче.

    Долг клиента остаётся в журнале: списывать его - отдельное решение
    владельца, и делается оно корректировкой, а не этой кнопкой.
    """
    if rental.get("status") != "active":
        raise ServiceError("Аренда уже закрыта.")
    reason = (note or "").strip() or "Признан потерянным: клиент не вернул велосипед"
    await close_rental(crm, rental, closed_on=date.today(), note=reason,
                       bike_status="lost", by=by, battery_status="lost")


async def buy_bikes(crm: Any, *, supplier_id: int | None, purchased_on: date,
                    codes: list[str], model: str, price: Decimal,
                    battery_count: int, service_months: int, residual: Decimal,
                    battery_price: Decimal | None, battery_months: int,
                    location: str | None, note: str | None, by: str) -> dict:
    """Завести партию велосипедов одной закупкой.

    Проверка занятых номеров - до вставки: заводить половину партии и
    падать на седьмом номере значит оставить парк в состоянии, которого
    оператор не ожидает.
    """
    if not codes:
        raise ServiceError("Укажите инвентарные номера.")
    taken = [code for code in codes if await crm.bike_by_code(code) is not None]
    if taken:
        raise ServiceError("Эти номера уже есть в парке: " + ", ".join(taken[:5])
                           + ("…" if len(taken) > 5 else ""))
    # Партия заводится по тем же правилам, что и одиночный велосипед из
    # панели: включена сверка - вся партия встаёт «на сборке». Иначе
    # двадцать рам с накладной сразу попадали в свободные, то есть в
    # знаменатель простоя и в убыток, хотя их ещё никто не собирал.
    status = ("new" if logic.bike_check_settings(await crm.settings())["required"]
              else "available")
    bikes = [{"code": code, "model": model, "battery_count": int(battery_count),
              "purchase_price": logic.to_money(price),
              "service_months": int(service_months),
              "residual_price": logic.to_money(residual),
              "battery_price": battery_price, "location": location,
              "battery_service_months": int(battery_months), "note": note,
              "status": status}
             for code in codes]
    try:
        purchase_id = await crm.create_purchase(
            supplier_id=supplier_id, purchased_on=purchased_on, note=note,
            bikes=bikes, created_by=by)
    except Exception as exc:                            # noqa: BLE001
        if "unique" in type(exc).__name__.lower():
            raise ServiceError("Пока вы заполняли форму, эти номера завели. "
                               "Проверьте парк.") from exc
        raise
    return {"purchase_id": purchase_id, "bikes": len(bikes)}


async def issue_with_batteries(crm: Any, rental_id: int, *, bike: dict | None,
                               battery_ids: list[int], by: str) -> int:
    """Выдать батареи вместе с велосипедом. Возвращает, сколько выдали.

    Проверка занятости - до выдачи: батарея, уже уехавшая с другим
    клиентом, не должна молча «выдаться» второй раз.
    """
    ready = []
    for battery_id in battery_ids:
        battery = await crm.battery(int(battery_id))
        if battery is None:
            raise ServiceError("Такой батареи нет.")
        if battery["status"] != "available":
            raise ServiceError(
                f"Батарея {battery['code']} сейчас "
                f"«{logic.BATTERY_STATUSES.get(battery['status'], battery['status'])}».")
        ready.append(int(battery_id))
    await crm.issue_batteries(rental_id, battery_ids=ready,
                              bike_id=(bike or {}).get("id"), by=by)
    return len(ready)


async def add_battery_extra(crm: Any, rental: dict, battery: dict, *,
                            tariffs: Iterable[dict], by: str) -> Decimal:
    """Добавить доп. аккумулятор в аренду как платную позицию.

    Цена берётся из тарифов на тот же срок, что у аренды. Нет такого
    тарифа - отказ: бесплатная батарея и батарея без цены выглядят
    одинаково, и разбираться с этим должен человек в тарифах, а не
    выдача молча.

    Новая цена действует со следующего начисления: текущий период уже
    начислен по старой, и переписывать начисленное задним числом
    значит менять клиенту сумму после того, как он её увидел.
    """
    if rental.get("status") != "active":
        raise ServiceError("Аренда закрыта.")
    if battery.get("status") != "available":
        raise ServiceError(
            f"Батарея {battery.get('code')} сейчас "
            f"«{logic.BATTERY_STATUSES.get(battery.get('status'), battery.get('status'))}».")
    live = logic.live_extras(await crm.rental_extras(rental["id"]))
    if sum(1 for e in live if e.get("kind") == "battery") >= logic.MAX_EXTRA_BATTERIES:
        raise ServiceError(
            f"Больше {logic.MAX_EXTRA_BATTERIES} доп. аккумуляторов на аренду "
            "не выдаём.")
    price = logic.battery_extra_price(tariffs, battery, rental.get("period_days"))
    if price is None:
        raise ServiceError(
            f"Нет тарифа на аккумулятор «{battery.get('model_title') or '—'}» "
            f"на {int(rental.get('period_days') or 0)} дн. — заведите цену "
            "в тарифах.")
    await crm.add_rental_extra(
        rental["id"], kind="battery",
        title=logic.extra_title("battery", battery.get("model_title")),
        price=price, battery_id=int(battery["id"]), by=by)
    await crm.issue_batteries(rental["id"], battery_ids=[int(battery["id"])],
                              bike_id=rental.get("bike_id"), by=by)
    return price


async def drop_battery_extra(crm: Any, rental: dict, extra: dict, *, by: str,
                             status: str = "available") -> None:
    """Снять позицию и вернуть батарею в парк.

    Снять одно без другого нельзя: батарея у клиента без позиции едет
    бесплатно, позиция без батареи - это деньги ни за что.
    """
    if int(extra.get("rental_id") or 0) != int(rental["id"]):
        raise ServiceError("Позиция не от этой аренды.")
    if not await crm.drop_rental_extra(int(extra["id"]), by=by):
        raise ServiceError("Позиция уже снята.")
    if extra.get("battery_id"):
        await crm.return_battery(int(extra["battery_id"]), status=status, by=by)


async def swap_battery(crm: Any, rental: dict, old: dict | None, new: dict, *,
                       by: str, old_status: str = "repair") -> None:
    """Заменить батарею у клиента: старая возвращается, новая уходит.

    Батарея ломается чаще велосипеда, и менять её - обычная операция
    на точке. Аренду это не трогает вовсе.
    """
    if rental.get("status") != "active":
        raise ServiceError("Аренда закрыта.")
    if new.get("status") != "available":
        raise ServiceError(
            f"Батарея {new.get('code')} сейчас "
            f"«{logic.BATTERY_STATUSES.get(new.get('status'), new.get('status'))}».")
    if old is not None and int(old["id"]) == int(new["id"]):
        raise ServiceError("Это та же батарея.")
    if old is not None:
        if old_status not in logic.BATTERY_MANUAL_STATUSES:
            raise ServiceError("Недопустимый статус снятой батареи.")
        await crm.update_battery(old["id"], status=old_status, rental_id=None,
                                 cycles=int(old.get("cycles") or 0) + 1, by=by)
    await crm.update_battery(new["id"], status="rented", rental_id=rental["id"],
                             bike_id=rental.get("bike_id"), by=by)


# ─────────────────────────── касса ───────────────────────────


async def open_cash_shift(crm: Any, *, location: str | None, opening: Decimal,
                          note: str | None, by: str) -> int:
    """Открыть смену на точке. Вторая открытая на той же точке невозможна:
    два ящика с одним названием - это два ответа на вопрос «куда легли
    деньги», и оба неверные."""
    if await crm.open_shift_at(location) is not None:
        raise ServiceError("На этой точке уже открыта смена. Сначала закройте её.")
    if opening < 0:
        raise ServiceError("Размен не может быть отрицательным.")
    return await crm.create_shift(location=location, opening=opening, note=note,
                                  by=by)


async def cash_move(crm: Any, shift: dict, *, kind: str, amount: Decimal,
                    reason: str | None, by: str) -> int:
    """Внесение или изъятие из ящика."""
    if shift.get("status") != "open":
        raise ServiceError("Смена закрыта.")
    if kind not in logic.CASH_MOVE_KINDS:
        raise ServiceError("Неизвестный вид движения.")
    if amount <= 0:
        raise ServiceError("Сумма движения должна быть больше нуля.")
    if kind == "out":
        state = logic.shift_state(shift, await crm.shift_payments(shift["id"]),
                                  await crm.cash_moves(shift["id"]))
        if amount > state["expected"]:
            raise ServiceError(
                f"В кассе {logic.money(state['expected'])} — изъять больше нечего.")
    return await crm.add_cash_move(shift["id"], kind=kind, amount=amount,
                                   reason=reason, by=by)


async def close_cash_shift(crm: Any, shift: dict, *, counted: Decimal,
                           note: str | None, by: str) -> dict:
    """Закрыть смену с пересчётом. Возвращает состояние с расхождением.

    Расхождение не «исправляется» подгонкой ожидаемого: оно записывается
    как есть. Смена, которая всегда сходится, ничего не проверяет.
    """
    if shift.get("status") != "open":
        raise ServiceError("Смена уже закрыта.")
    if counted < 0:
        raise ServiceError("Посчитанная сумма не может быть отрицательной.")
    state = logic.shift_state({**shift, "counted": counted},
                              await crm.shift_payments(shift["id"]),
                              await crm.cash_moves(shift["id"]))
    await crm.close_shift(shift["id"], counted=counted, expected=state["expected"],
                          note=note, by=by)
    return state


# ─────────────────────────── банк ───────────────────────────


async def credit_bank_txn(crm: Any, txn: dict, client: dict, *, by: str,
                          method: str = "transfer") -> int:
    """Зачислить поступление клиенту: платёж в журнал, строка - разобрана.

    Деньги попадают в журнал обычным платежом, а не особым видом записи:
    средний чек считается по платежам, и «банковский» платёж, невидимый
    отчётам, испортил бы его молча.
    """
    if txn.get("status") != "new":
        raise ServiceError("Эта строка выписки уже разобрана.")
    if txn.get("direction") != "credit":
        raise ServiceError("Это списание со счёта, а не поступление.")
    if client.get("status") != "active":
        raise ServiceError("Клиент заблокирован или в чёрном списке.")
    ledger_id = await crm.credit_bank_txn(
        txn["id"], client_id=client["id"], amount=logic.to_money(txn["amount"]),
        method=method,
        note=f"Выписка банка: {txn.get('purpose') or txn['txn_id']}"[:500],
        created_by=by)
    if ledger_id is None:
        raise ServiceError("Эта строка выписки уже разобрана.")
    return ledger_id


async def ignore_bank_txn(crm: Any, txn: dict, *, by: str) -> None:
    """Платёж не наш: ремонт чужой техники, возврат поставщика, личное."""
    if txn.get("status") == "matched":
        raise ServiceError("Поступление уже зачислено клиенту.")
    await crm.mark_bank_txn(txn["id"], status="ignored", client_id=None,
                            ledger_id=None, by=by)


async def import_statement(crm: Any, rows: Iterable[dict]) -> dict:
    """Сложить выписку в базу. Повторы - норма: выписку тянут за
    перекрывающиеся периоды, и вторая встреча той же операции не ошибка."""
    rows = list(rows)
    saved = 0
    for row in rows:
        if await crm.save_bank_txn(row) is not None:
            saved += 1
    return {"seen": len(rows), "saved": saved}


# ─────────────────────────── рассылки ───────────────────────────


async def create_campaign(crm: Any, *, title: str, template: dict, audience: str,
                          note: str | None, by: str,
                          today: date | None = None) -> dict:
    """Собрать кампанию и очередь получателей.

    Очередь считается сразу, а не в момент отправки: черновик существует
    именно затем, чтобы посмотреть на список до того, как двести человек
    получат сообщение.
    """
    if audience not in logic.AUDIENCES:
        raise ServiceError("Неизвестная аудитория.")
    if not template or not template.get("active", True):
        raise ServiceError("Шаблон не выбран или снят с публикации.")
    people = logic.pick_audience(audience, await crm.clients_for_mailing(),
                                 await crm.active_rentals(), today=today)
    if not people:
        raise ServiceError("В этой аудитории сейчас никого: "
                           "рассылать нечего.")
    campaign_id = await crm.create_campaign(
        title=title, template_id=template["id"], audience=audience, note=note,
        by=by)
    queued = await crm.queue_sends(
        campaign_id, [(int(p["id"]), p["channel"]) for p in people])
    return {"id": campaign_id, "queued": queued}


async def start_campaign(crm: Any, campaign: dict) -> None:
    if campaign.get("status") != "draft":
        raise ServiceError("Отправлять можно только черновик.")
    await crm.set_campaign_status(campaign["id"], "sending")


async def cancel_campaign(crm: Any, campaign: dict) -> int:
    """Остановить рассылку. Отправленное не отзывается - Telegram и MAX
    этого не умеют; снимается только то, что ещё в очереди."""
    if campaign.get("status") in ("done", "cancelled"):
        raise ServiceError("Кампания уже завершена.")
    left = 0
    for send in await crm.campaign_sends(campaign["id"], status="queued"):
        await crm.mark_send(send["id"], status="skipped", error="отменено")
        left += 1
    await crm.set_campaign_status(campaign["id"], "cancelled")
    return left


# ───────────── простая электронная подпись (ПЭП) ─────────────


def sign_docs(client: dict, rental: dict | None,
              bot_user: dict | None) -> list[dict]:
    """Пакет документов для подписания.

    Договор и согласие собирает бот при регистрации - у них уже есть
    файлы и хэши, и пересобирать их в панели нечем: паспортные данные
    зашифрованы ключом, которого у панели нет. Поэтому пакет ссылается
    на то, что уже выдано, а не выдумывает второй экземпляр.
    """
    docs: list[dict] = []
    user = bot_user or {}
    contract_no = client.get("contract_no") or user.get("contract_no") or ""
    if user.get("contract_sha256"):
        docs.append({"kind": "contract",
                     "title": f"Договор аренды{' № ' + contract_no if contract_no else ''}",
                     "sha256": user["contract_sha256"],
                     "path": user.get("contract_path")})
    if user.get("soglasie_sha256"):
        docs.append({"kind": "consent",
                     "title": "Согласие на обработку персональных данных",
                     "sha256": user["soglasie_sha256"],
                     "path": user.get("soglasie_path")})
    if rental is not None:
        docs.append({"kind": "act_in",
                     "title": f"Акт приёма-передачи по аренде № {rental['id']}",
                     "sha256": "", "path": None})
    return docs


async def start_signing(crm: Any, *, client: dict, rental: dict | None,
                        company: dict, bot_user: dict | None, by: str,
                        now: datetime | None = None) -> dict:
    """Создать заявку на подпись: пакет документов и соглашение об ЭП."""
    if client.get("status") != "active":
        raise ServiceError("Клиент заблокирован или в чёрном списке.")
    now = now or datetime.now(UTC)
    token = logic.make_sign_token()
    docs = sign_docs(client, rental, bot_user)
    # Номер заявки выдаёт база, а он стоит в тексте соглашения. Поэтому
    # сначала заявка, потом соглашение с её номером: придумывать номер
    # заранее значит разойтись с базой при первой же гонке операторов.
    created = await crm.create_sign_request(
        client_id=client["id"], rental_id=(rental or {}).get("id"), token=token,
        docs=docs, agreement="",
        expires_at=now + timedelta(days=logic.SIGN_LINK_DAYS), by=by)
    agreement = esign.build_agreement(company, client, no=created["no"], docs=docs,
                                      code_minutes=logic.SIGN_CODE_MINUTES)
    docs = [{"kind": "esign", "title": "Соглашение об использовании ПЭП",
             "sha256": esign.sha256_text(agreement), "path": None}, *docs]
    await crm.set_sign_agreement(created["id"], agreement=agreement, docs=docs)
    return {**created, "token": token, "docs": docs}


async def issue_sign_code(crm: Any, request: dict, *, ip: str | None = None,
                          agent: str | None = None,
                          now: datetime | None = None) -> str:
    """Выдать код подтверждения. Возвращает сам код - его увидит только
    клиент в сообщении и оператор в панели, в базе останется лишь хэш."""
    state = logic.sign_state(request, now=now)
    if not state["open"]:
        raise ServiceError("Ссылка недействительна: подписано, отменено "
                           "или истёк срок.")
    code = logic.make_sign_code()
    await crm.set_sign_code(request["id"],
                            code_hash=logic.hash_sign_code(request["token"], code))
    await crm.log_sign_event(request["id"], kind="code_sent", ip=ip, agent=agent)
    return code


async def verify_sign(crm: Any, request: dict, raw_code: str, *,
                      ip: str | None = None, agent: str | None = None,
                      now: datetime | None = None) -> dict:
    """Проверить код и подписать пакет.

    Неверный код - это событие в журнале и минус попытка, а не молчание:
    в споре важно видеть, сколько раз и когда пытались.
    """
    state = logic.sign_state(request, now=now)
    if state["signed"]:
        raise ServiceError("Документы уже подписаны.")
    if not state["open"]:
        raise ServiceError("Ссылка недействительна: отменено или истёк срок.")
    if not request.get("code_hash"):
        raise ServiceError("Сначала получите код.")
    if not state["code_valid"]:
        raise ServiceError("Код больше не действует — получите новый.")
    code = logic.clean_sign_code(raw_code)
    if len(code) != 6:
        raise ServiceError("Код — шесть цифр.")
    if logic.hash_sign_code(request["token"], code) != request["code_hash"]:
        left = await crm.bump_sign_attempt(request["id"])
        await crm.log_sign_event(request["id"], kind="code_wrong", ip=ip,
                                 agent=agent,
                                 note=f"попытка {left}")
        raise ServiceError(
            f"Неверный код. Осталось попыток: "
            f"{max(logic.SIGN_MAX_ATTEMPTS - left, 0)}.")
    if not await crm.mark_signed(request["id"], ip=ip, agent=agent):
        raise ServiceError("Документы уже подписаны.")
    digest = logic.sign_docs_digest(request.get("docs") or [])
    await crm.log_sign_event(request["id"], kind="signed", ip=ip, agent=agent,
                             note=f"хэш пакета {digest}")
    return {"digest": digest, "docs": list(request.get("docs") or [])}


# ─────────────────────── приём оплаты ───────────────────────


async def create_pay_order(crm: Any, *, client: dict, rental: dict | None,
                           amount: Decimal, by: str, kind: str = "link",
                           acquiring: Any = None) -> dict:
    """Выставить счёт и получить на него ссылку с чеком.

    Счёт заводится до похода в банк: если банк не ответит, останется
    запись с текстом отказа, а не молчание. Оператор увидит причину и
    примет наличные, а не будет гадать, нажалась ли кнопка.
    """
    amount = logic.to_money(amount)
    if amount <= 0:
        raise ServiceError("Сумма счёта должна быть больше нуля")
    purpose = logic.pay_purpose(client, rental)
    order_id = await crm.create_pay_order(
        client_id=client["id"], rental_id=(rental or {}).get("id"),
        amount=amount, purpose=purpose, kind=kind, created_by=by)
    if acquiring is None or not getattr(acquiring, "token", ""):
        await crm.mark_pay_failed(
            order_id, error="Эквайринг не настроен: ссылку выдать нечем")
        return await crm.pay_order(order_id)
    try:
        got = await acquiring.payment_link(
            amount=amount, purpose=purpose, client_phone=client.get("phone"),
            client_email=client.get("email"))
    except Exception as err:                            # noqa: BLE001
        log.warning("ссылка на оплату не получена", exc_info=True)
        await crm.mark_pay_failed(order_id, error=str(err))
        return await crm.pay_order(order_id)
    await crm.set_pay_link(order_id, link=got.get("link") or "",
                           operation_id=got.get("operation_id"))
    return await crm.pay_order(order_id)


async def check_pay_order(crm: Any, order: dict, *, acquiring: Any) -> str:
    """Спросить у банка про один счёт. Возвращает новое состояние.

    Оплату записываем один раз: `mark_pay_paid` сам отказывается писать
    в журнал повторно, поэтому лишний опрос ничего не ломает.
    """
    operation = str(order.get("operation_id") or "")
    if not operation or acquiring is None:
        return str(order.get("status") or "")
    try:
        state = await acquiring.payment_status(operation)
    except Exception:                                   # noqa: BLE001
        log.warning("статус счёта %s не получен", order.get("no"), exc_info=True)
        await crm.touch_pay_order(order["id"])
        return str(order.get("status") or "")
    if state.get("state") == "paid":
        await crm.mark_pay_paid(order["id"], method="card", by="эквайринг")
        await _remember_card(crm, order, state.get("card") or {})
        return "paid"
    if state.get("state") == "dead":
        await crm.mark_pay_failed(
            order["id"], error=f"банк: {state.get('status') or 'оплата не прошла'}")
        return "failed"
    await crm.touch_pay_order(order["id"])
    return str(order.get("status") or "")


async def _remember_card(crm: Any, order: dict, card: dict) -> None:
    """Сохранить карту, если банк отдал токен. Без токена автосписания
    не будет - и выдумывать его нельзя."""
    token = str(card.get("token") or "")
    if not token:
        return
    try:
        await crm.save_card_token(
            client_id=order["client_id"], token=token,
            mask=logic.card_mask(card.get("mask")),
            expires=str(card.get("expires") or "") or None)
    except Exception:                                   # noqa: BLE001
        log.warning("карта клиента %s не сохранена", order.get("client_id"),
                    exc_info=True)


async def cancel_pay_order(crm: Any, order: dict, *, by: str) -> None:
    if order.get("status") not in logic.PAY_OPEN:
        raise ServiceError("Счёт уже закрыт")
    await crm.cancel_pay_order(order["id"], by=by)


async def credit_pay_order(crm: Any, order: dict, *, by: str,
                           method: str = "cash") -> int | None:
    """Закрыть счёт руками: клиент заплатил наличными или переводом.

    Тот же счёт, тот же номер в назначении - но подтверждает человек, и
    в журнале это видно по способу оплаты.
    """
    if order.get("status") == "paid":
        raise ServiceError("Счёт уже оплачен")
    if order.get("status") == "cancelled":
        raise ServiceError("Счёт снят, оплачивать нечего")
    return await crm.mark_pay_paid(order["id"], method=method, by=by,
                                   shift_id=await cash_shift_id(crm, method, by))


async def autocharge_once(crm: Any, *, acquiring: Any, bot: Any = None,
                          today: date | None = None, limit: int = 50) -> dict:
    """Суточный проход автосписания.

    Списываем только уже начисленный долг: аренда платится вперёд, и
    начисление на новый период создаёт биллинг, а не эта функция. Без
    карты клиент сюда не попадает, а три отказа подряд снимают карту -
    дальше долбить банк бессмысленно.
    """
    today = today or date.today()
    settings = logic.pay_settings(await crm.settings())
    if not settings["autocharge"]:
        return {"charged": 0, "failed": 0, "pending": 0,
                "skipped": "выключено"}
    cards = {int(c["client_id"]): c for c in await crm.cards()}
    if not cards:
        return {"charged": 0, "failed": 0, "pending": 0,
                "skipped": "нет привязанных карт"}
    due = logic.autocharge_due(await crm.active_rentals(), today=today, cards=cards)
    charged = failed = pending = 0
    for item in due[:limit]:
        card = cards[item["client_id"]]
        client = await crm.client(item["client_id"])
        if client is None:
            continue
        # Счёт заводим напрямую: ссылка автосписанию не нужна, а через
        # create_pay_order он бы сначала стал «отказом банка».
        purpose = logic.pay_purpose(client, item["rental"])
        order_id = await crm.create_pay_order(
            client_id=client["id"], rental_id=item["rental"].get("id"),
            amount=item["amount"], purpose=purpose, kind="auto",
            created_by="автосписание")
        try:
            state = await acquiring.charge_saved_card(
                token=card["token"], amount=item["amount"],
                purpose=purpose, client_phone=client.get("phone"),
                client_email=client.get("email"))
        except Exception as err:                        # noqa: BLE001
            log.warning("автосписание клиенту %s не прошло", item["client_id"],
                        exc_info=True)
            await crm.mark_pay_failed(order_id, error=str(err))
            await _card_failed(crm, bot, client, card, item["amount"], str(err))
            failed += 1
            continue
        if state.get("state") == "paid":
            await crm.mark_pay_paid(order_id, method="card", by="автосписание")
            await crm.touch_card(card["id"])
            fresh = await crm.active_rental_of(client["id"])
            balance = await crm.client_balance(client["id"])
            summary = logic.rental_summary(fresh, balance, today=today)
            await _tell_autocharge(crm, bot, client, card, item["amount"],
                                   ok=True, until=summary.get("covered_until"))
            charged += 1
        elif state.get("state") == "pending" and state.get("operation_id"):
            # Банк принял платёж, но ещё не подтвердил. Это не отказ:
            # счёт остаётся открытым с номером операции, и минутный
            # опрос спросит банк сам. Раньше такой ответ считался
            # отказом - клиенту уходило «списание не прошло», карта
            # получала штрафное очко, а деньги потом списывались.
            await crm.set_pay_link(order_id, link="",
                                   operation_id=state["operation_id"])
            pending += 1
        else:
            reason = f"банк: {state.get('status') or 'списание не прошло'}"
            await crm.mark_pay_failed(order_id, error=reason)
            await _card_failed(crm, bot, client, card, item["amount"], reason)
            failed += 1
    return {"charged": charged, "failed": failed, "pending": pending,
            "skipped": ""}


async def _card_failed(crm: Any, bot: Any, client: dict, card: dict,
                       amount: Decimal, reason: str) -> None:
    """Отказ банка: посчитать его и снять карту на третьем подряд.

    Считать было нечем, и просроченная карта получала отказ каждые сутки,
    каждые сутки сообщая об этом клиенту. Счётчик сбрасывается удачным
    списанием: три отказа подряд - это мёртвая карта, а не пустой счёт
    в один конкретный день.
    """
    fails = 0
    try:
        fails = await crm.card_failed(card["id"], limit=logic.AUTOCHARGE_FAILS)
    except Exception:                                    # noqa: BLE001
        log.warning("отказ по карте клиента %s не посчитан", client.get("id"),
                    exc_info=True)
    dropped = fails >= logic.AUTOCHARGE_FAILS
    if dropped:
        reason = (f"{reason}. Карта отвязана после {fails} отказов подряд, "
                  "привяжите её заново при следующей оплате по ссылке")
    await _tell_autocharge(crm, bot, client, card, amount, ok=False, reason=reason)


async def _tell_autocharge(crm: Any, bot: Any, client: dict, card: dict,
                           amount: Decimal, *, ok: bool, reason: str = "",
                           until: Any = None) -> None:
    """Сказать клиенту о списании. Молчать нельзя ни при удаче, ни при
    отказе: в первом случае это выглядит как списание без спроса, во
    втором клиент узнаёт о долге только из просрочки."""
    if bot is None:
        return
    code = "autocharge_ok" if ok else "autocharge_fail"
    await notices.send_client(
        crm, code, client["id"],
        (lambda: notify.autocharge_ok(bot, client, amount, card, until)) if ok
        else (lambda: notify.autocharge_fail(bot, client, amount, card, reason)))


# ─────────────────── смета и счёт за ремонт ───────────────────


async def send_estimate(crm: Any, order: dict, *, by: str, bot: Any = None) -> dict:
    """Отправить смету клиенту и поставить наряд на согласование.

    Смета собирается из строк наряда, а не из поля «смета»: клиент должен
    видеть, за что платит. Пустой наряд согласовывать нечего - на этом и
    останавливаемся, вместо того чтобы прислать человеку «0 ₽».
    """
    if not logic.order_is_open(order):
        raise ServiceError("Наряд закрыт, согласовывать нечего.")
    if order.get("payer") != "client":
        raise ServiceError("Свой ремонт согласовывать не с кем: "
                           "смета нужна там, где платит клиент.")
    items = await crm.order_items(order["id"])
    total = logic.order_totals_client(items)
    if not items or total <= 0:
        raise ServiceError("В наряде нет строк с ценой клиенту — "
                           "смету собрать не из чего.")
    client = (await crm.client(int(order["client_id"]))
              if order.get("client_id") else None)
    await crm.update_work_order(
        order["id"], status="approve", estimate=total,
        estimate_sent_at=datetime.now(UTC), approved_at=None,
        approved_by=None, declined_at=None)
    sent = False
    if client is not None:
        sent = await notices.send_client(
            crm, "estimate_sent", client["id"],
            lambda: notify.estimate(bot, client, order, items, total))
    return {"total": total, "items": items, "sent": sent}


async def answer_estimate(crm: Any, order: dict, *, agree: bool, by: str) -> dict:
    """Ответ на смету: согласовано или отказ.

    Согласовать можно и не отправляя смету в бота: клиент стоит у стойки
    и говорит «да». Гнать его в переписку ради кнопки значит держать
    технику разобранной лишний час - и ровно поэтому статус «на
    согласовании» здесь не обязателен.

    Отказ закрывает наряд отменой: держать открытым то, от чего клиент
    отказался, значит вечно видеть его в «в работе» и считать простой.
    """
    live = order.get("status") != "approve"
    if live:
        # Ответ бывает один: согласованное потом «отказался» отменило бы
        # наряд, по которому уже работают.
        if order.get("approved_at") or order.get("declined_at"):
            raise ServiceError("По этой смете уже ответили.")
        if not logic.order_is_open(order):
            raise ServiceError("Наряд закрыт, согласовывать нечего.")
        if order.get("payer") != "client":
            raise ServiceError("Свой ремонт согласовывать не с кем: "
                               "смета нужна там, где платит клиент.")
    now = datetime.now(UTC)
    if agree:
        fields: dict[str, Any] = {"status": "in_work", "approved_at": now,
                                  "approved_by": by, "declined_at": None}
        if live:
            # Согласовали мимо отправки - сумму всё равно фиксируем по
            # строкам наряда: иначе в смете останется ноль, и на спор
            # «я такого не заказывал» отвечать будет нечем.
            items = await crm.order_items(order["id"])
            total = logic.order_totals_client(items)
            if not items or total <= 0:
                raise ServiceError("В наряде нет строк с ценой клиенту — "
                                   "согласовывать нечего.")
            fields["estimate"] = total
        if not await crm.answer_work_order(order["id"], **fields):
            raise ServiceError("По этой смете уже ответили.")
    else:
        if not await crm.answer_work_order(order["id"], status="cancelled",
                                           declined_at=now, closed_at=now):
            raise ServiceError("По этой смете уже ответили.")
        if order.get("bike_id"):
            bike = await crm.bike(order["bike_id"])
            if bike and bike.get("status") in ("repair", "maintenance"):
                await crm.update_bike(order["bike_id"], status="available", by=by)
    return {"agree": agree, "by": by}


async def invoice_order(crm: Any, order: dict, *, by: str,
                        acquiring: Any = None) -> dict:
    """Счёт клиенту за ремонт.

    Тот же счёт, что и за аренду, но привязан к наряду - и поэтому его
    оплата в crm.ledger не попадает: журнал это аренда, средний чек
    считается по нему, и ремонт чужого самоката его бы завысил.
    """
    if order.get("payer") != "client":
        raise ServiceError("Свой ремонт клиенту не выставляют.")
    if order.get("paid_at"):
        raise ServiceError("Ремонт уже оплачен.")
    amount = logic.to_money(order.get("total") or order.get("estimate"))
    if amount <= 0:
        raise ServiceError("Сумма ремонта не посчитана: закройте наряд "
                           "или соберите смету.")
    client = (await crm.client(int(order["client_id"]))
              if order.get("client_id") else None)
    if client is None:
        raise ServiceError("У наряда нет клиента, которому выставить счёт.")
    what = (f"№ {order['bike_code']}" if order.get("bike_code")
            else (order.get("object_note") or "техника"))
    purpose = f"Ремонт {what}, наряд {order.get('no') or ''}".strip()
    order_id = await crm.create_pay_order(
        client_id=client["id"], rental_id=None, amount=amount, purpose=purpose,
        kind="repair", work_order_id=order["id"], created_by=by)
    if acquiring is None or not getattr(acquiring, "token", ""):
        await crm.mark_pay_failed(
            order_id, error="Эквайринг не настроен: ссылку выдать нечем")
        return await crm.pay_order(order_id)
    try:
        got = await acquiring.payment_link(
            amount=amount, purpose=purpose, client_phone=client.get("phone"),
            client_email=client.get("email"))
    except Exception as err:                            # noqa: BLE001
        log.warning("ссылка на оплату ремонта не получена", exc_info=True)
        await crm.mark_pay_failed(order_id, error=str(err))
        return await crm.pay_order(order_id)
    await crm.set_pay_link(order_id, link=got.get("link") or "",
                           operation_id=got.get("operation_id"))
    return await crm.pay_order(order_id)


# ────────────── ввод техники в эксплуатацию ──────────────


async def commission_bike(crm: Any, bike: dict, *, by: str) -> None:
    """Выпустить велосипед в оборот.

    Проверка сверки здесь, а не в базе: владелец может её выключить, и
    тогда список полей остаётся подсказкой. Запрещать выдачу настройкой,
    которую сам же снял, система не должна.
    """
    if bike.get("status") != "new":
        raise ServiceError("Велосипед уже в обороте.")
    state = logic.bike_check_state(bike, await crm.settings())
    if not state["can_commission"]:
        raise ServiceError("Не сверено: " + ", ".join(state["left"])
                           + ". Подтвердите поля паспорта или снимите "
                             "требование сверки в настройках.")
    if not await crm.commission_bike(bike["id"], by=by):
        raise ServiceError("Велосипед уже выпустил кто-то другой.")


async def commission_battery(crm: Any, battery: dict, *, by: str) -> None:
    """Выпустить батарею в оборот - по тем же правилам, что велосипед."""
    if battery.get("status") != "new":
        raise ServiceError("Аккумулятор уже в обороте.")
    state = logic.battery_check_state(battery, await crm.settings())
    if not state["can_commission"]:
        raise ServiceError("Не сверено: " + ", ".join(state["left"])
                           + ". Подтвердите поля паспорта или снимите "
                             "требование сверки в настройках.")
    if not await crm.commission_battery(battery["id"], by=by):
        raise ServiceError("Аккумулятор уже выпустил кто-то другой.")


async def check_battery_field(crm: Any, battery: dict, field: str, *, by: str,
                              photo: str | None = None) -> None:
    """Отметить поле паспорта батареи сверенным."""
    if field not in logic.BATTERY_PASSPORT:
        raise ServiceError("Неизвестное поле паспорта.")
    if not logic.battery_field_value(battery, field):
        raise ServiceError(f"{logic.BATTERY_PASSPORT[field]}: поле пустое — "
                           "сверять нечего.")
    settings = await crm.settings()
    if (logic.bike_check_settings(settings)["photo"]
            and field in logic.BATTERY_PHOTO_FIELDS and not photo):
        marks = battery.get("checked") or {}
        was = marks.get(field) if isinstance(marks, dict) else None
        if not (isinstance(was, dict) and was.get("photo")):
            raise ServiceError(f"{logic.BATTERY_PASSPORT[field]}: нужен снимок. "
                               "Фотография доказывает, что человек смотрел "
                               "на технику, а не переписал номер из накладной.")
    await crm.mark_battery_checked(battery["id"], field, by=by, photo=photo)


async def check_bike_field(crm: Any, bike: dict, field: str, *, by: str,
                           photo: str | None = None) -> None:
    """Отметить поле паспорта сверенным.

    Пустое поле сверять нечего: отметка на пустоте - это ровно то
    «переписал из накладной», ради чего сверку и заводили.
    """
    if field not in logic.BIKE_PASSPORT:
        raise ServiceError("Неизвестное поле паспорта.")
    if not logic.bike_field_value(bike, field):
        raise ServiceError(f"{logic.BIKE_PASSPORT[field]}: поле пустое — "
                           "сверять нечего.")
    settings = await crm.settings()
    if (logic.bike_check_settings(settings)["photo"]
            and field in logic.BIKE_PHOTO_FIELDS and not photo):
        marks = bike.get("checked") or {}
        was = marks.get(field) if isinstance(marks, dict) else None
        if not (isinstance(was, dict) and was.get("photo")):
            raise ServiceError(f"{logic.BIKE_PASSPORT[field]}: нужен снимок. "
                               "Фотография доказывает, что человек смотрел "
                               "на технику, а не переписал номер из накладной.")
    await crm.mark_bike_checked(bike["id"], field, by=by, photo=photo)
