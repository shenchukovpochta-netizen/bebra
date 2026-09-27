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

Отсюда же часы работы в текстах бота (hours_note): у точек справочника
режим свой, и общее «ежедневно 10–19» в заявке или напоминании было бы
неправдой для точки «пн-пт 9–21».
"""

from __future__ import annotations

import html
import logging
import time
from collections.abc import Iterable, Mapping
from typing import Any

from .. import i18n

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


def hours_note(lang: str | None, location: str | None = None, *,
               rows: Iterable[Mapping[str, Any]] | None = None) -> str:
    """Часы работы для текста клиенту: приставка «\\n…» к фразе или пусто.

    location - точка аренды или заявки (имя из справочника): текст про
    неё называет её часы, а не чужие. Без точки (или её с тех пор
    закрыли) - все открытые: одна - её часы, несколько - списком.
    Точка без режима пропускается: выдумывать ей часы нельзя, время
    назовёт оператор. Справочника нет (пустой снимок, бот без CRM) -
    прежние зашитые часы, как у ответов app/faq.py.
    rows - строки справочника, если их уже прочитали; иначе снимок.
    """
    open_rows = [row for row in (snapshot() if rows is None else rows)
                 if row.get("active", True)]
    if not open_rows:
        return "\n" + i18n.t(lang, "HOURS_DEFAULT")
    own = [row for row in open_rows if location and row.get("name") == location]
    lines = [line for line in map(_hours_line, own or open_rows) if line]
    if not lines:
        return ""
    key = "HOURS_POINT" if len(lines) == 1 else "HOURS_POINTS"
    return "\n" + i18n.t(lang, key).format(lines="\n".join(lines))


def _hours_line(row: Mapping[str, Any]) -> str:
    """«📍 точка · 🕙 режим» - как короткий список точек в app/faq.py.
    Значения из панели экранируются: «&» в названии - сообщение, которое
    Telegram не разберёт."""
    hours = str(row.get("hours") or "").strip()
    title = str(row.get("public_title") or row.get("name") or "").strip()
    if not hours:
        return ""
    return f"📍 {html.escape(title, quote=False)} · 🕙 {html.escape(hours, quote=False)}"


async def rental_location(crm: Any, tg_id: int) -> str | None:
    """Точка идущей аренды клиента бота - для часов в тексте про неё.

    None - бот без CRM, карточки или аренды в CRM нет, или база не
    ответила: тогда текст назовёт часы всех точек, а не упадёт. Клиенту
    нужен ответ на его запрос, а не трассировка.
    """
    if crm is None:
        return None
    try:
        client = await crm.client_by_tg(tg_id)
        rental = await crm.active_rental_of(client["id"]) if client else None
    except Exception:                                    # noqa: BLE001
        log.exception("точка аренды клиента %s не прочитана", tg_id)
        return None
    return (rental or {}).get("location") or None


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
