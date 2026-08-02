"""Чистая логика без внешних зависимостей.

Здесь живут все решения, которые можно проверить без Telegram и без базы:
валидация, экранирование, сверка ФИО с документом, разбор ответа OCR.
Модуль намеренно не импортирует aiogram и asyncpg - тесты гоняются
на голом stdlib, без установки окружения.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable

# ─────────────────────────── состояния FSM ───────────────────────────

NEW = "new"
WAIT_FIO = "wait_fio"
# Один экран согласия вместо двух: согласие на обработку ПДн включено в оферту,
# отдельного документа политики нет. Состояние wait_pdn убрано, но колонки
# pdn_version/pdn_consent_at в базе сохранены - момент и редакция согласия
# фиксируются по-прежнему, иначе доказать его нечем.
WAIT_OFERTA = "wait_oferta"
WAIT_CONTACT = "wait_contact"
WAIT_DOC = "wait_doc"
WAIT_SELFIE = "wait_selfie"
CONFIRM = "confirm"
PENDING = "pending"
APPROVED = "approved"

# статусы заявки
ST_NEW, ST_PENDING, ST_APPROVED, ST_REJECTED = "new", "pending", "approved", "rejected"

SUBSCRIBED_STATUSES = frozenset({"creator", "administrator", "member"})

# Все состояния, у которых есть обработчик. Нужен, чтобы значение, оставшееся
# в базе от прошлой версии бота, не превращалось в тупик: у такого состояния
# не сработает ни один StateIs, и человек будет получать ответы из меню
# посреди регистрации.
KNOWN_STATES = frozenset({
    NEW, WAIT_FIO, WAIT_OFERTA, WAIT_CONTACT,
    WAIT_DOC, WAIT_SELFIE, CONFIRM, PENDING, APPROVED,
})


def is_known_state(state: str | None) -> bool:
    return state in KNOWN_STATES


# ─────────────────────────── экранирование ───────────────────────────

def esc(value: Any) -> str:
    """Экранирование для parse_mode=HTML.

    ФИО попадает и в сообщение пользователю, и в карточку модератора.
    Подделанная ссылка в чате модерации - это атака на того, кто жмёт
    «Одобрить», поэтому экранируем даже то, что уже отфильтровано на входе.
    """
    return (
        str("" if value is None else value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ─────────────────────────── валидация ввода ───────────────────────────

@dataclass(frozen=True)
class Validation:
    ok: bool
    value: str = ""
    error: str = ""


def validate_fio(raw: str | None) -> Validation:
    """Проверка ФИО.

    Правильность написания здесь не проверяется - сверка идёт с документом
    на этапе OCR и модерации. Отсекаем только явный мусор и разметку.
    """
    fio = re.sub(r"\s+", " ", (raw or "").strip())
    if not 5 <= len(fio) <= 120:
        return Validation(False, error="Похоже на опечатку. Введите ФИО полностью.")
    if any(ch.isdigit() for ch in fio):
        return Validation(False, error="Похоже на опечатку. Введите ФИО полностью, без цифр.")
    if re.search(r"[<>&]", fio):
        return Validation(False, error="В ФИО недопустимы символы < > и &.")
    return Validation(True, value=fio)


# Telegram отдаёт сжатые фото уже в JPEG; документом может прийти что угодно.
ALLOWED_UPLOAD_MIME = frozenset({
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/heif",
})
MAX_UPLOAD_BYTES = 12 * 1024 * 1024


def validate_upload(is_photo: bool, mime: str | None, size: int | None) -> Validation:
    """Проверка присланного файла.

    Без неё документом проходил любой файл: PDF или архив сохранялся с
    расширением .jpg, уходил в OCR, а send_photo с его file_id падал с 400 -
    карточка модерации не приходила, и заявка терялась молча.
    """
    if size is not None and size > MAX_UPLOAD_BYTES:
        return Validation(False, error="Файл слишком большой. Пришлите фото до 12 МБ.")
    if is_photo:
        return Validation(True)
    if (mime or "").strip().lower() not in ALLOWED_UPLOAD_MIME:
        return Validation(
            False,
            error="Нужно изображение (JPG, PNG или HEIC). Пришлите фото, а не файл другого типа.",
        )
    return Validation(True)


def should_process(chat_type: str | None, *, from_admin_chat: bool,
                   is_moderation_callback: bool) -> bool:
    """Пускать ли апдейт дальше.

    Личные чаты - да: там идёт вся регистрация. Групповые - только если это
    нажатие кнопки модерации в служебном чате. Раньше здесь стояло голое
    `chat_type != "private" -> отбросить`, из-за чего кнопки «Одобрить»
    в группе модерации не доходили до обработчика вообще, и заявки навсегда
    оставались в pending.
    """
    if chat_type == "private":
        return True
    return from_admin_chat and is_moderation_callback


def is_subscribed(status: str | None, is_member: bool | None = None) -> bool:
    """Гейт подписки без кэша.

    Кэш «проверяли N минут назад» здесь сознательно отсутствует: он
    перекрывает реальный статус и превращается в постоянный обход гейта,
    потому что отметка обновляется при каждом действии. getChatMember дёшев.
    """
    if status in SUBSCRIBED_STATUSES:
        return True
    return status == "restricted" and bool(is_member)


def contact_belongs_to_sender(contact_user_id: int | None, sender_id: int) -> bool:
    """Telegram позволяет переслать ЧУЖОЙ контакт из адресной книги."""
    return contact_user_id is not None and contact_user_id == sender_id


def parse_moderation_callback(data: str | None) -> tuple[str, int] | None:
    """Разбор callback_data вида approve:<tg_id>.

    Некорректный id обязан отсекаться здесь: иначе UPDATE уходит вхолостую,
    а модератор видит «Одобрено» и считает заявку закрытой.
    """
    if not data:
        return None
    m = re.fullmatch(r"(approve|reject):(-?\d+)", data)
    if not m:
        return None
    target = int(m.group(2))
    if target <= 0:
        return None
    return m.group(1), target


# ─────────────────────────── сверка ФИО с документом ───────────────────────────

MIN_TOKEN = 3          # «оглы», предлоги и инициалы в сравнении не участвуют
NAME_MATCH_MIN = 0.67  # 2 совпавших токена из 3


def normalize_name(value: str | None) -> str:
    return re.sub(
        r"\s+", " ",
        re.sub(r"[^А-ЯA-Z]+", " ", str(value or "").upper().replace("Ё", "Е")),
    ).strip()


@dataclass(frozen=True)
class OcrResult:
    entities: dict[str, str] = field(default_factory=dict)
    matched: tuple[str, ...] = ()
    tokens_total: int = 0
    score: float = 0.0
    recognized: bool = False

    @property
    def mismatch(self) -> bool:
        return not self.recognized or self.score < NAME_MATCH_MIN


def extract_yandex_vision(payload: Any) -> tuple[list[str], dict[str, str]]:
    """Единственное вендор-зависимое место.

    Ответ Yandex Vision: result.textAnnotation.{blocks[].lines[].text, entities[]}.
    Для SmartEngines / Cloud.ru / VK Cloud / PaddleOCR заменяется только эта
    функция - остальному коду нужны лишь строки и словарь полей.
    """
    if not isinstance(payload, dict):
        return [], {}
    ta = (payload.get("result") or {}).get("textAnnotation") or {}
    lines: list[str] = []
    for block in ta.get("blocks") or []:
        for line in (block or {}).get("lines") or []:
            text = (line or {}).get("text")
            if text:
                lines.append(str(text))
    entities: dict[str, str] = {}
    for ent in ta.get("entities") or []:
        name = (ent or {}).get("name")
        if name:
            entities[str(name)] = str((ent or {}).get("text") or "")
    return lines, entities


MAX_SUFFIX_DIFF = 2


def token_matches(token: str, words: Iterable[str]) -> bool:
    """Совпадение по слову целиком, а не по подстроке.

    Подстрочное сравнение засчитывало бы «ПЕТР» внутри «ПЕТРОВНА» - то есть
    мужское имя подтверждалось бы женским отчеством. Допуск на хвост оставлен
    для склонений и обрезки распознавания: «ИВАН» ↔ «ИВАНОВ» проходит,
    «ПЕТР» ↔ «ПЕТРОВНА» уже нет.
    """
    for word in words:
        if word == token:
            return True
        if word.startswith(token) and len(word) - len(token) <= MAX_SUFFIX_DIFF:
            return True
    return False


def match_name(full_name: str | None, payload: Any) -> OcrResult:
    """Насколько введённое ФИО совпало с тем, что видно в документе.

    Сырой текст документа наружу не отдаётся: только структурные поля и доля
    совпадения. Меньше ПДн в хранении при том же контроле.
    """
    lines, entities = extract_yandex_vision(payload)
    recognized = bool(lines or entities)
    words = [w for w in normalize_name(" ".join([*lines, *entities.values()])).split(" ") if w]
    tokens = [t for t in normalize_name(full_name).split(" ") if len(t) >= MIN_TOKEN]
    matched = tuple(t for t in tokens if token_matches(t, words))
    score = round(len(matched) / len(tokens), 2) if tokens else 0.0
    return OcrResult(
        entities=entities,
        matched=matched,
        tokens_total=len(tokens),
        score=score,
        recognized=recognized,
    )


# ─────────────────────────── прочее ───────────────────────────

STORE_FILE_NAME = re.compile(r"^\d+-(doc|selfie)-\d+\.jpg$")


def is_safe_store_path(path: str | None, storage_dir: Any) -> bool:
    """Путь приходит из своей же базы, но перед удалением всё равно сверяется
    с шаблоном: одна опечатка в запросе - и удаление уедет не туда.

    Каталог передаётся аргументом, а не зашит в регулярку. Раньше здесь было
    жёстко `/files/kyc`, хотя STORAGE_DIR настраивается: при любом другом
    значении ретеншен молча переставал удалять сканы - раз в шесть часов
    писал «подозрительный путь» в лог, а паспорта оставались на диске
    навсегда. Симптома, заметного снаружи, у этого не было.
    """
    if not path:
        return False
    p = PurePosixPath(str(path).replace("\\", "/"))
    base = PurePosixPath(str(storage_dir).replace("\\", "/"))
    return p.parent == base and bool(STORE_FILE_NAME.fullmatch(p.name))


def rate_limit_verdict(count: int, soft: int = 20, hard: int = 25) -> str:
    """'ok' | 'warn' | 'drop'. Выше жёсткого порога отвечать нельзя:
    ответ на флуд сам становится флудом."""
    if count > hard:
        return "drop"
    if count > soft:
        return "warn"
    return "ok"


def chunked(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    buf: list[Any] = []
    for item in items:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf
