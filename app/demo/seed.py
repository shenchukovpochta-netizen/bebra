"""Генератор данных демо-стенда: сброс базы и сид одной транзакцией.

reset() - снести схемы, накатить schema.sql и засеять заново; populate() -
только засеять свежую схему; summary() - что получилось, числами панели.

Почему SQL напрямую, а не сервисы панели: сервисы ставят now(), а демо
нужна история за полгода с датами в прошлом. Триггеры журналов статусов и
мест на время сида выключены (ALTER TABLE ... DISABLE TRIGGER, не
session_replication_role - та выключает и внешние ключи), журналы ядро
пишет само и явно. Детерминизм: одно зерно и одни «сегодня/сейчас» - одна
и та же база; всё случайное - только из random.Random(seed).
"""

from __future__ import annotations

import asyncio
import json
import random
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg

from ..crm import logic
from ..crm.db import CrmDB
from . import core, seed_extras, seed_service
from .people import People
from .world import MSK, Point, StaffMember, World, at

__all__ = ["World", "populate", "reset", "summary", "SCHEMA"]

SCHEMA = Path(__file__).resolve().parent.parent.parent / "schema.sql"
D = Decimal

# Логины демо: пароль у трёх показанных на входе - demo, у остальных
# случайный из rng - войти под ними незачем, это просто люди в журналах.
STAFF = (
    # логин, имя, профиль, роль, точка, пароль demo
    ("demo", "Демидов Демьян Демидович", "owner", "admin", None, True),
    ("operator", "Смирнова Алина Сергеевна", "manager", "manager", core.P1, True),
    ("mechanic", "Кузнецов Тимур Ринатович", "tech", "manager", core.P1, True),
    ("operator2", "Хасанова Диляра Маратовна", "manager", "manager", core.P2, False),
    ("mechanic2", "Сафин Ильдар Айдарович", "tech", "manager", core.P2, False),
    ("operator3", "Волков Артём Олегович", "manager", "manager", core.P3, False),
)

# Реквизиты явно вымышленные: ИНН из нулей, «ИП Демо Д. Д.» - ни один
# документ демо не должен выглядеть как настоящий.
COMPANY = {
    "company_name": "Индивидуальный предприниматель Демо Демьян Демидович",
    "company_short": "ИП Демо Д. Д.",
    "company_inn": "000000000000",
    "company_ogrn": "000000000000000",
    "company_tax": "УСН «доходы» (демо)",
    "company_address": "г. Казань, ул. Примерная, д. 0 (демо)",
    "company_phone": "+7 (000) 000-00-00",
    "company_email": "demo@example.com",
    "company_bank": "АО «Демо-Банк» (демо)",
    "company_account": "40802810000000000000",
    "company_bik": "000000000",
    "company_corr": "30101810000000000000",
    "company_director": "Демо Д. Д.",
}
# План месяца: часть выполнена, часть нет - сводке есть что подсветить.
PLAN = {"plan_rented": "160", "plan_check": "500", "plan_repair": "8",
        "plan_spare": "4", "plan_free": "8"}

# Таблицы с явными id сида и их колонки - в порядке записи (внешние ключи).
_TABLES = ("purchases", "bikes", "batteries", "clients", "rentals", "rental_bikes",
           "rental_extras", "cash_shifts", "ledger", "cash_moves", "bike_status_log",
           "bike_location_log", "battery_status_log")
_TRIGGERS = (("bikes", "bikes_status_log"), ("bikes", "bikes_location_log"),
             ("batteries", "batteries_status_log"))


def _moment(today: date, now: datetime | None) -> datetime:
    """«Сейчас» сида. По умолчанию 04:00 якорного дня - час ночного сброса.

    Ни одна строка не должна оказаться в будущем: журнал статусов дал бы
    отрицательные интервалы, пока этот момент не наступит. Поэтому
    умолчание из будущего - ошибка, а не молча испорченные три числа:
    до 04:00 передайте now явно (datetime.now(MSK)).
    """
    if now is None:
        moment = at(today, 4.0)
        if moment > datetime.now(MSK):
            raise ValueError("04:00 сегодня ещё не наступило: передайте now явно")
        return moment
    if now.tzinfo is None:
        raise ValueError("now должен быть с часовым поясом")
    now = now.astimezone(MSK)
    if now.date() != today:
        raise ValueError(f"now {now:%Y-%m-%d %H:%M} не в дне today {today}")
    return now


async def reset(pool: asyncpg.Pool, *, today: date, seed: int = 7,
                now: datetime | None = None, secret: str | None = None) -> dict[str, Any]:
    """Снести crm и bot, накатить schema.sql и засеять - одной транзакцией.
    Возвращает summary(): состав, три числа за 30 дней, расхождения.

    today - якорь истории, now - «сейчас» внутри него (aware; по
    умолчанию 04:00, см. _moment). Одни и те же seed/today/now - та же база.
    secret - CRM_SECRET демо-панели: от него ключ переписки «Входящих»
    (seed_extras.inbox_key_text), чтобы панель читала то, что зашифровал
    сид, без отдельного секрета. Не задан - постоянный DEMO_SECRET.

    Одна транзакция на всё: сбой ночного сброса оставляет вчерашнее демо
    целым, а не пустую схему; читатели панели ждут коммита и видят либо
    старое, либо новое. Схема накатывается тем же текстом и под той же
    консультативной блокировкой, что Database.apply_schema, - но на
    соединении сида, иначе она ушла бы отдельной транзакцией.
    """
    sql = SCHEMA.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        # Подготовленные запросы соединений пула помнят таблицы до сброса.
        # Вне транзакции asyncpg переподготовит их сам, а внутри - нет:
        # второй ночной сброс на том же соединении упал бы. Кэш - долой у
        # всего пула: и до сброса, и после, для запросов панели.
        await conn.reload_schema_state()
        async with conn.transaction():
            await conn.execute("select pg_advisory_xact_lock(7331)")
            # Запрос посетителя, начатый до 503, держит замки таблиц: без
            # предела DROP ждал бы его сколько угодно, и демо висело бы
            # в «обновляется». С пределом ожидание - ошибка, а её ночной
            # сброс повторит (runtime.RETRY_AFTER).
            await conn.execute("set local lock_timeout = '30s'")
            await conn.execute("drop schema if exists crm cascade; "
                               "drop schema if exists bot cascade")
            await conn.execute(sql)
            world = await populate(conn, today=today, seed=seed, now=now,
                                   secret=secret)
        await conn.reload_schema_state()
        return await summary(conn, world)


async def populate(pool_or_conn: asyncpg.Pool | asyncpg.Connection, *, today: date,
                   seed: int = 7, now: datetime | None = None,
                   secret: str | None = None) -> World:
    """Засеять свежую схему: ядро, затем сервис и остальное - одной
    транзакцией (внутри чужой - точкой сохранения)."""
    if isinstance(pool_or_conn, asyncpg.Pool):
        async with pool_or_conn.acquire() as conn:
            return await _populate(conn, today=today, seed=seed, now=now, secret=secret)
    return await _populate(pool_or_conn, today=today, seed=seed, now=now, secret=secret)


async def _populate(conn: asyncpg.Connection, *, today: date, seed: int,
                    now: datetime | None, secret: str | None) -> World:
    moment = _moment(today, now)
    rng = random.Random(seed)
    world = World(seed=seed, today=today, now=moment, rng=rng, people=People(rng),
                  history_start=at(core.history_start(today)))
    async with conn.transaction():
        # Сутки отчётов режутся по поясу сессии: сид обязан жить в том же.
        await conn.execute("set local time zone 'Europe/Moscow'")
        if await conn.fetchval("select exists (select 1 from crm.bikes)"):
            raise RuntimeError("демо-сид пишет только в пустую схему: сначала reset()")
        for table, trigger in _TRIGGERS:
            await conn.execute(f"alter table crm.{table} disable trigger {trigger}")
        try:
            # Точка сохранения: при сбое тело откатывается, а включение
            # триггеров ниже всё ещё выполнимо - транзакция не «сломана».
            async with conn.transaction():
                await _reference(conn, world)
                ids = {t: int(await conn.fetchval(
                    f"select coalesce(max(id), 0) from crm.{t}")) for t in _TABLES}
                # Симуляция - чистый Python на секунды: в потоке, чтобы
                # панель в том же процессе отвечала, пока идёт сброс.
                sim = await asyncio.to_thread(core.build_world, world, ids)
                world = sim.w
                world.later = core.later_today(sim)
                await _write(conn, world, sim)
                await seed_service.populate(conn, world)
                await seed_extras.populate(conn, world, secret=secret)
        finally:
            for table, trigger in _TRIGGERS:
                await conn.execute(f"alter table crm.{table} enable trigger {trigger}")
    return world


# ─────────────────────────── справочники ───────────────────────────

async def _reference(conn: asyncpg.Connection, w: World) -> None:
    """Точки, сотрудники, настройки, поставщик, батареи и тарифы - всё, у
    чего id выдаёт база и на что ссылается симуляция."""
    rng = w.rng
    start = w.history_start
    p3_open = core.p3_opened_on(w.today)
    spec = core.P3_SPEC
    await conn.execute(
        """
        insert into crm.locations (name, city, address, sort, public_title, phone,
                                   hours, lat, lon, created_at, note)
        values ($1, 'Казань', $2, 30, $3, $4, $5, $6, $7, $8,
                'Вымышленная точка демо-стенда')
        """, core.P3, spec["address"], spec["public_title"], spec["phone"],
        spec["hours"], spec["lat"], spec["lon"], at(p3_open, 9.0) - timedelta(days=2))
    await conn.execute("update crm.locations set created_at = $1 where name <> $2",
                       start, core.P3)
    # У двух настоящих точек схема ставит рабочий телефон проката. Демо
    # звонков не принимает: номер - вымышленный, как у всех в демо.
    for name, phone in core.POINT_PHONES.items():
        await conn.execute("update crm.locations set phone = $2 where name = $1",
                           name, phone)
    for row in await conn.fetch("select * from crm.locations order by sort, name"):
        opened = p3_open if row["name"] == core.P3 else start.date()
        w.points[row["name"]] = Point(
            id=row["id"], name=row["name"], address=row["address"] or "",
            lat=float(row["lat"]), lon=float(row["lon"]), opened_on=opened,
            phone=row["phone"] or "", hours=row["hours"] or "", sort=row["sort"])

    profiles = {r["code"]: r["id"] for r in
                await conn.fetch("select id, code from crm.access_profiles")}
    for login, name, profile, role, point, demo in STAFF:
        password = "demo" if demo else rng.randbytes(12).hex()
        created = (at(p3_open, 9.0) - timedelta(days=3) if point == core.P3
                   else start - timedelta(days=7))
        sid = await conn.fetchval(
            """
            insert into crm.staff (login, password_hash, name, role, profile_id,
                                   location, created_at)
            values ($1, $2, $3, $4, $5, $6, $7) returning id
            """, login, logic.hash_password(password, salt=rng.randbytes(16)), name,
            role, profiles[profile], point, created)
        w.staff[login] = StaffMember(id=sid, login=login, name=name, profile=profile,
                                     location=point)

    settings = {**COMPANY, **PLAN,
                "points_history_since": start.isoformat(),
                "bike_check_required": "1", "search_after_days": "7",
                "theft_after_days": "21"}
    await conn.executemany(
        """
        insert into crm.settings (key, value, updated_by, updated_at)
        values ($1, $2, 'staff:demo', $3)
        on conflict (key) do update set value = excluded.value,
          updated_by = excluded.updated_by, updated_at = excluded.updated_at
        """, [(k, v, start) for k, v in settings.items()])

    w.suppliers[core.SUPPLIER_BIKES] = await conn.fetchval(
        "insert into crm.suppliers (name, phone, note, created_at) "
        "values ($1, $2, 'Поставщик велосипедов, демо', $3) returning id",
        core.SUPPLIER_BIKES, "+7 (000) 000-11-22", start - timedelta(days=420))

    for title, brand, volts, capacity, price, months in core.BATTERY_MODELS:
        w.battery_models[title] = await conn.fetchval(
            """
            insert into crm.battery_models (title, brand, voltage, capacity, price,
                                            service_months, note)
            values ($1, $2, $3, $4, $5, $6, 'демо') returning id
            """, title, brand, volts, capacity, price, months)
    for row in await conn.fetch("select id, title from crm.bike_models"):
        w.bike_models[row["title"]] = row["id"]
    await conn.executemany(
        "insert into crm.compat (bike_model_id, battery_model_id, primary_fit) "
        "values ($1, $2, true)",
        [(w.bike_models[m], w.battery_models[b]) for m, b in core.BATTERY_OF.items()
         if m in w.bike_models])
    sort = 40
    for model, prices in core.BATTERY_TARIFFS.items():
        for days, price in prices.items():
            sort += 1
            await conn.execute(
                """
                insert into crm.tariffs (name, model, period_days, price, sort, kind,
                                         note, created_at)
                values ($1, $2, $3, $4, $5, 'battery', 'Доп. аккумулятор', $6)
                """, core.PERIOD_NAMES[days], model, days, D(price), sort, start)
    for row in await conn.fetch(
            "select id, name, model, period_days, price, kind from crm.tariffs "
            "where active and model is not null"):
        w.tariffs[(row["kind"], row["model"], row["period_days"])] = {
            "id": row["id"], "name": row["name"], "price": row["price"]}
    missing = [m for m in core.BATTERY_OF if ("bike", m, 7) not in w.tariffs]
    if missing:
        raise RuntimeError(f"в каталоге схемы нет тарифов моделей: {missing}")


# ─────────────────────────── запись ───────────────────────────

async def _copy(conn: asyncpg.Connection, table: str, columns: list[str],
                records: list[tuple]) -> None:
    if records:
        await conn.copy_records_to_table(table, schema_name="crm", columns=columns,
                                         records=records)


async def _write(conn: asyncpg.Connection, w: World, sim: core.Sim) -> None:
    """Всё насимулированное - в базу, по порядку внешних ключей, COPY."""
    batches = core.BATCHES
    await _copy(conn, "purchases",
                ["id", "no", "supplier_id", "purchased_on", "total", "note",
                 "created_by", "created_at"],
                [(p["id"], p["no"], p["supplier_id"], p["purchased_on"], p["total"],
                  p["note"], p["created_by"], p["created_at"]) for p in w.purchases])
    bat_price = {m[0]: m[4] for m in core.BATTERY_MODELS}
    rows = []
    for b in w.bikes:
        batch = batches[b.batch - 1]
        extra = sim.bike_extra[b.id]
        checked = b.commissioned_at is not None
        rows.append((
            b.id, b.code, b.model, b.frame_no, b.motor_no, 2, b.status, b.price,
            b.purchased_on, b.note, b.created_at, extra["updated_at"], b.point,
            batch.months, D(batch.residual), bat_price[core.BATTERY_OF[b.model]], 15,
            b.mileage_km, b.spare, b.purchase_id, b.plate_no, b.status != "new",
            b.tracker, b.commissioned_at, extra.get("commissioned_by") if checked
            else None))
    await _copy(conn, "bikes",
                ["id", "code", "model", "frame_no", "motor_no", "battery_count",
                 "status", "purchase_price", "purchased_on", "note", "created_at",
                 "updated_at", "location", "service_months", "residual_price",
                 "battery_price", "battery_service_months", "mileage_km", "spare",
                 "purchase_id", "plate_no", "plate_ok", "tracker_ok",
                 "commissioned_at", "commissioned_by"], rows)
    # jsonb - текстом через unnest: COPY зависел бы от кодека соединения.
    marks = [(b.id, json.dumps(sim.bike_extra[b.id]["checked"], ensure_ascii=False))
             for b in w.bikes if sim.bike_extra[b.id].get("checked")]
    if marks:
        await conn.execute(
            "update crm.bikes b set checked = x.c::jsonb "
            "from unnest($1::bigint[], $2::text[]) as x(id, c) where b.id = x.id",
            [m[0] for m in marks], [m[1] for m in marks])

    spec = {m[0]: m for m in core.BATTERY_MODELS}
    await _copy(conn, "clients",
                ["id", "full_name", "phone", "status", "contract_no", "note", "source",
                 "created_at", "updated_at", "ref_code", "invited_by", "invited_at",
                 "channel", "employer", "experience"],
                [(c.id, c.full_name, c.phone, c.status, c.contract_no, c.note, c.source,
                  c.created_at, c.created_at, c.ref_code, c.invited_by, c.invited_at,
                  c.channel, c.employer, c.experience) for c in w.clients])

    rows = []
    for r in w.rentals:
        x = sim.rx[r.id]
        seg = x.segments[-1]
        intent = sim.intents.get(r.id)
        rows.append((
            r.id, r.client_id, r.bike_id, r.tariff_id, r.tariff_name, r.period_days,
            r.price, "auto", w.client(r.client_id).contract_no, r.started_on,
            r.billed_until, r.status, r.closed_on,
            (core.LOST_NOTE if r.lost else core.RETURN_NOTE)
            if r.status == "closed" else None,
            x.segments[0]["by"], r.created_at, r.closed_at or x.touched,
            seg["mileage_start"], seg["mileage_end"] if r.status == "closed" else None,
            intent[0] if intent else None, intent[1] if intent else None,
            intent[2] if intent else None, intent[3] if intent else None,
            r.search_at, "staff:demo" if r.search_at else None,
            "Не отвечает на звонки и сообщения, трекер молчит" if r.search_at else None,
            r.base_price, r.point))
    await _copy(conn, "rentals",
                ["id", "client_id", "bike_id", "tariff_id", "tariff_name", "period_days",
                 "price", "billing", "contract_no", "started_on", "billed_until",
                 "status", "closed_on", "close_note", "created_by", "created_at",
                 "updated_at", "mileage_start", "mileage_end", "intent", "intent_until",
                 "intent_by", "intent_at", "search_at", "search_by", "search_note",
                 "base_price", "location"], rows)
    await _copy(conn, "rental_bikes",
                ["id", "rental_id", "bike_id", "issued_on", "returned_on",
                 "mileage_start", "mileage_end", "reason", "created_by", "created_at"],
                [(s["id"], r.id, s["bike_id"], s["issued_on"], s["returned_on"],
                  s["mileage_start"], s["mileage_end"], s["reason"], s["by"],
                  s["created_at"]) for r in w.rentals for s in sim.rx[r.id].segments])

    rows = []
    for bat in w.batteries:
        model = spec[bat.model]
        extra = sim.bat_extra.get(bat.id, {})
        rows.append((
            bat.id, bat.code, bat.model_id, bat.serial_no, bat.status, bat.point,
            bat.bike_id, bat.rental_id, bat.cycles, bat.price, bat.purchased_on,
            model[5], bat.created_at, sim.bat_last.get(bat.id, bat.created_at),
            model[2], model[3], extra.get("commissioned_at"),
            extra.get("commissioned_by")))
    await _copy(conn, "batteries",
                ["id", "code", "model_id", "serial_no", "status", "location", "bike_id",
                 "rental_id", "cycles", "purchase_price", "purchased_on",
                 "service_months", "created_at", "updated_at", "volts", "amp_hours",
                 "commissioned_at", "commissioned_by"], rows)
    marks = [(i, json.dumps(e["checked"], ensure_ascii=False))
             for i, e in sim.bat_extra.items() if e.get("checked")]
    if marks:
        await conn.execute(
            "update crm.batteries b set checked = x.c::jsonb "
            "from unnest($1::bigint[], $2::text[]) as x(id, c) where b.id = x.id",
            [m[0] for m in marks], [m[1] for m in marks])
    await _copy(conn, "rental_extras",
                ["id", "rental_id", "kind", "battery_id", "title", "price", "added_at",
                 "added_by", "removed_at", "removed_by"],
                [(e["id"], r.id, "battery", e["battery_id"], e["title"], e["price"],
                  e["added_at"], e["added_by"], e["removed_at"], e["removed_by"])
                 for r in w.rentals for e in sim.rx[r.id].extras])

    await _copy(conn, "cash_shifts",
                ["id", "no", "location", "status", "opened_at", "opened_by", "opening",
                 "closed_at", "closed_by", "counted", "expected", "diff"],
                [(s.id, s.no, s.point, s.status, s.opened_at, w.operator(s.point).actor,
                  s.opening, s.closed_at,
                  w.operator(s.point).actor if s.closed_at else None, s.counted,
                  s.expected, (s.counted - s.expected) if s.closed_at else None)
                 for s in w.shifts])
    await _copy(conn, "ledger",
                ["id", "client_id", "rental_id", "kind", "amount", "method",
                 "period_from", "period_to", "note", "created_by", "created_at",
                 "shift_id"], sim.ledger_rows)
    await _copy(conn, "cash_moves",
                ["id", "shift_id", "kind", "amount", "reason", "ledger_id", "created_at",
                 "created_by"], sim.move_rows)
    await _copy(conn, "bike_status_log",
                ["id", "bike_id", "from_status", "to_status", "changed_at", "changed_by",
                 "mileage_km"], sim.status_rows)
    await _copy(conn, "bike_location_log",
                ["id", "bike_id", "from_location", "to_location", "changed_at",
                 "changed_by"], sim.loc_rows)
    await _copy(conn, "battery_status_log",
                ["id", "battery_id", "from_status", "to_status", "changed_at",
                 "changed_by"], sim.bstatus_rows)

    # Счётчики: панель и модули после ядра заводят строки своими id.
    for table in (*_TABLES, "suppliers", "staff", "battery_models", "tariffs",
                  "locations"):
        await conn.execute(
            f"select setval(pg_get_serial_sequence('crm.{table}', 'id'), "
            f"greatest((select max(id) from crm.{table}), 1))")
    if w.contract_seq:
        await conn.execute("select setval('bot.contract_seq', $1)", w.contract_seq)


# ─────────────────────────── итог ───────────────────────────

def _num(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(round(value, 2))
    return value


async def summary(pool_or_conn: asyncpg.Pool | asyncpg.Connection,
                  world: World | None = None, *,
                  until: datetime | None = None) -> dict[str, Any]:
    """Что получилось - теми же функциями, что считает панель: дни и деньги
    из CrmDB, три числа из logic.fleet_metrics, точки из logic.points_rows,
    расхождения из logic.integrity_issues. Окно - 30 дней до «сейчас» сида
    (или until): итог не зависит от того, когда его спросили."""
    if isinstance(pool_or_conn, asyncpg.Pool):
        async with pool_or_conn.acquire() as conn:
            return await summary(conn, world, until=until)
    async with pool_or_conn.transaction():
        # Пояс - только на время итога: соединение принадлежит пулу панели.
        await pool_or_conn.execute("set local time zone 'Europe/Moscow'")
        return await _summary(pool_or_conn, world, until)


async def _summary(conn: asyncpg.Connection, world: World | None,
                   until: datetime | None) -> dict[str, Any]:
    crm = CrmDB(conn)
    until = until or (world.now if world else datetime.now(MSK))
    since = until - timedelta(days=30)
    metrics = logic.fleet_metrics(await crm.bike_days_by_status(since, until),
                                  await crm.rental_revenue(since, until))
    fleet = await crm.bikes(limit=10000)
    status = Counter(b["status"] for b in fleet)
    places = await crm.locations()
    points = logic.points_rows(places, bikes=fleet,
                               days=await crm.bike_days_by_location(since, until),
                               money=await crm.money_by_location(since, until))
    issues = logic.integrity_issues(
        fleet, await crm.active_rentals(), await crm.open_orders_by_bike(),
        await crm.debtors(200), batteries=await crm.batteries(limit=10000))
    ledger = {r["kind"]: {"rows": r["n"], "sum": _num(r["total"])} for r in
              await conn.fetch("select kind, count(*) as n, sum(amount) as total "
                               "from crm.ledger group by kind order by kind")}
    # Доля продлений в начислениях за то же окно - как «Продления» отчёта
    # по точкам (period_from позже начала аренды).
    share = await conn.fetchrow(
        """
        select coalesce(-sum(l.amount) filter (where l.period_from > r.started_on), 0)
                 as renewals,
               coalesce(-sum(l.amount), 0) as charged
          from crm.ledger l join crm.rentals r on r.id = l.rental_id
         where l.kind = 'charge' and l.created_at >= $1 and l.created_at < $2
        """, since, until)
    avg_days = await conn.fetchval(
        "select avg(closed_on - started_on + 1) from crm.rentals "
        "where status = 'closed' and close_note = $1", core.RETURN_NOTE)
    counts = {r["t"]: r["n"] for r in await conn.fetch(
        """
        select 'clients' as t, count(*) as n from crm.clients
        union all select 'rentals', count(*) from crm.rentals
        union all select 'rentals_active', count(*) from crm.rentals where status = 'active'
        union all select 'in_search', count(*) from crm.rentals
                  where status = 'active' and search_at is not null
        union all select 'swaps', count(*) from crm.rental_bikes where reason <> 'Выдача'
        union all select 'extras', count(*) from crm.rental_extras
        union all select 'batteries', count(*) from crm.batteries
        union all select 'shifts', count(*) from crm.cash_shifts
        union all select 'shifts_open', count(*) from crm.cash_shifts where status = 'open'
        union all select 'bike_status_log', count(*) from crm.bike_status_log
        union all select 'bike_location_log', count(*) from crm.bike_location_log
        union all select 'purchases', count(*) from crm.purchases
        """)}
    batteries = Counter(r["status"] for r in
                        await conn.fetch("select status from crm.batteries"))
    by_kind: dict[str, int] = defaultdict(int)
    for issue in issues:
        by_kind[issue["kind"]] += 1

    def three(m: dict[str, Any]) -> dict[str, Any]:
        return {"idle_percent": m["idle_percent"], "avg_check": _num(m["avg_check"]),
                "revenue": _num(m["revenue"]),
                "operational_days": _num(m["operational_days"]),
                "rented_days": _num(m["rented_days"]),
                "idle_days": {k: _num(v) for k, v in m["idle_breakdown"].items()}}

    return {
        "today": (world.today if world else until.date()).isoformat(),
        "until": until.isoformat(),
        "history_start": world.history_start.isoformat() if world else None,
        "counts": counts,
        "bikes": dict(sorted(status.items())),
        "operational": sum(status.get(s, 0) for s in logic.OPERATIONAL_STATUSES),
        "batteries": dict(sorted(batteries.items())),
        "three": three(metrics),
        "points": {(row["title"]): {"fleet": row["fleet"], **three(row["metrics"])}
                   for row in points["rows"]},
        "points_total": three(points["total"]["metrics"]),
        "ledger": ledger,
        "renewal_share": round(float(share["renewals"] / share["charged"]), 3)
        if share["charged"] else None,
        "avg_rental_days": round(float(avg_days), 1) if avg_days else None,
        "integrity": dict(sorted(by_kind.items())),
        "attempt": world.attempt if world else None,
        # Остаток сегодняшнего дня - его делает процесс демо в свой час
        # (runtime.live_day); в лог идёт числом, а не списком.
        "later": list(world.later) if world else [],
    }
