"""Проверка подписи Telegram Mini App (initData). Только stdlib.

Это граница доверия: всё, что не подписано токеном бота, обязано
превращаться в None - без исключений и без трейсбеков.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.fleet import webauth

TOKEN = "12345:test-token"
NOW = 1_800_000_000


def make(user: dict | None = None, *, auth_date: int = NOW,
         token: str = TOKEN) -> str:
    data = {
        "auth_date": str(auth_date),
        "query_id": "AAA",
        "user": json.dumps(user if user is not None
                           else {"id": 42, "first_name": "Иван",
                                 "username": "ivan"},
                           ensure_ascii=False),
    }
    return webauth.sign_init_data(data, token)


class TestInitData(unittest.TestCase):
    def parse(self, init_data, **kw):
        kw.setdefault("now", NOW)
        return webauth.parse_init_data(init_data, TOKEN, **kw)

    def test_valid_signature_returns_user(self):
        user = self.parse(make())
        self.assertEqual(user["id"], 42)
        self.assertEqual(user["first_name"], "Иван")

    def test_wrong_token_rejected(self):
        self.assertIsNone(self.parse(make(token="12345:another")))

    def test_tampered_user_rejected(self):
        # Подписано одним пользователем, подменено на другого.
        good = make()
        evil = good.replace("42", "43")
        self.assertIsNone(self.parse(evil))

    def test_missing_hash_rejected(self):
        self.assertIsNone(self.parse("auth_date=1&user=%7B%7D"))

    def test_garbage_is_none_not_traceback(self):
        for raw in (None, "", "не querystring", "a=b&c", "hash=deadbeef"):
            self.assertIsNone(self.parse(raw), raw)

    def test_stale_auth_date_rejected(self):
        old = make(auth_date=NOW - webauth.MAX_AGE_SECONDS - 1)
        self.assertIsNone(self.parse(old))
        fresh = make(auth_date=NOW - webauth.MAX_AGE_SECONDS + 60)
        self.assertIsNotNone(self.parse(fresh))

    def test_bad_user_json_rejected(self):
        data = {"auth_date": str(NOW), "user": "не json"}
        self.assertIsNone(self.parse(webauth.sign_init_data(data, TOKEN)))

    def test_user_without_id_rejected(self):
        self.assertIsNone(self.parse(make({"first_name": "Без id"})))

    def test_empty_token_rejected(self):
        self.assertIsNone(webauth.parse_init_data(make(), "", now=NOW))


if __name__ == "__main__":
    unittest.main()
