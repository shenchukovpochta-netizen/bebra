"""Аналитика по точкам: дни парка по точке, строки сравнения точек,
точка по свободному тексту адреса.

Правило, которое здесь стерегут: по точке - те же три числа и те же
формулы, только ограниченные точкой, и сумма по точкам вместе с «без
точки» - ровно общее число панели. Отчёт, который не сходится с
соседним экраном, перестают читать, и дальше он только вредит.
Живая база - в tests/test_points_pg.py.
"""

from __future__ import annotations

import asyncio
import random
import sys
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import logic  # noqa: E402
from tests.fake_crm import FakeCrm  # noqa: E402

D = Decimal
T0 = datetime(2026, 9, 1, tzinfo=UTC)
PAV, ADO, CHI = "Павлюхина", "Адоратского", "Чистопольская"


def at(hours):
    return T0 + timedelta(hours=hours)


def status(bike, hours, to, row_id=0):
    return {"id": row_id, "bike_id": bike, "to_status": to, "changed_at": at(hours)}


def place(bike, hours, to, row_id=0):
    return {"id": row_id, "bike_id": bike, "to_location": to, "changed_at": at(hours)}


def summed(by_point):
    out: dict[str, Decimal] = {}
    for cell in by_point.values():
        for s, d in cell.items():
            out[s] = out.get(s, D(0)) + d
    return out


class TestDaysByPoint(unittest.TestCase):
    def test_move_without_status_change_splits_idle(self):
        """Свободный велосипед перевезли, статус не менялся: сутки простоя
        на старой точке, трое - на новой, а не все четверо на одной."""
        days = logic.days_by_status_location(
            [status(1, 0, "available")],
            [place(1, 0, PAV, 1), place(1, 24, CHI, 2)], at(0), at(96))
        self.assertEqual(days, {PAV: {"available": D(1)}, CHI: {"available": D(3)}})

    def test_rented_bike_stands_where_the_log_says(self):
        """Выдача ставит точку тем же моментом, что и статус: дни аренды
        - на точке аренды, простой до неё - на точке, где стоял."""
        days = logic.days_by_status_location(
            [status(1, 0, "available", 1), status(1, 48, "rented", 2)],
            [place(1, 0, PAV, 1), place(1, 48, ADO, 2)], at(0), at(96))
        self.assertEqual(days, {PAV: {"available": D(2)}, ADO: {"rented": D(2)}})

    def test_bike_without_location_log_is_no_point(self):
        days = logic.days_by_status_location([status(7, 0, "repair")], [], at(0), at(24))
        self.assertEqual(days, {None: {"repair": D(1)}})

    def test_point_log_starting_later_reaches_back(self):
        """История мест началась с внедрения, статусы старше: точка до
        внедрения - та, что стояла в карточке, а не «без точки»."""
        days = logic.days_by_status_location(
            [status(1, 0, "available")], [place(1, 72, PAV)], at(0), at(96))
        self.assertEqual(days, {PAV: {"available": D(4)}})

    def test_bike_taken_off_a_point_goes_to_no_point(self):
        days = logic.days_by_status_location(
            [status(1, 0, "maintenance")],
            [place(1, 0, PAV, 1), place(1, 12, None, 2)], at(0), at(24))
        self.assertEqual(days, {PAV: {"maintenance": D("0.5")},
                                None: {"maintenance": D("0.5")}})

    def test_window_cuts_both_logs(self):
        days = logic.days_by_status_location(
            [status(1, 0, "available", 1), status(1, 36, "rented", 2)],
            [place(1, 0, PAV, 1), place(1, 12, ADO, 2)], at(24), at(48))
        self.assertEqual(days, {ADO: {"available": D("0.5"), "rented": D("0.5")}})

    def test_sum_over_points_is_days_by_status(self):
        """Любой журнал: переезды без смены статуса, два события в одну
        секунду, велосипеды без журнала мест, статусы старше истории
        мест, окно поперёк всего - сумма по точкам равна общему числу."""
        rnd = random.Random(20260925)
        statuses = list(logic.BIKE_STATUSES)
        points = [PAV, ADO, CHI, None]
        for _ in range(300):
            status_log, location_log, row_id = [], [], 0
            for bike in range(1, rnd.randint(1, 6) + 1):
                hours = rnd.randint(-100, 50)
                for _ in range(rnd.randint(1, 6)):
                    row_id += 1
                    status_log.append(status(bike, hours, rnd.choice(statuses), row_id))
                    hours += rnd.choice((0, 1, 5, 17, 48))
                if rnd.random() < 0.2:
                    continue
                hours = rnd.randint(-50, 100)
                for _ in range(rnd.randint(1, 5)):
                    row_id += 1
                    location_log.append(place(bike, hours, rnd.choice(points), row_id))
                    hours += rnd.choice((0, 3, 24, 60))
            rnd.shuffle(status_log)
            rnd.shuffle(location_log)
            since = at(rnd.randint(-120, 100))
            until = since + timedelta(hours=rnd.randint(1, 300),
                                      microseconds=rnd.randint(0, 999999))
            # days_by_status в равное время верит порядку строк, база - id:
            # подаём ему журнал в порядке id, как его отдают база и заглушка.
            overall = logic.days_by_status(sorted(status_log, key=lambda r: r["id"]),
                                           since, until)
            by_point = summed(logic.days_by_status_location(status_log, location_log,
                                                            since, until))
            self.assertEqual(set(by_point), set(overall))
            for s, d in overall.items():
                self.assertAlmostEqual(by_point[s], d, delta=D("1e-9"), msg=s)


def bikes(*rows):
    return [{"status": s, "location": p} for s, p in rows]


PLACES = [{"id": 1, "name": PAV, "active": True, "sort": 10},
          {"id": 2, "name": ADO, "active": True, "sort": 20},
          {"id": 3, "name": CHI, "active": True, "sort": 30},
          {"id": 4, "name": "Старая", "active": False, "sort": 40},
          {"id": 5, "name": "Закрытая с историей", "active": False, "sort": 50}]


class TestPointsRows(unittest.TestCase):
    def report(self, **extra):
        return logic.points_rows(
            PLACES,
            bikes=bikes(("rented", PAV), ("available", PAV), ("rented", ADO),
                        ("repair", ADO), ("lost", ADO), ("available", None),
                        ("new", CHI)),
            days={PAV: {"rented": D(20), "available": D(10)},
                  ADO: {"rented": D(15), "repair": D(15)},
                  "Закрытая с историей": {"available": D(5)},
                  None: {"available": D(3)}},
            money={PAV: {"paid": D(12000), "charged": D(9000)},
                   ADO: {"paid": D(6000)}, None: {"paid": D(700)}},
            rentals={PAV: {"issued": 2, "renewals": 3, "active": 1},
                     ADO: {"issued": 1, "active": 1}},
            debt={ADO: {"clients": 1, "debt": D(1500)}, None: {"clients": 2, "debt": D(90)}},
            cash={ADO: D(2200), None: D(-100)},
            service={CHI: {"orders": 2, "client_orders": 1, "revenue": D(900)},
                     "Марс": {"orders": 1}},
            **extra)

    def test_total_is_the_panel_numbers(self):
        """«Итого» - та же fleet_metrics от всех дней и всех платежей, то
        есть ровно три числа сводки за тот же период."""
        report = self.report()
        total = report["total"]
        days = {"rented": D(35), "available": D(18), "repair": D(15)}
        self.assertEqual(total["metrics"], logic.fleet_metrics(days, D(18700)))
        self.assertEqual(total["days"], days)
        self.assertEqual(total["paid"], D(18700))
        self.assertEqual(total["fleet"], 5, "N сейчас: операционные, без lost и new")
        self.assertEqual((total["issued"], total["renewals"], total["active"]), (3, 3, 2))
        self.assertEqual((total["debt"], total["debtors"]), (D(1590), 3))
        self.assertEqual(total["cash"], D(2100))
        self.assertEqual((total["orders"], total["service_revenue"]), (3, D(900)))

    def test_rows_follow_the_directory_then_orphans_then_no_point(self):
        rows = self.report()["rows"]
        self.assertEqual([r["title"] for r in rows],
                         [PAV, ADO, CHI, "Закрытая с историей", "Марс", "без точки"])
        self.assertEqual([r["id"] for r in rows], [1, 2, 3, 5, None, None])
        self.assertEqual([r["orphan"] for r in rows],
                         [False, False, False, False, True, False])
        self.assertEqual([r["closed"] for r in rows],
                         [False, False, False, True, False, False])
        self.assertNotIn("Старая", [r["title"] for r in rows],
                         "закрытая точка без данных не мешает")

    def test_open_point_is_shown_even_empty_and_no_point_only_with_data(self):
        report = logic.points_rows(PLACES[:3], bikes=bikes(("available", PAV)),
                                   days={PAV: {"available": D(1)}}, money={})
        self.assertEqual([r["title"] for r in report["rows"]], [PAV, ADO, CHI])
        self.assertEqual(report["rows"][2]["metrics"]["idle_percent"], None)

    def test_point_numbers_are_the_point_three_numbers(self):
        rows = {r["key"]: r for r in self.report()["rows"]}
        pav, ado = rows[PAV], rows[ADO]
        self.assertEqual(pav["metrics"]["idle_percent"], 33.3)
        self.assertEqual(pav["metrics"]["avg_check"], D("600.00"))
        self.assertTrue(pav["metrics"]["check_ok"])
        self.assertFalse(pav["metrics"]["idle_ok"])
        self.assertEqual(ado["metrics"]["avg_check"], D("400.00"))
        self.assertEqual((pav["fleet"], ado["fleet"]), (2, 2))
        self.assertEqual(ado["counts"], {"rented": 1, "repair": 1, "lost": 1})
        self.assertEqual((pav["charged"], pav["renewals"], ado["cash"]),
                         (D(9000), 3, D(2200)))
        self.assertEqual((pav["active"], rows[CHI]["active"]), (1, 0),
                         "active - идущие аренды точки, а не флаг справочника")
        self.assertEqual(rows[None]["metrics"]["avg_check"], None,
                         "платежи без дней аренды - не чек")

    def test_history_note_only_when_the_period_is_older(self):
        settings = {"points_history_since": "2026-09-10 12:00:00+03"}
        start = datetime.fromisoformat(settings["points_history_since"])
        self.assertEqual(logic.points_history_from(settings, T0), start)
        self.assertIsNone(logic.points_history_from(settings, start + timedelta(days=1)))
        self.assertIsNone(logic.points_history_from({}, T0))
        self.assertIsNone(logic.points_history_from({"points_history_since": "x"}, T0))

    def test_point_months(self):
        months = [{"month": date(2026, 9, 1),
                   "days": {PAV: {"rented": D(10), "available": D(10)}},
                   "money": {PAV: {"paid": D(5000)}}},
                  {"month": date(2026, 8, 1), "days": {}, "money": {}}]
        got = logic.point_months(months, PAV)
        self.assertEqual([m["month"] for m in got], [date(2026, 9, 1), date(2026, 8, 1)])
        self.assertEqual((got[0]["idle_percent"], got[0]["avg_check"]), (50.0, D(500)))
        self.assertEqual((got[1]["idle_percent"], got[1]["avg_check"]), (None, None))


DIRECTORY = [
    {"name": PAV, "city": "Казань", "active": True,
     "public_title": "Май Байк — сервис и аренда, Павлюхина",
     "address": "г. Казань, ул. Павлюхина, 97А"},
    {"name": ADO, "city": "Казань", "active": True,
     "public_title": "Май Байк — сервис и аренда, Адоратского",
     "address": "г. Казань, ул. Адоратского, 11А"},
    {"name": "Лёвы Толстого", "city": "Казань", "active": True, "public_title": None,
     "address": None},
    {"name": "Баумана", "city": "Казань", "active": False, "address": "ул. Баумана, 1"},
]


class TestMatchLocation(unittest.TestCase):
    def test_free_text_finds_the_point(self):
        for text, want in (("Адоратского 15", ADO), ("адоратского", ADO),
                           ("ул. Павлюхина, 97 А", PAV), ("Павлюхина 97А, Казань", PAV),
                           ("11А", ADO), ("Адоратского, 11а", ADO),
                           ("Май Байк — сервис и аренда, Павлюхина", PAV),
                           ("Левы Толстого 3", "Лёвы Толстого"),
                           ("ЛЁВЫ ТОЛСТОГО", "Лёвы Толстого")):
            self.assertEqual(logic.match_location(text, DIRECTORY), want, text)

    def test_unknown_or_ambiguous_is_none(self):
        """Не сопоставилось - точку возврата не трогают: угадывать нельзя."""
        for text in ("", None, "   ", "Казань", "ул.", "Май Байк", "у входа",
                     "Адоратского/Павлюхина", "Адоратск", "Баумана 1"):
            self.assertIsNone(logic.match_location(text, DIRECTORY), text)

    def test_point_is_found_by_its_own_address_next_to_a_street_named_point(self):
        """Вторая точка на той же улице: «Адоратского 52» - это её адрес,
        а не «где-то на Адоратского». Раньше подходили обе точки, и
        велосипед оставался на точке аренды."""
        places = [*DIRECTORY, {"name": "Адоратского-2", "city": "Казань", "active": True,
                               "address": "г. Казань, ул. Адоратского, 52"}]
        for text, want in (("Адоратского 52", "Адоратского-2"),
                           ("ул. Адоратского, 52", "Адоратского-2"),
                           ("Адоратского, 52", "Адоратского-2"),
                           ("г. Казань, ул. Адоратского, 52", "Адоратского-2"),
                           ("Адоратского 15", ADO), ("Адоратского 11А", ADO),
                           ("ул. Адоратского", ADO), ("Адоратского-2", "Адоратского-2")):
            self.assertEqual(logic.match_location(text, places), want, text)
        # Не угадываем: две точки в тексте - None, как и раньше.
        for text in ("Адоратского/Павлюхина 97А", "Адоратского 52 или Павлюхина"):
            self.assertIsNone(logic.match_location(text, places), text)

    def test_exact_name_beats_a_looser_match(self):
        places = [{"name": "Центр", "active": True},
                  {"name": "Центр 2", "active": True}]
        self.assertEqual(logic.match_location("центр 2", places), "Центр 2")
        self.assertEqual(logic.match_location("Центр", places), "Центр")


class TestFakeCrmByPoint(unittest.TestCase):
    def test_fake_days_by_point_add_up(self):
        """Заглушка считает дни по точке тем же зеркалом, и сумма сходится
        с её же днями по статусам после выдачи, переезда и возврата."""
        crm = FakeCrm()

        async def story():
            a = await crm.create_bike(code="A", model="M", location=PAV)
            b = await crm.create_bike(code="B", model="M")
            client = await crm.create_client(full_name="К", phone="+79990000001")
            rid = await crm.create_rental(
                client_id=client, bike_id=a, tariff_id=None, tariff_name="t",
                period_days=7, price=D(3000), billing="manual",
                started_on=date.today(), contract_no=None, created_by="t",
                location=ADO)
            await crm.update_bike(b, location=CHI)
            await crm.close_rental(rid, closed_on=date.today(), note=None,
                                   return_location=CHI)
            for row in crm.status_log_ + crm.location_log_:
                row["changed_at"] -= timedelta(days=3)
            now = datetime.now(UTC)
            return (await crm.bike_days_by_location(now - timedelta(days=5), now),
                    await crm.bike_days_by_status(now - timedelta(days=5), now))

        by_point, overall = asyncio.run(story())
        self.assertAlmostEqual(by_point[CHI]["available"], D(6), delta=D("0.001"),
                               msg="оба свободных - трое суток на Чистопольской")
        for s, d in overall.items():
            self.assertAlmostEqual(summed(by_point)[s], d, delta=D("1e-6"), msg=s)


if __name__ == "__main__":
    unittest.main()
