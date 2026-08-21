"""Фоновые задачи: ретеншен и напоминания о сроке аренды."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any

from aiogram.exceptions import TelegramAPIError

from . import i18n
from . import keyboards as kb
from . import logic, texts
from .config import Config
from .db import Database, utcnow
from .services import files

log = logging.getLogger(__name__)

INTERVAL_SECONDS = 6 * 3600
# Напоминания проверяются чаще ретеншена: пропущенный из-за перезапуска
# час не должен стоить клиенту целого дня молчания.
REMIND_INTERVAL_SECONDS = 900

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
                             row["act_in_path"], row["act_out_path"],
                             row["buyout_path"]) if p]
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


# ─────────────────────── напоминания о сроке ───────────────────────

REMIND_TEXT = {
    logic.REMIND_SOON: "REMIND_SOON",
    logic.REMIND_LAST: "REMIND_LAST_DAY",
    logic.REMIND_OVERDUE: "REMIND_OVERDUE",
}


async def _notify_deadline(bot: Any, db: Database, row: dict, stage: str, *,
                           today: date | None = None) -> bool:
    """Одно напоминание клиенту. False - не доставлено (бот заблокирован).

    Отметка о напоминании ставится в любом случае: иначе заблокировавший
    бота человек заставлял бы систему пытаться снова каждые пятнадцать
    минут до конца времён.
    """
    lang = i18n.user_lang(row)
    given = logic.issue_context(row.get("issue_data"))
    until = row["rent_until"]
    text = i18n.t(lang, REMIND_TEXT[stage]).format(
        bike=logic.esc(given["bike_model"]),
        until=until.strftime("%d.%m.%Y"),
        days=max(logic.days_left(until, today=today) or 0, 0),
    )
    delivered = True
    try:
        await bot.send_message(row["tg_id"], text, reply_markup=kb.extend(lang))
    except TelegramAPIError as exc:
        log.warning("напоминание %s для %s не доставлено: %s",
                    stage, row["tg_id"], exc)
        delivered = False
    await db.patch(row["tg_id"], **{logic.REMIND_FIELD[stage]: utcnow()})
    await db.log_event(row["tg_id"], "rent_reminder",
                       {"stage": stage, "delivered": delivered})
    return delivered


async def buyout_once(bot: Any, db: Database, cfg: Config, vault: Any, *,
                      today: date | None = None) -> int:
    """Проверить графики выкупа и выдать акт тем, кто выплатил всё.

    Живёт в том же дневном проходе, что напоминания: выкуп копится по
    оплаченным дням, и «выплачено полностью» - это событие календаря,
    а не нажатие кнопки. Акт выдаётся один раз (buyout_done_at).
    """
    from .handlers import contract as contract_handlers

    sent = 0
    for record in await db.active_rentals():
        row = dict(record)
        if row.get("buyout_done_at"):
            continue
        if row.get("state") != logic.APPROVED:
            # Клиент сейчас в диалоге: подписывает акт возврата, платит
            # за продление, пишет в поддержку. Перевод в подпись выкупа
            # оборвал бы это на середине - подождём до следующего прохода,
            # выкуп от одного дня не убежит.
            continue
        state = logic.buyout_state(row, today=today)
        if state is None or not state["done"]:
            continue
        try:
            await contract_handlers.send_buyout_act(bot, db, cfg, vault,
                                                    row["tg_id"])
            sent += 1
        except Exception:                               # noqa: BLE001
            # Акт не собрался или не ушёл - остальные клиенты не должны
            # страдать из-за одного; попробуем на следующем проходе.
            log.exception("акт выкупа для %s не выдан", row["tg_id"])
    return sent


async def remind_once(bot: Any, db: Database, cfg: Config, *,
                      today: date | None = None) -> tuple[int, str]:
    """Один проход напоминаний. Возвращает (сколько отправлено, сводка).

    Сводка возвращается наружу, а не шлётся здесь: решение о том, слать ли
    её сегодня, принимает вызывающий - оператору она нужна раз в день,
    а проход идёт каждые пятнадцать минут.
    """
    rows = [dict(r) for r in await db.active_rentals()]
    sent = 0
    for row in rows:
        stage = logic.reminder_due(row, before_days=cfg.remind_before_days,
                                   today=today)
        if stage is None:
            continue
        await _notify_deadline(bot, db, row, stage, today=today)
        sent += 1
    return sent, logic.deadline_digest(rows, today=today)


def due_today(now: datetime, last_run_on: date | None, hour: int) -> bool:
    """Пора ли делать дневной проход напоминаний.

    Не «ровно в этот час», а «в этот час или позже, если сегодня ещё
    не делали»: бота перезапускают среди дня, и привязка к одному часу
    молча съедала бы напоминания за целые сутки.
    """
    return now.hour >= hour and last_run_on != now.date()


async def reminders_loop(bot: Any, db: Database, cfg: Config,
                         vault: Any = None) -> None:
    """Напоминания клиентам и ежедневная сводка оператору.

    Раз в сутки, в «рабочий» час: сообщение о конце аренды в три ночи
    бесит и не читается. Сводка уходит тем же проходом и только если
    в ней есть строки.
    """
    last_run_on: date | None = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            if due_today(now, last_run_on, cfg.remind_hour_utc):
                last_run_on = now.date()
                sent, digest = await remind_once(bot, db, cfg, today=now.date())
                if sent:
                    log.info("напоминаний о сроке отправлено: %s", sent)
                if vault is not None:
                    done = await buyout_once(bot, db, cfg, vault,
                                             today=now.date())
                    if done:
                        log.info("актов выкупа выдано: %s", done)
                if digest:
                    await _send_digest(bot, cfg, digest, now.date())
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("прогон напоминаний не удался")
        await asyncio.sleep(REMIND_INTERVAL_SECONDS)


async def _send_digest(bot: Any, cfg: Config, digest: str, today: date) -> None:
    try:
        await bot.send_message(
            cfg.contract_chat_id,
            texts.DIGEST_INTRO.format(today=today.strftime("%d.%m.%Y"))
            + "\n\n" + digest)
    except TelegramAPIError:
        log.exception("сводка по срокам не доставлена")
