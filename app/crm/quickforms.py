"""Быстрые формы сотрудника в боте: сторонний ремонт и выдача.

Мастер у верстака и администратор на точке не открывают панель ради одной
записи: форма текстом в личку бота (шаблоны - /remont и /vydacha), бот
показывает, что понял, и пишет в CRM по кнопке - теми же функциями, что
и панель: наряд - `service.open_order`, `close_order`, `mark_repair_paid`;
выдача - `service.open_rental` и `add_entry`. Своих правил денег здесь нет:
ремонт по-прежнему в `crm.ledger` не идёт, платёж выдачи - обычный
`payment` со сменой принявшего.

Права - роль в панели: ремонт - «Сервис» на запись, выдача - «Быстрая
выдача» на запись. Сотрудник узнаётся по привязанному Telegram (`/staff
КОД`); отключённый или с истёкшим сроком - как чужой.

Модуль не знает про Telegram: возвращает текст (HTML), отправляет его
обработчик (app/handlers/staff.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from .. import logic as bot_logic
from . import billing, logic, notify, service

log = logging.getLogger(__name__)

REPAIR, ISSUE = "repair", "issue"
SECTIONS = {REPAIR: "service", ISSUE: "issue"}
# Объект наряда, если в форме нет строки «Техника:»: у чужой техники без
# велосипеда из парка наряд требует хоть какого-то описания.
THING_DEFAULT = "Техника клиента"


class FormError(Exception):
    """Форму не провести. Текст - сотруднику как есть (уже HTML)."""


@dataclass
class Preview:
    ok: bool
    text: str
    target: int = 0        # наряд, который форма дополняет; 0 - новый


def _esc(value: Any) -> str:
    return logic.html.escape(str(value if value not in (None, "") else "—"), quote=False)


def _money(value: Any) -> str:
    return logic.money(value)


def _dm(day: date | None) -> str:
    return day.strftime("%d.%m.%Y") if day else "—"


def _noon(day: date) -> datetime:
    """Момент из даты формы: полдень по местным часам - чтобы дата не
    съехала на соседние сутки ни в одном поясе отчёта."""
    return datetime.combine(day, time(12, 0)).astimezone()


def _phone(value: Any) -> str:
    return _esc(bot_logic.phone_for_form(value))


def who(staff: dict) -> str:
    """Автор записей - как в панели (`staff:логин`): по нему касса находит
    смену принявшего, а журналы - человека."""
    return f"staff:{staff['login']}"


async def staff_for(crm: Any, tg_id: int) -> dict | None:
    """Сотрудник по привязанному Telegram. Отключённый и с истёкшим сроком
    доступа - None: он и в панель уже не войдёт."""
    person = await crm.staff_by_tg(tg_id)
    if not person or not person.get("active", True) or logic.staff_expired(person):
        return None
    return person


def may(staff: dict | None, kind: str) -> bool:
    if not staff:
        return False
    return staff.get("role") == "admin" or logic.can_edit(staff, SECTIONS[kind])


def _errors(title: str, errors: list[str]) -> str:
    return (f"⚠️ <b>{title}: форму не провести</b>\n"
            + "\n".join(f"• {_esc(e)}" for e in errors)
            + "\n\nИсправьте и пришлите форму ещё раз.")


async def find_client(crm: Any, phone: str | None) -> dict | None:
    """Клиент по телефону - основному или запасному: звонит и пишет тот же
    человек с любого из трёх."""
    if not phone:
        return None
    client = await crm.client_by_phone(phone)
    if client is not None:
        return client
    for row in await crm.clients(q=phone[-10:], limit=20):
        for field in ("phone", "phone2", "phone3"):
            if bot_logic.normalize_phone(row.get(field)) == phone:
                return await crm.client(int(row["id"]))
    return None


# ─────────────────────────── сторонний ремонт ───────────────────────────

async def _tech(crm: Any, staff: dict, said: str) -> tuple[dict | None, str]:
    """Мастер из строки «Кто выполняет»: (сотрудник или None, предупреждение)."""
    word = logic._ops_norm(said)
    if not word or word in logic.QUICK_SELF_WORDS:
        return staff, ""
    found = logic.match_staff(await crm.staff_all(), said)
    if len(found) == 1:
        return found[0], ""
    if not found:
        return None, (f"«{said}» не нашёл среди сотрудников — наряд будет без мастера, "
                      "назначьте в панели или напишите имя, как в панели.")
    names = ", ".join(p.get("name") or p.get("login") for p in found)
    return None, f"«{said}» подходит нескольким: {names} — напишите точнее."


async def _repair_order(crm: Any, order_id: int) -> dict:
    order = await crm.work_order(order_id)
    if order is None:
        raise FormError("⚠️ Наряд не найден — пришлите форму заново, без ответа на карточку.")
    if order.get("payer") != "client" or order.get("bike_id"):
        raise FormError(f"⚠️ {_esc(order.get('no'))} — не сторонний ремонт: "
                        "его ведут в панели.")
    return order


async def preview_repair(crm: Any, staff: dict, text: str, *, today: date,
                         order: dict | None = None) -> Preview:
    """Что сделает форма: новый наряд или дополнение `order`."""
    data, errors = logic.parse_quick_repair(text, today=today)
    if errors:
        return Preview(False, _errors("Сторонний ремонт", errors))
    assert data is not None
    tech, warn = await _tech(crm, staff, data["tech"])
    lines: list[str] = []
    if order is None:
        lines.append("🔧 <b>Сторонний ремонт — новый наряд</b>")
        client = await find_client(crm, data["phone"])
        contact = _phone(data["phone"]) + (f" · @{_esc(data['username'])}"
                                           if data["username"] else "")
        if client is None:
            lines.append(f"Клиент: {_esc(data['name'])} · {contact} — <i>новая карточка</i>")
        else:
            lines.append(f"Клиент: {_esc(data['name'])} · {contact} — есть в CRM: "
                         f"<b>{_esc(client['full_name'])}</b>")
        lines.append(f"Обращение: {_dm(data['opened_on'])}")
        lines.append(f"Проблема: {_esc(data['problem'])}")
        if data["thing"]:
            lines.append(f"Техника: {_esc(data['thing'])}")
    else:
        lines.append(f"🔧 <b>{_esc(order['no'])} — дополнить</b>")
        lines.append(f"Клиент: {_esc(order.get('client_name') or data['name'])}")
        lines.append(f"Проблема: {_esc(order.get('complaint'))}")
    lines.append(f"Мастер: {_esc((tech or {}).get('name') or (tech or {}).get('login'))}"
                 if tech else f"Мастер: ⚠️ {_esc(warn)}")
    sums = data["work"] + data["parts"]
    lines.append(f"Работа: {_money(data['work'])} · запчасти: {_money(data['parts'])}")
    if order is not None:
        items = await crm.order_items(int(order["id"]))
        have = logic.order_totals_client(items)
        if items and sums > 0 and sums != have:
            lines.append(f"⚠️ В наряде уже строки на {_money(have)} — суммы формы не "
                         "применю, правьте строки в панели.")
    finish = data["finish_on"]
    open_now = order is None or logic.order_is_open(order)
    if finish and finish <= today:
        lines.append(f"Готово {_dm(finish)} — наряд закроется" if open_now
                     else f"Готово {_dm(finish)} — наряд уже закрыт")
    elif finish:
        lines.append(f"Срок: до {_dm(finish)} (оговорено) — наряд останется открытым")
    else:
        lines.append("Окончания нет — наряд останется открытым")
    if data["method"]:
        paid = order is not None and order.get("paid_at")
        method = logic.METHODS.get(data["method"], data["method"]).lower()
        lines.append("⚠️ Оплата уже отмечена — второй раз не отмечу" if paid else
                     f"Оплата: {method}, {_money(sums)}"
                     + (" → в кассу смены" if data["method"] == "cash" else ""))
    else:
        lines.append("Оплата: не отмечаю")
    return Preview(True, "\n".join(lines), target=int(order["id"]) if order else 0)


async def apply_repair(crm: Any, staff: dict, text: str, *, today: date,
                       order_id: int = 0, bot: Any = None) -> str:
    """Провести форму: открыть наряд (или дополнить `order_id`), строки
    сумм, закрытие по дате окончания и отметка оплаты. Возвращает карточку
    наряда - на неё отвечают той же формой, чтобы дополнить."""
    data, errors = logic.parse_quick_repair(text, today=today)
    if errors:
        raise FormError(_errors("Сторонний ремонт", errors))
    assert data is not None
    by = who(staff)
    tech, _ = await _tech(crm, staff, data["tech"])
    notes: list[str] = []
    if order_id:
        order = await _repair_order(crm, order_id)
        if tech and int(tech["id"]) != int(order.get("tech_id") or 0):
            await crm.update_work_order(order_id, tech_id=int(tech["id"]))
            await _tell_tech(crm, bot, order_id, tech, staff)
    else:
        client = await find_client(crm, data["phone"])
        if client is None:
            try:
                client_id = await crm.create_client(
                    full_name=data["name"], phone=data["phone"], username=data["username"],
                    note="Сторонний ремонт", source="manual", created_by=by)
            except Exception as exc:                     # noqa: BLE001
                # Тот же номер завели в эту секунду (панель или второй тап):
                # уникальный индекс телефона - берём ту карточку.
                client = await crm.client_by_phone(data["phone"])
                if client is None:
                    raise FormError("⚠️ Карточку клиента не завести — проверьте телефон.") \
                        from exc
            else:
                client = await crm.client(client_id)
        try:
            order_id = await service.open_order(
                crm, bike=None, payer="client", client=client,
                complaint=data["problem"], object_note=data["thing"] or THING_DEFAULT,
                tech_id=int(tech["id"]) if tech else None,
                estimate=data["work"] + data["parts"], by=by,
                location=staff.get("location") or None)
        except service.ServiceError as exc:
            raise FormError(f"⚠️ {_esc(exc)}") from exc
        if data["opened_on"] < today:
            await crm.update_work_order(order_id, opened_at=_noon(data["opened_on"]))
        if tech and int(tech["id"]) != int(staff["id"]):
            await _tell_tech(crm, bot, order_id, tech, staff)
    order = await crm.work_order(order_id)
    items = await crm.order_items(order_id)
    sums = data["work"] + data["parts"]
    if not items and sums > 0:
        for title, price in ((f"Работа: {data['problem']}"[:120], data["work"]),
                             ("Запчасти", data["parts"])):
            if price > 0:
                await crm.add_order_item(order_id, title=title, node=None,
                                         work_type_id=None, qty=1, price=price,
                                         parts_cost=Decimal(0), labor_cost=Decimal(0),
                                         note="из формы в боте")
    elif items and sums > 0 and sums != logic.order_totals_client(items):
        notes.append("суммы формы не применены: строки уже есть, правьте в панели")
    finish = data["finish_on"]
    if finish and finish > today:
        mark = f"Срок: до {finish:%d.%m.%Y} (оговорено с клиентом)"
        old = (order.get("note") or "").strip()
        if mark not in old:
            await crm.update_work_order(order_id, note=(f"{old}\n{mark}" if old else mark)[:1000])
    if finish and finish <= today and logic.order_is_open(order):
        try:
            await service.close_order(crm, order, by=by,
                                      closed_at=_noon(finish) if finish < today else None)
        except service.ServiceError as exc:
            notes.append(str(exc))
    if data["method"]:
        order = await crm.work_order(order_id)
        if order.get("paid_at"):
            notes.append("оплата уже была отмечена")
        else:
            if logic.order_is_open(order):
                # Оплата до закрытия: «к оплате» ещё не посчитан закрытием,
                # а наличные в ящик смены ложатся этой суммой.
                total = logic.order_totals_client(await crm.order_items(order_id))
                await crm.update_work_order(order_id, total=total)
                order = await crm.work_order(order_id)
            try:
                await service.mark_repair_paid(crm, order, method=data["method"], by=by)
            except service.ServiceError as exc:
                notes.append(str(exc))
    return await repair_card(crm, order_id, notes=notes)


async def _tell_tech(crm: Any, bot: Any, order_id: int, tech: dict, staff: dict) -> None:
    """Технику - «на тебя наряд», как из панели; себе не пишем."""
    if bot is None or int(tech["id"]) == int(staff["id"]):
        return
    order = await crm.work_order(order_id)
    if order is not None:
        await notify.order_assigned(bot, order, tech)


async def repair_card(crm: Any, order_id: int, *, notes: list[str] | None = None) -> str:
    """Карточка наряда в боте. Номер РЕМ- в ней - то, по чему ответ той же
    формой находит наряд, чтобы дополнить."""
    order = await crm.work_order(order_id)
    if order is None:
        return "⚠️ Наряд не найден."
    total = logic.order_totals_client(await crm.order_items(order_id))
    status = logic.ORDER_STATUSES.get(order.get("status"), order.get("status"))
    lines = [f"✅ <b>Наряд {_esc(order['no'])}</b> · сторонний ремонт",
             f"Клиент: {_esc(order.get('client_name'))}",
             f"Проблема: {_esc(order.get('complaint'))}",
             f"Мастер: {_esc(order.get('tech_name'))}",
             f"Сумма: {_money(total)} · {_esc(status)}"
             + (" · оплачен" if order.get("paid_at") else "")]
    lines += [f"⚠️ {_esc(n)}" for n in notes or []]
    lines.append("Чтобы дописать суммы, закрыть или отметить оплату — ответьте на это "
                 "сообщение той же формой.")
    return "\n".join(lines)


# ─────────────────────────────── выдача ───────────────────────────────

async def _issue_plan(crm: Any, data: dict, *, today: date) -> dict:
    """Клиент, велосипед, тариф и батарея для выдачи - или FormError с
    причиной, понятной администратору на точке."""
    client = await find_client(crm, data["phone"])
    if client is None:
        raise FormError(f"⚠️ Клиента с телефоном {_phone(data['phone'])} в CRM нет: "
                        "пусть пройдёт регистрацию в боте, или заведите карточку в панели.")
    if client.get("status") in ("blocked", "blacklist"):
        raise FormError(f"⚠️ {_esc(client['full_name'])}: клиент заблокирован или в "
                        "чёрном списке — выдача запрещена.")
    if await crm.active_rental_of(int(client["id"])) is not None:
        raise FormError(f"⚠️ У {_esc(client['full_name'])} уже идёт аренда — сначала "
                        "закройте её или оформите замену.")
    code = data["bike_code"]
    bike = await crm.bike_by_code(code) or await crm.bike_by_vin(code)
    if bike is None:
        raise FormError(f"⚠️ Велосипеда № {_esc(code)} в парке нет.")
    if bike.get("status") != "available":
        status = logic.BIKE_STATUSES.get(bike.get("status"), bike.get("status"))
        raise FormError(f"⚠️ № {_esc(bike['code'])} сейчас «{_esc(status)}» — "
                        "выдать можно только свободный.")
    tariffs = await crm.tariffs(active_only=True)
    aliases = logic.model_aliases(await crm.bike_models())
    rows = logic.tariffs_for_model(tariffs, bike.get("model"), aliases=aliases)
    tariff = next((t for t in rows if int(t.get("period_days") or 0) == data["term"]), None)
    if tariff is None:
        terms = sorted({int(t["period_days"]) for t in rows if t.get("period_days")})
        raise FormError(f"⚠️ Для «{_esc(bike.get('model'))}» нет тарифа на {data['term']} дн."
                        + (f" Есть: {', '.join(map(str, terms))}." if terms else ""))
    mileage = logic.check_mileage(str(data["mileage"]), current=bike.get("mileage_km"))
    if not mileage.ok:
        raise FormError(f"⚠️ {_esc(mileage.error)}")
    battery = None
    if data["battery_code"]:
        battery = await crm.battery_by_code(data["battery_code"])
        if battery is None:
            raise FormError(f"⚠️ Аккумулятора № {_esc(data['battery_code'])} нет.")
        if battery.get("status") != "available":
            raise FormError(f"⚠️ Аккумулятор № {_esc(battery['code'])} не свободен.")
    return {"client": client, "bike": bike, "tariff": tariff, "battery": battery,
            "mileage": mileage.value}


async def preview_issue(crm: Any, staff: dict, text: str, *, today: date) -> Preview:
    data, errors = logic.parse_quick_issue(text, today=today)
    if errors:
        return Preview(False, _errors("Выдача", errors))
    assert data is not None
    try:
        plan = await _issue_plan(crm, data, today=today)
    except FormError as exc:
        return Preview(False, str(exc))
    client, bike, tariff = plan["client"], plan["bike"], plan["tariff"]
    until = data["started_on"] + timedelta(days=int(tariff["period_days"]))
    lines = ["🚲 <b>Выдача — проверьте</b>",
             f"Клиент: <b>{_esc(client['full_name'])}</b> · {_phone(client.get('phone'))}",
             f"Велосипед: № {_esc(bike['code'])} · {_esc(bike.get('model'))}"
             + (f" · {_esc(bike['location'])}" if bike.get("location") else "")
             + f" · пробег {plan['mileage']} км",
             f"Тариф: {_esc(tariff['name'])} — {_money(tariff['price'])} за "
             f"{tariff['period_days']} дн., с {data['started_on']:%d.%m} по {until:%d.%m}"]
    if plan["battery"]:
        lines.append(f"Аккумулятор: № {_esc(plan['battery']['code'])}")
    if data["pay"] > 0:
        method = logic.METHODS.get(data["method"], data["method"]).lower()
        lines.append(f"Оплата: {_money(data['pay'])}, {method}"
                     + (" → в кассу смены" if data["method"] == "cash" else ""))
    else:
        lines.append("Без оплаты: первый период останется долгом на балансе")
    balance = await crm.client_balance(int(client["id"]))
    if balance:
        lines.append(f"На балансе сейчас: {_money(balance)}")
    risk = await service.client_risk(crm, int(client["id"]), today=today)
    if risk.get("level") not in (None, "low"):
        line = f"Риск: {_esc(risk.get('badge'))}"
        if risk.get("deposit") and logic.can_view(staff, "finance"):
            line += f" — рекомендуемый залог {_money(risk['deposit'])}"
        lines.append(line)
    if data["contract_no"]:
        lines.append(f"Договор: {_esc(data['contract_no'])}")
    return Preview(True, "\n".join(lines))


async def apply_issue(crm: Any, staff: dict, text: str, *, today: date, bot: Any = None,
                      db: Any = None, panel_url: str | None = None) -> str:
    """Провести выдачу: аренда с первым периодом, батарея, платёж в журнал
    со сменой принявшего, бонус приглашения, сообщение клиенту - как мастер
    выдачи в панели. Документы на подпись - шаг 5 мастера, по ссылке."""
    data, errors = logic.parse_quick_issue(text, today=today)
    if errors:
        raise FormError(_errors("Выдача", errors))
    assert data is not None
    plan = await _issue_plan(crm, data, today=today)
    client, bike, tariff = plan["client"], plan["bike"], plan["tariff"]
    by = who(staff)
    contract_no = data["contract_no"] or client.get("contract_no")
    if not contract_no and db is not None and client.get("tg_id"):
        contract_no = ((await db.get_user(int(client["tg_id"]))) or {}).get("contract_no")
    applied: list[dict] = []
    try:
        rental_id = await service.open_rental(
            crm, client=client, bike=bike, tariff=tariff, started_on=data["started_on"],
            contract_no=contract_no, by=by, mileage=plan["mileage"], applied=applied)
    except service.ServiceError as exc:
        raise FormError(f"⚠️ {_esc(exc)}") from exc
    notes: list[str] = []
    if plan["battery"]:
        try:
            await service.issue_with_batteries(crm, rental_id, bike=bike,
                                               battery_ids=[int(plan["battery"]["id"])],
                                               by=by)
        except service.ServiceError as exc:
            notes.append(f"{exc} Аккумулятор не выдан — отметьте в карточке аренды.")
    if data["pay"] > 0:
        await service.add_entry(crm, client, kind="payment", amount=data["pay"],
                                method=data["method"],
                                note=logic.ISSUE_PAY_NOTE.format(code=bike["code"]),
                                by=by, rental_id=rental_id)
        try:
            bonus = await service.ref_paid(crm, client, data["pay"], by=by)
            if bonus:
                await notify.referral_bonus(bot, db, bonus["agent"], client, bonus["bonus"])
        except Exception:                                # noqa: BLE001
            log.exception("реферальный бонус за клиента %s не начислен", client.get("id"))
    rental = await crm.rental(rental_id)
    if bot is not None:
        await notify.rental_opened(bot, db, crm, client, rental)
        await billing.tell_promos(bot, db, crm, applied)
    lines = [f"✅ <b>Выдача оформлена</b>: № {_esc(bike['code'])} у "
             f"{_esc(client['full_name'])}",
             f"Тариф: {_esc(tariff['name'])}, оплачено до "
             f"{_dm(rental.get('billed_until')) if rental else '—'}"]
    if data["pay"] > 0:
        lines.append(f"Принято: {_money(data['pay'])}, "
                     f"{logic.METHODS.get(data['method'], data['method']).lower()}")
    else:
        lines.append("Без оплаты: первый период — долгом на балансе")
    for got in applied:
        lines.append(f"Акция «{_esc(got['promo']['title'])}»: {_money(got['amount'])} баллами")
    lines += [f"⚠️ {_esc(n)}" for n in notes]
    if panel_url:
        lines.append(f"Документы на подпись: {panel_url}issue/docs?rental={rental_id}")
    return "\n".join(lines)
