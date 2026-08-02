from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)


def subscribe(channel_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подписаться на канал", url=channel_url)],
        [InlineKeyboardButton(text="Проверить подписку", callback_data="check_sub")],
    ])


def oferta(url: str, pdn_url: str = "") -> InlineKeyboardMarkup:
    """Оферта и согласие на обработку данных одним экраном.

    pdn_url необязателен: если политику обработки когда-нибудь опубликуют
    отдельным документом, достаточно заполнить переменную - появится вторая
    кнопка, трогать код не придётся.
    """
    rows = [[InlineKeyboardButton(text="Читать оферту и правила проката", url=url)]]
    if pdn_url:
        rows.append([InlineKeyboardButton(text="Политика обработки ПДн", url=pdn_url)])
    rows.append([InlineKeyboardButton(text="Принимаю", callback_data="oferta_ok")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def share_contact() -> ReplyKeyboardMarkup:
    # request_contact работает только в reply-клавиатуре и только в личном чате
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться контактом", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтверждаю", callback_data="confirm")],
        [InlineKeyboardButton(text="Заполнить повторно", callback_data="restart")],
    ])


def moderation(tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{tg_id}"),
        InlineKeyboardButton(text="⛔ Отклонить", callback_data=f"reject:{tg_id}"),
    ]])


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚲 Арендовать"), KeyboardButton(text="📋 Мои поездки")],
            [KeyboardButton(text="💰 Тарифы"), KeyboardButton(text="🆘 Поддержка")],
        ],
        resize_keyboard=True,
    )


remove = ReplyKeyboardRemove
