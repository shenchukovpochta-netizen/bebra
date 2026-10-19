"""Сводка владельца: «Критично» группами, парк по моделям, сравнение месяцев.

Сводка - не список дел, а картина сети: что горит, план-факт месяца,
деньги вперёд, парк и точки. Дела живут в «Задачах дня» (прозвон,
наряды, поручения) и во «Входящих» (заявки, «Я оплатил»), и на сводке их
нет. Здесь - то, что сводке нужно сверх общих построителей logic и
cashflow; своих формул трёх чисел тут нет:

- «Критично» тремя группами - клиенты, задачи, проблемы. Строки - из
  logic.today_tasks (те же источники, что у разделов) и то, чего там нет:
  просроченные поручения, расхождения, ждущие ответа. Строка видна тому,
  кому открыт её раздел, иначе она вела бы в «нет доступа».
- советы по парку (перевезти, скидка на простаивающую модель) - те же
  строки today_tasks, только не в «Критично», а в блоке парка;
- плитки парка: logic.fleet_tiles плюс бронь и подменные всегда, а
  потерянные, проданные и списанные - отдельно, вне оборота;
- парк по моделям сейчас: сколько каждой в аренде, свободно, в ремонте;
- сравнение с прошлым периодом: доход по статьям против того же
  отрезка прошлого месяца (logic.report_prev_span);
- платёжный календарь (cashflow.calendar) столбиками на две недели.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from . import logic

ZERO = Decimal("0.00")
# Платёжный календарь на сводке: две недели вперёд. Длиннее - на своей
# странице (/finance/calendar), там и остаток, и плановые строки.
CALENDAR_DAYS = 14

# Группы «Критично» в порядке показа: код, подпись, коды строк.
CRITICAL_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("clients", "Клиенты", ("overdue", "theft", "search")),
    ("tasks", "Задачи", ("tasks_late",)),
    ("problems", "Проблемы", ("integrity", "inbox", "orders", "alerts_urgent", "alerts")),
)
# Раздел строки: без права на него строки нет. None - видна всем
# (поручения: каждый видит в «Задачах» то, что ему открыто).
CRITICAL_SECTION: dict[str, str | None] = {
    "overdue": "rentals", "theft": "rentals", "search": "rentals",
    "tasks_late": None,
    "integrity": "bikes", "inbox": "inbox", "orders": "service",
    "alerts_urgent": "trackers", "alerts": "trackers",
}
# Советы по парку и раздел, куда ведёт строка: перевозка - в отчёт по
# точкам, простой - в форму акции.
ADVICE_SECTION = {"transfer": "reports", "idle_promo": "promos"}

# Плитки вне оборота: в знаменатель простоя они не идут никогда.
OUT_OF_SERVICE = ("lost", "sold", "written_off")


def critical(tasks: Iterable[Mapping[str, Any]], extra: Iterable[Mapping[str, Any]] = (),
             *, can: Callable[[str], bool]) -> dict[str, Any]:
    """Группы «Критично»: {"groups": [{code, title, rows}], "count"}.

    `tasks` - logic.today_tasks, `extra` - строки сверх них в том же виде
    (code, title, count, url, names). Пустые группы не показываются.
    Горящая строка (level hot) - красным счётчиком.
    """
    by_code: dict[str, list[dict[str, Any]]] = {}
    for item in (*tasks, *extra):
        code = str(item.get("code") or "")
        if code not in CRITICAL_SECTION:
            continue
        section = CRITICAL_SECTION[code]
        if section is not None and not can(section):
            continue
        by_code.setdefault(code, []).append(
            {**item, "hot": item.get("level", "hot") == "hot"})
    groups = []
    for code, title, codes in CRITICAL_GROUPS:
        rows = [i for c in codes for i in by_code.get(c, ())]
        if rows:
            groups.append({"code": code, "title": title, "rows": rows})
    return {"groups": groups, "count": sum(len(g["rows"]) for g in groups)}


def fleet_advice(tasks: Iterable[Mapping[str, Any]], *, can_view: Callable[[str], bool],
                 can_edit: Callable[[str], bool]) -> list[dict[str, Any]]:
    """Советы по парку из задач сводки: перевезти между точками, скидка на
    простаивающую модель. Предложение скидки ведёт в форму акции - видно
    тому, кто её заведёт (`edit`)."""
    out = []
    for item in tasks:
        section = ADVICE_SECTION.get(str(item.get("code") or ""))
        if section is None or not can_view(section):
            continue
        if item.get("edit") and not can_edit(section):
            continue
        out.append(dict(item))
    return out


def fleet_tiles(counts: Mapping[str, int], plan: Mapping[str, Any] | None = None, *,
                spare: int = 0) -> dict[str, Any]:
    """Плитки парка сводки: logic.fleet_tiles (у клиента, ремонт, свободны,
    ТО, на сборке - с нормами плана) плюс бронь и подменные, даже без
    нормы. Проценты - от операционного парка, как у logic.fleet_tiles.
    `out` - вне оборота: потеряны, проданы, списаны - числом, без процента."""
    base = sum(int(counts.get(code, 0)) for code in logic.OPERATIONAL_STATUSES)
    tiles = logic.fleet_tiles(counts, plan, spare=spare)
    have = {t["code"] for t in tiles}

    def plain(code: str, title: str, value: int) -> dict[str, Any]:
        return {"code": code, "title": title, "value": int(value), "norm": None,
                "sense": "", "percent": round(100 * value / base, 1) if base else None,
                "over": False, "under": False, "bad": False, "diff": 0}

    extra = []
    if "reserved" not in have:
        extra.append(plain("reserved", "Бронь", int(counts.get("reserved", 0))))
    if "spare" not in have:
        extra.append(plain("spare", "Подменные", spare))
    # Бронь - рядом с ТО, до «на сборке»: та в оборот ещё не вошла.
    at = next((i for i, t in enumerate(tiles) if t["code"] == "new"), len(tiles))
    tiles = tiles[:at] + extra[:1] + tiles[at:] + extra[1:]
    return {"tiles": tiles, "operational": base,
            "out": [{"code": code, "title": logic.BIKE_STATUSES[code],
                     "value": int(counts.get(code, 0))} for code in OUT_OF_SERVICE]}


def fleet_by_model(bikes: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Парк по моделям сейчас: строка на модель, как она записана в парке
    (так её ищет список /bikes). Модель только с потерянными, проданными
    и списанными не показывается - это уже не парк; на сборке - да.

    Загрузка - доля в аренде от операционного парка модели: та же доля,
    что у плитки «у клиента», только по модели.
    """
    keys = ("fleet", "rented", "free", "reserved", "repair", "new", "spare")
    rows: dict[str, dict[str, Any]] = {}
    for bike in bikes:
        status = bike.get("status")
        operational = status in logic.OPERATIONAL_STATUSES
        if not operational and status != "new":
            continue
        model = str(bike.get("model") or "").strip()
        row = rows.setdefault(model, {"model": model, **dict.fromkeys(keys, 0)})
        if operational:
            row["fleet"] += 1
            if bike.get("spare"):
                row["spare"] += 1
        if status == "rented":
            row["rented"] += 1
        elif status == "available":
            row["free"] += 1
        elif status == "reserved":
            row["reserved"] += 1
        elif status in ("repair", "maintenance"):
            row["repair"] += 1
        elif status == "new":
            row["new"] += 1
    out = sorted(rows.values(), key=lambda r: (-r["fleet"], -r["new"], r["model"].casefold()))
    total = {"model": "Итого", **{k: sum(r[k] for r in out) for k in keys}}
    for row in (*out, total):
        row["load"] = round(100 * row["rented"] / row["fleet"]) if row["fleet"] else None
    return {"rows": out, "total": total}


def change(now: Any, prev: Any) -> dict[str, Any]:
    """Сейчас против прошлого: разница и проценты. Прошлого не было - без
    процентов: «+∞ %» ничего не говорит."""
    now, prev = logic.to_money(now), logic.to_money(prev)
    diff = now - prev
    return {"now": now, "prev": prev, "diff": diff, "up": diff > 0,
            "percent": round(float(diff * 100 / prev), 1) if prev > 0 else None}


def short_money(value: Any) -> str:
    """Подпись столбика: «40,6к», «1,2 млн», «950». Полная сумма - в
    подсказке и в таблице по дням, под столбиком ей тесно."""
    amount = Decimal(str(value or 0))
    for size, unit in ((Decimal(1_000_000), " млн"), (Decimal(1000), "к")):
        if abs(amount) >= size:
            short = (amount / size).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            text = f"{short:.1f}".rstrip("0").rstrip(".")
            return text.replace(".", ",") + unit
    return f"{amount:.0f}"


def calendar_strip(cal: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Платёжный календарь (cashflow.calendar) столбиками по дням: сколько
    придёт (аренда и плановые приходы) и сколько уйдёт, в одном масштабе.
    Остаток ниже нуля - только когда остаток на сегодня введён: без него
    «минус» значил бы лишь, что расход раньше прихода."""
    days = list(cal["days"])
    top = max([d["rent"] + d["plan_in"] for d in days]
              + [d["plan_out"] for d in days] + [Decimal(1)])
    out = []
    for d in days:
        came = d["rent"] + d["plan_in"]
        out.append({**d, "came": came, "short": short_money(came),
                     "in_height": int(round(100 * came / top)),
                     "out_height": int(round(100 * d["plan_out"] / top)),
                     "negative": cal.get("opening") is not None and d["balance"] < 0,
                     "gap": cal.get("gap") == d["day"]})
    return out


INCOME_LINES: tuple[tuple[str, str], ...] = (
    ("rent", "Аренда"),
    ("repair_external", "Сторонний ремонт"),
    ("repair_renters", "Ремонт арендаторам"),
)


def income_compare(now: Mapping[str, Any], prev: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Доход по статьям против прошлого периода: строки cashflow.income
    `now` и `prev` по статьям и «Всего». Доля - от всего за период."""
    total_now = now["total"]["total"]
    rows = []
    for key, title in (*INCOME_LINES, ("total", "Всего")):
        a, b = now["total"][key], prev["total"][key]
        rows.append({"code": key, "title": title, **change(a, b),
                     "share": (round(float(a * 100 / total_now), 1) if total_now else None)})
    return rows
