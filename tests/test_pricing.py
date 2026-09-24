"""Цена зависит от модели: каталог, тарифы и выдача.

Monster Truck+ и Kugoo V3 Pro стоят по-разному, и плоский тариф
«неделя — 3 000» это различие терял. Здесь проверяется, что цена
берётся по модели, а смена модели на выдаче не оставляет чужую цену.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal
TRUCK = "Monster Truck + (Два АКБ)"
KUGOO = "Kugoo V3 Pro (Два АКБ)"


def tariffs() -> list[dict]:
    return [
        {"id": 1, "name": "Неделя", "model": TRUCK, "period_days": 7,
         "price": D(3000), "active": True},
        {"id": 2, "name": "Месяц", "model": TRUCK, "period_days": 30,
         "price": D(11000), "active": True},
        {"id": 3, "name": "Неделя", "model": KUGOO, "period_days": 7,
         "price": D(3500), "active": True},
        {"id": 4, "name": "Неделя", "model": None, "period_days": 7,
         "price": D(2900), "active": True},
    ]


class TestPricingLogic(unittest.TestCase):
    def test_model_has_its_own_prices(self):
        own = logic.tariffs_for_model(tariffs(), TRUCK)
        self.assertEqual([t["id"] for t in own], [1, 2])
        self.assertEqual([t["id"] for t in logic.tariffs_for_model(tariffs(), KUGOO)],
                         [3])

    def test_model_without_prices_falls_back_to_the_common_one(self):
        spare = logic.tariffs_for_model(tariffs(), "Велосипед без цены")
        self.assertEqual([t["id"] for t in spare], [4])
        self.assertEqual([t["id"] for t in logic.tariffs_for_model(tariffs(), "")],
                         [4], "без модели - тоже запасной")
        only_own = [t for t in tariffs() if t["model"]]
        self.assertEqual(logic.tariffs_for_model(only_own, "Чужая модель"), [],
                         "запасного нет - выдавать не по чему")

    def test_changing_the_model_keeps_the_period_and_takes_its_price(self):
        week_truck = tariffs()[0]
        moved = logic.match_tariff(tariffs(), week_truck, KUGOO)
        self.assertEqual(moved["id"], 3)
        self.assertEqual(moved["price"], D(3500))
        # Тот же тариф той же модели остаётся собой.
        self.assertEqual(logic.match_tariff(tariffs(), week_truck, TRUCK)["id"], 1)
        # У Kugoo нет месяца - подставлять нечего.
        month_truck = tariffs()[1]
        self.assertIsNone(logic.match_tariff(tariffs(), month_truck, KUGOO))
        self.assertIsNone(logic.match_tariff(tariffs(), None, KUGOO))

    def test_tiles_show_the_saving_of_a_longer_term(self):
        tiles = logic.tariff_tiles(logic.tariffs_for_model(tariffs(), TRUCK))
        by_days = {t["period_days"]: t for t in tiles}
        self.assertEqual(by_days[7]["saving"], D(0))
        # Месяц против четырёх недель по 3 000: 12 857,14 − 11 000.
        self.assertGreater(by_days[30]["saving"], D(0))
        self.assertEqual(by_days[30]["per_day"], logic.per_day(D(11000), 30))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestPricingPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.client_id = tw.run(self.crm.create_client(full_name="Иванов Иван",
                                                       phone="+79990000000",
                                                       tg_id=5001))
        self.truck = tw.run(self.crm.create_bike(code="B-1", model=TRUCK))
        self.kugoo = tw.run(self.crm.create_bike(code="B-2", model=KUGOO))
        for title in (TRUCK, KUGOO):
            tw.run(self.crm.create_bike_model(title=title, brand=None,
                                              factory_title=None, battery_slots=2,
                                              note=None))
        self.week_truck = tw.run(self.crm.create_tariff("Неделя", 7, D(3000), None,
                                                        model=TRUCK))
        self.week_kugoo = tw.run(self.crm.create_tariff("Неделя", 7, D(3500), None,
                                                        model=KUGOO))
        tw.run(self.crm.create_tariff("Месяц", 30, D(11000), None, model=TRUCK))

    def test_tariffs_screen_shows_the_model_and_refuses_duplicates(self):
        page = self.get_ok("/tariffs")
        self.assertIn(TRUCK, page)
        self.assertIn('value="3500"', page, "цены в полях правки - как есть")
        r = self.client.post("/tariffs", data={"name": "Неделя", "model": TRUCK,
                                               "period_days": "7", "price": "4000"})
        self.assertEqual(r.headers["location"], "/tariffs")
        self.assertIn("уже есть", self.get_ok("/tariffs"))
        self.assertEqual(len(tw.run(self.crm.tariffs())), 3)

    def test_wizard_shows_prices_of_the_chosen_model(self):
        # Названия моделей с «+» в адресе экранируются: иначе плюс
        # приедет пробелом, и модель не найдётся.
        base = f"/issue?client={self.client_id}"
        truck = self.get_ok(f"{base}&model={quote(TRUCK)}")
        self.assertIn("3 000 ₽", truck)
        self.assertNotIn("3 500 ₽", truck)
        kugoo = self.get_ok(f"{base}&model={quote(KUGOO)}")
        self.assertIn("3 500 ₽", kugoo)
        self.assertIn(f"цены модели «{KUGOO}»", kugoo)

    def test_changing_the_model_moves_the_price_with_it(self):
        page = self.get_ok(f"/issue?client={self.client_id}"
                           f"&tariff={self.week_truck}&model={quote(KUGOO)}"
                           f"&bike={self.kugoo}")
        self.assertIn("3 500 ₽", page, "цена стала ценой Kugoo")
        self.assertNotIn("3 000 ₽ / 7", page)

    def test_issue_charges_the_price_of_the_model(self):
        r = self.client.post("/issue", data={
            "client_id": str(self.client_id), "tariff_id": str(self.week_truck),
            "bike_id": str(self.kugoo), "started_on": date.today().isoformat(),
            "mileage": "10", "pay_amount": "0"})
        self.assertEqual(r.status_code, 303)
        rental = tw.run(self.crm.active_rental_of(self.client_id))
        self.assertEqual(rental["price"], D(3500),
                         "выдали Kugoo - списали по цене Kugoo")
        self.assertEqual(tw.run(self.crm.client_balance(self.client_id)), D(-3500))

    def test_model_without_a_price_is_refused_at_issue(self):
        odd = tw.run(self.crm.create_bike(code="B-9", model="Самокат без цены"))
        r = self.client.post("/issue", data={
            "client_id": str(self.client_id), "tariff_id": str(self.week_truck),
            "bike_id": str(odd), "started_on": date.today().isoformat(),
            "mileage": "10", "pay_amount": "0"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("нет тарифа", self.get_ok("/issue"))
        self.assertIsNone(tw.run(self.crm.active_rental_of(self.client_id)))

    def test_catalog_shows_prices_specs_and_strangers(self):
        model = tw.run(self.crm.bike_models())[0]
        self.client.post(f"/models/bikes/{model['id']}", data={
            "speed_kmh": "60", "range_km": "70", "motor_watt": "1200",
            "weight_kg": "52", "max_load_kg": "120", "wheel_size": "16 дюймов",
            "charge_hours": "6", "size_note": "120х43х110",
            "description": "Работаем 7/0", "note": ""})
        tw.run(self.crm.create_bike(code="B-8", model="Чужая модель"))
        page = self.get_ok("/models")
        self.assertIn("60 км/ч", page)
        self.assertIn("1200 Вт", page)
        self.assertIn("Неделя 3 000 ₽", page.replace(" ", " "))
        self.assertIn("Чужая модель", page)
        self.assertIn("которых нет в каталоге", page)

    def test_point_keeps_phone_hours_and_coordinates(self):
        self.client.post("/locations", data={
            "name": "Восстания", "city": "Казань",
            "public_title": "Май Байк — сервис и аренда, Восстания",
            "address": "г. Казань, ул. Восстания, 1", "phone": "+7 (904) 676-49-26",
            "hours": "пн-вс: 10:00-19:00", "lat": "55.8243", "lon": "49.1470",
            "note": ""})
        row = next(x for x in tw.run(self.crm.locations()) if x["name"] == "Восстания")
        self.assertEqual(row["phone"], "+7 (904) 676-49-26")
        self.assertEqual(row["hours"], "пн-вс: 10:00-19:00")
        self.assertAlmostEqual(row["lat"], 55.8243, places=4)
        page = self.get_ok("/locations")
        self.assertIn("пн-вс: 10:00-19:00", page)
        self.assertIn("55.8243", page)
        # мусор в координатах не сохраняется молча нулём
        self.client.post(f"/locations/{row['id']}", data={
            "address": row["address"], "lat": "север", "lon": "", "note": ""})
        self.assertIsNone(tw.run(self.crm.locations())[-1]["lat"])


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestRentalFormTariff(tw.WebCase):
    """Форма аренды и «сменить тариф» - те же правила цены, что на выдаче:
    тариф велосипеда, а не аккумулятора, и цена модели этого велосипеда."""

    def setUp(self):
        super().setUp()
        self.login()
        self.cid = tw.run(self.crm.create_client(full_name="Иванов Иван",
                                                 phone="+79990000000"))
        self.bike = tw.run(self.crm.create_bike(code="B-1", model="Monster",
                                                status="available"))
        tw.run(self.crm.create_tariff("Неделя", 7, D(3000), None))
        self.t_monster = tw.run(self.crm.create_tariff("Неделя Monster", 7, D(4200), None,
                                                       model="Monster"))
        self.t_kugoo = tw.run(self.crm.create_tariff("Неделя Kugoo", 7, D(2500), None,
                                                     model="Kugoo V3"))
        self.t_bat = tw.run(self.crm.create_tariff("Аккумулятор · неделя", 7, D(1170),
                                                   None, kind="battery"))

    def open(self, tariff_id):
        return self.client.post("/rentals", data={
            "client_id": self.cid, "bike_id": self.bike, "tariff_id": tariff_id,
            "started_on": date.today().isoformat(), "billing": "auto"})

    def test_battery_tariff_is_not_a_rent_price(self):
        self.assertNotIn("Аккумулятор · неделя", self.get_ok("/rentals/new"))
        r = self.open(self.t_bat)
        self.assertEqual(r.headers["location"], "/rentals/new")
        self.assertIsNone(tw.run(self.crm.active_rental_of(self.cid)))

    def test_other_models_price_is_swapped_for_this_one(self):
        self.open(self.t_kugoo)
        rental = tw.run(self.crm.active_rental_of(self.cid))
        self.assertEqual((rental["tariff_name"], rental["price"]),
                         ("Неделя Monster", D("4200.00")))

    def test_change_tariff_keeps_to_bike_prices(self):
        self.open(self.t_monster)
        rental = tw.run(self.crm.active_rental_of(self.cid))
        self.assertNotIn("Аккумулятор · неделя", self.get_ok(f"/rentals/{rental['id']}"))
        self.client.post(f"/rentals/{rental['id']}/tariff",
                         data={"tariff_id": self.t_bat, "billing": "auto"})
        self.assertEqual(tw.run(self.crm.rental(rental["id"]))["price"], D("4200.00"))
        self.client.post(f"/rentals/{rental['id']}/tariff",
                         data={"tariff_id": self.t_kugoo, "billing": "auto"})
        self.assertEqual(tw.run(self.crm.rental(rental["id"]))["price"], D("4200.00"),
                         "Kugoo-цена на Monster не встаёт")
