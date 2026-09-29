"""Сроки ремонта, запчасти на исходе и план замены аккумуляторов.

Три вопроса владельца, на которые раньше отвечала память механика:
какой велосипед стоит в ремонте дольше, чем должен; какая запчасть вот-вот
кончится; какие батареи пора менять и сколько на это отложить. Здесь -
арифметика (чистая логика), заглушка базы, дневной проход, экраны панели
и одна проверка новых колонок на живом Postgres.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import logic  # noqa: E402
from tests.fake_crm import FakeCrm  # noqa: E402

try:
    from app.crm import billing, service
    HAVE_BILLING = True
except ImportError:                                    # pragma: no cover
    HAVE_BILLING = False

try:
    from tests import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

try:
    import asyncpg
    import pgserver

    from app.crm.db import CrmDB
    from app.db import Database, _init_connection
    HAVE_PG = True
except ImportError:                                    # pragma: no cover
    HAVE_PG = False

D = Decimal
TODAY = date(2026, 9, 21)                  # понедельник
SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"


def run(coro):
    return asyncio.run(coro)


def order(days_open: int, *, status: str = "in_work", node_norm=None,
          norm_node=None, **extra) -> dict:
    opened = datetime(2026, 9, 21, 9, tzinfo=UTC) - timedelta(days=days_open)
    return {"id": days_open, "no": f"РЕМ-{days_open:06d}", "status": status,
            "opened_at": opened, "closed_at": None, "node_norm": node_norm,
            "norm_node": norm_node, **extra}


# ─────────────────────────── сроки ремонта ───────────────────────────

class TestRepairNorms(unittest.TestCase):
    def test_default_norm_comes_from_settings(self):
        self.assertEqual(logic.repair_norm_default({}), logic.ORDER_STUCK_DAYS)
        self.assertEqual(logic.repair_norm_default({"repair_norm_days": "5"}), 5)
        self.assertEqual(logic.repair_norm_default({"repair_norm_days": "0"}), 0,
                         "ноль - честный ноль: чиним день в день")
        for junk in ("-3", "abc", "", "999"):
            self.assertEqual(logic.repair_norm_default({"repair_norm_days": junk}),
                             logic.ORDER_STUCK_DAYS, junk)

    def test_node_norm_from_the_form(self):
        self.assertEqual(logic.check_norm_days("").value, None, "пусто - общий срок")
        self.assertEqual(logic.check_norm_days(" 0 ").value, 0)
        self.assertEqual(logic.check_norm_days("7").value, 7)
        for junk in ("-1", "²", "400", "три"):
            self.assertFalse(logic.check_norm_days(junk).ok, junk)

    def test_heaviest_node_wins_over_the_default(self):
        self.assertEqual(logic.order_norm(order(1, node_norm=5), 3), 5)
        self.assertEqual(logic.order_norm(order(1, node_norm=0), 3), 0)
        self.assertEqual(logic.order_norm(order(1), 3), 3, "без узлов - общая")

    def test_line_without_own_deadline_keeps_the_general_one(self):
        """Колодки (сутки) рядом с проводкой без срока: наряд меряет общий
        срок - добавленная работа не может укоротить срок всего наряда."""
        quick = order(2, node_norm=1, norm_node="Тормоза: колодки", norm_general=True)
        self.assertEqual(logic.order_norm(quick, 3), 3)
        self.assertEqual(logic.order_overdue(quick, default=3, today=TODAY), 0)
        self.assertFalse(logic.order_stuck(quick, today=TODAY))
        self.assertIsNone(logic.order_norm_node(quick, 3), "подпись - общий срок")
        # только колодки - их сутки
        alone = order(2, node_norm=1, norm_node="Тормоза: колодки", norm_general=False)
        self.assertEqual(logic.order_norm(alone, 3), 1)
        self.assertEqual(logic.order_norm_node(alone, 3), "Тормоза: колодки")
        # долгий узел длиннее общего - решает он, и подпись его
        motor = order(2, node_norm=5, norm_node="Мотор-колесо", norm_general=True)
        self.assertEqual(logic.order_norm(motor, 3), 5)
        self.assertEqual(logic.order_norm_node(motor, 3), "Мотор-колесо")
        self.assertIsNone(logic.order_norm_node(order(2), 3), "строк нет - общий")
        rows = logic.overdue_orders([order(5, node_norm=1, norm_node="Тормоза: колодки",
                                           norm_general=True, bike_code="B-1")],
                                    default=3, today=TODAY)
        self.assertEqual((rows[0]["norm"], rows[0]["overdue"]), (3, 2))
        text = logic.repair_overdue_lines(rows)
        self.assertIn("срок 3 дн., просрочено на 2 дн.", text)
        self.assertNotIn("колодки", text, "срок не колодок - и подпись не их")

    def test_overdue_counts_days_over_the_norm(self):
        self.assertEqual(logic.order_overdue(order(6, node_norm=5), default=3,
                                             today=TODAY), 1)
        self.assertEqual(logic.order_overdue(order(6), default=3, today=TODAY), 3)
        self.assertEqual(logic.order_overdue(order(5, node_norm=5), default=3,
                                             today=TODAY), 0, "ровно срок - укладывается")
        # ждёт запчасть и на согласовании - тоже простой
        for status in ("waiting", "approve", "new"):
            self.assertEqual(logic.order_overdue(order(5, status=status), default=3,
                                                 today=TODAY), 2, status)
        closed = order(10, status="done", closed_at=datetime(2026, 9, 20, tzinfo=UTC))
        self.assertEqual(logic.order_overdue(closed, default=3, today=TODAY), 0)

    def test_stuck_means_longer_than_the_norm(self):
        """«Стоят дольше срока»: ровно три дня при сроке три - ещё не просрочка."""
        self.assertFalse(logic.order_stuck(order(3), today=TODAY))
        self.assertTrue(logic.order_stuck(order(4), today=TODAY))
        self.assertFalse(logic.order_stuck(order(4, node_norm=7), today=TODAY),
                         "у мотор-колеса свой, более долгий срок")
        self.assertTrue(logic.order_stuck(order(2, node_norm=1), today=TODAY),
                        "колодки - за сутки")

    def test_overdue_orders_are_ranked_and_the_digest_is_escaped(self):
        rows = logic.overdue_orders(
            [order(2), order(9, node_norm=5, norm_node="Мотор-колесо", bike_code="B-7"),
             order(5, object_note="<b>самокат</b>", status="waiting")],
            default=3, today=TODAY)
        self.assertEqual([r["overdue"] for r in rows], [4, 2])
        text = logic.repair_overdue_lines(rows)
        self.assertIn("Наряды дольше срока ремонта: 2", text)
        self.assertIn("№ B-7: 9 дн., срок 5 дн. (Мотор-колесо), просрочено на 4 дн.",
                      text)
        self.assertIn("&lt;b&gt;самокат&lt;/b&gt;", text, "свободный текст экранирован")
        self.assertIn("Ждёт запчасть", text)
        self.assertEqual(logic.repair_overdue_lines([]), "", "нечего - молчим")

    def test_service_desk_rows_carry_the_norm(self):
        bikes = [{"id": 1, "code": "B-1", "status": "repair", "idle_days": 1},
                 {"id": 2, "code": "B-2", "status": "repair", "idle_days": 1},
                 {"id": 3, "code": "B-3", "status": "repair", "idle_days": 9}]
        rows = logic.service_rows(
            bikes, {1: order(4, bike_id=1), 2: order(4, node_norm=6, bike_id=2)},
            today=TODAY, norm=3)
        by = {r["code"]: r for r in rows}
        self.assertEqual((by["B-1"]["norm"], by["B-1"]["overdue"]), (3, 1))
        self.assertTrue(by["B-1"]["stuck"])
        self.assertEqual((by["B-2"]["norm"], by["B-2"]["overdue"]), (6, 0))
        self.assertFalse(by["B-2"]["stuck"])
        self.assertTrue(by["B-3"]["stuck"], "без наряда - всегда стоит")
        self.assertIsNone(by["B-3"]["norm"])
        summary = logic.service_summary(rows)
        self.assertEqual(summary["stuck"], 2)
        # плитка «дольше срока» ведёт в список нарядов: велосипед без наряда
        # там не виден, у него своя плитка
        self.assertEqual((summary["overdue"], summary["no_order"]), (1, 1))

    def test_today_tasks_use_the_owner_norm(self):
        orders = [order(4), order(2, node_norm=1)]
        tasks = {t["code"]: t for t in logic.today_tasks(orders=orders, today=TODAY)}
        self.assertEqual(tasks["orders"]["count"], 2)
        self.assertEqual(tasks["orders"]["url"], "/orders?overdue=1")
        tasks = {t["code"]: t for t in logic.today_tasks(orders=orders, today=TODAY,
                                                         repair_norm=5)}
        self.assertEqual(tasks["orders"]["count"], 1, "общий срок 5 - только колодки")


# ─────────────────────────── запчасти на исходе ───────────────────────────

def part(pid: int, title: str, minimum: int, **extra) -> dict:
    return {"id": pid, "title": title, "min_stock": minimum, "unit": "шт",
            "cost": D(100), "price": D(200), "active": True, **extra}


class TestPartsLow(unittest.TestCase):
    def rows(self, transit=None):
        parts = [part(1, "Камера", 5), part(2, "Колодки", 2), part(3, "Зеркало", 0),
                 part(4, "Грипсы", 3)]
        return logic.part_rows(parts, {1: 1, 2: 2, 3: 0, 4: 9}, today=TODAY,
                               transit=transit)

    def test_at_the_minimum_is_ordered_by_one(self):
        """Неснижаемый - точка заказа: «на пределе» тоже потребность, по
        одной штуке, - следующий расход уведёт ниже, а поставка идёт днями."""
        rows = {r["title"]: r for r in self.rows()}
        self.assertTrue(rows["Камера"]["below"])
        self.assertTrue(rows["Колодки"]["at_min"], "ровно неснижаемый - на пределе")
        self.assertFalse(rows["Колодки"]["below"])
        self.assertFalse(rows["Зеркало"]["at_min"], "без неснижаемого предела нет")
        self.assertEqual([r["title"] for r in self.rows()][:2], ["Камера", "Колодки"],
                         "ниже неснижаемого, за ним на пределе")
        summary = logic.stock_summary(self.rows())
        self.assertEqual((summary["below"], summary["at_min"]), (1, 1))
        needs = logic.part_needs(self.rows())
        self.assertEqual([(n["title"], n["qty"], n["source"], n["edge"]) for n in needs],
                         [("Камера", 4, "min_stock", False),
                          ("Колодки", 1, "min_stock", True)])
        # едет хоть одна - на пределе покрыт, второй раз не заказываем
        edge = next(n for n in logic.part_needs(self.rows(), transit={2: 1})
                    if n["part_id"] == 2)
        self.assertTrue(edge["covered"])
        archived = logic.part_rows([part(9, "Старое", 2, active=False)], {9: 2},
                                   today=TODAY)
        self.assertEqual(logic.part_needs(archived), [], "архив не заказывают")

    def test_transit_is_subtracted_and_shown(self):
        needs = {n["part_id"]: n for n in logic.part_needs(self.rows(), transit={1: 3})}
        self.assertEqual((needs[1]["qty"], needs[1]["transit"], needs[1]["covered"]),
                         (1, 3, False), "не хватает 4, едет 3 - заказать 1")
        needs = logic.part_needs(self.rows(), transit={1: 10})
        self.assertEqual(needs[-1]["part_id"], 1, "покрытые - в конце списка")
        self.assertTrue(needs[-1]["covered"])
        self.assertEqual(needs[-1]["qty"], 0)
        # наряд ждёт то, что уже едет: потребность покрыта
        waiting = [{"part_id": 4, "title": "Грипсы", "qty": 1, "work_order_id": 7,
                    "work_order_no": "РЕМ-000007", "bike_code": "B-1"}]
        needs = logic.part_needs(self.rows(), waiting, {4: 1})
        grips = next(n for n in needs if n["part_id"] == 4)
        self.assertTrue(grips["covered"])
        self.assertEqual(needs[-1]["part_id"], 4, "покрытые - в конце списка")

    def test_weekly_digest_lists_low_parts_with_transit(self):
        rows = logic.parts_low(self.rows({1: 3}))
        self.assertEqual([r["title"] for r in rows], ["Камера", "Колодки"])
        text = logic.parts_low_lines(rows)
        self.assertIn("Запчасти на исходе: 2 (ниже неснижаемого 1, на пределе 1)", text)
        self.assertIn("Камера — 1 из 5 шт, не хватает 4, в пути 3", text)
        self.assertIn("Колодки — 2 из 2 шт, ровно неснижаемый", text)
        self.assertEqual(logic.parts_low_lines([]), "")
        archived = logic.part_rows([part(9, "Старое", 5, active=False)], {}, today=TODAY)
        self.assertEqual(logic.parts_low(archived), [], "архив не заказывают")

    def test_weekly_notice_day(self):
        state = logic.notice_settings([])["parts_low"]
        self.assertTrue(logic.weekly_due(state, TODAY), "по умолчанию - понедельник")
        self.assertFalse(logic.weekly_due(state, TODAY + timedelta(days=1)))
        friday = {"extra": {"weekday": 5}}
        self.assertTrue(logic.weekly_due(friday, date(2026, 9, 25)))
        self.assertTrue(logic.weekly_due({"extra": {"weekday": 9}}, TODAY),
                        "мусор - понедельник, а не «никогда»")
        self.assertEqual(logic.notice_param_label("parts_low", "weekday")[3:], (1, 7))
        self.assertEqual(logic.notice_param_label("review_ask", "after_days"),
                         logic.NOTICE_PARAM_DEFAULT_LABEL)
        rows = logic.notice_rows(logic.notice_settings([]))
        team = {r["code"]: r for r in rows["team"]}
        self.assertEqual(team["parts_low"]["labels"]["weekday"][1], "день недели")
        self.assertIn("repair_overdue", team)


# ─────────────────────────── износ аккумуляторов ───────────────────────────

def battery(bid: int, **extra) -> dict:
    return {"id": bid, "code": f"АКБ-{bid}", "status": "available", "model_id": 1,
            "model_title": "Kugoo 60V", "model_price": D("16000"), "cycles": 0,
            "service_months": 15, "purchased_on": None, "purchase_price": None,
            "created_at": datetime(2026, 1, 1, tzinfo=UTC), "commissioned_at": None,
            **extra}


class TestBatteryWear(unittest.TestCase):
    def test_add_months_keeps_the_calendar(self):
        self.assertEqual(logic.add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(logic.add_months(date(2028, 1, 31), 1), date(2028, 2, 29))
        self.assertEqual(logic.add_months(date(2026, 11, 15), 2), date(2027, 1, 15))
        self.assertEqual(logic.add_months(date(2025, 6, 21), 15), date(2026, 9, 21))
        self.assertEqual(logic.add_months(date(2026, 12, 1), 0), date(2026, 12, 1))

    def test_age_decides_when_cycles_are_slow(self):
        wear = logic.battery_wear(battery(1, purchased_on=date(2025, 8, 1), cycles=20),
                                  today=TODAY)
        self.assertEqual(wear["replace_on"], date(2026, 11, 1))
        self.assertEqual(wear["reason"], "age")
        self.assertEqual(wear["age_months"], 13)
        self.assertTrue(wear["dated"])

    def test_cycles_decide_at_a_fast_pace(self):
        # 400 циклов за 200 дней - два в день; до 500 осталось 50 дней
        bought = TODAY - timedelta(days=200)
        wear = logic.battery_wear(battery(1, purchased_on=bought, cycles=400),
                                  today=TODAY, max_cycles=500)
        self.assertEqual(wear["reason"], "cycles")
        self.assertEqual(wear["replace_on"], TODAY + timedelta(days=50))
        # у модели свой ресурс - вдвое больше
        wear = logic.battery_wear(battery(1, purchased_on=bought, cycles=400,
                                          model_max_cycles=1000), today=TODAY)
        self.assertEqual(wear["cycle_limit"], 1000)
        self.assertEqual(wear["reason"], "age")
        # ресурс выработан - менять сегодня
        wear = logic.battery_wear(battery(1, purchased_on=bought, cycles=600),
                                  today=TODAY, max_cycles=500)
        self.assertEqual((wear["replace_on"], wear["left_days"]), (TODAY, 0))

    def test_young_battery_has_no_pace_and_undated_uses_the_card(self):
        wear = logic.battery_wear(battery(1, purchased_on=TODAY - timedelta(days=10),
                                          cycles=30), today=TODAY)
        self.assertEqual(wear["reason"], "age", "за десять дней темпа нет")
        wear = logic.battery_wear(battery(1), today=TODAY)
        self.assertFalse(wear["dated"])
        self.assertEqual(wear["replace_on"], date(2027, 4, 1), "от заведения карточки")
        wear = logic.battery_wear(battery(1, created_at=None), today=TODAY)
        self.assertIsNone(wear["replace_on"], "ни даты, ни истории - не посчитать")

    def test_undated_battery_has_no_cycle_pace(self):
        """Старую батарею заводят сразу с её циклами: 300 циклов «за месяц с
        заведения» - не темп, а история до карточки. Без даты покупки
        циклы решают, только когда ресурс уже выработан."""
        entered = datetime.combine(TODAY - timedelta(days=31), datetime.min.time(),
                                   tzinfo=UTC)
        old = battery(1, created_at=entered, cycles=300)
        wear = logic.battery_wear(old, today=TODAY, max_cycles=500)
        self.assertFalse(wear["dated"])
        self.assertIsNone(wear["by_cycles"])
        self.assertEqual(wear["reason"], "age")
        self.assertEqual(wear["replace_on"], logic.add_months(TODAY - timedelta(days=31), 15))
        plan = logic.battery_wear_plan([old], today=TODAY, max_cycles=500)
        self.assertEqual([h["count"] for h in plan["horizons"]], [0, 0, 0],
                         "в замену за месяц не попадает")
        spent = logic.battery_wear(battery(2, created_at=entered, cycles=500),
                                   today=TODAY, max_cycles=500)
        self.assertEqual((spent["replace_on"], spent["reason"]), (TODAY, "cycles"))
        # с датой покупки темп есть: те же 300 циклов за 31 день - гонка
        dated = logic.battery_wear(battery(3, purchased_on=TODAY - timedelta(days=31),
                                           cycles=300), today=TODAY, max_cycles=500)
        self.assertEqual(dated["reason"], "cycles")

    def test_plan_counts_horizons_and_budget(self):
        rows = [
            battery(1, purchased_on=date(2025, 6, 1)),                  # срок вышел
            battery(2, purchased_on=date(2025, 7, 10)),                 # 10.10.2026
            battery(3, purchased_on=date(2025, 9, 1), model_price=None,
                    purchase_price=D("15000"), model_id=2,
                    model_title="Monster 60V"),                         # 01.12.2026
            battery(4, purchased_on=date(2025, 12, 1), model_price=None,
                    model_id=2, model_title="Monster 60V"),             # 01.03.2027
            battery(5, purchased_on=date(2026, 9, 1)),                  # 2027 - вне плана
            battery(6, purchased_on=date(2025, 1, 1), status="lost"),   # не в обороте
            battery(7, purchased_on=date(2025, 1, 1), status="new"),
        ]
        plan = logic.battery_wear_plan(rows, today=TODAY)
        self.assertEqual(plan["total"], 5)
        self.assertEqual(plan["overdue"], 1)
        self.assertEqual([r["id"] for r in plan["rows"]], [1, 2, 3, 4])
        by = {h["months"]: h for h in plan["horizons"]}
        self.assertEqual([by[h]["count"] for h in (1, 3, 6)], [2, 3, 4],
                         "горизонты накопительные")
        self.assertEqual(by[1]["budget"], D("32000.00"), "две Kugoo по цене каталога")
        self.assertEqual(by[3]["budget"], D("47000.00"), "без цены модели - цена покупки")
        self.assertEqual(by[6]["unpriced"], 1)
        self.assertEqual(by[6]["budget"], D("47000.00"), "без цены вовсе - не в сумме")
        models = {m["title"]: m for m in plan["models"]}
        self.assertEqual(models["Kugoo 60V"]["cells"][1]["count"], 2)
        self.assertEqual(models["Monster 60V"]["cells"][6]["count"], 2)
        self.assertEqual(models["Monster 60V"]["cells"][6]["unpriced"], 1)

    def test_default_cycles_setting_and_tired_flag(self):
        self.assertEqual(logic.battery_max_cycles({}), logic.BATTERY_CYCLES_WARN)
        self.assertEqual(logic.battery_max_cycles({"battery_max_cycles": "800"}), 800)
        self.assertEqual(logic.battery_max_cycles({"battery_max_cycles": "0"}),
                         logic.BATTERY_CYCLES_WARN, "ноль списал бы весь парк")
        rows = logic.battery_rows([battery(1, cycles=600, model_max_cycles=1000),
                                   battery(2, cycles=600)], today=TODAY)
        by = {r["id"]: r for r in rows}
        self.assertFalse(by[1]["tired"], "у модели ресурс 1000")
        self.assertTrue(by[2]["tired"])
        self.assertEqual(by[2]["cycle_limit"], logic.BATTERY_CYCLES_WARN)


# ─────────────────────────── заглушка базы ───────────────────────────

class TestFakeCrm(unittest.TestCase):
    def setUp(self):
        self.crm = FakeCrm()

    def test_order_takes_the_heaviest_normed_node(self):
        crm = self.crm
        self.assertTrue(run(crm.set_node_norm("motor_wheel", 5)))
        self.assertTrue(run(crm.set_node_norm("brake_pads", 1)))
        self.assertFalse(run(crm.set_node_norm("nope", 1)))
        oid = run(crm.create_work_order(bike_id=None, payer="own", client_id=None,
                                        complaint=None, object_note="x", tech_id=None,
                                        estimate=D(0), created_by="t"))
        empty = run(crm.work_order(oid))
        self.assertIsNone(empty["node_norm"])
        self.assertFalse(empty["norm_general"], "строк нет - и мерить нечего")
        for node in ("brake_pads", "motor_wheel"):
            run(crm.add_order_item(oid, title=node, node=node, work_type_id=None, qty=1,
                                   price=D(0), parts_cost=D(0), labor_cost=D(0)))
        self.assertFalse(run(crm.work_order(oid))["norm_general"],
                         "у всех строк свой срок")
        run(crm.add_order_item(oid, title="wiring", node="wiring", work_type_id=None,
                               qty=1, price=D(0), parts_cost=D(0), labor_cost=D(0)))
        got = run(crm.work_order(oid))
        self.assertEqual((got["node_norm"], got["norm_node"], got["norm_general"]),
                         (5, "Мотор-колесо", True))
        nodes = {n["code"]: n["norm_days"] for n in run(crm.repair_nodes())}
        self.assertEqual((nodes["motor_wheel"], nodes["wiring"]), (5, None))
        # мотору срок сняли: колодки (сутки) рядом с проводкой без срока -
        # наряд меряет общий срок, а не сутки колодок
        run(crm.set_node_norm("motor_wheel", None))
        got = run(crm.work_order(oid))
        self.assertEqual((got["node_norm"], got["norm_general"]), (1, True))
        self.assertEqual(logic.order_norm(got, 3), 3)
        self.assertIsNone(logic.order_norm_node(got, 3))
        # строка без узла - тоже общий срок
        other = run(crm.create_work_order(bike_id=None, payer="own", client_id=None,
                                          complaint=None, object_note="y", tech_id=None,
                                          estimate=D(0), created_by="t"))
        for node in ("brake_pads", None):
            run(crm.add_order_item(other, title="Диагностика", node=node,
                                   work_type_id=None, qty=1, price=D(0),
                                   parts_cost=D(0), labor_cost=D(0)))
        self.assertTrue(run(crm.work_order(other))["norm_general"])

    @unittest.skipUnless(HAVE_BILLING, "aiogram не установлен")
    def test_transit_and_collect(self):
        crm = self.crm
        pid = run(crm.create_part(title="Контроллер", node="controller", unit="шт",
                                  cost=D(2500), price=D(4000), min_stock=3, model=None,
                                  note=None))
        first = run(service.collect_part_needs(crm, by="t"))
        self.assertEqual(first["added"], 1)
        run(crm.update_part_order(first["order"]["id"], status="ordered"))
        self.assertEqual(run(crm.parts_in_transit()), {pid: 3})
        again = run(service.collect_part_needs(crm, by="t"))
        self.assertEqual(again["added"], 0, "то, что едет, второй раз не заказываем")
        run(crm.update_part_order(first["order"]["id"], status="received"))
        self.assertEqual(run(crm.parts_in_transit()), {}, "принятое уже не в пути")


# ─────────────────────────── дневной проход ───────────────────────────

class Bot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))


class BotDB:
    async def get_user(self, tg_id):
        return None


CHAT = -100500


@unittest.skipUnless(HAVE_BILLING, "aiogram не установлен")
class TestDailyPass(unittest.TestCase):
    def setUp(self):
        self.crm = FakeCrm()
        self.bot = Bot()
        self.cfg = types.SimpleNamespace(contract_chat_id=CHAT, remind_before_days=2)

    def late_order(self, days: int, node: str | None = None,
                   since: date | None = None) -> int:
        """Наряд, открытый `days` суток назад от `since` (по умолчанию -
        сегодня): проход с датой из календаря теста считает сутки от неё,
        а не от настоящего «сейчас», иначе тест зависел бы от дня прогона."""
        oid = run(self.crm.create_work_order(
            bike_id=None, payer="own", client_id=None, complaint=None,
            object_note="самокат", tech_id=None, estimate=D(0), created_by="t"))
        base = (datetime.combine(since, datetime.min.time(), tzinfo=UTC).replace(hour=9)
                if since else datetime.now(UTC))
        self.crm.orders_[oid]["opened_at"] = base - timedelta(days=days)
        if node:
            run(self.crm.add_order_item(oid, title=node, node=node, work_type_id=None,
                                        qty=1, price=D(0), parts_cost=D(0),
                                        labor_cost=D(0)))
        return oid

    def low_part(self) -> int:
        return run(self.crm.create_part(title="Камера", node="tube_tire", unit="шт",
                                        cost=D(300), price=D(600), min_stock=4,
                                        model=None, note=None))

    def pass_at(self, day: date, hour: int, done: dict | None) -> list[str]:
        now = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=5)
        run(billing.run_daily(self.bot, BotDB(), self.crm, self.cfg, today=day, now=now,
                              done=done))
        return [t for c, t in self.bot.sent if c == CHAT]

    def test_overdue_orders_reach_the_chat_once_a_day(self):
        run(self.crm.set_node_norm("motor_wheel", 5))
        self.late_order(6, "motor_wheel")                 # просрочен на 1
        self.late_order(4, "motor_wheel")                 # укладывается
        done: dict = {}
        today = date.today()
        for hour in (8, 10, 14):
            sent = self.pass_at(today, hour, done)
        digests = [t for t in sent if "дольше срока" in t]
        self.assertEqual(len(digests), 1)
        self.assertIn("просрочено на 1 дн.", digests[0])
        self.assertIn("(Мотор-колесо)", digests[0])
        self.assertEqual(done["repair_overdue"], today)

    def test_quick_node_does_not_put_a_long_job_into_the_digest(self):
        """Колодки (сутки) плюс проводка без срока, двое суток: общий срок 3 -
        укладывается, в чат не идёт."""
        run(self.crm.set_node_norm("brake_pads", 1))
        oid = self.late_order(2, "brake_pads")
        run(self.crm.add_order_item(oid, title="wiring", node="wiring", work_type_id=None,
                                    qty=1, price=D(0), parts_cost=D(0), labor_cost=D(0)))
        self.assertFalse([t for t in self.pass_at(date.today(), 10, {})
                          if "дольше срока" in t])

    def test_overdue_digest_is_silent_and_switchable(self):
        self.late_order(1)
        self.assertFalse([t for t in self.pass_at(date.today(), 10, {})
                          if "дольше срока" in t], "все укладываются - молчим")
        self.late_order(9)
        run(self.crm.set_notice("repair_overdue", enabled=False, at_hour=10,
                                at_minute=0, chat_id=None, by="t"))
        self.assertFalse([t for t in self.pass_at(date.today(), 11, {})
                          if "дольше срока" in t])

    def test_parts_digest_goes_on_its_weekday_only(self):
        self.low_part()
        done: dict = {}
        tuesday = TODAY + timedelta(days=1)
        self.assertFalse([t for t in self.pass_at(tuesday, 10, done) if "исходе" in t],
                         "не понедельник")
        self.assertEqual(done["parts_low"], tuesday, "день отмечен - не проверяем снова")
        sent = [t for t in self.pass_at(TODAY, 10, {}) if "исходе" in t]
        self.assertEqual(len(sent), 1)
        self.assertIn("Камера — 0 из 4 шт, не хватает 4", sent[0])
        # владелец перенёс на пятницу
        run(self.crm.set_notice("parts_low", enabled=True, at_hour=9, at_minute=0,
                                chat_id=None, extra={"weekday": 5}, by="t"))
        self.bot.sent.clear()
        self.assertFalse([t for t in self.pass_at(TODAY, 10, {}) if "исходе" in t])
        self.assertTrue([t for t in self.pass_at(date(2026, 9, 25), 10, {})
                         if "исходе" in t])

    def test_manual_pass_sends_both_without_calendar(self):
        self.low_part()
        day = TODAY + timedelta(days=2)                  # среда: не день склада
        self.late_order(9, since=day)
        run(billing.run_daily(self.bot, BotDB(), self.crm, self.cfg, today=day))
        text = "\n".join(t for c, t in self.bot.sent if c == CHAT)
        self.assertIn("исходе", text)
        self.assertIn("дольше срока", text)


# ─────────────────────────── панель ───────────────────────────

@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestNormsInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def open_order(self, days: int, node: str) -> int:
        run(self.crm.update_bike(self.bike_id, status="repair"))
        oid = run(self.crm.create_work_order(
            bike_id=self.bike_id, payer="own", client_id=None, complaint="стук",
            object_note=None, tech_id=None, estimate=D(0), created_by="t"))
        self.crm.orders_[oid]["opened_at"] = datetime.now(UTC) - timedelta(days=days)
        run(self.crm.add_order_item(oid, title="Замена", node=node, work_type_id=None,
                                    qty=1, price=D(0), parts_cost=D(0), labor_cost=D(0)))
        return oid

    def test_norms_are_edited_on_the_work_types_page(self):
        page = self.get_ok("/work-types")
        self.assertIn("Сроки ремонта", page)
        self.assertIn('name="norm_motor_wheel"', page)
        r = self.client.post("/work-types/norms", data={
            "repair_norm_days": "4", "norm_motor_wheel": "6", "norm_brake_pads": "0",
            "norm_frame": ""})
        self.assertEqual(r.status_code, 303)
        self.assertIn("узлов изменено 2", self.get_ok("/work-types"))
        nodes = {n["code"]: n["norm_days"] for n in run(self.crm.repair_nodes())}
        self.assertEqual((nodes["motor_wheel"], nodes["brake_pads"], nodes["frame"]),
                         (6, 0, None))
        self.assertEqual(run(self.crm.settings())["repair_norm_days"], "4")
        r = self.client.post("/work-types/norms", data={"repair_norm_days": "4",
                                                        "norm_motor_wheel": "-2"})
        self.assertIn("Мотор-колесо: Срок", self.get_ok(r.headers["location"]))
        nodes = {n["code"]: n["norm_days"] for n in run(self.crm.repair_nodes())}
        self.assertEqual(nodes["motor_wheel"], 6, "ошибка - ничего не записано")

    def test_overdue_order_is_highlighted_everywhere(self):
        run(self.crm.set_node_norm("controller", 2))
        oid = self.open_order(5, "controller")
        self.assertIn("просрочено на 3 дн.", self.get_ok("/service"))
        page = self.get_ok("/orders?overdue=1")
        self.assertIn("просрочено на 3 дн.", page)
        self.assertIn("РЕМ-000001", page)
        card = self.get_ok(f"/orders/{oid}")
        self.assertIn("срок 2 дн. (Контроллер)", card)
        self.assertIn("просрочено на 3 дн.", card)
        csv = self.client.get("/orders.csv?overdue=1").content.decode("utf-8-sig")
        self.assertIn("Сверх срока", csv.splitlines()[0])
        self.assertIn("РЕМ-000001", csv)
        # срок контроллера длиннее - наряд укладывается и из «дольше срока» уходит
        run(self.crm.set_node_norm("controller", 9))
        self.assertNotIn("РЕМ-000001", self.get_ok("/orders?overdue=1"))
        self.assertNotIn("просрочено", self.get_ok("/service"))

    def test_quick_node_next_to_one_without_deadline(self):
        """Колодки (сутки) и проводка без своего срока, двое суток при общем
        трёх: наряд укладывается везде, а подпись срока - «общий»."""
        run(self.crm.set_node_norm("brake_pads", 1))
        oid = self.open_order(2, "brake_pads")
        run(self.crm.add_order_item(oid, title="Проводка", node="wiring",
                                    work_type_id=None, qty=1, price=D(0),
                                    parts_cost=D(0), labor_cost=D(0)))
        self.assertNotIn("просрочено", self.get_ok("/service"))
        self.assertNotIn("РЕМ-000001", self.get_ok("/orders?overdue=1"))
        card = self.get_ok(f"/orders/{oid}")
        self.assertIn("срок 3 дн. (общий)", card)
        self.assertNotIn("просрочено", card)

    def test_overdue_tile_counts_only_orders_it_lists(self):
        """Велосипед в ремонте без наряда - плитка «без наряда», а не «дольше
        срока»: та ведёт в список нарядов, где его нет."""
        run(self.crm.update_bike(self.bike_id, status="repair"))
        page = self.get_ok("/service")
        self.assertRegex(page, r'href="/orders\?overdue=1"><b>0</b>')
        self.assertIn("Нарядов нет", self.get_ok("/orders?overdue=1"))
        run(self.crm.set_node_norm("controller", 2))
        self.open_order(5, "controller")
        self.assertRegex(self.get_ok("/service"), r'href="/orders\?overdue=1"><b>1</b>')

    def test_viewer_sees_but_does_not_save_norms(self):
        profile = run(self.crm.access_profile_by_code("manager"))
        run(self.crm.create_staff("ivan", logic.hash_password("password-1"), "Иван",
                                  "manager", profile["id"]))
        self.client.post("/logout")
        self.login("ivan", "password-1")
        page = self.get_ok("/work-types")
        self.assertNotIn("Сохранить сроки", page)
        r = self.client.post("/work-types/norms", data={"repair_norm_days": "9"})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("repair_norm_days", run(self.crm.settings()))

    def test_parts_page_warns_about_the_edge_and_transit(self):
        pid = run(self.crm.create_part(title="Колодки", node="brake_pads", unit="шт",
                                       cost=D(300), price=D(600), min_stock=2,
                                       model=None, note=None))
        run(self.crm.add_part_move(part_id=pid, kind="receipt", qty=2, cost=D(300)))
        page = self.get_ok("/parts")
        self.assertIn("на пределе", page)
        self.assertIn("на пределе: неснижаемый 2", self.get_ok(f"/parts/{pid}"))
        # на пределе - тоже потребность, по одной
        self.assertIn("· на пределе", self.get_ok("/part-orders"))
        other = run(self.crm.create_part(title="Камера", node="tube_tire", unit="шт",
                                         cost=D(300), price=D(600), min_stock=3,
                                         model=None, note=None))
        self.client.post("/part-orders/collect")
        order = run(self.crm.open_part_order())
        lines = {i["part_id"]: (i["qty"], i["source"])
                 for i in run(self.crm.part_order_items(order["id"]))}
        self.assertEqual(lines, {pid: (1, "min_stock"), other: (3, "min_stock")})
        self.client.post(f"/part-orders/{order['id']}/status", data={"status": "ordered"})
        page = self.get_ok("/part-orders")
        self.assertIn("уже едет, заказывать не надо", page)
        self.assertIn("в пути 3", page)
        self.assertIn("в пути 3", self.get_ok("/parts"))
        self.assertIn("в пути", self.get_ok(f"/parts/{other}"))

    def test_notice_weekday_is_validated(self):
        page = self.get_ok("/notices")
        self.assertIn("день недели", page)
        self.assertIn("Наряды дольше срока ремонта", page)
        r = self.client.post("/notices/parts_low", data={"enabled": "1", "at_hour": "9",
                                                         "at_minute": "0",
                                                         "weekday": "9"})
        self.assertIn("День недели: целое число от 1 до 7",
                      self.get_ok(r.headers["location"]))
        self.client.post("/notices/parts_low", data={"enabled": "1", "at_hour": "9",
                                                     "at_minute": "0", "weekday": "5"})
        stored = logic.notice_settings(run(self.crm.notices()))["parts_low"]
        self.assertEqual(stored["extra"]["weekday"], 5)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestBatteryPlanInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.model_id = run(self.crm.create_battery_model(
            title="Kugoo 60V", brand=None, voltage=60, capacity=D(21), price=D(16000),
            service_months=15, note=None))
        today = date.today()
        run(self.crm.create_battery(code="АКБ-1", model_id=self.model_id,
                                    purchased_on=logic.add_months(today, -15)))
        run(self.crm.create_battery(code="АКБ-2", model_id=self.model_id,
                                    purchased_on=logic.add_months(today, -13)))
        run(self.crm.create_battery(code="АКБ-3", model_id=self.model_id,
                                    purchased_on=today))

    def test_plan_page_shows_horizons_and_budget(self):
        page = self.get_ok("/batteries/plan")
        self.assertIn("План замены", page)
        self.assertIn("АКБ-1", page)
        self.assertIn("АКБ-2", page)
        self.assertNotIn("АКБ-3", page, "новая - вне полугода")
        self.assertIn("32 000", page, "две Kugoo за квартал по цене каталога")
        self.assertIn(f"/batteries/new?model_id={self.model_id}", page)
        self.assertIn("План замены", self.get_ok("/batteries"))
        csv = self.client.get("/batteries/plan.csv").content.decode("utf-8-sig")
        self.assertIn("Заменить до", csv)
        self.assertIn("АКБ-1", csv)
        self.assertEqual(self.client.get("/batteries/plan.pdf").status_code, 404)

    def test_default_cycles_and_model_cycles(self):
        r = self.client.post("/batteries/plan", data={"battery_max_cycles": "800"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(run(self.crm.settings())["battery_max_cycles"], "800")
        r = self.client.post("/batteries/plan", data={"battery_max_cycles": "0"})
        self.assertIn("Ресурс, циклов", self.get_ok(r.headers["location"]))
        self.client.post(f"/models/batteries/{self.model_id}", data={
            "title": "Kugoo 60V", "price": "16000", "service_months": "15",
            "voltage": "60", "max_cycles": "1200"})
        self.assertEqual(run(self.crm.battery_model(self.model_id))["max_cycles"], 1200)
        self.client.post("/models/batteries", data={"title": "Monster", "price": "0",
                                                    "service_months": "15"})
        monster = next(m for m in run(self.crm.battery_models()) if m["title"] == "Monster")
        self.assertIsNone(monster["max_cycles"], "пусто - общий ресурс")
        battery = run(self.crm.batteries(q="АКБ-1"))[0]
        self.assertIn("из 1200", self.get_ok(f"/batteries/{battery['id']}"))

    def test_new_battery_form_takes_the_model(self):
        page = self.get_ok(f"/batteries/new?model_id={self.model_id}")
        self.assertIn(f'<option value="{self.model_id}" selected>', page)
        self.assertIn('name="purchase_price" value="16000', page)
        self.assertIn("Новая батарея", self.get_ok("/batteries/new?model_id=abc"))


# ─────────────────────────── живой Postgres ───────────────────────────

@unittest.skipUnless(HAVE_PG, "pgserver или asyncpg не установлены")
class TestNormsOnPostgres(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pg = pgserver.get_server(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(self.pg.get_uri(), min_size=1, max_size=3,
                                              init=_init_connection)
        await self.pool.execute("drop schema if exists crm cascade; "
                                "drop schema if exists bot cascade")
        await Database(self.pool).apply_schema(SCHEMA)
        self.crm = CrmDB(self.pool)

    async def asyncTearDown(self):
        await self.pool.close()

    async def test_new_columns_survive_reapply_and_feed_the_queries(self):
        # срок узла и ресурс модели - настройки владельца: повторный старт
        # схемы (сид узлов с on conflict do update) их не трогает
        self.assertTrue(await self.crm.set_node_norm("motor_wheel", 5))
        await self.crm.set_node_norm("brake_pads", 1)
        self.assertFalse(await self.crm.set_node_norm("nope", 1))
        model_id = await self.crm.create_battery_model(
            title="Kugoo", brand=None, voltage=60, capacity=D(21), price=D(16000),
            service_months=15, note=None, max_cycles=900)
        await Database(self.pool).apply_schema(SCHEMA)
        nodes = {n["code"]: n["norm_days"] for n in await self.crm.repair_nodes()}
        self.assertEqual((nodes["motor_wheel"], nodes["brake_pads"], nodes["frame"]),
                         (5, 1, None))
        self.assertEqual((await self.crm.battery_model(model_id))["max_cycles"], 900)
        await self.crm.update_battery_model(model_id, max_cycles=None)
        self.assertIsNone((await self.crm.battery_model(model_id))["max_cycles"])
        await self.crm.update_battery_model(model_id, max_cycles=700)
        battery_id = await self.crm.create_battery(code="1", model_id=model_id)
        self.assertEqual((await self.crm.battery(battery_id))["model_max_cycles"], 700)

        # наряд: самый долгий узел со своим сроком - в той же выборке
        oid = await self.crm.create_work_order(
            bike_id=None, payer="own", client_id=None, complaint=None,
            object_note="самокат", tech_id=None, estimate=D(0), created_by="t")
        self.assertIsNone((await self.crm.work_order(oid))["node_norm"])
        for node in ("brake_pads", "motor_wheel", "wiring"):
            await self.crm.add_order_item(oid, title=node, node=node, work_type_id=None,
                                          qty=1, price=D(0), parts_cost=D(0),
                                          labor_cost=D(0))
        got = await self.crm.work_order(oid)
        self.assertEqual((got["node_norm"], got["norm_node"], got["norm_general"]),
                         (5, "Мотор-колесо", True))
        listed = await self.crm.work_orders(open_only=True)
        self.assertEqual((listed[0]["node_norm"], listed[0]["norm_general"]), (5, True))

        # колодки (сутки) рядом с проводкой без срока и рядом со строкой без
        # узла - наряд меряет общий срок; одни колодки - их сутки
        quick = {}
        for name, nodes in (("pads", ("brake_pads",)), ("wiring", ("brake_pads", "wiring")),
                            ("free", ("brake_pads", None))):
            qid = await self.crm.create_work_order(
                bike_id=None, payer="own", client_id=None, complaint=None,
                object_note=name, tech_id=None, estimate=D(0), created_by="t")
            for node in nodes:
                await self.crm.add_order_item(qid, title="x", node=node, work_type_id=None,
                                              qty=1, price=D(0), parts_cost=D(0),
                                              labor_cost=D(0))
            await self.pool.execute("update crm.work_orders set opened_at = now() - "
                                    "interval '2 days' where id = $1", qid)
            quick[name] = await self.crm.work_order(qid)
        self.assertEqual({k: (o["node_norm"], o["norm_general"]) for k, o in quick.items()},
                         {"pads": (1, False), "wiring": (1, True), "free": (1, True)})
        today = date.today()
        self.assertEqual({k: logic.order_overdue(o, default=3, today=today)
                          for k, o in quick.items()},
                         {"pads": 1, "wiring": 0, "free": 0})
        by_bike = await self.crm.open_orders_by_bike()
        self.assertEqual(by_bike, {}, "наряд без велосипеда - не на рабочем столе")

        # в пути - только строки отправленных заказов
        part_id = await self.crm.create_part(title="Камера", node="tube_tire", unit="шт",
                                             cost=D(300), price=D(600), min_stock=4,
                                             model=None, note=None)
        collected = await service.collect_part_needs(self.crm, by="t")
        self.assertEqual(collected["added"], 1)
        self.assertEqual(await self.crm.parts_in_transit(), {})
        await self.crm.update_part_order(collected["order"]["id"], status="ordered")
        self.assertEqual(await self.crm.parts_in_transit(), {part_id: 4})
        again = await service.collect_part_needs(self.crm, by="t")
        self.assertEqual(again["added"], 0)

        # заглушка считает так же
        fake = FakeCrm()
        await fake.set_node_norm("motor_wheel", 5)
        await fake.set_node_norm("brake_pads", 1)
        foid = await fake.create_work_order(
            bike_id=None, payer="own", client_id=None, complaint=None,
            object_note="самокат", tech_id=None, estimate=D(0), created_by="t")
        for node in ("brake_pads", "motor_wheel", "wiring"):
            await fake.add_order_item(foid, title=node, node=node, work_type_id=None,
                                      qty=1, price=D(0), parts_cost=D(0),
                                      labor_cost=D(0))
        fgot = await fake.work_order(foid)
        self.assertEqual((fgot["node_norm"], fgot["norm_node"], fgot["norm_general"]),
                         (got["node_norm"], got["norm_node"], got["norm_general"]))


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
