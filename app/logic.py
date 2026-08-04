"""Чистая логика без внешних зависимостей.

Здесь живут все решения, которые можно проверить без Telegram и без базы:
валидация, экранирование, порядок шагов анкеты, реквизиты договора.
Модуль намеренно не импортирует aiogram и asyncpg - тесты гоняются
на голом stdlib, без установки окружения.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable

# ─────────────────────────── состояния FSM ───────────────────────────

NEW = "new"
WAIT_FIO = "wait_fio"
# Один экран согласия вместо двух: согласие на обработку ПДн включено в оферту,
# отдельного документа политики нет. Состояние wait_pdn убрано, но колонки
# pdn_version/pdn_consent_at в базе сохранены - момент и редакция согласия
# фиксируются по-прежнему, иначе доказать его нечем.
WAIT_OFERTA = "wait_oferta"
WAIT_CONTACT = "wait_contact"

# Анкета для договора. Каждое поле - отдельный шаг: разбирать «кем выдан,
# когда и код подразделения» из одного сообщения нечем, а ошибка в реквизитах
# договора обнаруживается только при споре, когда исправлять уже поздно.
WAIT_BIRTH = "wait_birth"
WAIT_BIRTH_PLACE = "wait_birth_place"
WAIT_PASSPORT = "wait_passport"
WAIT_PASSPORT_DATE = "wait_passport_date"
WAIT_PASSPORT_CODE = "wait_passport_code"
WAIT_PASSPORT_ISSUER = "wait_passport_issuer"
WAIT_REG_ADDR = "wait_reg_addr"
WAIT_LIVE_ADDR = "wait_live_addr"
WAIT_PHONE2 = "wait_phone2"
WAIT_PHONE3 = "wait_phone3"

WAIT_DOC = "wait_doc"
# Фото письменного согласия законного представителя. Шаг только для тех,
# кому от 16 до 18: у совершеннолетнего его в потоке нет вовсе.
WAIT_PARENT_CONSENT = "wait_parent_consent"
CONFIRM = "confirm"
PENDING = "pending"
# Договор сформирован и одобрен, ждём нажатия «Подписываю». Отдельное
# состояние, а не сразу approved: до подписи договора нет, и выдавать
# по нему велосипед нельзя.
WAIT_SIGN = "wait_sign"
APPROVED = "approved"

# статусы заявки
ST_NEW, ST_PENDING, ST_APPROVED, ST_REJECTED = "new", "pending", "approved", "rejected"
# Отдельно от status: договор живёт своим циклом. Заявка может быть одобрена,
# а договор - ещё не подписан.
CT_NONE, CT_ISSUED, CT_SIGNED = "none", "issued", "signed"

SUBSCRIBED_STATUSES = frozenset({"creator", "administrator", "member"})

# Все состояния, у которых есть обработчик. Нужен, чтобы значение, оставшееся
# в базе от прошлой версии бота, не превращалось в тупик: у такого состояния
# не сработает ни один StateIs, и человек будет получать ответы из меню
# посреди регистрации.
KNOWN_STATES = frozenset({
    NEW, WAIT_FIO, WAIT_OFERTA, WAIT_CONTACT,
    WAIT_BIRTH, WAIT_BIRTH_PLACE, WAIT_PASSPORT, WAIT_PASSPORT_DATE,
    WAIT_PASSPORT_CODE, WAIT_PASSPORT_ISSUER, WAIT_REG_ADDR, WAIT_LIVE_ADDR,
    WAIT_PHONE2, WAIT_PHONE3,
    WAIT_DOC, WAIT_PARENT_CONSENT, CONFIRM, PENDING, WAIT_SIGN, APPROVED,
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

    Правильность написания здесь не проверяется - ФИО сверяет с документом
    модератор по фотографии. Отсекаем только явный мусор и разметку.
    """
    fio = re.sub(r"\s+", " ", (raw or "").strip())
    if not 5 <= len(fio) <= 120:
        return Validation(False, error="Похоже на опечатку. Введите ФИО полностью.")
    if any(ch.isdigit() for ch in fio):
        return Validation(False, error="Похоже на опечатку. Введите ФИО полностью, без цифр.")
    if re.search(r"[<>&]", fio):
        return Validation(False, error="В ФИО недопустимы символы < > и &.")
    return Validation(True, value=fio)


# ─────────────────────── анкета для договора ───────────────────────
#
# Всё, что уходит в реквизиты договора, проверяется здесь и приводится
# к одному виду. Договор печатается из этих значений буквально: «12 34 567890»
# и «1234567890» в разных договорах - это уже разночтение в документе,
# которое всплывёт при разборе спора.

# Прокат с 16 лет: арендатор 16-17 лет заключает договор с письменного
# согласия законного представителя (фото согласия - отдельный шаг анкеты).
# С 18 - без согласия. MIN_AGE ниже 16 не опускать: с 14 до 16 сделки такого
# размера подросток совершать не вправе даже с согласия (ст. 26 ГК РФ
# разрешает ему только мелкие бытовые), а младше 14 - ничтожны.
MIN_AGE, ADULT_AGE, MAX_AGE = 16, 18, 100
PASSPORT_MIN_AGE = 14      # паспорт РФ выдаётся с 14 лет


def _clean(raw: str | None) -> str:
    """Схлопывание пробелов и обрезка. Пользователи копируют данные из заметок
    и приносят переносы строк и двойные пробелы внутри адреса."""
    return re.sub(r"\s+", " ", (raw or "").strip())


def _no_markup(value: str) -> bool:
    return not re.search(r"[<>&]", value)


def validate_date(raw: str | None, *, today: date | None = None,
                  min_year: int = 1900) -> Validation:
    """Дата в виде ДД.ММ.ГГГГ.

    Точки, слэши и дефисы как разделители принимаются одинаково: набирают
    по-разному, а отказ «неверный формат» на верно введённой дате - самый
    частый способ потерять человека посреди анкеты.
    """
    text = _clean(raw)
    m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", text)
    if not m:
        return Validation(False, error="Дата нужна в виде ДД.ММ.ГГГГ, например 07.03.1990.")
    day, month, year = (int(g) for g in m.groups())
    try:
        value = date(year, month, day)
    except ValueError:
        return Validation(False, error="Такой даты не существует. Проверьте число и месяц.")
    if value.year < min_year:
        return Validation(False, error=f"Год должен быть не раньше {min_year}.")
    if value > (today or date.today()):
        return Validation(False, error="Дата не может быть в будущем.")
    return Validation(True, value=value.strftime("%d.%m.%Y"))


def parse_date(value: str | None) -> date | None:
    """Обратный разбор уже нормализованной даты. None, если её ещё нет."""
    try:
        return datetime.strptime(_clean(value), "%d.%m.%Y").date()
    except (ValueError, TypeError):
        return None


def age_years(born: date, on: date) -> int:
    """Полных лет на дату. Вычитание годов с поправкой на «день рождения ещё
    не наступил» - иначе возраст завышается на единицу почти полгода."""
    return on.year - born.year - ((on.month, on.day) < (born.month, born.day))


def validate_birth_date(raw: str | None, *, today: date | None = None) -> Validation:
    """Дата рождения плюс проверка возраста.

    Возраст считается здесь, а не в момент выдачи велосипеда: договор
    с тем, кому нет 16, ничтожен целиком, и узнать об этом на выдаче -
    значит уже собрать паспортные данные ребёнка и завести на него договор.
    16-17 лет - не отказ: дальше у таких появится шаг с фото согласия
    законного представителя.
    """
    result = validate_date(raw, today=today, min_year=1900)
    if not result.ok:
        return result
    years = age_years(parse_date(result.value), today or date.today())
    if years < MIN_AGE:
        return Validation(
            False,
            error=f"Прокат доступен с {MIN_AGE} лет (до {ADULT_AGE} - "
                  f"с письменного согласия родителя).")
    if years > MAX_AGE:
        return Validation(False, error="Похоже на опечатку в годе рождения. Проверьте, пожалуйста.")
    return Validation(True, value=result.value)


def is_minor(anketa: dict | None, *, today: date | None = None) -> bool:
    """Несовершеннолетний арендатор: 16-17 лет, нужен шаг согласия родителя.

    Нечитаемая или отсутствующая дата рождения - НЕ несовершеннолетний:
    до этой проверки дата уже прошла валидатор, а лишний шаг согласия
    у взрослого из-за сбоя разбора - это отказ в регистрации на ровном месте.
    """
    born = parse_date((anketa or {}).get("birth_date"))
    if born is None:
        return False
    return age_years(born, today or date.today()) < ADULT_AGE


def state_after_doc(anketa: dict | None, *, has_parent_consent: bool,
                    today: date | None = None) -> str:
    """Куда идти после фото документа: взрослым - на подтверждение, 16-17-летним -
    за фото согласия родителя, если оно ещё не загружено."""
    if is_minor(anketa, today=today) and not has_parent_consent:
        return WAIT_PARENT_CONSENT
    return CONFIRM


def validate_passport_number(raw: str | None) -> Validation:
    """Серия и номер: 4 + 6 цифр, приводятся к «1234 567890»."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) != 10:
        return Validation(
            False,
            error="Серия и номер - это 10 цифр, например 1234 567890. "
                  "Проверьте, сколько получилось.",
        )
    return Validation(True, value=f"{digits[:4]} {digits[4:]}")


def validate_passport_code(raw: str | None) -> Validation:
    """Код подразделения: 6 цифр, приводится к «123-456»."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) != 6:
        return Validation(False, error="Код подразделения - 6 цифр, например 160-002.")
    return Validation(True, value=f"{digits[:3]}-{digits[3:]}")


def validate_passport_issuer(raw: str | None) -> Validation:
    text = _clean(raw)
    if not 5 <= len(text) <= 200:
        return Validation(
            False, error="Впишите, кем выдан паспорт, как в документе - строкой целиком.")
    if not _no_markup(text):
        return Validation(False, error="Недопустимы символы < > и &.")
    return Validation(True, value=text)


def validate_birth_place(raw: str | None) -> Validation:
    text = _clean(raw)
    if not 3 <= len(text) <= 150:
        return Validation(False, error="Укажите место рождения, как в паспорте.")
    if not _no_markup(text):
        return Validation(False, error="Недопустимы символы < > и &.")
    return Validation(True, value=text)


def validate_address(raw: str | None) -> Validation:
    """Адрес.

    Требование цифры в строке - не придирка к формату: адрес без номера дома
    («г. Казань, ул. Баумана») в договоре не идентифицирует никого, а это
    единственное место, по которому арендатора ищут, если велосипед не вернули.
    """
    text = _clean(raw)
    if not 10 <= len(text) <= 250:
        return Validation(
            False,
            error="Адрес нужен полностью: город, улица, дом, квартира. "
                  "Индекс по желанию.",
        )
    if not _no_markup(text):
        return Validation(False, error="В адресе недопустимы символы < > и &.")
    if not any(ch.isdigit() for ch in text):
        return Validation(False, error="В адресе не хватает номера дома.")
    return Validation(True, value=text)


def normalize_phone(raw: str | None) -> str | None:
    """Приведение к +7XXXXXXXXXX. None, если это не похоже на номер.

    Telegram отдаёт номер из контакта без плюса и иногда с восьмёркой,
    а руками пишут вообще как придётся. В договоре все три номера должны
    выглядеть одинаково.
    """
    text = (raw or "").strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    # Иностранные номера пропускаем как есть: гость из-за границы тоже клиент.
    if text.startswith("+") and 11 <= len(digits) <= 15:
        return "+" + digits
    return None


def validate_phone(raw: str | None, *, taken: Iterable[str] = ()) -> Validation:
    """Дополнительный телефон.

    Совпадение с уже введёнными отсекается: три одинаковых номера в договоре -
    это один номер, а смысл трёх контактов ровно в том, чтобы дозвониться,
    когда первый недоступен.
    """
    value = normalize_phone(raw)
    if value is None:
        return Validation(
            False, error="Не похоже на номер телефона. Пример: +7 900 123-45-67.")
    if value in set(taken):
        return Validation(False, error="Этот номер уже указан. Нужен другой.")
    return Validation(True, value=value)


@dataclass(frozen=True)
class Step:
    """Шаг анкеты: состояние, поле в анкете и его проверка.

    Таблица вместо десяти почти одинаковых обработчиков - иначе порядок шагов
    оказывается размазан по файлу и при вставке нового поля в середину
    какой-нибудь переход неизбежно забывается.
    """
    state: str
    field: str
    validate: Callable[..., Validation]


ANKETA_STEPS: tuple[Step, ...] = (
    Step(WAIT_BIRTH, "birth_date", validate_birth_date),
    Step(WAIT_BIRTH_PLACE, "birth_place", validate_birth_place),
    Step(WAIT_PASSPORT, "passport_number", validate_passport_number),
    Step(WAIT_PASSPORT_DATE, "passport_date", validate_date),
    Step(WAIT_PASSPORT_CODE, "passport_code", validate_passport_code),
    Step(WAIT_PASSPORT_ISSUER, "passport_issuer", validate_passport_issuer),
    Step(WAIT_REG_ADDR, "reg_address", validate_address),
    Step(WAIT_LIVE_ADDR, "live_address", validate_address),
    Step(WAIT_PHONE2, "phone2", validate_phone),
    Step(WAIT_PHONE3, "phone3", validate_phone),
)

ANKETA_BY_STATE = {step.state: step for step in ANKETA_STEPS}
ANKETA_FIELDS = tuple(step.field for step in ANKETA_STEPS)

# Полный порядок шагов. next_state ходит по нему, поэтому переход появляется
# автоматически, стоит вписать шаг в таблицу выше.
# Шаг согласия родителя стоит в таблице, но проходят его только 16-17-летние:
# обработчик документа выбирает следующий шаг через state_after_doc, а не
# по этой таблице.
FLOW: tuple[str, ...] = (
    WAIT_FIO, WAIT_OFERTA, WAIT_CONTACT,
    *(step.state for step in ANKETA_STEPS),
    WAIT_DOC, WAIT_PARENT_CONSENT, CONFIRM,
)


def next_state(state: str | None) -> str | None:
    """Следующий шаг по порядку. None - дальше по таблице ничего нет."""
    try:
        index = FLOW.index(state)
    except ValueError:
        return None
    return FLOW[index + 1] if index + 1 < len(FLOW) else None


def anketa_complete(anketa: dict | None) -> bool:
    data = anketa or {}
    return all(str(data.get(f) or "").strip() for f in ANKETA_FIELDS)


def missing_anketa_fields(anketa: dict | None) -> tuple[str, ...]:
    data = anketa or {}
    return tuple(f for f in ANKETA_FIELDS if not str(data.get(f) or "").strip())


def passport_date_consistent(anketa: dict | None) -> bool:
    """Дата выдачи не раньше 14-летия и не раньше даты рождения.

    Ловит перепутанные местами даты: «выдан 07.03.1990, родился 12.04.2015»
    в договор уходит молча, а на бумаге это очевидная ерунда.
    """
    data = anketa or {}
    born = parse_date(data.get("birth_date"))
    issued = parse_date(data.get("passport_date"))
    if born is None or issued is None:
        return True                      # нечего сверять, проверит свой валидатор
    years = issued.year - born.year - ((issued.month, issued.day) < (born.month, born.day))
    return years >= PASSPORT_MIN_AGE


# Telegram отдаёт сжатые фото уже в JPEG; документом может прийти что угодно.
ALLOWED_UPLOAD_MIME = frozenset({
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/heif",
})
MAX_UPLOAD_BYTES = 12 * 1024 * 1024


def validate_upload(is_photo: bool, mime: str | None, size: int | None) -> Validation:
    """Проверка присланного файла.

    Без неё документом проходил любой файл: PDF или архив сохранялся с
    расширением .jpg, а send_photo с его file_id падал с 400 - карточка
    модерации не приходила, и заявка терялась молча.
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
                   is_moderation_callback: bool,
                   is_moderation_reply: bool = False) -> bool:
    """Пускать ли апдейт дальше.

    Личные чаты - да: там идёт вся регистрация. Групповые - только если это
    нажатие кнопки модерации в служебном чате. Раньше здесь стояло голое
    `chat_type != "private" -> отбросить`, из-за чего кнопки «Одобрить»
    в группе модерации не доходили до обработчика вообще, и заявки навсегда
    оставались в pending.

    Отдельно пропускается ответ на карточку: отказ «с указанием ошибок»
    модератор пишет реплаем, и без этой ветки его сообщение отбрасывалось бы
    здесь - кнопка «Свой текст» просила бы ответ, которого бот не увидит.
    """
    if chat_type == "private":
        return True
    return from_admin_chat and (is_moderation_callback or is_moderation_reply)


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


# ─────────────────────────── прочее ───────────────────────────

STORE_FILE_NAME = re.compile(r"^\d+-(?:doc-\d+\.jpg|parent-\d+\.jpg|contract-\d+\.pdf)$")


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


# ─────────────────────────── договор ───────────────────────────

CONTRACT_PREFIX = "АВ"


def contract_number(seq: int, *, today: date | None = None) -> str:
    """Номер договора вида АВ-2026-000042.

    Сквозная нумерация из последовательности в базе, а не «tg_id + дата»:
    номер попадает в бумажный документ и в журнал выдачи, и он обязан быть
    уникальным и не подсказывать, сколько у проката клиентов в Telegram.
    """
    return f"{CONTRACT_PREFIX}-{(today or date.today()).year}-{int(seq):06d}"


# Поля, которые подставляются в шаблон договора. Порядок задаёт и вид
# карточки модератора: читать её сверху вниз надо так же, как договор.
CONTRACT_LABELS: tuple[tuple[str, str], ...] = (
    ("fio", "ФИО"),
    ("birth_date", "Дата рождения"),
    ("birth_place", "Место рождения"),
    ("passport_number", "Паспорт"),
    ("passport_date", "Дата выдачи"),
    ("passport_code", "Код подразделения"),
    ("passport_issuer", "Кем выдан"),
    ("reg_address", "Адрес регистрации"),
    ("live_address", "Адрес проживания"),
    ("phone", "Телефон основной"),
    ("phone2", "Телефон дополнительный"),
    ("phone3", "Телефон третий"),
)


# Абзац для договора с 16-17-летним. Подставляется в {{ minor_clause }}:
# у взрослого на этом месте пустота, у несовершеннолетнего - оговорка,
# без которой сделка с ним не имеет письменного следа согласия (ст. 26 ГК РФ).
MINOR_CLAUSE = (
    "Арендатор, не достигший 18 лет, заключает настоящий Договор с письменного "
    "согласия своего законного представителя (родителя, усыновителя или "
    "попечителя). Изображение письменного согласия передано Арендодателю "
    "до заключения Договора и хранится у Арендодателя вместе с заявкой."
)


def contract_context(user: dict, anketa: dict | None, *, number: str,
                     today: date | None = None) -> dict[str, str]:
    """Значения для подстановки в шаблон договора.

    Всё приводится к строкам здесь, а не в шаблоне: пустое поле должно стать
    видимым прочерком, а не строкой «None» посреди договора.
    """
    data = dict(anketa or {})
    ctx = {
        "contract_number": number,
        "contract_date": (today or date.today()).strftime("%d.%m.%Y"),
        "fio": str(user.get("full_name") or ""),
        "phone": normalize_phone(user.get("phone")) or str(user.get("phone") or ""),
        "tg_id": str(user.get("tg_id") or ""),
        "username": f"@{user['username']}" if user.get("username") else "",
    }
    for field_name in ANKETA_FIELDS:
        ctx[field_name] = str(data.get(field_name) or "")
    result = {k: (v.strip() or "—") for k, v in ctx.items()}
    # После прочерков, а не до: пустая оговорка у взрослого должна остаться
    # пустой строкой, «—» отдельным абзацем посреди договора выглядит браком.
    result["minor_clause"] = MINOR_CLAUSE if is_minor(data, today=today) else ""
    return result


# Готовые причины отказа. Каждая знает, на какой шаг вернуть человека:
# переигрывать всю анкету из-за нечитаемого селфи - верный способ получить
# брошенную заявку вместо исправленной.
REJECT_REASONS: dict[str, tuple[str, str]] = {
    "fio": ("ФИО не совпадает с документом", WAIT_FIO),
    "passport": ("Паспортные данные с ошибкой", WAIT_PASSPORT),
    "addr": ("Адрес указан не полностью", WAIT_REG_ADDR),
    "phones": ("Телефоны не подходят", WAIT_PHONE2),
    "doc": ("Фото документа не читается", WAIT_DOC),
    "parent": ("Согласие родителя не читается", WAIT_PARENT_CONSENT),
}


def reject_back_to(code: str, anketa: dict | None, *,
                   today: date | None = None) -> str:
    """Шаг, на который возвращает отказ.

    Причина «согласие родителя» у взрослого - это промах модератора по кнопке:
    отправить совершеннолетнего за согласием родителя значит запереть его
    на шаге, которого в его сценарии нет. Такому возвращаем шаг документа.
    """
    back_to = REJECT_REASONS[code][1]
    if back_to == WAIT_PARENT_CONSENT and not is_minor(anketa, today=today):
        return WAIT_DOC
    return back_to


MODERATION_DATA = re.compile(r"^(?:approve|reject|rj|rjc|rjx):-?\d+(?::[a-z]+)?$")


def is_moderation_data(data: str | None) -> bool:
    """Любая кнопка карточки модерации, а не только «Одобрить»/«Отклонить».

    Отдельная проверка нужна middleware: по ней апдейт пускается в служебный
    чат мимо пользовательского конвейера. Пока здесь стоял разбор только
    approve/reject, кнопки выбора причины отказа отбрасывались как чужие -
    в группе они не доходили до обработчика вообще.
    """
    return bool(data) and MODERATION_DATA.fullmatch(data) is not None


def parse_reject_callback(data: str | None) -> tuple[int, str] | None:
    """Разбор rj:<tg_id>:<code>. Неизвестный код отсекается здесь."""
    if not data:
        return None
    m = re.fullmatch(r"rj:(\d+):([a-z]+)", data)
    if not m:
        return None
    target = int(m.group(1))
    code = m.group(2)
    if target <= 0 or code not in REJECT_REASONS:
        return None
    return target, code


def reject_comment(raw: str | None) -> Validation:
    """Свободный текст отказа от модератора.

    Уходит пользователю, поэтому режется по длине и по разметке: карточка
    модерации и сообщение пользователю идут с parse_mode=HTML.
    """
    text = _clean(raw)
    if not 3 <= len(text) <= 500:
        return Validation(False, error="Опишите ошибку текстом от 3 до 500 символов.")
    return Validation(True, value=text)


RATE_SOFT_DEFAULT, RATE_HARD_DEFAULT = 40, 50


def rate_limit_verdict(count: int, soft: int = RATE_SOFT_DEFAULT,
                       hard: int = RATE_HARD_DEFAULT) -> str:
    """'ok' | 'warn' | 'drop'. Выше жёсткого порога отвечать нельзя:
    ответ на флуд сам становится флудом.

    Пороги подняты с 20/25 вместе с появлением анкеты: регистрация выросла
    с шести сообщений до шестнадцати, и человек, заполняющий её быстро и
    с парой опечаток, упирался в предупреждение о флуде посреди собственной
    анкеты - на ровном месте и без единой ошибки со своей стороны.
    """
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
