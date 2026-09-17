"""Какой шаблон документа брать: наш или загруженный владельцем.

У каждого вида документа всегда включён ровно один шаблон. По умолчанию
наш - тот, что лежит в образе. Владелец загружает свой, включает его, и
с этого момента документы собираются по нему; наш остаётся на месте и
возвращается одним снятием галочки.

Файл лежит на общем томе (`doctemplates`): панель его пишет, процесс
бота читает. В базе - только имя, размер и отпечаток: держать docx в
базе значит возить его в каждом дампе.

Сбой чтения своего шаблона не должен оставить выдачу без документа,
поэтому `resolve` при любой беде возвращает наш: сломанный шаблон - это
повод показать ошибку в панели, а не отказать клиенту в договоре.
"""

from __future__ import annotations

import hashlib
import io
import logging
import time
import zipfile
from pathlib import Path
from typing import Any

from ..services.contract import TemplateProblem, load_template
from . import logic

log = logging.getLogger(__name__)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def check_upload(raw: bytes, filename: str) -> None:
    """Файл годится в шаблоны? Иначе - понятная человеку причина.

    Проверяем до сохранения: шаблон, который не открывается, обнаружится
    в момент выдачи, когда клиенту уже сказали «оформляем».
    """
    if Path(filename).suffix.lower() != logic.DOC_SUFFIX:
        raise TemplateProblem("Шаблон должен быть файлом .docx: подстановки "
                              "заполняются в исходном файле юриста, и pdf "
                              "так не заполнить.")
    if len(raw) > logic.DOC_MAX_BYTES:
        raise TemplateProblem(
            f"Шаблон больше {logic.DOC_MAX_BYTES // (1024 * 1024)} МБ.")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8")
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError) as exc:
        raise TemplateProblem("Это не docx-файл: Word его не открывал.") from exc
    if "{{" not in xml:
        raise TemplateProblem(
            "В шаблоне нет ни одной подстановки вида {{ поле }} — "
            "заполнять в нём нечего.")


async def stored(crm: Any, kind: str) -> dict | None:
    """Включённый свой шаблон вида. None - работает наш."""
    try:
        return await crm.active_doc_template(kind)
    except Exception:                                    # noqa: BLE001
        log.exception("свои шаблоны не прочитаны, берём наш")
        return None


async def resolve(crm: Any, kind: str, ours: Path, *,
                  folder: Path | None = None) -> Path:
    """Путь к шаблону: свой, если включён и читается, иначе наш."""
    if kind in logic.DOC_CODE_ONLY:
        return ours
    row = await stored(crm, kind)
    if row is None or folder is None:
        return ours
    path = Path(folder) / str(row.get("filename") or "")
    try:
        load_template(path)
    except TemplateProblem:
        log.exception("свой шаблон %s не читается, берём наш", kind)
        return ours
    return path


async def marks(crm: Any, folder: Path | None) -> dict[str, bytes]:
    """Подпись и печать организации. Нет файла - нет и подстановки."""
    if folder is None:
        return {}
    out: dict[str, bytes] = {}
    try:
        rows = await crm.company_marks()
    except Exception:                                    # noqa: BLE001
        log.exception("подпись и печать не прочитаны")
        return {}
    for row in rows:
        path = Path(folder) / str(row.get("filename") or "")
        try:
            out[str(row["kind"])] = path.read_bytes()
        except OSError:
            log.warning("файл %s не читается", path)
    return out


# Снимок для процесса бота - той же схемой, что и реквизиты: панель
# пишет, бот читает раз в несколько минут. Ходить в базу за шаблоном на
# каждый апдейт незачем: шаблоны меняют раз в год.
TTL_SECONDS = 300

_snapshot: dict[str, dict] = {}
_marks: dict[str, bytes] = {}
_loaded_at = 0.0


def snapshot() -> dict[str, dict]:
    """Последние известные свои шаблоны: вид → строка базы."""
    return dict(_snapshot)


def mark_snapshot() -> dict[str, bytes]:
    return dict(_marks)


def set_snapshot(rows: Any = None, marks: dict[str, bytes] | None = None) -> None:
    """Подменить снимок - для тестов и для первого чтения на старте."""
    global _snapshot, _marks, _loaded_at
    _snapshot = {str(r["kind"]): dict(r) for r in (rows or [])
                 if r.get("active")}
    _marks = dict(marks or {})
    _loaded_at = time.monotonic()


def reset() -> None:
    global _snapshot, _marks, _loaded_at
    _snapshot, _marks, _loaded_at = {}, {}, 0.0


def is_fresh(*, now: float | None = None) -> bool:
    return (now or time.monotonic()) - _loaded_at < TTL_SECONDS


async def refresh(crm: Any, folder: Path | None, *, force: bool = False) -> None:
    """Перечитать шаблоны и печати, если снимок устарел.

    Ошибка базы снимок не роняет: документы важнее свежести, и прошлый
    шаблон лучше отсутствия шаблона.
    """
    if crm is None or (not force and is_fresh()):
        return
    try:
        set_snapshot(await crm.doc_templates(), await marks(crm, folder))
    except Exception:                                    # noqa: BLE001
        log.exception("свои шаблоны не перечитаны")


def path_for(kind: str, ours: Path, folder: Path | None = None) -> Path:
    """Путь к шаблону по снимку: свой, если включён и читается, иначе наш.

    Синхронная - её зовут из сборки документа, а там базы уже нет.
    """
    if kind in logic.DOC_CODE_ONLY or folder is None:
        return ours
    row = _snapshot.get(kind)
    if not row:
        return ours
    path = Path(folder) / str(row.get("filename") or "")
    try:
        load_template(path)
    except TemplateProblem:
        log.warning("свой шаблон %s не читается, берём наш", kind)
        return ours
    return path
