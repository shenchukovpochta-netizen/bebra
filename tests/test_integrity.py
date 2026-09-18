"""Расхождения между парком, арендами и нарядами.

Расхождение - это не «некрасиво в базе», а невидимый простой: велосипед,
числящийся в аренде без аренды, не попадает ни в выдачу, ни в ремонт,
и про него не вспомнят до пересчёта.
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

try:
    import test_cabinet as tc
    HAVE_AIOGRAM = tc.HAVE_AIOGRAM
except ImportError:                                    # pragma: no cover
    HAVE_AIOGRAM = False

D = Decimal


def bike(bike_id: int, status: str = "available", code: str = "B-1") -> dict:
    return {"id": bike_id, "code": code, "model": "Kugoo V3", "status": status}


def rental(rental_id: int, bike_id: int | None, client_id: int = 7) -> dict:
    return {"id": rental_id, "bike_id": bike_id, "client_id": client_id,
            "status": "active", "full_name": "Иванов Иван"}


def kinds(issues) -> set[str]:
    return {i["kind"] for i in issues}


class TestIntegrityLogic(unittest.TestCase):
    def test_clean_data_has_no_issues(self):
        issues = logic.integrity_issues([bike(1, "rented"), bike(2, "available", "B-2")],
                                        [rental(9, 1)], {})
        self.assertEqual(issues, [])

    def test_rented_without_rental(self):
        issues = logic.integrity_issues([bike(1, "rented")], [], {})
        self.assertEqual(kinds(issues), {"rented_no_rental"})
        self.assertIn("не попадает", issues[0]["what"])

    def test_rental_without_rented_status(self):
        issues = logic.integrity_issues([bike(1, "available")], [rental(9, 1)], {})
        self.assertEqual(kinds(issues), {"rental_no_bike_status"})
        self.assertIn("второму клиенту", issues[0]["what"])

    def test_rental_without_bike(self):
        issues = logic.integrity_issues([], [rental(9, None)], {})
        self.assertEqual(kinds(issues), {"rental_without_bike"})

    def test_order_on_a_rented_bike(self):
        issues = logic.integrity_issues([bike(1, "rented")], [rental(9, 1)],
                                        {1: {"no": "РЕМ-000001"}})
        self.assertEqual(kinds(issues), {"order_on_rented"})
        self.assertIn("РЕМ-000001", issues[0]["what"])

    def test_repair_without_order(self):
        issues = logic.integrity_issues([bike(1, "repair")], [], {})
        self.assertEqual(kinds(issues), {"repair_no_order"})

    def test_lost_bike_with_active_rental(self):
        issues = logic.integrity_issues([bike(1, "lost")], [rental(9, 1)], {})
        self.assertEqual(kinds(issues), {"lost_with_rental", "rental_no_bike_status"})

    def test_debt_without_rental_skips_kopecks_and_renters(self):
        debtors = [{"id": 7, "full_name": "Иванов", "balance": D(-3000)},
                   {"id": 8, "full_name": "Петров", "balance": D(-100)},
                   {"id": 9, "full_name": "Сидоров", "balance": D(-900)}]
        issues = logic.integrity_issues([bike(1, "rented")], [rental(5, 1, client_id=7)],
                                        {}, debtors)
        # Иванов арендует - с ним и так разговаривают; Петров - копейки.
        self.assertEqual([i["client"]["full_name"] for i in issues
                          if i["kind"] == "debt_without_rental"], ["Сидоров"])

    def test_summary_and_digest(self):
        issues = logic.integrity_issues([bike(1, "rented"), bike(2, "repair", "B-2")],
                                        [], {})
        summary = logic.integrity_summary(issues)
        self.assertEqual(summary["total"], 2)
        digest = logic.integrity_digest(issues)
        self.assertIn("Числится в аренде, а аренды нет: 1", digest)
        self.assertEqual(logic.integrity_digest([]), "")


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestIntegrityInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def test_clean_park_says_so(self):
        page = self.get_ok("/reports/integrity")
        self.assertIn("Расхождений нет", page)

    def test_battery_at_client_without_a_rental_is_reported(self):
        bid = tw.run(self.crm.create_battery(code="9510001", status="available"))
        tw.run(self.crm.update_battery(bid, status="rented", by="t"))
        page = self.get_ok("/reports/integrity")
        self.assertIn("Батарея «у клиента», а аренды нет", page)
        self.assertIn("9510001", page)

    def test_battery_linked_to_a_rental_but_not_rented_is_reported(self):
        rental_id = tw.run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="weekly",
            started_on=date.today(), contract_no="АВ-1", created_by="т"))
        bid = tw.run(self.crm.create_battery(code="9510002", status="available"))
        tw.run(self.crm.update_battery(bid, rental_id=rental_id, status="repair", by="t"))
        page = self.get_ok("/reports/integrity")
        self.assertIn("числится за арендой, а статус не «у клиента»", page)
        # Привели в порядок - расхождение исчезло.
        tw.run(self.crm.update_battery(bid, status="rented", by="t"))
        self.assertNotIn("числится за арендой", self.get_ok("/reports/integrity"))

    def test_broken_bike_status_is_shown(self):
        tw.run(self.crm.update_bike(self.bike_id, status="rented"))
        page = self.get_ok("/reports/integrity")
        self.assertIn("Числится в аренде, а аренды нет", page)
        self.assertIn("B-1", page)

    def test_page_needs_access_to_the_park(self):
        profile = tw.run(self.crm.create_access_profile(
            "Только заявки", {"sections": {"claims": "view"}}))
        tw.run(self.crm.create_staff("ivan", logic.hash_password("password-1"),
                                     "Иван", "manager", profile))
        self.client.post("/logout")
        self.login("ivan", "password-1")
        self.assertEqual(self.client.get("/reports/integrity").status_code, 403)


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestIntegrityInDailyPass(tc.CabinetCase):
    async def run_pass(self):
        from app.crm import billing
        await billing.run_daily(self.bot, self.db, self.crm, self.cfg,
                                today=tc.date.today())

    def chat_texts(self):
        return [t for t in self.texts_to(tc.ADMIN_CHAT) if "Расхождения" in t]

    async def test_clean_data_is_silent(self):
        await self.crm.create_bike(code="B-1", model="Kugoo V3")
        await self.run_pass()
        self.assertEqual(self.chat_texts(), [])

    async def test_issues_reach_the_service_chat(self):
        bike_id = await self.crm.create_bike(code="B-1", model="Kugoo V3")
        await self.crm.update_bike(bike_id, status="rented", by="test")
        await self.run_pass()
        posts = self.chat_texts()
        self.assertEqual(len(posts), 1)
        self.assertIn("Числится в аренде, а аренды нет: 1", posts[0])


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
