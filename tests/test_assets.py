"""Основные средства: закупки партиями, износ и остаточная стоимость.

Велосипеды приезжают партиями, а живут поштучно. Закупка помнит, что и
почём взяли; износ считается по сроку службы, потому что срок задан
у каждого велосипеда, а одометры переписывали не каждую выдачу.
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
TODAY = date(2026, 9, 17)


def bike(**over) -> dict:
    row = {"id": 1, "code": "B-1", "model": "Truck+", "status": "available",
           "purchase_price": D(47000), "residual_price": D(5000),
           "service_months": 24, "purchased_on": date(2025, 9, 17),
           "battery_price": D(9000), "battery_count": 2,
           "battery_service_months": 15}
    row.update(over)
    return row


class TestAssetLogic(unittest.TestCase):
    def test_months_between_counts_full_months(self):
        self.assertEqual(logic.months_between(date(2026, 1, 31), date(2026, 2, 28)), 0)
        self.assertEqual(logic.months_between(date(2025, 9, 17), TODAY), 12)
        self.assertEqual(logic.months_between(None, TODAY), 0)

    def test_wear_and_book_value(self):
        self.assertEqual(logic.wear_percent(bike(), today=TODAY), 50.0)
        # 5000 остаточной плюс половина от 42000 амортизируемых
        self.assertEqual(logic.book_value(bike(), today=TODAY), D(26000))

    def test_wear_stops_at_a_hundred(self):
        old = bike(purchased_on=date(2020, 1, 1))
        self.assertEqual(logic.wear_percent(old, today=TODAY), 100.0)
        self.assertEqual(logic.book_value(old, today=TODAY), D(5000),
                         "ниже остаточной не падает")

    def test_bike_without_a_price_has_no_book_value(self):
        self.assertIsNone(logic.book_value(bike(purchase_price=None), today=TODAY))
        self.assertIsNone(logic.wear_percent(bike(purchased_on=None), today=TODAY))

    def test_summary_skips_the_departed(self):
        rows = logic.asset_rows([bike(), bike(id=2, code="B-2", status="written_off"),
                                 bike(id=3, code="B-3", status="sold")], today=TODAY)
        summary = logic.asset_summary(rows)
        self.assertEqual((summary["bikes"], summary["live"]), (3, 1))
        self.assertEqual((summary["written_off"], summary["sold"]), (1, 1))
        self.assertEqual(summary["spent"], D(141000), "вложено - по всем")
        self.assertEqual(summary["book"], D(26000), "остаточная - только по живым")
        self.assertEqual(summary["month"], D("2950.00"))

    def test_rows_put_the_most_worn_first(self):
        rows = logic.asset_rows([bike(), bike(id=2, code="B-2",
                                              purchased_on=date(2020, 1, 1))],
                                today=TODAY)
        self.assertEqual([b["code"] for b in rows], ["B-2", "B-1"])
        self.assertTrue(rows[0]["worn_out"])

    def test_codes_are_parsed_from_any_separator(self):
        codes, error = logic.purchase_codes("B-101, B-102\nB-103  B-104")
        self.assertEqual(codes, ["B-101", "B-102", "B-103", "B-104"])
        self.assertEqual(error, "")

    def test_duplicate_and_empty_codes_are_refused(self):
        self.assertEqual(logic.purchase_codes("B-1 B-1")[0], [])
        self.assertIn("дважды", logic.purchase_codes("B-1 B-1")[1])
        self.assertIn("через пробел", logic.purchase_codes("   ")[1])
        self.assertIn("не больше", logic.purchase_codes(
            " ".join(f"B-{i}" for i in range(200)))[1])


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestAssetsInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def buy(self, **over):
        data = {"codes": "B-101 B-102 B-103", "model": "Truck+",
                "purchased_on": date.today().isoformat(), "supplier_id": "",
                "purchase_price": "47000", "residual_price": "5000",
                "service_months": "24", "battery_count": "2",
                "battery_price": "9000", "battery_service_months": "15",
                "location": "Павлюхина", "note": ""}
        data.update(over)
        return self.client.post("/assets", data=data)

    def test_purchase_creates_the_whole_batch(self):
        r = self.buy()
        self.assertEqual(r.status_code, 303)
        purchases = tw.run(self.crm.purchases())
        self.assertEqual(len(purchases), 1)
        self.assertEqual(purchases[0]["no"], "ЗАК-000001")
        self.assertEqual(purchases[0]["bikes"], 3)
        self.assertEqual(purchases[0]["total"], D(141000))
        bikes = tw.run(self.crm.purchase_bikes(purchases[0]["id"]))
        self.assertEqual([b["code"] for b in bikes], ["B-101", "B-102", "B-103"])
        self.assertEqual(bikes[0]["location"], "Павлюхина")
        self.assertEqual(bikes[0]["purchase_price"], D(47000))

    def test_taken_code_stops_the_whole_batch(self):
        """Половина заведённой партии хуже, чем незаведённая."""
        r = self.buy(codes="B-1 B-201")
        self.assertIn("уже есть в парке", self.get_ok(r.headers["location"]))
        self.assertEqual(tw.run(self.crm.purchases()), [])
        self.assertIsNone(tw.run(self.crm.bike_by_code("B-201")))

    def test_duplicate_in_the_form_is_refused(self):
        r = self.buy(codes="B-101 B-101")
        self.assertIn("дважды", self.get_ok(r.headers["location"]))
        self.assertEqual(tw.run(self.crm.purchases()), [])

    def test_page_shows_wear_and_purchases(self):
        self.buy()
        page = self.get_ok("/assets")
        self.assertIn("ЗАК-000001", page)
        self.assertIn("B-101", page)
        self.assertIn("вложено в парк", page)

    def test_tabs_filter_the_list(self):
        self.buy()
        tw.run(self.crm.update_bike(tw.run(self.crm.bike_by_code("B-101"))["id"],
                                    status="written_off"))
        page = self.get_ok("/assets?tab=written_off")
        self.assertIn("B-101", page)
        self.assertNotIn(">B-102<", page)

    def test_assets_are_money_only(self):
        profile = tw.run(self.crm.access_profile_by_code("tech"))
        tw.run(self.crm.create_staff("petr", logic.hash_password("password-1"),
                                     "Пётр", "manager", profile["id"]))
        self.client.post("/logout")
        self.login("petr", "password-1")
        self.assertEqual(self.client.get("/assets").status_code, 403)

    def test_purchase_needs_park_rights(self):
        manager = tw.run(self.crm.access_profile_by_code("manager"))
        tw.run(self.crm.create_staff("ivan", logic.hash_password("password-1"),
                                     "Иван", "manager", manager["id"]))
        self.client.post("/logout")
        self.login("ivan", "password-1")
        self.get_ok("/assets")
        self.assertEqual(self.buy().status_code, 403)


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
