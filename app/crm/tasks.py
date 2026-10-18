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

Страница делится на три вида задач (записи владельца): задачи владельца
(его поручения себе), задачи админов и мастеров по точкам (у владельца -
`points_board`, у сотрудника - его поручения и рабочие группы его точки)
и общие - незакреплённые, их берёт свободный.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, urlencode

from . import logic

STATUSES = {"open": "открыта", "done": "сделана", "cancelled": "отменена"}
TITLE_MAX = 300
NOTE_MAX = 2000
# Куда кнопки задачи возвращают: только свои страницы (адрес из формы -
# чужая строка), фильтр точки и блок страницы, где нажали, переносятся.
BACKS = ("/my", "/tasks")
ANCHORS = ("owner", "points", "point", "common")
NO_POINT = "none"


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


def plural(n: int, one: str, few: str, many: str) -> str:
    """«1 задача», «2 задачи», «5 задач»."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        word = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"


def counts_text(open_count: int, hot: int, people: int | None = None) -> str:
    """Счётчики группы словами: «2 сотрудника · 3 задачи · горит 1»."""
    bits = [] if people is None else [plural(people, "сотрудник", "сотрудника",
                                             "сотрудников")]
    bits.append(plural(open_count, "задача", "задачи", "задач") if open_count
                else "задач нет")
    if hot:
        bits.append(f"горит {hot}")
    return " · ".join(bits)


def _sees(place: str, where: Any) -> bool:
    """Незакреплённую задачу точки `place` видит она сама, задачу без точки -
    все. Люди «Без точки» видят все незакреплённые - как в своих «Задачах
    дня» (`my_lists`), поэтому и переключатель на них их не режет."""
    if not place or place == NO_POINT:
        return True
    return not where or where == place


def points_board(tasks: Iterable[Mapping[str, Any]], people: Iterable[Mapping[str, Any]], *,
                 staff: Mapping[str, Any], today: date, points: Iterable[str] = (),
                 location: str = "") -> dict[str, Any]:
    """«Задачи дня» владельца без его собственных: поручения админов и
    мастеров по точкам и общие (незакреплённые).

    Группа - точка СОТРУДНИКА (`staff.location`), а не задачи: у точки её
    люди со всеми их открытыми поручениями. Порядок - справочник точек
    (`points`), потом точки, которых в нём нет, последней - «Без точки»
    (сотрудник без точки и исполнитель, которого отключили). Открытая
    точка без людей тоже видна - пустой: владелец видит, что поручать
    там некому. Сам смотрящий в группы не входит - его задачи в «Задачах
    владельца». `location` - переключатель (`none` - без точки): группа
    этой точки и общие задачи, которые видят её люди (её и всей сети; у
    «Без точки» - все); неизвестное значение - чужая строка, показывается
    всё. `place_title` - что выбрано словами для подзаголовка, `point` -
    выбранная настоящая точка (у «Без точки» и «Все точки» пусто)."""
    me = _id(staff.get("id"))
    view = overview(tasks, people, staff=staff, today=today)
    cols = [c for c in view["people"] if _id(c["id"]) != me]
    names: list[str] = []
    for name in points:
        if name and name not in names:
            names.append(name)
    for c in cols:
        if c.get("location") and c["location"] not in names:
            names.append(c["location"])
    groups = []
    for name in (*names, None):
        members = [c for c in cols if (c.get("location") or None) == name]
        if name is None and not members:
            continue
        opened = sum(len(c["tasks"]) for c in members)
        hot = sum(c["hot"] for c in members)
        for c in members:
            c["counts"] = counts_text(len(c["tasks"]), c["hot"])
        groups.append({"key": name or NO_POINT, "title": name or "Без точки",
                       "people": members, "open": opened, "hot": hot,
                       "counts": counts_text(opened, hot, len(members))})
    titles = {g["key"]: g["title"] for g in groups}
    place = location if location in titles else ""
    return {"points": [g for g in groups if not place or g["key"] == place],
            "choices": list(titles.items()), "place": place,
            "place_title": titles.get(place, ""),
            "point": place if place != NO_POINT else "",
            "free": [t for t in view["free"] if _sees(place, t.get("location"))]}


def back_url(path: str, place: str = "", anchor: str = "") -> str:
    url = f"{path}?{urlencode({'location': place})}" if place else path
    return f"{url}#{anchor}" if anchor in ANCHORS else url


def safe_back(raw: str | None) -> str:
    """Возврат после «Взять», «Сделано» и новой задачи: /my или /tasks,
    фильтр точки и якорь блока (только из `ANCHORS`) собираются заново,
    остальное отбрасывается."""
    rest, _, anchor = (raw or "").partition("#")
    path, _, query = rest.partition("?")
    if path not in BACKS:
        return BACKS[0]
    place = (parse_qs(query).get("location") or [""])[0].strip()[:200]
    return back_url(path, place, anchor)
