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
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
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
PLAN_FIELDS = tuple(m[0] for m in METRICS) + TERMS
ZERO = Decimal("0.00")


MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
          "сентябрь", "октябрь", "ноябрь", "декабрь")


def month_name(first: date) -> str:
    return f"{MONTHS[first.month - 1]} {first.year}"


def month_of(raw: Any, *, today: date) -> date:
    """Месяц из адреса («2026-10»); чужое и будущее - текущий месяц."""
    current = today.replace(day=1)
    try:
        year, month = str(raw or "").split("-")
        got = date(int(year), int(month), 1)
    except (ValueError, TypeError):
        return current
    return got if got <= current else current


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


def progress(plan: Mapping[str, Any] | None, facts: Mapping[str, Any], *,
             days: int, passed: int) -> list[dict[str, Any]]:
    """Факт против плана по каждой метрике: процент и отставание от ровного
    темпа. Метрика без плана - просто число."""
    plan = plan or {}
    days = max(int(days), 1)
    passed = min(max(int(passed), 0), days)
    out = []
    for plan_key, fact_key, title, is_money in METRICS:
        fact = revenue(facts) if fact_key == "revenue" else _dec(facts.get(fact_key))
        target = plan.get(plan_key)
        row: dict[str, Any] = {"key": fact_key, "title": title, "money": is_money,
                               "fact": fact if is_money else int(fact),
                               "plan": None, "percent": None, "pace": None, "ahead": None}
        if target is not None and _dec(target) > 0:
            goal = _dec(target)
            pace = goal * passed / days
            row.update(plan=logic.to_money(goal) if is_money else int(goal),
                       percent=int((fact * 100 / goal).quantize(Decimal(1), ROUND_HALF_UP)),
                       pace=(logic.to_money(pace) if is_money
                             else int(pace.quantize(Decimal(1), ROUND_HALF_UP))),
                       ahead=fact >= pace)
        out.append(row)
    return out


def rows(people: Iterable[Mapping[str, Any]], facts: Mapping[int, Mapping[str, Any]],
         plans: Mapping[int, Mapping[str, Any]], *, days: int,
         passed: int) -> list[dict[str, Any]]:
    """Строки «Команды»: действующие сотрудники и отключённые с фактом
    в этом месяце (зарплату за отработанное считать всё равно надо)."""
    out = []
    for p in people:
        sid = int(p["id"])
        f = facts.get(sid) or {}
        busy = any(f.get(k) for k in ("issues", "payments", "orders", "tasks"))
        if not p.get("active", True) and not busy:
            continue
        plan = plans.get(sid)
        metrics = progress(plan, f, days=days, passed=passed)
        planned = [m for m in metrics if m["percent"] is not None]
        out.append({
            "id": sid, "name": p.get("name") or p.get("login"), "role": logic.role_title(p),
            "location": p.get("location"), "active": p.get("active", True),
            "facts": f, "metrics": {m["key"]: m for m in metrics},
            "percent": (round(sum(m["percent"] for m in planned) / len(planned))
                        if planned else None),
            "behind": any(m["ahead"] is False for m in planned),
            "plan": plan, "pay": salary(plan, f), "revenue": revenue(f),
            "inherited": bool(plan) and not plan.get("own")})
    out.sort(key=lambda r: (-(r["percent"] or -1), str(r["name"])))
    return out


def totals(team: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    team = list(team)
    return {"issues": sum(int(r["facts"].get("issues") or 0) for r in team),
            "orders": sum(int(r["facts"].get("orders") or 0) for r in team),
            "tasks": sum(int(r["facts"].get("tasks") or 0) for r in team),
            "revenue": logic.to_money(sum((r["revenue"] for r in team), ZERO)),
            "pay": logic.to_money(sum((r["pay"]["total"] for r in team), ZERO))}


def parse_plan(data: Mapping[str, str]) -> tuple[dict[str, Any] | None, str | None]:
    """План и условия из формы. Пустой план - «не планируется»; пустое
    условие - ноль."""
    out: dict[str, Any] = {}
    for key in PLAN_FIELDS:
        raw = (data.get(key) or "").replace(" ", "").replace(" ", "").replace(",", ".")
        if not raw:
            out[key] = None if key.startswith("plan_") else ZERO
            continue
        try:
            value = Decimal(raw)
        except InvalidOperation:
            return None, "Числа плана и условий - цифрами."
        if not value.is_finite() or value < 0:
            return None, "Числа плана и условий - не меньше нуля."
        if key.endswith("_pct") and value > 100:
            return None, "Процент - от 0 до 100."
        if value > Decimal("100000000"):
            return None, "Слишком большое число."
        if key in ("plan_issues", "plan_orders", "plan_tasks"):
            out[key] = int(value)
        else:
            out[key] = value.quantize(Decimal("0.01"))
    return out, None
