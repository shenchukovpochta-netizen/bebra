"""Фоновые задачи: ретеншен и напоминания о сроке аренды."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, date, datetime, time, tzinfo
from typing import Any

from aiogram.exceptions import TelegramAPIError

from . import i18n, logic, texts
from . import keyboards as kb
from .config import Config
from .crm import logic as crm_logic
from .crm import points
from .db import Database, utcnow
from .services import files

log = logging.getLogger(__name__)

INTERVAL_SECONDS = 6 * 3600
# Напоминания проверяются чаще ретеншена: пропущенный из-за перезапуска
# час не должен стоить клиенту целого дня молчания.
REMIND_INTERVAL_SECONDS = 900
# Память дневного прохода «что сегодня уже делали» в crm.settings: код
# уведомления -> местная дата. В переменных цикла её обнулял перезапуск,
# и бот, поднятый после часа сводки, слал сводки и пост о свободных
# велосипедах второй раз. Отметка прохода самого бота - ключом BOT_MARK.
DONE_KEY = "notice_done"
BOT_MARK = "bot_remind"

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


def _row_paths(row: Any) -> list[str]:
    return [p for p in (row["doc_path"], row["doc2_path"], row["parent_path"],
                        row["contract_path"], row["soglasie_path"],
                        row["act_in_path"], row["act_out_path"],
                        row["buyout_path"]) if p]


def _remove_files(row: Any, paths: list[str], cfg: Config) -> bool:
    """Удалить файлы строки. False - ссылки в базе трогать нельзя."""
    # Путь пришёл из своей же базы, но перед удалением всё равно сверяется
    # с шаблоном: одна опечатка в запросе - и rm уедет не туда.
    unsafe = [p for p in paths if not logic.is_safe_store_path(p, cfg.storage_dir)]
    if unsafe:
        log.error("подозрительный путь у %s: %s - пропускаю", row["tg_id"], unsafe)
        return False
    # Не удалилось - ссылку в базе не трогаем, иначе файл останется
    # на диске навсегда и без следа. Строка уедет на следующий прогон.
    return all(files.remove(p) for p in paths)


async def purge_once(db: Database, cfg: Config) -> tuple[int, int]:
    """Возвращает (удалено файлов, удалено записей журнала).

    Два правила. Первое - по `purge_after`: его ставят одобрение, отказ и
    подписи. Второе - брошенная регистрация (`db.stale_registrations`):
    анкета и сканы того, кто не дошёл до подписи договора, по сроку
    бездействия `purge_stale_days`. Правило общее для Telegram и MAX:
    у обоих ботов один `bot.users` и один этот проход.
    """
    removed = 0
    for row in await db.rows_to_purge():
        paths = _row_paths(row)
        if not _remove_files(row, paths, cfg):
            continue
        await db.clear_files(row["tg_id"])
        await db.log_event(row["tg_id"], "files_purged", {"count": len(paths)})
        removed += len(paths)

    stale_days = getattr(cfg, "purge_stale_days", 30)
    for row in await db.stale_registrations(
            logic.UNFINISHED_STATES, stale_days,
            max(stale_days, cfg.purge_approved_days)):
        paths = _row_paths(row)
        if not _remove_files(row, paths, cfg):
            continue
        if not await db.clear_stale_registration(row["tg_id"], row["updated_at"]):
            continue            # человек вернулся между выборкой и чисткой
        await db.log_event(row["tg_id"], "stale_registration_purged",
                           {"count": len(paths)})
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
                           today: date | None = None, crm: Any = None) -> bool:
    """Одно напоминание клиенту. False - не доставлено (бот заблокирован).

    Отметка о напоминании ставится в любом случае: иначе заблокировавший
    бота человек заставлял бы систему пытаться снова каждые пятнадцать
    минут до конца времён.
    «Скоро заканчивается» называет часы точки этой аренды (из CRM), а не
    общие: у точек справочника режим свой.
    """
    lang = i18n.user_lang(row)
    given = logic.issue_context(row.get("issue_data"))
    until = row["rent_until"]
    hours = ""
    if stage == logic.REMIND_SOON:
        hours = points.hours_note(lang, await points.rental_location(crm, row["tg_id"]))
    text = i18n.t(lang, REMIND_TEXT[stage]).format(
        bike=logic.esc(given["bike_model"]),
        until=until.strftime("%d.%m.%Y"),
        days=max(logic.days_left(until, today=today) or 0, 0),
        hours=hours,
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
                      today: date | None = None, crm: Any = None) -> int:
    """Проверить графики выкупа и выдать акт тем, кто выплатил всё.

    Живёт в том же дневном проходе, что напоминания: выкуп копится по
    оплаченным дням, и «выплачено полностью» - это событие календаря,
    а не нажатие кнопки. Акт выдаётся один раз (buyout_done_at).
    """
    from .crm import company, doctemplates
    from .handlers import contract as contract_handlers

    # Снимки реквизитов и своих шаблонов освежает конвейер апдейтов, а
    # этот проход идёт мимо него: после перезапуска бота первый же круг
    # собрал бы акт с прочерками вместо арендодателя и по нашему шаблону
    # вместо загруженного владельцем.
    await company.refresh(crm)
    await doctemplates.refresh(crm, getattr(cfg, "doc_dir", None))
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
                      today: date | None = None, crm: Any = None) -> tuple[int, str]:
    """Один проход напоминаний. Возвращает (сколько отправлено, сводка).

    Сводка возвращается наружу, а не шлётся здесь: решение о том, слать ли
    её сегодня, принимает вызывающий - оператору она нужна раз в день,
    а проход идёт каждые пятнадцать минут.
    """
    rows = [dict(r) for r in await db.active_rentals()]
    if crm is not None:
        # Снимок точек обновляет конвейер апдейтов, а в час напоминаний
        # апдейтов может не быть с самого перезапуска.
        await points.refresh(crm)
    sent = 0
    for row in rows:
        stage = logic.reminder_due(row, before_days=cfg.remind_before_days,
                                   today=today)
        if stage is None:
            continue
        await _notify_deadline(bot, db, row, stage, today=today, crm=crm)
        sent += 1
    return sent, logic.deadline_digest(rows, today=today)


def due_today(now: datetime, last_run_on: date | None, hour: int,
              tz: tzinfo | None = None) -> bool:
    """Пора ли делать дневной проход напоминаний.

    Не «ровно в этот час», а «в этот час или позже, если сегодня ещё
    не делали»: бота перезапускают среди дня, и привязка к одному часу
    молча съедала бы напоминания за целые сутки.

    Час в настройке - по UTC (REMIND_HOUR_UTC), а сутки - местные
    (Europe/Moscow, `tz` - для тестов): по дате UTC перезапуск между
    00:00 и 03:00 по Москве видел «вчера, час уже прошёл» и делал проход
    ночью.

    И не позже вечера (crm_logic.too_late_for_clients, то же окно, что у
    уведомлений CRM): проход шлёт клиентам «аренда заканчивается» и акты
    выкупа, и бот, поднятый в 23:30 после простоя, будил бы их. Сегодня
    тогда пропускается, завтра проход идёт в свой час.
    """
    if not 0 <= hour <= 23:
        return False
    local = now.astimezone(tz)
    start = datetime.combine(local.date(), time(hour), tzinfo=UTC).astimezone(tz).hour
    if crm_logic.too_late_for_clients(local.hour, start):
        return False
    return local.hour >= start and last_run_on != local.date()


def parse_done(raw: str | None) -> dict[str, date]:
    """Память прохода из crm.settings; мусор отбрасывается, а не роняет круг."""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    out: dict[str, date] = {}
    if isinstance(data, dict):
        for code, day in data.items():
            try:
                out[str(code)] = date.fromisoformat(str(day))
            except ValueError:
                continue
    return out


def dump_done(done: dict[str, date], bot_done_on: date | None) -> str:
    marks = {code: day.isoformat() for code, day in done.items()}
    if bot_done_on is not None:
        marks[BOT_MARK] = bot_done_on.isoformat()
    return json.dumps(marks, sort_keys=True)


async def _remember(crm: Any, done: dict[str, date], bot_done_on: date | None,
                    saved: str) -> str:
    """Записать память прохода, если она изменилась. Возвращает записанное.

    Сбой записи круг не валит: память в переменных остаётся, повтор
    грозит только после перезапуска - как раньше.
    """
    value = dump_done(done, bot_done_on)
    if crm is None or value == saved:
        return saved
    try:
        await crm.set_setting(DONE_KEY, value, by="bot")
    except Exception:                                   # noqa: BLE001
        log.warning("память дневного прохода не записана", exc_info=True)
        return saved
    return value


async def reminders_loop(bot: Any, db: Database, cfg: Config,
                         vault: Any = None, crm: Any = None) -> None:
    """Напоминания клиентам и ежедневная сводка оператору.

    Раз в сутки, в «рабочий» час: сообщение о конце аренды в три ночи
    бесит и не читается. Сводка уходит тем же проходом и только если
    в ней есть строки.
    """
    # Отметка «сегодня сделано» ставится только после удачного прохода:
    # один сбой базы или Telegram в назначенный час иначе оставлял клиентов
    # без напоминаний на сутки. Повтор безопасен - каждое напоминание
    # помечается в базе и второй раз не уходит. У бота и CRM отметки свои:
    # сбой одного не должен ни отменять, ни повторять проход другого.
    bot_done_on: date | None = None
    # У CRM теперь не одна отметка на сутки, а по отметке на уведомление:
    # у каждого свой час, и «сводка в 20:00» не должна ждать, пока
    # напоминание в 09:00 отработает.
    crm_done: dict[str, date] = {}
    # Обе памяти переживают перезапуск в crm.settings (DONE_KEY). Пока
    # она не прочитана, проход не идёт: с пустой памятью сводки ушли бы
    # второй раз. Без CRM читать неоткуда - память только в цикле.
    recalled = crm is None
    saved = ""
    while True:
        try:
            if not recalled:
                saved = (await crm.settings()).get(DONE_KEY) or ""
                crm_done = parse_done(saved)
                bot_done_on = crm_done.pop(BOT_MARK, None)
                recalled = True
            now = datetime.now(UTC)
            today = logic.local_date(now)
            if due_today(now, bot_done_on, cfg.remind_hour_utc):
                try:
                    sent, digest = await remind_once(bot, db, cfg, today=today,
                                                     crm=crm)
                    if sent:
                        log.info("напоминаний о сроке отправлено: %s", sent)
                    if vault is not None:
                        done = await buyout_once(bot, db, cfg, vault, today=today,
                                                 crm=crm)
                        if done:
                            log.info("актов выкупа выдано: %s", done)
                    if digest:
                        await _send_digest(bot, cfg, digest, today)
                    bot_done_on = today
                    saved = await _remember(crm, crm_done, bot_done_on, saved)
                except asyncio.CancelledError:
                    raise
                except Exception:                       # noqa: BLE001
                    log.exception("прогон напоминаний не удался, повтор через "
                                  "%s с", REMIND_INTERVAL_SECONDS)
            if crm is not None:
                # Проход CRM зовётся каждый круг, а что именно делать -
                # решает он сам по расписанию уведомлений. Начисления и
                # чистки внутри всё так же раз в сутки.
                from .crm import billing, waitlist
                local = datetime.now()
                try:
                    await billing.run_daily(bot, db, crm, cfg, today=local.date(),
                                            now=local, done=crm_done)
                finally:
                    saved = await _remember(crm, crm_done, bot_done_on, saved)
                # Лист ожидания - каждый круг, а не раз в сутки: велосипед
                # освобождается когда угодно, и звать к нему надо в тот же
                # день. Свой try: сбой сверки не должен отменять проход.
                try:
                    called = await waitlist.run_once(bot, db, crm, now=local)
                    if called:
                        log.info("лист ожидания: позвали клиентов %s", called)
                except asyncio.CancelledError:
                    raise
                except Exception:                       # noqa: BLE001
                    log.exception("лист ожидания не сверен")
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("дневной проход CRM не удался")
        await asyncio.sleep(REMIND_INTERVAL_SECONDS)


async def _send_digest(bot: Any, cfg: Config, digest: str, today: date) -> None:
    """Сводка оператору - при необходимости несколькими сообщениями.

    На четвёртом десятке аренд сводка перестаёт влезать в лимит Telegram,
    и одно длинное сообщение не доходит целиком - молча, с одной строкой
    в логе. Оператор при этом уверен, что просрочек нет.
    """
    text = (texts.DIGEST_INTRO.format(today=today.strftime("%d.%m.%Y"))
            + "\n\n" + digest)
    for part in logic.split_message(text):
        try:
            await bot.send_message(cfg.contract_chat_id, part)
        except TelegramAPIError:
            log.exception("сводка по срокам не доставлена")
            return
