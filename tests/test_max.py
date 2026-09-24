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


class TestMaxConfig(unittest.TestCase):
    """MAX-бот собирает Config руками, поле за полем.

    Новое обязательное поле в общем Config ломает его молча: бот падает
    на старте, а узнать об этом можно только по логам сервера. Этот тест
    и есть та самая проверка.
    """

    ENV = {
        "MAX_BOT_TOKEN": "max-token",
        "MAX_CHANNEL_ID": "-100500", "MAX_ADMIN_CHAT_ID": "-100501",
        "MAX_ADMINS": "111", "POSTGRES_PASSWORD": "pw", "PDN_KEY": "k" * 44,
        "OFERTA_URL": "https://example.ru/oferta",
    }

    def test_load_config_builds_with_minimal_env(self):
        import os

        from app import max_main

        saved = {k: os.environ.get(k) for k in self.ENV}
        os.environ.update(self.ENV)
        try:
            cfg = max_main.load_config()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(cfg.contract_prefix, "АВМ")
        self.assertEqual(cfg.pg["database"], "mybike_max")

    def test_load_config_without_oferta_url(self):
        """OFERTA_URL в .env.example пустой и у Telegram-бота необязателен -
        бот в MAX не должен падать на старте без него."""
        import os

        from app import max_main

        env = {k: v for k, v in self.ENV.items() if k != "OFERTA_URL"}
        saved = {k: os.environ.get(k) for k in self.ENV}
        os.environ.update(env)
        os.environ.pop("OFERTA_URL", None)
        try:
            cfg = max_main.load_config()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertFalse(cfg.oferta_url)


class _FakeMax:
    def __init__(self):
        self.sent = []

    async def send(self, *, user_id=None, chat_id=None, text, keyboard=None,
                   attachments=None, fmt="html", reply_to_mid=None):
        self.sent.append({"to": user_id or chat_id, "text": text, "kb": keyboard})
        return {"message": {"body": {"mid": f"mid.{len(self.sent)}"}}}

    async def answer_callback(self, cid, notification=None):
        self.sent.append({"to": "cb", "text": notification})

    async def upload_file(self, name, data):
        return {"type": "file", "payload": {"token": "f"}}


class TestMaxStart(unittest.IsolatedAsyncioTestCase):
    """/start в MAX - те же ветки, что в Telegram: отказ продолжается с
    шага, заявка на проверке не заполняется заново, договор на подписи
    приходит снова, а не меню, кнопки которого не работают."""

    async def asyncSetUp(self):
        from app.max.handlers import Ctx
        from app.services.crypto import Vault
        from tests import test_flow as tf
        self.cfg = tf.make_config()
        self.db = tf.FakeDB()
        self.cl = _FakeMax()
        self.ctx = Ctx(self.cl, self.db, self.cfg, Vault.from_raw(self.cfg.pdn_key), crm=None)

    def user(self, **over):
        row = {"tg_id": 42, "username": "u", "state": logic.WAIT_FIO,
               "status": logic.ST_NEW, "anketa_enc": None, "full_name": "Иванов Иван"}
        row.update(over)
        self.db.users[42] = row
        return row

    async def start(self, user):
        from app.max import handlers
        await handlers.start(self.ctx, user)
        return self.db.users[42]["state"], self.cl.sent[-1]["text"]

    async def test_rejected_continues_from_its_step(self):
        state, text = await self.start(self.user(state=logic.WAIT_REG_ADDR,
                                                 status=logic.ST_REJECTED))
        self.assertEqual(state, logic.WAIT_REG_ADDR)
        self.assertIn("Адрес регистрации", text)

    async def test_pending_is_not_reset(self):
        state, _ = await self.start(self.user(state=logic.PENDING,
                                              status=logic.ST_PENDING))
        self.assertEqual(state, logic.PENDING)

    async def test_new_user_starts_over(self):
        state, _ = await self.start(self.user(state=logic.NEW, status=logic.ST_NEW))
        self.assertEqual(state, logic.WAIT_FIO)


# Ключ переписки «Входящих» постоянный: тесты не зависят от случайности.
INBOX_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
MAX_USER, MODERATOR = 42, 111


class MaxInboxCase(unittest.IsolatedAsyncioTestCase):
    """Обработчики MAX напрямую, с мостом в CRM из make_crm()."""

    def make_crm(self):
        from tests.fake_crm import FakeCrm
        return FakeCrm()

    async def asyncSetUp(self):
        from app.max.handlers import Ctx
        from app.services.crypto import Vault
        from tests import test_flow as tf
        self.tf = tf
        self.cfg = tf.make_config(inbox_key=INBOX_KEY)
        self.db = tf.FakeDB()
        self.cl = _FakeMax()
        self.crm = self.make_crm()
        self.vault = Vault.from_raw(self.cfg.pdn_key)
        self.ctx = Ctx(self.cl, self.db, self.cfg, self.vault, crm=self.crm)

    def user(self, **over):
        row = {"tg_id": MAX_USER, "username": "u42", "state": logic.APPROVED,
               "status": logic.ST_APPROVED, "anketa_enc": None,
               "full_name": "Иванов Иван", "phone": "+79991571094"}
        row.update(over)
        self.db.users[MAX_USER] = row
        return dict(row)

    def confirming_user(self):
        """Анкета заполнена, документ загружен - шаг «Всё верно»."""
        anketa = {step.field: self.tf.ANSWERS_BY_FIELD[step.field]
                  for step in logic.anketa_steps(None)}
        return self.user(state=logic.CONFIRM, status=logic.ST_NEW,
                         doc_file_id="img-token", anketa_enc=self.vault.encrypt(anketa))

    async def ask(self, question):
        from app.max import handlers
        await handlers.st_support(self.ctx, self.user(state=logic.WAIT_SUPPORT), question)

    async def answer(self, text):
        from app.max import handlers
        mid = self.db.users[MAX_USER]["support_message_id"]
        self.assertIsNotNone(mid, "карточка вопроса не привязана")
        await handlers.mod_reply(self.ctx, MODERATOR, self.cfg.admin_chat_id, mid, text)

    async def confirm(self):
        from app.max import handlers
        await handlers.cb_confirm(self.ctx, self.confirming_user(), "cb-1")

    def sent_to(self, to):
        return [m["text"] or "" for m in self.cl.sent if m["to"] == to]

    def plain(self, body_enc):
        from app.crm import service
        return service.inbox_open(service.inbox_vault(INBOX_KEY), body_enc)

    async def only_thread(self):
        threads = await self.crm.inbox_threads()
        self.assertEqual(len(threads), 1, threads)
        return threads[0], await self.crm.inbox_messages(threads[0]["id"])


class TestMaxInbox(MaxInboxCase):
    """Вопрос в поддержку, ответ модератора и отправка анкеты в MAX попадают
    во «Входящие» основной базы каналом max. Сигнала в чат нет: карточка
    уже ушла туда сама."""

    async def test_support_question_opens_a_waiting_thread(self):
        client_id = await self.crm.create_client(full_name="Иванов Иван",
                                                 phone="+79990000000")
        await self.crm.link_client_max("+79990000000", MAX_USER)
        await self.ask("Можно продлить аренду на неделю?")
        self.assertEqual(self.db.users[MAX_USER]["state"], logic.APPROVED)
        self.assertTrue(any("Можно продлить аренду на неделю?" in t
                            for t in self.sent_to(self.cfg.admin_chat_id)))
        thread, messages = await self.only_thread()
        self.assertEqual((thread["channel"], thread["origin"], thread["ext_id"]),
                         ("max", "max_bot", str(MAX_USER)))
        self.assertEqual(thread["client_id"], client_id,
                         "карточка найдена по аккаунту MAX, а не по телефону")
        self.assertEqual((thread["name"], thread["username"], thread["phone"]),
                         ("Иванов Иван", "u42", "+79991571094"))
        self.assertEqual(thread["status"], "new")
        self.assertIsNotNone(thread["waiting_since"])
        self.assertIsNotNone(thread["announced_at"], "сигнал в чат - сама карточка")
        self.assertEqual(await self.crm.inbox_to_announce(), [])
        self.assertEqual(len(messages), 1)
        m = messages[0]
        self.assertEqual((m["direction"], m["kind"]), ("in", "text"))
        self.assertEqual(self.plain(m["body_enc"]), "Можно продлить аренду на неделю?")
        self.assertNotIn("продлить", m["body_enc"], "текст хранится зашифрованным")
        self.assertEqual(m["ext_id"], self.db.users[MAX_USER]["support_message_id"],
                         "mid карточки - защита от дубля")

    async def test_moderator_reply_is_recorded_as_out(self):
        await self.ask("Где зарядить аккумулятор?")
        await self.answer("На Павлюхина, с 10 до 19")
        self.assertTrue(any("На Павлюхина, с 10 до 19" in t
                            for t in self.sent_to(MAX_USER)))
        thread, messages = await self.only_thread()
        self.assertEqual([m["direction"] for m in messages], ["in", "out"])
        out = messages[-1]
        self.assertEqual(self.plain(out["body_enc"]), "На Павлюхина, с 10 до 19")
        self.assertEqual(out["status"], "sent")
        self.assertEqual(out["author"], f"max:{MODERATOR}")
        self.assertIsNone(thread["waiting_since"], "ответ из чата снимает ожидание")
        self.assertEqual(thread["status"], "work")

    async def test_not_admin_reply_records_nothing(self):
        from app.max import handlers
        await self.ask("Вопрос")
        mid = self.db.users[MAX_USER]["support_message_id"]
        await handlers.mod_reply(self.ctx, 999, self.cfg.admin_chat_id, mid, "чужой ответ")
        _, messages = await self.only_thread()
        self.assertEqual([m["direction"] for m in messages], ["in"])

    async def test_anketa_submission_is_an_event(self):
        await self.confirm()
        row = self.db.users[MAX_USER]
        self.assertEqual(row["state"], logic.PENDING)
        self.assertIsNotNone(row.get("mod_message_id"), "карточка анкеты ушла")
        thread, messages = await self.only_thread()
        self.assertEqual((thread["channel"], thread["origin"], thread["ext_id"]),
                         ("max", "max_bot", str(MAX_USER)))
        self.assertEqual(len(messages), 1)
        event = messages[0]
        self.assertEqual((event["direction"], event["kind"]), ("event", "other"))
        self.assertEqual(self.plain(event["body_enc"]), "Анкета отправлена на проверку")
        self.assertTrue(str(event["ext_id"]).startswith("anketa:"))
        self.assertIsNone(thread["waiting_since"], "отметка - не вопрос")
        self.assertIsNotNone(thread["announced_at"])


class _MaxFlowSurvivesInbox:
    """Без моста в CRM или при сбое записи сценарий MAX идёт как раньше."""

    async def test_support_question_still_reaches_moderators(self):
        from app import texts
        await self.ask("Когда можно подъехать?")
        row = self.db.users[MAX_USER]
        self.assertEqual(row["state"], logic.APPROVED)
        self.assertIsNotNone(row["support_message_id"])
        self.assertTrue(any("Когда можно подъехать?" in t
                            for t in self.sent_to(self.cfg.admin_chat_id)))
        self.assertIn(texts.SUPPORT_SENT, self.sent_to(MAX_USER))

    async def test_support_reply_still_reaches_user(self):
        from app import texts
        await self.ask("Вопрос про тормоза")
        await self.answer("Подтяните трос")
        self.assertTrue(any("Подтяните трос" in t for t in self.sent_to(MAX_USER)))
        self.assertIn(texts.SUPPORT_REPLIED, self.sent_to(self.cfg.admin_chat_id))

    async def test_anketa_submission_still_goes_to_moderation(self):
        from app import texts
        await self.confirm()
        row = self.db.users[MAX_USER]
        self.assertEqual(row["state"], logic.PENDING)
        self.assertIsNotNone(row.get("mod_message_id"))
        self.assertIn(texts.SUBMITTED, self.sent_to(MAX_USER))
        self.assertNotIn(texts.SUBMIT_PROBLEM, self.sent_to(MAX_USER))


class TestMaxInboxWithoutCrm(_MaxFlowSurvivesInbox, MaxInboxCase):
    def make_crm(self):
        return None


class TestMaxInboxWhenRecordFails(_MaxFlowSurvivesInbox, MaxInboxCase):
    def make_crm(self):
        from tests.test_flow import FailingInboxCrm
        return FailingInboxCrm()

    async def test_failure_is_logged_without_text(self):
        with self.assertLogs("app.crm.inbox", "WARNING") as logs:
            await self.ask("Мой адрес Баумана 1, заберите велосипед")
        joined = "\n".join(logs.output)
        self.assertIn(f"max/{MAX_USER}", joined)
        for secret in ("Баумана", "Иванов", "+7999"):
            self.assertNotIn(secret, joined)
        self.assertEqual(await self.crm.inbox_threads(), [])
