"""Реферальная программа: курьер приводит курьера.

Проверяется путь целиком - переход по ссылке на /start, карточка друга,
его аренда, первый платёж и бонус агенту. Главное: бонус пишется в журнал
корректировкой, а не платежом, иначе он завысил бы средний чек парка,
за который никто не платил.
"""

from __future__ import annotations

import importlib
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

try:
    import test_cabinet as tc
    HAVE_AIOGRAM = tc.HAVE_AIOGRAM
except ImportError:                                    # pragma: no cover
    HAVE_AIOGRAM = False

D = Decimal
FRIEND_ID = 8008


class TestReferralLogic(unittest.TestCase):
    def test_code_has_no_lookalike_characters(self):
        """Код диктуют голосом: ноль и «О» в нём не встречаются."""
        for _ in range(50):
            code = logic.make_ref_code()
            self.assertEqual(len(code), logic.REF_CODE_LEN)
            self.assertFalse(set(code) & set("01ILO"))

    def test_code_is_cleaned_from_any_input(self):
        self.assertEqual(logic.clean_ref_code(" ab3d9k "), "AB3D9K")
        self.assertEqual(logic.clean_ref_code("мой код AB3D9K"), "AB3D9K")
        self.assertEqual(logic.clean_ref_code("AB3"), "")
        self.assertEqual(logic.clean_ref_code(None), "")

    def test_link_falls_back_to_the_code(self):
        self.assertEqual(logic.ref_link("@MyBot", "AB3D9K"),
                         "https://t.me/MyBot?start=AB3D9K")
        self.assertEqual(logic.ref_link("", "AB3D9K"), "AB3D9K")

    def test_funnel_counts_every_step_the_friend_passed(self):
        rows = [{"status": "paid", "bonus": D(500)}, {"status": "rented"},
                {"status": "click"}, {"status": "signed"}]
        funnel = logic.ref_funnel(rows)
        # заплативший остаётся и в «перешёл»: иначе воронка сужалась бы задним числом
        self.assertEqual(funnel["click"], 4)
        self.assertEqual(funnel["signed"], 3)
        self.assertEqual(funnel["rented"], 2)
        self.assertEqual(funnel["paid"], 1)
        self.assertEqual(funnel["bonus"], D("500.00"))
        self.assertEqual(funnel["conversion"], 25.0)
        self.assertEqual(funnel["price"], D("500.00"))
        self.assertIsNone(logic.ref_funnel([])["conversion"])

    def test_agents_are_sorted_by_paying_friends(self):
        rows = [{"agent_id": 1, "agent_name": "Иван", "status": "click"},
                {"agent_id": 2, "agent_name": "Пётр", "status": "paid", "bonus": D(500)}]
        agents = logic.ref_agents(rows)
        self.assertEqual([a["agent_name"] for a in agents], ["Пётр", "Иван"])
        self.assertEqual(agents[0]["bonus"], D("500.00"))

    def test_settings_survive_garbage(self):
        default = logic.ref_settings({})
        self.assertEqual(default["bonus"], logic.REF_BONUS_DEFAULT)
        self.assertTrue(default["enabled"])
        broken = logic.ref_settings({"ref_bonus": "ой", "ref_min_payment": "-5",
                                     "ref_enabled": "0"})
        self.assertEqual(broken["bonus"], logic.REF_BONUS_DEFAULT)
        self.assertEqual(broken["min_payment"], logic.REF_MIN_PAYMENT_DEFAULT)
        self.assertFalse(broken["enabled"])


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestReferralsInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.agent_id = tw.run(self.crm.create_client(
            full_name="Агент Агентов", phone="+79991112233", tg_id=4242))
        tw.run(self.crm.set_ref_code(self.agent_id, "AB3D9K"))
        tw.run(self.crm.add_referral(agent_id=self.agent_id, tg_id=5001))
        ref = tw.run(self.crm.referral_of_tg(5001))
        tw.run(self.crm.update_referral(ref["id"], client_id=self.client_id,
                                        status="rented"))

    def pay(self, amount="3000"):
        return self.client.post(f"/clients/{self.client_id}/ledger",
                                data={"kind": "payment", "amount": amount,
                                      "method": "sbp", "note": ""})

    def test_friend_payment_pays_the_agent_a_bonus(self):
        self.pay()
        ref = tw.run(self.crm.referral_of_client(self.client_id))
        self.assertEqual(ref["status"], "paid")
        self.assertEqual(ref["bonus"], logic.REF_BONUS_DEFAULT)
        rows = tw.run(self.crm.ledger_of(self.agent_id, limit=10))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "bonus",
                         "бонус агенту - баллы, как и бонус другу: платежом он "
                         "завысил бы средний чек, а корректировкой смешался бы "
                         "с ручными правками и не попал в плитку «оплачено баллами»")
        self.assertEqual(rows[0]["amount"], logic.REF_BONUS_DEFAULT)

    def test_bonus_is_paid_once(self):
        self.pay()
        self.pay()
        self.assertEqual(len(tw.run(self.crm.ledger_of(self.agent_id, limit=10))), 1)

    def test_small_payment_does_not_buy_a_bonus(self):
        """Сто рублей с карты на карту знакомого бонуса не приносят."""
        self.pay("100")
        self.assertEqual(tw.run(self.crm.referral_of_client(self.client_id))["status"],
                         "rented")
        self.assertEqual(tw.run(self.crm.ledger_of(self.agent_id, limit=10)), [])

    def test_disabled_programme_pays_nothing(self):
        tw.run(self.crm.set_setting("ref_enabled", "0", by="staff:admin"))
        self.pay()
        self.assertEqual(tw.run(self.crm.ledger_of(self.agent_id, limit=10)), [])

    def test_blocked_agent_gets_nothing(self):
        tw.run(self.crm.update_client(self.agent_id, status="blacklist"))
        self.pay()
        self.assertEqual(tw.run(self.crm.ledger_of(self.agent_id, limit=10)), [])

    def test_rental_marks_the_friend_step(self):
        tw.run(self.crm.update_referral(
            tw.run(self.crm.referral_of_client(self.client_id))["id"], status="signed"))
        r = self.client.post("/issue", data={
            "client_id": self.client_id, "tariff_id": self.tariff_id,
            "bike_id": self.bike_id, "started_on": tw.date.today().isoformat(),
            "contract_no": "АВ-1", "pay": "0", "method": "sbp",
            "mileage": "1200"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(tw.run(self.crm.referral_of_client(self.client_id))["status"],
                         "rented")

    def test_report_shows_funnel_and_agents(self):
        self.pay()
        page = self.get_ok("/reports/referrals")
        self.assertIn("Агент Агентов", page)
        self.assertIn("AB3D9K", page)
        self.assertIn("Заплатил", page)

    def test_settings_are_saved(self):
        r = self.client.post("/reports/referrals",
                             data={"bonus": "700", "min_payment": "2000", "enabled": "1"})
        self.assertEqual(r.status_code, 303)
        settings = logic.ref_settings(tw.run(self.crm.settings()))
        self.assertEqual(settings["bonus"], D("700.00"))
        self.assertEqual(settings["min_payment"], D("2000.00"))
        self.pay("1500")
        self.assertEqual(tw.run(self.crm.ledger_of(self.agent_id, limit=10)), [],
                         "платёж ниже нового порога бонуса не даёт")

    def test_report_warns_when_the_programme_has_no_sums(self):
        tw.run(self.crm.set_setting("ref_bonus", "0", by="t"))
        tw.run(self.crm.set_setting("ref_friend_bonus", "0", by="t"))
        self.assertIn("суммы бонусов не заданы", self.get_ok("/reports/referrals"))
        tw.run(self.crm.set_setting("ref_bonus", "500", by="t"))
        self.assertNotIn("суммы бонусов не заданы", self.get_ok("/reports/referrals"))

    def test_report_is_money_only(self):
        profile = tw.run(self.crm.access_profile_by_code("tech"))
        tw.run(self.crm.create_staff("petr", logic.hash_password("password-1"),
                                     "Пётр", "manager", profile["id"]))
        self.client.post("/logout")
        self.login("petr", "password-1")
        self.assertEqual(self.client.get("/reports/referrals").status_code, 403)
        self.assertEqual(self.client.post("/reports/referrals",
                                          data={"bonus": "1"}).status_code, 403)


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestReferralsInBot(tc.CabinetCase):
    async def agent(self):
        client = await self.crm_client(tg_id=tc.USER_ID)
        await self.crm.set_ref_code(client["id"], "AB3D9K")
        return await self.crm.client(client["id"])

    async def test_start_with_code_records_the_door(self):
        agent = await self.agent()
        await self.feed(tc.msg("/start AB3D9K", user_id=FRIEND_ID, chat_id=FRIEND_ID))
        ref = await self.crm.referral_of_tg(FRIEND_ID)
        self.assertIsNotNone(ref, "переход по ссылке должен быть записан")
        self.assertEqual(ref["agent_id"], agent["id"])
        self.assertEqual(ref["status"], "click")

    async def test_own_code_is_not_a_referral(self):
        await self.agent()
        await self.feed(tc.msg("/start AB3D9K"))
        self.assertIsNone(await self.crm.referral_of_tg(tc.USER_ID))

    async def test_second_door_does_not_steal_the_friend(self):
        await self.agent()
        other = await self.crm_client(tg_id=4242, phone="+79995554433", name="Пётр")
        await self.crm.set_ref_code(other["id"], "XY7Z2Q")
        await self.feed(tc.msg("/start AB3D9K", user_id=FRIEND_ID, chat_id=FRIEND_ID))
        await self.feed(tc.msg("/start XY7Z2Q", user_id=FRIEND_ID, chat_id=FRIEND_ID))
        ref = await self.crm.referral_of_tg(FRIEND_ID)
        self.assertEqual(ref["agent_id"], (await self.crm.client_by_tg(tc.USER_ID))["id"])

    async def test_friend_card_links_to_the_door(self):
        """Друг дошёл до договора: карточка появилась - связь закрепилась."""
        agent = await self.agent()
        await self.feed(tc.msg("/start AB3D9K", user_id=FRIEND_ID, chat_id=FRIEND_ID))
        self.approved_user(tg_id=FRIEND_ID, phone="+79993334455")
        await self.feed(tc.msg("/cabinet", user_id=FRIEND_ID, chat_id=FRIEND_ID))
        friend = await self.crm.client_by_tg(FRIEND_ID)
        self.assertIsNotNone(friend)
        ref = await self.crm.referral_of_tg(FRIEND_ID)
        self.assertEqual(ref["status"], "signed")
        self.assertEqual(ref["client_id"], friend["id"])
        self.assertEqual(friend["invited_by"], agent["id"])

    async def test_friends_screen_shows_code_and_link(self):
        await self.agent()
        self.approved_user()
        await self.feed(tc.msg("/cabinet"))
        await self.feed(tc.cb("cab:friends"))
        text = self.last_text()
        self.assertIn("AB3D9K", text)
        self.assertIn("https://t.me/testbot?start=AB3D9K", text)
        self.assertIn("Пока по вашей ссылке никто не приходил", text)

    async def test_friend_hears_the_promise_on_start(self):
        """Друг пришёл по ссылке - бот сразу говорит, что ему за это будет,
        и суммы берёт из настроек панели, а не из кода."""
        await self.agent()
        await self.crm.set_setting("ref_bonus", "500", by="t")
        await self.crm.set_setting("ref_friend_bonus", "300", by="t")
        await self.feed(tc.msg("/start AB3D9K", user_id=FRIEND_ID, chat_id=FRIEND_ID))
        texts = "\n".join(self.texts_to(FRIEND_ID))
        self.assertIn("вам 300 ₽ и другу 500 ₽", texts)

    async def test_friend_without_sums_is_sent_to_the_manager(self):
        await self.agent()
        await self.crm.set_setting("ref_bonus", "0", by="t")
        await self.crm.set_setting("ref_friend_bonus", "0", by="t")
        await self.feed(tc.msg("/start AB3D9K", user_id=FRIEND_ID, chat_id=FRIEND_ID))
        texts = "\n".join(self.texts_to(FRIEND_ID))
        self.assertIn("уточняйте у менеджера", texts)
        self.assertNotIn("0 ₽", texts, "ноль рублей не обещаем")

    async def test_own_start_has_no_promise(self):
        await self.agent()
        await self.feed(tc.msg("/start AB3D9K"))
        self.assertNotIn("по приглашению", "\n".join(self.texts_to(tc.USER_ID)))

    async def test_friends_screen_without_agent_sum_points_to_the_manager(self):
        await self.agent()
        await self.crm.set_setting("ref_bonus", "0", by="t")
        self.approved_user()
        await self.feed(tc.msg("/cabinet"))
        await self.feed(tc.cb("cab:friends"))
        text = self.last_text()
        self.assertIn("AB3D9K", text)
        self.assertIn("уточняйте у менеджера", text)
        self.assertNotIn("0 ₽", text)

    async def test_friends_screen_issues_a_code_when_there_is_none(self):
        client = await self.crm_client(tg_id=tc.USER_ID)
        self.approved_user()
        await self.feed(tc.msg("/cabinet"))
        await self.feed(tc.cb("cab:friends"))
        code = (await self.crm.client(client["id"]))["ref_code"]
        self.assertTrue(code, "код выдаётся при первом открытии экрана")
        self.assertIn(code, self.last_text())

    async def test_disabled_programme_says_so(self):
        await self.agent()
        await self.crm.set_setting("ref_enabled", "0", by="test")
        self.approved_user()
        await self.feed(tc.msg("/cabinet"))
        await self.feed(tc.cb("cab:friends"))
        self.assertIn("выключена", self.last_text())

    async def test_agent_is_told_about_the_bonus(self):
        agent = await self.agent()
        friend = await self.crm_client(tg_id=FRIEND_ID, phone="+79993334455",
                                       name="Друг Друзей")
        await self.crm.add_referral(agent_id=agent["id"], tg_id=FRIEND_ID)
        ref = await self.crm.referral_of_tg(FRIEND_ID)
        await self.crm.update_referral(ref["id"], client_id=friend["id"],
                                       status="rented")
        claim_id = await self.crm.create_claim(friend["id"], D("3000"))
        claim = await self.crm.claim(claim_id)
        await cabinet_module().credit(self.bot, self.db, self.crm, claim, D("3000"),
                                      who="operator")
        self.assertEqual((await self.crm.referral_of_client(friend["id"]))["status"],
                         "paid")
        texts_to_agent = self.texts_to(tc.USER_ID)
        self.assertTrue(any("Бонус" in t for t in texts_to_agent),
                        "агент должен узнать о бонусе")


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestReferralFullCycle(tc.CabinetCase):
    """Друг проходит весь цикл бота по приглашению: договор, оплата, акт.

    Это основной путь выдачи в МАЙБАЙК - через бота, а не через панель.
    Воронка обязана пройти все четыре шага именно на нём. Шаги цикла берём
    у TestBotSync, но не наследуем его тесты - они уже прогоняются там.
    """

    ISSUE_FORM = tc.TestBotSync.ISSUE_FORM
    register_up_to_sign = tc.TestBotSync.register_up_to_sign
    confirm_pay = tc.TestBotSync.confirm_pay

    async def test_invited_friend_walks_the_whole_funnel(self):
        agent_id = await self.crm.create_client(full_name="Агент Агентов",
                                                phone="+79991112233", tg_id=4242)
        await self.crm.set_ref_code(agent_id, "AB3D9K")
        await self.feed(tc.msg("/start AB3D9K"))
        ref = await self.crm.referral_of_tg(tc.USER_ID)
        self.assertEqual(ref["status"], "click")

        await self.register_up_to_sign()
        await self.feed(tc.cb("sign"))
        friend = await self.crm.client_by_tg(tc.USER_ID)
        self.assertEqual((await self.crm.referral_of_tg(tc.USER_ID))["status"],
                         "signed", "договор подписан - связь закреплена")
        self.assertEqual(friend["invited_by"], agent_id)

        await self.confirm_pay()
        await self.feed(tc.cb("act_sign"))
        ref = await self.crm.referral_of_tg(tc.USER_ID)
        self.assertEqual(ref["status"], "paid")
        self.assertEqual(ref["bonus"], logic.REF_BONUS_DEFAULT)
        rows = await self.crm.ledger_of(agent_id, limit=5)
        self.assertEqual([r["kind"] for r in rows], ["bonus"])

    async def test_funnel_marks_the_bike_step_before_payment(self):
        """Аренду из бота оформляет не open_rental - шаг всё равно виден."""
        agent_id = await self.crm.create_client(full_name="Агент Агентов",
                                                phone="+79991112233", tg_id=4242)
        await self.crm.set_ref_code(agent_id, "AB3D9K")
        await self.crm.set_setting("ref_min_payment", "999999", by="test")
        await self.feed(tc.msg("/start AB3D9K"))
        await self.register_up_to_sign()
        await self.feed(tc.cb("sign"))
        await self.confirm_pay()
        await self.feed(tc.cb("act_sign"))
        ref = await self.crm.referral_of_tg(tc.USER_ID)
        self.assertEqual(ref["status"], "rented",
                         "велосипед выдан - шаг воронки отмечен")
        self.assertEqual(await self.crm.ledger_of(agent_id, limit=5), [],
                         "порог не пройден - бонуса нет")


def cabinet_module():
    return importlib.import_module("app.handlers.cabinet")


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
