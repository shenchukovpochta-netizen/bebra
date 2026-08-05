"""Формирование договора проката в DOCX.

Шаблон - настоящий docx-файл договора, в пустые графы которого вписаны
подстановки `{{ поле }}`. Бот не собирает документ заново, а заполняет
исходный файл юриста: вёрстка, шрифты и таблицы остаются нетронутыми -
меняются только подставленные значения. Поэтому «слетать» в документе
нечему, а прочерки (марка велосипеда, срок проката, арендная плата)
дозаполняются в Word при выдаче, как в бумажном оригинале.

Отпечаток документа. В договор печатается SHA-256, посчитанный по
word/document.xml с уже подставленными данными, но с пустым значением
на месте самого отпечатка. Проверка задним числом: распаковать docx,
в word/document.xml стереть напечатанное значение отпечатка, посчитать
SHA-256 заново. Хэшировать весь файл docx бессмысленно: zip несёт
метаданные времени сборки, и два одинаковых договора дали бы разные хэши.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)

PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
DOCUMENT_XML = "word/document.xml"
HASH_FIELD = "contract_sha256"


class TemplateProblem(Exception):
    """Шаблон недоступен или испорчен - договор формировать не из чего."""


def load_template(path: Path) -> bytes:
    """Читает шаблон и проверяет, что это docx с подстановками.

    Вызывается и на старте бота: опечатка в пути или битый файл должны
    обнаружиться сразу, а не в момент, когда пользователю уже сказано
    «заявка одобрена».
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise TemplateProblem(f"не читается шаблон договора {path}: {exc}") from exc
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read(DOCUMENT_XML).decode("utf-8")
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError) as exc:
        raise TemplateProblem(
            f"шаблон договора {path} не является docx-файлом: {exc}") from exc
    if not PLACEHOLDER.search(xml):
        raise TemplateProblem(
            f"в шаблоне договора {path} нет ни одной подстановки {{{{ поле }}}}")
    return data


def placeholders(template: bytes) -> set[str]:
    """Имена всех подстановок шаблона - для сверки с контекстом в тестах."""
    with zipfile.ZipFile(io.BytesIO(template)) as zf:
        xml = zf.read(DOCUMENT_XML).decode("utf-8")
    return set(PLACEHOLDER.findall(xml))


def substitute(xml: str, ctx: dict[str, Any]) -> str:
    """Подстановка `{{ поле }}` с экранированием под XML.

    Неизвестное поле остаётся в документе видимой пометкой, а не пустотой:
    опечатка в шаблоне должна бросаться в глаза тому, кто первый раз откроет
    договор, а не тихо выкидывать из него реквизит.
    """
    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in ctx:
            log.warning("в шаблоне договора неизвестное поле %r", key)
            return f"«нет поля {key}»"
        return escape(str(ctx[key]))

    return PLACEHOLDER.sub(replace, xml)


def drop_paragraph_with(xml: str, marker: str) -> str:
    """Удаляет абзац <w:p>...</w:p>, содержащий маркер.

    Нужен оговорке для 16-17-летних: у взрослого она пуста, и пустой абзац
    посреди договора выглядит браком. Границы ищутся строками, а не общей
    регуляркой: document.xml велик, и жадный шаблон с DOTALL легко уехал бы
    за соседние абзацы.
    """
    at = xml.find(marker)
    if at == -1:
        return xml
    start = xml.rfind("<w:p ", 0, at)
    if start == -1:
        start = xml.rfind("<w:p>", 0, at)
    end = xml.find("</w:p>", at)
    if start == -1 or end == -1:
        return xml
    return xml[:start] + xml[end + len("</w:p>"):]


def render_xml(template: bytes, ctx: dict[str, Any]) -> tuple[str, str]:
    """(заполненный document.xml, отпечаток).

    Отпечаток считается по тексту с пустым значением на месте самого
    отпечатка - иначе его нельзя было бы проверить по готовому документу.
    """
    with zipfile.ZipFile(io.BytesIO(template)) as zf:
        xml = zf.read(DOCUMENT_XML).decode("utf-8")

    if not str(ctx.get("minor_clause") or "").strip():
        xml = drop_paragraph_with(xml, "{{ minor_clause }}")

    without_hash = substitute(xml, {**ctx, HASH_FIELD: ""})
    digest = hashlib.sha256(without_hash.encode("utf-8")).hexdigest()
    return substitute(xml, {**ctx, HASH_FIELD: digest}), digest


def build(template_path: Path, ctx: dict[str, Any]) -> tuple[bytes, str]:
    """Готовый договор: (docx, отпечаток).

    Все части исходного файла, кроме word/document.xml, копируются байт
    в байт - стили, шрифты, колонтитулы и нумерация остаются ровно теми,
    какими их сохранил юрист.
    """
    template = load_template(template_path)
    filled, digest = render_xml(template, ctx)

    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(template)) as src, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            if item.filename == DOCUMENT_XML:
                dst.writestr(item, filled)
            else:
                dst.writestr(item, src.read(item.filename))
    return out.getvalue(), digest
