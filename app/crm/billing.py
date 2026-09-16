"""Дневной проход CRM: начисления, напоминания клиентам, сводка оператору.

Живёт в том же расписании, что напоминания бота о сроке (tasks.reminders_loop):
раз в сутки в рабочий час. Начисления идемпотентны (уникальный индекс
на период), поэтому лишний проход безвреден, а пропущенный - догоняется.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from aiogram.exceptions import TelegramAPIError

from .. import i18n, texts
from .. import keyboards as kb
from .. import logic as bot_logic
from . import logic, service

log = logging.getLogger(__name__)

REMIND_KEY = {
    logic.REMIND_SOON: "CAB_REMIND_SOON",
    logic.REMIND_DUE: "CAB_REMIND_DUE",
    logic.REMIND_OVERDUE: "CAB_REMIND_OVERDUE",
}


async def remind_once(bot: Any, db: Any, crm: Any, cfg: Any, *,
                      today: date) -> tuple[int, str]:
    """Напоминания по идущим арендам. Возвращает (отправлено, сводка).

    Отметка notified_on ставится и при недоставке: заблокировавший бота
    клиент не должен заставлять систему пытаться снова каждые 15 минут.
    """
    rentals = await crm.active_rentals()
    sent = 0
    for r in rentals:
        kind = logic.reminder_due(r, before_days=cfg.remind_before_days, today=today)
        if kind is None:
            continue
        await crm.mark_notified(r["id"], today, kind)
        if not r.get("tg_id"):
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
        except TelegramAPIError as exc:
            log.warning("напоминание CRM (%s) клиенту %s не доставлено: %s",
                        kind, r["tg_id"], exc)
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


async def report_integrity(bot: Any, crm: Any, cfg: Any) -> int:
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
        await bot.send_message(cfg.contract_chat_id, text)
    except TelegramAPIError:
        log.exception("сводка о расхождениях не доставлена")
    return len(issues)


async def run_daily(bot: Any, db: Any, crm: Any, cfg: Any, *, today: date) -> None:
    """Начислить, напомнить, отчитаться. Каждый шаг отдельно в try:
    сбой одного не должен отменять остальные."""
    try:
        charged = await service.charge_all(crm, today=today)
        if charged:
            log.info("CRM: начислений сделано %s", charged)
    except Exception:                                    # noqa: BLE001
        log.exception("CRM: проход начислений не удался")
    try:
        sent, digest = await remind_once(bot, db, crm, cfg, today=today)
        if sent:
            log.info("CRM: напоминаний об оплате отправлено %s", sent)
    except Exception:                                    # noqa: BLE001
        log.exception("CRM: проход напоминаний не удался")
        return
    try:
        found = await report_integrity(bot, crm, cfg)
        if found:
            log.info("CRM: расхождений в данных найдено %s", found)
    except Exception:                                    # noqa: BLE001
        log.exception("CRM: проверка расхождений не удалась")
    try:
        if await post_free_bikes(bot, crm, cfg):
            log.info("CRM: пост о свободных велосипедах отправлен")
    except Exception:                                    # noqa: BLE001
        log.exception("CRM: пост о свободных велосипедах не собран")
    if digest:
        text = (texts.CAB_DIGEST_INTRO.format(today=today.strftime("%d.%m.%Y"))
                + "\n\n" + digest)
        for part in bot_logic.split_message(text):
            try:
                await bot.send_message(cfg.contract_chat_id, part)
            except TelegramAPIError:
                log.exception("сводка по оплатам не доставлена")
                return
