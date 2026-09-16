"""Машиночитаемая зона документа: разбор трёх раскладок и сверка с анкетой.

Чистая логика без внешних зависимостей: на вход - текст из распознавателя,
на выход - поля с отметкой, сошлись ли контрольные цифры. Само распознавание
в app/services/ocr.py, оно и подаёт сюда текст.

Зачем контрольные цифры. МЧЗ - единственная часть документа, которая
проверяет сама себя: каждое поле несёт цифру, посчитанную по весам 7-3-1
(ICAO 9303). Неверно прочитанный символ почти всегда валит эту сумму, и
ошибка выходит наружу отказом, а не тихо подставленным числом. Именно
поэтому OCR здесь допустим: прошлый раз его убрали за ложные срабатывания,
а тут неудачное чтение видно сразу.

Что читаем. Курьеры приходят с тремя видами документов, и раскладки у них
разные:

    TD3, 2 строки по 44   паспорт-книжка. Тип «PN» + RUS - внутренний
                          российский, всё остальное - загранпаспорт любой
                          страны (UZB, TJK, KGZ, ...).
    TD1, 3 строки по 30   пластиковая карта: вид на жительство, РВП,
                          национальное удостоверение личности.

Раскладка TD3, строка 2:

              1-9   номер документа
              10    контрольная цифра к нему
              11-13 гражданство (RUS, UZB, ...)
              14-19 дата рождения ГГММДД
              20    контрольная цифра
              21    пол M/F
              22-27 срок действия ГГММДД
              28    контрольная цифра
              29-42 личный номер
              43    контрольная цифра к личному номеру
              44    общая контрольная цифра

У внутреннего российского паспорта срока действия нет (там «<<<<<<»), зато
личный номер занят: серия не влезает в девятизначное поле номера целиком
(4 + 6 = 10), поэтому четвёртая её цифра вынесена туда же, к коду
подразделения и дате выдачи. Это единственное место, где раскладка может
разойтись с конкретным бланком, - поэтому четвёртая цифра берётся только
при сошедшейся контрольной цифре личного номера, а иначе серия отдаётся
неполной.

У загранпаспорта наоборот: срок действия есть и он важнее всего (аренда не
должна пережить документ), а даты выдачи в МЧЗ нет вовсе - сверить её
неоткуда, и она остаётся целиком на человеке.

Раскладка TD1: строка 1 - тип, государство, номер документа и контрольная
цифра; строка 2 - дата рождения, пол, срок действия, гражданство; строка 3 -
фамилия и имя.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

# Длины строк по ICAO 9303: паспорт-книжка и пластиковая карта.
TD3_LEN = 44
TD1_LEN = 30
FILLER = "<"

# Вид документа - от него зависит и раскладка, и что имеет смысл сверять.
RF_INTERNAL = "rf_internal"      # внутренний паспорт РФ
PASSPORT = "passport"            # загранпаспорт любой страны
ID_CARD = "id_card"              # карта: ВНЖ, РВП, национальное удостоверение

KIND_TITLES: dict[str, str] = {
    RF_INTERNAL: "паспорт РФ",
    PASSPORT: "загранпаспорт",
    ID_CARD: "карта (ВНЖ, РВП или удостоверение)",
}

# Коды стран по ICAO для тех, кто реально приходит наниматься в курьеры.
# Незнакомый код показывается как есть - это лучше, чем врать названием.
COUNTRIES: dict[str, str] = {
    "RUS": "Россия", "UZB": "Узбекистан", "TJK": "Таджикистан",
    "KGZ": "Киргизия", "KAZ": "Казахстан", "TKM": "Туркменистан",
    "AZE": "Азербайджан", "ARM": "Армения", "BLR": "Беларусь",
    "UKR": "Украина", "MDA": "Молдавия", "GEO": "Грузия",
    "TUR": "Турция", "CHN": "Китай", "IND": "Индия", "VNM": "Вьетнам",
    "AFG": "Афганистан", "PAK": "Пакистан", "BGD": "Бангладеш",
}


def country_name(code: str) -> str:
    """Название страны по коду ICAO. Незнакомый код - как есть."""
    code = (code or "").strip().upper()
    return COUNTRIES.get(code, code)

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

    kind: str = PASSPORT                         # RF_INTERNAL, PASSPORT, ID_CARD
    country: str = ""                            # код гражданства: RUS, UZB, ...
    surname: str = ""
    given: str = ""                              # имя и отчество латиницей
    passport_number: str = ""                    # как в анкете
    # Девять цифр поля номера у внутреннего паспорта РФ: 3 из серии и 6 номера.
    # Остаются даже когда личный номер не прочитался и четвёртая цифра серии
    # неизвестна. У прочих документов совпадает с passport_number.
    number_digits: str = ""
    birth_date: date | None = None
    sex: str = ""                                # «М», «Ж» или пусто
    passport_date: date | None = None            # дата выдачи (только паспорт РФ)
    passport_code: str = ""                      # «160-002» (только паспорт РФ)
    passport_expiry: date | None = None          # срок действия (кроме паспорта РФ)
    failed: tuple[str, ...] = field(default=())  # поля с несошедшейся цифрой

    @property
    def ok(self) -> bool:
        """Есть ли хоть что-то, чему можно верить."""
        return bool(self.number_digits or self.passport_number or self.birth_date)

    @property
    def foreign(self) -> bool:
        """Документ иностранца: гражданство не РФ либо это не паспорт РФ."""
        return self.kind != RF_INTERNAL

    @property
    def citizenship(self) -> str:
        """Гражданство по-русски: «Узбекистан», «Россия», ...  Пусто - не знаем."""
        return country_name(self.country)

    def expired(self, *, today: date | None = None) -> bool | None:
        """Истёк ли документ. None - срока действия в МЧЗ нет."""
        if self.passport_expiry is None:
            return None
        return self.passport_expiry < (today or date.today())


def _rows(text: str, length: int) -> list[str]:
    """Строки нужной длины из вывода распознавателя.

    Распознаватель отдаёт всю страницу, и строки МЧЗ приходится искать среди
    прочего текста. Пара символов могла потеряться, поэтому длина берётся
    с допуском, а короткое добивается заполнителем. Пробелы внутри убираются -
    тессеракт любит вставить их в разрыв между группами символов.
    """
    out = []
    for raw in text.splitlines():
        row = re.sub(r"[^A-Z0-9<]", "", raw.upper())
        if length - 4 <= len(row) <= length + 4:
            out.append(row[:length].ljust(length, FILLER))
    return out


def find_lines(text: str) -> tuple[str, list[str]] | None:
    """Строки МЧЗ и формат: («td3», [строка1, строка2]) или («td1», [3 строки]).

    TD1 проверяется первым: его строки короче, и длинная строка TD3 под
    допуск TD1 не подойдёт, а вот обрывок TD3 - мог бы.
    """
    td1 = _rows(text, TD1_LEN)
    for i in range(len(td1) - 2, -1, -1):
        # У карты первая строка начинается с типа документа: «I», «A», «C»
        # (ВНЖ и удостоверения) или «P» - и почти всегда с кода государства.
        if td1[i][0:1] in ("I", "A", "C", "P") and td1[i][2:5].isalpha():
            return "td1", td1[i:i + 3]

    td3 = _rows(text, TD3_LEN)
    for i in range(len(td3) - 1, 0, -1):
        # «PN»/«P<» - тип документа, дальше код государства. Хватает одного
        # из двух: распознаватель может потерять любой символ, а промахнуться
        # в обоих местах сразу - вряд ли.
        if td3[i - 1][0:1] == "P" or td3[i - 1][2:5].isalpha():
            return "td3", [td3[i - 1], td3[i]]
    return None


def _names(raw: str) -> tuple[str, str]:
    """Фамилия и остальные имена из поля имён МЧЗ."""
    parts = raw.split("<<", 1)
    surname = parts[0].replace(FILLER, " ").strip()
    given = parts[1].replace(FILLER, " ").strip() if len(parts) > 1 else ""
    return surname, given


def _checked_date(field_raw: str, check: str, *, future_ok: bool,
                  today: date | None) -> date | None:
    """Дата из МЧЗ, если её контрольная цифра сошлась."""
    if check_digit(digits_only(field_raw)) != digits_only(check):
        return None
    return _date(field_raw, future_ok=future_ok, today=today)


def _document_number(raw: str, check: str) -> str:
    """Номер документа, если контрольная цифра сошлась.

    У загранпаспорта и карты номер буквенно-цифровой («AA1234567»), поэтому
    цифровая правка распознавания к нему неприменима: «O» в нём может быть
    настоящей буквой. Проверяет только контрольная цифра.
    """
    if check_digit(raw) != digits_only(check):
        return ""
    return raw.replace(FILLER, "").strip()


def _parse_td3(lines: list[str], *, today: date | None) -> Mrz:
    first, second = lines
    failed: list[str] = []
    surname, given = _names(first[5:])
    country = first[2:5]
    # «PN» + RUS - внутренний паспорт РФ, у него своя раскладка хвоста.
    internal = first.startswith("PN") and country == "RUS"
    sex = {"M": "М", "F": "Ж"}.get(second[20], "")

    birth = _checked_date(second[13:19], second[19], future_ok=False, today=today)
    if birth is None:
        failed.append("дата рождения")

    if internal:
        number_raw = digits_only(second[0:9])
        number_digits = ""
        passport_number = ""
        if number_raw.isdigit() and check_digit(number_raw) == digits_only(second[9]):
            number_digits = number_raw
        else:
            failed.append("серия и номер")
        # Личный номер: 4-я цифра серии, код подразделения, дата выдачи.
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
        return Mrz(kind=RF_INTERNAL, country=country, surname=surname, given=given,
                   passport_number=passport_number, number_digits=number_digits,
                   birth_date=birth, sex=sex, passport_date=passport_date,
                   passport_code=passport_code, failed=tuple(failed))

    number = _document_number(second[0:9], second[9])
    if not number:
        failed.append("номер документа")
    # Срок действия в будущем - норма, поэтому future_ok.
    expiry = _checked_date(second[21:27], second[27], future_ok=True, today=today)
    if expiry is None:
        failed.append("срок действия")
    return Mrz(kind=PASSPORT, country=second[10:13] or country, surname=surname,
               given=given, passport_number=number, number_digits=number,
               birth_date=birth, sex=sex, passport_expiry=expiry,
               failed=tuple(failed))


def _parse_td1(lines: list[str], *, today: date | None) -> Mrz:
    """Пластиковая карта: ВНЖ, РВП, национальное удостоверение."""
    first, second, third = (lines + ["", "", ""])[:3]
    failed: list[str] = []
    surname, given = _names(third)

    number = _document_number(first[5:14], first[14])
    if not number:
        failed.append("номер документа")
    birth = _checked_date(second[0:6], second[6], future_ok=False, today=today)
    if birth is None:
        failed.append("дата рождения")
    expiry = _checked_date(second[8:14], second[14], future_ok=True, today=today)
    if expiry is None:
        failed.append("срок действия")
    sex = {"M": "М", "F": "Ж"}.get(second[7], "")
    country = second[15:18] if second[15:18].isalpha() else first[2:5]
    return Mrz(kind=ID_CARD, country=country, surname=surname, given=given,
               passport_number=number, number_digits=number, birth_date=birth,
               sex=sex, passport_expiry=expiry, failed=tuple(failed))


def parse(text: str, *, today: date | None = None) -> Mrz | None:
    """Разбор МЧЗ. None - строк в тексте не нашлось вовсе."""
    found = find_lines(text)
    if found is None:
        return None
    layout, lines = found
    if layout == "td1":
        return _parse_td1(lines, today=today)
    return _parse_td3(lines, today=today)


# Поле анкеты -> подпись. Наборы разные, потому что разное знает сама МЧЗ:
# у внутреннего паспорта РФ есть дата выдачи и код подразделения, но нет
# срока действия; у загранпаспорта и карты наоборот - есть срок действия,
# а даты выдачи нет вовсе, и сверить её неоткуда.
#
# «Кем выдан», место рождения и адреса не записаны ни в одной МЧЗ и остаются
# полностью на человеке.
COMPARED_RF: tuple[tuple[str, str], ...] = (
    ("passport_number", "серия и номер"),
    ("birth_date", "дата рождения"),
    ("passport_date", "дата выдачи"),
    ("passport_code", "код подразделения"),
)
COMPARED_FOREIGN: tuple[tuple[str, str], ...] = (
    ("passport_number", "номер документа"),
    ("birth_date", "дата рождения"),
    ("passport_expiry", "срок действия"),
    ("citizenship", "гражданство"),
)


def compared_for(mrz: Mrz | None) -> tuple[tuple[str, str], ...]:
    return COMPARED_FOREIGN if mrz is not None and mrz.foreign else COMPARED_RF


def _as_text(value: object) -> str:
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    return str(value or "").strip()


def _number_pair(mrz: Mrz, typed: str) -> tuple[str, str, str]:
    """Что сравнивать в номере документа.

    У внутреннего паспорта РФ серия разорвана: четвёртая её цифра лежит
    в поле личного номера, а оно читается хуже прочих - последнее в строке
    и целиком цифровое. Когда оно не сошлось, остальные девять цифр всё
    равно проверены контрольной цифрой, и сверить их полезнее, чем не
    сверять ничего. У остальных документов номер цельный, и делить нечего.
    """
    if mrz.passport_number or mrz.kind != RF_INTERNAL:
        return mrz.passport_number, typed, "номер документа" if mrz.foreign \
            else "серия и номер"
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
    for field_name, label in compared_for(mrz):
        typed = _as_text(anketa.get(field_name))
        if mrz is None:
            from_mrz = ""
        elif field_name == "passport_number":
            from_mrz, typed, label = _number_pair(mrz, typed)
        elif field_name == "citizenship":
            from_mrz = mrz.citizenship
        else:
            from_mrz = _as_text(getattr(mrz, field_name, ""))
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
# За сколько дней до конца срока документа предупреждать. Средняя аренда -
# три недели, и документ, который кончится внутри неё, лучше увидеть сразу.
EXPIRY_WARN_DAYS = 30


def _clip(text: str) -> str:
    return text if len(text) <= SUMMARY_LIMIT else text[:SUMMARY_LIMIT - 1].rstrip() + "…"


def expiry_note(mrz: Mrz | None, *, today: date | None = None) -> str:
    """Слово про срок действия: пусто, если он в порядке или его нет."""
    if mrz is None or mrz.passport_expiry is None:
        return ""
    today = today or date.today()
    left = (mrz.passport_expiry - today).days
    if left < 0:
        return f"документ просрочен {mrz.passport_expiry:%d.%m.%Y}"
    if left <= EXPIRY_WARN_DAYS:
        return f"документ кончается {mrz.passport_expiry:%d.%m.%Y} — через {left} дн."
    return ""


def title(mrz: Mrz | None) -> str:
    """Что за документ и чей: «загранпаспорт Узбекистана»."""
    if mrz is None:
        return ""
    kind = KIND_TITLES.get(mrz.kind, "документ")
    country = mrz.citizenship
    if not mrz.foreign or not country:
        return kind
    return f"{kind} ({country})"


def summary(mrz: Mrz | None, anketa: dict | None,
            *, today: date | None = None) -> tuple[bool, str] | None:
    """Одна строка для карточки модератора и признак «всё в порядке».

    None - показывать нечего: МЧЗ не прочиталась или не дала ни одного поля,
    которое есть в анкете. Строка короткая намеренно: подпись к фотографии
    в Telegram ограничена 1024 символами, и длинный разбор вытеснил бы
    реквизиты договора.

    Просроченный документ - такой же повод присмотреться, как расхождение:
    сдавать велосипед на три недели по паспорту, который кончился, нельзя.
    """
    if mrz is None or not mrz.ok:
        return None
    result = compare(mrz, anketa)
    head = f"{title(mrz)}: " if mrz.foreign else ""
    expiry = expiry_note(mrz, today=today)
    if result["differ"] or expiry:
        parts = ([expiry] if expiry else []) + result["differ"]
        return False, _clip(head + "; ".join(parts))
    if result["same"]:
        tail = (f"; не прочиталось: {', '.join(result['unknown'])}"
                if result["unknown"] else "")
        return True, f"{head}сходится — {', '.join(result['same'])}{tail}"
    return None
