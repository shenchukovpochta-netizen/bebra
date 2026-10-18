"""Команда: план месяца сотрудника, его факт и расчёт зарплаты.

Владельцу - кнопка «Команда» (/team): кто сколько выдал, принял денег,
закрыл нарядов и задач за месяц против своего плана, и сколько ему
начислено по его условиям. У сотрудника - карточка (/team/<id>) с тем же
по дням месяца и списками сделанного; себя он видит в «Задачах дня».

Факт не хранится, он считается из своих таблиц (db.team_facts):

- выдачи - аренды, заведённые им (`rentals.created_by`);
- принял денег - платежи журнала, которые он записал (`ledger`, вид
  `payment`): банк и автозачисление не его заслуга;
- наряды - закрытые, где он техник; работа - сумма наряда без запчастей;
- задачи - сделанные им поручения (`tasks.done_by`) и их цена;
- «принёс денег» - принятые платежи плюс клиентские наряды: оплата
  чужого ремонта в журнал не идёт, но это тоже деньги компании.

Зарплата = оклад + ставка × выдачи + % от работы нарядов + % от
принесённых денег + цена сделанных задач. Условия и план - строка на
месяц (`crm.staff_plans`): месяц без строки живёт по последней прежней.
Это расчёт, а не запись: в журнал денег клиентов зарплата не идёт.

План на неделю (с понедельника) и на день - те же четыре числа без
условий (`crm.staff_period_plans`). Своего нет - доля плана месяца по
дням (`month_share`): каждый день берёт 1/N плана своего месяца, поэтому
неделя на стыке месяцев складывается из двух планов. Своё число главнее
выведенного, пустое поле своего плана - тоже доля месяца, ноль - «не
планируется»; свой план - целые штуки (доля бывает дробной: 1,3 в день).
Факт недели и дня - тот же db.team_facts по датам окна.

Рубли плана и условия оплаты - владельцу или с «Финансами»: без этого
права форма их не показывает, а сохранение берёт прежние из базы.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from . import logic

# Что планируется: ключ плана, ключ факта, подпись, деньги ли.
METRICS: tuple[tuple[str, str, str, bool], ...] = (
    ("plan_issues", "issues", "Выдачи", False),
    ("plan_orders", "orders", "Наряды", False),
    ("plan_tasks", "tasks", "Задачи", False),
    ("plan_revenue", "revenue", "Принёс денег", True),
)
TERMS = ("salary_base", "per_issue", "order_pct", "revenue_pct")
PERIOD_FIELDS = tuple(m[0] for m in METRICS)
PLAN_FIELDS = PERIOD_FIELDS + TERMS
COUNT_FIELDS = tuple(m[0] for m in METRICS if not m[3])
# Рубли плана и условия оплаты: без права на деньги их нет ни в форме,
# ни в сохранении (form_keys, keep_money).
MONEY_FIELDS = tuple(m[0] for m in METRICS if m[3]) + TERMS
ZERO = Decimal("0.00")

# Период «Команды»: месяц (с условиями оплаты), неделя и день (только план).
PERIODS: tuple[tuple[str, str], ...] = (("month", "Месяц"), ("week", "Неделя"),
                                        ("day", "День"))
PERIOD_DAYS = {"week": 7, "day": 1}
# Вперёд неделю и день планируют не дальше этого: дальше - опечатка в адресе.
PLAN_AHEAD_DAYS = 56
# «Аналитика помесячно» в карточке сотрудника.
HISTORY_MONTHS = 6

MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
          "сентябрь", "октябрь", "ноябрь", "декабрь")
MONTHS_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря")
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота",
            "воскресенье")


def month_name(first: date) -> str:
    return f"{MONTHS[first.month - 1]} {first.year}"


def period_kind(raw: Any) -> str:
    """Период из адреса: неделя, день, всё прочее - месяц."""
    return raw if raw in PERIOD_DAYS else "month"


def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def period_start(kind: str, raw: Any, *, today: date) -> date:
    """Начало недели (понедельник) или день из адреса («2026-10-14»).
    Чужое, древнее (стрелка «назад» от 0001-01-01 - переполнение даты) и
    слишком далёкое будущее - текущие неделя или день."""
    try:
        got = date.fromisoformat(str(raw or "").strip())
    except ValueError:
        got = today
    if got > today + timedelta(days=PLAN_AHEAD_DAYS) or got.year < 2000:
        got = today
    return monday(got) if kind == "week" else got


def period_title(kind: str, start: date, *, today: date) -> str:
    """«четверг, 15 октября», «12–18 октября»; год - когда он не текущий.
    Неделя через Новый год - год у обеих дат: 3 января уже другого года."""
    if kind == "day":
        year = f" {start.year}" if start.year != today.year else ""
        return f"{WEEKDAYS[start.weekday()]}, {start.day} {MONTHS_GEN[start.month - 1]}{year}"
    last = start + timedelta(days=6)
    if last.year != start.year:
        return (f"{start.day} {MONTHS_GEN[start.month - 1]} {start.year} – "
                f"{last.day} {MONTHS_GEN[last.month - 1]} {last.year}")
    year = f" {last.year}" if last.year != today.year else ""
    if last.month == start.month:
        return f"{start.day}–{last.day} {MONTHS_GEN[last.month - 1]}{year}"
    return (f"{start.day} {MONTHS_GEN[start.month - 1]} – "
            f"{last.day} {MONTHS_GEN[last.month - 1]}{year}")


def period_span(kind: str, start: date, *, today: date) -> dict[str, Any]:
    """Неделя или день: границы, сколько дней прошло (для темпа), соседи.
    Будущее открыто на PLAN_AHEAD_DAYS - план ставят заранее."""
    days = PERIOD_DAYS[kind]
    last = start + timedelta(days=days - 1)
    nxt = start + timedelta(days=days)
    return {"kind": kind, "start": start, "last": last, "end": nxt, "days": days,
            "passed": 0 if today < start else min((today - start).days + 1, days),
            "is_current": start <= today <= last, "future": start > today,
            "prev": start - timedelta(days=days),
            "next": nxt if nxt <= today + timedelta(days=PLAN_AHEAD_DAYS) else None,
            "key": start.isoformat(), "title": period_title(kind, start, today=today)}


def period_months(kind: str, start: date) -> list[date]:
    """Месяцы, чьи дни входят в неделю или день (на стыке - два)."""
    return sorted({(start + timedelta(days=i)).replace(day=1)
                   for i in range(PERIOD_DAYS[kind])})


def month_of(raw: Any, *, today: date) -> date:
    """Месяц из адреса («2026-10»); чужое, будущее и древнее (полгода
    аналитики назад от 0001 года - переполнение даты) - текущий месяц."""
    current = today.replace(day=1)
    try:
        year, month = str(raw or "").split("-")
        got = date(int(year), int(month), 1)
    except (ValueError, TypeError):
        return current
    return got if date(2000, 1, 1) <= got <= current else current


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value is not None else 0))
    except InvalidOperation:
        return Decimal(0)


def revenue(facts: Mapping[str, Any]) -> Decimal:
    return logic.to_money(_dec(facts.get("payments")) + _dec(facts.get("client_orders_total")))


def salary(terms: Mapping[str, Any] | None, facts: Mapping[str, Any]) -> dict[str, Any]:
    """Начислено по условиям месяца: разбивка и итог, в рублях с копейками."""
    terms = terms or {}
    base = logic.to_money(_dec(terms.get("salary_base")))
    issues = logic.to_money(_dec(terms.get("per_issue")) * int(facts.get("issues") or 0))
    orders = logic.to_money(_dec(facts.get("orders_works")) * _dec(terms.get("order_pct"))
                            / 100)
    money = logic.to_money(revenue(facts) * _dec(terms.get("revenue_pct")) / 100)
    tasks = logic.to_money(_dec(facts.get("tasks_pay")))
    total = base + issues + orders + money + tasks
    return {"base": base, "issues": issues, "orders": orders, "revenue": money,
            "tasks": tasks, "total": logic.to_money(total)}


def _count(value: Decimal, whole: bool) -> int | Decimal:
    """Штуки: целый план - целыми, доля плана месяца (1,3 выдачи в день) -
    до десятых: «план 1» при 1,3 врал бы на треть."""
    if whole:
        return int(value.quantize(Decimal(1), ROUND_HALF_UP))
    got = value.quantize(Decimal("0.1"), ROUND_HALF_UP)
    return int(got) if got == got.to_integral_value() else got


def number(value: Any, is_money: bool = False) -> str:
    """Число для глаз: деньги - logic.money, штуки - с запятой («1,3»)."""
    if value is None:
        return "—"
    return logic.money(value) if is_money else str(value).replace(".", ",")


def progress(plan: Mapping[str, Any] | None, facts: Mapping[str, Any], *,
             days: int, passed: int, open_day: bool = False) -> list[dict[str, Any]]:
    """Факт против плана по каждой метрике: процент и отставание от ровного
    темпа. Метрика без плана - просто число.

    Период ещё не начался (passed = 0) - отставания нет. `open_day` - идущий
    день: несделанное к вечеру - ещё не отставание, сделанное - «в темпе»."""
    plan = plan or {}
    sources = plan.get("sources") or {}
    days = max(int(days), 1)
    passed = min(max(int(passed), 0), days)
    out = []
    for plan_key, fact_key, title, is_money in METRICS:
        fact = revenue(facts) if fact_key == "revenue" else _dec(facts.get(fact_key))
        target = plan.get(plan_key)
        shown = fact if is_money else int(fact)
        row: dict[str, Any] = {"key": fact_key, "title": title, "money": is_money,
                               "fact": shown, "fact_text": number(shown, is_money),
                               "plan": None, "plan_text": None, "percent": None,
                               "pace": None, "pace_text": None, "ahead": None,
                               "source": None}
        if target is not None and _dec(target) > 0:
            goal = _dec(target)
            whole = goal == goal.to_integral_value()
            pace = goal * passed / days
            if passed == 0 or (open_day and fact < goal):
                ahead = None
            else:
                ahead = fact >= pace
            goal_shown = logic.to_money(goal) if is_money else _count(goal, whole)
            pace_shown = logic.to_money(pace) if is_money else _count(pace, whole)
            row.update(plan=goal_shown, plan_text=number(goal_shown, is_money),
                       percent=int((fact * 100 / goal).quantize(Decimal(1), ROUND_HALF_UP)),
                       pace=pace_shown, pace_text=number(pace_shown, is_money),
                       ahead=ahead, source=sources.get(plan_key))
        out.append(row)
    return out


def plan_source(metrics: Iterable[Mapping[str, Any]]) -> str | None:
    """Откуда план периода: свой, из плана месяца или вперемешку."""
    got = {m["source"] for m in metrics if m.get("source")}
    if not got:
        return None
    return got.pop() if len(got) == 1 else "mixed"


def rows(people: Iterable[Mapping[str, Any]], facts: Mapping[int, Mapping[str, Any]],
         plans: Mapping[int, Mapping[str, Any]], *, days: int,
         passed: int, open_day: bool = False) -> list[dict[str, Any]]:
    """Строки «Команды»: действующие сотрудники и отключённые с фактом
    в этом периоде (зарплату за отработанное считать всё равно надо)."""
    out = []
    for p in people:
        sid = int(p["id"])
        f = facts.get(sid) or {}
        busy = any(f.get(k) for k in ("issues", "payments", "orders", "tasks"))
        if not p.get("active", True) and not busy:
            continue
        plan = plans.get(sid)
        metrics = progress(plan, f, days=days, passed=passed, open_day=open_day)
        planned = [m for m in metrics if m["percent"] is not None]
        out.append({
            "id": sid, "name": p.get("name") or p.get("login"), "role": logic.role_title(p),
            "location": p.get("location"), "active": p.get("active", True),
            "facts": f, "metrics": {m["key"]: m for m in metrics},
            "percent": (round(sum(m["percent"] for m in planned) / len(planned))
                        if planned else None),
            "behind": any(m["ahead"] is False for m in planned),
            # Зелёный - только когда в темпе всё запланированное: один
            # сделанный показатель при нуле денег - не «всё в порядке».
            "ahead": bool(planned) and all(m["ahead"] is True for m in planned),
            "plan": plan, "pay": salary(plan, f), "revenue": revenue(f),
            "source": plan_source(planned),
            "inherited": bool(plan) and plan.get("own") is False})
    out.sort(key=lambda r: (-(r["percent"] or -1), str(r["name"])))
    return out


def month_share(month_plans: Mapping[date, Mapping[str, Any] | None], start: date,
                days: int) -> dict[str, Any]:
    """План недели или дня из плана месяца по доле дней: каждый день
    периода берёт 1/N плана своего месяца (N - дней в нём). Неделя на стыке
    месяцев - по датам, а не по месяцу начала. Показатель, не планируемый
    ни в одном из месяцев, - None. Деньги - до рубля, штуки - до десятых."""
    out: dict[str, Any] = {}
    for key, _fact, _title, is_money in METRICS:
        total, seen = Decimal(0), False
        for i in range(days):
            day = start + timedelta(days=i)
            value = (month_plans.get(day.replace(day=1)) or {}).get(key)
            if value is None:
                continue
            seen = True
            total += _dec(value) / monthrange(day.year, day.month)[1]
        if not seen:
            out[key] = None
        elif is_money:
            out[key] = logic.to_money(total.quantize(Decimal(1), ROUND_HALF_UP))
        else:
            out[key] = total.quantize(Decimal("0.1"), ROUND_HALF_UP)
    return out


def period_plan(own: Mapping[str, Any] | None,
                derived: Mapping[str, Any]) -> dict[str, Any] | None:
    """Свой план периода главнее выведенного - по каждому показателю:
    пустое поле своего плана берёт долю месяца. sources - откуда число."""
    plan: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for key in PERIOD_FIELDS:
        mine = (own or {}).get(key)
        if mine is not None:
            plan[key], sources[key] = mine, "own"
        elif derived.get(key) is not None:
            plan[key], sources[key] = derived[key], "month"
        else:
            plan[key] = None
    if not sources:
        return None
    plan["sources"] = sources
    return plan


def period_plans(kind: str, start: date, own: Mapping[int, Mapping[str, Any]],
                 month_plans: Mapping[date, Mapping[int, Mapping[str, Any]]]
                 ) -> dict[int, dict[str, Any]]:
    """Планы недели или дня всей команды: свои строки (db.staff_period_plans)
    поверх доли планов месяцев (db.staff_plans каждого месяца периода)."""
    months = period_months(kind, start)
    people = set(own)
    for first in months:
        people |= set(month_plans.get(first) or {})
    out = {}
    for sid in people:
        derived = month_share({m: (month_plans.get(m) or {}).get(sid) for m in months},
                              start, PERIOD_DAYS[kind])
        plan = period_plan(own.get(sid), derived)
        if plan:
            out[sid] = plan
    return out


def history_months(first: date, count: int = HISTORY_MONTHS) -> list[date]:
    """Первые числа `count` месяцев по `first` включительно, от раннего."""
    out = [first]
    while len(out) < count:
        out.insert(0, (out[0] - timedelta(days=1)).replace(day=1))
    return out


def history(months: Iterable[tuple[date, Mapping[str, Any] | None, Mapping[str, Any]]],
            *, today: date) -> list[dict[str, Any]]:
    """Аналитика сотрудника помесячно: план, факт и % по каждому показателю
    и начисленное. Месяцы до первого с планом или фактом не показываются
    (сотрудника тогда не было); новые сверху."""
    out: list[dict[str, Any]] = []
    for first, plan, facts in sorted(months, key=lambda m: m[0]):
        busy = (any(facts.get(k) for k in ("issues", "payments", "orders", "tasks"))
                or any((plan or {}).get(k) for k in PLAN_FIELDS))
        if not out and not busy:
            continue
        span = logic.month_bounds(first, today=today)
        metrics = progress(plan, facts, days=span["days"], passed=span["passed"])
        out.append({"first": first, "key": span["key"], "title": month_name(first),
                    "current": span["is_current"], "metrics": {m["key"]: m for m in metrics},
                    "pay": salary(plan, facts), "has_plan": bool(plan),
                    "inherited": bool(plan) and plan.get("own") is False})
    return out[::-1]


def totals(team: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    team = list(team)
    return {"issues": sum(int(r["facts"].get("issues") or 0) for r in team),
            "orders": sum(int(r["facts"].get("orders") or 0) for r in team),
            "tasks": sum(int(r["facts"].get("tasks") or 0) for r in team),
            "revenue": logic.to_money(sum((r["revenue"] for r in team), ZERO)),
            "pay": logic.to_money(sum((r["pay"]["total"] for r in team), ZERO))}


def form_keys(kind: str, money_ok: bool) -> tuple[str, ...]:
    """Поля формы плана: месяц - план и условия, неделя и день - план.
    Без права на деньги - только штуки."""
    keys = PLAN_FIELDS if kind == "month" else PERIOD_FIELDS
    return keys if money_ok else tuple(k for k in keys if k not in MONEY_FIELDS)


def keep_money(kind: str, fields: Mapping[str, Any],
               saved: Mapping[str, Any] | None) -> dict[str, Any]:
    """Форма без права на деньги рублей не несёт, а запись пишет строку
    целиком: деньги плана и условия - прежние из базы (`saved` - действующая
    строка: своя или унаследованная у месяца, своя у недели и дня), иначе
    сохранение штук молча стёрло бы денежный план и оклад."""
    out = dict(fields)
    for key in PLAN_FIELDS if kind == "month" else PERIOD_FIELDS:
        if key in MONEY_FIELDS:
            got = (saved or {}).get(key)
            out[key] = ZERO if got is None and key in TERMS else got
    return out


def parse_plan(data: Mapping[str, str], keys: tuple[str, ...] = PLAN_FIELDS
               ) -> tuple[dict[str, Any] | None, str | None]:
    """План и условия из формы. Пустой план месяца - «не планируется»,
    недели и дня (keys=PERIOD_FIELDS) - «как в плане месяца»; пустое
    условие - ноль. Штуки - целыми: «1,3» не усекается до 1 молча."""
    what = "плана и условий" if set(keys) & set(TERMS) else "плана"
    out: dict[str, Any] = {}
    for key in keys:
        raw = (data.get(key) or "").replace(" ", "").replace(" ", "").replace(",", ".")
        if not raw:
            out[key] = None if key.startswith("plan_") else ZERO
            continue
        try:
            value = Decimal(raw)
        except InvalidOperation:
            return None, f"Числа {what} - цифрами."
        if not value.is_finite() or value < 0:
            return None, f"Числа {what} - не меньше нуля."
        if key.endswith("_pct") and value > 100:
            return None, "Процент - от 0 до 100."
        if value > Decimal("100000000"):
            return None, "Слишком большое число."
        if key in COUNT_FIELDS:
            if value != value.to_integral_value():
                return None, "Выдачи, наряды и задачи - целым числом."
            out[key] = int(value)
        else:
            out[key] = value.quantize(Decimal("0.01"))
    return out, None
