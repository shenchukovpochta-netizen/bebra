"""Опрос трекеров: из StarLine в базу и тревоги в служебный чат.

Панель в интернет не ходит: она читает то, что сюда положили. Опрос
живёт в процессе бота, рядом с дневным проходом, — там уже есть и
расписание, и бот для сообщений.

Тревога поднимается один раз и висит, пока оператор её не снимет или
пока причина не исчезнет сама: велосипед вернулся на связь, уехал в
аренду, питание восстановилось. Иначе каждые пять минут в чат падало бы
одно и то же, и через неделю чат перестают читать. В чат она уходит по
отметке в базе (`reported_at`), а не из памяти круга: сводка, которую
Telegram не принял, повторяется следующими кругами (REPORT_TRIES).

Команды устройству (блокировка мотора) идут тем же путём, только в
обратную сторону: панель кладёт команду в очередь, опрос относит её в
StarLine в начале круга и записывает ответ. Блокировка у StarLine
срабатывает после остановки велосипеда, поэтому минуты до ближайшего
круга ничего не меняют.
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
# Сколько кругов подряд пробовать донести до чата тревогу или ответ на
# команду, если сводка не ушла. Час при опросе раз в пять минут: Telegram
# за это время поднимается, а сводка, которую он отвергает, не должна
# вечно стоять впереди всех следующих тревог. Тревога при этом не теряется
# - она открыта в панели.
REPORT_TRIES = 12


async def send_commands(crm: Any, client: Any, *, now: datetime | None = None) -> list[dict]:
    """Отнести в StarLine команды из очереди и записать ответ на каждую.

    Отказ одной команды не останавливает остальные, а ответ StarLine
    ложится на команду словами: оператор увидит его на карточке трекера,
    а не в логе бота.
    """
    now = now or datetime.now(UTC)
    done: list[dict] = []
    for command in await crm.pending_tracker_commands():
        on = command["command"] == "block"
        try:
            await client.block_motor(command["device_id"], on)
            ok, result = True, "StarLine принял команду"
        except Exception as exc:                        # noqa: BLE001
            ok, result = False, str(exc)[:300] or type(exc).__name__
            log.warning("команда %s трекеру %s не прошла: %s",
                        command["command"], command["device_id"], result)
        await crm.finish_tracker_command(command["id"], ok=ok, result=result)
        if ok:
            await crm.update_tracker(command["tracker_id"], blocked=on,
                                     blocked_at=now, blocked_by=command.get("requested_by"))
        done.append({**command, "ok": ok, "result": result})
    return done


async def poll_once(crm: Any, client: Any, *, now: datetime | None = None) -> dict:
    """Один круг опроса: команды в StarLine, состояния в базу, тревоги - на выход.

    Возвращает счётчики и список новых тревог: рассылку делает вызывающий,
    чтобы этот шаг можно было прогнать без бота.
    """
    now = now or datetime.now(UTC)
    commands = await send_commands(crm, client, now=now)
    devices = await client.devices()
    # Настройки - до записи состояний: порог «едет» из панели решает,
    # ставить ли отметку «ехал», а по ней считается «стоит при аренде».
    settings = await crm.settings()
    moving = logic.tracker_settings(settings)["moving_speed"]
    seen = 0
    fresh: list[dict] = []
    for device in devices:
        saved = await crm.save_tracker_state(device, moving_speed=moving)
        seen += 1
        if saved.get("created"):
            log.info("трекер %s заведён сам: он есть в StarLine, "
                     "но не был привязан к велосипеду", device["device_id"])
    rows = logic.tracker_rows(await crm.trackers(active_only=True), now=now,
                              settings=settings)
    open_now: dict[int, set[str]] = {}
    for alert in await crm.tracker_alerts(open_only=True, limit=1000):
        open_now.setdefault(int(alert["tracker_id"]), set()).add(alert["kind"])
    # Снятый с наблюдения трекер в круг не входит - и его тревоги иначе
    # висели бы открытыми вечно: закрыть их некому, кроме человека.
    watched = {int(row["id"]) for row in rows}
    for tracker_id in sorted(set(open_now) - watched):
        await crm.close_alerts(tracker_id, sorted(open_now[tracker_id]), by="tracking")
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
    return {"devices": seen, "alerts": fresh, "commands": commands}


async def report_alerts(bot: Any, cfg: Any, alerts: list[dict], *,
                        crm: Any = None) -> int:
    """Новые тревоги - одной сводкой в служебный чат.

    Срочные и жёлтые идут одним сообщением: два сообщения подряд читают
    так же, как одно, а разделять их значит завести второй чат. С `crm`
    исход ложится на тревоги (reported_at или ещё одна попытка): тревога
    поднимается один раз, и недоставленная сводка без этой отметки
    пропадала из чата насовсем.
    """
    digest = logic.tracker_digest(alerts)
    if not digest:
        return 0
    ids = [int(a["id"]) for a in alerts if a.get("id") is not None]
    try:
        await bot.send_message(cfg.contract_chat_id, digest)
    except TelegramAPIError:
        log.exception("сводка по трекерам не доставлена")
        if crm is not None:
            await crm.mark_alerts_reported(ids, ok=False)
        return 0
    if crm is not None:
        await crm.mark_alerts_reported(ids, ok=True)
    return len(alerts)


def commands_digest(commands: list[dict]) -> str:
    """Ответ StarLine на команды - строкой на каждую: оператор нажал
    кнопку минуты назад и ждёт не сводку, а «прошло / не прошло»."""
    lines = []
    for c in commands:
        what = logic.TRACKER_COMMANDS.get(str(c.get("command")), str(c.get("command")))
        where = f"№ {c['bike_code']}" if c.get("bike_code") else f"трекер {c.get('device_id')}"
        who = f" · {c['requested_by']}" if c.get("requested_by") else ""
        if c.get("ok"):
            lines.append(f"🔒 {what}: {where} — StarLine принял{who}")
        else:
            lines.append(f"⚠️ {what}: {where} — не прошла: {c.get('result')}{who}")
    return "\n".join(lines)


async def report_commands(bot: Any, cfg: Any, commands: list[dict], *,
                          crm: Any = None) -> int:
    """Ответ StarLine на команды - в служебный чат; с `crm` исход ложится
    на команды, как у тревог (report_alerts)."""
    text = commands_digest(commands)
    if not text:
        return 0
    ids = [int(c["id"]) for c in commands if c.get("id") is not None]
    try:
        await bot.send_message(cfg.contract_chat_id, text)
    except TelegramAPIError:
        log.exception("ответ на команды трекерам не доставлен")
        if crm is not None:
            await crm.mark_commands_reported(ids, ok=False)
        return 0
    if crm is not None:
        await crm.mark_commands_reported(ids, ok=True)
    return len(commands)


async def report_pending(bot: Any, crm: Any, cfg: Any, *,
                         tries: int = REPORT_TRIES) -> tuple[int, int]:
    """В чат - всё, чего он ещё не видел: ответы на команды и открытые
    тревоги. Берётся из базы, а не из итогов круга: сводка, не ушедшая
    на прошлом круге, и круг, оборвавшийся после записи тревог или
    команд, догоняются следующим. Возвращает (команд, тревог)."""
    commands = await crm.unreported_tracker_commands(max_tries=tries)
    told_commands = await report_commands(bot, cfg, commands, crm=crm)
    alerts = await crm.unreported_tracker_alerts(max_tries=tries)
    told_alerts = await report_alerts(bot, cfg, alerts, crm=crm)
    return told_commands, told_alerts


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
            log.info("трекеры: устройств %s, новых тревог %s, команд %s",
                     result["devices"], len(result["alerts"]),
                     len(result.get("commands") or []))
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("опрос трекеров не удался, повтор через %s с", interval)
        # Свой try: StarLine лежит - а недоставленное с прошлых кругов
        # всё равно пробуем донести.
        try:
            await report_pending(bot, crm, cfg)
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("сводка по трекерам не собрана")
        await asyncio.sleep(interval)
