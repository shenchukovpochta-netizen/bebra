"""«Входящие»: разбор хуков, право ответа, очередь ответов, опрос Авито.

Обращение - не клиент и не деньги, но переписка - ПДн: поэтому здесь
проверяется не только «дошло», но и «не утекло». Текст лежит в базе
шифротекстом, сигнал в служебный чат идёт без имени, телефона и слов
человека, а ссылка на объявление в карточке - только на Авито.

Сети нет: бот, MAX и Авито - заглушки, база - tests/fake_crm.py.
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    from app import logic as bot_logic
    from app import texts
    from app.crm import inbox, service
    from app.services import avito as avito_api
    from app.services.avito import AvitoError
    from app.services.crypto import Vault, generate_key
    from tests.fake_crm import FakeCrm
    HAVE_DEPS = True
except ImportError:                                    # pragma: no cover
    HAVE_DEPS = False

KEY = generate_key() if HAVE_DEPS else ""
OTHER_KEY = generate_key() if HAVE_DEPS else ""
OWN = 1001                     # id нашего аккаунта Авито
ADMIN_CHAT = -100500


def cfg(**over):
    values = {"inbox_key": KEY, "admin_chat_id": ADMIN_CHAT, "avito_poll_seconds": 60}
    values.update(over)
    return types.SimpleNamespace(**values)


# ───────────────────────────── чистая логика ─────────────────────────────

class TestInboxBasics(unittest.TestCase):
    def test_number(self):
        self.assertEqual(logic.inbox_no(7), "ВХ-000007")
        self.assertEqual(logic.inbox_no("12"), "ВХ-000012")
        self.assertEqual(logic.inbox_no(None), "ВХ-000000")
        self.assertEqual(logic.inbox_no(1234567), "ВХ-1234567", "номер не режется")

    def test_avito_link_only_https_on_avito(self):
        for good in ("https://avito.ru/kazan/velosipedy/monster_123",
                     "https://www.avito.ru/kazan/velosipedy/monster_123",
                     "https://m.avito.ru/i/123",
                     "HTTPS://WWW.AVITO.RU/x"):
            self.assertEqual(logic.safe_avito_url(good), good, good)
        self.assertEqual(logic.safe_avito_url("  https://avito.ru/x  "), "https://avito.ru/x")
        for bad in ("", None, "   ", "javascript:alert(1)",
                    "javascript://avito.ru/%0aalert(1)",
                    "http://www.avito.ru/kazan/x",          # без https
                    "https://evil-avito.ru/x",
                    "https://avito.ru.evil.com/x",
                    "https://evilavito.ru/",
                    "https://avito.ru@evil.com/",            # логин в адресе
                    "https://evil.com/#.avito.ru",
                    "https://evil.com/?u=https://avito.ru",
                    "data:text/html,<script>alert(1)</script>",
                    "//www.avito.ru/x",                     # без схемы
                    "https://[::1/"):                       # не разбирается
            self.assertIsNone(logic.safe_avito_url(bad), repr(bad))
        long = logic.safe_avito_url("https://www.avito.ru/" + "a" * 1000)
        self.assertEqual(len(long), 500, "длинная ссылка обрезается, а не роняет")

    def test_avito_link_backslash_is_not_avito(self):
        # Браузер читает «\» в https-адресе как «/»: хост ниже для него -
        # evil.com, а urlsplit видит «evil.com\.avito.ru». Ссылка «на
        # объявление» из хука уводила бы администратора на чужой сайт.
        for bad in ("https://evil.com\\.avito.ru/kazan/x",
                    "https://evil.com\\@www.avito.ru/"):
            self.assertIsNone(logic.safe_avito_url(bad), repr(bad))

    def test_links(self):
        thread = {"username": "@ildar_h", "phone": "8 (900) 123-45-67",
                  "subject_url": "https://www.avito.ru/kazan/x_1"}
        self.assertEqual(logic.inbox_links(thread), {
            "tg": "https://t.me/ildar_h", "wa": "https://wa.me/79001234567",
            "avito": "https://www.avito.ru/kazan/x_1"})
        # иностранный номер - как есть, без подмены кода страны
        self.assertEqual(logic.inbox_links({"phone": "+998 90 123 45 67"})["wa"],
                         "https://wa.me/998901234567")
        for username in ("abc", "ildar h", "<script>", "a" * 33, "ильдар_ок", ""):
            self.assertIsNone(logic.inbox_links({"username": username})["tg"], username)
        empty = logic.inbox_links({"phone": "не скажу",
                                   "subject_url": "javascript:alert(1)"})
        self.assertEqual(empty, {"tg": None, "wa": None, "avito": None})

    def test_can_reply_matrix(self):
        # Каналы, куда отвечает сам процесс бота, и чем это доказано.
        for channel in logic.INBOX_CHANNELS:
            for origin in logic.INBOX_ORIGINS:
                for status in logic.INBOX_STATUSES:
                    for avito_ok in (True, False):
                        thread = {"channel": channel, "origin": origin,
                                  "status": status}
                        ok, why = logic.inbox_can_reply(thread, avito_ok=avito_ok)
                        if status == "spam":
                            want = False
                        elif channel == "tg":
                            want = origin == "bot"
                        elif channel == "max":
                            want = origin == "max_bot"
                        elif channel == "avito":
                            # ext_id шлюза - не номер чата Авито: через API
                            # отвечаем только в чаты из опроса Авито.
                            want = avito_ok and origin == "avito_api"
                        else:
                            want = False
                        label = f"{channel}/{origin}/{status}/avito_ok={avito_ok}"
                        self.assertEqual(ok, want, label)
                        if ok:
                            self.assertEqual(why, "", label)
                        else:
                            self.assertTrue(why, f"отказ без причины: {label}")

    def test_can_reply_reasons(self):
        _, why = logic.inbox_can_reply({"channel": "wa", "origin": "hook", "status": "new"},
                                       avito_ok=True)
        self.assertIn("wa.me", why)
        _, why = logic.inbox_can_reply({"channel": "tg", "origin": "bot", "status": "spam"},
                                       avito_ok=True)
        self.assertIn("спам", why)
        _, why = logic.inbox_can_reply({"channel": "avito", "origin": "avito_api",
                                        "status": "new"}, avito_ok=False)
        self.assertIn("приложении Авито", why)
        _, why = logic.inbox_can_reply({"channel": "tg", "origin": "hook", "status": "new"},
                                       avito_ok=True)
        self.assertIn("бот", why)

    def test_reply_limits_per_channel(self):
        self.assertEqual(logic.INBOX_REPLY_LIMITS["avito"], avito_api.MESSAGE_LIMIT,
                         "предел панели и клиента Авито - одно число")
        for channel, limit in (("tg", 3500), ("max", 3500), ("avito", 1000),
                               ("wa", 3500), ("неизвестный", 3500), (None, 3500)):
            fits = logic.check_inbox_reply(channel, "я" * limit)
            self.assertTrue(fits.ok, channel)
            self.assertEqual(len(fits.value), limit)
            over = logic.check_inbox_reply(channel, "я" * (limit + 1))
            self.assertFalse(over.ok, channel)
            self.assertIn(str(limit), over.error)
        for empty in ("", "   \n ", None):
            self.assertFalse(logic.check_inbox_reply("tg", empty).ok)
        self.assertEqual(logic.check_inbox_reply("tg", "  Приезжайте  ").value, "Приезжайте")
        # пробелы по краям в предел не считаются
        self.assertTrue(logic.check_inbox_reply("avito", "  " + "я" * 1000 + "\n").ok)

    def test_moment(self):
        at = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
        ts = int(at.timestamp())
        self.assertEqual(logic._moment(ts), at)
        self.assertEqual(logic._moment(float(ts)), at)
        self.assertEqual(logic._moment(str(ts)), at)
        self.assertEqual(logic._moment("2026-09-24T10:00:00Z"), at)
        self.assertEqual(logic._moment("2026-09-24T13:00:00+03:00"), at)
        self.assertEqual(logic._moment("2026-09-24T10:00:00.000Z"), at)
        self.assertEqual(logic._moment("2026-09-24T10:00:00"), at, "без пояса - UTC")
        for junk in (None, "", "вчера", True, False, [ts], {"t": ts}, 10 ** 20,
                     "9" * 30, float("nan"), float("inf")):
            self.assertIsNone(logic._moment(junk), repr(junk))


class TestParseInbound(unittest.TestCase):
    def test_normalized_single_whatsapp(self):
        items, skipped = logic.parse_inbound({
            "channel": "WA", "phone": "8 (900) 123-45-67", "name": "  Ильдар  ",
            "text": "Велосипед свободен?", "msg_id": "n8n-1",
            "at": "2026-09-24T10:00:00Z"})
        self.assertEqual(skipped, 0)
        self.assertEqual(items, [{
            "channel": "wa", "ext_id": "+79001234567", "msg_id": "n8n-1",
            "name": "Ильдар", "phone": "+79001234567", "text": "Велосипед свободен?",
            "kind": "text", "subject": None, "subject_url": None,
            "at": datetime(2026, 9, 24, 10, 0, tzinfo=UTC)}])

    def test_normalized_whatsapp_chat_id_becomes_phone(self):
        items, _ = logic.parse_inbound({"channel": "wa", "from": "79001234567@c.us",
                                        "text": "Здравствуйте"})
        self.assertEqual(items[0]["ext_id"], "+79001234567")
        self.assertEqual(items[0]["phone"], "+79001234567")

    def test_normalized_batch_and_refusals(self):
        items, skipped = logic.parse_inbound({"items": [
            {"channel": "avito", "ext_id": "u2i-abc", "id": "m-1", "text": "Торг уместен?",
             "subject": "Электровелосипед Monster", "kind": "голубь",
             "subject_url": "https://www.avito.ru/kazan/velosipedy/monster_1"},
            {"channel": "avito", "chat_id": "u2i-xyz", "message_id": "m-2",
             "subject_url": "javascript:alert(1)", "kind": "image",
             "name": "Я" * 500, "text": "ы" * 10000},
            # Telegram и MAX пишет только сам бот: чужой запрос с токеном
            # не должен заводить обращения на чужие tg_id.
            {"channel": "tg", "ext_id": "5001", "text": "Я клиент 5001"},
            {"channel": "max", "ext_id": "777", "text": "Я клиент 777"},
            {"channel": "wa"},                     # ни адреса, ни телефона
            {"channel": "wa", "phone": "123"},      # не телефон
            {"ext_id": "без канала"},
            "мусор", 42, None]})
        self.assertEqual(skipped, 8)
        self.assertEqual([i["ext_id"] for i in items], ["u2i-abc", "u2i-xyz"])
        first, second = items
        self.assertEqual(first["msg_id"], "m-1")
        self.assertEqual(first["kind"], "other", "неизвестный вид - вложение")
        self.assertEqual(first["subject_url"],
                         "https://www.avito.ru/kazan/velosipedy/monster_1")
        self.assertIsNone(first["phone"])
        self.assertEqual(second["msg_id"], "m-2")
        self.assertEqual(second["kind"], "image")
        self.assertIsNone(second["subject_url"])
        self.assertEqual(len(second["name"]), logic.INBOX_NAME_MAX)
        self.assertEqual(len(second["text"]), logic.INBOX_TEXT_MAX)

    def test_huge_batch_is_cut_and_counted(self):
        one = {"channel": "avito", "ext_id": "u2i-1", "text": "?"}
        items, skipped = logic.parse_inbound(
            {"items": [dict(one, id=f"m{i}") for i in range(logic.HOOK_BATCH_LIMIT + 10)]})
        self.assertEqual(len(items), logic.HOOK_BATCH_LIMIT)
        self.assertEqual(skipped, 10)

    def test_junk_payloads(self):
        for junk in ([{"channel": "wa", "phone": "+79001234567"}], "текст", None, 42,
                     b"bytes", {}, {"hello": "world"}, {"statuses": [{"status": "read"}]},
                     {"items": "не список", "channel": "мимо"}):
            items, skipped = logic.parse_inbound(junk)
            self.assertEqual(items, [], repr(junk))
            self.assertEqual(skipped, 1, repr(junk))

    def test_wazzup_probe(self):
        self.assertEqual(logic.parse_inbound({"test": True}), ([], 0))
        self.assertEqual(logic.parse_inbound({"test": True, "id": "x"}), ([], 0))
        # «test» строкой - это не проверка адреса, а чей-то JSON
        self.assertEqual(logic.parse_inbound({"test": "true"}), ([], 1))

    # ─ Green-API ─

    def green(self, type_message="textMessage", chat="79001234567@c.us", **data):
        return {"typeWebhook": "incomingMessageReceived", "idMessage": "BAE5F4886AD8",
                "timestamp": 1789000000,
                "senderData": {"chatId": chat, "sender": chat, "senderName": "Ильдар"},
                "messageData": {"typeMessage": type_message, **data}}

    def test_green_text(self):
        items, skipped = logic.parse_inbound(self.green(
            textMessageData={"textMessage": "Сколько стоит неделя?"}))
        self.assertEqual(skipped, 0)
        self.assertEqual(items, [{
            "channel": "wa", "ext_id": "+79001234567", "msg_id": "BAE5F4886AD8",
            "name": "Ильдар", "phone": "+79001234567", "text": "Сколько стоит неделя?",
            "kind": "text", "subject": None, "subject_url": None,
            "at": datetime.fromtimestamp(1789000000, UTC)}])

    def test_green_extended_text(self):
        items, _ = logic.parse_inbound(self.green(
            "extendedTextMessage",
            extendedTextMessageData={"text": "Вот объявление https://avito.ru/x"}))
        self.assertEqual(items[0]["text"], "Вот объявление https://avito.ru/x")
        self.assertEqual(items[0]["kind"], "text")

    def test_green_file_caption(self):
        items, _ = logic.parse_inbound(self.green(
            "imageMessage", fileMessageData={"caption": "Фото рамы",
                                             "downloadUrl": "https://x/y.jpg"}))
        self.assertEqual((items[0]["kind"], items[0]["text"]), ("image", "Фото рамы"))
        items, _ = logic.parse_inbound(self.green("audioMessage", fileMessageData={}))
        self.assertEqual((items[0]["kind"], items[0]["text"]), ("voice", None))
        items, _ = logic.parse_inbound(self.green("pollMessage"))
        self.assertEqual(items[0]["kind"], "other")

    def test_green_incoming_call(self):
        items, skipped = logic.parse_inbound({
            "typeWebhook": "incomingCall", "from": "79001234567@c.us",
            "idMessage": "CALL-1", "timestamp": 1789000000, "status": "offer"})
        self.assertEqual(skipped, 0)
        self.assertEqual((items[0]["kind"], items[0]["ext_id"], items[0]["msg_id"]),
                         ("call", "+79001234567", "CALL-1"))
        self.assertIsNone(items[0]["text"])

    def test_green_skips_groups_outgoing_and_service(self):
        group = self.green(chat="120363043968066561@g.us",
                           textMessageData={"textMessage": "всем привет"})
        self.assertEqual(logic.parse_inbound(group), ([], 1))
        for hook in ("outgoingMessageReceived", "outgoingAPIMessageReceived",
                     "outgoingMessageStatus", "stateInstanceChanged"):
            payload = self.green(textMessageData={"textMessage": "наше"})
            payload["typeWebhook"] = hook
            self.assertEqual(logic.parse_inbound(payload), ([], 1), hook)
        broken = self.green(textMessageData={"textMessage": "x"})
        broken["senderData"] = "79001234567@c.us"
        self.assertEqual(logic.parse_inbound(broken), ([], 1))
        call_from_group = {"typeWebhook": "incomingCall", "from": "1203-63@g.us"}
        self.assertEqual(logic.parse_inbound(call_from_group), ([], 1))

    # ─ Wazzup ─

    def test_wazzup(self):
        def message(**over):
            row = {"messageId": "w-1", "dateTime": "2026-09-24T10:00:00.000Z",
                   "channelId": "ch-1", "chatType": "whatsapp", "chatId": "79001234567",
                   "type": "text", "isEcho": False, "contact": {"name": "Ильдар"},
                   "text": "Привет"}
            row.update(over)
            return row

        items, skipped = logic.parse_inbound({"messages": [
            message(),
            message(messageId="w-2", isEcho=True, text="наш ответ"),
            message(messageId="w-3", chatType="avito", chatId="av-123", type="image",
                    text=None, contact={"name": "Покупатель"}),
            message(messageId="w-4", chatType="whatsgroup"),
            message(messageId="w-5", chatType="telegram"),
            message(messageId="w-6", type="missing_call", text=None),
            message(messageId="w-7", type="audio", text=None, contact="не словарь"),
            "мусор"]})
        self.assertEqual(skipped, 4)
        self.assertEqual([i["msg_id"] for i in items], ["w-1", "w-3", "w-6", "w-7"])
        wa, av, call, voice = items
        self.assertEqual((wa["channel"], wa["ext_id"], wa["phone"], wa["name"]),
                         ("wa", "+79001234567", "+79001234567", "Ильдар"))
        self.assertEqual(wa["at"], datetime(2026, 9, 24, 10, 0, tzinfo=UTC))
        self.assertEqual((av["channel"], av["ext_id"], av["phone"], av["kind"]),
                         ("avito", "av-123", None, "image"))
        self.assertEqual(call["kind"], "call")
        self.assertEqual(voice["kind"], "voice")
        self.assertIsNone(voice["name"])

    def test_wazzup_every_message_is_accounted(self):
        messages = [{"messageId": f"w-{i}", "chatType": "whatsapp",
                     "chatId": "79001234567", "type": "text", "text": "?"}
                    for i in range(logic.HOOK_BATCH_LIMIT + 5)]
        items, skipped = logic.parse_inbound({"messages": messages})
        self.assertEqual(len(items) + skipped, len(messages))


class TestInboxRows(unittest.TestCase):
    def thread(self, **over):
        row = {"id": 1, "channel": "avito", "status": "new", "name": None,
               "username": None, "phone": None, "client_name": None,
               "subject": None, "ext_id": "u2i-1", "waiting_since": None}
        row.update(over)
        return row

    def test_rows(self):
        now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
        rows = logic.inbox_rows([
            self.thread(waiting_since=now - timedelta(hours=3, minutes=59)),
            self.thread(id=2, channel="tg", status="work", name="Ильдар",
                        client_name="Хасанов Ильдар Ринатович", username="ildar"),
            self.thread(id=3, channel="wa", username="ildar", phone="+79001234567"),
            self.thread(id=4, channel="max", phone="+79001234567",
                        waiting_since=now + timedelta(minutes=5)),
            self.thread(id=5, channel="??", status="??",
                        waiting_since=datetime(2026, 9, 24, 9, 0))], now=now)
        first, second, third, fourth, fifth = rows
        self.assertEqual((first["no"], first["waiting_hours"], first["who"]),
                         ("ВХ-000001", 3, "ВХ-000001"))
        self.assertEqual((first["channel_label"], first["status_label"]), ("Авито", "Новое"))
        self.assertEqual(second["who"], "Хасанов Ильдар Ринатович", "карточка важнее имени")
        self.assertIsNone(second["waiting_hours"])
        self.assertEqual(second["status_label"], "В работе")
        self.assertEqual(third["who"], "@ildar")
        self.assertEqual(fourth["who"], "+79001234567")
        self.assertEqual(fourth["waiting_hours"], 0, "часы ожидания не бывают отрицательными")
        self.assertIsNone(fifth["waiting_hours"], "время без пояса не считается")
        self.assertEqual((fifth["channel_label"], fifth["status_label"]), ("??", "??"))

    def test_matches(self):
        rows = logic.inbox_rows([
            self.thread(id=7, name="Ильдар", phone="+79001234567",
                        subject="Электровелосипед Monster"),
            self.thread(id=8, username="courier_kzn", ext_id="u2i-zzz")])
        ildar, courier = rows
        for query in ("", "   ", None, "ильдар", "ИЛЬДАР", "monster", "вх-000007",
                      "9001234567", "+7 900 123-45-67", "u2i-1"):
            self.assertTrue(logic.inbox_matches(ildar, query), repr(query))
        for query in ("courier", "u2i-zzz"):
            self.assertFalse(logic.inbox_matches(ildar, query), query)
            self.assertTrue(logic.inbox_matches(courier, query), query)
        self.assertFalse(logic.inbox_matches(ildar, "5555"))
        self.assertFalse(logic.inbox_matches(ildar, "12 34"),
                         "короткие цифры не ищутся по телефону")

    def test_counts(self):
        now = datetime.now(UTC)
        counts = logic.inbox_counts([
            self.thread(status="new", waiting_since=now - timedelta(hours=2)),
            self.thread(status="new", waiting_since=now - timedelta(minutes=10)),
            self.thread(status="work", waiting_since=now - timedelta(hours=5)),
            self.thread(status="work"),
            self.thread(status="done", waiting_since=now - timedelta(hours=5)),
            self.thread(status="spam", waiting_since=now - timedelta(hours=5)),
            self.thread(status="new", waiting_since=datetime(2020, 1, 1))])
        self.assertEqual(counts, {"new": 3, "work": 2, "waiting": 2})
        self.assertEqual(logic.inbox_counts([]), {"new": 0, "work": 0, "waiting": 0})

    def test_avito_state(self):
        now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

        def state(value):
            return logic.avito_state({"inbox_avito_state": value}, now=now)

        self.assertEqual(logic.avito_state({}, now=now), {
            "configured": False, "ok": False, "live": False, "at": None, "error": ""})
        fresh = state(json.dumps({"ok": True, "at": (now - timedelta(minutes=5)).isoformat(),
                                  "error": ""}))
        self.assertEqual((fresh["configured"], fresh["ok"], fresh["live"]), (True, True, True))
        self.assertEqual(fresh["at"], now - timedelta(minutes=5))
        as_dict = state({"ok": True, "at": now.isoformat()})
        self.assertTrue(as_dict["live"], "jsonb приходит и словарём")
        stale = state(json.dumps({"ok": True, "at": (now - timedelta(minutes=16)).isoformat()}))
        self.assertEqual((stale["ok"], stale["live"]), (True, False),
                         "опрос остановился - отвечать в Авито из панели нельзя")
        failed = state(json.dumps({"ok": False, "at": now.isoformat(),
                                   "error": "402 — нет доступа" + "!" * 500}))
        self.assertEqual((failed["configured"], failed["ok"], failed["live"]),
                         (True, False, False))
        self.assertTrue(failed["error"].startswith("402"))
        self.assertEqual(len(failed["error"]), 300)
        # Опрос раз в полчаса: отметка 40-минутной давности - ещё живой опрос,
        # порог не меньше трёх кругов.
        slow = state(json.dumps({"ok": True, "every": 1800,
                                 "at": (now - timedelta(minutes=40)).isoformat()}))
        self.assertTrue(slow["live"])
        dead = state(json.dumps({"ok": True, "every": 1800,
                                 "at": (now - timedelta(minutes=91)).isoformat()}))
        self.assertFalse(dead["live"])
        junk = state(json.dumps({"ok": True, "every": "много",
                                 "at": (now - timedelta(minutes=16)).isoformat()}))
        self.assertFalse(junk["live"], "мусор в every - обычный порог")
        no_time = state(json.dumps({"ok": True, "at": "вчера"}))
        self.assertEqual((no_time["live"], no_time["at"]), (False, None))
        for garbage in ("не json", "[1, 2]", "42", '"строка"', "null", "{"):
            got = state(garbage)
            self.assertEqual((got["configured"], got["live"]), (False, False), garbage)

    def test_preview(self):
        self.assertEqual(logic.inbox_preview("  Привет,\n  как   дела  ", "text"),
                         "Привет, как дела")
        long = logic.inbox_preview("а" * 200, "text")
        self.assertEqual(len(long), 90)
        self.assertTrue(long.endswith("…"))
        self.assertEqual(logic.inbox_preview("а" * 90, "text"), "а" * 90)
        self.assertEqual(logic.inbox_preview("а" * 20, "text", limit=10), "а" * 9 + "…")
        self.assertEqual(logic.inbox_preview(None, "image"), "[фото]")
        self.assertEqual(logic.inbox_preview("", "call"), "[звонок]")
        self.assertEqual(logic.inbox_preview(None, "sticker"), "[sticker]")
        self.assertEqual(logic.inbox_preview(None, "text"), "")
        self.assertEqual(logic.inbox_preview(None, None), "")
        self.assertEqual(logic.inbox_preview("Фото рамы", "image"), "Фото рамы")

    def test_team_text_has_no_personal_data(self):
        thread = {"id": 42, "channel": "avito", "name": "Хасанов Ильдар",
                  "username": "ildar_h", "phone": "+79001234567",
                  "client_name": "Хасанов Ильдар Ринатович", "ext_id": "u2i-secret",
                  "subject": "Электровелосипед <Monster> & АКБ",
                  "text": "Мой паспорт 9204 123456", "last_body_enc": "v1:AAAA"}
        text = logic.inbox_team_text(thread)
        self.assertIn("ВХ-000042", text)
        self.assertIn("Авито", text)
        self.assertIn("Электровелосипед &lt;Monster&gt; &amp; АКБ", text)
        for secret in ("Ильдар", "Хасанов", "ildar", "79001234567", "9001234567",
                       "u2i-secret", "паспорт", "9204", "v1:"):
            self.assertNotIn(secret, text, secret)
        bare = logic.inbox_team_text({"id": 1, "channel": "wa", "phone": "+79001234567"})
        self.assertEqual(bare.count("\n"), 1, "без темы - две строки")
        self.assertIn("WhatsApp", bare)
        self.assertNotIn("9001234567", bare)


# ───────────────────────────── сервис ─────────────────────────────

@unittest.skipUnless(HAVE_DEPS, "нет зависимостей панели")
class InboxCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.crm = FakeCrm()
        self.vault = Vault.from_raw(KEY)
        self.ildar = await self.crm.create_client(
            full_name="Хасанов Ильдар Ринатович", phone="+79001234567", tg_id=5001)
        self.max_man = await self.crm.create_client(
            full_name="Петров Пётр", phone="+79990000002")
        await self.crm.update_client(self.max_man, max_id=777)

    async def thread(self, vault="default", **fields):
        values = {"channel": "tg", "origin": "bot", "ext_id": "5001",
                  "text": "Как продлить аренду?"}
        values.update(fields)
        got = await service.inbox_in(self.crm, self.vault if vault == "default" else vault,
                                     **values)
        return await self.crm.inbox_thread(got["thread_id"])

    def messages(self, thread_id, **where):
        return [m for m in sorted(self.crm.inbox_messages_.values(), key=lambda m: m["id"])
                if m["thread_id"] == thread_id
                and all(m.get(k) == v for k, v in where.items())]


class TestInboxIn(InboxCase):
    async def test_client_by_telegram(self):
        thread = await self.thread(username="@ildar_h", name="  Ильдар ")
        self.assertEqual(thread["client_id"], self.ildar)
        self.assertEqual(thread["client_name"], "Хасанов Ильдар Ринатович")
        self.assertEqual((thread["username"], thread["name"]), ("ildar_h", "Ильдар"))
        self.assertEqual(thread["status"], "new")
        self.assertIsNotNone(thread["waiting_since"])

    async def test_client_by_max(self):
        thread = await self.thread(channel="max", origin="max_bot", ext_id=777)
        self.assertEqual(thread["client_id"], self.max_man)
        self.assertEqual(thread["ext_id"], "777")

    async def test_client_by_phone(self):
        thread = await self.thread(channel="wa", origin="hook", ext_id="+79001234567",
                                   phone="8 900 123 45 67")
        self.assertEqual(thread["client_id"], self.ildar)
        self.assertEqual(thread["phone"], "+79001234567")
        # незнакомый tg_id, но телефон из контакта - тоже карточка
        other = await self.thread(ext_id="99999", phone="+7 999 000-00-02")
        self.assertEqual(other["client_id"], self.max_man)
        stranger = await self.thread(ext_id="88888", phone="+79990009999")
        self.assertIsNone(stranger["client_id"])

    async def test_address_is_required(self):
        for ext in ("", "   ", None):
            with self.assertRaises(service.ServiceError):
                await self.thread(ext_id=ext)
        self.assertEqual(self.crm.inbox_threads_, {})

    async def test_duplicate_message_is_ignored(self):
        first = await service.inbox_in(self.crm, self.vault, channel="avito",
                                       origin="avito_api", ext_id="u2i-1", msg_id="m-1",
                                       text="Свободен?")
        again = await service.inbox_in(self.crm, self.vault, channel="avito",
                                       origin="avito_api", ext_id="u2i-1", msg_id="m-1",
                                       text="Свободен?")
        self.assertIsNotNone(first["message_id"])
        self.assertIsNone(again["message_id"])
        self.assertEqual(again["thread_id"], first["thread_id"])
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertEqual(len(self.messages(first["thread_id"])), 1)

    async def test_text_is_encrypted(self):
        secret = "Мой паспорт 9204 123456, живу на Павлюхина"
        thread = await self.thread(text=secret)
        [message] = self.messages(thread["id"])
        self.assertTrue(message["body_enc"].startswith("v1:"))
        for piece in (secret, "9204", "Павлюхина", "паспорт"):
            self.assertNotIn(piece, message["body_enc"])
        self.assertEqual(service.inbox_open(self.vault, message["body_enc"]), secret)
        self.assertEqual(service.inbox_open(None, message["body_enc"]),
                         "[текст зашифрован, ключа INBOX_KEY нет]")
        with self.assertLogs("app.services.crypto", "ERROR"):
            self.assertEqual(service.inbox_open(Vault.from_raw(OTHER_KEY),
                                                message["body_enc"]), "[не расшифровано]")
        self.assertIsNone(service.inbox_open(self.vault, None))
        self.assertEqual(thread["last_body_enc"], message["body_enc"])

    async def test_long_text_is_cut(self):
        thread = await self.thread(text="ы" * 5000)
        [message] = self.messages(thread["id"])
        self.assertEqual(len(service.inbox_open(self.vault, message["body_enc"])),
                         logic.INBOX_TEXT_MAX)

    async def test_without_key_text_is_not_stored(self):
        thread = await self.thread(vault=None, text="Открытым текстом нельзя")
        [message] = self.messages(thread["id"])
        self.assertIsNone(message["body_enc"], "без ключа текст не пишется вовсе")
        self.assertIsNotNone(thread["waiting_since"], "но обращение и ожидание есть")

    async def test_attachment_without_text(self):
        thread = await self.thread(kind="image", text=None)
        [message] = self.messages(thread["id"])
        self.assertEqual((message["kind"], message["body_enc"]), ("image", None))

    async def test_foreign_subject_url_is_dropped(self):
        thread = await self.thread(channel="avito", origin="hook", ext_id="u2i-9",
                                   subject="Велосипед",
                                   subject_url="javascript:alert(document.cookie)")
        self.assertIsNone(thread["subject_url"])
        self.assertEqual(thread["subject"], "Велосипед")

    def test_vault(self):
        self.assertIsNone(service.inbox_vault(""))
        self.assertIsNone(service.inbox_vault(None))
        self.assertIsNone(service.inbox_vault("   "))
        with self.assertLogs("app.crm.service", "ERROR"):
            self.assertIsNone(service.inbox_vault("негодный-ключ-" + generate_key()))
        vault = service.inbox_vault(KEY)
        self.assertIsInstance(vault, Vault)
        self.assertIs(service.inbox_vault(f"  {KEY}\n"), vault, "ключ из файла с переводом")
        self.assertIsNone(service.inbox_seal(None, "текст"))
        self.assertIsNone(service.inbox_seal(vault, ""))
        self.assertIsNone(service.inbox_seal(vault, None))


class TestInboxReply(InboxCase):
    async def test_queues_exactly_one(self):
        thread = await self.thread()
        mid = await service.inbox_reply(self.crm, self.vault, thread, "  Продлим, приходите  ",
                                        by="admin", avito_ok=False)
        [queued] = self.messages(thread["id"], status="queued")
        self.assertEqual(queued["id"], mid)
        self.assertEqual((queued["direction"], queued["author"]), ("out", "admin"))
        self.assertNotIn("Продлим", queued["body_enc"])
        self.assertEqual(service.inbox_open(self.vault, queued["body_enc"]),
                         "Продлим, приходите")
        fresh = await self.crm.inbox_thread(thread["id"])
        self.assertEqual((fresh["status"], fresh["handled_by"]), ("work", "admin"))
        self.assertIsNotNone(fresh["waiting_since"], "ожидание снимет отправка, не очередь")
        with self.assertRaises(service.ServiceError) as err:
            await service.inbox_reply(self.crm, self.vault, fresh, "Ещё одно",
                                      by="admin", avito_ok=False)
        self.assertIn("ещё отправляется", str(err.exception))
        self.assertEqual(len(self.messages(thread["id"], direction="out")), 1)
        # пока первый «отправляется» - тоже нельзя
        await self.crm.claim_inbox_out()
        with self.assertRaises(service.ServiceError):
            await service.inbox_reply(self.crm, self.vault, fresh, "Ещё одно",
                                      by="admin", avito_ok=False)

    async def test_status_other_than_new_is_kept(self):
        thread = await self.thread()
        await service.inbox_set_status(self.crm, thread, "done", note=None, by="owner")
        thread = await self.crm.inbox_thread(thread["id"])
        await service.inbox_reply(self.crm, self.vault, thread, "Ответ", by="admin",
                                  avito_ok=False)
        fresh = await self.crm.inbox_thread(thread["id"])
        self.assertEqual((fresh["status"], fresh["handled_by"]), ("done", "owner"))

    async def test_refusals_queue_nothing(self):
        wa = await self.thread(channel="wa", origin="hook", ext_id="+79001234567")
        tg_hook = await self.thread(channel="tg", origin="hook", ext_id="5002")
        max_hook = await self.thread(channel="max", origin="hook", ext_id="778")
        avito = await self.thread(channel="avito", origin="avito_api", ext_id="u2i-1")
        spam = await self.thread(ext_id="6001")
        await service.inbox_set_status(self.crm, spam, "spam", note=None, by="admin")
        spam = await self.crm.inbox_thread(spam["id"])
        tg = await self.thread(ext_id="6002")
        cases = [
            (wa, "Ответ", True, self.vault, "wa.me"),
            (tg_hook, "Ответ", True, self.vault, "бот"),
            (max_hook, "Ответ", True, self.vault, "MAX"),
            (spam, "Ответ", True, self.vault, "спам"),
            (avito, "Ответ", False, self.vault, "Авито"),
            (tg, "Ответ", True, None, "INBOX_KEY"),
            (tg, "   ", True, self.vault, "текст"),
            (tg, "я" * 3501, True, self.vault, "3500"),
            (avito, "я" * 1001, True, self.vault, "1000"),
        ]
        for thread, text, avito_ok, vault, why in cases:
            with self.subTest(channel=thread["channel"], origin=thread["origin"], why=why):
                with self.assertRaises(service.ServiceError) as err:
                    await service.inbox_reply(self.crm, vault, thread, text,
                                              by="admin", avito_ok=avito_ok)
                self.assertIn(why, str(err.exception))
        self.assertEqual([m for m in self.crm.inbox_messages_.values()
                          if m["status"] in ("queued", "sending")], [])

    async def test_avito_and_max_when_allowed(self):
        avito = await self.thread(channel="avito", origin="avito_api", ext_id="u2i-1")
        self.assertIsNotNone(await service.inbox_reply(
            self.crm, self.vault, avito, "я" * 1000, by="admin", avito_ok=True))
        max_thread = await self.thread(channel="max", origin="max_bot", ext_id="777")
        self.assertIsNotNone(await service.inbox_reply(
            self.crm, self.vault, max_thread, "Ждём", by="admin", avito_ok=False))


class TestInboxActions(InboxCase):
    async def test_set_status(self):
        thread = await self.thread()
        with self.assertRaises(service.ServiceError):
            await service.inbox_set_status(self.crm, thread, "closed", note=None, by="admin")
        with self.assertRaises(service.ServiceError):
            await service.inbox_set_status(self.crm, thread, "done",
                                           note="я" * (logic.NOTE_LIMIT + 1), by="admin")
        fresh = await self.crm.inbox_thread(thread["id"])
        self.assertEqual((fresh["status"], fresh["note"], fresh["handled_by"]),
                         ("new", None, None), "отказ ничего не меняет")
        await service.inbox_set_status(self.crm, thread, "done", note="  Перезвонил  ",
                                       by="admin")
        fresh = await self.crm.inbox_thread(thread["id"])
        self.assertEqual((fresh["status"], fresh["note"], fresh["handled_by"]),
                         ("done", "Перезвонил", "admin"))
        self.assertIsNotNone(fresh["handled_at"])
        await service.inbox_set_status(self.crm, thread, "work", note=None, by="owner")
        fresh = await self.crm.inbox_thread(thread["id"])
        self.assertEqual((fresh["status"], fresh["note"], fresh["handled_by"]),
                         ("work", "Перезвонил", "owner"), "без поля заметка не стирается")
        await service.inbox_set_status(self.crm, thread, "work", note="", by="owner")
        self.assertIsNone((await self.crm.inbox_thread(thread["id"]))["note"])

    async def test_link_client(self):
        thread = await self.thread(channel="wa", origin="hook", ext_id="+79005550000")
        self.assertIsNone(thread["client_id"])
        got = await service.inbox_link_client(self.crm, thread, f" {self.ildar} ", by="admin")
        self.assertEqual(got["id"], self.ildar)
        self.assertEqual((await self.crm.inbox_thread(thread["id"]))["client_id"], self.ildar)
        got = await service.inbox_link_client(self.crm, thread, "8 (999) 000-00-02",
                                              by="admin")
        self.assertEqual(got["id"], self.max_man)
        fresh = await self.crm.inbox_thread(thread["id"])
        self.assertEqual((fresh["client_id"], fresh["handled_by"]), (self.max_man, "admin"))
        for unknown in ("999999", "+79995550000", "Ильдар", "12"):
            with self.assertRaises(service.ServiceError, msg=unknown):
                await service.inbox_link_client(self.crm, thread, unknown, by="admin")
        self.assertEqual((await self.crm.inbox_thread(thread["id"]))["client_id"],
                         self.max_man, "неудачная привязка старую не снимает")
        self.assertIsNone(await service.inbox_link_client(self.crm, thread, "  ", by="admin"))
        self.assertIsNone((await self.crm.inbox_thread(thread["id"]))["client_id"])

    async def test_answered_elsewhere(self):
        thread = await self.thread(channel="wa", origin="hook", ext_id="+79001234567")
        self.assertIsNotNone(thread["waiting_since"])
        await service.inbox_answered_elsewhere(self.crm, thread, by="admin")
        fresh = await self.crm.inbox_thread(thread["id"])
        self.assertIsNone(fresh["waiting_since"])
        self.assertEqual(fresh["status"], "work")
        self.assertIsNone(fresh["announced_at"], "сигнал о самом обращении не отменяется")
        [out] = self.messages(thread["id"], direction="out")
        self.assertEqual((out["author"], out["status"], out["body_enc"]),
                         ("admin", "sent", None))
        self.assertEqual(len(self.crm.inbox_threads_), 1, "второго обращения нет")

    async def test_retry(self):
        thread = await self.thread()
        mid = await service.inbox_reply(self.crm, self.vault, thread, "Ответ",
                                        by="admin", avito_ok=False)
        with self.assertRaises(service.ServiceError):
            await service.inbox_retry(self.crm, mid, by="admin")   # ещё в очереди
        await self.crm.claim_inbox_out()
        await self.crm.finish_inbox_out(mid, ok=False, error="bot was blocked")
        new_id = await service.inbox_retry(self.crm, mid, by="owner")
        self.assertNotEqual(new_id, mid)
        again = self.crm.inbox_messages_[new_id]
        self.assertEqual((again["status"], again["author"]), ("queued", "owner"))
        self.assertEqual(again["body_enc"], self.crm.inbox_messages_[mid]["body_enc"])
        self.assertEqual(self.crm.inbox_messages_[mid]["status"], "failed",
                         "не ушедший остаётся в истории")
        with self.assertRaises(service.ServiceError):
            await service.inbox_retry(self.crm, mid, by="owner")   # новый ещё в очереди
        await self.crm.claim_inbox_out()
        await self.crm.finish_inbox_out(new_id, ok=True)
        with self.assertRaises(service.ServiceError):
            await service.inbox_retry(self.crm, new_id, by="owner")  # ушедший не повторяем
        with self.assertRaises(service.ServiceError):
            await service.inbox_retry(self.crm, 999999, by="owner")


# ─────────────────────── процесс бота: запись и отправка ───────────────────────

class FakeBot:
    def __init__(self, fail: Exception | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if self.fail is not None:
            raise self.fail
        self.sent.append((chat_id, text))


class FakeMax:
    def __init__(self, fail: Exception | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send(self, *, user_id=None, chat_id=None, text=""):
        if self.fail is not None:
            raise self.fail
        self.sent.append((user_id, text))


class FakeUsers:
    """bot.users: отправке нужен только язык клиента."""

    def __init__(self, **langs: str) -> None:
        self.langs = {int(k.lstrip("u")): v for k, v in langs.items()}

    async def get_user(self, tg_id):
        lang = self.langs.get(tg_id)
        return {"tg_id": tg_id, "lang": lang} if lang else None


def chat_raw(chat_id: str, last_id: str, *, updated: datetime, name: str = "Ильдар",
             title: str = "Электровелосипед Monster",
             url: str = "https://www.avito.ru/kazan/velosipedy/monster_123") -> dict:
    return {"id": chat_id, "updated": int(updated.timestamp()),
            "users": [{"id": OWN, "name": "МАЙБАЙК"}, {"id": 555, "name": name}],
            "context": {"type": "item", "value": {"title": title, "url": url}},
            "last_message": {"id": last_id}}


def msg_raw(msg_id: str, text: str | None, *, at: datetime, author: int = 555,
            kind: str = "text", **content) -> dict:
    body = {"text": text} if text is not None else {}
    body.update(content)
    return {"id": msg_id, "author_id": author, "created": int(at.timestamp()),
            "type": kind, "content": body}


class FakeAvito:
    """Messenger API без сети: те же разборщики, что у настоящего клиента."""

    def __init__(self, chats=(), messages=None, *, ready: bool = True,
                 error: Exception | None = None, sent_id: str = "m-sent") -> None:
        self.ready = ready
        self.raw_chats = list(chats)
        self.raw_messages = dict(messages or {})
        self.error = error
        self.sent_id = sent_id
        self.sent: list[tuple[str, str]] = []
        self.message_calls: list[str] = []
        self.self_calls = 0

    async def self_id(self):
        self.self_calls += 1
        if self.error is not None:
            raise self.error
        return OWN

    async def chats(self):
        return [avito_api.parse_chat(c, OWN) for c in self.raw_chats]

    async def messages(self, chat_id):
        self.message_calls.append(chat_id)
        return [avito_api.parse_message(m, OWN) for m in self.raw_messages.get(chat_id, [])]

    async def send_text(self, chat_id, text):
        if self.error is not None:
            raise self.error
        self.sent.append((chat_id, text))
        return {"id": self.sent_id, "created": 1789000000}


class TestRecord(InboxCase):
    async def test_writes_and_finds_client(self):
        got = await inbox.record(self.crm, cfg(), channel="tg", origin="bot", ext_id=5001,
                                 text="Вопрос")
        thread = await self.crm.inbox_thread(got["thread_id"])
        self.assertEqual(thread["client_id"], self.ildar)
        [message] = self.messages(thread["id"])
        self.assertEqual(service.inbox_open(self.vault, message["body_enc"]), "Вопрос")

    async def test_never_raises(self):
        self.assertIsNone(await inbox.record(None, cfg(), channel="tg", origin="bot",
                                             ext_id=5001, text="x"))

        class Broken(FakeCrm):
            async def inbox_record(self, **kw):
                raise RuntimeError("duplicate key: text=Мой паспорт 9204 123456")

        with self.assertLogs("app.crm.inbox", "WARNING") as logs:
            got = await inbox.record(Broken(), cfg(), channel="tg", origin="bot",
                                     ext_id=5001, text="Мой паспорт 9204 123456")
        self.assertIsNone(got)
        out = "\n".join(logs.output)
        self.assertIn("tg/5001", out)
        self.assertIn("RuntimeError", out)
        for secret in ("паспорт", "9204", "duplicate key"):
            self.assertNotIn(secret, out, "в логе нет срока хранения - текста там нет")
        with self.assertLogs("app.crm.inbox", "WARNING"):
            self.assertIsNone(await inbox.record(self.crm, cfg(), channel="tg",
                                                 origin="bot", ext_id="", text="x"))

    async def test_without_key_still_records(self):
        got = await inbox.record(self.crm, cfg(inbox_key=""), channel="wa", origin="hook",
                                 ext_id="+79001234567", text="Здравствуйте")
        [message] = self.messages(got["thread_id"])
        self.assertIsNone(message["body_enc"])


class TestSendOnce(InboxCase):
    async def queued(self, text="Ответ", **fields):
        thread = await self.thread(**fields)
        mid = await service.inbox_reply(self.crm, self.vault, thread, text, by="admin",
                                        avito_ok=True)
        return thread, mid

    async def test_empty_queue(self):
        self.assertFalse(await inbox.send_once(FakeBot(), self.crm, cfg()))

    async def test_telegram(self):
        thread, mid = await self.queued("<b>Велосипед</b> & АКБ готовы")
        bot = FakeBot()
        self.assertTrue(await inbox.send_once(bot, self.crm, cfg()))
        want = texts.SUPPORT_REPLY_USER.format(
            answer="&lt;b&gt;Велосипед&lt;/b&gt; &amp; АКБ готовы")
        self.assertEqual(bot.sent, [(5001, want)])
        self.assertEqual(bot.sent[0][1], texts.SUPPORT_REPLY_USER.format(
            answer=bot_logic.esc("<b>Велосипед</b> & АКБ готовы")))
        message = self.crm.inbox_messages_[mid]
        self.assertEqual((message["status"], message["error"]), ("sent", None))
        fresh = await self.crm.inbox_thread(thread["id"])
        self.assertIsNone(fresh["waiting_since"], "ответ ушёл - ожидание снято")
        self.assertFalse(await inbox.send_once(bot, self.crm, cfg()))
        self.assertEqual(len(bot.sent), 1, "второй раз не шлём")

    async def test_telegram_in_clients_language(self):
        await self.queued("Ok")
        bot = FakeBot()
        await inbox.send_once(bot, self.crm, cfg(), db=FakeUsers(u5001="en"))
        self.assertEqual(bot.sent[0][1], "💬 Support reply:\n\nOk")

    async def test_telegram_failure_is_recorded(self):
        thread, mid = await self.queued()
        await inbox.send_once(FakeBot(fail=RuntimeError("Forbidden: bot was blocked")),
                              self.crm, cfg())
        message = self.crm.inbox_messages_[mid]
        self.assertEqual(message["status"], "failed")
        self.assertIn("blocked", message["error"])
        self.assertIsNotNone((await self.crm.inbox_thread(thread["id"]))["waiting_since"])

    async def test_max(self):
        _, mid = await self.queued("Цена <3000>", channel="max", origin="max_bot",
                                   ext_id="777")
        max_client, bot = FakeMax(), FakeBot()
        await inbox.send_once(bot, self.crm, cfg(), max_client=max_client)
        self.assertEqual(max_client.sent, [(777, texts.SUPPORT_REPLY_USER.format(
            answer="Цена &lt;3000&gt;"))])
        self.assertEqual(bot.sent, [], "в Telegram MAX-ответ не уходит")
        self.assertEqual(self.crm.inbox_messages_[mid]["status"], "sent")

    async def test_max_without_client_fails(self):
        _, mid = await self.queued(channel="max", origin="max_bot", ext_id="777")
        await inbox.send_once(FakeBot(), self.crm, cfg(), max_client=None)
        message = self.crm.inbox_messages_[mid]
        self.assertEqual(message["status"], "failed", "пропущенный - это не ушедший")
        self.assertIn("MAX", message["error"])

    async def test_avito(self):
        _, mid = await self.queued("Цена <3000> & торг", channel="avito",
                                   origin="avito_api", ext_id="u2i-1")
        avito = FakeAvito(sent_id="m-100")
        await inbox.send_once(FakeBot(), self.crm, cfg(), avito=avito)
        self.assertEqual(avito.sent, [("u2i-1", "Цена <3000> & торг")],
                         "в Авито текст как есть: разметки там нет")
        message = self.crm.inbox_messages_[mid]
        self.assertEqual((message["status"], message["ext_id"]), ("sent", "m-100"))

    async def test_avito_not_connected_or_failing(self):
        for avito, why in ((None, "не подключён"), (FakeAvito(ready=False), "не подключён"),
                           (FakeAvito(error=AvitoError("429 — Авито просит реже", 429)),
                            "Авито: 429")):
            with self.subTest(why=why):
                self.crm = FakeCrm()
                _, mid = await self.queued(channel="avito", origin="avito_api",
                                           ext_id="u2i-1")
                await inbox.send_once(FakeBot(), self.crm, cfg(), avito=avito)
                message = self.crm.inbox_messages_[mid]
                self.assertEqual(message["status"], "failed")
                self.assertIn(why, message["error"])

    async def test_spam_is_not_sent(self):
        thread, mid = await self.queued()
        await service.inbox_set_status(self.crm, thread, "spam", note=None, by="admin")
        bot = FakeBot()
        await inbox.send_once(bot, self.crm, cfg())
        self.assertEqual(bot.sent, [])
        message = self.crm.inbox_messages_[mid]
        self.assertEqual(message["status"], "failed")
        self.assertIn("спам", message["error"])

    async def test_undecryptable_is_not_sent(self):
        for key in (OTHER_KEY, ""):
            with self.subTest(key=bool(key)):
                self.crm = FakeCrm()
                _, mid = await self.queued("Секретный ответ")
                bot = FakeBot()
                if key:
                    with self.assertLogs("app.services.crypto", "ERROR"):
                        await inbox.send_once(bot, self.crm, cfg(inbox_key=key))
                else:
                    await inbox.send_once(bot, self.crm, cfg(inbox_key=key))
                self.assertEqual(bot.sent, [], "вместо ответа человеку не уйдёт заглушка")
                message = self.crm.inbox_messages_[mid]
                self.assertEqual(message["status"], "failed")
                self.assertIn("INBOX_KEY", message["error"])

    async def test_channel_without_sender(self):
        # В WhatsApp и туда, куда бот сам не писал, очередь не отправляет,
        # даже если строка в неё как-то попала.
        for fields in ({"channel": "wa", "origin": "hook", "ext_id": "+79001234567"},
                       {"channel": "tg", "origin": "hook", "ext_id": "5001"},
                       {"channel": "max", "origin": "hook", "ext_id": "777"},
                       {"channel": "tg", "origin": "bot", "ext_id": "ildar_h"}):
            with self.subTest(**fields):
                self.crm = FakeCrm()
                thread = await self.thread(**fields)
                mid = await self.crm.queue_inbox_reply(
                    thread["id"], body_enc=service.inbox_seal(self.vault, "Ответ"),
                    author="admin")
                bot, max_client = FakeBot(), FakeMax()
                await inbox.send_once(bot, self.crm, cfg(), max_client=max_client,
                                      avito=FakeAvito())
                self.assertEqual((bot.sent, max_client.sent), ([], []))
                message = self.crm.inbox_messages_[mid]
                self.assertEqual(message["status"], "failed")
                self.assertIn("не пишет", message["error"])


class TestAnnounce(InboxCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.thread(name="Хасанов Ильдар", phone="+79001234567",
                          text="Мой паспорт 9204 123456")
        await self.thread(channel="avito", origin="avito_api", ext_id="u2i-1",
                          subject="Электровелосипед Monster", text="Свободен?")

    async def test_once_per_thread_without_personal_data(self):
        bot = FakeBot()
        self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), 2)
        self.assertEqual([chat for chat, _ in bot.sent], [ADMIN_CHAT, ADMIN_CHAT])
        joined = "\n".join(text for _, text in bot.sent)
        self.assertIn("Авито", joined)
        self.assertIn("Электровелосипед Monster", joined)
        for secret in ("Ильдар", "Хасанов", "9001234567", "паспорт", "Свободен"):
            self.assertNotIn(secret, joined)
        self.assertTrue(all(t["announced_at"] for t in self.crm.inbox_threads_.values()))
        self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), 0)
        self.assertEqual(len(bot.sent), 2)

    async def test_marked_even_when_sending_fails(self):
        bot = FakeBot(fail=RuntimeError("chat not found"))
        with self.assertLogs("app.crm.notices", "WARNING"):
            self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), 2)
        self.assertTrue(all(t["announced_at"] for t in self.crm.inbox_threads_.values()))
        self.assertEqual({n["status"] for n in self.crm.notice_log_}, {"failed"})
        self.assertEqual(await inbox.announce_once(FakeBot(), self.crm, cfg()), 0,
                         "залпа после починки чата нет")

    async def test_marked_when_switched_off_or_without_chat(self):
        await self.crm.set_notice("inbox_new", enabled=False, at_hour=None)
        bot = FakeBot()
        self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), 2)
        self.assertEqual(bot.sent, [])
        await self.thread(ext_id="6001")
        await self.crm.set_notice("inbox_new", enabled=True, at_hour=None)
        self.assertEqual(await inbox.announce_once(bot, self.crm,
                                                   cfg(admin_chat_id=None)), 1)
        self.assertEqual(bot.sent, [])
        self.assertTrue(all(t["announced_at"] for t in self.crm.inbox_threads_.values()))

    async def test_marked_when_notice_code_breaks(self):
        async def broken(*args, **kwargs):
            raise RuntimeError("сбой")

        with mock.patch.object(inbox.notices, "send_team", broken), \
                self.assertLogs("app.crm.inbox", "WARNING"):
            self.assertEqual(await inbox.announce_once(FakeBot(), self.crm, cfg()), 2)
        self.assertTrue(all(t["announced_at"] for t in self.crm.inbox_threads_.values()))

    async def test_answer_of_our_own_is_not_announced(self):
        crm = FakeCrm()
        await inbox.record(crm, cfg(), channel="avito", origin="avito_api", ext_id="u2i-7",
                           direction="out", author="avito-app", announce=False, text="Здравствуйте")
        self.assertEqual(await inbox.announce_once(FakeBot(), crm, cfg()), 0)


# ─────────────────────────── опрос Авито ───────────────────────────

class TestAvitoPoll(InboxCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.now = datetime.now(UTC)
        def ago(**kw):
            return self.now - timedelta(**kw)

        self.avito = FakeAvito(
            [chat_raw("u2i-1", "c1-m4", updated=ago(minutes=5))],
            {"u2i-1": [
                msg_raw("c1-m0", "Позапрошлогодний вопрос", at=ago(days=2)),
                msg_raw("c1-sys", "Пользователь создал чат", at=ago(minutes=30),
                        kind="system"),
                msg_raw("c1-stub", "Чтобы ответить, перейдите на подписку",
                        at=ago(minutes=29)),
                msg_raw("c1-bot", "Автоответ", at=ago(minutes=28), flow_id="flow-1"),
                msg_raw("c1-m1", "Добрый день, велосипед свободен?", at=ago(minutes=20)),
                msg_raw("c1-img", None, at=ago(minutes=19), kind="image",
                        image={"sizes": {}}),
                msg_raw("c1-m4", "Да, приезжайте на Павлюхина", at=ago(minutes=10),
                        author=OWN),
            ]})

    async def avito_thread(self):
        [thread] = await self.crm.inbox_threads(channel="avito")
        return thread

    async def test_first_run(self):
        counts = await inbox.avito_once(self.crm, self.avito, cfg())
        self.assertEqual(counts, {"chats": 1, "messages": 3})
        since = logic._moment(self.crm.settings_["inbox_avito_since"])
        self.assertAlmostEqual(since.timestamp(),
                               (self.now - inbox.AVITO_FIRST_LOOKBACK).timestamp(), delta=60)
        thread = await self.avito_thread()
        self.assertEqual((thread["origin"], thread["ext_id"], thread["name"]),
                         ("avito_api", "u2i-1", "Ильдар"))
        self.assertEqual((thread["subject"], thread["subject_url"]),
                         ("Электровелосипед Monster",
                          "https://www.avito.ru/kazan/velosipedy/monster_123"))
        self.assertEqual(thread["ext_cursor"], "c1-m4")
        self.assertIsNone(thread["waiting_since"], "наш ответ из приложения снял ожидание")
        self.assertEqual(thread["status"], "work")
        self.assertIsNone(thread["announced_at"], "обращение клиента - сигнал в чат")
        got = self.messages(thread["id"])
        self.assertEqual([m["ext_id"] for m in got], ["c1-m1", "c1-img", "c1-m4"],
                         "заглушки, служебные и старое - мимо")
        self.assertEqual([m["direction"] for m in got], ["in", "in", "out"])
        self.assertEqual(got[1]["kind"], "image")
        self.assertEqual((got[2]["author"], got[2]["status"]), ("avito-app", "sent"))
        self.assertEqual(service.inbox_open(self.vault, got[0]["body_enc"]),
                         "Добрый день, велосипед свободен?")
        self.assertNotIn("свободен", got[0]["body_enc"])
        state = logic.avito_state(self.crm.settings_)
        self.assertEqual((state["configured"], state["ok"], state["live"]), (True, True, True))

    async def test_cursor_skips_unchanged_chat(self):
        await inbox.avito_once(self.crm, self.avito, cfg())
        self.assertEqual(self.avito.message_calls, ["u2i-1"])
        again = await inbox.avito_once(self.crm, self.avito, cfg())
        self.assertEqual(again, {"chats": 0, "messages": 0})
        self.assertEqual(self.avito.message_calls, ["u2i-1"], "чат не перечитывался")
        # новое сообщение - чат читается снова, старые не дублируются
        self.avito.raw_chats = [chat_raw("u2i-1", "c1-m5", updated=self.now)]
        self.avito.raw_messages["u2i-1"].append(
            msg_raw("c1-m5", "А аккумулятор второй есть?", at=self.now))
        third = await inbox.avito_once(self.crm, self.avito, cfg())
        self.assertEqual(third, {"chats": 1, "messages": 1})
        thread = await self.avito_thread()
        self.assertEqual(len(self.messages(thread["id"])), 4)
        self.assertEqual(thread["ext_cursor"], "c1-m5")
        self.assertIsNotNone(thread["waiting_since"], "клиент снова ждёт")

    async def test_own_message_clears_waiting(self):
        self.avito.raw_chats = [chat_raw("u2i-1", "c1-m1", updated=self.now)]
        self.avito.raw_messages["u2i-1"] = self.avito.raw_messages["u2i-1"][:5]
        await inbox.avito_once(self.crm, self.avito, cfg())
        thread = await self.avito_thread()
        self.assertIsNotNone(thread["waiting_since"])
        self.assertEqual(thread["status"], "new")
        self.avito.raw_chats = [chat_raw("u2i-1", "c1-m9", updated=self.now)]
        self.avito.raw_messages["u2i-1"].append(
            msg_raw("c1-m9", "Ответил из телефона", at=self.now, author=OWN))
        await inbox.avito_once(self.crm, self.avito, cfg())
        thread = await self.avito_thread()
        self.assertIsNone(thread["waiting_since"])
        self.assertEqual(thread["status"], "work")
        [out] = self.messages(thread["id"], direction="out")
        self.assertEqual(out["author"], "avito-app")

    async def test_only_our_messages_open_no_signal(self):
        self.avito.raw_chats = [chat_raw("u2i-2", "c2-m1", updated=self.now)]
        self.avito.raw_messages = {"u2i-2": [msg_raw("c2-m1", "Мы написали первыми",
                                                     at=self.now, author=OWN)]}
        await inbox.avito_once(self.crm, self.avito, cfg())
        thread = await self.avito_thread()
        self.assertIsNotNone(thread["announced_at"], "своё сообщение - не обращение")
        self.assertIsNone(thread["waiting_since"])

    async def test_old_and_empty_chats_are_skipped(self):
        self.avito.raw_chats = [
            chat_raw("u2i-old", "old-m1", updated=self.now - timedelta(days=3)),
            {"id": "u2i-empty", "updated": int(self.now.timestamp()), "users": []}]
        counts = await inbox.avito_once(self.crm, self.avito, cfg())
        self.assertEqual(counts, {"chats": 0, "messages": 0})
        self.assertEqual(self.avito.message_calls, [])
        self.assertEqual(self.crm.inbox_threads_, {})

    async def test_since_is_kept(self):
        since = self.now - timedelta(minutes=15)
        self.crm.settings_["inbox_avito_since"] = since.isoformat()
        counts = await inbox.avito_once(self.crm, self.avito, cfg())
        self.assertEqual(self.crm.settings_["inbox_avito_since"], since.isoformat())
        self.assertEqual(counts["messages"], 1, "до отметки подключения - архив, не обращения")
        thread = await self.avito_thread()
        self.assertEqual([m["ext_id"] for m in self.messages(thread["id"])], ["c1-m4"])

    async def test_panel_reply_is_not_duplicated(self):
        self.avito.raw_chats = [chat_raw("u2i-1", "c1-m1", updated=self.now)]
        self.avito.raw_messages["u2i-1"] = self.avito.raw_messages["u2i-1"][:5]
        await inbox.avito_once(self.crm, self.avito, cfg())
        thread = await self.avito_thread()
        await service.inbox_reply(self.crm, self.vault, thread, "Свободен, приезжайте",
                                  by="admin", avito_ok=True)
        self.avito.sent_id = "c1-m7"
        await inbox.send_once(FakeBot(), self.crm, cfg(), avito=self.avito)
        self.assertEqual(self.avito.sent, [("u2i-1", "Свободен, приезжайте")])
        # тот же ответ приходит опросом как «своё» сообщение
        self.avito.raw_chats = [chat_raw("u2i-1", "c1-m7", updated=self.now)]
        self.avito.raw_messages["u2i-1"].append(
            msg_raw("c1-m7", "Свободен, приезжайте", at=self.now, author=OWN))
        counts = await inbox.avito_once(self.crm, self.avito, cfg())
        self.assertEqual(counts["messages"], 0)
        out = self.messages(thread["id"], direction="out")
        self.assertEqual([(m["author"], m["ext_id"]) for m in out], [("admin", "c1-m7")])

    async def test_error_is_written_and_raised(self):
        avito = FakeAvito(error=AvitoError("402 — нет доступа к API сообщений", 402))
        with self.assertRaises(AvitoError):
            await inbox.avito_once(self.crm, avito, cfg())
        state = logic.avito_state(self.crm.settings_)
        self.assertEqual((state["configured"], state["ok"], state["live"]),
                         (True, False, False))
        self.assertIn("402", state["error"])
        # починили - отметка снова живая
        await inbox.avito_once(self.crm, self.avito, cfg())
        self.assertTrue(logic.avito_state(self.crm.settings_)["live"])

    async def test_failed_write_does_not_lose_the_message(self):
        # Первый круг - обращение есть, курсор на c1-m4.
        await inbox.avito_once(self.crm, self.avito, cfg())
        self.avito.raw_chats = [chat_raw("u2i-1", "c1-m5", updated=self.now)]
        self.avito.raw_messages["u2i-1"].append(
            msg_raw("c1-m5", "Когда можно забрать?", at=self.now))
        # Второй круг: база на миг не записала сообщение. record() это
        # проглатывает - значит, курсор не должен уехать за него.
        real = self.crm.inbox_record

        async def flaky(**kw):
            raise RuntimeError("connection reset")

        self.crm.inbox_record = flaky
        with self.assertLogs("app.crm.inbox", "WARNING"):
            await inbox.avito_once(self.crm, self.avito, cfg())
        self.crm.inbox_record = real
        # Третий круг, чат не менялся: сообщение клиента должно дойти.
        await inbox.avito_once(self.crm, self.avito, cfg())
        thread = await self.avito_thread()
        self.assertIn("c1-m5", [m["ext_id"] for m in self.messages(thread["id"])],
                      "вопрос клиента потерян, пока он не напишет ещё раз")


class TestInboxLoop(InboxCase):
    class Stop(Exception):
        pass

    async def run_loop(self, avito, *, rounds: int = 2):
        clock = [1000.0]
        calls = [0]

        async def sleep(_seconds):
            calls[0] += 1
            clock[0] += 100             # больше интервала опроса, меньше паузы 402
            if calls[0] >= rounds:
                raise self.Stop

        fake_asyncio = types.SimpleNamespace(sleep=sleep,
                                             CancelledError=inbox.asyncio.CancelledError)
        fake_time = types.SimpleNamespace(monotonic=lambda: clock[0])
        with mock.patch.object(inbox, "asyncio", fake_asyncio), \
                mock.patch.object(inbox, "time", fake_time):
            with self.assertRaises(self.Stop):
                await inbox.inbox_loop(FakeBot(), self.crm, cfg(), avito=avito)

    async def test_stuck_sending_is_failed_at_start(self):
        thread = await self.thread()
        mid = await service.inbox_reply(self.crm, self.vault, thread, "Ответ",
                                        by="admin", avito_ok=False)
        await self.crm.claim_inbox_out()            # «отправляется», и процесс упал
        with self.assertLogs("app.crm.inbox", "WARNING"):
            await self.run_loop(None, rounds=1)
        message = self.crm.inbox_messages_[mid]
        self.assertEqual(message["status"], "failed", "повтор после таймаута - дубль человеку")

    async def test_queue_is_sent_and_threads_announced(self):
        thread = await self.thread()
        mid = await service.inbox_reply(self.crm, self.vault, thread, "Ответ",
                                        by="admin", avito_ok=False)
        await self.run_loop(None, rounds=1)
        self.assertEqual(self.crm.inbox_messages_[mid]["status"], "sent")
        self.assertIsNotNone(self.crm.inbox_threads_[thread["id"]]["announced_at"])

    async def test_402_pauses_polling(self):
        avito = FakeAvito(error=AvitoError("402 — нет доступа", 402))
        with self.assertLogs("app.crm.inbox", "WARNING"):
            await self.run_loop(avito, rounds=3)
        self.assertEqual(avito.self_calls, 1, "без подписки Авито не дёргаем каждую минуту")

    async def test_other_errors_retry_next_round(self):
        avito = FakeAvito(error=AvitoError("429 — Авито просит реже", 429))
        with self.assertLogs("app.crm.inbox", "WARNING"):
            await self.run_loop(avito, rounds=3)
        self.assertEqual(avito.self_calls, 3)

    async def test_not_configured_is_not_polled(self):
        avito = FakeAvito(ready=False)
        await self.run_loop(avito, rounds=2)
        self.assertEqual(avito.self_calls, 0)
        # Прежняя отметка опроса сброшена: плашка «опрос не работает» не висит.
        self.assertFalse(logic.avito_state(self.crm.settings_)["configured"])

    async def test_round_failure_does_not_stop_the_loop(self):
        calls = [0]

        async def broken(limit=20):
            calls[0] += 1
            raise RuntimeError("база недоступна")

        self.crm.inbox_to_announce = broken
        with self.assertLogs("app.crm.inbox", "ERROR"):
            await self.run_loop(None, rounds=3)
        self.assertEqual(calls[0], 3)


if __name__ == "__main__":
    unittest.main()
