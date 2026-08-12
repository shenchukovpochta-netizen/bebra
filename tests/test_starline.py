"""Клиент StarLine: подпись авторизации и цепочка команд.

Сеть подменяется (_get_json/_post переопределены), поэтому вся
последовательность getCode -> getToken -> login -> auth.slid -> set_param
проверяется без единого запроса наружу.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.fleet.starline import StarLine, extract_voltage, md5_hex


def run(coro):
    return asyncio.run(coro)


class FakeStarLine(StarLine):
    def __init__(self, **kw):
        super().__init__("APP", "SECRET", "user@example.com", "pass", **kw)
        self.calls: list = []
        self.setparam_calls = 0
        self.fail_first_setparam = False
        self.login_state = 1

    async def _get_json(self, url, params):
        self.calls.append(("GET", url, params))
        if "getCode" in url:
            return {"state": 1, "desc": {"code": "CODE42"}}
        if "getToken" in url:
            return {"state": 1, "desc": {"token": "APPTOKEN"}}
        return {}

    async def _post(self, url, *, json=None, data=None, params=None, cookies=None):
        self.calls.append(("POST", url, {"json": json, "data": data,
                                         "params": params, "cookies": cookies}))
        if "user/login" in url:
            # StarLine отдаёт user_token только при успехе; на отказе - без него.
            if self.login_state == 1:
                return {"state": 1, "user_token": "SLIDTOKEN"}
            return {"state": 0, "desc": {"error": "bad password"}}
        if "auth.slid" in url:
            return {"state": 1, "desc": {"user_id": "77"}, "slnet": "SLNET1"}
        if "set_param" in url:
            self.setparam_calls += 1
            if self.fail_first_setparam and self.setparam_calls == 1:
                return {"code": 401}          # протухший токен
            return {"code": 200}
        return {}


class TestSigning(unittest.TestCase):
    def test_md5_is_deterministic(self):
        self.assertEqual(md5_hex("SECRET"),
                         "44c7be48226ebad5dca8216674cad62b")

    def test_code_and_token_secrets_differ(self):
        # getToken подписывается secret+code, getCode - только secret:
        # перепутать их - значит не пройти авторизацию.
        self.assertNotEqual(md5_hex("SECRET"), md5_hex("SECRET" + "CODE42"))


class TestControl(unittest.TestCase):
    def test_block_authenticates_then_sets_param(self):
        sl = FakeStarLine()
        self.assertTrue(run(sl.block("DEV1")))
        urls = [c[1] for c in sl.calls]
        self.assertTrue(any("getCode" in u for u in urls))
        self.assertTrue(any("getToken" in u for u in urls))
        self.assertTrue(any("user/login" in u for u in urls))
        self.assertTrue(any("auth.slid" in u for u in urls))
        # последний вызов - set_param с arm=1 и cookie slnet
        method, url, kw = sl.calls[-1]
        self.assertIn("device/DEV1/set_param", url)
        self.assertEqual(kw["json"], {"type": "arm", "arm": 1})
        self.assertEqual(kw["cookies"], {"slnet": "SLNET1"})

    def test_unblock_sends_zero(self):
        sl = FakeStarLine()
        self.assertTrue(run(sl.unblock("DEV1")))
        self.assertEqual(sl.calls[-1][2]["json"], {"type": "arm", "arm": 0})

    def test_custom_block_param(self):
        sl = FakeStarLine(block_param="ign")
        run(sl.block("DEV1"))
        self.assertEqual(sl.calls[-1][2]["json"], {"type": "ign", "ign": 1})

    def test_password_is_hashed_not_sent_plain(self):
        sl = FakeStarLine()
        run(sl.block("DEV1"))
        login = next(c for c in sl.calls if "user/login" in c[1])
        self.assertEqual(login[2]["data"]["pass"], md5_hex("pass"))
        self.assertNotEqual(login[2]["data"]["pass"], "pass")

    def test_reauth_on_stale_token(self):
        # Первый set_param вернул 401 -> клиент переавторизуется и повторяет.
        # App-токен кэширован, поэтому повторно дёргается auth.slid, а не
        # getCode: пере-логин без лишнего получения кода приложения.
        sl = FakeStarLine()
        sl.fail_first_setparam = True
        self.assertTrue(run(sl.block("DEV1")))
        self.assertEqual(sl.setparam_calls, 2)
        self.assertEqual(sum("auth.slid" in c[1] for c in sl.calls), 2)

    def test_app_token_cached_between_commands(self):
        sl = FakeStarLine()
        run(sl.block("DEV1"))
        run(sl.block("DEV2"))
        # slnet кэширован после первой команды -> вторая не переавторизуется
        self.assertEqual(sum("getCode" in c[1] for c in sl.calls), 1)

    def test_login_failure_returns_false_not_raises(self):
        sl = FakeStarLine()
        sl.login_state = 0            # StarLine отверг логин
        self.assertFalse(run(sl.block("DEV1")))

    def test_from_config_disabled_returns_none(self):
        class Cfg:
            starline_enabled = False
        self.assertIsNone(StarLine.from_config(Cfg()))


class TestVoltage(unittest.TestCase):
    def test_finds_traction_battery_in_nested_data(self):
        payload = {"data": {"common": {"battery": 12.4},   # бортовые 12 В - мимо
                            "obd": {"voltage": 62.1}}}
        self.assertEqual(extract_voltage(payload), 62.1)

    def test_only_car_battery_is_none(self):
        # Одни бортовые 12 В: тяговой батареи в телеметрии нет - None,
        # а не 0% из чужого датчика.
        self.assertIsNone(extract_voltage({"data": {"battery": 12.4}}))

    def test_garbage_is_none(self):
        for payload in ({}, None, {"data": {"battery": "err"}}, [1, 2]):
            self.assertIsNone(extract_voltage(payload), payload)

    def test_voltage_cached(self):
        sl = FakeStarLine()

        async def fake_get(url, params, cookies=None):
            sl.calls.append(("GET", url, params))
            return {"data": {"voltage": 60.7}}
        sl._get_json = fake_get
        sl._slnet = "SLNET1"
        self.assertEqual(run(sl.voltage("DEV1")), 60.7)
        self.assertEqual(run(sl.voltage("DEV1")), 60.7)
        data_calls = [c for c in sl.calls if "device/DEV1/data" in c[1]]
        self.assertEqual(len(data_calls), 1)   # второй ответ - из кэша


if __name__ == "__main__":
    unittest.main()
