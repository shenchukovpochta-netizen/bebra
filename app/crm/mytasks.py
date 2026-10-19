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

Владельцу те же группы - по каждой точке (`by_point`): что видит
администратор и мастер этой точки, но строго её записи, а записи без
точки (и точки вне справочника) и «Я оплатил» - одной группой «общие».
Наряды у него - не «мои», а «дольше срока» по всем техникам точки и «без
техника». Источники читаются один раз на страницу и режутся здесь.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date
from typing import Any
from urllib.parse import urlencode

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
           items: list[dict], limit: int = GROUP_LIMIT) -> dict[str, Any] | None:
    """Группа задач; `hot` - горящих строк во всей группе, а не в
    показанных: из них счётчик точки у владельца."""
    if not items:
        return None
    return {"code": code, "title": title, "level": level, "section": section,
            "url": url, "count": len(items), "rows": items[:limit],
            "more": max(0, len(items) - limit),
            "hot": sum(1 for i in items if i.get("hot"))}


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
             booking_url: Callable[[Mapping[str, Any]], str] | None = None,
             match: Callable[[Any], bool] | None = None,
             overseer: bool = False,
             limit: int = GROUP_LIMIT
             ) -> list[dict[str, Any]]:
    """Группы задач сотрудника, горящее сверху. Пустые не возвращаются.

    `expiring` - строки logic.expiring (с summary), `search` - результат
    logic.search_rows, `orders` - открытые наряды, `bookings` - новые
    заявки, `alerts` - открытые тревоги (с bike_location), `claims` -
    заявки на зачисление, `shift_open` - открыта ли касса его точки
    (None - не проверяли). `booking_url` - адрес выдачи по заявке.
    `match` - чья запись по её точке (по умолчанию - своя точка и записи
    без точки), `overseer` - взгляд владельца на точку (`by_point`):
    наряды дольше срока у всех техников вместо «моих», `limit` - строк в
    группе.
    """
    today = today or date.today()
    point = staff.get("location") or None
    me = staff.get("id")
    money_ok = logic.can_view(staff, "finance")
    can = logic.can_edit
    mine_at = match or (lambda where: _mine(point, where))
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
            if overseer and o.get("tech_id"):
                bits.append(str(o.get("tech_name") or o.get("tech_login") or "техник"))
            if o.get("complaint"):
                bits.append(str(o["complaint"]))
            return {"title": f"{o.get('no') or ''} · {what}", "sub": " · ".join(bits),
                    "url": f"/orders/{o['id']}", "hot": stuck}

        if overseer:
            late = sorted((o for o in open_orders if o.get("tech_id")
                           and mine_at(o.get("location"))
                           and logic.order_stuck(o, today=today, default=repair_norm)),
                          key=lambda o: (o.get("opened_at") or today, o["id"]))
            groups.append(_group("orders_late", "Наряды дольше срока ремонта", "hot",
                                 "service", "/orders?overdue=1",
                                 [order_item(o) for o in late], limit))
        else:
            mine = sorted((o for o in open_orders
                           if me is not None and o.get("tech_id") == me),
                          key=lambda o: (not logic.order_stuck(o, today=today,
                                                               default=repair_norm),
                                         o.get("opened_at") or today, o["id"]))
            groups.append(_group("orders_mine", "Мои наряды", "hot" if any(
                logic.order_stuck(o, today=today, default=repair_norm) for o in mine)
                else "warn", "service", "/orders", [order_item(o) for o in mine], limit))
        free = [o for o in open_orders if not o.get("tech_id")
                and mine_at(o.get("location"))] if can(staff, "service") else []
        free.sort(key=lambda o: (o.get("opened_at") or today, o["id"]))
        groups.append(_group("orders_free", "Наряды без техника" if overseer
                             else "Наряды без техника — можно взять", "info",
                             "service", "/service", [order_item(o) for o in free], limit))

    if can(staff, "issue"):
        due = []
        for b in bookings:
            if b.get("status", "new") != "new" or not mine_at(b.get("location_name")):
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
                             "/bookings", items, limit))

    if can(staff, "rentals"):
        # «без точки» в строке - тому, кто видит свою точку и чужие записи
        # без неё; владельцу по точкам такие записи идут общими.
        mark = None if overseer else point
        rows = [r for r in expiring if mine_at(r.get("location"))]
        overdue = [r for r in rows if (r.get("summary") or {}).get("days_left") is not None
                   and r["summary"]["days_left"] <= 0]
        soon = [r for r in rows if r not in overdue]
        groups.append(_group(
            "overdue", "Просрочка и платёж сегодня — позвонить", "hot", "rentals",
            logic.EXPIRING_URL,
            [_rental_item(r, money_ok=money_ok, point=mark) for r in overdue], limit))
        search = search or {}
        wanted = [r for r in (*search.get("searching", ()), *search.get("candidates", ()))
                  if mine_at(r.get("location"))]
        groups.append(_group(
            "search", "Розыск — решить", "hot", "rentals", "/rentals/search",
            [{"title": r.get("full_name") or "—",
              "sub": " · ".join(x for x in (
                  f"№ {r['bike_code']}" if r.get("bike_code") else None,
                  ("пора признавать потерю" if r.get("theft") else
                   "в розыске" if r.get("search_at") else
                   f"просрочка {r.get('overdue_days')} дн. — кандидат")) if x),
              "url": f"/rentals/{r['id']}", "phone": r.get("phone"), "hot": True}
             for r in wanted], limit))
        groups.append(_group(
            "soon", "Истекает на днях — напомнить", "warn", "rentals",
            logic.EXPIRING_URL,
            [_rental_item(r, money_ok=money_ok, point=mark) for r in soon], limit))

    if logic.can_view(staff, "trackers"):
        fresh = [a for a in alerts if a.get("state", "new") == "new"
                 and mine_at(a.get("bike_location"))]
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
            i["hot"] for i in items) else "warn", "trackers", "/alerts", items, limit))

    if can(staff, "claims"):
        groups.append(_group(
            "claims", "Заявки на зачисление — сверить с банком", "warn", "claims",
            "/claims", [{"title": c.get("full_name") or "—",
                         "sub": "клиент нажал «Я оплатил»", "url": "/claims",
                         "hot": False} for c in claims], limit))

    if point is not None and shift_open is False and logic.can_edit(staff, "cash"):
        groups.append(_group("cash", f"Касса «{point}» не открыта", "warn", "cash",
                             "/cash", [{"title": "Открыть смену",
                                        "sub": "наличные без смены не принимаются",
                                        "url": "/cash", "hot": False}]))

    order = {level: i for i, level in enumerate(logic.TASK_LEVELS)}
    out = [g for g in groups if g]
    out.sort(key=lambda g: order.get(g["level"], len(order)))
    return out


def by_point(staff: Mapping[str, Any], points: Iterable[str], *,
             expiring: Iterable[Mapping[str, Any]] = (),
             search: Mapping[str, Any] | None = None,
             orders: Iterable[Mapping[str, Any]] = (),
             bookings: Iterable[Mapping[str, Any]] = (),
             alerts: Iterable[Mapping[str, Any]] = (),
             claims: Iterable[Mapping[str, Any]] = (),
             today: date | None = None,
             repair_norm: int = logic.ORDER_STUCK_DAYS,
             booking_url: Callable[[Mapping[str, Any]], str] | None = None,
             limit: int = GROUP_LIMIT) -> dict[str | None, list[dict[str, Any]]]:
    """Рабочие группы владельца по точкам: {точка: группы, None: общие}.

    У каждой точки - то, что видит её администратор и мастер (те же
    правила `my_tasks` и права смотрящего), но только записи этой точки:
    запись без точки у сотрудника видна всем, а у владельца она одна - в
    общих (None), вместе с записями точек вне справочника (закрытая,
    чужое имя) и «Я оплатил»: у заявки на зачисление точки нет. Касса
    точки владельцу не задача: «смена не открыта» - дело администратора,
    а у склада кассы нет вовсе. Источники - один раз на страницу: здесь
    они только режутся.
    """
    names = [p for p in dict.fromkeys(points) if p]
    known = set(names)
    common: dict[str, Any] = {
        "expiring": list(expiring), "search": search or {}, "orders": list(orders),
        "bookings": list(bookings), "alerts": list(alerts), "today": today,
        "repair_norm": repair_norm, "booking_url": booking_url, "overseer": True,
        "limit": limit}
    out: dict[str | None, list[dict[str, Any]]] = {}
    for name in names:
        groups = my_tasks({**staff, "location": name},
                          match=lambda where, n=name: where == n, **common)
        for g in groups:
            g["url"] = _at_point(g["url"], name)
        out[name] = groups
    out[None] = my_tasks({**staff, "location": None}, claims=list(claims),
                         match=lambda where: not where or where not in known, **common)
    return out


def _at_point(url: str, point: str) -> str:
    """«Весь список» группы точки - с фильтром точки, где раздел его знает
    (аренды и наряды); остальные списки - целиком."""
    if not url.startswith(("/rentals?", "/orders")):
        return url
    return f"{url}{'&' if '?' in url else '?'}{urlencode({'location': point})}"
