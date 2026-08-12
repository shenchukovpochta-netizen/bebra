"""Чистая логика парка и броней. Без aiogram и без asyncpg, как app/logic.py.

Здесь всё, что проверяется без Telegram и без базы: статусы единиц техники,
разбор операторских форм «ключ: значение», сводка парка, срок удержания
брони. Велосипед в этих терминах - конкретная единица с вин-номером рамы,
а не строка текста в issue_data: на вин-номере держится и автозавод единицы
при выдаче, и запрет двух активных аренд на одну раму.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from ..logic import KIT_FIELDS, esc, normalize_phone

# ─────────────────────────── статусы единицы ───────────────────────────

FREE, BOOKED, RENTED, SERVICE, LOST = "free", "booked", "rented", "service", "lost"
BIKE_STATUSES = frozenset({FREE, BOOKED, RENTED, SERVICE, LOST})

STATUS_TITLES = {FREE: "свободен", BOOKED: "бронь", RENTED: "в аренде",
                 SERVICE: "в сервисе", LOST: "утерян"}
# Эмодзи вместо слов в строках счётчиков: сводка по нескольким моделям
# и двум точкам словами не влезает в экран телефона оператора.
STATUS_MARKS = {FREE: "🟢", BOOKED: "🟡", RENTED: "🔵", SERVICE: "🔧", LOST: "⛔"}

# Ввод оператора: несколько написаний на статус, как в ISSUE_ALIASES, -
# форму заполняют с телефона, и «ремонт»/«сервис» - это одно и то же.
RU_STATUS = {
    "свободен": FREE, "свободный": FREE, "free": FREE,
    "бронь": BOOKED, "резерв": BOOKED, "забронирован": BOOKED,
    "аренда": RENTED, "в аренде": RENTED, "выдан": RENTED,
    "сервис": SERVICE, "ремонт": SERVICE, "в ремонте": SERVICE,
    "утерян": LOST, "потерян": LOST, "угнан": LOST,
}

# ─────────────────────────── статусы брони ───────────────────────────
#
# Бронь этапа «парк»: оператор удерживает конкретную единицу под клиента
# (телефонный звонок, «выезжаю»). Статус paid появится вместе с эквайрингом -
# предоплата будет продлевать удержание; сейчас бронь живёт только таймером.

HELD, ISSUED, EXPIRED, CANCELLED = "held", "issued", "expired", "cancelled"

HOLD_DEFAULT_MINUTES = 120          # «клиент выехал» - пара часов запаса
HOLD_MAX_MINUTES = 24 * 60          # дольше суток - это уже не удержание

# Бронь из приложения: единица держится до обещанного визита плюс запас
# на опоздание. Горизонт - двое суток: дальше это не бронь, а разговор
# с оператором о планах.
BOOKING_GRACE_MINUTES = 60
BOOKING_HORIZON_HOURS = 48


def _clean(raw: str | None) -> str:
    return re.sub(r"\s+", " ", (raw or "").strip())


def _no_markup(value: str) -> bool:
    return not re.search(r"[<>&]", value)


def normalize_vin(raw: str | None) -> str:
    """Вин-номер к одному виду: пробелы схлопнуты, буквы прописные.

    По вин-номеру рамы единица ищется и заводится автоматически при выдаче,
    и «ab 123» с «AB123» обязаны быть одной и той же рамой - иначе в парке
    появится вторая, несуществующая.
    """
    return re.sub(r"\s+", "", (raw or "").strip()).upper()


# ─────────────────────────── формы оператора ───────────────────────────

# Ключи формы /bike -> поле. Несколько написаний на ключ, как в ISSUE_ALIASES.
BIKE_ALIASES: dict[str, str] = {
    "рама": "vin_frame", "вин": "vin_frame", "vin": "vin_frame",
    "вин рамы": "vin_frame", "vin рамы": "vin_frame",
    "мотор": "vin_motor", "мотор-колесо": "vin_motor",
    "вин мотора": "vin_motor", "vin мотора": "vin_motor",
    "модель": "model", "марка": "model",
    "точка": "point", "адрес": "point",
    "акб": "battery_count",
    "статус": "status",
    "заметка": "notes", "заметки": "notes", "коммент": "notes",
}

BIKE_FORM_TEMPLATE = (
    "/bike\n"
    "рама: \n"
    "мотор: \n"
    "модель: Truck+\n"
    "точка: Адоратского\n"
    "акб: 2"
)


def parse_bike_form(raw: str | None) -> tuple[dict[str, object] | None, str]:
    """Разбор формы /bike: добавить или поправить единицу.

    Возвращает (поля, "") либо (None, текст ошибки). Неизвестные строки -
    ошибка вслух, а не молча: опечатка в ключе иначе тихо теряла бы значение,
    как и в parse_issue_form.
    """
    data: dict[str, object] = {}
    unknown: list[str] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        field = BIKE_ALIASES.get(_clean(key).lower())
        if field is None:
            unknown.append(key.strip())
            continue
        value = _clean(value)
        if not value:
            continue
        # Ключ и значение в текстах ошибок экранируются: ответ уходит
        # с parse_mode=HTML, и «<х>вилка» в сырой ошибке валила бы отправку.
        if not _no_markup(value):
            return None, f"В строке «{esc(key.strip())}» недопустимы символы < > и &."
        if field == "battery_count":
            if not value.isdigit():
                return None, (f"«{esc(key.strip())}» - нужно число, "
                              f"получено «{esc(value)}».")
            data[field] = int(value)
        elif field == "status":
            status = RU_STATUS.get(value.lower())
            if status is None:
                return None, ("Статус не распознан. Варианты: "
                              + ", ".join(sorted(set(RU_STATUS))) + ".")
            data[field] = status
        elif field in ("vin_frame", "vin_motor"):
            data[field] = normalize_vin(value)
        else:
            data[field] = value
    if unknown:
        return None, ("Не понял строки: " + ", ".join(esc(k) for k in unknown[:5])
                      + ". Ключи: " + ", ".join(sorted(set(BIKE_ALIASES))) + ".")
    if not data.get("vin_frame"):
        return None, "Не хватает строки «рама: ...» - без вин-номера рамы единицу не завести."
    return data, ""


# Ключи формы /hold. «до» и «часов» - два способа сказать одно и то же:
# кто-то думает временем («до 18:00»), кто-то длительностью («часов: 3»).
HOLD_ALIASES: dict[str, str] = {
    "велосипед": "bike", "вел": "bike", "байк": "bike", "id": "bike", "номер": "bike",
    "часов": "hours", "часа": "hours", "час": "hours",
    "минут": "minutes", "мин": "minutes",
    "до": "until",
    "кто": "note", "клиент": "note", "телефон": "note", "контакт": "note",
    "заметка": "note",
}

HOLD_FORM_TEMPLATE = (
    "/hold\n"
    "велосипед: 3\n"
    "часов: 2\n"
    "кто: +7 900 000-00-00, Иван"
)


def parse_hold_form(raw: str | None, *, now: datetime) -> tuple[dict[str, object] | None, str]:
    """Разбор формы /hold: удержать единицу под клиента.

    Возвращает {"bike_ref", "minutes", "note"} либо (None, ошибка).
    now передаётся снаружи: «до 18:00» без него не посчитать, а тесты
    не должны зависеть от часов на машине.
    """
    bike_ref = ""
    note_parts: list[str] = []
    hours = minutes = 0
    until: datetime | None = None
    unknown: list[str] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        clean_key = _clean(key).lower()
        field = HOLD_ALIASES.get(clean_key)
        if field is None:
            unknown.append(key.strip())
            continue
        # partition режет по первому двоеточию, поэтому «до: 18:00»
        # оставляет время целым: 18:00 - это уже значение.
        value = _clean(value)
        if not value:
            continue
        if not _no_markup(value):
            return None, f"В строке «{esc(key.strip())}» недопустимы символы < > и &."
        if field == "bike":
            bike_ref = value
        elif field == "hours":
            if not value.isdigit():
                return None, (f"«{esc(key.strip())}» - нужно число часов, "
                              f"получено «{esc(value)}».")
            hours = int(value)
        elif field == "minutes":
            if not value.isdigit():
                return None, (f"«{esc(key.strip())}» - нужно число минут, "
                              f"получено «{esc(value)}».")
            minutes = int(value)
        elif field == "until":
            m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", value)
            if not m:
                return None, "«до» - время в виде ЧЧ:ММ, например 18:00."
            try:
                until = now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                    second=0, microsecond=0)
            except ValueError:
                return None, "Такого времени не бывает. Проверьте часы и минуты."
            if until <= now:
                # «до 09:00» вечером - это до утра: время в прошлом
                # означает следующий день, а не ошибку.
                until += timedelta(days=1)
        else:
            note_parts.append(value)
    if unknown:
        return None, ("Не понял строки: " + ", ".join(esc(k) for k in unknown[:5])
                      + ". Ключи: " + ", ".join(sorted(set(HOLD_ALIASES))) + ".")
    if not bike_ref:
        return None, "Не хватает строки «велосипед: ...» - номер из /bikes или вин рамы."
    if until is not None and (hours or minutes):
        # Молчаливый победитель здесь опасен: оператор думает, что удержал
        # на 5 часов, а бронь живёт до «до». Пусть выберет что-то одно.
        return None, "Либо «до: ЧЧ:ММ», либо «часов/минут» - не вместе."
    if until is not None:
        total = int((until - now).total_seconds() // 60)
    else:
        total = hours * 60 + minutes or HOLD_DEFAULT_MINUTES
    if not 1 <= total <= HOLD_MAX_MINUTES:
        return None, (f"Удержание - от 1 минуты до {HOLD_MAX_MINUTES // 60} часов. "
                      f"Получилось {total} мин.")
    return {"bike_ref": bike_ref, "minutes": total, "note": " ".join(note_parts)}, ""


def parse_ref_args(raw: str | None) -> tuple[str, str]:
    """Аргументы /unhold, /service, /free: ссылка на единицу и заметка.

    Ссылка - номер из /bikes или вин рамы; чем именно, разбирается на слое
    базы (сначала id, потом вин): вин бывает и чисто цифровым, угадать
    по виду нельзя.
    """
    text = _clean(raw)
    if not text:
        return "", ""
    first, _, rest = text.partition(" ")
    return first, rest.strip()


# ─────────────────────────── команды ───────────────────────────

FLEET_COMMANDS = ("park", "bikes", "bike", "hold", "unhold", "service", "free")

# Длинные альтернативы раньше коротких: /bikes не должен совпасть как /bike.
# Суффикс @имя_бота обязателен в группах - Telegram дописывает его сам.
_CMD = re.compile(
    r"^/(" + "|".join(sorted(FLEET_COMMANDS, key=len, reverse=True))
    + r")(?:@[A-Za-z0-9_]+)?(?=\s|$)")


def command_name(text: str | None) -> str | None:
    m = _CMD.match((text or "").strip())
    return m.group(1) if m else None


def is_fleet_command(text: str | None) -> bool:
    """Команда парка. По ней middleware пускает сообщение из служебного
    чата мимо пользовательского конвейера - как кнопки модерации."""
    return command_name(text) is not None


def command_args(text: str | None) -> str:
    """Текст после команды: форма со следующей строки или аргументы в той же."""
    text = (text or "").strip()
    m = _CMD.match(text)
    return text[m.end():].strip() if m else text


def validate_pickup(ts, *, now: datetime, open_hour: int,
                    close_hour: int) -> tuple[datetime | None, str]:
    """Время визита из Mini App: (datetime, "") либо (None, ошибка).

    ts - unix-секунды; переводятся в местное время сервера (контейнер живёт
    в TZ проката), и рабочие часы проверяются в нём же. Пять минут назад -
    ещё не «прошло»: клиент жмёт «сейчас», а часы телефона спешат.
    """
    try:
        pickup = datetime.fromtimestamp(int(ts), tz=now.tzinfo)
    except (TypeError, ValueError, OverflowError, OSError):
        return None, "Не понял время визита."
    if (now - pickup).total_seconds() > 5 * 60:
        return None, "Это время уже прошло. Выберите время впереди."
    if (pickup - now).total_seconds() > BOOKING_HORIZON_HOURS * 3600:
        return None, (f"Бронь принимается не дальше, чем на "
                      f"{BOOKING_HORIZON_HOURS // 24} суток вперёд.")
    if not open_hour <= pickup.hour < close_hour:
        return None, f"Точки работают с {open_hour}:00 до {close_hour}:00."
    return pickup, ""


def hold_minutes_for_pickup(pickup: datetime, *, now: datetime) -> int:
    """Сколько держать единицу: до визита плюс запас на опоздание."""
    minutes = int((pickup - now).total_seconds() // 60) + BOOKING_GRACE_MINUTES
    return max(minutes, BOOKING_GRACE_MINUTES)


# ─────────────────────────── заряд АКБ ───────────────────────────
#
# Проценты считаются из напряжения тяговой батареи (телеметрия StarLine).
# Границы задал владелец: 54.2 В - батарея пуста, 67.2 В - полна.
# Шкала линейная: точнее кривой разряда бот не знает, а клиенту нужен
# ориентир «сколько осталось», а не лабораторная точность.

BATTERY_V_EMPTY = 54.2
BATTERY_V_FULL = 67.2


def battery_percent(voltage) -> int | None:
    """Процент заряда из напряжения. None - напряжение не похоже на правду
    (нет данных, датчик отдал мусор или бортовые 12 В вместо тяговой)."""
    try:
        volts = float(voltage)
    except (TypeError, ValueError):
        return None
    # Здравый диапазон тяговой батареи: сильно ниже «нуля» или выше
    # «сотни» - это не заряд, а чужой датчик.
    if not BATTERY_V_EMPTY - 5 <= volts <= BATTERY_V_FULL + 3:
        return None
    share = (volts - BATTERY_V_EMPTY) / (BATTERY_V_FULL - BATTERY_V_EMPTY)
    return max(0, min(100, round(share * 100)))


def price_amount(raw: str | None) -> int | None:
    """Сумма в рублях из строки оплаты: «3000qr» -> 3000, «3 000 нал» -> 3000.

    None - цифр нет или сумма неправдоподобна: предзаполнить поле счёта
    нечем, оператор впишет руками.
    """
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    amount = int(digits)
    return amount if 1 <= amount <= 1_000_000 else None


# ─────────────────────────── плановое ТО ───────────────────────────
#
# Раз в две недели аренды - «бесплатное обслуживание» из тарифа.
# Отсчёт от выдачи (велосипед проверен перед передачей) или от
# последнего проведённого ТО.

SERVICE_INTERVAL_DAYS = 14


def service_days_left(last_service_at, *, now: datetime) -> int | None:
    """Сколько дней осталось до планового ТО. Отрицательное - просрочено.

    None - момент последнего ТО неизвестен (старые строки до колонки):
    напоминать не о чем, пока оператор не отметит первое ТО или не выдаст
    единицу заново.
    """
    if last_service_at is None:
        return None
    if last_service_at.tzinfo is not None and now.tzinfo is None:
        now = now.astimezone(last_service_at.tzinfo)
    return SERVICE_INTERVAL_DAYS - (now - last_service_at).days


def service_due(last_service_at, *, now: datetime) -> bool:
    days = service_days_left(last_service_at, now=now)
    return days is not None and days <= 0


def overdue(due_at, closed_at, *, today: date) -> bool:
    """Аренда просрочена: срок возврата прошёл, а возврата не было.

    Пороговое условие для автоблокировки и для красной строки в CRM.
    Без due (срок не распознан) - не просрочена: блокировать по незнанию
    нельзя.
    """
    return bool(due_at) and closed_at is None and due_at < today


def needs_service(close: dict | None) -> bool:
    """Повреждения в форме закрытия - единица уходит в сервис, не на витрину.

    Критерий тот же, что у close_notes в app/logic.py: «0», прочерк и «нет»
    означают отсутствие повреждений. Разъехаться им нельзя - иначе акт
    говорит «повреждения есть», а велосипед стоит на витрине свободным.
    """
    damage = str((close or {}).get("damage") or "0").strip()
    return damage not in ("", "0", "-", "—", "нет")


# ─────────────────────────── срок аренды ───────────────────────────

_TERM_DATE = re.compile(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?")


def parse_due(rent_term: str | None, *, today: date) -> date | None:
    """Дата возврата из срока вида «03.08 - 10.08». None - не разобрать.

    Берётся последняя дата строки. Год чаще опущен: если конец срока
    оказался раньше НАЧАЛА срока, это переход через Новый год - берём
    следующий. Якорь - именно первая дата срока, а не «сегодня»: CRM
    импортирует и просроченные аренды, и «28.07 - 08.08» в середине
    августа - это просрочка на этой неделе, а не срок до следующего лета.
    Ошибка разбора - это None, а не исключение: срок пишет оператор
    свободным текстом, и «до конца месяца» не должно ронять выдачу.
    """
    matches = _TERM_DATE.findall(rent_term or "")
    if not matches:
        return None

    def to_date(match, year_default: int) -> date | None:
        day, month, year_raw = match
        year = int(year_raw) if year_raw else year_default
        if year < 100:
            year += 2000
        try:
            return date(year, int(month), int(day))
        except ValueError:
            return None

    anchor = today
    if len(matches) >= 2:
        start = to_date(matches[0], today.year)
        # Начало срока не может быть позже импорта. Если дата без года
        # оказалась в будущем - это прошлый год: «28.12 - 04.01»,
        # разбираемое в январе, началось в декабре ПРОШЛОГО года.
        if start is not None and not matches[0][2] and start > today:
            start = to_date(matches[0], today.year - 1) or start
        anchor = start or today
    value = to_date(matches[-1], anchor.year)
    if value is None:
        return None
    if not matches[-1][2] and value < anchor:
        return to_date(matches[-1], anchor.year + 1)     # None на 29.02
    return value


# ─────────────────── форма фиксации: разбор в CRM ───────────────────
#
# Заполненные формы фиксации (те самые «1. ФИО: ...», которые собирает
# fixation_form в app/logic.py) годами копились в теме «Фиксация сдачи».
# CRM разбирает их обратно в данные: клиент, единица, аренда.
# Разбор по МЕТКАМ строк, а не по номерам: нумерация в живых формах
# плавает - строка «Ник в Telegram» приходит и с «9.», и без него.

# Метка (по началу строки, без учёта регистра) -> поле. Пустое поле -
# заголовок, у которого значения ниже отдельными строками.
FIXATION_LABELS: tuple[tuple[str, str], ...] = (
    ("фио", "fio"),
    ("вин номер рамы", "vin_frame"),
    ("вин номер мотор", "vin_motor"),
    ("комплектация", ""),
    ("сроки аренды", "rent_term"),
    ("номер телефона (основной)", "phone"),
    ("номер телефона 2", "phone2"),
    ("номер телефона 3", "phone3"),
    ("ник в telegram", "tg_username"),
    ("сумма и способ оплаты", "rent_price"),
    ("адрес прописки", "reg_address"),
    ("адрес проживания", "live_address"),
    ("подключен gps", "gps"),
    ("адрес сдачи", "return_point"),
    ("кто выдал", "issued_by"),
    ("подписка на тг", "tg_subscribed"),
    ("реф", "ref_program"),
)

# Строки комплектации сверяются с подписями из формы (KIT_FIELDS в
# app/logic.py) - это один и тот же текст, печатает его один код.
_KIT_BY_LABEL = {label.lower(): field for field, label in KIT_FIELDS}

_LINE_NUMBER = re.compile(r"^\d{1,2}[.)]\s*")
_PHONE_HEAD = re.compile(r"[+\d][\d\s()\-]*")


def _split_phone(raw: str) -> tuple[str | None, str]:
    """Номер и приписка: «89053731217 друг» -> («+79053731217», «друг»).

    Голова регулярки жадно ест и цифры, и пробелы, поэтому «...17 2 симка»
    захватила бы лишнюю цифру приписки. Отрезаем хвостовые слова головы,
    пока остаток не станет похож на номер: «8 905 023 83 66» разбирается
    целиком, а «...17 2 симка» отдаёт номер и оставляет «2 симка» припиской.
    """
    text = _clean(raw)
    m = _PHONE_HEAD.match(text)
    if not m:
        return None, text
    head, tail = m.group(0).strip(), text[m.end():]
    while normalize_phone(head) is None and " " in head:
        head, _, dropped = head.rpartition(" ")
        tail = f"{dropped} {tail}"
    phone = normalize_phone(head)
    if phone is None:
        return None, text
    return phone, tail.strip(" ,;-")


def parse_fixation_form(raw: str | None) -> tuple[dict | None, list[str]]:
    """Разбор заполненной формы фиксации: (данные, предупреждения)
    либо (None, [ошибка]).

    Предупреждения разбор не останавливают: форму заполняют люди,
    и «строка не распознана» должна быть видна оператору в предпросмотре,
    а не превращаться в молчаливую потерю значения. Фатальны только
    отсутствие ФИО и вин-номера рамы - без них записывать нечего
    и не к чему привязать аренду.
    """
    data: dict = {"kit": {}}
    warnings: list[str] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(("-", "•", "–")):
            key, _, value = line.lstrip("-•– ").partition(":")
            field = _KIT_BY_LABEL.get(_clean(key).lower())
            if field is None:
                warnings.append(f"комплектация: не понял строку «{key.strip()}»")
                continue
            value = _clean(value)
            if value and not value.isdigit():
                warnings.append(f"комплектация «{key.strip()}»: ожидалось число, "
                                f"получено «{value}» - записан 0")
            data["kit"][field] = int(value) if value.isdigit() else 0
            continue

        stripped = _LINE_NUMBER.sub("", line)
        low = stripped.lower()
        for label, field in FIXATION_LABELS:      # noqa: B007 - field нужен после break
            # Граница слова обязательна: голый startswith дал бы «фио…»
            # для «Фиокрест» и молча затёр настоящее ФИО, а «реф» -
            # для «Рефлектор». Следующий за меткой символ - не буква.
            if low.startswith(label) and not low[len(label):len(label) + 1].isalpha():
                break
        else:
            warnings.append(f"не распознана строка: «{stripped[:50]}»")
            continue
        if not field:
            continue                       # заголовок «Комплектация»
        value = _clean(stripped.partition(":")[2])
        if not value or value in ("—", "-"):
            continue
        if field in ("phone", "phone2", "phone3"):
            phone, note = _split_phone(value)
            if phone is None:
                warnings.append(f"«{label}»: не похоже на номер - «{value}»")
                note = value
            else:
                data[field] = phone
            if note:
                data[field + "_note"] = note
        elif field in ("vin_frame", "vin_motor"):
            data[field] = normalize_vin(value)
        elif field == "tg_username":
            username = value.lstrip("@").strip()
            if re.fullmatch(r"[A-Za-z0-9_]{4,32}", username):
                data[field] = username
            else:
                warnings.append(f"ник в Telegram не распознан: «{value}»")
        elif field == "gps":
            data[field] = value.lower() in ("да", "есть", "подключен",
                                            "подключён", "+", "1")
        else:
            data[field] = value

    if not str(data.get("fio") or "").strip():
        return None, ["в форме нет строки «ФИО» - записывать некого"]
    if not data.get("vin_frame"):
        return None, ["в форме нет вин-номера рамы - не к чему привязать аренду"]
    return data, warnings


def fixation_extra(parsed: dict) -> dict:
    """Служебные поля аренды из формы - в один jsonb.

    Отдельные колонки им не положены: это заметки о выдаче (кто выдал,
    куда сдавать, подписка, реф), по ним не ищут и не строят инвариантов.
    """
    keep = ("gps", "issued_by", "return_point", "tg_subscribed",
            "ref_program", "phone_note", "phone2_note", "phone3_note")
    return {k: parsed[k] for k in keep if parsed.get(k) not in (None, "")}


# ─────────────────────────── сводки ───────────────────────────

_STATUS_ORDER = (FREE, BOOKED, RENTED, SERVICE, LOST)


def _counts_line(counts: dict[str, int]) -> str:
    return " · ".join(f"{STATUS_MARKS[s]} {counts[s]}"
                      for s in _STATUS_ORDER if counts.get(s))


def park_text(rows) -> str:
    """Сводка /park из строк (точка, модель, статус, количество).

    Пустой парк - отдельная фраза с подсказкой: пустой ответ выглядит
    как поломка бота, а не как пустая база.
    """
    rows = [(p, m, s, int(c)) for p, m, s, c in rows if int(c)]
    if not rows:
        return ("Парк пуст. Добавьте первую единицу:\n"
                "<code>/bike\nрама: VIN\nмодель: Truck+</code>")
    total: dict[str, int] = {}
    points: dict[str, dict[str, dict[str, int]]] = {}
    for point, model, status, count in rows:
        by_model = points.setdefault(point or "без точки", {})
        counts = by_model.setdefault(model or "без модели", {})
        counts[status] = counts.get(status, 0) + count
        total[status] = total.get(status, 0) + count

    lines = [f"🚲 <b>Парк — {sum(total.values())} ед.</b>"]
    for point in sorted(points):
        lines.append(f"\n📍 <b>{esc(point)}</b>")
        for model in sorted(points[point]):
            lines.append(f"· {esc(model)}: {_counts_line(points[point][model])}")
    lines.append("\nИтого: " + _counts_line(total))
    lines.append(" ".join(f"{STATUS_MARKS[s]} {STATUS_TITLES[s]}"
                          for s in _STATUS_ORDER if total.get(s)))
    return "\n".join(lines)


def bike_line(row: dict) -> str:
    """Одна единица в списке /bikes."""
    status = str(row.get("status") or FREE)
    head = (f"<b>#{row.get('id')}</b> {esc(row.get('model') or 'без модели')} · "
            f"{STATUS_MARKS.get(status, '·')} {STATUS_TITLES.get(status, status)}")
    parts = [head, f"    рама <code>{esc(row.get('vin_frame'))}</code>"
             + (f", мотор <code>{esc(row['vin_motor'])}</code>" if row.get("vin_motor") else "")]
    if row.get("point"):
        parts.append(f"    точка: {esc(row['point'])}")
    if status == RENTED and (row.get("renter_name") or row.get("renter_username")):
        handle = f" (@{esc(row['renter_username'])})" if row.get("renter_username") else ""
        parts.append(f"    арендатор: {esc(row.get('renter_name') or '—')}{handle}")
    if status == BOOKED and row.get("hold_note"):
        parts.append(f"    бронь: {esc(row['hold_note'])}")
    if row.get("notes"):
        parts.append(f"    {esc(row['notes'])}")
    return "\n".join(parts)


def availability_payload(rows) -> list[dict]:
    """Счётчики для /api/availability: только цифры, без вин-номеров и людей.

    Наружу из парка уходит ровно то, что видно с витрины: сколько единиц
    какой модели в каком статусе на какой точке.
    """
    out: dict[tuple, dict] = {}
    for point, model, status, count in rows:
        key = (point or "", model or "")
        item = out.setdefault(key, {
            "point": point, "model": model,
            **{s: 0 for s in _STATUS_ORDER},
        })
        item[status] = item.get(status, 0) + int(count)
    return [out[k] for k in sorted(out)]
