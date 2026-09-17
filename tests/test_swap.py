"""Замена велосипеда внутри аренды и подменный фонд.

До замены поломка означала «закрыть аренду и открыть новую», а вместе
с ней разъезжались оплаченный период и номер договора. Замена меняет
только то, что у клиента на руках.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
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


class TestSwapLogic(unittest.TestCase):
    def test_spare_bikes_come_first(self):
        bikes = [{"id": 1, "code": "B-1", "status": "available"},
                 {"id": 2, "code": "B-2", "status": "available", "spare": True},
                 {"id": 3, "code": "B-3", "status": "rented"},
                 {"id": 4, "code": "B-4", "status": "repair"}]
        rows = logic.swap_candidates(bikes, current_id=1)
        self.assertEqual([b["code"] for b in rows], ["B-2"])
        self.assertEqual([b["code"] for b in logic.swap_candidates(bikes)],
                         ["B-2", "B-1"], "подменный впереди свободного")

    def test_reason_decides_where_the_old_bike_goes(self):
        self.assertEqual(logic.SWAP_BIKE_STATUS["repair"], "repair")
        self.assertEqual(logic.SWAP_BIKE_STATUS["client"], "available")
        self.assertTrue(logic.check_swap_reason("maintenance").ok)
        self.assertFalse(logic.check_swap_reason("потому что").ok)

    def test_mileage_sums_across_bikes(self):
        """После замены одометр нового считается со своего начала."""
        rows = [{"issued_on": date(2026, 9, 1), "returned_on": date(2026, 9, 5),
                 "mileage_start": 100, "mileage_end": 180},
                {"issued_on": date(2026, 9, 5), "returned_on": None,
                 "mileage_start": 400, "mileage_end": None}]
        self.assertEqual(logic.rental_mileage(rows, current=460), 140)
        self.assertEqual(logic.rental_mileage(rows), 80, "открытая строка без текущего")
        self.assertEqual(logic.rental_mileage([]), 0)

    def test_rows_count_days_and_openness(self):
        rows = logic.rental_bike_rows(
            [{"issued_on": date(2026, 9, 1), "returned_on": date(2026, 9, 5),
              "mileage_start": 100, "mileage_end": 180},
             {"issued_on": date(2026, 9, 5), "returned_on": None,
              "mileage_start": 400, "mileage_end": None}],
            today=date(2026, 9, 10))
        self.assertEqual([(r["days"], r["open"], r["ridden"]) for r in rows],
                         [(4, False, 80), (5, True, None)])


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestSwapInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.spare_id = tw.run(self.crm.create_bike(code="B-SPARE", model="Truck+"))
        tw.run(self.crm.update_bike(self.spare_id, spare=True))
        self.other_id = tw.run(self.crm.create_bike(code="B-2", model="Kugoo V3"))
        self.rental_id = tw.run(tw.service.open_rental(
            self.crm, client=tw.run(self.crm.client(self.client_id)),
            bike=tw.run(self.crm.bike(self.bike_id)),
            tariff=tw.run(self.crm.tariff(self.tariff_id)),
            started_on=date.today() - timedelta(days=3), contract_no="АВ-1",
            by="staff:admin", mileage=1000))

    def rental(self):
        return tw.run(self.crm.rental(self.rental_id))

    def swap(self, **over):
        data = {"bike_id": self.spare_id, "reason": "repair", "old_status": "",
                "mileage_old": "1200", "mileage_new": "300"}
        data.update(over)
        return self.client.post(f"/rentals/{self.rental_id}/swap", data=data)

    def test_swap_keeps_money_and_dates(self):
        before = self.rental()
        r = self.swap()
        self.assertEqual(r.status_code, 303)
        after = self.rental()
        self.assertEqual(after["bike_id"], self.spare_id)
        self.assertEqual(after["status"], "active")
        self.assertEqual(after["billed_until"], before["billed_until"])
        self.assertEqual(after["started_on"], before["started_on"])
        self.assertEqual(after["contract_no"], before["contract_no"])
        self.assertEqual(after["balance"], before["balance"])

    def test_old_bike_goes_where_the_reason_says(self):
        self.swap(reason="repair")
        self.assertEqual(tw.run(self.crm.bike(self.bike_id))["status"], "repair")
        self.assertEqual(tw.run(self.crm.bike(self.spare_id))["status"], "rented")

    def test_old_bike_status_can_be_overridden(self):
        self.swap(reason="client", old_status="available")
        self.assertEqual(tw.run(self.crm.bike(self.bike_id))["status"], "available")

    def test_status_change_is_logged_with_the_author(self):
        self.swap()
        log = tw.run(self.crm.bike_status_log(self.bike_id))
        self.assertEqual(log[0]["to_status"], "repair")
        self.assertEqual(log[0]["changed_by"], "staff:admin")

    def test_movement_log_holds_both_bikes(self):
        self.swap()
        rows = tw.run(self.crm.rental_bikes(self.rental_id))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["bike_id"], self.bike_id)
        self.assertEqual(rows[0]["mileage_end"], 1200)
        self.assertIsNotNone(rows[0]["returned_on"])
        self.assertEqual(rows[1]["bike_id"], self.spare_id)
        self.assertEqual(rows[1]["reason"], "Поломка")
        self.assertIsNone(rows[1]["returned_on"])

    def test_mileage_counts_across_both_bikes(self):
        self.swap(mileage_old="1200", mileage_new="300")
        tw.run(self.crm.update_bike(self.spare_id, mileage_km=350))
        page = self.get_ok(f"/rentals/{self.rental_id}")
        self.assertIn("250 км", page, "200 на первом плюс 50 на втором")

    def test_busy_bike_is_refused(self):
        tw.run(self.crm.update_bike(self.other_id, status="repair"))
        r = self.swap(bike_id=self.other_id)
        self.assertIn("В ремонте", self.get_ok(r.headers["location"]))
        self.assertEqual(self.rental()["bike_id"], self.bike_id)

    def test_same_bike_is_refused(self):
        r = self.swap(bike_id=self.bike_id)
        self.assertIn("тот же велосипед", self.get_ok(r.headers["location"]))

    def test_odometer_does_not_go_backwards(self):
        r = self.swap(mileage_old="10")
        self.assertIn("Пробег", self.get_ok(r.headers["location"]))
        self.assertEqual(self.rental()["bike_id"], self.bike_id)

    def test_closed_rental_cannot_be_swapped(self):
        self.client.post(f"/rentals/{self.rental_id}/close",
                         data={"closed_on": date.today().isoformat(), "mileage": "",
                               "bike_status": "available", "note": ""})
        r = self.swap()
        self.assertIn("закрыта", self.get_ok(r.headers["location"]))

    def test_spare_is_offered_first(self):
        page = self.get_ok(f"/rentals/{self.rental_id}")
        self.assertIn("B-SPARE", page)
        self.assertIn("подменный", page)
        self.assertLess(page.index("B-SPARE"), page.index("B-2"))

    def test_viewer_cannot_swap(self):
        profile = tw.run(self.crm.access_profile_by_code("tech"))
        tw.run(self.crm.create_staff("petr", logic.hash_password("password-1"),
                                     "Пётр", "manager", profile["id"]))
        self.client.post("/logout")
        self.login("petr", "password-1")
        self.assertEqual(self.swap().status_code, 403)

    def test_spare_flag_is_saved_from_the_card(self):
        r = self.client.post(f"/bikes/{self.other_id}/edit", data={
            "code": "B-2", "model": "Kugoo V3", "battery_count": "2",
            "service_months": "24", "battery_service_months": "15",
            "residual_price": "0", "spare": "1", "note": ""})
        self.assertEqual(r.status_code, 303)
        self.assertTrue(tw.run(self.crm.bike(self.other_id))["spare"])

    def test_issue_writes_the_first_movement(self):
        rows = tw.run(self.crm.rental_bikes(self.rental_id))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "Выдача")
        self.assertEqual(rows[0]["mileage_start"], 1000)


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
