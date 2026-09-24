"""Трекеры: разбор ответа StarLine, состояние, тревоги и карта.

Сети в тестах нет и не должно быть: клиент StarLine получает заглушку
сессии, а всё, что считается по данным трекера, вынесено в чистые
функции и проверяется без базы.
"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402
from app.services import starline  # noqa: E402

try:
    import test_web as tw

    from app.crm import service, tracking
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
# Павлюхина и Адоратского - настоящие точки, расстояние между ними ~6 км.
PAVLYUHINA = (55.7692, 49.1440)
ADORATSKOGO = (55.8341, 49.1076)


def tracker(**over) -> dict:
    row = {"id": 1, "device_id": "1001", "alias": "Truck+ 101", "bike_id": 7,
           "bike_code": "B-101", "bike_status": "available", "active": True,
           "last_seen": NOW - timedelta(minutes=5), "lat": PAVLYUHINA[0],
           "lon": PAVLYUHINA[1], "speed": D(0), "course": 0, "voltage": D("12.6"),
           "gsm_level": 20, "alarm": False}
    row.update(over)
    return row


class TestStarlineParsing(unittest.TestCase):
    def test_v2_user_info_x_is_latitude(self):
        # Форма v2 user_info, которую и читает опрос: поля устройства в
        # корне, флаги тревог в car_alr_state (эталон - Home Assistant).
        got = starline.parse_device({
            "device_id": 1001, "alias": "Truck+ 101", "status": 1,
            "position": {"x": 55.7692, "y": 49.1440, "s": 18, "dir": 90,
                         "ts": 1789000000},
            "battery": 12.6, "gsm_lvl": 20, "ts_activity": 1789000300,
            "car_state": {"alarm": False, "hijack": False},
            "car_alr_state": {"door": False, "shock_h": False}})
        self.assertEqual(got["device_id"], "1001")
        self.assertEqual(got["lat"], 55.7692, "в v2 x - это широта")
        self.assertEqual(got["lon"], 49.1440, "в v2 y - это долгота")
        self.assertEqual(got["speed"], 18.0)
        self.assertEqual(got["voltage"], 12.6)
        self.assertEqual(got["gsm_level"], 20)
        self.assertFalse(got["alarm"])
        self.assertTrue(got["online"])
        self.assertEqual(got["recorded_at"],
                         datetime.fromtimestamp(1789000000, UTC))
        self.assertEqual(got["active_at"], datetime.fromtimestamp(1789000300, UTC))

    def test_v3_data_x_is_longitude(self):
        got = starline.parse_device({
            "device_id": 1001, "status": 1,
            "position": {"x": 49.1440, "y": 55.7692, "s": 18, "ts": 1789000000},
            "common": {"battery": 12.6, "gsm_lvl": 20, "ts": 1789000300},
            "alarm_state": {"door": False, "hijack": False, "ts": 1789000000}})
        self.assertEqual(got["lat"], 55.7692, "в v3 y - это широта")
        self.assertEqual(got["lon"], 49.1440)
        self.assertEqual(got["voltage"], 12.6)
        # Метка ts среди флагов - не тревога.
        self.assertFalse(got["alarm"])

    def test_alarm_flags_and_own_block_is_not_alarm(self):
        def alarm(**raw):
            return starline.parse_device({"device_id": "1", **raw})["alarm"]
        self.assertTrue(alarm(car_alr_state={"shock_h": True}))
        self.assertTrue(alarm(car_alr_state={"tilt": 1}))
        self.assertTrue(alarm(car_state={"alarm": "1"}))
        self.assertTrue(alarm(alarm_state={"shock_l": 1, "ts": 1789000000}))
        # «Антиограбление» - это наша же блокировка мотора, не угон.
        self.assertFalse(alarm(car_alr_state={"hijack": True},
                               car_state={"hijack": True, "alarm": False}))
        self.assertFalse(alarm(alarm_state={"hijack": 1, "ts": 1789000000}))

    def test_zero_coordinates_mean_no_fix(self):
        # Без спутников StarLine отдаёт «00.000000»: это не точка, а её нет.
        got = starline.parse_device({
            "device_id": "1", "status": 1, "ts_activity": 1789000300,
            "position": {"x": "00.000000", "y": "00.000000", "s": 0,
                         "ts": 1789000000}})
        self.assertIsNone(got["lat"])
        self.assertIsNone(got["lon"])
        self.assertIsNone(got["recorded_at"], "в журнал позиций не пишется")
        self.assertIsNotNone(got["active_at"], "на связи он при этом есть")
        # и старая такая точка в журнале не обрезает настоящий трек
        base = datetime(2026, 9, 24, 8, 0, tzinfo=UTC)
        line = logic.track_line(
            [{"lat": 0.0, "lon": 0.0, "recorded_at": base}]
            + [{"lat": 55.79 + i / 1000, "lon": 49.12, "recorded_at":
                base + timedelta(minutes=5 * (i + 1))} for i in range(3)])
        self.assertEqual(len(line), 3)
        self.assertAlmostEqual(line[0][0], 55.79)

    def test_zero_point_left_from_before_is_ignored_everywhere(self):
        # Записи 0/0 от опросов до исправления: ни на карту, ни в ссылку,
        # ни в пробег - и настоящий участок вокруг неё не теряется.
        base = datetime(2026, 9, 24, 8, 0, tzinfo=UTC)
        track = [{"lat": 55.79, "lon": 49.12, "recorded_at": base},
                 {"lat": 0.0, "lon": 0.0, "recorded_at": base + timedelta(minutes=5)},
                 {"lat": 55.80, "lon": 49.12, "recorded_at": base + timedelta(minutes=10)}]
        self.assertAlmostEqual(logic.track_distance(track), 1.1, places=1)
        self.assertIsNone(logic.map_url(0.0, 0.0))
        self.assertEqual(logic.map_points([{**tracker(), "lat": 0.0, "lon": 0.0,
                                            "offline": False}]), [])
        self.assertFalse(logic.has_fix(0.0, 0.0))
        self.assertTrue(logic.has_fix(55.79, 49.12))

    def test_status_two_is_offline(self):
        self.assertFalse(starline.parse_device({"device_id": "1", "status": 2})["online"])
        self.assertIsNone(starline.parse_device({"device_id": "1"})["online"])

    def test_missing_fields_stay_none(self):
        got = starline.parse_device({"device_id": "77"})
        self.assertIsNone(got["lat"])
        self.assertIsNone(got["recorded_at"])
        self.assertIsNone(got["active_at"])
        self.assertIsNone(got["voltage"])
        self.assertFalse(got["alarm"])

    def test_seen_at_is_the_later_of_fix_and_activity(self):
        fix = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)
        ping = datetime(2026, 9, 24, 7, 55, tzinfo=UTC)
        self.assertEqual(logic.tracker_seen_at({"recorded_at": fix, "active_at": ping}),
                         ping)
        self.assertEqual(logic.tracker_seen_at({"recorded_at": fix}), fix)
        self.assertIsNone(logic.tracker_seen_at({}))

    def test_error_envelope_is_recognised(self):
        self.assertEqual(starline.check_state({"state": 1, "desc": {"code": "abc"}},
                                              what="getCode"), {"code": "abc"})
        with self.assertRaises(starline.StarlineError):
            starline.check_state({"state": 0, "code": 403}, what="getCode")
        with self.assertRaises(starline.StarlineError):
            starline.check_state("<html>", what="getCode")

    def test_client_walks_the_whole_auth_chain(self):
        calls: list[tuple[str, str]] = []

        class Response:
            def __init__(self, data, *, status=200, cookies=None):
                self._data, self.status = data, status
                self.cookies = cookies or {}
                self.headers = {}

            async def json(self, content_type=None):
                return self._data

        class Session:
            async def request(self, method, url, **kwargs):
                calls.append((method, url))
                if url.endswith("getCode/"):
                    return Response({"state": 1, "desc": {"code": "CODE"}})
                if url.endswith("getToken/"):
                    return Response({"state": 1, "desc": {"token": "APP"}})
                if url.endswith("user/login/"):
                    return Response({"state": 1, "desc": {"user_token": "SLID"}})
                if url.endswith("auth.slid"):
                    return Response({"user_id": "42"}, cookies={"slnet": "COOKIE"})
                return Response({"devices": [{"device_id": 1, "alias": "A",
                                              "position": {"x": 55.7, "y": 49.1,
                                                           "ts": 1789000000}}],
                                 # расшаренный из другого кабинета и дубль
                                 "shared_devices": [{"device_id": 2, "alias": "B"},
                                                    {"device_id": 1, "alias": "A2"}]})

            async def close(self):
                pass

        client = starline.StarlineClient(app_id="1", app_secret="s", login="l",
                                         password="p", session_factory=Session)
        self.assertTrue(client.ready)
        devices = _run(client.devices(now=0.0))
        self.assertEqual([d["device_id"] for d in devices], ["1", "2"])
        self.assertEqual(devices[0]["alias"], "A")
        self.assertEqual(devices[0]["lat"], 55.7)
        self.assertEqual([url.rsplit("/apiV3", 1)[-1].rsplit("/json", 1)[-1]
                          for _, url in calls][:4],
                         ["/application/getCode/", "/application/getToken/",
                          "/user/login/", "/v2/auth.slid"])
        # Второй опрос идёт сразу за устройствами: токены и cookie в кэше.
        calls.clear()
        _run(client.devices(now=10.0))
        self.assertEqual(len(calls), 1)

    def test_client_without_credentials_does_nothing(self):
        client = starline.StarlineClient(app_id="", app_secret="", login="",
                                         password="")
        self.assertFalse(client.ready)
        self.assertEqual(_run(client.devices()), [])


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class TestTrackerLogic(unittest.TestCase):
    def test_state_is_derived_from_last_seen_and_speed(self):
        rows = logic.tracker_rows(
            [tracker(), tracker(id=2, device_id="1002", bike_code="B-102",
                                last_seen=NOW - timedelta(hours=20)),
             tracker(id=3, device_id="1003", bike_code="B-103", speed=D(24),
                     bike_status="rented"),
             tracker(id=4, device_id="1004", bike_code="B-104", last_seen=None)],
            now=NOW)
        by_code = {r["bike_code"]: r for r in rows}
        self.assertFalse(by_code["B-101"]["offline"])
        self.assertTrue(by_code["B-102"]["offline"], "20 часов молчания")
        self.assertTrue(by_code["B-103"]["moving"])
        self.assertTrue(by_code["B-104"]["offline"], "ни разу не выходил")
        self.assertEqual(rows[0]["bike_code"], "B-103", "едущие - первыми")
        self.assertIn("yandex.ru/maps", rows[0]["map"])

    def test_thresholds_come_from_settings(self):
        row = logic.tracker_rows([tracker(speed=D(7))], now=NOW,
                                 settings={"tracker_moving_speed": "10"})[0]
        self.assertFalse(row["moving"], "порог поднят - это ещё не движение")
        row = logic.tracker_rows([tracker(last_seen=NOW - timedelta(hours=3))],
                                 now=NOW, settings={"tracker_offline_hours": "2"})[0]
        self.assertTrue(row["offline"])
        # мусор в настройках не должен ронять экран
        self.assertEqual(logic.tracker_settings({"tracker_offline_hours": "ага"}),
                         logic.tracker_settings({}))

    def test_alerts_fire_only_where_they_mean_something(self):
        parked = logic.tracker_rows([tracker(speed=D(19))], now=NOW)[0]
        kinds = [a["kind"] for a in logic.detect_alerts(parked)]
        self.assertEqual(kinds, ["moving"], "по учёту стоит на точке, а едет")

        rented = logic.tracker_rows([tracker(speed=D(19), bike_status="rented")],
                                    now=NOW)[0]
        self.assertEqual(logic.detect_alerts(rented), [],
                         "велосипед в аренде ездит - это норма")

        silent = logic.tracker_rows([tracker(last_seen=NOW - timedelta(hours=30))],
                                    now=NOW)[0]
        self.assertEqual([a["kind"] for a in logic.detect_alerts(silent)], ["offline"])

        dead = logic.tracker_rows([tracker(voltage=D("11.0"), alarm=True)], now=NOW)[0]
        self.assertEqual({a["kind"] for a in logic.detect_alerts(dead)},
                         {"alarm", "low_power"})

        sold = logic.tracker_rows([tracker(bike_status="sold", last_seen=None)],
                                  now=NOW)[0]
        self.assertEqual(logic.detect_alerts(sold), [],
                         "проданный велосипед молчит по делу")

    def test_alert_carries_the_place_and_the_bike(self):
        row = logic.tracker_rows([tracker(speed=D(19))], now=NOW)[0]
        alert = logic.detect_alerts(row)[0]
        self.assertEqual(alert["tracker_id"], 1)
        self.assertEqual(alert["bike_id"], 7)
        self.assertEqual(alert["lat"], PAVLYUHINA[0])
        self.assertIn("км/ч", alert["note"])

    def test_distance_and_track(self):
        self.assertAlmostEqual(
            logic.distance_km(*PAVLYUHINA, *ADORATSKOGO), 7.6, delta=0.5)
        self.assertIsNone(logic.distance_km(None, 1.0, 2.0, 3.0))
        track = [{"lat": 55.7692, "lon": 49.1440, "recorded_at": NOW},
                 {"lat": 55.7712, "lon": 49.1480, "recorded_at": NOW + timedelta(minutes=5)},
                 {"lat": 55.7732, "lon": 49.1520, "recorded_at": NOW + timedelta(minutes=10)},
                 # скачок GPS через всю страну - в накат не идёт
                 {"lat": 44.0, "lon": 39.0, "recorded_at": NOW + timedelta(minutes=15)}]
        self.assertAlmostEqual(logic.track_distance(track), 0.6, delta=0.3)

    def test_map_points_paint_by_state(self):
        rows = logic.tracker_rows(
            [tracker(), tracker(id=2, device_id="1002", bike_status="rented",
                                bike_code="B-102"),
             tracker(id=3, device_id="1003", alarm=True, bike_code="B-103"),
             tracker(id=4, device_id="1004", bike_code="B-104",
                     last_seen=NOW - timedelta(days=2)),
             tracker(id=5, device_id="1005", bike_code="B-105", lat=None, lon=None)],
            now=NOW)
        states = {p["code"]: p["state"] for p in logic.map_points(rows)}
        self.assertEqual(states, {"B-101": "parked", "B-102": "rented",
                                  "B-103": "alarm", "B-104": "offline"})
        self.assertNotIn("B-105", states, "без координат точки нет")

    def test_digest_is_short_and_has_links(self):
        alerts = [{"kind": "moving", "note": "19 км/ч", "bike_code": "B-101",
                   "lat": PAVLYUHINA[0], "lon": PAVLYUHINA[1]},
                  {"kind": "offline", "note": "молчит 30 ч", "device_id": "1002"}]
        digest = logic.tracker_digest(alerts)
        self.assertIn("Едет без аренды", digest)
        self.assertIn("B-101", digest)
        self.assertIn("yandex.ru/maps", digest)
        self.assertEqual(logic.tracker_digest([]), "")


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestTrackingPoll(tw.WebCase):
    """Опрос: состояния в базу, тревоги - один раз."""

    class Client:
        def __init__(self, devices):
            self.payload = devices
            self.ready = True

        async def devices(self):
            return list(self.payload)

    def setUp(self):
        super().setUp()
        self.seed()

    def device(self, **over) -> dict:
        row = {"device_id": "1001", "alias": "Truck+ 101", "lat": PAVLYUHINA[0],
               "lon": PAVLYUHINA[1], "speed": 0.0, "course": 0,
               "recorded_at": datetime.now(UTC), "voltage": 12.6,
               "gsm_level": 20, "alarm": False, "online": True}
        row.update(over)
        return row

    def poll(self, devices):
        return tw.run(tracking.poll_once(self.crm, self.Client(devices)))

    def test_unknown_device_is_created_and_position_is_logged(self):
        out = self.poll([self.device()])
        self.assertEqual(out["devices"], 1)
        tracker_row = tw.run(self.crm.tracker_by_device("1001"))
        self.assertIsNotNone(tracker_row)
        self.assertEqual(tracker_row["alias"], "Truck+ 101")
        self.assertEqual(len(tw.run(self.crm.tracker_positions(tracker_row["id"]))), 1)
        # Повторный опрос с той же меткой времени точку не дублирует.
        self.poll([self.device(recorded_at=tracker_row["last_seen"])])
        self.assertEqual(len(tw.run(self.crm.tracker_positions(tracker_row["id"]))), 1)

    def test_alert_is_raised_once_and_closes_itself(self):
        self.poll([self.device()])
        tracker_row = tw.run(self.crm.tracker_by_device("1001"))
        tw.run(self.crm.update_tracker(tracker_row["id"], bike_id=self.bike_id))

        out = self.poll([self.device(speed=25.0)])
        self.assertEqual([a["kind"] for a in out["alerts"]], ["moving"])
        self.assertEqual(len(tw.run(self.crm.tracker_alerts())), 1)
        # Второй круг с тем же состоянием новой тревоги не даёт.
        out = self.poll([self.device(speed=25.0)])
        self.assertEqual(out["alerts"], [])
        self.assertEqual(len(tw.run(self.crm.tracker_alerts())), 1)
        # Велосипед встал - тревога снимается сама.
        self.poll([self.device(speed=0.0)])
        self.assertEqual(tw.run(self.crm.tracker_alerts()), [])
        closed = tw.run(self.crm.tracker_alerts(open_only=False))
        self.assertEqual(closed[0]["handled_by"], "tracking")

    def test_moving_in_a_rental_is_not_an_alert(self):
        self.poll([self.device()])
        tracker_row = tw.run(self.crm.tracker_by_device("1001"))
        tw.run(self.crm.update_tracker(tracker_row["id"], bike_id=self.bike_id))
        tw.run(service.open_rental(
            self.crm, client=tw.run(self.crm.client(self.client_id)),
            bike=tw.run(self.crm.bike(self.bike_id)),
            tariff=tw.run(self.crm.tariff(self.tariff_id)),
            started_on=date.today(), contract_no=None, by="t"))
        out = self.poll([self.device(speed=25.0)])
        self.assertEqual(out["alerts"], [])

    def test_digest_goes_to_the_service_chat(self):
        self.poll([self.device()])
        out = self.poll([self.device(alarm=True)])
        chat = types.SimpleNamespace(contract_chat_id=-100500)
        sent = tw.run(tracking.report_alerts(self.bot, chat, out["alerts"]))
        self.assertEqual(sent, 1)
        self.assertIn("Тревога StarLine", self.bot.sent[-1][1])


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestTrackerPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.tracker_id = tw.run(self.crm.create_tracker(
            device_id="1001", alias="Truck+ 101",
            last_seen=datetime.now(UTC), lat=PAVLYUHINA[0], lon=PAVLYUHINA[1],
            speed=D(0), voltage=D("12.6")))

    def test_map_and_list_render(self):
        page = self.get_ok("/map")
        self.assertIn("Карта парка", page)
        self.assertIn("leaflet", page)
        self.assertIn("1001", page)
        self.assertIn("Устройства", page)
        card = self.get_ok("/trackers")
        self.assertIn("Truck+ 101", card)
        self.assertIn("без велосипеда", card)

    def test_tracker_is_bound_to_a_bike_and_only_to_one(self):
        r = self.client.post(f"/trackers/{self.tracker_id}/bike",
                             data={"bike_id": str(self.bike_id)})
        self.assertEqual(r.headers["location"], f"/trackers/{self.tracker_id}")
        self.assertEqual(tw.run(self.crm.tracker(self.tracker_id))["bike_code"], "B-1")
        second = tw.run(self.crm.create_tracker(device_id="1002"))
        self.client.post(f"/trackers/{second}/bike", data={"bike_id": str(self.bike_id)})
        self.assertIn("уже стоит другой трекер", self.get_ok("/trackers"))
        self.assertIsNone(tw.run(self.crm.tracker(second))["bike_id"])
        # отвязка - пустым значением
        self.client.post(f"/trackers/{self.tracker_id}/bike", data={"bike_id": ""})
        self.assertIsNone(tw.run(self.crm.tracker(self.tracker_id))["bike_id"])

    def test_manual_tracker_and_duplicate(self):
        r = self.client.post("/trackers", data={"device_id": "2002", "alias": "Метка",
                                                "note": ""})
        self.assertEqual(r.headers["location"], "/trackers")
        self.assertIsNotNone(tw.run(self.crm.tracker_by_device("2002")))
        self.client.post("/trackers", data={"device_id": "2002"})
        self.assertIn("уже заведён", self.get_ok("/trackers"))

    def test_card_shows_the_track_and_alerts_are_handled(self):
        tw.run(self.crm.save_tracker_state(
            {"device_id": "1001", "lat": PAVLYUHINA[0], "lon": PAVLYUHINA[1],
             "speed": 12.0, "recorded_at": datetime.now(UTC)}))
        alert_id = tw.run(self.crm.raise_alert(
            tracker_id=self.tracker_id, kind="moving", note="25 км/ч",
            bike_id=None, lat=PAVLYUHINA[0], lon=PAVLYUHINA[1]))
        card = self.get_ok(f"/trackers/{self.tracker_id}")
        self.assertIn("Едет без аренды", card)
        self.assertIn("Накатано по треку", card)
        r = self.client.post(f"/trackers/alerts/{alert_id}", data={"next": "/map"})
        self.assertEqual(r.headers["location"], "/map")
        self.assertEqual(tw.run(self.crm.tracker_alerts()), [])

    def test_toggle_takes_the_tracker_off_watch(self):
        self.client.post(f"/trackers/{self.tracker_id}/toggle")
        self.assertFalse(tw.run(self.crm.tracker(self.tracker_id))["active"])
        self.assertIn("снят", self.get_ok("/trackers"))

    def test_missing_tracker_is_a_404(self):
        self.assertEqual(self.client.get("/trackers/999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
