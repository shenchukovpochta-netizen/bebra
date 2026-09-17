"""Батареи как отдельная сущность и справочники, на которых они держатся.

Батарея живёт своей жизнью: ломается чаще рамы, кочует между
велосипедами и стоит трети велосипеда. Пока она была счётчиком в
карточке велосипеда, «где батарея» никто ответить не мог, а
амортизация считалась по среднему. Здесь - карточка, статусы своим
журналом, выдача вместе с велосипедом и замена на точке.
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
TODAY = date(2026, 9, 17)


def battery(**over) -> dict:
    row = {"id": 1, "code": "A-1", "status": "available", "cycles": 0,
           "purchased_on": date(2026, 3, 17), "service_months": 15,
           "purchase_price": D(9000), "model_price": None}
    row.update(over)
    return row


class TestBatteryLogic(unittest.TestCase):
    def test_wear_is_counted_by_term_and_tired_by_cycles(self):
        rows = logic.battery_rows([battery(), battery(id=2, code="A-2", cycles=600),
                                   battery(id=3, code="A-3", purchased_on=None)],
                                  today=TODAY)
        by_code = {r["code"]: r for r in rows}
        self.assertEqual(by_code["A-1"]["wear"], 40.0, "6 месяцев из 15")
        self.assertFalse(by_code["A-1"]["tired"])
        self.assertTrue(by_code["A-2"]["tired"], "от 500 циклов - пора смотреть")
        self.assertIsNone(by_code["A-3"]["wear"], "без даты покупки износ не считается")
        self.assertEqual(rows[0]["code"], "A-2", "уставшие - первыми")

    def test_wear_stops_at_a_hundred_and_marks_tired(self):
        old = logic.battery_rows([battery(purchased_on=date(2023, 1, 1))],
                                 today=TODAY)[0]
        self.assertEqual(old["wear"], 100.0)
        self.assertTrue(old["tired"], "отслужила срок - тоже пора смотреть")

    def test_summary_counts_operational_and_tired(self):
        rows = logic.battery_rows([battery(), battery(id=2, code="A-2", status="rented"),
                                   battery(id=3, code="A-3", status="lost"),
                                   battery(id=4, code="A-4", cycles=700)], today=TODAY)
        out = logic.battery_summary(rows)
        self.assertEqual(out["total"], 4)
        self.assertEqual(out["operational"], 3, "утерянная в обороте не считается")
        self.assertEqual(out["rented"], 1)
        self.assertEqual(out["tired"], 1)

    def test_amortization_falls_back_to_the_model_price(self):
        self.assertEqual(logic.battery_amortization(battery()), D(600))
        self.assertEqual(logic.battery_amortization(
            battery(purchase_price=None, model_price=D(9000))), D(600))
        self.assertIsNone(logic.battery_amortization(
            battery(purchase_price=None)), "цены нет - и суммы нет")

    def test_a_battery_is_counted_once_either_by_the_bike_or_by_itself(self):
        bike = {"id": 7, "status": "available", "purchase_price": D(47000),
                "residual_price": D(5000), "service_months": 24,
                "battery_price": D(9000), "battery_count": 2,
                "battery_service_months": 15}
        # счётчик в карточке: рама 1750 + две батареи по 600
        self.assertEqual(logic.amortization_total([bike]), D(2950))
        # те же батареи заведены поштучно: у велосипеда остаётся рама
        own = [battery(bike_id=7, status="rented"),
               battery(id=2, code="A-2", bike_id=7, status="available")]
        self.assertEqual(logic.amortization_total([bike], own), D(2950))
        self.assertEqual(logic.frame_amortization(bike), D(1750))
        # батарея на складе, не привязанная к велосипеду, считается сверху
        spare = [battery(id=3, code="A-3")]
        self.assertEqual(logic.amortization_total([bike], spare), D(3550))
        # списанная не считается вовсе
        self.assertEqual(logic.amortization_total(
            [bike], [battery(id=4, code="A-4", status="written_off")]), D(2950))

    def test_location_is_checked_against_the_directory(self):
        self.assertTrue(logic.check_location("", ["Павлюхина"]).ok)
        self.assertIsNone(logic.check_location("", ["Павлюхина"]).value)
        self.assertEqual(logic.check_location("Павлюхина", ["Павлюхина"]).value,
                         "Павлюхина")
        self.assertFalse(logic.check_location("Марс", ["Павлюхина"]).ok)
        # пустой справочник - константа парка
        self.assertTrue(logic.check_location(logic.LOCATIONS[0]).ok)

    def test_compat_matrix_has_a_cell_for_every_pair(self):
        bikes = [{"id": 1, "title": "Truck+"}, {"id": 2, "title": "Kugoo V3"}]
        batteries = [{"id": 10, "title": "48V 20Ah"}, {"id": 11, "title": "60V 30Ah"}]
        out = logic.compat_matrix(bikes, batteries, [
            {"bike_model_id": 1, "battery_model_id": 10, "primary_fit": True},
            {"bike_model_id": 2, "battery_model_id": 10, "primary_fit": False}])
        rows = {r["bike"]["title"]: r["cells"] for r in out["rows"]}
        self.assertEqual([c["fits"] for c in rows["Truck+"]], [True, False])
        self.assertTrue(rows["Truck+"][0]["primary"])
        self.assertFalse(rows["Kugoo V3"][0]["primary"], "подходит, но не основная")
        self.assertEqual([m["title"] for m in out["batteries"]],
                         ["48V 20Ah", "60V 30Ah"])


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestBatteryService(tw.WebCase):
    """Выдача, замена и возврат - через service, без панели."""

    def setUp(self):
        super().setUp()
        self.seed()
        self.model_id = tw.run(self.crm.create_battery_model(
            title="48V 20Ah", brand="Sanyo", voltage=48, capacity=D(20),
            price=D(9000), service_months=15, note=None))
        self.a1 = tw.run(self.crm.create_battery(code="A-1", model_id=self.model_id,
                                                 by="t"))
        self.a2 = tw.run(self.crm.create_battery(code="A-2", model_id=self.model_id,
                                                 by="t"))
        client = tw.run(self.crm.client(self.client_id))
        bike = tw.run(self.crm.bike(self.bike_id))
        tariff = tw.run(self.crm.tariff(self.tariff_id))
        self.rental_id = tw.run(service.open_rental(
            self.crm, client=client, bike=bike, tariff=tariff,
            started_on=date.today(), contract_no=None, by="t"))

    def issue(self, ids):
        bike = tw.run(self.crm.bike(self.bike_id))
        return tw.run(service.issue_with_batteries(
            self.crm, self.rental_id, bike=bike, battery_ids=ids, by="t"))

    def test_issued_battery_follows_the_bike_and_comes_back_on_close(self):
        self.assertEqual(self.issue([self.a1]), 1)
        row = tw.run(self.crm.battery(self.a1))
        self.assertEqual(row["status"], "rented")
        self.assertEqual(row["rental_id"], self.rental_id)
        self.assertEqual(row["bike_code"], "B-1")
        self.assertEqual(row["client_name"], "Иванов Иван")
        tw.run(service.close_rental(self.crm, tw.run(self.crm.rental(self.rental_id)),
                                    closed_on=date.today(), note=None, by="t"))
        back = tw.run(self.crm.battery(self.a1))
        self.assertEqual(back["status"], "available")
        self.assertIsNone(back["rental_id"])
        self.assertEqual(back["cycles"], 1, "возврат - это один цикл")

    def test_a_busy_battery_is_not_issued_twice(self):
        self.issue([self.a1])
        with self.assertRaises(service.ServiceError) as err:
            self.issue([self.a1])
        self.assertIn("A-1", str(err.exception))
        with self.assertRaises(service.ServiceError):
            self.issue([9999])

    def test_swap_returns_the_old_one_to_repair(self):
        self.issue([self.a1])
        rental = tw.run(self.crm.rental(self.rental_id))
        tw.run(service.swap_battery(self.crm, rental, tw.run(self.crm.battery(self.a1)),
                                    tw.run(self.crm.battery(self.a2)), by="t"))
        old = tw.run(self.crm.battery(self.a1))
        new = tw.run(self.crm.battery(self.a2))
        self.assertEqual(old["status"], "repair")
        self.assertIsNone(old["rental_id"])
        self.assertEqual(old["cycles"], 1)
        self.assertEqual(new["status"], "rented")
        self.assertEqual(new["rental_id"], self.rental_id)

    def test_swap_refuses_the_same_battery_and_a_closed_rental(self):
        self.issue([self.a1])
        rental = tw.run(self.crm.rental(self.rental_id))
        a1 = tw.run(self.crm.battery(self.a1))
        with self.assertRaises(service.ServiceError):
            tw.run(service.swap_battery(self.crm, rental, a1, a1, by="t"))
        # занятая батарея на замену не годится
        with self.assertRaises(service.ServiceError):
            tw.run(service.swap_battery(self.crm, rental, None, a1, by="t"))
        tw.run(service.close_rental(self.crm, rental, closed_on=date.today(),
                                    note=None, by="t"))
        closed = tw.run(self.crm.rental(self.rental_id))
        with self.assertRaises(service.ServiceError):
            tw.run(service.swap_battery(self.crm, closed, None,
                                        tw.run(self.crm.battery(self.a2)), by="t"))

    def test_status_journal_keeps_every_move(self):
        self.issue([self.a1])
        rental = tw.run(self.crm.rental(self.rental_id))
        tw.run(service.swap_battery(self.crm, rental, tw.run(self.crm.battery(self.a1)),
                                    tw.run(self.crm.battery(self.a2)), by="staff:kolya"))
        log = tw.run(self.crm.battery_status_log(self.a1))
        self.assertEqual([x["to_status"] for x in log], ["repair", "rented", "available"])
        self.assertEqual(log[0]["from_status"], "rented")
        self.assertEqual(log[0]["changed_by"], "staff:kolya")


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestBatteryPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.model_id = tw.run(self.crm.create_battery_model(
            title="48V 20Ah", brand="Sanyo", voltage=48, capacity=D(20),
            price=D(9000), service_months=15, note=None))

    def create(self, **over):
        data = {"code": "A-1", "model_id": str(self.model_id), "serial_no": "SN-1",
                "location": "Павлюхина", "purchase_price": "9000",
                "purchased_on": (date.today() - timedelta(days=200)).isoformat(),
                "service_months": "15", "cycles": "0", "note": ""}
        data.update(over)
        return self.client.post("/batteries", data=data)

    def test_battery_is_created_and_shown_in_the_list(self):
        r = self.create()
        self.assertEqual(r.status_code, 303)
        battery_id = int(r.headers["location"].rsplit("/", 1)[1])
        page = self.get_ok("/batteries")
        self.assertIn("A-1", page)
        self.assertIn("48V 20Ah", page)
        card = self.get_ok(f"/batteries/{battery_id}")
        self.assertIn("Батарея A-1", card)
        self.assertIn("SN-1", card)
        self.assertIn("600 ₽ в месяц", card, "амортизация 9000 за 15 месяцев")

    def test_duplicate_code_is_refused(self):
        self.create()
        r = self.create()
        self.assertEqual(r.headers["location"], "/batteries/new")
        self.assertIn("уже есть", self.get_ok("/batteries/new"))

    def test_unknown_location_is_refused(self):
        r = self.create(location="Марс")
        self.assertEqual(r.headers["location"], "/batteries/new")
        self.assertIn("Точка", self.get_ok("/batteries"))

    def test_status_is_changed_by_hand_except_for_the_rented_one(self):
        battery_id = int(self.create().headers["location"].rsplit("/", 1)[1])
        self.client.post(f"/batteries/{battery_id}/status", data={"status": "repair"})
        self.assertEqual(tw.run(self.crm.battery(battery_id))["status"], "repair")
        self.client.post(f"/batteries/{battery_id}/status", data={"status": "rented"})
        self.assertIn("Статус батареи", self.get_ok(f"/batteries/{battery_id}"))
        tw.run(self.crm.update_battery(battery_id, status="rented", by="t"))
        self.client.post(f"/batteries/{battery_id}/status", data={"status": "available"})
        self.assertEqual(tw.run(self.crm.battery(battery_id))["status"], "rented")
        self.assertIn("её снимает возврат", self.get_ok(f"/batteries/{battery_id}"))

    def test_issue_offers_free_batteries_and_hands_them_over(self):
        battery_id = int(self.create().headers["location"].rsplit("/", 1)[1])
        page = self.get_ok(f"/issue?client={self.client_id}&tariff={self.tariff_id}"
                           f"&bike={self.bike_id}")
        self.assertIn("Батареи", page)
        self.assertIn(f'name="battery_ids" value="{battery_id}"', page)
        r = self.client.post("/issue", data={
            "client_id": str(self.client_id), "tariff_id": str(self.tariff_id),
            "bike_id": str(self.bike_id), "started_on": date.today().isoformat(),
            "mileage": "100", "pay_amount": "3000", "pay_method": "sbp",
            "battery_ids": str(battery_id)})
        self.assertEqual(r.status_code, 303)
        row = tw.run(self.crm.battery(battery_id))
        self.assertEqual(row["status"], "rented")
        self.assertEqual(row["bike_id"], self.bike_id)
        rental_id = int(r.headers["location"].rsplit("/", 1)[1])
        card = self.get_ok(f"/rentals/{rental_id}")
        self.assertIn("Батареи у клиента", card)
        self.assertIn("A-1", card)

    def test_swap_on_the_rental_card(self):
        first = int(self.create().headers["location"].rsplit("/", 1)[1])
        second = int(self.create(code="A-2").headers["location"].rsplit("/", 1)[1])
        self.client.post("/issue", data={
            "client_id": str(self.client_id), "tariff_id": str(self.tariff_id),
            "bike_id": str(self.bike_id), "started_on": date.today().isoformat(),
            "mileage": "100", "pay_amount": "0", "battery_ids": str(first)})
        rental_id = tw.run(self.crm.active_rental_of(self.client_id))["id"]
        r = self.client.post(f"/rentals/{rental_id}/battery",
                             data={"old_id": str(first), "battery_id": str(second),
                                   "old_status": "repair"})
        self.assertEqual(r.headers["location"], f"/rentals/{rental_id}")
        self.assertEqual(tw.run(self.crm.battery(first))["status"], "repair")
        self.assertEqual(tw.run(self.crm.battery(second))["status"], "rented")
        self.assertIn("Батарея заменена на A-2", self.get_ok(f"/rentals/{rental_id}"))
        # без выбора новой - понятная ошибка, а не пятисотка
        self.client.post(f"/rentals/{rental_id}/battery", data={"old_id": str(second)})
        self.assertIn("Выберите батарею", self.get_ok(f"/rentals/{rental_id}"))

    def test_directories_add_points_and_models(self):
        r = self.client.post("/locations", data={"name": "Горького", "city": "Казань",
                                                 "address": "ул. Горького, 1", "note": ""})
        self.assertEqual(r.headers["location"], "/locations")
        self.assertIn("Горького", self.get_ok("/locations"))
        names = tw.run(self.crm.location_names())
        self.assertEqual(names, ["Горького"])
        # справочник заполнен - форма батареи предлагает его, а не константу
        self.assertIn("Горького", self.get_ok("/batteries/new"))
        self.assertEqual(self.create(location="Павлюхина").headers["location"],
                         "/batteries/new")

        self.client.post("/models/bikes", data={"title": "Truck+", "brand": "Wolt",
                                                "factory_title": "", "battery_slots": "2",
                                                "note": ""})
        page = self.get_ok("/models")
        self.assertIn("Truck+", page)
        self.assertIn("48V 20Ah", page)
        bike_model = tw.run(self.crm.bike_models())[0]
        self.client.post("/models/compat", data={"bike_model_id": str(bike_model["id"]),
                                                 "battery_model_id": str(self.model_id),
                                                 "mode": "primary"})
        fit = tw.run(self.crm.compat_for_bike_model("Truck+"))
        self.assertEqual([m["title"] for m in fit], ["48V 20Ah"])
        self.assertTrue(fit[0]["primary_fit"])
        self.client.post("/models/compat", data={"bike_model_id": str(bike_model["id"]),
                                                 "battery_model_id": str(self.model_id),
                                                 "mode": "none"})
        self.assertEqual(tw.run(self.crm.compat_for_bike_model("Truck+")), [])

    def test_closed_point_stays_in_the_cards(self):
        self.client.post("/locations", data={"name": "Горького", "city": "Казань",
                                             "address": "", "note": ""})
        loc = tw.run(self.crm.locations())[0]
        self.create(location="Горького")
        self.client.post(f"/locations/{loc['id']}/toggle")
        self.assertEqual(tw.run(self.crm.location_names()), [],
                         "закрытая точка из форм уходит")
        self.assertIn("Горького", self.get_ok("/batteries"),
                      "но в карточках остаётся: велосипеды и батареи там же")

    def test_compat_narrows_the_issue_list(self):
        battery_id = int(self.create().headers["location"].rsplit("/", 1)[1])
        other = tw.run(self.crm.create_battery_model(
            title="60V 30Ah", brand=None, voltage=60, capacity=None,
            price=D(12000), service_months=15, note=None))
        alien = int(self.create(code="A-9", model_id=str(other))
                    .headers["location"].rsplit("/", 1)[1])
        model_id = tw.run(self.crm.create_bike_model(
            title="Kugoo V3", brand=None, factory_title=None, battery_slots=2, note=None))
        tw.run(self.crm.set_compat(model_id, self.model_id, fits=True, primary_fit=True))
        page = self.get_ok(f"/issue?client={self.client_id}&tariff={self.tariff_id}"
                           f"&bike={self.bike_id}")
        self.assertIn(f'name="battery_ids" value="{battery_id}"', page)
        self.assertNotIn(f'name="battery_ids" value="{alien}"', page,
                         "чужая батарея в раму не встанет")


if __name__ == "__main__":
    unittest.main()
