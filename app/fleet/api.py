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

import hmac
import json
import logging
from datetime import date, datetime
from pathlib import Path

from aiohttp import web

from . import catalog, logic, webauth
from .db import FleetDB

log = logging.getLogger(__name__)

WEBAPP_FILE = Path(__file__).resolve().parent / "webapp.html"
ADMIN_FILE = Path(__file__).resolve().parent / "admin.html"
# Фото моделей: файлы кладутся сюда владельцем (имена - в catalog.py)
# и отдаются с нашего же сервера, без внешних CDN.
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Токен короче этого - не защита, а её видимость: CRM с ним не включается.
CRM_TOKEN_MIN_LEN = 8

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
                 bot_token: str = "", crm_token: str = "",
                 admins: tuple[int, ...] = ()) -> None:
        self.fleet = fleet
        self.bot = bot
        self.admin_chat_id = admin_chat_id
        self.bot_token = bot_token
        self.crm_token = crm_token if len(crm_token or "") >= CRM_TOKEN_MIN_LEN else ""
        if crm_token and not self.crm_token:
            log.warning("CRM_TOKEN короче %s символов - CRM по токену выключена",
                        CRM_TOKEN_MIN_LEN)
        self.admins = frozenset(admins)
        # Файлы читаются на старте, а не на каждый запрос: они не меняются
        # без рестарта, а отсутствие должно обнаружиться сразу.
        self.webapp = WEBAPP_FILE.read_text(encoding="utf-8")
        self.adminapp = ADMIN_FILE.read_text(encoding="utf-8")

    # ─────────────────────── витрина ───────────────────────

    async def index(self, _request: web.Request) -> web.Response:
        return web.Response(text=self.webapp, content_type="text/html")

    async def health(self, _request: web.Request) -> web.Response:
        return _json({"ok": True})

    async def points(self, _request: web.Request) -> web.Response:
        rows = await self.fleet.points()
        return _json([
            {"id": r["id"], "title": r["title"], "address": r["address"],
             "lat": r["lat"], "lon": r["lon"], "phone": r["phone"],
             "open_hour": r["open_hour"], "close_hour": r["close_hour"]}
            for r in rows
        ])

    async def models(self, _request: web.Request) -> web.Response:
        # perks - общий блок «что входит»: текст один на все модели,
        # и в каждой карточке он был бы шумом.
        return _json({"models": await self.fleet.models_with_tariffs(),
                      "perks": catalog.PERKS})

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

    # ─────────────────────── CRM ───────────────────────

    def _is_admin(self, request: web.Request) -> bool:
        """Доступ в CRM: токен из .env (браузер) либо initData админа
        (открыто из Telegram). Сравнение токена constant-time: тайминг
        не должен подсказывать, на каком символе перебор промахнулся."""
        token = request.headers.get("X-CRM-Token", "")
        if self.crm_token and token and hmac.compare_digest(token, self.crm_token):
            return True
        user = self._client(request)
        return bool(user and user["id"] in self.admins)

    async def admin_page(self, _request: web.Request) -> web.Response:
        return web.Response(text=self.adminapp, content_type="text/html")

    async def admin_ping(self, request: web.Request) -> web.Response:
        if not self._is_admin(request):
            return _json({"error": "auth",
                          "enabled": bool(self.crm_token)}, status=401)
        return _json({"ok": True})

    async def admin_overview(self, request: web.Request) -> web.Response:
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        counts = await self.fleet.park_counts()
        by_status: dict[str, int] = {}
        for r in counts:
            by_status[r["status"]] = by_status.get(r["status"], 0) + r["count"]
        rentals = await self.fleet.rentals_admin(active=True, limit=200)
        today = date.today()
        overdue = [self._rental_json(r, today) for r in rentals
                   if r["due_at"] and r["due_at"] < today]
        return _json({
            "bikes": by_status, "total": sum(by_status.values()),
            "rentals_active": len(rentals), "overdue": overdue,
            "bookings_active": len(await self.fleet.bookings_admin()),
        })

    @staticmethod
    def _rental_json(r, today: date | None = None) -> dict:
        return {
            "id": r["id"], "bike_id": r["bike_id"], "vin_frame": r["vin_frame"],
            "model": r["model"], "point": r["point"],
            "client_name": r["client_name"], "client_phone": r["client_phone"],
            "client_username": r["client_username"], "client_id": r["client_id"],
            "tg_id": r["tg_id"], "contract_no": r["contract_no"],
            "term": r["rent_term"], "price": r["rent_price"],
            "due_at": str(r["due_at"] or ""), "opened_at": _fmt(r["opened_at"]),
            "closed_at": _fmt(r["closed_at"]), "close_notes": r["close_notes"],
            "kit": r["kit"], "extra": r["extra"],
            "overdue": bool(r["due_at"] and today and not r["closed_at"]
                            and r["due_at"] < today),
        }

    async def admin_bikes(self, request: web.Request) -> web.Response:
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        rows = await self.fleet.list_bikes(limit=500)
        return _json([{
            "id": r["id"], "vin_frame": r["vin_frame"], "vin_motor": r["vin_motor"],
            "model": r["model"], "point": r["point"], "status": r["status"],
            "notes": r["notes"], "renter_name": r["renter_name"],
            "renter_username": r["renter_username"], "hold_note": r["hold_note"],
        } for r in rows])

    async def admin_rentals(self, request: web.Request) -> web.Response:
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        today = date.today()
        active = [self._rental_json(r, today)
                  for r in await self.fleet.rentals_admin(active=True, limit=200)]
        closed = [self._rental_json(r)
                  for r in await self.fleet.rentals_admin(active=False, limit=30)]
        return _json({"active": active, "closed": closed})

    async def admin_bookings(self, request: web.Request) -> web.Response:
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        return _json([{
            "id": r["id"], "bike_id": r["bike_id"], "vin_frame": r["vin_frame"],
            "model": r["model"], "point": r["point"], "note": r["note"],
            "source": r["source"], "client_name": r["client_name"],
            "client_username": r["client_username"], "tg_id": r["tg_id"],
            "pickup_at": _fmt(r["pickup_at"]),
            "expires_at": _fmt(r["hold_expires_at"]),
        } for r in await self.fleet.bookings_admin()])

    async def admin_clients(self, request: web.Request) -> web.Response:
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        rows = await self.fleet.clients_admin(request.query.get("q", ""))
        return _json([{
            "id": r["id"], "full_name": r["full_name"], "phone": r["phone"],
            "phone2": r["phone2"], "phone3": r["phone3"],
            "tg_username": r["tg_username"], "tg_id": r["tg_id"],
            "live_address": r["live_address"], "created_at": _fmt(r["created_at"]),
            "rental_model": r["rental_model"],
            "rental_due": str(r["due_at"] or ""),
        } for r in rows])

    async def admin_parse_form(self, request: web.Request) -> web.Response:
        """Предпросмотр без записи: оператор видит, что распозналось
        и какие строки не поняты, ДО того как что-то попадёт в базу."""
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        try:
            body = await request.json()
        except ValueError:
            return _json({"error": "Не понял запрос."}, status=400)
        parsed, warnings = logic.parse_fixation_form(body.get("text"))
        if parsed is None:
            return _json({"error": warnings[0] if warnings else "пустая форма"},
                         status=400)
        due = logic.parse_due(str(parsed.get("rent_term") or ""),
                              today=date.today())
        return _json({"parsed": parsed, "warnings": warnings,
                      "due_at": str(due or "")})

    async def admin_ingest_form(self, request: web.Request) -> web.Response:
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        try:
            body = await request.json()
        except ValueError:
            return _json({"error": "Не понял запрос."}, status=400)
        parsed, warnings = logic.parse_fixation_form(body.get("text"))
        if parsed is None:
            return _json({"error": warnings[0] if warnings else "пустая форма"},
                         status=400)
        due = logic.parse_due(str(parsed.get("rent_term") or ""),
                              today=date.today())
        result = await self.fleet.ingest_fixation(parsed, due_at=due)
        return _json({"ok": True, "result": result, "warnings": warnings})

    async def admin_close_rental(self, request: web.Request) -> web.Response:
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        try:
            body = await request.json()
            rental_id = int(body["rental_id"])
        except (ValueError, KeyError, TypeError):
            return _json({"error": "Не понял запрос."}, status=400)
        bike_id = await self.fleet.close_rental_by_id(
            rental_id, notes=str(body.get("note") or "").strip() or None,
            to_service=bool(body.get("to_service")))
        if bike_id is None:
            return _json({"error": "Аренда уже закрыта или не найдена."},
                         status=409)
        return _json({"ok": True, "bike_id": bike_id})

    async def admin_cancel_booking(self, request: web.Request) -> web.Response:
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        try:
            body = await request.json()
            booking_id = int(body["booking_id"])
        except (ValueError, KeyError, TypeError):
            return _json({"error": "Не понял запрос."}, status=400)
        bike_id = await self.fleet.cancel_booking_by_id(booking_id)
        if bike_id is None:
            return _json({"error": "Бронь уже снята."}, status=409)
        return _json({"ok": True, "bike_id": bike_id})

    async def admin_bike_status(self, request: web.Request) -> web.Response:
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        try:
            body = await request.json()
            bike_id = int(body["bike_id"])
            status = str(body["status"])
        except (ValueError, KeyError, TypeError):
            return _json({"error": "Не понял запрос."}, status=400)
        if status not in logic.BIKE_STATUSES:
            return _json({"error": "Нет такого статуса."}, status=400)
        await self.fleet.set_status(bike_id, status,
                                    str(body.get("note") or "").strip() or None)
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
              bot_token: str = "", crm_token: str = "",
              admins: tuple[int, ...] = ()) -> web.Application:
    api = Api(fleet, bot=bot, admin_chat_id=admin_chat_id, bot_token=bot_token,
              crm_token=crm_token, admins=admins)
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
        # CRM: страница и её API. Всё под _is_admin - токен или админский
        # initData; без них любой запрос отвечает 401.
        web.get("/admin", api.admin_page),
        web.get("/api/admin/ping", api.admin_ping),
        web.get("/api/admin/overview", api.admin_overview),
        web.get("/api/admin/bikes", api.admin_bikes),
        web.get("/api/admin/rentals", api.admin_rentals),
        web.get("/api/admin/bookings", api.admin_bookings),
        web.get("/api/admin/clients", api.admin_clients),
        web.post("/api/admin/parse-form", api.admin_parse_form),
        web.post("/api/admin/ingest-form", api.admin_ingest_form),
        web.post("/api/admin/rental/close", api.admin_close_rental),
        web.post("/api/admin/booking/cancel", api.admin_cancel_booking),
        web.post("/api/admin/bike/status", api.admin_bike_status),
    ])
    if STATIC_DIR.is_dir():
        app.add_routes([web.static("/static", STATIC_DIR)])
    return app


async def start_api(fleet: FleetDB, port: int, *, bot=None,
                    admin_chat_id: int | None = None,
                    bot_token: str = "", crm_token: str = "",
                    admins: tuple[int, ...] = ()) -> web.AppRunner:
    """Поднимает витрину и возвращает runner - его гасит main() при остановке."""
    runner = web.AppRunner(build_app(
        fleet, bot=bot, admin_chat_id=admin_chat_id, bot_token=bot_token,
        crm_token=crm_token, admins=admins))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("витрина парка, Mini App и CRM слушают порт %s", port)
    return runner
