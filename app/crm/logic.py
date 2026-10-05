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
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, unquote_plus, urlsplit
from zoneinfo import ZoneInfo

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
# Заметка платежа, принятого мастером выдачи. Обучение отличает по ней
# оплату первого периода от пополнения баланса (app/crm/learning.py).
ISSUE_PAY_NOTE = "При выдаче № {code}"

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
# Из каких статусов велосипед уходит в аренду по акту, подписанному в боте.
# Отказать бот не может - велосипед уже у клиента, и «в ремонте» или
# «забронирован» в учёте значит забытую отметку. На сборке, утерянный,
# проданный и списанный - не тот велосипед или уже не наш: аренда
# заводится без него, расхождение видно в панели. Панель выдаёт только
# свободный (service.open_rental и условие в create_rental).
BOT_ISSUE_STATUSES = IDLE_STATUSES
# Точки выдачи живут в справочнике crm.locations; это - запасной список на
# пустой справочник (тестовая заглушка), и читает его только point_choices.
# Пусто у велосипеда - «не на точке»: в аренде он стоит на точке аренды.
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

MAX_AMOUNT = Decimal("10000000")
MAX_PERIOD_DAYS = 366
NAME_LIMIT = 120
NOTE_LIMIT = 2000
CODE_LIMIT = 40

# ─────────────────────────── деньги ───────────────────────────

CENT = Decimal("0.01")


# Одна функция на бота и на панель: разъехавшееся «сегодня» у двух
# процессов одной базы - это разные даты в договоре и в журнале.
local_date = bot_logic.local_date


def to_money(value: Any) -> Decimal:
    """Любое число -> Decimal с двумя знаками. Не для пользовательского ввода."""
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


# Неразрывный пробел для сумм: между разрядами и перед «₽». Именно U+00A0,
# а не узкий U+202F: ширина та же, что у обычного пробела (вёрстка плиток и
# таблиц не сдвигается), так группирует разряды русская локаль CLDR, тот же
# знак уже ставит сумме бот (app/logic.py, money), поиск по странице в
# браузере находит «2 107» и с ним, а Telegram, шрифты docx и старые
# Android рисуют его везде - узкий там местами становится квадратом.
NBSP = "\u00a0"


def money(value: Any) -> str:
    """Сумма для человека: 3000 -> «3 000 ₽», -428.5 -> «−428,50 ₽».

    Целые - без копеек: цены проката круглые, и «3 000,00» только шумит.
    Пробелы неразрывные (NBSP): перенос посреди «2 107 700» или отдельно
    «₽» на плитке сводки читается как опечатка. Только для глаз: выгрузки
    xlsx и csv кладут в ячейку число (to_money), а не эту строку.
    """
    if value is None:
        return "—"
    amount = to_money(value)
    sign = "−" if amount < 0 else ""
    amount = abs(amount)
    whole = int(amount)
    cents = int((amount - whole) * 100)
    text = f"{whole:,}".replace(",", NBSP)
    if cents:
        text += f",{cents:02d}"
    return f"{sign}{text}{NBSP}₽"


def cents(value: Any) -> int:
    """Сумма в копейках целым числом - для кнопок, где место дорого."""
    return int(to_money(value) * 100)


def money_signed(value: Any) -> str:
    """Как money, но с явным плюсом у прихода - для журнала."""
    amount = to_money(value)
    return ("+" if amount > 0 else "") + money(amount)


def parse_money(raw: Any) -> Decimal | None:
    """«3 000», «3000,50», «3000.5 ₽» -> Decimal. None, если это не сумма.

    Минус допускается: корректировка бывает в обе стороны. Знак у остальных
    видов записей всё равно переопределит signed_amount.
    """
    text = str(raw or "").strip()
    for space in (" ", NBSP, "\u202f", "\u2009"):
        text = text.replace(space, "")
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


def manual_reminder_kind(summary: Mapping[str, Any] | None) -> str | None:
    """Какое напоминание слать по кнопке оператора - без расписания.

    Расписание шлёт в свои дни, а оператор жмёт, когда решил сам:
    просрочка - «просрочка», сегодня - «сегодня», иначе «истекает через
    N дней», сколько бы их ни было. None - аренда не идёт.
    """
    if not summary or not summary.get("active"):
        return None
    left = summary.get("days_left")
    if left is None:
        return None
    if left < 0:
        return REMIND_OVERDUE
    return REMIND_DUE if left == 0 else REMIND_SOON


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
    kind = reminder_kind(days_left(until, today=today), before_days=before_days)
    # Клиент сказал «сдаю» про этот срок: «пополните баланс - и аренда
    # продолжится» ему ни к чему. Просрочка всё равно напоминается - не
    # сдал, значит, должен.
    if (kind in (REMIND_SOON, REMIND_DUE) and rental.get("intent") == "return"
            and rental.get("intent_until") == until):
        return None
    return kind


def bot_reminds(rental: Mapping[str, Any], bot_tg_ids: set[int] | frozenset[int]) -> bool:
    """О сроке этой аренды клиенту напоминает сам бот, и CRM молчит.

    Аренда, которую завёл бот по акту приёма, - ручная (`rental_from_issue`),
    и у того же клиента в bot.users идёт её срок `rent_until`: бот шлёт
    «заканчивается», «последний день» и «просрочка» сам (tasks.remind_once).
    CRM слала бы о том же своё - два потока одних напоминаний с разными
    кнопками. Остаётся бот: срок такой аренды ведёт он (оператор называет
    его формой выдачи и продления, периоды в журнал кладут события бота),
    его напоминание знает «клиент уже попросил закрыть» и «ждёт оплаты
    продления» и ведёт кнопкой в это продление. Ручная, заведённая в
    панели клиенту без аренды в боте, и аренда по тарифу напоминаются из
    CRM, как раньше. `bot_tg_ids` - tg_id идущих аренд бота
    (Database.active_rentals).
    """
    if rental.get("billing") != "manual" or not rental.get("tg_id"):
        return False
    try:
        tg_id = int(rental["tg_id"])
    except (TypeError, ValueError):
        return False
    return tg_id in bot_tg_ids


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
            # Ручное начисление не уходит в минус, пока период не записан:
            # «долг 0 ₽» при просрочке сбивал бы с толку. Сумма - как у
            # rental_summary: долг, а без долга - цена неначисленного периода.
            bal = to_money(r.get("balance", 0))
            owed = (f"долг {money(-bal)}" if bal < 0
                    else f"к оплате {money(r['price'])}")
            debtors.append(
                f"⚠️ {who}{tail} — {owed}, "
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


# Окно даты начала аренды. Без него опечатка в годе (0026, 2016 вместо
# 2026) начисляла сотни периодов разом - и ещё больше каждым дневным
# проходом. Задним числом оформляют забытую выдачу, вперёд - выдачу по
# заявке; месяца в обе стороны хватает с запасом.
RENTAL_BACKDATE_DAYS = 31
RENTAL_AHEAD_DAYS = 31


def rental_start_problem(started: date, today: date) -> str | None:
    """Почему эту дату начала аренды не принять; None - дата годится."""
    if started < today - timedelta(days=RENTAL_BACKDATE_DAYS):
        return (f"Дата начала {started:%d.%m.%Y} раньше, чем {RENTAL_BACKDATE_DAYS} дн. "
                "назад, — проверьте год: с ней сразу начислились бы все прошедшие периоды.")
    if started > today + timedelta(days=RENTAL_AHEAD_DAYS):
        return (f"Дата начала {started:%d.%m.%Y} дальше, чем через {RENTAL_AHEAD_DAYS} дн., "
                "— проверьте год.")
    return None


def rental_close_problem(closed: date, started: date | None, today: date) -> str | None:
    """Почему эту дату возврата не принять; None - дата годится.

    Возврат - не позже сегодня и не раньше начала аренды. Опечатка в годе
    (31.12.9999) ложилась в базу бесконечностью, и разность «возврат минус
    начало» в списке клиентов, риске и выписке падала на каждом открытии.
    Аренду с началом в будущем (выдача по заявке) отменяют сегодняшним
    днём: другой даты у неё нет.
    """
    if closed > today:
        return f"Дата возврата {closed:%d.%m.%Y} ещё не наступила — проверьте год."
    if started is not None and closed < min(started, today):
        return (f"Дата возврата {closed:%d.%m.%Y} раньше начала аренды "
                f"{started:%d.%m.%Y}.")
    return None


# Дата покупки техники: раньше 2000 года в парке ничего нет, а дата в
# будущем - опечатка в годе. 31.12.9998 роняла план замены и карточку:
# срок службы от неё уходил за 9999 год.
PURCHASE_FLOOR = date(2000, 1, 1)


def check_purchase_date(raw: Any, *, today: date,
                        default: date | None = None) -> Check:
    """Дата покупки из формы: пусто - `default` (None - «не знаем»)."""
    if not str(raw or "").strip():
        return Check(True, default)
    got = check_date(raw)
    if not got.ok:
        return Check(False, error="Дата покупки: в виде ДД.ММ.ГГГГ.")
    if got.value > today:
        return Check(False, error=f"Дата покупки {got.value:%d.%m.%Y} ещё не "
                                  "наступила — проверьте год.")
    if got.value < PURCHASE_FLOOR:
        return Check(False, error=f"Дата покупки: не раньше "
                                  f"{PURCHASE_FLOOR:%d.%m.%Y} — проверьте год.")
    return Check(True, got.value)


def parse_id(raw: Any) -> int | None:
    """Номер записи из адреса или формы: только ASCII-цифры, в bigint.

    str.isdigit верит «²» и «٢»: int() на первой падает (страница 500
    вместо 404), вторую молча читает как 2. Не номер - None.
    """
    text = str(raw or "").strip()
    if not (text.isascii() and text.isdigit()) or len(text) > 18:
        return None
    return int(text)


_PATH_NUMBER = re.compile(r"[\s0-9+\-_.]+")


def path_ids_ok(path: str) -> bool:
    """Номера в адресе влезают в bigint. FastAPI читает `/{id}` в int
    любой длины, и двадцать цифр падали в базе 500 (value out of int64
    range) вместо 404. Предел тот же, что у parse_id: 18 цифр.

    Смотрим на состав сегмента, а не на написание: pydantic читает в int
    и «+N», «-N», « N», «N.0», «1_000», и даже «0-9» (это -9). Все его
    написания - из ASCII-цифр, пробелов, знаков, «_» и «.»; больше 18
    значащих цифр в таком сегменте - число за пределом, какое бы ни было."""
    for part in str(path or "").split("/"):
        if (_PATH_NUMBER.fullmatch(part)
                and len(re.sub(r"[^0-9]", "", part).lstrip("0")) > 18):
            return False
    return True


def check_choice(raw: Any, choices: dict[str, str] | Iterable[str],
                 *, what: str = "Значение") -> Check:
    value = str(raw or "").strip()
    if value not in set(choices):
        return Check(False, error=f"{what}: недопустимое значение.")
    return Check(True, value)


# Срок доступа в форме сотрудника: код выбора -> дней от сегодня (0 -
# до конца сегодняшнего дня). Конец дня, а не «через 24 часа»: подменному
# оператору дают смену, а не сутки от минуты, когда владелец нажал кнопку.
STAFF_TERMS: dict[str, tuple[str, int]] = {
    "day": ("до конца дня", 0),
    "week": ("на неделю", 7),
    "month": ("на месяц", 30),
}


def is_owner(staff: Mapping[str, Any] | None) -> bool:
    """Владелец - встроенная роль «Владелец», а не любой с правом на всё.
    Ему меню прячет то, что делают на точке руками (выдачу): права это не
    отнимает, адрес открывается как прежде."""
    return (staff or {}).get("profile_code") == "owner"


def staff_expired(staff: Mapping[str, Any] | None, *, now: datetime | None = None) -> bool:
    """Срок доступа прошёл. Без срока - бессрочно."""
    until = (staff or {}).get("expires_at")
    if not isinstance(until, datetime):
        return False
    now = now or datetime.now(UTC)
    if until.tzinfo is None:
        until = until.astimezone()
    return until <= now


def check_staff_term(term: Any, until: Any, *, today: date | None = None,
                     keep_empty: bool = False) -> Check:
    """Срок доступа из формы: дата «до» (включительно) главнее кнопки
    срока. Пусто - бессрочно (Check(True, None)); «none» - снять срок.
    `keep_empty` - форма правки срока: там пусто значит «ничего не выбрал»,
    и это отказ, а не снятие срока - снимает только явное «бессрочно».
    Конец дня - по часам сервера (Europe/Moscow), как и вся система."""
    today = today or date.today()
    raw = str(until or "").strip()
    if raw:
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            return Check(False, error="Срок доступа: дата вида 2026-10-31.")
        if day < today:
            return Check(False, error="Срок доступа: дата уже прошла.")
    else:
        code = str(term or "").strip()
        if code == "" and keep_empty:
            return Check(False, error="Срок доступа: выберите срок или дату.")
        if code in ("", "none"):
            return Check(True, None)
        if code not in STAFF_TERMS:
            return Check(False, error="Срок доступа: выберите из списка.")
        day = today + timedelta(days=STAFF_TERMS[code][1])
    return Check(True, datetime.combine(day, time(23, 59, 59)).astimezone())


# Сводка клиентов за всё время: группа - вкладка списка «Клиенты».
# Должники - поперёк остальных: должен и тот, кто сейчас в аренде, и тот,
# кто давно сдал велосипед.
CLIENT_GROUPS: dict[str, str] = {
    "all": "Все за всё время", "active": "Действующие", "former": "Бывшие",
    "never": "Ни разу не брали", "debt": "Должники",
    "bought": "Выкупили", "repair": "Сторонний ремонт",
}

# Вид техники стороннего ремонта - по словам в объекте наряда: отдельной
# колонки у наряда нет, а мастер пишет «самокат Kugoo», «АКБ 60В».
# Порядок важен: «аккумулятор самоката» - это АКБ, а не самокат.
TECH_KINDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("battery", "АКБ", ("акб", "аккум", "батаре")),
    ("tricycle", "Трицикл", ("трицикл", "трёхколёс", "трехколес", "трайк")),
    ("scooter", "Самокат", ("самокат", "scooter", "kugoo", "ninebot", "xiaomi")),
    ("bike", "Велосипед", ("велосипед", "вело", "bike", "байк")),
)
TECH_KIND_TITLES = {code: title for code, title, _ in TECH_KINDS} | {"other": "Другое"}


def tech_kinds(text: Any) -> set[str]:
    """Виды техники в тексте объектов наряда (через « | »)."""
    out: set[str] = set()
    for part in str(text or "").lower().split(" | "):
        if not part.strip():
            continue
        for code, _title, words in TECH_KINDS:
            if any(w in part for w in words):
                out.add(code)
                break
        else:
            out.add("other")
    return out


def top_values(values: Iterable[Any], *, limit: int = 10) -> list[str]:
    """Самые частые непустые значения, частые первыми - «основные модели»."""
    counts: dict[str, int] = {}
    for v in values:
        if v:
            counts[str(v)] = counts.get(str(v), 0) + 1
    return [k for k, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def client_kinds(row: Mapping[str, Any]) -> set[str]:
    """Чем клиент пользуется: арендатор - велосипедом (парк - электровелосипеды),
    клиент ремонта - тем, что приносил."""
    kinds = tech_kinds(row.get("repair_objects"))
    if int(row.get("rentals_count") or 0) > 0 or row.get("rental_id"):
        kinds.add("bike")
    return kinds


def client_in_group(row: Mapping[str, Any], group: str) -> bool:
    """Клиент в группе: действующий - с идущей арендой, бывший - брал, но
    сейчас без велосипеда, «ни разу» - карточка без единой аренды.
    «Выкупили» - последняя аренда проданного велосипеда его (закрыта с
    велосипедом «продан (выкуп)»): арендатор, выкупивший свой велосипед,
    а не покупатель из amoCRM. «Сторонний ремонт» - чинил у нас чужую
    технику за свой счёт."""
    if group == "bought":
        return bool(row.get("bought"))
    if group == "repair":
        return int(row.get("external_repairs") or 0) > 0
    if group == "active":
        return bool(row.get("rental_id"))
    if group == "former":
        return not row.get("rental_id") and int(row.get("rentals_count") or 0) > 0
    if group == "never":
        return int(row.get("rentals_count") or 0) == 0 and not row.get("rental_id")
    if group == "debt":
        return to_money(row.get("balance") or 0) < 0
    return True


def client_tiles(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Плитки сводки: сколько в каждой группе, сколько заплатили за всё
    время и сколько должны сейчас."""
    rows = list(rows)
    out: dict[str, Any] = {g: sum(1 for r in rows if client_in_group(r, g))
                           for g in CLIENT_GROUPS}
    out["paid"] = sum((to_money(r.get("paid_total") or 0) for r in rows), Decimal(0))
    out["debt_sum"] = sum((-to_money(r.get("balance") or 0) for r in rows
                           if to_money(r.get("balance") or 0) < 0), Decimal(0))
    out["days"] = sum(int(r.get("rented_days") or 0) for r in rows)
    return out


def check_login(raw: Any) -> Check:
    text = str(raw or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,32}", text):
        return Check(False, error="Логин: 3–32 символа, латиница, цифры, точка, дефис.")
    return Check(True, text)


# Латиница для логина из ФИО. Своя таблица, а не библиотека: букв три
# десятка, и логин «kuznetsov.t» читается одинаково у всех, кто его
# набирает. Татарские буквы - к ближайшей русской: в Казани они в ФИО есть.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya", "ә": "a", "ө": "o", "ү": "u", "җ": "zh", "ң": "n", "һ": "h",
}
LOGIN_FALLBACK = "staff"


def translit(text: str) -> str:
    """Кириллица - латиницей, прочее, кроме латиницы и цифр, выпадает."""
    out = []
    for ch in str(text or "").lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif "a" <= ch <= "z" or "0" <= ch <= "9":
            out.append(ch)
    return "".join(out)


def login_from_name(name: Any, taken: Iterable[str] = ()) -> str:
    """Логин из ФИО, когда его не задали: «Кузнецов Тимур» -> kuznetsov.t.

    Фамилия и первая буква имени - так логины в панели понятны без
    справочника. Занятый получает номер (kuznetsov.t2); из ФИО без
    единой буквы выходит «staff» с номером. Ответ всегда проходит
    check_login: форму с ним не придётся исправлять второй раз.
    """
    # Первая буква имени - латиницей целиком: «Юлия» -> yu, а не y.
    words = [(translit(w), translit(w[:1])) for w in str(name or "").split()]
    words = [(w, first or w[0]) for w, first in words if w]
    base = words[0][0][:24] if words else ""
    if base and len(words) > 1:
        base += "." + words[1][1]
    if len(base) < 3:
        base = (base + "." if base else "") + LOGIN_FALLBACK
    busy = {str(t).lower() for t in taken}
    if base not in busy:
        return base
    for n in range(2, 1000):
        candidate = f"{base[:29]}{n}"
        if candidate not in busy:
            return candidate
    return f"{LOGIN_FALLBACK}{secrets.randbelow(10 ** 6)}"


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


def session_mark(password_hash: str | None) -> str:
    """Отпечаток пароля для сессии. Cookie сессии подписан, но живёт две
    недели сам по себе: без отпечатка смена пароля (свой или сброс
    владельцем после ухода сотрудника) не выбивала бы уже открытые
    сессии. Соль внутри хэша, так что по отпечатку пароль не подобрать."""
    return hashlib.sha256(str(password_hash or "").encode("utf-8")).hexdigest()[:16]


def safe_next(target: Any, home: str) -> str:
    """Куда вернуть после входа: только свой адрес.

    «//site» и «/\\site» браузер читает как адрес чужого сайта (обратную
    косую Chrome и Firefox считают прямой), и ссылка «войдите в панель»
    уводила бы сотрудника на подделку с теми же полями логина.
    """
    text = str(target or "")
    if (not text.startswith("/") or text.startswith(("//", "/\\"))
            or "\\" in text or any(ord(ch) < 32 for ch in text)):
        return home
    return text


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


# Кнопки пополнения в кабинете: долг, если он есть, и вперёд на 1, 2 и 4
# периода. Четыре недели - это «месяц» у недельного тарифа; дальше
# вперёд курьеры не платят.
TOPUP_MULTIPLES = (1, 2, 4)


def topup_options(summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Суммы к оплате для кнопок кабинета.

    Код кнопки, а не сумма, уходит в callback: сумма пересчитывается
    в момент нажатия по свежему состоянию, и подделанный callback не
    выставит счёт на чужую цифру. Долг равный периоду не дублируется.
    """
    out: list[dict[str, Any]] = []
    due = to_money(summary.get("due") or 0)
    if due > 0:
        out.append({"code": "debt", "amount": due, "periods": 0})
    price = to_money(summary.get("price") or 0)
    if summary.get("active") and price > 0:
        for n in TOPUP_MULTIPLES:
            amount = to_money(price * n)
            if n == 1 and amount == due:
                continue
            out.append({"code": f"p{n}", "amount": amount, "periods": n})
    return out


def topup_amount(summary: Mapping[str, Any], code: Any) -> Decimal | None:
    """Сумма по коду кнопки; None - кнопка устарела (долг закрыт, аренда
    закончилась)."""
    for option in topup_options(summary):
        if option["code"] == str(code or ""):
            return option["amount"]
    return None


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


def pay_method_from_text(text: Any) -> str:
    """Способ оплаты из строки формы выдачи: «3000 нал», «наличными» -
    наличные, всё прочее («3000 qr», «перевод») - СБП.

    «нал» - начало слова, а не любая его часть: «безнал» - это перевод.
    """
    low = str(text or "").casefold()
    return "cash" if re.search(r"(?<![а-яё])нал", low) else "sbp"


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


# ─────────────────── франшиза: метрики наружу и роялти ───────────────────
#
# Франчайзи живёт на своём сервере со своей копией системы. Франчайзеру он
# отдаёт только агрегаты - GET /hook/metrics по токену: парк, три числа,
# выручку по месяцам, названия точек. Ни клиента, ни телефона, ни суммы
# отдельной аренды. Процесс бота франчайзера раз в сутки забирает ответ,
# проверяет его здесь (чужой ответ - чужая строка) и считает роялти.
# Три числа франчайзер пересчитывает из дней и выручки той же формулой
# (fleet_metrics), а не верит готовым процентам: определение у сети одно.

METRICS_FORMAT = 1
# Текущий месяц и пять прошлых - как таблица трёх чисел в «Отчётах». Полгода
# истории хватает, чтобы опрос, пропустивший неделю, не оставил дыр в роялти.
METRICS_MONTHS = 6
# Ответ франчайзи - килобайты. Всё, что больше, не наш формат, и дочитывать
# его значит отдать чужому серверу память процесса бота.
METRICS_MAX_BYTES = 256 * 1024
METRICS_TIMEOUT = 20
# Запросов с верным токеном на один адрес в час. Франчайзер ходит раз в
# сутки и по кнопке; ответ - десяток запросов к базе, и утёкший токен не
# должен превращаться в нагрузку на чужую панель.
METRICS_RATE_LIMIT = 30
METRICS_RATE_WINDOW = 3600
METRICS_TEXT_LIMIT = 80
METRICS_POINTS_LIMIT = 100
METRICS_MONTHS_LIMIT = 24
METRICS_COUNT_LIMIT = 10_000_000
METRICS_DAYS_LIMIT = Decimal(10_000_000)
METRICS_MONEY_LIMIT = Decimal("9999999999.99")      # numeric(12,2)
# Ответ собирается в момент запроса: generated_at дальше суток от наших
# часов - сбитые часы или чужой кэш, и такой ответ затёр бы свежие месяцы
# старыми цифрами. Годы - те же, что у месяцев ответа.
METRICS_CLOCK_SKEW = timedelta(days=1)
METRICS_YEARS = (2000, 2100)
# Данные устарели, если принятого ответа нет полтора суток: опрос раз в
# сутки, и один пропущенный круг - ещё не повод для тревоги.
FRANCHISE_STALE_HOURS = 36
# Неудачный опрос повторяется не чаще раза в час: лежащему серверу
# франчайзи запрос каждые десять минут не поможет.
FRANCHISE_RETRY_MINUTES = 60
FRANCHISE_TOKEN_MIN, FRANCHISE_TOKEN_MAX = 16, 200
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_VERSION_RE = re.compile(r"[0-9A-Za-z._+-]{1,40}")
_MONTH_RE = re.compile(r"(\d{4})-(\d{2})")


def metrics_months(now: datetime, count: int = METRICS_MONTHS
                   ) -> list[tuple[datetime, datetime, bool]]:
    """Границы месяцев для ответа, от нового к старому: текущий - по сейчас
    (третий элемент True), прошлые - целиком. Тот же шаг, что у таблицы
    трёх чисел в «Отчётах»: франчайзи и франчайзер обязаны видеть одно."""
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    out = []
    for _ in range(count):
        nxt = (first + timedelta(days=32)).replace(day=1)
        out.append((first, min(nxt, now), nxt > now))
        first = (first - timedelta(days=1)).replace(day=1)
    return out


def _days_text(value: Any) -> str:
    return str(Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP))


def metrics_block(m: Mapping[str, Any]) -> dict[str, Any]:
    """Три числа периода (итог fleet_metrics) для ответа. Деньги и дни -
    строкой: float в JSON терял бы копейки."""
    return {
        "idle_percent": m.get("idle_percent"),
        "avg_check": None if m.get("avg_check") is None else str(to_money(m["avg_check"])),
        "revenue": str(to_money(m.get("revenue"))),
        "operational_days": _days_text(m.get("operational_days")),
        "rented_days": _days_text(m.get("rented_days")),
        "idle_days": _days_text(m.get("idle_days")),
    }


def metrics_payload(*, title: str, version: str, now: datetime,
                    places: Iterable[Mapping[str, Any]], bikes: Mapping[str, Any],
                    counts: Mapping[str, Any], last30: Mapping[str, Any],
                    months: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Ответ /hook/metrics. Только агрегаты: названия и города точек, число
    велосипедов, аренд и клиентов, три числа и выручка. Ни одного поля
    карточки клиента - это граница, которую держит тест на ключи ответа.
    Город копии - самый частый город открытых точек."""
    points = [{"name": str(p.get("name") or ""), "city": p.get("city") or None}
              for p in places if p.get("active", True) and p.get("name")]
    cities: dict[str, int] = {}
    for p in points:
        if p["city"]:
            cities[p["city"]] = cities.get(p["city"], 0) + 1
    return {
        "format": METRICS_FORMAT,
        "name": str(title or ""),
        "city": max(cities, key=lambda c: (cities[c], c)) if cities else None,
        "version": version,
        "generated_at": now.isoformat(timespec="seconds"),
        "points": points,
        "fleet": sum(int(bikes.get(s, 0) or 0) for s in OPERATIONAL_STATUSES),
        "rented": int(bikes.get("rented", 0) or 0),
        "rentals_active": int(counts.get("rentals", 0) or 0),
        "clients": int(counts.get("clients", 0) or 0),
        "last30": metrics_block(last30),
        "months": [{"month": m["month"].strftime("%Y-%m"), "partial": bool(m.get("partial")),
                    **metrics_block(m)} for m in months],
    }


class _BadMetrics(ValueError):
    """Ответ франчайзи не прошёл проверку; текст - что именно не так."""


def _m_text(value: Any, what: str, *, required: bool = False) -> str | None:
    """Чужая строка: управляющие и невидимые символы (перевод строки,
    переворот RTL, нулевая ширина, суррогаты) - вон, пробелы схлопнуты,
    длина обрезана. В таблице франчайзера такие символы переставляли бы
    соседние колонки и прятали подмену имени."""
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise _BadMetrics(f"{what}: ждали строку")
    text = "".join(" " if ch.isspace() else ch for ch in value[:METRICS_TEXT_LIMIT * 4]
                   if ch.isspace() or unicodedata.category(ch)[0] != "C")
    text = " ".join(text.split())[:METRICS_TEXT_LIMIT].strip()
    if not text:
        if required:
            raise _BadMetrics(f"{what}: пусто")
        return None
    return text


def _m_int(value: Any, what: str) -> int:
    # bool - подкласс int: true на месте числа клиентов - порча, а не 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _BadMetrics(f"{what}: ждали целое число")
    if not 0 <= value <= METRICS_COUNT_LIMIT:
        raise _BadMetrics(f"{what}: вне разумных пределов")
    return value


def _m_dec(value: Any, what: str, *, limit: Decimal, optional: bool = False,
           places: Decimal = CENT) -> Decimal | None:
    """Число из ответа: строка, целое или Decimal (parse_float=Decimal).
    NaN, бесконечность, минус и «1e999» - порча, а не число."""
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise _BadMetrics(f"{what}: ждали число")
    text = value.strip() if isinstance(value, str) else str(value)
    if len(text) > 40:
        raise _BadMetrics(f"{what}: ждали число")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        raise _BadMetrics(f"{what}: ждали число") from None
    if not number.is_finite() or number < 0 or number > limit:
        raise _BadMetrics(f"{what}: вне разумных пределов")
    return number.quantize(places, rounding=ROUND_HALF_UP)


def _m_block(value: Any, what: str) -> dict[str, Any]:
    """Три числа периода. Проценты и чек пересчитываются из дней и выручки
    той же fleet_metrics, что у самой панели: присланные готовыми они
    могли бы разойтись с днями, а сеть сравнивается по одной формуле."""
    if not isinstance(value, dict):
        raise _BadMetrics(f"{what}: ждали объект")
    revenue = _m_dec(value.get("revenue"), f"{what}.revenue", limit=METRICS_MONEY_LIMIT)
    idle = _m_dec(value.get("idle_days"), f"{what}.idle_days", limit=METRICS_DAYS_LIMIT)
    rented = _m_dec(value.get("rented_days"), f"{what}.rented_days", limit=METRICS_DAYS_LIMIT)
    operational = _m_dec(value.get("operational_days"), f"{what}.operational_days",
                         limit=METRICS_DAYS_LIMIT)
    # Операционный парк - это ровно простой плюс аренда: расхождение
    # больше округления значит, что дни собраны не нашей формулой.
    if abs(operational - idle - rented) > Decimal("0.05"):
        raise _BadMetrics(f"{what}: дни не сходятся (простой + аренда ≠ парк)")
    three = fleet_metrics({"available": idle, "rented": rented}, revenue)
    return {"revenue": revenue, "idle_days": idle, "rented_days": rented,
            "operational_days": operational, "idle_percent": three["idle_percent"],
            "avg_check": three["avg_check"]}


def _m_time(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise _BadMetrics("generated_at: ждали дату и время ISO")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise _BadMetrics("generated_at: ждали дату и время ISO") from None
    if moment.tzinfo is None:
        raise _BadMetrics("generated_at: без часового пояса")
    # «0001-01-01T00:00+14:00» в UTC - год 0: astimezone в шаблоне падал бы
    # OverflowError на каждом открытии карточки. Свой пояс момент хранит:
    # по нему франчайзи резал месяцы.
    try:
        utc = moment.astimezone(UTC)
    except (OverflowError, ValueError):
        raise _BadMetrics("generated_at: вне разумных пределов") from None
    low, high = METRICS_YEARS
    if not (low <= moment.year <= high and low <= utc.year <= high):
        raise _BadMetrics("generated_at: вне разумных пределов")
    return moment


def _month_after(month: date) -> date:
    return (month + timedelta(days=32)).replace(day=1)


def parse_metrics(data: Any, *, now: datetime | None = None) -> Check:
    """Проверка ответа франчайзи. Check.value - только известные поля в
    наших типах (Decimal, date, datetime); лишнее отброшено молча, негодное
    - отказ целиком с причиной. Чужой ответ не должен ни уронить панель
    франчайзера, ни подложить в неё разметку или «миллиард» в чек."""
    try:
        if not isinstance(data, dict):
            raise _BadMetrics("ответ - не объект JSON")
        fmt = data.get("format")
        if isinstance(fmt, bool) or fmt != METRICS_FORMAT:
            raise _BadMetrics(f"формат ответа не наш: ждали {METRICS_FORMAT}")
        generated = _m_time(data.get("generated_at"))
        if now is not None and generated - now > METRICS_CLOCK_SKEW:
            raise _BadMetrics("generated_at: из будущего")
        if now is not None and now - generated > METRICS_CLOCK_SKEW:
            raise _BadMetrics("generated_at: старше суток - сбиты часы франчайзи "
                              "или ответ из кэша")
        version = data.get("version")
        raw_points = data.get("points")
        if not isinstance(raw_points, list):
            raise _BadMetrics("points: ждали список")
        if len(raw_points) > METRICS_POINTS_LIMIT:
            raise _BadMetrics(f"points: больше {METRICS_POINTS_LIMIT}")
        points = []
        for item in raw_points:
            if not isinstance(item, dict):
                raise _BadMetrics("points: ждали объекты")
            points.append({"name": _m_text(item.get("name"), "points.name", required=True),
                           "city": _m_text(item.get("city"), "points.city")})
        raw_months = data.get("months")
        if not isinstance(raw_months, list):
            raise _BadMetrics("months: ждали список")
        if len(raw_months) > METRICS_MONTHS_LIMIT:
            raise _BadMetrics(f"months: больше {METRICS_MONTHS_LIMIT}")
        top = generated.date().replace(day=1)
        months: dict[date, dict[str, Any]] = {}
        for item in raw_months:
            if not isinstance(item, dict):
                raise _BadMetrics("months: ждали объекты")
            found = _MONTH_RE.fullmatch(str(item.get("month") or ""))
            if found is None or not 1 <= int(found.group(2)) <= 12 \
                    or not 2000 <= int(found.group(1)) <= 2100:
                raise _BadMetrics("months.month: ждали ГГГГ-ММ")
            month = date(int(found.group(1)), int(found.group(2)), 1)
            if month > top:
                raise _BadMetrics("months.month: месяц позже самого ответа")
            if month in months:
                raise _BadMetrics("months.month: месяц дважды")
            # Неполный - и месяц, закончившийся позже снимка, что бы ни
            # написал франчайзи: снимок 31-го в полночь не несёт последних
            # суток, и счёт по нему вышел бы заниженным.
            ends = datetime.combine(_month_after(month), datetime.min.time(),
                                    tzinfo=generated.tzinfo)
            months[month] = {"month": month,
                             "partial": item.get("partial") is True or generated < ends,
                             **_m_block(item, f"months[{month:%Y-%m}]")}
        out = {
            "format": METRICS_FORMAT,
            "name": _m_text(data.get("name"), "name", required=True),
            "city": _m_text(data.get("city"), "city"),
            "version": (version if isinstance(version, str)
                        and _VERSION_RE.fullmatch(version) else None),
            "generated_at": generated,
            "points": points,
            "fleet": _m_int(data.get("fleet"), "fleet"),
            "rented": _m_int(data.get("rented"), "rented"),
            "rentals_active": _m_int(data.get("rentals_active"), "rentals_active"),
            "clients": _m_int(data.get("clients"), "clients"),
            "last30": _m_block(data.get("last30"), "last30"),
            "months": [months[m] for m in sorted(months, reverse=True)],
        }
    except _BadMetrics as exc:
        return Check(False, error=str(exc))
    return Check(True, out)


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} в JSON - не число")


def parse_metrics_bytes(body: bytes, *, now: datetime | None = None) -> Check:
    """Тело ответа целиком: размер, UTF-8, JSON без NaN и Infinity (Python
    их принимает, а Decimal и Postgres - нет), глубина вложенности (тысячи
    скобок - RecursionError) и затем parse_metrics."""
    if len(body) > METRICS_MAX_BYTES:
        return Check(False, error=f"ответ больше {METRICS_MAX_BYTES // 1024} КБ")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return Check(False, error="ответ не в UTF-8")
    try:
        data = json.loads(text, parse_float=Decimal, parse_constant=_no_constant)
    except (ValueError, RecursionError, ArithmeticError):
        # ArithmeticError - decimal.InvalidOperation от «1e99999999999999999999»:
        # порядок больше, чем держит Decimal, и это не ValueError.
        return Check(False, error="ответ не JSON")
    return parse_metrics(data, now=now)


def metrics_json(parsed: Mapping[str, Any]) -> dict[str, Any]:
    """Проверенный ответ - обратно в формат провода для jsonb: деньги и дни
    строкой, месяц ГГГГ-ММ. Снимок из базы читается тем же parse_metrics."""
    block = metrics_block
    return {
        "format": METRICS_FORMAT, "name": parsed["name"], "city": parsed.get("city"),
        "version": parsed.get("version"),
        "generated_at": parsed["generated_at"].isoformat(timespec="seconds"),
        "points": [dict(p) for p in parsed.get("points") or []],
        "fleet": parsed["fleet"], "rented": parsed["rented"],
        "rentals_active": parsed["rentals_active"], "clients": parsed["clients"],
        "last30": block(parsed["last30"]),
        "months": [{"month": m["month"].strftime("%Y-%m"), "partial": bool(m.get("partial")),
                    **block(m)} for m in parsed.get("months") or []],
    }


def metrics_month_rows(parsed: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Месяцы ответа строками crm.franchise_months. partial едет в базу:
    прошлый месяц, который последний ответ застал недоделанным, отчёт
    роялти помечает, а не выдаёт за окончательный."""
    return [{"month": m["month"], "revenue": m["revenue"],
             "idle_percent": (None if m["idle_percent"] is None
                              else Decimal(str(m["idle_percent"]))),
             "avg_check": m["avg_check"], "operational_days": m["operational_days"],
             "rented_days": m["rented_days"], "partial": bool(m.get("partial"))}
            for m in parsed.get("months") or []]


def metrics_url(base_url: str) -> str:
    return str(base_url or "").rstrip("/") + "/hook/metrics"


def check_base_url(raw: Any) -> Check:
    """Адрес панели франчайзи. Только https: токен и чужие цифры открытым
    текстом не ездят. http - лишь на свою машину (localhost): там нечего
    перехватывать, и так устроены тесты. Логин в адресе, параметры и
    «#» - признак подделки или опечатки, а не адреса панели."""
    text = str(raw or "").strip().rstrip("/")
    if not text:
        return Check(False, error="Адрес: заполните поле, например https://crm.prokat.ru.")
    if (len(text) > 200 or not text.isascii() or "\\" in text
            or any(ch.isspace() or ord(ch) < 32 for ch in text)):
        return Check(False, error="Адрес: латиницей, без пробелов (домен на кириллице - "
                                  "в виде xn--…).")
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("https", "http") or not host:
        return Check(False, error="Адрес: начинается с https://.")
    if parts.scheme == "http" and host not in _LOOPBACK_HOSTS:
        return Check(False, error="Адрес: только https — токен и цифры франчайзи "
                                  "не должны ехать открытым текстом.")
    if parts.username or parts.password or parts.query or parts.fragment or "@" in text:
        return Check(False, error="Адрес: без логина, параметров и «#» — только "
                                  "адрес панели.")
    try:
        _ = parts.port
    except ValueError:
        return Check(False, error="Адрес: порт не число.")
    return Check(True, text)


def check_franchise_token(raw: Any) -> Check:
    """Токен метрик франчайзи: то, что лежит у него в secrets/metrics_token.
    Пусто - «не менять» (value None)."""
    text = str(raw or "").strip()
    if not text:
        return Check(True, None)
    if not (FRANCHISE_TOKEN_MIN <= len(text) <= FRANCHISE_TOKEN_MAX) or not text.isascii() \
            or any(ch.isspace() or ord(ch) < 33 for ch in text):
        return Check(False, error=f"Токен: {FRANCHISE_TOKEN_MIN}–{FRANCHISE_TOKEN_MAX} "
                                  "знаков латиницей и цифрами, как в файле "
                                  "secrets/metrics_token франчайзи.")
    return Check(True, text)


def _percent(raw: Any) -> Decimal | None:
    value = parse_money(raw)
    if value is None or value < 0 or value > 100:
        return None
    return value


def check_franchisee(data: Mapping[str, Any]) -> Check:
    """Карточка франчайзи из формы. Токен проверяется отдельно
    (check_franchise_token): его пустое поле значит «оставить прежний»."""
    name = check_name(data.get("name"), what="Название")
    if not name.ok:
        return name
    city = None
    if str(data.get("city") or "").strip():
        checked = check_name(data.get("city"), what="Город")
        if not checked.ok:
            return checked
        city = checked.value
    url = check_base_url(data.get("base_url"))
    if not url.ok:
        return url
    percent = _percent(str(data.get("royalty_percent") or "0"))
    if percent is None:
        return Check(False, error="Роялти: процент от 0 до 100, например 5 или 5,5.")
    fee = parse_money(str(data.get("fixed_fee") or "0"))
    if fee is None or fee < 0 or fee > MAX_AMOUNT:
        return Check(False, error="Фикс в месяц: сумма от нуля, например 15000.")
    start = check_date(data.get("contract_start"))
    if not start.ok:
        return Check(False, error="Начало договора: дата, с месяца которой "
                                  "считается роялти.")
    note = check_note(data.get("note"))
    if not note.ok:
        return note
    return Check(True, {"name": name.value, "city": city, "base_url": url.value,
                        "royalty_percent": percent, "fixed_fee": fee,
                        "contract_start": start.value, "note": note.value,
                        "active": bool(data.get("active"))})


def check_terms_from(raw: Any, *, today: date) -> Check:
    """С какого месяца действуют условия роялти из карточки. Пусто -
    текущий: пересмотр договора прошлых счетов не трогает. Прошлый месяц -
    осознанная правка: опечатка в проценте, замеченная после первого
    опроса, иначе навсегда осталась бы в закрытых месяцах. Будущего нет:
    идущий месяц опрос всё равно пишет по карточке."""
    current = today.replace(day=1)
    text = str(raw or "").strip()
    if not text:
        return Check(True, current)
    found = _MONTH_RE.fullmatch(text) or re.fullmatch(r"(\d{2})\.(\d{4})", text)
    if found is not None:
        year, month = ((found.group(1), found.group(2)) if "-" in text
                       else (found.group(2), found.group(1)))
        if 1 <= int(month) <= 12 and METRICS_YEARS[0] <= int(year):
            first = date(int(year), int(month), 1)
            if first <= current:
                return Check(True, first)
    return Check(False, error="Условия с месяца: ГГГГ-ММ, не позже текущего месяца.")


def royalty(revenue: Any, percent: Any, fixed: Any) -> Decimal:
    """Роялти месяца: выручка × процент + фикс, до копейки по правилу
    округления журнала. Только Decimal: процент от миллионной выручки во
    float терял бы копейки на каждом месяце."""
    share = (to_money(revenue) * Decimal(str(percent or 0)) / 100).quantize(
        CENT, rounding=ROUND_HALF_UP)
    return share + to_money(fixed)


def franchise_due(row: Mapping[str, Any], now: datetime) -> bool:
    """Пора ли опросить франчайзи: раз в сутки после принятого ответа,
    неудачу - не чаще FRANCHISE_RETRY_MINUTES. Без токена и выключенного
    не трогаем вовсе."""
    if not row.get("active") or not row.get("token_enc"):
        return False
    ok_at = row.get("ok_at")
    if ok_at is not None and ok_at.astimezone(now.tzinfo).date() >= now.date():
        return False
    polled = row.get("polled_at")
    return polled is None or now - polled >= timedelta(minutes=FRANCHISE_RETRY_MINUTES)


def franchise_stale(row: Mapping[str, Any], now: datetime) -> bool:
    """Нет свежих данных: у действующего франчайзи принятого ответа нет
    дольше FRANCHISE_STALE_HOURS (или не было никогда)."""
    if not row.get("active"):
        return False
    ok_at = row.get("ok_at")
    return ok_at is None or now - ok_at > timedelta(hours=FRANCHISE_STALE_HOURS)


def franchise_stale_text(stale: int, total: int) -> str:
    """Сигнал в служебный чат - без имён и цифр: чат читают все, а раздел
    «Франчайзи» только владелец."""
    return (f"Франчайзи без свежих данных: {stale} из {total} — не отвечают на "
            f"опрос дольше {FRANCHISE_STALE_HOURS} ч. Причина — в панели, "
            f"раздел «Франчайзи».")


def _trend(new: Decimal | None, old: Decimal | None) -> Decimal | None:
    if new is None or not old:
        return None
    return ((new - old) * 100 / old).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


ROYALTY_PARTIAL = "неполные данные"


def _month_royalty(f: Mapping[str, Any], row: Mapping[str, Any] | None, month: date,
                   current: date) -> dict[str, Any]:
    """Роялти одного месяца франчайзи: условия - записанные на месяц, до
    месяца начала договора - ноль, текущий - «идёт» (цифра растёт), прошлый,
    который последний принятый ответ застал незакончившимся, - «неполные
    данные»: счёт по нему вышел бы заниженным."""
    start = f.get("contract_start")
    if row is None:
        return {"revenue": None, "percent": None, "fixed": None, "royalty": None,
                "mark": "нет данных"}
    base = {"revenue": to_money(row["revenue"]), "percent": row["royalty_percent"],
            "fixed": to_money(row["fixed_fee"])}
    if start is None:
        return {**base, "royalty": Decimal("0.00"), "mark": "нет даты договора"}
    if month < start.replace(day=1):
        return {**base, "royalty": Decimal("0.00"), "mark": "до договора"}
    mark = ("идёт" if month >= current else ROYALTY_PARTIAL if row.get("partial")
            else "")
    return {**base, "royalty": royalty(row["revenue"], row["royalty_percent"],
                                       row["fixed_fee"]), "mark": mark}


def franchise_rows(franchisees: Iterable[Mapping[str, Any]],
                   months: Iterable[Mapping[str, Any]], *, now: datetime,
                   version: str | None = None) -> dict[str, Any]:
    """Сравнение франчайзи: последний снимок, свежесть, выручка прошлого
    месяца против позапрошлого и роялти прошлого месяца. Итог сети - не
    среднее процентов, а те же формулы по суммам дней и денег."""
    current = now.date().replace(day=1)
    last = (current - timedelta(days=1)).replace(day=1)
    before = (last - timedelta(days=1)).replace(day=1)
    by_month = {(int(m["franchisee_id"]), m["month"]): m for m in months}
    rows = []
    sums = {"fleet": 0, "rented": 0, "rentals_active": 0, "clients": 0,
            "idle": Decimal(0), "rented_days": Decimal(0), "revenue30": Decimal(0),
            "last": Decimal(0), "royalty": Decimal(0)}
    # Тренд сети - по сопоставимым: франчайзи, пришедший в прошлом месяце,
    # иначе выдал бы свою выручку за рост всей сети.
    same = {"last": Decimal(0), "before": Decimal(0)}
    with_data = 0
    for f in franchisees:
        parsed = parse_metrics(f.get("data")) if f.get("data") else Check(False)
        snap = parsed.value if parsed.ok else None
        fid = int(f["id"])
        last_row, before_row = by_month.get((fid, last)), by_month.get((fid, before))
        last_rev = to_money(last_row["revenue"]) if last_row else None
        before_rev = to_money(before_row["revenue"]) if before_row else None
        paid = _month_royalty(f, last_row, last, current)
        row = {**{k: f.get(k) for k in ("id", "name", "city", "active", "base_url", "ok_at",
                                        "polled_at", "error", "taken_at")},
               "has_token": bool(f.get("token_enc")), "stale": franchise_stale(f, now),
               "snap": snap, "last_revenue": last_rev, "before_revenue": before_rev,
               "trend": _trend(last_rev, before_rev), "royalty_last": paid["royalty"],
               "royalty_mark": paid["mark"],
               "same_version": (None if snap is None or not snap.get("version") or not version
                                else snap["version"] == version)}
        if snap is not None:
            three = snap["last30"]
            row.update(fleet=snap["fleet"], rented=snap["rented"],
                       rentals_active=snap["rentals_active"], clients=snap["clients"],
                       points=len(snap["points"]), idle_percent=three["idle_percent"],
                       avg_check=three["avg_check"], revenue30=three["revenue"],
                       idle_ok=(three["idle_percent"] is not None
                                and three["idle_percent"] < IDLE_TARGET_PERCENT),
                       check_ok=(three["avg_check"] is not None
                                 and three["avg_check"] >= CHECK_TARGET))
            if f.get("active"):
                with_data += 1
                for key in ("fleet", "rented", "rentals_active", "clients"):
                    sums[key] += snap[key]
                sums["idle"] += three["idle_days"]
                sums["rented_days"] += three["rented_days"]
                sums["revenue30"] += three["revenue"]
        # Выручка и роялти прошлого месяца - по тому же правилу, что отчёт
        # роялти: месяц с цифрами в счёт, даже если договор с тех пор снят.
        # Иначе плитка и отчёт за один месяц показывали бы разные суммы.
        sums["last"] += last_rev or 0
        sums["royalty"] += paid["royalty"] or 0
        if f.get("active"):
            if last_rev is not None and before_rev:
                same["last"] += last_rev
                same["before"] += before_rev
        rows.append(row)
    three = fleet_metrics({"available": sums["idle"], "rented": sums["rented_days"]},
                          sums["revenue30"])
    total = {**sums, "with_data": with_data, "idle_percent": three["idle_percent"],
             "avg_check": three["avg_check"], "revenue30": to_money(sums["revenue30"]),
             "trend": _trend(same["last"], same["before"])}
    return {"rows": rows, "total": total, "last": last, "before": before,
            "stale": sum(1 for r in rows if r["stale"]),
            "active": sum(1 for r in rows if r["active"])}


def royalty_rows(franchisees: Iterable[Mapping[str, Any]],
                 months: Iterable[Mapping[str, Any]], *, today: date,
                 count: int = METRICS_MONTHS) -> list[dict[str, Any]]:
    """Отчёт роялти: по месяцу на блок, от нового к старому, в блоке -
    франчайзи по имени и итог. Строка «нет данных» - у действующего
    франчайзи, чей месяц по договору уже идёт, а цифр нет: счёт за него
    выставлять не по чему, и это видно, а не пропало."""
    current = today.replace(day=1)
    by_month = {(int(m["franchisee_id"]), m["month"]): m for m in months}
    people = sorted(franchisees, key=lambda f: (str(f.get("name") or "").lower(), f["id"]))
    out = []
    month = current
    for _ in range(max(count, 1)):
        rows = []
        for f in people:
            row = by_month.get((int(f["id"]), month))
            start = f.get("contract_start")
            if row is None and not (f.get("active") and start is not None
                                    and start.replace(day=1) <= month):
                continue
            rows.append({"id": f["id"], "name": f.get("name"), "city": f.get("city"),
                         **_month_royalty(f, row, month, current)})
        out.append({"month": month, "current": month == current, "rows": rows,
                    "revenue": to_money(sum((r["revenue"] for r in rows
                                             if r["revenue"] is not None), Decimal(0))),
                    "royalty": to_money(sum((r["royalty"] for r in rows
                                             if r["royalty"] is not None), Decimal(0))),
                    "missing": sum(1 for r in rows if r["revenue"] is None),
                    "partial": sum(1 for r in rows if r["mark"] == ROYALTY_PARTIAL)})
        month = (month - timedelta(days=1)).replace(day=1)
    return out


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


# Вид тарифа. Велосипед и аккумулятор сдаются отдельно и стоят разного:
# курьер берёт вторую батарею, чтобы не заряжаться в середине смены.
TARIFF_KINDS: dict[str, str] = {"bike": "Велосипеды", "battery": "Аккумуляторы"}


def check_tariff_kind(raw: Any) -> Check:
    """Вид тарифа; пусто читается как «велосипед» - так было до батарей."""
    return check_choice(str(raw or "bike"), TARIFF_KINDS, what="Вид тарифа")


def model_aliases(models: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Как модель зовут в парке -> как она называется в каталоге.

    В парке модель записана так, как её назвал поставщик в накладной
    («Maikaolin Maikaolin H10»), а в каталоге и в тарифах - так, как её
    называют клиенту («Городской H10»). Совпадения букв в букву не будет
    никогда, и переименовывать парк нельзя: это живая история, на неё
    ссылаются закрытые аренды и наряды. Поэтому каталог хранит оба имени
    и связывает их здесь.
    """
    out: dict[str, str] = {}
    for row in models:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        for name in (title, row.get("factory_title")):
            key = str(name or "").strip().casefold()
            if key:
                out.setdefault(key, title)
    return out


def catalogue_entry(models: Iterable[Mapping[str, Any]], model: Any) -> dict | None:
    """Строка каталога для модели парка: по клиентскому или заводскому
    имени, без учёта регистра. None - в каталоге такой нет."""
    name = str(model or "").strip().casefold()
    if not name:
        return None
    for row in models:
        for candidate in (row.get("title"), row.get("factory_title")):
            if str(candidate or "").strip().casefold() == name:
                return dict(row)
    return None


def catalogue_model(model: Any, aliases: Mapping[str, str] | None = None) -> str:
    """Название модели в терминах каталога: по нему ищется цена."""
    name = str(model or "").strip()
    if not aliases:
        return name
    return aliases.get(name.casefold(), name)


def tariffs_for_model(tariffs: Iterable[dict], model: Any, *, kind: str = "bike",
                      aliases: Mapping[str, str] | None = None) -> list[dict]:
    """Тарифы для модели: её собственные, а если их нет - общие.

    Цена зависит от модели: Monster Truck+ и Kugoo V3 Pro стоят
    по-разному. Тариф без модели остаётся запасным - он работает, пока
    у модели нет своей цены. Имя модели сначала переводится в название
    каталога: в парке оно заводское, а цена стоит на клиентском.
    """
    rows = [dict(t) for t in tariffs
            if str(t.get("kind") or "bike") == kind]
    name = catalogue_model(model, aliases)
    own = [t for t in rows if str(t.get("model") or "").strip() == name and name]
    return own or [t for t in rows if not str(t.get("model") or "").strip()]


def match_tariff(tariffs: Iterable[dict], tariff: Mapping[str, Any] | None,
                 model: Any, *, aliases: Mapping[str, str] | None = None) -> dict | None:
    """Тот же срок, но по цене выбранной модели.

    Оператор выбирает тариф и модель на одном экране, и модель он может
    сменить последней. Подставлять чужую цену нельзя, отправлять его
    на шаг назад - грубо: берём тариф того же срока у нужной модели.
    """
    if tariff is None:
        return None
    rows = tariffs_for_model(tariffs, model, kind=str(tariff.get("kind") or "bike"),
                             aliases=aliases)
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


# ─────────────────── оценка риска клиента на выдаче ───────────────────
#
# Подсказка оператору перед выдачей: насколько клиент надёжен по его же
# истории у нас. Не решение: выдачу оценка не запирает (блокировка и
# чёрный список - статус карточки, и правило выдачи для них прежнее),
# ничего не пишет и никуда не уходит. Поэтому она обязана объяснять себя:
# уровень без причин оператор проигнорирует, а с причинами - спросит о
# долге или возьмёт залог.
#
# Баллы целые и простые, чтобы их можно было пересказать словами: минус
# истории прибавляет, доверие отнимает. Нет истории - не «низкий риск»:
# о новом клиенте мы не знаем ничего, и «надёжен» было бы враньём.

RISK_LEVELS: dict[str, str] = {"none": "нет истории", "low": "низкий",
                               "medium": "средний", "high": "высокий"}
# С какой суммы баллов уровень средний и высокий.
RISK_MEDIUM = 2
RISK_HIGH = 4
# Долг сейчас: с этой суммы три балла, меньше - два, и уровень не ниже
# среднего при любом доверии - сначала долг, потом велосипед. Порог около
# двух недель аренды по цели чека: неделю долга бывает и у честного курьера.
RISK_DEBT_BIG = Decimal(7000)
# Наибольший долг на конец суток: больше недели аренды - балл, больше
# месяца - два. Долг до вечера в день начисления сюда не попадает.
RISK_MAX_DEBT = (Decimal(5000), Decimal(15000))
# Просрочки (дни в минусе на конец суток, раз ушёл в минус) на 1, 2 и 3
# балла. Разовая задержка на день-два - не повод, привычка - повод.
RISK_OVERDUE = ((3, None), (7, 3), (20, 6))
# Штрафов и ремонта за счёт клиента на 1 и 2 балла; досрочных возвратов
# на балл: взял на неделю и вернул через три дня - повод спросить, а не
# отказать, поэтому вес малый и только за повтор.
RISK_FINES = (1, 3)
RISK_EARLY = 2
# Доверие: закрытых без потерь аренд и месяцев в аренде на -1 и -2, всего
# не больше трёх баллов - иначе старожил с привычкой к просрочкам выглядел
# бы надёжнее новичка без них. Приглашение надёжным клиентом - ещё -1.
# Месяцы - сумма дней его аренд, а не календарь с первой: год отсутствия
# после недели аренды доверия не прибавляет.
RISK_DONE = (2, 5)
RISK_MONTHS = (6, 12)
RISK_TRUST_CAP = 3
# Меньше месяца в аренде и без закрытой аренды - «нет истории»: первая
# неделя ещё ничего не говорит. Минусы новичка видны причинами, но уровень
# ставят, только набрав средний: «риск низкий» за разовую просрочку
# выглядел бы надёжнее чистого новичка и подсказывал бы залог меньше.
RISK_HISTORY_DAYS = 30
# Залог по уровню - в crm.settings, правит владелец; 0 - не брать.
RISK_DEPOSIT_KEYS = {level: f"risk_deposit_{level}" for level in RISK_LEVELS}


def risk_settings(raw: Mapping[str, Any] | None) -> dict[str, Decimal]:
    """Рекомендуемый залог по уровню. Мусор и минус в настройке - ноль:
    лучше не подсказать залог, чем подсказать неверный."""
    raw = raw or {}
    out: dict[str, Decimal] = {}
    for level, key in RISK_DEPOSIT_KEYS.items():
        value = parse_money(raw.get(key))
        out[level] = value if value is not None and 0 < value <= MAX_AMOUNT \
            else Decimal(0)
    return out


def debt_track(days: Iterable[tuple[date, Any]], *, today: date) -> dict[str, Any]:
    """Просрочки по журналу клиента: суточные суммы -> сколько дней и раз
    он был в минусе на конец суток, наибольший такой долг и долг сейчас.

    Период начисляется вперёд целиком, и в день начисления минус до вечера
    - норма, поэтому счёт идёт по остатку на конец суток. Сегодняшние сутки
    не кончились: их нет ни в днях, ни в наибольшем долге. Долг сейчас -
    по всему журналу, с сегодняшними записями; debt_days - сколько полных
    суток он тянется.
    """
    per_day: dict[date, Decimal] = {}
    for day, amount in days:
        per_day[day] = per_day.get(day, Decimal(0)) + to_money(amount)
    order = sorted(per_day)
    bal = Decimal(0)
    overdue_days = times = 0
    max_debt = Decimal(0)
    start: date | None = None
    for i, day in enumerate(order):
        bal += per_day[day]
        if bal >= 0:
            start = None
            continue
        if start is None:
            start = day
            if day < today:
                times += 1
        end = min(order[i + 1], today) if i + 1 < len(order) else today
        overdue_days += max((end - day).days, 0)
        if day < today:
            max_debt = max(max_debt, -bal)
    debt = max(-bal, Decimal(0))
    return {"overdue_days": overdue_days, "overdue_times": times,
            "max_debt": to_money(max_debt), "debt": to_money(debt),
            "debt_days": max((today - start).days, 0) if debt and start else 0}


def _risk_steps(value: Any, steps: Iterable[Any]) -> int:
    """Сколько порогов из возрастающего ряда пройдено."""
    return sum(1 for step in steps if value >= step)


def client_risk(facts: Mapping[str, Any] | None, *, today: date,
                agent_good: bool = False,
                deposits: Mapping[str, Decimal] | None = None) -> dict[str, Any]:
    """Уровень риска клиента с причинами и рекомендуемым залогом.

    facts - история из CrmDB.risk_facts: статус карточки, аренды (закрытые,
    потерянные, в розыске, досрочные), дни в закрытых арендах (rent_days)
    и начало идущей (active_on), суточные суммы журнала, штрафы.
    agent_good - клиента пригласил клиент с низким риском.

    Причина - {"text", "plain", "kind"}: plain без рублей для того, кому
    деньги в панели не показывают; kind - bad (риск выше), good (ниже).
    Чёрный список, блокировка, потеря и розыск сейчас - высокий уровень
    сразу: доверие такое не перевешивает.
    """
    f = facts or {}
    track = debt_track(f.get("days") or (), today=today)
    hard: list[dict] = []
    bad: list[tuple[int, dict]] = []
    good: list[tuple[int, dict]] = []

    def why(text: str, plain: str | None = None, kind: str = "bad") -> dict:
        return {"text": text, "plain": plain or text, "kind": kind}

    status = f.get("status") or "active"
    if status != "active":
        hard.append(why(CLIENT_STATUSES.get(status, status).lower()))
    lost = int(f.get("lost") or 0)
    if lost:
        hard.append(why(f"не вернул велосипед, признан потерянным (аренд: {lost})"))
    if int(f.get("search_now") or 0):
        hard.append(why("аренда сейчас в розыске"))
    searched = int(f.get("searched") or 0)
    if searched:
        bad.append((3, why(f"аренда была в розыске (аренд: {searched})")))
    debt, debt_days = track["debt"], track["debt_days"]
    # Долг сейчас - минус, если это не сегодняшнее начисление идущей
    # аренды: курьер платит вечером, и днём он «должник» по устройству.
    owes = debt > 0 and (not int(f.get("active") or 0) or debt_days >= 1)
    if owes:
        tail = f", {debt_days} дн." if debt_days else ""
        bad.append((3 if debt >= RISK_DEBT_BIG else 2,
                    why(f"долг сейчас {money(debt)}{tail}", f"долг сейчас{tail}")))
    days_, times = track["overdue_days"], track["overdue_times"]
    points = sum(1 for d, t in RISK_OVERDUE
                 if days_ >= d or (t is not None and times >= t))
    if points:
        bad.append((points, why(f"просрочек: {times}, дней в минусе: {days_}")))
    points = _risk_steps(track["max_debt"], RISK_MAX_DEBT)
    if points:
        bad.append((points, why(f"наибольший долг {money(track['max_debt'])}",
                                "был большой долг")))
    fines = int(f.get("fines") or 0)
    points = _risk_steps(fines, RISK_FINES)
    if points:
        bad.append((points, why(f"штрафы и ремонт за его счёт: {fines} на "
                                f"{money(f.get('fines_sum'))}",
                                f"штрафы и ремонт за его счёт: {fines}")))
    early = int(f.get("early") or 0)
    if early >= RISK_EARLY:
        bad.append((1, why(f"досрочных возвратов: {early}")))

    done = int(f.get("done") or 0)
    active_on = f.get("active_on")
    rented_days = int(f.get("rent_days") or 0) \
        + (max((today - active_on).days, 0) if active_on else 0)
    months = rented_days // 30
    points = _risk_steps(done, RISK_DONE)
    if done:
        good.append((points, why(f"закрыто аренд без потерь: {done}", kind="good")))
    points = _risk_steps(months, RISK_MONTHS)
    if months:
        good.append((points, why(f"в аренде {months} мес.", kind="good")))
    if agent_good:
        good.append((1, why("пришёл по приглашению надёжного клиента", kind="good")))
    risk = sum(p for p, _ in bad)
    trust = min(sum(p for p, _ in good), RISK_TRUST_CAP)
    score = risk - trust
    # История - то, на чём стоит доверие; минусы её не создают.
    history = bool(hard or done or rented_days >= RISK_HISTORY_DAYS)
    if hard:
        level = "high"
    elif score >= RISK_HIGH:
        level = "high"
    elif score >= RISK_MEDIUM or owes:
        level = "medium"
    elif not history:
        level = "none"
    else:
        level = "low"
    reasons = hard + [r for _, r in sorted(bad, key=lambda x: -x[0])]
    if history and not hard and not bad:
        reasons.append(why("просрочек, долгов и штрафов не было", kind="good"))
    reasons += [r for _, r in good]
    if level == "none":
        reasons = [why("первая аренда, выводы рано" if f.get("rentals")
                       else "у нас ещё не арендовал", kind="note")] + reasons
    return {"level": level, "label": RISK_LEVELS[level],
            "badge": "нет истории" if level == "none" else f"риск {RISK_LEVELS[level]}",
            "score": score, "reasons": reasons,
            "deposit": (deposits or {}).get(level, Decimal(0)), **track}


def risk_rules() -> list[tuple[str, str]]:
    """Правило оценки словами для страницы настроек - из тех же констант,
    что считают: описание не разойдётся с расчётом."""
    (o1, _), (o2, t2), (o3, t3) = RISK_OVERDUE
    return [
        ("Чёрный список, блокировка, потерянный велосипед, розыск сейчас",
         "сразу высокий"),
        ("Аренда была в розыске", "+3"),
        ("Долг сейчас (кроме сегодняшнего начисления идущей аренды)",
         f"+2, от {money(RISK_DEBT_BIG)} +3; уровень не ниже среднего"),
        ("Просрочки: дни в минусе на конец суток и сколько раз",
         f"от {o1} дн. +1, от {o2} дн. или {t2} раз +2, от {o3} дн. или {t3} раз +3"),
        ("Наибольший долг на конец суток",
         f"от {money(RISK_MAX_DEBT[0])} +1, от {money(RISK_MAX_DEBT[1])} +2"),
        ("Штрафы и ремонт за счёт клиента",
         f"от {RISK_FINES[0]} +1, от {RISK_FINES[1]} +2"),
        ("Досрочные возвраты (раньше первого оплаченного срока)",
         f"от {RISK_EARLY} +1"),
        ("Закрытые аренды без потерь", f"от {RISK_DONE[0]} −1, от {RISK_DONE[1]} −2"),
        ("Месяцев в аренде (дни всех его аренд, не календарь)",
         f"от {RISK_MONTHS[0]} −1, от {RISK_MONTHS[1]} −2"),
        ("Пришёл по приглашению клиента с низким риском", "−1"),
        ("Доверие всего", f"не больше −{RISK_TRUST_CAP}"),
    ]


# ─────────────────────── инструменты списков ───────────────────────
#
# Сортировка кликом по заголовку, размер страницы и подвал с итогом -
# одинаково нужны парку, арендам, нарядам и складу. Один набор правил на
# все списки: каждый со своим устройством разъедется на первой правке.

# Сколько строк показывать за раз. 50 - экран, 300 - «покажи всё»:
# больше трёхсот строк на странице не читает никто, для этого есть
# выгрузка.
LIST_SIZES = (50, 100, 300)
DEFAULT_LIST_SIZE = 50


def check_list_size(raw: Any) -> int:
    try:
        value = int(str(raw or "").strip())
    except ValueError:
        return DEFAULT_LIST_SIZE
    return value if value in LIST_SIZES else DEFAULT_LIST_SIZE


def sort_rows(rows: Iterable[Mapping[str, Any]], key: Any, direction: Any, *,
              allowed: Mapping[str, str] | None = None) -> list[dict]:
    """Отсортировать список по колонке. Неизвестная колонка - как было.

    Порядок не должен зависеть от того, у какой строки поле пустое:
    None уходит в конец при любом направлении, иначе «сортировка по
    клиенту» выносит наверх всё, что ещё не выдано.
    """
    rows = [dict(r) for r in rows]
    field = str(allowed.get(str(key), "") if allowed else key or "").strip()
    if not field:
        return rows
    down = str(direction or "").lower() in ("desc", "down", "-")

    def order(row: Mapping[str, Any]) -> tuple:
        value = row.get(field)
        if value is None or value == "":
            return (1, "")
        if isinstance(value, bool):
            return (0, int(value))
        if isinstance(value, (int, float, Decimal)):
            return (0, value)
        if isinstance(value, (date, datetime)):
            return (0, value.isoformat() if isinstance(value, date) else str(value))
        return (0, str(value).casefold())

    # Ключи разных типов не сравниваются между собой; внутри одной
    # колонки тип один, но пустые значения дают ("", ) против (0, ).
    numeric = all(isinstance(r.get(field), (int, float, Decimal, bool))
                  or r.get(field) in (None, "") for r in rows)
    if numeric:
        def order(row: Mapping[str, Any]) -> tuple:      # noqa: F811
            value = row.get(field)
            return (1, 0) if value in (None, "") else (0, Decimal(str(value)))
    rows.sort(key=order, reverse=down)
    if down:
        # reverse=True утащил бы пустые в начало - возвращаем их назад.
        filled = [r for r in rows if r.get(field) not in (None, "")]
        empty = [r for r in rows if r.get(field) in (None, "")]
        rows = filled + empty
    return rows


def page_of(rows: Sequence[Mapping[str, Any]], size: int = DEFAULT_LIST_SIZE,
            page: Any = 1) -> dict[str, Any]:
    """Страница списка и всё, что нужно подвалу.

    `total` - сколько строк нашлось всего, а не сколько показано: «итого
    77 аренд» при пятидесяти на экране - это и есть ответ на вопрос,
    ради которого открывали список.
    """
    size = size if size in LIST_SIZES else DEFAULT_LIST_SIZE
    total = len(rows)
    pages = max((total + size - 1) // size, 1)
    try:
        number = int(str(page or 1))
    except ValueError:
        number = 1
    number = min(max(number, 1), pages)
    start = (number - 1) * size
    return {"rows": list(rows[start:start + size]), "total": total,
            "page": number, "pages": pages, "size": size,
            "shown": min(size, max(total - start, 0)),
            "has_more": pages > 1}


def sum_of(rows: Iterable[Mapping[str, Any]], field: str) -> Decimal:
    """Сумма колонки по всем найденным строкам - для подвала списка."""
    return to_money(sum((to_money(r.get(field)) for r in rows), Decimal(0)))


# ─────────────────── позиции аренды сверх велосипеда ───────────────────
#
# Второй аккумулятор курьер берёт, чтобы не заряжаться в середине смены,
# и это отдельные деньги. Цена периода у аренды остаётся одна: она и
# начисляется, и попадает в средний чек. Позиции - расшифровка этой цены.

EXTRA_KINDS: dict[str, str] = {"battery": "Доп. аккумулятор"}
# Сколько батарей можно взять сверх той, что стоит в раме. Две - это уже
# полный рюкзак, а больше просят только чтобы перепродать.
MAX_EXTRA_BATTERIES = 2


def extra_title(kind: str, what: Any) -> str:
    """Название позиции так, как оно встанет в договор и в акт."""
    name = str(what or "").strip()
    head = EXTRA_KINDS.get(kind, kind)
    return f"{head} {name}".strip() if name else head


def live_extras(extras: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Действующие позиции: снятая с аренды батарея денег больше не стоит."""
    return [dict(e) for e in extras if not e.get("removed_at")]


def extras_total(extras: Iterable[Mapping[str, Any]]) -> Decimal:
    """Сколько позиции прибавляют к цене периода."""
    return to_money(sum((to_money(e.get("price")) for e in live_extras(extras)),
                        Decimal(0)))


def period_price(base: Any, extras: Iterable[Mapping[str, Any]] = ()) -> Decimal:
    """Цена периода целиком: велосипед плюс всё, что к нему взяли.

    Ровно это число ложится в `rentals.price` и начисляется. Складывать
    цену в двух местах нельзя: однажды сложат по-разному.
    """
    return to_money(to_money(base) + extras_total(extras))


def battery_extra_price(tariffs: Iterable[dict], battery: Mapping[str, Any] | None,
                        period_days: Any) -> Decimal | None:
    """Цена доп. аккумулятора за тот же срок, что и у аренды.

    Нет тарифа на этот срок - None, а не ноль: бесплатная батарея и
    батарея без цены выглядят одинаково, а стоят по-разному, и решать
    это должен человек в тарифах.
    """
    days = int(period_days or 0)
    if days <= 0:
        return None
    model = (battery or {}).get("model_title") or (battery or {}).get("model")
    rows = tariffs_for_model(tariffs, model, kind="battery")
    hit = next((t for t in rows if int(t.get("period_days") or 0) == days), None)
    return to_money(hit["price"]) if hit else None


def battery_options(batteries: Iterable[dict], tariffs: Iterable[dict],
                    period_days: Any) -> list[dict]:
    """Свободные батареи с ценой за период - то, из чего выбирает оператор."""
    out = []
    for row in batteries:
        price = battery_extra_price(tariffs, row, period_days)
        out.append({**row, "extra_price": price, "priced": price is not None})
    return out


# Через сколько дней кнопка «второй аккумулятор» снова появится после
# просьбы. Позицию добавили - кнопки нет и так; не добавили за неделю -
# команда забыла, и клиенту нужен способ напомнить без звонка.
BATTERY_ASK_DAYS = 7


def battery_offer(tariffs: Iterable[dict], rental: Mapping[str, Any] | None,
                  extras: Iterable[Mapping[str, Any]] = (),
                  batteries: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any] | None:
    """Что предложить клиенту при продлении: {price, days, exact} или None.

    `batteries` - то, что точка может выдать вторым (свободные, подходящие
    велосипеду). Цена каждой - ровно та, что возьмёт выдача
    (`battery_extra_price`): тариф модели батареи главнее запасного. Общий
    тариф отдельно не смотрим - при своей цене модели он не сработает, и
    клиенту назвали бы сумму, которую не спишут. Какую дадут, решит
    человек на точке, поэтому exact - только когда цена у всех одна,
    иначе честное «от» самой низкой.

    None - не предлагаем: аренды нет, доп. аккумулятор уже взят, выдать
    нечего или ни у одной батареи нет цены на срок аренды (без цены она
    не выдаётся вовсе).
    """
    if not rental or rental.get("status", "active") != "active":
        return None
    if live_extras(e for e in extras if (e.get("kind") or "battery") == "battery"):
        return None
    days = int(rental.get("period_days") or 0)
    if days <= 0:
        return None
    live = [t for t in tariffs if t.get("active", True)]
    prices = sorted({price for b in batteries
                     if (price := battery_extra_price(live, b, days)) is not None
                     and price > 0})
    if not prices:
        return None
    return {"price": prices[0], "days": days, "exact": len(prices) == 1}


def battery_asked_recently(rental: Mapping[str, Any] | None, *, now: datetime) -> bool:
    """Просьба о втором аккумуляторе ещё свежая - кнопку не показываем."""
    asked = (rental or {}).get("battery_asked_at")
    return isinstance(asked, datetime) and now - asked < timedelta(days=BATTERY_ASK_DAYS)


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
    # Порог тот же, что у чека: пока журналу статусов минуты, потенциал
    # около рубля, а выручка уже за весь период - «КПД 3 850 000 %».
    operational = Decimal(str(metrics.get("operational_days") or 0))
    efficiency = (float(round(100 * earned / potential, 1))
                  if potential and operational >= 1 else None)
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
    # isascii: «²» для isdigit - цифра, и int() на нём ронял форму 500.
    if not (text.isascii() and text.isdigit()):
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
    "inbox": "Входящие: Авито, Telegram, MAX, WhatsApp",
    "clients": "Клиенты",
    "rentals": "Аренды",
    "bikes": "Парк",
    "batteries": "Батареи",
    "trackers": "Трекеры и карта парка",
    "cash": "Касса и банк",
    "mailing": "Рассылки и шаблоны",
    "promos": "Акции",
    "service": "Сервис: наряды и виды работ",
    "inventory": "Склад: запчасти, приходы, заказы",
    "claims": "Заявки на зачисление",
    "finance": "Финансы",
    "tariffs": "Тарифы",
    "reports": "Отчёты",
    # Кабинет франчайзера: чужие копии системы, их цифры и роялти.
    "franchise": "Франчайзи и роялти",
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
    # Заявки на аренду - часть выдачи: из заявки открывается мастер.
    ("/bookings", "issue"),
    # Сделки воронки «Входящих» - путь к выдаче: заявка, касание, договор.
    ("/deals", "issue"),
    # Входящие обращения: по умолчанию только у встроенного «Владельца».
    ("/inbox", "inbox"),
    ("/clients", "clients"),
    # Журнал подписаний - тот же раздел, что и клиенты: подписывает
    # документы тот, кто ведёт клиента. Страница /sign/<токен> в список
    # не входит: она открыта клиенту и стража раздела не знает.
    ("/signings", "clients"),
    ("/rentals", "rentals"),
    # Журнал рабочей группы точек - про аренды: фиксация, замена, сдача.
    ("/ops", "rentals"),
    ("/bikes", "bikes"),
    ("/batteries", "batteries"),
    ("/map", "trackers"),
    ("/trackers", "trackers"),
    ("/alerts", "trackers"),
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
    ("/promos", "promos"),
    # После /finance: home_for берёт первый путь раздела, а /plan - это
    # форма на сводке, открывать её как страницу нечего.
    ("/plan", "finance"),
    ("/assets", "finance"),
    ("/tariffs", "tariffs"),
    ("/reports", "reports"),
    # Франчайзи: по умолчанию только у встроенного «Владельца», как
    # «Входящие», - цифры чужого бизнеса менеджеру точки ни к чему.
    ("/franchisees", "franchise"),
    ("/import", "import"),
    ("/staff", "staff"),
    # Команда: план, факт и зарплата сотрудников - тот же раздел, кто
    # ведёт людей, тот и видит их выработку.
    ("/team", "staff"),
    ("/profiles", "staff"),
    ("/company", "settings"),
    # Готовность установки: что не настроено - вкладка тех же настроек.
    ("/readiness", "settings"),
    # Мастер первого запуска - та же «Готовность» по шагам.
    ("/setup", "settings"),
    ("/notices", "settings"),
    ("/intake", "settings"),
    ("/documents", "settings"),
    ("/locations", "settings"),
    ("/models", "settings"),
    # Правило оценки риска и залог по уровням - решение владельца.
    ("/risk", "settings"),
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
    в «нет доступа» на первом же экране. Без сводки, но с задачами (выдача,
    аренды, сервис, тревоги, касса) - «Мои задачи»: сотруднику точки
    нужен свой список на сегодня, а не первый раздел меню."""
    if can_view(staff, "dashboard"):
        return "/"
    if any(can_view(staff, code) for code in TASK_SECTIONS):
        return "/my"
    first = next((code for code in visible_sections(staff) if code != "dashboard"), None)
    if first is None:
        return "/me"
    return next(path for path, code in SECTION_PATHS if code == first)


BUILT_IN_PROFILES: tuple[tuple[str, str, dict[str, Any], bool], ...] = (
    ("owner", "Владелец",
     {"sections": dict.fromkeys(SECTIONS, "edit"),
      "actions": dict.fromkeys(ACTIONS, True)}, True),
    ("manager", "Администратор",
     {"sections": {"dashboard": "view", "issue": "edit", "clients": "edit",
                   "rentals": "edit", "bikes": "view", "service": "view",
                   "claims": "edit", "finance": "view", "tariffs": "view",
                   "reports": "view", "inventory": "view", "batteries": "view",
                   "trackers": "view", "cash": "edit", "mailing": "view",
                   "promos": "view"},
      "actions": {}}, False),
    ("tech", "Мастер",
     {"sections": {"dashboard": "view", "bikes": "edit", "service": "edit",
                   "rentals": "view", "reports": "view", "inventory": "edit",
                   "batteries": "edit", "trackers": "view"},
      "actions": {}}, False),
)


# Профили «только задачи» (schema.sql кладёт их один раз, не встроенные):
# сотрудник видит «Мои задачи» и карточки, куда они ведут, но не сводку,
# деньги, отчёты и настройки. Без «dashboard» вход ведёт на /my (home_for).
TASK_PROFILES: tuple[tuple[str, str, dict[str, Any], bool], ...] = (
    ("tasks_operator", "Администратор: только задачи",
     {"sections": {"issue": "edit", "clients": "edit", "rentals": "edit", "cash": "edit",
                   "claims": "edit", "bikes": "view", "batteries": "view",
                   "trackers": "view"},
      "actions": {}}, False),
    ("tasks_tech", "Мастер: только задачи",
     {"sections": {"service": "edit", "bikes": "edit", "batteries": "edit",
                   "inventory": "edit", "trackers": "view"},
      "actions": {}}, False),
)
# Разделы, по которым у сотрудника бывают свои задачи (app/crm/mytasks.py).
TASK_SECTIONS: tuple[str, ...] = ("issue", "rentals", "service", "trackers", "claims",
                                  "cash")


def check_profile_name(raw: Any) -> Check:
    return check_name(raw, what="Название роли")


def role_title(staff: dict | None) -> str:
    """Роль сотрудника словами - название его профиля доступа («Мастер»).

    Под именем в меню стоит именно она. Старая колонка staff.role знает
    только admin/manager и мастеру писала «Менеджер»: права давно решает
    профиль, и подпись должна говорить то же, что права.
    """
    staff = staff or {}
    name = str(staff.get("profile_name") or "").strip()
    if name:
        return name
    return "Владелец" if staff.get("role") == "admin" else "Без роли"


def role_summary(perms: Any, *, limit: int = 4) -> str:
    """Что роль делает, одной строкой для выбора роли: разделы, где она
    меняет, по порядку меню; хвост - числом. Пусто - «только смотрит»."""
    sections = normalize_perms(perms)["sections"]
    edit = [SECTIONS[c] for c in SECTIONS if sections.get(c) == "edit"]
    view = [c for c in SECTIONS if sections.get(c) == "view"]
    if not edit:
        return "только смотрит" if view else "ничего не открыто"
    short = [label.split(":")[0].lower() for label in edit[:limit]]
    short[0] = short[0][:1].upper() + short[0][1:]
    tail = len(edit) - limit
    return ", ".join(short) + (f" и ещё {tail}" if tail > 0 else "")


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

# Группы прайса сервиса (регламент № 11) - как в печатном листе, чтобы
# владелец узнавал свой прайс. Хвост - категории первого сида: строки с
# ними остались на старых установках, и выпадающий список обязан их
# показывать, иначе первое же сохранение молча сменит категорию.
WORK_CATEGORIES: tuple[str, ...] = (
    "Передняя часть", "Задняя часть", "Мотор-колесо и шиномонтаж",
    "Аккумуляторы", "Гидроизоляция", "Рама и резьба", "Minako",
    "Штрафы и порча имущества", "ТО",
    "Электрика", "Тормоза", "Ходовая", "Свет", "Прочее",
)
FINES_CATEGORY = "Штрафы и порча имущества"

# Два листа прайса. Лист выбирается по объекту наряда: свой велосипед -
# арендатору, чужая техника - стороннему. Плательщик здесь ни при чём:
# он решает, выставят ли цену, а не какую.
PRICE_SHEETS: dict[str, str] = {"own": "Арендатору", "ext": "Стороннему"}
_SHEET_COLUMNS = {"own": ("price", "parts_price"),
                  "ext": ("price_ext", "parts_price_ext")}


def price_sheet(order: Mapping[str, Any] | None) -> str:
    """Какой лист прайса у наряда: без своего велосипеда - сторонний."""
    return "own" if (order or {}).get("bike_id") else "ext"


def sheet_price(work_type: Mapping[str, Any], sheet: str = "own") -> Decimal | None:
    """Цена работы клиенту по листу: работа плюс запчасть. None - в этом
    листе такой работы нет, и подставлять нечего."""
    work_col, parts_col = _SHEET_COLUMNS.get(sheet, _SHEET_COLUMNS["own"])
    work = work_type.get(work_col)
    if work is None:
        return None
    return to_money(work) + to_money(work_type.get(parts_col) or 0)


def work_type_rows(types: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Каталог с итогами по обоим листам - для списка и выгрузки."""
    rows = []
    for t in types:
        rows.append({**t, "own_total": sheet_price(t, "own"),
                     "ext_total": sheet_price(t, "ext")})
    return rows


def priced_types(types: Iterable[Mapping[str, Any]], sheet: str) -> list[dict]:
    """Каталог для формы строки наряда: цена того листа, что у наряда."""
    return [{**t, "sheet_total": sheet_price(t, sheet)} for t in types]


def fine_presets(types: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Позиции прайса арендатора для журнала клиента: штрафы первыми.

    Штраф за потерянную сумку - не ремонт, наряда под него нет, и
    оператор пишет его в журнал руками. Прайс подсказывает сумму и
    название, чтобы в заметке не оказалось «сумка 2000» пятью почерками.
    """
    rows = [{"id": t["id"], "title": t["title"], "category": t.get("category") or "",
             "total": sheet_price(t, "own")}
            for t in types if t.get("active", True)]
    rows = [r for r in rows if r["total"] is not None and r["total"] > 0]
    rows.sort(key=lambda r: (r["category"] != FINES_CATEGORY,
                             WORK_CATEGORIES.index(r["category"])
                             if r["category"] in WORK_CATEGORIES else 99))
    return rows

# Сколько суток наряд может стоять, прежде чем это станет заметно.
# Велосипед в ремонте - это велосипед вне аренды, то есть прямой простой.
# Это общий срок ремонта по умолчанию: владелец правит его настройкой
# `repair_norm_days`, а узлам ставит свой (`repair_nodes.norm_days`).
# «Срок», а не «норма»: нормой ремонта в плане месяца зовётся другое -
# сколько велосипедов в ремонте держать нормально.
ORDER_STUCK_DAYS = 3
REPAIR_NORM_MAX = 365


def repair_norm_default(settings: Mapping[str, Any] | None = None) -> int:
    """Общий срок ремонта, суток. Мусор в настройке - к умолчанию: срок
    минус три дня превратил бы в просрочку весь сервис."""
    try:
        value = int(str((settings or {}).get("repair_norm_days")))
    except (TypeError, ValueError):
        return ORDER_STUCK_DAYS
    return value if 0 <= value <= REPAIR_NORM_MAX else ORDER_STUCK_DAYS


def check_norm_days(raw: Any) -> Check:
    """Срок ремонта узла из формы. Пусто - «своего нет, общий»; ноль -
    честный ноль: колодки меняют в тот же день."""
    text = str(raw or "").strip()
    if not text:
        return Check(True, None)
    value = parse_id(text)
    if value is None or value > REPAIR_NORM_MAX:
        return Check(False, error=f"Срок: целое число суток от 0 до {REPAIR_NORM_MAX}.")
    return Check(True, value)


def order_norm(order: Mapping[str, Any] | None,
               default: int = ORDER_STUCK_DAYS) -> int:
    """Срок наряда: самый долгий из сроков его строк. Строка меряется
    сроком своего узла, а узел без своего срока и строка без узла - общим
    (`norm_general` из запроса); наряд без строк - тоже общим.

    Самый долгий, а не первый: наряд «колодки плюс мотор-колесо» стоит,
    пока не сделан мотор, и срок колодок сделал бы его просроченным
    на второй день. По той же причине строка без своего срока не
    выпадает из счёта: добавленная работа не может укоротить срок.
    """
    order = order or {}
    node = order.get("node_norm")
    if node is None:
        return int(default)
    if order.get("norm_general"):
        return max(int(node), int(default))
    return int(node)


def order_norm_node(order: Mapping[str, Any] | None,
                    default: int = ORDER_STUCK_DAYS) -> str | None:
    """Узел, по которому считается срок наряда; None - действует общий.
    Подпись к «срок N дн.» обязана совпадать с самим сроком: колодки в
    подписи при сроке в трое суток от проводки читались бы как ошибка."""
    order = order or {}
    node = order.get("node_norm")
    if node is None or (order.get("norm_general") and int(default) > int(node)):
        return None
    return order.get("norm_node")


def order_overdue(order: Mapping[str, Any] | None, *, default: int = ORDER_STUCK_DAYS,
                  today: date | None = None) -> int:
    """На сколько суток открытый наряд пересидел срок. 0 - укладывается
    или закрыт: закрытый простоя больше не копит."""
    if not order_is_open(dict(order or {})):
        return 0
    return max(order_days(dict(order or {}), today=today)
               - order_norm(order, default), 0)


def overdue_orders(orders: Iterable[Mapping[str, Any]], *,
                   default: int = ORDER_STUCK_DAYS,
                   today: date | None = None) -> list[dict]:
    """Открытые наряды дольше срока - дольше всех просроченные первыми."""
    rows = []
    for order in orders:
        late = order_overdue(order, default=default, today=today)
        if late:
            rows.append({**order, "norm": order_norm(order, default),
                         "norm_node": order_norm_node(order, default),
                         "days": order_days(dict(order), today=today),
                         "overdue": late})
    rows.sort(key=lambda r: (-r["overdue"], str(r.get("no") or "")))
    return rows


def repair_overdue_lines(rows: Sequence[Mapping[str, Any]], *, limit: int = 15) -> str:
    """Сводка просроченных нарядов для служебного чата, HTML.

    Узел в строке - тот, по которому считался срок: без него «срок 5»
    читается как придирка, а с ним видно, что держит велосипед.
    """
    if not rows:
        return ""
    lines = [f"⏱ Наряды дольше срока ремонта: {len(rows)}"]
    for row in rows[:limit]:
        what = html.escape(str(row.get("bike_code") and f"№ {row['bike_code']}"
                               or row.get("object_note") or "—"), quote=False)
        node = row.get("norm_node")
        norm = (f"срок {row['norm']} дн."
                + (f" ({html.escape(str(node), quote=False)})" if node else ""))
        stage = ORDER_STATUSES.get(str(row.get("status") or ""), "")
        lines.append(f"• {row.get('no')} — {what}: {row['days']} дн., {norm}, "
                     f"просрочено на {row['overdue']} дн. · {stage}")
    if len(rows) > limit:
        lines.append(f"…и ещё {len(rows) - limit}: «Сервис → Наряды».")
    return "\n".join(lines)


def order_no(number: int) -> str:
    """Человекочитаемый номер наряда: на него ссылаются в переписке."""
    return f"РЕМ-{int(number):06d}"


def check_order_status(raw: Any) -> Check:
    return check_choice(raw, ORDER_STATUSES, what="Статус наряда")


def check_payer(raw: Any) -> Check:
    return check_choice(raw, PAYERS, what="Плательщик")


def item_qty(item: Mapping[str, Any]) -> int:
    """Количество в строке. Пусто - одна штука, ноль - именно ноль.

    `qty` в базе `not null default 1`, поэтому `or 1` срабатывал ровно
    на нуле: в таблице наряда строка печаталась как 0 ₽, а в «Итого»,
    в смету и в журнал ремонта уходила как одна штука.
    """
    qty = item.get("qty")
    return 1 if qty is None else int(qty)


def item_total(item: dict) -> Decimal:
    """Строка наряда клиенту: цена за единицу на количество."""
    return to_money(item.get("price") or 0) * item_qty(item)


def item_cost(item: dict) -> Decimal:
    """Себестоимость строки: запчасти плюс работа, тоже на количество."""
    parts = to_money(item.get("parts_cost") or 0) + to_money(item.get("labor_cost") or 0)
    return parts * item_qty(item)


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
    start = local_date(opened)
    closed = order.get("closed_at")
    end = local_date(closed) or today or date.today()
    return max((end - start).days, 0)


def order_is_open(order: dict | None) -> bool:
    return bool(order) and str(order.get("status") or "") in ORDER_OPEN


def order_stuck(order: dict, *, today: date | None = None,
                default: int = ORDER_STUCK_DAYS) -> bool:
    """Наряд стоит дольше срока - велосипед копит простой. Срок - самый
    долгий из сроков его строк, общий (`default`) - у строк без своего."""
    return order_overdue(order, default=default, today=today) > 0


def service_rows(bikes: Iterable[dict], orders_by_bike: dict[int, dict], *,
                 today: date | None = None,
                 norm: int = ORDER_STUCK_DAYS) -> list[dict]:
    """Рабочий стол сервиса: велосипеды в ремонте и что с ними.

    Главная строка здесь - «в ремонте, а наряда нет»: велосипед стоит,
    никто им не занят, и в отчёте простоя он выглядит как обычный ремонт.
    Такие идут первыми и по убыванию суток. `norm` - общий срок ремонта:
    им меряются строки наряда без своего срока (`order_norm`).
    """
    today = today or date.today()
    rows = []
    for bike in bikes:
        if bike.get("status") not in ("repair", "maintenance"):
            continue
        order = orders_by_bike.get(bike["id"])
        days = order_days(order, today=today) if order else (bike.get("idle_days") or 0)
        order = order or {}
        overdue = order_overdue(order, default=norm, today=today) if order else 0
        rows.append({**bike, "order": order or None,
                     "stage": ORDER_STATUSES.get(order.get("status"), "Без наряда"),
                     "days": days,
                     # Что этот велосипед уже не заработал, пока стоит.
                     "lost": idle_cost(days),
                     "norm": order_norm(order, norm) if order else None,
                     "overdue": overdue,
                     "stuck": (not order) or overdue > 0,
                     # Плоские поля наряда - по ним сортируют, ищут и
                     # выгружают: вложенный словарь для этого не годится.
                     "order_no": order.get("no") or "",
                     "tech": order.get("tech_name") or "",
                     "payer_title": PAYERS.get(str(order.get("payer") or ""), ""),
                     "client": order.get("client_name") or "",
                     "estimate": to_money(order.get("estimate") or 0),
                     "complaint": order.get("complaint") or bike.get("note") or ""})
    # Без наряда - в начало: это и есть потерянные велосипеды сервиса.
    rows.sort(key=lambda r: (r["order"] is not None, -r["days"]))
    return rows


def service_summary(rows: Iterable[dict]) -> dict[str, Any]:
    """Сводка рабочего стола: сколько стоит, сколько без наряда и почём.

    Деньги здесь - оценка по цели среднего чека, а не факт: велосипед,
    который стоит, не заработал ничего, и «сколько бы он принёс» -
    единственный честный способ это назвать.
    """
    rows = list(rows)
    days = sum(r["days"] for r in rows)
    return {
        "total": len(rows),
        "no_order": sum(1 for r in rows if r["order"] is None),
        "stuck": sum(1 for r in rows if r["stuck"]),
        # Плитка «дольше срока» ведёт в /orders?overdue=1, поэтому считает
        # только наряды сверх срока: велосипед без наряда там не виден, и
        # у него своя плитка - иначе число и список расходились бы.
        "overdue": sum(1 for r in rows if r.get("overdue")),
        # Два состояния, где техника стоит не из-за нас: ждём клиента
        # и ждём поставщика. Их видно плитками, а не только фильтром.
        "approving": sum(1 for r in rows
                         if (r["order"] or {}).get("status") == "approve"),
        "waiting": sum(1 for r in rows
                       if (r["order"] or {}).get("status") == "waiting"),
        "days": days,
        "lost": idle_cost(days),
        # Сколько они стоят нам каждый следующий день, пока стоят.
        "per_day": idle_cost(len(rows)),
    }


def idle_cost(days: Any, *, rate: Any = CHECK_TARGET) -> Decimal:
    """Во сколько обходится простой: велосипеде-дни по цели чека.

    Цель, а не фактический чек: потери должны показывать расстояние до
    цели, а не подстраиваться под слабый месяц. Целые рубли - копейки
    в оценке выглядят точностью, которой нет.
    """
    return (Decimal(str(days or 0)) * to_money(rate)).quantize(
        Decimal(1), rounding=ROUND_HALF_UP)


# ───────────────────────── пересчёт техники ─────────────────────────

TAKE_SCOPES: dict[str, str] = {"all": "Весь парк", "location": "Одна точка"}
# Что считаем. Отдельной ведомости на батареи нет намеренно: человек с
# телефоном обходит точку один раз и вводит номера подряд, а какой из
# них чей - разбирается система.
TAKE_WHAT: dict[str, str] = {"all": "Всё", "bikes": "Велосипеды",
                             "batteries": "Аккумуляторы"}
TAKE_STATES: dict[str, str] = {
    "expected": "Не отмечен", "found": "На месте",
    "missing": "Не нашли", "extra": "Лишний",
}
# Кого ждём увидеть на точке. rented - у курьера, его на месте нет и быть
# не должно; sold и written_off из парка вышли. lost в ведомость не ставим:
# он уже потерян, а если найдётся - попадёт в неё лишним, ради этого
# пересчёт и затевается.
TAKE_EXPECTED_STATUSES = ("available", "repair", "maintenance", "reserved")
# То же для батарей. Резерва у них нет, «на сборке» тоже не ждём: она
# ещё не в обороте, и её отсутствие на точке ничего не значит.
TAKE_EXPECTED_BATTERY_STATUSES = ("available", "repair", "maintenance")


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


def expected_batteries(batteries: Iterable[dict], *, scope: str,
                       location: str | None = None) -> list[dict]:
    """Какие батареи ждём на точке. Снимок, как и у велосипедов."""
    rows = [b for b in batteries
            if b.get("status") in TAKE_EXPECTED_BATTERY_STATUSES]
    if scope == "location":
        rows = [b for b in rows if (b.get("location") or "") == (location or "")]
    return sorted(rows, key=lambda b: str(b.get("code") or ""))


def take_counts_by_kind(items: Iterable[dict]) -> dict[str, dict[str, int]]:
    """Счётчики отдельно по велосипедам и батареям.

    Сводное число ничего не говорит о том, где именно недостача, а
    искать пропавшую батарею и пропавший велосипед - разные разговоры.
    """
    rows = list(items)
    out = {}
    for kind, key in (("bikes", "bike_id"), ("batteries", "battery_id")):
        part = [i for i in rows if i.get(key)]
        out[kind] = {**take_counts(part), "any": bool(part)}
    return out


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
    what = TAKE_WHAT.get(str(take.get("what") or "bikes"), "")
    where = (f"Точка {take.get('location') or '—'}"
             if str(take.get("scope") or "") == "location" else TAKE_SCOPES["all"])
    return f"{what} · {where}" if what else where


# ─────────────────────── отчёты сервиса ───────────────────────
#
# Три вопроса, на которые окупаемость по моделям не отвечает: кто из
# техников сколько сделал, какая модель дороже всех обходится в
# запчастях и что вообще уходит со склада.

def tech_rows(rows: Iterable[dict]) -> list[dict]:
    """Выработка техников: наряды, деньги и средний срок ремонта."""
    out = []
    for row in rows:
        orders = int(row.get("orders") or 0)
        total = to_money(row.get("total"))
        cost = to_money(row.get("cost"))
        days = Decimal(str(row.get("days") or 0))
        out.append({
            **row,
            "orders": orders,
            "client_orders": int(row.get("client_orders") or 0),
            "total": total, "cost": cost,
            # Работы - это то, что осталось от суммы наряда за вычетом
            # запчастей: по ней и видно, сколько человек наработал
            # руками, а не сколько прошло через него железа.
            "works": to_money(total - cost),
            "avg_days": (days / orders).quantize(Decimal("0.1"),
                                                 rounding=ROUND_HALF_UP)
            if orders else Decimal(0),
            "avg_total": to_money(total / orders) if orders else Decimal(0),
        })
    return out


def tech_total(rows: Iterable[dict]) -> dict[str, Any]:
    rows = list(rows)
    orders = sum(r["orders"] for r in rows)
    total = to_money(sum((r["total"] for r in rows), Decimal(0)))
    cost = to_money(sum((r["cost"] for r in rows), Decimal(0)))
    return {"orders": orders, "total": total, "cost": cost,
            "works": to_money(total - cost),
            "client_orders": sum(r["client_orders"] for r in rows),
            "avg_total": to_money(total / orders) if orders else Decimal(0)}


def model_parts_rows(rows: Iterable[dict], bikes: Iterable[dict] | None = None,
                     *, days: Any = 0) -> list[dict]:
    """Траты по моделям: во сколько обходятся запчасти на велосипед.

    Само по себе «модель съела 40 000 ₽» ничего не говорит: у одной
    модели в парке пятьдесят штук, у другой пять. Поэтому рядом - трата
    на один велосипед и на велосипед в день.
    """
    fleet: dict[str, int] = {}
    for bike in bikes or []:
        if bike.get("status") in OPERATIONAL_STATUSES:
            model = str(bike.get("model") or "—")
            fleet[model] = fleet.get(model, 0) + 1
    span = Decimal(str(days or 0))
    out = []
    for row in rows:
        model = str(row.get("model") or "—")
        # В движениях расход отрицательный; человеку знак здесь ничего
        # не добавляет - «модель съела −4 500 ₽» читается хуже.
        cost = abs(to_money(row.get("cost")))
        count = fleet.get(model, 0)
        per_bike = to_money(cost / count) if count else None
        out.append({
            **row, "model": model, "cost": cost,
            "orders": int(row.get("orders") or 0),
            "qty": abs(int(row.get("qty") or 0)),
            "bikes": count,
            "per_bike": per_bike,
            "per_bike_day": (per_bike / span).quantize(CENT,
                                                       rounding=ROUND_HALF_UP)
            if per_bike is not None and span > 0 else None,
        })
    out.sort(key=lambda r: -r["cost"])
    return out


def spend_rows(rows: Iterable[dict]) -> list[dict]:
    """Расход склада: qty и cost в движениях отрицательные - показываем
    человеку положительные числа, знак здесь ничего не добавляет."""
    out = []
    for row in rows:
        out.append({**row,
                    "qty": abs(int(row.get("qty") or 0)),
                    "cost": abs(to_money(row.get("cost"))),
                    "orders": int(row.get("orders") or 0),
                    "node_title": REPAIR_NODES.get(str(row.get("node") or ""),
                                                   "—")})
    out.sort(key=lambda r: -r["cost"])
    return out


def spend_total(rows: Iterable[dict]) -> dict[str, Any]:
    rows = list(rows)
    return {"qty": sum(r["qty"] for r in rows),
            "cost": to_money(sum((r["cost"] for r in rows), Decimal(0))),
            "titles": len(rows)}


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


# ─────────────────────── выгодность тарифов ───────────────────────
#
# Какой срок аренды выгоднее: чек и удержание рядом. Строка - срок тарифа
# при выдаче (rentals.issue_period_days, а пока тариф не меняли -
# period_days), по желанию ещё и модель каталога. Деньги - платежи
# периода, отнесённые к аренде единым правилом журнала
# (db._ledger_rentals); дни в аренде - журнал статусов, отнесённый к той
# выдаче велосипеда, что тогда шла. Строки вместе со
# строкой «без аренды» складываются в общие три числа за тот же период.
#
# Исход аренды - продлили ли, сдали ли раньше срока, остался ли долг -
# берётся у закрытых за период: у идущей он ещё не известен, и месяц,
# выданный неделю назад, иначе выглядел бы «ни разу не продлённым».
# Признанная потерянной закрыта, но не сдана: в срок, продления и «раньше
# срока» она не идёт (её конец - порог розыска, а не решение курьера),
# в долг - идёт, и считается отдельно.

# Меньше стольких сданных за период аренд удержание срока не сравниваем:
# «продлили двое из двух» - это не 100 %.
TARIFF_MIN_FINISHED = 5
# Меньше стольких дней в аренде не сравниваем чек: неделя одного курьера -
# это клиент, а не тариф.
TARIFF_MIN_DAYS = 30
NO_RENTAL_TITLE = "без аренды"
PERIOD_TITLES: dict[int, str] = {1: "Сутки", 7: "Неделя", 14: "Две недели", 30: "Месяц"}
_TENTH = Decimal("0.1")


def period_title(days: Any) -> str:
    """Срок тарифа словами: 7 - «Неделя», 21 - «21 дн.»."""
    n = int(days or 0)
    return PERIOD_TITLES.get(n) or f"{n} дн."


def percent_of(part: Any, whole: Any) -> float | None:
    """Доля в процентах с одним знаком. От нуля - None, а не 0 %: «ноль
    из нуля» - это отсутствие данных, а не плохой результат."""
    total = Decimal(str(whole or 0))
    if total <= 0:
        return None
    return float(round(100 * Decimal(str(part or 0)) / total, 1))


def _mean(values: Sequence[Any]) -> Decimal | None:
    """Среднее с одним знаком; пусто - None: среднего из ничего нет."""
    if not values:
        return None
    return (Decimal(sum(values)) / len(values)).quantize(_TENTH, rounding=ROUND_HALF_UP)


def early_return(rental: Mapping[str, Any]) -> bool:
    """Сдал посреди оплаченного срока: не в его последний день и не в день
    следующего платежа. Потерянный не сдан вовсе.

    Срок - последнее начисление, начатое до дня сдачи (term_from,
    term_to), а не billed_until: ночной проход начисляет новый срок утром
    в день платежа, и курьер, сдавший велосипед в тот же день, иначе
    числился бы вернувшим его на шесть дней раньше. По начислениям, а не
    по сроку тарифа от начала: смена тарифа сдвигает границы сроков.
    Начислений нет (ручная аренда без них) - целые сроки тарифа от
    начала. У суточного тарифа раньше срока не сдают по определению.
    """
    start, end = rental.get("started_on"), rental.get("closed_on")
    if rental.get("lost") or start is None or end is None or end < start:
        return False
    term_from, term_to = rental.get("term_from"), rental.get("term_to")
    if term_from is not None and term_to is not None:
        return term_from <= end < term_to - timedelta(days=1)
    period = int(rental.get("period_days") or 0)
    if period < 2:
        return False
    used = (end - start).days
    return used < period - 1 or 1 <= used % period <= period - 2


def _tariff_row(key: Any, items: list[Mapping[str, Any]], *,
                total_paid: Decimal) -> dict[str, Any]:
    issued = [r for r in items if r.get("issued")]
    finished = [r for r in items if r.get("finished")]
    # Исход - у сданных: потерянная кончилась порогом розыска.
    returned = [r for r in finished if not r.get("lost")]
    count = len(returned)
    paid = to_money(sum((to_money(r.get("paid")) for r in items), Decimal(0)))
    rented = sum((Decimal(str(r.get("rented_days") or 0)) for r in items), Decimal(0))
    charged = to_money(sum((to_money(r.get("charged")) for r in finished), Decimal(0)))
    debt = to_money(sum((to_money(r.get("debt")) for r in finished), Decimal(0)))
    renewals = [int(r.get("renewals") or 0) for r in returned]
    renewed = sum(1 for n in renewals if n > 0)
    early = sum(1 for r in returned if early_return(r))
    lengths = [max((r["closed_on"] - r["started_on"]).days, 0) for r in returned
               if r.get("closed_on") and r.get("started_on")]
    # Цена по тарифу - велосипеда, без доп. аккумулятора (base_price), как
    # у акций: сравниваются сроки, а не комплектация. Цена - выдачи
    # (tariff_changed: база отдаёт issue_base_price), название тарифа -
    # только у несменённых: у сменённого оно уже нового срока.
    prices = [per_day(r.get("base_price") or r.get("price"), r.get("period_days"))
              for r in issued if int(r.get("period_days") or 0) > 0]
    if key == "total":
        title, period, model = "Итого", None, None
    elif key is None:
        title, period, model = NO_RENTAL_TITLE, None, None
    else:
        period, model = key
        title = period_title(period) + (f" · {model}" if model else "")
    return {
        "key": key, "title": title, "period_days": period, "model": model,
        # Названия тарифов строки, если они не повторяют срок словами.
        "names": sorted({str(r.get("tariff_name") or "").strip()
                         for r in items
                         if r.get("id") is not None and not r.get("tariff_changed")}
                        - {"", period_title(period) if period else ""}),
        "issued": len(issued), "finished": len(finished), "returned": count,
        "lost": len(finished) - count,
        "lost_share": percent_of(len(finished) - count, len(finished)),
        "avg_days": _mean(lengths),
        "renewed": renewed, "renewed_share": percent_of(renewed, count),
        "avg_renewals": _mean(renewals),
        "early": early, "early_share": percent_of(early, count),
        "paid": paid, "revenue_share": percent_of(paid, total_paid),
        "rented_days": rented,
        # Тот же средний чек, что на сводке, - и то же правило «от одного
        # полного велосипеде-дня».
        "avg_check": fleet_metrics({"rented": rented}, paid)["avg_check"],
        "charged": charged, "debt": debt,
        "debtors": sum(1 for r in finished if to_money(r.get("debt")) > 0),
        "debt_share": percent_of(debt, charged),
        "price_per_day": (to_money(sum(prices, Decimal(0)) / len(prices))
                          if prices else None),
        "few": count < TARIFF_MIN_FINISHED, "thin": rented < TARIFF_MIN_DAYS,
        "prepaid": False, "best_check": False, "best_hold": False,
    }


def tariff_rows(rentals: Iterable[Mapping[str, Any]], *, by_model: bool = False,
                aliases: Mapping[str, str] | None = None,
                window_days: int | None = None) -> dict[str, Any]:
    """Сравнение сроков аренды за период: строка на срок (или срок и
    модель каталога), «без аренды» - если в ней что-то есть, и «Итого».

    rentals - ответ CrmDB.tariff_rentals: строка на аренду с деньгами и
    днями периода и флагами issued (выдана в периоде) и finished (закрыта
    в периоде); id None - платежи клиентов без аренд и дни «в аренде» без
    аренды. «Итого» - сумма строк, его чек - средний чек парка.

    best: check - лучший чек, hold - дольше держит (средний срок сданных,
    при равенстве - доля продливших), both - один срок выигрывает и то и
    другое. Не по доле продлений: 21 день - два продления недели и ни
    одного у месяца, и доля выбирала бы самый короткий срок, даже когда
    месячные держат вдвое дольше. Спорят только сроки с данными (few,
    thin) и только когда их хотя бы два: «лучший из одного» ничего не
    говорит.
    Срок длиннее половины периода (window_days - его сутки) в спор о чеке
    не идёт (prepaid): его оплата вперёд ложится в период целиком, а дни -
    кусками, и месяц в окне тридцати дней выигрывал бы чек предоплатой.
    """
    items = list(rentals)
    total_paid = to_money(sum((to_money(r.get("paid")) for r in items), Decimal(0)))
    groups: dict[tuple[int, str | None], list[Mapping[str, Any]]] = {}
    orphans = []
    for r in items:
        if r.get("id") is None:
            orphans.append(r)
            continue
        model = (catalogue_model(r.get("model"), aliases) or "—") if by_model else None
        groups.setdefault((int(r.get("period_days") or 0), model), []).append(r)
    rows = [_tariff_row(key, groups[key], total_paid=total_paid)
            for key in sorted(groups, key=lambda k: (k[0], k[1] or ""))]
    if any(to_money(r.get("paid")) or Decimal(str(r.get("rented_days") or 0))
           for r in orphans):
        rows.append(_tariff_row(None, orphans, total_paid=total_paid))
    for row in rows:
        row["prepaid"] = bool(window_days and row["period_days"]
                              and 2 * row["period_days"] > window_days)
    # Есть ли число, а не правда ли оно: срок, где чек 0,00 или сдают в
    # день выдачи, - худший в споре, а не выбывший из него.
    checks = [r for r in rows if r["key"] is not None and not r["thin"]
              and not r["prepaid"] and r["avg_check"] is not None]
    holds = [r for r in rows if r["key"] is not None and not r["few"]
             and r["avg_days"] is not None]
    check = (max(checks, key=lambda r: (r["avg_check"], r["avg_days"] or 0))
             if len(checks) > 1 else None)
    hold = (max(holds, key=lambda r: (r["avg_days"], r["renewed_share"] or 0,
                                      r["avg_check"] or 0))
            if len(holds) > 1 else None)
    for row in rows:
        row["best_check"] = row is check
        row["best_hold"] = row is hold
    return {"rows": rows, "total": _tariff_row("total", items, total_paid=total_paid),
            "best": {"check": check, "hold": hold,
                     "both": check if check is not None and check is hold else None}}


# ─────────────────────── что купить следующим ───────────────────────
#
# Подсказка к следующей партии: какую модель и сколько. Модель - название
# каталога: в парке один велосипед записан по накладной, другой
# по-клиентски, а заявка из кабинета - всегда по-клиентски. Деньги модели -
# из окупаемости (payback_rows поверх того же model_money), дни - журнал
# статусов на журнале мест по модели и суткам, давление спроса - сутки без
# свободной модели на точке и открытые заявки, которым сейчас нечего
# выдать. Брать стоит модель, которая почти не стоит (простой ниже цели) и
# окупается быстрее срока службы; столько, чтобы при спросе периода простой
# вышел на цель, считая парк сейчас и на сборке.

# Период по умолчанию: решение о партии на месяц данных - это решение по
# одной выдаче партии или одному празднику.
BUY_PERIOD_DAYS = 90
# Меньше стольких велосипеде-дней модели за период судить о ней рано:
# два велосипеда неделю - это не статистика.
BUY_MIN_DAYS = 30
# Сутки «без свободной»: модель на точке ездила (в аренде хотя бы
# полвелосипеда за сутки), а свободной её не было и половины суток -
# пришедший за ней курьер скорее ушёл ни с чем. Единственный велосипед,
# простоявший сутки в ремонте, сюда не попадает: это поломка, а не спрос.
BUY_BUSY_MIN = Decimal("0.5")
BUY_FREE_MIN = Decimal("0.5")
BUY_VERDICTS: dict[str, str] = {"buy": "брать", "hold": "хватает", "skip": "не брать",
                                "few": "мало данных"}


def _buy_title(model: Any, aliases: Mapping[str, str] | None) -> str:
    """Модель парка названием каталога; пустая - «—», как в окупаемости."""
    return catalogue_model(model, aliases) or "—"


def model_days(rows: Iterable[Mapping[str, Any]], *,
               aliases: Mapping[str, str] | None = None) -> dict[str, dict[str, Decimal]]:
    """Велосипеде-дни модели каталога по статусам за период - сумма
    суточных строк CrmDB.model_point_days по точкам и суткам."""
    out: dict[str, dict[str, Decimal]] = {}
    for r in rows:
        cell = out.setdefault(_buy_title(r["model"], aliases), {})
        cell[r["status"]] = cell.get(r["status"], Decimal(0)) + Decimal(str(r["days"] or 0))
    return out


def model_presence(rows: Iterable[Mapping[str, Any]], *, now: datetime,
                   aliases: Mapping[str, str] | None = None) -> dict[str, Decimal]:
    """Сколько суток модель была в парке за период: сутки, где у неё есть
    хоть кусок велосипеде-дня, - целиком, идущие - до этой минуты. Спрос
    делится на них, а не на длину периода: модель, купленная месяц назад,
    в окне девяноста дней иначе выглядела бы втрое менее нужной."""
    seen: dict[str, set[date]] = {}
    for r in rows:
        seen.setdefault(_buy_title(r["model"], aliases), set()).add(r["day"])
    today = now.date()
    started = _span_days(now - datetime.combine(today, datetime.min.time(),
                                                tzinfo=now.tzinfo))
    return {model: sum((started if day == today else Decimal(1) for day in days),
                       Decimal(0))
            for model, days in seen.items()}


def zero_free_days(rows: Iterable[Mapping[str, Any]], *, before: date | None = None,
                   aliases: Mapping[str, str] | None = None
                   ) -> dict[str, dict[str | None, int]]:
    """Сутки, когда модель на точке ездила, а свободной не было: {модель:
    {точка: суток}}. `before` - первые сутки, которые ещё идут: неполный
    день не судим, к обеду «свободной не было и полдня» верно для любой
    модели. Два названия одной модели складываются до порога, а не после."""
    cells: dict[tuple[str, str | None, date], dict[str, Decimal]] = {}
    for r in rows:
        if before is not None and r["day"] >= before:
            continue
        key = (_buy_title(r["model"], aliases), r.get("location") or None, r["day"])
        cell = cells.setdefault(key, {})
        cell[r["status"]] = cell.get(r["status"], Decimal(0)) + Decimal(str(r["days"] or 0))
    out: dict[str, dict[str | None, int]] = {}
    for (model, point, _day), cell in cells.items():
        if (cell.get("rented", Decimal(0)) >= BUY_BUSY_MIN
                and cell.get("available", Decimal(0)) < BUY_FREE_MIN):
            by_point = out.setdefault(model, {})
            by_point[point] = by_point.get(point, 0) + 1
    return out


def last_purchase_prices(bikes: Iterable[Mapping[str, Any]],
                         purchases: Iterable[Mapping[str, Any]], *,
                         aliases: Mapping[str, str] | None = None) -> dict[str, dict]:
    """Цена модели в последней закупке ЗАК, где она была: {модель: {price,
    no, purchased_on}}. Следующая партия пойдёт по цене последней, а не по
    средней за три года. Модель без ЗАК - цена последнего заведённого
    велосипеда с ценой (no None: «по карточке»)."""
    docs = {p["id"]: p for p in purchases}
    batches: dict[tuple[str, Any], list[Mapping[str, Any]]] = {}
    for bike in bikes:
        if bike.get("purchase_price") is None:
            continue
        batches.setdefault((_buy_title(bike.get("model"), aliases), bike.get("purchase_id")),
                           []).append(bike)
    out: dict[str, dict] = {}
    for model in sorted({m for m, _ in batches}):
        own = [(docs[doc], rows) for (m, doc), rows in batches.items()
               if m == model and doc in docs]
        if own:
            doc, rows = max(own, key=lambda x: (x[0].get("purchased_on") or date.min,
                                                int(x[0]["id"])))
            price = sum((to_money(b["purchase_price"]) for b in rows), Decimal(0)) / len(rows)
            out[model] = {"price": to_money(price), "no": doc.get("no"),
                          "purchased_on": doc.get("purchased_on")}
            continue
        loose = [b for (m, _), rows in batches.items() if m == model for b in rows]
        bike = max(loose, key=lambda b: (b.get("purchased_on") or date.min,
                                         int(b.get("id") or 0)))
        out[model] = {"price": to_money(bike["purchase_price"]), "no": None,
                      "purchased_on": bike.get("purchased_on")}
    return out


def booking_pressure(bookings: Iterable[Mapping[str, Any]],
                     bikes: Iterable[Mapping[str, Any]], *,
                     aliases: Mapping[str, str] | None = None) -> dict[str, dict[str, int]]:
    """Открытые заявки по модели каталога: {название: {open, unmet}}.

    unmet - заявке сейчас нечего выдать: на её точке нет свободной модели
    (заявка без точки - нет нигде). Заявка велосипед не бронирует, поэтому
    это спрос, который парк прямо сейчас не закрывает.
    """
    free: dict[tuple[str, str | None], int] = {}
    for bike in bikes:
        if bike.get("status") == "available":
            key = (_buy_title(bike.get("model"), aliases), bike.get("location") or None)
            free[key] = free.get(key, 0) + 1
    out: dict[str, dict[str, int]] = {}
    for row in bookings:
        if (row.get("status") or "new") != "new" or not str(row.get("model") or "").strip():
            continue
        title = _buy_title(row.get("model"), aliases)
        point = row.get("location_name") or None
        have = (free.get((title, point), 0) if point
                else sum(n for (t, _), n in free.items() if t == title))
        cell = out.setdefault(title, {"open": 0, "unmet": 0})
        cell["open"] += 1
        cell["unmet"] += 0 if have else 1
    return out


def _usual_service_months(bikes: list[Mapping[str, Any]]) -> int:
    """Срок службы модели - самый частый у её велосипедов (24 по умолчанию,
    как в amortization_month)."""
    seen: dict[int, int] = {}
    for bike in bikes:
        months = int(bike.get("service_months") or 24)
        seen[months] = seen.get(months, 0) + 1
    return max(seen, key=lambda m: (seen[m], m)) if seen else 24


def buy_rows(payback: Iterable[Mapping[str, Any]], *,
             days: Mapping[str, Mapping[str, Any]],
             zero: Mapping[str, Mapping[str | None, int]],
             prices: Mapping[str, Mapping[str, Any]],
             demand: Mapping[str, Mapping[str, int]],
             bikes: Iterable[Mapping[str, Any]], presence: Mapping[str, Any],
             aliases: Mapping[str, str] | None = None) -> list[dict]:
    """Модели каталога с решением: брать, хватает, не брать, мало данных.

    payback - строки payback_rows за период (деньги модели по названию
    парка, здесь они складываются по каталогу), days - model_days, zero -
    zero_free_days, prices - last_purchase_prices, demand -
    booking_pressure, bikes - парк сейчас, presence - model_presence.

    Сколько брать: столько, чтобы при среднем спросе периода (дни в аренде
    на сутки, когда модель была в парке) простой вышел на цель, считая парк
    сейчас и на сборке, плюс заявки, которым нечего выдать. Спрос за
    периодом упирался в парк, поэтому это нижняя оценка. Порядок - от
    быстрее окупающейся.
    """
    target = Decimal(100 - IDLE_TARGET_PERCENT) / 100
    money: dict[str, dict[str, Decimal]] = {}
    for pay in payback:
        cell = money.setdefault(_buy_title(pay["model"], aliases),
                                {"paid": Decimal(0), "works": Decimal(0),
                                 "repair_cost": Decimal(0)})
        for key in cell:
            cell[key] += to_money(pay.get(key))
    fleet: dict[str, list[Mapping[str, Any]]] = {}
    for bike in bikes:
        fleet.setdefault(_buy_title(bike.get("model"), aliases), []).append(bike)
    out = []
    for model in sorted(set(money) | set(days) | set(demand)):
        own = fleet.get(model, [])
        cash = money.get(model) or {}
        wanted = demand.get(model) or {}
        now = sum(1 for b in own if b.get("status") in OPERATIONAL_STATUSES)
        assembly = sum(1 for b in own if b.get("status") == "new")
        paid = to_money(cash.get("paid"))
        metrics = fleet_metrics(days.get(model) or {}, paid)
        op = metrics["operational_days"]
        if not (now or assembly or op or wanted.get("open")):
            continue            # модель ушла из парка, и спроса на неё нет
        idle = metrics["idle_percent"]
        broken = sum((metrics["idle_breakdown"].get(s, Decimal(0))
                      for s in ("repair", "maintenance")), Decimal(0))
        repair = to_money(cash.get("repair_cost"))
        net = (paid + to_money(cash.get("works")) - repair) / op if op >= 1 else None
        price = (prices.get(model) or {}).get("price")
        months = _usual_service_months(
            [b for b in own if b.get("status") in OPERATIONAL_STATUSES] or own)
        payback_months = (
            (to_money(price) / (net * DAYS_IN_MONTH)).quantize(_TENTH, rounding=ROUND_HALF_UP)
            if price and net is not None and net > 0 else None)
        points = sorted(((p, n) for p, n in (zero.get(model) or {}).items() if n),
                        key=lambda x: (-x[1], x[0] or ""))
        row = {
            "model": model,
            # Как модель записана в парке, если иначе, чем в каталоге.
            "names": sorted({str(b.get("model") or "").strip() for b in own}
                            - {model, ""}),
            "fleet": now, "assembly": assembly,
            "operational_days": op, "rented_days": metrics["rented_days"],
            "utilization": percent_of(metrics["rented_days"], op),
            "idle_percent": idle, "repair_percent": percent_of(broken, op),
            "avg_check": metrics["avg_check"],
            "revenue_per_day": to_money(paid / op) if op >= 1 else None,
            "repair_per_day": to_money(repair / op) if op >= 1 else None,
            "net_per_day": to_money(net) if net is not None else None,
            "price": to_money(price) if price is not None else None,
            "price_no": (prices.get(model) or {}).get("no"),
            "service_months": months, "payback_months": payback_months,
            "zero_days": sum(n for _, n in points), "zero_points": points,
            "bookings": int(wanted.get("open") or 0),
            "unmet": int(wanted.get("unmet") or 0), "count": 0,
            "present_days": Decimal(str(presence.get(model) or 0)),
        }
        row["verdict"], row["reason"] = _buy_verdict(row, target=target)
        out.append(row)
    order = {"buy": 0, "hold": 1, "few": 2, "skip": 3}
    out.sort(key=lambda r: (order[r["verdict"]],
                            r["payback_months"] if r["payback_months"] is not None
                            else Decimal("Infinity"),
                            -(r["utilization"] or 0), r["model"]))
    return out


def _buy_verdict(row: dict[str, Any], *, target: Decimal) -> tuple[str, str]:
    """Решение по модели и его причина одной строкой; row["count"] -
    сколько брать. Простой - первым: это число, ради которого система."""
    op = row["operational_days"]
    if op < BUY_MIN_DAYS:
        if not row["fleet"] and not op:
            head = (f"{row['assembly']} на сборке, в прокате ещё нет" if row["assembly"]
                    else "в парке нет")
            return "few", head + (f"; заявок: {row['bookings']}" if row["bookings"] else "")
        return "few", f"мало данных: {int(op)} велосипеде-дней за период"
    idle = row["idle_percent"] or 0
    if idle >= IDLE_TARGET_PERCENT:
        tail = (f", из них ремонт и ТО {row['repair_percent']} %"
                if (row["repair_percent"] or 0) * 2 >= idle else "")
        return "skip", f"простой {idle} %{tail}"
    if row["net_per_day"] is not None and row["net_per_day"] <= 0:
        return "skip", "не окупается: ремонт съедает выручку"
    if row["payback_months"] is not None and row["payback_months"] > row["service_months"]:
        return "skip", (f"окупится за {row['payback_months']} мес. — дольше срока службы "
                        f"({row['service_months']} мес.)")
    # Спрос периода в велосипедах, которые держали бы простой на цели.
    # Сотые - чтобы хвост деления не превращался в лишний велосипед.
    span = row["present_days"]
    need = row["rented_days"] / target / span if span > 0 else Decimal(0)
    need = (need + row["unmet"] - row["fleet"] - row["assembly"]).quantize(CENT)
    row["count"] = math.ceil(need) if need > 0 else 0
    signs = [f"простой {idle} %"]
    if row["zero_days"]:
        signs.append(f"{row['zero_days']} сут. без свободной")
    if row["unmet"]:
        signs.append(f"заявок без велосипеда: {row['unmet']}")
    if row["price"] is None:
        signs.append("цена закупки неизвестна")
    if not row["count"]:
        coming = f" и {row['assembly']} на сборке" if row["assembly"] else ""
        return "hold", f"{signs[0]}, в парке {row['fleet']}{coming} — спрос закрыт"
    return "buy", ", ".join(signs)


def buy_plan(rows: Iterable[Mapping[str, Any]], *,
             budget: Decimal | None = None) -> dict[str, Any]:
    """Партия по решениям buy_rows: без бюджета - сколько просит спрос, с
    бюджетом - сколько на него влезает по цене последней ЗАК, начиная с
    быстрее окупающейся. Больше спроса бюджет не тратит: остаток денег
    лучше велосипеда, который будет стоять. lines - рекомендация словами."""
    rows = list(rows)
    items: list[dict[str, Any]] = []
    left = budget
    for row in rows:
        if row["verdict"] != "buy":
            continue
        count, price = row["count"], row["price"]
        priced = price is not None and price > 0
        if left is not None:
            count = min(count, int(left // price)) if priced else 0
        cost = to_money(price * count) if priced else None
        if left is not None and cost is not None:
            left = to_money(left - cost)
        items.append({"model": row["model"], "count": count, "wanted": row["count"],
                      "price": price if priced else None, "cost": cost})
    taken = [x for x in items if x["count"]]
    # Без цены бюджет в штуки не переводится: ни ЗАК, ни цены в карточке.
    unpriced = [x for x in items if budget is not None and x["price"] is None]
    short = [x for x in items if x["count"] < x["wanted"] and x["price"] is not None]
    skips = [r for r in rows if r["verdict"] == "skip"]
    lines = []
    if taken:
        head = f"На {money(budget)}: " if budget is not None else "Следующая партия: "
        lines.append(head + ", ".join(f"{x['model']} — {x['count']} шт." for x in taken)
                     + (f"; остаток {money(left)}." if budget is not None and left else ""))
    if short:
        lines.append("Не влезло в бюджет: " + ", ".join(
            f"{x['model']} — ещё {x['wanted'] - x['count']} шт. по {money(x['price'])}"
            for x in short) + ".")
    if unpriced:
        lines.append("Цена закупки неизвестна: " + ", ".join(
            f"{x['model']} — {x['wanted']} шт." for x in unpriced))
    if not items:
        lines.append("Докупать сейчас нечего: у моделей простой выше цели, спрос "
                     "закрыт парком или данных мало.")
    if skips:
        lines.append("Не брать: " + "; ".join(f"{r['model']} — {r['reason']}"
                                              for r in skips) + ".")
    spent = to_money(sum((x["cost"] or Decimal(0) for x in taken), Decimal(0)))
    return {"items": items, "taken": taken, "skips": skips, "lines": lines,
            "budget": budget, "left": left, "spent": spent,
            "count": sum(x["count"] for x in taken)}


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
# Где работает курьер. Для отчёта «кто наш клиент», а не для документов:
# у «Самоката» и «Яндекс Еды» разные графики и разные простои.
EMPLOYERS: dict[str, str] = {
    "yandex": "Яндекс Еда / Доставка",
    "samokat": "Самокат",
    "sbermarket": "Купер (СберМаркет)",
    "delivery": "Другая доставка",
    "other": "Не курьер / другое",
}

# Стаж курьера: новичок чаще ломает и чаще пропадает - это входит
# в решение о залоге и о сроке.
EXPERIENCE: dict[str, str] = {
    "none": "Впервые",
    "under_year": "До года",
    "years_1_3": "1–3 года",
    "over_3": "Больше 3 лет",
}


def check_employer(raw: Any) -> Check:
    """Работодатель из формы: пусто - не спросили, чужой код - ошибка."""
    text = str(raw or "").strip()
    if not text:
        return Check(True, None)
    return check_choice(text, EMPLOYERS, what="Компания")


def check_experience(raw: Any) -> Check:
    text = str(raw or "").strip()
    if not text:
        return Check(True, None)
    return check_choice(text, EXPERIENCE, what="Стаж")


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


def channel_totals(clients: Iterable[dict], *, since: date,
                   until: date) -> list[tuple[str, int]]:
    """Новые клиенты за период по каналам, больше - выше. Тот же ключ
    канала, что у отчёта «Каналы»: без канала - «не спросили» (пустой)."""
    totals: dict[str, int] = {}
    for client in clients:
        day = local_date(client.get("created_at"))
        if day is None or not since <= day <= until:
            continue
        channel = str(client.get("channel") or "")
        channel = channel if channel in CLIENT_CHANNELS else ""
        totals[channel] = totals.get(channel, 0) + 1
    return sorted(totals.items(), key=lambda x: (-x[1], x[0]))


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
        day = local_date(created)
        if day is None:
            continue
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
    "battery_rented_no_rental": "Батарея «у клиента», а аренды нет",
    "battery_rental_no_status": "Батарея числится за арендой, а статус не «у клиента»",
}
# Долг, ниже которого разбираться не с чем: копейки округления и
# недоплаты в пару рублей висят у половины базы.
DEBT_NOISE = Decimal(500)


def integrity_issues(bikes: Iterable[dict], rentals: Iterable[dict],
                     orders_by_bike: dict[int, dict],
                     debtors: Iterable[dict] = (),
                     batteries: Iterable[dict] = ()) -> list[dict]:
    """Расхождения между парком, арендами, нарядами и батареями.

    Расхождение - это не «некрасиво в базе», а невидимый простой: велосипед,
    числящийся в аренде без аренды, не попадает ни в выдачу, ни в ремонт,
    и никто про него не вспомнит, пока не придёт пересчёт. У батареи
    то же: «у клиента» без аренды - это батарея, которой нет ни на полке,
    ни в выдаче.
    """
    bikes = list(bikes)
    rentals = [r for r in rentals if r.get("status") == "active"]
    rented_bikes = {int(r["bike_id"]): r for r in rentals if r.get("bike_id")}
    active_ids = {int(r["id"]) for r in rentals}
    by_id = {int(b["id"]): b for b in bikes}
    issues: list[dict] = []

    def add(kind: str, *, bike: dict | None = None, rental: dict | None = None,
            what: str = "", battery: dict | None = None) -> None:
        issues.append({"kind": kind, "title": INTEGRITY_KINDS[kind], "bike": bike,
                       "rental": rental, "battery": battery, "what": what})

    for battery in batteries:
        rental_id = battery.get("rental_id")
        linked = rental_id is not None and int(rental_id) in active_ids
        if battery.get("status") == "rented" and not linked:
            add("battery_rented_no_rental", battery=battery,
                what="Ни на полке, ни в выдаче: пропадёт до пересчёта.")
        elif linked and battery.get("status") != "rented":
            add("battery_rental_no_status", battery=battery,
                rental=next((r for r in rentals if int(r["id"]) == int(rental_id)), None),
                what=f"Числится «{BATTERY_STATUSES.get(battery.get('status'), '—')}» "
                     "и может уйти второму клиенту.")

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
    "order": "Наряд ждёт запчасть", "min_stock": "Неснижаемый остаток",
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


# Сколько дней позиция может лежать без движения, прежде чем это станет
# заметно. Квартал: сезонную запчасть берут раз в сезон, а вот лежащая
# полгода - это деньги на полке.
STOCK_STALE_DAYS = 90


def part_rows(parts: Iterable[dict], stocks: dict[int, int],
              moved: Mapping[int, Any] | None = None,
              *, today: date | None = None,
              transit: Mapping[int, int] | None = None) -> list[dict]:
    """Остатки склада: позиция, сколько на полке и чего не хватает.

    Первыми - те, чей остаток ниже неснижаемого: это и есть список
    «что заказать», и он должен быть виден без прокрутки. За ними - «на
    пределе»: ровно неснижаемый, и следующий же расход уведёт ниже, -
    они тоже в заказе (по одной, `part_needs`): поставка идёт днями.

    `moved` - когда позицию последний раз трогали. Отсюда «дней на
    складе»: запчасть, которая лежит квартал, - это деньги на полке,
    и увидеть их можно только так. `transit` - сколько уже едет от
    поставщика: нехватка, которая уже в пути, - это не повод заказывать
    второй раз.
    """
    moved = moved or {}
    transit = transit or {}
    today = today or date.today()
    rows = []
    for part in parts:
        stock = int(stocks.get(int(part["id"]), 0))
        minimum = int(part.get("min_stock") or 0)
        last = moved.get(int(part["id"]))
        last_day = local_date(last)
        days = (today - last_day).days if last_day else None
        rows.append({**part, "stock": stock,
                     "short": max(minimum - stock, 0),
                     "below": stock < minimum,
                     # Без неснижаемого предела нет: «0 из 0» - это пустая
                     # полка (плитка «нет на полке»), а не сигнал заказать.
                     "at_min": minimum > 0 and stock == minimum,
                     "transit": int(transit.get(int(part["id"]), 0)),
                     "days_on_stock": days,
                     "stale": bool(days is not None and stock > 0
                                   and days >= STOCK_STALE_DAYS),
                     "cost_total": to_money(part.get("cost") or 0) * max(stock, 0),
                     "price_total": to_money(part.get("price") or 0) * max(stock, 0)})
    rows.sort(key=lambda r: (not r["below"], not r["at_min"], -r["short"],
                             str(r.get("title") or "")))
    return rows


def stock_summary(rows: Iterable[dict]) -> dict[str, Any]:
    rows = list(rows)
    return {
        "positions": len(rows),
        "below": sum(1 for r in rows if r["below"]),
        "at_min": sum(1 for r in rows if r.get("at_min")),
        "empty": sum(1 for r in rows if r["stock"] <= 0),
        "stale": sum(1 for r in rows if r.get("stale")),
        "cost": to_money(sum((r["cost_total"] for r in rows), Decimal(0))),
        # По клиентским ценам - что склад принесёт, если разойдётся весь;
        # рядом с себестоимостью это и есть наценка склада одним числом.
        "price": to_money(sum((r["price_total"] for r in rows), Decimal(0))),
    }


def part_needs(rows: Iterable[dict], waiting: Iterable[dict] = (),
               transit: Mapping[int, int] | None = None) -> list[dict]:
    """Что заказывать: нехватка до неснижаемого, позиции ровно на нём
    («на пределе», `edge`) и наряды, ждущие запчасть.

    Наряд в состоянии «ждёт запчасть» - это велосипед, который стоит
    и копит простой, поэтому его строки идут первыми, даже если на полке
    всё в норме.

    Потребности по одной позиции складываются: нехватка до неснижаемого
    считается от сегодняшнего остатка, а наряд заберёт ещё одну сверх.
    Иначе в заказ ушла бы только первая строка - уникальный индекс не даёт
    положить позицию в заказ дважды, и вторая потребность пропала бы молча.

    `transit` - сколько каждой позиции уже едет в отправленных заказах.
    Оно вычитается: остаток на полке ниже неснижаемого, пока поставка в
    пути, и без вычета «Собрать в заказ» заказал бы то же самое второй
    раз. Покрытая поставкой потребность остаётся в списке (`covered`) -
    видно, что её ждут, - но в заказ не идёт.
    """
    transit = transit or {}
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
             "edge": False,
             "work_order_id": order.get("work_order_id"),
             "work_order_no": order.get("work_order_no"),
             "bike_code": order.get("bike_code")})
    for row in rows:
        if not row.get("active", True) or not (row["below"] or row.get("at_min")):
            continue
        # Неснижаемый - точка заказа: ровно на нём следующий же расход уведёт
        # ниже, а поставка идёт днями. Ниже - нехватка до него; на пределе -
        # одна штука сверх, партию владелец поправит в заказе.
        add({"source": "min_stock", "part_id": int(row["id"]),
             "title": row.get("title") or "",
             "qty": row["short"] if row["below"] else 1, "edge": not row["below"],
             "work_order_id": None, "work_order_no": None, "bike_code": None})
    for need in needs:
        coming = int(transit.get(int(need["part_id"]), 0)) if need.get("part_id") else 0
        need["transit"] = coming
        need["qty"] = max(int(need["qty"]) - coming, 0)
        need["covered"] = coming > 0 and need["qty"] == 0
    needs.sort(key=lambda n: (n["covered"], n["source"] != "order", n["title"]))
    return needs


def parts_low(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Позиции на исходе: ниже неснижаемого или ровно на нём. Архивные -
    нет: их больше не заказывают."""
    return [dict(r) for r in rows
            if (r.get("below") or r.get("at_min")) and r.get("active", True)]


def parts_low_lines(rows: Sequence[Mapping[str, Any]], *, limit: int = 20) -> str:
    """Недельная сводка склада для служебного чата, HTML.

    «В пути» рядом с нехваткой обязательно: без него владелец каждую
    неделю видел бы одни и те же позиции и заказывал их повторно.
    """
    if not rows:
        return ""
    below = [r for r in rows if r.get("below")]
    edge = [r for r in rows if not r.get("below")]
    lines = [f"📦 Запчасти на исходе: {len(rows)}"
             + (f" (ниже неснижаемого {len(below)}, на пределе {len(edge)})"
                if below and edge else "")]
    for row in [*below, *edge][:limit]:
        title = html.escape(str(row.get("title") or "—"), quote=False)
        unit = html.escape(str(row.get("unit") or "шт"), quote=False)
        stock, minimum = int(row.get("stock") or 0), int(row.get("min_stock") or 0)
        state = (f"не хватает {minimum - stock}" if row.get("below")
                 else "ровно неснижаемый")
        coming = int(row.get("transit") or 0)
        lines.append(f"• {title} — {stock} из {minimum} {unit}, {state}"
                     + (f", в пути {coming}" if coming else ""))
    if len(rows) > limit:
        lines.append(f"…и ещё {len(rows) - limit}.")
    lines.append("Заказ собирается кнопкой: «Склад → Заказ запчастей».")
    return "\n".join(lines)


def order_total(items: Iterable[dict]) -> Decimal:
    return to_money(sum((to_money(i.get("price") or 0) * item_qty(i)
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
    start = local_date(started)
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
    """Строки для служебного чата. Пусто - искать некого, молчим.

    Сводка уходит с разметкой HTML: «<» или «&» в имени без экранирования
    и Telegram отвергает её целиком, как было с дневной сводкой оплат.
    """
    def who(row: dict) -> str:
        return (f"{html.escape(row.get('full_name') or '—', quote=False)} · "
                f"№ {html.escape(row.get('bike_code') or '—', quote=False)}")

    lines = []
    for row in rows["candidates"]:
        lines.append(f"• {who(row)} — просрочка {row['overdue_days']} дн., пора в розыск")
    for row in rows["searching"]:
        if row.get("theft"):
            lines.append(f"• {who(row)} — в розыске {row['search_days']} дн., "
                         "пора признавать потерю")
    return "\n".join(lines)


# ─────────────────── план месяца и прогноз освобождения ───────────────────

def month_plan(raw: dict[str, str] | None, *, fleet: int = 0,
               places: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """План на месяц из настроек. Не задан - считается от парка и целей.

    Умолчания берутся из трёх чисел, а не из воздуха: столько парк даёт,
    если держать простой в норме и чек на цели. План - это то, что можно
    подвинуть, а не то, что надо придумать с нуля.

    `places` - справочник точек. Общий план не задан (plan_rented пуст), а
    у каждой открытой точки свой - план сети это их сумма (source
    «points»): умолчание от парка спорило бы с тем, что владелец уже
    расписал по точкам. Заданный явно общий план главнее сумм.
    """
    raw = raw or {}

    def number(key: str, default: int) -> int:
        try:
            value = int(str(raw[key]))
        except (KeyError, ValueError, TypeError):
            return default
        return value if value >= 0 else default

    rented = number("plan_rented", -1)
    check = to_money(raw.get("plan_check") or CHECK_TARGET)
    if check <= 0:
        check = CHECK_TARGET
    points = plan_from_points(places, check=check) if places is not None else None
    if rented >= 0:
        source, per_day_plan = "settings", to_money(check * rented)
    elif points:
        source, rented, per_day_plan = "points", points["rented"], points["per_day"]
    else:
        rented = int(round(fleet * (100 - IDLE_TARGET_PERCENT) / 100))
        source, per_day_plan = "default", to_money(check * rented)
    # Нормы парка. Ремонт по умолчанию - половина допустимого простоя:
    # вторая половина уходит на «свободен» и «на ТО». Подменных - 2 % парка,
    # меньше двух штук держать бессмысленно.
    repair = number("plan_repair",
                    max(int(round(fleet * IDLE_TARGET_PERCENT / 200)), 1))
    spare = number("plan_spare", max(int(round(fleet * Decimal("0.02"))), 2))
    # Свободных - вторая половина допустимого простоя: столько стоит на
    # точке «на выдачу», больше - уже некому выдавать.
    free = number("plan_free", max(int(round(fleet * IDLE_TARGET_PERCENT / 200)), 1))
    return {"rented": rented,
            # Чек суммы точек - средний по их плановым деньгам: у точек
            # бывает свой чек, и «N велосипедов по X» обязано давать ту же
            # сумму, что и точки.
            "check": points["check"] if source == "points" else check,
            # Общий чек - тот, что в настройках: он же чек точек без своего,
            # и форма сводки правит его, а не средний по точкам.
            "base_check": check,
            "per_day": per_day_plan, "source": source, "points": points,
            "fleet": fleet, "repair": repair, "spare": spare, "free": free}


def point_plan(place: Mapping[str, Any] | None, *, check: Any) -> dict[str, Any] | None:
    """План месяца одной точки: велосипедов в аренде на ней и чек в день.

    Лежит в строке справочника (locations.plan_rented, plan_check), а не в
    настройках под именем точки: переименование - каскад по тексту имени,
    и ключ «план Павлюхиной» осиротел бы. Нет plan_rented или точка
    закрыта - плана нет. Чек не задан - общий чек плана: точку планируют
    числом велосипедов, а цена у сети одна.
    """
    if not place or place.get("active", True) is False:
        return None
    try:
        rented = int(str(place.get("plan_rented")))
    except (TypeError, ValueError):
        return None
    if rented < 0:
        return None
    own = to_money(place.get("plan_check") or 0)
    used = own if own > 0 else to_money(check)
    return {"rented": rented, "check": used, "own_check": own > 0,
            "per_day": to_money(used * rented)}


def plan_from_points(places: Iterable[Mapping[str, Any]] | None, *,
                     check: Any) -> dict[str, Any] | None:
    """Сумма планов открытых точек - только когда план есть у каждой.

    Одна открытая точка без плана - None: сумма без неё занизила бы план
    сети, и «план выполнен» было бы неправдой. Закрытые не в счёт.
    """
    plans = []
    for place in places or ():
        if not place.get("name") or place.get("active", True) is False:
            continue
        plan = point_plan(place, check=check)
        if plan is None:
            return None
        plans.append(plan)
    return sum_plans(plans, check=check) if plans else None


def sum_plans(plans: Iterable[Mapping[str, Any]], *, check: Any) -> dict[str, Any]:
    """Сумма планов точек: велосипеды и деньги в день складываются, чек -
    средний по деньгам (у точек он бывает свой). Без велосипедов - общий."""
    plans = list(plans)
    rented = sum(int(p["rented"]) for p in plans)
    money_per_day = to_money(sum((to_money(p["per_day"]) for p in plans), Decimal(0)))
    return {"rented": rented, "per_day": money_per_day, "count": len(plans),
            "check": (to_money(money_per_day / rented) if rented else to_money(check))}


# Плитки парка на сводке: статус, подпись и откуда берётся норма.
# Норма есть не у всех: «в аренде» - чем больше, тем лучше, и «сверх
# нормы» там было бы издевательством.
FLEET_TILES: tuple[tuple[str, str, str | None, str], ...] = (
    ("rented", "У клиента", "rented", "min"),
    ("repair", "В ремонте", "repair", "max"),
    # Свободных сверх нормы - это не запас, а простой: выдавать некому.
    ("available", "Свободны", "free", "max"),
    ("maintenance", "На ТО", None, ""),
    ("new", "На сборке", None, ""),
)


def fleet_tiles(counts: Mapping[str, int], plan: Mapping[str, Any] | None = None,
                *, spare: int = 0) -> list[dict]:
    """Плитки парка с нормой и пометкой «сверх нормы».

    Норма `min` - чем меньше факт, тем хуже (велосипедов в аренде);
    `max` - наоборот, чем больше, тем хуже (велосипедов в ремонте).
    Проценты считаются от операционного парка, а не от всего: `lost` и
    `sold` в знаменатель не попадают никогда.
    """
    plan = plan or {}
    base = sum(int(counts.get(code, 0)) for code in OPERATIONAL_STATUSES)
    out = []
    for code, title, plan_key, sense in FLEET_TILES:
        value = int(counts.get(code, 0))
        norm = int(plan.get(plan_key) or 0) if plan_key else 0
        over = bool(norm) and sense == "max" and value > norm
        under = bool(norm) and sense == "min" and value < norm
        out.append({
            "code": code, "title": title, "value": value, "norm": norm or None,
            "sense": sense,
            "percent": round(100 * value / base, 1) if base else None,
            "over": over, "under": under,
            "bad": over or under,
            "diff": value - norm if norm else 0,
        })
    if plan.get("spare"):
        norm = int(plan["spare"])
        out.append({"code": "spare", "title": "Подменные", "value": int(spare),
                    "norm": norm, "sense": "min",
                    "percent": round(100 * spare / base, 1) if base else None,
                    "over": False, "under": spare < norm, "bad": spare < norm,
                    "diff": int(spare) - norm})
    return out


def repair_chart(by_day: Mapping[date, Any], norm: int = 0) -> dict[str, Any]:
    """График «в ремонте по дням»: сколько дней в норме, сколько сверх.

    Дробные велосипеде-дни округляются к ближайшему целому: «4,6 в
    ремонте» на столбике не читается, а решение принимают по целым.
    """
    days = []
    for day in sorted(by_day):
        value = Decimal(str(by_day[day] or 0))
        count = int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        days.append({"day": day, "value": count, "over": bool(norm) and count > norm})
    top = max([d["value"] for d in days] + [norm, 1])
    for row in days:
        row["height"] = int(round(100 * row["value"] / top))
    return {"days": days, "norm": norm, "top": top,
            "ok_days": sum(1 for d in days if not d["over"]),
            "over_days": sum(1 for d in days if d["over"]),
            "peak": max((d["value"] for d in days), default=0)}


# Раньше этого дня отчётам нечего показать: проката не было. Граница
# нужна не данным, а арифметике: «0001-01-01 минус сутки» - OverflowError.
REPORT_FLOOR = date(2000, 1, 1)


def report_day(raw: Any, *, today: date, default: date | None = None) -> Check:
    """Граница периода отчёта из адреса: ГГГГ-ММ-ДД или ДД.ММ.ГГГГ.

    Пусто - `default` (без него - не ok). Раньше REPORT_FLOOR - мусор, как
    и нечитаемая дата. Будущее - сегодня: дней там нет, а «9999-12-31
    плюс сутки» роняли страницу 500 вместо отчёта.
    """
    if not str(raw or "").strip() and default is not None:
        return Check(True, default)
    check = check_date(raw)
    if not check.ok:
        return check
    if check.value < REPORT_FLOOR:
        return Check(False, error="Дата: слишком давно.")
    return Check(True, min(check.value, today))


def month_from(raw: Any, *, today: date | None = None) -> date:
    """Первое число месяца из «ГГГГ-ММ» в адресе. Мусор или будущее -
    текущий месяц: листать вперёд некуда, там ещё ничего не произошло."""
    today = today or date.today()
    current = today.replace(day=1)
    text = str(raw or "").strip()
    try:
        first = date(int(text[:4]), int(text[5:7]), 1) if len(text) == 7 else current
    except ValueError:
        return current
    # «0001-01» - тоже мусор: соседний месяц у него до нашей эры, и
    # month_bounds падал бы OverflowError, то есть страница - 500.
    if first < REPORT_FLOOR:
        return current
    return first if first <= current else current


def history_floor(start: date | datetime | None, *, today: date) -> date:
    """Первое число месяца, с которого у панели есть история (CrmDB.
    history_start): раньше него стрелка «прошлый месяц» не ведёт - там
    пустые месяцы, и листать их можно было до 2000 года. Ни одной записи -
    текущий месяц: назад листать нечего."""
    current = today.replace(day=1)
    if start is None:
        return current
    if isinstance(start, datetime):
        start = start.astimezone(MOSCOW).date()
    return min(max(start.replace(day=1), REPORT_FLOOR), current)


def month_bounds(first: date, *, today: date | None = None,
                 floor: date | None = None) -> dict[str, Any]:
    """Границы месяца для графиков: последний день, «сегодня» внутри
    месяца (для прошлого - его последний день), соседние месяцы.

    `floor` - первый месяц истории (history_floor): стрелки назад с него
    нет, а из месяца раньше него (старая ссылка) стрелка вперёд ведёт
    сразу в него, а не через пустые месяцы по одному."""
    today = today or date.today()
    next_first = (first + timedelta(days=32)).replace(day=1)
    last = next_first - timedelta(days=1)
    prev_first = (first - timedelta(days=1)).replace(day=1)
    current = today.replace(day=1)
    forward = min(max(next_first, floor), current) if floor else next_first
    return {"first": first, "last": last, "next": next_first,
            "prev": prev_first,
            "today": min(today, last),
            "days": (next_first - first).days,
            "passed": (min(today, last) - first).days + 1,
            "is_current": first == current,
            "prev_key": (prev_first.strftime("%Y-%m")
                         if floor is None or prev_first >= floor else None),
            "next_key": forward.strftime("%Y-%m") if first < current else None,
            "key": first.strftime("%Y-%m")}


def money_chart(rows: Iterable[Mapping[str, Any]], *, plan_per_day: Any = 0,
                today: date | None = None) -> dict[str, Any]:
    """График денег по дням месяца: пришло, накопленный долг, план в день.

    Долг показан накопительным: разовый провал ничего не значит, а линия,
    которая ползёт вверх весь месяц, - это и есть «копим долги».

    Дни после сегодняшнего остаются в ряду пустыми: месяц ещё идёт, и
    обрезать его значит делать вид, что он кончился.
    """
    today = today or date.today()
    per_day = to_money(plan_per_day)
    days, paid_sum, debt_sum = [], Decimal(0), Decimal(0)
    for row in rows:
        day = row["day"]
        future = day > today
        paid = to_money(row.get("paid"))
        charged = to_money(row.get("charged"))
        if not future:
            paid_sum += paid
            debt_sum += charged - paid
        days.append({"day": day, "paid": paid, "charged": charged,
                     "future": future,
                     # Долг копится нарастающим итогом и ниже нуля не
                     # опускается: переплата - это не отрицательный долг,
                     # а деньги вперёд, и рисовать её провалом нечестно.
                     "debt": max(debt_sum, Decimal(0)),
                     "over": bool(per_day) and paid >= per_day})
    past = [d for d in days if not d["future"]]
    top = max([d["paid"] for d in days] + [per_day, Decimal(1)])
    debt_top = max([d["debt"] for d in days] + [Decimal(1)])
    for row in days:
        row["height"] = int(round(100 * row["paid"] / top))
        row["debt_height"] = int(round(100 * row["debt"] / debt_top))
    best = max(past, key=lambda d: d["paid"], default=None)
    worked = [d for d in past if d["paid"] > 0]
    return {
        "days": days, "top": top, "debt_top": debt_top,
        "plan_per_day": per_day,
        "plan_height": int(round(100 * per_day / top)) if top else 0,
        "paid": to_money(paid_sum),
        "debt": max(debt_sum, Decimal(0)),
        "days_passed": len(past),
        "avg": to_money(paid_sum / len(past)) if past else Decimal(0),
        # Средний по рабочим дням: месяц, в котором половина дней пустая,
        # средним по всем дням выглядит вдвое хуже, чем он есть.
        "avg_worked": to_money(paid_sum / len(worked)) if worked else Decimal(0),
        "best": best,
        "over_days": sum(1 for d in past if d["over"]),
    }


def cumulative(days: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Тот же ряд накопительно: «сколько всего пришло к этому дню»."""
    out, total = [], Decimal(0)
    for row in days:
        if not row.get("future"):
            total += to_money(row.get("paid"))
        out.append({**row, "total": total})
    top = max([r["total"] for r in out] + [Decimal(1)])
    for row in out:
        row["height"] = int(round(100 * row["total"] / top))
    return out


def plan_progress(plan: dict[str, Any], metrics: dict[str, Any], *,
                  days_in_month: int, days_passed: int) -> dict[str, Any]:
    """Факт против плана: деньги за месяц и сколько ещё можно взять.

    План месяца - велосипеде-дни аренды на чек. Сравнивается с тем же
    средним чеком, что и в трёх числах, иначе цифры на соседних экранах
    разошлись бы.
    """
    days_in_month = max(int(days_in_month), 1)
    days_passed = min(max(int(days_passed), 0), days_in_month)
    # per_day - у суммы планов точек: у них свой чек, и средний чек на
    # число велосипедов дал бы копейки расхождения с точками.
    per_day_plan = plan.get("per_day")
    if per_day_plan is None:
        per_day_plan = plan["check"] * plan["rented"]
    target = to_money(per_day_plan * days_in_month)
    fact = to_money(metrics.get("revenue") or 0)
    # Сколько должно было прийти к сегодняшнему дню: план ровным темпом.
    pace = to_money(target * days_passed / days_in_month)
    left = max(target - fact, Decimal(0))
    days_left = days_in_month - days_passed
    # Прогноз - тем же темпом до конца месяца: «придёт столько, если
    # ничего не менять». Не обещание, а ответ на «успеваем или нет»,
    # поэтому в целых рублях: копейки оценки - ложная точность, и в
    # плитке «2 341 888,89 ₽» не влезало в строку.
    forecast = (to_money(fact * days_in_month / days_passed).quantize(
        Decimal(1), rounding=ROUND_HALF_UP) if days_passed > 0 else Decimal(0))
    return {
        "target": target, "fact": fact, "pace": pace, "left": left,
        "forecast": forecast,
        "forecast_ok": forecast >= target,
        "ahead": fact >= pace,
        "percent": (float(round(100 * fact / target, 1)) if target else None),
        "days_left": days_left,
        # Чтобы выйти на план, столько велосипедов должно кататься каждый
        # оставшийся день по целевому чеку.
        # Округление вверх - через math.ceil: у Decimal `//` усекает к нулю,
        # и трюк `-(-a // b)` превращается там в обычный floor. 6,67 велосипеда
        # он давал как 6, а шести на плановый чек уже не хватает.
        "need_rented": (int(math.ceil(left / (plan["check"] * days_left)))
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
        until = covered_until(rental["billed_until"], rental.get("balance", 0),
                              rental["price"], rental["period_days"])
        # Намерение - про тот срок, при котором его сказали (как в
        # intent_state): старое «продлит» с прошлого периода убирало бы
        # велосипед из прогноза навсегда.
        intent = rental.get("intent") if rental.get("intent_until") == until else None
        if intent == "renew":
            continue
        left = days_left(until, today=today)
        if left < 0:
            left = 0                 # просрочка: велосипед ждут уже сегодня
        if left <= horizon:
            out[str(left)].append({**rental, "free_on": today + timedelta(days=left),
                                   "returning": intent == "return"})
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


# ─────────────────── переброска между точками ───────────────────
#
# Точек несколько, и нужная модель часто стоит не там, где её спросят:
# на Адоратского три свободных Monster Truck, а на Павлюхина их завтра
# ждут. Подсказка на завтра и послезавтра: спрос - выдачи этой модели на
# точке в тот же день недели за восемь недель плюс открытые заявки,
# предложение - свободные сейчас и те, что вернутся по «оплачено до»
# (freeing_soon). Перевозит оператор, отмечая велосипеды сам: система
# считает, а не двигает парк.

TRANSFER_WEEKS = 8
TRANSFER_DAYS = 2
# Запас на точке по умолчанию: один свободный каждой модели сверх своего
# прогноза - на курьера, который пришёл без заявки.
TRANSFER_SAFETY = 1
TRANSFER_SAFETY_MAX = 50
# Сколько велосипедов показать к перевозке сверх предложенных: оператор
# выбирает сам, но весь парк точки в форме - это уже список, а не выбор.
TRANSFER_EXTRA_CHOICES = 3
# Велосипедов за одну перевозку: больше - это уже не газель, а чужая форма.
TRANSFER_MAX_BIKES = 50


def transfer_settings(settings: Mapping[str, Any] | None = None) -> dict[str, int]:
    """Запас на точке: сколько свободных каждой модели точка оставляет себе
    сверх своего прогноза. Ноль - честный ноль: «перевозить всё лишнее»."""
    raw = str((settings or {}).get("transfer_safety") or "").strip()
    value = parse_id(raw)
    if value is None or value > TRANSFER_SAFETY_MAX:
        value = TRANSFER_SAFETY
    return {"safety": value}


def weekday_demand(history: Iterable[Mapping[str, Any]], *, today: date,
                   weeks: int = TRANSFER_WEEKS,
                   aliases: Mapping[str, str] | None = None
                   ) -> dict[tuple[str, str, int], Decimal]:
    """Выдач в среднем за день недели: (точка, модель, weekday) -> число.

    Окно - `weeks` недель до сегодня, сам сегодняшний день не входит: он
    ещё не кончился. Делится на число недель, а не на дни с выдачами:
    вторник без единой выдачи - тоже вторник, и он тянет среднее вниз.
    """
    weeks = max(int(weeks), 1)
    start = today - timedelta(days=7 * weeks)
    counts: dict[tuple[str, str, int], int] = {}
    for row in history:
        on = row.get("started_on")
        if isinstance(on, datetime):
            on = on.date()
        place = str(row.get("location") or "").strip()
        model = catalogue_model(row.get("model"), aliases)
        if not isinstance(on, date) or not place or not model or not start <= on < today:
            continue
        key = (place, model, on.weekday())
        counts[key] = counts.get(key, 0) + int(row.get("issued") or 1)
    return {key: Decimal(n) / Decimal(weeks) for key, n in counts.items()}


def transfer_plan(*, points: Sequence[str], bikes: Iterable[Mapping[str, Any]],
                  rentals: Iterable[Mapping[str, Any]],
                  history: Iterable[Mapping[str, Any]],
                  bookings: Iterable[Mapping[str, Any]] = (), today: date,
                  safety: int = TRANSFER_SAFETY, weeks: int = TRANSFER_WEEKS,
                  days: int = TRANSFER_DAYS,
                  aliases: Mapping[str, str] | None = None,
                  idle: Mapping[int, int | None] | None = None) -> dict[str, Any]:
    """Прогноз по точкам и моделям на `days` дней вперёд и перевозки.

    По каждой точке и модели, к концу завтра и к концу послезавтра:
    спрос - среднее weekday_demand плюс открытые заявки на день (заявка на
    сегодня и просроченная - спрос завтрашнего дня: клиент ещё ждёт, а
    сегодняшний день прогноз не считает); будет - свободные сейчас плюс
    аренды, которые освободятся к этому дню (freeing_soon: «продлю» не
    возвращается, розыск - тоже). Спрос копится по дням и округляется до
    целых, а в предложении только свободные: ремонт, бронь и подменный
    фонд выдать нельзя.

    need - сколько не хватает в худший из дней; spare - сколько можно
    отдать: не больше свободных сейчас (везут только их) и так, чтобы в
    любой из дней у точки остался её прогноз плюс `safety`. Перевозки -
    жадно: самой большой нехватке - от самого большого излишка той же
    модели. `idle` - дни простоя по велосипеду: к перевозке предлагаются
    дольше всех стоящие, их и надо везти.
    """
    names = [str(p).strip() for p in points if str(p or "").strip()]
    order = {name: i for i, name in enumerate(names)}
    days = max(int(days), 1)
    safety = max(int(safety), 0)
    horizon = [today + timedelta(days=d) for d in range(1, days + 1)]
    idle = idle or {}
    cells: dict[tuple[str, str], dict[str, Any]] = {}

    def cell(place: str, model: str) -> dict[str, Any]:
        return cells.setdefault((place, model), {
            "location": place, "model": model, "free": 0, "bikes": [],
            # back[k] - освободится через k дней (0 - сегодня или просрочка)
            "back": [0] * (days + 1), "avg": [Decimal(0)] * days,
            "booked": [0] * days})

    for bike in bikes:
        place = str(bike.get("location") or "").strip()
        if bike.get("status") != "available" or bike.get("spare") or place not in order:
            continue
        model = catalogue_model(bike.get("model"), aliases)
        if not model:
            continue
        row = cell(place, model)
        row["free"] += 1
        row["bikes"].append({"id": int(bike["id"]), "code": bike.get("code"),
                             "idle_days": idle.get(int(bike["id"]))})
    for after, rows in freeing_soon(rentals, today=today, horizon=days).items():
        for rental in rows:
            # Аренда в розыске «освобождается сегодня» по просрочке, но
            # велосипед у пропавшего клиента завтра на точку не встанет.
            if rental.get("search_at"):
                continue
            place = str(rental.get("location") or "").strip()
            model = catalogue_model(rental.get("bike_model"), aliases)
            if place in order and model:
                cell(place, model)["back"][int(after)] += 1
    for (place, model, weekday), value in weekday_demand(
            history, today=today, weeks=weeks, aliases=aliases).items():
        if place not in order:
            continue
        for i, on in enumerate(horizon):
            if on.weekday() == weekday:
                cell(place, model)["avg"][i] += value
    for booking in bookings:
        if booking.get("status", "new") != "new":
            continue
        place = str(booking.get("location_name") or "").strip()
        model = catalogue_model(booking.get("model"), aliases)
        wanted = booking.get("wanted_on") or today
        i = max((wanted - today).days, 1) - 1
        if place in order and model and i < days:
            cell(place, model)["booked"][i] += 1

    out_rows: list[dict[str, Any]] = []
    for (place, model), row in cells.items():
        demand = Decimal(0)
        need, room = 0, row["free"]
        steps = []
        for i, on in enumerate(horizon):
            demand += row["avg"][i] + row["booked"][i]
            want = int(demand.quantize(Decimal(1), rounding=ROUND_HALF_UP))
            have = row["free"] + sum(row["back"][:i + 2])
            steps.append({"on": on, "demand": demand.quantize(Decimal("0.1"),
                                                              rounding=ROUND_HALF_UP),
                          "want": want, "have": have, "booked": row["booked"][i]})
            need = max(need, want - have)
            room = min(room, have - want - safety)
        if not (row["free"] or any(row["back"]) or demand):
            continue
        # Дольше всех стоящие - первыми: перевезти их и есть снижение
        # простоя; у кого журнала нет - в конец, по номеру.
        row["bikes"].sort(key=lambda b: (-(b["idle_days"] or 0), str(b["code"] or "")))
        out_rows.append({"location": place, "model": model, "free": row["free"],
                         "back": sum(row["back"]), "steps": steps,
                         "need": max(need, 0), "spare": max(room, 0),
                         "bikes": row["bikes"], "covered": 0})
    out_rows.sort(key=lambda r: (order[r["location"]], r["model"]))

    left = {(r["location"], r["model"]): r["spare"] for r in out_rows}
    taken = {(r["location"], r["model"]): 0 for r in out_rows}
    by_key = {(r["location"], r["model"]): r for r in out_rows}
    moves: list[dict[str, Any]] = []
    for target in sorted((r for r in out_rows if r["need"]),
                         key=lambda r: (-r["need"], order[r["location"]], r["model"])):
        want = target["need"]
        sources = sorted((r for r in out_rows if r["model"] == target["model"]
                          and r["location"] != target["location"]
                          and left[(r["location"], r["model"])] > 0),
                         key=lambda r: (-left[(r["location"], r["model"])],
                                        order[r["location"]]))
        for source in sources:
            if want <= 0:
                break
            key = (source["location"], source["model"])
            count = min(want, left[key])
            left[key] -= count
            want -= count
            # Предложенные - следующие по простою, ещё не отданные другой
            # перевозке; к ним пара запасных на выбор оператора.
            pool = by_key[key]["bikes"][taken[key]:]
            taken[key] += count
            moves.append({"source": source["location"], "target": target["location"],
                          "model": target["model"], "count": count,
                          "bikes": [{**b, "picked": i < count} for i, b in
                                    enumerate(pool[:count + TRANSFER_EXTRA_CHOICES])]})
        target["covered"] = target["need"] - want

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for move in moves:
        group = groups.setdefault((move["source"], move["target"]), {
            "source": move["source"], "target": move["target"], "lines": [], "count": 0})
        group["lines"].append(move)
        group["count"] += move["count"]
    for group in groups.values():
        group["label"] = (f"с {group['source']} на {group['target']}: " + ", ".join(
            f"{m['count']} × {m['model']}" for m in group["lines"]))
    return {"rows": out_rows, "moves": list(groups.values()), "days": horizon,
            "safety": safety, "weeks": weeks,
            "short": sum(r["need"] - r["covered"] for r in out_rows)}


# ─────────────────── закупки основных средств ───────────────────

def purchase_no(number: int) -> str:
    return f"ЗАК-{int(number):06d}"


def months_between(since: Any, until: date) -> int:
    """Полных месяцев между датами. Меньше месяца - ноль."""
    if since is None:
        return 0
    start = local_date(since)
    months = (until.year - start.year) * 12 + until.month - start.month
    if until.day < start.day:
        months -= 1
    return max(months, 0)


def add_months(day: date, months: int) -> date:
    """Та же дата через N месяцев; 31-е в коротком месяце - его последний
    день. Пара к months_between: срок службы «15 месяцев» от 31 января
    кончается 30 апреля следующего года, а не падает на несуществующем дне."""
    total = day.year * 12 + day.month - 1 + int(months)
    year, month = divmod(total, 12)
    month += 1
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return date(year, month, min(day.day, (nxt - timedelta(days=1)).day))


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
    "new": "Новая на сборке",
    "available": "Свободна", "rented": "У клиента", "repair": "В ремонте",
    "maintenance": "На ТО", "lost": "Утеряна", "written_off": "Списана",
    # Проданная - как проданный велосипед: остаётся в базе со своей
    # историей, в операционный парк и в пересчёт не входит.
    "sold": "Продана",
}
# Статусы, которые ставит оператор. rented - только через выдачу, как
# и у велосипеда: батарея уходит вместе с ним. new снимает только ввод
# в эксплуатацию: недособранную батарею выдавать нечего.
BATTERY_MANUAL_STATUSES = ("available", "repair", "maintenance", "lost",
                           "written_off", "sold")
# На сборке батарея в оборот не входит и в счёт парка не идёт: писать
# недособранную технику в наличие значит обещать клиенту то, чего нет.
BATTERY_OPERATIONAL = ("available", "rented", "repair", "maintenance")
# Циклов, после которых батарею пора смотреть: ёмкость к этому моменту
# заметно просела, и клиент начинает жаловаться на «не доезжает». Это
# общий ресурс по умолчанию: владелец правит его настройкой
# `battery_max_cycles`, а модели ставит свой (`battery_models.max_cycles`).
BATTERY_CYCLES_WARN = 500
BATTERY_CYCLES_MAX = 100000
# Горизонты плана замены, месяцев: «в этом месяце» - деньги сейчас,
# квартал и полгода - деньги, которые надо отложить.
BATTERY_PLAN_HORIZONS = (1, 3, 6)
# Темп циклов по батарее, которой меньше месяца, - не темп: первая неделя
# после ввода в оборот дала бы «кончится через месяц» или «через век».
BATTERY_PACE_MIN_DAYS = 30


def check_battery_status(raw: Any) -> Check:
    return check_choice(raw, BATTERY_STATUSES, what="Статус батареи")


def check_location(raw: Any, names: Iterable[str] | None = None) -> Check:
    """Точка выдачи. Список берётся из справочника; пусто - «не на точке»."""
    value = str(raw or "").strip()
    if not value:
        return Check(True, None)
    # Без списка - тот же запасной, что у форм: константа читается только
    # в point_choices, иначе у проверки и выпадающего списка было бы два
    # разных источника.
    allowed = set(names) if names is not None else set(point_choices([]))
    if value not in allowed:
        return Check(False, error="Точка: недопустимое значение.")
    return Check(True, value)


def point_choices(places: Iterable[Mapping[str, Any]], *current: Any) -> list[str]:
    """Точки для выпадающего списка и проверки формы - одни на всю панель.

    Действующие точки справочника в его порядке (sort, name), плюс текущие
    значения карточки, которых среди действующих нет: закрытая точка
    остаётся в карточке, а список без неё молча стёр бы её при первом же
    сохранении. Справочник пуст - константа LOCATIONS: пустая база не
    должна ломать формы.
    """
    rows = [p for p in places if p.get("name")]
    names = ([p["name"] for p in rows if p.get("active", True) is not False]
             if rows else list(LOCATIONS))
    for value in current:
        value = str(value or "").strip()
        if value and value not in names:
            names.append(value)
    return names


def bike_on_rent(bike: Mapping[str, Any]) -> bool:
    """Велосипед у клиента. Его точку, как и статус «в аренде», ставят
    выдача, возврат и замена, а не карточка: в аренде он стоит на точке
    аренды, иначе чек точки не сошёлся бы с её днями."""
    return bike.get("status") == "rented" or bool(bike.get("rental_id"))


def issue_point(chosen: Any, *, booking: Mapping[str, Any] | None = None,
                bike: Mapping[str, Any] | None = None) -> str | None:
    """Точка выдачи: выбранная оператором, иначе точка заявки - клиент сам
    назвал, куда придёт, - иначе точка велосипеда. None - не известна."""
    for value in (chosen, (booking or {}).get("location_name"),
                  (bike or {}).get("location")):
        value = str(value or "").strip()
        if value:
            return value
    return None


def battery_max_cycles(settings: Mapping[str, Any] | None = None) -> int:
    """Общий ресурс АКБ в циклах (`battery_max_cycles`): для моделей без
    своего. Мусор в настройке - к умолчанию, а не к нулю: ноль объявил бы
    весь парк батарей отслужившим."""
    try:
        value = int(str((settings or {}).get("battery_max_cycles")))
    except (TypeError, ValueError):
        return BATTERY_CYCLES_WARN
    return value if 1 <= value <= BATTERY_CYCLES_MAX else BATTERY_CYCLES_WARN


def battery_cycle_limit(battery: Mapping[str, Any],
                        default: int = BATTERY_CYCLES_WARN) -> int:
    """Ресурс этой батареи: модели (`model_max_cycles`), иначе общий."""
    own = battery.get("model_max_cycles")
    return int(own) if own else int(default)


def battery_rows(batteries: Iterable[dict], *, today: date | None = None,
                 since: Mapping[int, datetime] | None = None,
                 now: datetime | None = None,
                 max_cycles: int = BATTERY_CYCLES_WARN) -> list[dict]:
    """Список батарей с износом, признаком «пора смотреть» и днями.

    `since` - когда батарея вошла в текущий статус (по журналу): отсюда
    «в ремонте 12 дней». Дни у клиента - от начала аренды, за которой
    батарея числится: у клиента она с выдачи, а не с последней замены.
    `max_cycles` - общий ресурс; у модели может быть свой.
    """
    today = today or date.today()
    now = now or datetime.now(UTC)
    since = since or {}
    rows = []
    for battery in batteries:
        months = int(battery.get("service_months") or 15)
        passed = months_between(battery.get("purchased_on"), today)
        wear = (float(round(min(100 * passed / max(months, 1), 100), 1))
                if battery.get("purchased_on") else None)
        cycles = int(battery.get("cycles") or 0)
        limit = battery_cycle_limit(battery, max_cycles)
        started = battery.get("rental_started")
        if isinstance(started, datetime):
            started = started.date()
        rows.append({**battery, "wear": wear, "cycles": cycles,
                     "cycle_limit": limit, "cycles_out": cycles >= limit,
                     # Розыск - состояние аренды, а не батареи: пока
                     # клиент не нашёлся, батарея числится у него.
                     "in_search": bool(battery.get("search_at")),
                     "tired": cycles >= limit
                     or (wear is not None and wear >= 100),
                     "rental_days": (max((today - started).days, 0)
                                     if started and battery.get("client_id") else None),
                     # Без записи в журнале (импорт до триггера) - ноль,
                     # а не «None» в колонке.
                     "status_days": (idle_days(since.get(int(battery["id"])), now=now)
                                     if battery.get("id") is not None else None) or 0})
    # В розыске - первыми: их ищут, а не листают.
    rows.sort(key=lambda b: (not b["in_search"], not b["tired"],
                             str(b.get("code") or "")))
    return rows


def battery_summary(rows: Iterable[dict]) -> dict[str, int]:
    rows = list(rows)
    counts = {code: sum(1 for r in rows if r.get("status") == code)
              for code in BATTERY_STATUSES}
    counts["total"] = len(rows)
    counts["tired"] = sum(1 for r in rows if r["tired"])
    counts["search"] = sum(1 for r in rows if r.get("in_search"))
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


# Что считает план замены: батареи в обороте. На сборке ещё не служит,
# утеря, списание и продажа из парка уже вышли - менять там нечего.
BATTERY_PLAN_STATUSES = BATTERY_OPERATIONAL
BATTERY_WEAR_REASONS = {"age": "срок службы", "cycles": "ресурс циклов"}


def battery_wear(battery: Mapping[str, Any], *, today: date,
                 max_cycles: int = BATTERY_CYCLES_WARN) -> dict:
    """Когда батарею менять: по сроку службы или по циклам - что раньше.

    Срок - от даты покупки; без неё от заведения карточки, самой ранней
    известной даты (`dated` = False): батарея не моложе этого, и прогноз
    тогда оптимистичен - экран это помечает. Срок службы берётся с карточки
    батареи, как у амортизации и у «износа» в списке: одно число на одну
    батарею, иначе список и план спорили бы друг с другом.

    Циклы - по темпу этой батареи: сколько набрала за прожитые дни, столько
    наберёт и дальше. Моложе месяца - темпа нет, только срок. Без даты
    покупки темпа нет тоже: начало счётчика неизвестно, а карточку старой
    батареи заводят сразу с её циклами - 300 циклов «за месяц с заведения»
    отправили бы её в замену за три недели. Такая меряется ресурсом только
    когда он уже выработан.
    """
    months = max(int(battery.get("service_months") or 15), 1)
    start = local_date(battery.get("purchased_on"))
    dated = start is not None
    if start is None:
        start = (local_date(battery.get("created_at"))
                 or local_date(battery.get("commissioned_at")))
    limit = battery_cycle_limit(battery, max_cycles)
    cycles = int(battery.get("cycles") or 0)
    by_age = add_months(start, months) if start else None
    by_cycles = None
    if cycles >= limit:
        by_cycles = today
    elif cycles > 0 and dated and start is not None:
        lived = (today - start).days
        if lived >= BATTERY_PACE_MIN_DAYS:
            # Целочисленно: сколько дней уйдёт на оставшиеся циклы при
            # нынешнем темпе, с округлением вверх. Потолок в век - чтобы
            # батарея с одним циклом за год не уводила дату за 9999 год.
            left = -(-(limit - cycles) * lived // cycles)
            by_cycles = today + timedelta(days=min(left, 36500))
    options = [(d, why) for d, why in ((by_age, "age"), (by_cycles, "cycles"))
               if d is not None]
    replace_on, reason = min(options) if options else (None, None)
    return {"service_months": months, "age_months": (months_between(start, today)
                                                     if start else None),
            "dated": dated, "cycles": cycles, "cycle_limit": limit,
            "by_age": by_age, "by_cycles": by_cycles,
            "replace_on": replace_on, "reason": reason,
            "left_days": (replace_on - today).days if replace_on else None}


def battery_wear_plan(batteries: Iterable[Mapping[str, Any]], *, today: date,
                      max_cycles: int = BATTERY_CYCLES_WARN,
                      horizons: Sequence[int] = BATTERY_PLAN_HORIZONS) -> dict[str, Any]:
    """План замены АКБ: что менять в ближайшие 1/3/6 месяцев и почём.

    Горизонты накопительные: «за три месяца» включает первый, - так
    читается бюджет «сколько отложить к кварталу». Просроченные (срок уже
    вышел) входят в каждый горизонт: их менять первыми.

    Бюджет - по сегодняшней цене модели из каталога, а не по цене покупки:
    новую батарею купят по нынешней цене. Нет цены у модели - берётся
    цена покупки этой батареи; нет и её - батарея считается «без цены», и
    бюджет честно помечен неполным. Амортизацию план не трогает.
    """
    rows = []
    for battery in batteries:
        if battery.get("status") not in BATTERY_PLAN_STATUSES:
            continue
        wear = battery_wear(battery, today=today, max_cycles=max_cycles)
        model_price = to_money(battery.get("model_price") or 0)
        price = model_price if model_price > 0 else (
            to_money(battery["purchase_price"])
            if battery.get("purchase_price") is not None else None)
        rows.append({**battery, **wear, "price": price,
                     "overdue": bool(wear["replace_on"] and wear["replace_on"] <= today)})
    ends = {h: add_months(today, h) for h in horizons}
    far = ends[max(horizons)] if horizons else today
    due = [r for r in rows if r["replace_on"] and r["replace_on"] <= far]
    due.sort(key=lambda r: (r["replace_on"], str(r.get("code") or "")))
    models: dict[Any, dict[str, Any]] = {}
    for row in due:
        key = row.get("model_id")
        model = models.setdefault(key, {
            "model_id": key, "title": row.get("model_title") or "без модели",
            "price": to_money(row["model_price"]) if row.get("model_price") else None,
            "cells": {h: {"count": 0, "budget": Decimal(0), "unpriced": 0}
                      for h in horizons}})
        for h in horizons:
            if row["replace_on"] <= ends[h]:
                cell = model["cells"][h]
                cell["count"] += 1
                if row["price"] is None:
                    cell["unpriced"] += 1
                else:
                    cell["budget"] += row["price"]
    totals = []
    for h in horizons:
        cells = [m["cells"][h] for m in models.values()]
        totals.append({"months": h, "until": ends[h],
                       "count": sum(c["count"] for c in cells),
                       "budget": to_money(sum((c["budget"] for c in cells), Decimal(0))),
                       "unpriced": sum(c["unpriced"] for c in cells)})
    return {"rows": due, "horizons": totals,
            "models": sorted(models.values(), key=lambda m: m["title"]),
            "overdue": sum(1 for r in rows if r["overdue"]),
            "undated": sum(1 for r in rows if not r["dated"]),
            "unknown": sum(1 for r in rows if r["replace_on"] is None),
            "total": len(rows), "max_cycles": max_cycles}


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
    # Оплаченный велосипед, который стоит: клиент уехал, бросил работу
    # или собирается сдавать - и об этом лучше узнать до конца периода.
    "idle_rented": "Не двигается при аренде",
}
# Срочные - те, где велосипед прямо сейчас уезжает не туда. Остальные
# разбирают в свой черёд: у жёлтой тревоги нет минут, есть часы.
ALERT_URGENT_KINDS = ("moving", "alarm")

# Команды устройству. Блокировка у StarLine срабатывает после остановки
# велосипеда: мотор перестаёт тянуть, когда тот уже стоит, а не на ходу.
# Поэтому команда из панели безопасна, а задержка до ближайшего круга
# опроса ничего не меняет.
TRACKER_COMMANDS: dict[str, str] = {"block": "Заблокировать мотор",
                                    "unblock": "Снять блокировку"}
# Тревоги, при которых кнопка блокировки уместна прямо в списке.
BLOCKABLE_ALERTS = ("moving", "alarm")
# Команда в очереди дольше двух кругов опроса - опрос, похоже, не работает.
COMMAND_STALE_MINUTES = 10


def check_command(raw: Any) -> Check:
    return check_choice(raw, TRACKER_COMMANDS, what="Команда")


def command_rows(commands: Iterable[Mapping[str, Any]], *,
                 now: datetime | None = None) -> list[dict]:
    """Команды с состоянием для человека: в очереди, выполнена, отказ."""
    now = now or datetime.now(UTC)
    rows = []
    for c in commands:
        sent = c.get("sent_at")
        if sent is None:
            waited = (now - c["requested_at"]).total_seconds() / 60 \
                if c.get("requested_at") else 0
            state, title = "pending", "в очереди"
            stale = waited >= COMMAND_STALE_MINUTES
        elif c.get("ok"):
            state, title, stale = "done", "выполнена", False
        else:
            state, title, stale = "failed", "StarLine отказал", False
        rows.append({**c, "state": state, "state_title": title, "stale": stale,
                     "title": TRACKER_COMMANDS.get(str(c.get("command")),
                                                   str(c.get("command")))})
    return rows


def block_state(tracker: Mapping[str, Any],
                pending: Mapping[str, Any] | None) -> dict[str, Any]:
    """Что показать про мотор: заблокирован ли и какая команда ждёт.

    Следующая команда - обратная текущему состоянию, пока в очереди
    ничего нет: вторую туда не поставить (уникальный индекс), и кнопка
    в это время не нужна.
    """
    blocked = bool(tracker.get("blocked"))
    return {"blocked": blocked, "pending": pending,
            "next": None if pending else ("unblock" if blocked else "block"),
            "title": "мотор заблокирован" if blocked else "мотор не заблокирован"}
ALERT_LEVELS: dict[str, str] = {"urgent": "Срочно", "yellow": "Жёлтый"}
# Состояние открытой тревоги. Закрытая - та, у которой есть handled_at.
ALERT_STATES: dict[str, str] = {
    "new": "Новая",
    "working": "В работе",
    "snoozed": "Отложена",
    # «Это норма» - не закрытие: пока причина держится, тревога висит и
    # второй раз не поднимается (частичный уникальный индекс), а исчезнет
    # причина - опрос закроет её сам. Так норма сама себя убирает.
    "normal": "Это норма",
}
# На сколько откладывают тревогу по умолчанию: до конца смены.
ALERT_SNOOZE_HOURS = 4


def alert_level(kind: str) -> str:
    return "urgent" if kind in ALERT_URGENT_KINDS else "yellow"
# Сколько часов молчания считать пропажей связи. Полсуток - потому что
# велосипед ночует в подъезде, где связи нет, и час молчания ничего
# не значит.
TRACKER_OFFLINE_HOURS = 12
# Скорость, с которой «стоит» превращается в «едет». 5 км/ч - это уже
# не дрейф GPS у стены дома.
TRACKER_MOVING_SPEED = Decimal(5)
# Питание трекера: ниже этого он скоро замолчит совсем. 11,5 В - для
# трекера на 12-вольтовом питании. M13 на электровелосипеде питается от
# тяговой батареи (36-60 В), и там порог свой - или 0, «не следить».
TRACKER_LOW_VOLTS = Decimal("11.5")
# Сколько суток оплаченный велосипед может стоять, прежде чем это станет
# вопросом. Трое суток - это уже не выходные: курьер либо бросил работу,
# либо собрался сдавать, и узнать об этом лучше до конца периода.
TRACKER_IDLE_DAYS = 3
# Статусы велосипеда, при которых ехать он не должен.
TRACKER_PARKED_STATUSES = ("available", "reserved", "repair", "maintenance")
EARTH_KM = 6371.0088


def tracker_settings(settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Пороги тревог: из настроек, иначе значения по умолчанию.

    Питание - единственный порог, у которого ноль что-то значит: «не
    следить». У трекера на тяговой батарее напряжение - это заряд батареи
    курьера, и тревога на каждый разряд была бы шумом.
    """
    settings = settings or {}

    def number(key: str, default: Decimal | int, *, zero: bool = False) -> Decimal:
        raw = str(settings.get(key) or "").strip().replace(",", ".")
        try:
            value = Decimal(raw)
        except (InvalidOperation, ValueError):
            return Decimal(default)
        if not value.is_finite() or value < 0 or (value == 0 and not zero):
            return Decimal(default)
        return value

    return {"offline_hours": int(number("tracker_offline_hours",
                                        TRACKER_OFFLINE_HOURS)),
            "moving_speed": number("tracker_moving_speed", TRACKER_MOVING_SPEED),
            "low_volts": number("tracker_low_volts", TRACKER_LOW_VOLTS, zero=True),
            "idle_days": int(number("tracker_idle_days", TRACKER_IDLE_DAYS))}


# Пороги тревог из формы: ключ настройки, подпись, пределы, целое ли.
TRACKER_LIMITS: tuple[tuple[str, str, Decimal, Decimal, bool], ...] = (
    ("tracker_offline_hours", "Молчит дольше, ч", Decimal(1), Decimal(168), True),
    ("tracker_low_volts", "Питание ниже, В", Decimal(0), Decimal(100), False),
    ("tracker_moving_speed", "Едет быстрее, км/ч", Decimal(1), Decimal(60), False),
    ("tracker_idle_days", "Стоит при аренде дольше, сут.", Decimal(1), Decimal(30), True),
)


def check_tracker_limits(form: Mapping[str, Any]) -> tuple[dict[str, str], str | None]:
    """Пороги тревог из формы панели: значения для настроек или ошибка.

    Ошибка по первому неверному полю - и не сохраняется ничего: половина
    порогов новых, половина старых - это то, чего никто не вводил.
    """
    out: dict[str, str] = {}
    for key, label, low, high, whole in TRACKER_LIMITS:
        raw = str(form.get(key) or "").strip().replace(",", ".")
        try:
            value = Decimal(raw)
        except (InvalidOperation, ValueError):
            value = None
        if (value is None or not value.is_finite() or not low <= value <= high
                or (whole and value != value.to_integral_value())):
            kind = "целое число" if whole else "число"
            return {}, f"{label}: {kind} от {low} до {high}."
        # format «f», а не str(normalize()): иначе 60 записалось бы как «6E+1».
        out[key] = str(int(value)) if whole else format(value.normalize(), "f")
    return out, None


def tracker_seen_at(device: Mapping[str, Any]) -> datetime | None:
    """Когда трекер последний раз был на связи: позже из точки и связи.

    Точка и связь - разные события: в подвале трекер выходит на связь,
    а спутников не видит. По одной точке такой трекер через полсуток
    считался бы «молчит», хотя отвечает каждые пять минут.
    """
    seen = [m for m in (device.get("recorded_at"), device.get("active_at")) if m]
    return max(seen) if seen else None


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


def has_fix(lat: Any, lon: Any) -> bool:
    """Есть ли у точки координаты. «0, 0» - не Гвинейский залив, а трекер
    без спутников: такие записи остались от опросов до исправления."""
    if lat is None or lon is None:
        return False
    return not (abs(float(lat)) < 1e-6 and abs(float(lon)) < 1e-6)


def map_url(lat: float | None, lon: float | None) -> str | None:
    """Ссылка на карту с точкой. Яндекс: им пользуются на точках."""
    if not has_fix(lat, lon):
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
        moved = tracker.get("moved_at")
        still = None
        if moved is not None:
            still = max((now - moved).total_seconds() / 86400, 0)
        rows.append({**tracker,
                     "silent_hours": round(silent, 1) if silent is not None else None,
                     "offline": offline,
                     "moving": speed >= limits["moving_speed"],
                     "still_days": round(still, 1) if still is not None else None,
                     # Стоит при аренде: велосипед у клиента, на связи,
                     # но не двигался дольше порога. Молчащий трекер сюда
                     # не считается - про него уже есть своя тревога.
                     "idle_rented": bool(
                         tracker.get("rental_id") and not offline
                         and still is not None and still >= limits["idle_days"]),
                     "low_power": (tracker.get("voltage") is not None
                                   and limits["low_volts"] > 0
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
    if row.get("idle_rented"):
        days = row.get("still_days")
        out.append({"kind": "idle_rented",
                    "note": (f"стоит {days:.0f} сут., аренда идёт"
                             if days is not None else "не двигается, аренда идёт")})
    for alert in out:
        alert["tracker_id"] = row.get("id")
        alert["bike_id"] = row.get("bike_id")
        alert["level"] = alert_level(alert["kind"])
        alert["lat"], alert["lon"] = row.get("lat"), row.get("lon")
    return out


def alert_rows(alerts: Iterable[dict], *,
               now: datetime | None = None) -> list[dict]:
    """Тревоги с состоянием, понятным человеку.

    `needs` - требует внимания прямо сейчас: новая, взятая в работу или
    отложенная, у которой срок вышел. Признанная нормой и отложенная
    «на потом» из этого списка выпадают - ради этого их и отмечали.
    """
    now = now or datetime.now(UTC)
    rows = []
    for alert in alerts:
        state = str(alert.get("state") or "new")
        open_ = alert.get("handled_at") is None
        until = alert.get("snooze_until")
        due = state != "snoozed" or until is None or until <= now
        rows.append({
            **alert,
            "state": state,
            "level": str(alert.get("level") or alert_level(str(alert.get("kind") or ""))),
            "open": open_,
            "title": TRACKER_ALERTS.get(str(alert.get("kind") or ""),
                                        str(alert.get("kind") or "")),
            "snooze_due": due,
            "needs": open_ and state in ("new", "working", "snoozed") and due,
        })
    rows.sort(key=lambda a: (not a["needs"], a["level"] != "urgent",
                             -(a.get("id") or 0)))
    return rows


def tracker_state_title(row: Mapping[str, Any]) -> str:
    """Состояние трекера одним словом - то же, что цветной тег на карте."""
    if row.get("alarm"):
        return "тревога"
    if row.get("moving"):
        return "едет"
    if row.get("offline"):
        return "молчит"
    return "стоит"


def alert_summary(rows: Iterable[dict], *, now: datetime | None = None) -> dict[str, int]:
    rows = list(rows)
    open_rows = [r for r in rows if r["open"]]
    now = now or datetime.now(UTC)
    since = now - timedelta(days=1)

    def fresh(r: dict) -> bool:
        at = r.get("created_at")
        return isinstance(at, datetime) and at >= since

    return {
        "open": len(open_rows),
        # За сутки - и закрытые тоже: «сколько за ночь настреляло» отвечает
        # на другой вопрос, чем «сколько сейчас висит».
        "day": sum(1 for r in rows if fresh(r)),
        "needs": sum(1 for r in open_rows if r["needs"]),
        "urgent": sum(1 for r in open_rows if r["needs"] and r["level"] == "urgent"),
        "working": sum(1 for r in open_rows if r["state"] == "working"),
        "snoozed": sum(1 for r in open_rows if r["state"] == "snoozed"),
        "normal": sum(1 for r in open_rows if r["state"] == "normal"),
    }


def snooze_until(hours: Any = None, *, now: datetime | None = None) -> datetime:
    """До какого времени откладываем. Пусто - до конца смены."""
    now = now or datetime.now(UTC)
    text = str("" if hours is None else hours).strip()
    try:
        # Ноль часов - это не «отложить», а опечатка: берём минимум.
        value = int(text) if text else ALERT_SNOOZE_HOURS
    except ValueError:
        value = ALERT_SNOOZE_HOURS
    return now + timedelta(hours=min(max(value, 1), 72))


def map_points(rows: Iterable[dict]) -> list[dict]:
    """Точки для карты: только те, у кого есть координаты."""
    points = []
    for row in rows:
        if not has_fix(row.get("lat"), row.get("lon")):
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
# Один адрес без {s}: поддомены a/b/c OSM больше не советует.
MAP_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
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


# Готовые периоды трека: столько спрашивают на практике. Произвольный
# интервал тоже есть, но в девяти случаях из десяти нужен «сегодня».
TRACK_RANGES: dict[str, str] = {
    "today": "Сегодня", "yesterday": "Вчера", "week": "7 суток",
}


def track_period(kind: str, *, today: date | None = None,
                 since: date | None = None,
                 until: date | None = None) -> tuple[date, date]:
    """Период трека: (с, по включительно). Непонятный вид - сегодня."""
    today = today or date.today()
    if kind == "yesterday":
        day = today - timedelta(days=1)
        return day, day
    if kind == "week":
        return today - timedelta(days=6), today
    if kind == "custom" and since and until and since <= until:
        # Месяц - столько живёт журнал позиций; просить больше нечего.
        return max(since, today - timedelta(days=31)), min(until, today)
    return today, today


def track_line(positions: Iterable[Mapping[str, Any]]) -> list[list[float]]:
    """Точки трека для линии на карте: [[широта, долгота], …].

    Скачки GPS выкидываем той же меркой, что и в пробеге: линия через
    полгорода и обратно - это не поездка, а перескок спутника.
    """
    points = sorted(positions, key=lambda p: p["recorded_at"])
    line: list[list[float]] = []
    for point in points:
        lat, lon = point.get("lat"), point.get("lon")
        # «0, 0» в начале периода обрезала бы весь настоящий трек как скачок.
        if not has_fix(lat, lon):
            continue
        if line:
            step = distance_km(line[-1][0], line[-1][1], lat, lon)
            if step is not None and step >= 5:
                continue
        line.append([float(lat), float(lon)])
    return line


def track_distance(positions: Iterable[Mapping[str, Any]]) -> float:
    """Сколько накатано по журналу позиций, км.

    Это не одометр: точки приходят раз в несколько минут, и срезанные
    углы теряются. Для вопроса «он вообще ездит?» этого достаточно, а
    накат за аренду по-прежнему считается по пробегу с дисплея.
    """
    # Точка без спутников выкидывается до подсчёта: иначе оба отрезка к
    # ней и от неё - «скачки», и с ними пропадал настоящий участок.
    points = sorted((p for p in positions if has_fix(p.get("lat"), p.get("lon"))),
                    key=lambda p: p["recorded_at"])
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
    urgent = sum(1 for a in alerts
                 if (a.get("level") or alert_level(str(a.get("kind") or "")))
                 == "urgent")
    head = f"🛰 Трекеры: {len(alerts)}"
    lines = [head + (f", срочных {urgent}" if urgent else "")]
    # Срочные первыми: в чате читают первые три строки.
    order = sorted(alerts, key=lambda a: (a.get("level")
                                          or alert_level(str(a.get("kind") or "")))
                   != "urgent")
    for alert in order[:limit]:
        where = ""
        if alert.get("lat") is not None:
            where = f" — {map_url(alert['lat'], alert['lon'])}"
        code = alert.get("bike_code") or alert.get("alias") or alert.get("device_id")
        mark = "🔴 " if (alert.get("level")
                        or alert_level(str(alert.get("kind") or ""))) == "urgent" else ""
        lines.append(f"• {mark}{TRACKER_ALERTS.get(alert['kind'], alert['kind'])}: "
                     f"{code}{', ' + alert['note'] if alert.get('note') else ''}{where}")
    if len(order) > limit:
        lines.append(f"…и ещё {len(order) - limit}")
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
                moves: Iterable[Mapping[str, Any]],
                other: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Состояние смены для карточки: ожидаемое, посчитанное, расхождение.

    `other` - платежи смены не наличными: переводы, СБП, карта. В ящик
    они не попадают и в ожидаемое не входят, но выручка смены - это
    и они тоже: «сколько приняли за смену» и «сколько в ящике» - два
    разных вопроса.
    """
    payments, moves, other = list(payments), list(moves), list(other)
    expected = shift_expected(shift, payments, moves)
    counted = (to_money(shift["counted"]) if shift.get("counted") is not None
               else None)
    diff = to_money(counted - expected) if counted is not None else None
    cash = to_money(sum(to_money(p.get("amount") or 0) for p in payments))
    by_method: dict[str, Decimal] = {}
    for p in other:
        code = str(p.get("method") or "other")
        by_method[code] = to_money(by_method.get(code, Decimal(0))
                                   + to_money(p.get("amount") or 0))
    other_total = to_money(sum(by_method.values(), Decimal(0)))
    return {"expected": expected, "counted": counted, "diff": diff,
            "cash": cash,
            "other": other_total, "by_method": by_method,
            "other_count": len(other),
            "revenue": to_money(cash + other_total),
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
    "conflict": "договор есть, но рядом чужой номер, телефон или ФИО — сверьте",
    "contract_other": "номер договора не нашего вида — сверьте",
    "phone": "телефон в назначении",
    "name": "ФИО плательщика",
}
# Номер договора, который выдаёт сама система: АВ-2026-000042 (у бота MAX
# свой префикс). Только такой номер отличим от номера велосипеда, даты и
# слов в назначении; всё, что набрано в карточке руками, - подсказка.
OUR_CONTRACT = re.compile(r"([^\W\d_]{1,6})-(\d{4})-(\d{6})")
# Разделители внутри нашего номера: банк и клиент пишут как хотят -
# «АВ-2026-000042», «АВ 2026 000042», «№АВ2026-000042». Буквы класс не
# съедает: на нём держится различие «АВ» у Telegram и «АВМ» у MAX.
CONTRACT_SEP = r"[\s\-–—/№#]*"
# Продолжение после номера: «…000042/2», «…000042-2», «…000042А»,
# «…000042/Д1» - это уже другой договор (второй, допсоглашение), набранный
# кому-то руками, а не наш номер с точкой после.
CONTRACT_TAIL = r"(?![^\W_]|[/\-–—][^\W_])"
# Любой номер нашего вида в назначении, чей бы он ни был. Буквы - вся
# склеенная цепочка («ДОГОВОРУАВ»), префикс сверяется её концом. Год -
# только 20xx: иначе «тел 9990000009» тоже был бы «номером». Хвост
# запоминается, чтобы «…000042/2» не считался тем же номером.
OUR_NUMBER_TEXT = re.compile(
    rf"(?<!\d)([^\W\d_]+){CONTRACT_SEP}(20\d\d){CONTRACT_SEP}(\d{{6}})(?!\d)"
    r"((?:[/\-–—]?[^\W_]+)?)")

Span = tuple[int, int]
OurNumber = tuple[str, str, str]           # префикс, год, номер


def bank_settings(settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or {}
    return {"auto_credit": str(settings.get("bank_auto_credit") or "") == "1"}


def digits(raw: Any) -> str:
    return re.sub(r"\D", "", str(raw or ""))


# Клиент, разобранный под выписку один раз: ключ, карточка, договор, наш
# номер (если он нашего вида), последние 10 цифр телефона, ФИО для сравнения.
PreparedClient = tuple[int, Mapping[str, Any], str, "OurNumber | None", str, str]


def prepare_clients(clients: Iterable[Mapping[str, Any]]) -> list[PreparedClient]:
    """Разобрать карточки один раз на всю выписку, а не на каждую строку:
    при тысячах клиентов повторный разбор договора, телефона и ФИО на
    каждое поступление держал страницу «Касса → Выписка» секундами."""
    out: list[PreparedClient] = []
    for client in clients:
        key = id(client) if client.get("id") is None else int(client["id"])
        contract = str(client.get("contract_no") or "").strip()
        out.append((key, client, contract, our_number(contract) if contract else None,
                    digits(client.get("phone"))[-10:],
                    normalize_name(client.get("full_name"))))
    return out


def match_payment(txn: Mapping[str, Any],
                  clients: Iterable[Mapping[str, Any]], *,
                  prepared: list[PreparedClient] | None = None) -> dict[str, Any] | None:
    """Кому из клиентов принадлежит поступление.

    Признаки по убыванию надёжности: номер договора нашего вида
    (АВ-2026-000042) в назначении, телефон там же, номер договора, набранный
    в карточке руками, ФИО плательщика. Зачислять без человека можно только
    по первому: «15», «N15» или «01.09.2026» в назначении - это и номер
    велосипеда, и дата, и договор соседа.

    И по первому - только когда ему ничто не возражает: ни чужой номер
    поверх нашего («АВ-2026-000042 доп» у соседа), ни второй номер нашего
    вида, ни телефон или ФИО другого клиента. Спор - это подсказка
    «сверьте», а не зачисление: чужие деньги на чужом балансе дороже
    одного нажатия оператора.

    Каждый признак ищется по всем клиентам, а не до первого попавшегося:
    список идёт по алфавиту, и «первый» - случайность. Признак, который
    указывает на двух разных клиентов, не угадывает никого.
    """
    if txn.get("direction") != "credit":
        return None
    purpose = str(txn.get("purpose") or "")
    upper = purpose.upper()
    phones = {digits(p) for p in re.findall(r"[\d\-()+ ]{10,}", purpose)}
    payer = normalize_name(txn.get("payer_name"))
    ours: dict[int, tuple[Mapping[str, Any], OurNumber, list[Span]]] = {}
    other: dict[int, tuple[Mapping[str, Any], list[Span]]] = {}
    by_phone: dict[int, Mapping[str, Any]] = {}
    by_name: dict[int, Mapping[str, Any]] = {}
    tails = {p[-10:] for p in phones if len(p) >= 10}
    for key, client, contract, number, phone, name in (
            prepared if prepared is not None else prepare_clients(clients)):
        if contract:
            # Номер нашего вида ищется только строго: «АВ-2026-000042» в
            # «…000042/2» - это чужой договор, и подсказкой «сверьте» на
            # владельца короткого номера он тоже быть не должен.
            if number:
                if spans := our_contract_in(contract, upper, number=number):
                    ours[key] = (client, number, spans)
            elif spans := other_contract_in(contract, upper):
                other[key] = (client, spans)
        if phone and phone in tails:
            by_phone[key] = client
        if payer and name == payer:
            by_name[key] = client
    if len(ours) == 1:
        key, (client, number, spans) = next(iter(ours.items()))
        return contract_verdict(key, client, number, spans, upper, other,
                                by_phone, by_name)
    for found, reason in ((by_phone, "phone"),
                          ({k: c for k, (c, _) in other.items()}, "contract_other"),
                          (by_name, "name")):
        if len(found) == 1:
            return {"client": next(iter(found.values())), "reason": reason}
    return None


def contract_verdict(key: int, client: Mapping[str, Any], number: OurNumber,
                     spans: list[Span], upper: str,
                     other: Mapping[int, tuple[Mapping[str, Any], list[Span]]],
                     by_phone: Mapping[int, Any],
                     by_name: Mapping[int, Any]) -> dict[str, Any] | None:
    """Наш номер договора нашёлся у одного клиента: уверенно или спор.

    Номер соседа, набранный руками поверх нашего («АВ-2026-000042 доп»),
    значит, что в назначении стоит более точный номер: подсказкой идёт
    тот, чьё совпадение длиннее. Номера соседей в других местах
    назначения («велосипед № 15») не мешают - это шум, а не спор.
    """
    rivals = [(k, c, span) for k, (c, found) in other.items() if k != key
              for span in found if any(overlaps(span, s) for s in spans)]
    if rivals:
        mine = [(s, e) for s, e in spans
                if any(overlaps((s, e), span) for _, _, span in rivals)]
        longest = max(e - s for s, e in mine + [span for _, _, span in rivals])
        owners = {k: c for k, c, (s, e) in rivals if e - s == longest}
        if any(e - s == longest for s, e in mine):
            owners[key] = client
        if len(owners) != 1:
            return None                  # одинаковой длины у двоих - решает человек
        winner, owner = next(iter(owners.items()))
        return {"client": owner,
                "reason": "conflict" if winner == key else "contract_other"}
    # Второй номер нашего вида в том же назначении («по договорам
    # АВ-2026-000042 и АВМ-2026-000007»): за кого из двоих эти деньги,
    # решает человек, даже если второго номера ни у кого в карточке нет.
    foreign = any(not same_number(found, number) for found in our_numbers_in(upper))
    # Телефон или ФИО указывают на других клиентов, а не на владельца
    # договора: курьер мог заплатить за себя, перепутав номер.
    clash = any(found and key not in found for found in (by_phone, by_name))
    return {"client": client, "reason": "conflict" if foreign or clash else "contract"}


def overlaps(a: Span, b: Span) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def our_number(contract: str) -> OurNumber | None:
    """Префикс, год и номер из договора нашего вида, иначе None."""
    m = OUR_CONTRACT.fullmatch(contract.upper().replace(" ", ""))
    return None if m is None else (m[1], m[2], m[3])


def our_numbers_in(upper: str) -> list[tuple[str, str, str, str]]:
    """Все номера нашего вида в назначении: буквы, год, номер, хвост."""
    return [(m[1], m[2], m[3], m[4]) for m in OUR_NUMBER_TEXT.finditer(upper)]


def same_number(found: tuple[str, str, str, str], number: OurNumber) -> bool:
    """Номер из назначения - этот договор. Буквы сверяются концом:
    «ДОГОВОРУАВ-…» - это «АВ-…», а «АВМ-…» - нет."""
    letters, year, seq, tail = found
    prefix, own_year, own_seq = number
    return (year, seq, tail) == (own_year, own_seq, "") and letters.endswith(prefix)


def our_contract_in(contract: str, upper: str, *,
                    number: OurNumber | None = None) -> list[Span]:
    """Где в назначении стоит номер договора нашего вида (пусто - нигде).

    Разделители банк и клиент пишут как хотят: «АВ-2026-000042»,
    «АВ 2026 000042», «АВ2026-000042». Граница спереди - только цифра:
    «договоруАВ-…» банк склеивает сам, а «АВ» внутри «АВМ-…» у бота MAX
    не находится и так - разделитель не съедает букву «М». Поэтому префикс
    одного бота не должен быть концом префикса другого («АВ» и «ТАВ»).
    Сзади номер не продолжается: «…0000421», «…000042/2», «…000042А» -
    другие номера.
    """
    number = number or our_number(contract)
    if number is None:
        return []
    prefix, year, seq = number
    if seq not in upper:
        return []                        # дешёвый предфильтр: клиентов тысячи
    pattern = (rf"(?<!\d){re.escape(prefix)}{CONTRACT_SEP}{year}"
               rf"{CONTRACT_SEP}{seq}{CONTRACT_TAIL}")
    return [m.span() for m in re.finditer(pattern, upper)]


def other_contract_in(contract: str, upper: str) -> list[Span]:
    """Где в назначении стоит номер, набранный в карточке руками, целым словом.

    Только подсказка: границы - любые буква или цифра рядом, иначе «15»
    находилось бы в «АВ-2026-000150». Номер без единой цифры - «—», «б/н»,
    «нет», «без номера» - это заглушка «номера нет»: она совпадает со
    «счётом б/н» и тире в любом назначении и не подсказывает никого.
    """
    key = "".join(contract.upper().split())
    if not any(ch.isdigit() for ch in key) or key not in upper.replace(" ", ""):
        return []
    pattern = r"\s*".join(re.escape(ch) for ch in key)
    return [m.span() for m in re.finditer(rf"(?<![^\W_]){pattern}(?![^\W_])", upper)]


def normalize_name(raw: Any) -> str:
    """ФИО к сравнимому виду: «Иванов И. И.» и «ИВАНОВ ИВАН ИВАНОВИЧ»
    так и останутся разными, а регистр и лишние пробелы - нет."""
    return " ".join(str(raw or "").upper().replace("Ё", "Е").split())


def bank_rows(txns: Iterable[dict], clients: Iterable[dict] | None = None,
              *, settings: Mapping[str, Any] | None = None,
              credits: Iterable[Mapping[str, Any]] = ()) -> list[dict]:
    """Выписка с догадкой, кому зачислить. Неразобранные - первыми.

    `credits` - платежи, уже зачисленные заявкой «Я оплатил» или счётом
    (`CrmDB.credits_since`): совпадение с ними снимает «уверенно» -
    строку разбирает человек, причина в подсказке.
    """
    clients = list(clients or [])
    credits = list(credits)
    prepared = prepare_clients(clients)
    rows = []
    for txn in txns:
        guess = (match_payment(txn, clients, prepared=prepared)
                 if txn.get("status") == "new" and clients else None)
        reason = MATCH_REASONS.get((guess or {}).get("reason", ""), "")
        twice = (bank_credited_before(txn, guess["client"].get("id"), credits)
                 if guess and credits else None)
        if twice is not None:
            reason = f"{reason}; {twice['note']}"
        rows.append({**txn, "guess": guess, "guess_reason": reason, "twice": twice,
                     "sure": bool(guess) and guess["reason"] == MATCH_SURE
                     and twice is None})
    rows.sort(key=lambda t: t.get("booked_at"), reverse=True)
    rows.sort(key=lambda t: t.get("status") != "new")
    return rows


# Сколько суток вокруг даты операции в выписке искать тот же платёж, уже
# зачисленный другим путём: «Я оплатил» подтверждают и до выписки, и
# после, а перевод доходит до неё за день-два, в выходные дольше.
BANK_TWICE_DAYS = 3


def bank_credited_before(txn: Mapping[str, Any], client_id: Any,
                         credits: Iterable[Mapping[str, Any]], *,
                         days: int = BANK_TWICE_DAYS) -> dict[str, Any] | None:
    """Те же деньги уже в журнале? Платёж той же суммы тому же клиенту,
    зачисленный заявкой «Я оплатил» или счётом, в пределах `days` суток от
    операции. None - такого нет.

    Номер договора в назначении стоит и у перевода, по которому клиент
    нажал «Я оплатил»: автозачисление положило бы те же деньги второй раз.
    Совпадение - не приговор (клиент мог заплатить дважды), поэтому
    строка не зачисляется сама, а уходит человеку с причиной.
    """
    if client_id is None or txn.get("direction") != "credit":
        return None
    amount = to_money(txn.get("amount"))
    booked = txn.get("booked_at")
    for row in credits:
        if row.get("client_id") is None or int(row["client_id"]) != int(client_id):
            continue
        pending = row.get("source") == "pending"
        # Ждущая заявка - при любой сумме: клиент мог её не назвать или
        # ошибиться в ней, а зачисленная выписка плюс подтверждённая потом
        # заявка - те же деньги дважды.
        if not pending and to_money(row.get("amount")) != amount:
            continue
        paid = row.get("paid_at")
        if isinstance(booked, datetime) and isinstance(paid, datetime) \
                and abs(paid - booked) > timedelta(days=days):
            continue
        when = f" {paid:%d.%m}" if isinstance(paid, datetime) else ""
        if pending:
            return {**row, "note": f"клиент нажал «Я оплатил» (заявка {row.get('ref')}"
                                   f"{when}) — зачислите одно из двух: заявку или "
                                   "эту строку"}
        what = {"order": f"счёт {row.get('ref')}",
                "payment": f"платёж {row.get('ref')} в журнале"}.get(
                    row.get("source"), f"заявка «Я оплатил» {row.get('ref')}")
        return {**row, "note": f"{money(amount)} уже зачислено ({what}{when}) — "
                               "сверьте, не те же ли это деньги"}
    return None


def claim_bank_twice(claim: Mapping[str, Any], amount: Any,
                     bank: Iterable[Mapping[str, Any]]) -> str | None:
    """Обратная сторона `bank_credited_before`: заявку «Я оплатил» хотят
    подтвердить, а перевод той же суммы этому клиенту уже зачислен из
    выписки (автозачислением или человеком) рядом по дате. None - нет.

    Выписка с номером договора зачисляется сама и до того, как клиент
    нажмёт кнопку, - и подтверждённая потом заявка положила бы те же
    деньги второй раз."""
    want = to_money(amount)
    for row in bank:
        if to_money(row.get("amount")) == want:
            booked = row.get("booked_at")
            when = f" {booked:%d.%m}" if isinstance(booked, datetime) else ""
            return (f"{money(want)} этому клиенту уже зачислено из выписки банка{when}. "
                    "Если это тот же перевод — отклоните заявку; если клиент заплатил "
                    "второй раз — подтвердите с отметкой «это другой платёж».")
    return None


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
    "queued": "В очереди", "sending": "Отправляется", "sent": "Доставлено",
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
            last_day = local_date(last)
            if last_day is None:
                continue
            days = (today - last_day).days
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
# Новый код по ссылке - не чаще раза в минуту и не больше пяти в час:
# каждый код - ещё пять догадок, и «код + пять попыток» по кругу иначе
# перебирали бы шесть цифр без предела.
SIGN_CODE_GAP_SECONDS = 60
SIGN_CODES_PER_HOUR = 5
# Неверных кодов на заявку всего, сколько бы кодов ни выдали: дальше
# ссылка кодов не даёт и не принимает, код выдаёт только оператор из
# панели - он же снимает замок.
SIGN_MAX_WRONG = 20
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
    locked = int(request.get("wrong_total") or 0) >= SIGN_MAX_WRONG
    # Сколько секунд ждать следующего кода по ссылке.
    code_wait = 0
    if code_at is not None:
        code_wait = max(math.ceil(SIGN_CODE_GAP_SECONDS
                                  - (now - code_at).total_seconds()), 0)
    signed = request.get("status") == "signed"
    cancelled = request.get("status") == "cancelled"
    expired = expires is not None and now >= expires
    return {
        "signed": signed, "cancelled": cancelled, "expired": expired,
        "open": not (signed or cancelled or expired),
        "locked": locked,
        "code_valid": code_left is not None and code_left > 0
        and attempts < SIGN_MAX_ATTEMPTS and not locked,
        "code_left": round(code_left) if code_left is not None else None,
        "code_wait": code_wait,
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
# Закрытый у нас счёт ещё неделю спрашивают у банка: ссылки, выданные
# до того, как срок стал уходить в банк, живут у Точки 7 суток. Раз в
# полчаса - этого хватает, чтобы деньги не потерялись, и банк не
# заваливается запросами про мёртвые счета.
PAY_RECHECK_DAYS = 8
PAY_RECHECK_MINUTES = 30
# Счёт, закрытый руками (наличные, перевод), спрашивают у банка, пока жива
# его ссылка, и час сверху - на опрос, который увидит оплату последней
# минуты: оплата по ней после кассы - деньги дважды.
PAY_TWICE_HOURS = PAY_LINK_HOURS + 1
PAY_TWICE_NOTE = ("банк: оплачен ещё и по ссылке — деньги пришли дважды, "
                  "второй раз не зачислено; верните клиенту или зачтите руками")
# Оплачен второй счёт за ремонт, который уже оплачен (другим счётом или на
# месте): `mark_pay_paid` закрывает счёт с этой отметкой и возвращает
# REPAIR_PAID_TWICE вместо 0, а опрос говорит команде о двойных деньгах.
REPAIR_PAID_TWICE = -1
REPAIR_TWICE_NOTE = ("банк: ремонт уже был оплачен — деньги пришли дважды; "
                     "верните клиенту или зачтите руками")
# Банк не ответил на списание с карты (таймаут, 5xx, мусор вместо ответа):
# списал он или нет - неизвестно. Счёт остаётся открытым и держит клиента
# вне автосписания, пока человек не сверит операцию в Точке.
AUTOCHARGE_UNKNOWN_NOTE = ("банк не ответил на списание: прошло ли оно, неизвестно. "
                           "Сверьте операцию в Точке и снимите счёт или примите оплату")
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


def acquiring_enabled(settings: Mapping[str, Any] | None = None) -> bool:
    """Выключатель эквайринга в панели. Не задан - включён: токен в
    окружении и есть согласие им пользоваться."""
    return str((settings or {}).get("acquiring_enabled", "1")) not in ("0", "false")


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
    """Ссылка протухла: банк её уже не примет, опрашивать нечего.

    У автосписания ссылки нет: его операция живёт у банка своим сроком, и
    закрытие «по часам» освобождало клиента для нового списания, пока
    первое ещё могло пройти, - с карты уходило бы вдвое. Такой счёт
    закрывает ответ банка или человек.
    """
    if order.get("status") not in PAY_OPEN or order.get("kind") == "auto":
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
                   cards: Mapping[int, Any] | None = None,
                   busy: Iterable[int] = ()) -> list[dict]:
    """Кому сегодня можно списать с карты.

    Списываем только то, что уже начислено и не оплачено: автосписание
    закрывает долг, а не берёт вперёд «на всякий случай». Без карты и
    без долга аренда сюда не попадает.

    busy - клиенты с открытым счётом: вчерашнее списание, которое банк
    ещё не подтвердил, или ссылка, которую клиент вот-вот оплатит. Долг
    в журнале у них прежний, и новое списание взяло бы ту же сумму второй
    раз - с карты уходило бы вдвое.
    """
    today = today or date.today()
    cards = cards or {}
    busy = {int(x) for x in busy}
    due = []
    for rental in rentals:
        if rental.get("status") != "active":
            continue
        client_id = rental.get("client_id")
        if client_id is None or not cards.get(int(client_id)):
            continue
        if int(client_id) in busy:
            continue
        debt = to_money(rental.get("balance"))
        if debt >= 0:
            continue
        due.append({"rental": rental, "client_id": int(client_id),
                    "amount": to_money(-debt)})
    due.sort(key=lambda r: -r["amount"])
    return due


def autocharge_busy(orders: Iterable[Mapping[str, Any]],
                    claims: Iterable[Mapping[str, Any]] = ()) -> set[int]:
    """Кого автосписание сегодня не трогает: деньги у них уже в пути.

    Открытый счёт с операцией банка (ссылка, неподтверждённое списание),
    списание, на которое банк не ответил (счёт «new» без операции: списал
    он или нет, неизвестно), и «Я оплатил», которую ещё не разобрал
    оператор: долг в журнале у всех прежний, и списание взяло бы ту же
    сумму второй раз. Ссылка, которую банк так и не выдал, - не в счёт:
    оплатить её нечем.
    """
    busy = {int(o["client_id"]) for o in orders
            if o.get("status") in PAY_OPEN
            and (o.get("operation_id") or o.get("kind") == "auto")}
    busy |= {int(c["client_id"]) for c in claims
             if (c.get("status") or "pending") == "pending"}
    return busy


def autocharge_time(settings: Mapping[str, Any] | None, now: datetime) -> bool:
    """Пора ли дневному проходу автосписания: час настал, а сегодня (по
    местным часам) прохода ещё не было. Отметка - в crm.settings
    (`autocharge_done_on`), а не в памяти цикла: перезапуск бота после
    часа списания иначе прогонял бы проход второй раз за день.

    Догоняет только до вечера (too_late_for_clients): списание с карты
    приходит клиенту сообщением, и после простоя бота оно не должно
    уходить в половине двенадцатого ночи. Пропущенный день спишется
    завтра в свой час - долг никуда не денется."""
    hour = pay_settings(settings)["autocharge_hour"]
    if now.hour < hour or too_late_for_clients(now.hour, hour):
        return False
    return str((settings or {}).get("autocharge_done_on") or "") != now.date().isoformat()


# Как часто можно предложить одному клиенту привязать карту: платит он
# раз в период, и напоминать об этом на каждой оплате - уже спам.
CARD_NUDGE_DAYS = 30


def card_nudge_ready(settings: Mapping[str, Any] | None, cards_seen: Any) -> bool:
    """Предлагать ли клиенту привязку карты. Только когда автосписание
    включил владелец и банк уже присылал карты: без этого «спишем сами»
    было бы обещанием, которого система не выполнит."""
    return bool(pay_settings(settings)["autocharge"]) and int(cards_seen or 0) > 0


def renters_without_card(rentals: Iterable[Mapping[str, Any]],
                         cards: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Идущие аренды, у клиента которых нет привязанной карты: этим
    автосписание не поможет, платить они будут сами. Клиент - одна строка.

    nudged_at - `clients.card_nudge_at` со строки аренды: по той же отметке
    бот решает «рано предлагать снова». История отправок живёт 30 дней, а
    срок владелец ставит до 365 - по ней панель писала бы «не предлагали»
    клиенту, которого бот ещё держит."""
    have = {int(c["client_id"]) for c in cards
            if c.get("client_id") is not None and c.get("active", True)}
    seen: set[int] = set()
    out = []
    for rental in rentals:
        client_id = rental.get("client_id")
        if (client_id is None or rental.get("status", "active") != "active"
                or int(client_id) in have or int(client_id) in seen):
            continue
        seen.add(int(client_id))
        out.append({"client_id": int(client_id), "rental_id": rental.get("id"),
                    "full_name": rental.get("full_name"), "phone": rental.get("phone"),
                    "tg_id": rental.get("tg_id"), "bike_code": rental.get("bike_code"),
                    "balance": to_money(rental.get("balance")),
                    "nudged_at": rental.get("card_nudge_at")})
    out.sort(key=lambda r: str(r.get("full_name") or "").casefold())
    return out


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
# Дневное окно сообщений клиенту, местные часы (from, to). Им же меряются
# вечерняя граница уведомлений по расписанию (too_late_for_clients) и
# скидка ночного начисления, которая ждёт утра (promo_hours_ok). Вопрос
# после сдачи - то же окно (FEEDBACK_HOURS): сообщение в 23:40 будит.
CLIENT_HOURS = (9, 21)

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
    "card_nudge": {
        "group": "client", "target": "client", "hour": None,
        "title": "Предложить привязать карту",
        "hint": "После оплаты по ссылке без сохранённой карты - как работает "
                "автосписание. Только когда оно включено и банк уже присылал "
                "карты; одному клиенту не чаще раза в срок.",
        "params": {"every_days": 30},
    },
    "promo_applied": {
        "group": "client", "target": "client", "hour": None,
        "title": "Скидка по акции",
        "hint": "Уходит сразу, как акция сработала: на выдаче или при "
                "начислении периода. Ночное начисление (сразу после "
                "полуночи) ждёт утра: скидка уходит с 9:00.",
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
    "feedback_ask": {
        "group": "client", "target": "client", "hour": None,
        "title": "«Как вам аренда?» после сдачи",
        "hint": "Оценка от 1 до 5 кнопкой, когда велосипед вернули. "
                "Только днём: закрытие поздно вечером спросят утром; сдачу "
                "давнее двух суток и закрытую в день выдачи не спрашивают. "
                "Выключено - сдачи помечаются неспрошенными и после "
                "включения не догоняются.",
        "params": {"from_hour": 9, "to_hour": 21},
    },
    # ─ команде ─
    "feedback_low": {
        "group": "team", "target": "chat", "hour": None,
        "title": "Низкая оценка аренды",
        "hint": "Клиент поставил 3 и ниже. Ждёт комментарий до 10 минут; "
                "в сообщении имя, точка и номер аренды - без телефона и "
                "без текста комментария, он в отчёте «Оценки».",
    },
    "estimate_waiting": {
        "group": "team", "target": "chat", "hour": None,
        "title": "Наряд ждёт согласования",
        "hint": "В момент отправки сметы: что ушло клиенту и на сколько. "
                "Молчание дольше суток - отдельной сводкой ниже.",
    },
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
    "server_health": {
        # Сразу, а не в свой час: кончающийся ночью диск ждать утра не
        # будет. Повтор - раз в сутки, пока беда длится (HEALTH_REPEAT).
        "group": "team", "target": "chat", "hour": None,
        "title": "Здоровье сервера",
        "hint": "Бот раз в час смотрит диск, бэкап и его копию в облаке, "
                "проверку восстановления, панель и сертификаты доменов. "
                "Пишет, когда что-то сломалось, раз в сутки - пока не "
                "починено, и когда починилось. Упавший бот сам о себе не "
                "напишет - для этого внешний монитор (INSTALL.md).",
        "params": {"disk_pct": 10, "cert_days": 14},
    },
    "franchise_stale": {
        "group": "team", "target": "chat", "hour": 10,
        "title": "Франчайзи без свежих данных",
        "hint": "Кто из франчайзи дольше полутора суток не отвечает на опрос: "
                "сломан адрес, токен или сервер. Только число, без имён и "
                "цифр - раздел «Франчайзи» видит один владелец. Молчит, "
                "когда франчайзи нет или все отвечают.",
    },
    "bank_unmatched": {
        # Час, а не «сразу по событию»: строка выписки появляется молча,
        # и напоминать о ней нужно раз в день, а не на каждый круг опроса
        # банка. Без часа расписание это уведомление не подхватывало вовсе,
        # и тумблер в панели ничего не включал.
        "group": "team", "target": "chat", "hour": 10,
        "title": "Не разобранные поступления",
        "hint": "Деньги на счёте есть, а кому - неизвестно. "
                "Раз в сутки, пока строки висят неразобранными.",
    },
    "pay_paid": {
        "group": "team", "target": "chat", "hour": None,
        "title": "Оплачен счёт",
        "hint": "Оператор ждёт этого сообщения, чтобы выдать велосипед.",
    },
    "pay_twice": {
        "group": "team", "target": "chat", "hour": None,
        "title": "Счёт оплачен дважды",
        "hint": "Счёт закрыли наличными или переводом, а клиент оплатил и "
                "ссылку. Второй раз в журнал не зачислено: верните деньги "
                "или зачтите их руками.",
    },
    "autocharge_unknown": {
        "group": "team", "target": "chat", "hour": None,
        "title": "Банк не ответил на автосписание",
        "hint": "Прошло ли списание, неизвестно: счёт остаётся открытым, и "
                "клиенту больше не списываем, пока операцию не сверят в Точке.",
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
    "repair_overdue": {
        "group": "team", "target": "chat", "hour": 10,
        "title": "Наряды дольше срока ремонта",
        "hint": "Открытые наряды, которые стоят дольше срока своего самого "
                "долгого узла (или общего срока). Сроки - в «Сервис → Виды "
                "работ». Молчит, когда все укладываются.",
    },
    "parts_low": {
        # Раз в неделю, а не каждый день: запчасть заказывают партией, и
        # ежедневный список одних и тех же позиций перестают читать.
        "group": "team", "target": "chat", "hour": 10,
        "title": "Запчасти на исходе — раз в неделю",
        "hint": "Позиции ниже неснижаемого и на самом пределе, с тем, что "
                "уже едет от поставщика. Молчит, когда запас в норме.",
        "params": {"weekday": 1},
    },
    # ─ в канал ─
    "inbox_new": {
        "group": "team", "target": "chat", "hour": None,
        "title": "Новое обращение во «Входящих»",
        "hint": "Написали с Авито, WhatsApp или гость в боте. В сообщении "
                "номер обращения, канал и объявление - без имени, телефона "
                "и текста: служебный чат читают все.",
    },
    "booking_new": {
        "group": "team", "target": "chat", "hour": None,
        "title": "Новая заявка на аренду",
        "hint": "Клиент выбрал в кабинете модель, срок и точку. Уходит "
                "в момент заявки.",
    },
    "booking_cancelled": {
        "group": "client", "target": "client", "hour": None,
        "title": "Заявка на аренду снята",
        "hint": "Оператор снял заявку в панели; причина - в сообщении.",
    },
    "waitlist": {
        "group": "client", "target": "client", "hour": None,
        "title": "Лист ожидания: велосипед освободился",
        "hint": "Клиенту с открытой заявкой, когда его модель освободилась на "
                "его точке: сначала давним заявкам, раз в сутки на заявку и "
                "только днём. Велосипед не бронируется - кто первым приедет.",
        "params": {"per_bike": 2, "from_hour": 9, "to_hour": 18},
    },
    "waitlist_coming": {
        "group": "team", "target": "chat", "hour": None,
        "title": "Клиент из листа ожидания едет",
        "hint": "Нажал «Беру — приеду сегодня» под сообщением об освободившемся "
                "велосипеде. Выдача - из заявки, как обычно.",
    },
    "battery_request": {
        "group": "team", "target": "chat", "hour": None,
        "title": "Клиент просит второй аккумулятор",
        "hint": "Кнопка в кабинете при пополнении или «продлю». Денег не "
                "списано: выдайте батарею на точке и добавьте позицию в аренду.",
    },
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


# Числовые параметры уведомлений в панели: имя для ошибки, подпись до поля
# и после него, границы. Пока параметры были только сроками, панель писала
# «через N дн.» у каждого - у числа клиентов на велосипед это было бы враньём.
NOTICE_PARAMS: dict[str, tuple[str, str, str, int, int]] = {
    "after_days": ("Срок", "через", "дн.", 0, 365),
    "every_days": ("Срок", "не чаще раза в", "дн.", 1, 365),
    "per_bike": ("Клиентов на велосипед", "не больше", "клиентов на велосипед", 1, 10),
    "from_hour": ("Час начала", "с", "ч", 0, 23),
    "to_hour": ("Час конца", "до", "ч", 1, 24),
    # Здоровье сервера: у порога диска единица - проценты, а не дни.
    "disk_pct": ("Порог диска", "диск: свободно меньше", "%", 1, 90),
    "cert_days": ("Порог сертификата", "сертификат: осталось меньше", "дн.", 1, 90),
    # Недельная сводка: день недели, а не «через 1 дн.».
    "weekday": ("День недели", "день недели", "1 — пн … 7 — вс", 1, 7),
}


def notice_param(setting: Mapping[str, Any] | None, key: str,
                 default: int = 0) -> int:
    """Целый параметр уведомления. Мусор в базе - к умолчанию каталога."""
    extra = (setting or {}).get("extra") or {}
    try:
        return int(extra[key])
    except (KeyError, ValueError, TypeError):
        return default


# Параметр уведомления на экране: имя в ошибке, подпись до поля и после,
# пределы. Одна таблица на все уведомления - NOTICE_PARAMS по имени
# параметра; уведомлению с особой подписью того же имени хватит
# `param_labels` в каталоге. Неизвестное имя - срок в днях.
NOTICE_PARAM_DEFAULT_LABEL = ("Срок", "через", "дн.", 0, 365)


def notice_param_label(code: str, key: str) -> tuple[str, str, str, int, int]:
    labels = (NOTICES.get(code) or {}).get("param_labels") or {}
    return tuple(labels.get(key) or NOTICE_PARAMS.get(key)  # type: ignore[return-value]
                 or NOTICE_PARAM_DEFAULT_LABEL)


def weekly_due(setting: Mapping[str, Any] | None, today: date) -> bool:
    """Недельное уведомление: сегодня его день. Час проверяет обычное
    расписание; день недели вне 1..7 - понедельник, а не «никогда»."""
    weekday = notice_param(setting, "weekday", 1)
    if not 1 <= weekday <= 7:
        weekday = 1
    return today.isoweekday() == weekday


def notice_time(setting: Mapping[str, Any] | None) -> str:
    if not setting or setting.get("at_hour") is None:
        return "сразу"
    return f"{int(setting['at_hour']):02d}:{int(setting.get('at_minute') or 0):02d}"


def too_late_for_clients(now_hour: int, start_hour: int) -> bool:
    """Поздно ли догонять сообщение клиенту или в канал, чей час
    `start_hour`. Граница - конец дневного окна (CLIENT_HOURS), а час,
    который владелец сам поставил на вечер, догоняется только в пределах
    своего часа: иначе такое уведомление не уходило бы никогда."""
    return now_hour >= max(CLIENT_HOURS[1], int(start_hour) + 1)


def notice_due(setting: Mapping[str, Any] | None, now: datetime,
               done_on: date | None = None) -> bool:
    """Пора ли отправлять уведомление по расписанию.

    Не «ровно в этот час», а «в этот час или позже, если сегодня ещё не
    отправляли»: бота перезапускают среди дня, и привязка к минуте молча
    съедала бы уведомления за целые сутки.

    Догоняет клиентское и канальное только до вечера (too_late_for_clients):
    бот, поднятый в 23:30 после простоя, слал бы напоминание об оплате,
    просьбу об отзыве и пост о свободных велосипедах на ночь глядя.
    Пропущенное сегодня уходит завтра в свой час - отметка дня не
    ставится. Команде - без границы: служебный чат читают и вечером.
    """
    if not setting or not setting.get("enabled"):
        return False
    hour = setting.get("at_hour")
    if hour is None:
        return False                       # «сразу» расписанием не ловится
    if done_on == now.date():
        return False
    minutes_now = now.hour * 60 + now.minute
    if minutes_now < int(hour) * 60 + int(setting.get("at_minute") or 0):
        return False
    if setting.get("target") in ("client", "channel"):
        return not too_late_for_clients(now.hour, int(hour))
    return True


def promo_hours_ok(setting: Mapping[str, Any] | None, now: datetime) -> bool:
    """Днём ли говорить клиенту о скидке, которую дало начисление.
    Начисляет дневной проход в первом круге суток, сразу после полуночи, и
    «скидка по акции» в 00:05 будила бы - такие ждут утра в очереди."""
    return notice_hours_ok(setting, now, CLIENT_HOURS)


# Сколько скидок держит ночная очередь. Одна на период аренды - столько
# за ночь не набирается и у большого парка; предел - от мусора в строке.
PROMO_QUEUE_MAX = 500


def promo_queue_item(got: Mapping[str, Any]) -> dict[str, Any]:
    """Сработавшая акция - в строку очереди: только номера и сумма. Акцию
    перечитывают при отправке, клиента тоже - в настройке им не место."""
    return {"client_id": int(got["client_id"]), "promo_id": int(got["promo"]["id"]),
            "amount": str(to_money(got["amount"])),
            "period_index": int(got.get("period_index") or 0)}


def parse_promo_queue(raw: Any) -> list[dict[str, Any]]:
    """Очередь скидок из crm.settings. Мусор отбрасывается построчно, а не
    роняет проход: сломанная строка не должна держать остальных."""
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    out: list[dict[str, Any]] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            out.append({"client_id": int(item["client_id"]),
                        "promo_id": int(item["promo_id"]),
                        "amount": to_money(item["amount"]),
                        "period_index": int(item.get("period_index") or 0)})
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
    return out[-PROMO_QUEUE_MAX:]


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
        row["labels"] = {key: notice_param_label(code, key) for key in item["extra"]}
        out.setdefault(item["group"], []).append(row)
    return out


# ────────────────────── здоровье сервера ──────────────────────
#
# Сервис backup (backup.sh) кладёт итог своих шагов в crm.settings одной
# строкой JSON (BACKUP_STATUS_KEY). Бот раз в час меряет диск, панель и
# сертификаты, складывает с отчётом бэкапа и пишет владельцу уведомлением
# server_health. Что сейчас не так и когда об этом писали - тоже в
# crm.settings (HEALTH_KEY): перезапуск бота не должен ни повторять
# вчерашнюю тревогу, ни терять «починилось». Отметка проверки там же -
# пульс процесса бота: /healthz/bot панели по нему отвечает 503.

BACKUP_STATUS_KEY = "backup_status"
HEALTH_KEY = "server_health"
BACKUP_PARTS = ("dump", "offsite", "restore")
# Дамп раз в сутки: 26 часов - сутки и запас на долгий дамп и перезапуск.
BACKUP_STALE = timedelta(hours=26)
# Проверка восстановления раз в неделю; девятый день без неё - повод.
RESTORE_STALE = timedelta(days=8)
# Пока беда длится, о ней напоминают раз в сутки, а не каждый час:
# ежечасное «всё ещё» перестают читать к обеду.
HEALTH_REPEAT = timedelta(hours=24)
# Проверка раз в час; два пропущенных круга - процесс бота стоит.
HEALTH_PULSE = timedelta(hours=2)
BACKUP_TITLES = {"dump": "Бэкап базы", "offsite": "Копия в облаке",
                 "restore": "Проверка восстановления"}


def _health_time(moment: datetime) -> str:
    return moment.astimezone(MOSCOW).strftime("%d.%m %H:%M")


def _bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f}".replace(".", ",") + " ГБ"
    return f"{max(n, 0) / 1024 ** 2:.0f} МБ"


def _json_map(raw: Any) -> Mapping[str, Any] | None:
    if isinstance(raw, Mapping):
        return raw
    try:
        data = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, Mapping) else None


def parse_backup_status(raw: Any) -> dict[str, dict[str, Any]] | None:
    """Отчёт сервиса backup. Нет строки или мусор - None: бот скажет
    «сервис не отчитывался», а не упадёт на чужом JSON. Строку пишет
    shell-сценарий, поэтому каждое поле проверяется на тип."""
    data = _json_map(raw)
    if data is None:
        return None
    out: dict[str, dict[str, Any]] = {}
    for part in BACKUP_PARTS:
        item = data.get(part)
        item = item if isinstance(item, Mapping) else {}
        row: dict[str, Any] = {"at": _moment(item.get("at")),
                               "last_ok": _moment(item.get("last_ok"))}
        ok = item.get("ok")
        row["ok"] = ok if isinstance(ok, bool) else None
        for key in ("error", "prune_error", "file", "source", "target"):
            value = item.get(key)
            row[key] = str(value)[:300] if value not in (None, "") else None
        size = item.get("size")
        row["size"] = (size if isinstance(size, int) and not isinstance(size, bool)
                       and size >= 0 else None)
        # Облако выключено, пока сервис не сказал обратного.
        row["enabled"] = item.get("enabled") is True if part == "offsite" else True
        tables = item.get("tables")
        row["tables"] = {
            str(name): (pair[0], pair[1]) for name, pair in
            (tables.items() if isinstance(tables, Mapping) else ())
            if isinstance(pair, list) and len(pair) == 2
            and all(isinstance(n, int) and not isinstance(n, bool) for n in pair)}
        out[part] = row
    return out


def backup_problems(backup: Mapping[str, Mapping[str, Any]] | None,
                    now: datetime) -> dict[str, dict[str, str]]:
    """Беды бэкапа: часть отчёта → заголовок и текст. Пусто - порядок."""
    out: dict[str, dict[str, str]] = {}

    def add(part: str, text: str) -> None:
        out[part] = {"title": BACKUP_TITLES[part], "text": text}

    def since(moment: datetime | None, what: str) -> str:
        return f" {what} - {_health_time(moment)}." if moment else ""

    if backup is None:
        add("dump", "Сервис backup ни разу не отчитался - бэкапа может не быть "
                    "вовсе. Проверьте: docker compose ps backup")
        return out
    dump = backup.get("dump") or {}
    if dump.get("ok") is False and dump.get("at"):
        add("dump", f"Бэкап базы не сделался {_health_time(dump['at'])}: "
                    f"{dump.get('error') or 'причина не записана'}."
                    + since(dump.get("last_ok"), "Последний удачный"))
    elif dump.get("last_ok") is None:
        add("dump", "Бэкапа базы ещё не было.")
    elif now - dump["last_ok"] > BACKUP_STALE:
        add("dump", f"Бэкап базы не делался с {_health_time(dump['last_ok'])}: "
                    "сервис backup стоит? docker compose logs backup")
    off = backup.get("offsite") or {}
    # Облако включено, но ещё ни разу не пробовало - не беда: первая
    # отправка идёт следом за первым дампом.
    if off.get("enabled") and off.get("at"):
        if off.get("ok") is False:
            add("offsite", f"Копия в облако не ушла {_health_time(off['at'])}: "
                           f"{off.get('error') or 'причина не записана'}."
                           + since(off.get("last_ok"), "Последняя удачная"))
        elif off.get("last_ok") and now - off["last_ok"] > BACKUP_STALE:
            add("offsite", "Копия в облаке не обновлялась с "
                           f"{_health_time(off['last_ok'])}.")
        elif off.get("prune_error"):
            add("offsite", "Копия в облако ушла, но старые не удаляются: "
                           f"{off['prune_error']}. Бакет будет только расти.")
    rest = backup.get("restore") or {}
    where = "из облака" if rest.get("source") == "offsite" else "с диска"
    if rest.get("ok") is False and rest.get("at"):
        add("restore", f"Копия {where} не восстановилась {_health_time(rest['at'])}: "
                       f"{rest.get('error') or 'причина не записана'}."
                       + since(rest.get("last_ok"), "Последняя удачная проверка"))
    elif rest.get("at") is None:
        if dump.get("last_ok") is not None:
            add("restore", "Проверка восстановления ещё ни разу не проходила.")
    elif now - rest["at"] > RESTORE_STALE:
        add("restore", "Проверка восстановления не проходила с "
                       f"{_health_time(rest['at'])}.")
    return out


def health_problems(*, now: datetime,
                    backup: Mapping[str, Mapping[str, Any]] | None,
                    disk: tuple[int, int] | None = None,
                    panel_error: str | None = None,
                    certs: Iterable[tuple[str, datetime | None, str | None]] = (),
                    disk_pct: int = 10, cert_days: int = 14
                    ) -> dict[str, dict[str, str]]:
    """Что сейчас не так на сервере: код беды → заголовок и текст.

    Код - ключ памяти между проверками: тот же код через час - «всё ещё»,
    а не новая беда. У сертификата код с именем домена, у бэкапа - часть
    отчёта. `disk` - свободно и всего байт; None - замерить не вышло, и
    про диск молчим, а не пугаем.
    """
    out: dict[str, dict[str, str]] = {}
    if disk:
        free, total = disk
        if total > 0 and free * 100 < total * max(int(disk_pct), 0):
            out["disk"] = {"title": "Диск", "text": (
                f"Диск почти полон: свободно {_bytes(free)} из {_bytes(total)} "
                f"({free * 100 // total} %). Кончится место - встанут база и бэкап. "
                "Старое чистится так: docker system prune и BACKUP_KEEP_DAYS в .env.")}
    out.update(backup_problems(backup, now))
    if panel_error:
        out["panel"] = {"title": "Панель",
                        "text": f"Панель не отвечает: {panel_error}. "
                                "docker compose ps crm"}
    for host, until, error in certs:
        key = f"cert:{host}"
        if error:
            out[key] = {"title": f"HTTPS {host}",
                        "text": f"{host} не отвечает по HTTPS: {error}."}
        elif until is not None and until - now < timedelta(days=max(int(cert_days), 0)):
            left = max((until - now).days, 0)
            out[key] = {"title": f"Сертификат {host}", "text": (
                f"Сертификат {host} истекает {_health_time(until)} (осталось {left} дн.): "
                "Caddy не продлил его сам. docker compose logs caddy")}
    return out


def parse_health_state(raw: Any) -> dict[str, Any]:
    """Память проверки сервера. Мусор - чистый лист: лишнее сообщение
    лучше молчания из-за битой строки."""
    data = _json_map(raw) or {}
    problems: dict[str, dict[str, Any]] = {}
    stored = data.get("problems")
    for key, item in (stored.items() if isinstance(stored, Mapping) else ()):
        if isinstance(item, Mapping):
            problems[str(key)] = {"title": str(item.get("title") or key),
                                  "text": str(item.get("text") or ""),
                                  "since": _moment(item.get("since")),
                                  "alerted_at": _moment(item.get("alerted_at"))}
    return {"checked_at": _moment(data.get("checked_at")), "problems": problems}


def _health_dump(checked_at: datetime,
                 problems: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    def iso(moment: Any) -> str | None:
        return moment.isoformat() if isinstance(moment, datetime) else None
    return {"checked_at": checked_at.isoformat(),
            "problems": {key: {"title": p["title"], "text": p["text"],
                               "since": iso(p.get("since")),
                               "alerted_at": iso(p.get("alerted_at"))}
                         for key, p in problems.items()}}


def health_step(prev: Mapping[str, Any] | None,
                problems: Mapping[str, Mapping[str, str]],
                now: datetime) -> tuple[list[str], dict[str, Any]]:
    """Что написать владельцу и что запомнить до следующей проверки.

    Новая беда - сразу; длящаяся - раз в HEALTH_REPEAT с тем, с какого
    момента она тянется; исчезнувшая - «снова в порядке». Состояние -
    JSON для crm.settings.
    """
    old = (prev or {}).get("problems") or {}
    lines: list[str] = []
    keep: dict[str, dict[str, Any]] = {}
    for key, item in problems.items():
        was = old.get(key)
        entry = {"title": item["title"], "text": item["text"],
                 "since": (was or {}).get("since") or now,
                 "alerted_at": (was or {}).get("alerted_at")}
        if was is None:
            lines.append(f"⚠️ {item['text']}")
            entry["alerted_at"] = now
        elif entry["alerted_at"] is None or now - entry["alerted_at"] >= HEALTH_REPEAT:
            lines.append(f"⚠️ Всё ещё, с {_health_time(entry['since'])}: {item['text']}")
            entry["alerted_at"] = now
        keep[key] = entry
    for key, was in old.items():
        if key not in problems:
            started = was.get("since")
            lines.append(f"✅ {was.get('title') or key}: снова в порядке"
                         + (f" (сбой тянулся с {_health_time(started)})"
                            if isinstance(started, datetime) else ""))
    return lines, _health_dump(now, keep)


def health_keep(prev: Mapping[str, Any] | None, now: datetime) -> dict[str, Any]:
    """Прежняя память с новой отметкой проверки: сообщение не доставлено,
    и следующий круг должен сказать то же самое, а не промолчать."""
    return _health_dump(now, (prev or {}).get("problems") or {})


def health_message(lines: Iterable[str]) -> str:
    """Текст для Telegram (разметка HTML): ошибки чужих программ могут
    содержать «<», поэтому каждая строка экранируется."""
    return "🖥 <b>Сервер</b>\n" + "\n".join(html.escape(line) for line in lines)


def bot_alive(state: Mapping[str, Any] | None, now: datetime) -> bool:
    checked = (state or {}).get("checked_at")
    return isinstance(checked, datetime) and now - checked <= HEALTH_PULSE


def server_rows(backup: Mapping[str, Mapping[str, Any]] | None,
                state: Mapping[str, Any] | None, now: datetime, *,
                bot: bool = True) -> list[dict[str, Any]]:
    """Строки карточки «Сервер» в панели: ok True - в порядке, False -
    беда, None - нечего сказать (облако выключено, ещё не проверяли).
    Бэкап судится тем же правилом, что и сообщение владельцу. `bot` -
    есть ли процесс бота вовсе: у демо-стенда его нет, и строка о пульсе
    через два часа после сброса пугала бы покупателя."""
    state = state or {}
    checked = state.get("checked_at")
    if not bot:
        rows: list[dict[str, Any]] = []
    elif not isinstance(checked, datetime):
        rows = [{"title": "Процесс бота", "ok": None,
                 "text": "ещё ни разу не проверял сервер"}]
    elif bot_alive(state, now):
        rows = [{"title": "Процесс бота", "ok": True,
                 "text": f"на связи, проверка {_health_time(checked)}"}]
    else:
        rows = [{"title": "Процесс бота", "ok": False,
                 "text": f"молчит с {_health_time(checked)}: фоновые задачи, "
                         "начисления и уведомления стоят. docker compose ps bot"}]
    bad = backup_problems(backup, now)
    parts = backup or {}
    dump = parts.get("dump") or {}
    off = parts.get("offsite") or {}
    rest = parts.get("restore") or {}
    for part in BACKUP_PARTS:
        if part in bad:
            rows.append({"title": BACKUP_TITLES[part], "ok": False, "text": bad[part]["text"]})
        elif part == "dump" and dump.get("last_ok"):
            size = f", {_bytes(dump['size'])}" if dump.get("size") is not None else ""
            rows.append({"title": BACKUP_TITLES[part], "ok": True,
                         "text": f"{_health_time(dump['last_ok'])}{size}"})
        elif part == "offsite" and not off.get("enabled"):
            rows.append({"title": BACKUP_TITLES[part], "ok": None,
                         "text": "выключена: в .env не задан BACKUP_S3_BUCKET - "
                                 "умрёт сервер, умрут и дампы"})
        elif part == "offsite" and off.get("last_ok"):
            rows.append({"title": BACKUP_TITLES[part], "ok": True,
                         "text": f"{_health_time(off['last_ok'])} → {off.get('target') or 'S3'}"})
        elif part == "restore" and rest.get("ok"):
            where = "из облака" if rest.get("source") == "offsite" else "с диска"
            rows.append({"title": BACKUP_TITLES[part], "ok": True,
                         "text": f"{_health_time(rest['at'])}, копия {where} развернулась"})
        else:
            rows.append({"title": BACKUP_TITLES[part], "ok": None, "text": "ещё не было"})
    for key, item in (state.get("problems") or {}).items():
        if key not in BACKUP_PARTS:
            rows.append({"title": item.get("title") or key, "ok": False,
                         "text": item.get("text") or ""})
    return rows


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


# ────────────────────── оценка аренды после сдачи ──────────────────────
#
# Отзыв на площадке - для чужих глаз, оценка - для нас: «как вам аренда?»
# сразу после сдачи, одной кнопкой от 1 до 5. Низкая оценка - это клиент,
# который уйдёт к конкуренту, и владелец узнаёт о ней в тот же час.

FEEDBACK_SCORES = (1, 2, 3, 4, 5)
# «Тройка» у курьера - уже недовольство: довольный ставит пять не глядя.
FEEDBACK_LOW = 3
FEEDBACK_COMMENT_MAX = 1000
# Комментарий - слова клиента того же рода, что переписка «Входящих», и
# живёт столько же (INBOX_KEEP_DAYS): потом дневной проход бота его стирает,
# а оценка остаётся в отчёте. Ключом «Входящих» не шифруем: ключ
# необязателен, и без него комментарий было бы негде хранить.
FEEDBACK_COMMENT_KEEP_DAYS = 90
# Спрашиваем только свежую сдачу: бот, лежавший неделю, не должен после
# подъёма спросить «как вам аренда» у всех, кто сдал велосипед за неделю.
FEEDBACK_ASK_HOURS = 48
# Окно вопроса, местные часы (параметры уведомления «feedback_ask»):
# сдача днём и вечером спрашивается сразу, а закрытие, которое оператор
# провёл за полночь, ждёт утра - сообщение о вчерашней сдаче в 23:40 будит.
# Окно общее для сообщений клиенту (CLIENT_HOURS).
FEEDBACK_HOURS = CLIENT_HOURS
# Сигнал о низкой оценке ждёт комментарий: одно сообщение «2 из 5, есть
# комментарий» лучше двух подряд.
FEEDBACK_ALERT_WAIT_MINUTES = 10
# Спрашиваем, только когда техника вернулась: «как вам аренда?» после
# признания потери, выкупа или списания звучало бы издёвкой.
FEEDBACK_RETURNED = ("available", "repair", "maintenance", "reserved")
FEEDBACK_CHANNELS = {"tg": "Telegram", "max": "MAX"}
_FEEDBACK_DATA = re.compile(r"^fb:(\d{1,12}):([1-5])$")


def feedback_wanted(bike_status: Any) -> bool:
    return str(bike_status or "") in FEEDBACK_RETURNED


def feedback_callback(rental_id: int, score: int) -> str:
    """Кнопка оценки. Номер аренды в ней - не секрет: оценку принимает
    только клиент этой аренды (service.rate_rental сверяет, кто нажал)."""
    return f"fb:{int(rental_id)}:{int(score)}"


def parse_feedback_callback(data: Any) -> tuple[int, int] | None:
    found = _FEEDBACK_DATA.match(str(data or ""))
    if not found:
        return None
    return int(found.group(1)), int(found.group(2))


def feedback_low(score: Any) -> bool:
    try:
        return 1 <= int(score) <= FEEDBACK_LOW
    except (TypeError, ValueError):
        return False


def feedback_stars(score: Any) -> str:
    try:
        n = int(score)
    except (TypeError, ValueError):
        return ""
    if not 1 <= n <= 5:
        return ""
    return "★" * n + "☆" * (5 - n)


def check_feedback_comment(raw: Any) -> Check:
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return Check(False, error="Напишите комментарий текстом, одним сообщением.")
    if len(text) > FEEDBACK_COMMENT_MAX:
        return Check(False, error=f"Комментарий длиннее {FEEDBACK_COMMENT_MAX} знаков "
                                  "— сократите его.")
    return Check(True, text)


def feedback_channel(row: Mapping[str, Any], *, max_ready: bool) -> str | None:
    """Куда спрашивать: Telegram, если клиент там есть, иначе MAX, если
    MAX-бот подключён. Спросить некуда - None."""
    if row.get("tg_id"):
        return "tg"
    if row.get("max_id") and max_ready:
        return "max"
    return None


def feedback_stale(row: Mapping[str, Any], now: datetime) -> bool:
    """Сдача давнее FEEDBACK_ASK_HOURS: спрашивать поздно.

    Сдача - и момент закрытия в панели (closed_at), и дата возврата
    (closed_on). Закрытие задним числом - обычное дело: оператор вечером
    догоняет возвраты, и closed_at у них сегодняшний, а велосипед вернули
    неделю назад. Дата возврата живёт до конца своих местных суток."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    limit = timedelta(hours=FEEDBACK_ASK_HOURS)
    at = row.get("closed_at") or row.get("created_at")
    if isinstance(at, datetime):
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        if now - at > limit:
            return True
    day = row.get("closed_on")
    if isinstance(day, date) and not isinstance(day, datetime):
        end = datetime.combine(day + timedelta(days=1), datetime.min.time()).astimezone()
        return now - end > limit
    return False


def feedback_void(row: Mapping[str, Any]) -> bool:
    """Закрыта в день выдачи: исправление оператора, а не аренда - то же
    правило, что у оценки риска (CrmDB.risk_facts, `void`). Вопрос «как вам
    аренда?» пришёл бы человеку, который стоит на точке и ждёт исправленную
    выдачу. Потерянных здесь нет: их в очередь не ставят вовсе."""
    start, end = row.get("started_on"), row.get("closed_on")
    return isinstance(start, date) and isinstance(end, date) and end <= start


def feedback_hours_ok(setting: Mapping[str, Any] | None, now: datetime) -> bool:
    """Днём ли спрашивать; вне окна очередь ждёт утра. Час - местный."""
    return notice_hours_ok(setting, now, FEEDBACK_HOURS)


def feedback_skip_reason(row: Mapping[str, Any], *, enabled: bool, now: datetime,
                         max_ready: bool) -> str | None:
    """Почему не спрашиваем эту сдачу; None - спрашиваем."""
    if not enabled:
        return "выключено в настройках"
    if feedback_void(row):
        return "закрыта в день выдачи: исправление, а не аренда"
    if feedback_stale(row, now):
        return "сдача давнее двух суток"
    if str(row.get("client_status") or "active") != "active":
        return "клиент не активен"
    if feedback_channel(row, max_ready=max_ready) is None:
        return "клиента нет в боте"
    return None


def feedback_alert_text(row: Mapping[str, Any]) -> str:
    """Сигнал о низкой оценке в служебный чат. Имя - как в соседних
    командных уведомлениях; телефона и самого комментария нет: чат
    читают все, а комментарий - слова клиента, его место в панели."""
    score = int(row.get("score") or 0)
    place = row.get("location") or "без точки"
    bike = f" · № {row['bike_code']}" if row.get("bike_code") else ""
    tail = ("есть комментарий — в панели" if row.get("comment")
            else "без комментария")
    return (f"😟 Низкая оценка аренды: {score} из 5 {feedback_stars(score)}\n"
            f"{html.escape(str(row.get('full_name') or '—'), quote=False)}"
            f"{html.escape(bike, quote=False)} · {html.escape(str(place), quote=False)}\n"
            f"Аренда № {int(row.get('rental_id') or 0)} (/rentals/"
            f"{int(row.get('rental_id') or 0)}), {tail}.")


def _feedback_stats(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    asked = [r for r in rows if r.get("channel")]
    scored = [int(r["score"]) for r in rows if r.get("score")]
    dist = {s: scored.count(s) for s in FEEDBACK_SCORES}
    avg = (Decimal(sum(scored)) / len(scored)).quantize(Decimal("0.1"), ROUND_HALF_UP) \
        if scored else None
    return {"asked": len(asked), "answered": len(scored),
            "rate": round(100 * len(scored) / len(asked)) if asked else None,
            "avg": avg, "dist": dist,
            "low": sum(1 for s in scored if s <= FEEDBACK_LOW)}


def feedback_stats(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Итог оценок по набору сдач: спросили, ответили, средняя, низких.
    Для сводного окна отчётов - за его период, а не по месяцам."""
    return _feedback_stats(list(rows))


def feedback_report(rows: Iterable[Mapping[str, Any]], *, months: int = 12,
                    today: date | None = None) -> dict[str, Any]:
    """Оценки по месяцам сдачи и по точкам аренды, плюс низкие.

    Месяц - по дню возврата (closed_on), точка - точка выдачи аренды, как
    у денег в «По точкам»: иначе одна аренда жила бы в двух местах.
    «Спросили» - только ушедшие вопросы: не спрошенные (клиента нет в
    боте, выключено) в долю ответов не входят.
    """
    today = today or date.today()
    first = today.replace(day=1)
    scale: list[date] = []
    for _ in range(months):
        scale.append(first)
        first = (first - timedelta(days=1)).replace(day=1)
    scale.reverse()
    by_month: dict[date, list] = {m: [] for m in scale}
    by_point: dict[str, list] = {}
    kept = []
    for r in rows:
        day = r.get("closed_on") or local_date(r.get("asked_at"))
        if day is None or day.replace(day=1) not in by_month:
            continue
        kept.append(r)
        by_month[day.replace(day=1)].append(r)
        by_point.setdefault(str(r.get("location") or ""), []).append(r)
    points = sorted(by_point, key=lambda p: (p == "", p.lower()))
    low = sorted((r for r in kept if feedback_low(r.get("score"))),
                 key=lambda r: (r.get("answered_at") or datetime.min.replace(tzinfo=UTC)),
                 reverse=True)
    return {
        "months": [{"month": m, **_feedback_stats(by_month[m])} for m in reversed(scale)],
        "points": [{"location": p or None, **_feedback_stats(by_point[p])}
                   for p in points],
        "total": _feedback_stats(kept),
        "low": low,
    }


def feedback_avg(value: Any) -> str:
    """Средняя оценка для экрана: «4,3» или прочерк."""
    if value in (None, ""):
        return "—"
    return str(value).replace(".", ",")


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
        qty = item_qty(item)
        price = to_money(item.get("price")) * qty
        title = str(item.get("title") or "работа")
        lines.append(f"• {title}"
                     + (f" × {qty}" if qty > 1 else "")
                     + f" — {money(price)}")
    return "\n".join(lines)


def order_totals_client(items: Iterable[Mapping[str, Any]]) -> Decimal:
    """Сколько к оплате клиенту. Отдельно от order_totals: там ещё и
    себестоимость, а в смету она не идёт."""
    return to_money(sum(to_money(i.get("price")) * item_qty(i)
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
    "promo": "По акции",
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


def bonus_promise(settings: Mapping[str, Any], *, for_agent: bool = False) -> str:
    """Что бот обещает: другу по умолчанию, агенту - с for_agent.

    Суммы нет - и обещать нечего: пустая строка, и текст скажет «условия
    уточняйте у менеджера». Это честнее «0 ₽ на баланс».
    """
    # Выключенная программа ничего не обещает: суммы в настройках
    # остаются, но платить по ним уже не будут, и старая ссылка-приглашение
    # иначе обещала бы бонус, которого никто не начислит.
    if not settings.get("enabled", True):
        return ""
    agent = to_money(settings.get("bonus"))
    friend = to_money(settings.get("friend_bonus"))
    if agent <= 0 and friend <= 0:
        return ""
    mine, theirs = (agent, friend) if for_agent else (friend, agent)
    parts = []
    if mine > 0:
        parts.append(f"вам {money(mine)}")
    if theirs > 0:
        parts.append(f"другу {money(theirs)}")
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
        day = local_date(made)
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


# ────────────────── фото при сдаче ──────────────────
#
# Спор «царапина была до меня» решается снимком в момент возврата, а не
# памятью оператора. Снимки лежат на томе сверки (bikefiles): персональных
# данных в них нет, и том с паспортами панель по-прежнему не пишет.

RETURN_PHOTOS_MAX = 6
# Столько же, сколько у снимка сверки: телефонное фото столько и весит.
RETURN_PHOTO_MAX_BYTES = 8 * 1024 * 1024
RETURN_PHOTO_SUFFIXES = {".jpg": ".jpg", ".jpeg": ".jpg", ".png": ".png",
                         ".webp": ".webp"}
RETURN_PHOTO_MIMES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
# Бот принимает фото к аренде, закрытой не раньше часа назад: карточка
# сдачи живёт до следующей аренды, и снимок, присланный через неделю,
# лёг бы к давно закрытой аренде как «фото при сдаче».
RETURN_PHOTO_MINUTES = 60
RETURN_PHOTO_DAYS = 180
RETURN_PHOTO_DIR = "returns"
# Имя собираем сами: имя из браузера или Telegram - чужая строка. По
# этому же шаблону файл сверяется перед показом и удалением.
_RETURN_PHOTO_PATH = re.compile(r"^returns/ret-\d{1,12}-[0-9a-f]{12}\.(jpg|png|webp)$")


def return_photo_suffix(filename: Any = None, mime: Any = None) -> str | None:
    """Расширение снимка по имени файла или типу; не картинка - None."""
    if filename:
        dot = str(filename).rfind(".")
        suffix = str(filename)[dot:].lower() if dot >= 0 else ""
        if suffix in RETURN_PHOTO_SUFFIXES:
            return RETURN_PHOTO_SUFFIXES[suffix]
    if mime:
        return RETURN_PHOTO_MIMES.get(str(mime).lower())
    return None


def return_photo_path(rental_id: int, suffix: str, token: str) -> str:
    """Путь снимка от корня тома: returns/ret-<аренда>-<12 hex>.<ext>."""
    ext = RETURN_PHOTO_SUFFIXES.get(str(suffix).lower())
    if ext is None or not re.fullmatch(r"[0-9a-f]{12}", token):
        raise ValueError("недопустимое имя снимка")
    return f"{RETURN_PHOTO_DIR}/ret-{int(rental_id)}-{token}{ext}"


def is_return_photo_path(path: Any) -> bool:
    return bool(_RETURN_PHOTO_PATH.match(str(path or "")))


def return_photo_days(settings: Mapping[str, Any] | None = None) -> int:
    """Сколько дней хранить фото при сдаче. Мусор в базе - умолчание."""
    try:
        days = int(str((settings or {}).get("return_photo_days") or RETURN_PHOTO_DAYS))
    except ValueError:
        return RETURN_PHOTO_DAYS
    return days if 1 <= days <= 3650 else RETURN_PHOTO_DAYS


def return_photo_rental(active: Mapping[str, Any] | None,
                        last: Mapping[str, Any] | None, *, now: datetime,
                        minutes: int = RETURN_PHOTO_MINUTES) -> Mapping[str, Any] | None:
    """К какой аренде приложить фото из бота: идущая (сдача ещё не
    подписана) или только что закрытая. Давно закрытой - никакой."""
    if active is not None:
        return active
    if last is None or last.get("status") != "closed":
        return None
    closed = last.get("closed_at")
    if not isinstance(closed, datetime):
        return None
    if closed.tzinfo is None:
        closed = closed.replace(tzinfo=UTC)
    return last if now - closed <= timedelta(minutes=minutes) else None


# ────────────────── паспорт аккумулятора ──────────────────
#
# У велосипеда сверяют раму и наклейку, у батареи - корпус и табличку.
# Правила те же и настройки те же: «ввод техники» один на всю технику,
# заводить вторую страницу настроек ради второго вида железа незачем.

BATTERY_PASSPORT: dict[str, str] = {
    "model": "Модель и бренд",
    "code": "Номер наклейки",
    "serial_no": "Серийный номер (на корпусе)",
    "volts": "Напряжение по табличке",
    "amp_hours": "Ёмкость по табличке",
}
# Табличка и серийный номер - то, что переписывают из накладной чаще
# всего. Модель и наклейку видно и так.
BATTERY_PHOTO_FIELDS = ("serial_no", "amp_hours")


def battery_field_value(battery: Mapping[str, Any], field: str) -> str:
    """Что сверяем в этом поле. Модель приходит из каталога."""
    if field == "model":
        return str(battery.get("model_title") or "").strip()
    value = battery.get(field)
    if field in ("volts", "amp_hours"):
        return "" if value in (None, "") else str(value)
    return str(value or "").strip()


def battery_checks(battery: Mapping[str, Any],
                   settings: Mapping[str, Any] | None = None) -> list[dict]:
    """Строки сверки паспорта батареи: поле, значение и отметка."""
    checked = battery.get("checked")
    checked = checked if isinstance(checked, Mapping) else {}
    photo_needed = bike_check_settings(settings)["photo"]
    rows = []
    for field, title in BATTERY_PASSPORT.items():
        mark = checked.get(field)
        mark = mark if isinstance(mark, Mapping) else {}
        value = battery_field_value(battery, field)
        needs_photo = photo_needed and field in BATTERY_PHOTO_FIELDS
        rows.append({
            "field": field, "title": title, "value": value,
            "filled": bool(value),
            "at": mark.get("at"), "by": mark.get("by"), "photo": mark.get("photo"),
            "needs_photo": needs_photo,
            "ok": bool(mark.get("at")) and bool(value)
            and (not needs_photo or bool(mark.get("photo"))),
        })
    return rows


def battery_check_state(battery: Mapping[str, Any],
                        settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    rows = battery_checks(battery, settings)
    left = [r["title"] for r in rows if not r["ok"]]
    required = bike_check_settings(settings)["required"]
    return {"rows": rows, "left": left, "done": not left,
            "required": required,
            "can_commission": (not left) or not required,
            "new": str(battery.get("status") or "") == "new"}


def check_volts(raw: Any) -> Check:
    """Напряжение по табличке: 24-96 В. Вне этого - опечатка."""
    text = str(raw or "").strip().replace(",", ".")
    if not text:
        return Check(True, None)
    try:
        value = int(float(text))
    # «inf» и «1e309» float читает бесконечностью, и int() на ней падает
    # OverflowError - это тоже не напряжение, а не повод для 500.
    except (ValueError, OverflowError):
        return Check(False, error="Напряжение: только число, вольты.")
    if not 24 <= value <= 96:
        return Check(False, error="Напряжение: от 24 до 96 В.")
    return Check(True, value)


def check_amp_hours(raw: Any) -> Check:
    """Ёмкость по табличке, А·ч. Ноль - это не ёмкость."""
    text = str(raw or "").strip().replace(",", ".")
    if not text:
        return Check(True, None)
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return Check(False, error="Ёмкость: только число, ампер-часы.")
    # NaN сравнение «меньше-больше» не переносит (InvalidOperation).
    if not value.is_finite() or not Decimal(1) <= value <= Decimal(500):
        return Check(False, error="Ёмкость: от 1 до 500 А·ч.")
    return Check(True, value.quantize(Decimal("0.01")))


# Числа характеристик модели: подпись и предел колонки каталога
# (numeric(6,2), numeric(4,1), integer) с числом знаков после запятой.
# Сверх колонки база отвечала ошибкой, то есть 500 вместо формы.
MODEL_SPEC_NUMBERS: dict[str, tuple[str, Decimal, int]] = {
    "weight_kg": ("Вес, кг", Decimal("9999.99"), 2),
    "speed_kmh": ("Скорость, км/ч", Decimal(999), 0),
    "range_km": ("Запас хода, км", Decimal(9999), 0),
    "charge_hours": ("Зарядка, ч", Decimal("999.9"), 1),
    "motor_watt": ("Мотор, Вт", Decimal(99999), 0),
    "max_load_kg": ("Нагрузка, кг", Decimal(9999), 0),
}


def check_model_specs(data: Mapping[str, Any]) -> Check:
    """Числа характеристик модели из формы. Пустое поле - «не знаем»
    (None), а не ноль. «25 кг», бесконечность и число сверх колонки -
    отказ с названием поля: раньше Decimal("25 кг") ронял сохранение 500,
    а целое сверх integer - запрос в базу."""
    out: dict[str, Any] = {}
    for name, (what, most, places) in MODEL_SPEC_NUMBERS.items():
        text = str(data.get(name) or "").strip().replace(",", ".")
        if not text:
            out[name] = None
            continue
        try:
            value = Decimal(text)
        except (InvalidOperation, ValueError):
            return Check(False, error=f"{what}: только число.")
        if not value.is_finite() or not Decimal(0) <= value <= most:
            return Check(False, error=f"{what}: число от 0 до {most}.")
        if places == 0:
            if value != value.to_integral_value():
                return Check(False, error=f"{what}: целое число.")
            out[name] = int(value)
            continue
        # Округление - после предела: 9999,999 округлилось бы за колонку.
        value = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        if value > most:
            return Check(False, error=f"{what}: число от 0 до {most}.")
        out[name] = value
    return Check(True, out)


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


def doc_next_number(kind: str, filenames: Iterable[Any]) -> int:
    """Номер следующего файла вида: больший из занятых плюс один.

    Не «строк плюс один»: архивную строку удаляют, и такой номер
    совпадал с именем живого файла - загрузка затирала чужой шаблон,
    а удаление той строки потом стирало включённый."""
    pattern = re.compile(rf"{re.escape(kind)}-([0-9]+){re.escape(DOC_SUFFIX)}")
    taken = [int(m.group(1)) for name in filenames
             if (m := pattern.fullmatch(str(name or "")))]
    return max(taken, default=0) + 1


def mark_filename(kind: str) -> str:
    return f"{kind}.png"


# ─────────────────────────── точки выдачи ───────────────────────────

# Точка без города: в базе город обязателен, но старые строки и импорт
# могли оставить его пустым - терять такую точку в списке нельзя.
CITY_UNKNOWN = "Без города"


def by_city(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Точки, сгруппированные по городу: город - уровень над пунктом.

    Своего часового пояса у города нет намеренно. Вся система живёт
    в одном (Europe/Moscow): сроки аренды, начисления, отчёты и журнал
    статусов считаются в нём, и второй пояс пришлось бы протащить через
    каждый из них. Появится город в другом поясе - это отдельная работа,
    а не колонка в справочнике.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        city = str(row.get("city") or "").strip() or CITY_UNKNOWN
        groups.setdefault(city, []).append(dict(row))
    return [{"city": city,
             "rows": groups[city],
             "open": sum(1 for r in groups[city] if r.get("active")),
             "total": len(groups[city])}
            for city in sorted(groups)]


# ─────────────────────── аналитика по точкам ───────────────────────
#
# По точке - те же три числа и те же формулы (fleet_metrics), только
# ограниченные точкой: дни парка - по журналу мест, деньги - по точке
# аренды записи журнала. Ключ - имя точки, None - «без точки»: у этой
# корзины своя строка, иначе сумма по точкам не сошлась бы с общим
# числом панели, и отчёту перестали бы верить.

NO_POINT_TITLE = "без точки"

# Числа строки отчёта, которые складываются в «Итого». Штуки - целые,
# остальное - деньги.
POINT_COUNTS = ("issued", "first_periods", "renewals", "active", "debtors",
                "orders", "client_orders", "repairs")
POINT_MONEY = ("paid", "charged", "charged_fines", "bonus", "refunded", "debt",
               "cash", "service_revenue", "service_cost", "parts_cost", "repair_cost")


def _span_days(delta: timedelta) -> Decimal:
    """Сутки интервала точно, через микросекунды: куски по точкам обязаны
    складываться в целое, а float терял бы на каждом доли секунды."""
    micro = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    return Decimal(micro) / Decimal(86_400_000_000)


def days_by_status_location(status_log: Iterable[Mapping[str, Any]],
                            location_log: Iterable[Mapping[str, Any]],
                            since: datetime, until: datetime
                            ) -> dict[str | None, dict[str, Decimal]]:
    """Велосипеде-дни по точке и статусу за [since, until) - пересечение
    журнала статусов с журналом мест. Зеркало CrmDB.bike_days_by_location.

    Точка - та, где велосипед стоял в каждый момент интервала статуса:
    переезд свободного велосипеда без смены статуса делит его простой
    между точками. Первая строка журнала мест тянется в прошлое - статусы
    бывают старше истории мест, а точка до её начала та, что стояла в
    карточке. Велосипед без журнала мест целиком «без точки» (None).
    Сумма по точкам равна days_by_status при любом журнале.
    """
    places: dict[Any, list[Mapping[str, Any]]] = {}
    for row in location_log:
        places.setdefault(row["bike_id"], []).append(row)
    spans: dict[Any, list[tuple]] = {}
    for bike_id, rows in places.items():
        rows.sort(key=lambda r: (r["changed_at"], r.get("id") or 0))
        # (с, по, точка); None в «с» - с начала времён, в «по» - доныне.
        spans[bike_id] = [
            (None if i == 0 else row["changed_at"],
             rows[i + 1]["changed_at"] if i + 1 < len(rows) else None,
             row.get("to_location") or None)
            for i, row in enumerate(rows)]
    by_bike: dict[Any, list[Mapping[str, Any]]] = {}
    for row in status_log:
        by_bike.setdefault(row["bike_id"], []).append(row)
    out: dict[str | None, dict[str, Decimal]] = {}
    for bike_id, rows in by_bike.items():
        rows.sort(key=lambda r: (r["changed_at"], r.get("id") or 0))
        where = spans.get(bike_id) or [(None, None, None)]
        for i, row in enumerate(rows):
            start = max(row["changed_at"], since)
            end = min(rows[i + 1]["changed_at"] if i + 1 < len(rows) else until, until)
            for began, ended, location in where:
                lo = start if began is None else max(start, began)
                hi = end if ended is None else min(end, ended)
                if hi <= lo:
                    continue
                cell = out.setdefault(location, {})
                cell[row["to_status"]] = (cell.get(row["to_status"], Decimal(0))
                                          + _span_days(hi - lo))
    return out


def history_starts(status_log: Iterable[Mapping[str, Any]],
                   location_log: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """С какого момента у сети и у каждой точки есть дни. Зеркало
    CrmDB.history_starts.

    status - первая строка журнала статусов: раньше неё велосипеде-дней
    нет вовсе. points - {точка или None: момент}: первая строка журнала
    мест с этой точкой; первая строка велосипеда тянется назад к началу
    его статусов, как в days_by_status_location, а велосипед без журнала
    мест - «без точки» с первого статуса. Месяц, в котором стоит момент,
    неполный (month_coverage).
    """
    first_status: dict[Any, datetime] = {}
    for row in status_log:
        at = row["changed_at"]
        if row["bike_id"] not in first_status or at < first_status[row["bike_id"]]:
            first_status[row["bike_id"]] = at
    points: dict[str | None, datetime] = {}

    def seen(key: str | None, at: datetime) -> None:
        if key not in points or at < points[key]:
            points[key] = at

    placed: set[Any] = set()
    for row in sorted(location_log, key=lambda r: (r["changed_at"], r.get("id") or 0)):
        at = row["changed_at"]
        if row["bike_id"] not in placed:
            placed.add(row["bike_id"])
            at = min(at, first_status.get(row["bike_id"], at))
        seen(row.get("to_location") or None, at)
    for bike_id, at in first_status.items():
        if bike_id not in placed:
            seen(None, at)
    return {"status": min(first_status.values(), default=None), "points": points}


def _point_row(key: str | None, place: Mapping[str, Any] | None, *,
               counts: Mapping[str, int], days: Mapping[str, Any],
               money: Mapping[str, Any], rentals: Mapping[str, Any],
               debt: Mapping[str, Any], cash: Any,
               service: Mapping[str, Any]) -> dict[str, Any]:
    paid = to_money(money.get("paid") or 0)
    row: dict[str, Any] = {
        "key": key, "id": place.get("id") if place else None, "total": False,
        "title": key if key is not None else NO_POINT_TITLE,
        "place": dict(place) if place else None,
        # Закрытая точка справочника: видна, пока у неё есть история.
        "closed": bool(place) and place.get("active", True) is False,
        # Имя в данных, которого нет в справочнике: старая запись или
        # точка, заведённая до справочника. Своей страницы у неё нет.
        "orphan": key is not None and place is None,
        "counts": dict(counts),
        "fleet": sum(int(counts.get(s, 0)) for s in OPERATIONAL_STATUSES),
        "days": {s: Decimal(str(v)) for s, v in days.items()},
        "metrics": fleet_metrics(days, paid),
    }
    for k in ("paid", "charged", "charged_fines", "bonus", "refunded"):
        row[k] = to_money(money.get(k) or 0)
    for k in ("issued", "first_periods", "renewals", "active"):
        row[k] = int(rentals.get(k) or 0)
    row["debtors"] = int(debt.get("clients") or 0)
    row["debt"] = to_money(debt.get("debt") or 0)
    row["cash"] = to_money(cash or 0)
    for k in ("orders", "client_orders", "repairs"):
        row[k] = int(service.get(k) or 0)
    row["service_revenue"] = to_money(service.get("revenue") or 0)
    row["service_cost"] = to_money(service.get("cost") or 0)
    row["parts_cost"] = to_money(service.get("parts_cost") or 0)
    row["repair_cost"] = to_money(service.get("repair_cost") or 0)
    return row


def _point_empty(row: Mapping[str, Any]) -> bool:
    return (not any(row["counts"].values()) and not any(row["days"].values())
            and not any(row[k] for k in POINT_COUNTS + POINT_MONEY))


def points_rows(locations: Iterable[Mapping[str, Any]], *,
                bikes: Iterable[Mapping[str, Any]],
                days: Mapping[str | None, Mapping[str, Any]],
                money: Mapping[str | None, Mapping[str, Any]],
                rentals: Mapping[str | None, Mapping[str, Any]] | None = None,
                debt: Mapping[str | None, Mapping[str, Any]] | None = None,
                cash: Mapping[str | None, Any] | None = None,
                service: Mapping[str | None, Mapping[str, Any]] | None = None
                ) -> dict[str, Any]:
    """Сравнение точек за период: строка на точку и «Итого».

    locations - справочник в его порядке (sort, name); bikes - парк
    сейчас (N и статусы по текущей точке); остальное - ответы
    CrmDB.*_by_location вида {точка или None: числа}. Три числа точки -
    та же fleet_metrics от её дней и её платежей.

    Точка справочника видна всегда, закрытая - только с данными: её
    история остаётся историей. Имя из данных, которого нет в справочнике,
    - своей строкой: терять его деньги нельзя. «Без точки» - последней и
    только если в ней что-то есть. «Итого» - сумма всех строк, то есть
    ровно общие числа панели: те же дни и те же платежи, только сложенные.
    """
    rentals, debt, cash, service = rentals or {}, debt or {}, cash or {}, service or {}
    counts: dict[str | None, dict[str, int]] = {}
    for b in bikes:
        cell = counts.setdefault(b.get("location") or None, {})
        cell[b["status"]] = cell.get(b["status"], 0) + 1
    directory = [dict(p) for p in locations if p.get("name")]
    known = {p["name"] for p in directory}
    found: set[str | None] = set()
    for source in (counts, days, money, rentals, debt, cash, service):
        found.update(source)
    orphans = sorted(k for k in found if k is not None and k not in known)

    def build(key: str | None, place: Mapping[str, Any] | None) -> dict[str, Any]:
        return _point_row(key, place, counts=counts.get(key) or {},
                          days=days.get(key) or {}, money=money.get(key) or {},
                          rentals=rentals.get(key) or {}, debt=debt.get(key) or {},
                          cash=cash.get(key), service=service.get(key) or {})

    rows = []
    for place in directory:
        row = build(place["name"], place)
        if place.get("active", True) or not _point_empty(row):
            rows.append(row)
    rows += [row for row in (build(k, None) for k in orphans) if not _point_empty(row)]
    none = build(None, None)
    if not _point_empty(none):
        rows.append(none)

    total_counts: dict[str, int] = {}
    total_days: dict[str, Decimal] = {}
    for row in rows:
        for s, n in row["counts"].items():
            total_counts[s] = total_counts.get(s, 0) + n
        for s, d in row["days"].items():
            total_days[s] = total_days.get(s, Decimal(0)) + d
    total: dict[str, Any] = {
        "key": None, "id": None, "total": True, "title": "Итого", "place": None,
        "closed": False, "orphan": False, "counts": total_counts,
        "fleet": sum(r["fleet"] for r in rows), "days": total_days}
    for k in POINT_COUNTS:
        total[k] = sum(r[k] for r in rows)
    for k in POINT_MONEY:
        total[k] = to_money(sum((r[k] for r in rows), Decimal(0)))
    total["metrics"] = fleet_metrics(total_days, total["paid"])
    return {"rows": rows, "total": total}


def points_history_from(settings: Mapping[str, Any], since: datetime) -> datetime | None:
    """Момент, с которого история мест настоящая, - если период отчёта
    начался раньше него. До внедрения точка велосипеда - та, что стояла
    в карточке в день внедрения, и отчёт обязан сказать об этом одной
    строкой, а не выдавать догадку за историю. None - сказать нечего."""
    try:
        start = datetime.fromisoformat(str(settings.get("points_history_since") or ""))
        return start if since < start else None
    except (TypeError, ValueError):
        return None


def point_months(months: Iterable[Mapping[str, Any]], key: str | None, *,
                 starts: Mapping[str | None, datetime] | None = None) -> list[dict]:
    """Три числа одной точки по месяцам. months - [{"month", "days",
    "money"}] с ответами bike_days_by_location и money_by_location за
    месяц: запрос на месяц один на все точки, а не по запросу на точку.

    С окном месяца (since, until из month_windows) у строки есть
    coverage - неполный ли месяц: текущий или тот, где точка открылась
    (`starts` - history_starts()["points"]).
    """
    out = []
    for m in months:
        days = (m.get("days") or {}).get(key) or {}
        paid = ((m.get("money") or {}).get(key) or {}).get("paid") or 0
        row = {"month": m["month"], **fleet_metrics(days, paid)}
        if m.get("since") is not None and m.get("until") is not None:
            row["coverage"] = month_coverage(m["since"], m["until"],
                                             start=(starts or {}).get(key))
        out.append(row)
    return out


def query_with_renamed(query: Any, key: str, old: str, new: str) -> str | None:
    """Сохранённый фильтр (строка запроса) после переименования точки:
    пара `key=old` становится `key=new`. None - такой пары нет.

    Значение в строке %-кодировано («location=%D0%9F…», пробел бывает и
    «+»), поэтому сравнивается разобранное, а не текст. Остальные пары
    остаются байт в байт: сортировка и страница фильтра - его дело.
    """
    parts, changed = [], False
    for part in str(query or "").split("&"):
        name, sep, value = part.partition("=")
        if sep and unquote_plus(name) == key and unquote_plus(value) == old:
            part, changed = f"{name}={quote(new, safe='')}", True
        parts.append(part)
    return "&".join(parts) if changed else None


# Слова адреса, которые ничего не различают: «ул.» и «д.» есть в каждом
# адресе, и «ул. Адоратского» от «Адоратского» не отличается ничем.
_PLACE_NOISE = frozenset({
    "г", "гор", "город", "ул", "улица", "д", "дом", "пр", "просп", "проспект",
    "пер", "переулок", "бульвар", "ш", "шоссе", "пл", "площадь", "наб",
    "набережная", "к", "корп", "корпус", "стр", "строение", "в", "на", "у", "и",
    "по", "рф", "россия", "точка", "пункт"})


def _place_words(raw: Any) -> list[str]:
    """Слова адреса без регистра, ё=е и знаков препинания; буква дома
    прилипает к номеру: «11 А» и «11А» - один дом."""
    text = str(raw or "").lower().replace("ё", "е")
    text = re.sub(r"(\d)\s+([^\W\d_])(?![^\W\d_])", r"\1\2", text)
    return re.findall(r"[^\W_]+", text)


def match_location(text: Any, locations: Iterable[Mapping[str, Any]]) -> str | None:
    """Точка справочника по свободному тексту - строке «адрес» формы сдачи:
    «Адоратского 15», «ул. Павлюхина, 97А».

    Сравниваются имя, вывеска (public_title) и адрес: без регистра, ё=е,
    без знаков препинания, без слов вроде «ул.» и без названия города -
    они есть в каждом адресе. Сперва точное совпадение - того же текста
    или тех же значимых слов («Адоратского 52» и «г. Казань, ул.
    Адоратского, 52» - одно), затем «всё значимое из имени или адреса
    есть в тексте» или «весь текст - часть имени или адреса». Точное
    выигрывает у нестрогого: иначе точка «Адоратского» мешала бы найти
    по её же адресу соседнюю «Адоратского-2». Две точки подошли одинаково
    или ни одной - None: точку возврата не угадывают, её лучше не
    тронуть. Закрытые точки не рассматриваются - вернуть велосипед туда
    нельзя.
    """
    said_words = _place_words(text)
    if not said_words:
        return None
    places = [p for p in locations
              if p.get("name") and p.get("active", True) is not False]
    noise = _PLACE_NOISE | {w for p in places for w in _place_words(p.get("city"))}

    def meaning(words: list[str]) -> frozenset[str]:
        return frozenset(w for w in words
                         if w not in noise and not (len(w) == 1 and w.isalpha()))

    said = meaning(said_words)
    exact: set[str] = set()
    loose: set[str] = set()
    for place in places:
        for field in ("name", "public_title", "address"):
            words = _place_words(place.get(field))
            if not words:
                continue
            own = meaning(words)
            # Сырые слова несут «г. Казань, ул.»: без сравнения значимых
            # слов точка не находилась по собственному адресу без города.
            if words == said_words or (own and own == said):
                exact.add(place["name"])
            if own and said and (own <= said or said <= own):
                loose.add(place["name"])
    for found in (exact, loose):
        if found:
            return next(iter(found)) if len(found) == 1 else None
    return None


# Период отчёта по точкам по умолчанию - те же 30 дней до этой минуты,
# что у трёх чисел на сводке: «Итого» отчёта обязано совпасть с ними, а
# при другом окне совпадение было бы случайным.
POINTS_PERIOD_DAYS = 30
# Длиннее графику по дням нечего сказать: столбики сливаются, а картину
# по месяцам даёт таблица трёх чисел ниже.
POINT_CHART_DAYS = 62


def report_period(params: Mapping[str, Any], *, now: datetime,
                  floor: date | None = None) -> dict[str, Any]:
    """Период отчёта по точкам: [start, end).

    По умолчанию - последние 30 дней до этой минуты. `month=ГГГГ-ММ` -
    календарный месяц (текущий - по эту минуту), `since`/`until` - свой
    интервал по дням включительно. Мусор в адресе - умолчание, а не
    ошибка: отчёт открывают по ссылке, и страница отказа ничего не даёт.
    `query` - хвост адреса, которым период едет в выгрузку и на страницу
    точки: там обязаны быть те же числа. `floor` - первый месяц истории
    (history_floor): «прошлого» раньше него нет.
    """
    tz, today = now.tzinfo, now.date()
    # «Прошлый месяц» у окна дней и своего интервала - тот, что перед текущим.
    this_month = month_bounds(today.replace(day=1), today=today, floor=floor)

    def midnight(day: date) -> datetime:
        return datetime.combine(day, datetime.min.time(), tzinfo=tz)

    month = str(params.get("month") or "").strip()
    if month:
        first = month_from(month, today=today)
        span = month_bounds(first, today=today, floor=floor)
        return {"kind": "month", "start": midnight(first),
                "end": min(midnight(span["next"]), now),
                "since": first, "until": span["today"], "key": span["key"],
                "prev_key": span["prev_key"], "next_key": span["next_key"],
                "query": f"month={span['key']}", "label": first.strftime("%m.%Y"),
                # Сутки периода и сколько из них прошло - для плана точек:
                # тот же ровный темп, что у плана месяца на сводке.
                "days": span["days"], "passed": span["passed"]}
    raw_since = str(params.get("since") or "").strip()
    raw_until = str(params.get("until") or "").strip()
    # report_day, а не check_date: год 1 или 9999 читается датой, но
    # «минус 29 дней» и «плюс сутки» на нём - OverflowError и 500.
    since = report_day(raw_since, today=today) if raw_since else None
    until = report_day(raw_until, today=today) if raw_until else None
    if (since or until) and (since is None or since.ok) and (until is None or until.ok):
        last = until.value if until else today
        first = since.value if since else last - timedelta(days=POINTS_PERIOD_DAYS - 1)
        first, last = min(first, last), max(first, last)
        return {"kind": "custom", "start": midnight(first),
                "end": midnight(last + timedelta(days=1)), "since": first, "until": last,
                "key": today.strftime("%Y-%m"), "prev_key": this_month["prev_key"],
                "next_key": None,
                "query": f"since={first.isoformat()}&until={last.isoformat()}",
                "label": f"{first:%d.%m.%Y} — {last:%d.%m.%Y}",
                "days": (last - first).days + 1, "passed": (last - first).days + 1}
    start = now - timedelta(days=POINTS_PERIOD_DAYS)
    return {"kind": "days", "start": start, "end": now, "since": start.date(),
            "until": today, "key": today.strftime("%Y-%m"),
            "prev_key": this_month["prev_key"], "next_key": None, "query": "",
            "label": f"последние {POINTS_PERIOD_DAYS} дней",
            "days": POINTS_PERIOD_DAYS, "passed": POINTS_PERIOD_DAYS}


def report_prev_span(span: Mapping[str, Any], *,
                     now: datetime | None = None) -> dict[str, Any]:
    """Прошлый период для колонки «прошлый» в «Главном».

    Сравнивается равное с равным. Идущий период (конец позже `now`) - с
    тем же прошедшим временем прошлого: сегодня до 15:00 - со вчера до
    15:00, сентябрь по 30-е 15:00 - с августом по 30-е 15:00, а не с
    целым: иначе суммы «падали» бы каждый день до конца периода.
    Закончившийся месяц - с целым прошлым месяцем (сентябрь против всего
    августа, март против всего февраля). Окно дней и свои даты - с таким
    же отрезком прямо перед ними.
    """
    start, end = span["start"], span["end"]
    now = now or end
    elapsed = min(end, now) - start
    if span.get("kind") == "month":
        first = span["since"]
        prev_first = (first - timedelta(days=1)).replace(day=1)
        prev_start = start.replace(year=prev_first.year, month=prev_first.month, day=1)
        prev_end = start if end < now else min(prev_start + elapsed, start)
    else:
        prev_start = start - (end - start)
        prev_end = prev_start + elapsed
    last = (prev_end - timedelta(seconds=1)).date()
    whole_month = span.get("kind") == "month" and prev_end == start
    label = (prev_start.strftime("%m.%Y") if whole_month
             else f"{prev_start:%d.%m} — {max(last, prev_start.date()):%d.%m}")
    return {"start": prev_start, "end": prev_end, "since": prev_start.date(),
            "until": max(last, prev_start.date()), "label": label,
            "days": span_days(prev_start, prev_end)}


def span_days(start: datetime, end: datetime) -> float:
    """Длина отрезка в сутках, дробно: делитель «парка в среднем» - месяц,
    который ещё идёт, делится на прошедшее, а не на все его дни."""
    return max((end - start).total_seconds() / 86400, 0.0)


# Строки «Главного»: код, подпись, что лучше (меньше/больше), чьё право.
HEADLINE_ROWS: tuple[tuple[str, str, str, str], ...] = (
    ("fleet", "Парк в работе, в среднем", "more", ""),
    ("idle", "Простой", "less", ""),
    ("check", "Средний чек в день", "more", "finance"),
    ("paid", "Поступило от клиентов", "more", "finance"),
    ("issued", "Выдач / продлений", "more", ""),
    ("clients", "Новых клиентов", "more", "clients"),
    ("orders", "Нарядов закрыто", "more", "service"),
    ("debt", "Долг клиентов сейчас", "less", "finance"),
    ("integrity", "Расхождений в учёте", "less", "bikes"),
)


def _headline_value(code: str, figures: Mapping[str, Any] | None) -> tuple[Any, str]:
    """(число для сравнения, строка для глаз) одной строки «Главного»."""
    if not figures:
        return None, "—"
    m = figures.get("metrics") or {}
    if code == "fleet":
        days = figures.get("days") or 0
        op = m.get("operational_days")
        if not days or op is None:
            return None, "—"
        value = int((Decimal(str(op)) / Decimal(str(days))).quantize(Decimal(1)))
        return value, f"{value} шт."
    if code == "idle":
        value = m.get("idle_percent")
        return value, "—" if value is None else f"{value} %"
    if code == "check":
        value = m.get("avg_check")
        return value, "—" if value is None else money(value)
    if code == "paid":
        value = m.get("revenue")
        return value, "—" if value is None else money(value)
    if code == "issued":
        issued, renewals = figures.get("issued"), figures.get("renewals")
        if issued is None:
            return None, "—"
        return issued, f"{issued} / {renewals or 0}"
    if code == "debt":
        value = figures.get("debt")
        if value is None:
            return None, "—"
        count = figures.get("debtors")
        return value, money(value) + (f" · {count} чел." if count else "")
    key = {"clients": "new_clients", "orders": "orders", "integrity": "integrity"}[code]
    value = figures.get(key)
    return value, "—" if value is None else str(value)


def report_headline(now: Mapping[str, Any], prev: Mapping[str, Any] | None, *,
                    can: Callable[[str], bool]) -> list[dict[str, Any]]:
    """Таблица «Главное» отчётов: строка - показатель, колонки - выбранный
    период, прошлый такой же и цель; стрелка - стало лучше или хуже.

    Строка есть, только если её число вообще посчитано (None в `now` -
    раздел закрыт или данных нет) и открыт её раздел (`can`). Долг и
    расхождения - «сейчас»: прошлого значения у них нет.
    """
    rows = []
    for code, title, better, section in HEADLINE_ROWS:
        if section and not can(section):
            continue
        value, shown = _headline_value(code, now)
        if code in ("clients", "orders", "integrity", "debt") and value is None:
            continue
        before, before_shown = _headline_value(code, prev)
        if code in ("debt", "integrity"):
            before, before_shown = None, ""
        trend = up = None
        if value is not None and before is not None and value != before:
            up = value > before
            trend = up == (better == "more")
        goal, ok = "", None
        if code == "idle":
            goal = f"< {IDLE_TARGET_PERCENT} %"
            ok = None if value is None else value < IDLE_TARGET_PERCENT
        elif code == "check":
            goal = money(CHECK_TARGET)
            ok = None if value is None else value >= CHECK_TARGET
        elif code in ("debt", "integrity") and value is not None:
            ok = not value
        rows.append({"code": code, "title": title, "now": shown, "prev": before_shown,
                     "goal": goal, "ok": ok, "better": trend, "up": up})
    return rows


def plan_month(span: Mapping[str, Any], *, today: date) -> dict[str, Any]:
    """Месяц плана на странице точки: выбранный в отчёте месяц, иначе
    текущий. План месячный, и у «30 дней» или своего интервала своего
    месяца нет - берётся тот, что идёт, как на сводке."""
    first = span["since"] if span.get("kind") == "month" else today.replace(day=1)
    return month_bounds(first, today=today)


def month_windows(now: datetime, count: int = 6) -> list[dict[str, Any]]:
    """Последние `count` календарных месяцев, текущий первым и по эту
    минуту - тем же шагом, что таблица трёх чисел в отчётах."""
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    out = []
    for _ in range(count):
        following = (first + timedelta(days=32)).replace(day=1)
        out.append({"month": first.date(), "since": first,
                    "until": min(following, now)})
        first = (first - timedelta(days=1)).replace(day=1)
    return out


def month_coverage(since: datetime, until: datetime, *,
                   start: datetime | None = None) -> dict[str, Any]:
    """Сколько суток месяца стоит за его тремя числами.

    Неполный месяц - текущий (идёт по эту минуту) или тот, где началась
    история: у сети - первая строка журнала статусов, у точки - начало
    её журнала мест (точка открылась). Формулы те же, но предоплата за
    неделю на четыре дня аренды даёт чек 600-770 ₽ на 4-е число, и без
    пометки его сравнивают с полными месяцами. `since` - полночь первого
    числа, `until` - конец окна (month_windows). days - календарные сутки
    в счёте, `from` - с какого дня, если история началась внутри месяца.
    Месяц целиком до начала истории - не «неполный», а пустой: прочерк
    в таблице говорит сам за себя.
    """
    tz = since.tzinfo
    following = (since + timedelta(days=32)).replace(day=1)
    whole = (following.date() - since.date()).days
    begin = max(since, start) if start is not None else since
    if until <= begin:
        return {"partial": False, "days": 0, "of": whole, "from": None,
                "current": False}

    def day_of(moment: datetime) -> date:
        # Журнал приходит из базы в UTC: сутки считаются по часам панели.
        return moment.astimezone(tz).date() if tz and moment.tzinfo else moment.date()

    first_day = day_of(begin)
    # Минус микросекунда: окно [since, until) до полуночи 1-го числа
    # следующего месяца не задевает его первые сутки.
    last_day = day_of(until - timedelta(microseconds=1))
    current = until < following
    return {"partial": begin > since or current,
            "days": (last_day - first_day).days + 1, "of": whole,
            "from": first_day if begin > since else None, "current": current}


def point_card(report: Mapping[str, Any], key: str | None,
               place: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Строка одной точки из отчёта points_rows. Закрытой точки без данных
    и пустой корзины «без точки» среди строк нет - тогда строка с нулями:
    страница точки открывается и так, пустой отчёт - тоже ответ."""
    for row in report.get("rows") or ():
        if row["key"] == key:
            return row
    return _point_row(key, place, counts={}, days={}, money={}, rentals={},
                      debt={}, cash=0, service={})


def points_plan(report: Mapping[str, Any], *, check: Any, days: int,
                passed: int) -> dict[str, Any]:
    """Отчёт points_rows с планом точек: у строки - план за период и
    выполнение тем же ровным темпом, что у плана месяца на сводке
    (plan_progress). План периода - план в день × сутки периода: за 30
    дней и за свой интервал период прошёл целиком, у текущего месяца -
    прошедшие сутки (report_period, days и passed).

    «Итого» плана - только по точкам с планом: выручка точки без плана
    ничей план не выполняет. planned - сколько точек с планом; ноль -
    колонок плана в отчёте нет вовсе.
    """
    rows, plans, paid = [], [], Decimal(0)
    for row in report.get("rows") or ():
        plan = point_plan(row.get("place"), check=check)
        progress = None
        if plan is not None:
            progress = {**plan, **plan_progress(plan, {"revenue": row["paid"]},
                                                days_in_month=days, days_passed=passed)}
            plans.append(plan)
            paid += to_money(row["paid"])
        rows.append({**row, "plan": progress})
    total = {**(report.get("total") or {}), "plan": None}
    if plans:
        both = sum_plans(plans, check=check)
        total["plan"] = {**both, **plan_progress(both, {"revenue": paid},
                                                 days_in_month=days, days_passed=passed)}
    return {**report, "rows": rows, "total": total, "planned": len(plans)}


def bikes_by_point(bikes: Iterable[Mapping[str, Any]]) -> dict[str | None, dict[str, int]]:
    """Сколько велосипедов числится на точке сейчас: операционный парк,
    из него в аренде (он стоит на точке аренды), и все карточки вместе с
    потерянными и проданными - столько строк тронет переименование."""
    out: dict[str | None, dict[str, int]] = {}
    for bike in bikes:
        cell = out.setdefault(bike.get("location") or None,
                              {"fleet": 0, "rented": 0, "cards": 0})
        cell["cards"] += 1
        if bike.get("status") in OPERATIONAL_STATUSES:
            cell["fleet"] += 1
        if bike.get("status") == "rented":
            cell["rented"] += 1
    return out


def map_places(locations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Точки выдачи на карте: действующие и с координатами. Своим слоем,
    а не ещё одной точкой трекера: по ним видно, далеко ли велосипед от
    точки, а спутать пункт с велосипедом значит поехать не туда."""
    out = []
    for place in locations:
        if place.get("active", True) is False or not place.get("name"):
            continue
        if not has_fix(place.get("lat"), place.get("lon")):
            continue
        lat, lon = float(place["lat"]), float(place["lon"])
        out.append({"lat": lat, "lon": lon, "name": str(place["name"]),
                    "title": str(place.get("public_title") or place["name"]),
                    "address": str(place.get("address") or ""),
                    "url": map_url(lat, lon)})
    return out


# ─────────────────────── стоимость склада по месяцам ───────────────────────

# Сколько месяцев показываем: год - это вся сезонность проката,
# от зимнего затишья до майского пика.
STOCK_CHART_MONTHS = 12


def stock_value_chart(rows: Iterable[Mapping[str, Any]], *,
                      months: int = STOCK_CHART_MONTHS,
                      today: date | None = None) -> dict[str, Any]:
    """Стоимость склада по месяцам: сколько денег лежит на полке.

    Считается накопительно от первого движения: склад - это остаток,
    а не оборот месяца. Месяц без движений из ряда не выпадает - в нём
    та же сумма, что и в прошлом, а дыра читалась бы как «склад исчез».

    Цена берётся та, что стояла в движении. Плитка «склад по
    себестоимости» считает сегодняшний остаток по СРЕДНЕЙ себестоимости,
    которую пересчитывает каждый приход, - поэтому последний столбик
    с ней может не сойтись, и это не ошибка ни того, ни другого.
    """
    today = today or date.today()
    moved: dict[date, Decimal] = {}
    for row in rows:
        month = row.get("month")
        if month is None:
            continue
        if isinstance(month, datetime):
            month = month.date()
        month = month.replace(day=1)
        moved[month] = moved.get(month, Decimal(0)) + to_money(row.get("value"))
    empty = {"months": [], "top": Decimal(0), "now": Decimal(0),
             "delta": Decimal(0), "peak": Decimal(0)}
    if not moved:
        return empty
    month, last = min(moved), today.replace(day=1)
    series: list[dict] = []
    total = Decimal(0)
    while month <= last:
        step = moved.get(month, Decimal(0))
        total += step
        series.append({"month": month, "value": total, "moved": step})
        month = (month + timedelta(days=32)).replace(day=1)
    shown = series[-months:] if months > 0 else series
    if not shown:
        return empty
    top = max([r["value"] for r in shown] + [Decimal(1)])
    for row in shown:
        # Отрицательный остаток бывает только при кривых данных - рисуем
        # его нулевым столбиком, но число показываем как есть.
        row["height"] = int(round(100 * max(row["value"], Decimal(0)) / top))
    started = shown[0]["value"] - shown[0]["moved"]
    return {"months": shown, "top": top, "now": shown[-1]["value"],
            "delta": shown[-1]["value"] - started,
            "peak": max(r["value"] for r in shown)}


# ─────────────────────────── список аренд ───────────────────────────

def rental_search(rows: Iterable[Mapping[str, Any]], q: str | None) -> list[dict]:
    """Аренды по строке поиска: клиент, телефон, номер велосипеда,
    номер договора, номер аренды.

    Цифры ищутся и в телефоне без форматирования: оператор набирает
    «9170» с экрана телефона, а в базе лежит «+7 917 …».
    """
    text = " ".join(str(q or "").lower().split())
    if not text:
        return [dict(r) for r in rows]
    digits = re.sub(r"\D", "", text)
    out = []
    for r in rows:
        hay = " ".join(str(r.get(k) or "") for k in
                       ("full_name", "bike_code", "bike_model", "contract_no")).lower()
        phone = re.sub(r"\D", "", str(r.get("phone") or ""))
        if (text in hay or str(r.get("id")) == text
                or (digits and (digits in phone or digits == str(r.get("id"))))):
            out.append(dict(r))
    return out


def rows_search(rows: Iterable[Mapping[str, Any]], q: str | None,
                keys: tuple[str, ...]) -> list[dict]:
    """Строка поиска по нескольким полям строки: подстрока без учёта
    регистра. Пусто - все строки. Возвращает копии: списки правятся
    дальше (сортировка, страницы), исходные строки трогать нельзя."""
    text = " ".join(str(q or "").lower().split())
    if not text:
        return [dict(r) for r in rows]
    out = []
    for r in rows:
        hay = " ".join(str(r.get(k) or "") for k in keys).lower()
        if text in hay:
            out.append(dict(r))
    return out


def rental_days(rental: Mapping[str, Any], *, today: date | None = None) -> int:
    """Сколько суток идёт (или шла) аренда: от выдачи по сегодня или
    по закрытие. Выдача сегодня - это 0, а не 1: сутки ещё не прошли."""
    started = rental.get("started_on")
    if started is None:
        return 0
    if isinstance(started, datetime):
        started = started.date()
    end = rental.get("closed_on") or today or date.today()
    if isinstance(end, datetime):
        end = end.date()
    return max((end - started).days, 0)


def overdue_days(summary: Mapping[str, Any] | None) -> int:
    """Дней просрочки по сводке аренды: 0, если долга по сроку нет."""
    if not summary or not summary.get("active"):
        return 0
    left = summary.get("days_left")
    return max(-int(left), 0) if left is not None else 0


# ─────────────────────────── акции ───────────────────────────
#
# Акция - правило, по которому клиент получает баллы. Шесть шаблонов
# в коде, параметры - в строке crm.promos. Скидка ложится в журнал видом
# bonus, как и остальные баллы: платежом она не становится никогда,
# иначе средний чек парка вырос бы на деньги, которых никто не вносил.
#
# Одна акция на одно начисление периода. Три шаблона про первый период
# (первая аренда, возвращение, промокод), три про каждый следующий
# (сезонная, долгая аренда, каждый N-й). Подходят две - берётся
# выгоднейшая для клиента, а не первая по списку.

PROMO_KINDS: dict[str, dict[str, Any]] = {
    "first": {
        "title": "Первая аренда",
        "hint": "Скидка на первый период клиенту, у которого аренд ещё не было.",
        "when": "первый период первой аренды",
        "defaults": {"percent": 10, "once_per_client": True},
        "params": {},
        "text": "Добро пожаловать! На первый период аренды - скидка {discount}.",
    },
    "comeback": {
        "title": "Возвращение",
        "hint": "Клиент без аренды дольше N дней берёт велосипед снова.",
        "when": "первый период после перерыва",
        "defaults": {"percent": 15, "once_per_client": True},
        "params": {"after_days": 30},
        "text": "С возвращением! На первый период - скидка {discount}.",
    },
    "promocode": {
        "title": "Промокод",
        "hint": "Код называют на выдаче: из объявления, листовки или от партнёра.",
        "when": "первый период, если назван код",
        "defaults": {"percent": 10, "once_per_client": True, "max_uses": 100},
        "params": {},
        "text": "Промокод {code} принят: скидка {discount} на первый период.",
    },
    "season": {
        "title": "Сезонная",
        "hint": "Скидка на каждый период, начисленный в окне дат акции. Можно "
                "ограничить моделью, точкой и только новыми арендами - так "
                "снимают простой.",
        "when": "каждый период в окне дат",
        "defaults": {"percent": 10, "once_per_client": False},
        "params": {},
        # Ограничение моделью и точкой аренды - ровно то, что нужно
        # скидке на простаивающие: отдельного шаблона под неё нет.
        "scope": True,
        "text": "Акция «{title}»: скидка {discount} на период аренды.",
    },
    "renewal": {
        "title": "Долгая аренда",
        "hint": "С N-го периода подряд - скидка на каждый следующий.",
        "when": "каждый период начиная с N-го",
        "defaults": {"percent": 10, "once_per_client": False},
        "params": {"from_period": 4},
        "text": "Вы с нами уже {period}-й период: скидка {discount}.",
    },
    "loyalty": {
        "title": "Каждый N-й период",
        "hint": "Каждый N-й период со скидкой: четвёртая неделя за полцены.",
        "when": "каждый N-й период",
        "defaults": {"percent": 50, "once_per_client": False},
        "params": {"every": 4},
        "text": "Каждый {every}-й период - со скидкой: вам начислено {discount}.",
    },
}
# Шаблоны про первый период: остальные три считают номер периода.
PROMO_FIRST_KINDS = frozenset({"first", "comeback", "promocode"})
PROMO_PARAM_LABELS: dict[str, str] = {
    "after_days": "Дней без аренды",
    "from_period": "С какого периода",
    "every": "Каждый N-й период",
}
# Границы параметров: год без аренды - уже не «возвращение», а новый
# клиент; 52 периода - год недельных.
PROMO_PARAM_RANGES: dict[str, tuple[int, int]] = {
    "after_days": (1, 365), "from_period": (2, 52), "every": (2, 52),
}
# Подстановки в тексте клиенту. {name} и {balance} - те же, что в
# рассылках, остальные - свои: мост в шаблон рассылки подставляет их сам.
PROMO_TEXT_FIELDS: dict[str, str] = {
    "discount": "скидка: «10 %» или «300 ₽»",
    "title": "название акции",
    "code": "промокод",
    "period": "номер периода",
    "every": "каждый N-й",
    "name": "имя клиента",
    "balance": "баланс",
}
PROMO_CODE_RE = re.compile(r"[A-ZА-ЯЁ0-9_-]{2,20}")
PROMO_TEXT_LIMIT = 1000


def clean_promo_code(raw: Any) -> str:
    """Код с формы или с выдачи: без пробелов, в верхнем регистре."""
    return re.sub(r"\s+", "", str(raw or "")).upper()


def promo_params(promo: Mapping[str, Any]) -> dict[str, int]:
    """Параметры акции поверх умолчаний шаблона: чужие ключи и мусор
    отбрасываются; строка вместо словаря (заглушка, старая запись)
    разбирается, а не роняет."""
    kind = str(promo.get("kind") or "")
    defaults = dict(PROMO_KINDS.get(kind, {}).get("params", {}))
    raw = promo.get("params")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    out: dict[str, int] = {}
    for key, default in defaults.items():
        try:
            value = int((raw or {}).get(key, default))
        except (TypeError, ValueError, AttributeError):
            value = int(default)
        low, high = PROMO_PARAM_RANGES.get(key, (1, 10**6))
        out[key] = value if low <= value <= high else int(default)
    return out


def _promo_raw_params(promo: Mapping[str, Any]) -> Mapping[str, Any]:
    """params акции словарём: jsonb приходит и строкой, мусор - пустой."""
    raw = promo.get("params")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    return raw if isinstance(raw, Mapping) else {}


def promo_scope(promo: Mapping[str, Any]) -> dict[str, str | None]:
    """Ограничение акции: модель велосипеда (название каталога) и точка
    аренды. None - любая. Лежит в params, рядом с числовыми параметрами:
    колонка ради двух необязательных строк не нужна."""
    raw = _promo_raw_params(promo)
    return {key: (str(raw.get(key) or "").strip() or None)
            for key in ("model", "location")}


def promo_new_only(promo: Mapping[str, Any]) -> bool:
    """«Только новые аренды»: скидка лишь на первый период выдачи. Без неё
    сезонная ложится и на продления идущих аренд, а простой снимает только
    новый курьер - продлевающий катался бы и без скидки."""
    return _promo_raw_params(promo).get("new_only") is True


def promo_scope_label(promo: Mapping[str, Any]) -> str:
    """«Monster Truck · Адоратского» - для списка и карточки; пусто - без
    ограничения."""
    scope = promo_scope(promo)
    return " · ".join(v for v in (scope["model"], scope["location"]) if v)


def promo_discount(promo: Mapping[str, Any], price: Any) -> Decimal:
    """Скидка с цены велосипеда за период: процент или сумма, не больше
    самой цены. Доп. аккумулятор - отдельная позиция, в скидку не входит."""
    price = to_money(price)
    if price <= 0:
        return Decimal(0)
    percent = promo.get("percent")
    if percent:
        return to_money(price * Decimal(int(percent)) / Decimal(100))
    amount = to_money(promo.get("amount"))
    return min(amount, price) if amount > 0 else Decimal(0)


def promo_discount_label(promo: Mapping[str, Any]) -> str:
    """«10 %» или «300 ₽» - для текста клиенту и списка."""
    if promo.get("percent"):
        return f"{int(promo['percent'])} %"
    return money(promo.get("amount"))


def promo_alive(promo: Mapping[str, Any], *, today: date) -> bool:
    """Действует ли акция сегодня: включена, в окне дат, предел не выбран."""
    if not promo.get("active"):
        return False
    starts = promo.get("starts_on")
    ends = promo.get("ends_on")
    if starts and today < starts:
        return False
    if ends and today > ends:
        return False
    max_uses = promo.get("max_uses")
    if max_uses is not None and int(promo.get("uses") or 0) >= int(max_uses):
        return False
    return True


def promo_fits(promo: Mapping[str, Any], ctx: Mapping[str, Any]) -> bool:
    """Подходит ли акция к этому начислению.

    `ctx`: period_index (1 - первый период аренды), period_from (его
    начало), today, code (промокод, названный на выдаче), previous_rentals
    (сколько аренд у клиента было до этой), last_closed_on (когда
    закрылась последняя из них), client_uses ({promo_id: сколько раз
    клиент уже получал эту акцию}), model (модель велосипеда аренды по
    каталогу) и location (точка аренды) - для акции с ограничением;
    «только новые аренды» (promo_new_only) смотрит на period_index.
    """
    kind = str(promo.get("kind") or "")
    if kind not in PROMO_KINDS:
        return False
    params = promo_params(promo)
    index = int(ctx.get("period_index") or 0)
    if promo.get("once_per_client"):
        used = (ctx.get("client_uses") or {}).get(promo.get("id"), 0)
        if int(used or 0) > 0:
            return False
    # Ограничение проверяется у любого шаблона, где оно записано: лишняя
    # скидка хуже недоданной, а аренда без велосипеда или точки под
    # ограничение не подходит - про неё нельзя сказать, что это та модель.
    scope = promo_scope(promo)
    if scope["model"] and scope["model"].casefold() != \
            str(ctx.get("model") or "").strip().casefold():
        return False
    if scope["location"] and scope["location"] != str(ctx.get("location") or "").strip():
        return False
    if promo_new_only(promo) and index != 1:
        return False
    if kind in PROMO_FIRST_KINDS and index != 1:
        return False
    if kind == "first":
        return int(ctx.get("previous_rentals") or 0) == 0
    if kind == "comeback":
        last = ctx.get("last_closed_on")
        if last is None or not int(ctx.get("previous_rentals") or 0):
            return False
        # Перерыв - до начала аренды, а не до дня начисления: выдача задним
        # числом не делает перерыв длиннее, чем он был.
        since = ctx.get("period_from") or ctx["today"]
        return (since - last).days >= params["after_days"]
    if kind == "promocode":
        code = clean_promo_code(ctx.get("code"))
        return bool(code) and code == clean_promo_code(promo.get("code"))
    if kind == "season":
        return True                      # окно дат проверил promo_alive
    if kind == "renewal":
        return index >= params["from_period"]
    if kind == "loyalty":
        return index > 0 and index % params["every"] == 0
    return False


def pick_promo(promos: Iterable[Mapping[str, Any]], ctx: Mapping[str, Any],
               price: Any) -> tuple[dict, Decimal] | None:
    """Одна акция на начисление: выгоднейшая для клиента, при равной
    скидке - заведённая раньше. None - ничего не подошло."""
    best: tuple[dict, Decimal] | None = None
    for promo in promos:
        if not promo_alive(promo, today=ctx["today"]) or not promo_fits(promo, ctx):
            continue
        discount = promo_discount(promo, price)
        if discount <= 0:
            continue
        if best is None or discount > best[1] or (
                discount == best[1] and int(promo["id"]) < int(best[0]["id"])):
            best = (dict(promo), discount)
    return best


def promo_stamp(promo: Mapping[str, Any] | None, discount: Any) -> str:
    """Что предпросмотр выдачи показал оператору: «акция:скидка», «-» - без
    акции. Едет скрытым полем шага 4 и сверяется с пересчётом при
    оформлении: точку, дату и код на шаге меняют без перезагрузки, а
    акция с ограничением от них зависит - клиенту назвали бы одну сумму,
    а в журнал легла бы другая."""
    if promo is None:
        return "-"
    return f"{int(promo['id'])}:{to_money(discount)}"


def rental_history(rentals: Iterable[Mapping[str, Any]],
                   rental_id: int | None) -> dict[str, Any]:
    """Что было у клиента до этой аренды: сколько аренд и когда закрылась
    последняя. Текущая аренда из счёта исключается."""
    previous = [r for r in rentals if rental_id is None or int(r["id"]) != int(rental_id)]
    closed_days: list[date] = []
    for r in previous:
        closed = r.get("closed_on")
        if isinstance(closed, datetime):
            closed_days.append(closed.date())
        elif isinstance(closed, date):
            closed_days.append(closed)
    return {"previous_rentals": len(previous),
            "last_closed_on": max(closed_days) if closed_days else None}


def promo_text(promo: Mapping[str, Any], *, discount: Any,
               period_index: int = 0, name: str = "",
               balance: Any = None) -> str:
    """Текст клиенту: свой из строки, иначе из шаблона. Неизвестная
    подстановка остаётся как есть - падать в момент отправки незачем."""
    kind = str(promo.get("kind") or "")
    body = str(promo.get("text") or "").strip() or PROMO_KINDS.get(kind, {}).get("text", "")
    values = {
        "discount": money(discount), "title": str(promo.get("title") or ""),
        "code": str(promo.get("code") or ""), "period": str(period_index or ""),
        "every": str(promo_params(promo).get("every", "")),
        "name": name, "balance": money(balance) if balance is not None else "",
    }
    return render_template(body, values)


def promo_mailing_body(promo: Mapping[str, Any]) -> str:
    """Текст акции как тело шаблона рассылки: свои подстановки уходят
    значениями, {name} и {balance} остаются рассылке."""
    body = str(promo.get("text") or "").strip() or \
        PROMO_KINDS.get(str(promo.get("kind") or ""), {}).get("text", "")
    values = {
        "discount": promo_discount_label(promo), "title": str(promo.get("title") or ""),
        "code": str(promo.get("code") or ""),
        "period": str(promo_params(promo).get("from_period", "")),
        "every": str(promo_params(promo).get("every", "")),
    }
    return render_template(body, values)


def check_promo_text(raw: Any) -> Check:
    text = str(raw or "").strip()
    if len(text) > PROMO_TEXT_LIMIT:
        return Check(False, error=f"Текст клиенту: длиннее {PROMO_TEXT_LIMIT} "
                                  "символов не уйдёт.")
    unknown = [f for f in re.findall(r"{([a-zA-Z_]+)}", text)
               if f not in PROMO_TEXT_FIELDS]
    if unknown:
        return Check(False, error=f"Текст клиенту: неизвестная подстановка "
                                  f"{{{unknown[0]}}}.")
    return Check(True, text or None)


def check_promo_form(data: Mapping[str, Any], *, kind: str | None = None,
                     models: Iterable[str] = (), places: Iterable[str] = ()) -> Check:
    """Форма акции целиком: возвращает словарь колонок или первую ошибку.

    Скидка - либо процент, либо сумма: обе сразу это спор, ни одной -
    пустая акция. Код нужен только промокоду, у остальных он отбрасывается,
    чтобы случайное слово в поле не сделало из сезонной акции промокод.
    Ограничение моделью и точкой - только у шаблона со `scope`, и только
    из списков `models` и `places`: у прочих поле отбрасывается тем же
    правилом, что и код. Там же галочка «только новые аренды» (new_only).
    """
    kind = str(kind or data.get("kind") or "").strip()
    if kind not in PROMO_KINDS:
        return Check(False, error="Шаблон акции: недопустимое значение.")
    title = check_name(data.get("title"), what="Название акции")
    if not title.ok:
        return title
    raw_percent = str(data.get("percent") or "").strip()
    raw_amount = str(data.get("amount") or "").strip()
    percent: int | None = None
    amount: Decimal | None = None
    if raw_percent and raw_amount not in ("", "0"):
        return Check(False, error="Скидка: либо процент, либо сумма, не обе.")
    if raw_percent:
        # parse_id, а не isdigit: на «²» int() падал, и форма отвечала 500.
        percent = parse_id(raw_percent)
        if percent is None or not 1 <= percent <= 100:
            return Check(False, error="Процент скидки: целое от 1 до 100.")
    elif raw_amount:
        got = check_amount(raw_amount)
        if not got.ok:
            return got
        amount = got.value
    else:
        return Check(False, error="Скидка: укажите процент или сумму.")
    code: str | None = None
    if kind == "promocode":
        code = clean_promo_code(data.get("code"))
        if not PROMO_CODE_RE.fullmatch(code):
            return Check(False, error="Промокод: буквы, цифры, дефис, от 2 до 20 "
                                      "символов.")
    params: dict[str, int] = {}
    for key, default in PROMO_KINDS[kind]["params"].items():
        raw = str(data.get(key) or "").strip() or str(default)
        low, high = PROMO_PARAM_RANGES[key]
        value = parse_id(raw)
        if value is None or not low <= value <= high:
            return Check(False, error=f"{PROMO_PARAM_LABELS[key]}: целое от {low} "
                                      f"до {high}.")
        params[key] = value
    if PROMO_KINDS[kind].get("scope"):
        for key, allowed, what in (("model", models, "Модель"),
                                   ("location", places, "Точка")):
            value = " ".join(str(data.get(key) or "").split())
            if not value:
                continue
            if value not in set(allowed):
                return Check(False, error=f"{what}: недопустимое значение.")
            params[key] = value
        # Флаг пишется только поднятым: у старых акций ключа нет, и они
        # продолжают ложиться на продления, как ложились.
        if data.get("new_only"):
            params["new_only"] = True
    starts = ends = None
    if str(data.get("starts_on") or "").strip():
        got = check_date(data.get("starts_on"))
        if not got.ok:
            return Check(False, error="Действует с: " + got.error)
        starts = got.value
    if str(data.get("ends_on") or "").strip():
        got = check_date(data.get("ends_on"))
        if not got.ok:
            return Check(False, error="Действует по: " + got.error)
        ends = got.value
    if starts and ends and ends < starts:
        return Check(False, error="Окно дат: «по» раньше, чем «с».")
    max_uses: int | None = None
    raw_max = str(data.get("max_uses") or "").strip()
    if raw_max:
        max_uses = parse_id(raw_max)
        if max_uses is None or not 1 <= max_uses <= 100000:
            return Check(False, error="Предел применений: целое от 1 до 100000, "
                                      "пусто - без предела.")
    text = check_promo_text(data.get("text"))
    if not text.ok:
        return text
    note = check_note(data.get("note"))
    if not note.ok:
        return note
    return Check(True, {
        "kind": kind, "title": title.value, "percent": percent, "amount": amount,
        "code": code, "params": params, "starts_on": starts, "ends_on": ends,
        "max_uses": max_uses, "once_per_client": bool(data.get("once_per_client")),
        "text": text.value, "note": note.value,
    })


def promo_form_defaults(kind: str) -> dict[str, Any]:
    """Заготовка формы по шаблону: с ней акция заводится в два клика."""
    spec = PROMO_KINDS.get(kind) or {}
    defaults = dict(spec.get("defaults", {}))
    return {
        "kind": kind, "title": spec.get("title", ""),
        "percent": defaults.get("percent"), "amount": None, "code": None,
        "params": dict(spec.get("params", {})),
        "starts_on": None, "ends_on": None,
        "max_uses": defaults.get("max_uses"),
        "once_per_client": bool(defaults.get("once_per_client", True)),
        "text": spec.get("text", ""), "note": None, "active": True,
    }


def promo_form_echo(data: Mapping[str, Any], kind: str) -> dict[str, Any]:
    """Форма новой акции, вернувшаяся с ошибкой: введённое, а не заготовка.

    Редирект на чистую форму терял модель, точку, срок и «один раз на
    клиента» заготовки из подсказки о простое: человек правил одно поле из
    ошибки и заводил бессрочную скидку на всю сеть. Значения - как есть,
    строками: проверит их check_promo_form при следующей отправке; дата,
    которую не разобрать, остаётся пустой - ошибка про неё уже на экране.
    """
    promo = promo_form_defaults(kind)
    spec = PROMO_KINDS.get(kind) or {}

    def text(key: str) -> str:
        return str(data.get(key) or "").strip()

    def day(key: str) -> date | None:
        got = check_date(data.get(key))
        return got.value if got.ok else None

    params: dict[str, Any] = {key: text(key) or default
                              for key, default in spec.get("params", {}).items()}
    if spec.get("scope"):
        params.update({key: " ".join(text(key).split()) for key in ("model", "location")
                       if text(key)})
        if data.get("new_only"):
            params["new_only"] = True
    promo.update(title=text("title"), percent=text("percent") or None,
                 amount=text("amount") or None, code=text("code") or None,
                 params=params, starts_on=day("starts_on"), ends_on=day("ends_on"),
                 max_uses=text("max_uses") or None,
                 once_per_client=bool(data.get("once_per_client")),
                 text=text("text"), note=text("note") or None)
    return promo


def promo_totals(promos: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Плитки раздела: сколько акций действует, сколько раз сработали
    и на какую сумму - по всем, включая выключенные: скидка, розданная
    закрытой акцией, никуда не делась."""
    rows = list(promos)
    uses = sum(int(p.get("uses") or 0) for p in rows)
    total = to_money(sum((to_money(p.get("total")) for p in rows), Decimal(0)))
    return {"active": sum(1 for p in rows if p.get("active")),
            "count": len(rows), "uses": uses, "total": total}


# ─────────────────── скидка на простаивающие ───────────────────
#
# Модель на точке стоит свободной дольше порога в среднем - сводка и
# страница точки предлагают сезонную акцию, ограниченную этой моделью и
# точкой. Предлагают, а не включают: скидка - это деньги, решает человек.
# Заведённая акция встаёт на место предложения.

IDLE_PROMO_KIND = "season"
IDLE_PROMO_DAYS = 7
IDLE_PROMO_PERCENT = 15
# Срок заготовки: две недели - два недельных периода, за них видно,
# сработала ли скидка; бессрочная скидка на модель - уже цена, а не акция.
IDLE_PROMO_LENGTH = 14
# В «Задачах на сегодня» - самые долгие: весь список живёт на странице точки.
IDLE_PROMO_TASKS = 3


def idle_promo_settings(settings: Mapping[str, Any] | None = None) -> dict[str, int]:
    """Порог простоя в днях и скидка заготовки в процентах: из настроек,
    иначе умолчания. Ноль не принимается ни там, ни там: «простаивает 0
    дней» - весь свободный парк, скидка 0 % - не акция."""
    settings = settings or {}

    def number(key: str, default: int, high: int) -> int:
        value = parse_id(settings.get(key))
        return value if value is not None and 1 <= value <= high else default

    return {"days": number("idle_promo_days", IDLE_PROMO_DAYS, 365),
            "percent": number("idle_promo_percent", IDLE_PROMO_PERCENT, 100)}


def idle_models(bikes: Iterable[Mapping[str, Any]], *,
                since: Mapping[int, datetime | None], now: datetime, days: int,
                moved: Mapping[int, datetime | None] | None = None,
                points: Iterable[str] | None = None,
                aliases: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Модели, которые на точке стоят свободными дольше `days` в среднем.

    Среднее - по свободным велосипедам этой модели на этой точке: один
    давно стоящий среди выдаваемых - это не простой модели. Дни - idle_days
    от позднего из двух моментов: смены статуса (`since`) и переезда
    (`moved`, журнал мест). Переброска статус не трогает, и без журнала
    мест только что привезённый велосипед «простаивал» бы на новой точке
    все дни, что стоял на старой: подсказка звала бы скидку туда, куда его
    повезли под спрос. «Стоят дольше всех» считает по-прежнему от статуса -
    там вопрос, сколько велосипед не зарабатывает, а не где. Подменный фонд
    не в счёт: его держат под замены нарочно, и скидка его не выдаст.
    Велосипед без журнала - ноль дней, так же как в «Стоят дольше всех».
    `points` - только эти точки; «не на точке» не попадает никогда: акцию
    на него не ограничить.
    """
    moved = moved or {}
    allowed = None if points is None else {str(p) for p in points}
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for bike in bikes:
        place = str(bike.get("location") or "").strip()
        if bike.get("status") != "available" or bike.get("spare") or not place:
            continue
        if allowed is not None and place not in allowed:
            continue
        model = catalogue_model(bike.get("model"), aliases)
        if not model:
            continue
        start = since.get(int(bike["id"]))
        arrived = moved.get(int(bike["id"]))
        if start is not None and arrived is not None:
            start = max(start, arrived)
        stood = idle_days(start, now=now) or 0
        groups.setdefault((place, model), {"location": place, "model": model,
                                           "bikes": []})["bikes"].append(
            {"id": int(bike["id"]), "code": bike.get("code"), "idle_days": stood})
    out = []
    for group in groups.values():
        stood = [b["idle_days"] for b in group["bikes"]]
        average = Decimal(sum(stood)) / Decimal(len(stood))
        if average < max(int(days), 1):
            continue
        group["bikes"].sort(key=lambda b: (-b["idle_days"], str(b["code"] or "")))
        out.append({**group, "count": len(stood), "days": int(average),
                    "max": max(stood)})
    out.sort(key=lambda r: (-r["days"], r["location"], r["model"]))
    return out


def idle_promo_url(row: Mapping[str, Any]) -> str:
    """Форма акции, заполненная под простой: модель, точка, сколько стоит."""
    return (f"/promos/new?kind={IDLE_PROMO_KIND}"
            f"&model={quote(str(row['model']), safe='')}"
            f"&location={quote(str(row['location']), safe='')}"
            f"&idle={int(row.get('days') or 0)}")


def idle_promo_rows(rows: Iterable[Mapping[str, Any]],
                    promos: Iterable[Mapping[str, Any]], *,
                    today: date) -> list[dict[str, Any]]:
    """К каждому простою - действующая акция, которая его уже закрывает:
    с ограничением этой моделью, этой точкой или обоими. Общая акция без
    ограничения не в счёт: она про всех, а не ответ на этот простой, и
    прятать за ней подсказку значило бы молчать о простое."""
    live = [p for p in promos
            if PROMO_KINDS.get(str(p.get("kind") or ""), {}).get("scope")
            and promo_alive(p, today=today)]
    out = []
    for row in rows:
        running = None
        for promo in live:
            scope = promo_scope(promo)
            if not (scope["model"] or scope["location"]):
                continue
            if scope["model"] and scope["model"].casefold() != str(row["model"]).casefold():
                continue
            if scope["location"] and scope["location"] != row["location"]:
                continue
            running = dict(promo)
            break
        out.append({**row, "promo": running, "url": idle_promo_url(row)})
    return out


def idle_promo_form(query: Mapping[str, Any], *, today: date, percent: int,
                    models: Iterable[str], places: Iterable[str]) -> dict[str, Any]:
    """Заготовка акции из подсказки о простое: сезонная, ограниченная моделью
    и точкой, только новые аренды, скидка из настроек, две недели с
    сегодня, один раз на клиента - скидка зовёт нового курьера, а не дарит
    тем, кто уже катается: без «только новые» каждая идущая аренда этой
    модели с этой точки получила бы её на ближайшем продлении, а простой
    от этого не меньше. Модель и точка - только из списков: адрес собирает
    кто угодно, чужое значение просто не подставляется. Ничего не заводит:
    кнопку «Завести» жмёт человек."""
    promo = promo_form_defaults(IDLE_PROMO_KIND)
    model = " ".join(str(query.get("model") or "").split())
    place = " ".join(str(query.get("location") or "").split())
    model = model if model in set(models) else ""
    place = place if place in set(places) else ""
    if not (model or place):
        return promo
    stood = parse_id(query.get("idle"))
    promo.update(
        title=" на ".join(v for v in (model, place) if v)[:NAME_LIMIT],
        percent=percent, starts_on=today,
        ends_on=today + timedelta(days=IDLE_PROMO_LENGTH - 1),
        once_per_client=True,
        params={**promo["params"], **({"model": model} if model else {}),
                **({"location": place} if place else {}), "new_only": True},
        note=(f"Простой {stood} дн.: предложено сводкой" if stood
              else "Простой: предложено сводкой"))
    return promo


# ─────────────────── заявки на аренду из кабинета ───────────────────
#
# Заявка - намерение, а не аренда: велосипед не бронируется и статус ему
# не меняется, иначе одна заявка «на завтра» держала бы простаивающий
# велосипед сутки. Оператор открывает мастер выдачи из заявки с готовыми
# полями, и заявка закрывается выдачей.

BOOKING_STATUSES: dict[str, str] = {
    "new": "Ждёт выдачи", "done": "Выдано", "cancelled": "Снята",
}
# На сколько дней вперёд клиент может записаться: дальше он всё равно
# не приедет, а прогноз освобождения на неделю уже не смотрит.
BOOKING_DAYS_AHEAD = 3


def booking_models(models: Iterable[Mapping[str, Any]], bikes: Iterable[Mapping[str, Any]],
                   *, aliases: Mapping[str, str] | None = None,
                   location: str | None = None) -> list[dict[str, Any]]:
    """Модели каталога для выбора в кабинете, со счётом свободных сейчас.

    Модель без свободных остаётся в списке: заявка на неё - это лист
    ожидания, и оператор увидит спрос, которого не покрыл.
    location - выбранная точка (имя из справочника): считаются свободные
    только на ней, иначе «свободно 2» звало бы на точку, где их нет. Без
    неё - весь парк: так при одной точке, где место могли и не вести.
    """
    free: dict[str, int] = {}
    for bike in bikes:
        if bike.get("status") != "available":
            continue
        if location is not None and bike.get("location") != location:
            continue
        title = catalogue_model(bike.get("model"), aliases)
        free[title] = free.get(title, 0) + 1
    out = []
    for model in models:
        if not model.get("active", True):
            continue
        title = str(model.get("title") or "")
        out.append({"id": int(model["id"]), "title": title, "free": free.get(title, 0)})
    out.sort(key=lambda m: (-m["free"], m["title"]))
    return out


def booking_points(locations: Iterable[Mapping[str, Any]],
                   models: Iterable[Mapping[str, Any]], bikes: Iterable[Mapping[str, Any]],
                   *, aliases: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Открытые точки для первого шага заявки, со счётом свободных на каждой.

    Счёт - сумма того, что на точке покажет шаг моделей: велосипед модели
    не из каталога выбрать нельзя, и обещать его незачем. Порядок - как
    в справочнике, а не по запасу: точку выбирают по месту, и список не
    должен переставляться от выдачи к выдаче.
    """
    models, bikes = list(models), list(bikes)
    return [{"id": int(loc["id"]), "title": str(loc.get("public_title") or loc["name"]),
             "free": sum(m["free"] for m in booking_models(
                 models, bikes, aliases=aliases, location=str(loc["name"])))}
            for loc in locations if loc.get("active", True)]


def booking_when(raw: Any, *, today: date) -> date | None:
    """День выдачи из кнопки: дата «ГГГГММДД». None - кнопка устарела,
    чужая или слишком далеко.

    В кнопке сама дата, а не «через сколько дней»: кнопку «Завтра, 25.09»,
    нажатую 25-го, смещение превращало в 26-е - клиент приходил за
    велосипедом, которого на этот день никто не ждал.
    """
    text = str(raw or "")
    if not re.fullmatch(r"\d{8}", text):
        return None
    try:
        day = date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None
    if not today <= day <= today + timedelta(days=BOOKING_DAYS_AHEAD):
        return None
    return day


def booking_line(booking: Mapping[str, Any]) -> str:
    """Одна строка о заявке: модель, тариф, точка, день."""
    parts = [str(booking.get("model") or "любая модель")]
    if booking.get("tariff_name"):
        parts.append(str(booking["tariff_name"]))
    if booking.get("location_title"):
        parts.append(str(booking["location_title"]))
    wanted = booking.get("wanted_on")
    if isinstance(wanted, date):
        parts.append(wanted.strftime("%d.%m.%Y"))
    return " · ".join(parts)


# ─────────────────────────── лист ожидания ───────────────────────────
#
# Заявка на модель, которой нет, - лист ожидания. Освободился велосипед
# этой модели на точке заявки - бот зовёт: сначала давние заявки, не
# больше N человек на велосипед, раз в сутки на заявку и только днём.
# Велосипед при этом НЕ бронируется: кто первым приедет, того и он.

# Сколько назад смотреть события «освободился». Сутки перекрывают ночь:
# велосипед, сданный в 21:00, утром ещё свободен, и звать к нему надо.
WAITLIST_LOOKBACK = timedelta(hours=24)
# Умолчания параметров уведомления «waitlist»: двое на велосипед - один
# может не ответить; с 9 до 18 - чтобы «приеду сегодня» успеть до закрытия.
WAITLIST_PER_BIKE = 2
WAITLIST_HOURS = (9, 18)
# Заявка, чей день прошёл больше трёх суток назад, уже не ожидание, а
# забытая строка: клиент не пришёл, а снять её было некому. Звать её к
# каждому освободившемуся велосипеду - спам человеку, который давно
# передумал, и место в очереди впереди тех, кто подал заявку сегодня.
# Три дня - столько же, на сколько вперёд кабинет вообще принимает заявку.
WAITLIST_STALE_DAYS = 3


def notice_hours_ok(setting: Mapping[str, Any] | None, now: datetime,
                    default: tuple[int, int]) -> bool:
    """Час `now` внутри окна уведомления (from_hour, to_hour) - параметры
    в панели, иначе умолчание каталога. Час - местный."""
    start = notice_param(setting, "from_hour", default[0])
    end = notice_param(setting, "to_hour", default[1])
    return start <= now.hour < end


def waitlist_hours_ok(setting: Mapping[str, Any] | None, now: datetime) -> bool:
    """Днём ли сейчас: сообщение «освободился, приезжайте» в час ночи
    будит, а не зовёт. Окно - параметры уведомления, час - местный."""
    return notice_hours_ok(setting, now, WAITLIST_HOURS)


def waitlist_fits(bike: Mapping[str, Any], booking: Mapping[str, Any], *,
                  aliases: Mapping[str, str] | None = None) -> bool:
    """Велосипед годится заявке: свободен, та же модель (заводское имя
    парка сводится к названию каталога) и та же точка. Заявка без точки
    (точка была одна) ждёт на любой, без модели - любую."""
    if bike.get("status", "available") != "available":
        return False
    want = str(booking.get("model") or "").strip()
    if want and (catalogue_model(bike.get("model"), aliases)
                 != catalogue_model(want, aliases)):
        return False
    if booking.get("location_id") is not None:
        return bool(bike.get("location")) and bike.get("location") == booking.get(
            "location_name")
    return True


def _called_to(bike: Mapping[str, Any], booking: Mapping[str, Any]) -> bool:
    """Заявку уже звали к этому велосипеду после его освобождения."""
    freed = bike.get("freed_at")
    return (booking.get("waitlist_bike_id") == bike.get("id")
            and isinstance(booking.get("waitlist_at"), datetime)
            and (freed is None or booking["waitlist_at"] >= freed))


def waitlist_taken(bike: Mapping[str, Any], bookings: Iterable[Mapping[str, Any]]) -> int:
    """Сколько открытых заявок уже позвали к этому велосипеду с его
    освобождения. Снятая или выданная заявка место отдаёт следующему."""
    return sum(1 for b in bookings
               if b.get("status", "new") == "new" and _called_to(bike, b))


def waitlist_queue(bike: Mapping[str, Any], bookings: Iterable[Mapping[str, Any]], *,
                   today: date, aliases: Mapping[str, str] | None = None) -> list[dict]:
    """Кого звать к освободившемуся велосипеду - по очереди подачи.

    Заявка, поданная уже после освобождения, не ждала: клиент при подаче
    видел «свободно». Позванного сегодня (к этому или другому велосипеду)
    второй раз за день не зовём, а про этот же велосипед - не зовём вовсе:
    назавтра та же новость была бы уже спамом. Заявку, чей день прошёл
    больше WAITLIST_STALE_DAYS назад, не зовём: закрыть её забыли, а
    клиент давно передумал. Номер заявки растёт с подачей, поэтому
    очередь - по нему."""
    freed = bike.get("freed_at")
    oldest = today - timedelta(days=WAITLIST_STALE_DAYS)
    rows = [dict(b) for b in bookings
            if b.get("status", "new") == "new"
            and not (isinstance(b.get("wanted_on"), date) and b["wanted_on"] < oldest)
            and local_date(b.get("waitlist_at")) != today
            and not _called_to(bike, b)
            and (freed is None or not isinstance(b.get("created_at"), datetime)
                 or b["created_at"] < freed)
            and waitlist_fits(bike, b, aliases=aliases)]
    rows.sort(key=lambda b: int(b.get("id") or 0))
    return rows


def booking_served(booking: Mapping[str, Any], rentals: Iterable[Mapping[str, Any]]) -> bool:
    """Заявку уже закрыла аренда, заведённая после подачи. Выдача закрывает
    открытую заявку сама (service.open_rental), но старые данные и импорт
    могли оставить её «новой» - и лист ожидания позвал бы человека к
    велосипеду, который он только что сдал."""
    made = booking.get("created_at")
    return isinstance(made, datetime) and any(
        isinstance(r.get("created_at"), datetime) and r["created_at"] >= made
        for r in rentals)


def waitlist_coming_today(booking: Mapping[str, Any], *, today: date) -> bool:
    """Клиент нажал «Беру — приеду сегодня» на сегодняшнем зове: едет
    сегодня, какой бы день он ни выбрал в заявке. Ответ на прошлый зов
    к новому не относится - как и в строке waitlist_note."""
    at, coming = booking.get("waitlist_at"), booking.get("coming_at")
    return (isinstance(at, datetime) and isinstance(coming, datetime)
            and coming >= at and local_date(coming) == today)


def waitlist_note(booking: Mapping[str, Any], *, today: date) -> str:
    """Строка в списке заявок: «уведомлён 14:05 · ответил 14:12».

    «Не дошло» - сообщение не доставлено (бот заблокирован): место у
    велосипеда отдано следующему, а сегодня этого клиента больше не зовём.
    «Ответил» - только на последнее приглашение."""
    at = booking.get("waitlist_at")
    if not isinstance(at, datetime):
        return ""

    def when(moment: datetime) -> str:
        local = moment.astimezone() if moment.tzinfo else moment
        return local.strftime("%H:%M" if local.date() == today else "%d.%m %H:%M")

    line = ("уведомлён " if booking.get("waitlist_bike_id") else "не дошло ") + when(at)
    coming = booking.get("coming_at")
    if isinstance(coming, datetime) and coming >= at:
        line += " · ответил " + when(coming)
    return line


# ─────────────────────── сводка: задачи на сегодня ───────────────────────
#
# Один список вместо семи виджетов: оператор с утра должен видеть, кому
# звонить, кого искать, что чинить и кому выдавать, - и в каком порядке.
# Уровень «hot» - деньги или велосипед уходят сегодня; «warn» - завтра;
# «info» - работа есть, но не горит.

TASK_LEVELS = ("hot", "warn", "info")


def _names(rows: Iterable[Mapping[str, Any]], key: str = "full_name",
           limit: int = 3) -> list[str]:
    out: list[str] = []
    for row in rows:
        name = str(row.get(key) or "").strip()
        if name and name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


def today_tasks(*, expiring: Iterable[Mapping[str, Any]] = (),
                search: Mapping[str, Any] | None = None,
                orders: Iterable[Mapping[str, Any]] = (),
                claims: Iterable[Mapping[str, Any]] = (),
                bookings: Iterable[Mapping[str, Any]] = (),
                alerts: Iterable[Mapping[str, Any]] = (),
                transfers: Iterable[Mapping[str, Any]] = (),
                idle: Iterable[Mapping[str, Any]] = (),
                today: date | None = None,
                repair_norm: int = ORDER_STUCK_DAYS) -> list[dict[str, Any]]:
    """Задачи на сегодня по данным сводки. Пустые группы не показываются.

    `expiring` - строки виджета «истекает аренда» (с summary), `search` -
    результат search_rows, `orders` - открытые наряды, `bookings` - новые
    заявки, `alerts` - открытые тревоги, `transfers` - перевозки
    transfer_plan, `idle` - простои idle_promo_rows (самые долгие),
    `repair_norm` - общий срок ремонта (у узла наряда может быть свой).
    Порядок: горящее, потом завтрашнее, потом остальное; внутри уровня -
    по числу.
    """
    today = today or date.today()
    tasks: list[dict[str, Any]] = []

    def add(code: str, title: str, rows: list, url: str, level: str,
            key: str = "full_name") -> None:
        if rows:
            tasks.append({"code": code, "title": title, "count": len(rows),
                          "url": url, "level": level, "names": _names(rows, key)})

    expiring = list(expiring)
    overdue = [r for r in expiring
               if (r.get("summary") or {}).get("days_left") is not None
               and (r.get("summary") or {}).get("days_left") <= 0]
    soon = [r for r in expiring if r not in overdue]
    add("overdue", "Просрочка или платёж сегодня — позвонить", overdue, "/", "hot")
    add("soon", "Истекает на днях — напомнить", soon, "/", "warn")

    search = search or {}
    theft = [r for r in search.get("searching", []) if r.get("theft")]
    add("theft", "Пора признавать потерю", theft, "/rentals/search", "hot")
    add("search", "Кандидаты в розыск", list(search.get("candidates", [])),
        "/rentals/search", "hot")

    stuck = [o for o in orders if order_stuck(o, today=today, default=repair_norm)]
    add("orders", "Наряды дольше срока ремонта", stuck, "/orders?overdue=1",
        "warn", key="no")

    bookings = [b for b in bookings if b.get("status", "new") == "new"]
    # Сегодня - и заявка на завтра, если клиент ответил на сегодняшний
    # зов листа ожидания «приеду сегодня»: он уже в пути.
    due = [b for b in bookings if b.get("wanted_on") is None or b["wanted_on"] <= today
           or waitlist_coming_today(b, today=today)]
    later = [b for b in bookings if b not in due]
    add("bookings", "Заявки на выдачу сегодня", due, "/bookings", "hot")
    add("bookings_later", "Заявки на ближайшие дни", later, "/bookings", "info")

    add("claims", "Заявки на зачисление — сверить с банком", list(claims), "/claims",
        "warn")

    alerts = [a for a in alerts if a.get("state", "new") == "new"]
    urgent = [a for a in alerts if a.get("level") == "urgent"]
    yellow = [a for a in alerts if a not in urgent]
    add("alerts_urgent", "Срочные тревоги трекеров", urgent, "/alerts", "hot",
        key="bike_code")
    add("alerts", "Новые тревоги трекеров", yellow, "/alerts", "info", key="bike_code")

    # Перевозка и простой - по строке на предложение: у каждой свой адрес
    # (перевозка - на отчёт по точкам, простой - в готовую форму акции).
    for move in transfers:
        tasks.append({"code": "transfer", "title": "Перевезти " + move["label"],
                      "count": int(move["count"]), "url": "/reports/points#transfer",
                      "level": "warn", "names": []})
    for row in list(idle)[:IDLE_PROMO_TASKS]:
        promo = row.get("promo")
        head = f"{row['model']} на {row['location']} простаивает {row['days']} дн."
        tasks.append({
            "code": "idle_promo", "count": int(row["count"]), "level": "info",
            "title": (f"{head} — идёт акция «{promo['title']}»" if promo
                      else f"{head} — предложить скидку?"),
            "url": f"/promos/{promo['id']}" if promo else row["url"],
            # Предложение ведёт в форму акции: видно тому, кто её заведёт.
            "edit": promo is None,
            "names": _names(row.get("bikes") or [], "code")})

    order = {level: i for i, level in enumerate(TASK_LEVELS)}
    tasks.sort(key=lambda t: (order[t["level"]], -t["count"]))
    return tasks


# ─────────────────── операционная группа ───────────────────
#
# Рабочая группа точек с темами: фиксация выдачи, сдача, проверка долга,
# поиск трекера, итоги дня сервиса. Раньше её читал сценарий n8n и
# переписывал сообщения в Google-таблицу «Действующие арендаторы». Теперь
# сообщения читает бот и сверяет их с базой: кто на каком велосипеде,
# CRM знает сама, и сообщение в группе - проверка, что точка и база
# говорят одно и то же. 👍 - совпало, 👎 - нет, и ответом сказано, что.

OPS_KINDS: dict[str, str] = {
    "fix": "Фиксация выдачи", "swap": "Замена велосипеда",
    "return": "Сдача", "daily": "Итоги дня сервиса",
}
# Кириллица, похожая на латиницу: номер набирают с телефона, и раскладку
# не переключают. «В» - особый случай: в «60В240W» это «вольт», то есть
# латинская V, а на глаз - латинская B. Поэтому В, B и V в номере -
# один символ: два велосипеда, чьи номера разнятся только этим, в парке
# не встречаются, а «60В» с телефона находится.
VIN_LOOKALIKE_FROM = "АВСЕНКМОРТХУавсенкмортхуB"
VIN_LOOKALIKE_TO = "AVCEHKMOPTXYAVCEHKMOPTXYV"
_VIN_LOOKALIKE = str.maketrans(VIN_LOOKALIKE_FROM, VIN_LOOKALIKE_TO)
# Короче - это уже не номер. Два знака - чтобы находился и код парка
# «B-1»: совпадение только точное, а на реплики без совпадения бот молчит.
VIN_MIN = 2
OPS_VALUE_LIMIT = 300
OPS_KIT_LIMIT = 15
_OPS_BLANKS = frozenset({"", "-", "—", "–", "нет данных"})


def vin_key(raw: Any) -> str:
    """Номер рамы или мотора для сравнения: латиница, верхний регистр,
    без пробелов и дефисов. В базе сравнивается тем же выражением
    (`db.VIN_SQL`), иначе номер «60v240w 123» не нашёлся бы."""
    text = str(raw or "").upper().translate(_VIN_LOOKALIKE)
    return re.sub(r"[^0-9A-Z]", "", text)


def _ops_norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower().replace("ё", "е")).strip()


def ops_lines(text: Any) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _ops_pair(line: str) -> tuple[str, str] | None:
    """(ключ, значение) строки «ключ: значение». Номер пункта «1.» и
    маркер списка «-» к ключу не относятся."""
    if ":" not in line:
        return None
    key, _, value = line.partition(":")
    key = re.sub(r"^\s*(?:\d+\s*[.)]\s*|[-•–—]\s*)", "", key)
    return _ops_norm(key), value.strip()


def _ops_blank(value: str) -> str:
    value = value.strip()
    return "" if _ops_norm(value) in _OPS_BLANKS else value[:OPS_VALUE_LIMIT]


def ops_value(lines: Sequence[str], pattern: str, *, multiline: bool = False) -> str:
    """Значение первой строки, чей КЛЮЧ подходит под шаблон.

    Ищется по ключу, а не по всей строке, как делал n8n: иначе «рама»
    находилась бы в «Реф.программа», а адрес - в любом тексте со словом
    «адрес». multiline - следующие строки без двоеточия и без номера
    пункта считаются продолжением (повреждения пишут абзацем).
    """
    for i, line in enumerate(lines):
        pair = _ops_pair(line)
        if pair is None or not re.search(pattern, pair[0]):
            continue
        value = pair[1]
        if multiline:
            parts = [value] if value else []
            for nxt in lines[i + 1:]:
                if ":" in nxt or re.match(r"^(?:\d+\s*[.)]|[-•–—])", nxt):
                    break
                parts.append(nxt)
            value = " ".join(parts)
        return _ops_blank(value)
    return ""


def ops_money(raw: Any) -> Decimal | None:
    """Сумма из свободного текста: «1 500 наличными» -> 1500, «нет» -> 0.

    n8n выкидывал из строки всё, кроме цифр, и «1500.50» становилось
    150050. Здесь берётся первое число целиком. None - суммы в строке нет.
    """
    text = _ops_norm(raw)
    if text in _OPS_BLANKS or text in ("0", "нет", "не платил", "ничего"):
        return Decimal("0.00")
    match = re.search(r"\d[\d  ]*(?:[.,]\d{1,2})?", text)
    if not match:
        return None
    return parse_money(match.group().strip())


def _ops_phones(lines: Sequence[str]) -> list[str]:
    phones: list[str] = []
    for pattern in (r"основн", r"телефон\w*\s*2|дополнит", r"телефон\w*\s*3"):
        phone = bot_logic.normalize_phone(ops_value(lines, pattern))
        if phone and phone not in phones:
            phones.append(phone)
    return phones


def _ops_kit(lines: Sequence[str]) -> dict[str, str]:
    """Комплектация: строки «- АКБ: 2» под пунктом «4. Комплектация»."""
    kit: dict[str, str] = {}
    for line in lines:
        if not re.match(r"^[-•–—]", line):
            continue
        pair = _ops_pair(line)
        if pair is None:
            continue
        name = re.sub(r"^[-•–—]\s*", "", line.partition(":")[0]).strip()[:60]
        if name and len(kit) < OPS_KIT_LIMIT:
            kit[name] = _ops_blank(pair[1])[:40]
    return kit


def is_ops_fix(text: Any) -> bool:
    """Форма фиксации: начинается с «1. ФИО:» - как у фильтра n8n."""
    lines = ops_lines(text)
    return bool(lines) and bool(re.match(r"^1\s*[.)]\s*фио\s*:", _ops_norm(lines[0])))


def is_ops_swap(text: Any) -> bool:
    lines = ops_lines(text)
    return bool(lines) and _ops_norm(lines[0]).startswith("замена")


def parse_ops_fix(text: Any) -> tuple[dict | None, str]:
    """Форма фиксации выдачи (`bot_logic.fixation_form`, заполненная на точке).

    Адреса прописки и проживания НЕ разбираются и не хранятся: анкета
    лежит в базе зашифрованной, и класть те же адреса открытым текстом
    в журнал группы значило бы обойти шифрование. Телефоны нужны только
    для сверки с чёрным списком и в отчёт не пишутся.
    """
    lines = ops_lines(text)
    data = {
        "fio": ops_value(lines, r"\bфио\b"),
        "vin_frame": ops_value(lines, r"\bрам[аы]\b"),
        "vin_motor": ops_value(lines, r"\bмотор"),
        "rent_term": ops_value(lines, r"\bсрок"),
        "payment": ops_value(lines, r"\bсумма\b|\bоплат"),
        "telegram": ops_value(lines, r"\bник\b|telegram"),
        "gps": ops_value(lines, r"трекер|\bgps\b"),
        "given_by": ops_value(lines, r"\bвыдал"),
        "referral": ops_value(lines, r"\bреф"),
        "kit": _ops_kit(lines),
    }
    phones = _ops_phones(lines)
    if not vin_key(data["vin_motor"]) and not vin_key(data["vin_frame"]):
        return None, "В форме нет номера рамы или мотора - сверять не с чем."
    data["phones"] = phones
    return data, ""


SWAP_REASON_WORDS: tuple[tuple[str, str], ...] = (
    ("repair", r"слом|полом|ремонт|не\s*работ|неисправ|сгорел|\bдтп\b|авари|прокол"
               r"|спуст|барахл|глючит|не\s*едет|не\s*заряж|стуч|скрип|люфт|тормоз"),
    ("maintenance", r"(?:^|[^а-я])то(?:[^а-я]|$)|обслуж|планов"),
    ("client", r"просьб|попросил|хочет|захотел|клиент|модел|удобн"),
)


def swap_reason_code(text: Any) -> str:
    """Причина замены словами -> код `SWAP_REASONS`. Поломка проверяется
    первой: «клиент сломал» - это ремонт, а не просьба клиента, и снятый
    велосипед должен уйти в ремонт, а не обратно в выдачу."""
    norm = _ops_norm(text)
    for code, pattern in SWAP_REASON_WORDS:
        if re.search(pattern, norm):
            return code
    return "other"


def _ops_int(raw: str) -> int | None:
    digits = re.sub(r"\D", "", raw or "")
    return int(digits) if digits and len(digits) <= 7 else None


def parse_ops_swap(text: Any) -> tuple[dict | None, str]:
    """«ЗАМЕНА»: что было и что стало, по строке «стало:» посередине.

    n8n брал причину СЕДЬМОЙ строкой сообщения - лишняя пустая строка,
    и причиной становился номер рамы. Здесь причина - строка «причина:»,
    а без неё - первая строка без двоеточия.
    """
    lines = ops_lines(text)
    if not lines or not _ops_norm(lines[0]).startswith("замена"):
        return None, "Замена начинается со слова «ЗАМЕНА»."
    split = next((i for i, line in enumerate(lines)
                  if re.match(r"^стал[оа]\b", _ops_norm(line))), None)
    if split is None:
        return None, "Нет строки «стало:» - непонятно, что на что поменяли."
    before, after = lines[1:split], lines[split:]
    reason = ops_value(lines, r"причин")
    if not reason:
        free = [line for line in lines[1:]
                if ":" not in line and not re.match(r"^(?:был[оа]?|стал[оа])\b",
                                                    _ops_norm(line))]
        reason = free[0][:OPS_VALUE_LIMIT] if free else ""
    data = {
        "fio": ops_value(lines, r"\bфио\b"),
        "from_location": ops_value(lines, r"откуда"),
        "reason_text": reason,
        "reason": swap_reason_code(reason),
        "old_frame": ops_value(before, r"\bрам[аы]\b"),
        "old_motor": ops_value(before, r"\bмотор"),
        "new_frame": ops_value(after, r"\bрам[аы]\b"),
        "new_motor": ops_value(after, r"\bмотор"),
        "mileage_old": _ops_int(ops_value(before, r"пробег")),
        "mileage_new": _ops_int(ops_value(after, r"пробег")),
    }
    if not vin_key(data["old_motor"]) and not vin_key(data["old_frame"]):
        return None, "Не указан номер снятого велосипеда (до строки «стало:»)."
    if not vin_key(data["new_motor"]) and not vin_key(data["new_frame"]):
        return None, "Не указан номер нового велосипеда (после строки «стало:»)."
    return data, ""


def parse_ops_return(text: Any) -> tuple[dict | None, str]:
    """Отчёт о сдаче (`bot_logic.closure_report`, пересланный в тему).

    Суммы разбираются числом для сверки, но в журнал денег отсюда не
    идёт ничего: платёж проводит человек в панели, а текст в группе -
    не касса. Строки ищутся по ключу, так что «Кто принял велик» и
    «Кто принял вело» (оба написания живут в группе) читаются одинаково.
    """
    lines = ops_lines(text)
    data: dict[str, Any] = {
        "closed_at": ops_value(lines, r"\bкогда\b"),
        "debt_paid": ops_value(lines, r"\bдолг"),
        "damage": ops_value(lines, r"поврежд", multiline=True),
        "repair_paid": ops_value(lines, r"\bремонт"),
        "wash_paid": ops_value(lines, r"\bмойк"),
        "reason": ops_value(lines, r"причин", multiline=True),
        "return_address": ops_value(lines, r"\bадрес"),
        "accepted_by": ops_value(lines, r"\bпринял"),
        "review": ops_value(lines, r"\bотзыв"),
        "feedback": ops_value(lines, r"рекоменд", multiline=True),
        "fio": ops_value(lines, r"\bфио\b"),
        "vin_frame": ops_value(lines, r"\bрам[аы]\b"),
        "vin_motor": ops_value(lines, r"\bмотор"),
    }
    if not vin_key(data["vin_motor"]) and not vin_key(data["vin_frame"]):
        return None, "В отчёте нет номера рамы или мотора - сверять не с чем."
    for field in ("debt_paid", "repair_paid", "wash_paid"):
        amount = ops_money(data[field])
        data[field + "_sum"] = str(amount) if amount is not None else None
    return data, ""


OPS_DAILY_METRICS: tuple[tuple[str, str, str], ...] = (
    ("repair_start", r"начал\w* дня", "В ремонте на начало дня"),
    ("repaired_stock", r"склад", "Отремонтировано со склада"),
    ("repaired_clients", r"арендатор", "Отремонтировано у арендаторов"),
    ("repair_end", r"конц\w* дня|конец дня", "В ремонте на конец дня"),
    ("washed", r"помыт|мойк", "Помыто велосипедов"),
)
OPS_PARTS_LIMIT = 40


def _ops_line_number(lines: Sequence[str], pattern: str) -> int | None:
    """Первое число ПОСЛЕ ключевых слов: номер пункта «1.» стоит перед ними."""
    for line in lines:
        norm = _ops_norm(line)
        match = re.search(pattern, norm)
        if match is None:
            continue
        number = re.search(r"\d+", norm[match.end():])
        if number and len(number.group()) <= 5:
            return int(number.group())
    return None


def parse_ops_daily(text: Any) -> tuple[dict | None, str]:
    """Итоги дня сервиса: точка первой строкой, пункты 1-6 числами,
    в пункте 5 - израсходованные детали списком до пункта 6."""
    lines = ops_lines(text)
    if not lines:
        return None, "Пустой отчёт."
    data: dict[str, Any] = {"location": lines[0][:80]}
    for key, pattern, _ in OPS_DAILY_METRICS:
        data[key] = _ops_line_number(lines[1:], pattern)
    if all(data[key] is None for key, _, _ in OPS_DAILY_METRICS):
        return None, "Не похоже на итоги дня: нет ни одного числа по пунктам."
    parts: list[str] = []
    start = next((i for i, line in enumerate(lines) if "детал" in _ops_norm(line)), None)
    if start is not None:
        for line in lines[start + 1:]:
            if re.match(r"^\d+\s*[.)]", line):
                break
            if len(parts) < OPS_PARTS_LIMIT:
                parts.append(re.sub(r"^[-•–—]\s*", "", line)[:120])
    data["parts"] = parts
    return data, ""


def _name_tokens(name: Any) -> set[str]:
    return {t for t in re.split(r"[^a-zа-я]+", _ops_norm(name)) if len(t) >= 2}


def same_person(a: Any, b: Any) -> bool:
    """ФИО из формы и из карточки - про одного человека?

    Порядок слов и отчество не важны: на точке пишут «Иван Иванов», в
    договоре «Иванов Иван Иванович». Нужны два общих слова (или одно,
    если с одной стороны оно одно).
    """
    ta, tb = _name_tokens(a), _name_tokens(b)
    common = ta & tb
    return bool(common) and len(common) >= min(2, len(ta), len(tb))


def blacklist_hit(phones: Iterable[str], fio: Any,
                  clients: Iterable[Mapping[str, Any]]) -> tuple[dict, str] | None:
    """(клиент, «по чему совпало») среди НЕактивных карточек или None.

    n8n держал чёрный список пятью строками прямо в коде - и это были
    образцы, а не люди. Здесь список - карточки со статусом «чёрный
    список» и «заблокирован»: его ведут в панели, и бот видит правку сразу.
    Телефон сильнее ФИО: однофамильцев больше, чем общих номеров.
    """
    wanted = {p for p in (bot_logic.normalize_phone(x) for x in phones) if p}
    rows = [dict(c) for c in clients if c.get("status") != "active"]
    for client in rows:
        own = {bot_logic.normalize_phone(client.get(f)) for f in ("phone", "phone2", "phone3")}
        if wanted & (own - {None}):
            return client, "телефон"
    key = _ops_norm(fio)
    if key:
        for client in rows:
            if _ops_norm(client.get("full_name")) == key:
                return client, "ФИО"
    return None


def ops_blacklist_text(client: Mapping[str, Any], matched_by: str) -> str:
    status = CLIENT_STATUSES.get(client.get("status") or "", client.get("status") or "")
    note = (client.get("note") or "").strip()
    return ("⚠️ Клиент в стоп-листе CRM: " + html.escape(status, quote=False)
            + f"\nСовпадение по: {matched_by}"
            + f"\nКарточка: {html.escape(client.get('full_name') or '—', quote=False)}"
            + (f"\nЗаметка: {html.escape(note[:300], quote=False)}" if note else "")
            + "\nВыдачу не подтверждаю - решение за старшим смены.")


def _when(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "нет данных"
    local = moment.astimezone() if moment.tzinfo else moment
    minutes = int((now - moment).total_seconds() // 60) if moment.tzinfo else None
    ago = ""
    if minutes is not None and minutes >= 0:
        ago = (f" ({minutes} мин назад)" if minutes < 120
               else f" ({minutes // 60} ч назад)" if minutes < 48 * 60
               else f" ({minutes // 1440} дн. назад)")
    return local.strftime("%d.%m %H:%M") + ago


def _bike_head(bike: Mapping[str, Any]) -> str:
    parts = [f"Велосипед {html.escape(bike.get('code') or '—', quote=False)}"]
    if bike.get("model"):
        parts.append(html.escape(bike["model"], quote=False))
    if bike.get("motor_no"):
        parts.append("мотор " + html.escape(bike["motor_no"], quote=False))
    return " · ".join(parts)


OPS_DEBT_HINT = ("Нет данных или клиент спорит - пусть покажет чеки или платит "
                 "на месте, без расписок.")


def ops_debt_text(bike: Mapping[str, Any], rental: Mapping[str, Any] | None,
                  client: Mapping[str, Any] | None, bal: Any, *,
                  today: date) -> str:
    """Ответ в теме «проверка долга»: кто на велосипеде, сколько должен,
    до какого дня оплачено. Цифры - из журнала, а не из таблицы, которую
    кто-то забыл обновить."""
    status = BIKE_STATUSES.get(bike.get("status") or "", bike.get("status") or "")
    head = _bike_head(bike)
    if rental is None or client is None:
        return f"{head}\nАренды нет, статус: {status}."
    # Тот же расчёт, что в кабинете и карточке: у аренды с ручным
    # начислением долг в журнале нулевой, а платить за новый срок уже пора.
    summary = rental_summary(rental, bal, today=today)
    until = summary["covered_until"]
    paid = until.strftime("%d.%m.%Y") if until else "—"
    if summary["overdue"] and summary["days_left"] is not None:
        paid += f" (просрочка {-summary['days_left']} дн.)"
    lines = [
        head,
        f"Клиент: {html.escape(client.get('full_name') or '—', quote=False)}, "
        f"{html.escape(client.get('phone') or '—', quote=False)}",
        f"Тариф: {money(rental.get('price'))} за {int(rental.get('period_days') or 0)} дн.",
        f"Долг по журналу: {money(summary['debt'])}" if summary["debt"] > 0 else "Долга нет",
    ]
    if summary["due"] > summary["debt"]:
        lines.append(f"К оплате сейчас: {money(summary['due'])}")
    lines += [
        f"Оплачено до: {paid}",
        f"Статус велосипеда: {status}",
    ]
    if rental.get("search_at"):
        lines.append("🔎 В розыске")
    lines.append("")
    lines.append(OPS_DEBT_HINT)
    return "\n".join(lines)


OPS_GPS_HINT = ("Сфотографируйте номер на корпусе трекера и пришлите сюда. "
                "Нет номера SIM - позвоните с трекера на свой телефон.")


def ops_gps_text(bike: Mapping[str, Any], tracker: Mapping[str, Any] | None, *,
                 now: datetime) -> str:
    head = _bike_head(bike)
    if tracker is None:
        return f"{head}\nТрекер к велосипеду не привязан.\n\n{OPS_GPS_HINT}"
    name = tracker.get("alias") or tracker.get("device_id") or "—"
    lines = [
        head,
        f"Трекер: {html.escape(str(name), quote=False)}"
        + ("" if tracker.get("active", True) else " (снят с наблюдения)"),
        "SIM: " + html.escape(tracker.get("phone") or "номер не записан", quote=False),
        "На связи: " + _when(tracker.get("last_seen"), now),
    ]
    if tracker.get("voltage") is not None:
        lines.append(f"Питание: {tracker['voltage']} В")
    if tracker.get("blocked"):
        lines.append("⛔ Мотор заблокирован")
    url = map_url(tracker.get("lat"), tracker.get("lon"))
    lines.append(f"Карта: {url}" if url else "Координат нет")
    if not tracker.get("phone"):
        lines += ["", OPS_GPS_HINT]
    return "\n".join(lines)


def ops_report_summary(report: Mapping[str, Any]) -> str:
    """Одна строка о сообщении из группы - для журнала в панели."""
    data = report.get("data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = {}
    kind = report.get("kind")
    if kind == "daily":
        parts = [f"{label}: {data.get(key)}"
                 for key, _, label in OPS_DAILY_METRICS if data.get(key) is not None]
        return f"{data.get('location') or ''} · " + "; ".join(parts)
    if kind == "swap":
        return (f"{data.get('old_motor') or data.get('old_frame') or '—'} → "
                f"{data.get('new_motor') or data.get('new_frame') or '—'}"
                + (f" · {data.get('reason_text')}" if data.get("reason_text") else ""))
    if kind == "return":
        return " · ".join(x for x in (
            data.get("closed_at"), data.get("reason"),
            f"принял {data['accepted_by']}" if data.get("accepted_by") else "") if x)
    return " · ".join(x for x in (data.get("fio"), data.get("rent_term"),
                                  data.get("payment")) if x)


def ops_client_matches(phones: Iterable[str], fio: Any,
                       client: Mapping[str, Any]) -> bool:
    """Форма и карточка - один человек: общий телефон или то же ФИО."""
    own = {bot_logic.normalize_phone(client.get(f)) for f in ("phone", "phone2", "phone3")}
    wanted = {bot_logic.normalize_phone(p) for p in phones}
    if (own - {None}) & (wanted - {None}):
        return True
    return bool(_ops_norm(fio)) and same_person(fio, client.get("full_name"))


def looks_like_return(text: Any) -> bool:
    """Похоже на отчёт о сдаче, даже если номер забыли: тогда сказать
    об ошибке, а не промолчать, как на обычную реплику в теме."""
    return any((pair := _ops_pair(line)) is not None
               and re.search(r"когда сдал|принял|причина сдачи", pair[0])
               for line in ops_lines(text))


def ops_query(text: Any) -> str | None:
    """Запрос в теме долга или GPS: номер или телефон одной строкой.
    Реплика из нескольких слов - это разговор, на неё бот молчит."""
    lines = ops_lines(text)
    if len(lines) != 1 or len(lines[0].split()) > 3 or len(lines[0]) > 40:
        return None
    return lines[0]


# ─────────────────── входящие обращения ───────────────────
#
# Одна лента на всех, кто написал сам: Telegram, MAX, Авито, WhatsApp.
# Обращение - не клиент и не деньги: оно ничего не пишет в журнал и не
# создаёт аренд. Раздел по умолчанию только у встроенного «Владельца».

INBOX_CHANNELS: dict[str, str] = {
    "tg": "Telegram", "max": "MAX", "avito": "Авито", "wa": "WhatsApp",
    # Личный Telegram менеджера через Wazzup - не бот: отдельный канал, чтобы
    # хук не мог писать в разговоры бота (их ведёт только сам бот).
    "tgp": "Telegram (аккаунт)",
}
# Каналы, в которые отвечает Wazzup: канал «Входящих» -> chatType.
WAZZUP_CHAT_TYPES = {"wa": "whatsapp", "tgp": "telegram", "avito": "avito"}
INBOX_ORIGINS = ("bot", "max_bot", "avito_api", "hook")
INBOX_STATUSES: dict[str, str] = {
    "new": "Новое", "work": "В работе", "done": "Разобрано", "spam": "Спам",
}
INBOX_OPEN = ("new", "work")
INBOX_KINDS: dict[str, str] = {
    "text": "текст", "image": "фото", "voice": "голосовое", "file": "файл",
    "call": "звонок", "other": "вложение",
}
INBOX_OUT_STATUSES: dict[str, str] = {
    "queued": "в очереди", "sending": "отправляется", "sent": "отправлено",
    "failed": "не ушло",
}
# Переписка - ПДн: держим три месяца. Ответ на «что он писал» дальше не
# нужен, а срок хранения короче - меньше, что может утечь.
INBOX_KEEP_DAYS = 90
INBOX_TEXT_MAX = 4000
INBOX_NAME_MAX = 120
INBOX_SUBJECT_MAX = 200
# Длина ответа по каналу: у Авито жёсткий предел 1000 знаков, у Telegram
# и MAX запас под обёртку «Ответ оператора».
INBOX_REPLY_LIMITS: dict[str, int] = {"tg": 3500, "max": 3500, "avito": 1000, "wa": 3500}
# Хук: тело до 64 КиБ, до 50 сообщений за раз, после 20 неудачных
# токенов с адреса - пауза, как у входа в панель.
HOOK_MAX_BYTES = 64 * 1024
HOOK_BATCH_LIMIT = 200
HOOK_FAIL_LIMIT = 20
# Хук принимает только эти каналы: Telegram и MAX пишет сам бот, и чужой
# запрос с утёкшим токеном не должен заводить обращения на чужие tg_id.
HOOK_CHANNELS = ("avito", "wa", "tgp")
_TG_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}")


def inbox_no(thread_id: Any) -> str:
    return f"ВХ-{int(thread_id or 0):06d}"


def clean_text(value: Any) -> str:
    """Строка из чужого JSON - в то, что примут шифр и Postgres.

    NUL Postgres в text не принимает, а одиночный суррогат UTF-16 (эмодзи,
    обрезанный шлюзом посередине) роняет и шифрование, и кодек базы. Без
    чистки такая строка роняла бы пачку хука на каждой повторной доставке.
    """
    text = str(value or "").replace("\x00", "")
    return text.encode("utf-8", "replace").decode("utf-8")


def _cut(value: Any, limit: int) -> str | None:
    text = clean_text(value).strip()
    return text[:limit] if text else None


def safe_avito_url(url: Any) -> str | None:
    """Ссылка на объявление - только https на домен Авито. Чужой адрес
    или «javascript:» в карточку обращения не попадает."""
    text = str(url or "").strip()
    # Адрес объявления Авито - ASCII (кириллица в нём уже %-кодирована):
    # остальное - подделка или мусор, который уронил бы кодек базы.
    if not text or not text.isascii():
        return None
    # Обратную косую браузер читает как «/»: у «https://evil.com\.avito.ru»
    # urlsplit видит хост на avito.ru, а браузер уходит на evil.com.
    # Пробелы, управляющие символы и логин в адресе у ссылки на объявление
    # не встречаются - только у подделки.
    if "\\" in text or any(ch.isspace() or ord(ch) < 32 for ch in text):
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == "avito.ru" or host.endswith(".avito.ru")):
        return None
    if "@" in parts.netloc:
        return None
    return text[:500]


def inbox_links(thread: Mapping[str, Any]) -> dict[str, str | None]:
    """Куда ответить вне панели: t.me по @, wa.me по цифрам телефона."""
    username = str(thread.get("username") or "").lstrip("@")
    phone = inbox_phone(thread.get("channel"), thread.get("phone"))
    digits = re.sub(r"\D", "", phone or "")
    # Логин MAX - не логин Telegram: t.me по нему вёл бы к постороннему.
    is_tg = thread.get("channel") in (None, "tg", "tgp")
    return {
        "tg": (f"https://t.me/{username}"
               if is_tg and _TG_USERNAME.fullmatch(username) else None),
        "wa": f"https://wa.me/{digits}" if digits else None,
        "avito": safe_avito_url(thread.get("subject_url")),
    }


def inbox_can_reply(thread: Mapping[str, Any], *, avito_ok: bool,
                    wa: Mapping[str, Any] | None = None) -> tuple[bool, str]:
    """(можно ли ответить из панели, почему нет).

    В Telegram и MAX бот может написать только тому, кто сам писал боту:
    обращение, заведённое хуком, этого не доказывает. WhatsApp - через
    Wazzup (`wa` - wazzup_state), когда известно, с какого нашего номера
    отвечать; без Wazzup - вне панели.
    """
    channel = thread.get("channel")
    if thread.get("status") == "spam":
        return False, "Это спам - отвечать не нужно."
    if channel == "tg":
        if thread.get("origin") != "bot":
            return False, "В Telegram ответит только сам бот тем, кто писал ему."
        return True, ""
    if channel == "max":
        if thread.get("origin") != "max_bot":
            return False, "В MAX ответит только MAX-бот тем, кто писал ему."
        return True, ""
    if channel in ("avito", "tgp") and thread.get("origin") == "hook" \
            and wazzup_channel(thread.get("ext_channel")):
        # Пришло через Wazzup: ответ туда же, через тот же канал Wazzup.
        if not (wa and wa.get("live")):
            return False, "Wazzup не на связи - ответьте в приложении."
        if wazzup_pick_channel(thread.get("ext_channel"), wa.get("channels"),
                               kind=channel) is None:
            return False, "Канал Wazzup, через который писал человек, не найден."
        return True, ""
    if channel == "tgp":
        return False, "Ответьте в своём Telegram: этот чат пришёл без Wazzup."
    if channel == "avito":
        if thread.get("origin") != "avito_api":
            # Чат Авито из шлюза или n8n: его номер - номер шлюза, а не
            # чата Авито, и ответ через API ушёл бы в никуда.
            return False, ("Этот чат Авито пришёл через шлюз - ответьте в "
                           "приложении Авито или в самом шлюзе.")
        if not avito_ok:
            return False, ("Опрос Авито не работает - ответ уйдёт некуда. "
                           "Ответьте в приложении Авито.")
        return True, ""
    if channel == "wa" and wa and wa.get("live"):
        if not thread.get("phone") and not thread.get("ext_id"):
            return False, "У обращения нет телефона WhatsApp."
        if wazzup_pick_channel(thread.get("ext_channel"), wa.get("channels")) is None:
            return False, ("В Wazzup несколько номеров WhatsApp, а с какого писал "
                           "человек - неизвестно. Ответьте в WhatsApp и отметьте "
                           "«ответил вне панели».")
        return True, ""
    return False, ("WhatsApp: ответьте по ссылке wa.me и отметьте "
                   "«ответил вне панели».")


def check_inbox_reply(channel: Any, raw: Any) -> Check:
    # Форма шлёт перевод строки как CRLF, а maxlength браузера считает его
    # одним знаком: без нормализации разрешённый браузером ответ не проходил.
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return Check(False, error="Напишите текст ответа.")
    limit = INBOX_REPLY_LIMITS.get(str(channel), 3500)
    if len(text) > limit:
        return Check(False, error=f"Ответ длиннее {limit} знаков - сократите его.")
    return Check(True, text)


MOSCOW = ZoneInfo("Europe/Moscow")


def _moment(value: Any) -> datetime | None:
    """Время из чужого JSON: unix-секунды или ISO. Не разобрали - None.

    ISO без пояса - московское время: система живёт в Europe/Moscow, и
    n8n на том же сервере шлёт местное время, а не UTC.
    """
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return _plausible(datetime.fromtimestamp(float(value), UTC))
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()[:64]
    # isdigit верит и «²», и «①»: int() на них падает. Только ASCII-цифры.
    if text.isascii() and text.isdigit():
        return _moment(int(text))
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if not moment.tzinfo:
            moment = moment.replace(tzinfo=MOSCOW)
        return _plausible(moment.astimezone(UTC))
    except (ValueError, OverflowError):
        return None


def _plausible(moment: datetime) -> datetime | None:
    """«0001-01-01» (пустая дата .NET) и прочие века - не время сообщения:
    такое переполняется при переводе в UTC и роняло бы пачку хука."""
    return moment if 2000 <= moment.year <= 2100 else None


def wa_intl(raw: Any) -> str | None:
    """Номер из шлюза WhatsApp («84912345678@c.us», «79001234567») - уже
    международный: цифры с плюсом как есть. Восьмёрка впереди здесь - код
    страны (Вьетнам +84), а не «8» российского набора: перевод в +7 отдал
    бы ответ чужому человеку. Десять цифр с девятки - номер без кода РФ."""
    digits = re.sub(r"\D", "", str(raw or "").split("@", 1)[0])
    if 11 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    return None


def inbox_phone(channel: Any, phone: Any) -> str | None:
    """Телефон обращения. У WhatsApp номер с плюсом - международный из
    шлюза (wa_intl); набранное человеком (n8n, форма) - как везде."""
    text = str(phone or "").strip()
    if not text:
        return None
    if channel == "wa" and text.startswith("+"):
        return wa_intl(text)
    return bot_logic.normalize_phone(text)


def _wa_phone(raw: Any) -> str | None:
    """«79001234567@c.us» или «79001234567» -> +79001234567."""
    digits = re.sub(r"\D", "", str(raw or "").split("@", 1)[0])
    if not 10 <= len(digits) <= 15:
        return None
    return bot_logic.normalize_phone("+" + digits) or bot_logic.normalize_phone(digits)


def _inbound_item(channel: str, ext_id: Any, *, msg_id: Any = None, name: Any = None,
                  phone: Any = None, text: Any = None, kind: str = "text",
                  subject: Any = None, subject_url: Any = None,
                  at: Any = None, ext_channel: Any = None,
                  username: Any = None) -> dict | None:
    ext = _cut(ext_id, 100)
    if channel not in HOOK_CHANNELS or not ext:
        return None
    item = {
        "channel": channel, "ext_id": ext, "msg_id": _cut(msg_id, 100),
        "name": _cut(name, INBOX_NAME_MAX),
        "phone": inbox_phone(channel, phone),
        "text": _cut(text, INBOX_TEXT_MAX),
        "kind": kind if kind in INBOX_KINDS else "other",
        "subject": _cut(subject, INBOX_SUBJECT_MAX),
        "subject_url": safe_avito_url(subject_url),
        "at": _moment(at),
    }
    # Номер канала Wazzup: через какой наш WhatsApp, Telegram или Авито
    # писал человек - туда же уйдёт ответ.
    own = wazzup_channel(ext_channel) if channel in WAZZUP_CHAT_TYPES else None
    if own:
        item["ext_channel"] = own
    login = str(username or "").strip().lstrip("@")
    if channel == "tgp" and _TG_USERNAME.fullmatch(login):
        item["username"] = login
    return item


# Реакция, правка, удаление, голос в опросе - события к уже записанному
# сообщению, а не новое обращение: как сообщение они переоткрывали бы
# разобранное и слали сигнал в чат из-за 👍 на наш ответ.
_GREEN_SKIP = frozenset({"reactionMessage", "editedMessage", "deletedMessage",
                         "pollUpdateMessage"})
_GREEN_KINDS = {"textMessage": "text", "extendedTextMessage": "text",
                "quotedMessage": "text", "imageMessage": "image",
                "audioMessage": "voice", "documentMessage": "file",
                "videoMessage": "other", "stickerMessage": "other",
                "contactMessage": "other", "locationMessage": "other"}
_WAZZUP_KINDS = {"text": "text", "image": "image", "audio": "voice",
                 "document": "file", "missing_call": "call"}
_WAZZUP_CHANNELS = {"whatsapp": "wa", "whatsgroup": None, "avito": "avito",
                    "telegram": "tgp", "tgapi": "tgp", "telegroup": None}
_WAZZUP_CHANNEL_ID = re.compile(r"[A-Za-z0-9-]{1,64}")


def wazzup_channel(value: Any) -> str | None:
    """Номер канала Wazzup (uuid) из чужого JSON - или None."""
    text = str(value or "").strip()
    return text if _WAZZUP_CHANNEL_ID.fullmatch(text) else None


def _dict(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _green(payload: Mapping[str, Any]) -> tuple[list[dict], int]:
    """Вебхук Green-API. Берём только входящие личные сообщения и звонки:
    группы (@g.us), свои исходящие и смены состояния - не обращения."""
    kind_hook = payload.get("typeWebhook")
    sender = payload.get("senderData") or payload.get("from") or {}
    if kind_hook == "incomingCall":
        chat = str(payload.get("from") or "")
        phone = wa_intl(chat)
        item = _inbound_item("wa", phone, msg_id=payload.get("idMessage"), phone=phone,
                             kind="call", at=payload.get("timestamp"))
        return ([item], 0) if item else ([], 1)
    if kind_hook != "incomingMessageReceived" or not isinstance(sender, dict):
        return [], 1
    chat = str(sender.get("chatId") or "")
    if not chat.endswith("@c.us"):
        return [], 1
    # Вложенные части - только словари: строка вместо объекта от шлюза или
    # n8n - это сообщение без текста, а не падение хука.
    data = _dict(payload.get("messageData"))
    type_message = str(data.get("typeMessage") or "")
    if type_message in _GREEN_SKIP:
        return [], 1
    text = (_dict(data.get("textMessageData")).get("textMessage")
            or _dict(data.get("extendedTextMessageData")).get("text")
            or _dict(data.get("fileMessageData")).get("caption"))
    phone = wa_intl(chat)
    item = _inbound_item(
        "wa", phone, msg_id=payload.get("idMessage"),
        name=sender.get("senderName") or sender.get("chatName")
        or sender.get("senderContactName"),
        phone=phone, text=text, kind=_GREEN_KINDS.get(type_message, "other"),
        at=payload.get("timestamp"))
    return ([item], 0) if item else ([], 1)


def _wazzup(payload: Mapping[str, Any]) -> tuple[list[dict], int]:
    """Вебхук Wazzup: {messages: [...]}. isEcho - наше же исходящее.

    Сверх HOOK_BATCH_LIMIT сообщения не пишутся, но идут в счёт пропущенных:
    ответ 200 шлюз считает доставкой, и молча потерянное он не повторит.
    """
    items, skipped = [], 0
    messages = payload.get("messages") or []
    skipped += max(len(messages) - HOOK_BATCH_LIMIT, 0)
    for message in messages[:HOOK_BATCH_LIMIT]:
        if not isinstance(message, dict) or message.get("isEcho"):
            skipped += 1
            continue
        channel = _WAZZUP_CHANNELS.get(str(message.get("chatType") or ""))
        contact = message.get("contact") if isinstance(message.get("contact"), dict) else {}
        chat = message.get("chatId")
        if channel == "wa":
            phone = wa_intl(chat)
        elif channel == "tgp":
            # Телефон у контакта Telegram бывает, если человек его открыл.
            phone = contact.get("phone")
        else:
            phone = None
        item = _inbound_item(
            channel or "", phone if channel == "wa" else chat,
            msg_id=message.get("messageId"), name=contact.get("name"), phone=phone,
            text=message.get("text"),
            kind=_WAZZUP_KINDS.get(str(message.get("type") or ""), "other"),
            at=message.get("dateTime"), ext_channel=message.get("channelId"),
            username=contact.get("username"))
        if item is None:
            skipped += 1
        else:
            items.append(item)
    return items, skipped


def parse_inbound(payload: Any) -> tuple[list[dict], int]:
    """Тело хука -> (сообщения, сколько пропущено).

    Понимает три формы: наш нормализованный JSON (для n8n: один объект
    или {"items": [...]}), вебхук Green-API и вебхук Wazzup. Неизвестная
    форма - пусто, а не ошибка: шлюз шлёт и служебные уведомления.
    """
    if not isinstance(payload, dict):
        return [], 1
    if payload.get("test") is True and len(payload) <= 2:
        return [], 0                   # проверка адреса от Wazzup
    if "typeWebhook" in payload:
        return _green(payload)
    if "messages" in payload and isinstance(payload.get("messages"), list):
        return _wazzup(payload)
    raw_items = payload.get("items") if isinstance(payload.get("items"), list) else [payload]
    items, skipped = [], 0
    for raw in raw_items[:HOOK_BATCH_LIMIT]:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        channel = str(raw.get("channel") or "").strip().lower()
        phone = raw.get("phone")
        ext = raw.get("ext_id") or raw.get("from") or raw.get("chat_id")
        if channel == "wa" and not ext:
            ext = _wa_phone(phone)
        if channel == "wa":
            ext = _wa_phone(ext) or ext
            phone = phone or ext
        item = _inbound_item(
            channel, ext, msg_id=raw.get("msg_id") or raw.get("message_id") or raw.get("id"),
            name=raw.get("name"), phone=phone, text=raw.get("text"),
            kind=str(raw.get("kind") or "text"), subject=raw.get("subject"),
            subject_url=raw.get("subject_url"), at=raw.get("at"))
        if item is None:
            skipped += 1
        else:
            items.append(item)
    skipped += max(len(raw_items) - HOOK_BATCH_LIMIT, 0)
    return items, skipped


def inbox_team_text(thread: Mapping[str, Any]) -> str:
    """Сигнал в служебный чат. Без имени, телефона и текста: чат читают
    все, а переписка - ПДн и живёт в панели под ключом."""
    channel = INBOX_CHANNELS.get(str(thread.get("channel")), str(thread.get("channel")))
    line = f"📨 Новое обращение {inbox_no(thread.get('id'))} · {channel}"
    subject = thread.get("subject")
    if subject:
        line += f"\n{html.escape(str(subject)[:INBOX_SUBJECT_MAX], quote=False)}"
    return line + "\nОткройте раздел «Входящие» в панели."


def inbox_team_summary(threads: Iterable[Mapping[str, Any]]) -> str:
    """Сводка вместо пачки сигналов (первый опрос Авито, простой): сколько
    и откуда. Тоже без имён, телефонов и текста."""
    by_channel: dict[str, int] = {}
    total = 0
    for thread in threads:
        label = INBOX_CHANNELS.get(str(thread.get("channel")), str(thread.get("channel")))
        by_channel[label] = by_channel.get(label, 0) + 1
        total += 1
    parts = ", ".join(f"{label} — {n}" for label, n in sorted(by_channel.items()))
    return (f"📨 Новых обращений: {total} ({parts}).\n"
            "Откройте раздел «Входящие» в панели.")


def inbox_rows(threads: Iterable[Mapping[str, Any]], *,
               now: datetime | None = None) -> list[dict]:
    """Строки списка: номер, сколько ждёт, подписи."""
    now = now or datetime.now(UTC)
    rows = []
    for thread in threads:
        row = dict(thread)
        waiting = row.get("waiting_since")
        row["no"] = inbox_no(row.get("id"))
        row["waiting_hours"] = (
            max(int((now - waiting).total_seconds() // 3600), 0)
            if isinstance(waiting, datetime) and waiting.tzinfo else None)
        row["channel_label"] = INBOX_CHANNELS.get(row.get("channel"), row.get("channel"))
        row["status_label"] = INBOX_STATUSES.get(row.get("status"), row.get("status"))
        row["who"] = (row.get("client_name") or row.get("name")
                      or (f"@{row['username']}" if row.get("username") else None)
                      or row.get("phone") or row["no"])
        rows.append(row)
    return rows


def inbox_matches(row: Mapping[str, Any], query: Any) -> bool:
    q = str(query or "").strip().lower()
    if not q:
        return True
    hay = " ".join(str(row.get(k) or "") for k in (
        "name", "username", "phone", "subject", "client_name", "no", "ext_id")).lower()
    digits = re.sub(r"\D", "", q)
    return q in hay or (len(digits) >= 5 and digits in re.sub(r"\D", "", hay))


def inbox_counts(threads: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Плитки: сколько новых, в работе и ждут ответа дольше часа."""
    out = {"new": 0, "work": 0, "waiting": 0}
    now = datetime.now(UTC)
    for t in threads:
        status = t.get("status")
        if status in out:
            out[status] += 1
        waiting = t.get("waiting_since")
        if (status in INBOX_OPEN and isinstance(waiting, datetime) and waiting.tzinfo
                and (now - waiting) > timedelta(hours=1)):
            out["waiting"] += 1
    return out


# Состояние опроса Авито пишет процесс бота в crm.settings - панель в
# интернет не ходит и узнаёт о нём только так. Отметка старше этого
# срока - опрос не живой, и ответ в Авито из панели не принимается.
AVITO_STALE_MINUTES = 15


def avito_state(settings: Mapping[str, Any], *,
                now: datetime | None = None) -> dict[str, Any]:
    """{configured, ok, live, at, error} из settings.inbox_avito_state."""
    raw = settings.get("inbox_avito_state")
    data: dict[str, Any] = {}
    if isinstance(raw, dict):
        data = raw
    elif raw:
        try:
            loaded = json.loads(str(raw))
            data = loaded if isinstance(loaded, dict) else {}
        except ValueError:
            data = {}
    at = _moment(data.get("at"))
    now = now or datetime.now(UTC)
    # Опрос раз в полчаса - не мёртвый опрос: порог не меньше трёх кругов.
    every = data.get("every")
    every = int(every) if isinstance(every, (int, float)) and every > 0 else 0
    stale = max(timedelta(minutes=AVITO_STALE_MINUTES), timedelta(seconds=3 * every))
    live = bool(data.get("ok")) and at is not None and (now - at) <= stale
    return {"configured": bool(data), "ok": bool(data.get("ok")), "live": live,
            "at": at, "error": str(data.get("error") or "")[:300]}


# Wazzup: бот раз в час сверяет номера и подписку. Отметка старше трёх
# кругов - процесс бота не работает, и ответ из панели повиснет в очереди.
WAZZUP_STALE_HOURS = 3


def _json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw:
        try:
            loaded = json.loads(str(raw))
            return loaded if isinstance(loaded, dict) else {}
        except ValueError:
            return {}
    return {}


def wazzup_state(settings: Mapping[str, Any], *,
                 now: datetime | None = None) -> dict[str, Any]:
    """Состояние Wazzup из settings.inbox_wazzup_state (пишет процесс бота).

    ok - API ответил и ключ принят, live - и это было недавно; hook - адрес
    хука подписан (в настройке лежит отпечаток адреса, не сам адрес: в нём
    токен). channels - номера WhatsApp из кабинета Wazzup.
    """
    data = _json_dict(settings.get("inbox_wazzup_state"))
    at = _moment(data.get("at"))
    now = now or datetime.now(UTC)
    live = (bool(data.get("ok")) and at is not None
            and now - at <= timedelta(hours=WAZZUP_STALE_HOURS))
    channels = []
    for item in data.get("channels") if isinstance(data.get("channels"), list) else []:
        if isinstance(item, dict) and wazzup_channel(item.get("id")):
            channels.append({"id": wazzup_channel(item.get("id")),
                             # Отметка до Telegram и Авито знала только WhatsApp.
                             "kind": (item.get("kind") if item.get("kind")
                                      in WAZZUP_CHAT_TYPES else "wa"),
                             "phone": str(item.get("phone") or "")[:20] or None,
                             "state": str(item.get("state") or "")[:40],
                             "active": bool(item.get("active"))})
    return {"configured": bool(data), "ok": bool(data.get("ok")), "live": live, "at": at,
            "error": str(data.get("error") or "")[:300], "channels": channels,
            "hook": bool(data.get("hook")) and not data.get("hook_error"),
            "hook_error": str(data.get("hook_error") or "")[:300],
            "hooked_at": _moment(data.get("hooked_at"))}


def panel_host(domain: Any) -> str | None:
    """Домен панели из CRM_DOMAIN: без схемы и косых, строчными. Не похож
    на домен - None: по нему не собрать ни хук, ни кнопку бота."""
    host = str(domain or "").strip().lower()
    host = re.sub(r"^https?://", "", host).strip("/")
    if not host or not re.fullmatch(r"[a-z0-9.-]+(:\d+)?", host):
        return None
    return host


def panel_app_url(domain: Any) -> str | None:
    """Адрес панели для кнопки бота «Открыть CRM» (Telegram Mini App).

    Mini App Telegram открывает только по https, поэтому без домена панели
    (CRM_DOMAIN, профиль https) кнопки нет: голый адрес сервера по http
    Telegram не примет. Путь - корень: вошедшего панель ведёт на его
    стартовую страницу, остальных - на вход по логину и паролю."""
    host = panel_host(domain)
    return f"https://{host}/" if host else None


def wazzup_hook_url(domain: Any, token: Any) -> str | None:
    """Адрес хука для подписки Wazzup: https://<домен>/hook/inbox/<токен>.

    Без домена панели или токена хука подписывать нечего: Wazzup стучится
    снаружи, а пустой токен - это выключенный хук (404)."""
    host = panel_host(domain)
    secret = str(token or "").strip()
    if not host or not secret:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", secret):
        return None
    url = f"https://{host}/hook/inbox/{secret}"
    return url if len(url) <= 200 else None


def wazzup_fingerprint(url: str) -> str:
    """Отпечаток адреса хука: по нему видно, что подписка актуальна, а
    токен в базу не попадает."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def wazzup_pick_channel(own: Any, channels: Any, *, kind: str = "wa") -> str | None:
    """С какого нашего канала отвечать: тем, через который писал человек;
    не знаем - единственным живым каналом этого вида, иначе никаким."""
    mine = wazzup_channel(own)
    if mine:
        return mine
    alive = [c for c in channels or [] if isinstance(c, Mapping) and c.get("active")
             and (c.get("kind") or "wa") == kind]
    return wazzup_channel(alive[0].get("id")) if len(alive) == 1 else None


def inbox_preview(text: str | None, kind: str | None, *, limit: int = 90) -> str:
    """Строка превью: начало текста или вид вложения."""
    if text:
        line = " ".join(text.split())
        return line if len(line) <= limit else line[:limit - 1] + "…"
    if kind and kind != "text":
        return f"[{INBOX_KINDS.get(kind, kind)}]"
    return ""


# ─────────────────── быстрые формы сотрудника в боте ───────────────────
#
# Мастер заводит сторонний ремонт, администратор - выдачу, одним
# сообщением в личке бота: форма «ключ: значение», предпросмотр, кнопка.
# Здесь - только разбор текста; поиск в базе и запись - app/crm/quickforms.py.

QUICK_REPAIR_TEMPLATE = (
    "Дата обращения: {today}\n"
    "Имя клиента: \n"
    "Номер телефона клиента: \n"
    "Проблема/заказ: \n"
    "Дата окончания (фактического, либо оговорено с клиентом): -\n"
    "Кто выполняет (выполнил) работу: {who}\n"
    "Итоговая сумма (за работу): 0\n"
    "Итоговая сумма (за запчасти): 0\n"
    "Формат оплаты (нал/оплата по карте): 0"
)
QUICK_ISSUE_TEMPLATE = (
    "Выдача\n"
    "Телефон клиента: \n"
    "Велосипед №: \n"
    "Срок, дней: 7\n"
    "Пробег, км: \n"
    "Аккумулятор №: -\n"
    "Сумма оплаты: 0\n"
    "Формат оплаты (нал/карта/перевод): 0\n"
    "Договор №: -\n"
    "Дата начала: {today}"
)
# Слова способа оплаты -> код METHODS. «Безнал» и «перевод» - перевод,
# «нал» - только начало слова (как в pay_method_from_text).
QUICK_METHOD_WORDS: tuple[tuple[str, str], ...] = (
    ("transfer", r"безнал|перевод|по номеру"),
    ("cash", r"(?<![а-я])нал"),
    ("card", r"карт|термин|эквайр"),
    ("sbp", r"\bсбп\b|\bqr\b|\bкуар"),
)
QUICK_TERM_WORDS = {"неделя": 7, "неделю": 7, "месяц": 30, "сутки": 1, "день": 1}
# Мастер «я»/«сам» - тот, кто прислал форму.
QUICK_SELF_WORDS = frozenset({"я", "сам", "сама", "мной", "мною"})


def _quick_keys(lines: Sequence[str]) -> list[str]:
    return [pair[0] for pair in (_ops_pair(line) for line in lines) if pair]


def quick_value(lines: Sequence[str], pattern: str, *, text: bool = False) -> str:
    """Значение быстрой формы. Пустая строка ключа - значение строкой ниже
    («Итоговая сумма (за работу):» и «0» под ней - так пишут на точке).

    Строка ниже берётся, только когда у ключа пусто: «2500», под которым
    остался «0» из старой формы, иначе склеивалось в «2500 0» и читалось
    как 25 000 ₽. Абзац продолжения - только для текста (`text`): жалобу
    пишут в несколько строк, суммы и даты - нет."""
    for i, line in enumerate(lines):
        pair = _ops_pair(line)
        if pair is None or not re.search(pattern, pair[0]):
            continue
        if text:
            return ops_value(lines[i:], pattern, multiline=True)
        value = pair[1].strip()
        if not value and i + 1 < len(lines) and ":" not in lines[i + 1]:
            value = lines[i + 1].strip()
        return _ops_blank(value)
    return ""


def quick_form_kind(text: Any) -> str | None:
    """Какая это форма: "repair" (сторонний ремонт), "issue" (выдача) или
    None. Дёшево и без базы: по этому же гейт подписки пропускает форму
    сотрудника, а не только обработчик её узнаёт."""
    lines = ops_lines(text)
    if len(lines) < 3:
        return None
    keys = _quick_keys(lines)
    first = _ops_norm(lines[0])
    if re.match(r"^\W*выдача\b", first) and any(re.search(r"велосипед", k) for k in keys):
        return "issue"
    if any(re.search(r"проблем", k) for k in keys) \
            and any(re.search(r"дата обращ|\bимя\b", k) for k in keys):
        return "repair"
    return None


def form_date(raw: Any, *, today: date) -> tuple[date | None, str]:
    """Дата из формы: «23.09», «23.09.2026», «23.09.26», «сегодня», «вчера».
    Пусто или прочерк - (None, ""). Без года - тот год, где дата ближе всего
    к сегодня: «28.12», написанное 2 января, - это прошлый декабрь."""
    text = _ops_norm(raw)
    if _ops_blank(text) == "" or text in ("0", "нет"):
        return None, ""
    if text == "сегодня":
        return today, ""
    if text == "вчера":
        return today - timedelta(days=1), ""
    m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2}|\d{4}))?(?:\s*г\.?)?", text)
    if not m:
        return None, f"Не понял дату «{raw}»: пишите 23.09 или 23.09.2026."
    day, month = int(m.group(1)), int(m.group(2))
    year_raw = m.group(3)
    try:
        if year_raw:
            year = int(year_raw) + (2000 if len(year_raw) == 2 else 0)
            return date(year, month, day), ""
        options = []
        for year in (today.year - 1, today.year, today.year + 1):
            try:
                options.append(date(year, month, day))
            except ValueError:
                continue
        if not options:
            raise ValueError
        return min(options, key=lambda d: abs((d - today).days)), ""
    except ValueError:
        return None, f"Такой даты нет: «{raw}»."


def form_method(raw: Any) -> tuple[str | None, str]:
    """Способ оплаты из формы -> код METHODS. «0», «-», «нет» - не платил:
    (None, "")."""
    text = _ops_norm(raw)
    if _ops_blank(text) == "" or text in ("0", "нет", "не платил", "не оплачено", "долг"):
        return None, ""
    for code, pattern in QUICK_METHOD_WORDS:
        if re.search(pattern, text):
            return code, ""
    return None, f"Не понял формат оплаты «{raw}»: нал, карта или перевод."


def form_money(raw: Any, what: str) -> tuple[Decimal, str]:
    """Сумма из формы: пусто и прочерк - ноль; «1 500 р» - 1500."""
    if _ops_blank(str(raw or "")) == "":
        return Decimal("0.00"), ""
    got = ops_money(raw)
    if got is None or got < 0:
        return Decimal("0.00"), f"Не понял сумму «{what}: {raw}» — нужна цифра."
    return got, ""


def form_phone(raw: Any) -> tuple[str | None, str | None]:
    """(телефон +7…, ник Telegram без @) из строки «89996557593 @ivan»."""
    text = str(raw or "")
    nick = re.search(r"@([A-Za-z0-9_]{4,32})\b", text)
    found = re.search(r"\+?\d[\d\s()\-]{8,}\d", text)
    phone = bot_logic.normalize_phone(found.group()) if found else None
    return phone, nick.group(1) if nick else None


def form_term(raw: Any) -> int | None:
    """Срок аренды в днях: «7», «7 дней», «неделя», «2 недели», «месяц»."""
    text = _ops_norm(raw)
    if not text:
        return None
    m = re.match(r"^(\d{1,3})\s*(нед|мес)?", text)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        days = n * (7 if unit == "нед" else 30 if unit == "мес" else 1)
        return days if 0 < days <= 366 else None
    for word, days in QUICK_TERM_WORDS.items():
        if text.startswith(word):
            return days
    return None


def parse_quick_repair(text: Any, *, today: date) -> tuple[dict | None, list[str]]:
    """Форма стороннего ремонта (QUICK_REPAIR_TEMPLATE). Суммы и способ
    оплаты часто пишут строкой ниже ключа («Итоговая сумма (за работу):»
    и «0» под ней) - поэтому значения читаются с продолжением."""
    lines = ops_lines(text)
    errors: list[str] = []

    def value(pattern: str, *, text: bool = False) -> str:
        return quick_value(lines, pattern, text=text)

    opened, err = form_date(value(r"дата обращ"), today=today)
    if err:
        errors.append(err)
    opened = opened or today
    if opened > today:
        errors.append("Дата обращения в будущем — проверьте число.")
    name = " ".join(value(r"\bимя\b").split())[:120]
    if not name:
        errors.append("Нет имени клиента.")
    phone, nick = form_phone(value(r"телефон"))
    if not phone:
        errors.append("Нет телефона клиента или он не похож на номер.")
    problem = " ".join(value(r"проблем|заказ", text=True).split())[:500]
    if not problem:
        errors.append("Нет строки «Проблема/заказ».")
    finish, err = form_date(value(r"окончан|готов"), today=today)
    if err:
        errors.append(err)
    work, err = form_money(value(r"за работу|\bработа\b"), "за работу")
    if err:
        errors.append(err)
    parts, err = form_money(value(r"запчаст"), "за запчасти")
    if err:
        errors.append(err)
    method, err = form_method(value(r"формат оплат|способ оплат"))
    if err:
        errors.append(err)
    if method and work + parts <= 0:
        errors.append("Оплата указана, а сумма ноль — впишите сумму или 0 в оплате.")
    thing = " ".join(value(r"^техника|что чин|объект", text=True).split())[:200]
    data = {"opened_on": opened, "name": name, "phone": phone, "username": nick,
            "problem": problem, "thing": thing,
            "finish_on": finish, "tech": " ".join(value(r"выполня|мастер|исполнит").split())[:80],
            "work": work, "parts": parts, "method": method}
    return (None if errors else data), errors


def parse_quick_issue(text: Any, *, today: date) -> tuple[dict | None, list[str]]:
    """Форма быстрой выдачи (QUICK_ISSUE_TEMPLATE)."""
    lines = ops_lines(text)
    errors: list[str] = []

    def value(pattern: str) -> str:
        return quick_value(lines, pattern)

    phone, _ = form_phone(value(r"телефон"))
    if not phone:
        errors.append("Нет телефона клиента или он не похож на номер.")
    code = check_code(re.sub(r"^№\s*", "", value(r"велосипед")))
    if not code.ok:
        errors.append("Нет номера велосипеда.")
    term = form_term(value(r"срок"))
    if term is None:
        errors.append("Срок аренды: число дней, например 7.")
    mileage_raw = value(r"пробег")
    if not re.fullmatch(r"\d[\d  ]{0,8}(?:\s*км)?", mileage_raw or ""):
        errors.append("Пробег с дисплея — числом, без него выдачу не оформить.")
        mileage = None
    else:
        mileage = int(re.sub(r"\D", "", mileage_raw))
    battery_raw = re.sub(r"^№\s*", "", value(r"аккумулятор|\bакб\b|батаре"))
    battery = check_code(battery_raw).value if battery_raw else None
    pay, err = form_money(value(r"сумма"), "сумма оплаты")
    if err:
        errors.append(err)
    method, err = form_method(value(r"формат|способ"))
    if err:
        errors.append(err)
    if pay > 0 and not method:
        errors.append("Сумма есть, а формат оплаты не указан: нал, карта или перевод.")
    if method and pay <= 0:
        errors.append("Формат оплаты указан, а сумма ноль.")
    contract = " ".join(value(r"договор").split())[:60] or None
    started, err = form_date(value(r"дата|начал"), today=today)
    if err:
        errors.append(err)
    started = started or today
    problem = rental_start_problem(started, today)
    if problem:
        errors.append(problem)
    data = {"phone": phone, "bike_code": code.value if code.ok else None, "term": term,
            "mileage": mileage, "battery_code": battery, "pay": pay, "method": method,
            "contract_no": contract, "started_on": started}
    return (None if errors else data), errors


def match_staff(people: Iterable[Mapping[str, Any]], said: Any) -> list[dict]:
    """Сотрудники под «Кто выполняет»: точное слово имени, логин или ник
    Telegram; иначе - первые три буквы («вовуча» - это «Вова»). Несколько
    подходящих - решает человек, а не первый в списке."""
    word = _ops_norm(said).lstrip("@")
    if not word:
        return []
    active = [dict(p) for p in people if p.get("active", True) and not staff_expired(p)]

    def tokens(p: Mapping[str, Any]) -> set[str]:
        out = set(_ops_norm(p.get("name")).replace(".", " ").split())
        out.add(_ops_norm(p.get("login")))
        out.add(_ops_norm(p.get("tg_username")).lstrip("@"))
        return {t for t in out if t}
    exact = [p for p in active if word in tokens(p) or word == _ops_norm(p.get("name"))]
    if exact:
        return exact
    if len(word) < 3:
        return []
    return [p for p in active
            if any(len(t) >= 3 and (word.startswith(t[:3]) or t.startswith(word[:3]))
                   for t in tokens(p))]


ORDER_NO_RE = re.compile(r"РЕМ-\d{6}")


def order_no_in(text: Any) -> str | None:
    """Номер наряда из карточки бота («Наряд РЕМ-000123 …»)."""
    found = ORDER_NO_RE.search(str(text or ""))
    return found.group() if found else None
