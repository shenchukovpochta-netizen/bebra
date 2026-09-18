"""Счета на оплату, эквайринг и автосписание.

Главное, что здесь проверяется: счёт не деньги. Выставленный счёт не
должен трогать баланс клиента, а подтверждённая оплата обязана лечь в
журнал ровно один раз, сколько бы раз банк ни ответил «оплачено».

Сети в тестах нет: клиент банка - заглушка, разбор его ответов
проверяется отдельно, без запросов.
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
from app.services import tochka  # noqa: E402

try:
    import test_web as tw

    from app.crm import paying, service
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class FakeAcquiring:
    """Эквайринг без сети. `answers` - что банк отвечает на статус."""

    def __init__(self, *, link="https://pay.example/1", operation_id="op-1",
                 answers=None, fail=None, charge=None):
        self.token = "t"
        self.customer_code = "c"
        self.link, self.operation_id = link, operation_id
        self.answers = list(answers or [])
        self.fail = fail
        self.charge = charge
        self.calls: list[tuple] = []

    async def payment_link(self, *, amount, purpose, client_email=None,
                           client_phone=None):
        self.calls.append(("link", amount, purpose, client_phone))
        if self.fail:
            raise tochka.TochkaError(self.fail)
        return {"link": self.link, "operation_id": self.operation_id}

    async def payment_status(self, operation_id):
        self.calls.append(("status", operation_id))
        return self.answers.pop(0) if self.answers else {"state": "pending"}

    async def charge_saved_card(self, *, token, amount, purpose,
                                client_email=None, client_phone=None):
        self.calls.append(("charge", token, amount))
        if self.charge is None:
            raise tochka.TochkaError("рекуррентные платежи не подключены")
        return self.charge


class TestPayLogic(unittest.TestCase):
    def test_number_and_purpose(self):
        self.assertEqual(logic.pay_no(7), "СЧТ-000007")
        self.assertEqual(
            logic.pay_purpose({"contract_no": "АВ-42"}, {"bike_code": "B-11"}),
            "Аренда велосипеда № B-11, договор АВ-42")
        self.assertEqual(logic.pay_purpose(None, None), "Аренда велосипеда")

    def test_methods_default_to_all_and_survive_junk(self):
        self.assertEqual(logic.pay_methods({}), ["online", "cash", "transfer"])
        self.assertEqual(logic.pay_methods({"pay_methods": "cash,transfer"}),
                         ["cash", "transfer"])
        self.assertEqual(logic.pay_methods({"pay_methods": "биткоин"}),
                         ["online", "cash", "transfer"],
                         "мусор в настройке не должен запрещать приём денег")

    def test_settings_read_flags_and_hour(self):
        got = logic.pay_settings({"autocharge": "1", "autocharge_hour": "9"})
        self.assertTrue(got["autocharge"])
        self.assertEqual(got["autocharge_hour"], 9)
        self.assertEqual(logic.pay_settings({"autocharge_hour": "99"})["autocharge_hour"],
                         logic.AUTOCHARGE_HOUR, "час вне суток - к умолчанию")
        self.assertFalse(logic.pay_settings({})["autocharge"])

    def test_card_mask_keeps_only_four_digits(self):
        self.assertEqual(logic.card_mask("555555******4477"), "4477")
        self.assertEqual(logic.card_mask("4477"), "4477")
        self.assertEqual(logic.card_mask("77"), "")
        self.assertEqual(logic.card_title({"mask": "4477", "expires": "12/28"}),
                         "•••• 4477 · до 12/28")
        self.assertEqual(logic.card_title(None), "")

    def test_link_expires_after_a_day(self):
        fresh = {"status": "sent", "created_at": datetime.now(UTC)}
        old = {"status": "sent",
               "created_at": datetime.now(UTC) - timedelta(hours=25)}
        self.assertFalse(logic.pay_expired(fresh))
        self.assertTrue(logic.pay_expired(old))
        self.assertFalse(logic.pay_expired({**old, "status": "paid"}),
                         "оплаченный счёт не протухает")

    def test_rows_put_waiting_first_and_summary_counts_money(self):
        now = datetime.now(UTC)
        orders = [
            {"id": 1, "status": "paid", "amount": D(3000), "kind": "link",
             "created_at": now - timedelta(days=2)},
            {"id": 2, "status": "sent", "amount": D(5000), "kind": "link",
             "created_at": now},
            {"id": 3, "status": "failed", "amount": D(1000), "kind": "auto",
             "created_at": now - timedelta(days=1)},
            {"id": 4, "status": "sent", "amount": D(9000), "kind": "link",
             "created_at": now - timedelta(hours=30)},
        ]
        rows = logic.pay_rows(orders, now=now)
        self.assertEqual([r["id"] for r in rows][:2], [2, 4],
                         "ждущие оплату - наверху")
        self.assertTrue(rows[1]["expired"])
        summary = logic.pay_summary(orders, now=now)
        self.assertEqual(summary["waiting_sum"], D(5000),
                         "протухший счёт из ожидания уходит")
        self.assertEqual(summary["paid_sum"], D(3000))
        self.assertEqual(summary["failed"], 1)

    def test_autocharge_takes_only_debtors_with_cards(self):
        rentals = [
            {"id": 1, "client_id": 10, "status": "active", "balance": D(-3000)},
            {"id": 2, "client_id": 11, "status": "active", "balance": D(-500)},
            {"id": 3, "client_id": 12, "status": "active", "balance": D(0)},
            {"id": 4, "client_id": 13, "status": "closed", "balance": D(-9000)},
        ]
        cards = {10: {"token": "a"}, 11: {"token": "b"}, 12: {"token": "c"},
                 13: {"token": "d"}}
        due = logic.autocharge_due(rentals, cards=cards, today=date(2026, 9, 17))
        self.assertEqual([d["client_id"] for d in due], [10, 11],
                         "без долга и по закрытой аренде не списываем")
        self.assertEqual(due[0]["amount"], D(3000))
        self.assertEqual(logic.autocharge_due(rentals, cards={}), [],
                         "без карты списывать нечем")


class TestTochkaPing(unittest.TestCase):
    """Проверка подключения: один лёгкий запрос, ответ банка как есть."""

    @staticmethod
    def client(status, payload):
        class Response:
            def __init__(self):
                self.status = status

            async def json(self, content_type=None):
                return payload

        class Session:
            calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def request(self, method, url, headers=None, **kwargs):
                Session.calls.append((method, url, kwargs.get("params")))
                return Response()

        Session.calls.clear()
        return tochka.TochkaClient(token="t", customer_code="300000",
                                   session_factory=Session), Session

    def test_ping_counts_retailers(self):
        client, session = self.client(200, {"Data": {"Retailer": [{}, {}]}})
        got = tw.run(client.ping())
        self.assertEqual(got, {"retailers": 2})
        method, url, params = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertIn("acquiring/v1.0/retailers", url)
        self.assertEqual(params, {"customerCode": "300000"})

    def test_ping_reports_the_bank_error(self):
        client, _ = self.client(401, {"errors": [{"message": "bad token"}]})
        with self.assertRaises(tochka.TochkaError) as ctx:
            tw.run(client.ping())
        self.assertIn("401", str(ctx.exception))


class TestTochkaParsing(unittest.TestCase):
    def test_states_map_to_three_words(self):
        paid = tochka.payment_state(
            {"Data": {"Operation": [{"status": "APPROVED", "operationId": "op1"}]}})
        self.assertEqual(paid["state"], "paid")
        self.assertEqual(paid["operation_id"], "op1")
        self.assertEqual(tochka.payment_state(
            {"Data": {"Operation": {"status": "CREATED"}}})["state"], "pending")
        self.assertEqual(tochka.payment_state(
            {"Data": {"Operation": {"status": "EXPIRED"}}})["state"], "dead")
        self.assertEqual(tochka.payment_state(None)["state"], "pending",
                         "непонятный ответ - это не отказ, спросим ещё раз")

    def test_card_token_read_only_when_bank_gave_it(self):
        got = tochka.payment_state({"Data": {"Operation": {
            "status": "APPROVED",
            "Card": {"token": "tk", "pan": "555555******4477", "expDate": "12/28"}}}})
        self.assertEqual(got["card"], {"token": "tk", "mask": "4477",
                                       "expires": "12/28"})
        bare = tochka.payment_state({"Data": {"Operation": {"status": "APPROVED"}}})
        self.assertEqual(bare["card"]["token"], "",
                         "нет токена - автосписания не будет, и выдумывать нечего")


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestAcquiringSwitch(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_switch_lives_in_settings_and_shows_on_the_page(self):
        page = self.get_ok("/payments")
        self.assertIn("Эквайринг Точки", page)
        self.assertIn("не настроен", page, "в тестах токена нет")
        r = self.client.post("/payments/acquiring", data={"action": "off"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(tw.run(self.crm.settings())["acquiring_enabled"], "0")
        self.assertFalse(logic.acquiring_enabled(tw.run(self.crm.settings())))
        self.client.post("/payments/acquiring", data={"action": "on"})
        self.assertTrue(logic.acquiring_enabled(tw.run(self.crm.settings())))
        self.assertTrue(logic.acquiring_enabled({}), "не задано - включён")

    def test_check_without_a_token_says_so(self):
        self.client.post("/payments/acquiring", data={"action": "check"})
        self.assertIn("не настроен", self.get_ok("/payments"))

    def test_switch_is_money_only(self):
        profile = tw.run(self.crm.access_profile_by_code("tech"))
        tw.run(self.crm.create_staff("petr", logic.hash_password("password-1"),
                                     "Пётр", "manager", profile["id"]))
        self.client.post("/logout")
        self.login("petr", "password-1")
        self.assertEqual(self.client.post("/payments/acquiring",
                                          data={"action": "off"}).status_code, 403)


class TestPayFlow(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def rent(self):
        return _run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id,
            tariff_id=self.tariff_id, tariff_name="Неделя", period_days=7,
            price=D(3000), billing="weekly", started_on=date(2026, 9, 1),
            contract_no="АВ-1", created_by="тест"))

    def order(self, amount=None, acquiring=None):
        amount = D(3000) if amount is None else amount
        client = _run(self.crm.client(self.client_id))
        return _run(service.create_pay_order(
            self.crm, client=client, rental=None, amount=amount,
            by="оператор", acquiring=acquiring or FakeAcquiring()))

    def test_invoice_does_not_touch_the_balance(self):
        before = _run(self.crm.client_balance(self.client_id))
        order = self.order()
        self.assertEqual(order["status"], "sent")
        self.assertEqual(order["no"], "СЧТ-000001")
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), before,
                         "счёт - намерение: денег ещё нет")
        self.assertEqual(_run(self.crm.ledger_of(self.client_id)), [])

    def test_bank_silence_leaves_a_readable_refusal(self):
        order = self.order(acquiring=FakeAcquiring(fail="эквайринг отключён"))
        self.assertEqual(order["status"], "failed")
        self.assertIn("эквайринг отключён", order["error"])

    def test_without_acquiring_the_invoice_still_exists(self):
        client = _run(self.crm.client(self.client_id))
        order = _run(service.create_pay_order(
            self.crm, client=client, rental=None, amount=D(1000),
            by="оператор", acquiring=None))
        self.assertEqual(order["status"], "failed")
        self.assertIn("Эквайринг не настроен", order["error"])

    def test_paid_lands_in_the_ledger_once(self):
        acq = FakeAcquiring(answers=[{"state": "paid", "status": "APPROVED",
                                      "card": {"token": "tk", "mask": "4477",
                                               "expires": "12/28"}},
                                     {"state": "paid", "status": "APPROVED",
                                      "card": {}}])
        order = self.order(acquiring=acq)
        fresh = _run(self.crm.pay_order(order["id"]))
        self.assertEqual(_run(service.check_pay_order(self.crm, fresh,
                                                      acquiring=acq)), "paid")
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(3000))
        # Второй ответ банка про тот же счёт не должен удвоить платёж.
        again = _run(self.crm.pay_order(order["id"]))
        _run(service.check_pay_order(self.crm, again, acquiring=acq))
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(3000))
        self.assertEqual(len(_run(self.crm.ledger_of(self.client_id))), 1)

    def test_card_is_remembered_from_the_paid_link(self):
        acq = FakeAcquiring(answers=[{"state": "paid", "status": "APPROVED",
                                      "card": {"token": "tk", "mask": "4477",
                                               "expires": "12/28"}}])
        order = self.order(acquiring=acq)
        _run(service.check_pay_order(self.crm,
                                     _run(self.crm.pay_order(order["id"])),
                                     acquiring=acq))
        card = _run(self.crm.card_of(self.client_id))
        self.assertEqual(card["token"], "tk")
        self.assertEqual(card["mask"], "4477")

    def test_dead_status_closes_the_invoice(self):
        acq = FakeAcquiring(answers=[{"state": "dead", "status": "EXPIRED"}])
        order = self.order(acquiring=acq)
        state = _run(service.check_pay_order(self.crm,
                                             _run(self.crm.pay_order(order["id"])),
                                             acquiring=acq))
        self.assertEqual(state, "failed")
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(0))

    def test_cash_closes_the_invoice_by_hand(self):
        order = self.order()
        _run(service.credit_pay_order(self.crm, _run(self.crm.pay_order(order["id"])),
                                      by="оператор", method="cash"))
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(3000))
        entry = _run(self.crm.ledger_of(self.client_id))[0]
        self.assertEqual(entry["method"], "cash")
        self.assertIn("СЧТ-000001", entry["note"])

    def test_cancelled_invoice_cannot_be_paid(self):
        order = self.order()
        _run(service.cancel_pay_order(self.crm, _run(self.crm.pay_order(order["id"])),
                                      by="оператор"))
        with self.assertRaises(service.ServiceError):
            _run(service.credit_pay_order(
                self.crm, _run(self.crm.pay_order(order["id"])), by="оператор"))

    def test_zero_amount_is_refused(self):
        client = _run(self.crm.client(self.client_id))
        with self.assertRaises(service.ServiceError):
            _run(service.create_pay_order(self.crm, client=client, rental=None,
                                          amount=D(0), by="оператор",
                                          acquiring=FakeAcquiring()))

    def test_poll_closes_stale_links(self):
        acq = FakeAcquiring()
        order = self.order(acquiring=acq)
        self.crm.pay_orders_[order["id"]]["created_at"] = (
            datetime.now(UTC) - timedelta(hours=30))
        result = _run(paying.poll_once(self.crm, acq))
        self.assertEqual(result["expired"], 1)
        self.assertEqual(_run(self.crm.pay_order(order["id"]))["status"], "failed")

    def test_autocharge_is_off_until_switched_on(self):
        acq = FakeAcquiring(charge={"state": "paid", "status": "APPROVED"})
        got = _run(service.autocharge_once(self.crm, acquiring=acq))
        self.assertEqual(got["charged"], 0)
        self.assertEqual(acq.calls, [], "выключенное автосписание в банк не ходит")

    def test_autocharge_pays_the_debt_and_no_more(self):
        _run(self.crm.set_setting("autocharge", "1", by="тест"))
        _run(self.crm.save_card_token(client_id=self.client_id, token="tk",
                                      mask="4477"))
        rental_id = self.rent()
        _run(self.crm.add_ledger(client_id=self.client_id, rental_id=rental_id,
                                 kind="charge", amount=D(-3000)))
        acq = FakeAcquiring(charge={"state": "paid", "status": "APPROVED"})
        got = _run(service.autocharge_once(self.crm, acquiring=acq,
                                           today=date(2026, 9, 8)))
        self.assertEqual(got["charged"], 1)
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(0),
                         "списали ровно долг, не больше")
        charged = [c for c in acq.calls if c[0] == "charge"]
        self.assertEqual(charged[0][2], D(3000))

    def test_autocharge_refusal_is_written_down_verbatim(self):
        _run(self.crm.set_setting("autocharge", "1", by="тест"))
        _run(self.crm.save_card_token(client_id=self.client_id, token="tk"))
        rental_id = self.rent()
        _run(self.crm.add_ledger(client_id=self.client_id, rental_id=rental_id,
                                 kind="charge", amount=D(-3000)))
        acq = FakeAcquiring(charge=None)      # банк откажет
        got = _run(service.autocharge_once(self.crm, acquiring=acq,
                                           today=date(2026, 9, 8)))
        self.assertEqual(got["failed"], 1)
        order = _run(self.crm.pay_orders(client_id=self.client_id))[0]
        self.assertEqual(order["kind"], "auto")
        self.assertIn("рекуррентные", order["error"])
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(-3000))


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestPayPages(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def test_page_opens_and_warns_without_acquiring(self):
        text = self.get_ok("/payments")
        self.assertIn("Счета на оплату", text)
        self.assertIn("Эквайринг Точки не настроен", text)

    def test_invoice_from_the_panel_appears_in_the_list(self):
        r = self.client.post("/payments", data={"client_id": str(self.client_id),
                                                "amount": "3500"})
        self.assertEqual(r.status_code, 303)
        rows = _run(self.crm.pay_orders())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], D(3500))
        self.assertIn("СЧТ-000001", self.get_ok("/payments"))

    def test_invoice_without_a_client_is_refused(self):
        self.client.post("/payments", data={"amount": "3500"})
        self.assertEqual(_run(self.crm.pay_orders()), [])

    def test_settings_keep_at_least_one_method(self):
        self.client.post("/payments/settings", data={"autocharge_hour": "12"})
        self.assertEqual((_run(self.crm.settings())).get("pay_methods"), None,
                         "пустой набор способов не сохраняется")
        self.client.post("/payments/settings",
                         data={"methods": ["cash"], "autocharge_hour": "9",
                               "autocharge": "on"})
        settings = _run(self.crm.settings())
        self.assertEqual(settings["pay_methods"], "cash")
        self.assertEqual(settings["autocharge"], "1")
        self.assertEqual(settings["autocharge_hour"], "9")

    def test_settings_path_is_not_eaten_by_the_id_route(self):
        r = self.client.post("/payments/settings",
                             data={"methods": ["cash"], "autocharge_hour": "12"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/payments")

    def test_card_can_be_dropped_from_the_invoice_page(self):
        _run(self.crm.save_card_token(client_id=self.client_id, token="tk",
                                      mask="4477"))
        order_id = _run(self.crm.create_pay_order(
            client_id=self.client_id, rental_id=None, amount=D(1000),
            purpose="Аренда велосипеда"))
        text = self.get_ok(f"/payments/{order_id}")
        self.assertIn("•••• 4477", text)
        self.client.post(f"/payments/{order_id}", data={"action": "drop_card"})
        self.assertIsNone(_run(self.crm.card_of(self.client_id)))

    def test_client_card_shows_invoices_and_the_debt_hint(self):
        _run(self.crm.add_ledger(client_id=self.client_id, kind="charge",
                                 amount=D(-2500)))
        text = self.get_ok(f"/clients/{self.client_id}")
        self.assertIn("Выставить счёт", text)
        self.assertIn('placeholder="2500', text, "подсказка суммы - это долг")


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
