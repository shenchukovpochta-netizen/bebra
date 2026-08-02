"""Тесты чистой логики. Гоняются на голом stdlib, без установки aiogram:
    py -3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import logic  # noqa: E402


class TestEscaping(unittest.TestCase):
    def test_markup_neutralised(self):
        self.assertEqual(logic.esc("<script>x"), "&lt;script&gt;x")

    def test_ampersand_first(self):
        # & экранируется первым, иначе &lt; превратится в &amp;lt;
        self.assertEqual(logic.esc("a & <b>"), "a &amp; &lt;b&gt;")

    def test_none_is_empty(self):
        self.assertEqual(logic.esc(None), "")


class TestFio(unittest.TestCase):
    def test_valid(self):
        r = logic.validate_fio("  Иванов   Иван Иванович ")
        self.assertTrue(r.ok)
        self.assertEqual(r.value, "Иванов Иван Иванович")

    def test_too_short(self):
        self.assertFalse(logic.validate_fio("Ян").ok)

    def test_digits_rejected(self):
        self.assertFalse(logic.validate_fio("Иванов Иван 1990").ok)

    def test_markup_rejected(self):
        r = logic.validate_fio("<b>Иванов Иван</b>")
        self.assertFalse(r.ok)
        self.assertIn("недопустимы", r.error)

    def test_link_injection_rejected(self):
        # Именно этот вектор бьёт по модератору: подделанная ссылка рядом
        # с кнопкой «Одобрить».
        self.assertFalse(logic.validate_fio('<a href="https://evil.ru">Иванов Иван</a>').ok)

    def test_too_long(self):
        self.assertFalse(logic.validate_fio("И" * 121).ok)

    def test_none(self):
        self.assertFalse(logic.validate_fio(None).ok)


class TestSubscription(unittest.TestCase):
    def test_member_statuses(self):
        for status in ("creator", "administrator", "member"):
            self.assertTrue(logic.is_subscribed(status), status)

    def test_left_and_kicked(self):
        for status in ("left", "kicked", None, ""):
            self.assertFalse(logic.is_subscribed(status), status)

    def test_restricted_requires_membership(self):
        self.assertTrue(logic.is_subscribed("restricted", True))
        self.assertFalse(logic.is_subscribed("restricted", False))
        self.assertFalse(logic.is_subscribed("restricted", None))


class TestContact(unittest.TestCase):
    def test_own_contact(self):
        self.assertTrue(logic.contact_belongs_to_sender(5, 5))

    def test_forwarded_foreign_contact(self):
        self.assertFalse(logic.contact_belongs_to_sender(999, 5))

    def test_contact_without_account(self):
        self.assertFalse(logic.contact_belongs_to_sender(None, 5))


class TestModerationCallback(unittest.TestCase):
    def test_approve(self):
        self.assertEqual(logic.parse_moderation_callback("approve:123"), ("approve", 123))

    def test_reject(self):
        self.assertEqual(logic.parse_moderation_callback("reject:77"), ("reject", 77))

    def test_garbage_id(self):
        # Иначе UPDATE уходит вхолостую, а модератор видит «Одобрено»
        self.assertIsNone(logic.parse_moderation_callback("approve:abc"))

    def test_negative_and_zero(self):
        self.assertIsNone(logic.parse_moderation_callback("approve:-5"))
        self.assertIsNone(logic.parse_moderation_callback("approve:0"))

    def test_trailing_junk(self):
        self.assertIsNone(logic.parse_moderation_callback("approve:123x"))

    def test_unknown_action(self):
        self.assertIsNone(logic.parse_moderation_callback("delete:123"))
        self.assertIsNone(logic.parse_moderation_callback(None))


def vision(lines=(), entities=None):
    return {"result": {"textAnnotation": {
        "blocks": [{"lines": [{"text": t} for t in lines]}] if lines else [],
        "entities": [{"name": k, "text": v} for k, v in (entities or {}).items()],
    }}}


class TestNameMatch(unittest.TestCase):
    def test_full_match(self):
        r = logic.match_name("Иванов Иван Иванович",
                             vision(["ИВАНОВ", "ИВАН ИВАНОВИЧ", "паспорт"]))
        self.assertTrue(r.recognized)
        self.assertEqual(r.score, 1.0)
        self.assertFalse(r.mismatch)

    def test_yo_normalised(self):
        r = logic.match_name("Пётр Селёзнёв Артёмович",
                             vision(["ПЕТР СЕЛЕЗНЕВ АРТЕМОВИЧ"]))
        self.assertEqual(r.score, 1.0)

    def test_foreign_document(self):
        r = logic.match_name("Иванов Иван Иванович", vision(["ПЕТРОВ ПЕТР ПЕТРОВИЧ"]))
        self.assertEqual(r.score, 0.0)
        self.assertTrue(r.mismatch)

    def test_partial_two_of_three(self):
        r = logic.match_name("Иванов Иван Сергеевич", vision(["ИВАНОВ ИВАН ПЕТРОВИЧ"]))
        self.assertEqual(r.score, 0.67)
        self.assertFalse(r.mismatch)      # 0.67 - проходной минимум

    def test_one_of_three_is_mismatch(self):
        r = logic.match_name("Иванов Пётр Сергеевич", vision(["ИВАНОВ АННА ПЕТРОВНА"]))
        self.assertLess(r.score, logic.NAME_MATCH_MIN)
        self.assertTrue(r.mismatch)

    def test_substring_does_not_count(self):
        # Мужское имя не должно подтверждаться женским отчеством:
        # при сравнении по подстроке «ПЕТР» находился внутри «ПЕТРОВНА».
        self.assertFalse(logic.token_matches("ПЕТР", ["АННА", "ПЕТРОВНА"]))
        self.assertTrue(logic.token_matches("ИВАН", ["ИВАНОВ"]))
        self.assertTrue(logic.token_matches("ИВАНОВ", ["ИВАНОВ"]))

    def test_entities_without_blocks(self):
        r = logic.match_name("Иванов Иван Иванович",
                             vision(entities={"surname": "Иванов", "name": "Иван",
                                              "patronymic": "Иванович"}))
        self.assertTrue(r.recognized)
        self.assertEqual(r.score, 1.0)

    def test_empty_response_is_not_mismatch_but_unrecognised(self):
        r = logic.match_name("Иванов Иван Иванович", vision())
        self.assertFalse(r.recognized)
        self.assertTrue(r.mismatch)

    def test_broken_payload_does_not_raise(self):
        for payload in (None, "", [], {"result": None}, {"result": {"textAnnotation": None}}):
            r = logic.match_name("Иванов Иван Иванович", payload)
            self.assertFalse(r.recognized)

    def test_raw_text_is_not_exposed(self):
        # Наружу отдаются только структурные поля и доля совпадения:
        # серия и номер паспорта в базе не оседают.
        r = logic.match_name("Иванов Иван", vision(["ИВАНОВ ИВАН", "4509 123456"]))
        self.assertNotIn("123456", repr(r))

    def test_short_tokens_ignored(self):
        r = logic.match_name("Ли Ван Чжан", vision(["ВАН ЧЖАН"]))
        self.assertEqual(r.tokens_total, 2)      # «Ли» короче трёх букв


class TestStorePath(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(logic.is_safe_store_path("/files/kyc/12345-doc-1700000000000.jpg", "/files/kyc"))
        self.assertTrue(logic.is_safe_store_path("/files/kyc/1-selfie-1.jpg", "/files/kyc"))

    def test_traversal_and_foreign_paths(self):
        for path in ("/files/kyc/../../etc/passwd", "/etc/passwd", "/files/kyc/x-doc-1.jpg",
                     "/files/kyc/1-doc-1.png", "", None, "/files/kyc/1-other-1.jpg"):
            self.assertFalse(logic.is_safe_store_path(path, "/files/kyc"), path)

    def test_follows_configured_directory(self):
        # STORAGE_DIR настраивается; раньше каталог был зашит в регулярку,
        # и при любом другом значении ретеншен молча переставал удалять сканы
        self.assertTrue(logic.is_safe_store_path("/data/scans/7-doc-1.jpg", "/data/scans"))
        self.assertFalse(logic.is_safe_store_path("/files/kyc/7-doc-1.jpg", "/data/scans"))

    def test_traversal_rejected(self):
        for bad in ("/files/kyc/../../etc/passwd",
                    "/files/kyc/sub/7-doc-1.jpg",
                    "/files/kyc/7-doc-1.jpg.sh"):
            self.assertFalse(logic.is_safe_store_path(bad, "/files/kyc"), bad)



class TestRateLimit(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(logic.rate_limit_verdict(1), "ok")
        self.assertEqual(logic.rate_limit_verdict(20), "ok")
        self.assertEqual(logic.rate_limit_verdict(21), "warn")
        self.assertEqual(logic.rate_limit_verdict(25), "warn")
        self.assertEqual(logic.rate_limit_verdict(26), "drop")


if __name__ == "__main__":
    unittest.main(verbosity=2)
