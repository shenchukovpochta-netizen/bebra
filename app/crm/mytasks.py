"""«Мои задачи»: что сегодня делать этому сотруднику, а не всей сети.

Сводка отвечает владельцу - три числа, план, деньги и задачи всех точек.
Сотруднику точки это лишнее, а то, что его, в общей куче теряется: механик
ищет свои наряды среди чужих, оператор - клиентов своей точки среди всех.
Здесь задачи режутся двумя ключами:

- наряд - по технику: «мои» - где техник он; «без техника» - на его
  точке, их можно взять;
- аренда, заявка на выдачу, тревога трекера - по точке сотрудника
  (`staff.location`). Своей точки нет - задачи всех точек, с пометкой.
  Записи без точки (аренда, заведённая без неё, заявка без точки)
  показываются всем: иначе они не достались бы никому.

И третьим - профилем: группа - тому, кто по ней действует, то есть может
менять её раздел (звонит должнику тот, кто ведёт аренды, а не механик,
которому аренды открыты посмотреть). Исключения - свои наряды (их видно
и с правом смотреть: назначены на него) и тревоги трекеров (на них
реагируют звонком или выездом, а не правкой). Строка ведёт в карточку.
Рубли - только с правом на финансы, как на сводке. Источники те же, что
у сводки и разделов (logic.expiring, search_rows, order_stuck): список
не вправе показывать не то, что покажет раздел.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date
from typing import Any

from . import logic

# Разделы, по которым у сотрудника бывают задачи (logic.TASK_SECTIONS).
# Профиль без них (только отчёты, только финансы) «Моих задач» не
# получает главной страницей.
TASK_SECTIONS = logic.TASK_SECTIONS
# Сколько строк группы показывать: остальное - «и ещё N» со ссылкой на раздел.
GROUP_LIMIT = 30


def wants_tasks(staff: Mapping[str, Any] | None) -> bool:
    return any(logic.can_view(staff, code) for code in TASK_SECTIONS)


def _mine(point: str | None, where: Any) -> bool:
    """Запись этой точки. Без своей точки - всё; запись без точки - всем."""
    return point is None or where is None or where == point


def _group(code: str, title: str, level: str, section: str, url: str,
           items: list[dict]) -> dict[str, Any] | None:
    if not items:
        return None
    return {"code": code, "title": title, "level": level, "section": section,
            "url": url, "count": len(items), "rows": items[:GROUP_LIMIT],
            "more": max(0, len(items) - GROUP_LIMIT)}


def _rental_item(r: Mapping[str, Any], *, money_ok: bool, point: str | None) -> dict:
    s = r.get("summary") or {}
    bits = []
    if r.get("bike_code"):
        bits.append(f"№ {r['bike_code']}")
    until = s.get("covered_until")
    if until is not None:
        bits.append(f"оплачено до {until:%d.%m}")
    left = s.get("days_left")
    if left is not None and left < 0:
        bits.append(f"просрочка {-left} дн.")
    elif left == 0:
        bits.append("платёж сегодня")
    if money_ok and s.get("debt"):
        bits.append("долг " + logic.money(s["debt"]))
    if r.get("intent_label"):
        bits.append(str(r["intent_label"]))
    if point is not None and not r.get("location"):
        bits.append("без точки")
    return {"title": r.get("full_name") or "—", "sub": " · ".join(bits),
            "url": f"/rentals/{r['id']}", "phone": r.get("phone"),
            "hot": left is not None and left < 0}


def my_tasks(staff: Mapping[str, Any], *, expiring: Iterable[Mapping[str, Any]] = (),
             search: Mapping[str, Any] | None = None,
             orders: Iterable[Mapping[str, Any]] = (),
             bookings: Iterable[Mapping[str, Any]] = (),
             alerts: Iterable[Mapping[str, Any]] = (),
             claims: Iterable[Mapping[str, Any]] = (),
             shift_open: bool | None = None,
             today: date | None = None,
             repair_norm: int = logic.ORDER_STUCK_DAYS,
             booking_url: Callable[[Mapping[str, Any]], str] | None = None
             ) -> list[dict[str, Any]]:
    """Группы задач сотрудника, горящее сверху. Пустые не возвращаются.

    `expiring` - строки logic.expiring (с summary), `search` - результат
    logic.search_rows, `orders` - открытые наряды, `bookings` - новые
    заявки, `alerts` - открытые тревоги (с bike_location), `claims` -
    заявки на зачисление, `shift_open` - открыта ли касса его точки
    (None - не проверяли). `booking_url` - адрес выдачи по заявке.
    """
    today = today or date.today()
    point = staff.get("location") or None
    me = staff.get("id")
    money_ok = logic.can_view(staff, "finance")
    can = logic.can_edit
    groups: list[dict[str, Any] | None] = []

    if logic.can_view(staff, "service"):
        open_orders = [o for o in orders if logic.order_is_open(o)]

        def order_item(o: Mapping[str, Any]) -> dict:
            stuck = logic.order_stuck(o, today=today, default=repair_norm)
            what = (" ".join(x for x in (f"№ {o['bike_code']}" if o.get("bike_code")
                                         else None, o.get("bike_model")) if x)
                    or o.get("object_note") or "без объекта")
            bits = [logic.ORDER_STATUSES.get(o.get("status") or "", o.get("status") or ""),
                    f"{logic.order_days(o, today=today)} сут. в работе"]
            if stuck:
                bits.append("дольше срока")
            if o.get("complaint"):
                bits.append(str(o["complaint"]))
            return {"title": f"{o.get('no') or ''} · {what}", "sub": " · ".join(bits),
                    "url": f"/orders/{o['id']}", "hot": stuck}

        mine = sorted((o for o in open_orders if me is not None and o.get("tech_id") == me),
                      key=lambda o: (not logic.order_stuck(o, today=today,
                                                           default=repair_norm),
                                     o.get("opened_at") or today, o["id"]))
        groups.append(_group("orders_mine", "Мои наряды", "hot" if any(
            logic.order_stuck(o, today=today, default=repair_norm) for o in mine) else "warn",
            "service", "/orders", [order_item(o) for o in mine]))
        free = [o for o in open_orders if not o.get("tech_id")
                and _mine(point, o.get("location"))] if can(staff, "service") else []
        free.sort(key=lambda o: (o.get("opened_at") or today, o["id"]))
        groups.append(_group("orders_free", "Наряды без техника — можно взять", "info",
                             "service", "/service", [order_item(o) for o in free]))

    if can(staff, "issue"):
        due = []
        for b in bookings:
            if b.get("status", "new") != "new" or not _mine(point, b.get("location_name")):
                continue
            wanted = b.get("wanted_on")
            if wanted is None or wanted <= today or logic.waitlist_coming_today(b, today=today):
                due.append(b)
        items = [{"title": b.get("full_name") or "—",
                  "sub": " · ".join(x for x in (b.get("model"), b.get("tariff_name"),
                                                b.get("location_title")) if x),
                  "url": booking_url(b) if booking_url else "/bookings",
                  "phone": b.get("phone"), "hot": True} for b in due]
        groups.append(_group("bookings", "Выдать сегодня по заявкам", "hot", "issue",
                             "/bookings", items))

    if can(staff, "rentals"):
        rows = [r for r in expiring if _mine(point, r.get("location"))]
        overdue = [r for r in rows if (r.get("summary") or {}).get("days_left") is not None
                   and r["summary"]["days_left"] <= 0]
        soon = [r for r in rows if r not in overdue]
        groups.append(_group(
            "overdue", "Просрочка и платёж сегодня — позвонить", "hot", "rentals",
            "/?expiring=all" if logic.can_view(staff, "dashboard") else "/rentals",
            [_rental_item(r, money_ok=money_ok, point=point) for r in overdue]))
        search = search or {}
        wanted = [r for r in (*search.get("searching", ()), *search.get("candidates", ()))
                  if _mine(point, r.get("location"))]
        groups.append(_group(
            "search", "Розыск — решить", "hot", "rentals", "/rentals/search",
            [{"title": r.get("full_name") or "—",
              "sub": " · ".join(x for x in (
                  f"№ {r['bike_code']}" if r.get("bike_code") else None,
                  ("пора признавать потерю" if r.get("theft") else
                   "в розыске" if r.get("search_at") else
                   f"просрочка {r.get('overdue_days')} дн. — кандидат")) if x),
              "url": f"/rentals/{r['id']}", "phone": r.get("phone"), "hot": True}
             for r in wanted]))
        groups.append(_group(
            "soon", "Истекает на днях — напомнить", "warn", "rentals",
            "/?expiring=all" if logic.can_view(staff, "dashboard") else "/rentals",
            [_rental_item(r, money_ok=money_ok, point=point) for r in soon]))

    if logic.can_view(staff, "trackers"):
        fresh = [a for a in alerts if a.get("state", "new") == "new"
                 and _mine(point, a.get("bike_location"))]
        items = [{"title": " ".join(x for x in (
                      f"№ {a['bike_code']}" if a.get("bike_code") else a.get("alias"),
                      "—", logic.TRACKER_ALERTS.get(a.get("kind") or "", a.get("kind")))
                      if x),
                  "sub": " · ".join(x for x in (
                      a.get("client_name"),
                      "срочная" if a.get("level") == "urgent" else None) if x),
                  "url": f"/trackers/{a['tracker_id']}", "hot": a.get("level") == "urgent"}
                 for a in fresh]
        groups.append(_group("alerts", "Тревоги трекеров", "hot" if any(
            i["hot"] for i in items) else "warn", "trackers", "/alerts", items))

    if can(staff, "claims"):
        groups.append(_group(
            "claims", "Заявки на зачисление — сверить с банком", "warn", "claims",
            "/claims", [{"title": c.get("full_name") or "—",
                         "sub": "клиент нажал «Я оплатил»", "url": "/claims",
                         "hot": False} for c in claims]))

    if point is not None and shift_open is False and logic.can_edit(staff, "cash"):
        groups.append(_group("cash", f"Касса «{point}» не открыта", "warn", "cash",
                             "/cash", [{"title": "Открыть смену",
                                        "sub": "наличные без смены не принимаются",
                                        "url": "/cash", "hot": False}]))

    order = {level: i for i, level in enumerate(logic.TASK_LEVELS)}
    out = [g for g in groups if g]
    out.sort(key=lambda g: order.get(g["level"], len(order)))
    return out
