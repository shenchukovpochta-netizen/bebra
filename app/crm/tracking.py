"""Опрос трекеров: из StarLine в базу и тревоги в служебный чат.

Панель в интернет не ходит: она читает то, что сюда положили. Опрос
живёт в процессе бота, рядом с дневным проходом, — там уже есть и
расписание, и бот для сообщений.

Тревога поднимается один раз и висит, пока оператор её не снимет или
пока причина не исчезнет сама: велосипед вернулся на связь, уехал в
аренду, питание восстановилось. Иначе каждые пять минут в чат падало бы
одно и то же, и через неделю чат перестают читать.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from aiogram.exceptions import TelegramAPIError

from . import logic

log = logging.getLogger(__name__)

# Пять минут - компромисс: StarLine отдаёт позицию не чаще, а чаще
# спрашивать значит жечь лимиты их API ради той же точки.
POLL_SECONDS = 300


async def poll_once(crm: Any, client: Any, *, now: datetime | None = None) -> dict:
    """Один круг опроса: состояния в базу, тревоги - на выход.

    Возвращает счётчики и список новых тревог: рассылку делает вызывающий,
    чтобы этот шаг можно было прогнать без бота.
    """
    now = now or datetime.now(UTC)
    devices = await client.devices()
    seen = 0
    fresh: list[dict] = []
    for device in devices:
        saved = await crm.save_tracker_state(device)
        seen += 1
        if saved.get("created"):
            log.info("трекер %s заведён сам: он есть в StarLine, "
                     "но не был привязан к велосипеду", device["device_id"])
    settings = await crm.settings()
    rows = logic.tracker_rows(await crm.trackers(active_only=True), now=now,
                              settings=settings)
    open_now: dict[int, set[str]] = {}
    for alert in await crm.tracker_alerts(open_only=True, limit=1000):
        open_now.setdefault(int(alert["tracker_id"]), set()).add(alert["kind"])
    for row in rows:
        wanted = logic.detect_alerts(row, settings=settings)
        kinds = {alert["kind"] for alert in wanted}
        # Сначала снять то, чего больше нет: иначе «молчит» останется
        # висеть на велосипеде, который уже час как на связи. Закрываем
        # только реально открытые - лишний UPDATE на каждый трекер
        # каждые пять минут базе ни к чему.
        gone = sorted(open_now.get(int(row["id"]), set()) - kinds)
        if gone:
            await crm.close_alerts(row["id"], gone, by="tracking")
        for alert in wanted:
            alert_id = await crm.raise_alert(
                tracker_id=row["id"], kind=alert["kind"], note=alert.get("note"),
                bike_id=row.get("bike_id"), lat=row.get("lat"), lon=row.get("lon"),
                level=alert.get("level") or logic.alert_level(alert["kind"]))
            if alert_id is not None:
                fresh.append({**alert, "id": alert_id,
                              "bike_code": row.get("bike_code"),
                              "alias": row.get("alias"),
                              "device_id": row.get("device_id")})
    return {"devices": seen, "alerts": fresh}


async def report_alerts(bot: Any, cfg: Any, alerts: list[dict]) -> int:
    """Новые тревоги - одной сводкой в служебный чат.

    Срочные и жёлтые идут одним сообщением: два сообщения подряд читают
    так же, как одно, а разделять их значит завести второй чат.
    """
    digest = logic.tracker_digest(alerts)
    if not digest:
        return 0
    try:
        await bot.send_message(cfg.contract_chat_id, digest)
    except TelegramAPIError:
        log.exception("сводка по трекерам не доставлена")
        return 0
    return len(alerts)


async def tracking_loop(bot: Any, crm: Any, cfg: Any, client: Any, *,
                        interval: int = POLL_SECONDS) -> None:
    """Фоновый опрос. Сбой одного круга не останавливает следующие:
    StarLine отвечает не всегда, а терять из-за этого слежку нельзя."""
    if client is None or not getattr(client, "ready", False):
        log.info("трекеры StarLine не настроены, опрос не запускается")
        return
    while True:
        try:
            result = await poll_once(crm, client)
            if result["alerts"]:
                await report_alerts(bot, cfg, result["alerts"])
            log.info("трекеры: устройств %s, новых тревог %s",
                     result["devices"], len(result["alerts"]))
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("опрос трекеров не удался, повтор через %s с", interval)
        await asyncio.sleep(interval)
