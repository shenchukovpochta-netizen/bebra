"""Логика CRM без внешних зависимостей: деньги, периоды, напоминания,
проверки форм, пароли. Сюда смотрят тесты; база и Telegram - снаружи.

Модель денег. У клиента один журнал (crm.ledger): платежи со знаком «+»,
начисления, штрафы и возвраты со знаком «-». Баланс - сумма журнала,
отдельной колонки «баланс» нет намеренно: две копии одного числа
разъезжаются, а сумму журнала не подделать забытой строкой.

Модель периодов. Аренда - это тариф «цена за период» (неделя за 3 000,
месяц за 11 000). Начисление делается вперёд, в первый день периода:
billed_until - дата, с которой начинается ещё не начисленный период.
«Оплачено до» (covered_until) выводится из баланса: сколько целых
периодов покрыто деньгами сверх начисленного - или, при долге, сколько
периодов не оплачено. Дата исключительная: «оплачено до 20.09» значит,
что 20.09 наступает следующий платёж.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import math
import os
import random
import re
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .. import logic as bot_logic

# ─────────────────────────── словари ───────────────────────────

# Виды записей журнала. `bonus` - баллы: они меняют баланс, но платежом
# не считаются. Средний чек считается по `payment`, и бонус, попавший
# туда, завысил бы его - а это одно из трёх чисел парка.
KINDS = {
    "payment": "Платёж",
    "charge": "Начисление",
    "fine": "Штраф / ремонт",
    "refund": "Возврат клиенту",
    "adjust": "Корректировка",
    "bonus": "Баллы",
}
# Знак суммы по виду записи: оператор вводит число без знака, знак
# ставит система. Корректировка - единственная со свободным знаком.
KIND_SIGN = {"payment": 1, "charge": -1, "fine": -1, "refund": -1, "adjust": 0,
             "bonus": 1}

METHODS = {
    "sbp": "СБП", "cash": "Наличные", "card": "Карта",
    "transfer": "Перевод", "other": "Другое",
}

BIKE_STATUSES = {
    "new": "Новое на сборке", "available": "Свободен", "rented": "В аренде",
    "repair": "В ремонте", "maintenance": "На ТО", "reserved": "Забронирован",
    "lost": "Утерян", "sold": "Продан", "written_off": "Списан",
}
# Статусы, которые ставит оператор руками. rented - только через аренду,
# new снимает ввод в эксплуатацию: он проверяет сверку, а список - нет.
BIKE_MANUAL_STATUSES = ("available", "repair", "maintenance", "reserved", "lost", "sold",
                        "written_off")
# Операционный парк - то, что зарабатывает или может заработать. Потерянные,
# проданные и списанные в знаменатель простоя не попадают никогда.
# Новое на сборке - тоже нет: оно ещё не в обороте, и считать его простоем
# значило бы записать в убыток недособранный велосипед.
OPERATIONAL_STATUSES = ("available", "rented", "repair", "maintenance", "reserved")
# Простой: велосипед в парке, но не в аренде.
IDLE_STATUSES = ("available", "reserved", "repair", "maintenance")
# Точки выдачи. Пусто у велосипеда - «не на точке» (у клиента, в пути).
LOCATIONS = ("Павлюхина", "Адоратского")
# Цели: простой меньше десятой части парка, чек 500 ₽ в день на велосипед.
IDLE_TARGET_PERCENT = 10
CHECK_TARGET = Decimal(500)
# Узлы ремонта - фиксированный справочник (в базе crm.repair_nodes тот же
# список; здесь - для проверки формы и подписей без запроса в базу).
REPAIR_NODES = {
    "motor_wheel": "Мотор-колесо", "controller": "Контроллер", "battery": "АКБ",
    "bms": "BMS", "charger": "Зарядное устройство",
    "brake_pads": "Тормоза: колодки", "brake_disc": "Тормоза: диск",
    "brake_lever": "Тормоза: ручка", "brake_line": "Тормоза: гидролиния",
    "frame": "Рама", "fork": "Вилка / амортизация", "headset": "Рулевая колонка",
    "handlebar": "Руль", "throttle": "Ручка газа", "hall_sensors": "Датчики Холла",
    "wiring": "Проводка", "headlight": "Фара", "taillight": "Задний фонарь",
    "turn_signals": "Поворотники", "horn": "Сигнал",
    "wheel_front": "Колесо переднее", "wheel_rear": "Колесо заднее",
    "tube_tire": "Камера / покрышка", "fender_front": "Крыло переднее",
    "fender_rear": "Крыло заднее", "rack": "Багажник", "kickstand": "Подножка",
    "saddle": "Седло", "seatpost": "Подседельный штырь", "chain_guard": "Защита цепи",
    "mirrors": "Зеркала", "phone_holder": "Держатель телефона",
    "gps_tracker": "GPS-трекер", "other": "Прочее",
}

CLIENT_STATUSES = {
    "active": "Активен", "blocked": "Заблокирован", "blacklist": "Чёрный список",
}
RENTAL_STATUSES = {"active": "Идёт", "closed": "Закрыта"}
BILLING = {"auto": "по тарифу", "manual": "вручную"}
ROLES = {"admin": "Администратор", "manager": "Менеджер"}

MAX_AMOUNT = Decimal("10000000")
MAX_PERIOD_DAYS = 366
NAME_LIMIT = 120
NOTE_LIMIT = 2000
CODE_LIMIT = 40

# ─────────────────────────── деньги ───────────────────────────

CENT = Decimal("0.01")


def to_money(value: Any) -> Decimal:
    """Любое число -> Decimal с двумя знаками. Не для пользовательского ввода."""
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def money(value: Any) -> str:
    """Сумма для человека: 3000 -> «3 000 ₽», -428.5 -> «−428,50 ₽».

    Целые - без копеек: цены проката круглые, и «3 000,00» только шумит.
    Пробел неразрывный: перенос строки посреди «11 000» читается как опечатка.
    """
    if value is None:
        return "—"
    amount = to_money(value)
    sign = "−" if amount < 0 else ""
    amount = abs(amount)
    whole = int(amount)
    cents = int((amount - whole) * 100)
    text = f"{whole:,}".replace(",", " ")
    if cents:
        text += f",{cents:02d}"
    return f"{sign}{text} ₽"


def money_signed(value: Any) -> str:
    """Как money, но с явным плюсом у прихода - для журнала."""
    amount = to_money(value)
    return ("+" if amount > 0 else "") + money(amount)


def parse_money(raw: Any) -> Decimal | None:
    """«3 000», «3000,50», «3000.5 ₽» -> Decimal. None, если это не сумма.

    Минус допускается: корректировка бывает в обе стороны. Знак у остальных
    видов записей всё равно переопределит signed_amount.
    """
    text = str(raw or "").strip().replace(" ", "").replace(" ", "")
    text = text.replace("₽", "").replace("р.", "").replace("руб", "").replace(",", ".")
    if not re.fullmatch(r"-?\d{1,9}(\.\d{1,2})?", text):
        return None
    try:
        return Decimal(text).quantize(CENT)
    except InvalidOperation:      # pragma: no cover - формат уже проверен
        return None


def signed_amount(kind: str, amount: Decimal) -> Decimal:
    """Знак суммы по виду записи: платёж всегда +, начисление всегда -."""
    sign = KIND_SIGN.get(kind, 0)
    if sign == 0:
        return amount
    return abs(amount) * sign


def balance(rows: Iterable[dict]) -> Decimal:
    return to_money(sum((to_money(r.get("amount")) for r in rows), Decimal(0)))


# ─────────────────────────── периоды ───────────────────────────

def covered_until(billed_until: date, bal: Any, price: Any,
                  period_days: int) -> date:
    """До какой даты аренда оплачена.

    Начисления идут вперёд, поэтому baseline - billed_until: если баланс
    ровно ноль, всё начисленное оплачено и следующий платёж - в billed_until.
    Положительный баланс покрывает целые периоды вперёд (3 000 на балансе
    при неделе за 3 000 - это ещё неделя), отрицательный - отнимает:
    долг в один рубль уже означает, что текущий период не оплачен.
    """
    bal = to_money(bal)
    price = to_money(price)
    period = max(int(period_days or 1), 1)
    if price <= 0:
        # Бесплатная аренда (тест-драйв, подменный велосипед): оплачена
        # всегда, пока нет долга по штрафам.
        return billed_until if bal >= 0 else billed_until - timedelta(days=period)
    if bal >= 0:
        periods = int(bal // price)
        return billed_until + timedelta(days=periods * period)
    debt = -bal
    # ceil без float. Decimal делит нацело с усечением к нулю, поэтому
    # трюк -(-a // b) здесь не работает: остаток проверяется явно.
    periods = int(debt // price) + (1 if debt % price else 0)
    return billed_until - timedelta(days=periods * period)


def days_left(until: date | None, *, today: date | None = None) -> int | None:
    """Сколько дней до даты следующего платежа. Отрицательное - просрочка."""
    if until is None:
        return None
    return (until - (today or date.today())).days


def due_periods(billed_until: date, period_days: int, *,
                today: date) -> list[tuple[date, date]]:
    """Периоды, которые пора начислить: от billed_until до сегодня включительно.

    Список, а не один период: бот мог не работать несколько дней, и проход
    должен догнать пропущенное, а не молча начислить только последний.
    Аренда, оформленная сегодня, получает первый период сразу.
    """
    period = max(int(period_days or 1), 1)
    out: list[tuple[date, date]] = []
    start = billed_until
    # Верхняя граница: не больше года периодов за раз - защита от
    # billed_until из прошлого века при ошибке ввода.
    while start <= today and len(out) < MAX_PERIOD_DAYS:
        end = start + timedelta(days=period)
        out.append((start, end))
        start = end
    return out


def period_label(period_from: date | None, period_to: date | None) -> str:
    """«14.09 – 20.09.2026»: конец периода - последний оплаченный день."""
    if not period_from or not period_to:
        return ""
    last = period_to - timedelta(days=1)
    if last <= period_from:
        return period_from.strftime("%d.%m.%Y")
    return f"{period_from.strftime('%d.%m')} – {last.strftime('%d.%m.%Y')}"


# ─────────────────────────── напоминания ───────────────────────────

REMIND_SOON, REMIND_DUE, REMIND_OVERDUE = "soon", "due", "overdue"
# Просрочка напоминается не каждый день: на первый, третий, седьмой день
# и дальше раз в неделю. Ежедневное «вы должны» клиент отключает вместе
# с ботом, и связь с ним теряется совсем.
OVERDUE_DAYS = (1, 3, 7)


def reminder_kind(left: int | None, *, before_days: int) -> str | None:
    """Какое напоминание положено сегодня при таком числе дней до платежа."""
    if left is None:
        return None
    if left == before_days and before_days > 0:
        return REMIND_SOON
    if left == 0:
        return REMIND_DUE
    if left < 0:
        overdue = -left
        if overdue in OVERDUE_DAYS or (overdue > 7 and overdue % 7 == 0):
            return REMIND_OVERDUE
    return None


def reminder_due(rental: dict, *, before_days: int, today: date) -> str | None:
    """Напоминание по аренде на сегодня с учётом уже отправленного.

    rental - строка crm.rentals, дополненная balance. Одно напоминание
    в день на аренду: повторный проход (каждые 15 минут) видит notified_on.
    """
    if rental.get("status") != "active":
        return None
    if rental.get("notified_on") == today:
        return None
    until = covered_until(rental["billed_until"], rental.get("balance", 0),
                          rental["price"], rental["period_days"])
    return reminder_kind(days_left(until, today=today), before_days=before_days)


def digest(rentals: Iterable[dict], *, today: date, before_days: int) -> str:
    """Сводка оператору: кто в долгу и у кого платёж на днях.

    Только те, кому пора: если строк нет, возвращается пустая строка,
    и сводка не отправляется - молчание означает «всё оплачено».
    """
    debtors: list[str] = []
    soon: list[str] = []
    for r in rentals:
        if r.get("status") != "active":
            continue
        until = covered_until(r["billed_until"], r.get("balance", 0),
                              r["price"], r["period_days"])
        left = days_left(until, today=today) or 0
        # Сводка уходит с parse_mode=HTML: имя из таблицы импорта или
        # из бота может содержать «<» - без экранирования Telegram отвергает
        # всё сообщение, и сводки не видит никто.
        who = html.escape(r.get("full_name") or f"клиент #{r.get('client_id')}", quote=False)
        bike = html.escape(r.get("bike_code") or "", quote=False)
        tail = f" · {bike}" if bike else ""
        if left < 0:
            debtors.append(
                f"⚠️ {who}{tail} — долг {money(-to_money(r.get('balance', 0)))}, "
                f"не оплачено с {until.strftime('%d.%m')} ({-left} дн.)")
        elif left <= before_days:
            when = "сегодня" if left == 0 else f"{until.strftime('%d.%m')} ({left} дн.)"
            soon.append(f"⏳ {who}{tail} — платёж {when}, {money(r['price'])}")
    lines: list[str] = []
    if debtors:
        lines.append("<b>Долги</b>")
        lines.extend(debtors)
    if soon:
        if lines:
            lines.append("")
        lines.append("<b>Платёж на днях</b>")
        lines.extend(soon)
    return "\n".join(lines)


# ─────────────────────────── проверки форм ───────────────────────────

@dataclass(frozen=True)
class Check:
    ok: bool
    value: Any = None
    error: str = ""


def check_amount(raw: Any, *, allow_negative: bool = False) -> Check:
    amount = parse_money(raw)
    if amount is None:
        return Check(False, error="Сумма: число, например 3000 или 3000,50.")
    if amount == 0:
        return Check(False, error="Сумма не может быть нулём.")
    if amount < 0 and not allow_negative:
        return Check(False, error="Сумма должна быть положительной.")
    if abs(amount) > MAX_AMOUNT:
        return Check(False, error="Сумма слишком большая.")
    return Check(True, amount)


def check_name(raw: Any, *, what: str = "Название") -> Check:
    text = " ".join(str(raw or "").split())
    if not text:
        return Check(False, error=f"{what}: заполните поле.")
    if len(text) > NAME_LIMIT:
        return Check(False, error=f"{what}: не длиннее {NAME_LIMIT} символов.")
    if "<" in text or ">" in text:
        return Check(False, error=f"{what}: без угловых скобок.")
    if text[0] in "=+-@":
        return Check(False, error=f"{what}: не может начинаться с {text[0]}.")
    return Check(True, text)


def check_code(raw: Any) -> Check:
    """Инвентарный номер: буквы, цифры, дефис, точка, пробел."""
    text = " ".join(str(raw or "").split()).upper()
    if not text:
        return Check(False, error="Инвентарный номер: заполните поле.")
    if len(text) > CODE_LIMIT or not re.fullmatch(r"[\w .\-/№]+", text):
        return Check(False, error="Инвентарный номер: буквы, цифры, дефис; "
                                  f"не длиннее {CODE_LIMIT} символов.")
    return Check(True, text)


def check_note(raw: Any) -> Check:
    text = str(raw or "").strip()
    if len(text) > NOTE_LIMIT:
        return Check(False, error=f"Заметка: не длиннее {NOTE_LIMIT} символов.")
    return Check(True, text or None)


def check_period(raw: Any) -> Check:
    try:
        days = int(str(raw or "").strip())
    except ValueError:
        return Check(False, error="Период: целое число дней.")
    if not 1 <= days <= MAX_PERIOD_DAYS:
        return Check(False, error=f"Период: от 1 до {MAX_PERIOD_DAYS} дней.")
    return Check(True, days)


def check_date(raw: Any, *, default: date | None = None) -> Check:
    """Дата из формы: ГГГГ-ММ-ДД (input type=date) или ДД.ММ.ГГГГ."""
    text = str(raw or "").strip()
    if not text:
        if default is not None:
            return Check(True, default)
        return Check(False, error="Дата: заполните поле.")
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            from datetime import datetime
            return Check(True, datetime.strptime(text, fmt).date())
        except ValueError:
            continue
    return Check(False, error="Дата: в виде ДД.ММ.ГГГГ.")


def check_choice(raw: Any, choices: dict[str, str] | Iterable[str],
                 *, what: str = "Значение") -> Check:
    value = str(raw or "").strip()
    if value not in set(choices):
        return Check(False, error=f"{what}: недопустимое значение.")
    return Check(True, value)


def check_login(raw: Any) -> Check:
    text = str(raw or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,32}", text):
        return Check(False, error="Логин: 3–32 символа, латиница, цифры, точка, дефис.")
    return Check(True, text)


def check_password(raw: Any) -> Check:
    text = str(raw or "")
    if len(text) < 8:
        return Check(False, error="Пароль: не короче 8 символов.")
    if len(text) > 128:
        return Check(False, error="Пароль: не длиннее 128 символов.")
    return Check(True, text)


# ─────────────────────────── пароли ───────────────────────────
# scrypt из стандартной библиотеки: отдельная зависимость ради одной
# функции не нужна, а sha256 без растяжения для паролей не годится.

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest_bytes = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                  n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${salt.hex()}${digest_bytes.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, salt_hex, digest_hex = (stored or "").split("$")
        if algo != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    candidate = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                               n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return hmac.compare_digest(candidate.hex(), digest_hex)


def generate_password(length: int = 14) -> str:
    """Пароль первого администратора: без похожих друг на друга символов."""
    import secrets
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ─────────────────────────── кабинет ───────────────────────────

def rental_summary(rental: dict | None, bal: Any, *, today: date) -> dict[str, Any]:
    """Числа для экрана кабинета и карточки клиента - одним словарём.

    Один расчёт на оба места: у клиента в Telegram и у оператора в панели
    обязана быть одна и та же дата «оплачено до».
    """
    bal = to_money(bal)
    if rental is None or rental.get("status") != "active":
        debt = max(-bal, Decimal(0))
        return {"active": False, "balance": bal, "debt": debt, "due": debt,
                "covered_until": None, "days_left": None, "overdue": bal < 0}
    until = covered_until(rental["billed_until"], bal, rental["price"],
                          rental["period_days"])
    left = days_left(until, today=today) or 0
    debt = max(-bal, Decimal(0))
    price = to_money(rental["price"])
    # due - что внести прямо сейчас. У аренды с ручным начислением
    # (заведена ботом) просроченный период ещё не начислен, и долг
    # в журнале нулевой - но платить за новый срок уже пора.
    due = debt if debt > 0 else (price if left < 0 else Decimal(0))
    return {
        "active": True, "balance": bal, "debt": debt, "due": due,
        "covered_until": until, "days_left": left, "overdue": left < 0,
        "price": price,
        "period_days": int(rental["period_days"]),
        "tariff_name": rental.get("tariff_name") or "",
        "bike": " ".join(x for x in (rental.get("bike_model"),
                                      rental.get("bike_code")) if x),
    }


def topup_hint(summary: dict) -> Decimal:
    """Сколько предложить к оплате: что пора внести, иначе цена периода."""
    if summary.get("due"):
        return to_money(summary["due"])
    return to_money(summary.get("price") or 0)


# ─────────────────────────── синхронизация с ботом ───────────────────────────

def first_amount(text: Any) -> Decimal | None:
    """Сумма из свободного текста оператора: первое число, пробелы внутри
    числа допустимы («3 000 qr 14.09» -> 3000). Склеивать все цифры
    строки нельзя: «3000 qr 14.09» превращалось бы в 30 001 409."""
    # «3 000» - одно число (группы по три цифры), «3000 2 недели» - два:
    # берётся только первое.
    m = re.search(r"\d{1,3}(?:[ \u00a0]\d{3})+(?!\d)|\d+", str(text or ""))
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group())
    return to_money(int(digits)) if digits else None


def rental_from_issue(issue: dict | None, rent_from: date | None,
                      rent_until: date | None, *, today: date) -> dict[str, Any]:
    """Условия аренды из формы выдачи оператора (issue_data бота).

    Цена - число из строки «3000 qr», период - длина срока «03.08 - 10.08».
    Срока нет - берётся неделя: это самый частый тариф, и оператор поправит
    в панели. billing manual: начисления такой аренде делает не календарь,
    а события бота (выдача, продление).
    """
    issue = issue or {}
    price = first_amount(issue.get("rent_price")) or Decimal(0)
    start = rent_from or today
    if rent_until and rent_until > start:
        period = (rent_until - start).days
    else:
        period = 7
    return {
        "tariff_name": f"из формы выдачи: {issue.get('rent_term') or 'срок не указан'}",
        "period_days": min(max(period, 1), MAX_PERIOD_DAYS),
        "price": price,
        "billing": "manual",
        "started_on": start,
        "bike_model": str(issue.get("bike_model") or "").strip(),
        "frame_no": str(issue.get("vin_frame") or "").strip() or None,
        "motor_no": str(issue.get("vin_motor") or "").strip() or None,
    }


def bike_code_from_frame(frame_no: str | None, model: str | None) -> str:
    """Инвентарный номер для велосипеда, заведённого ботом: хвост номера
    рамы. Оператор переименует в панели, если у него своя нумерация."""
    tail = re.sub(r"[^\w]", "", str(frame_no or ""))[-6:].upper()
    if tail:
        return f"АВТО-{tail}"
    stem = re.sub(r"[^\w]", "", str(model or ""))[:8].upper() or "BIKE"
    return f"АВТО-{stem}"


# ─────────────────────────── метрики парка ───────────────────────────

def amortization_month(bike: dict) -> Decimal | None:
    """Сколько велосипед «съедает» в месяц: рама по сроку службы за вычетом
    остаточной стоимости плюс АКБ по своей цене и своему сроку. None -
    цена покупки не задана, считать нечего."""
    price = bike.get("purchase_price")
    if price is None:
        return None
    months = int(bike.get("service_months") or 24)
    residual = to_money(bike.get("residual_price") or 0)
    frame = max(to_money(price) - residual, Decimal(0)) / max(months, 1)
    battery = Decimal(0)
    if bike.get("battery_price"):
        battery = (to_money(bike["battery_price"]) * int(bike.get("battery_count") or 0)
                   / max(int(bike.get("battery_service_months") or 15), 1))
    return to_money(frame + battery)


def amortization_total(bikes: Iterable[dict],
                       batteries: Iterable[dict] | None = None) -> Decimal:
    """Отложить на обновление парка в этом месяце.

    Батареи, заведённые поштучно, считаются по себе, а у велосипедов тогда
    берётся только рама: иначе одна и та же батарея попала бы в сумму
    дважды - счётчиком у велосипеда и своей карточкой.
    """
    batteries = list(batteries or [])
    own = {int(b["bike_id"]) for b in batteries if b.get("bike_id")}
    total = Decimal(0)
    for b in bikes:
        if b.get("status") not in OPERATIONAL_STATUSES:
            continue
        if b.get("id") is not None and int(b["id"]) in own:
            total += frame_amortization(b) or Decimal(0)
        else:
            total += amortization_month(b) or Decimal(0)
    for battery in batteries:
        if battery.get("status") in BATTERY_OPERATIONAL:
            total += battery_amortization(battery) or Decimal(0)
    return to_money(total)


def frame_amortization(bike: dict) -> Decimal | None:
    """Амортизация только рамы, без АКБ: батареи посчитаны отдельно."""
    price = bike.get("purchase_price")
    if price is None:
        return None
    months = int(bike.get("service_months") or 24)
    residual = to_money(bike.get("residual_price") or 0)
    return to_money(max(to_money(price) - residual, Decimal(0)) / max(months, 1))


def days_by_status(log: Iterable[dict], since: datetime, until: datetime) -> dict[str, Decimal]:
    """Велосипеде-дни по статусам за период по журналу статусов.

    log - записи (bike_id, to_status, changed_at) в любом порядке; интервал
    статуса длится до следующей записи того же велосипеда или до until.
    Та же арифметика, что в SQL CrmDB.bike_days_by_status - на ней
    держатся простой и средний чек, поэтому она есть и в чистом виде.
    """
    by_bike: dict[int, list[dict]] = {}
    for r in log:
        by_bike.setdefault(r["bike_id"], []).append(r)
    out: dict[str, Decimal] = {}
    for rows in by_bike.values():
        rows.sort(key=lambda r: r["changed_at"])
        for i, r in enumerate(rows):
            start = max(r["changed_at"], since)
            end = min(rows[i + 1]["changed_at"] if i + 1 < len(rows) else until, until)
            if end <= start:
                continue
            days = Decimal((end - start).total_seconds()) / Decimal(86400)
            out[r["to_status"]] = out.get(r["to_status"], Decimal(0)) + days
    return out


def fleet_metrics(days: dict[str, Any], revenue: Any) -> dict[str, Any]:
    """Три числа за период из велосипеде-дней по статусам и арендной выручки.

    idle_percent - доля дней простоя в днях операционного парка;
    avg_check - выручка на один день аренды. None - данных нет.
    """
    days = {k: Decimal(str(v)) for k, v in days.items()}
    operational = sum((days.get(s, Decimal(0)) for s in OPERATIONAL_STATUSES), Decimal(0))
    idle = sum((days.get(s, Decimal(0)) for s in IDLE_STATUSES), Decimal(0))
    rented = days.get("rented", Decimal(0))
    idle_percent = (float(round(100 * idle / operational, 1)) if operational else None)
    # Чек имеет смысл от одного полного велосипеде-дня: деление на минуты
    # первой аренды давало бы «108 миллионов в день».
    avg_check = to_money(Decimal(str(revenue)) / rented) if rented >= 1 else None
    return {
        "operational_days": operational, "idle_days": idle, "rented_days": rented,
        "revenue": to_money(Decimal(str(revenue))),
        "idle_percent": idle_percent, "avg_check": avg_check,
        "idle_ok": idle_percent is not None and idle_percent < IDLE_TARGET_PERCENT,
        "check_ok": avg_check is not None and avg_check >= CHECK_TARGET,
        "idle_breakdown": {s: days.get(s, Decimal(0)) for s in IDLE_STATUSES},
    }


# ─────────────────────────── быстрая выдача ───────────────────────────
#
# Мастер выдачи в панели: телефон -> тариф и модель -> конкретный
# велосипед -> оплата и аренда. Здесь то, что считается без базы: цена
# за день против цели среднего чека, свободные по моделям и точкам,
# простой велосипеда, состояние клиента в боте, сколько взять при выдаче.

def per_day(price: Any, period_days: Any) -> Decimal:
    """Цена тарифа за день: сравнивать тарифы разной длины и цель 500 ₽."""
    days = int(period_days or 0)
    if days <= 0:
        return Decimal(0)
    return (to_money(price) / days).quantize(CENT, rounding=ROUND_HALF_UP)


def tariffs_for_model(tariffs: Iterable[dict], model: Any) -> list[dict]:
    """Тарифы для модели: её собственные, а если их нет - общие.

    Цена зависит от модели: Monster Truck+ и Kugoo V3 Pro стоят
    по-разному. Тариф без модели остаётся запасным - он работает, пока
    у модели нет своей цены.
    """
    rows = [dict(t) for t in tariffs]
    name = str(model or "").strip()
    own = [t for t in rows if str(t.get("model") or "").strip() == name and name]
    return own or [t for t in rows if not str(t.get("model") or "").strip()]


def match_tariff(tariffs: Iterable[dict], tariff: Mapping[str, Any] | None,
                 model: Any) -> dict | None:
    """Тот же срок, но по цене выбранной модели.

    Оператор выбирает тариф и модель на одном экране, и модель он может
    сменить последней. Подставлять чужую цену нельзя, отправлять его
    на шаг назад - грубо: берём тариф того же срока у нужной модели.
    """
    if tariff is None:
        return None
    rows = tariffs_for_model(tariffs, model)
    same = [t for t in rows if int(t.get("id") or 0) == int(tariff.get("id") or 0)]
    if same:
        return same[0]
    days = int(tariff.get("period_days") or 0)
    return next((t for t in rows if int(t.get("period_days") or 0) == days), None)


def tariff_tiles(tariffs: Iterable[dict]) -> list[dict]:
    """Плитки тарифов для выбора, от короткого к длинному.

    per_day - цена за день, hits_target - не ниже цели среднего чека;
    saving - сколько клиент экономит против самого короткого тарифа
    за тот же срок («неделя: −1 190 ₽»). Оператору видно, какой тариф
    продавать: длинный и с экономией для клиента, но не ниже цели.
    """
    rows = [dict(t) for t in tariffs
            if t.get("active", True) and int(t.get("period_days") or 0) > 0]
    if not rows:
        return []
    order = sorted(rows, key=lambda t: (int(t["period_days"]), to_money(t["price"])))
    base_price, base_days = to_money(order[0]["price"]), int(order[0]["period_days"])
    out = []
    for t in order:
        day = per_day(t["price"], t["period_days"])
        # Выгода считается от цен, а не от округлённой цены за день:
        # иначе «две недели» показывали бы 599,98 ₽ вместо 600 ₽.
        saving = (base_price * int(t["period_days"]) / base_days
                  - to_money(t["price"])).quantize(CENT, rounding=ROUND_HALF_UP)
        out.append({**t, "per_day": day, "saving": saving if saving > 0 else Decimal(0),
                    "hits_target": day >= CHECK_TARGET})
    return out


def model_availability(bikes: Iterable[dict]) -> list[dict]:
    """Свободные велосипеды по моделям и точкам: {model, free, by_location}.

    Модели с большим запасом идут первыми: выдавать надо то, чего много,
    а не последний экземпляр редкой модели.
    """
    by_model: dict[str, dict] = {}
    for b in bikes:
        if b.get("status") != "available":
            continue
        model = b.get("model") or "—"
        row = by_model.setdefault(model, {"model": model, "free": 0, "by_location": {}})
        row["free"] += 1
        place = b.get("location") or "не на точке"
        row["by_location"][place] = row["by_location"].get(place, 0) + 1
    return sorted(by_model.values(), key=lambda r: (-r["free"], r["model"]))


def idle_days(since: datetime | None, *, now: datetime) -> int | None:
    """Сколько дней велосипед стоит в текущем статусе. None - журнала нет."""
    if since is None:
        return None
    return max((now - since).days, 0)


def bot_client_state(user: dict | None) -> dict[str, str]:
    """Что о клиенте знает бот - код и подпись для мастера выдачи.

    none - не в боте; registering - анкета не закончена; pending -
    заявка на проверке; approved - одобрен, договор не подписан;
    signed - договор подписан; renting - велосипед на руках по акту бота.
    """
    if not user:
        return {"code": "none", "label": "не в боте"}
    if user.get("status") != bot_logic.ST_APPROVED:
        if user.get("state") == bot_logic.PENDING:
            return {"code": "pending", "label": "заявка в боте на проверке"}
        return {"code": "registering", "label": "регистрация в боте не завершена"}
    number = user.get("contract_no") or "—"
    if user.get("contract_status") != bot_logic.CT_SIGNED:
        return {"code": "approved", "label": "одобрен в боте, договор ещё не подписан"}
    if user.get("act_in_signed_at") and not user.get("act_out_signed_at"):
        return {"code": "renting", "label": f"по акту бота велосипед на руках, договор № {number}"}
    return {"code": "signed", "label": f"договор № {number} подписан в боте"}


def issue_payment_default(price: Any, balance: Any) -> Decimal:
    """Сколько взять при выдаче: цена первого периода за вычетом того, что
    уже лежит на балансе. Долг сюда не добавляется: он виден оператору
    отдельной строкой и закрывается своим платежом."""
    need = to_money(price) - max(to_money(balance), Decimal(0))
    return max(need, Decimal(0))


# ─────────────────────────── сводка оператора ───────────────────────────
#
# «Истекает аренда»: кто на днях платит или уже просрочил, что клиент
# сказал по телефону (продлит / сдаёт), кого отложили до завтра. Отсюда же
# прогноз свободных велосипедов и потери в рублях: каждый день простоя -
# это невыданный день по цели среднего чека.

INTENTS = {"renew": "продлит", "return": "сдаёт"}


def intent_state(rental: dict, summary: dict, *, today: date) -> dict[str, Any]:
    """Намерение клиента, если оно ещё про текущий срок.

    Намерение записывается вместе с датой «оплачено до» на тот момент:
    сдвинулась дата - клиент заплатил или срок пересчитан, и старое «продлит»
    больше ничего не значит. Отложенная строка (snooze_until в будущем)
    прячется из виджета до этой даты.
    """
    intent = rental.get("intent")
    if intent not in INTENTS or rental.get("intent_until") != summary.get("covered_until"):
        intent = None
    snooze = rental.get("snooze_until")
    return {"intent": intent, "label": INTENTS.get(intent, ""),
            "snoozed": bool(snooze and snooze > today)}


def expiring(rows: Iterable[dict], *, today: date, before_days: int) -> list[dict]:
    """Строки виджета «истекает аренда»: у кого платёж в ближайшие
    before_days дней или просрочка. Просроченные первыми. Отложенные
    не показываются, намерение - только актуальное."""
    out = []
    for r in rows:
        s = r.get("summary") or {}
        left = s.get("days_left")
        if not s.get("active") or left is None or left > before_days:
            continue
        st = intent_state(r, s, today=today)
        if st["snoozed"]:
            continue
        out.append({**r, "intent": st["intent"], "intent_label": st["label"]})
    out.sort(key=lambda r: (r["summary"]["days_left"], r["id"]))
    return out


def fleet_losses(metrics: dict, *, rate: Any = CHECK_TARGET) -> dict[str, Any]:
    """Потери в рублях за период по цели среднего чека.

    Потенциал - каждый день операционного парка по цели; заработано -
    выручка периода; потери - дни простоя по статусам × цель; КПД - доля
    потенциала, ставшая деньгами. Цель, а не фактический чек: потери должны
    показывать расстояние до цели, а не подстраиваться под слабый месяц.
    """
    # Целые рубли: потери - оценка по цели, копейки в ней выглядят
    # точностью, которой нет.
    rate = to_money(rate)
    whole = Decimal(1)
    by_status = {s: (Decimal(str(d)) * rate).quantize(whole, rounding=ROUND_HALF_UP)
                 for s, d in (metrics.get("idle_breakdown") or {}).items()}
    lost = sum(by_status.values(), Decimal(0))
    potential = (Decimal(str(metrics.get("operational_days") or 0)) * rate).quantize(
        whole, rounding=ROUND_HALF_UP)
    earned = to_money(metrics.get("revenue") or 0)
    efficiency = float(round(100 * earned / potential, 1)) if potential else None
    return {"rate": rate, "potential": potential, "earned": earned, "lost": lost,
            "by_status": by_status, "efficiency_percent": efficiency}


def loss_per_day(bikes_by_status: dict[str, int], *,
                 rate: Any = CHECK_TARGET) -> dict[str, Any]:
    """Сколько парк теряет прямо сейчас за день: простаивающие × цель чека."""
    rate = to_money(rate)
    by_status = {s: int(bikes_by_status.get(s, 0)) for s in IDLE_STATUSES}
    idle = sum(by_status.values())
    return {"idle": idle, "by_status": by_status,
            "amount": (idle * rate).quantize(Decimal(1), rounding=ROUND_HALF_UP)}


# ─────────────────────────── пробег ───────────────────────────
#
# Одометр велосипеда: число на дисплее, которое оператор переписывает
# при выдаче и при возврате. Разница - накат за аренду: по нему видно,
# кто возит по 60 км в день, а кто поставил велосипед во дворе.

MAX_MILEAGE_KM = 300_000


def check_mileage(raw: Any, *, current: Any = None, required: bool = True) -> Check:
    """Пробег с одометра, в целых километрах.

    Назад одометр не крутится: значение меньше прежнего - это либо опечатка
    в цифрах, либо перепутанный велосипед. Принять молча значит испортить
    и историю велосипеда, и «накатал» у аренды, поэтому такое отклоняется.
    """
    text = re.sub(r"[\s\u00a0]", "", str(raw or ""))
    if not text:
        return (Check(False, error="Пробег: число километров с одометра.")
                if required else Check(True, None))
    if not text.isdigit():
        return Check(False, error="Пробег: целое число километров, например 4266.")
    km = int(text)
    if km > MAX_MILEAGE_KM:
        return Check(False, error=f"Пробег: не больше {MAX_MILEAGE_KM} км.")
    if current is not None and km < int(current):
        return Check(False, error=f"Пробег меньше прежнего ({int(current)} км) - "
                                  f"проверьте номер велосипеда и цифры.")
    return Check(True, km)


def ridden(rental: dict) -> int | None:
    """Накат за аренду, км. None - одного из концов нет: аренда идёт
    или пробег не записали."""
    start, end = rental.get("mileage_start"), rental.get("mileage_end")
    if start is None or end is None:
        return None
    return max(int(end) - int(start), 0)


def ridden_per_day(rental: dict, *, days: int | None = None) -> int | None:
    """Сколько накатывали в день за аренду. None - считать не из чего."""
    km = ridden(rental)
    if km is None:
        return None
    if days is None:
        start, end = rental.get("started_on"), rental.get("closed_on")
        days = (end - start).days if start and end else None
    if not days or days <= 0:
        return None
    return int(round(km / days))


# ─────────────────────────── доступы сотрудников ───────────────────────────
#
# У каждого сотрудника профиль доступа: матрица «раздел -> смотреть/менять»
# плюс отдельные действия. Разделы совпадают с пунктами меню панели, чтобы
# оператор видел ровно то, что ему разрешили, а не пустые страницы.

SECTIONS: dict[str, str] = {
    "dashboard": "Сводка",
    "issue": "Быстрая выдача",
    "clients": "Клиенты",
    "rentals": "Аренды",
    "bikes": "Парк",
    "batteries": "Батареи",
    "trackers": "Трекеры и карта парка",
    "cash": "Касса и банк",
    "mailing": "Рассылки и шаблоны",
    "service": "Сервис: наряды и виды работ",
    "inventory": "Склад: запчасти, приходы, заказы",
    "claims": "Заявки на зачисление",
    "finance": "Финансы",
    "tariffs": "Тарифы",
    "reports": "Отчёты",
    "import": "Импорт таблицы",
    "staff": "Сотрудники и доступы",
    "settings": "Настройки: реквизиты и документы",
}

# Действия, которые не сводятся к разделу: менеджер выдаёт велосипеды
# и видит карточку клиента, но правку журнала и паспортные документы
# ему открывают отдельно.
ACTIONS: dict[str, str] = {
    "money_edit": "Записи в журнал руками: платёж, штраф, возврат, корректировка",
    "client_docs": "Паспортные документы клиента: скачивание подписанного договора",
}

LEVELS: dict[str, str] = {"": "нет доступа", "view": "смотреть", "edit": "смотреть и менять"}
LEVEL_ORDER = ("", "view", "edit")

# Путь -> раздел. Проверяется по префиксу, поэтому «/clients/7/ledger»
# и «/clients.csv» попадают в «clients» без отдельной строки на каждый
# маршрут. Точка в разделителях обязательна: без неё выгрузка CSV
# оказалась бы вне раздела и открытой всем.
SECTION_PATHS: tuple[tuple[str, str], ...] = (
    ("/issue", "issue"),
    ("/clients", "clients"),
    # Журнал подписаний - тот же раздел, что и клиенты: подписывает
    # документы тот, кто ведёт клиента. Страница /sign/<токен> в список
    # не входит: она открыта клиенту и стража раздела не знает.
    ("/signings", "clients"),
    ("/rentals", "rentals"),
    ("/bikes", "bikes"),
    ("/batteries", "batteries"),
    ("/map", "trackers"),
    ("/trackers", "trackers"),
    ("/stock-takes", "bikes"),
    ("/service", "service"),
    ("/orders", "service"),
    ("/work-types", "service"),
    ("/parts", "inventory"),
    ("/suppliers", "inventory"),
    ("/part-orders", "inventory"),
    ("/claims", "claims"),
    ("/finance", "finance"),
    ("/billing", "finance"),
    ("/payments", "finance"),
    ("/cash", "cash"),
    ("/bank", "cash"),
    ("/mailing", "mailing"),
    # После /finance: home_for берёт первый путь раздела, а /plan - это
    # форма на сводке, открывать её как страницу нечего.
    ("/plan", "finance"),
    ("/assets", "finance"),
    ("/tariffs", "tariffs"),
    ("/reports", "reports"),
    ("/import", "import"),
    ("/staff", "staff"),
    ("/profiles", "staff"),
    ("/company", "settings"),
    ("/notices", "settings"),
    ("/intake", "settings"),
    ("/documents", "settings"),
    ("/locations", "settings"),
    ("/models", "settings"),
)


def section_for(path: str) -> str | None:
    """Раздел, к которому относится адрес. None - общий (вход, свой пароль)."""
    if path == "/":
        return "dashboard"
    for prefix, code in SECTION_PATHS:
        if path == prefix or path.startswith((prefix + "/", prefix + ".")):
            return code
    return None


def normalize_perms(raw: Any) -> dict[str, Any]:
    """Матрица прав из формы или из базы - к одному виду.

    Неизвестные разделы и действия отбрасываются: профиль старой версии
    не должен открывать раздел, которого уже нет, и наоборот.
    """
    if isinstance(raw, str):                     # jsonb без кодека приходит строкой
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    data = raw if isinstance(raw, dict) else {}
    sections_raw = data.get("sections") if isinstance(data.get("sections"), dict) else {}
    actions_raw = data.get("actions") if isinstance(data.get("actions"), dict) else {}
    sections = {code: str(sections_raw.get(code) or "")
                for code in SECTIONS if str(sections_raw.get(code) or "") in ("view", "edit")}
    actions = {code: True for code in ACTIONS if actions_raw.get(code)}
    return {"sections": sections, "actions": actions}


def perms_of(staff: dict | None) -> dict[str, Any]:
    return normalize_perms((staff or {}).get("perms"))


def section_level(staff: dict | None, code: str) -> str:
    return perms_of(staff)["sections"].get(code, "")


def can_view(staff: dict | None, code: str) -> bool:
    return section_level(staff, code) in ("view", "edit")


def can_edit(staff: dict | None, code: str) -> bool:
    return section_level(staff, code) == "edit"


def can_act(staff: dict | None, action: str) -> bool:
    return bool(perms_of(staff)["actions"].get(action))


def visible_sections(staff: dict | None) -> list[str]:
    """Разделы, которые показывать в меню, в порядке SECTIONS."""
    return [code for code in SECTIONS if can_view(staff, code)]


def home_for(staff: dict | None) -> str:
    """Куда вести после входа. Профиль без сводки не должен упираться
    в «нет доступа» на первом же экране."""
    if can_view(staff, "dashboard"):
        return "/"
    first = next((code for code in visible_sections(staff) if code != "dashboard"), None)
    if first is None:
        return "/me"
    return next(path for path, code in SECTION_PATHS if code == first)


BUILT_IN_PROFILES: tuple[tuple[str, str, dict[str, Any], bool], ...] = (
    ("owner", "Владелец",
     {"sections": dict.fromkeys(SECTIONS, "edit"),
      "actions": dict.fromkeys(ACTIONS, True)}, True),
    ("manager", "Менеджер",
     {"sections": {"dashboard": "view", "issue": "edit", "clients": "edit",
                   "rentals": "edit", "bikes": "view", "service": "view",
                   "claims": "edit", "finance": "view", "tariffs": "view",
                   "reports": "view", "inventory": "view", "batteries": "view",
                   "trackers": "view", "cash": "edit", "mailing": "view"},
      "actions": {}}, False),
    ("tech", "Механик",
     {"sections": {"dashboard": "view", "bikes": "edit", "service": "edit",
                   "rentals": "view", "reports": "view", "inventory": "edit",
                   "batteries": "edit", "trackers": "view"},
      "actions": {}}, False),
)


def check_profile_name(raw: Any) -> Check:
    return check_name(raw, what="Название профиля")


# ─────────────────────────── сервис: наряды ───────────────────────────
#
# Ремонт как запись факта был и раньше: bike_log вида repair плюс позиции
# по узлам. Наряд - процесс вокруг него: кто взял, на каком этапе, сколько
# суток стоит и за чей счёт. Отсюда же ремонт чужой техники.
#
# Деньги наряда в crm.ledger не попадают намеренно: журнал - это аренда,
# по нему считается средний чек, и выручка чужого ремонта его бы испортила.

ORDER_STATUSES: dict[str, str] = {
    "new": "Новый",
    "in_work": "В работе",
    "approve": "На согласовании",
    "waiting": "Ждёт запчасть",
    "done": "Готов",
    "cancelled": "Отменён",
}
# Наряд в этих состояниях держит велосипед: он не свободен и не выдаётся.
# «На согласовании» - тоже: техника разобрана и ждёт ответа клиента.
ORDER_OPEN = ("new", "in_work", "approve", "waiting")
# Статусы, которые ставит человек в форме. «На согласовании» ставит
# отправка сметы, а не выпадающий список: без сметы согласовывать нечего.
ORDER_MANUAL_STATUSES = ("new", "in_work", "waiting", "cancelled")

PAYERS: dict[str, str] = {"own": "Наш", "client": "Клиент"}

WORK_CATEGORIES: tuple[str, ...] = (
    "Электрика", "Тормоза", "Ходовая", "Свет", "ТО", "Прочее",
)

# Сколько суток наряд может стоять, прежде чем это станет заметно.
# Велосипед в ремонте - это велосипед вне аренды, то есть прямой простой.
ORDER_STUCK_DAYS = 3


def order_no(number: int) -> str:
    """Человекочитаемый номер наряда: на него ссылаются в переписке."""
    return f"РЕМ-{int(number):06d}"


def check_order_status(raw: Any) -> Check:
    return check_choice(raw, ORDER_STATUSES, what="Статус наряда")


def check_payer(raw: Any) -> Check:
    return check_choice(raw, PAYERS, what="Плательщик")


def item_total(item: dict) -> Decimal:
    """Строка наряда клиенту: цена за единицу на количество."""
    return to_money(item.get("price") or 0) * int(item.get("qty") or 1)


def item_cost(item: dict) -> Decimal:
    """Себестоимость строки: запчасти плюс работа, тоже на количество."""
    parts = to_money(item.get("parts_cost") or 0) + to_money(item.get("labor_cost") or 0)
    return parts * int(item.get("qty") or 1)


def order_totals(items: Iterable[dict]) -> dict[str, Decimal]:
    """Итоги наряда по его строкам.

    Считается здесь, а не в базе: сумма наряда обязана совпадать с тем,
    что оператор видит на экране, а не с тем, что когда-то записали.
    """
    rows = list(items)
    total = sum((item_total(i) for i in rows), Decimal(0))
    cost = sum((item_cost(i) for i in rows), Decimal(0))
    return {"total": to_money(total), "cost": to_money(cost),
            "margin": to_money(total - cost), "lines": len(rows)}


def order_days(order: dict, *, today: date | None = None) -> int:
    """Суток в работе. Закрытый наряд считается по дате закрытия."""
    opened = order.get("opened_at")
    if opened is None:
        return 0
    start = opened.date() if isinstance(opened, datetime) else opened
    closed = order.get("closed_at")
    end = closed.date() if isinstance(closed, datetime) else (closed or today or date.today())
    return max((end - start).days, 0)


def order_is_open(order: dict | None) -> bool:
    return bool(order) and str(order.get("status") or "") in ORDER_OPEN


def order_stuck(order: dict, *, today: date | None = None) -> bool:
    """Наряд стоит дольше нормы - велосипед копит простой."""
    return order_is_open(order) and order_days(order, today=today) >= ORDER_STUCK_DAYS


def service_rows(bikes: Iterable[dict], orders_by_bike: dict[int, dict], *,
                 today: date | None = None) -> list[dict]:
    """Рабочий стол сервиса: велосипеды в ремонте и что с ними.

    Главная строка здесь - «в ремонте, а наряда нет»: велосипед стоит,
    никто им не занят, и в отчёте простоя он выглядит как обычный ремонт.
    Такие идут первыми и по убыванию суток.
    """
    today = today or date.today()
    rows = []
    for bike in bikes:
        if bike.get("status") not in ("repair", "maintenance"):
            continue
        order = orders_by_bike.get(bike["id"])
        days = order_days(order, today=today) if order else (bike.get("idle_days") or 0)
        rows.append({**bike, "order": order,
                     "stage": ORDER_STATUSES.get((order or {}).get("status"), "Без наряда"),
                     "days": days,
                     "stuck": (not order) or order_stuck(order, today=today)})
    # Без наряда - в начало: это и есть потерянные велосипеды сервиса.
    rows.sort(key=lambda r: (r["order"] is not None, -r["days"]))
    return rows


def service_summary(rows: Iterable[dict]) -> dict[str, int]:
    """Сводка рабочего стола: сколько стоит и сколько из них без наряда."""
    rows = list(rows)
    return {
        "total": len(rows),
        "no_order": sum(1 for r in rows if r["order"] is None),
        "stuck": sum(1 for r in rows if r["stuck"]),
        "days": sum(r["days"] for r in rows),
    }


# ───────────────────────── пересчёт техники ─────────────────────────

TAKE_SCOPES: dict[str, str] = {"all": "Весь парк", "location": "Одна точка"}
TAKE_STATES: dict[str, str] = {
    "expected": "Не отмечен", "found": "На месте",
    "missing": "Не нашли", "extra": "Лишний",
}
# Кого ждём увидеть на точке. rented - у курьера, его на месте нет и быть
# не должно; sold и written_off из парка вышли. lost в ведомость не ставим:
# он уже потерян, а если найдётся - попадёт в неё лишним, ради этого
# пересчёт и затевается.
TAKE_EXPECTED_STATUSES = ("available", "repair", "maintenance", "reserved")


def take_no(number: int) -> str:
    """Номер ведомости: на неё ссылаются в акте и в переписке с точкой."""
    return f"ПРТ-{int(number):06d}"


def check_scope(raw: Any) -> Check:
    return check_choice(raw, TAKE_SCOPES, what="Область пересчёта")


def take_is_open(take: dict | None) -> bool:
    return bool(take) and str((take or {}).get("status") or "") == "open"


def expected_bikes(bikes: Iterable[dict], *, scope: str,
                   location: str | None = None) -> list[dict]:
    """Кого ждём на точке в момент открытия ведомости.

    Снимок делается один раз при открытии: если считать «ожидалось» на лету,
    велосипед, выданный клиенту посреди пересчёта, молча исчезнет из
    недостачи и пропажу никто не увидит.
    """
    rows = [b for b in bikes if b.get("status") in TAKE_EXPECTED_STATUSES]
    if scope == "location":
        rows = [b for b in rows if (b.get("location") or "") == (location or "")]
    return sorted(rows, key=lambda b: str(b.get("code") or ""))


def take_counts(items: Iterable[dict]) -> dict[str, int]:
    """Счётчики ведомости по её строкам."""
    rows = list(items)
    counts = {state: 0 for state in TAKE_STATES}
    for item in rows:
        state = str(item.get("state") or "")
        if state in counts:
            counts[state] += 1
    # Ожидалось - вся ведомость без лишних: те в парке не числились.
    counts["total"] = len(rows) - counts["extra"]
    counts["left"] = counts["expected"]
    return counts


def take_progress(counts: dict[str, int]) -> int:
    """Сколько процентов ведомости пройдено - для полосы на экране."""
    total = int(counts.get("total") or 0)
    if total <= 0:
        return 0
    done = int(counts.get("found") or 0) + int(counts.get("missing") or 0)
    return min(int(round(done * 100 / total)), 100)


def take_title(take: dict) -> str:
    """Подпись ведомости: что считали и где."""
    if str(take.get("scope") or "") == "location":
        return f"Точка {take.get('location') or '—'}"
    return TAKE_SCOPES["all"]


# ─────────────────────── окупаемость по моделям ───────────────────────

# Дней в месяце для пересчёта амортизации на произвольный период.
# Месяц берётся средним: отчёт за две недели не должен зависеть от того,
# февраль это или июль.
DAYS_IN_MONTH = Decimal("30.44")


def payback_rows(bikes: Iterable[dict], money: dict[str, dict], *,
                 days: Decimal | int) -> list[dict]:
    """Окупаемость по моделям: что модель принесла и что съела за период.

    Маржа считается со всем, что модель стоила: ремонт своего парка и
    амортизация. Без амортизации модель с дешёвым прокатом и дорогой рамой
    выглядит прибыльной ровно до того дня, когда парк надо обновлять.
    Ремонт, оплаченный клиентом, идёт в плюс: это наш велосипед, починенный
    за его счёт.
    """
    days = Decimal(str(days or 0))
    fleet: dict[str, dict] = {}
    for bike in bikes:
        model = str(bike.get("model") or "—")
        cell = fleet.setdefault(model, {"bikes": 0, "amortization": Decimal(0),
                                        "priced": 0})
        cell["bikes"] += 1
        # Из парка выбывшие амортизацию не копят: списанный велосипед уже
        # списан, проданный больше не наш.
        if bike.get("status") not in OPERATIONAL_STATUSES:
            continue
        month = amortization_month(bike)
        if month is not None:
            cell["priced"] += 1
            cell["amortization"] += month * days / DAYS_IN_MONTH
    rows = []
    for model in sorted(set(fleet) | set(money)):
        cell = fleet.get(model, {"bikes": 0, "amortization": Decimal(0), "priced": 0})
        cash = money.get(model, {})
        paid = to_money(cash.get("paid") or 0)
        works = to_money(cash.get("works") or 0)
        repair = to_money(cash.get("repair_cost") or 0)
        amortization = to_money(cell["amortization"])
        earned = paid + works
        margin = earned - repair - amortization
        rented_days = Decimal(str(cash.get("rented_days") or 0))
        rows.append({
            "model": model, "bikes": cell["bikes"], "priced": cell["priced"],
            "paid": paid, "charged": to_money(cash.get("charged") or 0),
            "repair_cost": repair, "works": works, "amortization": amortization,
            "margin": to_money(margin),
            "margin_percent": (float(round(100 * margin / earned, 1)) if earned else None),
            "rented_days": rented_days,
            "check_per_day": (to_money(paid / rented_days) if rented_days > 0 else None),
        })
    rows.sort(key=lambda r: r["margin"], reverse=True)
    return rows


def payback_total(rows: Iterable[dict]) -> dict[str, Any]:
    """Строка «Итого»: та же арифметика, что и по строкам."""
    rows = list(rows)
    total = {key: to_money(sum((r[key] for r in rows), Decimal(0)))
             for key in ("paid", "charged", "repair_cost", "works", "amortization",
                         "margin")}
    total["bikes"] = sum(r["bikes"] for r in rows)
    total["priced"] = sum(r["priced"] for r in rows)
    total["rented_days"] = sum((r["rented_days"] for r in rows), Decimal(0))
    earned = total["paid"] + total["works"]
    total["margin_percent"] = (float(round(100 * total["margin"] / earned, 1))
                               if earned else None)
    total["check_per_day"] = (to_money(total["paid"] / total["rented_days"])
                              if total["rented_days"] > 0 else None)
    return total


# ─────────────────────── реферальная программа ───────────────────────

REF_STATUSES: dict[str, str] = {
    "click": "Перешёл", "signed": "Зарегистрировался",
    "rented": "Взял велосипед", "paid": "Заплатил",
}
# Воронка идёт только вперёд: друг, уже взявший велосипед, не откатывается
# в «перешёл» из-за повторного нажатия ссылки.
REF_ORDER = ("click", "signed", "rented", "paid")

# Код приглашения: без похожих символов (0/O, 1/I/L) - его диктуют голосом
# и переписывают с экрана.
REF_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
REF_CODE_LEN = 6
REF_BONUS_DEFAULT = Decimal("500.00")
# Бонус платится с первого платежа друга, но не за копейку: иначе хватило
# бы перевести 10 ₽ с собственной карты на карту знакомого.
REF_MIN_PAYMENT_DEFAULT = Decimal("1000.00")


def make_ref_code(rnd: Any = None) -> str:
    """Новый код приглашения. Уникальность проверяет база, а не эта функция."""
    rnd = rnd or random
    return "".join(rnd.choice(REF_ALPHABET) for _ in range(REF_CODE_LEN))


def clean_ref_code(raw: Any) -> str:
    """Код из ссылки или из сообщения: только буквы алфавита кода.

    Человек присылает его как угодно - строчными, с пробелами, вместе с
    «мой код». Всё лишнее отбрасывается, регистр поднимается.
    """
    text = str(raw or "").strip().upper()
    kept = "".join(ch for ch in text if ch in REF_ALPHABET)
    return kept[:REF_CODE_LEN] if len(kept) >= REF_CODE_LEN else ""


def ref_link(bot_username: str, code: str) -> str:
    """Ссылка-приглашение. Пустое имя бота - вернётся один код: показать
    его всё равно надо, ссылку соберёт оператор."""
    name = str(bot_username or "").lstrip("@")
    return f"https://t.me/{name}?start={code}" if name else code


def ref_status_at_least(status: str, target: str) -> bool:
    try:
        return REF_ORDER.index(status) >= REF_ORDER.index(target)
    except ValueError:
        return False


def ref_settings(raw: dict[str, str] | None) -> dict[str, Any]:
    """Настройки программы из таблицы настроек, с разумными значениями
    по умолчанию: программа работает сразу, без обязательной настройки."""
    raw = raw or {}

    def money_or(key: str, default: Decimal) -> Decimal:
        try:
            value = to_money(Decimal(str(raw[key])))
        except (KeyError, ArithmeticError, ValueError, TypeError):
            return default
        return value if value >= 0 else default

    return {
        "enabled": str(raw.get("ref_enabled", "1")) not in ("0", "", "false"),
        "bonus": money_or("ref_bonus", REF_BONUS_DEFAULT),
        "min_payment": money_or("ref_min_payment", REF_MIN_PAYMENT_DEFAULT),
    }


def ref_funnel(rows: Iterable[dict]) -> dict[str, Any]:
    """Воронка программы: перешли → зарегистрировались → взяли → заплатили.

    Каждый шаг считает и тех, кто ушёл дальше: друг, который уже заплатил,
    остаётся и в «перешёл». Иначе воронка сужалась бы задним числом и
    конверсия считалась бы от нуля.
    """
    rows = list(rows)
    steps = {code: sum(1 for r in rows
                       if ref_status_at_least(str(r.get("status") or ""), code))
             for code in REF_ORDER}
    paid_bonus = sum((to_money(r.get("bonus") or 0) for r in rows
                      if r.get("status") == "paid"), Decimal(0))
    steps["bonus"] = to_money(paid_bonus)
    steps["conversion"] = (float(round(100 * steps["paid"] / steps["click"], 1))
                           if steps["click"] else None)
    # Во что обошёлся приведённый клиент: бонусы делятся на заплативших.
    steps["price"] = (to_money(paid_bonus / steps["paid"]) if steps["paid"] else None)
    return steps


def ref_agents(rows: Iterable[dict]) -> list[dict]:
    """Агенты по убыванию заплативших друзей: кого благодарить."""
    agents: dict[int, dict] = {}
    for row in rows:
        agent_id = int(row["agent_id"])
        cell = agents.setdefault(agent_id, {
            "agent_id": agent_id, "agent_name": row.get("agent_name"),
            "agent_phone": row.get("agent_phone"), "ref_code": row.get("ref_code"),
            "click": 0, "signed": 0, "rented": 0, "paid": 0,
            "bonus": Decimal(0), "last_at": None, "last_friend": None})
        status = str(row.get("status") or "")
        for code in REF_ORDER:
            if ref_status_at_least(status, code):
                cell[code] += 1
        if status == "paid":
            cell["bonus"] += to_money(row.get("bonus") or 0)
        at = row.get("created_at")
        if at is not None and (cell["last_at"] is None or at > cell["last_at"]):
            cell["last_at"] = at
            cell["last_friend"] = row.get("friend_name")
    out = list(agents.values())
    for cell in out:
        cell["bonus"] = to_money(cell["bonus"])
    out.sort(key=lambda a: (a["paid"], a["signed"], a["click"]), reverse=True)
    return out


# ─────────────────── сотрудник и его Telegram ───────────────────

# Код привязки длиннее реферального: его вводят один раз и по нему
# открывается доступ сотрудника, а не скидка.
LINK_CODE_LEN = 8


def make_link_code(rnd: Any = None) -> str:
    rnd = rnd or random
    return "".join(rnd.choice(REF_ALPHABET) for _ in range(LINK_CODE_LEN))


def clean_link_code(raw: Any) -> str:
    text = str(raw or "").strip().upper()
    kept = "".join(ch for ch in text if ch in REF_ALPHABET)
    return kept[:LINK_CODE_LEN] if len(kept) >= LINK_CODE_LEN else ""


def staff_tg_label(staff: dict) -> str:
    """Что показать в колонке Telegram: подключён, ждёт кода или ничего."""
    if staff.get("tg_id"):
        name = staff.get("tg_username")
        return f"@{name}" if name else "Подключён"
    return "Ждёт кода" if staff.get("link_code") else "—"


# ─────────────────── витрина свободных велосипедов ───────────────────

def free_bikes_post(bikes: Iterable[dict], tariffs: Iterable[dict], *,
                    limit: int = 8) -> dict[str, Any] | None:
    """Данные поста «сегодня свободно» для канала. None - постить нечего.

    Свободный велосипед - это прямой простой, а канал читают те самые
    курьеры. Номера рам в пост не идут: клиенту нужна модель и цена,
    а не инвентарный номер.
    """
    by_model: dict[str, int] = {}
    for bike in bikes:
        if bike.get("status") != "available":
            continue
        by_model[str(bike.get("model") or "Без модели")] = \
            by_model.get(str(bike.get("model") or "Без модели"), 0) + 1
    if not by_model:
        return None
    rows = sorted(by_model.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    lines = [f"• {model} — {count} шт." for model, count in rows]
    prices = [t for t in tariffs if t.get("active")]
    cheapest = min((to_money(t["price"]) / max(int(t.get("period_days") or 1), 1)
                    for t in prices), default=None)
    total = sum(by_model.values())
    return {"lines": "\n".join(lines), "total": total,
            "price": money(cheapest) if cheapest is not None else ""}


# ─────────────────── откуда пришёл клиент ───────────────────

# Каналы привлечения. Порядок - по тому, как часто приходят курьеры;
# «сарафан» проставляется сам, когда клиента привёл друг по приглашению.
CLIENT_CHANNELS: dict[str, str] = {
    "avito": "Авито",
    "2gis": "2ГИС",
    "yandex_maps": "Яндекс Карты",
    "referral": "Сарафан (по приглашению)",
    "channel": "Наш Telegram-канал",
    "site": "Сайт",
    "other": "Другое",
}


def check_channel(raw: Any) -> Check:
    """Канал привлечения. Пусто - «не спросили», это нормальное состояние."""
    value = str(raw or "").strip()
    if not value:
        return Check(True, None)
    return check_choice(value, CLIENT_CHANNELS, what="Канал привлечения")


def channel_rows(clients: Iterable[dict], *, months: int = 12,
                 today: date | None = None) -> dict[str, Any]:
    """Новые клиенты по месяцам и каналам: куда давать рекламу.

    Считаются по дате появления карточки. Клиент без канала попадает
    в «не спросили»: честнее показать пробел, чем размазать его по
    известным каналам.
    """
    today = today or date.today()
    first = today.replace(day=1)
    scale: list[date] = []
    for _ in range(months):
        scale.append(first)
        first = (first - timedelta(days=1)).replace(day=1)
    scale.reverse()
    known = set(scale)
    grid: dict[date, dict[str, int]] = {m: {} for m in scale}
    totals: dict[str, int] = {}
    for client in clients:
        created = client.get("created_at")
        if created is None:
            continue
        day = created.date() if isinstance(created, datetime) else created
        month = day.replace(day=1)
        if month not in known:
            continue
        channel = str(client.get("channel") or "")
        channel = channel if channel in CLIENT_CHANNELS else ""
        grid[month][channel] = grid[month].get(channel, 0) + 1
        totals[channel] = totals.get(channel, 0) + 1
    # Колонки - только те каналы, по которым кто-то пришёл: пустые
    # столбцы занимают ширину и ничего не говорят.
    columns = [code for code in CLIENT_CHANNELS if totals.get(code)]
    if totals.get(""):
        columns.append("")
    rows = [{"month": month,
             "cells": {code: grid[month].get(code, 0) for code in columns},
             "total": sum(grid[month].values())}
            for month in scale]
    return {"columns": columns, "rows": rows, "totals": totals,
            "total": sum(totals.values())}


def channel_label(code: str) -> str:
    return CLIENT_CHANNELS.get(code, "не спросили")


# ─────────────────────── расхождения в данных ───────────────────────

# Что проверяем. Формулировки - для человека, который будет это чинить:
# не «нарушение инварианта», а что именно пойдёт не так.
INTEGRITY_KINDS: dict[str, str] = {
    "rented_no_rental": "Числится в аренде, а аренды нет",
    "rental_no_bike_status": "Аренда идёт, а велосипед не в аренде",
    "rental_without_bike": "Аренда без велосипеда",
    "order_on_rented": "Наряд открыт на велосипед, который у клиента",
    "repair_no_order": "В ремонте, а наряда нет",
    "lost_with_rental": "Числится утерянным, а аренда идёт",
    "debt_without_rental": "Долг есть, а аренды нет",
}
# Долг, ниже которого разбираться не с чем: копейки округления и
# недоплаты в пару рублей висят у половины базы.
DEBT_NOISE = Decimal(500)


def integrity_issues(bikes: Iterable[dict], rentals: Iterable[dict],
                     orders_by_bike: dict[int, dict],
                     debtors: Iterable[dict] = ()) -> list[dict]:
    """Расхождения между парком, арендами и нарядами.

    Расхождение - это не «некрасиво в базе», а невидимый простой: велосипед,
    числящийся в аренде без аренды, не попадает ни в выдачу, ни в ремонт,
    и никто про него не вспомнит, пока не придёт пересчёт.
    """
    bikes = list(bikes)
    rentals = [r for r in rentals if r.get("status") == "active"]
    rented_bikes = {int(r["bike_id"]): r for r in rentals if r.get("bike_id")}
    by_id = {int(b["id"]): b for b in bikes}
    issues: list[dict] = []

    def add(kind: str, *, bike: dict | None = None, rental: dict | None = None,
            what: str = "") -> None:
        issues.append({"kind": kind, "title": INTEGRITY_KINDS[kind], "bike": bike,
                       "rental": rental, "what": what})

    for bike in bikes:
        bike_id = int(bike["id"])
        status = bike.get("status")
        if status == "rented" and bike_id not in rented_bikes:
            add("rented_no_rental", bike=bike,
                what="Выдать его нельзя, в простой он не попадает.")
        if status == "lost" and bike_id in rented_bikes:
            add("lost_with_rental", bike=bike, rental=rented_bikes[bike_id],
                what="Либо велосипед у клиента, либо он потерян.")
        order = orders_by_bike.get(bike_id)
        if order and status == "rented":
            add("order_on_rented", bike=bike, rental=rented_bikes.get(bike_id),
                what=f"Наряд {order.get('no')} висит на велосипеде у клиента.")
        if status == "repair" and not order:
            add("repair_no_order", bike=bike,
                what="Работой никто не занят, а простой копится.")
    for rental in rentals:
        bike_id = rental.get("bike_id")
        if not bike_id:
            add("rental_without_bike", rental=rental,
                what="Непонятно, что у клиента на руках.")
            continue
        bike = by_id.get(int(bike_id))
        if bike is not None and bike.get("status") != "rented":
            add("rental_no_bike_status", bike=bike, rental=rental,
                what=f"Велосипед числится «{BIKE_STATUSES.get(bike.get('status'), '—')}»"
                     " и может уйти второму клиенту.")
    active_clients = {int(r["client_id"]) for r in rentals if r.get("client_id")}
    for debtor in debtors:
        if int(debtor.get("id") or 0) in active_clients:
            continue
        debt = -to_money(debtor.get("balance") or 0)
        if debt >= DEBT_NOISE:
            issues.append({"kind": "debt_without_rental",
                           "title": INTEGRITY_KINDS["debt_without_rental"],
                           "bike": None, "rental": None, "client": debtor,
                           "what": f"{money(debt)} за клиентом, аренда закрыта."})
    return issues


def integrity_summary(issues: Iterable[dict]) -> dict[str, int]:
    """Сколько расхождений какого вида: для плиток и для сообщения в чат."""
    out: dict[str, int] = {}
    for issue in issues:
        out[issue["kind"]] = out.get(issue["kind"], 0) + 1
    out["total"] = sum(out.values())
    return out


def integrity_digest(issues: Iterable[dict]) -> str:
    """Строки для служебного чата. Пусто - расхождений нет, молчим."""
    summary = integrity_summary(issues)
    lines = [f"• {INTEGRITY_KINDS[kind]}: {count}"
             for kind, count in summary.items() if kind in INTEGRITY_KINDS and count]
    return "\n".join(lines)


# ───────────────────────────── склад запчастей ─────────────────────────────

MOVE_KINDS: dict[str, str] = {
    "receipt": "Приход", "order": "В наряд", "issue": "Выдали со склада",
    "write_off": "Списание", "count": "Пересчёт",
}
# Движения, которые увеличивают остаток. Знак всё равно лежит в qty -
# этот набор нужен подписям и фильтрам, а не арифметике.
MOVE_IN = ("receipt",)
DOC_KINDS: dict[str, str] = {"receipt": "Приход", "write_off": "Списание"}
DOC_PREFIX = {"receipt": "ПРХ", "write_off": "СПС"}
PART_ORDER_STATUSES: dict[str, str] = {
    "new": "Собирается", "ordered": "Заказано", "received": "Принято",
    "cancelled": "Отменён",
}
NEED_SOURCES: dict[str, str] = {
    "order": "Наряд ждёт запчасть", "min_stock": "Ниже неснижаемого",
    "manual": "Вписали руками",
}
PART_UNITS: tuple[str, ...] = ("шт", "компл.", "м", "л", "кг")


def doc_no(kind: str, number: int) -> str:
    """Номер складского документа: ПРХ-000001, СПС-000001."""
    return f"{DOC_PREFIX.get(kind, 'ДОК')}-{int(number):06d}"


def part_order_no(number: int) -> str:
    return f"ЗАП-{int(number):06d}"


def check_move_kind(raw: Any) -> Check:
    return check_choice(raw, MOVE_KINDS, what="Вид движения")


def check_unit(raw: Any) -> Check:
    value = str(raw or "").strip() or "шт"
    if len(value) > 12:
        return Check(False, error="Единица: не длиннее 12 символов.")
    return Check(True, value)


def stock_of(moves: Iterable[dict]) -> int:
    """Остаток позиции - сумма её движений. Отдельной колонки нет
    намеренно: та разошлась бы с журналом на первой же гонке."""
    return sum(int(m.get("qty") or 0) for m in moves)


def average_cost(stock: int, cost: Any, qty: int, price: Any) -> Decimal:
    """Средняя себестоимость после прихода.

    Считается по средневзвешенной, а не по последней цене: иначе один
    дорогой приход задрал бы себестоимость всех ремонтов на складе,
    где лежит десяток старых дешёвых деталей.
    """
    stock, qty = max(int(stock), 0), int(qty)
    old_sum = to_money(cost) * stock
    new_sum = to_money(price) * qty
    total = stock + qty
    if total <= 0:
        return to_money(price)
    return to_money((old_sum + new_sum) / total)


def part_rows(parts: Iterable[dict], stocks: dict[int, int]) -> list[dict]:
    """Остатки склада: позиция, сколько на полке и чего не хватает.

    Первыми - те, чей остаток ниже неснижаемого: это и есть список
    «что заказать», и он должен быть виден без прокрутки.
    """
    rows = []
    for part in parts:
        stock = int(stocks.get(int(part["id"]), 0))
        minimum = int(part.get("min_stock") or 0)
        rows.append({**part, "stock": stock,
                     "short": max(minimum - stock, 0),
                     "below": stock < minimum,
                     "cost_total": to_money(part.get("cost") or 0) * max(stock, 0),
                     "price_total": to_money(part.get("price") or 0) * max(stock, 0)})
    rows.sort(key=lambda r: (not r["below"], -r["short"], str(r.get("title") or "")))
    return rows


def stock_summary(rows: Iterable[dict]) -> dict[str, Any]:
    rows = list(rows)
    return {
        "positions": len(rows),
        "below": sum(1 for r in rows if r["below"]),
        "empty": sum(1 for r in rows if r["stock"] <= 0),
        "cost": to_money(sum((r["cost_total"] for r in rows), Decimal(0))),
    }


def part_needs(rows: Iterable[dict], waiting: Iterable[dict] = ()) -> list[dict]:
    """Что заказывать: нехватка до неснижаемого и наряды, ждущие запчасть.

    Наряд в состоянии «ждёт запчасть» - это велосипед, который стоит
    и копит простой, поэтому его строки идут первыми, даже если на полке
    всё в норме.

    Потребности по одной позиции складываются: нехватка до неснижаемого
    считается от сегодняшнего остатка, а наряд заберёт ещё одну сверх.
    Иначе в заказ ушла бы только первая строка - уникальный индекс не даёт
    положить позицию в заказ дважды, и вторая потребность пропала бы молча.
    """
    needs: list[dict] = []
    by_part: dict[int, dict] = {}

    def add(need: dict) -> None:
        part_id = need.get("part_id")
        if not part_id:
            # Узел, под который позиции ещё не завели: складывать не с чем,
            # и в заказ она не пойдёт, пока её не заведут.
            needs.append(need)
            return
        seen = by_part.get(int(part_id))
        if seen is None:
            by_part[int(part_id)] = need
            needs.append(need)
            return
        seen["qty"] += need["qty"]
        if need["source"] == "order" and seen["source"] != "order":
            # Наряд важнее: за ним стоит велосипед, а не полка.
            seen.update(source="order", work_order_id=need["work_order_id"],
                        work_order_no=need["work_order_no"],
                        bike_code=need["bike_code"])

    for order in waiting:
        add({"source": "order", "part_id": order.get("part_id"),
             "title": order.get("title") or "", "qty": int(order.get("qty") or 1),
             "work_order_id": order.get("work_order_id"),
             "work_order_no": order.get("work_order_no"),
             "bike_code": order.get("bike_code")})
    for row in rows:
        if row["below"] and row.get("active", True):
            add({"source": "min_stock", "part_id": int(row["id"]),
                 "title": row.get("title") or "", "qty": row["short"],
                 "work_order_id": None, "work_order_no": None, "bike_code": None})
    needs.sort(key=lambda n: (n["source"] != "order", n["title"]))
    return needs


def order_total(items: Iterable[dict]) -> Decimal:
    return to_money(sum((to_money(i.get("price") or 0) * int(i.get("qty") or 1)
                         for i in items), Decimal(0)))


# ─────────────────── замена велосипеда внутри аренды ───────────────────

SWAP_REASONS: dict[str, str] = {
    "repair": "Поломка",
    "maintenance": "Плановое ТО",
    "client": "По просьбе клиента",
    "other": "Другое",
}
# Куда уходит снятый велосипед. По умолчанию в ремонт: заменяют обычно
# сломанный, и «свободен» вернул бы его в выдачу неисправным.
SWAP_BIKE_STATUS = {"repair": "repair", "maintenance": "maintenance",
                    "client": "available", "other": "available"}


def check_swap_reason(raw: Any) -> Check:
    return check_choice(raw, SWAP_REASONS, what="Причина замены")


def swap_candidates(bikes: Iterable[dict], *, current_id: Any = None) -> list[dict]:
    """Кого предложить на замену: свободные, подменные - первыми.

    Подменный фонд держат как раз под такие случаи, а свободный велосипед
    лучше оставить новому клиенту: замена аренду не увеличивает, а выдача
    увеличивает.
    """
    rows = [b for b in bikes
            if b.get("status") == "available" and int(b["id"]) != int(current_id or 0)]
    rows.sort(key=lambda b: (not b.get("spare"), str(b.get("code") or "")))
    return rows


def rental_bike_rows(rows: Iterable[dict], *, today: date | None = None) -> list[dict]:
    """Журнал перемещений аренды: что и сколько было у клиента."""
    today = today or date.today()
    out = []
    for row in rows:
        issued = row.get("issued_on") or today
        until = row.get("returned_on") or today
        start, end = row.get("mileage_start"), row.get("mileage_end")
        out.append({**row, "days": max((until - issued).days, 0),
                    "open": row.get("returned_on") is None,
                    "ridden": (int(end) - int(start))
                    if start is not None and end is not None else None})
    return out


def rental_mileage(rows: Iterable[dict], *, current: int | None = None) -> int:
    """Сколько накатали за аренду по всем велосипедам вместе.

    После замены одометр нового велосипеда считается со своего начала:
    иначе «накатал» получался бы разницей одометров разных машин.
    """
    total = 0
    for row in rows:
        start = row.get("mileage_start")
        end = row.get("mileage_end")
        if end is None and row.get("returned_on") is None and current is not None:
            end = current
        if start is not None and end is not None and int(end) >= int(start):
            total += int(end) - int(start)
    return total


# ───────────────────────────── розыск ─────────────────────────────

# Через сколько суток просрочки аренда попадает в розыск и через сколько
# суток розыска пора признавать велосипед потерянным. Значения по
# умолчанию - для Казани с недельным тарифом: неделя молчания это уже не
# «забыл оплатить».
SEARCH_AFTER_DAYS = 7
THEFT_AFTER_DAYS = 21


def search_settings(raw: dict[str, str] | None) -> dict[str, int]:
    raw = raw or {}

    def days(key: str, default: int) -> int:
        try:
            value = int(str(raw[key]))
        except (KeyError, ValueError, TypeError):
            return default
        return value if 1 <= value <= 365 else default

    return {"search_after": days("search_after_days", SEARCH_AFTER_DAYS),
            "theft_after": days("theft_after_days", THEFT_AFTER_DAYS)}


def in_search(rental: dict) -> bool:
    return bool(rental.get("search_at"))


def search_days(rental: dict, *, today: date | None = None) -> int:
    """Сколько суток аренда в розыске."""
    started = rental.get("search_at")
    if started is None:
        return 0
    start = started.date() if isinstance(started, datetime) else started
    return max(((today or date.today()) - start).days, 0)


def search_rows(rentals: Iterable[dict], *, settings: dict[str, int],
                today: date | None = None) -> dict[str, list[dict]]:
    """Кого искать: просрочившие дольше нормы и уже объявленные в розыск.

    Просрочка считается по тому же «оплачено до», что и напоминания:
    иначе в розыск попадал бы клиент, у которого на балансе есть деньги
    на следующий период.
    """
    today = today or date.today()
    candidates, searching = [], []
    for rental in rentals:
        if rental.get("status") != "active":
            continue
        until = covered_until(rental["billed_until"], rental.get("balance", 0),
                              rental["price"], rental["period_days"])
        overdue = max(-days_left(until, today=today), 0)
        row = {**rental, "overdue_days": overdue, "covered_until": until,
               "search_days": search_days(rental, today=today)}
        if in_search(rental):
            row["theft"] = row["search_days"] >= settings["theft_after"]
            searching.append(row)
        elif overdue >= settings["search_after"]:
            candidates.append(row)
    candidates.sort(key=lambda r: -r["overdue_days"])
    searching.sort(key=lambda r: -r["search_days"])
    return {"candidates": candidates, "searching": searching}


def search_digest(rows: dict[str, list[dict]]) -> str:
    """Строки для служебного чата. Пусто - искать некого, молчим."""
    lines = []
    for row in rows["candidates"]:
        lines.append(f"• {row.get('full_name') or '—'} · № {row.get('bike_code') or '—'}"
                     f" — просрочка {row['overdue_days']} дн., пора в розыск")
    for row in rows["searching"]:
        if row.get("theft"):
            lines.append(f"• {row.get('full_name') or '—'} · № {row.get('bike_code') or '—'}"
                         f" — в розыске {row['search_days']} дн., пора признавать потерю")
    return "\n".join(lines)


# ─────────────────── план месяца и прогноз освобождения ───────────────────

def month_plan(raw: dict[str, str] | None, *, fleet: int = 0) -> dict[str, Any]:
    """План на месяц из настроек. Не задан - считается от парка и целей.

    Умолчания берутся из трёх чисел, а не из воздуха: столько парк даёт,
    если держать простой в норме и чек на цели. План - это то, что можно
    подвинуть, а не то, что надо придумать с нуля.
    """
    raw = raw or {}

    def number(key: str, default: int) -> int:
        try:
            value = int(str(raw[key]))
        except (KeyError, ValueError, TypeError):
            return default
        return value if value >= 0 else default

    rented = number("plan_rented", int(round(fleet * (100 - IDLE_TARGET_PERCENT) / 100)))
    check = to_money(raw.get("plan_check") or CHECK_TARGET)
    if check <= 0:
        check = CHECK_TARGET
    return {"rented": rented, "check": check, "fleet": fleet}


def plan_progress(plan: dict[str, Any], metrics: dict[str, Any], *,
                  days_in_month: int, days_passed: int) -> dict[str, Any]:
    """Факт против плана: деньги за месяц и сколько ещё можно взять.

    План месяца - велосипеде-дни аренды на чек. Сравнивается с тем же
    средним чеком, что и в трёх числах, иначе цифры на соседних экранах
    разошлись бы.
    """
    days_in_month = max(int(days_in_month), 1)
    days_passed = min(max(int(days_passed), 0), days_in_month)
    target = to_money(plan["check"] * plan["rented"] * days_in_month)
    fact = to_money(metrics.get("revenue") or 0)
    # Сколько должно было прийти к сегодняшнему дню: план ровным темпом.
    pace = to_money(target * days_passed / days_in_month)
    left = max(target - fact, Decimal(0))
    days_left = days_in_month - days_passed
    return {
        "target": target, "fact": fact, "pace": pace, "left": left,
        "ahead": fact >= pace,
        "percent": (float(round(100 * fact / target, 1)) if target else None),
        "days_left": days_left,
        # Чтобы выйти на план, столько велосипедов должно кататься каждый
        # оставшийся день по целевому чеку.
        "need_rented": (int(-(-left / (plan["check"] * days_left) // 1))
                        if days_left > 0 and plan["check"] > 0 and left > 0 else 0),
    }


def freeing_soon(rentals: Iterable[dict], *, today: date | None = None,
                 horizon: int = 3) -> dict[str, list[dict]]:
    """Что освободится в ближайшие дни - по «оплачено до» и намерению.

    Мастеру выдачи важно не только «свободно сейчас», но и «завтра будет»:
    клиенту, который приедет после обеда, можно обещать конкретный день.
    Тот, кто сказал «продлю», в прогноз не идёт - его велосипед не вернётся.
    """
    today = today or date.today()
    out: dict[str, list[dict]] = {str(i): [] for i in range(horizon + 1)}
    for rental in rentals:
        if rental.get("status") != "active" or not rental.get("bike_id"):
            continue
        if rental.get("intent") == "renew":
            continue
        until = covered_until(rental["billed_until"], rental.get("balance", 0),
                              rental["price"], rental["period_days"])
        left = days_left(until, today=today)
        if left < 0:
            left = 0                 # просрочка: велосипед ждут уже сегодня
        if left <= horizon:
            out[str(left)].append({**rental, "free_on": today + timedelta(days=left),
                                   "returning": rental.get("intent") == "return"})
    for rows in out.values():
        rows.sort(key=lambda r: not r["returning"])
    return out


def forecast_summary(free_now: int, soon: dict[str, list[dict]]) -> dict[str, int]:
    """Сколько свободно сейчас и сколько станет к каждому из ближайших дней."""
    out = {"now": free_now}
    total = free_now
    for day in sorted(soon, key=int):
        total += len(soon[day])
        out[day] = total
    return out


# ─────────────────── закупки основных средств ───────────────────

def purchase_no(number: int) -> str:
    return f"ЗАК-{int(number):06d}"


def months_between(since: Any, until: date) -> int:
    """Полных месяцев между датами. Меньше месяца - ноль."""
    if since is None:
        return 0
    start = since.date() if isinstance(since, datetime) else since
    months = (until.year - start.year) * 12 + until.month - start.month
    if until.day < start.day:
        months -= 1
    return max(months, 0)


def wear_percent(bike: dict, *, today: date | None = None) -> float | None:
    """Износ рамы в процентах срока службы. None - дата покупки не задана.

    Считается по сроку, а не по пробегу: срок службы задан у каждого
    велосипеда, а одометры половины парка переписывали не каждую выдачу.
    """
    bought = bike.get("purchased_on")
    if bought is None:
        return None
    months = int(bike.get("service_months") or 24)
    passed = months_between(bought, today or date.today())
    return float(round(min(100 * passed / max(months, 1), 100), 1))


def book_value(bike: dict, *, today: date | None = None) -> Decimal | None:
    """Остаточная стоимость рамы: цена минус износ, но не ниже остаточной.

    Ниже остаточной не падает намеренно: это цена, за которую велосипед
    уходит после списания, и амортизация её не съедает.
    """
    price = bike.get("purchase_price")
    if price is None:
        return None
    residual = to_money(bike.get("residual_price") or 0)
    wear = wear_percent(bike, today=today)
    if wear is None:
        return to_money(price)
    depreciable = max(to_money(price) - residual, Decimal(0))
    return to_money(residual + depreciable * Decimal(str(100 - wear)) / 100)


def asset_rows(bikes: Iterable[dict], *, today: date | None = None) -> list[dict]:
    """Парк как основные средства: износ и остаточная стоимость каждой единицы."""
    today = today or date.today()
    rows = []
    for bike in bikes:
        wear = wear_percent(bike, today=today)
        rows.append({**bike, "wear": wear, "book": book_value(bike, today=today),
                     "month": amortization_month(bike),
                     "worn_out": wear is not None and wear >= 100})
    rows.sort(key=lambda b: (-(b["wear"] or 0), str(b.get("code") or "")))
    return rows


def asset_summary(rows: Iterable[dict], *, today: date | None = None) -> dict[str, Any]:
    """Сводка по парку: вложено, осталось по балансу, сколько съедает в месяц."""
    del today
    rows = list(rows)
    live = [b for b in rows if b.get("status") not in ("sold", "written_off")]
    spent = sum((to_money(b.get("purchase_price") or 0) for b in rows), Decimal(0))
    book = sum((b["book"] or Decimal(0) for b in live), Decimal(0))
    month = sum((b["month"] or Decimal(0) for b in live), Decimal(0))
    wears = [b["wear"] for b in live if b["wear"] is not None]
    return {
        "bikes": len(rows), "live": len(live),
        "written_off": sum(1 for b in rows if b.get("status") == "written_off"),
        "sold": sum(1 for b in rows if b.get("status") == "sold"),
        "worn_out": sum(1 for b in live if b["worn_out"]),
        "spent": to_money(spent), "book": to_money(book), "month": to_money(month),
        "no_price": sum(1 for b in live if b.get("purchase_price") is None),
        "wear": float(round(sum(wears) / len(wears), 1)) if wears else None,
    }


def purchase_codes(raw: Any, *, limit: int = 100) -> tuple[list[str], str]:
    """Инвентарные номера партии из формы: список и ошибка.

    Номера вводятся как есть - их уже наклеили на рамы, и придумывать за
    оператора нумерацию значило бы разойтись с наклейками.
    """
    text = str(raw or "")
    parts = [p for p in re.split(r"[\s,;]+", text) if p]
    codes: list[str] = []
    for part in parts:
        check = check_code(part)
        if not check.ok:
            return [], check.error
        if check.value in codes:
            return [], f"Номер {check.value} в списке дважды."
        codes.append(check.value)
    if not codes:
        return [], "Укажите инвентарные номера через пробел или запятую."
    if len(codes) > limit:
        return [], f"За раз можно завести не больше {limit} велосипедов."
    return codes, ""


# ───────────────── справочники: точки, модели, батареи ─────────────────

BATTERY_STATUSES: dict[str, str] = {
    "available": "Свободна", "rented": "У клиента", "repair": "В ремонте",
    "maintenance": "На ТО", "lost": "Утеряна", "written_off": "Списана",
}
# Статусы, которые ставит оператор. rented - только через выдачу, как
# и у велосипеда: батарея уходит вместе с ним.
BATTERY_MANUAL_STATUSES = ("available", "repair", "maintenance", "lost",
                           "written_off")
BATTERY_OPERATIONAL = ("available", "rented", "repair", "maintenance")
# Циклов, после которых батарею пора смотреть: ёмкость к этому моменту
# заметно просела, и клиент начинает жаловаться на «не доезжает».
BATTERY_CYCLES_WARN = 500


def check_battery_status(raw: Any) -> Check:
    return check_choice(raw, BATTERY_STATUSES, what="Статус батареи")


def check_location(raw: Any, names: Iterable[str] | None = None) -> Check:
    """Точка выдачи. Список берётся из справочника; пусто - «не на точке»."""
    value = str(raw or "").strip()
    if not value:
        return Check(True, None)
    allowed = set(names) if names is not None else set(LOCATIONS)
    if value not in allowed:
        return Check(False, error="Точка: недопустимое значение.")
    return Check(True, value)


def battery_rows(batteries: Iterable[dict], *, today: date | None = None) -> list[dict]:
    """Список батарей с износом и признаком «пора смотреть»."""
    today = today or date.today()
    rows = []
    for battery in batteries:
        months = int(battery.get("service_months") or 15)
        passed = months_between(battery.get("purchased_on"), today)
        wear = (float(round(min(100 * passed / max(months, 1), 100), 1))
                if battery.get("purchased_on") else None)
        cycles = int(battery.get("cycles") or 0)
        rows.append({**battery, "wear": wear, "cycles": cycles,
                     "tired": cycles >= BATTERY_CYCLES_WARN
                     or (wear is not None and wear >= 100)})
    rows.sort(key=lambda b: (not b["tired"], str(b.get("code") or "")))
    return rows


def battery_summary(rows: Iterable[dict]) -> dict[str, int]:
    rows = list(rows)
    counts = {code: sum(1 for r in rows if r.get("status") == code)
              for code in BATTERY_STATUSES}
    counts["total"] = len(rows)
    counts["tired"] = sum(1 for r in rows if r["tired"])
    counts["operational"] = sum(1 for r in rows
                                if r.get("status") in BATTERY_OPERATIONAL)
    return counts


def battery_amortization(battery: dict) -> Decimal | None:
    """Сколько батарея съедает в месяц. None - цена не задана."""
    price = battery.get("purchase_price")
    if price is None:
        model_price = battery.get("model_price")
        if model_price is None:
            return None
        price = model_price
    months = max(int(battery.get("service_months") or 15), 1)
    return to_money(to_money(price) / months)


def compat_matrix(bike_models: Iterable[dict], battery_models: Iterable[dict],
                  pairs: Iterable[dict]) -> dict[str, Any]:
    """Матрица совместимости для экрана: строки - велосипеды, колонки - АКБ.

    Пустая клетка и есть «не подходит»: хранить отдельно «нет» значило бы
    отличать «проверили и не подходит» от «ещё не проверяли», а на двух
    точках это различие никому не нужно.
    """
    fits: dict[tuple[int, int], bool] = {}
    for pair in pairs:
        fits[(int(pair["bike_model_id"]), int(pair["battery_model_id"]))] = \
            bool(pair.get("primary_fit"))
    rows = []
    for bike in bike_models:
        cells = []
        for battery in battery_models:
            key = (int(bike["id"]), int(battery["id"]))
            cells.append({"battery": battery, "fits": key in fits,
                          "primary": fits.get(key, False)})
        rows.append({"bike": bike, "cells": cells})
    return {"rows": rows, "batteries": list(battery_models)}


# ─────────────────────────── трекеры ───────────────────────────
#
# Трекер отвечает на вопрос «где велосипед» без звонка клиенту. Данные
# приходят из StarLine, здесь - только арифметика над ними: онлайн или
# молчит, едет или стоит, и не пора ли поднять тревогу.
#
# Тревоги намеренно четыре. Каждая означает «садись и разбирайся», а не
# «прими к сведению»: список, в котором половина строк - шум, оператор
# перестаёт читать на второй неделе.

TRACKER_ALERTS: dict[str, str] = {
    "moving": "Едет без аренды",
    "offline": "Не выходит на связь",
    "alarm": "Тревога StarLine",
    "low_power": "Питание трекера",
}
# Сколько часов молчания считать пропажей связи. Полсуток - потому что
# велосипед ночует в подъезде, где связи нет, и час молчания ничего
# не значит.
TRACKER_OFFLINE_HOURS = 12
# Скорость, с которой «стоит» превращается в «едет». 5 км/ч - это уже
# не дрейф GPS у стены дома.
TRACKER_MOVING_SPEED = Decimal(5)
# Питание трекера: ниже этого он скоро замолчит совсем.
TRACKER_LOW_VOLTS = Decimal("11.5")
# Статусы велосипеда, при которых ехать он не должен.
TRACKER_PARKED_STATUSES = ("available", "reserved", "repair", "maintenance")
EARTH_KM = 6371.0088


def tracker_settings(settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Пороги тревог: из настроек, иначе значения по умолчанию."""
    settings = settings or {}

    def number(key: str, default: Decimal | int) -> Decimal:
        raw = str(settings.get(key) or "").strip().replace(",", ".")
        try:
            value = Decimal(raw)
        except (InvalidOperation, ValueError):
            return Decimal(default)
        return value if value > 0 else Decimal(default)

    return {"offline_hours": int(number("tracker_offline_hours",
                                        TRACKER_OFFLINE_HOURS)),
            "moving_speed": number("tracker_moving_speed", TRACKER_MOVING_SPEED),
            "low_volts": number("tracker_low_volts", TRACKER_LOW_VOLTS)}


def distance_km(lat1: float | None, lon1: float | None,
                lat2: float | None, lon2: float | None) -> float | None:
    """Расстояние по прямой между двумя точками, км.

    Формула гаверсинуса: на городских расстояниях ошибка сотые доли
    процента, а зависимостей не нужно никаких.
    """
    if None in (lat1, lon1, lat2, lon2):
        return None
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2) - math.radians(lon1)
    h = (math.sin(dlat / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2)
    return round(2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h))), 3)


def map_url(lat: float | None, lon: float | None) -> str | None:
    """Ссылка на карту с точкой. Яндекс: им пользуются на точках."""
    if lat is None or lon is None:
        return None
    return f"https://yandex.ru/maps/?pt={lon:.6f},{lat:.6f}&z=17&l=map"


def tracker_rows(trackers: Iterable[dict], *, now: datetime | None = None,
                 settings: Mapping[str, Any] | None = None) -> list[dict]:
    """Список трекеров с состоянием: молчит, едет, где на карте."""
    now = now or datetime.now(UTC)
    limits = tracker_settings(settings)
    rows = []
    for tracker in trackers:
        seen = tracker.get("last_seen")
        silent = None
        if seen is not None:
            silent = max((now - seen).total_seconds() / 3600, 0)
        speed = to_money(tracker.get("speed") or 0)
        offline = silent is None or silent >= limits["offline_hours"]
        rows.append({**tracker,
                     "silent_hours": round(silent, 1) if silent is not None else None,
                     "offline": offline,
                     "moving": speed >= limits["moving_speed"],
                     "low_power": (tracker.get("voltage") is not None
                                   and to_money(tracker["voltage"]) <= limits["low_volts"]),
                     "map": map_url(tracker.get("lat"), tracker.get("lon"))})
    # Сначала то, что требует внимания: тревога, движение, молчание.
    rows.sort(key=lambda t: (not t.get("alarm"), not t["moving"], not t["offline"],
                             str(t.get("bike_code") or "я" + str(t.get("device_id")))))
    return rows


def tracker_summary(rows: Iterable[dict]) -> dict[str, int]:
    rows = list(rows)
    return {"total": len(rows),
            "online": sum(1 for r in rows if not r["offline"]),
            "offline": sum(1 for r in rows if r["offline"]),
            "moving": sum(1 for r in rows if r["moving"]),
            "alarm": sum(1 for r in rows if r.get("alarm")),
            "free": sum(1 for r in rows if not r.get("bike_id"))}


def detect_alerts(row: Mapping[str, Any], *,
                  settings: Mapping[str, Any] | None = None) -> list[dict]:
    """Какие тревоги поднимает состояние трекера прямо сейчас.

    Велосипед в аренде ездит - это норма, и «едет» для него не тревога.
    Тревога - когда едет тот, что по учёту стоит на точке или в ремонте.
    """
    limits = tracker_settings(settings)
    out: list[dict] = []
    if row.get("alarm"):
        out.append({"kind": "alarm", "note": "Устройство подняло тревогу"})
    status = row.get("bike_status")
    if row["moving"] and (status in TRACKER_PARKED_STATUSES
                          or (row.get("bike_id") is None and row.get("lat") is not None)):
        speed = to_money(row.get("speed") or 0)
        where = BIKE_STATUSES.get(status, "не привязан к велосипеду")
        out.append({"kind": "moving",
                    "note": f"{speed:.0f} км/ч, по учёту — {where.lower()}"})
    if row["offline"] and row.get("bike_status") not in ("sold", "written_off"):
        hours = row["silent_hours"]
        out.append({"kind": "offline",
                    "note": (f"молчит {hours:.0f} ч" if hours is not None
                             else "ни одного выхода на связь")})
    if row["low_power"]:
        out.append({"kind": "low_power",
                    "note": f"питание {to_money(row['voltage'])} В, "
                            f"порог {limits['low_volts']} В"})
    for alert in out:
        alert["tracker_id"] = row.get("id")
        alert["bike_id"] = row.get("bike_id")
        alert["lat"], alert["lon"] = row.get("lat"), row.get("lon")
    return out


def map_points(rows: Iterable[dict]) -> list[dict]:
    """Точки для карты: только те, у кого есть координаты."""
    points = []
    for row in rows:
        if row.get("lat") is None or row.get("lon") is None:
            continue
        if row.get("alarm"):
            state = "alarm"
        elif row.get("bike_status") == "rented":
            state = "rented"
        elif row["offline"]:
            state = "offline"
        else:
            state = "parked"
        points.append({"lat": row["lat"], "lon": row["lon"], "state": state,
                       "code": row.get("bike_code") or row.get("alias")
                       or row.get("device_id"),
                       "title": tracker_title(row), "id": row.get("id")})
    return points


def tracker_title(row: Mapping[str, Any]) -> str:
    """Подпись точки на карте: что это и в каком состоянии."""
    parts = []
    if row.get("bike_code"):
        parts.append(f"№ {row['bike_code']}")
    elif row.get("alias"):
        parts.append(str(row["alias"]))
    if row.get("client_name"):
        parts.append(str(row["client_name"]))
    elif row.get("bike_status"):
        parts.append(BIKE_STATUSES.get(row["bike_status"], row["bike_status"]))
    if row.get("moving"):
        parts.append(f"{to_money(row.get('speed') or 0):.0f} км/ч")
    if row.get("offline") and row.get("silent_hours") is not None:
        parts.append(f"молчит {row['silent_hours']:.0f} ч")
    return " · ".join(parts)


# Карта: единственное место в панели, где нужен внешний скрипт. Адреса
# вынесены в настройки, потому что сервер стоит в России: если OSM или
# unpkg окажутся недоступны, владелец подменит их своей копией, не
# пересобирая образ.
MAP_JS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
MAP_CSS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
MAP_TILES = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
MAP_ATTRIBUTION = "© OpenStreetMap"
MAP_CENTER = (55.7887, 49.1221)                 # Казань, если точек нет


def map_config(settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or {}

    def value(key: str, default: str) -> str:
        return str(settings.get(key) or "").strip() or default

    return {"js": value("map_js", MAP_JS), "css": value("map_css", MAP_CSS),
            "tiles": value("map_tiles", MAP_TILES),
            "attribution": value("map_attribution", MAP_ATTRIBUTION),
            "lat": MAP_CENTER[0], "lon": MAP_CENTER[1]}


def track_distance(positions: Iterable[Mapping[str, Any]]) -> float:
    """Сколько накатано по журналу позиций, км.

    Это не одометр: точки приходят раз в несколько минут, и срезанные
    углы теряются. Для вопроса «он вообще ездит?» этого достаточно, а
    накат за аренду по-прежнему считается по пробегу с дисплея.
    """
    points = sorted(positions, key=lambda p: p["recorded_at"])
    total = 0.0
    for before, after in zip(points, points[1:], strict=False):
        step = distance_km(before["lat"], before["lon"], after["lat"], after["lon"])
        # Скачок на десятки километров - это перескок GPS, а не поездка.
        if step is not None and step < 5:
            total += step
    return round(total, 1)


def tracker_digest(alerts: Iterable[dict], limit: int = 10) -> str:
    """Тревоги трекеров одной сводкой в служебный чат."""
    alerts = list(alerts)
    if not alerts:
        return ""
    lines = [f"🛰 Трекеры: {len(alerts)}"]
    for alert in alerts[:limit]:
        where = ""
        if alert.get("lat") is not None:
            where = f" — {map_url(alert['lat'], alert['lon'])}"
        code = alert.get("bike_code") or alert.get("alias") or alert.get("device_id")
        lines.append(f"• {TRACKER_ALERTS.get(alert['kind'], alert['kind'])}: "
                     f"{code}{', ' + alert['note'] if alert.get('note') else ''}{where}")
    if len(alerts) > limit:
        lines.append(f"…и ещё {len(alerts) - limit}")
    return "\n".join(lines)


# ─────────────────────────── касса ───────────────────────────
#
# Наличные на точке живут отдельно от журнала клиента. Журнал отвечает
# «сколько должен клиент», смена - «сколько денег в ящике и сходится ли».
# Платёж наличными попадает в оба места; размен, инкассация и недостача -
# только в смену.

CASH_MOVE_KINDS: dict[str, str] = {"in": "Внесение", "out": "Изъятие"}
CASH_STATUSES: dict[str, str] = {"open": "Открыта", "closed": "Закрыта"}
# Расхождение, на которое смотрят. Полтинник в конце дня - это сдача и
# округление, а не пропажа; с трёхсот рублей уже разбираются.
CASH_DIFF_NOISE = Decimal(300)


def shift_no(number: int) -> str:
    return f"КСМ-{int(number):06d}"


def check_cash_move(raw: Any) -> Check:
    return check_choice(raw, CASH_MOVE_KINDS, what="Вид движения")


def shift_expected(shift: Mapping[str, Any], payments: Iterable[Mapping[str, Any]],
                   moves: Iterable[Mapping[str, Any]]) -> Decimal:
    """Сколько должно быть в ящике: размен + наличные платежи + внесения −
    изъятия. Возврат наличными приходит платежом с минусом и вычитается
    сам - отдельного правила для него не нужно."""
    total = to_money(shift.get("opening") or 0)
    for payment in payments:
        total += to_money(payment.get("amount") or 0)
    for move in moves:
        amount = to_money(move.get("amount") or 0)
        total += amount if move.get("kind") == "in" else -amount
    return to_money(total)


def shift_state(shift: Mapping[str, Any], payments: Iterable[Mapping[str, Any]],
                moves: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Состояние смены для карточки: ожидаемое, посчитанное, расхождение."""
    payments, moves = list(payments), list(moves)
    expected = shift_expected(shift, payments, moves)
    counted = (to_money(shift["counted"]) if shift.get("counted") is not None
               else None)
    diff = to_money(counted - expected) if counted is not None else None
    return {"expected": expected, "counted": counted, "diff": diff,
            "cash": to_money(sum(to_money(p.get("amount") or 0) for p in payments)),
            "inflow": to_money(sum(to_money(m["amount"]) for m in moves
                                   if m.get("kind") == "in")),
            "outflow": to_money(sum(to_money(m["amount"]) for m in moves
                                    if m.get("kind") == "out")),
            "payments": len(payments), "moves": len(moves),
            "open": shift.get("status") == "open",
            "big_diff": diff is not None and abs(diff) >= CASH_DIFF_NOISE}


def shift_rows(shifts: Iterable[dict]) -> list[dict]:
    """Список смен: открытые первыми, дальше по дате закрытия."""
    rows = [{**s, "big_diff": s.get("diff") is not None
             and abs(to_money(s["diff"])) >= CASH_DIFF_NOISE} for s in shifts]
    # Открытая смена - наверху, дальше свежие: сортировка в два прохода,
    # потому что дату по убыванию и флаг по возрастанию одним ключом
    # не выразить без выдумок про минус на datetime.
    rows.sort(key=lambda s: s.get("opened_at"), reverse=True)
    rows.sort(key=lambda s: s.get("status") != "open")
    return rows


# ─────────────────────────── банк ───────────────────────────
#
# На счёт падает не только аренда: выручка чужого ремонта, возвраты
# поставщиков, личные переводы владельца. Поэтому строка выписки не
# становится платежом сама - оператор подтверждает зачисление. Догадка
# у системы есть, решение - у человека.

BANK_STATUSES: dict[str, str] = {
    "new": "Не разобран", "matched": "Зачислен", "ignored": "Не наш",
}
# Насколько уверенной должна быть догадка, чтобы её можно было зачислять
# без человека (когда автозачисление вообще включено).
MATCH_SURE = "contract"
MATCH_REASONS: dict[str, str] = {
    "contract": "номер договора в назначении",
    "phone": "телефон в назначении",
    "name": "ФИО плательщика",
}


def bank_settings(settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or {}
    return {"auto_credit": str(settings.get("bank_auto_credit") or "") == "1"}


def digits(raw: Any) -> str:
    return re.sub(r"\D", "", str(raw or ""))


def match_payment(txn: Mapping[str, Any],
                  clients: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Кому из клиентов принадлежит поступление.

    Три признака по убыванию надёжности: номер договора в назначении,
    телефон там же, ФИО плательщика. Совпадение ФИО - именно догадка:
    однофамильцы среди курьеров не редкость, и зачислять по ней без
    человека нельзя.
    """
    if txn.get("direction") != "credit":
        return None
    purpose = str(txn.get("purpose") or "")
    flat = purpose.upper().replace(" ", "")
    phones = {digits(p) for p in re.findall(r"[\d\-()+ ]{10,}", purpose)}
    payer = normalize_name(txn.get("payer_name"))
    by_name = None
    for client in clients:
        contract = str(client.get("contract_no") or "").strip()
        if contract and contract.upper().replace(" ", "") in flat:
            return {"client": client, "reason": "contract"}
        phone = digits(client.get("phone"))
        if phone and any(phone[-10:] == p[-10:] for p in phones if len(p) >= 10):
            return {"client": client, "reason": "phone"}
        if payer and by_name is None and normalize_name(client.get("full_name")) == payer:
            by_name = {"client": client, "reason": "name"}
    return by_name


def normalize_name(raw: Any) -> str:
    """ФИО к сравнимому виду: «Иванов И. И.» и «ИВАНОВ ИВАН ИВАНОВИЧ»
    так и останутся разными, а регистр и лишние пробелы - нет."""
    return " ".join(str(raw or "").upper().replace("Ё", "Е").split())


def bank_rows(txns: Iterable[dict], clients: Iterable[dict] | None = None,
              *, settings: Mapping[str, Any] | None = None) -> list[dict]:
    """Выписка с догадкой, кому зачислить. Неразобранные - первыми."""
    clients = list(clients or [])
    rows = []
    for txn in txns:
        guess = (match_payment(txn, clients) if txn.get("status") == "new"
                 and clients else None)
        rows.append({**txn, "guess": guess,
                     "guess_reason": MATCH_REASONS.get((guess or {}).get("reason", ""), ""),
                     "sure": bool(guess) and guess["reason"] == MATCH_SURE})
    rows.sort(key=lambda t: t.get("booked_at"), reverse=True)
    rows.sort(key=lambda t: t.get("status") != "new")
    return rows


def bank_summary(rows: Iterable[dict]) -> dict[str, Any]:
    """Сводка по выписке. «Не разобрано» - только поступления: списание
    разбирать нечего, оно никому не зачисляется и висело бы вечно."""
    rows = list(rows)
    new = [r for r in rows if r.get("status") == "new"
           and r.get("direction") == "credit"]
    return {"total": len(rows), "new": len(new),
            "new_amount": to_money(sum(to_money(r["amount"]) for r in new
                                       if r.get("direction") == "credit")),
            "credited": to_money(sum(to_money(r["amount"]) for r in rows
                                     if r.get("status") == "matched")),
            "sure": sum(1 for r in rows if r.get("sure"))}


# ─────────────────────────── рассылки ───────────────────────────
#
# Рассылка - это не «написать всем». Курьеру с велосипедом на руках
# предложение «возвращайтесь» выглядит издевательством, а должнику
# скидка - поощрением. Поэтому у кампании есть аудитория, а у шаблона -
# подстановки: имя, велосипед, долг, «оплачено до».
#
# Подстановки - белым списком. Шаблон пишет человек, и опечатка в имени
# поля не должна ни падать на отправке, ни уезжать клиенту как есть.

TEMPLATE_FIELDS: dict[str, str] = {
    "name": "имя клиента",
    "phone": "телефон",
    "bike": "номер велосипеда в аренде",
    "tariff": "название тарифа",
    "price": "цена периода",
    "until": "оплачено до",
    "debt": "долг (без минуса)",
    "balance": "баланс",
    "contract": "номер договора",
    "pay_url": "ссылка на оплату",
}
CAMPAIGN_STATUSES: dict[str, str] = {
    "draft": "Черновик", "sending": "Отправляется",
    "done": "Отправлена", "cancelled": "Отменена",
}
SEND_STATUSES: dict[str, str] = {
    "queued": "В очереди", "sent": "Доставлено",
    "failed": "Не доставлено", "skipped": "Пропущен",
}
SEND_CHANNELS: dict[str, str] = {"tg": "Telegram", "max": "MAX"}
AUDIENCES: dict[str, str] = {
    "renting": "Сейчас в аренде",
    "debtors": "Должники",
    "expiring": "Истекает срок",
    "comeback": "Уехали и не вернулись",
    "all": "Все клиенты",
}
# Сколько дней «не вернулся» считать поводом написать. Меньше двух недель -
# человек просто в отпуске, больше трёх месяцев - он уже не курьер.
COMEBACK_FROM_DAYS = 14
COMEBACK_TO_DAYS = 90
# Пауза между сообщениями. Telegram разрешает больше, но рассылка - не
# гонка: при 5 в секунду двести человек получат сообщение за минуту,
# а бот не поймает ограничение на массовую отправку.
SEND_PAUSE = 0.2


def campaign_no(number: int) -> str:
    return f"РСЛ-{int(number):06d}"


def check_slug(raw: Any, *, what: str = "Код") -> Check:
    """Код шаблона: латиница, цифры, подчёркивание. Не инвентарный номер -
    проверка кода велосипеда подняла бы его в верхний регистр."""
    value = str(raw or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{2,32}", value):
        return Check(False, error=f"{what}: латиница, цифры и подчёркивание, "
                                  "от 2 до 32 символов.")
    return Check(True, value)


def check_audience(raw: Any) -> Check:
    return check_choice(raw, AUDIENCES, what="Аудитория")


def check_template_body(raw: Any, *, what: str = "Текст") -> Check:
    """Текст шаблона: непустой, в пределах лимита Telegram и без
    неизвестных подстановок."""
    text = str(raw or "").strip()
    if not text:
        return Check(False, error=f"{what}: пусто.")
    if len(text) > 3000:
        return Check(False, error=f"{what}: длиннее 3000 символов не уйдёт.")
    unknown = [f for f in re.findall(r"{([a-zA-Z_]+)}", text)
               if f not in TEMPLATE_FIELDS]
    if unknown:
        return Check(False, error=f"{what}: неизвестная подстановка "
                                  f"{{{unknown[0]}}}.")
    return Check(True, text)


def plain_text(html_text: str) -> str:
    """Разметку - долой: MAX её не понимает и покажет теги как текст."""
    text = re.sub(r"<br\s*/?>", "\n", str(html_text or ""))
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def first_name(full_name: Any) -> str:
    """Имя из ФИО: «Ахмедов Бехруз Шухратович» - «Бехруз».

    Обращение по фамилии в рассылке звучит как повестка, а по полному
    ФИО - как робот. Одного слова в карточке не бывает почти никогда,
    но если так - оно и пойдёт в текст.
    """
    parts = str(full_name or "").split()
    return parts[1] if len(parts) > 1 else (parts[0] if parts else "")


def template_context(client: Mapping[str, Any], rental: Mapping[str, Any] | None,
                     balance: Any, *, pay_url: str = "",
                     today: date | None = None) -> dict[str, str]:
    """Значения подстановок для одного клиента."""
    balance = to_money(balance or 0)
    summary = rental_summary(rental, balance, today=today or date.today()) \
        if rental else {}
    until = summary.get("covered_until")
    return {
        "name": first_name(client.get("full_name")),
        "phone": str(client.get("phone") or ""),
        "bike": f"№ {rental['bike_code']}" if rental and rental.get("bike_code")
        else "—",
        "tariff": str((rental or {}).get("tariff_name") or "—"),
        "price": money((rental or {}).get("price") or 0),
        "until": until.strftime("%d.%m.%Y") if until else "—",
        "debt": money(-balance) if balance < 0 else money(0),
        "balance": money(balance),
        "contract": str(client.get("contract_no")
                        or (rental or {}).get("contract_no") or "—"),
        "pay_url": pay_url,
    }


def render_template(body: str, values: Mapping[str, str]) -> str:
    """Подставить значения. Неизвестное поле остаётся как есть: шаблон
    проверяется при сохранении, и падать на отправке ему незачем."""
    def one(match: re.Match) -> str:
        return str(values.get(match.group(1), match.group(0)))

    return re.sub(r"{([a-zA-Z_]+)}", one, str(body or ""))


def send_channel(client: Mapping[str, Any]) -> str | None:
    """Куда писать клиенту. Telegram первым: там кабинет и уведомления."""
    if client.get("tg_id"):
        return "tg"
    if client.get("max_id"):
        return "max"
    return None


def pick_audience(code: str, clients: Iterable[dict],
                  rentals: Iterable[dict] | None = None, *,
                  today: date | None = None,
                  before_days: int = 2) -> list[dict]:
    """Кому уйдёт кампания. Возвращает клиентов с их арендой, если она есть.

    Заблокированные и чёрный список не получают ничего никогда: рассылка
    не повод напомнить о себе тому, кому отказали.
    """
    today = today or date.today()
    by_client: dict[int, dict] = {}
    for rental in rentals or []:
        if rental.get("status") == "active":
            by_client[int(rental["client_id"])] = rental
    out = []
    for client in clients:
        if client.get("status") != "active":
            continue
        if send_channel(client) is None:
            continue
        rental = by_client.get(int(client["id"]))
        balance = to_money(client.get("balance") or 0)
        if code == "renting" and rental is None:
            continue
        if code == "debtors" and balance >= 0:
            continue
        if code == "expiring":
            if rental is None:
                continue
            summary = rental_summary(rental, to_money(rental.get("balance") or 0),
                                     today=today)
            left = summary.get("days_left")
            if left is None or left > before_days:
                continue
        if code == "comeback":
            if rental is not None:
                continue
            last = client.get("last_rental_on")
            if last is None:
                continue
            days = (today - (last.date() if isinstance(last, datetime) else last)).days
            if not COMEBACK_FROM_DAYS <= days <= COMEBACK_TO_DAYS:
                continue
        out.append({**client, "rental": rental,
                    "channel": send_channel(client)})
    out.sort(key=lambda c: str(c.get("full_name") or ""))
    return out


def campaign_progress(sends: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    sends = list(sends)
    counts = {code: sum(1 for s in sends if s.get("status") == code)
              for code in SEND_STATUSES}
    counts["total"] = len(sends)
    counts["tg"] = sum(1 for s in sends if s.get("channel") == "tg")
    counts["max"] = sum(1 for s in sends if s.get("channel") == "max")
    counts["percent"] = (round(100 * (counts["sent"] + counts["failed"]
                                      + counts["skipped"]) / len(sends))
                         if sends else 0)
    return counts


def campaign_rows(campaigns: Iterable[dict]) -> list[dict]:
    rows = list(campaigns)
    rows.sort(key=lambda c: c.get("created_at"), reverse=True)
    rows.sort(key=lambda c: c.get("status") != "sending")
    return rows


# ───────────── простая электронная подпись (ПЭП) ─────────────
#
# Кнопка «подписываю» фиксирует согласие, но не доказывает его. Здесь -
# арифметика доказательства: код живёт минуты, попыток немного, ссылка
# протухает, а каждый шаг ложится в журнал вместе с хэшами документов.
#
# Сам код нигде не хранится: в базе только его хэш вместе с токеном
# ссылки. Один и тот же код в двух заявках даст разные хэши.

SIGN_STATUSES: dict[str, str] = {
    "new": "Ждёт клиента", "code": "Код отправлен",
    "signed": "Подписано", "cancelled": "Отменено",
}
SIGN_EVENTS: dict[str, str] = {
    "created": "Заявка создана", "opened": "Клиент открыл документы",
    "code_sent": "Код отправлен", "code_wrong": "Неверный код",
    "signed": "Документы подписаны", "cancelled": "Отменено",
    "expired": "Срок ссылки истёк",
}
SIGN_DOC_KINDS: dict[str, str] = {
    "esign": "Соглашение об ЭП",
    "contract": "Договор аренды",
    "consent": "Согласие на обработку персональных данных",
    "act_in": "Акт приёма-передачи",
    "act_out": "Акт возврата",
    "other": "Документ",
}
# Код живёт десять минут: за это время человек успевает прочитать
# сообщение, а перехваченный код успевает протухнуть.
SIGN_CODE_MINUTES = 10
# Попыток на код. Пять - это опечатка и ещё четыре, дальше нужен новый.
SIGN_MAX_ATTEMPTS = 5
# Ссылка живёт неделю: оператор отправляет её заранее, клиент подписывает
# на точке. Дольше держать открытую дверь незачем.
SIGN_LINK_DAYS = 7


def sign_no(number: int) -> str:
    return f"ПЭП-{int(number):06d}"


def make_sign_token() -> str:
    """Токен ссылки: 32 шестнадцатеричных символа из системного источника."""
    return secrets.token_hex(16)


def make_sign_code() -> str:
    """Код подтверждения: шесть цифр, включая ведущие нули."""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_sign_code(token: str, code: str) -> str:
    """Хэш кода вместе с токеном: одинаковый код в двух заявках даёт
    разные хэши, а из базы код не восстановить."""
    return hashlib.sha256(f"{token}:{code}".encode()).hexdigest()


def clean_sign_code(raw: Any) -> str:
    """Код из формы: цифры, всё остальное отбрасывается."""
    return re.sub(r"\D", "", str(raw or ""))[:6]


def sign_state(request: Mapping[str, Any], *,
               now: datetime | None = None) -> dict[str, Any]:
    """Состояние заявки: можно ли подписывать и сколько осталось."""
    now = now or datetime.now(UTC)
    expires = request.get("expires_at")
    code_at = request.get("code_at")
    code_left = None
    if code_at is not None:
        code_left = SIGN_CODE_MINUTES - (now - code_at).total_seconds() / 60
    attempts = int(request.get("attempts") or 0)
    signed = request.get("status") == "signed"
    cancelled = request.get("status") == "cancelled"
    expired = expires is not None and now >= expires
    return {
        "signed": signed, "cancelled": cancelled, "expired": expired,
        "open": not (signed or cancelled or expired),
        "code_valid": code_left is not None and code_left > 0
        and attempts < SIGN_MAX_ATTEMPTS,
        "code_left": round(code_left) if code_left is not None else None,
        "attempts_left": max(SIGN_MAX_ATTEMPTS - attempts, 0),
        "docs": list(request.get("docs") or []),
    }


def sign_docs_digest(docs: Iterable[Mapping[str, Any]]) -> str:
    """Хэш пакета: хэши документов по порядку, склеенные и хэшированные.

    По нему проверяют, что подписали именно этот набор, а не похожий:
    подмена одного документа меняет общий хэш.
    """
    joined = "\n".join(str(doc.get("sha256") or "") for doc in docs)
    return hashlib.sha256(joined.encode()).hexdigest()


def sign_rows(requests: Iterable[dict], *,
              now: datetime | None = None) -> list[dict]:
    """Список заявок: незакрытые первыми, свежие сверху."""
    now = now or datetime.now(UTC)
    rows = [{**r, **{k: v for k, v in sign_state(r, now=now).items()
                     if k != "docs"},
             "docs_count": len(r.get("docs") or [])} for r in requests]
    rows.sort(key=lambda r: r.get("created_at"), reverse=True)
    rows.sort(key=lambda r: not r["open"])
    return rows


def sign_summary(rows: Iterable[dict]) -> dict[str, int]:
    rows = list(rows)
    return {"total": len(rows),
            "open": sum(1 for r in rows if r.get("open")),
            "signed": sum(1 for r in rows if r.get("status") == "signed"),
            "expired": sum(1 for r in rows if r.get("expired")
                           and r.get("status") != "signed")}


# ────────────────────── приём оплаты ──────────────────────
#
# Счёт - это намерение, платёж - факт. Ссылка на оплату живёт в
# `crm.pay_orders` и в журнал не попадает: журнал сложится в баланс
# клиента, и выставленный счёт закрыл бы ему долг, которого никто не
# платил. В `ledger` счёт превращается ровно один раз - когда банк
# подтвердил оплату.

PAY_STATUSES: dict[str, str] = {
    "new": "Ссылка готовится",
    "sent": "Ждём оплату",
    "paid": "Оплачен",
    "failed": "Отказ банка",
    "cancelled": "Снят",
}
# Счёт ещё чего-то ждёт: такие опрашиваются у банка.
PAY_OPEN = ("new", "sent")
PAY_KINDS: dict[str, str] = {
    "link": "Ссылка клиенту",
    "auto": "Автосписание",
}
# Способы оплаты, которые оператор может выбрать в панели. Эквайринг
# отличается от остальных: его подтверждает банк, а не человек.
PAY_METHODS: dict[str, str] = {
    "online": "Эквайринг (онлайн)",
    "cash": "Наличные",
    "transfer": "Перевод",
}
# Какому виду записи в журнале отвечает способ приёма.
PAY_METHOD_LEDGER: dict[str, str] = {
    "online": "card", "cash": "cash", "transfer": "transfer",
}
# Ссылка живёт сутки: дольше банк её всё равно не держит, а счёт
# недельной давности в списке «ждём оплату» только мешает смотреть.
PAY_LINK_HOURS = 24
# Автосписание пробуем в этот час - после утреннего напоминания, чтобы
# клиент успел положить деньги сам, и задолго до конца рабочего дня.
AUTOCHARGE_HOUR = 12
# Сколько раз подряд банк может отказать, прежде чем карта снимается:
# три отказа - это не «на счету пусто сегодня», а мёртвая карта.
AUTOCHARGE_FAILS = 3


def pay_no(number: int) -> str:
    return f"СЧТ-{int(number):06d}"


def pay_methods(settings: Mapping[str, Any] | None = None) -> list[str]:
    """Какие способы приёма открыты оператору.

    Пусто в настройках - открыты все: пустая настройка на свежей базе
    не должна запрещать принимать деньги.
    """
    raw = str((settings or {}).get("pay_methods") or "").strip()
    if not raw:
        return list(PAY_METHODS)
    chosen = [m.strip() for m in raw.split(",") if m.strip() in PAY_METHODS]
    return chosen or list(PAY_METHODS)


def pay_settings(settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or {}

    def flag(key: str) -> bool:
        return str(settings.get(key) or "") == "1"

    def hour(key: str, default: int) -> int:
        try:
            value = int(str(settings[key]))
        except (KeyError, ValueError, TypeError):
            return default
        return value if 0 <= value <= 23 else default

    return {"methods": pay_methods(settings),
            "online": "online" in pay_methods(settings),
            "autocharge": flag("autocharge"),
            "autocharge_hour": hour("autocharge_hour", AUTOCHARGE_HOUR)}


def card_mask(raw: Any) -> str:
    """Четыре последние цифры карты. Больше не храним и не показываем."""
    tail = re.sub(r"\D", "", str(raw or ""))[-4:]
    return tail if len(tail) == 4 else ""


def card_title(card: Mapping[str, Any] | None) -> str:
    if not card:
        return ""
    mask = card_mask(card.get("mask"))
    expires = str(card.get("expires") or "").strip()
    return f"•••• {mask}{' · до ' + expires if expires else ''}" if mask else "карта привязана"


def pay_expired(order: Mapping[str, Any], *, now: datetime | None = None) -> bool:
    """Ссылка протухла: банк её уже не примет, опрашивать нечего."""
    if order.get("status") not in PAY_OPEN:
        return False
    created = order.get("created_at")
    if not isinstance(created, datetime):
        return False
    now = now or datetime.now(created.tzinfo or UTC)
    return (now - created) > timedelta(hours=PAY_LINK_HOURS)


def pay_rows(orders: Iterable[Mapping[str, Any]], *,
             now: datetime | None = None) -> list[dict]:
    """Счета для списка: сначала ждущие оплаты, потом всё остальное."""
    rows = []
    for order in orders:
        row = dict(order)
        row["status_title"] = PAY_STATUSES.get(row.get("status", ""), "—")
        row["kind_title"] = PAY_KINDS.get(row.get("kind", ""), "—")
        row["expired"] = pay_expired(row, now=now)
        rows.append(row)
    rows.sort(key=lambda r: r.get("created_at") or datetime.min, reverse=True)
    rows.sort(key=lambda r: r.get("status") not in PAY_OPEN)
    return rows


def pay_summary(orders: Iterable[Mapping[str, Any]],
                *, now: datetime | None = None) -> dict[str, Any]:
    """Сколько ждём и сколько уже пришло эквайрингом."""
    waiting = Decimal(0)
    paid = Decimal(0)
    counts = {"waiting": 0, "paid": 0, "failed": 0}
    for order in orders:
        amount = to_money(order.get("amount"))
        status = order.get("status")
        if status in PAY_OPEN and not pay_expired(order, now=now):
            waiting += amount
            counts["waiting"] += 1
        elif status == "paid":
            paid += amount
            counts["paid"] += 1
        elif status == "failed":
            counts["failed"] += 1
    return {**counts, "waiting_sum": to_money(waiting), "paid_sum": to_money(paid)}


def pay_purpose(client: Mapping[str, Any] | None,
                rental: Mapping[str, Any] | None = None) -> str:
    """Назначение платежа. Номер договора здесь не для красоты: по нему
    выписка банка потом узнаёт платёж и без нашего счёта."""
    contract = str((client or {}).get("contract_no") or "").strip()
    bike = str((rental or {}).get("bike_code") or "").strip()
    head = "Аренда велосипеда" + (f" № {bike}" if bike else "")
    return head + (f", договор {contract}" if contract else "")


def autocharge_due(rentals: Iterable[Mapping[str, Any]],
                   *, today: date | None = None,
                   cards: Mapping[int, Any] | None = None) -> list[dict]:
    """Кому сегодня можно списать с карты.

    Списываем только то, что уже начислено и не оплачено: автосписание
    закрывает долг, а не берёт вперёд «на всякий случай». Без карты и
    без долга аренда сюда не попадает.
    """
    today = today or date.today()
    cards = cards or {}
    due = []
    for rental in rentals:
        if rental.get("status") != "active":
            continue
        client_id = rental.get("client_id")
        if client_id is None or not cards.get(int(client_id)):
            continue
        debt = to_money(rental.get("balance"))
        if debt >= 0:
            continue
        due.append({"rental": rental, "client_id": int(client_id),
                    "amount": to_money(-debt)})
    due.sort(key=lambda r: -r["amount"])
    return due


# ────────────────────── уведомления ──────────────────────
#
# Каталог - здесь, в коде: уведомление не появляется «по настройке», у
# него всегда есть отправитель и повод в коде. В базе (`crm.notices`)
# лежит только то, что владелец поменял, поэтому новое уведомление
# показывается в панели само, а строки, которой нет, читаются как
# умолчания отсюда.
#
# `hour is None` - «сразу по событию»: такое уходит в момент события, и
# часа у него нет. Остальные проверяет суточный проход.

NOTICE_GROUPS: dict[str, str] = {
    "client": "Клиентам",
    "team": "Команде",
    "channel": "Канал для клиентов",
}
# Кому уходит: клиенту в личку, в служебный чат, в клиентский канал.
NOTICE_TARGETS: dict[str, str] = {
    "client": "Клиенту", "chat": "Служебный чат", "channel": "Канал",
}
NOTICE_STATUSES: dict[str, str] = {
    "sent": "Отправлено", "failed": "Не доставлено", "skipped": "Пропущено",
}
# Сколько держим историю отправок. Месяц отвечает на «почему клиент
# говорит, что ему не написали»; дальше вопрос уже не задают.
NOTICE_LOG_DAYS = 30

NOTICES: dict[str, dict[str, Any]] = {
    # ─ клиентам ─
    "rent_soon": {
        "group": "client", "target": "client", "hour": 14,
        "title": "Аренда истекает через N дней",
        "hint": "За сколько дней предупреждать - в настройках бота "
                "(REMIND_BEFORE_DAYS).",
    },
    "rent_due": {
        "group": "client", "target": "client", "hour": 9,
        "title": "Аренда истекает сегодня",
        "hint": "Последний день оплаченного периода.",
    },
    "rent_overdue": {
        "group": "client", "target": "client", "hour": 8,
        "title": "Просрочка — напомнить клиенту",
        "hint": "Оплаченный период кончился, деньги не пришли.",
    },
    "pay_credited": {
        "group": "client", "target": "client", "hour": None,
        "title": "Платёж зачислен",
        "hint": "Уходит сразу после зачисления, с новой датой «оплачено до».",
    },
    "autocharge_ok": {
        "group": "client", "target": "client", "hour": None,
        "title": "Списание с карты прошло",
        "hint": "Только при включённом автосписании.",
    },
    "autocharge_fail": {
        "group": "client", "target": "client", "hour": None,
        "title": "Списание с карты не прошло",
        "hint": "Банк отказал: на карте нет денег или она недействительна.",
    },
    "estimate_sent": {
        "group": "client", "target": "client", "hour": None,
        "title": "Смета на ремонт",
        "hint": "Перечень работ и цена с кнопками «согласен» и «не надо».",
    },
    "repair_invoice": {
        "group": "client", "target": "client", "hour": None,
        "title": "Счёт за ремонт",
        "hint": "Ссылка на оплату ремонта с чеком.",
    },
    "repair_ready": {
        "group": "client", "target": "client", "hour": None,
        "title": "Техника готова после ремонта",
        "hint": "Только по нарядам за счёт клиента: свой парк чинится молча.",
    },
    "maintenance_invite": {
        "group": "client", "target": "client", "hour": 10,
        "title": "Приглашение на ТО",
        "hint": "Аренда идёт дольше срока, а велосипед за это время "
                "в сервис не заезжал.",
        "params": {"after_days": 30},
    },
    "review_ask": {
        "group": "client", "target": "client", "hour": 10,
        "title": "Просьба оставить отзыв",
        "hint": "Клиент с нами дольше срока и не должен денег. "
                "Площадки - в настройках отзывов.",
        "params": {"after_days": 21},
    },
    # ─ команде ─
    "daily_digest": {
        "group": "team", "target": "chat", "hour": 20,
        "title": "Ежедневный отчёт по оплатам",
        "hint": "Кто платит, кто должен, у кого кончается аренда.",
    },
    "search_digest": {
        "group": "team", "target": "chat", "hour": 20,
        "title": "Кого пора искать",
        "hint": "Молчит, когда искать некого.",
    },
    "integrity": {
        "group": "team", "target": "chat", "hour": 20,
        "title": "Расхождения в данных",
        "hint": "Парк, аренды и наряды не сходятся между собой. "
                "Молчит, когда всё сходится.",
    },
    "bank_unmatched": {
        "group": "team", "target": "chat", "hour": None,
        "title": "Не разобранные поступления",
        "hint": "Деньги на счёте есть, а кому - неизвестно.",
    },
    "pay_paid": {
        "group": "team", "target": "chat", "hour": None,
        "title": "Оплачен счёт",
        "hint": "Оператор ждёт этого сообщения, чтобы выдать велосипед.",
    },
    "ref_spike": {
        "group": "team", "target": "chat", "hour": 20,
        "title": "Всплеск приглашений у одного агента",
        "hint": "Столько друзей за сутки от одного человека стоит "
                "посмотреть глазами. Система ничего не блокирует.",
    },
    "part_arrived": {
        "group": "team", "target": "chat", "hour": None,
        "title": "Пришла запчасть, которую ждал наряд",
        "hint": "Наряд стоял в «ждёт запчасть» и теперь может ехать дальше.",
    },
    "order_waiting": {
        "group": "team", "target": "chat", "hour": 10,
        "title": "Наряд молчит на согласовании",
        "hint": "Клиенту отправили смету, ответа нет дольше суток, "
                "а техника разобрана и стоит.",
    },
    "order_answer": {
        "group": "team", "target": "chat", "hour": None,
        "title": "Клиент ответил на смету",
        "hint": "Согласовал или отказался - техник ждёт именно этого.",
    },
    # ─ в канал ─
    "free_bikes": {
        "group": "channel", "target": "channel", "hour": 10,
        "title": "Свободные велосипеды в канал",
        "hint": "Свободный велосипед - прямой простой, а канал читают "
                "те самые курьеры.",
    },
}


def notice_defaults(code: str) -> dict[str, Any]:
    """Умолчания уведомления из каталога. Неизвестный код - пусто."""
    item = NOTICES.get(code)
    if item is None:
        return {}
    return {"code": code, "title": item["title"], "group": item["group"],
            "target": item["target"], "hint": item.get("hint", ""),
            "enabled": True, "at_hour": item["hour"], "at_minute": 0,
            "chat_id": None, "extra": dict(item.get("params") or {})}


def notice_settings(rows: Iterable[Mapping[str, Any]] | None = None
                    ) -> dict[str, dict[str, Any]]:
    """Каталог, поверх которого легли правки владельца.

    Строки, которой нет в базе, достаточно: она читается как умолчание.
    Строка с кодом не из каталога игнорируется - уведомление без кода в
    коде отправлять нечем.
    """
    stored = {str(r.get("code")): r for r in (rows or [])}
    out = {}
    for code in NOTICES:
        item = notice_defaults(code)
        row = stored.get(code)
        if row is not None:
            item["enabled"] = bool(row.get("enabled", True))
            if row.get("at_hour") is not None:
                item["at_hour"] = int(row["at_hour"])
            item["at_minute"] = int(row.get("at_minute") or 0)
            item["chat_id"] = str(row.get("chat_id") or "") or None
            extra = row.get("extra")
            if isinstance(extra, Mapping):
                # Белый список: параметры уведомления заданы каталогом,
                # чужие ключи из базы в работу не идут.
                item["extra"].update({k: v for k, v in extra.items()
                                      if k in item["extra"]})
            item["updated_by"] = row.get("updated_by")
            item["updated_at"] = row.get("updated_at")
        out[code] = item
    return out


def notice_param(setting: Mapping[str, Any] | None, key: str,
                 default: int = 0) -> int:
    """Целый параметр уведомления. Мусор в базе - к умолчанию каталога."""
    extra = (setting or {}).get("extra") or {}
    try:
        return int(extra[key])
    except (KeyError, ValueError, TypeError):
        return default


def notice_time(setting: Mapping[str, Any] | None) -> str:
    if not setting or setting.get("at_hour") is None:
        return "сразу"
    return f"{int(setting['at_hour']):02d}:{int(setting.get('at_minute') or 0):02d}"


def notice_due(setting: Mapping[str, Any] | None, now: datetime,
               done_on: date | None = None) -> bool:
    """Пора ли отправлять уведомление по расписанию.

    Не «ровно в этот час», а «в этот час или позже, если сегодня ещё не
    отправляли»: бота перезапускают среди дня, и привязка к минуте молча
    съедала бы уведомления за целые сутки.
    """
    if not setting or not setting.get("enabled"):
        return False
    hour = setting.get("at_hour")
    if hour is None:
        return False                       # «сразу» расписанием не ловится
    if done_on == now.date():
        return False
    minutes_now = now.hour * 60 + now.minute
    return minutes_now >= int(hour) * 60 + int(setting.get("at_minute") or 0)


def notice_rows(settings: Mapping[str, Mapping[str, Any]],
                counts: Mapping[str, int] | None = None) -> dict[str, list[dict]]:
    """Уведомления по группам - в том порядке, в каком они в каталоге."""
    counts = counts or {}
    out: dict[str, list[dict]] = {group: [] for group in NOTICE_GROUPS}
    for code, item in settings.items():
        row = dict(item)
        row["time"] = notice_time(item)
        row["target_title"] = NOTICE_TARGETS.get(item["target"], "—")
        row["sent"] = int(counts.get(code, 0))
        out.setdefault(item["group"], []).append(row)
    return out


# Площадки для отзывов: кнопки в сообщении клиенту и в кабинете. Пустая
# ссылка - площадка не показывается: пустая кнопка хуже, чем её отсутствие.
REVIEW_SITES: dict[str, str] = {
    "review_yandex": "Яндекс.Карты",
    "review_2gis": "2ГИС",
    "review_avito": "Авито",
}


def review_links(settings: Mapping[str, Any] | None = None) -> list[dict[str, str]]:
    settings = settings or {}
    out = []
    for key, title in REVIEW_SITES.items():
        url = str(settings.get(key) or "").strip()
        if url.startswith(("http://", "https://")):
            out.append({"key": key, "title": title, "url": url})
    return out


# ────────────────────── смета и счёт за ремонт ──────────────────────
#
# Смета - перечень работ с ценой, а не число в поле. Отправили клиенту -
# наряд встал в «на согласовании» и ждёт ответа; техник за разобранную
# технику не берётся, пока клиент не сказал «да».

# Сколько наряд может молчать на согласовании, прежде чем это станет
# заметно. Сутки: за день клиент кнопку видит, а техника стоит зря.
ESTIMATE_SILENT_DAYS = 1


def estimate_lines(items: Iterable[Mapping[str, Any]]) -> str:
    """Смета словами клиента: что делаем и сколько это стоит.

    Себестоимость сюда не идёт никогда: клиенту незачем знать, во что
    запчасть обошлась нам, а нам - объяснять разницу.
    """
    lines = []
    for item in items:
        qty = int(item.get("qty") or 1)
        price = to_money(item.get("price")) * qty
        title = str(item.get("title") or "работа")
        lines.append(f"• {title}"
                     + (f" × {qty}" if qty > 1 else "")
                     + f" — {money(price)}")
    return "\n".join(lines)


def order_totals_client(items: Iterable[Mapping[str, Any]]) -> Decimal:
    """Сколько к оплате клиенту. Отдельно от order_totals: там ещё и
    себестоимость, а в смету она не идёт."""
    return to_money(sum(to_money(i.get("price")) * int(i.get("qty") or 1)
                        for i in items))


def estimate_state(order: Mapping[str, Any] | None,
                   *, now: datetime | None = None) -> dict[str, Any]:
    """Где смета: не отправляли, ждём ответа, согласована, отказ."""
    order = order or {}
    sent = order.get("estimate_sent_at")
    approved = order.get("approved_at")
    declined = order.get("declined_at")
    if declined:
        stage, title = "declined", "Клиент отказался"
    elif approved:
        stage, title = "approved", "Согласована"
    elif sent:
        stage, title = "waiting", "Ждём ответа клиента"
    else:
        stage, title = "draft", "Не отправлена"
    silent = 0
    if stage == "waiting" and isinstance(sent, datetime):
        now = now or datetime.now(sent.tzinfo or UTC)
        silent = max((now - sent).days, 0)
    return {"stage": stage, "title": title, "silent_days": silent,
            "too_silent": silent >= ESTIMATE_SILENT_DAYS,
            "by": order.get("approved_by")}


def invoice_state(order: Mapping[str, Any] | None,
                  invoices: Iterable[Mapping[str, Any]] | None = None
                  ) -> dict[str, Any]:
    """Выставлен ли счёт за ремонт и оплачен ли он.

    Оплата ремонта в журнал аренды не попадает - поэтому «оплачено»
    здесь читается с наряда (`paid_at`), а не из баланса клиента.
    """
    order = order or {}
    rows = [i for i in (invoices or [])
            if str(i.get("status") or "") != "cancelled"]
    paid = order.get("paid_at") is not None
    if paid:
        return {"stage": "paid", "title": "Оплачен", "invoice": None}
    waiting = next((i for i in rows if i.get("status") in PAY_OPEN), None)
    if waiting is not None:
        return {"stage": "sent", "title": "Счёт выставлен", "invoice": waiting}
    return {"stage": "none", "title": "Ещё не выставлен", "invoice": None}


def orders_unpaid(orders: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Сколько закрытых клиентских нарядов ещё не оплачено - в итог списка."""
    rows = [o for o in orders
            if o.get("payer") == "client" and o.get("status") == "done"
            and not o.get("paid_at")]
    return {"count": len(rows),
            "sum": to_money(sum(to_money(o.get("total")) for o in rows))}


# ────────────────────── баллы ──────────────────────
#
# Баллы - не деньги, а наша скидка. В журнале они живут видом `bonus`,
# и в средний чек не попадают никогда: чек считается по `payment`.

BONUS_KINDS: dict[str, str] = {
    "referral": "Агенту за друга",
    "friend": "Новому клиенту по приглашению",
    "review": "За опубликованный отзыв",
    "manual": "Начислено руками",
}
# Бонус другу и за отзыв по умолчанию нулевые: обещать клиенту то, чего
# владелец не назначал, нельзя, а вот бонус агенту программа платила и
# раньше - его умолчание остаётся.
FRIEND_BONUS_DEFAULT = Decimal("0.00")
REVIEW_BONUS_DEFAULT = Decimal("0.00")
# Сколько друзей у одного агента за сутки - это уже не «рассказал
# знакомым». Пятеро курьеров в один день от одного человека бывают, но
# посмотреть на них стоит.
REF_SPIKE_DEFAULT = 5


def bonus_settings(raw: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Настройки баллов поверх настроек реферальной программы."""
    raw = raw or {}

    def money_or(key: str, default: Decimal) -> Decimal:
        try:
            value = to_money(Decimal(str(raw[key])))
        except (KeyError, ArithmeticError, ValueError, TypeError):
            return default
        return value if value >= 0 else default

    def count_or(key: str, default: int) -> int:
        try:
            value = int(str(raw[key]))
        except (KeyError, ValueError, TypeError):
            return default
        return value if 1 <= value <= 100 else default

    return {**ref_settings(raw),
            "friend_bonus": money_or("ref_friend_bonus", FRIEND_BONUS_DEFAULT),
            "review_bonus": money_or("review_bonus", REVIEW_BONUS_DEFAULT),
            # Платить только за того, кого в базе ещё не было: иначе
            # «приведи друга» превращается в «перезаведи соседа».
            "new_only": str(raw.get("ref_new_only", "1")) not in ("0", "", "false"),
            "spike": count_or("ref_spike", REF_SPIKE_DEFAULT)}


def bonus_promise(settings: Mapping[str, Any]) -> str:
    """Что бот обещает другу. Суммы нет - и обещать нечего: «условия
    уточняйте у менеджера» честнее выдуманного числа."""
    agent = to_money(settings.get("bonus"))
    friend = to_money(settings.get("friend_bonus"))
    if agent <= 0 and friend <= 0:
        return ""
    parts = []
    if friend > 0:
        parts.append(f"вам {money(friend)}")
    if agent > 0:
        parts.append(f"другу {money(agent)}")
    return " и ".join(parts)


def is_new_friend(client: Mapping[str, Any] | None,
                  referral: Mapping[str, Any] | None) -> bool:
    """Правда ли друг - новый человек, а не давний клиент.

    Карточка старого клиента заведена раньше перехода по ссылке: телефон
    в базе уникален, поэтому вернувшийся получает ту же карточку, и
    сравнение дат отвечает точно.
    """
    made = (client or {}).get("created_at")
    clicked = (referral or {}).get("created_at")
    if not isinstance(made, datetime) or not isinstance(clicked, datetime):
        return True                      # дат нет - не наказываем клиента
    return made >= clicked - timedelta(minutes=5)


def ref_spikes(referrals: Iterable[Mapping[str, Any]], *,
               limit: int, today: date | None = None) -> list[dict]:
    """Агенты, у которых за сутки подозрительно много друзей.

    Система ничего не блокирует: она показывает. Заблокировать честного
    курьера, который привёл бригаду, дороже, чем разобрать пять строк
    руками.
    """
    today = today or date.today()
    by_agent: dict[int, int] = {}
    for ref in referrals:
        made = ref.get("created_at")
        day = made.date() if isinstance(made, datetime) else made
        if day == today and ref.get("agent_id") is not None:
            by_agent[int(ref["agent_id"])] = by_agent.get(int(ref["agent_id"]), 0) + 1
    rows = [{"agent_id": agent, "friends": n}
            for agent, n in by_agent.items() if n >= limit]
    rows.sort(key=lambda r: -r["friends"])
    return rows


def bonus_totals(entries: Iterable[Mapping[str, Any]],
                 payments: Any = None) -> dict[str, Any]:
    """Сколько роздано баллами и какая это доля от оплат.

    Доля нужна, чтобы увидеть, не превратилась ли программа в раздачу:
    «0,1 % от оплат месяца» - это скидка, «20 %» - это уже бизнес-модель.
    """
    rows = list(entries)                 # генератор пройти можно один раз
    total = to_money(sum(to_money(e.get("amount")) for e in rows))
    paid = to_money(payments or 0)
    share = (total * 100 / paid) if paid > 0 else Decimal(0)
    by_kind: dict[str, Decimal] = {}
    for entry in rows:
        code = str(entry.get("kind") or "manual")
        by_kind[code] = to_money(by_kind.get(code, Decimal(0))
                                 + to_money(entry.get("amount")))
    return {"total": total, "share": share.quantize(Decimal("0.1")),
            "by_kind": by_kind, "count": len(rows)}


# ────────────────── ввод техники в эксплуатацию ──────────────────
#
# Сверка - это не одна галочка «всё хорошо», а отметка по каждому полю
# паспорта. Переписать номер из накладной сверкой не назвать: для того
# и фотография, которую можно потребовать настройкой.

BIKE_PASSPORT: dict[str, str] = {
    "model": "Модель",
    "code": "Номер наклейки",
    "frame_no": "Серийный номер (на раме)",
    "plate_no": "Госномер",
    "tracker": "Трекер",
}
# Какие поля требуют фотографии, когда владелец её потребовал. Модель
# фотографировать незачем: её видно и так.
BIKE_PHOTO_FIELDS = ("frame_no", "plate_no")


def bike_check_settings(settings: Mapping[str, Any] | None = None) -> dict[str, bool]:
    settings = settings or {}
    return {
        # Выключено - список полей остаётся подсказкой, но кнопка ввода
        # в эксплуатацию не заперта.
        "required": str(settings.get("bike_check_required", "1"))
        not in ("0", "", "false"),
        "photo": str(settings.get("bike_photo_required", "0"))
        not in ("0", "", "false"),
    }


def bike_field_value(bike: Mapping[str, Any], field: str) -> str:
    """Что сверяем в этом поле. Трекер - это «привязан или нет»."""
    if field == "tracker":
        return "привязан" if bike.get("tracker_id") or bike.get("tracker_ok") else ""
    return str(bike.get(field) or "").strip()


def bike_checks(bike: Mapping[str, Any],
                settings: Mapping[str, Any] | None = None) -> list[dict]:
    """Строки сверки паспорта: что за поле, что в нём и сверено ли."""
    checked = bike.get("checked")
    checked = checked if isinstance(checked, Mapping) else {}
    photo_needed = bike_check_settings(settings)["photo"]
    rows = []
    for field, title in BIKE_PASSPORT.items():
        mark = checked.get(field)
        mark = mark if isinstance(mark, Mapping) else {}
        value = bike_field_value(bike, field)
        needs_photo = photo_needed and field in BIKE_PHOTO_FIELDS
        rows.append({
            "field": field, "title": title, "value": value,
            "filled": bool(value),
            "at": mark.get("at"), "by": mark.get("by"), "photo": mark.get("photo"),
            "needs_photo": needs_photo,
            # Сверено = отметка есть, поле заполнено, и снимок приложен,
            # если владелец его потребовал.
            "ok": bool(mark.get("at")) and bool(value)
            and (not needs_photo or bool(mark.get("photo"))),
        })
    return rows


def bike_check_state(bike: Mapping[str, Any],
                     settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    rows = bike_checks(bike, settings)
    left = [r["title"] for r in rows if not r["ok"]]
    required = bike_check_settings(settings)["required"]
    return {"rows": rows, "left": left, "done": not left,
            "required": required,
            # Выпускать можно, когда сверено всё - или когда владелец
            # сверку не требует: список тогда остаётся подсказкой.
            "can_commission": (not left) or not required,
            "new": str(bike.get("status") or "") == "new"}


def check_plate(raw: Any) -> Check:
    """Госномер: короткая строка, регистр приводим к верхнему.

    Формат не проверяем: у велосипедов и мопедов таблички бывают разные,
    и отвергнуть настоящий номер из-за нашего представления о нём хуже,
    чем принять опечатку - её видно на фотографии.
    """
    value = " ".join(str(raw or "").split()).upper()
    if not value:
        return Check(True, None)
    if len(value) > 24:
        return Check(False, error="Госномер: не длиннее 24 символов.")
    return Check(True, value)


# ────────────────── свои шаблоны документов ──────────────────
#
# У каждого вида документа всегда включён ровно один шаблон: наш или
# ваш. Выключить оба нельзя - выдачу тогда нечем оформить.

DOC_TEMPLATES: dict[str, dict[str, str]] = {
    "esign": {"title": "Соглашение об ЭП",
              "hint": "Собирается кодом, а не файлом: текст соглашения "
                      "хранится в самой заявке на подпись."},
    "contract": {"title": "Договор аренды",
                 "hint": "Основной документ выдачи."},
    "act_in": {"title": "Акт приёма-передачи",
               "hint": "Что именно отдали клиенту."},
    "consent": {"title": "Согласие на обработку ПДн",
                "hint": "Приложение к договору, подписывается вместе с ним."},
    "act_out": {"title": "Акт возврата",
                "hint": "Чем закрывается аренда."},
    "buyout": {"title": "Договор выкупа",
               "hint": "Аренда с правом выкупа."},
}
# Соглашение об ЭП файлом не задаётся: его текст собирается кодом и
# хранится в заявке ровно в том виде, в каком его приняли. Подменить его
# файлом значило бы сломать доказательство.
DOC_CODE_ONLY = ("esign",)
# Свой шаблон - docx: подстановки заполняются в исходном файле юриста,
# и pdf или odt так не заполнить.
DOC_SUFFIX = ".docx"
DOC_MAX_BYTES = 10 * 1024 * 1024

COMPANY_MARKS: dict[str, str] = {
    "signature": "Подпись",
    "stamp": "Печать",
}
MARK_SUFFIXES = (".png",)
MARK_MAX_BYTES = 2 * 1024 * 1024


def doc_rows(stored: Iterable[Mapping[str, Any]] | None = None) -> list[dict]:
    """Виды документов с тем, чей шаблон сейчас включён."""
    rows_by_kind: dict[str, list[dict]] = {}
    for row in stored or []:
        rows_by_kind.setdefault(str(row.get("kind")), []).append(dict(row))
    out = []
    for kind, item in DOC_TEMPLATES.items():
        own = rows_by_kind.get(kind, [])
        active = next((r for r in own if r.get("active")), None)
        out.append({
            "kind": kind, "title": item["title"], "hint": item["hint"],
            "code_only": kind in DOC_CODE_ONLY,
            "mine": active is not None, "active": active,
            "archive": [r for r in own if not r.get("active")],
            "source": "ваш шаблон" if active else "наш шаблон",
        })
    return out


def doc_summary(stored: Iterable[Mapping[str, Any]] | None = None) -> dict[str, int]:
    rows = doc_rows(stored)
    kinds = [r for r in rows if not r["code_only"]]
    return {"total": len(kinds),
            "mine": sum(1 for r in kinds if r["mine"])}


def doc_filename(kind: str, number: int) -> str:
    """Имя файла на диске собираем сами: имя из браузера - чужая строка,
    и «../../» в ней не шутка."""
    return f"{kind}-{int(number):04d}{DOC_SUFFIX}"


def mark_filename(kind: str) -> str:
    return f"{kind}.png"
