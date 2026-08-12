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

import asyncpg
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
                 admins: tuple[int, ...] = (), starline=None,
                 tochka=None) -> None:
        self.fleet = fleet
        self.bot = bot
        self.admin_chat_id = admin_chat_id
        self.bot_token = bot_token
        self.starline = starline
        self.tochka = tochka
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
        # и в каждой карточке он был бы шумом. contacts - телефон
        # и Telegram проката для блока «Контакты» и напоминаний о ТО.
        from .. import texts
        return _json({"models": await self.fleet.models_with_tariffs(),
                      "perks": catalog.PERKS,
                      "contacts": {"phone": catalog.PHONE,
                                   "telegram": texts.SUPPORT_CONTACT_URL}})

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
        rental_json = None
        if rental:
            rental_json = {
                "model": rental["model"], "contract_no": rental["contract_no"],
                "term": rental["rent_term"], "price": rental["rent_price"],
                "due_at": str(rental["due_at"] or ""),
                # closed_at здесь всегда null: запрос отдаёт только живую
                # аренду, поэтому просрочка меряется по одному due_at.
                "overdue": logic.overdue(rental["due_at"], None,
                                         today=date.today()),
                "blocked": bool(rental["blocked"]),
                "battery": await self._battery(rental["starline_device_id"]),
                "payment": self._payment_json(
                    await self.fleet.pending_payment_of_tg(tg_id)),
                # Плановое ТО раз в две недели: клиент видит, когда пора.
                "service_days": logic.service_days_left(
                    rental["last_service_at"], now=datetime.now()),
            }
        return _json({
            "booking": booking and {
                "bike_id": booking["bike_id"], "model": booking["model"],
                "point": booking["point"], "address": booking["address"],
                "pickup_at": _fmt(booking["pickup_at"]),
                "expires_at": _fmt(booking["hold_expires_at"]),
            },
            "rental": rental_json,
            "history": [
                {"model": h["model"], "contract_no": h["contract_no"],
                 "term": h["rent_term"], "closed_at": _fmt(h["closed_at"])}
                for h in history
            ],
        })

    async def _battery(self, device_id: str | None) -> dict | None:
        """Заряд тяговой АКБ из телеметрии StarLine - для карточки клиента.
        Нет трекера, нет связи или мусор в данных - None, а не выдумка."""
        if not device_id or self.starline is None:
            return None
        try:
            volts = await self.starline.voltage(device_id)
        except Exception:                               # noqa: BLE001
            log.exception("телеметрия %s не получена", device_id)
            return None
        percent = logic.battery_percent(volts)
        if percent is None:
            return None
        return {"percent": percent, "voltage": round(float(volts), 1)}

    @staticmethod
    def _payment_json(payment) -> dict | None:
        if payment is None:
            return None
        age_hours = 0.0
        created = payment["created_at"]
        if isinstance(created, datetime):
            now = datetime.now(created.tzinfo) if created.tzinfo else datetime.now()
            age_hours = (now - created).total_seconds() / 3600
        return {
            "id": payment["id"], "amount": payment["amount"],
            "link": payment["qr_payload"], "created_at": _fmt(payment["created_at"]),
            # «Просрочка оплаты»: счёту больше суток, а денег нет. Ровно
            # столько же живёт сам QR - дальше и платить уже не по чему.
            "overdue": age_hours > 24,
        }

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
        # Сравнение в байтах: compare_digest на строке с не-ASCII (кириллица
        # в заголовке или в самом токене) кидает TypeError - и граница
        # доверия отвечала бы 500 вместо честного отказа.
        if self.crm_token and token and hmac.compare_digest(
                token.encode("utf-8", "surrogateescape"), self.crm_token.encode()):
            return True
        user = self._client(request)
        return bool(user and user["id"] in self.admins)

    def _admin_id(self, request: web.Request) -> int | None:
        """tg_id оператора, если в CRM вошли из Telegram; при входе
        по токену - None (в журнал уйдёт «оператор по токену»)."""
        user = self._client(request)
        return user["id"] if user else None

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
        service_due = [{
            "bike_id": r["bike_id"], "model": r["model"],
            "vin_frame": r["vin_frame"], "client_name": r["client_name"],
            "client_phone": r["client_phone"],
            "client_username": r["client_username"],
            "days_overdue": -(logic.service_days_left(
                r["last_service_at"], now=datetime.now()) or 0),
        } for r in await self.fleet.bikes_service_due()]
        return _json({
            "bikes": by_status, "total": sum(by_status.values()),
            "rentals_active": len(rentals), "overdue": overdue,
            "bookings_active": len(await self.fleet.bookings_admin()),
            "blocked": await self.fleet.count_blocked(),
            "starline": self.starline is not None,
            "service_due": service_due,
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
            "starline_device_id": r["starline_device_id"], "blocked": r["blocked"],
            "service_days": logic.service_days_left(r["last_service_at"],
                                                    now=datetime.now()),
        } for r in rows])

    async def admin_rentals(self, request: web.Request) -> web.Response:
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        today = date.today()
        active = [self._rental_json(r, today)
                  for r in await self.fleet.rentals_admin(active=True, limit=200)]
        # Неоплаченные счета - бейджем на активных арендах: оператор видит
        # «ожидает N ₽», не уходя во вкладку расчётов.
        pending = await self.fleet.pending_by_rentals([r["id"] for r in active])
        for r in active:
            p = pending.get(r["id"])
            r["pending_amount"] = p["amount"] if p else None
        closed = [self._rental_json(r)
                  for r in await self.fleet.rentals_admin(active=False, limit=30)]
        return _json({"active": active, "closed": closed,
                      "tochka": self.tochka is not None})

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
        try:
            result = await self.fleet.ingest_fixation(parsed, due_at=due)
        except asyncpg.UniqueViolationError:
            # Параллельный импорт того же клиента/рамы, или телефон уже
            # принадлежит другой карточке. Данные целы (транзакция
            # откатилась) - просим повторить, а не роняем 500.
            return _json({"error": "Карточка занята другим импортом или "
                          "телефон/рама уже у другого клиента. Повторите."},
                         status=409)
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
        # Возврат снимает блокировку: держать обездвиженной сданную единицу
        # незачем, а разблокировать её отдельной кнопкой оператор забудет.
        await self._auto_unblock(bike_id, self._admin_id(request))
        return _json({"ok": True, "bike_id": bike_id})

    async def _auto_unblock(self, bike_id: int, admin_id: int | None) -> None:
        if self.starline is None:
            return
        bike = await self.fleet.get_bike(str(bike_id))
        if not bike or not bike["blocked"] or not bike["starline_device_id"]:
            return
        ok = await self.starline.unblock(bike["starline_device_id"])
        await self.fleet.log_starline(
            bike_id, bike["starline_device_id"], "unblock", ok,
            "при закрытии аренды" if ok else "команда StarLine не прошла", admin_id)
        if ok:
            await self.fleet.mark_blocked(bike_id, blocked=False, reason=None)

    async def admin_bike_starline(self, request: web.Request) -> web.Response:
        """Привязать единице устройство StarLine (или отвязать пустым id)."""
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        try:
            body = await request.json()
            bike_id = int(body["bike_id"])
        except (ValueError, KeyError, TypeError):
            return _json({"error": "Не понял запрос."}, status=400)
        device_id = str(body.get("device_id") or "").strip() or None
        await self.fleet.patch_bike(bike_id, starline_device_id=device_id)
        return _json({"ok": True})

    async def admin_bike_block(self, request: web.Request) -> web.Response:
        """Заблокировать/разблокировать единицу через StarLine.

        Отметка blocked меняется только после того, как StarLine принял
        команду: в базе - факт состояния устройства, а не намерение.
        """
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        if self.starline is None:
            return _json({"error": "StarLine не настроен на сервере "
                          "(заполните STARLINE_* в .env)."}, status=400)
        try:
            body = await request.json()
            bike_id = int(body["bike_id"])
            on = bool(body["on"])
        except (ValueError, KeyError, TypeError):
            return _json({"error": "Не понял запрос."}, status=400)
        bike = await self.fleet.get_bike(str(bike_id))
        if bike is None:
            return _json({"error": "Единица не найдена."}, status=404)
        device = bike["starline_device_id"]
        if not device:
            return _json({"error": "У единицы не привязан StarLine. "
                          "Укажите ID устройства."}, status=400)
        reason = str(body.get("reason") or "неоплата").strip()
        admin_id = self._admin_id(request)
        ok = await (self.starline.block(device) if on
                    else self.starline.unblock(device))
        await self.fleet.log_starline(
            bike_id, device, "block" if on else "unblock", ok,
            None if ok else "команда StarLine не прошла", admin_id)
        if not ok:
            return _json({"error": "StarLine не принял команду. "
                          "Проверьте связь и ID устройства."}, status=502)
        await self.fleet.mark_blocked(bike_id, blocked=on,
                                      reason=reason if on else None)
        return _json({"ok": True})

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

    async def admin_bike_serviced(self, request: web.Request) -> web.Response:
        """Отметка «ТО проведено»: отсчёт двух недель заново."""
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        try:
            body = await request.json()
            bike_id = int(body["bike_id"])
        except (ValueError, KeyError, TypeError):
            return _json({"error": "Не понял запрос."}, status=400)
        await self.fleet.mark_serviced(bike_id)
        return _json({"ok": True})

    # ─────────────────────── расчёты (СБП, Точка) ───────────────────────

    @staticmethod
    def _payment_row(p) -> dict:
        return {
            "id": p["id"], "rental_id": p["rental_id"], "amount": p["amount"],
            "purpose": p["purpose"], "status": p["status"],
            "link": p["qr_payload"], "client_name": p["client_name"],
            "model": p["model"], "contract_no": p["contract_no"],
            "created_at": _fmt(p["created_at"]), "paid_at": _fmt(p["paid_at"]),
        }

    async def admin_payments(self, request: web.Request) -> web.Response:
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        summary = await self.fleet.payments_summary()
        rows = await self.fleet.payments_admin()
        return _json({"summary": summary, "tochka": self.tochka is not None,
                      "payments": [self._payment_row(p) for p in rows]})

    async def admin_payment_create(self, request: web.Request) -> web.Response:
        """Выставить СБП-счёт по аренде: динамический QR Точки на точную
        сумму. Клиенту (если он из бота) сразу уходит ссылка оплаты."""
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        if self.tochka is None:
            return _json({"error": "Оплата СБП не настроена: заполните "
                          "TOCHKA_* в .env."}, status=400)
        try:
            body = await request.json()
            rental_id = int(body["rental_id"])
            amount = int(body["amount"])
        except (ValueError, KeyError, TypeError):
            return _json({"error": "Нужны rental_id и сумма в рублях."},
                         status=400)
        if not 1 <= amount <= 1_000_000:
            return _json({"error": "Сумма выглядит неправдоподобно."}, status=400)
        rental = await self.fleet.rental_brief(rental_id)
        if rental is None or rental["closed_at"] is not None:
            return _json({"error": "Аренда не найдена или уже закрыта."},
                         status=409)
        purpose = ("Аренда электровелосипеда"
                   + (f", договор {rental['contract_no']}"
                      if rental["contract_no"] else ""))
        try:
            qr = await self.tochka.create_qr(amount, purpose)
        except Exception:                               # noqa: BLE001
            log.exception("Точка: счёт по аренде %s не зарегистрирован", rental_id)
            return _json({"error": "Точка не приняла счёт. Проверьте "
                          "реквизиты TOCHKA_* и попробуйте ещё раз."},
                         status=502)
        try:
            payment = await self.fleet.create_payment(
                rental_id, tg_id=rental["tg_id"], client_id=rental["client_id"],
                amount=amount, purpose=purpose, qrc_id=qr["qrc_id"],
                qr_payload=qr["payload"], created_by=self._admin_id(request))
        except asyncpg.UniqueViolationError:
            return _json({"error": "По этой аренде уже висит неоплаченный "
                          "счёт - отмените его или дождитесь оплаты."},
                         status=409)
        await self._notify_invoice(rental, payment)
        return _json({"ok": True, "payment": {
            "id": payment["id"], "amount": amount, "link": qr["payload"]}})

    async def _notify_invoice(self, rental, payment) -> None:
        if self.bot is None or not rental["tg_id"]:
            return
        from .. import texts
        try:
            await self.bot.send_message(rental["tg_id"],
                                        texts.FLEET_INVOICE_CLIENT.format(
                                            amount=payment["amount"],
                                            link=payment["qr_payload"]))
        except Exception:                               # noqa: BLE001
            log.exception("клиент %s не получил счёт", rental["tg_id"])

    async def admin_payment_cancel(self, request: web.Request) -> web.Response:
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        try:
            body = await request.json()
            payment_id = int(body["payment_id"])
        except (ValueError, KeyError, TypeError):
            return _json({"error": "Не понял запрос."}, status=400)
        row = await self.fleet.mark_payment(payment_id, "cancelled")
        if row is None:
            return _json({"error": "Счёт уже не в ожидании."}, status=409)
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
              admins: tuple[int, ...] = (), starline=None,
              tochka=None) -> web.Application:
    api = Api(fleet, bot=bot, admin_chat_id=admin_chat_id, bot_token=bot_token,
              crm_token=crm_token, admins=admins, starline=starline,
              tochka=tochka)
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
        web.post("/api/admin/bike/starline", api.admin_bike_starline),
        web.post("/api/admin/bike/block", api.admin_bike_block),
        web.post("/api/admin/bike/serviced", api.admin_bike_serviced),
        web.get("/api/admin/payments", api.admin_payments),
        web.post("/api/admin/payment/create", api.admin_payment_create),
        web.post("/api/admin/payment/cancel", api.admin_payment_cancel),
    ])
    if STATIC_DIR.is_dir():
        app.add_routes([web.static("/static", STATIC_DIR)])
    return app


async def start_api(fleet: FleetDB, port: int, *, bot=None,
                    admin_chat_id: int | None = None,
                    bot_token: str = "", crm_token: str = "",
                    admins: tuple[int, ...] = (), starline=None,
                    tochka=None) -> web.AppRunner:
    """Поднимает витрину и возвращает runner - его гасит main() при остановке."""
    runner = web.AppRunner(build_app(
        fleet, bot=bot, admin_chat_id=admin_chat_id, bot_token=bot_token,
        crm_token=crm_token, admins=admins, starline=starline, tochka=tochka))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("витрина парка, Mini App и CRM слушают порт %s", port)
    return runner
