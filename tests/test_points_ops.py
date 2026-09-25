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
                                                 address="ул. Павлюхина, 97А",
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
        self.assertIn(f"location={quote(DEK)}", self.get_ok("/bookings"))
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
        self.assertIn(f"как у велосипеда ({PAV})", page)
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


if __name__ == "__main__":
    unittest.main()
