"""Оценка аренды после сдачи: вопрос клиенту и сигнал о низкой оценке.

Живёт в процессе бота: у него есть Telegram, MAX-клиент и расписание, а
панель в интернет в фоне не ходит. Очередь кладёт закрытие аренды
(service.close_rental - одно на панель и бота), круг раз в минуту её
разбирает. Нажатие кнопки принимают обработчики ботов
(app/handlers/feedback.py, app/max/handlers.py) через service.rate_rental.

Отметка «спросили» ставится до отправки, как у напоминаний: клиент,
заблокировавший бота, не должен получать попытку каждую минуту. Сигнал
о низкой оценке тоже отмечается до отправки - два одинаковых сигнала
хуже, чем один недоставленный, который виден в истории уведомлений.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from .. import texts
from ..max import keyboards as max_kb
from . import logic, notices, notify

log = logging.getLogger(__name__)

POLL_SECONDS = 60
BATCH = 50


async def _ask_max(max_client: Any, row: dict) -> bool:
    """MAX разметку понимает свою, а кнопки - тем же payload, что в Telegram."""
    try:
        await max_client.send(user_id=int(row["max_id"]),
                              text=texts.FEEDBACK_ASK.format(bike=notify.feedback_bike(row)),
                              keyboard=max_kb.feedback_scores(int(row["rental_id"])))
    except Exception as exc:                            # noqa: BLE001
        log.warning("вопрос об оценке в MAX клиенту %s не ушёл: %s",
                    row.get("client_id"), exc)
        return False
    return True


async def ask_once(bot: Any, crm: Any, *, max_client: Any = None,
                   now: datetime | None = None) -> int:
    """Разобрать очередь вопросов. Возвращает, сколько ушло."""
    now = now or datetime.now(UTC)
    state = await notices.settings(crm)
    enabled = bool(state.get("feedback_ask", {}).get("enabled", True))
    sent = 0
    for row in await crm.feedback_queue(BATCH):
        # Выключено - всё равно помечаем: включение обратно не должно
        # обрушить вопрос на всех, кто сдал велосипед за это время.
        why = logic.feedback_skip_reason(row, enabled=enabled, now=now,
                                         max_ready=max_client is not None)
        if why is not None:
            if await crm.mark_feedback_asked(row["id"], channel=None, skipped=why):
                await notices.record(crm, "feedback_ask", status="skipped",
                                     client_id=row.get("client_id"), detail=why)
            continue
        channel = logic.feedback_channel(row, max_ready=max_client is not None)
        if not await crm.mark_feedback_asked(row["id"], channel=channel):
            continue
        ok = (await notify.feedback_ask(bot, row) if channel == "tg"
              else await _ask_max(max_client, row))
        await notices.record(crm, "feedback_ask", status="sent" if ok else "failed",
                             client_id=row.get("client_id"),
                             detail=None if channel == "tg" else "MAX")
        sent += 1 if ok else 0
    return sent


async def alert_once(bot: Any, crm: Any, cfg: Any) -> int:
    """Низкие оценки - сигналом команде, один раз на оценку."""
    told = 0
    for row in await crm.feedback_to_alert(low=logic.FEEDBACK_LOW,
                                           wait_minutes=logic.FEEDBACK_ALERT_WAIT_MINUTES):
        if not await crm.mark_feedback_alerted(row["id"]):
            continue
        if await notices.send_team(crm, bot, "feedback_low", logic.feedback_alert_text(row),
                                   getattr(cfg, "contract_chat_id", None),
                                   client_id=row.get("client_id")):
            told += 1
    return told


async def feedback_loop(bot: Any, crm: Any, cfg: Any, *, max_client: Any = None,
                        interval: int = POLL_SECONDS) -> None:
    while True:
        try:
            asked = await ask_once(bot, crm, max_client=max_client)
            if asked:
                log.info("CRM: вопросов об оценке аренды отправлено %s", asked)
            told = await alert_once(bot, crm, cfg)
            if told:
                log.info("CRM: сигналов о низкой оценке %s", told)
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("круг оценок аренды не удался, повтор через %s с", interval)
        await asyncio.sleep(interval)
