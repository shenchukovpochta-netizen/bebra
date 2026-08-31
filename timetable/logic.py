"""Выборки по расписанию: день, неделя, текущая и ближайшая пара.

Чистые функции над данными из timetable.data. Ни одного обращения ко
времени «прямо сейчас» внутри расчётов: момент всегда приходит аргументом,
иначе тест на «какая сейчас пара» пришлось бы гонять в нужную минуту
нужного дня.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .data import SCHEDULE
from .model import (
    LAST_WEEK,
    MSK,
    Occurrence,
    Slot,
    combine,
    in_semester,
    monday_of_week,
    week_number,
)

# Последний день, до которого имеет смысл искать следующую пару: суббота
# восемнадцатой недели. Без границы поиск «что дальше» после конца семестра
# крутился бы вечно.
LAST_DAY = monday_of_week(LAST_WEEK) + timedelta(days=5)


def now_msk() -> datetime:
    """Текущий момент по UTC+3."""
    return datetime.now(MSK)


def slots_of(weekday: int) -> tuple[Slot, ...]:
    """Сетка пар для дня недели (0 - понедельник), как в файле расписания."""
    return SCHEDULE.get(weekday, ())


def occurrences(day: date) -> tuple[Occurrence, ...]:
    """Занятия конкретной даты, отсортированные по началу.

    Сортировка именно по фактическому началу, а не по порядку пар в сетке:
    физкультура сдвинута внутри своего слота, и порядок «по сетке» мог бы
    разойтись с порядком «по часам».
    """
    week = week_number(day)
    if not in_semester(week):
        return ()
    found: list[Occurrence] = []
    for slot in slots_of(day.weekday()):
        for lesson in slot.on_week(week):
            start, end = slot.bounds(lesson)
            found.append(
                Occurrence(
                    day=day,
                    week=week,
                    slot=slot,
                    lesson=lesson,
                    starts_at=combine(day, start),
                    ends_at=combine(day, end),
                )
            )
    found.sort(key=lambda occ: (occ.starts_at, occ.ends_at, occ.lesson.subject))
    return tuple(found)


def current(moment: datetime) -> tuple[Occurrence, ...]:
    """Пары, которые идут прямо сейчас.

    Их может быть несколько: в четверг в 10:10 в одной клетке стоят
    английский и немецкий - подгруппы расходятся по разным аудиториям.
    """
    return tuple(occ for occ in occurrences(moment.date()) if occ.contains(moment))


def upcoming(moment: datetime) -> tuple[Occurrence, ...]:
    """Ближайшая пара строго после moment - вместе со всеми параллельными.

    Идущая сейчас пара следующей не считается: у неё уже наступило начало,
    и вопрос «что скоро начнётся» про неё бессмыслен.
    """
    # Сканируем с начала семестра, а не с moment: при сбитых часах сервера
    # (или вопросе «что дальше» задолго до 1 сентября) цикл иначе перебирал
    # бы день за днём всё время до начала занятий.
    day = max(moment.date(), monday_of_week(1))
    while day <= LAST_DAY:
        todays = occurrences(day)
        later = [occ for occ in todays if occ.starts_at > moment]
        if later:
            first = later[0].starts_at
            return tuple(occ for occ in later if occ.starts_at == first)
        day += timedelta(days=1)
    return ()


@dataclass(frozen=True)
class Status:
    """Ответ на вопрос «что сейчас и что дальше»."""

    moment: datetime
    week: int
    current: tuple[Occurrence, ...]
    following: tuple[Occurrence, ...]
    # Все пары сегодняшнего дня. Без них нельзя отличить «пары кончились»
    # от «сегодня их и не было» и от «первая ещё не началась»: во всех трёх
    # случаях current пуст.
    today: tuple[Occurrence, ...] = ()

    @property
    def in_semester(self) -> bool:
        return in_semester(self.week)

    @property
    def minutes_left(self) -> int | None:
        """Сколько минут до конца текущей пары."""
        if not self.current:
            return None
        end = max(occ.ends_at for occ in self.current)
        return max(0, round((end - self.moment).total_seconds() / 60))

    @property
    def minutes_until(self) -> int | None:
        """Сколько минут до начала следующей пары."""
        if not self.following:
            return None
        start = self.following[0].starts_at
        return max(0, round((start - self.moment).total_seconds() / 60))

    @property
    def next_is_today(self) -> bool:
        return bool(self.following) and self.following[0].day == self.moment.date()


def status(moment: datetime) -> Status:
    """Срез расписания на момент moment."""
    todays = occurrences(moment.date())
    return Status(
        moment=moment,
        week=week_number(moment.date()),
        current=tuple(occ for occ in todays if occ.contains(moment)),
        following=upcoming(moment),
        today=todays,
    )


def week_plan(week: int) -> tuple[tuple[date, tuple[Occurrence, ...]], ...]:
    """Расписание всей учебной недели: пары дней с их занятиями.

    Дни без занятий остаются в списке с пустым кортежем - «в субботу пар
    нет» это ответ, а не отсутствие ответа.
    """
    monday = monday_of_week(week)
    return tuple(
        (monday + timedelta(days=shift), occurrences(monday + timedelta(days=shift)))
        for shift in range(6)
    )


def next_day_with_lessons(after: date) -> date | None:
    """Ближайший день строго после after, в котором есть пары."""
    day = after + timedelta(days=1)
    while day <= LAST_DAY:
        if occurrences(day):
            return day
        day += timedelta(days=1)
    return None

def parse_date(raw: str) -> date | None:
    """Разбор даты из команды /date: ДД.ММ.ГГГГ, ДД.ММ.ГГ или ДД.ММ.

    Живёт здесь, а не в хендлере: разбор пользовательского ввода -
    чистая логика, и тесты не должны ради неё ставить aiogram.
    """
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d.%m"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt == "%d.%m":
            # Без года подставляем тот, в котором эта дата попадает
            # в семестр: «15.09» - это сентябрь 2026, а «20.01» - январь 2027.
            first = monday_of_week(1)
            for year in (first.year, first.year + 1):
                try:
                    guess = parsed.date().replace(year=year)
                except ValueError:
                    continue
                if 1 <= week_number(guess) <= LAST_WEEK:
                    return guess
            # Ни один год не попал в семестр - выбираем по месяцу. Иначе
            # январь уезжал бы на год назад, и бот отвечал бы про другую
            # дату с другим днём недели, не показывая года в ответе.
            year = first.year if parsed.month >= first.month else first.year + 1
            try:
                return parsed.date().replace(year=year)
            except ValueError:
                return None
        return parsed.date()
    return None
