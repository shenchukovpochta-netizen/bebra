"""Привязка карты: после оплаты по ссылке без сохранённой карты бот один
раз за срок объясняет, зачем её привязать, - и только когда это правда.

Стерегут: сообщение уходит только при включённом автосписании и когда
банк уже присылал карты (иначе «спишем сами» - обещание, которого нет);
не уходит, если этой же оплатой карта сохранилась, при автосписании, без
Telegram и при выключенном уведомлении (и тогда срок не съедается); не
чаще раза в 30 дней на клиента и только арендатору; в панели «Счета» -
сколько арендаторов без карты и кто они, списком-карточками, с датой
предложения с карточки клиента (история отправок живёт 30 дней).
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
    from test_paying import FakeAcquiring

    from app import texts
    from app.crm import paying, service
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal


class TestNudgeLogic(unittest.TestCase):
    def test_ready_only_with_autocharge_and_cards_from_the_bank(self):
        self.assertTrue(logic.card_nudge_ready({"autocharge": "1"}, 3))
        self.assertFalse(logic.card_nudge_ready({"autocharge": "1"}, 0),
                         "банк карт не присылал - обещать автосписание нельзя")
        self.assertFalse(logic.card_nudge_ready({"autocharge": "0"}, 3))
        self.assertFalse(logic.card_nudge_ready({}, 3), "по умолчанию выключено")

    def test_renters_without_card(self):
        when = datetime(2026, 9, 20, tzinfo=UTC)
        rentals = [
            {"id": 1, "client_id": 10, "full_name": "Яковлев", "status": "active",
             "bike_code": "B-1", "balance": D(-500), "phone": "+7", "tg_id": 1,
             "card_nudge_at": when},
            {"id": 2, "client_id": 11, "full_name": "Абрамов", "status": "active",
             "bike_code": "B-2", "balance": None, "tg_id": None},
            {"id": 3, "client_id": 12, "full_name": "С картой", "status": "active"},
            {"id": 4, "client_id": 13, "full_name": "Закрыта", "status": "closed"},
            {"id": 5, "client_id": 10, "full_name": "Яковлев", "status": "active"},
        ]
        cards = [{"client_id": 12, "active": True}, {"client_id": 11, "active": False}]
        got = logic.renters_without_card(rentals, cards)
        self.assertEqual([r["client_id"] for r in got], [11, 10],
                         "по ФИО, клиент - одна строка, снятая карта - не карта")
        self.assertEqual(got[1]["nudged_at"], when)
        self.assertEqual(got[0]["balance"], D(0))
        self.assertIsNone(got[0]["nudged_at"])


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestNudgeAfterPayment(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.db.users[5001] = {"tg_id": 5001, "lang": "ru"}
        tw.run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="auto",
            started_on=date.today(), contract_no="АВ-1", created_by="t"))
        tw.run(self.crm.set_setting("autocharge", "1", by="t"))
        tw.run(self.crm.set_setting("autocharge_hour", "10", by="t"))
        # банк уже присылал карты: у соседа карта есть
        other = tw.run(self.crm.create_client(full_name="Сосед", phone="+79990000055"))
        tw.run(self.crm.save_card_token(client_id=other, token="tk-other", mask="1111"))

    def pay(self, *, card=None, kind="link"):
        """Оплата по ссылке целиком: счёт, ответ банка, «оплачено» всем."""
        self.paid = getattr(self, "paid", 0) + 1
        acq = FakeAcquiring(operation_id=f"op-{self.paid}",
                            answers=[{"state": "paid", "status": "APPROVED",
                                      "card": card or {}}])
        client = tw.run(self.crm.client(self.client_id))
        order = tw.run(service.create_pay_order(self.crm, client=client, rental=None,
                                                amount=D(3000), by="кабинет", kind=kind,
                                                acquiring=acq))
        tw.run(service.check_pay_order(self.crm, tw.run(self.crm.pay_order(order["id"])),
                                       acquiring=acq))
        self.bot.sent.clear()
        tw.run(paying.tell_paid(self.bot, self.db, self.crm, self.cfg,
                                tw.run(self.crm.pay_order(order["id"]))))
        return [text for chat, text in self.bot.sent if chat == 5001]

    def nudges(self, sent):
        return [t for t in sent if "Не хотите помнить о дате оплаты" in t]

    def test_link_payment_without_a_card_explains_once(self):
        sent = self.pay()
        self.assertTrue(any("зачислен" in t for t in sent), "сначала - про деньги")
        self.assertEqual(self.nudges(sent), [texts.CAB_CARD_NUDGE.format(hour=10)])
        self.assertIn("около 10:00", self.nudges(sent)[0], "час автосписания из настроек")
        self.assertEqual(self.nudges(self.pay()), [], "раз в 30 дней, не на каждой оплате")
        log = tw.run(self.crm.notice_log(code="card_nudge"))
        self.assertEqual([r["status"] for r in log], ["sent"])
        # срок прошёл - можно снова
        self.crm.clients_[self.client_id]["card_nudge_at"] -= timedelta(days=31)
        self.assertEqual(len(self.nudges(self.pay())), 1)

    def test_card_saved_by_this_payment_needs_no_nudge(self):
        sent = self.pay(card={"token": "tk", "mask": "4477", "expires": "12/28"})
        self.assertEqual(self.nudges(sent), [])
        self.assertIsNone(self.crm.clients_[self.client_id].get("card_nudge_at"))

    def test_quiet_when_it_would_not_be_true(self):
        tw.run(self.crm.set_setting("autocharge", "0", by="t"))
        self.assertEqual(self.nudges(self.pay()), [], "автосписание выключено")
        tw.run(self.crm.set_setting("autocharge", "1", by="t"))
        self.crm.cards_.clear()
        self.assertEqual(self.nudges(self.pay()), [], "банк карт не присылал")
        self.assertIsNone(self.crm.clients_[self.client_id].get("card_nudge_at"),
                          "срок не съеден: включат - предложим на следующей оплате")

    def test_autocharge_and_repair_invoices_are_not_link_payments(self):
        self.assertEqual(self.nudges(self.pay(kind="auto")), [])
        order = dict(tw.run(self.crm.pay_orders(client_id=self.client_id))[0],
                     work_order_id=7, ledger_id=None, kind="link")
        self.bot.sent.clear()
        tw.run(paying.tell_paid(self.bot, self.db, self.crm, self.cfg, order))
        self.assertEqual(self.nudges([t for _, t in self.bot.sent]), [],
                         "счёт за ремонт - не аренда")

    def test_switched_off_notice_keeps_the_term(self):
        tw.run(self.crm.set_notice("card_nudge", enabled=False, at_hour=None, by="t"))
        self.assertEqual(self.nudges(self.pay()), [])
        self.assertIsNone(self.crm.clients_[self.client_id].get("card_nudge_at"))
        tw.run(self.crm.set_notice("card_nudge", enabled=True, at_hour=None,
                                   extra={"every_days": 7}, by="t"))
        self.assertEqual(len(self.nudges(self.pay())), 1)
        self.crm.clients_[self.client_id]["card_nudge_at"] -= timedelta(days=8)
        self.assertEqual(len(self.nudges(self.pay())), 1, "срок - параметр владельца")

    def test_debtor_without_a_rental_is_not_promised_a_period(self):
        """Сдал велосипед и гасит долг по ссылке: «спишем новый период» -
        неправда, автосписание берёт только идущие аренды. И срок
        предложения не съедается до следующей аренды."""
        rental = tw.run(self.crm.active_rental_of(self.client_id))
        tw.run(self.crm.close_rental(rental["id"], closed_on=date.today(), note=None))
        self.assertEqual(self.nudges(self.pay()), [])
        self.assertIsNone(self.crm.clients_[self.client_id].get("card_nudge_at"))

    def test_client_without_telegram_is_not_marked(self):
        tw.run(self.crm.update_client(self.client_id, tg_id=None))
        self.assertEqual(self.nudges(self.pay()), [])
        self.assertIsNone(self.crm.clients_[self.client_id].get("card_nudge_at"))

    def test_english_client_gets_the_translation(self):
        self.db.users[5001]["lang"] = "en"
        sent = self.pay()
        self.assertTrue(any("by card" in t and "around 10:00" in t for t in sent), sent)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestRentersWithoutCardPage(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        tw.run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="auto",
            started_on=date.today(), contract_no="АВ-1", created_by="t"))
        self.second = tw.run(self.crm.create_client(full_name="Петров Пётр",
                                                    phone="+79990000002", tg_id=5002))
        bike = tw.run(self.crm.create_bike(code="B-2", model="Kugoo V3"))
        tw.run(self.crm.create_rental(
            client_id=self.second, bike_id=bike, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="auto",
            started_on=date.today(), contract_no="АВ-2", created_by="t"))

    def test_count_list_and_state(self):
        page = self.get_ok("/payments")
        self.assertIn("Без привязанной карты <span class=\"muted\">· 2 из 2 арендаторов", page)
        self.assertIn("Автосписание выключено", page)
        self.assertIn('data-label="Клиент"', page)
        self.assertIn("Петров Пётр", page)
        tw.run(self.crm.save_card_token(client_id=self.second, token="tk", mask="4477"))
        tw.run(self.crm.set_setting("autocharge", "1", by="t"))
        # предлагали 40 дней назад, срок владельца - 60: история отправок
        # уже вычищена, а бот клиента ещё держит - в списке дата, не прочерк
        tw.run(self.crm.set_notice("card_nudge", enabled=True, at_hour=None,
                                   extra={"every_days": 60}, by="t"))
        nudged = datetime.now(UTC) - timedelta(days=40)
        self.crm.clients_[self.client_id]["card_nudge_at"] = nudged
        self.assertEqual(tw.run(self.crm.notice_log(code="card_nudge")), [])
        page = self.get_ok("/payments")
        self.assertIn("· 1 из 2 арендаторов", page)
        self.assertNotIn(">Петров Пётр<", page, "с картой - не в списке")
        self.assertIn("Бот предлагает привязать карту", page)
        row = page.split("<th>Предлагали</th>")[-1]
        self.assertIn(f'data-label="Предлагали" class="muted">'
                      f'{nudged.astimezone():%d.%m.%Y}', row,
                      "дата с карточки клиента - та, по которой бот держит срок")

    def test_bank_without_cards_is_explained(self):
        tw.run(self.crm.set_setting("autocharge", "1", by="t"))
        page = self.get_ok("/payments")
        self.assertIn("Банк ещё не прислал ни одной карты", page)
        self.assertIn("Карт от банка за всё время: 0", page)

    def test_everyone_has_a_card_hides_the_list(self):
        for cid in (self.client_id, self.second):
            tw.run(self.crm.save_card_token(client_id=cid, token=f"tk{cid}", mask="4477"))
        page = self.get_ok("/payments")
        self.assertIn("· 0 из 2 арендаторов", page)
        self.assertNotIn('<table class="cards">', page)


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
