"""Розыск: кто перестал платить и пропал.

Из ~190 велосипедов ~25 числятся потерянными, и потеря всегда начинается
одинаково: клиент замолчал, а велосипед остался «в аренде», и никто его
не ищет. Розыск - отметка с датой и автором, после которой велосипед
перестаёт быть просто должником.
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

try:
    import test_cabinet as tc
    HAVE_AIOGRAM = tc.HAVE_AIOGRAM
except ImportError:                                    # pragma: no cover
    HAVE_AIOGRAM = False

D = Decimal
TODAY = date(2026, 9, 17)


def rental(**over) -> dict:
    row = {"id": 1, "status": "active", "billed_until": date(2026, 9, 1),
           "balance": D(-3000), "price": D(3000), "period_days": 7,
           "full_name": "Иванов Иван", "bike_code": "B-1", "search_at": None}
    row.update(over)
    return row


class TestSearchLogic(unittest.TestCase):
    def test_settings_survive_garbage(self):
        self.assertEqual(logic.search_settings(None)["search_after"],
                         logic.SEARCH_AFTER_DAYS)
        broken = logic.search_settings({"search_after_days": "ой",
                                        "theft_after_days": "0"})
        self.assertEqual(broken["search_after"], logic.SEARCH_AFTER_DAYS)
        self.assertEqual(broken["theft_after"], logic.THEFT_AFTER_DAYS)
        self.assertEqual(logic.search_settings({"search_after_days": "3"})["search_after"],
                         3)

    def test_overdue_rental_becomes_a_candidate(self):
        rows = logic.search_rows([rental()], settings=logic.search_settings(None),
                                 today=TODAY)
        self.assertEqual(len(rows["candidates"]), 1)
        self.assertEqual(rows["candidates"][0]["overdue_days"], 23)
        self.assertEqual(rows["searching"], [])

    def test_client_with_money_is_not_hunted(self):
        """Просрочка считается по «оплачено до», а не по «начислено до»."""
        paid = rental(balance=D(0))
        rows = logic.search_rows([paid], settings=logic.search_settings(None),
                                 today=date(2026, 9, 3))
        self.assertEqual(rows["candidates"], [])

    def test_declared_search_moves_to_the_other_list(self):
        row = rental(search_at=datetime(2026, 8, 20, tzinfo=UTC))
        rows = logic.search_rows([row], settings=logic.search_settings(None),
                                 today=TODAY)
        self.assertEqual(rows["candidates"], [])
        self.assertEqual(rows["searching"][0]["search_days"], 28)
        self.assertTrue(rows["searching"][0]["theft"], "28 дней больше порога 21")

    def test_closed_rentals_are_ignored(self):
        rows = logic.search_rows([rental(status="closed")],
                                 settings=logic.search_settings(None), today=TODAY)
        self.assertEqual((rows["candidates"], rows["searching"]), ([], []))

    def test_digest_is_silent_when_nobody_is_hunted(self):
        empty = {"candidates": [], "searching": []}
        self.assertEqual(logic.search_digest(empty), "")
        rows = logic.search_rows([rental()], settings=logic.search_settings(None),
                                 today=TODAY)
        self.assertIn("пора в розыск", logic.search_digest(rows))

    def test_search_days_counts_from_the_mark(self):
        self.assertEqual(logic.search_days(rental(), today=TODAY), 0)
        self.assertEqual(
            logic.search_days(rental(search_at=datetime(2026, 9, 10, tzinfo=UTC)),
                              today=TODAY), 7)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestSearchInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.rental_id = tw.run(tw.service.open_rental(
            self.crm, client=tw.run(self.crm.client(self.client_id)),
            bike=tw.run(self.crm.bike(self.bike_id)),
            tariff=tw.run(self.crm.tariff(self.tariff_id)),
            started_on=date.today() - timedelta(days=30), contract_no="АВ-1",
            by="staff:admin"))

    def rental(self):
        return tw.run(self.crm.rental(self.rental_id))

    def declare(self, action="start", note="не отвечает"):
        return self.client.post(f"/rentals/{self.rental_id}/search",
                                data={"action": action, "note": note})

    def test_overdue_rental_shows_up_on_the_page(self):
        page = self.get_ok("/rentals/search")
        self.assertIn("Иванов Иван", page)
        self.assertIn("B-1", page)

    def test_declaring_search_keeps_the_rental_running(self):
        r = self.declare()
        self.assertEqual(r.status_code, 303)
        rental = self.rental()
        self.assertIsNotNone(rental["search_at"])
        self.assertEqual(rental["search_by"], "staff:admin")
        self.assertEqual(rental["search_note"], "не отвечает")
        self.assertEqual(rental["status"], "active", "розыск не закрывает аренду")
        self.assertEqual(tw.run(self.crm.bike(self.bike_id))["status"], "rented")

    def test_second_declaration_is_refused(self):
        self.declare()
        r = self.declare()
        self.assertIn("уже в розыске", self.get_ok(r.headers["location"]))

    def test_search_can_be_lifted(self):
        self.declare()
        self.declare(action="stop")
        self.assertIsNone(self.rental()["search_at"])

    def test_theft_closes_the_rental_and_loses_the_bike(self):
        self.declare()
        before = tw.run(self.crm.client_balance(self.client_id))
        self.declare(action="theft", note="не вернул, телефон недоступен")
        rental = self.rental()
        self.assertEqual(rental["status"], "closed")
        self.assertIn("не вернул", rental["close_note"])
        self.assertEqual(tw.run(self.crm.bike(self.bike_id))["status"], "lost")
        self.assertEqual(tw.run(self.crm.client_balance(self.client_id)), before,
                         "долг клиента остаётся: списывать его - отдельное решение")
        log = tw.run(self.crm.bike_status_log(self.bike_id))
        self.assertEqual(log[0]["to_status"], "lost")
        self.assertEqual(log[0]["changed_by"], "staff:admin")

    def test_rule_is_saved_and_applied(self):
        r = self.client.post("/rentals/search",
                             data={"search_after_days": "60", "theft_after_days": "90"})
        self.assertEqual(r.status_code, 303)
        page = self.get_ok("/rentals/search")
        self.assertIn("Никого", page, "с порогом в 60 дней искать пока некого")

    def test_viewer_cannot_declare(self):
        profile = tw.run(self.crm.access_profile_by_code("tech"))
        tw.run(self.crm.create_staff("petr", logic.hash_password("password-1"),
                                     "Пётр", "manager", profile["id"]))
        self.client.post("/logout")
        self.login("petr", "password-1")
        self.assertEqual(self.declare().status_code, 403)
        self.assertEqual(self.client.post("/rentals/search",
                                          data={"search_after_days": "3"}).status_code,
                         403)


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestSearchInDailyPass(tc.CabinetCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.approved_user()
        self.crm_person = await self.crm_client(tg_id=tc.USER_ID)

    async def run_pass(self):
        from app.crm import billing
        await billing.run_daily(self.bot, self.db, self.crm, self.cfg,
                                today=date.today())

    def chat_texts(self):
        return [t for t in self.texts_to(tc.ADMIN_CHAT) if "Розыск" in t]

    async def test_nobody_to_hunt_is_silent(self):
        await self.crm_rental(self.crm_person)
        await self.run_pass()
        self.assertEqual(self.chat_texts(), [])

    async def test_long_overdue_reaches_the_chat(self):
        await self.crm_rental(self.crm_person, billed_offset=-30)
        await self.run_pass()
        posts = self.chat_texts()
        self.assertEqual(len(posts), 1)
        self.assertIn("пора в розыск", posts[0])


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
