"""Сотрудник и его Telegram: одноразовый код, привязка, наряды в бот.

Техник получает наряды в боте, а не ходит за ними в панель. Проверяется
и обратное: код гаснет после применения, чужой Telegram к сотруднику не
привязывается, а правка сметы не шлёт «на тебя наряд» второй раз.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

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
    import test_flow as tf
    from aiogram.methods import SetChatMenuButton
    from aiogram.types import ForceReply
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

    def test_panel_app_url_needs_an_https_domain(self):
        self.assertEqual(logic.panel_app_url("crm.example.ru"), "https://crm.example.ru/")
        self.assertEqual(logic.panel_app_url(" HTTPS://CRM.Example.ru/ "),
                         "https://crm.example.ru/")
        self.assertEqual(logic.panel_app_url("http://crm.example.ru"),
                         "https://crm.example.ru/", "Mini App - только https")
        for bad in ("", None, "crm example.ru", "crm.example.ru/путь", "javascript:x"):
            self.assertIsNone(logic.panel_app_url(bad), bad)

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

    def test_unlink_takes_the_crm_button_back(self):
        tw.run(self.crm.link_staff_tg(self.tech_id, TECH_ID, "petr"))
        self.client.post(f"/staff/{self.tech_id}/telegram", data={"unlink": "1"})
        self.assertEqual([chat for chat, _ in self.bot.menus], [TECH_ID])
        self.assertEqual(self.bot.menus[0][1].type, "default")

    def test_disabling_takes_the_crm_button_back_enabling_does_not(self):
        tw.run(self.crm.link_staff_tg(self.tech_id, TECH_ID, "petr"))
        self.client.post(f"/staff/{self.tech_id}/toggle")
        self.assertFalse(self.tech()["active"])
        self.assertEqual([chat for chat, _ in self.bot.menus], [TECH_ID])
        self.client.post(f"/staff/{self.tech_id}/toggle")
        self.assertTrue(self.tech()["active"])
        self.assertEqual(len(self.bot.menus), 1, "включение кнопку не ставит: /crm")

    def test_panel_pages_carry_the_telegram_bridge(self):
        """Страницы за входом - со скриптом Mini App; страница входа живёт
        без скриптов, как и была."""
        self.assertIn('src="/static/tg.js', self.get_ok("/"))
        self.client.post("/logout")
        self.assertNotIn("<script", self.get_ok("/login"))
        script = self.client.get("/static/tg.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("web_app_expand", script.text)

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


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestCrmAppInBot(tc.CabinetCase):
    """«/crm» - панель CRM внутри Telegram (Mini App). Вход - логин и пароль
    в форме самой панели: бот пароля не спрашивает и не видит."""

    DOMAIN = "crm.example.ru"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.bot.session.close()
        (self.dp, self.bot, self.db, self.crm, self.session,
         self.cfg, self.vault) = tc.build(tf.make_config(crm_domain=self.DOMAIN))

    def calls(self, kind):
        return [c for c in self.session.calls if isinstance(c, kind)]

    def buttons(self, chat_id):
        markup = next(m.reply_markup for m in reversed(self.session.sent_to(chat_id))
                      if m.reply_markup is not None)
        return [b for row in markup.inline_keyboard for b in row]

    async def linked_tech(self, active=True):
        staff_id = await self.crm.create_staff("petr", "hash", "Пётр", "manager")
        await self.crm.link_staff_tg(staff_id, TECH_ID, "petr")
        if not active:
            await self.crm.set_staff_active(staff_id, False)
        return staff_id

    async def test_command_opens_the_panel_inside_telegram(self):
        await self.feed(tc.msg("/crm", user_id=TECH_ID, chat_id=TECH_ID))
        self.assertIn("логин и пароль", self.last_text(TECH_ID))
        app, browser = self.buttons(TECH_ID)
        self.assertEqual(app.web_app.url, f"https://{self.DOMAIN}/")
        self.assertEqual(browser.url, f"https://{self.DOMAIN}/")

    async def test_password_is_never_asked_in_the_chat(self):
        """Бот не ждёт пароля ответом: ни просьбы, ни поля для ответа."""
        await self.feed(tc.msg("/crm", user_id=TECH_ID, chat_id=TECH_ID))
        self.assertIn("не сообщением сюда", self.last_text(TECH_ID))
        self.assertFalse(any(isinstance(m.reply_markup, ForceReply)
                             for m in self.session.sent_to(TECH_ID)))

    async def test_linked_employee_gets_the_menu_button(self):
        await self.linked_tech()
        await self.feed(tc.msg("/crm", user_id=TECH_ID, chat_id=TECH_ID))
        menus = self.calls(SetChatMenuButton)
        self.assertEqual(len(menus), 1)
        self.assertEqual(menus[0].chat_id, TECH_ID)
        self.assertEqual(menus[0].menu_button.web_app.url, f"https://{self.DOMAIN}/")

    async def test_stranger_and_disabled_get_no_menu_button(self):
        await self.feed(tc.msg("/crm", user_id=9300, chat_id=9300))
        await self.linked_tech(active=False)
        await self.feed(tc.msg("/crm", user_id=TECH_ID, chat_id=TECH_ID))
        self.assertEqual(self.calls(SetChatMenuButton), [],
                         "за кнопкой всё равно вход по паролю, но в меню её нет")

    async def test_works_without_channel_subscription(self):
        self.session.subscribed = False
        await self.feed(tc.msg("/crm", user_id=TECH_ID, chat_id=TECH_ID))
        self.assertIn("логин и пароль", self.last_text(TECH_ID))

    async def test_link_puts_the_menu_button_and_says_so(self):
        staff_id = await self.crm.create_staff("petr", "hash", "Пётр", "manager")
        await self.crm.set_staff_link_code(staff_id, "AB3D9K2M")
        await self.feed(tc.msg("/staff AB3D9K2M", user_id=TECH_ID, chat_id=TECH_ID))
        self.assertIn("/crm", self.last_text(TECH_ID))
        self.assertEqual(len(self.calls(SetChatMenuButton)), 1)

    async def test_group_is_sent_to_the_private_chat(self):
        """Кнопку Mini App Telegram в группе не примет - там подсказка. В
        группу /crm доходит только из тем рабочей группы точек: прочие
        групповые сообщения конвейер отбрасывает раньше."""
        answers = []

        async def answer(text, **kwargs):
            answers.append((text, kwargs))
        message = SimpleNamespace(answer=answer, chat=SimpleNamespace(type="supergroup"),
                                  from_user=SimpleNamespace(id=TECH_ID))
        await tc.staff_h.cmd_crm(message, bot=self.bot, crm=self.crm, cfg=self.cfg)
        self.assertEqual(len(answers), 1)
        self.assertIn("в личке", answers[0][0])
        self.assertNotIn("reply_markup", answers[0][1])
        self.assertEqual(self.calls(SetChatMenuButton), [])


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestCrmAppWithoutDomain(tc.CabinetCase):
    async def test_no_https_domain_no_button(self):
        await self.feed(tc.msg("/crm", user_id=TECH_ID, chat_id=TECH_ID))
        self.assertIn("CRM_DOMAIN", self.last_text(TECH_ID))
        self.assertFalse(any(m.reply_markup for m in self.session.sent_to(TECH_ID)))

    async def test_link_without_domain_says_nothing_about_crm(self):
        staff_id = await self.crm.create_staff("petr", "hash", "Пётр", "manager")
        await self.crm.set_staff_link_code(staff_id, "AB3D9K2M")
        await self.feed(tc.msg("/staff AB3D9K2M", user_id=TECH_ID, chat_id=TECH_ID))
        self.assertNotIn("/crm", self.last_text(TECH_ID))


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
