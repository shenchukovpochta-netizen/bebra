"""Процесс демо-стенда: конфиг без боевых ключей, сброс при старте и ночью.

Та же панель, что app.web, с тремя отличиями. Конфиг принудительно
демо: пустые токены бота, банка и хука, даже если их кто-то положил в
окружение. Бота нет вовсе - с ним «Начислить» в демо написало бы людям в
Telegram. Данные пересеиваются при старте и каждую ночь в 04:00 по
Москве, пока панель отвечает 503 (DemoGate): сброс - одна транзакция, и
неудачный оставляет вчерашнее демо целым, а ночной повторяется через
RETRY_AFTER до RETRY_UNTIL.

После сброса день живёт: live_day в свой час открывает смены на точках и
проводит платежи за продления, которые история запланировала на сегодня
(core.later_today), - обычным путём панели, через service. Иначе демо,
засеянное в 04:00, до следующей ночи показывало бы пустое «сегодня».
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

from ..crm import service
from ..web.config import WebConfig
from . import seed, seed_extras
from .world import MSK

log = logging.getLogger("crm.demo")

TITLE = "МАЙБАЙК · демо"
# Час ночного сброса по Москве. Ночью: в это время демо никто не смотрит,
# а числа «за сегодня» к утру снова свежие.
RESET_AT = time(4, 0)
# Неудачный ночной сброс (база на миг недоступна, замок не дождался)
# повторяется: демо, пропустившее ночь, весь день показывало бы вчерашнее
# «сегодня». Повторы - до утра; позже демо уже смотрят, и сброс посреди
# чужого показа хуже вчерашних чисел - тогда до следующей ночи.
RETRY_AFTER = timedelta(minutes=10)
RETRY_UNTIL = time(7, 0)
# Каталоги, куда панель могла бы писать. Загрузки в демо закрыты, а /tmp
# контейнера - место без тома и без чужих данных: ни сканов паспортов,
# ни шаблонов боевой панели сюда не смонтировано и не будет.
SCRATCH = Path("/tmp/mybike-demo")


def demo_config(cfg: WebConfig) -> WebConfig:
    """Конфиг панели в режиме демо. Всё внешнее - пусто, что бы ни было в
    окружении: логин демо публичен, и любой ключ здесь достался бы всем."""
    return dataclasses.replace(
        cfg, demo=True, title=TITLE,
        bot_token="", tochka_token="", tochka_customer_code="",
        inbox_hook_token="", contract_chat_id="", admin_password="",
        # Ссылка на оплату в предпросмотре рассылки - настоящий счёт
        # проката: из демо по ней ушли бы живые деньги.
        pay_url="",
        # Ключ «Входящих» - производный от CRM_SECRET, а не свой секрет:
        # переписка должна читаться, а файл в secrets/ ради вымышленных
        # сообщений - лишняя вещь, которую забудут. Сид шифрует тем же
        # (reset(..., secret=cfg.secret)).
        inbox_key=seed_extras.inbox_key_text(cfg.secret),
        storage_dir=SCRATCH / "kyc", bike_photo_dir=SCRATCH / "bikes",
        doc_dir=SCRATCH / "doctemplates")


def load_config() -> WebConfig:
    return demo_config(WebConfig.load())


def moscow_now() -> datetime:
    return datetime.now(MSK)


def next_reset(now: datetime) -> datetime:
    """Ближайшие 04:00 по Москве строго после now."""
    now = now.astimezone(MSK)
    at = datetime.combine(now.date(), RESET_AT, MSK)
    return at if at > now else at + timedelta(days=1)


async def check_database(pool: Any, database: str) -> None:
    """Сброс сносит схемы crm и bot целиком. Перепутанный адрес базы - это
    боевые клиенты, поэтому до первого DROP две проверки: имя базы - демо,
    и чужих сотрудников в ней нет."""
    if "demo" not in database.lower():
        raise RuntimeError(
            f"база «{database}» не похожа на демо: сброс снёс бы её схемы crm и "
            f"bot. Демо живёт в своей базе (POSTGRES_DB=mybike_demo, postgres-demo)")
    if await pool.fetchval("select to_regclass('crm.staff') is null"):
        return
    if await pool.fetchval("select exists (select 1 from crm.staff) and not exists "
                           "(select 1 from crm.staff where login = 'demo')"):
        raise RuntimeError(
            f"в базе «{database}» есть сотрудники, но нет логина demo: это не "
            f"демо-стенд, сбрасывать её нельзя")


async def has_demo(pool: Any) -> bool:
    """Есть ли что показывать, если сброс не удался: прошлый сид целиком."""
    if await pool.fetchval("select to_regclass('crm.staff') is null"):
        return False
    return bool(await pool.fetchval(
        "select exists (select 1 from crm.staff where login = 'demo')"))


def headline(summary: dict[str, Any]) -> str:
    three = summary.get("three") or {}
    return (f"день {summary.get('today')}, парк {summary.get('bikes')}, "
            f"простой {three.get('idle_percent')} %, чек {three.get('avg_check')} ₽/день, "
            f"расхождения {summary.get('integrity')}")


async def reset(pool: Any, cfg: WebConfig, *, now: datetime | None = None) -> dict[str, Any]:
    """Снести и засеять демо на «сейчас» по Москве. В итоге - и остаток
    дня (summary["later"]) для live_day; в лог он идёт числом."""
    now = (now or moscow_now()).astimezone(MSK)
    started = monotonic()
    summary = await seed.reset(pool, today=now.date(), now=now, secret=cfg.secret)
    log.info("демо засеяно за %.1f с: %s", monotonic() - started,
             headline(summary))
    shown = {**summary, "later": len(summary.get("later") or [])}
    log.info("итог сида: %s", json.dumps(shown, ensure_ascii=False, default=str))
    return summary


async def reset_in_maintenance(app: Any, pool: Any, cfg: WebConfig, *,
                               now: datetime | None = None) -> dict[str, Any] | None:
    """Сброс, пока панель отвечает 503. Итог сида или None: неудача - в
    лог, и демо остаётся вчерашним - транзакция сида откатилась целиком."""
    app.state.maintenance = True
    try:
        return await reset(pool, cfg, now=now)
    except Exception:                                   # noqa: BLE001
        log.exception("сброс демо не удался - остаются прежние данные")
        return None
    finally:
        app.state.maintenance = False


def after_failure(now: datetime, target: datetime) -> datetime:
    """Когда пробовать снова после неудачного сброса: через RETRY_AFTER,
    пока утро того же дня не наступило, иначе - следующей ночью."""
    now = now.astimezone(MSK)
    retry = now + RETRY_AFTER
    if retry.date() == target.astimezone(MSK).date() and retry.time() <= RETRY_UNTIL:
        return retry
    return next_reset(max(now, target))


async def nightly(app: Any, pool: Any, cfg: WebConfig, *,
                  clock: Callable[[], datetime] = moscow_now,
                  sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                  day: DemoDay | None = None) -> None:
    """Каждую ночь в RESET_AT - сброс. Следующий срок считается от
    прошлого, а не только от часов: проснувшись на миг раньше 04:00, круг
    иначе сбросил бы демо дважды подряд. Неудачный - повтор (after_failure).
    day - живой день: до сброса он останавливается (писать в сносимую
    схему нечего), после удачного начинается заново с новым остатком."""
    target = next_reset(clock())
    while True:
        await sleep(max(0.0, (target - clock()).total_seconds()))
        if day is not None:
            day.stop()
        summary = await reset_in_maintenance(app, pool, cfg, now=clock())
        if summary is None:
            target = after_failure(clock(), target)
            log.warning("следующая попытка сброса демо - %s",
                        target.astimezone(MSK).strftime("%d.%m %H:%M"))
            continue
        if day is not None:
            day.start(summary)
        target = next_reset(max(clock(), target))


# ─────────────────────────── живой день ───────────────────────────

async def apply_later(crm: Any, op: dict[str, Any]) -> str:
    """Одна запись остатка дня - тем же путём, что кнопка панели. Состояние
    к этому часу мог поменять посетитель: смену уже открыли, аренду
    закрыли, деньги приняли руками. Тогда запись пропускается - исходное
    и так вернёт ночной сброс. op["balance"] - баланс клиента в начале
    дня (live_day): вырос с тех пор - за клиента уже заплатили. Долг тут
    не мерило: треть продлений платят накануне начисления, при нулевом
    балансе. Возвращает, что сделано."""
    if op["kind"] == "shift":
        if await crm.open_shift_at(op["point"]) is not None:
            return "смена уже открыта"
        await service.open_cash_shift(crm, location=op["point"], opening=op["opening"],
                                      note=None, by=op["by"])
        return "смена открыта"
    if op["kind"] != "payment":
        return "неизвестная запись"
    client = await crm.client(op["client_id"])
    rental = await crm.rental(op["rental_id"])
    if client is None or rental is None or rental.get("status") != "active":
        return "аренды уже нет"
    if op.get("balance") is not None \
            and await crm.client_balance(client["id"]) > op["balance"]:
        return "уже заплатили"
    method, note = op["method"], op["note"]
    if method == "cash" and await service.cash_shift_id(crm, "cash", op["by"]) is None:
        # Кассу закрыли руками - наличные без смены не принимают.
        method, note = "sbp", "Перевод по СБП"
    await service.add_entry(crm, client, kind="payment", amount=op["amount"],
                            method=method, note=note, by=op["by"],
                            rental_id=op["rental_id"] if op["linked"] else None)
    return "платёж"


async def live_day(crm: Any, plan: list[dict[str, Any]], *,
                   clock: Callable[[], datetime] = moscow_now,
                   sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
    """Остаток дня по порядку: каждую запись - в её час. Сбой одной - в
    лог, день идёт дальше. Задачу снимает новый сброс (DemoDay)."""
    plan = [dict(op) for op in plan]
    for op in plan:
        # Баланс сразу после сброса: по нему apply_later узнает, что за
        # клиента уже заплатил посетитель.
        if op["kind"] == "payment":
            op["balance"] = await crm.client_balance(op["client_id"])
    for op in sorted(plan, key=lambda o: o["at"]):
        wait = (op["at"] - clock()).total_seconds()
        if wait > 0:
            await sleep(wait)
        try:
            await apply_later(crm, op)
        except Exception:                               # noqa: BLE001
            log.warning("демо: запись дня %s не сделана", op.get("kind"), exc_info=True)


class DemoDay:
    """Задача живого дня: одна на процесс, новый сброс заменяет её своей."""

    def __init__(self, crm: Any) -> None:
        self.crm = crm
        self.task: asyncio.Task | None = None

    def start(self, summary: dict[str, Any]) -> None:
        self.stop()
        self.task = asyncio.create_task(live_day(self.crm, list(summary.get("later") or [])))

    def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None
