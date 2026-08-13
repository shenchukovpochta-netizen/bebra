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

    def test_error_texts_escape_operator_input(self):
        # Ошибка уходит в Telegram с parse_mode=HTML: сырое «<х>вилка»
        # валило бы отправку, и оператор получал бы тишину.
        data, err = fleet.parse_bike_form("рама: 1\n<х>вилка: 26")
        self.assertIsNone(data)
        self.assertNotIn("<х>", err)
        self.assertIn("&lt;х&gt;", err)
        data, err = fleet.parse_bike_form("рама: 1\nакб: <два>")
        self.assertIsNone(data)
        self.assertNotIn("<два>", err)

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

    def test_until_past_means_tomorrow(self):
        # «до 09:00» вечером - это до утра, а не ошибка.
        data, err = self.parse("велосипед: 3\nдо: 13:00")
        self.assertEqual(err, "")
        self.assertEqual(data["minutes"], 23 * 60)

    def test_until_and_hours_together_rejected(self):
        # Молчаливый победитель опасен: оператор думает про 5 часов,
        # а бронь живёт до «до».
        data, err = self.parse("велосипед: 3\nчасов: 5\nдо: 18:00")
        self.assertIsNone(data)
        self.assertIn("не вместе", err)

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


SAMPLE_FIXATION = """1. ФИО: Дмитриев Арсений Андреевич
2. Вин номер рамы: ZQV202483465602
3. Вин номер мотор колеса: 240W25022083
4. Комплектация:
   - АКБ: 2
   - ЗУ: 1
   - Зеркала: 0
   - Теплые перчатки на руль (муфты): 0
   - Педали: 0
   - Дождевик: 0
   - Чехол на держатель для телефона: 0
   - Троссовый замок: 0
   - Курьерская сумка: 0
   - Стяжка для крепления сумки: 0
5. Сроки аренды: 11.08 - 18.08
6. Номер телефона (основной): 89050238366
7. Номер телефона 2: 89053731217 друг
8. Номер телефона 3: 89050222012 мать
Ник в Telegram: @BlaiseBolasie
10. Сумма и способ оплаты: 3000qr
11. Адрес прописки с квартирой в Казани: Ул, Чистопольская 97б, кв6
12. Адрес проживания с квартирой в Казани: Ул. Лаврентьева д24а, кв 129
13. Подключен GPS-Трекер: да
14. Адрес сдачи: адо
15. Кто выдал: ирик
16. Подписка на тг: была
17. Реф.программа: подумает"""


class TestFixationForm(unittest.TestCase):
    def test_sample_form_parses_fully(self):
        data, warnings = fleet.parse_fixation_form(SAMPLE_FIXATION)
        self.assertEqual(warnings, [])
        self.assertEqual(data["fio"], "Дмитриев Арсений Андреевич")
        self.assertEqual(data["vin_frame"], "ZQV202483465602")
        self.assertEqual(data["vin_motor"], "240W25022083")
        self.assertEqual(data["rent_term"], "11.08 - 18.08")
        self.assertEqual(data["phone"], "+79050238366")
        self.assertEqual(data["phone2"], "+79053731217")
        self.assertEqual(data["phone2_note"], "друг")
        self.assertEqual(data["phone3"], "+79050222012")
        self.assertEqual(data["phone3_note"], "мать")
        self.assertEqual(data["tg_username"], "BlaiseBolasie")
        self.assertEqual(data["rent_price"], "3000qr")
        self.assertEqual(data["reg_address"], "Ул, Чистопольская 97б, кв6")
        self.assertEqual(data["live_address"], "Ул. Лаврентьева д24а, кв 129")
        self.assertIs(data["gps"], True)
        self.assertEqual(data["return_point"], "адо")
        self.assertEqual(data["issued_by"], "ирик")
        self.assertEqual(data["tg_subscribed"], "была")
        self.assertEqual(data["ref_program"], "подумает")
        self.assertEqual(data["kit"]["kit_akb"], 2)
        self.assertEqual(data["kit"]["kit_zu"], 1)
        self.assertEqual(data["kit"]["kit_lock"], 0)

    def test_numbering_is_optional(self):
        # «Ник в Telegram» в образце идёт без номера - и любые другие
        # строки тоже могут: разбор держится за метки, не за номера.
        data, _ = fleet.parse_fixation_form(
            "ФИО: Иванов Иван\nВин номер рамы: A1\nСроки аренды: 01.01 - 08.01")
        self.assertEqual(data["fio"], "Иванов Иван")
        self.assertEqual(data["vin_frame"], "A1")

    def test_matches_bot_generated_form(self):
        # Форма, собранная самим ботом (logic.fixation_form), обязана
        # разбираться обратно: это один формат, печатает его один код.
        text = logic.fixation_form(
            {"tg_id": 1, "full_name": "Петров Пётр", "phone": "+79001112233",
             "username": "petrov"},
            {"phone2": "+79002223344", "reg_address": "Казань, Баумана 1, кв 2",
             "live_address": "Казань, Баумана 1, кв 2"},
            {"vin_frame": "AB123", "vin_motor": "M9", "rent_term": "01.02 - 08.02",
             "rent_price": "3000 qr"})
        data, warnings = fleet.parse_fixation_form(text)
        self.assertEqual(warnings, [])
        self.assertEqual(data["fio"], "Петров Пётр")
        self.assertEqual(data["vin_frame"], "AB123")
        self.assertEqual(data["phone"], "+79001112233")
        self.assertEqual(data["tg_username"], "petrov")
        self.assertEqual(data["kit"]["kit_akb"], 2)

    def test_no_fio_is_fatal(self):
        data, errs = fleet.parse_fixation_form("Вин номер рамы: A1")
        self.assertIsNone(data)
        self.assertIn("ФИО", errs[0])

    def test_no_vin_is_fatal(self):
        data, errs = fleet.parse_fixation_form("ФИО: Иванов Иван")
        self.assertIsNone(data)
        self.assertIn("вин", errs[0])

    def test_unknown_lines_warn_but_do_not_stop(self):
        data, warnings = fleet.parse_fixation_form(
            "ФИО: Иванов Иван\nВин номер рамы: A1\nЦвет рамы: красный")
        self.assertIsNotNone(data)
        self.assertTrue(any("Цвет рамы" in w for w in warnings))

    def test_bad_phone_warns_and_keeps_text(self):
        data, warnings = fleet.parse_fixation_form(
            "ФИО: Иванов Иван\nВин номер рамы: A1\n"
            "Номер телефона (основной): спросить у мамы")
        self.assertNotIn("phone", data)
        self.assertEqual(data["phone_note"], "спросить у мамы")
        self.assertTrue(any("не похоже на номер" in w for w in warnings))

    def test_phone_with_spaces_and_digit_note(self):
        # Голова номера жадно ест пробелы и цифры; приписка с цифрой
        # («2 симка») не должна ломать распознавание номера.
        data, _ = fleet.parse_fixation_form(
            "ФИО: И И\nВин номер рамы: A1\n"
            "Номер телефона (основной): 8 905 023 83 66\n"
            "Номер телефона 2: 89053731217 2 симка")
        self.assertEqual(data["phone"], "+79050238366")
        self.assertEqual(data["phone2"], "+79053731217")
        self.assertEqual(data["phone2_note"], "2 симка")

    def test_label_prefix_does_not_swallow_other_lines(self):
        # «Рефлектор» не должен уехать в реф-программу, а «Фиокрест» -
        # затереть настоящее ФИО: метка требует границы слова.
        data, warnings = fleet.parse_fixation_form(
            "ФИО: Иванов Иван\nВин номер рамы: A1\n"
            "Рефлектор: сломан\nФиокрест: чей-то")
        self.assertEqual(data["fio"], "Иванов Иван")
        self.assertNotIn("ref_program", data)
        self.assertTrue(any("Рефлектор" in w for w in warnings))
        self.assertTrue(any("Фиокрест" in w for w in warnings))

    def test_gps_no_and_dash_values(self):
        data, _ = fleet.parse_fixation_form(
            "ФИО: И И\nВин номер рамы: A1\nПодключен GPS-Трекер: нет\n"
            "Адрес сдачи: —")
        self.assertIs(data["gps"], False)
        self.assertNotIn("return_point", data)

    def test_fixation_extra_collects_service_fields(self):
        data, _ = fleet.parse_fixation_form(SAMPLE_FIXATION)
        extra = fleet.fixation_extra(data)
        self.assertEqual(extra["issued_by"], "ирик")
        self.assertEqual(extra["return_point"], "адо")
        self.assertIs(extra["gps"], True)
        self.assertEqual(extra["phone2_note"], "друг")
        self.assertNotIn("fio", extra)


class TestPickup(unittest.TestCase):
    NOW = datetime(2026, 8, 12, 14, 0)

    def check(self, ts, **kw):
        kw.setdefault("now", self.NOW)
        kw.setdefault("open_hour", 10)
        kw.setdefault("close_hour", 19)
        return fleet.validate_pickup(ts, **kw)

    def ts(self, dt: datetime) -> int:
        return int(dt.timestamp())

    def test_future_working_hour_ok(self):
        pickup, err = self.check(self.ts(datetime(2026, 8, 12, 17, 0)))
        self.assertEqual(err, "")
        self.assertEqual((pickup.hour, pickup.minute), (17, 0))

    def test_clock_skew_tolerated(self):
        # «Сейчас» с телефона, часы которого отстают на пару минут.
        pickup, err = self.check(self.ts(self.NOW) - 120)
        self.assertEqual(err, "")

    def test_past_rejected(self):
        pickup, err = self.check(self.ts(datetime(2026, 8, 12, 11, 0)))
        self.assertIsNone(pickup)
        self.assertIn("прошло", err)

    def test_too_far_rejected(self):
        pickup, err = self.check(self.ts(datetime(2026, 8, 15, 12, 0)))
        self.assertIsNone(pickup)
        self.assertIn("вперёд", err)

    def test_outside_working_hours_rejected(self):
        pickup, err = self.check(self.ts(datetime(2026, 8, 13, 9, 0)))
        self.assertIsNone(pickup)
        self.assertIn("работают", err)

    def test_garbage_is_error_not_traceback(self):
        for raw in (None, "не число", 10**18, -1):
            pickup, err = self.check(raw)
            self.assertIsNone(pickup, raw)
            self.assertTrue(err, raw)

    def test_hold_minutes_cover_pickup_plus_grace(self):
        pickup = datetime(2026, 8, 12, 17, 0)
        minutes = fleet.hold_minutes_for_pickup(pickup, now=self.NOW)
        self.assertEqual(minutes, 180 + fleet.BOOKING_GRACE_MINUTES)
        # Визит «прямо сейчас» всё равно держит единицу на запас опоздания.
        self.assertEqual(fleet.hold_minutes_for_pickup(self.NOW, now=self.NOW),
                         fleet.BOOKING_GRACE_MINUTES)


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
        # Выдано в декабре «28.12 - 04.01»: конец раньше начала срока -
        # значит, это уже следующий год.
        self.assertEqual(fleet.parse_due("28.12 - 04.01", today=date(2026, 12, 28)),
                         date(2027, 1, 4))

    def test_expired_term_stays_this_year(self):
        # «28.07 - 08.08» в середине августа - просрочка на этой неделе,
        # а не срок до следующего лета: якорь переноса года - начало
        # срока, не «сегодня».
        self.assertEqual(fleet.parse_due("28.07 - 08.08", today=self.TODAY),
                         date(2026, 8, 8))

    def test_winter_form_imported_in_january(self):
        # Зимнюю форму «28.12 - 04.01» разбирают уже в январе: начало
        # срока не может быть позже импорта - значит, декабрь прошлого года,
        # а конец - январь текущего, а не через год.
        self.assertEqual(fleet.parse_due("28.12 - 04.01", today=date(2027, 1, 10)),
                         date(2027, 1, 4))


class TestBatteryAlert(unittest.TestCase):
    def test_alert_when_low_and_silent(self):
        self.assertEqual(fleet.battery_alert(15, notified=False), "alert")
        self.assertEqual(fleet.battery_alert(20, notified=False), "alert")

    def test_no_repeat_while_low(self):
        self.assertIsNone(fleet.battery_alert(15, notified=True))

    def test_hysteresis_between_thresholds(self):
        # 30% - выше порога тревоги, но ниже порога «зарядили»: ни нового
        # предупреждения, ни сброса - колебания напряжения не спамят.
        self.assertIsNone(fleet.battery_alert(30, notified=False))
        self.assertIsNone(fleet.battery_alert(30, notified=True))

    def test_clear_after_charge(self):
        self.assertEqual(fleet.battery_alert(80, notified=True), "clear")
        self.assertIsNone(fleet.battery_alert(80, notified=False))

    def test_silent_telemetry_changes_nothing(self):
        self.assertIsNone(fleet.battery_alert(None, notified=False))
        self.assertIsNone(fleet.battery_alert(None, notified=True))


class TestTermDays(unittest.TestCase):
    OPENED = datetime(2026, 8, 3, 12, 0)

    def test_from_term_dates(self):
        self.assertEqual(
            fleet.term_days("03.08 - 10.08", self.OPENED, date(2026, 8, 10)), 7)

    def test_term_dates_beat_shifted_due(self):
        # Аренду уже продлевали: due уехал, но шаг продления - длина
        # периода из текста срока, а не разница «выдача - новый due».
        self.assertEqual(
            fleet.term_days("03.08 - 10.08", self.OPENED, date(2026, 8, 17)), 7)

    def test_new_year_span(self):
        self.assertEqual(
            fleet.term_days("28.12 - 04.01", datetime(2026, 12, 28), None), 7)

    def test_fallback_to_due_minus_opened(self):
        self.assertEqual(
            fleet.term_days("до конца недели", self.OPENED, date(2026, 8, 10)), 7)

    def test_insane_spans_rejected(self):
        self.assertIsNone(fleet.term_days("03.08 - 03.08", self.OPENED, None))
        self.assertIsNone(
            fleet.term_days("03.08.2025 - 03.08.2026", self.OPENED, None))
        self.assertIsNone(fleet.term_days(None, None, date(2026, 8, 10)))


class TestExtensionOffer(unittest.TestCase):
    OPENED = datetime(2026, 8, 3, 12, 0)

    def test_full_offer(self):
        offer = fleet.extension_offer(
            rent_term="03.08 - 10.08", rent_price="3000qr",
            opened_at=self.OPENED, due_at=date(2026, 8, 10))
        self.assertEqual(offer, {"days": 7, "amount": 3000,
                                 "new_due": date(2026, 8, 17)})

    def test_no_price_no_offer(self):
        self.assertIsNone(fleet.extension_offer(
            rent_term="03.08 - 10.08", rent_price="перевод другу",
            opened_at=self.OPENED, due_at=date(2026, 8, 10)))

    def test_no_due_no_offer(self):
        # Срок не распознан - двигать нечего: продление без точки отсчёта
        # выставило бы счёт в никуда.
        self.assertIsNone(fleet.extension_offer(
            rent_term="до победы", rent_price="3000",
            opened_at=self.OPENED, due_at=None))


class TestWeeklyTrend(unittest.TestCase):
    TODAY = date(2026, 8, 13)                            # четверг

    def test_counts_by_week(self):
        rentals = [
            (datetime(2026, 8, 10, 12), None),           # эта неделя, живая
            (datetime(2026, 8, 4, 9), datetime(2026, 8, 11, 18)),
            (datetime(2026, 8, 5, 9), datetime(2026, 8, 6, 10)),
        ]
        rows = fleet.weekly_trend(rentals, weeks=2, today=self.TODAY)
        self.assertEqual([r["start"] for r in rows],
                         [date(2026, 8, 3), date(2026, 8, 10)])
        self.assertEqual([r["issued"] for r in rows], [2, 1])
        self.assertEqual([r["returned"] for r in rows], [1, 1])

    def test_old_events_out_of_window_ignored(self):
        rows = fleet.weekly_trend([(datetime(2020, 1, 1), None)],
                                  weeks=4, today=self.TODAY)
        self.assertEqual(sum(r["issued"] for r in rows), 0)

    def test_empty_weeks_stay_zero(self):
        rows = fleet.weekly_trend([], weeks=12, today=self.TODAY)
        self.assertEqual(len(rows), 12)
        self.assertTrue(all(r["issued"] == 0 and r["returned"] == 0
                            for r in rows))


class TestRevenueEstimate(unittest.TestCase):
    TODAY = date(2026, 8, 13)
    OPENED = datetime(2026, 8, 3, 12)

    def test_one_period_closed_in_time(self):
        self.assertEqual(fleet.revenue_estimate(
            "03.08 - 10.08", "3000qr", self.OPENED,
            datetime(2026, 8, 10, 11), today=self.TODAY), 3000)

    def test_live_rental_counts_to_today(self):
        # Живая аренда: 03.08 - 10.08, а сегодня 13-е - пошёл второй период.
        self.assertEqual(fleet.revenue_estimate(
            "03.08 - 10.08", "3000qr", self.OPENED, None,
            today=self.TODAY), 6000)

    def test_unknown_step_single_period(self):
        self.assertEqual(fleet.revenue_estimate(
            "до конца лета", "3000", self.OPENED, None,
            today=self.TODAY), 3000)

    def test_unparseable_price_none(self):
        self.assertIsNone(fleet.revenue_estimate(
            "03.08 - 10.08", "перевод другу", self.OPENED, None,
            today=self.TODAY))


class TestPayback(unittest.TestCase):
    def test_percent(self):
        self.assertEqual(fleet.payback_percent(30000, 60000), 50)
        self.assertEqual(fleet.payback_percent(60000, 60000), 100)
        self.assertEqual(fleet.payback_percent(90000, 60000), 150)

    def test_no_price_no_answer(self):
        self.assertIsNone(fleet.payback_percent(30000, None))
        self.assertIsNone(fleet.payback_percent(30000, 0))


class TestMonthlyRevenue(unittest.TestCase):
    TODAY = date(2026, 8, 13)

    def test_rental_splits_between_months(self):
        # 14 дней на стыке июля и августа, 7000 ₽: поровну по 500/день.
        items = [(datetime(2026, 7, 25, 12), datetime(2026, 8, 8, 12), 7000)]
        rows = fleet.monthly_revenue(items, months=3, today=self.TODAY)
        by = {r["start"].strftime("%m"): r["amount"] for r in rows}
        self.assertEqual(by["07"], 3500)                 # 25.07-31.07 = 7 дней
        self.assertEqual(by["08"], 3500)
        self.assertEqual(by["06"], 0)

    def test_live_rental_counts_until_today(self):
        items = [(datetime(2026, 8, 3, 12), None, 3000)]
        rows = fleet.monthly_revenue(items, months=2, today=self.TODAY)
        self.assertEqual(rows[-1]["amount"], 3000)

    def test_zero_revenue_ignored(self):
        rows = fleet.monthly_revenue(
            [(datetime(2026, 8, 3), None, 0)], months=2, today=self.TODAY)
        self.assertEqual(sum(r["amount"] for r in rows), 0)


class TestUtilization(unittest.TestCase):
    TODAY = date(2026, 8, 30)

    def test_half_loaded(self):
        # Один велосипед, 15 дней аренды в 30-дневном окне.
        rentals = [(datetime(2026, 8, 5), datetime(2026, 8, 20))]
        self.assertEqual(fleet.utilization_percent(
            rentals, 1, days=30, today=self.TODAY), 50)

    def test_live_rental_and_cap(self):
        # Аренда старше окна и живая: заполняет всё окно, но не больше 100%.
        rentals = [(datetime(2026, 1, 1), None)]
        self.assertEqual(fleet.utilization_percent(
            rentals, 1, days=30, today=self.TODAY), 100)

    def test_empty_park_is_none(self):
        self.assertIsNone(fleet.utilization_percent([], 0, days=30,
                                                    today=self.TODAY))


class TestLifecycle(unittest.TestCase):
    TODAY = date(2026, 8, 13)

    def test_old_bike_due_for_replacement(self):
        c = fleet.lifecycle(date(2025, 9, 1), 55000, 60000, today=self.TODAY)
        self.assertTrue(c["replace_due"])
        self.assertEqual(c["left_months"], 0.0)

    def test_young_bike_counts_down(self):
        c = fleet.lifecycle(date(2026, 1, 13), 42000, 60000, today=self.TODAY)
        self.assertFalse(c["replace_due"])
        self.assertAlmostEqual(c["age_months"], 7.0, delta=0.1)
        # 42000 за 7 мес = 6000/мес при нужных 6000/мес - успевает.
        self.assertEqual(c["need_month"], 6000)
        self.assertTrue(c["on_track"])

    def test_slow_bike_flagged(self):
        c = fleet.lifecycle(date(2026, 1, 13), 21000, 60000, today=self.TODAY)
        self.assertFalse(c["on_track"])

    def test_no_date_no_answer(self):
        self.assertIsNone(fleet.lifecycle(None, 1000, 60000, today=self.TODAY))
        # дата из будущего - мусор, а не отрицательный возраст
        self.assertIsNone(fleet.lifecycle(date(2027, 1, 1), 0, None,
                                          today=self.TODAY))

    def test_no_price_no_pace_but_age_works(self):
        c = fleet.lifecycle(date(2026, 6, 1), 9000, None, today=self.TODAY)
        self.assertIsNone(c["need_month"])
        self.assertIsNone(c["on_track"])
        self.assertIsNotNone(c["age_months"])


class TestBikeFormPrice(unittest.TestCase):
    def test_price_parsed_with_noise(self):
        data, err = fleet.parse_bike_form("рама: ABC1\nцена: 45 000р")
        self.assertEqual(err, "")
        self.assertEqual(data["purchase_price"], 45000)

    def test_price_without_digits_is_error(self):
        data, err = fleet.parse_bike_form("рама: ABC1\nцена: дорого")
        self.assertIsNone(data)
        self.assertIn("нужно число", err)


GOOD_REG_FORM = {
    "fio": "Иванов Иван Иванович",
    "phone": "89050238366",
    "birth_date": "07.03.1990",
    "birth_place": "гор. Казань",
    "passport_number": "1234567890",
    "passport_date": "01.02.2015",
    "passport_code": "160002",
    "passport_issuer": "МВД по Республике Татарстан",
    "reg_address": "Казань, ул. Чистопольская, д. 97б, кв. 6",
    "live_address": "Казань, ул. Лаврентьева, д. 24а, кв. 129",
    "phone2": "89053731217",
    "phone3": "89050222012",
}


class TestRegistrationForm(unittest.TestCase):
    TODAY = date(2026, 8, 12)

    def check(self, **overrides):
        return fleet.validate_registration({**GOOD_REG_FORM, **overrides},
                                           today=self.TODAY)

    def test_good_form_normalized_like_bot(self):
        clean, errors = self.check()
        self.assertEqual(errors, {})
        # Нормализация ровно та же, что у бота: телефоны к +7, паспорт
        # к «1234 567890», код к «160-002».
        self.assertEqual(clean["phone"], "+79050238366")
        self.assertEqual(clean["anketa"]["passport_number"], "1234 567890")
        self.assertEqual(clean["anketa"]["passport_code"], "160-002")
        self.assertEqual(clean["anketa"]["birth_date"], "07.03.1990")
        self.assertFalse(logic.missing_anketa_fields(clean["anketa"]))

    def test_errors_come_per_field_not_first_only(self):
        clean, errors = self.check(fio="X", passport_number="12",
                                   reg_address="коротко")
        self.assertIsNone(clean)
        self.assertEqual(set(errors),
                         {"fio", "passport_number", "reg_address"})

    def test_duplicate_phones_rejected(self):
        clean, errors = self.check(phone2="89050238366")
        self.assertIsNone(clean)
        self.assertIn("уже указан", errors["phone2"])

    def test_minor_detected_after_validation(self):
        clean, errors = self.check(birth_date="01.01.2010",
                                   passport_date="01.02.2025")
        self.assertEqual(errors, {})
        self.assertTrue(logic.is_minor(clean["anketa"], today=self.TODAY))

    def test_underage_rejected(self):
        clean, errors = self.check(birth_date="01.01.2015")
        self.assertIsNone(clean)
        self.assertIn("birth_date", errors)

    def test_passport_before_14_years_rejected(self):
        clean, errors = self.check(birth_date="07.03.2005",
                                   passport_date="01.02.2015")
        self.assertIsNone(clean)
        self.assertIn("passport_date", errors)


class TestService(unittest.TestCase):
    NOW = datetime(2026, 8, 12, 14, 0)

    def test_fresh_issue_has_full_interval(self):
        self.assertEqual(fleet.service_days_left(self.NOW, now=self.NOW), 14)
        self.assertFalse(fleet.service_due(self.NOW, now=self.NOW))

    def test_two_weeks_later_is_due(self):
        last = datetime(2026, 7, 29, 10, 0)
        self.assertLessEqual(fleet.service_days_left(last, now=self.NOW), 0)
        self.assertTrue(fleet.service_due(last, now=self.NOW))

    def test_overdue_is_negative(self):
        last = datetime(2026, 7, 23, 14, 0)          # 20 дней назад
        self.assertEqual(fleet.service_days_left(last, now=self.NOW), -6)

    def test_unknown_last_service_is_none(self):
        # Старые строки без даты ТО: напоминать не о чем, а не «просрочено
        # с начала времён».
        self.assertIsNone(fleet.service_days_left(None, now=self.NOW))
        self.assertFalse(fleet.service_due(None, now=self.NOW))


class TestOverdue(unittest.TestCase):
    TODAY = date(2026, 8, 12)

    def test_past_due_open_is_overdue(self):
        self.assertTrue(fleet.overdue(date(2026, 8, 8), None, today=self.TODAY))

    def test_future_due_is_not(self):
        self.assertFalse(fleet.overdue(date(2026, 8, 20), None, today=self.TODAY))

    def test_closed_is_not(self):
        self.assertFalse(fleet.overdue(date(2026, 8, 1), object(), today=self.TODAY))

    def test_no_due_is_not(self):
        # Срок не распознан - блокировать по незнанию нельзя.
        self.assertFalse(fleet.overdue(None, None, today=self.TODAY))

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
