"""Тесты чистой логики. Гоняются на голом stdlib, без установки aiogram:
    py -3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import logic  # noqa: E402

# Дата, относительно которой считается возраст. Пинуется явно: тест,
# зависящий от сегодняшнего дня, однажды падает сам по себе.
TODAY = date(2026, 8, 2)


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



class TestDates(unittest.TestCase):
    def test_separators_are_interchangeable(self):
        # Набирают по-разному; отказ на верной дате - способ потерять человека
        for raw in ("07.03.1990", "7.3.1990", "07/03/1990", "07-03-1990"):
            self.assertEqual(logic.validate_date(raw, today=TODAY).value, "07.03.1990", raw)

    def test_nonexistent_date_rejected(self):
        self.assertFalse(logic.validate_date("31.02.1990", today=TODAY).ok)

    def test_future_rejected(self):
        self.assertFalse(logic.validate_date("01.01.2030", today=TODAY).ok)

    def test_garbage_rejected(self):
        for raw in ("вчера", "", None, "1990"):
            self.assertFalse(logic.validate_date(raw, today=TODAY).ok, repr(raw))


class TestBirthDate(unittest.TestCase):
    def test_adult_accepted(self):
        self.assertTrue(logic.validate_birth_date("07.03.1990", today=TODAY).ok)

    def test_underage_rejected(self):
        """Договор проката с несовершеннолетним оспаривается целиком,
        и узнать об этом на выдаче - значит уже завести на него договор."""
        self.assertFalse(logic.validate_birth_date("01.01.2015", today=TODAY).ok)

    def test_exactly_eighteen_today_accepted(self):
        self.assertTrue(logic.validate_birth_date("02.08.2008", today=TODAY).ok)

    def test_day_before_eighteenth_rejected(self):
        self.assertFalse(logic.validate_birth_date("03.08.2008", today=TODAY).ok)


class TestPassport(unittest.TestCase):
    def test_number_normalised(self):
        for raw in ("1234567890", "1234 567890", "12 34 567890", "1234-567890"):
            self.assertEqual(logic.validate_passport_number(raw).value, "1234 567890", raw)

    def test_wrong_length_rejected(self):
        for raw in ("12345", "12345678901", "", None):
            self.assertFalse(logic.validate_passport_number(raw).ok, repr(raw))

    def test_code_normalised(self):
        self.assertEqual(logic.validate_passport_code("160002").value, "160-002")
        self.assertEqual(logic.validate_passport_code("160-002").value, "160-002")

    def test_issue_date_before_fourteen_caught(self):
        """Перепутанные местами даты в договор уходят молча, а на бумаге
        это очевидная ерунда."""
        self.assertFalse(logic.passport_date_consistent(
            {"birth_date": "07.03.1990", "passport_date": "01.01.1995"}))

    def test_consistent_pair_passes(self):
        self.assertTrue(logic.passport_date_consistent(
            {"birth_date": "07.03.1990", "passport_date": "01.02.2015"}))

    def test_missing_half_is_not_a_verdict(self):
        self.assertTrue(logic.passport_date_consistent({"birth_date": "07.03.1990"}))
        self.assertTrue(logic.passport_date_consistent({}))


class TestAddress(unittest.TestCase):
    def test_full_address_accepted(self):
        self.assertTrue(logic.validate_address("г. Казань, ул. Баумана, д. 1, кв. 2").ok)

    def test_without_house_number_rejected(self):
        """Адрес без номера дома в договоре не идентифицирует никого,
        а это единственное, по чему ищут арендатора."""
        self.assertFalse(logic.validate_address("г. Казань, улица Баумана").ok)

    def test_too_short_rejected(self):
        self.assertFalse(logic.validate_address("д. 1").ok)

    def test_markup_rejected(self):
        self.assertFalse(logic.validate_address("г. Казань, <b>д. 1</b>, кв. 2").ok)


class TestPhones(unittest.TestCase):
    def test_russian_forms_normalised(self):
        for raw in ("89001234567", "+7 900 123-45-67", "9001234567", "7 900 1234567"):
            self.assertEqual(logic.normalize_phone(raw), "+79001234567", raw)

    def test_foreign_number_kept(self):
        self.assertEqual(logic.normalize_phone("+49 151 12345678"), "+4915112345678")

    def test_garbage_rejected(self):
        for raw in ("телефон", "123", "", None):
            self.assertIsNone(logic.normalize_phone(raw), repr(raw))

    def test_duplicate_rejected(self):
        """Три одинаковых номера в договоре - это один номер, а смысл трёх
        контактов ровно в том, чтобы дозвониться."""
        result = logic.validate_phone("8 900 123-45-67", taken=["+79001234567"])
        self.assertFalse(result.ok)


class TestFlowOrder(unittest.TestCase):
    def test_every_anketa_step_is_in_flow(self):
        for step in logic.ANKETA_STEPS:
            self.assertIn(step.state, logic.FLOW, step.state)

    def test_every_flow_state_is_known(self):
        for state in logic.FLOW:
            self.assertTrue(logic.is_known_state(state), state)

    def test_next_state_walks_to_confirm(self):
        state, seen = logic.WAIT_FIO, []
        while state is not None:
            seen.append(state)
            state = logic.next_state(state)
        self.assertEqual(seen[-1], logic.CONFIRM)
        self.assertEqual(seen, list(logic.FLOW))

    def test_unknown_state_has_no_successor(self):
        self.assertIsNone(logic.next_state("wait_pdn"))
        self.assertIsNone(logic.next_state(None))

    def test_fields_are_unique(self):
        self.assertEqual(len(set(logic.ANKETA_FIELDS)), len(logic.ANKETA_FIELDS))

    def test_completeness(self):
        full = {f: "x" for f in logic.ANKETA_FIELDS}
        self.assertTrue(logic.anketa_complete(full))
        self.assertEqual(logic.missing_anketa_fields(full), ())
        partial = dict(full, passport_code="   ")
        self.assertFalse(logic.anketa_complete(partial))
        self.assertEqual(logic.missing_anketa_fields(partial), ("passport_code",))


class TestContractNumbering(unittest.TestCase):
    def test_format(self):
        self.assertEqual(logic.contract_number(42, today=TODAY), "АВ-2026-000042")

    def test_sequence_is_padded(self):
        self.assertEqual(logic.contract_number(1, today=TODAY), "АВ-2026-000001")

    def test_context_fills_every_label(self):
        user = {"tg_id": 1, "full_name": "Иванов Иван", "phone": "89001234567"}
        anketa = {f: f"знач-{f}" for f in logic.ANKETA_FIELDS}
        ctx = logic.contract_context(user, anketa, number="АВ-2026-000001", today=TODAY)
        for field, _label in logic.CONTRACT_LABELS:
            self.assertIn(field, ctx)
            self.assertTrue(ctx[field])
        self.assertEqual(ctx["phone"], "+79001234567")

    def test_empty_field_becomes_dash(self):
        """Пустое поле должно стать видимым прочерком, а не строкой None
        посреди договора."""
        ctx = logic.contract_context({"tg_id": 1}, {}, number="X", today=TODAY)
        self.assertEqual(ctx["fio"], "—")
        self.assertEqual(ctx["passport_number"], "—")


class TestModerationCallbacks(unittest.TestCase):
    def test_reject_reason_parsed(self):
        self.assertEqual(logic.parse_reject_callback("rj:5001:doc"), (5001, "doc"))

    def test_unknown_reason_rejected(self):
        self.assertIsNone(logic.parse_reject_callback("rj:5001:nosuch"))

    def test_malformed_rejected(self):
        for raw in ("rj:5001", "rj:-5:doc", "rj:0:doc", "", None, "approve:1"):
            self.assertIsNone(logic.parse_reject_callback(raw), repr(raw))

    def test_every_reason_returns_to_a_known_state(self):
        for code, (title, state) in logic.REJECT_REASONS.items():
            self.assertTrue(title, code)
            self.assertTrue(logic.is_known_state(state), f"{code} -> {state}")

    def test_all_moderation_buttons_pass_the_gate(self):
        """Регрессия: middleware пускал в служебный чат только approve/reject,
        и кнопки выбора причины отказа там не доходили до обработчика."""
        for data in ("approve:5001", "reject:5001", "rj:5001:doc",
                     "rjc:5001", "rjx:5001"):
            self.assertTrue(logic.is_moderation_data(data), data)

    def test_user_buttons_do_not_pass_the_gate(self):
        for data in ("confirm", "restart", "sign", "oferta_ok", None):
            self.assertFalse(logic.is_moderation_data(data), repr(data))

    def test_moderator_reply_passes_only_from_service_chat(self):
        self.assertTrue(logic.should_process(
            "supergroup", from_admin_chat=True,
            is_moderation_callback=False, is_moderation_reply=True))
        self.assertFalse(logic.should_process(
            "supergroup", from_admin_chat=False,
            is_moderation_callback=False, is_moderation_reply=True))


class TestRejectComment(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(logic.reject_comment("  Паспорт  засвечен ").value,
                         "Паспорт засвечен")

    def test_too_short_and_too_long_rejected(self):
        self.assertFalse(logic.reject_comment("ок").ok)
        self.assertFalse(logic.reject_comment("x" * 501).ok)


class TestRateLimit(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(logic.rate_limit_verdict(1), "ok")
        self.assertEqual(logic.rate_limit_verdict(logic.RATE_SOFT_DEFAULT), "ok")
        self.assertEqual(logic.rate_limit_verdict(logic.RATE_SOFT_DEFAULT + 1), "warn")
        self.assertEqual(logic.rate_limit_verdict(logic.RATE_HARD_DEFAULT), "warn")
        self.assertEqual(logic.rate_limit_verdict(logic.RATE_HARD_DEFAULT + 1), "drop")

    def test_explicit_thresholds_still_honoured(self):
        self.assertEqual(logic.rate_limit_verdict(21, soft=20, hard=25), "warn")
        self.assertEqual(logic.rate_limit_verdict(26, soft=20, hard=25), "drop")

    def test_default_allows_a_full_registration(self):
        """Анкета выросла до шестнадцати сообщений. Порог, при котором человек
        упирается в предупреждение о флуде посреди собственной регистрации,
        сам по себе дефект - даже если формально это «защита»."""
        steps = len(logic.FLOW) + len(logic.ANKETA_STEPS)
        self.assertEqual(logic.rate_limit_verdict(steps), "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
