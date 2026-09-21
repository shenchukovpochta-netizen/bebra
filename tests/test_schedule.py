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


if __name__ == "__main__":
    unittest.main()
