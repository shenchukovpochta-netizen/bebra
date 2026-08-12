"""HTTP-сторона парка: витрина, Mini App и бронь из него.

Витрина (/api/points, /api/models, /api/availability) отдаётся без входа:
это будущая главная страница приложения, и первый экран обязан открываться
до всякой регистрации - как ветка частых вопросов открывается до подписки.
Наружу уходит ровно то, что видно с витрины: адреса, цены и счётчики.
Ни вин-номеров, ни людей.

Личное (/api/me, /api/book, /api/cancel) требует initData - строку,
которую Telegram подписал токеном бота (app/fleet/webauth.py). Проверка
на сервере обязательна: заголовок с tg_id может написать кто угодно.

Сам Mini App - один файл webapp.html без сборки, отдаётся с корня.
aiohttp - не новая зависимость: на нём работает aiogram. Сервер
поднимается только при заданном API_PORT.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from aiohttp import web

from . import logic, webauth
from .db import FleetDB

log = logging.getLogger(__name__)

WEBAPP_FILE = Path(__file__).resolve().parent / "webapp.html"

# Коды ошибок брони -> текст клиенту. Тексты здесь, а не в texts.py:
# это ответы HTTP-API, у них нет .format-подстановок, и живут они
# вместе со своими кодами.
BOOK_ERRORS = {
    "rental_active": "Велосипед уже у вас на руках - новая бронь не нужна.",
    "booking_exists": "У вас уже есть живая бронь. Отмените её, чтобы сделать новую.",
    "no_free": "Свободных единиц этой модели на точке не осталось.",
}


def _json(payload, status: int = 200) -> web.Response:
    # ensure_ascii=False: адреса и названия моделей - кириллица, и клиент
    # приложения должен видеть текст, а не \u-последовательности.
    return web.json_response(
        payload, status=status,
        dumps=lambda obj: json.dumps(obj, ensure_ascii=False, default=str))


def _fmt(dt) -> str:
    """Момент для клиента: местное время сервера, без секунд и зоны."""
    if dt is None:
        return ""
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%d.%m %H:%M")


class Api:
    """Обработчики держат зависимости полем, а не глобалом: тестам и второму
    экземпляру (если появится) не придётся делить состояние модуля."""

    def __init__(self, fleet: FleetDB, *, bot=None, admin_chat_id: int | None = None,
                 bot_token: str = "") -> None:
        self.fleet = fleet
        self.bot = bot
        self.admin_chat_id = admin_chat_id
        self.bot_token = bot_token
        # Файл читается на старте, а не на каждый запрос: он не меняется
        # без рестарта, а отсутствие должно обнаружиться сразу.
        self.webapp = WEBAPP_FILE.read_text(encoding="utf-8")

    # ─────────────────────── витрина ───────────────────────

    async def index(self, _request: web.Request) -> web.Response:
        return web.Response(text=self.webapp, content_type="text/html")

    async def health(self, _request: web.Request) -> web.Response:
        return _json({"ok": True})

    async def points(self, _request: web.Request) -> web.Response:
        rows = await self.fleet.points()
        return _json([
            {"id": r["id"], "title": r["title"], "address": r["address"],
             "open_hour": r["open_hour"], "close_hour": r["close_hour"]}
            for r in rows
        ])

    async def models(self, _request: web.Request) -> web.Response:
        return _json(await self.fleet.models_with_tariffs())

    async def availability(self, _request: web.Request) -> web.Response:
        rows = await self.fleet.park_counts()
        return _json(logic.availability_payload(
            [(r["point"], r["model"], r["status"], r["count"]) for r in rows]))

    # ─────────────────────── личное ───────────────────────

    def _client(self, request: web.Request) -> dict | None:
        """Пользователь из initData либо None. Заголовок X-Telegram-Init-Data
        шлёт webapp.html при каждом запросе."""
        return webauth.parse_init_data(
            request.headers.get("X-Telegram-Init-Data"), self.bot_token)

    async def me(self, request: web.Request) -> web.Response:
        user = self._client(request)
        if user is None:
            return _json({"error": "auth"}, status=401)
        tg_id = user["id"]
        booking = await self.fleet.active_booking_of(tg_id)
        rental = await self.fleet.active_rental_of(tg_id)
        history = await self.fleet.rentals_history(tg_id)
        return _json({
            "booking": booking and {
                "bike_id": booking["bike_id"], "model": booking["model"],
                "point": booking["point"], "address": booking["address"],
                "pickup_at": _fmt(booking["pickup_at"]),
                "expires_at": _fmt(booking["hold_expires_at"]),
            },
            "rental": rental and {
                "model": rental["model"], "contract_no": rental["contract_no"],
                "term": rental["rent_term"], "price": rental["rent_price"],
                "due_at": str(rental["due_at"] or ""),
            },
            "history": [
                {"model": h["model"], "contract_no": h["contract_no"],
                 "term": h["rent_term"], "closed_at": _fmt(h["closed_at"])}
                for h in history
            ],
        })

    async def book(self, request: web.Request) -> web.Response:
        user = self._client(request)
        if user is None:
            return _json({"error": "auth"}, status=401)
        try:
            body = await request.json()
        except ValueError:
            return _json({"error": "Не понял запрос."}, status=400)

        try:
            model_id = int(body.get("model_id"))
            point_id = int(body["point_id"]) if body.get("point_id") else None
        except (TypeError, ValueError):
            return _json({"error": "Выберите модель и точку."}, status=400)

        point = None
        if point_id is not None:
            point = next((p for p in await self.fleet.points()
                          if p["id"] == point_id), None)
            if point is None:
                return _json({"error": "Такой точки нет."}, status=400)
        now = datetime.now()
        pickup, err = logic.validate_pickup(
            body.get("pickup_at"), now=now,
            open_hour=point["open_hour"] if point else 10,
            close_hour=point["close_hour"] if point else 19)
        if err:
            return _json({"error": err}, status=400)

        name = " ".join(filter(None, (user.get("first_name"),
                                      user.get("last_name")))) or "без имени"
        username = user.get("username") or ""
        row, code = await self.fleet.book_model(
            user["id"], model_id=model_id, point_id=point_id,
            pickup_at=pickup,
            hold_minutes=logic.hold_minutes_for_pickup(pickup, now=now),
            note=f"{name}" + (f" @{username}" if username else ""))
        if row is None:
            return _json({"error": BOOK_ERRORS.get(code, "Не получилось.")},
                         status=409)

        booking = await self.fleet.active_booking_of(user["id"])
        await self._notify_operators(user, booking, pickup)
        return _json({"ok": True, "booking": booking and {
            "bike_id": booking["bike_id"], "model": booking["model"],
            "point": booking["point"], "address": booking["address"],
            "pickup_at": _fmt(booking["pickup_at"]),
            "expires_at": _fmt(booking["hold_expires_at"]),
        }})

    async def cancel(self, request: web.Request) -> web.Response:
        user = self._client(request)
        if user is None:
            return _json({"error": "auth"}, status=401)
        bike_id = await self.fleet.cancel_booking(user["id"])
        if bike_id is None:
            return _json({"error": "Живой брони не было."}, status=409)
        await self._notify_cancel(user, bike_id)
        return _json({"ok": True})

    # ─────────────────────── карточки операторам ───────────────────────

    async def _notify_operators(self, user: dict, booking, pickup) -> None:
        """Карточка брони в служебный чат. Сбой доставки бронь не отменяет:
        единица уже удержана, и таймер сам наведёт порядок."""
        if self.bot is None or self.admin_chat_id is None or booking is None:
            return
        from .. import texts
        from ..logic import esc
        name = " ".join(filter(None, (user.get("first_name"),
                                      user.get("last_name")))) or "без имени"
        try:
            await self.bot.send_message(self.admin_chat_id, texts.FLEET_BOOKING_CARD.format(
                name=esc(name),
                handle=("@" + esc(user["username"])) if user.get("username") else "без username",
                tg_id=user["id"], model=esc(booking["model"] or "без модели"),
                point=esc(booking["point"] or "любая"),
                pickup=_fmt(pickup), bike_id=booking["bike_id"],
                expires=_fmt(booking["hold_expires_at"])))
        except Exception:                               # noqa: BLE001
            log.exception("карточка брони %s не доставлена", user["id"])

    async def _notify_cancel(self, user: dict, bike_id: int) -> None:
        if self.bot is None or self.admin_chat_id is None:
            return
        from .. import texts
        from ..logic import esc
        name = " ".join(filter(None, (user.get("first_name"),
                                      user.get("last_name")))) or "без имени"
        try:
            await self.bot.send_message(
                self.admin_chat_id,
                texts.FLEET_BOOKING_CANCELLED.format(
                    name=esc(name), tg_id=user["id"], bike_id=bike_id))
        except Exception:                               # noqa: BLE001
            log.exception("карточка отмены брони %s не доставлена", user["id"])


def build_app(fleet: FleetDB, *, bot=None, admin_chat_id: int | None = None,
              bot_token: str = "") -> web.Application:
    api = Api(fleet, bot=bot, admin_chat_id=admin_chat_id, bot_token=bot_token)
    app = web.Application()
    app.add_routes([
        web.get("/", api.index),
        web.get("/api/health", api.health),
        web.get("/api/points", api.points),
        web.get("/api/models", api.models),
        web.get("/api/availability", api.availability),
        web.get("/api/me", api.me),
        web.post("/api/book", api.book),
        web.post("/api/cancel", api.cancel),
    ])
    return app


async def start_api(fleet: FleetDB, port: int, *, bot=None,
                    admin_chat_id: int | None = None,
                    bot_token: str = "") -> web.AppRunner:
    """Поднимает витрину и возвращает runner - его гасит main() при остановке."""
    runner = web.AppRunner(build_app(
        fleet, bot=bot, admin_chat_id=admin_chat_id, bot_token=bot_token))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("витрина парка и Mini App слушают порт %s", port)
    return runner
