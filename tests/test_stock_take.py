"""Пересчёт техники: ведомость ПРТ и что она делает с парком.

Главное, что проверяется: ведомость снимает парк один раз при открытии,
недостача сама по себе никого не теряет, а найденный потерянный велосипед
возвращается в парк - ради этого пересчёт и затевается.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False


class TestTakeLogic(unittest.TestCase):
    def test_number_is_human_readable(self):
        self.assertEqual(logic.take_no(1), "ПРТ-000001")
        self.assertEqual(logic.take_no(510003), "ПРТ-510003")

    def test_expected_bikes_skip_rented_and_retired(self):
        bikes = [
            {"id": 1, "code": "B-3", "status": "available", "location": "Павлюхина"},
            {"id": 2, "code": "B-1", "status": "rented", "location": "Павлюхина"},
            {"id": 3, "code": "B-2", "status": "repair", "location": "Адоратского"},
            {"id": 4, "code": "B-4", "status": "lost", "location": "Павлюхина"},
            {"id": 5, "code": "B-5", "status": "sold", "location": None},
            {"id": 6, "code": "B-6", "status": "reserved", "location": None},
        ]
        codes = [b["code"] for b in logic.expected_bikes(bikes, scope="all")]
        # В аренде - у курьера, продан и потерян - вне парка.
        self.assertEqual(codes, ["B-2", "B-3", "B-6"])

    def test_expected_bikes_by_location(self):
        bikes = [
            {"id": 1, "code": "B-1", "status": "available", "location": "Павлюхина"},
            {"id": 2, "code": "B-2", "status": "available", "location": "Адоратского"},
            {"id": 3, "code": "B-3", "status": "maintenance", "location": None},
        ]
        rows = logic.expected_bikes(bikes, scope="location", location="Павлюхина")
        self.assertEqual([b["code"] for b in rows], ["B-1"])

    def test_counts_and_progress(self):
        items = [{"state": "found"}, {"state": "found"}, {"state": "expected"},
                 {"state": "missing"}, {"state": "extra"}]
        counts = logic.take_counts(items)
        # Лишние в «ожидалось» не входят: их в парке на этой точке не ждали.
        self.assertEqual(counts["total"], 4)
        self.assertEqual(counts["found"], 2)
        self.assertEqual(counts["left"], 1)
        self.assertEqual(logic.take_progress(counts), 75)
        self.assertEqual(logic.take_progress(logic.take_counts([])), 0)

    def test_title_and_open_state(self):
        self.assertEqual(logic.take_title({"scope": "all"}), "Весь парк")
        self.assertEqual(
            logic.take_title({"scope": "location", "location": "Адоратского"}),
            "Точка Адоратского")
        self.assertTrue(logic.take_is_open({"status": "open"}))
        self.assertFalse(logic.take_is_open({"status": "done"}))
        self.assertFalse(logic.take_is_open(None))

    def test_scope_is_checked(self):
        self.assertTrue(logic.check_scope("all").ok)
        self.assertFalse(logic.check_scope("батареи").ok)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestStockTakeInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.b2 = tw.run(self.crm.create_bike(code="B-2", model="Kugoo V3",
                                              location="Павлюхина"))
        self.b3 = tw.run(self.crm.create_bike(code="B-3", model="Kugoo V3",
                                              location="Адоратского"))

    def start(self, **over):
        data = {"scope": "all", "location": "", "note": "Плановый пересчёт"}
        data.update(over)
        return self.client.post("/stock-takes", data=data)

    def take(self):
        return tw.run(self.crm.open_stock_take())

    def items(self, take_id):
        return tw.run(self.crm.take_items(take_id))

    # ─── открытие ───

    def test_start_snapshots_the_park(self):
        r = self.start()
        self.assertEqual(r.status_code, 303)
        take = self.take()
        self.assertEqual(take["no"], "ПРТ-000001")
        self.assertEqual(take["expected"], 3)
        states = {i["state"] for i in self.items(take["id"])}
        self.assertEqual(states, {"expected"})

    def test_rented_bike_is_not_expected_on_site(self):
        """Велосипед у курьера на точке не лежит - и в недостачу не идёт."""
        tw.run(self.crm.update_bike(self.bike_id, status="rented"))
        self.start()
        take = self.take()
        self.assertEqual(take["expected"], 2)
        self.assertNotIn(self.bike_id,
                         [i["bike_id"] for i in self.items(take["id"])])

    def test_second_take_is_refused(self):
        self.start()
        r = self.start()
        self.assertIn("Пересчёт уже идёт", self.get_ok(r.headers["location"]))
        self.assertEqual(len(tw.run(self.crm.stock_takes())), 1)

    def test_location_scope_needs_a_location(self):
        r = self.start(scope="location", location="")
        self.assertIn("выберите точку", self.get_ok(r.headers["location"]))

    def test_location_scope_counts_only_that_point(self):
        self.start(scope="location", location="Адоратского")
        take = self.take()
        self.assertEqual(take["expected"], 1)
        self.assertEqual(self.items(take["id"])[0]["bike_id"], self.b3)

    def test_empty_scope_is_refused(self):
        self.start(scope="location", location="Павлюхина")
        self.client.post(f"/stock-takes/{self.take()['id']}/close")
        tw.run(self.crm.update_bike(self.b2, status="sold"))
        r = self.start(scope="location", location="Павлюхина")
        self.assertIn("Считать нечего", self.get_ok(r.headers["location"]))

    # ─── отметки ───

    def test_scan_marks_expected_bike_found(self):
        self.start()
        take = self.take()
        r = self.client.post(f"/stock-takes/{take['id']}/scan", data={"code": "B-2"})
        page = self.get_ok(r.headers["location"])
        self.assertIn("B-2 на месте", page)
        item = tw.run(self.crm.take_item_of_bike(take["id"], self.b2))
        self.assertEqual(item["state"], "found")

    def test_scan_of_a_bike_from_another_point_is_extra(self):
        self.start(scope="location", location="Адоратского")
        take = self.take()
        r = self.client.post(f"/stock-takes/{take['id']}/scan", data={"code": "B-2"})
        self.assertIn("не ждали здесь", self.get_ok(r.headers["location"]))
        item = tw.run(self.crm.take_item_of_bike(take["id"], self.b2))
        self.assertEqual(item["state"], "extra")

    def test_scan_of_unknown_code_is_recorded(self):
        """Номер, которого в парке нет: запись остаётся, разбираются потом."""
        self.start()
        take = self.take()
        r = self.client.post(f"/stock-takes/{take['id']}/scan", data={"code": "B-999"})
        self.assertIn("такого номера в парке нет", self.get_ok(r.headers["location"]))
        extra = [i for i in self.items(take["id"]) if i["state"] == "extra"]
        self.assertEqual(len(extra), 1)
        self.assertEqual(extra[0]["code"], "B-999")
        self.assertIsNone(extra[0]["bike_id"])

    def test_second_scan_of_the_same_bike_does_not_double_the_count(self):
        self.start()
        take = self.take()
        self.client.post(f"/stock-takes/{take['id']}/scan", data={"code": "B-2"})
        r = self.client.post(f"/stock-takes/{take['id']}/scan", data={"code": "B-2"})
        self.assertIn("уже отмечен", self.get_ok(r.headers["location"]))
        counts = logic.take_counts(self.items(take["id"]))
        self.assertEqual(counts["found"], 1)
        self.assertEqual(counts["total"], 3)

    def test_rescan_of_extra_stays_extra(self):
        """Лишний остаётся лишним: иначе нашлось бы больше, чем ждали."""
        self.start(scope="location", location="Адоратского")
        take = self.take()
        self.client.post(f"/stock-takes/{take['id']}/scan", data={"code": "B-2"})
        r = self.client.post(f"/stock-takes/{take['id']}/scan", data={"code": "B-2"})
        self.assertIn("уже записан лишним", self.get_ok(r.headers["location"]))
        counts = logic.take_counts(self.items(take["id"]))
        self.assertEqual((counts["found"], counts["extra"], counts["total"]), (0, 1, 1))

    def test_mark_all_and_clear(self):
        self.start()
        take = self.take()
        self.client.post(f"/stock-takes/{take['id']}/mark-all", data={"state": "found"})
        self.assertEqual(logic.take_counts(self.items(take["id"]))["found"], 3)
        self.client.post(f"/stock-takes/{take['id']}/mark-all",
                         data={"state": "expected"})
        self.assertEqual(logic.take_counts(self.items(take["id"]))["found"], 0)

    def test_only_extra_rows_can_be_removed(self):
        self.start()
        take = self.take()
        row = self.items(take["id"])[0]
        r = self.client.post(
            f"/stock-takes/{take['id']}/items/{row['id']}/delete")
        self.assertIn("только лишнюю строку", self.get_ok(r.headers["location"]))
        self.assertEqual(len(self.items(take["id"])), 3)

    # ─── закрытие ───

    def close(self, take_id, **over):
        data = {"return_found": "1"}
        data.update(over)
        return self.client.post(f"/stock-takes/{take_id}/close", data=data)

    def test_closing_turns_unmarked_rows_into_shortage(self):
        self.start()
        take = self.take()
        self.client.post(f"/stock-takes/{take['id']}/scan", data={"code": "B-2"})
        r = self.close(take["id"])
        self.assertIn("не нашли 2", self.get_ok(r.headers["location"]))
        done = tw.run(self.crm.stock_take(take["id"]))
        self.assertEqual(done["status"], "done")
        self.assertEqual((done["expected"], done["found"], done["missing"]), (3, 1, 2))
        self.assertIsNone(self.take(), "закрытая ведомость не должна числиться текущей")

    def test_shortage_alone_does_not_lose_bikes(self):
        """Пропустить велосипед глазами легко, а «Утерян» потом не снимут."""
        self.start()
        take = self.take()
        self.close(take["id"])
        for bike_id in (self.bike_id, self.b2, self.b3):
            self.assertNotEqual(tw.run(self.crm.bike(bike_id))["status"], "lost")

    def test_shortage_can_be_written_off_as_lost(self):
        self.start()
        take = self.take()
        self.client.post(f"/stock-takes/{take['id']}/scan", data={"code": "B-2"})
        r = self.close(take["id"], lose_missing="1")
        self.assertIn("переведено в «Утерян»: 2", self.get_ok(r.headers["location"]))
        self.assertEqual(tw.run(self.crm.bike(self.b2))["status"], "available")
        self.assertEqual(tw.run(self.crm.bike(self.b3))["status"], "lost")
        # Статус пишется в журнал: потеря должна быть видна с автором.
        log = tw.run(self.crm.bike_status_log(self.b3))
        self.assertEqual(log[0]["to_status"], "lost")
        self.assertEqual(log[0]["changed_by"], "staff:admin")

    def test_found_lost_bike_returns_to_the_park(self):
        """Ради этого пересчёт и затевается: 25 потерянных - это парк."""
        tw.run(self.crm.update_bike(self.b3, status="lost"))
        self.start()
        take = self.take()
        self.assertEqual(take["expected"], 2)
        self.client.post(f"/stock-takes/{take['id']}/scan", data={"code": "B-3"})
        r = self.close(take["id"])
        self.assertIn("вернулось в парк: 1", self.get_ok(r.headers["location"]))
        self.assertEqual(tw.run(self.crm.bike(self.b3))["status"], "available")

    def test_closed_take_takes_no_more_marks(self):
        self.start()
        take = self.take()
        self.close(take["id"])
        r = self.client.post(f"/stock-takes/{take['id']}/scan", data={"code": "B-2"})
        self.assertIn("Пересчёт закрыт", self.get_ok(r.headers["location"]))

    def test_second_close_is_refused(self):
        self.start()
        take = self.take()
        self.close(take["id"])
        r = self.close(take["id"])
        self.assertIn("уже закрыт", self.get_ok(r.headers["location"]))

    # ─── экраны и доступ ───

    def test_pages_render(self):
        self.start()
        take = self.take()
        page = self.get_ok("/stock-takes")
        self.assertIn("ПРТ-000001", page)
        card = self.get_ok(f"/stock-takes/{take['id']}")
        self.assertIn("Отметить по номеру", card)
        self.assertIn("B-2", card)
        self.assertEqual(self.client.get("/stock-takes/999").status_code, 404)

    def test_viewer_cannot_count(self):
        """Смотрящий парк видит ведомость, но не правит её."""
        profile = tw.run(self.crm.access_profile_by_code("manager"))
        tw.run(self.crm.create_staff("ivan", logic.hash_password("password-1"),
                                     "Иван", "manager", profile["id"]))
        self.start()
        take = self.take()
        self.client.post("/logout")
        self.login("ivan", "password-1")
        self.assertEqual(self.client.get(f"/stock-takes/{take['id']}").status_code, 200)
        r = self.client.post(f"/stock-takes/{take['id']}/scan", data={"code": "B-2"})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
