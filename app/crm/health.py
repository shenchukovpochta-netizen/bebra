"""Здоровье сервера: раз в час - диск, бэкап, панель, сертификаты.

Живёт в процессе бота рядом с остальными фоновыми кругами: бот уже
умеет писать в Telegram, а панель в интернет сама не ходит. Замеры -
`app/services/probes.py`, суждение «беда или нет» и память между
проверками - `crm.logic` (`health_problems`, `health_step`), отправка -
через ворота уведомлений (`server_health`), чтобы владелец мог выключить
её или отдать другому человеку.

Чего этот круг не умеет: сказать, что упал сам бот или весь сервер.
Для этого панель отдаёт /healthz/bot (пульс этого круга), а снаружи
смотрит бесплатный монитор - см. INSTALL.md, «Здоровье сервера».
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from ..services import probes as default_probes
from . import logic, notices

log = logging.getLogger(__name__)

CHECK_SECONDS = 3600
# Первая проверка - не сразу: после выкладки панель и backup поднимаются
# вместе с ботом, и без паузы каждый деплой приносил бы ложную тревогу.
FIRST_CHECK_SECONDS = 900
# Панель перезапускается секунд десять: вторая попытка через 20 секунд
# отличает перезапуск от падения.
PANEL_RETRY_SECONDS = 20


def _reason(exc: BaseException) -> str:
    return (str(exc).strip() or type(exc).__name__)[:200]


def domains(cfg: Any) -> list[str]:
    """Домены, чьи сертификаты смотрим: панель и демо-стенд, если заданы."""
    out: list[str] = []
    for raw in (getattr(cfg, "crm_domain", ""), getattr(cfg, "demo_domain", "")):
        host = str(raw or "").strip().lower()
        if host and host not in out:
            out.append(host)
    return out


async def _panel_error(url: str, probes: Any) -> str | None:
    try:
        status = await probes.http_status(url)
    except Exception as exc:                            # noqa: BLE001
        return _reason(exc)
    return None if status == 200 else f"ответ {status}"


async def measure(cfg: Any, *, probes: Any = default_probes,
                  retry: float | None = PANEL_RETRY_SECONDS) -> dict[str, Any]:
    """Замеры одной проверки. Сбой замера - не сбой проверки: диск не
    измерился - про диск молчим, остальное всё равно проверяется.
    `retry` - пауза перед второй попыткой до панели, None - без неё."""
    disk = None
    try:
        # Том kycfiles лежит на диске docker - там же база и образы.
        disk = probes.disk_usage(cfg.storage_dir)
    except OSError as exc:
        log.warning("место на диске не измерено: %s", exc)
    panel = None
    url = str(getattr(cfg, "health_panel_url", "") or "")
    if url:
        panel = await _panel_error(url, probes)
        if panel and retry is not None:
            await asyncio.sleep(retry)
            panel = await _panel_error(url, probes)
    certs: list[tuple[str, datetime | None, str | None]] = []
    for host in domains(cfg):
        try:
            certs.append((host, await probes.cert_not_after(host), None))
        except Exception as exc:                        # noqa: BLE001
            certs.append((host, None, _reason(exc)))
    return {"disk": disk, "panel_error": panel, "certs": certs}


async def pulse(crm: Any, *, now: datetime | None = None) -> None:
    """Отметка «круг жив» без проверки - на старте, чтобы /healthz/bot не
    отвечал 503 первые четверть часа после выкладки."""
    now = now or datetime.now(UTC)
    state = logic.parse_health_state((await crm.settings()).get(logic.HEALTH_KEY))
    await crm.set_setting(logic.HEALTH_KEY,
                          json.dumps(logic.health_keep(state, now), ensure_ascii=False),
                          by="bot")


async def check_once(bot: Any, crm: Any, cfg: Any, *, now: datetime | None = None,
                     probes: Any = default_probes,
                     retry: float | None = PANEL_RETRY_SECONDS) -> dict[str, Any]:
    """Одна проверка: замерить, сравнить с прошлой, написать, запомнить."""
    got = await measure(cfg, probes=probes, retry=retry)
    now = now or datetime.now(UTC)
    settings = await crm.settings()
    state = logic.parse_health_state(settings.get(logic.HEALTH_KEY))
    notice = (await notices.settings(crm)).get("server_health") or {}
    problems = logic.health_problems(
        now=now, backup=logic.parse_backup_status(settings.get(logic.BACKUP_STATUS_KEY)),
        disk=got["disk"], panel_error=got["panel_error"], certs=got["certs"],
        disk_pct=logic.notice_param(notice, "disk_pct", 10),
        cert_days=logic.notice_param(notice, "cert_days", 14))
    lines, new = logic.health_step(state, problems, now)
    if lines:
        sent = await notices.send_team(crm, bot, "server_health",
                                       logic.health_message(lines), cfg.contract_chat_id)
        # Выключенное уведомление - не повод копить: состояние обновляется
        # и молча. Недоставленное - повод: следующий круг скажет то же.
        if not sent and notice.get("enabled", True):
            new = logic.health_keep(state, now)
    await crm.set_setting(logic.HEALTH_KEY, json.dumps(new, ensure_ascii=False), by="bot")
    return {"problems": problems, "lines": lines}


async def health_loop(bot: Any, crm: Any, cfg: Any, *, interval: int = CHECK_SECONDS,
                      first: int = FIRST_CHECK_SECONDS) -> None:
    """Фоновый круг. Сбой одной проверки не останавливает следующие."""
    try:
        await pulse(crm)
    except asyncio.CancelledError:
        raise
    except Exception:                                   # noqa: BLE001
        log.exception("отметка проверки сервера не записана")
    await asyncio.sleep(first)
    while True:
        try:
            result = await check_once(bot, crm, cfg)
            if result["problems"]:
                log.warning("сервер: %s", "; ".join(sorted(result["problems"])))
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("проверка сервера не удалась, повтор через %s с", interval)
        await asyncio.sleep(interval)
