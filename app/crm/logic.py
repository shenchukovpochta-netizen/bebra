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
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

# ─────────────────────────── словари ───────────────────────────

KINDS = {
    "payment": "Платёж",
    "charge": "Начисление",
    "fine": "Штраф / ремонт",
    "refund": "Возврат клиенту",
    "adjust": "Корректировка",
}
# Знак суммы по виду записи: оператор вводит число без знака, знак
# ставит система. Корректировка - единственная со свободным знаком.
KIND_SIGN = {"payment": 1, "charge": -1, "fine": -1, "refund": -1, "adjust": 0}

METHODS = {
    "sbp": "СБП", "cash": "Наличные", "card": "Карта",
    "transfer": "Перевод", "other": "Другое",
}

BIKE_STATUSES = {
    "available": "Свободен", "rented": "В аренде", "repair": "В ремонте",
    "reserved": "Забронирован", "lost": "Утерян", "sold": "Продан",
}
# Статусы, которые ставит оператор руками. rented - только через аренду.
BIKE_MANUAL_STATUSES = ("available", "repair", "reserved", "lost", "sold")

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
        who = r.get("full_name") or f"клиент #{r.get('client_id')}"
        bike = r.get("bike_code") or ""
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

def rental_from_issue(issue: dict | None, rent_from: date | None,
                      rent_until: date | None, *, today: date) -> dict[str, Any]:
    """Условия аренды из формы выдачи оператора (issue_data бота).

    Цена - число из строки «3000 qr», период - длина срока «03.08 - 10.08».
    Срока нет - берётся неделя: это самый частый тариф, и оператор поправит
    в панели. billing manual: начисления такой аренде делает не календарь,
    а события бота (выдача, продление).
    """
    issue = issue or {}
    digits = re.sub(r"[^\d]", "", str(issue.get("rent_price") or ""))
    price = to_money(int(digits)) if digits else Decimal(0)
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
