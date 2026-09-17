"""Мелочи списков: выгрузка, «без техники», дни на складе, трек за период.

Ничего из этого не меняет данные — поэтому здесь проверяется ровно одно:
что видно на экране, то и уезжает файлом, и что счётчики считают то, что
обещают подписью.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, date, datetime, timedelta
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


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def line_of(html: str) -> list:
    """Линия трека из страницы: она уезжает в скрипт как `var line = …`."""
    import json
    import re
    got = re.search(r"var line = (\[.*?\]);", html, re.S)
    return json.loads(got.group(1)) if got else []


class TestListLogic(unittest.TestCase):
    def test_days_on_stock_and_stale(self):
        now = datetime.now(UTC)
        parts = [{"id": 1, "title": "Камера", "min_stock": 5},
                 {"id": 2, "title": "Колодки", "min_stock": 0},
                 {"id": 3, "title": "Зеркало", "min_stock": 0}]
        moved = {1: now - timedelta(days=3),
                 2: now - timedelta(days=logic.STOCK_STALE_DAYS + 10)}
        rows = {r["id"]: r for r in logic.part_rows(
            parts, {1: 10, 2: 4, 3: 0}, moved, today=now.date())}
        self.assertEqual(rows[1]["days_on_stock"], 3)
        self.assertFalse(rows[1]["stale"])
        self.assertTrue(rows[2]["stale"], "лежит дольше квартала — деньги на полке")
        self.assertIsNone(rows[3]["days_on_stock"], "движений не было вовсе")
        self.assertEqual(logic.stock_summary(rows.values())["stale"], 1)

    def test_empty_shelf_is_not_stale(self):
        now = datetime.now(UTC)
        rows = logic.part_rows([{"id": 1, "title": "Камера", "min_stock": 0}],
                               {1: 0},
                               {1: now - timedelta(days=200)}, today=now.date())
        self.assertFalse(rows[0]["stale"],
                         "пустая полка не лежит — лежать нечему")

    def test_track_periods(self):
        today = date(2026, 9, 17)
        self.assertEqual(logic.track_period("today", today=today),
                         (today, today))
        self.assertEqual(logic.track_period("yesterday", today=today),
                         (date(2026, 9, 16), date(2026, 9, 16)))
        self.assertEqual(logic.track_period("week", today=today),
                         (date(2026, 9, 11), today))
        self.assertEqual(logic.track_period("чушь", today=today), (today, today),
                         "непонятный вид — сегодня, а не пустой экран")

    def test_custom_period_is_clamped_to_the_journal(self):
        today = date(2026, 9, 17)
        self.assertEqual(
            logic.track_period("custom", today=today, since=date(2026, 9, 10),
                               until=date(2026, 9, 12)),
            (date(2026, 9, 10), date(2026, 9, 12)))
        # Журнал позиций живёт месяц: просить больше нечего.
        first, last = logic.track_period("custom", today=today,
                                         since=date(2020, 1, 1),
                                         until=date(2030, 1, 1))
        self.assertEqual(first, today - timedelta(days=31))
        self.assertEqual(last, today)
        # Перевёрнутый период — к умолчанию, а не к пустоте.
        self.assertEqual(
            logic.track_period("custom", today=today, since=date(2026, 9, 12),
                               until=date(2026, 9, 10)), (today, today))

    def test_track_line_drops_gps_jumps(self):
        rows = [{"recorded_at": 2, "lat": 55.80, "lon": 49.10},
                {"recorded_at": 1, "lat": 55.79, "lon": 49.09},
                {"recorded_at": 3, "lat": 10.0, "lon": 10.0},
                {"recorded_at": 4, "lat": 55.81, "lon": 49.11}]
        line = logic.track_line(rows)
        self.assertEqual(line[0], [55.79, 49.09], "линия идёт от старой точки")
        self.assertNotIn([10.0, 10.0], line, "перескок спутника — не поездка")
        self.assertEqual(len(line), 3)

    def test_track_line_survives_empty_coordinates(self):
        self.assertEqual(logic.track_line([]), [])
        self.assertEqual(
            logic.track_line([{"recorded_at": 1, "lat": None, "lon": 49.0}]), [])


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestExports(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def csv(self, path):
        r = self.client.get(path)
        self.assertEqual(r.status_code, 200, path)
        self.assertIn("text/csv", r.headers["content-type"])
        self.assertTrue(r.text.startswith("﻿"), "BOM нужен Excel")
        return r.text

    def test_bikes_export_follows_the_filter(self):
        _run(self.crm.create_bike(code="B-Z", model="Другая"))
        everything = self.csv("/bikes.csv")
        self.assertIn("B-1", everything)
        self.assertIn("B-Z", everything)
        only = self.csv("/bikes.csv?q=B-Z")
        self.assertIn("B-Z", only)
        self.assertNotIn("B-1", only, "выгружают то, что видят")

    def test_rentals_export_opens(self):
        text = self.csv("/rentals.csv?status=all")
        self.assertIn("Аренда", text)

    def test_orders_export_opens(self):
        _run(self.crm.create_work_order(
            bike_id=self.bike_id, payer="own", client_id=None,
            complaint="стук", object_note=None, tech_id=None, estimate=D(0),
            created_by="тест"))
        text = self.csv("/orders.csv")
        self.assertIn("РЕМ-000001", text)

    def test_parts_export_has_days_on_stock(self):
        _run(self.crm.create_part(title="Камера", node="tube_tire", unit="шт",
                                  cost=D(100), price=D(300), min_stock=5,
                                  model=None, note=None))
        self.assertIn("Дней на складе", self.csv("/parts.csv"))

    def test_export_needs_the_section(self):
        """Кладовщик без доступа к парку выгрузку парка не получит."""
        profile = _run(self.crm.create_access_profile(
            "Только склад", {"sections": {"inventory": "edit"}}))
        _run(self.crm.create_staff("sklad", logic.hash_password("sklad-pass-1"),
                                   name="Кладовщик", role="manager",
                                   profile_id=profile))
        self.client.post("/logout")
        self.login("sklad", "sklad-pass-1")
        self.assertEqual(self.client.get("/bikes.csv").status_code, 403)
        self.assertEqual(self.client.get("/parts.csv").status_code, 200)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestRentalViews(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.rental_id = _run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id,
            tariff_id=self.tariff_id, tariff_name="Неделя", period_days=7,
            price=D(3000), billing="weekly", started_on=date(2026, 9, 1),
            contract_no="АВ-1", created_by="тест"))

    def test_nobike_shows_rentals_without_equipment(self):
        text = self.get_ok("/rentals?view=nobike")
        self.assertNotIn("АВ-1", text)
        _run(self.crm.update_rental(self.rental_id, bike_id=None))
        self.assertIn("Иванов", self.get_ok("/rentals?view=nobike"),
                      "аренда идёт, а велосипеда на руках нет")

    def test_debt_view_counts_only_debtors(self):
        self.assertNotIn("Иванов", self.get_ok("/rentals?view=debt"))
        _run(self.crm.add_ledger(client_id=self.client_id,
                                 rental_id=self.rental_id, kind="charge",
                                 amount=D(-3000)))
        self.assertIn("Иванов", self.get_ok("/rentals?view=debt"))

    def test_tabs_are_on_the_page(self):
        text = self.get_ok("/rentals")
        self.assertIn("Без техники", text)
        self.assertIn("/rentals.csv", text)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestTrackPage(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.bike_id = _run(self.crm.create_bike(code="B-7", model="Kugoo"))
        self.tracker_id = _run(self.crm.create_tracker(
            device_id="1007", alias="Трекер 7", bike_id=self.bike_id))

    def point(self, when, lat, lon):
        _run(self.crm.save_tracker_state({
            "device_id": "1007", "alias": "Трекер 7", "lat": lat, "lon": lon,
            "speed": D(10), "voltage": D("12.6"), "recorded_at": when,
            "course": None, "gsm_level": None, "alarm": False}))

    def test_period_tabs_and_line(self):
        now = datetime.now().astimezone()
        self.point(now - timedelta(hours=1), 55.79, 49.09)
        self.point(now - timedelta(days=2), 55.70, 49.00)
        today = line_of(self.get_ok(f"/trackers/{self.tracker_id}?range=today"))
        self.assertIn([55.79, 49.09], today, "точка за сегодня попала в линию")
        self.assertNotIn([55.70, 49.00], today,
                         "позавчерашняя точка в «сегодня» не идёт")
        week = line_of(self.get_ok(f"/trackers/{self.tracker_id}?range=week"))
        self.assertIn([55.70, 49.00], week)

    def test_custom_period(self):
        now = datetime.now().astimezone()
        self.point(now - timedelta(days=3), 55.75, 49.05)
        day = (now - timedelta(days=3)).date().isoformat()
        got = line_of(self.get_ok(
            f"/trackers/{self.tracker_id}?range=custom&since={day}&until={day}"))
        self.assertEqual(got, [[55.75, 49.05]])


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
