"""Быстрая выдача: чистая логика мастера и сам мастер через TestClient.

Обвязка панели (FakeCrm, FakeBotDB, FakeBot) - из tests/test_web.py.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
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


class TestIssueLogic(unittest.TestCase):
    def test_tariff_tiles_show_per_day_saving_and_target(self):
        tiles = logic.tariff_tiles([
            {"id": 2, "name": "Неделя", "period_days": 7, "price": D(4290), "active": True},
            {"id": 1, "name": "Сутки", "period_days": 1, "price": D(750), "active": True},
            {"id": 3, "name": "Неделя эконом", "period_days": 7, "price": D(3000)},
            {"id": 4, "name": "Старый", "period_days": 7, "price": D(1), "active": False},
        ])
        self.assertEqual([t["id"] for t in tiles], [1, 3, 2], "от короткого к длинному")
        day, cheap, week = tiles
        self.assertEqual(day["per_day"], D(750))
        self.assertEqual(day["saving"], D(0))
        self.assertTrue(day["hits_target"])
        self.assertEqual(week["per_day"], D("612.86"))
        self.assertEqual(week["saving"], D(960), "7 × 750 − 4 290")
        self.assertTrue(week["hits_target"])
        self.assertEqual(cheap["per_day"], D("428.57"))
        self.assertFalse(cheap["hits_target"], "ниже цели 500 ₽/день")
        self.assertEqual(cheap["saving"], D(2250), "7 × 750 − 3 000")
        # выгода от цен, а не от округлённой цены за день: 3000 × 14 / 7 − 5400
        two_weeks = logic.tariff_tiles([
            {"id": 1, "name": "Неделя", "period_days": 7, "price": D(3000)},
            {"id": 2, "name": "Две недели", "period_days": 14, "price": D(5400)},
        ])[1]
        self.assertEqual(two_weeks["saving"], D(600))
        self.assertEqual(logic.tariff_tiles([]), [])

    def test_model_availability_counts_free_by_model_and_location(self):
        rows = logic.model_availability([
            {"model": "Truck+", "status": "available", "location": "Павлюхина"},
            {"model": "Truck+", "status": "available", "location": None},
            {"model": "Truck+", "status": "rented", "location": "Павлюхина"},
            {"model": "Kugoo", "status": "available", "location": "Адоратского"},
        ])
        self.assertEqual([r["model"] for r in rows], ["Truck+", "Kugoo"])
        self.assertEqual(rows[0]["free"], 2)
        self.assertEqual(rows[0]["by_location"], {"Павлюхина": 1, "не на точке": 1})
        self.assertEqual(rows[1]["by_location"], {"Адоратского": 1})

    def test_idle_days(self):
        now = datetime(2026, 9, 16, 12, tzinfo=UTC)
        self.assertEqual(logic.idle_days(now - timedelta(days=5, hours=3), now=now), 5)
        self.assertEqual(logic.idle_days(now + timedelta(hours=1), now=now), 0)
        self.assertIsNone(logic.idle_days(None, now=now))

    def test_bot_client_state(self):
        self.assertEqual(logic.bot_client_state(None)["code"], "none")
        self.assertEqual(logic.bot_client_state({"status": "new", "state": "wait_doc"})["code"],
                         "registering")
        self.assertEqual(logic.bot_client_state({"status": "pending", "state": "pending"})["code"],
                         "pending")
        self.assertEqual(logic.bot_client_state({"status": "approved",
                                                 "contract_status": "issued"})["code"],
                         "approved")
        signed = {"status": "approved", "contract_status": "signed",
                  "contract_no": "АВ-2026-000007"}
        state = logic.bot_client_state(signed)
        self.assertEqual(state["code"], "signed")
        self.assertIn("АВ-2026-000007", state["label"])
        renting = logic.bot_client_state({**signed, "act_in_signed_at": "x"})
        self.assertEqual(renting["code"], "renting")
        self.assertEqual(logic.bot_client_state({**signed, "act_in_signed_at": "x",
                                                 "act_out_signed_at": "y"})["code"], "signed")

    def test_bike_matches(self):
        bike = {"code": "МБ-12", "motor_no": "60V 240W-2024A123", "frame_no": "FR-0099"}
        for query in ("мб-12", "", "2024а123", "60в240w", "fr0099"):
            self.assertTrue(logic.bike_matches(bike, query), query)
        for query in ("МБ-13", "A777", "9"):
            self.assertFalse(logic.bike_matches(bike, query), query)

    def test_issue_payment_default(self):
        self.assertEqual(logic.issue_payment_default(D(3000), D(0)), D(3000))
        self.assertEqual(logic.issue_payment_default(D(3000), D(500)), D(2500))
        self.assertEqual(logic.issue_payment_default(D(3000), D(-700)), D(3000),
                         "долг не прибавляется: он виден отдельно")
        self.assertEqual(logic.issue_payment_default(D(3000), D(5000)), D(0))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestIssueWizard(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def run_(self, coro):
        return tw.run(coro)

    def issue(self, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return self.get_ok("/issue" + (f"?{query}" if query else ""))

    # ─── шаг 1 ───

    def test_phone_lookup_redirects_to_client_step(self):
        r = self.client.get("/issue?phone=8 (999) 000-00-00")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], f"/issue?client={self.client_id}")
        page = self.get_ok(r.headers["location"])
        self.assertIn("Иванов Иван", page)
        self.assertIn("Неделя", page)
        self.assertIn("в день", page)
        self.assertIn("ниже цели", page, "3 000 / 7 дн. меньше 500 ₽ в день")
        self.assertIn("Kugoo V3", page)
        self.assertIn("свободно 1", page)

    def test_bad_phone_is_explained(self):
        self.assertIn("не похоже на номер", self.issue(phone="abc"))

    def test_unknown_phone_offers_new_client_with_data_from_bot(self):
        self.db.users[7001] = {"tg_id": 7001, "phone": "+79995550000", "username": "sid",
                               "full_name": "Сидоров Сидор", "status": "new",
                               "state": "wait_doc", "contract_no": None}
        page = self.issue(phone="+79995550000")
        self.assertIn("Новый клиент", page)
        self.assertIn('value="Сидоров Сидор"', page)
        self.assertIn("регистрация в боте не завершена", page)

        r = self.client.post("/issue/client", data={"phone": "+79995550000",
                                                    "full_name": "Сидоров Сидор"})
        self.assertEqual(r.status_code, 303)
        created = self.run_(self.crm.client_by_phone("+79995550000"))
        self.assertIsNotNone(created)
        self.assertEqual(created["tg_id"], 7001, "Telegram подхвачен из бота")
        self.assertEqual(r.headers["location"], f"/issue?client={created['id']}")
        self.assertIn("Telegram подхвачен", self.get_ok(r.headers["location"]))

    def test_new_client_without_name_is_refused(self):
        r = self.client.post("/issue/client", data={"phone": "+79995550000", "full_name": ""})
        self.assertEqual(r.status_code, 303)
        self.assertIn("phone=%2B79995550000", r.headers["location"])
        self.assertIsNone(self.run_(self.crm.client_by_phone("+79995550000")))

    def test_client_with_active_rental_cannot_be_issued_again(self):
        self.run_(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="auto",
            started_on=date.today(), contract_no=None, created_by="t"))
        page = self.issue(client=self.client_id)
        self.assertIn("уже идёт аренда", page)
        self.assertNotIn("Далее", page)

    def test_invite_link_for_client_without_telegram(self):
        other = self.run_(self.crm.create_client(full_name="Петров Пётр",
                                                 phone="+79991110000"))
        page = self.issue(client=other)
        self.assertIn("t.me/mybike_test_bot?start=issue", page)
        self.assertIn("не в боте", page)

    def test_bot_rental_in_progress_is_flagged(self):
        self.db.users[5001] = {"tg_id": 5001, "phone": "+79990000000", "status": "approved",
                               "state": "approved", "contract_status": "signed",
                               "contract_no": "АВ-2026-000001", "act_in_signed_at": "x",
                               "act_out_signed_at": None}
        page = self.issue(client=self.client_id)
        self.assertIn("велосипед на руках", page)
        self.assertIn("АВ-2026-000001", page)

    # ─── шаги 3 и 4 ───

    def test_bike_step_sorts_by_idle_and_summary_has_defaults(self):
        second = self.run_(self.crm.create_bike(code="B-2", model="Kugoo V3",
                                                location="Адоратского"))
        # B-2 простаивает дольше: его запись в журнале старше
        for row in self.crm.status_log_:
            if row["bike_id"] == second:
                row["changed_at"] -= timedelta(days=6)
        page = self.issue(client=self.client_id, tariff=self.tariff_id, model="Kugoo V3")
        self.assertIn("№ B-1", page)
        self.assertIn("№ B-2", page)
        self.assertLess(page.index("№ B-2"), page.index("№ B-1"),
                        "дольше простаивающий - первым")
        self.assertIn("простаивает 6 дн.", page)
        self.assertIn("Адоратского", page)
        self.assertNotIn("№ B-1", self.issue(client=self.client_id, tariff=self.tariff_id,
                                            model="Kugoo V3", q="B-2"))

        page = self.issue(client=self.client_id, tariff=self.tariff_id, bike=second)
        self.assertIn("Оформить выдачу", page)
        self.assertIn('value="3000"', page, "принять при выдаче - цена периода")
        until = (date.today() + timedelta(days=7)).strftime("%d.%m.%Y")
        self.assertIn(until, page)

    def test_find_bike_by_motor_vin(self):
        """VIN мотора с техники - сразу к велосипеду, как набрали с телефона:
        кириллица, пробелы, «В» вместо «V»."""
        self.run_(self.crm.update_bike(self.bike_id, motor_no="60V240W2024A123"))
        second = self.run_(self.crm.create_bike(code="B-2", model="Kugoo V3",
                                                motor_no="60V240W2024A777"))
        page = self.issue(client=self.client_id, find="2024%D0%90123")   # кириллица А
        self.assertIn("Выбран велосипед <b>№ B-1</b>", page)
        page = self.issue(client=self.client_id, find="60%D0%92240W")    # «60В240W»
        self.assertIn("Подходят несколько свободных", page)
        self.assertIn(f"bike={second}", page)
        self.assertIn("мотор 60V240W2024A777", page)
        # на шаге велосипеда тот же поиск сужает список
        page = self.issue(client=self.client_id, tariff=self.tariff_id, model="Kugoo V3",
                          q="A777")
        self.assertIn("№ B-2", page)
        self.assertNotIn("№ B-1", page)
        # занятый - объяснение, а не пустота
        self.run_(self.crm.update_bike(second, status="repair"))
        page = self.issue(client=self.client_id, find="A777")
        self.assertIn("B-2 сейчас «В ремонте»", page)

    def test_wizard_from_bike_card_skips_bike_step(self):
        page = self.issue(bike=self.bike_id)
        self.assertIn("уже выбран", page)
        r = self.client.get(f"/issue?phone=+79990000000&bike={self.bike_id}")
        self.assertEqual(r.headers["location"],
                         f"/issue?client={self.client_id}&bike={self.bike_id}")
        page = self.get_ok(r.headers["location"])
        self.assertIn("Выбран велосипед", page)
        page = self.issue(client=self.client_id, bike=self.bike_id, tariff=self.tariff_id)
        self.assertIn("Оформить выдачу", page)

    def test_busy_bike_from_card_is_dropped(self):
        self.run_(self.crm.update_bike(self.bike_id, status="repair"))
        page = self.issue(client=self.client_id, bike=self.bike_id)
        self.assertIn("В ремонте", page)
        self.assertNotIn("Выбран велосипед", page)

    # ─── оформление ───

    def test_issue_opens_rental_records_payment_and_notifies(self):
        self.db.users[5001] = {"tg_id": 5001, "phone": "+79990000000", "status": "approved",
                               "state": "approved", "contract_status": "signed",
                               "contract_no": "АВ-2026-000001", "lang": "ru"}
        r = self.client.post("/issue", data={"client_id": self.client_id,
                                             "tariff_id": self.tariff_id,
                                             "bike_id": self.bike_id,
                                             "started_on": date.today().isoformat(),
                                             "pay_amount": "3000", "pay_method": "cash",
                                             "mileage": "1200", "contract_no": ""})
        self.assertEqual(r.status_code, 303)
        rental = self.run_(self.crm.active_rental_of(self.client_id))
        self.assertIsNotNone(rental)
        # Мастер не бросает оператора в карточку: пятый шаг - документы.
        self.assertEqual(r.headers["location"], f"/issue/docs?rental={rental['id']}")
        self.assertEqual(rental["contract_no"], "АВ-2026-000001", "номер договора из бота")
        self.assertEqual(rental["mileage_start"], 1200)
        self.assertEqual(self.run_(self.crm.bike(self.bike_id))["status"], "rented")
        self.assertEqual(self.run_(self.crm.client_balance(self.client_id)), D(0),
                         "начислен первый период и принята оплата")
        payment = [x for x in self.crm.ledger_ if x["kind"] == "payment"][-1]
        self.assertEqual(payment["method"], "cash")
        self.assertEqual(payment["rental_id"], rental["id"])
        self.assertIn("B-1", payment["note"])
        self.assertIn("Аренда оформлена", self.bot.sent[-1][1])
        docs = self.get_ok(r.headers["location"])
        self.assertIn("Документы", docs)
        self.assertIn("Выдача оформлена", docs, "флеш доезжает до шага документов")
        page = self.get_ok(f"/rentals/{rental['id']}")
        self.assertIn("3 000", page)

    def test_issue_without_payment_leaves_debt(self):
        r = self.client.post("/issue", data={"client_id": self.client_id,
                                             "tariff_id": self.tariff_id,
                                             "bike_id": self.bike_id,
                                             "pay_amount": "0", "pay_method": "cash",
                                             "mileage": "0"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.run_(self.crm.client_balance(self.client_id)), D(-3000))
        self.assertIn("без оплаты", self.get_ok(r.headers["location"]))

    def test_issue_refuses_bad_amount_and_busy_bike(self):
        r = self.client.post("/issue", data={"client_id": self.client_id,
                                             "tariff_id": self.tariff_id,
                                             "bike_id": self.bike_id,
                                             "pay_amount": "много", "pay_method": "cash"})
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("/issue?client="))
        self.assertIn("Сумма", self.get_ok(r.headers["location"]))
        self.assertIsNone(self.run_(self.crm.active_rental_of(self.client_id)))

        other = self.run_(self.crm.create_client(full_name="Петров Пётр",
                                                 phone="+79991110000"))
        self.run_(self.crm.create_rental(
            client_id=other, bike_id=self.bike_id, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="auto",
            started_on=date.today(), contract_no=None, created_by="t"))
        r = self.client.post("/issue", data={"client_id": self.client_id,
                                             "tariff_id": self.tariff_id,
                                             "bike_id": self.bike_id, "mileage": "0",
                                             "pay_amount": "3000", "pay_method": "cash"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("В аренде", self.get_ok(r.headers["location"]))
        self.assertIsNone(self.run_(self.crm.active_rental_of(self.client_id)))

    def test_issue_needs_all_three_parts(self):
        r = self.client.post("/issue", data={"client_id": self.client_id, "tariff_id": "",
                                             "bike_id": ""})
        self.assertEqual(r.status_code, 303)
        self.assertIn("Выберите клиента, тариф и велосипед", self.get_ok(r.headers["location"]))

    # ─── сумма к оплате с доп. аккумулятором ───

    def battery_step(self):
        model = self.run_(self.crm.create_battery_model(
            title="Аккумулятор 70 Ач", brand=None, voltage=60, capacity=D(70),
            price=D(12000), service_months=15, note=None))
        self.run_(self.crm.create_battery(code="951", model_id=model, status="available"))
        self.run_(self.crm.create_tariff("АКБ · неделя", 7, D(1170), None, kind="battery"))
        # Плюс на балансе вычитается из суммы к оплате - и на сервере, и в скрипте.
        self.run_(self.crm.add_ledger(client_id=self.client_id, kind="payment",
                                      amount=D(500), method="cash"))
        return self.issue(client=self.client_id, tariff=self.tariff_id, bike=self.bike_id)

    def test_pay_amount_knows_what_the_extra_battery_adds_to(self):
        """Первый период начисляется целиком, с доп. аккумулятором. Сумма
        «принять сейчас» считалась от одного велосипеда, и клиент уходил с
        долгом на цену батареи. Скрипт шага пересчитывает её по тем же
        слагаемым, что сервер."""
        page = self.battery_step()
        self.assertIn('value="2500"', page, "без батареи: 3 000 минус 500 на балансе")
        self.assertIn('id="pay-amount"', page)
        self.assertIn('data-credit="500.00"', page)
        self.assertIn('data-off="0"', page)
        self.assertIn('data-price="1170', page)
        self.assertIn('id="first-charge"', page)

    @unittest.skipUnless(shutil.which("node"), "node не установлен")
    def test_page_script_moves_the_pay_amount_with_the_extra(self):
        page = self.battery_step()
        script = next(s for s in re.findall(r"<script>(.*?)</script>", page, re.S)
                      if "period-total" in s)
        harness = """
const listeners = [];
const box = {dataset: {price: '1170.00'}, checked: false,
             addEventListener: (e, f) => listeners.push(f)};
const els = {
  'period-total': {dataset: {base: '3000.00'}, textContent: ''},
  'first-charge': {textContent: ''},
  'pay-amount': {dataset: {credit: '500.00', off: '0'}, value: '2500'},
};
const due = [{textContent: ''}];
global.document = {
  getElementById: (id) => els[id] || null,
  querySelectorAll: (sel) => sel === '.extra-battery' ? [box] : due,
};
""" + script + """
const out = [els['pay-amount'].value];
box.checked = true; listeners.forEach((f) => f());
out.push(els['pay-amount'].value, els['first-charge'].textContent, due[0].textContent);
els['pay-amount'].value = '1000';
box.checked = false; listeners.forEach((f) => f());
out.push(els['pay-amount'].value);
console.log(JSON.stringify(out));
"""
        got = subprocess.run(["node", "-e", harness], capture_output=True, text=True,
                             timeout=30, check=True)
        start, checked, first, due_text, edited = json.loads(got.stdout)
        self.assertEqual((start, checked), ("2500", "3670"),
                         "с батареей: 3 000 + 1 170 − 500")
        self.assertIn("4", first.replace(" ", " "), "первый период - с батареей")
        self.assertIn("670", due_text.replace(" ", " "))
        self.assertEqual(edited, "1000", "сумму, поправленную руками, скрипт не трогает")


if __name__ == "__main__":
    unittest.main()
