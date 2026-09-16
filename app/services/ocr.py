"""Чтение машиночитаемой зоны с фотографии паспорта. Всё на своём сервере.

Наружу ничего не уходит: tesseract стоит в том же контейнере, что бот и
панель, картинка передаётся ему через stdin и на диск вторым файлом не
ложится. Это и было условием - паспорт не должен покидать VPS в РФ, иначе
понадобилось бы поручение на обработку по 152-ФЗ и правка текста согласия.

Разбор прочитанного - в app/services/mrz.py; здесь только картинка -> текст.
Любая неудача (нет tesseract, битый файл, пустой вывод) возвращает None:
распознавание - подсказка модератору, и его отсутствие не должно ломать
ни регистрацию, ни карточку.
"""

from __future__ import annotations

import asyncio
import io
import logging
import shutil
import subprocess

from .. import logic, texts
from . import mrz

log = logging.getLogger(__name__)

# МЧЗ состоит только из заглавной латиницы, цифр и «<». Белый список режет
# большую часть ошибок распознавания ещё до контрольных цифр.
CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<"
TIMEOUT_SEC = 20

# Ширина, до которой растягивается картинка перед распознаванием: строки МЧЗ
# на телефонном снимке мелкие, а tesseract уверенно читает текст примерно
# от 30 пикселей в высоту.
TARGET_WIDTH = 2000
# Доля высоты снизу, где на развороте паспорта лежит МЧЗ. Сначала пробуем
# эту полосу - на ней меньше постороннего текста, - потом всю страницу.
BOTTOM_STRIP = 0.35


def available() -> bool:
    """Установлен ли tesseract. Без него модуль просто молчит."""
    return shutil.which("tesseract") is not None


def _run(image_bytes: bytes) -> str:
    """tesseract из stdin в stdout: временных файлов с паспортом не остаётся."""
    result = subprocess.run(                            # noqa: S603
        ["tesseract", "stdin", "stdout", "--psm", "6", "-l", "eng",
         "-c", f"tessedit_char_whitelist={CHARS}",
         "-c", "classify_bln_numeric_mode=0"],
        input=image_bytes, capture_output=True, timeout=TIMEOUT_SEC, check=False,
    )
    return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else ""


def _prepared(raw: bytes) -> list[bytes]:
    """Картинки для распознавания: сначала нижняя полоса, потом вся страница.

    Pillow здесь нужен ровно для трёх вещей - обесцветить, обрезать и
    увеличить. Без них tesseract на снимке с телефона читает МЧЗ примерно
    в половине случаев, с ними - почти всегда.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:                                 # pragma: no cover
        log.warning("Pillow не установлен, МЧЗ читается по исходной картинке")
        return [raw]
    try:
        with Image.open(io.BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source).convert("L")
    except Exception:                                   # noqa: BLE001
        log.warning("не удалось открыть картинку для распознавания МЧЗ")
        return [raw]

    variants = []
    strip = image.crop((0, int(image.height * (1 - BOTTOM_STRIP)),
                        image.width, image.height))
    for candidate in (strip, image):
        if candidate.width < TARGET_WIDTH:
            scale = TARGET_WIDTH / candidate.width
            candidate = candidate.resize(
                (TARGET_WIDTH, max(int(candidate.height * scale), 1)),
                Image.LANCZOS)
        buf = io.BytesIO()
        candidate.save(buf, format="PNG")
        variants.append(buf.getvalue())
    return variants


def read(raw: bytes) -> str | None:
    """Текст с картинки, где ожидается МЧЗ. None - прочитать не удалось."""
    if not available():
        return None
    try:
        for image_bytes in _prepared(raw):
            text = _run(image_bytes)
            if "<<" in text:                            # похоже на МЧЗ
                return text
        return None
    except subprocess.TimeoutExpired:
        log.warning("tesseract не уложился в %s с - МЧЗ не прочитана", TIMEOUT_SEC)
        return None
    except Exception:                                   # noqa: BLE001
        log.exception("распознавание МЧЗ не удалось")
        return None


def read_file(path: str | None) -> str | None:
    """То же самое по пути на диске. Файла нет - None, без исключения."""
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            return read(fh.read())
    except OSError:
        log.warning("скан %s не открылся для распознавания МЧЗ", path)
        return None


async def card_line(data: dict, anketa: dict) -> str:
    """Строка сверки МЧЗ для карточки модератора. Пусто - показывать нечего.

    Живёт здесь, а не в обработчике: карточку собирают и телеграм-бот, и
    зеркало для MAX, а видеть модератор должен одно и то же.

    Распознавание идёт при сборке карточки, а не при загрузке фотографии:
    так паспортные данные не появляются в базе вторым экземпляром -
    прочитанное живёт только в подписи. Tesseract блокирующий и уходит
    в поток; любая неудача - пустая строка, карточка уходит как раньше.
    """
    try:
        text = await asyncio.to_thread(read_file, data.get("doc_path"))
        found = mrz.summary(mrz.parse(text or ""), anketa)
    except Exception:                                   # noqa: BLE001
        log.exception("сверка МЧЗ для %s не удалась", data.get("tg_id"))
        return ""
    if found is None:
        return ""
    matched, line = found
    template = texts.CARD_MRZ_OK_LINE if matched else texts.CARD_MRZ_DIFF_LINE
    return template.format(summary=logic.esc(line))
