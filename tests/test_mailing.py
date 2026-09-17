"""Рассылки: шаблоны, аудитории, отправка в Telegram и MAX.

Рассылка - это не «написать всем»: курьеру с велосипедом на руках
предложение «возвращайтесь» выглядит издевательством. Здесь проверяется,
что аудитория отбирает кого надо, подстановки подставляются, а отправка
не шлёт одному человеку дважды.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_web as tw

    from app.crm import mailing
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal
TODAY = date(2026, 9, 17)


def client(**over) -> dict:
    row = {"id": 1, "full_name": "Ахмедов Бехруз Шухратович", "phone": "+79990000001",
           "tg_id": 5001, "max_id": None, "status": "active", "balance": D(0),
           "last_rental_on": None, "contract_no": "АВ-2026-000042"}
    row.update(over)
    return row


def rental(**over) -> dict:
    row = {"id": 10, "client_id": 1, "status": "active", "bike_code": "B-101",
           "tariff_name": "Неделя", "price": D(3000), "period_days": 7,
           "billed_until": TODAY + timedelta(days=1), "balance": D(0),
           "started_on": TODAY - timedelta(days=6)}
    row.update(over)
    return row


class TestTemplateLogic(unittest.TestCase):
    def test_unknown_placeholder_is_refused_on_save(self):
        self.assertTrue(logic.check_template_body("Привет, {name}!").ok)
        bad = logic.check_template_body("Привет, {имя}! {nmae}")
        self.assertFalse(bad.ok)
        self.assertIn("nmae", bad.error)
        self.assertFalse(logic.check_template_body("   ").ok)
        self.assertFalse(logic.check_template_body("x" * 3001).ok)

    def test_context_and_render(self):
        values = logic.template_context(client(), rental(), D(-4500), today=TODAY,
                                        pay_url="https://pay.example")
        self.assertEqual(values["name"], "Бехруз", "обращение по имени, не по фамилии")
        self.assertEqual(values["bike"], "№ B-101")
        self.assertEqual(values["debt"], logic.money(D(4500)))
        self.assertEqual(values["contract"], "АВ-2026-000042")
        text = logic.render_template(
            "Здравствуйте, {name}! Долг {debt} по {bike}. Оплата: {pay_url}", values)
        self.assertIn("Бехруз", text)
        self.assertIn("№ B-101", text)
        self.assertIn("https://pay.example", text)
        # без аренды подстановки не пустые, а прочерки
        empty = logic.template_context(client(), None, D(0), today=TODAY)
        self.assertEqual(empty["bike"], "—")
        self.assertEqual(empty["until"], "—")

    def test_render_keeps_unknown_field_as_is(self):
        self.assertEqual(logic.render_template("{nope} {name}", {"name": "Азиз"}),
                         "{nope} Азиз")

    def test_plain_text_for_max(self):
        self.assertEqual(logic.plain_text("<b>Долг</b> 3&nbsp;000 ₽<br>сегодня"),
                         "Долг 3 000 ₽\nсегодня")


class TestAudienceLogic(unittest.TestCase):
    def people(self):
        return [client(),
                client(id=2, full_name="Петров Пётр", phone="+79990000002",
                       tg_id=None, max_id=777, balance=D(-3000)),
                client(id=3, full_name="Сидоров Сидор", phone="+79990000003",
                       tg_id=5003, last_rental_on=TODAY - timedelta(days=30)),
                client(id=4, full_name="Блок Блокович", phone="+79990000004",
                       tg_id=5004, status="blacklist", balance=D(-9000)),
                client(id=5, full_name="Немой Никто", phone="+79990000005",
                       tg_id=None, balance=D(-500))]

    def test_renting_and_debtors(self):
        renting = logic.pick_audience("renting", self.people(), [rental()],
                                      today=TODAY)
        self.assertEqual([p["id"] for p in renting], [1])
        debtors = logic.pick_audience("debtors", self.people(), [rental()],
                                      today=TODAY)
        self.assertEqual([p["id"] for p in debtors], [2],
                         "чёрный список не пишем, без мессенджера - некуда")
        self.assertEqual(debtors[0]["channel"], "max")

    def test_expiring_uses_the_same_window_as_reminders(self):
        soon = rental(billed_until=TODAY, balance=D(0))
        got = logic.pick_audience("expiring", self.people(), [soon], today=TODAY)
        self.assertEqual([p["id"] for p in got], [1])
        far = rental(billed_until=TODAY + timedelta(days=30), balance=D(12000))
        self.assertEqual(logic.pick_audience("expiring", self.people(), [far],
                                             today=TODAY), [])

    def test_comeback_has_both_edges(self):
        got = logic.pick_audience("comeback", self.people(), [], today=TODAY)
        self.assertEqual([p["id"] for p in got], [3], "30 дней назад - в самый раз")
        fresh = [client(id=7, full_name="Свежий Клиент", phone="+79990000007",
                        tg_id=5007, last_rental_on=TODAY - timedelta(days=3))]
        self.assertEqual(logic.pick_audience("comeback", fresh, [], today=TODAY), [],
                         "три дня назад - человек просто в отпуске")
        old = [client(id=8, full_name="Старый Клиент", phone="+79990000008",
                      tg_id=5008, last_rental_on=TODAY - timedelta(days=200))]
        self.assertEqual(logic.pick_audience("comeback", old, [], today=TODAY), [],
                         "полгода назад - он уже не курьер")

    def test_all_needs_a_messenger(self):
        got = logic.pick_audience("all", self.people(), [], today=TODAY)
        self.assertEqual([p["id"] for p in got], [1, 2, 3],
                         "по алфавиту, без чёрного списка и без «некуда писать»")

    def test_channel_prefers_telegram(self):
        self.assertEqual(logic.send_channel({"tg_id": 1, "max_id": 2}), "tg")
        self.assertEqual(logic.send_channel({"max_id": 2}), "max")
        self.assertIsNone(logic.send_channel({}))

    def test_progress(self):
        got = logic.campaign_progress([
            {"status": "sent", "channel": "tg"}, {"status": "sent", "channel": "max"},
            {"status": "failed", "channel": "tg"}, {"status": "queued", "channel": "tg"}])
        self.assertEqual(got["total"], 4)
        self.assertEqual(got["sent"], 2)
        self.assertEqual(got["max"], 1)
        self.assertEqual(got["percent"], 75)
        self.assertEqual(logic.campaign_progress([])["percent"], 0)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestMailingPanel(tw.WebCase):
    class MaxClient:
        def __init__(self, fail: bool = False):
            self.sent: list[tuple[int, str]] = []
            self.fail = fail

        async def send(self, *, user_id=None, chat_id=None, text=""):
            if self.fail:
                raise RuntimeError("MAX недоступен")
            self.sent.append((user_id, text))

    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.template_id = tw.run(self.crm.create_template(
            code="debt", title="Напоминание о долге",
            body="Здравствуйте, {name}! Долг {debt} по {bike}.",
            body_max=None, note=None))
        # должник с Telegram и должник с MAX
        tw.run(self.crm.add_ledger(client_id=self.client_id, kind="charge",
                                   amount=D(-3000)))
        self.max_client_id = tw.run(self.crm.create_client(
            full_name="Петров Пётр", phone="+79990000002"))
        tw.run(self.crm.link_client_max("+79990000002", 777))
        tw.run(self.crm.add_ledger(client_id=self.max_client_id, kind="charge",
                                   amount=D(-1500)))

    def make(self, audience="debtors"):
        r = self.client.post("/mailing", data={
            "title": "Долги, сентябрь", "template_id": str(self.template_id),
            "audience": audience, "note": ""})
        return r

    def test_draft_collects_the_audience_and_previews_the_text(self):
        r = self.make()
        self.assertEqual(r.status_code, 303)
        campaign_id = int(r.headers["location"].rsplit("/", 1)[1])
        sends = tw.run(self.crm.campaign_sends(campaign_id))
        self.assertEqual({s["channel"] for s in sends}, {"tg", "max"})
        self.assertEqual(len(sends), 2)
        card = self.get_ok(f"/mailing/{campaign_id}")
        self.assertIn("Иван", card, "в предпросмотре имя первого получателя")
        self.assertIn("Черновик", card)
        self.assertEqual(tw.run(self.crm.campaign(campaign_id))["status"], "draft")

    def test_empty_audience_is_refused(self):
        tw.run(self.crm.update_client(self.client_id, status="blacklist"))
        tw.run(self.crm.update_client(self.max_client_id, status="blocked"))
        r = self.make()
        self.assertEqual(r.headers["location"], "/mailing")
        self.assertIn("никого", self.get_ok("/mailing"))
        self.assertEqual(tw.run(self.crm.campaigns()), [])

    def test_sending_goes_to_both_messengers_once(self):
        campaign_id = int(self.make().headers["location"].rsplit("/", 1)[1])
        self.client.post(f"/mailing/{campaign_id}/start")
        self.assertEqual(tw.run(self.crm.campaign(campaign_id))["status"], "sending")
        max_client = self.MaxClient()
        counts = tw.run(mailing.run_campaign(
            self.bot, self.crm, tw.run(self.crm.campaign(campaign_id)),
            max_client=max_client, pay_url="https://pay.example", pause=0))
        self.assertEqual(counts["sent"], 2)
        self.assertEqual(len(self.bot.sent), 1)
        self.assertIn("Иван", self.bot.sent[0][1])
        self.assertIn("3 000", self.bot.sent[0][1])
        self.assertEqual(max_client.sent[0][0], 777)
        self.assertIn("Пётр", max_client.sent[0][1])
        # второй проход ничего не шлёт и закрывает кампанию
        again = tw.run(mailing.run_campaign(
            self.bot, self.crm, tw.run(self.crm.campaign(campaign_id)),
            max_client=max_client, pause=0))
        self.assertTrue(again["done"])
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(tw.run(self.crm.campaign(campaign_id))["status"], "done")

    def test_max_without_a_bot_is_skipped_not_lost(self):
        campaign_id = int(self.make().headers["location"].rsplit("/", 1)[1])
        self.client.post(f"/mailing/{campaign_id}/start")
        tw.run(mailing.run_campaign(self.bot, self.crm,
                                    tw.run(self.crm.campaign(campaign_id)),
                                    max_client=None, pause=0))
        sends = {s["channel"]: s for s in tw.run(self.crm.campaign_sends(campaign_id))}
        self.assertEqual(sends["max"]["status"], "skipped")
        self.assertIn("MAX-бот", sends["max"]["error"])
        self.assertEqual(sends["tg"]["status"], "sent")

    def test_failed_send_is_recorded_with_the_reason(self):
        campaign_id = int(self.make().headers["location"].rsplit("/", 1)[1])
        self.client.post(f"/mailing/{campaign_id}/start")
        counts = tw.run(mailing.run_campaign(
            self.bot, self.crm, tw.run(self.crm.campaign(campaign_id)),
            max_client=self.MaxClient(fail=True), pause=0))
        self.assertEqual(counts["failed"], 1)
        sends = {s["channel"]: s for s in tw.run(self.crm.campaign_sends(campaign_id))}
        self.assertEqual(sends["max"]["status"], "failed")
        self.assertIn("MAX недоступен", sends["max"]["error"])

    def test_blocked_client_is_skipped_at_send_time(self):
        campaign_id = int(self.make().headers["location"].rsplit("/", 1)[1])
        self.client.post(f"/mailing/{campaign_id}/start")
        tw.run(self.crm.update_client(self.client_id, status="blacklist"))
        tw.run(mailing.run_campaign(self.bot, self.crm,
                                    tw.run(self.crm.campaign(campaign_id)),
                                    max_client=self.MaxClient(), pause=0))
        sends = {s["channel"]: s for s in tw.run(self.crm.campaign_sends(campaign_id))}
        self.assertEqual(sends["tg"]["status"], "skipped")
        self.assertEqual(self.bot.sent, [])

    def test_cancel_clears_the_queue_only(self):
        campaign_id = int(self.make().headers["location"].rsplit("/", 1)[1])
        self.client.post(f"/mailing/{campaign_id}/start")
        r = self.client.post(f"/mailing/{campaign_id}/cancel")
        self.assertEqual(r.headers["location"], f"/mailing/{campaign_id}")
        self.assertEqual(tw.run(self.crm.campaign(campaign_id))["status"], "cancelled")
        self.assertTrue(all(s["status"] == "skipped"
                            for s in tw.run(self.crm.campaign_sends(campaign_id))))
        self.assertIn("не отзывается", self.get_ok(f"/mailing/{campaign_id}"))

    def test_template_form_refuses_a_typo_in_a_placeholder(self):
        r = self.client.post("/mailing/templates", data={
            "code": "promo", "title": "Скидка", "body": "Привет, {nmae}!",
            "body_max": "", "note": ""})
        self.assertEqual(r.headers["location"], "/mailing")
        self.assertIn("неизвестная подстановка", self.get_ok("/mailing"))
        self.assertEqual(len(tw.run(self.crm.templates())), 1)

    def test_template_is_created_and_edited(self):
        self.client.post("/mailing/templates", data={
            "code": "promo", "title": "Скидка", "body": "Привет, {name}!",
            "body_max": "Привет, {name}!", "note": "осенняя"})
        codes = {t["code"] for t in tw.run(self.crm.templates())}
        self.assertIn("promo", codes)
        promo = next(t for t in tw.run(self.crm.templates()) if t["code"] == "promo")
        self.client.post("/mailing/templates", data={
            "id": str(promo["id"]), "title": "Скидка 20%",
            "body": "Здравствуйте, {name}!", "body_max": "", "note": ""})
        self.assertEqual(tw.run(self.crm.template(promo["id"]))["title"], "Скидка 20%")
        # повторный код - отказ
        self.client.post("/mailing/templates", data={
            "code": "promo", "title": "Ещё раз", "body": "{name}"})
        self.assertIn("уже есть", self.get_ok("/mailing"))
        # код - не инвентарный номер: кириллица и пробелы не проходят
        self.client.post("/mailing/templates", data={
            "code": "промо осень", "title": "Осень", "body": "{name}"})
        self.assertIn("Код шаблона", self.get_ok("/mailing"))

    def test_missing_campaign_is_a_404(self):
        self.assertEqual(self.client.get("/mailing/999").status_code, 404)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestMaxBridge(tw.WebCase):
    """Мост MAX -> карточка клиента: связь по телефону."""

    def setUp(self):
        super().setUp()
        self.seed()

    def test_link_by_phone_and_conflicts(self):
        self.assertEqual(tw.run(self.crm.link_client_max("+79990000000", 777)),
                         self.client_id)
        self.assertEqual(tw.run(self.crm.client(self.client_id))["max_id"], 777)
        # повтор того же - не ошибка
        self.assertEqual(tw.run(self.crm.link_client_max("+79990000000", 777)),
                         self.client_id)
        other = tw.run(self.crm.create_client(full_name="Петров Пётр",
                                              phone="+79990000002"))
        self.assertIsNone(tw.run(self.crm.link_client_max("+79990000002", 777)),
                          "чужой MAX-аккаунт не перевешивается")
        self.assertIsNone(tw.run(self.crm.client(other))["max_id"])
        self.assertIsNone(tw.run(self.crm.link_client_max("+79991112233", 888)),
                          "карточки с таким телефоном нет")


if __name__ == "__main__":
    unittest.main()
