"""Рабочая группа точек: темы фиксации, сдачи, долга, GPS и итогов дня.

Раньше группу читал сценарий n8n. Он же держал токен бота в адресах
десяти запросов и писал в Google-таблицу, которая расходилась с CRM.
Теперь сообщения читает сам бот, сверяет с базой (`crm.opsgroup`) и
ставит реакцию: 👍 - совпало, 👎 - нет, и ответом сказано, что именно.

Роутер подключается ПЕРВЫМ и забирает любое сообщение из тем группы:
иначе реплику «ок» в теме подобрал бы общий обработчик меню и ответил
бы в рабочий чат клиентским текстом.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter
from aiogram.types import Message, ReactionTypeEmoji

from ..config import Config
from ..crm import logic as crm_logic
from ..crm import opsgroup
from ..filters import is_operator, ops_topic

log = logging.getLogger(__name__)
router = Router(name="ops")


class OpsTopic(BaseFilter):
    """Сообщение из темы рабочей группы. Тема уходит в обработчик."""

    async def __call__(self, message: Message, cfg: Any = None) -> bool | dict:
        topic = ops_topic(cfg, message) if cfg is not None else None
        return {"ops_kind": topic} if topic else False


def _who(user: Any) -> str:
    return f"@{user.username}" if user.username else (user.first_name or str(user.id))


async def _may_swap(crm: Any, cfg: Config, user_id: int) -> bool:
    """Замена меняет аренду - её проводит оператор из ADMINS или
    сотрудник панели с правом менять аренды, привязавший Telegram."""
    if is_operator(cfg, user_id):
        return True
    staff = await crm.staff_by_tg(user_id)
    if not staff or not staff.get("active", True):
        return False
    return staff.get("role") == "admin" or crm_logic.can_edit(staff, "rentals")


async def _react(bot: Bot, message: Message, emoji: str) -> None:
    try:
        await bot.set_message_reaction(chat_id=message.chat.id,
                                       message_id=message.message_id,
                                       reaction=[ReactionTypeEmoji(emoji=emoji)])
    except TelegramAPIError as exc:
        # Реакции в группе могут быть выключены - ответ текстом всё равно уйдёт.
        log.warning("реакция в рабочей группе не поставлена: %s", exc)


@router.message(OpsTopic())
async def ops_message(message: Message, bot: Bot, cfg: Config, ops_kind: str,
                      crm: Any = None) -> None:
    user = message.from_user
    if user is None or user.is_bot or crm is None:
        return
    text = message.text or message.caption or ""
    if not text.strip():
        return
    meta = {"chat_id": message.chat.id, "message_id": message.message_id,
            "thread_id": message.message_thread_id, "author_tg": user.id,
            "author": _who(user)}
    swap_allowed = (ops_kind == "fix" and crm_logic.is_ops_swap(text)
                    and await _may_swap(crm, cfg, user.id))
    try:
        outcome = await opsgroup.handle(
            crm, ops_kind, text, meta, today=date.today(), now=datetime.now(UTC),
            swap_allowed=swap_allowed, by=f"tg:{user.id}")
    except Exception:                                    # noqa: BLE001
        log.exception("рабочая группа: сообщение %s не разобрано", message.message_id)
        return
    if outcome.reaction:
        await _react(bot, message, outcome.reaction)
    if outcome.reply:
        try:
            await message.reply(outcome.reply, disable_web_page_preview=True)
        except TelegramAPIError as exc:
            log.warning("ответ в рабочую группу не ушёл: %s", exc)
