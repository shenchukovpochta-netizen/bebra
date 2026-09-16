"""Пробег: одометр велосипеда, ввод при выдаче и при возврате, накат
за аренду. Чистая логика и панель через TestClient (обвязка из test_web.py).
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
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


class TestMileageLogic(unittest.TestCase):
    def test_check_mileage_accepts_digits_and_spaces(self):
        self.assertEqual(logic.check_mileage("4266").value, 4266)
        self.assertEqual(logic.check_mileage(" 4 266 ").value, 4266)
        self.assertEqual(logic.check_mileage("0").value, 0)

    def test_check_mileage_rejects_junk_and_overflow(self):
        for bad in ("", "  ", "много", "42,6", "-5", "4.2"):
            self.assertFalse(logic.check_mileage(bad).ok, bad)
        self.assertIn("число километров", logic.check_mileage("").error)
        self.assertIn("целое число", logic.check_mileage("4.2").error)
        big = logic.check_mileage(str(logic.MAX_MILEAGE_KM + 1))
        self.assertFalse(big.ok)
        self.assertIn("не больше", big.error)

    def test_check_mileage_does_not_go_backwards(self):
        back = logic.check_mileage("4000", current=4266)
        self.assertFalse(back.ok)
        self.assertIn("меньше прежнего (4266 км)", back.error)
        self.assertTrue(logic.check_mileage("4266", current=4266).ok, "равный принимается")
        self.assertTrue(logic.check_mileage("4300", current=4266).ok)

    def test_check_mileage_optional(self):
        empty = logic.check_mileage("", required=False)
        self.assertTrue(empty.ok)
        self.assertIsNone(empty.value, "пусто - не трогать значение")
        self.assertFalse(logic.check_mileage("абв", required=False).ok)

    def test_ridden_and_per_day(self):
        rental = {"mileage_start": 4266, "mileage_end": 4586,
                  "started_on": date(2026, 9, 1), "closed_on": date(2026, 9, 11)}
        self.assertEqual(logic.ridden(rental), 320)
        self.assertEqual(logic.ridden_per_day(rental), 32)
        self.assertEqual(logic.ridden_per_day(rental, days=8), 40)
        # аренда идёт: конца ещё нет
        self.assertIsNone(logic.ridden({**rental, "mileage_end": None}))
        self.assertIsNone(logic.ridden_per_day({**rental, "mileage_end": None}))
        self.assertIsNone(logic.ridden({"mileage_end": 100}))
        # сдали в день выдачи - делить не на что, но накат виден
        same = {**rental, "closed_on": date(2026, 9, 1)}
        self.assertEqual(logic.ridden(same), 320)
        self.assertIsNone(logic.ridden_per_day(same))
        # кривая пара (велосипед перепутали) не даёт отрицательного наката
        self.assertEqual(logic.ridden({"mileage_start": 500, "mileage_end": 100}), 0)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestMileageInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        tw.run(self.crm.update_bike(self.bike_id, mileage_km=4266))

    def issue_form(self, **over):
        data = {"client_id": self.client_id, "tariff_id": self.tariff_id,
                "bike_id": self.bike_id, "pay_amount": "3000", "pay_method": "cash",
                "mileage": "4266"}
        data.update(over)
        return self.client.post("/issue", data=data)

    # ─── мастер ───

    def test_wizard_shows_mileage_on_bike_and_summary_steps(self):
        page = self.get_ok(f"/issue?client={self.client_id}&tariff={self.tariff_id}"
                           f"&model=Kugoo+V3")
        self.assertIn("4266 км", page)
        page = self.get_ok(f"/issue?client={self.client_id}&tariff={self.tariff_id}"
                           f"&bike={self.bike_id}")
        self.assertIn("Пробег при выдаче", page)
        self.assertIn('name="mileage" inputmode="numeric" value="4266"', page)

    def test_issue_writes_mileage_to_rental_and_bike(self):
        r = self.issue_form(mileage="4300")
        self.assertEqual(r.status_code, 303)
        rental = tw.run(self.crm.active_rental_of(self.client_id))
        self.assertEqual(rental["mileage_start"], 4300)
        self.assertIsNone(rental["mileage_end"])
        self.assertEqual(tw.run(self.crm.bike(self.bike_id))["mileage_km"], 4300,
                         "одометр парка подтянулся к введённому")
        self.assertIn("с 4300 км", self.get_ok(f"/rentals/{rental['id']}"))

    def test_issue_without_mileage_is_refused(self):
        r = self.issue_form(mileage="")
        self.assertEqual(r.status_code, 303)
        self.assertIn("Пробег", self.get_ok(r.headers["location"]))
        self.assertIsNone(tw.run(self.crm.active_rental_of(self.client_id)))

    def test_issue_with_smaller_mileage_is_refused(self):
        r = self.issue_form(mileage="4000")
        self.assertEqual(r.status_code, 303)
        self.assertIn("меньше прежнего", self.get_ok(r.headers["location"]))
        self.assertIsNone(tw.run(self.crm.active_rental_of(self.client_id)))
        self.assertEqual(tw.run(self.crm.bike(self.bike_id))["mileage_km"], 4266)

    # ─── возврат ───

    def close(self, **over):
        rental = tw.run(self.crm.active_rental_of(self.client_id))
        data = {"closed_on": "", "bike_status": "available", "note": ""}
        data.update(over)
        return rental, self.client.post(f"/rentals/{rental['id']}/close", data=data)

    def test_close_with_mileage_counts_the_ride(self):
        self.issue_form(mileage="4266")
        rental, r = self.close(mileage="4586")
        self.assertEqual(r.status_code, 303)
        fresh = tw.run(self.crm.rental(rental["id"]))
        self.assertEqual(fresh["mileage_end"], 4586)
        self.assertEqual(logic.ridden(fresh), 320)
        self.assertEqual(tw.run(self.crm.bike(self.bike_id))["mileage_km"], 4586)
        page = self.get_ok(f"/rentals/{rental['id']}")
        self.assertIn("Накатал 320 км", page)
        self.assertIn("накатал 320 км", page)

    def test_close_without_mileage_still_closes(self):
        self.issue_form(mileage="4266")
        rental, r = self.close(mileage="")
        self.assertEqual(r.status_code, 303)
        fresh = tw.run(self.crm.rental(rental["id"]))
        self.assertEqual(fresh["status"], "closed")
        self.assertIsNone(fresh["mileage_end"])
        self.assertIsNone(logic.ridden(fresh))
        self.assertNotIn("Накатал", self.get_ok(f"/rentals/{rental['id']}"))

    def test_close_with_smaller_mileage_is_refused(self):
        self.issue_form(mileage="4266")
        rental, r = self.close(mileage="100")
        self.assertEqual(r.status_code, 303)
        self.assertIn("меньше прежнего", self.get_ok(f"/rentals/{rental['id']}"))
        self.assertEqual(tw.run(self.crm.rental(rental["id"]))["status"], "active",
                         "аренда не закрыта из-за опечатки в пробеге")

    # ─── парк ───

    def test_park_list_and_card_show_and_edit_mileage(self):
        # в списке парка тысячи разделены неразрывным пробелом
        self.assertIn("4\u00a0266 км", self.get_ok("/bikes"))
        r = self.client.post(f"/bikes/{self.bike_id}/edit", data={
            "code": "B-1", "model": "Kugoo V3", "mileage_km": "5000",
            "battery_count": "2", "service_months": "24",
            "battery_service_months": "15"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(tw.run(self.crm.bike(self.bike_id))["mileage_km"], 5000)

    def test_bike_edit_keeps_mileage_when_field_left_empty(self):
        r = self.client.post(f"/bikes/{self.bike_id}/edit", data={
            "code": "B-1", "model": "Kugoo V3", "mileage_km": "",
            "battery_count": "2", "service_months": "24",
            "battery_service_months": "15"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(tw.run(self.crm.bike(self.bike_id))["mileage_km"], 4266)

    def test_new_bike_accepts_mileage(self):
        r = self.client.post("/bikes", data={
            "code": "B-9", "model": "Truck+", "mileage_km": "1200",
            "battery_count": "2", "service_months": "24",
            "battery_service_months": "15"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(tw.run(self.crm.bike_by_code("B-9"))["mileage_km"], 1200)


if __name__ == "__main__":
    unittest.main()
