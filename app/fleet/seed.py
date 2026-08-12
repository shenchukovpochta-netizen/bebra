"""Посев справочников парка и бэкфилл из bot.users и bot.events.

Запускается при каждом старте бота, после применения fleet_schema.sql,
и весь идемпотентен. Справочники (точки, модели, тарифы) сеются только
в ПУСТЫЕ таблицы: дальше они живут в базе своей жизнью, и переименованная
оператором точка не должна задваиваться при рестарте. Источник посева -
app/faq.py: адреса и прайс живут там одним экземпляром.

Бэкфилл восстанавливает парк из того, что бот уже накопил: единицы -
из данных выдачи (ключ - вин-номер рамы), активные аренды - из подписанных
актов, закрытые - из событий rental_closed. Повторный прогон защищён
ключами: вин-номером, парой not exists, source_event_id.

Вин-номера нормализуются в Python (logic.normalize_vin), а не в SQL:
upper() в C-локали контейнерного Postgres не поднимает кириллицу, и
«ав 123» с «АВ123» стали бы двумя разными рамами - двумя единицами
в парке вместо одной.
"""

from __future__ import annotations

import json
import logging
import re

from .. import faq
from . import logic
from .db import FleetDB

log = logging.getLogger(__name__)

# Столбцы faq.TARIFF_ROWS: (название, неделя, 2 недели, месяц).
PERIODS: tuple[tuple[int, int], ...] = ((7, 1), (14, 2), (30, 3))


def _price(raw: str) -> int:
    """«3 000» -> 3000. Прайс в faq.py набран с пробелами для людей."""
    return int(re.sub(r"\D", "", raw or "") or 0)


async def ensure_seed(fleet: FleetDB) -> None:
    pool = fleet.pool

    if not await pool.fetchval("select count(*) from fleet.points"):
        for title in (faq.POINT_1, faq.POINT_2):
            await pool.execute(
                "insert into fleet.points (title, address, open_hour, close_hour) "
                "values ($1, $1, $2, $3) on conflict (title) do nothing",
                title, faq.OPEN_HOUR, faq.CLOSE_HOUR)

    if not await pool.fetchval("select count(*) from fleet.models"):
        for row in faq.TARIFF_ROWS:
            model_id = await pool.fetchval(
                "insert into fleet.models (title) values ($1) "
                "on conflict (title) do update set title = excluded.title "
                "returning id", row[0])
            for days, col in PERIODS:
                await pool.execute(
                    "insert into fleet.tariffs (model_id, period_days, price) "
                    "values ($1, $2, $3) "
                    "on conflict (model_id, period_days) do nothing",
                    model_id, days, _price(row[col]))

    await _backfill(fleet)


def _issue_of(row) -> dict:
    """issue_data строки пользователя. Строкой, а не dict, она приходит
    из базы без jsonb-кодека - бэкфилл не вправе на это упасть."""
    data = row["issue_data"]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return {}
    return dict(data or {})


async def _backfill(fleet: FleetDB) -> None:
    pool = fleet.pool
    rows = await pool.fetch(
        "select tg_id, issue_data, contract_no, act_in_signed_at, "
        "       act_out_signed_at, updated_at "
        "from bot.users where issue_data is not null")

    # Одна рама могла пройти через несколько клиентов: предпочитается
    # строка с действующей арендой, затем самая свежая.
    by_vin: dict[str, dict] = {}
    for row in rows:
        data = _issue_of(row)
        vin = logic.normalize_vin(data.get("vin_frame"))
        if not vin:
            continue
        active = (row["act_in_signed_at"] is not None
                  and row["act_out_signed_at"] is None)
        current = by_vin.get(vin)
        if current is None or ((active, row["updated_at"])
                               > (current["active"], current["row"]["updated_at"])):
            by_vin[vin] = {"vin": vin, "active": active, "row": row, "data": data}

    bikes = rentals = 0
    for cand in by_vin.values():
        data = cand["data"]
        inserted = await pool.fetchval(
            "insert into fleet.bikes (vin_frame, vin_motor, model_id, status) "
            "values ($1, $2, $3, $4) on conflict (vin_frame) do nothing "
            "returning id",
            cand["vin"],
            logic.normalize_vin(data.get("vin_motor")) or None,
            await fleet.model_id_by_title(data.get("bike_model")),
            logic.RENTED if cand["active"] else logic.FREE)
        bikes += inserted is not None

    for cand in by_vin.values():
        if not cand["active"]:
            continue
        row, data = cand["row"], cand["data"]
        # Срок возврата считается от даты ОТКРЫТИЯ аренды, а не от
        # «сегодня»: иначе просроченный «01.05 - 01.06» при бэкфилле
        # в августе уехал бы на следующий год.
        inserted = await pool.fetchval(
            """
            insert into fleet.rentals
              (bike_id, tg_id, contract_no, rent_term, rent_price,
               opened_at, due_at)
            select b.id, $2, $3, $4, $5, $6, $7
            from fleet.bikes b
            where b.vin_frame = $1
              and not exists (select 1 from fleet.rentals r
                              where r.closed_at is null
                                and (r.tg_id = $2 or r.bike_id = b.id))
            returning id
            """,
            cand["vin"], row["tg_id"], row["contract_no"],
            (data.get("rent_term") or "").strip() or None,
            (data.get("rent_price") or "").strip() or None,
            row["act_in_signed_at"],
            logic.parse_due(data.get("rent_term"),
                            today=row["act_in_signed_at"].date()))
        rentals += inserted is not None

    # Закрытые аренды: рама в payload не писалась, поэтому bike_id пуст.
    # opened_at неизвестен - берётся момент события, то есть закрытия:
    # для истории важен факт и порядок, а не длительность.
    closed = await pool.execute("""
        insert into fleet.rentals
          (tg_id, contract_no, rent_term, opened_at, closed_at,
           close_notes, source_event_id)
        select e.tg_id,
               nullif(e.payload->>'number', ''),
               nullif(nullif(e.payload->>'term', ''), '—'),
               e.created_at, e.created_at,
               'история: восстановлено из события', e.id
        from bot.events e
        where e.type = 'rental_closed' and e.tg_id is not null
        on conflict (source_event_id) do nothing
        """)

    log.info("парк: бэкфилл - единиц %s, активных аренд %s, закрытых %s",
             bikes, rentals, _count(closed))


def _count(result: str | None) -> int:
    """'INSERT 0 3' -> 3. asyncpg возвращает статус команды строкой."""
    try:
        return int((result or "").rsplit(" ", 1)[-1])
    except ValueError:
        return 0
