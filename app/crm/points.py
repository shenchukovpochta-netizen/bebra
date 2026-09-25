"""Точки выдачи для бота: снимок открытых точек справочника.

Точки заводит и правит владелец в панели (`crm.locations`), а отвечает
клиенту «где вы» и «до скольки работаете» бот - другой процесс. Общая у
них только база, поэтому здесь, как у реквизитов (company.py), снимок с
коротким сроком жизни: третья точка, заведённая в панели, доезжает до
ответов бота за несколько минут и без перезапуска.

В снимке только открытые точки и только то, что можно сказать клиенту:
название, адрес, как найти, режим и телефон. Описание и координаты сюда
не едут - описание пишут для своих, а не для курьера; «как найти» -
отдельное поле именно для клиента («заезд в ГСК, 9-й бокс»).

Пустой снимок - это «справочника нет», а не «точек нет»: ответы бота
тогда остаются зашитыми в app/faq.py. Так же читает пустой справочник
и панель (db.location_names -> logic.LOCATIONS).
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# Что о точке знает клиент: название для курьеров (иначе служебное имя),
# адрес, как найти, режим работы, телефон.
FIELDS: tuple[str, ...] = ("name", "public_title", "address", "directions", "hours",
                           "phone")

# Сколько живёт снимок. Точки заводят раз в сезон, минуты задержки после
# правки в панели никого не задевают, а запрос на каждый апдейт - лишний.
TTL_SECONDS = 300

_snapshot: list[dict[str, str]] = []
# None - снимка ещё не было. Не 0.0: time.monotonic() считает от загрузки
# системы, и первые пять минут после перезагрузки сервера «загружено в 0»
# выглядело бы свежим - бот отвечал бы зашитыми адресами до первого TTL.
_loaded_at: float | None = None


def snapshot() -> list[dict[str, str]]:
    """Открытые точки в порядке справочника. Пусто - справочника нет."""
    return [dict(row) for row in _snapshot]


def set_snapshot(rows: list[dict[str, Any]] | None) -> None:
    """Подменить снимок - для тестов и для первого чтения на старте.

    Закрытая точка отсекается и здесь: снимок могут подать строками всего
    справочника, а звать клиента на закрытую точку нельзя.
    """
    global _snapshot, _loaded_at
    _snapshot = [{field: str(row.get(field) or "").strip() for field in FIELDS}
                 for row in rows or () if row.get("active", True)]
    _loaded_at = time.monotonic()


def reset() -> None:
    """Забыть снимок: следующий refresh обязательно сходит в базу."""
    global _snapshot, _loaded_at
    _snapshot = []
    _loaded_at = None


def is_fresh(*, now: float | None = None) -> bool:
    if _loaded_at is None:
        return False
    return (now if now is not None else time.monotonic()) - _loaded_at < TTL_SECONDS


async def refresh(crm: Any, *, force: bool = False) -> list[dict[str, str]]:
    """Перечитать открытые точки, если снимок устарел.

    Бот без CRM справочника не видит вовсе: снимок сбрасывается, и ответы
    остаются зашитыми, а не тем, что в снимке осталось от прошлого. Ошибка
    базы снимок не роняет: вчерашний список точек лучше зашитого.
    """
    if crm is None:
        reset()
        return []
    # Пустой справочник - тоже снимок: ходить за ним каждый апдейт незачем.
    if not force and is_fresh():
        return snapshot()
    try:
        set_snapshot(await crm.locations(active_only=True))
    except Exception:                                    # noqa: BLE001
        log.exception("точки выдачи не перечитаны")
    return snapshot()
