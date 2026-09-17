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
import time
from typing import Any

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
                 for code in COMPANY_FIELDS}
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
