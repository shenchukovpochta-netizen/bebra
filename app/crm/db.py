"""Доступ к схеме crm. Тот же пул asyncpg, что у бота, без ORM.

Все методы возвращают dict или list[dict], а не Record: строки уходят
в шаблоны Jinja и в тексты Telegram, и там удобнее словарь. Суммы
приходят Decimal - numeric в Postgres, и терять копейки во float незачем.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import asyncpg

from . import logic

# Белые списки колонок для UPDATE: имена подставляются в SQL текстом.
BIKE_FIELDS = frozenset({
    "code", "model", "frame_no", "motor_no", "battery_count", "status",
    "purchase_price", "purchased_on", "note", "location", "service_months",
    "residual_price", "battery_price", "battery_service_months", "mileage_km",
    "spare",
})
CLIENT_FIELDS = frozenset({
    "full_name", "phone", "tg_id", "username", "status", "contract_no",
    "note", "source", "channel", "ref_code", "invited_by", "invited_at",
})
TARIFF_FIELDS = frozenset({"name", "period_days", "price", "note", "active", "sort"})
WORK_TYPE_FIELDS = frozenset({"title", "category", "minutes", "price", "node",
                              "active", "sort"})
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
})
RENTAL_FIELDS = frozenset({
    "search_at", "search_by", "search_note",
    "tariff_id", "tariff_name", "period_days", "price", "billing",
    "contract_no", "bike_id", "billed_until", "notified_on", "notified_kind",
    "intent", "intent_until", "intent_by", "intent_at", "snooze_until",
    "mileage_start", "mileage_end",
})


def _rows(records: list[asyncpg.Record]) -> list[dict]:
    return [dict(r) for r in records]


def _row(record: asyncpg.Record | None) -> dict | None:
    return dict(record) if record is not None else None


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

    async def tariffs(self, *, active_only: bool = False) -> list[dict]:
        where = "where active" if active_only else ""
        return _rows(await self.pool.fetch(
            f"select * from crm.tariffs {where} order by sort, period_days, id"))

    async def tariff(self, tariff_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.tariffs where id = $1", tariff_id))

    async def create_tariff(self, name: str, period_days: int, price: Decimal,
                            note: str | None) -> int:
        return int(await self.pool.fetchval(
            "insert into crm.tariffs (name, period_days, price, note) "
            "values ($1, $2, $3, $4) returning id",
            name, period_days, price, note))

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
                            mileage_start: int | None = None) -> int:
        """Аренда и статус велосипеда - одной транзакцией.

        Уникальные индексы на активную аренду клиента и велосипеда бросают
        UniqueViolationError; вызывающий переводит его в понятное сообщение.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('crm.actor', $1, true)", created_by or "")
            rental_id = int(await conn.fetchval(
                """
                insert into crm.rentals
                  (client_id, bike_id, tariff_id, tariff_name, period_days, price,
                   billing, started_on, billed_until, contract_no, created_by,
                   mileage_start)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $8, $9, $10, $11)
                returning id
                """, client_id, bike_id, tariff_id, tariff_name, period_days,
                price, billing, started_on, contract_no, created_by, mileage_start))
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
                                created_by: str) -> int:
        """Открыть ведомость и сразу записать в неё снимок ожидаемого парка.

        Номер и строки - одной транзакцией: ведомость без строк оператор
        примет за «всё сошлось», а это просто недописанный документ.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("lock table crm.stock_takes in share row exclusive mode")
            next_no = int(await conn.fetchval(
                "select coalesce(max(substring(no from '[0-9]+$')::bigint), 0) + 1 "
                "from crm.stock_takes") or 1)
            take_id = int(await conn.fetchval(
                """
                insert into crm.stock_takes (no, scope, location, note, expected, created_by)
                values ($1, $2, $3, $4, $5, $6) returning id
                """, logic.take_no(next_no), scope, location, note,
                len(bike_ids), created_by))
            if bike_ids:
                await conn.executemany(
                    "insert into crm.stock_take_items (take_id, bike_id, state) "
                    "values ($1, $2, 'expected')",
                    [(take_id, bike_id) for bike_id in bike_ids])
            return take_id

    async def update_stock_take(self, take_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets, values = _set_clause(fields, TAKE_FIELDS, 2)
        await self.pool.execute(
            f"update crm.stock_takes set {sets} where id = $1", take_id, *values)

    _TAKE_ITEM_SELECT = """
        select i.*, b.code as bike_code, b.model as bike_model,
               b.status as bike_status, b.location as bike_location
        from crm.stock_take_items i
        left join crm.bikes b on b.id = i.bike_id
    """

    async def take_items(self, take_id: int) -> list[dict]:
        return _rows(await self.pool.fetch(
            f"{self._TAKE_ITEM_SELECT} where i.take_id = $1 "
            "order by coalesce(b.code, i.code), i.id", take_id))

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
                            state: str = "extra", note: str | None = None) -> int:
        return int(await self.pool.fetchval(
            "insert into crm.stock_take_items (take_id, bike_id, code, state, note) "
            "values ($1, $2, $3, $4, $5) returning id",
            take_id, bike_id, code, state, note))

    async def delete_take_item(self, take_id: int, item_id: int) -> bool:
        row = await self.pool.fetchrow(
            "delete from crm.stock_take_items where take_id = $1 and id = $2 "
            "returning id", take_id, item_id)
        return row is not None

    async def close_stock_take(self, take_id: int, *, counts: dict[str, int],
                               closed_at: datetime) -> list[int]:
        """Закрыть ведомость: неотмеченное становится недостачей.

        Возвращает id велосипедов, которых не нашли: что с ними делать -
        решает не база.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "update crm.stock_take_items set state = 'missing' "
                "where take_id = $1 and state = 'expected' returning bike_id", take_id)
            await conn.execute(
                """
                update crm.stock_takes
                set status = 'done', closed_at = $2, expected = $3, found = $4,
                    missing = $5, extra = $6
                where id = $1
                """, take_id, closed_at, int(counts.get("total") or 0),
                int(counts.get("found") or 0), int(counts.get("missing") or 0),
                int(counts.get("extra") or 0))
            return [int(r["bike_id"]) for r in rows if r["bike_id"] is not None]

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
