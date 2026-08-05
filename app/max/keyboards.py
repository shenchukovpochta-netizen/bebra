"""Inline-клавиатуры для MAX.

В MAX нет reply-клавиатур - всё, что в Telegram было кнопками у поля ввода
(меню, «Поделиться контактом», «Отмена»), здесь становится inline-кнопками
под сообщением. Контакт запрашивается кнопкой типа request_contact.
"""

from __future__ import annotations


def _cb(text: str, payload: str) -> dict:
    return {"type": "callback", "text": text, "payload": payload}


def _link(text: str, url: str) -> dict:
    return {"type": "link", "text": text, "url": url}


def subscribe(channel_url: str) -> list:
    return [
        [_link("Подписаться на канал", channel_url)],
        [_cb("Проверить подписку", "check_sub")],
    ]


def oferta(url: str = "", pdn_url: str = "") -> list:
    """Экран согласия на обработку ПДн; обе ссылки необязательны."""
    rows = []
    if url:
        rows.append([_link("Правила проката", url)])
    if pdn_url:
        rows.append([_link("Политика обработки ПДн", pdn_url)])
    rows.append([_cb("✅ Даю согласие", "oferta_ok")])
    return rows


def share_contact() -> list:
    return [[{"type": "request_contact", "text": "📱 Поделиться контактом"}]]


def same_address() -> list:
    return [[_cb("Совпадает с регистрацией", "same_addr")]]


def confirm() -> list:
    return [
        [_cb("Подтверждаю", "confirm")],
        [_cb("Заполнить повторно", "restart")],
    ]


def moderation(tg_id: int) -> list:
    return [[
        _cb("✅ Одобрить", f"approve:{tg_id}"),
        _cb("⛔ Отклонить", f"reject:{tg_id}"),
    ]]


def reject_reasons(tg_id: int, reasons: dict[str, tuple[str, str]]) -> list:
    rows = [[_cb(title, f"rj:{tg_id}:{code}")]
            for code, (title, _state) in reasons.items()]
    rows.append([_cb("✍️ Свой текст", f"rjc:{tg_id}")])
    rows.append([_cb("← Назад", f"rjx:{tg_id}")])
    return rows


def sign_contract() -> list:
    return [
        [_cb("✍️ Подписываю", "sign")],
        [_cb("Есть ошибка", "contract_mistake")],
    ]


def main_menu() -> list:
    return [
        [_cb("🚲 Арендовать", "menu:tariffs"), _cb("💰 Тарифы", "menu:tariffs")],
        [_cb("📋 Мои поездки", "menu:trips"), _cb("🆘 Поддержка", "menu:support")],
    ]


def support_cancel() -> list:
    return [[_cb("Отмена", "support_cancel")]]
