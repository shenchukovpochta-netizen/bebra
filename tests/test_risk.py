"""Оценка риска клиента на выдаче: чистое правило таблицей случаев, история
из базы (FakeCrm и живой Postgres - одинаково), значок в мастере выдачи и
в карточке, настройка залога и фильтр списка. Выдачу оценка не запирает -
это проверено отдельно.

Обвязка панели - из tests/test_web.py; Postgres - через pgserver, без него
набор паритета пропускается.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402
from tests.plain import plain  # noqa: E402

try:
    from app.crm import service
    from tests.fake_crm import FakeCrm
    HAVE_SERVICE = True
except ImportError:                                    # pragma: no cover
    HAVE_SERVICE = False

try:
    import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

try:
    import asyncpg
    import pgserver

    from app.crm.db import CrmDB
    from app.db import Database, _init_connection
    HAVE_PG = HAVE_SERVICE
except ImportError:                                    # pragma: no cover
    HAVE_PG = False

D = Decimal
TODAY = date(2026, 9, 27)
MSK = timezone(timedelta(hours=3))
SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"


def ago(n: int, base: date = TODAY) -> date:
    return base - timedelta(days=n)


def texts(risk: dict) -> str:
    return plain(" | ".join(r["text"] for r in risk["reasons"]))


class TestDebtTrack(unittest.TestCase):
    """Просрочка по остатку на конец суток: начисление идёт вперёд целиком,
    и минус до вечера в день начисления - не просрочка."""

    CASES = [
        # (что, суточные суммы, дней, раз, наибольший, долг, суток долга)
        ("пусто", [], 0, 0, 0, 0, 0),
        ("сегодняшнее начисление не просрочка", [(ago(0), -3000)], 0, 0, 0, 3000, 0),
        ("оплата в тот же день", [(ago(9), -3000), (ago(9), 3000)], 0, 0, 0, 0, 0),
        ("оплата через двое суток", [(ago(10), -3000), (ago(8), 3000)], 2, 1, 3000, 0, 0),
        ("две просрочки", [(ago(20), -3000), (ago(19), 3000), (ago(10), -3000),
                           (ago(7), 3000)], 4, 2, 3000, 0, 0),
        ("долг тянется", [(ago(5), -3000)], 5, 1, 3000, 3000, 5),
        ("долг растёт одной просрочкой", [(ago(14), -3000), (ago(7), -3000)],
         14, 1, 6000, 6000, 14),
        ("предоплата покрыла начисление", [(ago(10), 3000), (ago(3), -3000)],
         0, 0, 0, 0, 0),
        ("сегодня закрыл вчерашний долг", [(ago(1), -3000), (ago(0), 3000)],
         1, 1, 3000, 0, 0),
        ("долг сегодня поверх вчерашнего", [(ago(2), -1000), (ago(0), -2000)],
         2, 1, 1000, 3000, 2),
    ]

    def test_cases(self):
        for what, days, n_days, times, top, debt, debt_days in self.CASES:
            with self.subTest(what):
                got = logic.debt_track(days, today=TODAY)
                self.assertEqual((got["overdue_days"], got["overdue_times"],
                                  got["max_debt"], got["debt"], got["debt_days"]),
                                 (n_days, times, D(top), D(debt), debt_days))

    def test_same_day_rows_are_merged(self):
        """Суточные суммы из базы уже сложены, но и две строки одного дня
        считаются одним днём - порядок записей внутри суток неважен."""
        got = logic.debt_track([(ago(4), D(3000)), (ago(4), D(-3000))], today=TODAY)
        self.assertEqual(got["overdue_days"], 0)


class TestClientRisk(unittest.TestCase):
    """Правило целиком: уровень и причины по истории клиента."""

    def risk(self, facts=None, **kw):
        return logic.client_risk(facts, today=TODAY, **kw)

    CASES = [
        # (что, история, уровень, что обязано быть в причинах)
        ("совсем новый", None, "none", "у нас ещё не арендовал"),
        ("первая неделя без просрочки",
         {"rentals": 1, "active": 1, "active_on": ago(10),
          "days": [(ago(10), -3000), (ago(10), 3000)]}, "none", "первая аренда"),
        ("разовая просрочка новичка - не «низкий», а всё ещё нет истории",
         {"rentals": 1, "active": 1, "active_on": ago(12),
          "days": [(ago(12), -3000), (ago(12), 3000), (ago(5), -3000), (ago(2), 3000)]},
         "none", "просрочек: 1, дней в минусе: 3"),
        ("штраф новичка - причиной, без уровня",
         {"rentals": 1, "active": 1, "active_on": ago(12), "fines": 1,
          "fines_sum": D(1500), "days": [(ago(6), -1500), (ago(6), 1500)]},
         "none", "штрафы и ремонт за его счёт: 1"),
        ("новичок, набравший средний, - средний",
         {"rentals": 1, "active": 1, "active_on": ago(20),
          "days": [(ago(20), -3000), (ago(13), 3000)]}, "medium", "дней в минусе: 7"),
        ("чёрный список без истории", {"status": "blacklist"}, "high", "чёрный список"),
        ("заблокирован", {"status": "blocked", "done": 3, "rent_days": 60},
         "high", "заблокирован"),
        ("потеря сильнее доверия",
         {"lost": 1, "done": 8, "rentals": 9, "rent_days": 400}, "high",
         "признан потерянным"),
        ("розыск сейчас", {"search_now": 1, "rentals": 1, "active": 1,
                           "active_on": ago(40)}, "high", "сейчас в розыске"),
        ("надёжный старожил",
         {"done": 6, "rentals": 6, "rent_days": 430}, "low", "в аренде 14 мес."),
        ("идущая аренда - тоже стаж",
         {"done": 2, "rentals": 3, "active": 1, "rent_days": 120, "active_on": ago(70)},
         "low", "в аренде 6 мес."),
        ("одна закрытая аренда - уже история",
         {"done": 1, "rentals": 1, "rent_days": 20}, "low", "не было"),
        ("привычка к просрочкам не прячется за доверием",
         {"done": 4, "rentals": 4, "rent_days": 150,
          "days": [x for k in range(6) for x in ((ago(150 - 20 * k), -3000),
                                                  (ago(148 - 20 * k), 3000))]},
         "medium", "просрочек: 6, дней в минусе: 12"),
        ("ушёл с долгом",
         {"done": 1, "rentals": 1, "rent_days": 30,
          "days": [(ago(30), -3000), (ago(30), 3000), (ago(23), -3000)]},
         "high", "долг сейчас 3 000 ₽, 23 дн."),
        ("долг старожила - не ниже среднего",
         {"done": 7, "rentals": 7, "rent_days": 500,
          "days": [(ago(1), -500)]}, "medium", "долг сейчас 500 ₽, 1 дн."),
        ("сегодняшнее начисление идущей аренды - не долг",
         {"done": 3, "rentals": 4, "active": 1, "rent_days": 190, "active_on": ago(0),
          "days": [(ago(0), -3000)]}, "low", "не было"),
        ("был в розыске и вернул", {"searched": 1, "done": 1, "rentals": 1,
                                   "rent_days": 30}, "medium", "была в розыске"),
        ("штрафы и досрочные возвраты",
         {"done": 3, "early": 2, "fines": 3, "fines_sum": D(4500), "rentals": 3,
          "rent_days": 15,
          "days": [(ago(90), -4500), (ago(90), 4500)]},
         "medium", "штрафы и ремонт за его счёт: 3 на 4 500 ₽"),
    ]

    def test_cases(self):
        for what, facts, level, reason in self.CASES:
            with self.subTest(what):
                got = self.risk(facts)
                self.assertEqual(got["level"], level, texts(got))
                self.assertIn(reason, texts(got))
                self.assertEqual(got["label"], logic.RISK_LEVELS[level])

    def test_no_history_is_not_low_risk(self):
        got = self.risk(None)
        self.assertEqual((got["level"], got["badge"]), ("none", "нет истории"))
        self.assertEqual(self.risk({"done": 1, "rent_days": 10})["badge"],
                         "риск низкий")

    def test_newcomer_minus_never_lowers_the_deposit(self):
        """Минус новичка не делает его «низким риском»: иначе разовая
        просрочка давала бы залог меньше, чем у чистого новичка."""
        deposits = logic.risk_settings({"risk_deposit_none": "2000",
                                        "risk_deposit_medium": "3000"})
        clean = {"rentals": 1, "active": 1, "active_on": ago(12),
                 "days": [(ago(12), -3000), (ago(12), 3000)]}
        late = {**clean, "days": clean["days"] + [(ago(5), -3000), (ago(2), 3000)]}
        fined = {**clean, "fines": 1, "fines_sum": D(1500)}
        got = [self.risk(f, deposits=deposits) for f in (clean, late, fined)]
        self.assertEqual([(r["level"], r["deposit"]) for r in got],
                         [("none", D(2000))] * 3, [texts(r) for r in got])
        self.assertEqual([r["kind"] for r in got[1]["reasons"]], ["note", "bad"],
                         "минус виден причиной под «нет истории»")

    def test_tenure_is_days_in_rentals_not_the_calendar(self):
        """Неделя аренды год назад - не «с нами 13 мес.»: отсутствие
        доверия не копит, и давняя просрочка весит как свежая."""
        def once(n):
            return {"done": 1, "rentals": 1, "rent_days": 7,
                    "days": [(ago(n), -3000), (ago(n - 7), 3000)]}
        old, recent = self.risk(once(400)), self.risk(once(60))
        self.assertEqual((old["level"], old["score"]), (recent["level"], recent["score"]))
        self.assertEqual(old["level"], "medium", texts(old))
        self.assertNotIn("мес.", texts(old))

    def test_referral_by_a_reliable_client_is_a_small_plus(self):
        # Три дня в минусе и штраф: два балла - средний; приглашение надёжного
        # клиента снимает один - низкий. Больше одного балла оно не весит.
        facts = {"done": 1, "rentals": 1, "rent_days": 21, "fines": 1,
                 "fines_sum": D(500),
                 "days": [(ago(60), -3000), (ago(57), 3000), (ago(45), -500),
                          (ago(45), 500)]}
        self.assertEqual(self.risk(facts)["level"], "medium")
        invited = self.risk(facts, agent_good=True)
        self.assertEqual(invited["level"], "low")
        self.assertIn("по приглашению надёжного клиента", texts(invited))

    def test_trust_is_capped(self):
        """Старожил с большой просрочкой: доверие не больше трёх баллов."""
        facts = {"done": 20, "rentals": 20, "rent_days": 800,
                 "days": [(ago(100), -3000), (ago(75), 3000)]}
        got = self.risk(facts)
        # 25 дней в минусе - три балла, доверия набралось бы четыре, в счёт три.
        self.assertEqual(got["score"], 3 - logic.RISK_TRUST_CAP, texts(got))

    def test_plain_reasons_carry_no_rubles(self):
        facts = {"done": 1, "rentals": 1, "rent_days": 30, "fines": 1,
                 "fines_sum": D(700),
                 "days": [(ago(30), -9000), (ago(20), 1000)]}
        got = self.risk(facts)
        self.assertIn("₽", texts(got))
        for reason in got["reasons"]:
            self.assertNotIn("₽", reason["plain"], reason["text"])

    def test_deposit_by_level(self):
        deposits = logic.risk_settings({"risk_deposit_none": "2000",
                                        "risk_deposit_high": "5 000 ₽"})
        self.assertEqual(self.risk(None, deposits=deposits)["deposit"], D(2000))
        self.assertEqual(self.risk({"status": "blacklist"}, deposits=deposits)["deposit"],
                         D(5000))
        self.assertEqual(self.risk({"done": 3, "rent_days": 90},
                                   deposits=deposits)["deposit"], D(0))

    def test_settings_garbage_is_no_deposit(self):
        self.assertEqual(logic.risk_settings(None),
                         dict.fromkeys(logic.RISK_LEVELS, D(0)))
        got = logic.risk_settings({"risk_deposit_low": "abc",
                                   "risk_deposit_medium": "-300",
                                   "risk_deposit_high": "3 000,50"})
        self.assertEqual((got["low"], got["medium"], got["high"]),
                         (D(0), D(0), D("3000.50")))

    def test_rules_follow_the_constants(self):
        rules = dict(logic.risk_rules())
        self.assertIn(logic.money(logic.RISK_DEBT_BIG), " ".join(rules.values()))
        self.assertEqual(len(rules), len(logic.risk_rules()))

    def test_risk_page_is_a_settings_page(self):
        self.assertEqual(logic.section_for("/risk"), "settings")


# ─────────────────── история из базы: один сценарий на обе ───────────────────

def at(n: int, today: date) -> datetime:
    """Полдень по Москве: местная дата одна и та же в UTC и в Москве."""
    return datetime.combine(ago(n, today), datetime.min.time(), tzinfo=MSK) \
        + timedelta(hours=12)


async def build(crm, today: date) -> dict[str, int]:
    """Клиенты на каждую ветку правила - одинаково для FakeCrm и CrmDB."""
    tariff = await crm.create_tariff("Неделя", 7, D(3000), None)
    names = ("потерял", "старожил", "новый", "друг", "должник", "досрочный",
             "чёрный", "был в розыске", "ищем", "в день выдачи", "задним числом",
             "потерял при замене", "замена идёт", "сдал до кражи", "угнали в день выдачи",
             "платил переводом", "давний разовый", "новичок с просрочкой",
             "сменил на месяц", "сменил на неделю")
    ids = {}
    for i, name in enumerate(names):
        ids[name] = await crm.create_client(full_name=name, phone=f"+7999100{i:04d}")
    numbers = iter(range(100))

    async def bike():
        return await crm.create_bike(code=f"R-{next(numbers)}", model="M")

    async def rent(who, started, *, closed=None, bike_id=None, status="available",
                   period=7, switch_to=None):
        rid = await crm.create_rental(
            client_id=ids[who], bike_id=bike_id or await bike(), tariff_id=tariff,
            tariff_name="Неделя", period_days=period, price=D(3000), billing="manual",
            started_on=ago(started, today), contract_no=None, created_by="t")
        if switch_to is not None:
            # Смена тарифа посреди первого срока: срок выдачи помнит триггер.
            await crm.update_rental(rid, period_days=switch_to)
        if closed is not None:
            await crm.close_rental(rid, closed_on=ago(closed, today), note=None,
                                   bike_status=status, closed_by="t")
        return rid

    async def swap_lost(rid):
        """Замена, в которой снятый велосипед отмечен потерянным."""
        await service.swap_bike(crm, await crm.rental(rid), await crm.bike(await bike()),
                                reason="other", old_status="lost", by="t")

    async def money(who, n, amount, kind=None):
        amount = D(amount)
        await crm.add_ledger(client_id=ids[who], amount=amount, created_at=at(n, today),
                             kind=kind or ("payment" if amount > 0 else "charge"))

    rid = await rent("потерял", 40)
    await money("потерял", 40, -3000)
    await money("потерял", 40, 3000)
    await money("потерял", 33, -3000)
    await crm.update_rental(rid, search_at=at(25, today))
    await crm.close_rental(rid, closed_on=today, note="потерян", bike_status="lost",
                           closed_by="t")
    for started, closed in ((400, 380), (200, 150)):
        await rent("старожил", started, closed=closed)
        await money("старожил", started, -3000)
        await money("старожил", started, 3000)
    await rent("друг", 60, closed=40)
    await money("друг", 60, -3000)
    await money("друг", 57, 3000)
    await money("друг", 45, -500, "fine")
    await money("друг", 45, 500)
    ref = await crm.add_referral(agent_id=ids["старожил"], tg_id=777001)
    await crm.update_referral(ref, client_id=ids["друг"], status="signed")
    await rent("должник", 30, closed=16)
    await money("должник", 30, -3000)
    await money("должник", 30, 3000)
    await money("должник", 23, -3000)
    for started in (100, 50):
        await rent("досрочный", started, closed=started - 3)
    await crm.update_client(ids["чёрный"], status="blacklist")
    rid = await rent("был в розыске", 90)
    await crm.update_rental(rid, search_at=at(70, today))
    await crm.close_rental(rid, closed_on=ago(60, today), note=None, closed_by="t")
    rid = await rent("ищем", 30)
    await crm.update_rental(rid, search_at=at(5, today))
    await rent("в день выдачи", 5, closed=5)
    # Закрыта сегодня датой три недели назад: журнал статусов - сегодняшний.
    await rent("задним числом", 40, closed=20, status="lost")
    rid = await rent("потерял при замене", 60)
    await swap_lost(rid)
    await crm.close_rental(rid, closed_on=today, note=None, closed_by="t")
    await swap_lost(await rent("замена идёт", 20))
    # Сдал сегодня, тот же велосипед сегодня же ушёл другому и угнан:
    # чужая потеря того же велосипеда не его.
    shared = await bike()
    await rent("сдал до кражи", 20, closed=0, bike_id=shared)
    await rent("угнали в день выдачи", 0, closed=0, bike_id=shared, status="lost")
    # Платил день в день переводом, а зачислили сегодня: «Я оплатил» и
    # строка выписки датируют платёж днём оплаты, а не зачисления.
    who = ids["платил переводом"]
    await rent("платил переводом", 30, closed=16)
    await money("платил переводом", 30, -3000)
    claim = await crm.create_claim(who, D(3000))
    await backdate_claim(crm, claim, at(30, today))
    await crm.credit_claim(claim, client_id=who, amount=D(3000), method="transfer",
                           note="перевод", created_by="t")
    await money("платил переводом", 23, -3000)
    txn = await crm.save_bank_txn({"txn_id": "B-1", "booked_at": at(23, today),
                                   "amount": D(3000), "direction": "credit"})
    await crm.credit_bank_txn(txn, client_id=who, amount=D(3000), method="transfer",
                              note="выписка", created_by="t")
    # Неделя год назад с недельной просрочкой: давность доверия не даёт.
    await rent("давний разовый", 400, closed=393)
    await money("давний разовый", 400, -3000)
    await money("давний разовый", 393, 3000)
    await rent("новичок с просрочкой", 12)
    for n, amount in ((12, -3000), (12, 3000), (5, -3000), (2, 3000)):
        await money("новичок с просрочкой", n, amount)
    # Неделю перевёл на месяц и сдал ровно в конце оплаченной недели - не
    # досрочно; месяц перевёл на неделю и сдал на десятый день - досрочно.
    await rent("сменил на месяц", 60, closed=53, switch_to=30)
    await rent("сменил на неделю", 60, closed=50, period=30, switch_to=7)
    return ids


async def backdate_claim(crm, claim_id: int, when: datetime) -> None:
    """«Я оплатил» задним числом: у базы такого метода нет и не нужно."""
    if isinstance(crm, FakeCrm):
        crm.claims_[claim_id]["created_at"] = when
    else:
        await crm.pool.execute(
            "update crm.payment_claims set created_at = $2 where id = $1", claim_id, when)


EXPECTED = {"потерял": "high", "старожил": "low", "новый": "none", "друг": "low",
            "должник": "high", "досрочный": "low", "чёрный": "high",
            "был в розыске": "medium", "ищем": "high", "в день выдачи": "none",
            "задним числом": "high", "потерял при замене": "high",
            "замена идёт": "high", "сдал до кражи": "low",
            "угнали в день выдачи": "high", "платил переводом": "low",
            "давний разовый": "medium", "новичок с просрочкой": "none",
            "сменил на месяц": "low", "сменил на неделю": "low"}


def normal(facts: dict, ids: dict[str, int]) -> dict:
    """История без id базы: у FakeCrm и Postgres они разные."""
    names = {v: k for k, v in ids.items()}
    return {names[cid]: {**{k: v for k, v in f.items() if k != "client_id"},
                         "agent_id": names.get(f["agent_id"]),
                         "days": [(d, D(a)) for d, a in f["days"]]}
            for cid, f in facts.items()}


@unittest.skipUnless(HAVE_SERVICE, "зависимости сервиса не установлены")
class TestRiskOnFake(unittest.TestCase):
    def setUp(self):
        self.crm = FakeCrm()
        self.today = date.today()
        self.ids = asyncio.run(build(self.crm, self.today))

    def test_levels_from_history(self):
        got = asyncio.run(service.client_risks(self.crm, None, today=self.today))
        levels = {name: got[cid]["level"] for name, cid in self.ids.items()}
        self.assertEqual(levels, EXPECTED)
        self.assertIn("по приглашению надёжного клиента", texts(got[self.ids["друг"]]))
        self.assertIn("досрочных возвратов: 2", texts(got[self.ids["досрочный"]]))

    def test_facts(self):
        facts = normal(asyncio.run(self.crm.risk_facts()), self.ids)
        self.assertEqual((facts["потерял"]["lost"], facts["потерял"]["done"]), (1, 0))
        self.assertEqual(facts["был в розыске"]["searched"], 1)
        self.assertEqual(facts["ищем"]["search_now"], 1)
        self.assertEqual(facts["досрочный"]["early"], 2)
        self.assertEqual((facts["сменил на месяц"]["early"],
                          facts["сменил на неделю"]["early"]), (0, 1),
                         "досрочно - против срока при выдаче, а не после смены тарифа")
        self.assertEqual(facts["в день выдачи"]["early"], 0,
                         "закрытая в день выдачи - исправление, а не возврат")
        self.assertEqual((facts["в день выдачи"]["done"], facts["в день выдачи"]["rentals"],
                          facts["в день выдачи"]["rent_days"]), (0, 0, 0),
                         "и не история: ошибка выдачи не делает новичка надёжным")
        self.assertEqual((facts["давний разовый"]["rent_days"],
                          facts["ищем"]["active_on"]), (7, ago(30, self.today)),
                         "стаж - дни в арендах, а не календарь с первой")
        self.assertEqual(facts["друг"]["agent_id"], "старожил")
        self.assertEqual((facts["друг"]["fines"], facts["друг"]["fines_sum"]), (1, D(500)))
        self.assertEqual(facts["новый"]["days"], [])

    def test_payment_is_dated_by_its_source(self):
        """Зачислили сегодня, а платил день в день: «Я оплатил» и строка
        выписки ставят платёж на день оплаты - задержка оператора не
        становится просрочкой клиента."""
        facts = normal(asyncio.run(self.crm.risk_facts()), self.ids)["платил переводом"]
        self.assertEqual(facts["days"], [(ago(30, self.today), D(0)),
                                         (ago(23, self.today), D(0))])
        self.assertEqual(facts["balance"], D(0))

    def test_loss_follows_the_bike_in_hand(self):
        """Потеря - у того, у кого велосипед был на руках, а не у того, чьи
        даты аренды случайно накрыли строку журнала."""
        facts = normal(asyncio.run(self.crm.risk_facts()), self.ids)
        got = {name: (facts[name]["lost"], facts[name]["done"])
               for name in ("задним числом", "потерял при замене", "замена идёт",
                            "сдал до кражи", "угнали в день выдачи")}
        self.assertEqual(got, {"задним числом": (1, 0), "потерял при замене": (1, 0),
                               "замена идёт": (1, 0), "сдал до кражи": (0, 1),
                               "угнали в день выдачи": (1, 0)})
        self.assertEqual(facts["угнали в день выдачи"]["rentals"], 1,
                         "потеря в день выдачи - не исправление оператора")

    def test_only_asked_clients(self):
        one = asyncio.run(self.crm.risk_facts([self.ids["новый"]]))
        self.assertEqual(list(one), [self.ids["новый"]])
        self.assertEqual(asyncio.run(service.client_risks(self.crm, [],
                                                          today=self.today)), {})

    def test_found_bike_does_not_clear_the_loss(self):
        """Велосипед нашёлся и вернулся в парк - клиент его всё равно не
        вернул: потеря остаётся в истории."""
        facts = asyncio.run(self.crm.risk_facts([self.ids["потерял"]]))
        rental = next(r for r in self.crm.rentals_.values()
                      if r["client_id"] == self.ids["потерял"])
        asyncio.run(self.crm.update_bike(rental["bike_id"], status="available", by="t"))
        again = asyncio.run(self.crm.risk_facts([self.ids["потерял"]]))
        self.assertEqual(again[self.ids["потерял"]]["lost"],
                         facts[self.ids["потерял"]]["lost"])


@unittest.skipUnless(HAVE_PG, "pgserver или asyncpg не установлены")
class TestRiskOnPostgres(unittest.IsolatedAsyncioTestCase):
    """Паритет: тот же сценарий на живой базе и на FakeCrm даёт ту же
    историю и те же уровни."""

    @classmethod
    def setUpClass(cls):
        # Местная дата журнала - из пояса сессии, а он из TZ процесса.
        cls.tz_before = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Moscow"
        time.tzset()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pg = pgserver.get_server(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()
        cls.tmp.cleanup()
        if cls.tz_before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = cls.tz_before
        time.tzset()

    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(self.pg.get_uri(), min_size=1, max_size=3,
                                              init=_init_connection)
        await self.pool.execute("drop schema if exists crm cascade; "
                                "drop schema if exists bot cascade")
        await Database(self.pool).apply_schema(SCHEMA)
        self.crm = CrmDB(self.pool)

    async def asyncTearDown(self):
        await self.pool.close()

    async def test_same_history_and_levels_as_fake(self):
        today = date.today()
        ids = await build(self.crm, today)
        fake = FakeCrm()
        fake_ids = await build(fake, today)
        real = normal(await self.crm.risk_facts(), ids)
        self.assertEqual(real, normal(await fake.risk_facts(), fake_ids))
        self.assertEqual(real["потерял"]["lost"], 1)
        levels = await service.client_risks(self.crm, None, today=today)
        self.assertEqual({name: levels[cid]["level"] for name, cid in ids.items()},
                         EXPECTED)
        some = await self.crm.risk_facts([ids["друг"], ids["новый"]])
        self.assertEqual(sorted(some), sorted([ids["друг"], ids["новый"]]))


# ─────────────────────────── панель ───────────────────────────

class TestRiskBadgeContrast(unittest.TestCase):
    """Метки читаются в обеих темах: белый на светлом --bad тёмной темы
    был 2,6:1 - «высокий» оператор читал хуже «среднего». Метка - тинт
    (rgba) поверх карточки и цветной текст, поэтому фон считается
    наложением тинта на --card той же темы. Проверяются все цветные
    метки, риск - в их числе."""

    CSS = Path(__file__).resolve().parent.parent / "app/web/static/style.css"

    @staticmethod
    def luminance(rgb: tuple) -> float:
        out = []
        for c in rgb[:3]:
            c /= 255
            out.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]

    @staticmethod
    def color(value: str) -> tuple:
        value = value.strip().replace(" ", "")
        if value.lower() == "#fff":
            value = "#FFFFFF"
        if value.startswith("#"):
            return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5)) + (1.0,)
        r, g, b, a = re.fullmatch(r"rgba\((\d+),(\d+),(\d+),([\d.]+)\)", value).groups()
        return int(r), int(g), int(b), float(a)

    @staticmethod
    def tokens(body: str) -> dict:
        return dict(re.findall(r"(--[\w-]+):([^;]+);", body))

    def themes(self, text: str) -> dict:
        light = self.tokens(re.search(r":root\{([^}]*)\}", text).group(1))
        chosen = re.search(r":root\[data-theme=dark\]\{([^}]*)\}", text).group(1)
        system = re.search(r"prefers-color-scheme:dark\)\{"
                           r":root:not\(\[data-theme=light\]\)\{([^}]*)\}", text).group(1)
        self.assertEqual(self.tokens(chosen), self.tokens(system),
                         "тёмная по системе и тёмная кнопкой - одни значения")
        return {"light": light, "dark": {**light, **self.tokens(chosen)}}

    def resolve(self, tokens: dict, value: str) -> tuple:
        value = value.strip()
        while value.startswith("var("):
            value = tokens[value[4:-1]].strip()
        return self.color(value)

    def test_tags_are_readable_in_both_themes(self):
        text = self.CSS.read_text()
        rules = re.findall(r"((?:\.tag\.[\w-]+,?\s*)+)\{([^}]*)\}", text)
        checked = set()
        for selectors, body in rules:
            props = dict(re.findall(r"(background|color):([^;}]+)", body))
            if "background" not in props or "color" not in props:
                continue
            names = re.findall(r"\.tag\.([\w-]+)", selectors)
            for theme, tokens in self.themes(text).items():
                card = self.resolve(tokens, "var(--card)")
                bg = self.resolve(tokens, props["background"])
                bg = tuple(round(bg[i] * bg[3] + card[i] * (1 - bg[3])) for i in range(3))
                fg = self.resolve(tokens, props["color"])
                a, b = sorted((self.luminance(bg), self.luminance(fg)))
                with self.subTest(tags=names[0], theme=theme):
                    self.assertGreaterEqual((b + 0.05) / (a + 0.05), 4.5, f"{names} {theme}")
            checked.update(names)
        self.assertLessEqual({"risk-low", "risk-medium", "risk-high", "rented", "bad", "ok"},
                             checked)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestRiskWeb(tw.WebCase if HAVE_WEB else unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def lose_a_bike(self, client_id):
        """В истории клиента - велосипед, признанный потерянным."""
        other = tw.run(self.crm.create_bike(code="L-1", model="Kugoo V3"))
        rid = tw.run(self.crm.create_rental(
            client_id=client_id, bike_id=other, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="manual",
            started_on=date.today() - timedelta(days=30), contract_no=None,
            created_by="t"))
        tw.run(service.declare_theft(self.crm, tw.run(self.crm.rental(rid)), note=None,
                                     by="t"))

    def owe(self, client_id, amount=3000, days=12):
        tw.run(self.crm.add_ledger(
            client_id=client_id, kind="fine", amount=-D(amount),
            created_at=datetime.now(MSK) - timedelta(days=days)))

    def step4(self):
        return (f"/issue?client={self.client_id}&tariff={self.tariff_id}"
                f"&model=Kugoo%20V3&bike={self.bike_id}")

    def test_new_client_is_no_history_at_every_step(self):
        page = self.get_ok(f"/issue?client={self.client_id}")
        self.assertIn('class="tag risk-none"', page)
        self.assertIn("у нас ещё не арендовал", page)
        page = self.get_ok(self.step4())
        self.assertIn("<dt>Риск</dt>", page, "в сводке перед подтверждением")
        self.assertIn("нет истории", page)
        self.assertNotIn("рекомендуемый залог", page, "залог не задан - не подсказываем")

    def test_deposit_is_suggested_from_settings(self):
        r = self.client.post("/risk", data={"risk_deposit_none": "2000",
                                            "risk_deposit_high": "5 000"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.crm.settings_["risk_deposit_none"], "2000")
        self.assertEqual(self.crm.settings_["risk_deposit_low"], "0")
        page = self.get_ok(self.step4())
        self.assertIn("рекомендуемый залог <b>2 000 ₽</b>", page)
        page = self.get_ok("/risk")
        self.assertIn('value="5000"', page)
        self.assertIn("Как считается", page)

    def test_bad_deposit_is_refused_whole(self):
        self.client.post("/risk", data={"risk_deposit_none": "1000",
                                        "risk_deposit_high": "много"})
        self.assertNotIn("risk_deposit_none", self.crm.settings_)
        self.assertIn("Залог «высокий»", self.get_ok("/risk"))

    def test_high_risk_does_not_block_issuing(self):
        self.lose_a_bike(self.client_id)
        page = self.get_ok(f"/issue?client={self.client_id}")
        self.assertIn('class="tag risk-high"', page)
        self.assertIn("не вернул велосипед", page)
        self.assertIn("Далее", page, "шаги мастера открыты")
        r = self.client.post("/issue", data={"client_id": self.client_id,
                                             "tariff_id": self.tariff_id,
                                             "bike_id": self.bike_id,
                                             "pay_amount": "3000", "pay_method": "cash",
                                             "mileage": "0"})
        self.assertEqual(r.status_code, 303)
        rental = tw.run(self.crm.active_rental_of(self.client_id))
        self.assertIsNotNone(rental, "оценка - подсказка, выдачу решает оператор")
        self.assertEqual(r.headers["location"], f"/issue/docs?rental={rental['id']}")

    def test_blacklist_keeps_its_own_rule(self):
        tw.run(self.crm.update_client(self.client_id, status="blacklist"))
        page = self.get_ok(f"/issue?client={self.client_id}")
        self.assertIn("Клиент заблокирован или в чёрном списке", page)
        self.assertIn("риск высокий", page)
        self.client.post("/issue", data={"client_id": self.client_id,
                                         "tariff_id": self.tariff_id,
                                         "bike_id": self.bike_id, "pay_amount": "0",
                                         "mileage": "0"})
        self.assertIsNone(tw.run(self.crm.active_rental_of(self.client_id)))

    def test_client_card_shows_reasons_and_hides_rubles_without_finance(self):
        self.owe(self.client_id)
        page = self.get_ok(f"/clients/{self.client_id}")
        self.assertIn("<dt>Риск</dt>", page)
        self.assertIn("долг сейчас 3 000 ₽, 12 дн.", page)
        self.assertIn('href="/risk"', page, "владельцу - как считается")
        perms = {"sections": {"clients": "view"}, "actions": {}}
        pid = tw.run(self.crm.create_access_profile("Только клиенты", perms))
        tw.run(self.crm.create_staff("anna", logic.hash_password("password-1"), "Анна",
                                     "manager", pid))
        self.client.post("/logout")
        self.login("anna", "password-1")
        page = self.get_ok(f"/clients/{self.client_id}")
        self.assertIn("долг сейчас, 12 дн.", page)
        self.assertNotIn("3 000", page)
        self.assertNotIn('href="/risk"', page, "страница правила - раздел настроек")
        self.assertEqual(self.client.get("/risk").status_code, 403)

    def test_clients_filter_and_export(self):
        self.lose_a_bike(self.client_id)
        run = tw.run
        other = run(self.crm.create_client(full_name="Петров Пётр", phone="+79990000002"))
        page = self.get_ok("/clients?risk=high")
        self.assertIn("Иванов Иван", page)
        self.assertNotIn("Петров Пётр", page)
        self.assertIn(f"/clients/{other}", self.get_ok("/clients?risk=none"))
        self.assertIn("Петров Пётр", self.get_ok("/clients?risk=bogus"),
                      "чужое значение - без фильтра")
        body = self.client.get("/clients.csv?risk=high").text
        self.assertIn("Риск", body.splitlines()[0])
        self.assertIn("высокий", body)
        self.assertNotIn("Петров", body)
        self.assertIn("нет истории", self.client.get("/clients.csv").text)
        self.assertIn("/clients.csv?q=&status=&risk=high", page)

    def test_risk_page_counts_clients_by_level(self):
        self.lose_a_bike(self.client_id)
        page = self.get_ok("/risk")
        self.assertIn('href="/clients?risk=high"', page)
        self.assertIn("Риск клиента", self.get_ok("/intake"), "вкладка настроек")


if __name__ == "__main__":
    unittest.main()
