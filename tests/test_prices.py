"""Прайс сервиса: два листа, подстановка в наряд, штрафы в журнале.

Лист выбирается по объекту наряда, цена клиенту - работа плюс запчасть,
отсутствие работы в листе - None, а не ноль: ноль в прайсе честный
(колодки в рамках ТО), и наряд обязан их различать.
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

    from app.crm import service
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal


def work(**over) -> dict:
    row = {"id": 1, "title": "Пайка фары", "category": "Передняя часть",
           "active": True, "price": D(600), "parts_price": D(0),
           "price_ext": D(900), "parts_price_ext": D(0), "node": "headlight"}
    row.update(over)
    return row


class TestSheets(unittest.TestCase):
    def test_sheet_is_chosen_by_the_object_not_the_payer(self):
        self.assertEqual(logic.price_sheet({"bike_id": 7, "payer": "client"}), "own")
        self.assertEqual(logic.price_sheet({"bike_id": 7, "payer": "own"}), "own")
        self.assertEqual(logic.price_sheet({"bike_id": None, "payer": "client",
                                            "object_note": "Kugoo клиента"}), "ext")
        self.assertEqual(logic.price_sheet(None), "ext")

    def test_price_is_work_plus_parts(self):
        row = work(price=D(1000), parts_price=D(3500),
                   price_ext=D(2500), parts_price_ext=D(4500))
        self.assertEqual(logic.sheet_price(row, "own"), D(4500))
        self.assertEqual(logic.sheet_price(row, "ext"), D(7000))
        self.assertEqual(logic.sheet_price(row), D(4500), "лист по умолчанию - арендатор")

    def test_missing_sheet_is_none_and_zero_is_zero(self):
        row = work(price=D(0), parts_price=D(0), price_ext=None)
        self.assertEqual(logic.sheet_price(row, "own"), D(0))
        self.assertIsNone(logic.sheet_price(row, "ext"))
        rows = logic.work_type_rows([row])
        self.assertEqual(rows[0]["own_total"], D(0))
        self.assertIsNone(rows[0]["ext_total"])
        self.assertIsNone(logic.priced_types([row], "ext")[0]["sheet_total"])
        self.assertEqual(logic.priced_types([row], "own")[0]["sheet_total"], D(0))

    def test_parts_without_work_is_a_price(self):
        """«Замена зеркал»: работа прочерком, запчасть 500 - цена 500."""
        self.assertEqual(logic.sheet_price(work(price=D(0), parts_price=D(500))), D(500))

    def test_fine_presets_put_fines_first_and_skip_free_and_missing(self):
        rows = [work(id=1, title="Пайка фары", category="Передняя часть"),
                work(id=2, title="Потеря сумки", category=logic.FINES_CATEGORY,
                     price=D(0), parts_price=D(2000)),
                work(id=3, title="ТО", category="ТО", price=D(0), parts_price=D(0)),
                work(id=4, title="Заварить раму", category="Рама и резьба", price=None),
                work(id=5, title="Старое", active=False)]
        got = logic.fine_presets(rows)
        self.assertEqual([r["id"] for r in got], [2, 1])
        self.assertEqual(got[0]["total"], D(2000))

    def test_categories_keep_the_old_ones(self):
        for category in ("Передняя часть", "Штрафы и порча имущества", "Гидроизоляция",
                         "Электрика", "Прочее"):
            self.assertIn(category, logic.WORK_CATEGORIES)
        self.assertEqual(logic.FINES_CATEGORY, "Штрафы и порча имущества")


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestPricePanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def add_type(self, title="Пайка ручки газа", **over) -> dict:
        fields = {"category": "Передняя часть", "minutes": 30, "price": D(800),
                  "node": "throttle", "parts_price": D(0), "price_ext": D(1500),
                  "parts_price_ext": D(0)}
        fields.update(over)
        type_id = tw.run(self.crm.create_work_type(title=title, **fields))
        return tw.run(self.crm.work_type(type_id))

    def open(self, *, payer="client", foreign=False) -> int:
        client = tw.run(self.crm.client(self.client_id))
        bike = None if foreign else tw.run(self.crm.bike(self.bike_id))
        return tw.run(service.open_order(
            self.crm, bike=bike, payer=payer, client=client if payer == "client" else None,
            complaint="не едет", object_note="Kugoo клиента" if foreign else None,
            tech_id=None, estimate=D(0), by="t"))

    def items(self, order_id: int) -> list[dict]:
        return tw.run(self.crm.order_items(order_id))

    def test_catalogue_holds_two_sheets(self):
        r = self.client.post("/work-types", data={
            "title": "Пайка ручки газа", "category": "Передняя часть", "minutes": "30",
            "price": "800", "parts_price": "", "price_ext": "1500", "parts_price_ext": ""})
        self.assertEqual(r.status_code, 303)
        row = next(t for t in tw.run(self.crm.work_types())
                   if t["title"] == "Пайка ручки газа")
        self.assertEqual((row["price"], row["parts_price"]), (D(800), D(0)))
        self.assertEqual((row["price_ext"], row["parts_price_ext"]), (D(1500), D(0)))
        page = self.get_ok("/work-types")
        self.assertIn("Арендатору", page)
        self.assertIn(logic.money(D(1500)), page)
        # Оба поля листа пустые - работы в этом прайсе нет.
        self.client.post(f"/work-types/{row['id']}", data={
            "title": "Пайка ручки газа", "minutes": "30", "category": "Передняя часть",
            "price": "800", "parts_price": "0", "price_ext": "", "parts_price_ext": ""})
        row = tw.run(self.crm.work_type(row["id"]))
        self.assertIsNone(row["price_ext"])
        self.assertIn("нет в прайсе", self.get_ok("/work-types"))
        # Запчасть без работы - это лист с работой ноль.
        self.client.post(f"/work-types/{row['id']}", data={
            "title": "Пайка ручки газа", "minutes": "30", "category": "Передняя часть",
            "price": "", "parts_price": "300", "price_ext": "", "parts_price_ext": ""})
        row = tw.run(self.crm.work_type(row["id"]))
        self.assertEqual((row["price"], row["parts_price"]), (D(0), D(300)))
        # Мусор в цене отбивается, строка не портится.
        self.client.post(f"/work-types/{row['id']}", data={
            "title": "Пайка ручки газа", "minutes": "30", "category": "Передняя часть",
            "price": "abc", "parts_price": "", "price_ext": "", "parts_price_ext": ""})
        self.assertEqual(tw.run(self.crm.work_type(row["id"]))["parts_price"], D(300))
        csv = self.client.get("/work-types.csv").text
        self.assertIn("Стороннему: итого", csv)
        self.assertEqual(self.client.get("/work-types.xlsx").status_code, 200)
        self.assertEqual(self.client.get("/work-types?sort=price_ext&dir=desc").status_code,
                         200)

    def test_order_on_own_bike_takes_the_tenant_price_when_the_client_pays(self):
        work_type = self.add_type()
        order_id = self.open(payer="client")
        page = self.get_ok(f"/orders/{order_id}")
        self.assertIn("Из прайса «Арендатору»", page)
        self.assertIn(logic.money(D(800)), page)
        r = self.client.post(f"/orders/{order_id}/items", data={
            "work_type_id": work_type["id"], "qty": "1", "price": "",
            "parts_cost": "0", "labor_cost": "0"})
        self.assertEqual(r.status_code, 303)
        item = self.items(order_id)[0]
        self.assertEqual(item["price"], D(800))
        self.assertEqual(item["title"], "Пайка ручки газа")
        self.assertEqual(item["node"], "throttle")
        # Цена руками важнее прайса, и «0» руками - это ноль.
        self.client.post(f"/orders/{order_id}/items", data={
            "work_type_id": work_type["id"], "qty": "1", "price": "999",
            "parts_cost": "0", "labor_cost": "0"})
        self.client.post(f"/orders/{order_id}/items", data={
            "work_type_id": work_type["id"], "qty": "1", "price": "0",
            "parts_cost": "0", "labor_cost": "0"})
        self.assertEqual([i["price"] for i in self.items(order_id)],
                         [D(800), D(999), D(0)])

    def test_foreign_bike_takes_the_external_price(self):
        work_type = self.add_type()
        order_id = self.open(payer="client", foreign=True)
        self.assertIn("Из прайса «Стороннему»", self.get_ok(f"/orders/{order_id}"))
        self.client.post(f"/orders/{order_id}/items", data={
            "work_type_id": work_type["id"], "qty": "2", "price": "",
            "parts_cost": "0", "labor_cost": "0"})
        self.assertEqual(self.items(order_id)[0]["price"], D(1500))
        # Работы нет в листе стороннего - цену просят руками, строки нет.
        missing = self.add_type("Замена / установка корзинки", price=D(200),
                                parts_price=D(1600), price_ext=None)
        r = self.client.post(f"/orders/{order_id}/items", data={
            "work_type_id": missing["id"], "qty": "1", "price": "",
            "parts_cost": "0", "labor_cost": "0"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("нет - укажите цену", self.get_ok(f"/orders/{order_id}"))
        self.assertEqual(len(self.items(order_id)), 1)
        self.assertIn("нет в прайсе", self.get_ok(f"/orders/{order_id}"))

    def test_own_repair_does_not_take_a_client_price(self):
        work_type = self.add_type()
        order_id = self.open(payer="own")
        self.client.post(f"/orders/{order_id}/items", data={
            "work_type_id": work_type["id"], "qty": "1", "price": "",
            "parts_cost": "100", "labor_cost": "50"})
        item = self.items(order_id)[0]
        self.assertEqual(item["price"], D(0), "свой ремонт: цена клиенту ни к чему")
        self.assertEqual((item["parts_cost"], item["labor_cost"]), (D(100), D(50)))

    def test_fine_preset_fills_amount_and_note(self):
        preset = self.add_type("Потеря курьерской сумки", category=logic.FINES_CATEGORY,
                               minutes=0, price=D(0), node=None, parts_price=D(2000),
                               price_ext=None)
        page = self.get_ok(f"/clients/{self.client_id}")
        self.assertIn("Штраф из прайса", page)
        self.assertIn("Потеря курьерской сумки", page)
        r = self.client.post(f"/clients/{self.client_id}/ledger", data={
            "kind": "fine", "amount": "", "note": "", "preset": str(preset["id"])})
        self.assertEqual(r.status_code, 303)
        rows = tw.run(self.crm.ledger_of(self.client_id, 10))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "fine")
        self.assertEqual(abs(rows[0]["amount"]), D(2000))
        self.assertEqual(rows[0]["note"], "Потеря курьерской сумки")
        # Платёж по позиции прайса - описка, не запись.
        self.client.post(f"/clients/{self.client_id}/ledger", data={
            "kind": "payment", "amount": "", "note": "", "preset": str(preset["id"])})
        self.assertIn("Штраф / ремонт", self.get_ok(f"/clients/{self.client_id}"))
        self.assertEqual(len(tw.run(self.crm.ledger_of(self.client_id, 10))), 1)
        # Своя сумма важнее прайса: порча перчаток «от 500».
        self.client.post(f"/clients/{self.client_id}/ledger", data={
            "kind": "fine", "amount": "1500", "note": "", "preset": str(preset["id"])})
        rows = tw.run(self.crm.ledger_of(self.client_id, 10))
        self.assertEqual({abs(r["amount"]) for r in rows}, {D(2000), D(1500)})
        # Чужой id - ошибка, а не молчаливый ноль.
        self.client.post(f"/clients/{self.client_id}/ledger", data={
            "kind": "fine", "amount": "", "note": "", "preset": "9999"})
        self.assertIn("Такой позиции в прайсе нет", self.get_ok(f"/clients/{self.client_id}"))


if __name__ == "__main__":
    unittest.main()
