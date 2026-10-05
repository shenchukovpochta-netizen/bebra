"""Меню панели: группы, пункты, подпункты, иконки и счётчики.

Одно дерево на всю панель. base.html рисует из него боковую колонку, а
над заголовком страницы - «крошки» (Склад › Приходы). Раньше меню было
разметкой в шаблоне, и подпункты разделов (приходы склада, наряды
сервиса) жили только вкладками внутри страниц: чтобы попасть в
«Списания», сначала открывали «Склад». Теперь они в колонке, как в
примере владельца, а вкладки на страницах остались для телефона.

Права режет тот же can_view по разделу, что и страж маршрутов:
закрытый раздел не показывается вовсе. Пункт знает свой раздел сам,
а не через logic.section_for: «Расход» лежит под /reports, но без права
на склад он механику ни к чему - такое условие пишется у пункта (`also`).

Какой пункт горит, решает самый длинный подходящий адрес: /parts/receipts
- это «Приходы», а не «Остатки» (/parts); /parts/12 - «Остатки».
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..crm import logic


@dataclass(frozen=True)
class Item:
    label: str
    href: str
    icon: str
    section: str | None = None          # раздел прав; None - открыт всем
    also: tuple[str, ...] = ()          # ещё нужные разделы (все)
    any_of: tuple[str, ...] = ()        # хватит любого из (Входящие)
    match: tuple[str, ...] = ()         # адреса, на которых пункт горит
    badge: str | None = None            # ключ счётчика
    children: tuple[Item, ...] = ()
    # Не показывать владельцу: то, что делают на точке руками (выдача), в
    # его меню лишнее. Права не отнимаются - адрес открывается как прежде.
    not_owner: bool = False
    # Пункт для записи, а не для просмотра (новая аренда): механик с
    # просмотром аренд его не видит.
    edit: bool = False

    @property
    def prefixes(self) -> tuple[str, ...]:
        return self.match or (self.href,)


@dataclass(frozen=True)
class Group:
    title: str
    items: tuple[Item, ...] = field(default_factory=tuple)


NAV: tuple[Group, ...] = (
    Group("Главное", (
        Item("Задачи дня", "/my", "square-check", match=("/my", "/tasks")),
        Item("Сводка", "/", "dashboard", section="dashboard"),
        Item("Команда", "/team", "briefcase", section="staff"),
        Item("Выдача", "/issue", "circle-plus", section="issue", not_owner=True, children=(
            Item("Быстрая выдача", "/issue", "circle-plus", section="issue",
                 not_owner=True),
            # Старая форма аренды (Аренды -> «Форма»): клиенту, который уже
            # был, и редкие случаи - аренда без велосипеда, ручное начисление.
            Item("Повторная выдача", "/rentals/new", "key", section="rentals",
                 not_owner=True, edit=True),
        )),
        # Сообщения, заявки на аренду и «Я оплатил» - один пункт: для
        # точки это одно и то же - клиент ждёт ответа (app/crm/incoming.py).
        Item("Входящие", "/incoming", "inbox", any_of=("inbox", "issue", "claims"),
             match=("/incoming", "/inbox", "/bookings", "/claims", "/deals"), badge="incoming"),
    )),
    Group("Клиенты", (
        Item("Клиенты", "/clients", "users", section="clients",
             match=("/clients", "/signings")),
        Item("Аренды", "/rentals", "key", section="rentals"),
        Item("Сверка с отчётами", "/ops", "message", section="rentals"),
        Item("Рассылки", "/mailing", "send", section="mailing"),
        Item("Акции", "/promos", "tag", section="promos"),
    )),
    Group("Парк", (
        Item("Велосипеды", "/bikes", "bike", section="bikes"),
        Item("Аккумуляторы", "/batteries", "battery", section="batteries"),
        Item("Пересчёт техники", "/stock-takes", "clipboard-check", section="bikes"),
        Item("Карта", "/map", "map-pin", section="trackers", match=("/map", "/trackers")),
        Item("Тревоги", "/alerts", "flag", section="trackers", badge="alerts"),
    )),
    Group("Сервис", (
        Item("Сервис", "/service", "wrench", section="service", children=(
            Item("Рабочий стол", "/service", "wrench", section="service"),
            Item("Наряды", "/orders", "clipboard-list", section="service", badge="orders"),
            Item("Виды работ", "/work-types", "hammer", section="service"),
            Item("Отчёты", "/reports/techs", "chart", section="reports", also=("service",),
                 match=("/reports/techs", "/reports/model-parts")),
        )),
    )),
    Group("Склад", (
        Item("Склад", "/parts", "package", section="inventory", children=(
            Item("Остатки", "/parts", "chart", section="inventory"),
            Item("Заказ запчастей", "/part-orders", "cart", section="inventory",
                 badge="parts"),
            Item("Приходы", "/parts/receipts", "download", section="inventory"),
            Item("Списания", "/parts/write-offs", "trash", section="inventory"),
            Item("Расход", "/reports/spend", "upload", section="reports",
                 also=("inventory",)),
            Item("Движения", "/parts/moves", "arrows", section="inventory"),
            Item("Поставщики", "/suppliers", "truck", section="inventory"),
        )),
    )),
    Group("Деньги", (
        Item("Финансы", "/finance", "wallet", section="finance",
             match=("/finance", "/payments", "/assets")),
        Item("Касса", "/cash", "banknote", section="cash", match=("/cash", "/bank")),
        Item("Тарифы", "/tariffs", "percent", section="tariffs"),
    )),
    Group("Отчёты", (
        Item("Отчёты", "/reports", "pie", section="reports"),
        Item("Франчайзи", "/franchisees", "building", section="franchise"),
        Item("Импорт", "/import", "file-up", section="import"),
    )),
    Group("Система", (
        Item("Сотрудники", "/staff", "user", section="staff"),
        Item("Роли", "/profiles", "shield", section="staff"),
        Item("Настройки", "/company", "settings", section="settings", children=(
            Item("Реквизиты", "/company", "briefcase", section="settings"),
            Item("Документы", "/documents", "file-text", section="settings"),
            Item("Точки", "/locations", "map-pin", section="settings"),
            Item("Каталог", "/models", "layers", section="settings"),
            Item("Уведомления", "/notices", "bell", section="settings"),
            Item("Ввод техники", "/intake", "package", section="settings"),
            Item("Риск клиента", "/risk", "alert", section="settings"),
            Item("Готовность", "/readiness", "circle-check", section="settings",
                 match=("/readiness", "/setup")),
        )),
    )),
)

# Какие разделы открывают какую часть «Входящих»: счётчик пункта - сумма
# только видимых частей, иначе механик видел бы число чужих сообщений.
INCOMING_PARTS: dict[str, str] = {"message": "inbox", "booking": "issue", "claim": "claims"}


def visible(item: Item, staff: Mapping[str, Any] | None) -> bool:
    if item.not_owner and logic.is_owner(staff):
        return False
    if item.children:
        return any(visible(child, staff) for child in item.children)
    if item.section and not logic.can_view(staff, item.section):
        return False
    if item.edit and item.section and not logic.can_edit(staff, item.section):
        return False
    if any(not logic.can_view(staff, code) for code in item.also):
        return False
    return not item.any_of or any(logic.can_view(staff, code) for code in item.any_of)


def _hits(prefix: str, path: str) -> bool:
    if prefix == "/":
        return path == "/"
    return path == prefix or path.startswith((prefix + "/", prefix + "."))


def active_item(path: str, staff: Mapping[str, Any] | None = None) -> Item | None:
    """Пункт, который горит на этом адресе: самый длинный подходящий адрес
    среди видимых листьев. Родитель с подпунктами сам не горит - горит
    его подпункт, а родитель раскрыт."""
    best: tuple[int, Item] | None = None
    for group in NAV:
        for item in group.items:
            for leaf in (item.children or (item,)):
                if staff is not None and not visible(leaf, staff):
                    continue
                for prefix in leaf.prefixes:
                    if _hits(prefix, path) and (best is None or len(prefix) > best[0]):
                        best = (len(prefix), leaf)
    return best[1] if best else None


def badge_value(item: Item, counts: Mapping[str, Any], staff: Mapping[str, Any] | None) -> int:
    if item.children:
        return sum(badge_value(c, counts, staff) for c in item.children if visible(c, staff))
    if not item.badge:
        return 0
    value = counts.get(item.badge)
    if item.badge == "incoming":
        parts = value if isinstance(value, Mapping) else {}
        return sum(int(parts.get(kind) or 0) for kind, code in INCOMING_PARTS.items()
                   if logic.can_view(staff, code))
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def menu(staff: Mapping[str, Any] | None, path: str,
         counts: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Меню сотрудника для шаблона: только видимое, с отметкой «горит»,
    раскрытием родителя и счётчиками. Пустая группа не выводится вовсе:
    подпись над пустотой выглядела бы как поломка."""
    counts = counts or {}
    on = active_item(path, staff)
    out = []
    for group in NAV:
        items = []
        for item in group.items:
            if not visible(item, staff):
                continue
            children = [{"label": c.label, "href": c.href, "icon": c.icon,
                         "on": c is on, "badge": badge_value(c, counts, staff)}
                        for c in item.children if visible(c, staff)]
            items.append({"label": item.label, "href": item.href, "icon": item.icon,
                          "on": item is on, "open": any(c["on"] for c in children),
                          "children": children,
                          "badge": badge_value(item, counts, staff)})
        if items:
            out.append({"title": group.title, "items": items})
    return out


def trail(path: str, staff: Mapping[str, Any] | None = None) -> list[dict[str, str]]:
    """Крошки над заголовком: группа › родитель › пункт. Последняя - ссылка
    на список раздела: из карточки велосипеда «Парк › Велосипеды» ведёт
    назад к списку."""
    on = active_item(path, staff)
    if on is None:
        return []
    for group in NAV:
        for item in group.items:
            if item is on:
                if item.label == group.title:
                    return [{"label": item.label, "href": item.href}]
                return [{"label": group.title, "href": ""},
                        {"label": item.label, "href": item.href}]
            if on in item.children:
                crumbs = [{"label": group.title, "href": ""}]
                if item.label != group.title:
                    crumbs.append({"label": item.label, "href": ""})
                crumbs.append({"label": on.label, "href": on.href})
                return crumbs
    return []


def is_page(path: str) -> bool:
    """Страница панели, а не файл, выгрузка или служебный адрес: меню со
    счётчиками нужно только ей."""
    if path.startswith(("/static", "/healthz", "/manifest", "/hook", "/sign/", "/robots")):
        return False
    return "." not in path.rsplit("/", 1)[-1]


def all_items() -> Iterable[Item]:
    """Все пункты и подпункты - для проверки, что каждый адрес живой."""
    for group in NAV:
        for item in group.items:
            yield item
            yield from item.children


# ─────────────── счётчики ───────────────


async def gather_counts(crm: Any, *, today: Any,
                        settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Числа для меню: сколько ждёт во «Входящих», новых тревог, запчастей
    на исходе и нарядов дольше срока. Считаются теми же правилами, что и
    страницы, - число в меню не должно спорить с экраном, куда ведёт."""
    # Ждущих ответа - одним count, тем же, что плитка «входящих ждут ответа»
    # на сводке: полная лента с расшифровкой ради числа была бы дорогой.
    waiting = await crm.inbox_open_count()
    alerts = await crm.tracker_alerts(open_only=True, limit=1000)
    settings = settings if settings is not None else await crm.settings()
    orders = logic.overdue_orders(
        await crm.work_orders(open_only=True, limit=500),
        default=logic.repair_norm_default(settings), today=today)
    return {
        "incoming": {"message": waiting,
                     "booking": len(await crm.bookings(status="new")),
                     "claim": len(await crm.pending_claims())},
        "alerts": sum(1 for a in alerts if (a.get("state") or "new") == "new"),
        # Остаток и «в пути» - тем же построителем, что у склада и недельной
        # сводки: сырые строки crm.parts пометок «ниже»/«на пределе» не несут,
        # и без part_rows число здесь было бы нулём всегда.
        "parts": len(logic.parts_low(logic.part_rows(
            await crm.parts(active_only=True), await crm.stock_map(),
            transit=await crm.parts_in_transit()))),
        "orders": len(orders),
    }


class CountsCache:
    """Счётчики меню на короткое время на процесс: страница открывается
    чаще, чем меняются числа, а четыре запроса к базе на каждый клик -
    лишняя работа. Любая запись в панели сбрасывает кэш (drop): после
    «Зачислить» число в меню не должно висеть ещё полминуты."""

    def __init__(self, ttl: float = 30.0, clock: Callable[[], float] | None = None) -> None:
        import time
        self.ttl = ttl
        self.clock = clock or time.monotonic
        self._at: float | None = None
        self._value: dict[str, Any] = {}
        # Поколение: подсчёт, начатый до записи, не должен лечь в кэш после
        # неё - иначе старые числа прожили бы ещё полминуты как свежие.
        self.generation = 0

    def fresh(self) -> dict[str, Any] | None:
        if self._at is not None and self.clock() - self._at < self.ttl:
            return self._value
        return None

    def put(self, value: dict[str, Any], *, generation: int | None = None) -> dict[str, Any]:
        """Положить числа, если с начала подсчёта не было записи
        (`generation` - взятое до подсчёта); иначе только вернуть их."""
        if generation is None or generation == self.generation:
            self._value, self._at = value, self.clock()
        return value

    def drop(self) -> None:
        self._at = None
        self.generation += 1
