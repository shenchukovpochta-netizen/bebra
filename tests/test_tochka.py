"""Клиент Точка-банка (СБП) и логика оплат. Сеть подменяется целиком."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.fleet import logic as fleet
from app.fleet.tochka import Tochka, TochkaError


def run(coro):
    return asyncio.run(coro)


class FakeTochka(Tochka):
    def __init__(self):
        super().__init__("TOKEN", "MERCH1", "40802810/044525104")
        self.calls: list = []
        self.status_payload: dict = {}

    async def _get(self, url):
        self.calls.append(("GET", url))
        return self.status_payload

    async def _post(self, url, *, json):
        self.calls.append(("POST", url, json))
        return {"Data": {"qrcId": "QR123", "payload": "https://qr.nspk.ru/QR123"}}


class TestCreateQr(unittest.TestCase):
    def test_amount_goes_in_kopecks(self):
        t = FakeTochka()
        qr = run(t.create_qr(3000, "Аренда, договор АВ-1"))
        self.assertEqual(qr, {"qrc_id": "QR123",
                              "payload": "https://qr.nspk.ru/QR123"})
        _, url, body = t.calls[-1]
        self.assertIn("/qr-code/merchant/MERCH1/40802810/044525104", url)
        self.assertEqual(body["Data"]["amount"], 300000)
        self.assertEqual(body["Data"]["qrcType"], "02")

    def test_purpose_is_trimmed(self):
        t = FakeTochka()
        run(t.create_qr(100, "х" * 500))
        self.assertLessEqual(len(t.calls[-1][2]["Data"]["paymentPurpose"]), 140)

    def test_broken_answer_raises(self):
        t = FakeTochka()

        async def bad_post(url, *, json):
            return {"Data": {}}
        t._post = bad_post
        with self.assertRaises(TochkaError):
            run(t.create_qr(100, "x"))


class TestStatus(unittest.TestCase):
    def check(self, payment_list):
        t = FakeTochka()
        t.status_payload = {"Data": {"paymentList": payment_list}}
        return run(t.payment_status("QR123"))

    def test_paid_codes(self):
        for code in ("ACWP", "ACSC", "Accepted", "Confirmed"):
            self.assertEqual(self.check([{"status": code}]), "paid", code)

    def test_rejected(self):
        self.assertEqual(self.check([{"status": "RJCT"}]), "rejected")

    def test_unknown_and_empty_are_pending(self):
        # Неизвестный статус - НЕ оплата: деньги не подтверждены.
        self.assertEqual(self.check([{"status": "InProgress"}]), "pending")
        self.assertEqual(self.check([]), "pending")

    def test_network_error_is_pending_not_crash(self):
        t = FakeTochka()

        async def boom(url):
            raise TochkaError("HTTP 500")
        t._get = boom
        self.assertEqual(run(t.payment_status("QR123")), "pending")

    def test_from_config_disabled(self):
        class Cfg:
            tochka_enabled = False
        self.assertIsNone(Tochka.from_config(Cfg()))


class TestBattery(unittest.TestCase):
    def test_bounds(self):
        # Границы задал владелец: 54.2 В - 0%, 67.2 В - 100%.
        self.assertEqual(fleet.battery_percent(54.2), 0)
        self.assertEqual(fleet.battery_percent(67.2), 100)

    def test_midpoint_and_rounding(self):
        self.assertEqual(fleet.battery_percent(60.7), 50)
        self.assertEqual(fleet.battery_percent(62.1), 61)

    def test_clamped_inside_sane_range(self):
        self.assertEqual(fleet.battery_percent(53.0), 0)      # чуть ниже нуля
        self.assertEqual(fleet.battery_percent(68.0), 100)    # чуть выше сотни

    def test_garbage_is_none(self):
        # Бортовые 12 В, мусор и отсутствие данных - не заряд.
        for value in (12.4, 0, None, "нет", 500):
            self.assertIsNone(fleet.battery_percent(value), value)


class TestPriceAmount(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(fleet.price_amount("3000qr"), 3000)
        self.assertEqual(fleet.price_amount("3 000 нал"), 3000)
        self.assertEqual(fleet.price_amount("оплата 650"), 650)

    def test_no_digits_or_absurd(self):
        self.assertIsNone(fleet.price_amount("качели"))
        self.assertIsNone(fleet.price_amount(None))
        self.assertIsNone(fleet.price_amount("99999999999"))


if __name__ == "__main__":
    unittest.main()
