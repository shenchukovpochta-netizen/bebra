"""Задачи дня: поручения людям, которых нет в базе как событий.

«Задачи дня» (/my) собирают сами то, что система знает: наряды, просрочки,
заявки на выдачу, тревоги (app/crm/mytasks.py). Здесь - остальное, что
владелец или администратор поручает словами: «закупить запчасти»,
«напомнить о продлении», «ремонт АКБ». Задача:

- закреплена за сотрудником или незакреплена - тогда её видит точка
  (`location`; без точки - вся сеть), а берёт тот, кто свободен;
- со сроком или без; просроченная горит;
- сделана - кем (`done_by`): по нему расчёт зарплаты (app/crm/team.py)
  считает сделанные задачи и их цену (`pay`).

Ставит задачу любой сотрудник - это разговор команды, а не право
раздела. Цену задачи ставит и видит только тот, кто ведёт команду
(владелец или право на «Сотрудников»): она уходит в зарплату. Закрыть
может исполнитель, автор и руководитель; незакреплённую - любой, и тогда
сделал он.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from . import logic

STATUSES = {"open": "открыта", "done": "сделана", "cancelled": "отменена"}
TITLE_MAX = 300
NOTE_MAX = 2000


def manages(staff: Mapping[str, Any] | None) -> bool:
    """Ведёт команду: ставит цену задач, правит и отменяет чужие."""
    return logic.is_owner(staff) or logic.can_edit(staff, "staff")


def money_ok(staff: Mapping[str, Any] | None) -> bool:
    return manages(staff) or logic.can_view(staff, "finance")


def _id(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def may_finish(staff: Mapping[str, Any], task: Mapping[str, Any]) -> bool:
    if task.get("status") != "open":
        return False
    me = _id(staff.get("id"))
    who = _id(task.get("assignee_id"))
    return (who is None or who == me or _id(task.get("created_by_id")) == me
            or manages(staff))


def may_change(staff: Mapping[str, Any], task: Mapping[str, Any]) -> bool:
    """Правка, отмена и возврат в работу: автор или руководитель."""
    return _id(task.get("created_by_id")) == _id(staff.get("id")) or manages(staff)


def may_take(staff: Mapping[str, Any], task: Mapping[str, Any]) -> bool:
    return task.get("status") == "open" and task.get("assignee_id") is None


def due_label(task: Mapping[str, Any], *, today: date) -> tuple[str, bool]:
    """Срок словами и горит ли: «сегодня», «завтра», «просрочена на 2 дн.»."""
    due = task.get("due_on")
    if task.get("status") != "open" or not isinstance(due, date):
        return ("", False)
    if due < today:
        return (f"просрочена на {(today - due).days} дн.", True)
    if due == today:
        return ("сегодня", True)
    if due == today + timedelta(days=1):
        return ("завтра", False)
    return (f"до {due:%d.%m}", False)


def parse_form(data: Mapping[str, str], *, staff: Mapping[str, Any],
               today: date) -> tuple[dict[str, Any] | None, str | None]:
    """Поля задачи из формы: (поля, None) или (None, что не так)."""
    title = " ".join((data.get("title") or "").split())[:TITLE_MAX]
    if not title:
        return None, "Напишите, что сделать."
    fields: dict[str, Any] = {
        "title": title,
        "note": (data.get("note") or "").strip()[:NOTE_MAX] or None,
        "location": (data.get("location") or "").strip() or None,
    }
    who = (data.get("assignee") or "").strip()
    if who == "me":
        fields["assignee_id"] = _id(staff.get("id"))
    elif who in ("", "none"):
        fields["assignee_id"] = None
    else:
        parsed = logic.parse_id(who)
        if parsed is None:
            return None, "Исполнитель не найден."
        fields["assignee_id"] = parsed
    raw_due = (data.get("due_on") or "").strip()
    if raw_due:
        try:
            due = date.fromisoformat(raw_due)
        except ValueError:
            return None, "Срок - дата."
        if due < today - timedelta(days=1):
            return None, "Срок уже прошёл: поставьте сегодня или позже."
        fields["due_on"] = due
    else:
        fields["due_on"] = None
    if manages(staff):
        raw_pay = (data.get("pay") or "").replace(" ", "").replace(",", ".")
        if raw_pay:
            try:
                pay = Decimal(raw_pay)
            except InvalidOperation:
                return None, "Цена задачи - число рублей."
            if pay < 0 or pay > Decimal("1000000") or not pay.is_finite():
                return None, "Цена задачи - от нуля до миллиона."
            fields["pay"] = pay.quantize(Decimal("0.01"))
        else:
            fields["pay"] = None
    return fields, None


def _mine(point: str | None, where: Any) -> bool:
    return point is None or where is None or where == point


def dress(task: Mapping[str, Any], *, staff: Mapping[str, Any], today: date) -> dict:
    t = dict(task)
    t["due_text"], t["hot"] = due_label(task, today=today)
    t["can_finish"] = may_finish(staff, task)
    t["can_take"] = may_take(staff, task)
    t["can_change"] = may_change(staff, task)
    if not money_ok(staff):
        t["pay"] = None
    return t


def _order(t: Mapping[str, Any]) -> tuple:
    due = t.get("due_on")
    return (not t.get("hot"), due if isinstance(due, date) else date.max, t.get("id") or 0)


def my_lists(staff: Mapping[str, Any], tasks: Iterable[Mapping[str, Any]], *,
             today: date) -> dict[str, list[dict]]:
    """«Задачи дня» сотрудника: свои открытые и незакреплённые его точки,
    горящие и ранние сроки сверху."""
    me = _id(staff.get("id"))
    point = staff.get("location") or None
    mine, free = [], []
    for task in tasks:
        if task.get("status") != "open":
            continue
        t = dress(task, staff=staff, today=today)
        if _id(t.get("assignee_id")) == me:
            mine.append(t)
        elif t.get("assignee_id") is None and _mine(point, t.get("location")):
            free.append(t)
    mine.sort(key=_order)
    free.sort(key=_order)
    return {"mine": mine, "free": free}


def overview(tasks: Iterable[Mapping[str, Any]], people: Iterable[Mapping[str, Any]], *,
             staff: Mapping[str, Any], today: date, location: str = "") -> dict[str, Any]:
    """Все открытые задачи: незакреплённые и по каждому сотруднику, с
    фильтром по точке. У сотрудника своя точка - он в ней; без точки -
    его задачи на любой."""
    rows = [dress(t, staff=staff, today=today) for t in tasks if t.get("status") == "open"]
    if location == "none":
        rows = [t for t in rows if not t.get("location")]
    elif location:
        rows = [t for t in rows if t.get("location") in (None, location)]
    rows.sort(key=_order)
    free = [t for t in rows if t.get("assignee_id") is None]
    by_person: dict[int, list[dict]] = {}
    for t in rows:
        if t.get("assignee_id") is not None:
            by_person.setdefault(int(t["assignee_id"]), []).append(t)
    cols = []
    for p in people:
        items = by_person.pop(int(p["id"]), [])
        if location and location != "none" and p.get("location") not in (None, location) \
                and not items:
            continue
        cols.append({"id": p["id"], "name": p.get("name") or p.get("login"),
                     "location": p.get("location"), "role": logic.role_title(p),
                     "tasks": items, "hot": sum(1 for t in items if t["hot"])})
    for pid, items in by_person.items():           # исполнитель отключён
        cols.append({"id": pid, "name": items[0].get("assignee_name") or "—",
                     "location": None, "role": "", "tasks": items,
                     "hot": sum(1 for t in items if t["hot"])})
    cols.sort(key=lambda c: (-len(c["tasks"]), str(c["name"])))
    return {"free": free, "people": cols, "open": len(rows),
            "hot": sum(1 for t in rows if t["hot"])}
