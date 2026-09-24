"""Отправка рассылок: один и тот же текст уходит в Telegram и в MAX.

Отправитель живёт в процессе бота: у него есть и Bot, и расписание.
MAX-клиент подключается, если задан его токен, - без него сообщения
MAX-клиентам помечаются пропущенными, а не теряются молча.

Кампания не стартует сама. Оператор собирает черновик, смотрит на список
получателей и нажимает «Отправить»: двести человек, получивших не тот
текст, - это не опечатка, а испорченный день.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from . import logic

log = logging.getLogger(__name__)

# Как часто смотреть, не появилась ли кампания к отправке.
POLL_SECONDS = 20
# Сколько сообщений берём за один круг: рассылка не должна занимать
# процесс бота целиком - у него есть и живые клиенты.
BATCH = 50


async def send_one(bot: Any, max_client: Any, send: dict, text: str, *,
                   reply_markup: Any = None) -> tuple[str, str]:
    """Одно сообщение. Возвращает (статус, ошибка). reply_markup - только
    для Telegram: у MAX своя разметка кнопок."""
    if send["channel"] == "max":
        if max_client is None:
            return "skipped", "MAX-бот не подключён"
        try:
            await max_client.send(user_id=int(send["max_id"]), text=text)
        except Exception as exc:                        # noqa: BLE001
            return "failed", str(exc)[:200]
        return "sent", ""
    extra = {"reply_markup": reply_markup} if reply_markup is not None else {}
    try:
        await bot.send_message(int(send["tg_id"]), text, **extra)
    except TelegramRetryAfter as exc:
        # Телеграм сам говорит, сколько ждать: это не ошибка доставки,
        # а просьба притормозить.
        await asyncio.sleep(float(getattr(exc, "retry_after", 1)) + 1)
        try:
            await bot.send_message(int(send["tg_id"]), text, **extra)
        except TelegramAPIError as retry_exc:
            return "failed", str(retry_exc)[:200]
        return "sent", ""
    except TelegramAPIError as exc:
        # Самая частая причина - клиент заблокировал бота. Это факт,
        # который стоит видеть в списке, а не повод падать.
        return "failed", str(exc)[:200]
    except Exception as exc:                            # noqa: BLE001
        return "failed", str(exc)[:200]
    return "sent", ""


async def run_campaign(bot: Any, crm: Any, campaign: dict, *,
                       max_client: Any = None, pay_url: str = "",
                       limit: int = BATCH, pause: float = logic.SEND_PAUSE) -> dict:
    """Отправить очередную порцию кампании. Возвращает счётчики."""
    queued = await crm.campaign_sends(campaign["id"], status="queued", limit=limit)
    if not queued:
        await crm.set_campaign_status(campaign["id"], "done")
        return {"sent": 0, "failed": 0, "skipped": 0, "done": True}
    counts = {"sent": 0, "failed": 0, "skipped": 0, "done": False}
    for send in queued:
        # Кампанию могли отменить посреди порции: без этой проверки после
        # «Отменить» уходили бы ещё до полусотни сообщений из снимка очереди.
        fresh = await crm.campaign(campaign["id"])
        if fresh is None or fresh.get("status") != "sending":
            break
        client = await crm.client(send["client_id"])
        if client is None or client.get("status") != "active":
            # Пока кампания шла, клиента заблокировали: это ровно тот
            # случай, ради которого статус проверяется ещё раз.
            await crm.mark_send(send["id"], status="skipped",
                                error="клиент заблокирован")
            counts["skipped"] += 1
            continue
        rental = await crm.active_rental_of(client["id"])
        values = logic.template_context(
            client, rental, await crm.client_balance(client["id"]),
            pay_url=pay_url)
        body = campaign.get("body") or ""
        if send["channel"] == "max":
            body = campaign.get("body_max") or logic.plain_text(body)
        status, error = await send_one(bot, max_client, send,
                                       logic.render_template(body, values))
        await crm.mark_send(send["id"], status=status, error=error or None)
        counts[status] = counts.get(status, 0) + 1
        await asyncio.sleep(pause)
    return counts


async def mailing_loop(bot: Any, crm: Any, cfg: Any, *, max_client: Any = None,
                       interval: int = POLL_SECONDS) -> None:
    """Фоновая отправка: кампании, которые оператор пустил в дело."""
    while True:
        try:
            for row in await crm.sending_campaigns():
                campaign = await crm.campaign(row["id"])
                if campaign is None:
                    continue
                counts = await run_campaign(
                    bot, crm, campaign, max_client=max_client,
                    pay_url=getattr(cfg, "pay_url", ""))
                if counts["sent"] or counts["failed"]:
                    log.info("рассылка %s: доставлено %s, не доставлено %s",
                             campaign["no"], counts["sent"], counts["failed"])
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("отправка рассылки не удалась, повтор через %s с",
                          interval)
        await asyncio.sleep(interval)
