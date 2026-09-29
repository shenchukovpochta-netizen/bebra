"""Второй аккумулятор при продлении: кнопка в кабинете - просьба, а не
покупка.

Стерегут: кнопка есть только у аренды без доп. аккумулятора, когда есть
свободная подходящая батарея с ценой на её срок (без цены батарея не
выдаётся вовсе); цена - та, что возьмёт выдача (тариф модели главнее
запасного), а при разных ценах - честное «от»; нажатие не трогает ни
журнал, ни позиции, а записывает просьбу и шлёт карточку команде ровно
один раз; добавленная позиция просьбу снимает; чужая, старая и битая
кнопка - «устарела»; просьба видна в карточке аренды в панели.
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
    from aiogram.methods import AnswerCallbackQuery, SendMessage

    from app import texts
    from app.crm import service
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
NOW = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)


def _tariff(tid, price, days=7, model=None, kind="battery", active=True):
    return {"id": tid, "kind": kind, "model": model, "period_days": days,
            "price": D(price), "active": active}


def _battery(bid, model=None):
    return {"id": bid, "code": f"A-{bid}", "status": "available", "model_title": model}


class TestOfferLogic(unittest.TestCase):
    RENTAL = {"id": 1, "status": "active", "period_days": 7}
    FLEET = [_battery(1)]

    def test_price_is_what_issuing_will_charge(self):
        """Тариф модели батареи главнее запасного - как у выдачи
        (battery_extra_price). Общий тариф при своей цене модели не
        сработает, и назвать его клиенту - обещать сумму, которую не спишут."""
        tariffs = [_tariff(1, 500), _tariff(2, 700, model="LG 20Ah"),
                   _tariff(3, 2500, days=30), _tariff(4, 3000, kind="bike")]
        lg = [_battery(1, "LG 20Ah"), _battery(2, "LG 20Ah")]
        self.assertEqual(logic.battery_offer(tariffs, self.RENTAL, (), lg),
                         {"price": D(700), "days": 7, "exact": True})
        self.assertEqual(logic.battery_offer(tariffs, self.RENTAL, (), lg)["price"],
                         logic.battery_extra_price(tariffs, lg[0], 7))
        mixed = [*lg, _battery(3)]
        self.assertEqual(logic.battery_offer(tariffs, self.RENTAL, (), mixed),
                         {"price": D(500), "days": 7, "exact": False},
                         "у батареи без своей цены - запасной, и тогда честное «от»")
        # нулевой запасной не прячет кнопку батареям с ценой модели
        zero = [_tariff(1, 0), _tariff(2, 700, model="LG 20Ah")]
        self.assertEqual(logic.battery_offer(zero, self.RENTAL, (), lg)["price"], D(700))
        # у модели своя цена только на месяц - на неделю её не выдать вовсе
        month = [_tariff(1, 500), _tariff(2, 2500, days=30, model="LG 20Ah")]
        self.assertIsNone(logic.battery_offer(month, self.RENTAL, (), lg))

    def test_different_models_are_from(self):
        tariffs = [_tariff(1, 900, model="A"), _tariff(2, 700, model="B")]
        both = [_battery(1, "A"), _battery(2, "B")]
        self.assertEqual(logic.battery_offer(tariffs, self.RENTAL, (), both),
                         {"price": D(700), "days": 7, "exact": False})
        self.assertTrue(logic.battery_offer(tariffs, self.RENTAL, (), both[:1])["exact"],
                        "выдать можно только A - цена точная")
        same = [_tariff(1, 700, model="A"), _tariff(2, 700, model="B")]
        self.assertTrue(logic.battery_offer(same, self.RENTAL, (), both)["exact"])

    def test_no_offer_without_price_battery_rental_or_with_an_extra(self):
        tariffs = [_tariff(1, 700)]
        fleet = self.FLEET
        self.assertIsNone(logic.battery_offer(tariffs, self.RENTAL),
                          "свободной батареи нет - выдать нечего")
        self.assertIsNone(logic.battery_offer([_tariff(1, 700, days=14)], self.RENTAL, (),
                                              fleet),
                          "нет цены на срок аренды - батарею не выдают вовсе")
        self.assertIsNone(logic.battery_offer([_tariff(1, 700, active=False)], self.RENTAL,
                                              (), fleet))
        self.assertIsNone(logic.battery_offer([_tariff(1, 0)], self.RENTAL, (), fleet),
                          "бесплатная и без цены выглядят одинаково")
        self.assertIsNone(logic.battery_offer(tariffs, None, (), fleet))
        self.assertIsNone(logic.battery_offer(tariffs, {**self.RENTAL, "status": "closed"},
                                              (), fleet))
        self.assertIsNone(logic.battery_offer(tariffs, {**self.RENTAL, "period_days": 0},
                                              (), fleet))
        live = [{"kind": "battery", "removed_at": None, "price": D(700)}]
        self.assertIsNone(logic.battery_offer(tariffs, self.RENTAL, live, fleet))
        gone = [{"kind": "battery", "removed_at": NOW, "price": D(700)}]
        self.assertIsNotNone(logic.battery_offer(tariffs, self.RENTAL, gone, fleet),
                             "снятая позиция - снова можно предложить")

    def test_request_is_fresh_for_a_week(self):
        self.assertFalse(logic.battery_asked_recently({}, now=NOW))
        asked = {"battery_asked_at": NOW - timedelta(days=6)}
        self.assertTrue(logic.battery_asked_recently(asked, now=NOW))
        self.assertFalse(logic.battery_asked_recently(asked, now=NOW + timedelta(days=2)),
                         "команда забыла - клиент может напомнить")


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestBatteryButton(CabinetCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.approved_user()
        self.client = await self.crm_client(tg_id=USER_ID)
        self.rental = await self.crm_rental(self.client)
        await self.crm.create_tariff("АКБ неделя", 7, D(700), None, kind="battery")
        # свободная батарея без модели - её цена запасной тариф
        self.battery = await self.crm.create_battery(code="A-1")

    def labels(self):
        markup = self.session.last_markup()
        return [b.text for row in markup.inline_keyboard for b in row] if markup else []

    def callbacks(self):
        markup = self.session.last_markup()
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    def alerts(self):
        return [m.text for m in self.session.calls
                if isinstance(m, AnswerCallbackQuery) and m.show_alert]

    def team_cards(self):
        return [m.text for m in self.session.sent_to(ADMIN_CHAT)
                if isinstance(m, SendMessage) and "второй аккумулятор" in (m.text or "")]

    async def test_pay_screen_offers_the_battery_for_the_rental_term(self):
        await self.feed(cb("cab:pay"))
        self.assertIn("🔋 + второй аккумулятор за 700 ₽ / 7 дн.", self.labels())
        self.assertIn(f"cab:bat:{self.rental['id']}", self.callbacks())
        self.assertEqual(self.callbacks()[-1], "cab:home", "«назад» остаётся последней")

    async def test_renew_intent_offers_it_too(self):
        await self.feed(cb("cab:intent:renew"))
        renew = [m for m in self.session.sent_to(USER_ID)
                 if isinstance(m, SendMessage) and m.text == texts.CAB_INTENT_RENEW]
        self.assertTrue(renew and renew[0].reply_markup)
        data = [b.callback_data for row in renew[0].reply_markup.inline_keyboard for b in row]
        self.assertEqual(data, [f"cab:bat:{self.rental['id']}"])
        self.session.calls.clear()
        await self.feed(cb("cab:intent:return"))
        back = [m for m in self.session.sent_to(USER_ID)
                if isinstance(m, SendMessage) and "сдаёте" in (m.text or "")]
        self.assertIsNone(back[0].reply_markup, "сдающему батарею не предлагаем")

    async def test_no_button_without_price_or_with_an_extra(self):
        for t in list(self.crm.tariffs_.values()):
            if t.get("kind") == "battery":
                t["active"] = False
        await self.feed(cb("cab:pay"))
        self.assertFalse([x for x in self.labels() if "аккумулятор" in x])
        await self.crm.create_tariff("АКБ неделя", 7, D(800), None, kind="battery")
        await self.crm.add_rental_extra(self.rental["id"], kind="battery", title="Доп. АКБ",
                                        price=D(800), battery_id=None, by="t")
        await self.feed(cb("cab:pay"))
        self.assertFalse([x for x in self.labels() if "аккумулятор" in x],
                         "второй уже взят")

    async def battery_model(self, title, price):
        """Модель батареи с ценой на неделю и одна свободная батарея её."""
        model = await self.crm.create_battery_model(
            title=title, brand=None, voltage=48, capacity=D(20), price=D(15000),
            service_months=24, note=None)
        await self.crm.create_tariff(f"АКБ {title}", 7, D(price), None, model=title,
                                     kind="battery")
        return model, await self.crm.create_battery(code=f"M-{title}", model_id=model)

    async def test_model_prices_are_offered_as_from(self):
        await self.crm.update_battery(self.battery, status="repair")
        await self.battery_model("48В", 900)
        await self.battery_model("60В", 600)
        await self.feed(cb("cab:pay"))
        self.assertIn("🔋 + второй аккумулятор от 600 ₽ / 7 дн.", self.labels())
        await self.feed(cb(f"cab:bat:{self.rental['id']}"))
        self.assertIn("Цена: от 600 ₽ за 7 дн.", self.team_cards()[-1])

    async def test_model_price_beats_the_fallback_as_issuing_does(self):
        """Запасной 700, у модели LG своя 900, на точке только LG: клиенту
        «за 900», и столько же берёт выдача позиции - не 700."""
        await self.crm.update_battery(self.battery, status="repair")
        _, lg = await self.battery_model("LG 20Ah", 900)
        await self.feed(cb("cab:pay"))
        self.assertIn("🔋 + второй аккумулятор за 900 ₽ / 7 дн.", self.labels())
        await self.feed(cb(f"cab:bat:{self.rental['id']}"))
        self.assertIn("Цена: 900 ₽ за 7 дн.", self.team_cards()[-1])
        price = await service.add_battery_extra(
            self.crm, await self.crm.rental(self.rental["id"]), await self.crm.battery(lg),
            tariffs=await self.crm.tariffs(active_only=True), by="t")
        self.assertEqual(price, D(900))

    async def test_compatibility_narrows_the_price(self):
        """Матрица совместимости знает модель велосипеда - в кнопке цена
        только тех батарей, что встанут в раму, как в мастере выдачи."""
        await self.crm.update_battery(self.battery, status="repair")
        await self.battery_model("48В", 600)
        fits, _ = await self.battery_model("60В", 900)
        await self.feed(cb("cab:pay"))
        self.assertIn("🔋 + второй аккумулятор от 600 ₽ / 7 дн.", self.labels())
        bike_model = await self.crm.create_bike_model(
            title="Kugoo V3", brand="Kugoo", factory_title=None, battery_slots=1, note=None)
        await self.crm.set_compat(bike_model, fits, fits=True)
        await self.feed(cb("cab:pay"))
        self.assertIn("🔋 + второй аккумулятор за 900 ₽ / 7 дн.", self.labels())

    async def test_no_free_battery_no_button(self):
        await self.crm.update_battery(self.battery, status="rented")
        await self.feed(cb("cab:pay"))
        self.assertFalse([x for x in self.labels() if "аккумулятор" in x],
                         "выдать нечего - и просить не о чем")
        self.session.calls.clear()
        await self.feed(cb(f"cab:bat:{self.rental['id']}"))
        self.assertEqual(self.alerts(), [texts.CAB_BATTERY_STALE])
        self.assertEqual(self.team_cards(), [])

    async def test_added_extra_fulfils_the_request_for_good(self):
        """Позиция снимает просьбу: снятая позиция (курьер вернул вторую
        батарею) не прячет кнопку на неделю и не будит старую просьбу."""
        await self.feed(cb(f"cab:bat:{self.rental['id']}"))
        rental = await self.crm.rental(self.rental["id"])
        await service.add_battery_extra(self.crm, rental, await self.crm.battery(self.battery),
                                        tariffs=await self.crm.tariffs(active_only=True),
                                        by="t")
        self.assertIsNone((await self.crm.rental(self.rental["id"]))["battery_asked_at"])
        extra = (await self.crm.rental_extras(self.rental["id"]))[0]
        await service.drop_battery_extra(self.crm, rental, extra, by="t")
        await self.feed(cb("cab:pay"))
        self.assertIn("🔋 + второй аккумулятор за 700 ₽ / 7 дн.", self.labels())

    async def test_press_records_the_request_and_tells_the_team_once(self):
        ledger_before = await self.crm.ledger_of(self.client["id"])
        await self.feed(cb(f"cab:bat:{self.rental['id']}"))
        rental = await self.crm.rental(self.rental["id"])
        self.assertIsNotNone(rental["battery_asked_at"])
        self.assertEqual(await self.crm.ledger_of(self.client["id"]), ledger_before,
                         "просьба - не деньги")
        self.assertEqual(await self.crm.rental_extras(self.rental["id"]), [],
                         "позицию добавляет человек на точке")
        self.assertEqual(rental["price"], D(3000))
        self.assertIn(texts.CAB_BATTERY_ASKED.format(days=7), self.texts_to(USER_ID))
        cards = self.team_cards()
        self.assertEqual(len(cards), 1)
        self.assertIn("Иванов Иван", cards[0])
        self.assertIn(f"Аренда № {self.rental['id']} · велосипед B-7", cards[0])
        self.assertIn("Цена: 700 ₽ за 7 дн.", cards[0])
        # двойное нажатие и повтор - одна карточка, клиенту «уже у менеджера»
        self.session.calls.clear()
        await self.feed(cb(f"cab:bat:{self.rental['id']}"))
        self.assertEqual(self.alerts(), [texts.CAB_BATTERY_PENDING])
        self.assertEqual(self.team_cards(), [])
        # пока просьба свежая, кнопки на экране пополнения нет
        await self.feed(cb("cab:pay"))
        self.assertFalse([x for x in self.labels() if "аккумулятор" in x])
        log = await self.crm.notice_log(code="battery_request")
        self.assertEqual(len(log), 1)

    async def test_switched_off_team_notice_still_records_the_request(self):
        await self.crm.set_notice("battery_request", enabled=False, at_hour=None, by="t")
        await self.feed(cb(f"cab:bat:{self.rental['id']}"))
        self.assertEqual(self.team_cards(), [])
        self.assertIsNotNone((await self.crm.rental(self.rental["id"]))["battery_asked_at"],
                             "просьба видна в панели и без сообщения в чат")

    async def test_foreign_old_and_broken_buttons_are_stale(self):
        other = await self.crm_client(tg_id=7007, phone="+79990000077", name="Чужой")
        foreign = await self.crm.create_rental(
            client_id=other["id"], bike_id=None, tariff_id=None, tariff_name="Неделя",
            period_days=7, price=D(3000), billing="auto", started_on=date.today(),
            contract_no=None, created_by="t")
        for data in (f"cab:bat:{foreign}", "cab:bat:", "cab:bat:abc", "cab:bat:²",
                     "cab:bat:" + "9" * 30, f"cab:bat:{self.rental['id']}0"):
            with self.subTest(data):
                self.session.calls.clear()
                await self.feed(cb(data))
                self.assertEqual(self.alerts(), [texts.CAB_BATTERY_STALE])
        self.assertIsNone((await self.crm.rental(foreign)).get("battery_asked_at"))
        # кнопка закрытой аренды
        await self.crm.close_rental(self.rental["id"], closed_on=date.today(), note=None)
        self.session.calls.clear()
        await self.feed(cb(f"cab:bat:{self.rental['id']}"))
        self.assertEqual(self.alerts(), [texts.CAB_BATTERY_STALE])
        self.assertEqual(self.team_cards(), [])

    async def test_english_client_gets_the_translated_button(self):
        self.db.users[USER_ID]["lang"] = "en"
        await self.feed(cb("cab:pay"))
        self.assertIn("🔋 + second battery — 700 ₽ / 7 d.", self.labels())


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestRequestInThePanel(tw.WebCase if HAVE_WEB else unittest.TestCase):
    def test_rental_card_shows_the_request_until_an_extra_is_added(self):
        self.login()
        self.seed()
        rid = tw.run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="auto",
            started_on=date.today(), contract_no=None, created_by="t"))
        self.assertNotIn("попросил второй аккумулятор", self.get_ok(f"/rentals/{rid}"))
        tw.run(self.crm.claim_battery_ask(rid, days=7))
        self.assertIn("Клиент попросил второй аккумулятор", self.get_ok(f"/rentals/{rid}"))
        extra = tw.run(self.crm.add_rental_extra(rid, kind="battery", title="Доп. АКБ",
                                                 price=D(700), battery_id=None, by="t"))
        self.assertNotIn("попросил второй аккумулятор", self.get_ok(f"/rentals/{rid}"),
                         "позиция добавлена - просьба выполнена")
        # курьер вернул вторую батарею посреди аренды: выполненная просьба
        # плашкой не возвращается
        tw.run(self.crm.drop_rental_extra(extra, by="t"))
        self.assertNotIn("попросил второй аккумулятор", self.get_ok(f"/rentals/{rid}"))


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
