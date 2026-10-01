"""Дневной проход по расписанию: у каждого уведомления свой час.

Тесты остальных проходов зовут `run_daily` без расписания - в ручном
режиме он делает всё включённое сразу. Ровно поэтому две ошибки жили
незамеченными: сводка по оплатам не уходила никогда, а напоминание
«истекает через N дней» уходило в час просрочки. Здесь проход гоняется
так, как он работает на сервере: с часами и с памятью «что уже сегодня».
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_crm import FakeCrm  # noqa: E402

from app.crm import billing  # noqa: E402

D = Decimal


def run(coro):
    return asyncio.run(coro)


class Bot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))


class BotDB:
    async def get_user(self, tg_id):
        return None


CHAT = -100500


class ScheduleCase(unittest.TestCase):
    """Один клиент с арендой и общий проход суток по часам."""

    def setUp(self):
        self.crm = FakeCrm()
        self.bot = Bot()
        self.db = BotDB()
        self.cfg = types.SimpleNamespace(contract_chat_id=CHAT, remind_before_days=2)
        self.today = date(2026, 9, 21)
        self.client_id = run(self.crm.create_client(
            full_name="Иванов Иван", phone="+79990000000", tg_id=5001))
        self.bike_id = run(self.crm.create_bike(code="B-1", model="Kugoo"))
        self.tariff_id = run(self.crm.create_tariff("Неделя", 7, D(3000), None))

    def rental(self, *, billed_offset: int = 0, balance: D | None = None) -> int:
        rental_id = run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="auto",
            started_on=self.today - timedelta(days=7), contract_no="АВ-1",
            created_by="t"))
        run(self.crm.update_rental(rental_id,
                                   billed_until=self.today + timedelta(days=billed_offset)))
        if balance is not None:
            run(self.crm.add_ledger(client_id=self.client_id, rental_id=rental_id,
                                    kind="charge", amount=-balance, method=None,
                                    note="долг", created_by="t"))
        return rental_id

    def pass_at(self, hour: int, done: dict) -> None:
        now = datetime.combine(self.today, datetime.min.time()).replace(hour=hour,
                                                                        minute=5)
        run(billing.run_daily(self.bot, self.db, self.crm, self.cfg,
                              today=self.today, now=now, done=done))

    def to_client(self) -> list[str]:
        return [t for c, t in self.bot.sent if c == 5001]

    def to_chat(self) -> list[str]:
        return [t for c, t in self.bot.sent if c == CHAT]


class TestDigestReachesTheChat(ScheduleCase):
    def test_digest_goes_out_in_its_own_slot(self):
        """Сводка собиралась только в проходе напоминаний, а уходить должна
        была в свой час - и не уходила никогда, кроме как после вечернего
        перезапуска бота."""
        self.rental(billed_offset=-3, balance=D(6000))
        done: dict = {}
        for hour in (8, 9, 10, 14, 20, 23):
            self.pass_at(hour, done)
        digests = [t for t in self.to_chat() if "Сводка по оплатам" in t]
        self.assertEqual(len(digests), 1, "сводка уходит ровно один раз за сутки")
        self.assertIn("Иванов Иван", digests[0])
        self.assertEqual(done["daily_digest"], self.today)

    def test_digest_is_silent_when_everyone_paid(self):
        """Молчание означает «всё оплачено»: пустая сводка не уходит."""
        self.rental(billed_offset=30)
        done: dict = {}
        self.pass_at(20, done)
        self.assertFalse([t for t in self.to_chat() if "Сводка по оплатам" in t])

    def test_switched_off_digest_stays_off(self):
        self.rental(billed_offset=-3, balance=D(6000))
        run(self.crm.set_notice("daily_digest", enabled=False, at_hour=20,
                                at_minute=0, chat_id=None, by="t"))
        done: dict = {}
        for hour in (8, 20, 23):
            self.pass_at(hour, done)
        self.assertFalse([t for t in self.to_chat() if "Сводка по оплатам" in t])


class TestReminderHours(ScheduleCase):
    def test_each_reminder_waits_for_its_own_hour(self):
        """«Истекает через N дней» стоит на 14:00 и в 08:00 уходить не должно:
        раньше проход слал все три вида в час самого раннего."""
        self.rental(billed_offset=2)
        done: dict = {}
        self.pass_at(8, done)
        self.assertEqual(self.to_client(), [], "в восемь утра его час ещё не настал")
        self.assertNotIn("rent_soon", done)

        self.pass_at(14, done)
        texts_now = self.to_client()
        self.assertEqual(len(texts_now), 1)
        self.assertIn("заканчивается", texts_now[0])
        self.assertEqual(done["rent_soon"], self.today)

        self.pass_at(20, done)
        self.assertEqual(len(self.to_client()), 1, "второй раз за сутки не уходит")

    def test_overdue_goes_in_the_morning(self):
        # Просрочка напоминается на первые, третьи и седьмые сутки:
        # долг в один период сдвигает «оплачено до» на неделю назад.
        self.rental(billed_offset=6, balance=D(3000))
        done: dict = {}
        self.pass_at(8, done)
        self.assertTrue(any("не оплачена" in t for t in self.to_client()),
                        self.to_client())
        self.assertEqual(done["rent_overdue"], self.today)

    def test_manual_pass_sends_everything_at_once(self):
        """Ручной проход из панели и из тестов расписания не знает."""
        self.rental(billed_offset=2)
        run(billing.run_daily(self.bot, self.db, self.crm, self.cfg, today=self.today))
        self.assertEqual(len(self.to_client()), 1)
        self.assertTrue([t for t in self.to_chat() if "Сводка по оплатам" in t])


class TestChargeMarkAfterWork(ScheduleCase):
    def test_failed_charge_is_retried_on_the_next_round(self):
        """Отметка «начислено» ставится после прохода: сбой базы в назначенный
        час иначе оставлял бы парк без начислений до следующих суток."""
        self.rental(billed_offset=-7)      # период, который пора начислить
        done: dict = {}
        broken = self.crm.charge_period

        async def fail(*a, **kw):
            raise RuntimeError("база недоступна")

        self.crm.charge_period = fail
        self.pass_at(8, done)
        self.assertNotIn("charge", done, "сбойный проход сделанным не считается")

        self.crm.charge_period = broken
        self.pass_at(8, done)
        self.assertEqual(done["charge"], self.today)
        charges = [x for x in self.crm.ledger_ if x["kind"] == "charge"]
        self.assertTrue(charges, "начисление догналось следующим кругом")


class TestReminderMarkAfterWork(ScheduleCase):
    def test_failed_reminder_pass_is_retried_on_the_next_round(self):
        """Отметка rent_* ставилась до прохода: сбой базы посреди него
        съедал напоминания до завтра."""
        self.rental(billed_offset=6, balance=D(3000))      # просрочка
        done: dict = {}
        alive = self.crm.active_rentals

        async def fail(*a, **kw):
            raise RuntimeError("база недоступна")

        self.crm.active_rentals = fail
        self.pass_at(8, done)
        self.assertNotIn("rent_overdue", done, "сбойный проход сделанным не считается")
        self.assertEqual(self.to_client(), [])

        self.crm.active_rentals = alive
        self.pass_at(8, done)
        self.assertTrue(any("не оплачена" in t for t in self.to_client()),
                        "напоминание догналось следующим кругом")
        self.assertEqual(done["rent_overdue"], self.today)
        self.pass_at(9, done)
        self.assertEqual(len(self.to_client()), 1, "второй раз за сутки не уходит")


class TestPromoWaitsForTheDay(ScheduleCase):
    """Начисление - первым кругом после полуночи, и «скидка по акции»
    уходила клиенту в 00:05. Теперь деньги - ночью, сообщение - утром."""

    def setUp(self):
        super().setUp()
        run(self.crm.create_promo(
            kind="season", title="Сезонная", percent=10, amount=None, code=None,
            params={}, starts_on=None, ends_on=None, max_uses=None,
            once_per_client=False, text=None, note=None, by="t"))

    def promo_texts(self) -> list[str]:
        return [t for t in self.to_client() if "Сезонная" in t]

    def test_night_charge_tells_the_client_in_the_morning_once(self):
        self.rental(billed_offset=0)              # период начинается сегодня
        done: dict = {}
        self.pass_at(0, done)
        self.assertEqual(done["charge"], self.today, "деньги - ночью, как раньше")
        bonuses = [x for x in self.crm.ledger_ if x["kind"] == "bonus"]
        self.assertEqual([x["amount"] for x in bonuses], [D(300)])
        self.assertEqual(self.promo_texts(), [], "в 00:05 клиента не будим")
        self.assertIn(billing.PROMO_QUEUE_KEY, self.crm.settings_)

        self.pass_at(8, done)
        self.assertEqual(self.promo_texts(), [], "до девяти - ещё ночь")
        self.pass_at(9, done)
        self.assertEqual(len(self.promo_texts()), 1, self.to_client())
        self.assertIn("300", self.promo_texts()[0])
        for hour in (10, 14, 20):
            self.pass_at(hour, done)
        self.assertEqual(len(self.promo_texts()), 1, "второй раз не уходит")
        self.assertEqual(run(self.crm.settings())[billing.PROMO_QUEUE_KEY], "[]")

    def test_queue_survives_a_restart(self):
        """Очередь - в crm.settings: бот, перезапущенный до утра, начинает
        с пустой памятью круга, а скидку всё равно сообщает."""
        self.rental(billed_offset=0)
        self.pass_at(0, {})
        self.pass_at(9, {"charge": self.today})       # новая память круга
        self.assertEqual(len(self.promo_texts()), 1)

    def test_daytime_charge_tells_at_once(self):
        """Бот лежал всю ночь и поднялся днём: начисление и сообщение -
        тем же кругом, очередь не нужна."""
        self.rental(billed_offset=0)
        self.pass_at(11, {})
        self.assertEqual(len(self.promo_texts()), 1)
        self.assertNotIn(billing.PROMO_QUEUE_KEY, self.crm.settings_)

    def test_junk_in_the_queue_does_not_break_the_pass(self):
        self.crm.settings_[billing.PROMO_QUEUE_KEY] = '[{"client_id": "x"}, 5, "мусор"'
        done: dict = {}
        self.pass_at(9, done)
        self.assertEqual(self.promo_texts(), [])


class TestEveningCutoff(ScheduleCase):
    """Бот, поднятый в 23:30 после простоя, догонял «в этот час или позже»
    и слал клиенту напоминание об оплате на ночь глядя."""

    def test_client_reminder_waits_for_tomorrow_team_digest_does_not(self):
        self.rental(billed_offset=7, balance=D(3000))      # сегодня последний день
        done: dict = {}
        self.pass_at(23, done)
        self.assertEqual(self.to_client(), [], "клиенту в 23:05 не пишем")
        self.assertNotIn("rent_due", done, "отметки нет - завтра уйдёт в свой час")
        self.assertTrue([t for t in self.to_chat() if "Сводка по оплатам" in t],
                        "команде - без вечерней границы")
        tomorrow = self.today + timedelta(days=1)
        now = datetime.combine(tomorrow, datetime.min.time()).replace(hour=8, minute=5)
        run(billing.run_daily(self.bot, self.db, self.crm, self.cfg,
                              today=tomorrow, now=now, done=done))
        self.assertTrue(any("не оплачена" in t for t in self.to_client()),
                        self.to_client())

    def test_bot_pass_and_autocharge_stop_in_the_evening(self):
        from zoneinfo import ZoneInfo

        from app import tasks
        from app.crm import logic
        msk = ZoneInfo("Europe/Moscow")
        late = datetime(2026, 9, 21, 20, 30, tzinfo=msk)          # 20:30 МСК
        self.assertTrue(tasks.due_today(late, None, 7, tz=msk))
        night = datetime(2026, 9, 21, 21, 30, tzinfo=msk)         # 21:30 МСК
        self.assertFalse(tasks.due_today(night, None, 7, tz=msk),
                         "проход бота шлёт клиентам - не позже вечера")
        # Час, поставленный на вечер, догоняется в пределах своего часа.
        self.assertTrue(tasks.due_today(datetime(2026, 9, 21, 22, 30, tzinfo=msk),
                                        None, 19, tz=msk))
        self.assertFalse(tasks.due_today(datetime(2026, 9, 21, 23, 30, tzinfo=msk),
                                         None, 19, tz=msk))
        settings = {"autocharge_hour": "12"}
        self.assertTrue(logic.autocharge_time(settings, datetime(2026, 9, 21, 20, 59)))
        self.assertFalse(logic.autocharge_time(settings, datetime(2026, 9, 21, 23, 30)),
                         "списание с сообщением клиенту - не ночью")


class _TrackedDB:
    """База бота, у которой идёт своя аренда клиента 5001."""

    def __init__(self, tg_ids=(5001,)) -> None:
        self.tg_ids = list(tg_ids)
        self.reads = 0

    async def active_rentals(self):
        self.reads += 1
        return [{"tg_id": t} for t in self.tg_ids]

    async def get_user(self, tg_id):
        return None


class TestOneReminderStream(ScheduleCase):
    """Аренде, которую завёл бот, напоминали двое: бот по rent_until и CRM
    по балансу. Теперь о ручной аренде клиента из бота напоминает бот."""

    def manual(self) -> int:
        rental_id = self.rental(billed_offset=6, balance=D(3000))   # просрочка
        run(self.crm.update_rental(rental_id, billing="manual"))
        return rental_id

    def test_bot_rental_is_reminded_by_the_bot_only(self):
        rental_id = self.manual()
        self.db = _TrackedDB()
        done: dict = {}
        self.pass_at(8, done)
        self.assertEqual(self.to_client(), [], "напоминает бот, CRM молчит")
        self.assertIsNone(run(self.crm.rental(rental_id)).get("notified_on"),
                          "отметку не ставим: молчим вместо бота, а не за клиента")
        self.assertEqual(done["rent_overdue"], self.today)

    def test_panel_manual_rental_without_bot_rental_is_still_reminded(self):
        self.manual()
        self.db = _TrackedDB(tg_ids=())
        self.pass_at(8, {})
        self.assertTrue(any("не оплачена" in t for t in self.to_client()))

    def test_auto_rental_is_reminded_by_crm(self):
        self.rental(billed_offset=6, balance=D(3000))
        self.db = _TrackedDB()
        self.pass_at(8, {})
        self.assertTrue(any("не оплачена" in t for t in self.to_client()))
        self.assertEqual(self.db.reads, 0, "аренды бота читаются, только если есть ручная")

    def test_unreadable_bot_rentals_mean_crm_reminds(self):
        """Лучше два напоминания, чем ни одного."""
        self.manual()

        class Broken(_TrackedDB):
            async def active_rentals(self):
                raise RuntimeError("база бота недоступна")

        self.db = Broken()
        with self.assertLogs("app.crm.billing", "WARNING"):
            self.pass_at(8, {})
        self.assertTrue(any("не оплачена" in t for t in self.to_client()))


class _LoopDB:
    """База бота для круга напоминаний: считает проходы самого бота."""

    def __init__(self) -> None:
        self.passes = 0

    async def active_rentals(self):
        self.passes += 1
        return []

    async def get_user(self, tg_id):
        return None


class TestMemorySurvivesRestart(ScheduleCase):
    """Память «что сегодня уже делали» жила в переменных цикла: бот,
    перезапущенный после часа сводки, слал её второй раз."""

    def loop_round(self, db, *, hour: int) -> None:
        from app import tasks
        from app.crm import waitlist
        local = datetime.combine(self.today, datetime.min.time()).replace(hour=hour,
                                                                          minute=5)

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return local if tz is None else local.astimezone(tz)

        async def stop(_):
            raise asyncio.CancelledError

        cfg = types.SimpleNamespace(contract_chat_id=CHAT, remind_before_days=2,
                                    remind_hour_utc=0)
        with mock.patch.object(tasks, "datetime", Frozen), \
                mock.patch.object(waitlist, "run_once", mock.AsyncMock(return_value=0)), \
                mock.patch.object(tasks.asyncio, "sleep", stop), \
                self.assertRaises(asyncio.CancelledError):
            run(tasks.reminders_loop(self.bot, db, cfg, None, self.crm))

    def test_restart_after_the_digest_hour_does_not_repeat_it(self):
        from app import tasks
        self.rental(billed_offset=-3, balance=D(6000))
        db = _LoopDB()
        self.loop_round(db, hour=20)
        digests = [t for t in self.to_chat() if "Сводка по оплатам" in t]
        self.assertEqual(len(digests), 1)
        self.assertEqual(db.passes, 1)
        memory = tasks.parse_done(self.crm.settings_[tasks.DONE_KEY])
        self.assertEqual(memory["daily_digest"], self.today)
        self.assertIn(tasks.BOT_MARK, memory)

        self.loop_round(db, hour=21)                 # перезапуск бота
        digests = [t for t in self.to_chat() if "Сводка по оплатам" in t]
        self.assertEqual(len(digests), 1, "сводка после перезапуска второй раз не уходит")
        self.assertEqual(db.passes, 1, "проход бота после перезапуска не повторяется")

    def test_unreadable_memory_holds_the_pass(self):
        """Память не прочитана - проход ждёт: с пустой сводки ушли бы снова."""
        self.rental(billed_offset=-3, balance=D(6000))

        async def fail():
            raise RuntimeError("база недоступна")

        self.crm.settings = fail
        db = _LoopDB()
        with self.assertLogs("app.tasks", "ERROR"):
            self.loop_round(db, hour=20)
        self.assertEqual(self.to_chat(), [])
        self.assertEqual(db.passes, 0)


if __name__ == "__main__":
    unittest.main()
