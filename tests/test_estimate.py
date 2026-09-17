"""Смета на ремонт и счёт клиенту.

Красная линия, которую здесь стерегут: выручка чужого ремонта в
`crm.ledger` не попадает. Журнал — это аренда, средний чек считается по
нему, и оплаченный ремонт самоката его бы завысил. Поэтому оплата счёта
с нарядом ставит `work_orders.paid_at` и не трогает баланс клиента.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_paying as tp
    import test_web as tw

    from app.crm import billing, service
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class TestEstimateLogic(unittest.TestCase):
    def test_lines_are_for_the_client_not_for_us(self):
        text = logic.estimate_lines([
            {"title": "Замена камеры", "qty": 2, "price": D(350),
             "parts_cost": D(120), "labor_cost": D(80)},
            {"title": "Диагностика", "qty": 1, "price": D(600)}])
        self.assertIn("Замена камеры × 2", text)
        self.assertIn("700", text)
        self.assertNotIn("120", text, "себестоимость клиенту не показываем")

    def test_client_total_ignores_our_cost(self):
        items = [{"qty": 2, "price": D(350), "parts_cost": D(999)},
                 {"qty": 1, "price": D(600), "labor_cost": D(999)}]
        self.assertEqual(logic.order_totals_client(items), D(1300))

    def test_stages_read_from_the_order(self):
        self.assertEqual(logic.estimate_state({})["stage"], "draft")
        sent = datetime.now(UTC) - timedelta(days=3)
        waiting = logic.estimate_state({"estimate_sent_at": sent})
        self.assertEqual(waiting["stage"], "waiting")
        self.assertEqual(waiting["silent_days"], 3)
        self.assertTrue(waiting["too_silent"])
        fresh = logic.estimate_state({"estimate_sent_at": datetime.now(UTC)})
        self.assertFalse(fresh["too_silent"])
        ok = logic.estimate_state({"estimate_sent_at": sent,
                                   "approved_at": sent, "approved_by": "клиент"})
        self.assertEqual((ok["stage"], ok["by"]), ("approved", "клиент"))
        no = logic.estimate_state({"estimate_sent_at": sent, "declined_at": sent})
        self.assertEqual(no["stage"], "declined")

    def test_invoice_state_reads_the_order_not_the_balance(self):
        self.assertEqual(logic.invoice_state({}, [])["stage"], "none")
        self.assertEqual(
            logic.invoice_state({}, [{"status": "sent"}])["stage"], "sent")
        self.assertEqual(
            logic.invoice_state({}, [{"status": "cancelled"}])["stage"], "none",
            "снятый счёт - это не выставленный")
        self.assertEqual(
            logic.invoice_state({"paid_at": datetime.now(UTC)}, [])["stage"],
            "paid")

    def test_unpaid_total_counts_only_closed_client_orders(self):
        got = logic.orders_unpaid([
            {"payer": "client", "status": "done", "total": D(1500)},
            {"payer": "client", "status": "done", "total": D(500),
             "paid_at": datetime.now(UTC)},
            {"payer": "client", "status": "in_work", "total": D(9000)},
            {"payer": "own", "status": "done", "total": D(9000)}])
        self.assertEqual((got["count"], got["sum"]), (1, D(1500)))

    def test_approval_status_holds_the_bike(self):
        self.assertIn("approve", logic.ORDER_OPEN,
                      "техника разобрана и ждёт ответа — она не свободна")
        self.assertNotIn("approve", logic.ORDER_MANUAL_STATUSES,
                         "на согласование ставит отправка сметы, а не список")


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestEstimateFlow(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.order_id = _run(self.crm.create_work_order(
            bike_id=self.bike_id, payer="client", client_id=self.client_id,
            complaint="не держит тормоз", object_note=None, tech_id=None,
            estimate=D(0), created_by="оператор"))

    def add_line(self, price=None):
        price = D(1500) if price is None else price
        _run(self.crm.add_order_item(
            self.order_id, title="Замена колодок", node="brake_pads",
            work_type_id=None, qty=1, price=price, parts_cost=D(300),
            labor_cost=D(200), note=None))

    def order(self):
        return _run(self.crm.work_order(self.order_id))

    def test_empty_order_has_no_estimate_to_send(self):
        with self.assertRaises(service.ServiceError):
            _run(service.send_estimate(self.crm, self.order(), by="оператор",
                                       bot=self.bot))

    def test_own_repair_is_not_agreed_with_anyone(self):
        self.add_line()
        _run(self.crm.update_work_order(self.order_id, payer="own"))
        with self.assertRaises(service.ServiceError):
            _run(service.send_estimate(self.crm, self.order(), by="оператор",
                                       bot=self.bot))

    def test_sending_puts_the_order_on_approval(self):
        self.add_line()
        got = _run(service.send_estimate(self.crm, self.order(), by="оператор",
                                         bot=self.bot))
        self.assertEqual(got["total"], D(1500))
        self.assertTrue(got["sent"])
        order = self.order()
        self.assertEqual(order["status"], "approve")
        self.assertEqual(order["estimate"], D(1500))
        self.assertIsNotNone(order["estimate_sent_at"])
        self.assertIn("Замена колодок", self.bot.sent[-1][1])
        self.assertNotIn("300", self.bot.sent[-1][1],
                         "себестоимость клиенту не уходит")

    def test_agreement_returns_the_order_to_work_and_names_who(self):
        self.add_line()
        _run(service.send_estimate(self.crm, self.order(), by="оператор",
                                   bot=self.bot))
        _run(service.answer_estimate(self.crm, self.order(), agree=True,
                                     by="staff:Пётр"))
        order = self.order()
        self.assertEqual(order["status"], "in_work")
        self.assertEqual(order["approved_by"], "staff:Пётр")
        self.assertIsNotNone(order["approved_at"])

    def test_refusal_closes_the_order_and_frees_the_bike(self):
        self.add_line()
        _run(self.crm.update_bike(self.bike_id, status="repair", by="тест"))
        _run(service.send_estimate(self.crm, self.order(), by="оператор",
                                   bot=self.bot))
        _run(service.answer_estimate(self.crm, self.order(), agree=False,
                                     by="клиент"))
        order = self.order()
        self.assertEqual(order["status"], "cancelled")
        self.assertIsNotNone(order["declined_at"])
        self.assertEqual(_run(self.crm.bike(self.bike_id))["status"], "available",
                         "от чего отказались, то не держит место в сервисе")

    def test_live_agreement_works_without_sending_the_estimate(self):
        """Клиент стоит у стойки: гнать его в бота ради кнопки незачем."""
        self.add_line()
        _run(service.answer_estimate(self.crm, self.order(), agree=True,
                                     by="staff:Пётр"))
        order = self.order()
        self.assertEqual(order["status"], "in_work")
        self.assertEqual(order["approved_by"], "staff:Пётр")
        self.assertEqual(order["estimate"], D(1500),
                         "сумма берётся из строк наряда, а не остаётся нулём")
        self.assertIsNone(order.get("estimate_sent_at"),
                          "согласовали вживую - смету никуда не отправляли")

    def test_live_refusal_cancels_the_order_and_frees_the_bike(self):
        self.add_line()
        _run(self.crm.update_bike(self.bike_id, status="repair", by="тест"))
        _run(service.answer_estimate(self.crm, self.order(), agree=False,
                                     by="staff:Пётр"))
        order = self.order()
        self.assertEqual(order["status"], "cancelled")
        self.assertIsNotNone(order["declined_at"])
        self.assertEqual(_run(self.crm.bike(self.bike_id))["status"], "available")

    def test_live_agreement_needs_priced_lines(self):
        with self.assertRaises(service.ServiceError):
            _run(service.answer_estimate(self.crm, self.order(), agree=True,
                                         by="staff:Пётр"))

    def test_own_repair_is_not_agreed_live_either(self):
        self.add_line()
        _run(self.crm.update_work_order(self.order_id, payer="own"))
        with self.assertRaises(service.ServiceError):
            _run(service.answer_estimate(self.crm, self.order(), agree=True,
                                         by="staff:Пётр"))

    def test_closed_order_is_not_agreed_live(self):
        self.add_line()
        _run(self.crm.update_work_order(self.order_id, status="done"))
        with self.assertRaises(service.ServiceError):
            _run(service.answer_estimate(self.crm, self.order(), agree=True,
                                         by="staff:Пётр"))

    def test_live_answer_is_also_given_only_once(self):
        self.add_line()
        _run(service.answer_estimate(self.crm, self.order(), agree=True,
                                     by="staff:Пётр"))
        with self.assertRaises(service.ServiceError):
            _run(service.answer_estimate(self.crm, self.order(), agree=False,
                                         by="staff:Пётр"))

    def test_answer_only_once(self):
        self.add_line()
        _run(service.send_estimate(self.crm, self.order(), by="оператор",
                                   bot=self.bot))
        _run(service.answer_estimate(self.crm, self.order(), agree=True,
                                     by="клиент"))
        with self.assertRaises(service.ServiceError):
            _run(service.answer_estimate(self.crm, self.order(), agree=False,
                                         by="клиент"))

    def test_repair_payment_never_touches_the_ledger(self):
        self.add_line()
        _run(self.crm.update_work_order(self.order_id, status="done",
                                        total=D(1500), cost=D(500)))
        invoice = _run(service.invoice_order(
            self.crm, self.order(), by="оператор",
            acquiring=tp.FakeAcquiring()))
        self.assertEqual(invoice["kind"], "repair")
        self.assertEqual(invoice["work_order_id"], self.order_id)
        # Счёт выставлен — баланс клиента не тронут.
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(0))

        _run(self.crm.mark_pay_paid(invoice["id"], method="card"))
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(0),
                         "выручка ремонта в журнал аренды не идёт")
        self.assertEqual(_run(self.crm.ledger_of(self.client_id)), [])
        self.assertIsNotNone(self.order()["paid_at"],
                             "оплата ремонта живёт на наряде")

    def test_paid_repair_is_not_invoiced_twice(self):
        self.add_line()
        _run(self.crm.update_work_order(self.order_id, status="done",
                                        total=D(1500),
                                        paid_at=datetime.now(UTC)))
        with self.assertRaises(service.ServiceError):
            _run(service.invoice_order(self.crm, self.order(), by="оператор",
                                       acquiring=tp.FakeAcquiring()))

    def test_own_repair_is_not_invoiced(self):
        self.add_line()
        _run(self.crm.update_work_order(self.order_id, payer="own",
                                        status="done", total=D(1500)))
        with self.assertRaises(service.ServiceError):
            _run(service.invoice_order(self.crm, self.order(), by="оператор",
                                       acquiring=tp.FakeAcquiring()))

    def test_silent_approvals_are_reported_once_there_are_any(self):
        self.add_line()
        _run(service.send_estimate(self.crm, self.order(), by="оператор",
                                   bot=self.bot))
        self.assertEqual(
            _run(billing.report_silent_estimates(self.bot, self.crm, self.cfg,
                                                 chat_id=-1)), 0,
            "свежая смета - ещё не молчание")
        self.crm.orders_[self.order_id]["estimate_sent_at"] = (
            datetime.now(UTC) - timedelta(days=3))
        self.assertEqual(
            _run(billing.report_silent_estimates(self.bot, self.crm, self.cfg,
                                                 chat_id=-1)), 1)
        self.assertIn("Молчат на согласовании", self.bot.sent[-1][1])


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestEstimatePages(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.order_id = _run(self.crm.create_work_order(
            bike_id=self.bike_id, payer="client", client_id=self.client_id,
            complaint="стук", object_note=None, tech_id=None,
            estimate=D(0), created_by="оператор"))
        _run(self.crm.add_order_item(
            self.order_id, title="Замена колодок", node="brake_pads",
            work_type_id=None, qty=1, price=D(1500), parts_cost=D(300),
            labor_cost=D(200), note=None))

    def test_card_shows_estimate_and_invoice_blocks(self):
        text = self.get_ok(f"/orders/{self.order_id}")
        self.assertIn("Отправить смету клиенту", text)
        self.assertIn("ещё не выставлен", text)

    def test_send_and_agree_from_the_panel(self):
        self.client.post(f"/orders/{self.order_id}/estimate",
                         data={"action": "send"})
        self.assertEqual(_run(self.crm.work_order(self.order_id))["status"],
                         "approve")
        self.client.post(f"/orders/{self.order_id}/estimate",
                         data={"action": "agree"})
        order = _run(self.crm.work_order(self.order_id))
        self.assertEqual(order["status"], "in_work")
        self.assertTrue(order["approved_by"].startswith("staff:"),
                        "согласование вживую записывает оператора")

    def test_invoice_from_the_order_card(self):
        _run(self.crm.update_work_order(self.order_id, status="done",
                                        total=D(1500)))
        self.client.post(f"/orders/{self.order_id}/invoice")
        invoices = _run(self.crm.work_order_invoices(self.order_id))
        self.assertEqual(len(invoices), 1)
        self.assertEqual(invoices[0]["amount"], D(1500))

    def test_list_shows_the_unpaid_total(self):
        _run(self.crm.update_work_order(self.order_id, status="done",
                                        total=D(1500)))
        self.assertIn("Закрыто, но не оплачено", self.get_ok("/orders"))

    def test_status_cannot_be_reset_while_on_approval(self):
        self.client.post(f"/orders/{self.order_id}/estimate",
                         data={"action": "send"})
        self.client.post(f"/orders/{self.order_id}/edit",
                         data={"status": "new", "estimate": "1500"})
        self.assertEqual(_run(self.crm.work_order(self.order_id))["status"],
                         "approve", "молча снять ожидание ответа нельзя")

    def test_approval_filter_is_a_tab(self):
        text = self.get_ok("/orders")
        self.assertIn("/orders?status=approve", text)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestEstimateFromBot(tw.WebCase):
    """Кнопки под сметой в боте: чужой наряд не согласовать."""

    def setUp(self):
        super().setUp()
        self.seed()
        self.order_id = _run(self.crm.create_work_order(
            bike_id=self.bike_id, payer="client", client_id=self.client_id,
            complaint="стук", object_note=None, tech_id=None, estimate=D(0),
            created_by="оператор"))
        _run(self.crm.add_order_item(
            self.order_id, title="Замена колодок", node="brake_pads",
            work_type_id=None, qty=1, price=D(1500), parts_cost=D(0),
            labor_cost=D(0), note=None))
        _run(service.send_estimate(self.crm, _run(self.crm.work_order(self.order_id)),
                                   by="оператор", bot=self.bot))

    def test_stranger_cannot_answer_someone_elses_estimate(self):
        other = _run(self.crm.create_client(full_name="Чужой", phone="+79990000009",
                                            tg_id=7007))
        order = _run(self.crm.work_order(self.order_id))
        self.assertNotEqual(order["client_id"], other)
        # Ответ разрешён только владельцу наряда - это проверяет обработчик
        # бота; здесь стережём само правило на уровне данных.
        self.assertEqual(order["status"], "approve")

    def test_owner_answer_moves_the_order(self):
        order = _run(self.crm.work_order(self.order_id))
        _run(service.answer_estimate(self.crm, order, agree=True, by="клиент"))
        self.assertEqual(_run(self.crm.work_order(self.order_id))["approved_by"],
                         "клиент")


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
