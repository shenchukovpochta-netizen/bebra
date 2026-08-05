"""Шифрование анкеты и сборка договора.

Шифрование требует cryptography, поэтому его часть пропускается там, где
библиотеки нет. Договор собирается голым stdlib: docx-шаблон заполняется
через zipfile и re.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import logic  # noqa: E402

from app.services import contract  # noqa: E402

try:
    from app.services.crypto import KeyProblem, Vault, generate_key, load_key
    HAVE_CRYPTO = True
except ImportError:                                    # pragma: no cover
    HAVE_CRYPTO = False

TEMPLATE = ROOT / "app" / "contract_template.docx"
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


def document_text(docx: bytes) -> str:
    """Видимый текст документа: содержимое всех <w:t> из word/document.xml."""
    import io
    import re
    import zipfile
    xml = zipfile.ZipFile(io.BytesIO(docx)).read("word/document.xml").decode("utf-8")
    return "".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", xml, re.S))


class TestTemplate(unittest.TestCase):
    def setUp(self):
        self.template = contract.load_template(TEMPLATE)

    def test_real_contract_landed(self):
        """Шаблон - настоящий docx договора ИП Галимзянова, а не рыба."""
        docx, _ = contract.build(TEMPLATE, make_ctx())
        text = document_text(docx).replace("\xa0", " ")
        for marker in ("Галимзянов", "165921923517", "324169000199701",
                       "150 000 (сто пятьдесят тысяч)",
                       "ПРАВИЛА ЭКСПЛУАТАЦИИ ЭЛЕКТРОВЕЛОСИПЕДА",
                       "АКТ ВОЗВРАТА"):
            self.assertIn(marker, text, marker)

    def test_every_placeholder_is_provided(self):
        """Опечатка в шаблоне не должна тихо выкидывать реквизит из договора."""
        used = contract.placeholders(self.template)
        provided = set(make_ctx()) | {contract.HASH_FIELD}
        self.assertEqual(used - provided, set())

    def test_unknown_placeholder_is_visible(self):
        rendered = contract.substitute("а {{ нетполя }} б", {})
        self.assertIn("нет поля", rendered)

    def test_values_are_xml_escaped(self):
        """Амперсанд в «кем выдан» не должен ломать XML документа."""
        ctx = make_ctx(passport_issuer="ОУФМС & Ко <тест>")
        docx, _ = contract.build(TEMPLATE, ctx)
        text = document_text(docx)
        self.assertIn("ОУФМС &amp; Ко &lt;тест&gt;", text)

    def test_all_anketa_fields_reach_the_document(self):
        docx, _ = contract.build(TEMPLATE, make_ctx())
        text = document_text(docx)
        for value in ANKETA.values():
            self.assertIn(value, text, value)
        self.assertIn(USER["full_name"], text)

    def test_minor_clause_rendered_for_minor_only(self):
        minor_docx, _ = contract.build(TEMPLATE, make_ctx(minor_clause=logic.MINOR_CLAUSE))
        adult_docx, _ = contract.build(TEMPLATE, make_ctx())
        self.assertIn("не достигший 18 лет", document_text(minor_docx))
        adult_text = document_text(adult_docx)
        self.assertNotIn("не достигший 18 лет", adult_text)
        # абзац удаляется целиком, а не оставляет пустой оговорки
        self.assertNotIn("minor_clause", adult_text)

    def test_hash_is_reproducible(self):
        _, digest = contract.build(TEMPLATE, make_ctx())
        _, digest2 = contract.build(TEMPLATE, make_ctx())
        self.assertEqual(digest, digest2)

    def test_hash_covers_the_data(self):
        _, digest = contract.build(TEMPLATE, make_ctx())
        _, changed = contract.build(TEMPLATE, make_ctx(passport_number="9999 999999"))
        self.assertNotEqual(digest, changed)

    def test_signed_copy_differs_from_issued(self):
        """Подписанный экземпляр несёт свой отпечаток: в нём проставлен
        момент подписания."""
        _, unsigned = contract.build(TEMPLATE, make_ctx())
        _, signed = contract.build(TEMPLATE, make_ctx(signed_at="02.08.2026 11:00 UTC"))
        self.assertNotEqual(unsigned, signed)

    def test_hash_verifiable_from_the_document(self):
        """Правило проверки задним числом: стереть напечатанный отпечаток
        из document.xml, посчитать SHA-256 заново - должно сойтись."""
        import hashlib
        import io
        import zipfile
        docx, digest = contract.build(TEMPLATE, make_ctx())
        xml = zipfile.ZipFile(io.BytesIO(docx)).read("word/document.xml").decode("utf-8")
        self.assertIn(digest, xml)
        recomputed = hashlib.sha256(xml.replace(digest, "").encode("utf-8")).hexdigest()
        self.assertEqual(digest, recomputed)


class TestDocx(unittest.TestCase):
    def test_builds_a_docx(self):
        docx, digest = contract.build(TEMPLATE, make_ctx())
        self.assertTrue(docx.startswith(b"PK"))
        self.assertEqual(len(digest), 64)
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(docx)) as zf:
            self.assertIsNone(zf.testzip())
            self.assertIn("word/document.xml", zf.namelist())
            self.assertIn("[Content_Types].xml", zf.namelist())

    def test_only_document_xml_changes(self):
        """Стили, шрифты и колонтитулы юриста копируются байт в байт."""
        import io
        import zipfile
        template = TEMPLATE.read_bytes()
        docx, _ = contract.build(TEMPLATE, make_ctx())
        with zipfile.ZipFile(io.BytesIO(template)) as src, \
                zipfile.ZipFile(io.BytesIO(docx)) as dst:
            self.assertEqual(sorted(src.namelist()), sorted(dst.namelist()))
            for name in src.namelist():
                if name == "word/document.xml":
                    continue
                self.assertEqual(src.read(name), dst.read(name), name)

    def test_long_values_do_not_break_build(self):
        ctx = make_ctx()
        ctx["passport_issuer"] = "ОТДЕЛОМ " + "ОЧЕНЬ ДЛИННОЕ НАЗВАНИЕ " * 8
        ctx["reg_address"] = "г. Казань, " + "ул. Очень Длинная, " * 10 + "д. 1"
        docx, _ = contract.build(TEMPLATE, ctx)
        self.assertTrue(docx.startswith(b"PK"))

    def test_missing_template_reports_clearly(self):
        with self.assertRaises(contract.TemplateProblem):
            contract.load_template(TEMPLATE.parent / "нет-такого.docx")

    def test_non_docx_template_reports_clearly(self):
        with self.assertRaises(contract.TemplateProblem):
            contract.load_template(ROOT / "README.md")


if __name__ == "__main__":
    unittest.main(verbosity=2)
