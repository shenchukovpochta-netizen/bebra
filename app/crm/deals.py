"""Сделки - «Входящие» воронкой, как в CRM продаж.

Путь человека от первого сообщения до сдачи велосипеда:

    Новая заявка -> 1-е касание -> Заключение договора -> В аренде
        -> Повторное продление / Сдал
    с ранних этапов: Отложенный спрос или Не взял

Сделка - не клиент и не аренда: она ссылается на них. Сообщение из
Telegram, MAX, Авито или WhatsApp, заявка из кабинета, выдача без заявки -
у каждого своя сделка, и у клиента одна открытая. Этапы «В аренде»,
«Повторное продление» и «Сдал» ставит сама аренда, и руками их не
переставить: колонка «В аренде» обязана совпадать с арендами. Ранние
этапы двигает человек, перетаскиванием карточки.

Связь с фактами - сверкой (`sync`), а не крючками в путях выдачи и
возврата: путей десяток (панель, бот, быстрые формы, импорт), а сверка
одна и идёт при открытии доски. Время этапа - время самого события
(выдача, ответ, подпись), а не момент сверки.

Денег здесь нет: журнал по-прежнему ledger, средний чек сделки не трогают.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from .. import logic as bot_logic
from . import logic, quickforms

log = logging.getLogger(__name__)

STAGES: dict[str, str] = {
    "new": "Новая заявка",
    "touch": "1-е касание",
    "contract": "Заключение договора",
    "rented": "В аренде",
    "renewed": "Повторное продление",
    "returned": "Сдал",
    "deferred": "Отложенный спрос",
    "lost": "Не взял",
}
# До аренды: здесь сделку двигает человек.
EARLY = ("new", "touch", "contract", "deferred")
# Ставит аренда: выдача, продление, возврат.
AUTO = ("rented", "renewed", "returned")
MANUAL = ("new", "touch", "contract", "deferred", "lost")
CLOSED = ("returned", "lost")
SOURCES: dict[str, str] = {
    "tg": "Telegram", "max": "MAX", "avito": "Авито", "wa": "WhatsApp",
    "tgp": "Telegram", "booking": "Заявка", "rental": "Выдача", "manual": "Вручную",
}
# Закрытые на доске - за этот срок: «Сдал» копится сотнями.
CLOSED_DAYS = 30
# Имя и телефон человека без карточки в закрытой сделке живут столько:
# это ПДн из чужого канала, а сделка, не ставшая арендой, их не оправдывает.
PURGE_DAYS = 180


def stage_title(code: Any) -> str:
    return STAGES.get(str(code or ""), str(code or "—"))


def can_move(deal: Mapping[str, Any], to: str) -> str | None:
    """Почему сделку нельзя перенести на этап `to`; None - можно."""
    if to not in STAGES:
        return "Такого этапа нет."
    if to == deal.get("stage"):
        return None
    if to in AUTO:
        return ("«В аренде», «Повторное продление» и «Сдал» ставит сама аренда: "
                "оформите выдачу или закройте аренду, и сделка переедет.")
    if deal.get("stage") in AUTO:
        return ("Сделка идёт арендой - её этап ставит аренда. Отложить или "
                "закрыть её можно, только закрыв аренду.")
    return None


def renewed(rental: Mapping[str, Any]) -> date | None:
    """День, с которого аренду продлили (начало второго периода), или None:
    начислено не больше первого периода. Срок первого периода - при выдаче
    (`issue_period_days`), смена тарифа его не меняет."""
    started, billed = rental.get("started_on"), rental.get("billed_until")
    days = int(rental.get("issue_period_days") or rental.get("period_days") or 0)
    if not isinstance(started, date) or not isinstance(billed, date) or days <= 0:
        return None
    if (billed - started).days <= days:
        return None
    return started + timedelta(days=days)


def _moment(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.astimezone()
    if isinstance(value, date):
        return datetime.combine(value, time(12, 0)).astimezone()
    return None


def _op(op: str, **fields: Any) -> dict[str, Any]:
    return {"op": op, **fields}


# ─────────────── сверка с фактами: чистые правила по шагам ───────────────
#
# Каждый шаг получает свежие сделки (сервис перечитывает их между шагами)
# и возвращает операции: create (новая сделка), move (этап) и link
# (ссылка на обращение, заявку, карточку). Внутри шага второй источник
# того же клиента пропускается: следующая сверка привяжет его к сделке,
# созданной этой.

def _open(deals: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [d for d in deals if not d.get("closed_at")]


def plan_rentals(deals: Iterable[Mapping[str, Any]], active: Iterable[Mapping[str, Any]],
                 closed: Mapping[int, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Аренды двигают сделки: выдача - «В аренде», второй период -
    «Повторное продление», возврат - «Сдал». Выдача без сделки (клиент
    пришёл на точку сам) заводит сделку сразу на «В аренде»."""
    deals = list(deals)
    ops: list[dict[str, Any]] = []
    by_rental = {int(d["rental_id"]): d for d in deals if d.get("rental_id")}
    open_by_client = {int(d["client_id"]): d for d in _open(deals) if d.get("client_id")}
    active_ids = set()
    for r in active:
        rid = int(r["id"])
        active_ids.add(rid)
        again = renewed(r)
        stage = "renewed" if again else "rented"
        at = _moment(r.get("created_at")) or _moment(r.get("started_on"))
        deal = by_rental.get(rid)
        if deal is not None:
            if deal.get("stage") == "rented" and again:
                ops.append(_op("move", id=deal["id"], stage="renewed", at=_moment(again)))
            continue
        client = int(r["client_id"])
        deal = open_by_client.get(client)
        if deal is not None and deal.get("stage") in AUTO:
            continue                     # у клиента одна идущая аренда
        title = r.get("bike_model") or r.get("tariff_name")
        if deal is not None:
            extra = {"rental_id": rid}
            if not deal.get("title") and title:
                extra["title"] = title
            if not deal.get("location") and r.get("location"):
                extra["location"] = r["location"]
            ops.append(_op("move", id=deal["id"], stage=stage, at=at, set=extra))
        else:
            ops.append(_op("create", stage=stage, at=at, fields={
                "client_id": client, "rental_id": rid, "title": title,
                "location": r.get("location"), "source": "rental"}))
        open_by_client[client] = {"stage": stage}
    for d in _open(deals):
        if d.get("stage") not in ("rented", "renewed") or not d.get("rental_id"):
            continue
        rid = int(d["rental_id"])
        if rid in active_ids:
            continue
        r = closed.get(rid)
        if r is not None and r.get("status") == "closed":
            ops.append(_op("move", id=d["id"], stage="returned", close=True,
                           at=_moment(r.get("closed_at")) or _moment(r.get("closed_on"))))
    return ops


def plan_bookings(deals: Iterable[Mapping[str, Any]], open_bookings: Iterable[Mapping[str, Any]],
                  gone: Mapping[int, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Заявка из кабинета - «Новая заявка» (или в сделку клиента, если она
    уже идёт до аренды); снятая заявка без аренды - «Не взял»."""
    deals = list(deals)
    ops: list[dict[str, Any]] = []
    by_booking = {int(d["booking_id"]): d for d in deals if d.get("booking_id")}
    open_by_client = {int(d["client_id"]): d for d in _open(deals) if d.get("client_id")}
    seen: set[int] = set()
    for b in open_bookings:
        bid = int(b["id"])
        if bid in by_booking:
            continue
        client = int(b["client_id"])
        if client in seen:
            continue
        title = " · ".join(x for x in (b.get("model"), b.get("tariff_name")) if x) or None
        place = b.get("location_name") or b.get("location_title")
        deal = open_by_client.get(client)
        if deal is not None:
            if deal.get("stage") in EARLY and not deal.get("booking_id"):
                extra: dict[str, Any] = {"booking_id": bid}
                if not deal.get("title") and title:
                    extra["title"] = title
                if not deal.get("location") and place:
                    extra["location"] = place
                ops.append(_op("link", id=deal["id"], set=extra))
            continue
        ops.append(_op("create", stage="new", at=_moment(b.get("created_at")), fields={
            "client_id": client, "booking_id": bid, "title": title, "location": place,
            "source": "booking"}))
        seen.add(client)
    for d in _open(deals):
        bid = d.get("booking_id")
        if not bid or d.get("stage") not in EARLY:
            continue
        b = gone.get(int(bid))
        if b is not None and b.get("status") == "cancelled":
            ops.append(_op("move", id=d["id"], stage="lost", close=True,
                           at=_moment(b.get("handled_at")) or _moment(b.get("updated_at"))))
    return ops


def plan_threads(deals: Iterable[Mapping[str, Any]], threads: Iterable[Mapping[str, Any]], *,
                 since: datetime | None) -> list[dict[str, Any]]:
    """Обращение - «Новая заявка»; ответ ему (из панели или из чата) -
    «1-е касание»; спам - «Не взял». Старые закрытые обращения (до первой
    сверки) сделок не заводят: доска начинается с сегодняшней работы."""
    deals = list(deals)
    ops: list[dict[str, Any]] = []
    by_thread = {int(d["thread_id"]): d for d in deals if d.get("thread_id")}
    open_by_client = {int(d["client_id"]): d for d in _open(deals) if d.get("client_id")}
    seen: set[int] = set()
    for t in threads:
        tid = int(t["id"])
        status = t.get("status")
        deal = by_thread.get(tid)
        if deal is not None:
            if deal.get("closed_at"):
                continue
            if status == "spam" and deal.get("stage") in EARLY:
                ops.append(_op("move", id=deal["id"], stage="lost", close=True,
                               at=_moment(t.get("updated_at"))))
            elif deal.get("stage") == "new" and t.get("last_out_at"):
                ops.append(_op("move", id=deal["id"], stage="touch",
                               at=_moment(t.get("last_out_at"))))
            client = t.get("client_id")
            if client and not deal.get("client_id") and int(client) not in open_by_client:
                ops.append(_op("link", id=deal["id"], set={"client_id": int(client)}))
                open_by_client[int(client)] = deal
            continue
        if status == "spam":
            continue
        created = _moment(t.get("created_at"))
        if status not in logic.INBOX_OPEN and (since is None or created is None
                                               or created < since):
            continue
        client = int(t["client_id"]) if t.get("client_id") else None
        if client is not None:
            deal = open_by_client.get(client)
            if deal is not None:
                if not deal.get("thread_id"):
                    ops.append(_op("link", id=deal["id"], set={"thread_id": tid}))
                continue
            if client in seen:
                continue
            seen.add(client)
        answered = t.get("last_out_at")
        ops.append(_op("create", stage="touch" if answered else "new",
                       at=_moment(answered) or created, fields={
                           "thread_id": tid, "client_id": client, "name": t.get("name"),
                           "phone": t.get("phone"), "title": t.get("subject"),
                           "source": t.get("channel") or "manual"}))
    return ops


def plan_contracts(deals: Iterable[Mapping[str, Any]],
                   signed: Mapping[int, datetime]) -> list[dict[str, Any]]:
    """Договор подписан в боте после того, как сделка завелась, а аренды
    ещё нет - «Заключение договора». `signed` - id клиента -> момент подписи."""
    ops: list[dict[str, Any]] = []
    for d in _open(deals):
        if d.get("stage") not in ("new", "touch", "deferred") or not d.get("client_id"):
            continue
        at = signed.get(int(d["client_id"]))
        created = _moment(d.get("created_at"))
        if at is not None and (created is None or at >= created):
            ops.append(_op("move", id=d["id"], stage="contract", at=at))
    return ops


# ─────────────────────────── доска ───────────────────────────

def board(deals: Iterable[Mapping[str, Any]], *, now: datetime | None = None,
          per_column: int = 200) -> list[dict[str, Any]]:
    """Колонки доски по этапам, свежие сверху. Закрытые - за CLOSED_DAYS."""
    now = now or datetime.now(UTC)
    edge = now - timedelta(days=CLOSED_DAYS)
    columns = {code: [] for code in STAGES}
    for d in deals:
        closed = _moment(d.get("closed_at"))
        if closed is not None and closed < edge:
            continue
        columns.setdefault(d.get("stage"), []).append(d)
    out = []
    for code, title in STAGES.items():
        cards = sorted(columns.get(code, []),
                       key=lambda d: _moment(d.get("stage_at")) or edge, reverse=True)
        out.append({"code": code, "title": title, "count": len(cards),
                    "cards": cards[:per_column], "more": max(0, len(cards) - per_column),
                    "auto": code in AUTO})
    return out


def dress(deal: Mapping[str, Any], *, inbox_ok: bool,
          now: datetime | None = None) -> dict[str, Any]:
    """Карточка доски. Имя и телефон из чужого канала без карточки клиента
    видит только тот, кому открыта переписка: раздел «Сообщения» по
    умолчанию у владельца, и доска его не обходит."""
    d = dict(deal)
    hidden = not inbox_ok and bool(d.get("thread_id")) and not d.get("client_id")
    d["hidden"] = hidden
    d["who"] = "Обращение" if hidden else card_name(d)
    d["phone_shown"] = None if hidden else (d.get("client_phone") or d.get("phone"))
    d["source_title"] = SOURCES.get(str(d.get("source") or ""), d.get("source") or "—")
    d["stage_title"] = stage_title(d.get("stage"))
    d["when"] = when(d.get("stage_at"), now=now)
    d["waiting"] = (bool(d.get("waiting_since"))
                    and d.get("thread_status") in logic.INBOX_OPEN)
    return d


def matches(deal: Mapping[str, Any], *, q: str = "", who: str = "",
            me: int | None = None, location: str = "") -> bool:
    """Фильтр доски: поиск по имени, телефону и сути, ответственный
    («me» - мои, «none» - без ответственного, номер), точка."""
    resp = deal.get("responsible_id")
    if who == "me" and (me is None or resp != me):
        return False
    if who == "none" and resp is not None:
        return False
    if who.isdigit() and resp != int(who):
        return False
    if location == "none" and deal.get("location"):
        return False
    if location and location != "none" and deal.get("location") != location:
        return False
    q = q.strip().lower()
    if not q:
        return True
    digits = "".join(ch for ch in q if ch.isdigit())
    if len(digits) >= 4:
        phone = "".join(ch for ch in str(deal.get("phone_shown") or "") if ch.isdigit())
        if digits[-10:] in phone:
            return True
    text = " ".join(str(deal.get(k) or "") for k in ("who", "title", "bike_code", "note"))
    return q in text.lower()


def card_name(deal: Mapping[str, Any]) -> str:
    return (deal.get("client_name") or deal.get("name") or deal.get("phone")
            or "Без имени")


def when(value: Any, *, now: datetime | None = None) -> str:
    """«Сегодня 09:28», «Вчера 22:44», «03.10.2026» - как в карточке CRM."""
    moment = _moment(value)
    if moment is None:
        return ""
    local = moment.astimezone()
    today = (now or datetime.now().astimezone()).astimezone().date()
    if local.date() == today:
        return f"Сегодня {local:%H:%M}"
    if local.date() == today - timedelta(days=1):
        return f"Вчера {local:%H:%M}"
    return f"{local:%d.%m.%Y}"


# ─────────────────────────── сервис ───────────────────────────

SINCE_KEY = "deals_since"


def _since(raw: Any) -> datetime | None:
    """Момент первой сверки из crm.settings: с него закрытые обращения
    тоже становятся сделками, а до него - только открытые."""
    try:
        got = datetime.fromisoformat(str(raw or ""))
    except ValueError:
        return None
    return got if got.tzinfo else got.replace(tzinfo=UTC)


async def _apply(crm: Any, ops: list[dict[str, Any]]) -> int:
    done = 0
    for op in ops:
        try:
            if op["op"] == "create":
                got = await crm.create_deal(stage=op["stage"], at=op.get("at"), by="auto",
                                            **op["fields"])
                done += got is not None
            elif op["op"] == "move":
                done += bool(await crm.move_deal(
                    int(op["id"]), op["stage"], by="auto", at=op.get("at"),
                    close=bool(op.get("close")), fields=op.get("set") or {}))
            elif op["op"] == "link":
                done += bool(await crm.update_deal(int(op["id"]), **op["set"]))
        except Exception:                                # noqa: BLE001
            # Сверка - подсказка доске, а не путь денег: сбой одной сделки
            # (гонка двух окон о тот же уникальный индекс) не валит остальные.
            log.warning("сделка: %s не применена", op, exc_info=True)
    return done


async def sync(crm: Any, *, db: Any = None, now: datetime | None = None) -> int:
    """Сверить сделки с арендами, заявками, обращениями и договорами.
    Идёт при открытии доски и карточки сделки; повтор ничего не задваивает
    (уникальные индексы), а время этапа берётся у события."""
    now = now or datetime.now(UTC)
    settings = await crm.settings()
    since = _since(settings.get(SINCE_KEY))
    if since is None:
        since = now
        await crm.set_setting(SINCE_KEY, now.isoformat(), by="deals")
    changed = 0
    active = await crm.rentals(status="active", limit=10000)
    deals = await crm.deals(open_only=True)
    active_ids = {int(r["id"]) for r in active}
    closed: dict[int, dict] = {}
    for d in deals:
        rid = d.get("rental_id")
        if rid and d.get("stage") in ("rented", "renewed") and int(rid) not in active_ids:
            r = await crm.rental(int(rid))
            if r is not None:
                closed[int(rid)] = r
    changed += await _apply(crm, plan_rentals(deals, active, closed))

    deals = await crm.deals(open_only=True)
    gone: dict[int, dict] = {}
    for d in deals:
        if d.get("booking_id") and d.get("stage") in EARLY:
            b = await crm.booking(int(d["booking_id"]))
            if b is not None and b.get("status") != "new":
                gone[int(d["booking_id"])] = b
    changed += await _apply(crm, plan_bookings(deals, await crm.bookings(status="new"), gone))

    deals = await crm.deals(open_only=True)
    threads = await crm.inbox_threads(limit=3000)
    changed += await _apply(crm, plan_threads(deals, threads, since=since))

    if db is not None:
        deals = await crm.deals(open_only=True)
        signed: dict[int, datetime] = {}
        for d in deals:
            if d.get("stage") not in ("new", "touch", "deferred") or not d.get("client_tg_id"):
                continue
            try:
                user = await db.get_user(int(d["client_tg_id"]))
            except Exception:                            # noqa: BLE001
                continue
            if user and user.get("contract_status") == "signed":
                at = _moment(user.get("contract_signed_at"))
                if at is not None:
                    signed[int(d["client_id"])] = at
        changed += await _apply(crm, plan_contracts(deals, signed))
    await crm.purge_deal_contacts(days=PURGE_DAYS)
    return changed


async def move(crm: Any, deal: Mapping[str, Any], stage: str, *, by: str) -> str | None:
    """Перенос руками. Возвращает причину отказа или None."""
    problem = can_move(deal, stage)
    if problem:
        return problem
    if stage == deal.get("stage"):
        return None
    await crm.move_deal(int(deal["id"]), stage, by=by, close=stage in CLOSED)
    return None


async def quick_add(crm: Any, *, name: str | None, phone: str | None, title: str | None,
                    by: str, responsible_id: int | None = None,
                    location: str | None = None, source: str = "manual") -> int:
    """«Быстрое добавление»: человек позвонил или подошёл. Телефон есть в
    карточках - сделка этого клиента; у него уже идёт сделка - она и есть."""
    phone = bot_logic.normalize_phone(phone) or (phone or "").strip() or None
    client = await quickforms.find_client(crm, phone) if phone else None
    if client is not None:
        for d in await crm.deals(open_only=True, client_id=int(client["id"])):
            return int(d["id"])
    elif phone:
        # Тот же человек позвонил второй раз до карточки - та же сделка.
        for d in await crm.deals(open_only=True):
            if not d.get("client_id") and d.get("phone") == phone:
                return int(d["id"])
    got = await crm.create_deal(stage="new", by=by, client_id=client["id"] if client else None,
                                name=None if client else name, phone=None if client else phone,
                                title=title, source=source, responsible_id=responsible_id,
                                location=location)
    if got is None:
        raise ValueError("сделку не завести")
    return int(got)
