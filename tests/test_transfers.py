"""Переброска между точками: прогноз на завтра и послезавтра и перевозки.

Спрос - выдачи модели на точке в тот же день недели за восемь недель плюс
открытые заявки, предложение - свободные сейчас и те, что вернутся по
«оплачено до». Здесь стерегут три вещи: арифметику прогноза (таблицей
случаев), то, что точка-источник не опускается ниже своего прогноза и
запаса, и то, что перевозит человек: панель ставит точку только отмеченным
свободным велосипедам, и журнал мест это записывает.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import logic  # noqa: E402

try:
    from app.crm import service
    from tests import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal
TODAY = date(2026, 9, 23)                    # среда: завтра четверг, потом пятница
THU, FRI = TODAY + timedelta(days=1), TODAY + timedelta(days=2)
PAV, ADO, DEK = "Павлюхина", "Адоратского", "Декабристов"
POINTS = (PAV, ADO, DEK)
MT, KG = "Monster Truck", "Kugoo"


def bikes(place, count, *, model=MT, status="available", spare=False, start=1):
    return [{"id": start + i, "code": f"{place[:3]}-{start + i}", "model": model,
             "location": place, "status": status, "spare": spare}
            for i in range(count)]


def rental(place, *, left, model=MT, intent=None, rid=1):
    """Идущая аренда, у которой «оплачено до» наступает через `left` дней."""
    until = TODAY + timedelta(days=left)
    return {"id": rid, "status": "active", "bike_id": 100 + rid, "location": place,
            "bike_model": model, "billed_until": until, "balance": D(0),
            "price": D(3000), "period_days": 7, "intent": intent, "intent_until": until}


def issued(place, weekday_date, times, *, model=MT, weeks=8):
    """`times` выдач на этот день недели, разложенных по прошлым неделям."""
    return [{"location": place, "model": model,
             "started_on": weekday_date - timedelta(days=7 * (1 + i % weeks)),
             "issued": 1} for i in range(times)]


def plan(**over):
    args = {"points": POINTS, "bikes": [], "rentals": [], "history": [],
            "bookings": [], "today": TODAY, "safety": 1}
    args.update(over)
    return logic.transfer_plan(**args)


def row(result, place, model=MT):
    return next(r for r in result["rows"] if (r["location"], r["model"]) == (place, model))


def moves(result):
    return [(m["source"], m["target"], m["model"], m["count"])
            for g in result["moves"] for m in g["lines"]]


class TestWeekdayDemand(unittest.TestCase):
    def test_average_is_per_week_not_per_busy_day(self):
        """Четверг без выдач - тоже четверг: делим на восемь недель."""
        got = logic.weekday_demand(issued(PAV, THU, 4), today=TODAY)
        self.assertEqual(got, {(PAV, MT, THU.weekday()): D("0.5")})

    def test_window_is_eight_weeks_without_today(self):
        rows = [{"location": PAV, "model": MT, "started_on": TODAY, "issued": 5},
                {"location": PAV, "model": MT,
                 "started_on": TODAY - timedelta(days=57), "issued": 5},
                {"location": PAV, "model": MT,
                 "started_on": TODAY - timedelta(days=56), "issued": 2},
                {"location": None, "model": MT, "started_on": TODAY - timedelta(days=7)},
                {"location": PAV, "model": "", "started_on": TODAY - timedelta(days=7)}]
        got = logic.weekday_demand(rows, today=TODAY)
        self.assertEqual(got, {(PAV, MT, TODAY.weekday()): D("0.25")},
                         "сегодня не кончился, девятая неделя не в окне, "
                         "без точки и модели спроса нет")

    def test_model_goes_through_the_catalogue(self):
        aliases = logic.model_aliases([{"title": "Городской H10",
                                        "factory_title": "Maikaolin H10"}])
        got = logic.weekday_demand(issued(PAV, THU, 8, model="Maikaolin H10"),
                                   today=TODAY, aliases=aliases)
        self.assertEqual(got, {(PAV, "Городской H10", THU.weekday()): D(1)})


class TestTransferPlan(unittest.TestCase):
    # (случай, аргументы, ждём: перевозки, нехватка по точкам)
    CASES = (
        ("излишек закрывает нехватку, запас остаётся",
         {"bikes": bikes(ADO, 4), "history": issued(PAV, THU, 8) + issued(PAV, FRI, 16)},
         [(ADO, PAV, MT, 3)], {PAV: 3}),
        ("ниже запаса не отдаём",
         {"bikes": bikes(ADO, 2), "history": issued(PAV, THU, 8) + issued(PAV, FRI, 16)},
         [(ADO, PAV, MT, 1)], {PAV: 3}),
        ("запас ноль - отдаём всё лишнее",
         {"bikes": bikes(ADO, 2), "safety": 0,
          "history": issued(PAV, THU, 8) + issued(PAV, FRI, 16)},
         [(ADO, PAV, MT, 2)], {PAV: 3}),
        ("возвраты закрывают спрос сами",
         {"bikes": bikes(ADO, 4),
          "rentals": [rental(PAV, left=0, rid=1), rental(PAV, left=1, rid=2)],
          "history": issued(PAV, THU, 8) + issued(PAV, FRI, 16)},
         [(ADO, PAV, MT, 1)], {PAV: 1}),
        ("«продлю» не возвращается",
         {"bikes": bikes(ADO, 4),
          "rentals": [rental(PAV, left=1, intent="renew")],
          "history": issued(PAV, THU, 8)},
         [(ADO, PAV, MT, 1)], {PAV: 1}),
        ("из розыска завтра не возвращаются",
         {"bikes": bikes(ADO, 4),
          "rentals": [{**rental(PAV, left=-20), "search_at": datetime(2026, 9, 10)}],
          "history": issued(PAV, THU, 8)},
         [(ADO, PAV, MT, 1)], {PAV: 1}),
        ("просроченная аренда без розыска ждётся сегодня",
         {"bikes": bikes(ADO, 4), "rentals": [rental(PAV, left=-3)],
          "history": issued(PAV, THU, 8)},
         [], {}),
        ("ремонт, бронь и подменные не отдаются",
         {"bikes": bikes(ADO, 1) + bikes(ADO, 1, status="repair", start=10)
          + bikes(ADO, 1, status="reserved", start=20)
          + bikes(ADO, 1, spare=True, start=30),
          "history": issued(PAV, THU, 8)},
         [], {PAV: 1}),
        ("чужая модель не везётся",
         {"bikes": bikes(ADO, 5, model=KG), "history": issued(PAV, THU, 8)},
         [], {PAV: 1}),
        ("источник держит свой прогноз",
         {"bikes": bikes(ADO, 4), "history": issued(ADO, THU, 16) + issued(PAV, THU, 8)},
         [(ADO, PAV, MT, 1)], {PAV: 1}),
        ("спрос меньше половины велосипеда - не спрос",
         {"bikes": bikes(ADO, 4), "history": issued(PAV, THU, 3)},
         [], {}),
        ("половина - уже велосипед",
         {"bikes": bikes(ADO, 4), "history": issued(PAV, THU, 4)},
         [(ADO, PAV, MT, 1)], {PAV: 1}),
        ("большой нехватке - первой",
         {"bikes": bikes(ADO, 5), "history": issued(PAV, THU, 24) + issued(DEK, THU, 16)},
         [(ADO, PAV, MT, 3), (ADO, DEK, MT, 1)], {PAV: 3, DEK: 2}),
        ("точка вне справочника не участвует",
         {"bikes": bikes("Склад", 5), "history": issued(PAV, THU, 8)},
         [], {PAV: 1}),
    )

    def test_cases(self):
        for title, args, want_moves, want_need in self.CASES:
            with self.subTest(title):
                got = plan(**args)
                self.assertEqual(moves(got), want_moves)
                self.assertEqual({r["location"]: r["need"] for r in got["rows"]
                                  if r["need"]}, want_need)

    def test_bookings_count_on_their_day(self):
        """Заявка на завтра - спрос завтра; сегодняшняя и просроченная -
        тоже завтра: клиент ещё ждёт. Дальше горизонта и снятые - нет."""
        booked = [{"status": "new", "location_name": PAV, "model": MT, "wanted_on": THU},
                  {"status": "new", "location_name": PAV, "model": MT,
                   "wanted_on": TODAY - timedelta(days=2)},
                  {"status": "new", "location_name": PAV, "model": MT, "wanted_on": FRI},
                  {"status": "new", "location_name": PAV, "model": MT,
                   "wanted_on": TODAY + timedelta(days=3)},
                  {"status": "cancelled", "location_name": PAV, "model": MT,
                   "wanted_on": THU},
                  {"status": "new", "location_name": None, "model": MT, "wanted_on": THU}]
        got = plan(bikes=bikes(ADO, 5), bookings=booked)
        pav = row(got, PAV)
        self.assertEqual([(s["booked"], s["want"]) for s in pav["steps"]],
                         [(2, 2), (1, 3)])
        self.assertEqual(moves(got), [(ADO, PAV, MT, 3)])

    def test_steps_add_up_day_by_day(self):
        got = plan(bikes=bikes(PAV, 1), rentals=[rental(PAV, left=2)],
                   history=issued(PAV, THU, 8) + issued(PAV, FRI, 4))
        pav = row(got, PAV)
        self.assertEqual([(s["on"], s["demand"], s["want"], s["have"]) for s in pav["steps"]],
                         [(THU, D("1.0"), 1, 1), (FRI, D("1.5"), 2, 2)])
        self.assertEqual((pav["free"], pav["back"], pav["need"], pav["spare"]), (1, 1, 0, 0))

    def test_catalogue_joins_factory_and_client_names(self):
        aliases = logic.model_aliases([{"title": "Городской H10",
                                        "factory_title": "Maikaolin H10"}])
        got = plan(bikes=bikes(ADO, 3, model="Maikaolin H10"), aliases=aliases,
                   bookings=[{"status": "new", "location_name": PAV,
                              "model": "Городской H10", "wanted_on": THU}])
        self.assertEqual(moves(got), [(ADO, PAV, "Городской H10", 1)])

    def test_longest_standing_bikes_are_offered_first(self):
        fleet = bikes(ADO, 6)
        idle = {1: 2, 2: 30, 3: None, 4: 11, 5: 11, 6: 7}
        got = plan(bikes=fleet, idle=idle, history=issued(PAV, THU, 16))
        line = got["moves"][0]["lines"][0]
        self.assertEqual([(b["code"], b["picked"]) for b in line["bikes"]],
                         [("Адо-2", True), ("Адо-4", True), ("Адо-5", False),
                          ("Адо-6", False), ("Адо-1", False)],
                         "двое дольше всех - отмечены, трое - на выбор")

    def test_two_shortages_get_different_bikes(self):
        got = plan(bikes=bikes(ADO, 5), history=issued(PAV, THU, 16) + issued(DEK, THU, 8),
                   idle={i: 10 - i for i in range(1, 6)})
        picked = {g["target"]: [b["code"] for m in g["lines"] for b in m["bikes"]
                                if b["picked"]] for g in got["moves"]}
        self.assertEqual(picked, {PAV: ["Адо-1", "Адо-2"], DEK: ["Адо-3"]})

    def test_label_names_route_and_models(self):
        got = plan(bikes=bikes(ADO, 4) + bikes(ADO, 3, model=KG, start=10),
                   history=issued(PAV, THU, 16) + issued(PAV, THU, 8, model=KG))
        self.assertEqual([g["label"] for g in got["moves"]],
                         [f"с {ADO} на {PAV}: 2 × {MT}, 1 × {KG}"])
        self.assertEqual(got["moves"][0]["count"], 3)

    def test_short_counts_what_transfers_cannot_cover(self):
        got = plan(bikes=bikes(ADO, 2), history=issued(PAV, THU, 24))
        self.assertEqual((row(got, PAV)["need"], row(got, PAV)["covered"], got["short"]),
                         (3, 1, 2))

    def test_empty_cells_are_not_rows(self):
        got = plan(bikes=bikes(ADO, 1, status="repair"))
        self.assertEqual(got["rows"], [])
        self.assertEqual(got["moves"], [])
        self.assertEqual(got["days"], [THU, FRI])

    def test_settings(self):
        self.assertEqual(logic.transfer_settings({}), {"safety": logic.TRANSFER_SAFETY})
        self.assertEqual(logic.transfer_settings({"transfer_safety": "0"}), {"safety": 0})
        self.assertEqual(logic.transfer_settings({"transfer_safety": "3"}), {"safety": 3})
        for junk in ("-1", "²", "abc", "999"):
            self.assertEqual(logic.transfer_settings({"transfer_safety": junk}),
                             {"safety": logic.TRANSFER_SAFETY}, junk)

    def test_task_line_leads_to_the_report(self):
        got = plan(bikes=bikes(ADO, 4), history=issued(PAV, THU, 16))
        tasks = logic.today_tasks(transfers=got["moves"], today=TODAY)
        self.assertEqual([(t["code"], t["count"], t["url"], t["title"]) for t in tasks],
                         [("transfer", 2, "/reports/points#transfer",
                           f"Перевезти с {ADO} на {PAV}: 2 × {MT}")])


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestTransferPages(tw.WebCase if HAVE_WEB else unittest.TestCase):
    """Блок на отчёте, строка на сводке и перевозка отмеченных."""

    def setUp(self):
        super().setUp()
        self.login()
        for name in (PAV, ADO):
            tw.run(self.crm.create_location(name=name, city="Казань", address=None,
                                            note=None))
        self.tariff_id = tw.run(self.crm.create_tariff("Неделя", 7, D(3000), None))
        self.ids = [tw.run(self.crm.create_bike(code=f"MT-{i}", model=MT, location=ADO))
                    for i in range(1, 5)]
        # Спрос на Павлюхина: две выдачи по каждому дню недели за восемь недель.
        today = date.today()
        for n in range(8 * 7 * 2):
            day = today - timedelta(days=1 + n // 2)
            cid = tw.run(self.crm.create_client(full_name=f"Курьер {n}",
                                                phone=f"+7900{n:07d}"))
            bid = tw.run(self.crm.create_bike(code=f"OLD-{n}", model=MT, location=PAV))
            rid = tw.run(self.crm.create_rental(
                client_id=cid, bike_id=bid, tariff_id=self.tariff_id, tariff_name="Неделя",
                period_days=7, price=D(3000), billing="manual", started_on=day,
                contract_no=None, created_by="t", location=PAV))
            tw.run(self.crm.close_rental(rid, closed_on=day, note=None, bike_status="sold",
                                         closed_by="t"))

    def test_report_suggests_and_dashboard_points_to_it(self):
        page = self.get_ok("/reports/points")
        self.assertIn('id="transfer"', page)
        self.assertIn(f"Перевезти с {ADO} на {PAV}: 3 × {MT}", page)
        self.assertIn('action="/bikes/transfer"', page)
        self.assertIn(f'<input type="hidden" name="source" value="{ADO}">', page)
        self.assertEqual(page.count('name="bike_ids"'), 4,
                         "три сверх запаса отмечены, четвёртый - на выбор")
        self.assertEqual(page.count(" checked>"), 3)
        self.assertIn("Прогноз по точкам и моделям", page)
        self.assertIn('action="/locations/transfer"', page)
        dash = self.get_ok("/")
        self.assertIn(f"Перевезти с {ADO} на {PAV}: 3 × {MT}", dash)
        self.assertIn('href="/reports/points#transfer"', dash)

    def test_operator_moves_what_he_checked(self):
        """Перевозятся только отмеченные и только свободные; журнал мест
        пишет переезд с автором, статус не трогается."""
        rented = self.ids[3]
        cid = tw.run(self.crm.create_client(full_name="Клиент", phone="+79990001122"))
        tw.run(service.open_rental(
            self.crm, client=tw.run(self.crm.client(cid)), bike=tw.run(self.crm.bike(rented)),
            tariff=tw.run(self.crm.tariff(self.tariff_id)), started_on=date.today(),
            contract_no=None, by="t", billing="manual"))
        r = self.client.post("/bikes/transfer", data={
            "source": ADO, "target": PAV,
            "bike_ids": [str(self.ids[0]), str(self.ids[1]), str(rented)]})
        self.assertEqual((r.status_code, r.headers["location"]),
                         (303, "/reports/points#transfer"))
        spots = {i: tw.run(self.crm.bike(i))["location"] for i in self.ids}
        self.assertEqual(spots, {self.ids[0]: PAV, self.ids[1]: PAV, self.ids[2]: ADO,
                                 rented: ADO})
        log = tw.run(self.crm.bike_location_log(self.ids[0]))
        self.assertEqual((log[0]["from_location"], log[0]["to_location"],
                          log[0]["changed_by"]), (ADO, PAV, "staff:admin"))
        self.assertEqual(tw.run(self.crm.bike(self.ids[0]))["status"], "available")
        page = self.get_ok("/reports/points")
        self.assertIn(f"С {ADO} на {PAV} перевезено 2: № MT-1, № MT-2.", page)
        self.assertIn(f"Не перевезены — уже не свободны или уже не на {ADO}", page)
        self.assertIn("№ MT-4", page)

    def test_stale_form_does_not_move_from_another_point(self):
        """Форма знает, откуда везут: велосипед, который тем временем увезли
        на третью точку, отсюда не «переезжает» - журнал мест не получает
        рейса, которого не было."""
        tw.run(self.crm.create_location(name=DEK, city="Казань", address=None, note=None))
        tw.run(self.crm.update_bike(self.ids[0], location=DEK, by="staff:other"))
        r = self.client.post("/bikes/transfer", data={
            "source": ADO, "target": PAV, "bike_ids": [str(self.ids[0]), str(self.ids[1])]})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(tw.run(self.crm.bike(self.ids[0]))["location"], DEK)
        self.assertEqual(tw.run(self.crm.bike(self.ids[1]))["location"], PAV)
        log = tw.run(self.crm.bike_location_log(self.ids[0]))
        self.assertEqual((log[0]["from_location"], log[0]["to_location"]), (ADO, DEK))
        page = self.get_ok("/reports/points")
        self.assertIn(f"уже не на {ADO}: № MT-1.", page)
        # форма без точки отправления (открыта до обновления) - ничего не везёт
        self.client.post("/bikes/transfer", data={"target": PAV,
                                                  "bike_ids": [str(self.ids[2])]})
        self.assertEqual(tw.run(self.crm.bike(self.ids[2]))["location"], ADO)
        self.assertIn("Форма перевозки устарела", self.get_ok("/reports/points"))

    def test_service_checks_the_source_in_the_update(self):
        """Условие - в самом UPDATE: велосипед, уехавший между чтением и
        записью (вторая вкладка), не переписывается."""
        stale = tw.run(self.crm.bike(self.ids[0]))
        tw.run(self.crm.update_bike(self.ids[0], location=PAV, by="staff:other"))
        got = tw.run(service.transfer_bikes(self.crm, [stale], source=ADO, target=DEK,
                                            by="staff:admin"))
        self.assertEqual((got["moved"], [b["code"] for b in got["skipped"]]),
                         ([], ["MT-1"]))
        self.assertEqual(tw.run(self.crm.bike(self.ids[0]))["location"], PAV)
        with self.assertRaises(service.ServiceError):
            tw.run(service.transfer_bikes(self.crm, [stale], source="", target=DEK,
                                          by="staff:admin"))

    def test_bad_target_and_empty_choice_move_nothing(self):
        for data in ({"source": ADO, "target": "Луна", "bike_ids": [str(self.ids[0])]},
                     {"source": ADO, "target": "", "bike_ids": [str(self.ids[0])]},
                     {"source": ADO, "target": PAV},
                     {"source": ADO, "target": PAV, "bike_ids": ["²", "999999"]},
                     {"source": ADO, "target": ADO, "bike_ids": [str(self.ids[0])]},
                     {"source": "Луна", "target": PAV, "bike_ids": [str(self.ids[0])]}):
            with self.subTest(data=data):
                self.assertEqual(self.client.post("/bikes/transfer", data=data).status_code,
                                 303)
                self.assertEqual(tw.run(self.crm.bike(self.ids[0]))["location"], ADO)

    def test_manager_sees_the_advice_but_cannot_move(self):
        manager = tw.run(self.crm.access_profile_by_code("manager"))
        tw.run(self.crm.create_staff("ivan", logic.hash_password("password-1"), "Иван",
                                     "manager", manager["id"]))
        self.client.post("/logout")
        self.login("ivan", "password-1")
        page = self.get_ok("/reports/points")
        self.assertIn(f"Перевезти с {ADO} на {PAV}", page)
        self.assertNotIn('action="/bikes/transfer"', page)
        self.assertNotIn('action="/locations/transfer"', page)
        r = self.client.post("/bikes/transfer", data={"source": ADO, "target": PAV,
                                                      "bike_ids": [str(self.ids[0])]})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.post("/locations/transfer",
                                          data={"transfer_safety": "0"}).status_code, 403)
        self.assertEqual(tw.run(self.crm.bike(self.ids[0]))["location"], ADO)

    def test_safety_setting_changes_the_advice(self):
        r = self.client.post("/locations/transfer", data={"transfer_safety": "0"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(tw.run(self.crm.settings())["transfer_safety"], "0")
        self.assertIn(f"Перевезти с {ADO} на {PAV}: 4 × {MT}", self.get_ok("/reports/points"))
        self.client.post("/locations/transfer", data={"transfer_safety": "²"})
        self.assertEqual(tw.run(self.crm.settings())["transfer_safety"], "0")
        self.assertIn("Запас на точке: целое число", self.get_ok("/reports/points"))

    def test_one_point_has_no_block(self):
        place = next(p for p in tw.run(self.crm.locations()) if p["name"] == PAV)
        tw.run(self.crm.update_location(place["id"], active=False))
        self.assertNotIn('id="transfer"', self.get_ok("/reports/points"))
        self.assertNotIn("Перевезти", self.get_ok("/"))

    def test_issues_by_day_takes_the_first_bike_of_a_swap(self):
        """Спрос - на ту модель, что выдали, а не на ту, что на руках после
        замены: спрашивали именно её."""
        cid = tw.run(self.crm.create_client(full_name="Клиент", phone="+79990002233"))
        kugoo = tw.run(self.crm.create_bike(code="KG-1", model=KG, location=ADO))
        rid = tw.run(service.open_rental(
            self.crm, client=tw.run(self.crm.client(cid)),
            bike=tw.run(self.crm.bike(self.ids[0])),
            tariff=tw.run(self.crm.tariff(self.tariff_id)), started_on=date.today(),
            contract_no=None, by="t", billing="manual"))
        tw.run(service.swap_bike(self.crm, tw.run(self.crm.rental(rid)),
                                 tw.run(self.crm.bike(kugoo)), reason="client", by="t"))
        rows = tw.run(self.crm.issues_by_day(date.today(), date.today() + timedelta(days=1)))
        self.assertEqual([(r["location"], r["model"], r["issued"]) for r in rows],
                         [(ADO, MT, 1)])


if __name__ == "__main__":
    unittest.main()
