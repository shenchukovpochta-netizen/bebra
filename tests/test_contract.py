"""Шифрование анкеты и сборка договора.

Обе части требуют внешних библиотек (cryptography, fpdf2), поэтому набор
пропускается там, где их нет, - остальные тесты должны оставаться
запускаемыми на голом stdlib.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import logic  # noqa: E402

try:
    from app.services import contract
    HAVE_FPDF = True
except ImportError:                                    # pragma: no cover
    HAVE_FPDF = False

try:
    from app.services.crypto import KeyProblem, Vault, generate_key, load_key
    HAVE_CRYPTO = True
except ImportError:                                    # pragma: no cover
    HAVE_CRYPTO = False

TEMPLATE = ROOT / "app" / "contract_template.md"
TODAY = date(2026, 8, 2)

ANKETA = {
    "birth_date": "07.03.1990",
    "birth_place": "гор. Казань",
    "passport_number": "1234 567890",
    "passport_date": "01.02.2015",
    "passport_code": "160-002",
    "passport_issuer": "ОУФМС России по Респ. Татарстан",
    "reg_address": "г. Казань, ул. Баумана, д. 1, кв. 2",
    "live_address": "г. Казань, ул. Кремлёвская, д. 5, кв. 9",
    "phone2": "+79001112233",
    "phone3": "+79004445566",
}
USER = {"tg_id": 5001, "full_name": "Иванов Иван Иванович", "phone": "+79990000000"}


def make_ctx(**extra) -> dict:
    ctx = logic.contract_context(USER, ANKETA, number="АВ-2026-000042", today=TODAY)
    ctx.update(purge_days="90", signed_at="не подписан")
    ctx.update(extra)
    return ctx


@unittest.skipUnless(HAVE_CRYPTO, "cryptography не установлена")
class TestVault(unittest.TestCase):
    def setUp(self):
        self.vault = Vault.from_raw(generate_key())

    def test_roundtrip(self):
        self.assertEqual(self.vault.decrypt(self.vault.encrypt(ANKETA)), ANKETA)

    def test_ciphertext_hides_the_data(self):
        token = self.vault.encrypt(ANKETA)
        for secret in ("1234 567890", "Баумана", "160-002", "Иванов"):
            self.assertNotIn(secret, token, secret)

    def test_empty_anketa_is_null(self):
        """Пустая анкета должна выглядеть в базе как NULL, а не как валидный
        шифротекст - иначе «анкеты нет» и «анкета пустая» не различить."""
        self.assertIsNone(self.vault.encrypt({}))
        self.assertIsNone(self.vault.encrypt(None))
        self.assertEqual(self.vault.decrypt(None), {})

    def test_nonce_differs_between_calls(self):
        self.assertNotEqual(self.vault.encrypt(ANKETA), self.vault.encrypt(ANKETA))

    def test_wrong_key_does_not_raise(self):
        """Разные ключи на двух запусках не должны блокировать человеку
        вообще любое действие, включая /start."""
        token = self.vault.encrypt(ANKETA)
        other = Vault.from_raw(generate_key())
        self.assertEqual(other.decrypt(token), {})

    def test_tampered_ciphertext_detected(self):
        token = self.vault.encrypt(ANKETA)
        broken = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        self.assertEqual(self.vault.decrypt(broken), {})

    def test_unknown_format_ignored(self):
        self.assertEqual(self.vault.decrypt("что-то из прошлой версии"), {})

    def test_key_accepts_base64_and_hex(self):
        self.assertEqual(len(load_key(generate_key())), 32)
        self.assertEqual(len(load_key("ab" * 32)), 32)

    def test_missing_or_short_key_refused(self):
        for raw in ("", None, "короткий"):
            with self.assertRaises(KeyProblem):
                load_key(raw)


@unittest.skipUnless(HAVE_FPDF, "fpdf2 не установлена")
class TestTemplate(unittest.TestCase):
    def setUp(self):
        self.template = contract.load_template(TEMPLATE)

    def test_comments_are_not_printed(self):
        body = contract.strip_comments(self.template)
        self.assertNotIn("ЗАМЕНИТЕ ЭТОТ РАЗДЕЛ", body)
        self.assertNotIn("{{ contract_number }}  номер договора", body)

    def test_section_headings_survive_comment_stripping(self):
        """Регрессия: комментарий «#» съедал и заголовки «## », и договор
        собирался вообще без названий разделов."""
        body = contract.strip_comments(self.template)
        for heading in ("## 1. Стороны", "## 5. Обработка персональных данных"):
            self.assertIn(heading, body)

    def test_every_placeholder_is_provided(self):
        """Опечатка в шаблоне не должна тихо выкидывать реквизит из договора."""
        body = contract.strip_comments(self.template)
        used = set(contract.PLACEHOLDER.findall(body))
        provided = set(make_ctx()) | {contract.HASH_FIELD}
        self.assertEqual(used - provided, set())

    def test_unknown_placeholder_is_visible(self):
        rendered = contract.substitute("а {{ нетполя }} б", {})
        self.assertIn("нет поля", rendered)

    def test_all_anketa_fields_reach_the_document(self):
        text, _ = contract.render_text(self.template, make_ctx())
        for value in ANKETA.values():
            self.assertIn(value, text, value)
        self.assertIn(USER["full_name"], text)

    def test_hash_is_reproducible(self):
        text, digest = contract.render_text(self.template, make_ctx())
        again, digest2 = contract.render_text(self.template, make_ctx())
        self.assertEqual(digest, digest2)
        self.assertIn(digest, text)

    def test_hash_covers_the_data(self):
        _, digest = contract.render_text(self.template, make_ctx())
        other = make_ctx()
        other["passport_number"] = "9999 999999"
        _, changed = contract.render_text(self.template, other)
        self.assertNotEqual(digest, changed)

    def test_signed_copy_differs_from_issued(self):
        """Подписанный экземпляр несёт свой отпечаток: в нём проставлен
        момент подписания."""
        _, unsigned = contract.render_text(self.template, make_ctx())
        _, signed = contract.render_text(
            self.template, make_ctx(signed_at="02.08.2026 11:00 UTC"))
        self.assertNotEqual(unsigned, signed)


@unittest.skipUnless(HAVE_FPDF, "fpdf2 не установлена")
class TestPdf(unittest.TestCase):
    def test_builds_a_pdf(self):
        pdf, digest = contract.build(TEMPLATE, make_ctx())
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(len(digest), 64)

    def test_long_values_do_not_break_layout(self):
        """Регрессия: строка с длинным пробельным отбивом уходила
        в выравнивание по ширине и роняла сборку целиком."""
        ctx = make_ctx()
        ctx["passport_issuer"] = "ОТДЕЛОМ " + "ОЧЕНЬ ДЛИННОЕ НАЗВАНИЕ " * 8
        ctx["reg_address"] = "г. Казань, " + "ул. Очень Длинная, " * 10 + "д. 1"
        pdf, _ = contract.build(TEMPLATE, ctx)
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_missing_template_reports_clearly(self):
        with self.assertRaises(contract.TemplateProblem):
            contract.load_template(TEMPLATE.parent / "нет-такого.md")


if __name__ == "__main__":
    unittest.main(verbosity=2)
