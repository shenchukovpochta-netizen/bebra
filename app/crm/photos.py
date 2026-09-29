"""Фото при сдаче: файлы на томе bikefiles и их строки в crm.return_photos.

Пишут двое: панель (форма закрытия аренды) и бот (ответ оператора фото на
карточку сдачи). Поэтому запись одна на оба входа - здесь, рядом с
удалением по сроку, которое зовёт дневной проход бота. Том тот же, что у
снимков сверки: персональных данных в снимке велосипеда нет, а том с
паспортами панель не пишет и писать не должна.

Имя файла собираем сами (`logic.return_photo_path`): имя из браузера или
Telegram - чужая строка. Перед показом и удалением путь сверяется с тем
же шаблоном, так что строка базы не уведёт ни чтение, ни unlink за
пределы каталога.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Any

from . import logic, service

log = logging.getLogger(__name__)


def path_of(folder: Path | str, path: Any) -> Path | None:
    """Файл снимка на диске или None, если путь из базы не наш."""
    if not logic.is_return_photo_path(path):
        return None
    return Path(folder) / str(path)


async def save(crm: Any, folder: Path | str, rental: dict, raw: bytes, suffix: str,
               *, by: str) -> int:
    """Снимок на диск и строка к аренде. Отказ - ServiceError с текстом
    для человека; файл без строки на диске не остаётся."""
    ext = logic.RETURN_PHOTO_SUFFIXES.get(str(suffix or "").lower())
    if ext is None:
        raise service.ServiceError("Снимок: только jpg, png или webp.")
    if not raw:
        raise service.ServiceError("Снимок пустой — пришлите ещё раз.")
    if len(raw) > logic.RETURN_PHOTO_MAX_BYTES:
        raise service.ServiceError(
            f"Снимок больше {logic.RETURN_PHOTO_MAX_BYTES // (1024 * 1024)} МБ — "
            "сфотографируйте меньшим размером.")
    rel = logic.return_photo_path(rental["id"], ext, secrets.token_hex(6))
    target = Path(folder) / rel
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Сразу с правами и без перезаписи: имя случайное, и совпадение
        # значит ошибку, а не повод затереть чужой снимок.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
    except OSError as err:
        log.warning("фото при сдаче не сохранено: %s", err)
        raise service.ServiceError("Снимок не сохранился — попробуйте ещё раз.") from err
    try:
        photo_id = await crm.add_return_photo(
            rental["id"], bike_id=rental.get("bike_id"), path=rel, created_by=by,
            limit=logic.RETURN_PHOTOS_MAX)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if photo_id is None:
        target.unlink(missing_ok=True)
        raise service.ServiceError(
            f"К аренде уже {logic.RETURN_PHOTOS_MAX} фото — больше не принимаем.")
    return int(photo_id)


async def purge(crm: Any, folder: Path | str, days: int) -> int:
    """Снимки старше срока: сперва файл, потом строка. Файл, который не
    удалился, оставляет и строку - иначе он пролежал бы на диске вечно
    и без следа (так же устроен ретеншен сканов)."""
    gone: list[int] = []
    for row in await crm.old_return_photos(days):
        target = path_of(folder, row.get("path"))
        if target is None:
            log.warning("фото при сдаче %s: чужой путь в базе, строку убираю",
                        row.get("id"))
            gone.append(int(row["id"]))
            continue
        try:
            target.unlink(missing_ok=True)
        except OSError as err:
            log.error("фото при сдаче %s не удалено: %s", target, err)
            continue
        gone.append(int(row["id"]))
    return await crm.drop_return_photos(gone) if gone else 0
