"""Инструменты списков там, где их не было: пересчёты, тревоги, рабочий
стол сервиса, виды работ, карта. Поиск, сортировка кликом, подвал
с итогом и выгрузка - тем же макросом, что у парка и аренд.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal
_run = tw.run if HAVE_WEB else None


class TestRowsSearch(unittest.TestCase):
    ROWS = [{"code": "B-1", "model": "Kugoo V3", "tech": "Хомяков И."},
            {"code": "B-2", "model": "Truck+", "tech": ""}]

    def test_substring_in_any_field_case_insensitive(self):
        self.assertEqual([r["code"] for r in logic.rows_search(self.ROWS, "kugoo",
                                                               ("code", "model"))],
                         ["B-1"])
        self.assertEqual([r["code"] for r in logic.rows_search(self.ROWS, "хомяк",
                                                               ("code", "tech"))],
                         ["B-1"])
        self.assertEqual(len(logic.rows_search(self.ROWS, "b-", ("code",))), 2)
        self.assertEqual(logic.rows_search(self.ROWS, "нет", ("code", "model")), [])

    def test_empty_query_returns_copies_of_everything(self):
        out = logic.rows_search(self.ROWS, "  ", ("code",))
        self.assertEqual(len(out), 2)
        out[0]["code"] = "X"
        self.assertEqual(self.ROWS[0]["code"], "B-1", "исходные строки не трогаем")

    def test_tracker_state_title(self):
        self.assertEqual(logic.tracker_state_title({"alarm": True, "moving": True}), "тревога")
        self.assertEqual(logic.tracker_state_title({"moving": True}), "едет")
        self.assertEqual(logic.tracker_state_title({"offline": True}), "молчит")
        self.assertEqual(logic.tracker_state_title({}), "стоит")

    def test_alert_summary_counts_the_last_day_including_closed(self):
        now = datetime(2026, 9, 18, 12, tzinfo=UTC)
        rows = [{"open": True, "needs": True, "level": "urgent", "state": "new",
                 "created_at": now - timedelta(hours=2)},
                {"open": False, "needs": False, "level": "yellow", "state": "new",
                 "created_at": now - timedelta(hours=20)},
                {"open": True, "needs": True, "level": "yellow", "state": "new",
                 "created_at": now - timedelta(days=3)}]
        got = logic.alert_summary(rows, now=now)
        self.assertEqual(got["day"], 2, "закрытая за сутки тоже считается")
        self.assertEqual(got["open"], 2)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestStockTakesList(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        for note in ("Летний обход", "После кражи"):
            self.client.post("/stock-takes", data={"scope": "all", "location": "",
                                                   "note": note})
            take = _run(self.crm.open_stock_take())
            self.client.post(f"/stock-takes/{take['id']}/close", data={})

    def test_search_sort_footer_and_export(self):
        page = self.get_ok("/stock-takes")
        self.assertIn("Итого 2", page)
        self.assertIn("sort=missing", page, "заголовки сортируются")
        found = self.get_ok("/stock-takes?q=кражи")
        self.assertIn("После кражи", found)
        self.assertNotIn("Летний обход", found)
        self.assertEqual(self.client.get("/stock-takes?sort=expected&dir=desc").status_code, 200)
        csv = self.client.get("/stock-takes.csv?q=кражи")
        self.assertEqual(csv.status_code, 200)
        self.assertIn("Что считали", csv.text)
        self.assertIn("После кражи", csv.text)
        self.assertNotIn("Летний обход", csv.text)
        self.assertEqual(self.client.get("/stock-takes.xlsx").status_code, 200)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestAlertsList(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.b1 = _run(self.crm.create_bike(code="B-1", model="Kugoo V3"))
        self.b2 = _run(self.crm.create_bike(code="B-2", model="Truck+"))
        t1 = _run(self.crm.create_tracker(device_id="1001", alias="Метка 1", bike_id=self.b1))
        t2 = _run(self.crm.create_tracker(device_id="1002", alias="Метка 2", bike_id=self.b2))
        _run(self.crm.raise_alert(tracker_id=t1, kind="moving", note="30 км/ч",
                                  bike_id=self.b1, lat=None, lon=None, level="urgent"))
        _run(self.crm.raise_alert(tracker_id=t2, kind="offline", note="молчит",
                                  bike_id=self.b2, lat=None, lon=None, level="yellow"))

    def test_search_sort_day_counter_and_export(self):
        page = self.get_ok("/alerts")
        self.assertIn("Итого 2", page)
        self.assertIn("за сутки", page)
        found = self.get_ok("/alerts?q=B-2")
        self.assertIn("B-2", found)
        self.assertNotIn("№ B-1", found)
        self.assertEqual(self.client.get("/alerts?sort=level&dir=desc").status_code, 200)
        csv = self.client.get("/alerts.csv?view=open&q=B-1")
        self.assertEqual(csv.status_code, 200)
        self.assertIn("Уровень", csv.text)
        self.assertIn("B-1", csv.text)
        self.assertNotIn("B-2", csv.text)

    def test_export_needs_the_section(self):
        profile = _run(self.crm.create_access_profile(
            "Только клиенты", {"sections": {"clients": "view"}}))
        _run(self.crm.create_staff("clients", logic.hash_password("clients-pass-1"),
                                   name="Клиентщик", role="manager", profile_id=profile))
        self.client.post("/logout")
        self.login("clients", "clients-pass-1")
        self.assertEqual(self.client.get("/alerts.csv").status_code, 403)
        self.assertEqual(self.client.get("/map.csv").status_code, 403)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestServiceDeskList(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.b2 = _run(self.crm.create_bike(code="B-2", model="Truck+"))
        _run(self.crm.update_bike(self.bike_id, status="repair", by="т"))
        _run(self.crm.update_bike(self.b2, status="repair", by="т"))
        self.client.post("/orders", data={"bike_id": self.b2, "payer": "client",
                                          "client_id": self.client_id, "estimate": "1500",
                                          "complaint": "не едет"})

    def test_client_and_estimate_columns_search_and_export(self):
        page = self.get_ok("/service")
        self.assertIn("Иванов Иван", page, "клиент клиентского ремонта виден на доске")
        self.assertIn("1 500 ₽", page, "смета видна на доске")
        self.assertIn("Итого 2", page)
        found = self.get_ok("/service?q=иванов")
        self.assertIn("B-2", found)
        self.assertNotIn("№ B-1", found)
        self.assertEqual(self.client.get("/service?sort=estimate&dir=desc").status_code, 200)
        csv = self.client.get("/service.csv")
        self.assertEqual(csv.status_code, 200)
        self.assertIn("Смета", csv.text)
        self.assertIn("Иванов Иван", csv.text)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestWorkTypesList(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        for title, cat, price in (("Замена покрышки", "Ходовая", "300"),
                                  ("Диагностика электрики", "Электрика", "600")):
            self.client.post("/work-types", data={"title": title, "category": cat,
                                                  "minutes": "15", "price": price,
                                                  "node": ""})

    def test_search_sort_and_export(self):
        page = self.get_ok("/work-types")
        self.assertIn("Итого", page)
        self.assertIn("sort=used", page)
        found = self.get_ok("/work-types?q=электр")
        self.assertIn("Диагностика электрики", found)
        self.assertNotIn("Замена покрышки", found)
        self.assertEqual(self.client.get("/work-types?sort=price&dir=desc").status_code, 200)
        csv = self.client.get("/work-types.csv")
        self.assertEqual(csv.status_code, 200)
        self.assertIn("Использований", csv.text)
        self.assertIn("Замена покрышки", csv.text)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestMapList(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.b2 = _run(self.crm.create_bike(code="B-2", model="Truck+"))
        _run(self.crm.create_tracker(device_id="1001", alias="Метка 1", bike_id=self.bike_id,
                                     last_seen=datetime.now(UTC), lat=55.79, lon=49.12,
                                     speed=D(0), voltage=D("12.6")))
        _run(self.crm.create_tracker(device_id="1002", alias="Метка 2", bike_id=self.b2,
                                     last_seen=datetime.now(UTC), lat=55.80, lon=49.13,
                                     speed=D(0), voltage=D("12.6")))

    def test_search_narrows_the_map_and_the_list(self):
        page = self.get_ok("/map?q=B-2")
        self.assertIn("№ B-2", page)
        self.assertNotIn("№ B-1", page)
        self.assertIn("Итого 1", page)
        self.assertNotIn('"B-1"', page, "точка ненайденного велосипеда с карты уходит")
        self.assertEqual(self.client.get("/map?sort=bike&dir=desc").status_code, 200)
        csv = self.client.get("/map.csv")
        self.assertEqual(csv.status_code, 200)
        self.assertIn("Состояние", csv.text)
        self.assertIn("B-1", csv.text)
        self.assertIn("стоит", csv.text)


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
