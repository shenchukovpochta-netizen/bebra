"""Импорт учётной таблицы проката (xlsx) в CRM.

Формат - рабочая таблица «ДЕЙСТВУЮЩИЕ АРЕНДАТОРЫ»: одна строка на
велосипед, колонки узнаются по заголовкам (№, Модель, ВИН колеса,
ВИН рамы, ФИО, телефон, тариф, цена, оплачено, долг, «до какого
оплачена», статус, адреса, комментарий, GPS). Порядок колонок
неважен, лишние игнорируются.

Из строки получаются:
  - велосипед (всегда, если есть хоть номер или VIN),
  - клиент (если есть ФИО и разборчивый телефон),
  - аренда с начислением и платежом (если статус «в аренде»).

Импорт идемпотентен: велосипед с известным VIN или номером, клиент
с известным телефоном и клиент с уже идущей арендой пропускаются
с пометкой в отчёте. Сначала строится план, потом он применяется -
поэтому сухой прогон показывает ровно то, что случится.

Запуск из консоли (в контейнере crm или bot):
    python -m app.crm.import_xlsx таблица.xlsx            # только отчёт
    python -m app.crm.import_xlsx таблица.xlsx --apply    # записать
"""

from __future__ import annotations

import asyncio
import io
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from .. import logic as bot_logic
from . import logic

MAX_ROWS = 5000


class ImportError_(Exception):
    """Файл не читается или в нём нет узнаваемых заголовков."""

# Заголовок -> внутреннее имя. Сравнение по нормализованному тексту
# (без пробелов, дефисов и регистра): в таблице заголовки с переносами.
COLUMNS = {
    "№": "no", "n": "no", "номер": "no",
    "модель": "model",
    "винколеса": "motor", "винмотора": "motor", "мотор": "motor",
    "винрамы": "frame", "рама": "frame",
    "фио": "fio",
    "основнойномертелефона": "phone", "телефон": "phone", "номертелефона": "phone",
    "комплектация": "kit",
    "допномертелфона": "phone2", "допномертелефона": "phone2", "доптелефон": "phone2",
    "никвtg": "tg", "никвтг": "tg", "telegram": "tg",
    "ссылканаконтактвwa": "wa", "whatsapp": "wa",
    "когдабрал": "started", "датавыдачи": "started",
    "ценаарендыподоговору": "price", "цена": "price",
    "тариф": "tariff",
    "числодней": "days",
    "сколькооплатил": "paid", "оплачено": "paid",
    "суммадолга": "debt", "долг": "debt",
    "хронологиязвонковпереписок": "log",
    "докакогооплаченааренда": "paid_until", "оплаченодо": "paid_until",
    "статус": "status",
    "адресрегистрации": "addr_reg",
    "адреспроживания": "addr_live",
    "комментарий": "comment",
    "gpsтрекер": "gps",
}


def _norm_header(text: Any) -> str:
    return re.sub(r"[\s\-_/?()]+", "", str(text or "")).lower()


def _cell(value: Any) -> Any:
    """Ячейка как есть, но пробельная строка и ошибка формулы - пусто."""
    if isinstance(value, str):
        value = value.strip()
        if not value or value.startswith("#"):
            return None
    return value


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(str(value).split())


def _multiline(value: Any) -> str:
    return "\n".join(line.strip() for line in str(value or "").splitlines() if line.strip())


def _vin(value: Any) -> str | None:
    """Заводской номер из ячейки: самая длинная буквенно-цифровая цепочка.
    «старая рама ZQV2024…» -> ZQV2024…, «БЕЗ РАМЫ» -> None."""
    tokens = re.findall(r"[A-Za-z0-9\-]{6,}", str(value or ""))
    tokens = [t for t in tokens if re.search(r"\d", t)]
    return max(tokens, key=len).upper() if tokens else None


def _money_cell(value: Any) -> Decimal | None:
    """Сумма из ячейки: число, «22к», «3 500». Текст - None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return logic.to_money(value) if 0 <= value <= 10_000_000 else None
    text = str(value).strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)(к|k|тыс\.?)?(р|₽|руб\.?)?", text)
    if not m:
        return None
    amount = Decimal(m.group(1).replace(",", "."))
    if m.group(2):
        amount *= 1000
    return logic.to_money(amount) if amount <= 10_000_000 else None


def _days_cell(value: Any) -> int | None:
    """Срок в днях: «7», «7 дней», «2 недели», «1 месяц» (30 дней)."""
    text = str(value or "").lower()
    m = re.search(r"\d+", text)
    if not m:
        return None
    days = int(m.group())
    if "нед" in text:
        days *= 7
    elif "мес" in text:
        days *= 30
    return days if 1 <= days <= logic.MAX_PERIOD_DAYS else None


def _date_cell(value: Any, *, year_from: date | None = None,
               not_after: date | None = None) -> date | None:
    """Дата из ячейки: datetime, «до 14.09», «на 28.03», «14.09.2026».
    Год без года берётся от даты выдачи; если получилось раньше выдачи -
    следующий год. Для самой даты выдачи год - текущий, но не в будущем:
    «28.12» в январе - прошлый декабрь (not_after=сегодня)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?", str(value or ""))
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    year_text = m.group(3)
    base = year_from or date.today()
    year = int(year_text) if year_text else base.year
    if year < 100:
        year += 2000
    try:
        result = date(year, month, day)
    except ValueError:
        return None
    if not year_text and year_from and result < year_from:
        try:
            result = date(year + 1, month, day)
        except ValueError:
            return None
    if not year_text and not_after and result > not_after:
        try:
            result = date(year - 1, month, day)
        except ValueError:
            return None
    return result


def _phones(value: Any) -> tuple[str | None, list[str]]:
    """Основной телефон и остальные из ячейки; «(основной)» побеждает."""
    text = str(value or "")
    if isinstance(value, (int, float)):
        text = str(int(value))
    found: list[str] = []
    for chunk in re.split(r"[\n,;/]+", text):
        phone = bot_logic.normalize_phone(chunk)
        if phone and phone not in found:
            found.append(phone)
    if not found:
        return None, []
    main = found[0]
    for chunk in re.split(r"[\n,;/]+", text):
        if "основн" in chunk.lower():
            phone = bot_logic.normalize_phone(chunk)
            if phone:
                main = phone
    return main, [p for p in found if p != main]


def bike_status(status: str) -> tuple[str, str]:
    """Статус велосипеда в CRM и пояснение (место) из статуса таблицы."""
    s = status.lower()
    place = ""
    m = re.search(r"\(([^)]+)\)", status)
    if m:
        place = m.group(1).strip()
    if "аренд" in s:
        return "rented", place
    if "ремонт" in s:
        return "repair", place or status
    if "продан" in s:
        return "sold", ""
    if "полици" in s or "не можем найти" in s or "украл" in s:
        return "lost", status
    if "ждет сдачи" in s or "ждёт сдачи" in s:
        return "available", place or status
    if not s:
        return "available", ""
    return "available", status        # «Аметьево» и прочие места хранения


@dataclass
class Row:
    line: int
    no: str = ""
    model: str = ""
    motor: str | None = None
    frame: str | None = None
    fio: str = ""
    phone: str | None = None
    extra_phones: list[str] = field(default_factory=list)
    kit: str = ""
    tg: str = ""
    wa: str = ""
    started: date | None = None
    price: Decimal | None = None
    tariff: str = ""
    days: int | None = None
    paid: Decimal | None = None
    debt: Decimal | None = None
    log: str = ""
    paid_until: date | None = None
    status: str = ""
    addr_reg: str = ""
    addr_live: str = ""
    comment: str = ""
    gps: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def read_rows(source: str | bytes | io.BytesIO, *, today: date | None = None) -> list[Row]:
    """Строки таблицы: первая строка - заголовки, дальше по одной на велосипед."""
    import openpyxl

    today = today or date.today()

    if isinstance(source, bytes):
        source = io.BytesIO(source)
    try:
        wb = openpyxl.load_workbook(source, data_only=True, read_only=True)
    except Exception as e:                       # noqa: BLE001 - zip/xml, любой формат
        raise ImportError_(f"Файл не читается как xlsx: {e}") from e
    if not wb.worksheets:
        raise ImportError_("В файле нет листов.")
    ws = wb.worksheets[0]
    rows: list[Row] = []
    columns: dict[int, str] = {}
    for line, values in enumerate(ws.iter_rows(values_only=True), start=1):
        if line > MAX_ROWS:
            break
        if not columns:
            for idx, cell in enumerate(values):
                key = COLUMNS.get(_norm_header(cell))
                if key and key not in columns.values():
                    columns[idx] = key
            if "fio" in columns.values() or "motor" in columns.values():
                continue
            columns = {}
            continue
        raw = {columns[i]: _cell(values[i]) for i in columns if i < len(values)}
        if not any(_text(raw.get(k)) for k in ("no", "model", "motor", "frame", "fio", "phone")):
            continue
        phone, extra = _phones(raw.get("phone"))
        phone2, extra2 = _phones(raw.get("phone2"))
        if phone2:
            extra = extra + [phone2] + extra2
        started = _date_cell(raw.get("started"), not_after=today)
        row = Row(
            line=line, no=_text(raw.get("no")), model=_text(raw.get("model")),
            motor=_vin(raw.get("motor")), frame=_vin(raw.get("frame")),
            fio=_text(raw.get("fio")), phone=phone, extra_phones=extra,
            kit=_multiline(raw.get("kit")), tg=_text(raw.get("tg")), wa=_text(raw.get("wa")),
            started=started, price=_money_cell(raw.get("price")),
            tariff=_text(raw.get("tariff")), days=_days_cell(raw.get("days"))
            or _days_cell(raw.get("tariff")),
            paid=_money_cell(raw.get("paid")), debt=_money_cell(raw.get("debt")),
            log=_multiline(raw.get("log")),
            paid_until=_date_cell(raw.get("paid_until"), year_from=started),
            status=_text(raw.get("status")), addr_reg=_text(raw.get("addr_reg")),
            addr_live=_text(raw.get("addr_live")), comment=_multiline(raw.get("comment")),
            gps=_text(raw.get("gps")), raw=raw,
        )
        rows.append(row)
    wb.close()
    if not columns:
        raise ImportError_("Не нашёл строку заголовков: нужны хотя бы «ФИО» или «ВИН колеса».")
    return rows


# ─────────────────────────── план ───────────────────────────

@dataclass
class Plan:
    bikes: list[dict] = field(default_factory=list)
    clients: list[dict] = field(default_factory=list)      # без дублей по телефону
    rentals: list[dict] = field(default_factory=list)
    skipped_bikes: list[str] = field(default_factory=list)
    skipped_clients: list[str] = field(default_factory=list)
    skipped_rentals: list[str] = field(default_factory=list)
    debts: list[dict] = field(default_factory=list)        # долги без аренды
    warnings: list[str] = field(default_factory=list)
    rows: int = 0

    def summary(self) -> str:
        lines = [
            f"Строк с данными: {self.rows}",
            f"Велосипеды: добавить {len(self.bikes)}, пропустить {len(self.skipped_bikes)}",
            f"Клиенты: добавить {len(self.clients)}, пропустить {len(self.skipped_clients)}",
            f"Аренды: оформить {len(self.rentals)}, пропустить {len(self.skipped_rentals)}",
        ]
        if self.debts:
            total = f"{sum(d['amount'] for d in self.debts):,.0f}".replace(",", " ")
            lines.append(f"Долги без аренды (полиция, невозврат): {len(self.debts)} на {total} ₽")
        if self.warnings:
            lines.append(f"Замечания: {len(self.warnings)}")
        return "\n".join(lines)


def _client_note(row: Row) -> str:
    parts = []
    if row.extra_phones:
        parts.append("Доп. телефоны: " + ", ".join(row.extra_phones))
    if row.tg and not row.tg.startswith("@"):
        parts.append("Telegram: " + row.tg)
    if row.wa:
        parts.append("WhatsApp: " + row.wa)
    if row.addr_reg:
        parts.append("Адрес регистрации: " + row.addr_reg)
    if row.addr_live:
        parts.append("Адрес проживания: " + row.addr_live)
    if row.kit:
        parts.append("Комплектация: " + row.kit.replace("\n", "; "))
    if row.log:
        parts.append("Хронология: " + row.log.replace("\n", "; "))
    if row.comment:
        parts.append("Комментарий: " + row.comment.replace("\n", "; "))
    if row.status:
        parts.append(f"Статус в таблице: {row.status}")
    if isinstance(row.raw.get("paid"), str) and row.paid is None:
        parts.append("Оплата (из таблицы): " + _text(row.raw["paid"]))
    if isinstance(row.raw.get("debt"), str) and row.debt is None:
        parts.append("Долг (из таблицы): " + _text(row.raw["debt"]))
    if isinstance(row.raw.get("paid_until"), str) and row.paid_until is None:
        parts.append("Оплачено до (из таблицы): " + _text(row.raw["paid_until"]))
    parts.append(f"Импорт из таблицы, строка {row.line}, велосипед № {row.no or '—'}")
    return "\n".join(parts)[: logic.NOTE_LIMIT]


def _dated(day: date) -> datetime:
    """Момент записи журнала для строки таблицы: полдень дня выдачи по UTC.
    Деньги из таблицы - за прошлые месяцы; датировать их днём загрузки
    значило бы показать в отчёте «поступило за месяц» миллион, которого
    в этом месяце не было."""
    return datetime.combine(day, time(12), tzinfo=UTC)


def _bike_note(row: Row, place: str) -> str:
    parts = []
    if place:
        parts.append("Место: " + place)
    if row.gps:
        parts.append("GPS-трекер: " + row.gps)
    if not row.model:
        parts.append("Модель в таблице не указана")
    if row.comment and not row.fio:
        parts.append(row.comment.replace("\n", "; "))
    if row.fio and row.status.lower().startswith("продан"):
        parts.append(f"Покупатель по таблице: {row.fio}")
    elif row.fio and not row.phone:
        parts.append(f"Арендатор по таблице: {row.fio} (телефон не разобран)")
    if row.status:
        parts.append(f"Статус в таблице: {row.status}")
    parts.append(f"Импорт из таблицы, строка {row.line}")
    return "\n".join(parts)[: logic.NOTE_LIMIT]


async def build_plan(crm: Any, rows: list[Row], *, today: date | None = None) -> Plan:
    today = today or date.today()
    plan = Plan(rows=len(rows))
    seen_codes: set[str] = set()
    seen_vins: set[str] = set()
    planned_clients: dict[str, dict] = {}       # телефон -> клиент из плана
    clients_with_rental: set[str] = set()
    bikes_taken: set[int] = set()               # существующие, уже отданные в плане
    no_model: list[int] = []

    for row in rows:
        warn = lambda msg, r=row: plan.warnings.append(f"строка {r.line}: {msg}")  # noqa: E731
        status, place = bike_status(row.status)

        # ── велосипед ──
        code = row.no or (row.motor[-6:] if row.motor else f"СТР-{row.line}")
        if code in seen_codes:
            code = f"{code}-{row.line}"
        bike_ref: dict | None = None
        existing = None
        if row.frame:
            existing = await crm.bike_by_frame(row.frame)
        if existing is None and row.motor:
            existing = await crm.bike_by_motor(row.motor)
        if existing is None and not (row.frame or row.motor):
            # По номеру - только когда в строке нет VIN: перенумерованная
            # таблица иначе привязала бы аренду к чужому велосипеду.
            existing = await crm.bike_by_code(code)
        elif existing is None and await crm.bike_by_code(code) is not None:
            code = f"{code}-{row.line}"
        if existing is not None:
            plan.skipped_bikes.append(
                f"строка {row.line}: велосипед уже есть ({existing.get('code')})")
            bike_ref = {"existing_id": existing["id"], "status": existing.get("status")}
        elif (row.frame and row.frame in seen_vins) or (row.motor and row.motor in seen_vins):
            plan.skipped_bikes.append(f"строка {row.line}: повтор VIN в таблице")
        elif not (row.motor or row.frame or row.no):
            plan.skipped_bikes.append(f"строка {row.line}: ни номера, ни VIN")
        else:
            seen_codes.add(code)
            for v in (row.frame, row.motor):
                if v:
                    seen_vins.add(v)
            # «В аренде» без арендатора в строке: велосипед всё равно не на
            # точке - статус «в аренде», карточку аренды заведёт оператор.
            bike_ref = {"code": code, "model": row.model or "Truck+",
                        "frame_no": row.frame, "motor_no": row.motor,
                        "status": ("rented" if status == "rented" and not row.fio
                                   else status if status != "rented" else "available"),
                        "note": _bike_note(row, place), "line": row.line,
                        "wants_rented": status == "rented"}
            if not row.model:
                no_model.append(row.line)
            plan.bikes.append(bike_ref)

        # ── клиент ──
        if not row.fio:
            continue
        if status == "sold":
            plan.skipped_clients.append(
                f"строка {row.line}: {row.fio} - велосипед продан, покупатель не клиент")
            continue
        if not row.phone:
            if bike_ref is not None and bike_ref.get("wants_rented"):
                bike_ref["status"] = "rented"
            plan.skipped_clients.append(
                f"строка {row.line}: {row.fio} - телефон не разобран "
                f"({_text(row.raw.get('phone')) or 'пусто'})")
            continue

        def keep_rented(ref: dict | None = bike_ref, wanted: bool = status == "rented") -> None:
            # Аренда в CRM не заводится, но велосипед по таблице у клиента:
            # новый велосипед записывается «в аренде», а не «свободен».
            if wanted and ref is not None and not ref.get("existing_id"):
                ref["status"] = "rented"
        client_status = "blacklist" if status == "lost" else "active"
        client_ref = planned_clients.get(row.phone)
        if client_ref is None:
            existing_client = await crm.client_by_phone(row.phone)
            if existing_client is not None:
                plan.skipped_clients.append(
                    f"строка {row.line}: {row.fio} - телефон {row.phone} уже есть "
                    f"({existing_client['full_name']})")
                client_ref = {"existing_id": existing_client["id"], "phone": row.phone}
                if await crm.active_rental_of(existing_client["id"]) is not None:
                    clients_with_rental.add(row.phone)
                if status != "rented" and row.debt:
                    warn(f"{row.fio}: клиент уже есть в CRM - долг "
                         f"{row.debt:,.0f} ₽ из таблицы не записан".replace(",", " "))
            else:
                client_ref = {
                    "full_name": row.fio, "phone": row.phone,
                    "username": row.tg.lstrip("@") if row.tg.startswith("@") else None,
                    "status": client_status, "note": _client_note(row), "line": row.line,
                }
                plan.clients.append(client_ref)
                # Долг клиента, у которого аренды уже нет (полиция, невозврат):
                # без записи в журнале его карточка выглядела бы чистой.
                if status != "rented" and row.debt:
                    plan.debts.append({"phone": row.phone, "amount": row.debt,
                                       "note": f"Долг по таблице ({row.status})",
                                       "line": row.line, "fio": row.fio,
                                       "dated": row.started or row.paid_until or today})
            planned_clients[row.phone] = client_ref
        else:
            warn(f"{row.fio}: телефон {row.phone} уже встречался выше - карточка одна"
                 + (f", долг {row.debt:,.0f} ₽ не записан".replace(",", " ")
                    if status != "rented" and row.debt else ""))

        # ── аренда ──
        if status != "rented":
            continue
        if row.phone in clients_with_rental:
            plan.skipped_rentals.append(
                f"строка {row.line}: у {row.fio} уже есть идущая аренда")
            keep_rented()
            continue
        if bike_ref is None:
            warn(f"{row.fio}: велосипед не определён - аренда оформлена без велосипеда")
        elif bike_ref.get("existing_id") and (bike_ref.get("status") == "rented"
                                              or bike_ref["existing_id"] in bikes_taken):
            plan.skipped_rentals.append(
                f"строка {row.line}: велосипед уже в аренде в CRM")
            continue
        if bike_ref is not None and bike_ref.get("existing_id"):
            bikes_taken.add(bike_ref["existing_id"])
        started = row.started or row.paid_until or today
        if row.started is None:
            warn(f"{row.fio}: дата выдачи не разобрана "
                 f"({_text(row.raw.get('started')) or 'пусто'}) - взята {started:%d.%m.%Y}")
        period = row.days or 7
        price = row.price if row.price is not None else Decimal(0)
        if row.price is None:
            warn(f"{row.fio}: цена не разобрана ({_text(row.raw.get('price')) or 'пусто'})")
        if row.debt is None and isinstance(row.raw.get("debt"), str):
            warn(f"{row.fio}: долг не разобран ({_text(row.raw['debt'])})")
        paid = row.paid or Decimal(0)
        debt = row.debt or Decimal(0)
        billed_until = row.paid_until or (started + timedelta(days=period))
        # Что должен был заплатить: оплачено + долг. Если ни того, ни другого
        # в таблице нет - первый период по цене.
        charge = paid + debt if (row.paid is not None or row.debt is not None) else price
        plan.rentals.append({
            "phone": row.phone, "bike": bike_ref, "line": row.line, "fio": row.fio,
            "tariff_name": f"из таблицы: {row.tariff or f'{period} дн.'}",
            "period_days": period, "price": logic.to_money(price),
            "started_on": started, "billed_until": billed_until,
            "charge": logic.to_money(charge), "paid": logic.to_money(paid),
            "note_paid_until": row.paid_until,
        })
        clients_with_rental.add(row.phone)
    if no_model:
        shown = ", ".join(str(n) for n in no_model[:8]) + (", …" if len(no_model) > 8 else "")
        plan.warnings.append(f"модель не указана у {len(no_model)} велосипедов "
                             f"(строки {shown}) - записаны как Truck+")
    return plan


async def apply_plan(crm: Any, plan: Plan, *, by: str = "import") -> dict[str, int]:
    """Записать план. Возвращает счётчики. Порядок: велосипеды, клиенты, аренды."""
    done = {"bikes": 0, "clients": 0, "rentals": 0, "ledger": 0}
    for b in plan.bikes:
        b["id"] = await crm.create_bike(code=b["code"], model=b["model"], frame_no=b["frame_no"],
                                        motor_no=b["motor_no"], status=b["status"],
                                        note=b["note"])
        done["bikes"] += 1
    ids: dict[str, int] = {}
    for c in plan.clients:
        cid = await crm.create_client(full_name=c["full_name"], phone=c["phone"],
                                      username=c["username"], note=c["note"],
                                      source="import")
        if c["status"] != "active":
            await crm.update_client(cid, status=c["status"])
        ids[c["phone"]] = cid
        done["clients"] += 1
    for r in plan.rentals:
        client_id = ids.get(r["phone"])
        if client_id is None:
            existing = await crm.client_by_phone(r["phone"])
            if existing is None:
                continue
            client_id = existing["id"]
        bike = r["bike"] or {}
        bike_id = bike.get("id") or bike.get("existing_id")
        try:
            rental_id = await crm.create_rental(
                client_id=client_id, bike_id=bike_id, tariff_id=None,
                tariff_name=r["tariff_name"], period_days=r["period_days"],
                price=r["price"], billing="manual", started_on=r["started_on"],
                contract_no=None, created_by=by)
        except Exception as exc:                          # noqa: BLE001
            if "unique" not in type(exc).__name__.lower():
                raise
            # Уникальные индексы: одна активная аренда на клиента и на велосипед.
            plan.skipped_rentals.append(
                f"строка {r['line']}: аренда {r['fio']} не записана - "
                f"клиент или велосипед уже в аренде")
            continue
        done["rentals"] += 1
        dated = _dated(r["started_on"])
        if r["charge"]:
            await crm.charge_period(
                rental_id, client_id, period_from=r["started_on"],
                period_to=r["billed_until"], amount=-r["charge"],
                note=f"Начислено по таблице ({r['tariff_name']})", created_by=by,
                created_at=dated)
            done["ledger"] += 1
        else:
            await crm.update_rental(rental_id, billed_until=r["billed_until"])
        if r["paid"]:
            await crm.add_ledger(client_id=client_id, rental_id=rental_id, kind="payment",
                                 amount=r["paid"], method="other",
                                 note="Оплачено по таблице", created_by=by,
                                 created_at=dated)
            done["ledger"] += 1
    for d in plan.debts:
        client_id = ids.get(d["phone"])
        if client_id is None:
            continue
        await crm.add_ledger(client_id=client_id, kind="adjust", amount=-d["amount"],
                             note=d["note"], created_by=by, created_at=_dated(d["dated"]))
        done["ledger"] += 1
    return done


def report_text(plan: Plan, done: dict[str, int] | None = None) -> str:
    lines = [plan.summary(), ""]
    if done:
        lines += [f"Записано: велосипедов {done['bikes']}, клиентов {done['clients']}, "
                  f"аренд {done['rentals']}, записей журнала {done['ledger']}", ""]
    for title, items in (("Пропущенные велосипеды", plan.skipped_bikes),
                         ("Пропущенные клиенты", plan.skipped_clients),
                         ("Пропущенные аренды", plan.skipped_rentals),
                         ("Замечания", plan.warnings)):
        if items:
            lines.append(f"{title} ({len(items)}):")
            lines.extend("  - " + x for x in items)
            lines.append("")
    return "\n".join(lines).rstrip()


async def run(crm: Any, source: str | bytes | io.BytesIO, *, apply: bool,
              by: str = "import") -> tuple[Plan, dict[str, int] | None]:
    # openpyxl - чистый CPU на секунды: в потоке, чтобы панель не замирала.
    rows = await asyncio.to_thread(read_rows, source)
    plan = await build_plan(crm, rows)
    done = await apply_plan(crm, plan, by=by) if apply else None
    return plan, done


async def _main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    path, apply = argv[0], "--apply" in argv
    from ..config import _env, _int, _secret
    from ..db import Database
    from .db import CrmDB
    db = await Database.connect({
        "user": _env("POSTGRES_USER", "mybike"), "password": _secret("POSTGRES_PASSWORD"),
        "database": _env("POSTGRES_DB", "mybike"), "host": _env("POSTGRES_HOST", "postgres"),
        "port": _int("POSTGRES_PORT", "5432")})
    try:
        plan, done = await run(CrmDB(db.pool), path, apply=apply, by="import:cli")
    finally:
        await db.close()
    print(report_text(plan, done))
    if not apply:
        print("\nЭто сухой прогон. Чтобы записать: добавьте --apply")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
