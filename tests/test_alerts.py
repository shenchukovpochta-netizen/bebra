"""Тревога как задача: уровень, состояние, действия.

Тревога была отметкой: подняли — сняли. Ложная и разобранная выглядели
одинаково, и понять, чем занимался оператор, было нельзя.

Главное правило здесь: «это норма» — не закрытие. Тревога остаётся
открытой, поэтому второй раз она не поднимается, пока причина держится,
а исчезнет причина — опрос закроет её сам. Так норма сама себя убирает.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def alert(**over):
    row = {"id": 1, "tracker_id": 1, "kind": "offline", "level": "yellow",
           "state": "new", "handled_at": None, "snooze_until": None,
           "created_at": NOW}
    row.update(over)
    return row


class TestAlertLevels(unittest.TestCase):
    def test_moving_and_alarm_are_urgent(self):
        self.assertEqual(logic.alert_level("moving"), "urgent")
        self.assertEqual(logic.alert_level("alarm"), "urgent")

    def test_the_rest_waits_its_turn(self):
        self.assertEqual(logic.alert_level("offline"), "yellow")
        self.assertEqual(logic.alert_level("low_power"), "yellow")
        self.assertEqual(logic.alert_level("idle_rented"), "yellow")

    def test_idle_rented_is_a_known_kind(self):
        self.assertIn("idle_rented", logic.TRACKER_ALERTS)


class TestAlertRows(unittest.TestCase):
    def test_new_needs_attention(self):
        row = logic.alert_rows([alert()], now=NOW)[0]
        self.assertTrue(row["needs"])
        self.assertTrue(row["open"])
        self.assertEqual(row["title"], "Не выходит на связь")

    def test_normal_drops_out_of_the_list(self):
        row = logic.alert_rows([alert(state="normal")], now=NOW)[0]
        self.assertFalse(row["needs"], "ради этого её и отмечали")
        self.assertTrue(row["open"], "но остаётся открытой: иначе поднимется снова")

    def test_snoozed_comes_back_when_the_time_is_up(self):
        later = logic.alert_rows([alert(state="snoozed",
                                        snooze_until=NOW + timedelta(hours=2))],
                                 now=NOW)[0]
        self.assertFalse(later["needs"])
        due = logic.alert_rows([alert(state="snoozed",
                                      snooze_until=NOW - timedelta(minutes=1))],
                               now=NOW)[0]
        self.assertTrue(due["needs"], "срок вышел - снова в списке")

    def test_closed_needs_nothing(self):
        row = logic.alert_rows([alert(handled_at=NOW)], now=NOW)[0]
        self.assertFalse(row["open"])
        self.assertFalse(row["needs"])

    def test_urgent_comes_first(self):
        rows = logic.alert_rows([alert(id=1, kind="offline", level="yellow"),
                                 alert(id=2, kind="moving", level="urgent")],
                                now=NOW)
        self.assertEqual(rows[0]["id"], 2)

    def test_summary_counts_each_state(self):
        got = logic.alert_summary(logic.alert_rows([
            alert(id=1, kind="moving", level="urgent"),
            alert(id=2, kind="offline", state="working"),
            alert(id=3, kind="low_power", state="snoozed",
                  snooze_until=NOW + timedelta(hours=3)),
            alert(id=4, kind="idle_rented", state="normal"),
        ], now=NOW))
        self.assertEqual(got["open"], 4)
        self.assertEqual(got["urgent"], 1)
        self.assertEqual(got["working"], 1)
        self.assertEqual(got["snoozed"], 1)
        self.assertEqual(got["normal"], 1)
        self.assertEqual(got["needs"], 2, "отложенная и норма не в счёт")

    def test_snooze_until_is_clamped(self):
        self.assertEqual(logic.snooze_until(4, now=NOW), NOW + timedelta(hours=4))
        self.assertEqual(logic.snooze_until("", now=NOW),
                         NOW + timedelta(hours=logic.ALERT_SNOOZE_HOURS))
        self.assertEqual(logic.snooze_until(1000, now=NOW), NOW + timedelta(hours=72))
        self.assertEqual(logic.snooze_until(0, now=NOW), NOW + timedelta(hours=1))


class TestIdleRented(unittest.TestCase):
    """Оплаченный велосипед, который стоит."""

    def tracker(self, **over):
        row = {"id": 1, "device_id": "1", "last_seen": NOW - timedelta(minutes=5),
               "speed": D(0), "voltage": D("12.6"), "alarm": False,
               "rental_id": 7, "bike_id": 3, "bike_status": "rented",
               "moved_at": NOW - timedelta(days=5), "lat": 55.8, "lon": 49.1}
        row.update(over)
        return row

    def test_standing_rented_bike_raises_it(self):
        row = logic.tracker_rows([self.tracker()], now=NOW)[0]
        self.assertTrue(row["idle_rented"])
        kinds = {a["kind"] for a in logic.detect_alerts(row)}
        self.assertIn("idle_rented", kinds)

    def test_a_moving_one_does_not(self):
        row = logic.tracker_rows(
            [self.tracker(moved_at=NOW - timedelta(hours=2))], now=NOW)[0]
        self.assertFalse(row["idle_rented"])

    def test_a_free_bike_does_not(self):
        row = logic.tracker_rows([self.tracker(rental_id=None)], now=NOW)[0]
        self.assertFalse(row["idle_rented"],
                         "свободный велосипед и должен стоять")

    def test_a_silent_tracker_does_not(self):
        row = logic.tracker_rows(
            [self.tracker(last_seen=NOW - timedelta(days=3))], now=NOW)[0]
        self.assertFalse(row["idle_rented"],
                         "про молчащий трекер уже есть своя тревога")

    def test_without_a_movement_mark_we_stay_quiet(self):
        row = logic.tracker_rows([self.tracker(moved_at=None)], now=NOW)[0]
        self.assertFalse(row["idle_rented"],
                         "трекер только что завели - обвинять его не в чем")

    def test_threshold_is_a_setting(self):
        row = logic.tracker_rows([self.tracker(moved_at=NOW - timedelta(days=2))],
                                 now=NOW, settings={"tracker_idle_days": "1"})[0]
        self.assertTrue(row["idle_rented"])


class TestDigest(unittest.TestCase):
    def test_urgent_first_and_counted(self):
        text = logic.tracker_digest([
            {"kind": "offline", "level": "yellow", "bike_code": "B-1"},
            {"kind": "moving", "level": "urgent", "bike_code": "B-2"}])
        self.assertIn("срочных 1", text)
        lines = text.splitlines()
        self.assertIn("B-2", lines[1], "в чате читают первые три строки")
        self.assertIn("🔴", lines[1])


class TestTrackerLimits(unittest.TestCase):
    """Пороги тревог из панели. M13 на электровелосипеде питается от
    тяговой батареи: порог «питания» для машины (11,5 В) у него не
    сработает никогда, а ноль должен значить «не следить»."""

    def form(self, **over):
        data = {"tracker_offline_hours": "12", "tracker_low_volts": "40",
                "tracker_moving_speed": "5", "tracker_idle_days": "3"}
        data.update(over)
        return data

    def test_form_values_are_checked(self):
        values, error = logic.check_tracker_limits(self.form(tracker_low_volts="42,5"))
        self.assertIsNone(error)
        self.assertEqual(values["tracker_low_volts"], "42.5")
        self.assertEqual(values["tracker_offline_hours"], "12")
        # 60 - это «60», а не «6E+1», которое потом не прочтёт никто
        values, _ = logic.check_tracker_limits(self.form(tracker_moving_speed="60"))
        self.assertEqual(values["tracker_moving_speed"], "60")
        for bad in ({"tracker_offline_hours": "0"}, {"tracker_offline_hours": "1.5"},
                    {"tracker_offline_hours": "500"}, {"tracker_low_volts": "-1"},
                    {"tracker_low_volts": "abc"}, {"tracker_moving_speed": "0"},
                    {"tracker_idle_days": ""}, {"tracker_low_volts": "NaN"}):
            values, error = logic.check_tracker_limits(self.form(**bad))
            self.assertEqual(values, {}, bad)
            self.assertIsNotNone(error, bad)

    def test_zero_volts_means_do_not_watch(self):
        limits = logic.tracker_settings({"tracker_low_volts": "0"})
        self.assertEqual(limits["low_volts"], D(0))
        row = {"id": 1, "device_id": "1", "voltage": D("30.5"),
               "last_seen": NOW - timedelta(minutes=5), "speed": D(0)}
        watched = logic.tracker_rows([row], now=NOW,
                                     settings={"tracker_low_volts": "40"})[0]
        self.assertTrue(watched["low_power"])
        off = logic.tracker_rows([row], now=NOW,
                                 settings={"tracker_low_volts": "0"})[0]
        self.assertFalse(off["low_power"])
        self.assertNotIn("low_power",
                         [a["kind"] for a in logic.detect_alerts(
                             off, settings={"tracker_low_volts": "0"})])
        # ноль у остальных порогов бессмыслен - берётся умолчание
        self.assertEqual(logic.tracker_settings({"tracker_offline_hours": "0"})
                         ["offline_hours"], logic.TRACKER_OFFLINE_HOURS)

    def test_offline_threshold_follows_the_setting(self):
        row = {"id": 1, "device_id": "1", "last_seen": NOW - timedelta(hours=20),
               "speed": D(0)}
        self.assertTrue(logic.tracker_rows([row], now=NOW)[0]["offline"])
        self.assertFalse(logic.tracker_rows(
            [row], now=NOW, settings={"tracker_offline_hours": "24"})[0]["offline"])


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestAlertsPage(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.bike_id = _run(self.crm.create_bike(code="B-1", model="Kugoo V3"))
        self.tracker_id = _run(self.crm.create_tracker(
            device_id="1001", alias="Метка 1", bike_id=self.bike_id))
        self.alert_id = _run(self.crm.raise_alert(
            tracker_id=self.tracker_id, kind="moving", note="30 км/ч",
            bike_id=self.bike_id, lat=55.8, lon=49.1, level="urgent"))

    def state(self):
        return _run(self.crm.tracker_alert(self.alert_id))

    def listed(self, url):
        """Строки таблицы, а не вся страница: названия видов тревог есть
        и в выпадающем фильтре, и по ним проверять нельзя."""
        import re
        text = self.get_ok(url)
        body = text.split("<table>", 1)[-1].split("</table>", 1)[0]
        return set(re.findall(r'action="/alerts/(\d+)"', body))

    def act(self, action, **data):
        return self.client.post(f"/alerts/{self.alert_id}",
                                data={"action": action, **data})

    def test_page_shows_the_alert(self):
        self.assertIn(str(self.alert_id), self.listed("/alerts"))

    def test_thresholds_are_saved_from_the_page(self):
        page = self.get_ok("/alerts")
        self.assertIn("Пороги тревог", page)
        self.assertIn('name="tracker_low_volts" value="11.5"', page)
        r = self.client.post("/alerts/settings", data={
            "tracker_offline_hours": "24", "tracker_low_volts": "0",
            "tracker_moving_speed": "7", "tracker_idle_days": "2"})
        self.assertEqual(r.status_code, 303)
        settings = _run(self.crm.settings())
        self.assertEqual((settings["tracker_offline_hours"], settings["tracker_low_volts"],
                          settings["tracker_moving_speed"], settings["tracker_idle_days"]),
                         ("24", "0", "7", "2"))
        page = self.get_ok("/alerts")
        self.assertIn("Пороги тревог сохранены", page)
        self.assertIn('name="tracker_offline_hours" value="24"', page)

    def test_bad_threshold_saves_nothing(self):
        self.client.post("/alerts/settings", data={
            "tracker_offline_hours": "24", "tracker_low_volts": "сорок",
            "tracker_moving_speed": "7", "tracker_idle_days": "2"})
        self.assertIn("Питание ниже, В", self.get_ok("/alerts"))
        self.assertNotIn("tracker_offline_hours", _run(self.crm.settings()),
                         "половину порогов не сохраняем")

    def test_manager_sees_alerts_but_not_thresholds(self):
        manager = _run(self.crm.access_profile_by_code("manager"))
        _run(self.crm.create_staff("olga", logic.hash_password("password-1"), "Ольга",
                                   "manager", profile_id=manager["id"]))
        self.client.post("/logout")
        self.assertEqual(self.login("olga", "password-1").status_code, 303)
        self.assertNotIn("Пороги тревог", self.get_ok("/alerts"))
        r = self.client.post("/alerts/settings", data={
            "tracker_offline_hours": "1", "tracker_low_volts": "0",
            "tracker_moving_speed": "7", "tracker_idle_days": "2"})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("tracker_offline_hours", _run(self.crm.settings()))
        self.assertIn("30 км/ч", self.get_ok("/alerts"))

    def test_take_it(self):
        self.act("take")
        self.assertEqual(self.state()["state"], "working")
        self.assertTrue(self.state()["taken_by"])

    def test_snooze_drops_it_from_the_list(self):
        self.act("snooze", hours="4")
        row = self.state()
        self.assertEqual(row["state"], "snoozed")
        self.assertIsNotNone(row["snooze_until"])
        self.assertNotIn(str(self.alert_id), self.listed("/alerts?view=needs"))
        self.assertIn(str(self.alert_id), self.listed("/alerts?view=open"))

    def test_normal_keeps_it_open(self):
        self.act("normal")
        row = self.state()
        self.assertEqual(row["state"], "normal")
        self.assertIsNone(row["handled_at"],
                          "закрыть её значило бы поднять заново на следующем опросе")
        # Второй раз та же тревога не поднимается.
        again = _run(self.crm.raise_alert(
            tracker_id=self.tracker_id, kind="moving", note="ещё раз",
            bike_id=self.bike_id, lat=None, lon=None, level="urgent"))
        self.assertIsNone(again)

    def test_close_it(self):
        self.act("close")
        self.assertIsNotNone(self.state()["handled_at"])
        self.assertNotIn(str(self.alert_id), self.listed("/alerts?view=open"))

    def test_filters(self):
        other = _run(self.crm.raise_alert(
            tracker_id=self.tracker_id, kind="offline", note="молчит",
            bike_id=self.bike_id, lat=None, lon=None, level="yellow"))
        shown = self.listed("/alerts?view=open&level=urgent")
        self.assertIn(str(self.alert_id), shown)
        self.assertNotIn(str(other), shown)
        by_kind = self.listed("/alerts?view=open&kind=offline")
        self.assertEqual(by_kind, {str(other)})

    def test_closed_alert_cannot_be_taken(self):
        self.act("close")
        self.act("take")
        self.assertEqual(self.state()["state"], "new",
                         "закрытую в работу не возвращают: опрос поднимет новую")


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
