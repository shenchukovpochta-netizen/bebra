"""Чистые разборы апдейтов MAX: vCard, дедупликация, слепки, клавиатуры.

Сетевые части (client.py) сюда не входят - их гоняет стенд с заглушкой
Bot API MAX, как run_e2e для Telegram-версии.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import logic  # noqa: E402
from app.max import keyboards as kb  # noqa: E402
from app.max import parse  # noqa: E402

VCF = "BEGIN:VCARD\nVERSION:3.0\nFN:Иван\nTEL;TYPE=CELL:+79991571094\nEND:VCARD"


class TestVcf(unittest.TestCase):
    def test_phone_extracted(self):
        self.assertEqual(parse.phone_from_vcf(VCF), "+79991571094")

    def test_no_phone(self):
        self.assertIsNone(parse.phone_from_vcf("BEGIN:VCARD\nFN:Иван\nEND:VCARD"))
        self.assertIsNone(parse.phone_from_vcf(None))

    def test_contact_attachment(self):
        atts = [{"type": "contact",
                 "payload": {"vcf_info": VCF, "max_info": {"user_id": 42}}}]
        phone, owner = parse.contact_from(atts)
        self.assertEqual(phone, "+79991571094")
        self.assertEqual(owner, 42)

    def test_foreign_contact_has_no_matching_owner(self):
        """MAX позволяет переслать чужой контакт - защита та же, что в TG."""
        atts = [{"type": "contact",
                 "payload": {"vcf_info": VCF, "max_info": {"user_id": 999}}}]
        _, owner = parse.contact_from(atts)
        self.assertFalse(logic.contact_belongs_to_sender(owner, 42))


class TestDedup(unittest.TestCase):
    def test_same_update_same_id(self):
        upd = {"update_type": "message_created",
               "message": {"body": {"mid": "mid.001"}}}
        self.assertEqual(parse.dedup_id(upd), parse.dedup_id(dict(upd)))

    def test_different_updates_differ(self):
        a = {"update_type": "message_created",
             "message": {"body": {"mid": "mid.001"}}}
        b = {"update_type": "message_created",
             "message": {"body": {"mid": "mid.002"}}}
        self.assertNotEqual(parse.dedup_id(a), parse.dedup_id(b))

    def test_fits_bigint(self):
        upd = {"update_type": "message_callback",
               "callback": {"callback_id": "x" * 64}}
        self.assertLess(parse.dedup_id(upd), 2 ** 63)

    def test_unknown_type_skipped(self):
        self.assertIsNone(parse.dedup_id({"update_type": "user_added"}))


class TestDescribe(unittest.TestCase):
    def test_message(self):
        info = parse.describe({
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 7, "username": "u7", "is_bot": False},
                "recipient": {"chat_id": 100, "chat_type": "dialog"},
                "body": {"mid": "m1", "text": "привет", "attachments": []},
            }})
        self.assertEqual(info["kind"], "message")
        self.assertEqual(info["user_id"], 7)
        self.assertEqual(info["chat_type"], "dialog")
        self.assertIsNone(info["reply_to_mid"])

    def test_reply_detected(self):
        info = parse.describe({
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 7}, "recipient": {"chat_id": -5,
                                                        "chat_type": "chat"},
                "body": {"mid": "m2", "text": "ответ"},
                "link": {"type": "reply", "message": {"mid": "m1"}},
            }})
        self.assertEqual(info["reply_to_mid"], "m1")

    def test_callback(self):
        info = parse.describe({
            "update_type": "message_callback",
            "callback": {"callback_id": "cb1", "payload": "approve:7",
                         "user": {"user_id": 111}},
            "message": {"recipient": {"chat_id": -5, "chat_type": "chat"},
                        "body": {"mid": "m3"}},
        })
        self.assertEqual(info["kind"], "callback")
        self.assertEqual(info["payload_cb"], "approve:7")
        self.assertEqual(info["user_id"], 111)

    def test_bot_started(self):
        info = parse.describe({"update_type": "bot_started", "chat_id": 100,
                               "user": {"user_id": 7, "username": "u7"}})
        self.assertEqual(info["kind"], "start")
        self.assertEqual(info["chat_type"], "dialog")


class TestKeyboards(unittest.TestCase):
    def test_moderation_payloads_match_logic(self):
        """Кнопки MAX обязаны собирать те же payload, что разбирает logic."""
        rows = kb.moderation(42)
        payloads = [b["payload"] for row in rows for b in row]
        for p in payloads:
            self.assertTrue(logic.is_moderation_data(p), p)

    def test_reject_reasons_payloads_parse(self):
        rows = kb.reject_reasons(42, logic.REJECT_REASONS)
        flat = [b["payload"] for row in rows for b in row]
        for p in flat:
            if p.startswith("rj:"):
                self.assertIsNotNone(logic.parse_reject_callback(p), p)

    def test_contact_button_is_request_contact(self):
        rows = kb.share_contact()
        self.assertEqual(rows[0][0]["type"], "request_contact")

    def test_every_button_has_text(self):
        for rows in (kb.subscribe("https://max.ru/x"), kb.oferta("https://o"),
                     kb.confirm(), kb.sign_contract(), kb.main_menu(),
                     kb.support_cancel(), kb.same_address()):
            for row in rows:
                for b in row:
                    self.assertTrue(b.get("text"), b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
