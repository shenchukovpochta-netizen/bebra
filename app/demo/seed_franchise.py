"""Демо: кабинет франчайзера - три вымышленных франчайзи с цифрами.

Сети в демо нет: франчайзи - строки в базе, как их оставил бы опрос
процесса бота, а «Обновить сейчас» закрыто стражем демо (запрос на чужой
сервер). Адреса - в зоне .example: её не существует, и никуда ссылка не
ведёт. Снимок каждого собран тем же metrics_payload, что отдаёт
/hook/metrics, и прошёл тот же parse_metrics - демо показывает ровно то,
что увидел бы франчайзер.

Цифры детерминированы (без rng): у демо одна история на зерно, а этот
модуль в ней не участвует и потока случайностей ядра не двигает.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from decimal import Decimal
from typing import Any

import asyncpg

from ..crm import franchise, logic, service
from .world import World, at

D = Decimal

# название, город, адрес, %, фикс, месяцев по договору, месяцев истории до
# договора, парк, простой %, чек, точки, суток без ответа (0 - свежий)
FRANCHISEES: tuple[tuple[Any, ...], ...] = (
    ("Май Байк Самара", "Самара", "https://crm.samara.example", "5", "15000", 4, 1, 64,
     D("8.5"), D("545"), ("Молодогвардейская", "Победы"), 0),
    ("Май Байк Уфа", "Уфа", "https://crm.ufa.example", "6", "10000", 3, 0, 38,
     D("13"), D("505"), ("Проспект Октября",), 0),
    ("Май Байк Пенза", "Пенза", "https://crm.penza.example", "4.5", "0", 1, 0, 21,
     D("21"), D("470"), ("Московская",), 3),
)
# Пенза стоит на прошлой сборке - так в сравнении видна метка версии.
OLD_VERSION = "0a1b2c3d4e5f"
STALE_ERROR = "франчайзи не отвечает: TimeoutError"


def franchise_key_for(secret: str) -> bytes:
    """Ключ токенов франчайзи в демо: sha256(секрет панели + "demo-franchise").
    Свой файл в secrets/ ради вымышленных токенов - лишняя вещь, которую
    забудут; к боевому FRANCHISE_KEY ключ отношения не имеет."""
    return hashlib.sha256((secret + "demo-franchise").encode("utf-8")).digest()


def franchise_key_text(secret: str) -> str:
    """Тот же ключ строкой, как его ждут WebConfig.franchise_key и Vault."""
    return base64.b64encode(franchise_key_for(secret)).decode("ascii")


def _period(fleet: int, idle: Decimal, check: Decimal, days: Decimal) -> dict[str, Any]:
    """Дни и деньги периода той же арифметикой, что у панели: парк × дни,
    простой - доля, выручка - дни аренды × чек."""
    operational = D(fleet) * days
    idle_days = operational * idle / 100
    rented = operational - idle_days
    revenue = logic.to_money(rented * check)
    return logic.fleet_metrics({"available": idle_days, "rented": rented}, revenue)


def build(spec: tuple[Any, ...], *, now: Any, version: str) -> dict[str, Any]:
    """Ответ франчайзи на момент now, как его отдал бы /hook/metrics, - уже
    проверенный."""
    (name, city, _url, _pct, _fee, months_in, before, fleet, idle, check, points,
     _silent) = spec
    moment = now
    rented = round(fleet * (100 - float(idle)) / 100)
    months = []
    for i, (first, until, partial) in enumerate(logic.metrics_months(moment)):
        if i >= months_in + before + 1:
            break
        days = D(str((until - first).total_seconds())) / 86400
        # Сеть растёт: полгода назад парк меньше, чек ниже, простой выше.
        months.append({"month": first.date(), "partial": partial,
                       **_period(max(fleet - 3 * i, 5), idle + i, check - 6 * i, days)})
    last30 = _period(fleet, idle, check, D(30))
    payload = logic.metrics_payload(
        title=name, version=version, now=moment,
        places=[{"name": p, "city": city, "active": True} for p in points],
        bikes={"rented": rented, "available": fleet - rented},
        counts={"clients": fleet * 3 + 17, "rentals": rented},
        last30=last30, months=months)
    checked = logic.parse_metrics(payload)
    if not checked.ok:                                  # pragma: no cover - сид сломан
        raise RuntimeError(f"демо-франчайзи {name}: {checked.error}")
    return checked.value


async def populate(conn: asyncpg.Connection, world: World, *, secret: str) -> None:
    """Франчайзи, их снимки и месяцы роялти - в транзакции сида."""
    vault = service.franchise_vault(franchise_key_text(secret))
    version = franchise.code_stamp()
    current = world.today.replace(day=1)
    for n, spec in enumerate(FRANCHISEES, start=1):
        name, city, url, pct, fee, months_in, _before, *_rest, silent = spec
        start = current
        for _ in range(months_in):
            start = (start - timedelta(days=1)).replace(day=1)
        # Ответ принят ночным кругом опроса, до «сейчас» сида; собран
        # франчайзи в ту же минуту.
        ok_at = min(at(world.today - timedelta(days=silent), 3.0 + n / 10),
                    world.now - timedelta(days=silent, minutes=10 * n))
        parsed = build(spec, now=ok_at, version=OLD_VERSION if silent else version)
        polled = world.now - timedelta(hours=1) if silent else ok_at
        fid = await conn.fetchval(
            """
            insert into crm.franchisees (name, city, base_url, token_enc, royalty_percent,
                                         fixed_fee, contract_start, active, note,
                                         polled_at, ok_at, error, created_at)
            values ($1, $2, $3, $4, $5, $6, $7, true, $8, $9, $10, $11, $12)
            returning id
            """, name, city, url,
            service.franchise_seal(vault, f"demo-metrics-token-{n:02d}-000000"),
            D(pct), D(fee), start + timedelta(days=4), "Вымышленный франчайзи демо-стенда",
            polled, ok_at, STALE_ERROR if silent else None, at(start, 12.0))
        await conn.execute(
            "insert into crm.franchise_snapshots (franchisee_id, taken_on, taken_at, data) "
            "values ($1, $2, $3, $4)", fid, ok_at.date(), ok_at, logic.metrics_json(parsed))
        for m in logic.metrics_month_rows(parsed):
            await conn.execute(
                """
                insert into crm.franchise_months (franchisee_id, month, revenue,
                    idle_percent, avg_check, operational_days, rented_days, partial,
                    royalty_percent, fixed_fee, reported_at)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """, fid, m["month"], m["revenue"], m["idle_percent"], m["avg_check"],
                m["operational_days"], m["rented_days"], m["partial"], D(pct), D(fee),
                ok_at)

