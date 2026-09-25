"""Мир демо-сида: что насимулировало ядро и на что опираются модули
сервиса (seed_service) и остального (seed_extras).

Отдельный модуль, а не seed.py: seed.py зовёт seed_service и seed_extras,
а им нужен World - через seed.py импорт замкнулся бы в круг.

Все моменты времени - aware datetime в Москве (MSK). Постоянное смещение
+03:00, а не zoneinfo: Москва без перевода часов с 2014 года, и сиду не
нужен tzdata в образе.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from .people import People

MSK = timezone(timedelta(hours=3), "MSK")


def at(day: date, hours: float = 0.0) -> datetime:
    """Момент дня по Москве: at(day, 10.5) - 10:30."""
    return datetime.combine(day, time(0), MSK) + timedelta(hours=hours)


@dataclass
class Point:
    """Точка проката. opened_on - с какого дня на ней выдают."""
    id: int
    name: str
    address: str
    lat: float
    lon: float
    opened_on: date
    phone: str
    hours: str
    sort: int


@dataclass
class StaffMember:
    id: int
    login: str
    name: str
    profile: str              # owner | manager | tech
    location: str | None

    @property
    def actor(self) -> str:
        """Как автора пишет журнал: staff:<логин>."""
        return f"staff:{self.login}"


@dataclass
class Bike:
    """Велосипед в итоговом состоянии (сейчас)."""
    id: int
    code: str
    model: str
    frame_no: str
    motor_no: str
    plate_no: str
    point: str | None          # bikes.location сейчас
    status: str
    created_at: datetime
    purchased_on: date
    purchase_id: int
    price: Decimal
    mileage_km: int = 0
    spare: bool = False
    tracker: bool = False      # подсказка seed_extras: у этого велосипеда трекер
    commissioned_at: datetime | None = None
    batch: int = 0             # номер закупки по порядку, 1..N
    note: str | None = None


@dataclass
class Battery:
    id: int
    code: str
    model_id: int
    model: str
    serial_no: str
    status: str
    point: str | None
    bike_id: int | None
    rental_id: int | None
    cycles: int
    purchased_on: date
    price: Decimal
    created_at: datetime


@dataclass
class ServiceInterval:
    """Велосипед вне выдачи: ремонт или ТО с start до end.

    end None - идёт сейчас: seed_service обязан открыть наряд на каждый
    такой ремонт (иначе «В ремонте, а наряда нет» в расхождениях).
    outcome - куда велосипед ушёл после: available, written_off, None.
    """
    bike_id: int
    kind: str                  # repair | maintenance
    start: datetime
    end: datetime | None
    point: str | None
    reason: str                # return | swap | import | accident
    outcome: str | None
    rental_id: int | None      # аренда, после которой (или в которой) сломался
    mileage_km: int


@dataclass
class Client:
    id: int
    full_name: str
    phone: str
    employer: str
    channel: str
    experience: str
    created_at: datetime
    point: str | None          # точка первой аренды
    contract_no: str | None
    status: str = "active"
    invited_by: int | None = None
    invited_at: datetime | None = None
    ref_code: str | None = None
    source: str = "manual"
    note: str | None = None


@dataclass
class Rental:
    id: int
    client_id: int
    bike_id: int               # велосипед на руках (или последний)
    bike_ids: list[int]        # все велосипеды аренды по порядку
    point: str
    started_on: date
    created_at: datetime
    tariff_id: int
    tariff_name: str
    period_days: int
    base_price: Decimal
    price: Decimal
    status: str = "active"
    closed_on: date | None = None
    closed_at: datetime | None = None
    billed_until: date | None = None
    search_at: datetime | None = None
    lost: bool = False
    periods: int = 0           # сколько периодов начислено
    extras: list[int] = field(default_factory=list)      # id батарей-позиций
    battery_ids: list[int] = field(default_factory=list)  # выданные батареи


@dataclass
class Payment:
    """Платёж журнала: seed_extras привязывает к ним выписку и счета."""
    ledger_id: int
    client_id: int
    rental_id: int             # к какой аренде относится по смыслу
    amount: Decimal
    method: str
    created_at: datetime
    point: str
    shift_id: int | None
    note: str
    linked: bool               # rental_id записан в ledger (иначе null)


@dataclass
class Shift:
    id: int
    no: str
    point: str
    opened_at: datetime
    closed_at: datetime | None
    opening: Decimal
    expected: Decimal | None
    counted: Decimal | None
    status: str


@dataclass
class World:
    """Всё, что ядро знает о насимулированном мире.

    rng - генератор выбранной реализации ядра: seed_service и seed_extras
    продолжают его поток, и детерминизм держится, пока порядок вызовов
    один. people - раздатчик ФИО и телефонов: сторонних клиентов заводить
    только через него, иначе телефон совпадёт с уже выданным (unique).
    Id в базе у всего, что здесь лежит, уже есть; счётчики таблиц ядро
    подвинуло, так что свои строки модули вставляют без явных id.
    """
    seed: int
    today: date
    now: datetime                              # момент «сейчас» сида
    rng: random.Random
    people: People
    history_start: datetime                    # начало истории (points_history_since)
    points: dict[str, Point] = field(default_factory=dict)
    staff: dict[str, StaffMember] = field(default_factory=dict)
    suppliers: dict[str, int] = field(default_factory=dict)
    bike_models: dict[str, int] = field(default_factory=dict)       # title -> id
    battery_models: dict[str, int] = field(default_factory=dict)    # title -> id
    battery_for_model: dict[str, str] = field(default_factory=dict)  # модель велосипеда -> АКБ
    # (kind, model, period_days) -> {"id", "name", "price"}
    tariffs: dict[tuple[str, str, int], dict[str, Any]] = field(default_factory=dict)
    purchases: list[dict[str, Any]] = field(default_factory=list)
    bikes: list[Bike] = field(default_factory=list)
    batteries: list[Battery] = field(default_factory=list)
    service_intervals: list[ServiceInterval] = field(default_factory=list)
    # Ремонт батарей: (battery_id, start, end или None, точка)
    battery_repairs: list[tuple[int, datetime, datetime | None, str | None]] = \
        field(default_factory=list)
    clients: list[Client] = field(default_factory=list)
    rentals: list[Rental] = field(default_factory=list)
    payments: list[Payment] = field(default_factory=list)
    shifts: list[Shift] = field(default_factory=list)
    transfers: list[tuple[int, datetime, str | None, str | None]] = \
        field(default_factory=list)
    ledger_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    contract_seq: int = 0
    # Остаток сегодняшнего дня (core.later_today): открытие смен и платежи
    # позже «сейчас». Их делает процесс демо в свой час (runtime.live_day).
    later: list[dict[str, Any]] = field(default_factory=list)

    attempt: int = 0                           # какая реализация ядра выбрана

    def fork(self, attempt: int) -> World:
        """Копия со справочниками и пустой историей - попытка ядра со своим
        генератором: первая - ровно seed, дальше seed * 1000 + номер."""
        rng = random.Random(self.seed if attempt == 0 else self.seed * 1000 + attempt)
        return World(
            seed=self.seed, today=self.today, now=self.now, rng=rng, people=People(rng),
            history_start=self.history_start, points=dict(self.points),
            staff=dict(self.staff), suppliers=dict(self.suppliers),
            bike_models=dict(self.bike_models), battery_models=dict(self.battery_models),
            battery_for_model=dict(self.battery_for_model), tariffs=dict(self.tariffs),
            attempt=attempt)

    # ─── справки для модулей после ядра ───

    def bike(self, bike_id: int) -> Bike:
        return self._bike_index()[bike_id]

    def _bike_index(self) -> dict[int, Bike]:
        index = self.__dict__.get("_bikes_by_id")
        if index is None or len(index) != len(self.bikes):
            index = {b.id: b for b in self.bikes}
            self.__dict__["_bikes_by_id"] = index
        return index

    def client(self, client_id: int) -> Client:
        index = self.__dict__.get("_clients_by_id")
        if index is None or len(index) != len(self.clients):
            index = {c.id: c for c in self.clients}
            self.__dict__["_clients_by_id"] = index
        return index[client_id]

    @property
    def techs(self) -> list[StaffMember]:
        return [s for s in self.staff.values() if s.profile == "tech"]

    def operator(self, point: str | None) -> StaffMember:
        """Оператор точки: он выдаёт, принимает и держит кассу."""
        for s in self.staff.values():
            if s.profile == "manager" and s.location == point:
                return s
        return self.staff["demo"]

    def mechanic(self, point: str | None) -> StaffMember:
        """Механик точки; на точке без своего - механик первой точки."""
        techs = self.techs
        for s in techs:
            if s.location == point:
                return s
        return techs[0]

    def active_rentals(self) -> list[Rental]:
        return [r for r in self.rentals if r.status == "active"]

    def open_repairs(self) -> list[ServiceInterval]:
        """Ремонты и ТО, идущие сейчас: под каждый ремонт нужен открытый наряд."""
        return [i for i in self.service_intervals if i.end is None]
