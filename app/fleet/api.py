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
from datetime import date, datetime, timedelta
from pathlib import Path

import asyncpg
from aiohttp import web

from . import catalog, logic, webauth
from .db import FleetDB

log = logging.getLogger(__name__)

WEBAPP_FILE = Path(__file__).resolve().parent / "webapp.html"
ADMIN_FILE = Path(__file__).resolve().parent / "admin.html"
LANDING_FILE = Path(__file__).resolve().parent / "landing.html"
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
                 tochka=None, db=None, vault=None, cfg=None) -> None:
        self.fleet = fleet
        self.bot = bot
        self.admin_chat_id = admin_chat_id
        self.bot_token = bot_token
        self.starline = starline
        self.tochka = tochka
        # Регистрация и KYC пишут в bot.users: db - тот же Database, что
        # у бота, vault шифрует анкету, cfg даёт редакции политики и пути.
        self.db = db
        self.vault = vault
        self.cfg = cfg
        self.crm_token = crm_token if len(crm_token or "") >= CRM_TOKEN_MIN_LEN else ""
        if crm_token and not self.crm_token:
            log.warning("CRM_TOKEN короче %s символов - CRM по токену выключена",
                        CRM_TOKEN_MIN_LEN)
        self.admins = frozenset(admins)
        # Файлы читаются на старте, а не на каждый запрос: они не меняются
        # без рестарта, а отсутствие должно обнаружиться сразу.
        self.webapp = WEBAPP_FILE.read_text(encoding="utf-8")
        self.adminapp = ADMIN_FILE.read_text(encoding="utf-8")
        self.landing = LANDING_FILE.read_text(encoding="utf-8")

    # ─────────────────────── витрина ───────────────────────

    async def index(self, _request: web.Request) -> web.Response:
        """Корень домена - публичный сайт-лендинг. Открытый из Telegram,
        он сам уводит в Mini App (/app): MINIAPP_URL со старых установок
        может указывать на корень, и ломать его нельзя."""
        return web.Response(text=self.landing, content_type="text/html")

    async def app_page(self, _request: web.Request) -> web.Response:
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
            payment = await self.fleet.pending_payment_of_rental(rental["id"])
            rental_json = {
                "model": rental["model"], "contract_no": rental["contract_no"],
                "term": rental["rent_term"], "price": rental["rent_price"],
                # Дата по-русски, как в сообщениях бота: клиенту в бейдж
                # «вернуть до», а не ISO из базы.
                "due_at": (rental["due_at"].strftime("%d.%m.%Y")
                           if rental["due_at"] else ""),
                # closed_at здесь всегда null: запрос отдаёт только живую
                # аренду, поэтому просрочка меряется по одному due_at.
                "overdue": logic.overdue(rental["due_at"], None,
                                         today=date.today()),
                "blocked": bool(rental["blocked"]),
                "battery": await self._battery(rental["starline_device_id"]),
                "payment": self._payment_json(payment),
                # Плановое ТО раз в две недели: клиент видит, когда пора.
                "service_days": logic.service_days_left(
                    rental["last_service_at"], now=datetime.now()),
                "extend": self._extend_offer(rental, payment),
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
        return {"percent": percent, "voltage": round(float(volts), 1),
                # Порог «пора на зарядку» отдаёт сервер: он один на баннер
                # в приложении и на фоновое уведомление от бота.
                "low": percent <= logic.LOW_BATTERY_PCT}

    def _extend_offer(self, rental, payment) -> dict | None:
        """Предложение «продлить кнопкой» для карточки клиента. None -
        кнопки нет: без Точки платить нечем, при живом счёте продление
        создало бы второй pending (и упёрлось бы в инвариант базы)."""
        if self.tochka is None or payment is not None:
            return None
        offer = logic.extension_offer(
            rent_term=rental["rent_term"], rent_price=rental["rent_price"],
            opened_at=rental["opened_at"], due_at=rental["due_at"])
        if offer is None:
            return None
        return {"days": offer["days"], "amount": offer["amount"],
                "new_due": offer["new_due"].strftime("%d.%m.%Y")}

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

    async def extend(self, request: web.Request) -> web.Response:
        """Продление аренды кнопкой: счёт СБП на автосчитанную сумму.

        Срок двигается ТОЛЬКО после подтверждения оплаты банком (в
        payments_loop) - кнопка лишь выставляет счёт. Сумма и шаг считаются
        сервером заново, а не берутся из запроса: клиент не должен уметь
        продлиться на год за сто рублей, подправив JSON.
        """
        user = self._client(request)
        if user is None:
            return _json({"error": "auth"}, status=401)
        if self.tochka is None:
            return _json({"error": "Оплата в приложении не подключена - "
                          "продлите через чат с ботом."}, status=503)
        rental = await self.fleet.active_rental_of(user["id"])
        if rental is None:
            return _json({"error": "Активной аренды нет."}, status=409)
        if await self.fleet.pending_payment_of_rental(rental["id"]) is not None:
            return _json({"error": "Сначала оплатите текущий счёт - он уже "
                          "выставлен и виден в приложении."}, status=409)
        offer = logic.extension_offer(
            rent_term=rental["rent_term"], rent_price=rental["rent_price"],
            opened_at=rental["opened_at"], due_at=rental["due_at"])
        if offer is None:
            return _json({"error": "Сумму продления по договору посчитать "
                          "не получилось - напишите нам в чат с ботом."},
                         status=409)
        purpose = (f"Продление аренды на {offer['days']} дн."
                   + (f", договор {rental['contract_no']}"
                      if rental["contract_no"] else ""))
        try:
            qr = await self.tochka.create_qr(offer["amount"], purpose)
        except Exception:                               # noqa: BLE001
            log.exception("Точка: счёт продления аренды %s не зарегистрирован",
                          rental["id"])
            return _json({"error": "Банк не принял счёт - попробуйте ещё раз "
                          "чуть позже."}, status=502)
        try:
            payment = await self.fleet.create_payment(
                rental["id"], tg_id=user["id"], client_id=None,
                amount=offer["amount"], purpose=purpose, qrc_id=qr["qrc_id"],
                qr_payload=qr["payload"], created_by=None,
                extend_days=offer["days"])
        except asyncpg.UniqueViolationError:
            # Гонка с оператором, выставившим счёт между проверкой и вставкой.
            return _json({"error": "По аренде уже есть неоплаченный счёт - "
                          "обновите экран."}, status=409)
        await self._notify_extend_request(rental, offer)
        return _json({"ok": True, "payment": self._payment_json(payment),
                      "days": offer["days"], "new_due": offer["new_due"]
                      .strftime("%d.%m.%Y")})

    async def _notify_extend_request(self, rental, offer) -> None:
        """Карточка оператору: клиент сам выставил себе счёт на продление.
        Сбой доставки счёт не отменяет - он уже в базе и в опросе оплат."""
        if self.bot is None or self.admin_chat_id is None:
            return
        from .. import texts
        brief = await self.fleet.rental_brief(rental["id"])
        try:
            await self.bot.send_message(
                self.admin_chat_id, texts.FLEET_EXTEND_REQUEST_CARD.format(
                    client=logic.esc((brief and brief["client_name"]) or "—"),
                    number=logic.esc(rental["contract_no"] or "—"),
                    days=offer["days"], amount=offer["amount"],
                    due=offer["new_due"].strftime("%d.%m.%Y")))
        except Exception:                               # noqa: BLE001
            log.exception("карточка продления аренды %s не доставлена",
                          rental["id"])

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
        # Контроль по точкам: те же счётчики в разрезе точки приписки.
        # Из одного списка единиц, без отдельного SQL: блокировки и зов ТО
        # считаются той же логикой, что красит бейджи в «Парке».
        by_point: dict[str, dict] = {}
        for b in await self.fleet.list_bikes(limit=500):
            slot = by_point.setdefault(b["point"] or "без точки", {
                "free": 0, "booked": 0, "rented": 0, "service": 0,
                "lost": 0, "blocked": 0, "service_due": 0})
            slot[b["status"]] = slot.get(b["status"], 0) + 1
            slot["blocked"] += bool(b["blocked"])
            days = logic.service_days_left(b["last_service_at"],
                                           now=datetime.now())
            slot["service_due"] += (b["status"] == "rented"
                                    and days is not None and days <= 0)
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
            "tochka": self.tochka is not None,
            "service_due": service_due,
            "points": [{"title": title, **slot}
                       for title, slot in sorted(by_point.items())],
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
            "model": r["model"], "point": r["point"], "point_id": r["point_id"],
            "status": r["status"],
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
                      "tochka": self.tochka is not None,
                      "starline": self.starline is not None})

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
        from .starline import unblock_bike
        await unblock_bike(self.fleet, self.starline, bike_id,
                           reason="при закрытии аренды", admin_id=admin_id)

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

    async def admin_bike_price(self, request: web.Request) -> web.Response:
        """Закупочная цена единицы - для окупаемости. Пусто - снять цену."""
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        try:
            body = await request.json()
            bike_id = int(body["bike_id"])
            raw = body.get("price")
            price = int(raw) if raw not in (None, "") else None
        except (ValueError, KeyError, TypeError):
            return _json({"error": "Не понял запрос."}, status=400)
        if price is not None and not 1 <= price <= 10_000_000:
            return _json({"error": "Цена выглядит неправдоподобно."}, status=400)
        if not await self.fleet.patch_bike(bike_id, purchase_price=price):
            return _json({"error": "Единица не найдена."}, status=404)
        return _json({"ok": True})

    async def admin_bike_point(self, request: web.Request) -> web.Response:
        """Перенос единицы на точку из CRM - учёт следует за перестановкой
        техники. Пустая точка допустима: велосипед в дороге или в гараже."""
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        try:
            body = await request.json()
            bike_id = int(body["bike_id"])
            raw = body.get("point_id")
            point_id = int(raw) if raw not in (None, "") else None
        except (ValueError, KeyError, TypeError):
            return _json({"error": "Не понял запрос."}, status=400)
        if point_id is not None and not any(
                p["id"] == point_id for p in await self.fleet.points()):
            return _json({"error": "Такой точки нет."}, status=400)
        if not await self.fleet.patch_bike(bike_id, point_id=point_id):
            return _json({"error": "Единица не найдена."}, status=404)
        return _json({"ok": True})

    async def admin_bike_since(self, request: web.Request) -> web.Response:
        """Дата ввода в строй - отсчёт жизненного цикла. Пусто - снять
        (вернётся автоотсчёт от первой выдачи)."""
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        try:
            body = await request.json()
            bike_id = int(body["bike_id"])
            raw = str(body.get("since") or "").strip()
            since = date.fromisoformat(raw) if raw else None
        except (ValueError, KeyError, TypeError):
            return _json({"error": "Не понял запрос."}, status=400)
        if since is not None and not date(2015, 1, 1) <= since <= date.today():
            return _json({"error": "Дата вне разумного: от 2015 года "
                          "и не из будущего."}, status=400)
        if not await self.fleet.patch_bike(bike_id, in_service_since=since):
            return _json({"error": "Единица не найдена."}, status=404)
        return _json({"ok": True})

    async def admin_analytics(self, request: web.Request) -> web.Response:
        """Аналитика проката: тренд выдач и возвратов, PnL, окупаемость.

        Выручка двух сортов и они не смешиваются втихую: оплаченные счета
        СБП - факт, подтверждённый банком; оценка по договору - цена
        периода х число периодов. За выручку аренды берётся большее из
        двух: складывать их значило бы посчитать одни деньги дважды.
        """
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        today = date.today()
        rentals = await self.fleet.analytics_rentals()
        paid_map = await self.fleet.paid_by_rental()
        bikes = await self.fleet.analytics_bikes()

        trend = logic.weekly_trend(
            [(r["opened_at"], r["closed_at"]) for r in rentals],
            weeks=12, today=today)

        by_bike: dict[int, dict] = {}
        totals = {"revenue": 0, "paid": 0, "unpriced_rentals": 0}
        money_items = []          # (opened, closed, revenue) - для рядов денег
        for r in rentals:
            est = logic.revenue_estimate(r["rent_term"], r["rent_price"],
                                         r["opened_at"], r["closed_at"],
                                         today=today)
            fact = paid_map.get(r["id"], 0)
            revenue = max(fact, est or 0)
            if est is None and fact == 0:
                totals["unpriced_rentals"] += 1
            totals["revenue"] += revenue
            totals["paid"] += fact
            money_items.append((r["opened_at"], r["closed_at"], revenue))
            if r["bike_id"] is None:
                continue
            slot = by_bike.setdefault(r["bike_id"], {
                "revenue": 0, "paid": 0, "rentals": 0, "days": 0,
                "active": False})
            slot["revenue"] += revenue
            slot["paid"] += fact
            slot["rentals"] += 1
            if r["opened_at"] is not None:
                end = r["closed_at"] or datetime.now(r["opened_at"].tzinfo)
                slot["days"] += max(0, (end - r["opened_at"]).days)
            if r["closed_at"] is None:
                slot["active"] = True

        months = logic.monthly_revenue(money_items, months=12, today=today)
        revenue_30 = logic.revenue_in_window(
            money_items, start=today - timedelta(days=30),
            end=today + timedelta(days=1))
        util_30 = logic.utilization_percent(
            [(r["opened_at"], r["closed_at"]) for r in rentals],
            len(bikes), days=30, today=today)

        bikes_json, park_price, paid_off_count = [], 0, 0
        replace_due = replace_soon = 0
        # Деньги в разрезе точек - по ТЕКУЩЕЙ приписке единицы: историю
        # перестановок учёт не хранит, и честнее сказать это прямо,
        # чем изображать точность, которой нет.
        by_point: dict[str, dict] = {}
        for b in bikes:
            slot = by_bike.get(b["id"], {"revenue": 0, "paid": 0,
                                         "rentals": 0, "days": 0,
                                         "active": False})
            percent = logic.payback_percent(slot["revenue"],
                                            b["purchase_price"])
            if b["purchase_price"]:
                park_price += b["purchase_price"]
                if percent is not None and percent >= 100:
                    paid_off_count += 1
            # Отсчёт цикла: дата «в строю с» от владельца, иначе первая
            # выдача. created_at не годится: у бэкфилла это день деплоя.
            since = b["in_service_since"] or b["first_rented_at"]
            cycle = logic.lifecycle(since, slot["revenue"],
                                    b["purchase_price"], today=today)
            if cycle:
                replace_due += cycle["replace_due"]
                replace_soon += cycle["replace_soon"]
            pt = by_point.setdefault(b["point"] or "без точки", {
                "bikes": 0, "revenue": 0, "paid": 0, "rentals": 0,
                "active": 0, "priced": 0, "paid_off": 0, "replace_due": 0})
            pt["bikes"] += 1
            pt["revenue"] += slot["revenue"]
            pt["paid"] += slot["paid"]
            pt["rentals"] += slot["rentals"]
            pt["active"] += slot["active"]
            if b["purchase_price"]:
                pt["priced"] += 1
                pt["paid_off"] += (percent is not None and percent >= 100)
            if cycle:
                pt["replace_due"] += cycle["replace_due"]
            bikes_json.append({
                "id": b["id"], "vin": b["vin_frame"], "model": b["model"],
                "status": b["status"], "price": b["purchase_price"],
                "revenue": slot["revenue"], "paid": slot["paid"],
                "rentals": slot["rentals"], "days": slot["days"],
                "active": slot["active"], "payback": percent,
                "paid_off": percent is not None and percent >= 100,
                "since": str(since) if since else None,
                "since_auto": b["in_service_since"] is None,
                "cycle": cycle,
            })

        issued_total = sum(1 for r in rentals if r["opened_at"] is not None)
        returned_total = sum(1 for r in rentals if r["closed_at"] is not None)
        return _json({
            "trend": [{"start": row["start"].strftime("%d.%m"),
                       "issued": row["issued"], "returned": row["returned"]}
                      for row in trend],
            "months": [{"start": row["start"].strftime("%m.%Y"),
                        "amount": row["amount"]} for row in months],
            "summary": {
                "revenue": totals["revenue"], "paid": totals["paid"],
                "park_price": park_price,
                "profit": totals["revenue"] - park_price,
                "payback": logic.payback_percent(totals["revenue"],
                                                 park_price),
                "issued": issued_total, "returned": returned_total,
                "open": issued_total - returned_total,
                "bikes": len(bikes), "paid_off": paid_off_count,
                "priced": sum(1 for b in bikes if b["purchase_price"]),
                "unpriced_rentals": totals["unpriced_rentals"],
                "revenue_30": revenue_30, "utilization_30": util_30,
                "replace_due": replace_due, "replace_soon": replace_soon,
                "lifecycle_months": logic.LIFECYCLE_MONTHS,
            },
            "points": [{"title": title, **pt}
                       for title, pt in sorted(by_point.items())],
            "bikes": bikes_json,
        })

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

    # ─────────────────── регистрация из Mini App ───────────────────

    @property
    def _reg_ready(self) -> bool:
        return all((self.db, self.vault, self.cfg))

    async def reg_status(self, request: web.Request) -> web.Response:
        user = self._client(request)
        if user is None:
            return _json({"error": "auth"}, status=401)
        if not self._reg_ready:
            return _json({"error": "Оформление в приложении не подключено."},
                         status=503)
        from .. import logic as bot_logic
        row = await self.db.get_user(user["id"])
        data = dict(row) if row else {}
        status = data.get("status") or "new"
        return _json({
            "status": "none" if status == "new" else status,
            "reject_reason": data.get("reject_reason"),
            "full_name": data.get("full_name"),
            "contract_signed": data.get("contract_status") == bot_logic.CT_SIGNED,
            "policy_url": (self.cfg.pdn_url or "") if self.cfg else "",
            "policy_file": bool(self.cfg and self.cfg.pdn_policy_file.exists()),
        })

    async def policy_file(self, _request: web.Request) -> web.Response:
        """Политика обработки ПДн - тот же файл, что бот шлёт на шаге
        ознакомления. Публична: это документ для клиента, а не секрет."""
        if self.cfg and self.cfg.pdn_policy_file.exists():
            return web.FileResponse(self.cfg.pdn_policy_file)
        raise web.HTTPNotFound

    # Telegram-карточка принимает эти форматы фотографией; HEIC, который
    # пропускает бот (там Telegram сам перекодирует), из формы не пройдёт.
    _CARD_MIME = frozenset({"image/jpeg", "image/jpg", "image/png", "image/webp"})

    def _check_upload(self, field, *, required_error: str) -> tuple[bytes | None, str]:
        from .. import logic as bot_logic
        if not isinstance(field, web.FileField):
            return None, required_error
        data = field.file.read()
        check = bot_logic.validate_upload(False, field.content_type, len(data))
        if not check.ok:
            return None, check.error
        if (field.content_type or "").lower() not in self._CARD_MIME:
            return None, "Нужен JPG, PNG или WebP."
        return data, ""

    async def reg_submit(self, request: web.Request) -> web.Response:
        """Анкета из Mini App: та же регистрация, что в боте, одной формой.

        Пишет в bot.users и шлёт ту же карточку модерации в служебный чат -
        решение можно принять и кнопками в Telegram, и в CRM. Дальше
        работает существующий конвейер: договор, подпись, оплата.
        """
        user = self._client(request)
        if user is None:
            return _json({"error": "auth"}, status=401)
        if not self._reg_ready:
            return _json({"error": "Оформление в приложении не подключено."},
                         status=503)
        from datetime import date as _date

        from .. import logic as bot_logic
        from ..db import utcnow
        from ..services import files as file_store

        tg_id = user["id"]
        post = await request.post()
        if post.get("policy") != "1" or post.get("consent") != "1":
            return _json({"errors": {"consent":
                          "Нужно ознакомиться с Политикой и дать согласие "
                          "на обработку персональных данных."}}, status=400)

        form = {k: post.get(k) for k in
                ("fio", "phone", *bot_logic.ANKETA_FIELDS)}
        clean, errors = logic.validate_registration(form, today=_date.today())

        doc_bytes, doc_err = self._check_upload(
            post.get("doc"),
            required_error="Прикрепите фото разворота паспорта.")
        if doc_err:
            errors["doc"] = doc_err
        minor = bool(clean) and bot_logic.is_minor(clean["anketa"],
                                                   today=_date.today())
        parent_bytes = None
        if minor:
            parent_bytes, parent_err = self._check_upload(
                post.get("parent"),
                required_error="Вам 16–17 лет: нужно фото письменного "
                               "согласия родителя.")
            if parent_err:
                errors["parent"] = parent_err
        if errors:
            return _json({"errors": errors}, status=400)

        row = await self.db.upsert_user(tg_id, user.get("username"))
        data = dict(row)
        if data.get("status") == bot_logic.ST_PENDING:
            return _json({"error": "Заявка уже на проверке - дождитесь "
                          "решения."}, status=409)
        if (data.get("status") == bot_logic.ST_APPROVED
                and data.get("contract_status") == bot_logic.CT_SIGNED):
            return _json({"error": "Вы уже зарегистрированы - договор "
                          "подписан."}, status=409)

        doc_path, doc_sha = file_store.store(self.cfg.storage_dir, tg_id,
                                             "doc", doc_bytes)
        now = utcnow()
        patch = {
            "full_name": clean["fio"], "phone": clean["phone"],
            "anketa_enc": self.vault.encrypt(clean["anketa"]),
            "doc_path": str(doc_path), "doc_sha256": doc_sha,
            "doc_is_photo": True,
            # Два юридических факта с двумя отметками, как в боте:
            # ознакомление с Политикой и согласие на обработку.
            "policy_version": self.cfg.pdn_version, "policy_ack_at": now,
            "oferta_version": self.cfg.oferta_version,
            "oferta_accepted_at": now,
            "pdn_version": self.cfg.consent_version, "pdn_consent_at": now,
            "status": bot_logic.ST_PENDING, "state": bot_logic.PENDING,
            "reject_reason": None,
        }
        if parent_bytes:
            parent_path, parent_sha = file_store.store(
                self.cfg.storage_dir, tg_id, "parent", parent_bytes)
            patch.update(parent_path=str(parent_path),
                         parent_sha256=parent_sha, parent_is_photo=True)

        patch.update(await self._send_kyc_card(
            tg_id, clean, doc_bytes, parent_bytes, minor))
        await self.db.patch(tg_id, **patch)
        await self.db.log_event(tg_id, "submitted", {"via": "app"})

        duplicates = await self.db.count_duplicate_docs(tg_id, doc_sha)
        if duplicates and self.bot is not None:
            from .. import texts
            await self.db.log_event(tg_id, "duplicate_document",
                                    {"count": duplicates})
            try:
                await self.bot.send_message(
                    self.admin_chat_id, texts.ALERT_DUPLICATE.format(
                        tg_id=tg_id, count=duplicates))
            except Exception:                           # noqa: BLE001
                log.exception("алерт о дубле документа %s не доставлен", tg_id)
        await self._notify_client(tg_id, "PENDING_WAIT")
        return _json({"ok": True, "status": "pending"})

    async def _send_kyc_card(self, tg_id: int, clean: dict, doc_bytes: bytes,
                             parent_bytes: bytes | None, minor: bool) -> dict:
        """Карточка модерации - та же, что шлёт бот: фото документа, анкета
        в порядке договора и кнопки «Одобрить/Отклонить» (работают
        в Telegram, решение из CRM снимает их).

        Сбой доставки заявку не хоронит: mod_* остаются пустыми, а CRM
        показывает pending-заявки и без карточки.
        """
        if self.bot is None or self.admin_chat_id is None:
            return {}
        from aiogram.types import BufferedInputFile

        from .. import keyboards as kb
        from .. import logic as bot_logic
        from .. import texts
        result: dict = {}
        try:
            if minor and parent_bytes:
                sent = await self.bot.send_photo(
                    self.admin_chat_id,
                    BufferedInputFile(parent_bytes, "parent.jpg"),
                    caption=texts.PARENT_CARD_CAPTION.format(tg_id=tg_id))
                if sent.photo:
                    result["parent_file_id"] = sent.photo[-1].file_id
            ctx = bot_logic.contract_context(
                {"tg_id": tg_id, "full_name": clean["fio"],
                 "phone": clean["phone"]},
                clean["anketa"], number="")
            fields = "\n".join(
                f"{label}: <b>{bot_logic.esc(ctx.get(field, '—'))}</b>"
                for field, label in bot_logic.CONTRACT_LABELS)
            caption = texts.CONTRACT_CARD.format(
                number="будет присвоен", fields=fields, tg_id=tg_id)
            if minor:
                caption += texts.CARD_MINOR_LINE
            sent = await self.bot.send_photo(
                self.admin_chat_id, BufferedInputFile(doc_bytes, "doc.jpg"),
                caption=caption, reply_markup=kb.moderation(tg_id))
            if sent.photo:
                result["doc_file_id"] = sent.photo[-1].file_id
            result["mod_chat_id"] = sent.chat.id
            result["mod_message_id"] = sent.message_id
        except Exception:                               # noqa: BLE001
            log.exception("карточка модерации %s не доставлена - заявка "
                          "останется видна в CRM", tg_id)
            await self.db.log_event(tg_id, "moderation_card_failed",
                                    {"via": "app"})
        return result

    async def _notify_client(self, tg_id: int, key: str, **fmt) -> None:
        """Сообщение клиенту от бота на его языке - как _notify в модерации."""
        if self.bot is None or self.db is None:
            return
        from .. import i18n
        row = await self.db.get_user(tg_id)
        text = i18n.t(i18n.user_lang(dict(row) if row else None), key)
        if fmt:
            text = text.format(**fmt)
        try:
            await self.bot.send_message(tg_id, text)
        except Exception:                               # noqa: BLE001
            log.exception("клиент %s не получил уведомление %s", tg_id, key)

    # ─────────────────────── KYC в CRM ───────────────────────

    async def admin_kyc(self, request: web.Request) -> web.Response:
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        if not self._reg_ready:
            return _json({"error": "KYC в CRM не подключён."}, status=503)
        from .. import logic as bot_logic
        rows = await self.db.pool.fetch(
            "select * from bot.users where status = 'pending' "
            "order by updated_at desc limit 50")
        items = []
        for row in rows:
            data = dict(row)
            anketa = self.vault.decrypt(data.get("anketa_enc"))
            duplicates = 0
            if data.get("doc_sha256"):
                duplicates = await self.db.count_duplicate_docs(
                    data["tg_id"], data["doc_sha256"])
            items.append({
                "tg_id": data["tg_id"], "username": data.get("username"),
                "full_name": data.get("full_name"),
                "phone": data.get("phone"),
                "submitted": _fmt(data.get("updated_at")),
                "minor": bot_logic.is_minor(anketa),
                "has_doc": bool(data.get("doc_path")),
                "has_parent": bool(data.get("parent_path")),
                "duplicates": duplicates,
                "anketa": [
                    [label, anketa.get(field) or "—"]
                    for field, label in bot_logic.CONTRACT_LABELS
                    if field in bot_logic.ANKETA_FIELDS
                ],
            })
        return _json({
            "items": items,
            "reject_reasons": [[code, title] for code, (title, _)
                               in bot_logic.REJECT_REASONS.items()],
        })

    async def admin_kyc_photo(self, request: web.Request) -> web.Response:
        """Скан из заявки. ПДн - поэтому только за админской авторизацией,
        и путь перед отдачей сверяется с шаблоном хранилища, как в ретеншене."""
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        if not self._reg_ready:
            raise web.HTTPNotFound
        from .. import logic as bot_logic
        try:
            tg_id = int(request.match_info["tg_id"])
        except ValueError:
            raise web.HTTPNotFound from None
        kind = request.match_info["kind"]
        if kind not in ("doc", "parent"):
            raise web.HTTPNotFound
        row = await self.db.get_user(tg_id)
        path = dict(row).get(f"{kind}_path") if row else None
        if not path or not bot_logic.is_safe_store_path(
                path, self.cfg.storage_dir):
            raise web.HTTPNotFound
        return web.FileResponse(path)

    async def admin_kyc_decide(self, request: web.Request) -> web.Response:
        """Решение по заявке из CRM - тот же перевод статуса, что кнопки
        в Telegram: guard по expected_status не даст двум модераторам
        (или CRM и кнопке разом) решить одну заявку дважды."""
        if not self._is_admin(request):
            return _json({"error": "auth"}, status=401)
        if not self._reg_ready:
            return _json({"error": "KYC в CRM не подключён."}, status=503)
        from .. import logic as bot_logic
        from ..db import utcnow
        try:
            body = await request.json()
            tg_id = int(body["tg_id"])
            approve = bool(body["approve"])
        except (ValueError, KeyError, TypeError):
            return _json({"error": "Не понял запрос."}, status=400)
        row = await self.db.get_user(tg_id)
        if row is None:
            return _json({"error": "Заявка не найдена."}, status=404)
        data = dict(row)
        admin_id = self._admin_id(request)

        if approve:
            ok = await self.db.patch(
                tg_id, expected_status=bot_logic.ST_PENDING,
                status=bot_logic.ST_APPROVED, state=bot_logic.PENDING,
                reject_reason=None, reviewed_by=admin_id,
                reviewed_at=utcnow())
            if not ok:
                return _json({"error": "Заявка уже обработана."}, status=409)
            await self.db.log_event(tg_id, "moderation_approved",
                                    {"by": admin_id or 0, "via": "crm"})
            await self._notify_client(tg_id, "APPROVED_WAIT_ISSUE")
            await self._send_issue_prompt(tg_id, data)
        else:
            code = str(body.get("reason_code") or "")
            if code in bot_logic.REJECT_REASONS:
                reason = bot_logic.REJECT_REASONS[code][0]
                back_to = bot_logic.reject_back_to(
                    code, self.vault.decrypt(data.get("anketa_enc")))
            else:
                comment = bot_logic.reject_comment(body.get("comment"))
                if not comment.ok:
                    return _json({"errors": {"comment": comment.error}},
                                 status=400)
                reason, back_to = comment.value, bot_logic.WAIT_FIO
            ok = await self.db.patch(
                tg_id, expected_status=bot_logic.ST_PENDING,
                status=bot_logic.ST_REJECTED, state=back_to,
                reject_reason=reason, reviewed_by=admin_id,
                reviewed_at=utcnow())
            if not ok:
                return _json({"error": "Заявка уже обработана."}, status=409)
            await self.db.log_event(tg_id, "moderation_rejected",
                                    {"by": admin_id or 0, "reason": reason,
                                     "via": "crm"})
            await self.db.set_purge_after(tg_id, self.cfg.purge_rejected_days)
            await self._notify_client(tg_id, "REJECTED_WITH_REASON",
                                      reason=bot_logic.esc(reason))
        # Снять кнопки с Telegram-карточки: второй модератор не должен
        # решать уже решённую заявку. Сбой не критичен - guard отобьёт.
        if self.bot is not None and data.get("mod_chat_id"):
            try:
                await self.bot.edit_message_reply_markup(
                    data["mod_chat_id"], data["mod_message_id"],
                    reply_markup=None)
            except Exception:                           # noqa: BLE001
                log.debug("кнопки карточки %s не сняты", tg_id)
        return _json({"ok": True})

    async def _send_issue_prompt(self, tg_id: int, data: dict) -> None:
        """Приглашение «данные выдачи» - как после «Одобрить» в Telegram:
        без него одобренная заявка не поедет дальше по конвейеру договора."""
        if self.bot is None or self.admin_chat_id is None:
            return
        from .. import logic as bot_logic
        from .. import texts
        fio = data.get("full_name") or "без имени"
        try:
            sent = await self.bot.send_message(
                self.admin_chat_id,
                texts.ISSUE_PROMPT.format(fio=bot_logic.esc(fio), tg_id=tg_id,
                                          form=bot_logic.ISSUE_FORM_TEMPLATE))
            await self.db.patch(tg_id, issue_chat_id=sent.chat.id,
                                issue_message_id=sent.message_id)
        except Exception as exc:                        # noqa: BLE001
            log.exception("приглашение выдачи для %s не доставлено", tg_id)
            await self.db.log_event(tg_id, "issue_prompt_failed",
                                    {"error": str(exc)})

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
        # Честно про ограничение: деактивировать QR в банке нечем, ссылка
        # у клиента живёт до конца суток. Оплату по ней фоновая задача
        # заметит и поднимет счёт в paid - но лучше, чтобы оператор знал.
        return _json({"ok": True, "warning":
                      "Ссылка оплаты у клиента остаётся действующей до конца "
                      "суток. Если он оплатит по ней - счёт сам поднимется "
                      "в «оплачен», и вы увидите карточку."})

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
              tochka=None, db=None, vault=None, cfg=None) -> web.Application:
    api = Api(fleet, bot=bot, admin_chat_id=admin_chat_id, bot_token=bot_token,
              crm_token=crm_token, admins=admins, starline=starline,
              tochka=tochka, db=db, vault=vault, cfg=cfg)
    # client_max_size: анкета приходит с двумя фото до 12 МБ; дефолтный
    # мегабайт aiohttp резал бы её на середине загрузки.
    app = web.Application(client_max_size=30 * 1024 * 1024)
    app.add_routes([
        web.get("/", api.index),
        web.get("/app", api.app_page),
        web.get("/api/health", api.health),
        web.get("/policy", api.policy_file),
        web.get("/api/reg/status", api.reg_status),
        web.post("/api/reg/submit", api.reg_submit),
        web.get("/api/admin/kyc", api.admin_kyc),
        web.get("/api/admin/kyc/photo/{tg_id}/{kind}", api.admin_kyc_photo),
        web.post("/api/admin/kyc/decide", api.admin_kyc_decide),
        web.get("/api/points", api.points),
        web.get("/api/models", api.models),
        web.get("/api/availability", api.availability),
        web.get("/api/me", api.me),
        web.post("/api/book", api.book),
        web.post("/api/cancel", api.cancel),
        web.post("/api/extend", api.extend),
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
        web.post("/api/admin/bike/price", api.admin_bike_price),
        web.post("/api/admin/bike/since", api.admin_bike_since),
        web.post("/api/admin/bike/point", api.admin_bike_point),
        web.get("/api/admin/analytics", api.admin_analytics),
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
                    tochka=None, db=None, vault=None, cfg=None) -> web.AppRunner:
    """Поднимает витрину и возвращает runner - его гасит main() при остановке."""
    runner = web.AppRunner(build_app(
        fleet, bot=bot, admin_chat_id=admin_chat_id, bot_token=bot_token,
        crm_token=crm_token, admins=admins, starline=starline, tochka=tochka,
        db=db, vault=vault, cfg=cfg))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("витрина парка, Mini App и CRM слушают порт %s", port)
    return runner
