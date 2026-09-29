"""Франшиза без панели: ответ /hook/metrics, проверка чужого ответа,
роялти, опрос франчайзи через настоящий HTTP-сервер на 127.0.0.1 и
запись на живом Postgres.

Главное здесь - граница доверия: ответ франчайзи - чужая строка, и ни
размер, ни вложенность, ни NaN, ни разметка в имени не должны уронить
процесс бота или панель франчайзера. И обратная граница: в ответ
франчайзи не попадает ни одного поля клиента.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import logic  # noqa: E402

try:
    from app.crm import franchise, service
    from app.demo import seed_franchise
    from app.services import franchise as franchise_http
    from app.services.crypto import generate_key
    from tests.fake_crm import FakeCrm
    HAVE_APP = True
except ImportError:                                     # pragma: no cover
    HAVE_APP = False

try:
    from aiohttp import web
    HAVE_AIOHTTP = True
except ImportError:                                     # pragma: no cover
    HAVE_AIOHTTP = False

try:
    import asyncpg
    import pgserver

    from app.crm.db import CrmDB
    from app.db import Database, _init_connection
    HAVE_PG = True
except ImportError:                                     # pragma: no cover
    HAVE_PG = False

D = Decimal
NOW = datetime(2026, 9, 20, 12, 0).astimezone()
SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"
# Поля карточки клиента и аренды: ни одно не должно встретиться в ответе.
PII_KEYS = {"full_name", "phone", "phone2", "phone3", "tg_id", "max_id", "username",
            "passport", "address", "email", "birth", "birthday", "inn", "contract_no",
            "client", "client_id", "clients_list", "note", "anketa", "token"}
ALLOWED_KEYS = {"format", "name", "city", "version", "generated_at", "points", "fleet",
                "rented", "rentals_active", "clients", "last30", "months", "month",
                "partial", "idle_percent", "avg_check", "revenue", "operational_days",
                "rented_days", "idle_days"}


def keys_of(value) -> set[str]:
    """Все ключи ответа на любой глубине."""
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys_of(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys_of(v)}
    return set()


def block(idle="10", rented="90", revenue="45000.00") -> dict:
    op = D(idle) + D(rented)
    return {"idle_percent": 99.9, "avg_check": "1", "revenue": revenue,
            "operational_days": str(op), "rented_days": rented, "idle_days": idle}


def payload(**over) -> dict:
    data = {
        "format": 1, "name": "Май Байк Самара", "city": "Самара", "version": "abc123def456",
        "generated_at": NOW.isoformat(timespec="seconds"),
        "points": [{"name": "Победы", "city": "Самара"}],
        "fleet": 64, "rented": 55, "rentals_active": 54, "clients": 210,
        "last30": block(),
        "months": [{"month": NOW.strftime("%Y-%m"), "partial": True, **block()},
                   {"month": (NOW.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"),
                    "partial": False, **block("20", "80", "40000")}],
    }
    data.update(over)
    return data


def raw(data) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


class TestPayloadContract(unittest.TestCase):
    """Своя сторона: что отдаёт /hook/metrics и что с ним сделает франчайзер."""

    def test_roundtrip_through_our_own_validator(self):
        """Ответ, собранный metrics_payload, проходит parse_metrics, а
        снимок из базы (metrics_json) читается тем же parse_metrics."""
        first = NOW.replace(day=1).date()
        three = logic.fleet_metrics({"available": D("30"), "rented": D("270")}, D("135000"))
        data = logic.metrics_payload(
            title="Прокат", version="v1", now=NOW,
            places=[{"name": "Павлюхина", "city": "Казань", "active": True},
                    {"name": "Закрытая", "city": "Уфа", "active": False}],
            bikes={"available": 3, "rented": 9, "lost": 5, "sold": 1},
            counts={"clients": 40, "rentals": 9}, last30=three,
            months=[{"month": first, "partial": True, **three}])
        self.assertEqual(data["fleet"], 12, "потерянные и проданные - не парк")
        self.assertEqual(data["city"], "Казань")
        self.assertEqual([p["name"] for p in data["points"]], ["Павлюхина"])
        self.assertEqual(data["last30"]["revenue"], "135000.00", "деньги строкой")
        parsed = logic.parse_metrics(json.loads(json.dumps(data)), now=NOW)
        self.assertTrue(parsed.ok, parsed.error)
        again = logic.parse_metrics(logic.metrics_json(parsed.value), now=NOW)
        self.assertEqual(again.value, parsed.value)
        self.assertEqual(parsed.value["last30"]["idle_percent"], 10.0)
        self.assertEqual(parsed.value["last30"]["avg_check"], D("500.00"))

    def test_three_numbers_are_recomputed_not_trusted(self):
        """Готовым процентам и чеку не верим: 99,9 % простоя в ответе при
        днях 10 из 100 - это 10 %, и чек - выручка на день аренды."""
        parsed = logic.parse_metrics(payload(), now=NOW)
        self.assertTrue(parsed.ok, parsed.error)
        self.assertEqual(parsed.value["last30"]["idle_percent"], 10.0)
        self.assertEqual(parsed.value["last30"]["avg_check"], D("500.00"))
        self.assertIsInstance(parsed.value["last30"]["revenue"], Decimal)

    def test_months_are_ordered_and_typed(self):
        parsed = logic.parse_metrics(payload(), now=NOW).value
        months = [m["month"] for m in parsed["months"]]
        self.assertEqual(months, sorted(months, reverse=True))
        self.assertIsInstance(months[0], date)
        rows = logic.metrics_month_rows(parsed)
        self.assertEqual(rows[1]["revenue"], D("40000.00"))
        self.assertEqual(rows[1]["idle_percent"], D("20.0"))

    def test_month_caught_before_its_end_is_partial(self):
        """Снимок 30.09 23:55 не несёт последних минут сентября, что бы ни
        написал франчайзи; снимок 01.10 00:05 - сентябрь целиком."""
        month = {"month": "2026-09", "partial": False, **block()}
        early = datetime(2026, 9, 30, 23, 55).astimezone()
        got = logic.parse_metrics(payload(generated_at=early.isoformat(), months=[month]),
                                  now=early)
        self.assertTrue(got.ok, got.error)
        self.assertTrue(got.value["months"][0]["partial"])
        self.assertTrue(logic.metrics_month_rows(got.value)[0]["partial"], "едет в базу")
        late = datetime(2026, 10, 1, 0, 5).astimezone()
        got = logic.parse_metrics(payload(generated_at=late.isoformat(), months=[
            {**month, "month": "2026-10", "partial": True}, month]), now=late)
        self.assertEqual([m["partial"] for m in got.value["months"]], [True, False])
        self.assertFalse(logic.metrics_month_rows(got.value)[1]["partial"])

    def test_metrics_months_match_the_reports_table(self):
        spans = logic.metrics_months(NOW)
        self.assertEqual(len(spans), logic.METRICS_MONTHS)
        self.assertEqual(spans[0][0], NOW.replace(day=1, hour=0, minute=0, second=0,
                                                  microsecond=0))
        self.assertEqual(spans[0][1], NOW)
        self.assertTrue(spans[0][2])
        self.assertTrue(all(not partial for _, _, partial in spans[1:]))
        for (first, _, _), (_, until, _) in zip(spans, spans[1:], strict=False):
            self.assertEqual(until, first, "месяцы идут встык, без дыр")


class TestHostilePayload(unittest.TestCase):
    """Ответ франчайзи - чужая строка: всё негодное - отказ с причиной,
    а не исключение и не мусор в базе франчайзера."""

    def bad(self, body, fragment=""):
        got = logic.parse_metrics_bytes(body if isinstance(body, bytes) else raw(body),
                                        now=NOW)
        self.assertFalse(got.ok, body if isinstance(body, dict) else body[:80])
        self.assertIn(fragment, got.error)
        return got

    def test_size_encoding_json(self):
        self.bad(b" " * (logic.METRICS_MAX_BYTES + 1), "больше")
        self.bad(b"\xff\xfe\x00{", "UTF-8")
        self.bad(b"<html>login</html>", "не JSON")
        self.bad(b"[" * 100_000 + b"]" * 100_000, "не JSON")
        self.bad(b'{"format": 1, "fleet": NaN}', "не JSON")
        self.bad(b'{"format": 1, "fleet": Infinity}', "не JSON")
        self.bad(b"1" * 5000, "не JSON")
        self.bad(b"[]", "не объект")

    def test_format_and_types(self):
        self.bad(payload(format=2), "формат")
        self.bad(payload(format=True), "формат")
        self.bad(payload(fleet=True), "fleet")
        self.bad(payload(fleet=-1), "fleet")
        self.bad(payload(fleet="64"), "fleet")
        self.bad(payload(clients=10 ** 12), "clients")
        self.bad(payload(name=None), "name")
        self.bad(payload(name={"$gt": ""}), "name")
        self.bad(payload(generated_at="вчера"), "generated_at")
        self.bad(payload(generated_at="2026-09-20T12:00:00"), "пояса")
        self.bad(payload(generated_at=(NOW + timedelta(days=3)).isoformat()), "будущего")
        self.bad({k: v for k, v in payload().items() if k != "last30"}, "last30")

    def test_generated_at_is_bounded(self):
        """Год 0001 с поясом +14:00 в UTC уходит в год 0: принятый, он ронял
        карточку франчайзи (OverflowError в шаблоне) на каждом открытии.
        Отказ - и с now, и без него: снимок из базы читается без now."""
        for moment in ("0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00",
                       "1970-01-01T00:00:00+00:00"):
            self.bad(payload(generated_at=moment, months=[]), "generated_at")
            self.assertFalse(logic.parse_metrics(payload(generated_at=moment, months=[]))
                             .ok, moment)
        # Старый ответ (сбитые часы, чужой кэш) затёр бы свежие месяцы.
        self.bad(payload(generated_at=(NOW - timedelta(days=3)).isoformat()), "старше суток")
        self.assertTrue(logic.parse_metrics(
            payload(generated_at=(NOW - timedelta(hours=20)).isoformat()), now=NOW).ok)

    def test_huge_exponent_is_not_json(self):
        """decimal.InvalidOperation - не ValueError: без него «Обновить
        сейчас» отвечала 500, а причина в карточку не ложилась."""
        self.bad(b'{"format": 1, "fleet": 1e99999999999999999999}', "не JSON")
        self.bad(raw(payload()).replace(b'"45000.00"', b"1e99999999999999999999", 1),
                 "не JSON")

    def test_numbers(self):
        for value in ("NaN", "Infinity", "-5", "1e999", "12abc", "", True, [1], {"a": 1},
                      "9" * 60):
            self.bad(payload(last30={**block(), "revenue": value}), "revenue")
        self.bad(payload(last30={**block(), "rented_days": "1000"}), "не сходятся")
        self.bad(payload(last30={**block(), "idle_days": "-1"}), "idle_days")

    def test_lists(self):
        self.bad(payload(points="Победы"), "points")
        self.bad(payload(points=[{"name": "x"}] * (logic.METRICS_POINTS_LIMIT + 1)),
                 "points")
        self.bad(payload(points=["x"]), "points")
        self.bad(payload(points=[{"name": "   "}]), "points.name")
        month = {"partial": False, **block()}
        self.bad(payload(months=[{**month, "month": "2026-13"}]), "ГГГГ-ММ")
        self.bad(payload(months=[{**month, "month": "2026-9"}]), "ГГГГ-ММ")
        self.bad(payload(months=[{**month, "month": "2099-01"}]), "позже")
        self.bad(payload(months=[{**month, "month": "2026-08"}] * 2), "дважды")
        self.bad(payload(months=[{**month, "month": "2026-08"}] * 30), "больше")

    def test_strings_are_cleaned_not_trusted(self):
        """Управляющие и невидимые символы - вон, длина обрезана. Разметку
        не трогаем: её экранирует шаблон (tests/test_franchise_web.py)."""
        got = logic.parse_metrics(payload(
            name="Май‮Байк\x00​\n<script>alert(1)</script>" + "я" * 500,
            points=[{"name": "\ud800Победы\x07", "city": "⁦Самара"}]), now=NOW)
        self.assertTrue(got.ok, got.error)
        name = got.value["name"]
        self.assertEqual(len(name), logic.METRICS_TEXT_LIMIT)
        self.assertTrue(name.startswith("МайБайк <script>"))
        self.assertFalse(any(ch in name for ch in "‮\x00​\n"))
        self.assertEqual(got.value["points"], [{"name": "Победы", "city": "Самара"}])
        # Такая строка уезжает в jsonb без ошибок кодека.
        json.dumps(logic.metrics_json(got.value), ensure_ascii=False).encode("utf-8")

    def test_unknown_keys_are_dropped(self):
        got = logic.parse_metrics(payload(evil={"x": 1}, clients_list=["Иванов"]), now=NOW)
        self.assertTrue(got.ok)
        self.assertNotIn("evil", got.value)
        self.assertNotIn("clients_list", logic.metrics_json(got.value))
        self.assertIsNone(logic.parse_metrics(payload(version="<b>1</b>"), now=NOW)
                          .value["version"])


class TestRoyalty(unittest.TestCase):
    """Роялти - деньги: только Decimal, копейка по правилу журнала, условия
    месяца - записанные на месяц, а не сегодняшние."""

    def test_royalty_math(self):
        self.assertEqual(logic.royalty(D("123456.78"), D("5.5"), D("5000")), D("11790.12"))
        self.assertEqual(logic.royalty(D("0.10"), D("5"), D("0")), D("0.01"),
                         "половина копейки - вверх")
        self.assertEqual(logic.royalty(D("0"), D("5"), D("15000")), D("15000.00"))
        got = logic.royalty("100000", "4.5", "0")
        self.assertIsInstance(got, Decimal)
        self.assertEqual(got, D("4500.00"))

    def franchisee(self, **over):
        return {"id": 1, "name": "Самара", "city": "Самара", "active": True,
                "contract_start": date(2026, 7, 15), "token_enc": "x", **over}

    def month(self, month, revenue, percent="5", fee="1000", fid=1):
        return {"franchisee_id": fid, "month": month, "revenue": D(revenue),
                "royalty_percent": D(percent), "fixed_fee": D(fee)}

    def test_rows_by_month(self):
        months = [self.month(date(2026, 9, 1), "10000", "6", "2000"),
                  self.month(date(2026, 8, 1), "100000"),
                  self.month(date(2026, 6, 1), "90000")]
        blocks = logic.royalty_rows([self.franchisee()], months, today=date(2026, 9, 20),
                                    count=4)
        by = {b["month"]: b for b in blocks}
        self.assertEqual([b["month"] for b in blocks],
                         [date(2026, 9, 1), date(2026, 8, 1), date(2026, 7, 1),
                          date(2026, 6, 1)])
        sep = by[date(2026, 9, 1)]["rows"][0]
        self.assertEqual((sep["royalty"], sep["mark"]), (D("2600.00"), "идёт"))
        aug = by[date(2026, 8, 1)]["rows"][0]
        self.assertEqual((aug["royalty"], aug["mark"]), (D("6000.00"), ""),
                         "условия - записанные на август, а не сентябрьские")
        jul = by[date(2026, 7, 1)]
        self.assertEqual((jul["rows"][0]["mark"], jul["missing"]), ("нет данных", 1),
                         "месяц начала договора без цифр виден, а не пропал")
        self.assertIsNone(jul["rows"][0]["royalty"])
        jun = by[date(2026, 6, 1)]["rows"][0]
        self.assertEqual((jun["royalty"], jun["mark"]), (D("0.00"), "до договора"))
        self.assertEqual(by[date(2026, 8, 1)]["royalty"], D("6000.00"))

    def test_past_month_with_partial_data_is_marked(self):
        """Опрос с 1-го не проходит, а последний ответ застал октябрь до
        конца: 3 ноября счёт по нему занизил бы роялти молча."""
        months = [{**self.month(date(2026, 10, 1), "100000"), "partial": True},
                  {**self.month(date(2026, 11, 1), "5000"), "partial": True}]
        blocks = logic.royalty_rows([self.franchisee()], months, today=date(2026, 11, 3),
                                    count=2)
        nov, octo = blocks
        self.assertEqual(nov["rows"][0]["mark"], "идёт")
        self.assertEqual(octo["rows"][0]["mark"], logic.ROYALTY_PARTIAL)
        self.assertEqual((octo["partial"], nov["partial"]), (1, 0))
        # Следующий удачный ответ (после конца месяца) снимает отметку.
        months[0]["partial"] = False
        octo = logic.royalty_rows([self.franchisee()], months, today=date(2026, 11, 3),
                                  count=2)[1]
        self.assertEqual((octo["rows"][0]["mark"], octo["partial"]), ("", 0))

    def test_tile_and_report_count_royalty_alike(self):
        """Снятый с договора франчайзи с цифрами за прошлый месяц - в счёт и
        в отчёте, и на плитке сравнения: суммы за один месяц не расходятся."""
        now = datetime(2026, 9, 20, 12, 0).astimezone()
        people = [self.franchisee(id=1), self.franchisee(id=2, name="Уфа", active=False)]
        months = [self.month(date(2026, 8, 1), "100000"),
                  self.month(date(2026, 8, 1), "40000", fid=2)]
        tile = logic.franchise_rows(people, months, now=now)["total"]
        report = logic.royalty_rows(people, months, today=now.date(), count=2)[1]
        self.assertEqual(tile["royalty"], report["royalty"])
        self.assertEqual(tile["royalty"], D("6000.00") + D("3000.00"))
        self.assertEqual(tile["last"], report["revenue"])

    def test_terms_from(self):
        today = date(2026, 10, 2)
        check = logic.check_terms_from
        self.assertEqual(check("", today=today).value, date(2026, 10, 1), "пусто - идущий")
        self.assertEqual(check("2026-09", today=today).value, date(2026, 9, 1))
        self.assertEqual(check("09.2026", today=today).value, date(2026, 9, 1))
        for bad in ("2026-11", "2027-01", "1999-12", "2026-13", "сентябрь", "2026-9"):
            self.assertFalse(check(bad, today=today).ok, bad)

    def test_inactive_without_data_is_not_listed(self):
        blocks = logic.royalty_rows([self.franchisee(active=False)], [],
                                    today=date(2026, 9, 20), count=2)
        self.assertEqual([b["rows"] for b in blocks], [[], []])

    def test_comparison_and_network_total(self):
        """Итог сети - по суммам дней и денег, а не среднее процентов."""
        now = datetime(2026, 9, 20, 12, 0).astimezone()
        snap_a = logic.metrics_json(logic.parse_metrics(payload(
            last30=block("10", "90", "45000")), now=now).value)
        snap_b = logic.metrics_json(logic.parse_metrics(payload(
            fleet=10, last30=block("30", "10", "6000")), now=now).value)
        rows = [self.franchisee(id=1, data=snap_a, ok_at=now - timedelta(hours=2)),
                self.franchisee(id=2, name="Уфа", data=snap_b,
                                ok_at=now - timedelta(hours=50), error="таймаут"),
                self.franchisee(id=3, name="Пенза", data=None, ok_at=None)]
        months = [self.month(date(2026, 8, 1), "100000"),
                  self.month(date(2026, 7, 1), "80000"),
                  self.month(date(2026, 8, 1), "30000", fid=2)]
        got = logic.franchise_rows(rows, months, now=now, version="abc123def456")
        a, b, c = got["rows"]
        self.assertFalse(a["stale"])
        self.assertTrue(b["stale"])
        self.assertTrue(c["stale"], "ни одного ответа - тоже нет свежих данных")
        self.assertEqual(got["stale"], 2)
        self.assertEqual(a["trend"], D("25.0"))
        self.assertIsNone(b["trend"], "без позапрошлого месяца тренда нет")
        self.assertEqual(a["royalty_last"], D("6000.00"))
        self.assertTrue(a["same_version"])
        total = got["total"]
        self.assertEqual(total["fleet"], 74)
        # (10 + 30) / (100 + 40) = 28,6 %, а не среднее 10 % и 75 %
        self.assertEqual(total["idle_percent"], 28.6)
        self.assertEqual(total["avg_check"], D("510.00"))
        self.assertEqual(total["royalty"], D("6000.00") + D("2500.00"))
        self.assertEqual(total["last"], D("130000.00"))
        self.assertEqual(total["trend"], D("25.0"),
                         "тренд сети - по сопоставимым: Уфа без июля в него не входит")

    def test_due_and_stale(self):
        now = datetime(2026, 9, 20, 12, 0).astimezone()
        row = self.franchisee()
        self.assertTrue(logic.franchise_due({**row, "ok_at": None, "polled_at": None}, now))
        self.assertFalse(logic.franchise_due({**row, "ok_at": now - timedelta(hours=5)}, now),
                         "сегодня уже принят - до завтра")
        self.assertTrue(logic.franchise_due(
            {**row, "ok_at": now - timedelta(days=1), "polled_at": now - timedelta(days=1)},
            now))
        self.assertFalse(logic.franchise_due(
            {**row, "ok_at": None, "polled_at": now - timedelta(minutes=20)}, now),
            "неудача - не чаще раза в час")
        self.assertTrue(logic.franchise_due(
            {**row, "ok_at": None, "polled_at": now - timedelta(minutes=61)}, now))
        self.assertFalse(logic.franchise_due({**row, "active": False}, now))
        self.assertFalse(logic.franchise_due({**row, "token_enc": None}, now))
        self.assertFalse(logic.franchise_stale({**row, "active": False, "ok_at": None}, now))
        self.assertIn("2 из 5", logic.franchise_stale_text(2, 5))


class TestForms(unittest.TestCase):
    def test_base_url(self):
        ok = logic.check_base_url
        self.assertEqual(ok("https://crm.samara.example/").value, "https://crm.samara.example")
        self.assertTrue(ok("https://crm.example:8443/panel").ok)
        self.assertTrue(ok("http://127.0.0.1:8080").ok, "http - только на свою машину")
        self.assertTrue(ok("http://localhost:9000").ok)
        for bad in ("", "crm.example", "http://crm.example", "ftp://crm.example",
                    "javascript:alert(1)", "https://user:pw@crm.example",
                    "https://crm.example/?x=1", "https://crm.example/#a",
                    "https://crm.example\\@evil.example", "https://срм.рф",
                    "https://crm.example:99999", "https://crm .example",
                    "https://" + "a" * 300 + ".example"):
            self.assertFalse(ok(bad).ok, bad)
        self.assertEqual(logic.metrics_url("https://crm.example/"),
                         "https://crm.example/hook/metrics")

    def test_franchisee_form(self):
        form = {"name": "Самара", "city": "", "base_url": "https://crm.samara.example",
                "royalty_percent": "5,5", "fixed_fee": "15 000", "contract_start": "2026-07-15",
                "active": "1"}
        got = logic.check_franchisee(form)
        self.assertTrue(got.ok, got.error)
        self.assertEqual((got.value["royalty_percent"], got.value["fixed_fee"]),
                         (D("5.50"), D("15000.00")))
        self.assertIsNone(got.value["city"])
        self.assertTrue(got.value["active"])
        for key, value in (("royalty_percent", "101"), ("royalty_percent", "-1"),
                           ("royalty_percent", "пять"), ("fixed_fee", "-5"),
                           ("contract_start", ""), ("base_url", "http://evil.example"),
                           ("name", ""), ("name", "=HYPERLINK(1)")):
            self.assertFalse(logic.check_franchisee({**form, key: value}).ok, (key, value))
        self.assertFalse(logic.check_franchisee({**form, "active": ""}).value["active"])

    def test_token(self):
        self.assertIsNone(logic.check_franchise_token("  ").value)
        self.assertTrue(logic.check_franchise_token("a" * 64).ok)
        for bad in ("short", "a" * 16 + " b", "т" * 20, "a" * 201):
            self.assertFalse(logic.check_franchise_token(bad).ok, bad)


@unittest.skipUnless(HAVE_APP, "нет зависимостей приложения")
class TestServiceSide(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.crm = FakeCrm()
        self.vault = service.franchise_vault(generate_key())

    async def seeded(self):
        """Клиенты с именами и телефонами, парк, платежи - и ни одно из
        этих значений не должно уехать в ответ /hook/metrics."""
        await self.crm.create_location(name="Павлюхина", city="Казань", address="ул. Секретная 1",
                                       note=None)
        for i in range(3):
            cid = await self.crm.create_client(full_name=f"Секретов Иван {i}",
                                               phone=f"+7999123456{i}", tg_id=900 + i)
            await self.crm.add_ledger(client_id=cid, kind="payment", amount=D("3500"),
                                      note="паспорт 9200 123456")
        for i, status in enumerate(("rented", "rented", "available", "lost")):
            await self.crm.create_bike(code=f"МБ-{i}", model="Kugoo", status=status)
        for entry in self.crm.status_log_:
            entry["changed_at"] -= timedelta(days=40)
        for entry in self.crm.ledger_:
            entry["created_at"] -= timedelta(days=3)

    async def test_metrics_have_no_personal_data(self):
        await self.seeded()
        data = await service.franchise_metrics(self.crm, title="МАЙБАЙК", version="v",
                                               now=datetime.now().astimezone())
        text = json.dumps(data, ensure_ascii=False)
        self.assertEqual(keys_of(data) & PII_KEYS, set())
        self.assertLessEqual(keys_of(data), ALLOWED_KEYS)
        for secret in ("Секретов", "+79991234560", "900", "Секретная", "9200 123456"):
            self.assertNotIn(secret, text)
        self.assertEqual((data["fleet"], data["rented"], data["clients"]), (3, 2, 3))
        self.assertEqual(data["last30"]["revenue"], "10500.00")
        self.assertEqual(data["points"], [{"name": "Павлюхина", "city": "Казань"}])
        self.assertEqual(len(data["months"]), logic.METRICS_MONTHS)
        # Свой ответ проходит свою же проверку франчайзера.
        parsed = logic.parse_metrics_bytes(raw(data))
        self.assertTrue(parsed.ok, parsed.error)
        self.assertAlmostEqual(parsed.value["last30"]["idle_percent"], 33.3, places=1)

    async def test_token_is_stored_encrypted(self):
        fields = logic.check_franchisee({
            "name": "Самара", "base_url": "https://crm.samara.example",
            "royalty_percent": "5", "fixed_fee": "0", "contract_start": "2026-07-01",
            "active": "1"}).value
        with self.assertRaises(service.ServiceError):
            await service.save_franchisee(self.crm, None, None, fields, token="t" * 32)
        with self.assertRaises(service.ServiceError):
            await service.save_franchisee(self.crm, self.vault, None, fields, token=None)
        fid = await service.save_franchisee(self.crm, self.vault, None, fields,
                                            token="secret-token-" + "x" * 20)
        row = await self.crm.franchisee(fid)
        self.assertNotIn("secret-token", row["token_enc"])
        self.assertEqual(service.franchise_token(self.vault, row),
                         "secret-token-" + "x" * 20)
        # Пустой токен при правке - прежний остаётся.
        await service.save_franchisee(self.crm, self.vault, fid, {**fields, "city": "Самара"},
                                      token=None)
        self.assertEqual(service.franchise_token(self.vault, await self.crm.franchisee(fid)),
                         "secret-token-" + "x" * 20)
        # Чужой ключ - None, а не исключение.
        other = service.franchise_vault(generate_key())
        self.assertIsNone(service.franchise_token(other, row))

    async def test_rates_follow_the_month(self):
        """Правка процента ложится на текущий месяц, прошлый - как был."""
        fid = await self.crm.create_franchisee(
            name="Самара", base_url="https://crm.samara.example", token_enc="x",
            royalty_percent=D("5"), fixed_fee=D("1000"), contract_start=date(2026, 1, 1))
        today = date.today()
        cur = today.replace(day=1)
        prev = (cur - timedelta(days=1)).replace(day=1)
        rows = [{"month": m, "revenue": D("100"), "idle_percent": None, "avg_check": None,
                 "operational_days": D(0), "rented_days": D(0)} for m in (cur, prev)]
        await self.crm.save_franchise_snapshot(fid, data={}, months=rows, taken_on=today)
        await self.crm.update_franchisee(fid, royalty_percent=D("7"))
        await self.crm.save_franchise_snapshot(fid, data={}, months=rows, taken_on=today)
        got = {m["month"]: m["royalty_percent"] for m in await self.crm.franchise_months(prev)}
        self.assertEqual(got, {cur: D("7"), prev: D("5")})
        self.assertFalse(await self.crm.delete_franchisee(fid), "история роялти держит")
        # Опечатка, замеченная после первого опроса: явно - и на прошлый.
        fields = {"royalty_percent": D("8"), "fixed_fee": D("1000")}
        await service.save_franchisee(self.crm, self.vault, fid, fields, token=None,
                                      terms_from=prev)
        got = {m["month"]: m["royalty_percent"] for m in await self.crm.franchise_months(prev)}
        self.assertEqual(got, {cur: D("8"), prev: D("8")})
        # Дубль, заведённый по ошибке, стирается только явно - с месяцами.
        self.assertTrue(await self.crm.delete_franchisee(fid, wipe=True))
        self.assertEqual(await self.crm.franchise_months(prev), [])

    async def test_report_stale_is_silent_or_nameless(self):
        bot = mock.AsyncMock()
        cfg = mock.Mock(contract_chat_id="-100")
        self.assertEqual(await franchise.report_stale(bot, self.crm, cfg, now=datetime.now()),
                         0)
        await self.crm.create_franchisee(name="Секретная Самара", base_url="https://a.example",
                                         token_enc="x", contract_start=date(2026, 1, 1))
        self.assertEqual(await franchise.report_stale(bot, self.crm, cfg, now=datetime.now()),
                         1)
        text = bot.send_message.await_args.args[1]
        self.assertIn("1 из 1", text)
        self.assertNotIn("Секретная", text)

    def test_demo_franchisees_pass_the_check(self):
        """Демо собирает снимки тем же metrics_payload и той же проверкой."""
        for spec in seed_franchise.FRANCHISEES:
            got = seed_franchise.build(spec, now=datetime.now().astimezone(), version="v")
            self.assertGreater(got["fleet"], 0)
            self.assertTrue(got["months"])
        key = seed_franchise.franchise_key_text("s")
        self.assertIsNotNone(service.franchise_vault(key))
        self.assertNotEqual(key, seed_franchise.franchise_key_text("t"))


@unittest.skipUnless(HAVE_APP and HAVE_AIOHTTP, "нет aiohttp")
class TestPollerWithServer(unittest.IsolatedAsyncioTestCase):
    """Опрос через настоящий HTTP на 127.0.0.1: адрес, токен, пределы."""

    TOKEN = "metrics-token-" + "z" * 24

    async def asyncSetUp(self):
        self.crm = FakeCrm()
        self.vault = service.franchise_vault(generate_key())
        self.hits: list[str] = []
        app = web.Application()
        app.router.add_get("/{name}/hook/metrics", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = self.runner.addresses[0][1]

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def handle(self, request):
        name = request.match_info["name"]
        self.hits.append(name)
        if request.headers.get("Authorization") != f"Bearer {self.TOKEN}":
            return web.json_response({"ok": False}, status=401)
        if name == "good":
            return web.json_response(payload(generated_at=datetime.now().astimezone()
                                              .isoformat(timespec="seconds")))
        if name == "huge":
            return web.Response(body=b"{" + b" " * (logic.METRICS_MAX_BYTES + 10) + b"}",
                                content_type="application/json")
        if name == "stream":
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            for _ in range(40):
                await response.write(b" " * 16384)
            return response
        if name == "slow":
            await asyncio.sleep(3)
            return web.json_response({})
        if name == "redirect":
            raise web.HTTPFound("http://127.0.0.1:1/steal")
        if name == "html":
            return web.Response(text="<html>вход</html>", content_type="text/html")
        if name == "evil":
            return web.json_response(payload(fleet=True))
        return web.json_response({}, status=500)

    async def add(self, name, *, token=None, **over):
        fields = {"name": name, "base_url": f"http://127.0.0.1:{self.port}/{name}",
                  "royalty_percent": D("5"), "fixed_fee": D("0"),
                  "contract_start": date(2026, 1, 1), "active": True}
        fields.update(over)
        fid = await service.save_franchisee(self.crm, self.vault, None, fields,
                                            token=token or self.TOKEN)
        return await self.crm.franchisee(fid)

    async def test_good_answer_is_stored(self):
        row = await self.add("good")
        self.assertIsNone(await franchise.refresh_one(self.crm, self.vault, row))
        row = await self.crm.franchisee(row["id"])
        self.assertIsNotNone(row["ok_at"])
        self.assertIsNone(row["error"])
        self.assertEqual(row["data"]["fleet"], 64)
        self.assertEqual(len(self.crm.franchise_months_), 2)
        self.assertEqual({m["royalty_percent"] for m in self.crm.franchise_months_.values()},
                         {D("5")})

    async def test_failures_are_explained_and_keep_old_data(self):
        cases = {"huge": "больше", "stream": "больше", "redirect": "переадресация",
                 "html": "не JSON", "evil": "не прошёл проверку", "boom": "ответил 500"}
        for name, fragment in cases.items():
            row = await self.add(name)
            error = await franchise.refresh_one(self.crm, self.vault, row)
            self.assertIn(fragment, error or "", name)
            stored = await self.crm.franchisee(row["id"])
            self.assertIn(fragment, stored["error"], name)
            self.assertIsNone(stored["ok_at"], name)
            self.assertIsNotNone(stored["polled_at"], name)
        self.assertNotIn("steal", self.hits, "переадресацию не проходим")
        row = await self.add("good2", token="wrong-token-" + "y" * 20,
                             base_url=f"http://127.0.0.1:{self.port}/good")
        self.assertIn("токен не принят", await franchise.refresh_one(self.crm, self.vault, row))

    async def test_timeout(self):
        row = await self.add("slow")
        fetch = lambda url, token: franchise_http.fetch_metrics(url, token,  # noqa: E731
                                                                 timeout=0.5)
        started = asyncio.get_running_loop().time()
        error = await franchise.refresh_one(self.crm, self.vault, row, fetch=fetch)
        self.assertIn("не отвечает", error)
        self.assertLess(asyncio.get_running_loop().time() - started, 2.5)

    async def test_poll_once_goes_on_after_a_failure(self):
        await self.add("boom")
        await self.add("good")
        await self.add("off", active=False)
        got = await franchise.poll_once(self.crm, self.vault)
        self.assertEqual(got, {"done": 1, "failed": 1})
        self.assertNotIn("off", self.hits, "выключенного не опрашиваем")
        # Сразу второй круг: принятый - до завтра, неудачный - через час.
        self.hits.clear()
        self.assertEqual(await franchise.poll_once(self.crm, self.vault),
                         {"done": 0, "failed": 0})
        self.assertEqual(self.hits, [])

    async def test_no_key_no_loop(self):
        cfg = mock.Mock(franchise_key="")
        await asyncio.wait_for(franchise.franchise_loop(self.crm, cfg), 1)

    async def test_https_only_before_any_network(self):
        for url in ("http://example.com/hook/metrics", "https://example.com/other",
                    "ftp://example.com/hook/metrics"):
            with self.assertRaises(franchise_http.MetricsError):
                await franchise_http.fetch_metrics(url, "t" * 20)


@unittest.skipUnless(HAVE_PG and HAVE_APP, "pgserver или asyncpg не установлены")
class TestFranchiseOnPostgres(unittest.IsolatedAsyncioTestCase):
    """Запись принятого ответа и условия роялти на настоящей схеме."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pg = pgserver.get_server(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(self.pg.get_uri(), min_size=1, max_size=2,
                                              init=_init_connection)
        await self.pool.execute("drop schema if exists crm cascade; "
                                "drop schema if exists bot cascade")
        db = Database(self.pool)
        await db.apply_schema(SCHEMA)
        await db.apply_schema(SCHEMA)
        self.crm = CrmDB(self.pool)

    async def asyncTearDown(self):
        await self.pool.close()

    async def test_snapshot_months_and_rates(self):
        vault = service.franchise_vault(generate_key())
        fields = logic.check_franchisee({
            "name": "Самара", "base_url": "https://crm.samara.example", "royalty_percent": "5",
            "fixed_fee": "1000", "contract_start": "2026-01-01", "active": "1"}).value
        fid = await service.save_franchisee(self.crm, vault, None, fields, token="t" * 32)
        now = datetime.now().astimezone()
        cur = now.date().replace(day=1)
        prev = (cur - timedelta(days=1)).replace(day=1)
        body = payload(generated_at=now.isoformat(timespec="seconds"),
                       months=[{"month": cur.strftime("%Y-%m"), "partial": True, **block()},
                               {"month": prev.strftime("%Y-%m"), "partial": False,
                                **block("20", "80", "40000")}])
        parsed = logic.parse_metrics_bytes(raw(body), now=now)
        self.assertTrue(parsed.ok, parsed.error)
        await service.franchise_store(self.crm, fid, parsed.value, today=now.date())
        await service.franchise_store(self.crm, fid, parsed.value, today=now.date())
        row = await self.crm.franchisee(fid)
        self.assertIsNotNone(row["ok_at"])
        self.assertEqual(row["data"]["last30"]["revenue"], "45000.00")
        self.assertEqual(await self.pool.fetchval(
            "select count(*) from crm.franchise_snapshots"), 1, "снимок - один на сутки")
        # Новые условия: текущий месяц сразу, прошлый - как был, и после
        # следующего ответа тоже.
        await self.crm.update_franchisee(fid, royalty_percent=D("7"), fixed_fee=D("0"))
        await service.franchise_store(self.crm, fid, parsed.value, today=now.date())
        months = {m["month"]: m for m in await self.crm.franchise_months(prev)}
        self.assertEqual((months[cur]["royalty_percent"], months[cur]["fixed_fee"]),
                         (D("7.00"), D("0.00")))
        self.assertEqual((months[prev]["royalty_percent"], months[prev]["fixed_fee"]),
                         (D("5.00"), D("1000.00")))
        self.assertEqual(months[prev]["revenue"], D("40000.00"))
        self.assertEqual((months[cur]["partial"], months[prev]["partial"]), (True, False),
                         "неполнота месяца живёт в базе, а не только в снимке")
        blocks = logic.royalty_rows([row], list(months.values()), today=now.date(), count=2)
        self.assertEqual(blocks[1]["rows"][0]["royalty"], D("3000.00"))
        # Условия с прошлого месяца - явная правка опечатки.
        await self.crm.update_franchisee(fid, terms_from=prev, royalty_percent=D("6"),
                                         fixed_fee=D("500"))
        months = {m["month"]: m for m in await self.crm.franchise_months(prev)}
        self.assertEqual((months[prev]["royalty_percent"], months[prev]["fixed_fee"]),
                         (D("6.00"), D("500.00")))
        await self.crm.franchise_failed(fid, "таймаут")
        row = await self.crm.franchisee(fid)
        self.assertEqual(row["error"], "таймаут")
        self.assertIsNotNone(row["ok_at"], "неудача не стирает прежние цифры")
        self.assertFalse(await self.crm.delete_franchisee(fid))
        empty = await self.crm.create_franchisee(name="Пусто", base_url="https://e.example")
        self.assertTrue(await self.crm.delete_franchisee(empty))
        twin = await self.crm.create_franchisee(name="Дубль", base_url="https://e.example")
        await service.franchise_store(self.crm, twin, parsed.value, today=now.date())
        self.assertFalse(await self.crm.delete_franchisee(twin))
        self.assertTrue(await self.crm.delete_franchisee(twin, wipe=True))
        self.assertEqual(await self.pool.fetchval(
            "select count(*) from crm.franchise_months where franchisee_id = $1", twin), 0)
        self.assertEqual(len(await self.crm.franchise_months(prev)), 2, "чужие месяцы целы")
        self.assertEqual(await self.crm.purge_franchise_snapshots(400), 0)
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.crm.update_franchisee(fid, royalty_percent=D("150"))


if __name__ == "__main__":
    unittest.main()
