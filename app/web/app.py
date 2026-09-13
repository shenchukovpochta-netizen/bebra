"""Веб-панель CRM: FastAPI + Jinja2, формы без JavaScript-фреймворков.

Всё серверное: страница - это шаблон, действие - POST формы и редирект.
Так панель открывается с любого телефона, а код читается сверху вниз.
Данные приходят из CrmDB (или его заглушки в тестах), решения - из
app.crm.logic и app.crm.service, уведомления клиентам - app.crm.notify.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
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
from ..crm import logic, notify, service
from .config import WebConfig

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
PUBLIC = ("/login", "/static", "/healthz")
SESSION_DAYS = 14


def _dmy(value: Any) -> str:
    if not value:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    return str(value)


def _file_exists(path: str) -> bool:
    return os.path.exists(path)


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
        CLIENT_STATUSES=logic.CLIENT_STATUSES, RENTAL_STATUSES=logic.RENTAL_STATUSES,
        BILLING=logic.BILLING, ROLES=logic.ROLES, app_title=cfg.title,
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

    def is_admin(request: Request) -> bool:
        staff = getattr(request.state, "staff", None)
        return bool(staff and staff.get("role") == "admin")

    async def auth(request: Request, call_next: Any) -> Response:
        request.state.staff = None
        staff_id = request.session.get("staff_id")
        if staff_id:
            staff = await crm.staff_by_id(int(staff_id))
            if staff and staff.get("active"):
                request.state.staff = staff
        path = request.url.path
        if request.state.staff is None and not path.startswith(PUBLIC):
            return redirect("/login?next=" + quote(path, safe=""))
        return await call_next(request)

    # Порядок важен: последний add_middleware - внешний. Сессия должна быть
    # распакована ДО проверки входа, поэтому SessionMiddleware добавляется
    # после auth.
    app.add_middleware(BaseHTTPMiddleware, dispatch=auth)
    app.add_middleware(SessionMiddleware, secret_key=cfg.secret, session_cookie="crm_session",
                       same_site="strict", max_age=SESSION_DAYS * 24 * 3600)

    async def form(request: Request) -> dict[str, str]:
        data = await request.form()
        return {k: (v if isinstance(v, str) else "") for k, v in data.items()}

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
        login_check = logic.check_login(data.get("login"))
        staff = await crm.staff_by_login(login_check.value) if login_check.ok else None
        if (staff is None or not staff.get("active")
                or not logic.verify_password(data.get("password") or "",
                                             staff.get("password_hash"))):
            return render(request, "login.html", status_code=401,
                          error="Неверный логин или пароль.",
                          next=data.get("next") or "/")
        request.session.clear()
        request.session["staff_id"] = staff["id"]
        target = data.get("next") or "/"
        if not target.startswith("/") or target.startswith("//"):
            target = "/"
        return redirect(target)

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        request.session.clear()
        return redirect("/login")

    @app.post("/me/password")
    async def my_password(request: Request) -> Response:
        data = await form(request)
        staff = request.state.staff
        if not logic.verify_password(data.get("old") or "", staff.get("password_hash")):
            flash(request, "Текущий пароль неверный.", "err")
            return redirect("/staff")
        check = logic.check_password(data.get("new"))
        if not check.ok:
            flash(request, check.error, "err")
            return redirect("/staff")
        await crm.set_staff_password(staff["id"], logic.hash_password(check.value))
        flash(request, "Пароль изменён.")
        return redirect("/staff")

    # ─────────────────────── дашборд ───────────────────────

    @app.get("/")
    async def dashboard(request: Request) -> Response:
        rentals = await crm.active_rentals()
        rows = []
        for r in rentals:
            s = summarize(r, r.get("balance", 0))
            rows.append({**r, "summary": s})
        rows.sort(key=lambda r: (r["summary"]["days_left"] or 0, r["id"]))
        attention = [r for r in rows
                     if (r["summary"]["days_left"] or 0) <= cfg.remind_before_days]
        return render(request, "dashboard.html",
                      counts=await crm.counts(), bikes=await crm.bike_counts(),
                      claims=await crm.pending_claims(), rentals=rows,
                      attention=attention, debtors=await crm.debtors(10),
                      month=await crm.ledger_totals(since=date.today().replace(day=1)))

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

    @app.get("/clients/new")
    async def client_new(request: Request) -> Response:
        return render(request, "client_form.html", client=None)

    async def _client_fields(request: Request, data: dict, *, current: dict | None) -> dict | None:
        name = logic.check_name(data.get("full_name"), what="ФИО")
        phone = bot_logic.normalize_phone(data.get("phone"))
        note = logic.check_note(data.get("note"))
        status = logic.check_choice(data.get("status") or "active", logic.CLIENT_STATUSES,
                                    what="Статус")
        contract = logic.check_name(data.get("contract_no"), what="Договор") \
            if (data.get("contract_no") or "").strip() else logic.Check(True, None)
        for check in (name, note, status, contract):
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
                "status": status.value, "contract_no": contract.value}

    @app.post("/clients")
    async def client_create(request: Request) -> Response:
        data = await form(request)
        fields = await _client_fields(request, data, current=None)
        if fields is None:
            return redirect("/clients/new")
        client_id = await crm.create_client(full_name=fields["full_name"],
                                            phone=fields["phone"], note=fields["note"],
                                            contract_no=fields["contract_no"])
        if fields["status"] != "active":
            await crm.update_client(client_id, status=fields["status"])
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
        flash(request, "Запись добавлена.")
        return redirect(f"/clients/{client_id}")

    @app.get("/clients/{client_id}/contract")
    async def client_contract(request: Request, client_id: int) -> Response:
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
        return render(request, "bikes.html", q=q, status=status,
                      rows=await crm.bikes(q=q or None, status=status or None),
                      counts=await crm.bike_counts())

    @app.get("/bikes/new")
    async def bike_new(request: Request) -> Response:
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
        return {"code": code.value, "model": model.value, "note": note.value,
                "frame_no": (data.get("frame_no") or "").strip() or None,
                "motor_no": (data.get("motor_no") or "").strip() or None,
                "battery_count": int(batteries), "purchase_price": price.value,
                "purchased_on": bought.value}

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
        bike_id = await crm.create_bike(**fields)
        flash(request, "Велосипед добавлен.")
        return redirect(f"/bikes/{bike_id}")

    @app.get("/bikes/{bike_id}")
    async def bike_card(request: Request, bike_id: int) -> Response:
        bike = await crm.bike(bike_id)
        if bike is None:
            return render(request, "missing.html", status_code=404, what="Велосипед")
        return render(request, "bike.html", bike=bike, log=await crm.bike_log(bike_id),
                      rentals=await crm.bike_rentals(bike_id))

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
        await crm.update_bike(bike_id, status=status.value)
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
        flash(request, "Аренда оформлена, первый период начислен.")
        return redirect(f"/rentals/{rental_id}")

    @app.get("/rentals/{rental_id}")
    async def rental_card(request: Request, rental_id: int) -> Response:
        rental = await crm.rental(rental_id)
        if rental is None:
            return render(request, "missing.html", status_code=404, what="Аренда")
        ledger = [x for x in await crm.ledger_of(rental["client_id"], 200)
                  if x.get("rental_id") == rental_id]
        return render(request, "rental.html", rental=rental,
                      summary=summarize(rental if rental["status"] == "active" else None,
                                        rental.get("balance", 0)),
                      ledger=ledger, tariffs=await crm.tariffs(active_only=True))

    @app.post("/rentals/{rental_id}/close")
    async def rental_close(request: Request, rental_id: int) -> Response:
        rental = await crm.rental(rental_id)
        if rental is None:
            return render(request, "missing.html", status_code=404, what="Аренда")
        data = await form(request)
        note = logic.check_note(data.get("note"))
        closed = logic.check_date(data.get("closed_on"), default=date.today())
        if not note.ok or not closed.ok:
            flash(request, note.error or closed.error, "err")
            return redirect(f"/rentals/{rental_id}")
        try:
            await service.close_rental(crm, rental, closed_on=closed.value, note=note.value,
                                       bike_status=data.get("bike_status") or "available")
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/rentals/{rental_id}")
        client = await crm.client(rental["client_id"])
        await notify.rental_closed(bot, db, crm, client, rental)
        flash(request, "Аренда закрыта, велосипед освобождён.")
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
        fleet = sum(v for k, v in bikes_by.items() if k not in ("sold", "lost"))
        rented = bikes_by.get("rented", 0)
        return render(request, "reports.html", months=await crm.revenue_by_month(12),
                      bikes=bikes_by, fleet=fleet, rented=rented,
                      utilization=(round(100 * rented / fleet) if fleet else 0),
                      debtors=await crm.debtors(50))

    # ─────────────────────── сотрудники ───────────────────────

    @app.get("/staff")
    async def staff_page(request: Request) -> Response:
        rows = await crm.staff_all() if is_admin(request) else []
        return render(request, "staff.html", rows=rows, admin=is_admin(request))

    @app.post("/staff")
    async def staff_create(request: Request) -> Response:
        if not is_admin(request):
            return Response("Только для администратора", status_code=403)
        data = await form(request)
        login_check = logic.check_login(data.get("login"))
        password = logic.check_password(data.get("password"))
        name = logic.check_name(data.get("name") or data.get("login"), what="Имя")
        role = logic.check_choice(data.get("role") or "manager", logic.ROLES, what="Роль")
        for check in (login_check, password, name, role):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/staff")
        if await crm.staff_by_login(login_check.value) is not None:
            flash(request, "Такой логин уже есть.", "err")
            return redirect("/staff")
        await crm.create_staff(login_check.value, logic.hash_password(password.value),
                               name.value, role.value)
        flash(request, f"Сотрудник {login_check.value} добавлен.")
        return redirect("/staff")

    @app.post("/staff/{staff_id}/password")
    async def staff_password(request: Request, staff_id: int) -> Response:
        if not is_admin(request):
            return Response("Только для администратора", status_code=403)
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
        if not is_admin(request):
            return Response("Только для администратора", status_code=403)
        target = await crm.staff_by_id(staff_id)
        if target is None:
            return render(request, "missing.html", status_code=404, what="Сотрудник")
        if target["id"] == request.state.staff["id"]:
            flash(request, "Себя отключить нельзя.", "err")
            return redirect("/staff")
        await crm.set_staff_active(staff_id, not target["active"])
        flash(request, "Доступ " + ("включён." if not target["active"] else "отключён."))
        return redirect("/staff")

    return app


async def ensure_admin(crm: Any, cfg: WebConfig) -> str | None:
    """Первый администратор при пустой таблице сотрудников.

    Пароль - из секрета CRM_ADMIN_PASSWORD; если его нет, генерируется
    и возвращается вызывающему, чтобы тот показал его в логе один раз.
    """
    if await crm.staff_count() > 0:
        return None
    password = cfg.admin_password or logic.generate_password()
    await crm.create_staff(cfg.admin_login, logic.hash_password(password),
                           "Администратор", "admin")
    return None if cfg.admin_password else password

