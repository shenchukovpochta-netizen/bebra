"""Веб-панель CRM: FastAPI + Jinja2, формы без JavaScript-фреймворков.

Всё серверное: страница - это шаблон, действие - POST формы и редирект.
Так панель открывается с любого телефона, а код читается сверху вниз.
Данные приходят из CrmDB (или его заглушки в тестах), решения - из
app.crm.logic и app.crm.service, уведомления клиентам - app.crm.notify.
"""

from __future__ import annotations

import asyncio
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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .. import logic as bot_logic
from .. import texts
from ..crm import billing, company, doctemplates, import_xlsx, logic, notices, notify, service
from ..services import contract as contract_service
from ..services import tochka
from .config import WebConfig

log = logging.getLogger(__name__)

# Снимок сверки техники: телефонное фото столько и весит, а всё,
# что больше, - это чей-то скриншот экрана целиком.
BIKE_PHOTO_MAX = 8 * 1024 * 1024
HERE = Path(__file__).resolve().parent
# Страница подписания открыта клиенту: он не сотрудник и в панель
# не входит. Защита у неё одна - случайный токен в ссылке.
PUBLIC = ("/login", "/static", "/healthz", "/sign/")
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


def _xlsx(filename: str, header: list[str], rows: list[list[Any]]) -> Response:
    """Тот же набор строк, но настоящей таблицей Excel.

    CSV Excel открывает по-разному в зависимости от настроек локали, и
    суммы в нём - текст: выгрузку приходится доводить руками. Здесь числа
    остаются числами, даты датами, шапка закреплена - файл открывают и
    сразу считают.

    Защиты от формул тут не нужно: значение уезжает ячейкой своего типа,
    и строка, начинающаяся с «=», лежит строкой - openpyxl не делает из
    неё формулу.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    book = Workbook()
    sheet = book.active
    sheet.title = "Выгрузка"
    sheet.append(list(header))
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in rows:
        sheet.append([_xlsx_cell(v) for v in row])
    # Шапка не уезжает при прокрутке: в выгрузке парка 190 строк.
    sheet.freeze_panes = "A2"
    widths = [len(str(h)) for h in header]
    for row in rows:
        for i, value in enumerate(row[:len(widths)]):
            widths[i] = max(widths[i], len(_cell(value)))
    for i, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 9), 42)
    for column in sheet.iter_cols(min_row=2):
        for cell in column:
            if isinstance(cell.value, (int, float)):
                cell.number_format = "# ##0.00" if isinstance(cell.value, float) \
                    else "# ##0"
            elif isinstance(cell.value, datetime):
                cell.number_format = "DD.MM.YYYY HH:MM"
            elif isinstance(cell.value, date):
                cell.number_format = "DD.MM.YYYY"
            elif isinstance(cell.value, str) and cell.value.startswith("="):
                # openpyxl по первому символу решает, что это формула.
                # Имя клиента приходит из бота как набрал человек, и
                # «=HYPERLINK(…)» в ФИО превратило бы выгрузку в ссылку
                # у оператора. Говорим явно: это строка.
                cell.data_type = "s"
    buf = io.BytesIO()
    book.save(buf)
    return Response(
        buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument."
                   "spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _xlsx_cell(value: Any) -> Any:
    """Значение как есть, чтобы Excel считал его числом или датой."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        # Excel не понимает часовой пояс: приводим к местному и снимаем его.
        return _local(value).replace(tzinfo=None)
    if isinstance(value, (int, float, date)):
        return value
    return str(value)


# В каких видах отдаются выгрузки. xlsx - рабочий: числа остаются
# числами и сумму можно поставить сразу. csv оставлен для тех, кто
# грузит выгрузку во что-то своё.
EXPORT_FORMATS = ("xlsx", "csv")


def _table(fmt: str, stem: str, header: list[str], rows: list[list[Any]]) -> Response:
    """Одна выгрузка в двух видах. Неизвестное расширение - 404.

    Молча отдать csv на запрос `.pdf` значит соврать в имени файла, и
    оператор откроет его один раз, а потом перестанет доверять выгрузке.
    """
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(status_code=404)
    if fmt == "xlsx":
        return _xlsx(f"{stem}.xlsx", header, rows)
    return _csv(f"{stem}.csv", header, rows)


def _iso(value: Any) -> str:
    return value.strftime("%Y-%m-%d") if isinstance(value, date) else ""


def create_app(*, crm: Any, db: Any, cfg: WebConfig, bot: Any = None) -> FastAPI:
    app = FastAPI(title=cfg.title, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals.update(
        money=logic.money, money_signed=logic.money_signed, period_label=logic.period_label,
        per_day=logic.per_day,
        KINDS=logic.KINDS, METHODS=logic.METHODS, BIKE_STATUSES=logic.BIKE_STATUSES,
        BIKE_MANUAL_STATUSES=logic.BIKE_MANUAL_STATUSES,
        OPERATIONAL_STATUSES=logic.OPERATIONAL_STATUSES, IDLE_STATUSES=logic.IDLE_STATUSES,
        LOCATIONS=logic.LOCATIONS, REPAIR_NODES=logic.REPAIR_NODES,
        TRACKER_ALERTS=logic.TRACKER_ALERTS, TRACK_RANGES=logic.TRACK_RANGES,
        SIGN_STATUSES=logic.SIGN_STATUSES, SIGN_EVENTS=logic.SIGN_EVENTS,
        SIGN_DOC_KINDS=logic.SIGN_DOC_KINDS,
        SIGN_CODE_MINUTES=logic.SIGN_CODE_MINUTES,
        SIGN_LINK_DAYS=logic.SIGN_LINK_DAYS,
        TARIFF_KINDS=logic.TARIFF_KINDS, EXTRA_KINDS=logic.EXTRA_KINDS,
        MAX_EXTRA_BATTERIES=logic.MAX_EXTRA_BATTERIES,
        AUDIENCES=logic.AUDIENCES, CAMPAIGN_STATUSES=logic.CAMPAIGN_STATUSES,
        SEND_STATUSES=logic.SEND_STATUSES, SEND_CHANNELS=logic.SEND_CHANNELS,
        TEMPLATE_FIELDS=logic.TEMPLATE_FIELDS,
        CASH_MOVE_KINDS=logic.CASH_MOVE_KINDS, CASH_STATUSES=logic.CASH_STATUSES,
        CASH_DIFF_NOISE=logic.CASH_DIFF_NOISE,
        BANK_STATUSES=logic.BANK_STATUSES, MATCH_REASONS=logic.MATCH_REASONS,
        PAY_STATUSES=logic.PAY_STATUSES, PAY_KINDS=logic.PAY_KINDS,
        NOTICES=logic.NOTICES, NOTICE_GROUPS=logic.NOTICE_GROUPS,
        DOC_TEMPLATES=logic.DOC_TEMPLATES, COMPANY_MARKS=logic.COMPANY_MARKS,
        BIKE_PASSPORT=logic.BIKE_PASSPORT, TAKE_WHAT=logic.TAKE_WHAT,
        ALERT_LEVELS=logic.ALERT_LEVELS, ALERT_STATES=logic.ALERT_STATES,
        ALERT_SNOOZE_HOURS=logic.ALERT_SNOOZE_HOURS,
        BATTERY_PASSPORT=logic.BATTERY_PASSPORT,
        STOCK_STALE_DAYS=logic.STOCK_STALE_DAYS,
        LIST_SIZES=logic.LIST_SIZES,
        NOTICE_TARGETS=logic.NOTICE_TARGETS,
        NOTICE_STATUSES=logic.NOTICE_STATUSES,
        NOTICE_LOG_DAYS=logic.NOTICE_LOG_DAYS,
        PAY_METHODS=logic.PAY_METHODS, card_title=logic.card_title,
        AUTOCHARGE_HOUR=logic.AUTOCHARGE_HOUR,
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
        ORDER_MANUAL_STATUSES=logic.ORDER_MANUAL_STATUSES,
        ORDER_OPEN=logic.ORDER_OPEN,
        WORK_CATEGORIES=logic.WORK_CATEGORIES, ORDER_STUCK_DAYS=logic.ORDER_STUCK_DAYS,
        TAKE_SCOPES=logic.TAKE_SCOPES, TAKE_STATES=logic.TAKE_STATES,
        REF_STATUSES=logic.REF_STATUSES, staff_tg_label=logic.staff_tg_label,
        BONUS_KINDS=logic.BONUS_KINDS, REVIEW_SITES=logic.REVIEW_SITES,
        COMPANY_FIELDS=company.COMPANY_FIELDS,
        CONTACT_FIELDS=company.CONTACT_FIELDS,
        CLIENT_CHANNELS=logic.CLIENT_CHANNELS, channel_label=logic.channel_label,
        EMPLOYERS=logic.EMPLOYERS, EXPERIENCE=logic.EXPERIENCE,
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
        # Месяц листается стрелками: прошлый - целиком, текущий - по
        # сегодняшний день, вперёд листать некуда.
        span = logic.month_bounds(
            logic.month_from(request.query_params.get("month"), today=today),
            today=today)
        first, next_month = span["first"], span["next"]
        month_metrics = await period_metrics(
            since=datetime.combine(first, datetime.min.time()).astimezone(),
            until=(datetime.now().astimezone() if span["is_current"] else
                   datetime.combine(next_month, datetime.min.time()).astimezone()))
        soon = logic.freeing_soon(rows, today=today)
        month_totals = await crm.ledger_totals(since=today.replace(day=1))
        # Деньги по дням месяца: столбики «пришло», линия накопленного
        # долга и пунктир плана в день. Помесячных чисел мало - по ним
        # не видно, в какой день всё пошло не так.
        chart = logic.money_chart(
            await crm.money_by_day(first, span["last"]),
            plan_per_day=logic.to_money(plan["check"] * plan["rented"]),
            today=span["today"])
        return render(request, "dashboard.html",
                      plan=plan, span=span, bot_state=await bot_health(),
                      progress=logic.plan_progress(
                          plan, month_metrics,
                          days_in_month=span["days"],
                          days_passed=span["passed"]),
                      soon=soon,
                      counts=await crm.counts(), bikes=bikes_by,
                      operational=operational,
                      tiles=logic.fleet_tiles(
                          bikes_by, plan,
                          spare=sum(1 for b in fleet if b.get("spare")
                                    and b.get("status") in logic.OPERATIONAL_STATUSES)),
                      metrics=metrics, losses=logic.fleet_losses(metrics),
                      loss_today=logic.loss_per_day(bikes_by),
                      # Кто именно стоит и почём: список с деньгами -
                      # это решение, а плитка «в ремонте 7» - только повод
                      # сходить в сервис и посмотреть.
                      standing=await standing_bikes(fleet),
                      amortization=logic.amortization_total(fleet, own_batteries),
                      idle_by_location=idle_by_location(fleet),
                      claims=await crm.pending_claims(), rentals=rows,
                      expiring=expiring, before_days=cfg.remind_before_days,
                      forecast=logic.forecast_summary(bikes_by.get("available", 0), soon),
                      debtors=await crm.debtors(10),
                      month=month_totals, chart=chart,
                      chart_total=logic.cumulative(chart["days"]),
                      chart_view=request.query_params.get("chart") or "days",
                      # Доля баллов от оплат: «0,1 %» - это скидка,
                      # «20 %» - уже бизнес-модель, и это видно сразу.
                      bonus_share=logic.bonus_totals(
                          [{"kind": "all", "amount": month_totals.get("bonus", 0)}],
                          month_totals.get("payment", 0))["share"])

    # ─────────────────── инструменты списков ───────────────────

    def list_tools(request: Request, rows: list[dict], *,
                   allowed: dict[str, str]) -> dict:
        """Сортировка, страница и подвал - одинаково для всех списков."""
        p = request.query_params
        sort = p.get("sort") or ""
        direction = p.get("dir") or "asc"
        ordered = logic.sort_rows(rows, sort, direction, allowed=allowed) \
            if sort in allowed else list(rows)
        page = logic.page_of(ordered, logic.check_list_size(p.get("rows")),
                             p.get("page"))
        return {**page, "all_rows": ordered, "sort": sort, "dir": direction,
                "query": clean_query(request, drop=("page",))}

    def clean_query(request: Request, *, drop: tuple[str, ...] = ()) -> str:
        """Строка запроса без указанных параметров - для ссылок сортировки."""
        keep = [(k, v) for k, v in request.query_params.multi_items()
                if k not in drop]
        return "&".join(f"{k}={quote(str(v), safe='')}" for k, v in keep if v != "")

    async def views_of(request: Request, section: str) -> list[dict]:
        """Свои фильтры этого списка. Чужие не показываются."""
        staff = getattr(request.state, "staff", None) or {}
        if not staff.get("id"):
            return []
        return await crm.saved_views(int(staff["id"]), section)

    @app.post("/views")
    async def view_save(request: Request) -> Response:
        """Сохранить текущий набор фильтров под именем."""
        staff = getattr(request.state, "staff", None) or {}
        data = await form(request)
        section = str(data.get("section") or "")
        back = section + (("?" + str(data.get("query") or ""))
                          if data.get("query") else "")
        if not staff.get("id") or not section.startswith("/"):
            return redirect("/")
        name = logic.check_name(data.get("name"), what="Название фильтра")
        if not name.ok:
            flash(request, name.error, "err")
            return redirect(back)
        await crm.save_view(staff_id=int(staff["id"]), section=section,
                            name=name.value, query=str(data.get("query") or ""))
        flash(request, f"Фильтр «{name.value}» сохранён.")
        return redirect(back)

    @app.post("/views/{view_id}/delete")
    async def view_delete(request: Request, view_id: int) -> Response:
        staff = getattr(request.state, "staff", None) or {}
        data = await form(request)
        view = await crm.saved_view(view_id)
        back = str(view["section"]) if view else "/"
        if not staff.get("id") or not await crm.drop_saved_view(
                view_id, staff_id=int(staff["id"])):
            flash(request, "Такого фильтра у вас нет.", "err")
            return redirect(back)
        del data
        flash(request, "Фильтр убран.")
        return redirect(back)

    async def standing_bikes(fleet: list[dict], limit: int = 5) -> list[dict]:
        """Велосипеды, которые стоят дольше всех, с ценой простоя."""
        since = await crm.bike_status_since()
        now = datetime.now(UTC)
        rows = []
        for bike in fleet:
            if bike.get("status") not in logic.IDLE_STATUSES:
                continue
            days = logic.idle_days(since.get(bike["id"]), now=now) or 0
            rows.append({**bike, "idle_days": days, "lost": logic.idle_cost(days)})
        rows.sort(key=lambda b: (-b["idle_days"], str(b.get("code") or "")))
        return rows[:limit]

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
        repair = count_field(data, "plan_repair", what="Норма ремонта",
                             default="0", limit=9999)
        spare = count_field(data, "plan_spare", what="Норма подменных",
                            default="0", limit=9999)
        free = count_field(data, "plan_free", what="Норма свободных",
                           default="0", limit=9999)
        for field in (rented, check, repair, spare, free):
            if not field.ok:
                flash(request, field.error, "err")
                return redirect("/")
        await crm.set_setting("plan_rented", str(rented.value), by=who(request))
        await crm.set_setting("plan_check", str(check.value), by=who(request))
        await crm.set_setting("plan_repair", str(repair.value), by=who(request))
        await crm.set_setting("plan_spare", str(spare.value), by=who(request))
        await crm.set_setting("plan_free", str(free.value), by=who(request))
        flash(request, "План на месяц сохранён.")
        return redirect("/")

    async def tell_parts_arrived(orders: list[dict]) -> None:
        """В служебный чат: пришла запчасть, которую ждал наряд."""
        if not orders or bot is None or not cfg.contract_chat_id:
            return
        if not await notices.allowed(crm, "part_arrived"):
            return
        lines = ["📦 Пришла запчасть — наряды могут ехать дальше:"]
        lines += [f"• {o.get('no')} — {o.get('bike_code') or o.get('object_note') or '—'}"
                  for o in orders[:10]]
        try:
            await bot.send_message(cfg.contract_chat_id, "\n".join(lines))
        except Exception as err:                         # noqa: BLE001
            log.warning("сообщение о приходе запчасти не ушло: %s", err)
            await notices.record(crm, "part_arrived", status="failed",
                                 detail=str(err))
            return
        await notices.record(crm, "part_arrived", status="sent")

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

    @app.get("/clients.{ext}")
    async def clients_csv(request: Request, ext: str) -> Response:
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
        return _table(ext, "clients",
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
        employer = logic.check_employer(data.get("employer"))
        experience = logic.check_experience(data.get("experience"))
        for check in (name, note, status, contract, channel, employer, experience):
            if not check.ok:
                flash(request, check.error, "err")
                return None
        if phone is None:
            flash(request, "Телефон: не похоже на номер. Пример: +7 900 123-45-67.", "err")
            return None
        # Запасные телефоны: необязательны, но если вписаны - это номера.
        spare: dict[str, str | None] = {}
        for key in ("phone2", "phone3"):
            raw = (data.get(key) or "").strip()
            if not raw:
                spare[key] = None
                continue
            normal = bot_logic.normalize_phone(raw)
            if normal is None:
                flash(request, f"Запасной телефон «{raw}»: не похоже на номер.", "err")
                return None
            spare[key] = normal
        other = await crm.client_by_phone(phone)
        if other is not None and (current is None or other["id"] != current["id"]):
            flash(request, f"Этот телефон уже у клиента «{other['full_name']}».", "err")
            return None
        # MAX-аккаунт руками: мост из MAX-бота проставляет его сам, но
        # мост поднят не у всех, а рассылке нужен адрес получателя.
        raw_max = (data.get("max_id") or "").strip()
        if raw_max and not raw_max.isdigit():
            flash(request, "MAX id: только цифры, как в кабинете MAX.", "err")
            return None
        fields = {"full_name": name.value, "phone": phone, "note": note.value,
                  "status": status.value, "contract_no": contract.value,
                  "channel": channel.value, **spare,
                  "employer": employer.value, "experience": experience.value}
        if current is not None:
            fields["max_id"] = int(raw_max) if raw_max else None
        return fields

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
                      bot_user=bot_user, has_contract=has_contract,
                      signings=await crm.sign_requests(client_id=client_id,
                                                       limit=20),
                      # Подсказка суммы счёта - ровно долг: чаще всего
                      # выставляют его, и набирать заново незачем.
                      pay_hint=(str(-logic.to_money(balance))
                                if logic.to_money(balance) < 0 else ""),
                      pay_orders=await crm.pay_orders(client_id=client_id,
                                                      limit=10),
                      bonuses=await crm.bonuses(client_id=client_id, limit=20))

    @app.post("/clients/{client_id}/edit")
    async def client_edit(request: Request, client_id: int) -> Response:
        client = await crm.client(client_id)
        if client is None:
            return render(request, "missing.html", status_code=404, what="Клиент")
        data = await form(request)
        fields = await _client_fields(request, data, current=client)
        if fields is not None:
            try:
                await crm.update_client(client_id, **fields)
            except Exception as exc:                    # noqa: BLE001
                if "unique" in type(exc).__name__.lower():
                    flash(request, "Этот MAX-аккаунт уже привязан к другому "
                                   "клиенту.", "err")
                    return redirect(f"/clients/{client_id}")
                raise
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
            await notices.send_client(
                crm, "pay_credited", client["id"],
                lambda: notify.payment_credited(bot, db, crm, client,
                                                amount.value))
            await referral_bonus(client, amount.value, who(request))
        flash(request, "Запись добавлена.")
        # С карточки аренды платёж принимают, не уходя с неё.
        nxt = data.get("next") or ""
        if nxt.startswith("/") and not nxt.startswith("//"):
            return redirect(nxt)
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

    # Колонки, по которым можно сортировать список. Белым списком, а не
    # именем поля из адреса: имя поля из запроса - это чужая строка.
    BIKE_SORTS = {"code": "code", "model": "model", "status": "status",
                  "location": "location", "mileage": "mileage_km",
                  "client": "full_name", "idle": "idle_days"}

    @app.get("/bikes")
    async def bikes(request: Request) -> Response:
        q = request.query_params.get("q") or ""
        status = request.query_params.get("status") or ""
        location = request.query_params.get("location") or ""
        rows = await crm.bikes(q=q or None, status=status or None,
                               location=location or None, limit=10000)
        since = await crm.bike_status_since()
        now = datetime.now(UTC)
        for bike in rows:
            bike["idle_days"] = (logic.idle_days(since.get(bike["id"]), now=now)
                                 if bike.get("status") in logic.IDLE_STATUSES
                                 else None)
        tools = list_tools(request, rows, allowed=BIKE_SORTS)
        return render(request, "bikes.html", q=q, status=status, location=location,
                      rows=tools["rows"], tools=tools,
                      views=await views_of(request, "/bikes"),
                      counts=await crm.bike_counts())

    @app.get("/bikes.{ext}")
    async def bikes_csv(request: Request, ext: str) -> Response:
        """Парк файлом. Фильтры те же, что на экране: выгружают то, что
        видят, а не «всё вообще»."""
        if not may_view(request, "bikes"):
            return denied(request, "bikes")
        rows = await crm.bikes(q=request.query_params.get("q") or None,
                               status=request.query_params.get("status") or None,
                               location=request.query_params.get("location") or None,
                               limit=10000)
        money_ok = may_view(request, "finance")
        header = ["Номер", "Модель", "Статус", "Точка", "Госномер", "Пробег, км",
                  "Номер рамы", "Клиент", "Заведён"]
        if money_ok:
            header.insert(6, "Цена покупки")
        out = []
        for b in rows:
            line = [b["code"], b["model"],
                    logic.BIKE_STATUSES.get(b["status"], b["status"]),
                    b.get("location"), b.get("plate_no"), b.get("mileage_km"),
                    b.get("frame_no"), b.get("full_name"), b.get("created_at")]
            if money_ok:
                line.insert(6, logic.to_money(b.get("purchase_price") or 0))
            out.append(line)
        return _table(ext, "bikes", header, out)

    @app.get("/bikes/new")
    async def bike_new(request: Request) -> Response:
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        return render(request, "bike_form.html", bike=None)

    def _bike_fields(request: Request, data: dict) -> dict | None:
        plate = logic.check_plate(data.get("plate_no"))
        if not plate.ok:
            flash(request, plate.error, "err")
            return None
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
                "plate_no": plate.value,
                "plate_ok": bool(data.get("plate_ok")),
                "tracker_ok": bool(data.get("tracker_ok")),
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
        # Новая техника заводится «на сборке», когда сверка требуется:
        # велосипед, попавший в выдачу сразу после накладной, - это
        # ровно то, ради чего сверку и заводили. Требование снято -
        # ведём себя как раньше и не мешаем.
        if logic.bike_check_settings(await crm.settings())["required"]:
            fields = {**fields, "status": "new"}
        bike_id = await crm.create_bike(by=who(request), **fields)
        flash(request, "Велосипед заведён на сборку: сверьте паспорт "
                       "и введите в эксплуатацию."
              if fields.get("status") == "new" else "Велосипед добавлен.")
        return redirect(f"/bikes/{bike_id}")

    @app.get("/bikes/{bike_id}")
    async def bike_card(request: Request, bike_id: int) -> Response:
        bike = await crm.bike(bike_id)
        if bike is None:
            return render(request, "missing.html", status_code=404, what="Велосипед")
        settings = await crm.settings()
        # Сколько он уже не заработал, пока стоит. Простаивающий велосипед
        # в списке - это строка, а в рублях - решение.
        since = await crm.bike_status_since()
        idle = (logic.idle_days(since.get(bike_id), now=datetime.now(UTC))
                if bike.get("status") in logic.IDLE_STATUSES else 0)
        # Трекер - по привязке crm.trackers, а не по ручной галочке: галочка
        # говорит «поставили», привязка - «работает и где».
        tracker = await crm.tracker_of_bike(bike_id)
        orders = await crm.work_orders(bike_id=bike_id, limit=20)
        for o in orders:
            o["days"] = logic.order_days(o, today=date.today())
        return render(request, "bike.html", bike=bike, log=await crm.bike_log(bike_id),
                      rentals=await crm.bike_rentals(bike_id),
                      status_log=await crm.bike_status_log(bike_id),
                      nodes=await crm.repair_nodes(),
                      order=await crm.open_order_of(bike_id),
                      orders=orders,
                      tracker=(logic.tracker_rows([tracker], settings=settings)[0]
                               if tracker else None),
                      catalogue=logic.catalogue_entry(await crm.bike_models(),
                                                      bike.get("model")),
                      passport=logic.bike_check_state(bike, settings),
                      idle_days=idle, idle_lost=logic.idle_cost(idle),
                      amortization=logic.amortization_month(bike))

    @app.post("/bikes/{bike_id}/check")
    async def bike_check(request: Request, bike_id: int) -> Response:
        """Сверка поля паспорта и ввод в эксплуатацию.

        Форма приходит multipart: к номеру на раме прикладывают снимок,
        когда владелец его потребовал.
        """
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        bike = await crm.bike(bike_id)
        if bike is None:
            return render(request, "missing.html", status_code=404, what="Велосипед")
        data = await request.form()
        action = str(data.get("action") or "")
        back = f"/bikes/{bike_id}"
        try:
            if action == "commission":
                await service.commission_bike(crm, bike, by=who(request))
                flash(request, f"Велосипед № {bike['code']} в обороте.")
            elif action == "clear":
                field = str(data.get("field") or "")
                if field not in logic.BIKE_PASSPORT:
                    flash(request, "Неизвестное поле паспорта.", "err")
                    return redirect(back)
                await crm.clear_bike_check(bike_id, field)
                flash(request, f"{logic.BIKE_PASSPORT[field]}: сверка снята.")
            else:
                field = str(data.get("field") or "")
                photo = await save_check_photo("bike", bike, field,
                                               data.get("photo"))
                await service.check_bike_field(crm, bike, field,
                                               by=who(request), photo=photo)
                flash(request, f"{logic.BIKE_PASSPORT.get(field, field)}: сверено.")
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
        return redirect(back)

    async def save_check_photo(prefix: str, row: dict, field: str,
                               upload: Any) -> str | None:
        """Снимок сверки на диск. Возвращает имя или None, если не прислали.

        Имя собираем сами из вида техники, её номера и поля: имя из
        браузера - это чужая строка, и «../../etc/passwd» в ней не шутка.
        Префикс разводит велосипед и батарею: номера у них свои, и без
        него батарея № 7 затёрла бы снимок велосипеда № 7.
        """
        filename = getattr(upload, "filename", "") or ""
        if not filename:
            return None
        raw = await upload.read()
        if not raw:
            return None
        if len(raw) > BIKE_PHOTO_MAX:
            raise service.ServiceError(
                f"Снимок больше {BIKE_PHOTO_MAX // (1024 * 1024)} МБ — "
                "сфотографируйте меньшим размером.")
        suffix = Path(filename).suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
            raise service.ServiceError("Снимок: только jpg, png или webp.")
        folder = Path(cfg.bike_photo_dir)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{prefix}-{int(row['id'])}-{field}{suffix}"
            (folder / name).write_bytes(raw)
        except OSError as err:
            log.warning("снимок сверки не сохранён: %s", err)
            raise service.ServiceError(
                "Снимок не сохранился — попробуйте ещё раз.") from err
        return name

    @app.get("/bikes/{bike_id}/photo/{field}")
    async def bike_photo(request: Request, bike_id: int, field: str) -> Response:
        """Снимок сверки. Имя берём из базы, а не из адреса: путь,
        собранный из параметра запроса, уводит куда угодно."""
        if not may_view(request, "bikes"):
            return denied(request, "bikes")
        bike = await crm.bike(bike_id)
        marks = (bike or {}).get("checked") or {}
        mark = marks.get(field) if isinstance(marks, dict) else None
        name = (mark or {}).get("photo") if isinstance(mark, dict) else None
        path = Path(cfg.bike_photo_dir) / str(name or "")
        if not name or not path.is_file():
            return render(request, "missing.html", status_code=404, what="Снимок")
        return FileResponse(path)

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
        # Пробег необязателен: в мастерскую велосипед иногда закатывают
        # с мёртвым дисплеем. Зато записанный здесь попадает в журнал
        # статусов тем же триггером - и «сколько накатал между ремонтами»
        # становится видно без аренды.
        mileage = logic.check_mileage(data.get("mileage"),
                                      current=bike.get("mileage_km"),
                                      required=False)
        if not status.ok or not note.ok or not mileage.ok:
            flash(request, (status.error or note.error or mileage.error), "err")
            return redirect(f"/bikes/{bike_id}")
        if bike.get("rental_id"):
            flash(request, "Велосипед в аренде: сначала закройте аренду.", "err")
            return redirect(f"/bikes/{bike_id}")
        if bike.get("status") == "new":
            # Иначе «Свободен» из выпадающего списка выпускал бы технику
            # в оборот мимо сверки - ровно то, что она и должна ловить.
            flash(request, "Велосипед на сборке: выпускает его кнопка "
                           "«Ввести в эксплуатацию», а не смена статуса.", "err")
            return redirect(f"/bikes/{bike_id}")
        # Пробег пишется ТЕМ ЖЕ обновлением, что и статус: триггер снимает
        # одометр со строки велосипеда, и отдельный апдейт записал бы
        # в журнал старое число.
        await crm.update_bike(
            bike_id, by=who(request), status=status.value,
            **({"mileage_km": mileage.value} if mileage.value is not None else {}))
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
        all_tariffs = await crm.tariffs(active_only=True)
        aliases = logic.model_aliases(await crm.bike_models())
        ctx["models"] = logic.model_availability(available)
        # Цена зависит от модели, поэтому плитки тарифов собираются под
        # выбранную: пока модели нет, показываем цены первой свободной -
        # пустой экран «выберите модель» оператору ничего не даёт.
        picked_model = (ctx["model"] or (ctx["bike"] or {}).get("model")
                        or (ctx["models"][0]["model"] if ctx["models"] else ""))
        ctx["tariff_model"] = picked_model
        ctx["tariffs"] = logic.tariff_tiles(
            logic.tariffs_for_model(all_tariffs, picked_model, aliases=aliases))
        # Клиенту, который приедет завтра, можно обещать конкретный день:
        # прогноз считается по «оплачено до», а не по слову оператора.
        ctx["soon"] = logic.freeing_soon(await crm.active_rentals())
        if (p.get("tariff") or "").isdigit():
            ctx["tariff"] = await crm.tariff(int(p["tariff"]))
        if ctx["bike"] is not None:
            ctx["model"] = ctx["bike"]["model"]
        # Модель могли сменить последней: тариф берём того же срока, но
        # по цене выбранной модели. Иначе клиент платил бы за Kugoo
        # цену Monster Truck.
        if ctx["tariff"] is not None and ctx["model"]:
            fixed = logic.match_tariff(all_tariffs, ctx["tariff"], ctx["model"],
                                       aliases=aliases)
            if fixed is None:
                flash(request, f"Для модели «{ctx['model']}» нет тарифа на "
                               f"{ctx['tariff']['period_days']} дн. — "
                               "заведите его в тарифах.", "err")
                ctx["tariff"] = None
            elif int(fixed["id"]) != int(ctx["tariff"]["id"]):
                ctx["tariff"] = fixed
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
        fit = await crm.compat_for_bike_model(
            logic.catalogue_model(ctx["bike"]["model"], aliases))
        fit_ids = {m["id"] for m in fit}
        if fit_ids:
            free = [b for b in free if b.get("model_id") in fit_ids]
        # Основная батарея входит в цену велосипеда, каждая следующая -
        # платная позиция: курьер берёт её, чтобы не заряжаться в смену.
        ctx.update(batteries=logic.battery_options(free, all_tariffs,
                                                   tariff["period_days"]),
                   battery_slots=int(ctx["bike"].get("battery_count") or 0),
                   max_extra=logic.MAX_EXTRA_BATTERIES)
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
        # Последняя проверка перед деньгами: цена должна быть ценой этой
        # модели, а не той, с которой оператор начинал.
        all_tariffs = await crm.tariffs(active_only=True)
        aliases = logic.model_aliases(await crm.bike_models())
        fixed = logic.match_tariff(all_tariffs, tariff, bike.get("model"),
                                   aliases=aliases)
        if fixed is None:
            flash(request, f"Для модели «{bike.get('model')}» нет тарифа "
                           f"на {tariff['period_days']} дн.", "err")
            return redirect(back)
        tariff = fixed
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
        # Доп. аккумуляторы - платные позиции: цена считается до открытия
        # аренды, потому что начисляется цена периода целиком. Нет цены на
        # этот срок - отказ до денег, а не бесплатная батарея после.
        extra_ids = await form_ids(request, "extra_battery_ids")
        if len(extra_ids) > logic.MAX_EXTRA_BATTERIES:
            flash(request, f"Доп. аккумуляторов не больше "
                           f"{logic.MAX_EXTRA_BATTERIES} на аренду.", "err")
            return redirect(back)
        extras: list[dict] = []
        for battery_id in extra_ids:
            battery = await crm.battery(battery_id)
            if battery is None or battery.get("status") != "available":
                flash(request, "Доп. аккумулятор уже занят — обновите страницу.",
                      "err")
                return redirect(back)
            price = logic.battery_extra_price(all_tariffs, battery,
                                              tariff["period_days"])
            if price is None:
                flash(request, f"Нет тарифа на аккумулятор "
                               f"«{battery.get('model_title') or '—'}» на "
                               f"{tariff['period_days']} дн. — заведите цену "
                               "в тарифах.", "err")
                return redirect(back)
            extras.append({"kind": "battery", "battery_id": battery["id"],
                           "title": logic.extra_title("battery",
                                                      battery.get("model_title")),
                           "price": price})
        try:
            rental_id = await service.open_rental(
                crm, client=client, bike=bike, tariff=tariff, started_on=started.value,
                contract_no=contract_no, by=who(request), mileage=mileage.value,
                extras=extras)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(back)
        battery_ids = [*(await form_ids(request, "battery_ids")),
                       *(e["battery_id"] for e in extras)]
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
        # Пятый шаг мастера: документы и подпись. У них они собираются до
        # аренды, у нас - после: в договор и акт идёт номер велосипеда и
        # дата выдачи, а до открытия аренды их ещё нет. Оператору это
        # всё равно одна лента, а не поход в другой раздел.
        return redirect(f"/issue/docs?rental={rental_id}")

    @app.get("/issue/docs")
    async def issue_docs(request: Request) -> Response:
        """Шаг «документы»: пакет на подпись по только что открытой аренде."""
        if not may_edit(request, "rentals"):
            return denied(request, "rentals")
        raw = request.query_params.get("rental") or ""
        rental = await crm.rental(int(raw)) if raw.isdigit() else None
        if rental is None:
            return render(request, "missing.html", status_code=404, what="Аренда")
        client = await crm.client(rental["client_id"])
        if client is None:
            return render(request, "missing.html", status_code=404, what="Клиент")
        # Заявка на эту аренду уже может быть: оператор вернулся на шаг
        # назад или обновил страницу. Второй пакет на те же документы -
        # это два протокола на одну выдачу.
        rows = [r for r in await crm.sign_requests(client_id=client["id"], limit=20)
                if int(r.get("rental_id") or 0) == int(rental["id"])]
        row = next((r for r in rows if r["status"] != "cancelled"), None)
        problem = None
        if row is None:
            try:
                row = await service.start_signing(
                    crm, client=client, rental=rental, company=await sign_company(),
                    bot_user=await bot_user_for(client), by=who(request))
            except service.ServiceError as exc:
                problem = str(exc)
        code = request.session.pop("sign_code", None) if row else None
        return render(request, "issue.html", step=5, client=client, rental=rental,
                      req=row, problem=problem,
                      state=logic.sign_state(row) if row else None,
                      link=sign_link(request, row["token"]) if row else "",
                      code=(code or {}).get("code")
                      if row and (code or {}).get("id") == row["id"] else None,
                      bot_state=logic.bot_client_state(await bot_user_for(client)),
                      extras=logic.live_extras(
                          await crm.rental_extras(rental["id"])))

    # ─────────────────────── аренды ───────────────────────

    RENTAL_SORTS = {"no": "id", "client": "full_name", "bike": "bike_code",
                    "started": "started_on", "paid": "billed_until",
                    "debt": "balance", "tariff": "tariff_name",
                    "days": "days_running", "overdue": "overdue_days"}
    ORDER_SORTS = {"no": "no", "bike": "bike_code", "status": "status",
                   "payer": "payer", "client": "client_name",
                   "tech": "tech_name", "total": "total", "opened": "opened_at"}
    PART_SORTS = {"title": "title", "node": "node_title", "stock": "stock",
                  "cost": "cost", "price": "price", "days": "days_on_stock",
                  "cost_total": "cost_total", "price_total": "price_total",
                  "min": "min_stock", "model": "model"}

    def rental_rows(rows: list[dict], q: str = "",
                    open_orders: dict | None = None) -> list[dict]:
        """Аренды после поиска, со сводкой, сутками и просрочкой.

        «В ремонте» у аренды - это открытый наряд на велосипеде, который
        сейчас у клиента: статус велосипеда остаётся «в аренде» (его ставит
        и снимает только аренда), а вот наряд на нём - факт сервиса.
        """
        today = date.today()
        out = logic.rental_search(rows, q)
        for r in out:
            r["summary"] = summarize(r if r["status"] == "active" else None,
                                     r.get("balance", 0))
            r["overdue_days"] = logic.overdue_days(r["summary"])
            r["days_running"] = logic.rental_days(r, today=today)
            r["in_repair"] = bool(r["status"] == "active" and r.get("bike_id")
                                  and open_orders and r["bike_id"] in open_orders)
        return out

    def rental_view(rows: list[dict], view: str) -> list[dict]:
        """Фильтр вида поверх статуса.

        «Без техники» - не статус, а состояние: аренда идёт, а велосипеда
        на руках нет. Так бывает после замены, когда подменный уже забрали,
        а новый ещё не выдали, - и такую аренду видно только отсюда.
        """
        if view == "nobike":
            return [r for r in rows if r["status"] == "active" and not r.get("bike_id")]
        if view == "debt":
            return [r for r in rows if logic.to_money(r.get("balance")) < 0]
        if view == "search":
            return [r for r in rows if logic.in_search(r)]
        if view == "overdue":
            return [r for r in rows if r["overdue_days"] > 0]
        if view == "repair":
            return [r for r in rows if r["in_repair"]]
        return rows

    def rental_counts(rows: list[dict]) -> dict[str, int]:
        return {"nobike": sum(1 for r in rows if r["status"] == "active"
                              and not r.get("bike_id")),
                "debt": sum(1 for r in rows if logic.to_money(r.get("balance")) < 0),
                "search": sum(1 for r in rows if logic.in_search(r)),
                "overdue": sum(1 for r in rows if r["overdue_days"] > 0),
                "repair": sum(1 for r in rows if r["in_repair"])}

    @app.get("/rentals")
    async def rentals(request: Request) -> Response:
        status = request.query_params.get("status") or "active"
        view = request.query_params.get("view") or ""
        q = request.query_params.get("q") or ""
        rows = rental_rows(await crm.rentals(status=status if status != "all" else None),
                           q, await crm.open_orders_by_bike())
        shown = rental_view(rows, view)
        tools = list_tools(request, shown, allowed=RENTAL_SORTS)
        return render(request, "rentals.html", rows=tools["rows"], tools=tools,
                      status=status, view=view, q=q,
                      views=await views_of(request, "/rentals"),
                      debt_total=logic.sum_of(
                          [r for r in tools["all_rows"]
                           if logic.to_money(r.get("balance")) < 0], "balance"),
                      overdue_total=sum(1 for r in tools["all_rows"]
                                        if r["overdue_days"] > 0),
                      # Счётчики - по найденному: чипы отвечают на «сколько
                      # из этих», а не «сколько вообще».
                      counts=rental_counts(rows))

    @app.get("/rentals.{ext}")
    async def rentals_csv(request: Request, ext: str) -> Response:
        if not may_view(request, "rentals"):
            return denied(request, "rentals")
        status = request.query_params.get("status") or "active"
        rows = rental_view(
            rental_rows(await crm.rentals(status=status if status != "all" else None),
                        request.query_params.get("q") or "",
                        await crm.open_orders_by_bike()),
            request.query_params.get("view") or "")
        money_ok = may_view(request, "finance")
        header = ["Аренда", "Клиент", "Телефон", "Велосипед", "Тариф",
                  "Начало", "Идёт, дн.", "Оплачено до", "Просрочка, дн.",
                  "Статус", "Договор"]
        if money_ok:
            header.insert(9, "Баланс")
        out = []
        for r in rows:
            line = [r["id"], r.get("full_name"), r.get("phone"),
                    r.get("bike_code"), r.get("tariff_name"), r.get("started_on"),
                    r["days_running"],
                    (r["summary"] or {}).get("covered_until"),
                    r["overdue_days"],
                    logic.RENTAL_STATUSES.get(r["status"], r["status"]),
                    r.get("contract_no")]
            if money_ok:
                line.insert(9, logic.to_money(r.get("balance") or 0))
            out.append(line)
        return _table(ext, "rentals", header, out)

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
                      order=(await crm.open_order_of(rental["bike_id"])
                             if rental.get("bike_id") else None),
                      days_running=logic.rental_days(rental, today=date.today()),
                      remind_kind=logic.manual_reminder_kind(summary),
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
                      free_batteries=logic.battery_options(
                          await crm.batteries(status="available", limit=500),
                          await crm.tariffs(active_only=True),
                          rental.get("period_days")),
                      extras=await crm.rental_extras(rental_id),
                      extras_total=logic.extras_total(
                          await crm.rental_extras(rental_id, live_only=True)),
                      max_extra=logic.MAX_EXTRA_BATTERIES)

    @app.post("/rentals/{rental_id}/extras")
    async def rental_extra_add(request: Request, rental_id: int) -> Response:
        """Доп. аккумулятор в идущую аренду - платной позицией.

        Новая цена действует со следующего начисления: текущий период уже
        начислен, и менять клиенту сумму после того, как он её увидел,
        нельзя.
        """
        if not may_edit(request, "rentals"):
            return denied(request, "rentals")
        rental = await crm.rental(rental_id)
        if rental is None:
            return render(request, "missing.html", status_code=404, what="Аренда")
        data = await form(request)
        raw = data.get("battery_id") or ""
        battery = await crm.battery(int(raw)) if raw.isdigit() else None
        if battery is None:
            flash(request, "Выберите аккумулятор.", "err")
            return redirect(f"/rentals/{rental_id}")
        try:
            price = await service.add_battery_extra(
                crm, rental, battery, tariffs=await crm.tariffs(active_only=True),
                by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/rentals/{rental_id}")
        flash(request, f"Доп. аккумулятор № {battery['code']} выдан: "
                       f"+{logic.money(price)} к периоду со следующего начисления.")
        return redirect(f"/rentals/{rental_id}")

    @app.post("/rentals/{rental_id}/extras/{extra_id}")
    async def rental_extra_drop(request: Request, rental_id: int,
                                extra_id: int) -> Response:
        if not may_edit(request, "rentals"):
            return denied(request, "rentals")
        rental = await crm.rental(rental_id)
        extra = await crm.rental_extra(extra_id)
        if rental is None or extra is None:
            return render(request, "missing.html", status_code=404, what="Позиция")
        data = await form(request)
        status = data.get("status") or "available"
        if status not in logic.BATTERY_STATUSES:
            status = "available"
        try:
            await service.drop_battery_extra(crm, rental, extra, by=who(request),
                                             status=status)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/rentals/{rental_id}")
        flash(request, "Позиция снята, аккумулятор принят. "
                       "Цена периода уменьшится со следующего начисления.")
        return redirect(f"/rentals/{rental_id}")

    @app.post("/rentals/{rental_id}/remind")
    async def rental_remind(request: Request, rental_id: int) -> Response:
        """Напоминание клиенту по кнопке: тот же текст, что шлёт расписание,
        но сейчас и мимо тумблера - оператор нажал сам."""
        data = await form(request)
        nxt = data.get("next") or ""
        back = nxt if nxt.startswith("/") and not nxt.startswith("//") else f"/rentals/{rental_id}"
        rental = await crm.rental(rental_id)
        if rental is None or rental["status"] != "active":
            flash(request, "Аренда не идёт - напоминать не о чем.", "err")
            return redirect(back)
        if not rental.get("tg_id"):
            flash(request, f"{rental['full_name']}: клиента нет в боте, "
                           "напоминание отправить некуда - позвоните.", "err")
            return redirect(back)
        if bot is None:
            flash(request, "Бот не подключён к панели.", "err")
            return redirect(back)
        kind = logic.manual_reminder_kind(summarize(rental, rental.get("balance", 0)))
        sent = await billing.send_reminder(bot, db, crm, rental, kind=kind,
                                           today=date.today(), manual=True)
        flash(request, f"{rental['full_name']}: напоминание отправлено." if sent
              else f"{rental['full_name']}: не доставлено - клиент заблокировал бота?",
              "ok" if sent else "err")
        return redirect(back)

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
        rows = await crm.tariffs()
        # Порядок строки в таблице: сначала запасные «любая модель», дальше
        # модели по алфавиту, внутри модели - по сроку. Иначе одинаковые
        # тарифы разных моделей стоят вперемешку и цены не сравнить.
        rows.sort(key=lambda t: (str(t.get("model") or "").lower(),
                                 int(t.get("period_days") or 0)))
        models = await crm.bike_models(active_only=True)
        batteries = await crm.battery_models(active_only=True)
        # Модели, у которых нет ни одной своей цены: на выдаче они уедут
        # на запасной тариф, и это стоит видеть до выдачи, а не после.
        def unpriced(catalogue: list[dict], kind: str) -> list[str]:
            priced = {str(t.get("model") or "") for t in rows
                      if t.get("active") and (t.get("kind") or "bike") == kind}
            return [m["title"] for m in catalogue if m["title"] not in priced]

        def has_common(kind: str) -> bool:
            return any(not t.get("model") for t in rows
                       if t.get("active") and (t.get("kind") or "bike") == kind)

        return render(request, "tariffs.html",
                      groups=[
                          {"kind": "bike", "title": logic.TARIFF_KINDS["bike"],
                           "rows": [t for t in rows
                                    if (t.get("kind") or "bike") == "bike"],
                           "models": models, "unpriced": unpriced(models, "bike"),
                           "has_common": has_common("bike"),
                           "hint": "Цена велосипеда за период. Тариф без модели — "
                                   "запасной: он работает, пока у модели нет своей."},
                          {"kind": "battery", "title": logic.TARIFF_KINDS["battery"],
                           "rows": [t for t in rows
                                    if (t.get("kind") or "bike") == "battery"],
                           "models": batteries,
                           "unpriced": unpriced(batteries, "battery"),
                           "has_common": has_common("battery"),
                           "hint": "Цена доп. аккумулятора за тот же период, что "
                                   "и аренда. Нет цены на срок — доп. аккумулятор "
                                   "на этот срок не выдать."},
                      ])

    def _tariff_fields(request: Request, data: dict) -> dict | None:
        name = logic.check_name(data.get("name"), what="Название")
        period = logic.check_period(data.get("period_days"))
        price = logic.check_amount(data.get("price"))
        note = logic.check_note(data.get("note"))
        for check in (name, period, price, note):
            if not check.ok:
                flash(request, check.error, "err")
                return None
        kind = logic.check_tariff_kind(data.get("kind"))
        if not kind.ok:
            flash(request, kind.error, "err")
            return None
        return {"name": name.value, "period_days": period.value, "price": price.value,
                "note": note.value, "kind": kind.value,
                "model": (data.get("model") or "").strip() or None}

    @app.post("/tariffs")
    async def tariff_create(request: Request) -> Response:
        fields = _tariff_fields(request, await form(request))
        if fields is not None:
            try:
                await crm.create_tariff(**fields)
            except Exception as exc:                    # noqa: BLE001
                if "unique" in type(exc).__name__.lower():
                    flash(request, "Такой срок для этой модели уже есть — "
                                   "исправьте цену в существующем тарифе.", "err")
                    return redirect("/tariffs")
                raise
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
            try:
                await crm.update_tariff(tariff_id, **fields)
            except Exception as exc:                    # noqa: BLE001
                if "unique" in type(exc).__name__.lower():
                    flash(request, "Такой срок для этой модели уже есть.", "err")
                    return redirect("/tariffs")
                raise
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

    @app.get("/finance.{ext}")
    async def finance_csv(request: Request, ext: str) -> Response:
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
        name = f"finance-{since.value:%Y%m%d}-{until.value:%Y%m%d}"
        return _table(ext, name, ["Дата", "Клиент", "Вид", "Сумма", "Период", "Способ",
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
        await notices.send_client(
                crm, "pay_credited", client["id"],
                lambda: notify.payment_credited(bot, db, crm, client,
                                                amount.value))
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

    @app.get("/reports/payback.{ext}")
    async def payback_csv(request: Request, ext: str) -> Response:
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
        name = f"payback-{data['since']:%Y%m%d}-{data['until']:%Y%m%d}"
        return _table(ext, name, ["Модель", "Великов", "Дней в аренде", "Чек/день",
                           "Оплачено", "Начислено", "Ремонт", "Работы клиентам",
                           "Амортизация", "Маржа", "Маржа %"], rows)

    async def period_of(request: Request) -> dict:
        """Период отчёта: как в финансах - с начала месяца по сегодня."""
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
        # содержать сегодняшние наряды.
        end = datetime.combine(until.value + timedelta(days=1),
                               datetime.min.time(), tzinfo=tz)
        return {"since": since.value, "until": until.value,
                "start": start, "end": end,
                "days": (until.value - since.value).days + 1}

    @app.get("/reports/techs")
    async def techs_report(request: Request) -> Response:
        """Выработка техников: кто сколько закрыл и на сколько."""
        if not may_view(request, "service"):
            return denied(request, "service")
        span = await period_of(request)
        rows = logic.tech_rows(await crm.tech_work(span["start"], span["end"]))
        return render(request, "techs.html", rows=rows,
                      total=logic.tech_total(rows), **span)

    @app.get("/reports/techs.{ext}")
    async def techs_csv(request: Request, ext: str) -> Response:
        if not may_view(request, "service"):
            return denied(request, "service")
        span = await period_of(request)
        rows = logic.tech_rows(await crm.tech_work(span["start"], span["end"]))
        total = logic.tech_total(rows)
        data = [[r["tech"], r["orders"], r["client_orders"], r["avg_days"],
                 r["total"], r["cost"], r["works"], r["avg_total"]] for r in rows]
        data.append(["ИТОГО", total["orders"], total["client_orders"], "",
                     total["total"], total["cost"], total["works"],
                     total["avg_total"]])
        return _table(ext, f"techs-{span['since']:%Y%m%d}-{span['until']:%Y%m%d}",
                    ["Техник", "Нарядов", "Из них клиентских", "Средн. суток",
                     "Сумма", "Запчасти", "Работы", "Средний наряд"], data)

    @app.get("/reports/model-parts")
    async def model_parts_report(request: Request) -> Response:
        """Траты по моделям: какая модель дороже всех в запчастях."""
        if not may_view(request, "service"):
            return denied(request, "service")
        span = await period_of(request)
        rows = logic.model_parts_rows(
            await crm.model_parts(span["start"], span["end"]),
            await crm.bikes(limit=10000), days=span["days"])
        return render(request, "model_parts.html", rows=rows,
                      total=logic.spend_total(rows), **span)

    @app.get("/reports/spend")
    async def spend_report(request: Request) -> Response:
        """Расход склада за период: что уходит и на сколько."""
        if not may_view(request, "inventory"):
            return denied(request, "inventory")
        span = await period_of(request)
        rows = logic.spend_rows(await crm.part_spend(span["start"], span["end"]))
        return render(request, "spend.html", rows=rows,
                      total=logic.spend_total(rows), **span)

    @app.get("/reports/spend.{ext}")
    async def spend_csv(request: Request, ext: str) -> Response:
        if not may_view(request, "inventory"):
            return denied(request, "inventory")
        span = await period_of(request)
        rows = logic.spend_rows(await crm.part_spend(span["start"], span["end"]))
        total = logic.spend_total(rows)
        data = [[r["title"], r["node_title"], r["qty"], r["unit"], r["orders"],
                 r["cost"]] for r in rows]
        data.append(["ИТОГО", "", total["qty"], "", "", total["cost"]])
        return _table(ext, f"spend-{span['since']:%Y%m%d}-{span['until']:%Y%m%d}",
                    ["Позиция", "Узел", "Ушло", "Ед.", "Нарядов", "Себестоимость"],
                    data)

    async def integrity_data() -> list[dict]:
        """Расхождения между парком, арендами и нарядами."""
        return logic.integrity_issues(
            await crm.bikes(limit=10000), await crm.active_rentals(),
            await crm.open_orders_by_bike(), await crm.debtors(200),
            batteries=await crm.batteries(limit=10000))

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

    @app.get("/reports/channels.{ext}")
    async def channels_csv(request: Request, ext: str) -> Response:
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
        return _table(ext, "channels", header, rows)

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
        raw = await crm.settings()
        grants = await crm.bonuses(since=since.value, until=until.value, limit=2000)
        return render(request, "referrals.html", rows=rows,
                      funnel=logic.ref_funnel(rows), agents=logic.ref_agents(rows),
                      settings=logic.bonus_settings(raw),
                      links=logic.review_links(raw),
                      bonuses=grants,
                      totals=logic.bonus_totals(
                          grants, await crm.payments_total(since=since.value,
                                                           until=until.value)),
                      free_bikes=str(raw.get("free_bikes_post", "0"))
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
        friend = cost_field(data, "friend_bonus")
        review = cost_field(data, "review_bonus")
        spike = count_field(data, "spike", what="Порог всплеска",
                            default=str(logic.REF_SPIKE_DEFAULT), limit=100)
        for check in (bonus, minimum, friend, review, spike):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/reports/referrals")
        by = who(request)
        await crm.set_setting("ref_enabled", "1" if data.get("enabled") else "0", by=by)
        await crm.set_setting("ref_bonus", str(bonus.value), by=by)
        await crm.set_setting("ref_min_payment", str(minimum.value), by=by)
        await crm.set_setting("ref_friend_bonus", str(friend.value), by=by)
        await crm.set_setting("review_bonus", str(review.value), by=by)
        await crm.set_setting("ref_new_only",
                              "1" if data.get("new_only") else "0", by=by)
        await crm.set_setting("ref_spike", str(spike.value), by=by)
        for key in logic.REVIEW_SITES:
            url = (data.get(key) or "").strip()
            if url and not url.startswith(("http://", "https://")):
                flash(request, f"{logic.REVIEW_SITES[key]}: ссылка должна "
                               "начинаться с http:// или https://", "err")
                return redirect("/reports/referrals")
            await crm.set_setting(key, url, by=by)
        await crm.set_setting("free_bikes_post", "1" if data.get("free_bikes") else "0",
                              by=by)
        flash(request, "Настройки программы сохранены.")
        return redirect("/reports/referrals")

    @app.post("/clients/{client_id}/bonus")
    async def client_bonus(request: Request, client_id: int) -> Response:
        """Баллы клиенту: за отзыв или руками.

        Отзыв проверяет человек по скриншоту: у площадок нет ни API, ни
        обязанности нам отвечать, и правило в коде здесь было бы враньём.
        """
        if not logic.can_act(request.state.staff, "money_edit"):
            return denied(request, "money_edit")
        client = await crm.client(client_id)
        if client is None:
            return render(request, "missing.html", status_code=404, what="Клиент")
        data = await form(request)
        back = f"/clients/{client_id}"
        try:
            if (data.get("action") or "") == "review":
                amount = await service.grant_review_bonus(
                    crm, client, by=who(request))
            else:
                got = cost_field(data, "amount")
                if not got.ok:
                    flash(request, got.error, "err")
                    return redirect(back)
                note = logic.check_note(data.get("note"))
                if not note.ok:
                    flash(request, note.error, "err")
                    return redirect(back)
                amount = await service.grant_manual_bonus(
                    crm, client, got.value, note=note.value or "", by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(back)
        flash(request, f"Начислено баллами: {logic.money(amount)}. "
                       "Это не платёж — в средний чек они не идут.")
        return redirect(back)

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
        q = request.query_params.get("q") or ""
        rows = logic.rows_search(
            logic.service_rows(bikes, await crm.open_orders_by_bike(), today=today),
            q, ("code", "model", "order_no", "tech", "client", "complaint"))
        tools = list_tools(request, rows, allowed=SERVICE_SORTS)
        settings = await crm.settings()
        counts = await crm.bike_counts()
        plan = logic.month_plan(settings, fleet=sum(
            counts.get(code, 0) for code in logic.OPERATIONAL_STATUSES))
        # График за месяц: по нему видно, ремонт у нас ровный или
        # скачет - и когда именно скакнул. Месяц листается стрелками.
        span = logic.month_bounds(
            logic.month_from(request.query_params.get("month"), today=today),
            today=today)
        chart = logic.repair_chart(
            await crm.bikes_in_status_by_day("repair", span["first"], span["today"]),
            norm=int(plan["repair"]))
        # Подменный фонд - на столе сервиса: это его резерв на замены.
        spares = [b for b in bikes if b.get("spare")
                  and b.get("status") in logic.OPERATIONAL_STATUSES]
        return render(request, "service.html", rows=tools["rows"], tools=tools, q=q,
                      summary=logic.service_summary(rows), spares=spares,
                      chart=chart, plan=plan, month=span["first"], span=span,
                      tiles=logic.fleet_tiles(
                          counts, plan,
                          spare=sum(1 for b in bikes if b.get("spare")
                                    and b.get("status") in logic.OPERATIONAL_STATUSES)),
                      orders=await crm.work_orders(open_only=True, limit=200))

    SERVICE_SORTS = {"bike": "code", "model": "model", "stage": "stage",
                     "days": "days", "lost": "lost", "order": "order_no",
                     "tech": "tech", "payer": "payer_title", "client": "client",
                     "estimate": "estimate"}

    @app.get("/service.{ext}")
    async def service_csv(request: Request, ext: str) -> Response:
        bikes = await crm.bikes(limit=10000)
        since = await crm.bike_status_since()
        today, now = date.today(), datetime.now(UTC)
        for bike in bikes:
            bike["idle_days"] = logic.idle_days(since.get(bike["id"]), now=now)
        rows = logic.rows_search(
            logic.service_rows(bikes, await crm.open_orders_by_bike(), today=today),
            request.query_params.get("q") or "",
            ("code", "model", "order_no", "tech", "client", "complaint"))
        money_ok = may_view(request, "finance")
        header = ["Велосипед", "Модель", "Этап", "Суток", "Наряд", "Техник",
                  "Чей ремонт", "Клиент", "Жалоба"]
        if money_ok:
            header[4:4] = ["Потеряно"]
            header.append("Смета")
        out = []
        for r in rows:
            line = [r["code"], r.get("model"), r["stage"], r["days"], r["order_no"],
                    r["tech"], r["payer_title"], r["client"], r["complaint"]]
            if money_ok:
                line[4:4] = [r["lost"]]
                line.append(r["estimate"])
            out.append(line)
        return _table(ext, "service", header, out)

    @app.get("/orders")
    async def orders_page(request: Request) -> Response:
        status = request.query_params.get("status") or ""
        payer = request.query_params.get("payer") or ""
        bike_q = request.query_params.get("bike") or ""
        # Наряды одного велосипеда - его история ремонтов целиком: с карточки
        # велосипеда сюда ведёт «все наряды».
        bike_filter = (await crm.bike(int(bike_q))
                       if bike_q.isdigit() else None)
        rows = await crm.work_orders(status=status or None, payer=payer or None,
                                     bike_id=bike_filter["id"] if bike_filter else None,
                                     limit=300)
        for order in rows:
            order["days"] = logic.order_days(order, today=date.today())
        # Итог «сколько за ремонт ещё не заплатили» считается по всем
        # закрытым клиентским нарядам, а не по видимой странице: иначе
        # он менялся бы от фильтра и ничего не значил.
        tools = list_tools(request, rows, allowed=ORDER_SORTS)
        return render(request, "orders.html", rows=tools["rows"], tools=tools,
                      bike_filter=bike_filter,
                      status=status, payer=payer,
                      views=await views_of(request, "/orders"),
                      total=logic.sum_of(tools["all_rows"], "total"),
                      unpaid=logic.orders_unpaid(
                          await crm.work_orders(payer="client", limit=1000)))

    @app.get("/orders.{ext}")
    async def orders_csv(request: Request, ext: str) -> Response:
        if not may_view(request, "service"):
            return denied(request, "service")
        rows = await crm.work_orders(
            status=request.query_params.get("status") or None,
            payer=request.query_params.get("payer") or None, limit=5000)
        money_ok = may_view(request, "finance")
        header = ["Наряд", "Открыт", "Объект", "Статус", "Плательщик", "Клиент",
                  "Техник", "Суток", "Закрыт", "Оплачен"]
        if money_ok:
            header.insert(8, "Сумма")
        today = date.today()
        out = []
        for o in rows:
            line = [o["no"], o.get("opened_at"),
                    o.get("bike_code") or o.get("object_note"),
                    logic.ORDER_STATUSES.get(o["status"], o["status"]),
                    logic.PAYERS.get(o["payer"], o["payer"]), o.get("client_name"),
                    o.get("tech_name"), logic.order_days(o, today=today),
                    o.get("closed_at"), o.get("paid_at")]
            if money_ok:
                line.insert(8, logic.to_money(
                    o.get("total") if o["status"] == "done" else o.get("estimate")))
            out.append(line)
        return _table(ext, "orders", header, out)

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
        invoices = await crm.work_order_invoices(order_id)
        return render(request, "order.html", order=order, items=items,
                      totals=logic.order_totals(items),
                      client_total=logic.order_totals_client(items),
                      estimate=logic.estimate_state(order),
                      invoice=logic.invoice_state(order, invoices),
                      invoices=invoices,
                      days=logic.order_days(order, today=date.today()),
                      types=await crm.work_types(active_only=True),
                      techs=await crm.staff_all(),
                      parts=logic.part_rows(await crm.parts(active_only=True), stocks),
                      may_stock=may_view(request, "inventory"))

    @app.post("/orders/{order_id}/estimate")
    async def order_estimate(request: Request, order_id: int) -> Response:
        """Смета: отправить клиенту или согласовать вживую."""
        if not may_edit(request, "service"):
            return denied(request, "service")
        order = await crm.work_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Наряд")
        action = (await form(request)).get("action") or "send"
        back = f"/orders/{order_id}"
        try:
            if action == "send":
                got = await service.send_estimate(crm, order, by=who(request),
                                                  bot=bot)
                flash(request, f"Смета на {logic.money(got['total'])} отправлена."
                      if got["sent"] else
                      "Смета собрана, но клиента нет в боте — "
                      "согласуйте вживую.", "ok" if got["sent"] else "err")
                if got["sent"]:
                    # Команде - сразу, а не сводкой через сутки: наряд
                    # встал, и кто-то должен знать, что ждём клиента.
                    await notices.send_team(
                        crm, bot, "estimate_waiting",
                        f"⏳ Наряд {order.get('no')} ждёт согласования: смета "
                        f"на {logic.money(got['total'])} ушла клиенту "
                        f"{order.get('client_name') or ''}.".replace("  ", " "),
                        cfg.contract_chat_id)
            elif action in ("agree", "decline"):
                # «Согласовать вживую»: клиент стоит рядом и сказал «да».
                # Пишем, кто именно согласовал - на спор «я такого не
                # заказывал» это ответ.
                await service.answer_estimate(crm, order, agree=action == "agree",
                                              by=who(request))
                flash(request, "Согласовано, наряд в работе."
                      if action == "agree" else "Отказ: наряд отменён.")
            else:
                flash(request, "Непонятное действие.", "err")
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
        return redirect(back)

    @app.post("/orders/{order_id}/invoice")
    async def order_invoice(request: Request, order_id: int) -> Response:
        """Счёт клиенту за ремонт со ссылкой на оплату."""
        if not may_edit(request, "service"):
            return denied(request, "service")
        order = await crm.work_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Наряд")
        try:
            invoice = await service.invoice_order(crm, order, by=who(request),
                                                  acquiring=await acquiring_live())
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/orders/{order_id}")
        if invoice.get("status") == "failed":
            flash(request, f"Счёт {invoice['no']} заведён, но ссылки нет: "
                           f"{invoice.get('error') or 'банк не ответил'}", "err")
        else:
            sent = await notices.send_client(
                crm, "repair_invoice", invoice["client_id"],
                lambda: notify.pay_link(bot, db, invoice))
            flash(request, f"Счёт {invoice['no']} на "
                           f"{logic.money(invoice['amount'])} "
                  + ("отправлен клиенту." if sent else
                     "готов — передайте ссылку клиенту сами."))
        return redirect(f"/orders/{order_id}")

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
        if order["status"] == "approve" and status.value != "approve":
            # Форма на согласовании статус не отдаёт, но запрос можно
            # послать и мимо неё: молча снять ожидание ответа нельзя.
            flash(request, "Наряд на согласовании: ответьте за клиента "
                           "или отправьте смету заново.", "err")
            return redirect(f"/orders/{order_id}")
        tech_id = int(data["tech_id"]) if (data.get("tech_id") or "").isdigit() else None
        fields: dict[str, Any] = {"status": status.value, "tech_id": tech_id,
                                  "estimate": estimate.value, "note": note.value}
        # Плательщик задавался при открытии и потом не менялся - а «наш»
        # ремонт, оказавшийся клиентским после разборки, приходилось
        # закрывать и заводить заново. Меняется, пока смета не ушла и
        # счёт не выставлен: после этого клиенту уже что-то обещали.
        payer = logic.check_choice(data.get("payer") or order["payer"], logic.PAYERS,
                                   what="Плательщик")
        if not payer.ok:
            flash(request, payer.error, "err")
            return redirect(f"/orders/{order_id}")
        phone = bot_logic.normalize_phone(data.get("client_phone"))
        if phone:
            found = await crm.client_by_phone(phone)
            if found is None:
                flash(request, f"Клиента с телефоном {phone} нет.", "err")
                return redirect(f"/orders/{order_id}")
            fields["client_id"] = found["id"]
        if payer.value != order["payer"]:
            if order.get("estimate_sent_at") or await crm.work_order_invoices(order_id):
                flash(request, "Плательщика не сменить: смета уже отправлена или "
                               "счёт выставлен.", "err")
                return redirect(f"/orders/{order_id}")
            if payer.value == "client" and not (fields.get("client_id")
                                                or order.get("client_id")
                                                or order.get("object_note")):
                flash(request, "Клиентский ремонт: укажите клиента по телефону.", "err")
                return redirect(f"/orders/{order_id}")
            fields["payer"] = payer.value
        await crm.update_work_order(order_id, **fields)
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
                await notices.send_client(
                    crm, "repair_ready", client["id"],
                    lambda: notify.repair_ready(bot, client, order,
                                                totals["total"]))
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

    WORK_SORTS = {"title": "title", "category": "category", "minutes": "minutes",
                  "price": "price", "used": "used"}

    @app.get("/work-types")
    async def work_types_page(request: Request) -> Response:
        q = request.query_params.get("q") or ""
        rows = logic.rows_search(await crm.work_types(), q, ("title", "category"))
        tools = list_tools(request, rows, allowed=WORK_SORTS)
        return render(request, "work_types.html", rows=tools["rows"], tools=tools,
                      q=q, can_manage=may_edit(request, "service"))

    @app.get("/work-types.{ext}")
    async def work_types_csv(request: Request, ext: str) -> Response:
        rows = logic.rows_search(await crm.work_types(),
                                 request.query_params.get("q") or "",
                                 ("title", "category"))
        return _table(ext, "work-types",
                      ["Наименование", "Категория", "Узел", "Время, мин",
                       "Цена клиенту", "Использований", "Статус"],
                      [[r["title"], r.get("category"),
                        logic.REPAIR_NODES.get(str(r.get("node") or ""), ""),
                        r.get("minutes"), logic.to_money(r.get("price") or 0),
                        r.get("used"), "активна" if r.get("active") else "выключена"]
                       for r in rows])

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
        current = await crm.work_type(type_id)
        # Категорию и узел завели при создании и потом не трогали - а
        # «Замена мотор-колеса» в «Электрике» вместо «Ходовой» портила
        # отчёт «что ломается» навсегда.
        category = logic.check_choice(data.get("category") or current["category"],
                                      logic.WORK_CATEGORIES, what="Категория")
        node = str(data.get("node") if "node" in data else current.get("node") or "")
        if node and node not in logic.REPAIR_NODES:
            flash(request, "Узел: только из справочника.", "err")
            return redirect("/work-types")
        for check in (title, minutes, price, category):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/work-types")
        await crm.update_work_type(type_id, title=title.value, price=price.value,
                                   minutes=minutes.value, category=category.value,
                                   node=node or None)
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
                              for code in company.ALL_FIELDS},
                      default_contact=texts.SUPPORT_CONTACT_URL,
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
        for code, label in company.ALL_FIELDS.items():
            # Контакт менеджера проверяется строже реквизита: это ссылка,
            # по которой пойдёт клиент, а не строка в шапке договора.
            check = (company.check_contact if code in company.CONTACT_FIELDS
                     else company.check_value)
            value, error = check(data.get(code))
            if error:
                flash(request, f"{label}: {error}.", "err")
                return redirect("/company")
            clean[code] = value
        for code, value in clean.items():
            await crm.set_setting(code, value, by=who(request))
        # Панель и бот читают одни и те же настройки: снимок в этом
        # процессе обновляем сразу, чтобы не ждать своего же TTL.
        company.set_snapshot(await crm.settings())
        flash(request, "Сохранено. Бот подхватит правку в течение "
                       "нескольких минут.")
        return redirect("/company")

    # ───────────────── справочники: точки, модели, совместимость ─────────────────

    @app.get("/locations")
    async def locations_page(request: Request) -> Response:
        if not may_view(request, "settings"):
            return denied(request, "settings")
        rows = await crm.locations()
        return render(request, "locations.html", rows=rows,
                      cities=logic.by_city(rows))

    def location_extra(data: dict) -> dict:
        """Телефон, режим и координаты пункта: по ним клиент находит точку,
        а карта - центр города, когда трекеров ещё нет."""
        def coord(name: str) -> float | None:
            raw = (data.get(name) or "").strip().replace(",", ".")
            try:
                value = float(raw) if raw else None
            except (TypeError, ValueError):
                return None
            return value if value is not None and -180 <= value <= 180 else None

        return {"public_title": (data.get("public_title") or "").strip() or None,
                "phone": (data.get("phone") or "").strip() or None,
                "hours": (data.get("hours") or "").strip() or None,
                "lat": coord("lat"), "lon": coord("lon")}

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
            await crm.create_location(
                name=name.value, city=city.value,
                address=(data.get("address") or "").strip() or None,
                note=note.value, **location_extra(data))
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Точка с таким названием уже есть.", "err")
                return redirect("/locations")
            raise
        flash(request, "Точка добавлена.")
        return redirect("/locations")

    @app.post("/locations/{location_id}")
    async def location_edit(request: Request, location_id: int) -> Response:
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        rows = [x for x in await crm.locations() if x["id"] == location_id]
        if not rows:
            return render(request, "missing.html", status_code=404, what="Точка")
        data = await form(request)
        note = logic.check_note(data.get("note"))
        city = logic.check_name(data.get("city") or rows[0]["city"], what="Город")
        for check in (note, city):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/locations")
        await crm.update_location(
            location_id, address=(data.get("address") or "").strip() or None,
            note=note.value, city=city.value, **location_extra(data))
        flash(request, "Точка сохранена.")
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
        tariffs = [t for t in await crm.tariffs(active_only=True) if t.get("model")]
        prices: dict[str, list] = {}
        for tariff in sorted(tariffs, key=lambda t: int(t["period_days"])):
            prices.setdefault(str(tariff["model"]), []).append(tariff)
        # Названия в парке и в каталоге связаны текстом: расхождение
        # стоит показать здесь, а не выяснять на выдаче.
        known = {m["title"] for m in bikes}
        park = {str(b.get("model") or "").strip()
                for b in await crm.bikes(limit=10000)}
        return render(request, "models.html", bike_models=bikes,
                      battery_models=batteries, prices=prices,
                      unknown_models=sorted(m for m in park if m and m not in known),
                      matrix=logic.compat_matrix(
                          [m for m in bikes if m["active"]],
                          [m for m in batteries if m["active"]],
                          await crm.compat_pairs()))

    def model_specs(data: dict) -> dict:
        """Характеристики модели из формы. Пустое поле - это «не знаем»,
        а не ноль: «максимальная скорость 0» хуже прочерка."""
        def number(name: str, cast: Any) -> Any:
            raw = (data.get(name) or "").strip().replace(",", ".")
            try:
                return cast(raw) if raw else None
            except (TypeError, ValueError):
                return None

        return {"weight_kg": number("weight_kg", Decimal),
                "speed_kmh": number("speed_kmh", int),
                "range_km": number("range_km", int),
                "charge_hours": number("charge_hours", Decimal),
                "motor_watt": number("motor_watt", int),
                "max_load_kg": number("max_load_kg", int),
                "wheel_size": (data.get("wheel_size") or "").strip() or None,
                "size_note": (data.get("size_note") or "").strip() or None,
                "photo_url": (data.get("photo_url") or "").strip() or None,
                "description": (data.get("description") or "").strip() or None}

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
        specs = model_specs(data)
        try:
            await crm.create_bike_model(
                title=title.value, brand=(data.get("brand") or "").strip() or None,
                factory_title=(data.get("factory_title") or "").strip() or None,
                battery_slots=slots.value, note=note.value, **specs)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Такая модель уже есть.", "err")
                return redirect("/models")
            raise
        flash(request, "Модель добавлена.")
        return redirect("/models")

    @app.post("/models/bikes/{model_id}")
    async def bike_model_edit(request: Request, model_id: int) -> Response:
        """Характеристики правятся после заведения: в первый раз их обычно
        переписывают с коробки, а коробка не всегда под рукой."""
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        if await crm.bike_model(model_id) is None:
            return render(request, "missing.html", status_code=404, what="Модель")
        data = await form(request)
        if (data.get("action") or "") == "toggle":
            model = await crm.bike_model(model_id)
            await crm.update_bike_model(model_id, active=not model["active"])
            flash(request, "Модель убрана в архив." if model["active"]
                  else "Модель вернулась в каталог.")
            return redirect("/models")
        note = logic.check_note(data.get("note"))
        if not note.ok:
            flash(request, note.error, "err")
            return redirect("/models")
        await crm.update_bike_model(model_id, note=note.value, **model_specs(data))
        flash(request, "Модель сохранена.")
        return redirect("/models")

    @app.post("/models/batteries/{model_id}")
    async def battery_model_edit(request: Request, model_id: int) -> Response:
        """Правка модели АКБ: цена и срок службы меняются, и амортизация
        батарей этой модели пересчитывается с ними - каталог и есть
        источник этих чисел."""
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        if await crm.battery_model(model_id) is None:
            return render(request, "missing.html", status_code=404, what="Модель АКБ")
        data = await form(request)
        if data.get("action") == "toggle":
            current = await crm.battery_model(model_id)
            await crm.update_battery_model(model_id, active=not current["active"])
            flash(request, "Модель убрана в архив." if current["active"]
                  else "Модель возвращена из архива.")
            return redirect("/models")
        title = logic.check_name(data.get("title"), what="Название модели")
        price = cost_field(data, "price")
        months = count_field(data, "service_months", what="Срок службы",
                             default="15", limit=240)
        volt = count_field(data, "voltage", what="Напряжение", default="0", limit=200)
        for check in (title, price, months, volt):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/models")
        capacity = cost_field(data, "capacity")
        try:
            await crm.update_battery_model(
                model_id, title=title.value,
                brand=(data.get("brand") or "").strip() or None,
                voltage=volt.value or None,
                capacity=capacity.value if capacity.ok and capacity.value else None,
                price=price.value, service_months=months.value or 15)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Модель АКБ с таким названием уже есть.", "err")
                return redirect("/models")
            raise
        flash(request, "Модель АКБ сохранена.")
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
        view = request.query_params.get("view") or ""
        rows = logic.battery_rows(await crm.batteries(
            status=status or None, q=q or None, location=location or None,
            in_search=view == "search"), since=await crm.battery_status_since())
        return render(request, "batteries.html", rows=rows,
                      summary=logic.battery_summary(
                          logic.battery_rows(await crm.batteries())),
                      status=status, q=q, location=location, view=view,
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
        volts = logic.check_volts(data.get("volts"))
        amps = logic.check_amp_hours(data.get("amp_hours"))
        for check in (code, note, price, bought, location, cycles, volts, amps):
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
                "cycles": cycles.value, "note": note.value,
                "volts": volts.value, "amp_hours": amps.value}

    @app.post("/batteries")
    async def battery_create(request: Request) -> Response:
        if not may_edit(request, "batteries"):
            return denied(request, "batteries")
        fields = await battery_fields(request, await form(request))
        if fields is None:
            return redirect("/batteries/new")
        # Новая батарея заводится «на сборке», если владелец требует
        # сверку: недособранную выдавать нечего. Требование снято -
        # заводится сразу свободной, как было раньше.
        checks = logic.bike_check_settings(await crm.settings())
        try:
            battery_id = await crm.create_battery(
                by=who(request), status="new" if checks["required"] else "available",
                **fields)
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
        row = logic.battery_rows([battery], since=await crm.battery_status_since())[0]
        return render(request, "battery.html", battery=row,
                      log=await crm.battery_status_log(battery_id),
                      models=await crm.battery_models(active_only=True),
                      locations=await location_names(),
                      checks=logic.battery_check_state(battery,
                                                       await crm.settings()),
                      amortization=logic.battery_amortization(battery))

    @app.post("/batteries/{battery_id}/check")
    async def battery_check(request: Request, battery_id: int) -> Response:
        """Сверка поля паспорта батареи и ввод её в эксплуатацию."""
        if not may_edit(request, "batteries"):
            return denied(request, "batteries")
        battery = await crm.battery(battery_id)
        if battery is None:
            return render(request, "missing.html", status_code=404, what="Батарея")
        data = await request.form()
        action = str(data.get("action") or "")
        back = f"/batteries/{battery_id}"
        try:
            if action == "commission":
                await service.commission_battery(crm, battery, by=who(request))
                flash(request, f"Аккумулятор № {battery['code']} в обороте.")
            elif action == "clear":
                field = str(data.get("field") or "")
                if field not in logic.BATTERY_PASSPORT:
                    flash(request, "Неизвестное поле паспорта.", "err")
                    return redirect(back)
                await crm.clear_battery_check(battery_id, field)
                flash(request, f"{logic.BATTERY_PASSPORT[field]}: сверка снята.")
            else:
                field = str(data.get("field") or "")
                photo = await save_check_photo("akb", battery, field,
                                               data.get("photo"))
                await service.check_battery_field(crm, battery, field,
                                                  by=who(request), photo=photo)
                flash(request, f"{logic.BATTERY_PASSPORT.get(field, field)}: сверено.")
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
        return redirect(back)

    @app.get("/batteries/{battery_id}/photo/{field}")
    async def battery_photo(request: Request, battery_id: int, field: str) -> Response:
        if not may_view(request, "batteries"):
            return denied(request, "batteries")
        battery = await crm.battery(battery_id)
        marks = (battery or {}).get("checked") or {}
        mark = marks.get(field) if isinstance(marks, dict) else None
        name = (mark or {}).get("photo") if isinstance(mark, dict) else None
        path = Path(cfg.bike_photo_dir) / str(name or "")
        if not name or not path.is_file():
            return render(request, "missing.html", status_code=404, what="Снимок")
        return FileResponse(path)

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
        if battery["status"] == "new":
            flash(request, "Батарея на сборке: из этого состояния её выводит "
                           "только кнопка «Ввести в эксплуатацию».", "err")
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

    # ───────────── подписание документов (ПЭП) ─────────────

    def sign_link(request: Request, token: str) -> str:
        """Ссылка для клиента - абсолютная: её отправляют в мессенджер."""
        return str(request.base_url).rstrip("/") + f"/sign/{token}"

    async def sign_company() -> dict:
        settings = await crm.settings()
        return {code: settings.get(code, "") for code in company.COMPANY_FIELDS}

    @app.get("/signings")
    async def signings_page(request: Request) -> Response:
        if not may_view(request, "clients"):
            return denied(request, "clients")
        rows = logic.sign_rows(await crm.sign_requests(limit=200))
        return render(request, "signings.html", rows=rows,
                      summary=logic.sign_summary(rows))

    @app.post("/clients/{client_id}/sign")
    async def sign_start(request: Request, client_id: int) -> Response:
        """Собрать пакет документов и ссылку на подписание."""
        if not may_edit(request, "clients"):
            return denied(request, "clients")
        client = await crm.client(client_id)
        if client is None:
            return render(request, "missing.html", status_code=404, what="Клиент")
        bot_user = await db.get_user(client["tg_id"]) if client.get("tg_id") else None
        try:
            created = await service.start_signing(
                crm, client=client, rental=await crm.active_rental_of(client_id),
                company=await sign_company(),
                bot_user=dict(bot_user) if bot_user else None, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/clients/{client_id}")
        flash(request, f"Заявка на подпись {created['no']} готова. "
                       "Отправьте клиенту ссылку и продиктуйте код, когда он "
                       "его запросит.")
        return redirect(f"/signings/{created['id']}")

    @app.get("/signings/{request_id}")
    async def sign_card(request: Request, request_id: int) -> Response:
        if not may_view(request, "clients"):
            return denied(request, "clients")
        row = await crm.sign_request(request_id)
        if row is None:
            return render(request, "missing.html", status_code=404, what="Заявка")
        return render(request, "signing.html", req=row,
                      state=logic.sign_state(row),
                      link=sign_link(request, row["token"]),
                      digest=logic.sign_docs_digest(row.get("docs") or []),
                      events=await crm.sign_events(request_id))

    @app.get("/signings/{request_id}/doc/{index}")
    async def sign_card_doc(request: Request, request_id: int, index: int) -> Response:
        """Файл пакета для оператора - тот же, что видит клиент по ссылке.

        Путь берётся из списка документов заявки, а не из запроса: по
        индексу нельзя дотянуться до чужого файла.
        """
        if not may_view(request, "clients"):
            return denied(request, "clients")
        row = await crm.sign_request(request_id)
        if row is None:
            return render(request, "missing.html", status_code=404, what="Заявка")
        docs = list(row.get("docs") or [])
        if not 0 <= index < len(docs) or not docs[index].get("path"):
            return render(request, "missing.html", status_code=404, what="Документ")
        path = Path(str(docs[index]["path"]))
        if not path.is_file():
            return render(request, "missing.html", status_code=404, what="Файл документа")
        return FileResponse(path, filename=f"{docs[index]['title']}{path.suffix}")

    @app.post("/signings/{request_id}/code")
    async def sign_code_send(request: Request, request_id: int) -> Response:
        """Код по просьбе оператора: клиент без Telegram узнаёт его
        голосом, по телефону."""
        if not may_edit(request, "clients"):
            return denied(request, "clients")
        row = await crm.sign_request(request_id)
        if row is None:
            return render(request, "missing.html", status_code=404, what="Заявка")
        try:
            code = await service.issue_sign_code(crm, row, ip=client_ip(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/signings/{request_id}")
        await notify.sign_code(bot, row, code)
        request.session["sign_code"] = {"id": request_id, "code": code}
        flash(request, f"Код {code} действует {logic.SIGN_CODE_MINUTES} минут."
                       + (" Он же ушёл клиенту в Telegram." if row.get("tg_id")
                          else " Клиент не в боте — продиктуйте код."))
        return redirect(f"/signings/{request_id}")

    @app.post("/signings/{request_id}/cancel")
    async def sign_cancel(request: Request, request_id: int) -> Response:
        if not may_edit(request, "clients"):
            return denied(request, "clients")
        row = await crm.sign_request(request_id)
        if row is None:
            return render(request, "missing.html", status_code=404, what="Заявка")
        if row["status"] == "signed":
            flash(request, "Подписанное не отменяется: заявка - это протокол.",
                  "err")
            return redirect(f"/signings/{request_id}")
        await crm.cancel_sign_request(request_id, by=who(request))
        flash(request, "Заявка отменена, ссылка больше не работает.")
        return redirect(f"/signings/{request_id}")

    # ─── страница клиента: без входа в панель, по токену из ссылки ───

    async def sign_by_token(token: str) -> dict | None:
        return await crm.sign_request_by_token(token)

    def agent_of(request: Request) -> str:
        return (request.headers.get("user-agent") or "")[:300]

    @app.get("/sign/{token}")
    async def sign_page(request: Request, token: str) -> Response:
        row = await sign_by_token(token)
        if row is None:
            return render(request, "sign_missing.html", status_code=404)
        state = logic.sign_state(row)
        if state["open"]:
            await crm.log_sign_event(row["id"], kind="opened",
                                     ip=client_ip(request), agent=agent_of(request))
        return render(request, "sign.html", req=row, state=state,
                      digest=logic.sign_docs_digest(row.get("docs") or []),
                      company=await sign_company())

    @app.get("/sign/{token}/agreement")
    async def sign_agreement(request: Request, token: str) -> Response:
        """Соглашение об ЭП - ровно тот текст, который подписывают."""
        row = await sign_by_token(token)
        if row is None:
            return render(request, "sign_missing.html", status_code=404)
        return render(request, "sign_agreement.html", req=row,
                      text=row.get("agreement") or "")

    @app.get("/sign/{token}/doc/{index}")
    async def sign_doc(request: Request, token: str, index: int) -> Response:
        """Файл из пакета. Отдаём только то, что лежит в самой заявке:
        путь приходит не из запроса, а из её списка документов."""
        row = await sign_by_token(token)
        if row is None:
            return render(request, "sign_missing.html", status_code=404)
        docs = list(row.get("docs") or [])
        if not 0 <= index < len(docs) or not docs[index].get("path"):
            return render(request, "sign_missing.html", status_code=404)
        path = Path(str(docs[index]["path"]))
        if not path.is_file():
            return render(request, "sign_missing.html", status_code=404)
        return FileResponse(path, filename=f"{docs[index]['title']}{path.suffix}")

    @app.post("/sign/{token}/code")
    async def sign_ask_code(request: Request, token: str) -> Response:
        row = await sign_by_token(token)
        if row is None:
            return render(request, "sign_missing.html", status_code=404)
        try:
            code = await service.issue_sign_code(crm, row, ip=client_ip(request),
                                                 agent=agent_of(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/sign/{token}")
        sent = await notify.sign_code(bot, row, code)
        flash(request, "Код отправлен в Telegram." if sent
              else "Код готов — позвоните оператору, он его продиктует.")
        return redirect(f"/sign/{token}")

    @app.post("/sign/{token}")
    async def sign_submit(request: Request, token: str) -> Response:
        row = await sign_by_token(token)
        if row is None:
            return render(request, "sign_missing.html", status_code=404)
        data = await form(request)
        try:
            await service.verify_sign(crm, row, data.get("code"),
                                      ip=client_ip(request),
                                      agent=agent_of(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/sign/{token}")
        flash(request, "Документы подписаны. Экземпляры остаются доступны "
                       "по этой ссылке.")
        return redirect(f"/sign/{token}")

    # ─────────────────────── рассылки ───────────────────────

    async def audience_people(code: str) -> list[dict]:
        return logic.pick_audience(code, await crm.clients_for_mailing(),
                                   await crm.active_rentals(),
                                   before_days=cfg.remind_before_days)

    @app.get("/mailing")
    async def mailing_page(request: Request) -> Response:
        if not may_view(request, "mailing"):
            return denied(request, "mailing")
        sizes = {code: len(await audience_people(code)) for code in logic.AUDIENCES}
        return render(request, "mailing.html",
                      rows=logic.campaign_rows(await crm.campaigns(limit=100)),
                      templates=await crm.templates(),
                      sizes=sizes)

    @app.post("/mailing/templates")
    async def template_save(request: Request) -> Response:
        """Новый шаблон или правка существующего."""
        if not may_edit(request, "mailing"):
            return denied(request, "mailing")
        data = await form(request)
        title = logic.check_name(data.get("title"), what="Название шаблона")
        body = logic.check_template_body(data.get("body"), what="Текст")
        raw_max = (data.get("body_max") or "").strip()
        body_max = (logic.check_template_body(raw_max, what="Текст для MAX")
                    if raw_max else logic.Check(True, None))
        note = logic.check_note(data.get("note"))
        for check in (title, body, body_max, note):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/mailing")
        template_id = int(data["id"]) if (data.get("id") or "").isdigit() else None
        if template_id:
            await crm.update_template(template_id, title=title.value,
                                      body=body.value, body_max=body_max.value,
                                      note=note.value)
            flash(request, "Шаблон сохранён.")
            return redirect("/mailing")
        code = logic.check_slug(data.get("code"), what="Код шаблона")
        if not code.ok:
            flash(request, code.error, "err")
            return redirect("/mailing")
        try:
            await crm.create_template(code=code.value, title=title.value,
                                      body=body.value, body_max=body_max.value,
                                      note=note.value)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Шаблон с таким кодом уже есть.", "err")
                return redirect("/mailing")
            raise
        flash(request, "Шаблон добавлен.")
        return redirect("/mailing")

    @app.post("/mailing")
    async def campaign_create(request: Request) -> Response:
        if not may_edit(request, "mailing"):
            return denied(request, "mailing")
        data = await form(request)
        title = logic.check_name(data.get("title"), what="Название рассылки")
        audience = logic.check_audience(data.get("audience"))
        note = logic.check_note(data.get("note"))
        for check in (title, audience, note):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/mailing")
        template = (await crm.template(int(data["template_id"]))
                    if (data.get("template_id") or "").isdigit() else None)
        if template is None:
            flash(request, "Выберите шаблон.", "err")
            return redirect("/mailing")
        try:
            created = await service.create_campaign(
                crm, title=title.value, template=template,
                audience=audience.value, note=note.value, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/mailing")
        flash(request, f"Черновик собран: {created['queued']} получателей. "
                       "Посмотрите список и запустите отправку.")
        return redirect(f"/mailing/{created['id']}")

    @app.get("/mailing/{campaign_id}")
    async def campaign_card(request: Request, campaign_id: int) -> Response:
        if not may_view(request, "mailing"):
            return denied(request, "mailing")
        campaign = await crm.campaign(campaign_id)
        if campaign is None:
            return render(request, "missing.html", status_code=404, what="Рассылка")
        sends = await crm.campaign_sends(campaign_id, limit=1000)
        preview = ""
        if sends:
            client = await crm.client(sends[0]["client_id"])
            if client is not None:
                values = logic.template_context(
                    client, await crm.active_rental_of(client["id"]),
                    await crm.client_balance(client["id"]),
                    pay_url=cfg.pay_url)
                preview = logic.render_template(campaign.get("body") or "", values)
        return render(request, "campaign.html", campaign=campaign, sends=sends,
                      progress=logic.campaign_progress(sends), preview=preview)

    @app.post("/mailing/{campaign_id}/start")
    async def campaign_start(request: Request, campaign_id: int) -> Response:
        if not may_edit(request, "mailing"):
            return denied(request, "mailing")
        campaign = await crm.campaign(campaign_id)
        if campaign is None:
            return render(request, "missing.html", status_code=404, what="Рассылка")
        try:
            await service.start_campaign(crm, campaign)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/mailing/{campaign_id}")
        flash(request, "Отправка началась. Сообщения уходят из процесса бота, "
                       "по несколько в секунду.")
        return redirect(f"/mailing/{campaign_id}")

    @app.post("/mailing/{campaign_id}/cancel")
    async def campaign_cancel(request: Request, campaign_id: int) -> Response:
        if not may_edit(request, "mailing"):
            return denied(request, "mailing")
        campaign = await crm.campaign(campaign_id)
        if campaign is None:
            return render(request, "missing.html", status_code=404, what="Рассылка")
        try:
            left = await service.cancel_campaign(crm, campaign)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/mailing/{campaign_id}")
        flash(request, f"Рассылка остановлена, снято из очереди: {left}. "
                       "Отправленное не отзывается — ни Telegram, ни MAX этого "
                       "не умеют.")
        return redirect(f"/mailing/{campaign_id}")

    # ─────────────────────── касса и банк ───────────────────────

    @app.get("/cash")
    async def cash_page(request: Request) -> Response:
        if not may_view(request, "cash"):
            return denied(request, "cash")
        rows = logic.shift_rows(await crm.cash_shifts(limit=100))
        current = await crm.open_shift()
        state = None
        if current is not None:
            state = logic.shift_state(current,
                                      await crm.shift_payments(current["id"]),
                                      await crm.cash_moves(current["id"]),
                                      other=await crm.shift_payments(current["id"],
                                                                     cash=False))
        # Что должно лежать в ящике при открытии - «насчитали» прошлой
        # смены на той же точке: открывать с нуля, не глядя, нельзя.
        previous = sorted(await crm.last_closed_shifts(),
                          key=lambda x: str(x.get("location") or ""))
        return render(request, "cash.html", rows=rows, current=current, state=state,
                      previous=previous, locations=await location_names())

    @app.post("/cash")
    async def cash_open(request: Request) -> Response:
        if not may_edit(request, "cash"):
            return denied(request, "cash")
        data = await form(request)
        opening = cost_field(data, "opening")
        note = logic.check_note(data.get("note"))
        location = logic.check_location(data.get("location"), await location_names())
        for check in (opening, note, location):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/cash")
        try:
            shift_id = await service.open_cash_shift(
                crm, location=location.value, opening=opening.value,
                note=note.value, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/cash")
        flash(request, "Смена открыта.")
        return redirect(f"/cash/{shift_id}")

    @app.get("/cash/{shift_id}")
    async def cash_shift_card(request: Request, shift_id: int) -> Response:
        if not may_view(request, "cash"):
            return denied(request, "cash")
        shift = await crm.cash_shift(shift_id)
        if shift is None:
            return render(request, "missing.html", status_code=404, what="Смена")
        payments = await crm.shift_payments(shift_id)
        other = await crm.shift_payments(shift_id, cash=False)
        moves = await crm.cash_moves(shift_id)
        return render(request, "cash_shift.html", shift=shift, payments=payments,
                      other=other, moves=moves,
                      state=logic.shift_state(shift, payments, moves, other=other))

    @app.post("/cash/{shift_id}/move")
    async def cash_move(request: Request, shift_id: int) -> Response:
        if not may_edit(request, "cash"):
            return denied(request, "cash")
        shift = await crm.cash_shift(shift_id)
        if shift is None:
            return render(request, "missing.html", status_code=404, what="Смена")
        data = await form(request)
        amount = logic.check_amount(data.get("amount"))
        kind = logic.check_cash_move(data.get("kind"))
        reason = logic.check_note(data.get("reason"))
        for check in (amount, kind, reason):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(f"/cash/{shift_id}")
        try:
            await service.cash_move(crm, shift, kind=kind.value, amount=amount.value,
                                    reason=reason.value, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/cash/{shift_id}")
        flash(request, f"{logic.CASH_MOVE_KINDS[kind.value]}: "
                       f"{logic.money(amount.value)}.")
        return redirect(f"/cash/{shift_id}")

    @app.post("/cash/{shift_id}/close")
    async def cash_close(request: Request, shift_id: int) -> Response:
        if not may_edit(request, "cash"):
            return denied(request, "cash")
        shift = await crm.cash_shift(shift_id)
        if shift is None:
            return render(request, "missing.html", status_code=404, what="Смена")
        data = await form(request)
        counted = cost_field(data, "counted")
        note = logic.check_note(data.get("note"))
        for check in (counted, note):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(f"/cash/{shift_id}")
        try:
            state = await service.close_cash_shift(
                crm, shift, counted=counted.value, note=note.value, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/cash/{shift_id}")
        if state["diff"]:
            sign = "излишек" if state["diff"] > 0 else "недостача"
            flash(request, f"Смена закрыта. Расхождение: {sign} "
                           f"{logic.money(abs(state['diff']))}.",
                  "err" if state["big_diff"] else "ok")
        else:
            flash(request, "Смена закрыта, касса сошлась.")
        return redirect(f"/cash/{shift_id}")

    @app.get("/bank")
    async def bank_page(request: Request) -> Response:
        if not may_view(request, "cash"):
            return denied(request, "cash")
        status = request.query_params.get("status") or "new"
        settings = await crm.settings()
        clients = await crm.clients(limit=10000)
        rows = logic.bank_rows(
            await crm.bank_txns(status=None if status == "all" else status, limit=200),
            clients, settings=settings)
        if status == "new":
            # Списания в «не разобрано» не показываем: разбирать в них
            # нечего, они никому не зачисляются и висели бы вечно.
            rows = [r for r in rows if r["direction"] == "credit"]
        # В выпадающем списке - те, кто платит: должники и те, у кого идёт
        # аренда. Весь список клиентов в select не помещается и не нужен:
        # платёж от закрывшегося год назад - повод открыть его карточку,
        # а не искать в двух сотнях строк.
        picks = {c["id"]: c for c in await crm.debtors(200)}
        for rental in await crm.active_rentals():
            picks.setdefault(rental["client_id"],
                             {"id": rental["client_id"],
                              "full_name": rental.get("full_name"),
                              "phone": rental.get("phone")})
        return render(request, "bank.html", rows=rows, status=status,
                      summary=logic.bank_summary(
                          logic.bank_rows(await crm.bank_txns(limit=500))),
                      auto=logic.bank_settings(settings)["auto_credit"],
                      clients_for_pick=sorted(
                          picks.values(), key=lambda c: str(c.get("full_name") or "")),
                      last_at=await crm.last_bank_txn_at())

    @app.post("/bank/settings")
    async def bank_settings(request: Request) -> Response:
        if not may_edit(request, "cash"):
            return denied(request, "cash")
        data = await form(request)
        await crm.set_setting("bank_auto_credit", "1" if data.get("auto") else "0",
                              by=who(request))
        flash(request, "Автозачисление включено: строки с номером договора "
                       "в назначении будут зачисляться сами."
              if data.get("auto") else "Автозачисление выключено.")
        return redirect("/bank")

    # Раньше /bank/{txn_id}: иначе «settings» уедет в числовой параметр.
    @app.post("/bank/{txn_id}")
    async def bank_handle(request: Request, txn_id: int) -> Response:
        """Зачислить поступление клиенту или отметить «не наш»."""
        if not may_edit(request, "cash"):
            return denied(request, "cash")
        txn = await crm.bank_txn(txn_id)
        if txn is None:
            return render(request, "missing.html", status_code=404,
                          what="Строка выписки")
        data = await form(request)
        if (data.get("action") or "") == "ignore":
            try:
                await service.ignore_bank_txn(crm, txn, by=who(request))
            except service.ServiceError as exc:
                flash(request, str(exc), "err")
                return redirect("/bank")
            flash(request, "Отмечено: платёж не наш.")
            return redirect("/bank")
        client = (await crm.client(int(data["client_id"]))
                  if (data.get("client_id") or "").isdigit() else None)
        if client is None:
            flash(request, "Выберите клиента, которому зачислить.", "err")
            return redirect("/bank")
        try:
            await service.credit_bank_txn(crm, txn, client, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/bank")
        await referral_bonus(client, logic.to_money(txn["amount"]), who(request))
        flash(request, f"{logic.money(txn['amount'])} зачислено: "
                       f"{client['full_name']}.")
        return redirect("/bank")

    # ─────────────────── документы: свой шаблон ───────────────────

    # Наши шаблоны лежат в образе бота; панель их только отдаёт на
    # скачивание, чтобы было с чего начинать свой.
    OUR_TEMPLATES = {
        "contract": Path("app/contract_template.docx"),
        "act_in": Path("app/act_priema_template.docx"),
        "act_out": Path("app/act_vozvrata_template.docx"),
        "buyout": Path("app/act_vykup_template.docx"),
        "consent": Path("app/soglasie_template.docx"),
    }

    def our_template(kind: str) -> Path | None:
        path = OUR_TEMPLATES.get(kind)
        if path is None:
            return None
        here = Path(__file__).resolve().parent.parent.parent / path
        return here if here.is_file() else None

    @app.get("/documents")
    async def documents_page(request: Request) -> Response:
        if not may_view(request, "settings"):
            return denied(request, "settings")
        stored = await crm.doc_templates()
        return render(request, "documents.html",
                      rows=logic.doc_rows(stored),
                      summary=logic.doc_summary(stored),
                      marks={m["kind"]: m for m in await crm.company_marks()},
                      ours={k: our_template(k) is not None
                            for k in logic.DOC_TEMPLATES})

    @app.get("/documents/ours/{kind}")
    async def document_ours(request: Request, kind: str) -> Response:
        """Наш шаблон на скачивание: с него начинают свой."""
        if not may_view(request, "settings"):
            return denied(request, "settings")
        path = our_template(kind)
        if path is None:
            return render(request, "missing.html", status_code=404, what="Шаблон")
        return FileResponse(path, filename=f"{kind}-наш{logic.DOC_SUFFIX}")

    @app.get("/documents/mine/{template_id}")
    async def document_mine(request: Request, template_id: int) -> Response:
        """Загруженный шаблон: скачать и посмотреть, что именно включено."""
        if not may_view(request, "settings"):
            return denied(request, "settings")
        row = await crm.doc_template(template_id)
        path = Path(cfg.doc_dir) / str((row or {}).get("filename") or "")
        if row is None or not path.is_file():
            return render(request, "missing.html", status_code=404, what="Шаблон")
        return FileResponse(path, filename=str(row.get("original")
                                               or row["filename"]))

    @app.post("/documents/{kind}")
    async def document_upload(request: Request, kind: str) -> Response:
        """Загрузить свой шаблон, включить наш обратно или убрать из архива."""
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        if kind not in logic.DOC_TEMPLATES or kind in logic.DOC_CODE_ONLY:
            return render(request, "missing.html", status_code=404,
                          what="Вид документа")
        data = await request.form()
        action = str(data.get("action") or "upload")
        by = who(request)
        if action == "ours":
            await crm.disable_doc_templates(kind)
            flash(request, f"«{logic.DOC_TEMPLATES[kind]['title']}»: "
                           "вернули наш шаблон. Ваш остался в архиве.")
            return redirect("/documents")
        if action in ("enable", "drop"):
            raw_id = str(data.get("template_id") or "")
            row = (await crm.doc_template(int(raw_id))
                   if raw_id.isdigit() else None)
            if row is None or row["kind"] != kind:
                flash(request, "Шаблон не найден.", "err")
                return redirect("/documents")
            if action == "enable":
                await crm.enable_doc_template(row["id"])
                flash(request, f"«{logic.DOC_TEMPLATES[kind]['title']}»: "
                               "включён ваш шаблон.")
            else:
                dropped = await crm.drop_doc_template(row["id"])
                if dropped is None:
                    flash(request, "Включённый шаблон не удаляется: "
                                   "сначала верните наш.", "err")
                else:
                    (Path(cfg.doc_dir) / str(dropped["filename"])).unlink(
                        missing_ok=True)
                    flash(request, "Шаблон убран из архива.")
            return redirect("/documents")
        upload = data.get("template")
        filename = getattr(upload, "filename", "") or ""
        raw = await upload.read() if filename else b""
        if not raw:
            flash(request, "Выберите файл шаблона.", "err")
            return redirect("/documents")
        try:
            doctemplates.check_upload(raw, filename)
        except contract_service.TemplateProblem as exc:
            flash(request, str(exc), "err")
            return redirect("/documents")
        folder = Path(cfg.doc_dir)
        number = len(await crm.doc_templates(kind)) + 1
        name = logic.doc_filename(kind, number)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(raw)
        except OSError as err:
            log.warning("шаблон не сохранён: %s", err)
            flash(request, "Шаблон не сохранился — попробуйте ещё раз.", "err")
            return redirect("/documents")
        template_id = await crm.add_doc_template(
            kind=kind, filename=name, original=filename[:200],
            size_bytes=len(raw), sha256=doctemplates.digest(raw), by=by)
        await crm.enable_doc_template(template_id)
        flash(request, f"«{logic.DOC_TEMPLATES[kind]['title']}»: ваш шаблон "
                       "загружен и включён. Наш выключился сам.")
        return redirect("/documents")

    @app.post("/documents/marks/{kind}")
    async def company_mark(request: Request, kind: str) -> Response:
        """Подпись и печать: png на прозрачном фоне."""
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        if kind not in logic.COMPANY_MARKS:
            return render(request, "missing.html", status_code=404, what="Файл")
        data = await request.form()
        if str(data.get("action") or "") == "drop":
            await crm.drop_company_mark(kind)
            (Path(cfg.doc_dir) / logic.mark_filename(kind)).unlink(missing_ok=True)
            flash(request, f"{logic.COMPANY_MARKS[kind]} убрана: "
                           "подстановка в документах просто исчезнет.")
            return redirect("/documents")
        upload = data.get("mark")
        filename = getattr(upload, "filename", "") or ""
        raw = await upload.read() if filename else b""
        if not raw:
            flash(request, "Выберите файл.", "err")
            return redirect("/documents")
        if Path(filename).suffix.lower() not in logic.MARK_SUFFIXES:
            flash(request, "Только png: прозрачный фон бывает только у него, "
                           "а подпись на белом квадрате закроет текст.", "err")
            return redirect("/documents")
        if len(raw) > logic.MARK_MAX_BYTES:
            flash(request, f"Файл больше "
                           f"{logic.MARK_MAX_BYTES // (1024 * 1024)} МБ.", "err")
            return redirect("/documents")
        name = logic.mark_filename(kind)
        try:
            Path(cfg.doc_dir).mkdir(parents=True, exist_ok=True)
            (Path(cfg.doc_dir) / name).write_bytes(raw)
        except OSError as err:
            log.warning("подпись не сохранена: %s", err)
            flash(request, "Файл не сохранился — попробуйте ещё раз.", "err")
            return redirect("/documents")
        await crm.set_company_mark(kind, filename=name, size_bytes=len(raw),
                                   by=who(request))
        flash(request, f"{logic.COMPANY_MARKS[kind]} загружена: она встанет "
                       "на место подстановки в шаблоне.")
        return redirect("/documents")

    @app.get("/documents/marks/{kind}")
    async def company_mark_file(request: Request, kind: str) -> Response:
        if not may_view(request, "settings"):
            return denied(request, "settings")
        path = Path(cfg.doc_dir) / logic.mark_filename(kind)
        if kind not in logic.COMPANY_MARKS or not path.is_file():
            return render(request, "missing.html", status_code=404, what="Файл")
        return FileResponse(path)

    # ─────────────────── ввод техники в эксплуатацию ───────────────────

    @app.get("/intake")
    async def intake_page(request: Request) -> Response:
        if not may_view(request, "settings"):
            return denied(request, "settings")
        settings = await crm.settings()
        rows = await crm.bikes_on_assembly()
        cells = await crm.batteries_on_assembly()
        return render(request, "intake.html",
                      checks=logic.bike_check_settings(settings),
                      search=logic.search_settings(settings),
                      rows=[{**b, "state": logic.bike_check_state(b, settings)}
                            for b in rows],
                      cells=[{**b, "state": logic.battery_check_state(b, settings)}
                             for b in cells])

    @app.post("/intake")
    async def intake_save(request: Request) -> Response:
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        data = await form(request)
        by = who(request)
        await crm.set_setting("bike_check_required",
                              "1" if data.get("required") else "0", by=by)
        await crm.set_setting("bike_photo_required",
                              "1" if data.get("photo") else "0", by=by)
        for key, what in (("search_after_days", "Розыск"),
                          ("theft_after_days", "Кража")):
            got = count_field(data, key, what=what, default="0", limit=365)
            if not got.ok:
                flash(request, got.error, "err")
                return redirect("/intake")
            if got.value:
                await crm.set_setting(key, str(got.value), by=by)
        flash(request, "Правила ввода техники сохранены.")
        return redirect("/intake")

    # ─────────────────────── уведомления ───────────────────────

    @app.get("/notices")
    async def notices_page(request: Request) -> Response:
        if not may_view(request, "settings"):
            return denied(request, "settings")
        state = logic.notice_settings(await crm.notices())
        counts = await crm.notice_counts(logic.NOTICE_LOG_DAYS)
        return render(request, "notices.html",
                      groups=logic.notice_rows(state, counts),
                      log=await crm.notice_log(limit=50),
                      bot_ready=bot is not None, bot_state=await bot_health(),
                      # Адресат командного уведомления - сотрудник с Telegram
                      # вместо служебного чата: техник получает «ждёт
                      # запчасть» лично, а не в общем потоке.
                      recipients=[x for x in await crm.staff_all()
                                  if x.get("tg_id") and x.get("active")],
                      chat_ready=bool(cfg.contract_chat_id))

    @app.post("/notices/{code}")
    async def notice_save(request: Request, code: str) -> Response:
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        if code not in logic.NOTICES:
            return render(request, "missing.html", status_code=404,
                          what="Уведомление")
        data = await form(request)
        default = logic.notice_defaults(code)
        at_hour: int | None = None
        if default["at_hour"] is not None:
            # «Сразу» остаётся «сразу»: перенести событийное уведомление на
            # час нельзя - события не ждут расписания.
            hour = count_field(data, "at_hour", what="Час",
                               default=str(default["at_hour"]), limit=23)
            minute = count_field(data, "at_minute", what="Минуты",
                                 default="0", limit=59)
            for check in (hour, minute):
                if not check.ok:
                    flash(request, check.error, "err")
                    return redirect("/notices")
            at_hour, at_minute = hour.value, minute.value
        else:
            at_minute = 0
        extra = {}
        for key, fallback in (default["extra"] or {}).items():
            got = count_field(data, key, what="Срок", default=str(fallback),
                              limit=365)
            if not got.ok:
                flash(request, got.error, "err")
                return redirect("/notices")
            extra[key] = got.value
        await crm.set_notice(code, enabled=bool(data.get("enabled")),
                             at_hour=at_hour, at_minute=at_minute,
                             chat_id=(data.get("chat_id") or "").strip() or None,
                             extra=extra, by=who(request))
        flash(request, f"«{default['title']}»: "
              + ("включено." if data.get("enabled") else "выключено."))
        return redirect("/notices")

    # ─────────────────────── счета на оплату ───────────────────────

    # Живость бота: get_me раз в пять минут, не на каждый экран. Если бот
    # молчит, слать об этом в Telegram тем же ботом бессмысленно - поэтому
    # предупреждение живёт в панели, а не в уведомлениях.
    _bot_health: dict[str, Any] = {"at": 0.0, "ok": None, "name": "", "error": ""}

    async def bot_health() -> dict[str, Any]:
        if bot is None:
            return {"ok": None, "name": "", "error": "бот к панели не подключён"}
        now = time.monotonic()
        if now - _bot_health["at"] < 300 and _bot_health["ok"] is not None:
            return dict(_bot_health)
        try:
            me = await asyncio.wait_for(bot.get_me(), timeout=5)
            _bot_health.update(at=now, ok=True, name=getattr(me, "username", "") or "",
                               error="")
        except Exception as exc:                         # noqa: BLE001
            _bot_health.update(at=now, ok=False, error=str(exc) or type(exc).__name__)
        return dict(_bot_health)

    def acquiring() -> Any:
        """Эквайринг Точки для одного запроса.

        Панель ходит в банк только здесь и только по нажатию кнопки:
        ссылку оператор просит при клиенте, и ждать круга опроса в
        процессе бота ему негде. Сами опросы статусов там и остались.
        """
        if not (cfg.tochka_token and cfg.tochka_customer_code):
            return None
        return tochka.TochkaClient(token=cfg.tochka_token,
                                   customer_code=cfg.tochka_customer_code)

    async def acquiring_live() -> Any:
        """Эквайринг, если он настроен И не выключен владельцем в панели.

        Выключатель - настройка, а не удаление токена из окружения:
        отключить на день эквайринг, который спорит с кассой, должен
        мочь владелец, а не тот, кто правит .env на сервере.
        """
        if not logic.acquiring_enabled(await crm.settings()):
            return None
        return acquiring()

    def acquiring_state(settings: dict[str, Any]) -> dict[str, Any]:
        code = str(cfg.tochka_customer_code or "")
        return {"configured": acquiring() is not None,
                "enabled": logic.acquiring_enabled(settings),
                "code": ("•••" + code[-4:]) if code else ""}

    @app.post("/payments/acquiring")
    async def acquiring_toggle(request: Request) -> Response:
        """Включить, выключить или проверить эквайринг из панели."""
        if not may_edit(request, "finance"):
            return denied(request, "finance")
        action = (await form(request)).get("action") or ""
        if action in ("on", "off"):
            await crm.set_setting("acquiring_enabled", "1" if action == "on" else "0",
                                  by=who(request))
            flash(request, "Эквайринг включён." if action == "on"
                  else "Эквайринг выключен: ссылки на оплату не выставляются.")
        elif action == "check":
            client = acquiring()
            if client is None:
                flash(request, "Эквайринг не настроен: нет токена или кода клиента "
                               "в окружении панели.", "err")
            else:
                try:
                    got = await client.ping()
                    flash(request, f"Банк принял токен: торговых точек "
                                   f"эквайринга - {got['retailers']}.")
                except Exception as exc:                 # noqa: BLE001
                    flash(request, f"Банк не принял: {exc}", "err")
        else:
            flash(request, "Непонятное действие.", "err")
        return redirect("/payments")

    @app.get("/payments")
    async def payments_page(request: Request) -> Response:
        if not may_view(request, "finance"):
            return denied(request, "finance")
        status = request.query_params.get("status") or ""
        orders = await crm.pay_orders(status=status or None, limit=200)
        settings = logic.pay_settings(await crm.settings())
        # В выпадающем списке - те, кто платит: должники и действующие
        # аренды. Весь список клиентов сюда не влезает и не нужен.
        picks = {c["id"]: c for c in await crm.debtors(200)}
        for rental in await crm.active_rentals():
            picks.setdefault(rental["client_id"],
                             {"id": rental["client_id"],
                              "full_name": rental.get("full_name"),
                              "phone": rental.get("phone")})
        raw_settings = await crm.settings()
        return render(request, "payments.html",
                      rows=logic.pay_rows(orders), status=status,
                      settings=settings, acq=acquiring_state(raw_settings),
                      summary=logic.pay_summary(
                          await crm.pay_orders(limit=500)),
                      online=await acquiring_live() is not None,
                      clients_for_pick=sorted(
                          picks.values(), key=lambda c: str(c.get("full_name") or "")))

    @app.post("/payments")
    async def payment_create(request: Request) -> Response:
        """Выставить счёт: сумма и клиент. Ссылку берём у банка сразу."""
        if not may_edit(request, "finance"):
            return denied(request, "finance")
        data = await form(request)
        client = (await crm.client(int(data["client_id"]))
                  if (data.get("client_id") or "").isdigit() else None)
        if client is None:
            flash(request, "Выберите клиента, которому выставить счёт.", "err")
            return redirect("/payments")
        amount = logic.check_amount(data.get("amount"))
        if not amount.ok:
            flash(request, amount.error, "err")
            return redirect("/payments")
        rental = await crm.active_rental_of(client["id"])
        order = await service.create_pay_order(
            crm, client=client, rental=rental, amount=amount.value,
            by=who(request), acquiring=await acquiring_live())
        if order.get("status") == "failed":
            flash(request, f"Счёт {order['no']} заведён, но ссылки нет: "
                           f"{order.get('error') or 'банк не ответил'}", "err")
        else:
            flash(request, f"Счёт {order['no']} на {logic.money(order['amount'])} "
                           f"готов — отправьте ссылку клиенту.")
        return redirect(f"/payments/{order['id']}")

    @app.post("/payments/settings")
    async def payments_settings(request: Request) -> Response:
        if not may_edit(request, "finance"):
            return denied(request, "finance")
        data = await request.form()
        methods = [m for m in data.getlist("methods") if m in logic.PAY_METHODS]
        if not methods:
            flash(request, "Хотя бы один способ приёма должен остаться.", "err")
            return redirect("/payments")
        hour = count_field({"hour": (data.get("autocharge_hour") or "")},
                           "hour", what="Час автосписания",
                           default=str(logic.AUTOCHARGE_HOUR), limit=23)
        if not hour.ok:
            flash(request, hour.error, "err")
            return redirect("/payments")
        by = who(request)
        await crm.set_setting("pay_methods", ",".join(methods), by=by)
        await crm.set_setting("autocharge",
                              "1" if data.get("autocharge") else "0", by=by)
        await crm.set_setting("autocharge_hour", str(hour.value), by=by)
        flash(request, "Настройки приёма оплаты сохранены."
              if not data.get("autocharge") else
              "Автосписание включено: долг у клиентов с привязанной картой "
              f"будет списываться в {hour.value}:00.")
        return redirect("/payments")

    # Раньше /payments/{order_id}: иначе «settings» уедет в число.
    @app.get("/payments/{order_id}")
    async def payment_page(request: Request, order_id: int) -> Response:
        if not may_view(request, "finance"):
            return denied(request, "finance")
        order = await crm.pay_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Счёт")
        return render(request, "payment.html", order=order,
                      expired=logic.pay_expired(order),
                      card=await crm.card_of(order["client_id"]),
                      methods=logic.pay_methods(await crm.settings()))

    @app.post("/payments/{order_id}")
    async def payment_handle(request: Request, order_id: int) -> Response:
        """Действия по счёту: отправить клиенту, закрыть руками, снять."""
        if not may_edit(request, "finance"):
            return denied(request, "finance")
        order = await crm.pay_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Счёт")
        action = (await form(request)).get("action") or "send"
        back = f"/payments/{order_id}"
        if action == "cancel":
            try:
                await service.cancel_pay_order(crm, order, by=who(request))
            except service.ServiceError as exc:
                flash(request, str(exc), "err")
                return redirect(back)
            flash(request, f"Счёт {order['no']} снят.")
            return redirect(back)
        if action == "send":
            sent = await notify.pay_link(bot, db, order)
            flash(request, "Ссылка отправлена клиенту." if sent else
                  "Клиента нет в боте — скопируйте ссылку и передайте сами.",
                  "ok" if sent else "err")
            return redirect(back)
        if action in ("cash", "transfer"):
            try:
                await service.credit_pay_order(crm, order, by=who(request),
                                               method=action)
            except service.ServiceError as exc:
                flash(request, str(exc), "err")
                return redirect(back)
            client = await crm.client(order["client_id"])
            if client is not None:
                await referral_bonus(client, logic.to_money(order["amount"]),
                                     who(request))
            flash(request, f"{logic.money(order['amount'])} зачислено "
                           f"по счёту {order['no']}.")
            return redirect(back)
        if action == "drop_card":
            await crm.drop_card(order["client_id"])
            flash(request, "Карта отвязана: автосписания больше не будет.")
            return redirect(back)
        flash(request, "Непонятное действие.", "err")
        return redirect(back)

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
        q = request.query_params.get("q") or ""
        found = logic.rows_search(rows, q, TRACKER_SEARCH)
        tools = list_tools(request, found, allowed=TRACKER_SORTS)
        # Точки на карте - по найденному: поиск и есть «выделить найденное».
        points = logic.map_points(tools["all_rows"])
        return render(request, "map.html", rows=tools["rows"], tools=tools, q=q,
                      points=points,
                      points_json=json.dumps(points, ensure_ascii=False),
                      map_cfg=logic.map_config(settings),
                      summary=logic.tracker_summary(rows),
                      alerts=await crm.tracker_alerts(open_only=True, limit=50))

    TRACKER_SORTS = {"bike": "bike_code", "speed": "speed", "silent": "silent_hours",
                     "client": "client_name"}
    TRACKER_SEARCH = ("bike_code", "bike_model", "client_name", "alias", "device_id")

    @app.get("/map.{ext}")
    async def map_csv(request: Request, ext: str) -> Response:
        if not may_view(request, "trackers"):
            return denied(request, "trackers")
        rows = logic.rows_search(
            logic.tracker_rows(await crm.trackers(), settings=await crm.settings()),
            request.query_params.get("q") or "", TRACKER_SEARCH)
        return _table(ext, "map",
                      ["Велосипед", "Модель", "Трекер", "Состояние", "Скорость",
                       "Связь, ч назад", "У кого", "Широта", "Долгота"],
                      [[r.get("bike_code"), r.get("bike_model"),
                        r.get("alias") or r.get("device_id"),
                        logic.tracker_state_title(r), r.get("speed"),
                        r.get("silent_hours"), r.get("client_name"),
                        r.get("lat"), r.get("lon")] for r in rows])

    @app.get("/alerts")
    async def alerts_page(request: Request) -> Response:
        """Реестр тревог: что случилось, кто взял и что с этим делают."""
        if not may_view(request, "trackers"):
            return denied(request, "trackers")
        rows, view, level, kind, q = await alert_list(request)
        tools = list_tools(request, rows, allowed=ALERT_SORTS)
        return render(request, "alerts.html", rows=tools["rows"], tools=tools,
                      view=view, level=level, kind=kind, q=q,
                      summary=logic.alert_summary(
                          logic.alert_rows(await crm.tracker_alerts(open_only=False,
                                                                    limit=2000))))

    ALERT_SORTS = {"level": "level", "title": "title", "bike": "bike_code",
                   "client": "client_name", "created": "created_at",
                   "state": "state"}

    async def alert_list(request: Request) -> tuple[list[dict], str, str, str, str]:
        view = request.query_params.get("view") or "needs"
        level = request.query_params.get("level") or ""
        kind = request.query_params.get("kind") or ""
        q = request.query_params.get("q") or ""
        rows = logic.alert_rows(await crm.tracker_alerts(
            open_only=view != "all", level=level or None, kind=kind or None,
            limit=2000))
        shown = [r for r in rows if r["needs"]] if view == "needs" else rows
        return (logic.rows_search(shown, q, ("title", "note", "bike_code",
                                             "client_name", "alias", "device_id")),
                view, level, kind, q)

    @app.get("/alerts.{ext}")
    async def alerts_csv(request: Request, ext: str) -> Response:
        if not may_view(request, "trackers"):
            return denied(request, "trackers")
        rows, *_ = await alert_list(request)
        return _table(ext, "alerts",
                      ["Уровень", "Что случилось", "Подробности", "Велосипед",
                       "Клиент", "Когда", "Состояние", "Кто взял", "Закрыта"],
                      [[logic.ALERT_LEVELS.get(r["level"], r["level"]), r["title"],
                        r.get("note"), r.get("bike_code") or r.get("alias")
                        or r.get("device_id"), r.get("client_name"),
                        r.get("created_at"),
                        logic.ALERT_STATES.get(r["state"], r["state"]) if r["open"]
                        else "закрыта",
                        r.get("taken_by"), r.get("handled_at")]
                       for r in rows])

    @app.post("/alerts/{alert_id}")
    async def alert_action(request: Request, alert_id: int) -> Response:
        """Взять, отложить, признать нормой или закрыть.

        «Это норма» - не закрытие: тревога остаётся открытой, поэтому
        второй раз она не поднимется, пока причина держится, а исчезнет
        причина - опрос закроет её сам.
        """
        if not may_edit(request, "trackers"):
            return denied(request, "trackers")
        if await crm.tracker_alert(alert_id) is None:
            return render(request, "missing.html", status_code=404, what="Тревога")
        data = await form(request)
        action = str(data.get("action") or "")
        nxt = data.get("next") or ""
        back = nxt if nxt.startswith("/") and not nxt.startswith("//") else "/alerts"
        by = who(request)
        if action == "close":
            await crm.handle_alert(alert_id, by=by)
            flash(request, "Тревога закрыта.")
        elif action == "take":
            await crm.set_alert_state(alert_id, state="working", by=by)
            flash(request, "Взяли в работу.")
        elif action == "snooze":
            until = logic.snooze_until(data.get("hours"))
            await crm.set_alert_state(alert_id, state="snoozed", by=by,
                                      snooze_until=until)
            flash(request, f"Отложено до {_dmy(until)}.")
        elif action == "normal":
            await crm.set_alert_state(alert_id, state="normal", by=by)
            flash(request, "Помечено нормой: пока причина держится, "
                           "тревога больше не поднимется.")
        else:
            flash(request, "Непонятное действие.", "err")
        return redirect(back)

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
        # Трек за период: «где он был вчера» - главный вопрос к трекеру,
        # и отвечать на него списком координат было бы издевательством.
        kind = request.query_params.get("range") or "today"
        since_q = logic.check_date(request.query_params.get("since"), default=None)
        until_q = logic.check_date(request.query_params.get("until"), default=None)
        first, last = logic.track_period(
            kind, since=since_q.value if since_q.ok else None,
            until=until_q.value if until_q.ok else None)
        tz = datetime.now().astimezone().tzinfo
        track = await crm.track_between(
            tracker_id,
            since=datetime.combine(first, datetime.min.time(), tzinfo=tz),
            until=datetime.combine(last + timedelta(days=1), datetime.min.time(),
                                   tzinfo=tz))
        return render(request, "tracker.html", tracker=row, track=track,
                      run_km=logic.track_distance(track),
                      track_range=kind, track_since=first, track_until=last,
                      line_json=json.dumps(logic.track_line(track)),
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
                               await crm.stock_map(), await crm.part_last_moved())
        tools = list_tools(request, rows, allowed=PART_SORTS)
        # График денег на полке - только тем, кому открыты деньги: это
        # сумма, а не количество гаек.
        chart = (logic.stock_value_chart(await crm.stock_value_by_month())
                 if may_view(request, "finance") else None)
        return render(request, "parts.html", rows=tools["rows"], tools=tools,
                      summary=logic.stock_summary(tools["all_rows"]),
                      node=node, q=q, chart=chart,
                      views=await views_of(request, "/parts"),
                      nodes=await crm.repair_nodes())

    @app.get("/parts.{ext}")
    async def parts_csv(request: Request, ext: str) -> Response:
        if not may_view(request, "inventory"):
            return denied(request, "inventory")
        rows = logic.part_rows(
            await crm.parts(node=request.query_params.get("node") or None,
                            q=request.query_params.get("q") or None),
            await crm.stock_map(), await crm.part_last_moved())
        money_ok = may_view(request, "finance")
        header = ["Позиция", "Узел", "Совместимость", "Остаток", "Ед.",
                  "Неснижаемый", "Не хватает", "Дней на складе"]
        if money_ok:
            header += ["Себестоимость", "Σ себестоимость", "Цена клиенту",
                       "Σ по клиенту"]
        out = []
        for r in rows:
            line = [r["title"], logic.REPAIR_NODES.get(r.get("node"), ""),
                    r.get("model") or "все", r["stock"], r.get("unit"),
                    r.get("min_stock"), r["short"] or "", r.get("days_on_stock")]
            if money_ok:
                line += [logic.to_money(r.get("cost") or 0), r["cost_total"],
                         logic.to_money(r.get("price") or 0), r["price_total"]]
            out.append(line)
        return _table(ext, "parts", header, out)

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
        items = await crm.part_order_items(order_id)
        try:
            doc_id = await service.receive_part_order(crm, order, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/part-orders")
        doc = await crm.part_doc(doc_id)
        # Наряд, который стоял из-за этой запчасти, может ехать дальше -
        # и техник должен узнать об этом сейчас, а не заглянув на склад.
        await tell_parts_arrived(await service.orders_waiting_for(crm, items))
        flash(request, f"Заказ принят: приход {doc['no']} на {logic.money(doc['total'])}.")
        return redirect("/part-orders")

    # ─────────────────────── пересчёт техники ───────────────────────

    TAKE_SORTS = {"no": "no", "started": "started_at", "what": "title",
                  "expected": "expected", "found": "found", "missing": "missing",
                  "extra": "extra", "who": "created_by"}

    async def take_rows(request: Request) -> tuple[list[dict], str]:
        q = request.query_params.get("q") or ""
        rows = await crm.stock_takes(limit=2000)
        for r in rows:
            r["title"] = logic.take_title(r)
        return logic.rows_search(rows, q, ("no", "title", "note", "created_by",
                                           "location")), q

    @app.get("/stock-takes")
    async def stock_takes_page(request: Request) -> Response:
        rows, q = await take_rows(request)
        tools = list_tools(request, rows, allowed=TAKE_SORTS)
        return render(request, "stock_takes.html", rows=tools["rows"], tools=tools,
                      q=q, current=await crm.open_stock_take())

    @app.get("/stock-takes.{ext}")
    async def stock_takes_csv(request: Request, ext: str) -> Response:
        rows, _ = await take_rows(request)
        return _table(ext, "stock-takes",
                      ["№", "Дата", "Что считали", "Состояние", "Ожидалось",
                       "Найдено", "Не нашли", "Лишние", "Кто провёл", "Комментарий"],
                      [[r["no"], r.get("started_at"), r["title"],
                        logic.TAKE_STATES.get(r.get("status"), r.get("status")),
                        r.get("expected"), r.get("found"), r.get("missing"),
                        r.get("extra"), r.get("created_by"), r.get("note")]
                       for r in rows])

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
        what = logic.check_choice(data.get("what") or "all", logic.TAKE_WHAT,
                                  what="Что считаем")
        if not what.ok:
            flash(request, what.error, "err")
            return redirect("/stock-takes")
        try:
            take_id = await service.start_stock_take(
                crm, scope=scope.value, location=location, note=note.value,
                what=what.value, by=who(request))
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
                      counts=counts, progress=logic.take_progress(counts),
                      by_kind=logic.take_counts_by_kind(items))

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

