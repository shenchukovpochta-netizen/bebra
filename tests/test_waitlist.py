"""Лист ожидания: освободилась модель заявки на её точке - бот зовёт
клиента, а велосипед при этом не бронируется.

Стерегут здесь очередь (давние первыми, не больше N на велосипед, раз в
сутки на заявку, про тот же велосипед - один раз, поданные после
освобождения не ждали), ночь (не пишем), повтор круга и перезапуск
(второго сообщения нет), недоставку (место - следующему), кнопку «Беру»
(чужая, старая, битая - «устарела», второе нажатие не шлёт команде вторую
карточку), строку в списке заявок и параметры уведомления в панели.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    from aiogram.methods import AnswerCallbackQuery, SendMessage

    from app import tasks, texts
    from app.crm import waitlist
    from tests.fake_crm import FakeCrm
    from tests.test_cabinet import CabinetCase
    from tests.test_flow import ADMIN_CHAT, USER_ID, cb
    HAVE_AIOGRAM = True
except ImportError:                                    # pragma: no cover
    HAVE_AIOGRAM = False
    CabinetCase = unittest.IsolatedAsyncioTestCase    # type: ignore[misc,assignment]

try:
    import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal
TODAY = date(2026, 9, 27)
T0 = datetime(2026, 9, 27, 7, 0, tzinfo=UTC)


def _run(coro):
    return asyncio.run(coro)


class TestWaitlistLogic(unittest.TestCase):
    def bike(self, **over):
        return {"id": 5, "code": "B-5", "model": "Maikaolin H10", "location": "Павлюхина",
                "freed_at": T0, **over}

    def booking(self, bid, **over):
        return {"id": bid, "client_id": bid, "model": "Городской H10", "status": "new",
                "location_id": 1, "location_name": "Павлюхина",
                "created_at": T0 - timedelta(hours=bid), "waitlist_at": None,
                "waitlist_bike_id": None, "coming_at": None, **over}

    ALIASES = {"maikaolin h10": "Городской H10", "городской h10": "Городской H10"}

    def test_day_window_comes_from_the_notice(self):
        self.assertTrue(logic.waitlist_hours_ok({}, datetime(2026, 9, 27, 9, 0)))
        self.assertFalse(logic.waitlist_hours_ok({}, datetime(2026, 9, 27, 18, 0)),
                         "в 18 уже поздно звать «сегодня»")
        self.assertFalse(logic.waitlist_hours_ok({}, datetime(2026, 9, 27, 2, 0)))
        late = {"extra": {"from_hour": 10, "to_hour": 21}}
        self.assertTrue(logic.waitlist_hours_ok(late, datetime(2026, 9, 27, 20, 30)))
        self.assertFalse(logic.waitlist_hours_ok(late, datetime(2026, 9, 27, 9, 30)))

    def test_fits_by_catalogue_name_and_point(self):
        bike = self.bike()
        self.assertTrue(logic.waitlist_fits(bike, self.booking(1), aliases=self.ALIASES),
                        "заводское имя парка = название каталога в заявке")
        self.assertFalse(logic.waitlist_fits(bike, self.booking(1, location_name="Адоратского",
                                                                location_id=2),
                                             aliases=self.ALIASES))
        self.assertTrue(logic.waitlist_fits(bike, self.booking(1, location_id=None,
                                                               location_name=None),
                                            aliases=self.ALIASES),
                        "заявка без точки (точка была одна) ждёт на любой")
        self.assertFalse(logic.waitlist_fits(bike, self.booking(1, model="Truck+"),
                                             aliases=self.ALIASES))
        self.assertTrue(logic.waitlist_fits(bike, self.booking(1, model=None),
                                            aliases=self.ALIASES))
        self.assertFalse(logic.waitlist_fits({**bike, "status": "rented"}, self.booking(1),
                                             aliases=self.ALIASES))
        self.assertFalse(logic.waitlist_fits({**bike, "location": None}, self.booking(1),
                                             aliases=self.ALIASES),
                         "велосипед «не на точке» на точке не ждёт")

    def test_queue_goes_oldest_first_and_skips_who_did_not_wait(self):
        bike = self.bike()
        rows = [self.booking(3), self.booking(1), self.booking(2),
                # подана после освобождения - клиент видел «свободно»
                self.booking(4, created_at=T0 + timedelta(minutes=5)),
                self.booking(5, status="cancelled"),
                # звали сегодня к другому велосипеду
                self.booking(6, waitlist_at=datetime(2026, 9, 27, 8, 0, tzinfo=UTC),
                             waitlist_bike_id=77)]
        got = logic.waitlist_queue(bike, rows, today=TODAY, aliases=self.ALIASES)
        self.assertEqual([b["id"] for b in got], [1, 2, 3])

    def test_forgotten_booking_is_not_called(self):
        """Заявка, чей день прошёл больше трёх суток назад, - забытая строка,
        а не ожидание: её не зовут и места в очереди она не занимает."""
        bike = self.bike()
        rows = [self.booking(1, wanted_on=TODAY - timedelta(days=logic.WAITLIST_STALE_DAYS + 1)),
                self.booking(2, wanted_on=TODAY - timedelta(days=logic.WAITLIST_STALE_DAYS)),
                self.booking(3, wanted_on=TODAY + timedelta(days=1)),
                self.booking(4, wanted_on=None)]
        got = logic.waitlist_queue(bike, rows, today=TODAY, aliases=self.ALIASES)
        self.assertEqual([b["id"] for b in got], [2, 3, 4])

    def test_coming_client_is_on_today_dashboard(self):
        """Заявка на завтра, а клиент на сегодняшний зов ответил «приеду
        сегодня» - на сводке он в горящих «на выдачу сегодня», а не в
        «ближайших днях». Ответ на вчерашний зов сегодня ничего не значит."""
        called = datetime(2026, 9, 27, 12, 0).astimezone()
        row = self.booking(1, full_name="Едет Сегодня", wanted_on=TODAY + timedelta(days=1),
                           waitlist_at=called, waitlist_bike_id=5,
                           coming_at=called + timedelta(minutes=7))
        waiting = self.booking(2, full_name="Ждёт Завтра", wanted_on=TODAY + timedelta(days=1))
        yesterday = self.booking(3, full_name="Ответил Вчера",
                                 wanted_on=TODAY + timedelta(days=1),
                                 waitlist_at=called - timedelta(days=1), waitlist_bike_id=5,
                                 coming_at=called - timedelta(days=1, minutes=-5))
        tasks = {t["code"]: t for t in logic.today_tasks(
            bookings=[row, waiting, yesterday], today=TODAY)}
        self.assertEqual(tasks["bookings"]["level"], "hot")
        self.assertEqual(tasks["bookings"]["names"], ["Едет Сегодня"])
        self.assertEqual(tasks["bookings_later"]["names"], ["Ждёт Завтра", "Ответил Вчера"])
        self.assertTrue(logic.waitlist_coming_today(row, today=TODAY))
        self.assertFalse(logic.waitlist_coming_today(
            {**row, "coming_at": called - timedelta(minutes=1)}, today=TODAY),
            "ответ до зова - ответ на прошлый зов")

    def test_same_bike_is_news_only_once(self):
        """Назавтра тот же велосипед - уже не новость: звать про него второй
        раз - спам, даже если «раз в сутки» позволяет."""
        bike = self.bike()
        called = self.booking(1, waitlist_at=T0 + timedelta(minutes=1), waitlist_bike_id=5)
        tomorrow = TODAY + timedelta(days=1)
        self.assertEqual(logic.waitlist_queue(bike, [called], today=tomorrow,
                                              aliases=self.ALIASES), [])
        self.assertEqual(logic.waitlist_taken(bike, [called]), 1)
        # велосипед освободился снова (вернули после новой аренды) - снова новость
        again = self.bike(freed_at=T0 + timedelta(days=1))
        self.assertEqual(logic.waitlist_taken(again, [called]), 0)
        self.assertEqual(len(logic.waitlist_queue(again, [called], today=tomorrow,
                                                  aliases=self.ALIASES)), 1)

    def test_taken_counts_only_open_bookings(self):
        bike = self.bike()
        rows = [self.booking(1, waitlist_at=T0 + timedelta(minutes=1), waitlist_bike_id=5),
                self.booking(2, waitlist_at=T0 + timedelta(minutes=1), waitlist_bike_id=5,
                             status="cancelled"),
                # недоставленное место не занимает
                self.booking(3, waitlist_at=T0 + timedelta(minutes=1), waitlist_bike_id=None),
                # звали до освобождения - это прошлое событие
                self.booking(4, waitlist_at=T0 - timedelta(minutes=1), waitlist_bike_id=5)]
        self.assertEqual(logic.waitlist_taken(bike, rows), 1)

    def test_booking_served_by_a_later_rental(self):
        booking = self.booking(1)
        self.assertFalse(logic.booking_served(booking, []))
        self.assertFalse(logic.booking_served(
            booking, [{"created_at": booking["created_at"] - timedelta(days=9)}]),
            "прошлая аренда до подачи - заявка ждёт")
        self.assertTrue(logic.booking_served(booking, [{"created_at": T0}]))
        self.assertFalse(logic.booking_served({**booking, "created_at": None},
                                              [{"created_at": T0}]))

    def test_note_for_the_booking_list(self):
        at = datetime(2026, 9, 27, 14, 5).astimezone()
        row = {"waitlist_at": at, "waitlist_bike_id": 5,
               "coming_at": at + timedelta(minutes=7)}
        self.assertEqual(logic.waitlist_note(row, today=TODAY),
                         "уведомлён 14:05 · ответил 14:12")
        self.assertEqual(logic.waitlist_note({**row, "coming_at": None}, today=TODAY),
                         "уведомлён 14:05")
        self.assertEqual(logic.waitlist_note({**row, "coming_at": None,
                                              "waitlist_bike_id": None}, today=TODAY),
                         "не дошло 14:05")
        self.assertEqual(logic.waitlist_note(row, today=TODAY + timedelta(days=1)),
                         "уведомлён 27.09 14:05 · ответил 27.09 14:12")
        # ответ на прошлое приглашение к новому не относится
        old = {**row, "coming_at": at - timedelta(days=1)}
        self.assertEqual(logic.waitlist_note(old, today=TODAY), "уведомлён 14:05")
        self.assertEqual(logic.waitlist_note({}, today=TODAY), "")

    def test_catalog_and_param_labels(self):
        for code in ("waitlist", "waitlist_coming", "battery_request", "card_nudge"):
            self.assertIn(code, logic.NOTICES)
        self.assertEqual(logic.NOTICES["waitlist"]["group"], "client")
        self.assertIsNone(logic.NOTICES["waitlist"]["hour"], "событийное, час - окно")
        self.assertEqual(logic.NOTICES["waitlist_coming"]["target"], "chat")
        # у каждого параметра каталога есть подпись и границы в панели
        for code, item in logic.NOTICES.items():
            for key, value in (item.get("params") or {}).items():
                with self.subTest(code, key=key):
                    self.assertIn(key, logic.NOTICE_PARAMS)
                    least, limit = logic.NOTICE_PARAMS[key][3:]
                    self.assertTrue(least <= value <= limit)


class _Bot:
    """Бот без сети: копит (чат, текст, клавиатура); fail_for - кто
    заблокировал бота."""

    def __init__(self, fail_for=()):
        self.sent: list[tuple] = []
        self.fail_for = set(fail_for)

    async def send_message(self, chat_id, text, reply_markup=None, **_):
        if chat_id in self.fail_for:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.sent.append((chat_id, text, reply_markup))


class _DB:
    async def get_user(self, tg_id):
        return None


def _noon() -> datetime:
    """Полдень сегодня по местным часам - внутри окна по умолчанию."""
    return datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestWaitlistRound(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.crm = FakeCrm()
        crm = self.crm
        self.p1 = await crm.create_location(name="Павлюхина", city="Казань", address=None,
                                            note=None, hours="10:00-19:00")
        self.p2 = await crm.create_location(name="Адоратского", city="Казань", address=None,
                                            note=None)
        await crm.create_bike_model(title="Городской H10", brand="M",
                                    factory_title="Maikaolin H10", battery_slots=1, note=None)
        self.tariff = await crm.create_tariff("Неделя", 7, D(3000), None)
        self.bike = await crm.create_bike(code="B-1", model="Maikaolin H10", status="repair",
                                          location="Павлюхина")
        self.clients = {}
        self.bookings = {}
        for n, (name, place) in enumerate((("Первый", self.p1), ("Второй", self.p1),
                                           ("Третий", self.p1), ("Чужая точка", self.p2)),
                                          start=1):
            cid = await crm.create_client(full_name=name, phone=f"+7999000000{n}",
                                          tg_id=100 + n)
            bid = await crm.create_booking(client_id=cid, model="Городской H10",
                                           tariff_id=self.tariff, location_id=place,
                                           wanted_on=date.today())
            # поданы раньше, чем велосипед освободится
            crm.bookings_[bid]["created_at"] -= timedelta(hours=5 - n)
            self.clients[n], self.bookings[n] = cid, bid
        self.bot = _Bot()

    async def free(self, bike_id=None, **fields):
        await self.crm.update_bike(bike_id or self.bike, status="available", by="t", **fields)

    async def round(self, now=None, bot=None):
        return await waitlist.run_once(bot or self.bot, _DB(), self.crm, now=now or _noon())

    def called(self):
        return [chat for chat, _, _ in self.bot.sent]

    async def test_freed_bike_calls_the_oldest_two_at_its_point(self):
        self.assertEqual(await self.round(), 0, "пока в ремонте - звать не к чему")
        await self.free()
        self.assertEqual(await self.round(), 2)
        self.assertEqual(self.called(), [101, 102],
                         "двое по умолчанию, давние первыми, другая точка молчит")
        chat, text, markup = self.bot.sent[0]
        self.assertIn("Освободился <b>Городской H10</b>", text, "имя каталога, не накладной")
        self.assertIn("не бронируется", text)
        self.assertIn("10:00-19:00", text, "часы той точки, где велосипед")
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(data, [f"wl:{self.bookings[1]}:{self.bike}", "cab:book:cancel"])
        self.assertLessEqual(max(len(x.encode()) for x in data), 64)
        booking = await self.crm.booking(self.bookings[1])
        self.assertEqual(booking["waitlist_bike_id"], self.bike)
        self.assertIsNotNone(booking["waitlist_at"])
        # велосипед заявке не отдан: статус тот же, заявка открыта
        self.assertEqual((await self.crm.bike(self.bike))["status"], "available")
        self.assertEqual(booking["status"], "new")
        log = await self.crm.notice_log(code="waitlist")
        self.assertEqual(sorted(r["client_id"] for r in log),
                         sorted([self.clients[1], self.clients[2]]))

    async def test_forgotten_booking_gives_its_place_to_today(self):
        """Первая заявка забыта с прошлой недели: зовут второго и третьего,
        а не давнего, который давно передумал."""
        self.crm.bookings_[self.bookings[1]]["wanted_on"] = (
            date.today() - timedelta(days=logic.WAITLIST_STALE_DAYS + 4))
        await self.free()
        self.assertEqual(await self.round(), 2)
        self.assertEqual(self.called(), [102, 103])

    async def test_next_round_and_restart_send_nothing_new(self):
        await self.free()
        await self.round()
        self.bot.sent.clear()
        self.assertEqual(await self.round(), 0, "следующий круг")
        self.assertEqual(await waitlist.run_once(_Bot(), _DB(), self.crm, now=_noon()), 0,
                         "перезапуск бота: памяти круга нет, отметка - в базе")
        self.assertEqual(self.bot.sent, [])

    async def test_per_bike_limit_is_the_owner_setting(self):
        await self.crm.set_notice("waitlist", enabled=True, at_hour=None,
                                  extra={"per_bike": 1}, by="t")
        await self.free()
        await self.round()
        self.assertEqual(self.called(), [101])

    async def test_cancelled_booking_frees_its_place(self):
        await self.free()
        await self.round()
        await self.crm.update_booking(self.bookings[1], status="cancelled")
        self.bot.sent.clear()
        await self.round()
        self.assertEqual(self.called(), [103], "место снятой заявки - следующему")

    async def test_night_is_quiet_and_morning_catches_up(self):
        await self.free()
        night = datetime.now().replace(hour=23, minute=30, second=0, microsecond=0)
        self.assertEqual(await self.round(now=night), 0)
        self.assertEqual(await self.round(now=night.replace(hour=6)), 0)
        self.assertEqual(self.bot.sent, [])
        self.assertIsNone((await self.crm.booking(self.bookings[1]))["waitlist_at"],
                          "ночью ничего не отмечено - утром позовём")
        self.assertEqual(await self.round(now=night.replace(hour=9)), 2)

    async def test_switched_off_notice_sends_and_marks_nothing(self):
        await self.crm.set_notice("waitlist", enabled=False, at_hour=None, by="t")
        await self.free()
        self.assertEqual(await self.round(), 0)
        self.assertEqual(self.bot.sent, [])
        self.assertIsNone((await self.crm.booking(self.bookings[1]))["waitlist_at"])

    async def test_booking_placed_after_the_bike_was_free_did_not_wait(self):
        await self.free()
        for n in (1, 2, 3):
            self.crm.bookings_[self.bookings[n]]["created_at"] = datetime.now(UTC) + \
                timedelta(minutes=1)
        self.assertEqual(await self.round(), 0)

    async def test_renter_and_blacklisted_are_skipped(self):
        other = await self.crm.create_bike(code="B-9", model="Kugoo", status="available")
        await self.crm.create_rental(client_id=self.clients[1], bike_id=other,
                                     tariff_id=self.tariff, tariff_name="Неделя",
                                     period_days=7, price=D(3000), billing="auto",
                                     started_on=date.today(), contract_no=None,
                                     created_by="t")
        await self.crm.update_client(self.clients[2], status="blacklist")
        await self.free()
        await self.round()
        self.assertEqual(self.called(), [103],
                         "аренда идёт - не зовём; чёрный список - не зовём")

    async def test_booking_served_past_it_calls_nobody(self):
        """Старые данные: выдали мимо заявки (она осталась «новой»), клиент
        сдал велосипед - и тот освободился. Звать его к только что сданному
        велосипеду нельзя: заявку уже исполнила та аренда."""
        await self.free()
        rid = await self.crm.create_rental(
            client_id=self.clients[1], bike_id=self.bike, tariff_id=self.tariff,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="auto",
            started_on=date.today(), contract_no=None, created_by="t")
        await self.crm.close_rental(rid, closed_on=date.today(), note=None)
        self.assertEqual((await self.crm.booking(self.bookings[1]))["status"], "new")
        await self.round()
        self.assertEqual(self.called(), [102, 103])
        self.assertIsNone((await self.crm.booking(self.bookings[1]))["waitlist_at"])

    async def test_blocked_bot_gives_the_place_to_the_next(self):
        await self.free()
        bot = _Bot(fail_for={101})
        self.assertEqual(await self.round(bot=bot), 2)
        self.assertEqual([chat for chat, _, _ in bot.sent], [102, 103])
        first = await self.crm.booking(self.bookings[1])
        self.assertIsNone(first["waitlist_bike_id"], "место не занято недоставкой")
        self.assertIsNotNone(first["waitlist_at"], "сегодня его больше не зовём")
        log = await self.crm.notice_log(code="waitlist")
        self.assertEqual([r["status"] for r in log if r["client_id"] == self.clients[1]],
                         ["skipped"], "недоставка видна в истории отправок")
        again = _Bot()
        self.assertEqual(await self.round(bot=again), 0)

    async def test_transfer_to_the_point_is_a_freed_bike_there(self):
        """Переезд свободного велосипеда - тоже «освободился», на новой точке."""
        moved = await self.crm.create_bike(code="B-2", model="Maikaolin H10",
                                           status="available", location="Адоратского")
        # велосипед давно свободен: событие его заведения - за сутками
        for row in self.crm.status_log_ + self.crm.location_log_:
            if row["bike_id"] == moved:
                row["changed_at"] -= timedelta(days=3)
        self.assertEqual(await self.round(), 0)
        await self.crm.update_bike(moved, location="Павлюхина", by="t")
        await self.round()
        self.assertEqual(self.called(), [101, 102])

    async def test_one_client_is_called_once_a_day_even_for_two_bikes(self):
        second = await self.crm.create_bike(code="B-2", model="Maikaolin H10",
                                            status="repair", location="Павлюхина")
        await self.free()
        await self.free(second)
        await self.round()
        self.assertEqual(self.called(), [101, 102, 103],
                         "второй велосипед - следующим в очереди, не тем же дважды")
        # назавтра новый велосипед - первого снова можно позвать; вчерашние
        # события вместе со вчерашними приглашениями уходят в прошлое
        day = timedelta(days=1, hours=1)
        for n in (1, 2, 3):
            self.crm.bookings_[self.bookings[n]]["waitlist_at"] -= day
        for row in self.crm.status_log_ + self.crm.location_log_:
            row["changed_at"] -= day
        third = await self.crm.create_bike(code="B-3", model="Maikaolin H10",
                                           status="repair", location="Павлюхина")
        await self.free(third)
        self.bot.sent.clear()
        await self.round()
        self.assertEqual(self.called(), [101, 102])


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestWaitlistInTheLoop(unittest.IsolatedAsyncioTestCase):
    async def test_reminders_loop_runs_the_waitlist_every_round(self):
        """Сверка - в круге напоминаний, и её сбой проход не валит."""
        calls = []

        async def fake_run(bot, db, crm, *, now):
            calls.append(crm)
            raise RuntimeError("база недоступна")

        async def stop(_):
            raise asyncio.CancelledError

        cfg = mock.Mock(remind_hour_utc=24)
        # Память дневного прохода цикл читает из crm.settings до работы.
        crm = mock.Mock(settings=mock.AsyncMock(return_value={}),
                        set_setting=mock.AsyncMock())
        with mock.patch.object(waitlist, "run_once", fake_run), \
                mock.patch("app.crm.billing.run_daily", mock.AsyncMock()) as daily, \
                mock.patch.object(tasks.asyncio, "sleep", stop), \
                self.assertLogs("app.tasks", "ERROR") as logs:
            with self.assertRaises(asyncio.CancelledError):
                await tasks.reminders_loop(_Bot(), _DB(), cfg, None, crm)
        self.assertEqual(calls, [crm])
        daily.assert_awaited_once()
        self.assertTrue(any("лист ожидания" in line for line in logs.output))


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestTakeButton(CabinetCase):
    """«Беру — приеду сегодня»: отметка на заявке и карточка команде;
    велосипед не бронируется."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.approved_user()
        self.client = await self.crm_client(tg_id=USER_ID)
        await self.crm.create_bike_model(title="Kugoo V3", brand="K", factory_title=None,
                                         battery_slots=1, note=None)
        self.loc = await self.crm.create_location(name="Павлюхина", city="Казань",
                                                  address=None, note=None,
                                                  hours="10:00-19:00")
        self.tariff = await self.crm.create_tariff("Неделя", 7, D(3000), None)
        self.bike = await self.crm.create_bike(code="B-1", model="Kugoo V3",
                                               location="Павлюхина")
        self.booking = await self.crm.create_booking(
            client_id=self.client["id"], model="Kugoo V3", tariff_id=self.tariff,
            location_id=self.loc, wanted_on=date.today())
        await self.crm.mark_waitlist(self.booking, self.bike)

    def alerts(self):
        return [m.text for m in self.session.calls
                if isinstance(m, AnswerCallbackQuery) and m.show_alert]

    def team_cards(self):
        return [m.text for m in self.session.sent_to(ADMIN_CHAT)
                if isinstance(m, SendMessage) and "листа ожидания" in (m.text or "")]

    async def test_take_marks_the_booking_and_tells_the_team_once(self):
        await self.feed(cb(f"wl:{self.booking}:{self.bike}"))
        booking = await self.crm.booking(self.booking)
        self.assertIsNotNone(booking["coming_at"])
        self.assertEqual(booking["status"], "new", "заявка открыта до выдачи")
        self.assertEqual((await self.crm.bike(self.bike))["status"], "available",
                         "велосипед не бронируется")
        text = self.last_text()
        self.assertIn("ждём вас сегодня", text)
        self.assertIn("10:00-19:00", text)
        cards = self.team_cards()
        self.assertEqual(len(cards), 1)
        self.assertIn("Иванов Иван", cards[0])
        self.assertIn("№ B-1 · Павлюхина", cards[0])
        await self.feed(cb(f"wl:{self.booking}:{self.bike}"))
        self.assertEqual(len(self.team_cards()), 1, "второе нажатие - без второй карточки")
        log = await self.crm.notice_log(code="waitlist_coming")
        self.assertEqual(len(log), 1)

    async def test_taken_bike_is_replaced_by_the_same_model_or_refused(self):
        spare = await self.crm.create_bike(code="B-2", model="Kugoo V3",
                                           location="Павлюхина")
        await self.crm.update_bike(self.bike, status="repair", by="t")
        await self.feed(cb(f"wl:{self.booking}:{self.bike}"))
        self.assertIn("№ B-2", self.team_cards()[-1], "любой той же модели на точке")
        await self.crm.update_bike(spare, status="repair", by="t")
        self.crm.bookings_[self.booking]["coming_at"] = None
        self.session.calls.clear()
        await self.feed(cb(f"wl:{self.booking}:{self.bike}"))
        self.assertIn(texts.CAB_WAITLIST_GONE, self.alerts())
        self.assertEqual(self.team_cards(), [])

    async def test_foreign_closed_and_broken_buttons_are_stale(self):
        other = await self.crm.create_client(full_name="Чужой", phone="+79990000077",
                                             tg_id=7007)
        foreign = await self.crm.create_booking(client_id=other, model="Kugoo V3",
                                                tariff_id=self.tariff, location_id=self.loc,
                                                wanted_on=date.today())
        for data in (f"wl:{foreign}:{self.bike}", "wl:abc", "wl:1", "wl:",
                     f"wl:{self.booking}:²", f"wl:{self.booking}:{self.bike}:9",
                     "wl:" + "9" * 30 + ":1"):
            with self.subTest(data):
                self.session.calls.clear()
                await self.feed(cb(data))
                self.assertEqual(self.alerts(), [texts.CAB_BOOK_STALE])
        self.assertIsNone((await self.crm.booking(foreign))["coming_at"])
        await self.crm.update_booking(self.booking, status="cancelled")
        self.session.calls.clear()
        await self.feed(cb(f"wl:{self.booking}:{self.bike}"))
        self.assertEqual(self.alerts(), [texts.CAB_BOOK_STALE], "снятая заявка")
        self.assertEqual(self.team_cards(), [])


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestWaitlistPanel(tw.WebCase if HAVE_WEB else unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def test_booking_list_shows_who_was_called_and_answered(self):
        loc = tw.run(self.crm.create_location(name="Павлюхина", city="Казань",
                                              address=None, note=None))
        bid = tw.run(self.crm.create_booking(client_id=self.client_id, model="Kugoo V3",
                                             tariff_id=self.tariff_id, location_id=loc,
                                             wanted_on=date.today()))
        page = self.get_ok("/bookings")
        self.assertNotIn("🔔 уведомлён", page)
        tw.run(self.crm.mark_waitlist(bid, self.bike_id))
        tw.run(self.crm.mark_coming(bid))
        row = self.crm.bookings_[bid]
        note = logic.waitlist_note(row, today=date.today())
        self.assertTrue(note.startswith("уведомлён ") and " · ответил " in note)
        self.assertIn(f"🔔 {note}", self.get_ok("/bookings"))

    def test_notice_params_are_labelled_and_bounded(self):
        page = self.get_ok("/notices")
        self.assertIn("клиентов на велосипед", page)
        self.assertIn('name="per_bike"', page)
        self.assertIn('name="every_days"', page)
        self.client.post("/notices/waitlist", data={"enabled": "on", "per_bike": "3",
                                                    "from_hour": "10", "to_hour": "20"})
        state = logic.notice_settings(tw.run(self.crm.notices()))
        self.assertEqual(state["waitlist"]["extra"],
                         {"per_bike": 3, "from_hour": 10, "to_hour": 20})
        for bad in ({"per_bike": "0"}, {"from_hour": "25"}, {"to_hour": "0"}):
            with self.subTest(bad):
                self.client.post("/notices/waitlist", data={"enabled": "on", **bad})
                state = logic.notice_settings(tw.run(self.crm.notices()))
                self.assertEqual(state["waitlist"]["extra"]["per_bike"], 3)
                self.assertEqual(state["waitlist"]["extra"]["from_hour"], 10)
                self.assertEqual(state["waitlist"]["extra"]["to_hour"], 20)
        # прежние сроки - как раньше: «через N дн.»
        self.assertIn("через", page)


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
