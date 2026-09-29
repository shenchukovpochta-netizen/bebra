"""Выгодность тарифов и что купить следующим: чистая логика и панель.

«Тарифы» сравнивают сроки аренды по чеку и удержанию: деньги - единым
правилом журнала, дни в аренде - по выдаче велосипеда, «Итого» - общие
три числа. «Что купить» ставит модели решение и считает партию: простой
первым, окупаемость - из тех же денег, что отчёт окупаемости, спрос -
сутки без свободной и заявки без велосипеда.
"""

from __future__ import annotations

import csv
import io
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
T0 = date(2026, 9, 1)


def rental(rid, period=7, *, model="M1", issued=False, finished=False, paid=0, days=0,
           renewals=0, used=None, charged=0, debt=0, name=None, price=3000, lost=False,
           changed=False):
    """Строка CrmDB.tariff_rentals: used - сколько суток прожила закрытая."""
    return {"id": rid, "period_days": period, "model": model, "issued": issued,
            "finished": finished, "paid": D(paid), "rented_days": D(days),
            "renewals": renewals, "started_on": T0,
            "closed_on": T0 + timedelta(days=used) if used is not None else None,
            "charged": D(charged), "debt": D(debt), "price": D(price),
            "base_price": D(price), "tariff_name": name or logic.period_title(period),
            "lost": lost, "tariff_changed": changed}


class TestTariffLogic(unittest.TestCase):
    def test_period_titles_and_shares(self):
        self.assertEqual(logic.period_title(7), "Неделя")
        self.assertEqual(logic.period_title(30), "Месяц")
        self.assertEqual(logic.period_title(21), "21 дн.")
        self.assertEqual(logic.percent_of(1, 3), 33.3)
        self.assertIsNone(logic.percent_of(0, 0), "ноль из нуля - нет данных, а не 0 %")

    def test_early_return_counts_whole_periods_from_the_start(self):
        """Сдал в последний день срока или в день следующего платежа - в
        срок; посреди оплаченного - раньше. Суточный раньше не сдают."""
        week = {"started_on": T0, "period_days": 7}
        for used, early in ((0, True), (3, True), (6, False), (7, False), (8, True),
                            (12, True), (13, False), (14, False)):
            row = {**week, "closed_on": T0 + timedelta(days=used)}
            self.assertEqual(logic.early_return(row), early, used)
        self.assertFalse(logic.early_return({**week, "closed_on": None}))
        self.assertFalse(logic.early_return({"started_on": T0, "period_days": 1,
                                             "closed_on": T0 + timedelta(days=2)}))
        self.assertFalse(logic.early_return({**week, "closed_on": T0 - timedelta(days=1)}),
                         "дата закрытия раньше начала - мусор, а не ранняя сдача")

    def test_early_return_by_the_last_charged_term(self):
        """Срок - последнее начисление, начатое до дня сдачи: после смены
        тарифа и у импорта, начислившего три недели одной строкой, границы
        уже не «целые недели от начала»."""
        def closed(used, lo, hi, **extra):
            return logic.early_return({"started_on": T0, "period_days": 7,
                                       "closed_on": T0 + timedelta(days=used),
                                       "term_from": T0 + timedelta(days=lo),
                                       "term_to": T0 + timedelta(days=hi), **extra})
        # Неделя: в сам день платежа начисление дня сдачи в срок не берётся.
        for used, early in ((0, True), (3, True), (6, False), (7, False)):
            self.assertEqual(closed(used, 0, 7), early, used)
        # Неделя, две недели продлений, потом месяц [21, 51): день 51 - день
        # платежа, 50 - последний, 30 и 44 - посреди месяца.
        for used, early in ((51, False), (50, False), (30, True), (44, True)):
            self.assertEqual(closed(used, 21, 51), early, used)
        # Импорт: одна строка на три недели - 13-й день посреди оплаченного.
        self.assertTrue(closed(13, 0, 21))
        self.assertFalse(closed(20, 0, 21))
        self.assertFalse(closed(5, 5, 6), "суточный срок раньше не сдают")
        self.assertFalse(closed(12, 14, 21), "сдал до начала срока - мусор, не ранняя")
        self.assertFalse(closed(3, 0, 7, lost=True), "потерянный не сдан вовсе")

    def story(self):
        weeks = [rental(i, 7, finished=True, used=13, renewals=1, charged=6000,
                        paid=3000, days=20) for i in range(1, 5)]
        weeks += [rental(5, 7, finished=True, used=3, charged=3000, debt=1500, days=4),
                  rental(6, 7, issued=True, paid=3000, days=6)]
        months = [rental(10 + i, 30, finished=True, used=59, renewals=2, charged=27000,
                         paid=9000 if i < 3 else 0, days=25, price=9000)
                  for i in range(5)]
        orphan = {"id": None, "paid": D(700), "rented_days": D(2)}
        return [*weeks, *months, orphan]

    def test_rows_add_up_to_the_park(self):
        """Строки со «без аренды» - ровно деньги и дни парка: чек «Итого»
        - средний чек сводки за тот же период."""
        report = logic.tariff_rows(self.story())
        rows = report["rows"]
        self.assertEqual([r["title"] for r in rows], ["Неделя", "Месяц", "без аренды"])
        week, month, orphan = rows
        self.assertEqual((week["issued"], week["finished"]), (1, 5))
        self.assertEqual(week["renewed_share"], 80.0)
        self.assertEqual(week["avg_renewals"], D("0.8"))
        self.assertEqual(week["avg_days"], D("11.0"))
        self.assertEqual((week["early"], week["early_share"]), (1, 20.0))
        self.assertEqual(week["paid"], D(15000))
        self.assertEqual(week["rented_days"], D(90))
        self.assertEqual(week["avg_check"], D("166.67"))
        self.assertEqual(week["debt"], D(1500))
        self.assertEqual(week["debt_share"], 5.6, "1500 из 27000 начисленного")
        self.assertEqual(week["debtors"], 1)
        self.assertEqual(week["price_per_day"], D("428.57"), "3000 за неделю")
        self.assertEqual(month["renewed_share"], 100.0)
        self.assertEqual(month["avg_check"], D(27000) / 125)
        self.assertEqual(orphan["paid"], D(700))
        total = report["total"]
        self.assertEqual(total["paid"], D(42700))
        self.assertEqual(total["rented_days"], D(90 + 125 + 2))
        self.assertEqual(total["avg_check"],
                         logic.fleet_metrics({"rented": D(217)}, D(42700))["avg_check"])
        self.assertAlmostEqual(sum(r["revenue_share"] for r in rows), 100, delta=0.2)

    def test_best_check_and_best_hold(self):
        report = logic.tariff_rows(self.story())
        self.assertEqual(report["best"]["check"]["title"], "Месяц")
        self.assertEqual(report["best"]["hold"]["title"], "Месяц")
        self.assertIs(report["best"]["both"], report["best"]["check"])
        self.assertTrue(report["rows"][1]["best_check"] and report["rows"][1]["best_hold"])
        self.assertFalse(report["rows"][2]["best_check"], "«без аренды» в спор не идёт")

    def test_hold_is_the_longest_stay_not_the_renewal_share(self):
        """21 день - два продления недели и ни одного у месяца: доля
        продливших выбирала бы неделю, хотя месячные держат вдвое дольше."""
        rows = [*(rental(i, 7, finished=True, used=21, renewals=2) for i in range(10)),
                *(rental(20 + i, 30, finished=True, used=59 if i < 5 else 29,
                         renewals=1 if i < 5 else 0) for i in range(10))]
        report = logic.tariff_rows(rows)
        week, month = report["rows"]
        self.assertEqual((week["avg_days"], week["renewed_share"]), (D(21), 100.0))
        self.assertEqual((month["avg_days"], month["renewed_share"]), (D(44), 50.0))
        self.assertIs(report["best"]["hold"], month)

    def test_zero_is_a_result_not_missing_data(self):
        """Ноль продлений, сдача в день выдачи и чек 0,00 - худшие в споре,
        а не выбывшие из него: иначе «лучший из одного» пропадал ровно
        тогда, когда разница самая большая."""
        rows = [*(rental(i, 7, finished=True, used=13, renewals=1, days=40, paid=0)
                  for i in range(6)),
                *(rental(20 + i, 14, finished=True, used=13, renewals=0, days=4,
                         paid=2000) for i in range(10)),
                *(rental(40 + i, 1, finished=True, used=0) for i in range(5))]
        report = logic.tariff_rows(rows)
        day, week, two = report["rows"]
        self.assertEqual((two["renewed_share"], day["avg_days"]), (0.0, D(0)))
        self.assertEqual(week["avg_check"], D(0))
        self.assertIs(report["best"]["hold"], week, "13 дней поровну - решают продления")
        self.assertIs(report["best"]["check"], two)

    def test_lost_rentals_are_not_returns(self):
        """Признанная потерянной закрыта, но не сдана: её конец - порог
        розыска. В срок, продления и «раньше срока» не идёт, в долг - идёт."""
        rows = [*(rental(i, 7, finished=True, used=13, renewals=1, charged=6000)
                  for i in range(5)),
                *(rental(10 + i, 7, finished=True, used=16, renewals=2, charged=9000,
                         debt=3000, lost=True) for i in range(2))]
        row = logic.tariff_rows(rows)["rows"][0]
        self.assertEqual((row["finished"], row["returned"], row["lost"], row["lost_share"]),
                         (7, 5, 2, 28.6))
        self.assertEqual((row["avg_days"], row["renewed_share"], row["early"]),
                         (D(13), 100.0, 0))
        self.assertEqual((row["debt"], row["debtors"], row["charged"]),
                         (D(6000), 2, D(48000)))
        self.assertFalse(row["few"], "пять сданных - уже сравнимо")

    def test_changed_tariff_name_is_not_the_issue_name(self):
        """Сменённая аренда стоит в строке срока выдачи, а её название -
        уже нового тарифа: подписью строки оно не идёт."""
        rows = [rental(1, 7, issued=True, name="Месяц", changed=True),
                rental(2, 7, issued=True, name="Неделя курьера")]
        self.assertEqual(logic.tariff_rows(rows)["rows"][0]["names"], ["Неделя курьера"])

    def test_small_samples_do_not_win(self):
        """Две закрытые аренды из двух продлённых - это случай: удержание
        сравнивают сроки с пятью закрытыми, чек - с тридцатью днями."""
        rows = [rental(1, 30, finished=True, used=59, renewals=1, paid=20000, days=10),
                rental(2, 30, finished=True, used=59, renewals=1, days=10),
                *(rental(10 + i, 7, finished=True, used=6, paid=2000, days=7)
                  for i in range(5))]
        report = logic.tariff_rows(rows)
        month, week = sorted(report["rows"], key=lambda r: -r["period_days"])
        self.assertTrue(month["few"] and month["thin"])
        self.assertFalse(week["few"] or week["thin"])
        self.assertIsNone(report["best"]["check"], "сравнивать не с чем - лучшего нет")
        self.assertIsNone(report["best"]["hold"])

    def test_by_model_uses_the_catalogue_name(self):
        """Велосипед, записанный по накладной, стоит под клиентским именем:
        иначе одна модель разъехалась бы на две строки."""
        aliases = logic.model_aliases([{"title": "Городской", "factory_title": "M1"}])
        rows = [rental(1, model="M1", issued=True), rental(2, model="Городской", issued=True),
                rental(3, model="M2", issued=True), rental(4, 30, model=None, issued=True)]
        report = logic.tariff_rows(rows, by_model=True, aliases=aliases)
        self.assertEqual([(r["title"], r["issued"]) for r in report["rows"]],
                         [("Неделя · M2", 1), ("Неделя · Городской", 2), ("Месяц · —", 1)])
        self.assertEqual(logic.tariff_rows(rows)["rows"][0]["issued"], 3)

    def test_long_prepaid_term_does_not_win_the_check_in_a_short_window(self):
        """Месяц в окне тридцати дней: оплата вперёд целиком, дни - кусками;
        его чек виден, но в спор не идёт, пока окно не вдвое длиннее."""
        rows = [*(rental(i, 7, paid=3000, days=7) for i in range(1, 6)),
                *(rental(10 + i, 14, paid=5600, days=14) for i in range(5)),
                *(rental(20 + i, 30, paid=9000, days=8) for i in range(5))]
        short = logic.tariff_rows(rows, window_days=31)
        self.assertEqual([r["prepaid"] for r in short["rows"]], [False, False, True])
        self.assertEqual(short["best"]["check"]["title"], "Неделя")
        wide = logic.tariff_rows(rows, window_days=90)
        self.assertEqual(wide["best"]["check"]["title"], "Месяц")

    def test_empty_period(self):
        report = logic.tariff_rows([])
        self.assertEqual(report["rows"], [])
        self.assertIsNone(report["total"]["avg_check"])
        self.assertIsNone(report["best"]["both"])
        # Сироты без денег и дней строкой не показываются.
        self.assertEqual(logic.tariff_rows([{"id": None, "paid": D(0),
                                             "rented_days": D(0)}])["rows"], [])


def day_row(model, day, status, days, location="П"):
    return {"model": model, "location": location, "day": day, "status": status,
            "days": D(days)}


def bike(bid, model, status="available", **over):
    row = {"id": bid, "model": model, "status": status, "location": "П",
           "purchase_price": None, "purchase_id": None, "purchased_on": None,
           "service_months": 24}
    row.update(over)
    return row


class TestBuyLogic(unittest.TestCase):
    def test_zero_free_days_need_a_ride_and_no_free_bike(self):
        d1, d2, d3, today = (T0 + timedelta(days=i) for i in range(4))
        aliases = logic.model_aliases([{"title": "Городской", "factory_title": "M1"}])
        rows = [
            day_row("M1", d1, "rented", 2), day_row("M1", d1, "available", "0.2"),
            # Сутки в ремонте без аренды - поломка, а не спрос.
            day_row("M2", d1, "repair", 1),
            # Два имени одной модели: свободной вместе больше полусуток.
            day_row("M1", d2, "rented", 1), day_row("M1", d2, "available", "0.3"),
            day_row("Городской", d2, "available", "0.3"),
            day_row("M1", d3, "rented", 1, location=None),
            day_row("M1", today, "rented", 3),
        ]
        zero = logic.zero_free_days(rows, before=today, aliases=aliases)
        self.assertEqual(zero, {"Городской": {"П": 1, None: 1}})
        self.assertEqual(logic.zero_free_days(rows, aliases=aliases)["Городской"]["П"], 2,
                         "без before идущие сутки тоже судятся")

    def test_presence_counts_days_in_the_park(self):
        now = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
        rows = [day_row("M1", date(2026, 9, 8), "available", 1),
                day_row("M1", date(2026, 9, 8), "rented", 1),
                day_row("M1", date(2026, 9, 9), "rented", "0.1"),
                day_row("M1", date(2026, 9, 10), "rented", "0.75")]
        self.assertEqual(logic.model_presence(rows, now=now), {"M1": D(2) + D("0.75")})

    def test_last_purchase_price(self):
        purchases = [{"id": 1, "no": "ЗАК-000001", "purchased_on": date(2025, 5, 1)},
                     {"id": 2, "no": "ЗАК-000002", "purchased_on": date(2026, 3, 1)}]
        aliases = logic.model_aliases([{"title": "Городской", "factory_title": "M1"}])
        bikes = [bike(1, "M1", purchase_price=D(40000), purchase_id=1),
                 bike(2, "M1", purchase_price=D(52000), purchase_id=2),
                 bike(3, "Городской", purchase_price=D(54000), purchase_id=2),
                 bike(4, "M2", purchase_price=D(30000), purchased_on=date(2024, 1, 1)),
                 bike(5, "M2", purchase_price=D(33000), purchased_on=date(2025, 1, 1)),
                 bike(6, "M3")]
        prices = logic.last_purchase_prices(bikes, purchases, aliases=aliases)
        self.assertEqual(prices["Городской"], {"price": D(53000), "no": "ЗАК-000002",
                                               "purchased_on": date(2026, 3, 1)})
        self.assertEqual(prices["M2"]["price"], D(33000), "без ЗАК - последняя карточка")
        self.assertIsNone(prices["M2"]["no"])
        self.assertNotIn("M3", prices)

    def test_booking_pressure(self):
        bikes = [bike(1, "M1", location="П"), bike(2, "M1", "rented", location="А")]
        bookings = [{"model": "M1", "status": "new", "location_name": "П"},
                    {"model": "M1", "status": "new", "location_name": "А"},
                    {"model": "M1", "status": "new", "location_name": None},
                    {"model": "M9", "status": "new", "location_name": None},
                    {"model": "M1", "status": "done", "location_name": "А"},
                    {"model": None, "status": "new", "location_name": "А"}]
        self.assertEqual(logic.booking_pressure(bookings, bikes),
                         {"M1": {"open": 3, "unmet": 1}, "M9": {"open": 1, "unmet": 1}})

    def fleet(self):
        """A: всегда в аренде, быстро окупается; B: простой 18 %; C: мало
        дней; D: окупается дольше срока службы; E: парк уже подрос; F: только
        заявки; G: продан весь и не нужен."""
        bikes = [*(bike(i, "A", "rented") for i in range(1, 11)),
                 *(bike(20 + i, "B") for i in range(10)),
                 bike(40, "C"),
                 *(bike(50 + i, "D", "rented") for i in range(5)),
                 *(bike(60 + i, "E", "rented") for i in range(12)),
                 bike(80, "G", "sold")]
        payback = [
            {"model": "A", "paid": D(450000), "works": D(0), "repair_cost": D(9000)},
            {"model": "B", "paid": D(300000), "works": D(0), "repair_cost": D(0)},
            {"model": "C", "paid": D(5000)},
            {"model": "D", "paid": D(9000), "works": D(0), "repair_cost": D(0)},
            {"model": "E", "paid": D(400000), "works": D(0), "repair_cost": D(0)},
            {"model": "G", "paid": D(0)},
        ]
        days = {"A": {"rented": D(855), "available": D(45)},
                "B": {"rented": D(738), "available": D(100), "repair": D(62)},
                "C": {"available": D(20)},
                "D": {"rented": D(430), "available": D(20)},
                "E": {"rented": D(880), "available": D(20)}}
        presence = {"A": D(90), "B": D(90), "C": D(20), "D": D(90), "E": D(90)}
        prices = {"A": {"price": D(50000), "no": "ЗАК-000003"},
                  "D": {"price": D(80000), "no": None}}
        zero = {"A": {"П": 12, None: 2}}
        demand = {"A": {"open": 2, "unmet": 1}, "F": {"open": 2, "unmet": 2}}
        return logic.buy_rows(payback, days=days, zero=zero, prices=prices, demand=demand,
                              bikes=bikes, presence=presence)

    def test_verdicts_and_order(self):
        rows = {r["model"]: r for r in self.fleet()}
        self.assertNotIn("G", rows, "продан весь и спроса нет - не строка")
        a = rows["A"]
        self.assertEqual(a["verdict"], "buy")
        self.assertEqual(a["utilization"], 95.0)
        self.assertEqual(a["idle_percent"], 5.0)
        self.assertEqual(a["revenue_per_day"], D(500))
        self.assertEqual(a["repair_per_day"], D(10))
        # 50000 / (490 * 30.44) = 3.35
        self.assertEqual(a["payback_months"], D("3.4"))
        # спрос 855/90 = 9.5 в аренде; при простое 10 % - 10.56; +1 заявка - 10 в парке
        self.assertEqual(a["count"], 2)
        self.assertEqual(a["zero_days"], 14)
        self.assertEqual(a["zero_points"], [("П", 12), (None, 2)])
        self.assertIn("14 сут. без свободной", a["reason"])
        # Ремонт - треть простоя: причиной он не назван, простой - да.
        self.assertEqual((rows["B"]["verdict"], rows["B"]["reason"]),
                         ("skip", "простой 18.0 %"))
        self.assertEqual(rows["C"]["verdict"], "few")
        self.assertIn("мало данных", rows["C"]["reason"])
        d = rows["D"]
        self.assertEqual(d["verdict"], "skip")
        self.assertIn("дольше срока службы (24 мес.)", d["reason"])
        e = rows["E"]
        self.assertEqual((e["verdict"], e["count"]), ("hold", 0))
        self.assertIn("спрос закрыт", e["reason"])
        f = rows["F"]
        self.assertEqual((f["verdict"], f["reason"]), ("few", "в парке нет; заявок: 2"))
        self.assertEqual([r["verdict"] for r in self.fleet()],
                         ["buy", "hold", "few", "few", "skip", "skip"])

    def test_repair_heavy_idle_says_so(self):
        rows = logic.buy_rows(
            [{"model": "B", "paid": D(1000)}], days={"B": {"rented": D(70), "repair": D(30)}},
            zero={}, prices={}, demand={}, bikes=[bike(1, "B", "repair")],
            presence={"B": D(100)})
        self.assertEqual(rows[0]["reason"], "простой 30.0 %, из них ремонт и ТО 30.0 %")

    def test_repairs_eating_the_revenue(self):
        rows = logic.buy_rows(
            [{"model": "A", "paid": D(1000), "repair_cost": D(5000)}],
            days={"A": {"rented": D(95), "available": D(5)}}, zero={}, prices={},
            demand={}, bikes=[bike(1, "A", "rented")], presence={"A": D(100)})
        self.assertEqual((rows[0]["verdict"], rows[0]["reason"]),
                         ("skip", "не окупается: ремонт съедает выручку"))

    def test_catalogue_merges_two_names(self):
        """Деньги, дни и парк двух названий одной модели - одна строка."""
        aliases = logic.model_aliases([{"title": "Городской", "factory_title": "M1"}])
        rows = logic.buy_rows(
            [{"model": "M1", "paid": D(1000)}, {"model": "Городской", "paid": D(2000)}],
            days={"Городской": {"rented": D(40)}}, zero={}, prices={}, demand={},
            bikes=[bike(1, "M1", "rented"), bike(2, "Городской", "rented")],
            presence={"Городской": D(20)}, aliases=aliases)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["model"], rows[0]["fleet"], rows[0]["names"]),
                         ("Городской", 2, ["M1"]))
        self.assertEqual(rows[0]["revenue_per_day"], D(75))

    def test_plan_without_and_with_budget(self):
        rows = self.fleet()
        plan = logic.buy_plan(rows)
        self.assertEqual(plan["lines"][0], "Следующая партия: A — 2 шт.")
        self.assertTrue(plan["lines"][-1].startswith("Не брать: D — окупится за 131.4 мес."),
                        plan["lines"])
        self.assertIn("; B — простой 18.0 %.", plan["lines"][-1])
        self.assertEqual(plan["count"], 2)
        plan = logic.buy_plan(rows, budget=D(70000))
        self.assertEqual(plan["lines"][0], "На 70 000 ₽: A — 1 шт.; остаток 20 000 ₽.")
        self.assertEqual(plan["lines"][1], "Не влезло в бюджет: A — ещё 1 шт. по 50 000 ₽.")
        self.assertEqual((plan["spent"], plan["left"]), (D(50000), D(20000)))
        plan = logic.buy_plan(rows, budget=D(10000))
        self.assertEqual(plan["taken"], [])
        self.assertIn("Не влезло в бюджет: A — ещё 2 шт. по 50 000 ₽.", plan["lines"])

    def test_plan_with_unknown_price(self):
        rows = logic.buy_rows(
            [{"model": "A", "paid": D(50000)}], days={"A": {"rented": D(100)}}, zero={},
            prices={}, demand={}, bikes=[bike(1, "A", "rented")], presence={"A": D(100)})
        self.assertEqual((rows[0]["verdict"], rows[0]["count"]), ("buy", 1))
        self.assertIn("цена закупки неизвестна", rows[0]["reason"])
        self.assertEqual(logic.buy_plan(rows)["lines"][0], "Следующая партия: A — 1 шт.")
        plan = logic.buy_plan(rows, budget=D(100000))
        self.assertEqual(plan["taken"], [])
        self.assertEqual(plan["lines"], ["Цена закупки неизвестна: A — 1 шт."])

    def test_nothing_to_buy(self):
        self.assertEqual(logic.buy_plan([])["lines"],
                         ["Докупать сейчас нечего: у моделей простой выше цели, спрос "
                          "закрыт парком или данных мало."])


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestTariffAndBuyInPanel(tw.WebCase):
    """Отчёты на заглушке базы: история сдвинута на 40 суток назад, чтобы
    дни в аренде были днями, а не секундами."""

    def setUp(self):
        super().setUp()
        self.login()
        run = tw.run
        self.week = run(self.crm.create_tariff("Неделя", 7, D(3000), None))
        self.month = run(self.crm.create_tariff("Месяц", 30, D(9000), None))
        tw.run(self.crm.create_bike_model(title="Городской", brand=None,
                                          factory_title="Kugoo V3", battery_slots=1,
                                          note=None))
        purchase = run(self.crm.create_purchase(
            supplier_id=None, purchased_on=date.today() - timedelta(days=60), note=None,
            created_by="staff:admin",
            bikes=[{"code": f"K-{i}", "model": "Kugoo V3", "battery_count": 1,
                    "purchase_price": D(50000), "service_months": 24,
                    "residual_price": D(0), "battery_service_months": 15}
                   for i in (1, 2)]))
        self.bikes = [b["id"] for b in run(self.crm.bikes(limit=100))
                      if b.get("purchase_id") == purchase]
        self.idle_bike = run(self.crm.create_bike(code="S-1", model="Kugoo S1"))
        for n, (bike_id, tariff, paid) in enumerate(zip(
                self.bikes, (self.week, self.month), (24000, 16000), strict=True)):
            client = run(self.crm.create_client(full_name=f"Клиент {n}",
                                                phone=f"+7999000010{n}"))
            t = run(self.crm.tariff(tariff))
            rid = run(self.crm.create_rental(
                client_id=client, bike_id=bike_id, tariff_id=tariff,
                tariff_name=t["name"], period_days=t["period_days"], price=t["price"],
                billing="manual", started_on=date.today(), contract_no=None,
                created_by="staff:admin"))
            run(self.crm.add_rental_bike(rid, bike_id=bike_id, issued_on=date.today(),
                                         mileage_start=None, reason="Выдача",
                                         created_by="staff:admin"))
            run(self.crm.charge_period(rid, client, period_from=date.today(),
                                       period_to=date.today() + timedelta(days=7),
                                       amount=-t["price"], note="x"))
            run(self.crm.add_ledger(client_id=client, kind="payment", amount=D(paid),
                                    method="sbp", created_by="staff:admin"))
        self.shift_back(40)

    def shift_back(self, days):
        """Вся история заглушки - на `days` суток назад, как shift_back в
        тестах точек на базе."""
        gap = timedelta(days=days)
        for row in (*self.crm.status_log_, *self.crm.location_log_):
            row["changed_at"] -= gap
        for row in self.crm.ledger_:
            row["created_at"] -= gap
            for key in ("period_from", "period_to"):
                if row.get(key):
                    row[key] -= gap
        for row in self.crm.rentals_.values():
            for key in ("started_on", "closed_on", "billed_until"):
                if row.get(key):
                    row[key] -= gap
        for row in self.crm.rental_bikes_:
            for key in ("issued_on", "returned_on"):
                if row.get(key):
                    row[key] -= gap

    def staff(self, login, profile):
        prof = tw.run(self.crm.access_profile_by_code(profile))
        tw.run(self.crm.create_staff(login, logic.hash_password("password-1"), login,
                                     "manager", prof["id"]))
        self.client.post("/logout")
        self.login(login, "password-1")

    def test_tariffs_page_for_the_owner(self):
        since = (date.today() - timedelta(days=70)).isoformat()
        page = self.get_ok(f"/reports/tariffs?since={since}")
        for text in ("Неделя", "Месяц", "Итого", "Чек/день"):
            self.assertIn(text, page)
        self.assertNotIn("None", page)
        # 24 000 и 16 000 на 40 дней аренды каждому; «Итого» - 40 000 на 80.
        self.assertIn(f"Лучший чек — <b>Неделя</b>: {logic.money(D(600))}", page)
        self.assertIn(logic.money(D(500)), page)
        # 30 дней по умолчанию: платежи 40 суток назад в окно не попали.
        self.assertNotIn("Лучший чек — ", self.get_ok("/reports/tariffs"),
                         "нулевой чек лучшим не бывает")
        # В окне 46 суток месяц в спор о чеке не идёт - спорить не с кем.
        short = (date.today() - timedelta(days=45)).isoformat()
        page = self.get_ok(f"/reports/tariffs?since={short}")
        self.assertNotIn("Лучший чек — ", page)
        self.assertIn("Срок длиннее половины периода", page)
        page = self.get_ok(f"/reports/tariffs?since={since}&by=model")
        self.assertIn("Неделя · Городской", page)
        self.assertIn("by=model", page.split("tariffs.csv")[1][:80])

    def test_period_controls_keep_the_model_split(self):
        """Месяц, «30 дней» и свои даты на «Срок и модель» остаются в этом
        разрезе: переключатели периода знают только адрес страницы."""
        month = date.today().strftime("%Y-%m")
        def period(page):
            """Переключатели периода - от «30 дней» до формы дат."""
            return page[page.rindex("<nav", 0, page.index(">30 дней<")):
                        page.index("</form>")]

        for url in (f"/reports/tariffs/model?month={month}", "/reports/tariffs?by=model"):
            page = self.get_ok(url)
            self.assertIn("Неделя · Городской", page, url)
            controls = period(page)
            self.assertIn('href="/reports/tariffs/model"', controls, "«30 дней»")
            self.assertIn('href="/reports/tariffs/model?month=', controls, url)
            self.assertIn('action="/reports/tariffs/model"', controls, url)
            self.assertNotIn("/reports/tariffs?", controls, url)
        page = self.get_ok(f"/reports/tariffs?month={month}")
        self.assertNotIn("Неделя · Городской", page)
        self.assertIn('action="/reports/tariffs"', period(page))
        self.assertIn(f'href="/reports/tariffs/model?month={month}"', page, "вкладка разреза")

    def test_tariffs_export_and_rights(self):
        r = self.client.get("/reports/tariffs.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Оплачено", r.text)
        self.assertIn("Неделя", r.text)
        self.assertEqual(self.client.get("/reports/tariffs.xlsx").status_code, 200)
        self.assertEqual(self.client.get("/reports/tariffs.pdf").status_code, 404)
        r = self.client.get("/reports/tariffs.csv?by=model")
        self.assertIn("Модель", r.text.splitlines()[0])
        # Механик: отчёт открыт, рубли - нет ни на экране, ни в выгрузке.
        self.staff("petr", "tech")
        page = self.get_ok("/reports/tariffs")
        self.assertIn("Продлили", page)
        self.assertNotIn("Чек/день", page)
        self.assertNotIn("Что купить", page, "вкладка за правом финансов")
        head = self.client.get("/reports/tariffs.csv").text.splitlines()[0]
        self.assertIn("Выдано", head)
        self.assertNotIn("Оплачено", head)
        self.assertNotIn("Чек", head)

    def test_changed_tariff_and_theft_in_the_report(self):
        """Неделю перевели на месяц - аренда осталась в строке недели;
        месячную признали потерянной - она видна отдельно от сданных."""
        week_rental, month_rental = sorted(self.crm.rentals_)
        self.client.post(f"/rentals/{week_rental}/tariff",
                         data={"tariff_id": self.month, "billing": "manual"})
        self.assertEqual((self.crm.rentals_[week_rental]["period_days"],
                          self.crm.rentals_[week_rental]["issue_period_days"]), (30, 7))
        self.client.post(f"/rentals/{month_rental}/search",
                         data={"action": "theft", "note": ""})
        self.assertEqual(self.crm.rentals_[month_rental]["status"], "closed")
        since = (date.today() - timedelta(days=70)).isoformat()
        text = self.client.get(f"/reports/tariffs.csv?since={since}").text
        lines = list(csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=";"))
        rows = {line[0]: dict(zip(lines[0], line, strict=True)) for line in lines[1:]}
        self.assertEqual((rows["Неделя"]["Выдано"], rows["Месяц"]["Выдано"]), ("1", "1"))
        self.assertEqual((rows["Месяц"]["Закрыто"], rows["Месяц"]["Из них потеряно"]),
                         ("1", "1"))
        self.assertEqual(rows["Месяц"]["Сдали раньше срока, %"], "")
        self.assertIn("потеряно 1", self.get_ok(f"/reports/tariffs?since={since}"))

    def test_buy_from_the_report_floor(self):
        """«С» 2000 года открывается и советует то же: суток до первой
        строки журнала нет, и перебирать их незачем."""
        page = self.get_ok(f"/reports/buy?since={logic.REPORT_FLOOR.isoformat()}")
        self.assertIn("Следующая партия: Городской — 1 шт.", page)
        rows = tw.run(self.crm.model_point_days(logic.REPORT_FLOOR, date.today()))
        first = min(r["changed_at"] for r in self.crm.status_log_).astimezone().date()
        self.assertEqual(min(r["day"] for r in rows), first)

    def test_buy_page_recommends_and_counts_the_budget(self):
        page = self.get_ok("/reports/buy")
        self.assertIn("Рекомендация", page)
        self.assertIn("Следующая партия: Городской — 1 шт.", page)
        self.assertIn("Не брать: Kugoo S1 — простой 100.0 %", page)
        self.assertIn("ЗАК-000001", page)
        self.assertNotIn("None", page)
        page = self.get_ok("/reports/buy?budget=120000")
        self.assertIn("На 120 000 ₽: Городской — 1 шт.; остаток 70 000 ₽.", page)
        self.assertIn("budget=120000", page, "бюджет едет в выгрузку")
        page = self.get_ok("/reports/buy?budget=abc")
        self.assertIn("Бюджет не принят. Сумма: число", page)
        self.assertIn("Следующая партия: Городской — 1 шт.", page,
                      "без бюджета - сколько просит спрос")
        r = self.client.get("/reports/buy.csv?budget=120000")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Городской", r.text)
        self.assertIn("брать", r.text)
        self.assertEqual(self.client.get("/reports/buy.xlsx").status_code, 200)
        self.assertEqual(self.client.get("/reports/buy.txt").status_code, 404)

    def test_buy_is_money_only(self):
        self.staff("petr", "tech")
        self.assertEqual(self.client.get("/reports/buy").status_code, 403)
        self.assertEqual(self.client.get("/reports/buy.csv").status_code, 403)


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
