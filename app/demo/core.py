"""Ядро демо-сида: точки, люди, закупки, парк и день за днём аренды с
деньгами и кассой.

Почему симуляция, а не готовые строки: три числа панели считаются по
журналам (статусы, места, деньги), и сойтись между собой и с «По точкам»
они могут, только если все журналы - следствие одной истории. История
идёт событиями по времени (выдача, начисление, платёж, возврат, ремонт,
замена, розыск), журналы пишутся по ходу, а база получает их одним махом.

Сервисы панели ставят now(), поэтому ядро пишет SQL само, с явными
датами. Всё случайное - из world.rng: одно зерно, «сегодня» и «сейчас»
дают одну и ту же базу.
"""

from __future__ import annotations

import heapq
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from ..crm import logic
from ..logic import contract_number
from .world import (
    Battery,
    Bike,
    Client,
    Payment,
    Rental,
    ServiceInterval,
    Shift,
    World,
    at,
)

D = Decimal
DAY = timedelta(days=1)

# ───────────────────────── справочники мира ─────────────────────────

P1, P2, P3 = "Павлюхина", "Адоратского", "Проспект Победы"
MONSTER = "Monster Truck + (Два АКБ)"
MONSTER_A = "Monster Truck + с задними амортизаторами"
KUGOO = "Kugoo V3 Pro (Два АКБ)"
KUGOO_P = "Kugoo V3 Pro + (Два АКБ)"
BAT_MONSTER = "АКБ Monster 60V 20Ah"
BAT_KUGOO = "АКБ Kugoo 60V 21Ah"

# Третья точка вымышленная целиком: адрес с пометкой «демо», телефон с
# кодом +7 000, которого нет ни у кого, координаты - Казань, проспект Победы.
P3_SPEC = {"address": "г. Казань, пр. Победы, 100 (демо)", "lat": 55.750540,
           "lon": 49.212610, "phone": "+7 (000) 012-34-56",
           "hours": "пн-вс: 10:00-19:00",
           "public_title": "Май Байк — сервис и аренда, Проспект Победы (демо)"}
# Две настоящие точки схема заводит с рабочим телефоном проката; в демо
# у них вымышленный - звонок покупателя франшизы не должен уходить на
# точку, где его не ждут.
POINT_PHONES = {P1: "+7 (000) 010-10-10", P2: "+7 (000) 020-20-20"}
P3_OPEN_DAYS = 60          # точка открыта примерно столько дней назад
CASH_DAYS = 60             # касса ведётся сменами последние N дней

# (название, бренд, вольты, ампер-часы, цена, срок службы в месяцах)
BATTERY_MODELS = (
    (BAT_MONSTER, "Monster", 60, D("20"), D("14500"), 15),
    (BAT_KUGOO, "Kugoo", 60, D("21"), D("16000"), 15),
)
BATTERY_OF = {MONSTER: BAT_MONSTER, MONSTER_A: BAT_MONSTER,
              KUGOO: BAT_KUGOO, KUGOO_P: BAT_KUGOO}
# Цена доп. аккумулятора по сроку: без тарифа на срок аренды батарея не
# выдаётся вовсе (logic.battery_extra_price), поэтому все три срока.
BATTERY_TARIFFS = {BAT_MONSTER: {7: 800, 14: 1500, 30: 2900},
                   BAT_KUGOO: {7: 900, 14: 1700, 30: 3200}}
PERIOD_NAMES = {7: "Неделя", 14: "Две недели", 30: "Месяц"}


@dataclass(frozen=True)
class _Batch:
    """Закупка ЗАК: anchor S - от начала истории, T - от сегодня, P3 - от
    открытия третьей точки (её парк приходит к открытию, а не к дате)."""
    anchor: str
    offset: int
    model: str
    price: int
    months: int
    residual: int
    split: tuple[tuple[str, int], ...]
    note: str
    spare_batteries: int = 0
    assembly: bool = False      # так и стоит «на сборке» к сегодняшнему дню


BATCHES = (
    _Batch("S", -400, MONSTER, 58000, 24, 4000, ((P1, 25), (P2, 20)),
           "Первая партия, демо", 6),
    _Batch("S", -260, KUGOO, 67000, 24, 5000, ((P1, 22), (P2, 18)),
           "Kugoo под Яндекс.Еду, демо", 6),
    _Batch("S", -130, MONSTER_A, 63000, 24, 5000, ((P1, 16), (P2, 14)),
           "Monster с амортизаторами, демо", 4),
    _Batch("S", -40, KUGOO_P, 72000, 30, 6000, ((P1, 14), (P2, 11)),
           "Kugoo Pro+ перед сезоном, демо", 4),
    _Batch("S", 35, KUGOO, 68500, 24, 5000, ((P1, 9), (P2, 7)),
           "Докупка в сезон, демо", 4),
    _Batch("P3", -4, MONSTER, 60500, 24, 4000, ((P3, 28),),
           "Парк новой точки на Победы, демо", 8),
    _Batch("T", -22, KUGOO_P, 73000, 30, 6000, ((P1, 4), (P3, 4)),
           "Докупка Kugoo Pro+, демо", 4),
    _Batch("T", -1, KUGOO_P, 73000, 30, 6000, ((P1, 3),),
           "Партия на сборке, демо", 0, assembly=True),
)
SUPPLIER_BIKES = "ООО «Демо-Вело» (демо)"
RETURN_NOTE = "Сдал велосипед, без замечаний"
# Та же формулировка, что у кнопки «Признать потерянным» (service.declare_theft).
LOST_NOTE = "Признан потерянным: клиент не вернул велосипед"

# ───────────────────────── калибровка ─────────────────────────
#
# Три числа за 30 дней держатся на этих ручках: ожидание выдачи после
# возврата (простой «свободен»), доля и длина ремонта и ТО, смесь сроков
# и доп. аккумуляторов (чек). Точки нарочно разные: Павлюхина лучшая,
# новая точка на Победы хуже - отчёт «По точкам» должен что-то показать.

POINT_WAIT = {P1: 0.5, P2: 0.72, P3: 1.4}
# Доля простоя, к которой тянется точка: лишние свободные велосипеды
# оператор раздаёт быстрее (объявления, звонки старым клиентам), при
# нехватке курьеры ждут. Без этой обратной связи простой одной
# реализации гулял бы на ±2 пункта от дня к дню.
IDLE_TARGET = {P1: 0.08, P2: 0.10, P3: 0.15}
EXTRA_SHARE = {P1: 0.5, P2: 0.36, P3: 0.14}
PERIOD_MIX = {P1: ((7, 0.89), (14, 0.07), (30, 0.04)),
              P2: ((7, 0.9), (14, 0.06), (30, 0.04)),
              P3: ((7, 0.84), (14, 0.09), (30, 0.07))}
# Сколько периодов берут: средняя аренда около трёх недель, а продления
# дают около двух третей выручки (1 - 1/3.25 недели).
PERIODS = {7: ((1, 0.24), (2, 0.24), (3, 0.17), (4, 0.12), (5, 0.08), (6, 0.06),
               (8, 0.06), (10, 0.03)),
           14: ((1, 0.45), (2, 0.35), (3, 0.20)),
           30: ((1, 0.6), (2, 0.4))}
REPAIR_SHARE = 0.27        # после возврата в ремонт
MAINT_SHARE = 0.18         # после возврата на ТО
CLIENTS_TARGET = 520       # клиентов к сегодняшнему дню
EMPLOYERS = (("yandex", 0.42), ("samokat", 0.25), ("sbermarket", 0.15),
             ("delivery", 0.12), ("other", 0.06))
CHANNELS = (("avito", 0.28), ("2gis", 0.12), ("yandex_maps", 0.10),
            ("referral", 0.22), ("channel", 0.08), ("site", 0.10), ("other", 0.10))
EXPERIENCE = (("none", 0.2), ("under_year", 0.35), ("years_1_3", 0.3),
              ("over_3", 0.15))
REF_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

_SAME = object()


def _pick(rng, pairs):
    """Выбор по весам из ((значение, вес), ...)."""
    values, weights = zip(*pairs, strict=True)
    return rng.choices(values, weights=weights)[0]


def history_start(today: date) -> date:
    """Первое число месяца пятью месяцами раньше: отчёты показывают текущий
    месяц и пять прошлых, и ни один из них не должен быть обрезком."""
    month = today.month - 5
    year = today.year
    while month < 1:
        month += 12
        year -= 1
    return date(year, month, 1)


@dataclass
class _R:
    """Аренда изнутри симуляции: то, что в базу не идёт."""
    r: Rental
    plan: int | None           # сколько периодов возьмёт; None - не вернёт
    km: float                  # км в сутки у этого курьера
    since: datetime            # когда выдан велосипед на руках
    theft: dict | None = None
    skip_last: bool = False
    segments: list[dict] = field(default_factory=list)
    extras: list[dict] = field(default_factory=list)
    touched: datetime | None = None   # последнее изменение строки аренды


class Sim:
    """Симуляция. Состояние - в объектах World, журналы - в списках строк."""

    def __init__(self, w: World, ids: dict[str, int]) -> None:
        self.w = w
        self.rng = w.rng
        self.S = w.history_start.date()
        self.T = w.today
        self.now = w.now
        self.ids = ids
        self.heap: list[tuple] = []
        self.seq = 0
        self.status_rows: list[tuple] = []
        self.loc_rows: list[tuple] = []
        self.bstatus_rows: list[tuple] = []
        self.ledger_rows: list[tuple] = []
        self.move_rows: list[tuple] = []
        self.bike_extra: dict[int, dict[str, Any]] = {}   # checked и прочее
        self.bat_extra: dict[int, dict[str, Any]] = {}
        self.intents: dict[int, tuple] = {}
        self.last_ts: dict[int, datetime] = {}
        self.bat_last: dict[int, datetime] = {}
        self.token: dict[int, int | None] = {}
        self.balance: dict[int, D] = defaultdict(D)
        self.active: dict[int, _R] = {}                   # client -> аренда
        self.rx: dict[int, _R] = {}                       # rental -> аренда
        self.pool: list[tuple[int, datetime, str]] = []   # клиенты «вернутся»
        self.no_return: set[int] = set()
        self.shift_at: dict[tuple[str, date], Shift] = {}
        self.shift_cash: dict[int, D] = defaultdict(D)
        self.bikes: dict[int, Bike] = {}
        self.bats: dict[int, Battery] = {}
        self.intervals: dict[int, ServiceInterval] = {}   # bike -> идущий ремонт
        self.bat_intervals: dict[int, int] = {}
        self.theft_plan: list[dict] = []
        # Сегодняшние смены, которые к «сейчас» ещё не открыты: (час, точка).
        self.later_shifts: list[tuple[datetime, str]] = []
        self.carry: dict[str, D] = {}
        self.skip_plan: list[date] = []
        self.writeoff_plan: list[date] = []
        self.n_import = 0

    # ─────────────────────── служебное ───────────────────────

    def nid(self, table: str) -> int:
        self.ids[table] = self.ids.get(table, 0) + 1
        return self.ids[table]

    def push(self, t: datetime, kind: str, *args: Any) -> None:
        self.seq += 1
        heapq.heappush(self.heap, (t, self.seq, kind, args))

    def hours(self, day: date, lo: float, hi: float) -> datetime:
        return at(day, self.rng.uniform(lo, hi))

    def _after(self, last: dict[int, datetime], key: int, t: datetime) -> datetime:
        """Журнал одного велосипеда строго по времени: второе событие в ту же
        секунду сдвигается на секунду, иначе lead() в отчётах спорил бы с id."""
        prev = last.get(key)
        if prev is not None and t <= prev:
            t = prev + timedelta(seconds=1)
        last[key] = t
        return t

    def actor(self, point: str | None) -> str:
        return self.w.operator(point).actor

    def mech(self, point: str | None) -> str:
        return self.w.mechanic(point).actor

    # ─────────────────────── велосипед и батарея ───────────────────────

    def bike_created(self, b: Bike, t: datetime, by: str) -> None:
        self.bikes[b.id] = b
        self.w.bikes.append(b)
        t = self._after(self.last_ts, b.id, t)
        self.status_rows.append((self.nid("bike_status_log"), b.id, None, b.status, t,
                                 by, b.mileage_km))
        self.loc_rows.append((self.nid("bike_location_log"), b.id, None, b.point, t, by))
        self.bike_extra[b.id] = {"updated_at": t}

    def set_status(self, b: Bike, t: datetime, to: str, by: str, *,
                   point: Any = _SAME) -> datetime:
        """Смена статуса и, если надо, точки - одним моментом, как один
        UPDATE в панели пишет оба журнала с одним now()."""
        t = self._after(self.last_ts, b.id, t)
        self.status_rows.append((self.nid("bike_status_log"), b.id, b.status, to, t,
                                 by, b.mileage_km))
        if point is not _SAME and point != b.point:
            self.loc_rows.append((self.nid("bike_location_log"), b.id, b.point, point,
                                  t, by))
            b.point = point
        b.status = to
        self.bike_extra[b.id]["updated_at"] = t
        return t

    def move_bike(self, b: Bike, t: datetime, point: str, by: str) -> None:
        """Переезд свободного велосипеда: место меняется, статус - нет."""
        t = self._after(self.last_ts, b.id, t)
        self.loc_rows.append((self.nid("bike_location_log"), b.id, b.point, point, t, by))
        self.w.transfers.append((b.id, t, b.point, point))
        b.point = point
        self.bike_extra[b.id]["updated_at"] = t
        for bat in self.bats.values():
            if bat.bike_id == b.id and bat.status == "available":
                bat.point = point

    def bat_created(self, bat: Battery, t: datetime, by: str) -> None:
        self.bats[bat.id] = bat
        self.w.batteries.append(bat)
        t = self._after(self.bat_last, bat.id, t)
        self.bstatus_rows.append((self.nid("battery_status_log"), bat.id, None,
                                  bat.status, t, by))

    def bat_status(self, bat: Battery, t: datetime, to: str, by: str) -> None:
        t = self._after(self.bat_last, bat.id, t)
        self.bstatus_rows.append((self.nid("battery_status_log"), bat.id, bat.status,
                                  to, t, by))
        bat.status = to

    # ─────────────────────── деньги ───────────────────────

    def ledger(self, client_id: int, rental_id: int | None, kind: str, amount: D,
               t: datetime, *, method: str | None = None, pf: date | None = None,
               pt: date | None = None, note: str | None = None,
               by: str | None = None, shift_id: int | None = None) -> int:
        lid = self.nid("ledger")
        self.ledger_rows.append((lid, client_id, rental_id, kind, amount, method, pf,
                                 pt, note, by, t, shift_id))
        self.balance[client_id] += amount
        return lid

    def charge(self, x: _R, i: int, t: datetime) -> None:
        r = x.r
        pf = r.started_on + timedelta(days=r.period_days * (i - 1))
        pt = pf + timedelta(days=r.period_days)
        self.ledger(r.client_id, r.id, "charge", -r.price, t, pf=pf, pt=pt,
                    note=f"{r.tariff_name}: {logic.period_label(pf, pt)}", by="billing")
        r.billed_until = pt
        r.periods = i
        x.touched = t

    def shift_window(self, point: str, day: date) -> tuple[datetime, datetime] | None:
        shift = self.shift_at.get((point, day))
        if shift is None:
            return None
        lo = shift.opened_at + timedelta(minutes=5)
        hi = (shift.closed_at or self.now) - timedelta(minutes=5)
        return (lo, hi) if hi > lo else None

    def pay(self, x: _R, t: datetime, *, first: bool, bike_code: str = "") -> None:
        """Платёж за период. Наличные - только в окне смены своей точки и
        с её shift_id: иначе касса точки и журнал разошлись бы."""
        r = x.r
        rng = self.rng
        window = self.shift_window(r.point, t.date())
        if first:
            cash_ok = window is not None and window[0] <= t <= window[1]
            weights = (("cash", 0.36 if cash_ok else 0), ("sbp", 0.36), ("card", 0.14),
                       ("transfer", 0.14))
        else:
            weights = (("cash", 0.2 if window else 0), ("sbp", 0.45), ("card", 0.15),
                       ("transfer", 0.2))
        method = _pick(rng, weights)
        shift_id = None
        linked = first
        if method == "cash" and not first:
            # Наличные приносят на точку в часы кассы того же дня.
            t = window[0] + (window[1] - window[0]) * rng.random()
        if t > self.now:
            return
        by = self.actor(r.point)
        if method == "cash":
            shift = self.shift_at[(r.point, t.date())]
            shift_id = shift.id
            self.shift_cash[shift.id] += r.price
            note = "Продление, наличные"
        elif method == "card":
            by, note, linked = "эквайринг", "Оплата по ссылке", True
        elif method == "transfer":
            by, note = "staff:demo", "Перевод на расчётный счёт"
        else:
            note = "Перевод по СБП"
        if first:
            note = f"При выдаче № {bike_code}"
        lid = self.ledger(r.client_id, r.id if linked else None, "payment", r.price, t,
                          method=method, note=note, by=by, shift_id=shift_id)
        self.w.payments.append(Payment(
            ledger_id=lid, client_id=r.client_id, rental_id=r.id, amount=r.price,
            method=method, created_at=t, point=r.point, shift_id=shift_id, note=note,
            linked=linked))

    # ─────────────────────── клиенты ───────────────────────

    def new_client(self, t: datetime, point: str, *, source: str = "manual") -> Client:
        rng = self.rng
        w = self.w
        name, phone = w.people.person()
        created = t - timedelta(minutes=rng.uniform(8, 180)) if source != "import" else t
        w.contract_seq += 1
        client = Client(
            id=self.nid("clients"), full_name=name, phone=phone,
            employer=_pick(rng, EMPLOYERS), channel=_pick(rng, CHANNELS),
            experience=_pick(rng, EXPERIENCE), created_at=created, point=point,
            contract_no=contract_number(w.contract_seq, today=created.date()),
            source=source)
        if client.channel == "referral":
            agents = [c for c in w.clients[-200:]
                      if c.status == "active" and c.created_at < created - 10 * DAY]
            if agents:
                agent = rng.choice(agents)
                client.invited_by = agent.id
                client.invited_at = created - timedelta(minutes=rng.uniform(2, 90))
                if agent.ref_code is None:
                    agent.ref_code = self.ref_code()
            else:
                client.channel = "avito"
        if client.ref_code is None and rng.random() < 0.2:
            client.ref_code = self.ref_code()
        w.clients.append(client)
        return client

    def ref_code(self) -> str:
        taken = self.__dict__.setdefault("_codes", set())
        while True:
            code = "".join(self.rng.choice(REF_ALPHABET) for _ in range(6))
            if code not in taken:
                taken.add(code)
                return code

    def pick_client(self, t: datetime, point: str) -> Client:
        """Новый или вернувшийся: новых столько, чтобы к сегодня вышло
        CLIENTS_TARGET, остальные - курьеры, бравшие раньше."""
        span = max((self.now - self.w.history_start).total_seconds(), 1)
        done = (t - self.w.history_start).total_seconds() / span
        target = self.n_import + (CLIENTS_TARGET - self.n_import) * done
        if len(self.w.clients) >= target:
            ready = [i for i, (_, when, _p) in enumerate(self.pool) if when <= t]
            if ready:
                same = [i for i in ready if self.pool[i][2] == point]
                pick = self.rng.choice(same if same and self.rng.random() < 0.75
                                       else ready)
                cid = self.pool.pop(pick)[0]
                return self.w.client(cid)
        return self.new_client(t, point)

    # ─────────────────────── выдача ───────────────────────

    def take_batteries(self, b: Bike, point: str, *, extra: bool = False) -> list[Battery]:
        """Две батареи к велосипеду: свои первыми, потом ничьи, потом любые
        той же модели на этой точке - как их выбирает оператор на выдаче."""
        model = self.w.battery_for_model[b.model]
        free = [x for x in self.bats.values()
                if x.status == "available" and x.point == point and x.model == model]
        if extra:
            free = [x for x in free if x.bike_id != b.id]
            return free[:1] if len(free) > 3 else []
        free.sort(key=lambda x: (x.bike_id != b.id, x.bike_id is not None, x.id))
        return free[:2]

    def start_rental(self, b: Bike, t: datetime, client: Client, *, started_on: date,
                     imported: bool = False, theft: dict | None = None) -> _R:
        rng = self.rng
        w = self.w
        point = b.point
        assert point is not None
        period = _pick(rng, PERIOD_MIX[point]) if theft is None else 7
        if self.skip_plan and not imported and theft is None and point == P2 \
                and self.skip_plan[0] <= t.date():
            self.skip_plan.pop(0)
            period, plan, skip = 7, 3, True
        else:
            plan = _pick(rng, PERIODS[period]) if theft is None else None
            skip = False
        tariff = w.tariffs[("bike", b.model, period)]
        base = D(tariff["price"])
        by = "import" if imported else self.actor(point)
        rid = self.nid("rentals")
        r = Rental(id=rid, client_id=client.id, bike_id=b.id, bike_ids=[b.id],
                   point=point, started_on=started_on, created_at=t,
                   tariff_id=tariff["id"], tariff_name=tariff["name"],
                   period_days=period, base_price=base, price=base,
                   billed_until=started_on)
        x = _R(r=r, plan=plan, km=rng.uniform(45, 110), since=t, theft=theft,
               skip_last=skip)
        # Ездит с начала аренды: у перенесённой из таблицы пробег на
        # выдаче меньше сегодняшнего на уже прошедшие дни.
        ridden = int(x.km * (t.date() - started_on).days)
        x.segments.append({"id": self.nid("rental_bikes"), "bike_id": b.id,
                           "issued_on": started_on, "returned_on": None,
                           "mileage_start": max(b.mileage_km - ridden, 0),
                           "mileage_end": None, "reason": "Выдача", "by": by,
                           "created_at": t})
        w.rentals.append(r)
        self.rx[rid] = x
        self.active[client.id] = x
        self.token[b.id] = None
        if imported:
            # Перенесённая аренда: велосипед заведён сразу «в аренде», его
            # батареи - сразу у клиента, без секунды «свободна».
            bats = [self.make_battery(b, BATTERY_OF[b.model], point, t, "rented",
                                      b.purchased_on, by="import") for _ in range(2)]
        else:
            self.set_status(b, t, "rented", by)
            bats = self.take_batteries(b, point)
            for bat in bats:
                self.bat_status(bat, t, "rented", by)
        for bat in bats:
            bat.rental_id, bat.bike_id = rid, b.id
            r.battery_ids.append(bat.id)
        if not imported and theft is None and rng.random() < EXTRA_SHARE[point]:
            for bat in self.take_batteries(b, point, extra=True):
                price = D(w.tariffs[("battery", bat.model, period)]["price"])
                self.bat_status(bat, t, "rented", by)
                bat.rental_id, bat.bike_id = rid, b.id
                r.battery_ids.append(bat.id)
                r.extras.append(bat.id)
                r.price += price
                x.extras.append({"id": self.nid("rental_extras"), "battery_id": bat.id,
                                 "title": logic.extra_title("battery", bat.model),
                                 "price": price, "added_at": t, "added_by": by,
                                 "removed_at": None, "removed_by": None})
        # Первый период - сразу при выдаче, платёж через пару минут.
        self.charge(x, 1, t + timedelta(seconds=40))
        if imported:
            when = t + timedelta(minutes=1)
            method = _pick(rng, (("sbp", 0.5), ("transfer", 0.3), ("card", 0.2)))
            note = "Остаток из таблицы при переносе в CRM"
            if when <= self.now:
                lid = self.ledger(client.id, rid, "payment", r.price, when,
                                  method=method, note=note, by="import")
                self.w.payments.append(Payment(
                    ledger_id=lid, client_id=client.id, rental_id=rid, amount=r.price,
                    method=method, created_at=when, point=point, shift_id=None,
                    note=note, linked=True))
        else:
            self.push(t + timedelta(minutes=rng.uniform(1, 6)), "first_pay", rid,
                      b.code)
        paid = plan if theft is None else theft["paid"]
        last_paid = plan - 1 if skip else paid
        # Продления: начисление ночным проходом в день начала периода,
        # платёж - накануне вечером, в тот же день или с опозданием.
        horizon = plan if plan is not None else 2
        for i in range(2, horizon + 1):
            self.push_charge(x, i)
        for i in range(2, (last_paid or 0) + 1):
            pf = started_on + timedelta(days=period * (i - 1))
            roll = rng.random()
            if roll < 0.40:
                when = self.hours(pf - DAY, 18, 23.5)
            elif roll < 0.87:
                when = self.hours(pf, 8, 21)
            else:
                when = self.hours(pf + rng.randint(1, 4) * DAY, 9, 21)
            self.push(when, "pay", rid, i)
        if plan is not None:
            last_day = started_on + timedelta(days=period * plan - 1)
            self.push(self.hours(last_day, 9.5, 16), "return", rid)
        elif theft["search_delay"] is not None:
            overdue = started_on + timedelta(days=period * theft["paid"])
            search = self.hours(overdue + theft["search_delay"] * DAY, 11, 17)
            self.push(search, "search", rid)
            if theft.get("lost_delay") is not None:
                self.push(search + theft["lost_delay"] * DAY
                          + timedelta(hours=rng.uniform(-2, 2)), "lost", rid)
        return x

    def push_charge(self, x: _R, i: int) -> None:
        pf = x.r.started_on + timedelta(days=x.r.period_days * (i - 1))
        t = at(pf, 0.01 + (x.r.id % 180) / 3600)
        if pf == self.T:
            # Ночной проход уже прошёл к любому «сейчас»: у идущей аренды
            # billed_until обязан быть позже сегодня.
            t = min(t, self.now - timedelta(seconds=30))
        self.push(t, "charge", x.r.id, i)

    # ─────────────────────── расписание выдач ───────────────────────

    def wait_days(self, point: str, day: date) -> float:
        rng = self.rng
        roll = rng.random()
        if roll < 0.42:
            w = rng.uniform(0.05, 0.35)
        elif roll < 0.82:
            w = rng.uniform(0.7, 1.3)
        else:
            w = rng.uniform(1.5, 4.0)
        w *= POINT_WAIT[point] * self.pressure(point)
        opened = self.w.points[point].opened_on
        if opened > self.S:
            # Новая точка раскачивается: первые недели курьеры о ней не знают.
            age = (day - opened).days
            w *= 1 + 7 * max(0.0, 1 - age / 38)
        return w

    def pressure(self, point: str) -> float:
        """Множитель ожидания: простой точки выше цели - выдают быстрее."""
        fleet = idle = 0
        for b in self.bikes.values():
            if b.point == point and b.status in logic.OPERATIONAL_STATUSES:
                fleet += 1
                idle += b.status != "rented"
        if not fleet:
            return 1.0
        ratio = (idle / fleet) / IDLE_TARGET[point]
        return min(max(ratio ** -1.8, 0.3), 3.0)

    def schedule_rent(self, b: Bike, t_avail: datetime) -> None:
        """Следующая выдача велосипеда: через ожидание в рабочие часы точки.
        Иногда под заявку - тогда перед выдачей он стоит «забронирован»."""
        rng = self.rng
        point = b.point
        opened = at(self.w.points[point].opened_on, 10)
        start = max(t_avail, opened)
        t = start + timedelta(days=self.wait_days(point, start.date()))
        hour = (t - at(t.date())).total_seconds() / 3600
        if hour < 10:
            t = self.hours(t.date(), 10, 12)
        elif hour > 19.5:
            t = self.hours(t.date() + DAY, 10, 13)
        token = self.seq + 1
        self.token[b.id] = token
        gap = (t - t_avail).total_seconds() / 3600
        if gap > 14 and rng.random() < 0.35:
            self.push(t - timedelta(hours=rng.uniform(3, min(gap - 1, 22))),
                      "reserve", b.id, token)
        self.push(t, "rent", b.id, token)

    def after_service_or_return(self, b: Bike, t: datetime, *, rental: _R | None,
                                point: str) -> None:
        """Куда велосипед после возврата: ремонт, ТО или сразу в выдачу."""
        rng = self.rng
        roll = rng.random()
        if rental is not None and self.writeoff_plan and point in (P1, P2) \
                and self.writeoff_plan[0] <= t.date():
            self.writeoff_plan.pop(0)
            self.service(b, t, "repair", reason="accident", rental=rental,
                         days=rng.uniform(3, 6), outcome="written_off", point=point)
        elif roll < REPAIR_SHARE:
            r2 = rng.random()
            days = (rng.uniform(0.2, 1.2) if r2 < 0.5 else rng.uniform(1.5, 4.5)
                    if r2 < 0.86 else rng.uniform(5, 11))
            self.service(b, t, "repair", reason="return", rental=rental, days=days,
                         point=point)
        elif roll < REPAIR_SHARE + MAINT_SHARE:
            self.service(b, t, "maintenance", reason="return", rental=rental,
                         days=rng.uniform(0.3, 3.0), point=point)
        else:
            self.set_status(b, t, "available", self.actor(point), point=point)
            self.schedule_rent(b, t)

    def service(self, b: Bike, t: datetime, kind: str, *, reason: str,
                rental: _R | None, days: float, point: str,
                outcome: str | None = "available") -> None:
        t = self.set_status(b, t, kind, self.actor(point) if rental else self.mech(point),
                            point=point)
        self.open_interval(b, t, kind, reason=reason, rental=rental, days=days,
                           point=point, outcome=outcome)

    def open_interval(self, b: Bike, t: datetime, kind: str, *, reason: str,
                      rental: _R | None, days: float, point: str,
                      outcome: str | None = "available") -> None:
        """Ремонт или ТО с этого момента; конец - событием service_end."""
        interval = ServiceInterval(bike_id=b.id, kind=kind, start=t, end=None,
                                   point=point, reason=reason, outcome=None,
                                   rental_id=rental.r.id if rental else None,
                                   mileage_km=b.mileage_km)
        self.w.service_intervals.append(interval)
        self.intervals[b.id] = interval
        end = t + timedelta(days=days)
        hour = (end - at(end.date())).total_seconds() / 3600
        if hour < 9.5 or hour > 20:
            end = self.hours(end.date() + (DAY if hour > 20 else timedelta()), 10, 18)
        self.push(end, "service_end", b.id, outcome)

    # ─────────────────────── события ───────────────────────

    def run(self) -> None:
        while self.heap:
            t, _seq, kind, args = heapq.heappop(self.heap)
            if t > self.now:
                break
            getattr(self, "on_" + kind)(t, *args)

    def on_purchase(self, t: datetime, batch_no: int, batch: _Batch,
                    purchase: dict) -> None:
        rng = self.rng
        for point, count in batch.split:
            for _ in range(count):
                b = self.make_bike(batch_no, batch, purchase, point, t, status="new")
                self.bike_created(b, t, "staff:demo")
                for _ in range(2):
                    self.make_battery(b, BATTERY_OF[b.model], point, t, "new",
                                      b.purchased_on, by="staff:demo")
                t += timedelta(seconds=rng.randint(20, 60))
                if batch.assembly:
                    continue
                # До открытия точки велосипед ждёт «на сборке»: свободным
                # до первого дня работы он записал бы точке простой в
                # месяце, когда её ещё не было.
                when = self.hours(purchase["purchased_on"] + rng.randint(1, 3) * DAY,
                                  11, 18)
                opened = at(self.w.points[point].opened_on, 9.0)
                self.push(max(when, opened + timedelta(seconds=rng.randint(60, 900))),
                          "commission", b.id)
        for point, _count in batch.split[:1]:
            for _ in range(batch.spare_batteries):
                self.make_battery(None, BATTERY_OF[batch.model], point, t, "available",
                                  purchase["purchased_on"], by="staff:demo")

    def on_commission(self, t: datetime, bike_id: int) -> None:
        b = self.bikes[bike_id]
        if b.status != "new":
            return
        by = self.mech(b.point)
        t = self.set_status(b, t, "available", by)
        b.commissioned_at = t
        marks = {}
        for i, key in enumerate(logic.BIKE_PASSPORT):
            marks[key] = {"at": (t - timedelta(minutes=40 - 7 * i)).isoformat(), "by": by}
        self.bike_extra[b.id]["checked"] = marks
        self.bike_extra[b.id]["commissioned_by"] = by
        b.tracker = True
        for bat in self.bats.values():
            if bat.bike_id == b.id and bat.status == "new":
                self.bat_status(bat, t, "available", by)
                self.bat_extra[bat.id] = {
                    "commissioned_at": t, "commissioned_by": by,
                    "checked": {key: {"at": (t - timedelta(minutes=30)).isoformat(),
                                      "by": by} for key in logic.BATTERY_PASSPORT}}
        self.schedule_rent(b, t)

    def on_reserve(self, t: datetime, bike_id: int, token: int) -> None:
        b = self.bikes[bike_id]
        if self.token.get(bike_id) != token or b.status != "available":
            return
        self.set_status(b, t, "reserved", self.actor(b.point))

    def on_rent(self, t: datetime, bike_id: int, token: int) -> None:
        b = self.bikes[bike_id]
        if self.token.get(bike_id) != token or b.status not in ("available", "reserved"):
            return
        theft = None
        if self.theft_plan and self.theft_plan[0]["start"] <= t.date():
            theft = self.theft_plan.pop(0)
        client = self.new_client(t, b.point) if theft else self.pick_client(t, b.point)
        self.start_rental(b, t, client, started_on=t.date(), theft=theft)

    def on_first_pay(self, t: datetime, rental_id: int, code: str) -> None:
        x = self.rx[rental_id]
        if x.r.status == "active":
            self.pay(x, t, first=True, bike_code=code)

    def on_charge(self, t: datetime, rental_id: int, i: int) -> None:
        x = self.rx[rental_id]
        if x.r.status != "active" or x.r.periods >= i:
            return
        self.charge(x, i, t)
        if x.plan is None:
            self.push_charge(x, i + 1)

    def on_pay(self, t: datetime, rental_id: int, i: int) -> None:
        x = self.rx[rental_id]
        if x.r.status == "active":
            self.pay(x, t, first=False)

    def ride(self, x: _R, b: Bike, t: datetime) -> None:
        days = max((t - x.since).total_seconds() / 86400, 0)
        b.mileage_km += int(days * x.km)
        x.since = t

    def close_segment(self, x: _R, t: datetime, b: Bike) -> None:
        seg = x.segments[-1]
        seg["returned_on"] = t.date()
        seg["mileage_end"] = b.mileage_km

    def release_batteries(self, x: _R, t: datetime, status: str, point: str,
                          by: str) -> None:
        for bat_id in x.r.battery_ids:
            bat = self.bats[bat_id]
            if bat.rental_id != x.r.id or bat.status != "rented":
                continue
            self.bat_status(bat, t, status, by)
            bat.rental_id = None
            bat.cycles += 1
            bat.point = point
        for extra in x.extras:
            if extra["removed_at"] is None:
                extra["removed_at"], extra["removed_by"] = t, by

    def on_return(self, t: datetime, rental_id: int) -> None:
        x = self.rx[rental_id]
        r = x.r
        if r.status != "active":
            return
        rng = self.rng
        b = self.bikes[r.bike_id]
        self.ride(x, b, t)
        point = r.point
        if rng.random() < 0.02:
            others = [p for p in self.open_points(t.date()) if p != r.point]
            point = rng.choice(others) if others else point
        by = self.actor(point)
        r.status, r.closed_on, r.closed_at = "closed", t.date(), t
        self.close_segment(x, t, b)
        self.release_batteries(x, t, "available", point, by)
        del self.active[r.client_id]
        if x.skip_last:
            # Намеренный должник: больше не приходит, долг висит без аренды.
            self.no_return.add(r.client_id)
        if r.client_id not in self.no_return:
            self.pool.append((r.client_id, t + timedelta(days=rng.uniform(2, 25)), r.point))
        self.after_service_or_return(b, t, rental=x, point=point)

    def on_service_end(self, t: datetime, bike_id: int, outcome: str) -> None:
        b = self.bikes[bike_id]
        interval = self.intervals.pop(bike_id, None)
        if interval is None or b.status not in ("repair", "maintenance"):
            return
        b.mileage_km += self.rng.randint(0, 4)       # обкатка после ремонта
        if outcome == "written_off":
            t = self.set_status(b, t, "written_off", "staff:demo")
            b.note = "Списан после ДТП: рама и вилка не подлежат ремонту (демо)"
        else:
            t = self.set_status(b, t, "available", self.mech(b.point))
            self.schedule_rent(b, t)
        interval.end = t
        interval.outcome = outcome

    def on_swap(self, t: datetime, tries: int) -> None:
        """Замена в аренде: сломанный уходит в ремонт на точке, клиент уезжает
        на свободном той же точки. Аренда, деньги и батареи - те же."""
        rng = self.rng
        cands = []
        for x in self.active.values():
            r = x.r
            if x.plan is None or r.search_at is not None:
                continue
            last_day = r.started_on + timedelta(days=r.period_days * x.plan - 1)
            if (t - x.since).days < 3 or (last_day - t.date()).days < 3:
                continue
            free = [b for b in self.bikes.values()
                    if b.status == "available" and b.point == r.point]
            if free:
                cands.append((x, free))
        if not cands:
            if tries < 6:
                self.push(t + timedelta(hours=3), "swap", tries + 1)
            return
        x, free = rng.choice(cands)
        r = x.r
        old = self.bikes[r.bike_id]
        free.sort(key=lambda b: (not b.spare, b.model != old.model, b.id))
        new = free[0]
        by = self.actor(r.point)
        self.ride(x, old, t)
        self.close_segment(x, t, old)
        self.service(old, t, "repair", reason="swap", rental=x,
                     days=rng.uniform(2, 5), point=r.point)
        self.token[new.id] = None
        t2 = self.set_status(new, t + timedelta(minutes=2), "rented", by)
        x.since = t2
        x.segments.append({"id": self.nid("rental_bikes"), "bike_id": new.id,
                           "issued_on": t2.date(), "returned_on": None,
                           "mileage_start": new.mileage_km, "mileage_end": None,
                           "reason": logic.SWAP_REASONS["repair"], "by": by,
                           "created_at": t2})
        r.bike_id = new.id
        r.bike_ids.append(new.id)

    def on_search(self, t: datetime, rental_id: int) -> None:
        r = self.rx[rental_id].r
        if r.status == "active":
            r.search_at = t

    def on_lost(self, t: datetime, rental_id: int) -> None:
        """Признан потерянным: аренда закрыта, велосипед и батареи - lost,
        долг остаётся в журнале до решения владельца."""
        x = self.rx[rental_id]
        r = x.r
        if r.status != "active":
            return
        b = self.bikes[r.bike_id]
        self.ride(x, b, t)
        r.status, r.closed_on, r.closed_at, r.lost = "closed", t.date(), t, True
        self.close_segment(x, t, b)
        self.set_status(b, t, "lost", "staff:demo")
        b.note = "Признан потерянным: клиент не вернул велосипед (демо)"
        self.release_batteries(x, t, "lost", r.point, "staff:demo")
        del self.active[r.client_id]
        client = self.w.client(r.client_id)
        client.status = "blacklist"
        client.note = "Не вернул велосипед, заявление в полицию (демо)"
        self.no_return.add(r.client_id)
        if x.theft and x.theft.get("write_off"):
            self.push(t + timedelta(days=self.rng.uniform(1, 4)), "write_off", rental_id)

    def on_write_off(self, t: datetime, rental_id: int) -> None:
        r = self.rx[rental_id].r
        debt = -self.balance[r.client_id]
        if debt > 0:
            self.ledger(r.client_id, r.id, "adjust", debt, t,
                        note="Списание долга: велосипед утерян, заявление в полицию",
                        by="staff:demo")

    def on_sell(self, t: datetime, tries: int) -> None:
        cands = [b for b in self.bikes.values()
                 if b.status == "available" and b.batch <= 2 and b.point in (P1, P2)]
        if not cands:
            if tries < 10:
                self.push(t + timedelta(hours=5), "sell", tries + 1)
            return
        b = max(cands, key=lambda x: (x.mileage_km, x.id))
        self.token[b.id] = None
        t = self.set_status(b, t, "sold", "staff:demo")
        b.note = "Продан курьеру после аренды; покупатель - в amoCRM (демо)"
        for bat in self.bats.values():
            if bat.bike_id == b.id and bat.status == "available":
                self.bat_status(bat, t, "sold", "staff:demo")

    def on_transfer(self, t: datetime, src: str | None, dst: str | None,
                    count: int) -> None:
        """Переезд свободных велосипедов между точками без смены статуса."""
        open_now = self.open_points(t.date())
        if src is None:
            free: dict[str, list[Bike]] = {p: [] for p in open_now}
            for b in self.bikes.values():
                if b.status == "available" and b.point in free:
                    free[b.point].append(b)
            src = max(open_now, key=lambda p: (len(free[p]), p))
            dst = min(open_now, key=lambda p: (len(free[p]), p))
            if src == dst or len(free[src]) < 2:
                return
        moved = 0
        for b in sorted(self.bikes.values(), key=lambda b: (b.model != MONSTER, b.id)):
            if moved >= count:
                break
            if b.status == "available" and b.point == src:
                self.move_bike(b, t + timedelta(minutes=moved * 3), dst,
                               self.actor(dst))
                moved += 1

    def on_battery_repair(self, t: datetime, days: float) -> None:
        free = [x for x in self.bats.values() if x.status == "available"]
        if not free:
            return
        free.sort(key=lambda x: (x.bike_id is not None, -x.cycles, x.id))
        bat = free[self.rng.randrange(min(len(free), 6))]
        self.bat_status(bat, t, "repair", self.mech(bat.point))
        self.w.battery_repairs.append((bat.id, t, None, bat.point))
        self.bat_intervals[bat.id] = len(self.w.battery_repairs) - 1
        self.push(t + timedelta(days=days), "battery_back", bat.id)

    def on_battery_back(self, t: datetime, battery_id: int) -> None:
        bat = self.bats[battery_id]
        if bat.status != "repair":
            return
        self.bat_status(bat, t, "available", self.mech(bat.point))
        i = self.bat_intervals.pop(battery_id)
        bid, start, _end, point = self.w.battery_repairs[i]
        self.w.battery_repairs[i] = (bid, start, t, point)

    # ─────────────────────── заготовки ───────────────────────

    def open_points(self, day: date) -> list[str]:
        return [p.name for p in self.w.points.values() if p.opened_on <= day]

    def make_bike(self, batch_no: int, batch: _Batch, purchase: dict, point: str,
                  t: datetime, *, status: str) -> Bike:
        rng = self.rng
        n = self.nid("bikes")
        prefix = "MT" if batch.model.startswith("Monster") else "KG"
        made = purchase["purchased_on"]
        frame = f"{prefix}{made:%y%m}{rng.randrange(10**6):06d}{n:03d}"
        motor = f"M1200-{made:%y%m}-{rng.randrange(10**5):05d}{n % 10}"
        letters = "АВЕКМНОРСТУХ"
        plate = (f"{n:04d} {rng.choice(letters)}{rng.choice(letters)} 16")
        age = max((self.S - made).days, 0)
        return Bike(
            id=n, code=f"МБ-{n:03d}", model=batch.model, frame_no=frame, motor_no=motor,
            plate_no=plate, point=point, status=status, created_at=t,
            purchased_on=made, purchase_id=purchase["id"], price=D(batch.price),
            mileage_km=int(age * rng.uniform(35, 55)) if age else rng.randint(0, 3),
            batch=batch_no)

    def make_battery(self, bike: Bike | None, model: str, point: str | None,
                     t: datetime, status: str, purchased_on: date, *,
                     by: str) -> Battery:
        n = self.nid("batteries")
        spec = next(m for m in BATTERY_MODELS if m[0] == model)
        age = max((self.S - purchased_on).days, 0)
        bat = Battery(
            id=n, code=f"АКБ-{n:04d}", model_id=self.w.battery_models[model],
            model=model, serial_no=f"{'MN' if 'Monster' in model else 'KG'}60-"
                                   f"{purchased_on:%y%m}-{n:05d}",
            status=status, point=point, bike_id=bike.id if bike else None,
            rental_id=None, cycles=age // 18, purchased_on=purchased_on, price=spec[4],
            created_at=t)
        self.bat_created(bat, t, by)
        return bat


# ─────────────────────────── сборка мира ───────────────────────────

# Сколько реализаций пробовать. Одна реализация честная, но её три числа
# гуляют на ±4 % от дня к дню (фаза недельных начислений, месячные
# тарифы в окне), а демо обязано каждый день показывать коридор. Поэтому
# ядро прогоняет историю, меряет последние 30 дней той же арифметикой,
# что панель, и при промахе берёт следующее зерно - детерминированно.
MAX_ATTEMPTS = 60
GOAL_IDLE = (8.8, 10.2)
GOAL_CHECK = (D(505), D(525))
GOAL_RENEWALS = (0.61, 0.72)       # доля продлений в начислениях окна


def build_world(base: World, ids: dict[str, int]) -> Sim:
    """Лучшая из реализаций истории от начала до base.now.

    base - мир со справочниками (точки, сотрудники, модели, тарифы - их id
    дала база); каждая попытка - его копия со своим генератором. Первая
    попытка - ровно зерно seed, следующие - seed * 1000 + номер.
    """
    best: tuple[float, Sim] | None = None
    for attempt in range(MAX_ATTEMPTS):
        sim = simulate(base.fork(attempt), dict(ids))
        score = grade(sim)[0]
        if best is None or score < best[0]:
            best = (score, sim)
        if score == 0:
            break
    assert best is not None
    return best[1]


def simulate(w: World, ids: dict[str, int]) -> Sim:
    """Одна реализация истории, досчитанная до конца (finish)."""
    sim = Sim(w, ids)
    rng = w.rng
    S, T = sim.S, sim.T
    w.battery_for_model = dict(BATTERY_OF)
    plan_shifts(sim)

    # Закупки: до истории - заводятся импортом в первый день, в истории -
    # своим днём и встают «на сборке» до ввода в эксплуатацию.
    pre: list[tuple[int, _Batch, dict]] = []
    for no, batch in enumerate(BATCHES, start=1):
        anchor = {"S": S, "T": T, "P3": w.points[P3].opened_on}[batch.anchor]
        day = anchor + timedelta(days=batch.offset)
        purchase = {"id": sim.nid("purchases"), "no": logic.purchase_no(no),
                    "supplier_id": w.suppliers[SUPPLIER_BIKES], "purchased_on": day,
                    "total": D(batch.price) * sum(c for _p, c in batch.split),
                    "note": batch.note, "created_by": "staff:demo",
                    "created_at": at(max(day, S), 8.5 if day < S else 12)}
        w.purchases.append(purchase)
        if day < S:
            pre.append((no, batch, purchase))
        else:
            sim.push(at(day, rng.uniform(12, 15)), "purchase", no, batch, purchase)

    plan_events(sim)
    import_fleet(sim, pre)
    sim.run()
    finish(sim)
    return sim


def grade(sim: Sim) -> tuple[float, dict[str, Any]]:
    """Штраф реализации: 0 - всё в цели. Дни - logic.days_by_status и
    days_by_status_location по журналам сида, выручка - платежи окна:
    это та же арифметика, что у CrmDB.bike_days_by_status и сводки."""
    until = sim.now
    since = until - timedelta(days=30)
    log = [{"id": r[0], "bike_id": r[1], "to_status": r[3], "changed_at": r[4]}
           for r in sim.status_rows]
    places = [{"id": r[0], "bike_id": r[1], "to_location": r[3], "changed_at": r[4]}
              for r in sim.loc_rows]
    paid: dict[str | None, D] = defaultdict(D)
    for p in sim.w.payments:
        if since <= p.created_at < until:
            paid[p.point] += p.amount
    # Продления - как в «По точкам»: начисление периода позже начала аренды.
    charged = renewals = D(0)
    for row in sim.ledger_rows:
        if row[3] == "charge" and since <= row[10] < until:
            charged -= row[4]
            if row[6] > sim.rx[row[2]].r.started_on:
                renewals -= row[4]
    share = float(renewals / charged) if charged else 0.0
    total = logic.fleet_metrics(logic.days_by_status(log, since, until),
                                sum(paid.values(), D(0)))
    by_point = logic.days_by_status_location(log, places, since, until)
    point = {p: logic.fleet_metrics(by_point.get(p, {}), paid.get(p, D(0)))
             for p in (P1, P2, P3)}
    counts: dict[str, int] = defaultdict(int)
    for b in sim.w.bikes:
        counts[b.status] += 1

    def outside(value: Any, lo: Any, hi: Any, unit: float) -> float:
        if value is None:
            return 10.0
        return (max(lo - value, 0) + max(value - hi, 0)) / unit

    score = outside(total["idle_percent"], *GOAL_IDLE, 0.5)
    score += float(outside(total["avg_check"], *GOAL_CHECK, 5))
    idle = {p: m["idle_percent"] or 0 for p, m in point.items()}
    check = {p: m["avg_check"] or D(0) for p, m in point.items()}
    # Точки разные: Павлюхина лучшая по простою, новая хуже всех и по чеку.
    score += (idle[P1] > idle[P2]) + (idle[P3] <= max(idle[P1], idle[P2]))
    score += check[P3] >= check[P1]
    # Срез «сейчас» - как в описании демо: ремонт 5-9, свободно 4-13, ТО есть.
    score += outside(counts["repair"], 5, 9, 2) + outside(counts["available"], 4, 13, 2)
    score += outside(counts["maintenance"], 1, 4, 2)
    score += outside(counts["reserved"], 1, 2, 1)
    score += outside(share, *GOAL_RENEWALS, 0.02)
    return float(score), {"total": total, "points": point, "counts": dict(counts),
                          "renewals": share}


def p3_opened_on(today: date) -> date:
    """День открытия третьей точки: первое число, ближайшее к «P3_OPEN_DAYS
    назад», - 45-75 дней. Первое число, а не любой день: в «Три числа по
    месяцам» месяц открытия иначе был бы обрезком в пару дней, где
    предоплата первых аренд делится на горстку велосипеде-дней, - чек в
    тысячи рублей и простой 70-90 % на отчёте, который смотрит покупатель."""
    day = today - timedelta(days=P3_OPEN_DAYS)
    first = day.replace(day=1)
    return first if day.day <= 15 else (first + timedelta(days=32)).replace(day=1)


def plan_shifts(sim: Sim) -> None:
    """Смены кассы: каждая точка, каждый день последних CASH_DAYS дней.
    Сегодняшняя открыта, только если её час уже прошёл: сброс в 04:00
    иначе открывал бы кассу «в 03:50» на точке, что работает с 10. Не
    открытая к «сейчас» уходит в остаток дня (later_today), и её
    открывает процесс демо в свой час."""
    w = sim.w
    first = sim.T - timedelta(days=CASH_DAYS)
    windows = []
    day = first
    while day <= sim.T:
        for point in w.points.values():
            if point.opened_on > day:
                continue
            opened = sim.hours(day, 9.6, 10.0)
            closed = sim.hours(day, 19.1, 20.2)
            if day == sim.T:
                closed = None
                if opened > w.now - timedelta(minutes=10):
                    sim.later_shifts.append((opened, point.name))
                    continue
            windows.append((opened, point.name, day, closed))
        day += DAY
    windows.sort(key=lambda x: (x[0], x[1]))
    for opened, point, day, closed in windows:
        sid = sim.nid("cash_shifts")
        shift = Shift(id=sid, no=logic.shift_no(sid), point=point, opened_at=opened,
                      closed_at=closed, opening=D(0), expected=None, counted=None,
                      status="closed" if closed else "open")
        sim.shift_at[(point, day)] = shift
        w.shifts.append(shift)


def plan_events(sim: Sim) -> None:
    """Редкие события истории - по плану, а не броском кубика: розыск,
    замены, продажи и списания в демо обязаны быть, и в нужном числе."""
    rng, S, T = sim.rng, sim.S, sim.T
    # Кражи: 13 закрытых признанием потери, последняя - с долгом без
    # списания (намеренное расхождение), две в розыске сейчас и кандидат.
    last_start = T - timedelta(days=62)
    span = (last_start - S).days
    for i in range(13):
        sim.theft_plan.append({
            "start": S + timedelta(days=round(i * span / 12)),
            "paid": 1 if i % 3 else 2, "search_delay": rng.randint(7, 10),
            "lost_delay": rng.randint(22, 28), "write_off": i != 12})
    sim.theft_plan.append({"start": T - timedelta(days=41), "paid": 1, "search_delay": 8,
                           "lost_delay": None, "write_off": False})
    sim.theft_plan.append({"start": T - timedelta(days=25), "paid": 1, "search_delay": 8,
                           "lost_delay": None, "write_off": False})
    # И кандидат: полторы недели не платит, в розыск его ещё не ставили -
    # раздел «Розыск» показывает, кого система предлагает искать.
    sim.theft_plan.append({"start": T - timedelta(days=17), "paid": 1, "search_delay": None,
                           "lost_delay": None, "write_off": False})
    sim.theft_plan.sort(key=lambda p: p["start"])
    # Второй намеренный должник: вернул велосипед, последнюю неделю не оплатил.
    sim.skip_plan.append(T - timedelta(days=45))
    sim.writeoff_plan = [S + timedelta(days=60), T - timedelta(days=40)]
    for day in (S + timedelta(days=30), S + timedelta(days=75), T - timedelta(days=50),
                T - timedelta(days=18), T - timedelta(days=6)):
        sim.push(sim.hours(day, 12, 16), "swap", 0)
    for day in (S + timedelta(days=45), T - timedelta(days=80), T - timedelta(days=30)):
        sim.push(sim.hours(day, 12, 17), "sell", 0)
    opening = sim.w.points[P3].opened_on
    sim.push(at(opening, 9.3), "transfer", P1, P3, 4)
    sim.push(at(opening, 9.5), "transfer", P2, P3, 2)
    day = S + timedelta(days=5)
    while day < T:
        sim.push(sim.hours(day, 11, 17), "transfer", None, None, 1)
        day += timedelta(days=rng.randint(7, 12))
    for i, day in enumerate((S + timedelta(days=20), S + timedelta(days=48),
                             S + timedelta(days=77), S + timedelta(days=103),
                             T - timedelta(days=40), T - timedelta(days=21),
                             T - timedelta(days=6), T - timedelta(days=2))):
        sim.push(sim.hours(day, 11, 16), "battery_repair",
                 rng.uniform(2, 9) if i < 6 else rng.uniform(9, 14))


def import_fleet(sim: Sim, pre: list[tuple[int, _Batch, dict]]) -> None:
    """Первый день истории: парк и идущие аренды переносят из таблицы.

    Велосипеды до истории попадают в CRM здесь, в своих статусах: часть
    уже у курьеров (аренда начата до переноса), часть в ремонте, семь
    потеряны ещё до CRM. Первый период перенесённой аренды начисляется
    и оплачивается в момент переноса - раньше истории денег нет.
    """
    w, rng, S = sim.w, sim.rng, sim.S
    t = at(S, 9.0)
    bikes: list[tuple[Bike, int, _Batch, dict]] = []
    for no, batch, purchase in pre:
        for point, count in batch.split:
            for _ in range(count):
                b = sim.make_bike(no, batch, purchase, point, t, status="available")
                bikes.append((b, no, batch, purchase))
    order = list(range(len(bikes)))
    rng.shuffle(order)
    # Потерянные до CRM - из двух старших партий: их дольше всех возили.
    lost = set([i for i in order if bikes[i][1] <= 2][:7])
    rest = [i for i in order if i not in lost]
    repair, maint = set(rest[:5]), set(rest[5:7])
    rented = set(rest[7:7 + int(len(rest) * 0.88)])
    for i, (b, _no, _batch, _purchase) in enumerate(bikes):
        t += timedelta(seconds=rng.randint(25, 50))
        b.tracker = rng.random() < 0.62 and i not in lost
        if i in lost:
            b.status = "lost"
            b.note = "Потерян до перехода на CRM (демо)"
        elif i in repair:
            b.status = "repair"
        elif i in maint:
            b.status = "maintenance"
        elif i in rented:
            b.status = "rented"
        # Первая строка журнала - сразу в перенесённом статусе: до CRM
        # истории нет, и «свободен» на секунду исказил бы «сколько стоит».
        sim.bike_created(b, t, "import")
        if b.status == "rented":
            theft = None
            if sim.theft_plan and sim.theft_plan[0]["start"] <= S:
                theft = sim.theft_plan.pop(0)
            client = sim.new_client(t, b.point, source="import")
            sim.start_rental(b, t, client, imported=True, theft=theft,
                             started_on=S - timedelta(days=rng.randint(0, 6)))
            continue
        for _ in range(2):
            sim.make_battery(b, BATTERY_OF[b.model], b.point, t,
                             "lost" if b.status == "lost" else "available",
                             b.purchased_on, by="import")
        if b.status in ("repair", "maintenance"):
            sim.open_interval(b, t, b.status, reason="import", rental=None,
                              days=rng.uniform(0.5, 5), point=b.point)
        elif b.status == "available":
            sim.schedule_rent(b, t)
    # Ничьи батареи первого дня: запас точек под доп. аккумуляторы.
    for _no, batch, purchase in pre:
        for point, _count in batch.split:
            for _ in range(batch.spare_batteries // len(batch.split) + 2):
                sim.make_battery(None, BATTERY_OF[batch.model], point, t, "available",
                                 purchase["purchased_on"], by="import")
    sim.n_import = len(w.clients)


# ─────────────────────────── итог симуляции ───────────────────────────

# Расхождение кассы вечером: чаще ноль, иногда полтинник-сотня сдачи,
# изредка больше порога logic.CASH_DIFF_NOISE - есть что разобрать.
_DIFFS = ((0, 0.76), (-50, 0.06), (-100, 0.05), (50, 0.04), (100, 0.03),
          (-200, 0.03), (-150, 0.02), (-350, 0.01))


def finish(sim: Sim) -> None:
    """Досчитать то, что известно только в конце: кассу смен, намерения
    по истекающим арендам, подменный фонд и сводку журнала денег."""
    w, rng = sim.w, sim.rng
    carry: dict[str, D] = {}
    for shift in sorted(w.shifts, key=lambda s: (s.opened_at, s.id)):
        shift.opening = carry.get(shift.point, D(2000))
        if shift.closed_at is None:
            continue
        before = shift.opening + sim.shift_cash.get(shift.id, D(0))
        out = D(int(max(before - 2000, 0) // 500) * 500)
        if out > 0:
            sim.move_rows.append((
                sim.nid("cash_moves"), shift.id, "out", out, "Сдано владельцу", None,
                shift.closed_at - timedelta(minutes=rng.uniform(3, 12)),
                w.operator(shift.point).actor))
        shift.expected = before - out
        shift.counted = max(shift.expected + D(_pick(rng, _DIFFS)), D(0))
        carry[shift.point] = shift.counted
    sim.carry = carry

    # Бронь «на завтра» в срезе есть всегда, как на живой точке: если
    # история к «сейчас» её не оставила, оператор бронирует свободный.
    if not any(b.status == "reserved" for b in w.bikes):
        free = sorted((b for b in w.bikes
                       if b.status == "available" and b.point in (P1, P2)),
                      key=lambda b: (sim.last_ts[b.id], b.id))
        for b in free[:1]:
            t = max(sim.last_ts[b.id] + timedelta(minutes=10),
                    sim.now - timedelta(hours=rng.uniform(1, 6)))
            if t < sim.now:
                sim.token[b.id] = None
                sim.set_status(b, t, "reserved", sim.actor(b.point))

    # «Продлю» / «верну» у аренд, которые истекают в ближайшие два дня:
    # прогноз освобождения на сводке без них пуст.
    for x in sim.rx.values():
        r = x.r
        if r.status != "active" or x.plan is None:
            continue
        until = logic.covered_until(r.billed_until, sim.balance[r.client_id], r.price,
                                    r.period_days)
        if 0 <= (until - sim.T).days <= 2 and rng.random() < 0.6:
            intent = "renew" if x.plan > r.periods else "return"
            when = max(sim.now - timedelta(hours=rng.uniform(1, 20)), r.created_at)
            sim.intents[r.id] = (intent, until, sim.actor(r.point), when)

    # Подменный фонд - три велосипеда старшего Kugoo на старых точках.
    spare = [b for b in w.bikes if b.batch == 2 and b.status in logic.OPERATIONAL_STATUSES]
    for b in spare[:3]:
        b.spare = True

    stats: dict[str, dict[str, Any]] = {}
    for row in sim.ledger_rows:
        cell = stats.setdefault(row[3], {"rows": 0, "sum": D(0)})
        cell["rows"] += 1
        cell["sum"] += row[4]
    w.ledger_stats = stats


# До какого часа живёт «сегодня» после сброса: позже точки закрыты.
DAY_END = 21.0


def later_today(sim: Sim) -> list[dict[str, Any]]:
    """Остаток сегодняшнего дня после «сейчас»: открытие смен и платежи за
    продления, которые история уже запланировала на сегодня.

    Сид их не пишет: будущего в базе быть не должно - журналы дали бы
    отрицательные интервалы, пока момент не наступит. А без них демо,
    засеянное в 04:00, весь день показывало бы пустое «сегодня» и кассы,
    открытые «в 03:50», а первого числа - пустой месяц. Процесс демо
    делает их в свой час обычным путём панели (runtime.live_day), с now()
    базы. Генератор свой: этот расчёт не сдвигает ни историю, ни модули
    после ядра.
    """
    w = sim.w
    end = at(sim.T, DAY_END)
    if w.now >= end:
        return []
    rng = random.Random(f"later:{w.seed}:{w.attempt}:{sim.T.isoformat()}")
    ops: list[dict[str, Any]] = []
    opens: dict[str, datetime] = {}
    for when, point in sorted(sim.later_shifts):
        opens[point] = when
        ops.append({"at": when, "kind": "shift", "point": point,
                    "opening": sim.carry.get(point, D(2000)), "by": sim.actor(point)})
    for (point, day), shift in sim.shift_at.items():
        if day == sim.T and shift.closed_at is None:
            opens.setdefault(point, shift.opened_at)
    for t, _seq, kind, args in sorted(sim.heap, key=lambda e: (e[0], e[1])):
        if t > end:
            break
        if kind != "pay" or t <= w.now:
            continue
        r = sim.rx[args[0]].r
        if r.status != "active":
            continue
        # Наличные - как в Sim.pay: в часы кассы своей точки и тем же днём.
        window = None
        if r.point in opens:
            lo = max(opens[r.point] + timedelta(minutes=5), w.now + timedelta(minutes=1))
            hi = end - timedelta(minutes=5)
            window = (lo, hi) if hi > lo else None
        method = _pick(rng, (("cash", 0.2 if window else 0), ("sbp", 0.45),
                             ("card", 0.15), ("transfer", 0.2)))
        by, linked = sim.actor(r.point), False
        if method == "cash":
            assert window is not None
            t = window[0] + (window[1] - window[0]) * rng.random()
            note = "Продление, наличные"
        elif method == "card":
            by, note, linked = "эквайринг", "Оплата по ссылке", True
        elif method == "transfer":
            by, note = "staff:demo", "Перевод на расчётный счёт"
        else:
            note = "Перевод по СБП"
        ops.append({"at": t, "kind": "payment", "point": r.point, "client_id": r.client_id,
                    "rental_id": r.id, "amount": r.price, "method": method, "note": note,
                    "by": by, "linked": linked})
    ops.sort(key=lambda o: (o["at"], o["kind"] != "shift"))
    return ops
