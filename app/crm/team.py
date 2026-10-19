"""Команда: план сотрудника по его роли, факт и расчёт зарплаты.

Владельцу - кнопка «Команда» (/team): таблицы по ролям («Мастера»,
«Администраторы»), у каждой свои показатели за месяц, неделю или день
против плана, и сколько начислено по условиям. У сотрудника - карточка
(/team/<id>) с тем же по показателям его роли и списками сделанного;
себя он видит в «Задачах дня».

Роль - по доступу (`roles_of`), а не по колонке staff.role: мастер меняет
«Сервис» (наряды), администратор точки - выдачу, аренды или кассу; оба
права - оба набора. Владелец исполнителем не считается: плана и зарплаты
у него нет, на странице - строкой «сам сделал» под таблицами.

Факт не хранится, он считается из своих таблиц (db.team_facts):

мастер
- ремонт парка - закрытые наряды, где он техник, по велосипеду парка;
- сторонний ремонт - закрытые наряды по чужой технике (без велосипеда);
- в срок - доля закрытых нарядов, уложившихся в срок ремонта
  (logic.order_norm: самый долгий срок строк, общий - у строк без своего);
- сумма работ - наряд без себестоимости (запчасти и работа), как в
  «Выработке техников», но не меньше нуля: свой ремонт без цены клиенту
  - ноль, а не минус себестоимость (иначе % от работ отнимал бы оклад);
- выручка ремонта - оплаченные клиентом наряды (paid_at): сторонний
  ремонт и ремонт арендатору за его счёт. В журнале этих денег нет;

администратор
- выдачи - аренды, заведённые им (`rentals.created_by`);
- продления - принятые им платежи по идущей аренде, начатой раньше дня
  платежа (платёж выдачи - не продление); два платежа одной аренды за
  день - одно продление;
- сдачи - аренды, которые он закрыл с возвратом техники (журнал статусов
  в момент закрытия: из «в аренде» в свободный, ремонт, ТО, бронь;
  потеря, выкуп и списание - не сдача);
- принял денег - платежи журнала, которые он записал (`ledger`, вид
  `payment`): банк и автозачисление не его заслуга;
- новые клиенты - карточки, заведённые им в панели (`clients.created_by`);

у всех - задачи: сделанные им поручения (`tasks.done_by`) и их цена.

Зарплата = оклад + цена сделанных задач + условия роли: мастеру % от
суммы работ, администратору ставка × выдачи и % от принятых денег.
Условие чужой роли не начисляется и в форме не показывается. Условия и
план - строка на месяц (`crm.staff_plans`): месяц без строки живёт по
последней прежней. Это расчёт, а не запись: в журнал денег клиентов
зарплата не идёт.

План на неделю (с понедельника) и на день - те же числа плана без
условий (`crm.staff_period_plans`). Своего нет - доля плана месяца по
дням (`month_share`): каждый день берёт 1/N плана своего месяца, поэтому
неделя на стыке месяцев складывается из двух планов. Своё число главнее
выведенного, пустое поле своего плана - тоже доля месяца, ноль - «не
планируется»; свой план - целые штуки (доля бывает дробной: 1,3 в день).
Факт недели и дня - тот же db.team_facts по датам окна.

Рубли плана и условия оплаты - владельцу или с «Финансами»: без этого
права форма их не показывает, а сохранение берёт прежние из базы. Числа
чужой роли форма тоже не несёт и сохранение их не трогает (`keep_saved`).
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, NamedTuple

from . import logic


class Metric(NamedTuple):
    """Показатель «Команды»: колонка плана (staff_plans, staff_period_plans),
    ключ факта (db.team_facts), подпись, деньги ли, роль (None - у всех)."""
    plan: str
    fact: str
    title: str
    money: bool
    role: str | None


# Роли «Команды» по порядку таблиц. Роль - по доступу (roles_of).
ROLES: tuple[tuple[str, str], ...] = (("master", "Мастера"), ("admin", "Администраторы"))
ROLE_ONE = {"master": "мастер", "admin": "администратор"}
# Администратор точки - тот, кто меняет выдачу, аренды или кассу.
ADMIN_SECTIONS = ("issue", "rentals", "cash")

METRICS: tuple[Metric, ...] = (
    Metric("plan_orders_own", "orders_own", "Ремонт парка", False, "master"),
    Metric("plan_orders_ext", "orders_ext", "Сторонний ремонт", False, "master"),
    Metric("plan_works", "orders_works", "Сумма работ", True, "master"),
    Metric("plan_repair_revenue", "repair_revenue", "Выручка ремонта", True, "master"),
    Metric("plan_issues", "issues", "Выдачи", False, "admin"),
    Metric("plan_extensions", "extensions", "Продления", False, "admin"),
    Metric("plan_returns", "returns", "Сдачи", False, "admin"),
    Metric("plan_clients", "new_clients", "Новые клиенты", False, "admin"),
    Metric("plan_revenue", "payments", "Принял денег", True, "admin"),
    Metric("plan_tasks", "tasks", "Задачи", False, None),
)
BY_FACT = {m.fact: m for m in METRICS}
# Условия оплаты и чья это роль: оклад - всем, ставка за выдачу и % денег -
# администратору, % от суммы работ - мастеру.
TERMS = ("salary_base", "per_issue", "order_pct", "revenue_pct")
TERM_ROLE: dict[str, str | None] = {"salary_base": None, "per_issue": "admin",
                                    "order_pct": "master", "revenue_pct": "admin"}
TERM_TITLES = {"salary_base": "Оклад, ₽", "per_issue": "За выдачу, ₽",
               "order_pct": "% от суммы работ", "revenue_pct": "% от принятых денег"}
PERIOD_FIELDS = tuple(m.plan for m in METRICS)
PLAN_FIELDS = PERIOD_FIELDS + TERMS
COUNT_FIELDS = tuple(m.plan for m in METRICS if not m.money)
# Рубли плана и условия оплаты: без права на деньги их нет ни в форме,
# ни в сохранении (form_keys, keep_saved).
MONEY_FIELDS = tuple(m.plan for m in METRICS if m.money) + TERMS
# Что считается «сотрудник работал в периоде» (отключённый с фактом
# остаётся в таблице, месяц аналитики не пропускается).
BUSY_FACTS = ("orders", "issues", "extensions", "returns", "payments", "new_clients",
              "repair_revenue", "tasks")
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


def roles_of(staff: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Роли сотрудника в «Команде» по его доступу: мастер - меняет
    «Сервис», администратор - выдачу, аренды или кассу; оба права - обе
    роли. Владелец - пусто: он не исполнитель, плана и зарплаты у него нет.
    Профиль «только смотрит» - тоже пусто: показателей роли у него нет,
    остаются задачи."""
    if logic.is_owner(staff):
        return ()
    got = dict(staff or {})
    out = []
    if logic.can_edit(got, "service"):
        out.append("master")
    if any(logic.can_edit(got, code) for code in ADMIN_SECTIONS):
        out.append("admin")
    return tuple(out)


def role_words(roles: Sequence[str]) -> str:
    """«мастер и администратор» - подпись роли в «Команде»."""
    return " и ".join(ROLE_ONE[r] for r in roles)


def metrics_for(roles: Sequence[str] | None = None) -> tuple[Metric, ...]:
    """Показатели ролей по порядку ROLES, задачи - последними (они у всех).
    None - все показатели (выгрузка, план периода)."""
    if roles is None:
        return METRICS
    own = [m for code, _t in ROLES if code in roles for m in METRICS if m.role == code]
    return tuple(own + [m for m in METRICS if m.role is None])


def terms_for(roles: Sequence[str]) -> tuple[str, ...]:
    """Условия оплаты роли: оклад - всем, остальное - своей роли."""
    return tuple(t for t in TERMS if TERM_ROLE[t] is None or TERM_ROLE[t] in roles)


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


def busy(facts: Mapping[str, Any]) -> bool:
    return any(facts.get(k) for k in BUSY_FACTS)


def salary(terms: Mapping[str, Any] | None, facts: Mapping[str, Any],
           roles: Sequence[str] = ("master", "admin")) -> dict[str, Any]:
    """Начислено по условиям месяца: разбивка и итог, в рублях с копейками.
    Условие чужой роли не начисляется: мастеру % денег, которых он не
    принимает, не положен, даже если остался в строке с прошлых правил."""
    terms = terms or {}
    allowed = terms_for(roles)

    def term(key: str) -> Decimal:
        return _dec(terms.get(key)) if key in allowed else Decimal(0)
    base = logic.to_money(term("salary_base"))
    issues = logic.to_money(term("per_issue") * int(facts.get("issues") or 0))
    orders = logic.to_money(_dec(facts.get("orders_works")) * term("order_pct") / 100)
    money = logic.to_money(_dec(facts.get("payments")) * term("revenue_pct") / 100)
    tasks = logic.to_money(_dec(facts.get("tasks_pay")))
    total = base + issues + orders + money + tasks
    return {"base": base, "issues": issues, "orders": orders, "revenue": money,
            "tasks": tasks, "total": logic.to_money(total), "terms": allowed}


def on_time(facts: Mapping[str, Any]) -> dict[str, Any] | None:
    """Доля нарядов, закрытых в срок ремонта. Нарядов нет - None (не «0 %»)."""
    total = int(facts.get("orders") or 0)
    if not total:
        return None
    done = int(facts.get("orders_on_time") or 0)
    return {"done": done, "of": total, "late": total - done,
            "percent": int((Decimal(done * 100) / total).quantize(Decimal(1), ROUND_HALF_UP))}


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
             days: int, passed: int, open_day: bool = False,
             roles: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Факт против плана по каждому показателю ролей (`roles`; None - все):
    процент и отставание от ровного темпа. Показатель без плана - просто
    число.

    Период ещё не начался (passed = 0) - отставания нет. `open_day` - идущий
    день: несделанное к вечеру - ещё не отставание, сделанное - «в темпе»."""
    plan = plan or {}
    sources = plan.get("sources") or {}
    days = max(int(days), 1)
    passed = min(max(int(passed), 0), days)
    out = []
    for m in metrics_for(roles):
        fact = logic.to_money(_dec(facts.get(m.fact))) if m.money else _dec(facts.get(m.fact))
        target = plan.get(m.plan)
        shown = fact if m.money else int(fact)
        row: dict[str, Any] = {"key": m.fact, "title": m.title, "money": m.money,
                               "role": m.role, "fact": shown,
                               "fact_text": number(shown, m.money),
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
            goal_shown = logic.to_money(goal) if m.money else _count(goal, whole)
            pace_shown = logic.to_money(pace) if m.money else _count(pace, whole)
            row.update(plan=goal_shown, plan_text=number(goal_shown, m.money),
                       percent=int((fact * 100 / goal).quantize(Decimal(1), ROUND_HALF_UP)),
                       pace=pace_shown, pace_text=number(pace_shown, m.money),
                       ahead=ahead, source=sources.get(m.plan))
        out.append(row)
    return out


def plan_source(metrics: Iterable[Mapping[str, Any]]) -> str | None:
    """Откуда план периода: свой, из плана месяца или вперемешку."""
    got = {m["source"] for m in metrics if m.get("source")}
    if not got:
        return None
    return got.pop() if len(got) == 1 else "mixed"


def _score(metrics: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Сводка строки по запланированным показателям: средний процент,
    отстаёт ли что-то, в темпе ли всё, откуда план."""
    planned = [m for m in metrics if m["percent"] is not None]
    return {"percent": (round(sum(m["percent"] for m in planned) / len(planned))
                        if planned else None),
            "behind": any(m["ahead"] is False for m in planned),
            # Зелёный - только когда в темпе всё запланированное: один
            # сделанный показатель при нуле денег - не «всё в порядке».
            "ahead": bool(planned) and all(m["ahead"] is True for m in planned),
            "source": plan_source(planned)}


def rows(people: Iterable[Mapping[str, Any]], facts: Mapping[int, Mapping[str, Any]],
         plans: Mapping[int, Mapping[str, Any]], *, days: int,
         passed: int, open_day: bool = False) -> list[dict[str, Any]]:
    """Строки «Команды»: действующие сотрудники и отключённые с фактом
    в этом периоде (зарплату за отработанное считать всё равно надо).
    Показатели - своей роли (roles_of); владелец помечен `owner`: в
    таблицы ролей он не входит."""
    out = []
    for p in people:
        sid = int(p["id"])
        f = facts.get(sid) or {}
        if not p.get("active", True) and not busy(f):
            continue
        roles = roles_of(p)
        plan = plans.get(sid)
        metrics = progress(plan, f, days=days, passed=passed, open_day=open_day,
                           roles=roles)
        out.append({
            "id": sid, "name": p.get("name") or p.get("login"), "role": logic.role_title(p),
            "roles": roles, "owner": logic.is_owner(p),
            "location": p.get("location"), "active": p.get("active", True),
            "facts": f, "metrics": {m["key"]: m for m in metrics},
            **_score(metrics), "on_time": on_time(f),
            "plan": plan, "pay": salary(plan, f, roles),
            "inherited": bool(plan) and plan.get("own") is False})
    out.sort(key=lambda r: (-(r["percent"] or -1), str(r["name"])))
    return out


def sections(team_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Таблицы «Команды» по ролям: у мастеров - свои колонки, у
    администраторов - свои; сотрудник с обеими ролями - в обеих, процент
    плана в каждой - по показателям этой роли. Без роли (профиль «только
    смотрит») - «Остальные» с задачами, только если такие есть. Владелец -
    ни в одной: он не исполнитель (owners)."""
    out = []
    for code, title in (*ROLES, ("other", "Остальные")):
        keys = metrics_for(() if code == "other" else (code,))
        members = []
        for r in team_rows:
            if r["owner"] or (code == "other" and r["roles"]) or (
                    code != "other" and code not in r["roles"]):
                continue
            cells = [r["metrics"][m.fact] for m in keys]
            members.append({**r, "cells": cells, **_score(cells),
                            "both": len(r["roles"]) > 1})
        if code == "other" and not members:
            continue
        members.sort(key=lambda r: (-(r["percent"] or -1), str(r["name"])))
        total = {m.fact: (logic.to_money(sum((_dec(r["facts"].get(m.fact)) for r in members),
                                             Decimal(0)))
                          if m.money else sum(int(r["facts"].get(m.fact) or 0)
                                              for r in members))
                 for m in keys}
        total["on_time"] = on_time({
            "orders": sum(int(r["facts"].get("orders") or 0) for r in members),
            "orders_on_time": sum(int(r["facts"].get("orders_on_time") or 0)
                                  for r in members)})
        out.append({"code": code, "title": title, "metrics": keys, "rows": members,
                    "total": total})
    return out


def owners(team_rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Владелец сам что-то сделал (выдал, принял деньги, закрыл наряд) -
    строкой под таблицами: в план и зарплату это не идёт, но и не
    пропадает из виду. Без факта - не показывается."""
    out = []
    for r in team_rows:
        if not r["owner"] or not busy(r["facts"]):
            continue
        done = []
        for m in METRICS:
            value = r["facts"].get(m.fact)
            if value:
                done.append({"title": m.title, "money": m.money,
                             "text": number(logic.to_money(_dec(value)) if m.money
                                            else int(value), m.money)})
        out.append({"id": r["id"], "name": r["name"], "done": done})
    return out


def month_share(month_plans: Mapping[date, Mapping[str, Any] | None], start: date,
                days: int) -> dict[str, Any]:
    """План недели или дня из плана месяца по доле дней: каждый день
    периода берёт 1/N плана своего месяца (N - дней в нём). Неделя на стыке
    месяцев - по датам, а не по месяцу начала. Показатель, не планируемый
    ни в одном из месяцев, - None. Деньги - до рубля, штуки - до десятых."""
    out: dict[str, Any] = {}
    for m in METRICS:
        total, seen = Decimal(0), False
        for i in range(days):
            day = start + timedelta(days=i)
            value = (month_plans.get(day.replace(day=1)) or {}).get(m.plan)
            if value is None:
                continue
            seen = True
            total += _dec(value) / monthrange(day.year, day.month)[1]
        if not seen:
            out[m.plan] = None
        elif m.money:
            out[m.plan] = logic.to_money(total.quantize(Decimal(1), ROUND_HALF_UP))
        else:
            out[m.plan] = total.quantize(Decimal("0.1"), ROUND_HALF_UP)
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
            *, today: date, roles: Sequence[str] = ("master", "admin")
            ) -> list[dict[str, Any]]:
    """Аналитика сотрудника помесячно по показателям его роли: план, факт и
    % по каждому и начисленное. Месяцы до первого с планом или фактом не
    показываются (сотрудника тогда не было); новые сверху."""
    out: list[dict[str, Any]] = []
    for first, plan, facts in sorted(months, key=lambda m: m[0]):
        busy_month = busy(facts) or any((plan or {}).get(k) for k in PLAN_FIELDS)
        if not out and not busy_month:
            continue
        span = logic.month_bounds(first, today=today)
        metrics = progress(plan, facts, days=span["days"], passed=span["passed"],
                           roles=roles)
        out.append({"first": first, "key": span["key"], "title": month_name(first),
                    "current": span["is_current"], "metrics": {m["key"]: m for m in metrics},
                    "on_time": on_time(facts), "pay": salary(plan, facts, roles),
                    "has_plan": bool(plan),
                    "inherited": bool(plan) and plan.get("own") is False})
    return out[::-1]


def totals(team: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Плитки над таблицами: по команде без владельца - каждый сотрудник
    один раз, даже если он в двух таблицах ролей."""
    team = [r for r in team if not r.get("owner")]

    def count(key: str) -> int:
        return sum(int(r["facts"].get(key) or 0) for r in team)

    def cash(key: str) -> Decimal:
        return logic.to_money(sum((_dec(r["facts"].get(key)) for r in team), Decimal(0)))
    return {"orders": count("orders"), "orders_own": count("orders_own"),
            "orders_ext": count("orders_ext"), "issues": count("issues"),
            "extensions": count("extensions"), "returns": count("returns"),
            "tasks": count("tasks"), "payments": cash("payments"),
            "repair_revenue": cash("repair_revenue"),
            "pay": logic.to_money(sum((r["pay"]["total"] for r in team), ZERO))}


def form_keys(kind: str, money_ok: bool,
              roles: Sequence[str] = ("master", "admin")) -> tuple[str, ...]:
    """Поля формы плана: показатели роли, у месяца - и условия роли; неделя
    и день - только план. Без права на деньги - только штуки."""
    keys = tuple(m.plan for m in metrics_for(roles))
    if kind == "month":
        keys += terms_for(roles)
    return keys if money_ok else tuple(k for k in keys if k not in MONEY_FIELDS)


def keep_saved(kind: str, fields: Mapping[str, Any],
               saved: Mapping[str, Any] | None) -> dict[str, Any]:
    """Форма несёт не все числа: без права на деньги - без рублей, у
    мастера - без чисел администратора и наоборот. Запись же пишет строку
    целиком, поэтому недостающее - прежнее из базы (`saved` - действующая
    строка: своя или унаследованная у месяца, своя у недели и дня). Иначе
    сохранение штук молча стёрло бы денежный план и оклад, а сохранение
    плана мастера - план того же человека как администратора."""
    out = dict(fields)
    for key in PLAN_FIELDS if kind == "month" else PERIOD_FIELDS:
        if key not in out:
            got = (saved or {}).get(key)
            out[key] = ZERO if got is None and key in TERMS else got
    return out


def parse_plan(data: Mapping[str, str], keys: tuple[str, ...] = PLAN_FIELDS
               ) -> tuple[dict[str, Any] | None, str | None]:
    """План и условия из формы. Пустой план месяца - «не планируется»,
    недели и дня (keys без условий) - «как в плане месяца»; пустое
    условие - ноль. Штуки - целыми: «1,3» не усекается до 1 молча."""
    what = "плана и условий" if set(keys) & set(TERMS) else "плана"
    out: dict[str, Any] = {}
    for key in keys:
        raw = (data.get(key) or "").replace(" ", "").replace(" ", "").replace(",", ".")
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
                return None, "Штуки плана - целым числом."
            out[key] = int(value)
        else:
            out[key] = value.quantize(Decimal("0.01"))
    return out, None
