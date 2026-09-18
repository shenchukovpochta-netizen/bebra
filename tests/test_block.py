"""Блокировка мотора: команда StarLine, очередь, опрос, панель.

Сети нет: клиент StarLine получает заглушку сессии, опрос - заглушку
клиента. Проверяется путь целиком: кнопка в панели кладёт команду в
очередь, опрос относит её и пишет ответ, карточка показывает состояние.
"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402
from app.services import starline  # noqa: E402

try:
    import test_web as tw

    from app.crm import tracking
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class Response:
    def __init__(self, data, *, status=200, cookies=None):
        self._data, self.status = data, status
        self.cookies = cookies or {}
        self.headers = {}

    async def json(self, content_type=None):
        return self._data


def session_factory(calls: list, *, reply: dict):
    """Сессия, которая проходит авторизацию и отвечает `reply` на set_param."""
    class Session:
        async def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if url.endswith("getCode/"):
                return Response({"state": 1, "desc": {"code": "CODE"}})
            if url.endswith("getToken/"):
                return Response({"state": 1, "desc": {"token": "APP"}})
            if url.endswith("user/login/"):
                return Response({"state": 1, "desc": {"user_token": "SLID"}})
            if url.endswith("auth.slid"):
                return Response({"user_id": "42"}, cookies={"slnet": "COOKIE"})
            return Response(reply)

        async def close(self):
            pass
    return Session


class TestSetParam(unittest.TestCase):
    def client(self, calls, reply):
        return starline.StarlineClient(app_id="1", app_secret="s", login="l",
                                      password="p",
                                      session_factory=session_factory(calls, reply=reply))

    def test_block_is_hijack_via_set_param(self):
        calls: list = []
        client = self.client(calls, {"code": 200, "codestring": "OK"})
        got = _run(client.block_motor("1001", True, now=0.0))
        self.assertEqual(got["code"], 200)
        method, url, kwargs = calls[-1]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/v1/device/1001/set_param"), url)
        self.assertEqual(kwargs["json"], {"type": "hijack", "hijack": 1})
        self.assertEqual(kwargs["headers"]["Cookie"], "slnet=COOKIE")
        # Снятие - тот же параметр нулём, авторизация из кэша.
        calls.clear()
        _run(client.block_motor("1001", False, now=1.0))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2]["json"], {"type": "hijack", "hijack": 0})

    def test_refusal_carries_starline_words(self):
        client = self.client([], {"code": 403, "codestring": "Device offline"})
        with self.assertRaises(starline.StarlineError) as ctx:
            _run(client.block_motor("1001", True, now=0.0))
        self.assertIn("Device offline", str(ctx.exception))

    def test_unconfigured_client_refuses_instead_of_pretending(self):
        client = starline.StarlineClient(app_id="", app_secret="", login="", password="")
        with self.assertRaises(starline.StarlineError):
            _run(client.block_motor("1001", True))


class TestCommandLogic(unittest.TestCase):
    def test_command_rows_know_their_state(self):
        rows = logic.command_rows([
            {"id": 1, "command": "block", "requested_at": NOW - timedelta(minutes=1),
             "sent_at": None, "ok": None},
            {"id": 2, "command": "block", "requested_at": NOW - timedelta(minutes=30),
             "sent_at": None, "ok": None},
            {"id": 3, "command": "unblock", "requested_at": NOW - timedelta(hours=1),
             "sent_at": NOW, "ok": True},
            {"id": 4, "command": "block", "requested_at": NOW - timedelta(hours=2),
             "sent_at": NOW, "ok": False, "result": "Device offline"}], now=NOW)
        self.assertEqual([r["state"] for r in rows], ["pending", "pending", "done", "failed"])
        self.assertEqual([r["stale"] for r in rows], [False, True, False, False])
        self.assertEqual(rows[2]["title"], "Снять блокировку")

    def test_next_command_is_the_opposite_unless_one_is_waiting(self):
        self.assertEqual(logic.block_state({"blocked": False}, None)["next"], "block")
        self.assertEqual(logic.block_state({"blocked": True}, None)["next"], "unblock")
        self.assertIsNone(logic.block_state({"blocked": False},
                                            {"command": "block"})["next"])
        self.assertTrue(logic.check_command("block").ok)
        self.assertFalse(logic.check_command("explode").ok)


class Client:
    """Заглушка клиента StarLine для опроса."""

    def __init__(self, *, fail=False):
        self.ready = True
        self.fail = fail
        self.blocked: list[tuple[str, bool]] = []

    async def devices(self):
        return []

    async def block_motor(self, device_id, on):
        if self.fail:
            raise starline.StarlineError("set_param: StarLine отказал (Device offline)")
        self.blocked.append((device_id, on))
        return {"code": 200}


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestPollCommands(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.tracker_id = tw.run(self.crm.create_tracker(
            device_id="1001", alias="Truck+ 101", bike_id=self.bike_id))

    def queue(self, command="block"):
        return tw.run(self.crm.queue_tracker_command(
            tracker_id=self.tracker_id, command=command, by="admin"))

    def test_queued_command_is_sent_once_and_recorded(self):
        self.queue()
        client = Client()
        out = tw.run(tracking.poll_once(self.crm, client))
        self.assertEqual(client.blocked, [("1001", True)])
        self.assertTrue(out["commands"][0]["ok"])
        tracker = tw.run(self.crm.tracker(self.tracker_id))
        self.assertTrue(tracker["blocked"])
        self.assertEqual(tracker["blocked_by"], "admin")
        self.assertIsNone(tw.run(self.crm.pending_command_of(self.tracker_id)))
        # Второй круг ничего не шлёт: команда выполнена.
        out = tw.run(tracking.poll_once(self.crm, client))
        self.assertEqual(out["commands"], [])
        self.assertEqual(len(client.blocked), 1)
        # Снятие блокировки - обратная команда.
        self.queue("unblock")
        tw.run(tracking.poll_once(self.crm, client))
        self.assertEqual(client.blocked[-1], ("1001", False))
        self.assertFalse(tw.run(self.crm.tracker(self.tracker_id))["blocked"])

    def test_refusal_is_written_on_the_command_and_state_stays(self):
        cid = self.queue()
        out = tw.run(tracking.poll_once(self.crm, Client(fail=True)))
        self.assertFalse(out["commands"][0]["ok"])
        self.assertIn("Device offline", out["commands"][0]["result"])
        self.assertFalse(tw.run(self.crm.tracker(self.tracker_id))["blocked"])
        # Отказ закрывает команду: бесконечно долбить StarLine не нужно.
        self.assertIsNone(tw.run(self.crm.pending_command_of(self.tracker_id)))
        row = tw.run(self.crm.tracker_commands(self.tracker_id))[0]
        self.assertEqual((row["id"], row["ok"]), (cid, False))

    def test_second_command_waits_for_the_first(self):
        self.queue()
        with self.assertRaises(Exception) as ctx:
            self.queue("unblock")
        self.assertIn("pending", str(ctx.exception))

    def test_digest_says_what_happened(self):
        self.queue()
        out = tw.run(tracking.poll_once(self.crm, Client()))
        chat = types.SimpleNamespace(contract_chat_id=-100500)
        self.assertEqual(tw.run(tracking.report_commands(self.bot, chat, out["commands"])), 1)
        text = self.bot.sent[-1][1]
        self.assertIn("StarLine принял", text)
        self.assertIn("B-1", text)
        self.assertIn("admin", text)
        self.queue("unblock")
        out = tw.run(tracking.poll_once(self.crm, Client(fail=True)))
        tw.run(tracking.report_commands(self.bot, chat, out["commands"]))
        self.assertIn("не прошла", self.bot.sent[-1][1])
        self.assertEqual(tracking.commands_digest([]), "")


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestBlockPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.tracker_id = tw.run(self.crm.create_tracker(
            device_id="1001", alias="Truck+ 101", bike_id=self.bike_id,
            last_seen=datetime.now(UTC), lat=55.7692, lon=49.1440,
            speed=D(0), voltage=D("12.6")))

    def card(self) -> str:
        return self.get_ok(f"/trackers/{self.tracker_id}")

    def test_button_queues_the_command_and_the_card_follows_it(self):
        page = self.card()
        self.assertIn("Заблокировать мотор", page)
        self.assertIn("Не заблокирован", page)
        r = self.client.post(f"/trackers/{self.tracker_id}/command",
                             data={"command": "block"})
        self.assertEqual(r.status_code, 303)
        pending = tw.run(self.crm.pending_command_of(self.tracker_id))
        self.assertEqual((pending["command"], pending["requested_by"]),
                         ("block", "staff:admin"))
        page = self.card()
        self.assertIn("в очереди", page)
        self.assertNotIn('name="command" value="block"', page, "пока команда ждёт, кнопки нет")
        # Вторая команда до отправки первой - отказ словами.
        self.client.post(f"/trackers/{self.tracker_id}/command", data={"command": "unblock"})
        self.assertIn("уже в очереди", self.card())
        # Опрос отнёс - карточка показывает блокировку и обратную кнопку.
        tw.run(tracking.poll_once(self.crm, Client()))
        page = self.card()
        self.assertIn("мотор заблокирован", page)
        self.assertIn("Снять блокировку", page)
        self.assertIn("выполнена", page)
        self.assertIn("🔒", self.get_ok("/trackers"))

    def test_bad_command_and_inactive_tracker_are_refused(self):
        self.client.post(f"/trackers/{self.tracker_id}/command", data={"command": "explode"})
        self.assertIn("Команда", self.card())
        self.assertIsNone(tw.run(self.crm.pending_command_of(self.tracker_id)))
        tw.run(self.crm.update_tracker(self.tracker_id, active=False))
        self.client.post(f"/trackers/{self.tracker_id}/command", data={"command": "block"})
        self.assertIn("снят с наблюдения", self.card())
        self.assertIsNone(tw.run(self.crm.pending_command_of(self.tracker_id)))
        self.assertEqual(self.client.post("/trackers/999/command",
                                          data={"command": "block"}).status_code, 404)

    def test_alert_row_offers_the_block(self):
        alert_id = tw.run(self.crm.raise_alert(
            tracker_id=self.tracker_id, kind="moving", note="25 км/ч",
            bike_id=self.bike_id, lat=None, lon=None, level="urgent"))
        button = 'name="command" value="block"'
        page = self.get_ok("/alerts")
        self.assertIn(button, page)
        self.assertIn(f'action="/trackers/{self.tracker_id}/command"', page)
        r = self.client.post(f"/trackers/{self.tracker_id}/command", data={
            "command": "block", "alert_id": str(alert_id), "next": "/alerts"})
        self.assertEqual(r.headers["location"], "/alerts")
        pending = tw.run(self.crm.pending_command_of(self.tracker_id))
        self.assertEqual(pending["alert_id"], alert_id)
        # StarLine принял - в строке тревоги вместо кнопки отметка.
        tw.run(self.crm.finish_tracker_command(pending["id"], ok=True, result="ok"))
        tw.run(self.crm.update_tracker(self.tracker_id, blocked=True))
        page = self.get_ok("/alerts")
        self.assertNotIn(button, page)
        self.assertIn("мотор заблокирован", page)
        # Жёлтая тревога кнопки не предлагает.
        tw.run(self.crm.update_tracker(self.tracker_id, blocked=False))
        tw.run(self.crm.close_alerts(self.tracker_id, ["moving"], by="t"))
        tw.run(self.crm.raise_alert(tracker_id=self.tracker_id, kind="low_power",
                                    note=None, bike_id=self.bike_id, lat=None, lon=None))
        self.assertNotIn(button, self.get_ok("/alerts"))

    def test_viewer_cannot_block(self):
        """Механик трекеры только видит: кнопки нет, POST отбивается."""
        profile = tw.run(self.crm.access_profile_by_code("tech"))
        tw.run(self.crm.create_staff("petr", logic.hash_password("password-1"),
                                     "Пётр", "manager", profile["id"]))
        self.client.post("/logout")
        self.login("petr", "password-1")
        r = self.client.post(f"/trackers/{self.tracker_id}/command",
                             data={"command": "block"})
        self.assertIn(r.status_code, (302, 303, 403))
        self.assertIsNone(tw.run(self.crm.pending_command_of(self.tracker_id)))
        self.assertNotIn("Заблокировать мотор", self.card())

    def test_stale_command_is_flagged(self):
        tw.run(self.crm.queue_tracker_command(tracker_id=self.tracker_id,
                                              command="block", by="admin"))
        for command in self.crm.commands_:
            command["requested_at"] = datetime.now(UTC) - timedelta(minutes=30)
        self.assertIn("опрос не забирает", self.card())


if __name__ == "__main__":
    unittest.main()
