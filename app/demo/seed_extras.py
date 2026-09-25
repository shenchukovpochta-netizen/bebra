"""Остальное демо-стенда: трекеры и карта, банк, счета и карты, входящие,
рассылки, акции, приглашения и баллы, заявки, ПЭП, рабочая группа точек,
уведомления, сохранённые фильтры.

Опора - World ядра: аренды, платежи (world.payments), клиенты, велосипеды
с подсказкой «у этого трекер» (Bike.tracker), точки и сотрудники. Чего в
World нет - начислений, журналов статусов и мест, записей ремонта, - модуль
читает из базы: ядро и сервис записали их той же транзакцией.

Правила ядра: SQL напрямую, явные даты, ни одной строки из будущего. Id
модуль раздаёт сам и заранее (журнал ← приглашение ← повод баллов
ссылаются друг на друга), счётчики двигает в конце. Всё случайное - из
своего генератора от зерна и попытки ядра: правка сервиса не перетасовывает
трекеры и переписку. Шифротекст переписки - исключение: одноразовый nonce
AES-GCM случаен у панели, и сид шифрует тем же Vault, что и она.

История повторяет правила панели и бота, а не выдумывает свои: напоминания
- по «оплачено до» на тот час (logic.covered_until), просьба об отзыве и
приглашение на ТО - по правилам дневного прохода, тревоги - те, что поднял
бы опрос (logic.detect_alerts), формы группы разобраны настоящими парсерами.

Ничего, что процесс бота взял бы в работу: ни ответа в очереди, ни
рассылки в отправке, ни команды трекеру без ответа, ни необъявленного
обращения. Бот к демо не подключается, но если подключат по ошибке -
номера мессенджеров вымышленные, и написать он никому не сможет.
"""

from __future__ import annotations

import base64
import bisect
import hashlib
import json
import math
import random
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from ..crm import esign, logic, service
from ..crm.db import CrmDB
from ..logic import contract_number
from . import core
from .world import MSK, Client, Payment, Rental, at

if TYPE_CHECKING:
    import asyncpg

    from .world import World

D = Decimal
DAY = timedelta(days=1)

# Секрет подписи cookie демо-панели по умолчанию - для тестов и сида без
# секрета. Боевой сброс передаёт настоящий CRM_SECRET демо (populate(...,
# secret=) или world.secret): ключ переписки производный от него.
DEMO_SECRET = "demo-secret"

# Вымышленные номера мессенджеров. Настоящие id Telegram сейчас меньше
# 10^10; тринадцатизначные с круглым началом не принадлежат никому, и бот,
# подключённый к демо по ошибке, не написал бы живому человеку. Без них
# пусты аудитории рассылок и воронка приглашений (tg_id там обязателен).
TG_BASE = 9_000_000_000_000
MAX_BASE = 8_000_000_000_000
CLICK_SHIFT = 500_000         # переход по ссылке без регистрации
STRANGER_SHIFT = 700_000      # писал в бот, но клиентом не стал
OPS_CHAT = -1_009_000_000_001
OPS_TOPICS = {"fix": 2, "swap": 2, "return": 4, "daily": 7}

# Порог «молчит» для демо: данные застывают между ночными сбросами, и при
# умолчании в 12 часов к вечеру вся карта стала бы серой. Сутки с запасом
# на неудачный сброс; трекеры в розыске молчат дольше и остаются серыми.
DEMO_OFFLINE_HOURS = 30
# Опрос Авито «раз в 8 часов»: отметка круга живёт 3 периода, то есть до
# следующего ночного сброса. С настоящим периодом (минута) к утру панель
# кричала бы «Авито: опрос не работает».
DEMO_AVITO_EVERY = 8 * 3600

# Сколько друзей пришло по ссылкам с запуска программы: воронка в 10-20
# человек читается целиком. Остальные «по рекомендации» из ядра пришли
# раньше, чем появились ссылки, - канал у них остаётся, агента нет.
REF_FRIENDS = 14

# Как у бота по умолчанию (WebConfig.remind_before_days).
REMIND_BEFORE_DAYS = 2
REMIND_HOURS = ((logic.REMIND_OVERDUE, 8.0), (logic.REMIND_DUE, 9.0),
                (logic.REMIND_SOON, 14.0))
REMIND_CODE = {logic.REMIND_SOON: "rent_soon", logic.REMIND_DUE: "rent_due",
               logic.REMIND_OVERDUE: "rent_overdue"}

# Районы Казани, где живут и возят курьеры: от них - дом и заказы.
DISTRICTS = (
    (55.7963, 49.1088), (55.7830, 49.1370), (55.8290, 49.1150), (55.8170, 49.0850),
    (55.8000, 49.0600), (55.8550, 49.0900), (55.7630, 49.1850), (55.7560, 49.2150),
    (55.7500, 49.1700), (55.8300, 49.1750), (55.7900, 49.1600), (55.7700, 49.1100),
)
CENTER = (55.7963, 49.1088)
# Где «застрял» велосипед из истории с блокировкой мотора: окраина.
FAR_AWAY = (55.8615, 49.2365)

SETTINGS = {
    "tracker_offline_hours": str(DEMO_OFFLINE_HOURS),
    "review_bonus": "300",
}
REF_SETTINGS = {"ref_enabled": "1", "ref_bonus": "500", "ref_min_payment": "1000"}

_TABLES = ("clients", "ledger", "bonuses", "referrals", "trackers", "tracker_positions",
           "tracker_alerts", "tracker_commands", "bank_txns", "pay_orders", "card_tokens",
           "promos", "bookings", "sign_requests", "sign_events", "message_templates",
           "campaigns", "campaign_sends", "inbox_threads", "inbox_messages",
           "ops_reports", "saved_views", "notice_log")
_LEDGER = ["id", "client_id", "rental_id", "kind", "amount", "method", "period_from",
           "period_to", "note", "created_by", "created_at", "shift_id"]
_BONUS = ["id", "client_id", "kind", "amount", "ledger_id", "ref_id", "note",
          "created_by", "created_at", "promo_id", "rental_id", "period_from"]


def inbox_key_for(secret: str) -> bytes:
    """Ключ переписки демо: sha256(секрет панели + "demo-inbox"), 32 байта.

    Отдельного секрета у демо нет намеренно: переписка должна читаться, а
    лишний файл в secrets/ - это ещё одна вещь, которую забудут. Ключ
    производный, поэтому сид и панель считают его одинаково, а к боевому
    INBOX_KEY он отношения не имеет.
    """
    return hashlib.sha256((secret + "demo-inbox").encode("utf-8")).digest()


def inbox_key_text(secret: str) -> str:
    """Тот же ключ строкой, как его ждут WebConfig.inbox_key и Vault.from_raw."""
    return base64.b64encode(inbox_key_for(secret)).decode("ascii")


async def populate(conn: asyncpg.Connection, world: World, *,
                   secret: str | None = None) -> None:
    """Всё остальное демо поверх ядра и сервиса - в их транзакции.

    secret - CRM_SECRET демо-панели: от него ключ переписки «Входящих».
    Не задан - world.secret, если ядро его несёт, иначе DEMO_SECRET.
    """
    c = await _Ctx.load(conn, world)
    c.secret = secret or getattr(world, "secret", None) or DEMO_SECRET
    await _messengers(conn, c)
    await _new_clients(conn, c)
    await _trackers(conn, c)
    await _bank(conn, c)
    await _pay_orders(conn, c)
    await _referrals(conn, c)
    await _promos(conn, c)
    await _reviews(conn, c)
    await _bookings(conn, c)
    await _signing(conn, c)
    await _mailing(conn, c)
    await _inbox(conn, c)
    await _ops_group(conn, c)
    await _saved_views(conn, c)
    await _settings(conn, c)
    await _notices(conn, c)
    await _claims(conn, c)
    await c.ids.sync(conn)


# ─────────────────────────── основа ───────────────────────────

class _Ids:
    """Id заранее: строки ссылаются друг на друга, и возвращать их по одной
    из базы дольше, чем раздать. Счётчики таблиц двигаются в конце."""

    def __init__(self, start: dict[str, int]) -> None:
        self.next = dict(start)

    def take(self, table: str) -> int:
        value = self.next[table]
        self.next[table] = value + 1
        return value

    async def sync(self, conn: asyncpg.Connection) -> None:
        for table in _TABLES:
            await conn.execute(
                f"select setval(pg_get_serial_sequence('crm.{table}', 'id'), "
                f"greatest((select max(id) from crm.{table}), 1))")


class _Balances:
    """Баланс клиента на любой момент: журнал по времени нарастающим итогом.

    Напоминания, аудитории и просьбы об отзыве решают по балансу на тот
    час, как решал бы дневной проход, а не по сегодняшнему: иначе история
    уведомлений разошлась бы с журналом денег.
    """

    def __init__(self) -> None:
        self.times: dict[int, list[datetime]] = defaultdict(list)
        self.sums: dict[int, list[Decimal]] = defaultdict(list)

    @classmethod
    async def load(cls, conn: asyncpg.Connection) -> _Balances:
        out = cls()
        for r in await conn.fetch("select client_id, amount, created_at from crm.ledger "
                                  "order by client_id, created_at, id"):
            sums = out.sums[r["client_id"]]
            out.times[r["client_id"]].append(r["created_at"])
            sums.append((sums[-1] if sums else D(0)) + r["amount"])
        return out

    def at(self, client_id: int, t: datetime) -> Decimal:
        times = self.times.get(client_id)
        if not times:
            return D(0)
        i = bisect.bisect_right(times, t)
        return self.sums[client_id][i - 1] if i else D(0)


@dataclass
class _Ctx:
    w: World
    rng: random.Random
    ids: _Ids
    balance: dict[int, Decimal]
    company: dict[str, str]
    charges: dict[int, list[dict[str, Any]]]
    secret: str = DEMO_SECRET
    rentals: dict[int, Rental] = field(default_factory=dict)
    by_client: dict[int, list[Rental]] = field(default_factory=dict)
    pays: dict[int, list[Payment]] = field(default_factory=dict)
    tg: dict[int, int] = field(default_factory=dict)
    mx: dict[int, int] = field(default_factory=dict)
    notes: list[tuple] = field(default_factory=list)
    reviewed: dict[int, datetime] = field(default_factory=dict)
    open_bookings: set[int] = field(default_factory=set)
    bank: list[tuple] = field(default_factory=list)
    ref_launch: datetime | None = None

    @classmethod
    async def load(cls, conn: asyncpg.Connection, w: World) -> _Ctx:
        start = {t: int(await conn.fetchval(f"select coalesce(max(id), 0) from crm.{t}")) + 1
                 for t in _TABLES}
        balance = {r["client_id"]: D(r["b"]) for r in await conn.fetch(
            "select client_id, sum(amount) as b from crm.ledger group by client_id")}
        company = {r["key"]: r["value"] for r in await conn.fetch(
            "select key, value from crm.settings where key like 'company_%'")}
        charges: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for r in await conn.fetch(
                "select id, rental_id, period_from, period_to, created_at from crm.ledger "
                "where kind = 'charge' and rental_id is not null "
                "order by rental_id, period_from"):
            charges[r["rental_id"]].append(dict(r))
        c = cls(w=w, rng=random.Random(f"demo-extras:{w.seed}:{w.attempt}"),
                ids=_Ids(start), balance=defaultdict(D, balance), company=company,
                charges=charges)
        c.rentals = {r.id: r for r in w.rentals}
        by_client: dict[int, list[Rental]] = defaultdict(list)
        for r in sorted(w.rentals, key=lambda r: (r.created_at, r.id)):
            by_client[r.client_id].append(r)
        c.by_client = by_client
        pays: dict[int, list[Payment]] = defaultdict(list)
        for p in sorted(w.payments, key=lambda p: (p.created_at, p.ledger_id)):
            pays[p.client_id].append(p)
        c.pays = pays
        return c

    @property
    def now(self) -> datetime:
        return self.w.now

    @property
    def today(self) -> date:
        return self.w.today

    def active_of(self, client_id: int) -> Rental | None:
        for r in self.by_client.get(client_id, []):
            if r.status == "active":
                return r
        return None

    def active_at(self, client_id: int, t: datetime) -> Rental | None:
        for r in self.by_client.get(client_id, []):
            if _alive(r, t):
                return r
        return None

    def reachable(self, client_id: int | None) -> bool:
        return client_id is not None and (client_id in self.tg or client_id in self.mx)

    def may_bonus(self, client_id: int) -> bool:
        """Баллы - только тем, у кого они что-то значат: не заблокированным и
        не намеренным должникам ядра (их долг без аренды - часть демо)."""
        client = self.w.client(client_id)
        if client.status != "active":
            return False
        return self.balance[client_id] >= 0 or self.active_of(client_id) is not None

    def billed_until(self, r: Rental, t: datetime) -> date:
        """billed_until аренды на момент t: конец последнего начисленного к
        тому часу периода. До первого начисления - день начала, как у
        create_rental."""
        until = r.started_on
        for charge in self.charges.get(r.id, []):
            if charge["created_at"] <= t and charge["period_to"] > until:
                until = charge["period_to"]
        return until

    def covered(self, r: Rental, t: datetime, bal: _Balances) -> date:
        """«Оплачено до» на момент t - той же формулой, что у бота."""
        return logic.covered_until(self.billed_until(r, t), bal.at(r.client_id, t),
                                   r.price, r.period_days)

    def notice(self, t: datetime, code: str, client_id: int | None = None, *,
               status: str | None = None, detail: str | None = None) -> None:
        """Строка истории уведомлений. Клиенту без мессенджера - «пропущено»,
        как у настоящего прохода; история живёт 30 дней - старше не пишем."""
        if not (self.now - timedelta(days=logic.NOTICE_LOG_DAYS) <= t <= self.now):
            return
        target = logic.NOTICES[code]["target"]
        if status is None:
            if target == "client" and not self.reachable(client_id):
                status, detail = "skipped", "клиента нет в боте"
            else:
                status = "sent"
        self.notes.append((t, code, client_id, target, status, detail))

    def bonus(self, ledger: list[tuple], bonuses: list[tuple], *, client_id: int,
              kind: str, amount: Decimal, t: datetime, note: str, by: str,
              rental_id: int | None = None, pf: date | None = None,
              pt: date | None = None, ref_id: int | None = None,
              promo_id: int | None = None) -> int:
        """Баллы: запись журнала видом bonus и повод рядом - как grant_bonus."""
        lid = self.ids.take("ledger")
        ledger.append((lid, client_id, rental_id, "bonus", amount, None, pf, pt, note,
                       by, t, None))
        bonuses.append((self.ids.take("bonuses"), client_id, kind, amount, lid, ref_id,
                        note, by, t, promo_id, rental_id, pf if promo_id else None))
        self.balance[client_id] += amount
        return lid

    def operator(self, point: str | None) -> str:
        return self.w.operator(point).actor


async def _copy(conn: asyncpg.Connection, table: str, columns: list[str],
                records: list[tuple]) -> None:
    if records:
        await conn.copy_records_to_table(table, schema_name="crm", columns=columns,
                                         records=records)


async def _money(conn: asyncpg.Connection, ledger: list[tuple],
                 bonuses: list[tuple]) -> None:
    await _copy(conn, "ledger", _LEDGER, ledger)
    await _copy(conn, "bonuses", _BONUS, bonuses)


async def _next_no(conn: asyncpg.Connection, table: str) -> int:
    """Следующий номер документа - как у панели: максимум хвоста плюс один."""
    return int(await conn.fetchval(
        f"select coalesce(max(substring(no from '[0-9]+$')::bigint), 0) + 1 "
        f"from crm.{table}"))


def _msk(t: datetime) -> datetime:
    return t.astimezone(MSK)


def _alive(r: Rental, t: datetime) -> bool:
    return r.created_at <= t and (r.closed_at is None or r.closed_at > t)


def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _short(full_name: str) -> str:
    """«Сафин Ильдар Айдарович» → «Ильдар С.»: так имя видно в мессенджере."""
    parts = full_name.split()
    return f"{parts[1]} {parts[0][0]}." if len(parts) > 1 else full_name


def _spoken_phone(phone: str) -> str:
    """+70000123456 → «8 000 012-34-56»: так телефон пишут в назначении."""
    d = phone[-10:]
    return f"8 {d[:3]} {d[3:6]}-{d[6:8]}-{d[8:]}"


def _imported(p: Payment) -> bool:
    """Остаток, перенесённый из таблицы в день перехода на CRM: ни счёта,
    ни строки выписки у него нет - деньги пришли до системы."""
    return p.note.startswith("Остаток из таблицы")


def _thousands(amount: Decimal) -> str:
    return f"{int(amount):,}".replace(",", " ")


# ─────────────────────── мессенджеры клиентов ───────────────────────

async def _messengers(conn: asyncpg.Connection, c: _Ctx) -> None:
    """Telegram и MAX у клиентов. Без них пусты аудитории рассылок,
    воронка приглашений (друг приходит по ссылке в бот) и половина
    «Входящих». Номера вымышленные: TG_BASE/MAX_BASE плюс id карточки."""
    for client in sorted(c.w.clients, key=lambda x: x.id):
        roll = c.rng.random()
        if client.invited_by is not None or roll < 0.7:
            c.tg[client.id] = TG_BASE + client.id
        elif roll < 0.8:
            c.mx[client.id] = MAX_BASE + client.id
    await _set_messengers(conn, c)


async def _set_messengers(conn: asyncpg.Connection, c: _Ctx) -> None:
    for column, ids in (("tg_id", c.tg), ("max_id", c.mx)):
        keys = sorted(ids)
        await conn.execute(
            f"update crm.clients x set {column} = u.v "
            "from unnest($1::bigint[], $2::bigint[]) as u(id, v) where x.id = u.id",
            keys, [ids[k] for k in keys])


async def _new_clients(conn: asyncpg.Connection, c: _Ctx) -> None:
    """Три свежие регистрации из бота без аренды: двое по приглашению
    (воронка «зарегистрировался»), один с Авито. Двоим из них - открытые
    заявки на аренду."""
    w, rng = c.w, c.rng
    agents = [x for x in w.clients if x.ref_code and x.status == "active"
              and c.active_of(x.id) is not None]
    agents.sort(key=lambda x: x.id)
    plan = (("referral", 1.3), ("avito", 0.3), ("referral", 3.2))
    rows = []
    for channel, days in plan:
        name, phone = w.people.person()
        created = c.now - timedelta(days=days, minutes=rng.uniform(0, 90))
        w.contract_seq += 1
        client = Client(
            id=c.ids.take("clients"), full_name=name, phone=phone,
            employer=rng.choice(("yandex", "samokat", "sbermarket")), channel=channel,
            experience=rng.choice(("under_year", "years_1_3")), created_at=created,
            point=None, contract_no=contract_number(w.contract_seq, today=created.date()),
            source="bot")
        if channel == "referral" and agents:
            agent = agents.pop(rng.randrange(len(agents)))
            client.invited_by = agent.id
            client.invited_at = created - timedelta(minutes=rng.uniform(3, 40))
        w.clients.append(client)
        c.tg[client.id] = TG_BASE + client.id
        rows.append((client.id, client.full_name, client.phone, client.status,
                     client.contract_no, client.source, created, created,
                     client.invited_by, client.invited_at, client.channel, client.employer,
                     client.experience, c.tg[client.id]))
    await _copy(conn, "clients",
                ["id", "full_name", "phone", "status", "contract_no", "source", "created_at",
                 "updated_at", "invited_by", "invited_at", "channel", "employer",
                 "experience", "tg_id"], rows)
    await conn.execute("select setval('bot.contract_seq', $1)", w.contract_seq)


# ─────────────────────────── трекеры ───────────────────────────

def _shift(ll: tuple[float, float], north_km: float, east_km: float) -> tuple[float, float]:
    lat, lon = ll
    return (lat + north_km / 111.0, lon + east_km / (111.0 * math.cos(math.radians(lat))))


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    return logic.distance_km(a[0], a[1], b[0], b[1]) or 0.0


def _bearing(a: tuple[float, float], b: tuple[float, float]) -> int:
    north = b[0] - a[0]
    east = (b[1] - a[1]) * math.cos(math.radians(a[0]))
    return int(math.degrees(math.atan2(east, north))) % 360


class _Track:
    """Точки журнала одного трекера: (время, широта, долгота, скорость, курс)."""

    def __init__(self) -> None:
        self.points: list[tuple[datetime, float, float, float, int]] = []

    def add(self, t: datetime, ll: tuple[float, float], speed: float = 0.0,
            course: int = 0) -> None:
        self.points.append((t, round(ll[0], 6), round(ll[1], 6),
                            round(max(speed, 0.0), 1), course))

    def last_ll(self) -> tuple[float, float] | None:
        return (self.points[-1][1], self.points[-1][2]) if self.points else None


def _ride(rng: random.Random, track: _Track, t: datetime, src: tuple[float, float],
          dst: tuple[float, float], *, step: float) -> datetime:
    """Поездка с опросом раз в step минут. Шаг держит соседние точки ближе
    5 км: дальше линия трека на карточке считает скачком и рвётся."""
    minutes = max(_km(src, dst) / rng.uniform(14, 24) * 60, 2.0)
    speed = _km(src, dst) / minutes * 60
    course = _bearing(src, dst)
    # Поездка короче шага опроса - всё равно одна точка в пути: иначе у
    # велосипеда, выданного вчера вечером, не было бы ни одной отметки
    # «ехал», и опрос поднял бы ему «стоит при аренде».
    marks = [k * step for k in range(1, int(minutes // step) + 1) if k * step < minutes]
    if not marks and speed >= 5:
        marks = [minutes / 2]
    for m in marks:
        f = m / minutes
        ll = (src[0] + (dst[0] - src[0]) * f + rng.gauss(0, 0.0005),
              src[1] + (dst[1] - src[1]) * f + rng.gauss(0, 0.0008))
        track.add(t + timedelta(minutes=m), ll, max(speed + rng.uniform(-4, 4), 6.0),
                  course)
    arrive = t + timedelta(minutes=minutes)
    track.add(arrive, dst, 0.0, course)
    return arrive


def _home(rng: random.Random, point: tuple[float, float]) -> tuple[float, float]:
    """Дом курьера: ближние к точке районы вероятнее дальних."""
    weights = [1 / (_km(d, point) + 1.5) for d in DISTRICTS]
    base = rng.choices(DISTRICTS, weights=weights)[0]
    return _shift(base, rng.gauss(0, 0.8), rng.gauss(0, 0.8))


def _courier(rng: random.Random, *, start: datetime, end: datetime, window: datetime,
             home: tuple[float, float], point: tuple[float, float],
             back: tuple[float, float] | None, today: date,
             rest_from: datetime | None = None) -> _Track:
    """Велосипед у курьера: ночью дома, днём 4-7 заказов по району, выходной
    не чаще через день; в конце аренды - обратно на точку. Пишется только
    то, что попадает в окно журнала (месяц) - раньше смысла считать нет."""
    track = _Track()
    t = start
    cur = point
    if start >= window:
        track.add(start, point)
        t = _ride(rng, track, start + timedelta(minutes=rng.uniform(3, 12)), point, home,
                  step=3.0 if _msk(start).date() == today else 7.0)
        cur = home
    else:
        t = window - DAY
        cur = home
    day = _msk(t).date()
    off_before = False
    while at(day) < end:
        dstart = at(day)
        step = 3.0 if day == today else rng.choice((6.0, 7.0, 8.0))
        for hour in (0.7, 3.4, 6.3):
            ping = dstart + timedelta(hours=hour + rng.uniform(0, 0.8))
            if t < ping < end:
                track.add(ping, cur)
                t = ping
        resting = rest_from is not None and dstart + timedelta(hours=8) >= rest_from
        off = not off_before and rng.random() < 0.12
        off_before = off
        if off or resting:
            for hour in (11.0, 15.0, 19.5):
                ping = dstart + timedelta(hours=hour + rng.uniform(0, 1))
                if t < ping < end:
                    track.add(ping, cur)
                    t = ping
        else:
            begin = dstart + timedelta(hours=rng.uniform(8.5, 11))
            finish = dstart + timedelta(hours=rng.uniform(19.5, 22.5))
            trips = rng.randint(4, 7)
            slot = (finish - begin) / trips
            for k in range(trips):
                depart = begin + slot * k + slot * rng.uniform(0, 0.35)
                if depart <= t or depart >= end:
                    continue
                if rng.random() < 0.25:
                    dest = _shift(CENTER, rng.gauss(0, 0.9), rng.gauss(0, 0.9))
                else:
                    dest = _shift(home, rng.uniform(-3, 3), rng.uniform(-3.5, 3.5))
                t = _ride(rng, track, depart, cur, dest, step=step)
                cur = dest
            if t < finish < end:
                t = _ride(rng, track, finish, cur, home, step=step)
                cur = home
        day += DAY
    if back is not None:
        # Сдача: последний отрезок - на точку, к моменту возврата.
        track.points = [p for p in track.points if p[0] < end - timedelta(minutes=45)]
        src = track.last_ll() or cur
        minutes = max(_km(src, back) / 18 * 60, 5.0)
        depart = end - timedelta(minutes=minutes)
        if not track.points or depart > track.points[-1][0]:
            _ride(rng, track, depart, src, back, step=6.0)
    track.points = [p for p in track.points if window <= p[0] <= end]
    return track


def _parked(rng: random.Random, track: _Track, *, start: datetime, end: datetime,
            ll: tuple[float, float]) -> None:
    """Стоит на точке: сигнал раз в несколько часов, дрожание GPS в метрах."""
    t = start + timedelta(minutes=rng.uniform(5, 40))
    while t < end:
        track.add(t, (ll[0] + rng.gauss(0, 0.00012), ll[1] + rng.gauss(0, 0.0002)))
        t += timedelta(hours=rng.uniform(2.5, 5))


async def _pieces(conn: asyncpg.Connection, bike_ids: list[int],
                  now: datetime) -> dict[int, list[tuple[datetime, datetime, str, str]]]:
    """Журналы статусов и мест велосипеда - в куски «с - по, статус, точка»."""
    events: dict[int, list[tuple[datetime, int, str, str]]] = defaultdict(list)
    for r in await conn.fetch(
            "select bike_id, to_status, changed_at from crm.bike_status_log "
            "where bike_id = any($1::bigint[]) order by bike_id, changed_at, id", bike_ids):
        events[r["bike_id"]].append((r["changed_at"], 0, "s", r["to_status"]))
    for r in await conn.fetch(
            "select bike_id, to_location, changed_at from crm.bike_location_log "
            "where bike_id = any($1::bigint[]) order by bike_id, changed_at, id", bike_ids):
        events[r["bike_id"]].append((r["changed_at"], 1, "l", r["to_location"]))
    out: dict[int, list[tuple[datetime, datetime, str, str]]] = {}
    for bike_id, evs in events.items():
        evs.sort(key=lambda e: (e[0], e[1]))
        status, place, since = "", "", None
        pieces: list[tuple[datetime, datetime, str, str]] = []
        for t, _order, what, value in evs:
            if since is not None and t > since:
                pieces.append((since, t, status, place))
            if what == "s":
                status = value
            else:
                place = value or ""
            since = t
        if since is not None and since < now:
            pieces.append((since, now, status, place))
        out[bike_id] = pieces
    return out


async def _trackers(conn: asyncpg.Connection, c: _Ctx) -> None:
    """Трекеры у велосипедов с отметкой «трекер установлен», журнал позиций
    за месяц по журналам статусов и мест, тревоги ровно те, что поднял бы
    опрос (logic.detect_alerts), и одна блокировка мотора в прошлом.

    Истории: двое в розыске молчат (тревога «не выходит на связь»), один
    курьер болеет и стоит четвёртые сутки («это норма»), у одного садится
    питание трекера; ночью полторы недели назад свободный велосипед
    «поехал» с точки - тревога закрылась сама. Велосипед, признанный
    потерянным последним, в розыске получил блокировку мотора; трекеры
    потерянных сняты с наблюдения.
    """
    w, rng, now = c.w, c.rng, c.now
    window = now - timedelta(days=30)
    active = w.active_rentals()
    search = sorted((r for r in active if r.search_at is not None),
                    key=lambda r: r.search_at)
    lost = sorted((r for r in w.rentals if r.lost), key=lambda r: r.closed_at)
    story = lost[-1] if lost else None
    tracked = [b for b in w.bikes if b.status in logic.OPERATIONAL_STATUSES and b.tracker]
    forced = {r.bike_id for r in search}
    tracked += [w.bike(bid) for bid in sorted(forced) if not w.bike(bid).tracker]
    gone = [b for b in w.bikes if b.status == "lost" and b.tracker]
    if story is not None and not w.bike(story.bike_id).tracker:
        gone.append(w.bike(story.bike_id))
    bikes = sorted(tracked + gone, key=lambda b: b.id)
    pieces = await _pieces(conn, [b.id for b in bikes], now)
    points = {name: (p.lat, p.lon) for name, p in w.points.items()}

    # Роли историй - из аренд, идущих достаточно долго на одном велосипеде.
    steady = [r for r in active if r.search_at is None and len(r.bike_ids) == 1
              and (c.today - r.started_on).days >= 8 and w.bike(r.bike_id).tracker]
    steady.sort(key=lambda r: r.id)
    idle = steady.pop(rng.randrange(len(steady))) if steady else None
    weak = steady.pop(rng.randrange(len(steady))) if steady else None
    rest_from = now - timedelta(days=4, hours=rng.uniform(1, 5))
    silent_at = {r.bike_id: r.search_at - timedelta(hours=rng.uniform(20, 30))
                 for r in search}
    block_at = (story.search_at + timedelta(days=1, hours=rng.uniform(1, 4))
                if story is not None and story.search_at is not None else None)
    # Ночная тревога: свободный велосипед тронулся с точки, через десять
    # минут стоял на месте - опрос закрыл тревогу сам.
    night = at(c.today - timedelta(days=9), 2.6 + rng.uniform(0, 0.4))
    night_move: tuple[int, tuple[float, float], str] | None = None
    for b in bikes:
        piece = next((p for p in pieces.get(b.id, [])
                      if p[0] < night - timedelta(hours=1)
                      and night + timedelta(hours=1) < p[1]
                      and p[2] in ("available", "reserved")), None)
        if piece is not None and b.status != "lost":
            night_move = (b.id, points.get(piece[3]) or points[core.P1], piece[2])
            break

    rows, positions, alerts, commands = [], [], [], []
    tracker_of: dict[int, int] = {}
    fixes: dict[int, _Track] = {}
    for n, b in enumerate(bikes):
        tid = c.ids.take("trackers")
        tracker_of[b.id] = tid
        installed = (b.commissioned_at - timedelta(minutes=35) if b.commissioned_at
                     else b.created_at)
        is_story = story is not None and b.id == story.bike_id
        track = _Track()
        homes: dict[datetime, tuple[float, float]] = {}
        plist = pieces.get(b.id, [])
        for i, (start, end, status, place) in enumerate(plist):
            if end <= window or end <= installed:
                continue
            ll = points.get(place) or points[core.P1]
            if status == "rented":
                nxt = plist[i + 1] if i + 1 < len(plist) else None
                back = (points.get(nxt[3]) or ll) if nxt and nxt[2] != "lost" else None
                home = FAR_AWAY if is_story else homes.setdefault(start, _home(rng, ll))
                part = _courier(rng, start=start, end=end, window=max(window, installed),
                                home=home, point=ll, back=back, today=c.today,
                                rest_from=rest_from if idle and b.id == idle.bike_id
                                else None)
                track.points += part.points
            elif status in logic.TRACKER_PARKED_STATUSES:
                _parked(rng, track, start=max(start, window, installed), end=end, ll=ll)
        if night_move is not None and b.id == night_move[0]:
            for minutes, north, speed in ((0, 0.25, 17.0), (4, 0.6, 12.0), (11, 0.02, 0.0)):
                track.add(night + timedelta(minutes=minutes, seconds=7),
                          _shift(night_move[1], north, 0.1 * minutes / 4), speed, 20)
        track.points.sort(key=lambda p: p[0])
        seen: set[datetime] = set()
        track.points = [p for p in track.points
                        if p[0] not in seen and not seen.add(p[0])]
        cut = silent_at.get(b.id)
        if is_story and block_at is not None:
            cut = block_at + timedelta(minutes=40)
        elif b.status == "lost":
            cut = window
        if cut is not None:
            track.points = [p for p in track.points if p[0] <= cut]
        fixes[b.id] = track

        online = b.status != "lost" and b.id not in silent_at
        if online:
            last = track.last_ll() or points.get(b.point or core.P1) or points[core.P1]
            ping = now - timedelta(minutes=rng.uniform(1, 9))
            if track.points and track.points[-1][0] >= ping:
                ping = track.points[-1][0] + timedelta(seconds=30)
            moving = (b.status == "rented" and 9 <= now.hour < 22 and rng.random() < 0.18
                      and not (idle and b.id == idle.bike_id))
            if moving:
                course = rng.randrange(360)
                ll = _shift(last, 0.3 * math.cos(math.radians(course)),
                            0.3 * math.sin(math.radians(course)))
                track.add(min(ping, now), ll, rng.uniform(13, 25), course)
            else:
                track.add(min(ping, now), last)
        # «Ехал» - последняя точка со скоростью от порога, как ставит опрос
        # (save_tracker_state). Не ехал за месяц журнала - с тех пор, как
        # встал в нынешнее состояние: обратный путь на точку был раньше.
        moved = [p[0] for p in track.points if p[3] >= float(logic.TRACKER_MOVING_SPEED)]
        last_pt = track.points[-1] if track.points else None
        moved_at = moved[-1] if moved else (plist[-1][0] if plist else None)
        if b.status == "lost" and not is_story:
            lost_r = next((r for r in lost if r.bike_id == b.id), None)
            last_seen = (lost_r.search_at - DAY if lost_r and lost_r.search_at
                         else b.created_at)
            lat = lon = speed = course = None
            moved_at = None
        elif is_story and last_pt is None:
            # История старше месяца журнала: точек уже вычистили, а карточка
            # трекера помнит, где он замолчал.
            last_seen = (block_at + timedelta(minutes=40) if block_at
                         else story.search_at or story.created_at)
            lat, lon = round(FAR_AWAY[0], 6), round(FAR_AWAY[1], 6)
            speed, course, moved_at = D(0), None, last_seen
        else:
            last_seen = last_pt[0] if last_pt else None
            lat, lon = (last_pt[1], last_pt[2]) if last_pt else (None, None)
            speed = D(str(last_pt[3])) if last_pt and online else D(0)
            course = last_pt[4] if last_pt else None
        voltage = D(str(round(rng.uniform(12.3, 13.4), 2)))
        if weak and b.id == weak.bike_id:
            voltage = D("11.30")
        note = None
        if b.status == "lost":
            note = "Снят с наблюдения: велосипед признан потерянным (демо)"
        rows.append((
            tid, str(10_400_000 + n * 13 + rng.randrange(13)), b.code, b.id,
            b.status != "lost", last_seen, lat, lon, speed, course, voltage,
            rng.randint(14, 31), False, note, installed,
            (story.closed_at if is_story and story.closed_at else now)
            if b.status == "lost" else now,
            moved_at, bool(is_story and block_at), block_at + timedelta(minutes=3)
            if is_story and block_at else None,
            "staff:demo" if is_story and block_at else None,
            w.people.phone() if rng.random() < 0.6 else None))
        for p in track.points:
            positions.append((tid, p[1], p[2], D(str(p[3])), p[4], p[0]))

    def alert(bike_id: int, kind: str, created: datetime, note: str, *, state: str,
              taken_by: str | None = None, taken_at: datetime | None = None,
              snooze: datetime | None = None, handled: datetime | None = None,
              handled_by: str | None = None) -> None:
        track = fixes[bike_id]
        before = [p for p in track.points if p[0] <= created] or track.points
        lat, lon = (before[-1][1], before[-1][2]) if before else (None, None)
        alerts.append((c.ids.take("tracker_alerts"), tracker_of[bike_id], bike_id, kind,
                       note, lat, lon, created, handled, handled_by,
                       logic.alert_level(kind), state, taken_by, taken_at, snooze))

    limits = logic.tracker_settings({"tracker_offline_hours": str(DEMO_OFFLINE_HOURS)})
    for i, r in enumerate(search):
        created = silent_at[r.bike_id] + timedelta(hours=limits["offline_hours"],
                                                   minutes=rng.uniform(1, 5))
        if created > now:
            continue
        note = f"молчит {limits['offline_hours']} ч"
        if i == 0:
            alert(r.bike_id, "offline", created, note, state="working",
                  taken_by="staff:demo", taken_at=created + timedelta(hours=1.5))
        else:
            taken = max(created + timedelta(hours=1), now - timedelta(hours=5))
            alert(r.bike_id, "offline", created, note, state="snoozed",
                  taken_by=c.operator(r.point), taken_at=min(taken, now),
                  snooze=now + timedelta(hours=2))
    if idle is not None:
        moved = [p[0] for p in fixes[idle.bike_id].points
                 if p[3] >= float(logic.TRACKER_MOVING_SPEED)]
        if moved:
            created = moved[-1] + timedelta(days=limits["idle_days"],
                                            minutes=rng.uniform(2, 6))
            if created < now:
                alert(idle.bike_id, "idle_rented", created,
                      f"стоит {limits['idle_days']} сут., аренда идёт", state="normal",
                      taken_by=c.operator(idle.point),
                      taken_at=min(created + timedelta(hours=2), now))
    if weak is not None:
        alert(weak.bike_id, "low_power", now - timedelta(hours=rng.uniform(3, 7)),
              f"питание {logic.to_money(D('11.30'))} В, порог {limits['low_volts']} В",
              state="new")
    if night_move is not None:
        alert(night_move[0], "moving", night + timedelta(seconds=7),
              f"17 км/ч, по учёту — {logic.BIKE_STATUSES[night_move[2]].lower()}",
              state="working", taken_by="staff:demo",
              taken_at=night + timedelta(minutes=6),
              handled=night + timedelta(minutes=17), handled_by="tracking")
    if story is not None and block_at is not None:
        commands.append((c.ids.take("tracker_commands"), tracker_of[story.bike_id], "block",
                         None, "Клиент в розыске: не выходит на связь, велосипед не вернул",
                         "staff:demo", block_at, block_at + timedelta(minutes=3), True,
                         "StarLine принял команду"))

    await _copy(conn, "trackers",
                ["id", "device_id", "alias", "bike_id", "active", "last_seen", "lat", "lon",
                 "speed", "course", "voltage", "gsm_level", "alarm", "note", "created_at",
                 "updated_at", "moved_at", "blocked", "blocked_at", "blocked_by", "phone"],
                rows)
    # Позиции - по порядку времени: так их читает трек и так их писал опрос.
    positions.sort(key=lambda p: (p[5], p[0]))
    first = c.ids.next["tracker_positions"]
    c.ids.next["tracker_positions"] = first + len(positions)
    await _copy(conn, "tracker_positions",
                ["id", "tracker_id", "lat", "lon", "speed", "course", "recorded_at"],
                [(first + i, *p) for i, p in enumerate(positions)])
    await _copy(conn, "tracker_alerts",
                ["id", "tracker_id", "bike_id", "kind", "note", "lat", "lon", "created_at",
                 "handled_at", "handled_by", "level", "state", "taken_by", "taken_at",
                 "snooze_until"], alerts)
    await _copy(conn, "tracker_commands",
                ["id", "tracker_id", "command", "alert_id", "note", "requested_by",
                 "requested_at", "sent_at", "ok", "result"], commands)
    # Галочка «трекер установлен» - ровно у тех, на ком трекер есть: у
    # проданных и списанных его сняли, розыску поставили при выдаче.
    await conn.execute(
        "update crm.bikes set tracker_ok = (id = any($1::bigint[]))",
        sorted(tracker_of))


# ─────────────────────────── банк ───────────────────────────

_PURPOSES = (
    "Оплата аренды по договору {contract}",
    "Перевод по договору {contract}, аренда велосипеда",
    "Аренда электровелосипеда, договор {contract}",
    "Продление аренды, тел. {phone}",
    "Оплата за велосипед {name}",
)
_IGNORED = (
    ("ООО «ДЕМО-ДОСТАВКА»", "0000000000", D("4500"),
     "Оплата по счёту № 17 за ремонт электросамоката (демо)"),
    ("ООО «ДЕМО-ВЕЛО»", "0000000000", D("2300"),
     "Возврат излишне уплаченных средств по счёту 88 (демо)"),
    ("ДЕМО ДЕМЬЯН ДЕМИДОВИЧ", None, D("50000"), "Перевод собственных средств (демо)"),
)
_DEBITS = (
    (2, "ООО «ДЕМО-НЕДВИЖИМОСТЬ»", D("45000"), "Аренда помещения ул. Павлюхина за месяц (демо)"),
    (2, "ООО «ДЕМО-НЕДВИЖИМОСТЬ»", D("38000"), "Аренда помещения ул. Адоратского за месяц (демо)"),
    (3, "ИП Демов Д. Д.", D("30000"), "Аренда помещения пр. Победы за месяц (демо)"),
    (6, "ООО «ДЕМО-ВЕЛО»", D("18400"), "Оплата по счёту 131 за запчасти (демо)"),
    (12, "АО «ДЕМО-БАНК»", D("1490"), "Комиссия за ведение счёта (демо)"),
    (15, "ООО «ДЕМО-ТЕЛЕКОМ»", D("3600"), "Связь для трекеров, 120 SIM (демо)"),
    (21, "ООО «ДЕМО-ВЕЛО»", D("26750"), "Оплата по счёту 127 за аккумуляторы (демо)"),
    (25, "УФК по Республике Татарстан (демо)", D("21000"), "Авансовый платёж УСН (демо)"),
)


async def _bank(conn: asyncpg.Connection, c: _Ctx) -> None:
    """Выписка Точки за 30 дней - с тех пор, как банк переехал в панель.

    Переводы на счёт за продления (в журнале ядра - «staff:demo») стали
    зачисленными строками: платёж и строка связаны ledger_id, а запись
    журнала получает назначение из выписки - как её пишет «Зачислить»
    (service.credit_bank_txn). Несколько поступлений ждут оператора: одно
    узнаётся по номеру договора, одно по телефону, одно по ФИО, одно чужое.
    """
    w, rng, now = c.w, c.rng, c.now
    since = now - timedelta(days=30)
    account = c.company.get("company_account") or "40802810000000000000"
    rows, notes = [], []

    def txn(booked: datetime, amount: Decimal, direction: str, payer: str | None,
            inn: str | None, purpose: str, status: str = "new",
            client_id: int | None = None, ledger_id: int | None = None,
            handled: datetime | None = None, by: str | None = None) -> None:
        fetched = booked + timedelta(minutes=rng.uniform(4, 25))
        if handled is not None:
            fetched = min(fetched, handled - timedelta(seconds=30))
        rows.append((c.ids.take("bank_txns"), _uuid(rng), account, booked, amount,
                     direction, payer, inn, purpose, status, client_id, ledger_id,
                     handled, by, min(fetched, now)))

    for p in w.payments:
        if (p.method != "transfer" or not since <= p.created_at <= now
                or p.note.startswith("При выдаче") or _imported(p)):
            continue
        client = w.client(p.client_id)
        purpose = rng.choice(_PURPOSES).format(
            contract=client.contract_no or "", phone=_spoken_phone(client.phone),
            name=client.full_name)
        booked = max(p.created_at - timedelta(minutes=rng.uniform(15, 300)), since)
        txn(booked, p.amount, "credit", client.full_name.upper(), None, purpose,
            "matched", client.id, p.ledger_id, p.created_at, "staff:demo")
        notes.append((p.ledger_id, f"Выписка банка: {purpose}"[:500]))
        c.notice(p.created_at, "pay_credited", client.id)
    # Ждут оператора: по договору, по телефону, по ФИО - и одно чужое.
    # Платит тот, кто уже катается: карточка и договор старше перевода.
    payers = [r for r in w.active_rentals() if r.search_at is None
              and r.created_at < now - timedelta(days=3)]
    payers.sort(key=lambda r: r.id)
    picks = rng.sample(payers, 3) if len(payers) >= 3 else payers
    for i, r in enumerate(picks):
        client = w.client(r.client_id)
        booked = now - timedelta(hours=rng.uniform(2, 30) + 6 * i)
        purpose = (f"Оплата аренды по договору {client.contract_no}",
                   f"Аренда вела, тел {_spoken_phone(client.phone)}",
                   "Перевод по номеру счёта")[i]
        txn(booked, r.price, "credit", client.full_name.upper(), None, purpose)
    txn(now - timedelta(hours=rng.uniform(1, 20)), D("6200"), "credit",
        "ООО «ДЕМО-ЛОГИСТИК»", "0000000000",
        "Оплата по счёту № 45 за ремонт трицикла (демо)")
    for k, (payer, inn, amount, purpose) in enumerate(_IGNORED):
        booked = now - timedelta(days=4 + 8 * k, hours=rng.uniform(0, 8))
        txn(booked, amount, "credit", payer, inn, purpose, "ignored",
            handled=booked + timedelta(hours=rng.uniform(1, 5)), by="staff:demo")
    for days, payer, amount, purpose in _DEBITS:
        booked = at(c.today - timedelta(days=days), rng.uniform(10, 17))
        txn(booked, amount, "debit", payer, "0000000000", purpose)
    rows.sort(key=lambda r: r[3])
    first = rows[0][0] if rows else 0
    rows = [(first + i,) + r[1:] for i, r in enumerate(rows)]
    c.bank = rows
    await _copy(conn, "bank_txns",
                ["id", "txn_id", "account", "booked_at", "amount", "direction",
                 "payer_name", "payer_inn", "purpose", "status", "client_id", "ledger_id",
                 "handled_at", "handled_by", "created_at"], rows)
    await _ledger_notes(conn, notes)


async def _ledger_notes(conn: asyncpg.Connection, notes: list[tuple[int, str]]) -> None:
    if notes:
        await conn.execute(
            "update crm.ledger l set note = u.note "
            "from unnest($1::bigint[], $2::text[]) as u(id, note) where l.id = u.id",
            [n[0] for n in notes], [n[1] for n in notes])


# ─────────────────────────── счета и карты ───────────────────────────

async def _pay_orders(conn: asyncpg.Connection, c: _Ctx) -> None:
    """Счета на оплату. Каждый платёж эквайринга ядра - это оплаченный счёт
    по ссылке: счёт ссылается на платёж, платёж подписан номером счёта, как
    их связывает mark_pay_paid. Плюс живые: один ждёт оплату (моложе суток),
    один с отказом банка, один снятый оператором. Карты - у нескольких
    плательщиков по ссылке; автосписание выключено.

    Номера СЧТ - по времени через все счета, вместе со счётом за ремонт
    сервиса: номер выдаёт база по порядку, и май после августа в списке
    выглядел бы ошибкой.
    """
    w, rng, now = c.w, c.rng, c.now
    orders: list[dict[str, Any]] = []
    for p in w.payments:
        if p.method != "card" or _imported(p):
            continue
        rental = c.rentals.get(p.rental_id)
        created = p.created_at - timedelta(minutes=rng.uniform(3, 40))
        if rental is not None:
            created = max(created, rental.created_at + timedelta(seconds=20))
        created = min(created, p.created_at - timedelta(seconds=30))
        orders.append({"p": p, "rental": rental, "client_id": p.client_id,
                       "amount": p.amount, "created": created, "status": "paid",
                       "by": c.operator(p.point), "paid": p.created_at, "error": None,
                       "checked": p.created_at})
    live = [r for r in w.active_rentals() if r.search_at is None]
    live.sort(key=lambda r: r.id)
    taken: set[int] = set()
    for i, created in enumerate((now - timedelta(minutes=rng.uniform(40, 100)),
                                 now - timedelta(days=2, hours=rng.uniform(1, 5)),
                                 now - timedelta(days=6, hours=rng.uniform(1, 5)))):
        # Счёт выставляют по идущей аренде - значит, начатой раньше счёта.
        fits = [r for r in live if r.created_at < created - timedelta(hours=1)
                and r.client_id not in taken]
        if not fits:
            continue
        r = fits[rng.randrange(len(fits))]
        taken.add(r.client_id)
        status = ("sent", "failed", "cancelled")[i]
        error = (None, "Банк отклонил операцию: недостаточно средств на карте (демо)",
                 f"снял {c.operator(r.point)}")[i]
        checked = (created + timedelta(seconds=5), created + timedelta(minutes=12),
                   created + timedelta(hours=3))[i]
        orders.append({"p": None, "rental": r, "client_id": r.client_id,
                       "amount": r.price, "created": created, "status": status,
                       "by": c.operator(r.point), "paid": None, "error": error,
                       "checked": min(checked, now)})
    existing = [dict(r) for r in await conn.fetch(
        "select id, created_at from crm.pay_orders order by created_at, id")]
    timeline = sorted([(r["created_at"], 0, r["id"], None) for r in existing]
                      + [(o["created"], 1, i, o) for i, o in enumerate(orders)],
                      key=lambda x: (x[0], x[1], x[2]))
    renumber: list[tuple[int, str]] = []
    for number, (_t, _k, ref, order) in enumerate(timeline, start=1):
        if order is None:
            renumber.append((ref, logic.pay_no(number)))
        else:
            order["no"] = logic.pay_no(number)
    if renumber:
        # В два шага: номер уникален, и прямой обмен столкнулся бы сам с собой.
        ids = [r[0] for r in renumber]
        await conn.execute("update crm.pay_orders set no = 'tmp-' || id "
                           "where id = any($1::bigint[])", ids)
        await conn.execute(
            "update crm.pay_orders p set no = u.no "
            "from unnest($1::bigint[], $2::text[]) as u(id, no) where p.id = u.id",
            ids, [r[1] for r in renumber])
    orders.sort(key=lambda o: (o["created"], o["client_id"]))
    rows, notes = [], []
    cards: dict[int, datetime] = {}
    for o in orders:
        client = w.client(o["client_id"])
        rental = o["rental"]
        purpose = logic.pay_purpose(
            {"contract_no": client.contract_no},
            {"bike_code": w.bike(rental.bike_id).code} if rental else None)
        op = _uuid(rng)
        p = o["p"]
        rows.append((c.ids.take("pay_orders"), o["no"], client.id,
                     rental.id if rental else None, o["amount"], purpose, "link",
                     o["status"], "tochka", op, f"https://example.com/demo-pay/{op}",
                     o["error"], p.ledger_id if p else None, o["by"], o["created"],
                     o["created"] + timedelta(seconds=4), o["paid"], o["checked"], None))
        if p is not None:
            notes.append((p.ledger_id, f"Счёт {o['no']}"))
            # Как paying.tell_paid: карточка команде и зачисление клиенту.
            c.notice(p.created_at, "pay_paid", client.id)
            c.notice(p.created_at + timedelta(seconds=2), "pay_credited", client.id)
            cards[client.id] = p.created_at
    await _copy(conn, "pay_orders",
                ["id", "no", "client_id", "rental_id", "amount", "purpose", "kind",
                 "status", "provider", "operation_id", "link", "error", "ledger_id",
                 "created_by", "created_at", "sent_at", "paid_at", "checked_at",
                 "work_order_id"], rows)
    await _ledger_notes(conn, notes)
    # Карта привязывается, когда клиент платит по ссылке: у части
    # плательщиков с идущей арендой она есть. Списаний не было - выключено.
    holders = sorted(cid for cid in cards if c.active_of(cid) is not None)
    tokens = []
    for cid in rng.sample(holders, min(6, len(holders))):
        tokens.append((c.ids.take("card_tokens"), cid, "tochka",
                       f"demo-{uuid.UUID(int=rng.getrandbits(128)).hex}",
                       f"{rng.randrange(10000):04d}",
                       f"{rng.randint(1, 12):02d}/{rng.randint(27, 30)}", True, cards[cid],
                       None, 0))
    await _copy(conn, "card_tokens",
                ["id", "client_id", "provider", "token", "mask", "expires", "active",
                 "created_at", "used_at", "fails"], tokens)


# ─────────────────────── приглашения, акции, отзывы ───────────────────────

async def _referrals(conn: asyncpg.Connection, c: _Ctx) -> None:
    """Воронка «приведи друга» с запуска программы: переход, регистрация,
    аренда и бонус агенту с первого платежа от порога (как ref_paid); плюс
    переходы без регистрации и свежие регистрации без аренды.

    Ядро помечает «пришёл от друга» каждого пятого клиента за полгода.
    Ссылки же появились позже: кто пришёл до запуска, остаётся с каналом
    «по рекомендации» (так он ответил в анкете), но без агента - в панели
    invited_by ставит только переход по ссылке (service.ref_signed).
    """
    w, rng = c.w, c.rng
    bonus, floor = D(REF_SETTINGS["ref_bonus"]), D(REF_SETTINGS["ref_min_payment"])
    friends = sorted((x for x in w.clients if x.invited_by is not None),
                     key=lambda x: (x.invited_at or x.created_at, x.id))
    launch = w.history_start + timedelta(days=30)
    if len(friends) > REF_FRIENDS:
        pivot = friends[-REF_FRIENDS].invited_at or friends[-REF_FRIENDS].created_at
        launch = max(at(_msk(pivot).date() - timedelta(days=rng.randint(1, 3)), 12.0),
                     w.history_start + DAY)
    c.ref_launch = launch
    early = [x for x in friends if (x.invited_at or x.created_at) < launch]
    for x in early:
        x.invited_by = x.invited_at = None
    if early:
        await conn.execute("update crm.clients set invited_by = null, invited_at = null "
                           "where id = any($1::bigint[])", sorted(x.id for x in early))
    friends = [x for x in friends if x.invited_by is not None]

    # Бонус пишется автором того платежа, что его принёс (ref_paid, by=):
    # эквайринг, оператор на выдаче или «Зачислить» выписки.
    authors = {r["id"]: r["created_by"] for r in await conn.fetch(
        "select id, created_by from crm.ledger where kind = 'payment' "
        "and client_id = any($1::bigint[])", [x.id for x in friends])}
    rows, ledger, bonuses = [], [], []
    for friend in friends:
        agent = w.client(friend.invited_by)
        rid = c.ids.take("referrals")
        status, rented, paid, lid, amount = "signed", None, None, None, D(0)
        rentals = c.by_client.get(friend.id, [])
        if rentals:
            status, rented = "rented", rentals[0].created_at
            first = next((p for p in c.pays.get(friend.id, []) if p.amount >= floor), None)
            if first is not None and c.may_bonus(agent.id):
                status, paid, amount = "paid", first.created_at, bonus
                lid = c.bonus(ledger, bonuses, client_id=agent.id, kind="referral",
                              amount=bonus, t=first.created_at + timedelta(seconds=1),
                              note=f"Бонус за друга: {friend.full_name}",
                              by=authors.get(first.ledger_id) or "referral",
                              ref_id=rid)
        rows.append((rid, agent.id, c.tg[friend.id], friend.id, status, amount, lid, None,
                     friend.invited_at or friend.created_at, friend.created_at, rented,
                     paid))
    # Перешли по ссылке и не дошли до регистрации.
    agents = sorted({x.invited_by for x in friends if x.invited_by is not None})
    agents = [a for a in agents if w.client(a).status == "active"]
    span = max((c.now - launch).total_seconds(), 3600.0)
    for n in range(rng.randint(5, 7) if agents else 0):
        agent = agents[rng.randrange(len(agents))]
        clicked = launch + timedelta(seconds=rng.uniform(0, span))
        rows.append((c.ids.take("referrals"), agent, TG_BASE + CLICK_SHIFT + n, None,
                     "click", D(0), None, None, clicked, None, None, None))
    # Порядок внешних ключей: журнал ← приглашение ← повод баллов.
    await _copy(conn, "ledger", _LEDGER, ledger)
    rows.sort(key=lambda r: (r[8], r[0]))
    await _copy(conn, "referrals",
                ["id", "agent_id", "tg_id", "client_id", "status", "bonus", "ledger_id",
                 "note", "created_at", "signed_at", "rented_at", "paid_at"], rows)
    await _copy(conn, "bonuses", _BONUS, bonuses)


async def _promos(conn: asyncpg.Connection, c: _Ctx) -> None:
    """Две действующие акции и их применения: «С возвращением» ко всем, кто
    вернулся после месяца без аренды, и промокод из листовки у части новых.
    Скидка ложится как в charge_period: bonus на тот же период тем же
    моментом, что начисление, и повод в crm.bonuses с акцией и арендой."""
    w, rng, today = c.w, c.rng, c.today
    comeback = {"id": c.ids.take("promos"), "kind": "comeback", "title": "С возвращением",
                "percent": 15, "code": None, "params": {"after_days": 30},
                "starts_on": today - timedelta(days=60), "ends_on": None, "max_uses": None,
                "note": "Месяц без аренды - скидка на первую неделю (демо)"}
    flyer = {"id": c.ids.take("promos"), "kind": "promocode",
             "title": "Промокод из листовки", "percent": 10, "code": "ВЕЛО10", "params": {},
             "starts_on": today - timedelta(days=40), "ends_on": today + timedelta(days=50),
             "max_uses": 100, "note": "Листовки у ресторанов на Баумана (демо)"}
    await conn.executemany(
        """
        insert into crm.promos (id, kind, title, percent, code, params, starts_on, ends_on,
                                max_uses, once_per_client, active, note, created_by,
                                created_at, updated_at)
        values ($1, $2, $3, $4, $5, $6::text::jsonb, $7, $8, $9, true, true, $10,
                'staff:demo', $11, $11)
        """,
        [(p["id"], p["kind"], p["title"], p["percent"], p["code"], json.dumps(p["params"]),
          p["starts_on"], p["ends_on"], p["max_uses"], p["note"],
          at(p["starts_on"] - DAY, 18.0)) for p in (comeback, flyer)])
    ledger, bonuses, coded = [], [], []

    def grant(promo: dict[str, Any], r: Rental) -> None:
        charge = (c.charges.get(r.id) or [None])[0]
        if charge is None or not c.may_bonus(r.client_id):
            return
        amount = logic.promo_discount(promo, r.base_price)
        label = logic.period_label(charge["period_from"], charge["period_to"])
        c.bonus(ledger, bonuses, client_id=r.client_id, kind="promo", amount=amount,
                t=charge["created_at"], note=f"Акция «{promo['title']}»: {label}",
                by="promo", rental_id=r.id, pf=charge["period_from"],
                pt=charge["period_to"], promo_id=promo["id"])
        c.notice(charge["created_at"] + timedelta(seconds=30), "promo_applied", r.client_id)

    # «С возвращением»: перерыв - от закрытия прошлой аренды до начала этой
    # (promo_fits: last_closed_on и period_from), одна на клиента.
    for client_id in sorted(c.by_client):
        history = c.by_client[client_id]
        for k, r in enumerate(history[1:], start=1):
            closed = [x.closed_on for x in history[:k] if x.closed_on is not None]
            if (r.started_on >= comeback["starts_on"] and closed
                    and (r.started_on - max(closed)).days >= 30):
                grant(comeback, r)
                break
    fresh = [hist[0] for cid, hist in sorted(c.by_client.items())
             if hist[0].started_on >= flyer["starts_on"]
             and w.client(cid).channel in ("site", "channel", "other", "avito")
             and w.client(cid).source != "import"]
    for r in rng.sample(fresh, min(12, len(fresh))):
        before = len(bonuses)
        grant(flyer, r)
        if len(bonuses) > before:
            coded.append(r.id)
    await _money(conn, ledger, bonuses)
    await conn.execute("update crm.rentals set promo_code = $2 where id = any($1::bigint[])",
                       sorted(coded), flyer["code"])


async def _reviews(conn: asyncpg.Connection, c: _Ctx) -> None:
    """Просьба об отзыве и приглашение на ТО - по правилам дневного прохода
    (billing.ask_for_review, invite_to_service), и баллы тем, кто прислал
    скриншот отзыва: начисляет оператор, один раз на клиента.

    Отзыв: идущая аренда от 21 дня, клиент в Telegram, баланс не в минусе
    на тот час; один раз на аренду (review_asked_at). ТО: аренда от 30
    дней, велосипед за 30 дней в ремонте не был, звать не чаще раза в 30
    дней (service_invited_at). Без Telegram проход клиента пропускает.
    """
    w, rng, now = c.w, c.rng, c.now
    bal = await _Balances.load(conn)
    state = logic.notice_settings([])
    ask_after = logic.notice_param(state.get("review_ask"), "after_days", 21)
    invite_after = logic.notice_param(state.get("maintenance_invite"), "after_days", 30)
    repairs: dict[int, list[datetime]] = defaultdict(list)
    for r in await conn.fetch("select bike_id, created_at from crm.bike_log "
                              "where kind = 'repair' order by created_at"):
        repairs[r["bike_id"]].append(r["created_at"])
    first_day = _msk(w.history_start).date()
    asked, invited, ledger, bonuses = [], [], [], []
    places = ("Отзыв на Яндекс Картах, скриншот в чате",
              "Отзыв в 2ГИС, скриншот проверен", "Отзыв на Авито, скриншот проверен")
    for r in sorted(w.rentals, key=lambda r: r.id):
        if r.client_id not in c.tg:
            continue
        end = min(r.closed_at or now, now)
        day = max(r.started_on + timedelta(days=ask_after), first_day)
        while at(day, 10.0) < end:
            t = at(day, 10.0) + timedelta(seconds=rng.uniform(5, 240))
            if _alive(r, t) and t <= now and bal.at(r.client_id, t) >= 0:
                asked.append((r.id, t))
                c.notice(t, "review_ask", r.client_id)
                client = w.client(r.client_id)
                if (client.id not in c.reviewed and client.status == "active"
                        and rng.random() < 0.15 and c.may_bonus(client.id)):
                    granted = t + timedelta(days=rng.uniform(1, 3))
                    if granted < now:
                        c.bonus(ledger, bonuses, client_id=client.id, kind="review",
                                amount=D(SETTINGS["review_bonus"]), t=granted,
                                note=rng.choice(places), by=c.operator(r.point))
                        c.reviewed[client.id] = granted
                break
            day += DAY
        day = max(r.started_on + timedelta(days=invite_after), first_day)
        last: datetime | None = None
        while at(day, 10.0) < end:
            t = at(day, 10.0) + timedelta(seconds=rng.uniform(5, 240))
            recent = last is not None and (day - _msk(last).date()).days < invite_after
            fixed = any(t - timedelta(days=invite_after) <= x <= t
                        for x in repairs.get(r.bike_id, ()))
            if not recent and not fixed and _alive(r, t) and t <= now:
                last = t
                c.notice(t + timedelta(seconds=1), "maintenance_invite", r.client_id)
            day += DAY
        if last is not None:
            invited.append((r.id, last))
    await _money(conn, ledger, bonuses)
    for column, marks in (("review_asked_at", asked), ("service_invited_at", invited)):
        if marks:
            await conn.execute(
                f"update crm.rentals r set {column} = u.t "
                "from unnest($1::bigint[], $2::timestamptz[]) as u(id, t) where r.id = u.id",
                [m[0] for m in marks], [m[1] for m in marks])


# ─────────────────────────── заявки ───────────────────────────

async def _bookings(conn: asyncpg.Connection, c: _Ctx) -> None:
    """Заявки из кабинета: четыре открытые (две от свежих регистраций, две
    от вернувшихся), закрытые выдачей и снятые с причиной. Велосипед под
    заявку не бронируется - так и в панели."""
    w, rng, now, today = c.w, c.rng, c.now, c.today
    loc = {name: p.id for name, p in w.points.items()}
    rows: list[tuple] = []

    def tariff(model: str, days: int = 7) -> int:
        return w.tariffs[("bike", model, days)]["id"]

    fresh = [x for x in w.clients if x.source == "bot" and not c.by_client.get(x.id)]
    fresh.sort(key=lambda x: x.id)
    models = sorted(core.BATTERY_OF)
    plan = ((core.P3, 0, core.KUGOO_P), (core.P1, 1, core.MONSTER))
    for client, (point, days, model) in zip(fresh[:2], plan, strict=False):
        created = client.created_at + timedelta(minutes=rng.uniform(20, 90))
        rows.append((client.id, model, tariff(model), loc[point], today + timedelta(days=days),
                     "Работаю в Самокате, нужен на завтра с утра" if days else None,
                     "new", None, None, None, min(created, now)))
    back = [x for x in w.clients if x.status == "active" and c.active_of(x.id) is None
            and c.by_client.get(x.id) and c.balance[x.id] >= 0
            and c.by_client[x.id][-1].closed_on is not None
            and (today - c.by_client[x.id][-1].closed_on).days >= 5]
    back.sort(key=lambda x: x.id)
    for i, client in enumerate(rng.sample(back, min(2, len(back)))):
        last = c.by_client[client.id][-1]
        model = w.bike(last.bike_id).model
        created = max(now - timedelta(hours=rng.uniform(2, 20)),
                      last.closed_at + timedelta(hours=1))
        rows.append((client.id, model, tariff(model, 14 if i else 7), loc[last.point],
                     today + timedelta(days=1 - i), "Как в прошлый раз" if i else None,
                     "new", None, None, None, min(created, now)))
    c.open_bookings = {r[0] for r in rows}
    # Закрытые выдачей: аренда началась в день заявки, до неё аренды не было.
    issued = [r for r in w.rentals if r.started_on >= today - timedelta(days=30)
              and r.created_at > w.history_start + DAY and r.search_at is None
              and c.active_at(r.client_id, r.created_at - timedelta(hours=30)) is None
              and r.client_id not in c.open_bookings]
    issued.sort(key=lambda r: r.id)
    for r in rng.sample(issued, min(6, len(issued))):
        # Заявку подают из кабинета - то есть уже с карточкой клиента.
        created = max(r.created_at - timedelta(hours=rng.uniform(4, 28)),
                      w.client(r.client_id).created_at + timedelta(minutes=10))
        if created >= r.created_at - timedelta(minutes=5):
            continue
        model = w.bike(r.bike_ids[0]).model
        rows.append((r.client_id, model, r.tariff_id, loc[r.point], r.started_on, None,
                     "done", r.id, c.operator(r.point), r.created_at, created))
    idle = [x for x in back if x.id not in c.open_bookings]
    for client, why in zip(rng.sample(idle, min(2, len(idle))),
                           ("Передумал: нашёл работу пешим курьером",
                            "Нет нужной модели на точке, ждать не готов"), strict=False):
        created = now - timedelta(days=rng.uniform(3, 20))
        last = c.by_client[client.id][-1]
        if last.closed_at is not None and created < last.closed_at:
            created = last.closed_at + timedelta(days=1)
        if created >= now:
            continue
        model = w.bike(last.bike_id).model if last.bike_id else models[0]
        handled = min(created + timedelta(hours=rng.uniform(1, 6)), now)
        rows.append((client.id, model, tariff(model), loc[last.point],
                     created.date() + DAY, why, "cancelled", None, c.operator(last.point),
                     handled, created))
        c.notice(handled, "booking_cancelled", client.id)
    rows.sort(key=lambda r: r[10])
    for r in rows:
        c.notice(r[10], "booking_new", r[0])
    await _copy(conn, "bookings",
                ["id", "client_id", "model", "tariff_id", "location_id", "wanted_on", "note",
                 "status", "rental_id", "handled_by", "handled_at", "created_at",
                 "updated_at"],
                [(c.ids.take("bookings"), *r[:10], r[10], r[9] or r[10]) for r in rows])


# ─────────────────────────── ПЭП ───────────────────────────

_AGENTS = (
    "Mozilla/5.0 (Linux; Android 13; SM-A325F) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 12; Redmi Note 11) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0 Mobile Safari/537.36",
)


async def _signing(conn: asyncpg.Connection, c: _Ctx) -> None:
    """Заявки на подпись: пакет (соглашение об ЭП, договор, согласие, акт) и
    журнал шагов. Подписанные - на выдачах последних двух месяцев; одна
    ждёт кода (последняя выдача: код истёк, ссылка жива), одна истекла,
    одна отменена. Текст соглашения - настоящий
    esign.build_agreement с вымышленными реквизитами демо. IP - из
    документационного диапазона 203.0.113.0/24: чужих адресов в демо нет."""
    w, rng, now = c.w, c.rng, c.now
    pool = [r for r in w.rentals if r.created_at >= now - timedelta(days=60)
            and r.created_at > w.history_start + DAY
            and w.client(r.client_id).status == "active"]
    pool.sort(key=lambda r: r.id)
    # Ждёт кода - самая свежая выдача из всех, а не из выборки: ссылка
    # живёт SIGN_LINK_DAYS от создания и обязана дожить до следующего
    # ночного сброса. Выдача недельной давности истекала бы к вечеру, и
    # демо показывало бы вместо «ждёт кода» просроченную ссылку.
    latest = max(pool, key=lambda r: (r.created_at, r.id))
    picks = rng.sample([r for r in pool if r is not latest], min(10, len(pool) - 1))
    # Пакет уходит на выдаче: заявка - за минуты до аренды, но не раньше
    # карточки клиента. Истекла - ссылка старше недели, отменена - любая.
    kinds = ["signed"] * len(picks)
    old = [i for i, r in enumerate(picks) if r.created_at <= now - timedelta(days=8)]
    if old:
        kinds[old[0]] = "expired"
    rest = [i for i in range(len(picks)) if kinds[i] == "signed"]
    if len(rest) > 5:
        kinds[rest[len(rest) // 2]] = "cancelled"
    picks.append(latest)
    kinds.append("code")
    plan: list[tuple[Rental, str, datetime]] = []
    for r, kind in zip(picks, kinds, strict=True):
        created = max(r.created_at - timedelta(minutes=rng.uniform(6, 15)),
                      w.client(r.client_id).created_at + timedelta(minutes=2))
        plan.append((r, kind, created))
    plan.sort(key=lambda x: x[2])
    number = await _next_no(conn, "sign_requests")
    rows, events = [], []

    def event(rid: int, kind: str, t: datetime, *, ip: str | None = None,
              agent: str | None = None, note: str | None = None) -> None:
        events.append((c.ids.take("sign_events"), rid, kind, min(t, now), ip, agent, note))

    for r, kind, created in plan:
        client = w.client(r.client_id)
        rid = c.ids.take("sign_requests")
        no = logic.sign_no(number)
        number += 1
        by = c.operator(r.point)
        token = rng.randbytes(16).hex()
        contract = client.contract_no or ""
        docs = [{"kind": "contract", "title": f"Договор аренды № {contract}",
                 "sha256": hashlib.sha256(f"demo-contract:{contract}".encode()).hexdigest(),
                 "path": None},
                {"kind": "consent", "title": "Согласие на обработку персональных данных",
                 "sha256": hashlib.sha256(f"demo-consent:{contract}".encode()).hexdigest(),
                 "path": None},
                {"kind": "act_in", "title": f"Акт приёма-передачи по аренде № {r.id}",
                 "sha256": "", "path": None}]
        agreement = esign.build_agreement(
            c.company, {"full_name": client.full_name, "phone": client.phone}, no=no,
            docs=docs, today=_msk(created).date(), code_minutes=logic.SIGN_CODE_MINUTES)
        docs = [{"kind": "esign", "title": "Соглашение об использовании ПЭП",
                 "sha256": esign.sha256_text(agreement), "path": None}, *docs]
        ip = f"203.0.113.{rng.randint(2, 250)}"
        agent = rng.choice(_AGENTS)
        opened = created + timedelta(minutes=rng.uniform(0.5, 3))
        code_at = opened + timedelta(minutes=rng.uniform(0.3, 1.5))
        status, code_hash, signed, attempts, got_code = kind, None, None, 0, None
        event(rid, "created", created, note=by)
        if kind in ("signed", "code"):
            event(rid, "opened", opened, ip=ip, agent=agent)
            event(rid, "code_sent", code_at, ip=ip, agent=agent)
            got_code = code_at
        if kind == "signed":
            if rng.random() < 0.3:
                attempts = 1
                event(rid, "code_wrong", code_at + timedelta(minutes=1), ip=ip, agent=agent,
                      note="попытка 1")
            signed = code_at + timedelta(minutes=rng.uniform(1.2, 3))
            event(rid, "signed", signed, ip=ip, agent=agent,
                  note=f"хэш пакета {logic.sign_docs_digest(docs)}")
        elif kind == "code":
            code_hash = logic.hash_sign_code(token, f"{rng.randrange(1_000_000):06d}")
        elif kind == "cancelled":
            event(rid, "cancelled", created + timedelta(minutes=rng.uniform(20, 90)),
                  note=by)
        elif kind == "expired":
            status = "new"
        rows.append((rid, no, client.id, r.id, token, json.dumps(docs, ensure_ascii=False),
                     agreement, code_hash, got_code, attempts, status,
                     created + timedelta(days=logic.SIGN_LINK_DAYS), signed,
                     ip if signed else None, agent if signed else None, by, created))
    await conn.executemany(
        """
        insert into crm.sign_requests (id, no, client_id, rental_id, token, docs, agreement,
                                       code_hash, code_at, attempts, status, expires_at,
                                       signed_at, signed_ip, signed_agent, created_by,
                                       created_at)
        values ($1, $2, $3, $4, $5, $6::text::jsonb, $7, $8, $9, $10, $11, $12, $13, $14,
                $15, $16, $17)
        """, rows)
    events.sort(key=lambda e: (e[3], e[0]))
    await _copy(conn, "sign_events",
                ["id", "request_id", "kind", "at", "ip", "user_agent", "note"], events)


# ─────────────────────────── рассылки ───────────────────────────

_FLYER_TEMPLATE = (
    "Здравствуйте, {name}!\n\nНазовите на выдаче промокод ВЕЛО10 — скидка 10 % на "
    "первую неделю.\nТочки: Павлюхина, Адоратского и Проспект Победы (демо).")


async def _mailing(conn: asyncpg.Connection, c: _Ctx) -> None:
    """Две завершённые рассылки (вернуть ушедших и должникам) и черновик
    с промокодом. Аудитории - по правилам logic.pick_audience на час
    кампании: должник - минус на балансе в тот час, «вернуть» - 14-90 дней
    без аренды. У черновика очередь собрана сейчас, как её собирает
    create_campaign, и никуда не уходит, пока его не запустят."""
    w, rng, now, today = c.w, c.rng, c.now, c.today
    bal = await _Balances.load(conn)
    templates = {r["code"]: r["id"] for r in await conn.fetch(
        "select id, code from crm.message_templates")}
    tid = c.ids.take("message_templates")
    await conn.execute(
        """
        insert into crm.message_templates (id, code, title, body, body_max, note,
                                           created_at, updated_at)
        values ($1, 'promo_velo10', 'Промокод ВЕЛО10', $2, $2, 'Демо: к акции из листовки',
                $3, $3)
        """, tid, _FLYER_TEMPLATE, now - timedelta(days=39))
    number = await _next_no(conn, "campaigns")
    campaigns, sends = [], []
    # Чёрный список ставит признание потери (ядро): до того клиент был
    # обычным адресатом. Отправка после - «пропущено», как у mailing.
    listed = {r.client_id: r.closed_at for r in w.rentals if r.lost and r.closed_at
              and w.client(r.client_id).status != "active"}

    def active_at(client: Client, moment: datetime) -> bool:
        if client.status == "active":
            return True
        return client.id in listed and listed[client.id] > moment

    def people_at(code: str, moment: datetime) -> list[int]:
        day = _msk(moment).date()
        out = []
        for client in sorted(w.clients, key=lambda x: (x.full_name, x.id)):
            if not active_at(client, moment) or not c.reachable(client.id):
                continue
            if client.created_at > moment:
                continue
            if code == "debtors":
                if bal.at(client.id, moment) < 0:
                    out.append(client.id)
                continue
            if c.active_at(client.id, moment) is not None:
                continue
            seen = [r.closed_on or r.started_on for r in c.by_client.get(client.id, [])
                    if r.created_at <= moment]
            if seen and (logic.COMEBACK_FROM_DAYS <= (day - max(seen)).days
                         <= logic.COMEBACK_TO_DAYS):
                out.append(client.id)
        return out

    # Должникам - в день, когда одного из них признали потерявшим велосипед:
    # очередь собрана утром, а к отправке он уже в чёрном списке.
    debt_at = at(today - timedelta(days=12), 11.0 + rng.uniform(0, 0.5))
    debt_lost: datetime | None = None
    for r in sorted((r for r in w.rentals if r.lost and r.closed_at),
                    key=lambda r: r.closed_at, reverse=True):
        created = r.closed_at - timedelta(hours=rng.uniform(1, 2))
        if (now - timedelta(days=25) <= created <= now - timedelta(days=3)
                and c.reachable(r.client_id) and bal.at(r.client_id, created) < 0):
            debt_at, debt_lost = created, r.closed_at
            break
    for title, code, created, template in (
            ("Возвращайтесь в сезон", "comeback",
             at(today - timedelta(days=38), 11.0 + rng.uniform(0, 0.5)), "comeback"),
            ("Напоминание о долге", "debtors", debt_at, "debt")):
        cid = c.ids.take("campaigns")
        people = people_at(code, created)
        started = created + timedelta(minutes=rng.uniform(4, 15))
        if code == "debtors" and debt_lost is not None:
            started = max(started, debt_lost + timedelta(minutes=rng.uniform(5, 20)))
        finished = started + timedelta(seconds=len(people) * 0.4 + 60)
        campaigns.append((cid, logic.campaign_no(number), title, templates[template], code,
                          "done", "staff:demo", created, started, finished, None))
        number += 1
        for k, client_id in enumerate(people):
            sent = started + timedelta(seconds=0.4 * k + 1)
            status, error = "sent", None
            channel = "tg" if client_id in c.tg else "max"
            if not active_at(w.client(client_id), sent):
                status, error = "skipped", "клиент заблокирован"
            elif rng.random() < 0.06:
                status = "failed"
                error = ("Telegram server says - Forbidden: bot was blocked by the user"
                         if channel == "tg" else "MAX: пользователь запретил сообщения")
            sends.append((c.ids.take("campaign_sends"), cid, client_id, channel, status,
                          error, sent))
    # Черновик: очередь - по сегодняшней аудитории «уехали и не вернулись».
    crm = CrmDB(conn)
    people = logic.pick_audience("comeback", await crm.clients_for_mailing(),
                                 await crm.active_rentals(), today=today)
    cid = c.ids.take("campaigns")
    campaigns.append((cid, logic.campaign_no(number), "ВЕЛО10: вернуть тех, кто уехал",
                      tid, "comeback", "draft", "staff:demo",
                      now - timedelta(hours=rng.uniform(1, 3)), None, None,
                      "Проверить список и запустить в пятницу (демо)"))
    for p in people:
        sends.append((c.ids.take("campaign_sends"), cid, p["id"], p["channel"], "queued",
                      None, None))
    await _copy(conn, "campaigns",
                ["id", "no", "title", "template_id", "audience", "status", "created_by",
                 "created_at", "started_at", "finished_at", "note"], campaigns)
    await _copy(conn, "campaign_sends",
                ["id", "campaign_id", "client_id", "channel", "status", "error", "sent_at"],
                sends)


# ─────────────────────────── входящие ───────────────────────────

# (канал, источник, кто, статус, дней назад, час, тема, сообщения).
# Кто: client_tg / client_max / client_wa - карточка найдётся сама (по
# Telegram, MAX или телефону), и аренда у неё уже шла; stranger - клиентом
# не стал; client_paid - чей перевод зачислили из выписки (время - от
# зачисления), reviewed - клиент с баллами за отзыв (время - от баллов).
# Дней 0 - час от «сейчас» (ночью - от вчерашнего вечера). Сообщение:
# (минут от начала, направление, вид, текст, автор ответа). Ответ без
# текста - «ответил вне панели».
_THREADS: tuple[tuple, ...] = (
    ("avito", "avito_api", "stranger", "done", 16, 11.2,
     "Электровелосипед Monster в аренду курьерам (демо)", (
         (0, "in", "text", "Здравствуйте! Сколько стоит неделя?", None),
         (6, "out", "text", "Здравствуйте! Monster — 3 000 ₽ в неделю, Kugoo — 3 500 ₽. "
                            "Два аккумулятора в комплекте, хватает на смену.", "avito-app"),
         (14, "in", "text", "А залог нужен?", None),
         (19, "out", "text", "Залога нет: паспорт и договор, оформление 15 минут. "
                             "Павлюхина, 97А — с 10 до 19.", "staff:operator"),
         (23, "in", "text", "Спасибо, завтра подъеду", None))),
    ("avito", "avito_api", "stranger", "new", 0, -0.6,
     "Kugoo V3 Pro в аренду, Проспект Победы (демо)", (
         (0, "in", "text", "Добрый день, на Победы есть свободные Kugoo? Хочу сегодня "
                           "забрать", None),)),
    ("avito", "avito_api", "stranger", "work", 1, 12.5,
     "Аренда электровелосипеда на месяц (демо)", (
         (0, "in", "text", "Здравствуйте, работаю в Яндекс Еде, можно взять на месяц?", None),
         (9, "out", "text", "Здравствуйте! Можно: месяц от 11 000 ₽ в зависимости от "
                            "модели, ТО и ремонт за наш счёт.", "staff:operator"),
         (31, "in", "text", "А если через неделю верну, остаток вернёте?", None))),
    ("avito", "avito_api", "stranger", "spam", 9, 15.1, "Электровелосипеды (демо)", (
         (0, "in", "text", "Продвинем ваше объявление в ТОП Авито! Первые 3 дня "
                           "бесплатно", None),)),
    ("avito", "hook", "stranger", "done", 23, 18.4, "Аренда на две недели (демо)", (
         (0, "in", "text", "Интересует аренда на две недели, что нужно из документов?",
          None),
         (40, "out", "text", None, "staff:operator2"))),
    ("tg", "bot", "client_tg", "done", 5, 13.3, None, (
         (0, "in", "text", "Здравствуйте, у меня задний тормоз скрипит, можно заехать "
                           "сегодня?", None),
         (7, "out", "text", "Здравствуйте! Приезжайте до 19:00, механик посмотрит за "
                            "15 минут.", "staff:operator"),
         (12, "in", "text", "Спасибо, буду в 17", None))),
    ("tg", "bot", "client_paid", "done", 2, 19.6, None, (
         (0, "in", "text", "Я оплатил переводом, проверьте пожалуйста", None),
         (26, "out", "text", "Платёж зачислен, спасибо!", "staff:demo"))),
    ("tg", "bot", "stranger", "new", 0, -2.1, None, (
         (0, "in", "text", "Здравствуйте! Как оформить аренду? Я курьер Самоката, "
                           "документы есть", None),)),
    ("tg", "bot", "client_tg", "work", 1, 10.4, None, (
         (0, "event", "other", "Нажал «Связаться с человеком»", None),
         (1, "in", "text", "Хочу продлить сразу на месяц, есть скидка?", None),
         (15, "out", "text", "Месяц выходит дешевле недель: переоформим при следующей "
                             "оплате.", "staff:operator2"),
         (48, "in", "text", "Ок, тогда в пятницу приеду", None))),
    ("max", "max_bot", "client_max", "work", 3, 16.2, None, (
         (0, "in", "text", "Добрый день! Можно поменять велосипед? Этот быстро "
                           "разряжается", None),
         (11, "out", "text", "Добрый день! Привозите на точку — поменяем аккумуляторы "
                             "или велосипед целиком.", "staff:operator2"))),
    ("max", "max_bot", "stranger", "done", 12, 20.3, None, (
         (0, "in", "text", "Здравствуйте, у вас есть аренда с выкупом?", None),
         (35, "out", "text", "Выкупа нет — только аренда. Зато ремонт и ТО за наш счёт.",
          "staff:demo"),
         (41, "in", "text", "Понял, спасибо", None))),
    ("wa", "hook", "client_wa", "done", 4, 21.1, None, (
         (0, "in", "text", "Добрый вечер, завтра смогу вернуть велосипед после 12?", None),
         (52, "out", "text", None, "staff:operator"))),
    ("wa", "hook", "stranger", "new", 0, -0.9, None, (
         (0, "in", "call", None, None),
         (2, "in", "text", "Перезвоните пожалуйста, хотим взять два велосипеда", None))),
    ("wa", "hook", "stranger", "work", 2, 14.8, None, (
         (0, "in", "text", "Сколько стоит ремонт самоката Kugoo? Не заряжается", None),
         (18, "out", "text", None, "staff:mechanic"),
         (95, "in", "text", "Хорошо, привезу в субботу", None))),
    ("tg", "bot", "reviewed", "done", 7, 12.9, None, (
         (0, "in", "text", "Спасибо, всё отлично! Оставил отзыв на картах, скриншот "
                           "прикрепил", None),
         (1, "in", "image", None, None),
         (22, "out", "text", "Спасибо! Начислили баллы за отзыв — они уйдут в оплату "
                             "следующей недели.", "staff:operator"))),
    ("max", "max_bot", "client_max", "new", 0, -0.35, None, (
         (0, "in", "text", "Здравствуйте, велосипед не включается после зарядки", None),)),
)


async def _inbox(conn: asyncpg.Connection, c: _Ctx) -> None:
    """Обращения из Авито, Telegram, MAX и WhatsApp в разных состояниях.

    Текст зашифрован ключом демо (inbox_key_for от секрета панели) тем же
    Vault, что у панели. Поля - как их пишут источники: бот кладёт имя и
    телефон из анкеты и не объявляет обращение отдельно (карточка вопроса
    уже в чате), хук и опрос Авито объявляют каждое начало ожидания -
    отсюда inbox_new в истории. Без очереди: ответы уже ушли.
    """
    w, rng, now = c.w, c.rng, c.now
    vault = service.inbox_vault(inbox_key_text(c.secret))
    used: set[int] = set()
    anchor = now if now.hour >= 10 else at(c.today - DAY, 22.5)

    def client_for(kind: str, start: datetime) -> tuple[int | None, datetime]:
        """Клиент под сюжет и момент начала: переписка не старше карточки
        и аренды, «оплатил» - перед зачислением, «отзыв» - перед баллами."""
        if kind == "reviewed":
            pool = [(g - timedelta(minutes=23), x) for x, g in c.reviewed.items()
                    if x in c.tg and now - timedelta(days=60) <= g <= now]
        elif kind == "client_paid":
            pool = [(b[12] - timedelta(minutes=26), b[10]) for b in c.bank
                    if b[9] == "matched" and b[10] in c.tg
                    and now - timedelta(days=6) <= b[12] <= now - DAY]
        else:
            ids = c.tg if kind == "client_tg" else c.mx if kind == "client_max" else None
            pool = [(start, r.client_id) for r in w.active_rentals() if r.search_at is None
                    and r.created_at < start - timedelta(hours=1)
                    and (ids is None or r.client_id in ids)]
        pool = sorted(p for p in set(pool) if p[1] not in used)
        if not pool:
            return None, start
        moment, pick = pool[rng.randrange(len(pool))]
        used.add(pick)
        return pick, moment

    threads, messages = [], []
    for n, (channel, origin, who, status, days, hour, subject, script) in enumerate(_THREADS):
        start = (anchor + timedelta(hours=hour) if days == 0
                 else at(c.today - timedelta(days=days), hour))
        client_id = None
        if who != "stranger":
            client_id, start = client_for(who, start)
        if who != "stranger" and client_id is None:
            continue
        client = w.client(client_id) if client_id is not None else None
        name = phone = None
        if channel in ("tg", "max"):
            # Бот пишет имя и телефон из анкеты; незарегистрированный - имя
            # из профиля мессенджера, без телефона.
            base = TG_BASE if channel == "tg" else MAX_BASE
            ids = c.tg if channel == "tg" else c.mx
            ext = str(ids[client_id] if client else base + STRANGER_SHIFT + n)
            if client is not None:
                name, phone = client.full_name, client.phone
            else:
                name = _short(w.people.name())
        elif channel == "wa":
            phone = client.phone if client else w.people.phone()
            ext = phone
            name = (client.full_name if client else w.people.name()).split()[1]
        else:
            ext = f"u2i-demo{n:04d}" if origin == "avito_api" else f"wz-demo{n:04d}"
            name = w.people.name().split()[1]
        announce = origin in ("hook", "avito_api")
        tid = c.ids.take("inbox_threads")
        last_in = last_out = waiting = replier = replied = None
        announced = start
        end = start
        for k, (minute, direction, kind, text, author) in enumerate(script):
            t = start + timedelta(minutes=minute, seconds=rng.uniform(0, 40))
            if t > now:
                t = now - timedelta(seconds=len(script) - k)
            body = service.inbox_seal(vault, text) if text else None
            if direction == "out":
                own = author == "avito-app"
                messages.append((c.ids.take("inbox_messages"), tid, "out", kind,
                                 f"av{tid}-{k}" if own else None, body, author, "sent",
                                 None, t, t + timedelta(seconds=12) if body and not own
                                 else t, t + timedelta(seconds=4) if body and not own
                                 else None))
                last_out, waiting = t, None
                if replier is None and author.startswith("staff:"):
                    replier, replied = author, t
            else:
                messages.append((c.ids.take("inbox_messages"), tid, direction, kind,
                                 f"{tid}{k:02d}" if direction == "in" else None, body,
                                 None, None, None, t, None, None))
                last_in = t
                if direction == "in" and waiting is None:
                    waiting = t
                    if announce:
                        # Хук и опрос Авито объявляют каждое начало ожидания.
                        announced = min(t + timedelta(seconds=rng.uniform(20, 70)), now)
                        c.notice(announced, "inbox_new")
            end = t
        handled_by = handled_at = None
        if status in ("done", "spam"):
            waiting = None
            handled_by = replier or "staff:operator"
            handled_at = min(end + timedelta(minutes=rng.uniform(2, 30)), now)
        elif status == "work":
            handled_by, handled_at = replier, replied
        threads.append((tid, channel, origin, ext, name, None, phone, subject, client_id,
                        status, waiting, last_in, last_out, announced, handled_by,
                        handled_at, start, max(end, handled_at or end), False))
    await _copy(conn, "inbox_threads",
                ["id", "channel", "origin", "ext_id", "name", "username", "phone", "subject",
                 "client_id", "status", "waiting_since", "last_in_at", "last_out_at",
                 "announced_at", "handled_by", "handled_at", "created_at", "updated_at",
                 "client_manual"], threads)
    messages.sort(key=lambda m: (m[9], m[0]))
    first = messages[0][0] if messages else 0
    messages = [(first + i,) + m[1:] for i, m in enumerate(messages)]
    await _copy(conn, "inbox_messages",
                ["id", "thread_id", "direction", "kind", "ext_id", "body_enc", "author",
                 "status", "error", "created_at", "sent_at", "claimed_at"], messages)


# ─────────────────────── рабочая группа точек ───────────────────────

def _fix_text(*, fio: str, frame: str, motor: str, batteries: int, term: str,
              phone: str, payment: str, gps: bool, given_by: str, referral: str) -> str:
    """Форма фиксации выдачи - как bot_logic.fixation_form, заполненная на
    точке. Адреса - прочерками: их парсер всё равно не берёт."""
    kit = {"АКБ": str(batteries), "ЗУ": "1", "Зеркала": "2", "Курьерская сумка": "0"}
    lines = "\n".join(f"  - {k}: {v}" for k, v in kit.items())
    return (f"1. ФИО: {fio}\n2. Вин номер рамы: {frame}\n"
            f"3. Вин номер мотор колеса: {motor}\n4. Комплектация: \n{lines}\n"
            f"5. Сроки аренды: {term}\n6. Номер телефона (основной): 8{phone[2:]}\n"
            f"7. Номер телефона 2: —\n8. Номер телефона 3: —\n9. Ник в Telegram: —\n"
            f"10. Сумма и способ оплаты: {payment}\n"
            f"11. Адрес прописки с квартирой в Казани: —\n"
            f"12. Адрес проживания с квартирой в Казани: —\n"
            f"13. Подключен GPS-Трекер: {'да' if gps else 'нет'}\n14. Адрес сдачи: —\n"
            f"15. Кто выдал: {given_by}\n16. Подписка на тг: да\n"
            f"17. Реф.программа: {referral}")


def _return_text(*, closed: str, reason: str, place: str, accepted: str, review: str,
                 feedback: str, fio: str, frame: str, motor: str, wash: str) -> str:
    """Отчёт о сдаче - дословно в формате bot_logic.closure_report."""
    return (f"Когда сдал: {closed}\nСколько оплатил долгов: 0\n"
            f"Какие повреждения есть: нет\nСколько оплатил за ремонт: 0\n"
            f"Оплатил мойку велосипеда: {wash}\nПричина сдачи: {reason}\n"
            f"Адрес сдачи: {place}\nКто принял велик: {accepted}\n"
            f"Оставил отзыв: {review}\n"
            f"Какие рекомендации по улучшению сервиса/вело дали: {feedback}\n"
            f"1. ФИО: {fio}\n2. Вин номер рамы: {frame}\n3. Вин номер мотор колеса: {motor}")


_DAILY_PARTS = ("колодки ×2", "камера 18×3.0", "тормозной трос", "фара", "грипсы",
                "подножка", "покрышка 18×3.0", "ручка газа")


async def _ops_group(conn: asyncpg.Connection, c: _Ctx) -> None:
    """Пять дней группы точек: формы выдачи и сдачи, сверенные с базой (👍),
    одна выдача с опечаткой в моторе и одна сдача раньше закрытия аренды
    (👎), замены из карточки аренды, итоги дня сервиса. Данные - разбор
    текста формы настоящими logic.parse_ops_*: в журнале ровно те поля, что
    сохранил бы бот, телефонов и адресов среди них нет."""
    w, rng, now, today = c.w, c.rng, c.now, c.today
    since = at(today - timedelta(days=4))
    staff_name = {s.actor: s.name.split()[1] for s in w.staff.values()}
    reports: list[tuple] = []

    def report(kind: str, t: datetime, data: dict[str, Any] | None, *, ok: bool,
               bike_id: int | None = None, rental: Rental | None = None,
               note: str | None = None, author: str = "") -> None:
        if data is None or t > now:
            return
        data.pop("phones", None)          # как opsgroup.fixation: только для сверки
        reports.append((kind, t, data, ok, bike_id, rental.id if rental else None,
                        rental.client_id if rental else None, note, author))

    fresh = [r for r in w.rentals if r.created_at >= since
             and r.created_at > w.history_start + DAY]
    fresh.sort(key=lambda r: r.created_at)
    wrong_motor = fresh[rng.randrange(len(fresh))].id if fresh else None
    for r in fresh:
        bike = w.bike(r.bike_ids[0])
        client = w.client(r.client_id)
        by = staff_name[c.operator(r.point)]
        t = r.created_at + timedelta(minutes=rng.uniform(4, 25))
        term = (f"{r.started_on:%d.%m} - "
                f"{r.started_on + timedelta(days=r.period_days):%d.%m}")
        fields = {"fio": client.full_name, "frame": bike.frame_no, "motor": bike.motor_no,
                  "batteries": 2 + len(r.extras), "term": term, "phone": client.phone,
                  "payment": f"{_thousands(r.price)} {rng.choice(('qr', 'сбп', 'нал'))}",
                  "gps": bike.tracker, "given_by": by,
                  "referral": "да" if client.invited_by else "нет"}
        if r.id == wrong_motor:
            typo = bike.motor_no[:-1] + str((int(bike.motor_no[-1]) + 3) % 10) \
                if bike.motor_no[-1:].isdigit() else bike.motor_no + "7"
            data, _ = logic.parse_ops_fix(_fix_text(**{**fields, "motor": typo}))
            report("fix", t, data, ok=False, bike_id=bike.id,
                   note="мотор в форме не совпадает с карточкой", author=by)
            t += timedelta(minutes=rng.uniform(3, 9))
        data, _ = logic.parse_ops_fix(_fix_text(**fields))
        report("fix", t, data, ok=True, bike_id=bike.id, rental=r, author=by)

    # Замены, проведённые из карточки аренды: сообщение в группе их только
    # подтверждает («замена уже в CRM»), второй раз ничего не меняется.
    swaps = await conn.fetch(
        """
        select rb.rental_id, rb.bike_id, rb.mileage_start, rb.created_at, rb.reason,
               p.bike_id as old_id, p.mileage_end as old_km
          from crm.rental_bikes rb
          join lateral (select bike_id, mileage_end from crm.rental_bikes x
                         where x.rental_id = rb.rental_id and x.id < rb.id
                         order by x.id desc limit 1) p on true
         where rb.created_at >= $1 order by rb.created_at
        """, since)
    for s in swaps:
        r = c.rentals[s["rental_id"]]
        old, new = w.bike(s["old_id"]), w.bike(s["bike_id"])
        text = (f"ЗАМЕНА\nФИО: {w.client(r.client_id).full_name}\nОткуда: {r.point}\n"
                f"Причина: {s['reason'].lower()}\nБыло:\nРама: {old.frame_no}\n"
                f"Мотор: {old.motor_no}\nПробег: {s['old_km'] or 0}\nСтало:\n"
                f"Рама: {new.frame_no}\nМотор: {new.motor_no}\n"
                f"Пробег: {s['mileage_start'] or 0}")
        data, _ = logic.parse_ops_swap(text)
        report("swap", s["created_at"] + timedelta(minutes=rng.uniform(3, 15)), data,
               ok=True, bike_id=new.id, rental=r, note="замена уже в CRM",
               author=staff_name[c.operator(r.point)])

    returned = [r for r in w.rentals if r.status == "closed" and not r.lost
                and r.closed_at is not None and r.closed_at >= since]
    returned.sort(key=lambda r: r.closed_at)
    early = returned[rng.randrange(len(returned))].id if returned else None
    for r in returned:
        bike = w.bike(r.bike_id)
        client = w.client(r.client_id)
        by = staff_name[c.operator(r.point)]
        text = _return_text(
            closed=f"{_msk(r.closed_at):%d.%m.%Y}",
            reason=rng.choice(("закончил сезон", "уезжает домой", "перешёл на авто",
                               "взял свой велосипед")),
            place=r.point, accepted=by, review=rng.choice(("5", "5", "4")),
            feedback=rng.choice(("Всё устроило", "Больше Kugoo на Победы", "нет")),
            fio=client.full_name, frame=bike.frame_no, motor=bike.motor_no,
            wash=rng.choice(("0", "0", "200")))
        data, _ = logic.parse_ops_return(text)
        if r.id == early:
            report("return", r.closed_at - timedelta(minutes=rng.uniform(10, 25)), data,
                   ok=False, bike_id=bike.id, rental=r, note="аренда в CRM ещё идёт",
                   author=by)
            continue
        report("return", r.closed_at + timedelta(minutes=rng.uniform(5, 50)), data,
               ok=True, bike_id=bike.id, rental=r, author=by)

    day = today - timedelta(days=4)
    while at(day, 21.2) <= now:
        for point in w.points.values():
            if point.opened_on > day:
                continue
            in_repair = [i for i in w.service_intervals if i.kind == "repair"
                         and i.point == point.name and i.start < at(day, 20.0)
                         and (i.end is None or i.end > at(day))]
            start = sum(1 for i in in_repair if i.start < at(day))
            fixed = sum(1 for i in in_repair if i.end is not None and i.end <= at(day, 20.0))
            parts = rng.sample(_DAILY_PARTS, rng.randint(1, 3))
            text = (f"{point.name}\n1. В ремонте на начало дня: {start}\n"
                    f"2. Отремонтировано со склада: {fixed}\n"
                    f"3. Отремонтировано у арендаторов: {rng.randint(0, 2)}\n"
                    f"4. В ремонте на конец дня: {len(in_repair) - fixed}\n"
                    f"5. Израсходованные детали:\n"
                    + "".join(f"- {p}\n" for p in parts)
                    + f"6. Помыто велосипедов: {rng.randint(2, 9)}")
            data, _ = logic.parse_ops_daily(text)
            report("daily", at(day, 20.3 + rng.uniform(0, 0.8)), data, ok=True,
                   author=w.mechanic(point.name).name.split()[1])
        day += DAY
    reports.sort(key=lambda x: x[1])
    await conn.executemany(
        """
        insert into crm.ops_reports (id, kind, chat_id, message_id, thread_id, author_tg,
                                     author, bike_id, rental_id, client_id, data, ok, note,
                                     created_at)
        values ($1, $2, $3, $4, $5, null, $6, $7, $8, $9, $10::text::jsonb, $11, $12, $13)
        """,
        [(c.ids.take("ops_reports"), kind, OPS_CHAT, 4100 + i * 3 + rng.randint(0, 2),
          OPS_TOPICS[kind], author, bike_id, rental_id, client_id,
          json.dumps(data, ensure_ascii=False), ok, note, t)
         for i, (kind, t, data, ok, bike_id, rental_id, client_id, note, author)
         in enumerate(reports)])


# ─────────────────────── фильтры и настройки ───────────────────────

async def _saved_views(conn: asyncpg.Connection, c: _Ctx) -> None:
    """Свои фильтры владельца демо: должники, просрочка, ремонт, точки.
    Строка запроса - как её сохраняет «Сохранить» списка (clean_query)."""
    owner = c.w.staff["demo"]
    views = (("/rentals", "Должники", {"status": "active", "view": "debt"}),
             ("/rentals", "Просрочка", {"status": "active", "view": "overdue"}),
             ("/rentals", "Проспект Победы", {"status": "active", "location": core.P3}),
             ("/bikes", "В ремонте", {"status": "repair"}),
             ("/bikes", "Свободные", {"status": "available"}),
             ("/orders", "Ждут согласования", {"status": "approve"}),
             ("/inbox", "Авито", {"tab": "all", "channel": "avito"}))
    await _copy(conn, "saved_views",
                ["id", "staff_id", "section", "name", "query", "created_at"],
                [(c.ids.take("saved_views"), owner.id, section, name, urlencode(query),
                  c.now - timedelta(days=40 - 5 * i))
                 for i, (section, name, query) in enumerate(views)])


async def _settings(conn: asyncpg.Connection, c: _Ctx) -> None:
    """Настройки, от которых зависят разделы демо: порог «молчит» трекеров,
    бонус за отзыв, программа приглашений (с её запуска) и отметка опроса
    Авито - без неё панель показала бы «Авито: опрос не работает»."""
    avito = json.dumps({"ok": True, "at": c.now.isoformat(), "error": "",
                        "every": DEMO_AVITO_EVERY}, ensure_ascii=False)
    values = [(k, v, c.w.history_start) for k, v in SETTINGS.items()]
    values += [(k, v, c.ref_launch or c.w.history_start) for k, v in REF_SETTINGS.items()]
    values.append(("inbox_avito_state", avito, c.now))
    await conn.executemany(
        """
        insert into crm.settings (key, value, updated_by, updated_at)
        values ($1, $2, 'staff:demo', $3)
        on conflict (key) do update set value = excluded.value,
          updated_by = excluded.updated_by, updated_at = excluded.updated_at
        """, values)


# ─────────────────────────── уведомления ───────────────────────────

async def _notices(conn: asyncpg.Connection, c: _Ctx) -> None:
    """История уведомлений за 30 дней. Событийные (зачисление, счёт, акция,
    обращение, заявка, отзыв, ТО) собрали разделы выше; здесь - расписание
    дневного прохода, пересчитанное на каждый его час по журналу денег:
    напоминания по «оплачено до» (billing.remind_once: одно в день на
    аренду, 8/9/14 часов), вечерние сводки по оплатам, розыску и
    расхождениям - только в дни, когда им было что сказать, утренняя
    сводка неразобранной выписки. Отметка последнего напоминания ложится
    на идущие аренды (notified_on), как ставит mark_notified."""
    w, rng, now = c.w, c.rng, c.now
    since = now - timedelta(days=logic.NOTICE_LOG_DAYS)
    bal = await _Balances.load(conn)
    crm = CrmDB(conn)
    hunt = logic.search_settings(await crm.settings())
    intents = {r["id"]: dict(r) for r in await conn.fetch(
        "select id, intent, intent_until, intent_at from crm.rentals "
        "where intent is not null")}
    days = [since.date() + timedelta(days=k) for k in range((c.today - since.date()).days + 1)]
    marks: list[tuple[int, date, str]] = []
    digest_days: set[date] = set()
    search_days: set[date] = set()
    for r in sorted(w.rentals, key=lambda r: r.id):
        if (r.closed_at is not None and r.closed_at < since) or r.created_at > now:
            continue
        last: tuple[date, str] | None = None
        for day in days:
            for wanted, hour in REMIND_HOURS:
                t = at(day, hour)
                if t < since or t > now or not _alive(r, t) or (last and last[0] == day):
                    continue
                until = c.covered(r, t, bal)
                kind = logic.reminder_kind(logic.days_left(until, today=day),
                                           before_days=REMIND_BEFORE_DAYS)
                if kind != wanted:
                    continue
                intent = intents.get(r.id)
                if (kind in (logic.REMIND_SOON, logic.REMIND_DUE) and intent
                        and intent["intent"] == "return" and intent["intent_at"] <= t
                        and intent["intent_until"] == until):
                    continue
                last = (day, kind)
                c.notice(t + timedelta(seconds=rng.uniform(1, 90)), REMIND_CODE[kind],
                         r.client_id)
            evening = at(day, 20.0)
            if since <= evening <= now and _alive(r, evening):
                left = logic.days_left(c.covered(r, evening, bal), today=day)
                if left <= REMIND_BEFORE_DAYS:
                    digest_days.add(day)
                if r.search_at is not None and r.search_at <= evening:
                    if (day - _msk(r.search_at).date()).days >= hunt["theft_after"]:
                        search_days.add(day)
                elif -left >= hunt["search_after"]:
                    search_days.add(day)
        if r.status == "active" and last is not None:
            marks.append((r.id, last[0], last[1]))
    # Расхождения в чат - с тех пор, как появился первый намеренный долг
    # без аренды: до него в демо сходилось всё.
    debts = [c.by_client[x.id][-1].closed_at for x in w.clients
             if c.by_client.get(x.id) and c.active_of(x.id) is None
             and c.balance[x.id] <= -500 and c.by_client[x.id][-1].closed_at]
    issues_from = min(debts) if debts else None
    for day in days:
        evening = at(day, 20.0)
        if not since <= evening <= now:
            continue
        if day in digest_days:
            c.notice(evening + timedelta(seconds=rng.uniform(5, 30)), "daily_digest")
        if day in search_days:
            c.notice(evening + timedelta(seconds=rng.uniform(31, 60)), "search_digest")
        if issues_from is not None and issues_from <= evening:
            c.notice(evening + timedelta(seconds=rng.uniform(61, 90)), "integrity")
    for day in days:
        morning = at(day, 10.0)
        if not since <= morning <= now:
            continue
        waiting = [b for b in c.bank if b[5] == "credit" and b[14] <= morning
                   and b[9] != "ignored" and (b[12] is None or b[12] > morning)]
        if waiting:
            c.notice(morning + timedelta(seconds=rng.uniform(1, 30)), "bank_unmatched")
    if marks:
        await conn.execute(
            "update crm.rentals r set notified_on = u.d, notified_kind = u.k "
            "from unnest($1::bigint[], $2::date[], $3::text[]) as u(id, d, k) "
            "where r.id = u.id",
            [m[0] for m in marks], [m[1] for m in marks], [m[2] for m in marks])
    rows = sorted(c.notes, key=lambda n: (n[0], n[1], n[2] or 0))
    await _copy(conn, "notice_log",
                ["id", "code", "client_id", "target", "status", "detail", "created_at"],
                [(c.ids.take("notice_log"), code, client_id, target, status, detail, t)
                 for t, code, client_id, target, status, detail in rows])


# ─────────────────────── заявки на зачисление ───────────────────────

async def _claims(conn: asyncpg.Connection, c: _Ctx) -> None:
    """Две заявки «Я оплатил(а)», которые ждут оператора: раздел «Заявки»
    и задача на сводке. Кнопка живёт в кабинете бота, поэтому клиент - с
    Telegram, идущей арендой и долгом не больше двух периодов: он платит
    по сроку, а не пропал. Чек пришёл в тот же чат - файла у демо нет,
    receipt_file_id условный (панель пишет только «прислан в чат»).
    Шаг последний: выборка из c.rng не сдвигает данные прежних шагов."""
    balance = {r["client_id"]: D(r["b"]) for r in await conn.fetch(
        "select client_id, sum(amount) as b from crm.ledger group by client_id")}
    pool = sorted((r for r in c.rentals.values()
                   if r.status == "active" and r.search_at is None and r.client_id in c.tg
                   and -2 * r.price <= balance.get(r.client_id, D(0)) < 0),
                  key=lambda r: r.id)
    rows = []
    for r in c.rng.sample(pool, min(2, len(pool))):
        created = max(c.now - timedelta(minutes=c.rng.uniform(20, 300)),
                      r.created_at + timedelta(minutes=5))
        rows.append((r.client_id, r.price, f"demo-receipt-{r.client_id}", True,
                     min(created, c.now)))
    await conn.executemany(
        "insert into crm.payment_claims (client_id, amount_hint, receipt_file_id, "
        "receipt_is_photo, created_at) values ($1, $2, $3, $4, $5)", rows)
