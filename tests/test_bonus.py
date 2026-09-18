"""Баллы и отзывы.

Красная линия: баллы — не платёж. Они меняют баланс клиента, но в
`payment` не попадают никогда, иначе средний чек — одно из трёх чисел
парка — начал бы врать. Здесь это и стерегут, вместе с защитой от
накрутки приглашений.
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

    from app.crm import billing, service
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class TestBonusLogic(unittest.TestCase):
    def test_bonus_is_its_own_kind_and_adds_to_the_balance(self):
        self.assertIn("bonus", logic.KINDS)
        self.assertEqual(logic.KIND_SIGN["bonus"], 1)
        self.assertNotEqual("bonus", "payment",
                            "средний чек считается по payment — бонус туда не идёт")

    def test_settings_default_to_promising_nothing(self):
        got = logic.bonus_settings({})
        self.assertEqual(got["friend_bonus"], D(0))
        self.assertEqual(got["review_bonus"], D(0))
        self.assertTrue(got["new_only"])
        self.assertEqual(got["spike"], logic.REF_SPIKE_DEFAULT)

    def test_settings_survive_junk(self):
        got = logic.bonus_settings({"ref_friend_bonus": "-5",
                                    "review_bonus": "много",
                                    "ref_spike": "0"})
        self.assertEqual(got["friend_bonus"], D(0))
        self.assertEqual(got["review_bonus"], D(0))
        self.assertEqual(got["spike"], logic.REF_SPIKE_DEFAULT)

    def test_promise_is_silent_without_sums(self):
        self.assertEqual(logic.bonus_promise({"bonus": D(0), "friend_bonus": D(0)}),
                         "", "обещать то, чего владелец не назначал, нельзя")
        self.assertIn("вам", logic.bonus_promise({"bonus": D(0),
                                                  "friend_bonus": D(300)}))
        self.assertIn("другу", logic.bonus_promise({"bonus": D(500),
                                                    "friend_bonus": D(0)}))

    def test_promise_is_addressed_to_the_right_side(self):
        settings = {"bonus": D(500), "friend_bonus": D(300)}
        self.assertEqual(logic.bonus_promise(settings), "вам 300 ₽ и другу 500 ₽",
                         "другу - его сумма первой")
        self.assertEqual(logic.bonus_promise(settings, for_agent=True),
                         "вам 500 ₽ и другу 300 ₽", "агенту - наоборот")
        self.assertEqual(logic.bonus_promise({"bonus": D(500), "friend_bonus": D(0)},
                                             for_agent=True), "вам 500 ₽")

    def test_old_client_is_not_a_new_friend(self):
        now = datetime.now(UTC)
        self.assertTrue(logic.is_new_friend({"created_at": now},
                                            {"created_at": now}))
        self.assertFalse(
            logic.is_new_friend({"created_at": now - timedelta(days=30)},
                                {"created_at": now}),
            "карточка старого клиента заведена раньше перехода по ссылке")
        self.assertTrue(logic.is_new_friend({}, {}),
                        "дат нет — клиента за это не наказываем")

    def test_spikes_show_but_do_not_block(self):
        today = date(2026, 9, 17)
        now = datetime(2026, 9, 17, 12, tzinfo=UTC)
        rows = [{"agent_id": 1, "created_at": now} for _ in range(6)]
        rows += [{"agent_id": 2, "created_at": now} for _ in range(2)]
        rows += [{"agent_id": 1,
                  "created_at": now - timedelta(days=2)} for _ in range(9)]
        got = logic.ref_spikes(rows, limit=5, today=today)
        self.assertEqual(got, [{"agent_id": 1, "friends": 6}],
                         "вчерашние переходы сегодняшним всплеском не считаются")
        self.assertEqual(logic.ref_spikes(rows, limit=50, today=today), [])

    def test_totals_show_the_share_of_payments(self):
        got = logic.bonus_totals(
            [{"kind": "referral", "amount": D(500)},
             {"kind": "review", "amount": D(200)},
             {"kind": "referral", "amount": D(500)}], D(100000))
        self.assertEqual(got["total"], D(1200))
        self.assertEqual(got["share"], D("1.2"))
        self.assertEqual(got["by_kind"]["referral"], D(1000))
        self.assertEqual(got["count"], 3)
        self.assertEqual(logic.bonus_totals([], D(0))["share"], D(0),
                         "деления на ноль быть не должно")


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestBonusFlow(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def setting(self, key, value):
        _run(self.crm.set_setting(key, value, by="тест"))

    def test_bonus_changes_the_balance_but_is_not_a_payment(self):
        _run(service.grant_manual_bonus(
            self.crm, _run(self.crm.client(self.client_id)), D(500),
            note="акция", by="оператор"))
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(500))
        entry = _run(self.crm.ledger_of(self.client_id))[0]
        self.assertEqual(entry["kind"], "bonus")
        totals = _run(self.crm.ledger_totals(since=date(2000, 1, 1)))
        self.assertEqual(totals.get("payment", D(0)), D(0),
                         "в платежи бонус не попал — средний чек цел")
        self.assertEqual(totals.get("bonus"), D(500))

    def test_zero_bonus_is_refused(self):
        with self.assertRaises(service.ServiceError):
            _run(service.grant_manual_bonus(
                self.crm, _run(self.crm.client(self.client_id)), D(0),
                note="", by="оператор"))

    def test_review_bonus_needs_a_sum_and_pays_once(self):
        client = _run(self.crm.client(self.client_id))
        with self.assertRaises(service.ServiceError):
            _run(service.grant_review_bonus(self.crm, client, by="оператор"))
        self.setting("review_bonus", "300")
        got = _run(service.grant_review_bonus(self.crm, client, by="оператор"))
        self.assertEqual(got, D(300))
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(300))
        with self.assertRaises(service.ServiceError):
            _run(service.grant_review_bonus(self.crm, client, by="оператор"))

    def friend_with_referral(self, *, friend_made_at=None):
        agent = self.client_id
        friend = _run(self.crm.create_client(full_name="Друг",
                                             phone="+79990000002", tg_id=6001))
        ref = _run(self.crm.add_referral(agent_id=agent, tg_id=6001))
        _run(self.crm.update_referral(ref, client_id=friend, status="rented"))
        if friend_made_at is not None:
            self.crm.clients_[friend]["created_at"] = friend_made_at
        return agent, friend, ref

    def test_friend_bonus_goes_to_both_when_set(self):
        self.setting("ref_bonus", "500")
        self.setting("ref_friend_bonus", "300")
        self.setting("ref_min_payment", "1000")
        agent, friend, _ = self.friend_with_referral()
        got = _run(service.ref_paid(self.crm, _run(self.crm.client(friend)),
                                    D(3000), by="тест"))
        self.assertIsNotNone(got)
        self.assertEqual(_run(self.crm.client_balance(agent)), D(500))
        self.assertEqual(_run(self.crm.client_balance(friend)), D(300))
        kinds = {b["kind"] for b in _run(self.crm.bonuses())}
        self.assertEqual(kinds, {"referral", "friend"})

    def test_returning_client_earns_nobody_a_bonus(self):
        self.setting("ref_bonus", "500")
        self.setting("ref_min_payment", "1000")
        old = datetime.now(UTC) - timedelta(days=200)
        agent, friend, _ = self.friend_with_referral(friend_made_at=old)
        got = _run(service.ref_paid(self.crm, _run(self.crm.client(friend)),
                                    D(3000), by="тест"))
        self.assertIsNone(got, "«приведи друга» — про новых людей")
        self.assertEqual(_run(self.crm.client_balance(agent)), D(0))

    def test_switch_off_new_only_pays_anyway(self):
        self.setting("ref_bonus", "500")
        self.setting("ref_min_payment", "1000")
        self.setting("ref_new_only", "0")
        old = datetime.now(UTC) - timedelta(days=200)
        agent, friend, _ = self.friend_with_referral(friend_made_at=old)
        self.assertIsNotNone(_run(service.ref_paid(
            self.crm, _run(self.crm.client(friend)), D(3000), by="тест")))
        self.assertEqual(_run(self.crm.client_balance(agent)), D(500))

    def test_spike_report_is_silent_until_there_is_one(self):
        self.setting("ref_spike", "3")
        self.assertEqual(
            _run(billing.report_ref_spikes(self.bot, self.crm, self.cfg,
                                           today=date.today(), chat_id=-1)), 0)
        for tg in (7001, 7002, 7003):
            _run(self.crm.add_referral(agent_id=self.client_id, tg_id=tg))
        self.assertEqual(
            _run(billing.report_ref_spikes(self.bot, self.crm, self.cfg,
                                           today=date.today(), chat_id=-1)), 1)
        self.assertIn("Всплеск приглашений", self.bot.sent[-1][1])
        self.assertIn("ничего не заблокировала", self.bot.sent[-1][1])


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestBonusPages(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def test_settings_save_sums_and_links(self):
        r = self.client.post("/reports/referrals",
                             data={"enabled": "1", "new_only": "1",
                                   "bonus": "500", "friend_bonus": "300",
                                   "min_payment": "1000", "review_bonus": "200",
                                   "spike": "4",
                                   "review_yandex": "https://ya.ru/x",
                                   "review_2gis": "", "review_avito": ""})
        self.assertEqual(r.status_code, 303)
        got = logic.bonus_settings(_run(self.crm.settings()))
        self.assertEqual(got["friend_bonus"], D(300))
        self.assertEqual(got["review_bonus"], D(200))
        self.assertEqual(got["spike"], 4)
        links = logic.review_links(_run(self.crm.settings()))
        self.assertEqual([x["key"] for x in links], ["review_yandex"])

    def test_broken_review_link_is_refused(self):
        self.client.post("/reports/referrals",
                         data={"enabled": "1", "bonus": "500",
                               "friend_bonus": "0", "min_payment": "1000",
                               "review_bonus": "0", "spike": "5",
                               "review_yandex": "ya.ru"})
        self.assertEqual(_run(self.crm.settings()).get("review_yandex"), None)

    def test_manual_bonus_from_the_client_card(self):
        r = self.client.post(f"/clients/{self.client_id}/bonus",
                             data={"action": "manual", "amount": "250",
                                   "note": "извинения за простой"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(250))
        self.assertIn("Баллы", self.get_ok(f"/clients/{self.client_id}"))

    def test_review_bonus_button_says_why_it_cannot(self):
        self.client.post(f"/clients/{self.client_id}/bonus",
                         data={"action": "review"})
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(0),
                         "без заданной суммы начислять нечего")

    def test_referrals_page_shows_what_was_given_away(self):
        _run(self.crm.grant_bonus(client_id=self.client_id, kind="manual",
                                  amount=D(700), note="акция", by="тест"))
        text = self.get_ok("/reports/referrals")
        self.assertIn("Роздано баллами", text)
        self.assertIn("Начислено руками", text)
        self.assertIn("Площадки для отзывов", text)


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
