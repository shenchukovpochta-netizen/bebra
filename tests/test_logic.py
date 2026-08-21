"""Тесты чистой логики. Гоняются на голом stdlib, без установки aiogram:
    py -3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
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


class TestStorePath(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(
            logic.is_safe_store_path("/files/kyc/12345-doc-1700000000000.jpg", "/files/kyc"))

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

    def test_contract_pdf_recognised(self):
        self.assertTrue(logic.is_safe_store_path("/files/kyc/1-contract-17.pdf", "/files/kyc"))
        self.assertTrue(logic.is_safe_store_path("/files/kyc/1-soglasie-17.docx",
                                                 "/files/kyc"))

    def test_parent_consent_recognised(self):
        """Файл согласия родителя обязан подходить под шаблон - иначе
        ретеншен никогда его не удалит."""
        self.assertTrue(logic.is_safe_store_path("/files/kyc/1-parent-17.jpg", "/files/kyc"))

    def test_removed_selfie_slot_rejected(self):
        """Слот селфи убран. Файл с таким именем ретеншен опознавать не должен:
        иначе он подметает то, чего бот больше не создаёт."""
        self.assertFalse(logic.is_safe_store_path("/files/kyc/1-selfie-1.jpg", "/files/kyc"))


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

    def test_under_sixteen_rejected(self):
        """Сделка с тем, кому нет 16, ничтожна целиком, и узнать об этом
        на выдаче - значит уже завести на ребёнка договор."""
        self.assertFalse(logic.validate_birth_date("01.01.2015", today=TODAY).ok)

    def test_exactly_sixteen_today_accepted(self):
        self.assertTrue(logic.validate_birth_date("02.08.2010", today=TODAY).ok)

    def test_day_before_sixteenth_rejected(self):
        self.assertFalse(logic.validate_birth_date("03.08.2010", today=TODAY).ok)

    def test_seventeen_accepted_as_minor(self):
        """16-17 лет - не отказ: дальше появится шаг согласия родителя."""
        result = logic.validate_birth_date("03.08.2008", today=TODAY)
        self.assertTrue(result.ok)
        self.assertTrue(logic.is_minor({"birth_date": result.value}, today=TODAY))


class TestIssueForm(unittest.TestCase):
    FORM = ("рама: 264022410703084\n"
            "мотор: 240W25021406\n"
            "модель: Truck+\n"
            "акб: 2\nзу: 1\n"
            "срок: 03.08 - 10.08\n"
            "оплата: 3000 qr")

    def test_full_form_parses(self):
        data, err = logic.parse_issue_form(self.FORM)
        self.assertEqual(err, "")
        self.assertEqual(data["vin_frame"], "264022410703084")
        self.assertEqual(data["vin_motor"], "240W25021406")
        self.assertEqual(data["bike_model"], "Truck+")
        self.assertEqual(data["rent_term"], "03.08 - 10.08")
        self.assertEqual(data["rent_price"], "3000 qr")
        self.assertEqual(data["kit_akb"], "2")
        self.assertEqual(data["kit_mirrors"], "0", "не названное - по умолчанию")

    def test_missing_required_named(self):
        data, err = logic.parse_issue_form("рама: 1\nмотор: 2\nсрок: x")
        self.assertIsNone(data)
        self.assertIn("оплата", err)

    def test_unknown_key_is_error_not_silence(self):
        """Опечатка в ключе не должна тихо терять значение."""
        data, err = logic.parse_issue_form(self.FORM + "\nколесо: 2")
        self.assertIsNone(data)
        self.assertIn("колесо", err)

    def test_kit_must_be_number(self):
        data, err = logic.parse_issue_form(self.FORM.replace("акб: 2", "акб: два"))
        self.assertIsNone(data)
        self.assertIn("акб", err)

    def test_markup_rejected(self):
        data, err = logic.parse_issue_form(self.FORM + "\nмодель: <b>x</b>")
        self.assertIsNone(data)

    def test_issue_context_defaults_to_dashes(self):
        ctx = logic.issue_context(None)
        self.assertEqual(ctx["vin_frame"], "—")
        self.assertEqual(ctx["kit_akb"], "2")
        self.assertEqual(ctx["kit_helmet"], "0")

    def test_return_form(self):
        data, err = logic.parse_return_form("Царапина, штраф 500")
        self.assertEqual(data["return_notes"], "Царапина, штраф 500")
        self.assertIsNone(logic.parse_return_form("")[0])
        self.assertIsNone(logic.parse_return_form("<b>x</b> длиннее трёх")[0])


class TestMinor(unittest.TestCase):
    def test_eighteen_is_not_minor(self):
        self.assertFalse(logic.is_minor({"birth_date": "02.08.2008"}, today=TODAY))

    def test_no_birth_date_is_not_minor(self):
        """Сбой разбора даты не должен навешивать взрослому лишний шаг:
        это отказ в регистрации на ровном месте."""
        self.assertFalse(logic.is_minor({}, today=TODAY))
        self.assertFalse(logic.is_minor(None, today=TODAY))
        self.assertFalse(logic.is_minor({"birth_date": "мусор"}, today=TODAY))

    def test_minor_without_consent_goes_to_parent_step(self):
        self.assertEqual(
            logic.state_after_doc({"birth_date": "01.01.2009"},
                                  has_parent_consent=False, today=TODAY),
            logic.WAIT_PARENT_CONSENT)

    def test_minor_with_consent_goes_to_confirm(self):
        self.assertEqual(
            logic.state_after_doc({"birth_date": "01.01.2009"},
                                  has_parent_consent=True, today=TODAY),
            logic.CONFIRM)

    def test_adult_skips_parent_step(self):
        self.assertEqual(
            logic.state_after_doc({"birth_date": "07.03.1990"},
                                  has_parent_consent=False, today=TODAY),
            logic.CONFIRM)

    def test_reject_parent_reason_for_adult_goes_to_doc(self):
        """Промах модератора по кнопке «согласие родителя» на взрослом
        не должен запирать его на шаге, которого нет в его сценарии."""
        self.assertEqual(
            logic.reject_back_to("parent", {"birth_date": "07.03.1990"},
                                 today=TODAY),
            logic.WAIT_DOC)

    def test_reject_parent_reason_for_minor_goes_to_parent(self):
        self.assertEqual(
            logic.reject_back_to("parent", {"birth_date": "01.01.2009"},
                                 today=TODAY),
            logic.WAIT_PARENT_CONSENT)

    def test_fixation_form_exact_labels(self):
        """Нумерация и написание строк - дословно: форму разбирает другой бот,
        и любое расхождение в метке - потерянная колонка таблицы."""
        user = {"tg_id": 1, "full_name": "Михайлов Данил Дамирович",
                "phone": "+79991571094", "username": "sdafgaerg"}
        anketa = {"phone2": "+79194485203", "phone3": "+79223051941",
                  "reg_address": "пермский район",
                  "live_address": "ботаническая 20 кв 17"}
        form = logic.fixation_form(user, anketa)
        lines = form.splitlines()
        self.assertEqual(lines[0], "1. ФИО: Михайлов Данил Дамирович")
        self.assertEqual(lines[1], "2. Вин номер рамы: —")
        self.assertEqual(lines[2], "3. Вин номер мотор колеса: —")
        self.assertEqual(lines[3], "4. Комплектация: ")
        self.assertEqual(lines[4], "  - АКБ: 2")
        self.assertEqual(lines[5], "  - ЗУ: 1")
        self.assertIn("  - Теплые перчатки на руль (муфты): 0", lines)
        self.assertIn("  - Стяжка для крепления сумки: 0", lines)
        self.assertIn("5. Сроки аренды: —", lines)
        self.assertIn("6. Номер телефона (основной): 89991571094", lines)
        self.assertIn("7. Номер телефона 2: 89194485203", lines)
        self.assertIn("8. Номер телефона 3: 89223051941", lines)
        self.assertIn("9. Ник в Telegram: @sdafgaerg", lines)
        self.assertIn("11. Адрес прописки с квартирой в Казани: пермский район", lines)
        self.assertIn("12. Адрес проживания с квартирой в Казани: ботаническая 20 кв 17",
                      lines)
        self.assertIn("16. Подписка на тг: да", lines)
        self.assertEqual(lines[-1], "17. Реф.программа: —")

    def test_fixation_form_blanks_without_data(self):
        form = logic.fixation_form({"tg_id": 1}, None, subscribed=False)
        self.assertIn("1. ФИО: —", form)
        self.assertIn("9. Ник в Telegram: —", form)
        self.assertIn("16. Подписка на тг: нет", form)

    def test_phone_for_form(self):
        self.assertEqual(logic.phone_for_form("+79991571094"), "89991571094")
        # иностранный номер не коверкаем
        self.assertEqual(logic.phone_for_form("+4915112345678"), "+4915112345678")
        self.assertEqual(logic.phone_for_form(None), "—")

    def test_minor_clause_in_contract_context(self):
        user = {"tg_id": 1, "full_name": "Иванов Иван", "phone": "+79001234567"}
        minor = logic.contract_context(
            user, {"birth_date": "01.01.2009"}, number="АВ-1", today=TODAY)
        adult = logic.contract_context(
            user, {"birth_date": "07.03.1990"}, number="АВ-1", today=TODAY)
        self.assertEqual(minor["minor_clause"], logic.MINOR_CLAUSE)
        # У взрослого - пустота, а не прочерк: «—» отдельным абзацем
        # посреди договора выглядит браком.
        self.assertEqual(adult["minor_clause"], "")


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
        self.assertIsNone(logic.next_state("wait_selfie"))
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
        и кнопки выбора причины отказа там не доходили до обработчика.
        «pay» - кнопка «Оплата получена» на карточке оплаты."""
        for data in ("approve:5001", "reject:5001", "rj:5001:doc",
                     "rjc:5001", "rjx:5001", "pay:5001"):
            self.assertTrue(logic.is_moderation_data(data), data)

    def test_user_buttons_do_not_pass_the_gate(self):
        for data in ("confirm", "restart", "sign", "oferta_ok", "pdn_ok",
                     "paid", None):
            self.assertFalse(logic.is_moderation_data(data), repr(data))


class TestPayCallback(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(logic.parse_pay_callback("pay:5001"), 5001)

    def test_malformed_rejected(self):
        for raw in ("pay:abc", "pay:-5", "pay:0", "pay:", "paid",
                    "approve:5001", "", None):
            self.assertIsNone(logic.parse_pay_callback(raw), repr(raw))

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


class TestCloseForm(unittest.TestCase):
    """Форма закрытия аренды: из неё собираются акт возврата и отчёт."""

    TODAY = date(2026, 8, 7)

    def parse(self, raw, **kw):
        return logic.parse_close_form(raw, today=self.TODAY, **kw)

    def test_minimum_is_address_and_who_accepted(self):
        data, err = self.parse("адрес: адоратского\nпринял: ирик")
        self.assertEqual(err, "")
        self.assertEqual(data["return_address"], "адоратского")
        self.assertEqual(data["accepted_by"], "ирик")

    def test_money_fields_default_to_zero(self):
        """Оператор не должен писать четыре нуля руками: пустые поля
        в отчёте читаются как «неизвестно», а не как «не платил»."""
        data, _ = self.parse("адрес: а\nпринял: и")
        for field in ("debt_paid", "damage", "repair_paid", "wash_paid"):
            self.assertEqual(data[field], "0", field)
        self.assertEqual(data["closed_at"], "07.08")

    def test_client_reason_is_taken_without_retyping(self):
        data, _ = self.parse("адрес: а\nпринял: и", reason="выхожу на работу")
        self.assertEqual(data["reason"], "выхожу на работу")

    def test_operator_can_override_the_reason(self):
        data, _ = self.parse("адрес: а\nпринял: и\nпричина: сломал ногу",
                             reason="выхожу на работу")
        self.assertEqual(data["reason"], "сломал ногу")

    def test_missing_required_is_named(self):
        data, err = self.parse("принял: ирик")
        self.assertIsNone(data)
        self.assertIn("адрес", err)

    def test_unknown_key_is_named(self):
        data, err = self.parse("адрес: а\nпринял: и\nколесо: 2")
        self.assertIsNone(data)
        self.assertIn("колесо", err)

    def test_markup_is_rejected(self):
        data, err = self.parse("адрес: <b>а</b>\nпринял: и")
        self.assertIsNone(data)
        self.assertIn("< >", err)

    def test_notes_for_the_act_mention_only_property(self):
        """В акт возврата идут повреждения и суммы, а не причина и отзывы."""
        data, _ = self.parse("адрес: а\nпринял: и\nпричина: надоело\n"
                             "отзыв: оставил\nповреждения: царапина\nремонт: 500")
        notes = logic.close_notes(data)
        self.assertIn("царапина", notes)
        self.assertIn("500", notes)
        self.assertNotIn("надоело", notes)
        self.assertNotIn("оставил", notes)

    def test_clean_return_says_so(self):
        data, _ = self.parse("адрес: а\nпринял: и")
        self.assertEqual(logic.close_notes(data), "Без замечаний")

    def test_report_is_verbatim(self):
        """Отчёт разбирает чужой бот: строки сверяются дословно."""
        data, _ = self.parse("адрес: адоратского\nпринял: ирик\nотзыв: оставил\n"
                             "рекомендации: все ок", reason="на осн работу выходит")
        report = logic.closure_report(
            {"full_name": "Михеев Никита Владимирович"},
            {"vin_frame": "264022410701350", "vin_motor": "240W2024081673"}, data)
        self.assertEqual(report.splitlines(), [
            "Когда сдал: 07.08",
            "Сколько оплатил долгов: 0",
            "Какие повреждения есть: 0",
            "Сколько оплатил за ремонт: 0",
            "Оплатил мойку велосипеда: 0",
            "Причина сдачи: на осн работу выходит",
            "Адрес сдачи: адоратского",
            "Кто принял велик: ирик",
            "Оставил отзыв: оставил",
            "Какие рекомендации по улучшению сервиса/вело дали: все ок",
            "1. ФИО: Михеев Никита Владимирович",
            "2. Вин номер рамы: 264022410701350",
            "3. Вин номер мотор колеса: 240W2024081673",
        ])

    def test_report_survives_missing_issue_data(self):
        data, _ = self.parse("адрес: а\nпринял: и")
        report = logic.closure_report({}, None, data)
        self.assertIn("1. ФИО: —", report)
        self.assertIn("2. Вин номер рамы: —", report)

    def test_report_escapes_html(self):
        data, _ = self.parse("адрес: а\nпринял: и")
        report = logic.closure_report({"full_name": "Иванов & Со"}, None, data)
        self.assertIn("&amp;", report)


class TestCloseReason(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(logic.close_reason("  выхожу  на работу ").value,
                         "выхожу на работу")

    def test_too_short_and_too_long_rejected(self):
        self.assertFalse(logic.close_reason("ок").ok)
        self.assertFalse(logic.close_reason("x" * 301).ok)
        self.assertFalse(logic.close_reason(None).ok)

    def test_markup_rejected(self):
        self.assertFalse(logic.close_reason("<b>работа</b>").ok)


class TestTermDates(unittest.TestCase):
    TODAY = date(2026, 8, 12)

    def parse(self, raw):
        return logic.parse_term_dates(raw, today=self.TODAY)

    def test_range_with_and_without_spaces(self):
        for raw in ("03.08 - 10.08", "03.08-10.08", "03.08 — 10.08",
                    "с 3.8 по 10.8"):
            self.assertEqual(self.parse(raw),
                             (date(2026, 8, 3), date(2026, 8, 10)), raw)

    def test_dash_separated_dates_are_not_a_year(self):
        """«03.08-10.08» - это диапазон, а не «3 августа 2010»: двузначное
        число после дефиса годом не считается."""
        self.assertEqual(self.parse("03.08-10.08")[1], date(2026, 8, 10))
        # четырёхзначный год после дефиса - всё ещё год
        self.assertEqual(self.parse("03-08-2026 - 10-08-2026"),
                         (date(2026, 8, 3), date(2026, 8, 10)))

    def test_single_date_is_the_end(self):
        self.assertEqual(self.parse("10.08"), (None, date(2026, 8, 10)))

    def test_single_date_after_s_is_the_start(self):
        """«1 месяц с 01.08» - дата начала. Приняв её за конец, бот прислал бы
        напоминание об окончании в день выдачи."""
        self.assertEqual(self.parse("1 месяц с 01.08"), (date(2026, 8, 1), None))

    def test_new_year_rollover(self):
        self.assertEqual(self.parse("28.12 - 04.01"),
                         (date(2026, 12, 28), date(2027, 1, 4)))

    def test_words_give_nothing(self):
        self.assertEqual(self.parse("неделя"), (None, None))
        self.assertEqual(self.parse(""), (None, None))
        self.assertEqual(self.parse(None), (None, None))

    def test_impossible_date_ignored(self):
        self.assertEqual(self.parse("31.02 - 10.08")[1], date(2026, 8, 10))

    def test_absurd_range_rejected(self):
        """Больше года - это опечатка в дате, а не аренда: молчим, вместо
        того чтобы годами не напоминать о просрочке."""
        self.assertEqual(self.parse("03.08.2026 - 10.08.2028"), (None, None))

    def test_explicit_until_wins_over_term(self):
        issue = {"rent_term": "неделя", "rent_until": "20.08"}
        self.assertEqual(logic.rent_dates(issue)[1].strftime("%d.%m"), "20.08")

    def test_term_with_new_end_keeps_the_start(self):
        self.assertEqual(logic.term_with_new_end("03.08 - 10.08",
                                                 date(2026, 8, 17)),
                         "03.08 - 17.08")
        self.assertEqual(logic.term_with_new_end("неделя", date(2026, 8, 17)),
                         "17.08")


class TestExtendForm(unittest.TestCase):
    TODAY = date(2026, 8, 12)

    def parse(self, raw):
        return logic.parse_extend_form(raw, today=self.TODAY)

    def test_minimal_form(self):
        data, err = self.parse("до: 17.08\nоплата: 3000 qr")
        self.assertEqual(err, "")
        self.assertEqual(data["rent_until"], date(2026, 8, 17))
        self.assertEqual(data["rent_price"], "3000 qr")

    def test_past_date_rejected(self):
        data, err = self.parse("до: 01.08.2026\nоплата: 3000")
        self.assertIsNone(data)
        self.assertIn("прошла", err)

    def test_missing_parts_named(self):
        self.assertIn("даты", self.parse("оплата: 3000")[1])
        self.assertIn("суммы", self.parse("до: 17.08")[1])

    def test_unknown_key_is_reported(self):
        data, err = self.parse("рама: 123\nдо: 17.08\nоплата: 3000")
        self.assertIsNone(data)
        self.assertIn("рама", err)

    def test_markup_rejected(self):
        self.assertIsNone(self.parse("до: 17.08\nоплата: <b>3000</b>")[0])


class TestReminders(unittest.TestCase):
    TODAY = date(2026, 8, 12)

    def row(self, until, **flags):
        base = {"tg_id": 1, "full_name": "Иванов И.", "contract_no": "АВ-1",
                "issue_data": {"bike_model": "Truck+"}, "rent_until": until}
        base.update(flags)
        return base

    def due(self, row):
        return logic.reminder_due(row, before_days=2, today=self.TODAY)

    def test_stages_by_days_left(self):
        self.assertEqual(self.due(self.row(date(2026, 8, 14))), logic.REMIND_SOON)
        self.assertEqual(self.due(self.row(date(2026, 8, 12))), logic.REMIND_LAST)
        self.assertEqual(self.due(self.row(date(2026, 8, 10))), logic.REMIND_OVERDUE)
        self.assertIsNone(self.due(self.row(date(2026, 9, 1))))
        self.assertIsNone(self.due(self.row(None)))

    def test_each_stage_fires_once(self):
        for until, field in ((date(2026, 8, 14), "remind_soon_at"),
                             (date(2026, 8, 12), "remind_last_at"),
                             (date(2026, 8, 10), "remind_overdue_at")):
            self.assertIsNone(self.due(self.row(until, **{field: "уже"})))

    def test_overdue_wins_over_stale_flags(self):
        """Бот сутки лежал: клиент должен получить актуальную просрочку,
        а не догоняющую цепочку из трёх сообщений."""
        row = self.row(date(2026, 8, 10), remind_soon_at="было",
                       remind_last_at="было")
        self.assertEqual(self.due(row), logic.REMIND_OVERDUE)

    def test_no_reminders_while_closure_requested(self):
        """Клиент уже попросил закрыть аренду: «продлите или сдайте»
        в ответ выглядит так, будто его не услышали."""
        row = self.row(date(2026, 8, 10), close_requested_at="вчера")
        self.assertIsNone(self.due(row))

    def test_no_reminders_while_extension_awaits_payment(self):
        """Заявка на продление принята, клиент платит - напоминать нечего."""
        row = self.row(date(2026, 8, 10), extend_until=date(2026, 8, 20))
        self.assertIsNone(self.due(row))

    def test_no_reminders_after_the_buyout(self):
        """Велосипед выкуплен и стал собственностью клиента: «продлите
        аренду или верните велосипед» после этого - неправда."""
        row = self.row(date(2026, 8, 10), buyout_done_at="вчера")
        self.assertIsNone(self.due(row))

    def test_digest_marks_bought_out_rentals(self):
        """Оператору пометка нужна: без неё строка читается как обычная
        просрочка, и он поедет искать велосипед, который уже не вернут."""
        rows = [self.row(date(2026, 8, 10), buyout_done_at="вчера")]
        digest = logic.deadline_digest(rows, today=self.TODAY)
        self.assertIn("выкуплен", digest)
        self.assertIn("просрочка", digest)

    def test_operator_still_sees_such_rentals_in_the_digest(self):
        """Клиента не трогаем, но у оператора аренда обязана остаться
        на виду: велосипед всё ещё не вернули."""
        rows = [self.row(date(2026, 8, 10), close_requested_at="вчера")]
        self.assertIn("просрочка", logic.deadline_digest(rows, today=self.TODAY))

    def test_digest_lists_overdue_and_ending(self):
        rows = [self.row(date(2026, 8, 10)),
                {**self.row(date(2026, 8, 12)), "tg_id": 2},
                {**self.row(date(2026, 8, 13)), "tg_id": 3},
                {**self.row(date(2026, 9, 1)), "tg_id": 4}]
        digest = logic.deadline_digest(rows, today=self.TODAY)
        self.assertIn("Просрочены", digest)
        self.assertIn("просрочка 2 дн.", digest)
        self.assertIn("сегодня", digest)
        self.assertIn("завтра", digest)
        self.assertNotIn("ID 4", digest, "далёкая аренда в сводке не нужна")

    def test_digest_is_empty_when_nothing_happens(self):
        """Пустую сводку не шлём: ежедневное «всё в порядке» перестают
        читать, и настоящая строка о просрочке в ней потеряется."""
        self.assertEqual(
            logic.deadline_digest([self.row(date(2026, 9, 1))], today=self.TODAY),
            "")


class TestReminderSchedule(unittest.TestCase):
    """Когда фоновый цикл делает дневной проход."""

    def now(self, hour):
        from datetime import datetime, timezone
        return datetime(2026, 8, 12, hour, 30, tzinfo=timezone.utc)

    def due(self, hour, last_run_on):
        from app import tasks
        return tasks.due_today(self.now(hour), last_run_on, 7)

    def test_fires_at_the_configured_hour(self):
        self.assertTrue(self.due(7, None))

    def test_silent_before_the_hour(self):
        self.assertFalse(self.due(6, None))

    def test_catches_up_after_a_midday_restart(self):
        """Бота перезапустили в 15:00 - напоминания за сегодня всё равно
        должны уйти, а не пропасть на сутки."""
        self.assertTrue(self.due(15, date(2026, 8, 11)))

    def test_once_a_day(self):
        self.assertFalse(self.due(9, date(2026, 8, 12)))


class TestBuyout(unittest.TestCase):
    """Аренда с правом выкупа: график из договора - два числа."""

    START = date(2026, 8, 5)

    def data(self, *, paid_until=None, total="150 000", payments="120", **extra):
        issue = {"bike_model": "Truck+"}
        if total is not None:
            issue["buyout_total"] = total
        if payments is not None:
            issue["buyout_payments"] = payments
        base = {"issue_data": issue, "rent_from": self.START,
                "buyout_from": self.START,
                "rent_until": paid_until or self.START}
        base.update(extra)
        return base

    def test_no_plan_without_numbers(self):
        self.assertIsNone(logic.buyout_state(self.data(total=None)))
        self.assertIsNone(logic.buyout_state(self.data(payments=None)))
        self.assertIsNone(logic.buyout_state({"issue_data": {}}))

    def test_absurd_numbers_are_ignored(self):
        """Опечатка оператора не должна превращаться в график на век."""
        self.assertIsNone(logic.buyout_state(self.data(payments="99999")))
        self.assertIsNone(logic.buyout_state(self.data(total="99999999999")))
        self.assertIsNone(logic.buyout_state(self.data(payments="0")))

    def test_money_is_parsed_from_human_text(self):
        state = logic.buyout_state(self.data(total="150 000 ₽"),
                                   today=self.START)
        self.assertEqual(state["total"], 150000)
        self.assertEqual(state["per_payment"], 1250)

    def test_accrues_by_paid_days(self):
        # оплачен месяц вперёд, но сегодня только пятый день
        data = self.data(paid_until=date(2026, 9, 5))
        state = logic.buyout_state(data, today=date(2026, 8, 9))
        self.assertEqual(state["paid_days"], 5)
        self.assertEqual(state["paid"], 6250)
        self.assertEqual(state["left"], 143750)
        self.assertFalse(state["done"])

    def test_does_not_count_beyond_today(self):
        """Оплачено вперёд - но платёж по графику наступает в свой день,
        и обещать собственность за завтрашние дни нельзя."""
        data = self.data(paid_until=date(2026, 12, 31))
        state = logic.buyout_state(data, today=date(2026, 8, 10))
        self.assertEqual(state["paid_days"], 6)

    def test_does_not_count_unpaid_days(self):
        """Срок кончился, клиент не продлил - выкуп стоит на месте."""
        data = self.data(paid_until=date(2026, 8, 20))
        state = logic.buyout_state(data, today=date(2026, 9, 30))
        self.assertEqual(state["paid_days"], 16)

    def test_completes_exactly_at_the_schedule_end(self):
        data = self.data(paid_until=date(2026, 12, 31))
        state = logic.buyout_state(data, today=date(2026, 12, 2))
        self.assertTrue(state["done"])
        self.assertEqual(state["paid_days"], 120)
        self.assertEqual(state["paid"], 150000)
        self.assertEqual(state["left"], 0)
        self.assertEqual(state["percent"], 100)
        # график из документа: 120 платежей с 05.08 - последний 02.12
        self.assertEqual(state["finish"], date(2026, 12, 2))

    def test_never_exceeds_the_total(self):
        data = self.data(paid_until=date(2027, 12, 31))
        state = logic.buyout_state(data, today=date(2027, 12, 31))
        self.assertEqual(state["paid"], 150000)
        self.assertEqual(state["left"], 0)
        self.assertEqual(state["paid_days"], 120)

    def test_rounding_does_not_lose_rubles(self):
        """Неровная сумма: последний платёж закрывает остаток целиком."""
        data = self.data(total="100000", payments="7",
                         paid_until=date(2026, 8, 11))
        state = logic.buyout_state(data, today=date(2026, 8, 11))
        self.assertTrue(state["done"])
        self.assertEqual(state["paid"], 100000)

    def test_progress_line_for_operator(self):
        data = self.data(paid_until=date(2026, 9, 5))
        line = logic.buyout_progress(data, today=date(2026, 8, 9))
        self.assertIn(logic.money(6250), line)
        self.assertIn(logic.money(150000), line)
        self.assertIn("5/120", line)
        self.assertEqual(logic.buyout_progress({"issue_data": {}}), "")

    def test_issue_form_accepts_buyout_lines(self):
        parsed, err = logic.parse_issue_form(
            "рама: 1\nмотор: 2\nсрок: 05.08 - 12.08\nоплата: 3000\n"
            "выкуп: 150000\nплатежей: 120")
        self.assertEqual(err, "")
        self.assertEqual(parsed["buyout_total"], "150000")
        self.assertEqual(parsed["buyout_payments"], "120")

    def test_days_of_previous_rentals_are_carried(self):
        """Платежи прошлых аренд лежат числом: график сквозной, а отсчёт
        по датам начинается заново с каждой выдачи."""
        data = self.data(paid_until=date(2026, 8, 9), buyout_days=30)
        state = logic.buyout_state(data, today=date(2026, 8, 9))
        self.assertEqual(state["paid_days"], 35)
        self.assertEqual(state["paid"], 43750)

    def test_gap_between_rentals_is_not_paid(self):
        """Между арендами человек за велосипед не платил: перерыв
        в график выкупа не идёт, иначе месяц без велосипеда выкупал бы
        его наравне с месяцем езды."""
        first = self.data(paid_until=date(2026, 8, 14))
        carried = logic.buyout_state(first, today=date(2026, 8, 14))["paid_days"]
        self.assertEqual(carried, 10)
        # месяц перерыва, потом новая выдача с 15.09
        second = self.data(paid_until=date(2026, 9, 24), buyout_days=carried,
                           buyout_from=date(2026, 9, 15),
                           rent_from=date(2026, 9, 15))
        state = logic.buyout_state(second, today=date(2026, 9, 24))
        self.assertEqual(state["paid_days"], 20, "перерыв засчитан как оплата")

    def test_carried_days_never_exceed_the_schedule(self):
        data = self.data(paid_until=date(2026, 8, 9), buyout_days=500)
        state = logic.buyout_state(data, today=date(2026, 8, 9))
        self.assertEqual(state["paid_days"], 120)
        self.assertEqual(state["paid"], 150000)
        self.assertTrue(state["done"])

    def test_finish_accounts_for_carried_days(self):
        """Дата окончания считается от последнего оплаченного дня:
        «начало плюс 120» с накопленными днями обещало бы собственность
        позже, чем она наступит."""
        data = self.data(paid_until=date(2026, 12, 31), buyout_days=100)
        state = logic.buyout_state(data, today=self.START)
        self.assertEqual(state["paid_days"], 101)
        self.assertEqual(state["finish"], self.START + timedelta(days=19))

    def test_half_filled_buyout_is_an_error(self):
        """Забытая строка выключала бы выкуп молча, и узнал бы об этом
        клиент - через несколько месяцев, не дождавшись собственности."""
        form = "рама: 1\nмотор: 2\nсрок: 05.08 - 12.08\nоплата: 3000\n"
        parsed, err = logic.parse_issue_form(form + "выкуп: 150000")
        self.assertIsNone(parsed)
        self.assertIn("платежей", err)
        parsed, err = logic.parse_issue_form(form + "платежей: 120")
        self.assertIsNone(parsed)
        self.assertIn("сумм", err.lower())

    def test_absurd_buyout_numbers_are_an_error(self):
        form = "рама: 1\nмотор: 2\nсрок: 05.08 - 12.08\nоплата: 3000\n"
        parsed, err = logic.parse_issue_form(
            form + "выкуп: 150000\nплатежей: 99999")
        self.assertIsNone(parsed)
        self.assertIn("платежей", err)
        parsed, err = logic.parse_issue_form(
            form + "выкуп: 99999999999\nплатежей: 120")
        self.assertIsNone(parsed)
        self.assertIn("выкуп", err)
        parsed, err = logic.parse_issue_form(
            form + "выкуп: сто тысяч\nплатежей: 120")
        self.assertIsNone(parsed)

    def test_money_formats_with_non_breaking_spaces(self):
        """Разряды разделяются неразрывным пробелом: перенос строки посреди
        «150 000» читается как другая цена."""
        self.assertEqual(logic.money(150000), "150\u00a0000\u00a0₽")
        self.assertEqual(logic.money(0), "0\u00a0₽")
        self.assertEqual(logic.money(None), "—")
