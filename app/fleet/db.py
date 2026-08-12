"""Доступ к схеме fleet. Тонкий слой поверх того же пула, что и bot.*.

Свой класс, а не методы в Database: у парка свои инварианты (одна живая
бронь и одна активная аренда на единицу), и все переходы статусов, где
участвуют две таблицы, идут в транзакции - удержание без смены статуса
единицы или наоборот оставляло бы парк враным.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

import asyncpg

from . import logic

log = logging.getLogger(__name__)

# Белый список колонок для UPDATE единицы - по тем же причинам, что
# PATCHABLE в app/db.py: имена колонок подставляются в SQL текстом.
BIKE_PATCHABLE = frozenset({
    "model_id", "point_id", "vin_motor", "status", "battery_count", "notes",
})


class FleetDB:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def apply_schema(self, path: Path) -> None:
        await self.pool.execute(path.read_text(encoding="utf-8"))

    # ─────────────────────── справочники ───────────────────────

    async def model_id_by_title(self, title: str | None) -> int | None:
        """Модель по названию, без учёта регистра. None - не нашли:
        единица заводится и без модели, карточку оператор дозаполнит."""
        if not (title or "").strip():
            return None
        return await self.pool.fetchval(
            "select id from fleet.models where lower(title) = lower($1)",
            title.strip(),
        )

    async def point_id_by_title(self, needle: str | None) -> int | None:
        """Точка по вхождению: оператор пишет «Адоратского», в базе -
        «Адоратского, 11А». Совпадений несколько - берётся младшая по id,
        то есть заведённая раньше."""
        if not (needle or "").strip():
            return None
        return await self.pool.fetchval(
            "select id from fleet.points "
            "where is_active and title ilike '%' || $1 || '%' "
            "order by id limit 1",
            needle.strip(),
        )

    async def points(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "select id, title, address, lat, lon, phone, open_hour, close_hour "
            "from fleet.points where is_active order by id")

    async def models_with_tariffs(self) -> list[dict]:
        """Каталог для API: модели с тарифами одной структурой."""
        models = await self.pool.fetch(
            "select id, title, extend_price, photo_url, photo_page, "
            "       description, specs "
            "from fleet.models where is_active order by id")
        tariffs = await self.pool.fetch(
            "select model_id, period_days, price from fleet.tariffs "
            "where is_active order by model_id, period_days")
        by_model: dict[int, list[dict]] = {}
        for t in tariffs:
            by_model.setdefault(t["model_id"], []).append(
                {"period_days": t["period_days"], "price": t["price"]})
        return [{"id": m["id"], "title": m["title"],
                 "extend_price": m["extend_price"],
                 "photo": m["photo_url"], "photo_page": m["photo_page"],
                 "description": m["description"], "specs": m["specs"] or [],
                 "tariffs": by_model.get(m["id"], [])} for m in models]

    # ─────────────────────── единицы ───────────────────────

    async def upsert_bike(self, vin_frame: str, *, vin_motor: str | None = None,
                          model_id: int | None = None, point_id: int | None = None,
                          battery_count: int | None = None, status: str | None = None,
                          notes: str | None = None,
                          conn: asyncpg.Connection | None = None) -> asyncpg.Record:
        """Единица по вин-номеру рамы: обновить или завести.

        None означает «не трогать»: форма /bike с одной строкой «мотор:»
        не должна сбрасывать точку и заметку, а автозавод при выдаче -
        затирать то, что оператор заполнил руками.
        """
        return await (conn or self.pool).fetchrow(
            """
            insert into fleet.bikes
              (vin_frame, vin_motor, model_id, point_id, battery_count, status, notes)
            values ($1, $2, $3, $4, coalesce($5, 2), coalesce($6, 'free'), $7)
            on conflict (vin_frame) do update set
              vin_motor     = coalesce(excluded.vin_motor, fleet.bikes.vin_motor),
              model_id      = coalesce(excluded.model_id, fleet.bikes.model_id),
              point_id      = coalesce(excluded.point_id, fleet.bikes.point_id),
              battery_count = coalesce($5, fleet.bikes.battery_count),
              status        = coalesce($6, fleet.bikes.status),
              notes         = coalesce(excluded.notes, fleet.bikes.notes),
              updated_at    = now()
            returning *
            """,
            logic.normalize_vin(vin_frame), vin_motor or None, model_id, point_id,
            battery_count, status, notes or None,
        )

    async def get_bike(self, ref: str) -> asyncpg.Record | None:
        """Единица по ссылке оператора: номер из /bikes либо вин рамы.

        Сначала id, потом вин - вин бывает и чисто цифровым, по виду
        не угадать.
        """
        ref = (ref or "").strip()
        if not ref:
            return None
        if ref.isdigit():
            row = await self.pool.fetchrow(
                "select * from fleet.bikes where id = $1", int(ref))
            if row is not None:
                return row
        return await self.pool.fetchrow(
            "select * from fleet.bikes where vin_frame = $1",
            logic.normalize_vin(ref))

    async def bike_card(self, bike_id: int) -> asyncpg.Record | None:
        """Одна единица с названиями модели и точки - для карточки-ответа."""
        return await self.pool.fetchrow(
            """
            select b.id, b.vin_frame, b.vin_motor, b.status, b.notes,
                   b.battery_count, m.title as model, p.title as point
            from fleet.bikes b
            left join fleet.models m on m.id = b.model_id
            left join fleet.points p on p.id = b.point_id
            where b.id = $1
            """, bike_id)

    async def patch_bike(self, bike_id: int, **fields: Any) -> bool:
        unknown = set(fields) - BIKE_PATCHABLE
        if unknown:
            raise ValueError(f"недопустимые колонки: {sorted(unknown)}")
        if not fields:
            return True
        cols = list(fields)
        sets = [f"{col} = ${i}" for i, col in enumerate(cols, start=2)]
        assignments = ", ".join([*sets, "updated_at = now()"])
        row = await self.pool.fetchrow(
            f"update fleet.bikes set {assignments} where id = $1 returning id",
            bike_id, *[fields[col] for col in cols],
        )
        return row is not None

    async def park_counts(self) -> list[asyncpg.Record]:
        """Строки (точка, модель, статус, количество) для сводки и API."""
        return await self.pool.fetch(
            """
            select p.title as point, m.title as model, b.status, count(*)::int as count
            from fleet.bikes b
            left join fleet.points p on p.id = b.point_id
            left join fleet.models m on m.id = b.model_id
            group by 1, 2, 3
            """)

    async def list_bikes(self, limit: int = 50) -> list[asyncpg.Record]:
        """Список для /bikes: единица + кто её держит.

        ФИО арендатора берётся из bot.users - список уходит в служебный
        чат, где карточки заявок и так его содержат.
        """
        return await self.pool.fetch(
            """
            select b.id, b.vin_frame, b.vin_motor, b.status, b.notes,
                   m.title as model, p.title as point,
                   u.full_name as renter_name, u.username as renter_username,
                   bk.note as hold_note
            from fleet.bikes b
            left join fleet.models m on m.id = b.model_id
            left join fleet.points p on p.id = b.point_id
            left join fleet.rentals r on r.bike_id = b.id and r.closed_at is null
            left join bot.users u on u.tg_id = r.tg_id
            left join fleet.bookings bk on bk.bike_id = b.id and bk.status = 'held'
            order by b.id
            limit $1
            """, limit)

    # ─────────────────────── брони ───────────────────────

    async def hold(self, bike_id: int, minutes: int, note: str,
                   created_by: int | None) -> bool:
        """Удержать единицу. False - она не свободна.

        Сначала guard-UPDATE статуса, потом бронь, и всё в транзакции:
        два оператора, удержавшие одну единицу одновременно, иначе оба
        увидели бы «готово».
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                updated = await conn.fetchrow(
                    "update fleet.bikes set status = 'booked', updated_at = now() "
                    "where id = $1 and status = 'free' returning id",
                    bike_id)
                if updated is None:
                    return False
                try:
                    await conn.execute(
                        "insert into fleet.bookings "
                        "  (bike_id, note, hold_expires_at, created_by) "
                        "values ($1, $2, now() + ($3 || ' minutes')::interval, $4)",
                        bike_id, note or None, str(int(minutes)), created_by)
                except asyncpg.UniqueViolationError:
                    # Осиротевшая живая бронь (единицу освободили мимо
                    # /unhold). Честное «не свободна» вместо трейсбека:
                    # транзакция откатится, статус вернётся как был.
                    return False
        return True

    async def unhold(self, bike_id: int) -> bool:
        """Снять живую бронь. False - удерживать было нечего."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "update fleet.bookings set status = 'cancelled', updated_at = now() "
                    "where bike_id = $1 and status = 'held' returning id",
                    bike_id)
                if row is None:
                    return False
                await conn.execute(
                    "update fleet.bikes set status = 'free', updated_at = now() "
                    "where id = $1 and status = 'booked'",
                    bike_id)
        return True

    async def expire_holds(self) -> int:
        """Снять просроченные удержания. Возвращает, сколько снято."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    "update fleet.bookings set status = 'expired', updated_at = now() "
                    "where status = 'held' and hold_expires_at < now() "
                    "returning bike_id")
                if rows:
                    await conn.execute(
                        "update fleet.bikes set status = 'free', updated_at = now() "
                        "where id = any($1::int[]) and status = 'booked'",
                        [r["bike_id"] for r in rows])
        return len(rows)

    async def cancel_holds(self, bike_id: int,
                           conn: asyncpg.Connection | None = None) -> None:
        """Снять живые брони единицы, не трогая её статус.

        Вызывается при любой явной смене статуса оператором (set_status
        и форма /bike): оставленная held-бронь на свободной единице -
        это мина, о которую следующий /hold споткнётся об уникальный индекс.
        """
        await (conn or self.pool).execute(
            "update fleet.bookings set status = 'cancelled', updated_at = now() "
            "where bike_id = $1 and status = 'held'",
            bike_id)

    async def set_status(self, bike_id: int, status: str,
                         note: str | None = None) -> None:
        """Перевод руками: /service, /free. Живая бронь при этом снимается -
        оператор, отправляющий единицу в ремонт, решил за бронь тоже."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self.cancel_holds(bike_id, conn=conn)
                await conn.execute(
                    "update fleet.bikes set status = $2, "
                    "notes = coalesce($3, notes), updated_at = now() where id = $1",
                    bike_id, status, note or None)

    # ─────────────────────── бронь из приложения ───────────────────────

    async def active_rental_of(self, tg_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            """
            select r.id, r.contract_no, r.rent_term, r.rent_price, r.due_at,
                   r.opened_at, b.id as bike_id, m.title as model
            from fleet.rentals r
            left join fleet.bikes b on b.id = r.bike_id
            left join fleet.models m on m.id = b.model_id
            where r.tg_id = $1 and r.closed_at is null
            """, tg_id)

    async def active_booking_of(self, tg_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            """
            select bk.id, bk.pickup_at, bk.hold_expires_at, bk.created_at,
                   b.id as bike_id, m.title as model, p.title as point,
                   p.address
            from fleet.bookings bk
            join fleet.bikes b on b.id = bk.bike_id
            left join fleet.models m on m.id = b.model_id
            left join fleet.points p on p.id = b.point_id
            where bk.tg_id = $1 and bk.status = 'held'
            """, tg_id)

    async def rentals_history(self, tg_id: int, limit: int = 10) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """
            select r.contract_no, r.rent_term, r.opened_at, r.closed_at,
                   m.title as model
            from fleet.rentals r
            left join fleet.bikes b on b.id = r.bike_id
            left join fleet.models m on m.id = b.model_id
            where r.tg_id = $1 and r.closed_at is not null
            order by r.id desc limit $2
            """, tg_id, limit)

    async def book_model(self, tg_id: int, *, model_id: int,
                         point_id: int | None, pickup_at, hold_minutes: int,
                         note: str) -> tuple[asyncpg.Record | None, str]:
        """Бронь из приложения: удержать свободную единицу нужной модели.

        Возвращает (бронь, "") либо (None, код ошибки): rental_active -
        велосипед уже на руках, booking_exists - живая бронь уже есть,
        no_free - свободных единиц нет.

        for update skip locked - от гонки двух клиентов за последнюю
        единицу: второй не ждёт чужую транзакцию, а сразу берёт следующую
        строку или честно получает no_free.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if await conn.fetchrow(
                        "select 1 from fleet.rentals "
                        "where tg_id = $1 and closed_at is null", tg_id):
                    return None, "rental_active"
                if await conn.fetchrow(
                        "select 1 from fleet.bookings "
                        "where tg_id = $1 and status = 'held'", tg_id):
                    return None, "booking_exists"
                bike = await conn.fetchrow(
                    "select id from fleet.bikes "
                    "where status = 'free' and model_id = $1 "
                    "  and ($2::int is null or point_id = $2) "
                    "order by id limit 1 for update skip locked",
                    model_id, point_id)
                if bike is None:
                    return None, "no_free"
                await conn.execute(
                    "update fleet.bikes set status = 'booked', updated_at = now() "
                    "where id = $1", bike["id"])
                try:
                    row = await conn.fetchrow(
                        "insert into fleet.bookings "
                        "  (bike_id, tg_id, note, pickup_at, hold_expires_at, source) "
                        "values ($1, $2, $3, $4, "
                        "        now() + ($5 || ' minutes')::interval, 'app') "
                        "returning id, hold_expires_at",
                        bike["id"], tg_id, note or None, pickup_at,
                        str(int(hold_minutes)))
                except asyncpg.UniqueViolationError:
                    # Параллельная бронь того же клиента успела первой.
                    return None, "booking_exists"
        return row, ""

    async def cancel_booking(self, tg_id: int) -> int | None:
        """Клиент отменяет свою бронь. Возвращает bike_id либо None."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "update fleet.bookings set status = 'cancelled', "
                    "updated_at = now() "
                    "where tg_id = $1 and status = 'held' returning bike_id",
                    tg_id)
                if row is None:
                    return None
                await conn.execute(
                    "update fleet.bikes set status = 'free', updated_at = now() "
                    "where id = $1 and status = 'booked'",
                    row["bike_id"])
        return row["bike_id"]

    # ─────────────────────── хуки проката ───────────────────────

    async def open_rental(self, tg_id: int, *, vin_frame: str,
                          vin_motor: str | None, model_title: str | None,
                          contract_no: str | None, rent_term: str | None,
                          rent_price: str | None, due_at: date | None) -> None:
        """Акт приёма-передачи подписан - аренда открывается в учёте.

        Единица заводится по вин-номеру сама: парк наполняется из живого
        потока выдач, а не ручным переписыванием. Незакрытые хвосты -
        прошлая аренда этого клиента или этой рамы - закрываются с пометкой:
        в реальности велосипед уже передали, и учёт обязан догнать жизнь,
        а не заблокировать выдачу.
        """
        vin = logic.normalize_vin(vin_frame)
        if not vin:
            return
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                model_id = None
                if (model_title or "").strip():
                    model_id = await conn.fetchval(
                        "select id from fleet.models where lower(title) = lower($1)",
                        model_title.strip())
                bike = await self.upsert_bike(
                    vin, vin_motor=logic.normalize_vin(vin_motor) or None,
                    model_id=model_id, status=logic.RENTED, conn=conn)
                await conn.execute(
                    "update fleet.rentals set closed_at = now(), "
                    "close_notes = 'закрыта автоматически: новая выдача' "
                    "where closed_at is null and (tg_id = $1 or bike_id = $2)",
                    tg_id, bike["id"])
                # Живая бронь на эту раму выдана - каким бы клиентом она
                # ни была помечена: единицу только что передали в руки.
                await conn.execute(
                    "update fleet.bookings set status = 'issued', updated_at = now() "
                    "where bike_id = $1 and status = 'held'",
                    bike["id"])
                await conn.execute(
                    "insert into fleet.rentals "
                    "  (bike_id, tg_id, contract_no, rent_term, rent_price, due_at) "
                    "values ($1, $2, $3, $4, $5, $6)",
                    bike["id"], tg_id, contract_no or None, rent_term or None,
                    rent_price or None, due_at)

    async def close_rental(self, tg_id: int, *, notes: str | None,
                           to_service: bool) -> int | None:
        """Акт возврата подписан - аренда закрывается, единица свободна.

        to_service - в форме закрытия указаны повреждения: единица уходит
        в сервис, а не на витрину. Возвращает bike_id либо None, если
        активной аренды в учёте не было (старые клиенты до бэкфилла).
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "update fleet.rentals set closed_at = now(), close_notes = $2 "
                    "where tg_id = $1 and closed_at is null returning bike_id",
                    tg_id, notes or None)
                if row is None or row["bike_id"] is None:
                    return None
                await conn.execute(
                    "update fleet.bikes set status = $2, updated_at = now() "
                    "where id = $1 and status = 'rented'",
                    row["bike_id"], logic.SERVICE if to_service else logic.FREE)
        return row["bike_id"]

