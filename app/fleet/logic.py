"""Чистая логика парка и броней. Без aiogram и без asyncpg, как app/logic.py.

Здесь всё, что проверяется без Telegram и без базы: статусы единиц техники,
разбор операторских форм «ключ: значение», сводка парка, срок удержания
брони. Велосипед в этих терминах - конкретная единица с вин-номером рамы,
а не строка текста в issue_data: на вин-номере держится и автозавод единицы
при выдаче, и запрет двух активных аренд на одну раму.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from ..logic import esc

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
        if not _no_markup(value):
            return None, f"В строке «{key.strip()}» недопустимы символы < > и &."
        if field == "battery_count":
            if not value.isdigit():
                return None, f"«{key.strip()}» - нужно число, получено «{value}»."
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
        return None, ("Не понял строки: " + ", ".join(unknown[:5])
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
            return None, f"В строке «{key.strip()}» недопустимы символы < > и &."
        if field == "bike":
            bike_ref = value
        elif field == "hours":
            if not value.isdigit():
                return None, f"«{key.strip()}» - нужно число часов, получено «{value}»."
            hours = int(value)
        elif field == "minutes":
            if not value.isdigit():
                return None, f"«{key.strip()}» - нужно число минут, получено «{value}»."
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
                return None, "«до " + value + "» уже прошло - укажите время впереди."
        else:
            note_parts.append(value)
    if unknown:
        return None, ("Не понял строки: " + ", ".join(unknown[:5])
                      + ". Ключи: " + ", ".join(sorted(set(HOLD_ALIASES))) + ".")
    if not bike_ref:
        return None, "Не хватает строки «велосипед: ...» - номер из /bikes или вин рамы."
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

    Берётся последняя дата строки. Год чаще опущен: если дата без года
    оказалась в прошлом, это переход через Новый год - берём следующий.
    Ошибка разбора - это None, а не исключение: срок пишет оператор
    свободным текстом, и «до конца месяца» не должно ронять выдачу.
    """
    matches = _TERM_DATE.findall(rent_term or "")
    if not matches:
        return None
    day, month, year_raw = matches[-1]
    year = int(year_raw) if year_raw else today.year
    if year < 100:
        year += 2000
    try:
        value = date(year, int(month), int(day))
    except ValueError:
        return None
    if not year_raw and value < today:
        try:
            value = date(year + 1, int(month), int(day))
        except ValueError:
            return None                       # 29.02 в невисокосный
    return value


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
