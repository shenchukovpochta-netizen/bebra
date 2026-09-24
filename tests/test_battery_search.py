"""Розыск и кража доходят до аккумулятора.

Розыск — состояние аренды, а не батареи: пока клиент не нашёлся, батарея
у него и числится «у клиента». А вот признание кражи должно уводить в
«утеряна» и велосипед, и его батареи — иначе велосипед числится
потерянным, а две батареи при нём живыми и свободными к выдаче.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, date, datetime
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


class TestBatterySearchRows(unittest.TestCase):
    def test_search_mark_comes_from_the_rental(self):
        rows = logic.battery_rows([
            {"id": 1, "code": "A-1", "status": "rented", "cycles": 0,
             "search_at": datetime.now(UTC)},
            {"id": 2, "code": "A-2", "status": "available", "cycles": 0,
             "search_at": None}])
        by_code = {r["code"]: r for r in rows}
        self.assertTrue(by_code["A-1"]["in_search"])
        self.assertFalse(by_code["A-2"]["in_search"])

    def test_searched_batteries_come_first(self):
        rows = logic.battery_rows([
            {"id": 1, "code": "A-1", "status": "available", "cycles": 900},
            {"id": 2, "code": "A-2", "status": "rented", "cycles": 0,
             "search_at": datetime.now(UTC)}])
        self.assertEqual(rows[0]["code"], "A-2", "их ищут, а не листают")

    def test_summary_counts_them(self):
        rows = logic.battery_rows([
            {"id": 1, "code": "A-1", "status": "rented", "cycles": 0,
             "search_at": datetime.now(UTC)},
            {"id": 2, "code": "A-2", "status": "available", "cycles": 0}])
        self.assertEqual(logic.battery_summary(rows)["search"], 1)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestSearchAndTheft(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.battery_id = _run(self.crm.create_battery(
            code="9510001", status="available"))
        self.rental_id = _run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id,
            tariff_id=self.tariff_id, tariff_name="Неделя", period_days=7,
            price=D(3000), billing="weekly", started_on=date(2026, 9, 1),
            contract_no="АВ-1", created_by="тест"))
        _run(self.crm.issue_batteries(self.rental_id,
                                      battery_ids=[self.battery_id],
                                      bike_id=self.bike_id, by="тест"))

    def battery(self):
        return _run(self.crm.battery(self.battery_id))

    def test_search_buttons_without_free_bikes(self):
        # Свободных велосипедов нет (единственный - в этой аренде): менять
        # не на что, а объявить розыск и признать потерю всё равно надо.
        page = self.get_ok(f"/rentals/{self.rental_id}")
        self.assertNotIn("Заменить велосипед", page)
        self.assertIn("Объявить в розыск", page)
        self.client.post(f"/rentals/{self.rental_id}/search",
                         data={"action": "start", "note": ""})
        self.assertIn("Признать потерянным", self.get_ok(f"/rentals/{self.rental_id}"))

    def test_search_marks_the_battery_too(self):
        self.assertFalse(logic.battery_rows([self.battery()])[0]["in_search"])
        self.client.post(f"/rentals/{self.rental_id}/search",
                         data={"action": "start", "note": "не отвечает"})
        row = logic.battery_rows([self.battery()])[0]
        self.assertTrue(row["in_search"])
        self.assertEqual(row["status"], "rented",
                         "розыск не меняет статус: батарея всё ещё у клиента")
        self.assertIn("9510001", self.get_ok("/batteries?view=search"))

    def test_search_is_taken_off_with_the_rental(self):
        self.client.post(f"/rentals/{self.rental_id}/search",
                         data={"action": "start", "note": ""})
        self.client.post(f"/rentals/{self.rental_id}/search",
                         data={"action": "stop"})
        self.assertFalse(logic.battery_rows([self.battery()])[0]["in_search"])
        self.assertNotIn("9510001", self.get_ok("/batteries?view=search"))

    def test_theft_takes_the_battery_with_the_bike(self):
        self.client.post(f"/rentals/{self.rental_id}/search",
                         data={"action": "theft", "note": "месяц не платит"})
        self.assertEqual(_run(self.crm.bike(self.bike_id))["status"], "lost")
        self.assertEqual(self.battery()["status"], "lost",
                         "иначе велосипед потерян, а батарея при нём жива")
        self.assertIsNone(self.battery()["rental_id"])
        self.assertEqual(_run(self.crm.rental(self.rental_id))["status"], "closed")

    def test_debt_survives_the_theft(self):
        _run(self.crm.add_ledger(client_id=self.client_id,
                                 rental_id=self.rental_id, kind="charge",
                                 amount=D(-3000)))
        self.client.post(f"/rentals/{self.rental_id}/search",
                         data={"action": "theft", "note": ""})
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(-3000),
                         "списать долг - отдельное решение владельца")

    def test_ordinary_close_returns_the_battery_to_the_park(self):
        self.client.post(f"/rentals/{self.rental_id}/close",
                         data={"closed_on": date.today().isoformat(),
                               "bike_status": "available"})
        self.assertEqual(self.battery()["status"], "available")

    def test_search_tab_is_on_the_page(self):
        self.assertIn("В розыске", self.get_ok("/batteries"))


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
