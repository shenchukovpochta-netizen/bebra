"""Деньги вперёд и по статьям: доход сводки и платёжный календарь.

**Доход по статьям** - откуда пришли деньги за период и на какой точке:

- аренда - платежи журнала (`ledger`, вид `payment`) по точке своей
  аренды (db.money_by_location - то же правило, что у трёх чисел);
- сторонний ремонт - оплаченные клиентские наряды на чужую технику;
- ремонт арендаторам - оплаченные клиентом наряды на наш велосипед.

Ремонт в журнале не живёт (журнал - это аренда), поэтому статьи
складываются здесь, а не в ledger, и средний чек аренды не трогают.
Штрафы - начисление, а не приход: они показаны справочно.

**Платёжный календарь** - что придёт и уйдёт по дням вперёд:

- ожидаемые платежи аренды - из идущих аренд: следующий период
  начисляется в день «оплачено до» на цену периода, дальше - каждые
  `period_days`. Не ждём: ручное начисление, «сдаёт» и розыск;
- долги - отдельной строкой «к сбору»: когда их отдадут, неизвестно;
- плановые расходы и приходы (`crm.cash_plan`) - то, чего в базе нет
  фактом: аренда помещения, зарплата, закупка. Повтор - каждые N
  месяцев с даты.

Остаток на начало вводит человек: денег на счёте система не знает. День,
когда остаток уходит в минус, - кассовый разрыв.
"""

from __future__ import annotations

import calendar as _calendar
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from . import logic

ZERO = Decimal("0.00")
HORIZONS = (14, 30, 60, 90)
DIRECTIONS = {"out": "расход", "in": "приход"}


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value is not None else 0))
    except InvalidOperation:
        return Decimal(0)


# ─────────────────────────── доход по статьям ───────────────────────────

def income(money: Mapping[Any, Mapping[str, Any]], repairs: Mapping[Any, Mapping[str, Any]],
           places: Iterable[str]) -> dict[str, Any]:
    """Строки по точкам (в порядке справочника, «без точки» - если есть
    деньги) и итог. `money` - db.money_by_location, `repairs` -
    db.repair_income_by_location."""
    names = [p for p in places]
    for key in (*money, *repairs):
        if key not in names and key is not None:
            names.append(key)
    if None in money or None in repairs:
        names.append(None)
    rows = []
    for name in names:
        m, r = money.get(name) or {}, repairs.get(name) or {}
        rent = logic.to_money(_dec(m.get("paid")))
        ext = logic.to_money(_dec(r.get("external")))
        own = logic.to_money(_dec(r.get("renters")))
        fines = logic.to_money(_dec(m.get("charged_fines")) - _dec(m.get("charged")))
        row = {"location": name, "rent": rent, "repair_external": ext,
               "repair_renters": own, "total": rent + ext + own, "fines": fines,
               "refunded": logic.to_money(_dec(m.get("refunded"))),
               "orders": int(r.get("orders") or 0)}
        if name is None and not (row["total"] or row["fines"]):
            continue
        rows.append(row)
    keys = ("rent", "repair_external", "repair_renters", "total", "fines", "refunded")
    total = {k: logic.to_money(sum((r[k] for r in rows), ZERO)) for k in keys}
    total["orders"] = sum(r["orders"] for r in rows)
    total["share"] = {k: (round(float(total[k] * 100 / total["total"]), 1)
                          if total["total"] else None)
                      for k in ("rent", "repair_external", "repair_renters")}
    return {"rows": rows, "total": total}


# ─────────────────────────── платёжный календарь ───────────────────────────

def add_months(day: date, months: int) -> date:
    """Тот же день через N месяцев; 31-е в коротком месяце - его последний день."""
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return date(year, month, min(day.day, _calendar.monthrange(year, month)[1]))


def expected_rent(rentals: Iterable[Mapping[str, Any]], *, start: date,
                  end: date) -> list[dict[str, Any]]:
    """Платежи аренды, которых ждём по дням: начисление следующего периода
    в день «оплачено до» и дальше каждые period_days."""
    out = []
    for r in rentals:
        if r.get("status", "active") != "active" or r.get("billing", "auto") != "auto":
            continue
        if r.get("intent") == "return" or r.get("search_at"):
            continue
        price = _dec(r.get("price"))
        step = int(r.get("period_days") or 0)
        nxt = r.get("billed_until")
        if price <= 0 or step <= 0 or not isinstance(nxt, date):
            continue
        nxt = max(nxt, start)
        while nxt <= end:
            out.append({"day": nxt, "amount": logic.to_money(price), "direction": "in",
                        "kind": "rent", "title": r.get("full_name") or "аренда",
                        "url": f"/rentals/{r['id']}", "location": r.get("location")})
            nxt += timedelta(days=step)
    return out


def planned(items: Iterable[Mapping[str, Any]], *, start: date,
            end: date) -> list[dict[str, Any]]:
    """Плановые строки на окно: разовые - в свой день (просроченная
    неотмеченная - сегодня), повторяющиеся - каждые N месяцев."""
    out = []
    for x in items:
        if x.get("done_at"):
            continue
        due = x["due_on"]
        every = int(x.get("repeat_months") or 0)
        days = []
        if every <= 0:
            days.append(max(due, start))
        else:
            n = 0
            while (day := add_months(due, n * every)) <= end:
                if day >= start:
                    days.append(day)
                n += 1
        for day in days:
            if day > end:
                continue
            out.append({"day": day, "amount": logic.to_money(_dec(x.get("amount"))),
                        "direction": x.get("direction") or "out", "kind": "plan",
                        "title": x.get("title") or "", "id": x.get("id"),
                        "repeat": every, "overdue": every <= 0 and due < start,
                        "location": x.get("location")})
    return out


def calendar(rentals: Iterable[Mapping[str, Any]], plans: Iterable[Mapping[str, Any]], *,
             start: date, days: int, opening: Decimal | None = None,
             debts: Decimal = ZERO, location: str = "") -> dict[str, Any]:
    """Дни вперёд: приход аренды, плановые приход и расход, итог дня и
    остаток. Точка - фильтр по аренде и плану (строки без точки - общие,
    они остаются)."""
    end = start + timedelta(days=max(int(days), 1) - 1)
    items = expected_rent(rentals, start=start, end=end) + planned(plans, start=start, end=end)
    if location:
        items = [i for i in items if i.get("location") in (None, location)
                 or (location == "none" and not i.get("location"))]
    by_day: dict[date, list[dict]] = {}
    for item in items:
        by_day.setdefault(item["day"], []).append(item)
    balance = opening if opening is not None else ZERO
    rows, gap = [], None
    tot = {"rent": ZERO, "plan_in": ZERO, "plan_out": ZERO}
    for n in range((end - start).days + 1):
        day = start + timedelta(days=n)
        lst = sorted(by_day.get(day, []), key=lambda i: (i["direction"] != "out", i["title"]))
        rent = sum((i["amount"] for i in lst if i["kind"] == "rent"), ZERO)
        p_in = sum((i["amount"] for i in lst if i["kind"] == "plan"
                    and i["direction"] == "in"), ZERO)
        p_out = sum((i["amount"] for i in lst if i["kind"] == "plan"
                     and i["direction"] == "out"), ZERO)
        net = rent + p_in - p_out
        balance += net
        tot["rent"] += rent
        tot["plan_in"] += p_in
        tot["plan_out"] += p_out
        if opening is not None and gap is None and balance < 0:
            gap = day
        rows.append({"day": day, "rent": rent, "plan_in": p_in, "plan_out": p_out,
                     "net": net, "balance": logic.to_money(balance), "entries": lst,
                     "rent_count": sum(1 for i in lst if i["kind"] == "rent"),
                     "weekend": day.weekday() >= 5})
    return {"days": rows, "start": start, "end": end, "gap": gap, "opening": opening,
            "debts": logic.to_money(debts),
            "total": {k: logic.to_money(v) for k, v in tot.items()},
            "net": logic.to_money(tot["rent"] + tot["plan_in"] - tot["plan_out"])}


def parse_plan(data: Mapping[str, str], *, today: date
               ) -> tuple[dict[str, Any] | None, str | None]:
    """Плановая строка из формы."""
    title = " ".join((data.get("title") or "").split())[:200]
    if not title:
        return None, "Напишите, что за платёж."
    try:
        due = date.fromisoformat((data.get("due_on") or "").strip())
    except ValueError:
        return None, "Дата платежа - дата."
    if due < today - timedelta(days=366):
        return None, "Дата слишком давняя."
    raw = (data.get("amount") or "").replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None, "Сумма - число рублей."
    if not amount.is_finite() or amount <= 0 or amount > Decimal("100000000"):
        return None, "Сумма - больше нуля."
    direction = data.get("direction") if data.get("direction") in DIRECTIONS else "out"
    try:
        every = int(data.get("repeat_months") or 0)
    except ValueError:
        every = 0
    return {"due_on": due, "title": title, "amount": amount.quantize(Decimal("0.01")),
            "direction": direction, "repeat_months": min(max(every, 0), 12),
            "location": (data.get("location") or "").strip() or None,
            "note": (data.get("note") or "").strip()[:500] or None}, None


def week_ahead(cal: Mapping[str, Any], days: int = 7) -> dict[str, Any]:
    """Сводке: ближайшая неделя календаря одной строкой."""
    rows = list(cal["days"])[:days]
    return {"rent": logic.to_money(sum((r["rent"] for r in rows), ZERO)),
            "plan_out": logic.to_money(sum((r["plan_out"] for r in rows), ZERO)),
            "plan_in": logic.to_money(sum((r["plan_in"] for r in rows), ZERO)),
            "count": sum(r["rent_count"] for r in rows)}
