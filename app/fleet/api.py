"""HTTP-витрина парка: точки, каталог, наличие. Только чтение, только JSON.

Это «API парка, точек и тарифов» для будущего приложения: главная страница
Mini App будет собираться из этих трёх ответов. Наружу уходит ровно то,
что видно с витрины: адреса точек, модели с ценами и счётчики наличия.
Ни вин-номеров, ни людей, ни договоров здесь нет - поэтому нет и
аутентификации: скрывать нечего, а первый экран обязан открываться
до всякого входа, как ветка частых вопросов открывается до подписки.

aiohttp - не новая зависимость: на нём работает сам aiogram. Сервер
поднимается только при заданном API_PORT: у бота без приложения лишний
открытый порт не нужен.
"""

from __future__ import annotations

import json
import logging

from aiohttp import web

from . import logic
from .db import FleetDB

log = logging.getLogger(__name__)


def _json(payload) -> web.Response:
    # ensure_ascii=False: адреса и названия моделей - кириллица, и клиент
    # приложения должен видеть текст, а не \u-последовательности.
    return web.json_response(
        payload, dumps=lambda obj: json.dumps(obj, ensure_ascii=False))


def build_app(fleet: FleetDB) -> web.Application:
    async def health(_request: web.Request) -> web.Response:
        return _json({"ok": True})

    async def points(_request: web.Request) -> web.Response:
        rows = await fleet.points()
        return _json([
            {"id": r["id"], "title": r["title"], "address": r["address"],
             "open_hour": r["open_hour"], "close_hour": r["close_hour"]}
            for r in rows
        ])

    async def models(_request: web.Request) -> web.Response:
        return _json(await fleet.models_with_tariffs())

    async def availability(_request: web.Request) -> web.Response:
        rows = await fleet.park_counts()
        return _json(logic.availability_payload(
            [(r["point"], r["model"], r["status"], r["count"]) for r in rows]))

    app = web.Application()
    app.add_routes([
        web.get("/api/health", health),
        web.get("/api/points", points),
        web.get("/api/models", models),
        web.get("/api/availability", availability),
    ])
    return app


async def start_api(fleet: FleetDB, port: int) -> web.AppRunner:
    """Поднимает витрину и возвращает runner - его гасит main() при остановке."""
    runner = web.AppRunner(build_app(fleet))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("витрина парка слушает порт %s", port)
    return runner
