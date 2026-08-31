"""Клавиатуры бота."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from .model import DAY_NAMES

NOW = "🕒 Сейчас"
TODAY = "📅 Сегодня"
TOMORROW = "📆 Завтра"
WEEK = "🗓 Неделя"
DAYS = "🔎 Дни"

# resize_keyboard обязателен: без него Telegram растягивает клавиатуру на
# пол-экрана и расписание уезжает под неё.
MAIN = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=NOW)],
        [KeyboardButton(text=TODAY), KeyboardButton(text=TOMORROW)],
        [KeyboardButton(text=WEEK), KeyboardButton(text=DAYS)],
    ],
    resize_keyboard=True,
)

# Учебных дней шесть: воскресенье в расписании не расчерчено вовсе.
DAY_PICKER = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text=DAY_NAMES[i], callback_data=f"day:{i}") for i in row]
        for row in ((0, 1), (2, 3), (4, 5))
    ]
)
