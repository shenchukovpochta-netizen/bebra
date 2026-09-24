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
    from app import faq_i18n, texts
    from app import logic as bot_logic
    from app.crm import company, inbox, service
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

    def test_avito_link_whitespace_control_and_login(self):
        # Пробел, управляющий символ или логин в адресе у ссылки на
        # объявление не встречаются - только у подделки: браузер режет
        # таб и перевод строки молча и увидел бы другой адрес.
        for bad in ("https://www.avito.ru/kazan x",
                    "https://www.avito.ru/\tkazan/x",
                    "https://www.avito.ru/\nkazan/x",
                    "https://www.avito.ru/kazan\x00/x",
                    "https://www.avito.ru/kazan\x1b/x",
                    "https://www.avito.ru/ kazan",
                    "https://evil.com\t.avito.ru/",
                    "https://user@www.avito.ru/kazan/x",
                    "https://user:pass@avito.ru/kazan/x",
                    "https://:@avito.ru/"):
            self.assertIsNone(logic.safe_avito_url(bad), repr(bad))
        # обычная ссылка с запросом и якорем - проходит
        good = "https://www.avito.ru/kazan/velosipedy/monster_123?utm=1#photo"
        self.assertEqual(logic.safe_avito_url(good), good)

    def test_tme_only_for_telegram(self):
        # Логин MAX - не логин Telegram: t.me по нему вёл бы к постороннему.
        for channel in ("max", "avito", "wa", "??"):
            links = logic.inbox_links({"channel": channel, "username": "ildar_h",
                                       "phone": "+79001234567"})
            self.assertIsNone(links["tg"], channel)
            self.assertEqual(links["wa"], "https://wa.me/79001234567", channel)
        for channel in ("tg", None):
            self.assertEqual(logic.inbox_links({"channel": channel,
                                                "username": "@ildar_h"})["tg"],
                             "https://t.me/ildar_h", repr(channel))

    def test_cut_drops_nul(self):
        # NUL Postgres в text не принимает: одна такая строка от шлюза
        # роняла бы всю пачку хука на каждой повторной доставке.
        self.assertEqual(logic._cut("Иль\x00дар", 120), "Ильдар")
        self.assertEqual(logic._cut("  \x00Ильдар\x00  ", 120), "Ильдар")
        self.assertEqual(logic._cut("а\x00" * 5, 3), "ааа", "предел - после чистки")
        for empty in ("\x00", " \x00 ", "", None):
            self.assertIsNone(logic._cut(empty, 10), repr(empty))

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

    def test_avito_reply_only_into_polled_chats(self):
        # ext_id шлюза - не номер чата Авито: даже при живом опросе ответ
        # через API ушёл бы в никуда.
        for origin in ("hook", "bot", "max_bot"):
            ok, why = logic.inbox_can_reply(
                {"channel": "avito", "origin": origin, "status": "new"}, avito_ok=True)
            self.assertFalse(ok, origin)
            self.assertIn("шлюз", why, origin)
        self.assertEqual(logic.inbox_can_reply(
            {"channel": "avito", "origin": "avito_api", "status": "work"}, avito_ok=True),
            (True, ""))

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

    def test_reply_crlf_is_one_character(self):
        # Форма шлёт перевод строки как CRLF, а maxlength браузера считает
        # его одним знаком: ответ, который браузер пропустил, проходит и здесь.
        lines = ["я"] * 500
        crlf = "\r\n".join(lines)
        self.assertEqual(len(crlf), 1498)
        got = logic.check_inbox_reply("avito", crlf)
        self.assertTrue(got.ok, got.error)
        self.assertEqual(got.value, "\n".join(lines))
        self.assertNotIn("\r", got.value)
        self.assertEqual(logic.check_inbox_reply("tg", "Привет\rПока").value, "Привет\nПока")
        over = logic.check_inbox_reply("avito", "\r\n".join(["я"] * 501))
        self.assertFalse(over.ok, "1001 знак и после нормализации - больше предела")
        self.assertIn("1000", over.error)
        self.assertFalse(logic.check_inbox_reply("tg", "\r\n\r\n").ok, "одни переводы - пусто")

    def test_moment_naive_is_moscow_and_digits_are_ascii(self):
        at = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
        self.assertEqual(logic._moment("2026-09-24 13:00:00"), at, "пробел вместо T")
        # В Москве нет перевода часов: зимой те же +3.
        self.assertEqual(logic._moment("2026-01-15T13:00:00"),
                         datetime(2026, 1, 15, 10, 0, tzinfo=UTC))
        naive = logic._moment("2026-09-24T13:00:00")
        self.assertIsNotNone(naive.tzinfo, "время без пояса не остаётся «наивным»")
        # isdigit верит арабским, полноширинным и надстрочным цифрам - int()
        # и время на них падать не должны.
        for junk in ("١٢٣", "１２３", "²", "①", "1" * 5000, "٠" * 10):
            self.assertIsNone(logic._moment(junk), repr(junk))
        self.assertEqual(logic._moment(f"  {int(at.timestamp())}  "), at)

    def test_moment(self):
        at = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
        ts = int(at.timestamp())
        self.assertEqual(logic._moment(ts), at)
        self.assertEqual(logic._moment(float(ts)), at)
        self.assertEqual(logic._moment(str(ts)), at)
        self.assertEqual(logic._moment("2026-09-24T10:00:00Z"), at)
        self.assertEqual(logic._moment("2026-09-24T13:00:00+03:00"), at)
        self.assertEqual(logic._moment("2026-09-24T10:00:00.000Z"), at)
        # Без пояса - московское время: система живёт в Europe/Moscow, и n8n
        # на том же сервере шлёт местное время.
        self.assertEqual(logic._moment("2026-09-24T13:00:00"), at, "без пояса - Москва")
        for junk in (None, "", "вчера", True, False, [ts], {"t": ts}, 10 ** 20,
                     "9" * 30, float("nan"), float("inf"), "²", "①", "1" * 5000):
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

    def test_wazzup_excess_is_counted_as_skipped(self):
        # Ответ 200 шлюз считает доставкой: сверх предела сообщения не
        # пишутся, но и не пропадают молча - они в счёте пропущенных.
        def message(i, **over):
            row = {"messageId": f"w-{i}", "chatType": "whatsapp", "chatId": "79001234567",
                   "type": "text", "text": "?"}
            row.update(over)
            return row

        messages = [message(i) for i in range(logic.HOOK_BATCH_LIMIT + 7)]
        messages[0]["isEcho"] = True               # наше же - тоже пропуск
        items, skipped = logic.parse_inbound({"messages": messages})
        self.assertEqual(len(items), logic.HOOK_BATCH_LIMIT - 1)
        self.assertEqual(skipped, 7 + 1)
        self.assertEqual(items[-1]["msg_id"], f"w-{logic.HOOK_BATCH_LIMIT - 1}",
                         "пишутся первые по порядку")


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

    def test_team_summary_has_no_personal_data(self):
        # Сводка вместо пачки сигналов: сколько и откуда, без имён,
        # телефонов, логинов и слов человека.
        threads = [
            {"id": 1, "channel": "avito", "name": "Хасанов Ильдар", "ext_id": "u2i-secret",
             "subject": "Электровелосипед Monster", "text": "Мой паспорт 9204 123456"},
            {"id": 2, "channel": "avito", "name": "Петров Пётр", "phone": "+79990000002"},
            {"id": 3, "channel": "wa", "phone": "+79001234567", "ext_id": "+79001234567"},
            {"id": 4, "channel": "tg", "username": "ildar_h", "ext_id": "5001",
             "client_name": "Хасанов Ильдар Ринатович", "last_body_enc": "v1:AAAA"},
            {"id": 5, "channel": "??"}]
        text = logic.inbox_team_summary(iter(threads))       # хватает одного прохода
        self.assertIn("Новых обращений: 5", text)
        for part in ("Авито — 2", "WhatsApp — 1", "Telegram — 1", "?? — 1"):
            self.assertIn(part, text)
        self.assertIn("«Входящие»", text)
        for secret in ("Ильдар", "Хасанов", "Петров", "ildar", "79001234567", "9001234567",
                       "9990000002", "u2i-secret", "5001", "паспорт", "9204", "v1:"):
            self.assertNotIn(secret, text, secret)


# ───────────────────────────── сервис ─────────────────────────────

@unittest.skipUnless(HAVE_DEPS, "нет зависимостей панели")
class InboxCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Память неудачных сигналов - на процесс: номера обращений у
        # FakeCrm повторяются от теста к тесту.
        inbox._announce_fails.clear()
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

    async def test_nul_is_dropped_everywhere(self):
        # NUL в любом поле от шлюза - не 500 на каждой повторной доставке,
        # а то же обращение без этого знака.
        thread = await self.thread(channel="avito", origin="hook", ext_id="u2i-\x009",
                                   msg_id="m-\x001", name="Иль\x00дар",
                                   username="@ildar\x00_h", subject="Вело\x00сипед",
                                   text="Свобо\x00ден?")
        self.assertEqual((thread["ext_id"], thread["name"], thread["username"],
                          thread["subject"]), ("u2i-9", "Ильдар", "ildar_h", "Велосипед"))
        [message] = self.messages(thread["id"])
        self.assertEqual(message["ext_id"], "m-1")
        for key, value in {**thread, **message}.items():
            if isinstance(value, str):
                self.assertNotIn("\x00", value, key)
        # повтор той же доставки - тот же адрес и то же сообщение, не дубль
        again = await service.inbox_in(self.crm, self.vault, channel="avito", origin="hook",
                                       ext_id="u2i-9", msg_id="m-1", text="Свободен?")
        self.assertEqual(again["thread_id"], thread["id"])
        self.assertIsNone(again["message_id"])

    async def test_time_from_the_future_is_clamped(self):
        # Время шлюза из будущего держало бы обращение первым в списке и
        # вне срока чистки.
        future = datetime.now(UTC) + timedelta(days=30)
        thread = await self.thread(channel="wa", origin="hook", ext_id="+79005550000",
                                   at=future)
        [message] = self.messages(thread["id"])
        edge = datetime.now(UTC)
        self.assertLessEqual(message["created_at"], edge)
        self.assertLessEqual(thread["waiting_since"], edge)
        self.assertLessEqual(thread["last_in_at"], edge)
        past = datetime.now(UTC) - timedelta(hours=2)
        other = await self.thread(channel="wa", origin="hook", ext_id="+79005550001", at=past)
        self.assertEqual(other["waiting_since"], past, "прошлое время шлюза - как есть")

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
        body = self.crm.inbox_messages_[mid]["body_enc"]
        new_id = await service.inbox_retry(self.crm, mid, by="owner")
        # Та же строка обратно в очередь, а не копия: у копии исходное «не
        # ушло» оставалось бы с кнопкой, и второе нажатие слало бы дубль.
        self.assertEqual(new_id, mid)
        again = self.crm.inbox_messages_[mid]
        self.assertEqual((again["status"], again["author"], again["error"]),
                         ("queued", "owner", None))
        self.assertEqual(again["body_enc"], body)
        with self.assertRaises(service.ServiceError):
            await service.inbox_retry(self.crm, mid, by="owner")   # уже в очереди
        await self.crm.claim_inbox_out()
        await self.crm.finish_inbox_out(mid, ok=True)
        with self.assertRaises(service.ServiceError):
            await service.inbox_retry(self.crm, mid, by="owner")   # ушедший не повторяем
        outs = [m for m in self.crm.inbox_messages_.values() if m["direction"] == "out"]
        self.assertEqual(len(outs), 1, "одна строка - одно сообщение человеку")
        with self.assertRaises(service.ServiceError):
            await service.inbox_retry(self.crm, 999999, by="owner")

    async def test_retry_waits_for_the_newer_reply(self):
        # Не ушёл первый, администратор написал второй: повтор первого -
        # только когда второй ушёл, иначе в очереди обращения два ответа.
        thread = await self.thread()
        first = await service.inbox_reply(self.crm, self.vault, thread, "Первый",
                                          by="admin", avito_ok=False)
        await self.crm.claim_inbox_out()
        await self.crm.finish_inbox_out(first, ok=False, error="timeout")
        fresh = await self.crm.inbox_thread(thread["id"])
        second = await service.inbox_reply(self.crm, self.vault, fresh, "Второй",
                                           by="admin", avito_ok=False)
        with self.assertRaises(service.ServiceError):
            await service.inbox_retry(self.crm, first, by="owner")
        self.assertEqual(self.crm.inbox_messages_[first]["status"], "failed",
                         "отказ строку не трогает")
        await self.crm.claim_inbox_out()
        with self.assertRaises(service.ServiceError):
            await service.inbox_retry(self.crm, first, by="owner")   # второй отправляется
        await self.crm.finish_inbox_out(second, ok=True)
        self.assertEqual(await service.inbox_retry(self.crm, first, by="owner"), first)
        row = self.crm.inbox_messages_[first]
        self.assertEqual((row["status"], row["author"], row["error"], row["sent_at"],
                          row["claimed_at"]), ("queued", "owner", None, None, None))

    async def test_stuck_sweep_only_older_than(self):
        # «Отправляется» дольше STUCK_MINUTES - итог не записался; свежую
        # отправку круг не трогает, иначе она ушла бы и стала «не ушло».
        now = datetime.now(UTC)
        rows = {}
        for ext, minutes in (("6001", inbox.STUCK_MINUTES + 1), ("6002", 1), ("6003", None)):
            thread = await self.thread(ext_id=ext)
            mid = await service.inbox_reply(self.crm, self.vault, thread, "Ответ",
                                            by="admin", avito_ok=False)
            claimed = await self.crm.claim_inbox_out()
            self.assertEqual(claimed["id"], mid)
            self.assertIsNotNone(self.crm.inbox_messages_[mid]["claimed_at"],
                                 "взятие в отправку ставит время")
            if minutes is None:
                # строка до правки: времени взятия нет - считается от создания
                self.crm.inbox_messages_[mid].update(
                    claimed_at=None, created_at=now - timedelta(hours=1))
            else:
                self.crm.inbox_messages_[mid]["claimed_at"] = now - timedelta(minutes=minutes)
            rows[ext] = mid
        swept = await self.crm.fail_stuck_inbox_out(older_minutes=inbox.STUCK_MINUTES)
        self.assertEqual(swept, 2)
        status = {ext: self.crm.inbox_messages_[mid]["status"] for ext, mid in rows.items()}
        self.assertEqual(status, {"6001": "failed", "6002": "sending", "6003": "failed"})
        self.assertIn("неизвестно, ушло ли", self.crm.inbox_messages_[rows["6001"]]["error"])
        # после перезапуска - все «отправляется», без срока
        self.assertEqual(await self.crm.fail_stuck_inbox_out(), 1)
        self.assertEqual(self.crm.inbox_messages_[rows["6002"]]["status"], "failed")


class TestThreadOrigin(InboxCase):
    """Обращение принадлежит источнику, который его завёл."""

    async def test_hook_does_not_write_into_polled_avito_chat(self):
        # Утёкший токен хука не должен подкладывать «слова клиента» в
        # настоящий чат Авито, куда панель отвечает через API.
        real = await self.thread(channel="avito", origin="avito_api", ext_id="u2i-1",
                                 name="Ильдар", subject="Электровелосипед Monster",
                                 text="Свободен?")
        await inbox.announce_once(FakeBot(), self.crm, cfg())
        before = dict(self.crm.inbox_threads_[real["id"]])
        count = len(self.crm.inbox_messages_)
        with self.assertRaises(service.ServiceError):
            await service.inbox_in(
                self.crm, self.vault, channel="avito", origin="hook", ext_id="u2i-1",
                msg_id="fake-1", name="Служба безопасности Авито", phone="+79990009999",
                subject="Возврат предоплаты", subject_url="https://www.avito.ru/x",
                text="Переведите предоплату на карту 2200 0000 0000 0000")
        self.assertEqual(len(self.crm.inbox_messages_), count, "сообщения нет")
        self.assertEqual(self.crm.inbox_threads_[real["id"]], before,
                         "имя, телефон, тема, ожидание и сигнал - прежние")
        self.assertEqual(len(self.crm.inbox_threads_), 1, "и второго обращения рядом нет")

    async def test_poll_does_not_write_into_hook_thread(self):
        hook = await self.thread(channel="avito", origin="hook", ext_id="u2i-1",
                                 name="Из n8n", text="Через шлюз")
        before = dict(self.crm.inbox_threads_[hook["id"]])
        with self.assertRaises(service.ServiceError):
            await service.inbox_in(self.crm, self.vault, channel="avito",
                                   origin="avito_api", ext_id="u2i-1", name="Ильдар",
                                   text="Из опроса")
        self.assertEqual(self.crm.inbox_threads_[hook["id"]], before)
        self.assertEqual(len(self.messages(hook["id"])), 1)

    async def test_record_swallows_foreign_origin_without_text(self):
        await self.thread(channel="avito", origin="avito_api", ext_id="u2i-1")
        with self.assertLogs("app.crm.inbox", "WARNING") as logs:
            got = await inbox.record(self.crm, cfg(), channel="avito", origin="hook",
                                     ext_id="u2i-1", text="Мой паспорт 9204 123456")
        self.assertIsNone(got)
        out = "\n".join(logs.output)
        self.assertIn("avito/u2i-1", out)
        self.assertIn("ServiceError", out)
        self.assertNotIn("9204", out)

    async def test_same_address_in_another_channel_is_another_thread(self):
        tg = await self.thread(channel="tg", origin="bot", ext_id="777")
        max_thread = await self.thread(channel="max", origin="max_bot", ext_id="777")
        self.assertNotEqual(tg["id"], max_thread["id"])
        self.assertEqual(max_thread["client_id"], self.max_man)


class TestManualClient(InboxCase):
    """Ручная привязка к карточке важнее найденной по телефону."""

    async def test_unlink_is_not_undone_by_next_message(self):
        thread = await self.thread(channel="wa", origin="hook", ext_id="+79001234567",
                                   phone="+79001234567")
        self.assertEqual(thread["client_id"], self.ildar)
        self.assertFalse(thread["client_manual"], "найденная по телефону - не ручная")
        await service.inbox_link_client(self.crm, thread, "", by="admin")
        fresh = await self.crm.inbox_thread(thread["id"])
        self.assertEqual((fresh["client_id"], fresh["client_manual"]), (None, True))
        # Общий телефон (семья, курьерский аккаунт на двоих) вернул бы
        # чужую карточку следующим же сообщением.
        await self.thread(channel="wa", origin="hook", ext_id="+79001234567",
                          phone="+79001234567", text="Ещё вопрос")
        self.assertIsNone((await self.crm.inbox_thread(thread["id"]))["client_id"])

    async def test_unlink_holds_for_telegram_id_too(self):
        thread = await self.thread()
        self.assertEqual(thread["client_id"], self.ildar)
        await service.inbox_link_client(self.crm, thread, None, by="admin")
        await self.thread(text="Алло?")
        self.assertIsNone((await self.crm.inbox_thread(thread["id"]))["client_id"])

    async def test_manual_link_is_not_replaced(self):
        thread = await self.thread(channel="wa", origin="hook", ext_id="+79005550000")
        self.assertIsNone(thread["client_id"])
        await service.inbox_link_client(self.crm, thread, str(self.max_man), by="admin")
        self.assertTrue((await self.crm.inbox_thread(thread["id"]))["client_manual"])
        await self.thread(channel="wa", origin="hook", ext_id="+79005550000",
                          phone="+79001234567", text="Это Ильдар, телефон жены")
        fresh = await self.crm.inbox_thread(thread["id"])
        self.assertEqual((fresh["client_id"], fresh["client_manual"]), (self.max_man, True))

    async def test_auto_link_still_works_without_manual(self):
        # Пока руками не трогали, карточка подтягивается, как только появилась.
        thread = await self.thread(channel="wa", origin="hook", ext_id="+79005550000")
        self.assertIsNone(thread["client_id"])
        newcomer = await self.crm.create_client(full_name="Новиков Никита",
                                                phone="+79005550000")
        await self.thread(channel="wa", origin="hook", ext_id="+79005550000",
                          phone="+79005550000", text="Я заполнил анкету")
        fresh = await self.crm.inbox_thread(thread["id"])
        self.assertEqual((fresh["client_id"], fresh["client_manual"]), (newcomer, False))


class TestWaitingAndSignal(InboxCase):
    """«Ждёт с» и сигнал в чат - от момента, когда человек начал ждать."""

    async def test_done_and_spam_clear_waiting(self):
        for status, ext in (("done", "6001"), ("spam", "6002")):
            with self.subTest(status=status):
                thread = await self.thread(ext_id=ext)
                self.assertIsNotNone(thread["waiting_since"])
                await service.inbox_set_status(self.crm, thread, status, note=None,
                                               by="admin")
                fresh = await self.crm.inbox_thread(thread["id"])
                self.assertIsNone(fresh["waiting_since"])
                # вернули в работу - прошлое ожидание не воскресает
                await service.inbox_set_status(self.crm, fresh, "work", note=None,
                                               by="admin")
                self.assertIsNone((await self.crm.inbox_thread(thread["id"]))["waiting_since"])
        self.assertEqual(await self.crm.inbox_open_count(), 0)

    async def test_work_keeps_waiting(self):
        now = datetime.now(UTC)
        thread = await self.thread(at=now - timedelta(hours=2))
        await service.inbox_set_status(self.crm, thread, "work", note=None, by="admin")
        self.assertEqual((await self.crm.inbox_thread(thread["id"]))["waiting_since"],
                         now - timedelta(hours=2), "в работе - а ответа всё ещё нет")

    async def test_reopen_waits_from_the_new_message(self):
        now = datetime.now(UTC)
        thread = await self.thread(at=now - timedelta(days=3))
        await service.inbox_set_status(self.crm, thread, "done", note=None, by="admin")
        await self.thread(text="Снова я", at=now - timedelta(minutes=5))
        fresh = await self.crm.inbox_thread(thread["id"])
        self.assertEqual(fresh["status"], "new")
        self.assertEqual(fresh["waiting_since"], now - timedelta(minutes=5))

    async def test_reopen_ignores_stale_waiting_of_old_rows(self):
        # Разобранное до правки хранило «ждёт с» прошлого вопроса: новое
        # сообщение не должно показывать ожидание в трое суток.
        now = datetime.now(UTC)
        thread = await self.thread(at=now - timedelta(days=3))
        self.crm.inbox_threads_[thread["id"]].update(status="done")
        self.assertIsNotNone(self.crm.inbox_threads_[thread["id"]]["waiting_since"])
        await self.thread(text="Снова я", at=now - timedelta(minutes=5))
        fresh = await self.crm.inbox_thread(thread["id"])
        self.assertEqual((fresh["status"], fresh["waiting_since"]),
                         ("new", now - timedelta(minutes=5)))

    async def test_second_message_keeps_first_waiting(self):
        now = datetime.now(UTC)
        thread = await self.thread(at=now - timedelta(hours=1))
        await self.thread(text="Алло?", at=now - timedelta(minutes=1))
        fresh = await self.crm.inbox_thread(thread["id"])
        self.assertEqual(fresh["waiting_since"], now - timedelta(hours=1))
        self.assertEqual(fresh["last_in_at"], now - timedelta(minutes=1))

    async def announced(self, thread_id) -> bool:
        return (await self.crm.inbox_thread(thread_id))["announced_at"] is not None

    async def test_signal_when_someone_starts_waiting(self):
        bot = FakeBot()
        thread = await self.thread(text="Первый вопрос")
        self.assertFalse(await self.announced(thread["id"]), "новое обращение - сигнал")
        self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), 1)
        # второе «алло?» подряд - человек уже ждёт, сигнала нет
        await self.thread(text="Алло?")
        self.assertTrue(await self.announced(thread["id"]))
        self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), 0)
        # ответили - первое входящее после ответа снова сигнал
        fresh = await self.crm.inbox_thread(thread["id"])
        await service.inbox_reply(self.crm, self.vault, fresh, "Ответ", by="admin",
                                  avito_ok=False)
        await inbox.send_once(FakeBot(), self.crm, cfg())
        self.assertIsNone((await self.crm.inbox_thread(thread["id"]))["waiting_since"])
        await self.thread(text="Спасибо, а ещё вопрос")
        self.assertFalse(await self.announced(thread["id"]))
        self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), 1)
        # разобрали - возврат из разобранных тоже сигнал
        fresh = await self.crm.inbox_thread(thread["id"])
        await service.inbox_set_status(self.crm, fresh, "done", note=None, by="admin")
        await self.thread(text="Снова я")
        fresh = await self.crm.inbox_thread(thread["id"])
        self.assertEqual(fresh["status"], "new")
        self.assertIsNone(fresh["announced_at"])
        self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), 1)
        self.assertEqual(len(bot.sent), 3)

    async def test_answered_elsewhere_then_new_question_signals(self):
        thread = await self.thread()
        await inbox.announce_once(FakeBot(), self.crm, cfg())
        await service.inbox_answered_elsewhere(self.crm, thread, by="admin")
        await self.thread(text="А ещё?")
        self.assertFalse(await self.announced(thread["id"]))

    async def test_spam_never_signals(self):
        thread = await self.thread()
        await inbox.announce_once(FakeBot(), self.crm, cfg())
        await service.inbox_set_status(self.crm, thread, "spam", note=None, by="admin")
        for text in ("Купите рекламу", "Скидка 90%"):
            await self.thread(text=text)
        fresh = await self.crm.inbox_thread(thread["id"])
        self.assertEqual(fresh["status"], "spam")
        self.assertIsNotNone(fresh["announced_at"])
        bot = FakeBot()
        self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), 0)
        self.assertEqual(bot.sent, [])

    async def test_silent_record_does_not_signal(self):
        # announce=False (отметка анкеты, своё сообщение) - сигнала нет,
        # даже когда человек не ждал.
        thread = await self.thread()
        await inbox.announce_once(FakeBot(), self.crm, cfg())
        await service.inbox_answered_elsewhere(self.crm, thread, by="admin")
        await inbox.record(self.crm, cfg(), channel="tg", origin="bot", ext_id=5001,
                           direction="event", kind="other", msg_id="anketa:1",
                           text="Анкета отправлена на проверку", announce=False)
        self.assertTrue(await self.announced(thread["id"]))
        await inbox.record(self.crm, cfg(), channel="tg", origin="bot", ext_id=5001,
                           text="А когда проверят?", announce=False)
        self.assertTrue(await self.announced(thread["id"]))


# ─────────────────────── процесс бота: запись и отправка ───────────────────────

class FakeBot:
    def __init__(self, fail: Exception | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.markups: list = []
        self.fail = fail

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if self.fail is not None:
            raise self.fail
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)


class FakeMax:
    def __init__(self, fail: Exception | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send(self, *, user_id=None, chat_id=None, text=""):
        if self.fail is not None:
            raise self.fail
        self.sent.append((user_id, text))


class FakeUsers:
    """bot.users: язык клиента и его состояние в сценарии бота."""

    def __init__(self, *, state: str = "approved", **langs: str) -> None:
        self.langs = {int(k.lstrip("u")): v for k, v in langs.items()}
        self.states = {tg_id: state for tg_id in self.langs}
        self.patched: list[tuple[int, str, str]] = []

    async def get_user(self, tg_id):
        lang = self.langs.get(tg_id)
        return ({"tg_id": tg_id, "lang": lang, "state": self.states.get(tg_id)}
                if lang else None)

    async def patch(self, tg_id, *, expected_state=None, **fields):
        if expected_state is not None and self.states.get(tg_id) != expected_state:
            return False
        if "state" in fields:
            self.patched.append((tg_id, self.states.get(tg_id), fields["state"]))
            self.states[tg_id] = fields["state"]
        return True


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

    async def chats(self, *, limit=100, offset=0):
        self.chat_pages = getattr(self, "chat_pages", []) + [offset]
        page = self.raw_chats[offset:offset + limit]
        return [avito_api.parse_chat(c, OWN) for c in page]

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

    async def test_approved_client_can_answer_back(self):
        # Ответ из панели - не тупик: клиент с договором переходит в режим
        # вопроса, и его следующее сообщение придёт во «Входящие».
        await self.queued("Велосипед будет завтра")
        bot, users = FakeBot(), FakeUsers(u5001="ru")
        await inbox.send_once(bot, self.crm, cfg(), db=users)
        self.assertEqual(users.patched, [(5001, bot_logic.APPROVED, bot_logic.WAIT_SUPPORT)])
        self.assertEqual(len(bot.sent), 2)
        self.assertIn("Велосипед будет завтра", bot.sent[0][1])
        self.assertIn("одним сообщением", bot.sent[1][1])

    async def test_guest_mid_anketa_gets_contact_not_prompt(self):
        # Посреди анкеты свободный текст бот читает как шаг анкеты: режим
        # вопроса не включаем, а к ответу приклеиваем прямой контакт.
        await self.queued("Приходите завтра")
        bot = FakeBot()
        users = FakeUsers(state=bot_logic.WAIT_BIRTH_PLACE, u5001="ru")
        await inbox.send_once(bot, self.crm, cfg(), db=users)
        [(_, text)] = bot.sent
        self.assertIn("Приходите завтра", text)
        self.assertIn("Написать нам:", text)
        self.assertEqual(users.patched, [])

    async def test_failed_send_does_not_switch_mode(self):
        await self.queued("Ok")
        users = FakeUsers(u5001="ru")
        await inbox.send_once(FakeBot(fail=RuntimeError("Forbidden: bot was blocked")),
                              self.crm, cfg(), db=users)
        self.assertEqual(users.patched, [])

    async def test_prompt_in_clients_language_with_cancel(self):
        await self.queued("Ready")
        bot, users = FakeBot(), FakeUsers(u5001="en")
        await inbox.send_once(bot, self.crm, cfg(), db=users)
        self.assertEqual([text for _, text in bot.sent],
                         ["💬 Support reply:\n\nReady", faq_i18n.T["en"]["handoff"]])
        self.assertEqual([chat for chat, _ in bot.sent], [5001, 5001])
        self.assertIsNone(bot.markups[0], "у самого ответа клавиатуры нет")
        cancel = bot.markups[1]
        self.assertEqual([[b.text for b in row] for row in cancel.keyboard], [["Cancel"]],
                         "передумал - кнопка отмены на его языке")
        self.assertEqual(users.states[5001], bot_logic.WAIT_SUPPORT)

    async def test_russian_prompt_and_cancel(self):
        await self.queued("Велосипед будет завтра")
        bot = FakeBot()
        await inbox.send_once(bot, self.crm, cfg(), db=FakeUsers(u5001="ru"))
        self.assertEqual(bot.sent[1][1], texts.FAQ_HANDOFF)
        self.assertEqual([[b.text for b in row] for row in bot.markups[1].keyboard],
                         [["Отмена"]])

    async def test_already_asking_gets_plain_reply(self):
        # Уже в режиме вопроса: ни второго приглашения, ни контакта.
        await self.queued("Ok")
        bot = FakeBot()
        users = FakeUsers(state=bot_logic.WAIT_SUPPORT, u5001="ru")
        await inbox.send_once(bot, self.crm, cfg(), db=users)
        self.assertEqual(bot.sent, [(5001, texts.SUPPORT_REPLY_USER.format(answer="Ok"))])
        self.assertEqual(users.patched, [])
        self.assertEqual(users.states[5001], bot_logic.WAIT_SUPPORT)

    async def test_unknown_user_gets_plain_reply(self):
        class Broken(FakeUsers):
            async def get_user(self, tg_id):
                raise RuntimeError("база бота недоступна")

        for users in (None, FakeUsers(), Broken(u5001="ru")):
            with self.subTest(users=type(users).__name__):
                self.crm = FakeCrm()
                _, mid = await self.queued("Ok")
                bot = FakeBot()
                await inbox.send_once(bot, self.crm, cfg(), db=users)
                self.assertEqual(bot.sent,
                                 [(5001, texts.SUPPORT_REPLY_USER.format(answer="Ok"))])
                self.assertEqual(self.crm.inbox_messages_[mid]["status"], "sent")
                if users is not None:
                    self.assertEqual(users.patched, [])

    async def test_mid_anketa_contact_in_language_and_from_setting(self):
        # Контакт менеджера из настройки подменяет зашитый - и в переводе.
        company.reset()
        self.addCleanup(company.reset)
        company.set_snapshot({"support_contact": "https://t.me/novy_menedzher"})
        for lang, prefix in (("ru", "Написать нам:"), ("en", "Write to us:")):
            with self.subTest(lang=lang):
                self.crm = FakeCrm()
                await self.queued("<b>Ok</b>")
                bot = FakeBot()
                users = FakeUsers(state=bot_logic.WAIT_FIO, u5001=lang)
                await inbox.send_once(bot, self.crm, cfg(), db=users)
                [(_, text)] = bot.sent
                self.assertIn("&lt;b&gt;Ok&lt;/b&gt;", text)
                self.assertIn(f"{prefix} https://t.me/novy_menedzher", text)
                self.assertNotIn(texts.SUPPORT_CONTACT_URL, text)
                self.assertEqual(users.patched, [])

    async def test_state_changed_meanwhile_no_prompt(self):
        # Между чтением и правкой человек нажал другую кнопку: правка с
        # ожидаемым состоянием не проходит, приглашения нет.
        class Racing(FakeUsers):
            async def get_user(self, tg_id):
                row = await super().get_user(tg_id)
                self.states[tg_id] = bot_logic.WAIT_FIO
                return row

        await self.queued("Ok")
        bot, users = FakeBot(), Racing(u5001="ru")
        await inbox.send_once(bot, self.crm, cfg(), db=users)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(users.patched, [])
        self.assertEqual(users.states[5001], bot_logic.WAIT_FIO, "сценарий не сбит")

    async def test_prompt_failure_keeps_reply_sent(self):
        class SecondFails(FakeBot):
            async def send_message(self, chat_id, text, reply_markup=None, **kw):
                if self.sent:
                    raise RuntimeError("Too Many Requests")
                await super().send_message(chat_id, text, reply_markup=reply_markup)

        _, mid = await self.queued("Ok")
        bot, users = SecondFails(), FakeUsers(u5001="ru")
        with self.assertLogs("app.crm.inbox", "WARNING"):
            self.assertTrue(await inbox.send_once(bot, self.crm, cfg(), db=users))
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(self.crm.inbox_messages_[mid]["status"], "sent",
                         "ответ ушёл - сбой приглашения его не отменяет")

    async def test_max_reply_does_not_touch_bot_state(self):
        await self.queued("Ok", channel="max", origin="max_bot", ext_id="777")
        users = FakeUsers(u777="ru")
        await inbox.send_once(FakeBot(), self.crm, cfg(), db=users, max_client=FakeMax())
        self.assertEqual(users.patched, [], "состояние MAX живёт в базе MAX-бота")

    async def test_finish_is_retried(self):
        _, mid = await self.queued()
        real, calls = self.crm.finish_inbox_out, []

        async def flaky(message_id, **kw):
            calls.append(message_id)
            if len(calls) < inbox.FINISH_TRIES:
                raise RuntimeError("connection reset")
            return await real(message_id, **kw)

        self.crm.finish_inbox_out = flaky
        bot = FakeBot()
        with mock.patch.object(inbox, "FINISH_PAUSE", 0):
            self.assertTrue(await inbox.send_once(bot, self.crm, cfg()))
        self.assertEqual(calls, [mid] * inbox.FINISH_TRIES)
        self.assertEqual(self.crm.inbox_messages_[mid]["status"], "sent")
        self.assertEqual(len(bot.sent), 1, "повторяется запись итога, а не отправка")

    async def test_unwritten_result_is_swept_later(self):
        thread, mid = await self.queued()
        calls = []

        async def broken(message_id, **kw):
            calls.append(message_id)
            raise RuntimeError("база недоступна")

        real = self.crm.finish_inbox_out
        self.crm.finish_inbox_out = broken
        bot = FakeBot()
        with mock.patch.object(inbox, "FINISH_PAUSE", 0), \
                self.assertLogs("app.crm.inbox", "ERROR"):
            self.assertTrue(await inbox.send_once(bot, self.crm, cfg()))
        self.crm.finish_inbox_out = real
        self.assertEqual(calls, [mid] * inbox.FINISH_TRIES)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(self.crm.inbox_messages_[mid]["status"], "sending")
        self.assertFalse(await inbox.send_once(bot, self.crm, cfg()), "второй раз не шлём")
        # Через STUCK_MINUTES круг снимает строку - очередь обращения свободна.
        self.assertEqual(await self.crm.fail_stuck_inbox_out(
            older_minutes=inbox.STUCK_MINUTES), 0)
        self.crm.inbox_messages_[mid]["claimed_at"] -= timedelta(
            minutes=inbox.STUCK_MINUTES + 1)
        self.assertEqual(await self.crm.fail_stuck_inbox_out(
            older_minutes=inbox.STUCK_MINUTES), 1)
        self.assertEqual(self.crm.inbox_messages_[mid]["status"], "failed")
        self.assertEqual(await service.inbox_retry(self.crm, mid, by="admin"), mid,
                         "повторяет человек кнопкой")

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

    async def test_failed_signal_is_retried_then_given_up(self):
        # Сбой отправки (лимит Telegram, сеть) - повтор следующим кругом, но
        # не вечно: после ANNOUNCE_TRIES отметка ставится, залпа нет.
        bot = FakeBot(fail=RuntimeError("chat not found"))
        with self.assertLogs("app.crm.notices", "WARNING"):
            for _ in range(inbox.ANNOUNCE_TRIES - 1):
                self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), 0)
        self.assertFalse(any(t["announced_at"] for t in self.crm.inbox_threads_.values()))
        with self.assertLogs("app.crm.inbox", "WARNING"):
            self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), 2)
        self.assertTrue(all(t["announced_at"] for t in self.crm.inbox_threads_.values()))
        self.assertEqual({n["status"] for n in self.crm.notice_log_}, {"failed"})
        self.assertEqual(await inbox.announce_once(FakeBot(), self.crm, cfg()), 0,
                         "залпа после починки чата нет")

    async def test_failed_signal_goes_out_next_round(self):
        bot = FakeBot(fail=RuntimeError("Too Many Requests: retry after 30"))
        with self.assertLogs("app.crm.notices", "WARNING"):
            self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), 0)
        fixed = FakeBot()
        self.assertEqual(await inbox.announce_once(fixed, self.crm, cfg()), 2)
        self.assertEqual(len(fixed.sent), 2, "сигнал не потерян из-за лимита")

    async def test_burst_goes_as_one_summary(self):
        for i in range(inbox.ANNOUNCE_BURST + 2):
            await self.thread(channel="wa", origin="hook", ext_id=f"+7900555{i:04d}",
                              name=f"Клиент {i}", text="Есть свободные?")
        bot = FakeBot()
        total = len(self.crm.inbox_threads_)
        self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), total)
        [(chat, text)] = bot.sent
        self.assertEqual(chat, ADMIN_CHAT)
        self.assertIn(f"Новых обращений: {total}", text)
        self.assertIn("WhatsApp", text)
        for secret in ("Клиент", "Ильдар", "+7900555", "Есть свободные"):
            self.assertNotIn(secret, text)

    async def add(self, count: int) -> None:
        for i in range(count):
            await self.thread(channel="wa", origin="hook", ext_id=f"+7900555{i:04d}",
                              name=f"Клиент {i}", text="Есть свободные?")

    async def test_exactly_burst_goes_one_by_one(self):
        await self.add(inbox.ANNOUNCE_BURST - len(self.crm.inbox_threads_))
        bot = FakeBot()
        self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()),
                         inbox.ANNOUNCE_BURST)
        self.assertEqual(len(bot.sent), inbox.ANNOUNCE_BURST)
        for _, text in bot.sent:
            self.assertIn("Новое обращение ВХ-", text)
            self.assertNotIn("Новых обращений", text)

    async def test_failed_summary_goes_out_next_round(self):
        await self.add(inbox.ANNOUNCE_BURST)
        total = len(self.crm.inbox_threads_)
        with self.assertLogs("app.crm.notices", "WARNING"):
            self.assertEqual(await inbox.announce_once(
                FakeBot(fail=RuntimeError("Too Many Requests")), self.crm, cfg()), 0)
        self.assertEqual(inbox._announce_fails, {0: 1}, "сбой сводки помнится под нулём")
        self.assertFalse(any(t["announced_at"] for t in self.crm.inbox_threads_.values()))
        bot = FakeBot()
        self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), total)
        [(_, text)] = bot.sent
        self.assertIn(f"Новых обращений: {total}", text)
        self.assertEqual(inbox._announce_fails, {}, "удача память стирает")

    async def test_failed_summary_given_up(self):
        await self.add(inbox.ANNOUNCE_BURST)
        total = len(self.crm.inbox_threads_)
        bot = FakeBot(fail=RuntimeError("chat not found"))
        with self.assertLogs("app.crm.notices", "WARNING"):
            for _ in range(inbox.ANNOUNCE_TRIES - 1):
                self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), 0)
        with self.assertLogs("app.crm.inbox", "WARNING") as logs:
            self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), total)
        self.assertIn("пачке", "\n".join(logs.output))
        self.assertTrue(all(t["announced_at"] for t in self.crm.inbox_threads_.values()))
        self.assertEqual(inbox._announce_fails, {})

    async def test_one_failed_signal_does_not_hold_others(self):
        first = min(self.crm.inbox_threads_)
        bad = logic.inbox_no(first)

        class Picky(FakeBot):
            async def send_message(self, chat_id, text, reply_markup=None, **kw):
                if bad in text:
                    raise RuntimeError("Too Many Requests")
                await super().send_message(chat_id, text, reply_markup=reply_markup)

        bot = Picky()
        with self.assertLogs("app.crm.notices", "WARNING"):
            self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), 1)
        self.assertEqual(len(bot.sent), 1)
        self.assertNotIn(bad, bot.sent[0][1])
        self.assertIsNone(self.crm.inbox_threads_[first]["announced_at"])
        self.assertEqual(inbox._announce_fails, {first: 1})
        fixed = FakeBot()
        self.assertEqual(await inbox.announce_once(fixed, self.crm, cfg()), 1)
        [(_, text)] = fixed.sent
        self.assertIn(bad, text, "повтор - только не ушедшему")
        self.assertEqual(inbox._announce_fails, {})

    async def test_burst_when_switched_off_is_marked_silently(self):
        await self.add(inbox.ANNOUNCE_BURST + 5)
        await self.crm.set_notice("inbox_new", enabled=False, at_hour=None)
        bot = FakeBot()
        total = len(self.crm.inbox_threads_)
        self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), total)
        self.assertEqual(bot.sent, [])
        self.assertTrue(all(t["announced_at"] for t in self.crm.inbox_threads_.values()))
        self.assertEqual(await inbox.announce_once(None, self.crm, cfg()), 0)

    async def test_without_bot_marked_at_once(self):
        self.assertEqual(await inbox.announce_once(None, self.crm, cfg()), 2)
        self.assertTrue(all(t["announced_at"] for t in self.crm.inbox_threads_.values()))

    async def test_round_takes_at_most_the_limit(self):
        await self.add(inbox.ANNOUNCE_LIMIT + 3)
        total = len(self.crm.inbox_threads_)
        bot = FakeBot()
        self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()),
                         inbox.ANNOUNCE_LIMIT)
        self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()),
                         total - inbox.ANNOUNCE_LIMIT)
        self.assertEqual(len(bot.sent), 2, "две сводки, а не полсотни сигналов")
        self.assertIn(f"Новых обращений: {inbox.ANNOUNCE_LIMIT}", bot.sent[0][1])

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
            for _ in range(inbox.ANNOUNCE_TRIES):
                done = await inbox.announce_once(FakeBot(), self.crm, cfg())
        self.assertEqual(done, 2)
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

    # ─ начало отсчёта и срок хранения ─

    async def test_since_is_set_only_by_a_successful_round(self):
        # Ключи завели до покупки тарифа с API: неудачные круги не должны
        # «застолбить» начало отсчёта, иначе первый удачный потянул бы
        # переписку за все недели ожидания.
        broken = FakeAvito(error=AvitoError("402 — нет доступа", 402))
        with self.assertRaises(AvitoError):
            await inbox.avito_once(self.crm, broken, cfg())
        self.assertNotIn("inbox_avito_since", self.crm.settings_)

        async def failing(chat_id):
            raise AvitoError("500 — Авито не ответил", 500)

        self.avito.messages = failing                  # сбой посреди круга
        with self.assertRaises(AvitoError):
            await inbox.avito_once(self.crm, self.avito, cfg())
        self.assertNotIn("inbox_avito_since", self.crm.settings_)
        self.assertEqual(self.crm.inbox_threads_, {})
        del self.avito.messages
        await inbox.avito_once(self.crm, self.avito, cfg())
        since = logic._moment(self.crm.settings_["inbox_avito_since"])
        self.assertAlmostEqual(since.timestamp(),
                               (datetime.now(UTC) - inbox.AVITO_FIRST_LOOKBACK).timestamp(),
                               delta=60)

    async def test_purged_history_does_not_come_back(self):
        # Подключены давно, переписку старше срока удалила дневная чистка -
        # опрос не возвращает её обратно.
        keep = timedelta(days=logic.INBOX_KEEP_DAYS)
        self.crm.settings_["inbox_avito_since"] = (self.now - keep * 2).isoformat()
        avito = FakeAvito(
            [chat_raw("u2i-1", "c1-m3", updated=self.now),
             chat_raw("u2i-old", "old-m1", updated=self.now - keep - timedelta(days=5))],
            {"u2i-1": [
                msg_raw("c1-m1", "Старше срока", at=self.now - keep - timedelta(days=1)),
                msg_raw("c1-m2", "В сроке", at=self.now - keep + timedelta(days=1)),
                msg_raw("c1-m3", "Сегодня", at=self.now)],
             "u2i-old": [msg_raw("old-m1", "Давно", at=self.now - keep - timedelta(days=5))]})
        counts = await inbox.avito_once(self.crm, avito, cfg())
        self.assertEqual(counts, {"chats": 1, "messages": 2})
        self.assertEqual(avito.message_calls, ["u2i-1"], "старый чат даже не читается")
        thread = await self.avito_thread()
        self.assertEqual([m["ext_id"] for m in self.messages(thread["id"])],
                         ["c1-m2", "c1-m3"])

    # ─ чаты без обращения ─

    def noise_chat(self, chat_id: str, last_id: str) -> tuple[dict, list[dict]]:
        return (chat_raw(chat_id, last_id, updated=self.now),
                [msg_raw(last_id, "Пользователь создал чат", at=self.now, kind="system"),
                 msg_raw(f"{last_id}-stub", "Чтобы ответить, перейдите на подписку",
                         at=self.now)])

    async def test_noise_only_chat_is_remembered(self):
        chat, messages = self.noise_chat("u2i-2", "c2-sys")
        avito = FakeAvito([chat], {"u2i-2": messages})
        self.assertEqual(await inbox.avito_once(self.crm, avito, cfg()),
                         {"chats": 1, "messages": 0})
        self.assertEqual(self.crm.inbox_threads_, {}, "служебное - не обращение")
        self.assertEqual(json.loads(self.crm.settings_["inbox_avito_seen"]),
                         {"u2i-2": "c2-sys"})
        # чат не менялся - не качаем его каждый круг
        self.assertEqual(await inbox.avito_once(self.crm, avito, cfg()),
                         {"chats": 0, "messages": 0})
        self.assertEqual(avito.message_calls, ["u2i-2"])
        # написал человек - чат читается и обращение заводится
        avito.raw_chats = [chat_raw("u2i-2", "c2-m1", updated=self.now)]
        avito.raw_messages["u2i-2"].append(msg_raw("c2-m1", "Здравствуйте", at=self.now))
        self.assertEqual(await inbox.avito_once(self.crm, avito, cfg()),
                         {"chats": 1, "messages": 1})
        self.assertEqual(avito.message_calls, ["u2i-2", "u2i-2"])
        thread = await self.avito_thread()
        self.assertEqual((thread["ext_id"], thread["ext_cursor"]), ("u2i-2", "c2-m1"))

    async def test_old_only_chat_is_remembered(self):
        # Чат ожил служебным сообщением, а слова человека - до начала отсчёта.
        self.crm.settings_["inbox_avito_since"] = (self.now - timedelta(minutes=15)).isoformat()
        avito = FakeAvito([chat_raw("u2i-3", "c3-m1", updated=self.now)],
                          {"u2i-3": [msg_raw("c3-m1", "Старый вопрос",
                                             at=self.now - timedelta(hours=1))]})
        await inbox.avito_once(self.crm, avito, cfg())
        await inbox.avito_once(self.crm, avito, cfg())
        self.assertEqual(self.crm.inbox_threads_, {})
        self.assertEqual(avito.message_calls, ["u2i-3"])
        self.assertEqual(json.loads(self.crm.settings_["inbox_avito_seen"]),
                         {"u2i-3": "c3-m1"})

    async def test_seen_memory_is_bounded(self):
        chats, messages = [], {}
        for chat_id in ("u2i-a", "u2i-b", "u2i-c"):
            chat, rows = self.noise_chat(chat_id, f"{chat_id}-sys")
            chats.append(chat)
            messages[chat_id] = rows
        with mock.patch.object(inbox, "AVITO_SEEN_KEEP", 2):
            await inbox.avito_once(self.crm, FakeAvito(chats, messages), cfg())
        self.assertEqual(json.loads(self.crm.settings_["inbox_avito_seen"]),
                         {"u2i-b": "u2i-b-sys", "u2i-c": "u2i-c-sys"})

    async def test_broken_seen_memory_is_ignored(self):
        for junk in ("не json", "[1, 2]", "42", "null"):
            with self.subTest(junk=junk):
                self.crm = FakeCrm()
                self.crm.settings_["inbox_avito_seen"] = junk
                chat, messages = self.noise_chat("u2i-2", "c2-sys")
                await inbox.avito_once(self.crm, FakeAvito([chat], {"u2i-2": messages}),
                                       cfg())
                self.assertEqual(json.loads(self.crm.settings_["inbox_avito_seen"]),
                                 {"u2i-2": "c2-sys"})

    async def test_empty_answer_does_not_move_cursor(self):
        # Чат изменился, а сообщений Авито не отдал - ответ неполный.
        await inbox.avito_once(self.crm, self.avito, cfg())
        saved = self.avito.raw_messages["u2i-1"]
        self.avito.raw_chats = [chat_raw("u2i-1", "c1-m5", updated=self.now)]
        self.avito.raw_messages["u2i-1"] = []
        await inbox.avito_once(self.crm, self.avito, cfg())
        self.assertEqual((await self.avito_thread())["ext_cursor"], "c1-m4")
        self.avito.raw_messages["u2i-1"] = saved + [
            msg_raw("c1-m5", "Когда можно забрать?", at=self.now)]
        counts = await inbox.avito_once(self.crm, self.avito, cfg())
        self.assertEqual(counts["messages"], 1)
        thread = await self.avito_thread()
        self.assertEqual(thread["ext_cursor"], "c1-m5")
        self.assertIn("c1-m5", [m["ext_id"] for m in self.messages(thread["id"])])

    async def test_empty_answer_for_new_chat_is_not_remembered(self):
        avito = FakeAvito([chat_raw("u2i-4", "c4-m1", updated=self.now)], {})
        await inbox.avito_once(self.crm, avito, cfg())
        seen = json.loads(self.crm.settings_.get("inbox_avito_seen") or "{}")
        self.assertNotIn("u2i-4", seen, "пустой ответ - не «одни служебные»")
        avito.raw_messages["u2i-4"] = [msg_raw("c4-m1", "Свободен?", at=self.now)]
        await inbox.avito_once(self.crm, avito, cfg())
        self.assertEqual(avito.message_calls, ["u2i-4", "u2i-4"])
        thread = await self.avito_thread()
        self.assertEqual(thread["ext_cursor"], "c4-m1")

    # ─ страницы списка чатов ─

    def many(self, count: int) -> FakeAvito:
        chats = [chat_raw(f"u2i-{i}", f"c{i}-m1", updated=self.now - timedelta(minutes=i))
                 for i in range(count)]
        messages = {f"u2i-{i}": [msg_raw(f"c{i}-m1", "Свободен?",
                                         at=self.now - timedelta(minutes=i))]
                    for i in range(count)}
        return FakeAvito(chats, messages)

    async def test_chats_are_read_in_pages(self):
        avito = self.many(5)
        with mock.patch.object(inbox, "AVITO_PAGE", 2):
            counts = await inbox.avito_once(self.crm, avito, cfg())
        self.assertEqual(avito.chat_pages, [0, 2, 4], "короткая страница - последняя")
        self.assertEqual(counts, {"chats": 5, "messages": 5})
        self.assertEqual(len(self.crm.inbox_threads_), 5)

    async def test_default_page_is_asked_explicitly(self):
        calls = []

        class Recording(FakeAvito):
            async def chats(self, **kw):
                calls.append(kw)
                return await super().chats(**kw)

        await inbox.avito_once(self.crm, Recording(self.avito.raw_chats,
                                                   self.avito.raw_messages), cfg())
        self.assertEqual(calls, [{"limit": inbox.AVITO_PAGE, "offset": 0}],
                         "страница и сдвиг - явно, а не умолчания клиента")

    async def test_pages_are_capped(self):
        avito = self.many(7)
        with mock.patch.object(inbox, "AVITO_PAGE", 2), \
                mock.patch.object(inbox, "AVITO_PAGES", 2):
            counts = await inbox.avito_once(self.crm, avito, cfg())
        self.assertEqual(avito.chat_pages, [0, 2])
        self.assertEqual(counts["chats"], 4)
        self.assertEqual(avito.message_calls, ["u2i-0", "u2i-1", "u2i-2", "u2i-3"])

    async def test_page_without_changes_stops(self):
        avito = self.many(4)
        with mock.patch.object(inbox, "AVITO_PAGE", 2):
            await inbox.avito_once(self.crm, avito, cfg())
            self.assertEqual(avito.chat_pages, [0, 2, 4], "полная страница - смотрим дальше")
            avito.chat_pages = []
            counts = await inbox.avito_once(self.crm, avito, cfg())
        self.assertEqual(avito.chat_pages, [0], "список от свежих: без изменений - дальше старое")
        self.assertEqual(counts, {"chats": 0, "messages": 0})

    # ─ сигнал ─

    async def test_our_first_message_then_answer_signals(self):
        # Написали первыми - не обращение; человек ответил - начал ждать.
        self.avito.raw_chats = [chat_raw("u2i-2", "c2-m1", updated=self.now)]
        self.avito.raw_messages = {"u2i-2": [
            msg_raw("c2-m1", "Мы написали первыми", at=self.now - timedelta(minutes=5),
                    author=OWN)]}
        await inbox.avito_once(self.crm, self.avito, cfg())
        self.assertIsNotNone((await self.avito_thread())["announced_at"])
        self.avito.raw_chats = [chat_raw("u2i-2", "c2-m2", updated=self.now)]
        self.avito.raw_messages["u2i-2"].append(msg_raw("c2-m2", "Да, интересно",
                                                        at=self.now))
        await inbox.avito_once(self.crm, self.avito, cfg())
        thread = await self.avito_thread()
        self.assertIsNone(thread["announced_at"])
        self.assertIsNotNone(thread["waiting_since"])
        bot = FakeBot()
        self.assertEqual(await inbox.announce_once(bot, self.crm, cfg()), 1)
        self.assertIn(logic.inbox_no(thread["id"]), bot.sent[0][1])

    async def test_hook_thread_chat_is_not_reread_every_round(self):
        # Обращение с тем же номером чата уже завёл шлюз (n8n до подключения
        # API): опрос в него не пишет - но и не должен качать неизменный
        # чат каждый круг с предупреждением в лог.
        hook = await self.thread(channel="avito", origin="hook", ext_id="u2i-1",
                                 text="Через n8n")
        with mock.patch.object(inbox.log, "warning"):      # отказ записи - не предмет
            await inbox.avito_once(self.crm, self.avito, cfg())
            self.assertEqual(len(self.messages(hook["id"])), 1, "чужое обращение не тронуто")
            self.assertEqual(self.avito.message_calls, ["u2i-1"])
            await inbox.avito_once(self.crm, self.avito, cfg())
        self.assertEqual(self.avito.message_calls, ["u2i-1"],
                         "чат не менялся - перечитывать его незачем")


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

    async def test_every_round_sweeps_stuck_replies(self):
        # Итог отправки не записался и с повтором - строка «отправляется»
        # держит очередь обращения. Круг снимает её, не дожидаясь перезапуска,
        # но только старше STUCK_MINUTES.
        calls = []
        real = self.crm.fail_stuck_inbox_out
        rows = {}

        async def spy(**kw):
            calls.append(kw)
            got = await real(**kw)
            if not kw:                   # после стартовой проверки - зависли два
                now = datetime.now(UTC)
                for ext, minutes in (("6001", inbox.STUCK_MINUTES + 1), ("6002", 1)):
                    thread = await self.thread(ext_id=ext)
                    mid = await service.inbox_reply(self.crm, self.vault, thread, "Ответ",
                                                    by="admin", avito_ok=False)
                    await self.crm.claim_inbox_out()
                    self.crm.inbox_messages_[mid]["claimed_at"] = (
                        now - timedelta(minutes=minutes))
                    rows[ext] = mid
            return got

        self.crm.fail_stuck_inbox_out = spy
        with self.assertLogs("app.crm.inbox", "WARNING"):
            await self.run_loop(None, rounds=2)
        self.assertEqual(calls, [{}] + [{"older_minutes": inbox.STUCK_MINUTES}] * 2,
                         "стартовая - без срока, дальше каждый круг - со сроком")
        self.assertEqual(self.crm.inbox_messages_[rows["6001"]]["status"], "failed")
        self.assertEqual(self.crm.inbox_messages_[rows["6002"]]["status"], "sending",
                         "свежая отправка идёт своим чередом")

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
