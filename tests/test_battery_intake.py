"""Паспорт аккумулятора и его сверка.

Правило то же, что у велосипеда: пока не сверено — в оборот не выйдет.
И то же, ради чего сверку заводили: отметка на пустом поле — это ровно
«переписал из накладной», поэтому пустое поле сверить нельзя.

Отдельно стережём табличку: в каталоге лежит паспорт модели, а на
корпусе — табличка конкретной батареи, и сверяют то, что на корпусе.
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


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def battery(**over):
    row = {"id": 1, "code": "9510001", "status": "new", "serial_no": "SN-1",
           "model_title": "Аккумулятор 70 Ач", "volts": 60,
           "amp_hours": D("70.00"), "checked": {}}
    row.update(over)
    return row


def all_checked(fields=None):
    return {f: {"at": "2026-09-17T10:00:00", "by": "staff:t"}
            for f in (fields or logic.BATTERY_PASSPORT)}


class TestBatteryPassport(unittest.TestCase):
    def test_every_field_is_listed(self):
        rows = logic.battery_checks(battery())
        self.assertEqual([r["field"] for r in rows],
                         list(logic.BATTERY_PASSPORT))

    def test_model_comes_from_the_catalogue(self):
        self.assertEqual(logic.battery_field_value(battery(), "model"),
                         "Аккумулятор 70 Ач")
        self.assertEqual(
            logic.battery_field_value(battery(model_title=None), "model"), "")

    def test_plate_values_are_read_from_the_case(self):
        self.assertEqual(logic.battery_field_value(battery(), "volts"), "60")
        self.assertEqual(logic.battery_field_value(battery(), "amp_hours"),
                         "70.00")
        self.assertEqual(
            logic.battery_field_value(battery(amp_hours=None), "amp_hours"), "",
            "не переписана с корпуса — сверять нечего")

    def test_zero_is_not_an_empty_plate(self):
        self.assertEqual(logic.battery_field_value(battery(volts=0), "volts"), "0")

    def test_mark_on_an_empty_field_does_not_count(self):
        row = battery(serial_no=None, checked=all_checked())
        state = logic.battery_check_state(row)
        self.assertIn("Серийный номер (на корпусе)", state["left"])
        self.assertFalse(state["done"])

    def test_all_checked_lets_it_out(self):
        state = logic.battery_check_state(battery(checked=all_checked()))
        self.assertTrue(state["done"])
        self.assertTrue(state["can_commission"])
        self.assertTrue(state["new"])

    def test_photo_is_required_only_where_asked(self):
        settings = {"bike_photo_required": "1"}
        rows = {r["field"]: r for r in logic.battery_checks(
            battery(checked=all_checked()), settings)}
        self.assertTrue(rows["serial_no"]["needs_photo"])
        self.assertTrue(rows["amp_hours"]["needs_photo"])
        self.assertFalse(rows["code"]["needs_photo"],
                         "наклейку видно и так")
        self.assertFalse(rows["serial_no"]["ok"], "отметка без снимка не считается")

    def test_photo_makes_it_count(self):
        marks = all_checked()
        marks["serial_no"]["photo"] = "akb-1-serial_no.jpg"
        marks["amp_hours"]["photo"] = "akb-1-amp_hours.jpg"
        state = logic.battery_check_state(battery(checked=marks),
                                          {"bike_photo_required": "1"})
        self.assertTrue(state["done"])

    def test_without_the_requirement_the_list_is_only_a_hint(self):
        state = logic.battery_check_state(battery(), {"bike_check_required": "0"})
        self.assertFalse(state["done"])
        self.assertTrue(state["can_commission"],
                        "владелец снял требование — кнопка не заперта")

    def test_new_is_out_of_the_operational_fleet(self):
        self.assertNotIn("new", logic.BATTERY_OPERATIONAL)
        self.assertNotIn("new", logic.BATTERY_MANUAL_STATUSES)
        self.assertIn("new", logic.BATTERY_STATUSES)


class TestPlateChecks(unittest.TestCase):
    def test_volts(self):
        self.assertEqual(logic.check_volts("60").value, 60)
        self.assertEqual(logic.check_volts("").value, None)
        self.assertFalse(logic.check_volts("1000").ok, "это не велосипед")
        self.assertFalse(logic.check_volts("12 вольт").ok)

    def test_amp_hours(self):
        self.assertEqual(logic.check_amp_hours("70").value, D("70.00"))
        self.assertEqual(logic.check_amp_hours("20,5").value, D("20.50"),
                         "запятая - это тоже число")
        self.assertEqual(logic.check_amp_hours("").value, None)
        self.assertFalse(logic.check_amp_hours("0").ok, "ноль - не ёмкость")
        self.assertFalse(logic.check_amp_hours("много").ok)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestBatteryIntakePages(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.model_id = _run(self.crm.create_battery_model(
            title="Аккумулятор 70 Ач", brand=None, voltage=60, capacity=D(70),
            price=D(12000), service_months=15, note=None))

    def create(self, **over):
        data = {"code": "9510001", "model_id": str(self.model_id),
                "serial_no": "SN-1", "volts": "60", "amp_hours": "70",
                "service_months": "15", "cycles": "0", "note": "", "location": ""}
        data.update(over)
        r = self.client.post("/batteries", data=data)
        return int(r.headers["location"].rsplit("/", 1)[1])

    def check_all(self, battery_id):
        for field in logic.BATTERY_PASSPORT:
            self.client.post(f"/batteries/{battery_id}/check",
                             data={"field": field, "action": "check"})

    def test_new_battery_lands_on_assembly(self):
        battery_id = self.create()
        self.assertEqual(_run(self.crm.battery(battery_id))["status"], "new")
        self.assertIn("на сборке", self.get_ok(f"/batteries/{battery_id}"))
        self.assertIn("9510001", self.get_ok("/intake"))

    def test_it_cannot_be_released_by_the_status_select(self):
        battery_id = self.create()
        self.client.post(f"/batteries/{battery_id}/status",
                         data={"status": "available"})
        self.assertEqual(_run(self.crm.battery(battery_id))["status"], "new",
                         "выпускает только кнопка ввода в эксплуатацию")

    def test_commission_needs_every_field(self):
        battery_id = self.create()
        self.client.post(f"/batteries/{battery_id}/check",
                         data={"action": "commission"})
        self.assertEqual(_run(self.crm.battery(battery_id))["status"], "new")
        self.check_all(battery_id)
        self.client.post(f"/batteries/{battery_id}/check",
                         data={"action": "commission"})
        row = _run(self.crm.battery(battery_id))
        self.assertEqual(row["status"], "available")
        self.assertIsNotNone(row["commissioned_at"])

    def test_empty_plate_cannot_be_checked(self):
        battery_id = self.create(amp_hours="")
        self.client.post(f"/batteries/{battery_id}/check",
                         data={"field": "amp_hours", "action": "check"})
        marks = _run(self.crm.battery(battery_id))["checked"] or {}
        self.assertNotIn("amp_hours", marks,
                         "отметка на пустоте - это и есть «переписал из накладной»")

    def test_check_can_be_taken_back(self):
        battery_id = self.create()
        self.check_all(battery_id)
        self.client.post(f"/batteries/{battery_id}/check",
                         data={"field": "code", "action": "clear"})
        marks = _run(self.crm.battery(battery_id))["checked"] or {}
        self.assertNotIn("code", marks)

    def test_with_the_requirement_off_it_is_free_from_the_start(self):
        _run(self.crm.set_setting("bike_check_required", "0", by="тест"))
        battery_id = self.create(code="9510002")
        self.assertEqual(_run(self.crm.battery(battery_id))["status"], "available")

    def test_plate_is_on_the_card(self):
        battery_id = self.create()
        text = self.get_ok(f"/batteries/{battery_id}")
        self.assertIn("По табличке", text)
        self.assertIn("Напряжение по табличке", text)

    def test_bad_plate_is_refused(self):
        r = self.client.post("/batteries", data={
            "code": "9510009", "model_id": str(self.model_id), "serial_no": "",
            "volts": "1000", "amp_hours": "70", "service_months": "15",
            "cycles": "0", "note": "", "location": ""})
        self.assertEqual(r.status_code, 303)
        self.assertEqual([b for b in _run(self.crm.batteries())
                          if b["code"] == "9510009"], [])


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
