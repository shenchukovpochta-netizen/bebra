"""Формирование договора проката в PDF.

Шаблон - обычный текстовый файл с подстановками `{{ поле }}`. Так сделано
намеренно: текст договора правят владелец проката и юрист, и правка не должна
требовать ни пересборки образа, ни разметки. Файл монтируется в контейнер,
читается при каждом формировании - достаточно поправить и перезапустить.

Отпечаток документа. В договор печатается SHA-256, посчитанный по тексту,
в котором строка самого отпечатка ещё не подставлена (плейсхолдер заменён
пустой строкой). Такое правило позволяет проверить документ задним числом:
взять текст, стереть значение отпечатка, посчитать хэш заново. Печатать хэш
файла PDF было бы бессмысленно - он зависит от даты сборки и версии
библиотеки, и два одинаковых договора дали бы разные значения.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from fpdf import FPDF

log = logging.getLogger(__name__)

PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
# Одна решётка - комментарий, две - заголовок раздела. Без отрицательного
# просмотра вперёд комментарий съедал и заголовки: договор собирался вообще
# без «1. Стороны», «2. Предмет договора» и остальных названий разделов.
COMMENT_LINE = re.compile(r"^\s*#(?!#)")
# Длинный пробельный отбив в шаблоне - это две колонки строки: слева город,
# справа дата. Отдать такую строку в выравнивание по ширине нельзя: fpdf2
# пытается растянуть пробельный прогон и падает с «not enough horizontal
# space», а договор не формируется вообще.
COLUMNS = re.compile(r"\s{3,}")

FONT_NAME = "DejaVu"
# Шрифт с кириллицей обязателен: встроенные в PDF шрифты fpdf2 - latin-1,
# и весь договор вышел бы вопросительными знаками.
FONT_REGULAR = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FONT_BOLD = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

HASH_FIELD = "contract_sha256"


class TemplateProblem(Exception):
    """Шаблон недоступен или пуст - договор формировать не из чего."""


def load_template(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TemplateProblem(f"не читается шаблон договора {path}: {exc}") from exc
    if not text.strip():
        raise TemplateProblem(f"шаблон договора {path} пуст")
    return text


def strip_comments(template: str) -> str:
    """Строки, начинающиеся с решётки, в договор не попадают."""
    return "\n".join(line for line in template.splitlines()
                     if not COMMENT_LINE.match(line))


def substitute(text: str, ctx: dict[str, Any]) -> str:
    """Подстановка `{{ поле }}`.

    Неизвестное поле остаётся в документе видимой пометкой, а не пустотой:
    опечатка в шаблоне должна бросаться в глаза тому, кто первый раз откроет
    договор, а не тихо выкидывать из него реквизит.
    """
    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in ctx:
            log.warning("в шаблоне договора неизвестное поле %r", key)
            return f"«нет поля {key}»"
        return str(ctx[key])

    return PLACEHOLDER.sub(replace, text)


def render_text(template: str, ctx: dict[str, Any]) -> tuple[str, str]:
    """Текст договора и его отпечаток.

    Возвращает (текст с подставленным отпечатком, сам отпечаток).
    """
    body = strip_comments(template)
    without_hash = substitute(body, {**ctx, HASH_FIELD: ""})
    digest = hashlib.sha256(without_hash.encode("utf-8")).hexdigest()
    return substitute(body, {**ctx, HASH_FIELD: digest}), digest


class _Document(FPDF):
    def header(self) -> None:                              # pragma: no cover
        pass

    def footer(self) -> None:
        self.set_y(-15)
        self.set_font(FONT_NAME, size=8)
        self.set_text_color(120)
        self.cell(0, 8, f"стр. {self.page_no()} из {{nb}}", align="C")
        self.set_text_color(0)


def to_pdf(text: str) -> bytes:
    """Сборка PDF из размеченного текста.

    Разметка минимальная и намеренно: «## » - заголовок раздела, «- » - пункт
    списка, пустая строка - разрыв абзаца. Полноценный HTML здесь был бы
    лишней степенью свободы в документе, который должен выглядеть одинаково
    всегда.
    """
    if not FONT_REGULAR.exists():
        raise TemplateProblem(
            f"нет шрифта {FONT_REGULAR}: поставьте пакет fonts-dejavu-core, "
            f"иначе кириллица в договоре не отрисуется"
        )

    pdf = _Document(format="A4", unit="mm")
    pdf.set_margins(20, 18, 20)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_font(FONT_NAME, "", str(FONT_REGULAR))
    pdf.add_font(FONT_NAME, "B", str(FONT_BOLD if FONT_BOLD.exists() else FONT_REGULAR))
    pdf.add_page()

    def block(height: float, body: str, **kwargs: Any) -> None:
        """multi_cell, всегда возвращающий курсор на левое поле.

        По умолчанию fpdf2 оставляет курсор справа от блока, и следующий
        абзац с шириной 0 получает нулевую ширину - вместо договора
        «not enough horizontal space to render a single character».
        """
        pdf.multi_cell(0, height, body, new_x="LMARGIN", new_y="NEXT", **kwargs)

    title_done = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            pdf.ln(3)
            continue
        if not title_done:
            pdf.set_font(FONT_NAME, "B", 13)
            block(7, stripped, align="C")
            pdf.ln(4)
            title_done = True
            continue
        if stripped.startswith("## "):
            pdf.ln(2)
            pdf.set_font(FONT_NAME, "B", 11)
            block(6, stripped[3:].strip())
            pdf.ln(1)
            continue
        if stripped.startswith("- "):
            pdf.set_font(FONT_NAME, size=10)
            pdf.set_x(pdf.l_margin + 5)
            block(5.5, "• " + COLUMNS.sub(" ", stripped[2:].strip()))
            continue

        pdf.set_font(FONT_NAME, size=10)
        columns = COLUMNS.split(stripped)
        if len(columns) == 2 and all(columns):
            half = (pdf.w - pdf.l_margin - pdf.r_margin) / 2
            pdf.cell(half, 5.5, columns[0], align="L")
            pdf.cell(half, 5.5, columns[1], align="R", new_x="LMARGIN", new_y="NEXT")
            continue
        block(5.5, COLUMNS.sub(" ", stripped), align="J")

    return bytes(pdf.output())


def build(template_path: Path, ctx: dict[str, Any]) -> tuple[bytes, str]:
    """Готовый договор: (PDF, отпечаток текста)."""
    text, digest = render_text(load_template(template_path), ctx)
    return to_pdf(text), digest
