from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .faq import MENU_BUTTON as BTN_FAQ
from .texts import BTN_CLOSE_RENT


def subscribe(channel_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подписаться на канал", url=channel_url)],
        [InlineKeyboardButton(text="Проверить подписку", callback_data="check_sub")],
    ])


def policy_ack(pdn_url: str = "") -> InlineKeyboardMarkup:
    """Экран ознакомления с Политикой обработки ПДн.

    Отдельная «галочка» ПЕРЕД согласием: ознакомление с политикой и согласие
    на обработку - два разных юридических факта, и каждый фиксируется своей
    кнопкой со своим моментом в базе.
    """
    rows = []
    if pdn_url:
        rows.append([InlineKeyboardButton(text="Политика (веб-версия)", url=pdn_url)])
    rows.append([InlineKeyboardButton(
        text="✔️ Ознакомлен(а) с Политикой", callback_data="pdn_ok")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def consent(rules_url: str = "", pdn_url: str = "") -> InlineKeyboardMarkup:
    """Экран согласия на обработку персональных данных.

    Обе ссылки необязательны: правила проката и отдельная политика ПДн
    появляются кнопками, как только заполнены переменные, - трогать код
    не придётся. callback остался «oferta_ok» намеренно: старые кнопки
    в открытых чатах продолжают работать.
    """
    rows = []
    if rules_url:
        rows.append([InlineKeyboardButton(text="Правила проката", url=rules_url)])
    if pdn_url:
        rows.append([InlineKeyboardButton(text="Политика обработки ПДн", url=pdn_url)])
    rows.append([InlineKeyboardButton(text="✅ Даю согласие", callback_data="oferta_ok")])
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


def reject_reasons(tg_id: int, reasons: dict[str, tuple[str, str]]) -> InlineKeyboardMarkup:
    """Второй экран кнопки «Отклонить»: за что именно.

    Готовые причины, а не только свободный текст: у каждой из них есть шаг,
    на который человека вернут. Переигрывать всю анкету из-за нечитаемого
    селфи - верный способ получить брошенную заявку вместо исправленной.
    """
    rows = [[InlineKeyboardButton(text=title, callback_data=f"rj:{tg_id}:{code}")]
            for code, (title, _state) in reasons.items()]
    rows.append([InlineKeyboardButton(text="✍️ Свой текст", callback_data=f"rjc:{tg_id}")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"rjx:{tg_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def same_address() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Совпадает с регистрацией")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def sign_contract() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Подписываю", callback_data="sign")],
        [InlineKeyboardButton(text="Есть ошибка", callback_data="contract_mistake")],
    ])


def paid(pay_url: str = "") -> InlineKeyboardMarkup:
    """Этап оплаты: ссылка на расчётный счёт и «я оплатил».

    Ссылка кнопкой, а не только текстом: с телефона по ней открывается
    приложение банка, и человеку не нужно копировать длинный адрес.
    Ссылка тем не менее дублируется в тексте - на десктопе кнопку СБП
    открыть нечем. «Я оплатил(а)» сама по себе состояние не меняет:
    поступление подтверждает оператор кнопкой на своей карточке.
    """
    rows = []
    # Кнопка добавляется, только если это похоже на ссылку: Telegram
    # отвергает СООБЩЕНИЕ ЦЕЛИКОМ из-за кнопки с битым url, и опечатка
    # в PAY_URL оставила бы клиента вообще без реквизитов. Сама ссылка
    # при этом остаётся в тексте - там она безобидна.
    if pay_url.startswith(("http://", "https://")):
        rows.append([InlineKeyboardButton(text="💳 Оплатить", url=pay_url)])
    rows.append([InlineKeyboardButton(text="✅ Я оплатил(а)", callback_data="paid")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pay_confirm(tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Оплата получена", callback_data=f"pay:{tg_id}")],
    ])


def sign_act() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Подписываю", callback_data="act_sign")],
        [InlineKeyboardButton(text="Есть ошибка", callback_data="act_mistake")],
    ])


def sign_return() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Подтверждаю", callback_data="return_sign")],
        [InlineKeyboardButton(text="Есть ошибка", callback_data="return_mistake")],
    ])


def support_cancel() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отмена")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def faq_topics(topics) -> InlineKeyboardMarkup:
    """Темы частых вопросов - по кнопке на строку.

    Заголовки длинные, по две в ряд Telegram обрезает их до многоточия,
    и человек не понимает, куда жмёт.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=topic.title, callback_data=f"faq:{topic.code}")]
        for topic in topics
    ])


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚲 Арендовать"), KeyboardButton(text="📋 Мои аренды")],
            [KeyboardButton(text="💰 Тарифы"), KeyboardButton(text="🆘 Поддержка")],
            [KeyboardButton(text=BTN_FAQ), KeyboardButton(text=BTN_CLOSE_RENT)],
        ],
        resize_keyboard=True,
    )


remove = ReplyKeyboardRemove
