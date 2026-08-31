"""Тесты бота расписания. Гоняются на голом stdlib, без aiogram:

    py -3 -m unittest discover -s tests -v

Импортируется только timetable.logic и ниже: timetable.bot тянет aiogram,
и тест, который нельзя запустить без установки зависимостей, не запускают.
"""

from __future__ import annotations

import re
import sys
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timetable import logic, texts  # noqa: E402
from timetable.data import SCHEDULE  # noqa: E402
from timetable.model import (  # noqa: E402
    EVEN_WEEK,
    LAST_WEEK,
    MSK,
    ODD_WEEK,
    Lesson,
    in_semester,
    monday_of_week,
    pair_number,
    week_number,
)


def moment(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=MSK)


class TestWeekNumbers(unittest.TestCase):
    """Отсчёт недель с 1 сентября - главное требование к боту."""

    def test_first_week_contains_first_september(self):
        self.assertEqual(week_number(date(2026, 9, 1)), 1)

    def test_week_starts_on_monday(self):
        # 01.09.2026 - вторник, поэтому неделя №1 начинается 31.08.
        self.assertEqual(monday_of_week(1), date(2026, 8, 31))
        self.assertEqual(week_number(date(2026, 8, 31)), 1)
        self.assertEqual(week_number(date(2026, 9, 6)), 1)

    def test_second_week(self):
        self.assertEqual(week_number(date(2026, 9, 7)), 2)
        self.assertEqual(monday_of_week(2), date(2026, 9, 7))

    def test_before_semester(self):
        self.assertEqual(week_number(date(2026, 8, 30)), 0)
        self.assertFalse(in_semester(week_number(date(2026, 8, 30))))

    def test_last_week(self):
        last_monday = monday_of_week(LAST_WEEK)
        self.assertEqual(week_number(last_monday), LAST_WEEK)
        self.assertEqual(week_number(last_monday + timedelta(days=7)), LAST_WEEK + 1)
        self.assertFalse(in_semester(LAST_WEEK + 1))

    def test_every_week_starts_on_monday(self):
        for week in range(1, LAST_WEEK + 1):
            self.assertEqual(monday_of_week(week).weekday(), 0, f"неделя {week}")

    def test_parity_matches_number(self):
        # «ч.н.» в расписании - это чётный номер учебной недели.
        self.assertTrue(week_number(monday_of_week(4)) % 2 == 0)


class TestLessonWeeks(unittest.TestCase):
    def test_inside_range(self):
        lesson = Lesson("Физика", first_week=2, last_week=9)
        self.assertFalse(lesson.happens_on(1))
        self.assertTrue(lesson.happens_on(2))
        self.assertTrue(lesson.happens_on(9))
        self.assertFalse(lesson.happens_on(10))

    def test_even_weeks_only(self):
        lesson = Lesson("Лаб", first_week=1, last_week=18, parity=EVEN_WEEK)
        self.assertFalse(lesson.happens_on(3))
        self.assertTrue(lesson.happens_on(4))

    def test_odd_weeks_only(self):
        lesson = Lesson("Лаб", first_week=1, last_week=18, parity=ODD_WEEK)
        self.assertTrue(lesson.happens_on(3))
        self.assertFalse(lesson.happens_on(4))


class TestGrid(unittest.TestCase):
    def test_pair_numbers(self):
        self.assertEqual(pair_number(time(8, 30)), 1)
        self.assertEqual(pair_number(time(13, 50)), 4)

    def test_unknown_time_has_no_number(self):
        self.assertIsNone(pair_number(time(14, 0)))


class TestOccurrences(unittest.TestCase):
    def test_saturday_is_empty(self):
        # 05.09.2026 - суббота первой недели: у 06-445 суббот нет вовсе.
        self.assertEqual(logic.occurrences(date(2026, 9, 5)), ())

    def test_sunday_is_empty(self):
        self.assertEqual(logic.occurrences(date(2026, 9, 6)), ())

    def test_first_monday_is_empty(self):
        # Все понедельничные занятия идут с «2н»: понедельник первой недели
        # (31.08) - день до начала семестра, пар в нём нет.
        self.assertEqual(logic.occurrences(date(2026, 8, 31)), ())

    def test_second_monday_has_lessons(self):
        # Схемотехника в 10:10 идёт только с 10-й недели, поэтому на второй
        # неделе понедельник состоит из четырёх пар, а не из пяти.
        occs = logic.occurrences(date(2026, 9, 7))
        self.assertEqual([o.slot.start.hour for o in occs], [8, 12, 13, 15])
        self.assertEqual(occs[0].lesson.subject, "Экономика")

    def test_schemotechnics_appears_on_week_ten(self):
        occs = logic.occurrences(monday_of_week(10))
        self.assertEqual([o.slot.start.hour for o in occs], [8, 10, 12, 13, 15])
        self.assertEqual(occs[1].lesson.subject, "Аналоговая и цифровая схемотехника")

    def test_outside_semester_is_empty(self):
        self.assertEqual(logic.occurrences(date(2026, 8, 25)), ())
        self.assertEqual(logic.occurrences(monday_of_week(LAST_WEEK) + timedelta(days=7)), ())

    def test_sorted_by_start(self):
        occs = logic.occurrences(date(2026, 9, 8))
        starts = [o.starts_at for o in occs]
        self.assertEqual(starts, sorted(starts))

    def test_lessons_swap_mid_semester(self):
        # Понедельник, 12:10: до 9-й недели физика, с 10-й - нанотехнологии.
        early = logic.occurrences(monday_of_week(9))
        late = logic.occurrences(monday_of_week(10))
        at = lambda occs: next(o.lesson.subject for o in occs if o.slot.start == time(12, 10))
        self.assertEqual(at(early), "Физика")
        self.assertTrue(at(late).startswith("Физико-химические основы"))


class TestCurrentAndNext(unittest.TestCase):
    """«Какая сейчас пара и какая скоро начнётся» - по московскому времени."""

    def test_during_a_lesson(self):
        st = logic.status(moment(2026, 9, 14, 15, 12))
        self.assertEqual(len(st.current), 1)
        self.assertEqual(st.current[0].lesson.subject, "Введение в квантовую физику")
        self.assertEqual(st.minutes_left, 8)
        self.assertEqual(st.following[0].lesson.kind, "пр")
        self.assertEqual(st.minutes_until, 38)

    def test_break_between_lessons(self):
        st = logic.status(moment(2026, 9, 14, 15, 30))
        self.assertEqual(st.current, ())
        self.assertTrue(st.next_is_today)
        self.assertEqual(st.minutes_until, 20)

    def test_lesson_start_counts_as_current(self):
        st = logic.status(moment(2026, 9, 14, 13, 50))
        self.assertEqual(len(st.current), 1)
        self.assertEqual(st.minutes_left, 90)

    def test_lesson_end_is_not_current(self):
        # В 15:20 пара уже кончилась: конец интервала исключён, иначе бот
        # звал бы на пару, из которой все вышли.
        st = logic.status(moment(2026, 9, 14, 15, 20))
        self.assertEqual(st.current, ())

    def test_after_last_lesson_next_is_tomorrow(self):
        st = logic.status(moment(2026, 9, 14, 20, 0))
        self.assertEqual(st.current, ())
        self.assertFalse(st.next_is_today)
        self.assertEqual(st.following[0].day, date(2026, 9, 15))
        self.assertEqual(st.following[0].lesson.subject, "Планирование и обработка эксперимента")

    def test_weekend_next_is_monday(self):
        # Воскресенье: ближайшая пара - в понедельник в 08:30.
        st = logic.status(moment(2026, 9, 13, 12, 0))
        self.assertEqual(st.current, ())
        self.assertEqual(st.following[0].day, date(2026, 9, 14))
        self.assertEqual(st.following[0].starts_at.hour, 8)

    def test_parallel_lessons_are_both_current(self):
        # Четверг 10:10: английский и немецкий стоят в одной клетке.
        st = logic.status(moment(2026, 9, 10, 10, 30))
        self.assertEqual(len(st.current), 2)

    def test_after_semester_nothing_follows(self):
        st = logic.status(datetime.combine(
            monday_of_week(LAST_WEEK) + timedelta(days=8), time(9, 0), tzinfo=MSK))
        self.assertEqual(st.current, ())
        self.assertEqual(st.following, ())
        self.assertFalse(st.in_semester)

    def test_before_semester_first_lesson_follows(self):
        st = logic.status(moment(2026, 8, 30, 12, 0))
        self.assertFalse(st.in_semester)
        self.assertEqual(st.following[0].day, date(2026, 9, 1))


class TestPhysicalEducation(unittest.TestCase):
    """Физкультура идёт 14:00-15:30, хотя стоит в слоте 13:50-15:20."""

    def test_not_started_at_slot_time(self):
        st = logic.status(moment(2026, 9, 15, 13, 55))
        self.assertEqual(st.current, ())
        self.assertEqual(st.minutes_until, 5)

    def test_running_at_real_time(self):
        st = logic.status(moment(2026, 9, 15, 15, 25))
        self.assertEqual(len(st.current), 1)
        self.assertTrue(st.current[0].lesson.subject.startswith("Элективные курсы"))

    def test_real_bounds(self):
        occ = next(o for o in logic.occurrences(date(2026, 9, 15))
                   if o.lesson.subject.startswith("Элективные"))
        self.assertEqual((occ.starts_at.hour, occ.starts_at.minute), (14, 0))
        self.assertEqual((occ.ends_at.hour, occ.ends_at.minute), (15, 30))


class TestTimezone(unittest.TestCase):
    def test_offset_is_utc_plus_three(self):
        self.assertEqual(MSK.utcoffset(None), timedelta(hours=3))

    def test_occurrences_are_aware(self):
        occ = logic.occurrences(date(2026, 9, 7))[0]
        self.assertIsNotNone(occ.starts_at.tzinfo)
        # Пара 08:30 по Москве - это 05:30 UTC.
        self.assertEqual(occ.starts_at.utctimetuple().tm_hour, 5)


class TestWeekPlan(unittest.TestCase):
    def test_six_days(self):
        plan = logic.week_plan(3)
        self.assertEqual(len(plan), 6)
        self.assertEqual(plan[0][0].weekday(), 0)
        self.assertEqual(plan[-1][0].weekday(), 5)

    def test_saturday_present_but_empty(self):
        plan = logic.week_plan(3)
        self.assertEqual(plan[5][1], ())

    def test_next_day_with_lessons_skips_saturday(self):
        self.assertEqual(logic.next_day_with_lessons(date(2026, 9, 11)), date(2026, 9, 14))


class TestRendering(unittest.TestCase):
    def test_escaping(self):
        self.assertEqual(texts.esc("a & <b>"), "a &amp; &lt;b&gt;")

    def test_none_is_empty(self):
        self.assertEqual(texts.esc(None), "")

    def test_minutes(self):
        self.assertEqual(texts.minutes(5), "5 мин")
        self.assertEqual(texts.minutes(60), "1 ч")
        self.assertEqual(texts.minutes(85), "1 ч 25 мин")

    def test_empty_day_says_so(self):
        answer = texts.day_answer(date(2026, 9, 5), logic.occurrences(date(2026, 9, 5)))
        self.assertIn("Пар нет", answer)

    def test_parallel_lessons_share_one_time_header(self):
        # Четверг, 10:10: английский и немецкий - одна пара, а не две подряд.
        day = date(2026, 9, 10)
        answer = texts.day_answer(day, logic.occurrences(day))
        self.assertEqual(answer.count("2-я пара"), 1)
        self.assertIn("немецкий язык", answer)
        self.assertIn("Хованская Е.С.", answer)

    def test_day_answer_lists_every_lesson(self):
        day = date(2026, 9, 9)
        answer = texts.day_answer(day, logic.occurrences(day))
        for occ in logic.occurrences(day):
            self.assertIn(texts.esc(occ.lesson.subject), answer)

    def test_answers_fit_telegram_limit(self):
        """Ни один ответ бота не должен превышать лимит после разбиения."""
        checked = 0
        for week in range(1, LAST_WEEK + 1):
            for chunk in texts.split_message(texts.week_answer(week, logic.week_plan(week))):
                self.assertLessEqual(len(chunk), 4096)
                checked += 1
            monday = monday_of_week(week)
            for shift in range(6):
                day = monday + timedelta(days=shift)
                for chunk in texts.split_message(texts.day_answer(day, logic.occurrences(day))):
                    self.assertLessEqual(len(chunk), 4096)
                    checked += 1
        self.assertGreater(checked, 0)

    def test_split_keeps_all_blocks(self):
        text = "\n\n".join(f"блок {i} " + "x" * 200 for i in range(40))
        parts = texts.split_message(text)
        self.assertGreater(len(parts), 1)
        for i in range(40):
            self.assertTrue(any(f"блок {i} " in p for p in parts), f"потерян блок {i}")

    def test_short_text_is_not_split(self):
        self.assertEqual(texts.split_message("коротко"), ["коротко"])

    def test_tags_are_balanced(self):
        """Разбиение не должно рвать HTML-теги: Telegram отвергнет часть."""
        for week in range(1, LAST_WEEK + 1):
            for chunk in texts.split_message(texts.week_answer(week, logic.week_plan(week))):
                self.assertEqual(chunk.count("<b>"), chunk.count("</b>"))
                self.assertEqual(chunk.count("<i>"), chunk.count("</i>"))


class TestDataMatchesSource(unittest.TestCase):
    """Сверка разобранного расписания с исходным текстом клеток xlsx.

    Ловит опечатку при переносе: предмет, преподаватель, аудитория или
    диапазон недель, которых в исходной клетке нет.
    """

    @staticmethod
    def flat(text: str) -> str:
        return re.sub(r"\s+", "", text or "")

    def test_subject_teacher_room_come_from_source(self):
        for weekday, slots in SCHEDULE.items():
            for slot in slots:
                source = self.flat(slot.raw)
                for lesson in slot.lessons:
                    where = f"день {weekday}, {slot.start}, {lesson.subject}"
                    for field in ("subject", "teacher", "room"):
                        value = self.flat(getattr(lesson, field))
                        if value:
                            self.assertIn(value, source, f"{where}: {field} нет в исходнике")

    def test_week_ranges_come_from_source(self):
        for weekday, slots in SCHEDULE.items():
            for slot in slots:
                found = re.findall(r"(\d+)-(\d+)н", self.flat(slot.raw))
                ranges = {(int(a), int(b)) for a, b in found}
                for lesson in slot.lessons:
                    where = f"день {weekday}, {slot.start}, {lesson.subject}"
                    if ranges:
                        self.assertIn((lesson.first_week, lesson.last_week), ranges,
                                      f"{where}: такого диапазона недель в клетке нет")
                    else:
                        # Недели в клетке не указаны - значит весь семестр.
                        self.assertEqual((lesson.first_week, lesson.last_week), (1, LAST_WEEK),
                                         f"{where}: недель в клетке нет, ожидался весь семестр")

    def test_empty_cells_have_no_lessons(self):
        for slots in SCHEDULE.values():
            for slot in slots:
                if not slot.raw.strip():
                    self.assertEqual(slot.lessons, ())

    def test_weeks_are_sane(self):
        for slots in SCHEDULE.values():
            for slot in slots:
                for lesson in slot.lessons:
                    self.assertLessEqual(lesson.first_week, lesson.last_week)
                    self.assertGreaterEqual(lesson.first_week, 1)
                    self.assertLessEqual(lesson.last_week, LAST_WEEK)

    def test_slots_do_not_overlap(self):
        for weekday, slots in SCHEDULE.items():
            for slot in slots:
                self.assertLess(slot.start, slot.end, f"день {weekday}, слот {slot.start}")
            starts = [slot.start for slot in slots]
            self.assertEqual(starts, sorted(starts), f"день {weekday}: слоты не по порядку")

    def test_every_weekday_present(self):
        self.assertEqual(sorted(SCHEDULE), [0, 1, 2, 3, 4, 5])


if __name__ == "__main__":
    unittest.main()
