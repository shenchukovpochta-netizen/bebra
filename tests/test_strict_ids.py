"""Номер из адреса и формы - только ASCII-цифры (logic.parse_id).

str.isdigit верит «²» и «٢»: int() на первой ронял страницу 500, вторую
молча читал как 2 - то есть чужую запись, номера которой никто не набирал.
Каждый маршрут, где номер разбирался isdigit, проверяется одинаково:
юникодные «цифры» и номер длиннее bigint отвечают ровно как обычный
мусор «abc» - тот же код, тот же адрес редиректа, то же сообщение.
База - tests/fake_crm.py, обвязка - из tests/test_web.py.
"""

from __future__ import annotations

import re
import sys
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import logic as bot_logic  # noqa: E402
from app.crm import logic  # noqa: E402

try:
    import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

run = tw.run
D = Decimal
FLASH = re.compile(r'<div class="flash (\w+)">(.*?)</div>', re.S)


def arabic(number: int) -> str:
    """Тот же номер арабско-индийскими цифрами: int() читает его молча."""
    return str(number).translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))


class TestStrictDigitsInForms(unittest.TestCase):
    """Проверки форм, которые тоже верили isdigit."""

    def test_mileage(self):
        for junk in ("²", "٢", "１２"):
            self.assertFalse(logic.check_mileage(junk).ok, junk)
        self.assertEqual(logic.check_mileage("4 266").value, 4266)

    def test_promo_numbers(self):
        base = {"kind": "promocode", "title": "Акция", "code": "ОСЕНЬ", "percent": "10"}
        self.assertTrue(logic.check_promo_form(base).ok)
        for field in ("percent", "max_uses"):
            for junk in ("²", "٢"):
                self.assertFalse(logic.check_promo_form({**base, field: junk}).ok,
                                 (field, junk))
        comeback = {"kind": "comeback", "title": "Возврат", "percent": "10"}
        self.assertTrue(logic.check_promo_form({**comeback, "after_days": "30"}).ok)
        self.assertFalse(logic.check_promo_form({**comeback, "after_days": "²"}).ok)

    def test_kit_in_the_issue_form(self):
        """В акт уходило «АКБ: ²» - число, которого на складе не бывает."""
        data, error = bot_logic.parse_issue_form("акб: ²")
        self.assertIsNone(data)
        self.assertIn("нужно число", error)
        _, error = bot_logic.parse_issue_form("акб: 3")
        self.assertNotIn("нужно число", error)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestStrictIdsInThePanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        crm = self.crm
        self.spare = run(crm.create_bike(code="B-2", model="Kugoo V3"))
        self.rental = run(crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="auto",
            started_on=date.today(), contract_no=None, created_by="t"))
        self.order = run(crm.create_work_order(
            bike_id=None, payer="client", client_id=self.client_id, complaint="скрипит",
            object_note="самокат", tech_id=None, estimate=None, created_by="t"))
        self.tracker = run(crm.create_tracker(device_id="1001"))
        self.txn = run(crm.save_bank_txn({"txn_id": "t-1", "booked_at": datetime.now(UTC),
                                          "amount": "500", "direction": "in"}))
        self.part_order = run(crm.create_part_order(supplier_id=None, note=None,
                                                    created_by="t"))
        profile = run(crm.access_profile_by_code("manager"))["id"]
        self.staff = run(crm.create_staff("u2", logic.hash_password("password-1"), "U2",
                                          "manager", profile))

    def outcome(self, method: str, path: str, data: dict, junk: str,
                *, flash: bool = True) -> tuple:
        """Код, адрес редиректа и сообщения - без самого мусора и номеров:
        адрес «назад» повторяет значение поля, а новые записи - свои id."""
        if method == "GET":
            r = self.client.get(path, params=data)
        else:
            r = self.client.post(path, data=data)
        self.assertNotEqual(r.status_code, 500, (path, junk))
        where = unquote(r.headers.get("location") or "").replace(junk, "J")
        said = ()
        if flash and r.status_code == 303:
            said = tuple(re.sub(r"\d+", "#", text.replace(junk, "J"))
                         for _, text in FLASH.findall(self.client.get("/me").text))
        return r.status_code, re.sub(r"\d+", "#", where), said

    def same_as_junk(self, method: str, path: str, data: dict, field: str,
                     real: int | None = None, *, flash: bool = True,
                     long: bool = True) -> None:
        """«²», «٢», номер настоящей записи арабскими цифрами и номер
        длиннее bigint (long - если поле номер, а не просто число) - как «abc»."""
        want = self.outcome(method, path, {**data, field: "abc"}, "abc", flash=flash)
        junks = ["²", "٢", *(["9" * 30] if long else []),
                 *([arabic(real)] if real is not None else [])]
        for junk in junks:
            with self.subTest(path=path, field=field, junk=junk):
                got = self.outcome(method, path, {**data, field: junk}, junk, flash=flash)
                self.assertEqual(got, want)

    def test_pages(self):
        free = run(self.crm.create_client(full_name="Петров Пётр", phone="+79990000001"))
        self.same_as_junk("GET", "/issue", {}, "bike", self.spare)
        self.same_as_junk("GET", "/issue", {}, "client", self.client_id)
        # Тариф разбирается у клиента без аренды: у занятого мастер стоит.
        self.same_as_junk("GET", "/issue", {"client": free}, "tariff", self.tariff_id)
        self.same_as_junk("GET", "/rentals/new", {}, "client", self.client_id)
        self.same_as_junk("GET", "/rentals/new", {}, "bike", self.spare)
        self.same_as_junk("GET", "/orders", {}, "bike", self.bike_id)
        self.same_as_junk("GET", "/orders.csv", {}, "bike", self.bike_id)
        self.same_as_junk("GET", "/orders/new", {}, "bike", self.bike_id)
        for junk in ("²", "٢", arabic(self.rental), "9" * 30):
            r = self.client.get("/issue/docs", params={"rental": junk})
            self.assertEqual(r.status_code, 404, junk)

    def test_clients_and_fleet(self):
        cid, rid = self.client_id, self.rental
        self.same_as_junk("POST", f"/clients/{cid}/edit",
                          {"full_name": "Иванов Иван", "phone": "+79990000000"}, "max_id")
        self.same_as_junk("POST", f"/clients/{cid}/ledger",
                          {"kind": "fine", "amount": "100"}, "preset")
        bike = {"code": "Z-1", "model": "Kugoo V3"}
        for field in ("battery_count", "service_months", "battery_service_months"):
            self.same_as_junk("POST", "/bikes", bike, field)
        self.same_as_junk("POST", "/batteries", {"code": "A-1"}, "service_months")
        # Модель - на правке: создание второй раз упёрлось бы в тот же номер.
        battery = run(self.crm.create_battery(code="A-2", status="available"))
        self.same_as_junk("POST", f"/batteries/{battery}/edit",
                          {"code": "A-2", "service_months": "15"}, "model_id")
        self.same_as_junk("POST", "/models/compat", {"battery_model_id": "1"},
                          "bike_model_id")
        for field in ("service_months", "battery_service_months", "battery_count"):
            self.same_as_junk("POST", "/assets", {"codes": "Z-10", "model": "Kugoo V3"},
                              field)
        # Пробег - число, а не номер: длинное у него своя ошибка.
        self.same_as_junk("POST", f"/rentals/{rid}/close", {}, "mileage", long=False)

    def test_issue_and_rentals(self):
        rid = self.rental
        self.same_as_junk("POST", "/issue", {"tariff_id": self.tariff_id,
                                             "bike_id": self.spare}, "client_id",
                          self.client_id)
        self.same_as_junk("POST", "/rentals", {"tariff_id": self.tariff_id}, "client_id",
                          self.client_id)
        self.same_as_junk("POST", f"/rentals/{rid}/extras", {}, "battery_id")
        self.same_as_junk("POST", f"/rentals/{rid}/swap", {"reason": "repair"}, "bike_id",
                          self.spare)
        self.same_as_junk("POST", f"/rentals/{rid}/tariff", {}, "tariff_id",
                          self.tariff_id)
        self.same_as_junk("POST", f"/rentals/{rid}/battery", {}, "battery_id")
        self.assertEqual(run(self.crm.rental(rid))["bike_id"], self.bike_id,
                         "«٢» не увёл аренду на чужой велосипед")

    def test_staff_orders_and_stock(self):
        oid = self.order
        self.same_as_junk("POST", "/staff", {"login": "newbie", "password": "password-123",
                                             "name": "Новый"}, "profile_id")
        self.same_as_junk("POST", f"/staff/{self.staff}/profile", {}, "profile_id")
        self.same_as_junk("POST", "/orders", {"payer": "own"}, "bike_id", self.bike_id)
        self.same_as_junk("POST", "/orders", {"payer": "client", "complaint": "стук",
                                              "object_note": "самокат"}, "client_id",
                          self.client_id)
        self.same_as_junk("POST", "/orders", {"payer": "client", "complaint": "стук",
                                              "object_note": "самокат",
                                              "client_id": self.client_id}, "tech_id")
        self.same_as_junk("POST", f"/orders/{oid}/items", {"title": "Работа", "qty": "1",
                                                           "price": "100"}, "work_type_id")
        self.same_as_junk("POST", f"/orders/{oid}/parts", {"qty": "1"}, "part_id")
        self.same_as_junk("POST", f"/orders/{oid}/edit", {"status": "new"}, "tech_id")
        self.same_as_junk("POST", "/parts/receipts", {"qty_0": "1"}, "part_id_0")
        self.same_as_junk("POST", "/parts/receipts", {"part_id_0": "1"}, "qty_0")
        self.same_as_junk("POST", "/parts/receipts", {}, "supplier_id")
        self.same_as_junk("POST", "/part-orders/items", {"qty": "1"}, "part_id")
        self.same_as_junk("POST", f"/part-orders/{self.part_order}/status",
                          {"status": "cancelled"}, "supplier_id")

    def test_money_mailing_documents_trackers(self):
        self.same_as_junk("POST", f"/bank/{self.txn}", {"action": "credit"}, "client_id",
                          self.client_id)
        self.same_as_junk("POST", "/payments", {"amount": "100"}, "client_id",
                          self.client_id)
        self.same_as_junk("POST", "/mailing/templates", {"title": "Шаблон",
                                                         "body": "Текст"}, "id")
        self.same_as_junk("POST", "/mailing", {"title": "Рассылка", "audience": "all"},
                          "template_id")
        self.same_as_junk("POST", "/documents/contract", {"action": "enable"},
                          "template_id")
        self.same_as_junk("POST", "/promos", {"kind": "first", "title": "Акция"},
                          "percent")
        self.same_as_junk("POST", f"/trackers/{self.tracker}/bike", {}, "bike_id",
                          self.spare)
        # Команда встаёт в очередь один раз: сообщения у повторов другие.
        self.same_as_junk("POST", f"/trackers/{self.tracker}/command",
                          {"command": "block"}, "alert_id", flash=False)
        self.assertIsNone(run(self.crm.tracker(self.tracker))["bike_id"],
                          "«٢» не привязал трекер к чужому велосипеду")

    def test_ids_in_the_path(self):
        """/{id} FastAPI читает в int: «²» и «abc» давали JSON 422, номер
        длиннее bigint - 500 в базе. Всё это адрес без записи - 404 страницей."""
        huge = "9" * 19
        for path in ("/clients/%C2%B2", f"/clients/{quote('٢')}", "/clients/abc",
                     f"/clients/{huge}", f"/trackers/{huge}", f"/rentals/{huge}/battery"):
            r = (self.client.post(path, data={}) if path.endswith("/battery")
                 else self.client.get(path))
            self.assertEqual(r.status_code, 404, path)
            self.assertIn("Адрес не найден", r.text, path)
        self.assertTrue(logic.path_ids_ok(f"/clients/{'9' * 18}"), "bigint - ещё номер")
        self.assertFalse(logic.path_ids_ok(f"/clients/{huge}/edit"))
        self.assertTrue(logic.path_ids_ok("/sign/Ab-9/doc/0"))
        # Всё, что pydantic читает в int, а не только голые цифры.
        for spelling in (f"+{huge}", f"-{huge}", f" {huge}", f"{huge}\t", f"{huge}.0",
                         f" {huge}", "1_000_000_000_000_000_000", f"0-{huge}"):
            self.assertFalse(logic.path_ids_ok(f"/clients/{spelling}"), repr(spelling))
        for fine in ("-5", " 5", "5.0", "1_000", "0" * 30 + "5", "Ab-9", "photo.jpg"):
            self.assertTrue(logic.path_ids_ok(f"/clients/{fine}"), repr(fine))
        # Клиент на странице подписи видит свою страницу, а не панель.
        self.client.post("/logout")
        r = self.client.get("/sign/token/doc/%C2%B2")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Ссылка не найдена", r.text)
        r = self.client.get(f"/clients/{huge}")
        self.assertEqual(r.status_code, 303, "без входа - сначала вход, а не 404")

    def test_path_guard_knows_every_char_pydantic_reads(self):
        """Страж по составу сегмента верен, пока pydantic читает в int только
        цифры, пробелы, знаки, «_» и «.». Начнёт читать что-то ещё - номер
        за пределом bigint с этим символом снова дойдёт до базы 500."""
        from pydantic import TypeAdapter
        as_int = TypeAdapter(int)
        for code in range(0x3100):              # все пробелы Юникода - до U+3000
            char = chr(code)
            if 0xD800 <= code <= 0xDFFF:
                continue
            for text in (char + "1", "1" + char, "1" + char + "1"):
                try:
                    as_int.validate_python(text)
                except ValueError:
                    continue
                self.assertTrue(logic._PATH_NUMBER.fullmatch(char), hex(code))

    def test_checked_ids_skip_unicode_digits(self):
        """Галочки списком (form_ids): «²» ронял выдачу 500 уже после всех
        проверок, а номер арабскими цифрами int() читал молча - и выдача
        забирала батарею, которую никто не отмечал."""
        other = run(self.crm.create_client(full_name="Петров Пётр", phone="+79990000001"))
        battery = run(self.crm.create_battery(code="A-7", status="available"))
        r = self.client.post("/issue", data={
            "client_id": other, "tariff_id": self.tariff_id, "bike_id": self.spare,
            "mileage": "0", "battery_ids": ["²", arabic(battery)],
            "extra_battery_ids": ["²", arabic(battery)]})
        self.assertEqual(r.status_code, 303)
        rental = run(self.crm.active_rental_of(other))
        self.assertIsNotNone(rental, "выдача прошла, мусор в галочках выпал")
        self.assertEqual(run(self.crm.battery(battery))["status"], "available")


if __name__ == "__main__":
    unittest.main()
