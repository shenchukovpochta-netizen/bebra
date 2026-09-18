"""Ворота уведомлений: включено ли, пора ли, и что из этого вышло.

Через этот модуль проходит каждое сообщение, которое система шлёт сама.
Раньше их было три и они были зашиты в код - владелец не мог ни
выключить, ни перенести на другой час, ни проверить, ушло ли вообще.

Устройство простое: каталог уведомлений в `logic.NOTICES`, правки
владельца в `crm.notices`, а здесь - `allowed()` перед отправкой и
`record()` после неё. Отправляют по-прежнему те, кто отправлял:
`billing`, `paying`, `notify`. Разносить доставку по этому модулю было
бы хуже - у каждого своя клавиатура, свой язык и свой получатель.

Сбой записи в историю не отменяет отправку: клиенту сообщение уже ушло,
и падать из-за журнала нельзя.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from . import logic

log = logging.getLogger(__name__)


async def settings(crm: Any) -> dict[str, dict[str, Any]]:
    """Каталог, поверх которого легли правки владельца."""
    try:
        rows = await crm.notices()
    except Exception:                                    # noqa: BLE001
        log.exception("настройки уведомлений не прочитаны, берём умолчания")
        rows = []
    return logic.notice_settings(rows)


async def allowed(crm: Any, code: str) -> bool:
    """Включено ли уведомление. Неизвестный код - да: уведомление, которого
    нет в каталоге, выключить владелец всё равно не мог."""
    if code not in logic.NOTICES:
        return True
    return bool((await settings(crm)).get(code, {}).get("enabled", True))


async def record(crm: Any, code: str, *, status: str,
                 client_id: int | None = None, detail: str | None = None) -> None:
    """Отметка в истории отправок. Ошибка записи отправку не отменяет."""
    target = (logic.NOTICES.get(code) or {}).get("target", "chat")
    try:
        await crm.log_notice(code, target=target, status=status,
                             client_id=client_id, detail=detail)
    except Exception:                                    # noqa: BLE001
        log.warning("уведомление %s не записано в историю", code, exc_info=True)


async def send_client(crm: Any, code: str, client_id: int | None,
                      sender: Any) -> bool:
    """Отправить клиенту одно уведомление с проверкой и отметкой.

    `sender` - корутина без аргументов, которая и шлёт: у каждого
    уведомления свой текст, язык и клавиатура, и собирать их здесь
    значило бы переписать сюда половину notify.py.
    """
    if not await allowed(crm, code):
        await record(crm, code, status="skipped", client_id=client_id,
                     detail="выключено в настройках")
        return False
    try:
        sent = bool(await sender())
    except Exception as err:                             # noqa: BLE001
        log.warning("уведомление %s клиенту %s не ушло", code, client_id,
                    exc_info=True)
        await record(crm, code, status="failed", client_id=client_id,
                     detail=str(err))
        return False
    await record(crm, code, status="sent" if sent else "skipped",
                 client_id=client_id,
                 detail=None if sent else "клиента нет в боте")
    return sent


async def send_team(crm: Any, bot: Any, code: str, text: str, default_chat: Any,
                    *, reply_markup: Any = None) -> bool:
    """Командное уведомление: адресат - переопределение владельца
    (конкретный сотрудник) или служебный чат. True - доставлено."""
    state = await settings(crm)
    if not state.get(code, {}).get("enabled", True):
        await record(crm, code, status="skipped", detail="выключено в настройках")
        return False
    chat = chat_for(state, code, default_chat)
    if bot is None or not chat:
        await record(crm, code, status="skipped", detail="служебный чат не задан")
        return False
    try:
        await bot.send_message(chat, text, reply_markup=reply_markup)
    except Exception as err:                             # noqa: BLE001
        log.warning("уведомление %s команде не ушло", code, exc_info=True)
        await record(crm, code, status="failed", detail=str(err))
        return False
    await record(crm, code, status="sent")
    return True


def due(state: dict[str, dict[str, Any]], code: str, now: datetime,
        done: dict[str, date] | None = None) -> bool:
    """Пора ли по расписанию. `done` - память прохода: код → дата."""
    return logic.notice_due(state.get(code), now, (done or {}).get(code))


def mark(done: dict[str, date] | None, code: str, today: date) -> None:
    if done is not None:
        done[code] = today


def chat_for(state: dict[str, dict[str, Any]], code: str, default: Any) -> Any:
    """Куда слать командное уведомление: переопределение или служебный чат."""
    chat = (state.get(code) or {}).get("chat_id")
    return chat or default
