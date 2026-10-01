"""Дефекты бота из проверки кода: каждый - со своим тестом.

Ретеншен брошенных регистраций (152-ФЗ), гражданство «РФ», учёт в CRM до
сборки документа, потерянные карточки оператору, контакт менеджера в
частых вопросах, повторная анкета после ретеншена, MAX (сброс после
CardNotReady, местная дата, свой шаблон), деньги из бота мимо журнала и
наличные мимо кассы, команда /staff, экранирование и служебные кнопки.
Подпись в docx и раскладка МЧЗ - в test_documents.py и test_mrz.py.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import logic  # noqa: E402

try:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage, SendPhoto
    from aiogram.types import CallbackQuery, Chat, Message, User

    from app import faq, middlewares, texts
    from app.crm import company, doctemplates, sync
    from app.handlers import cabinet, staff
    from app.max import handlers as max_handlers
    from app.services import files
    from app.services.crypto import Vault
    from tests import test_flow as tf
    from tests.fake_crm import FakeCrm
    HAVE_AIOGRAM = tf.HAVE_AIOGRAM
except ImportError:                                    # pragma: no cover
    HAVE_AIOGRAM = False

try:
    import asyncpg
    import pgserver

    from app import tasks
    from app.db import Database, _init_connection
    from app.services import files as pg_files
    REAL_REMOVE = pg_files.remove
    HAVE_PG = True
except ImportError:                                    # pragma: no cover
    HAVE_PG = False

D = Decimal
SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
USER_ID = 5001
ADMIN_CHAT = -1009876543210


# ─────────────── 1. ретеншен брошенных регистраций (Postgres) ───────────────

@unittest.skipUnless(HAVE_PG, "pgserver или asyncpg не установлены")
class TestStaleRegistrations(unittest.IsolatedAsyncioTestCase):
    """purge_after брошенной регистрации не ставит никто: «Подтверждаю»,
    «на проверке», «одобрено, ждём данных», подпись договора и «Есть
    ошибка» его снимают или не трогают. Анкета и скан такого человека
    жили вечно. Правило общее для Telegram и MAX: один bot.users, один
    проход `tasks.purge_once`."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pg = pgserver.get_server(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        # Удаление - настоящее: соседний набор мог оставить заглушку.
        self._remove = pg_files.remove
        pg_files.remove = REAL_REMOVE
        self.pool = await asyncpg.create_pool(self.pg.get_uri(), min_size=1, max_size=3,
                                              init=_init_connection)
        await self.pool.execute("drop schema if exists crm cascade; "
                                "drop schema if exists bot cascade")
        self.db = Database(self.pool)
        await self.db.apply_schema(SCHEMA)
        self.dir = Path(tempfile.mkdtemp())
        self.cfg = SimpleNamespace(storage_dir=self.dir, purge_approved_days=90,
                                   purge_stale_days=30, updates_log_days=7)

    async def asyncTearDown(self):
        pg_files.remove = self._remove
        await self.pool.close()

    async def person(self, tg_id: int, *, state: str, days: int,
                     status: str = logic.ST_NEW, contract: str = logic.CT_NONE,
                     act_in: bool = False, act_out: bool = False) -> Path:
        # Файл - руками, а не files.store: заглушку store оставляют
        # соседние наборы, а здесь нужен настоящий файл на диске.
        scan = self.dir / f"{tg_id}-doc-{int(time.time() * 1000)}.jpg"
        scan.write_bytes(b"scan")
        now = datetime.now(UTC)
        await self.pool.execute(
            "insert into bot.users (tg_id, state, status, contract_status, anketa_enc, "
            "  doc_file_id, doc_path, contract_no, contract_issued_at, "
            "  act_in_signed_at, act_out_signed_at) "
            "values ($1, $2, $3, $4, 'v1:анкета', 'file-id', $5, 'АВ-1', $6, $7, $8)",
            tg_id, state, status, contract, str(scan),
            now if contract != logic.CT_NONE else None,
            now if act_in else None, now if act_out else None)
        await self.pool.execute(
            "update bot.users set updated_at = now() - make_interval(days => $2) "
            "where tg_id = $1", tg_id, days)
        return scan

    async def row(self, tg_id: int) -> dict:
        return dict(await self.pool.fetchrow("select * from bot.users where tg_id = $1",
                                             tg_id))

    async def test_abandoned_unfinished_steps_are_purged_and_restarted(self):
        cases = [
            (1, logic.CONFIRM, logic.ST_NEW, logic.CT_NONE),
            (2, logic.PENDING, logic.ST_PENDING, logic.CT_NONE),
            # одобрено, данные выдачи так и не пришли
            (3, logic.PENDING, logic.ST_APPROVED, logic.CT_NONE),
            (4, logic.WAIT_SIGN, logic.ST_APPROVED, logic.CT_ISSUED),
            # «Есть ошибка»: анкета стёрта, а скан и договор - нет
            (5, logic.WAIT_FIO, logic.ST_NEW, logic.CT_NONE),
        ]
        scans = {tg: await self.person(tg, state=state, status=status, contract=ct,
                                       days=31)
                 for tg, state, status, ct in cases}
        await tasks.purge_once(self.db, self.cfg)
        for tg, *_ in cases:
            row = await self.row(tg)
            self.assertIsNone(row["anketa_enc"], tg)
            self.assertIsNone(row["doc_file_id"], tg)
            self.assertIsNone(row["doc_path"], tg)
            self.assertFalse(scans[tg].exists(), f"скан {tg} остался на диске")
            self.assertEqual((row["state"], row["status"], row["contract_status"]),
                             (logic.NEW, logic.ST_NEW, logic.CT_NONE), tg)
            self.assertIsNone(row["contract_issued_at"],
                              "новый договор не должен выйти датой брошенного")
            self.assertEqual(row["contract_no"], "АВ-1", "номер живёт, как в clear_files")
        events = await self.pool.fetch(
            "select tg_id from bot.events where type = 'stale_registration_purged'")
        self.assertEqual(sorted(r["tg_id"] for r in events), [1, 2, 3, 4, 5])

    async def test_fresh_and_finished_rows_are_kept(self):
        fresh = await self.person(10, state=logic.CONFIRM, days=10)
        # подписал и в меню: его срок - purge_after, не это правило
        signed = await self.person(11, state=logic.APPROVED, status=logic.ST_APPROVED,
                                   contract=logic.CT_SIGNED, days=400)
        # идущая аренда не трогается никогда, в каком бы шаге ни была строка
        renting = await self.person(12, state=logic.PENDING, status=logic.ST_PENDING,
                                    contract=logic.CT_SIGNED, act_in=True, days=400)
        await tasks.purge_once(self.db, self.cfg)
        for tg, scan in ((10, fresh), (11, signed), (12, renting)):
            row = await self.row(tg)
            self.assertIsNotNone(row["anketa_enc"], tg)
            self.assertTrue(scan.exists(), tg)

    async def test_signed_client_refilling_anketa_waits_the_approved_term(self):
        """Повторная анкета после ретеншена: договор подписан, и его
        документы живут срок подписавшего, а не тридцать дней."""
        kept = await self.person(20, state=logic.WAIT_BIRTH, contract=logic.CT_SIGNED,
                                 act_in=True, act_out=True, days=40)
        gone = await self.person(21, state=logic.WAIT_BIRTH, contract=logic.CT_SIGNED,
                                 act_in=True, act_out=True, days=95)
        await tasks.purge_once(self.db, self.cfg)
        self.assertTrue(kept.exists())
        self.assertFalse(gone.exists())
        row = await self.row(21)
        self.assertIsNone(row["anketa_enc"])
        self.assertEqual((row["state"], row["status"], row["contract_status"]),
                         (logic.APPROVED, logic.ST_APPROVED, logic.CT_SIGNED),
                         "подписавший возвращается в меню, договор при нём")
        self.assertIsNotNone(row["contract_issued_at"])

    async def test_row_touched_after_selection_is_not_cleared(self):
        await self.person(30, state=logic.CONFIRM, days=31)
        seen = (await self.row(30))["updated_at"]
        await self.db.patch(30, state=logic.PENDING)       # человек вернулся
        self.assertFalse(await self.db.clear_stale_registration(30, seen))
        self.assertIsNotNone((await self.row(30))["anketa_enc"])


class TestUnfinishedStates(unittest.TestCase):
    def test_every_step_before_signing_and_nothing_after(self):
        for state in (logic.CONFIRM, logic.PENDING, logic.WAIT_SIGN, logic.WAIT_DOC2,
                      logic.WAIT_FIO, logic.WAIT_BIRTH, logic.WAIT_PARENT_CONSENT):
            self.assertIn(state, logic.UNFINISHED_STATES)
        for state in (logic.APPROVED, logic.WAIT_PAYMENT, logic.WAIT_ACT_SIGN,
                      logic.WAIT_RETURN_SIGN, logic.WAIT_BUYOUT_SIGN, logic.WAIT_SUPPORT,
                      logic.WAIT_CLOSE_REASON):
            self.assertNotIn(state, logic.UNFINISHED_STATES)

    def test_max_bot_reads_the_same_term(self):
        from app import max_main
        env = {"MAX_BOT_TOKEN": "t", "MAX_CHANNEL_ID": "-1", "MAX_ADMIN_CHAT_ID": "-2",
               "MAX_ADMINS": "1", "POSTGRES_PASSWORD": "pw", "PDN_KEY": "k" * 44,
               "PURGE_STALE_DAYS": "45"}
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            cfg = max_main.load_config()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(cfg.purge_stale_days, 45)


# ─────────────────────────── 3. гражданство ───────────────────────────

class TestCitizenshipSynonyms(unittest.TestCase):
    def test_russia_in_any_spelling_is_russia(self):
        """Подсказка на английском и узбекском прямо советует «Russia» и
        «Rossiya», а в MAX кнопок нет вовсе: «РФ» уводила россиянина в
        ветку иностранца - срок действия вместо кода подразделения."""
        for raw in ("РФ", "рф", "Р.Ф.", "Российская Федерация", "российская федерация",
                    "Russia", "RUSSIA", "Russian Federation", "Rossiya", "rossiya",
                    "россия", "  Россия  "):
            result = logic.validate_citizenship(raw)
            self.assertTrue(result.ok, raw)
            self.assertEqual(result.value, logic.RUSSIA, raw)
            self.assertFalse(logic.is_foreign({"citizenship": result.value}), raw)

    def test_other_countries_are_untouched(self):
        self.assertEqual(logic.validate_citizenship("узбекистан").value, "Узбекистан")
        self.assertEqual(logic.validate_citizenship("Беларусь").value, "Беларусь")
        self.assertTrue(logic.is_foreign(
            {"citizenship": logic.validate_citizenship("Казахстан").value}))


# ─────────────────── 13. экранирование (чистая логика) ───────────────────

class TestFixationFormEscapesKit(unittest.TestCase):
    def test_kit_from_the_operator_form_is_escaped(self):
        form = logic.fixation_form({"full_name": "Иванов"}, {},
                                   {"kit_mirrors": "<2> & ещё"})
        self.assertIn("&lt;2&gt; &amp; ещё", form)
        self.assertNotIn("<2>", form)


# ─────────────────────── 15. наличные из бота ───────────────────────

class TestPayMethodFromText(unittest.TestCase):
    def test_cash_words(self):
        from app.crm import logic as crm_logic
        for text in ("3000 нал", "3500 наличными", "Наличка 3000", "НАЛ 2000"):
            self.assertEqual(crm_logic.pay_method_from_text(text), "cash", text)
        for text in ("3000 qr", "3000 безнал", "перевод", "", None):
            self.assertEqual(crm_logic.pay_method_from_text(text), "sbp", text)


def run(coro):
    import asyncio
    return asyncio.run(coro)


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestSyncMoney(unittest.IsolatedAsyncioTestCase):
    """«3000 нал» в форме выдачи - это наличные: в журнал со способом cash
    и в смену принявшего, иначе ящик расходится с журналом на каждую
    такую выдачу. Платёж, не легший в журнал, - причина для оператора."""

    async def asyncSetUp(self):
        self.crm = FakeCrm()
        self.client_id = await self.crm.create_client(
            full_name="Иванов Иван", phone="+79990000000", tg_id=USER_ID, source="bot")
        self.shift_id = await self.crm.create_shift(location=None, opening=D(0),
                                                    note=None, by="tg:111")

    def user(self, price: str, **over) -> dict:
        return {"tg_id": USER_ID, "phone": "+79990000000", "full_name": "Иванов Иван",
                "contract_no": "АВ-1", "issue_data": {"rent_price": price}, **over}

    async def payments(self):
        return [x for x in await self.crm.ledger_of(self.client_id) if x["kind"] == "payment"]

    async def test_cash_payment_lands_in_the_shift(self):
        self.assertIsNone(await sync.on_payment_confirmed(
            self.crm, self.user("3000 нал"), by="tg:111"))
        pay = (await self.payments())[0]
        self.assertEqual((pay["method"], pay["shift_id"], pay["amount"]),
                         ("cash", self.shift_id, D(3000)))

    async def test_transfer_stays_out_of_the_drawer(self):
        await sync.on_payment_confirmed(self.crm, self.user("3000 qr"), by="tg:111")
        pay = (await self.payments())[0]
        self.assertEqual((pay["method"], pay["shift_id"]), ("sbp", None))

    async def test_cash_extension_lands_in_the_shift(self):
        started = date.today() - timedelta(days=7)
        rental_id = await self.crm.create_rental(
            client_id=self.client_id, bike_id=None, tariff_id=None, tariff_name="Неделя",
            period_days=7, price=D(3000), billing="manual", started_on=started,
            contract_no="АВ-1", created_by="bot")
        rental = await self.crm.rental(rental_id)
        until = rental["billed_until"] + timedelta(days=7)
        self.assertIsNone(await sync.on_rental_extended(
            self.crm, self.user("2500 наличными"), until=until, by="tg:111"))
        pay = (await self.payments())[0]
        self.assertEqual((pay["method"], pay["shift_id"], pay["amount"]),
                         ("cash", self.shift_id, D(2500)))

    async def test_phone_of_another_telegram_is_reported(self):
        await self.crm.update_client(self.client_id, tg_id=999)
        problem = await sync.on_payment_confirmed(self.crm, self.user("3000 qr"),
                                                  by="tg:111")
        self.assertEqual(problem, sync.NO_CLIENT)
        self.assertEqual(await self.payments(), [])

    async def test_amount_in_words_is_reported(self):
        problem = await sync.on_payment_confirmed(self.crm, self.user("три тысячи"),
                                                  by="tg:111")
        self.assertIn("не распознана", problem)


# ─────────────── сценарии через настоящий Dispatcher ───────────────

def flaky(session, needle: str):
    """Сообщения в чат договоров с этим текстом не доходят - как при
    сбое Telegram. Возвращает выключатель."""
    original = session.make_request
    state = {"on": True}

    async def make_request(bot, method, timeout=None):
        if (state["on"] and isinstance(method, SendMessage)
                and method.chat_id == ADMIN_CHAT and needle in (method.text or "")):
            raise TelegramBadRequest(method=method, message="chat not found")
        return await original(bot, method, timeout)

    session.make_request = make_request
    return state


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class FlowCase(unittest.IsolatedAsyncioTestCase):
    def make_crm(self):
        return None

    async def asyncSetUp(self):
        self.crm = self.make_crm()
        (self.dp, self.bot, self.db, self.session, self.cfg,
         self.vault) = tf.build(tf.make_config(), crm=self.crm)
        self._orig = (files.download, files.store, files.remove)
        files.download = lambda bot, file_id, max_bytes: tf._async(b"bytes")
        files.store = lambda d, tg, slot, data: (
            Path(f"/tmp/{tg}-{slot}.{files.SLOT_EXT[slot]}"), "hash")
        files.remove = lambda path: True

    async def asyncTearDown(self):
        files.download, files.store, files.remove = self._orig
        await self.bot.session.close()

    # помощники TestFlow - те же функции, без повторного прогона его тестов
    feed = tf.TestFlow.feed
    fill_anketa = tf.TestFlow.fill_anketa
    register_up_to_confirm = tf.TestFlow.register_up_to_confirm
    submit = tf.TestFlow.submit
    approve = tf.TestFlow.approve
    provide_issue = tf.TestFlow.provide_issue
    approve_fully = tf.TestFlow.approve_fully
    confirm_pay = tf.TestFlow.confirm_pay
    register_fully = tf.TestFlow.register_fully
    request_close = tf.TestFlow.request_close
    provide_return = tf.TestFlow.provide_return
    close_rental = tf.TestFlow.close_rental
    ISSUE_FORM = tf.TestFlow.ISSUE_FORM
    CLOSE_FORM = tf.TestFlow.CLOSE_FORM
    REPEAT_FORM = tf.TestFlow.REPEAT_FORM

    def to_admin(self) -> list[str]:
        return [tf.plain(m.text) for m in self.session.sent_to(ADMIN_CHAT)
                if isinstance(m, SendMessage)]

    def to_user(self) -> list[str]:
        return [tf.plain(m.text) for m in self.session.sent_to(USER_ID)
                if isinstance(m, SendMessage)]

    def pay_buttons(self) -> list:
        """Сообщения в чат договоров с кнопкой «Оплата получена»."""
        return [m for m in self.session.sent_to(ADMIN_CHAT)
                if m.reply_markup is not None
                and any(b.callback_data == f"pay:{USER_ID}"
                        for row in m.reply_markup.inline_keyboard for b in row)]


class TestLostCards(FlowCase):
    """Карточка оператору уходит один раз, и сбой Telegram в этот момент
    оставлял клиента в ожидании навсегда. Следующее сообщение клиента
    ставит пропавшую карточку заново."""

    async def test_moderation_card_is_resent_on_the_next_message(self):
        await self.register_up_to_confirm()
        self.session.fail_photo_to = ADMIN_CHAT
        await self.feed(tf.cb("confirm"))
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.PENDING)
        self.assertIsNone(row.get("mod_message_id"), "карточка не ушла")
        self.session.fail_photo_to = None
        await self.feed(tf.msg("ну что там?"))
        row = self.db.users[USER_ID]
        self.assertIsNotNone(row.get("mod_message_id"), "карточка не переотправлена")
        cards = [m for m in self.session.sent_to(ADMIN_CHAT)
                 if isinstance(m, SendPhoto) and m.reply_markup]
        self.assertEqual(len(cards), 1)
        # дальше путь обычный: одобрение по новой карточке работает
        await self.approve()
        self.assertEqual(self.db.users[USER_ID]["status"], logic.ST_APPROVED)

    async def test_sent_card_is_not_duplicated(self):
        await self.submit()
        await self.feed(tf.msg("ну что там?"))
        await self.feed(tf.msg("/start"))
        cards = [m for m in self.session.sent_to(ADMIN_CHAT)
                 if isinstance(m, SendPhoto) and m.reply_markup]
        self.assertEqual(len(cards), 1)

    async def test_issue_prompt_is_resent_on_the_next_message(self):
        await self.submit()
        switch = flaky(self.session, "данными выдачи")
        await self.approve()
        row = self.db.users[USER_ID]
        self.assertEqual((row["state"], row["status"]), (logic.PENDING, logic.ST_APPROVED))
        self.assertIsNone(row.get("issue_message_id"))
        switch["on"] = False
        await self.feed(tf.msg("когда договор?"))
        self.assertIsNotNone(self.db.users[USER_ID].get("issue_message_id"))
        await self.provide_issue()
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_SIGN)

    async def test_pay_card_is_resent_and_nudges_carry_the_button(self):
        await self.submit()
        await self.approve_fully()
        switch = flaky(self.session, "Ожидание оплаты")
        await self.feed(tf.cb("sign"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PAYMENT)
        self.assertIsNone(self.db.users[USER_ID].get("pay_message_id"))
        # «Я оплатил» и чек приходят оператору уже с кнопкой подтверждения
        await self.feed(tf.cb("paid"))
        await self.feed(tf.msg(photo=True, file_id="receipt"))
        self.assertEqual(len(self.pay_buttons()), 2, "сигнал и чек без кнопки")
        switch["on"] = False
        await self.feed(tf.msg("оплатил"))
        self.assertIsNotNone(self.db.users[USER_ID].get("pay_message_id"),
                             "карточка оплаты не переотправлена")
        await self.confirm_pay()
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_ACT_SIGN)

    async def test_new_price_without_a_card_puts_a_new_card(self):
        await self.submit()
        await self.approve_fully()
        switch = flaky(self.session, "Ожидание оплаты")
        await self.feed(tf.cb("sign"))
        switch["on"] = False
        await self.provide_issue(self.ISSUE_FORM.replace("3000 qr", "3500 qr"))
        self.assertIsNotNone(self.db.users[USER_ID].get("pay_message_id"))
        self.assertTrue(any("3500 qr" in t for t in self.to_admin()
                            if "Ожидание оплаты" in t))


class TestFaqContact(FlowCase):
    async def test_guest_handoff_uses_the_configured_contact(self):
        company.set_snapshot({"support_contact": "https://t.me/novy_menedzher"})
        self.addCleanup(company.reset)
        intent = next(i for i in faq.MENU_TOPICS if i.handoff and not i.red)
        await self.feed(tf.msg("/start"))
        await self.feed(tf.cb("lang:en"))
        await self.feed(tf.cb(f"faq:{intent.code}"))
        last = self.to_user()[-1]
        self.assertIn("https://t.me/novy_menedzher", last)
        self.assertNotIn(texts.SUPPORT_CONTACT_URL, last)


class TestRepeatAfterRetention(FlowCase):
    """Повторная аренда, а анкету стёр ретеншен: акт приёма вышел бы с
    прочерками вместо паспорта. Клиент проходит анкету и проверку заново."""

    def purge(self):
        """То же, что db.clear_files через срок хранения после закрытия."""
        self.db.users[USER_ID].update(anketa_enc=None, doc_file_id=None, doc_path=None,
                                      doc2_file_id=None, contract_path=None)

    async def test_rent_button_leads_through_the_anketa_and_moderation(self):
        await self.register_fully()
        await self.close_rental()
        self.purge()
        await self.feed(tf.msg("🚲 Арендовать"))
        row = self.db.users[USER_ID]
        first = logic.next_state(logic.WAIT_CONTACT)
        self.assertEqual((row["state"], row["status"]), (first, logic.ST_NEW))
        self.assertEqual(row["contract_status"], logic.CT_SIGNED, "договор прежний")
        self.assertFalse([t for t in self.to_admin() if "повторную аренду" in t],
                         "заявка с пустой анкетой не должна уходить оператору")
        self.assertIn("анкету ещё раз", self.to_user()[-2])

        await self.fill_anketa()
        await self.feed(tf.msg(photo=True, file_id="new-scan"))
        await self.feed(tf.cb("doc_enough"))
        await self.feed(tf.cb("confirm"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.PENDING)
        await self.approve()
        await self.provide_issue(self.REPEAT_FORM)
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PAYMENT)
        await self.confirm_pay()
        act = [m for m in self.session.documents()
               if m.chat_id == USER_ID and "Акт приёма" in (m.caption or "")][-1]
        text = tf.docx_text(act.document.data)
        self.assertIn("1234 567890", text, "паспорт в акте повторной аренды")

    async def test_operator_form_on_a_purged_anketa_is_not_applied(self):
        await self.register_fully()
        await self.close_rental()
        self.purge()
        before = dict(self.db.users[USER_ID]["issue_data"])
        await self.provide_issue(self.REPEAT_FORM)
        row = self.db.users[USER_ID]
        self.assertEqual(row["issue_data"], before, "форма не записана")
        self.assertEqual(row["state"], logic.next_state(logic.WAIT_CONTACT))
        self.assertTrue(any("стёрта по сроку хранения" in t for t in self.to_admin()))


class TestStaffCommandGate(FlowCase):
    async def test_glued_staff_text_is_neither_a_command_nor_a_name(self):
        await self.feed(tf.msg("/start"))
        await self.feed(tf.cb("lang:ru"))
        self.session.subscribed = False
        await self.feed(tf.msg("/staffИван Петров"))
        row = self.db.users[USER_ID]
        self.assertIsNone(row["full_name"], "гейт подписки пропустил «/staff…»")
        self.assertEqual(row["state"], logic.WAIT_FIO)

    def test_command_shapes(self):
        def message(text):
            return Message(message_id=1, date=datetime.now(UTC),
                           chat=Chat(id=1, type="private"),
                           from_user=User(id=1, is_bot=False, first_name="u"), text=text)
        for text in ("/staff AB3D9K2M", "/staff", "/STAFF ab3d9k2m",
                     "/staff@mybike_bot AB3D9K2M"):
            self.assertTrue(middlewares._is_staff_link(message(text)), text)
        for text in ("/staffИван Петров", "/staffer x", "staff AB3D9K2M"):
            self.assertFalse(middlewares._is_staff_link(message(text)), text)


class TestServiceButtonsSkipTheGate(FlowCase):
    def test_waitlist_cancel_and_inbox_answer_are_service(self):
        def callback(data):
            return CallbackQuery(id="1", chat_instance="c", data=data,
                                 from_user=User(id=1, is_bot=False, first_name="u"))
        for data in ("cab:book:cancel", "inbox_answer", "wl:1:2", "fb:1:5"):
            self.assertTrue(middlewares._is_service_callback(callback(data)), data)
        self.assertFalse(middlewares._is_service_callback(callback("cab:book")))

    async def test_unsubscribed_client_can_answer_the_panel(self):
        self.db.users[USER_ID] = {
            "tg_id": USER_ID, "username": "ivan", "state": logic.APPROVED,
            "status": logic.ST_APPROVED, "rl_count": 0, "full_name": "Иванов Иван",
            "phone": "+79990000000", "lang": "ru", "anketa_enc": None}
        self.session.subscribed = False
        await self.feed(tf.cb("inbox_answer"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_SUPPORT)


class CrmFlowCase(FlowCase):
    def make_crm(self):
        return FakeCrm()

    async def client(self):
        return await self.crm.client_by_tg(USER_ID)


class TestCrmBeforeDocuments(CrmFlowCase):
    """Учёт идёт сразу за подписью: сбой сборки акта больше не оставляет
    сданный велосипед в аренде с начислениями, а выданный - без аренды."""

    def break_acts(self):
        def broken(*args, **kwargs):
            raise tf.contract.ContractProblem("шаблон испорчен")
        original = tf.contract._build_act
        tf.contract._build_act = broken
        self.addCleanup(setattr, tf.contract, "_build_act", original)

    async def test_rental_starts_and_return_is_invited_without_the_act(self):
        await self.submit()
        await self.approve_fully()
        await self.feed(tf.cb("sign"))
        await self.confirm_pay()
        self.break_acts()
        await self.feed(tf.cb("act_sign"))
        client = await self.client()
        self.assertIsNotNone(await self.crm.active_rental_of(client["id"]),
                             "аренда в CRM не заведена")
        self.assertIsNotNone(self.db.users[USER_ID].get("return_message_id"),
                             "без приглашения возврата аренду из бота не закрыть")
        self.assertTrue(any("акт приёма не пересобрался" in t for t in self.to_admin()))

    async def test_return_closes_the_rental_without_the_act(self):
        await self.register_fully()
        await self.request_close()
        await self.provide_return()
        self.break_acts()
        await self.feed(tf.cb("return_sign"))
        client = await self.client()
        self.assertIsNone(await self.crm.active_rental_of(client["id"]),
                          "сданный велосипед остался в аренде")
        self.assertTrue(await self.db.rentals_of(USER_ID), "история аренды записана")


class TestMoneyMissingFromCrm(CrmFlowCase):
    async def test_operator_is_told_when_payment_misses_the_ledger(self):
        """Телефон клиента в CRM записан за карточкой с другим Telegram:
        платёж раньше пропадал строкой в журнале бота."""
        await self.crm.create_client(full_name="Чужой", phone="+79990000000", tg_id=999)
        await self.submit()
        await self.approve_fully()
        await self.feed(tf.cb("sign"))
        await self.confirm_pay()
        alerts = [t for t in self.to_admin() if "не записано в CRM" in t]
        self.assertTrue(alerts, "оператор не узнал о потерянном платеже")
        self.assertIn("Оплата по договору", alerts[0])
        self.assertNotIn("+7999", alerts[0], "телефон в чат не идёт")


# ─────────────────────────── 13. экранирование ───────────────────────────

class RecordingBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **_):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent), chat=SimpleNamespace(id=chat_id))


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestEscaping(unittest.IsolatedAsyncioTestCase):
    async def test_estimate_answer_escapes_the_client_name(self):
        crm, bot = FakeCrm(), RecordingBot()
        cid = await crm.create_client(full_name="Иван <b>& Co</b>", phone="+79990000001")
        await cabinet._tell_estimate_answer(
            bot, crm, SimpleNamespace(contract_chat_id=ADMIN_CHAT),
            {"no": "НР-<1>", "estimate": D(1500)}, await crm.client(cid), agree=True)
        text = bot.sent[-1][1]
        self.assertIn("Иван &lt;b&gt;&amp; Co&lt;/b&gt;", text)
        self.assertIn("НР-&lt;1&gt;", text)

    async def test_staff_link_escapes_the_name_from_the_panel(self):
        crm = FakeCrm()
        staff_id = await crm.create_staff("petr", "hash", "Пётр <script>", "manager")
        await crm.set_staff_link_code(staff_id, "AB3D9K2M")
        answers = []

        async def answer(text, **_):
            answers.append(text)
        message = SimpleNamespace(answer=answer,
                                  from_user=SimpleNamespace(id=9100, username="petr"))
        await staff.cmd_staff(message, command=SimpleNamespace(args="AB3D9K2M"), crm=crm)
        self.assertIn("Пётр &lt;script&gt;", answers[-1])


# ─────────────────────────────── 9-10. MAX ───────────────────────────────

class FakeMax:
    def __init__(self):
        self.sent = []

    async def send(self, *, user_id=None, chat_id=None, text, keyboard=None,
                   attachments=None, fmt="html", reply_to_mid=None):
        self.sent.append({"to": user_id or chat_id, "text": text})
        return {"message": {"body": {"mid": f"mid.{len(self.sent)}"}}}

    async def answer_callback(self, cid, notification=None):
        self.sent.append({"to": "cb", "text": notification})

    async def upload_file(self, name, data):
        return {"type": "file", "payload": {"token": "f"}}


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestMaxFixes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.cfg = tf.make_config(doc_dir=self.folder)
        self.db = tf.FakeDB()
        self.cl = FakeMax()
        self.vault = Vault.from_raw(self.cfg.pdn_key)
        self.ctx = max_handlers.Ctx(self.cl, self.db, self.cfg, self.vault, crm=None)
        self.addCleanup(doctemplates.reset)
        self.addCleanup(company.reset)

    async def test_card_not_ready_returns_to_the_first_step(self):
        """Анкету стёр ретеншен, пока человек думал: pending без карточки -
        тупик. Как в Telegram - на первый шаг, а не «отправлено» навсегда."""
        self.db.users[42] = {"tg_id": 42, "state": logic.CONFIRM, "status": logic.ST_NEW,
                             "anketa_enc": None, "doc_file_id": None,
                             "full_name": "Иванов Иван", "phone": "+79990000000"}
        await max_handlers.cb_confirm(self.ctx, dict(self.db.users[42]), "cb-1")
        row = self.db.users[42]
        self.assertEqual((row["state"], row["status"]), (logic.WAIT_FIO, logic.ST_NEW))
        self.assertEqual(self.cl.sent[-1]["text"], texts.WELCOME)

    async def test_contract_date_is_the_local_day(self):
        saved = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Moscow"
        time.tzset()
        try:
            ctx = max_handlers._contract_ctx(
                self.ctx, {"tg_id": 42, "full_name": "Иванов Иван",
                           "phone": "+79990000000"}, {}, number="АВМ-1",
                signed_at="не подписан",
                issued_at=datetime(2026, 9, 30, 22, 30, tzinfo=UTC))   # 01:30 МСК
        finally:
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            time.tzset()
        self.assertEqual(ctx["contract_date"], "01.10.2026")

    async def test_owner_template_and_signature_are_used(self):
        own = self.folder / "contract-0001.docx"
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types></Types>")
            zf.writestr("word/_rels/document.xml.rels", "<Relationships></Relationships>")
            zf.writestr("word/document.xml",
                        "<w:document><w:body><w:p><w:r><w:t>Свой договор {{ fio }} "
                        "{{ signature }}</w:t></w:r></w:p></w:body></w:document>")
        own.write_bytes(out.getvalue())
        doctemplates.set_snapshot([{"kind": "contract", "active": True,
                                    "filename": own.name}], {"signature": PNG})
        docx, _ = await max_handlers._build_docx(
            self.ctx, {"tg_id": 42, "full_name": "Иванов Иван"}, {}, number="АВМ-1",
            signed_at="не подписан", issued_at=None)
        with zipfile.ZipFile(io.BytesIO(docx)) as zf:
            xml = zf.read("word/document.xml").decode()
        self.assertIn("Свой договор", xml)
        self.assertIn('r:embed="rIdMarksignature"', xml)


if __name__ == "__main__":
    unittest.main()
