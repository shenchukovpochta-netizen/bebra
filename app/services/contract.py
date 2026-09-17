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
RELS_XML = "word/_rels/document.xml.rels"
CONTENT_TYPES = "[Content_Types].xml"
HASH_FIELD = "contract_sha256"
# Подстановки, на месте которых в документ вставляется картинка, и её
# размер в EMU (914400 EMU = 1 дюйм). Подпись шире печати: так они и
# выглядят на бумаге.
MARK_FIELDS = {"signature": (1828800, 685800), "stamp": (1371600, 1371600)}
DRAWING_NS = ("xmlns:wp=\"http://schemas.openxmlformats.org/drawingml/2006/"
              "wordprocessingDrawing\" "
              "xmlns:a=\"http://schemas.openxmlformats.org/drawingml/2006/main\" "
              "xmlns:pic=\"http://schemas.openxmlformats.org/drawingml/2006/"
              "picture\"")


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
    # Из двух вариантов открытия абзаца - с атрибутами и без - берётся
    # БЛИЖАЙШИЙ к маркеру. Предпочтение «<w:p » находило открытие соседнего
    # абзаца (абзац оговорки записан без атрибутов), и у взрослых из договора
    # вырезалась ещё и строка «(номер телефона свой и второй)» перед ней.
    start = max(xml.rfind("<w:p ", 0, at), xml.rfind("<w:p>", 0, at))
    end = xml.find("</w:p>", at)
    if start == -1 or end == -1:
        return xml
    return xml[:start] + xml[end + len("</w:p>"):]


def picture_xml(rel_id: str, name: str, width: int, height: int) -> str:
    """Врезка картинки на месте подстановки.

    Собрано руками, без библиотеки: docx - это zip с xml, и вставка
    одной картинки короче, чем зависимость ради неё. Картинка идёт
    inline, в поток текста: «плавающая» уехала бы при первой правке
    документа в Word.
    """
    return (
        f'<w:drawing {DRAWING_NS}>'
        f'<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{width}" cy="{height}"/>'
        f'<wp:docPr id="{abs(hash(rel_id)) % 100000 + 1}" name="{name}"/>'
        f'<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/'
        f'drawingml/2006/picture">'
        f'<pic:pic><pic:nvPicPr>'
        f'<pic:cNvPr id="0" name="{name}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rel_id}"/>'
        f'<a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/>'
        f'<a:ext cx="{width}" cy="{height}"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        f'</pic:pic></a:graphicData></a:graphic>'
        f'</wp:inline></w:drawing>')


def put_marks(xml: str, marks: dict[str, bytes]) -> tuple[str, dict[str, bytes]]:
    """Подставить подпись и печать. Возвращает (xml, файлы для media).

    Нет картинки - подстановка просто исчезает, как пустое поле: пустая
    рамка на месте печати выглядит хуже, чем её отсутствие.
    """
    media: dict[str, bytes] = {}
    for field, (width, height) in MARK_FIELDS.items():
        marker = "{{ " + field + " }}"
        raw = marks.get(field)
        if not raw:
            # Нет картинки - подстановка исчезает: пустая рамка на месте
            # печати выглядит хуже, чем её отсутствие.
            xml = re.sub(r"\{\{\s*" + field + r"\s*\}\}", "", xml)
            continue
        rel_id = f"rIdMark{field}"
        media[f"media/{field}.png"] = raw
        picture = picture_xml(rel_id, field, width, height)
        # Через re.sub с готовой строкой, а не с заменой: в картинке есть
        # обратные слэши и группы, которые re истолковал бы по-своему.
        xml = re.sub(r"\{\{\s*" + field + r"\s*\}\}", lambda _m, pic=picture: pic,
                     xml)
        del marker
    return xml, media


def add_rels(rels_xml: str, media: dict[str, bytes]) -> str:
    """Связи документа с картинками. Без них Word покажет красный крест."""
    if not media:
        return rels_xml
    extra = "".join(
        f'<Relationship Id="rIdMark{Path(name).stem}" '
        f'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        f'relationships/image" Target="{name}"/>'
        for name in media)
    return rels_xml.replace("</Relationships>", extra + "</Relationships>")


def add_png_type(types_xml: str) -> str:
    """Тип png в [Content_Types].xml - иначе docx не откроется вовсе."""
    if 'Extension="png"' in types_xml:
        return types_xml
    return types_xml.replace(
        "</Types>",
        '<Default Extension="png" ContentType="image/png"/></Types>')


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


def build(template_path: Path, ctx: dict[str, Any],
          marks: dict[str, bytes] | None = None) -> tuple[bytes, str]:
    """Готовый договор: (docx, отпечаток).

    Все части исходного файла, кроме word/document.xml (и связей, когда
    вставляется картинка), копируются байт в байт - стили, шрифты,
    колонтитулы и нумерация остаются ровно теми, какими их сохранил
    юрист.

    Подпись и печать вставляются после подсчёта отпечатка: отпечаток
    считается по тексту документа, и картинка его не меняет - иначе
    проверить уже подписанный договор было бы нечем.
    """
    template = load_template(template_path)
    filled, digest = render_xml(template, ctx)
    filled, media = put_marks(filled, marks or {})

    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(template)) as src, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        names = set(src.namelist())
        for item in src.infolist():
            if item.filename == DOCUMENT_XML:
                dst.writestr(item, filled)
            elif media and item.filename == RELS_XML:
                dst.writestr(item, add_rels(
                    src.read(item.filename).decode("utf-8"), media))
            elif media and item.filename == CONTENT_TYPES:
                dst.writestr(item, add_png_type(
                    src.read(item.filename).decode("utf-8")))
            else:
                dst.writestr(item, src.read(item.filename))
        for name, raw in media.items():
            full = f"word/{name}"
            if full not in names:
                dst.writestr(full, raw)
    return out.getvalue(), digest
