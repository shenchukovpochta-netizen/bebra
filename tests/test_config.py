"""Тесты конфигурации и маршрутизации апдейтов.

Каждый тест здесь закрывает конкретный дефект, найденный аудитом, - чтобы он
не вернулся при следующей правке.
    py -3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import os
import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import logic  # noqa: E402
from app.config import Config  # noqa: E402

BASE_ENV = {
    "BOT_TOKEN": "123:abc",
    "POSTGRES_PASSWORD": "pw",
    # 32 байта в base64: ключ шифрования анкеты обязателен, без него бот
    # не стартует - паспортным данным негде лежать.
    "PDN_KEY": base64.b64encode(b"k" * 32).decode(),
    "CHANNEL_ID": "-1001234567890",
    "ADMIN_CHAT_ID": "-1009876543210",
    "ADMINS": "111 222",
    "OFERTA_URL": "https://example.ru/oferta",
    "PDN_URL": "https://example.ru/pdn",
}


def load(**overrides) -> Config:
    env = {**BASE_ENV, **overrides}
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update({k: v for k, v in env.items() if v is not None})
    try:
        return Config.load()
    finally:
        os.environ.clear()
        os.environ.update(saved)


class TestEmptyEnvDefaults(unittest.TestCase):
    """docker compose подставляет "" для любого ${VAR}, которого нет в .env.

    Наивное os.environ.get(name, default) вернёт эту пустую строку вместо
    значения по умолчанию. Так пустыми уезжали версии оферты и политики -
    и версионирование согласия обесценивалось.
    """

    def test_empty_string_falls_back_to_default(self):
        cfg = load(OFERTA_VERSION="", PDN_VERSION="")
        self.assertEqual(cfg.oferta_version, "2026-01-15")
        self.assertEqual(cfg.pdn_version, "2026-01-15")

    def test_whitespace_is_also_empty(self):
        self.assertEqual(load(CHANNEL_URL="   ").channel_url, "https://t.me/mybike")

    def test_explicit_value_wins(self):
        self.assertEqual(load(OFERTA_VERSION="2027-01-01").oferta_version, "2027-01-01")

    def test_empty_channel_url_never_reaches_button(self):
        # Кнопка с url="" отвергается Telegram, и сообщение гейта не уходит вовсе
        self.assertTrue(load(CHANNEL_URL="").channel_url)


class TestPgParams(unittest.TestCase):
    """Пароль не должен разбираться как часть URL."""

    def test_password_with_slash_survives(self):
        # ровно то, что раньше генерировал bootstrap.sh
        pw = "q+siRqXOn9fhmJjmGWet1mnKpU/4jbE9"
        cfg = load(POSTGRES_PASSWORD=pw)
        self.assertEqual(cfg.pg["password"], pw)
        self.assertEqual(cfg.pg["host"], "postgres")

    def test_no_dsn_attribute_left(self):
        # Пока существует поле dsn, кто-нибудь снова соберёт строку руками
        self.assertFalse(hasattr(load(), "dsn"))

    def test_port_is_int(self):
        self.assertIsInstance(load(POSTGRES_PORT="6543").pg["port"], int)

    def test_dsn_approach_would_have_broken(self):
        """Фиксируем сам дефект: 40% паролей из base64 ломали DSN."""
        broken = 0
        for _ in range(500):
            pw = base64.b64encode(os.urandom(24)).decode()
            parsed = urlsplit(f"postgresql://bot:{pw}@postgres:5432/bot")
            try:
                if parsed.password != pw or parsed.hostname != "postgres":
                    broken += 1
            except ValueError:
                broken += 1
        self.assertGreater(broken, 100, "ожидали заметную долю сломанных DSN")


class TestOcrConfig(unittest.TestCase):
    def test_folder_id_required_when_enabled(self):
        with self.assertRaises(RuntimeError):
            load(OCR_API_KEY="key", OCR_ENABLED="1", OCR_FOLDER_ID="")

    def test_disabled_without_key(self):
        self.assertFalse(load(OCR_ENABLED="1").ocr_enabled)

    def test_enabled_with_key_and_folder(self):
        cfg = load(OCR_API_KEY="key", OCR_FOLDER_ID="b1gxxx")
        self.assertTrue(cfg.ocr_enabled)


class TestRequiredValues(unittest.TestCase):
    def test_missing_admins_raises(self):
        with self.assertRaises(RuntimeError):
            load(ADMINS="")

    def test_non_numeric_channel_id_raises(self):
        with self.assertRaises(RuntimeError):
            load(CHANNEL_ID="не число")

    def test_admins_parsed_from_spaces_and_commas(self):
        self.assertEqual(load(ADMINS="1, 2  3").admins, (1, 2, 3))


class TestUpdateRouting(unittest.TestCase):
    """Раньше здесь стояло голое «не private - отбросить», и кнопки модерации
    в групповом чате не доходили до обработчика вообще."""

    def test_private_always_passes(self):
        self.assertTrue(logic.should_process(
            "private", from_admin_chat=False, is_moderation_callback=False))

    def test_moderation_callback_in_admin_chat_passes(self):
        self.assertTrue(logic.should_process(
            "supergroup", from_admin_chat=True, is_moderation_callback=True))

    def test_group_chatter_is_dropped(self):
        self.assertFalse(logic.should_process(
            "supergroup", from_admin_chat=True, is_moderation_callback=False))

    def test_moderation_callback_from_foreign_chat_is_dropped(self):
        self.assertFalse(logic.should_process(
            "supergroup", from_admin_chat=False, is_moderation_callback=True))

    def test_channel_post_dropped(self):
        self.assertFalse(logic.should_process(
            "channel", from_admin_chat=False, is_moderation_callback=False))


class TestConsentVersion(unittest.TestCase):
    """Включение OCR обязано менять редакцию согласия: без OCR экран не
    называет обработчика, значит согласие было дано на другой набор условий."""

    def test_without_ocr(self):
        self.assertEqual(load(OFERTA_VERSION="2026-01-15").consent_version, "2026-01-15")

    def test_with_ocr_differs(self):
        cfg = load(OFERTA_VERSION="2026-01-15", OCR_API_KEY="k", OCR_FOLDER_ID="b1g")
        self.assertNotEqual(cfg.consent_version, "2026-01-15")
        self.assertIn("ocr", cfg.consent_version)

    def test_follows_oferta_version_by_default(self):
        self.assertEqual(load(OFERTA_VERSION="2027-03-01").consent_version, "2027-03-01")

    def test_explicit_pdn_version_wins(self):
        cfg = load(OFERTA_VERSION="2026-01-15", PDN_VERSION="2026-06-01")
        self.assertEqual(cfg.consent_version, "2026-06-01")


class TestKnownStates(unittest.TestCase):
    """Состояние, оставшееся от прошлой версии бота, не должно быть тупиком."""

    def test_removed_state_is_unknown(self):
        self.assertFalse(logic.is_known_state("wait_pdn"))

    def test_none_is_unknown(self):
        self.assertFalse(logic.is_known_state(None))

    def test_every_live_state_is_known(self):
        for state in (logic.NEW, logic.WAIT_FIO, logic.WAIT_OFERTA, logic.WAIT_CONTACT,
                      logic.WAIT_DOC, logic.WAIT_SELFIE, logic.CONFIRM,
                      logic.PENDING, logic.APPROVED):
            self.assertTrue(logic.is_known_state(state), state)

    def test_no_stale_state_left_in_set(self):
        self.assertNotIn("wait_pdn", logic.KNOWN_STATES)


class TestUploadValidation(unittest.TestCase):
    def test_photo_always_ok(self):
        self.assertTrue(logic.validate_upload(True, None, 1024).ok)

    def test_pdf_rejected(self):
        r = logic.validate_upload(False, "application/pdf", 1024)
        self.assertFalse(r.ok)
        self.assertIn("изображение", r.error)

    def test_image_document_accepted(self):
        self.assertTrue(logic.validate_upload(False, "image/png", 1024).ok)

    def test_mime_case_and_spaces(self):
        self.assertTrue(logic.validate_upload(False, " Image/JPEG ", 1024).ok)

    def test_oversized_rejected(self):
        r = logic.validate_upload(True, None, logic.MAX_UPLOAD_BYTES + 1)
        self.assertFalse(r.ok)

    def test_unknown_size_allowed(self):
        self.assertTrue(logic.validate_upload(True, None, None).ok)


if __name__ == "__main__":
    unittest.main()
