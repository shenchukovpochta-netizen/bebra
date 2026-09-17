"""Веб-панель CRM: FastAPI + Jinja2, формы без JavaScript-фреймворков.

Всё серверное: страница - это шаблон, действие - POST формы и редирект.
Так панель открывается с любого телефона, а код читается сверху вниз.
Данные приходят из CrmDB (или его заглушки в тестах), решения - из
app.crm.logic и app.crm.service, уведомления клиентам - app.crm.notify.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .. import logic as bot_logic
from ..crm import company, import_xlsx, logic, notify, service
from ..services import contract as contract_service
from .config import WebConfig

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
PUBLIC = ("/login", "/static", "/healthz")
# Свой кабинет доступен любому сотруднику, каким бы урезанным ни был профиль.
ALWAYS_OPEN = ("/logout", "/me", "/me/password")
SESSION_DAYS = 14
# Перебор пароля: после LOGIN_LIMIT неудач по одному логину вход в него
# закрыт на LOGIN_WINDOW секунд; отдельный, более щедрый предел на адрес
# (LOGIN_IP_LIMIT) - против перебора логинов. За Caddy и SSH-туннелем все
# запросы приходят с одного адреса, поэтому основной ключ - логин: иначе
# чужие десять попыток закрывали бы вход всем сотрудникам. Память
# процесса, без базы: панель одна, и рестарт, обнуляющий счётчик,
# атакующему ничего не даёт.
LOGIN_LIMIT, LOGIN_IP_LIMIT, LOGIN_WINDOW = 10, 100, 15 * 60
LOGIN_KEYS_SWEEP = 500
# Учётная таблица проката - сотни строк, единицы мегабайт.
IMPORT_MAX_BYTES = 20 * 1024 * 1024


def _local(value: datetime) -> datetime:
    """Момент из базы (timestamptz приходит в UTC) - в часовом поясе
    контейнера (TZ в compose), как его ждёт оператор."""
    return value.astimezone() if value.tzinfo else value


def _dmy(value: Any) -> str:
    if not value:
        return "—"
    if isinstance(value, datetime):
        return _local(value).strftime("%d.%m.%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    return str(value)


def _file_exists(path: str) -> bool:
    return os.path.exists(path)


def _csv(filename: str, header: list[str], rows: list[list[Any]]) -> Response:
    """CSV для Excel: BOM, точка с запятой, десятичная запятая."""
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# Символы, с которых Excel и LibreOffice начинают формулу. Имя клиента
# приходит из бота как набрал человек: «=HYPERLINK(...)» в ФИО превратил бы
# выгрузку в фишинговую ссылку у оператора. Такие строки отдаются как
# формула-строка ="...": таблица показывает текст и ничего не вычисляет.
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value:.2f}".replace(".", ",")
    if isinstance(value, datetime):
        return _local(value).strftime("%d.%m.%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    text = str(value)
    if text.startswith(_FORMULA_STARTS):
        return '="' + text.replace('"', '""') + '"'
    return text


def _iso(value: Any) -> str:
    return value.strftime("%Y-%m-%d") if isinstance(value, date) else ""


def create_app(*, crm: Any, db: Any, cfg: WebConfig, bot: Any = None) -> FastAPI:
    app = FastAPI(title=cfg.title, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals.update(
        money=logic.money, money_signed=logic.money_signed, period_label=logic.period_label,
        KINDS=logic.KINDS, METHODS=logic.METHODS, BIKE_STATUSES=logic.BIKE_STATUSES,
        BIKE_MANUAL_STATUSES=logic.BIKE_MANUAL_STATUSES,
        OPERATIONAL_STATUSES=logic.OPERATIONAL_STATUSES, IDLE_STATUSES=logic.IDLE_STATUSES,
        LOCATIONS=logic.LOCATIONS, REPAIR_NODES=logic.REPAIR_NODES,
        TRACKER_ALERTS=logic.TRACKER_ALERTS,
        map_url=logic.map_url,
        BATTERY_STATUSES=logic.BATTERY_STATUSES,
        BATTERY_MANUAL_STATUSES=logic.BATTERY_MANUAL_STATUSES,
        BATTERY_CYCLES_WARN=logic.BATTERY_CYCLES_WARN,
        IDLE_TARGET_PERCENT=logic.IDLE_TARGET_PERCENT, CHECK_TARGET=logic.CHECK_TARGET,
        amortization_month=logic.amortization_month, fleet_losses=logic.fleet_losses,
        ridden=logic.ridden, ridden_per_day=logic.ridden_per_day,
        INTENTS=logic.INTENTS,
        CLIENT_STATUSES=logic.CLIENT_STATUSES, RENTAL_STATUSES=logic.RENTAL_STATUSES,
        BILLING=logic.BILLING, ROLES=logic.ROLES, app_title=cfg.title,
        ORDER_STATUSES=logic.ORDER_STATUSES, PAYERS=logic.PAYERS,
        WORK_CATEGORIES=logic.WORK_CATEGORIES, ORDER_STUCK_DAYS=logic.ORDER_STUCK_DAYS,
        TAKE_SCOPES=logic.TAKE_SCOPES, TAKE_STATES=logic.TAKE_STATES,
        REF_STATUSES=logic.REF_STATUSES, staff_tg_label=logic.staff_tg_label,
        COMPANY_FIELDS=company.COMPANY_FIELDS,
        CLIENT_CHANNELS=logic.CLIENT_CHANNELS, channel_label=logic.channel_label,
        MOVE_KINDS=logic.MOVE_KINDS, DOC_KINDS=logic.DOC_KINDS,
        SWAP_REASONS=logic.SWAP_REASONS, in_search=logic.in_search,
        search_days=logic.search_days,
        PART_ORDER_STATUSES=logic.PART_ORDER_STATUSES,
        NEED_SOURCES=logic.NEED_SOURCES, PART_UNITS=logic.PART_UNITS,
        INTEGRITY_KINDS=logic.INTEGRITY_KINDS, DEBT_NOISE=logic.DEBT_NOISE,
        take_title=logic.take_title,
        SECTIONS=logic.SECTIONS, ACTIONS=logic.ACTIONS, LEVELS=logic.LEVELS,
        LEVEL_ORDER=logic.LEVEL_ORDER, can_view=logic.can_view, can_edit=logic.can_edit,
        can_act=logic.can_act, visible_sections=logic.visible_sections,
        home_for=logic.home_for,
        today=date.today, bot_enabled=bot is not None,
    )
    templates.env.filters["dmy"] = _dmy
    templates.env.filters["iso"] = _iso

    # ─────────────────────── обвязка ───────────────────────

    def render(request: Request, name: str, status_code: int = 200, **ctx: Any) -> Response:
        messages = list(request.session.get("flash") or [])
        if messages:
            request.session["flash"] = []
        ctx.update(staff=getattr(request.state, "staff", None), flash=messages)
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def flash(request: Request, text: str, kind: str = "ok") -> None:
        # Присваивание, а не append: сессия Starlette пишет cookie только
        # при изменении своих ключей, правку вложенного списка она не видит.
        request.session["flash"] = [*(request.session.get("flash") or []), [kind, text]]

    def redirect(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    def who(request: Request) -> str:
        staff = getattr(request.state, "staff", None)
        return f"staff:{staff['login']}" if staff else "staff:?"

    def may_view(request: Request, code: str) -> bool:
        return logic.can_view(getattr(request.state, "staff", None), code)

    def may_edit(request: Request, code: str) -> bool:
        return logic.can_edit(getattr(request.state, "staff", None), code)

    def denied(request: Request, code: str) -> Response:
        """Отказ показывается страницей, а не голым 403: оператор должен
        увидеть, какого права ему не хватает, и кому писать."""
        return render(request, "denied.html", status_code=403,
                      what=logic.SECTIONS.get(code) or logic.ACTIONS.get(code, code))

    async def auth(request: Request, call_next: Any) -> Response:
        request.state.staff = None
        staff_id = request.session.get("staff_id")
        if staff_id:
            staff = await crm.staff_by_id(int(staff_id))
            if staff and staff.get("active"):
                request.state.staff = staff
        path = request.url.path
        if request.state.staff is None and not path.startswith(PUBLIC):
            target = path + (f"?{request.url.query}" if request.url.query else "")
            return redirect("/login?next=" + quote(target, safe=""))
        # Один страж на все маршруты раздела: забыть его в новом обработчике
        # нельзя, поэтому дыры вида «страницу закрыли, а POST оставили» не
        # появляются. Свой пароль и выход открыты всегда.
        code = logic.section_for(path) if path not in ALWAYS_OPEN else None
        if code and request.state.staff is not None:
            allowed = (may_view(request, code) if request.method in ("GET", "HEAD")
                       else may_edit(request, code))
            if not allowed:
                return denied(request, code)
        return await call_next(request)

    # Порядок важен: последний add_middleware - внешний. Сессия должна быть
    # распакована ДО проверки входа, поэтому SessionMiddleware добавляется
    # после auth.
    app.add_middleware(BaseHTTPMiddleware, dispatch=auth)
    app.add_middleware(SessionMiddleware, secret_key=cfg.secret, session_cookie="crm_session",
                       same_site="strict", max_age=SESSION_DAYS * 24 * 3600)

    login_failures: dict[str, list[float]] = {}

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "?"

    def login_throttled(key: str, limit: int) -> bool:
        now = time.monotonic()
        if len(login_failures) > LOGIN_KEYS_SWEEP:
            # Ключи - логины, которые выбирает атакующий: без чистки словарь
            # рос бы бесконечно. Стираются те, у кого окно уже истекло.
            for stale in [k for k, ts in login_failures.items()
                          if not ts or now - ts[-1] >= LOGIN_WINDOW]:
                login_failures.pop(stale, None)
        recent = [t for t in login_failures.get(key, ()) if now - t < LOGIN_WINDOW]
        if recent:
            login_failures[key] = recent
        else:
            login_failures.pop(key, None)
        return len(recent) >= limit

    async def form(request: Request) -> dict[str, str]:
        data = await request.form()
        return {k: (v if isinstance(v, str) else "") for k, v in data.items()}

    async def form_ids(request: Request, name: str) -> list[int]:
        """Отмеченные галочками номера: form() оставляет только последний."""
        data = await request.form()
        return [int(v) for v in data.getlist(name) if str(v).isdigit()]

    def cost_field(data: dict, name: str) -> logic.Check:
        """Стоимость в форме: пусто и «0» - ноль (запчастей не было, работа
        своя), иначе обычная проверка суммы."""
        raw = (data.get(name) or "").strip().replace(",", ".")
        if raw in ("", "0", "0.0", "0.00"):
            return logic.Check(True, Decimal(0))
        return logic.check_amount(raw)

    def count_field(data: dict, name: str, *, what: str, default: str = "1",
                    limit: int = 999) -> logic.Check:
        """Небольшое целое из формы: количество в наряде, минуты норматива."""
        raw = (data.get(name) or "").strip() or default
        if not raw.isdigit() or not 0 <= int(raw) <= limit:
            return logic.Check(False, error=f"{what}: целое число от 0 до {limit}.")
        return logic.Check(True, int(raw))

    def summarize(rental: dict | None, balance: Any) -> dict:
        return logic.rental_summary(rental, balance, today=date.today())

    # ─────────────────────── вход ───────────────────────

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/login")
    async def login_form(request: Request) -> Response:
        if request.state.staff is not None:
            return redirect("/")
        return render(request, "login.html", next=request.query_params.get("next") or "/")

    @app.post("/login")
    async def login(request: Request) -> Response:
        data = await form(request)
        login_key = "login:" + (data.get("login") or "").strip().lower()[:64]
        ip_key = "ip:" + client_ip(request)
        if login_throttled(login_key, LOGIN_LIMIT) or login_throttled(ip_key, LOGIN_IP_LIMIT):
            return render(request, "login.html", status_code=429,
                          error="Слишком много попыток входа. Подождите 15 минут.",
                          next=data.get("next") or "/")
        login_check = logic.check_login(data.get("login"))
        staff = await crm.staff_by_login(login_check.value) if login_check.ok else None
        if (staff is None or not staff.get("active")
                or not logic.verify_password(data.get("password") or "",
                                             staff.get("password_hash"))):
            for key in (login_key, ip_key):
                login_failures.setdefault(key, []).append(time.monotonic())
            return render(request, "login.html", status_code=401,
                          error="Неверный логин или пароль.",
                          next=data.get("next") or "/")
        login_failures.pop(login_key, None)
        request.session.clear()
        request.session["staff_id"] = staff["id"]
        home = logic.home_for(staff)
        target = data.get("next") or home
        if not target.startswith("/") or target.startswith("//"):
            target = home
        if target == "/" and home != "/":
            target = home
        return redirect(target)

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        request.session.clear()
        return redirect("/login")

    @app.get("/me")
    async def my_page(request: Request) -> Response:
        """Свой кабинет: пароль и список выданных прав. Открыт всем —
        сотрудник должен видеть, что ему разрешено, не спрашивая владельца."""
        return render(request, "me.html")

    @app.post("/me/password")
    async def my_password(request: Request) -> Response:
        data = await form(request)
        staff = request.state.staff
        if not logic.verify_password(data.get("old") or "", staff.get("password_hash")):
            flash(request, "Текущий пароль неверный.", "err")
            return redirect("/me")
        check = logic.check_password(data.get("new"))
        if not check.ok:
            flash(request, check.error, "err")
            return redirect("/me")
        await crm.set_staff_password(staff["id"], logic.hash_password(check.value))
        flash(request, "Пароль изменён.")
        return redirect("/me")

    # ─────────────────────── дашборд ───────────────────────

    @app.get("/")
    async def dashboard(request: Request) -> Response:
        rentals = await crm.active_rentals()
        rows = []
        for r in rentals:
            s = summarize(r, r.get("balance", 0))
            rows.append({**r, "summary": s})
        today = date.today()
        expiring = logic.expiring(rows, today=today, before_days=cfg.remind_before_days)
        bikes_by = await crm.bike_counts()
        fleet = await crm.bikes(limit=10000)
        own_batteries = await crm.batteries(limit=10000)
        metrics = await period_metrics(days=30)
        settings = await crm.settings()
        operational = sum(bikes_by.get(s, 0) for s in logic.OPERATIONAL_STATUSES)
        plan = logic.month_plan(settings, fleet=operational)
        first = today.replace(day=1)
        next_month = (first + timedelta(days=32)).replace(day=1)
        month_metrics = await period_metrics(
            since=datetime.combine(first, datetime.min.time()).astimezone(),
            until=datetime.now().astimezone())
        soon = logic.freeing_soon(rows, today=today)
        return render(request, "dashboard.html",
                      plan=plan,
                      progress=logic.plan_progress(
                          plan, month_metrics,
                          days_in_month=(next_month - first).days,
                          days_passed=today.day),
                      soon=soon,
                      counts=await crm.counts(), bikes=bikes_by,
                      operational=operational,
                      metrics=metrics, losses=logic.fleet_losses(metrics),
                      loss_today=logic.loss_per_day(bikes_by),
                      amortization=logic.amortization_total(fleet, own_batteries),
                      idle_by_location=idle_by_location(fleet),
                      claims=await crm.pending_claims(), rentals=rows,
                      expiring=expiring, before_days=cfg.remind_before_days,
                      forecast=logic.forecast_summary(bikes_by.get("available", 0), soon),
                      debtors=await crm.debtors(10),
                      month=await crm.ledger_totals(since=today.replace(day=1)))

    @app.post("/plan")
    async def plan_save(request: Request) -> Response:
        """План месяца: сколько велосипедов держать в аренде и по какому чеку.

        Умолчания считаются от парка и целей, поэтому план правится, а не
        придумывается с нуля.
        """
        if not may_edit(request, "finance"):
            return denied(request, "finance")
        data = await form(request)
        rented = count_field(data, "plan_rented", what="Велосипедов в аренде",
                             default="0", limit=9999)
        check = cost_field(data, "plan_check")
        for field in (rented, check):
            if not field.ok:
                flash(request, field.error, "err")
                return redirect("/")
        await crm.set_setting("plan_rented", str(rented.value), by=who(request))
        await crm.set_setting("plan_check", str(check.value), by=who(request))
        flash(request, "План на месяц сохранён.")
        return redirect("/")

    async def referral_bonus(client: dict, amount: Decimal, by: str) -> None:
        """Друг заплатил - начислить бонус агенту и сказать ему об этом.

        Зовётся после каждого платежа клиента: платёж может прийти из
        панели, из заявки и из выдачи, а бонус обязан начисляться один раз
        и одинаково.
        """
        try:
            bonus = await service.ref_paid(crm, client, amount, by=by)
        except Exception:                                # noqa: BLE001
            log.exception("реферальный бонус за клиента %s не начислен",
                          client.get("id"))
            return
        if bonus:
            await notify.referral_bonus(bot, db, bonus["agent"], client,
                                        bonus["bonus"])

    async def period_metrics(*, days: int = 0, since: datetime | None = None,
                             until: datetime | None = None) -> dict:
        """Три числа за период: простой, средний чек, дни. По умолчанию -
        последние `days` дней до текущего момента."""
        now = datetime.now().astimezone()
        until = until or now
        since = since or (until - timedelta(days=days))
        return logic.fleet_metrics(await crm.bike_days_by_status(since, until),
                                   await crm.rental_revenue(since, until))

    def idle_by_location(fleet: list[dict]) -> list[dict]:
        """Где стоят простаивающие велосипеды: по точкам, свободные отдельно
        от ремонта, чтобы было видно, что выдавать нечего, а что чинить."""
        out: dict[str, dict] = {}
        for b in fleet:
            if b.get("status") not in logic.IDLE_STATUSES:
                continue
            row = out.setdefault(b.get("location") or "не на точке",
                                 {"location": b.get("location") or "не на точке",
                                  "free": 0, "repair": 0})
            if b["status"] in ("available", "reserved"):
                row["free"] += 1
            else:
                row["repair"] += 1
        order = {loc: i for i, loc in enumerate(logic.LOCATIONS)}
        return sorted(out.values(), key=lambda r: (order.get(r["location"], 99), r["location"]))

    @app.post("/billing/run")
    async def billing_run(request: Request) -> Response:
        done = await service.charge_all(crm, today=date.today())
        flash(request, f"Начислений сделано: {done}.")
        return redirect("/")

    # ─────────────────────── клиенты ───────────────────────

    @app.get("/clients")
    async def clients(request: Request) -> Response:
        q = request.query_params.get("q") or ""
        status = request.query_params.get("status") or ""
        rows = await crm.clients(q=q or None, status=status or None)
        for r in rows:
            rental = {"status": "active", "billed_until": r["billed_until"],
                      "price": r["price"], "period_days": r["period_days"],
                      "tariff_name": r["tariff_name"], "bike_model": r.get("bike_model"),
                      "bike_code": r.get("bike_code")} if r.get("rental_id") else None
            r["summary"] = summarize(rental, r.get("balance", 0))
        return render(request, "clients.html", rows=rows, q=q, status=status)

    @app.get("/clients.csv")
    async def clients_csv(request: Request) -> Response:
        # В выгрузке колонка «Баланс»: она уезжает файлом, поэтому право
        # на финансы обязательно - на самой странице баланс тоже скрыт.
        if not may_view(request, "finance"):
            return denied(request, "finance")
        q = request.query_params.get("q") or ""
        status = request.query_params.get("status") or ""
        rows = []
        for c in await crm.clients(q=q or None, status=status or None, limit=10000):
            rental = ({"status": "active", "billed_until": c["billed_until"],
                       "price": c["price"], "period_days": c["period_days"]}
                      if c.get("rental_id") else None)
            s = summarize(rental, c.get("balance", 0))
            rows.append([c["full_name"], c["phone"], logic.CLIENT_STATUSES.get(c["status"]),
                         ("@" + c["username"]) if c.get("username") else
                         ("есть" if c.get("tg_id") else ""),
                         logic.to_money(c.get("balance", 0)), c.get("bike_code"),
                         c.get("tariff_name"), s.get("covered_until"),
                         c.get("contract_no"), c.get("created_at")])
        return _csv("clients.csv",
                    ["ФИО", "Телефон", "Статус", "Telegram", "Баланс", "Велосипед",
                     "Тариф", "Оплачено до", "Договор", "Добавлен"], rows)

    @app.get("/clients/new")
    async def client_new(request: Request) -> Response:
        # «Завести нового» - правка раздела, а не просмотр: страж судит
        # по методу запроса и такую страницу пропустил бы.
        if not may_edit(request, "clients"):
            return denied(request, "clients")
        return render(request, "client_form.html", client=None)

    async def _client_fields(request: Request, data: dict, *, current: dict | None) -> dict | None:
        name = logic.check_name(data.get("full_name"), what="ФИО")
        phone = bot_logic.normalize_phone(data.get("phone"))
        note = logic.check_note(data.get("note"))
        status = logic.check_choice(data.get("status") or "active", logic.CLIENT_STATUSES,
                                    what="Статус")
        contract = logic.check_name(data.get("contract_no"), what="Договор") \
            if (data.get("contract_no") or "").strip() else logic.Check(True, None)
        channel = logic.check_channel(data.get("channel"))
        for check in (name, note, status, contract, channel):
            if not check.ok:
                flash(request, check.error, "err")
                return None
        if phone is None:
            flash(request, "Телефон: не похоже на номер. Пример: +7 900 123-45-67.", "err")
            return None
        other = await crm.client_by_phone(phone)
        if other is not None and (current is None or other["id"] != current["id"]):
            flash(request, f"Этот телефон уже у клиента «{other['full_name']}».", "err")
            return None
        return {"full_name": name.value, "phone": phone, "note": note.value,
                "status": status.value, "contract_no": contract.value,
                "channel": channel.value}

    @app.post("/clients")
    async def client_create(request: Request) -> Response:
        data = await form(request)
        fields = await _client_fields(request, data, current=None)
        if fields is None:
            return redirect("/clients/new")
        client_id = await crm.create_client(full_name=fields["full_name"],
                                            phone=fields["phone"], note=fields["note"],
                                            contract_no=fields["contract_no"])
        patch = {key: fields[key] for key in ("channel",) if fields.get(key)}
        if fields["status"] != "active":
            patch["status"] = fields["status"]
        if patch:
            await crm.update_client(client_id, **patch)
        flash(request, "Клиент добавлен.")
        return redirect(f"/clients/{client_id}")

    @app.get("/clients/{client_id}")
    async def client_card(request: Request, client_id: int) -> Response:
        client = await crm.client(client_id)
        if client is None:
            return render(request, "missing.html", status_code=404, what="Клиент")
        balance = await crm.client_balance(client_id)
        rental = await crm.active_rental_of(client_id)
        bot_user = await db.get_user(client["tg_id"]) if client.get("tg_id") else None
        bot_user = dict(bot_user) if bot_user else None
        has_contract = bool(bot_user and bot_user.get("contract_status") == "signed"
                            and bot_user.get("contract_path"))
        return render(request, "client.html", client=client, balance=balance,
                      rental=rental, summary=summarize(rental, balance),
                      ledger=await crm.ledger_of(client_id, 100),
                      rentals=await crm.client_rentals(client_id),
                      claim=await crm.pending_claim_of(client_id),
                      bot_user=bot_user, has_contract=has_contract)

    @app.post("/clients/{client_id}/edit")
    async def client_edit(request: Request, client_id: int) -> Response:
        client = await crm.client(client_id)
        if client is None:
            return render(request, "missing.html", status_code=404, what="Клиент")
        data = await form(request)
        fields = await _client_fields(request, data, current=client)
        if fields is not None:
            await crm.update_client(client_id, **fields)
            flash(request, "Карточка сохранена.")
        return redirect(f"/clients/{client_id}")

    @app.post("/clients/{client_id}/ledger")
    async def client_ledger_add(request: Request, client_id: int) -> Response:
        if not logic.can_act(request.state.staff, "money_edit"):
            return denied(request, "money_edit")
        client = await crm.client(client_id)
        if client is None:
            return render(request, "missing.html", status_code=404, what="Клиент")
        data = await form(request)
        kind = logic.check_choice(data.get("kind"), ("payment", "fine", "refund", "adjust"),
                                  what="Вид записи")
        amount = logic.check_amount(data.get("amount"),
                                    allow_negative=data.get("kind") == "adjust")
        note = logic.check_note(data.get("note"))
        method = data.get("method") or None
        if method and not logic.check_choice(method, logic.METHODS).ok:
            method = None
        for check in (kind, amount, note):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(f"/clients/{client_id}")
        rental = await crm.active_rental_of(client_id)
        await service.add_entry(crm, client, kind=kind.value, amount=amount.value,
                                method=method, note=note.value, by=who(request),
                                rental_id=rental["id"] if rental else None)
        if kind.value == "payment":
            await notify.payment_credited(bot, db, crm, client, amount.value)
            await referral_bonus(client, amount.value, who(request))
        flash(request, "Запись добавлена.")
        return redirect(f"/clients/{client_id}")

    @app.get("/clients/{client_id}/contract")
    async def client_contract(request: Request, client_id: int) -> Response:
        # В договоре паспортные данные: право на него отдельное от карточки.
        if not logic.can_act(request.state.staff, "client_docs"):
            return denied(request, "client_docs")
        client = await crm.client(client_id)
        if client is None or not client.get("tg_id"):
            return render(request, "missing.html", status_code=404, what="Договор")
        row = await db.get_user(client["tg_id"])
        data = dict(row) if row else {}
        path = data.get("contract_path")
        if not path or not bot_logic.is_safe_store_path(path, cfg.storage_dir) \
                or not _file_exists(path):
            return render(request, "missing.html", status_code=404, what="Договор")
        return FileResponse(path, filename=Path(path).name)

    # ─────────────────────── парк ───────────────────────

    @app.get("/bikes")
    async def bikes(request: Request) -> Response:
        q = request.query_params.get("q") or ""
        status = request.query_params.get("status") or ""
        location = request.query_params.get("location") or ""
        return render(request, "bikes.html", q=q, status=status, location=location,
                      rows=await crm.bikes(q=q or None, status=status or None,
                                           location=location or None),
                      counts=await crm.bike_counts())

    @app.get("/bikes/new")
    async def bike_new(request: Request) -> Response:
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        return render(request, "bike_form.html", bike=None)

    def _bike_fields(request: Request, data: dict) -> dict | None:
        code = logic.check_code(data.get("code"))
        model = logic.check_name(data.get("model"), what="Модель")
        note = logic.check_note(data.get("note"))
        batteries = data.get("battery_count") or "2"
        price = (logic.check_amount(data.get("purchase_price"))
                 if (data.get("purchase_price") or "").strip() else logic.Check(True, None))
        bought = logic.check_date(data.get("purchased_on"), default=None) \
            if (data.get("purchased_on") or "").strip() else logic.Check(True, None)
        for check in (code, model, note, price, bought):
            if not check.ok:
                flash(request, check.error, "err")
                return None
        if not batteries.isdigit() or not 0 <= int(batteries) <= 10:
            flash(request, "АКБ: число от 0 до 10.", "err")
            return None
        location = (data.get("location") or "").strip()
        if location and location not in logic.LOCATIONS:
            flash(request, "Точка: недопустимое значение.", "err")
            return None
        months = data.get("service_months") or "24"
        bat_months = data.get("battery_service_months") or "15"
        for label, value in (("Срок службы", months), ("Срок службы АКБ", bat_months)):
            if not value.isdigit() or not 1 <= int(value) <= 240:
                flash(request, f"{label}: число месяцев от 1 до 240.", "err")
                return None
        residual = cost_field(data, "residual_price")
        bat_price = (logic.check_amount(data.get("battery_price"))
                     if (data.get("battery_price") or "").strip() else logic.Check(True, None))
        # Пробег правится руками: одометр могли не переписать при выдаче,
        # а здесь поле пустое означает «не трогать», а не «обнулить».
        mileage = logic.check_mileage(data.get("mileage_km"), required=False)
        for check in (residual, bat_price, mileage):
            if not check.ok:
                flash(request, check.error, "err")
                return None
        return {"code": code.value, "model": model.value, "note": note.value,
                "frame_no": (data.get("frame_no") or "").strip() or None,
                "motor_no": (data.get("motor_no") or "").strip() or None,
                "battery_count": int(batteries), "purchase_price": price.value,
                "purchased_on": bought.value, "location": location or None,
                "service_months": int(months), "residual_price": residual.value,
                "battery_price": bat_price.value, "battery_service_months": int(bat_months),
                # Подменный держат под замены, а не под выдачу: своего
                # статуса у него нет, он такой же свободный.
                "spare": bool(data.get("spare")),
                **({"mileage_km": mileage.value} if mileage.value is not None else {})}

    @app.post("/bikes")
    async def bike_create(request: Request) -> Response:
        data = await form(request)
        fields = _bike_fields(request, data)
        if fields is None:
            return redirect("/bikes/new")
        if await crm.bike_by_code(fields["code"]) is not None:
            flash(request, f"Инвентарный номер {fields['code']} уже занят.", "err")
            return redirect("/bikes/new")
        if fields["frame_no"] and await crm.bike_by_frame(fields["frame_no"]) is not None:
            flash(request, "Велосипед с таким номером рамы уже есть.", "err")
            return redirect("/bikes/new")
        bike_id = await crm.create_bike(by=who(request), **fields)
        flash(request, "Велосипед добавлен.")
        return redirect(f"/bikes/{bike_id}")

    @app.get("/bikes/{bike_id}")
    async def bike_card(request: Request, bike_id: int) -> Response:
        bike = await crm.bike(bike_id)
        if bike is None:
            return render(request, "missing.html", status_code=404, what="Велосипед")
        return render(request, "bike.html", bike=bike, log=await crm.bike_log(bike_id),
                      rentals=await crm.bike_rentals(bike_id),
                      status_log=await crm.bike_status_log(bike_id),
                      nodes=await crm.repair_nodes(),
                      order=await crm.open_order_of(bike_id),
                      amortization=logic.amortization_month(bike))

    @app.post("/bikes/{bike_id}/edit")
    async def bike_edit(request: Request, bike_id: int) -> Response:
        bike = await crm.bike(bike_id)
        if bike is None:
            return render(request, "missing.html", status_code=404, what="Велосипед")
        data = await form(request)
        fields = _bike_fields(request, data)
        if fields is None:
            return redirect(f"/bikes/{bike_id}")
        other = await crm.bike_by_code(fields["code"])
        if other is not None and other["id"] != bike_id:
            flash(request, f"Инвентарный номер {fields['code']} уже занят.", "err")
            return redirect(f"/bikes/{bike_id}")
        if fields["frame_no"]:
            other = await crm.bike_by_frame(fields["frame_no"])
            if other is not None and other["id"] != bike_id:
                flash(request, "Велосипед с таким номером рамы уже есть.", "err")
                return redirect(f"/bikes/{bike_id}")
        await crm.update_bike(bike_id, **fields)
        flash(request, "Сохранено.")
        return redirect(f"/bikes/{bike_id}")

    @app.post("/bikes/{bike_id}/status")
    async def bike_status(request: Request, bike_id: int) -> Response:
        bike = await crm.bike(bike_id)
        if bike is None:
            return render(request, "missing.html", status_code=404, what="Велосипед")
        data = await form(request)
        status = logic.check_choice(data.get("status"), logic.BIKE_MANUAL_STATUSES,
                                    what="Статус")
        note = logic.check_note(data.get("note"))
        if not status.ok or not note.ok:
            flash(request, (status.error or note.error), "err")
            return redirect(f"/bikes/{bike_id}")
        if bike.get("rental_id"):
            flash(request, "Велосипед в аренде: сначала закройте аренду.", "err")
            return redirect(f"/bikes/{bike_id}")
        await crm.update_bike(bike_id, by=who(request), status=status.value)
        await crm.add_bike_log(bike_id, "status",
                               f"{logic.BIKE_STATUSES[status.value]}"
                               + (f": {note.value}" if note.value else ""),
                               None, who(request))
        flash(request, "Статус изменён.")
        return redirect(f"/bikes/{bike_id}")

    @app.post("/bikes/{bike_id}/log")
    async def bike_log_add(request: Request, bike_id: int) -> Response:
        if await crm.bike(bike_id) is None:
            return render(request, "missing.html", status_code=404, what="Велосипед")
        data = await form(request)
        kind = logic.check_choice(data.get("kind") or "note", ("repair", "note"),
                                  what="Вид записи")
        note = logic.check_note(data.get("note"))
        cost = (logic.check_amount(data.get("cost"))
                if (data.get("cost") or "").strip() else logic.Check(True, None))
        for check in (kind, note, cost):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(f"/bikes/{bike_id}")
        if not note.value and cost.value is None:
            flash(request, "Заметка пуста.", "err")
            return redirect(f"/bikes/{bike_id}")
        await crm.add_bike_log(bike_id, kind.value, note.value, cost.value, who(request))
        flash(request, "Запись добавлена.")
        return redirect(f"/bikes/{bike_id}")

    @app.post("/bikes/{bike_id}/repair")
    async def bike_repair(request: Request, bike_id: int) -> Response:
        """Ремонт по узлу: запчасти и работа отдельно. Узел - только из
        справочника, иначе отчёт «что ломается» не соберётся."""
        if await crm.bike(bike_id) is None:
            return render(request, "missing.html", status_code=404, what="Велосипед")
        data = await form(request)
        node = logic.check_choice(data.get("node"), tuple(logic.REPAIR_NODES), what="Узел")
        note = logic.check_note(data.get("note"))
        parts = cost_field(data, "parts_cost")
        labor = cost_field(data, "labor_cost")
        for check in (node, note, parts, labor):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(f"/bikes/{bike_id}")
        await crm.create_repair(bike_id, items=[{"node": node.value, "parts_cost": parts.value,
                                                 "labor_cost": labor.value, "note": note.value}],
                                note=f"{logic.REPAIR_NODES[node.value]}"
                                     + (f": {note.value}" if note.value else ""),
                                created_by=who(request))
        flash(request, "Ремонт записан.")
        return redirect(f"/bikes/{bike_id}")

    # ─────────────────────── быстрая выдача ───────────────────────
    #
    # Мастер в четыре шага: телефон клиента -> тариф и модель -> конкретный
    # велосипед -> сводка, оплата и аренда. Состояние живёт в адресе
    # (?client=&tariff=&model=&bike=), поэтому мастер открывается и с карточки
    # клиента, и с карточки велосипеда, а «назад» - обычная ссылка.
    # Брони нет намеренно: черновики, которые никто не закрывает, держали бы
    # велосипеды «забронированными»; вместо этого свободность проверяется
    # в момент оформления, а гонку двух операторов ловит уникальный индекс.

    bot_name: dict[str, str] = {}

    async def bot_username() -> str:
        """Имя бота для ссылки-приглашения; пусто - бота нет или он недоступен."""
        if bot is None:
            return ""
        if "name" not in bot_name:
            try:
                me = await bot.get_me()
                bot_name["name"] = getattr(me, "username", "") or ""
            except Exception:                                # noqa: BLE001
                log.warning("не удалось узнать имя бота для ссылки-приглашения")
                return ""
        return bot_name["name"]

    async def bot_user_by_phone(phone: str | None) -> dict | None:
        if db is None or not phone or not hasattr(db, "user_by_phone"):
            return None
        row = await db.user_by_phone(phone)
        return dict(row) if row else None

    async def bot_user_for(client: dict) -> dict | None:
        """Строка bot.users для клиента: по Telegram, иначе по телефону."""
        row = None
        if db is not None and client.get("tg_id"):
            row = await db.get_user(client["tg_id"])
        if row is None:
            return await bot_user_by_phone(client.get("phone"))
        return dict(row)

    def issue_url(**params: Any) -> str:
        query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items()
                         if v not in (None, ""))
        return "/issue" + (f"?{query}" if query else "")

    def plain_amount(value: Decimal) -> str:
        """Сумма в поле формы: 3000, а не 3000.00."""
        text = f"{value:f}"
        return text.rstrip("0").rstrip(".") if "." in text else text

    @app.get("/issue")
    async def issue(request: Request) -> Response:
        p = request.query_params
        ctx: dict[str, Any] = {"step": 1, "client": None, "phone": p.get("phone") or "",
                               "new_client": None, "bike": None, "tariff": None,
                               "model": (p.get("model") or "").strip()}
        if (p.get("bike") or "").isdigit():
            # С карточки велосипеда: модель известна, шаг выбора пропускается.
            bike = await crm.bike(int(p["bike"]))
            if bike is not None and bike.get("status") != "available":
                flash(request, f"Велосипед {bike['code']} сейчас "
                               f"«{logic.BIKE_STATUSES.get(bike['status'], bike['status'])}» "
                               "- выберите другой.", "err")
                bike = None
            ctx["bike"] = bike
        client = None
        if (p.get("client") or "").isdigit():
            client = await crm.client(int(p["client"]))
        elif p.get("phone"):
            phone = bot_logic.normalize_phone(p["phone"])
            if phone is None:
                flash(request, "Телефон: не похоже на номер. Пример: +7 900 123-45-67.", "err")
                return render(request, "issue.html", **ctx)
            client = await crm.client_by_phone(phone)
            if client is None:
                # Новый клиент: ФИО и Telegram подсказывает бот, если человек
                # уже регистрировался там с этим номером.
                user = await bot_user_by_phone(phone)
                ctx["new_client"] = {"phone": phone,
                                     "full_name": (user or {}).get("full_name") or "",
                                     "bot": logic.bot_client_state(user)}
                ctx["phone"] = phone
                return render(request, "issue.html", **ctx)
            return redirect(issue_url(client=client["id"], bike=p.get("bike")))
        if client is None:
            return render(request, "issue.html", **ctx)

        balance = await crm.client_balance(client["id"])
        active = await crm.active_rental_of(client["id"])
        bot_user = await bot_user_for(client)
        ctx.update(step=2, client=client, balance=balance, active=active, bot_user=bot_user,
                   bot_state=logic.bot_client_state(bot_user),
                   bot_username=await bot_username())
        if active is not None or client.get("status") != "active":
            # Дальше идти некуда: сначала закрыть аренду или снять блокировку.
            return render(request, "issue.html", **ctx)
        available = await crm.bikes(status="available")
        ctx["tariffs"] = logic.tariff_tiles(await crm.tariffs(active_only=True))
        ctx["models"] = logic.model_availability(available)
        # Клиенту, который приедет завтра, можно обещать конкретный день:
        # прогноз считается по «оплачено до», а не по слову оператора.
        ctx["soon"] = logic.freeing_soon(await crm.active_rentals())
        if (p.get("tariff") or "").isdigit():
            ctx["tariff"] = await crm.tariff(int(p["tariff"]))
        if ctx["bike"] is not None:
            ctx["model"] = ctx["bike"]["model"]
        tariff = ctx["tariff"]
        if tariff is None or not ctx["model"]:
            return render(request, "issue.html", **ctx)
        if ctx["bike"] is None:
            q = (p.get("q") or "").strip()
            since = await crm.bike_status_since()
            now = datetime.now(UTC)
            rows = [dict(b) for b in available
                    if b.get("model") == ctx["model"]
                    and (not q or q.lower() in (b.get("code") or "").lower())]
            for b in rows:
                b["idle_days"] = logic.idle_days(since.get(b["id"]), now=now)
            # Дольше всех простаивающий - первым: выдать его и есть
            # снижение простоя, а не просто удобство оператора.
            rows.sort(key=lambda b: (-(b["idle_days"] or 0), b["code"]))
            ctx.update(step=3, bikes=rows, q=q)
            return render(request, "issue.html", **ctx)
        started = logic.check_date(p.get("started_on"), default=date.today())
        start = started.value if started.ok else date.today()
        # Батареи предлагаются те, что подходят модели: на двух точках
        # парк разношёрстный, и чужая батарея просто не встанет в раму.
        free = await crm.batteries(status="available", limit=500)
        fit = await crm.compat_for_bike_model(ctx["bike"]["model"])
        fit_ids = {m["id"] for m in fit}
        if fit_ids:
            free = [b for b in free if b.get("model_id") in fit_ids]
        ctx.update(batteries=free,
                   battery_slots=int(ctx["bike"].get("battery_count") or 0))
        ctx.update(step=4, started_on=start,
                   ends_on=start + timedelta(days=int(tariff["period_days"])),
                   per_day=logic.per_day(tariff["price"], tariff["period_days"]),
                   mileage=int(ctx["bike"].get("mileage_km") or 0),
                   pay_default=plain_amount(
                       logic.issue_payment_default(tariff["price"], balance)),
                   contract_no=(client.get("contract_no")
                                or (bot_user or {}).get("contract_no") or ""))
        return render(request, "issue.html", **ctx)

    @app.post("/issue/client")
    async def issue_client(request: Request) -> Response:
        data = await form(request)
        fields = await _client_fields(request, data, current=None)
        if fields is None:
            return redirect(issue_url(phone=data.get("phone"), bike=data.get("bike_id")))
        # Человек мог уже зарегистрироваться в боте с этим номером: тогда
        # карточка сразу получает его Telegram и номер договора, и
        # уведомления о сроке и оплате доходят с первого дня.
        user = await bot_user_by_phone(fields["phone"])
        tg_id = user.get("tg_id") if user else None
        if tg_id and await crm.client_by_tg(tg_id) is not None:
            tg_id = None
        client_id = await crm.create_client(
            full_name=fields["full_name"], phone=fields["phone"], note=fields["note"],
            tg_id=tg_id, username=(user or {}).get("username") if tg_id else None,
            contract_no=fields["contract_no"] or (user or {}).get("contract_no"))
        if tg_id:
            await service.ref_signed(crm, await crm.client(client_id) or {})
        flash(request, "Клиент добавлен." + (" Telegram подхвачен из бота." if tg_id else ""))
        return redirect(issue_url(client=client_id, bike=data.get("bike_id")))

    @app.post("/issue")
    async def issue_create(request: Request) -> Response:
        data = await form(request)
        back = issue_url(client=data.get("client_id"), tariff=data.get("tariff_id"),
                         bike=data.get("bike_id"))
        try:
            client = await crm.client(int(data.get("client_id") or 0))
            tariff = await crm.tariff(int(data.get("tariff_id") or 0))
            bike = await crm.bike(int(data.get("bike_id") or 0))
        except (TypeError, ValueError):
            client = tariff = bike = None
        if client is None or tariff is None or bike is None:
            flash(request, "Выберите клиента, тариф и велосипед.", "err")
            return redirect(back)
        started = logic.check_date(data.get("started_on"), default=date.today())
        pay = cost_field(data, "pay_amount")
        method = data.get("pay_method") or "sbp"
        contract = (logic.check_name(data.get("contract_no"), what="Договор")
                    if (data.get("contract_no") or "").strip() else logic.Check(True, None))
        # Пробег на выдаче обязателен: без него «накатал за аренду» не
        # посчитать никогда, а переписать число с дисплея - секунда.
        mileage = logic.check_mileage(data.get("mileage"),
                                      current=bike.get("mileage_km"))
        for check in (started, pay, contract, mileage):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(back)
        if pay.value > 0 and method not in logic.METHODS:
            flash(request, "Выберите способ оплаты.", "err")
            return redirect(back)
        # Номер договора: с формы, иначе из карточки, иначе из бота - оператор
        # его наизусть не помнит, а в акте и отчётах он нужен.
        contract_no = contract.value or client.get("contract_no")
        if not contract_no:
            contract_no = ((await bot_user_for(client)) or {}).get("contract_no")
        try:
            rental_id = await service.open_rental(
                crm, client=client, bike=bike, tariff=tariff, started_on=started.value,
                contract_no=contract_no, by=who(request), mileage=mileage.value)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(back)
        battery_ids = await form_ids(request, "battery_ids")
        if battery_ids:
            try:
                await service.issue_with_batteries(crm, rental_id, bike=bike,
                                                   battery_ids=battery_ids,
                                                   by=who(request))
            except service.ServiceError as exc:
                # Аренда уже открыта: батарею доедем отдельно, а операцию
                # не откатываем - велосипед у клиента.
                flash(request, f"{exc} Батареи не выданы, отметьте их в карточке.",
                      "err")
        if pay.value > 0:
            # Платёж после начисления первого периода: баланс сразу честный,
            # и уведомление клиенту уходит с верной датой «оплачено до».
            await service.add_entry(crm, client, kind="payment", amount=pay.value,
                                    method=method, note=f"При выдаче № {bike['code']}",
                                    by=who(request), rental_id=rental_id)
            await referral_bonus(client, pay.value, who(request))
        rental = await crm.rental(rental_id)
        await notify.rental_opened(bot, db, crm, client, rental)
        if pay.value > 0:
            flash(request, f"Выдача оформлена: № {bike['code']} у клиента, "
                           f"принято {logic.money(pay.value)}.")
        else:
            flash(request, f"Выдача оформлена без оплаты: № {bike['code']} у клиента, "
                           "первый период остался долгом на балансе.")
        return redirect(f"/rentals/{rental_id}")

    # ─────────────────────── аренды ───────────────────────

    @app.get("/rentals")
    async def rentals(request: Request) -> Response:
        status = request.query_params.get("status") or "active"
        rows = await crm.rentals(status=status if status != "all" else None)
        for r in rows:
            r["summary"] = summarize(r if r["status"] == "active" else None,
                                     r.get("balance", 0))
        return render(request, "rentals.html", rows=rows, status=status)

    @app.get("/rentals/new")
    async def rental_new(request: Request) -> Response:
        if not may_edit(request, "rentals"):
            return denied(request, "rentals")
        clients_all = await crm.clients(status="active")
        free_clients = [c for c in clients_all if not c.get("rental_id")]
        client_id = request.query_params.get("client")
        bike_id = request.query_params.get("bike")
        return render(request, "rental_form.html", clients=free_clients,
                      bikes=await crm.bikes(status="available"),
                      tariffs=await crm.tariffs(active_only=True),
                      client_id=int(client_id) if client_id and client_id.isdigit() else None,
                      bike_id=int(bike_id) if bike_id and bike_id.isdigit() else None)

    @app.post("/rentals")
    async def rental_create(request: Request) -> Response:
        data = await form(request)
        try:
            client = await crm.client(int(data.get("client_id") or 0))
            tariff = await crm.tariff(int(data.get("tariff_id") or 0))
            bike = (await crm.bike(int(data["bike_id"]))
                    if (data.get("bike_id") or "").isdigit() else None)
        except (TypeError, ValueError):
            client = tariff = bike = None
        started = logic.check_date(data.get("started_on"), default=date.today())
        billing = data.get("billing") or "auto"
        if client is None or tariff is None:
            flash(request, "Выберите клиента и тариф.", "err")
            return redirect("/rentals/new")
        if bike is None and (data.get("bike_id") or "").strip():
            flash(request, "Такого велосипеда нет.", "err")
            return redirect("/rentals/new")
        if not started.ok or billing not in logic.BILLING:
            flash(request, started.error or "Недопустимый режим начисления.", "err")
            return redirect("/rentals/new")
        contract_no = (data.get("contract_no") or "").strip() or client.get("contract_no")
        try:
            rental_id = await service.open_rental(
                crm, client=client, bike=bike, tariff=tariff, started_on=started.value,
                contract_no=contract_no, by=who(request), billing=billing)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/rentals/new")
        rental = await crm.rental(rental_id)
        await notify.rental_opened(bot, db, crm, client, rental)
        if billing == "manual":
            flash(request, "Аренда оформлена без начисления: записи в журнал делаете вы.")
        elif started.value > date.today():
            flash(request, f"Аренда оформлена. Первый период начислится "
                           f"{started.value:%d.%m.%Y}.")
        else:
            flash(request, "Аренда оформлена, первый период начислен.")
        return redirect(f"/rentals/{rental_id}")

    @app.get("/rentals/search")
    async def rentals_search(request: Request) -> Response:
        """Розыск: кто перестал платить и пропал.

        Потеря велосипеда начинается одинаково - клиент замолчал, а
        велосипед остался «в аренде», и никто его не ищет.
        """
        settings = logic.search_settings(await crm.settings())
        rows = logic.search_rows(await crm.active_rentals(), settings=settings)
        return render(request, "search.html", settings=settings, **rows)

    @app.post("/rentals/search")
    async def rentals_search_settings(request: Request) -> Response:
        if not may_edit(request, "rentals"):
            return denied(request, "rentals")
        data = await form(request)
        after = count_field(data, "search_after_days", what="Срок до розыска",
                            default=str(logic.SEARCH_AFTER_DAYS), limit=365)
        theft = count_field(data, "theft_after_days", what="Срок до признания потери",
                            default=str(logic.THEFT_AFTER_DAYS), limit=365)
        for check in (after, theft):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/rentals/search")
        await crm.set_setting("search_after_days", str(after.value), by=who(request))
        await crm.set_setting("theft_after_days", str(theft.value), by=who(request))
        flash(request, "Правило розыска сохранено.")
        return redirect("/rentals/search")

    @app.post("/rentals/{rental_id}/search")
    async def rental_search(request: Request, rental_id: int) -> Response:
        if not may_edit(request, "rentals"):
            return denied(request, "rentals")
        rental = await crm.rental(rental_id)
        if rental is None:
            return render(request, "missing.html", status_code=404, what="Аренда")
        data = await form(request)
        note = logic.check_note(data.get("note"))
        if not note.ok:
            flash(request, note.error, "err")
            return redirect(f"/rentals/{rental_id}")
        action = data.get("action") or "start"
        try:
            if action == "stop":
                await service.stop_search(crm, rental, by=who(request))
                flash(request, "Розыск снят.")
            elif action == "theft":
                await service.declare_theft(crm, rental, note=note.value,
                                            by=who(request))
                flash(request, "Велосипед признан потерянным, аренда закрыта. "
                               "Долг клиента остался в журнале.")
            else:
                await service.start_search(crm, rental, note=note.value,
                                           by=who(request))
                flash(request, "Аренда в розыске.")
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
        return redirect(f"/rentals/{rental_id}")

    @app.get("/rentals/{rental_id}")
    async def rental_card(request: Request, rental_id: int) -> Response:
        rental = await crm.rental(rental_id)
        if rental is None:
            return render(request, "missing.html", status_code=404, what="Аренда")
        ledger = [x for x in await crm.ledger_of(rental["client_id"], 200)
                  if x.get("rental_id") == rental_id]
        summary = summarize(rental if rental["status"] == "active" else None,
                            rental.get("balance", 0))
        bike = await crm.bike(rental["bike_id"]) if rental.get("bike_id") else None
        moves = logic.rental_bike_rows(await crm.rental_bikes(rental_id))
        return render(request, "rental.html", rental=rental, summary=summary, bike=bike,
                      intent=logic.intent_state(rental, summary, today=date.today()),
                      ledger=ledger, tariffs=await crm.tariffs(active_only=True),
                      moves=moves,
                      total_km=logic.rental_mileage(
                          moves, current=(bike or {}).get("mileage_km")),
                      swap_bikes=logic.swap_candidates(
                          await crm.bikes(status="available", limit=10000),
                          current_id=rental.get("bike_id")),
                      batteries=logic.battery_rows(
                          await crm.batteries(rental_id=rental_id)),
                      free_batteries=await crm.batteries(status="available",
                                                         limit=500))

    @app.post("/rentals/{rental_id}/intent")
    async def rental_intent(request: Request, rental_id: int) -> Response:
        """Что клиент сказал про истекающий срок: продлит, сдаёт, или
        отложить строку до завтра. Хранится с датой «оплачено до» на момент
        отметки, поэтому после оплаты устаревает само."""
        data = await form(request)
        nxt = data.get("next") or ""
        back = nxt if nxt.startswith("/") and not nxt.startswith("//") else f"/rentals/{rental_id}"
        rental = await crm.rental(rental_id)
        if rental is None or rental["status"] != "active":
            flash(request, "Аренда не идёт - отмечать нечего.", "err")
            return redirect(back)
        action = data.get("intent") or ""
        name = rental["full_name"]
        if action in logic.INTENTS:
            summary = summarize(rental, rental.get("balance", 0))
            await crm.update_rental(rental_id, intent=action,
                                    intent_until=summary["covered_until"],
                                    intent_by=who(request), intent_at=datetime.now(UTC),
                                    snooze_until=None)
            flash(request, f"{name}: {logic.INTENTS[action]}.")
        elif action == "snooze":
            await crm.update_rental(rental_id, snooze_until=date.today() + timedelta(days=1))
            flash(request, f"{name}: отложено до завтра.")
        elif action == "clear":
            await crm.update_rental(rental_id, intent=None, intent_until=None, intent_by=None,
                                    intent_at=None, snooze_until=None)
            flash(request, f"{name}: отметка снята.")
        else:
            flash(request, "Неизвестное действие.", "err")
        return redirect(back)

    @app.post("/rentals/{rental_id}/swap")
    async def rental_swap(request: Request, rental_id: int) -> Response:
        """Заменить велосипед, не трогая деньги и сроки аренды."""
        if not may_edit(request, "rentals"):
            return denied(request, "rentals")
        rental = await crm.rental(rental_id)
        if rental is None:
            return render(request, "missing.html", status_code=404, what="Аренда")
        data = await form(request)
        reason = logic.check_swap_reason(data.get("reason") or "repair")
        new_bike = await crm.bike(int(data["bike_id"])) \
            if (data.get("bike_id") or "").isdigit() else None
        old_bike = await crm.bike(rental["bike_id"]) if rental.get("bike_id") else None
        mileage_old = logic.check_mileage(
            data.get("mileage_old"), current=(old_bike or {}).get("mileage_km"),
            required=False)
        # Тот же велосипед - случай отдельный: сверять его одометр «с самим
        # собой» бессмысленно, и оператор получил бы разговор про пробег
        # вместо понятного «это тот же велосипед».
        same = bool(old_bike and new_bike and int(old_bike["id"]) == int(new_bike["id"]))
        mileage_new = logic.check_mileage(
            data.get("mileage_new"),
            current=None if same else (new_bike or {}).get("mileage_km"),
            required=False)
        for check in (reason, mileage_old, mileage_new):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(f"/rentals/{rental_id}")
        if new_bike is None:
            flash(request, "Выберите велосипед на замену.", "err")
            return redirect(f"/rentals/{rental_id}")
        try:
            await service.swap_bike(
                crm, rental, new_bike, reason=reason.value,
                mileage_old=mileage_old.value, mileage_new=mileage_new.value,
                old_status=data.get("old_status") or None, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/rentals/{rental_id}")
        flash(request, f"Велосипед заменён на № {new_bike['code']}. "
                       "Деньги и сроки аренды не изменились.")
        return redirect(f"/rentals/{rental_id}")

    @app.post("/rentals/{rental_id}/close")
    async def rental_close(request: Request, rental_id: int) -> Response:
        rental = await crm.rental(rental_id)
        if rental is None:
            return render(request, "missing.html", status_code=404, what="Аренда")
        data = await form(request)
        note = logic.check_note(data.get("note"))
        closed = logic.check_date(data.get("closed_on"), default=date.today())
        # Пробег возврата необязателен: велосипед могли принять без дисплея
        # (разряжен, разбит). Тогда «накатал» у этой аренды останется пустым.
        bike = await crm.bike(rental["bike_id"]) if rental.get("bike_id") else None
        floor = (bike or {}).get("mileage_km", rental.get("mileage_start"))
        mileage = logic.check_mileage(data.get("mileage"), current=floor, required=False)
        for check in (note, closed, mileage):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(f"/rentals/{rental_id}")
        try:
            await service.close_rental(crm, rental, closed_on=closed.value, note=note.value,
                                       bike_status=data.get("bike_status") or "available",
                                       by=who(request), mileage=mileage.value)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/rentals/{rental_id}")
        client = await crm.client(rental["client_id"])
        await notify.rental_closed(bot, db, crm, client, rental)
        km = logic.ridden({**rental, "mileage_end": mileage.value})
        flash(request, "Аренда закрыта, велосипед освобождён."
              + (f" Накатал {km} км." if km is not None else ""))
        return redirect(f"/rentals/{rental_id}")

    @app.post("/rentals/{rental_id}/tariff")
    async def rental_tariff(request: Request, rental_id: int) -> Response:
        rental = await crm.rental(rental_id)
        if rental is None:
            return render(request, "missing.html", status_code=404, what="Аренда")
        data = await form(request)
        tariff = await crm.tariff(int(data["tariff_id"])) \
            if (data.get("tariff_id") or "").isdigit() else None
        if tariff is None or rental["status"] != "active":
            flash(request, "Выберите тариф; менять можно только у идущей аренды.", "err")
            return redirect(f"/rentals/{rental_id}")
        try:
            await service.change_tariff(crm, rental, tariff,
                                        billing=data.get("billing") or rental["billing"])
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/rentals/{rental_id}")
        flash(request, "Тариф изменён со следующего периода.")
        return redirect(f"/rentals/{rental_id}")

    # ─────────────────────── тарифы ───────────────────────

    @app.get("/tariffs")
    async def tariffs(request: Request) -> Response:
        return render(request, "tariffs.html", rows=await crm.tariffs())

    def _tariff_fields(request: Request, data: dict) -> dict | None:
        name = logic.check_name(data.get("name"), what="Название")
        period = logic.check_period(data.get("period_days"))
        price = logic.check_amount(data.get("price"))
        note = logic.check_note(data.get("note"))
        for check in (name, period, price, note):
            if not check.ok:
                flash(request, check.error, "err")
                return None
        return {"name": name.value, "period_days": period.value, "price": price.value,
                "note": note.value}

    @app.post("/tariffs")
    async def tariff_create(request: Request) -> Response:
        fields = _tariff_fields(request, await form(request))
        if fields is not None:
            await crm.create_tariff(**fields)
            flash(request, "Тариф добавлен.")
        return redirect("/tariffs")

    @app.post("/tariffs/{tariff_id}")
    async def tariff_edit(request: Request, tariff_id: int) -> Response:
        if await crm.tariff(tariff_id) is None:
            return render(request, "missing.html", status_code=404, what="Тариф")
        data = await form(request)
        if data.get("action") == "toggle":
            tariff = await crm.tariff(tariff_id)
            await crm.update_tariff(tariff_id, active=not tariff["active"])
            flash(request, "Тариф " + ("включён." if not tariff["active"] else "выключен."))
            return redirect("/tariffs")
        fields = _tariff_fields(request, data)
        if fields is not None:
            await crm.update_tariff(tariff_id, **fields)
            flash(request, "Тариф сохранён.")
        return redirect("/tariffs")

    # ─────────────────────── финансы и заявки ───────────────────────

    @app.get("/finance")
    async def finance(request: Request) -> Response:
        since = logic.check_date(request.query_params.get("since"),
                                 default=date.today().replace(day=1))
        until = logic.check_date(request.query_params.get("until"), default=date.today())
        kind = request.query_params.get("kind") or ""
        if not since.ok or not until.ok:
            flash(request, "Дата: в виде ДД.ММ.ГГГГ.", "err")
            return redirect("/finance")
        rows = await crm.ledger(since=since.value, until=until.value,
                                kind=kind or None, limit=1000)
        totals = await crm.ledger_totals(since=since.value, until=until.value)
        return render(request, "finance.html", rows=rows, totals=totals,
                      since=since.value, until=until.value, kind=kind)

    @app.get("/finance.csv")
    async def finance_csv(request: Request) -> Response:
        since = logic.check_date(request.query_params.get("since"),
                                 default=date.today().replace(day=1))
        until = logic.check_date(request.query_params.get("until"), default=date.today())
        kind = request.query_params.get("kind") or ""
        if not since.ok or not until.ok:
            return redirect("/finance")
        rows = [[x["created_at"], x["full_name"], logic.KINDS.get(x["kind"], x["kind"]),
                 logic.to_money(x["amount"]),
                 logic.period_label(x.get("period_from"), x.get("period_to")),
                 logic.METHODS.get(x.get("method"), x.get("method") or ""),
                 x.get("note"), x.get("created_by")]
                for x in await crm.ledger(since=since.value, until=until.value,
                                          kind=kind or None, limit=100000)]
        name = f"finance-{since.value:%Y%m%d}-{until.value:%Y%m%d}.csv"
        return _csv(name, ["Дата", "Клиент", "Вид", "Сумма", "Период", "Способ",
                           "Заметка", "Кто"], rows)

    @app.get("/claims")
    async def claims(request: Request) -> Response:
        rows = await crm.pending_claims()
        for r in rows:
            r["balance"] = await crm.client_balance(r["client_id"])
        return render(request, "claims.html", rows=rows)

    @app.post("/claims/{claim_id}/confirm")
    async def claim_confirm(request: Request, claim_id: int) -> Response:
        claim = await crm.claim(claim_id)
        if claim is None:
            return render(request, "missing.html", status_code=404, what="Заявка")
        data = await form(request)
        amount = logic.check_amount(data.get("amount") or claim.get("amount_hint"))
        method = data.get("method") or "sbp"
        if not amount.ok or not logic.check_choice(method, logic.METHODS).ok:
            flash(request, amount.error or "Способ оплаты: недопустимое значение.", "err")
            return redirect("/claims")
        ledger_id = await service.credit_claim(crm, claim, amount.value, by=who(request),
                                               method=method)
        if ledger_id is None:
            flash(request, "Заявку уже обработали.", "err")
            return redirect("/claims")
        client = await crm.client(claim["client_id"])
        await notify.payment_credited(bot, db, crm, client, amount.value)
        await referral_bonus(client, amount.value, who(request))
        flash(request, f"Зачислено {logic.money(amount.value)} клиенту {client['full_name']}.")
        return redirect("/claims")

    @app.post("/claims/{claim_id}/reject")
    async def claim_reject(request: Request, claim_id: int) -> Response:
        claim = await crm.claim(claim_id)
        if claim is None:
            return render(request, "missing.html", status_code=404, what="Заявка")
        if not await service.reject_claim(crm, claim, by=who(request)):
            flash(request, "Заявку уже обработали.", "err")
            return redirect("/claims")
        await notify.payment_rejected(bot, db, claim)
        flash(request, "Заявка отклонена.")
        return redirect("/claims")

    # ─────────────────────── отчёты ───────────────────────

    @app.get("/reports")
    async def reports(request: Request) -> Response:
        bikes_by = await crm.bike_counts()
        fleet_rows = await crm.bikes(limit=10000)
        fleet = sum(bikes_by.get(s, 0) for s in logic.OPERATIONAL_STATUSES)
        rented = bikes_by.get("rented", 0)
        # Три числа по месяцам: текущий и пять прошлых.
        now = datetime.now().astimezone()
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        months_metrics = []
        for _ in range(6):
            nxt = (first + timedelta(days=32)).replace(day=1)
            m = await period_metrics(since=first, until=min(nxt, now))
            months_metrics.append({"month": first.date(), **m})
            first = (first - timedelta(days=1)).replace(day=1)
        # Ровно 12 календарных месяцев, включая текущий: тем же шагом,
        # что и таблица выше, а не «минус 335 дней».
        since_year = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for _ in range(11):
            since_year = (since_year - timedelta(days=1)).replace(day=1)
        return render(request, "reports.html", months=await crm.revenue_by_month(12),
                      bikes=bikes_by, fleet=fleet, rented=rented,
                      utilization=(round(100 * rented / fleet) if fleet else 0),
                      months_metrics=months_metrics,
                      amortization=logic.amortization_total(
                          fleet_rows, await crm.batteries(limit=10000)),
                      priced=sum(1 for b in fleet_rows
                                 if b.get("status") in logic.OPERATIONAL_STATUSES
                                 and b.get("purchase_price") is not None),
                      repairs=await crm.repair_stats(since_year, now),
                      debtors=await crm.debtors(50))

    async def payback_data(request: Request) -> dict:
        """Окупаемость по моделям за период. Период - как в финансах:
        с начала месяца по сегодня, если не задан другой."""
        since = logic.check_date(request.query_params.get("since"),
                                 default=date.today().replace(day=1))
        until = logic.check_date(request.query_params.get("until"),
                                 default=date.today())
        if not since.ok or not until.ok:
            since = logic.Check(True, date.today().replace(day=1))
            until = logic.Check(True, date.today())
        tz = datetime.now().astimezone().tzinfo
        start = datetime.combine(since.value, datetime.min.time(), tzinfo=tz)
        # Верхняя граница включительно по дате: отчёт «по сегодня» обязан
        # содержать сегодняшние платежи.
        end = datetime.combine(until.value + timedelta(days=1),
                               datetime.min.time(), tzinfo=tz)
        money = await crm.model_money(start, end)
        rows = logic.payback_rows(await crm.bikes(limit=10000), money,
                                  days=(until.value - since.value).days + 1)
        return {"rows": rows, "total": logic.payback_total(rows),
                "since": since.value, "until": until.value}

    @app.get("/reports/payback")
    async def payback_report(request: Request) -> Response:
        if not may_view(request, "finance"):
            return denied(request, "finance")
        return render(request, "payback.html", **await payback_data(request))

    @app.get("/reports/payback.csv")
    async def payback_csv(request: Request) -> Response:
        if not may_view(request, "finance"):
            return denied(request, "finance")
        data = await payback_data(request)
        rows = [[r["model"], r["bikes"], round(float(r["rented_days"]), 1),
                 r["check_per_day"], r["paid"], r["charged"], r["repair_cost"],
                 r["works"], r["amortization"], r["margin"], r["margin_percent"]]
                for r in data["rows"]]
        total = data["total"]
        rows.append(["ИТОГО", total["bikes"], round(float(total["rented_days"]), 1),
                     total["check_per_day"], total["paid"], total["charged"],
                     total["repair_cost"], total["works"], total["amortization"],
                     total["margin"], total["margin_percent"]])
        name = f"payback-{data['since']:%Y%m%d}-{data['until']:%Y%m%d}.csv"
        return _csv(name, ["Модель", "Великов", "Дней в аренде", "Чек/день",
                           "Оплачено", "Начислено", "Ремонт", "Работы клиентам",
                           "Амортизация", "Маржа", "Маржа %"], rows)

    async def integrity_data() -> list[dict]:
        """Расхождения между парком, арендами и нарядами."""
        return logic.integrity_issues(
            await crm.bikes(limit=10000), await crm.active_rentals(),
            await crm.open_orders_by_bike(), await crm.debtors(200))

    @app.get("/reports/integrity")
    async def integrity_report(request: Request) -> Response:
        """Расхождение - это не «некрасиво в базе», а невидимый простой."""
        if not may_view(request, "bikes"):
            return denied(request, "bikes")
        issues = await integrity_data()
        return render(request, "integrity.html", issues=issues,
                      summary=logic.integrity_summary(issues))

    @app.get("/reports/channels")
    async def channels_report(request: Request) -> Response:
        """Откуда приходят клиенты - по месяцам. Куда давать рекламу.

        Это разрез клиентской базы, поэтому и право нужно на клиентов:
        механику с доступом к отчётам парка она ни к чему.
        """
        if not may_view(request, "clients"):
            return denied(request, "clients")
        months = 12
        now = datetime.now().astimezone()
        since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for _ in range(months - 1):
            since = (since - timedelta(days=1)).replace(day=1)
        data = logic.channel_rows(await crm.clients_since(since), months=months,
                                  today=now.date())
        return render(request, "channels.html", **data)

    @app.get("/reports/channels.csv")
    async def channels_csv(request: Request) -> Response:
        if not may_view(request, "clients"):
            return denied(request, "clients")
        now = datetime.now().astimezone()
        since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for _ in range(11):
            since = (since - timedelta(days=1)).replace(day=1)
        data = logic.channel_rows(await crm.clients_since(since), months=12,
                                  today=now.date())
        header = ["Месяц", *(logic.channel_label(c) for c in data["columns"]), "Всего"]
        rows = [[r["month"].strftime("%m.%Y"),
                 *(r["cells"][c] for c in data["columns"]), r["total"]]
                for r in data["rows"]]
        rows.append(["ИТОГО", *(data["totals"].get(c, 0) for c in data["columns"]),
                     data["total"]])
        return _csv("channels.csv", header, rows)

    @app.get("/reports/referrals")
    async def referrals_report(request: Request) -> Response:
        if not may_view(request, "finance"):
            return denied(request, "finance")
        since = logic.check_date(request.query_params.get("since"),
                                 default=date.today().replace(day=1))
        until = logic.check_date(request.query_params.get("until"),
                                 default=date.today())
        if not since.ok or not until.ok:
            since = logic.Check(True, date.today().replace(day=1))
            until = logic.Check(True, date.today())
        tz = datetime.now().astimezone().tzinfo
        start = datetime.combine(since.value, datetime.min.time(), tzinfo=tz)
        end = datetime.combine(until.value + timedelta(days=1),
                               datetime.min.time(), tzinfo=tz)
        rows = await crm.referrals(since=start, until=end, limit=5000)
        return render(request, "referrals.html", rows=rows,
                      funnel=logic.ref_funnel(rows), agents=logic.ref_agents(rows),
                      settings=logic.ref_settings(await crm.settings()),
                      free_bikes=str((await crm.settings()).get("free_bikes_post", "0"))
                      not in ("0", "", "false"),
                      since=since.value, until=until.value)

    @app.post("/reports/referrals")
    async def referrals_settings(request: Request) -> Response:
        """Настройки программы: включена ли, бонус и порог платежа."""
        if not may_edit(request, "finance"):
            return denied(request, "finance")
        data = await form(request)
        bonus = cost_field(data, "bonus")
        minimum = cost_field(data, "min_payment")
        for check in (bonus, minimum):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/reports/referrals")
        await crm.set_setting("ref_enabled", "1" if data.get("enabled") else "0",
                              by=who(request))
        await crm.set_setting("ref_bonus", str(bonus.value), by=who(request))
        await crm.set_setting("ref_min_payment", str(minimum.value), by=who(request))
        await crm.set_setting("free_bikes_post", "1" if data.get("free_bikes") else "0",
                              by=who(request))
        flash(request, "Настройки программы сохранены.")
        return redirect("/reports/referrals")

    # ─────────────────────── сотрудники ───────────────────────

    async def profile_choices() -> list[dict]:
        return await crm.access_profiles()

    def role_for(profile: dict | None) -> str:
        """Старая колонка role остаётся: журнал и импорт её пишут. Права
        решает профиль, role лишь повторяет его крупным планом."""
        return "admin" if (profile or {}).get("code") == "owner" else "manager"

    @app.get("/staff")
    async def staff_page(request: Request) -> Response:
        return render(request, "staff.html", rows=await crm.staff_all(),
                      profiles=await profile_choices(),
                      can_manage=may_edit(request, "staff"))

    @app.post("/staff")
    async def staff_create(request: Request) -> Response:
        data = await form(request)
        login_check = logic.check_login(data.get("login"))
        password = logic.check_password(data.get("password"))
        name = logic.check_name(data.get("name") or data.get("login"), what="Имя")
        for check in (login_check, password, name):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/staff")
        profile = await crm.access_profile(int(data.get("profile_id") or 0)) \
            if (data.get("profile_id") or "").isdigit() else None
        if profile is None:
            flash(request, "Выберите профиль доступа.", "err")
            return redirect("/staff")
        if await crm.staff_by_login(login_check.value) is not None:
            flash(request, "Такой логин уже есть.", "err")
            return redirect("/staff")
        await crm.create_staff(login_check.value, logic.hash_password(password.value),
                               name.value, role_for(profile), profile["id"])
        flash(request, f"Сотрудник {login_check.value} добавлен — профиль "
                       f"«{profile['name']}».")
        return redirect("/staff")

    @app.post("/staff/{staff_id}/profile")
    async def staff_set_profile(request: Request, staff_id: int) -> Response:
        data = await form(request)
        target = await crm.staff_by_id(staff_id)
        if target is None:
            return render(request, "missing.html", status_code=404, what="Сотрудник")
        if target["id"] == request.state.staff["id"]:
            # Иначе владелец одним движением снимает с себя доступ к этой же
            # странице и чинить это придётся руками в базе.
            flash(request, "Свой профиль менять нельзя — попросите другого "
                           "сотрудника с доступом к разделу.", "err")
            return redirect("/staff")
        profile = await crm.access_profile(int(data.get("profile_id") or 0)) \
            if (data.get("profile_id") or "").isdigit() else None
        if profile is None:
            flash(request, "Такого профиля нет.", "err")
            return redirect("/staff")
        await crm.set_staff_profile(staff_id, profile["id"])
        flash(request, f"{target['login']}: профиль «{profile['name']}».")
        return redirect("/staff")

    @app.post("/staff/{staff_id}/telegram")
    async def staff_telegram(request: Request, staff_id: int) -> Response:
        """Код привязки Telegram сотруднику - или отвязка.

        Пароль от панели в переписку не отдают, а одноразовый код можно:
        он гаснет при первом применении и открывает ровно одну связь.
        """
        if not may_edit(request, "staff"):
            return denied(request, "staff")
        person = await crm.staff_by_id(staff_id)
        if person is None:
            return render(request, "missing.html", status_code=404, what="Сотрудник")
        if (await form(request)).get("unlink"):
            await crm.unlink_staff_tg(staff_id)
            flash(request, f"Telegram сотрудника {person['login']} отвязан.")
            return redirect("/staff")
        for _ in range(10):
            code = logic.make_link_code()
            if await crm.staff_by_link_code(code) is not None:
                continue
            if await crm.set_staff_link_code(staff_id, code):
                flash(request, f"Код для {person['login']}: {code}. Пусть отправит "
                               f"боту «/staff {code}» — код погаснет сразу после этого.")
                return redirect("/staff")
        flash(request, "Не удалось выдать код, попробуйте ещё раз.", "err")
        return redirect("/staff")

    @app.post("/staff/{staff_id}/password")
    async def staff_password(request: Request, staff_id: int) -> Response:
        data = await form(request)
        password = logic.check_password(data.get("password"))
        if not password.ok:
            flash(request, password.error, "err")
            return redirect("/staff")
        if await crm.staff_by_id(staff_id) is None:
            return render(request, "missing.html", status_code=404, what="Сотрудник")
        await crm.set_staff_password(staff_id, logic.hash_password(password.value))
        flash(request, "Пароль обновлён.")
        return redirect("/staff")

    @app.post("/staff/{staff_id}/toggle")
    async def staff_toggle(request: Request, staff_id: int) -> Response:
        target = await crm.staff_by_id(staff_id)
        if target is None:
            return render(request, "missing.html", status_code=404, what="Сотрудник")
        if target["id"] == request.state.staff["id"]:
            flash(request, "Себя отключить нельзя.", "err")
            return redirect("/staff")
        await crm.set_staff_active(staff_id, not target["active"])
        flash(request, "Доступ " + ("включён." if not target["active"] else "отключён."))
        return redirect("/staff")

    # ─────────────────────── профили доступа ───────────────────────

    def name_taken(exc: Exception) -> bool:
        """Уникальный индекс на название профиля - единственная ошибка,
        которую здесь можно объяснить оператору; всё прочее наверх."""
        return "unique" in type(exc).__name__.lower()

    def perms_from_form(data: Any) -> dict:
        """Матрица из формы: по полю на раздел, галочки на действия."""
        return logic.normalize_perms({
            "sections": {code: (data.get(f"s_{code}") or "") for code in logic.SECTIONS},
            "actions": {code: bool(data.get(f"a_{code}")) for code in logic.ACTIONS},
        })

    @app.get("/profiles")
    async def profiles_page(request: Request) -> Response:
        return render(request, "profiles.html", rows=await crm.access_profiles(),
                      can_manage=may_edit(request, "staff"))

    @app.get("/profiles/{profile_id}")
    async def profile_page(request: Request, profile_id: int) -> Response:
        profile = await crm.access_profile(profile_id)
        if profile is None:
            return render(request, "missing.html", status_code=404, what="Профиль")
        staff_on_it = [s for s in await crm.staff_all()
                       if s.get("profile_id") == profile_id]
        return render(request, "profile.html", profile=profile,
                      perms=logic.normalize_perms(profile.get("perms")),
                      staff_on_it=staff_on_it, can_manage=may_edit(request, "staff"))

    @app.post("/profiles")
    async def profile_create(request: Request) -> Response:
        data = await form(request)
        name = logic.check_profile_name(data.get("name"))
        if not name.ok:
            flash(request, name.error, "err")
            return redirect("/profiles")
        try:
            profile_id = await crm.create_access_profile(name.value, perms_from_form(data))
        except Exception as exc:                           # noqa: BLE001
            if not name_taken(exc):
                raise
            flash(request, "Профиль с таким названием уже есть.", "err")
            return redirect("/profiles")
        flash(request, f"Профиль «{name.value}» создан — отметьте разделы.")
        return redirect(f"/profiles/{profile_id}")

    @app.post("/profiles/{profile_id}")
    async def profile_save(request: Request, profile_id: int) -> Response:
        data = await form(request)
        profile = await crm.access_profile(profile_id)
        if profile is None:
            return render(request, "missing.html", status_code=404, what="Профиль")
        if profile["built_in"]:
            flash(request, "Профиль «Владелец» не меняется: это запасной ключ "
                           "от панели.", "err")
            return redirect(f"/profiles/{profile_id}")
        name = logic.check_profile_name(data.get("name"))
        if not name.ok:
            flash(request, name.error, "err")
            return redirect(f"/profiles/{profile_id}")
        perms = perms_from_form(data)
        me = request.state.staff
        if me.get("profile_id") == profile_id and perms["sections"].get("staff") != "edit":
            flash(request, "Это ваш профиль: доступ к разделу «Сотрудники» "
                           "снимать нельзя — некому будет его вернуть.", "err")
            return redirect(f"/profiles/{profile_id}")
        try:
            await crm.update_access_profile(profile_id, name=name.value, perms=perms)
        except Exception as exc:                           # noqa: BLE001
            if not name_taken(exc):
                raise
            flash(request, "Профиль с таким названием уже есть.", "err")
            return redirect(f"/profiles/{profile_id}")
        # Сотрудники подхватят новые права со следующего запроса: права
        # читаются из базы на каждом, а не кладутся в сессию при входе.
        flash(request, "Права сохранены.")
        return redirect(f"/profiles/{profile_id}")

    @app.post("/profiles/{profile_id}/delete")
    async def profile_delete(request: Request, profile_id: int) -> Response:
        profile = await crm.access_profile(profile_id)
        if profile is None:
            return render(request, "missing.html", status_code=404, what="Профиль")
        if not await crm.delete_access_profile(profile_id):
            flash(request, "Профиль встроенный или на нём ещё есть сотрудники — "
                           "сначала переведите их.", "err")
            return redirect(f"/profiles/{profile_id}")
        flash(request, f"Профиль «{profile['name']}» удалён.")
        return redirect("/profiles")

    # ─────────────────────── сервис: наряды ───────────────────────

    async def notify_tech(order_id: int, tech_id: int) -> None:
        """Сказать технику о наряде в Telegram. Не привязан - промолчать:
        наряд от этого не перестаёт существовать."""
        tech = await crm.staff_by_id(tech_id)
        if not tech or not tech.get("tg_id"):
            return
        order = await crm.work_order(order_id)
        if order:
            await notify.order_assigned(bot, order, tech)

    @app.get("/service")
    async def service_desk(request: Request) -> Response:
        """Рабочий стол сервиса: что стоит в ремонте и кто этим занят.

        Первыми - велосипеды в ремонте без наряда: они копят простой, а
        в отчётах выглядят как обычный ремонт, которым кто-то занимается.
        """
        bikes = await crm.bikes(limit=10000)
        since = await crm.bike_status_since()
        today, now = date.today(), datetime.now(UTC)
        for bike in bikes:
            bike["idle_days"] = logic.idle_days(since.get(bike["id"]), now=now)
        rows = logic.service_rows(bikes, await crm.open_orders_by_bike(), today=today)
        return render(request, "service.html", rows=rows,
                      summary=logic.service_summary(rows),
                      orders=await crm.work_orders(open_only=True, limit=200))

    @app.get("/orders")
    async def orders_page(request: Request) -> Response:
        status = request.query_params.get("status") or ""
        payer = request.query_params.get("payer") or ""
        rows = await crm.work_orders(status=status or None, payer=payer or None,
                                     limit=300)
        for order in rows:
            order["days"] = logic.order_days(order, today=date.today())
        return render(request, "orders.html", rows=rows, status=status, payer=payer)

    @app.get("/orders/new")
    async def order_new(request: Request) -> Response:
        if not may_edit(request, "service"):
            return denied(request, "service")
        bike_id = request.query_params.get("bike")
        bike = await crm.bike(int(bike_id)) if (bike_id or "").isdigit() else None
        return render(request, "order_form.html", bike=bike,
                      bikes=await crm.bikes(limit=10000),
                      techs=await crm.staff_all())

    @app.post("/orders")
    async def order_create(request: Request) -> Response:
        data = await form(request)
        payer = logic.check_payer(data.get("payer") or "own")
        if not payer.ok:
            flash(request, payer.error, "err")
            return redirect("/orders/new")
        bike = await crm.bike(int(data["bike_id"])) \
            if (data.get("bike_id") or "").isdigit() else None
        client = await crm.client(int(data["client_id"])) \
            if (data.get("client_id") or "").isdigit() else None
        estimate = cost_field(data, "estimate")
        complaint = logic.check_note(data.get("complaint"))
        for check in (estimate, complaint):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/orders/new")
        tech_id = int(data["tech_id"]) if (data.get("tech_id") or "").isdigit() else None
        try:
            order_id = await service.open_order(
                crm, bike=bike, payer=payer.value, client=client,
                complaint=complaint.value,
                object_note=(data.get("object_note") or "").strip() or None,
                tech_id=tech_id, estimate=estimate.value, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/orders/new")
        if tech_id:
            await notify_tech(order_id, tech_id)
        flash(request, "Наряд открыт.")
        return redirect(f"/orders/{order_id}")

    @app.get("/orders/{order_id}")
    async def order_page(request: Request, order_id: int) -> Response:
        order = await crm.work_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Наряд")
        items = await crm.order_items(order_id)
        stocks = await crm.stock_map()
        return render(request, "order.html", order=order, items=items,
                      totals=logic.order_totals(items),
                      days=logic.order_days(order, today=date.today()),
                      types=await crm.work_types(active_only=True),
                      techs=await crm.staff_all(),
                      parts=logic.part_rows(await crm.parts(active_only=True), stocks),
                      may_stock=may_view(request, "inventory"))

    @app.post("/orders/{order_id}/items")
    async def order_add_item(request: Request, order_id: int) -> Response:
        order = await crm.work_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Наряд")
        if not logic.order_is_open(order):
            flash(request, "Наряд закрыт - строки больше не добавляются.", "err")
            return redirect(f"/orders/{order_id}")
        data = await form(request)
        work_type = await crm.work_type(int(data["work_type_id"])) \
            if (data.get("work_type_id") or "").isdigit() else None
        title = logic.check_name(data.get("title") or (work_type or {}).get("title"),
                                 what="Работа")
        qty = count_field(data, "qty", what="Количество", limit=99)
        price = cost_field(data, "price")
        parts = cost_field(data, "parts_cost")
        labor = cost_field(data, "labor_cost")
        for check in (title, qty, price, parts, labor):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(f"/orders/{order_id}")
        node = (data.get("node") or (work_type or {}).get("node") or "").strip() or None
        if node and not logic.check_choice(node, logic.REPAIR_NODES).ok:
            node = None
        await crm.add_order_item(
            order_id, title=title.value, node=node,
            work_type_id=(work_type or {}).get("id"), qty=qty.value,
            price=price.value, parts_cost=parts.value, labor_cost=labor.value)
        flash(request, "Строка добавлена.")
        return redirect(f"/orders/{order_id}")

    @app.post("/orders/{order_id}/parts")
    async def order_take_part(request: Request, order_id: int) -> Response:
        """Списать запчасть со склада в наряд.

        Себестоимость строки берётся со склада, а не с потолка: до склада
        механик писал её руками, и отчёт по ремонту ничего не значил.
        """
        order = await crm.work_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Наряд")
        if not may_edit(request, "service"):
            return denied(request, "service")
        data = await form(request)
        part = await crm.part(int(data["part_id"])) \
            if (data.get("part_id") or "").isdigit() else None
        qty = count_field(data, "qty", what="Количество", default="1", limit=999)
        if part is None or not qty.ok:
            flash(request, qty.error or "Выберите позицию склада.", "err")
            return redirect(f"/orders/{order_id}")
        try:
            result = await service.issue_part_to_order(crm, order, part, qty.value,
                                                       by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/orders/{order_id}")
        flash(request, f"«{part['title']}» списано со склада: {result['qty']} шт., "
                       f"на полке осталось {result['stock_left']}.")
        return redirect(f"/orders/{order_id}")

    @app.post("/orders/{order_id}/items/{item_id}/delete")
    async def order_delete_item(request: Request, order_id: int,
                                item_id: int) -> Response:
        if not await crm.delete_order_item(order_id, item_id):
            flash(request, "Строки уже нет.", "err")
        return redirect(f"/orders/{order_id}")

    @app.post("/orders/{order_id}/edit")
    async def order_edit(request: Request, order_id: int) -> Response:
        order = await crm.work_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Наряд")
        data = await form(request)
        status = logic.check_order_status(data.get("status") or order["status"])
        estimate = cost_field(data, "estimate")
        note = logic.check_note(data.get("note"))
        for check in (status, estimate, note):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(f"/orders/{order_id}")
        if status.value == "done":
            flash(request, "Готовый наряд закрывается кнопкой «Закрыть наряд»: "
                           "она считает сумму и пишет ремонт в журнал.", "err")
            return redirect(f"/orders/{order_id}")
        tech_id = int(data["tech_id"]) if (data.get("tech_id") or "").isdigit() else None
        await crm.update_work_order(order_id, status=status.value, tech_id=tech_id,
                                    estimate=estimate.value, note=note.value)
        # Только смена техника: иначе человек получал бы «на тебя наряд»
        # при каждой правке сметы.
        if tech_id and tech_id != order.get("tech_id"):
            await notify_tech(order_id, tech_id)
        flash(request, "Наряд сохранён.")
        return redirect(f"/orders/{order_id}")

    @app.post("/orders/{order_id}/close")
    async def order_close(request: Request, order_id: int) -> Response:
        order = await crm.work_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Наряд")
        data = await form(request)
        bike_status = logic.check_choice(data.get("bike_status") or "available",
                                         logic.BIKE_MANUAL_STATUSES, what="Статус")
        if not bike_status.ok:
            flash(request, bike_status.error, "err")
            return redirect(f"/orders/{order_id}")
        try:
            totals = await service.close_order(crm, order, by=who(request),
                                               bike_status=bike_status.value)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/orders/{order_id}")
        # Клиентский ремонт - это человек, который ждёт свою технику.
        # Свой парк чинится молча: ждать там нечего и некому.
        if order.get("payer") == "client" and order.get("client_id"):
            client = await crm.client(order["client_id"])
            if client:
                await notify.repair_ready(bot, client, order, totals["total"])
        flash(request, f"Наряд закрыт: клиенту {logic.money(totals['total'])}, "
                       f"себестоимость {logic.money(totals['cost'])}.")
        return redirect(f"/orders/{order_id}")

    @app.post("/orders/{order_id}/paid")
    async def order_paid(request: Request, order_id: int) -> Response:
        """Отметка об оплате клиентского ремонта.

        Деньги остаются на наряде и в crm.ledger не попадают: журнал -
        это аренда, по нему считается средний чек парка.
        """
        order = await crm.work_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Наряд")
        if order["payer"] != "client":
            flash(request, "Свой ремонт клиент не оплачивает.", "err")
            return redirect(f"/orders/{order_id}")
        await crm.update_work_order(order_id, paid_at=datetime.now(UTC))
        flash(request, "Отмечено как оплаченный.")
        return redirect(f"/orders/{order_id}")

    # ─────────────────────── сервис: виды работ ───────────────────────

    @app.get("/work-types")
    async def work_types_page(request: Request) -> Response:
        return render(request, "work_types.html", rows=await crm.work_types(),
                      can_manage=may_edit(request, "service"))

    @app.post("/work-types")
    async def work_type_create(request: Request) -> Response:
        data = await form(request)
        title = logic.check_name(data.get("title"), what="Наименование")
        minutes = count_field(data, "minutes", what="Время", default="0", limit=999)
        price = cost_field(data, "price")
        for check in (title, minutes, price):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/work-types")
        category = (data.get("category") or "Прочее").strip()
        if category not in logic.WORK_CATEGORIES:
            category = "Прочее"
        node = (data.get("node") or "").strip() or None
        if node and not logic.check_choice(node, logic.REPAIR_NODES).ok:
            node = None
        try:
            await crm.create_work_type(title=title.value, category=category,
                                       minutes=minutes.value, price=price.value,
                                       node=node)
        except Exception as exc:                        # noqa: BLE001
            if not name_taken(exc):
                raise
            flash(request, "Работа с таким названием уже есть.", "err")
            return redirect("/work-types")
        flash(request, "Вид работ добавлен.")
        return redirect("/work-types")

    @app.post("/work-types/{type_id}")
    async def work_type_edit(request: Request, type_id: int) -> Response:
        if await crm.work_type(type_id) is None:
            return render(request, "missing.html", status_code=404, what="Вид работ")
        data = await form(request)
        if data.get("action") == "toggle":
            current = await crm.work_type(type_id)
            await crm.update_work_type(type_id, active=not current["active"])
            return redirect("/work-types")
        title = logic.check_name(data.get("title"), what="Наименование")
        minutes = count_field(data, "minutes", what="Время", default="0", limit=999)
        price = cost_field(data, "price")
        for check in (title, minutes, price):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/work-types")
        await crm.update_work_type(type_id, title=title.value, price=price.value,
                                   minutes=minutes.value)
        flash(request, "Сохранено.")
        return redirect("/work-types")

    # ─────────────────── реквизиты и шаблоны документов ───────────────────

    def template_rows(request: Request) -> list[dict]:
        """Шаблоны документов: что есть, читается ли и какие в нём поля.

        Панель не хранит шаблоны у себя - она показывает те, из которых бот
        собирает документы прямо сейчас. Проверка чтением: битый файл лучше
        увидеть здесь, чем в момент, когда клиенту уже сказали «договор готов».
        """
        del request
        rows = []
        for title, path in (("Договор аренды", cfg_path("contract_template")),
                            ("Акт приёма-передачи", cfg_path("act_in_template")),
                            ("Акт возврата", cfg_path("act_out_template")),
                            ("Согласие на обработку ПДн", cfg_path("soglasie_template")),
                            ("Договор выкупа", cfg_path("buyout_template")),
                            ("Политика обработки ПДн", cfg_path("pdn_policy_file"))):
            row = {"title": title, "path": str(path) if path else "—",
                   "ok": False, "fields": [], "error": ""}
            if path is None:
                row["error"] = "путь не задан"
            elif not _file_exists(str(path)):
                row["error"] = "файла нет на диске"
            else:
                row["size"] = os.path.getsize(str(path))
                try:
                    data = contract_service.load_template(Path(str(path)))
                    row["ok"] = True
                    row["fields"] = sorted(contract_service.placeholders(data))
                except Exception as exc:                 # noqa: BLE001
                    row["error"] = str(exc)
            rows.append(row)
        return rows

    def cfg_path(name: str) -> Any:
        return getattr(cfg, name, None)

    @app.get("/company")
    async def company_page(request: Request) -> Response:
        if not may_view(request, "settings"):
            return denied(request, "settings")
        settings = await crm.settings()
        return render(request, "company.html",
                      values={code: settings.get(code, "")
                              for code in company.COMPANY_FIELDS},
                      templates=template_rows(request))

    @app.post("/company")
    async def company_save(request: Request) -> Response:
        """Реквизиты организации: их подставляют договор и акты.

        Бот - другой процесс, он подхватывает правку снимком в течение
        нескольких минут; в панели об этом написано прямо.
        """
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        data = await form(request)
        clean: dict[str, str] = {}
        for code, label in company.COMPANY_FIELDS.items():
            value, error = company.check_value(data.get(code))
            if error:
                flash(request, f"{label}: {error}.", "err")
                return redirect("/company")
            clean[code] = value
        for code, value in clean.items():
            await crm.set_setting(code, value, by=who(request))
        # Панель и бот читают одни и те же настройки: снимок в этом
        # процессе обновляем сразу, чтобы не ждать своего же TTL.
        company.set_snapshot(await crm.settings())
        flash(request, "Реквизиты сохранены. Бот подхватит их в течение "
                       "нескольких минут.")
        return redirect("/company")

    # ───────────────── справочники: точки, модели, совместимость ─────────────────

    @app.get("/locations")
    async def locations_page(request: Request) -> Response:
        if not may_view(request, "settings"):
            return denied(request, "settings")
        return render(request, "locations.html", rows=await crm.locations())

    @app.post("/locations")
    async def location_create(request: Request) -> Response:
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        data = await form(request)
        name = logic.check_name(data.get("name"), what="Название точки")
        city = logic.check_name(data.get("city") or "Казань", what="Город")
        note = logic.check_note(data.get("note"))
        for check in (name, city, note):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/locations")
        try:
            await crm.create_location(name=name.value, city=city.value,
                                      address=(data.get("address") or "").strip() or None,
                                      note=note.value)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Точка с таким названием уже есть.", "err")
                return redirect("/locations")
            raise
        flash(request, "Точка добавлена.")
        return redirect("/locations")

    @app.post("/locations/{location_id}/toggle")
    async def location_toggle(request: Request, location_id: int) -> Response:
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        rows = [x for x in await crm.locations() if x["id"] == location_id]
        if not rows:
            return render(request, "missing.html", status_code=404, what="Точка")
        # Закрытая точка остаётся в карточках парка: велосипеды на ней
        # никуда не делись, и переписывать их ради красоты справочника
        # значит потерять, где они стоят.
        await crm.update_location(location_id, active=not rows[0]["active"])
        return redirect("/locations")

    @app.get("/models")
    async def models_page(request: Request) -> Response:
        if not may_view(request, "settings"):
            return denied(request, "settings")
        bikes = await crm.bike_models()
        batteries = await crm.battery_models()
        return render(request, "models.html", bike_models=bikes,
                      battery_models=batteries,
                      matrix=logic.compat_matrix(
                          [m for m in bikes if m["active"]],
                          [m for m in batteries if m["active"]],
                          await crm.compat_pairs()))

    @app.post("/models/bikes")
    async def bike_model_create(request: Request) -> Response:
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        data = await form(request)
        title = logic.check_name(data.get("title"), what="Название модели")
        note = logic.check_note(data.get("note"))
        slots = count_field(data, "battery_slots", what="Слотов АКБ", default="2",
                            limit=10)
        for check in (title, note, slots):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/models")
        try:
            await crm.create_bike_model(
                title=title.value, brand=(data.get("brand") or "").strip() or None,
                factory_title=(data.get("factory_title") or "").strip() or None,
                battery_slots=slots.value, note=note.value)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Такая модель уже есть.", "err")
                return redirect("/models")
            raise
        flash(request, "Модель добавлена.")
        return redirect("/models")

    @app.post("/models/batteries")
    async def battery_model_create(request: Request) -> Response:
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        data = await form(request)
        title = logic.check_name(data.get("title"), what="Название модели")
        note = logic.check_note(data.get("note"))
        price = cost_field(data, "price")
        months = count_field(data, "service_months", what="Срок службы",
                             default="15", limit=240)
        volt = count_field(data, "voltage", what="Напряжение", default="0", limit=200)
        for check in (title, note, price, months, volt):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/models")
        capacity = cost_field(data, "capacity")
        try:
            await crm.create_battery_model(
                title=title.value, brand=(data.get("brand") or "").strip() or None,
                voltage=volt.value or None,
                capacity=capacity.value if capacity.ok and capacity.value else None,
                price=price.value, service_months=months.value or 15, note=note.value)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Такая модель АКБ уже есть.", "err")
                return redirect("/models")
            raise
        flash(request, "Модель АКБ добавлена.")
        return redirect("/models")

    @app.post("/models/compat")
    async def compat_set(request: Request) -> Response:
        """Клетка матрицы совместимости: подходит, основная или пусто."""
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        data = await form(request)
        if not (data.get("bike_model_id") or "").isdigit() \
                or not (data.get("battery_model_id") or "").isdigit():
            flash(request, "Выберите модели.", "err")
            return redirect("/models")
        mode = data.get("mode") or "none"
        await crm.set_compat(int(data["bike_model_id"]), int(data["battery_model_id"]),
                             fits=mode in ("fits", "primary"),
                             primary_fit=mode == "primary")
        return redirect("/models")

    # ───────────────────────────── батареи ─────────────────────────────

    async def location_names() -> list[str]:
        """Точки из справочника; пустой справочник - константа из logic.

        Константа остаётся сидом: парк заведён с этими названиями, и
        пустая база не должна ломать формы.
        """
        try:
            names = await crm.location_names()
        except Exception:                                # noqa: BLE001
            names = []
        return names or list(logic.LOCATIONS)

    @app.get("/batteries")
    async def batteries_page(request: Request) -> Response:
        status = request.query_params.get("status") or ""
        q = request.query_params.get("q") or ""
        location = request.query_params.get("location") or ""
        rows = logic.battery_rows(await crm.batteries(
            status=status or None, q=q or None, location=location or None))
        return render(request, "batteries.html", rows=rows,
                      summary=logic.battery_summary(
                          logic.battery_rows(await crm.batteries())),
                      status=status, q=q, location=location,
                      locations=await location_names(),
                      models=await crm.battery_models(active_only=True))

    @app.get("/batteries/new")
    async def battery_new(request: Request) -> Response:
        if not may_edit(request, "batteries"):
            return denied(request, "batteries")
        return render(request, "battery_form.html", battery=None,
                      models=await crm.battery_models(active_only=True),
                      locations=await location_names())

    async def battery_fields(request: Request, data: dict) -> dict | None:
        code = logic.check_code(data.get("code"))
        note = logic.check_note(data.get("note"))
        price = (logic.check_amount(data.get("purchase_price"))
                 if (data.get("purchase_price") or "").strip() else logic.Check(True, None))
        bought = (logic.check_date(data.get("purchased_on"), default=None)
                  if (data.get("purchased_on") or "").strip() else logic.Check(True, None))
        location = logic.check_location(data.get("location"), await location_names())
        months = data.get("service_months") or "15"
        cycles = count_field(data, "cycles", what="Циклы", default="0", limit=99999)
        for check in (code, note, price, bought, location, cycles):
            if not check.ok:
                flash(request, check.error, "err")
                return None
        if not str(months).isdigit() or not 1 <= int(months) <= 240:
            flash(request, "Срок службы: число месяцев от 1 до 240.", "err")
            return None
        model_id = int(data["model_id"]) if (data.get("model_id") or "").isdigit() else None
        return {"code": code.value, "model_id": model_id,
                "serial_no": (data.get("serial_no") or "").strip() or None,
                "location": location.value, "purchase_price": price.value,
                "purchased_on": bought.value, "service_months": int(months),
                "cycles": cycles.value, "note": note.value}

    @app.post("/batteries")
    async def battery_create(request: Request) -> Response:
        if not may_edit(request, "batteries"):
            return denied(request, "batteries")
        fields = await battery_fields(request, await form(request))
        if fields is None:
            return redirect("/batteries/new")
        try:
            battery_id = await crm.create_battery(by=who(request), **fields)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Батарея с таким номером уже есть.", "err")
                return redirect("/batteries/new")
            raise
        flash(request, "Батарея заведена.")
        return redirect(f"/batteries/{battery_id}")

    @app.get("/batteries/{battery_id}")
    async def battery_card(request: Request, battery_id: int) -> Response:
        battery = await crm.battery(battery_id)
        if battery is None:
            return render(request, "missing.html", status_code=404, what="Батарея")
        row = logic.battery_rows([battery])[0]
        return render(request, "battery.html", battery=row,
                      log=await crm.battery_status_log(battery_id),
                      models=await crm.battery_models(active_only=True),
                      locations=await location_names(),
                      amortization=logic.battery_amortization(battery))

    @app.post("/batteries/{battery_id}/edit")
    async def battery_edit(request: Request, battery_id: int) -> Response:
        if not may_edit(request, "batteries"):
            return denied(request, "batteries")
        if await crm.battery(battery_id) is None:
            return render(request, "missing.html", status_code=404, what="Батарея")
        fields = await battery_fields(request, await form(request))
        if fields is None:
            return redirect(f"/batteries/{battery_id}")
        await crm.update_battery(battery_id, by=who(request), **fields)
        flash(request, "Батарея сохранена.")
        return redirect(f"/batteries/{battery_id}")

    @app.post("/batteries/{battery_id}/status")
    async def battery_status(request: Request, battery_id: int) -> Response:
        if not may_edit(request, "batteries"):
            return denied(request, "batteries")
        battery = await crm.battery(battery_id)
        if battery is None:
            return render(request, "missing.html", status_code=404, what="Батарея")
        data = await form(request)
        status = logic.check_choice(data.get("status"), logic.BATTERY_MANUAL_STATUSES,
                                    what="Статус батареи")
        if not status.ok:
            flash(request, status.error, "err")
            return redirect(f"/batteries/{battery_id}")
        if battery["status"] == "rented":
            flash(request, "Батарея у клиента: её снимает возврат или замена, "
                           "а не смена статуса.", "err")
            return redirect(f"/batteries/{battery_id}")
        await crm.update_battery(battery_id, status=status.value, by=who(request))
        flash(request, f"Статус: {logic.BATTERY_STATUSES[status.value]}.")
        return redirect(f"/batteries/{battery_id}")

    @app.post("/rentals/{rental_id}/battery")
    async def rental_battery_swap(request: Request, rental_id: int) -> Response:
        """Замена батареи у клиента: аренду это не трогает."""
        if not may_edit(request, "rentals"):
            return denied(request, "rentals")
        rental = await crm.rental(rental_id)
        if rental is None:
            return render(request, "missing.html", status_code=404, what="Аренда")
        data = await form(request)
        new = await crm.battery(int(data["battery_id"])) \
            if (data.get("battery_id") or "").isdigit() else None
        old = await crm.battery(int(data["old_id"])) \
            if (data.get("old_id") or "").isdigit() else None
        if new is None:
            flash(request, "Выберите батарею на замену.", "err")
            return redirect(f"/rentals/{rental_id}")
        try:
            await service.swap_battery(crm, rental, old, new, by=who(request),
                                       old_status=data.get("old_status") or "repair")
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/rentals/{rental_id}")
        flash(request, f"Батарея заменена на {new['code']}." if old is not None
              else f"Батарея {new['code']} выдана клиенту.")
        return redirect(f"/rentals/{rental_id}")

    # ─────────────────────── трекеры и карта ───────────────────────

    async def tracker_rows_now() -> list[dict]:
        """Трекеры с состоянием: панель в StarLine не ходит, она читает базу.

        Опрос живёт в процессе бота: у него уже есть расписание и бот
        для тревог, а веб-процессов может быть несколько — и каждый
        опрашивал бы StarLine по своему кругу.
        """
        return logic.tracker_rows(await crm.trackers(), settings=await crm.settings())

    @app.get("/map")
    async def fleet_map(request: Request) -> Response:
        if not may_view(request, "trackers"):
            return denied(request, "trackers")
        settings = await crm.settings()
        rows = logic.tracker_rows(await crm.trackers(), settings=settings)
        points = logic.map_points(rows)
        return render(request, "map.html", rows=rows, points=points,
                      points_json=json.dumps(points, ensure_ascii=False),
                      map_cfg=logic.map_config(settings),
                      summary=logic.tracker_summary(rows),
                      alerts=await crm.tracker_alerts(open_only=True, limit=50))

    @app.get("/trackers")
    async def trackers_page(request: Request) -> Response:
        if not may_view(request, "trackers"):
            return denied(request, "trackers")
        rows = await tracker_rows_now()
        return render(request, "trackers.html", rows=rows,
                      summary=logic.tracker_summary(rows),
                      alerts=await crm.tracker_alerts(open_only=True, limit=50),
                      free_bikes=await crm.bikes(limit=10000))

    @app.post("/trackers")
    async def tracker_create(request: Request) -> Response:
        """Метка заводится руками, если её ещё не видел опрос."""
        if not may_edit(request, "trackers"):
            return denied(request, "trackers")
        data = await form(request)
        device = logic.check_code(data.get("device_id"))
        note = logic.check_note(data.get("note"))
        for check in (device, note):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/trackers")
        try:
            await crm.create_tracker(
                device_id=device.value,
                alias=(data.get("alias") or "").strip() or None, note=note.value)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Трекер с таким номером устройства уже заведён.", "err")
                return redirect("/trackers")
            raise
        flash(request, "Трекер заведён. Привяжите его к велосипеду.")
        return redirect("/trackers")

    @app.post("/trackers/{tracker_id}/bike")
    async def tracker_bind(request: Request, tracker_id: int) -> Response:
        """Привязка трекера к велосипеду - и есть весь смысл раздела:
        без неё координаты принадлежат неизвестно чему."""
        if not may_edit(request, "trackers"):
            return denied(request, "trackers")
        tracker = await crm.tracker(tracker_id)
        if tracker is None:
            return render(request, "missing.html", status_code=404, what="Трекер")
        data = await form(request)
        raw = (data.get("bike_id") or "").strip()
        bike_id = int(raw) if raw.isdigit() else None
        if bike_id is not None and await crm.bike(bike_id) is None:
            flash(request, "Такого велосипеда нет.", "err")
            return redirect("/trackers")
        try:
            await crm.update_tracker(tracker_id, bike_id=bike_id)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "На этом велосипеде уже стоит другой трекер.", "err")
                return redirect("/trackers")
            raise
        flash(request, "Трекер привязан." if bike_id else "Трекер отвязан.")
        return redirect(f"/trackers/{tracker_id}")

    @app.post("/trackers/{tracker_id}/toggle")
    async def tracker_toggle(request: Request, tracker_id: int) -> Response:
        if not may_edit(request, "trackers"):
            return denied(request, "trackers")
        tracker = await crm.tracker(tracker_id)
        if tracker is None:
            return render(request, "missing.html", status_code=404, what="Трекер")
        await crm.update_tracker(tracker_id, active=not tracker["active"])
        flash(request, "Трекер снят с наблюдения." if tracker["active"]
              else "Трекер снова под наблюдением.")
        return redirect(f"/trackers/{tracker_id}")

    @app.post("/trackers/alerts/{alert_id}")
    async def tracker_alert_handle(request: Request, alert_id: int) -> Response:
        if not may_edit(request, "trackers"):
            return denied(request, "trackers")
        data = await form(request)
        nxt = data.get("next") or ""
        back = nxt if nxt.startswith("/") and not nxt.startswith("//") else "/trackers"
        await crm.handle_alert(alert_id, by=who(request))
        flash(request, "Тревога снята.")
        return redirect(back)

    @app.get("/trackers/{tracker_id}")
    async def tracker_card(request: Request, tracker_id: int) -> Response:
        if not may_view(request, "trackers"):
            return denied(request, "trackers")
        tracker = await crm.tracker(tracker_id)
        if tracker is None:
            return render(request, "missing.html", status_code=404, what="Трекер")
        settings = await crm.settings()
        row = logic.tracker_rows([tracker], settings=settings)[0]
        track = await crm.tracker_positions(tracker_id, 200)
        return render(request, "tracker.html", tracker=row, track=track,
                      run_km=logic.track_distance(track),
                      map_cfg=logic.map_config(settings),
                      points_json=json.dumps(logic.map_points([row]),
                                             ensure_ascii=False),
                      free_bikes=await crm.bikes(limit=10000),
                      alerts=[a for a in await crm.tracker_alerts(open_only=False,
                                                                  limit=50)
                              if a["tracker_id"] == tracker_id])

    # ─────────────────── закупки основных средств ───────────────────

    @app.get("/assets")
    async def assets_page(request: Request) -> Response:
        """Парк как основные средства: сколько вложено, сколько осталось."""
        if not may_view(request, "finance"):
            return denied(request, "finance")
        tab = request.query_params.get("tab") or "all"
        rows = logic.asset_rows(await crm.bikes(limit=10000))
        summary = logic.asset_summary(rows)
        if tab == "worn":
            rows = [b for b in rows if b["worn_out"]
                    and b.get("status") not in ("sold", "written_off")]
        elif tab == "written_off":
            rows = [b for b in rows if b.get("status") in ("sold", "written_off")]
        elif tab == "live":
            rows = [b for b in rows if b.get("status") not in ("sold", "written_off")]
        return render(request, "assets.html", rows=rows, summary=summary, tab=tab,
                      purchases=await crm.purchases(limit=100),
                      suppliers=await crm.suppliers(active_only=True))

    @app.post("/assets")
    async def asset_purchase(request: Request) -> Response:
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        data = await form(request)
        codes, error = logic.purchase_codes(data.get("codes"))
        model = logic.check_name(data.get("model"), what="Модель")
        price = logic.check_amount(data.get("purchase_price")) \
            if (data.get("purchase_price") or "").strip() else logic.Check(True, None)
        bought = logic.check_date(data.get("purchased_on"), default=date.today())
        residual = cost_field(data, "residual_price")
        note = logic.check_note(data.get("note"))
        if error:
            flash(request, error, "err")
            return redirect("/assets")
        for check in (model, price, bought, residual, note):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/assets")
        months = data.get("service_months") or "24"
        bat_months = data.get("battery_service_months") or "15"
        batteries = data.get("battery_count") or "2"
        for label, value in (("Срок службы", months), ("Срок службы АКБ", bat_months),
                             ("АКБ", batteries)):
            if not str(value).isdigit():
                flash(request, f"{label}: нужно число.", "err")
                return redirect("/assets")
        bat_price = logic.check_amount(data.get("battery_price")) \
            if (data.get("battery_price") or "").strip() else logic.Check(True, None)
        if not bat_price.ok:
            flash(request, bat_price.error, "err")
            return redirect("/assets")
        location = (data.get("location") or "").strip() or None
        if location and location not in logic.LOCATIONS:
            flash(request, "Точка: недопустимое значение.", "err")
            return redirect("/assets")
        supplier_id = int(data["supplier_id"]) \
            if (data.get("supplier_id") or "").isdigit() else None
        try:
            result = await service.buy_bikes(
                crm, supplier_id=supplier_id, purchased_on=bought.value, codes=codes,
                model=model.value, price=price.value or Decimal(0),
                battery_count=int(batteries), service_months=int(months),
                residual=residual.value, battery_price=bat_price.value,
                battery_months=int(bat_months), location=location, note=note.value,
                by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/assets")
        purchase = await crm.purchase(result["purchase_id"])
        flash(request, f"Закупка {purchase['no']}: заведено велосипедов "
                       f"{result['bikes']} на {logic.money(purchase['total'])}.")
        return redirect("/assets")

    # ─────────────────────── склад запчастей ───────────────────────

    def doc_lines(data: dict, *, limit: int = 8) -> list[dict]:
        """Строки складского документа из формы без JS: фиксированное число
        пустых строк, заполненные берём, пустые молча пропускаем."""
        lines = []
        for i in range(limit):
            part_id = (data.get(f"part_id_{i}") or "").strip()
            qty = (data.get(f"qty_{i}") or "").strip()
            if not part_id.isdigit() or not qty.isdigit() or int(qty) <= 0:
                continue
            price = cost_field(data, f"price_{i}")
            lines.append({"part_id": int(part_id), "qty": int(qty),
                          "price": price.value if price.ok else Decimal(0)})
        return lines

    @app.get("/parts")
    async def parts_page(request: Request) -> Response:
        node = request.query_params.get("node") or ""
        q = request.query_params.get("q") or ""
        rows = logic.part_rows(await crm.parts(node=node or None, q=q or None),
                               await crm.stock_map())
        return render(request, "parts.html", rows=rows,
                      summary=logic.stock_summary(rows), node=node, q=q,
                      nodes=await crm.repair_nodes())

    @app.get("/parts/new")
    async def part_new(request: Request) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        return render(request, "part_form.html", part=None,
                      nodes=await crm.repair_nodes())

    @app.post("/parts")
    async def part_create(request: Request) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        data = await form(request)
        fields = part_fields(request, data)
        if fields is None:
            return redirect("/parts/new")
        try:
            part_id = await crm.create_part(**fields)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Позиция с таким названием уже есть.", "err")
                return redirect("/parts/new")
            raise
        flash(request, "Позиция заведена.")
        return redirect(f"/parts/{part_id}")

    def part_fields(request: Request, data: dict) -> dict | None:
        title = logic.check_name(data.get("title"), what="Название")
        unit = logic.check_unit(data.get("unit"))
        cost = cost_field(data, "cost")
        price = cost_field(data, "price")
        minimum = count_field(data, "min_stock", what="Неснижаемый остаток",
                              default="0", limit=9999)
        note = logic.check_note(data.get("note"))
        for check in (title, unit, cost, price, minimum, note):
            if not check.ok:
                flash(request, check.error, "err")
                return None
        node = (data.get("node") or "").strip() or None
        if node and not logic.check_choice(node, logic.REPAIR_NODES).ok:
            node = None
        return {"title": title.value, "node": node, "unit": unit.value,
                "cost": cost.value, "price": price.value, "min_stock": minimum.value,
                "model": (data.get("model") or "").strip() or None, "note": note.value}

    @app.get("/parts/receipts")
    async def part_receipts(request: Request) -> Response:
        return render(request, "part_docs.html", kind="receipt",
                      rows=await crm.part_docs(kind="receipt"),
                      parts=await crm.parts(active_only=True),
                      suppliers=await crm.suppliers(active_only=True))

    @app.post("/parts/receipts")
    async def part_receipt_create(request: Request) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        data = await form(request)
        note = logic.check_note(data.get("note"))
        if not note.ok:
            flash(request, note.error, "err")
            return redirect("/parts/receipts")
        supplier_id = int(data["supplier_id"]) \
            if (data.get("supplier_id") or "").isdigit() else None
        try:
            doc_id = await service.receive_parts(
                crm, supplier_id=supplier_id, lines=doc_lines(data),
                note=note.value, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/parts/receipts")
        doc = await crm.part_doc(doc_id)
        flash(request, f"Приход {doc['no']} проведён на {logic.money(doc['total'])}.")
        return redirect("/parts/receipts")

    @app.get("/parts/write-offs")
    async def part_write_offs(request: Request) -> Response:
        return render(request, "part_docs.html", kind="write_off",
                      rows=await crm.part_docs(kind="write_off"),
                      parts=await crm.parts(active_only=True), suppliers=[])

    @app.post("/parts/write-offs")
    async def part_write_off_create(request: Request) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        data = await form(request)
        note = logic.check_note(data.get("note"))
        if not note.ok:
            flash(request, note.error, "err")
            return redirect("/parts/write-offs")
        try:
            doc_id = await service.write_off_parts(
                crm, lines=doc_lines(data, limit=5), note=note.value, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/parts/write-offs")
        doc = await crm.part_doc(doc_id)
        flash(request, f"Списание {doc['no']} проведено.")
        return redirect("/parts/write-offs")

    @app.get("/parts/moves")
    async def part_moves_page(request: Request) -> Response:
        kind = request.query_params.get("kind") or ""
        return render(request, "part_moves.html", kind=kind,
                      rows=await crm.part_moves(kind=kind or None, limit=300))

    @app.get("/parts/{part_id}")
    async def part_card(request: Request, part_id: int) -> Response:
        part = await crm.part(part_id)
        if part is None:
            return render(request, "missing.html", status_code=404, what="Позиция")
        return render(request, "part.html", part=part,
                      stock=await crm.part_stock(part_id),
                      moves=await crm.part_moves(part_id=part_id, limit=100),
                      nodes=await crm.repair_nodes())

    @app.post("/parts/{part_id}/edit")
    async def part_edit(request: Request, part_id: int) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        part = await crm.part(part_id)
        if part is None:
            return render(request, "missing.html", status_code=404, what="Позиция")
        data = await form(request)
        fields = part_fields(request, data)
        if fields is None:
            return redirect(f"/parts/{part_id}")
        # Себестоимость правится только приходом: руками её поставить -
        # значит разойтись со складом на первом же ремонте.
        fields.pop("cost", None)
        fields["active"] = bool(data.get("active"))
        await crm.update_part(part_id, **fields)
        flash(request, "Позиция сохранена.")
        return redirect(f"/parts/{part_id}")

    @app.post("/parts/{part_id}/count")
    async def part_count(request: Request, part_id: int) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        part = await crm.part(part_id)
        if part is None:
            return render(request, "missing.html", status_code=404, what="Позиция")
        data = await form(request)
        fact = count_field(data, "fact", what="Факт на полке", default="0", limit=99999)
        if not fact.ok:
            flash(request, fact.error, "err")
            return redirect(f"/parts/{part_id}")
        try:
            result = await service.count_part(crm, part, fact.value, by=who(request),
                                              note=(data.get("note") or "").strip() or None)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/parts/{part_id}")
        if result["delta"] == 0:
            flash(request, "Сошлось: остаток и факт совпадают.")
        else:
            flash(request, f"Поправлено на {result['delta']:+d}, "
                           f"остаток {result['stock']}.")
        return redirect(f"/parts/{part_id}")

    # ─────────────────────── склад: поставщики ───────────────────────

    @app.get("/suppliers")
    async def suppliers_page(request: Request) -> Response:
        return render(request, "suppliers.html", rows=await crm.suppliers())

    @app.post("/suppliers")
    async def supplier_create(request: Request) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        data = await form(request)
        name = logic.check_name(data.get("name"), what="Поставщик")
        note = logic.check_note(data.get("note"))
        for check in (name, note):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/suppliers")
        phone = bot_logic.normalize_phone(data.get("phone")) \
            if (data.get("phone") or "").strip() else None
        try:
            await crm.create_supplier(name=name.value, phone=phone, note=note.value)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Такой поставщик уже есть.", "err")
                return redirect("/suppliers")
            raise
        flash(request, "Поставщик добавлен.")
        return redirect("/suppliers")

    @app.post("/suppliers/{supplier_id}/toggle")
    async def supplier_toggle(request: Request, supplier_id: int) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        supplier = await crm.supplier(supplier_id)
        if supplier is None:
            return render(request, "missing.html", status_code=404, what="Поставщик")
        await crm.update_supplier(supplier_id, active=not supplier["active"])
        return redirect("/suppliers")

    # ─────────────────────── склад: заказ запчастей ───────────────────────

    @app.get("/part-orders")
    async def part_orders_page(request: Request) -> Response:
        rows = logic.part_rows(await crm.parts(active_only=True), await crm.stock_map())
        order = await crm.open_part_order()
        return render(request, "part_orders.html",
                      needs=logic.part_needs(rows, await crm.waiting_orders_parts()),
                      orders=await crm.part_orders(limit=100), current=order,
                      items=await crm.part_order_items(order["id"]) if order else [],
                      parts=await crm.parts(active_only=True),
                      suppliers=await crm.suppliers(active_only=True))

    @app.post("/part-orders/collect")
    async def part_order_collect(request: Request) -> Response:
        """Собрать потребности в заказ одной кнопкой."""
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        result = await service.collect_part_needs(crm, by=who(request))
        if result["added"]:
            flash(request, f"В заказ {result['order']['no']} добавлено строк: "
                           f"{result['added']}.")
        else:
            flash(request, "Новых потребностей нет: всё уже в заказе.")
        return redirect("/part-orders")

    @app.post("/part-orders/items")
    async def part_order_add_item(request: Request) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        data = await form(request)
        part = await crm.part(int(data["part_id"])) \
            if (data.get("part_id") or "").isdigit() else None
        qty = count_field(data, "qty", what="Количество", default="1", limit=9999)
        if part is None or not qty.ok:
            flash(request, qty.error or "Выберите позицию.", "err")
            return redirect("/part-orders")
        order = await crm.open_part_order()
        if order is None:
            order_id = await crm.create_part_order(supplier_id=None, note=None,
                                                   created_by=who(request))
            order = await crm.part_order(order_id)
        added = await crm.add_part_order_item(
            order["id"], part_id=part["id"], qty=qty.value,
            price=logic.to_money(part.get("cost") or 0), source="manual")
        flash(request, "Позиция добавлена в заказ." if added
              else "Эта позиция в заказе уже есть.", "ok" if added else "err")
        return redirect("/part-orders")

    @app.post("/part-orders/{order_id}/items/{item_id}/delete")
    async def part_order_delete_item(request: Request, order_id: int,
                                     item_id: int) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        if not await crm.delete_part_order_item(order_id, item_id):
            flash(request, "Строки уже нет.", "err")
        return redirect("/part-orders")

    @app.post("/part-orders/{order_id}/status")
    async def part_order_status(request: Request, order_id: int) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        order = await crm.part_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Заказ")
        data = await form(request)
        status = logic.check_choice(data.get("status"), ("ordered", "cancelled"),
                                    what="Статус заказа")
        if not status.ok:
            flash(request, status.error, "err")
            return redirect("/part-orders")
        supplier_id = int(data["supplier_id"]) \
            if (data.get("supplier_id") or "").isdigit() else order.get("supplier_id")
        items = await crm.part_order_items(order_id)
        patch = {"status": status.value, "supplier_id": supplier_id,
                 "total": logic.order_total(items)}
        if status.value == "ordered":
            patch["ordered_at"] = datetime.now(UTC)
        else:
            patch["closed_at"] = datetime.now(UTC)
        await crm.update_part_order(order_id, **patch)
        flash(request, "Заказ отправлен поставщику." if status.value == "ordered"
              else "Заказ отменён.")
        return redirect("/part-orders")

    @app.post("/part-orders/{order_id}/receive")
    async def part_order_receive(request: Request, order_id: int) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        order = await crm.part_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Заказ")
        try:
            doc_id = await service.receive_part_order(crm, order, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/part-orders")
        doc = await crm.part_doc(doc_id)
        flash(request, f"Заказ принят: приход {doc['no']} на {logic.money(doc['total'])}.")
        return redirect("/part-orders")

    # ─────────────────────── пересчёт техники ───────────────────────

    @app.get("/stock-takes")
    async def stock_takes_page(request: Request) -> Response:
        return render(request, "stock_takes.html",
                      rows=await crm.stock_takes(limit=100),
                      current=await crm.open_stock_take())

    @app.post("/stock-takes")
    async def stock_take_start(request: Request) -> Response:
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        data = await form(request)
        scope = logic.check_scope(data.get("scope") or "all")
        note = logic.check_note(data.get("note"))
        for check in (scope, note):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/stock-takes")
        location = (data.get("location") or "").strip() or None
        try:
            take_id = await service.start_stock_take(
                crm, scope=scope.value, location=location, note=note.value,
                by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/stock-takes")
        flash(request, "Пересчёт начат: отмечайте технику, которую видите.")
        return redirect(f"/stock-takes/{take_id}")

    @app.get("/stock-takes/{take_id}")
    async def stock_take_page(request: Request, take_id: int) -> Response:
        take = await crm.stock_take(take_id)
        if take is None:
            return render(request, "missing.html", status_code=404, what="Пересчёт")
        items = await crm.take_items(take_id)
        counts = logic.take_counts(items)
        return render(request, "stock_take.html", take=take, items=items,
                      counts=counts, progress=logic.take_progress(counts))

    @app.post("/stock-takes/{take_id}/scan")
    async def stock_take_scan(request: Request, take_id: int) -> Response:
        """Отметка по номеру на раме: один ввод - одна единица техники."""
        take = await crm.stock_take(take_id)
        if take is None:
            return render(request, "missing.html", status_code=404, what="Пересчёт")
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        if not logic.take_is_open(take):
            flash(request, "Пересчёт закрыт.", "err")
            return redirect(f"/stock-takes/{take_id}")
        data = await form(request)
        code = logic.check_code(data.get("code"))
        if not code.ok:
            flash(request, code.error, "err")
            return redirect(f"/stock-takes/{take_id}")
        result = await service.take_add_found(crm, take, code.value)
        flash(request, result["message"], "ok" if result["state"] == "found" else "err")
        return redirect(f"/stock-takes/{take_id}")

    @app.post("/stock-takes/{take_id}/items/{item_id}")
    async def stock_take_item(request: Request, take_id: int, item_id: int) -> Response:
        take = await crm.stock_take(take_id)
        if take is None:
            return render(request, "missing.html", status_code=404, what="Пересчёт")
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        if not logic.take_is_open(take):
            flash(request, "Пересчёт закрыт.", "err")
            return redirect(f"/stock-takes/{take_id}")
        data = await form(request)
        state = logic.check_choice(data.get("state"), ("found", "expected", "missing"),
                                   what="Отметка")
        if not state.ok:
            flash(request, state.error, "err")
            return redirect(f"/stock-takes/{take_id}")
        if not await crm.set_take_item(take_id, item_id, state=state.value):
            flash(request, "Строки уже нет.", "err")
        return redirect(f"/stock-takes/{take_id}")

    @app.post("/stock-takes/{take_id}/items/{item_id}/delete")
    async def stock_take_item_delete(request: Request, take_id: int,
                                     item_id: int) -> Response:
        take = await crm.stock_take(take_id)
        if take is None:
            return render(request, "missing.html", status_code=404, what="Пересчёт")
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        if not logic.take_is_open(take):
            flash(request, "Пересчёт закрыт.", "err")
            return redirect(f"/stock-takes/{take_id}")
        item = await crm.take_item(take_id, item_id)
        # Убрать можно только лишнюю строку: снести ожидаемую - это стереть
        # недостачу, ради которой пересчёт и делают.
        if item is None or item["state"] != "extra":
            flash(request, "Убрать можно только лишнюю строку.", "err")
            return redirect(f"/stock-takes/{take_id}")
        await crm.delete_take_item(take_id, item_id)
        flash(request, "Лишняя строка убрана.")
        return redirect(f"/stock-takes/{take_id}")

    @app.post("/stock-takes/{take_id}/mark-all")
    async def stock_take_mark_all(request: Request, take_id: int) -> Response:
        take = await crm.stock_take(take_id)
        if take is None:
            return render(request, "missing.html", status_code=404, what="Пересчёт")
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        if not logic.take_is_open(take):
            flash(request, "Пересчёт закрыт.", "err")
            return redirect(f"/stock-takes/{take_id}")
        data = await form(request)
        state = "expected" if (data.get("state") or "") == "expected" else "found"
        hit = await crm.mark_take_all(take_id, state=state)
        flash(request, f"Отмечено строк: {hit}." if state == "found"
              else f"Снято отметок: {hit}.")
        return redirect(f"/stock-takes/{take_id}")

    @app.post("/stock-takes/{take_id}/close")
    async def stock_take_close(request: Request, take_id: int) -> Response:
        take = await crm.stock_take(take_id)
        if take is None:
            return render(request, "missing.html", status_code=404, what="Пересчёт")
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        data = await form(request)
        try:
            result = await service.finish_stock_take(
                crm, take, by=who(request),
                lose_missing=bool(data.get("lose_missing")),
                return_found=bool(data.get("return_found")))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/stock-takes/{take_id}")
        parts = [f"нашли {result['found']} из {result['total']}"]
        if result["missing"]:
            parts.append(f"не нашли {result['missing']}")
        if result["lost"]:
            parts.append(f"переведено в «Утерян»: {result['lost']}")
        if result["returned"]:
            parts.append(f"вернулось в парк: {result['returned']}")
        flash(request, "Пересчёт закрыт: " + ", ".join(parts) + ".")
        return redirect(f"/stock-takes/{take_id}")

    # ─────────────────────── импорт таблицы ───────────────────────

    @app.get("/import")
    async def import_page(request: Request) -> Response:
        return render(request, "import.html", report=None, applied=False)

    @app.post("/import")
    async def import_run(request: Request) -> Response:
        data = await request.form()
        upload = data.get("file")
        apply = data.get("apply") == "1"
        if upload is None or isinstance(upload, str) or not upload.filename:
            flash(request, "Выберите файл таблицы (.xlsx).", "err")
            return redirect("/import")
        if not upload.filename.lower().endswith(".xlsx"):
            flash(request, "Нужна таблица Excel в формате .xlsx.", "err")
            return redirect("/import")
        content = await upload.read(IMPORT_MAX_BYTES + 1)
        await upload.close()
        if len(content) > IMPORT_MAX_BYTES:
            flash(request, "Файл больше 20 МБ - это не учётная таблица.", "err")
            return redirect("/import")
        try:
            plan, done = await import_xlsx.run(crm, content, apply=apply, by=who(request))
        except import_xlsx.ImportError_ as e:
            flash(request, str(e), "err")
            return redirect("/import")
        except Exception:                                  # noqa: BLE001
            log.exception("импорт таблицы %s не удался", upload.filename)
            flash(request, "Импорт прерван ошибкой; что успело записаться - в базе, "
                           "повторная загрузка пропустит уже добавленное. "
                           "Подробности в логе панели.", "err")
            return redirect("/import")
        if apply:
            flash(request, f"Записано: велосипедов {done['bikes']}, клиентов {done['clients']}, "
                           f"аренд {done['rentals']}.")
        return render(request, "import.html", report=import_xlsx.report_text(plan, done),
                      applied=apply, filename=upload.filename)

    return app


async def ensure_admin(crm: Any, cfg: WebConfig) -> str | None:
    """Первый администратор при пустой таблице сотрудников.

    Пароль - из секрета CRM_ADMIN_PASSWORD; если его нет, генерируется
    и возвращается вызывающему, чтобы тот показал его в логе один раз.
    """
    if await crm.staff_count() > 0:
        return None
    password = cfg.admin_password or logic.generate_password()
    owner = await crm.access_profile_by_code("owner")
    await crm.create_staff(cfg.admin_login, logic.hash_password(password),
                           "Администратор", "admin", owner["id"] if owner else None)
    return None if cfg.admin_password else password

