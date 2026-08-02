"""Фильтры, общие для нескольких роутеров."""

from __future__ import annotations

from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import Message


class ServiceChatReply(BaseFilter):
    """Ответ на сообщение в служебном чате - и только там.

    Роутер модерации подключается первым, поэтому голый фильтр «любой реплай»
    ловил бы и обычного пользователя, ответившего на сообщение бота в личке:
    aiogram останавливает разбор на первом совпавшем обработчике, и человек
    посреди анкеты не получил бы ничего в ответ.
    """

    async def __call__(self, message: Message, cfg: Any = None) -> bool:
        if cfg is None or message.reply_to_message is None:
            return False
        return message.chat.id in {cfg.admin_chat_id, cfg.contract_chat_id}


class StateIs(BaseFilter):
    """Шаг пользователя из анкеты, загруженной middleware."""

    def __init__(self, *states: str) -> None:
        self.states = set(states)

    # user приходит из middleware, но для апдейтов модерации его нет:
    # у админа анкеты не заводится. Значение по умолчанию обязательно,
    # иначе фильтр упадёт с TypeError на чужом апдейте.
    async def __call__(self, event: Any, user: dict | None = None) -> bool:
        return bool(user) and user.get("state") in self.states
