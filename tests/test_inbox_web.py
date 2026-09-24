"""«Входящие» в панели и хук /hook/inbox через TestClient.

Раздел по умолчанию только у «Владельца»: менеджер и механик не видят
ни ссылки в меню, ни списка, ни карточки, и ни одна форма им не
открыта. Ответ из панели - строка в очереди, а не сообщение: отправляет
процесс бота, поэтому бот-заглушка панели должен остаться немым.

Хук открыт без входа в панель, но закрыт своим токеном, лимитом неудач
с адреса и размером тела; Telegram и MAX через него не заводятся.
База - tests/fake_crm.py, обвязка - из tests/test_web.py.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_web as tw

    from app.crm import service
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

run = tw.run
# Ключи постоянные: тесты не должны зависеть от случайности.
KEY = base64.b64encode(bytes(range(32))).decode("ascii")
OTHER_KEY = base64.b64encode(bytes(range(1, 33))).decode("ascii")
HOOK = "hook-token-5f0c1e"


def green(phone="79001234567", text="Здравствуйте, велосипед свободен?",
          msg_id="BAE5F4886F6F2D05", name="Азиз"):
    """Вебхук Green-API о входящем личном сообщении."""
    return {
        "typeWebhook": "incomingMessageReceived",
        "instanceData": {"idInstance": 1101000001, "wid": "79990000000@c.us"},
        "timestamp": 1790000000,
        "idMessage": msg_id,
        "senderData": {"chatId": f"{phone}@c.us", "sender": f"{phone}@c.us",
                       "senderName": name, "chatName": name},
        "messageData": {"typeMessage": "textMessage",
                        "textMessageData": {"textMessage": text}},
    }


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class InboxCase(tw.WebCase):
    """Панель с ключом переписки и токеном хука."""

    def setUp(self):
        super().setUp()
        self.build(inbox_key=KEY, inbox_hook_token=HOOK)

    def build(self, **over):
        """Пересобрать панель с другими настройками «Входящих». База та же."""
        self.cfg = dataclasses.replace(self.cfg, **over)
        self.app = tw.create_app(crm=self.crm, db=self.db, cfg=self.cfg, bot=self.bot)
        self.client = tw.TestClient(self.app, follow_redirects=False)
        self.vault = service.inbox_vault(self.cfg.inbox_key)

    def put(self, **kw):
        """Сообщение во «Входящие» тем же путём, что у бота и хука."""
        return run(service.inbox_in(self.crm, self.vault, **kw))

    def seed_inbox(self):
        """Четыре обращения: бот Telegram, WhatsApp через шлюз, Авито и
        разобранное из MAX."""
        self.tg = self.put(channel="tg", origin="bot", ext_id="7001", name="Иван Курьеров",
                           username="ivan_kur",
                           text="Когда можно забрать велосипед?")["thread_id"]
        self.wa = self.put(channel="wa", origin="hook", ext_id="+79001112233",
                           phone="+79001112233", name="Азиз Каримов",
                           text="Сколько стоит неделя?")["thread_id"]
        self.av = self.put(channel="avito", origin="avito_api", ext_id="u2i-abc",
                           name="Мария Авито", subject="Электровелосипед Kugoo V3 в аренду",
                           subject_url="https://www.avito.ru/kazan/velosipedy/kugoo_123",
                           text="Ещё актуально?")["thread_id"]
        self.done = self.put(channel="max", origin="max_bot", ext_id="8001",
                             name="Пётр Готовый", text="Спасибо, всё понятно")["thread_id"]
        run(self.crm.update_inbox_thread(self.done, status="done"))

    def as_(self, login, code, password="password-1"):
        """Войти сотрудником на встроенном профиле (code) или на своём (id)."""
        profile_id = (code if isinstance(code, int)
                      else run(self.crm.access_profile_by_code(code))["id"])
        run(self.crm.create_staff(login, logic.hash_password(password), login,
                                  "manager", profile_id))
        self.client.post("/logout")
        r = self.login(login, password)
        self.assertEqual(r.status_code, 303, f"{login} не вошёл")
        return r

    def thread(self, thread_id):
        return run(self.crm.inbox_thread(thread_id))

    def messages(self, thread_id, direction=None):
        rows = run(self.crm.inbox_messages(thread_id))
        return [m for m in rows if direction is None or m["direction"] == direction]

    def text_of(self, message):
        return service.inbox_open(self.vault, message["body_enc"])

    def avito_live(self):
        """Отметка живого опроса Авито - её пишет процесс бота."""
        run(self.crm.set_setting("inbox_avito_state", json.dumps(
            {"ok": True, "at": datetime.now(UTC).isoformat()}), by="test"))


# ─────────────────────── права ───────────────────────


class TestInboxAccess(InboxCase):
    def setUp(self):
        super().setUp()
        self.seed_inbox()

    def test_owner_sees_section_link_and_tile(self):
        self.login()
        page = self.get_ok("/")
        self.assertIn('href="/inbox"', page)
        self.assertIn(">Входящие</a>", page)
        self.assertIn("входящих ждут ответа", page)
        self.assertIn('<a class="tile hot" href="/inbox"><b>3</b>', page,
                      "ждут трое: разобранное из MAX не в счёт")
        self.assertIn("Иван Курьеров", self.get_ok("/inbox"))
        self.get_ok(f"/inbox/{self.tg}")

    def test_tile_counts_only_waiting(self):
        self.login()
        for tid in (self.tg, self.wa, self.av):
            run(service.inbox_answered_elsewhere(self.crm, self.thread(tid), by="t"))
        page = self.get_ok("/")
        self.assertIn('<a class="tile " href="/inbox"><b>0</b>', page,
                      "никто не ждёт - плитка есть, но не горит")

    def test_manager_and_tech_are_refused_everywhere(self):
        before = {tid: (self.thread(tid)["status"], len(self.messages(tid)))
                  for tid in (self.tg, self.wa, self.av, self.done)}
        failed = self.put(channel="tg", origin="bot", ext_id="7002", text="Алло?")
        mid = run(self.crm.queue_inbox_reply(failed["thread_id"], body_enc=None,
                                             author="staff:admin"))
        run(self.crm.claim_inbox_out())
        run(self.crm.finish_inbox_out(mid, ok=False, error="бот заблокирован"))
        for n, code in enumerate(("manager", "tech")):
            with self.subTest(profile=code):
                self.as_(f"user{n}", code)
                page = self.get_ok("/")
                self.assertNotIn('href="/inbox"', page, "ссылки в меню и плитки нет")
                self.assertNotIn("Входящие", page)
                self.assertNotIn("входящих ждут ответа", page)
                for path in ("/inbox", "/inbox?tab=all", "/inbox?channel=wa&q=Азиз",
                             f"/inbox/{self.tg}", f"/inbox/{self.wa}", "/inbox/999999"):
                    r = self.client.get(path)
                    self.assertEqual(r.status_code, 403, path)
                    self.assertNotIn("Иван Курьеров", r.text, path)
                    self.assertNotIn("Когда можно забрать", r.text, path)
                self.assertIn("Входящие", self.client.get("/inbox").text,
                              "отказ называет раздел")
                posts = (
                    (f"/inbox/{self.tg}/reply", {"text": "Взлом", "once": f"x{n}"}),
                    (f"/inbox/{self.tg}/status", {"status": "spam", "note": "x"}),
                    (f"/inbox/{self.wa}/client", {"client": "1"}),
                    (f"/inbox/{self.wa}/answered", {}),
                    (f"/inbox/{failed['thread_id']}/out/{mid}/again", {}),
                    ("/inbox/999999/status", {"status": "done"}),
                )
                for path, data in posts:
                    r = self.client.post(path, data=data)
                    self.assertEqual(r.status_code, 403, path)
        after = {tid: (self.thread(tid)["status"], len(self.messages(tid)))
                 for tid in (self.tg, self.wa, self.av, self.done)}
        self.assertEqual(after, before, "ни одна форма не прошла")
        self.assertEqual([m["status"] for m in self.messages(failed["thread_id"], "out")],
                         ["failed"], "повтор не поставлен")
        self.assertIsNone(self.thread(self.wa)["client_id"])
        self.assertEqual(self.bot.sent, [])

    def test_view_only_profile_reads_but_cannot_act(self):
        pid = run(self.crm.create_access_profile(
            "Смотрит входящие", {"sections": {"inbox": "view"}, "actions": {}}))
        r = self.as_("viewer", pid)
        self.assertEqual(r.headers["location"], "/inbox",
                         "профиль без сводки попадает в свой единственный раздел")
        page = self.get_ok("/inbox")
        self.assertIn("Иван Курьеров", page)
        page = self.get_ok(f"/inbox/{self.tg}")
        self.assertIn("Когда можно забрать велосипед?", page)
        for action in ("/reply", "/status", "/client", "/answered"):
            self.assertNotIn(f'action="/inbox/{self.tg}{action}"', page, action)
        r = self.client.post(f"/inbox/{self.tg}/reply", data={"text": "Да", "once": "v1"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.messages(self.tg, "out"), [])
        # карточка клиента видна именем, но без ссылки в закрытый раздел
        cid = run(self.crm.create_client(full_name="Каримов Азиз", phone="+79001112233"))
        run(self.crm.update_inbox_thread(self.wa, client_id=cid))
        page = self.get_ok(f"/inbox/{self.wa}")
        self.assertIn("Каримов Азиз", page)
        self.assertNotIn(f'href="/clients/{cid}"', page)

    def spy_client_lookups(self):
        """Счётчик поисков карточки: номер или телефон. Поиск показывает
        ФИО, поэтому без раздела «Клиенты» он не должен случиться вовсе."""
        calls = []
        by_id, by_phone = self.crm.client, self.crm.client_by_phone

        async def client(client_id):
            calls.append(("client", client_id))
            return await by_id(client_id)

        async def client_by_phone(phone):
            calls.append(("phone", phone))
            return await by_phone(phone)

        self.crm.client, self.crm.client_by_phone = client, client_by_phone
        return calls

    def test_link_to_client_needs_clients_section(self):
        """Правка «Входящих» без раздела «Клиенты»: обращению можно
        ответить и сменить состояние, но привязка к карточке - это поиск
        по базе клиентов, и он закрыт отказом без единого запроса."""
        cid = run(self.crm.create_client(full_name="Каримов Азиз", phone="+79001112233"))
        pid = run(self.crm.create_access_profile(
            "Входящие без клиентов", {"sections": {"inbox": "edit"}, "actions": {}}))
        self.as_("inboxer", pid)
        page = self.get_ok(f"/inbox/{self.wa}")
        self.assertIn(f'action="/inbox/{self.wa}/status"', page, "правка раздела есть")
        self.assertIn(f'action="/inbox/{self.wa}/answered"', page)
        self.assertNotIn(f'action="/inbox/{self.wa}/client"', page,
                         "формы привязки без раздела «Клиенты» нет")
        self.assertNotIn("Привязать к клиенту", page)
        self.assertIn(f'action="/inbox/{self.tg}/reply"', self.get_ok(f"/inbox/{self.tg}"))
        calls = self.spy_client_lookups()
        for raw in (str(cid), "+79001112233", "8 900 111-22-33", "999999"):
            with self.subTest(client=raw):
                r = self.client.post(f"/inbox/{self.wa}/client", data={"client": raw})
                self.assertEqual(r.status_code, 403)
                self.assertIn("Нет доступа", r.text, "отказ страницей, а не голым 403")
                self.assertIn(logic.SECTIONS["clients"], r.text,
                              "отказ называет недостающий раздел")
                self.assertNotIn("Каримов Азиз", r.text, "ФИО из поиска не утекло")
        self.assertEqual(calls, [], "карточку даже не искали")
        t = self.thread(self.wa)
        self.assertEqual((t["client_id"], t["client_manual"], t["handled_by"]),
                         (None, False, None))
        # и отвязать уже привязанное тоже нельзя
        run(self.crm.update_inbox_thread(self.wa, client_id=cid))
        page = self.get_ok(f"/inbox/{self.wa}")
        self.assertIn("Каримов Азиз", page)
        self.assertNotIn("Отвязать от карточки", page)
        r = self.client.post(f"/inbox/{self.wa}/client", data={"client": ""})
        self.assertEqual(r.status_code, 403)
        t = self.thread(self.wa)
        self.assertEqual((t["client_id"], t["client_manual"]), (cid, False))
        self.assertEqual(calls, [])

    def test_link_to_client_with_clients_view_is_allowed(self):
        """Смотреть «Клиентов» достаточно: привязка карточки не меняет."""
        cid = run(self.crm.create_client(full_name="Каримов Азиз", phone="+79001112233"))
        pid = run(self.crm.create_access_profile(
            "Входящие и клиенты", {"sections": {"inbox": "edit", "clients": "view"},
                                   "actions": {}}))
        self.as_("linker", pid)
        page = self.get_ok(f"/inbox/{self.wa}")
        self.assertIn(f'action="/inbox/{self.wa}/client"', page)
        r = self.client.post(f"/inbox/{self.wa}/client", data={"client": "8 900 111-22-33"})
        self.assertEqual(r.status_code, 303)
        t = self.thread(self.wa)
        self.assertEqual((t["client_id"], t["client_manual"], t["handled_by"]),
                         (cid, True, "staff:linker"))

    def test_anonymous_is_sent_to_login(self):
        for path in ("/inbox", f"/inbox/{self.tg}"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 303, path)
            self.assertTrue(r.headers["location"].startswith("/login?next="), path)
        r = self.client.post(f"/inbox/{self.tg}/reply", data={"text": "x"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.messages(self.tg, "out"), [])


# ─────────────────────── список ───────────────────────


class TestInboxList(InboxCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed_inbox()

    def test_open_tab_by_default(self):
        page = self.get_ok("/inbox")
        for name in ("Иван Курьеров", "Азиз Каримов", "Мария Авито"):
            self.assertIn(name, page)
        self.assertNotIn("Пётр Готовый", page, "разобранное в «Открытых» не видно")
        # превью - расшифрованное последнее сообщение
        self.assertIn("Когда можно забрать велосипед?", page)
        self.assertIn("Электровелосипед Kugoo V3 в аренду", page)
        self.assertIn(logic.inbox_no(self.wa), page)
        self.assertIn("<b>3</b><span>новых</span>", page)
        self.assertNotIn("Не задан ключ", page)

    def test_tabs(self):
        page = self.get_ok("/inbox?tab=done")
        self.assertIn("Пётр Готовый", page)
        self.assertNotIn("Иван Курьеров", page)
        page = self.get_ok("/inbox?tab=all")
        for name in ("Иван Курьеров", "Азиз Каримов", "Мария Авито", "Пётр Готовый"):
            self.assertIn(name, page)
        self.assertIn("Обращений нет.", self.get_ok("/inbox?tab=spam"))
        page = self.get_ok("/inbox?tab=выдумка")
        self.assertIn("Иван Курьеров", page, "неизвестная вкладка - открытые")
        self.assertNotIn("Пётр Готовый", page)

    def test_channel_filter(self):
        page = self.get_ok("/inbox?channel=wa")
        self.assertIn("Азиз Каримов", page)
        self.assertNotIn("Иван Курьеров", page)
        self.assertNotIn("Мария Авито", page)
        page = self.get_ok("/inbox?tab=all&channel=max")
        self.assertIn("Пётр Готовый", page)
        self.assertNotIn("Азиз Каримов", page)
        page = self.get_ok("/inbox?channel=sms")
        self.assertIn("Азиз Каримов", page, "неизвестный канал - без фильтра")
        self.assertIn("Иван Курьеров", page)

    def test_search(self):
        page = self.get_ok("/inbox?q=kugoo")
        self.assertIn("Мария Авито", page, "поиск по объявлению без регистра")
        self.assertNotIn("Азиз Каримов", page)
        self.assertNotIn("Иван Курьеров", page)
        page = self.get_ok("/inbox?q=900+111-22-33")
        self.assertIn("Азиз Каримов", page, "телефон ищется по цифрам")
        self.assertNotIn("Мария Авито", page)
        page = self.get_ok("/inbox?q=ivan_kur")
        self.assertIn("Иван Курьеров", page)
        self.assertNotIn("Азиз Каримов", page)
        self.assertIn("Обращений нет.", self.get_ok("/inbox?q=никого-такого"))

    def test_sort_is_whitelisted(self):
        page = self.get_ok("/inbox?sort=name&dir=desc")
        order = [page.index(n) for n in ("Мария Авито", "Иван Курьеров", "Азиз Каримов")]
        self.assertEqual(order, sorted(order), "по имени от Я к А")
        page = self.get_ok("/inbox?sort=name&dir=asc")
        order = [page.index(n) for n in ("Азиз Каримов", "Иван Курьеров", "Мария Авито")]
        self.assertEqual(order, sorted(order))
        for sort in ("wait", "last", "channel", "status", "body_enc", "id;drop"):
            self.get_ok(f"/inbox?sort={sort}&dir=desc")

    def test_long_wait_is_highlighted(self):
        tid = self.put(channel="wa", origin="hook", ext_id="+79007770000",
                       phone="+79007770000", name="Долго Ждёт", text="Ау",
                       at=datetime.now(UTC) - timedelta(hours=3, minutes=5))["thread_id"]
        page = self.get_ok("/inbox")
        self.assertIn('<div class="tile warn"><b>1</b><span>ждут ответа дольше часа</span>',
                      page)
        self.assertIn("<b>3 ч</b>", page)
        first = page.index(logic.inbox_no(tid))
        self.assertLess(first, page.index("Иван Курьеров"), "дольше всех ждёт - первым")

    def test_client_card_name_wins(self):
        cid = run(self.crm.create_client(full_name="Иванов Иван Клиентович",
                                         phone="+79990000000", tg_id=7003))
        tid = self.put(channel="tg", origin="bot", ext_id="7003", name="Ваня",
                       text="Хочу продлить")["thread_id"]
        self.assertEqual(self.thread(tid)["client_id"], cid, "карточка найдена по tg_id")
        page = self.get_ok("/inbox")
        self.assertIn("Иванов Иван Клиентович", page)
        self.assertIn("клиент", page)


# ─────────────────────── карточка обращения ───────────────────────


class TestInboxThread(InboxCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed_inbox()

    def test_text_is_stored_encrypted_and_shown_decrypted(self):
        [msg] = self.messages(self.wa)
        self.assertTrue(msg["body_enc"].startswith("v1:"))
        self.assertNotIn("неделя", msg["body_enc"])
        page = self.get_ok(f"/inbox/{self.wa}")
        self.assertIn("Сколько стоит неделя?", page)
        self.assertIn("WhatsApp", page)
        self.assertIn("(через шлюз)", page)
        self.assertIn('href="https://wa.me/79001112233"', page)
        self.assertIn("+79001112233", page)

    def test_whatsapp_has_no_reply_form(self):
        page = self.get_ok(f"/inbox/{self.wa}")
        self.assertNotIn(f'action="/inbox/{self.wa}/reply"', page)
        self.assertIn("ответьте по ссылке wa.me", page)
        self.assertIn(f'action="/inbox/{self.wa}/answered"', page)

    def test_telegram_bot_thread_has_reply_form_with_once_key(self):
        page = self.get_ok(f"/inbox/{self.tg}")
        self.assertIn(f'action="/inbox/{self.tg}/reply"', page)
        self.assertIn('name="once"', page)
        self.assertIn('maxlength="3500"', page)
        self.assertIn('href="https://t.me/ivan_kur"', page)
        self.assertIn("не в боте", page, "состояние в боте берётся из bot.users")

    def test_avito_link_and_dead_polling(self):
        page = self.get_ok(f"/inbox/{self.av}")
        self.assertIn('href="https://www.avito.ru/kazan/velosipedy/kugoo_123"', page)
        self.assertNotIn(f'action="/inbox/{self.av}/reply"', page)
        self.assertIn("Опрос Авито не работает", page)
        self.avito_live()
        page = self.get_ok(f"/inbox/{self.av}")
        self.assertIn(f'action="/inbox/{self.av}/reply"', page)
        self.assertIn('maxlength="1000"', page)

    def test_max_login_is_not_a_telegram_link(self):
        """Логин MAX - не логин Telegram: t.me по нему открыл бы чужого
        человека. Подпись называет канал, ссылки нет."""
        tid = self.put(channel="max", origin="max_bot", ext_id="8002", name="Пётр Макс",
                       username="petr_max", text="Здравствуйте")["thread_id"]
        page = self.get_ok(f"/inbox/{tid}")
        self.assertIn("<dt>Логин в MAX</dt><dd>@petr_max</dd>", page)
        self.assertNotIn("t.me/", page)
        self.assertNotIn("<dt>Telegram</dt>", page)
        # у Telegram подпись и ссылка прежние
        page = self.get_ok(f"/inbox/{self.tg}")
        self.assertIn('<dt>Telegram</dt><dd><a href="https://t.me/ivan_kur"', page)
        self.assertNotIn("Логин в", page)

    def test_missing_thread_is_404(self):
        self.assertEqual(self.client.get("/inbox/999999").status_code, 404)
        for action in ("reply", "status", "client", "answered"):
            r = self.client.post(f"/inbox/999999/{action}", data={"text": "x"})
            self.assertEqual(r.status_code, 404, action)
        r = self.client.post(f"/inbox/999999/out/{self.tg}/again")
        self.assertEqual(r.status_code, 404)

    def test_without_key_text_is_not_readable_and_reply_is_off(self):
        self.build(inbox_key="", inbox_hook_token=HOOK)
        self.login()
        page = self.get_ok("/inbox")
        self.assertIn("Не задан ключ", page)
        self.assertNotIn("Когда можно забрать велосипед?", page)
        page = self.get_ok(f"/inbox/{self.tg}")
        self.assertIn("[текст зашифрован, ключа INBOX_KEY нет]", page)
        self.assertNotIn(f'action="/inbox/{self.tg}/reply"', page)
        self.assertIn("INBOX_KEY", page)
        r = self.client.post(f"/inbox/{self.tg}/reply", data={"text": "Да", "once": "nk"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.messages(self.tg, "out"), [], "открытым текстом не пишем")
        self.assertIn("INBOX_KEY", r.text)
        # без ключа новое сообщение пишется без текста
        tid = self.put(channel="wa", origin="hook", ext_id="+79005550000",
                       phone="+79005550000", text="секрет")["thread_id"]
        self.assertIsNone(self.messages(tid)[0]["body_enc"])

    def test_foreign_key_does_not_break_the_page(self):
        self.build(inbox_key=OTHER_KEY, inbox_hook_token=HOOK)
        self.login()
        with self.assertLogs("app.services.crypto", level="ERROR"):
            page = self.get_ok(f"/inbox/{self.tg}")
            self.get_ok("/inbox")
        self.assertIn("[не расшифровано]", page)
        self.assertNotIn("Когда можно забрать велосипед?", page)

    def test_html_everywhere_is_escaped(self):
        tid = self.put(channel="wa", origin="hook", ext_id="+79005556677",
                       phone="+79005556677", name="<script>alert(1)</script>",
                       subject="<img src=x onerror=alert(2)>",
                       subject_url="javascript:alert(3)",
                       text="<script>alert(4)</script>")["thread_id"]
        self.assertIsNone(self.thread(tid)["subject_url"], "чужая ссылка не сохраняется")
        run(self.crm.update_inbox_thread(tid, note='"><script>alert(5)</script>'))
        tg = self.put(channel="tg", origin="bot", ext_id="7009", username="<b>x</b>",
                      text="привет")["thread_id"]
        mid = run(self.crm.queue_inbox_reply(tg, body_enc=service.inbox_seal(
            self.vault, "<script>alert(6)</script>"), author="staff:<i>admin</i>"))
        run(self.crm.claim_inbox_out())
        run(self.crm.finish_inbox_out(mid, ok=False, error="<script>alert(7)</script>"))
        # ошибку опроса Авито пишет процесс бота со слов площадки
        run(self.crm.set_setting("inbox_avito_state", json.dumps(
            {"ok": False, "at": datetime.now(UTC).isoformat(),
             "error": "<script>alert(9)</script>"}), by="test"))
        pages = {
            "список": self.get_ok("/inbox"),
            "поиск": self.get_ok("/inbox?q=%3Cscript%3Ealert(8)%3C/script%3E"),
            "карточка": self.get_ok(f"/inbox/{tid}"),
            "ответ": self.get_ok(f"/inbox/{tg}"),
        }
        for label, page in pages.items():
            with self.subTest(page=label):
                self.assertNotIn("<script>alert", page)
                self.assertNotIn("<img src=x", page)
                self.assertNotIn("javascript:alert", page)
                self.assertNotIn("<b>x</b>", page)
                self.assertNotIn("<i>admin</i>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", pages["список"])
        self.assertIn("&lt;script&gt;alert(9)", pages["список"])
        self.assertIn("&lt;script&gt;alert(8)", pages["поиск"])
        self.assertIn("&lt;script&gt;alert(4)&lt;/script&gt;", pages["карточка"])
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", pages["карточка"])
        self.assertIn("&lt;script&gt;alert(5)", pages["карточка"])
        self.assertIn("&lt;script&gt;alert(6)", pages["ответ"])
        self.assertIn("&lt;script&gt;alert(7)", pages["ответ"])


# ─────────────────────── действия ───────────────────────


class TestInboxActions(InboxCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed_inbox()

    def reply(self, thread_id, text, once="k1"):
        """Принятый ответ - редирект на обращение. Отказ - та же страница
        сразу (400) с причиной и набранным текстом в поле: редирект
        потерял бы черновик. Страница отказа - в self.last."""
        data = {"text": text}
        if once is not None:
            data["once"] = once
        r = self.client.post(f"/inbox/{thread_id}/reply", data=data)
        self.last = r
        if r.status_code == 400:
            self.assertIn('class="flash err"', r.text)
            return r
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], f"/inbox/{thread_id}")
        return r

    def test_reply_queues_exactly_one_message_and_panel_sends_nothing(self):
        self.reply(self.tg, "  Можно сегодня до 20:00  ")
        [out] = self.messages(self.tg, "out")
        self.assertEqual(out["status"], "queued")
        self.assertEqual(out["author"], "staff:admin")
        self.assertEqual(self.text_of(out), "Можно сегодня до 20:00")
        self.assertNotIn("20:00", out["body_enc"], "ответ лежит шифротекстом")
        t = self.thread(self.tg)
        self.assertEqual(t["status"], "work", "ответ берёт обращение в работу")
        self.assertEqual(t["handled_by"], "staff:admin")
        self.assertIsNotNone(t["waiting_since"], "ожидание снимает отправка, не очередь")
        self.assertEqual(self.bot.sent, [], "отправляет процесс бота, не панель")
        page = self.get_ok(f"/inbox/{self.tg}")
        self.assertIn("Ответ в очереди", page)
        self.assertIn("в очереди", page)

    def test_same_once_key_is_skipped(self):
        self.reply(self.tg, "Первый", once="dbl")
        self.reply(self.tg, "Первый", once="dbl")
        self.assertEqual(len(self.messages(self.tg, "out")), 1)
        self.assertIn("повторное нажатие пропущено", self.get_ok(f"/inbox/{self.tg}"))
        self.assertEqual(self.bot.sent, [])

    def test_refused_form_can_be_resubmitted(self):
        # Отказ пришёл страницей на POST: F5 повторяет форму с тем же ключом.
        # Ключ отказанной формы освобождён - иначе неотправленный ответ
        # назывался бы «уже отправлен».
        self.reply(self.tg, "Первый", once="p1")
        self.reply(self.tg, "Второй", once="p2")
        self.assertEqual(self.last.status_code, 400)
        run(self.crm.claim_inbox_out())
        run(self.crm.finish_inbox_out(self.messages(self.tg, "out")[0]["id"], ok=True))
        self.reply(self.tg, "Второй", once="p2")
        self.assertEqual(self.last.status_code, 303)
        self.assertEqual([self.text_of(m) for m in self.messages(self.tg, "out")],
                         ["Первый", "Второй"])
        # а принятая форма по-прежнему не повторяется
        self.reply(self.tg, "Второй", once="p2")
        self.assertEqual(len(self.messages(self.tg, "out")), 2)

    def test_one_reply_in_queue_per_thread(self):
        self.reply(self.tg, "Первый", once="a")
        self.reply(self.tg, "Второй", once="b")
        self.assertEqual(self.last.status_code, 400)
        self.assertIn("Второй", self.last.text, "набранный ответ остался в поле")
        self.reply(self.tg, "Третий", once=None)
        outs = self.messages(self.tg, "out")
        self.assertEqual([self.text_of(m) for m in outs], ["Первый"])
        self.assertIn("Предыдущий ответ ещё отправляется", self.last.text)

    def test_refused_reply_is_the_thread_page_with_the_draft(self):
        """Отказ - не редирект, а та же карточка с кодом 400: причина во
        флеше, набранный текст в поле ответа (экранированным), переписка
        на месте. Флеш показан один раз - следующая загрузка чистая."""
        self.reply(self.tg, "Первый", once="a")
        draft = 'Второй & <b>жирный</b></textarea><script>alert(1)</script>'
        r = self.reply(self.tg, draft, once="b")
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("location", r.headers, "не редирект: черновик потерялся бы")
        self.assertIn("Предыдущий ответ ещё отправляется", r.text)
        self.assertIn("Когда можно забрать велосипед?", r.text, "это карточка обращения")
        self.assertIn(f'action="/inbox/{self.tg}/reply"', r.text)
        self.assertIn('name="once"', r.text, "новый ключ формы для следующей попытки")
        escaped = ("Второй &amp; &lt;b&gt;жирный&lt;/b&gt;&lt;/textarea&gt;"
                   "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertIn(f'от имени бота">{escaped}</textarea>', r.text,
                      "набранный текст - в поле ответа")
        self.assertNotIn("<script>alert(1)", r.text)
        self.assertNotIn("<b>жирный</b>", r.text)
        self.assertEqual([self.text_of(m) for m in self.messages(self.tg, "out")],
                         ["Первый"], "в очередь ничего не добавилось")
        page = self.get_ok(f"/inbox/{self.tg}")
        self.assertNotIn("Предыдущий ответ ещё отправляется", page, "флеш уже показан")
        self.assertIn('от имени бота"></textarea>', page, "черновик только в ответе на отказ")

    def test_too_long_reply_keeps_the_whole_draft(self):
        """Длинный ответ возвращается в поле целиком: сократить его должен
        человек, а не панель молча."""
        long_text = "я" * 3501
        r = self.reply(self.tg, long_text, once="long")
        self.assertEqual(r.status_code, 400)
        self.assertIn("длиннее 3500", r.text)
        self.assertIn(f'от имени бота">{long_text}</textarea>', r.text)
        self.assertEqual(self.messages(self.tg, "out"), [])

    def test_reply_without_once_key_still_works(self):
        """Старая вкладка без ключа формы не хуже, чем была до ключа."""
        self.reply(self.tg, "Без ключа", once=None)
        self.assertEqual(len(self.messages(self.tg, "out")), 1)

    def test_bad_replies_are_refused(self):
        self.reply(self.tg, "   ", once="e1")
        self.assertIn("Напишите текст ответа", self.last.text)
        self.reply(self.tg, "я" * 3501, once="e2")
        self.assertIn("длиннее 3500", self.last.text)
        self.reply(self.wa, "Здравствуйте", once="e3")
        self.assertIn("WhatsApp", self.get_ok(f"/inbox/{self.wa}"))
        self.reply(self.av, "Актуально", once="e4")
        self.assertIn("Опрос Авито не работает", self.get_ok(f"/inbox/{self.av}"))
        run(self.crm.update_inbox_thread(self.tg, status="spam"))
        self.reply(self.tg, "Спаму не отвечаем", once="e5")
        for tid in (self.tg, self.wa, self.av):
            self.assertEqual(self.messages(tid, "out"), [], tid)
        self.assertEqual(self.thread(self.wa)["status"], "new", "отказ не берёт в работу")

    def test_line_breaks_count_as_the_browser_counts_them(self):
        # Перевод строки браузер считает одним знаком, а форма шлёт CRLF:
        # 3500 знаков с переносами по счёту браузера проходят.
        text = "\r\n".join(["я" * 99] * 35)
        self.assertLessEqual(len(text.replace("\r\n", "\n")), 3500)
        self.assertGreater(len(text), 3500)
        self.reply(self.tg, text, once="crlf")
        self.assertEqual(self.last.status_code, 303)
        [out] = self.messages(self.tg, "out")
        self.assertNotIn("\r", self.text_of(out))

    def test_bot_only_threads_refuse_panel_reply_when_not_from_bot(self):
        """Telegram-обращение, заведённое не ботом, ответа из панели не даёт:
        бот может писать только тем, кто писал ему сам."""
        tid = self.put(channel="tg", origin="hook", ext_id="7010", text="x")["thread_id"]
        self.reply(tid, "Ответ", once="h1")
        self.assertEqual(self.messages(tid, "out"), [])
        self.assertIn("ответит только сам бот", self.get_ok(f"/inbox/{tid}"))

    def test_avito_reply_when_polling_is_live(self):
        self.avito_live()
        self.reply(self.av, "я" * 1001, once="a1")
        self.assertEqual(self.messages(self.av, "out"), [], "у Авито предел 1000 знаков")
        self.reply(self.av, "я" * 1000, once="a2")
        [out] = self.messages(self.av, "out")
        self.assertEqual(len(self.text_of(out)), 1000)
        self.assertEqual(self.bot.sent, [])

    def test_status_and_note(self):
        r = self.client.post(f"/inbox/{self.wa}/status",
                             data={"status": "done", "note": " перезвонил сам "})
        self.assertEqual(r.status_code, 303)
        t = self.thread(self.wa)
        self.assertEqual((t["status"], t["note"], t["handled_by"]),
                         ("done", "перезвонил сам", "staff:admin"))
        self.assertIn("перезвонил сам", self.get_ok(f"/inbox/{self.wa}"))
        # без ключа формы: статус не денежный, повтор ничего не ломает
        self.client.post(f"/inbox/{self.wa}/status", data={"status": "spam"})
        self.assertEqual(self.thread(self.wa)["status"], "spam")
        self.assertEqual(self.thread(self.wa)["note"], "перезвонил сам",
                         "без поля заметки заметка не стирается")
        self.assertIn("Пётр Готовый", self.get_ok("/inbox?tab=all"))

    def test_bad_status_and_long_note_change_nothing(self):
        self.client.post(f"/inbox/{self.wa}/status", data={"status": "archived"})
        self.assertIn("Нет такого состояния", self.get_ok(f"/inbox/{self.wa}"))
        self.client.post(f"/inbox/{self.wa}/status",
                         data={"status": "done", "note": "x" * (logic.NOTE_LIMIT + 1)})
        self.assertIn("Заметка", self.get_ok(f"/inbox/{self.wa}"))
        t = self.thread(self.wa)
        self.assertEqual((t["status"], t["note"], t["handled_by"]), ("new", None, None))

    def test_link_client_by_card_and_phone_then_unlink(self):
        cid = run(self.crm.create_client(full_name="Каримов Азиз", phone="+79001112233"))
        self.client.post(f"/inbox/{self.wa}/client", data={"client": str(cid)})
        self.assertEqual(self.thread(self.wa)["client_id"], cid)
        page = self.get_ok(f"/inbox/{self.wa}")
        self.assertIn("Привязано к карточке: Каримов Азиз.", page)
        self.assertIn(f'href="/clients/{cid}"', page)
        self.client.post(f"/inbox/{self.wa}/client", data={"client": ""})
        self.assertIsNone(self.thread(self.wa)["client_id"])
        self.assertIn("отвязано", self.get_ok(f"/inbox/{self.wa}"))
        self.client.post(f"/inbox/{self.av}/client", data={"client": "8 900 111-22-33"})
        self.assertEqual(self.thread(self.av)["client_id"], cid, "по телефону")
        self.client.post(f"/inbox/{self.tg}/client", data={"client": "999999"})
        self.assertIsNone(self.thread(self.tg)["client_id"])
        self.assertIn("Клиента с таким номером", self.get_ok(f"/inbox/{self.tg}"))

    def test_answered_elsewhere_stops_waiting(self):
        self.assertEqual(run(self.crm.inbox_open_count()), 3)
        r = self.client.post(f"/inbox/{self.wa}/answered")
        self.assertEqual(r.status_code, 303)
        [out] = self.messages(self.wa, "out")
        self.assertIsNone(out["body_enc"])
        self.assertEqual(out["author"], "staff:admin")
        t = self.thread(self.wa)
        self.assertIsNone(t["waiting_since"])
        self.assertEqual(t["status"], "work")
        self.assertEqual(run(self.crm.inbox_open_count()), 2)
        page = self.get_ok(f"/inbox/{self.wa}")
        self.assertIn("Отмечено: ответили вне панели.", page)
        self.assertIn("ответили вне панели", page)
        self.assertEqual(self.bot.sent, [])

    def fail_reply(self, thread_id, text="Можно"):
        mid = run(self.crm.queue_inbox_reply(thread_id, body_enc=service.inbox_seal(
            self.vault, text), author="staff:admin"))
        run(self.crm.claim_inbox_out())
        run(self.crm.finish_inbox_out(mid, ok=False, error="Forbidden: bot was blocked"))
        return mid

    def test_retry_failed_reply(self):
        mid = self.fail_reply(self.tg)
        page = self.get_ok(f"/inbox/{self.tg}")
        self.assertIn("не ушло: Forbidden: bot was blocked", page)
        self.assertIn(f'action="/inbox/{self.tg}/out/{mid}/again"', page)
        r = self.client.post(f"/inbox/{self.tg}/out/{mid}/again")
        self.assertEqual(r.status_code, 303)
        # Та же строка обратно в очередь: кнопки на старом «не ушло» больше
        # нет, и повторное нажатие не отправит человеку дубль.
        [out] = self.messages(self.tg, "out")
        self.assertEqual((out["id"], out["status"]), (mid, "queued"))
        self.assertEqual(self.text_of(out), "Можно", "тот же текст")
        self.assertEqual(out["author"], "staff:admin")
        page = self.get_ok(f"/inbox/{self.tg}")
        self.assertIn("Ответ снова в очереди.", page)
        self.assertNotIn(f"/out/{mid}/again", page)
        # второй повтор, пока первый в очереди, - отказ, а не второе сообщение
        self.client.post(f"/inbox/{self.tg}/out/{mid}/again")
        self.assertEqual(len(self.messages(self.tg, "out")), 1)
        self.assertIn("Повторить можно только", self.get_ok(f"/inbox/{self.tg}"))
        self.assertEqual(self.bot.sent, [])

    def test_retry_only_failed_reply_of_this_thread(self):
        mid = self.fail_reply(self.tg)
        r = self.client.post(f"/inbox/{self.wa}/out/{mid}/again")
        self.assertEqual(r.status_code, 404, "чужой ответ через свой адрес не повторить")
        self.assertEqual(len(self.messages(self.tg, "out")), 1)
        [incoming] = self.messages(self.wa, "in")
        self.client.post(f"/inbox/{self.wa}/out/{incoming['id']}/again")
        self.assertEqual(self.messages(self.wa, "out"), [], "входящее не повторяется")
        self.assertIn("Повторить можно только", self.get_ok(f"/inbox/{self.wa}"))


# ─────────────────────── хук ───────────────────────


class TestInboxHook(InboxCase):
    def hook(self, payload=None, *, token=HOOK, content=None, headers=None, client=None):
        head = dict(headers or {})
        if token is not None:
            head["Authorization"] = f"Bearer {token}"
        if content is None:
            content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return (client or self.client).post("/hook/inbox", content=content, headers=head)

    def counts(self, r):
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        return body["saved"], body["duplicates"], body["skipped"]

    def threads(self):
        return run(self.crm.inbox_threads(limit=1000))

    def test_no_token_configured_means_no_hook(self):
        self.build(inbox_key=KEY, inbox_hook_token="")
        for token in (HOOK, "", None):
            r = self.hook(green(), token=token)
            self.assertEqual(r.status_code, 404, token)
        self.assertEqual(self.threads(), [])

    def test_wrong_or_missing_token_is_401(self):
        cases = {
            "без заголовка": {},
            "чужой токен": {"Authorization": "Bearer nope"},
            "пустой Bearer": {"Authorization": "Bearer "},
            "Basic": {"Authorization": f"Basic {HOOK}"},
            "токен без схемы": {"Authorization": HOOK},
            "префикс токена": {"Authorization": f"Bearer {HOOK[:-1]}"},
            "токен длиннее": {"Authorization": f"Bearer {HOOK}x"},
        }
        for label, headers in cases.items():
            with self.subTest(label):
                r = self.hook(green(), token=None, headers=headers)
                self.assertEqual(r.status_code, 401)
                self.assertNotIn("set-cookie", r.headers)
        self.assertEqual(self.threads(), [])
        # схема без учёта регистра - как в RFC 7235
        r = self.hook(green(), token=None, headers={"Authorization": f"bearer {HOOK}"})
        self.assertEqual(self.counts(r), (1, 0, 0))

    def test_panel_session_does_not_replace_the_token(self):
        self.login()
        r = self.hook(green(), token=None)
        self.assertEqual(r.status_code, 401, "вход в панель - не пропуск в хук")
        self.assertEqual(self.threads(), [])

    def test_many_failures_from_one_address_are_throttled(self):
        for i in range(logic.HOOK_FAIL_LIMIT):
            r = self.hook(green(), token=f"guess-{i}")
            self.assertEqual(r.status_code, 401, i)
        r = self.hook(green(), token="guess-last")
        self.assertEqual(r.status_code, 429)
        self.assertNotIn("set-cookie", r.headers)
        self.assertEqual(self.threads(), [])
        # Верный токен с того же адреса проходит: шлюзы WhatsApp шлют с
        # общих адресов, и чужой инстанс не должен запирать наш хук.
        self.assertEqual(self.counts(self.hook(green())), (1, 0, 0))
        # счёт неудач хука - свой: вход в панель с того же адреса открыт
        self.assertEqual(self.login().status_code, 303)

    def test_valid_token_is_neither_counted_nor_throttled(self):
        """Токен проверяется раньше счёта неудач: верные запросы в счёт не
        идут и паузой не запираются, а неверные с того же адреса остаются
        под паузой - удачный запрос счёт не обнуляет."""
        for i in range(logic.HOOK_FAIL_LIMIT + 5):
            self.assertEqual(self.counts(self.hook(green(msg_id=f"OK{i}"))), (1, 0, 0), i)
        self.assertEqual(self.hook(green(), token="guess-first").status_code, 401,
                         "верные запросы неудачами не считались")
        for i in range(logic.HOOK_FAIL_LIMIT - 1):
            self.assertEqual(self.hook(green(), token=f"guess-{i}").status_code, 401, i)
        self.assertEqual(self.hook(green(), token="guess-last").status_code, 429)
        for i in range(3):
            self.assertEqual(self.counts(self.hook(green(msg_id=f"LATE{i}"))), (1, 0, 0), i)
        self.assertEqual(self.hook(green(), token="guess-again").status_code, 429,
                         "удачный запрос паузу для подбора не снимает")
        [t] = self.threads()
        self.assertEqual(len(self.messages(t["id"])), logic.HOOK_FAIL_LIMIT + 5 + 3)

    def test_below_the_limit_is_not_throttled(self):
        for i in range(logic.HOOK_FAIL_LIMIT - 1):
            self.assertEqual(self.hook(green(), token=f"guess-{i}").status_code, 401)
        self.assertEqual(self.counts(self.hook(green())), (1, 0, 0))

    def test_too_large_body_is_413(self):
        big = json.dumps({"channel": "wa", "ext_id": "+79001234567",
                          "text": "я" * logic.HOOK_MAX_BYTES}).encode()
        self.assertGreater(len(big), logic.HOOK_MAX_BYTES)
        r = self.hook(content=big)
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self.threads(), [])

    def test_too_large_chunked_body_is_413(self):
        """Без Content-Length тело считается по ходу чтения."""
        chunk = b" " * 4096

        def body():
            yield b'{"channel": "wa", "ext_id": "+79001234567", "text": "x"'
            for _ in range(logic.HOOK_MAX_BYTES // len(chunk) + 2):
                yield chunk
            yield b"}"

        r = self.client.post("/hook/inbox", content=body(),
                             headers={"Authorization": f"Bearer {HOOK}"})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self.threads(), [])

    def test_body_at_the_limit_is_accepted(self):
        payload = json.dumps({"channel": "wa", "ext_id": "+79001234567",
                              "text": "ok"}).encode()
        padded = payload[:-1] + b" " * (logic.HOOK_MAX_BYTES - len(payload)) + b"}"
        self.assertEqual(len(padded), logic.HOOK_MAX_BYTES)
        self.assertEqual(self.counts(self.hook(content=padded)), (1, 0, 0))

    def test_bad_json_is_400(self):
        for content in (b"{not json", b'{"a": ', b"\xff\xfe\x00", b"{'a': 1}"):
            with self.subTest(content=content):
                r = self.hook(content=content)
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json()["error"], "not json")
        self.assertEqual(self.threads(), [])

    def test_empty_and_foreign_shapes_are_skipped_not_errors(self):
        self.assertEqual(self.counts(self.hook(content=b"")), (0, 0, 1))
        self.assertEqual(self.counts(self.hook([1, 2, 3])), (0, 0, 1))
        self.assertEqual(self.counts(self.hook("строка")), (0, 0, 1))
        self.assertEqual(self.counts(self.hook({"test": True})), (0, 0, 0),
                         "проверка адреса от Wazzup")
        self.assertEqual(self.counts(self.hook({"typeWebhook": "stateInstanceChanged",
                                                "stateInstance": "authorized"})),
                         (0, 0, 1))
        self.assertEqual(self.threads(), [])

    def quiet_client(self):
        """Клиент, который отдаёт 500 ответом, а не исключением теста."""
        return tw.TestClient(self.app, follow_redirects=False,
                             raise_server_exceptions=False)

    def test_malformed_green_payload_is_skipped_not_500(self):
        """«Неизвестная форма - пусто, а не ошибка» (logic.parse_inbound):
        кривое тело от шлюза или n8n отвечает 200 с пропуском, а не 500."""
        bad_data = green()
        bad_data["messageData"] = "textMessage"
        bad_text = green(msg_id="X2")
        bad_text["messageData"] = {"typeMessage": "textMessage",
                                   "textMessageData": "привет"}
        client = self.quiet_client()
        for label, payload in (("messageData строкой", bad_data),
                               ("textMessageData строкой", bad_text)):
            with self.subTest(label):
                r = self.hook(payload, client=client)
                self.assertEqual(r.status_code, 200, f"{label}: {r.status_code}")
                self.assertEqual(r.json()["saved"] + r.json()["skipped"], 1)

    def nul_guard(self):
        """Как Postgres: NUL в строковом поле роняет запись ошибкой базы
        (CharacterNotInRepertoireError, не ValueError). Заглушка его
        пропустила бы молча, а в проде это 500 на каждой повторной
        доставке пачки."""
        record = self.crm.inbox_record

        class DatabaseError(Exception):
            pass

        async def guarded(**kw):
            for key, value in kw.items():
                if isinstance(value, str) and "\x00" in value:
                    raise DatabaseError(f"invalid byte sequence 0x00 в поле {key}")
            return await record(**kw)

        self.crm.inbox_record = guarded

    def test_nul_in_text_fields_is_dropped_not_500(self):
        self.nul_guard()
        client = self.quiet_client()
        n8n = {"channel": "avito", "ext_id": "chat\x00-9", "msg_id": "m\x001",
               "name": "Ол\x00ег", "text": "При\x00вет", "subject": "Kugoo\x00 V3",
               "subject_url": "https://www.avito.ru/kazan/x\x00_1"}
        r = self.hook(n8n, client=client)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.counts(r), (1, 0, 0))
        wa = green(name="Аз\x00из", text="Сколько\x00 стоит?", msg_id="NUL\x00-1")
        self.assertEqual(self.counts(self.hook(wa, client=client)), (1, 0, 0))
        wazzup = {"messages": [{"messageId": "wz\x00-1", "chatType": "whatsapp",
                                "chatId": "79005554433", "type": "text",
                                "text": "Добрый\x00 день", "contact": {"name": "Рус\x00там"}}]}
        self.assertEqual(self.counts(self.hook(wazzup, client=client)), (1, 0, 0))
        by_channel = {}
        for t in self.threads():
            by_channel.setdefault(t["channel"], []).append(t)
        [avito] = by_channel["avito"]
        self.assertEqual((avito["ext_id"], avito["name"], avito["subject"]),
                         ("chat-9", "Олег", "Kugoo V3"))
        self.assertIsNone(avito["subject_url"], "ссылка с управляющим знаком - подделка")
        [m] = self.messages(avito["id"])
        self.assertEqual((m["ext_id"], self.text_of(m)), ("m1", "Привет"))
        names = sorted(t["name"] for t in by_channel["wa"])
        self.assertEqual(names, ["Азиз", "Рустам"])
        texts = sorted(self.text_of(m) for t in by_channel["wa"]
                       for m in self.messages(t["id"]))
        self.assertEqual(texts, ["Добрый день", "Сколько стоит?"])
        for t in self.threads():
            for m in self.messages(t["id"]):
                self.assertNotIn("\x00", m["ext_id"] or "")
        # повторная доставка той же пачки - повтор, а не новая строка
        self.assertEqual(self.counts(self.hook(n8n, client=client)), (0, 1, 0))

    def test_deeply_nested_json_is_400_not_500(self):
        """Разбор такого тела упирается в предел рекурсии: это «не JSON»
        для хука, а не падение обработчика."""
        r = self.hook(content=b"[" * 5000 + b"]" * 5000, client=self.quiet_client())
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.threads(), [])

    def test_green_api_message(self):
        r = self.hook(green())
        self.assertEqual(self.counts(r), (1, 0, 0))
        self.assertNotIn("set-cookie", r.headers, "хук сессий не заводит")
        [t] = self.threads()
        self.assertEqual((t["channel"], t["origin"], t["ext_id"], t["phone"], t["name"]),
                         ("wa", "hook", "+79001234567", "+79001234567", "Азиз"))
        self.assertEqual(t["status"], "new")
        self.assertEqual(t["waiting_since"], datetime.fromtimestamp(1790000000, UTC))
        [m] = self.messages(t["id"])
        self.assertEqual((m["direction"], m["kind"], m["ext_id"]),
                         ("in", "text", "BAE5F4886F6F2D05"))
        self.assertEqual(self.text_of(m), "Здравствуйте, велосипед свободен?")
        self.assertIn(t["id"], [a["id"] for a in run(self.crm.inbox_to_announce())],
                      "о новом обращении бот сообщит в служебный чат")
        self.assertEqual(self.bot.sent, [], "панель сама никому не пишет")
        # и владелец видит его в ленте
        self.login()
        self.assertIn("Азиз", self.get_ok("/inbox"))
        page = self.get_ok(f"/inbox/{t['id']}")
        self.assertIn("Здравствуйте, велосипед свободен?", page)
        self.assertIn("(через шлюз)", page)

    def test_green_api_skips_groups_and_own_messages(self):
        group = green()
        group["senderData"]["chatId"] = "120363043968066561@g.us"
        outgoing = green()
        outgoing["typeWebhook"] = "outgoingMessageReceived"
        for payload in (group, outgoing):
            self.assertEqual(self.counts(self.hook(payload)), (0, 0, 1))
        self.assertEqual(self.threads(), [])

    def test_green_api_call_and_image(self):
        call = {"typeWebhook": "incomingCall", "from": "79001234567@c.us",
                "idMessage": "CALL1", "timestamp": 1790000100, "status": "offer"}
        self.assertEqual(self.counts(self.hook(call)), (1, 0, 0))
        image = green(msg_id="IMG1")
        image["messageData"] = {"typeMessage": "imageMessage",
                                "fileMessageData": {"caption": "вот велосипед",
                                                    "downloadUrl": "https://x/y.jpg"}}
        self.assertEqual(self.counts(self.hook(image)), (1, 0, 0))
        [t] = self.threads()
        kinds = [(m["kind"], self.text_of(m)) for m in self.messages(t["id"])]
        self.assertEqual(kinds, [("call", None), ("image", "вот велосипед")])

    def test_wazzup_messages(self):
        payload = {"messages": [
            {"messageId": "wz-1", "chatType": "whatsapp", "chatId": "79005554433",
             "type": "text", "text": "Добрый день", "isEcho": False,
             "dateTime": "2026-09-20T10:00:00.000Z", "contact": {"name": "Рустам"}},
            {"messageId": "wz-2", "chatType": "avito", "chatId": "avito-chat-77",
             "type": "text", "text": "Сдаёте?", "contact": {"name": "Покупатель"}},
            {"messageId": "wz-3", "chatType": "whatsapp", "chatId": "79005554433",
             "type": "text", "text": "наше исходящее", "isEcho": True},
            {"messageId": "wz-4", "chatType": "whatsgroup", "chatId": "grp",
             "type": "text", "text": "группа"},
        ]}
        self.assertEqual(self.counts(self.hook(payload)), (2, 0, 2))
        by_channel = {t["channel"]: t for t in self.threads()}
        self.assertEqual(set(by_channel), {"wa", "avito"})
        self.assertEqual(by_channel["wa"]["ext_id"], "+79005554433")
        self.assertEqual(by_channel["wa"]["name"], "Рустам")
        self.assertEqual(by_channel["avito"]["ext_id"], "avito-chat-77")
        self.assertEqual(by_channel["wa"]["waiting_since"],
                         datetime(2026, 9, 20, 10, 0, tzinfo=UTC))
        texts = [self.text_of(m) for t in self.threads() for m in self.messages(t["id"])]
        self.assertNotIn("наше исходящее", texts)

    def test_same_message_twice_is_one_row(self):
        self.assertEqual(self.counts(self.hook(green())), (1, 0, 0))
        self.assertEqual(self.counts(self.hook(green())), (0, 1, 0))
        [t] = self.threads()
        self.assertEqual(len(self.messages(t["id"])), 1)
        # новое сообщение того же человека - в то же обращение
        self.assertEqual(self.counts(self.hook(green(msg_id="NEXT", text="Алло?"))),
                         (1, 0, 0))
        [t] = self.threads()
        self.assertEqual(len(self.messages(t["id"])), 2)

    def test_telegram_and_max_are_not_accepted(self):
        """С утёкшим токеном бот не должен стать рассыльщиком по чужим tg_id."""
        for channel in ("tg", "max", "TG", "sms", ""):
            with self.subTest(channel=channel):
                r = self.hook({"channel": channel, "ext_id": "5001", "text": "Привет",
                               "name": "Чужой"})
                self.assertEqual(self.counts(r), (0, 0, 1))
        self.assertEqual(self.threads(), [])
        mixed = {"items": [
            {"channel": "tg", "ext_id": "5001", "text": "x"},
            {"channel": "wa", "phone": "8 900 111-22-33", "text": "Сколько стоит?",
             "msg_id": "n8n-1", "name": "Олег"},
            {"channel": "avito", "ext_id": "chat-9", "subject": "Kugoo V3",
             "subject_url": "javascript:alert(1)", "text": "Актуально?"},
            "не объект",
        ]}
        self.assertEqual(self.counts(self.hook(mixed)), (2, 0, 2))
        by_channel = {t["channel"]: t for t in self.threads()}
        self.assertEqual(set(by_channel), {"wa", "avito"})
        self.assertEqual(by_channel["wa"]["ext_id"], "+79001112233")
        self.assertEqual(by_channel["wa"]["phone"], "+79001112233")
        self.assertIsNone(by_channel["avito"]["subject_url"])
        # и через форму Wazzup Telegram тоже не заводится
        wazzup = {"messages": [{"messageId": "wz-tg", "chatType": "telegram",
                                "chatId": "5001", "type": "text", "text": "x"}]}
        self.assertEqual(self.counts(self.hook(wazzup)), (0, 0, 1))
        self.assertEqual({t["channel"] for t in self.threads()}, {"wa", "avito"})

    def test_batch_is_capped(self):
        items = [{"channel": "wa", "ext_id": f"+7900{i:07d}", "text": f"№{i}"}
                 for i in range(logic.HOOK_BATCH_LIMIT + 10)]
        self.assertEqual(self.counts(self.hook({"items": items})),
                         (logic.HOOK_BATCH_LIMIT, 0, 10))
        self.assertEqual(len(self.threads()), logic.HOOK_BATCH_LIMIT)

    def test_every_gateway_message_is_accounted_for(self):
        """Wazzup шлёт пачкой. Ответ 200 шлюз считает доставкой и больше не
        повторяет, поэтому каждое сообщение пачки обязано попасть в счёт:
        сохранено, повтор или пропущено - но не пропасть молча."""
        total = logic.HOOK_BATCH_LIMIT + 10
        payload = {"messages": [
            {"messageId": f"wz-{i}", "chatType": "whatsapp", "chatId": f"7900{i:07d}",
             "type": "text", "text": f"№{i}"} for i in range(total)]}
        saved, duplicates, skipped = self.counts(self.hook(payload))
        self.assertEqual(saved + duplicates + skipped, total,
                         f"сохранено {saved}, повторов {duplicates}, пропущено {skipped}")
        self.assertEqual(len(self.threads()), saved)

    def test_hook_needs_no_login_and_sets_no_cookie(self):
        fresh = tw.TestClient(self.app, follow_redirects=False)
        r = self.hook(green(), client=fresh)
        self.assertEqual(self.counts(r), (1, 0, 0), "не редирект на вход")
        self.assertNotIn("set-cookie", r.headers)
        self.assertEqual(dict(fresh.cookies), {})
        self.assertEqual(r.headers.get("x-frame-options"), "DENY")
        # хук не открывает ничего по соседству
        r = fresh.get("/hook/inbox")
        self.assertIn(r.status_code, (404, 405))
        self.assertEqual(fresh.get("/inbox").status_code, 303)

    def test_existing_client_is_linked_by_phone(self):
        cid = run(self.crm.create_client(full_name="Абдуллаев Азиз", phone="+79001234567"))
        self.counts(self.hook(green()))
        [t] = self.threads()
        self.assertEqual(t["client_id"], cid)
        self.assertEqual(t["client_name"], "Абдуллаев Азиз")

    def test_unlink_by_hand_holds_against_the_next_message(self):
        """Карточка, найденная по телефону, отвязывается кнопкой - и
        следующее сообщение с того же номера её не возвращает. Кнопка
        «Отвязать» есть только у привязанного обращения."""
        cid = run(self.crm.create_client(full_name="Абдуллаев Азиз", phone="+79001234567"))
        self.counts(self.hook(green()))
        [t] = self.threads()
        tid = t["id"]
        self.assertEqual((t["client_id"], t["client_manual"]), (cid, False))
        self.login()
        page = self.get_ok(f"/inbox/{tid}")
        self.assertIn("Отвязать от карточки", page)
        self.assertIn('<input type="hidden" name="client" value="">', page)
        self.assertNotIn("Карточку выбрали вручную", page)
        r = self.client.post(f"/inbox/{tid}/client", data={"client": ""})
        self.assertEqual(r.status_code, 303)
        t = self.thread(tid)
        self.assertEqual((t["client_id"], t["client_manual"]), (None, True))
        page = self.get_ok(f"/inbox/{tid}")
        self.assertIn("Обращение отвязано от карточки.", page)
        self.assertNotIn("Отвязать от карточки", page, "отвязывать нечего")
        self.assertIn(f'action="/inbox/{tid}/client"', page, "привязать можно снова")
        self.assertIn("Карточку выбрали вручную", page)
        self.assertEqual(self.counts(self.hook(green(msg_id="AFTER", text="Алло?"))),
                         (1, 0, 0))
        t = self.thread(tid)
        self.assertEqual((t["client_id"], t["client_manual"]), (None, True),
                         "телефон карточку не вернул")
        self.assertEqual(len(self.messages(tid)), 2)
        self.assertNotIn("Абдуллаев Азиз", self.get_ok(f"/inbox/{tid}"))
        # ручная привязка к другой карточке тоже держится
        other = run(self.crm.create_client(full_name="Сабиров Рустам", phone="+79005550001"))
        self.client.post(f"/inbox/{tid}/client", data={"client": str(other)})
        self.counts(self.hook(green(msg_id="AFTER2", text="Жду")))
        t = self.thread(tid)
        self.assertEqual((t["client_id"], t["client_manual"]), (other, True))
        self.assertIn("Отвязать от карточки", self.get_ok(f"/inbox/{tid}"))

    def test_unlinked_thread_has_no_unlink_button(self):
        self.counts(self.hook(green()))
        [t] = self.threads()
        self.assertIsNone(t["client_id"])
        self.login()
        page = self.get_ok(f"/inbox/{t['id']}")
        self.assertIn(f'action="/inbox/{t["id"]}/client"', page)
        self.assertIn("Привязать к клиенту", page)
        self.assertNotIn("Отвязать от карточки", page)
        self.assertNotIn("Карточку выбрали вручную", page)

    def test_spam_stays_spam(self):
        self.counts(self.hook(green()))
        [t] = self.threads()
        run(self.crm.update_inbox_thread(t["id"], status="spam",
                                         announced_at=datetime.now(UTC)))
        self.assertEqual(self.counts(self.hook(green(msg_id="SPAM2"))), (1, 0, 0))
        t = self.thread(t["id"])
        self.assertEqual(t["status"], "spam")
        self.assertIsNotNone(t["announced_at"], "о спаме в чат второй раз не пишем")
        self.assertEqual(len(self.threads()), 1, "второго обращения не заводится")

    def test_done_thread_reopens_on_new_message(self):
        self.counts(self.hook(green()))
        [t] = self.threads()
        run(self.crm.update_inbox_thread(t["id"], status="done",
                                         announced_at=datetime.now(UTC)))
        self.counts(self.hook(green(msg_id="AGAIN", text="Ещё вопрос")))
        t = self.thread(t["id"])
        self.assertEqual(t["status"], "new")
        self.assertIsNone(t["announced_at"], "вернувшийся в новые снова объявляется")


if __name__ == "__main__":
    unittest.main()
