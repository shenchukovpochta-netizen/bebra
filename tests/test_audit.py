"""Сверка кода: то, что нашли агенты, и что теперь не вернётся.

Каждый тест здесь - ровно один дефект из разбора. Они собраны в одном
файле не по разделу системы, а по происхождению: так видно, что именно
было сломано, и почему проверка выглядит именно так. Разбиение по
разделам вернёт эти ошибки в общую кучу, где их и не замечали.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import keyboards as kb  # noqa: E402
from app.crm import banking, logic  # noqa: E402
from app.services import tochka  # noqa: E402

try:
    import test_web as tw

    from app.crm import paying, service, sync
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class TestLocalDates(unittest.TestCase):
    """Даты по Москве, а не по UTC.

    asyncpg отдаёт timestamptz в UTC, а `today` приходит местный. Ночью
    по Москве `.date()` возвращал вчерашний день, и всё, что считается
    «сколько суток прошло», старело на сутки.

    Пояс в тесте выставляется явно: контейнеры живут в Europe/Moscow, а
    машина, на которой гоняют тесты, - где угодно, и в UTC ошибка не
    воспроизводится вовсе.
    """

    NIGHT = datetime(2026, 9, 19, 22, 0, tzinfo=UTC)     # 20.09 01:00 МСК
    TODAY = date(2026, 9, 20)

    def setUp(self):
        self.tz_before = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Moscow"
        time.tzset()

    def tearDown(self):
        if self.tz_before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.tz_before
        time.tzset()

    def test_local_date_takes_the_local_day(self):
        self.assertEqual(logic.local_date(self.NIGHT), self.TODAY)
        self.assertEqual(self.NIGHT.date(), date(2026, 9, 19),
                         "по UTC это ещё вчера - ровно та ошибка, что чинили")
        self.assertIsNone(logic.local_date(None))
        self.assertEqual(logic.local_date(date(2026, 1, 2)), date(2026, 1, 2))
        self.assertEqual(logic.local_date(datetime(2026, 9, 20, 1, 0)),
                         self.TODAY, "наивное время уже местное")

    def test_search_days_does_not_age_the_rental_overnight(self):
        self.assertEqual(
            logic.search_days({"search_at": self.NIGHT}, today=self.TODAY), 0,
            "розыск начался час назад - это ноль суток, а не одни")

    def test_order_days_does_not_age_the_order_overnight(self):
        self.assertEqual(
            logic.order_days({"opened_at": self.NIGHT, "status": "work"},
                             today=self.TODAY), 0)
        self.assertFalse(logic.order_stuck(
            {"opened_at": self.NIGHT, "status": "in_work"}, today=self.TODAY))

    def test_channel_rows_put_the_client_in_his_own_month(self):
        october = datetime(2026, 9, 30, 22, 30, tzinfo=UTC)   # 01.10 01:30 МСК
        table = logic.channel_rows([{"created_at": october, "channel": "avito"}],
                                   today=date(2026, 10, 5), months=3)
        row = next(r for r in table["rows"] if r["month"] == date(2026, 10, 1))
        self.assertEqual(row["total"], 1,
                         "клиент заведён первого октября, а не тридцатого сентября")


class TestPlanRounding(unittest.TestCase):
    def test_need_rented_rounds_up(self):
        """Шесть велосипедов по целевому чеку плана не дают - значит семь.

        У Decimal `//` усекает к нулю, и трюк `-(-a // b)` превращался
        там в обычный floor: 6,67 печаталось как 6.
        """
        progress = logic.plan_progress(
            {"rented": 100, "check": D(500)}, {"revenue": D(1490000)},
            days_in_month=30, days_passed=27)
        self.assertEqual(progress["left"], D("10000.00"))
        self.assertEqual(progress["days_left"], 3)
        self.assertEqual(progress["need_rented"], 7)
        need = progress["need_rented"] * progress["days_left"] * D(500)
        self.assertGreaterEqual(need, progress["left"])


class TestOrderQty(unittest.TestCase):
    def test_zero_is_zero_and_empty_is_one(self):
        """`qty` в базе not null default 1, поэтому `or 1` срабатывал
        ровно на нуле: в таблице строка печаталась как 0 ₽, а в «Итого»
        уходила как одна штука."""
        self.assertEqual(logic.item_qty({"qty": 0}), 0)
        self.assertEqual(logic.item_qty({"qty": None}), 1)
        self.assertEqual(logic.item_qty({}), 1)
        self.assertEqual(logic.item_total({"price": D(500), "qty": 0}), D(0))
        self.assertEqual(logic.item_cost({"parts_cost": D(300), "qty": 0}), D(0))
        self.assertEqual(logic.order_totals_client([{"price": D(500), "qty": 0}]),
                         D(0))
        self.assertNotIn("500", logic.estimate_lines(
            [{"title": "Камера", "price": D(500), "qty": 0}]))


class TestSubscribeKeyboard(unittest.TestCase):
    def test_broken_channel_url_does_not_kill_the_message(self):
        """Telegram отвергает сообщение целиком из-за кнопки с битым url.
        CHANNEL_URL без схемы оставлял всех неподписанных без ответа."""
        good = kb.subscribe("https://t.me/mybike", "ru")
        self.assertEqual(len(good.inline_keyboard), 2)
        self.assertEqual(good.inline_keyboard[0][0].url, "https://t.me/mybike")
        bad = kb.subscribe("t.me/mybike", "ru")
        self.assertEqual(len(bad.inline_keyboard), 1)
        self.assertEqual(bad.inline_keyboard[0][0].callback_data, "check_sub")


class TestSeveralAccounts(unittest.TestCase):
    """Деньги за аренду приходят на любой из десятка счетов: выписка - по
    каждому, номер заказа - свой у каждого, отказ одного не слепит все."""

    def test_accounts_are_parsed_from_one_setting(self):
        self.assertEqual(tochka.parse_accounts(
            "4080/1, 4080/2;4080/3  4080/1\n4080/4"),
            ["4080/1", "4080/2", "4080/3", "4080/4"])
        self.assertEqual(tochka.parse_accounts(""), [])
        self.assertTrue(tochka.TochkaClient(token="t", account_id="A, B").ready)
        self.assertFalse(tochka.TochkaClient(token="t", account_id=" , ").ready)

    def test_each_account_has_its_own_statement(self):
        calls: list = []

        class Response:
            def __init__(self, data):
                self._data, self.status = data, 200

            async def json(self, content_type=None):
                return self._data

        class Session:
            async def request(self, method, url, **kwargs):
                if method == "POST":
                    account = kwargs["json"]["Data"]["Statement"]["accountId"]
                    calls.append(("POST", account))
                    return Response({"Data": {"Statement": {
                        "statementId": f"S-{account}"}}})
                account, number = url.rsplit("/", 2)[-2:]
                calls.append(("GET", account, number))
                if account == "B":
                    return Response({"Data": {"Statement": {"status": "Processing"}}})
                if account == "C":
                    return Response({"Data": {"Statement": {"status": "Ready",
                        "Transaction": [{"transactionId": "T-C1", "amount": 3000,
                                         "creditDebitIndicator": "Credit",
                                         "documentDate": "2026-09-16",
                                         "paymentPurpose": "аренда"}]}}})
                return Response({"Data": {"Statement": {"status": "Ready",
                                                        "Transaction": []}}})

            async def close(self):
                pass

        saved: list = []

        class Crm:
            async def save_bank_txn(self, row):
                saved.append(row)
                return len(saved)

            async def settings(self):
                return {}

        client = tochka.TochkaClient(token="t", account_id="A, B, C",
                                     session_factory=Session)
        first = _run(banking.import_once(Crm(), client, today=date(2026, 9, 16)))
        self.assertEqual(first["pending"], {"B": "S-B"})
        self.assertEqual([r["account"] for r in saved], ["C"], "счёт строки - свой")
        calls.clear()
        _run(banking.import_once(Crm(), client, today=date(2026, 9, 16),
                                 pending=first["pending"]))
        self.assertIn(("GET", "B", "S-B"), calls, "B читается по своему номеру")
        self.assertNotIn(("POST", "B"), calls, "и не заказывается заново")

    def test_one_failing_account_does_not_blind_the_rest(self):
        class Client:
            accounts = ["A", "BAD"]

            async def statement(self, *, since, until, statement_id=None,
                                account_id=None):
                if account_id == "BAD":
                    raise tochka.TochkaError("счёт закрыт")
                return {"ready": True, "statement_id": "S", "rows": [
                    {"txn_id": "T-1", "account": "A"}]}

        class Crm:
            async def save_bank_txn(self, row):
                return 1

            async def settings(self):
                return {}

        out = _run(banking.import_once(Crm(), Client(), today=date(2026, 9, 16)))
        self.assertEqual(out["saved"], 1)

        class Broken(Client):
            accounts = ["BAD"]

        with self.assertRaises(tochka.TochkaError):
            _run(banking.import_once(Crm(), Broken(), today=date(2026, 9, 16)))


class TestStatementReadiness(unittest.TestCase):
    """Выписку заказывают один раз и читают по её номеру.

    Банк собирает документ не мгновенно. Круг, который каждый раз
    заказывает новую выписку и читает её тут же, не прочитает её никогда.
    """

    class Response:
        def __init__(self, data):
            self._data, self.status = data, 200

        async def json(self, content_type=None):
            return self._data

    def client(self, second: dict, log: list):
        outer = self

        class Session:
            async def request(self, method, url, **kwargs):
                log.append((method, url))
                if method == "POST":
                    return outer.Response(
                        {"Data": {"Statement": {"statementId": "S-1"}}})
                return outer.Response(second)

            async def close(self):
                pass

        return tochka.TochkaClient(token="tok", account_id="ACC",
                                   session_factory=Session)

    def test_empty_but_ready_statement_is_ready(self):
        """За выходные не было ни одной операции. Готовность - от статуса
        банка, а не от числа строк."""
        client = self.client({"Data": {"Statement": {"status": "Ready",
                                                     "Transaction": []}}}, [])
        got = _run(client.statement(since=date(2026, 9, 14),
                                    until=date(2026, 9, 16)))
        self.assertTrue(got["ready"])
        self.assertEqual(got["rows"], [])

    def test_the_next_round_reads_the_same_statement(self):
        calls: list = []
        client = self.client({"Data": {"Statement": {"status": "Processing"}}},
                             calls)
        first = _run(banking.import_once(None, client, today=date(2026, 9, 16)))
        self.assertEqual(first["pending"], {"ACC": "S-1"})
        second = _run(banking.import_once(None, client, today=date(2026, 9, 16),
                                          pending=first["pending"]))
        self.assertEqual(second["pending"], {"ACC": "S-1"})
        self.assertEqual([m for m, _ in calls], ["POST", "GET", "GET"],
                         "заказ один, чтений сколько угодно")


@unittest.skipUnless(HAVE_WEB, "нет fastapi/starlette")
class TestMoneyFixes(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.login()

    def test_tariff_change_keeps_the_extra_in_the_price(self):
        """Цена периода - велосипед плюс живые позиции. Смена тарифа
        писала голую цену тарифа: доп. аккумулятор переставал начисляться,
        а снятие позиции возвращало цену старого тарифа."""
        rental_id = tw.run(service.open_rental(
            self.crm, client=tw.run(self.crm.client(self.client_id)),
            bike=tw.run(self.crm.bike(self.bike_id)),
            tariff=tw.run(self.crm.tariff(self.tariff_id)),
            started_on=date(2026, 9, 1), contract_no="АВ-1", by="test"))
        tw.run(self.crm.add_rental_extra(
            rental_id, kind="battery", title="Доп. АКБ", price=D(1170),
            battery_id=None, by="test"))
        self.assertEqual(tw.run(self.crm.rental(rental_id))["price"], D("4170.00"))
        two_weeks = tw.run(self.crm.create_tariff("Две недели", 14, D(5500), None))
        self.client.post(f"/rentals/{rental_id}/tariff",
                         data={"tariff_id": two_weeks, "billing": "auto"})
        rental = tw.run(self.crm.rental(rental_id))
        self.assertEqual(rental["base_price"], D("5500.00"),
                         "цена велосипеда - это новый тариф")
        self.assertEqual(rental["price"], D("6670.00"),
                         "цена периода - тариф плюс доп. аккумулятор")

    def test_removing_a_stocked_line_puts_the_part_back(self):
        """Списание в наряд и строка наряда рождаются одним действием -
        уходить обязаны вместе, иначе на полке остаётся минус."""
        part_id = tw.run(self.crm.create_part(
            title="Камера", node="tube_tire", unit="шт", price=D(500), cost=D(300),
            min_stock=0, model=None, note=None))
        tw.run(self.crm.add_part_move(part_id=part_id, kind="receipt", qty=10,
                                      cost=D(300), created_by="test"))
        self.client.post("/orders", data={"bike_id": self.bike_id, "payer": "own",
                                          "estimate": "0", "complaint": "не едет"})
        order = tw.run(self.crm.work_orders())[0]
        self.client.post(f"/orders/{order['id']}/parts",
                         data={"part_id": part_id, "qty": "5"})
        self.assertEqual(tw.run(self.crm.part_stock(part_id)), 5)
        item = tw.run(self.crm.order_items(order["id"]))[0]
        self.client.post(f"/orders/{order['id']}/items/{item['id']}/delete")
        self.assertEqual(tw.run(self.crm.part_stock(part_id)), 10,
                         "строку убрали - запчасть вернулась на полку")

    def test_closed_order_keeps_its_parts(self):
        """У закрытого наряда запчасть уже в ремонте велосипеда: «вернуть
        на полку» значило бы держать одну деталь и на складе, и в ремонте."""
        part_id = tw.run(self.crm.create_part(
            title="Колодки", node="brake_pads", unit="шт", price=D(400), cost=D(200),
            min_stock=0, model=None, note=None))
        tw.run(self.crm.add_part_move(part_id=part_id, kind="receipt", qty=5,
                                      cost=D(200), created_by="test"))
        self.client.post("/orders", data={"bike_id": self.bike_id, "payer": "own",
                                          "estimate": "0", "complaint": "тормоза"})
        order = tw.run(self.crm.work_orders())[0]
        self.client.post(f"/orders/{order['id']}/parts",
                         data={"part_id": part_id, "qty": "2"})
        self.client.post(f"/orders/{order['id']}/close", data={})
        self.assertFalse(tw.run(self.crm.work_order(order["id"]))["status"] in
                         logic.ORDER_OPEN, "наряд закрылся")
        item = tw.run(self.crm.order_items(order["id"]))[0]
        self.client.post(f"/orders/{order['id']}/items/{item['id']}/delete")
        self.assertEqual(tw.run(self.crm.part_stock(part_id)), 3)
        self.assertEqual(len(tw.run(self.crm.order_items(order["id"]))), 1)
        self.assertFalse(tw.run(self.crm.delete_order_item(order["id"], item["id"])),
                         "и мимо панели тоже")

    def test_closing_an_order_without_nodes_still_records_the_repair(self):
        """Узел в строке необязателен. Без шапки bike_log себестоимость
        ремонта пропадала из месячного отчёта и из окупаемости."""
        self.client.post("/orders", data={"bike_id": self.bike_id, "payer": "own",
                                          "estimate": "0", "complaint": "стук"})
        order = tw.run(self.crm.work_orders())[0]
        self.client.post(f"/orders/{order['id']}/items", data={
            "title": "Перебрать каретку", "node": "", "qty": "1",
            "price": "0", "parts_cost": "1500", "labor_cost": "2500"})
        self.client.post(f"/orders/{order['id']}/close",
                         data={"bike_status": "available"})
        logs = [r for r in tw.run(self.crm.bike_log(self.bike_id))
                if r["kind"] == "repair"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["cost"], D("4000.00"))

    def test_closing_an_order_twice_writes_one_repair(self):
        self.client.post("/orders", data={"bike_id": self.bike_id, "payer": "own",
                                          "estimate": "0", "complaint": "стук"})
        order = tw.run(self.crm.work_orders())[0]
        self.client.post(f"/orders/{order['id']}/items", data={
            "title": "Замена камеры", "node": "wheels", "qty": "1",
            "price": "0", "parts_cost": "300", "labor_cost": "200"})
        self.client.post(f"/orders/{order['id']}/close",
                         data={"bike_status": "available"})
        self.client.post(f"/orders/{order['id']}/close",
                         data={"bike_status": "available"})
        logs = [r for r in tw.run(self.crm.bike_log(self.bike_id))
                if r["kind"] == "repair"]
        self.assertEqual(len(logs), 1, "два нажатия - один ремонт в журнале")

    def test_approve_cannot_be_set_from_the_edit_form(self):
        """«На согласовании» ставит отправка сметы: статус открытый,
        техника разобрана и держит место, и руками его не переставить -
        ни в него, ни из него."""
        self.client.post("/orders", data={"bike_id": self.bike_id, "payer": "own",
                                          "estimate": "0", "complaint": "стук"})
        order = tw.run(self.crm.work_orders())[0]
        r = self.client.post(f"/orders/{order['id']}/edit", data={
            "status": "approve", "tech_id": "", "estimate": "0", "note": ""})
        self.assertIn("недопустимое значение", self.get_ok(r.headers["location"]))
        self.assertEqual(tw.run(self.crm.work_order(order["id"]))["status"], "new")

    def test_purchase_respects_the_assembly_rule(self):
        """Партия заводится по тем же правилам, что и одиночный велосипед:
        включена сверка - вся партия «на сборке», а не в свободных."""
        tw.run(service.buy_bikes(
            self.crm, supplier_id=None, purchased_on=date(2026, 9, 1),
            codes=["P-1", "P-2"], model="Kugoo V3", price=D(50000),
            battery_count=2, service_months=36, residual=D(5000),
            battery_price=D(9000), battery_months=24, location=None,
            note=None, by="test"))
        for code in ("P-1", "P-2"):
            bike = tw.run(self.crm.bike_by_code(code))
            self.assertEqual(bike["status"], "new")

    def test_repair_invoice_does_not_pay_a_referral_bonus(self):
        """Оплата счёта за ремонт в журнал не идёт - значит и бонуса за
        друга по ней быть не может: аренду этот человек не брал."""
        agent_id = tw.run(self.crm.create_client(full_name="Агент Агентов",
                                                 phone="+79991112233"))
        tw.run(self.crm.set_ref_code(agent_id, "AB3D9K"))
        tw.run(self.crm.add_referral(agent_id=agent_id, tg_id=5001))
        ref = tw.run(self.crm.referral_of_tg(5001))
        tw.run(self.crm.update_referral(ref["id"], client_id=self.client_id,
                                        status="rented"))
        self.client.post("/orders", data={"bike_id": "", "payer": "client",
                                          "client_id": self.client_id,
                                          "object_note": "самокат клиента",
                                          "estimate": "3000",
                                          "complaint": "не едет"})
        order = tw.run(self.crm.work_orders())[0]
        self.client.post(f"/orders/{order['id']}/invoice")
        pay = tw.run(self.crm.pay_orders())[0]
        self.client.post(f"/payments/{pay['id']}", data={"action": "cash"})
        self.assertEqual(tw.run(self.crm.ledger_of(agent_id, limit=5)), [],
                         "бонус агенту за ремонт чужого самоката не платится")


@unittest.skipUnless(HAVE_WEB, "нет fastapi/starlette")
class TestTeamNoticeAddress(tw.WebCase):
    """Получателя командного уведомления назначает владелец в панели.

    Четыре уведомления слали напрямую в `cfg.contract_chat_id` мимо
    `notices.send_team`, и выбор в панели у них ничего не менял.
    """

    def setUp(self):
        super().setUp()
        self.seed()
        self.login()

    def test_paid_invoice_goes_to_the_chosen_person(self):
        tw.run(self.crm.set_notice("pay_paid", enabled=True, at_hour=None,
                                   chat_id="777", by="t"))
        order = {"no": "СЧТ-000001", "amount": D(3000), "purpose": "аренда",
                 "full_name": "Иванов Иван", "client_id": self.client_id}
        sent = tw.run(paying.report_paid(self.bot, self.crm, self.cfg, order))
        self.assertTrue(sent)
        self.assertEqual(self.bot.sent[-1][0], "777")

    def test_paid_invoice_can_be_switched_off(self):
        tw.run(self.crm.set_notice("pay_paid", enabled=False, at_hour=None, by="t"))
        order = {"no": "СЧТ-000001", "amount": D(3000), "purpose": "аренда",
                 "full_name": "Иванов Иван", "client_id": self.client_id}
        self.assertFalse(tw.run(paying.report_paid(self.bot, self.crm, self.cfg,
                                                   order)))
        self.assertEqual(self.bot.sent, [])


@unittest.skipUnless(HAVE_WEB, "нет fastapi/starlette")
class TestStaticCache(tw.WebCase):
    """Метка сборки в адресе стилей.

    Без неё браузер держал старую таблицу стилей после обновления
    панели: `StaticFiles` не шлёт `Cache-Control`, и новая разметка
    ехала по чужим стилям - боковое меню разваливалось в столбец, а
    технический чекбокс вылезал полем во всю ширину.
    """

    def test_stylesheets_carry_a_build_stamp(self):
        self.login()
        page = self.get_ok("/")
        self.assertRegex(page, r'/static/style\.css\?v=[0-9a-f]{8}"')
        self.assertRegex(page, r'/static/fonts\.css\?v=[0-9a-f]{8}"')

    def test_stamp_changes_when_a_file_changes(self):
        from app.web.app import static_stamp
        folder = Path(tempfile.mkdtemp())
        (folder / "style.css").write_text("a{}")
        before = static_stamp(folder)
        (folder / "style.css").write_text("a{color:red}")
        os.utime(folder / "style.css", (0, 0))
        self.assertNotEqual(static_stamp(folder), before)
        self.assertEqual(len(before), 8)

    def test_pages_are_never_cached_and_static_is(self):
        """Страница с балансом и телефоном клиента в памяти браузера не
        остаётся; версионированная статика, наоборот, живёт долго."""
        self.login()
        page = self.client.get("/")
        self.assertEqual(page.headers.get("cache-control"), "no-store")
        css = self.client.get("/static/style.css?v=abc")
        self.assertEqual(css.status_code, 200)
        self.assertIn("max-age=31536000", css.headers.get("cache-control", ""))
        # Без метки год давать нельзя: на шрифты ссылается сам fonts.css,
        # и подменённый файл иначе не подхватился бы до конца года.
        bare = self.client.get("/static/style.css")
        self.assertEqual(bare.headers.get("cache-control"), "no-cache")
        self.assertEqual(self.client.get("/static/нет.css").status_code, 404)

    def test_hidden_beats_the_input_rule(self):
        """Правило `input{display:block}` сильнее браузерного
        `[hidden]{display:none}` - скрытое надо защитить явно."""
        root = Path(__file__).resolve().parent.parent
        css = (root / "app/web/static/style.css").read_text()
        hidden = css.index("[hidden]{display:none !important}")
        fields = css.index("input,select,textarea{display:block")
        self.assertLess(hidden, fields, "защита стоит до правила полей")


@unittest.skipUnless(HAVE_WEB, "нет fastapi/starlette")
class TestBotClosesRental(tw.WebCase):
    """Акт возврата подписан в боте - батареи возвращаются вместе с
    велосипедом. Синхронизация закрывала аренду мимо сервисного слоя, и
    батареи оставались «у клиента» навсегда: по две штуки на аренду."""

    def setUp(self):
        super().setUp()
        self.seed()

    def test_batteries_come_back_with_the_bike(self):
        model = tw.run(self.crm.create_battery_model(
            title="48V 20Ah", brand="Kugoo", voltage=48, capacity=20,
            price=D(9000), service_months=24, note=None))
        battery_id = tw.run(self.crm.create_battery(
            by="test", code="AKB-1", model_id=model, status="available"))
        rental_id = tw.run(service.open_rental(
            self.crm, client=tw.run(self.crm.client(self.client_id)),
            bike=tw.run(self.crm.bike(self.bike_id)),
            tariff=tw.run(self.crm.tariff(self.tariff_id)),
            started_on=date(2026, 9, 1), contract_no="АВ-1", by="test"))
        tw.run(service.issue_with_batteries(self.crm, rental_id,
                                            bike=tw.run(self.crm.bike(self.bike_id)),
                                            battery_ids=[battery_id], by="test"))
        self.assertEqual(tw.run(self.crm.battery(battery_id))["status"], "rented")
        tw.run(sync.on_rental_closed(self.crm, {"tg_id": 5001, "contract_no": "АВ-1"},
                                     today=date(2026, 9, 21)))
        self.assertEqual(tw.run(self.crm.rental(rental_id))["status"], "closed")
        self.assertEqual(tw.run(self.crm.battery(battery_id))["status"], "available",
                         "батарея вернулась вместе с велосипедом")

    def test_buyout_closes_the_rental_and_sells_the_bike(self):
        """Выкупленный велосипед - чужой: аренда закрыта, в парк не вернётся,
        а аккумулятор по акту выкупа возвращён и свободен."""
        model = tw.run(self.crm.create_battery_model(
            title="48V 20Ah", brand="Kugoo", voltage=48, capacity=20,
            price=D(9000), service_months=24, note=None))
        battery_id = tw.run(self.crm.create_battery(
            by="test", code="AKB-2", model_id=model, status="available"))
        rental_id = tw.run(service.open_rental(
            self.crm, client=tw.run(self.crm.client(self.client_id)),
            bike=tw.run(self.crm.bike(self.bike_id)),
            tariff=tw.run(self.crm.tariff(self.tariff_id)),
            started_on=date(2026, 8, 1), contract_no="АВ-1", by="test"))
        tw.run(service.issue_with_batteries(self.crm, rental_id,
                                            bike=tw.run(self.crm.bike(self.bike_id)),
                                            battery_ids=[battery_id], by="test"))
        tw.run(sync.on_buyout_signed(self.crm, {"tg_id": 5001, "contract_no": "АВ-1"},
                                     today=date(2026, 9, 21)))
        self.assertEqual(tw.run(self.crm.rental(rental_id))["status"], "closed")
        self.assertEqual(tw.run(self.crm.bike(self.bike_id))["status"], "sold")
        self.assertEqual(tw.run(self.crm.battery(battery_id))["status"], "available")

    def test_return_act_after_buyout_does_not_free_the_bike(self):
        rental_id = tw.run(service.open_rental(
            self.crm, client=tw.run(self.crm.client(self.client_id)),
            bike=tw.run(self.crm.bike(self.bike_id)),
            tariff=tw.run(self.crm.tariff(self.tariff_id)),
            started_on=date(2026, 8, 1), contract_no="АВ-1", by="test"))
        tw.run(sync.on_rental_closed(self.crm, {"tg_id": 5001, "contract_no": "АВ-1",
                                                "buyout_signed_at": datetime.now(UTC)},
                                     today=date(2026, 9, 21)))
        self.assertEqual(tw.run(self.crm.rental(rental_id))["status"], "closed")
        self.assertEqual(tw.run(self.crm.bike(self.bike_id))["status"], "sold")


@unittest.skipUnless(HAVE_WEB, "нет fastapi/starlette")
class TestSigningLink(tw.WebCase):
    """Ссылка ПЭП открыта клиенту без входа - значит, закрываться она
    обязана сама: отменённая и просроченная заявка отдавала договор и
    соглашение об ЭП кому угодно с токеном."""

    def setUp(self):
        super().setUp()
        self.seed()
        self.login()
        self.path = Path("/tmp/kyc/аудит-договор.pdf")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes("%PDF-1.4 подписанный договор".encode())

    def make(self, **kw):
        return tw.run(self.crm.create_sign_request(
            client_id=self.client_id, rental_id=None, token=kw.get("token", "tok"),
            docs=[{"title": "Договор", "path": str(self.path), "sha256": "x"}],
            agreement="соглашение об ЭП",
            expires_at=kw.get("expires_at",
                              datetime.now(UTC) + timedelta(days=7)),
            by="admin"))

    def test_open_link_serves_the_packet(self):
        self.make(token="live")
        self.assertEqual(self.client.get("/sign/live/doc/0").status_code, 200)
        self.assertEqual(self.client.get("/sign/live/agreement").status_code, 200)

    def test_cancelled_link_stops_serving(self):
        req = self.make(token="dead")
        tw.run(self.crm.cancel_sign_request(req["id"], by="admin"))
        self.assertEqual(self.client.get("/sign/dead/doc/0").status_code, 404)
        self.assertEqual(self.client.get("/sign/dead/agreement").status_code, 404)

    def test_expired_link_stops_serving(self):
        self.make(token="old", expires_at=datetime.now(UTC) - timedelta(days=1))
        self.assertEqual(self.client.get("/sign/old/doc/0").status_code, 404)
        self.assertEqual(self.client.get("/sign/old/agreement").status_code, 404)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/starlette")
class TestMapEscaping(tw.WebCase):
    """Кличка трекера приходит и из формы, и из кабинета StarLine - оба
    источника чужие, а карта - единственная страница со скриптом."""

    def setUp(self):
        super().setUp()
        self.seed()
        self.login()

    def test_alias_cannot_close_the_script(self):
        # Трекер без велосипеда: кличка тогда и есть подпись точки.
        tid = tw.run(self.crm.create_tracker(
            device_id="1001", alias="</script><img src=x onerror=alert(1)>",
            bike_id=None, note=None))
        tw.run(self.crm.update_tracker(tid, lat=55.78, lon=49.12,
                                       last_seen=datetime.now(UTC)))
        page = self.get_ok("/map")
        self.assertNotIn("</script><img", page,
                         "кличка не должна закрывать скрипт")
        self.assertIn("u003c/script", page.lower(),
                      "она уходит в скрипт экранированной")


@unittest.skipUnless(HAVE_WEB, "нет fastapi/starlette")
class TestTwoShifts(tw.WebCase):
    """Две точки - две открытые смены. Наличный платёж принадлежит одной."""

    def setUp(self):
        super().setUp()
        self.seed()
        self.login()

    def test_cash_lands_in_one_shift_only(self):
        first = tw.run(self.crm.create_shift(location="Павлюхина", opening=D(0),
                                             note=None, by="staff:admin"))
        second = tw.run(self.crm.create_shift(location="Адоратского", opening=D(0),
                                              note=None, by="staff:other"))
        self.client.post(f"/clients/{self.client_id}/ledger",
                         data={"kind": "payment", "amount": "3000",
                               "method": "cash", "note": "за неделю"})
        mine = tw.run(self.crm.shift_payments(first))
        theirs = tw.run(self.crm.shift_payments(second))
        self.assertEqual([p["amount"] for p in mine], [D("3000.00")])
        self.assertEqual(theirs, [],
                         "на второй точке этих денег нет - и недостачи тоже")


if __name__ == "__main__":
    unittest.main()
