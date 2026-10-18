"""Пересчёт техники: ведомость ПРТ и что она делает с парком.

Главное, что проверяется: ведомость снимает парк один раз при открытии,
недостача сама по себе никого не теряет, а найденный потерянный велосипед
возвращается в парк - ради этого пересчёт и затевается.
"""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

D = Decimal

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
        # В подписи теперь и «что», и «где»: ведомость на батареи от
        # ведомости на велосипеды иначе не отличить.
        self.assertEqual(logic.take_title({"scope": "all"}),
                         "Велосипеды · Весь парк")
        self.assertEqual(logic.take_title({"scope": "all", "what": "all"}),
                         "Всё · Весь парк")
        self.assertEqual(
            logic.take_title({"scope": "location", "location": "Адоратского",
                              "what": "batteries"}),
            "Аккумуляторы · Точка Адоратского")
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

    def test_comment_is_added_kept_and_cleared(self):
        """Комментарий пишется своей формой: отметка его не трогает, пустой
        - стирает, закрытая ведомость его уже не принимает."""
        self.start()
        take = self.take()
        item = tw.run(self.crm.take_item_of_bike(take["id"], self.b2))
        page = self.get_ok(f"/stock-takes/{take['id']}")
        self.assertIn(f'/stock-takes/{take["id"]}/items/{item["id"]}/note', page)
        # Слова владельца: «Наименование» (вид и модель, без отдельной
        # «Модели»), «Статус» вместо «Числится».
        self.assertIn("<th>Наименование</th>", page)
        self.assertIn("<th>Статус</th>", page)
        self.assertIn("Велосипед · Kugoo V3", page)
        for gone in ("Предмет", "Модель", "Числится", ">Что<"):
            self.assertNotIn(gone, page)
        self.client.post(f"/stock-takes/{take['id']}/items/{item['id']}/note",
                         data={"note": "на ремонте у Марата"})
        self.client.post(f"/stock-takes/{take['id']}/items/{item['id']}",
                         data={"state": "found"})
        item = tw.run(self.crm.take_item_of_bike(take["id"], self.b2))
        self.assertEqual((item["state"], item["note"]), ("found", "на ремонте у Марата"))
        self.assertIn('value="на ремонте у Марата"', self.get_ok(f"/stock-takes/{take['id']}"))
        self.client.post(f"/stock-takes/{take['id']}/items/{item['id']}/note",
                         data={"note": ""})
        self.assertIsNone(tw.run(self.crm.take_item_of_bike(take["id"], self.b2))["note"])
        self.client.post(f"/stock-takes/{take['id']}/close")
        self.client.post(f"/stock-takes/{take['id']}/items/{item['id']}/note",
                         data={"note": "поздно"})
        self.assertIsNone(tw.run(self.crm.take_item_of_bike(take["id"], self.b2))["note"])

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
        """Смотрящий парк без действия «Пересчёт техники» видит ведомость,
        но не правит её."""
        profile_id = tw.run(self.crm.create_access_profile(
            "Смотрит парк", {"sections": {"bikes": "view"}, "actions": {}}))
        tw.run(self.crm.create_staff("ivan", logic.hash_password("password-1"),
                                     "Иван", "manager", profile_id))
        self.start()
        take = self.take()
        self.client.post("/logout")
        self.login("ivan", "password-1")
        self.assertEqual(self.client.get(f"/stock-takes/{take['id']}").status_code, 200)
        r = self.client.post(f"/stock-takes/{take['id']}/scan", data={"code": "B-2"})
        self.assertEqual(r.status_code, 403)


class TestTakeCodes(unittest.TestCase):
    """Как человек набирает номер: сканер, телефон, любая раскладка."""

    UNITS = [
        {"kind": "bike", "id": 1, "code": "МБ-001", "motor_no": "60V240W2305123456",
         "frame_no": "LYX2023F0001"},
        {"kind": "bike", "id": 2, "code": "МБ-002", "motor_no": None, "frame_no": None},
        {"kind": "bike", "id": 3, "code": "510010", "motor_no": None, "frame_no": None},
        {"kind": "battery", "id": 7, "code": "АКБ-0017", "serial_no": "SN-77001"},
    ]

    def index(self):
        return logic.TakeIndex(self.UNITS)

    def test_key_ignores_case_layout_dashes_and_zeros(self):
        same = {logic.take_code_key(x) for x in
                ("МБ-001", "мб-1", "МБ 001", "MB-001", "mb001", "Мб-0001", " мб - 01 ")}
        self.assertEqual(same, {"MB1"})
        # Нули снимаются после склейки: «1-01» - это 101, а не 11.
        self.assertEqual(logic.take_code_key("B-1-01"), "B101")
        self.assertNotEqual(logic.take_code_key("B-100"), logic.take_code_key("B-1"))
        self.assertEqual(logic.take_code_key("000"), "0")
        self.assertEqual(logic.take_code_key(""), "")

    def test_index_finds_by_every_way_of_typing(self):
        index = self.index()
        for raw in ("МБ-001", "мб-1", "МБ 001", "MB-001", "mb001",
                    "60v240w2305123456", "60В240W 2305 123456", "lyx2023f0001",
                    "123456"):
            hits = index.find(raw)
            self.assertEqual([h["id"] for h in hits], [1], raw)
        self.assertEqual([h["id"] for h in index.find("акб-17")], [7])
        self.assertEqual([h["id"] for h in index.find("sn77001")], [7])
        self.assertEqual(index.find("МБ-999"), [])
        # Хвост короче шести знаков - не номер: «56» нашёл бы полпарка.
        self.assertEqual(index.find("3456"), [])

    def test_exact_code_wins_over_a_loose_match(self):
        index = logic.TakeIndex([{"kind": "bike", "id": 1, "code": "B-1"},
                                 {"kind": "bike", "id": 2, "code": "B-01"}])
        self.assertEqual([h["id"] for h in index.find("b-01")], [2])
        self.assertEqual([h["id"] for h in index.find("B-1")], [1])
        # «B 001» под оба - номер надо уточнить, а не отмечать наугад.
        self.assertEqual(len(index.find("B 001")), 2)

    def test_bike_wins_a_number_shared_with_a_battery(self):
        index = logic.TakeIndex([{"kind": "battery", "id": 9, "code": "510010"},
                                 {"kind": "bike", "id": 3, "code": "510010"}])
        self.assertEqual([(h["kind"], h["id"]) for h in index.find("510010")],
                         [("bike", 3)])

    def test_several_numbers_in_one_field(self):
        known = lambda text: bool(self.index().find(text))  # noqa: E731
        self.assertEqual(logic.take_codes("МБ-001 МБ-002", known), ["МБ-001", "МБ-002"])
        self.assertEqual(logic.take_codes("МБ 001 МБ 002", known), ["МБ 001", "МБ 002"])
        self.assertEqual(logic.take_codes("мб-1, мб-2;АКБ-0017\n510010\r\n", known),
                         ["мб-1", "мб-2", "АКБ-0017", "510010"])
        # Неизвестный номер с пробелом - один номер: буквы без цифр - его начало.
        self.assertEqual(logic.take_codes("МБ 1070", known), ["МБ 1070"])
        self.assertEqual(logic.take_codes("XYZ-1 XYZ-2 МБ-001", known),
                         ["XYZ-1", "XYZ-2", "МБ-001"])
        self.assertEqual(logic.take_codes(" , ;\n", known), [])

    def test_item_title_is_kind_and_model(self):
        self.assertEqual(logic.take_item_title({"bike_id": 1, "bike_model": "Truck +"}),
                         "Велосипед · Truck +")
        self.assertEqual(logic.take_item_title({"battery_id": 1,
                                                "battery_model": "АКБ Kugoo 60V 21Ah"}),
                         "АКБ Kugoo 60V 21Ah", "вид уже в названии - без повтора")
        self.assertEqual(logic.take_item_title({"battery_id": 1,
                                                "battery_model": "Kugoo 60V"}),
                         "АКБ · Kugoo 60V")
        self.assertEqual(logic.take_item_title({"bike_id": 1, "bike_model": None}),
                         "Велосипед")
        self.assertEqual(logic.take_item_title({"code": "X-1"}), "—")

    def test_who_may_count(self):
        owner = {"perms": logic.BUILT_IN_PROFILES[0][2]}
        admin = {"perms": logic.BUILT_IN_PROFILES[1][2], "location": "Павлюхина"}
        roaming = {"perms": logic.BUILT_IN_PROFILES[1][2], "location": None}
        viewer = {"perms": {"sections": {"bikes": "view"}, "actions": {}}}
        here = {"scope": "location", "location": "Павлюхина"}
        there = {"scope": "location", "location": "Адоратского"}
        park = {"scope": "all", "location": None}
        for take in (here, there, park):
            self.assertTrue(logic.take_may_count(owner, **take))
            self.assertTrue(logic.take_may_count(roaming, **take))
            self.assertFalse(logic.take_may_count(viewer, **take))
        self.assertTrue(logic.take_may_count(admin, **here))
        self.assertFalse(logic.take_may_count(admin, **there))
        self.assertFalse(logic.take_may_count(admin, **park))

    def test_points_count_at_once_but_not_twice(self):
        open_takes = [{"no": "ПРТ-1", "scope": "location", "location": "Павлюхина"}]
        self.assertIsNone(logic.take_conflict(open_takes, scope="location",
                                              location="Адоратского"))
        self.assertIsNotNone(logic.take_conflict(open_takes, scope="location",
                                                 location="Павлюхина"))
        self.assertIsNotNone(logic.take_conflict(open_takes, scope="all", location=None))
        park = [{"no": "ПРТ-2", "scope": "all", "location": None}]
        self.assertIsNotNone(logic.take_conflict(park, scope="location",
                                                 location="Адоратского"))
        self.assertIsNone(logic.take_conflict([], scope="all", location=None))

    def test_summary_of_a_list(self):
        text = logic.take_scan_summary([
            {"state": "found", "code": "A"}, {"state": "found", "code": "B", "again": True},
            {"state": "extra", "code": "X"},
            {"state": "ambiguous", "code": "Y", "message": "Y: подходит несколько."}])
        self.assertEqual(text, "Номеров: 4; на месте: 1; уже отмечены: 1; лишние: 1 (X); "
                               "не записаны: 1 (Y). Y: подходит несколько.")


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestTakeInput(tw.WebCase):
    """Ввод номеров на экране ведомости - так, как его делает человек."""

    def setUp(self):
        super().setUp()
        self.login()
        self.b1 = tw.run(self.crm.create_bike(code="МБ-001", model="Truck +",
                                              location="Павлюхина",
                                              motor_no="60V240W2305123456"))
        self.b2 = tw.run(self.crm.create_bike(code="МБ-002", model="Truck +",
                                              location="Павлюхина"))
        self.b3 = tw.run(self.crm.create_bike(code="МБ-003", model="Truck +",
                                              location="Адоратского"))
        model = tw.run(self.crm.create_battery_model(
            title="АКБ Kugoo 60V 21Ah", brand=None, voltage=60, capacity=None,
            price=D(9000), service_months=15, note=None))
        self.cell = tw.run(self.crm.create_battery(code="АКБ-0017", model_id=model,
                                                   status="available",
                                                   location="Павлюхина"))

    def start(self, **over):
        data = {"scope": "location", "location": "Павлюхина", "what": "all", "note": ""}
        data.update(over)
        r = self.client.post("/stock-takes", data=data)
        self.assertEqual(r.status_code, 303)
        return int(r.headers["location"].rsplit("/", 1)[1])

    def scan(self, take_id, code, **extra):
        r = self.client.post(f"/stock-takes/{take_id}/scan", data={"code": code, **extra})
        self.assertEqual(r.status_code, 303)
        return r

    def state(self, take_id, bike_id=None, battery_id=None):
        if battery_id:
            return tw.run(self.crm.take_item_of_battery(take_id, battery_id))["state"]
        return tw.run(self.crm.take_item_of_bike(take_id, bike_id))["state"]

    def extras(self, take_id):
        return [i for i in tw.run(self.crm.take_items(take_id)) if i["state"] == "extra"]

    def test_number_typed_any_way_is_found(self):
        for raw in ("мб-1", "МБ 001", "MB-001", "mb001"):
            take_id = self.start()
            r = self.scan(take_id, raw)
            self.assertIn("МБ-001 на месте", self.get_ok(r.headers["location"]), raw)
            self.assertEqual(self.state(take_id, self.b1), "found", raw)
            self.assertEqual(self.extras(take_id), [], f"{raw}: мусорных лишних нет")
            self.client.post(f"/stock-takes/{take_id}/close")

    def test_motor_number_and_battery_number(self):
        take_id = self.start()
        self.scan(take_id, "60в240w 2305123456")
        self.assertEqual(self.state(take_id, self.b1), "found")
        self.scan(take_id, "акб-17")
        self.assertEqual(self.state(take_id, battery_id=self.cell), "found")

    def test_several_numbers_in_one_field(self):
        for sep in (" ", "\n", ", ", "; "):
            take_id = self.start()
            r = self.scan(take_id, sep.join(["мб-1", "МБ-002", "АКБ-0017"]))
            page = self.get_ok(r.headers["location"])
            self.assertIn("Номеров: 3; на месте: 3.", page, repr(sep))
            self.assertEqual(self.state(take_id, self.b2), "found")
            self.assertEqual(self.state(take_id, battery_id=self.cell), "found")
            self.client.post(f"/stock-takes/{take_id}/close")

    def test_unknown_number_is_recorded_once(self):
        take_id = self.start()
        self.scan(take_id, "XYZ-999")
        r = self.scan(take_id, "xyz 999")
        self.assertIn("уже записан лишним", self.get_ok(r.headers["location"]))
        self.assertEqual([i["code"] for i in self.extras(take_id)], ["XYZ-999"])

    def test_number_of_another_point_is_extra_with_its_point(self):
        take_id = self.start()
        r = self.scan(take_id, "мб-3")
        page = self.get_ok(r.headers["location"])
        self.assertIn("МБ-003 не ждали здесь: статус «Свободен», точка Адоратского", page)
        extra = self.extras(take_id)
        self.assertEqual(extra[0]["note"],
                         "Статус при отметке: Свободен, точка Адоратского")
        self.assertNotIn("Числится", page)

    def test_ambiguous_number_is_not_marked(self):
        tw.run(self.crm.create_bike(code="МБ-01", model="Truck +", location="Павлюхина"))
        take_id = self.start()
        r = self.scan(take_id, "мб 0001")
        self.assertIn("подходит несколько", self.get_ok(r.headers["location"]))
        self.assertEqual(self.state(take_id, self.b1), "expected")

    def test_bad_and_empty_input(self):
        take_id = self.start()
        r = self.scan(take_id, "   ")
        self.assertIn("Номер: заполните поле", self.get_ok(r.headers["location"]))
        r = self.scan(take_id, "<b>")
        self.assertIn("Инвентарный номер", self.get_ok(r.headers["location"]))
        r = self.scan(take_id, "x" * 6000)
        self.assertIn("не больше", self.get_ok(r.headers["location"]))
        self.assertEqual(self.extras(take_id), [])

    def test_extra_row_cannot_become_expected(self):
        """Лишнюю строку отметкой «на месте» и «снять отметки» было
        превратить в ожидаемую: «ожидалось» росло на номер, которого в
        парке нет, а при закрытии он уходил в недостачу."""
        take_id = self.start()
        self.scan(take_id, "XYZ-999")
        extra = self.extras(take_id)[0]
        r = self.client.post(f"/stock-takes/{take_id}/items/{extra['id']}",
                             data={"state": "found"})
        self.assertIn("лишняя", self.get_ok(r.headers["location"]))
        self.client.post(f"/stock-takes/{take_id}/mark-all", data={"state": "found"})
        self.client.post(f"/stock-takes/{take_id}/mark-all", data={"state": "expected"})
        self.assertEqual(len(self.extras(take_id)), 1)
        self.client.post(f"/stock-takes/{take_id}/close")
        done = tw.run(self.crm.stock_take(take_id))
        self.assertEqual((done["expected"], done["missing"], done["extra"]), (3, 3, 1))

    def test_filter_keeps_its_view_after_a_mark(self):
        take_id = self.start()
        page = self.get_ok(f"/stock-takes/{take_id}?show=expected")
        self.assertIn("Не отмечены", page)
        r = self.scan(take_id, "мб-1", show="expected")
        self.assertEqual(r.headers["location"], f"/stock-takes/{take_id}?show=expected")
        page = self.get_ok(r.headers["location"])
        self.assertNotIn(f'href="/bikes/{self.b1}"', page, "отмеченный ушёл из вида")
        self.assertIn(f'href="/bikes/{self.b2}"', page)
        self.assertIn("Таких строк нет", self.get_ok(f"/stock-takes/{take_id}?show=missing"))
        # Чужое значение фильтра - просто «все».
        self.get_ok(f"/stock-takes/{take_id}?show=<x>")

    def test_take_comment(self):
        """Общий комментарий: правится, пока идёт, виден после закрытия,
        ищется в списке и уходит в Excel."""
        take_id = self.start(note="Плановый")
        page = self.get_ok(f"/stock-takes/{take_id}")
        self.assertIn("Комментарий к пересчёту", page)
        self.assertIn(">Плановый</textarea>", page)
        r = self.client.post(f"/stock-takes/{take_id}/note",
                             data={"note": "Считали с Маратом, МБ-002 в гараже"})
        self.assertEqual(r.headers["location"], f"/stock-takes/{take_id}#take-note")
        self.assertIn("Комментарий сохранён", self.get_ok(r.headers["location"]))
        self.client.post(f"/stock-takes/{take_id}/close")
        r = self.client.post(f"/stock-takes/{take_id}/note", data={"note": "поздно"})
        self.assertIn("Пересчёт закрыт", self.get_ok(r.headers["location"]))
        page = self.get_ok(f"/stock-takes/{take_id}")
        self.assertIn("Считали с Маратом, МБ-002 в гараже", page)
        self.assertNotIn("/note\"", page.split("Комментарий к пересчёту")[1][:400])
        listing = self.get_ok("/stock-takes?q=маратом")
        self.assertIn("ПРТ-000001", listing)
        self.assertIn("Ничего не нашлось", self.get_ok("/stock-takes?q=нет-такого"))
        csv = self.client.get("/stock-takes.csv").text
        self.assertIn("Статус", csv)
        self.assertIn("Закрыт", csv)
        self.assertIn("Считали с Маратом", csv)
        xlsx = self.client.get("/stock-takes.xlsx?q=маратом")
        self.assertEqual(xlsx.status_code, 200)

    def test_take_exports_itself(self):
        take_id = self.start()
        self.scan(take_id, "мб-1")
        r = self.client.get(f"/stock-takes/{take_id}.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Наименование", r.text)
        self.assertIn("МБ-001;Велосипед · Truck +;Свободен;Павлюхина;На месте", r.text)
        self.assertIn("АКБ-0017;АКБ Kugoo 60V 21Ah", r.text)
        self.assertEqual(self.client.get(f"/stock-takes/{take_id}.xlsx").status_code, 200)
        self.assertEqual(self.client.get(f"/stock-takes/{take_id}.pdf").status_code, 404)
        self.assertEqual(self.client.get("/stock-takes/999.csv").status_code, 404)

    def test_two_points_count_at_once(self):
        here = self.start()
        there = self.start(location="Адоратского")
        self.assertNotEqual(here, there)
        page = self.get_ok("/stock-takes")
        self.assertIn("ПРТ-000001", page)
        self.assertIn("ПРТ-000002", page)
        # Та же точка второй раз и весь парк рядом с ними - нет.
        r = self.client.post("/stock-takes", data={"scope": "location", "what": "all",
                                                   "location": "Павлюхина", "note": ""})
        self.assertIn("Пересчёт уже идёт: ПРТ-000001", self.get_ok(r.headers["location"]))
        r = self.client.post("/stock-takes", data={"scope": "all", "what": "all",
                                                   "location": "", "note": ""})
        self.assertIn("Пересчёт уже идёт", self.get_ok(r.headers["location"]))
        self.assertEqual(len(tw.run(self.crm.open_stock_takes())), 2)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestTakeRights(tw.WebCase):
    """Администратор точки считает свою точку, владелец - любую."""

    def setUp(self):
        super().setUp()
        self.login()
        tw.run(self.crm.create_bike(code="B-1", model="M", location="Павлюхина"))
        tw.run(self.crm.create_bike(code="B-2", model="M", location="Адоратского"))
        profile = tw.run(self.crm.access_profile_by_code("manager"))
        tw.run(self.crm.create_staff("anna", logic.hash_password("password-1"), "Анна",
                                     "manager", profile["id"], location="Павлюхина"))

    def as_admin(self):
        self.client.post("/logout")
        self.login("anna", "password-1")

    def test_admin_counts_her_point(self):
        self.as_admin()
        page = self.get_ok("/stock-takes")
        self.assertIn("Новый пересчёт", page)
        self.assertIn('value="Точка Павлюхина" disabled', page)
        r = self.client.post("/stock-takes", data={"scope": "location", "what": "all",
                                                   "location": "Павлюхина", "note": ""})
        take_id = int(r.headers["location"].rsplit("/", 1)[1])
        r = self.client.post(f"/stock-takes/{take_id}/scan", data={"code": "b-1"})
        self.assertIn("B-1 на месте", self.get_ok(r.headers["location"]))
        r = self.client.post(f"/stock-takes/{take_id}/close", data={})
        self.assertEqual(tw.run(self.crm.stock_take(take_id))["status"], "done")

    def test_admin_cannot_count_another_point_or_the_park(self):
        self.as_admin()
        for scope, location in (("location", "Адоратского"), ("all", "")):
            r = self.client.post("/stock-takes", data={"scope": scope, "what": "all",
                                                       "location": location, "note": ""})
            self.assertIn("только своей точки: Павлюхина",
                          self.get_ok(r.headers["location"]))
        self.assertEqual(tw.run(self.crm.stock_takes()), [])
        # Чужую ведомость она видит, но не правит.
        self.client.post("/logout")
        self.login()
        r = self.client.post("/stock-takes", data={"scope": "location", "what": "all",
                                                   "location": "Адоратского", "note": ""})
        take_id = int(r.headers["location"].rsplit("/", 1)[1])
        self.as_admin()
        page = self.get_ok(f"/stock-takes/{take_id}")
        self.assertNotIn("Отметить по номеру", page)
        for path in ("scan", "note", "mark-all", "close"):
            r = self.client.post(f"/stock-takes/{take_id}/{path}",
                                 data={"code": "B-2", "state": "found"})
            self.assertEqual(r.status_code, 403, path)
        self.assertEqual(tw.run(self.crm.stock_take(take_id))["status"], "open")

    def test_admin_without_point_counts_anywhere(self):
        staff = tw.run(self.crm.staff_by_login("anna"))
        tw.run(self.crm.set_staff_location(staff["id"], None))
        self.as_admin()
        r = self.client.post("/stock-takes", data={"scope": "all", "what": "all",
                                                   "location": "", "note": ""})
        self.assertRegex(r.headers["location"], r"^/stock-takes/\d+$")

    def test_action_can_be_taken_away(self):
        """Действие - галочка профиля: владелец снимает её, и админ точки
        снова только смотрит."""
        profile = tw.run(self.crm.access_profile_by_code("manager"))
        perms = logic.normalize_perms(profile["perms"])
        perms["actions"].pop("stock_take")
        tw.run(self.crm.update_access_profile(profile["id"], name=profile["name"],
                                             perms=perms))
        self.as_admin()
        self.assertNotIn("Новый пересчёт", self.get_ok("/stock-takes"))
        r = self.client.post("/stock-takes", data={"scope": "location", "what": "all",
                                                   "location": "Павлюхина", "note": ""})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()


class TestExpectedBatteries(unittest.TestCase):
    """Кого ждём на точке из батарей."""

    def rows(self):
        return [{"id": 1, "code": "9510001", "status": "available",
                 "location": "Павлюхина"},
                {"id": 2, "code": "9510002", "status": "rented",
                 "location": "Павлюхина"},
                {"id": 3, "code": "9510003", "status": "repair",
                 "location": "Адоратского"},
                {"id": 4, "code": "9510004", "status": "new",
                 "location": "Павлюхина"},
                {"id": 5, "code": "9510005", "status": "lost",
                 "location": "Павлюхина"}]

    def test_rented_and_lost_are_not_expected(self):
        got = logic.expected_batteries(self.rows(), scope="all")
        self.assertEqual([b["code"] for b in got], ["9510001", "9510003"])

    def test_new_is_not_expected(self):
        got = logic.expected_batteries(self.rows(), scope="all")
        self.assertNotIn("9510004", [b["code"] for b in got],
                         "на сборке - ещё не в обороте, её отсутствие "
                         "на точке ничего не значит")

    def test_location_narrows(self):
        got = logic.expected_batteries(self.rows(), scope="location",
                                       location="Адоратского")
        self.assertEqual([b["code"] for b in got], ["9510003"])


class TestCountsByKind(unittest.TestCase):
    def test_counts_are_split(self):
        items = [{"bike_id": 1, "state": "found"},
                 {"bike_id": 2, "state": "missing"},
                 {"battery_id": 7, "state": "found"},
                 {"battery_id": 8, "state": "found"},
                 {"code": "чужое", "state": "extra"}]
        got = logic.take_counts_by_kind(items)
        self.assertEqual(got["bikes"]["found"], 1)
        self.assertEqual(got["bikes"]["missing"], 1)
        self.assertEqual(got["batteries"]["found"], 2)
        self.assertEqual(got["batteries"]["missing"], 0)

    def test_empty_kind_is_marked(self):
        got = logic.take_counts_by_kind([{"bike_id": 1, "state": "found"}])
        self.assertTrue(got["bikes"]["any"])
        self.assertFalse(got["batteries"]["any"])


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestTakeWithBatteries(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.bike_id = tw.run(self.crm.create_bike(code="B-1", model="Kugoo V3",
                                                location="Павлюхина"))
        self.battery_id = tw.run(self.crm.create_battery(
            code="9510001", status="available", location="Павлюхина"))
        self.gone_id = tw.run(self.crm.create_battery(
            code="9510002", status="available", location="Павлюхина"))

    def start(self, what="all"):
        self.client.post("/stock-takes", data={"scope": "all", "what": what,
                                               "note": ""})
        return tw.run(self.crm.open_stock_take())["id"]

    def test_all_counts_both(self):
        take_id = self.start("all")
        items = tw.run(self.crm.take_items(take_id))
        self.assertEqual(len(items), 3, "велосипед и две батареи")
        self.assertEqual(tw.run(self.crm.stock_take(take_id))["expected"], 3)

    def test_batteries_only_leaves_the_bike_out(self):
        take_id = self.start("batteries")
        items = tw.run(self.crm.take_items(take_id))
        self.assertEqual({i["battery_code"] for i in items},
                         {"9510001", "9510002"})
        self.assertTrue(all(i["bike_id"] is None for i in items))

    def test_a_battery_number_is_recognised(self):
        take_id = self.start("all")
        self.client.post(f"/stock-takes/{take_id}/scan", data={"code": "9510001"})
        item = tw.run(self.crm.take_item_of_battery(take_id, self.battery_id))
        self.assertEqual(item["state"], "found")

    def test_a_bike_number_still_works(self):
        take_id = self.start("all")
        self.client.post(f"/stock-takes/{take_id}/scan", data={"code": "B-1"})
        item = tw.run(self.crm.take_item_of_bike(take_id, self.bike_id))
        self.assertEqual(item["state"], "found")

    def test_battery_number_in_a_bikes_only_take_is_not_recorded(self):
        """Батарея в ведомости на велосипеды - не «нет в парке» и не
        лишняя: её здесь просто не считают, и человеку говорится это."""
        take_id = self.start("bikes")
        r = self.client.post(f"/stock-takes/{take_id}/scan", data={"code": "9510001"})
        self.assertIn("9510001 - аккумулятор, а считаем: велосипеды",
                      self.get_ok(r.headers["location"]))
        items = tw.run(self.crm.take_items(take_id))
        self.assertEqual([i for i in items if i["state"] == "extra"], [])

    def test_missing_battery_becomes_lost_only_by_the_checkbox(self):
        take_id = self.start("all")
        self.client.post(f"/stock-takes/{take_id}/scan", data={"code": "B-1"})
        self.client.post(f"/stock-takes/{take_id}/scan", data={"code": "9510001"})
        self.client.post(f"/stock-takes/{take_id}/close", data={})
        self.assertEqual(tw.run(self.crm.battery(self.gone_id))["status"],
                         "available", "без галочки ничего не списывается")

    def test_with_the_checkbox_it_is_lost(self):
        take_id = self.start("all")
        self.client.post(f"/stock-takes/{take_id}/scan", data={"code": "B-1"})
        self.client.post(f"/stock-takes/{take_id}/scan", data={"code": "9510001"})
        self.client.post(f"/stock-takes/{take_id}/close", data={"lose_missing": "1"})
        self.assertEqual(tw.run(self.crm.battery(self.gone_id))["status"], "lost")
        self.assertEqual(tw.run(self.crm.battery(self.battery_id))["status"],
                         "available")

    def test_found_lost_battery_comes_back(self):
        tw.run(self.crm.update_battery(self.gone_id, status="lost", by="тест"))
        take_id = self.start("all")
        self.client.post(f"/stock-takes/{take_id}/scan", data={"code": "9510002"})
        self.client.post(f"/stock-takes/{take_id}/close",
                         data={"return_found": "1"})
        self.assertEqual(tw.run(self.crm.battery(self.gone_id))["status"], "available",
                         "нашлась - значит физически стоит на точке")

    def test_card_shows_both_kinds(self):
        take_id = self.start("all")
        text = self.get_ok(f"/stock-takes/{take_id}")
        self.assertIn("АКБ", text)
        self.assertIn("Велосипед · Kugoo V3", text)
        self.assertIn("Аккумуляторы:", text, "счётчики по видам")
