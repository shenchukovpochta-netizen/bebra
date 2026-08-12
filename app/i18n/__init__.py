"""Переводы клиентского диалога на языки из ветки частых вопросов.

Русский - источник: ключи совпадают с именами констант в texts.py, и при
отсутствии перевода бот молча отвечает по-русски (та же дисциплина, что
в faq_i18n). Переводятся ТОЛЬКО сообщения клиенту и подписи его кнопок:

  - служебные карточки и алерты операторам всегда русские - их читает
    команда проката;
  - юридические документы (договор, согласие, акты, политика) всегда
    русские - подписывается русский текст, и перевод создал бы второй
    «экземпляр» с расхождениями;
  - свободный ввод клиента (ФИО, адреса, причина сдачи) не переводится -
    в договор и отчёты попадает то, что он написал.

Часть кнопок - reply-клавиатура: Telegram возвращает их нажатие обычным
текстом сообщения, поэтому обработчики сверяются со ВСЕМИ языковыми
вариантами подписи (variants/button_key), а не с одной русской строкой.

Переводы машинные и ждут вычитки носителями (особенно чувашский
и туркменский) - как и в faq_i18n.
"""

from __future__ import annotations

from .. import texts
from ..faq_i18n import LANG_TITLES, LANGS, pick_prompt  # noqa: F401 - реэкспорт
from . import ar, cv, en, fa, hi, tk, uz

# Пакеты переводов: язык -> {ключ -> текст}. Русского здесь нет намеренно -
# его источник texts.py и BUTTONS_RU, и вторая копия разъехалась бы.
PACKS: dict[str, dict[str, str]] = {
    "en": en.T, "uz": uz.T, "tk": tk.T, "ar": ar.T,
    "fa": fa.T, "hi": hi.T, "cv": cv.T,
}
# Переводы ошибок валидации: язык -> {русский текст ошибки -> перевод}.
# Ключ - сама русская строка из logic.py: валидаторы не пришлось трогать,
# а непереведённая ошибка уходит по-русски (честный fallback).
ERRORS: dict[str, dict[str, str]] = {
    "en": en.ERRORS, "uz": uz.ERRORS, "tk": tk.ERRORS, "ar": ar.ERRORS,
    "fa": fa.ERRORS, "hi": hi.ERRORS, "cv": cv.ERRORS,
}

# Русские подписи кнопок - канонические. Кнопок нет в texts.py (они жили
# строками в keyboards.py), поэтому источник - этот словарь; тест сверяет
# его с фактическими подписями там, где они обязаны совпадать.
BUTTONS_RU: dict[str, str] = {
    "BTN_RENT": "🚲 Арендовать",
    "BTN_TRIPS": "📋 Мои аренды",
    "BTN_TARIFFS": "💰 Тарифы",
    "BTN_SUPPORT": "🆘 Поддержка",
    "BTN_FAQ": "❓ Ответы на частые вопросы",
    "BTN_CLOSE_RENT": "🔚 Закрыть аренду",
    "BTN_CANCEL": "Отмена",
    "BTN_SAME_ADDRESS": "Совпадает с регистрацией",
    "BTN_SHARE_CONTACT": "📱 Поделиться контактом",
    "BTN_SUBSCRIBE": "Подписаться на канал",
    "BTN_CHECK_SUB": "Проверить подписку",
    "BTN_POLICY_WEB": "Политика (веб-версия)",
    "BTN_POLICY_ACK": "✔️ Ознакомлен(а) с Политикой",
    "BTN_RULES": "Правила проката",
    "BTN_PDN": "Политика обработки ПДн",
    "BTN_CONSENT": "✅ Даю согласие",
    "BTN_CONFIRM": "Подтверждаю",
    "BTN_RESTART": "Заполнить повторно",
    "BTN_SIGN": "✍️ Подписываю",
    "BTN_MISTAKE": "Есть ошибка",
    "BTN_PAY": "💳 Оплатить",
    "BTN_PAID": "✅ Я оплатил(а)",
    "BTN_RETURN_SIGN": "✍️ Подтверждаю",
}

# Кнопки reply-клавиатур, чьё нажатие приходит текстом сообщения: только
# их варианты попадают в обратный индекс button_key. Инлайн-кнопки ходят
# callback'ами, им обратный поиск не нужен.
_TEXT_BUTTONS = ("BTN_RENT", "BTN_TRIPS", "BTN_TARIFFS", "BTN_SUPPORT",
                 "BTN_FAQ", "BTN_CLOSE_RENT", "BTN_CANCEL", "BTN_SAME_ADDRESS",
                 "BTN_SHARE_CONTACT")


def norm(lang: str | None) -> str:
    """Код языка из базы -> поддерживаемый. Неизвестный или пустой - русский."""
    return lang if lang in PACKS else "ru"


def user_lang(user: dict | None) -> str:
    return norm((user or {}).get("lang"))


def t(lang: str | None, key: str) -> str:
    """Текст по ключу на языке клиента, с молчаливым русским fallback.

    Возвращает ШАБЛОН: плейсхолдеры {number}/{price}/... подставляет
    вызывающий тем же .format(), что и раньше, - поэтому замена
    texts.X на t(lang, "X") не трогает остальную строку кода.
    """
    lang = norm(lang)
    if lang != "ru":
        val = PACKS[lang].get(key)
        if val:
            return val
    if key in BUTTONS_RU:
        return BUTTONS_RU[key]
    return getattr(texts, key)


def err(lang: str | None, message: str) -> str:
    """Перевод ошибки валидации по её русскому тексту. Нет перевода - как есть."""
    lang = norm(lang)
    if lang == "ru":
        return message
    return ERRORS[lang].get(message, message)


def variants(key: str) -> frozenset[str]:
    """Все языковые варианты подписи кнопки, включая русский."""
    labels = {BUTTONS_RU[key]}
    labels.update(p[key] for p in PACKS.values() if p.get(key))
    return frozenset(labels)


_REVERSE: dict[str, str] = {}
for _key in _TEXT_BUTTONS:
    for _label in variants(_key):
        _REVERSE[_label] = _key


def button_key(text: str | None) -> str | None:
    """Какой кнопке меню принадлежит текст сообщения - на любом языке."""
    return _REVERSE.get((text or "").strip())
