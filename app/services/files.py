"""Скачивание фото из Telegram и укладка на диск."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path

from aiogram import Bot

log = logging.getLogger(__name__)


class TooLarge(Exception):
    """Файл больше допустимого - качать его в память не станем."""


async def download(bot: Bot, file_id: str, max_bytes: int) -> bytes:
    """Скачивание с проверкой размера ДО чтения в память.

    getFile отдаёт file_size заранее, и это единственный дешёвый способ не
    затащить в оперативку двадцатимегабайтный файл на каждое сообщение.
    """
    file = await bot.get_file(file_id)
    if file.file_size and file.file_size > max_bytes:
        raise TooLarge(f"{file.file_size} байт при лимите {max_bytes}")
    buf = await bot.download_file(file.file_path)
    data = buf.read()
    if len(data) > max_bytes:
        raise TooLarge(f"{len(data)} байт при лимите {max_bytes}")
    return data


# Слот -> расширение файла. Расширение задаётся здесь, а не аргументом:
# имя файла на диске сверяется с шаблоном перед удалением (logic.STORE_FILE_NAME),
# и произвольное расширение означало бы, что ретеншен такой файл не опознает
# и не удалит - скан или договор останется на диске навсегда.
SLOT_EXT = {"doc": "jpg", "parent": "jpg", "contract": "docx",
            "soglasie": "docx", "actin": "docx", "actout": "docx"}


def store(storage_dir: Path, tg_id: int, slot: str, data: bytes) -> tuple[Path, str]:
    """Кладёт файл на диск и возвращает путь и sha256.

    Хэш нужен не для целостности, а для антифрода: один и тот же документ
    не должен проходить регистрацию с разных аккаунтов.
    """
    if slot not in SLOT_EXT:
        raise ValueError(f"неизвестный слот {slot!r}")
    storage_dir.mkdir(parents=True, exist_ok=True)
    # mkdir применяет umask, поэтому права выставляем явно: в каталоге лежат
    # изображения паспортов и договоры, чужим процессам там делать нечего.
    storage_dir.chmod(0o700)

    digest = hashlib.sha256(data).hexdigest()
    path = storage_dir / f"{int(tg_id)}-{slot}-{int(time.time() * 1000)}.{SLOT_EXT[slot]}"
    # Открываем сразу с нужными правами, а не chmod после записи: иначе между
    # созданием файла и сменой прав есть окно, когда скан читается всеми.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path, digest


def remove(path: str | Path) -> bool:
    """Удаление скана. False - файл остался, значит ссылку в базе стирать
    нельзя: иначе он останется на диске навсегда и без следа."""
    try:
        Path(path).unlink(missing_ok=True)
        return True
    except OSError as exc:
        log.error("не удалось удалить %s: %s", path, exc)
        return False
