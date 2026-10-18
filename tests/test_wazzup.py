"""Wazzup: клиент API, правила, отправка ответа из очереди, ежечасная
сверка каналов и подписки, хук с токеном в адресе и панель. Каналы -
WhatsApp, Telegram-аккаунт, Авито, Instagram, ВКонтакте, Viber; заявки -
«создать контакт / сделку» из окна чата Wazzup (createContact, createDeal).

Сети нет: клиенту подменена сессия, процессу бота - клиент целиком.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic, readiness  # noqa: E402

try:
    from app.crm import inbox, service
    from app.services import wazzup as wz
    from app.services.crypto import Vault, generate_key
    from tests.fake_crm import FakeCrm
    HAVE_DEPS = True
except ImportError:                                    # pragma: no cover
    HAVE_DEPS = False

try:
    import test_inbox_web as tiw
    HAVE_WEB = tiw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

KEY = generate_key() if HAVE_DEPS else ""
CH1 = "b96a999e-06f5-4cac-8413-ba999993f981"
CH2 = "0f0e0d0c-0b0a-4909-8807-060504030201"
NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)


def state(**over):
    value = {"ok": True, "at": NOW.isoformat(), "error": "",
             "channels": [{"id": CH1, "phone": "+79991110000", "state": "active",
                           "active": True}],
             "hook": "abc", "hooked_at": NOW.isoformat(), "hook_error": ""}
    value.update(over)
    return {"inbox_wazzup_state": json.dumps(value)}


# ───────────────────────────── клиент API ─────────────────────────────


class FakeResponse:
    def __init__(self, status, payload):
        self.status, self.payload = status, payload

    async def json(self, content_type=None):
        return self.payload


class FakeServer:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def factory(self):
        server = self

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def request(self, method, url, **kwargs):
                server.calls.append((method, url, kwargs))
                status, payload = server.replies.pop(0)
                return FakeResponse(status, payload)
        return Session()


@unittest.skipUnless(HAVE_DEPS, "нет зависимостей")
class TestClient(unittest.IsolatedAsyncioTestCase):
    def client(self, *replies):
        self.server = FakeServer(replies)
        return wz.WazzupClient(api_key="k-1", session_factory=self.server.factory)

    async def test_send_text(self):
        client = self.client((201, {"messageId": "wz-1", "chatId": "79001234567"}))
        got = await client.send_text(CH1, "+7 900 123-45-67", "Свободен, приезжайте",
                                     crm_message_id="mybike-inbox-5")
        self.assertEqual(got["messageId"], "wz-1")
        method, url, kwargs = self.server.calls[0]
        self.assertEqual((method, url), ("POST", "https://api.wazzup24.com/v3/message"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer k-1")
        self.assertEqual(kwargs["json"], {
            "channelId": CH1, "chatType": "whatsapp", "chatId": "79001234567",
            "text": "Свободен, приезжайте", "crmMessageId": "mybike-inbox-5"})

    async def test_repeat_is_already_sent(self):
        client = self.client((400, {"error": "repeatedCrmMessageId",
                                    "description": "You have already sent message"}))
        self.assertEqual(await client.send_text(CH1, "+79001234567", "Да",
                                                crm_message_id="x"), {"repeated": True})

    async def test_errors(self):
        for status, payload, words in ((401, {}, "ключ"), (429, {}, "реже"),
                                       (400, {"error": "chatNotFound",
                                              "description": "Chat not found"},
                                        "Chat not found")):
            with self.subTest(status=status):
                client = self.client((status, payload))
                with self.assertRaises(wz.WazzupError) as err:
                    await client.send_text(CH1, "+79001234567", "Да", crm_message_id="x")
                self.assertIn(words, str(err.exception))
        client = self.client()
        for channel, phone, text in ((CH1, "", "Да"), ("../x", "+79001234567", "Да"),
                                     (CH1, "+79001234567", "  ")):
            with self.assertRaises(wz.WazzupError):
                await client.send_text(channel, phone, text, crm_message_id="x")
        self.assertEqual(self.server.calls, [], "негодное не уходит в сеть")

    async def test_channels_and_webhook(self):
        client = self.client((200, [
            {"channelId": CH1, "transport": "whatsapp", "plainId": "79991110000",
             "state": "active"},
            {"channelId": CH2, "transport": "wapi", "plainId": "79992220000",
             "state": "phoneUnavailable"},
            {"channelId": "tg-1", "transport": "telegram", "plainId": "x", "state": "active"},
            "мусор"]), (200, None))
        channels = await client.channels()
        self.assertEqual([(c["id"], c["kind"], c["phone"], c["active"]) for c in channels],
                         [(CH1, "wa", "+79991110000", True), (CH2, "wa", "+79992220000", False),
                          ("tg-1", "tgp", None, True)])
        await client.set_webhook("https://crm.example.ru/hook/inbox/t")
        method, url, kwargs = self.server.calls[1]
        self.assertEqual((method, url), ("PATCH", "https://api.wazzup24.com/v3/webhooks"))
        self.assertEqual(kwargs["json"]["webhooksUri"], "https://crm.example.ru/hook/inbox/t")
        self.assertTrue(kwargs["json"]["subscriptions"]["messagesAndStatuses"])
        self.assertIs(kwargs["json"]["subscriptions"]["contactsAndDealsCreation"], True,
                      "заявки из окна чата Wazzup идут в воронку")

    async def test_all_channel_kinds(self):
        """Instagram, ВКонтакте и Viber - свои каналы «Входящих»; номер
        телефона - только где plainId телефон; незнакомое - мимо."""
        client = self.client((200, [
            {"channelId": "ig-1", "transport": "instagram", "plainId": "mybike_kzn",
             "state": "active"},
            {"channelId": "vk-1", "transport": "vk", "plainId": "219876543",
             "state": "active"},
            {"channelId": "vb-1", "transport": "viber", "plainId": "79993330000",
             "state": "active"},
            {"channelId": "av-1", "transport": "avito", "plainId": "123456789012",
             "state": "active"},
            {"channelId": "mx-1", "transport": "max", "plainId": "1", "state": "active"},
            {"channelId": "x/y", "transport": "vk", "state": "active"}]))
        got = [(c["id"], c["kind"], c["phone"]) for c in await client.channels()]
        self.assertEqual(got, [("ig-1", "ig", None), ("vk-1", "vk", None),
                               ("vb-1", "viber", "+79993330000"), ("av-1", "avito", None)])

    async def test_send_to_new_channels(self):
        for kind, chat_type, chat in (("ig", "instagram", "anna.kzn"),
                                      ("vk", "vk", "123456789"),
                                      ("viber", "viber", "uX9+ab/c==")):
            with self.subTest(kind=kind):
                client = self.client((201, {"messageId": f"m-{kind}"}))
                got = await client.send_text("ch-1", chat, "Свободен", crm_message_id="c-1",
                                             kind=kind)
                self.assertEqual(got["messageId"], f"m-{kind}")
                body = self.server.calls[0][2]["json"]
                self.assertEqual((body["chatType"], body["chatId"], body["channelId"]),
                                 (chat_type, chat, "ch-1"))
        client = self.client()
        for kind, chat in (("ig", "анна"), ("vk", ""), ("tg", "5001"), ("max", "5001")):
            with self.assertRaises(wz.WazzupError, msg=kind):
                await client.send_text("ch-1", chat, "Да", crm_message_id="x", kind=kind)
        self.assertEqual(self.server.calls, [], "бот Telegram и MAX - не через Wazzup")


# ───────────────────────────── правила ─────────────────────────────


class TestRules(unittest.TestCase):
    def test_pick_channel_by_kind(self):
        channels = [{"id": "wa-1", "kind": "wa", "active": True},
                    {"id": "tg-1", "kind": "tgp", "active": True},
                    {"id": "av-1", "kind": "avito", "active": True}]
        self.assertEqual(logic.wazzup_pick_channel(None, channels), "wa-1")
        self.assertEqual(logic.wazzup_pick_channel(None, channels, kind="tgp"), "tg-1")
        self.assertEqual(logic.wazzup_pick_channel("av-2", channels, kind="avito"), "av-2")
        live = {"live": True, "channels": channels}
        thread = {"channel": "tgp", "origin": "hook", "ext_id": "5001",
                  "ext_channel": "tg-1", "status": "new"}
        self.assertEqual(logic.inbox_can_reply(thread, avito_ok=False, wa=live), (True, ""))
        self.assertFalse(logic.inbox_can_reply({**thread, "ext_channel": None},
                                               avito_ok=False, wa=live)[0])
        avito = {**thread, "channel": "avito", "ext_channel": "av-1"}
        self.assertEqual(logic.inbox_can_reply(avito, avito_ok=False, wa=live), (True, ""),
                         "Авито из Wazzup отвечает Wazzup, опрос Авито не нужен")
        self.assertFalse(logic.inbox_can_reply(avito, avito_ok=True, wa=None)[0])

    def test_hook_url(self):
        self.assertEqual(logic.wazzup_hook_url("https://CRM.mybike.ru/", "ab-12"),
                         "https://crm.mybike.ru/hook/inbox/ab-12")
        for domain, token in (("", "t"), ("crm.ru", ""), ("crm.ru/x", "t"),
                              ("crm.ru", "t/../x"), ("crm.ru", "a" * 300)):
            self.assertIsNone(logic.wazzup_hook_url(domain, token), (domain, token))

    def test_state(self):
        live = logic.wazzup_state(state(), now=NOW + timedelta(hours=1))
        self.assertTrue(live["live"] and live["hook"])
        self.assertEqual([c["id"] for c in live["channels"]], [CH1])
        stale = logic.wazzup_state(state(), now=NOW + timedelta(hours=4))
        self.assertFalse(stale["live"], "бот молчит три часа - ответ повиснет")
        broken = logic.wazzup_state(state(hook_error="нет домена"), now=NOW)
        self.assertTrue(broken["live"])
        self.assertFalse(broken["hook"])
        self.assertFalse(logic.wazzup_state({}, now=NOW)["configured"])
        junk = logic.wazzup_state({"inbox_wazzup_state": "{не json"}, now=NOW)
        self.assertFalse(junk["configured"])

    def test_pick_channel(self):
        one = [{"id": CH1, "active": True}]
        two = one + [{"id": CH2, "active": True}]
        self.assertEqual(logic.wazzup_pick_channel(CH2, one), CH2, "свой номер главнее")
        self.assertEqual(logic.wazzup_pick_channel(None, one), CH1)
        self.assertIsNone(logic.wazzup_pick_channel(None, two), "два номера - не гадаем")
        self.assertIsNone(logic.wazzup_pick_channel(None, [{"id": CH1, "active": False}]))

    def test_can_reply(self):
        thread = {"channel": "wa", "origin": "hook", "ext_id": "+79001234567",
                  "phone": "+79001234567", "status": "new"}
        wa = logic.wazzup_state(state(), now=datetime.now(UTC) - timedelta(minutes=1))
        wa["live"] = True
        self.assertEqual(logic.inbox_can_reply(thread, avito_ok=False, wa=wa), (True, ""))
        ok, why = logic.inbox_can_reply(thread, avito_ok=False)
        self.assertFalse(ok)
        self.assertIn("wa.me", why)
        two = {**wa, "channels": [{"id": CH1, "active": True}, {"id": CH2, "active": True}]}
        ok, why = logic.inbox_can_reply(thread, avito_ok=False, wa=two)
        self.assertFalse(ok)
        self.assertIn("несколько номеров", why)
        self.assertTrue(logic.inbox_can_reply({**thread, "ext_channel": CH2},
                                              avito_ok=False, wa=two)[0])
        self.assertFalse(logic.inbox_can_reply({**thread, "status": "spam"},
                                               avito_ok=False, wa=wa)[0])

    def test_webhook_carries_channel(self):
        items, skipped = logic.parse_inbound({"messages": [
            {"messageId": "m1", "channelId": CH1, "chatType": "whatsapp",
             "chatId": "79001234567", "type": "text", "text": "Есть свободные?",
             "isEcho": False, "contact": {"name": "Азиз"}},
            {"messageId": "m2", "channelId": CH1, "chatType": "whatsapp",
             "chatId": "79001234567", "type": "text", "text": "наш ответ", "isEcho": True},
            {"messageId": "m3", "channelId": "../evil", "chatType": "whatsapp",
             "chatId": "79005550000", "type": "text", "text": "Привет"}]})
        self.assertEqual(skipped, 1, "своё исходящее - не обращение")
        self.assertEqual([(i["ext_id"], i.get("ext_channel")) for i in items],
                         [("+79001234567", CH1), ("+79005550000", None)])

    def test_foreign_number_stays_foreign(self):
        """chatId WhatsApp - международный: «84…» - Вьетнам, а не «8» набора РФ.
        Перевод в +7 отдал бы ответ чужому человеку."""
        [item], _ = logic.parse_inbound({"messages": [
            {"messageId": "m9", "channelId": CH1, "chatType": "whatsapp",
             "chatId": "84912345678", "type": "text", "text": "Xin chào"}]})
        self.assertEqual((item["ext_id"], item["phone"]), ("+84912345678", "+84912345678"))
        self.assertEqual(logic.inbox_phone("wa", "+84912345678"), "+84912345678")
        self.assertEqual(logic.inbox_phone("wa", "89001234567"), "+79001234567",
                         "набранное руками (n8n) - как везде")
        self.assertEqual(logic.inbox_links({"channel": "wa", "phone": "+84912345678"})["wa"],
                         "https://wa.me/84912345678")
        self.assertEqual(wz.chat_id("+84912345678"), "84912345678")

    def test_new_channels_from_webhook(self):
        """Instagram, ВКонтакте и Viber - свои каналы; группы, Telegram и MAX
        бота хуком не заводятся."""
        def msg(chat_type, chat, **extra):
            return {"messageId": f"m-{chat_type}-{chat}", "channelId": CH2,
                    "chatType": chat_type, "chatId": chat, "type": "text",
                    "text": "Привет", "isEcho": False, **extra}
        items, skipped = logic.parse_inbound({"messages": [
            msg("instagram", "anna.kzn", contact={"name": "Анна", "username": "anna.kzn"}),
            msg("vk", "123456789", contact={"name": "Пётр", "phone": "89001112233"}),
            msg("viber", "uX9+ab/c==", contact={"name": "Ильдар"}),
            msg("Instagram", "UPPER.case"),
            msg("whatsgroup", "120363@g.us"), msg("telegroup", "-100123"),
            msg("vkgroup", "-1"), msg("max", "5001"), msg("tg", "5001"), msg("", "1")]})
        self.assertEqual(skipped, 6, "группы, MAX, Telegram бота и пустой тип - мимо")
        self.assertEqual([(i["channel"], i["ext_id"], i.get("ext_channel")) for i in items],
                         [("ig", "anna.kzn", CH2), ("vk", "123456789", CH2),
                          ("viber", "uX9+ab/c==", CH2), ("ig", "UPPER.case", CH2)])
        ig, vk, viber, _ = items
        self.assertEqual(ig["username"], "anna.kzn")
        self.assertEqual(vk["phone"], "+79001112233", "телефон - только из контакта")
        self.assertIsNone(viber["phone"])
        for channel in ("tg", "max"):
            got, skipped = logic.parse_inbound({"channel": channel, "ext_id": "5001",
                                                "text": "x"})
            self.assertEqual((got, skipped), ([], 1), channel)
        got, _ = logic.parse_inbound({"channel": "vk", "ext_id": "42", "text": "x"})
        self.assertEqual(got[0]["channel"], "vk", "n8n может прислать и ВКонтакте")

    def test_new_channels_reply_rules(self):
        channels = [{"id": "ig-1", "kind": "ig", "active": True},
                    {"id": "vk-1", "kind": "vk", "active": True}]
        live = {"live": True, "channels": channels}
        for channel, own in (("ig", "ig-1"), ("vk", "vk-1"), ("viber", "vb-1")):
            with self.subTest(channel=channel):
                thread = {"channel": channel, "origin": "hook", "ext_id": "c-1",
                          "ext_channel": own, "status": "new"}
                self.assertEqual(logic.inbox_can_reply(thread, avito_ok=False, wa=live),
                                 (True, ""))
                ok, why = logic.inbox_can_reply({**thread, "ext_channel": None},
                                                avito_ok=False, wa=live)
                self.assertFalse(ok)
                self.assertIn("без канала Wazzup", why)
                self.assertFalse(logic.inbox_can_reply(thread, avito_ok=False, wa=None)[0])
        self.assertEqual(logic.INBOX_REPLY_LIMITS["ig"], 1000)
        self.assertFalse(logic.check_inbox_reply("ig", "я" * 1001).ok)
        self.assertTrue(logic.check_inbox_reply("vk", "я" * 3500).ok)
        links = logic.inbox_links({"channel": "ig", "username": "anna.kzn"})
        self.assertEqual(links["ig"], "https://instagram.com/anna.kzn")
        self.assertIsNone(logic.inbox_links({"channel": "ig", "username": "a/b"})["ig"])
        self.assertEqual(logic.inbox_links({"channel": "vk", "ext_id": "123"})["vk"],
                         "https://vk.com/id123")
        self.assertIsNone(logic.inbox_links({"channel": "vk", "ext_id": "1 or 2"})["vk"])
        self.assertIsNone(logic.inbox_links({"channel": "ig", "username": "anna_k"})["tg"],
                          "логин Instagram - не логин Telegram")

    def test_crm_request(self):
        contact = {"createContact": {
            "responsibleUserId": "u-7", "name": "Азиз", "source": "byUser",
            "contactData": [{"chatType": "whatsgroup", "chatId": "1@g.us"},
                            {"chatType": "whatsapp", "chatId": "79001234567",
                             "extra": {"deep": {"deeper": 1}}},
                            {"chatType": "whatsapp", "chatId": "79001234567"},
                            {"chatType": "instagram", "chatId": "anna.kzn",
                             "username": "anna.kzn"}],
            "unknown": "лишнее поле"}}
        req = logic.wazzup_crm_request(contact)
        self.assertEqual(req["kind"], "contact")
        self.assertEqual([(c["channel"], c["ext_id"]) for c in req["chats"]],
                         [("wa", "+79001234567"), ("ig", "anna.kzn")],
                         "группа мимо, повтор чата - один раз")
        self.assertEqual(req["chats"][0]["phone"], "+79001234567")
        self.assertEqual((req["name"], req["responsible"], req["source"]),
                         ("Азиз", "u-7", "byUser"))
        self.assertEqual(req["contact_data"][1]["chatId"], "79001234567")
        self.assertIsNone(req["contact_data"][1]["extra"],
                          "в ответ - список и словари чатов, глубже - нет")
        deal = logic.wazzup_crm_request({"createDeal": {
            "responsibleUserId": 5, "contacts": ["thread-12", "client-3", "thread-12",
                                                 "wz-uuid", 7, "thread-0", "thread-1x"],
            "source": "auto"}})
        self.assertEqual(deal["kind"], "deal")
        self.assertEqual(deal["refs"], [("thread", 12), ("client", 3)])
        self.assertEqual(deal["contacts"][:2], ["thread-12", "client-3"])
        for junk in ({"createContact": "строка"}, {"createDeal": None},
                     {"createContact": {"contactData": "не список"}},
                     {"createDeal": {"contacts": {"a": 1}}}):
            req = logic.wazzup_crm_request(junk)
            self.assertEqual((req["chats"], req["refs"]), ([], []), junk)
        for not_ours in ({"messages": []}, {"test": True}, [], "x", None,
                         {"createContact": {}, "createDeal": {}}):
            self.assertIsNone(logic.wazzup_crm_request(not_ours), not_ours)
        nan = logic.wazzup_crm_request(json.loads(
            '{"createContact": {"responsibleUserId": NaN, "contactData": [{"x": Infinity}]}}'))
        self.assertIsNone(nan["responsible"])
        self.assertEqual(nan["contact_data"], [{"x": None}])
        json.dumps(logic.wazzup_contact_reply(nan, contact_id="thread-1", name="x", uri=None),
                   allow_nan=False)

    def test_crm_replies(self):
        req = logic.wazzup_crm_request({"createContact": {
            "responsibleUserId": "u-7", "name": "Азиз",
            "contactData": [{"chatType": "whatsapp", "chatId": "79001234567"}]}})
        self.assertEqual(logic.wazzup_contact_reply(
            req, contact_id="thread-5", name="Азиз", uri="https://crm.ru/inbox/5"), {
            "id": "thread-5", "responsibleUserId": "u-7", "name": "Азиз",
            "contactData": [{"chatType": "whatsapp", "chatId": "79001234567"}],
            "uri": "https://crm.ru/inbox/5"})
        deal_req = logic.wazzup_crm_request({"createDeal": {"contacts": ["thread-5"]}})
        self.assertEqual(logic.wazzup_deal_reply(deal_req, deal_id=9, contact_id="thread-5",
                                                 uri=None), {
            "id": "deal-9", "name": "Заявка № 9", "contacts": ["thread-5"], "closed": False})
        by_data = logic.wazzup_crm_request({"createDeal": {"contactData": [
            {"chatType": "vk", "chatId": "42"}]}})
        self.assertEqual(logic.wazzup_deal_reply(by_data, deal_id=9, contact_id="thread-6",
                                                 uri=None)["contacts"], ["thread-6"])
        self.assertEqual(logic.wazzup_panel_url("https://CRM.mybike.ru/", "/deals/9"),
                         "https://crm.mybike.ru/deals/9")
        self.assertIsNone(logic.wazzup_panel_url("", "/deals/9"))
        self.assertIsNone(logic.wazzup_panel_url("evil.ru/x?", "/deals/9"))

    def test_deal_signal_rule(self):
        now = NOW
        self.assertTrue(logic.wazzup_deal_signal(None, now=now), "сделка на карточку")
        fresh = {"status": "new", "waiting_since": None, "announced_at": now}
        self.assertFalse(logic.wazzup_deal_signal(fresh, now=now),
                         "контакт только что завёл Wazzup - сигнал даст сообщение")
        old = {**fresh, "announced_at": now - timedelta(hours=13)}
        self.assertTrue(logic.wazzup_deal_signal(old, now=now))
        self.assertFalse(logic.wazzup_deal_signal({**old, "waiting_since": now}, now=now),
                         "человек ждёт - сигнал «новое обращение» уже был")
        self.assertFalse(logic.wazzup_deal_signal({**old, "announced_at": None}, now=now),
                         "сигнал обращения в очереди")
        self.assertFalse(logic.wazzup_deal_signal({**old, "status": "spam"}, now=now))
        text = logic.wazzup_deal_team_text({"id": 9, "source": "ig", "name": "Анна",
                                            "phone": "+79001234567"})
        self.assertIn("№ 9", text)
        self.assertIn("Instagram", text)
        for secret in ("Анна", "+7900", "9001234567"):
            self.assertNotIn(secret, text)

    def test_readiness_row(self):
        off = readiness.check_wazzup({}, NOW)
        self.assertEqual(off["state"], readiness.OFF)
        ok = readiness.check_wazzup(state(), NOW)
        self.assertEqual(ok["state"], readiness.OK)
        self.assertIn("+79991110000", ok["text"])
        warn = readiness.check_wazzup(state(hook_error="нет домена"), NOW)
        self.assertEqual(warn["state"], readiness.WARN)
        many = readiness.check_wazzup(state(channels=[
            {"id": CH1, "kind": "wa", "phone": "+79991110000", "active": True},
            {"id": "ig-1", "kind": "ig", "active": True},
            {"id": "vk-1", "kind": "vk", "active": False}]), NOW)
        self.assertEqual(many["state"], readiness.OK)
        self.assertIn("Instagram", many["text"])
        self.assertIn("ВКонтакте (не в сети)", many["text"])
        self.assertIn("Создать сделку", many["text"])
        dead = readiness.check_wazzup(state(), NOW + timedelta(hours=5))
        self.assertEqual(dead["state"], readiness.WARN)

    def test_access_log_hides_token(self):
        from app.web.__main__ import HideHookToken
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                                   '%s - "%s %s HTTP/%s" %d',
                                   ("1.2.3.4:5", "POST", "/hook/inbox/secret-token", "1.1",
                                    200), None)
        HideHookToken().filter(record)
        self.assertNotIn("secret-token", record.getMessage())
        self.assertIn("/hook/inbox/***", record.getMessage())


# ─────────────────────── процесс бота: отправка и сверка ───────────────────────


class FakeWazzup:
    def __init__(self, *, ready=True, channels=None, error=None, hook_error=None,
                 message_id="wz-9"):
        self.ready = ready
        self.raw_channels = channels if channels is not None else [
            {"id": CH1, "phone": "+79991110000", "state": "active", "active": True}]
        self.error, self.hook_error, self.message_id = error, hook_error, message_id
        self.sent, self.hooks, self.kinds = [], [], []

    async def channels(self):
        if self.error:
            raise self.error
        return self.raw_channels

    async def set_webhook(self, uri):
        if self.hook_error:
            raise self.hook_error
        self.hooks.append(uri)

    async def send_text(self, channel_id, phone, text, *, crm_message_id, kind="wa"):
        if self.error:
            raise self.error
        self.sent.append((channel_id, phone, text, crm_message_id))
        self.kinds.append(kind)
        return {"messageId": self.message_id}


def cfg(**over):
    values = {"inbox_key": KEY, "admin_chat_id": -100, "avito_poll_seconds": 60,
              "crm_domain": "crm.mybike.ru", "inbox_hook_token": "hook-5f0c"}
    values.update(over)
    return types.SimpleNamespace(**values)


@unittest.skipUnless(HAVE_DEPS, "нет зависимостей")
class TestBotSide(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.crm = FakeCrm()
        self.vault = Vault.from_raw(KEY)

    async def queued(self, text="Да, свободен", **thread):
        values = {"channel": "wa", "origin": "hook", "ext_id": "+79001234567",
                  "phone": "+79001234567", "text": "Свободен?"}
        values.update(thread)
        got = await service.inbox_in(self.crm, self.vault, **values)
        mid = await self.crm.queue_inbox_reply(
            got["thread_id"], body_enc=service.inbox_seal(self.vault, text), author="admin")
        return got["thread_id"], mid

    async def test_reply_goes_from_the_number_they_wrote_to(self):
        await self.crm.set_setting("inbox_wazzup_state", state()["inbox_wazzup_state"],
                                   by="t")
        _, mid = await self.queued(ext_channel=CH2)
        fake = FakeWazzup()
        await inbox.send_once(None, self.crm, cfg(), wazzup=fake)
        [sent] = fake.sent
        self.assertEqual(sent[:3], (CH2, "+79001234567", "Да, свободен"))
        created = self.crm.inbox_messages_[mid]["created_at"]
        self.assertEqual(sent[3], f"mybike-inbox-{mid}-{int(created.timestamp() * 1000)}",
                         "номер строки и миг её создания: после восстановления базы "
                         "номера строк повторяются")
        message = self.crm.inbox_messages_[mid]
        self.assertEqual((message["status"], message["ext_id"]), ("sent", "wz-9"))

    async def test_telegram_and_avito_through_wazzup(self):
        """Личный Telegram и Авито из Wazzup отвечают через тот же канал
        Wazzup: адрес - номер чата, а не телефон; из n8n (без канала) -
        никуда."""
        await self.crm.set_setting("inbox_wazzup_state", state()["inbox_wazzup_state"],
                                   by="t")
        for channel, chat in (("tgp", "5001"), ("avito", "u2i-abc~1")):
            with self.subTest(channel=channel):
                _, mid = await self.queued(channel=channel, ext_id=chat, phone=None,
                                           ext_channel=CH2)
                fake = FakeWazzup()
                await inbox.send_once(None, self.crm, cfg(), wazzup=fake)
                self.assertEqual(fake.sent[0][:3], (CH2, chat, "Да, свободен"))
                self.assertEqual(fake.kinds, [channel])
                self.assertEqual(self.crm.inbox_messages_[mid]["status"], "sent")
        _, mid = await self.queued(channel="avito", ext_id="n8n-chat", phone=None)
        fake = FakeWazzup()
        await inbox.send_once(None, self.crm, cfg(), wazzup=fake)
        self.assertEqual(fake.sent, [])
        self.assertEqual(self.crm.inbox_messages_[mid]["status"], "failed")

    async def test_old_thread_uses_the_only_number(self):
        await self.crm.set_setting("inbox_wazzup_state", state()["inbox_wazzup_state"],
                                   by="t")
        _, mid = await self.queued()
        fake = FakeWazzup()
        await inbox.send_once(None, self.crm, cfg(), wazzup=fake)
        self.assertEqual(fake.sent[0][0], CH1)
        self.assertEqual(self.crm.inbox_messages_[mid]["status"], "sent")

    async def test_not_sent(self):
        two = json.dumps({**json.loads(state()["inbox_wazzup_state"]), "channels": [
            {"id": CH1, "active": True}, {"id": CH2, "active": True}]})
        cases = ((None, None, "Wazzup не подключён"),
                 (FakeWazzup(ready=False), None, "Wazzup не подключён"),
                 (FakeWazzup(), two, "с какого нашего канала"),
                 (FakeWazzup(error=wz.WazzupError("401 — Wazzup не принял ключ API", 401)),
                  None, "Wazzup: 401"))
        for fake, raw, why in cases:
            with self.subTest(why=why):
                self.crm = FakeCrm()
                await self.crm.set_setting(
                    "inbox_wazzup_state", raw or state()["inbox_wazzup_state"], by="t")
                _, mid = await self.queued()
                await inbox.send_once(None, self.crm, cfg(), wazzup=fake)
                message = self.crm.inbox_messages_[mid]
                self.assertEqual(message["status"], "failed")
                self.assertIn(why, message["error"])

    async def test_check_subscribes_once_and_keeps_no_token(self):
        fake = FakeWazzup()
        got = await inbox.wazzup_once(self.crm, fake, cfg(), now=NOW)
        self.assertTrue(got["ok"])
        self.assertEqual(fake.hooks, ["https://crm.mybike.ru/hook/inbox/hook-5f0c"])
        raw = (await self.crm.settings())["inbox_wazzup_state"]
        self.assertNotIn("hook-5f0c", raw, "токен хука в базу не попадает")
        await inbox.wazzup_once(self.crm, fake, cfg(), now=NOW + timedelta(hours=1))
        self.assertEqual(len(fake.hooks), 1, "подписка свежая - второй раз не ставим")
        await inbox.wazzup_once(self.crm, fake, cfg(inbox_hook_token="new-token"),
                                now=NOW + timedelta(hours=2))
        self.assertEqual(len(fake.hooks), 2, "сменился токен - подписываем новый адрес")
        await inbox.wazzup_once(self.crm, fake, cfg(inbox_hook_token="new-token"),
                                now=NOW + timedelta(hours=27))
        self.assertEqual(len(fake.hooks), 3, "сутки - освежаем подписку")
        seen = logic.wazzup_state(await self.crm.settings(), now=NOW + timedelta(hours=27))
        self.assertTrue(seen["live"] and seen["hook"])

    async def test_check_reports_problems(self):
        got = await inbox.wazzup_once(self.crm, FakeWazzup(), cfg(crm_domain=""), now=NOW)
        self.assertTrue(got["ok"])
        self.assertIn("CRM_DOMAIN", got["hook_error"])
        failing = FakeWazzup(hook_error=wz.WazzupError("Wazzup ответил 400: test failed",
                                                       400))
        got = await inbox.wazzup_once(self.crm, failing, cfg(), now=NOW)
        self.assertIn("подписка хука", got["hook_error"])
        dead = FakeWazzup(error=wz.WazzupError("401 — Wazzup не принял ключ API", 401))
        got = await inbox.wazzup_once(self.crm, dead, cfg(), now=NOW)
        self.assertFalse(got["ok"])
        self.assertIn("401", got["error"])
        self.assertEqual(len(got["channels"]), 1, "номера с прошлой сверки не теряются")

    async def test_loop_clears_state_without_key(self):
        await self.crm.set_setting("inbox_wazzup_state", state()["inbox_wazzup_state"],
                                   by="t")
        task = asyncio.create_task(inbox.inbox_loop(None, self.crm, cfg(),
                                                    wazzup=FakeWazzup(ready=False),
                                                    interval=3600))
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse((await self.crm.settings()).get("inbox_wazzup_state"))


# ───────────────────────────── хук и панель ─────────────────────────────


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestHookAndPanel(tiw.InboxCase if HAVE_WEB else unittest.TestCase):
    def webhook(self, token=tiw.HOOK if HAVE_WEB else "", **message):
        body = {"messageId": "wz-in-1", "channelId": CH2, "chatType": "whatsapp",
                "chatId": "79001234567", "type": "text", "text": "Велосипед свободен?",
                "isEcho": False, "contact": {"name": "Азиз"},
                "dateTime": "2026-09-30T09:00:00.000Z"}
        body.update(message)
        return self.client.post(f"/hook/inbox/{token}", json={"messages": [body]})

    def test_token_in_path(self):
        r = self.client.post(f"/hook/inbox/{tiw.HOOK}", json={"test": True})
        self.assertEqual(r.status_code, 200, "проверка адреса от Wazzup")
        r = self.webhook()
        self.assertEqual(r.json()["saved"], 1)
        [thread] = tiw.run(self.crm.inbox_threads(limit=10))
        self.assertEqual((thread["channel"], thread["ext_id"], thread["ext_channel"]),
                         ("wa", "+79001234567", CH2))
        self.assertEqual(self.webhook(token="wrong").status_code, 401)
        self.assertEqual(self.webhook().json()["duplicates"], 1)
        self.build(inbox_key=tiw.KEY, inbox_hook_token="")
        self.assertEqual(self.webhook().status_code, 404, "пустой токен - хука нет")

    def test_reply_form_follows_wazzup_state(self):
        self.webhook()
        [thread] = tiw.run(self.crm.inbox_threads(limit=10))
        self.login()
        form = f'action="/inbox/{thread["id"]}/reply"'
        page = self.get_ok(f"/inbox/{thread['id']}")
        self.assertNotIn(form, page)
        self.assertIn("wa.me", page)
        fresh = json.loads(state()["inbox_wazzup_state"])
        fresh["at"] = datetime.now(UTC).isoformat()
        tiw.run(self.crm.set_setting("inbox_wazzup_state", json.dumps(fresh), by="t"))
        page = self.get_ok(f"/inbox/{thread['id']}")
        self.assertIn(form, page)
        self.assertIn("с нашего номера через Wazzup", page)
        r = self.client.post(f"/inbox/{thread['id']}/reply",
                             data={"text": "Да, приезжайте", "once": "w1"})
        self.assertEqual(r.status_code, 303, r.text[:300])
        queued = [m for m in self.crm.inbox_messages_.values() if m["direction"] == "out"]
        self.assertEqual([m["status"] for m in queued], ["queued"])
        listing = self.get_ok("/inbox")
        self.assertIn("+79991110000", listing)
        fresh["hook_error"] = "нет домена панели"
        tiw.run(self.crm.set_setting("inbox_wazzup_state", json.dumps(fresh), by="t"))
        self.assertIn("входящие не придут", self.get_ok("/inbox"))



# ─────────────── новые каналы и заявки: процесс бота ───────────────


@unittest.skipUnless(HAVE_DEPS, "нет зависимостей")
class TestNewChannelsBotSide(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.crm = FakeCrm()
        self.vault = Vault.from_raw(KEY)
        await self.crm.set_setting("inbox_wazzup_state", state()["inbox_wazzup_state"],
                                   by="t")

    async def test_reply_goes_to_instagram_vk_viber(self):
        """Ответ из панели уходит через Wazzup с того канала, куда писал
        человек: chatType - канал «Входящих», адрес - номер чата."""
        for channel, chat in (("ig", "anna.kzn"), ("vk", "123456789"),
                              ("viber", "uX9+ab/c==")):
            with self.subTest(channel=channel):
                got = await service.inbox_in(self.crm, self.vault, channel=channel,
                                             origin="hook", ext_id=chat, text="Привет",
                                             ext_channel=CH2)
                mid = await self.crm.queue_inbox_reply(
                    got["thread_id"], body_enc=service.inbox_seal(self.vault, "Да"),
                    author="admin")
                fake = FakeWazzup()
                await inbox.send_once(None, self.crm, cfg(), wazzup=fake)
                self.assertEqual(fake.sent[0][:3], (CH2, chat, "Да"))
                self.assertEqual(fake.kinds, [channel])
                self.assertEqual(self.crm.inbox_messages_[mid]["status"], "sent")
                self.assertEqual(wz.CHAT_TYPES[channel], logic.WAZZUP_CHAT_TYPES[channel])
        # без канала Wazzup (пришло из n8n) - вне панели
        got = await service.inbox_in(self.crm, self.vault, channel="vk", origin="hook",
                                     ext_id="777", text="Привет")
        mid = await self.crm.queue_inbox_reply(
            got["thread_id"], body_enc=service.inbox_seal(self.vault, "Да"), author="a")
        fake = FakeWazzup()
        await inbox.send_once(None, self.crm, cfg(), wazzup=fake)
        self.assertEqual(fake.sent, [])
        self.assertEqual(self.crm.inbox_messages_[mid]["status"], "failed")

    async def test_old_subscription_is_renewed_for_deals(self):
        """Подписка, поставленная до заявок (без отметки вида), обновляется
        первым же кругом, не дожидаясь суток."""
        url = logic.wazzup_hook_url("crm.mybike.ru", "hook-5f0c")
        old = json.loads(state(hook=logic.wazzup_fingerprint(url))["inbox_wazzup_state"])
        await self.crm.set_setting("inbox_wazzup_state", json.dumps(old), by="t")
        fake = FakeWazzup()
        got = await inbox.wazzup_once(self.crm, fake, cfg(), now=NOW + timedelta(hours=1))
        self.assertEqual(fake.hooks, [url])
        self.assertEqual(got["subs"], inbox.WAZZUP_SUBS)
        await inbox.wazzup_once(self.crm, fake, cfg(), now=NOW + timedelta(hours=2))
        self.assertEqual(len(fake.hooks), 1, "вид тот же - второй раз не ставим")

    async def test_deal_signal_once_without_names(self):
        bot = FakeBot()
        deal_id = await self.crm.create_deal(stage="new", by="wazzup", source="ig",
                                             name="Анна Курьерова", phone="+79001234567",
                                             announced_at=None)
        await self.crm.create_deal(stage="new", by="auto", source="wa", name="Без сигнала")
        self.assertEqual(await inbox.announce_deals_once(bot, self.crm, cfg()), 1)
        [(chat, text)] = bot.sent
        self.assertEqual(chat, -100)
        self.assertIn(f"№ {deal_id}", text)
        for secret in ("Анна", "Курьерова", "9001234567"):
            self.assertNotIn(secret, text)
        self.assertEqual(await inbox.announce_deals_once(bot, self.crm, cfg()), 0)
        self.assertEqual(len(bot.sent), 1, "сигнал один раз")

    async def test_deal_signal_off_marks_without_sending(self):
        await self.crm.create_deal(stage="new", by="wazzup", source="wa", announced_at=None)
        bot = FakeBot()
        self.assertEqual(await inbox.announce_deals_once(bot, self.crm,
                                                         cfg(admin_chat_id=None)), 1)
        self.assertEqual(bot.sent, [])
        self.assertEqual(await self.crm.deals_to_announce(), [],
                         "включение не обрушит накопленное")

    async def test_deal_signal_retries_then_gives_up(self):
        await self.crm.create_deal(stage="new", by="wazzup", source="wa", announced_at=None)
        broken = FakeBot(fail=RuntimeError("chat not found"))
        for _ in range(inbox.ANNOUNCE_TRIES - 1):
            self.assertEqual(await inbox.announce_deals_once(broken, self.crm, cfg()), 0)
            self.assertEqual(len(await self.crm.deals_to_announce()), 1)
        self.assertEqual(await inbox.announce_deals_once(broken, self.crm, cfg()), 1)
        self.assertEqual(await self.crm.deals_to_announce(), [])

    async def test_many_deal_signals_go_as_summary(self):
        for _ in range(inbox.ANNOUNCE_BURST + 1):
            await self.crm.create_deal(stage="new", by="wazzup", source="wa",
                                       announced_at=None)
        bot = FakeBot()
        self.assertEqual(await inbox.announce_deals_once(bot, self.crm, cfg()),
                         inbox.ANNOUNCE_BURST + 1)
        [(_, text)] = bot.sent
        self.assertIn(f"Новых заявок из Wazzup: {inbox.ANNOUNCE_BURST + 1}", text)


class FakeBot:
    def __init__(self, fail: Exception | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if self.fail is not None:
            raise self.fail
        self.sent.append((chat_id, text))


# ─────────────── заявки из окна чата Wazzup: хук целиком ───────────────


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestWazzupCrmHook(tiw.InboxCase if HAVE_WEB else unittest.TestCase):
    """createContact и createDeal на /hook/inbox/<токен>: ответ - созданная
    сущность, повтор - то же самое, обращение не создаёт клиента."""

    def setUp(self):
        super().setUp()
        self.build(inbox_key=tiw.KEY, inbox_hook_token=tiw.HOOK, crm_domain="crm.mybike.ru")

    def hook(self, body, token=tiw.HOOK if HAVE_WEB else ""):
        return self.client.post(f"/hook/inbox/{token}", json=body)

    def contact(self, chat_type="whatsapp", chat_id="79001234567", **extra):
        body = {"responsibleUserId": "wz-user-1", "name": "Азиз Каримов",
                "source": "byUser",
                "contactData": [{"chatType": chat_type, "chatId": chat_id}], **extra}
        return self.hook({"createContact": body})

    def test_contact_is_a_thread_not_a_client(self):
        clients_before = len(self.crm.clients_)
        r = self.contact()
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        [thread] = tiw.run(self.crm.inbox_threads(limit=10))
        self.assertEqual(body, {
            "id": f"thread-{thread['id']}", "responsibleUserId": "wz-user-1",
            "name": "Азиз Каримов",
            "contactData": [{"chatType": "whatsapp", "chatId": "79001234567"}],
            "uri": f"https://crm.mybike.ru/inbox/{thread['id']}"})
        self.assertEqual((thread["channel"], thread["ext_id"], thread["phone"],
                          thread["origin"]), ("wa", "+79001234567", "+79001234567", "hook"))
        self.assertEqual(len(self.crm.clients_), clients_before, "карточку хук не заводит")
        self.assertEqual(self.messages(thread["id"]), [], "сообщения нет - пусто")
        self.assertIsNotNone(thread["announced_at"], "контакт сигнала не даёт")
        self.assertEqual(self.contact().json()["id"], body["id"], "повтор - тот же контакт")
        self.assertEqual(len(tiw.run(self.crm.inbox_threads(limit=10))), 1)
        # сообщение того же чата - в то же обращение, и сигнал даёт оно
        self.client.post(f"/hook/inbox/{tiw.HOOK}", json={"messages": [{
            "messageId": "m-1", "channelId": CH2, "chatType": "whatsapp",
            "chatId": "79001234567", "type": "text", "text": "Свободно?"}]})
        [thread] = tiw.run(self.crm.inbox_threads(limit=10))
        self.assertEqual(thread["ext_channel"], CH2)
        self.assertIsNone(thread["announced_at"])

    def test_contact_links_existing_card_by_phone(self):
        client_id = tiw.run(self.crm.create_client(full_name="Каримов Азиз Рустамович",
                                                   phone="+79001234567"))
        body = self.contact().json()
        [thread] = tiw.run(self.crm.inbox_threads(limit=10))
        self.assertEqual(thread["client_id"], client_id)
        self.assertEqual(body["uri"], f"https://crm.mybike.ru/clients/{client_id}")
        self.assertEqual(body["name"], "Азиз Каримов", "ФИО карточки наружу не уходит")

    def test_contact_without_domain_has_no_uri(self):
        self.build(inbox_key=tiw.KEY, inbox_hook_token=tiw.HOOK, crm_domain="")
        self.assertNotIn("uri", self.contact().json())

    def test_contact_channels(self):
        for chat_type, chat, channel in (("instagram", "anna.kzn", "ig"),
                                         ("vk", "123456789", "vk"),
                                         ("viber", "uX9+ab/c==", "viber"),
                                         ("telegram", "5001", "tgp"),
                                         ("avito", "u2i-abc~1", "avito")):
            with self.subTest(chat_type=chat_type):
                body = self.contact(chat_type, chat).json()
                ref = logic.wazzup_ref(body["id"])
                thread = tiw.run(self.crm.inbox_thread(ref[1]))
                self.assertEqual((thread["channel"], thread["ext_id"]), (channel, chat))

    def test_garbage_is_skipped_with_200(self):
        before = len(tiw.run(self.crm.inbox_threads(limit=10)))
        for body in ({"createContact": "строка"}, {"createContact": {}},
                     {"createContact": {"contactData": [{"chatType": "whatsgroup",
                                                         "chatId": "1@g.us"}]}},
                     {"createContact": {"contactData": [{"chatType": "max",
                                                         "chatId": "5001"}]}},
                     {"createDeal": {"contacts": ["thread-999999"]}},
                     {"createDeal": {"contacts": ["чужой-id"]}},
                     {"createDeal": None}):
            with self.subTest(body=body):
                r = self.hook(body)
                self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "skipped": 1}))
        r = self.hook({"createContact": {}, "createDeal": {}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["skipped"], 1, "два вида сразу - не наш запрос")
        self.assertEqual(len(tiw.run(self.crm.inbox_threads(limit=10))), before)

    def test_same_guards_as_messages(self):
        self.assertEqual(self.hook({"createContact": {}}, token="wrong").status_code, 401)
        r = self.client.post(f"/hook/inbox/{tiw.HOOK}",
                             content=b'{"createContact": {"name": "' + b"x" * 70000 + b'"}}',
                             headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 413)
        self.build(inbox_key=tiw.KEY, inbox_hook_token="", crm_domain="crm.mybike.ru")
        self.assertEqual(self.contact().status_code, 404)

    def test_no_names_or_phones_in_log(self):
        with self.assertLogs("app.web.app", level="INFO") as logs:
            contact = self.contact().json()
            self.hook({"createDeal": {"contacts": [contact["id"]]}})
            self.hook({"createContact": {"name": "Азиз"}})
        text = "\n".join(logs.output)
        for secret in ("Азиз", "Каримов", "79001234567", "9001234567"):
            self.assertNotIn(secret, text)

    def test_deal_from_contact_and_repeat(self):
        contact = self.contact().json()
        r = self.hook({"createDeal": {"responsibleUserId": "wz-user-1",
                                      "contacts": [contact["id"]], "source": "byUser"}})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        [deal] = tiw.run(self.crm.deals())
        self.assertEqual(body, {
            "id": f"deal-{deal['id']}", "responsibleUserId": "wz-user-1",
            "name": f"Заявка № {deal['id']}", "contacts": [contact["id"]],
            "uri": f"https://crm.mybike.ru/deals/{deal['id']}", "closed": False})
        thread_id = logic.wazzup_ref(contact["id"])[1]
        self.assertEqual((deal["stage"], deal["thread_id"], deal["source"],
                          deal["created_by"], deal["name"]),
                         ("new", thread_id, "wa", "wazzup", "Азиз Каримов"))
        self.assertIsNotNone(deal["announced_at"],
                             "контакт только что завёл Wazzup - сигнал даст сообщение")
        again = self.hook({"createDeal": {"contacts": [contact["id"]]}}).json()
        self.assertEqual(again["id"], body["id"], "повтор - та же сделка")
        self.assertEqual(len(tiw.run(self.crm.deals())), 1)
        self.assertEqual(len(self.crm.clients_), 0, "сделка карточку не заводит")
        self.assertEqual(self.crm.ledger_, [], "в журнал денег заявка не пишет")

    def test_deal_by_contact_data_and_funnel_shows_it(self):
        r = self.hook({"createDeal": {"contactData": [
            {"chatType": "instagram", "chatId": "anna.kzn", "username": "anna.kzn"}],
            "name": "Анна"}})
        body = r.json()
        [deal] = tiw.run(self.crm.deals())
        self.assertEqual(body["id"], f"deal-{deal['id']}")
        thread = tiw.run(self.crm.inbox_thread(deal["thread_id"]))
        self.assertEqual(body["contacts"], [f"thread-{thread['id']}"])
        self.assertEqual((thread["channel"], thread["username"]), ("ig", "anna.kzn"))
        self.login()
        board = self.get_ok("/incoming")
        self.assertIn(f'href="/deals/{deal["id"]}"', board)
        self.assertIn("Instagram", board)
        self.assertIn(">Wazzup<", board, "видно, что заявку завели в Wazzup")
        card = self.get_ok(f"/deals/{deal['id']}")
        self.assertIn("заявка из Wazzup", card)
        tiw.run(deals_module().sync(self.crm))
        self.assertEqual(len(tiw.run(self.crm.deals())), 1, "сверка не заводит вторую")

    def test_deal_joins_client_open_deal(self):
        client_id = tiw.run(self.crm.create_client(full_name="Каримов Азиз",
                                                   phone="+79001234567"))
        open_id = tiw.run(self.crm.create_deal(stage="touch", by="t", client_id=client_id,
                                               source="manual"))
        contact = self.contact().json()
        body = self.hook({"createDeal": {"contacts": [contact["id"]]}}).json()
        self.assertEqual(body["id"], f"deal-{open_id}", "у клиента одна открытая")
        deal = tiw.run(self.crm.deal(open_id))
        self.assertEqual((deal["stage"], deal["thread_id"]),
                         ("touch", logic.wazzup_ref(contact["id"])[1]),
                         "этап не откатывается, обращение привязано")
        by_card = self.hook({"createDeal": {"contacts": [f"client-{client_id}"]}}).json()
        self.assertEqual(by_card["id"], f"deal-{open_id}")

    def test_closed_deal_rules(self):
        contact = self.contact("vk", "42").json()
        thread_id = logic.wazzup_ref(contact["id"])[1]
        first = self.hook({"createDeal": {"contacts": [contact["id"]]}}).json()
        deal_id = int(first["id"].split("-")[1])
        tiw.run(self.crm.move_deal(deal_id, "lost", by="t", close=True))
        reopened = self.hook({"createDeal": {"contacts": [contact["id"]]}}).json()
        self.assertEqual((reopened["id"], reopened["closed"]), (first["id"], False),
                         "«Не взял» без карточки - та же сделка снова в работе")
        self.assertEqual(tiw.run(self.crm.deal(deal_id))["stage"], "new")
        tiw.run(self.crm.move_deal(deal_id, "lost", by="t", close=True))
        tiw.run(self.crm.update_inbox_thread(thread_id, status="spam"))
        spam = self.hook({"createDeal": {"contacts": [contact["id"]]}}).json()
        self.assertEqual((spam["id"], spam["closed"]), (first["id"], True),
                         "спам не оживает")
        # закрытая сделка с карточкой - новая сделка на клиента
        tiw.run(self.crm.update_inbox_thread(thread_id, status="done"))
        client_id = tiw.run(self.crm.create_client(full_name="Петров Пётр",
                                                   phone="+79005556677"))
        tiw.run(self.crm.update_inbox_thread(thread_id, client_id=client_id))
        fresh = self.hook({"createDeal": {"contacts": [contact["id"]]}}).json()
        self.assertNotEqual(fresh["id"], first["id"])
        new = tiw.run(self.crm.deal(int(fresh["id"].split("-")[1])))
        self.assertEqual((new["client_id"], new["thread_id"], new["stage"]),
                         (client_id, None, "new"))
        again = self.hook({"createDeal": {"contacts": [contact["id"]]}}).json()
        self.assertEqual(again["id"], fresh["id"], "повтор - та же")

    def test_deal_signal_for_old_conversation(self):
        """Заявка на давний разговор, о котором чат уже забыл, - сигнал;
        на свежий - нет: о нём сигнал уже был."""
        contact = self.contact().json()
        thread_id = logic.wazzup_ref(contact["id"])[1]
        tiw.run(self.crm.update_inbox_thread(
            thread_id, announced_at=datetime.now(UTC) - timedelta(days=2)))
        self.hook({"createDeal": {"contacts": [contact["id"]]}})
        [deal] = tiw.run(self.crm.deals())
        self.assertIsNone(deal["announced_at"], "сигнал ждёт круга бота")

    def test_reply_from_panel_to_instagram(self):
        self.client.post(f"/hook/inbox/{tiw.HOOK}", json={"messages": [{
            "messageId": "ig-1", "channelId": "ig-ch", "chatType": "instagram",
            "chatId": "anna.kzn", "type": "text", "text": "Есть на неделю?",
            "contact": {"name": "Анна", "username": "anna.kzn"}}]})
        [thread] = tiw.run(self.crm.inbox_threads(limit=10))
        self.assertEqual((thread["channel"], thread["ext_channel"]), ("ig", "ig-ch"))
        fresh = json.loads(state(channels=[{"id": "ig-ch", "kind": "ig", "state": "active",
                                            "active": True}])["inbox_wazzup_state"])
        fresh["at"] = datetime.now(UTC).isoformat()
        tiw.run(self.crm.set_setting("inbox_wazzup_state", json.dumps(fresh), by="t"))
        self.login()
        page = self.get_ok(f"/inbox/{thread['id']}")
        self.assertIn('maxlength="1000"', page)
        self.assertIn("через Wazzup", page)
        self.assertIn("https://instagram.com/anna.kzn", page)
        r = self.client.post(f"/inbox/{thread['id']}/reply",
                             data={"text": "я" * 1001, "once": "ig-long"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f"/inbox/{thread['id']}/reply",
                             data={"text": "Да, есть", "once": "ig-ok"})
        self.assertEqual(r.status_code, 303, r.text[:300])
        listing = self.get_ok("/inbox?channel=ig")
        self.assertIn("Instagram", listing)
        self.assertIn(">Instagram<", self.get_ok("/inbox"))


def deals_module():
    from app.crm import deals
    return deals


if __name__ == "__main__":
    unittest.main()
