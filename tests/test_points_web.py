"""Точки в панели: отчёт «По точкам», страница точки, блок на сводке,
справочник точек (переименование, порядок), фильтры списков по точке,
метки точек на карте и третья точка во всех формах парка.

Правило, которое здесь стерегут: «Итого» отчёта - это ровно три числа
сводки за то же окно, а список точек у каждой формы и проверки один -
справочник. Отчёт, который не сходится со сводкой, перестают читать;
форма, которая знает только две точки, не даёт работать третьей.
Обвязка панели - tests/test_web.py, живая база - tests/test_web_pg.py.
"""

from __future__ import annotations

import ast
import csv
import html
import io
import json
import sys
import unittest
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import logic  # noqa: E402

try:
    from app.crm import import_xlsx as ix
    from app.crm import service
    from tests import test_web as tw
    from tests.test_import import ROWS, sheet
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal
PAV, ADO, DEK = "Павлюхина", "Адоратского", "Декабристов"
MSK = timezone(timedelta(hours=3))


class TestPeriod(unittest.TestCase):
    NOW = datetime(2026, 9, 25, 15, 30, tzinfo=MSK)

    def test_default_is_the_dashboard_window(self):
        """30 дней до этой минуты - окно трёх чисел сводки: иначе «Итого»
        совпадало бы с ними только случайно."""
        span = logic.report_period({}, now=self.NOW)
        self.assertEqual((span["kind"], span["end"], span["start"]),
                         ("days", self.NOW, self.NOW - timedelta(days=30)))
        self.assertEqual(span["query"], "")
        self.assertEqual((span["key"], span["prev_key"]), ("2026-09", "2026-08"))

    def test_month_current_ends_now_past_is_whole(self):
        span = logic.report_period({"month": "2026-09"}, now=self.NOW)
        self.assertEqual((span["start"], span["end"]),
                         (datetime(2026, 9, 1, tzinfo=MSK), self.NOW))
        self.assertIsNone(span["next_key"], "вперёд листать некуда")
        span = logic.report_period({"month": "2026-02"}, now=self.NOW)
        self.assertEqual((span["start"], span["end"], span["until"]),
                         (datetime(2026, 2, 1, tzinfo=MSK),
                          datetime(2026, 3, 1, tzinfo=MSK), date(2026, 2, 28)))
        self.assertEqual((span["query"], span["next_key"]), ("month=2026-02", "2026-03"))

    def test_custom_interval_is_inclusive_and_forgiving(self):
        span = logic.report_period({"since": "2026-09-10", "until": "2026-09-01"},
                                   now=self.NOW)
        self.assertEqual((span["kind"], span["since"], span["until"]),
                         ("custom", date(2026, 9, 1), date(2026, 9, 10)),
                         "перепутанные границы меняются местами")
        self.assertEqual(span["end"], datetime(2026, 9, 11, tzinfo=MSK),
                         "по дату включительно")
        self.assertEqual(span["query"], "since=2026-09-01&until=2026-09-10")
        only_since = logic.report_period({"since": "01.09.2026"}, now=self.NOW)
        self.assertEqual((only_since["since"], only_since["until"]),
                         (date(2026, 9, 1), date(2026, 9, 25)))
        for junk in ({"since": "вчера"}, {"month": "2099-01"}, {"until": "31.02.2026"}):
            span = logic.report_period(junk, now=self.NOW)
            self.assertIn(span["kind"], ("days", "month"), junk)
        self.assertEqual(logic.report_period({"month": "2099-01"}, now=self.NOW)["key"],
                         "2026-09", "будущий месяц - текущий")

    def test_month_windows(self):
        months = logic.month_windows(self.NOW, 3)
        self.assertEqual([m["month"] for m in months],
                         [date(2026, 9, 1), date(2026, 8, 1), date(2026, 7, 1)])
        self.assertEqual(months[0]["until"], self.NOW)
        self.assertEqual(months[1]["until"], datetime(2026, 9, 1, tzinfo=MSK))


class TestPointHelpers(unittest.TestCase):
    def test_point_card_of_an_empty_point(self):
        """Закрытой точки без данных среди строк отчёта нет - страница всё
        равно открывается, с нулями."""
        report = logic.points_rows([{"id": 1, "name": PAV, "active": False}],
                                   bikes=[], days={}, money={})
        self.assertEqual(report["rows"], [])
        card = logic.point_card(report, PAV, {"id": 1, "name": PAV, "active": False})
        self.assertEqual((card["title"], card["fleet"], card["paid"], card["closed"]),
                         (PAV, 0, D(0), True))
        self.assertIsNone(card["metrics"]["idle_percent"])
        self.assertEqual(logic.point_card(report, None)["title"], logic.NO_POINT_TITLE)

    def test_bikes_by_point(self):
        counts = logic.bikes_by_point([
            {"location": PAV, "status": "available"}, {"location": PAV, "status": "rented"},
            {"location": PAV, "status": "lost"}, {"location": None, "status": "new"}])
        self.assertEqual(counts[PAV], {"fleet": 2, "rented": 1, "cards": 3})
        self.assertEqual(counts[None], {"fleet": 0, "rented": 0, "cards": 1})

    def test_map_places_only_open_points_with_coordinates(self):
        places = logic.map_places([
            {"name": PAV, "active": True, "lat": 55.77, "lon": 49.14,
             "public_title": "Май Байк, Павлюхина", "address": "ул. Павлюхина, 97А"},
            {"name": ADO, "active": True, "lat": None, "lon": None},
            {"name": DEK, "active": False, "lat": 55.8, "lon": 49.1},
            {"name": "Ноль", "active": True, "lat": 0, "lon": 0}])
        self.assertEqual([p["name"] for p in places], [PAV])
        self.assertEqual(places[0]["title"], "Май Байк, Павлюхина")
        self.assertTrue(places[0]["url"].startswith("https://yandex.ru/maps/"))

    def test_check_location_without_list_uses_the_same_fallback(self):
        self.assertEqual(set(logic.point_choices([])), set(logic.LOCATIONS))
        self.assertTrue(logic.check_location(logic.point_choices([])[0]).ok)
        self.assertFalse(logic.check_location(DEK).ok)

    def test_no_template_reads_the_constant(self):
        """Список точек у формы - только справочник: константа в шаблоне
        значит, что третью точку завести можно, а выбрать нельзя."""
        root = Path(__file__).resolve().parent.parent / "app"
        for path in (root / "web" / "templates").glob("*.html"):
            self.assertNotIn("LOCATIONS", path.read_text(encoding="utf-8"), path.name)
        # В коде - по дереву разбора: упоминание в комментарии не в счёт.
        for path in (root / "web" / "app.py", root / "crm" / "import_xlsx.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            used = [node.lineno for node in ast.walk(tree)
                    if isinstance(node, ast.Attribute) and node.attr == "LOCATIONS"
                    or isinstance(node, ast.Name) and node.id == "LOCATIONS"]
            self.assertEqual(used, [], path.name)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class PointsWebCase(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        crm = self.crm
        self.pav = tw.run(crm.create_location(
            name=PAV, city="Казань", address="ул. Павлюхина, 97А", note=None, sort=10,
            phone="+7 (904) 676-49-26", hours="пн-вс: 10:00-19:00",
            lat=55.7669, lon=49.1486))
        self.ado = tw.run(crm.create_location(name=ADO, city="Казань",
                                              address="ул. Адоратского, 11А",
                                              note=None, sort=20))
        # Третья точка: её нет в logic.LOCATIONS.
        self.dek = tw.run(crm.create_location(name=DEK, city="Казань",
                                              address="ул. Декабристов, 1", note=None))
        self.tariff = tw.run(crm.tariff(tw.run(crm.create_tariff("Неделя", 7, D(3000),
                                                                 None))))

    def bike(self, code, location, status="available"):
        return tw.run(self.crm.create_bike(code=code, model="Kugoo V3",
                                           location=location, status=status))

    def rent(self, bike_id, name, phone, paid):
        client_id = tw.run(self.crm.create_client(full_name=name, phone=phone))
        rental_id = tw.run(service.open_rental(
            self.crm, client=tw.run(self.crm.client(client_id)),
            bike=tw.run(self.crm.bike(bike_id)), tariff=self.tariff,
            started_on=date.today(), contract_no=None, by="test"))
        tw.run(self.crm.add_ledger(client_id=client_id, kind="payment", amount=D(paid),
                                   rental_id=rental_id, method="card"))
        return client_id, rental_id

    def story(self):
        """Три точки, велосипед без точки и клиент без аренд. Журналы
        сдвинуты на 40 дней назад: окно «30 дней» целиком внутри истории,
        и числа выходят круглыми при любом «сейчас».

        Павлюхина: в аренде и свободен, 15 000 - простой 50 %, чек 500.
        Адоратского: в аренде и в ремонте, 9 000 - 50 %, чек 300.
        Декабристов: в аренде, 12 000 - 0 %, чек 400.
        Без точки: свободен и 500 от клиента без аренд - 100 %, чека нет.
        Итого: 90 из 180 дней простоя - 50 %, 36 500 / 90 = 405,56.
        """
        self.p1, self.p2 = self.bike("P-1", PAV), self.bike("P-2", PAV)
        self.a1, self.a2 = self.bike("A-1", ADO), self.bike("A-2", ADO, "repair")
        self.d1 = self.bike("D-1", DEK)
        self.n1 = self.bike("N-1", None)
        self.c1, self.r1 = self.rent(self.p1, "Павлов Пётр", "+79990000001", 15000)
        self.c2, self.r2 = self.rent(self.a1, "Адамов Антон", "+79990000002", 9000)
        self.c3, self.r3 = self.rent(self.d1, "Денисов Дмитрий", "+79990000003", 12000)
        self.c4 = tw.run(self.crm.create_client(full_name="Нилов Никита",
                                                phone="+79990000004"))
        tw.run(self.crm.add_ledger(client_id=self.c4, kind="payment", amount=D(500),
                                   method="card"))
        for row in self.crm.status_log_ + self.crm.location_log_:
            row["changed_at"] -= timedelta(days=40)

    def table(self, path):
        r = self.client.get(path)
        self.assertEqual(r.status_code, 200, path)
        return {row[0]: row for row in csv.reader(io.StringIO(r.text.lstrip("﻿")),
                                                  delimiter=";")}


class TestPointsReport(PointsWebCase):
    def test_three_points_and_totals_equal_the_dashboard(self):
        self.story()
        page = self.get_ok("/reports/points")
        for name in (PAV, ADO, DEK, logic.NO_POINT_TITLE, "Итого"):
            self.assertIn(name, page)
        self.assertIn(f'href="/reports/points/{self.dek}"', page)
        self.assertIn('href="/reports/points/none"', page)
        rows = self.table("/reports/points.csv")
        head = rows["Точка"]

        def cell(name, column):
            return rows[name][head.index(column)]

        self.assertEqual([cell(n, "Простой, %") for n in (PAV, ADO, DEK)],
                         ["50.0", "50.0", "0.0"])
        self.assertEqual([cell(n, "Чек/день") for n in (PAV, ADO, DEK)],
                         ["500,00", "300,00", "400,00"])
        self.assertEqual((cell(logic.NO_POINT_TITLE, "Выручка"),
                          cell(logic.NO_POINT_TITLE, "Чек/день")), ("500,00", ""),
                         "платёж клиента без аренд - «без точки», а не потерян")
        self.assertEqual([cell("ИТОГО", c) for c in ("Парк сейчас", "Простой, %",
                                                     "Чек/день", "Выручка")],
                         ["6", "50.0", "405,56", "36500,00"])
        # Сводка: те же числа в плитках трёх чисел и блок по точкам.
        dash = self.get_ok("/")
        self.assertIn("По точкам за 30 дней", dash)
        self.assertIn("50.0 %", dash)
        self.assertIn(logic.money(D("405.56")), dash)
        self.assertIn(logic.money(D(500)), dash)
        self.assertIn('href="/reports/points"', dash)

    def test_dashboard_block_needs_two_points(self):
        self.story()
        tw.run(self.crm.update_location(self.ado, active=False))
        self.assertIn("По точкам за 30 дней", self.get_ok("/"))
        tw.run(self.crm.update_location(self.dek, active=False))
        self.assertNotIn("По точкам за 30 дней", self.get_ok("/"),
                         "с одной точкой блок повторял бы три числа над ним")

    def test_idle_line_follows_the_directory_order(self):
        self.bike("X-1", DEK)
        self.bike("X-2", PAV)
        tw.run(self.crm.update_location(self.dek, sort=5))
        page = self.get_ok("/")
        self.assertLess(page.index(f"{DEK}: свободных"), page.index(f"{PAV}: свободных"))

    def test_point_page(self):
        self.story()
        tw.run(self.crm.add_ledger(client_id=self.c1, kind="charge", amount=D(-20000),
                                   rental_id=self.r1))
        page = self.get_ok(f"/reports/points/{self.pav}")
        self.assertIn("ул. Павлюхина, 97А", page)
        self.assertIn("пн-вс: 10:00-19:00", page)
        self.assertIn("Деньги по дням", page)
        self.assertIn("Три числа по месяцам", page)
        self.assertIn("Павлов Пётр", page, "должник точки")
        self.assertNotIn("Адамов Антон", page)
        self.assertIn("№ P-2", page, "стоит дольше всех на этой точке")
        self.assertNotIn("№ A-2", page)
        self.assertIn(f"/rentals?location={quote(PAV)}", page)
        self.assertIn(f"/orders?location={quote(PAV)}", page)
        none = self.get_ok("/reports/points/none")
        self.assertIn("Без точки", none)
        self.assertIn("№ N-1", none)
        self.assertEqual(self.client.get("/reports/points/99999").status_code, 404)
        self.assertEqual(self.client.get("/reports/points/abc").status_code, 404)

    def test_closed_point_page_opens_with_zeros(self):
        tw.run(self.crm.update_location(self.ado, active=False))
        page = self.get_ok(f"/reports/points/{self.ado}")
        self.assertIn("закрыта", page)
        self.assertNotIn(f'href="/reports/points/{self.ado}"',
                         self.get_ok("/reports/points"),
                         "закрытая точка без данных в сравнении не нужна")

    def test_period_is_carried_to_the_point_and_the_export(self):
        self.story()
        page = self.get_ok("/reports/points?month=2026-01")
        self.assertIn("01.2026", page)
        self.assertIn(f"/reports/points/{self.pav}?month=2026-01", page)
        self.assertIn("/reports/points.xlsx?month=2026-01", page)
        rows = self.table("/reports/points.csv?month=2026-01")
        self.assertEqual(rows["ИТОГО"][rows["Точка"].index("Выручка")], "0,00",
                         "в январе денег не было")
        page = self.get_ok("/reports/points?since=2026-01-01&until=2026-01-31")
        self.assertIn("01.01.2026 — 31.01.2026", page)
        self.assertIn("since=2026-01-01&amp;until=2026-01-31", page)

    def test_history_note_is_one_honest_line(self):
        self.story()
        self.assertNotIn("История мест ведётся", self.get_ok("/reports/points"))
        since = datetime.now(UTC) - timedelta(days=3)
        tw.run(self.crm.set_setting("points_history_since",
                                    since.isoformat(timespec="seconds"), by="test"))
        self.assertIn("История мест ведётся", self.get_ok("/reports/points"))
        self.assertIn("История мест ведётся", self.get_ok(f"/reports/points/{self.pav}"))

    def test_exports(self):
        self.story()
        r = self.client.get("/reports/points.xlsx")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r.headers["content-type"])
        self.assertEqual(self.client.get("/reports/points.pdf").status_code, 404)

    def test_money_only_with_finance(self):
        """Простой по точкам нужен и тому, кому рубли не показывают, - но
        рублей он не увидит ни на странице, ни в выгрузке."""
        self.story()
        profile = tw.run(self.crm.create_access_profile(
            "Отчёты без денег", {"sections": {"reports": "view"}}))
        tw.run(self.crm.create_staff("rep", logic.hash_password("rep-pass-123"),
                                     name="Аналитик", role="manager", profile_id=profile))
        self.client.post("/logout")
        self.login("rep", "rep-pass-123")
        page = self.get_ok("/reports/points")
        self.assertIn(PAV, page)
        self.assertNotIn("Чек/день", page)
        self.assertNotIn(logic.money(D(15000)), page)
        head = self.table("/reports/points.csv")["Точка"]
        self.assertIn("Простой, %", head)
        for column in ("Чек/день", "Выручка", "Долг", "Наличные", "Выручка сервиса"):
            self.assertNotIn(column, head)
        point = self.get_ok(f"/reports/points/{self.pav}")
        self.assertNotIn("Деньги по дням", point)
        self.assertNotIn("Должники точки", point)

    def test_report_needs_the_reports_section(self):
        profile = tw.run(self.crm.create_access_profile(
            "Только парк", {"sections": {"bikes": "edit"}}))
        tw.run(self.crm.create_staff("park", logic.hash_password("park-pass-123"),
                                     name="Парк", role="manager", profile_id=profile))
        self.client.post("/logout")
        self.login("park", "park-pass-123")
        for path in ("/reports/points", "/reports/points.csv", "/reports/points/none"):
            self.assertEqual(self.client.get(path).status_code, 403, path)

    def test_tab_in_reports(self):
        self.assertIn('href="/reports/points"', self.get_ok("/reports"))


class TestLocationsPage(PointsWebCase):
    def test_rename_cascades_through_cards_rentals_and_history(self):
        self.story()
        r = self.client.post(f"/locations/{self.dek}/rename",
                             data={"name": "Декабристов 1"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("переименована", self.get_ok("/locations"))
        self.assertEqual(tw.run(self.crm.bike(self.d1))["location"], "Декабристов 1")
        self.assertEqual(tw.run(self.crm.rental(self.r3))["location"], "Декабристов 1")
        self.assertEqual({x["to_location"] for x in tw.run(
            self.crm.bike_location_log(self.d1))}, {"Декабристов 1"},
            "история - та же точка под новым именем, а не переезд")
        rows = self.table("/reports/points.csv")
        self.assertIn("Декабристов 1", rows)
        self.assertNotIn(DEK, rows)

    def test_rename_refuses_a_taken_name(self):
        self.story()
        self.client.post(f"/locations/{self.dek}/rename", data={"name": PAV})
        self.assertIn("уже есть", self.get_ok("/locations"))
        self.assertEqual(tw.run(self.crm.bike(self.d1))["location"], DEK)
        self.client.post(f"/locations/{self.dek}/rename", data={"name": "none"})
        self.assertIn("«без точки»", self.get_ok("/locations"))
        self.assertEqual(self.client.post("/locations/99999/rename",
                                          data={"name": "Новая"}).status_code, 404)

    def test_sort_bike_counts_and_analytics_link(self):
        self.story()
        r = self.client.post(f"/locations/{self.dek}", data={
            "city": "Казань", "address": "ул. Декабристов, 1", "sort": "5"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual([p["name"] for p in tw.run(self.crm.locations())][:1], [DEK])
        page = self.get_ok("/locations")
        self.assertIn(f'href="/reports/points/{self.pav}"', page)
        self.assertIn("в аренде 1", page)
        self.assertIn("Переименовать", page)
        form = self.get_ok("/bikes/new")
        self.assertLess(form.index(f'value="{DEK}"'), form.index(f'value="{PAV}"'),
                        "порядок справочника - порядок выпадающих списков")
        self.client.post(f"/locations/{self.dek}", data={"sort": "много"})
        self.assertEqual(tw.run(self.crm.locations())[0]["sort"], 5)

    def test_closed_point_keeps_its_bikes(self):
        self.story()
        self.client.post(f"/locations/{self.dek}/toggle")
        self.assertEqual(tw.run(self.crm.bike(self.d1))["location"], DEK)
        page = self.get_ok("/locations")
        self.assertIn("закрыта", page)


class TestThirdPointEverywhere(PointsWebCase):
    def test_bike_form_filter_purchase_and_stock_take(self):
        form = self.get_ok("/bikes/new")
        self.assertIn(f'<option value="{DEK}"', form)
        r = self.client.post("/bikes", data={"code": "B-3", "model": "Truck+",
                                             "location": DEK})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(tw.run(self.crm.bike_by_code("B-3"))["location"], DEK)
        listing = self.client.get("/bikes", params={"location": DEK}).text
        self.assertIn("B-3", listing)
        self.assertIn(f'<option value="{DEK}" selected', listing)
        # Закупка партии на третью точку.
        self.assertIn(f'<option value="{DEK}"', self.get_ok("/assets"))
        r = self.client.post("/assets", data={"codes": "Z-1 Z-2", "model": "Truck+",
                                              "location": DEK})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(tw.run(self.crm.bike_by_code("Z-2"))["location"], DEK)
        self.client.post("/assets", data={"codes": "Z-3", "model": "Truck+",
                                          "location": "Марс"})
        self.assertIsNone(tw.run(self.crm.bike_by_code("Z-3")))
        # Пересчёт третьей точки: партия и карточка встали «на сборке», и
        # ждать на точке нечего - нужен свободный велосипед.
        self.bike("D-9", DEK)
        self.assertIn(f'<option value="{DEK}"', self.get_ok("/stock-takes"))
        r = self.client.post("/stock-takes", data={"scope": "location",
                                                   "location": DEK, "what": "bikes"})
        take = tw.run(self.crm.open_stock_take())
        self.assertEqual((take or {}).get("location"), DEK, r.headers.get("location"))

    def test_stock_take_refuses_an_unknown_point(self):
        self.client.post("/stock-takes", data={"scope": "location", "location": "Марс"})
        self.assertIsNone(tw.run(self.crm.open_stock_take()))

    def test_closed_point_stays_in_filters_and_battery_card(self):
        self.bike("X-1", DEK)
        tw.run(self.crm.update_location(self.dek, active=False))
        self.assertIn(f'<option value="{DEK}"', self.get_ok("/bikes"),
                      "на закрытой точке ещё стоит велосипед - найти его надо")
        self.assertNotIn(f'<option value="{DEK}"', self.get_ok("/bikes/new"))
        battery_id = tw.run(self.crm.create_battery(code="BAT-1", location=DEK,
                                                    status="available"))
        self.assertIn(f'<option value="{DEK}"', self.get_ok(f"/batteries/{battery_id}"))
        r = self.client.post(f"/batteries/{battery_id}/edit",
                             data={"code": "BAT-1", "location": DEK})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(tw.run(self.crm.battery(battery_id))["location"], DEK,
                         "сохранение карточки не стирает закрытую точку")

    def test_import_knows_the_third_point(self):
        self.assertEqual(ix._location("Ремонт (декабристов)", [PAV, ADO, DEK]), DEK)
        self.assertEqual(ix._location("Свободен (Павлюхина 2)",
                                      [PAV, "Павлюхина 2"]), "Павлюхина 2",
                         "длинное имя раньше короткого")
        rows = [dict(ROWS[7], статус=f"Ждет сдачи ({DEK})")]
        tw.run(ix.run(self.crm, sheet(rows), apply=True, by="import:test"))
        bike = tw.run(self.crm.bike_by_code("8"))
        self.assertEqual(bike["location"], DEK)


class TestListFilters(PointsWebCase):
    def test_rentals_by_point_with_export_sort_and_saved_view(self):
        self.story()
        page = self.client.get("/rentals", params={"location": PAV}).text
        self.assertIn("Павлов Пётр", page)
        self.assertNotIn("Адамов Антон", page)
        self.assertIn(f'<option value="{PAV}" selected', page)
        self.assertIn("Итого 1", page)
        text = self.client.get("/rentals.csv", params={"location": ADO}).text
        self.assertIn("Адамов Антон", text)
        self.assertNotIn("Павлов Пётр", text)
        self.assertIn("Точка выдачи", text)
        self.assertNotIn("Павлов", self.client.get("/rentals",
                                                   params={"location": "none"}).text)
        ordered = self.client.get("/rentals", params={"sort": "location"}).text
        self.assertLess(ordered.index("Адамов Антон"), ordered.index("Павлов Пётр"))
        r = self.client.post("/views", data={"section": "/rentals", "name": "Адоратского",
                                             "query": f"location={ADO}"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("★ Адоратского", self.get_ok("/rentals"))

    def test_orders_by_point_with_export(self):
        self.story()
        for bike_id in (self.p2, self.a2):
            tw.run(service.open_order(
                self.crm, bike=tw.run(self.crm.bike(bike_id)), payer="own", client=None,
                complaint="стук", object_note=None, tech_id=None, estimate=D(0),
                by="test"))
        tw.run(service.open_order(
            self.crm, bike=None, payer="client", client=tw.run(self.crm.client(self.c4)),
            complaint="самокат", object_note="Ninebot", tech_id=None, estimate=D(0),
            by="test"))
        page = self.client.get("/orders", params={"location": ADO}).text
        self.assertIn("A-2", page)
        self.assertNotIn("P-2", page)
        none = self.client.get("/orders", params={"location": "none"}).text
        self.assertIn("Ninebot", none)
        self.assertNotIn("A-2", none)
        text = self.client.get("/orders.csv", params={"location": PAV}).text
        self.assertIn("P-2", text)
        self.assertNotIn("A-2", text)
        self.assertIn(f"/orders.xlsx?status=&payer=&location={quote(PAV)}",
                      html.unescape(self.client.get("/orders",
                                                    params={"location": PAV}).text),
                      "выгружают то, что видят")


class TestMapPlaces(PointsWebCase):
    def test_points_with_coordinates_are_labelled_markers(self):
        page = self.get_ok("/map")
        self.assertIn(f'"name": {json.dumps(PAV)}', page)
        self.assertIn("map-place", page)
        self.assertIn("точка выдачи", page)
        self.assertIn(logic.map_url(55.7669, 49.1486), page.replace("&amp;", "&"),
                      "ссылка на точку работает и без подложки карты")
        self.assertNotIn(json.dumps(ADO), page, "без координат на карту не попадает")
        self.assertNotIn(ADO, page)
        self.assertIn("map-offline", page, "без подложки карты - честная строка")
        # Встроенный скрипт карты цел: закрывающий тег в его комментарии
        # обрывал скрипт на середине, и карта не рисовалась вовсе.
        script = page.split("<script>", 1)[1].split("</script>", 1)[0]
        self.assertIn("var places", script)
        self.assertIn("fitBounds", script)


if __name__ == "__main__":
    unittest.main()
