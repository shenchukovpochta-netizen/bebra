"""Кабинет клиента (CRM) сквозь настоящий Dispatcher: привязка, экран
кабинета, пополнение с заявкой оператору, зачисление, чек, история,
договор; синхронизация бот -> CRM по полному циклу аренды; дневной
проход биллинга. База CRM - в памяти (tests/fake_crm.py).
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from aiogram import Bot, Dispatcher
    from aiogram.methods import EditMessageText, SendMessage, SendPhoto

    from app import logic, texts
    from app.crm import billing, service
    from app.crm import logic as crm_logic
    from app.handlers import cabinet, contract, menu, moderation, registration
    from app.handlers import faq as faq_handlers
    from app.handlers import staff as staff_h
    from app.middlewares import PipelineMiddleware
    from app.services import files
    from app.services.crypto import Vault
    from tests.fake_crm import FakeCrm
    from tests.test_flow import (
        ADMIN_CHAT,
        ADMIN_ID,
        USER_ID,
        FakeDB,
        FakeSession,
        _async,
        cb,
        make_config,
        msg,
        settle,
    )
    HAVE_AIOGRAM = True
except ImportError:                                    # pragma: no cover
    HAVE_AIOGRAM = False

D = Decimal
OTHER_ID = 7007


def build(cfg=None):
    for module in (cabinet, staff_h, contract, registration, moderation,
                   faq_handlers, menu):
        importlib.reload(module)
    cfg = cfg or make_config()
    db, crm, session = FakeDB(), FakeCrm(), FakeSession()
    bot = Bot("123:abc", session=session)
    vault = Vault.from_raw(cfg.pdn_key)
    dp = Dispatcher()
    dp.update.outer_middleware(PipelineMiddleware(db, cfg, vault, crm))
    dp.include_router(cabinet.router)
    dp.include_router(staff_h.router)
    dp.include_router(moderation.router)
    dp.include_router(contract.router)
    dp.include_router(registration.router)
    dp.include_router(faq_handlers.router)
    dp.include_router(menu.router)
    return dp, bot, db, crm, session, cfg, vault


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class CabinetCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (self.dp, self.bot, self.db, self.crm, self.session,
         self.cfg, self.vault) = build()
        self._orig = (files.download, files.store, files.remove)
        files.download = lambda bot, file_id, max_bytes: _async(b"bytes")
        files.store = lambda d, tg, slot, data: (
            Path(f"/tmp/{tg}-{slot}.{files.SLOT_EXT[slot]}"), "hash")
        files.remove = lambda path: True

    async def asyncTearDown(self):
        files.download, files.store, files.remove = self._orig
        await self.bot.session.close()

    async def feed(self, update):
        await self.dp.feed_update(self.bot, update)
        await settle()

    def texts_to(self, chat_id):
        return [m.text for m in self.session.sent_to(chat_id) if isinstance(m, SendMessage)]

    def last_text(self, chat_id=USER_ID):
        return self.texts_to(chat_id)[-1]

    def approved_user(self, tg_id=USER_ID, phone="+79990000000", **over):
        """Зарегистрированный клиент в меню: минимум полей bot.users."""
        self.db.users[tg_id] = {
            "tg_id": tg_id, "username": "ivan", "state": logic.APPROVED,
            "status": logic.ST_APPROVED, "rl_count": 0, "full_name": "Иванов Иван",
            "phone": phone, "lang": None, "contract_no": "АВ-2026-000001",
            "contract_status": logic.CT_SIGNED, "contract_path": None,
            "issue_data": None, "rent_until": None, "extend_until": None,
            "act_in_signed_at": None, "act_out_signed_at": None, "anketa_enc": None,
            **over,
        }
        return self.db.users[tg_id]

    async def crm_client(self, *, tg_id=None, phone="+79990000000", name="Иванов Иван"):
        cid = await self.crm.create_client(full_name=name, phone=phone, tg_id=tg_id)
        return await self.crm.client(cid)

    async def crm_rental(self, client, *, billed_offset=3, price="3000", period=7,
                         billing="auto", bike=True):
        bike_id = None
        if bike:
            bike_id = await self.crm.create_bike(code="B-7", model="Kugoo V3")
        rid = await self.crm.create_rental(
            client_id=client["id"], bike_id=bike_id, tariff_id=None,
            tariff_name="Неделя", period_days=period, price=D(price), billing=billing,
            started_on=date.today() - timedelta(days=period - billed_offset),
            contract_no=None, created_by="test")
        # начислен один период, оплачен полностью
        await self.crm.update_rental(rid, billed_until=date.today() + timedelta(days=billed_offset))
        await self.crm.add_ledger(client_id=client["id"], rental_id=rid, kind="charge",
                                  amount=D(price) * -1, note="период")
        await self.crm.add_ledger(client_id=client["id"], kind="payment", amount=D(price))
        return await self.crm.rental(rid)


class TestLinking(CabinetCase):
    async def test_registered_user_gets_client_card_automatically(self):
        self.approved_user()
        await self.feed(msg("/cabinet"))
        client = await self.crm.client_by_tg(USER_ID)
        self.assertIsNotNone(client, "карточка клиента должна завестись по телефону")
        self.assertEqual(client["source"], "bot")
        self.assertEqual(client["contract_no"], "АВ-2026-000001")
        text = self.last_text()
        self.assertIn("Мой кабинет", text)
        self.assertIn("Иванов Иван", text)
        self.assertIn(texts.CAB_NO_RENTAL, text)
        self.assertIn("0 ₽", text)

    async def test_registered_user_links_to_manual_card_by_phone(self):
        manual = await self.crm_client(name="Иванов И. (руками)")
        self.approved_user()
        await self.feed(msg("🚲 Мой кабинет"))
        linked = await self.crm.client(manual["id"])
        self.assertEqual(linked["tg_id"], USER_ID)
        self.assertEqual(len(self.crm.clients_), 1, "дубль карточки заводиться не должен")

    async def test_unregistered_user_is_asked_for_contact(self):
        await self.feed(msg("/cabinet"))
        self.assertIn(texts.CAB_LINK_PROMPT, self.texts_to(USER_ID))
        markup = self.session.last_markup()
        self.assertTrue(markup.keyboard[0][0].request_contact)

    async def test_contact_not_in_system(self):
        await self.feed(msg("/cabinet"))
        await self.feed(msg(contact_user_id=USER_ID))
        text = self.last_text()
        self.assertIn("не найден в системе", text)
        self.assertIn(texts.SUPPORT_CONTACT_URL, text)
        self.assertIsNone(await self.crm.client_by_tg(USER_ID))

    async def test_contact_links_manual_card(self):
        await self.crm_client()          # +79990000000 - номер из контакта
        await self.feed(msg("/cabinet"))
        await self.feed(msg(contact_user_id=USER_ID))
        client = await self.crm.client_by_tg(USER_ID)
        self.assertIsNotNone(client)
        joined = " ".join(self.texts_to(USER_ID))
        self.assertIn("Аккаунт привязан", joined)
        self.assertIn("Мой кабинет", joined)

    async def test_foreign_contact_is_refused(self):
        await self.crm_client()
        await self.feed(msg("/cabinet"))
        await self.feed(msg(contact_user_id=OTHER_ID))
        self.assertIsNone(await self.crm.client_by_tg(USER_ID))
        self.assertIn(texts.CONTACT_FOREIGN, self.texts_to(USER_ID))

    async def test_phone_taken_by_another_telegram_is_not_relinked(self):
        await self.crm_client(tg_id=OTHER_ID)
        await self.feed(msg("/cabinet"))
        await self.feed(msg(contact_user_id=USER_ID))
        self.assertIn("не найден в системе", self.last_text())
        client = await self.crm.client_by_phone("+79990000000")
        self.assertEqual(client["tg_id"], OTHER_ID)

    async def test_contact_step_of_registration_is_not_hijacked(self):
        """Контакт на шаге анкеты - ответ регистрации, а не привязка."""
        await self.feed(msg("/start"))
        await self.feed(cb("lang:ru"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("pdn_ok"))
        await self.feed(cb("oferta_ok"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_CONTACT)
        await self.feed(msg(contact_user_id=USER_ID))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_BIRTH)
        self.assertNotIn("не найден в системе", " ".join(self.texts_to(USER_ID)))

    async def test_cabinet_opens_mid_registration(self):
        """/cabinet с любого шага анкеты, шаг при этом не сбивается."""
        await self.feed(msg("/start"))
        await self.feed(cb("lang:ru"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_FIO)
        await self.feed(msg("/cabinet"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_FIO)
        self.assertIn(texts.CAB_LINK_PROMPT, self.texts_to(USER_ID))

    async def test_blocked_client_sees_refusal(self):
        client = await self.crm_client(tg_id=USER_ID)
        await self.crm.update_client(client["id"], status="blacklist")
        self.approved_user()
        await self.feed(msg("/cabinet"))
        self.assertIn("недоступен", self.last_text())

    async def test_menu_button_exits_support_dialog(self):
        self.approved_user(state=logic.WAIT_SUPPORT)
        await self.feed(msg("🚲 Мой кабинет"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.APPROVED)
        self.assertIn("Мой кабинет", self.last_text())

    async def test_without_crm_cabinet_says_unavailable(self):
        # Роутеры уже подключены к Dispatcher из build(): перезагружаем
        # модули, чтобы получить свежие экземпляры для второго диспетчера.
        importlib.reload(cabinet)
        importlib.reload(menu)
        self.dp = Dispatcher()
        self.dp.update.outer_middleware(PipelineMiddleware(self.db, self.cfg, self.vault))
        self.dp.include_router(cabinet.router)
        self.dp.include_router(menu.router)
        self.approved_user()
        await self.feed(msg("/cabinet"))
        self.assertIn(texts.CAB_UNAVAILABLE, self.texts_to(USER_ID))


class TestCabinetScreens(CabinetCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.approved_user()
        self.client = await self.crm_client(tg_id=USER_ID)

    async def test_home_shows_rental_and_paid_until(self):
        await self.crm_rental(self.client, billed_offset=3)
        await self.feed(msg("/cabinet"))
        text = self.last_text()
        until = (date.today() + timedelta(days=3)).strftime("%d.%m.%Y")
        self.assertIn("Kugoo V3 B-7", text)
        self.assertIn("Неделя — 3 000 ₽ / 7 дн.", text)
        self.assertIn(f"Оплачено до: <b>{until}</b> (осталось дней: 3)", text)
        self.assertIn(texts.CAB_HINT_OK, text)

    async def test_home_shows_debt(self):
        await self.crm_rental(self.client, billed_offset=-4)
        await self.crm.add_ledger(client_id=self.client["id"], kind="fine",
                                  amount=D("-500"), note="царапина")
        await self.feed(msg("/cabinet"))
        text = self.last_text()
        self.assertIn("Баланс: <b>−500 ₽</b>", text)
        # долг 500 при неделе за 3000: текущий период не оплачен, «оплачено до»
        # откатывается на его начало - 11 дней назад
        self.assertIn("просрочка 11 дн., долг 500 ₽", text)
        self.assertIn("Пополните баланс на 500 ₽", text)

    async def test_english_client_gets_translated_cabinet(self):
        self.db.users[USER_ID]["lang"] = "en"
        await self.feed(msg("/cabinet"))
        text = self.last_text()
        self.assertIn("My cabinet", text)
        self.assertIn("No active rental", text)

    def labels(self):
        markup = self.session.last_markup()
        return [b.text for row in markup.inline_keyboard for b in row]

    def callbacks(self):
        markup = self.session.last_markup()
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    async def test_pay_screen_offers_periods_ahead(self):
        await self.crm_rental(self.client)
        await self.feed(cb("cab:pay"))
        text = self.last_text()
        self.assertIn("Выберите сумму", text)
        self.assertIn(texts.CAB_HINT_OK, text)
        self.assertEqual(self.labels()[:3], ["1 × 7 дн. — 3 000 ₽", "2 × 7 дн. — 6 000 ₽",
                                             "4 × 7 дн. — 12 000 ₽"])
        self.assertEqual(self.callbacks()[:3], ["cab:pay:p1", "cab:pay:p2", "cab:pay:p4"])

    async def test_pay_screen_puts_the_debt_first(self):
        await self.crm_rental(self.client, billed_offset=-3)
        await self.feed(cb("cab:pay"))
        labels = self.labels()
        self.assertEqual(labels[0], "Долг — 3 000 ₽")
        self.assertNotIn("1 × 7 дн. — 3 000 ₽", labels, "долг равен периоду - не дублируем")
        self.assertIn("2 × 7 дн. — 6 000 ₽", labels)

    async def test_pay_screen_without_rental_or_debt(self):
        await self.feed(cb("cab:pay"))
        self.assertIn("пополнять нечего", self.last_text())

    async def test_amount_without_acquiring_falls_back_to_sbp_and_claim(self):
        await self.crm_rental(self.client)
        await self.feed(cb("cab:pay:p2"))
        text = self.last_text()
        self.assertIn("Рекомендуемая сумма: <b>6 000 ₽</b>", text)
        # ссылка экранирована для HTML: & -> &amp;, как и в PAY_PROMPT
        self.assertIn(logic.esc(self.cfg.pay_url), text)
        self.assertIn("💳 Оплатить", self.labels())
        self.assertIn("cab:paid:p2", self.callbacks())
        self.assertEqual(await self.crm.pay_orders(), [], "без эквайринга счёт не заводится")
        await self.feed(cb("cab:paid:p2"))
        claims = await self.crm.pending_claims()
        self.assertEqual(claims[0]["amount_hint"], D(6000), "заявка помнит выбранную сумму")

    async def test_stale_amount_is_refused(self):
        await self.feed(cb("cab:pay:p1"))
        self.assertEqual(await self.crm.pay_orders(), [])
        self.assertFalse([t for t in self.texts_to(USER_ID) if "Рекомендуемая" in t])

    async def test_amount_with_acquiring_makes_an_invoice_the_bank_confirms(self):
        from test_paying import FakeAcquiring
        acq = FakeAcquiring(link="https://pay.example/9", operation_id="op-9",
                            answers=[{"state": "pending"},
                                     {"state": "paid", "status": "APPROVED", "card": {}}])
        orig = cabinet.acquiring_for
        cabinet.acquiring_for = lambda cfg: acq
        try:
            await self.crm_rental(self.client)
            await self.feed(cb("cab:pay:p1"))
            orders = await self.crm.pay_orders()
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["status"], "sent")
            self.assertEqual(orders[0]["amount"], D(3000))
            self.assertEqual(orders[0]["created_by"], "кабинет")
            text = self.last_text()
            self.assertIn(f"Счёт {orders[0]['no']} на <b>3 000 ₽</b>", text)
            labels = self.labels()
            self.assertIn("💳 Оплатить", labels)
            self.assertIn("🔄 Проверить оплату", labels)
            self.assertNotIn("✅ Я оплатил(а)", labels, "оплату подтверждает банк")
            check = f"cab:paycheck:{orders[0]['id']}"
            self.assertIn(check, self.callbacks())
            # первый ответ банка - ещё ждём: денег нет, сообщения нет
            await self.feed(cb(check))
            self.assertEqual(await self.crm.client_balance(self.client["id"]), D(0))
            # второй - оплачено: платёж в журнале, клиенту и команде сказано
            await self.feed(cb(check))
            self.assertEqual(await self.crm.client_balance(self.client["id"]), D(3000))
            self.assertIn("оплачен: 3 000 ₽", self.last_text())
            self.assertTrue([t for t in self.texts_to(ADMIN_CHAT) if "Оплачен счёт" in t])
            self.assertEqual((await self.crm.pay_order(orders[0]["id"]))["status"], "paid")
            # повторная проверка не удваивает платёж
            await self.feed(cb(check))
            self.assertEqual(await self.crm.client_balance(self.client["id"]), D(3000))
        finally:
            cabinet.acquiring_for = orig

    async def test_owner_switch_off_keeps_the_sbp_path(self):
        from test_paying import FakeAcquiring
        orig = cabinet.acquiring_for
        cabinet.acquiring_for = lambda cfg: FakeAcquiring()
        await self.crm.set_setting("acquiring_enabled", "0", by="t")
        try:
            await self.crm_rental(self.client)
            await self.feed(cb("cab:pay:p1"))
            self.assertEqual(await self.crm.pay_orders(), [])
            self.assertIn("Рекомендуемая сумма", self.last_text())
        finally:
            cabinet.acquiring_for = orig

    async def test_intent_buttons_only_with_a_rental(self):
        await self.feed(msg("/cabinet"))
        self.assertNotIn("✅ Продлю", self.labels())
        rental = await self.crm_rental(self.client, billed_offset=2)
        await self.feed(msg("/cabinet"))
        self.assertIn("✅ Продлю", self.labels())
        self.assertIn("↩️ Сдаю", self.labels())
        await self.feed(cb("cab:intent:renew"))
        fresh = await self.crm.rental(rental["id"])
        self.assertEqual(fresh["intent"], "renew")
        self.assertEqual(fresh["intent_by"], "клиент")
        self.assertEqual(fresh["intent_until"], date.today() + timedelta(days=2))
        self.assertIn(texts.CAB_INTENT_RENEW, self.texts_to(USER_ID))
        self.assertIn("Вы сказали: продлеваете", self.last_text())
        await self.feed(cb("cab:intent:return"))
        fresh = await self.crm.rental(rental["id"])
        self.assertEqual(fresh["intent"], "return")
        until = (date.today() + timedelta(days=2)).strftime("%d.%m.%Y")
        self.assertIn(f"сдаёте {until}", " ".join(self.texts_to(USER_ID)))

    async def test_history_lists_operations(self):
        await self.crm_rental(self.client)
        await self.feed(cb("cab:history"))
        text = self.last_text()
        self.assertIn("История операций", text)
        self.assertIn("+3 000 ₽</b> — платёж", text)
        self.assertIn("−3 000 ₽</b> — начисление", text)

    async def test_history_empty(self):
        await self.feed(cb("cab:history"))
        self.assertIn(texts.CAB_HISTORY_EMPTY, self.last_text())

    async def test_contract_missing_and_present(self):
        await self.feed(cb("cab:contract"))
        self.assertIn(texts.CAB_CONTRACT_NONE, self.last_text())
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as fh:
            fh.write(b"PK")
            path = fh.name
        try:
            self.db.users[USER_ID]["contract_path"] = path
            await self.feed(cb("cab:contract"))
            docs = [m for m in self.session.documents() if m.chat_id == USER_ID]
            self.assertTrue(docs, "договор не отправлен")
            self.assertIn("АВ-2026-000001", docs[-1].caption)
        finally:
            os.unlink(path)


class TestTopUp(CabinetCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.approved_user()
        self.client = await self.crm_client(tg_id=USER_ID)
        await self.crm_rental(self.client)

    def claim_card(self):
        cards = [m for m in self.session.sent_to(ADMIN_CHAT)
                 if isinstance(m, SendMessage) and "Заявка на зачисление" in (m.text or "")]
        self.assertTrue(cards, "карточка заявки не ушла оператору")
        return cards[-1]

    def admin_cb(self, data):
        return self.feed(cb(data, chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                            chat_type="supergroup"))

    async def test_paid_creates_claim_and_card(self):
        await self.feed(cb("cab:paid"))
        claims = await self.crm.pending_claims()
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["amount_hint"], D(3000))
        card = self.claim_card()
        self.assertIn("Иванов Иван", card.text)
        self.assertIn("ожидаемая сумма: <b>3 000 ₽</b>", card.text)
        labels = [b.text for row in card.reply_markup.inline_keyboard for b in row]
        self.assertEqual(labels, ["✅ Зачислить 3 000 ₽", "❌ Отклонить"])
        self.assertIn(texts.CAB_CLAIM_SENT, self.texts_to(USER_ID))
        # карточка привязана: по ней найдётся ответ суммой
        found = await self.crm.claim_by_card(card.chat_id, self.session.calls.index(card) + 1)
        self.assertIsNotNone(found)

    async def test_second_claim_is_refused_while_pending(self):
        await self.feed(cb("cab:paid"))
        await self.feed(cb("cab:paid"))
        self.assertEqual(len(await self.crm.pending_claims()), 1)

    async def test_admin_credits_hint_amount(self):
        await self.feed(cb("cab:paid"))
        claim = (await self.crm.pending_claims())[0]
        await self.admin_cb(f"crmpay:{claim['id']}:ok")
        self.assertEqual(await self.crm.client_balance(self.client["id"]), D(3000))
        done = await self.crm.claim(claim["id"])
        self.assertEqual(done["status"], "confirmed")
        self.assertEqual(done["resolved_by"], f"tg:{ADMIN_ID}")
        # карточка помечена, кнопки сняты
        edits = [m for m in self.session.calls if isinstance(m, EditMessageText)]
        self.assertTrue(edits and "Зачислено 3 000 ₽" in edits[-1].text)
        self.assertIsNone(edits[-1].reply_markup)
        # клиент узнал о зачислении и новой дате
        text = self.last_text()
        self.assertIn("Платёж 3 000 ₽ зачислен. Баланс: 3 000 ₽", text)
        until = (date.today() + timedelta(days=10)).strftime("%d.%m.%Y")
        self.assertIn(f"Оплачено до: {until}", text)

    async def test_non_admin_cannot_credit(self):
        await self.feed(cb("cab:paid"))
        claim = (await self.crm.pending_claims())[0]
        await self.feed(cb(f"crmpay:{claim['id']}:ok", chat_id=ADMIN_CHAT,
                           user_id=OTHER_ID, chat_type="supergroup"))
        self.assertEqual(await self.crm.client_balance(self.client["id"]), D(0))
        self.assertEqual((await self.crm.claim(claim["id"]))["status"], "pending")

    async def test_admin_replies_with_other_amount(self):
        await self.feed(cb("cab:paid"))
        card = self.claim_card()
        card_id = self.session.calls.index(card) + 1
        await self.feed(msg("2 500", chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                            chat_type="supergroup", reply_to=card_id))
        self.assertEqual(await self.crm.client_balance(self.client["id"]), D(2500))
        replies = self.texts_to(ADMIN_CHAT)
        self.assertIn("Зачислено 2 500 ₽. Баланс клиента: 2 500 ₽.", replies)

    async def test_bad_amount_reply_is_explained(self):
        await self.feed(cb("cab:paid"))
        card = self.claim_card()
        card_id = self.session.calls.index(card) + 1
        await self.feed(msg("две с половиной", chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                            chat_type="supergroup", reply_to=card_id))
        self.assertIn(texts.CAB_CLAIM_AMOUNT_BAD, self.texts_to(ADMIN_CHAT))
        self.assertEqual(await self.crm.client_balance(self.client["id"]), D(0))

    async def test_double_credit_is_impossible(self):
        await self.feed(cb("cab:paid"))
        claim = (await self.crm.pending_claims())[0]
        await self.admin_cb(f"crmpay:{claim['id']}:ok")
        await self.admin_cb(f"crmpay:{claim['id']}:ok")
        self.assertEqual(await self.crm.client_balance(self.client["id"]), D(3000))

    async def test_reject_notifies_client(self):
        await self.feed(cb("cab:paid"))
        claim = (await self.crm.pending_claims())[0]
        await self.admin_cb(f"crmpay:{claim['id']}:no")
        self.assertEqual((await self.crm.claim(claim["id"]))["status"], "rejected")
        self.assertEqual(await self.crm.client_balance(self.client["id"]), D(0))
        self.assertIn("не нашёл ваш платёж", self.last_text())

    async def test_receipt_is_forwarded_to_card(self):
        await self.feed(cb("cab:paid"))
        card = self.claim_card()
        card_id = self.session.calls.index(card) + 1
        await self.feed(msg(photo=True, file_id="receipt-1"))
        photos = [m for m in self.session.calls
                  if isinstance(m, SendPhoto) and m.chat_id == ADMIN_CHAT]
        self.assertTrue(photos, "чек не переслан оператору")
        self.assertEqual(photos[-1].photo, "receipt-1")
        self.assertEqual(photos[-1].reply_to_message_id, card_id)
        self.assertIn("Чек к заявке", photos[-1].caption)
        claim = (await self.crm.pending_claims())[0]
        self.assertEqual(claim["receipt_file_id"], "receipt-1")
        self.assertIn(texts.CAB_RECEIPT_ATTACHED, self.texts_to(USER_ID))

    async def test_photo_without_claim_falls_to_menu(self):
        await self.feed(msg(photo=True))
        self.assertIn(texts.MENU_PROMPT, self.last_text())
        self.assertFalse([m for m in self.session.calls if isinstance(m, SendPhoto)])

    async def test_other_service_replies_still_reach_moderation(self):
        """Реплай не на карточку заявки уходит в модерацию, как раньше."""
        await self.feed(msg("привет", chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                            chat_type="supergroup", reply_to=999))
        self.assertIn(texts.MOD_REPLY_NOT_A_CARD, self.texts_to(ADMIN_CHAT))


class TestBotSync(CabinetCase):
    """Полный цикл бота отражается в CRM без участия оператора."""

    ISSUE_FORM = ("рама: 264022410703084\n"
                  "мотор: 240W25021406\n"
                  "модель: Truck+\n"
                  "срок: 03.08 - 10.08\n"
                  "оплата: 3000 qr")
    CLOSE_FORM = ("когда: 07.08\nадрес: адоратского\nпринял: ирик\n"
                  "отзыв: оставил\nрекомендации: все ок")

    async def register_up_to_sign(self):
        await self.feed(msg("/start"))
        await self.feed(cb("lang:ru"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("pdn_ok"))
        await self.feed(cb("oferta_ok"))
        await self.feed(msg(contact_user_id=USER_ID))
        from tests.test_flow import ANKETA_ANSWERS
        for answer in ANKETA_ANSWERS:
            await self.feed(msg(answer))
        await self.feed(msg(photo=True))
        await self.feed(msg(photo=True, file_id="f2"))
        await self.feed(cb("confirm"))
        await self.feed(cb(f"approve:{USER_ID}", chat_id=ADMIN_CHAT,
                           user_id=ADMIN_ID, chat_type="supergroup"))
        prompt_id = self.db.users[USER_ID]["issue_message_id"]
        await self.feed(msg(self.ISSUE_FORM, chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                            chat_type="supergroup", reply_to=prompt_id))

    def confirm_pay(self):
        return self.feed(cb(f"pay:{USER_ID}", chat_id=ADMIN_CHAT,
                            user_id=ADMIN_ID, chat_type="supergroup"))

    async def test_full_cycle_lands_in_crm(self):
        await self.register_up_to_sign()
        self.assertIsNone(await self.crm.client_by_tg(USER_ID))

        await self.feed(cb("sign"))
        client = await self.crm.client_by_tg(USER_ID)
        self.assertIsNotNone(client, "подпись договора должна завести клиента")
        self.assertEqual(client["full_name"], "Иванов Иван Иванович")
        self.assertEqual(client["phone"], "+79990000000")
        self.assertEqual(client["contract_no"], self.db.users[USER_ID]["contract_no"])
        self.assertEqual(client["source"], "bot")

        await self.confirm_pay()
        self.assertEqual(await self.crm.client_balance(client["id"]), D(3000),
                         "«Оплата получена» - платёж в журнале")

        await self.feed(cb("act_sign"))
        rental = await self.crm.active_rental_of(client["id"])
        self.assertIsNotNone(rental, "акт приёма должен открыть аренду")
        self.assertEqual(rental["billing"], "manual")
        self.assertEqual(rental["price"], D(3000))
        self.assertEqual(rental["period_days"], 7)
        self.assertEqual(rental["bike_model"], "Truck+")
        bike = await self.crm.bike_by_frame("264022410703084")
        self.assertIsNotNone(bike, "велосипед заводится из формы выдачи")
        self.assertEqual(bike["status"], "rented")
        self.assertEqual(await self.crm.client_balance(client["id"]), D(0),
                         "первый период начислен и покрыт платежом")
        summary = crm_logic.rental_summary(rental, D(0), today=date(2026, 8, 5))
        self.assertEqual(summary["covered_until"], date(2026, 8, 10))

        # закрытие: запрос клиента, форма оператора, подпись акта возврата
        await self.feed(msg(texts.BTN_CLOSE_RENT))
        await self.feed(msg("выхожу на основную работу"))
        prompt_id = self.db.users[USER_ID]["return_message_id"]
        await self.feed(msg(self.CLOSE_FORM, chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                            chat_type="supergroup", reply_to=prompt_id))
        await self.feed(cb("return_sign"))
        self.assertIsNone(await self.crm.active_rental_of(client["id"]))
        bike = await self.crm.bike_by_frame("264022410703084")
        self.assertEqual(bike["status"], "available")

    async def test_extension_adds_payment_and_period(self):
        await self.register_up_to_sign()
        await self.feed(cb("sign"))
        await self.confirm_pay()
        await self.feed(cb("act_sign"))
        client = await self.crm.client_by_tg(USER_ID)
        # заявка на продление и ответ оператора формой, затем оплата
        await self.feed(cb("extend"))
        prompt_id = self.db.users[USER_ID]["extend_message_id"]
        self.assertIsNotNone(prompt_id)
        until = date.today() + timedelta(days=14)
        await self.feed(msg(f"до: {until.strftime('%d.%m.%Y')}\nоплата: 3500 qr",
                            chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                            chat_type="supergroup", reply_to=prompt_id))
        await self.confirm_pay()
        self.assertEqual(await self.crm.client_balance(client["id"]), D(0),
                         "платёж за продление и начисление за новый срок")
        rental = await self.crm.active_rental_of(client["id"])
        self.assertEqual(rental["billed_until"], until)
        charges = [x for x in self.crm.ledger_ if x["kind"] == "charge"]
        self.assertEqual(len(charges), 2)
        self.assertEqual(charges[-1]["amount"], D(-3500))

    async def test_crm_failure_does_not_break_the_bot(self):
        async def boom(*a, **k):
            raise RuntimeError("crm down")
        self.crm.client_by_tg = boom
        await self.register_up_to_sign()
        await self.feed(cb("sign"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PAYMENT)
        await self.confirm_pay()
        await self.feed(cb("act_sign"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.APPROVED)


class TestBilling(CabinetCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.approved_user()
        self.client = await self.crm_client(tg_id=USER_ID)

    async def test_daily_pass_charges_missed_periods(self):
        rental = await self.crm_rental(self.client, billed_offset=-8)
        today = date.today()
        await billing.run_daily(self.bot, self.db, self.crm, self.cfg, today=today)
        charges = [x for x in self.crm.ledger_ if x["kind"] == "charge"]
        self.assertEqual(len(charges), 3, "два пропущенных периода догоняются")
        fresh = await self.crm.rental(rental["id"])
        self.assertEqual(fresh["billed_until"], today - timedelta(days=8) + timedelta(days=14))
        self.assertEqual(await self.crm.client_balance(self.client["id"]), D(-6000))
        # повторный проход ничего не добавляет
        await billing.run_daily(self.bot, self.db, self.crm, self.cfg, today=today)
        self.assertEqual(len([x for x in self.crm.ledger_ if x["kind"] == "charge"]), 3)

    async def test_manual_rental_is_not_charged_by_calendar(self):
        await self.crm_rental(self.client, billed_offset=-8, billing="manual")
        await billing.run_daily(self.bot, self.db, self.crm, self.cfg, today=date.today())
        self.assertEqual(len([x for x in self.crm.ledger_ if x["kind"] == "charge"]), 1)

    async def test_reminder_soon_and_digest(self):
        await self.crm_rental(self.client, billed_offset=self.cfg.remind_before_days)
        today = date.today()
        await billing.run_daily(self.bot, self.db, self.crm, self.cfg, today=today)
        text = self.last_text()
        self.assertIn("Оплаченный период аренды <b>Kugoo V3 B-7</b>", text)
        self.assertIn("Пополните баланс на 3 000 ₽", text)
        labels = [b.text for row in self.session.last_markup().inline_keyboard for b in row]
        self.assertEqual(labels, ["💳 Пополнить баланс"])
        digest = [t for t in self.texts_to(ADMIN_CHAT) if "Сводка по оплатам" in t]
        self.assertTrue(digest)
        self.assertIn("Иванов Иван · B-7 — платёж", digest[0])
        # второй проход в тот же день молчит
        before = len(self.session.calls)
        await billing.run_daily(self.bot, self.db, self.crm, self.cfg, today=today)
        client_msgs = [m for m in self.session.calls[before:]
                       if isinstance(m, SendMessage) and m.chat_id == USER_ID]
        self.assertFalse(client_msgs)

    async def test_overdue_reminder(self):
        await self.crm_rental(self.client, billed_offset=-1, billing="manual")
        await billing.run_daily(self.bot, self.db, self.crm, self.cfg, today=date.today())
        text = self.last_text()
        self.assertIn("не оплачена с", text)
        self.assertIn("Долг: 3 000 ₽", text)

    async def test_open_rental_service_charges_first_period(self):
        bike_id = await self.crm.create_bike(code="B-1", model="Truck+")
        tariff_id = await self.crm.create_tariff("Месяц", 30, D(11000), None)
        rid = await service.open_rental(
            self.crm, client=self.client, bike=await self.crm.bike(bike_id),
            tariff=await self.crm.tariff(tariff_id), started_on=date.today(),
            contract_no=None, by="test")
        rental = await self.crm.rental(rid)
        self.assertEqual(rental["billed_until"], date.today() + timedelta(days=30))
        self.assertEqual(await self.crm.client_balance(self.client["id"]), D(-11000))
        self.assertEqual((await self.crm.bike(bike_id))["status"], "rented")
        with self.assertRaises(service.ServiceError):
            await service.open_rental(
                self.crm, client=self.client, bike=None,
                tariff=await self.crm.tariff(tariff_id), started_on=date.today(),
                contract_no=None, by="test")


if __name__ == "__main__":
    unittest.main()


class TestBlacklistOnCard(CabinetCase):
    """Телефон заявителя в чёрном списке CRM - отметка на карточке модерации."""

    async def submit(self):
        from tests.test_flow import ANKETA_ANSWERS
        await self.feed(msg("/start"))
        await self.feed(cb("lang:ru"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("pdn_ok"))
        await self.feed(cb("oferta_ok"))
        await self.feed(msg(contact_user_id=USER_ID))
        for answer in ANKETA_ANSWERS:
            await self.feed(msg(answer))
        await self.feed(msg(photo=True))
        await self.feed(msg(photo=True, file_id="f2"))
        await self.feed(cb("confirm"))

    def card_caption(self):
        cards = [m for m in self.session.calls
                 if isinstance(m, SendPhoto) and m.chat_id == ADMIN_CHAT
                 and "Договор на утверждение" in (m.caption or "")]
        self.assertTrue(cards, "карточка модерации не ушла")
        return cards[-1].caption

    async def test_blacklisted_phone_is_flagged(self):
        client = await self.crm_client(tg_id=OTHER_ID)     # старый аккаунт, тот же номер
        await self.crm.update_client(client["id"], status="blacklist", note="не вернул АКБ")
        await self.submit()
        caption = self.card_caption()
        self.assertIn("⛔ <b>В CRM: Чёрный список.</b> не вернул АКБ", caption)

    async def test_clean_phone_has_no_flag(self):
        await self.crm_client()
        await self.submit()
        self.assertNotIn("⛔", self.card_caption())
