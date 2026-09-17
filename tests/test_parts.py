"""Склад запчастей: остатки, приход, списание, расход в наряд, заказы.

Главное, ради чего склад заводился: себестоимость ремонта берётся со
склада, а не пишется руками, и остаток на полке уменьшается тем же
действием. Остаток - сумма движений, отдельной колонки нет.
"""

from __future__ import annotations

import sys
import unittest
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


class TestStockLogic(unittest.TestCase):
    def test_document_numbers(self):
        self.assertEqual(logic.doc_no("receipt", 1), "ПРХ-000001")
        self.assertEqual(logic.doc_no("write_off", 42), "СПС-000042")
        self.assertEqual(logic.part_order_no(7), "ЗАП-000007")

    def test_stock_is_the_sum_of_moves(self):
        moves = [{"qty": 10}, {"qty": -3}, {"qty": -2}, {"qty": 1}]
        self.assertEqual(logic.stock_of(moves), 6)
        self.assertEqual(logic.stock_of([]), 0)

    def test_average_cost_is_weighted(self):
        """Один дорогой приход не задирает стоимость всего склада."""
        self.assertEqual(logic.average_cost(4, D(300), 6, D(400)), D("360.00"))
        # пустой склад: себестоимость равна цене прихода
        self.assertEqual(logic.average_cost(0, D(0), 5, D(250)), D("250.00"))
        # ушли в ноль: считаем по новой цене, а не делим на ноль
        self.assertEqual(logic.average_cost(-3, D(100), 0, D(200)), D("200.00"))

    def test_rows_put_shortages_first(self):
        parts = [{"id": 1, "title": "Камера", "cost": D(200), "price": D(500),
                  "min_stock": 4, "active": True},
                 {"id": 2, "title": "Колодки", "cost": D(300), "price": D(600),
                  "min_stock": 10, "active": True},
                 {"id": 3, "title": "Трос", "cost": D(100), "price": D(200),
                  "min_stock": 0, "active": True}]
        rows = logic.part_rows(parts, {1: 9, 2: 2, 3: 0})
        self.assertEqual([r["title"] for r in rows], ["Колодки", "Камера", "Трос"])
        self.assertEqual(rows[0]["short"], 8)
        self.assertFalse(rows[1]["below"])
        self.assertEqual(rows[1]["cost_total"], D(1800))

    def test_summary(self):
        parts = [{"id": 1, "title": "Камера", "cost": D(200), "price": D(500),
                  "min_stock": 4, "active": True}]
        summary = logic.stock_summary(logic.part_rows(parts, {1: 0}))
        self.assertEqual((summary["positions"], summary["below"], summary["empty"]),
                         (1, 1, 0 + 1))
        self.assertEqual(summary["cost"], D(0))

    def test_needs_put_waiting_orders_first(self):
        rows = logic.part_rows([{"id": 1, "title": "Камера", "cost": D(0),
                                 "price": D(0), "min_stock": 5, "active": True}],
                               {1: 1})
        needs = logic.part_needs(rows, [{"part_id": 2, "title": "Контроллер",
                                         "qty": 1, "work_order_no": "РЕМ-000001"}])
        self.assertEqual([n["source"] for n in needs], ["order", "min_stock"])
        self.assertEqual(needs[1]["qty"], 4, "заказываем нехватку до неснижаемого")

    def test_needs_for_one_part_are_summed(self):
        """Наряд ждёт одну, а до неснижаемого не хватает двух - заказать три.

        В заказ позиция кладётся один раз (уникальный индекс), поэтому
        разложенные по двум строкам потребности потеряли бы одну из них.
        """
        rows = logic.part_rows([{"id": 1, "title": "Контроллер", "cost": D(0),
                                 "price": D(0), "min_stock": 2, "active": True}],
                               {1: 0})
        needs = logic.part_needs(rows, [{"part_id": 1, "title": "Контроллер",
                                         "qty": 1, "work_order_id": 5,
                                         "work_order_no": "РЕМ-000005",
                                         "bike_code": "B-02"}])
        self.assertEqual(len(needs), 1)
        self.assertEqual(needs[0]["qty"], 3)
        self.assertEqual(needs[0]["source"], "order", "наряд важнее полки")
        self.assertEqual(needs[0]["work_order_no"], "РЕМ-000005")

    def test_need_without_a_catalogue_part_stays_apart(self):
        needs = logic.part_needs([], [{"part_id": None, "title": "Мотор-колесо",
                                       "qty": 1},
                                      {"part_id": None, "title": "Мотор-колесо",
                                       "qty": 1}])
        self.assertEqual(len(needs), 2, "складывать не с чем: позиции в базе нет")

    def test_order_total(self):
        self.assertEqual(logic.order_total([{"qty": 2, "price": D(300)},
                                            {"qty": 1, "price": "150.50"}]),
                         D("750.50"))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestStockInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.part_id = tw.run(self.crm.create_part(
            title="Колодки дисковые", node="brake_pads", unit="шт", cost=D(0),
            price=D(600), min_stock=10, model=None, note=None))

    def receive(self, qty="10", price="300", **over):
        data = {"supplier_id": "", "note": "", "part_id_0": self.part_id,
                "qty_0": qty, "price_0": price}
        data.update(over)
        return self.client.post("/parts/receipts", data=data)

    def part(self):
        return tw.run(self.crm.part(self.part_id))

    def stock(self):
        return tw.run(self.crm.part_stock(self.part_id))

    # ─── приход ───

    def test_receipt_fills_stock_and_cost(self):
        r = self.receive()
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.stock(), 10)
        self.assertEqual(self.part()["cost"], D(300))
        docs = tw.run(self.crm.part_docs(kind="receipt"))
        self.assertEqual(docs[0]["no"], "ПРХ-000001")
        self.assertEqual(docs[0]["total"], D(3000))

    def test_second_receipt_averages_the_cost(self):
        self.receive(qty="4", price="300")
        self.receive(qty="6", price="400")
        self.assertEqual(self.part()["cost"], D("360.00"))
        self.assertEqual(self.stock(), 10)

    def test_empty_receipt_is_refused(self):
        r = self.receive(qty="", price="")
        self.assertIn("Приход пуст", self.get_ok(r.headers["location"]))

    # ─── списание ───

    def test_write_off_needs_a_reason(self):
        self.receive()
        r = self.client.post("/parts/write-offs", data={
            "part_id_0": self.part_id, "qty_0": "2", "note": ""})
        self.assertIn("причину списания", self.get_ok(r.headers["location"]))
        self.assertEqual(self.stock(), 10)

    def test_write_off_more_than_stock_is_refused(self):
        self.receive(qty="3")
        r = self.client.post("/parts/write-offs", data={
            "part_id_0": self.part_id, "qty_0": "5", "note": "брак"})
        self.assertIn("на складе 3", self.get_ok(r.headers["location"]))
        self.assertEqual(self.stock(), 3)

    def test_write_off_reduces_stock(self):
        self.receive()
        self.client.post("/parts/write-offs", data={
            "part_id_0": self.part_id, "qty_0": "2", "note": "брак"})
        self.assertEqual(self.stock(), 8)

    # ─── расход в наряд ───

    def open_order(self):
        self.client.post("/orders", data={"bike_id": self.bike_id, "payer": "own",
                                          "estimate": "0", "complaint": "не тормозит"})
        return tw.run(self.crm.work_orders())[0]

    def test_part_goes_from_stock_into_the_order(self):
        self.receive(qty="10", price="300")
        order = self.open_order()
        r = self.client.post(f"/orders/{order['id']}/parts",
                             data={"part_id": self.part_id, "qty": "2"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.stock(), 8, "полка уменьшилась тем же действием")
        items = tw.run(self.crm.order_items(order["id"]))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["parts_cost"], D(300),
                         "себестоимость со склада, а не с потолка")
        self.assertEqual(items[0]["price"], D(600))
        self.assertEqual(items[0]["node"], "brake_pads")

    def test_order_cannot_take_more_than_the_shelf_has(self):
        self.receive(qty="1")
        order = self.open_order()
        r = self.client.post(f"/orders/{order['id']}/parts",
                             data={"part_id": self.part_id, "qty": "3"})
        self.assertIn("на складе 1", self.get_ok(r.headers["location"]))
        self.assertEqual(self.stock(), 1)
        self.assertEqual(tw.run(self.crm.order_items(order["id"])), [])

    def test_closed_order_takes_no_parts(self):
        self.receive()
        order = self.open_order()
        self.client.post(f"/orders/{order['id']}/close",
                         data={"bike_status": "available"})
        r = self.client.post(f"/orders/{order['id']}/parts",
                             data={"part_id": self.part_id, "qty": "1"})
        self.assertIn("закрыт", self.get_ok(r.headers["location"]))
        self.assertEqual(self.stock(), 10)

    def test_closed_order_carries_the_part_cost_into_the_repair_log(self):
        """Ремонт в журнале велосипеда теперь считается по складу."""
        self.receive(qty="10", price="300")
        order = self.open_order()
        self.client.post(f"/orders/{order['id']}/parts",
                         data={"part_id": self.part_id, "qty": "2"})
        self.client.post(f"/orders/{order['id']}/close",
                         data={"bike_status": "available"})
        log = tw.run(self.crm.bike_log(self.bike_id, kind="repair"))
        self.assertEqual(log[0]["cost"], D(600), "две колодки по 300")

    # ─── пересчёт ───

    def test_count_writes_a_move_not_a_silent_fix(self):
        self.receive(qty="10")
        r = self.client.post(f"/parts/{self.part_id}/count",
                             data={"fact": "8", "note": "пересчёт"})
        self.assertIn("-2", self.get_ok(r.headers["location"]))
        self.assertEqual(self.stock(), 8)
        moves = tw.run(self.crm.part_moves(part_id=self.part_id))
        self.assertEqual(moves[0]["kind"], "count")
        self.assertEqual(moves[0]["qty"], -2)

    def test_count_that_matches_changes_nothing(self):
        self.receive(qty="10")
        self.client.post(f"/parts/{self.part_id}/count", data={"fact": "10"})
        self.assertEqual(len(tw.run(self.crm.part_moves(part_id=self.part_id))), 1)

    # ─── экраны и доступ ───

    def test_pages_render(self):
        self.receive()
        self.assertIn("Колодки дисковые", self.get_ok("/parts"))
        self.assertIn("Колодки дисковые", self.get_ok(f"/parts/{self.part_id}"))
        self.assertIn("ПРХ-000001", self.get_ok("/parts/receipts"))
        self.assertIn("Приход", self.get_ok("/parts/moves"))
        self.assertEqual(self.client.get("/parts/999").status_code, 404)

    def test_cost_cannot_be_set_by_hand(self):
        self.receive(qty="10", price="300")
        self.client.post(f"/parts/{self.part_id}/edit", data={
            "title": "Колодки дисковые", "unit": "шт", "cost": "1", "price": "700",
            "min_stock": "10", "model": "", "note": "", "active": "1"})
        part = self.part()
        self.assertEqual(part["cost"], D(300), "себестоимость правится только приходом")
        self.assertEqual(part["price"], D(700))

    def test_manager_only_looks(self):
        profile = tw.run(self.crm.access_profile_by_code("manager"))
        tw.run(self.crm.create_staff("ivan", logic.hash_password("password-1"),
                                     "Иван", "manager", profile["id"]))
        self.client.post("/logout")
        self.login("ivan", "password-1")
        self.get_ok("/parts")
        self.assertEqual(self.client.get("/parts/new").status_code, 403)
        self.assertEqual(self.receive().status_code, 403)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestPartOrdersInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.part_id = tw.run(self.crm.create_part(
            title="Контроллер", node="controller", unit="шт", cost=D(2500),
            price=D(4000), min_stock=2, model=None, note=None))

    def test_needs_come_from_min_stock(self):
        page = self.get_ok("/part-orders")
        self.assertIn("Контроллер", page)
        self.assertIn("Ниже неснижаемого", page)

    def test_waiting_order_makes_a_need(self):
        self.client.post("/orders", data={"bike_id": self.bike_id, "payer": "own",
                                          "estimate": "0", "complaint": "не едет"})
        order = tw.run(self.crm.work_orders())[0]
        self.client.post(f"/orders/{order['id']}/items", data={
            "title": "Замена контроллера", "node": "controller", "qty": "1",
            "price": "4000", "parts_cost": "0", "labor_cost": "500"})
        self.client.post(f"/orders/{order['id']}/edit", data={
            "status": "waiting", "tech_id": "", "estimate": "0", "note": ""})
        page = self.get_ok("/part-orders")
        self.assertIn("Наряд ждёт запчасть", page)
        self.assertIn(order["no"], page)

    def test_collected_order_holds_the_summed_need(self):
        """Наряд ждёт контроллер, и на полке его нет: в заказе три штуки."""
        self.client.post("/orders", data={"bike_id": self.bike_id, "payer": "own",
                                          "estimate": "0", "complaint": "не едет"})
        order = tw.run(self.crm.work_orders())[0]
        self.client.post(f"/orders/{order['id']}/items", data={
            "title": "Замена контроллера", "node": "controller", "qty": "1",
            "price": "4000", "parts_cost": "0", "labor_cost": "500"})
        self.client.post(f"/orders/{order['id']}/edit", data={
            "status": "waiting", "tech_id": "", "estimate": "0", "note": ""})
        self.client.post("/part-orders/collect")
        items = tw.run(self.crm.part_order_items(
            tw.run(self.crm.open_part_order())["id"]))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["qty"], 3, "две до неснижаемого плюс одна в наряд")
        self.assertEqual(items[0]["source"], "order")

    def test_collect_fills_the_order_once(self):
        self.client.post("/part-orders/collect")
        order = tw.run(self.crm.open_part_order())
        self.assertIsNotNone(order)
        self.assertEqual(order["no"], "ЗАП-000001")
        items = tw.run(self.crm.part_order_items(order["id"]))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["qty"], 2)
        # второй раз - без задвоения
        r = self.client.post("/part-orders/collect")
        self.assertIn("Новых потребностей нет", self.get_ok(r.headers["location"]))
        self.assertEqual(len(tw.run(self.crm.part_order_items(order["id"]))), 1)

    def test_order_goes_to_supplier_and_comes_back_as_a_receipt(self):
        supplier_id = tw.run(self.crm.create_supplier(name="ВелоЗапчасть",
                                                      phone=None, note=None))
        self.client.post("/part-orders/collect")
        order = tw.run(self.crm.open_part_order())
        self.client.post(f"/part-orders/{order['id']}/status",
                         data={"status": "ordered", "supplier_id": supplier_id})
        self.assertEqual(tw.run(self.crm.part_order(order["id"]))["status"], "ordered")
        r = self.client.post(f"/part-orders/{order['id']}/receive")
        self.assertIn("приход ПРХ-000001", self.get_ok(r.headers["location"]))
        self.assertEqual(tw.run(self.crm.part_stock(self.part_id)), 2)
        done = tw.run(self.crm.part_order(order["id"]))
        self.assertEqual(done["status"], "received")
        self.assertIsNotNone(done["doc_id"])

    def test_second_receive_is_refused(self):
        self.client.post("/part-orders/collect")
        order = tw.run(self.crm.open_part_order())
        self.client.post(f"/part-orders/{order['id']}/status", data={"status": "ordered"})
        self.client.post(f"/part-orders/{order['id']}/receive")
        r = self.client.post(f"/part-orders/{order['id']}/receive")
        self.assertIn("уже принят", self.get_ok(r.headers["location"]))
        self.assertEqual(tw.run(self.crm.part_stock(self.part_id)), 2)

    def test_manual_line_is_not_duplicated(self):
        self.client.post("/part-orders/items", data={"part_id": self.part_id, "qty": "3"})
        r = self.client.post("/part-orders/items",
                             data={"part_id": self.part_id, "qty": "3"})
        self.assertIn("уже есть", self.get_ok(r.headers["location"]))
        order = tw.run(self.crm.open_part_order())
        self.assertEqual(len(tw.run(self.crm.part_order_items(order["id"]))), 1)


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
