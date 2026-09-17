"""Реквизиты организации: одно место, откуда их берут документы.

Реквизиты правит владелец в панели, а подставляет их в договор и акты
бот - другой процесс. Общая у них только база, поэтому здесь снимок
настроек с коротким сроком жизни: панель пишет в `crm.settings`, бот
подхватывает в течение нескольких минут.

Пустой реквизит превращается в прочерк, а не в «None»: договор с «None»
посреди шапки хуже, чем договор с прочерком. Шаблон, который реквизиты
не использует (они вписаны в него текстом), не меняется вовсе - поля
просто не встречаются.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from .. import texts

log = logging.getLogger(__name__)

# Поля реквизитов: код -> подпись в панели. Порядок - как в карточке
# организации у банка, чтобы переписывать с выписки сверху вниз.
COMPANY_FIELDS: dict[str, str] = {
    "company_name": "Наименование полное",
    "company_short": "Наименование краткое",
    "company_inn": "ИНН",
    "company_ogrn": "ОГРН / ОГРНИП",
    "company_tax": "Система налогообложения",
    "company_address": "Юридический адрес",
    "company_phone": "Телефон",
    "company_email": "Электронная почта",
    "company_bank": "Банк",
    "company_account": "Расчётный счёт",
    "company_bik": "БИК",
    "company_corr": "Корреспондентский счёт",
    "company_director": "Кто подписывает договоры",
}

# Контакты для клиента - отдельно от реквизитов: реквизиты идут в документы,
# а это то, что клиент видит в боте. Ссылка на менеджера вшита в texts.py и
# в восемь языковых пакетов, поэтому настройка подменяет её подстановкой в
# готовый текст: иначе смена аккаунта означала бы правку девяти файлов.
CONTACT_FIELDS: dict[str, str] = {
    "support_contact": "Ссылка на менеджера",
}

# Всё, что панель пишет в настройки на этой странице, а бот читает снимком.
ALL_FIELDS: dict[str, str] = {**COMPANY_FIELDS, **CONTACT_FIELDS}

# Сколько живёт снимок. Реквизиты меняют раз в год, поэтому минуты
# задержки после правки в панели никого не задевают, а запрос на каждый
# апдейт бота - лишний.
TTL_SECONDS = 300

_snapshot: dict[str, str] = {}
_loaded_at = 0.0


def context(values: dict[str, Any] | None = None) -> dict[str, str]:
    """Значения для подстановки в шаблон: все поля, пустые - прочерком."""
    values = values or {}
    return {code: (str(values.get(code) or "").strip() or "—")
            for code in COMPANY_FIELDS}


def snapshot() -> dict[str, str]:
    """Последние известные реквизиты. Пусто - значит ещё не читали."""
    return dict(_snapshot)


# Длина реквизита: банковская выписка в одну строку не бывает длиннее.
VALUE_LIMIT = 200


def check_value(raw: Any) -> tuple[str, str]:
    """Значение реквизита из формы: (значение, ошибка).

    Отдельная проверка, а не check_name: в реквизитах цифры, дроби и
    точки - ИНН и БИК не прошли бы проверку имени.
    """
    text = " ".join(str(raw or "").split())
    if len(text) > VALUE_LIMIT:
        return "", f"не длиннее {VALUE_LIMIT} символов"
    return text, ""


def set_snapshot(values: dict[str, Any] | None) -> None:
    """Подменить снимок - для тестов и для первого чтения на старте."""
    global _snapshot, _loaded_at
    _snapshot = {code: str((values or {}).get(code) or "")
                 for code in ALL_FIELDS}
    _loaded_at = time.monotonic()


def reset() -> None:
    """Забыть снимок: следующий refresh обязательно сходит в базу."""
    global _snapshot, _loaded_at
    _snapshot = {}
    _loaded_at = 0.0


def is_fresh(*, now: float | None = None) -> bool:
    return (now or time.monotonic()) - _loaded_at < TTL_SECONDS


async def refresh(crm: Any, *, force: bool = False) -> dict[str, str]:
    """Перечитать реквизиты, если снимок устарел.

    Ошибка базы снимок не роняет: документы важнее свежести реквизитов,
    и старые значения лучше прочерков.
    """
    # Пустой снимок - это тоже снимок: у организации может не быть ни
    # одного заполненного реквизита, и ходить за ними каждый апдейт незачем.
    if crm is None or (not force and is_fresh()):
        return snapshot()
    try:
        set_snapshot(await crm.settings())
    except Exception:                                    # noqa: BLE001
        log.exception("реквизиты организации не перечитаны")
    return snapshot()


# ─────────────────────── контакт менеджера ───────────────────────
# Код настройки: ею подменяется контакт, зашитый в texts.SUPPORT_CONTACT_URL.
CONTACT_SETTING = "support_contact"


def check_contact(raw: Any) -> tuple[str, str]:
    """Контакт менеджера из формы: (значение, ошибка).

    Пусто - не ошибка, а «работает зашитый контакт»: стереть поле должно
    быть можно, иначе владелец останется с чужой ссылкой навсегда.
    «@имя» разворачивается в ссылку: контакт подставляется в обычный
    текст сообщения, и кликабельной строку делает именно ссылка.
    """
    text = " ".join(str(raw or "").split())
    if not text:
        return "", ""
    if len(text) > VALUE_LIMIT:
        return "", f"не длиннее {VALUE_LIMIT} символов"
    if text.startswith("@"):
        if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", text[1:]):
            return "", "имя в Telegram - латиница, цифры и подчёркивание"
        return f"https://t.me/{text[1:]}", ""
    if text.startswith("t.me/"):
        text = "https://" + text
    if not text.startswith(("http://", "https://")):
        return "", "ссылка вида https://t.me/… или @имя"
    return text, ""


def support_url(values: dict[str, Any] | None = None) -> str:
    """Контакт менеджера для клиента: настройка, иначе зашитый в texts."""
    raw = str((values or _snapshot).get(CONTACT_SETTING) or "").strip()
    return raw or texts.SUPPORT_CONTACT_URL


def with_contact(text: str, url: str | None = None) -> str:
    """Подставить контакт менеджера в готовый текст клиента.

    replace, а не format: в текстах клиента встречаются фигурные скобки
    сами по себе, и format упал бы на них уже на живом человеке.
    Заменяется значение по умолчанию - оно одно на все языки, поэтому
    один вызов чинит и русский текст, и восемь переводов сразу.
    """
    url = url or support_url()
    if url == texts.SUPPORT_CONTACT_URL:
        return text
    return text.replace(texts.SUPPORT_CONTACT_URL, url)
