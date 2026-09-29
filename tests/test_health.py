"""Здоровье сервера: что считается бедой, когда об этом пишут и когда
замолкают. Замеры (диск, панель, сертификат) подменены заглушками, база -
tests/fake_crm.py, бот - список отправленного.

Главное: беда приходит один раз и напоминает о себе раз в сутки, а не
каждый час; её конец виден («снова в порядке»); недоставленное сообщение
повторяется, выключенное - копиться не должно.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import health, logic  # noqa: E402
from app.services import probes  # noqa: E402
from tests.fake_crm import FakeCrm  # noqa: E402

try:
    import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    try:
        from tests import test_web as tw
        HAVE_WEB = tw.HAVE_WEB
    except ImportError:                                # pragma: no cover
        HAVE_WEB = False

NOW = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)
GB = 1024 ** 3


def run(coro):
    return asyncio.run(coro)


def backup_ok(now=NOW, **parts):
    base = {"dump": {"at": (now - timedelta(hours=3)).isoformat(), "ok": True,
                     "last_ok": (now - timedelta(hours=3)).isoformat(), "size": 50 * 1024 ** 2},
            "offsite": {"enabled": True, "at": (now - timedelta(hours=3)).isoformat(),
                        "ok": True, "last_ok": (now - timedelta(hours=3)).isoformat(),
                        "target": "storage.yandexcloud.net/bk/kzn"},
            "restore": {"at": (now - timedelta(days=2)).isoformat(), "ok": True,
                        "last_ok": (now - timedelta(days=2)).isoformat(), "source": "offsite"}}
    base.update(parts)
    return json.dumps(base)


class Probes:
    """Замеры по заказу теста."""

    def __init__(self, *, disk=(40 * GB, 100 * GB), panel=(200,), cert_days=60,
                 cert_error=None):
        self.disk = disk
        self.panel = list(panel)
        self.cert_days = cert_days
        self.cert_error = cert_error
        self.panel_calls = 0

    def disk_usage(self, path):
        if isinstance(self.disk, Exception):
            raise self.disk
        return self.disk

    async def http_status(self, url):
        self.panel_calls += 1
        answer = self.panel[min(self.panel_calls, len(self.panel)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def cert_not_after(self, host):
        if self.cert_error:
            raise self.cert_error
        return NOW + timedelta(days=self.cert_days)


class Bot:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.fail:
            raise RuntimeError("Telegram недоступен")
        self.sent.append((chat_id, text))


def cfg(**kw):
    base = {"contract_chat_id": -100500, "storage_dir": Path("/files/kyc"),
            "health_panel_url": "http://crm:8080/healthz",
            "crm_domain": "crm.x.ru", "demo_domain": ""}
    base.update(kw)
    return types.SimpleNamespace(**base)


class TestProblems(unittest.TestCase):
    def problems(self, **kw):
        kw.setdefault("backup", logic.parse_backup_status(backup_ok()))
        return logic.health_problems(now=NOW, **kw)

    def test_all_good(self):
        self.assertEqual(self.problems(disk=(40 * GB, 100 * GB),
                                       certs=[("crm.x.ru", NOW + timedelta(days=60), None)]),
                         {})

    def test_disk_below_threshold(self):
        p = self.problems(disk=(8 * GB, 100 * GB))
        self.assertIn("свободно 8,0 ГБ из 100,0 ГБ (8 %)", p["disk"]["text"])
        self.assertNotIn("disk", self.problems(disk=(8 * GB, 100 * GB), disk_pct=5))
        self.assertNotIn("disk", self.problems(disk=None), "не измерили - молчим")

    def test_panel(self):
        p = self.problems(panel_error="Cannot connect to host crm:8080")
        self.assertIn("Панель не отвечает: Cannot connect", p["panel"]["text"])

    def test_certificate(self):
        soon = self.problems(certs=[("crm.x.ru", NOW + timedelta(days=5, hours=2), None)])
        self.assertIn("осталось 5 дн.", soon["cert:crm.x.ru"]["text"])
        self.assertEqual(self.problems(certs=[("crm.x.ru", NOW + timedelta(days=30), None)]), {})
        broken = self.problems(certs=[("demo.x.ru", None, "certificate has expired")])
        self.assertIn("не отвечает по HTTPS: certificate has expired",
                      broken["cert:demo.x.ru"]["text"])

    def test_backup_part_of_the_problems(self):
        stale = logic.parse_backup_status(backup_ok(restore={
            "at": (NOW - timedelta(days=9)).isoformat(), "ok": True}))
        self.assertIn("restore", self.problems(backup=stale))
        self.assertIn("dump", self.problems(backup=None))


class TestStep(unittest.TestCase):
    DISK = {"disk": {"title": "Диск", "text": "Диск почти полон"}}

    def step(self, prev, problems, now):
        lines, state = logic.health_step(prev, problems, now)
        # Состояние живёт в crm.settings строкой - проверяем через JSON.
        return lines, logic.parse_health_state(json.dumps(state))

    def test_alert_once_repeat_daily_and_recover(self):
        lines, state = self.step(None, self.DISK, NOW)
        self.assertEqual(lines, ["⚠️ Диск почти полон"])
        lines, state = self.step(state, self.DISK, NOW + timedelta(hours=1))
        self.assertEqual(lines, [], "через час - тишина")
        lines, state = self.step(state, self.DISK, NOW + timedelta(hours=23, minutes=59))
        self.assertEqual(lines, [])
        lines, state = self.step(state, self.DISK, NOW + timedelta(hours=24))
        self.assertEqual(lines, ["⚠️ Всё ещё, с 27.09 12:00: Диск почти полон"])
        lines, state = self.step(state, {}, NOW + timedelta(hours=30))
        self.assertEqual(lines, ["✅ Диск: снова в порядке (сбой тянулся с 27.09 12:00)"])
        self.assertEqual(state["problems"], {})
        self.assertEqual(state["checked_at"], NOW + timedelta(hours=30))

    def test_text_changes_without_a_new_alert(self):
        _, state = self.step(None, self.DISK, NOW)
        lines, state = self.step(state, {"disk": {"title": "Диск", "text": "Уже 3 %"}},
                                 NOW + timedelta(hours=1))
        self.assertEqual(lines, [])
        self.assertEqual(state["problems"]["disk"]["text"], "Уже 3 %")

    def test_keep_forgets_nothing(self):
        _, state = self.step(None, self.DISK, NOW)
        kept = logic.health_keep(state, NOW + timedelta(hours=1))
        kept = logic.parse_health_state(json.dumps(kept))
        self.assertEqual(kept["problems"], state["problems"])
        self.assertEqual(kept["checked_at"], NOW + timedelta(hours=1))

    def test_garbage_state_is_a_clean_slate(self):
        for raw in (None, "", "{", "[]", json.dumps({"problems": "x"})):
            self.assertEqual(logic.parse_health_state(raw),
                             {"checked_at": None, "problems": {}})

    def test_message_is_escaped(self):
        text = logic.health_message(["⚠️ Панель не отвечает: <html> & co"])
        self.assertIn("&lt;html&gt; &amp; co", text)
        self.assertTrue(text.startswith("🖥 <b>Сервер</b>\n"))

    def test_pulse(self):
        state = {"checked_at": NOW, "problems": {}}
        self.assertTrue(logic.bot_alive(state, NOW + timedelta(hours=2)))
        self.assertFalse(logic.bot_alive(state, NOW + timedelta(hours=2, minutes=1)))
        self.assertFalse(logic.bot_alive({"checked_at": None}, NOW))


class TestCheckOnce(unittest.TestCase):
    def setUp(self):
        self.crm = FakeCrm()
        self.crm.settings_[logic.BACKUP_STATUS_KEY] = backup_ok()

    def check(self, bot, probes, now=NOW, **kw):
        return run(health.check_once(bot, self.crm, cfg(**kw), now=now, probes=probes,
                                     retry=0))

    def state(self):
        return logic.parse_health_state(self.crm.settings_[logic.HEALTH_KEY])

    def test_quiet_when_all_is_well(self):
        bot = Bot()
        result = self.check(bot, Probes())
        self.assertEqual((result["problems"], bot.sent), ({}, []))
        self.assertEqual(self.state()["checked_at"], NOW, "пульс записан и без бед")

    def test_problem_goes_to_the_owner_chat_once(self):
        bot = Bot()
        low = Probes(disk=(3 * GB, 100 * GB))
        self.check(bot, low)
        self.assertEqual(len(bot.sent), 1)
        chat, text = bot.sent[0]
        self.assertEqual(chat, -100500)
        self.assertIn("Диск почти полон", text)
        self.check(bot, low, NOW + timedelta(hours=1))
        self.assertEqual(len(bot.sent), 1, "раз в сутки, а не каждый час")
        self.check(bot, Probes(), NOW + timedelta(hours=2))
        self.assertIn("✅ Диск: снова в порядке", bot.sent[-1][1])
        self.assertEqual([n["status"] for n in self.crm.notice_log_], ["sent", "sent"])

    def test_owner_can_send_it_to_a_person(self):
        run(self.crm.set_notice("server_health", enabled=True, at_hour=None,
                                chat_id="777", extra={"disk_pct": 50, "cert_days": 14},
                                by="admin"))
        bot = Bot()
        self.check(bot, Probes(disk=(40 * GB, 100 * GB)))
        self.assertEqual(bot.sent[0][0], "777")
        self.assertIn("40 %", bot.sent[0][1], "порог владельца - 50 %")

    def test_disabled_notice_does_not_pile_up(self):
        run(self.crm.set_notice("server_health", enabled=False, at_hour=None,
                                chat_id=None, extra={}, by="admin"))
        bot = Bot()
        self.check(bot, Probes(panel=(502,)))
        self.assertEqual(bot.sent, [])
        self.assertIn("panel", self.state()["problems"], "панель показывает беду и так")
        run(self.crm.set_notice("server_health", enabled=True, at_hour=None,
                                chat_id=None, extra={}, by="admin"))
        self.check(bot, Probes(panel=(502,)), NOW + timedelta(hours=1))
        self.assertEqual(bot.sent, [], "включение не обрушивает накопленное")

    def test_undelivered_message_is_repeated(self):
        self.check(Bot(fail=True), Probes(panel=(502,)))
        self.assertEqual(self.state()["problems"], {})
        bot = Bot()
        self.check(bot, Probes(panel=(502,)), NOW + timedelta(hours=1))
        self.assertIn("Панель не отвечает: ответ 502", bot.sent[0][1])
        self.assertEqual([n["status"] for n in self.crm.notice_log_], ["failed", "sent"])

    def test_panel_restart_is_not_a_failure(self):
        """Вторая попытка отличает перезапуск панели при выкладке от падения."""
        probe = Probes(panel=(ConnectionRefusedError("refused"), 200))
        bot = Bot()
        self.check(bot, probe)
        self.assertEqual((probe.panel_calls, bot.sent), (2, []))

    def test_certificates_of_both_domains(self):
        bot = Bot()
        result = self.check(bot, Probes(cert_days=3), demo_domain="Demo.x.ru")
        self.assertEqual(sorted(result["problems"]), ["cert:crm.x.ru", "cert:demo.x.ru"])
        self.assertEqual(len(bot.sent), 1, "одна проверка - одно сообщение")

    def test_no_domain_no_certificate_check(self):
        result = self.check(Bot(), Probes(cert_error=OSError("нет")), crm_domain="")
        self.assertEqual(result["problems"], {})

    def test_backup_report_missing(self):
        del self.crm.settings_[logic.BACKUP_STATUS_KEY]
        bot = Bot()
        self.check(bot, Probes())
        self.assertIn("Сервис backup ни разу не отчитался", bot.sent[0][1])

    def test_disk_that_cannot_be_measured_is_skipped(self):
        result = self.check(Bot(), Probes(disk=OSError("нет тома")))
        self.assertEqual(result["problems"], {})

    def test_pulse_on_start_keeps_problems(self):
        self.check(Bot(), Probes(panel=(502,)))
        run(health.pulse(self.crm, now=NOW + timedelta(minutes=5)))
        state = self.state()
        self.assertEqual(state["checked_at"], NOW + timedelta(minutes=5))
        self.assertIn("panel", state["problems"])


class TestProbes(unittest.TestCase):
    def test_disk_usage_is_real(self):
        free, total = probes.disk_usage(Path(__file__).parent)
        self.assertGreater(total, 0)
        self.assertLessEqual(free, total)

    def test_http_status_of_a_real_socket(self):
        async def main():
            async def answer(reader, writer):
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                             b"Connection: close\r\n\r\n")
                await writer.drain()
                writer.close()
            server = await asyncio.start_server(answer, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            async with server:
                return await probes.http_status(f"http://127.0.0.1:{port}/healthz")
        try:
            import aiohttp  # noqa: F401
        except ImportError:                            # pragma: no cover
            self.skipTest("нет aiohttp")
        self.assertEqual(run(main()), 503)

    @unittest.skipUnless(shutil.which("openssl"), "нужен openssl")
    def test_certificate_date_and_refusal(self):
        """Срок читается из сертификата, который отдаёт сервер; чужой
        (самоподписанный без доверия) - исключение, как у браузера."""
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = Path(tmp) / "c.pem", Path(tmp) / "k.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", str(key), "-out", str(cert), "-days", "10",
                            "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost"],
                           check=True, capture_output=True)

            async def main():
                server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                server_ctx.load_cert_chain(cert, key)

                async def hold(reader, writer):
                    with contextlib.suppress(Exception):
                        await reader.read(1)
                    writer.close()
                server = await asyncio.start_server(hold, "127.0.0.1", 0, ssl=server_ctx)
                port = server.sockets[0].getsockname()[1]
                async with server:
                    trusted = ssl.create_default_context(cafile=str(cert))
                    until = await probes.cert_not_after("localhost", port=port,
                                                        context=trusted)
                    with self.assertRaises(ssl.SSLError):
                        await probes.cert_not_after("localhost", port=port)
                return until
            until = run(main())
        left = until - datetime.now(UTC)
        self.assertTrue(timedelta(days=9) < left <= timedelta(days=10, minutes=1), left)

    def test_domains(self):
        self.assertEqual(health.domains(cfg(crm_domain=" CRM.x.ru ", demo_domain="crm.x.ru")),
                         ["crm.x.ru"])
        self.assertEqual(health.domains(cfg(crm_domain="", demo_domain="")), [])


class TestServerRows(unittest.TestCase):
    def test_rows(self):
        rows = logic.server_rows(logic.parse_backup_status(backup_ok()),
                                 {"checked_at": NOW - timedelta(minutes=10), "problems": {
                                     "disk": {"title": "Диск", "text": "мало места"}}}, NOW)
        by = {r["title"]: r for r in rows}
        self.assertTrue(by["Процесс бота"]["ok"])
        self.assertTrue(by["Бэкап базы"]["ok"])
        self.assertIn("50 МБ", by["Бэкап базы"]["text"])
        self.assertIn("storage.yandexcloud.net/bk/kzn", by["Копия в облаке"]["text"])
        self.assertIn("из облака", by["Проверка восстановления"]["text"])
        self.assertIs(by["Диск"]["ok"], False)

    def test_silent_bot_and_no_cloud(self):
        rows = logic.server_rows(
            logic.parse_backup_status(backup_ok(offsite={"enabled": False})),
            {"checked_at": NOW - timedelta(hours=3), "problems": {}}, NOW)
        by = {r["title"]: r for r in rows}
        self.assertIs(by["Процесс бота"]["ok"], False)
        self.assertIsNone(by["Копия в облаке"]["ok"])
        self.assertIn("BACKUP_S3_BUCKET", by["Копия в облаке"]["text"])


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestWeb(tw.WebCase if HAVE_WEB else unittest.TestCase):
    def test_bot_pulse_endpoint_is_public(self):
        r = self.client.get("/healthz/bot")
        self.assertEqual(r.status_code, 503, "бот ещё не отмечался")
        self.assertEqual(r.json()["ok"], False)
        run(health.pulse(self.crm))
        r = self.client.get("/healthz/bot")
        self.assertEqual((r.status_code, r.json()["ok"]), (200, True))

    def test_notices_page_shows_the_server(self):
        self.crm.settings_[logic.BACKUP_STATUS_KEY] = backup_ok(datetime.now(UTC))
        run(health.pulse(self.crm))
        self.login()
        page = self.get_ok("/notices")
        self.assertIn("Сервер", page)
        self.assertIn("storage.yandexcloud.net/bk/kzn", page)
        self.assertIn("/healthz/bot", page)
        self.assertIn("Здоровье сервера", page)
        self.assertIn("диск: свободно меньше", page, "подпись порога - не «через N дн.»")

    def test_server_card_text_sits_under_the_title(self):
        """Телефон: объяснение беды - в той же ячейке, что заголовок, а не
        третьей колонкой, которая уезжала за край карточки."""
        error = "выгрузка: AccessDenied https://storage.yandexcloud.net/mybike-backup/kzn"
        self.crm.settings_[logic.BACKUP_STATUS_KEY] = backup_ok(
            datetime.now(UTC), offsite={"enabled": True, "at": datetime.now(UTC).isoformat(),
                                        "ok": False, "error": error})
        self.login()
        page = self.get_ok("/notices")
        card = page[page.index("<h2>Сервер</h2>"):]
        card = card[:card.index("</section>")]
        rows = re.findall(r"(?s)<tr>(.*?)</tr>", card)
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row.count("<td"), 2, row)
        cloud = next(r for r in rows if "Копия в облаке" in r)
        cell = re.search(r"(?s)<td[^>]*>((?:(?!</td>).)*Копия в облаке.*?)</td>", cloud).group(1)
        self.assertIn("AccessDenied", cell)
        self.assertIn("overflow-wrap:anywhere", cloud, "длинный адрес переносится")

    def test_threshold_is_saved(self):
        self.login()
        r = self.client.post("/notices/server_health",
                             data={"enabled": "1", "disk_pct": "15", "cert_days": "21"})
        self.assertEqual(r.status_code, 303)
        state = logic.notice_settings(run(self.crm.notices()))["server_health"]
        self.assertEqual(state["extra"], {"disk_pct": 15, "cert_days": 21})
        self.assertIsNone(state["at_hour"], "событийное остаётся событийным")


if __name__ == "__main__":
    unittest.main()
