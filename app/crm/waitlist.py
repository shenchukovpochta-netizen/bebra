"""Лист ожидания: освободился велосипед - позвать тех, кто его ждёт.

Почему сверка по журналам, а не крючок в каждом пути. «Велосипед снова
свободен» случается в десятке мест: возврат из бота и из панели, закрытие
наряда, ввод в эксплуатацию, пересчёт, перенос между точками, карточка,
импорт. Крючок в каждом забудет первый же новый путь (и столкнётся с
соседними правками возврата), а журналы статусов и мест пишет база
триггером на любой из них. Поэтому круг напоминаний (tasks.reminders_loop,
15 минут) смотрит свободные сейчас велосипеды, у которых за сутки была
строка «стал свободен» или «переехал», и сверяет их с открытыми заявками.
Четверть часа задержки курьеру, который едет «сегодня», ничего не стоит.

Курсора в настройках нет: сколько людей уже позвали к велосипеду, видно
по самим заявкам (waitlist_bike_id и waitlist_at позже освобождения), а
«не чаще раза в сутки» держит условие в UPDATE (`mark_waitlist`).
Перезапуск бота второго сообщения не даёт, окно в сутки переносит
сданный вечером велосипед на утро - ночью не пишем.

Велосипед не бронируется (CLAUDE.md, «Заявка на аренду»): кто первым
приедет, того и он, и клиент читает это в самом сообщении.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from . import logic, notices, notify

log = logging.getLogger(__name__)


def _can_hear(client: dict | None) -> bool:
    """Звать можно действующего клиента с Telegram: остальным писать некуда
    или нельзя (чёрный список)."""
    return bool(client and client.get("tg_id") and client.get("status") == "active")


async def run_once(bot: Any, db: Any, crm: Any, *, now: datetime) -> int:
    """Один круг: кого позвать к освободившимся велосипедам. Возвращает
    число доставленных сообщений. `now` - местное время круга."""
    state = await notices.settings(crm)
    setting = state.get("waitlist") or {}
    if bot is None or not setting.get("enabled", True):
        return 0
    if not logic.waitlist_hours_ok(setting, now):
        return 0
    moment = now.astimezone()
    freed = await crm.freed_bikes(moment - logic.WAITLIST_LOOKBACK)
    if not freed:
        return 0
    bookings = await crm.bookings(status="new", limit=1000)
    if not bookings:
        return 0
    aliases = logic.model_aliases(await crm.bike_models())
    locations = await crm.locations(active_only=True)
    per_bike = logic.notice_param(setting, "per_bike", logic.WAITLIST_PER_BIKE)
    today = moment.date()
    sent = 0
    for bike in freed:
        slots = per_bike - logic.waitlist_taken(bike, bookings)
        for booking in logic.waitlist_queue(bike, bookings, today=today, aliases=aliases):
            if slots <= 0:
                break
            client = await crm.client(booking["client_id"])
            if not _can_hear(client) or await crm.active_rental_of(client["id"]):
                continue
            if logic.booking_served(booking, await crm.client_rentals(client["id"], limit=1)):
                continue                 # выдали мимо заявки - ждать нечего
            if not await crm.mark_waitlist(booking["id"], bike["id"]):
                continue                 # сегодня уже звали - другим кругом
            # Та же строка в списке заявок: следующий велосипед этого круга
            # не позовёт клиента второй раз за день.
            row = next(b for b in bookings if b["id"] == booking["id"])
            row.update(waitlist_at=moment, waitlist_bike_id=bike["id"])
            slots -= 1
            ok = await notices.send_client(
                crm, "waitlist", client["id"],
                lambda c=client, b=booking, x=bike: notify.waitlist_free(
                    bot, db, c, b, x, locations=locations))
            if ok:
                sent += 1
                continue
            # Не дошло (бот заблокирован, уведомление выключено посреди
            # круга): место у велосипеда - следующему, а этого клиента
            # сегодня больше не зовём - отметка времени остаётся.
            await crm.update_booking(booking["id"], waitlist_bike_id=None)
            row["waitlist_bike_id"] = None
            slots += 1
    return sent


async def free_bike(crm: Any, booking: dict, bike_id: int | None) -> dict | None:
    """Свободный велосипед под заявку: тот, о котором писали, иначе любой
    той же модели на той же точке - клиенту важна модель, а не номер.
    None - все разобраны."""
    aliases = logic.model_aliases(await crm.bike_models())
    bike = await crm.bike(bike_id) if bike_id is not None else None
    if bike is not None and logic.waitlist_fits(bike, booking, aliases=aliases):
        return bike
    for row in await crm.bikes(status="available", limit=10000):
        if logic.waitlist_fits(row, booking, aliases=aliases):
            return row
    return None
