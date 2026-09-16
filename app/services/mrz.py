"""Машиночитаемая зона паспорта РФ: разбор двух строк и сверка с анкетой.

Чистая логика без внешних зависимостей: на вход - текст из распознавателя,
на выход - поля с отметкой, сошлись ли контрольные цифры. Само распознавание
в app/services/ocr.py, оно и подаёт сюда текст.

Зачем контрольные цифры. МЧЗ - единственная часть паспорта, которая
проверяет сама себя: каждое поле несёт цифру, посчитанную по весам 7-3-1
(ICAO 9303). Неверно прочитанный символ почти всегда валит эту сумму, и
ошибка выходит наружу отказом, а не тихо подставленным числом. Именно
поэтому OCR здесь допустим: прошлый раз его убрали за ложные срабатывания,
а тут неудачное чтение видно сразу.

Раскладка внутреннего паспорта РФ (2 строки по 44 символа, TD3):

    строка 1  PN RUS ФАМИЛИЯ<<ИМЯ<ОТЧЕСТВО<<<...
    строка 2  1-9   номер документа: 3 цифры серии + 6 цифр номера
              10    контрольная цифра к нему
              11-13 RUS
              14-19 дата рождения ГГММДД
              20    контрольная цифра
              21    пол M/F
              22-27 срок действия - у внутреннего паспорта его нет, «<<<<<<»
              28    контрольная цифра
              29-42 личный номер: 4-я цифра серии + код подразделения +
                    дата выдачи ГГММДД
              43    контрольная цифра к личному номеру
              44    общая контрольная цифра

Серия не влезает в девятизначное поле номера целиком (4 + 6 = 10), поэтому
четвёртая её цифра вынесена в личный номер. Это и есть главное место, где
раскладка может разойтись с конкретным бланком, - поэтому четвёртая цифра
берётся только при сошедшейся контрольной цифре личного номера, а иначе
серия отдаётся неполной.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

LINE_LEN = 44
FILLER = "<"

# Веса ICAO 9303: по кругу 7-3-1 на каждый символ поля.
WEIGHTS = (7, 3, 1)

# Распознаватель путает буквы и цифры в числовых полях. Замены односторонние:
# в датах и номерах букв быть не может, а контрольная цифра скажет, помогло ли.
TO_DIGIT = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1",
                          "Z": "2", "S": "5", "B": "8", "G": "6", "T": "7"})


def _value(char: str) -> int:
    """Вес символа: цифра - сама собой, буква - номер в алфавите плюс 9."""
    if char.isdigit():
        return int(char)
    if char == FILLER:
        return 0
    if "A" <= char <= "Z":
        return ord(char) - ord("A") + 10
    return -1                                    # мусор: сумма не сойдётся


def check_digit(text: str) -> str:
    """Контрольная цифра поля по весам 7-3-1. «<» для нечитаемого символа."""
    total = 0
    for i, char in enumerate(text):
        weight = _value(char)
        if weight < 0:
            return FILLER
        total += weight * WEIGHTS[i % 3]
    return str(total % 10)


def digits_only(text: str) -> str:
    """Числовое поле МЧЗ с поправкой на типовые ошибки распознавания."""
    return text.translate(TO_DIGIT)


def _date(raw: str, *, future_ok: bool, today: date | None = None) -> date | None:
    """ГГММДД -> дата. Век выбирается так, чтобы дата не оказалась в будущем.

    В МЧЗ год двузначный, и «15» - это и 1915, и 2015. Ни дата рождения, ни
    дата выдачи паспорта в будущем быть не могут, поэтому век однозначен.
    """
    raw = digits_only(raw)
    if not re.fullmatch(r"\d{6}", raw):
        return None
    year, month, day = int(raw[0:2]), int(raw[2:4]), int(raw[4:6])
    today = today or date.today()
    for century in (2000, 1900):
        try:
            value = date(century + year, month, day)
        except ValueError:
            continue
        if future_ok or value <= today:
            return value
    return None


@dataclass(frozen=True)
class Mrz:
    """Разобранная МЧЗ. Пустое поле - не прочиталось или не сошлось."""

    surname: str = ""
    given: str = ""                              # имя и отчество латиницей
    passport_number: str = ""                    # «1234 567890» как в анкете
    # Девять цифр поля номера: 3 из серии и 6 номера. Остаются даже когда
    # личный номер не прочитался и четвёртая цифра серии неизвестна.
    number_digits: str = ""
    birth_date: date | None = None
    sex: str = ""                                # «М», «Ж» или пусто
    passport_date: date | None = None            # дата выдачи
    passport_code: str = ""                      # «160-002» как в анкете
    failed: tuple[str, ...] = field(default=())  # поля с несошедшейся цифрой

    @property
    def ok(self) -> bool:
        """Есть ли хоть что-то, чему можно верить."""
        return bool(self.number_digits or self.birth_date)


def find_lines(text: str) -> tuple[str, str] | None:
    """Две строки МЧЗ в выводе распознавателя.

    Распознаватель отдаёт всю страницу, и строки МЧЗ приходится искать среди
    прочего текста: берутся две последние подряд идущие строки нужной длины,
    начинающиеся с «PN». Пробелы внутри убираются - тессеракт любит вставить
    их в разрыв между группами символов.
    """
    rows = []
    for raw in text.splitlines():
        row = re.sub(r"[^A-Z0-9<]", "", raw.upper())
        if len(row) >= LINE_LEN - 4:             # пара символов могла потеряться
            rows.append(row[:LINE_LEN].ljust(LINE_LEN, FILLER))
    for i in range(len(rows) - 1, 0, -1):
        # «PN» - тип документа, «RUS» - государство. Хватает одного из двух:
        # распознаватель может потерять любой символ, а промахнуться в обоих
        # местах сразу - вряд ли.
        if rows[i - 1].startswith("PN") or rows[i - 1][2:5] == "RUS":
            return rows[i - 1], rows[i]
    return None


def parse(text: str, *, today: date | None = None) -> Mrz | None:
    """Разбор МЧЗ. None - двух строк в тексте не нашлось вовсе."""
    found = find_lines(text)
    if found is None:
        return None
    first, second = found
    failed: list[str] = []

    # ─── имена ───
    names = first[5:].split("<<", 1)
    surname = names[0].replace(FILLER, " ").strip()
    given = names[1].replace(FILLER, " ").strip() if len(names) > 1 else ""

    # ─── номер документа: 3 цифры серии + 6 цифр номера ───
    number_raw = digits_only(second[0:9])
    passport_number = ""
    number_digits = ""
    if check_digit(number_raw) == digits_only(second[9]) and number_raw.isdigit():
        number_digits = number_raw               # четвёртая цифра серии - ниже
    else:
        failed.append("серия и номер")

    # ─── дата рождения ───
    birth_raw = second[13:19]
    birth = _date(birth_raw, future_ok=False, today=today)
    if check_digit(digits_only(birth_raw)) != digits_only(second[19]):
        failed.append("дата рождения")
        birth = None

    sex = {"M": "М", "F": "Ж"}.get(second[20], "")

    # ─── личный номер: 4-я цифра серии, код подразделения, дата выдачи ───
    personal = digits_only(second[28:42])
    passport_date = None
    passport_code = ""
    if check_digit(personal) == digits_only(second[42]):
        if number_digits and re.fullmatch(r"\d", personal[0:1]):
            digits = number_digits[0:3] + personal[0] + number_digits[3:9]
            passport_number = f"{digits[:4]} {digits[4:]}"
        if re.fullmatch(r"\d{6}", personal[1:7]):
            passport_code = f"{personal[1:4]}-{personal[4:7]}"
        passport_date = _date(personal[7:13], future_ok=False, today=today)
    else:
        failed.append("личный номер (код подразделения и дата выдачи)")

    return Mrz(surname=surname, given=given, passport_number=passport_number,
               number_digits=number_digits, birth_date=birth, sex=sex,
               passport_date=passport_date, passport_code=passport_code,
               failed=tuple(failed))


# Поле анкеты -> как достать то же самое из МЧЗ. Сверяются только те поля,
# которые МЧЗ знает точно; «кем выдан», место рождения и адреса в ней
# не записаны вовсе и остаются полностью на человеке.
COMPARED: tuple[tuple[str, str], ...] = (
    ("passport_number", "серия и номер"),
    ("birth_date", "дата рождения"),
    ("passport_date", "дата выдачи"),
    ("passport_code", "код подразделения"),
)


def _as_text(value: object) -> str:
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    return str(value or "").strip()


def _number_pair(mrz: Mrz, typed: str) -> tuple[str, str, str]:
    """Что сравнивать в серии и номере: полные десять цифр или девять.

    Личный номер читается хуже остальных полей - он последний в строке и
    целиком цифровой. Когда он не сошёлся, четвёртой цифры серии нет, но
    остальные девять проверены контрольной цифрой, и сверить их всё равно
    полезнее, чем не сверять ничего.
    """
    if mrz.passport_number:
        return mrz.passport_number, typed, "серия и номер"
    digits = re.sub(r"\D", "", typed)
    short = digits[0:3] + digits[4:10] if len(digits) == 10 else digits
    return mrz.number_digits, short, "серия и номер (без 4-й цифры серии)"


def compare(mrz: Mrz | None, anketa: dict | None) -> dict[str, list[str]]:
    """Сверка МЧЗ с тем, что человек ввёл руками.

    Возвращает три списка: что сошлось, что разошлось (с обоими значениями)
    и что МЧЗ не дала. Решение не принимается: это подсказка модератору,
    который всё равно смотрит на фотографию.
    """
    same: list[str] = []
    differ: list[str] = []
    unknown: list[str] = []
    anketa = anketa or {}
    for field_name, label in COMPARED:
        typed = _as_text(anketa.get(field_name))
        if field_name == "passport_number" and mrz is not None:
            from_mrz, typed, label = _number_pair(mrz, typed)
        else:
            from_mrz = _as_text(getattr(mrz, field_name, "") if mrz else "")
        if not from_mrz:
            unknown.append(label)
        elif not typed:
            differ.append(f"{label}: в МЧЗ {from_mrz}, в анкете пусто")
        elif from_mrz == typed:
            same.append(label)
        else:
            differ.append(f"{label}: в МЧЗ {from_mrz}, в анкете {typed}")
    return {"same": same, "differ": differ, "unknown": unknown}


# Предел длины строки сверки. Подпись к фотографии в Telegram - 1024 символа
# на всё вместе с реквизитами договора, и разошедшиеся четыре поля сразу
# съели бы у них четверть места. Первых расхождений хватает, чтобы понять,
# куда смотреть.
SUMMARY_LIMIT = 200


def _clip(text: str) -> str:
    return text if len(text) <= SUMMARY_LIMIT else text[:SUMMARY_LIMIT - 1].rstrip() + "…"


def summary(mrz: Mrz | None, anketa: dict | None) -> tuple[bool, str] | None:
    """Одна строка для карточки модератора и признак «всё сошлось».

    None - показывать нечего: МЧЗ не прочиталась или не дала ни одного поля,
    которое есть в анкете. Строка короткая намеренно: подпись к фотографии
    в Telegram ограничена 1024 символами, и длинный разбор вытеснил бы
    реквизиты договора.
    """
    if mrz is None or not mrz.ok:
        return None
    result = compare(mrz, anketa)
    if result["differ"]:
        return False, _clip("; ".join(result["differ"]))
    if result["same"]:
        parts = ", ".join(result["same"])
        tail = f"; не прочиталось: {', '.join(result['unknown'])}" if result["unknown"] else ""
        return True, f"сходится — {parts}{tail}"
    return None
