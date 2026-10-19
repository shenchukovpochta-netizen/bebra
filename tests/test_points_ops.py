"""Точки в операциях: выдача, возврат, замена, наряд, сотрудник, карточка
велосипеда и закрытие аренды из бота.

Правило, ради которого всё это: велосипед в аренде стоит на точке аренды,
иначе выручка точки (по точке аренды) и её дни (по журналу мест) считали
бы разные велосипеды. Списки точек - из справочника, а не из константы:
третью точку можно завести, и велосипед обязан на неё встать.

Обвязка панели (FakeCrm, FakeBotDB, FakeBot) - из tests/test_web.py.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_web as tw

    from app.crm import service, sync
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal
PAV, ADO, DEK = "Павлюхина", "Адоратского", "Декабристов"


class TestPointChoices(unittest.TestCase):
    PLACES = [
        {"name": PAV, "active": True, "sort": 10},
        {"name": ADO, "active": False, "sort": 20},
        {"name": DEK, "active": True, "sort": 100},
    ]

    def test_active_points_in_directory_order(self):
        self.assertEqual(logic.point_choices(self.PLACES), [PAV, DEK],
                         "закрытая точка новой карточке не предлагается")

    def test_closed_current_value_stays(self):
        """Закрытая точка остаётся в карточке: список без неё стёр бы её
        при первом же сохранении."""
        self.assertEqual(logic.point_choices(self.PLACES, ADO), [PAV, DEK, ADO])
        self.assertEqual(logic.point_choices(self.PLACES, PAV, None, "", ADO, ADO),
                         [PAV, DEK, ADO], "пустое и повтор не добавляются")

    def test_empty_directory_falls_back_to_the_constant(self):
        self.assertEqual(logic.point_choices([]), list(logic.LOCATIONS))
        self.assertEqual(logic.point_choices([], DEK), [*logic.LOCATIONS, DEK])

    def test_issue_point_order(self):
        booking, bike = {"location_name": DEK}, {"location": PAV}
        self.assertEqual(logic.issue_point(ADO, booking=booking, bike=bike), ADO)
        self.assertEqual(logic.issue_point("", booking=booking, bike=bike), DEK,
                         "клиент сам назвал точку в заявке")
        self.assertEqual(logic.issue_point(None, booking={"location_name": None},
                                           bike=bike), PAV)
        self.assertIsNone(logic.issue_point(" ", booking=None, bike={"location": None}))

    def test_bike_on_rent(self):
        self.assertTrue(logic.bike_on_rent({"status": "rented"}))
        self.assertTrue(logic.bike_on_rent({"status": "repair", "rental_id": 7}),
                        "идущая аренда на велосипеде - тоже у клиента")
        self.assertFalse(logic.bike_on_rent({"status": "available", "rental_id": None}))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class PointsCase(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        crm = self.crm
        self.pav_id = tw.run(crm.create_location(name=PAV, city="Казань",
                                                 address="ул. Павлюхина, 97",
                                                 note=None, sort=10))
        self.ado_id = tw.run(crm.create_location(name=ADO, city="Казань",
                                                 address="ул. Адоратского, 11А",
                                                 note=None, sort=20))
        # Третья точка: её нет в logic.LOCATIONS, и велосипед обязан на неё
        # вставать так же, как на две первые.
        self.dek_id = tw.run(crm.create_location(name=DEK, city="Казань",
                                                 address="ул. Декабристов, 1",
                                                 note=None))
        tw.run(crm.update_bike(self.bike_id, location=PAV))
        self.bike2_id = tw.run(crm.create_bike(code="B-2", model="Kugoo V3",
                                               location=DEK))

    def bike(self, bike_id=None):
        return tw.run(self.crm.bike(bike_id or self.bike_id))

    def rental(self):
        return tw.run(self.crm.active_rental_of(self.client_id))

    def open_rental(self, location=None, bike_id=None):
        return tw.run(service.open_rental(
            self.crm, client=tw.run(self.crm.client(self.client_id)),
            bike=self.bike(bike_id), tariff=tw.run(self.crm.tariff(self.tariff_id)),
            started_on=date.today(), contract_no="АВ-1", by="test",
            location=location))

    def issue_form(self, **extra):
        data = {"client_id": self.client_id, "tariff_id": self.tariff_id,
                "bike_id": self.bike_id, "started_on": date.today().isoformat(),
                "pay_amount": "0", "pay_method": "cash", "mileage": "0"}
        data.update(extra)
        return self.client.post("/issue", data=data)


class TestIssueWithPoint(PointsCase):
    def test_bike_step_filters_free_bikes_by_point(self):
        base = f"/issue?client={self.client_id}&tariff={self.tariff_id}&model=Kugoo+V3"
        page = self.get_ok(base + f"&location={DEK}")
        self.assertIn("№ B-2", page)
        self.assertNotIn("№ B-1", page, "велосипед с другой точки клиенту не подать")
        self.assertIn("На других точках свободных этой модели ещё 1", page)
        self.assertIn(f'<option value="{DEK}" selected>', page)
        page = self.get_ok(base)
        self.assertIn("№ B-1", page)
        self.assertIn("№ B-2", page, "без точки - все свободные")
        # Имя из адреса - чужая строка: не точка справочника - не фильтр.
        page = self.get_ok(base + "&location=Луна")
        self.assertIn("№ B-1", page)
        self.assertIn("№ B-2", page)

    def test_summary_defaults_to_the_bike_point_and_offers_the_third(self):
        page = self.get_ok(f"/issue?client={self.client_id}&tariff={self.tariff_id}"
                           f"&bike={self.bike_id}")
        self.assertIn("Точка выдачи", page)
        self.assertIn(f'<option value="{PAV}" selected>', page)
        self.assertIn(f'<option value="{DEK}"', page)
        page = self.get_ok(f"/issue?client={self.client_id}&tariff={self.tariff_id}"
                           f"&bike={self.bike_id}&location={ADO}")
        self.assertIn(f'<option value="{ADO}" selected>', page,
                      "выбранная на шагах точка важнее точки велосипеда")

    def test_issue_puts_the_rental_and_the_bike_on_the_chosen_point(self):
        r = self.issue_form(location=ADO)
        self.assertEqual(r.status_code, 303)
        rental = self.rental()
        self.assertEqual(rental["location"], ADO)
        bike = self.bike()
        self.assertEqual((bike["status"], bike["location"]), ("rented", ADO),
                         "в аренде велосипед стоит на точке аренды")
        moves = tw.run(self.crm.bike_location_log(self.bike_id))
        self.assertEqual((moves[0]["from_location"], moves[0]["to_location"],
                          moves[0]["changed_by"]), (PAV, ADO, "staff:admin"))

    def test_issue_without_a_choice_takes_the_bike_point(self):
        self.issue_form()
        self.assertEqual(self.rental()["location"], PAV)
        self.assertEqual(self.bike()["location"], PAV)

    def test_unknown_point_is_refused_before_money(self):
        r = self.issue_form(location="Луна", pay_amount="3000")
        self.assertEqual(r.status_code, 303)
        self.assertIn("location=", r.headers["location"], "точка едет назад в мастер")
        self.assertIsNone(self.rental())
        self.assertEqual(self.crm.ledger_, [])
        self.assertIn("Точка: недопустимое значение", self.get_ok("/"))

    def test_booking_point_goes_through_the_wizard(self):
        booking = tw.run(service.create_booking(
            self.crm, client=tw.run(self.crm.client(self.client_id)), model="Kugoo V3",
            tariff=tw.run(self.crm.tariff(self.tariff_id)),
            location=tw.run(self.crm.locations())[2], wanted_on=date.today()))
        self.assertEqual(booking["location_name"], DEK)
        page = self.get_ok("/issue")
        self.assertIn(f"location={quote(DEK)}", page, "ссылка из заявки несёт её точку")
        self.assertNotIn("/issue?", self.get_ok("/bookings"), "владелец сам не выдаёт")
        # «Выдать» в заявках - у администратора точки, с точкой заявки.
        manager = tw.run(self.crm.access_profile_by_code("manager"))
        tw.run(self.crm.create_staff("ivan", logic.hash_password("password-123"), "Иван",
                                     "manager", manager["id"]))
        self.client.post("/logout")
        self.login("ivan", "password-123")
        self.assertIn(f"location={quote(DEK)}", self.get_ok("/bookings"))
        self.client.post("/logout")
        self.login()
        page = self.get_ok(f"/issue?client={self.client_id}&model=Kugoo+V3"
                           f"&booking={booking['id']}&location={DEK}")
        self.assertIn("её выбрал клиент в заявке", page)
        self.assertIn(f'name="location" value="{DEK}"', page, "точка едет на шаг 3")
        page = self.get_ok(f"/issue?client={self.client_id}&tariff={self.tariff_id}"
                           f"&model=Kugoo+V3&booking={booking['id']}&location={DEK}")
        self.assertIn("№ B-2", page)
        self.assertNotIn("№ B-1", page)
        # Поле точки не пришло (старая форма) - точка заявки, а не велосипеда.
        self.issue_form(booking_id=str(booking["id"]))
        rental = self.rental()
        self.assertEqual(rental["location"], DEK)
        self.assertEqual(self.bike()["location"], DEK)
        self.assertEqual(tw.run(self.crm.booking(booking["id"]))["status"], "done")

    def test_manual_rental_takes_the_point(self):
        page = self.get_ok("/rentals/new")
        self.assertIn(f'<option value="{DEK}">', page)
        r = self.client.post("/rentals", data={
            "client_id": self.client_id, "tariff_id": self.tariff_id,
            "bike_id": self.bike_id, "location": DEK, "billing": "manual"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.rental()["location"], DEK)
        self.assertEqual(self.bike()["location"], DEK)

    def test_manual_rental_refuses_unknown_point(self):
        self.client.post("/rentals", data={
            "client_id": self.client_id, "tariff_id": self.tariff_id,
            "bike_id": self.bike_id, "location": "Луна"})
        self.assertIsNone(self.rental())


class TestReturnAndSwap(PointsCase):
    def test_return_point_moves_the_bike(self):
        rental_id = self.open_rental()
        page = self.get_ok(f"/rentals/{rental_id}")
        self.assertIn("Точка возврата", page)
        self.assertIn(f'<option value="{PAV}" selected>', page,
                      "по умолчанию - точка аренды")
        r = self.client.post(f"/rentals/{rental_id}/close",
                             data={"return_location": DEK, "bike_status": "available"})
        self.assertEqual(r.status_code, 303)
        bike = self.bike()
        self.assertEqual((bike["status"], bike["location"]), ("available", DEK),
                         "сданный на другой точке простаивает там")
        self.assertEqual(tw.run(self.crm.rental(rental_id))["location"], PAV,
                         "точка аренды - снимок выдачи")

    def test_return_without_a_choice_leaves_the_rental_point(self):
        rental_id = self.open_rental(location=ADO)
        self.client.post(f"/rentals/{rental_id}/close", data={"bike_status": "repair"})
        self.assertEqual(self.bike()["location"], ADO)

    def test_return_refuses_unknown_point(self):
        rental_id = self.open_rental()
        self.client.post(f"/rentals/{rental_id}/close", data={"return_location": "Луна"})
        self.assertEqual(tw.run(self.crm.rental(rental_id))["status"], "active")

    def test_swap_leaves_the_old_bike_where_it_was_swapped(self):
        rental_id = self.open_rental()
        page = self.get_ok(f"/rentals/{rental_id}")
        self.assertIn("Где меняли", page)
        r = self.client.post(f"/rentals/{rental_id}/swap",
                             data={"bike_id": self.bike2_id, "reason": "repair",
                                   "swap_location": ADO})
        self.assertEqual(r.status_code, 303)
        old, new = self.bike(), self.bike(self.bike2_id)
        self.assertEqual((old["status"], old["location"]), ("repair", ADO))
        self.assertEqual((new["status"], new["location"]), ("rented", PAV),
                         "новый встаёт на точку аренды")
        self.assertEqual(tw.run(self.crm.rental(rental_id))["location"], PAV,
                         "замена точку аренды не меняет")

    def test_swap_refuses_unknown_point(self):
        rental_id = self.open_rental()
        self.client.post(f"/rentals/{rental_id}/swap",
                         data={"bike_id": self.bike2_id, "reason": "repair",
                               "swap_location": "Луна"})
        self.assertEqual(tw.run(self.crm.rental(rental_id))["bike_id"], self.bike_id)


class TestOrderAndStaffPoint(PointsCase):
    def order(self, order_id):
        return tw.run(self.crm.work_order(order_id))

    def test_order_point_defaults_to_the_bike_and_is_chosen_for_foreign(self):
        page = self.get_ok(f"/orders/new?bike={self.bike_id}")
        # Подпись точки - с адресом (logic.point_label), значение - имя.
        self.assertIn(f"как у велосипеда ({PAV} · ул. Павлюхина, 97)", page)
        self.client.post("/orders", data={"bike_id": self.bike_id, "payer": "own"})
        own = max(self.crm.orders_)
        self.assertEqual(self.order(own)["location"], PAV)
        self.client.post("/orders", data={"payer": "client", "location": DEK,
                                          "object_note": "самокат Kugoo"})
        foreign = max(self.crm.orders_)
        self.assertEqual(self.order(foreign)["location"], DEK,
                         "у чужой техники точку выбирают")
        self.client.post("/orders", data={"payer": "client",
                                          "object_note": "АКБ 60 Ач"})
        self.assertIsNone(self.order(max(self.crm.orders_))["location"])
        before = len(self.crm.orders_)
        self.client.post("/orders", data={"payer": "client", "location": "Луна",
                                          "object_note": "самокат"})
        self.assertEqual(len(self.crm.orders_), before, "чужое имя точки - отказ")

    def test_order_point_is_edited_and_kept(self):
        self.client.post("/orders", data={"bike_id": self.bike_id, "payer": "own"})
        order_id = max(self.crm.orders_)
        page = self.get_ok(f"/orders/{order_id}")
        self.assertIn("Где чиним", page)
        self.client.post(f"/orders/{order_id}/edit",
                         data={"status": "in_work", "location": ADO})
        self.assertEqual(self.order(order_id)["location"], ADO)
        # Поля нет в запросе - точку не трогаем, а не стираем.
        self.client.post(f"/orders/{order_id}/edit", data={"status": "in_work"})
        self.assertEqual(self.order(order_id)["location"], ADO)

    def test_staff_own_point(self):
        page = self.get_ok("/staff")
        self.assertIn("Своя точка", page)
        manager = tw.run(self.crm.access_profile_by_code("manager"))
        self.client.post("/staff", data={"login": "ivan", "password": "password-123",
                                         "name": "Иван", "location": DEK,
                                         "profile_id": manager["id"]})
        ivan = tw.run(self.crm.staff_by_login("ivan"))
        self.assertEqual(ivan["location"], DEK)
        self.client.post(f"/staff/{ivan['id']}/location", data={"location": ADO})
        self.assertEqual(tw.run(self.crm.staff_by_id(ivan["id"]))["location"], ADO)
        self.client.post(f"/staff/{ivan['id']}/location", data={"location": "Луна"})
        self.assertEqual(tw.run(self.crm.staff_by_id(ivan["id"]))["location"], ADO)
        # Закрытую точку в строке сотрудника не теряем.
        tw.run(self.crm.update_location(self.ado_id, active=False))
        page = self.get_ok("/staff")
        self.assertIn(f'<option value="{ADO}" selected>', page)
        self.client.post(f"/staff/{ivan['id']}/location", data={"location": ADO})
        self.assertEqual(tw.run(self.crm.staff_by_id(ivan["id"]))["location"], ADO)
        self.client.post(f"/staff/{ivan['id']}/location", data={"location": ""})
        self.assertIsNone(tw.run(self.crm.staff_by_id(ivan["id"]))["location"])


class TestBikeCardPoint(PointsCase):
    def edit(self, bike_id=None, **extra):
        bike = self.bike(bike_id)
        data = {"code": bike["code"], "model": bike["model"], "battery_count": "2"}
        data.update(extra)
        return self.client.post(f"/bikes/{bike['id']}/edit", data=data)

    def test_third_point_can_be_set_on_a_bike(self):
        page = self.get_ok(f"/bikes/{self.bike_id}")
        self.assertIn(f'<option value="{DEK}"', page)
        self.edit(location=DEK)
        self.assertEqual(self.bike()["location"], DEK)
        self.edit(location="Луна")
        self.assertEqual(self.bike()["location"], DEK, "чужое имя точки - отказ")
        self.assertIn(f'<option value="{DEK}"', self.get_ok("/bikes/new"))

    def test_closed_point_survives_saving_the_card(self):
        tw.run(self.crm.update_location(self.pav_id, active=False))
        page = self.get_ok(f"/bikes/{self.bike_id}")
        self.assertIn(f'<option value="{PAV}" selected>', page)
        self.assertNotIn(f'<option value="{PAV}"', self.get_ok("/bikes/new"),
                         "новому велосипеду закрытая точка не предлагается")
        self.edit(location=PAV, note="после осмотра")
        bike = self.bike()
        self.assertEqual((bike["location"], bike["note"]), (PAV, "после осмотра"))

    def test_rented_bike_point_is_read_only(self):
        self.open_rental()
        page = self.get_ok(f"/bikes/{self.bike_id}")
        self.assertNotIn('name="location"', page, "поля точки у велосипеда в аренде нет")
        self.assertIn("точку меняют возврат", page.replace("\n", " "))
        r = self.edit(location=ADO)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.bike()["location"], PAV, "сервер тоже не даёт")
        self.edit(note="царапина на раме")
        bike = self.bike()
        self.assertEqual((bike["location"], bike["note"]), (PAV, "царапина на раме"),
                         "остальное сохраняется, точка - нет")
        # Та же точка в устаревшей форме - не переезд, сохранить можно.
        self.edit(location=PAV, note="ещё раз")
        self.assertEqual(self.bike()["note"], "ещё раз")

    def test_stale_card_of_a_returned_bike_keeps_its_point(self):
        """Карточку открыли, пока велосипед был в аренде (поля точки в ней
        нет), его сдали на Адоратского, карточку сохранили: «поля нет» - не
        «не на точке»."""
        self.open_rental()
        self.assertNotIn('name="location"', self.get_ok(f"/bikes/{self.bike_id}"))
        tw.run(service.close_rental(self.crm, self.rental(), closed_on=date.today(),
                                    note=None, by="bot", return_location=ADO))
        self.assertEqual(self.bike()["location"], ADO)
        r = self.edit(note="после сдачи")
        self.assertEqual(r.status_code, 303)
        bike = self.bike()
        self.assertEqual((bike["location"], bike["note"]), (ADO, "после сдачи"))
        self.assertEqual(tw.run(self.crm.bike_location_log(self.bike_id))[0]["to_location"],
                         ADO, "переезда «в никуда» в журнале нет")
        # Выбор «не на точке» в свежей форме - пустое значение, и он работает.
        self.edit(location="")
        self.assertIsNone(self.bike()["location"])

    def test_issue_between_reading_and_saving_the_card_keeps_the_rental_point(self):
        """Карточку свободного велосипеда сохраняют, а между её чтением и
        записью другой оператор выдаёт его с Адоратского. Проверка «не в
        аренде» уже пройдена - точку не пускает условие в самом UPDATE."""
        crm, issued = self.crm, []
        real = crm.bike_by_code

        async def racy(code):
            if not issued:
                issued.append(code)
                await service.open_rental(
                    crm, client=await crm.client(self.client_id),
                    bike=await crm.bike(self.bike_id),
                    tariff=await crm.tariff(self.tariff_id), started_on=date.today(),
                    contract_no=None, by="staff:op2", location=ADO)
            return await real(code)

        crm.bike_by_code = racy
        r = self.edit(location=DEK, note="смотрел")
        self.assertEqual(r.status_code, 303)
        bike = self.bike()
        self.assertEqual((bike["status"], bike["location"], bike["note"]),
                         ("rented", ADO, "смотрел"), "остальное сохранено, точка - нет")
        self.assertEqual(self.rental()["location"], ADO)
        page = self.get_ok(f"/bikes/{self.bike_id}")
        self.assertIn("кроме точки", page)


class TestBotPoint(PointsCase):
    USER = {"tg_id": 5001, "phone": "+79990000000", "contract_no": "АВ-1"}

    def close_from_bot(self, address):
        tw.run(sync.on_rental_closed(
            self.crm, {**self.USER, "return_data": {"return_address": address}},
            today=date.today()))

    def test_close_form_address_sets_the_return_point(self):
        rental_id = self.open_rental()
        self.close_from_bot("ул. Адоратского, 15")
        self.assertEqual(tw.run(self.crm.rental(rental_id))["status"], "closed")
        self.assertEqual(self.bike()["location"], ADO)

    def test_unmatched_address_leaves_the_rental_point(self):
        rental_id = self.open_rental(location=DEK)
        self.close_from_bot("у метро, возле шаурмы")
        self.assertEqual(tw.run(self.crm.rental(rental_id))["status"], "closed")
        self.assertEqual(self.bike()["location"], DEK)

    def test_directory_failure_does_not_block_the_close(self):
        rental_id = self.open_rental()

        async def broken(**kwargs):
            raise RuntimeError("нет связи")

        self.crm.locations = broken
        with self.assertLogs("app.crm.sync", level="ERROR"):
            self.close_from_bot("Адоратского")
        self.assertEqual(tw.run(self.crm.rental(rental_id))["status"], "closed")
        self.assertEqual(self.bike()["location"], PAV)

    def test_bot_issue_takes_the_bike_point(self):
        tw.run(self.crm.update_bike(self.bike2_id, frame_no="FR-777"))
        tw.run(sync.on_rental_started(self.crm, {
            **self.USER, "full_name": "Иванов Иван",
            "issue_data": {"rent_price": "3000", "vin_frame": "FR-777",
                           "bike_model": "Kugoo V3"},
            "rent_from": date.today(), "rent_until": date.today() + timedelta(days=7),
        }, today=date.today()))
        rental = self.rental()
        self.assertEqual(rental["bike_id"], self.bike2_id)
        self.assertEqual(rental["location"], DEK)
        self.assertEqual(self.bike(self.bike2_id)["location"], DEK)


# ───────────────── склад и подпись точки адресом ─────────────────

STORE = "Склад"
STORE_ROW = {"name": STORE, "city": "Казань", "active": True, "sort": 30,
             "address": "г. Казань, ул. Динмухаметова, 14А", "kind": "warehouse"}
POINT_ROWS = [
    {"name": PAV, "city": "Казань", "active": True, "sort": 10,
     "address": "г. Казань, ул. Павлюхина, 97", "kind": "point", "hours": "10-19"},
    {"name": ADO, "city": "Казань", "active": True, "sort": 20,
     "address": "г. Казань, ул. Адоратского, 11А", "kind": "point", "hours": "10-19"},
    STORE_ROW,
]


class TestPointLabel(unittest.TestCase):
    def test_label_is_name_and_address_without_own_city(self):
        """Эдгар: в списке «Точка» - полные адреса. Город точки в подписи
        опускается (он у точки отдельным полем), чужой - остаётся."""
        self.assertEqual(logic.point_label(POINT_ROWS[0]), "Павлюхина · ул. Павлюхина, 97")
        self.assertEqual(logic.point_label(POINT_ROWS[1]),
                         "Адоратского · ул. Адоратского, 11А")
        self.assertEqual(logic.point_label(STORE_ROW), "Склад · ул. Динмухаметова, 14А")
        self.assertEqual(logic.point_address({"city": "Казань",
                                              "address": "Казань, ул. Баумана, 1"}),
                         "ул. Баумана, 1")
        self.assertEqual(logic.point_address({"city": "Казань",
                                              "address": "г. Зеленодольск, ул. Ленина, 2"}),
                         "г. Зеленодольск, ул. Ленина, 2")

    def test_label_without_address_is_the_name(self):
        self.assertEqual(logic.point_label({"name": DEK, "address": None}), DEK)
        self.assertEqual(logic.point_label({"name": "ул. Ленина, 1", "city": "Казань",
                                            "address": "г. Казань, ул. Ленина, 1"}),
                         "ул. Ленина, 1", "адрес и есть имя - не повторяем")
        self.assertEqual(logic.point_label(None), "")

    def test_labels_map_marks_the_warehouse(self):
        labels = logic.point_labels(POINT_ROWS)
        self.assertEqual(labels[PAV]["label"], "Павлюхина · ул. Павлюхина, 97")
        self.assertEqual(labels[STORE]["address"], "ул. Динмухаметова, 14А")
        self.assertTrue(labels[STORE]["warehouse"])
        self.assertFalse(labels[PAV]["warehouse"])


class TestWarehouseLogic(unittest.TestCase):
    def test_client_points_and_choices_skip_the_warehouse(self):
        self.assertEqual([p["name"] for p in logic.client_points(POINT_ROWS)], [PAV, ADO])
        self.assertEqual(logic.warehouse_names(POINT_ROWS), {STORE})
        self.assertEqual(logic.point_choices(POINT_ROWS), [PAV, ADO, STORE],
                         "для парка, пересчёта и задач склад - точка")
        self.assertEqual(logic.point_choices(POINT_ROWS, STORE, DEK, clients=True),
                         [PAV, ADO, DEK],
                         "выдаче склад не предлагается и текущим значением")
        # Склад без вида (строка старой схемы) - точка выдачи.
        self.assertFalse(logic.is_warehouse({"name": "X"}))
        self.assertEqual(logic.point_choices([STORE_ROW], clients=True), [],
                         "справочник есть - константа не подставляется")

    def test_warehouse_has_no_plan(self):
        rows = [dict(POINT_ROWS[0], plan_rented=10), dict(POINT_ROWS[1], plan_rented=5),
                dict(STORE_ROW)]
        total = logic.plan_from_points(rows, check=D(500))
        self.assertEqual(total["rented"], 15,
                         "у склада плана нет - сумма точек выдачи и есть сеть")
        self.assertIsNone(logic.point_plan(dict(STORE_ROW, plan_rented=3), check=D(500)))

    def test_transfer_takes_from_the_warehouse_and_never_brings_to_it(self):
        today = date(2026, 10, 19)
        bikes = ([{"id": i, "code": f"S-{i}", "model": "Truck", "status": "available",
                   "location": STORE} for i in range(1, 4)]
                 + [{"id": 10, "code": "P-1", "model": "Truck", "status": "rented",
                     "location": PAV}])
        # Каждый день недели по выдаче на Павлюхина - спрос 1 в день.
        history = [{"started_on": today - timedelta(days=d), "location": PAV,
                    "model": "Truck"} for d in range(1, 57)]
        # Выдача «на складе» в истории и заявка на него - не спрос склада.
        history += [{"started_on": today - timedelta(days=d), "location": STORE,
                     "model": "Truck"} for d in range(1, 57)]
        plan = logic.transfer_plan(points=[PAV, ADO, STORE], bikes=bikes, rentals=[],
                                   history=history, today=today, safety=1,
                                   stores=[STORE],
                                   bookings=[{"location_name": STORE, "model": "Truck",
                                              "wanted_on": today + timedelta(days=1)}])
        store = next(r for r in plan["rows"] if r["location"] == STORE)
        self.assertEqual((store["need"], store["spare"], store["store"]), (0, 3, True),
                         "склад отдаёт всё свободное: запас держат под курьера")
        self.assertEqual([(m["source"], m["target"], m["count"]) for m in plan["moves"]],
                         [(STORE, PAV, 2)])
        self.assertTrue(all(m["target"] != STORE for m in plan["moves"]))
        # Без пометки склада - обычная точка со своим спросом и запасом.
        plain = logic.transfer_plan(points=[PAV, STORE], bikes=bikes, rentals=[],
                                    history=history, today=today, safety=1)
        self.assertLess(next(r for r in plain["rows"] if r["location"] == STORE)["spare"], 3)

    def test_waitlist_and_booking_do_not_count_warehouse_bikes(self):
        bike = {"id": 1, "status": "available", "model": "Truck", "location": STORE}
        booking = {"id": 5, "status": "new", "model": "Truck", "location_id": None}
        self.assertTrue(logic.waitlist_fits(bike, booking))
        self.assertFalse(logic.waitlist_fits(bike, booking, stores={STORE}),
                         "звать к велосипеду на складе - звать туда, где не выдают")
        self.assertEqual(logic.waitlist_queue(bike, [booking], today=date(2026, 10, 19),
                                              stores={STORE}), [])
        models = [{"id": 1, "title": "Truck"}]
        self.assertEqual(logic.booking_models(models, [bike], stores={STORE})[0]["free"], 0)
        self.assertEqual(logic.booking_models(models, [bike])[0]["free"], 1)
        rows = [dict(r, id=i) for i, r in enumerate(POINT_ROWS, 1)]
        self.assertEqual([p["title"] for p in logic.booking_points(rows, models, [bike])],
                         [PAV, ADO], "склада в заявке нет")

    def test_bot_never_names_the_warehouse(self):
        from app.crm import points, readiness
        try:
            points.set_snapshot(POINT_ROWS)
            self.assertEqual([p["name"] for p in points.snapshot()], [PAV, ADO])
        finally:
            points.reset()
        note = points.hours_note("ru", rows=[dict(STORE_ROW, hours="круглосуточно"),
                                             POINT_ROWS[0]])
        self.assertNotIn("круглосуточно", note, "сдача «на любой точке» - не на склад")
        self.assertIn("10-19", note)
        # «Готовность» не просит у склада телефон и режим.
        rows = [dict(POINT_ROWS[0], phone="+7", address="ул. Павлюхина, 97"),
                dict(STORE_ROW)]
        self.assertEqual(readiness.check_points(rows)["state"], readiness.OK)
        self.assertNotIn(STORE, readiness.check_points(rows)["text"])


class TestWarehouseInPanel(PointsCase):
    """Склад - точка для парка, пересчёта, задач и перевозки, но не для
    выдачи и кассы. Подпись точки в списках - с адресом."""

    def setUp(self):
        super().setUp()
        self.store_id = tw.run(self.crm.create_location(
            name=STORE, city="Казань", address="г. Казань, ул. Динмухаметова, 14А",
            note=None, sort=30, kind="warehouse"))
        self.store_bike = tw.run(self.crm.create_bike(code="S-1", model="Kugoo V3",
                                                      location=STORE))

    def test_issue_offers_no_warehouse_and_asks_for_a_point(self):
        base = f"/issue?client={self.client_id}&tariff={self.tariff_id}"
        page = self.get_ok(base + "&model=Kugoo+V3")
        self.assertNotIn(f'<option value="{STORE}"', page)
        self.assertIn(f'<option value="{PAV}" >Павлюхина · ул. Павлюхина, 97</option>',
                      page, "значение - имя, подпись - с адресом")
        page = self.get_ok(base + f"&bike={self.store_bike}")
        self.assertNotIn(f'<option value="{STORE}"', page)
        self.assertIn('<option value="">не указана</option>', page,
                      "велосипед со склада: точку выдачи называет оператор")
        # Без точки и со складом - отказ до денег.
        self.issue_form(bike_id=self.store_bike, pay_amount="3000")
        self.assertIsNone(self.rental())
        self.assertEqual(self.crm.ledger_, [])
        self.assertIn("стоит на складе", self.get_ok("/"))
        self.issue_form(bike_id=self.store_bike, location=STORE)
        self.assertIsNone(self.rental())
        # С точкой - выдача, велосипед переезжает на точку аренды.
        self.issue_form(bike_id=self.store_bike, location=ADO)
        self.assertEqual(self.rental()["location"], ADO)
        self.assertEqual(self.bike(self.store_bike)["location"], ADO)

    def test_service_refuses_the_warehouse_for_every_path(self):
        """Быстрая выдача из лички и ручная аренда идут тем же
        service.open_rental: склад точкой аренды не станет ни выбором, ни
        «как у велосипеда»."""
        for location in (None, STORE):
            with self.assertRaises(service.ServiceError):
                self.open_rental(location=location, bike_id=self.store_bike)
        self.assertNotIn(f'<option value="{STORE}"', self.get_ok("/rentals/new"))
        self.assertNotIn(f'<option value="{STORE}"', self.get_ok("/rentals"))

    def test_cash_and_promo_have_no_warehouse(self):
        page = self.get_ok("/cash")
        self.assertIn(f'<option value="{PAV}"', page)
        self.assertNotIn(f'<option value="{STORE}"', page)
        self.client.post("/cash", data={"location": STORE, "opening": "0"})
        self.assertEqual(tw.run(self.crm.open_shifts()), [], "на складе кассы нет")
        page = self.get_ok(f"/promos/new?kind={logic.IDLE_PROMO_KIND}")
        self.assertIn(f'<option value="{PAV}"', page)
        self.assertNotIn(f'<option value="{STORE}"', page, "акция ложится на аренду")

    def test_warehouse_is_a_point_for_the_fleet(self):
        label = f'<option value="{STORE}">Склад · ул. Динмухаметова, 14А</option>'
        for path in ("/bikes/new", "/stock-takes", "/assets", "/staff", "/tasks"):
            self.assertIn(f'value="{STORE}"', self.get_ok(path), path)
        self.assertIn(label, self.get_ok("/bikes/new"))
        # Форма «Поставить задачу» - макрос, импортированный без контекста
        # страницы: подпись с адресом доходит и до него.
        self.assertIn(">Склад · ул. Динмухаметова, 14А</option>", self.get_ok("/my"))
        self.assertIn(">Павлюхина · ул. Павлюхина, 97</option>", self.get_ok("/my"))
        r = self.client.post("/bikes/transfer", data={
            "source": STORE, "target": PAV, "bike_ids": str(self.store_bike)})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.bike(self.store_bike)["location"], PAV,
                         "с него возят переброской")

    def test_locations_page_marks_and_edits_the_kind(self):
        page = self.get_ok("/locations")
        self.assertIn('title="клиентам не показывается, выдачи и кассы нет">склад</span>',
                      page)
        self.client.post(f"/locations/{self.dek_id}", data={
            "city": "Казань", "address": "ул. Декабристов, 1",
            "kind": "warehouse"})
        self.assertEqual(self.place(self.dek_id)["kind"], "warehouse")
        # Галочку сняли - форма несёт только скрытое «point».
        self.client.post(f"/locations/{self.dek_id}", data={
            "city": "Казань", "address": "ул. Декабристов, 1", "kind": "point"})
        self.assertEqual(self.place(self.dek_id)["kind"], "point")
        # Форма без поля (открыта до обновления) вид не трогает.
        self.client.post(f"/locations/{self.store_id}", data={"city": "Казань"})
        self.assertEqual(self.place(self.store_id)["kind"], "warehouse")
        self.client.post("/locations", data={"name": "Гараж", "city": "Казань",
                                             "kind": "warehouse"})
        self.assertEqual(next(x for x in tw.run(self.crm.locations())
                              if x["name"] == "Гараж")["kind"], "warehouse")

    def place(self, location_id):
        return next(x for x in tw.run(self.crm.locations()) if x["id"] == location_id)


if __name__ == "__main__":
    unittest.main()
