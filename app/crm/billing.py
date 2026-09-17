"""Дневной проход CRM: начисления, напоминания клиентам, сводка оператору.

Живёт в том же расписании, что напоминания бота о сроке (tasks.reminders_loop):
раз в сутки в рабочий час. Начисления идемпотентны (уникальный индекс
на период), поэтому лишний проход безвреден, а пропущенный - догоняется.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from aiogram.exceptions import TelegramAPIError

from .. import i18n, texts
from .. import keyboards as kb
from .. import logic as bot_logic
from . import logic, notices, notify, service

log = logging.getLogger(__name__)

REMIND_KEY = {
    logic.REMIND_SOON: "CAB_REMIND_SOON",
    logic.REMIND_DUE: "CAB_REMIND_DUE",
    logic.REMIND_OVERDUE: "CAB_REMIND_OVERDUE",
}


# Напоминание об аренде - три разных уведомления с разными тумблерами:
# «истекает через N дней», «истекает сегодня» и «просрочка» владелец
# выключает по отдельности, и одним кодом их не накрыть.
REMIND_CODE = {
    logic.REMIND_SOON: "rent_soon",
    logic.REMIND_DUE: "rent_due",
    logic.REMIND_OVERDUE: "rent_overdue",
}


async def remind_once(bot: Any, db: Any, crm: Any, cfg: Any, *,
                      today: date, state: dict | None = None) -> tuple[int, str]:
    """Напоминания по идущим арендам. Возвращает (отправлено, сводка).

    Отметка notified_on ставится и при недоставке: заблокировавший бота
    клиент не должен заставлять систему пытаться снова каждые 15 минут.
    """
    rentals = await crm.active_rentals()
    state = state if state is not None else await notices.settings(crm)
    sent = 0
    for r in rentals:
        kind = logic.reminder_due(r, before_days=cfg.remind_before_days, today=today)
        if kind is None:
            continue
        code = REMIND_CODE[kind]
        if not state.get(code, {}).get("enabled", True):
            # Выключенное уведомление всё равно помечаем отправленным:
            # иначе при обратном включении клиенту прилетит всё, что
            # накопилось за месяц, разом.
            await crm.mark_notified(r["id"], today, kind)
            await notices.record(crm, code, status="skipped",
                                 client_id=r.get("client_id"),
                                 detail="выключено в настройках")
            continue
        await crm.mark_notified(r["id"], today, kind)
        if not r.get("tg_id"):
            await notices.record(crm, code, status="skipped",
                                 client_id=r.get("client_id"),
                                 detail="клиента нет в боте")
            continue                # клиент без Telegram: только в сводку
        summary = logic.rental_summary(r, r.get("balance", 0), today=today)
        lang = "ru"
        try:
            row = await db.get_user(r["tg_id"])
            lang = i18n.user_lang(dict(row) if row else None)
        except Exception:                                # noqa: BLE001
            pass
        text = i18n.t(lang, REMIND_KEY[kind]).format(
            bike=bot_logic.esc(summary.get("bike") or "—"),
            until=summary["covered_until"].strftime("%d.%m.%Y"),
            days=max(summary["days_left"], 0),
            amount=logic.money(logic.topup_hint(summary)),
            debt=logic.money(summary["due"]))
        try:
            await bot.send_message(r["tg_id"], text, reply_markup=kb.cab_topup(lang))
            sent += 1
            await notices.record(crm, code, status="sent",
                                 client_id=r.get("client_id"))
        except TelegramAPIError as exc:
            log.warning("напоминание CRM (%s) клиенту %s не доставлено: %s",
                        kind, r["tg_id"], exc)
            await notices.record(crm, code, status="failed",
                                 client_id=r.get("client_id"), detail=str(exc))
    return sent, logic.digest(rentals, today=today, before_days=cfg.remind_before_days)


async def post_free_bikes(bot: Any, crm: Any, cfg: Any) -> bool:
    """Пост «сегодня свободно» в клиентский канал. False - не постили.

    Свободный велосипед - прямой простой, а канал читают те самые курьеры,
    ради которых парк и стоит. Выключатель - в настройках программы, чтобы
    не спамить в межсезонье.
    """
    if not getattr(cfg, "channel_id", None):
        return False
    settings = await crm.settings()
    if str(settings.get("free_bikes_post", "0")) in ("0", "", "false"):
        return False
    post = logic.free_bikes_post(await crm.bikes(limit=10000),
                                 await crm.tariffs(active_only=True))
    if post is None:
        return False
    try:
        await bot.send_message(cfg.channel_id, texts.FREE_BIKES_POST.format(**post))
    except TelegramAPIError:
        log.exception("пост о свободных велосипедах не доставлен")
        return False
    return True


async def report_search(bot: Any, crm: Any, cfg: Any, *, today: date,
                        chat_id: Any = None) -> int:
    """Кого пора искать - в служебный чат. Возвращает число строк.

    Молчим, когда искать некого: ежедневное «все платят» перестают читать,
    а вместе с ним и то, ради чего сообщение есть.
    """
    settings = logic.search_settings(await crm.settings())
    rows = logic.search_rows(await crm.active_rentals(), settings=settings,
                             today=today)
    lines = logic.search_digest(rows)
    if not lines:
        return 0
    try:
        await bot.send_message(chat_id or cfg.contract_chat_id,
                               texts.SEARCH_DIGEST.format(lines=lines))
        await notices.record(crm, "search_digest", status="sent")
    except TelegramAPIError as exc:
        log.exception("сводка по розыску не доставлена")
        await notices.record(crm, "search_digest", status="failed",
                             detail=str(exc))
    return len(rows["candidates"]) + sum(1 for r in rows["searching"] if r.get("theft"))


async def report_integrity(bot: Any, crm: Any, cfg: Any, *,
                           chat_id: Any = None) -> int:
    """Расхождения - в служебный чат. Возвращает число расхождений.

    Молчим, когда всё сходится: ежедневное «расхождений нет» перестают
    читать через неделю, а вместе с ним и то, ради чего сообщение есть.
    """
    issues = logic.integrity_issues(
        await crm.bikes(limit=10000), await crm.active_rentals(),
        await crm.open_orders_by_bike(), await crm.debtors(200))
    if not issues:
        return 0
    text = texts.INTEGRITY_DIGEST.format(total=len(issues),
                                         lines=logic.integrity_digest(issues))
    try:
        await bot.send_message(chat_id or cfg.contract_chat_id, text)
        await notices.record(crm, "integrity", status="sent")
    except TelegramAPIError as exc:
        log.exception("сводка о расхождениях не доставлена")
        await notices.record(crm, "integrity", status="failed", detail=str(exc))
    return len(issues)


async def invite_to_service(crm: Any, bot: Any, *, state: dict,
                            today: date) -> int:
    """Позвать на ТО тех, кто катается давно и в сервис не заезжал.

    Правило простое и проверяемое: аренда идёт дольше срока из настройки,
    а по её велосипеду за это время нет ни одной записи ремонта. Звать
    чаще раза в срок нельзя - приглашение раз в неделю читается как спам,
    поэтому повтор отсекается историей отправок.
    """
    after = logic.notice_param(state.get("maintenance_invite"), "after_days", 30)
    recent = {r["client_id"] for r in await crm.notice_log(
        code="maintenance_invite", limit=1000)
        if r.get("client_id") and r.get("status") == "sent"
        and (today - r["created_at"].date()).days < after}
    sent = 0
    for rental in await crm.active_rentals():
        if rental.get("client_id") in recent or not rental.get("tg_id"):
            continue
        started = rental.get("started_on")
        if started is None or (today - started).days < after:
            continue
        if rental.get("bike_id") and await crm.repairs_since(
                int(rental["bike_id"]), today - timedelta(days=after)):
            continue                     # был в сервисе - звать незачем
        ok = await notices.send_client(
            crm, "maintenance_invite", rental.get("client_id"),
            lambda r=rental: notify.maintenance_invite(bot, r))
        sent += int(ok)
    return sent


async def ask_for_review(crm: Any, bot: Any, *, state: dict, today: date) -> int:
    """Попросить отзыв у того, кто с нами давно и ничего не должен.

    У должника просить отзыв - гарантированная единица: он как раз
    объяснит, что о нас думает. Поэтому только те, у кого баланс не
    отрицательный. Просим один раз на аренду.
    """
    after = logic.notice_param(state.get("review_ask"), "after_days", 21)
    asked = {r["client_id"] for r in await crm.notice_log(
        code="review_ask", limit=2000)
        if r.get("client_id") and r.get("status") == "sent"}
    links = logic.review_links(await crm.settings())
    sent = 0
    for rental in await crm.active_rentals():
        client_id = rental.get("client_id")
        if client_id in asked or not rental.get("tg_id"):
            continue
        started = rental.get("started_on")
        if started is None or (today - started).days < after:
            continue
        if logic.to_money(rental.get("balance")) < 0:
            continue
        ok = await notices.send_client(
            crm, "review_ask", client_id,
            lambda r=rental: notify.review_ask(bot, r, links))
        sent += int(ok)
    return sent


async def report_silent_estimates(bot: Any, crm: Any, cfg: Any, *,
                                  chat_id: Any = None) -> int:
    """Наряды, которые молчат на согласовании. Молчим, когда таких нет.

    Это самый дорогой простой: техника разобрана, клиент не отвечает, а
    место в сервисе занято. Раньше такой наряд было видно только в
    списке, и то если туда заглянуть.
    """
    rows = [o for o in await crm.work_orders(status="approve", limit=200)
            if logic.estimate_state(o)["too_silent"]]
    if not rows:
        return 0
    lines = [f"🔧 Молчат на согласовании: {len(rows)}"]
    for order in rows[:10]:
        state = logic.estimate_state(order)
        what = order.get("bike_code") or order.get("object_note") or "—"
        lines.append(f"• {order.get('no')} — {what}, "
                     f"{logic.money(order.get('estimate'))}, "
                     f"{state['silent_days']} дн.")
    try:
        await bot.send_message(chat_id or cfg.contract_chat_id, "\n".join(lines))
        await notices.record(crm, "order_waiting", status="sent")
    except TelegramAPIError as exc:
        log.exception("сводка по согласованиям не доставлена")
        await notices.record(crm, "order_waiting", status="failed",
                             detail=str(exc))
        return 0
    return len(rows)


# Сколько дней держать точки трекеров. Две недели назад - это «где он
# ездил на прошлой неделе», дальше вопросов уже не задают.
TRACK_KEEP_DAYS = 30


async def run_daily(bot: Any, db: Any, crm: Any, cfg: Any, *, today: date,
                    now: datetime | None = None,
                    done: dict[str, date] | None = None) -> None:
    """Начислить, напомнить, отчитаться. Каждый шаг отдельно в try:
    сбой одного не должен отменять остальные.

    `now` и `done` - расписание уведомлений: у каждого свой час, а
    `done` помнит, что уже уходило сегодня. Без них проход считается
    ручным и делает всё включённое сразу: так его зовут из панели и из
    тестов, и так он работал до появления расписания.
    """
    now = now or datetime.now()
    state = await notices.settings(crm)
    manual = done is None

    def due(code: str) -> bool:
        """Включено и пора. В ручном проходе - просто «включено»."""
        if not state.get(code, {}).get("enabled", True):
            return False
        return True if manual else notices.due(state, code, now, done)

    def chat(code: str) -> Any:
        return notices.chat_for(state, code, cfg.contract_chat_id)

    # Начисления - не уведомление, тумблера у них нет: это деньги.
    # Один раз в сутки, в тот же час, что и раньше.
    if manual or done.get("charge") != today:
        notices.mark(done, "charge", today)
        try:
            charged = await service.charge_all(crm, today=today)
            if charged:
                log.info("CRM: начислений сделано %s", charged)
        except Exception:                                # noqa: BLE001
            log.exception("CRM: проход начислений не удался")

    digest = ""
    # Напоминания об аренде: три кода, но один проход по арендам -
    # второй раз читать их незачем. Пора хотя бы одному - идём.
    if any(due(code) for code in REMIND_CODE.values()):
        for code in REMIND_CODE.values():
            if due(code):
                notices.mark(done, code, today)
        try:
            sent, digest = await remind_once(bot, db, crm, cfg, today=today,
                                             state=state)
            if sent:
                log.info("CRM: напоминаний об оплате отправлено %s", sent)
        except Exception:                                # noqa: BLE001
            log.exception("CRM: проход напоминаний не удался")
            return

    if due("search_digest"):
        notices.mark(done, "search_digest", today)
        try:
            hunted = await report_search(bot, crm, cfg, today=today,
                                         chat_id=chat("search_digest"))
            if hunted:
                log.info("CRM: строк розыска отправлено %s", hunted)
        except Exception:                                # noqa: BLE001
            log.exception("CRM: сводка по розыску не собрана")

    if due("integrity"):
        notices.mark(done, "integrity", today)
        try:
            found = await report_integrity(bot, crm, cfg,
                                           chat_id=chat("integrity"))
            if found:
                log.info("CRM: расхождений в данных найдено %s", found)
        except Exception:                                # noqa: BLE001
            log.exception("CRM: проверка расхождений не удалась")

    if due("free_bikes"):
        notices.mark(done, "free_bikes", today)
        try:
            if await post_free_bikes(bot, crm, cfg):
                log.info("CRM: пост о свободных велосипедах отправлен")
        except Exception:                                # noqa: BLE001
            log.exception("CRM: пост о свободных велосипедах не собран")

    if due("order_waiting"):
        notices.mark(done, "order_waiting", today)
        try:
            silent = await report_silent_estimates(bot, crm, cfg,
                                                   chat_id=chat("order_waiting"))
            if silent:
                log.info("CRM: нарядов молчит на согласовании %s", silent)
        except Exception:                                # noqa: BLE001
            log.exception("CRM: сводка по согласованиям не собрана")

    if due("maintenance_invite"):
        notices.mark(done, "maintenance_invite", today)
        try:
            called = await invite_to_service(crm, bot, state=state, today=today)
            if called:
                log.info("CRM: приглашений на ТО отправлено %s", called)
        except Exception:                                # noqa: BLE001
            log.exception("CRM: приглашения на ТО не собраны")

    if due("review_ask"):
        notices.mark(done, "review_ask", today)
        try:
            asked = await ask_for_review(crm, bot, state=state, today=today)
            if asked:
                log.info("CRM: просьб об отзыве отправлено %s", asked)
        except Exception:                                # noqa: BLE001
            log.exception("CRM: просьбы об отзыве не собраны")

    # Чистки - тоже не уведомления: расходный материал базы.
    if manual or done.get("purge") != today:
        notices.mark(done, "purge", today)
        try:
            # Журнал позиций трекеров - расходный материал: точка на каждый
            # опрос за месяц даёт десятки тысяч строк на велосипед.
            dropped = await crm.purge_tracker_positions(TRACK_KEEP_DAYS)
            if dropped:
                log.info("CRM: старых точек трекеров удалено %s", dropped)
            old = await crm.purge_notice_log(logic.NOTICE_LOG_DAYS)
            if old:
                log.info("CRM: старых записей истории отправок удалено %s", old)
        except Exception:                                # noqa: BLE001
            log.exception("CRM: чистка журналов не удалась")

    if digest and due("daily_digest"):
        notices.mark(done, "daily_digest", today)
        text = (texts.CAB_DIGEST_INTRO.format(today=today.strftime("%d.%m.%Y"))
                + "\n\n" + digest)
        for part in bot_logic.split_message(text):
            try:
                await bot.send_message(chat("daily_digest"), part)
            except TelegramAPIError:
                log.exception("сводка по оплатам не доставлена")
                await notices.record(crm, "daily_digest", status="failed")
                return
        await notices.record(crm, "daily_digest", status="sent")
