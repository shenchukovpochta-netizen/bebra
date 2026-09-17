"""Касса и банк: смена, пересчёт, выписка Точки и зачисление.

Наличные на точке и журнал клиента - разные вопросы: «сколько в ящике»
и «сколько должен клиент». Здесь проверяется, что они сходятся там, где
должны, и не смешиваются там, где не должны. Сети в тестах нет: клиент
банка получает заглушку сессии.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402
from app.services import tochka  # noqa: E402

try:
    import test_web as tw

    from app.crm import banking, service
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class TestCashLogic(unittest.TestCase):
    def test_expected_is_opening_plus_cash_plus_moves(self):
        shift = {"opening": D(2000), "status": "open"}
        payments = [{"amount": D(3000)}, {"amount": D(1500)},
                    {"amount": D(-500)}]      # возврат наличными - минусом
        moves = [{"kind": "in", "amount": D(1000)}, {"kind": "out", "amount": D(4000)}]
        self.assertEqual(logic.shift_expected(shift, payments, moves), D(3000))

    def test_state_counts_the_difference_and_its_size(self):
        shift = {"opening": D(1000), "status": "open", "counted": D(3900)}
        payments = [{"amount": D(3000)}]
        state = logic.shift_state(shift, payments, [])
        self.assertEqual(state["expected"], D(4000))
        self.assertEqual(state["diff"], D(-100))
        self.assertFalse(state["big_diff"], "сотня - это сдача, а не пропажа")
        big = logic.shift_state({**shift, "counted": D(3500)}, payments, [])
        self.assertTrue(big["big_diff"])
        opened = logic.shift_state({"opening": D(0), "status": "open"}, [], [])
        self.assertIsNone(opened["diff"], "пока не считали - расхождения нет")
        self.assertTrue(opened["open"])

    def test_shift_number_and_order(self):
        self.assertEqual(logic.shift_no(7), "КСМ-000007")
        rows = logic.shift_rows([
            {"id": 1, "status": "closed", "opened_at": datetime(2026, 9, 1, tzinfo=UTC),
             "diff": D(-500)},
            {"id": 2, "status": "closed", "opened_at": datetime(2026, 9, 5, tzinfo=UTC),
             "diff": D(-100)},
            {"id": 3, "status": "open", "opened_at": datetime(2026, 8, 1, tzinfo=UTC),
             "diff": None}])
        self.assertEqual([r["id"] for r in rows], [3, 2, 1],
                         "открытая первой, дальше свежие")
        self.assertTrue(rows[2]["big_diff"])
        self.assertFalse(rows[1]["big_diff"])


class TestBankLogic(unittest.TestCase):
    def clients(self):
        return [{"id": 1, "full_name": "Иванов Иван Иванович", "phone": "+79990000001",
                 "contract_no": "АВ-2026-000042", "status": "active"},
                {"id": 2, "full_name": "Петров Пётр", "phone": "+79990000002",
                 "contract_no": None, "status": "active"}]

    def txn(self, **over):
        row = {"id": 10, "txn_id": "T-1", "direction": "credit", "amount": D(3000),
               "status": "new", "payer_name": "ПЕТРОВ ПЕТР",
               "purpose": "Оплата аренды", "booked_at": datetime(2026, 9, 16, tzinfo=UTC)}
        row.update(over)
        return row

    def test_contract_number_wins(self):
        got = logic.match_payment(
            self.txn(purpose="Оплата по договору АВ-2026-000042"), self.clients())
        self.assertEqual(got["client"]["id"], 1)
        self.assertEqual(got["reason"], "contract")

    def test_phone_then_name(self):
        by_phone = logic.match_payment(
            self.txn(purpose="аренда велосипеда, тел +7 999 000-00-02"), self.clients())
        self.assertEqual(by_phone["client"]["id"], 2)
        self.assertEqual(by_phone["reason"], "phone")
        by_name = logic.match_payment(self.txn(payer_name="петров пётр"), self.clients())
        self.assertEqual(by_name["client"]["id"], 2)
        self.assertEqual(by_name["reason"], "name")
        self.assertIsNone(logic.match_payment(
            self.txn(payer_name="Сидоров", purpose="возврат"), self.clients()))
        self.assertIsNone(logic.match_payment(self.txn(direction="debit"),
                                              self.clients()),
                          "списание никому не зачисляется")

    def test_rows_mark_only_the_contract_guess_as_sure(self):
        rows = logic.bank_rows(
            [self.txn(purpose="договор АВ-2026-000042"),
             self.txn(id=11, txn_id="T-2", payer_name="петров пётр"),
             self.txn(id=12, txn_id="T-3", status="matched", amount=D(5000))],
            self.clients())
        by_id = {r["id"]: r for r in rows}
        self.assertTrue(by_id[10]["sure"])
        self.assertFalse(by_id[11]["sure"])
        self.assertEqual(by_id[11]["guess_reason"], "ФИО плательщика")
        self.assertIsNone(by_id[12]["guess"], "разобранную строку не гадаем")
        summary = logic.bank_summary(rows + [self.txn(id=13, txn_id="T-4",
                                                       direction="debit",
                                                       amount=D(900))])
        self.assertEqual(summary["new"], 2, "списание разбирать нечего")
        self.assertEqual(summary["new_amount"], D(6000))
        self.assertEqual(summary["credited"], D(5000))

    def test_auto_credit_is_off_until_switched_on(self):
        self.assertFalse(logic.bank_settings({})["auto_credit"])
        self.assertTrue(logic.bank_settings({"bank_auto_credit": "1"})["auto_credit"])


class TestTochkaParsing(unittest.TestCase):
    def test_transaction_is_flattened(self):
        got = tochka.parse_transaction({
            "transactionId": "T-1", "transactionAmount": "3000.00",
            "creditDebitIndicator": "Credit", "documentDate": "2026-09-16T10:00:00Z",
            "paymentPurpose": "Оплата по договору АВ-2026-000042",
            "sidePayer": {"name": "ИВАНОВ ИВАН", "inn": "166012345678"}},
            account="40802810/044525104")
        self.assertEqual(got["txn_id"], "T-1")
        self.assertEqual(got["amount"], D("3000.00"))
        self.assertEqual(got["direction"], "credit")
        self.assertEqual(got["payer_name"], "ИВАНОВ ИВАН")
        self.assertEqual(got["booked_at"],
                         datetime(2026, 9, 16, 10, 0, tzinfo=UTC))
        self.assertEqual(got["account"], "40802810/044525104")

    def test_debit_takes_the_other_side_and_broken_rows_are_dropped(self):
        got = tochka.parse_transaction({
            "transactionId": "T-2", "amount": "-1200", "creditDebitIndicator": "Debit",
            "documentDate": "2026-09-16", "sideRecipient": {"name": "ООО Запчасти"}})
        self.assertEqual(got["direction"], "debit")
        self.assertEqual(got["amount"], D("1200.00"), "сумма всегда положительная")
        self.assertEqual(got["payer_name"], "ООО Запчасти")
        self.assertIsNone(tochka.parse_transaction({"amount": "100"}))
        self.assertIsNone(tochka.parse_transaction({"transactionId": "T-3"}))

    def test_statement_is_ordered_and_read(self):
        calls = []

        class Response:
            def __init__(self, data, status=200):
                self._data, self.status = data, status

            async def json(self, content_type=None):
                return self._data

        class Session:
            async def request(self, method, url, **kwargs):
                calls.append((method, url))
                if method == "POST":
                    return Response({"Data": {"Statement": {"statementId": "S-1"}}})
                return Response({"Data": {"Statement": {
                    "status": "Ready",
                    "Transaction": [
                        {"transactionId": "T-1", "transactionAmount": "3000",
                         "creditDebitIndicator": "Credit",
                         "documentDate": "2026-09-16T10:00:00Z",
                         "paymentPurpose": "аренда"},
                        {"amount": "нечто"}]}}})

            async def close(self):
                pass

        client = tochka.TochkaClient(token="tok", account_id="ACC",
                                     session_factory=Session)
        self.assertTrue(client.ready)
        got = _run(client.statement(since=date(2026, 9, 14), until=date(2026, 9, 16)))
        self.assertTrue(got["ready"])
        self.assertEqual(got["statement_id"], "S-1")
        self.assertEqual([t["txn_id"] for t in got["rows"]], ["T-1"],
                         "битая строка выбрасывается, а не роняет разбор")
        self.assertEqual([m for m, _ in calls], ["POST", "GET"])

    def test_statement_not_ready_yet(self):
        class Response:
            def __init__(self, data):
                self._data, self.status = data, 200

            async def json(self, content_type=None):
                return self._data

        class Session:
            async def request(self, method, url, **kwargs):
                if method == "POST":
                    return Response({"Data": {"Statement": {"statementId": "S-1"}}})
                return Response({"Data": {"Statement": {"status": "Processing"}}})

            async def close(self):
                pass

        client = tochka.TochkaClient(token="tok", account_id="ACC",
                                     session_factory=Session)
        got = _run(client.statement(since=date(2026, 9, 14), until=date(2026, 9, 16)))
        self.assertFalse(got["ready"])
        self.assertEqual(got["rows"], [])
        self.assertEqual(got["statement_id"], "S-1", "номер вернулся для следующего круга")

    def test_bank_error_is_readable(self):
        class Response:
            def __init__(self):
                self.status = 403

            async def json(self, content_type=None):
                return {"Errors": [{"message": "нет доступа к счёту"}]}

        class Session:
            async def request(self, method, url, **kwargs):
                return Response()

            async def close(self):
                pass

        client = tochka.TochkaClient(token="tok", account_id="ACC",
                                     session_factory=Session)
        with self.assertRaises(tochka.TochkaError) as err:
            _run(client.statement(since=date(2026, 9, 1), until=date(2026, 9, 2)))
        self.assertIn("нет доступа к счёту", str(err.exception))

    def test_client_without_account_does_nothing(self):
        client = tochka.TochkaClient(token="", account_id="")
        self.assertFalse(client.ready)
        self.assertEqual(_run(client.statement(since=date(2026, 9, 1),
                                               until=date(2026, 9, 2)))["rows"], [])

    def test_receipt_has_one_line_without_vat(self):
        items = tochka.receipt_items("Аренда велосипеда, неделя", D(3000))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["vatType"], "none")
        self.assertEqual(items[0]["amount"], 3000.0)
        self.assertEqual(items[0]["paymentObject"], "service")


class TestReceiptLink(unittest.TestCase):
    """Ссылка на оплату с чеком 54-ФЗ: есть эквайринг - есть чек,
    нет или банк молчит - остаётся обычная ссылка СБП."""

    class Cfg:
        pay_url = "https://pay.example/sbp"
        tochka_token = "tok"
        tochka_customer_code = "CUST"
        tochka_account_id = "ACC"

    def link(self, session_factory, **over):
        from app.handlers import cabinet
        cfg = self.Cfg()
        for key, value in over.items():
            setattr(cfg, key, value)
        original = tochka.TochkaClient

        def factory(**kwargs):
            return original(**kwargs, session_factory=session_factory)

        tochka.TochkaClient = factory
        try:
            return _run(cabinet.pay_link(
                cfg, {"phone": "+79990000000", "contract_no": "АВ-2026-000042"},
                D(3000)))
        finally:
            tochka.TochkaClient = original

    def test_link_with_receipt_is_used_when_acquiring_is_set_up(self):
        seen = {}

        class Response:
            status = 200

            async def json(self, content_type=None):
                return {"Data": {"paymentLink": "https://pay.tochka/abc",
                                 "operationId": "OP-1"}}

        class Session:
            async def request(self, method, url, **kwargs):
                seen["json"] = kwargs.get("json")
                return Response()

            async def close(self):
                pass

        self.assertEqual(self.link(Session), "https://pay.tochka/abc")
        data = seen["json"]["Data"]
        self.assertEqual(data["Client"]["phone"], "+79990000000")
        self.assertIn("АВ-2026-000042", data["purpose"])
        self.assertEqual(len(data["Items"]), 1)

    def test_bank_silence_falls_back_to_the_plain_link(self):
        class Session:
            async def request(self, method, url, **kwargs):
                raise OSError("сеть недоступна")

            async def close(self):
                pass

        self.assertEqual(self.link(Session), "https://pay.example/sbp")

    def test_without_acquiring_no_request_is_made(self):
        class Session:
            async def request(self, method, url, **kwargs):
                raise AssertionError("в банк ходить не должны")

            async def close(self):
                pass

        self.assertEqual(self.link(Session, tochka_customer_code=""),
                         "https://pay.example/sbp")


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestCashPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def open_shift(self, opening="2000"):
        r = self.client.post("/cash", data={"location": "Павлюхина",
                                            "opening": opening, "note": ""})
        return int(r.headers["location"].rsplit("/", 1)[1])

    def test_shift_collects_cash_payments_and_closes_with_a_difference(self):
        shift_id = self.open_shift()
        self.assertIn("Открыта смена", self.get_ok("/cash"))
        client = tw.run(self.crm.client(self.client_id))
        tw.run(service.add_entry(self.crm, client, kind="payment", amount=D(3000),
                                 method="cash", note="аренда", by="t"))
        tw.run(service.add_entry(self.crm, client, kind="payment", amount=D(5000),
                                 method="sbp", note="перевод", by="t"))
        card = self.get_ok(f"/cash/{shift_id}")
        self.assertIn("3 000 ₽", card)
        self.assertNotIn("перевод", card, "СБП в ящик не попадает")

        self.client.post(f"/cash/{shift_id}/move",
                         data={"kind": "out", "amount": "1000", "reason": "инкассация"})
        r = self.client.post(f"/cash/{shift_id}/close",
                             data={"counted": "3500", "note": "не хватает пятисот"})
        self.assertEqual(r.headers["location"], f"/cash/{shift_id}")
        shift = tw.run(self.crm.cash_shift(shift_id))
        self.assertEqual(shift["expected"], D(4000))
        self.assertEqual(shift["counted"], D(3500))
        self.assertEqual(shift["diff"], D(-500))
        self.assertEqual(shift["status"], "closed")
        self.assertIn("недостача", self.get_ok("/cash"))

    def test_second_shift_on_the_same_point_is_refused(self):
        self.open_shift()
        r = self.client.post("/cash", data={"location": "Павлюхина", "opening": "0"})
        self.assertEqual(r.headers["location"], "/cash")
        self.assertIn("уже открыта смена", self.get_ok("/cash"))
        # на другой точке - можно
        r = self.client.post("/cash", data={"location": "Адоратского", "opening": "0"})
        self.assertTrue(r.headers["location"].startswith("/cash/"))

    def test_cannot_take_out_more_than_there_is(self):
        shift_id = self.open_shift(opening="500")
        self.client.post(f"/cash/{shift_id}/move",
                         data={"kind": "out", "amount": "900", "reason": "зарплата"})
        self.assertIn("изъять больше нечего", self.get_ok(f"/cash/{shift_id}"))
        self.assertEqual(tw.run(self.crm.cash_moves(shift_id)), [])

    def test_closed_shift_takes_no_moves(self):
        shift_id = self.open_shift()
        self.client.post(f"/cash/{shift_id}/close", data={"counted": "2000"})
        self.client.post(f"/cash/{shift_id}/move",
                         data={"kind": "in", "amount": "100", "reason": "размен"})
        self.assertIn("Смена закрыта", self.get_ok(f"/cash/{shift_id}"))
        self.assertEqual(tw.run(self.crm.cash_moves(shift_id)), [])

    def test_missing_shift_is_a_404(self):
        self.assertEqual(self.client.get("/cash/999").status_code, 404)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestBankPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        tw.run(self.crm.update_client(self.client_id, contract_no="АВ-2026-000042"))
        self.txn_id = tw.run(self.crm.save_bank_txn({
            "txn_id": "T-1", "account": "ACC", "booked_at": datetime.now(UTC),
            "amount": D(3000), "direction": "credit", "payer_name": "ИВАНОВ ИВАН",
            "payer_inn": None, "purpose": "Оплата по договору АВ-2026-000042"}))

    def test_statement_row_is_credited_to_the_client(self):
        page = self.get_ok("/bank")
        self.assertIn("АВ-2026-000042", page)
        self.assertIn("номер договора в назначении", page)
        r = self.client.post(f"/bank/{self.txn_id}",
                             data={"client_id": str(self.client_id)})
        self.assertEqual(r.headers["location"], "/bank")
        self.assertEqual(tw.run(self.crm.client_balance(self.client_id)), D(3000))
        txn = tw.run(self.crm.bank_txn(self.txn_id))
        self.assertEqual(txn["status"], "matched")
        self.assertEqual(txn["client_id"], self.client_id)
        self.assertIsNotNone(txn["ledger_id"])
        # Повторно зачислить ту же строку нельзя.
        self.client.post(f"/bank/{self.txn_id}", data={"client_id": str(self.client_id)})
        self.assertIn("уже разобрана", self.get_ok("/bank"))
        self.assertEqual(tw.run(self.crm.client_balance(self.client_id)), D(3000))

    def test_not_our_payment_is_set_aside(self):
        r = self.client.post(f"/bank/{self.txn_id}", data={"action": "ignore"})
        self.assertEqual(r.headers["location"], "/bank")
        self.assertEqual(tw.run(self.crm.bank_txn(self.txn_id))["status"], "ignored")
        self.assertEqual(tw.run(self.crm.client_balance(self.client_id)), D(0))

    def test_credit_needs_a_client(self):
        self.client.post(f"/bank/{self.txn_id}", data={"client_id": ""})
        self.assertIn("Выберите клиента", self.get_ok("/bank"))
        self.assertEqual(tw.run(self.crm.bank_txn(self.txn_id))["status"], "new")

    def test_debit_is_not_credited_at_all(self):
        debit = tw.run(self.crm.save_bank_txn({
            "txn_id": "T-2", "booked_at": datetime.now(UTC), "amount": D(1200),
            "direction": "debit", "purpose": "оплата поставщику"}))
        self.client.post(f"/bank/{debit}", data={"client_id": str(self.client_id)})
        self.assertIn("списание со счёта", self.get_ok("/bank"))

    def test_auto_credit_switch_and_run(self):
        self.assertEqual(tw.run(banking.auto_credit(self.crm)), 0,
                         "по умолчанию выключено")
        r = self.client.post("/bank/settings", data={"auto": "1"})
        self.assertEqual(r.headers["location"], "/bank")
        self.assertEqual(tw.run(banking.auto_credit(self.crm)), 1)
        self.assertEqual(tw.run(self.crm.client_balance(self.client_id)), D(3000))
        # догадка по ФИО автозачислением не пользуется
        tw.run(self.crm.save_bank_txn({
            "txn_id": "T-3", "booked_at": datetime.now(UTC), "amount": D(1000),
            "direction": "credit", "payer_name": "Иванов Иван",
            "purpose": "за велосипед"}))
        self.assertEqual(tw.run(banking.auto_credit(self.crm)), 0)
        self.client.post("/bank/settings", data={})
        self.assertIn("Автозачисление выключено", self.get_ok("/bank"))

    def test_import_is_idempotent(self):
        class Client:
            ready = True

            async def statement(self, *, since, until, statement_id=None):
                return {"ready": True, "statement_id": "S-1", "rows": [
                    {"txn_id": "T-1", "booked_at": datetime.now(UTC),
                     "amount": D(3000), "direction": "credit",
                     "purpose": "повтор той же операции"},
                    {"txn_id": "T-9", "booked_at": datetime.now(UTC),
                     "amount": D(4000), "direction": "credit",
                     "purpose": "новая"}]}

        out = tw.run(banking.import_once(self.crm, Client()))
        self.assertEqual((out["seen"], out["saved"]), (2, 1))
        self.assertEqual(len(tw.run(self.crm.bank_txns(limit=50))), 2)


if __name__ == "__main__":
    unittest.main()
