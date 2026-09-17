"""Центр уведомлений: тумблер, час, история отправок.

Главное, что здесь проверяется: выключенное уведомление не уходит, а
включённое уходит один раз и ровно в свой час. И что выключение не
копит долг — снова включив «истекает сегодня», владелец не должен
получить залп за все пропущенные дни.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_web as tw

    from app.crm import billing, notices
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class TestNoticeLogic(unittest.TestCase):
    def test_catalogue_is_the_source_of_truth(self):
        state = logic.notice_settings([])
        self.assertEqual(set(state), set(logic.NOTICES))
        self.assertTrue(all(n["enabled"] for n in state.values()),
                        "по умолчанию всё включено")
        self.assertIsNone(state["pay_credited"]["at_hour"],
                          "событийное уведомление часа не имеет")
        self.assertEqual(state["daily_digest"]["at_hour"], 20)

    def test_stored_row_overlays_the_catalogue(self):
        state = logic.notice_settings([
            {"code": "rent_due", "enabled": False, "at_hour": 11,
             "at_minute": 30, "chat_id": "-100500", "extra": {}}])
        row = state["rent_due"]
        self.assertFalse(row["enabled"])
        self.assertEqual(logic.notice_time(row), "11:30")
        self.assertEqual(row["chat_id"], "-100500")

    def test_unknown_codes_and_keys_are_ignored(self):
        state = logic.notice_settings([
            {"code": "самодельное", "enabled": True},
            {"code": "review_ask", "enabled": True, "extra": {"after_days": 45,
                                                              "чужое": 1}}])
        self.assertNotIn("самодельное", state,
                         "уведомление без кода в коде отправлять нечем")
        self.assertEqual(state["review_ask"]["extra"], {"after_days": 45})

    def test_param_falls_back_to_the_catalogue(self):
        state = logic.notice_settings([
            {"code": "review_ask", "enabled": True, "extra": {"after_days": "нет"}}])
        self.assertEqual(logic.notice_param(state["review_ask"], "after_days", 21),
                         21, "мусор в базе не должен ломать правило")

    def test_due_waits_for_the_hour_and_fires_once(self):
        state = logic.notice_settings([])
        digest = state["daily_digest"]
        self.assertFalse(logic.notice_due(digest, datetime(2026, 9, 17, 19, 59)))
        self.assertTrue(logic.notice_due(digest, datetime(2026, 9, 17, 20, 0)))
        self.assertTrue(logic.notice_due(digest, datetime(2026, 9, 17, 23, 0)),
                        "перезапуск среди дня не должен съедать сутки")
        self.assertFalse(logic.notice_due(digest, datetime(2026, 9, 17, 23, 0),
                                          done_on=date(2026, 9, 17)))
        self.assertFalse(logic.notice_due(state["pay_credited"],
                                          datetime(2026, 9, 17, 23, 0)),
                         "«сразу» расписанием не ловится")
        off = logic.notice_settings([{"code": "daily_digest", "enabled": False,
                                      "at_hour": 20}])
        self.assertFalse(logic.notice_due(off["daily_digest"],
                                          datetime(2026, 9, 17, 21, 0)))

    def test_rows_are_grouped_and_counted(self):
        rows = logic.notice_rows(logic.notice_settings([]), {"rent_due": 12})
        self.assertEqual(set(rows), set(logic.NOTICE_GROUPS))
        client = {r["code"]: r for r in rows["client"]}
        self.assertEqual(client["rent_due"]["sent"], 12)
        self.assertEqual(client["pay_credited"]["time"], "сразу")
        self.assertEqual(client["rent_due"]["target_title"], "Клиенту")

    def test_review_links_need_a_real_url(self):
        links = logic.review_links({"review_yandex": "https://ya.ru/x",
                                    "review_2gis": "  ",
                                    "review_avito": "не ссылка"})
        self.assertEqual([x["key"] for x in links], ["review_yandex"],
                         "кнопка в никуда хуже, чем её отсутствие")


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestNoticeGate(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def off(self, code):
        _run(self.crm.set_notice(code, enabled=False, at_hour=None, by="тест"))

    def test_disabled_notice_is_not_sent_but_is_recorded(self):
        self.off("pay_credited")
        sent = _run(notices.send_client(self.crm, "pay_credited", self.client_id,
                                        lambda: _ok()))
        self.assertFalse(sent)
        row = _run(self.crm.notice_log())[0]
        self.assertEqual(row["status"], "skipped")
        self.assertIn("выключено", row["detail"])

    def test_enabled_notice_is_sent_and_recorded(self):
        sent = _run(notices.send_client(self.crm, "pay_credited", self.client_id,
                                        lambda: _ok()))
        self.assertTrue(sent)
        row = _run(self.crm.notice_log())[0]
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["target"], "client")

    def test_failure_is_recorded_and_does_not_raise(self):
        sent = _run(notices.send_client(self.crm, "pay_credited", self.client_id,
                                        lambda: _boom()))
        self.assertFalse(sent)
        row = _run(self.crm.notice_log())[0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("телеграм лёг", row["detail"])

    def test_unknown_code_is_allowed(self):
        self.assertTrue(_run(notices.allowed(self.crm, "нет-такого")),
                        "выключить то, чего нет в каталоге, владелец не мог")

    def test_log_is_purged_by_age(self):
        _run(self.crm.log_notice("rent_due", target="client", status="sent"))
        self.crm.notice_log_[0]["created_at"] = datetime.now(UTC) - timedelta(days=40)
        self.assertEqual(_run(self.crm.purge_notice_log(30)), 1)
        self.assertEqual(_run(self.crm.notice_log()), [])


async def _ok():
    return True


async def _boom():
    raise RuntimeError("телеграм лёг")


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestReminderToggles(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.rental_id = _run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id,
            tariff_id=self.tariff_id, tariff_name="Неделя", period_days=7,
            price=D(3000), billing="weekly", started_on=date(2026, 9, 1),
            contract_no="АВ-1", created_by="тест"))
        _run(self.crm.add_ledger(client_id=self.client_id,
                                 rental_id=self.rental_id, kind="charge",
                                 amount=D(-3000), period_from=date(2026, 9, 1),
                                 period_to=date(2026, 9, 8)))
        _run(self.crm.add_ledger(client_id=self.client_id,
                                 rental_id=self.rental_id, kind="payment",
                                 amount=D(3000)))

    def kind_today(self, today):
        """Какое из трёх напоминаний сегодня положено этой аренде."""
        rental = [r for r in _run(self.crm.active_rentals())
                  if r["id"] == self.rental_id][0]
        return billing.REMIND_CODE[logic.reminder_due(
            rental, before_days=self.cfg.remind_before_days, today=today)]

    def test_disabled_reminder_is_marked_so_it_does_not_pile_up(self):
        today = date(2026, 9, 8)
        code = self.kind_today(today)
        _run(self.crm.set_notice(code, enabled=False, at_hour=9, by="тест"))
        sent, _ = _run(billing.remind_once(self.bot, self.db, self.crm, self.cfg,
                                           today=today))
        self.assertEqual(sent, 0)
        self.assertEqual(self.bot.sent, [])
        rental = _run(self.crm.rental(self.rental_id))
        self.assertEqual(rental["notified_on"], date(2026, 9, 8),
                         "иначе при обратном включении прилетит залп за месяц")
        row = _run(self.crm.notice_log())[0]
        self.assertEqual((row["code"], row["status"]), (code, "skipped"))

    def test_enabled_reminder_goes_out_and_is_logged(self):
        today = date(2026, 9, 8)
        code = self.kind_today(today)
        sent, _ = _run(billing.remind_once(self.bot, self.db, self.crm, self.cfg,
                                           today=today))
        self.assertEqual(sent, 1)
        self.assertEqual(len(self.bot.sent), 1)
        row = _run(self.crm.notice_log())[0]
        self.assertEqual((row["code"], row["status"]), (code, "sent"))


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestNoticePages(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_page_lists_every_group(self):
        text = self.get_ok("/notices")
        for title in logic.NOTICE_GROUPS.values():
            self.assertIn(title, text)
        self.assertIn("Аренда истекает сегодня", text)
        self.assertIn("История отправок", text)

    def test_toggle_and_hour_are_saved(self):
        r = self.client.post("/notices/daily_digest",
                             data={"at_hour": "21", "at_minute": "15"})
        self.assertEqual(r.status_code, 303)
        state = logic.notice_settings(_run(self.crm.notices()))
        self.assertFalse(state["daily_digest"]["enabled"],
                         "галочки нет в форме — значит выключено")
        self.assertEqual(logic.notice_time(state["daily_digest"]), "21:15")

    def test_instant_notice_keeps_no_hour(self):
        self.client.post("/notices/pay_credited",
                         data={"enabled": "on", "at_hour": "5"})
        state = logic.notice_settings(_run(self.crm.notices()))
        self.assertIsNone(state["pay_credited"]["at_hour"],
                          "события не ждут расписания")

    def test_param_is_saved_and_checked(self):
        self.client.post("/notices/review_ask",
                         data={"enabled": "on", "at_hour": "10",
                               "at_minute": "0", "after_days": "45"})
        state = logic.notice_settings(_run(self.crm.notices()))
        self.assertEqual(logic.notice_param(state["review_ask"], "after_days"), 45)
        self.client.post("/notices/review_ask",
                         data={"enabled": "on", "at_hour": "10",
                               "at_minute": "0", "after_days": "999"})
        state = logic.notice_settings(_run(self.crm.notices()))
        self.assertEqual(logic.notice_param(state["review_ask"], "after_days"), 45,
                         "срок вне разумного не сохраняется")

    def test_bad_hour_is_refused(self):
        self.client.post("/notices/daily_digest",
                         data={"enabled": "on", "at_hour": "77"})
        self.assertEqual(_run(self.crm.notices()), [])

    def test_unknown_code_is_404(self):
        r = self.client.post("/notices/нет-такого", data={"enabled": "on"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
