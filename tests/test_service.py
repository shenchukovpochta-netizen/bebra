"""Сервисный контур: виды работ, наряды, рабочий стол.

Чистая логика и панель через TestClient (обвязка из tests/test_web.py).
Главное, что проверяется: наряд и статус велосипеда не расходятся, деньги
чужого ремонта не попадают в журнал аренды, а закрытый наряд наполняет
тот же отчёт «что ломается», что и ручной ремонт.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, date, datetime, timedelta
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
TODAY = date(2026, 9, 16)


def order(days_open: int = 0, status: str = "in_work", **extra) -> dict:
    opened = datetime(2026, 9, 16, 12, tzinfo=UTC) - timedelta(days=days_open)
    return {"id": 1, "no": "РЕМ-000001", "status": status,
            "opened_at": opened, "closed_at": None, **extra}


class TestOrderLogic(unittest.TestCase):
    def test_order_number_is_human_readable(self):
        self.assertEqual(logic.order_no(1), "РЕМ-000001")
        self.assertEqual(logic.order_no(12485), "РЕМ-012485")

    def test_totals_count_quantity(self):
        items = [{"price": "200", "qty": 2, "parts_cost": "50", "labor_cost": "30"},
                 {"price": "600", "qty": 1, "parts_cost": "0", "labor_cost": "100"}]
        totals = logic.order_totals(items)
        self.assertEqual(totals["total"], D(1000))
        self.assertEqual(totals["cost"], D(260))
        self.assertEqual(totals["margin"], D(740))
        self.assertEqual(totals["lines"], 2)
        empty = logic.order_totals([])
        self.assertEqual(empty["total"], D(0))
        self.assertEqual(empty["lines"], 0)

    def test_days_and_stuck(self):
        self.assertEqual(logic.order_days(order(days_open=4), today=TODAY), 4)
        self.assertTrue(logic.order_stuck(order(days_open=4), today=TODAY))
        self.assertFalse(logic.order_stuck(order(days_open=1), today=TODAY))
        # закрытый наряд считается по дате закрытия и простоя не копит
        closed = order(days_open=10, status="done",
                       closed_at=datetime(2026, 9, 9, tzinfo=UTC))
        self.assertEqual(logic.order_days(closed, today=TODAY), 3)
        self.assertFalse(logic.order_stuck(closed, today=TODAY))

    def test_open_states(self):
        for status in ("new", "in_work", "waiting"):
            self.assertTrue(logic.order_is_open(order(status=status)), status)
        for status in ("done", "cancelled"):
            self.assertFalse(logic.order_is_open(order(status=status)), status)
        self.assertFalse(logic.order_is_open(None))

    def test_service_rows_put_bikes_without_an_order_first(self):
        """Велосипед в ремонте без наряда - это потерянный велосипед:
        он копит простой, а в отчётах выглядит как обычный ремонт."""
        bikes = [{"id": 1, "code": "B-1", "status": "repair", "idle_days": 2},
                 {"id": 2, "code": "B-2", "status": "repair", "idle_days": 9},
                 {"id": 3, "code": "B-3", "status": "available", "idle_days": 30},
                 {"id": 4, "code": "B-4", "status": "maintenance", "idle_days": 1}]
        orders = {1: order(days_open=1)}
        rows = logic.service_rows(bikes, orders, today=TODAY)
        self.assertEqual([r["code"] for r in rows], ["B-2", "B-4", "B-1"],
                         "без наряда - первыми, по убыванию суток")
        self.assertNotIn("B-3", [r["code"] for r in rows], "свободный не в сервисе")
        self.assertEqual(rows[0]["stage"], "Без наряда")
        self.assertEqual(rows[2]["stage"], "В работе")
        summary = logic.service_summary(rows)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["no_order"], 2)
        self.assertEqual(summary["stuck"], 2, "оба без наряда считаются стоящими")

    def test_checks(self):
        self.assertTrue(logic.check_payer("client").ok)
        self.assertFalse(logic.check_payer("сосед").ok)
        self.assertTrue(logic.check_order_status("waiting").ok)
        self.assertFalse(logic.check_order_status("готово").ok)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestServiceInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def open_order(self, **over):
        data = {"bike_id": self.bike_id, "payer": "own", "estimate": "0",
                "complaint": "не едет"}
        data.update(over)
        return self.client.post("/orders", data=data)

    def current(self):
        return tw.run(self.crm.open_order_of(self.bike_id))

    # ─── открытие ───

    def test_opening_an_order_takes_the_bike_to_repair(self):
        r = self.open_order()
        self.assertEqual(r.status_code, 303)
        order = self.current()
        self.assertIsNotNone(order)
        self.assertEqual(order["no"], "РЕМ-000001")
        self.assertEqual(order["status"], "new")
        self.assertEqual(tw.run(self.crm.bike(self.bike_id))["status"], "repair",
                         "наряд открыт - велосипед не должен числиться свободным")

    def test_desk_counts_approval_and_parts_and_lists_spares(self):
        self.open_order()
        order = self.current()
        tw.run(self.crm.update_work_order(order["id"], status="waiting"))
        spare = tw.run(self.crm.create_bike(code="B-9", model="Truck+", spare=True))
        page = self.get_ok("/service")
        self.assertIn("ждём запчасть", page)
        self.assertIn('href="/orders?status=waiting"><b>1</b>', page)
        self.assertIn('href="/orders?status=approve"><b>0</b>', page)
        self.assertIn("Подменный фонд", page)
        self.assertIn(f"/bikes/{spare}", page)
        past = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        self.assertEqual(self.client.get(f"/service?month={past}").status_code, 200)
        self.assertIn(f"/service?month={past}", self.get_ok("/service"),
                      "стрелка на прошлый месяц")
        summary = logic.service_summary(logic.service_rows(
            tw.run(self.crm.bikes(limit=100)), tw.run(self.crm.open_orders_by_bike())))
        self.assertEqual((summary["waiting"], summary["approving"]), (1, 0))

    def test_payer_changes_while_nothing_was_promised(self):
        """«Наш» ремонт оказался клиентским после разборки: плательщик
        меняется на месте, а не закрытием и новым нарядом."""
        self.open_order()
        order = self.current()
        r = self.client.post(f"/orders/{order['id']}/edit", data={
            "status": "in_work", "tech_id": "", "estimate": "0", "note": "",
            "payer": "client", "client_phone": ""})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.current()["payer"], "own", "без клиента клиентским не станет")
        self.assertIn("укажите клиента", self.get_ok(f"/orders/{order['id']}"))
        self.client.post(f"/orders/{order['id']}/edit", data={
            "status": "in_work", "tech_id": "", "estimate": "0", "note": "",
            "payer": "client", "client_phone": "8 (999) 000-00-00"})
        order = self.current()
        self.assertEqual(order["payer"], "client")
        self.assertEqual(order["client_id"], self.client_id, "клиент найден по телефону")
        # Смета ушла - плательщик заперт.
        tw.run(self.crm.update_work_order(order["id"], estimate_sent_at=datetime.now(UTC)))
        self.client.post(f"/orders/{order['id']}/edit", data={
            "status": "in_work", "tech_id": "", "estimate": "0", "note": "",
            "payer": "own"})
        self.assertEqual(self.current()["payer"], "client")
        self.assertIn("не сменить", self.get_ok(f"/orders/{order['id']}"))
        self.assertIn("disabled", self.get_ok(f"/orders/{order['id']}"))

    def test_unknown_phone_does_not_bind_a_client(self):
        self.open_order()
        order = self.current()
        self.client.post(f"/orders/{order['id']}/edit", data={
            "status": "in_work", "tech_id": "", "estimate": "0", "note": "",
            "payer": "own", "client_phone": "+79991112233"})
        self.assertIsNone(self.current().get("client_id"))
        self.assertIn("нет", self.get_ok(f"/orders/{order['id']}"))

    def test_work_type_category_and_node_are_editable(self):
        self.client.post("/work-types", data={"title": "Замена мотор-колеса",
                                              "category": "Электрика", "minutes": "90",
                                              "price": "1500", "node": ""})
        row = tw.run(self.crm.work_types())[0]
        r = self.client.post(f"/work-types/{row['id']}", data={
            "title": "Замена мотор-колеса", "minutes": "90", "price": "1500",
            "category": "Ходовая", "node": "motor_wheel"})
        self.assertEqual(r.status_code, 303)
        row = tw.run(self.crm.work_type(row["id"]))
        self.assertEqual(row["category"], "Ходовая")
        self.assertEqual(row["node"], "motor_wheel")
        # Чужая категория и чужой узел отбиваются.
        self.client.post(f"/work-types/{row['id']}", data={
            "title": "Замена мотор-колеса", "minutes": "90", "price": "1500",
            "category": "Кузов", "node": "motor_wheel"})
        self.assertEqual(tw.run(self.crm.work_type(row["id"]))["category"], "Ходовая")
        self.client.post(f"/work-types/{row['id']}", data={
            "title": "Замена мотор-колеса", "minutes": "90", "price": "1500",
            "category": "Ходовая", "node": "nonsense"})
        self.assertEqual(tw.run(self.crm.work_type(row["id"]))["node"], "motor_wheel")
        page = self.get_ok("/work-types")
        self.assertIn('name="category" form="work', page, "категория правится в строке")

    def test_second_order_on_the_same_bike_is_refused(self):
        self.open_order()
        r = self.open_order()
        self.assertEqual(r.status_code, 303)
        self.assertIn("уже открыт наряд", self.get_ok(r.headers["location"]))
        self.assertEqual(len(tw.run(self.crm.work_orders())), 1)

    def test_order_without_bike_needs_an_object(self):
        r = self.open_order(bike_id="", object_note="")
        self.assertIn("Укажите велосипед", self.get_ok(r.headers["location"]))

    def test_foreign_object_order(self):
        """Чужая техника - второе направление: велосипеда в парке нет."""
        r = self.open_order(bike_id="", payer="client",
                            object_note="Самокат Kugoo M4", estimate="1500")
        self.assertEqual(r.status_code, 303)
        orders = tw.run(self.crm.work_orders())
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["payer"], "client")
        self.assertIsNone(orders[0]["bike_id"])
        self.assertEqual(orders[0]["estimate"], D(1500))

    # ─── строки и закрытие ───

    def add_item(self, order_id, **over):
        data = {"title": "Замена камеры", "node": "tube_tire", "qty": "2",
                "price": "200", "parts_cost": "50", "labor_cost": "30"}
        data.update(over)
        return self.client.post(f"/orders/{order_id}/items", data=data)

    def test_items_and_closing_write_the_repair_log(self):
        self.open_order()
        oid = self.current()["id"]
        self.add_item(oid)
        page = self.get_ok(f"/orders/{oid}")
        self.assertIn("Замена камеры", page)
        self.assertIn("400", page, "клиенту: 200 x 2")

        r = self.client.post(f"/orders/{oid}/close", data={"bike_status": "available"})
        self.assertEqual(r.status_code, 303)
        fresh = tw.run(self.crm.work_order(oid))
        self.assertEqual(fresh["status"], "done")
        self.assertEqual(fresh["total"], D(400))
        self.assertEqual(fresh["cost"], D(160), "себестоимость: (50+30) x 2")
        self.assertIsNotNone(fresh["closed_at"])
        self.assertEqual(tw.run(self.crm.bike(self.bike_id))["status"], "available")

        # отчёт «что ломается» собирается по тем же записям, что и раньше
        log = tw.run(self.crm.bike_log(self.bike_id, kind="repair"))
        self.assertEqual(len(log), 1)
        self.assertIn("РЕМ-000001", log[0]["note"])
        self.assertEqual(log[0]["cost"], D(160))
        stats = tw.run(self.crm.repair_stats(
            datetime.now(UTC) - timedelta(days=1), datetime.now(UTC) + timedelta(days=1)))
        self.assertEqual([n["code"] for n in stats["by_node"]], ["tube_tire"])
        self.assertEqual(stats["by_node"][0]["cost"], D(160))

    def test_client_repair_money_stays_out_of_the_rental_ledger(self):
        """Красная линия: журнал - это аренда, по нему считается средний
        чек парка. Выручка чужого ремонта его бы испортила."""
        self.open_order(payer="client", client_id=self.client_id)
        oid = self.current()["id"]
        self.add_item(oid, price="1500", parts_cost="200", labor_cost="100")
        self.client.post(f"/orders/{oid}/close", data={"bike_status": "available"})
        self.assertEqual(tw.run(self.crm.client_balance(self.client_id)), 0,
                         "ремонт не уходит в баланс клиента")
        self.assertEqual(tw.run(self.crm.ledger_of(self.client_id, 50)), [])
        r = self.client.post(f"/orders/{oid}/paid")
        self.assertEqual(r.status_code, 303)
        self.assertIsNotNone(tw.run(self.crm.work_order(oid))["paid_at"])
        self.assertEqual(tw.run(self.crm.client_balance(self.client_id)), 0)

    def test_closed_order_takes_no_more_items(self):
        self.open_order()
        oid = self.current()["id"]
        self.add_item(oid)
        self.client.post(f"/orders/{oid}/close", data={"bike_status": "available"})
        r = self.add_item(oid, title="Ещё работа")
        self.assertEqual(r.status_code, 303)
        self.assertIn("Наряд закрыт", self.get_ok(f"/orders/{oid}"))
        self.assertEqual(len(tw.run(self.crm.order_items(oid))), 1)

    def test_status_done_is_not_set_by_hand(self):
        """«Готов» руками обошёл бы подсчёт суммы и запись ремонта."""
        self.open_order()
        oid = self.current()["id"]
        r = self.client.post(f"/orders/{oid}/edit",
                             data={"status": "done", "estimate": "0"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("кнопкой", self.get_ok(f"/orders/{oid}"))
        self.assertEqual(tw.run(self.crm.work_order(oid))["status"], "new")

    def test_item_can_be_removed_while_open(self):
        self.open_order()
        oid = self.current()["id"]
        self.add_item(oid)
        item_id = tw.run(self.crm.order_items(oid))[0]["id"]
        r = self.client.post(f"/orders/{oid}/items/{item_id}/delete")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(tw.run(self.crm.order_items(oid)), [])

    # ─── рабочий стол ───

    def test_service_desk_shows_bikes_without_an_order(self):
        tw.run(self.crm.update_bike(self.bike_id, status="repair"))
        page = self.get_ok("/service")
        self.assertIn("Без наряда", page)
        self.assertIn("в ремонте без наряда", page)
        self.assertIn("B-1", page)

    def test_bike_card_warns_about_a_mismatch(self):
        self.open_order()
        tw.run(self.crm.update_bike(self.bike_id, status="available"))
        page = self.get_ok(f"/bikes/{self.bike_id}")
        self.assertIn("открыт, а статус", page)
        self.assertIn("попадёт в выдачу как свободный", page)

    def test_bike_in_repair_without_an_order_is_flagged_on_its_card(self):
        tw.run(self.crm.update_bike(self.bike_id, status="repair"))
        page = self.get_ok(f"/bikes/{self.bike_id}")
        self.assertIn("наряда нет", page)

    # ─── виды работ ───

    def test_work_type_catalogue(self):
        page = self.get_ok("/work-types")
        self.assertIn("Замена камеры", page)
        r = self.client.post("/work-types", data={
            "title": "Замена спицы", "category": "Ходовая", "minutes": "20",
            "price": "250", "node": "wheel_rear"})
        self.assertEqual(r.status_code, 303)
        titles = [t["title"] for t in tw.run(self.crm.work_types())]
        self.assertIn("Замена спицы", titles)
        # повтор названия
        self.client.post("/work-types", data={"title": "Замена спицы",
                                              "category": "Ходовая",
                                              "minutes": "20", "price": "250"})
        self.assertIn("уже есть", self.get_ok("/work-types"))

    def test_item_from_the_catalogue_takes_its_title_and_node(self):
        self.open_order()
        oid = self.current()["id"]
        work_type = tw.run(self.crm.work_types(active_only=True))[0]
        r = self.client.post(f"/orders/{oid}/items", data={
            "work_type_id": work_type["id"], "qty": "1",
            "price": str(work_type["price"]), "parts_cost": "0", "labor_cost": "0"})
        self.assertEqual(r.status_code, 303)
        item = tw.run(self.crm.order_items(oid))[0]
        self.assertEqual(item["title"], work_type["title"])
        self.assertEqual(item["node"], work_type["node"])

    # ─── доступы ───

    def test_mechanic_can_work_orders_manager_only_looks(self):
        profile = tw.run(self.crm.access_profile_by_code("tech"))
        tw.run(self.crm.create_staff("petr", logic.hash_password("password-1"),
                                     "Пётр", "manager", profile["id"]))
        self.client.post("/logout")
        self.login("petr", "password-1")
        self.get_ok("/service")
        self.get_ok("/orders/new")

        manager = tw.run(self.crm.access_profile_by_code("manager"))
        tw.run(self.crm.create_staff("ivan", logic.hash_password("password-1"),
                                     "Иван", "manager", manager["id"]))
        self.client.post("/logout")
        self.login("ivan", "password-1")
        self.get_ok("/orders")
        self.assertEqual(self.client.get("/orders/new").status_code, 403)
        self.assertEqual(self.client.post("/orders", data={"payer": "own"}).status_code,
                         403)


if __name__ == "__main__":
    unittest.main()
