"""Модель расписания: занятие, пара, арифметика учебных недель.

Ни одной сторонней зависимости. Расчёт номера недели и попадания занятия в
неделю - самое хрупкое место бота, и проверять его тестами нужно на голом
stdlib, без установленного aiogram: ровно так же устроен app/logic.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

# Расписание составлено для Казани, а КФУ живёт по московскому времени.
# Смещение фиксированное: перевода часов в России нет с 2014 года, поэтому
# ZoneInfo с его базой tzdata здесь не нужен - лишняя зависимость, которая
# в slim-образе ещё и отсутствует.
MSK = timezone(timedelta(hours=3), "MSK")

# Первый понедельник семестра. 01.09.2026 - вторник, а сетка расписания
# расчерчена по дням недели, поэтому неделя №1 - это та, в которую попадает
# 1 сентября, то есть неделя с понедельника 31.08.2026.
#
# Проверяется самим файлом расписания: занятия со вторника по пятницу идут
# с «1н», а все понедельничные - с «2н». Понедельник первой недели (31.08)
# приходится на день до начала семестра, пар в нём и нет.
WEEK1_MONDAY = date(2026, 8, 31)

# Последняя учебная неделя осеннего семестра: дальше диапазонов «-18н»
# в файле не встречается.
LAST_WEEK = 18

# Дни недели так, как они подписаны в файле; индекс совпадает с
# date.weekday(), поэтому переводить одно в другое не нужно.
DAY_NAMES = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)

# Чётность недели, по которой ходит подгруппа: «ч.н.» и «н.н.» в файле.
ANY_WEEK = "any"
EVEN_WEEK = "even"
ODD_WEEK = "odd"

PARITY_NAMES = {
    ANY_WEEK: "",
    EVEN_WEEK: "по чётным неделям",
    ODD_WEEK: "по нечётным неделям",
}


# Сетка пар КФУ: начало каждой из них. Нужна только для нумерации («3-я
# пара») - считать номер по позиции в списке дня нельзя, в дне бывают
# пропущенные слоты, и вторая по счёту пара оказалась бы «второй», хотя
# по сетке она третья.
GRID = (
    time(8, 30),
    time(10, 10),
    time(12, 10),
    time(13, 50),
    time(15, 50),
    time(17, 30),
    time(19, 10),
)


def pair_number(start: time) -> int | None:
    """Номер пары по сетке. None - если время не из сетки."""
    return GRID.index(start) + 1 if start in GRID else None


@dataclass(frozen=True)
class Lesson:
    """Одно занятие внутри пары.

    В одной клетке расписания их бывает несколько: например, «Экономика (л)
    2-10н» и «Физико-химические основы (пр) 11-18н» стоят в одной паре
    понедельника и сменяют друг друга по ходу семестра. Поэтому недели - не
    свойство пары, а свойство занятия.
    """

    subject: str
    kind: str = ""            # л / пр / лаб / л+пр - как в файле
    teacher: str = ""
    first_week: int = 1
    last_week: int = LAST_WEEK
    parity: str = ANY_WEEK
    subgroup: str = ""        # «1/2 гр.», «1гр», «2гр» - как в файле
    room: str = ""
    # Фактическое время, если оно отличается от сетки. Физкультура стоит
    # в слоте 13:50-15:20, но идёт 14:00-15:30, и «какая сейчас пара» без
    # этой поправки врёт на десять минут дважды в неделю.
    start: time | None = None
    end: time | None = None
    note: str = ""

    def happens_on(self, week: int) -> bool:
        """Идёт ли занятие на учебной неделе с номером week."""
        if not self.first_week <= week <= self.last_week:
            return False
        if self.parity == EVEN_WEEK and week % 2 != 0:
            return False
        if self.parity == ODD_WEEK and week % 2 == 0:
            return False
        return True

    @property
    def title(self) -> str:
        return f"{self.subject} ({self.kind})" if self.kind else self.subject


@dataclass(frozen=True)
class Slot:
    """Пара: время по сетке и занятия, которые в неё попадают."""

    start: time
    end: time
    lessons: tuple[Lesson, ...] = ()
    # Исходный текст клетки из xlsx. Хранится не для показа, а для сверки:
    # тест проверяет, что каждый разобранный предмет и каждый диапазон
    # недель действительно есть в исходнике, и опечатка при переносе
    # расписания руками не доживает до пользователя.
    raw: str = ""

    def on_week(self, week: int) -> tuple[Lesson, ...]:
        return tuple(lesson for lesson in self.lessons if lesson.happens_on(week))

    def bounds(self, lesson: Lesson | None = None) -> tuple[time, time]:
        """Границы пары с учётом персонального времени занятия."""
        if lesson is None:
            return self.start, self.end
        return lesson.start or self.start, lesson.end or self.end


@dataclass(frozen=True)
class Occurrence:
    """Занятие, привязанное к конкретной дате: то, чем оперирует бот.

    Держим и aware-datetime границы, и исходные слот с занятием: по первым
    считается «идёт сейчас / начнётся через», по вторым - текст ответа.
    """

    day: date
    week: int
    slot: Slot
    lesson: Lesson
    starts_at: datetime
    ends_at: datetime

    def contains(self, moment: datetime) -> bool:
        return self.starts_at <= moment < self.ends_at


def week_number(day: date) -> int:
    """Номер учебной недели. 0 и меньше - семестр ещё не начался."""
    return (day - WEEK1_MONDAY).days // 7 + 1


def monday_of_week(week: int) -> date:
    """Понедельник учебной недели с номером week."""
    return WEEK1_MONDAY + timedelta(days=7 * (week - 1))


def in_semester(week: int) -> bool:
    return 1 <= week <= LAST_WEEK


def combine(day: date, moment: time) -> datetime:
    """Дата и время в московский aware-datetime.

    Naive datetime здесь недопустим: сравнение с datetime.now(MSK) уронило бы
    бота с TypeError, причём только в момент, когда кто-то спросит «что
    сейчас», а не на старте.
    """
    return datetime.combine(day, moment, tzinfo=MSK)
