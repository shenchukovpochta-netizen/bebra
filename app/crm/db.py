"""Доступ к схеме crm. Тот же пул asyncpg, что у бота, без ORM.

Все методы возвращают dict или list[dict], а не Record: строки уходят
в шаблоны Jinja и в тексты Telegram, и там удобнее словарь. Суммы
приходят Decimal - numeric в Postgres, и терять копейки во float незачем.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg

from . import logic

# Белые списки колонок для UPDATE: имена подставляются в SQL текстом.
BIKE_FIELDS = frozenset({
    "code", "model", "frame_no", "motor_no", "battery_count", "status",
    "purchase_price", "purchased_on", "note", "location", "service_months",
    "residual_price", "battery_price", "battery_service_months", "mileage_km",
    "spare", "plate_no", "plate_ok", "tracker_ok", "checked",
    "commissioned_at", "commissioned_by",
})
CLIENT_FIELDS = frozenset({
    "full_name", "phone", "tg_id", "username", "status", "contract_no",
    "note", "source", "channel", "ref_code", "invited_by", "invited_at",
    "max_id",
})
TARIFF_FIELDS = frozenset({"name", "period_days", "price", "note", "active",
                          "sort", "model", "kind"})
WORK_TYPE_FIELDS = frozenset({"title", "category", "minutes", "price", "node",
                              "active", "sort"})
BATTERY_FIELDS = frozenset({"code", "model_id", "serial_no", "status", "location",
                            "bike_id", "rental_id", "cycles", "purchase_price",
                            "purchased_on", "service_months", "note",
                            "volts", "amp_hours"})
LOCATION_FIELDS = frozenset({"city", "name", "address", "note", "active", "sort",
                            "public_title", "phone", "hours", "lat", "lon"})
BIKE_MODEL_FIELDS = frozenset({"title", "brand", "factory_title",
                               "battery_slots", "active", "note",
                               "weight_kg", "speed_kmh", "range_km",
                               "charge_hours", "wheel_size", "motor_watt",
                               "max_load_kg", "size_note", "photo_url",
                               "description"})
BATTERY_MODEL_FIELDS = frozenset({"title", "brand", "voltage", "capacity",
                                  "price", "service_months", "active", "note"})
TEMPLATE_FIELDS_DB = frozenset({"code", "title", "body", "body_max", "active",
                                "note"})
TRACKER_FIELDS = frozenset({"device_id", "alias", "bike_id", "active", "last_seen",
                            "lat", "lon", "speed", "course", "voltage", "gsm_level",
                            "alarm", "note"})
SUPPLIER_FIELDS = frozenset({"name", "phone", "note", "active"})
PART_FIELDS = frozenset({"title", "node", "unit", "cost", "price", "min_stock",
                         "model", "active", "note"})
PART_ORDER_FIELDS = frozenset({"supplier_id", "status", "total", "note",
                               "ordered_at", "closed_at", "doc_id"})
REFERRAL_FIELDS = frozenset({"status", "client_id", "bonus", "ledger_id", "note",
                             "signed_at", "rented_at", "paid_at"})
TAKE_FIELDS = frozenset({"scope", "location", "status", "note", "expected",
                         "found", "missing", "extra", "closed_at"})
ORDER_FIELDS = frozenset({
    "status", "tech_id", "complaint", "object_note", "estimate", "note",
    "payer", "client_id", "total", "cost", "closed_at", "paid_at", "log_id",
    "estimate_sent_at", "approved_at", "approved_by", "declined_at",
})
RENTAL_FIELDS = frozenset({
    "search_at", "search_by", "search_note",
    "tariff_id", "tariff_name", "period_days", "price", "base_price", "billing",
    "contract_no", "bike_id", "billed_until", "notified_on", "notified_kind",
    "intent", "intent_until", "intent_by", "intent_at", "snooze_until",
    "mileage_start", "mileage_end",
})


def _rows(records: list[asyncpg.Record]) -> list[dict]:
    return [dict(r) for r in records]


def _row(record: asyncpg.Record | None) -> dict | None:
    return dict(record) if record is not None else None


def _money(value: Any) -> Decimal | None:
    """Число из внешнего API - в numeric. Float в колонку numeric asyncpg
    не примет, а данные трекера приходят именно float."""
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _set_clause(fields: dict[str, Any], allowed: frozenset[str],
                start: int) -> tuple[str, list[Any]]:
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"недопустимые колонки: {sorted(unknown)}")
    cols = list(fields)
    sets = [f"{col} = ${i}" for i, col in enumerate(cols, start=start)]
    return ", ".join(sets), [fields[c] for c in cols]


class CrmDB:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # ─────────────────────── сотрудники ───────────────────────

    async def staff_count(self) -> int:
        return int(await self.pool.fetchval("select count(*) from crm.staff") or 0)

    # Сотрудник всегда читается вместе со своим профилем: права нужны
    # на каждом запросе, и второй поход в базу за ними - лишний.
    _STAFF_SELECT = """
        select s.*, p.name as profile_name, p.code as profile_code,
               p.built_in as profile_built_in,
               coalesce(p.perms, '{}'::jsonb) as perms
        from crm.staff s
        left join crm.access_profiles p on p.id = s.profile_id
    """

    async def staff_by_login(self, login: str) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._STAFF_SELECT} where s.login = $1", login))

    async def staff_by_id(self, staff_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._STAFF_SELECT} where s.id = $1", staff_id))

    async def staff_all(self) -> list[dict]:
        return _rows(await self.pool.fetch(
            f"{self._STAFF_SELECT} order by s.active desc, s.id"))

    async def create_staff(self, login: str, password_hash: str, name: str,
                           role: str, profile_id: int | None = None) -> int:
        return int(await self.pool.fetchval(
            "insert into crm.staff (login, password_hash, name, role, profile_id) "
            "values ($1, $2, $3, $4, $5) returning id",
            login, password_hash, name, role, profile_id))

    async def set_staff_profile(self, staff_id: int, profile_id: int | None) -> None:
        await self.pool.execute(
            "update crm.staff set profile_id = $2 where id = $1", staff_id, profile_id)

    # ─────────────────────── профили доступа ───────────────────────

    async def access_profiles(self) -> list[dict]:
        """Профили со счётчиком сотрудников: сколько человек потеряет
        доступ, если профиль изменить или удалить."""
        return _rows(await self.pool.fetch(
            """
            select p.*, count(s.id) filter (where s.active) as staff_count
            from crm.access_profiles p
            left join crm.staff s on s.profile_id = p.id
            group by p.id
            order by p.built_in desc, p.name
            """))

    async def access_profile(self, profile_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.access_profiles where id = $1", profile_id))

    async def access_profile_by_code(self, code: str) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.access_profiles where code = $1", code))

    async def create_access_profile(self, name: str, perms: dict) -> int:
        return int(await self.pool.fetchval(
            "insert into crm.access_profiles (name, perms) values ($1, $2::jsonb) "
            "returning id", name, perms))

    async def update_access_profile(self, profile_id: int, *, name: str,
                                    perms: dict) -> None:
        await self.pool.execute(
            "update crm.access_profiles set name = $2, perms = $3::jsonb, "
            "updated_at = now() where id = $1 and not built_in",
            profile_id, name, perms)

    async def delete_access_profile(self, profile_id: int) -> bool:
        """False - профиль встроенный или на нём ещё висят сотрудники."""
        row = await self.pool.fetchrow(
            "delete from crm.access_profiles where id = $1 and not built_in "
            "and not exists (select 1 from crm.staff where profile_id = $1) "
            "returning id", profile_id)
        return row is not None

    async def set_staff_password(self, staff_id: int, password_hash: str) -> None:
        await self.pool.execute(
            "update crm.staff set password_hash = $2 where id = $1",
            staff_id, password_hash)

    async def set_staff_active(self, staff_id: int, active: bool) -> None:
        await self.pool.execute(
            "update crm.staff set active = $2 where id = $1", staff_id, active)

    # ─────────────────────── тарифы ───────────────────────

    async def tariffs(self, *, active_only: bool = False,
                      kind: str | None = None) -> list[dict]:
        conds, args = [], []
        if active_only:
            conds.append("active")
        if kind:
            args.append(kind)
            conds.append(f"kind = ${len(args)}")
        where = ("where " + " and ".join(conds)) if conds else ""
        return _rows(await self.pool.fetch(
            f"select * from crm.tariffs {where} "
            "order by kind, sort, period_days, id", *args))

    async def tariff(self, tariff_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.tariffs where id = $1", tariff_id))

    async def create_tariff(self, name: str, period_days: int, price: Decimal,
                            note: str | None, model: str | None = None,
                            kind: str = "bike") -> int:
        return int(await self.pool.fetchval(
            "insert into crm.tariffs (name, period_days, price, note, model, kind) "
            "values ($1, $2, $3, $4, $5, $6) returning id",
            name, period_days, price, note, model, kind))

    async def update_tariff(self, tariff_id: int, **fields: Any) -> None:
        sets, values = _set_clause(fields, TARIFF_FIELDS, 2)
        await self.pool.execute(
            f"update crm.tariffs set {sets} where id = $1", tariff_id, *values)

    # ─────────────────────── парк ───────────────────────

    async def bikes(self, *, status: str | None = None, q: str | None = None,
                    location: str | None = None, limit: int = 500) -> list[dict]:
        """Список с текущим арендатором - одним запросом, без N+1."""
        conds: list[str] = []
        args: list[Any] = []
        if status:
            args.append(status)
            conds.append(f"b.status = ${len(args)}")
        if location == "none":
            conds.append("b.location is null")
        elif location:
            args.append(location)
            conds.append(f"b.location = ${len(args)}")
        if q:
            args.append(f"%{q.strip()}%")
            conds.append(f"(b.code ilike ${len(args)} or b.model ilike ${len(args)} "
                         f"or b.frame_no ilike ${len(args)})")
        where = ("where " + " and ".join(conds)) if conds else ""
        args.append(limit)
        return _rows(await self.pool.fetch(
            f"""
            select b.*, r.id as rental_id, c.id as client_id, c.full_name
            from crm.bikes b
            left join crm.rentals r on r.bike_id = b.id and r.status = 'active'
            left join crm.clients c on c.id = r.client_id
            {where}
            order by b.code
            limit ${len(args)}
            """, *args))

    async def bike(self, bike_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            """
            select b.*, r.id as rental_id, c.id as client_id, c.full_name
            from crm.bikes b
            left join crm.rentals r on r.bike_id = b.id and r.status = 'active'
            left join crm.clients c on c.id = r.client_id
            where b.id = $1
            """, bike_id))

    async def bike_by_frame(self, frame_no: str) -> dict | None:
        # Без учёта регистра: панель и бот хранят VIN как ввели, импорт
        # приводит к верхнему регистру - иначе один велосипед заводился бы дважды.
        return _row(await self.pool.fetchrow(
            "select * from crm.bikes where upper(frame_no) = upper($1)", frame_no))

    async def bike_by_motor(self, motor_no: str) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.bikes where upper(motor_no) = upper($1)", motor_no))

    async def bike_by_code(self, code: str) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.bikes where code = $1", code))

    async def create_bike(self, *, by: str | None = None, **fields: Any) -> int:
        unknown = set(fields) - BIKE_FIELDS
        if unknown:
            raise ValueError(f"недопустимые колонки: {sorted(unknown)}")
        cols = list(fields)
        placeholders = ", ".join(f"${i}" for i in range(1, len(cols) + 1))
        async with self.pool.acquire() as conn, conn.transaction():
            # Первая запись журнала статусов тоже должна знать автора.
            await conn.execute("select set_config('crm.actor', $1, true)", by or "")
            return int(await conn.fetchval(
                f"insert into crm.bikes ({', '.join(cols)}) values ({placeholders}) "
                f"returning id", *[fields[c] for c in cols]))

    async def update_bike(self, bike_id: int, *, by: str | None = None,
                          **fields: Any) -> None:
        """by - кто меняет: триггер журнала статусов читает его из
        set_config('crm.actor') в той же транзакции."""
        sets, values = _set_clause(fields, BIKE_FIELDS, 2)
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('crm.actor', $1, true)", by or "")
            await conn.execute(
                f"update crm.bikes set {sets}, updated_at = now() where id = $1",
                bike_id, *values)

    async def bike_status_log(self, bike_id: int, limit: int = 30) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select * from crm.bike_status_log where bike_id = $1 "
            "order by changed_at desc, id desc limit $2", bike_id, limit))

    async def bike_status_since(self) -> dict[int, datetime]:
        """С какого момента каждый велосипед в текущем статусе - по журналу.

        Одним запросом на весь парк: мастеру выдачи нужен простой каждого
        свободного велосипеда, чтобы выдавать тот, что стоит дольше всех.
        """
        rows = await self.pool.fetch(
            "select bike_id, max(changed_at) as since from crm.bike_status_log "
            "group by bike_id")
        return {int(r["bike_id"]): r["since"] for r in rows}

    async def bike_days_by_status(self, since: datetime, until: datetime) -> dict[str, Decimal]:
        """Велосипеде-дни по статусам за [since, until) по журналу статусов.

        Интервал статуса длится до следующей записи того же велосипеда,
        открытый - до текущего момента. Та же арифметика, что
        logic.days_by_status: на ней держатся простой и средний чек.
        """
        rows = await self.pool.fetch(
            """
            with s as (
              select bike_id, to_status, changed_at,
                     lead(changed_at) over (partition by bike_id order by changed_at, id)
                       as next_at
              from crm.bike_status_log
            )
            select to_status as status,
                   sum(extract(epoch from (least(coalesce(next_at, now()), $2::timestamptz)
                                           - greatest(changed_at, $1::timestamptz)))) / 86400
                     as days
            from s
            where changed_at < $2::timestamptz and coalesce(next_at, now()) > $1::timestamptz
            group by to_status
            """, since, until)
        return {r["status"]: Decimal(str(r["days"])) for r in rows if r["days"] and r["days"] > 0}

    async def rental_revenue(self, since: datetime, until: datetime) -> Decimal:
        """Арендная выручка за период - все платежи клиентов. Ремонт чужой
        техники сюда не попадает: он не в журнале клиентов."""
        return Decimal(await self.pool.fetchval(
            "select coalesce(sum(amount), 0) from crm.ledger "
            "where kind = 'payment' and created_at >= $1 and created_at < $2",
            since, until))

    # ─────────────────────── ремонт по узлам ───────────────────────

    async def repair_nodes(self) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select code, title from crm.repair_nodes order by sort, code"))

    async def create_repair(self, bike_id: int, *, items: list[dict], note: str | None,
                            created_by: str | None) -> int:
        """Ремонт: запись журнала велосипеда с общей суммой и позиции по
        узлам - одной транзакцией."""
        total = sum((Decimal(str(i.get("parts_cost") or 0)) + Decimal(str(i.get("labor_cost") or 0))
                     for i in items), Decimal(0))
        async with self.pool.acquire() as conn, conn.transaction():
            log_id = int(await conn.fetchval(
                "insert into crm.bike_log (bike_id, kind, note, cost, created_by) "
                "values ($1, 'repair', $2, $3, $4) returning id",
                bike_id, note, total, created_by))
            for i in items:
                await conn.execute(
                    """
                    insert into crm.repair_items
                      (log_id, bike_id, node, parts_cost, labor_cost, note)
                    values ($1, $2, $3, $4, $5, $6)
                    """, log_id, bike_id, i["node"],
                    Decimal(str(i.get("parts_cost") or 0)),
                    Decimal(str(i.get("labor_cost") or 0)), i.get("note"))
            return log_id

    async def repair_stats(self, since: datetime, until: datetime) -> dict[str, list[dict]]:
        """Что ломается и что дорого: по узлам (только структурные записи)
        и по моделям (все ремонты, включая старые без позиций)."""
        by_node = _rows(await self.pool.fetch(
            """
            select n.code, n.title, count(*) as n,
                   sum(i.parts_cost + i.labor_cost) as cost
            from crm.repair_items i join crm.repair_nodes n on n.code = i.node
            where i.created_at >= $1 and i.created_at < $2
            group by n.code, n.title, n.sort
            order by cost desc, n.sort
            """, since, until))
        by_model = _rows(await self.pool.fetch(
            """
            select b.model, count(*) as n, count(distinct b.id) as bikes,
                   sum(coalesce(l.cost, 0)) as cost
            from crm.bike_log l join crm.bikes b on b.id = l.bike_id
            where l.kind = 'repair' and l.created_at >= $1 and l.created_at < $2
            group by b.model
            order by cost desc
            """, since, until))
        return {"by_node": by_node, "by_model": by_model}

    async def bike_counts(self) -> dict[str, int]:
        rows = await self.pool.fetch(
            "select status, count(*) as n from crm.bikes group by status")
        return {r["status"]: int(r["n"]) for r in rows}

    async def bike_log(self, bike_id: int, limit: int = 50,
                       kind: str | None = None) -> list[dict]:
        if kind:
            return _rows(await self.pool.fetch(
                "select * from crm.bike_log where bike_id = $1 and kind = $3 "
                "order by id desc limit $2", bike_id, limit, kind))
        return _rows(await self.pool.fetch(
            "select * from crm.bike_log where bike_id = $1 order by id desc limit $2",
            bike_id, limit))

    async def add_bike_log(self, bike_id: int, kind: str, note: str | None,
                           cost: Decimal | None, created_by: str | None) -> int:
        return int(await self.pool.fetchval(
            "insert into crm.bike_log (bike_id, kind, note, cost, created_by) "
            "values ($1, $2, $3, $4, $5) returning id",
            bike_id, kind, note, cost, created_by))

    async def bike_rentals(self, bike_id: int, limit: int = 30) -> list[dict]:
        return _rows(await self.pool.fetch(
            """
            select r.*, c.full_name
            from crm.rentals r join crm.clients c on c.id = r.client_id
            where r.bike_id = $1 order by r.id desc limit $2
            """, bike_id, limit))

    # ─────────────────────── клиенты ───────────────────────

    async def clients(self, *, q: str | None = None, status: str | None = None,
                      limit: int = 500) -> list[dict]:
        """Список с балансом и активной арендой - одним запросом."""
        conds: list[str] = []
        args: list[Any] = []
        if status:
            args.append(status)
            conds.append(f"c.status = ${len(args)}")
        if q:
            args.append(f"%{q.strip()}%")
            text_cond = (f"c.full_name ilike ${len(args)} or c.phone ilike ${len(args)} "
                         f"or c.contract_no ilike ${len(args)} "
                         f"or c.username ilike ${len(args)}")
            # Телефон ищут как набрали: «900 111», «8 (900) 111-22-33».
            # Сравниваются только цифры, у восьмёрки отбрасывается код страны.
            digits = re.sub(r"\D", "", q)
            if len(digits) >= 3:
                if len(digits) == 11 and digits[0] in "78":
                    digits = digits[1:]
                args.append(f"%{digits}%")
                text_cond += f" or regexp_replace(c.phone, '\\D', '', 'g') like ${len(args)}"
            conds.append(f"({text_cond})")
        where = ("where " + " and ".join(conds)) if conds else ""
        args.append(limit)
        return _rows(await self.pool.fetch(
            f"""
            select c.*,
                   coalesce(l.balance, 0) as balance,
                   r.id as rental_id, r.billed_until, r.price, r.period_days,
                   r.tariff_name, b.code as bike_code, b.model as bike_model
            from crm.clients c
            left join (select client_id, sum(amount) as balance
                       from crm.ledger group by client_id) l on l.client_id = c.id
            left join crm.rentals r on r.client_id = c.id and r.status = 'active'
            left join crm.bikes b on b.id = r.bike_id
            {where}
            order by c.full_name
            limit ${len(args)}
            """, *args))

    async def client(self, client_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.clients where id = $1", client_id))

    async def client_by_tg(self, tg_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.clients where tg_id = $1", tg_id))

    async def client_by_phone(self, phone: str) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.clients where phone = $1", phone))

    async def create_client(self, *, full_name: str, phone: str,
                            tg_id: int | None = None, username: str | None = None,
                            note: str | None = None, source: str = "manual",
                            contract_no: str | None = None) -> int:
        return int(await self.pool.fetchval(
            """
            insert into crm.clients (full_name, phone, tg_id, username, note,
                                     source, contract_no)
            values ($1, $2, $3, $4, $5, $6, $7) returning id
            """, full_name, phone, tg_id, username, note, source, contract_no))

    async def update_client(self, client_id: int, **fields: Any) -> None:
        sets, values = _set_clause(fields, CLIENT_FIELDS, 2)
        await self.pool.execute(
            f"update crm.clients set {sets}, updated_at = now() where id = $1",
            client_id, *values)

    async def link_client_tg(self, client_id: int, tg_id: int,
                             username: str | None) -> bool:
        """Привязать Telegram к карточке. False - tg_id уже занят другой."""
        try:
            await self.pool.execute(
                "update crm.clients set tg_id = $2, username = $3, "
                "updated_at = now() where id = $1",
                client_id, tg_id, username)
        except asyncpg.UniqueViolationError:
            return False
        return True

    async def client_balance(self, client_id: int) -> Decimal:
        value = await self.pool.fetchval(
            "select coalesce(sum(amount), 0) from crm.ledger where client_id = $1",
            client_id)
        return Decimal(value or 0)

    async def client_rentals(self, client_id: int, limit: int = 30) -> list[dict]:
        return _rows(await self.pool.fetch(
            """
            select r.*, b.code as bike_code, b.model as bike_model
            from crm.rentals r left join crm.bikes b on b.id = r.bike_id
            where r.client_id = $1 order by r.id desc limit $2
            """, client_id, limit))

    # ─────────────────────── аренды ───────────────────────

    _RENTAL_SELECT = """
        select r.*, c.full_name, c.phone, c.tg_id, c.status as client_status,
               b.code as bike_code, b.model as bike_model,
               coalesce(l.balance, 0) as balance
        from crm.rentals r
        join crm.clients c on c.id = r.client_id
        left join crm.bikes b on b.id = r.bike_id
        left join (select client_id, sum(amount) as balance
                   from crm.ledger group by client_id) l on l.client_id = c.id
    """

    async def rentals(self, *, status: str | None = None, limit: int = 500) -> list[dict]:
        args: list[Any] = []
        where = ""
        if status:
            args.append(status)
            where = f"where r.status = ${len(args)}"
        args.append(limit)
        return _rows(await self.pool.fetch(
            f"{self._RENTAL_SELECT} {where} order by r.status = 'active' desc, "
            f"r.id desc limit ${len(args)}", *args))

    async def rental(self, rental_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._RENTAL_SELECT} where r.id = $1", rental_id))

    async def active_rental_of(self, client_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._RENTAL_SELECT} where r.client_id = $1 and r.status = 'active'",
            client_id))

    async def active_rentals(self) -> list[dict]:
        """Все идущие аренды с балансом - для биллинга, сводки и дашборда."""
        return _rows(await self.pool.fetch(
            f"{self._RENTAL_SELECT} where r.status = 'active' order by r.id"))

    async def create_rental(self, *, client_id: int, bike_id: int | None,
                            tariff_id: int | None, tariff_name: str,
                            period_days: int, price: Decimal, billing: str,
                            started_on: date, contract_no: str | None,
                            created_by: str | None,
                            mileage_start: int | None = None,
                            base_price: Decimal | None = None) -> int:
        """Аренда и статус велосипеда - одной транзакцией.

        `price` - цена периода целиком, вместе с позициями; `base_price` -
        цена одного велосипеда. По первой идёт начисление, по второй
        пересчёт, когда позицию снимают.

        Уникальные индексы на активную аренду клиента и велосипеда бросают
        UniqueViolationError; вызывающий переводит его в понятное сообщение.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('crm.actor', $1, true)", created_by or "")
            rental_id = int(await conn.fetchval(
                """
                insert into crm.rentals
                  (client_id, bike_id, tariff_id, tariff_name, period_days, price,
                   base_price, billing, started_on, billed_until, contract_no,
                   created_by, mileage_start)
                values ($1, $2, $3, $4, $5, $6, $12, $7, $8, $8, $9, $10, $11)
                returning id
                """, client_id, bike_id, tariff_id, tariff_name, period_days,
                price, billing, started_on, contract_no, created_by, mileage_start,
                base_price if base_price is not None else price))
            if bike_id is not None:
                # greatest: пробег велосипеда не уменьшается никогда, даже
                # если аренду задним числом оформили с меньшим числом.
                await conn.execute(
                    "update crm.bikes set status = 'rented', "
                    "mileage_km = greatest(mileage_km, coalesce($2, mileage_km)), "
                    "updated_at = now() where id = $1", bike_id, mileage_start)
            return rental_id

    async def update_rental(self, rental_id: int, **fields: Any) -> None:
        sets, values = _set_clause(fields, RENTAL_FIELDS, 2)
        await self.pool.execute(
            f"update crm.rentals set {sets}, updated_at = now() where id = $1",
            rental_id, *values)

    async def close_rental(self, rental_id: int, *, closed_on: date,
                           note: str | None, bike_status: str = "available",
                           closed_by: str | None = None,
                           mileage_end: int | None = None) -> bool:
        """Закрыть аренду и освободить велосипед. False - уже закрыта.

        Пробег возврата пишется в ту же транзакцию, что и статус велосипеда:
        иначе одометр парка и «накатал» у аренды разъезжались бы при сбое
        между двумя запросами.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('crm.actor', $1, true)", closed_by or "")
            row = await conn.fetchrow(
                """
                update crm.rentals
                   set status = 'closed', closed_on = $2, close_note = $3,
                       mileage_end = coalesce($4, mileage_end), updated_at = now()
                 where id = $1 and status = 'active'
                returning bike_id
                """, rental_id, closed_on, note, mileage_end)
            if row is None:
                return False
            if row["bike_id"] is not None:
                await conn.execute(
                    "update crm.bikes set status = $2, "
                    "mileage_km = greatest(mileage_km, coalesce($3, mileage_km)), "
                    "updated_at = now() where id = $1 and status = 'rented'",
                    row["bike_id"], bike_status, mileage_end)
            # Позиции закрываются вместе с арендой: доп. аккумулятор
            # вернулся на склад, и висеть действующим ему незачем.
            await conn.execute(
                "update crm.rental_extras set removed_at = now(), removed_by = $2 "
                "where rental_id = $1 and removed_at is null",
                rental_id, closed_by)
            return True

    async def charge_period(self, rental_id: int, client_id: int, *,
                            period_from: date, period_to: date, amount: Decimal,
                            note: str, created_by: str = "billing",
                            created_at: datetime | None = None) -> bool:
        """Начислить период и сдвинуть billed_until - одной транзакцией.

        Повтор того же периода (второй проход, ручной запуск) упирается
        в уникальный индекс и возвращает False, ничего не списав.
        created_at задаёт только импорт: записи из таблицы датируются
        днём выдачи, а не днём загрузки, чтобы не раздувать текущий месяц.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            try:
                await conn.execute(
                    """
                    insert into crm.ledger
                      (client_id, rental_id, kind, amount, period_from, period_to,
                       note, created_by, created_at)
                    values ($1, $2, 'charge', $3, $4, $5, $6, $7, coalesce($8, now()))
                    """, client_id, rental_id, amount, period_from, period_to,
                    note, created_by, created_at)
            except asyncpg.UniqueViolationError:
                return False
            await conn.execute(
                "update crm.rentals set billed_until = greatest(billed_until, $2), "
                "updated_at = now() where id = $1", rental_id, period_to)
            return True

    async def mark_notified(self, rental_id: int, today: date, kind: str) -> None:
        await self.pool.execute(
            "update crm.rentals set notified_on = $2, notified_kind = $3 where id = $1",
            rental_id, today, kind)

    # ─────────────────────── журнал ───────────────────────

    async def add_ledger(self, *, client_id: int, kind: str, amount: Decimal,
                         rental_id: int | None = None, method: str | None = None,
                         note: str | None = None, created_by: str | None = None,
                         period_from: date | None = None,
                         period_to: date | None = None,
                         created_at: datetime | None = None) -> int:
        return int(await self.pool.fetchval(
            """
            insert into crm.ledger (client_id, rental_id, kind, amount, method,
                                    note, created_by, period_from, period_to, created_at)
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, coalesce($10, now()))
            returning id
            """, client_id, rental_id, kind, amount, method, note, created_by,
            period_from, period_to, created_at))

    async def ledger_of(self, client_id: int, limit: int = 100) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select * from crm.ledger where client_id = $1 order by id desc limit $2",
            client_id, limit))

    async def ledger(self, *, since: date | None = None, until: date | None = None,
                     kind: str | None = None, limit: int = 1000) -> list[dict]:
        conds: list[str] = []
        args: list[Any] = []
        if since:
            args.append(since)
            conds.append(f"l.created_at >= ${len(args)}::date")
        if until:
            args.append(until)
            conds.append(f"l.created_at < (${len(args)}::date + interval '1 day')")
        if kind:
            args.append(kind)
            conds.append(f"l.kind = ${len(args)}")
        where = ("where " + " and ".join(conds)) if conds else ""
        args.append(limit)
        return _rows(await self.pool.fetch(
            f"""
            select l.*, c.full_name
            from crm.ledger l join crm.clients c on c.id = l.client_id
            {where} order by l.id desc limit ${len(args)}
            """, *args))

    async def ledger_totals(self, *, since: date | None = None,
                            until: date | None = None) -> dict[str, Decimal]:
        conds: list[str] = []
        args: list[Any] = []
        if since:
            args.append(since)
            conds.append(f"created_at >= ${len(args)}::date")
        if until:
            args.append(until)
            conds.append(f"created_at < (${len(args)}::date + interval '1 day')")
        where = ("where " + " and ".join(conds)) if conds else ""
        rows = await self.pool.fetch(
            f"select kind, sum(amount) as total from crm.ledger {where} group by kind",
            *args)
        return {r["kind"]: Decimal(r["total"] or 0) for r in rows}

    async def revenue_by_month(self, months: int = 12) -> list[dict]:
        """Платежи и начисления по месяцам: сколько пришло и сколько
        заработано. Расходы на ремонт - из журнала велосипедов."""
        return _rows(await self.pool.fetch(
            """
            with m as (
              select date_trunc('month', created_at)::date as month,
                     sum(amount) filter (where kind = 'payment') as paid,
                     -sum(amount) filter (where kind in ('charge', 'fine')) as charged,
                     -sum(amount) filter (where kind = 'refund') as refunded
              from crm.ledger
              where created_at >= date_trunc('month', now()) - ($1 || ' months')::interval
              group by 1
            ), rep as (
              select date_trunc('month', created_at)::date as month,
                     sum(cost) as repairs
              from crm.bike_log
              where created_at >= date_trunc('month', now()) - ($1 || ' months')::interval
              group by 1
            )
            select coalesce(m.month, rep.month) as month,
                   coalesce(m.paid, 0) as paid, coalesce(m.charged, 0) as charged,
                   coalesce(m.refunded, 0) as refunded,
                   coalesce(rep.repairs, 0) as repairs
            from m full join rep on rep.month = m.month
            order by 1 desc
            """, str(months)))

    async def debtors(self, limit: int = 50) -> list[dict]:
        return _rows(await self.pool.fetch(
            """
            select c.id, c.full_name, c.phone, c.status, sum(l.amount) as balance
            from crm.ledger l join crm.clients c on c.id = l.client_id
            group by c.id having sum(l.amount) < 0
            order by sum(l.amount) limit $1
            """, limit))

    async def counts(self) -> dict[str, int]:
        row = await self.pool.fetchrow(
            """
            select (select count(*) from crm.clients) as clients,
                   (select count(*) from crm.rentals where status = 'active') as rentals,
                   (select count(*) from crm.payment_claims where status = 'pending') as claims,
                   (select count(*) from crm.bikes) as bikes
            """)
        return {k: int(v or 0) for k, v in dict(row).items()}

    # ─────────────────────── заявки на оплату ───────────────────────

    async def create_claim(self, client_id: int, amount_hint: Decimal | None) -> int:
        return int(await self.pool.fetchval(
            "insert into crm.payment_claims (client_id, amount_hint) "
            "values ($1, $2) returning id", client_id, amount_hint))

    _CLAIM_SELECT = """
        select p.*, c.full_name, c.phone, c.tg_id
        from crm.payment_claims p join crm.clients c on c.id = p.client_id
    """

    async def claim(self, claim_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._CLAIM_SELECT} where p.id = $1", claim_id))

    async def pending_claims(self) -> list[dict]:
        return _rows(await self.pool.fetch(
            f"{self._CLAIM_SELECT} where p.status = 'pending' order by p.id"))

    async def pending_claim_of(self, client_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._CLAIM_SELECT} where p.client_id = $1 and p.status = 'pending' "
            f"order by p.id desc limit 1", client_id))

    async def claim_by_card(self, chat_id: int, message_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._CLAIM_SELECT} where p.card_chat_id = $1 and p.card_message_id = $2",
            chat_id, message_id))

    async def set_claim_card(self, claim_id: int, chat_id: int, message_id: int) -> None:
        await self.pool.execute(
            "update crm.payment_claims set card_chat_id = $2, card_message_id = $3 "
            "where id = $1", claim_id, chat_id, message_id)

    async def set_claim_receipt(self, claim_id: int, file_id: str, is_photo: bool) -> None:
        await self.pool.execute(
            "update crm.payment_claims set receipt_file_id = $2, receipt_is_photo = $3 "
            "where id = $1", claim_id, file_id, is_photo)

    async def credit_claim(self, claim_id: int, *, client_id: int, amount: Decimal,
                           method: str, note: str, created_by: str) -> int | None:
        """Зачислить заявку: закрыть её и записать платёж одной транзакцией.

        None - заявку уже закрыл кто-то другой (двойной тап, панель и
        Telegram одновременно); тогда в журнал не пишется ничего.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                update crm.payment_claims
                   set status = 'confirmed', resolved_by = $2, resolved_at = now()
                 where id = $1 and status = 'pending'
                returning id
                """, claim_id, created_by)
            if row is None:
                return None
            ledger_id = int(await conn.fetchval(
                """
                insert into crm.ledger (client_id, kind, amount, method, note, created_by)
                values ($1, 'payment', $2, $3, $4, $5) returning id
                """, client_id, amount, method, note, created_by))
            await conn.execute(
                "update crm.payment_claims set ledger_id = $2 where id = $1",
                claim_id, ledger_id)
            return ledger_id

    async def resolve_claim(self, claim_id: int, *, status: str, resolved_by: str,
                            ledger_id: int | None = None) -> bool:
        """Закрыть заявку. False - её уже закрыл кто-то другой (двойной тап,
        подтверждение в панели и в Telegram одновременно)."""
        row = await self.pool.fetchrow(
            """
            update crm.payment_claims
               set status = $2, resolved_by = $3, ledger_id = $4, resolved_at = now()
             where id = $1 and status = 'pending'
            returning id
            """, claim_id, status, resolved_by, ledger_id)
        return row is not None

    # ─────────────────────── сервис: виды работ ───────────────────────

    async def work_types(self, *, active_only: bool = False) -> list[dict]:
        """Каталог работ со счётчиком использований: по нему видно, какие
        позиции живые, а какие завели и забыли."""
        where = "where t.active" if active_only else ""
        return _rows(await self.pool.fetch(
            f"""
            select t.*, count(i.id) as used
            from crm.work_types t
            left join crm.work_order_items i on i.work_type_id = t.id
            {where}
            group by t.id
            order by t.active desc, t.sort, t.title
            """))

    async def work_type(self, type_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.work_types where id = $1", type_id))

    async def create_work_type(self, *, title: str, category: str, minutes: int,
                               price: Decimal, node: str | None) -> int:
        return int(await self.pool.fetchval(
            """
            insert into crm.work_types (title, category, minutes, price, node)
            values ($1, $2, $3, $4, $5) returning id
            """, title, category, minutes, price, node))

    async def update_work_type(self, type_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets, values = _set_clause(fields, WORK_TYPE_FIELDS, 2)
        await self.pool.execute(
            f"update crm.work_types set {sets} where id = $1", type_id, *values)

    # ─────────────────────── сервис: наряды ───────────────────────

    _ORDER_SELECT = """
        select o.*, b.code as bike_code, b.model as bike_model, b.status as bike_status,
               c.full_name as client_name, c.phone as client_phone,
               s.name as tech_name, s.login as tech_login
        from crm.work_orders o
        left join crm.bikes b on b.id = o.bike_id
        left join crm.clients c on c.id = o.client_id
        left join crm.staff s on s.id = o.tech_id
    """

    async def work_orders(self, *, status: str | None = None, payer: str | None = None,
                          tech_id: int | None = None, bike_id: int | None = None,
                          open_only: bool = False, limit: int = 300) -> list[dict]:
        where, values = [], []
        if status:
            values.append(status)
            where.append(f"o.status = ${len(values)}")
        if payer:
            values.append(payer)
            where.append(f"o.payer = ${len(values)}")
        if tech_id:
            values.append(tech_id)
            where.append(f"o.tech_id = ${len(values)}")
        if bike_id:
            values.append(bike_id)
            where.append(f"o.bike_id = ${len(values)}")
        if open_only:
            where.append("o.status in ('new', 'in_work', 'waiting')")
        values.append(limit)
        clause = ("where " + " and ".join(where)) if where else ""
        return _rows(await self.pool.fetch(
            f"{self._ORDER_SELECT} {clause} order by o.opened_at desc, o.id desc "
            f"limit ${len(values)}", *values))

    async def work_order(self, order_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._ORDER_SELECT} where o.id = $1", order_id))

    async def open_order_of(self, bike_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._ORDER_SELECT} where o.bike_id = $1 "
            "and o.status in ('new', 'in_work', 'waiting')", bike_id))

    async def open_orders_by_bike(self) -> dict[int, dict]:
        """Открытые наряды разом: рабочему столу нужен наряд у каждой
        строки, и запрос на велосипед превратил бы экран в сотню запросов."""
        rows = _rows(await self.pool.fetch(
            f"{self._ORDER_SELECT} where o.bike_id is not null "
            "and o.status in ('new', 'in_work', 'waiting')"))
        return {int(r["bike_id"]): r for r in rows}

    async def create_work_order(self, *, bike_id: int | None, payer: str,
                                client_id: int | None, complaint: str | None,
                                object_note: str | None, tech_id: int | None,
                                estimate: Decimal, created_by: str) -> int:
        """Наряд с человекочитаемым номером. Номер берётся из счётчика
        самой таблицы в той же транзакции: две одновременные кнопки
        «открыть наряд» не должны получить один и тот же РЕМ-."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("lock table crm.work_orders in share row exclusive mode")
            next_no = int(await conn.fetchval(
                "select coalesce(max(substring(no from '[0-9]+$')::bigint), 0) + 1 "
                "from crm.work_orders") or 1)
            return int(await conn.fetchval(
                """
                insert into crm.work_orders
                    (no, bike_id, payer, client_id, complaint, object_note,
                     tech_id, estimate, created_by)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9) returning id
                """, logic.order_no(next_no), bike_id, payer, client_id, complaint,
                object_note, tech_id, estimate, created_by))

    async def update_work_order(self, order_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets, values = _set_clause(fields, ORDER_FIELDS, 2)
        await self.pool.execute(
            f"update crm.work_orders set {sets} where id = $1", order_id, *values)

    async def order_items(self, order_id: int) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select * from crm.work_order_items where order_id = $1 order by id",
            order_id))

    async def add_order_item(self, order_id: int, *, title: str, node: str | None,
                             work_type_id: int | None, qty: int, price: Decimal,
                             parts_cost: Decimal, labor_cost: Decimal,
                             note: str | None = None) -> int:
        return int(await self.pool.fetchval(
            """
            insert into crm.work_order_items
                (order_id, work_type_id, title, node, qty, price,
                 parts_cost, labor_cost, note)
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9) returning id
            """, order_id, work_type_id, title, node, qty, price,
            parts_cost, labor_cost, note))

    async def delete_order_item(self, order_id: int, item_id: int) -> bool:
        row = await self.pool.fetchrow(
            "delete from crm.work_order_items where id = $1 and order_id = $2 "
            "returning id", item_id, order_id)
        return row is not None

    async def order_stats(self, since: datetime, until: datetime) -> dict[str, Any]:
        """Итоги сервиса за период: сколько закрыто и сколько заработано
        на чужой технике. Выручка чужого ремонта считается отдельно от
        аренды - в crm.ledger она не попадает намеренно."""
        row = await self.pool.fetchrow(
            """
            select count(*) as closed,
                   count(*) filter (where payer = 'client') as client_orders,
                   coalesce(sum(total) filter (where payer = 'client'), 0) as revenue,
                   coalesce(sum(cost), 0) as cost
            from crm.work_orders
            where status = 'done' and closed_at >= $1 and closed_at < $2
            """, since, until)
        return dict(row) if row else {}

    # ─────────────────────── пересчёт техники ───────────────────────

    async def stock_takes(self, *, limit: int = 100) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select * from crm.stock_takes order by started_at desc, id desc limit $1",
            limit))

    async def stock_take(self, take_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.stock_takes where id = $1", take_id))

    async def open_stock_take(self) -> dict | None:
        """Текущая ведомость. Она одна: индекс не даёт открыть вторую."""
        return _row(await self.pool.fetchrow(
            "select * from crm.stock_takes where status = 'open' "
            "order by id desc limit 1"))

    async def create_stock_take(self, *, scope: str, location: str | None,
                                note: str | None, bike_ids: list[int],
                                created_by: str, what: str = "bikes",
                                battery_ids: list[int] | None = None) -> int:
        """Открыть ведомость и сразу записать в неё снимок ожидаемого парка.

        Номер и строки - одной транзакцией: ведомость без строк оператор
        примет за «всё сошлось», а это просто недописанный документ.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("lock table crm.stock_takes in share row exclusive mode")
            next_no = int(await conn.fetchval(
                "select coalesce(max(substring(no from '[0-9]+$')::bigint), 0) + 1 "
                "from crm.stock_takes") or 1)
            battery_ids = list(battery_ids or [])
            take_id = int(await conn.fetchval(
                """
                insert into crm.stock_takes (no, scope, location, note, expected,
                                             created_by, what)
                values ($1, $2, $3, $4, $5, $6, $7) returning id
                """, logic.take_no(next_no), scope, location, note,
                len(bike_ids) + len(battery_ids), created_by, what))
            if bike_ids:
                await conn.executemany(
                    "insert into crm.stock_take_items (take_id, bike_id, state) "
                    "values ($1, $2, 'expected')",
                    [(take_id, bike_id) for bike_id in bike_ids])
            if battery_ids:
                await conn.executemany(
                    "insert into crm.stock_take_items (take_id, battery_id, state) "
                    "values ($1, $2, 'expected')",
                    [(take_id, battery_id) for battery_id in battery_ids])
            return take_id

    async def update_stock_take(self, take_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets, values = _set_clause(fields, TAKE_FIELDS, 2)
        await self.pool.execute(
            f"update crm.stock_takes set {sets} where id = $1", take_id, *values)

    _TAKE_ITEM_SELECT = """
        select i.*, b.code as bike_code, b.model as bike_model,
               b.status as bike_status, b.location as bike_location,
               a.code as battery_code, m.title as battery_model,
               a.status as battery_status, a.location as battery_location
        from crm.stock_take_items i
        left join crm.bikes b on b.id = i.bike_id
        left join crm.batteries a on a.id = i.battery_id
        left join crm.battery_models m on m.id = a.model_id
    """

    async def take_items(self, take_id: int) -> list[dict]:
        return _rows(await self.pool.fetch(
            f"{self._TAKE_ITEM_SELECT} where i.take_id = $1 "
            "order by coalesce(b.code, a.code, i.code), i.id", take_id))

    async def take_item_of_battery(self, take_id: int,
                                   battery_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._TAKE_ITEM_SELECT} where i.take_id = $1 and i.battery_id = $2",
            take_id, battery_id))

    async def take_item(self, take_id: int, item_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._TAKE_ITEM_SELECT} where i.take_id = $1 and i.id = $2",
            take_id, item_id))

    async def take_item_of_bike(self, take_id: int, bike_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._TAKE_ITEM_SELECT} where i.take_id = $1 and i.bike_id = $2",
            take_id, bike_id))

    async def set_take_item(self, take_id: int, item_id: int, *, state: str,
                            note: str | None = None) -> bool:
        row = await self.pool.fetchrow(
            "update crm.stock_take_items set state = $3, note = coalesce($4, note) "
            "where take_id = $1 and id = $2 returning id",
            take_id, item_id, state, note)
        return row is not None

    async def mark_take_all(self, take_id: int, *, state: str) -> int:
        """Кнопка «всё на месте» и обратная ей: одним запросом по ведомости."""
        source = "found" if state == "expected" else "expected"
        return int(await self.pool.fetchval(
            "with upd as (update crm.stock_take_items set state = $2 "
            "where take_id = $1 and state = $3 returning 1) "
            "select count(*) from upd", take_id, state, source) or 0)

    async def add_take_item(self, take_id: int, *, bike_id: int | None, code: str | None,
                            state: str = "extra", note: str | None = None,
                            battery_id: int | None = None) -> int:
        return int(await self.pool.fetchval(
            "insert into crm.stock_take_items (take_id, bike_id, battery_id, code, "
            "state, note) values ($1, $2, $6, $3, $4, $5) returning id",
            take_id, bike_id, code, state, note, battery_id))

    async def delete_take_item(self, take_id: int, item_id: int) -> bool:
        row = await self.pool.fetchrow(
            "delete from crm.stock_take_items where take_id = $1 and id = $2 "
            "returning id", take_id, item_id)
        return row is not None

    async def close_stock_take(self, take_id: int, *, counts: dict[str, int],
                               closed_at: datetime) -> dict[str, list[int]]:
        """Закрыть ведомость: неотмеченное становится недостачей.

        Возвращает id ненайденных - велосипедов и батарей отдельно:
        что с ними делать, решает не база.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "update crm.stock_take_items set state = 'missing' "
                "where take_id = $1 and state = 'expected' "
                "returning bike_id, battery_id", take_id)
            await conn.execute(
                """
                update crm.stock_takes
                set status = 'done', closed_at = $2, expected = $3, found = $4,
                    missing = $5, extra = $6
                where id = $1
                """, take_id, closed_at, int(counts.get("total") or 0),
                int(counts.get("found") or 0), int(counts.get("missing") or 0),
                int(counts.get("extra") or 0))
            return {
                "bikes": [int(r["bike_id"]) for r in rows
                          if r["bike_id"] is not None],
                "batteries": [int(r["battery_id"]) for r in rows
                              if r["battery_id"] is not None],
            }

    # ─────────────────────── окупаемость по моделям ───────────────────────

    async def model_money(self, since: datetime, until: datetime) -> dict[str, dict]:
        """Деньги, ремонт и дни аренды за период - в разрезе модели.

        Платёж привязывается к модели через аренду. У части платежей
        `rental_id` пуст (зачисление по заявке клиента из бота), поэтому
        для них берётся аренда клиента, шедшая в день платежа: иначе
        выручка модели просела бы ровно на самый частый способ оплаты.
        Если в тот день аренды не было - предоплата за велосипед, который
        ещё не выдали, или доплата после возврата, - берётся ближайшая
        по времени аренда того же клиента.
        """
        money = _rows(await self.pool.fetch(
            """
            with attr as (
              select l.kind, l.amount,
                     coalesce(l.rental_id, (
                       select r.id from crm.rentals r
                       where r.client_id = l.client_id
                         and r.started_on <= l.created_at::date
                         and (r.closed_on is null or r.closed_on >= l.created_at::date)
                       order by r.id desc limit 1), (
                       select r.id from crm.rentals r
                       where r.client_id = l.client_id
                       order by abs(r.started_on - l.created_at::date), r.id
                       limit 1)) as rental_id
              from crm.ledger l
              where l.created_at >= $1 and l.created_at < $2
            )
            select b.model,
                   coalesce(sum(a.amount) filter (where a.kind = 'payment'), 0) as paid,
                   coalesce(-sum(a.amount) filter (where a.kind in ('charge', 'fine')), 0)
                     as charged
            from attr a
            join crm.rentals r on r.id = a.rental_id
            join crm.bikes b on b.id = r.bike_id
            group by b.model
            """, since, until))
        repairs = _rows(await self.pool.fetch(
            """
            select b.model, coalesce(sum(l.cost), 0) as repair_cost
            from crm.bike_log l join crm.bikes b on b.id = l.bike_id
            where l.kind = 'repair' and l.created_at >= $1 and l.created_at < $2
            group by b.model
            """, since, until))
        works = _rows(await self.pool.fetch(
            """
            select b.model, coalesce(sum(o.total), 0) as works
            from crm.work_orders o join crm.bikes b on b.id = o.bike_id
            where o.payer = 'client' and o.status = 'done'
              and o.closed_at >= $1 and o.closed_at < $2
            group by b.model
            """, since, until))
        rented = _rows(await self.pool.fetch(
            """
            with s as (
              select l.bike_id, l.to_status, l.changed_at,
                     lead(l.changed_at) over (partition by l.bike_id
                                              order by l.changed_at, l.id) as next_at
              from crm.bike_status_log l
            )
            select b.model,
                   sum(extract(epoch from (least(coalesce(s.next_at, now()), $2::timestamptz)
                                           - greatest(s.changed_at, $1::timestamptz)))) / 86400
                     as rented_days
            from s join crm.bikes b on b.id = s.bike_id
            where s.to_status = 'rented'
              and s.changed_at < $2::timestamptz
              and coalesce(s.next_at, now()) > $1::timestamptz
            group by b.model
            """, since, until))
        out: dict[str, dict] = {}
        for rows, keys in ((money, ("paid", "charged")), (repairs, ("repair_cost",)),
                           (works, ("works",)), (rented, ("rented_days",))):
            for row in rows:
                cell = out.setdefault(row["model"], {})
                for key in keys:
                    cell[key] = row[key]
        return out

    # ─────────────────────── настройки ───────────────────────

    async def settings(self) -> dict[str, str]:
        rows = await self.pool.fetch("select key, value from crm.settings")
        return {r["key"]: r["value"] for r in rows}

    async def set_setting(self, key: str, value: str, *, by: str) -> None:
        await self.pool.execute(
            """
            insert into crm.settings (key, value, updated_by) values ($1, $2, $3)
            on conflict (key) do update
              set value = excluded.value, updated_by = excluded.updated_by,
                  updated_at = now()
            """, key, value, by)

    # ─────────────────────── реферальная программа ───────────────────────

    async def client_by_ref_code(self, code: str) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.clients where ref_code = $1", code))

    async def set_ref_code(self, client_id: int, code: str) -> bool:
        """Закрепить код за клиентом. False - код уже занят, зовите снова."""
        try:
            await self.pool.execute(
                "update crm.clients set ref_code = $2, updated_at = now() "
                "where id = $1", client_id, code)
        except asyncpg.UniqueViolationError:
            return False
        return True

    _REFERRAL_SELECT = """
        select r.*, a.full_name as agent_name, a.phone as agent_phone,
               a.ref_code as ref_code, f.full_name as friend_name,
               f.phone as friend_phone
        from crm.referrals r
        join crm.clients a on a.id = r.agent_id
        left join crm.clients f on f.id = r.client_id
    """

    async def referrals(self, *, agent_id: int | None = None,
                        since: datetime | None = None, until: datetime | None = None,
                        limit: int = 1000) -> list[dict]:
        conds, args = [], []
        if agent_id:
            args.append(agent_id)
            conds.append(f"r.agent_id = ${len(args)}")
        if since is not None:
            args.append(since)
            conds.append(f"r.created_at >= ${len(args)}")
        if until is not None:
            args.append(until)
            conds.append(f"r.created_at < ${len(args)}")
        where = ("where " + " and ".join(conds)) if conds else ""
        args.append(limit)
        return _rows(await self.pool.fetch(
            f"{self._REFERRAL_SELECT} {where} order by r.created_at desc, r.id desc "
            f"limit ${len(args)}", *args))

    async def referral_of_tg(self, tg_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._REFERRAL_SELECT} where r.tg_id = $1", tg_id))

    async def referral_of_client(self, client_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._REFERRAL_SELECT} where r.client_id = $1", client_id))

    async def add_referral(self, *, agent_id: int, tg_id: int) -> int | None:
        """Записать переход. None - этот человек уже за кем-то закреплён."""
        try:
            return int(await self.pool.fetchval(
                "insert into crm.referrals (agent_id, tg_id) values ($1, $2) "
                "returning id", agent_id, tg_id))
        except asyncpg.UniqueViolationError:
            return None

    async def update_referral(self, ref_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets, values = _set_clause(fields, REFERRAL_FIELDS, 2)
        await self.pool.execute(
            f"update crm.referrals set {sets} where id = $1", ref_id, *values)

    async def pay_referral_bonus(self, ref_id: int, *, agent_id: int, amount: Decimal,
                                 note: str, created_by: str) -> int | None:
        """Начислить бонус агенту и отметить друга оплатившим - одной
        транзакцией. None - бонус по этому другу уже платили.

        Вид записи - adjust, а не payment: платежи клиентов формируют
        средний чек парка, и бонус завысил бы его на деньги, которых
        никто не вносил.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "update crm.referrals set status = 'paid', paid_at = now(), "
                "bonus = $2 where id = $1 and status <> 'paid' returning id", ref_id,
                amount)
            if row is None:
                return None
            ledger_id = int(await conn.fetchval(
                "insert into crm.ledger (client_id, kind, amount, note, created_by) "
                "values ($1, 'adjust', $2, $3, $4) returning id",
                agent_id, amount, note, created_by))
            await conn.execute("update crm.referrals set ledger_id = $2 where id = $1",
                               ref_id, ledger_id)
            return ledger_id

    # ─────────────────── сотрудник и его Telegram ───────────────────

    async def staff_by_tg(self, tg_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._STAFF_SELECT} where s.tg_id = $1", tg_id))

    async def staff_by_link_code(self, code: str) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._STAFF_SELECT} where s.link_code = $1", code))

    async def set_staff_link_code(self, staff_id: int, code: str | None) -> bool:
        try:
            await self.pool.execute(
                "update crm.staff set link_code = $2 where id = $1", staff_id, code)
        except asyncpg.UniqueViolationError:
            return False
        return True

    async def link_staff_tg(self, staff_id: int, tg_id: int,
                            username: str | None) -> bool:
        """Привязать Telegram сотруднику и погасить код. False - этот
        Telegram уже закреплён за другим сотрудником."""
        try:
            await self.pool.execute(
                "update crm.staff set tg_id = $2, tg_username = $3, "
                "link_code = null, linked_at = now() where id = $1",
                staff_id, tg_id, username)
        except asyncpg.UniqueViolationError:
            return False
        return True

    async def unlink_staff_tg(self, staff_id: int) -> None:
        await self.pool.execute(
            "update crm.staff set tg_id = null, tg_username = null, "
            "link_code = null, linked_at = null where id = $1", staff_id)

    async def clients_since(self, since: datetime) -> list[dict]:
        """Карточки, заведённые с даты: отчёту по каналам нужны только
        дата и канал, а не балансы всей базы."""
        return _rows(await self.pool.fetch(
            "select id, full_name, channel, source, created_at from crm.clients "
            "where created_at >= $1 order by created_at", since))

    # ───────────────────────── склад: поставщики ─────────────────────────

    async def suppliers(self, *, active_only: bool = False) -> list[dict]:
        where = "where s.active" if active_only else ""
        return _rows(await self.pool.fetch(
            f"""
            select s.*, count(d.id) as receipts,
                   coalesce(sum(d.total), 0) as spent,
                   max(d.created_at) as last_at
            from crm.suppliers s
            left join crm.part_docs d on d.supplier_id = s.id and d.kind = 'receipt'
            {where}
            group by s.id
            order by s.active desc, s.name
            """))

    async def supplier(self, supplier_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.suppliers where id = $1", supplier_id))

    async def create_supplier(self, *, name: str, phone: str | None,
                              note: str | None) -> int:
        return int(await self.pool.fetchval(
            "insert into crm.suppliers (name, phone, note) values ($1, $2, $3) "
            "returning id", name, phone, note))

    async def update_supplier(self, supplier_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets, values = _set_clause(fields, SUPPLIER_FIELDS, 2)
        await self.pool.execute(
            f"update crm.suppliers set {sets} where id = $1", supplier_id, *values)

    # ───────────────────────── склад: номенклатура ─────────────────────────

    _PART_SELECT = """
        select p.*, n.title as node_title
        from crm.parts p
        left join crm.repair_nodes n on n.code = p.node
    """

    async def parts(self, *, active_only: bool = False, node: str | None = None,
                    q: str | None = None) -> list[dict]:
        conds, args = [], []
        if active_only:
            conds.append("p.active")
        if node:
            args.append(node)
            conds.append(f"p.node = ${len(args)}")
        if q:
            args.append(f"%{q.strip()}%")
            conds.append(f"(p.title ilike ${len(args)} or p.model ilike ${len(args)})")
        where = ("where " + " and ".join(conds)) if conds else ""
        return _rows(await self.pool.fetch(
            f"{self._PART_SELECT} {where} order by p.title", *args))

    async def part(self, part_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._PART_SELECT} where p.id = $1", part_id))

    async def part_by_title(self, title: str) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.parts where lower(title) = lower($1)", title))

    async def create_part(self, *, title: str, node: str | None, unit: str,
                          cost: Decimal, price: Decimal, min_stock: int,
                          model: str | None, note: str | None) -> int:
        return int(await self.pool.fetchval(
            """
            insert into crm.parts (title, node, unit, cost, price, min_stock,
                                   model, note)
            values ($1, $2, $3, $4, $5, $6, $7, $8) returning id
            """, title, node, unit, cost, price, min_stock, model, note))

    async def update_part(self, part_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets, values = _set_clause(fields, PART_FIELDS, 2)
        await self.pool.execute(
            f"update crm.parts set {sets} where id = $1", part_id, *values)

    async def stock_map(self) -> dict[int, int]:
        """Остатки всех позиций разом: экран остатков - это весь склад,
        и запрос на позицию превратил бы его в сотню запросов."""
        rows = await self.pool.fetch(
            "select part_id, sum(qty) as stock from crm.part_moves group by part_id")
        return {int(r["part_id"]): int(r["stock"] or 0) for r in rows}

    async def part_stock(self, part_id: int) -> int:
        return int(await self.pool.fetchval(
            "select coalesce(sum(qty), 0) from crm.part_moves where part_id = $1",
            part_id) or 0)

    # ───────────────────────── склад: движения ─────────────────────────

    _MOVE_SELECT = """
        select m.*, p.title as part_title, p.unit as part_unit,
               d.no as doc_no, d.kind as doc_kind, o.no as order_no
        from crm.part_moves m
        join crm.parts p on p.id = m.part_id
        left join crm.part_docs d on d.id = m.doc_id
        left join crm.work_orders o on o.id = m.order_id
    """

    async def part_moves(self, *, part_id: int | None = None, kind: str | None = None,
                         order_id: int | None = None, limit: int = 300) -> list[dict]:
        conds, args = [], []
        if part_id:
            args.append(part_id)
            conds.append(f"m.part_id = ${len(args)}")
        if kind:
            args.append(kind)
            conds.append(f"m.kind = ${len(args)}")
        if order_id:
            args.append(order_id)
            conds.append(f"m.order_id = ${len(args)}")
        where = ("where " + " and ".join(conds)) if conds else ""
        args.append(limit)
        return _rows(await self.pool.fetch(
            f"{self._MOVE_SELECT} {where} order by m.created_at desc, m.id desc "
            f"limit ${len(args)}", *args))

    async def add_part_move(self, *, part_id: int, kind: str, qty: int,
                            cost: Decimal, doc_id: int | None = None,
                            order_id: int | None = None, note: str | None = None,
                            created_by: str | None = None) -> int:
        return int(await self.pool.fetchval(
            """
            insert into crm.part_moves (part_id, kind, qty, cost, doc_id, order_id,
                                        note, created_by)
            values ($1, $2, $3, $4, $5, $6, $7, $8) returning id
            """, part_id, kind, qty, cost, doc_id, order_id, note, created_by))

    # ───────────────────────── склад: документы ─────────────────────────

    async def part_docs(self, *, kind: str | None = None,
                        limit: int = 200) -> list[dict]:
        conds, args = [], []
        if kind:
            args.append(kind)
            conds.append(f"d.kind = ${len(args)}")
        where = ("where " + " and ".join(conds)) if conds else ""
        args.append(limit)
        return _rows(await self.pool.fetch(
            f"""
            select d.*, s.name as supplier_name, count(m.id) as lines
            from crm.part_docs d
            left join crm.suppliers s on s.id = d.supplier_id
            left join crm.part_moves m on m.doc_id = d.id
            {where}
            group by d.id, s.name
            order by d.created_at desc, d.id desc
            limit ${len(args)}
            """, *args))

    async def part_doc(self, doc_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            """
            select d.*, s.name as supplier_name
            from crm.part_docs d
            left join crm.suppliers s on s.id = d.supplier_id
            where d.id = $1
            """, doc_id))

    async def create_part_doc(self, *, kind: str, supplier_id: int | None,
                              lines: list[dict], note: str | None,
                              created_by: str) -> int:
        """Документ склада, его движения и пересчёт себестоимости - одной
        транзакцией.

        Средняя себестоимость пересчитывается здесь же: между вставкой
        движения и правкой цены не должно быть окна, в котором наряд
        спишет запчасть по старой цене.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("lock table crm.part_docs in share row exclusive mode")
            next_no = int(await conn.fetchval(
                "select coalesce(max(substring(no from '[0-9]+$')::bigint), 0) + 1 "
                "from crm.part_docs where kind = $1", kind) or 1)
            total = sum((Decimal(str(line["price"])) * int(line["qty"])
                         for line in lines), Decimal(0)) if kind == "receipt" else \
                sum((Decimal(str(line.get("cost") or 0)) * int(line["qty"])
                     for line in lines), Decimal(0))
            doc_id = int(await conn.fetchval(
                """
                insert into crm.part_docs (no, kind, supplier_id, total, note, created_by)
                values ($1, $2, $3, $4, $5, $6) returning id
                """, logic.doc_no(kind, next_no), kind, supplier_id, total, note,
                created_by))
            for line in lines:
                part_id, qty = int(line["part_id"]), int(line["qty"])
                if kind == "receipt":
                    row = await conn.fetchrow(
                        "select p.cost, coalesce(sum(m.qty), 0) as stock from crm.parts p "
                        "left join crm.part_moves m on m.part_id = p.id "
                        "where p.id = $1 group by p.cost", part_id)
                    cost = logic.average_cost(int(row["stock"] or 0), row["cost"], qty,
                                              line["price"])
                    await conn.execute("update crm.parts set cost = $2 where id = $1",
                                       part_id, cost)
                    move_qty, move_cost = qty, Decimal(str(line["price"]))
                else:
                    cost = Decimal(str(line.get("cost") or 0))
                    move_qty, move_cost = -qty, cost
                await conn.execute(
                    """
                    insert into crm.part_moves (part_id, kind, qty, cost, doc_id,
                                                note, created_by)
                    values ($1, $2, $3, $4, $5, $6, $7)
                    """, part_id, kind, move_qty, move_cost, doc_id,
                    line.get("note"), created_by)
            return doc_id

    # ───────────────────────── склад: заказы ─────────────────────────

    _PART_ORDER_SELECT = """
        select o.*, s.name as supplier_name, count(i.id) as lines
        from crm.part_orders o
        left join crm.suppliers s on s.id = o.supplier_id
        left join crm.part_order_items i on i.order_id = o.id
    """

    async def part_orders(self, *, status: str | None = None,
                          limit: int = 200) -> list[dict]:
        conds, args = [], []
        if status:
            args.append(status)
            conds.append(f"o.status = ${len(args)}")
        where = ("where " + " and ".join(conds)) if conds else ""
        args.append(limit)
        return _rows(await self.pool.fetch(
            f"{self._PART_ORDER_SELECT} {where} group by o.id, s.name "
            f"order by o.created_at desc, o.id desc limit ${len(args)}", *args))

    async def part_order(self, order_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._PART_ORDER_SELECT} where o.id = $1 group by o.id, s.name",
            order_id))

    async def open_part_order(self) -> dict | None:
        """Собираемый заказ. Он один: потребности копятся в общий список,
        а не расползаются по десятку черновиков."""
        return _row(await self.pool.fetchrow(
            f"{self._PART_ORDER_SELECT} where o.status = 'new' "
            "group by o.id, s.name order by o.id desc limit 1"))

    async def create_part_order(self, *, supplier_id: int | None, note: str | None,
                                created_by: str) -> int:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("lock table crm.part_orders in share row exclusive mode")
            next_no = int(await conn.fetchval(
                "select coalesce(max(substring(no from '[0-9]+$')::bigint), 0) + 1 "
                "from crm.part_orders") or 1)
            return int(await conn.fetchval(
                """
                insert into crm.part_orders (no, supplier_id, note, created_by)
                values ($1, $2, $3, $4) returning id
                """, logic.part_order_no(next_no), supplier_id, note, created_by))

    async def update_part_order(self, order_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets, values = _set_clause(fields, PART_ORDER_FIELDS, 2)
        await self.pool.execute(
            f"update crm.part_orders set {sets} where id = $1", order_id, *values)

    async def part_order_items(self, order_id: int) -> list[dict]:
        return _rows(await self.pool.fetch(
            """
            select i.*, p.title, p.unit, p.node, w.no as work_order_no
            from crm.part_order_items i
            join crm.parts p on p.id = i.part_id
            left join crm.work_orders w on w.id = i.work_order_id
            where i.order_id = $1 order by p.title
            """, order_id))

    async def add_part_order_item(self, order_id: int, *, part_id: int, qty: int,
                                  price: Decimal, source: str,
                                  work_order_id: int | None = None) -> int | None:
        """Строка заказа. None - эта позиция в заказе уже есть."""
        try:
            return int(await self.pool.fetchval(
                """
                insert into crm.part_order_items (order_id, part_id, qty, price,
                                                  source, work_order_id)
                values ($1, $2, $3, $4, $5, $6) returning id
                """, order_id, part_id, qty, price, source, work_order_id))
        except asyncpg.UniqueViolationError:
            return None

    async def delete_part_order_item(self, order_id: int, item_id: int) -> bool:
        row = await self.pool.fetchrow(
            "delete from crm.part_order_items where order_id = $1 and id = $2 "
            "returning id", order_id, item_id)
        return row is not None

    async def waiting_orders_parts(self) -> list[dict]:
        """Наряды в состоянии «ждёт запчасть» с их узлами: из них и
        собирается половина потребностей склада."""
        return _rows(await self.pool.fetch(
            """
            select o.id as work_order_id, o.no as work_order_no, b.code as bike_code,
                   i.node, coalesce(n.title, 'Без узла') as node_title,
                   p.id as part_id, coalesce(p.title, coalesce(n.title, 'Без узла'))
                     as title, sum(i.qty) as qty
            from crm.work_orders o
            left join crm.work_order_items i on i.order_id = o.id
            left join crm.bikes b on b.id = o.bike_id
            left join crm.repair_nodes n on n.code = i.node
            left join lateral (
              select p.id, p.title from crm.parts p
              where p.node = i.node and p.active order by p.id limit 1
            ) p on true
            where o.status = 'waiting'
            group by o.id, o.no, b.code, i.node, n.title, p.id, p.title
            order by o.no
            """))

    # ─────────────────── позиции аренды сверх велосипеда ───────────────────

    async def rental_extras(self, rental_id: int, *,
                            live_only: bool = False) -> list[dict]:
        where = "and e.removed_at is null" if live_only else ""
        return _rows(await self.pool.fetch(
            f"""
            select e.*, b.code as battery_code, m.title as battery_model
            from crm.rental_extras e
            left join crm.batteries b on b.id = e.battery_id
            left join crm.battery_models m on m.id = b.model_id
            where e.rental_id = $1 {where} order by e.id
            """, rental_id))

    async def rental_extra(self, extra_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.rental_extras where id = $1", extra_id))

    async def add_rental_extra(self, rental_id: int, *, kind: str, title: str,
                               price: Decimal, battery_id: int | None,
                               by: str | None) -> int:
        """Позиция и новая цена периода - одной транзакцией.

        Цена аренды складывается из велосипеда и позиций. Записать позицию
        и забыть переписать цену значит выдать батарею бесплатно, а
        переписать цену без позиции - взять деньги неизвестно за что.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            extra_id = int(await conn.fetchval(
                """
                insert into crm.rental_extras (rental_id, kind, battery_id,
                                               title, price, added_by)
                values ($1, $2, $3, $4, $5, $6) returning id
                """, rental_id, kind, battery_id, title, price, by))
            await conn.execute(
                "update crm.rentals set price = $2, updated_at = now() "
                "where id = $1", rental_id,
                await self._period_price(conn, rental_id))
            return extra_id

    async def drop_rental_extra(self, extra_id: int, *, by: str | None) -> bool:
        async with self.pool.acquire() as conn, conn.transaction():
            rental_id = await conn.fetchval(
                "update crm.rental_extras set removed_at = now(), removed_by = $2 "
                "where id = $1 and removed_at is null returning rental_id",
                extra_id, by)
            if rental_id is None:
                return False
            await conn.execute(
                "update crm.rentals set price = $2, updated_at = now() "
                "where id = $1", int(rental_id),
                await self._period_price(conn, int(rental_id)))
            return True

    @staticmethod
    async def _period_price(conn: Any, rental_id: int) -> Decimal:
        """Цена периода: цена велосипеда на выдаче плюс действующие позиции.

        `rentals.price` уже включает позиции, складывать её с ними второй
        раз нельзя - потому база и хранится отдельной колонкой.
        """
        base = await conn.fetchval(
            "select coalesce(base_price, price) from crm.rentals where id = $1",
            rental_id)
        extras = await conn.fetchval(
            "select coalesce(sum(price), 0) from crm.rental_extras "
            "where rental_id = $1 and removed_at is null", rental_id)
        return Decimal(str(base or 0)) + Decimal(str(extras or 0))

    # ─────────────────── замена велосипеда в аренде ───────────────────

    async def rental_bikes(self, rental_id: int) -> list[dict]:
        return _rows(await self.pool.fetch(
            """
            select rb.*, b.code as bike_code, b.model as bike_model,
                   b.status as bike_status, b.mileage_km as bike_mileage
            from crm.rental_bikes rb join crm.bikes b on b.id = rb.bike_id
            where rb.rental_id = $1 order by rb.id
            """, rental_id))

    async def open_rental_bike(self, rental_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.rental_bikes where rental_id = $1 "
            "and returned_on is null order by id desc limit 1", rental_id))

    async def add_rental_bike(self, rental_id: int, *, bike_id: int, issued_on: date,
                              mileage_start: int | None, reason: str | None,
                              created_by: str | None) -> int:
        return int(await self.pool.fetchval(
            """
            insert into crm.rental_bikes (rental_id, bike_id, issued_on,
                                          mileage_start, reason, created_by)
            values ($1, $2, $3, $4, $5, $6) returning id
            """, rental_id, bike_id, issued_on, mileage_start, reason, created_by))

    async def swap_rental_bike(self, rental_id: int, *, old_bike_id: int | None,
                               new_bike_id: int, old_status: str,
                               mileage_old: int | None, mileage_new: int | None,
                               reason: str, today: date, by: str) -> bool:
        """Замена велосипеда внутри аренды - одной транзакцией.

        Снять старый, выдать новый и переписать аренду по отдельности
        нельзя: сбой между запросами оставил бы клиента без велосипеда
        либо с двумя, а деньги аренды - на снятом.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('crm.actor', $1, true)", by or "")
            rental = await conn.fetchrow(
                "select bike_id, started_on, mileage_start, status from crm.rentals "
                "where id = $1 for update", rental_id)
            if rental is None or rental["status"] != "active":
                return False
            if rental["bike_id"] != old_bike_id:
                return False          # велосипед уже сменили в другом окне
            if old_bike_id is not None:
                # Журнал мог не застать выдачу (аренда старше замены) -
                # тогда открываем строку задним числом по данным аренды.
                open_row = await conn.fetchrow(
                    "select id from crm.rental_bikes where rental_id = $1 "
                    "and returned_on is null order by id desc limit 1", rental_id)
                if open_row is None:
                    await conn.execute(
                        """
                        insert into crm.rental_bikes (rental_id, bike_id, issued_on,
                                                      mileage_start, reason, created_by)
                        values ($1, $2, $3, $4, 'Выдача', $5)
                        """, rental_id, old_bike_id, rental["started_on"],
                        rental["mileage_start"], by)
                await conn.execute(
                    """
                    update crm.rental_bikes set returned_on = $2, mileage_end = $3
                     where rental_id = $1 and returned_on is null
                    """, rental_id, today, mileage_old)
                await conn.execute(
                    "update crm.bikes set status = $2, "
                    "mileage_km = greatest(mileage_km, coalesce($3, mileage_km)), "
                    "updated_at = now() where id = $1",
                    old_bike_id, old_status, mileage_old)
            await conn.execute(
                """
                insert into crm.rental_bikes (rental_id, bike_id, issued_on,
                                              mileage_start, reason, created_by)
                values ($1, $2, $3, $4, $5, $6)
                """, rental_id, new_bike_id, today, mileage_new, reason, by)
            await conn.execute(
                "update crm.bikes set status = 'rented', "
                "mileage_km = greatest(mileage_km, coalesce($2, mileage_km)), "
                "updated_at = now() where id = $1", new_bike_id, mileage_new)
            await conn.execute(
                "update crm.rentals set bike_id = $2, mileage_start = coalesce($3, 0), "
                "mileage_end = null, updated_at = now() where id = $1",
                rental_id, new_bike_id, mileage_new)
            return True

    # ─────────────────── закупки основных средств ───────────────────

    async def purchases(self, *, limit: int = 200) -> list[dict]:
        return _rows(await self.pool.fetch(
            """
            select p.*, s.name as supplier_name, count(b.id) as bikes,
                   count(b.id) filter (where b.status = 'written_off') as written_off,
                   coalesce(sum(b.purchase_price), 0) as spent
            from crm.purchases p
            left join crm.suppliers s on s.id = p.supplier_id
            left join crm.bikes b on b.purchase_id = p.id
            group by p.id, s.name
            order by p.purchased_on desc, p.id desc
            limit $1
            """, limit))

    async def purchase(self, purchase_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            """
            select p.*, s.name as supplier_name
            from crm.purchases p
            left join crm.suppliers s on s.id = p.supplier_id
            where p.id = $1
            """, purchase_id))

    async def create_purchase(self, *, supplier_id: int | None, purchased_on: date,
                              note: str | None, bikes: list[dict],
                              created_by: str) -> int:
        """Закупка и её велосипеды - одной транзакцией.

        Половина заведённой партии хуже, чем незаведённая: оператор считает,
        что парк пополнен, а в выдаче половины номеров нет.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('crm.actor', $1, true)", created_by)
            await conn.execute("lock table crm.purchases in share row exclusive mode")
            next_no = int(await conn.fetchval(
                "select coalesce(max(substring(no from '[0-9]+$')::bigint), 0) + 1 "
                "from crm.purchases") or 1)
            total = sum((Decimal(str(b.get("purchase_price") or 0)) for b in bikes),
                        Decimal(0))
            purchase_id = int(await conn.fetchval(
                """
                insert into crm.purchases (no, supplier_id, purchased_on, total, note,
                                           created_by)
                values ($1, $2, $3, $4, $5, $6) returning id
                """, logic.purchase_no(next_no), supplier_id, purchased_on, total,
                note, created_by))
            for bike in bikes:
                await conn.execute(
                    """
                    insert into crm.bikes (code, model, battery_count, purchase_price,
                                           purchased_on, location, service_months,
                                           residual_price, battery_price,
                                           battery_service_months, purchase_id, note)
                    values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    """, bike["code"], bike["model"], bike["battery_count"],
                    bike["purchase_price"], purchased_on, bike.get("location"),
                    bike["service_months"], bike["residual_price"],
                    bike.get("battery_price"), bike["battery_service_months"],
                    purchase_id, bike.get("note"))
            return purchase_id

    async def purchase_bikes(self, purchase_id: int) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select * from crm.bikes where purchase_id = $1 order by code",
            purchase_id))

    # ───────────────── справочники: точки и модели ─────────────────

    async def locations(self, *, active_only: bool = False) -> list[dict]:
        where = "where active" if active_only else ""
        return _rows(await self.pool.fetch(
            f"select * from crm.locations {where} order by sort, name"))

    async def location_names(self) -> list[str]:
        """Названия точек для форм и проверок. Пусто - справочник не завели,
        и вызывающий откатывается на константу из logic."""
        rows = await self.pool.fetch(
            "select name from crm.locations where active order by sort, name")
        return [r["name"] for r in rows]

    async def create_location(self, *, name: str, city: str, address: str | None,
                              note: str | None, **extra: Any) -> int:
        unknown = set(extra) - LOCATION_FIELDS
        if unknown:
            raise ValueError(f"недопустимые колонки: {sorted(unknown)}")
        cols = ["name", "city", "address", "note", *extra]
        values = [name, city, address, note, *extra.values()]
        places = ", ".join(f"${i}" for i in range(1, len(cols) + 1))
        return int(await self.pool.fetchval(
            f"insert into crm.locations ({', '.join(cols)}) "
            f"values ({places}) returning id", *values))

    async def update_location(self, location_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets, values = _set_clause(fields, LOCATION_FIELDS, 2)
        await self.pool.execute(
            f"update crm.locations set {sets} where id = $1", location_id, *values)

    async def bike_models(self, *, active_only: bool = False) -> list[dict]:
        where = "where m.active" if active_only else ""
        return _rows(await self.pool.fetch(
            f"""
            select m.*, count(b.id) as bikes
            from crm.bike_models m
            left join crm.bikes b on b.model = m.title
            {where}
            group by m.id
            order by m.active desc, m.title
            """))

    async def bike_model(self, model_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.bike_models where id = $1", model_id))

    async def create_bike_model(self, *, title: str, brand: str | None,
                                factory_title: str | None, battery_slots: int,
                                note: str | None, **specs: Any) -> int:
        """Модель с характеристиками. Их спрашивает каждый второй курьер,
        и раньше ответ жил в голове оператора."""
        unknown = set(specs) - BIKE_MODEL_FIELDS
        if unknown:
            raise ValueError(f"недопустимые колонки: {sorted(unknown)}")
        cols = ["title", "brand", "factory_title", "battery_slots", "note",
                *specs]
        values = [title, brand, factory_title, battery_slots, note,
                  *specs.values()]
        places = ", ".join(f"${i}" for i in range(1, len(cols) + 1))
        return int(await self.pool.fetchval(
            f"insert into crm.bike_models ({', '.join(cols)}) "
            f"values ({places}) returning id", *values))

    async def update_bike_model(self, model_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets, values = _set_clause(fields, BIKE_MODEL_FIELDS, 2)
        await self.pool.execute(
            f"update crm.bike_models set {sets} where id = $1", model_id, *values)

    async def battery_models(self, *, active_only: bool = False) -> list[dict]:
        where = "where m.active" if active_only else ""
        return _rows(await self.pool.fetch(
            f"""
            select m.*, count(b.id) as batteries
            from crm.battery_models m
            left join crm.batteries b on b.model_id = m.id
            {where}
            group by m.id
            order by m.active desc, m.title
            """))

    async def battery_model(self, model_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.battery_models where id = $1", model_id))

    async def create_battery_model(self, *, title: str, brand: str | None,
                                   voltage: int | None, capacity: Decimal | None,
                                   price: Decimal, service_months: int,
                                   note: str | None) -> int:
        return int(await self.pool.fetchval(
            """
            insert into crm.battery_models (title, brand, voltage, capacity, price,
                                            service_months, note)
            values ($1, $2, $3, $4, $5, $6, $7) returning id
            """, title, brand, voltage, capacity, price, service_months, note))

    async def update_battery_model(self, model_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets, values = _set_clause(fields, BATTERY_MODEL_FIELDS, 2)
        await self.pool.execute(
            f"update crm.battery_models set {sets} where id = $1", model_id, *values)

    # ───────────────── совместимость ─────────────────

    async def compat_pairs(self) -> list[dict]:
        return _rows(await self.pool.fetch(
            """
            select c.*, bm.title as bike_title, tm.title as battery_title
            from crm.compat c
            join crm.bike_models bm on bm.id = c.bike_model_id
            join crm.battery_models tm on tm.id = c.battery_model_id
            order by bm.title, tm.title
            """))

    async def set_compat(self, bike_model_id: int, battery_model_id: int, *,
                         fits: bool, primary_fit: bool = False) -> None:
        """Одна клетка матрицы. Снятая галочка удаляет пару, а не хранит
        «не подходит»: пустая клетка и есть «не подходит»."""
        if not fits:
            await self.pool.execute(
                "delete from crm.compat where bike_model_id = $1 "
                "and battery_model_id = $2", bike_model_id, battery_model_id)
            return
        await self.pool.execute(
            """
            insert into crm.compat (bike_model_id, battery_model_id, primary_fit)
            values ($1, $2, $3)
            on conflict (bike_model_id, battery_model_id)
            do update set primary_fit = excluded.primary_fit
            """, bike_model_id, battery_model_id, primary_fit)

    async def compat_for_bike_model(self, title: str) -> list[dict]:
        """Какие батареи подходят этой модели велосипеда - по названию:
        в crm.bikes лежит текст модели, а не ссылка на каталог."""
        return _rows(await self.pool.fetch(
            """
            select tm.*, c.primary_fit
            from crm.compat c
            join crm.bike_models bm on bm.id = c.bike_model_id
            join crm.battery_models tm on tm.id = c.battery_model_id
            where bm.title = $1 and tm.active
            order by c.primary_fit desc, tm.title
            """, title))

    # ───────────────────────────── батареи ─────────────────────────────

    _BATTERY_SELECT = """
        select b.*, m.title as model_title, m.voltage, m.capacity,
               m.price as model_price, bk.code as bike_code, bk.model as bike_model,
               c.full_name as client_name, r.client_id,
               -- Розыск - состояние аренды, а не батареи: пока клиент
               -- не нашёлся, батарея у него, и статус у неё «у клиента».
               r.search_at as search_at
        from crm.batteries b
        left join crm.battery_models m on m.id = b.model_id
        left join crm.bikes bk on bk.id = b.bike_id
        left join crm.rentals r on r.id = b.rental_id and r.status = 'active'
        left join crm.clients c on c.id = r.client_id
    """

    async def batteries(self, *, status: str | None = None, q: str | None = None,
                        location: str | None = None, bike_id: int | None = None,
                        rental_id: int | None = None, in_search: bool = False,
                        limit: int = 1000) -> list[dict]:
        conds, args = [], []
        if status:
            args.append(status)
            conds.append(f"b.status = ${len(args)}")
        if location == "none":
            conds.append("b.location is null")
        elif location:
            args.append(location)
            conds.append(f"b.location = ${len(args)}")
        if bike_id:
            args.append(bike_id)
            conds.append(f"b.bike_id = ${len(args)}")
        if rental_id:
            args.append(rental_id)
            conds.append(f"b.rental_id = ${len(args)}")
        if q:
            args.append(f"%{q.strip()}%")
            conds.append(f"(b.code ilike ${len(args)} or b.serial_no ilike ${len(args)})")
        if in_search:
            conds.append("r.search_at is not null")
        where = ("where " + " and ".join(conds)) if conds else ""
        args.append(limit)
        return _rows(await self.pool.fetch(
            f"{self._BATTERY_SELECT} {where} order by b.code limit ${len(args)}", *args))

    async def battery(self, battery_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._BATTERY_SELECT} where b.id = $1", battery_id))

    async def battery_by_code(self, code: str) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.batteries where code = $1", code))

    async def create_battery(self, *, by: str | None = None, **fields: Any) -> int:
        unknown = set(fields) - BATTERY_FIELDS
        if unknown:
            raise ValueError(f"недопустимые колонки: {sorted(unknown)}")
        cols = list(fields)
        places = ", ".join(f"${i}" for i in range(1, len(cols) + 1))
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('crm.actor', $1, true)", by or "")
            return int(await conn.fetchval(
                f"insert into crm.batteries ({', '.join(cols)}) values ({places}) "
                "returning id", *[fields[c] for c in cols]))

    async def update_battery(self, battery_id: int, *, by: str | None = None,
                             **fields: Any) -> None:
        """by - кто менял: триггер журнала статусов читает его из
        set_config('crm.actor') в той же транзакции."""
        sets, values = _set_clause(fields, BATTERY_FIELDS, 2)
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('crm.actor', $1, true)", by or "")
            await conn.execute(
                f"update crm.batteries set {sets}, updated_at = now() where id = $1",
                battery_id, *values)

    async def battery_status_log(self, battery_id: int, limit: int = 30) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select * from crm.battery_status_log where battery_id = $1 "
            "order by changed_at desc, id desc limit $2", battery_id, limit))

    async def battery_counts(self) -> dict[str, int]:
        rows = await self.pool.fetch(
            "select status, count(*) as n from crm.batteries group by status")
        return {r["status"]: int(r["n"]) for r in rows}

    async def issue_batteries(self, rental_id: int, *, battery_ids: list[int],
                              bike_id: int | None, by: str) -> None:
        """Выдать батареи вместе с арендой - одной транзакцией.

        Батарея уходит к клиенту так же, как велосипед: статус, привязка
        к аренде и журнал - вместе, иначе выданная батарея останется
        «свободной» и уедет второму клиенту.
        """
        if not battery_ids:
            return
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('crm.actor', $1, true)", by or "")
            await conn.execute(
                """
                update crm.batteries
                   set status = 'rented', rental_id = $2, bike_id = coalesce($3, bike_id),
                       updated_at = now()
                 where id = any($1::bigint[]) and status = 'available'
                """, battery_ids, rental_id, bike_id)

    async def return_batteries(self, rental_id: int, *, status: str = "available",
                               by: str) -> int:
        """Принять батареи обратно при закрытии аренды или замене."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('crm.actor', $1, true)", by or "")
            rows = await conn.fetch(
                """
                update crm.batteries
                   set status = $2, rental_id = null, cycles = cycles + 1,
                       updated_at = now()
                 where rental_id = $1 and status = 'rented'
                returning id
                """, rental_id, status)
            return len(rows)

    async def return_battery(self, battery_id: int, *, status: str = "available",
                             by: str) -> bool:
        """Принять одну батарею: снятие доп. аккумулятора среди аренды."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('crm.actor', $1, true)", by or "")
            row = await conn.fetchrow(
                """
                update crm.batteries
                   set status = $2, rental_id = null, cycles = cycles + 1,
                       updated_at = now()
                 where id = $1 and status = 'rented'
                returning id
                """, battery_id, status)
            return row is not None

    # ─────────────────────────── трекеры ───────────────────────────

    _TRACKER_SELECT = """
        select t.*, b.code as bike_code, b.model as bike_model, b.status as bike_status,
               r.id as rental_id, c.full_name as client_name, c.phone as client_phone,
               c.id as client_id
          from crm.trackers t
          left join crm.bikes b on b.id = t.bike_id
          left join crm.rentals r on r.bike_id = b.id and r.status = 'active'
          left join crm.clients c on c.id = r.client_id
    """

    async def trackers(self, *, active_only: bool = False,
                       unbound: bool = False) -> list[dict]:
        where = ["true"]
        if active_only:
            where.append("t.active")
        if unbound:
            where.append("t.bike_id is null")
        return _rows(await self.pool.fetch(
            f"{self._TRACKER_SELECT} where {' and '.join(where)} "
            "order by b.code nulls last, t.device_id"))

    async def tracker(self, tracker_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._TRACKER_SELECT} where t.id = $1", tracker_id))

    async def tracker_by_device(self, device_id: str) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._TRACKER_SELECT} where t.device_id = $1", device_id))

    async def tracker_of_bike(self, bike_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._TRACKER_SELECT} where t.bike_id = $1", bike_id))

    async def create_tracker(self, **fields: Any) -> int:
        unknown = set(fields) - TRACKER_FIELDS
        if unknown:
            raise ValueError(f"недопустимые колонки: {sorted(unknown)}")
        cols = list(fields)
        places = ", ".join(f"${i}" for i in range(1, len(cols) + 1))
        return int(await self.pool.fetchval(
            f"insert into crm.trackers ({', '.join(cols)}) values ({places}) "
            "returning id", *[fields[c] for c in cols]))

    async def update_tracker(self, tracker_id: int, **fields: Any) -> None:
        unknown = set(fields) - TRACKER_FIELDS
        if unknown:
            raise ValueError(f"недопустимые колонки: {sorted(unknown)}")
        if not fields:
            return
        sets = ", ".join(f"{c} = ${i}" for i, c in enumerate(fields, start=2))
        await self.pool.execute(
            f"update crm.trackers set {sets}, updated_at = now() where id = $1",
            tracker_id, *fields.values())

    async def save_tracker_state(self, device: dict) -> dict:
        """Состояние устройства из StarLine: карточка и точка журнала.

        Устройство, которого ещё нет, заводится само: связать его с
        велосипедом оператор успеет, а терять координаты новой метки,
        пока до неё не дошли руки, незачем. Позиция пишется в журнал
        только с новой меткой времени - индекс не даст дублей, а
        `on conflict do nothing` не даст падения на гонке опросов.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                insert into crm.trackers (device_id, alias, last_seen, lat, lon,
                                          speed, course, voltage, gsm_level, alarm)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                on conflict (device_id) do update
                   set alias = coalesce(excluded.alias, crm.trackers.alias),
                       last_seen = coalesce(excluded.last_seen, crm.trackers.last_seen),
                       lat = coalesce(excluded.lat, crm.trackers.lat),
                       lon = coalesce(excluded.lon, crm.trackers.lon),
                       speed = excluded.speed, course = excluded.course,
                       voltage = coalesce(excluded.voltage, crm.trackers.voltage),
                       gsm_level = excluded.gsm_level, alarm = excluded.alarm,
                       updated_at = now()
                returning id, (xmax = 0) as created
                """,
                device["device_id"], device.get("alias"), device.get("recorded_at"),
                device.get("lat"), device.get("lon"),
                _money(device.get("speed")), device.get("course"),
                _money(device.get("voltage")), device.get("gsm_level"),
                bool(device.get("alarm")))
            tracker_id = int(row["id"])
            if device.get("lat") is not None and device.get("recorded_at") is not None:
                await conn.execute(
                    """
                    insert into crm.tracker_positions (tracker_id, lat, lon, speed,
                                                       course, recorded_at)
                    values ($1, $2, $3, $4, $5, $6)
                    on conflict (tracker_id, recorded_at) do nothing
                    """, tracker_id, device["lat"], device["lon"],
                    _money(device.get("speed")), device.get("course"),
                    device["recorded_at"])
            return {"id": tracker_id, "created": bool(row["created"])}

    async def track_between(self, tracker_id: int, *, since: datetime,
                            until: datetime, limit: int = 2000) -> list[dict]:
        """Точки трекера за период - для линии на карте.

        Сортировка по времени вперёд: линию рисуют от старой точки к
        новой, и разворачивать её в шаблоне было бы странно.
        """
        return _rows(await self.pool.fetch(
            "select * from crm.tracker_positions where tracker_id = $1 "
            "and recorded_at >= $2 and recorded_at < $3 "
            "order by recorded_at limit $4",
            tracker_id, since, until, limit))

    async def tracker_positions(self, tracker_id: int, limit: int = 200) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select * from crm.tracker_positions where tracker_id = $1 "
            "order by recorded_at desc limit $2", tracker_id, limit))

    async def purge_tracker_positions(self, days: int = 30) -> int:
        """Журнал позиций - расходный материал: точка на опрос за месяц
        даёт десятки тысяч строк, а нужен он на пару недель назад."""
        rows = await self.pool.fetch(
            "delete from crm.tracker_positions "
            "where recorded_at < now() - make_interval(days => $1) returning id", days)
        return len(rows)

    async def tracker_alerts(self, *, open_only: bool = True,
                             limit: int = 200) -> list[dict]:
        where = "where a.handled_at is null" if open_only else ""
        return _rows(await self.pool.fetch(
            f"""
            select a.*, t.device_id, t.alias, b.code as bike_code, b.model as bike_model
              from crm.tracker_alerts a
              join crm.trackers t on t.id = a.tracker_id
              left join crm.bikes b on b.id = a.bike_id
            {where}
            order by a.created_at desc limit $1
            """, limit))

    async def raise_alert(self, *, tracker_id: int, kind: str, note: str | None,
                          bike_id: int | None, lat: float | None,
                          lon: float | None) -> int | None:
        """Поднять тревогу. None - такая уже висит открытой."""
        return await self.pool.fetchval(
            """
            insert into crm.tracker_alerts (tracker_id, bike_id, kind, note, lat, lon)
            values ($1, $2, $3, $4, $5, $6)
            on conflict do nothing
            returning id
            """, tracker_id, bike_id, kind, note, lat, lon)

    async def close_alerts(self, tracker_id: int, kinds: list[str], *,
                           by: str | None = None) -> int:
        """Снять тревоги, которых больше нет: велосипед вернулся на связь
        или уехал в аренду по-честному."""
        if not kinds:
            return 0
        rows = await self.pool.fetch(
            "update crm.tracker_alerts set handled_at = now(), handled_by = $3 "
            "where tracker_id = $1 and kind = any($2::text[]) and handled_at is null "
            "returning id", tracker_id, kinds, by)
        return len(rows)

    async def handle_alert(self, alert_id: int, *, by: str) -> None:
        await self.pool.execute(
            "update crm.tracker_alerts set handled_at = now(), handled_by = $2 "
            "where id = $1 and handled_at is null", alert_id, by)

    # ─────────────────────────── касса ───────────────────────────

    async def cash_shifts(self, *, limit: int = 100) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select * from crm.cash_shifts order by opened_at desc limit $1", limit))

    async def cash_shift(self, shift_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.cash_shifts where id = $1", shift_id))

    async def open_shift(self) -> dict | None:
        """Смена, открытая прямо сейчас. Их может быть по одной на точку -
        берётся самая ранняя: оператор работает в своей, а сводка
        показывает ту, что дольше всех висит незакрытой."""
        return _row(await self.pool.fetchrow(
            "select * from crm.cash_shifts where status = 'open' "
            "order by opened_at limit 1"))

    async def open_shift_at(self, location: str | None) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.cash_shifts "
            "where status = 'open' and coalesce(location, '') = coalesce($1, '')",
            location))

    async def create_shift(self, *, location: str | None, opening: Decimal,
                           note: str | None, by: str) -> int:
        """Открыть смену. Номер выдаётся в той же транзакции: иначе две
        кассы, открытые в одну секунду, получат один номер."""
        async with self.pool.acquire() as conn, conn.transaction():
            number = int(await conn.fetchval(
                "select count(*) + 1 from crm.cash_shifts"))
            return int(await conn.fetchval(
                """
                insert into crm.cash_shifts (no, location, opening, note, opened_by)
                values ($1, $2, $3, $4, $5) returning id
                """, logic.shift_no(number), location, opening, note, by))

    async def add_cash_move(self, shift_id: int, *, kind: str, amount: Decimal,
                            reason: str | None, by: str,
                            ledger_id: int | None = None) -> int:
        return int(await self.pool.fetchval(
            """
            insert into crm.cash_moves (shift_id, kind, amount, reason, ledger_id,
                                        created_by)
            values ($1, $2, $3, $4, $5, $6) returning id
            """, shift_id, kind, amount, reason, ledger_id, by))

    async def cash_moves(self, shift_id: int) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select * from crm.cash_moves where shift_id = $1 order by id", shift_id))

    async def shift_payments(self, shift_id: int) -> list[dict]:
        """Наличные платежи за время смены - из журнала клиентов.

        Смена не хранит копию этих строк: копия разошлась бы с журналом
        при первой же правке платежа, а сходимость кассы держится именно
        на том, что деньги в ящике и деньги в журнале - одно и то же.
        """
        return _rows(await self.pool.fetch(
            """
            select l.*, c.full_name
              from crm.ledger l
              join crm.clients c on c.id = l.client_id
              join crm.cash_shifts s on s.id = $1
             where l.kind in ('payment', 'refund') and l.method = 'cash'
               and l.created_at >= s.opened_at
               and l.created_at < coalesce(s.closed_at, now())
             order by l.id
            """, shift_id))

    async def close_shift(self, shift_id: int, *, counted: Decimal,
                          expected: Decimal, note: str | None, by: str) -> None:
        await self.pool.execute(
            """
            update crm.cash_shifts
               set status = 'closed', closed_at = now(), closed_by = $2,
                   counted = $3::numeric, expected = $4::numeric,
                   -- Приведение обязательно: без него Postgres видит
                   -- «неизвестное минус неизвестное» и не выбирает оператор.
                   diff = $3::numeric - $4::numeric,
                   note = coalesce($5, note)
             where id = $1 and status = 'open'
            """, shift_id, by, counted, expected, note)

    # ─────────────────────────── банк ───────────────────────────

    async def bank_txns(self, *, status: str | None = None,
                        limit: int = 200) -> list[dict]:
        where = "where t.status = $2" if status else ""
        args = [limit] + ([status] if status else [])
        return _rows(await self.pool.fetch(
            f"""
            select t.*, c.full_name as client_name
              from crm.bank_txns t
              left join crm.clients c on c.id = t.client_id
            {where}
            order by t.booked_at desc limit $1
            """, *args))

    async def bank_txn(self, txn_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.bank_txns where id = $1", txn_id))

    async def save_bank_txn(self, txn: dict) -> int | None:
        """Строка выписки. None - такая уже есть: выписку тянут за
        перекрывающиеся периоды, и повторы - норма, а не сбой."""
        return await self.pool.fetchval(
            """
            insert into crm.bank_txns (txn_id, account, booked_at, amount,
                                       direction, payer_name, payer_inn, purpose)
            values ($1, $2, $3, $4, $5, $6, $7, $8)
            on conflict (txn_id) do nothing
            returning id
            """, txn["txn_id"], txn.get("account"), txn["booked_at"],
            txn["amount"], txn["direction"], txn.get("payer_name"),
            txn.get("payer_inn"), txn.get("purpose"))

    async def mark_bank_txn(self, txn_id: int, *, status: str,
                            client_id: int | None = None,
                            ledger_id: int | None = None, by: str) -> None:
        await self.pool.execute(
            """
            update crm.bank_txns
               set status = $2, client_id = $3, ledger_id = $4,
                   handled_at = now(), handled_by = $5
             where id = $1
            """, txn_id, status, client_id, ledger_id, by)

    async def last_bank_txn_at(self) -> datetime | None:
        return await self.pool.fetchval("select max(booked_at) from crm.bank_txns")

    # ─────────────────────────── рассылки ───────────────────────────

    async def templates(self, *, active_only: bool = False) -> list[dict]:
        where = "where active" if active_only else ""
        return _rows(await self.pool.fetch(
            f"select * from crm.message_templates {where} order by title"))

    async def template(self, template_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.message_templates where id = $1", template_id))

    async def create_template(self, *, code: str, title: str, body: str,
                              body_max: str | None, note: str | None) -> int:
        return int(await self.pool.fetchval(
            """
            insert into crm.message_templates (code, title, body, body_max, note)
            values ($1, $2, $3, $4, $5) returning id
            """, code, title, body, body_max, note))

    async def update_template(self, template_id: int, **fields: Any) -> None:
        unknown = set(fields) - TEMPLATE_FIELDS_DB
        if unknown:
            raise ValueError(f"недопустимые колонки: {sorted(unknown)}")
        if not fields:
            return
        sets = ", ".join(f"{c} = ${i}" for i, c in enumerate(fields, start=2))
        await self.pool.execute(
            f"update crm.message_templates set {sets}, updated_at = now() "
            "where id = $1", template_id, *fields.values())

    async def clients_for_mailing(self, limit: int = 10000) -> list[dict]:
        """Клиенты с балансом и датой последней аренды - для аудиторий.

        Баланс и «когда в последний раз брал» считаются здесь, а отбор -
        в logic.pick_audience: правила аудиторий меняются чаще, чем схема,
        и проверять их удобнее без базы.
        """
        return _rows(await self.pool.fetch(
            """
            select c.*, coalesce(l.balance, 0) as balance,
                   r.last_rental_on
              from crm.clients c
              left join (select client_id, sum(amount) as balance
                           from crm.ledger group by client_id) l
                     on l.client_id = c.id
              left join (select client_id, max(coalesce(closed_on, started_on))
                                as last_rental_on
                           from crm.rentals group by client_id) r
                     on r.client_id = c.id
             order by c.full_name limit $1
            """, limit))

    async def campaigns(self, *, limit: int = 100) -> list[dict]:
        return _rows(await self.pool.fetch(
            """
            select c.*, t.title as template_title,
                   count(s.id) as total,
                   count(s.id) filter (where s.status = 'sent') as sent,
                   count(s.id) filter (where s.status = 'failed') as failed
              from crm.campaigns c
              left join crm.message_templates t on t.id = c.template_id
              left join crm.campaign_sends s on s.campaign_id = c.id
             group by c.id, t.title
             order by c.created_at desc limit $1
            """, limit))

    async def campaign(self, campaign_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            """
            select c.*, t.title as template_title, t.body, t.body_max
              from crm.campaigns c
              left join crm.message_templates t on t.id = c.template_id
             where c.id = $1
            """, campaign_id))

    async def create_campaign(self, *, title: str, template_id: int,
                              audience: str, note: str | None, by: str) -> int:
        async with self.pool.acquire() as conn, conn.transaction():
            number = int(await conn.fetchval(
                "select count(*) + 1 from crm.campaigns"))
            return int(await conn.fetchval(
                """
                insert into crm.campaigns (no, title, template_id, audience,
                                           note, created_by)
                values ($1, $2, $3, $4, $5, $6) returning id
                """, logic.campaign_no(number), title, template_id, audience,
                note, by))

    async def queue_sends(self, campaign_id: int,
                          rows: list[tuple[int, str]]) -> int:
        """Поставить получателей в очередь. Повтор ничего не добавляет:
        уникальный индекс не даст отправить одному человеку дважды."""
        if not rows:
            return 0
        done = await self.pool.fetch(
            """
            insert into crm.campaign_sends (campaign_id, client_id, channel)
            select $1, x.client_id, x.channel
              from unnest($2::bigint[], $3::text[]) as x(client_id, channel)
            on conflict (campaign_id, client_id) do nothing
            returning id
            """, campaign_id, [r[0] for r in rows], [r[1] for r in rows])
        return len(done)

    async def campaign_sends(self, campaign_id: int, *, status: str | None = None,
                             limit: int = 1000) -> list[dict]:
        where = "and s.status = $3" if status else ""
        args = [campaign_id, limit] + ([status] if status else [])
        return _rows(await self.pool.fetch(
            f"""
            select s.*, c.full_name, c.phone, c.tg_id, c.max_id, c.contract_no
              from crm.campaign_sends s
              join crm.clients c on c.id = s.client_id
             where s.campaign_id = $1 {where}
             order by s.id limit $2
            """, *args))

    async def mark_send(self, send_id: int, *, status: str,
                        error: str | None = None) -> None:
        await self.pool.execute(
            "update crm.campaign_sends set status = $2, error = $3, "
            "sent_at = now() where id = $1", send_id, status, error)

    async def set_campaign_status(self, campaign_id: int, status: str) -> None:
        await self.pool.execute(
            """
            update crm.campaigns
               set status = $2,
                   started_at = case when $2 = 'sending' then now() else started_at end,
                   finished_at = case when $2 in ('done', 'cancelled') then now()
                                      else finished_at end
             where id = $1
            """, campaign_id, status)

    async def sending_campaigns(self) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select * from crm.campaigns where status = 'sending' order by id"))

    async def link_client_max(self, phone: str, max_id: int) -> int | None:
        """Связать карточку с аккаунтом MAX по телефону. None - такого
        клиента нет или аккаунт уже занят другой карточкой."""
        return await self.pool.fetchval(
            """
            update crm.clients set max_id = $2, updated_at = now()
             where phone = $1
               and (max_id is null or max_id = $2)
               and not exists (select 1 from crm.clients other
                                where other.max_id = $2 and other.phone <> $1)
            returning id
            """, phone, max_id)

    # ───────────── простая электронная подпись (ПЭП) ─────────────

    _SIGN_SELECT = """
        select s.*, c.full_name, c.phone, c.tg_id
          from crm.sign_requests s
          join crm.clients c on c.id = s.client_id
    """

    async def sign_requests(self, *, client_id: int | None = None,
                            limit: int = 200) -> list[dict]:
        where = "where s.client_id = $2" if client_id else ""
        args = [limit] + ([client_id] if client_id else [])
        return _rows(await self.pool.fetch(
            f"{self._SIGN_SELECT} {where} order by s.id desc limit $1", *args))

    async def sign_request(self, request_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._SIGN_SELECT} where s.id = $1", request_id))

    async def sign_request_by_token(self, token: str) -> dict | None:
        return _row(await self.pool.fetchrow(
            f"{self._SIGN_SELECT} where s.token = $1", token))

    async def create_sign_request(self, *, client_id: int, rental_id: int | None,
                                  token: str, docs: list[dict], agreement: str,
                                  expires_at: datetime, by: str) -> dict:
        """Завести заявку. Номер выдаётся в той же транзакции: две заявки,
        созданные одновременно, иначе получат один номер."""
        async with self.pool.acquire() as conn, conn.transaction():
            number = int(await conn.fetchval(
                "select count(*) + 1 from crm.sign_requests"))
            row = await conn.fetchrow(
                """
                insert into crm.sign_requests (no, client_id, rental_id, token,
                                               docs, agreement, expires_at,
                                               created_by)
                values ($1, $2, $3, $4, $5, $6, $7, $8)
                returning id, no
                """, logic.sign_no(number), client_id, rental_id, token,
                # Кодек json уже стоит на соединении: свой json.dumps здесь
                # завернул бы список в строку, и обратно пришла бы строка.
                docs, agreement, expires_at, by)
            await conn.execute(
                "insert into crm.sign_events (request_id, kind, note) "
                "values ($1, 'created', $2)", row["id"], by)
            return {"id": int(row["id"]), "no": row["no"]}

    async def set_sign_agreement(self, request_id: int, *, agreement: str,
                                 docs: list[dict]) -> None:
        """Текст соглашения и итоговый пакет - сразу после создания заявки:
        номер заявки стоит в тексте, а его выдаёт база."""
        await self.pool.execute(
            "update crm.sign_requests set agreement = $2, docs = $3 "
            "where id = $1", request_id, agreement, docs)

    async def set_sign_code(self, request_id: int, *, code_hash: str) -> None:
        """Новый код обнуляет счётчик попыток: старые промахи к нему
        отношения не имеют."""
        await self.pool.execute(
            "update crm.sign_requests set code_hash = $2, code_at = now(), "
            "attempts = 0, status = 'code' where id = $1 and status in ('new', 'code')",
            request_id, code_hash)

    async def bump_sign_attempt(self, request_id: int) -> int:
        return int(await self.pool.fetchval(
            "update crm.sign_requests set attempts = attempts + 1 "
            "where id = $1 returning attempts", request_id))

    async def mark_signed(self, request_id: int, *, ip: str | None,
                          agent: str | None) -> bool:
        """Подписать. False - кто-то успел раньше: подпись одна на заявку."""
        row = await self.pool.fetchrow(
            """
            update crm.sign_requests
               set status = 'signed', signed_at = now(), signed_ip = $2,
                   signed_agent = $3, code_hash = null
             where id = $1 and status in ('new', 'code')
            returning id
            """, request_id, ip, agent)
        return row is not None

    async def cancel_sign_request(self, request_id: int, *, by: str) -> None:
        await self.pool.execute(
            "update crm.sign_requests set status = 'cancelled' "
            "where id = $1 and status in ('new', 'code')", request_id)
        await self.log_sign_event(request_id, kind="cancelled", note=by)

    async def log_sign_event(self, request_id: int, *, kind: str,
                             ip: str | None = None, agent: str | None = None,
                             note: str | None = None) -> None:
        await self.pool.execute(
            "insert into crm.sign_events (request_id, kind, ip, user_agent, note) "
            "values ($1, $2, $3, $4, $5)", request_id, kind, ip, agent, note)

    async def sign_events(self, request_id: int, limit: int = 100) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select * from crm.sign_events where request_id = $1 "
            "order by id limit $2", request_id, limit))

    # ─────────────────────── приём оплаты ───────────────────────

    async def create_pay_order(self, *, client_id: int, rental_id: int | None,
                               amount: Decimal, purpose: str, kind: str = "link",
                               work_order_id: int | None = None,
                               created_by: str | None = None) -> int:
        """Счёт с человекочитаемым номером. Номер берётся в той же
        транзакции, что и вставка: две кнопки «выставить счёт» подряд не
        должны получить один и тот же СЧТ-."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("lock table crm.pay_orders in share row exclusive mode")
            next_no = int(await conn.fetchval(
                "select coalesce(max(substring(no from '[0-9]+$')::bigint), 0) + 1 "
                "from crm.pay_orders") or 1)
            return int(await conn.fetchval(
                """
                insert into crm.pay_orders
                    (no, client_id, rental_id, amount, purpose, kind,
                     work_order_id, created_by)
                values ($1, $2, $3, $4, $5, $6, $7, $8) returning id
                """, logic.pay_no(next_no), client_id, rental_id, _money(amount),
                purpose, kind, work_order_id, created_by))

    async def set_pay_link(self, order_id: int, *, link: str,
                           operation_id: str | None) -> None:
        await self.pool.execute(
            """
            update crm.pay_orders
               set link = $2, operation_id = $3, status = 'sent',
                   sent_at = now(), error = null
             where id = $1 and status = 'new'
            """, order_id, link, operation_id)

    async def mark_pay_paid(self, order_id: int, *, method: str = "card",
                            by: str | None = None) -> int | None:
        """Оплата подтверждена: счёт закрывается и ровно одной записью
        ложится в журнал. Обе правки в одной транзакции - иначе рестарт
        между ними оставил бы оплаченный счёт без денег в журнале.

        Возвращает id записи журнала или None, если счёт уже закрыт: у
        банка легко спросить статус дважды.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            order = await conn.fetchrow(
                "select * from crm.pay_orders where id = $1 for update", order_id)
            if order is None or order["status"] == "paid":
                return None
            if order["work_order_id"] is not None:
                # Красная линия: выручка чужого ремонта в crm.ledger не
                # попадает - журнал это аренда, и средний чек считается
                # по нему. Оплата ремонта живёт на наряде.
                await conn.execute(
                    "update crm.work_orders set paid_at = now() where id = $1",
                    order["work_order_id"])
                await conn.execute(
                    "update crm.pay_orders set status = 'paid', paid_at = now(), "
                    "checked_at = now(), error = null where id = $1", order_id)
                return None
            ledger_id = int(await conn.fetchval(
                """
                insert into crm.ledger (client_id, rental_id, kind, amount,
                                        method, note, created_by)
                values ($1, $2, 'payment', $3, $4, $5, $6) returning id
                """, order["client_id"], order["rental_id"], order["amount"],
                method, f"Счёт {order['no']}", by or "эквайринг"))
            await conn.execute(
                "update crm.pay_orders set status = 'paid', paid_at = now(), "
                "checked_at = now(), ledger_id = $2, error = null where id = $1",
                order_id, ledger_id)
            return ledger_id

    async def mark_pay_failed(self, order_id: int, *, error: str) -> None:
        await self.pool.execute(
            "update crm.pay_orders set status = 'failed', checked_at = now(), "
            "error = $2 where id = $1 and status <> 'paid'", order_id, error[:500])

    async def touch_pay_order(self, order_id: int) -> None:
        """Спросили у банка, ответ прежний. Отметка нужна, чтобы видеть,
        что опрос вообще идёт."""
        await self.pool.execute(
            "update crm.pay_orders set checked_at = now() where id = $1", order_id)

    async def cancel_pay_order(self, order_id: int, *, by: str) -> None:
        await self.pool.execute(
            "update crm.pay_orders set status = 'cancelled', checked_at = now(), "
            "error = $2 where id = $1 and status in ('new', 'sent')",
            order_id, f"снял {by}")

    async def pay_order(self, order_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            """
            select p.*, c.full_name, c.phone, c.tg_id, c.max_id, c.contract_no
              from crm.pay_orders p join crm.clients c on c.id = p.client_id
             where p.id = $1
            """, order_id))

    async def pay_orders(self, *, client_id: int | None = None,
                         status: str | None = None, limit: int = 200) -> list[dict]:
        conds: list[str] = []
        args: list[Any] = []
        if client_id is not None:
            args.append(client_id)
            conds.append(f"p.client_id = ${len(args)}")
        if status:
            args.append(status)
            conds.append(f"p.status = ${len(args)}")
        where = ("where " + " and ".join(conds)) if conds else ""
        args.append(limit)
        return _rows(await self.pool.fetch(
            f"""
            select p.*, c.full_name, c.phone, c.tg_id, c.max_id, c.contract_no
              from crm.pay_orders p join crm.clients c on c.id = p.client_id
             {where} order by p.id desc limit ${len(args)}
            """, *args))

    async def open_pay_orders(self, limit: int = 200) -> list[dict]:
        """Счета, у которых ещё можно спросить статус."""
        return _rows(await self.pool.fetch(
            """
            select p.*, c.full_name, c.phone, c.tg_id, c.max_id, c.contract_no
              from crm.pay_orders p join crm.clients c on c.id = p.client_id
             where p.status in ('new', 'sent') and p.operation_id is not null
             order by p.id limit $1
            """, limit))

    async def work_order_invoices(self, order_id: int) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select * from crm.pay_orders where work_order_id = $1 "
            "order by id desc", order_id))

    async def save_card_token(self, *, client_id: int, token: str,
                              mask: str | None = None,
                              expires: str | None = None,
                              provider: str = "tochka") -> int:
        """Привязать карту. Старая уходит: действующая карта одна, и
        частичный уникальный индекс этого же требует."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "update crm.card_tokens set active = false "
                "where client_id = $1 and provider = $2 and active",
                client_id, provider)
            return int(await conn.fetchval(
                "insert into crm.card_tokens (client_id, provider, token, mask, expires) "
                "values ($1, $2, $3, $4, $5) returning id",
                client_id, provider, token, mask, expires))

    async def card_of(self, client_id: int, provider: str = "tochka") -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.card_tokens where client_id = $1 and provider = $2 "
            "and active", client_id, provider))

    async def cards(self) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select * from crm.card_tokens where active order by client_id"))

    async def drop_card(self, client_id: int, provider: str = "tochka") -> None:
        await self.pool.execute(
            "update crm.card_tokens set active = false "
            "where client_id = $1 and provider = $2 and active", client_id, provider)

    async def touch_card(self, card_id: int) -> None:
        await self.pool.execute(
            "update crm.card_tokens set used_at = now() where id = $1", card_id)

    async def repairs_since(self, bike_id: int, since: date) -> int:
        """Сколько раз велосипед был в сервисе с даты. Нужен приглашению
        на ТО: тот, кто заезжал, зовётся зря."""
        return int(await self.pool.fetchval(
            "select count(*) from crm.bike_log where bike_id = $1 "
            "and kind = 'repair' and created_at >= $2",
            bike_id, since) or 0)

    # ─────────────────────── уведомления ───────────────────────

    async def notices(self) -> list[dict]:
        """Только то, что владелец менял. Каталог - в logic.NOTICES."""
        return _rows(await self.pool.fetch(
            "select * from crm.notices order by code"))

    async def set_notice(self, code: str, *, enabled: bool,
                         at_hour: int | None, at_minute: int = 0,
                         chat_id: str | None = None,
                         extra: dict | None = None, by: str | None = None) -> None:
        await self.pool.execute(
            """
            insert into crm.notices (code, enabled, at_hour, at_minute, chat_id,
                                     extra, updated_by, updated_at)
            values ($1, $2, $3, $4, $5, coalesce($6, '{}'::jsonb), $7, now())
            on conflict (code) do update
               set enabled = excluded.enabled, at_hour = excluded.at_hour,
                   at_minute = excluded.at_minute, chat_id = excluded.chat_id,
                   extra = excluded.extra, updated_by = excluded.updated_by,
                   updated_at = now()
            """, code, enabled, at_hour, at_minute, chat_id, extra or {}, by)

    async def log_notice(self, code: str, *, target: str, status: str,
                         client_id: int | None = None,
                         detail: str | None = None) -> None:
        await self.pool.execute(
            "insert into crm.notice_log (code, client_id, target, status, detail) "
            "values ($1, $2, $3, $4, $5)",
            code, client_id, target, status, (detail or "")[:500] or None)

    async def notice_log(self, *, code: str | None = None,
                         limit: int = 200) -> list[dict]:
        conds, args = [], []
        if code:
            args.append(code)
            conds.append(f"n.code = ${len(args)}")
        where = ("where " + " and ".join(conds)) if conds else ""
        args.append(limit)
        return _rows(await self.pool.fetch(
            f"""
            select n.*, c.full_name
              from crm.notice_log n
              left join crm.clients c on c.id = n.client_id
             {where} order by n.id desc limit ${len(args)}
            """, *args))

    async def notice_counts(self, days: int = 30) -> dict[str, int]:
        """Сколько раз уведомление уходило за период - для экрана."""
        rows = await self.pool.fetch(
            "select code, count(*) as n from crm.notice_log "
            "where status = 'sent' and created_at >= now() - make_interval(days => $1) "
            "group by code", days)
        return {r["code"]: int(r["n"]) for r in rows}

    async def purge_notice_log(self, days: int) -> int:
        return int((await self.pool.execute(
            "delete from crm.notice_log "
            "where created_at < now() - make_interval(days => $1)",
            days)).split()[-1] or 0)

    # ─────────────────────── баллы ───────────────────────

    async def grant_bonus(self, *, client_id: int, kind: str, amount: Decimal,
                          note: str | None = None, ref_id: int | None = None,
                          by: str | None = None) -> int | None:
        """Начислить баллы: запись в журнал и повод рядом.

        Обе вставки одной транзакцией. Повторный бонус за отзыв или другу
        упирается в частичный уникальный индекс - тогда в журнале тоже
        ничего не появляется, и баланс не поедет.
        """
        amount = _money(amount) or Decimal(0)
        if amount <= 0:
            return None
        async with self.pool.acquire() as conn, conn.transaction():
            ledger_id = int(await conn.fetchval(
                """
                insert into crm.ledger (client_id, kind, amount, note, created_by)
                values ($1, 'bonus', $2, $3, $4) returning id
                """, client_id, amount, note, by))
            return int(await conn.fetchval(
                """
                insert into crm.bonuses (client_id, kind, amount, ledger_id,
                                         ref_id, note, created_by)
                values ($1, $2, $3, $4, $5, $6, $7) returning id
                """, client_id, kind, amount, ledger_id, ref_id, note, by))

    async def record_bonus(self, *, client_id: int, kind: str, amount: Decimal,
                           ledger_id: int | None = None,
                           ref_id: int | None = None, note: str | None = None,
                           by: str | None = None) -> int:
        """Повод для уже сделанной записи журнала. Нужен там, где деньги
        пишет другой метод - например, бонус агенту в одной транзакции
        с закрытием приглашения."""
        return int(await self.pool.fetchval(
            """
            insert into crm.bonuses (client_id, kind, amount, ledger_id,
                                     ref_id, note, created_by)
            values ($1, $2, $3, $4, $5, $6, $7) returning id
            """, client_id, kind, _money(amount), ledger_id, ref_id, note, by))

    async def bonuses(self, *, client_id: int | None = None,
                      kind: str | None = None, since: date | None = None,
                      until: date | None = None, limit: int = 500) -> list[dict]:
        conds: list[str] = []
        args: list[Any] = []
        if client_id is not None:
            args.append(client_id)
            conds.append(f"b.client_id = ${len(args)}")
        if kind:
            args.append(kind)
            conds.append(f"b.kind = ${len(args)}")
        if since:
            args.append(since)
            conds.append(f"b.created_at >= ${len(args)}::date")
        if until:
            args.append(until)
            conds.append(f"b.created_at < (${len(args)}::date + interval '1 day')")
        where = ("where " + " and ".join(conds)) if conds else ""
        args.append(limit)
        return _rows(await self.pool.fetch(
            f"""
            select b.*, c.full_name, c.phone
              from crm.bonuses b join crm.clients c on c.id = b.client_id
             {where} order by b.id desc limit ${len(args)}
            """, *args))

    async def bonus_of(self, client_id: int, kind: str) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.bonuses where client_id = $1 and kind = $2 "
            "order by id desc limit 1", client_id, kind))

    async def payments_total(self, *, since: date, until: date) -> Decimal:
        """Сумма платежей за период - знаменатель доли баллов."""
        return _money(await self.pool.fetchval(
            "select coalesce(sum(amount), 0) from crm.ledger "
            "where kind = 'payment' and created_at >= $1::date "
            "and created_at < ($2::date + interval '1 day')",
            since, until)) or Decimal(0)

    async def referrals_since(self, since: date) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select * from crm.referrals where created_at >= $1::date", since))

    # ────────────── ввод техники в эксплуатацию ──────────────

    async def mark_bike_checked(self, bike_id: int, field: str, *, by: str,
                                photo: str | None = None) -> None:
        """Отметить поле паспорта сверенным.

        jsonb правится слиянием в базе, а не чтением-записью в коде: два
        техника, сверяющие соседние поля одновременно, иначе затёрли бы
        отметки друг друга.
        """
        mark: dict[str, Any] = {"at": datetime.now(UTC).isoformat(timespec="seconds"),
                                "by": by}
        if photo:
            mark["photo"] = photo
        await self.pool.execute(
            "update crm.bikes set checked = coalesce(checked, '{}'::jsonb) || $2::jsonb, "
            "updated_at = now() where id = $1", bike_id, {field: mark})

    async def clear_bike_check(self, bike_id: int, field: str) -> None:
        await self.pool.execute(
            "update crm.bikes set checked = coalesce(checked, '{}'::jsonb) - $2, "
            "updated_at = now() where id = $1", bike_id, field)

    async def commission_bike(self, bike_id: int, *, by: str) -> bool:
        """Выпустить в оборот. False - велосипед уже не «на сборке».

        Актёр ставится в той же транзакции: смену статуса пишет триггер
        журнала, и без set_config автор записи потерялся бы.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('crm.actor', $1, true)", by or "")
            row = await conn.fetchrow(
                """
                update crm.bikes set status = 'available', commissioned_at = now(),
                       commissioned_by = $2, updated_at = now()
                 where id = $1 and status = 'new'
                returning id
                """, bike_id, by)
            return row is not None

    async def mark_battery_checked(self, battery_id: int, field: str, *, by: str,
                                   photo: str | None = None) -> None:
        """Отметить поле паспорта батареи сверенным - слиянием в базе."""
        mark: dict[str, Any] = {"at": datetime.now(UTC).isoformat(timespec="seconds"),
                                "by": by}
        if photo:
            mark["photo"] = photo
        await self.pool.execute(
            "update crm.batteries set "
            "checked = coalesce(checked, '{}'::jsonb) || $2::jsonb, "
            "updated_at = now() where id = $1", battery_id, {field: mark})

    async def clear_battery_check(self, battery_id: int, field: str) -> None:
        await self.pool.execute(
            "update crm.batteries set checked = coalesce(checked, '{}'::jsonb) - $2, "
            "updated_at = now() where id = $1", battery_id, field)

    async def commission_battery(self, battery_id: int, *, by: str) -> bool:
        """Выпустить батарею в оборот. False - она уже не «на сборке»."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('crm.actor', $1, true)", by or "")
            row = await conn.fetchrow(
                """
                update crm.batteries set status = 'available',
                       commissioned_at = now(), commissioned_by = $2,
                       updated_at = now()
                 where id = $1 and status = 'new'
                returning id
                """, battery_id, by)
            return row is not None

    async def batteries_on_assembly(self, limit: int = 200) -> list[dict]:
        return _rows(await self.pool.fetch(
            f"{self._BATTERY_SELECT} where b.status = 'new' "
            "order by b.id desc limit $1", limit))

    async def bikes_on_assembly(self, limit: int = 200) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select * from crm.bikes where status = 'new' order by id desc limit $1",
            limit))

    # ────────────── свои шаблоны документов ──────────────

    async def doc_templates(self, kind: str | None = None) -> list[dict]:
        if kind:
            return _rows(await self.pool.fetch(
                "select * from crm.doc_templates where kind = $1 "
                "order by uploaded_at desc", kind))
        return _rows(await self.pool.fetch(
            "select * from crm.doc_templates order by kind, uploaded_at desc"))

    async def active_doc_template(self, kind: str) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.doc_templates where kind = $1 and active", kind))

    async def add_doc_template(self, *, kind: str, filename: str,
                               original: str | None, size_bytes: int,
                               sha256: str | None, by: str | None = None) -> int:
        return int(await self.pool.fetchval(
            """
            insert into crm.doc_templates (kind, filename, original, size_bytes,
                                           sha256, uploaded_by)
            values ($1, $2, $3, $4, $5, $6) returning id
            """, kind, filename, original, size_bytes, sha256, by))

    async def enable_doc_template(self, template_id: int) -> bool:
        """Включить свой шаблон. Прежний включённый того же вида уходит
        в архив: у вида всегда ровно один включённый, и уникальный
        индекс не даст обойти это стороной."""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "select kind from crm.doc_templates where id = $1", template_id)
            if row is None:
                return False
            await conn.execute(
                "update crm.doc_templates set active = false "
                "where kind = $1 and active", row["kind"])
            await conn.execute(
                "update crm.doc_templates set active = true where id = $1",
                template_id)
            return True

    async def disable_doc_templates(self, kind: str) -> None:
        """Вернуться на наш шаблон. Свой остаётся в архиве."""
        await self.pool.execute(
            "update crm.doc_templates set active = false where kind = $1", kind)

    async def doc_template(self, template_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.doc_templates where id = $1", template_id))

    async def drop_doc_template(self, template_id: int) -> dict | None:
        """Убрать из архива. Включённый не удаляем: тогда вид документа
        остался бы без шаблона вовсе."""
        return _row(await self.pool.fetchrow(
            "delete from crm.doc_templates where id = $1 and not active "
            "returning *", template_id))

    async def company_marks(self) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select * from crm.company_marks order by kind"))

    async def set_company_mark(self, kind: str, *, filename: str,
                               size_bytes: int, by: str | None = None) -> None:
        await self.pool.execute(
            """
            insert into crm.company_marks (kind, filename, size_bytes, uploaded_by,
                                           uploaded_at)
            values ($1, $2, $3, $4, now())
            on conflict (kind) do update
               set filename = excluded.filename, size_bytes = excluded.size_bytes,
                   uploaded_by = excluded.uploaded_by, uploaded_at = now()
            """, kind, filename, size_bytes, by)

    async def drop_company_mark(self, kind: str) -> None:
        await self.pool.execute(
            "delete from crm.company_marks where kind = $1", kind)

    async def part_last_moved(self) -> dict[int, datetime]:
        """Когда позицию последний раз трогали - одним запросом на склад.

        Нужен колонке «дней на складе»: запрос на позицию превратил бы
        экран остатков в сотню запросов.
        """
        rows = await self.pool.fetch(
            "select part_id, max(created_at) as moved from crm.part_moves "
            "group by part_id")
        return {int(r["part_id"]): r["moved"] for r in rows}
