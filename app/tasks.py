"""Ретеншен: удаление сканов и договоров по сроку, чистка журнала апдейтов."""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from . import logic, texts
from .config import Config
from .db import Database
from .services import files

log = logging.getLogger(__name__)

INTERVAL_SECONDS = 6 * 3600

# Ссылки на живые фоновые задачи. Без них сборщик мусора вправе уничтожить
# задачу на середине: событийный цикл держит только слабую ссылку. Симптом -
# скан не сохранился, договор не собрался, и ни строчки в логах.
_background: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def drain(timeout: float = 10.0) -> None:
    """Дать фоновым задачам доработать при остановке."""
    if not _background:
        return
    log.info("жду завершения фоновых задач: %s", len(_background))
    await asyncio.wait(set(_background), timeout=timeout)


async def purge_once(db: Database, cfg: Config) -> tuple[int, int]:
    """Возвращает (удалено файлов, удалено записей журнала)."""
    removed = 0
    for row in await db.rows_to_purge():
        paths = [p for p in (row["doc_path"], row["parent_path"],
                             row["contract_path"], row["soglasie_path"],
                             row["act_in_path"], row["act_out_path"]) if p]
        # Путь пришёл из своей же базы, но перед удалением всё равно сверяется
        # с шаблоном: одна опечатка в запросе - и rm уедет не туда.
        unsafe = [p for p in paths if not logic.is_safe_store_path(p, cfg.storage_dir)]
        if unsafe:
            log.error("подозрительный путь у %s: %s - пропускаю", row["tg_id"], unsafe)
            continue
        if not all(files.remove(p) for p in paths):
            # Не удалилось - ссылку в базе не трогаем, иначе файл останется
            # на диске навсегда и без следа. Строка уедет на следующий прогон.
            continue
        await db.clear_files(row["tg_id"])
        await db.log_event(row["tg_id"], "files_purged", {"count": len(paths)})
        removed += len(paths)

    pruned = await db.prune_updates_log(cfg.updates_log_days)
    return removed, pruned


async def retention_loop(db: Database, cfg: Config) -> None:
    while True:
        try:
            removed, pruned = await purge_once(db, cfg)
            if removed or pruned:
                log.info("ретеншен: удалено файлов %s, записей журнала %s", removed, pruned)
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("прогон ретеншена не удался")
        await asyncio.sleep(INTERVAL_SECONDS)


# Просроченные удержания снимаются раз в минуту: бронь живёт часами,
# и минута опоздания никому не мешает, а чаще - лишние запросы к базе.
FLEET_INTERVAL_SECONDS = 60


async def fleet_loop(fleet, bot=None, admin_chat_id=None) -> None:
    """Снятие просроченных броней парка и зов на плановое ТО."""
    while True:
        try:
            expired = await fleet.expire_holds()
            if expired:
                log.info("парк: снято просроченных броней: %s", expired)
            await _service_calls(fleet, bot, admin_chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("прогон задач парка не удался")
        await asyncio.sleep(FLEET_INTERVAL_SECONDS)


async def _service_calls(fleet, bot, admin_chat_id) -> None:
    """Карточка «пора на плановое ТО» оператору - один раз на цикл ТО.

    service_notified_at не даёт слать её каждый прогон: повторный зов
    случится только после отметки «ТО проведено» и нового цикла.
    """
    if bot is None or admin_chat_id is None:
        return
    from datetime import datetime
    for row in await fleet.bikes_service_due():
        if (row["service_notified_at"] is not None
                and row["service_notified_at"] >= row["last_service_at"]):
            continue
        last = row["last_service_at"]
        now = datetime.now(last.tzinfo) if last.tzinfo else datetime.now()
        overdue_days = (now - last).days
        try:
            await bot.send_message(admin_chat_id, texts.FLEET_SERVICE_CARD.format(
                bike_id=row["bike_id"],
                model=logic.esc(row["model"] or "без модели"),
                client=logic.esc(row["client_name"] or "—"),
                phone=logic.esc(row["client_phone"] or "—"),
                days=overdue_days))
            await fleet.mark_service_notified(row["bike_id"])
        except Exception:                               # noqa: BLE001
            log.exception("карточка ТО по единице %s не доставлена",
                          row["bike_id"])


PAYMENTS_INTERVAL_SECONDS = 60
PAYMENT_MAX_AGE_HOURS = 24        # столько же живёт QR в Точке (ttl)


async def check_payments_once(fleet, tochka, bot=None, admin_chat_id=None,
                              starline=None) -> int:
    """Один проход по неоплаченным счетам. Возвращает число оплаченных.

    Оплата подтверждается банком, а не словами клиента: статус QR
    спрашивается у Точки. Оплаченный счёт снимает блокировку StarLine,
    если единица была обездвижена за неоплату, - клиент не должен ждать
    оператора, чтобы поехать после оплаты.
    """
    paid = 0
    for p in await fleet.pending_payments(max_age_hours=PAYMENT_MAX_AGE_HOURS):
        status = await tochka.payment_status(p["qrc_id"])
        if status == "pending":
            continue
        row = await fleet.mark_payment(p["id"], "paid" if status == "paid"
                                       else "cancelled")
        if row is None or status != "paid":
            continue
        paid += 1
        log.info("оплата: счёт #%s на %s ₽ оплачен", p["id"], p["amount"])
        await _after_paid(fleet, row, bot, admin_chat_id, starline)
    for p in await fleet.stale_payments(max_age_hours=PAYMENT_MAX_AGE_HOURS):
        await fleet.mark_payment(p["id"], "expired")
    return paid


async def _after_paid(fleet, payment, bot, admin_chat_id, starline) -> None:
    """Всё, что следует за оплатой: карточка оператору, сообщение клиенту,
    разблокировка. Каждый шаг сам по себе: сбой одного не съедает остальные."""
    rental = (await fleet.rental_brief(payment["rental_id"])
              if payment["rental_id"] else None)
    if bot is not None and admin_chat_id is not None:
        try:
            await bot.send_message(admin_chat_id, texts.FLEET_PAID_CARD.format(
                amount=payment["amount"],
                client=logic.esc((rental and rental["client_name"]) or "—"),
                number=logic.esc((rental and rental["contract_no"]) or "—")))
        except Exception:                               # noqa: BLE001
            log.exception("карточка оплаты #%s не доставлена", payment["id"])
    if bot is not None and payment["tg_id"]:
        try:
            await bot.send_message(payment["tg_id"],
                                   texts.FLEET_PAID_CLIENT.format(
                                       amount=payment["amount"]))
        except Exception:                               # noqa: BLE001
            log.exception("клиент %s не узнал об оплате", payment["tg_id"])
    if (starline is not None and rental is not None
            and rental["bike_id"] and rental["blocked"]):
        bike = await fleet.get_bike(str(rental["bike_id"]))
        device = bike and bike["starline_device_id"]
        if device:
            ok = await starline.unblock(device)
            await fleet.log_starline(rental["bike_id"], device, "unblock", ok,
                                     "оплата счёта" if ok
                                     else "команда StarLine не прошла", None)
            if ok:
                await fleet.mark_blocked(rental["bike_id"], blocked=False,
                                         reason=None)
                log.info("StarLine: единица %s разблокирована после оплаты",
                         rental["bike_id"])


async def payments_loop(fleet, tochka, bot=None, admin_chat_id=None,
                        starline=None) -> None:
    """Опрос статусов СБП-счетов раз в минуту - вебхуку нужен публичный
    адрес, а минутный опрос платежей не теряет и устроен как весь бот."""
    while True:
        try:
            await check_payments_once(fleet, tochka, bot, admin_chat_id, starline)
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("прогон проверки оплат не удался")
        await asyncio.sleep(PAYMENTS_INTERVAL_SECONDS)


# Автоблокировка реже броней: просрочка меряется сутками, и минутный такт
# здесь только зря дёргал бы StarLine и базу.
STARLINE_INTERVAL_SECONDS = 5 * 60


async def starline_loop(fleet, starline, bot=None, admin_chat_id=None) -> None:
    """Автоблокировка просроченных аренд через StarLine.

    Запускается ТОЛЬКО при STARLINE_AUTO_BLOCK=1. Блокирует единицу один
    раз (флаг blocked это гарантирует) и зовёт оператора карточкой -
    обездвиживание клиента не должно происходить бесшумно.
    """
    while True:
        try:
            for row in await fleet.bikes_to_autoblock(date.today()):
                device = row["starline_device_id"]
                ok = await starline.block(device)
                await fleet.log_starline(
                    row["bike_id"], device, "block", ok,
                    "автоблокировка: просрочка" if ok else "авто: команда не прошла",
                    None)
                if not ok:
                    log.warning("StarLine: автоблок единицы %s не прошёл", row["bike_id"])
                    continue
                await fleet.mark_blocked(row["bike_id"], blocked=True,
                                         reason="автоблокировка: просрочка")
                log.info("StarLine: автоблок единицы %s (просрочка)", row["bike_id"])
                if bot is not None and admin_chat_id is not None:
                    try:
                        await bot.send_message(
                            admin_chat_id, texts.FLEET_AUTOBLOCK_CARD.format(
                                bike_id=row["bike_id"],
                                model=logic.esc(row["model"] or "без модели"),
                                client=logic.esc(row["client_name"] or "—"),
                                due=str(row["due_at"] or "")))
                    except Exception:                   # noqa: BLE001
                        log.exception("карточка автоблока %s не доставлена",
                                      row["bike_id"])
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("прогон автоблокировки StarLine не удался")
        await asyncio.sleep(STARLINE_INTERVAL_SECONDS)
