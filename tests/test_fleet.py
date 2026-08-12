"""Чистая логика парка: статусы, формы оператора, сводки, срок брони.

Как test_logic.py: только stdlib, даты и время передаются параметрами.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import logic
from app.fleet import logic as fleet


class TestVin(unittest.TestCase):
    def test_spaces_collapse_and_upper(self):
        self.assertEqual(fleet.normalize_vin("  ab 12 3 "), "AB123")

    def test_same_vin_two_spellings_is_one_frame(self):
        self.assertEqual(fleet.normalize_vin("ab 123"), fleet.normalize_vin("AB123"))

    def test_empty(self):
        self.assertEqual(fleet.normalize_vin(None), "")
        self.assertEqual(fleet.normalize_vin("   "), "")


class TestBikeForm(unittest.TestCase):
    def test_full_form(self):
        data, err = fleet.parse_bike_form(
            "рама: ab 123\nмотор: m9\nмодель: Truck+\nточка: Адоратского\n"
            "акб: 2\nстатус: ремонт\nзаметка: скрипит цепь")
        self.assertEqual(err, "")
        self.assertEqual(data["vin_frame"], "AB123")
        self.assertEqual(data["vin_motor"], "M9")
        self.assertEqual(data["model"], "Truck+")
        self.assertEqual(data["battery_count"], 2)
        self.assertEqual(data["status"], fleet.SERVICE)
        self.assertEqual(data["notes"], "скрипит цепь")

    def test_vin_frame_required(self):
        data, err = fleet.parse_bike_form("модель: Truck+")
        self.assertIsNone(data)
        self.assertIn("рама", err)

    def test_unknown_key_is_loud(self):
        data, err = fleet.parse_bike_form("рама: 1\nколесо: 26")
        self.assertIsNone(data)
        self.assertIn("колесо", err)

    def test_battery_must_be_digit(self):
        data, err = fleet.parse_bike_form("рама: 1\nакб: два")
        self.assertIsNone(data)
        self.assertIn("число", err)

    def test_unknown_status_rejected(self):
        data, err = fleet.parse_bike_form("рама: 1\nстатус: сломан")
        self.assertIsNone(data)
        self.assertIn("Статус", err)

    def test_markup_rejected(self):
        data, err = fleet.parse_bike_form("рама: 1\nзаметка: <b>x</b>")
        self.assertIsNone(data)
        self.assertIn("недопустимы", err)

    def test_empty_values_skipped(self):
        data, err = fleet.parse_bike_form("рама: 1\nмотор: \nмодель: ")
        self.assertEqual(err, "")
        self.assertNotIn("vin_motor", data)
        self.assertNotIn("model", data)


class TestHoldForm(unittest.TestCase):
    NOW = datetime(2026, 8, 12, 14, 0)

    def parse(self, raw):
        return fleet.parse_hold_form(raw, now=self.NOW)

    def test_default_minutes(self):
        data, err = self.parse("велосипед: 3\nкто: Иван")
        self.assertEqual(err, "")
        self.assertEqual(data, {"bike_ref": "3", "minutes": fleet.HOLD_DEFAULT_MINUTES,
                                "note": "Иван"})

    def test_hours_plus_minutes(self):
        data, err = self.parse("велосипед: 3\nчасов: 2\nминут: 30")
        self.assertEqual(err, "")
        self.assertEqual(data["minutes"], 150)

    def test_until_future(self):
        data, err = self.parse("велосипед: 3\nдо: 18:30")
        self.assertEqual(err, "")
        self.assertEqual(data["minutes"], 270)

    def test_until_past_rejected(self):
        data, err = self.parse("велосипед: 3\nдо: 13:00")
        self.assertIsNone(data)
        self.assertIn("прошло", err)

    def test_until_bad_time(self):
        data, err = self.parse("велосипед: 3\nдо: 25:99")
        self.assertIsNone(data)

    def test_bike_required(self):
        data, err = self.parse("часов: 2")
        self.assertIsNone(data)
        self.assertIn("велосипед", err)

    def test_cap(self):
        data, err = self.parse("велосипед: 3\nчасов: 48")
        self.assertIsNone(data)
        self.assertIn("Удержание", err)

    def test_unknown_key_is_loud(self):
        data, err = self.parse("велосипед: 3\nсрок: завтра")
        self.assertIsNone(data)
        self.assertIn("срок", err)

    def test_vin_as_ref(self):
        data, err = self.parse("велосипед: AB 123")
        self.assertEqual(err, "")
        self.assertEqual(data["bike_ref"], "AB 123")


class TestCommands(unittest.TestCase):
    def test_plain_commands(self):
        for text in ("/park", "/bikes", "/bike\nрама: 1", "/hold\nвелосипед: 1",
                     "/unhold 3", "/service 3 скрипит", "/free 3"):
            self.assertTrue(fleet.is_fleet_command(text), text)

    def test_group_mention_suffix(self):
        # В группах Telegram дописывает @имя_бота - команда обязана узнаться.
        self.assertTrue(fleet.is_fleet_command("/park@mybike_bot"))
        self.assertEqual(fleet.command_name("/bikes@mybike_bot"), "bikes")

    def test_bikes_is_not_bike(self):
        self.assertEqual(fleet.command_name("/bikes"), "bikes")
        self.assertEqual(fleet.command_name("/bike"), "bike")

    def test_prefix_word_is_not_command(self):
        self.assertFalse(fleet.is_fleet_command("/parking"))
        self.assertFalse(fleet.is_fleet_command("привет"))
        self.assertFalse(fleet.is_fleet_command(None))

    def test_args_same_line_and_next_line(self):
        self.assertEqual(fleet.command_args("/unhold 3 передумал"), "3 передумал")
        self.assertEqual(fleet.command_args("/bike\nрама: 1"), "рама: 1")
        self.assertEqual(fleet.command_args("/park"), "")

    def test_should_process_lets_service_command_through(self):
        # Команда парка - обычное сообщение без реплая: без своего флага
        # она отбрасывалась бы, как когда-то кнопки «Одобрить».
        self.assertTrue(logic.should_process(
            "supergroup", from_admin_chat=True, is_moderation_callback=False,
            is_moderation_reply=False, is_service_command=True))
        self.assertFalse(logic.should_process(
            "supergroup", from_admin_chat=False, is_moderation_callback=False,
            is_moderation_reply=False, is_service_command=True))
        # Прежние вызовы без нового аргумента ведут себя как раньше.
        self.assertFalse(logic.should_process(
            "supergroup", from_admin_chat=True, is_moderation_callback=False))


class TestRefArgs(unittest.TestCase):
    def test_ref_and_note(self):
        self.assertEqual(fleet.parse_ref_args(" 3  скрипит цепь "), ("3", "скрипит цепь"))

    def test_ref_only(self):
        self.assertEqual(fleet.parse_ref_args("AB123"), ("AB123", ""))

    def test_empty(self):
        self.assertEqual(fleet.parse_ref_args(""), ("", ""))
        self.assertEqual(fleet.parse_ref_args(None), ("", ""))


class TestNeedsService(unittest.TestCase):
    def test_damage_sends_to_service(self):
        self.assertTrue(fleet.needs_service({"damage": "царапина на раме"}))
        self.assertTrue(fleet.needs_service({"damage": "1500"}))

    def test_no_damage_stays_free(self):
        for value in ("0", "-", "—", "нет", "", None):
            self.assertFalse(fleet.needs_service({"damage": value}), value)
        self.assertFalse(fleet.needs_service(None))
        self.assertFalse(fleet.needs_service({}))

    def test_matches_close_notes_criteria(self):
        # Критерий един с logic.close_notes: если акт говорит «повреждения
        # есть», единица не должна оставаться свободной, и наоборот.
        for value in ("0", "нет", "—", "-", "порвано седло"):
            close = {"damage": value}
            in_notes = "Повреждения" in logic.close_notes(close)
            self.assertEqual(fleet.needs_service(close), in_notes, value)


class TestDue(unittest.TestCase):
    TODAY = date(2026, 8, 12)

    def test_range_takes_last_date(self):
        self.assertEqual(fleet.parse_due("03.08 - 10.09", today=self.TODAY),
                         date(2026, 9, 10))

    def test_new_year_rollover(self):
        # Выдано в декабре «28.12 - 04.01»: конец в прошлом текущего года -
        # значит, это уже следующий год.
        self.assertEqual(fleet.parse_due("28.12 - 04.01", today=date(2026, 12, 28)),
                         date(2027, 1, 4))

    def test_explicit_year_kept(self):
        self.assertEqual(fleet.parse_due("до 10.01.2026", today=self.TODAY),
                         date(2026, 1, 10))

    def test_short_year(self):
        self.assertEqual(fleet.parse_due("10.09.26", today=self.TODAY),
                         date(2026, 9, 10))

    def test_free_text_is_none(self):
        self.assertIsNone(fleet.parse_due("до конца месяца", today=self.TODAY))
        self.assertIsNone(fleet.parse_due("", today=self.TODAY))
        self.assertIsNone(fleet.parse_due(None, today=self.TODAY))

    def test_impossible_date_is_none(self):
        self.assertIsNone(fleet.parse_due("32.13", today=self.TODAY))


class TestParkText(unittest.TestCase):
    def test_empty_park_has_hint(self):
        text = fleet.park_text([])
        self.assertIn("Парк пуст", text)
        self.assertIn("/bike", text)

    def test_grouping_and_totals(self):
        text = fleet.park_text([
            ("Адоратского, 11А", "Truck+", fleet.FREE, 3),
            ("Адоратского, 11А", "Truck+", fleet.RENTED, 2),
            (None, None, fleet.SERVICE, 1),
        ])
        self.assertIn("6 ед.", text)
        self.assertIn("Адоратского, 11А", text)
        self.assertIn("без точки", text)
        self.assertIn("без модели", text)
        self.assertIn("🟢 3", text)
        self.assertIn("🔵 2", text)
        self.assertIn("🔧 1", text)

    def test_zero_rows_dropped(self):
        text = fleet.park_text([("Точка", "Truck+", fleet.FREE, 0)])
        self.assertIn("Парк пуст", text)

    def test_titles_escaped(self):
        text = fleet.park_text([("<script>", "A&B", fleet.FREE, 1)])
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("A&amp;B", text)


class TestBikeLine(unittest.TestCase):
    def test_rented_shows_renter(self):
        line = fleet.bike_line({
            "id": 4, "model": "Truck+", "vin_frame": "AB123", "vin_motor": "M9",
            "status": fleet.RENTED, "point": "Адоратского, 11А",
            "renter_name": "Иванов Иван", "renter_username": "ivan",
        })
        self.assertIn("#4", line)
        self.assertIn("AB123", line)
        self.assertIn("Иванов Иван", line)
        self.assertIn("@ivan", line)

    def test_free_hides_renter(self):
        line = fleet.bike_line({
            "id": 4, "model": "Truck+", "vin_frame": "AB123",
            "status": fleet.FREE, "renter_name": "Иванов",
        })
        self.assertNotIn("Иванов", line)

    def test_booked_shows_hold_note(self):
        line = fleet.bike_line({
            "id": 4, "model": None, "vin_frame": "AB123",
            "status": fleet.BOOKED, "hold_note": "+7900, Иван",
        })
        self.assertIn("без модели", line)
        self.assertIn("+7900, Иван", line)

    def test_escapes_user_content(self):
        line = fleet.bike_line({
            "id": 1, "model": "T", "vin_frame": "V", "status": fleet.RENTED,
            "renter_name": "<b>x</b>", "renter_username": None,
        })
        self.assertNotIn("<b>x</b>", line)


class TestAvailabilityPayload(unittest.TestCase):
    def test_counts_merged_and_sorted(self):
        rows = [
            ("Б", "Truck+", fleet.FREE, 2),
            ("А", "Kugoo", fleet.RENTED, 1),
            ("Б", "Truck+", fleet.SERVICE, 1),
        ]
        payload = fleet.availability_payload(rows)
        self.assertEqual([p["point"] for p in payload], ["А", "Б"])
        b = payload[1]
        self.assertEqual(b["free"], 2)
        self.assertEqual(b["service"], 1)
        self.assertEqual(b["rented"], 0)

    def test_no_vins_or_people_in_payload(self):
        payload = fleet.availability_payload([("А", "M", fleet.FREE, 1)])
        keys = set(payload[0])
        self.assertEqual(keys, {"point", "model", "free", "booked", "rented",
                                "service", "lost"})


if __name__ == "__main__":
    unittest.main()
