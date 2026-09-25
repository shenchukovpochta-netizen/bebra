"""Сервис демо-стенда: наряды, склад запчастей, смета и счёт за ремонт,
пересчёт техники.

Наряды - следствие истории ядра, а не отдельная выдумка: каждый ремонт и
ТО из world.service_intervals получает наряд с теми же датами, иначе
«стоит в ремонте» на сводке и «в работе» в сервисе рассказывали бы разные
истории. Идущий ремонт - открытый наряд (иначе «в ремонте, а наряда нет»
в расхождениях), закрытый - запись ремонта bike_log + repair_items, как
пишет закрытие наряда в панели: отчёт «что ломается» собран по ним.

Склад считается от расхода: сначала известно, что и когда ушло в наряды,
потом под это планируются приходы. Так остаток не уходит в минус ни в
один момент истории, а средневзвешенная себестоимость считается тем же
logic.average_cost, что у прихода в панели.

Деньги ремонта в crm.ledger не пишутся никогда: журнал - это аренда, и
средний чек считается по нему. Выручка ремонта живёт на наряде (paid_at).
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from ..crm import logic
from .core import P1, P2, P3
from .world import MSK, StaffMember, at

if TYPE_CHECKING:
    import asyncpg

    from .world import ServiceInterval, World

D = Decimal
DAY = timedelta(days=1)
MINUTE = timedelta(minutes=1)

# Поставщики запчастей. Телефоны - из пустого диапазона, как у людей демо.
SUPPLIERS = (
    ("ООО «Демо-Запчасть» (демо)", "+7 (000) 000-22-33",
     "Запчасти к электровелосипедам, демо"),
    ("ИП Электродеталь (демо)", "+7 (000) 000-44-55", "Электрика и свет, демо"),
)

# Склад: (название, узел, ед., себестоимость, цена клиенту, неснижаемый,
# пачка). Цена клиенту - та, что лист «Арендатору» ставит за запчасть:
# работа из прайса плюс запчасть со склада дают его итог.
PARTS = (
    ("Камера 18×3.0", "tube_tire", "шт", 380, 600, 10, 10),
    ("Покрышка 18×3.0 всесезонная", "tube_tire", "шт", 1250, 2000, 4, 2),
    ("Колодки тормозные дисковые, пара", "brake_pads", "компл.", 260, 500, 8, 10),
    ("Минеральное масло для тормозов, 100 мл", "brake_line", "шт", 180, 300, 4, 5),
    ("Суппорт гидравлического тормоза", "brake_line", "шт", 750, 1200, 2, 1),
    ("Ручка газа 60V", "throttle", "шт", 1100, 1800, 3, 1),
    ("Контроллер 60V 30A", "controller", "шт", 2300, 3500, 0, 1),
    ("Датчики Холла, комплект", "hall_sensors", "компл.", 150, 300, 3, 5),
    ("Мотор-колесо 60V 1000W", "motor_wheel", "шт", 10500, 15000, 0, 1),
    ("Фара передняя LED", "headlight", "шт", 850, 1400, 3, 1),
    ("Фонарь задний (стоп-сигнал)", "taillight", "шт", 850, 1400, 2, 1),
    ("Поворотник", "turn_signals", "шт", 600, 1000, 4, 2),
    ("Сигнал звуковой", "horn", "шт", 420, 750, 2, 1),
    ("Крыло переднее", "fender_front", "шт", 480, 800, 2, 1),
    ("Крыло заднее", "fender_rear", "шт", 580, 950, 2, 1),
    ("Зеркало заднего вида", "mirrors", "шт", 280, 500, 6, 10),
    ("Держатель телефона", "phone_holder", "шт", 450, 800, 4, 2),
    ("Подножка боковая", "kickstand", "шт", 1100, 1900, 2, 1),
    ("Блок управления с дисплеем 60V", "wiring", "шт", 750, 1200, 2, 1),
    ("Клеммы и термоусадка, комплект", "wiring", "компл.", 90, 150, 10, 10),
    ("Разъём зарядки АКБ", "battery", "шт", 520, 900, 4, 2),
    ("Седло", "saddle", "шт", 850, 1400, 2, 1),
    ("Грипсы, пара", "handlebar", "компл.", 170, 300, 4, 5),
    ("Подшипники вилки, комплект", "fork", "компл.", 380, 600, 2, 2),
    ("Герметик для гидроизоляции", "other", "шт", 420, 700, 3, 2),
    ("Зарядное устройство 60V 2A", "charger", "шт", 1700, 3000, 3, 1),
    ("Тормозной диск 180 мм", "brake_disc", "шт", 600, 1000, 0, 1),
    ("Амортизационная вилка", "fork", "шт", 4300, 6500, 0, 1),
)
# Дорогое берут под ремонт, а не на полку: остаток после окна - ноль.
# Поэтому наряд «ждёт запчасть» ждёт именно контроллер - его нет.
ON_DEMAND = {"Контроллер 60V 30A", "Мотор-колесо 60V 1000W"}
WAIT_PART = "Контроллер 60V 30A"
# Лежат с первого дня без движения: плитка «лежит квартал» склада.
STALE = {"Тормозной диск 180 мм": 2, "Амортизационная вилка": 1,
         "Зарядное устройство 60V 2A": 3}
# Последняя поставка их не добирает: к сегодня они ниже неснижаемого и
# едут в заказе у поставщика - есть что показать в «что заказать».
LOW_AT_END = {"Ручка газа 60V", "Фонарь задний (стоп-сигнал)", "Держатель телефона"}


@dataclass(frozen=True)
class _Recipe:
    weight: int
    work: str                  # строка прайса work_types.title
    part: str | None           # запчасть со склада
    complaint: str
    heavy: bool = False        # долгий ремонт: электрика и мотор
    damage: bool = False       # порча арендатором: платит он, лист «Арендатору»


RECIPES = (
    _Recipe(18, "Шиномонтаж заднего колеса с установкой камеры", "Камера 18×3.0",
            "Прокол заднего колеса"),
    _Recipe(9, "Шиномонтаж переднего камерного колеса с установкой камеры",
            "Камера 18×3.0", "Спустило переднее колесо"),
    _Recipe(5, "Шиномонтаж заднего колеса с бескамерной всесезонной покрышкой",
            "Покрышка 18×3.0 всесезонная", "Лысая покрышка, виден корд"),
    _Recipe(10, "Замена тормозных колодок перед / зад (не в рамках ТО)",
            "Колодки тормозные дисковые, пара", "Скрипят и плохо тормозят тормоза"),
    _Recipe(5, "Прокачка гидролинии переднего тормоза",
            "Минеральное масло для тормозов, 100 мл",
            "Проваливается ручка переднего тормоза"),
    _Recipe(3, "Замена суппорта переднего тормоза", "Суппорт гидравлического тормоза",
            "Течёт передний суппорт"),
    _Recipe(7, "Замена / установка ручки газа", "Ручка газа 60V", "Заедает ручка газа"),
    _Recipe(3, "Замена контроллера", "Контроллер 60V 30A", "Не едет: ошибка контроллера",
            heavy=True),
    _Recipe(2, "Замена датчиков Холла", "Датчики Холла, комплект",
            "Дёргается мотор на старте", heavy=True),
    _Recipe(1, "Замена мотор-колеса с шиномонтажом", "Мотор-колесо 60V 1000W",
            "Гул и люфт мотор-колеса", heavy=True),
    _Recipe(4, "Замена / установка передней фары", "Фара передняя LED", "Не горит фара"),
    _Recipe(3, "Замена задней фары (стоп-сигнала)", "Фонарь задний (стоп-сигнал)",
            "Не работает стоп-сигнал"),
    _Recipe(2, "Замена / установка поворотника, шт", "Поворотник", "Разбит поворотник",
            damage=True),
    _Recipe(1, "Замена / установка сигнального гудка", "Сигнал звуковой",
            "Не работает сигнал"),
    _Recipe(3, "Замена / установка заднего крыла", "Крыло заднее",
            "Треснуло заднее крыло", damage=True),
    _Recipe(2, "Замена / установка переднего крыла", "Крыло переднее",
            "Сломано переднее крыло"),
    _Recipe(4, "Замена зеркал, шт", "Зеркало заднего вида", "Разбито зеркало",
            damage=True),
    _Recipe(4, "Замена / установка держателя для телефона", "Держатель телефона",
            "Сломан держатель телефона", damage=True),
    _Recipe(2, "Замена / установка подножки", "Подножка боковая", "Подножка не держит"),
    _Recipe(3, "Замена / установка блока управления", "Блок управления с дисплеем 60V",
            "Не включается дисплей", heavy=True),
    _Recipe(3, "Замена разъёма зарядки аккумулятора (папа / мама)", "Разъём зарядки АКБ",
            "АКБ не заряжается, греется разъём"),
    _Recipe(2, "Пайка фазной проводки контроллера", "Клеммы и термоусадка, комплект",
            "Пропадает питание на ходу", heavy=True),
    _Recipe(2, "Замена седла", "Седло", "Порвано седло", damage=True),
    _Recipe(2, "Замена / установка грипсы", "Грипсы, пара", "Прокручивается грипса"),
    _Recipe(1, "Замена подшипников амортизационной вилки", "Подшипники вилки, комплект",
            "Стук в передней вилке"),
    # Лист «Арендатору» знает гидроизоляцию только у Monster.
    _Recipe(2, "Гидроизоляция полная (монстров)", "Герметик для гидроизоляции",
            "После дождя отключается на ходу"),
)
MONSTER_ONLY = "Гидроизоляция полная (монстров)"
TO_WORK = "Плановое ТО (каждые 2 недели)"
TO_PADS = "Замена тормозных колодок перед / зад (в рамках ТО)"
TO_FLUID = "Осмотр уровня тормозной жидкости и долив"
TO_LABOR = D(350)          # час механика по ставке точки, а не цена клиенту
LABOR_SHARE = D("0.45")    # себестоимость работы - доля цены из прайса
PARTS_SHARE = D("0.6")     # запчасть, купленная под чужой ремонт, - доля её цены

# Чужая техника - второе направление. (объект, клиент - буква карточки или
# None для «зашёл с улицы», работы [(прайс, запчасть со склада, штук)],
# жалоба, сколько дней назад принесли, исход, точка или None - по весам
# EXT_POINTS). Свежие приколоты к точкам: отчёт «По точкам» за 30 дней
# показывает выручку сервиса у каждой, а не у той, куда выпал жребий.
EXTERNAL = (
    ("Самокат Ninebot Max G30", "A",
     (("Шиномонтаж заднего колеса с установкой камеры", None, 1),),
     "Прокол заднего колеса", 142, "paid", None),
    ("Электросамокат Kugoo M4 Pro", "B", (("Замена / установка ручки газа", None, 1),),
     "Не реагирует ручка газа", 128, "paid", None),
    ("Электровелосипед Monster, свой у курьера", "C",
     (("Гидроизоляция полная (монстров)", None, 1),),
     "После дождя отключается на ходу", 117, "paid", None),
    ("Трицикл грузовой 48V", "D", (("Замена датчиков Холла", None, 1),),
     "Дёргается на старте", 103, "paid", None),
    ("АКБ 60V 20Ah от электровелосипеда", None,
     (("Гидроизоляция аккумулятора", None, 1),
      ("Замена разъёма зарядки аккумулятора (папа / мама)", None, 1)),
     "Не заряжается после дождя", 94, "paid", None),
    ("Самокат Xiaomi Pro 2", None,
     (("Шиномонтаж переднего камерного колеса с установкой камеры", None, 1),),
     "Прокол переднего колеса", 83, "paid", None),
    ("Электровелосипед Kugoo V3 Pro, свой у курьера", "E",
     (("Замена тормозных колодок перед / зад (не в рамках ТО)",
       "Колодки тормозные дисковые, пара", 1),
      ("Прокачка гидролинии переднего тормоза",
       "Минеральное масло для тормозов, 100 мл", 1)),
     "Плохо тормозит", 71, "paid", None),
    ("Электровелосипед Monster, свой у курьера", "C",
     (("Замена контроллера", "Контроллер 60V 30A", 1),),
     "Не едет: ошибка контроллера", 57, "paid", None),
    ("Электросамокат Kugoo G2 Pro", None, (("Пайка блока управления", None, 1),),
     "Не включается дисплей", 44, "paid", None),
    ("Электровелосипед Jetson, свой у курьера", "F",
     (("Замена / установка передней фары", "Фара передняя LED", 1),),
     "Не горит фара", 33, "invoice", None),
    ("АКБ 48V 13Ah от самоката", "G",
     (("Гидроизоляция аккумулятора", None, 1), ("Клеммирование проводов, шт", None, 2)),
     "Пропадает питание", 21, "paid", P3),
    ("Самокат Ninebot Max G30", "A",
     (("Замена тормозных колодок перед / зад (не в рамках ТО)",
       "Колодки тормозные дисковые, пара", 1),),
     "Скрипят тормоза", 12, "paid", P2),
    ("Электровелосипед Minako, свой у курьера", "I", (("Замена контроллера", None, 1),),
     "Не едет", 9, "declined", P1),
    ("Электровелосипед Kugoo V3 Pro, свой у курьера", "E",
     (("Шиномонтаж заднего колеса с установкой камеры", "Камера 18×3.0", 1),),
     "Прокол заднего колеса", 6, "paid", P1),
    ("Электросамокат Kugoo G2 Pro", "B",
     (("Шиномонтаж заднего колеса с бескамерной всесезонной покрышкой", None, 1),),
     "Лысая покрышка", 4, "unpaid", P1),
    ("Трицикл грузовой 48V", "D",
     (("Заварить раму", None, 1), ("Прогонка резьбовых соединений, шт", None, 2)),
     "Трещина рамы у багажника", 3, "unpaid", P2),
    ("Электросамокат Kugoo M4 Pro", "H", (("Замена датчиков Холла", None, 1),),
     "Дёргается мотор", 2, "in_work", P3),
    ("Электровелосипед Monster, свой у курьера", "C",
     (("Замена мотор-колеса с шиномонтажом", None, 1),),
     "Гул и люфт мотор-колеса", 2, "approve", P1),
    ("Самокат Xiaomi Pro 2", "J",
     (("Протяжка держателя телефона через косу проводов", None, 1),),
     "Поставить держатель телефона", 1, "approve", P2),
)
# Карточки клиентов сервиса: курьеры со своей техникой, в прокате не были.
EXT_CLIENTS = {"A": ("2gis", "samokat"), "B": ("avito", "yandex"),
               "C": ("yandex_maps", "delivery"), "D": ("site", "other"),
               "E": ("avito", "yandex"), "F": ("2gis", "sbermarket"),
               "G": ("avito", "samokat"), "H": ("yandex_maps", "yandex"),
               "I": ("2gis", "other"), "J": ("site", "samokat")}
EXT_POINTS = ((P1, 5), (P2, 3), (P3, 2))

# Списания и пересчёты склада: (дней от начала истории или None, дней до
# сегодня, запчасть, штук, причина).
WRITE_OFFS = (
    (40, None, "Ручка газа 60V", 1, "Брак: не работает из коробки, поставщик не принял"),
    (95, None, "Крыло заднее", 1, "Треснуло при хранении"),
    (None, 20, "Фара передняя LED", 1, "Брак: не держит влагу, мигает"),
)
COUNTS = (
    (70, None, "Грипсы, пара", 1, "Излишек по пересчёту"),
    (None, 33, "Зеркало заднего вида", -1, "Недостача по пересчёту"),
)

# Таблицы, куда модуль пишет со своими id: счётчики двигаем в конце.
_TABLES = ("suppliers", "parts", "part_docs", "clients", "bike_log", "work_orders",
           "part_moves", "work_order_items", "repair_items", "part_orders",
           "part_order_items", "pay_orders", "stock_takes", "stock_take_items")


def _round10(value: Any) -> D:
    return D(int((D(str(value)) / 10).to_integral_value()) * 10)


# ─────────────────────────── строки в памяти ───────────────────────────

@dataclass
class _Part:
    id: int
    title: str
    node: str
    unit: str
    base: D
    price: D
    min_stock: int
    pack: int
    cost: D = D(0)             # средневзвешенная после всех приходов


@dataclass
class _Line:
    """Строка наряда. part - запчасть со склада: её себестоимость известна
    только после прохода склада, поэтому parts_cost проставляется там."""
    title: str
    node: str | None
    qty: int
    price: D
    parts_cost: D
    labor_cost: D
    at: datetime
    work_type_id: int | None = None
    note: str | None = None
    part: _Part | None = None
    move_id: int | None = None
    id: int = 0


@dataclass
class _Order:
    kind: str                  # repair | maintenance | external
    bike_id: int | None
    object_note: str | None
    payer: str
    client_id: int | None
    status: str
    tech: StaffMember | None
    complaint: str
    opened_at: datetime
    closed_at: datetime | None
    location: str | None
    created_by: str
    lines: list[_Line] = field(default_factory=list)
    paid_at: datetime | None = None
    note: str | None = None
    estimate: D = D(0)
    estimate_sent_at: datetime | None = None
    approved_at: datetime | None = None
    approved_by: str | None = None
    declined_at: datetime | None = None
    outcome: str = ""          # исход чужого наряда: paid, invoice, ...
    id: int = 0
    no: str = ""
    log_id: int | None = None
    total: D = D(0)
    cost: D = D(0)


@dataclass
class _Event:
    """Движение склада до записи: знак в qty, как в part_moves."""
    at: datetime
    part: _Part
    kind: str                  # receipt | order | write_off | count
    qty: int
    line: _Line | None = None
    order: _Order | None = None
    doc: dict | None = None
    note: str | None = None
    by: str | None = None
    price: D = D(0)            # цена прихода
    cost: D = D(0)             # себестоимость единицы, после прохода
    id: int = 0


# ─────────────────────────── сборка ───────────────────────────

class _Service:
    """Наряды, склад и документы сервиса - всё в памяти, потом COPY."""

    def __init__(self, w: World, rng: random.Random, ids: dict[str, int],
                 nos: dict[str, int], types: dict[str, dict], since: dict[int, datetime]
                 ) -> None:
        self.w = w
        self.rng = rng
        self.ids = ids
        self.nos = nos
        self.types = types
        self.since = since
        self.S = w.history_start
        self.now = w.now
        self.parts: dict[str, _Part] = {}
        self.suppliers: list[int] = []
        self.orders: list[_Order] = []
        self.events: list[_Event] = []
        self.docs: list[dict] = []
        self.part_orders: list[dict] = []
        self.clients: list[dict] = []
        self.client_of: dict[str, int] = {}
        self.pay_orders: list[dict] = []
        self.bike_log: list[tuple] = []
        self.repair_items: list[tuple] = []
        self.rentals = {r.id: r for r in w.rentals}
        self.waiting: _Order | None = None

    def nid(self, table: str) -> int:
        self.ids[table] += 1
        return self.ids[table]

    def nno(self, key: str) -> int:
        self.nos[key] += 1
        return self.nos[key]

    def work(self, title: str) -> dict:
        row = self.types.get(title)
        if row is None:
            raise RuntimeError(f"в прайсе сервиса нет строки «{title}»")
        return row

    def between(self, lo: datetime, hi: datetime, a: float = 0.1, b: float = 0.6
                ) -> datetime:
        """Момент внутри [lo, hi] - доля a..b пути; пустое окно - lo."""
        if hi <= lo:
            return lo
        return self.workday(lo + (hi - lo) * self.rng.uniform(a, b), lo, hi)

    def workday(self, t: datetime, lo: datetime, hi: datetime) -> datetime:
        """t в рабочие часы точки (10:00-19:30), если это возможно внутри
        [lo, hi]: строка наряда в час ночи выглядела бы выдумкой."""
        hour = (t - at(t.date())).total_seconds() / 3600
        if 10 <= hour <= 19.5:
            return t
        day = t.date() if hour < 10 else t.date() + DAY
        moved = at(day, self.rng.uniform(10.2, 11.5))
        return moved if lo <= moved <= hi else t

    def later(self, t: datetime, lo: float, hi: float) -> datetime:
        """Через lo..hi часов, но в рабочие часы: вечер переезжает на утро."""
        moved = t + timedelta(hours=self.rng.uniform(lo, hi))
        return self.workday(moved, moved, moved + 2 * DAY)

    # ─────────────── справочники ───────────────

    def reference(self) -> None:
        for name, _phone, _note in SUPPLIERS:
            sid = self.nid("suppliers")
            self.suppliers.append(sid)
            self.w.suppliers[name] = sid
        for title, node, unit, cost, price, minimum, pack in PARTS:
            self.parts[title] = _Part(id=self.nid("parts"), title=title, node=node,
                                      unit=unit, base=D(cost), price=D(price),
                                      min_stock=minimum, pack=pack, cost=D(cost))
        for recipe in RECIPES:
            self.work(recipe.work)
        for title in (TO_WORK, TO_PADS, TO_FLUID):
            self.work(title)

    # ─────────────── строки нарядов ───────────────

    def labor(self, wt: dict) -> D:
        """Себестоимость работы: доля цены прайса, но не меньше 150 ₽ -
        даже «бесплатная» по прайсу грипса стоит механику времени."""
        base = wt["price"] if wt["price"] is not None else (wt["price_ext"] or 0)
        return max(_round10(D(str(base)) * LABOR_SHARE), D(150))

    def work_lines(self, title: str, part_title: str | None, qty: int, t: datetime,
                   *, sheet: str, client_pays: bool) -> list[_Line]:
        """Работа из прайса и, если есть, запчасть со склада - как их
        вносит панель: строка прайса и «Со склада» отдельной строкой.

        Цена клиенту - только когда он платит (своему ремонту она ни к
        чему). Запчасть со склада панель ставит по её цене всегда; чтобы
        работа с запчастью не посчиталась дважды, работа тогда идёт своей
        колонкой листа, без «запчасти» из прайса."""
        wt = self.work(title)
        work_col, parts_col = (("price", "parts_price") if sheet == "own"
                               else ("price_ext", "parts_price_ext"))
        part = self.parts[part_title] if part_title else None
        price = D(0)
        parts_cost = D(0)
        if client_pays:
            if wt[work_col] is None:
                raise RuntimeError(f"«{title}» нет в листе {sheet}")
            price = (logic.to_money(wt[work_col]) if part
                     else logic.sheet_price(wt, sheet))
        if part is None:
            parts_cost = _round10(D(str(wt[parts_col] or 0)) * PARTS_SHARE)
        lines = [_Line(title=title, node=wt["node"], qty=qty, price=price,
                       parts_cost=parts_cost, labor_cost=self.labor(wt), at=t,
                       work_type_id=wt["id"])]
        if part is not None:
            lines.append(_Line(title=part.title, node=part.node, qty=1,
                               price=part.price, parts_cost=D(0), labor_cost=D(0),
                               at=t + timedelta(minutes=self.rng.randint(2, 15)),
                               note="Со склада", part=part))
        return lines

    def to_lines(self, t: datetime, mileage: int) -> tuple[list[_Line], str]:
        """ТО: работа по регламенту, колодки и долив - по состоянию."""
        rng = self.rng
        wt = self.work(TO_WORK)
        lines = [_Line(title=wt["title"], node=wt["node"], qty=1, price=D(0),
                       parts_cost=D(0), labor_cost=TO_LABOR, at=t, work_type_id=wt["id"])]
        extra = [(TO_PADS, "Колодки тормозные дисковые, пара", 0.35),
                 (TO_FLUID, "Минеральное масло для тормозов, 100 мл", 0.15)]
        for title, part_title, share in extra:
            if rng.random() < share:
                w2 = self.work(title)
                t = t + timedelta(minutes=rng.randint(10, 30))
                part = self.parts[part_title]
                lines.append(_Line(title=w2["title"], node=w2["node"], qty=1,
                                   price=D(0), parts_cost=D(0), labor_cost=D(0), at=t,
                                   work_type_id=w2["id"]))
                lines.append(_Line(title=part.title, node=part.node, qty=1,
                                   price=part.price, parts_cost=D(0), labor_cost=D(0),
                                   at=t + MINUTE * 3, note="Со склада", part=part))
        return lines, f"Плановое ТО, пробег {mileage} км"

    def pick_recipes(self, model: str, *, days: float, damage: bool) -> list[_Recipe]:
        rng = self.rng
        pool = [r for r in RECIPES
                if (r.work != MONSTER_ONLY or model.startswith("Monster"))
                and (r.damage or not damage)]
        n = 1 if damage else rng.choices((1, 2, 3), weights=(60, 30, 10))[0]
        chosen: list[_Recipe] = []
        if days >= 4 and not damage and rng.random() < 0.7:
            heavy = [r for r in pool if r.heavy]
            chosen.append(rng.choices(heavy, weights=[r.weight for r in heavy])[0])
        while len(chosen) < n:
            recipe = rng.choices(pool, weights=[r.weight for r in pool])[0]
            if all(recipe.work != c.work for c in chosen):
                chosen.append(recipe)
        return chosen

    # ─────────────── наряды своего парка ───────────────

    def own_orders(self) -> None:
        """Наряд на каждый ремонт и ТО истории. Порча арендатором - наряд
        за его счёт по листу «Арендатору»: лист - по объекту, не по
        плательщику (CLAUDE.md, «Прайс сервиса»)."""
        intervals = sorted(self.w.service_intervals,
                           key=lambda i: (i.start, i.bike_id))
        damage_pool = [k for k, i in enumerate(intervals)
                       if i.kind == "repair" and i.reason == "return"
                       and i.end is not None and i.outcome == "available"
                       and i.rental_id in self.rentals]
        damaged = set()
        if damage_pool:
            step = len(damage_pool) / 6
            damaged = {damage_pool[min(int(step * (j + 0.5)), len(damage_pool) - 1)]
                       for j in range(6)}
        for k, interval in enumerate(intervals):
            bike = self.w.bike(interval.bike_id)
            if interval.end is None and bike.status not in ("repair", "maintenance"):
                continue
            self.orders.append(self.own_order(interval, damage=k in damaged))
        # Страховка: велосипед в ремонте без идущего ремонта в истории ядра
        # всё равно получает наряд - иначе он вылез бы в «Расхождениях».
        covered = {o.bike_id for o in self.orders if o.closed_at is None}
        for bike in sorted(self.w.bikes, key=lambda b: b.id):
            if bike.status == "repair" and bike.id not in covered:
                opened = self.since.get(bike.id, self.now - timedelta(hours=2))
                self.orders.append(_Order(
                    kind="repair", bike_id=bike.id, object_note=None, payer="own",
                    client_id=None, status="new", tech=None,
                    complaint="Принят в ремонт, ждёт диагностики", opened_at=opened,
                    closed_at=None, location=bike.point,
                    created_by=self.w.operator(bike.point).actor))
        self.open_stages()

    def own_order(self, interval: ServiceInterval, *, damage: bool) -> _Order:
        rng = self.rng
        bike = self.w.bike(interval.bike_id)
        point = interval.point or bike.point
        opened = interval.start
        end = interval.end
        closed = None
        if end is not None:
            closed = max(end - timedelta(minutes=rng.uniform(1, 4)), opened + MINUTE)
        horizon = closed if closed is not None else self.now
        days = (horizon - opened).total_seconds() / 86400
        tech = self.w.mechanic(point)
        by = ("staff:demo" if interval.reason == "import"
              else self.w.operator(point).actor)
        order = _Order(kind=interval.kind, bike_id=bike.id, object_note=None, payer="own",
                       client_id=None, status="done" if closed else "in_work",
                       tech=tech, complaint="", opened_at=opened, closed_at=closed,
                       location=point, created_by=by)
        t = self.between(opened, horizon, 0.1, 0.5)
        if interval.kind == "maintenance":
            order.lines, order.complaint = self.to_lines(t, interval.mileage_km)
        elif interval.outcome == "written_off":
            # Ремонт не начали: рама и вилка - дороже велосипеда. Наряд
            # отменён, велосипед списывает владелец (журнал статусов ядра).
            order.status = "cancelled"
            order.complaint = "ДТП: погнуты рама и вилка"
            order.note = "Ремонт нецелесообразен: велосипед списан (демо)"
        else:
            recipes = self.pick_recipes(bike.model, days=days, damage=damage)
            if damage:
                order.payer = "client"
                order.client_id = self.rentals[interval.rental_id].client_id
            for recipe in recipes:
                order.lines += self.work_lines(recipe.work, recipe.part, 1, t,
                                               sheet="own", client_pays=damage)
                t = t + timedelta(minutes=rng.randint(15, 60))
            order.complaint = ", ".join(
                [recipes[0].complaint] + [r.complaint.lower() for r in recipes[1:]])
            if damage:
                # Согласовали у стойки, заплатил при выдаче - мимо журнала:
                # журнал это аренда (красная линия счёта за ремонт).
                order.complaint = "Порча при аренде: " + order.complaint.lower()
                order.estimate = logic.order_totals_client(
                    [{"price": ln.price, "qty": ln.qty} for ln in order.lines])
                order.approved_at = min(opened + timedelta(minutes=rng.randint(5, 30)),
                                        horizon)
                order.approved_by = self.w.operator(point).actor
                order.paid_at = closed
        prefix = {"import": "Перенос из таблицы: ", "swap": "Замена в аренде: "}
        order.complaint = prefix.get(interval.reason, "") + order.complaint
        for line in order.lines:
            line.at = min(line.at, horizon - MINUTE)
            line.at = max(line.at, opened)
        return order

    def open_stages(self) -> None:
        """Идущие ремонты в разных стадиях: самый старый ждёт контроллер,
        самый свежий только принят, остальные в работе."""
        open_repairs = sorted((o for o in self.orders
                               if o.closed_at is None and o.kind == "repair"),
                              key=lambda o: (o.opened_at, o.bike_id or 0))
        if not open_repairs:
            return
        wait = open_repairs[0]
        wt = self.work("Замена контроллера")
        t = self.between(wait.opened_at, self.now - MINUTE, 0.05, 0.2)
        wait.status = "waiting"
        wait.complaint = "Не едет: ошибка контроллера"
        wait.lines = [_Line(title=wt["title"], node=wt["node"], qty=1, price=D(0),
                            parts_cost=D(0), labor_cost=self.labor(wt), at=t,
                            work_type_id=wt["id"])]
        self.waiting = wait
        if len(open_repairs) > 1:
            fresh = open_repairs[-1]
            fresh.status, fresh.tech, fresh.lines = "new", None, []

    # ─────────────── чужая техника ───────────────

    def external_orders(self) -> None:
        rng = self.rng
        today = self.w.today
        for obj, who, works, complaint, age, outcome, pinned in EXTERNAL:
            day = today - timedelta(days=age)
            opened = at(day, rng.uniform(10.5, 15.5))
            if opened >= self.now:
                opened = self.now - timedelta(hours=rng.uniform(2, 3))
            points = [(p, wgt) for p, wgt in EXT_POINTS
                      if p in self.w.points and self.w.points[p].opened_on <= day]
            if pinned is not None and pinned in dict(points):
                point = pinned
            else:
                point = rng.choices([p for p, _ in points],
                                    weights=[x for _, x in points])[0]
            operator = self.w.operator(point)
            client_id = self.ext_client(who, opened) if who else None
            order = _Order(kind="external", bike_id=None, object_note=obj,
                           payer="client", client_id=client_id, status="in_work",
                           tech=self.w.mechanic(point), complaint=complaint,
                           opened_at=opened, closed_at=None, location=point,
                           created_by=operator.actor, outcome=outcome)
            t = opened + timedelta(minutes=rng.randint(20, 90))
            for title, part_title, qty in works:
                order.lines += self.work_lines(title, part_title, qty, t, sheet="ext",
                                               client_pays=True)
                t += timedelta(minutes=rng.randint(10, 40))
            total = logic.order_totals_client(
                [{"price": ln.price, "qty": ln.qty} for ln in order.lines])
            order.estimate = total
            ready = max(ln.at for ln in order.lines)
            if outcome in ("approve", "in_work", "declined"):
                # Смета ушла в бота: наряд ждёт ответа клиента.
                order.estimate_sent_at = self.later(ready, 0.1, 0.6)
                order.status = "approve"
            if outcome == "in_work":
                order.approved_at = self.later(order.estimate_sent_at, 3, 8)
                order.approved_by = "клиент"
                order.status = "in_work"
            elif outcome == "declined":
                order.declined_at = self.later(order.estimate_sent_at, 18, 26)
                order.closed_at = order.declined_at
                order.status = "cancelled"
                order.note = "Клиент отказался: дорого, починит сам"
            elif outcome in ("paid", "invoice", "unpaid"):
                # Согласовано у стойки, забрал через несколько часов - сутки.
                order.approved_at = opened + timedelta(minutes=rng.randint(5, 20))
                order.approved_by = operator.actor
                order.closed_at = self.later(ready, 2, 26)
                order.status = "done"
                if outcome == "paid":
                    order.paid_at = self.later(order.closed_at, 0.2, 5)
            for stamp in ("estimate_sent_at", "approved_at", "declined_at",
                          "closed_at", "paid_at"):
                value = getattr(order, stamp)
                if value is not None and value >= self.now:
                    raise RuntimeError(f"чужой наряд «{obj}»: {stamp} в будущем")
            self.orders.append(order)

    def ext_client(self, who: str, t: datetime) -> int:
        """Карточка курьера со своей техникой. Телефон - из общего
        раздатчика: номер клиента уникален во всей базе."""
        if who in self.client_of:
            return self.client_of[who]
        name, phone = self.w.people.person()
        channel, employer = EXT_CLIENTS[who]
        cid = self.nid("clients")
        created = t - timedelta(minutes=self.rng.randint(3, 15))
        self.clients.append({
            "id": cid, "full_name": name, "phone": phone, "created_at": created,
            "channel": channel, "employer": employer,
            "note": "Сторонний ремонт: своя техника, в прокате не был"})
        self.client_of[who] = cid
        return cid

    # ─────────────── номера и итоги ───────────────

    def number(self) -> None:
        """Номера РЕМ- по времени открытия: сквозной счётчик панели."""
        self.orders.sort(key=lambda o: (o.opened_at, o.bike_id or 0, o.object_note or ""))
        for order in self.orders:
            order.id = self.nid("work_orders")
            order.no = logic.order_no(self.nno("work_orders"))

    # ─────────────── склад ───────────────

    def moment(self, days_from_start: int | None, days_before_today: int | None
               ) -> datetime:
        day = (self.S.date() + timedelta(days=days_from_start)
               if days_from_start is not None
               else self.w.today - timedelta(days=days_before_today or 0))
        return at(day, self.rng.uniform(11, 17))

    def stock(self) -> None:
        """Приходы под расход, затем проход по времени: остаток и
        средневзвешенная себестоимость каждой позиции на каждый момент."""
        rng = self.rng
        mech = self.w.staff.get("mechanic")
        mech_by = mech.actor if mech else "staff:demo"
        out: dict[str, list[_Event]] = defaultdict(list)
        for order in self.orders:
            for line in order.lines:
                if line.part is not None:
                    out[line.part.title].append(_Event(
                        at=line.at, part=line.part, kind="order", qty=-line.qty,
                        line=line, order=order, note=f"Наряд {order.no}",
                        by=order.tech.actor if order.tech else order.created_by))
        for start, before, title, qty, reason in WRITE_OFFS:
            part = self.parts[title]
            out[title].append(_Event(at=self.moment(start, before), part=part,
                                     kind="write_off", qty=-qty, note=reason, by=mech_by))
        for start, before, title, qty, reason in COUNTS:
            part = self.parts[title]
            out[title].append(_Event(at=self.moment(start, before), part=part,
                                     kind="count", qty=qty, note=reason, by=mech_by))

        # Поставки: первая - начальный остаток при переходе на CRM, затем
        # раз в две недели, последняя - не позже чем за три дня до сегодня.
        times = [self.S + timedelta(hours=8, minutes=20)]
        day = self.S.date() + timedelta(days=14)
        while day <= self.w.today - timedelta(days=3):
            times.append(at(day, rng.uniform(11, 16)))
            day += timedelta(days=rng.randint(12, 16))
        plan: list[dict[str, int]] = [{} for _ in times]
        for title, part in self.parts.items():
            events = sorted(out.get(title, []), key=lambda e: e.at)
            received = 0
            for k, when in enumerate(times):
                end = times[k + 1] if k + 1 < len(times) else self.now + DAY
                before = received + sum(e.qty for e in events if e.at < when)
                need = -sum(e.qty for e in events if when <= e.at < end and e.qty < 0)
                # Две последние поставки добирают «низкие» ровно под расход:
                # остаток прошлых окон тает, к сегодня он ниже неснижаемого.
                late = k >= len(times) - 2
                if title in STALE:
                    qty = STALE[title] if k == 0 else 0
                elif title in ON_DEMAND or (late and title in LOW_AT_END):
                    qty = max(need - before, 0)
                else:
                    target = need + part.min_stock + math.ceil(need * 0.25)
                    qty = max(target - before, 0)
                    qty = math.ceil(qty / part.pack) * part.pack if qty else 0
                if qty:
                    plan[k][title] = qty
                    received += qty
        for k, when in enumerate(times):
            if not plan[k]:
                continue
            doc = {"kind": "receipt", "at": when, "k": k,
                   "supplier_id": self.suppliers[k % len(self.suppliers)],
                   "by": "staff:demo" if k == 0 else mech_by, "lines": []}
            months = (when - self.S).days / 30
            for title in sorted(plan[k], key=lambda x: self.parts[x].id):
                part = self.parts[title]
                drift = D(str(1 + 0.01 * months)) * D(str(rng.uniform(0.97, 1.04)))
                price = (part.base * drift).quantize(D(1))
                price = _round10(price) if part.base >= 200 else price
                event = _Event(at=when, part=part, kind="receipt", qty=plan[k][title],
                               price=price, doc=doc, by=doc["by"])
                doc["lines"].append(event)
                out[title].append(event)
            self.docs.append(doc)
        for doc in self.docs_of_write_offs(out):
            self.docs.append(doc)

        # Проход по времени: приход раньше расхода в ту же секунду.
        order_of = {"receipt": 0, "count": 1, "write_off": 2, "order": 3}
        for title, events in out.items():
            part = self.parts[title]
            events.sort(key=lambda e: (e.at, order_of[e.kind]))
            stock, cost = 0, part.base
            for e in events:
                if e.kind == "receipt":
                    cost = logic.average_cost(stock, cost, e.qty, e.price)
                    e.cost = e.price
                else:
                    e.cost = logic.to_money(cost)
                stock += e.qty
                if stock < 0:
                    raise RuntimeError(f"склад ушёл в минус: {title} на {e.at}")
                if e.line is not None:
                    e.line.parts_cost = e.cost
            part.cost = logic.to_money(cost)
            self.events += events
        self.events.sort(key=lambda e: (e.at, order_of[e.kind], e.part.id))
        for e in self.events:
            e.id = self.nid("part_moves")
            if e.line is not None:
                e.line.move_id = e.id

    def docs_of_write_offs(self, out: dict[str, list[_Event]]) -> list[dict]:
        docs = []
        for events in out.values():
            for e in events:
                if e.kind == "write_off":
                    doc = {"kind": "write_off", "at": e.at, "supplier_id": None,
                           "by": e.by, "lines": [e], "note": e.note}
                    e.doc = doc
                    docs.append(doc)
        return docs

    def finish_docs(self) -> None:
        """Номера ПРХ/СПС по времени (у каждого вида свой счётчик) и суммы:
        приход - по цене прихода, списание - по себестоимости."""
        self.docs.sort(key=lambda d: (d["at"], d["kind"]))
        for doc in self.docs:
            doc["id"] = self.nid("part_docs")
            key = "part_docs" if doc["kind"] == "receipt" else "part_docs:write_off"
            doc["no"] = logic.doc_no(doc["kind"], self.nno(key))
            if doc["kind"] == "receipt":
                doc["total"] = sum((e.price * e.qty for e in doc["lines"]), D(0))
            else:
                doc["total"] = sum((e.cost * -e.qty for e in doc["lines"]), D(0))

    def totals(self) -> None:
        """Итоги наряда - logic.order_totals, как у кнопки «Закрыть»; у
        закрытого своего - запись ремонта в журнал велосипеда."""
        closed = []
        for order in self.orders:
            items = [{"price": ln.price, "qty": ln.qty, "parts_cost": ln.parts_cost,
                      "labor_cost": ln.labor_cost} for ln in order.lines]
            sums = logic.order_totals(items)
            if order.status == "done":
                order.total, order.cost = sums["total"], sums["cost"]
                if order.bike_id is not None and (order.lines or order.cost > 0):
                    closed.append(order)
            else:
                # Открытый наряд итогов не знает: их считает закрытие.
                order.total = order.cost = D(0)
        closed.sort(key=lambda o: (o.closed_at, o.id))
        for order in closed:
            order.log_id = self.nid("bike_log")
            by = order.tech.actor if order.tech else order.created_by
            self.bike_log.append((order.log_id, order.bike_id, "repair",
                                  f"Наряд {order.no}", order.cost, by, order.closed_at))
            for line in order.lines:
                if not line.node:
                    continue
                self.repair_items.append((
                    self.nid("repair_items"), order.log_id, order.bike_id, line.node,
                    logic.to_money(line.parts_cost * line.qty),
                    logic.to_money(line.labor_cost * line.qty), line.title,
                    order.closed_at))
        for order in self.orders:
            for line in sorted(order.lines, key=lambda ln: ln.at):
                line.id = self.nid("work_order_items")

    # ─────────────── заказы поставщику и счёт ───────────────

    def supplier_orders(self) -> None:
        """Каждая вторая поставка пришла заказом ЗАП; один заказ отменён,
        один едет от поставщика (в нём контроллер для ждущего наряда),
        один собирается."""
        rng = self.rng
        mech = self.w.staff.get("mechanic")
        by = mech.actor if mech else "staff:demo"
        rows = []
        for doc in self.docs:
            if doc["kind"] != "receipt" or doc["k"] % 2 == 0:
                continue
            created = doc["at"] - timedelta(days=3) + timedelta(hours=rng.uniform(-2, 2))
            items = [(e.part, e.qty, e.price,
                      "min_stock" if e.part.min_stock else "manual", None)
                     for e in doc["lines"]]
            rows.append({"supplier_id": doc["supplier_id"], "status": "received",
                         "created_at": created, "ordered_at": created + DAY,
                         "closed_at": doc["at"], "doc": doc, "note": None, "by": by,
                         "items": items})
        receipts = [d for d in self.docs if d["kind"] == "receipt"]
        if len(receipts) > 4:
            when = receipts[3]["at"] + timedelta(days=4, hours=rng.uniform(0, 4))
            items = [(self.parts["Подшипники вилки, комплект"], 2,
                      self.parts["Подшипники вилки, комплект"].base, "manual", None),
                     (self.parts["Сигнал звуковой"], 2,
                      self.parts["Сигнал звуковой"].base, "manual", None)]
            rows.append({"supplier_id": self.suppliers[1], "status": "cancelled",
                         "created_at": when, "ordered_at": when + timedelta(hours=2),
                         "closed_at": when + timedelta(days=2), "doc": None,
                         "note": "Поставщик не подтвердил сроки - взяли следующей "
                                 "поставкой", "by": by, "items": items})
        # В пути: контроллер под ждущий наряд и то, что ниже неснижаемого.
        stock = defaultdict(int)
        for e in self.events:
            stock[e.part.title] += e.qty
        items = []
        if self.waiting is not None:
            part = self.parts[WAIT_PART]
            items.append((part, 2, part.cost, "order", self.waiting))
        below = [p for p in self.parts.values()
                 if p.title not in STALE and stock[p.title] < p.min_stock]
        for part in below:
            qty = max(part.min_stock * 2 - stock[part.title], part.pack)
            if all(part is not i[0] for i in items):
                items.append((part, qty, part.cost, "min_stock", None))
        base = (self.waiting.opened_at + timedelta(hours=1.5) if self.waiting
                else self.now - timedelta(days=2))
        created = min(base, self.now - timedelta(minutes=40))
        rows.append({"supplier_id": self.suppliers[0], "status": "ordered",
                     "created_at": created,
                     "ordered_at": min(created + timedelta(minutes=15),
                                       self.now - MINUTE),
                     "closed_at": None, "doc": None, "by": by, "items": items,
                     "note": "Контроллер - под наряд, остальное - добор до "
                             "неснижаемого"})
        # Собирается: мелочь, вписанная руками вчера вечером.
        created = min(at(self.w.today - DAY, 18.5), self.now - timedelta(hours=1))
        items = [(self.parts["Грипсы, пара"], 10, self.parts["Грипсы, пара"].cost,
                  "manual", None),
                 (self.parts["Клеммы и термоусадка, комплект"], 10,
                  self.parts["Клеммы и термоусадка, комплект"].cost, "manual", None)]
        rows.append({"supplier_id": None, "status": "new", "created_at": created,
                     "ordered_at": None, "closed_at": None, "doc": None, "by": by,
                     "items": items, "note": None})
        rows.sort(key=lambda r: r["created_at"])
        for row in rows:
            row["id"] = self.nid("part_orders")
            row["no"] = logic.part_order_no(self.nno("part_orders"))
            row["total"] = (D(0) if row["status"] == "new" else
                            sum((logic.to_money(i[2]) * i[1] for i in row["items"]), D(0)))
            if row["doc"] is not None:
                row["doc"]["note"] = f"Заказ {row['no']}"
        for doc in self.docs:
            if doc["kind"] == "receipt" and not doc.get("note"):
                doc["note"] = ("Начальный остаток склада при переходе на CRM"
                               if doc["k"] == 0 else
                               f"Поставка по накладной № {100 + doc['k'] * 7} (демо)")
        self.part_orders = rows
        if self.waiting is not None:
            ordered = next(r for r in rows if r["status"] == "ordered")
            self.waiting.note = f"Ждём контроллер: заказ {ordered['no']} в пути"

    def invoice(self) -> None:
        """Счёт за ремонт по ссылке: оплачен - у наряда paid_at, в журнал
        аренды не пишется ничего (ledger_id пуст)."""
        rng = self.rng
        for order in self.orders:
            if order.outcome != "invoice":
                continue
            created = order.closed_at + timedelta(minutes=rng.randint(10, 30))
            paid = created + timedelta(hours=rng.uniform(1, 5))
            order.paid_at = paid
            what = order.object_note or "техника"
            self.pay_orders.append({
                "id": self.nid("pay_orders"), "no": logic.pay_no(self.nno("pay_orders")),
                "client_id": order.client_id, "amount": order.total,
                "purpose": f"Ремонт {what}, наряд {order.no}", "created_at": created,
                "sent_at": created + MINUTE, "paid_at": paid,
                "by": self.w.operator(order.location).actor, "work_order_id": order.id})


# ─────────────────────────── пересчёт ───────────────────────────

async def _stock_take(conn: asyncpg.Connection, w: World, rng: random.Random,
                      ids: dict[str, int], nos: dict[str, int]) -> None:
    """Закрытая ведомость ПРТ по всему парку в первые дни прошлого месяца,
    до открытия точек. Первые дни, а не конец: номер ПРТ сквозной, и
    ведомость, которую позже заводит seed_extras (по точке, 20-го), идёт
    следующим номером и позже по времени, а не наоборот.

    Ожидаемое - снимок на её начало по журналам статусов и мест, как
    снимает панель (logic.expected_bikes): свободные, в ремонте, на ТО и в
    брони. Недостачу в потери не переводили - это галочка оператора, а не
    автомат; лишние - велосипед, который числится в аренде (курьер оставил
    на ночь), и номер, которого в парке нет. Статусы техники ведомость не
    меняет - журналы ядра остаются как есть."""
    first = (w.today.replace(day=1) - DAY).replace(day=1)
    day = first + timedelta(days=rng.randint(2, 6))
    started = at(day, rng.uniform(8.9, 9.1))
    closed = started + timedelta(minutes=rng.randint(35, 50))
    rows = await conn.fetch(
        """
        select b.id, b.code,
               (select s.to_status from crm.bike_status_log s
                 where s.bike_id = b.id and s.changed_at <= $1
                 order by s.changed_at desc, s.id desc limit 1) as status,
               (select l.to_location from crm.bike_location_log l
                 where l.bike_id = b.id and l.changed_at <= $1
                 order by l.changed_at desc, l.id desc limit 1) as location
          from crm.bikes b
         order by b.code
        """, started)
    snapshot = [dict(r) for r in rows if r["status"] is not None]
    expected = logic.expected_bikes(snapshot, scope="all")
    if len(expected) < 3:
        return
    free = [b for b in expected if b["status"] == "available"]
    lost = rng.choice(free or expected)
    rented = [b for b in snapshot if b["status"] == "rented"]
    take_id = ids["stock_takes"] = ids["stock_takes"] + 1
    nos["stock_takes"] += 1
    items = []
    for b in expected:
        if b["id"] == lost["id"]:
            where = (f"на точке {b['location']}" if b["location"]
                     else "без точки")
            items.append((b["id"], None, "missing",
                          f"Не нашли: по журналу {where}", started))
        else:
            items.append((b["id"], None, "found", None, started))
    t = started + timedelta(minutes=rng.randint(15, 30))
    if rented:
        other = rng.choice(rented)
        items.append((other["id"], other["code"], "extra",
                      "Числится: " + logic.BIKE_STATUSES["rented"], t))
    items.append((None, "МБ-1070", "extra", "Нет такого в парке", t + MINUTE * 4))
    counts = logic.take_counts([{"state": x[2]} for x in items])
    await conn.execute(
        """
        insert into crm.stock_takes (id, no, scope, location, status, expected, found,
                                     missing, extra, note, created_by, started_at,
                                     closed_at, what)
        values ($1, $2, 'all', null, 'done', $3, $4, $5, $6, $7, 'staff:demo', $8, $9,
                'bikes')
        """, take_id, logic.take_no(nos["stock_takes"]), counts["total"],
        counts["found"], counts["missing"], counts["extra"],
        "Пересчёт всего парка перед закрытием месяца", started, closed)
    records = []
    for bike_id, code, state, note, created in items:
        ids["stock_take_items"] += 1
        records.append((ids["stock_take_items"], take_id, bike_id, code, state, note,
                        created))
    await _copy(conn, "stock_take_items",
                ["id", "take_id", "bike_id", "code", "state", "note", "created_at"],
                records)


# ─────────────────────────── запись ───────────────────────────

async def _copy(conn: asyncpg.Connection, table: str, columns: list[str],
                records: list[tuple]) -> None:
    if records:
        await conn.copy_records_to_table(table, schema_name="crm", columns=columns,
                                         records=records)


async def _write(conn: asyncpg.Connection, s: _Service) -> None:
    """По порядку внешних ключей: поставщики и склад, клиенты, журнал
    ремонтов, наряды, движения, строки нарядов, заказы, счёт."""
    await _copy(conn, "suppliers", ["id", "name", "phone", "note", "active", "created_at"],
                [(sid, name, phone, note, True, s.S - timedelta(days=30))
                 for sid, (name, phone, note) in zip(s.suppliers, SUPPLIERS, strict=True)])
    await _copy(conn, "parts",
                ["id", "title", "node", "unit", "cost", "price", "min_stock", "active",
                 "created_at"],
                [(p.id, p.title, p.node, p.unit, p.cost, p.price, p.min_stock, True,
                  s.S - timedelta(days=2)) for p in s.parts.values()])
    await _copy(conn, "part_docs",
                ["id", "no", "kind", "supplier_id", "total", "note", "created_by",
                 "created_at"],
                [(d["id"], d["no"], d["kind"], d["supplier_id"], logic.to_money(d["total"]),
                  d.get("note"), d["by"], d["at"]) for d in s.docs])
    await _copy(conn, "clients",
                ["id", "full_name", "phone", "status", "note", "source", "created_at",
                 "updated_at", "channel", "employer"],
                [(c["id"], c["full_name"], c["phone"], "active", c["note"], "manual",
                  c["created_at"], c["created_at"], c["channel"], c["employer"])
                 for c in s.clients])
    await _copy(conn, "bike_log",
                ["id", "bike_id", "kind", "note", "cost", "created_by", "created_at"],
                s.bike_log)
    await _copy(conn, "work_orders",
                ["id", "no", "bike_id", "object_note", "payer", "client_id", "status",
                 "tech_id", "complaint", "estimate", "total", "cost", "paid_at", "note",
                 "created_by", "opened_at", "closed_at", "log_id", "estimate_sent_at",
                 "approved_at", "approved_by", "declined_at", "location"],
                [(o.id, o.no, o.bike_id, o.object_note, o.payer, o.client_id, o.status,
                  o.tech.id if o.tech else None, o.complaint, logic.to_money(o.estimate),
                  o.total, o.cost, o.paid_at, o.note, o.created_by, o.opened_at,
                  o.closed_at, o.log_id, o.estimate_sent_at, o.approved_at,
                  o.approved_by, o.declined_at, o.location) for o in s.orders])
    await _copy(conn, "part_moves",
                ["id", "part_id", "kind", "qty", "cost", "doc_id", "order_id", "note",
                 "created_by", "created_at"],
                [(e.id, e.part.id, e.kind, e.qty, logic.to_money(e.cost),
                  e.doc["id"] if e.doc else None, e.order.id if e.order else None,
                  e.note, e.by, e.at) for e in s.events])
    rows = []
    for o in s.orders:
        for ln in o.lines:
            rows.append((ln.id, o.id, ln.work_type_id, ln.title, ln.node, ln.qty,
                         logic.to_money(ln.price), logic.to_money(ln.parts_cost),
                         logic.to_money(ln.labor_cost), ln.note, ln.at, ln.move_id))
    rows.sort()
    await _copy(conn, "work_order_items",
                ["id", "order_id", "work_type_id", "title", "node", "qty", "price",
                 "parts_cost", "labor_cost", "note", "created_at", "move_id"], rows)
    await _copy(conn, "repair_items",
                ["id", "log_id", "bike_id", "node", "parts_cost", "labor_cost", "note",
                 "created_at"], s.repair_items)
    await _copy(conn, "part_orders",
                ["id", "no", "supplier_id", "status", "total", "note", "created_by",
                 "created_at", "ordered_at", "closed_at", "doc_id"],
                [(r["id"], r["no"], r["supplier_id"], r["status"], r["total"], r["note"],
                  r["by"], r["created_at"], r["ordered_at"], r["closed_at"],
                  r["doc"]["id"] if r["doc"] else None) for r in s.part_orders])
    items = []
    for r in s.part_orders:
        for part, qty, price, source, order in r["items"]:
            s.ids["part_order_items"] += 1
            items.append((s.ids["part_order_items"], r["id"], part.id, qty,
                           logic.to_money(price), source, order.id if order else None,
                           r["created_at"]))
    await _copy(conn, "part_order_items",
                ["id", "order_id", "part_id", "qty", "price", "source", "work_order_id",
                 "created_at"], items)
    await _copy(conn, "pay_orders",
                ["id", "no", "client_id", "amount", "purpose", "kind", "status",
                 "provider", "created_by", "created_at", "sent_at", "paid_at",
                 "checked_at", "work_order_id"],
                [(p["id"], p["no"], p["client_id"], p["amount"], p["purpose"], "repair",
                  "paid", "tochka", p["by"], p["created_at"], p["sent_at"], p["paid_at"],
                  p["paid_at"], p["work_order_id"]) for p in s.pay_orders])


async def _max_numbers(conn: asyncpg.Connection) -> dict[str, int]:
    """Последние номера документов: модуль продолжает счётчики панели,
    а не начинает с единицы - в базе к этому моменту может что-то быть."""
    sql = ("select coalesce(max(substring(no from '[0-9]+$')::bigint), 0) "
           "from crm.{table}")
    nos = {}
    for table in ("work_orders", "part_orders", "pay_orders", "stock_takes"):
        nos[table] = int(await conn.fetchval(sql.format(table=table)))
    for key, kind in (("part_docs", "receipt"), ("part_docs:write_off", "write_off")):
        nos[key] = int(await conn.fetchval(
            sql.format(table="part_docs") + " where kind = $1", kind))
    return nos


async def populate(conn: asyncpg.Connection, world: World) -> None:
    """Наряды за всю историю, склад с приходами и расходом, заказы
    поставщику, смета на согласовании, счёт за ремонт, пересчёт.

    Свой генератор от одного числа из world.rng: сколько случайности съел
    сервис, модулю после него (seed_extras) не важно - порядок один."""
    rng = random.Random(world.rng.getrandbits(64))
    ids = {t: int(await conn.fetchval(f"select coalesce(max(id), 0) from crm.{t}"))
           for t in _TABLES}
    nos = await _max_numbers(conn)
    types = {r["title"]: dict(r) for r in await conn.fetch(
        "select id, title, node, price, parts_price, price_ext, parts_price_ext "
        "from crm.work_types where active")}
    # asyncpg отдаёт моменты в UTC, а рабочие часы сид считает по Москве.
    since = {int(r["bike_id"]): r["at"].astimezone(MSK) for r in await conn.fetch(
        "select bike_id, max(changed_at) as at from crm.bike_status_log group by bike_id")}
    s = _Service(world, rng, ids, nos, types, since)
    s.reference()
    s.own_orders()
    s.external_orders()
    s.number()
    s.stock()
    s.finish_docs()
    s.totals()
    s.supplier_orders()
    s.invoice()
    await _write(conn, s)
    await _stock_take(conn, world, rng, ids, nos)
    for table in _TABLES:
        await conn.execute(
            f"select setval(pg_get_serial_sequence('crm.{table}', 'id'), "
            f"greatest((select max(id) from crm.{table}), 1))")
