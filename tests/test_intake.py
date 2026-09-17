"""Ввод техники в эксплуатацию: статус «на сборке» и сверка паспорта.

Смысл сверки в том, чтобы человек посмотрел на технику, а не переписал
номер из накладной. Поэтому отметка ставится по каждому полю отдельно,
на пустом поле не ставится вовсе, а когда владелец потребовал снимок —
без снимка не считается.
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


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class TestIntakeLogic(unittest.TestCase):
    def bike(self, **over):
        row = {"id": 1, "status": "new", "model": "Kugoo", "code": "B-1",
               "frame_no": "DEMO-1", "plate_no": "АА 1234", "tracker_ok": True,
               "checked": {}}
        row.update(over)
        return row

    def all_checked(self, fields=None, photo=None):
        mark = {"at": "2026-09-17T10:00:00", "by": "staff:1"}
        if photo:
            mark = {**mark, "photo": photo}
        return {f: dict(mark) for f in (fields or logic.BIKE_PASSPORT)}

    def test_assembly_is_not_in_the_operational_fleet(self):
        self.assertIn("new", logic.BIKE_STATUSES)
        self.assertNotIn("new", logic.OPERATIONAL_STATUSES,
                         "недособранный велосипед в убыток не пишем")
        self.assertNotIn("new", logic.IDLE_STATUSES)
        self.assertNotIn("new", logic.BIKE_MANUAL_STATUSES,
                         "из сборки выводит кнопка, а не выпадающий список")

    def test_every_passport_field_is_checked_on_its_own(self):
        state = logic.bike_check_state(self.bike())
        self.assertFalse(state["done"])
        self.assertEqual(len(state["rows"]), len(logic.BIKE_PASSPORT))
        done = logic.bike_check_state(self.bike(checked=self.all_checked()))
        self.assertTrue(done["done"])
        self.assertTrue(done["can_commission"])

    def test_empty_field_cannot_be_checked(self):
        state = logic.bike_check_state(
            self.bike(plate_no=None, checked=self.all_checked()))
        self.assertFalse(state["done"])
        self.assertIn("Госномер", state["left"],
                      "отметка на пустоте — это и есть «переписал из накладной»")

    def test_photo_is_demanded_only_when_asked(self):
        marks = self.all_checked()
        loose = logic.bike_check_state(self.bike(checked=marks))
        self.assertTrue(loose["done"])
        strict = logic.bike_check_state(self.bike(checked=marks),
                                        {"bike_photo_required": "1"})
        self.assertFalse(strict["done"])
        self.assertEqual(sorted(strict["left"]),
                         sorted(logic.BIKE_PASSPORT[f]
                                for f in logic.BIKE_PHOTO_FIELDS))
        with_photo = self.all_checked(logic.BIKE_PHOTO_FIELDS, photo="1-frame.jpg")
        with_photo.update(self.all_checked(
            [f for f in logic.BIKE_PASSPORT if f not in logic.BIKE_PHOTO_FIELDS]))
        ok = logic.bike_check_state(self.bike(checked=with_photo),
                                    {"bike_photo_required": "1"})
        self.assertTrue(ok["done"])

    def test_switching_the_rule_off_unlocks_the_button(self):
        state = logic.bike_check_state(self.bike(),
                                       {"bike_check_required": "0"})
        self.assertFalse(state["done"])
        self.assertTrue(state["can_commission"],
                        "список остаётся подсказкой, но кнопка не заперта")

    def test_tracker_counts_as_filled_when_bound(self):
        rows = {r["field"]: r for r in
                logic.bike_checks(self.bike(tracker_ok=False, tracker_id=None))}
        self.assertFalse(rows["tracker"]["filled"])
        bound = {r["field"]: r for r in
                 logic.bike_checks(self.bike(tracker_ok=False, tracker_id=7))}
        self.assertTrue(bound["tracker"]["filled"])

    def test_plate_is_uppercased_and_bounded(self):
        self.assertEqual(logic.check_plate("  аа 12 34 ").value, "АА 12 34")
        self.assertIsNone(logic.check_plate("").value)
        self.assertFalse(logic.check_plate("A" * 30).ok)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestIntakeFlow(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.bike_id = _run(self.crm.create_bike(
            code="B-100", model="Kugoo", frame_no="DEMO-100", status="new"))

    def bike(self):
        return _run(self.crm.bike(self.bike_id))

    def check_all(self):
        _run(self.crm.update_bike(self.bike_id, plate_no="АА 1234",
                                  tracker_ok=True, by="тест"))
        for field in logic.BIKE_PASSPORT:
            _run(service.check_bike_field(self.crm, self.bike(), field,
                                          by="staff:t"))

    def test_unchecked_bike_does_not_go_into_service(self):
        with self.assertRaises(service.ServiceError) as got:
            _run(service.commission_bike(self.crm, self.bike(), by="staff:t"))
        self.assertIn("Не сверено", str(got.exception))
        self.assertEqual(self.bike()["status"], "new")

    def test_checked_bike_goes_into_service_once(self):
        self.check_all()
        _run(service.commission_bike(self.crm, self.bike(), by="staff:t"))
        bike = self.bike()
        self.assertEqual(bike["status"], "available")
        self.assertIsNotNone(bike["commissioned_at"])
        self.assertEqual(bike["commissioned_by"], "staff:t")
        with self.assertRaises(service.ServiceError):
            _run(service.commission_bike(self.crm, self.bike(), by="staff:t"))

    def test_commissioning_is_written_to_the_status_log(self):
        self.check_all()
        _run(service.commission_bike(self.crm, self.bike(), by="staff:t"))
        log = _run(self.crm.bike_status_log(self.bike_id))
        self.assertEqual((log[0]["from_status"], log[0]["to_status"]),
                         ("new", "available"))
        self.assertEqual(log[0]["changed_by"], "staff:t")

    def test_empty_field_is_refused(self):
        with self.assertRaises(service.ServiceError) as got:
            _run(service.check_bike_field(self.crm, self.bike(), "plate_no",
                                          by="staff:t"))
        self.assertIn("поле пустое", str(got.exception))

    def test_unknown_field_is_refused(self):
        with self.assertRaises(service.ServiceError):
            _run(service.check_bike_field(self.crm, self.bike(), "цвет",
                                          by="staff:t"))

    def test_photo_rule_blocks_the_serial_without_one(self):
        _run(self.crm.set_setting("bike_photo_required", "1", by="тест"))
        with self.assertRaises(service.ServiceError) as got:
            _run(service.check_bike_field(self.crm, self.bike(), "frame_no",
                                          by="staff:t"))
        self.assertIn("нужен снимок", str(got.exception))
        _run(service.check_bike_field(self.crm, self.bike(), "frame_no",
                                      by="staff:t", photo="100-frame_no.jpg"))
        rows = {r["field"]: r for r in logic.bike_checks(
            self.bike(), {"bike_photo_required": "1"})}
        self.assertTrue(rows["frame_no"]["ok"])
        # Модель снимка не требует: её видно и так.
        _run(service.check_bike_field(self.crm, self.bike(), "model",
                                      by="staff:t"))

    def test_rule_off_lets_the_bike_out_unchecked(self):
        _run(self.crm.set_setting("bike_check_required", "0", by="тест"))
        _run(service.commission_bike(self.crm, self.bike(), by="staff:t"))
        self.assertEqual(self.bike()["status"], "available")

    def test_check_can_be_taken_back(self):
        _run(service.check_bike_field(self.crm, self.bike(), "model",
                                      by="staff:t"))
        self.assertIn("model", self.bike()["checked"])
        _run(self.crm.clear_bike_check(self.bike_id, "model"))
        self.assertNotIn("model", self.bike()["checked"])


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestIntakePages(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_new_bike_lands_on_assembly(self):
        self.client.post("/bikes", data={"code": "B-77", "model": "Kugoo"})
        bike = _run(self.crm.bike_by_code("B-77"))
        self.assertEqual(bike["status"], "new")
        self.assertIn("B-77", self.get_ok("/intake"))

    def test_rule_off_keeps_the_old_behaviour(self):
        _run(self.crm.set_setting("bike_check_required", "0", by="тест"))
        self.client.post("/bikes", data={"code": "B-78", "model": "Kugoo"})
        self.assertEqual(_run(self.crm.bike_by_code("B-78"))["status"],
                         "available")

    def test_settings_page_saves_the_rules(self):
        r = self.client.post("/intake", data={"photo": "on",
                                              "search_after_days": "10",
                                              "theft_after_days": "25"})
        self.assertEqual(r.status_code, 303)
        settings = _run(self.crm.settings())
        self.assertEqual(settings["bike_check_required"], "0")
        self.assertEqual(settings["bike_photo_required"], "1")
        self.assertEqual(settings["search_after_days"], "10")

    def test_card_shows_the_passport_and_commissions(self):
        self.client.post("/bikes", data={"code": "B-79", "model": "Kugoo",
                                         "frame_no": "F-79"})
        bike = _run(self.crm.bike_by_code("B-79"))
        text = self.get_ok(f"/bikes/{bike['id']}")
        self.assertIn("Паспорт и сверка", text)
        self.assertIn("Ввести в эксплуатацию", text)
        _run(self.crm.update_bike(bike["id"], plate_no="АА 1", tracker_ok=True,
                                  by="тест"))
        for field in logic.BIKE_PASSPORT:
            self.client.post(f"/bikes/{bike['id']}/check",
                             data={"action": "check", "field": field})
        self.client.post(f"/bikes/{bike['id']}/check",
                         data={"action": "commission"})
        self.assertEqual(_run(self.crm.bike(bike["id"]))["status"], "available")

    def test_plate_and_toggles_are_saved_from_the_card(self):
        self.client.post("/bikes", data={"code": "B-80", "model": "Kugoo"})
        bike = _run(self.crm.bike_by_code("B-80"))
        self.client.post(f"/bikes/{bike['id']}/edit",
                         data={"code": "B-80", "model": "Kugoo",
                               "plate_no": "аа 555", "plate_ok": "1",
                               "tracker_ok": "1"})
        fresh = _run(self.crm.bike(bike["id"]))
        self.assertEqual(fresh["plate_no"], "АА 555")
        self.assertTrue(fresh["plate_ok"])
        self.assertTrue(fresh["tracker_ok"])

    def test_status_form_cannot_skip_the_check(self):
        self.client.post("/bikes", data={"code": "B-82", "model": "Kugoo"})
        bike = _run(self.crm.bike_by_code("B-82"))
        self.client.post(f"/bikes/{bike['id']}/status",
                         data={"status": "available"})
        self.assertEqual(_run(self.crm.bike(bike["id"]))["status"], "new",
                         "выпускать в оборот должна только сверка")

    def test_missing_photo_is_a_404_not_a_path_walk(self):
        self.client.post("/bikes", data={"code": "B-81", "model": "Kugoo"})
        bike = _run(self.crm.bike_by_code("B-81"))
        r = self.client.get(f"/bikes/{bike['id']}/photo/frame_no")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
