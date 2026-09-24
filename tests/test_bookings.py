"""Заявка на аренду из кабинета: клиент выбирает модель, срок, точку и
день, оператор открывает из неё мастер выдачи с готовыми полями.

Главное: заявка - намерение, а не аренда. Велосипед под неё не
бронируется, одна открытая на клиента, при идущей аренде не подаётся,
выдача закрывает её ссылкой на аренду, снятие уходит клиенту.
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

    from app.crm import service
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal
TODAY = date(2026, 9, 24)


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class TestBookingLogic(unittest.TestCase):
    def test_models_count_free_and_keep_the_empty_ones(self):
        models = [{"id": 1, "title": "Kugoo V3", "active": True},
                  {"id": 2, "title": "Truck+", "active": True},
                  {"id": 3, "title": "Старая", "active": False}]
        bikes = [{"model": "Kugoo V3", "status": "available"},
                 {"model": "Kugoo V3", "status": "rented"},
                 {"model": "kugoo v3 pro", "status": "available"}]
        got = logic.booking_models(models, bikes, aliases={"kugoo v3 pro": "Kugoo V3"})
        self.assertEqual([(m["title"], m["free"]) for m in got],
                         [("Kugoo V3", 2), ("Truck+", 0)])
        self.assertEqual(got[0]["id"], 1)

    def test_when_is_bounded(self):
        day = TODAY.strftime("%Y%m%d")
        self.assertEqual(logic.booking_when(day, today=TODAY), TODAY)
        later = (TODAY + timedelta(days=2)).strftime("%Y%m%d")
        self.assertEqual(logic.booking_when(later, today=TODAY), TODAY + timedelta(days=2))
        far = (TODAY + timedelta(days=9)).strftime("%Y%m%d")
        self.assertIsNone(logic.booking_when(far, today=TODAY))
        self.assertIsNone(logic.booking_when("x", today=TODAY))
        self.assertIsNone(logic.booking_when("20261399", today=TODAY))
        # Смещение вместо даты - кнопка старого вида: по ней не понять,
        # какой день человек видел, поэтому она устарела.
        self.assertIsNone(logic.booking_when("1", today=TODAY))

    def test_tomorrow_pressed_tomorrow_is_still_that_day(self):
        """«Завтра, 25.09», нажатая 25-го, - это 25-е, а не 26-е."""
        tomorrow = TODAY + timedelta(days=1)
        self.assertEqual(logic.booking_when(tomorrow.strftime("%Y%m%d"), today=tomorrow),
                         tomorrow)
        yesterday = (TODAY - timedelta(days=1)).strftime("%Y%m%d")
        self.assertIsNone(logic.booking_when(yesterday, today=TODAY), "прошедший день")

    def test_line(self):
        self.assertEqual(logic.booking_line({"model": "Kugoo V3", "tariff_name": "Неделя",
                                             "location_title": "Павлюхина",
                                             "wanted_on": TODAY}),
                         "Kugoo V3 · Неделя · Павлюхина · 24.09.2026")
        self.assertEqual(logic.booking_line({}), "любая модель")
        self.assertIn("booking_new", logic.NOTICES)
        self.assertIn("booking_cancelled", logic.NOTICES)
        self.assertEqual(logic.section_for("/bookings/3/cancel"), "issue")


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestBookingFlow(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.model_id = _run(self.crm.create_bike_model(
            title="Kugoo V3", brand="Kugoo", factory_title=None, battery_slots=2,
            note=None))
        self.loc_id = _run(self.crm.create_location(name="Павлюхина", city="Казань",
                                                    address=None, note=None))

    def book(self, client_id=None, wanted=None):
        client = _run(self.crm.client(client_id or self.client_id))
        tariff = _run(self.crm.tariff(self.tariff_id))
        location = _run(self.crm.locations())[0]
        return _run(service.create_booking(
            self.crm, client=client, model="Kugoo V3", tariff=tariff,
            location=location, wanted_on=wanted or date.today()))

    def test_one_open_booking_and_none_during_a_rental(self):
        booking = self.book()
        self.assertEqual(booking["status"], "new")
        self.assertEqual(booking["tariff_name"], "Неделя")
        self.assertEqual(booking["location_title"], "Павлюхина")
        with self.assertRaises(service.ServiceError):
            self.book()
        _run(service.cancel_booking(self.crm, booking, by="клиент"))
        self.assertIsNone(_run(self.crm.open_booking_of(self.client_id)))
        _run(service.open_rental(
            self.crm, client=_run(self.crm.client(self.client_id)),
            bike=_run(self.crm.bike(self.bike_id)),
            tariff=_run(self.crm.tariff(self.tariff_id)),
            started_on=date.today(), contract_no=None, by="t"))
        with self.assertRaises(service.ServiceError):
            self.book()

    def test_issue_starts_from_the_booking_and_closes_it(self):
        booking = self.book(wanted=date.today() + timedelta(days=1))
        page = self.get_ok("/issue")
        self.assertIn("Заявки из кабинета", page)
        self.assertIn("Kugoo V3 · Неделя · Павлюхина", page)
        self.assertIn(f"booking={booking['id']}", page)
        # мастер по ссылке из заявки: клиент, модель и тариф уже выбраны
        page = self.get_ok(f"/issue?client={self.client_id}&model=Kugoo+V3"
                           f"&tariff={self.tariff_id}&booking={booking['id']}")
        self.assertIn("3 · Велосипед", page)
        page = self.get_ok(f"/issue?client={self.client_id}&model=Kugoo+V3"
                           f"&tariff={self.tariff_id}&bike={self.bike_id}"
                           f"&booking={booking['id']}")
        self.assertIn(f'name="booking_id" value="{booking["id"]}"', page)
        r = self.client.post("/issue", data={"client_id": self.client_id,
                                             "tariff_id": self.tariff_id,
                                             "bike_id": self.bike_id,
                                             "booking_id": str(booking["id"]),
                                             "pay_amount": "3000", "pay_method": "cash",
                                             "mileage": "10"})
        self.assertEqual(r.status_code, 303)
        fresh = _run(self.crm.booking(booking["id"]))
        self.assertEqual(fresh["status"], "done")
        rental = _run(self.crm.active_rental_of(self.client_id))
        self.assertEqual(fresh["rental_id"], rental["id"])
        self.assertEqual(fresh["handled_by"], "staff:admin")
        self.assertNotIn("Заявки из кабинета", self.get_ok("/issue"))

    def test_bookings_page_lists_and_cancels_with_a_note(self):
        booking = self.book()
        page = self.get_ok("/bookings")
        self.assertIn("Иванов Иван", page)
        self.assertIn("Ждёт выдачи", page)
        self.assertIn('href="/bookings"', page, "пункт меню «Брони»")
        r = self.client.post(f"/bookings/{booking['id']}/cancel",
                             data={"note": "модели нет до пятницы"})
        self.assertEqual(r.status_code, 303)
        fresh = _run(self.crm.booking(booking["id"]))
        self.assertEqual(fresh["status"], "cancelled")
        self.assertEqual(fresh["note"], "модели нет до пятницы")
        self.assertTrue(any(chat == 5001 and "снята оператором" in t
                            and "модели нет до пятницы" in t
                            for chat, t in self.bot.sent), self.bot.sent)
        log = _run(self.crm.notice_log(limit=20))
        self.assertTrue(any(r["code"] == "booking_cancelled" for r in log))
        # второй раз снимать нечего
        r = self.client.post(f"/bookings/{booking['id']}/cancel", data={})
        self.assertEqual(r.status_code, 303)
        self.assertIn("Снята", self.get_ok("/bookings"))

    def test_closing_an_already_cancelled_booking_is_a_no_op(self):
        booking = self.book()
        _run(service.cancel_booking(self.crm, booking, by="клиент"))
        _run(service.close_booking(self.crm, booking["id"], rental_id=1, by="t"))
        self.assertEqual(_run(self.crm.booking(booking["id"]))["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()


class _Forms:
    """Поля форм страницы - как их отправит браузер (без JS)."""

    def __init__(self, page: str):
        from html.parser import HTMLParser
        forms: list[dict] = []

        class Parser(HTMLParser):
            cur = None

            def handle_starttag(self, tag, attrs):
                a = dict(attrs)
                if tag == "form":
                    self.cur = {"method": a.get("method", "get"),
                                "action": a.get("action"), "fields": []}
                    forms.append(self.cur)
                elif tag == "input" and self.cur is not None and a.get("name"):
                    if a.get("type") in ("radio", "checkbox") and "checked" not in a:
                        return
                    self.cur["fields"].append((a["name"], a.get("value", "")))

            def handle_endtag(self, tag):
                if tag == "form":
                    self.cur = None

        Parser().feed(page)
        self.forms = forms


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestBookingThroughTheWizard(tw.WebCase):
    """Заявка из кабинета проходит мастер настоящими формами: без этого
    шаг 3 терял номер заявки и день, и выданная заявка оставалась
    открытой, а аренда начиналась сегодня."""

    def test_booking_and_day_survive_every_step(self):
        import re
        from urllib.parse import urlencode
        self.login()
        self.seed()
        wanted = date.today() + timedelta(days=2)
        booking = tw.run(tw.service.create_booking(
            self.crm, client=tw.run(self.crm.client(self.client_id)), model="Kugoo V3",
            tariff=tw.run(self.crm.tariff(self.tariff_id)), location=None,
            wanted_on=wanted))
        link = re.search(r'href="(/issue\?client=[^"]*booking=[^"]*)"',
                         self.get_ok("/issue")).group(1).replace("&amp;", "&")
        page = self.get_ok(link)
        form = next(f for f in _Forms(page).forms
                    if any(n == "bike" for n, _ in f["fields"]))
        page = self.get_ok("/issue?" + urlencode(form["fields"]))
        post = next(f for f in _Forms(page).forms
                    if f["action"] == "/issue" and f["method"] == "post")
        data = dict(post["fields"])
        self.assertEqual(data.get("started_on"), wanted.isoformat())
        data.update({"pay_amount": "3000", "pay_method": "cash", "mileage": "10"})
        self.client.post("/issue", data=data)
        self.assertEqual(tw.run(self.crm.booking(booking["id"]))["status"], "done")
        rental = tw.run(self.crm.active_rental_of(self.client_id))
        self.assertEqual(rental["started_on"], wanted)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestBookingByCatalogueTitle(tw.WebCase):
    def test_step_three_finds_the_bike_under_its_factory_name(self):
        """Заявка названа по каталогу, велосипед в парке - по накладной."""
        import re
        self.login()
        cid = tw.run(self.crm.create_client(full_name="Иванов Иван", phone="+79990000000",
                                            tg_id=5001))
        tw.run(self.crm.create_bike_model(title="Городской H10", brand="M",
                                          factory_title="Maikaolin H10",
                                          battery_slots=1, note=None))
        tw.run(self.crm.create_bike(code="B-1", model="Maikaolin H10", status="available"))
        tid = tw.run(self.crm.create_tariff("Неделя", 7, Decimal(3000), None))
        tw.run(tw.service.create_booking(
            self.crm, client=tw.run(self.crm.client(cid)), model="Городской H10",
            tariff=tw.run(self.crm.tariff(tid)), location=None, wanted_on=date.today()))
        link = re.search(r'href="(/issue\?client=[^"]*booking=[^"]*)"',
                         self.get_ok("/issue")).group(1).replace("&amp;", "&")
        page = self.get_ok(link)
        self.assertIn("№ B-1", page)
        self.assertNotIn("Свободных велосипедов этой модели нет", page)
