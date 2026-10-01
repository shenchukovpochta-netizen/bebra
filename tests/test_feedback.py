"""Оценка аренды после сдачи: «Как вам аренда?» и сигнал о низкой оценке.

Что стережём: вопрос встаёт в очередь при закрытии аренды с любого входа
(панель, бот) и только когда техника вернулась; спрашивает один круг
процесса бота и один раз; тумблер владельца выключает вопрос, и включение
обратно не обрушивает накопленное; оценку ставит только клиент этой
аренды и только один раз - кнопка с чужим или подделанным callback
ничего не пишет; низкая оценка просит комментарий и уходит в служебный
чат без телефона и без слов клиента.
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_web as tw

    from app.crm import feedback, service
    from tests.fake_crm import FakeCrm
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

try:
    from app import texts as texts_ru
    from app.handlers import feedback as feedback_h
    from app.max import handlers as max_h
    HAVE_AIOGRAM = True
except ImportError:                                    # pragma: no cover
    HAVE_AIOGRAM = False

try:
    import asyncpg
    import pgserver

    from app.crm.db import CrmDB
    from app.db import Database, _init_connection
    HAVE_PG = True
except ImportError:                                    # pragma: no cover
    HAVE_PG = False

SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"

D = Decimal
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
# Аренда неделю назад: закрытая в день выдачи - исправление оператора, и
# её не спрашивают (logic.feedback_void).
WEEK_AGO = date.today() - timedelta(days=7)


def daytime(days: int = 0) -> datetime:
    """Местный полдень: вопрос задаётся только днём (FEEDBACK_HOURS), и
    прогон в полночь не должен менять исход теста."""
    noon = datetime.combine(date.today() + timedelta(days=days), datetime.min.time())
    return noon.replace(hour=12).astimezone()


def run(coro):
    return asyncio.run(coro)


# ─────────────────────────── чистая логика ───────────────────────────

class TestFeedbackLogic(unittest.TestCase):
    def test_callback_round_trip(self):
        data = logic.feedback_callback(42, 4)
        self.assertEqual(data, "fb:42:4")
        self.assertEqual(logic.parse_feedback_callback(data), (42, 4))

    def test_tampered_callback_is_refused(self):
        """Подделанная кнопка - это не оценка: вне 1..5, лишний хвост,
        не число, отрицательный номер - всё None."""
        for bad in ("fb:42:6", "fb:42:0", "fb:42:4:1", "fb:x:4", "fb:-1:4",
                    "fb:42", "fbx:42:4", "", None, "fb:1234567890123:5"):
            self.assertIsNone(logic.parse_feedback_callback(bad), bad)

    def test_low_is_three_and_below(self):
        self.assertEqual([s for s in logic.FEEDBACK_SCORES if logic.feedback_low(s)],
                         [1, 2, 3])
        self.assertFalse(logic.feedback_low(None))
        self.assertFalse(logic.feedback_low("мусор"))

    def test_only_a_returned_bike_is_asked_about(self):
        for status in ("available", "repair", "maintenance", "reserved"):
            self.assertTrue(logic.feedback_wanted(status), status)
        for status in ("lost", "sold", "written_off", None):
            self.assertFalse(logic.feedback_wanted(status), status)

    def test_channel_prefers_telegram_and_needs_max_bot(self):
        self.assertEqual(logic.feedback_channel({"tg_id": 1, "max_id": 2},
                                                max_ready=True), "tg")
        self.assertEqual(logic.feedback_channel({"max_id": 2}, max_ready=True), "max")
        self.assertIsNone(logic.feedback_channel({"max_id": 2}, max_ready=False))
        self.assertIsNone(logic.feedback_channel({}, max_ready=True))

    def test_skip_reasons(self):
        fresh = {"tg_id": 1, "client_status": "active",
                 "closed_at": NOW - timedelta(hours=1)}
        self.assertIsNone(logic.feedback_skip_reason(fresh, enabled=True, now=NOW,
                                                     max_ready=False))
        self.assertEqual(logic.feedback_skip_reason(fresh, enabled=False, now=NOW,
                                                    max_ready=False),
                         "выключено в настройках")
        old = {**fresh, "closed_at": NOW - timedelta(hours=logic.FEEDBACK_ASK_HOURS + 1)}
        self.assertIn("давнее", logic.feedback_skip_reason(old, enabled=True, now=NOW,
                                                           max_ready=False))
        blocked = {**fresh, "client_status": "blacklist"}
        self.assertEqual(logic.feedback_skip_reason(blocked, enabled=True, now=NOW,
                                                    max_ready=False), "клиент не активен")
        nobody = {**fresh, "tg_id": None}
        self.assertEqual(logic.feedback_skip_reason(nobody, enabled=True, now=NOW,
                                                    max_ready=False), "клиента нет в боте")
        same_day = {**fresh, "started_on": NOW.date(), "closed_on": NOW.date()}
        self.assertIn("в день выдачи", logic.feedback_skip_reason(
            same_day, enabled=True, now=NOW, max_ready=False))
        week = {**fresh, "started_on": NOW.date() - timedelta(days=7),
                "closed_on": NOW.date()}
        self.assertIsNone(logic.feedback_skip_reason(week, enabled=True, now=NOW,
                                                     max_ready=False))

    def test_backdated_return_is_stale_by_its_date(self):
        """Закрыли сейчас, а дата возврата - неделю назад: closed_at свежий,
        но спрашивать про такую сдачу поздно. Вчерашняя - ещё нет."""
        row = {"closed_at": NOW - timedelta(minutes=1),
               "started_on": NOW.date() - timedelta(days=30)}
        self.assertTrue(logic.feedback_stale(
            {**row, "closed_on": NOW.date() - timedelta(days=7)}, NOW))
        self.assertTrue(logic.feedback_stale(
            {**row, "closed_on": NOW.date() - timedelta(days=3)}, NOW))
        self.assertFalse(logic.feedback_stale(
            {**row, "closed_on": NOW.date() - timedelta(days=1)}, NOW))
        self.assertFalse(logic.feedback_stale({**row, "closed_on": NOW.date()}, NOW))

    def test_day_window_comes_from_the_notice(self):
        self.assertEqual(logic.NOTICES["feedback_ask"]["params"],
                         {"from_hour": logic.FEEDBACK_HOURS[0],
                          "to_hour": logic.FEEDBACK_HOURS[1]})
        self.assertTrue(logic.feedback_hours_ok({}, datetime(2026, 9, 20, 9, 0)))
        self.assertTrue(logic.feedback_hours_ok({}, datetime(2026, 9, 20, 20, 59)))
        self.assertFalse(logic.feedback_hours_ok({}, datetime(2026, 9, 20, 23, 40)))
        self.assertFalse(logic.feedback_hours_ok({}, datetime(2026, 9, 20, 3, 0)))
        late = {"extra": {"from_hour": 10, "to_hour": 24}}
        self.assertTrue(logic.feedback_hours_ok(late, datetime(2026, 9, 20, 23, 40)))

    def test_comment_is_checked(self):
        self.assertFalse(logic.check_feedback_comment("   ").ok)
        self.assertFalse(logic.check_feedback_comment(None).ok)
        self.assertFalse(logic.check_feedback_comment(
            "я" * (logic.FEEDBACK_COMMENT_MAX + 1)).ok)
        self.assertEqual(logic.check_feedback_comment("  тормоза\r\nскрипят ").value,
                         "тормоза\nскрипят")

    def test_alert_has_no_phone_and_no_words_of_the_client(self):
        row = {"score": 2, "full_name": "Иванов <Иван>", "phone": "+79990000000",
               "location": "Павлюхина", "rental_id": 17, "bike_code": "МБ-7",
               "comment": "грязный велосипед, позвоните мне +79990000000"}
        text = logic.feedback_alert_text(row)
        self.assertIn("2 из 5", text)
        self.assertIn("Иванов &lt;Иван&gt;", text, "имя экранировано под HTML")
        self.assertIn("/rentals/17", text)
        self.assertIn("Павлюхина", text)
        self.assertIn("есть комментарий", text)
        self.assertNotIn("+7999", text)
        self.assertNotIn("грязный", text)
        self.assertIn("без комментария", logic.feedback_alert_text({**row, "comment": None}))
        self.assertIn("без точки", logic.feedback_alert_text({**row, "location": None}))

    def test_report_by_month_and_point(self):
        today = date(2026, 9, 20)
        rows = [
            {"channel": "tg", "score": 5, "closed_on": date(2026, 9, 3),
             "location": "Павлюхина"},
            {"channel": "tg", "score": 2, "closed_on": date(2026, 9, 5),
             "location": "Павлюхина", "answered_at": NOW, "comment": "долго"},
            {"channel": "max", "score": None, "closed_on": date(2026, 8, 30),
             "location": "Адоратского"},
            # не спросили: клиента нет в боте - в долю ответов не входит
            {"channel": None, "score": None, "closed_on": date(2026, 8, 29),
             "location": None},
            # за окном отчёта
            {"channel": "tg", "score": 1, "closed_on": date(2024, 1, 1),
             "location": "Павлюхина"},
        ]
        data = logic.feedback_report(rows, months=3, today=today)
        self.assertEqual([m["month"] for m in data["months"]],
                         [date(2026, 9, 1), date(2026, 8, 1), date(2026, 7, 1)])
        sept = data["months"][0]
        self.assertEqual((sept["asked"], sept["answered"], sept["low"]), (2, 2, 1))
        self.assertEqual(sept["avg"], D("3.5"))
        self.assertEqual(sept["dist"], {1: 0, 2: 1, 3: 0, 4: 0, 5: 1})
        aug = data["months"][1]
        self.assertEqual((aug["asked"], aug["answered"], aug["rate"], aug["avg"]),
                         (1, 0, 0, None))
        self.assertIsNone(data["months"][2]["rate"], "не спрашивали - доли нет")
        self.assertEqual([p["location"] for p in data["points"]],
                         ["Адоратского", "Павлюхина", None], "«без точки» - последней")
        self.assertEqual(data["total"]["asked"], 3)
        self.assertEqual([r["comment"] for r in data["low"]], ["долго"])
        self.assertEqual(logic.feedback_avg(D("4.3")), "4,3")
        self.assertEqual(logic.feedback_avg(None), "—")
        self.assertEqual(logic.feedback_stars(4), "★★★★☆")
        self.assertEqual(logic.feedback_stars(9), "")

    def test_ask_and_low_notices_are_in_the_catalogue(self):
        self.assertIsNone(logic.NOTICES["feedback_ask"]["hour"], "уходит по событию")
        self.assertEqual(logic.NOTICES["feedback_ask"]["target"], "client")
        self.assertEqual(logic.NOTICES["feedback_low"]["target"], "chat")


# ─────────────────────────── очередь и сервис ───────────────────────────

class SentMessage(types.SimpleNamespace):
    pass


class RecordingBot:
    """Бот, который возвращает отправленное сообщение с номером - как
    Telegram: номер просьбы о комментарии запоминается у оценки."""

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send_message(self, chat_id, text, reply_markup=None, **_):
        self.sent.append((chat_id, text, reply_markup))
        return SentMessage(message_id=1000 + len(self.sent))


class FakeMax:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.answers: list[tuple] = []
        self.fail = fail

    async def send(self, *, user_id=None, chat_id=None, text, keyboard=None, **_):
        if self.fail:
            raise RuntimeError("MAX лёг")
        self.sent.append({"user_id": user_id, "text": text, "keyboard": keyboard})
        return {"message": {"body": {"mid": f"mid.{len(self.sent)}"}}}

    async def answer_callback(self, callback_id, notification=None):
        self.answers.append((callback_id, notification))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class FeedbackCase(unittest.TestCase):
    def setUp(self):
        self.crm = FakeCrm()
        self.bot = RecordingBot()
        self.cfg = types.SimpleNamespace(contract_chat_id=-1001)
        self.client_id = run(self.crm.create_client(full_name="Иванов Иван",
                                                    phone="+79990000000", tg_id=5001))
        self.bike_id = run(self.crm.create_bike(code="МБ-7", model="Kugoo V3",
                                                location="Павлюхина"))

    def rent(self, client_id=None):
        return run(self.crm.create_rental(
            client_id=client_id or self.client_id, bike_id=self.bike_id, tariff_id=None,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="manual",
            started_on=WEEK_AGO, contract_no=None, created_by="t"))

    def close(self, rental_id, bike_status="available"):
        run(service.close_rental(self.crm, run(self.crm.rental(rental_id)),
                                 closed_on=date.today(), note=None,
                                 bike_status=bike_status, by="staff:t"))

    def asked(self, rental_id, **kw):
        """Вопрос ушёл: круг бота разобрал очередь."""
        self.close(rental_id, **kw)
        run(feedback.ask_once(self.bot, self.crm, now=daytime()))
        return run(self.crm.feedback_of_rental(rental_id))


class TestQueue(FeedbackCase):
    def test_closing_queues_one_question(self):
        rid = self.rent()
        self.close(rid)
        row = run(self.crm.feedback_of_rental(rid))
        self.assertIsNotNone(row)
        self.assertIsNone(row["asked_at"])
        self.assertFalse(run(self.crm.queue_feedback(rid, self.client_id)),
                         "строка одна на аренду - второго вопроса не будет")

    def test_lost_sold_and_written_off_are_not_asked(self):
        for n, status in enumerate(("lost", "sold", "written_off")):
            bike = run(self.crm.create_bike(code=f"X-{status}", model="Kugoo V3"))
            client = run(self.crm.create_client(full_name=status, phone=f"+7900000000{n}",
                                                tg_id=6000 + n))
            rid = run(self.crm.create_rental(
                client_id=client, bike_id=bike, tariff_id=None, tariff_name="Неделя",
                period_days=7, price=D(3000), billing="manual", started_on=WEEK_AGO,
                contract_no=None, created_by="t"))
            self.close(rid, bike_status=status)
            self.assertIsNone(run(self.crm.feedback_of_rental(rid)), status)

    def test_theft_declaration_does_not_ask(self):
        rid = self.rent()
        run(service.declare_theft(self.crm, run(self.crm.rental(rid)), note=None,
                                  by="staff:t"))
        self.assertIsNone(run(self.crm.feedback_of_rental(rid)))

    def test_queue_failure_does_not_break_closing(self):
        rid = self.rent()

        async def boom(*_a, **_k):
            raise RuntimeError("база легла")
        self.crm.queue_feedback = boom
        self.close(rid)
        self.assertEqual(run(self.crm.rental(rid))["status"], "closed")

    def test_bot_close_path_queues_too(self):
        """Акт возврата подписан в боте - тот же service.close_rental."""
        from app.crm import sync
        rid = self.rent()
        run(sync.on_rental_closed(self.crm, {"tg_id": 5001, "contract_no": "АВ-1"},
                                  today=date.today()))
        self.assertEqual(run(self.crm.rental(rid))["status"], "closed")
        self.assertIsNotNone(run(self.crm.feedback_of_rental(rid)))

    def test_buyout_in_the_bot_does_not_ask(self):
        from app.crm import sync
        rid = self.rent()
        run(sync.on_rental_closed(self.crm, {"tg_id": 5001, "contract_no": "АВ-1",
                                             "buyout_signed_at": NOW},
                                  today=date.today()))
        self.assertIsNone(run(self.crm.feedback_of_rental(rid)))


class TestAsk(FeedbackCase):
    def test_question_goes_once_with_five_buttons(self):
        rid = self.rent()
        self.close(rid)
        self.assertEqual(run(feedback.ask_once(self.bot, self.crm, now=daytime())), 1)
        chat, text, markup = self.bot.sent[-1]
        self.assertEqual(chat, 5001)
        self.assertIn("Как вам аренда", text)
        self.assertIn("МБ-7", text)
        data = [b.callback_data for b in markup.inline_keyboard[0]]
        self.assertEqual(data, [f"fb:{rid}:{s}" for s in range(1, 6)])
        row = run(self.crm.feedback_of_rental(rid))
        self.assertEqual(row["channel"], "tg")
        self.assertIsNotNone(row["asked_at"])
        self.assertEqual(run(feedback.ask_once(self.bot, self.crm, now=daytime())), 0)
        self.assertEqual(len(self.bot.sent), 1, "второй круг не спрашивает снова")
        log = run(self.crm.notice_log(limit=5))
        self.assertEqual((log[0]["code"], log[0]["status"]), ("feedback_ask", "sent"))

    def test_switched_off_is_marked_and_not_caught_up(self):
        run(self.crm.set_notice("feedback_ask", enabled=False, at_hour=None, by="t"))
        rid = self.rent()
        self.close(rid)
        self.assertEqual(run(feedback.ask_once(self.bot, self.crm, now=daytime())), 0)
        self.assertEqual(self.bot.sent, [])
        row = run(self.crm.feedback_of_rental(rid))
        self.assertIsNone(row["channel"])
        self.assertEqual(row["skipped"], "выключено в настройках")
        self.assertEqual(run(self.crm.notice_log(limit=1))[0]["status"], "skipped")
        # Включили обратно - накопленное не догоняется.
        run(self.crm.set_notice("feedback_ask", enabled=True, at_hour=None, by="t"))
        run(feedback.ask_once(self.bot, self.crm, now=daytime()))
        self.assertEqual(self.bot.sent, [])

    def test_client_without_a_bot_is_skipped(self):
        other = run(self.crm.create_client(full_name="Без бота", phone="+79990000001"))
        rid = self.rent(other)
        self.close(rid)
        run(feedback.ask_once(self.bot, self.crm, now=daytime()))
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(run(self.crm.feedback_of_rental(rid))["skipped"],
                         "клиента нет в боте")

    def test_max_client_is_asked_in_max(self):
        other = run(self.crm.create_client(full_name="Из MAX", phone="+79990000002"))
        run(self.crm.update_client(other, max_id=777))
        rid = self.rent(other)
        self.close(rid)
        mx = FakeMax()
        self.assertEqual(run(feedback.ask_once(self.bot, self.crm, max_client=mx,
                                               now=daytime())), 1)
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(mx.sent[0]["user_id"], 777)
        self.assertEqual(mx.sent[0]["keyboard"][0][0]["payload"], f"fb:{rid}:1")
        self.assertEqual(run(self.crm.feedback_of_rental(rid))["channel"], "max")

    def test_max_failure_is_recorded_not_retried(self):
        other = run(self.crm.create_client(full_name="Из MAX", phone="+79990000002"))
        run(self.crm.update_client(other, max_id=777))
        rid = self.rent(other)
        self.close(rid)
        self.assertEqual(run(feedback.ask_once(self.bot, self.crm,
                                               max_client=FakeMax(fail=True), now=daytime())), 0)
        self.assertEqual(run(self.crm.notice_log(limit=1))[0]["status"], "failed")
        mx = FakeMax()
        run(feedback.ask_once(self.bot, self.crm, max_client=mx, now=daytime()))
        self.assertEqual(mx.sent, [], "отметка до отправки: повтора нет")

    def test_same_day_close_is_a_correction_not_asked(self):
        """Выдали не тот велосипед и закрыли в тот же день - это исправление
        оператора (как у оценки риска), и человеку на точке не приходит
        «как вам аренда?»."""
        rid = run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=None,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="manual",
            started_on=date.today(), contract_no=None, created_by="t"))
        self.close(rid)
        self.assertEqual(run(feedback.ask_once(self.bot, self.crm, now=daytime())), 0)
        self.assertEqual(self.bot.sent, [])
        row = run(self.crm.feedback_of_rental(rid))
        self.assertIsNone(row["channel"])
        self.assertIn("в день выдачи", row["skipped"])

    def test_backdated_close_is_not_asked(self):
        """Оператор вечером догоняет возвраты: закрытие сейчас, дата - неделю
        назад. Спрашивать поздно, как и у бота, пролежавшего неделю."""
        rid = self.rent()
        run(service.close_rental(self.crm, run(self.crm.rental(rid)),
                                 closed_on=date.today() - timedelta(days=5), note=None,
                                 by="staff:t"))
        run(feedback.ask_once(self.bot, self.crm, now=daytime()))
        self.assertEqual(self.bot.sent, [])
        self.assertIn("давнее", run(self.crm.feedback_of_rental(rid))["skipped"])

    def test_night_close_is_asked_in_the_morning(self):
        rid = self.rent()
        self.close(rid)
        night = daytime().replace(hour=23, minute=40)
        self.assertEqual(run(feedback.ask_once(self.bot, self.crm, now=night)), 0)
        self.assertIsNone(run(self.crm.feedback_of_rental(rid))["asked_at"],
                          "ночью очередь ждёт, а не помечается")
        morning = daytime(1).replace(hour=9, minute=5)
        self.assertEqual(run(feedback.ask_once(self.bot, self.crm, now=morning)), 1)
        self.assertEqual(run(self.crm.feedback_of_rental(rid))["channel"], "tg")

    def test_stale_return_is_not_asked(self):
        rid = self.rent()
        self.close(rid)
        later = daytime(3)
        run(feedback.ask_once(self.bot, self.crm, now=later))
        self.assertEqual(self.bot.sent, [])
        self.assertIn("давнее", run(self.crm.feedback_of_rental(rid))["skipped"])


class TestRate(FeedbackCase):
    def test_only_the_client_of_the_rental_can_rate(self):
        rid = self.rent()
        self.asked(rid)
        with self.assertRaises(service.ServiceError) as err:
            run(service.rate_rental(self.crm, rid, 1, channel="tg", user_id=9999))
        self.assertIn("не для вас", str(err.exception))
        with self.assertRaises(service.ServiceError):
            run(service.rate_rental(self.crm, rid, 5, channel="max", user_id=5001))
        self.assertIsNone(run(self.crm.feedback_of_rental(rid))["score"])

    def test_one_answer_per_rental(self):
        rid = self.rent()
        self.asked(rid)
        run(service.rate_rental(self.crm, rid, 4, channel="tg", user_id=5001))
        with self.assertRaises(service.ServiceError) as err:
            run(service.rate_rental(self.crm, rid, 1, channel="tg", user_id=5001))
        self.assertIn("уже принята", str(err.exception))
        self.assertEqual(run(self.crm.feedback_of_rental(rid))["score"], 4)

    def test_not_asked_cannot_be_rated(self):
        """Кнопку не показывали - и оценки нет: номер аренды подобрать
        можно, но вопрос в очереди или пропущен."""
        rid = self.rent()
        self.close(rid)
        with self.assertRaises(service.ServiceError):
            run(service.rate_rental(self.crm, rid, 5, channel="tg", user_id=5001))
        run(self.crm.mark_feedback_asked(run(self.crm.feedback_of_rental(rid))["id"],
                                         channel=None, skipped="выключено в настройках"))
        with self.assertRaises(service.ServiceError):
            run(service.rate_rental(self.crm, rid, 5, channel="tg", user_id=5001))
        with self.assertRaises(service.ServiceError):
            run(service.rate_rental(self.crm, 999, 5, channel="tg", user_id=5001))

    def test_score_out_of_range_is_refused(self):
        rid = self.rent()
        self.asked(rid)
        with self.assertRaises(service.ServiceError):
            run(service.rate_rental(self.crm, rid, 7, channel="tg", user_id=5001))

    def test_comment_once(self):
        rid = self.rent()
        row = self.asked(rid)
        run(service.rate_rental(self.crm, rid, 2, channel="tg", user_id=5001))
        with self.assertRaises(service.ServiceError):
            run(service.comment_rental(self.crm, row, "  "))
        self.assertEqual(run(service.comment_rental(self.crm, row, "грязный")), "грязный")
        with self.assertRaises(service.ServiceError):
            run(service.comment_rental(self.crm, row, "и ещё"))
        self.assertEqual(run(self.crm.feedback_of_rental(rid))["comment"], "грязный")

    def test_comment_lives_as_long_as_the_inbox(self):
        """Комментарий - слова клиента, как переписка «Входящих»: дневной
        проход стирает его по тому же сроку, оценка остаётся. Поздний ответ
        на ту же просьбу место стёртого не занимает."""
        from app.crm import billing
        self.assertEqual(logic.FEEDBACK_COMMENT_KEEP_DAYS, logic.INBOX_KEEP_DAYS)
        rid = self.rent()
        row = self.asked(rid)
        run(service.rate_rental(self.crm, rid, 2, channel="tg", user_id=5001))
        run(service.comment_rental(self.crm, row, "перезвоните, живу на Павлюхина 12"))
        cfg = types.SimpleNamespace(contract_chat_id=-1, remind_before_days=2,
                                    bike_photo_dir=None)
        db = types.SimpleNamespace(get_user=_none)

        def daily():
            run(billing.run_daily(self.bot, db, self.crm, cfg, today=date.today(),
                                  now=datetime.now(), done={}))
        daily()
        self.assertIsNotNone(run(self.crm.feedback_of_rental(rid))["comment"], "свежий")
        f = next(x for x in self.crm.feedback_.values() if x["rental_id"] == rid)
        f["commented_at"] -= timedelta(days=logic.FEEDBACK_COMMENT_KEEP_DAYS + 1)
        daily()
        after = run(self.crm.feedback_of_rental(rid))
        self.assertIsNone(after["comment"])
        self.assertEqual(after["score"], 2, "оценка остаётся в отчёте")
        with self.assertRaises(service.ServiceError):
            run(service.comment_rental(self.crm, row, "поздний ответ"))
        self.assertIsNone(run(self.crm.feedback_of_rental(rid))["comment"])


async def _none(*_a, **_k):
    return None


class TestAlert(FeedbackCase):
    def rated(self, score):
        rid = self.rent()
        self.asked(rid)
        run(service.rate_rental(self.crm, rid, score, channel="tg", user_id=5001))
        self.bot.sent.clear()
        return rid

    def test_low_score_waits_for_the_comment(self):
        rid = self.rated(2)
        self.assertEqual(run(feedback.alert_once(self.bot, self.crm, self.cfg)), 0,
                         "комментария ещё нет и время ждать не вышло")
        row = run(self.crm.feedback_of_rental(rid))
        run(service.comment_rental(self.crm, row, "долго ждал, +79990000000"))
        self.assertEqual(run(feedback.alert_once(self.bot, self.crm, self.cfg)), 1)
        chat, text, _ = self.bot.sent[-1]
        self.assertEqual(chat, -1001, "служебный чат по умолчанию")
        self.assertIn("2 из 5", text)
        self.assertNotIn("+7999", text)
        self.assertNotIn("долго ждал", text)
        self.assertEqual(run(feedback.alert_once(self.bot, self.crm, self.cfg)), 0,
                         "сигнал один раз")

    def test_low_score_without_comment_goes_after_the_wait(self):
        rid = self.rated(1)
        f = next(x for x in self.crm.feedback_.values() if x["rental_id"] == rid)
        f["answered_at"] -= timedelta(minutes=logic.FEEDBACK_ALERT_WAIT_MINUTES + 1)
        self.assertEqual(run(feedback.alert_once(self.bot, self.crm, self.cfg)), 1)
        self.assertIn("без комментария", self.bot.sent[-1][1])

    def test_good_score_is_not_a_signal(self):
        rid = self.rated(4)
        f = next(x for x in self.crm.feedback_.values() if x["rental_id"] == rid)
        f["answered_at"] -= timedelta(hours=1)
        self.assertEqual(run(feedback.alert_once(self.bot, self.crm, self.cfg)), 0)
        self.assertEqual(self.bot.sent, [])

    def test_owner_picks_the_recipient_or_switches_it_off(self):
        rid = self.rated(3)
        row = run(self.crm.feedback_of_rental(rid))
        run(service.comment_rental(self.crm, row, "тормоза"))
        run(self.crm.set_notice("feedback_low", enabled=True, at_hour=None,
                                chat_id="777", by="t"))
        run(feedback.alert_once(self.bot, self.crm, self.cfg))
        self.assertEqual(self.bot.sent[-1][0], "777")
        rid2 = self.rated(2)
        run(service.comment_rental(self.crm, run(self.crm.feedback_of_rental(rid2)), "x"))
        run(self.crm.set_notice("feedback_low", enabled=False, at_hour=None, by="t"))
        self.bot.sent.clear()
        run(feedback.alert_once(self.bot, self.crm, self.cfg))
        self.assertEqual(self.bot.sent, [])
        self.assertIsNotNone(run(self.crm.feedback_of_rental(rid2))["alerted_at"],
                             "выключенный сигнал не копится")


# ─────────────────────────── кнопки ботов ───────────────────────────

@unittest.skipUnless(HAVE_WEB and HAVE_AIOGRAM, "aiogram или fastapi не установлены")
class TestTelegramButtons(FeedbackCase):
    def press(self, data, user_id=5001):
        answered = []

        async def answer(text=None, show_alert=False):
            answered.append(text)
        cb = types.SimpleNamespace(data=data, answer=answer,
                                   from_user=types.SimpleNamespace(id=user_id))
        run(feedback_h.cb_feedback(cb, self.bot, crm=self.crm))
        return answered

    def test_stranger_and_tampered_buttons_do_nothing(self):
        rid = self.rent()
        self.asked(rid)
        self.assertIn("не для вас", self.press(f"fb:{rid}:1", user_id=4242)[0])
        self.assertEqual(self.press(f"fb:{rid}:9")[0], "Эта кнопка устарела.")
        self.assertEqual(self.press("fb:abc:1")[0], "Эта кнопка устарела.")
        self.assertEqual(self.press(f"fb:{rid + 1000}:5")[0], "Эта кнопка устарела.")
        self.assertIsNone(run(self.crm.feedback_of_rental(rid))["score"])

    def test_good_score_says_thanks(self):
        rid = self.rent()
        self.asked(rid)
        self.bot.sent.clear()
        self.assertIn("принята", self.press(f"fb:{rid}:5")[0])
        self.assertEqual(run(self.crm.feedback_of_rental(rid))["score"], 5)
        self.assertIn("Спасибо за оценку", self.bot.sent[-1][1])
        self.assertIn("уже принята", self.press(f"fb:{rid}:1")[0])

    def test_low_score_asks_for_a_comment_by_reply(self):
        rid = self.rent()
        self.asked(rid)
        self.bot.sent.clear()
        self.press(f"fb:{rid}:2")
        chat, text, markup = self.bot.sent[-1]
        self.assertIn("Что пошло не так", text)
        self.assertTrue(markup.force_reply)
        row = run(self.crm.feedback_of_rental(rid))
        self.assertEqual(row["prompt_msg"], "1001")
        # Ответ на просьбу - комментарий; фильтр сверяет и сообщение, и автора.
        flt = feedback_h.FeedbackReply()

        def reply(user_id, to_id, text="велосипед грязный"):
            answers = []

            async def answer(t, **_):
                answers.append(t)
            return types.SimpleNamespace(
                chat=types.SimpleNamespace(type="private"),
                from_user=types.SimpleNamespace(id=user_id), text=text, caption=None,
                reply_to_message=types.SimpleNamespace(
                    message_id=to_id, from_user=types.SimpleNamespace(is_bot=True)),
                answer=answer, answers=answers)

        self.assertFalse(run(flt(reply(4242, 1001), crm=self.crm)), "чужой ответ")
        self.assertFalse(run(flt(reply(5001, 555), crm=self.crm)), "не на просьбу")
        msg = reply(5001, 1001)
        got = run(flt(msg, crm=self.crm))
        self.assertEqual(got["feedback"]["rental_id"], rid)
        run(feedback_h.st_feedback_comment(msg, got["feedback"], crm=self.crm))
        self.assertIn("Спасибо", msg.answers[-1])
        self.assertEqual(run(self.crm.feedback_of_rental(rid))["comment"],
                         "велосипед грязный")
        again = reply(5001, 1001, "второй")
        run(feedback_h.st_feedback_comment(again, got["feedback"], crm=self.crm))
        self.assertIn("уже получили", again.answers[-1])


@unittest.skipUnless(HAVE_WEB and HAVE_AIOGRAM, "aiogram или fastapi не установлены")
class TestMaxButtons(FeedbackCase):
    def setUp(self):
        super().setUp()
        self.max_client = run(self.crm.create_client(full_name="Из MAX",
                                                     phone="+79990000002"))
        run(self.crm.update_client(self.max_client, max_id=777))
        self.rid = self.rent(self.max_client)
        self.close(self.rid)
        self.mx = FakeMax()
        run(feedback.ask_once(self.bot, self.crm, max_client=self.mx, now=daytime()))
        self.ctx = types.SimpleNamespace(cl=self.mx, crm=self.crm)

    def test_stranger_is_refused_owner_rates_and_comments(self):
        run(max_h.cb_feedback(self.ctx, {"tg_id": 4242}, "c1", f"fb:{self.rid}:1"))
        self.assertIn("не для вас", self.mx.answers[-1][1])
        run(max_h.cb_feedback(self.ctx, {"tg_id": 777}, "c2", f"fb:{self.rid}:2"))
        row = run(self.crm.feedback_of_rental(self.rid))
        self.assertEqual(row["score"], 2)
        self.assertEqual(row["prompt_msg"], f"mid.{len(self.mx.sent)}")
        self.assertFalse(run(max_h.st_feedback_comment(self.ctx, {"tg_id": 777},
                                                       "mid.other", "текст")))
        self.assertFalse(run(max_h.st_feedback_comment(self.ctx, {"tg_id": 4242},
                                                       row["prompt_msg"], "текст")))
        self.assertTrue(run(max_h.st_feedback_comment(self.ctx, {"tg_id": 777},
                                                      row["prompt_msg"], "тормоза")))
        self.assertEqual(run(self.crm.feedback_of_rental(self.rid))["comment"], "тормоза")

    def test_telegram_button_data_is_not_a_max_answer(self):
        """Вопрос ушёл в MAX: то же нажатие из Telegram (тот же номер у
        человека в обоих мессенджерах) не принимается."""
        with self.assertRaises(service.ServiceError):
            run(service.rate_rental(self.crm, self.rid, 5, channel="tg", user_id=777))

    def test_tampered_payload(self):
        run(max_h.cb_feedback(self.ctx, {"tg_id": 777}, "c3", "fb:1:77"))
        self.assertEqual(self.mx.answers[-1][1], "Эта кнопка устарела.")
        self.assertIsNone(run(self.crm.feedback_of_rental(self.rid))["score"])


@unittest.skipUnless(HAVE_WEB and HAVE_AIOGRAM, "aiogram или fastapi не установлены")
class TestThroughTheDispatcher(unittest.IsolatedAsyncioTestCase):
    """Настоящий Dispatcher с роутером оценки перед меню, как в app/main.py:
    нажатие проходит конвейер, а ответ на просьбу о комментарии не
    проваливается в ловушку меню."""

    async def asyncSetUp(self):
        import importlib

        import test_flow as tf
        from aiogram import Bot, Dispatcher

        from app.middlewares import PipelineMiddleware
        from app.services.crypto import Vault
        self.tf = tf
        for module in (tf.contract, tf.registration, tf.moderation,
                       tf.faq_handlers, tf.menu, feedback_h):
            importlib.reload(module)
        self.crm = FakeCrm()
        self.cfg = tf.make_config()
        self.db = tf.FakeDB()
        self.session = tf.FakeSession()
        self.bot = Bot("123:abc", session=self.session)
        self.dp = Dispatcher()
        self.dp.update.outer_middleware(PipelineMiddleware(
            self.db, self.cfg, Vault.from_raw(self.cfg.pdn_key), self.crm))
        for module in (feedback_h, tf.moderation, tf.contract, tf.registration,
                       tf.faq_handlers, tf.menu):
            self.dp.include_router(module.router)
        await self.db.upsert_user(tf.USER_ID, "ivan")
        self.db.users[tf.USER_ID].update(state=tf.logic.APPROVED,
                                         status=tf.logic.ST_APPROVED, lang="ru",
                                         full_name="Иванов Иван")
        client = await self.crm.create_client(full_name="Иванов Иван",
                                              phone="+79990000000", tg_id=tf.USER_ID)
        bike = await self.crm.create_bike(code="МБ-7", model="Kugoo V3")
        self.rid = await self.crm.create_rental(
            client_id=client, bike_id=bike, tariff_id=None, tariff_name="Неделя",
            period_days=7, price=D(3000), billing="manual", started_on=WEEK_AGO,
            contract_no=None, created_by="t")
        await service.close_rental(self.crm, await self.crm.rental(self.rid),
                                   closed_on=date.today(), note=None, by="bot")
        await feedback.ask_once(self.bot, self.crm, now=daytime())

    async def asyncTearDown(self):
        await self.bot.session.close()

    async def feed(self, update):
        await self.dp.feed_update(self.bot, update)

    def texts(self):
        return [m.text or "" for m in self.session.sent_to(self.tf.USER_ID)
                if isinstance(m, self.tf.SendMessage)]

    async def test_press_and_comment(self):
        tf = self.tf
        await self.feed(tf.cb(f"fb:{self.rid}:2", user_id=tf.USER_ID + 1,
                              chat_id=tf.USER_ID + 1))
        self.assertIsNone((await self.crm.feedback_of_rental(self.rid))["score"],
                          "чужое нажатие не ставит оценку")
        await self.feed(tf.cb(f"fb:{self.rid}:2"))
        row = await self.crm.feedback_of_rental(self.rid)
        self.assertEqual(row["score"], 2)
        self.assertIn("Что пошло не так", self.texts()[-1])
        before = len(self.texts())
        await self.feed(tf.msg("Тормоза скрипели", reply_to=int(row["prompt_msg"])))
        self.assertEqual((await self.crm.feedback_of_rental(self.rid))["comment"],
                         "Тормоза скрипели")
        self.assertEqual(self.texts()[before:], ["Спасибо, передали руководителю."],
                         "меню ответ не перехватило")
        self.assertEqual(self.db.users[tf.USER_ID]["state"], tf.logic.APPROVED,
                         "состояние сценария оценка не трогает")
        # Просьба пришла с ForceReply - он скрыл меню; ответ его возвращает,
        # иначе недовольный клиент остался бы без «Поддержки» до /start.
        from app import keyboards as kb
        menu = kb.main_menu("ru")
        self.assertEqual(self.menus()[-1], menu)
        await self.feed(tf.msg("и ещё", reply_to=int(row["prompt_msg"])))
        self.assertIn("уже получили", self.texts()[-1])
        self.assertEqual(self.menus()[-1], menu, "и на повторный ответ")

    async def test_former_subscriber_rates_and_comments(self):
        """Из канала ушли вместе с арендой: оценка и комментарий к ней идут
        мимо гейта подписки, остальное гейт держит."""
        tf = self.tf
        self.session.subscribed = False
        await self.feed(tf.cb(f"fb:{self.rid}:2"))
        row = await self.crm.feedback_of_rental(self.rid)
        self.assertEqual(row["score"], 2)
        self.assertNotIn("не подписаны", " ".join(self.texts()))
        await self.feed(tf.msg("Тормоза скрипели", reply_to=int(row["prompt_msg"])))
        self.assertEqual((await self.crm.feedback_of_rental(self.rid))["comment"],
                         "Тормоза скрипели")
        self.assertEqual(self.texts()[-1], "Спасибо, передали руководителю.")
        await self.feed(tf.msg("Тормоза скрипели", reply_to=999))
        self.assertIn("не подписаны", self.texts()[-1], "чужой ответ - за гейтом")

    def menus(self):
        return [m.reply_markup for m in self.session.sent_to(self.tf.USER_ID)
                if isinstance(m, self.tf.SendMessage)]

    def answers(self):
        return [m.text for m in self.session.calls
                if isinstance(m, self.tf.AnswerCallbackQuery)]

    async def test_client_language_is_kept(self):
        """Курьер выбрал английский на /start: вопрос, благодарность, просьба
        о комментарии, подсказка поля и отказы - на английском, как
        остальные сообщения клиенту (MAX-бот говорит по-русски)."""
        from app.i18n import en
        tf = self.tf
        self.db.users[tf.USER_ID]["lang"] = "en"
        rid = await self.crm.create_rental(
            client_id=(await self.crm.feedback_of_rental(self.rid))["client_id"],
            bike_id=await self.crm.create_bike(code="МБ-8", model="Kugoo V3"),
            tariff_id=None, tariff_name="Неделя", period_days=7, price=D(3000),
            billing="manual", started_on=WEEK_AGO, contract_no=None, created_by="t")
        await service.close_rental(self.crm, await self.crm.rental(rid),
                                   closed_on=date.today(), note=None, by="bot")
        before = len(self.texts())
        await feedback.ask_once(self.bot, self.crm, db=self.db, now=daytime())
        self.assertEqual(self.texts()[before:], [
            en.T["FEEDBACK_ASK"].format(bike=en.T["FEEDBACK_BIKE"].format(code="МБ-8"))])
        await self.feed(tf.cb(f"fb:{rid}:2"))
        self.assertEqual(self.answers()[-1], en.T["FEEDBACK_THANKS_TOAST"])
        self.assertEqual(self.texts()[-1], en.T["FEEDBACK_ASK_COMMENT"])
        self.assertEqual(self.menus()[-1].input_field_placeholder,
                         en.T["FEEDBACK_COMMENT_PLACEHOLDER"])
        await self.feed(tf.cb(f"fb:{rid}:4"))
        self.assertEqual(self.answers()[-1], en.T["FEEDBACK_ALREADY"])
        await self.feed(tf.cb(f"fb:{self.rid}:3", user_id=tf.USER_ID + 1,
                              chat_id=tf.USER_ID + 1))
        self.assertEqual(self.answers()[-1], texts_ru.FEEDBACK_NOT_YOURS,
                         "чужой без языка - по-русски")
        row = await self.crm.feedback_of_rental(rid)
        await self.feed(tf.msg("Brakes squeaked", reply_to=int(row["prompt_msg"])))
        self.assertEqual(self.texts()[-1], en.T["FEEDBACK_COMMENT_THANKS"])
        await self.feed(tf.msg("Again", reply_to=int(row["prompt_msg"])))
        self.assertEqual(self.texts()[-1], en.T["FEEDBACK_COMMENT_DONE"])

    def test_every_refusal_is_translatable(self):
        """Отказы сервиса совпадают со строками texts дословно: иначе
        перевод по тексту молча отвалится и клиент получит русский."""
        crm = FakeCrm()

        async def refusals():
            got = []
            cid = await crm.create_client(full_name="К", phone="+79990000001", tg_id=1)
            rid = await crm.create_rental(
                client_id=cid, bike_id=await crm.create_bike(code="B", model="M"),
                tariff_id=None, tariff_name="Неделя", period_days=7, price=D(3000),
                billing="manual", started_on=WEEK_AGO, contract_no=None, created_by="t")
            try:                    # не спрошенную оценить нельзя - «устарела»
                await service.rate_rental(crm, rid, 3, channel="tg", user_id=1)
            except service.ServiceError as exc:
                got.append(str(exc))
            await service.close_rental(crm, await crm.rental(rid), closed_on=date.today(),
                                       note=None, by="t")
            await feedback.ask_once(None, crm, now=daytime())   # вопрос «ушёл» в tg
            for user in (2, 1, 1):
                try:
                    await service.rate_rental(crm, rid, 2, channel="tg", user_id=user)
                except service.ServiceError as exc:
                    got.append(str(exc))
            row = await crm.feedback_of_rental(rid)
            for text in ("", "я" * (logic.FEEDBACK_COMMENT_MAX + 1), "ок", "снова"):
                try:
                    await service.comment_rental(crm, row, text)
                except service.ServiceError as exc:
                    got.append(str(exc))
            return got

        got = asyncio.run(refusals())
        self.assertEqual(len(got), 6, got)
        for text in got:
            self.assertIn(text, feedback_h.FEEDBACK_ERRORS)
            key = feedback_h.FEEDBACK_ERRORS[text]
            self.assertNotEqual(feedback_h.refusal("en", Exception(text)), text, key)


# ─────────────────────────── панель ───────────────────────────

@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestFeedbackPages(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        rid = run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="manual",
            started_on=WEEK_AGO, contract_no=None, created_by="t"))
        r = self.client.post(f"/rentals/{rid}/close",
                             data={"closed_on": date.today().isoformat(),
                                   "bike_status": "available"})
        self.assertEqual(r.status_code, 303)
        self.rid = rid
        run(feedback.ask_once(RecordingBot(), self.crm, now=daytime()))
        run(service.rate_rental(self.crm, rid, 2, channel="tg", user_id=5001))
        run(service.comment_rental(self.crm, run(self.crm.feedback_of_rental(rid)),
                                   "Долго ждал оператора"))

    def test_panel_close_queues_the_question(self):
        self.assertEqual(run(self.crm.feedback_of_rental(self.rid))["channel"], "tg")

    def test_report_shows_months_points_and_low_scores(self):
        page = self.get_ok("/reports/feedback")
        self.assertIn("Оценки аренды", page)
        self.assertIn("Долго ждал оператора", page)
        self.assertIn(f"/rentals/{self.rid}", page)
        self.assertIn("По точкам аренды", page)
        self.assertIn(date.today().strftime("%m.%Y"), page)
        self.assertIn('href="/reports/feedback"', self.get_ok("/reports/channels"),
                      "вкладка отчёта")

    def test_expired_comment_is_marked_not_blank(self):
        """Стёртый по сроку комментарий - не «клиент молчал»: отчёт это
        различает и называет срок."""
        f = next(x for x in self.crm.feedback_.values() if x["rental_id"] == self.rid)
        f["commented_at"] -= timedelta(days=logic.FEEDBACK_COMMENT_KEEP_DAYS + 1)
        run(self.crm.purge_feedback_comments(logic.FEEDBACK_COMMENT_KEEP_DAYS))
        page = self.get_ok("/reports/feedback")
        self.assertNotIn("Долго ждал оператора", page)
        self.assertIn("стёрт по сроку", page)
        self.assertRegex(page, rf"хранится\s+{logic.FEEDBACK_COMMENT_KEEP_DAYS} дней")

    def test_client_and_rental_cards_show_the_score(self):
        page = self.get_ok(f"/clients/{self.client_id}")
        self.assertIn("Оценки аренд", page)
        self.assertIn("★★☆☆☆", page)
        page = self.get_ok(f"/rentals/{self.rid}")
        self.assertIn("Оценка клиента", page)
        self.assertIn("Долго ждал оператора", page)

    def test_report_needs_access_to_clients(self):
        from app.crm import logic as crm_logic
        tech = run(self.crm.access_profile_by_code("tech"))
        run(self.crm.create_staff("petr", crm_logic.hash_password("password-1"),
                                  "Пётр", "manager", tech["id"]))
        self.client.post("/logout")
        self.login("petr", "password-1")
        self.assertEqual(self.client.get("/reports/feedback").status_code, 403)
        # Карточку аренды механик видит, но слова клиента - нет: иначе она
        # была бы обходом закрытого отчёта. Оценка остаётся.
        page = self.get_ok(f"/rentals/{self.rid}")
        self.assertIn("★★☆☆☆", page)
        self.assertNotIn("Долго ждал оператора", page)
        self.assertIn("с правом на клиентов", page)

    def test_notice_switches_are_on_the_notices_page(self):
        page = self.get_ok("/notices")
        self.assertIn("«Как вам аренда?» после сдачи", page)
        self.assertIn("Низкая оценка аренды", page)


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()


# ─────────────────────────── настоящий Postgres ───────────────────────────


@unittest.skipUnless(HAVE_PG and HAVE_WEB, "pgserver или asyncpg не установлены")
class TestFeedbackOnPostgres(unittest.IsolatedAsyncioTestCase):
    """Уникальный индекс, «один ответ» и окно сигнала - на настоящей базе:
    заглушка держит их проверками на Python, база - индексом и условием."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pg = pgserver.get_server(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(self.pg.get_uri(), min_size=1, max_size=3,
                                              init=_init_connection)
        await self.pool.execute("drop schema if exists crm cascade; "
                                "drop schema if exists bot cascade")
        db = Database(self.pool)
        await db.apply_schema(SCHEMA)
        await db.apply_schema(SCHEMA, force=True)
        self.crm = CrmDB(self.pool)
        self.client_id = await self.crm.create_client(
            full_name="Иванов Иван", phone="+79990000000", tg_id=5001)
        await self.crm.update_client(self.client_id, max_id=777)
        self.bike_id = await self.crm.create_bike(code="МБ-7", model="Kugoo V3",
                                                  location="Павлюхина")
        self.rid = await self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=None,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="manual",
            started_on=WEEK_AGO, contract_no=None, created_by="t")

    async def asyncTearDown(self):
        await self.pool.close()

    async def test_queue_answer_comment_alert(self):
        await service.close_rental(self.crm, await self.crm.rental(self.rid),
                                   closed_on=date.today(), note=None, by="staff:t")
        rental = await self.crm.rental(self.rid)
        self.assertIsNotNone(rental["closed_at"], "момент закрытия записан")
        self.assertFalse(await self.crm.queue_feedback(self.rid, self.client_id))
        queue = await self.crm.feedback_queue()
        self.assertEqual([r["rental_id"] for r in queue], [self.rid])
        self.assertEqual((queue[0]["tg_id"], queue[0]["max_id"], queue[0]["location"]),
                         (5001, 777, "Павлюхина"))
        self.assertFalse(await self.crm.answer_feedback(self.rid, 5), "не спрошенная")
        self.assertTrue(await self.crm.mark_feedback_asked(queue[0]["id"], channel="tg"))
        self.assertFalse(await self.crm.mark_feedback_asked(queue[0]["id"], channel="tg"))
        self.assertEqual(await self.crm.feedback_queue(), [])
        row = await service.rate_rental(self.crm, self.rid, 2, channel="tg", user_id=5001)
        self.assertFalse(await self.crm.answer_feedback(self.rid, 5), "один ответ")
        await self.crm.set_feedback_prompt(row["id"], "1001")
        self.assertIsNone(await self.crm.feedback_by_prompt("tg", "1001", 9999))
        self.assertIsNone(await self.crm.feedback_by_prompt("max", "1001", 777),
                          "канал другой")
        found = await self.crm.feedback_by_prompt("tg", "1001", 5001)
        self.assertEqual(found["rental_id"], self.rid)
        self.assertEqual(await self.crm.feedback_to_alert(low=3, wait_minutes=10), [])
        await self.pool.execute("update crm.feedback set answered_at = now() - "
                                "interval '11 minutes'")
        alert = await self.crm.feedback_to_alert(low=3, wait_minutes=10)
        self.assertEqual([r["id"] for r in alert], [row["id"]])
        self.assertTrue(await self.crm.comment_feedback(row["id"], "долго"))
        self.assertFalse(await self.crm.comment_feedback(row["id"], "ещё"))
        self.assertTrue(await self.crm.mark_feedback_alerted(row["id"]))
        self.assertFalse(await self.crm.mark_feedback_alerted(row["id"]))
        self.assertEqual(await self.crm.feedback_to_alert(low=3, wait_minutes=10), [])
        rows = await self.crm.feedback_rows(date.today() - timedelta(days=1))
        self.assertEqual([(r["score"], r["comment"]) for r in rows], [(2, "долго")])
        self.assertEqual(len(await self.crm.client_feedback(self.client_id)), 1)
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.pool.execute("update crm.feedback set score = 6")
        # Срок комментария: свежий не трогаем, старый стираем, оценка
        # остаётся, и поздний ответ на ту же просьбу его не возвращает.
        self.assertEqual(await self.crm.purge_feedback_comments(90), 0)
        await self.pool.execute("update crm.feedback set commented_at = now() - "
                                "interval '91 days'")
        self.assertEqual(await self.crm.purge_feedback_comments(90), 1)
        self.assertEqual(await self.crm.purge_feedback_comments(90), 0)
        row = await self.crm.feedback_of_rental(self.rid)
        self.assertEqual((row["score"], row["comment"]), (2, None))
        self.assertFalse(await self.crm.comment_feedback(row["id"], "поздний"))

    async def test_last_closed_rental_is_the_latest_closure(self):
        await service.close_rental(self.crm, await self.crm.rental(self.rid),
                                   closed_on=date.today(), note=None, by="t")
        second = await self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=None,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="manual",
            started_on=WEEK_AGO, contract_no=None, created_by="t")
        await service.close_rental(self.crm, await self.crm.rental(second),
                                   closed_on=date.today() - timedelta(days=3), note=None,
                                   by="t")
        last = await self.crm.last_closed_rental_of(self.client_id)
        self.assertEqual(last["id"], second, "по моменту закрытия, а не по дате возврата")
