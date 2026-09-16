"""Витрина свободных велосипедов и «техника готова».

Два уведомления, которые бьют по делу: свободный велосипед - это прямой
простой, а клиентский ремонт - человек, который ждёт свою технику.
"""

from __future__ import annotations

import sys
import unittest
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


class TestFreeBikesPost(unittest.TestCase):
    def bikes(self):
        return [{"status": "available", "model": "Truck+"},
                {"status": "available", "model": "Truck+"},
                {"status": "available", "model": "Kugoo V3"},
                {"status": "rented", "model": "Kugoo V3"},
                {"status": "repair", "model": "Truck+"},
                {"status": "lost", "model": "Truck+"}]

    def test_post_counts_only_free_bikes(self):
        post = logic.free_bikes_post(
            self.bikes(), [{"active": True, "price": D(3000), "period_days": 7}])
        self.assertEqual(post["total"], 3)
        self.assertIn("Truck+ — 2 шт.", post["lines"])
        self.assertIn("Kugoo V3 — 1 шт.", post["lines"])
        # 3000 за 7 дней - это 428,57 в день
        self.assertIn("428", post["price"])

    def test_nothing_free_means_no_post(self):
        self.assertIsNone(logic.free_bikes_post(
            [{"status": "rented", "model": "Truck+"}], []))

    def test_post_without_tariffs_still_works(self):
        post = logic.free_bikes_post(self.bikes(), [])
        self.assertEqual(post["price"], "")

    def test_models_are_sorted_by_count(self):
        post = logic.free_bikes_post(self.bikes(), [])
        self.assertLess(post["lines"].index("Truck+"), post["lines"].index("Kugoo V3"))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestRepairReady(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def close_order(self, **over):
        data = {"bike_id": "", "payer": "client", "client_id": self.client_id,
                "object_note": "Самокат Kugoo M4", "estimate": "0",
                "complaint": "не едет"}
        data.update(over)
        self.client.post("/orders", data=data)
        order = tw.run(self.crm.work_orders())[0]
        self.client.post(f"/orders/{order['id']}/items", data={
            "title": "Диагностика", "qty": "1", "price": "1500",
            "parts_cost": "0", "labor_cost": "200"})
        return self.client.post(f"/orders/{order['id']}/close", data={}), order

    def test_client_is_told_his_gear_is_ready(self):
        r, order = self.close_order()
        self.assertEqual(r.status_code, 303)
        sent = [m for m in self.bot.sent if m[0] == 5001]
        self.assertEqual(len(sent), 1)
        self.assertIn(order["no"], sent[0][1])
        self.assertIn("Самокат Kugoo M4", sent[0][1])
        self.assertIn("1 500 ₽", sent[0][1])

    def test_own_repair_is_silent(self):
        """Свой парк чинится молча: ждать там нечего и некому."""
        self.client.post("/orders", data={
            "bike_id": self.bike_id, "payer": "own", "estimate": "0",
            "complaint": "не едет"})
        order = tw.run(self.crm.work_orders())[0]
        self.client.post(f"/orders/{order['id']}/close",
                         data={"bike_status": "available"})
        self.assertEqual([m for m in self.bot.sent if m[0] == 5001], [])

    def test_channel_post_switch_is_saved(self):
        r = self.client.post("/reports/referrals",
                             data={"bonus": "500", "min_payment": "1000",
                                   "enabled": "1", "free_bikes": "1"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(tw.run(self.crm.settings())["free_bikes_post"], "1")
        page = self.get_ok("/reports/referrals")
        self.assertIn("Публиковать свободные велосипеды", page)


try:
    import test_cabinet as tc
    HAVE_AIOGRAM = tc.HAVE_AIOGRAM
except ImportError:                                    # pragma: no cover
    HAVE_AIOGRAM = False


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestFreeBikesInDailyPass(tc.CabinetCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.crm.create_bike(code="B-1", model="Truck+")
        await self.crm.create_bike(code="B-2", model="Truck+")
        await self.crm.create_tariff("Неделя", 7, D(3000), None)

    async def run_pass(self):
        from app.crm import billing
        await billing.run_daily(self.bot, self.db, self.crm, self.cfg,
                                today=tc.date.today())

    def channel_texts(self):
        return [t for t in self.texts_to(self.cfg.channel_id)]

    async def test_switch_off_means_no_post(self):
        await self.run_pass()
        self.assertEqual(self.channel_texts(), [])

    async def test_post_goes_to_the_channel(self):
        await self.crm.set_setting("free_bikes_post", "1", by="test")
        await self.run_pass()
        posts = self.channel_texts()
        self.assertEqual(len(posts), 1)
        self.assertIn("Truck+ — 2 шт.", posts[0])
        self.assertIn("Всего готово к выдаче: 2", posts[0])

    async def test_nothing_free_nothing_posted(self):
        await self.crm.set_setting("free_bikes_post", "1", by="test")
        for bike in await self.crm.bikes(limit=10):
            await self.crm.update_bike(bike["id"], status="rented")
        await self.run_pass()
        self.assertEqual(self.channel_texts(), [])


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
