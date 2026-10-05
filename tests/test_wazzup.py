"""WhatsApp через Wazzup: клиент API, правила, отправка ответа из очереди,
ежечасная сверка номеров и подписки, хук с токеном в адресе и панель.

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

    def test_readiness_row(self):
        off = readiness.check_wazzup({}, NOW)
        self.assertEqual(off["state"], readiness.OFF)
        ok = readiness.check_wazzup(state(), NOW)
        self.assertEqual(ok["state"], readiness.OK)
        self.assertIn("+79991110000", ok["text"])
        warn = readiness.check_wazzup(state(hook_error="нет домена"), NOW)
        self.assertEqual(warn["state"], readiness.WARN)
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


if __name__ == "__main__":
    unittest.main()
