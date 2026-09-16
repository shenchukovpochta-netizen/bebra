"""Сотрудник и его Telegram: одноразовый код, привязка, наряды в бот.

Техник получает наряды в боте, а не ходит за ними в панель. Проверяется
и обратное: код гаснет после применения, чужой Telegram к сотруднику не
привязывается, а правка сметы не шлёт «на тебя наряд» второй раз.
"""

from __future__ import annotations

import sys
import unittest
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

TECH_ID = 9100


class TestLinkLogic(unittest.TestCase):
    def test_code_is_long_enough_and_readable(self):
        code = logic.make_link_code()
        self.assertEqual(len(code), logic.LINK_CODE_LEN)
        self.assertFalse(set(code) & set("01ILO"))

    def test_code_is_cleaned(self):
        self.assertEqual(logic.clean_link_code(" ab3d9k2m "), "AB3D9K2M")
        self.assertEqual(logic.clean_link_code("ab3d9k2m лишнее"), "AB3D9K2M")
        self.assertEqual(logic.clean_link_code("AB3"), "")

    def test_label_tells_the_state(self):
        self.assertEqual(logic.staff_tg_label({"tg_id": 1, "tg_username": "ivan"}),
                         "@ivan")
        self.assertEqual(logic.staff_tg_label({"tg_id": 1}), "Подключён")
        self.assertEqual(logic.staff_tg_label({"link_code": "AB3D9K2M"}), "Ждёт кода")
        self.assertEqual(logic.staff_tg_label({}), "—")


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestStaffLinkInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        profile = tw.run(self.crm.access_profile_by_code("tech"))
        self.tech_id = tw.run(self.crm.create_staff(
            "petr", logic.hash_password("password-1"), "Пётр", "manager",
            profile["id"]))

    def tech(self):
        return tw.run(self.crm.staff_by_id(self.tech_id))

    def test_code_is_issued_and_shown_once(self):
        r = self.client.post(f"/staff/{self.tech_id}/telegram")
        page = self.get_ok(r.headers["location"])
        code = self.tech()["link_code"]
        self.assertTrue(code)
        self.assertIn(code, page)
        self.assertIn("Ждёт кода", page)

    def test_unlink_clears_everything(self):
        tw.run(self.crm.link_staff_tg(self.tech_id, TECH_ID, "petr"))
        self.client.post(f"/staff/{self.tech_id}/telegram", data={"unlink": "1"})
        tech = self.tech()
        self.assertIsNone(tech["tg_id"])
        self.assertIsNone(tech["link_code"])

    def test_only_staff_editors_can_issue_codes(self):
        manager = tw.run(self.crm.access_profile_by_code("manager"))
        tw.run(self.crm.create_staff("ivan", logic.hash_password("password-1"),
                                     "Иван", "manager", manager["id"]))
        self.client.post("/logout")
        self.login("ivan", "password-1")
        r = self.client.post(f"/staff/{self.tech_id}/telegram")
        self.assertEqual(r.status_code, 403)

    def test_assigned_order_reaches_the_technician(self):
        tw.run(self.crm.link_staff_tg(self.tech_id, TECH_ID, "petr"))
        r = self.client.post("/orders", data={
            "bike_id": self.bike_id, "payer": "own", "estimate": "0",
            "complaint": "не едет", "tech_id": self.tech_id})
        self.assertEqual(r.status_code, 303)
        sent = [m for m in self.bot.sent if m[0] == TECH_ID]
        self.assertEqual(len(sent), 1)
        self.assertIn("РЕМ-000001", sent[0][1])
        self.assertIn("не едет", sent[0][1])

    def test_order_without_telegram_is_silent(self):
        r = self.client.post("/orders", data={
            "bike_id": self.bike_id, "payer": "own", "estimate": "0",
            "complaint": "не едет", "tech_id": self.tech_id})
        self.assertEqual(r.status_code, 303)
        self.assertEqual([m for m in self.bot.sent if m[0] == TECH_ID], [])

    def test_editing_the_estimate_does_not_ping_the_tech_again(self):
        tw.run(self.crm.link_staff_tg(self.tech_id, TECH_ID, "petr"))
        self.client.post("/orders", data={
            "bike_id": self.bike_id, "payer": "own", "estimate": "0",
            "complaint": "не едет", "tech_id": self.tech_id})
        order = tw.run(self.crm.work_orders())[0]
        self.client.post(f"/orders/{order['id']}/edit",
                         data={"status": "in_work", "tech_id": self.tech_id,
                               "estimate": "1500", "note": ""})
        self.assertEqual(len([m for m in self.bot.sent if m[0] == TECH_ID]), 1)

    def test_reassigned_order_reaches_the_new_technician(self):
        other = tw.run(self.crm.create_staff("sergey", logic.hash_password("password-1"),
                                             "Сергей", "manager", None))
        tw.run(self.crm.link_staff_tg(other, 9200, "sergey"))
        self.client.post("/orders", data={
            "bike_id": self.bike_id, "payer": "own", "estimate": "0",
            "complaint": "не едет", "tech_id": ""})
        order = tw.run(self.crm.work_orders())[0]
        self.client.post(f"/orders/{order['id']}/edit",
                         data={"status": "in_work", "tech_id": other,
                               "estimate": "0", "note": ""})
        sent = [m for m in self.bot.sent if m[0] == 9200]
        self.assertEqual(len(sent), 1)


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestStaffLinkInBot(tc.CabinetCase):
    async def staff_with_code(self, code="AB3D9K2M"):
        staff_id = await self.crm.create_staff("petr", "hash", "Пётр", "manager")
        await self.crm.set_staff_link_code(staff_id, code)
        return staff_id

    async def test_code_links_the_account(self):
        staff_id = await self.staff_with_code()
        await self.feed(tc.msg("/staff AB3D9K2M", user_id=TECH_ID, chat_id=TECH_ID))
        person = await self.crm.staff_by_id(staff_id)
        self.assertEqual(person["tg_id"], TECH_ID)
        self.assertIsNone(person["link_code"], "код одноразовый")
        self.assertIn("Наряды на ремонт будут приходить сюда",
                      self.last_text(TECH_ID))

    async def test_used_code_does_not_work_twice(self):
        await self.staff_with_code()
        await self.feed(tc.msg("/staff AB3D9K2M", user_id=TECH_ID, chat_id=TECH_ID))
        await self.feed(tc.msg("/staff AB3D9K2M", user_id=9300, chat_id=9300))
        self.assertIn("Код не подошёл", self.last_text(9300))
        self.assertIsNone(await self.crm.staff_by_tg(9300))

    async def test_command_without_code_explains_itself(self):
        await self.feed(tc.msg("/staff", user_id=TECH_ID, chat_id=TECH_ID))
        self.assertIn("Код выдаёт панель", self.last_text(TECH_ID))

    async def test_disabled_employee_cannot_link(self):
        staff_id = await self.staff_with_code()
        await self.crm.set_staff_active(staff_id, False)
        await self.feed(tc.msg("/staff AB3D9K2M", user_id=TECH_ID, chat_id=TECH_ID))
        self.assertIn("Код не подошёл", self.last_text(TECH_ID))

    async def test_link_works_without_channel_subscription(self):
        """Техник не клиент: читать канал он не обязан."""
        self.session.subscribed = False
        staff_id = await self.staff_with_code()
        await self.feed(tc.msg("/staff AB3D9K2M", user_id=TECH_ID, chat_id=TECH_ID))
        self.assertEqual((await self.crm.staff_by_id(staff_id))["tg_id"], TECH_ID)


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
