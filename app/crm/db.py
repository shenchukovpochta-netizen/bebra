"""Доступ к схеме crm. Тот же пул asyncpg, что у бота, без ORM.

Все методы возвращают dict или list[dict], а не Record: строки уходят
в шаблоны Jinja и в тексты Telegram, и там удобнее словарь. Суммы
приходят Decimal - numeric в Postgres, и терять копейки во float незачем.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg

# Белые списки колонок для UPDATE: имена подставляются в SQL текстом.
BIKE_FIELDS = frozenset({
    "code", "model", "frame_no", "motor_no", "battery_count", "status",
    "purchase_price", "purchased_on", "note",
})
CLIENT_FIELDS = frozenset({
    "full_name", "phone", "tg_id", "username", "status", "contract_no",
    "note", "source",
})
TARIFF_FIELDS = frozenset({"name", "period_days", "price", "note", "active", "sort"})
RENTAL_FIELDS = frozenset({
    "tariff_id", "tariff_name", "period_days", "price", "billing",
    "contract_no", "bike_id", "billed_until", "notified_on", "notified_kind",
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

    async def staff_by_login(self, login: str) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.staff where login = $1", login))

    async def staff_by_id(self, staff_id: int) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.staff where id = $1", staff_id))

    async def staff_all(self) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select * from crm.staff order by active desc, id"))

    async def create_staff(self, login: str, password_hash: str, name: str,
                           role: str) -> int:
        return int(await self.pool.fetchval(
            "insert into crm.staff (login, password_hash, name, role) "
            "values ($1, $2, $3, $4) returning id",
            login, password_hash, name, role))

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
                    limit: int = 500) -> list[dict]:
        """Список с текущим арендатором - одним запросом, без N+1."""
        conds, args = [], []
        if status:
            args.append(status)
            conds.append(f"b.status = ${len(args)}")
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
        return _row(await self.pool.fetchrow(
            "select * from crm.bikes where frame_no = $1", frame_no))

    async def bike_by_code(self, code: str) -> dict | None:
        return _row(await self.pool.fetchrow(
            "select * from crm.bikes where code = $1", code))

    async def create_bike(self, **fields: Any) -> int:
        unknown = set(fields) - BIKE_FIELDS
        if unknown:
            raise ValueError(f"недопустимые колонки: {sorted(unknown)}")
        cols = list(fields)
        placeholders = ", ".join(f"${i}" for i in range(1, len(cols) + 1))
        return int(await self.pool.fetchval(
            f"insert into crm.bikes ({', '.join(cols)}) values ({placeholders}) "
            f"returning id", *[fields[c] for c in cols]))

    async def update_bike(self, bike_id: int, **fields: Any) -> None:
        sets, values = _set_clause(fields, BIKE_FIELDS, 2)
        await self.pool.execute(
            f"update crm.bikes set {sets}, updated_at = now() where id = $1",
            bike_id, *values)

    async def bike_counts(self) -> dict[str, int]:
        rows = await self.pool.fetch(
            "select status, count(*) as n from crm.bikes group by status")
        return {r["status"]: int(r["n"]) for r in rows}

    async def bike_log(self, bike_id: int, limit: int = 50) -> list[dict]:
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
        conds, args = [], []
        if status:
            args.append(status)
            conds.append(f"c.status = ${len(args)}")
        if q:
            args.append(f"%{q.strip()}%")
            conds.append(f"(c.full_name ilike ${len(args)} or c.phone ilike ${len(args)} "
                         f"or c.contract_no ilike ${len(args)} "
                         f"or c.username ilike ${len(args)})")
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
                            created_by: str | None) -> int:
        """Аренда и статус велосипеда - одной транзакцией.

        Уникальные индексы на активную аренду клиента и велосипеда бросают
        UniqueViolationError; вызывающий переводит его в понятное сообщение.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            rental_id = int(await conn.fetchval(
                """
                insert into crm.rentals
                  (client_id, bike_id, tariff_id, tariff_name, period_days, price,
                   billing, started_on, billed_until, contract_no, created_by)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $8, $9, $10)
                returning id
                """, client_id, bike_id, tariff_id, tariff_name, period_days,
                price, billing, started_on, contract_no, created_by))
            if bike_id is not None:
                await conn.execute(
                    "update crm.bikes set status = 'rented', updated_at = now() "
                    "where id = $1", bike_id)
            return rental_id

    async def update_rental(self, rental_id: int, **fields: Any) -> None:
        sets, values = _set_clause(fields, RENTAL_FIELDS, 2)
        await self.pool.execute(
            f"update crm.rentals set {sets}, updated_at = now() where id = $1",
            rental_id, *values)

    async def close_rental(self, rental_id: int, *, closed_on: date,
                           note: str | None, bike_status: str = "available") -> bool:
        """Закрыть аренду и освободить велосипед. False - уже закрыта."""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                update crm.rentals
                   set status = 'closed', closed_on = $2, close_note = $3,
                       updated_at = now()
                 where id = $1 and status = 'active'
                returning bike_id
                """, rental_id, closed_on, note)
            if row is None:
                return False
            if row["bike_id"] is not None:
                await conn.execute(
                    "update crm.bikes set status = $2, updated_at = now() "
                    "where id = $1 and status = 'rented'", row["bike_id"], bike_status)
            return True

    async def charge_period(self, rental_id: int, client_id: int, *,
                            period_from: date, period_to: date, amount: Decimal,
                            note: str, created_by: str = "billing") -> bool:
        """Начислить период и сдвинуть billed_until - одной транзакцией.

        Повтор того же периода (второй проход, ручной запуск) упирается
        в уникальный индекс и возвращает False, ничего не списав.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            try:
                await conn.execute(
                    """
                    insert into crm.ledger
                      (client_id, rental_id, kind, amount, period_from, period_to,
                       note, created_by)
                    values ($1, $2, 'charge', $3, $4, $5, $6, $7)
                    """, client_id, rental_id, amount, period_from, period_to,
                    note, created_by)
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
                         period_to: date | None = None) -> int:
        return int(await self.pool.fetchval(
            """
            insert into crm.ledger (client_id, rental_id, kind, amount, method,
                                    note, created_by, period_from, period_to)
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9) returning id
            """, client_id, rental_id, kind, amount, method, note, created_by,
            period_from, period_to))

    async def ledger_of(self, client_id: int, limit: int = 100) -> list[dict]:
        return _rows(await self.pool.fetch(
            "select * from crm.ledger where client_id = $1 order by id desc limit $2",
            client_id, limit))

    async def ledger(self, *, since: date | None = None, until: date | None = None,
                     kind: str | None = None, limit: int = 1000) -> list[dict]:
        conds, args = [], []
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
        conds, args = [], []
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
