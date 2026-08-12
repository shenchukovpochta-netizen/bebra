"""Посев справочников парка и бэкфилл из bot.users и bot.events.

Запускается при каждом старте бота, после применения fleet_schema.sql.
Всё идемпотентно: справочники сеются insert on conflict do nothing, поэтому
правки оператора в базе (переименовал точку, поменял цену) переживают
рестарт; бэкфилл защищён ключами - вин-номером рамы у единиц, парой
not exists у активных аренд, source_event_id у закрытых.

Источник справочников - app/faq.py: адреса точек и прайс живут там одним
экземпляром, и сеять их из второй копии значило бы поменять цену в одном
месте и забыть в другом.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from .. import faq
from . import logic
from .db import FleetDB

log = logging.getLogger(__name__)

# Столбцы faq.TARIFF_ROWS: (название, неделя, 2 недели, месяц).
PERIODS: tuple[tuple[int, int], ...] = ((7, 1), (14, 2), (30, 3))

# Нормализация вин-номера в SQL - та же, что logic.normalize_vin в Python:
# пробельные символы вон, буквы прописные. Два написания одной рамы
# обязаны совпасть и здесь, и там.
_VIN_SQL = "regexp_replace(upper(coalesce({expr}, '')), '\\s', '', 'g')"


def _price(raw: str) -> int:
    """«3 000» -> 3000. Прайс в faq.py набран с пробелами для людей."""
    return int(re.sub(r"\D", "", raw or "") or 0)


async def ensure_seed(fleet: FleetDB) -> None:
    pool = fleet.pool

    for title in (faq.POINT_1, faq.POINT_2):
        await pool.execute(
            "insert into fleet.points (title, address, open_hour, close_hour) "
            "values ($1, $1, $2, $3) on conflict (title) do nothing",
            title, faq.OPEN_HOUR, faq.CLOSE_HOUR)

    for row in faq.TARIFF_ROWS:
        # do update вместо do nothing - иначе на конфликте returning пуст
        # и id существующей модели не узнать одним запросом.
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


async def _backfill(fleet: FleetDB) -> None:
    """Единицы и аренды из того, что бот уже знает.

    До появления парка велосипед существовал только строками в issue_data.
    Отсюда восстанавливаются: единицы (по вин-номеру рамы), активные аренды
    (акт приёма подписан, акт возврата - нет) и закрытые (события
    rental_closed; у них может не быть рамы - история важнее полноты).
    """
    pool = fleet.pool
    vin = _VIN_SQL.format(expr="u.issue_data->>'vin_frame'")
    vin_motor = _VIN_SQL.format(expr="u.issue_data->>'vin_motor'")

    # Одна рама могла пройти через несколько клиентов: предпочитается
    # строка с действующей арендой, затем самая свежая.
    bikes = await pool.execute(f"""
        insert into fleet.bikes (vin_frame, vin_motor, model_id, status)
        select distinct on (v.vin)
               v.vin, nullif({vin_motor.replace("u.", "v.")}, ''), m.id,
               case when v.act_in_signed_at is not null
                         and v.act_out_signed_at is null
                    then 'rented' else 'free' end
        from (select u.*, {vin} as vin from bot.users u) v
        left join fleet.models m
               on lower(m.title) = lower(v.issue_data->>'bike_model')
        where v.vin <> ''
        order by v.vin,
                 (v.act_in_signed_at is not null
                  and v.act_out_signed_at is null) desc,
                 v.updated_at desc
        on conflict (vin_frame) do nothing
        """)

    rentals = await pool.execute(f"""
        insert into fleet.rentals
          (bike_id, tg_id, contract_no, rent_term, rent_price, opened_at)
        select distinct on (b.id)
               b.id, u.tg_id, u.contract_no,
               nullif(u.issue_data->>'rent_term', ''),
               nullif(u.issue_data->>'rent_price', ''),
               u.act_in_signed_at
        from bot.users u
        join fleet.bikes b on b.vin_frame = {vin}
        where u.act_in_signed_at is not null
          and u.act_out_signed_at is null
          and not exists (select 1 from fleet.rentals r
                          where r.closed_at is null
                            and (r.tg_id = u.tg_id or r.bike_id = b.id))
        order by b.id, u.act_in_signed_at desc
        """)

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

    # Срок возврата активных аренд - из строки «03.08 - 10.08». Разбор
    # в Python, а не в SQL: правила (год, переход через Новый год) уже
    # написаны и оттестированы в logic.parse_due.
    filled = 0
    for row in await fleet.rentals_open():
        due = logic.parse_due(row["rent_term"], today=date.today())
        if due is not None:
            await fleet.set_due(row["id"], due)
            filled += 1

    log.info("парк: бэкфилл - единиц %s, активных аренд %s, закрытых %s, "
             "сроков проставлено %s",
             _count(bikes), _count(rentals), _count(closed), filled)


def _count(result: str | None) -> int:
    """'INSERT 0 3' -> 3. asyncpg возвращает статус команды строкой."""
    try:
        return int((result or "").rsplit(" ", 1)[-1])
    except ValueError:
        return 0
