from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .. import logic

log = logging.getLogger(__name__)


async def check_subscription(bot: Bot, channel_id: int, user_id: int) -> bool:
    """Проверка подписки на канал.

    Бот обязан быть администратором канала, иначе Telegram не отдаст статус.
    При любой ошибке API гейт закрывается, а не открывается: недоступность
    проверки не должна становиться способом её обойти.
    """
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
    except TelegramAPIError as exc:
        log.warning("getChatMember для %s не удался: %s", user_id, exc)
        return False
    return logic.is_subscribed(
        member.status, getattr(member, "is_member", None)
    )
