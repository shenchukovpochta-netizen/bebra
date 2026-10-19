"""Задачи дня: поручения людям, которых нет в базе как событий.

«Задачи дня» (/my) собирают сами то, что система знает: наряды, просрочки,
заявки на выдачу, тревоги (app/crm/mytasks.py). Здесь - остальное, что
владелец или администратор поручает словами: «закупить запчасти»,
«напомнить о продлении», «ремонт АКБ». Задача:

- закреплена за сотрудником или незакреплена - тогда её видит точка
  (`location`; без точки - вся сеть), а берёт тот, кто свободен;
- две даты словами владельца: «Дата постановки» (`start_on`, по
  умолчанию сегодня) - с неё задача в «Задачах дня», до неё она только
  в «Запланированных» доски и у автора; «Дата выполнения» (`due_on`,
  необязательна) - по ней «просрочена», просроченная горит;
- сделана - кем (`done_by`): по нему расчёт зарплаты (app/crm/team.py)
  считает сделанные задачи и оплату за них (`pay`).

Ставит задачу любой сотрудник любому (себе, другому, незакреплённой) -
это разговор команды, а не право раздела. Оплату исполнителю за задачу
ставит и видит только тот, кто ведёт команду (владелец или право на
«Сотрудников»): она уходит в зарплату. Закрыть может исполнитель, автор и
руководитель; незакреплённую - любой, и тогда сделал он.

Страница делится на три вида задач (записи владельца): задачи владельца
(его поручения себе), задачи админов и мастеров по точкам (у владельца -
`points_board`: рабочие задачи точки и её люди с поручениями, у
сотрудника - его поручения и рабочие группы его точки) и общие -
незакреплённые, их берёт свободный, а у владельца ещё рабочие без точки.
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
ANCHORS = ("owner", "points", "point", "common", "planned")
NO_POINT = "none"
# Окно, в которое бот говорит исполнителю о задаче, чей день постановки
# настал (notify.announce_tasks): с утра до вечера, местные часы. Задача
# «с завтра» не будит в полночь, а поднятый ночью бот ждёт утра.
ANNOUNCE_HOURS = (9, 21)
# Строк владельца на группу точки: точек несколько, весь список - в разделе.
BOARD_GROUP_LIMIT = 5


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


def planned(task: Mapping[str, Any], *, today: date) -> bool:
    """Открытая задача с датой постановки впереди: в «Задачах дня» её ещё
    нет, она в «Запланированных» доски и у автора."""
    start = task.get("start_on")
    return task.get("status") == "open" and isinstance(start, date) and start > today


def start_label(task: Mapping[str, Any], *, today: date) -> str:
    """Когда запланированная появится: «с завтра», «с 25.10»."""
    if not planned(task, today=today):
        return ""
    start = task["start_on"]
    return "с завтра" if start == today + timedelta(days=1) else f"с {start:%d.%m}"


def due_label(task: Mapping[str, Any], *, today: date) -> tuple[str, bool]:
    """Дата выполнения словами и горит ли: «сегодня», «завтра»,
    «просрочена на 2 дн.»."""
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


def _day(raw: Any, what: str) -> tuple[date | None, str | None]:
    text = str(raw or "").strip()
    if not text:
        return None, None
    try:
        return date.fromisoformat(text), None
    except ValueError:
        return None, f"{what} - дата."


def parse_form(data: Mapping[str, str], *, staff: Mapping[str, Any], today: date,
               task: Mapping[str, Any] | None = None
               ) -> tuple[dict[str, Any] | None, str | None]:
    """Поля задачи из формы: (поля, None) или (None, что не так).

    «Дата постановки» пустая - сегодня; «Дата выполнения» необязательна и
    не раньше постановки. Прошедшая дата не принимается (вчерашняя - да:
    форму открыли до полуночи), кроме той, что в правке (`task`) не
    меняли: иначе просроченную задачу нельзя было бы даже переименовать.
    """
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
    old = task or {}
    start, problem = _day(data.get("start_on"), "Дата постановки")
    if problem:
        return None, problem
    start = start or today
    start_kept = start == old.get("start_on")
    if start < today - timedelta(days=1) and not start_kept:
        return None, "Дата постановки уже прошла: поставьте сегодня или позже."
    due, problem = _day(data.get("due_on"), "Дата выполнения")
    if problem:
        return None, problem
    if due is not None:
        due_kept = due == old.get("due_on")
        if due < today - timedelta(days=1) and not due_kept:
            return None, "Дата выполнения уже прошла: поставьте сегодня или позже."
        if due < start and not (due_kept and start_kept):
            return None, "Дата выполнения раньше даты постановки."
    fields["start_on"], fields["due_on"] = start, due
    if manages(staff):
        raw_pay = (data.get("pay") or "").replace(" ", "").replace(",", ".")
        if raw_pay:
            try:
                pay = Decimal(raw_pay)
            except InvalidOperation:
                return None, "Оплата исполнителю - число рублей."
            if not pay.is_finite() or pay < 0 or pay > Decimal("1000000"):
                return None, "Оплата исполнителю - от нуля до миллиона рублей."
            fields["pay"] = pay.quantize(Decimal("0.01"))
        else:
            fields["pay"] = None
    return fields, None


def assignees(people: Iterable[Mapping[str, Any]], *, staff: Mapping[str, Any],
              task: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """«Кому» в форме задачи: все действующие сотрудники, кроме себя (себе -
    «мне»), без фильтра по точке и роли - ставят задачи все всем. Подпись -
    имя, роль и точка; по точке, потом по имени. Срок доступа истёк - не в
    списке: задачу он не увидит. Исполнитель правимой задачи (`task`)
    остаётся, даже отключённый: иначе сохранение молча сняло бы его."""
    me = _id(staff.get("id"))
    out = []
    for p in people:
        pid = _id(p.get("id"))
        if pid is None or pid == me or logic.staff_expired(p):
            continue
        out.append({"id": pid, "where": p.get("location") or "",
                    "label": " · ".join(str(x) for x in (
                        p.get("name") or p.get("login"), logic.role_title(dict(p)),
                        p.get("location")) if x)})
    out.sort(key=lambda o: (o["where"], o["label"].lower()))
    current = _id((task or {}).get("assignee_id"))
    if current is not None and current != me and all(o["id"] != current for o in out):
        out.append({"id": current, "where": "",
                    "label": f"{(task or {}).get('assignee_name') or '—'} · нет доступа"})
    return out


def _mine(point: str | None, where: Any) -> bool:
    return point is None or where is None or where == point


def dress(task: Mapping[str, Any], *, staff: Mapping[str, Any], today: date) -> dict:
    t = dict(task)
    t["due_text"], t["hot"] = due_label(task, today=today)
    t["planned"] = planned(task, today=today)
    t["start_text"] = start_label(task, today=today)
    t["can_finish"] = may_finish(staff, task)
    t["can_take"] = may_take(staff, task)
    t["can_change"] = may_change(staff, task)
    if not money_ok(staff):
        t["pay"] = None
    return t


def _order(t: Mapping[str, Any]) -> tuple:
    due = t.get("due_on")
    return (not t.get("hot"), due if isinstance(due, date) else date.max, t.get("id") or 0)


def _start_order(t: Mapping[str, Any]) -> tuple:
    start = t.get("start_on")
    return (start if isinstance(start, date) else date.max, t.get("id") or 0)


def my_lists(staff: Mapping[str, Any], tasks: Iterable[Mapping[str, Any]], *,
             today: date) -> dict[str, list[dict]]:
    """«Задачи дня» сотрудника: свои открытые и незакреплённые его точки,
    горящие и ранние сроки сверху. Задача с датой постановки впереди -
    не здесь, а в `planned` у её автора (кому бы её ни поставили)."""
    me = _id(staff.get("id"))
    point = staff.get("location") or None
    mine, free, later = [], [], []
    for task in tasks:
        if task.get("status") != "open":
            continue
        t = dress(task, staff=staff, today=today)
        if t["planned"]:
            if me is not None and _id(t.get("created_by_id")) == me:
                later.append(t)
            continue
        if _id(t.get("assignee_id")) == me:
            mine.append(t)
        elif t.get("assignee_id") is None and _mine(point, t.get("location")):
            free.append(t)
    mine.sort(key=_order)
    free.sort(key=_order)
    later.sort(key=_start_order)
    return {"mine": mine, "free": free, "planned": later}


def overview(tasks: Iterable[Mapping[str, Any]], people: Iterable[Mapping[str, Any]], *,
             staff: Mapping[str, Any], today: date, location: str = "") -> dict[str, Any]:
    """Все открытые задачи: незакреплённые и по каждому сотруднику, с
    фильтром по точке. У сотрудника своя точка - он в ней; без точки -
    его задачи на любой. Запланированные (дата постановки впереди) -
    отдельно, `planned`: в колонках людей их ещё нет."""
    rows = [dress(t, staff=staff, today=today) for t in tasks if t.get("status") == "open"]
    if location == "none":
        rows = [t for t in rows if not t.get("location")]
    elif location:
        rows = [t for t in rows if t.get("location") in (None, location)]
    later = sorted((t for t in rows if t["planned"]), key=_start_order)
    rows = [t for t in rows if not t["planned"]]
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
            "hot": sum(1 for t in rows if t["hot"]), "planned": later}


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


def counts_text(open_count: int, hot: int, people: int | None = None,
                work: int | None = None) -> str:
    """Счётчики группы словами: «2 сотрудника · 3 задачи · горит 1». У
    точки владельца (`work` - её рабочие задачи) поручения названы
    поручениями: «2 сотрудника · 9 рабочих задач · 1 поручение · горит 5»;
    «горит» - и рабочие, и поручения."""
    bits = [] if people is None else [plural(people, "сотрудник", "сотрудника",
                                             "сотрудников")]
    if work is None:
        bits.append(plural(open_count, "задача", "задачи", "задач") if open_count
                    else "задач нет")
    else:
        if work:
            bits.append(plural(work, "рабочая задача", "рабочие задачи", "рабочих задач"))
        if open_count:
            bits.append(plural(open_count, "поручение", "поручения", "поручений"))
        if not work and not open_count:
            bits.append("задач нет")
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
                 location: str = "",
                 work: Mapping[str | None, list[dict]] | None = None) -> dict[str, Any]:
    """«Задачи дня» владельца без его собственных: задачи админов и
    мастеров по точкам и общие (незакреплённые).

    У точки - её рабочие задачи (`work`, mytasks.by_point: то, что видит
    её администратор и мастер, - прозвон, выдачи, наряды, тревоги) и её
    люди со всеми их открытыми поручениями. Группа людей - точка
    СОТРУДНИКА (`staff.location`), а не задачи. Порядок - справочник точек
    (`points`), потом точки, которых в нём нет, последней - «Без точки»
    (сотрудник без точки и исполнитель, которого отключили). Открытая
    точка без людей тоже видна - пустой: владелец видит, что поручать
    там некому. Сам смотрящий в группы не входит - его задачи в «Задачах
    владельца». Рабочие задачи без точки (`work[None]`) - в общих
    (`work_free`), при любом переключателе: их видят все точки.
    `location` - переключатель (`none` - без точки): группа этой точки и
    общие задачи, которые видят её люди (её и всей сети; у «Без точки» -
    все); неизвестное значение - чужая строка, показывается всё.
    `place_title` - что выбрано словами для подзаголовка, `point` -
    выбранная настоящая точка (у «Без точки» и «Все точки» пусто).
    Запланированных (дата постановки впереди) здесь нет - они на доске."""
    me = _id(staff.get("id"))
    work = work or {}
    view = overview(tasks, people, staff=staff, today=today)
    cols = [c for c in view["people"] if _id(c["id"]) != me]
    names: list[str] = []
    for name in (*points, *(c.get("location") for c in cols), *work):
        if name and name not in names:
            names.append(name)
    groups = []
    for name in (*names, None):
        members = [c for c in cols if (c.get("location") or None) == name]
        jobs = list(work.get(name) or []) if name else []
        if name is None and not members:
            continue
        opened = sum(len(c["tasks"]) for c in members)
        hot = sum(c["hot"] for c in members)
        busy = sum(int(g.get("count") or 0) for g in jobs)
        burning = sum(int(g.get("hot") or 0) for g in jobs)
        for c in members:
            c["counts"] = counts_text(len(c["tasks"]), c["hot"])
        groups.append({"key": name or NO_POINT, "title": name or "Без точки",
                       "people": members, "open": opened, "hot": hot + burning,
                       "work": jobs, "work_count": busy,
                       "counts": counts_text(opened, hot + burning, len(members),
                                             work=busy if work else None)})
    titles = {g["key"]: g["title"] for g in groups}
    place = location if location in titles else ""
    return {"points": [g for g in groups if not place or g["key"] == place],
            "choices": list(titles.items()), "place": place,
            "place_title": titles.get(place, ""),
            "point": place if place != NO_POINT else "",
            "free": [t for t in view["free"] if _sees(place, t.get("location"))],
            "work_free": list(work.get(None) or [])}


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
