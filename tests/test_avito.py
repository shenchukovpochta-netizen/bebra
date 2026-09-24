"""Клиент Авито (Messenger API): токен, повтор на 403, пути, разбор ответов.

Сети в тестах нет: `session_factory` подменён сервером в памяти, который
пишет каждый запрос и отвечает по пути. Главное, что здесь проверяется:
ключ не уходит в адрес (адреса попадают в логи), просроченный токен
лечится ровно одним повтором, отказ по тарифу и «реже» не повторяются
вслепую, а текст длиннее предела отсекается до сети. Сбой сети, TLS и
таймаут - AvitoError, а не падение круга опроса; пустой ответ на чтение -
ошибка, а на отправку - «ушло». Разбор чата и сообщения проверяется
отдельно, без запросов.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import avito  # noqa: E402
from app.services.avito import AvitoClient, AvitoError  # noqa: E402

try:
    import aiohttp
except ImportError:                                     # pragma: no cover
    aiohttp = None

OWN = 777                     # id нашего аккаунта у Авито
SECRET = "s3cr3t-ключ"
SELF_PATH = "core/v1/accounts/self"
CHATS_PATH = f"messenger/v2/accounts/{OWN}/chats"


def msg_path(chat_id: str) -> str:
    return f"messenger/v3/accounts/{OWN}/chats/{chat_id}/messages/"


def send_path(chat_id: str) -> str:
    return f"messenger/v1/accounts/{OWN}/chats/{chat_id}/messages"


class FakeResponse:
    def __init__(self, server: FakeAvito, status: int, payload):
        self.server, self.status, self.payload = server, status, payload

    async def json(self, content_type=None):
        # У aiohttp тело после закрытия сессии уже не прочитать.
        if self.server.open <= 0:
            self.server.read_outside += 1
            raise RuntimeError("Session is closed")
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, server: FakeAvito):
        self.server = server

    async def __aenter__(self):
        self.server.sessions += 1
        self.server.open += 1
        return self

    async def __aexit__(self, *exc):
        self.server.open -= 1
        return False

    async def request(self, method, url, **kwargs):
        # Уступаем цикл: так параллельные вызовы действительно пересекаются.
        await asyncio.sleep(0)
        return self.server.handle(method, url, kwargs)


class FakeAvito:
    """Авито в памяти. Ответы на путь - очередь: последний повторяется.

    Ответ - пара (код, тело) или исключение: его бросает сам запрос, как
    aiohttp при обрыве, таймауте или отказе TLS - ответа нет вовсе.
    """

    def __init__(self, *, expires_in: int = 86400):
        self.calls: list[dict] = []
        self.sessions = 0
        self.open = 0
        self.read_outside = 0
        self.issued = 0
        self.expires_in = expires_in
        self.token_reply: tuple[int, object] | BaseException | None = None
        self.routes: dict[tuple[str, str], list[tuple[int, object] | BaseException]] = {
            ("GET", SELF_PATH): [(200, {"id": OWN, "name": "МАЙБАЙК"})]}

    def reply(self, method: str, path: str,
              *answers: tuple[int, object] | BaseException) -> None:
        self.routes[(method, path)] = list(answers)

    def factory(self) -> FakeSession:
        return FakeSession(self)

    def handle(self, method: str, url: str, kwargs: dict) -> FakeResponse:
        base = avito.API_URL + "/"
        assert url.startswith(base), url
        path = url[len(base):]
        self.calls.append({"method": method, "url": url, "path": path, **kwargs})
        if path == "token/":
            if isinstance(self.token_reply, BaseException):
                raise self.token_reply
            if self.token_reply is not None:
                return FakeResponse(self, *self.token_reply)
            self.issued += 1
            return FakeResponse(self, 200, {"access_token": f"tok-{self.issued}",
                                            "expires_in": self.expires_in,
                                            "token_type": "Bearer"})
        answers = self.routes.get((method, path))
        if not answers:
            return FakeResponse(self, 404, {"error": {"code": 404}})
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, BaseException):
            raise answer
        status, payload = answer
        return FakeResponse(self, status, payload)

    def token_calls(self) -> list[dict]:
        return [c for c in self.calls if c["path"] == "token/"]

    def api_calls(self, path: str | None = None) -> list[dict]:
        return [c for c in self.calls
                if c["path"] != "token/" and (path is None or c["path"] == path)]


def make(**over) -> tuple[AvitoClient, FakeAvito]:
    server = FakeAvito(**over)
    client = AvitoClient(client_id="cid-1", client_secret=SECRET,
                         session_factory=server.factory)
    return client, server


def bearer(call: dict) -> str | None:
    return (call.get("headers") or {}).get("Authorization")


def aiohttp_like(name: str, module: str = "aiohttp.client_exceptions") -> type[Exception]:
    """Двойник исключения aiohttp: тот же модуль, без самой библиотеки.
    Родитель - голый Exception, чтобы сработало именно правило модуля, а
    не OSError/TimeoutError, от которых наследуются некоторые настоящие."""
    return type(name, (Exception,), {"__module__": module})


def messenger_calls(server: FakeAvito) -> list[dict]:
    return [c for c in server.api_calls() if c["path"].startswith("messenger/")]


class TestReady(unittest.TestCase):
    def test_ready_needs_both_keys(self):
        self.assertTrue(AvitoClient(client_id="a", client_secret="b").ready)
        self.assertFalse(AvitoClient(client_id="", client_secret="b").ready)
        self.assertFalse(AvitoClient(client_id="a", client_secret="").ready)
        self.assertFalse(AvitoClient(client_id=None, client_secret=None).ready)

    def test_token_is_not_in_repr(self):
        client = AvitoClient(client_id="a", client_secret="b")
        client._token = "живой-токен"
        self.assertNotIn("живой-токен", repr(client), "токен не попадает в логи")


class TestToken(unittest.IsolatedAsyncioTestCase):
    async def test_token_requested_once_and_cached(self):
        client, server = make()
        server.reply("GET", CHATS_PATH, (200, {"chats": []}))
        server.reply("GET", msg_path("c1"), (200, []))
        await client.chats()
        await client.chats()
        await client.messages("c1")
        self.assertEqual(len(server.token_calls()), 1, "токен живёт сутки - один запрос")
        self.assertEqual({bearer(c) for c in server.api_calls()}, {"Bearer tok-1"})

    async def test_secret_goes_in_form_body_not_url(self):
        client, server = make()
        await client.self_id()
        token = server.token_calls()[0]
        self.assertEqual(token["method"], "POST")
        self.assertEqual(token["data"], {"grant_type": "client_credentials",
                                         "client_id": "cid-1", "client_secret": SECRET})
        self.assertNotIn("params", token, "ключи не в строке запроса")
        self.assertNotIn("json", token, "форма, а не JSON")
        self.assertIsNone(bearer(token), "за токеном ходят без токена")
        for call in server.calls:
            self.assertNotIn(SECRET, call["url"], call["url"])
            self.assertNotIn("cid-1", call["url"], call["url"])
            self.assertNotIn(SECRET, str(call.get("params") or ""))
            self.assertNotIn(SECRET, str(call.get("headers") or ""))

    async def test_token_refreshed_ahead_of_expiry(self):
        client, server = make(expires_in=3600)
        clock = [1000.0]
        # Часы подменяются только у модуля Авито: общие time.monotonic -
        # это часы самого цикла asyncio.
        with mock.patch.object(avito, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            await client._call("GET", SELF_PATH)
            clock[0] += 3600 - avito.TOKEN_MARGIN - 1
            await client._call("GET", SELF_PATH)
            self.assertEqual(len(server.token_calls()), 1, "до запаса - старый токен")
            clock[0] += 2
            await client._call("GET", SELF_PATH)
        self.assertEqual(len(server.token_calls()), 2, "за запасом - новый, не ждём 403")
        self.assertEqual(bearer(server.api_calls()[-1]), "Bearer tok-2")

    async def test_short_token_still_lives_a_minute(self):
        client, server = make(expires_in=30)
        clock = [0.0]
        with mock.patch.object(avito, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            await client._call("GET", SELF_PATH)
            clock[0] = 59.0
            await client._call("GET", SELF_PATH)
            self.assertEqual(len(server.token_calls()), 1, "не меньше минуты на токен")
            clock[0] = 61.0
            await client._call("GET", SELF_PATH)
        self.assertEqual(len(server.token_calls()), 2)

    async def test_parallel_calls_share_one_token(self):
        client, server = make()
        server.reply("GET", "a", (200, {}))
        server.reply("GET", "b", (200, {}))
        await asyncio.gather(client._call("GET", "a"), client._call("GET", "b"),
                             client._call("GET", "a"))
        self.assertEqual(len(server.token_calls()), 1, "замок: один токен на всех")

    async def test_token_refused(self):
        client, server = make()
        server.token_reply = (400, {"error": "invalid_client"})
        with self.assertRaises(AvitoError) as err:
            await client.self_id()
        self.assertEqual(err.exception.status, 400)
        self.assertNotIn(SECRET, str(err.exception))
        self.assertEqual(server.api_calls(), [], "без токена в API не ходим")

    async def test_token_without_access_token(self):
        client, server = make()
        server.token_reply = (200, {"expires_in": 86400})
        with self.assertRaises(AvitoError):
            await client.self_id()
        self.assertEqual(server.api_calls(), [])
        self.assertIsNone(client._token, "пустой токен не кэшируется")


class TestErrors(unittest.IsolatedAsyncioTestCase):
    async def test_403_gets_new_token_and_one_retry(self):
        client, server = make()
        server.reply("GET", CHATS_PATH, (403, {"error": "expired"}), (200, {"chats": []}))
        self.assertEqual(await client.chats(), [])
        calls = server.api_calls(CHATS_PATH)
        self.assertEqual(len(calls), 2)
        self.assertEqual(bearer(calls[0]), "Bearer tok-1")
        self.assertEqual(bearer(calls[1]), "Bearer tok-2", "повтор - с новым токеном")
        self.assertEqual(calls[0]["params"], calls[1]["params"], "повтор - тот же запрос")
        self.assertEqual(len(server.token_calls()), 2)
        # Новый токен дальше живёт в кэше.
        await client.chats()
        self.assertEqual(len(server.token_calls()), 2)

    async def test_second_403_is_an_error(self):
        client, server = make()
        server.reply("GET", CHATS_PATH, (403, {"error": "forbidden"}))
        with self.assertRaises(AvitoError) as err:
            await client.chats()
        self.assertEqual(err.exception.status, 403)
        self.assertEqual(len(server.api_calls(CHATS_PATH)), 2, "ровно один повтор")
        self.assertEqual(len(server.token_calls()), 2)

    async def test_403_retry_keeps_the_body(self):
        client, server = make()
        server.reply("POST", send_path("c1"), (403, {}), (200, {"id": "m-1"}))
        got = await client.send_text("c1", "Здравствуйте")
        self.assertEqual(got, {"id": "m-1"})
        calls = server.api_calls(send_path("c1"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["json"], calls[0]["json"])

    async def test_402_is_the_tariff_not_a_retry(self):
        client, server = make()
        server.reply("GET", CHATS_PATH, (402, {"error": "payment required"}))
        with self.assertRaises(AvitoError) as err:
            await client.chats()
        self.assertEqual(err.exception.status, 402)
        self.assertIn("402", str(err.exception))
        self.assertEqual(len(server.api_calls(CHATS_PATH)), 1, "402 не лечится повтором")
        self.assertEqual(len(server.token_calls()), 1)

    async def test_429_is_not_retried(self):
        client, server = make()
        server.reply("GET", CHATS_PATH, (429, {}))
        with self.assertRaises(AvitoError) as err:
            await client.chats()
        self.assertEqual(err.exception.status, 429)
        self.assertEqual(len(server.api_calls(CHATS_PATH)), 1)

    async def test_other_errors_keep_status(self):
        client, server = make()
        server.reply("GET", CHATS_PATH, (500, {}))
        with self.assertRaises(AvitoError) as err:
            await client.chats()
        self.assertEqual(err.exception.status, 500)
        self.assertIn(CHATS_PATH, str(err.exception))
        self.assertEqual(len(server.api_calls(CHATS_PATH)), 1, "500 не повторяется")

    async def test_error_text_has_no_query(self):
        client, server = make()
        with self.assertRaises(AvitoError) as err:
            await client._call("GET", "x/y?client_secret=zzz")
        self.assertNotIn("zzz", str(err.exception), "хвост адреса в текст ошибки не идёт")
        self.assertIn("x/y", str(err.exception))

    async def test_body_is_read_inside_the_session(self):
        client, server = make()
        server.reply("GET", CHATS_PATH, (200, {"chats": [{"id": "c1"}]}))
        self.assertEqual([c["id"] for c in await client.chats()], ["c1"])
        self.assertEqual(server.read_outside, 0)
        self.assertEqual(server.open, 0, "сессии закрыты")

    async def test_not_json_body_is_an_error_not_empty(self):
        # HTML шлюза вместо JSON - не «чатов нет»: пустой ответ сдвинул бы
        # курсоры опроса за непрочитанное. Ошибка с текстом - в плашку панели.
        client, server = make()
        server.reply("GET", CHATS_PATH, (200, ValueError("<html>")))
        with self.assertRaises(avito.AvitoError) as err:
            await client.chats()
        self.assertIn("не разобрать", str(err.exception))


class TestNetwork(unittest.IsolatedAsyncioTestCase):
    """Сбой сети - AvitoError «Авито недоступен»: круг опроса ловит только
    его, всё прочее уронило бы круг мимо плашки в панели."""

    async def assert_unavailable(self, exc: BaseException) -> AvitoError:
        client, server = make()
        server.reply("GET", CHATS_PATH, exc)
        with self.assertRaises(AvitoError) as err:
            await client.chats()
        self.assertTrue(str(err.exception).startswith("Авито недоступен: "),
                        str(err.exception))
        self.assertIn(type(exc).__name__, str(err.exception))
        self.assertIsNone(err.exception.status, "ответа не было - и кода нет")
        self.assertIs(err.exception.__cause__, exc, "исходная причина - для журнала")
        self.assertEqual(server.open, 0, "сессия закрыта и после сбоя")
        self.assertEqual(len(server.api_calls(CHATS_PATH)), 1,
                         "без повтора: повторит следующий круг опроса")
        return err.exception

    async def test_os_and_timeout_errors(self):
        # asyncio.TimeoutError с 3.11 - тот же TimeoutError.
        for exc in (OSError(101, "Network is unreachable"), ConnectionResetError(),
                    ConnectionRefusedError(), TimeoutError(),
                    ssl.SSLError("certificate verify failed"),
                    ssl.SSLCertVerificationError("self-signed certificate")):
            with self.subTest(exc=type(exc).__name__):
                await self.assert_unavailable(exc)

    async def test_aiohttp_errors_by_module(self):
        # ClientError и родня наследуются от Exception, не от OSError:
        # узнаются по модулю aiohttp.
        for name, module in (("ServerDisconnectedError", "aiohttp.client_exceptions"),
                             ("ClientPayloadError", "aiohttp.client_exceptions"),
                             ("ClientConnectionError", "aiohttp.client_exceptions"),
                             ("BadHttpMessage", "aiohttp.http_exceptions")):
            with self.subTest(name=name):
                await self.assert_unavailable(aiohttp_like(name, module)("обрыв"))

    @unittest.skipUnless(aiohttp is not None, "aiohttp не установлен")
    async def test_real_aiohttp_errors(self):
        for exc in (aiohttp.ServerDisconnectedError(), aiohttp.ClientPayloadError("обрыв"),
                    aiohttp.ClientConnectionError("сброс"),
                    aiohttp.ServerTimeoutError("таймаут"), aiohttp.InvalidURL("x")):
            with self.subTest(exc=type(exc).__name__):
                await self.assert_unavailable(exc)

    async def test_error_text_is_the_type_not_the_message(self):
        # Текст исключения aiohttp несёт адрес и хвост запроса - в плашку
        # панели и журнал идёт только имя типа.
        leak = f"Cannot connect to host api.avito.ru:443 ?client_secret={SECRET}"
        for exc in (OSError(leak), aiohttp_like("ClientConnectorError")(leak)):
            with self.subTest(exc=type(exc).__name__):
                err = await self.assert_unavailable(exc)
                self.assertNotIn(SECRET, str(err))
                self.assertNotIn("api.avito.ru", str(err))

    async def test_foreign_errors_are_not_disguised(self):
        # Ошибка не сети и не aiohttp - это ошибка кода: «Авито недоступен»
        # спрятал бы её за вечной плашкой.
        for exc in (RuntimeError("баг"), KeyError("id"), AttributeError("get")):
            with self.subTest(exc=type(exc).__name__):
                client, server = make()
                server.reply("GET", CHATS_PATH, exc)
                with self.assertRaises(type(exc)) as err:
                    await client.chats()
                self.assertNotIsInstance(err.exception, AvitoError)
                self.assertEqual(server.open, 0)

    async def test_cancel_is_not_swallowed(self):
        # Остановка бота отменяет круг: отмена не становится «Авито недоступен».
        client, server = make()
        server.reply("GET", CHATS_PATH, asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await client.chats()
        self.assertEqual(server.open, 0)

    async def test_token_network_error_then_recovers(self):
        client, server = make()
        server.token_reply = ConnectionResetError()
        with self.assertRaises(AvitoError) as err:
            await client.self_id()
        self.assertIn("Авито недоступен", str(err.exception))
        self.assertIsNone(client._token)
        self.assertEqual(server.api_calls(), [], "без токена в API не ходим")
        self.assertFalse(client._lock.locked(), "замок токена отпущен")
        server.token_reply = None
        self.assertEqual(await client.self_id(), OWN, "следующий круг - как обычно")
        self.assertEqual(bearer(server.api_calls(SELF_PATH)[0]), "Bearer tok-1")

    async def test_network_error_after_403_is_unavailable(self):
        # Повтор после 403 упал по сети - та же AvitoError, а не 403.
        client, server = make()
        server.reply("GET", CHATS_PATH, (403, {}), TimeoutError())
        with self.assertRaises(AvitoError) as err:
            await client.chats()
        self.assertIn("Авито недоступен", str(err.exception))
        self.assertIsNone(err.exception.status)
        self.assertEqual(len(server.api_calls(CHATS_PATH)), 2)

    async def test_send_network_error_is_an_error(self):
        # Не «ушло»: ответа не было, и оператор видит «не ушло» с причиной.
        for exc in (TimeoutError(), aiohttp_like("ServerDisconnectedError")()):
            with self.subTest(exc=type(exc).__name__):
                client, server = make()
                server.reply("POST", send_path("c1"), exc)
                with self.assertRaises(AvitoError) as err:
                    await client.send_text("c1", "Здравствуйте")
                self.assertIn("Авито недоступен", str(err.exception))
                self.assertEqual(len(server.api_calls(send_path("c1"))), 1,
                                 "без повтора: второй раз - дубль человеку")

    async def test_body_cut_mid_read(self):
        # Обрыв при чтении тела: у чтения - ошибка, у отправки - «ушло»
        # (код 200 уже пришёл, повтор был бы вторым сообщением).
        cut = aiohttp_like("ClientPayloadError")("Response payload is not completed")
        client, server = make()
        server.reply("GET", CHATS_PATH, (200, cut))
        with self.assertRaises(AvitoError) as err:
            await client.chats()
        self.assertIn("не разобрать", str(err.exception))
        server.reply("POST", send_path("c1"), (200, cut))
        self.assertEqual(await client.send_text("c1", "ок"), {})


class TestEmptyBody(unittest.IsolatedAsyncioTestCase):
    """2xx с пустым или нечитаемым телом: у чтения - AvitoError (иначе опрос
    сдвинул бы курсор за непрочитанное), у отправки - «ушло»."""

    EMPTY = (None, ValueError("<html>"))

    async def test_reads_refuse_empty_2xx(self):
        for status in (200, 204):
            for payload in self.EMPTY:
                with self.subTest(status=status, payload=payload):
                    client, server = make()
                    server.reply("GET", CHATS_PATH, (status, payload))
                    server.reply("GET", msg_path("c1"), (status, payload))
                    with self.assertRaises(AvitoError) as err:
                        await client.chats()
                    self.assertIn("не разобрать", str(err.exception))
                    with self.assertRaises(AvitoError) as err:
                        await client.messages("c1")
                    self.assertIn("не разобрать", str(err.exception))

    async def test_self_id_refuses_empty(self):
        client, server = make()
        server.reply("GET", SELF_PATH, (200, None), (200, {"id": OWN}))
        with self.assertRaises(AvitoError):
            await client.self_id()
        self.assertEqual(server.api_calls(CHATS_PATH), [], "без id дальше не идём")
        self.assertEqual(await client.self_id(), OWN)

    async def test_error_text_has_no_query(self):
        client, server = make()
        server.reply("GET", "x/y", (200, None))
        with self.assertRaises(AvitoError) as err:
            await client._call("GET", "x/y?client_secret=zzz")
        self.assertNotIn("zzz", str(err.exception))

    async def test_error_status_wins_over_empty_body(self):
        # Пустое тело при 4xx/5xx - всё равно код ответа, а не «не разобрать».
        for status in (402, 429, 500, 502):
            with self.subTest(status=status):
                client, server = make()
                server.reply("GET", CHATS_PATH, (status, None))
                with self.assertRaises(AvitoError) as err:
                    await client.chats()
                self.assertEqual(err.exception.status, status)
                self.assertNotIn("не разобрать", str(err.exception))

    async def test_read_after_403_still_refuses_empty(self):
        client, server = make()
        server.reply("GET", msg_path("c1"), (403, {}), (200, None))
        with self.assertRaises(AvitoError):
            await client.messages("c1")
        self.assertEqual(len(server.api_calls(msg_path("c1"))), 2)

    async def test_send_empty_2xx_is_sent(self):
        for status in (200, 201, 204):
            for payload in self.EMPTY:
                with self.subTest(status=status, payload=payload):
                    client, server = make()
                    server.reply("POST", send_path("c1"), (status, payload))
                    self.assertEqual(await client.send_text("c1", "Здравствуйте"), {})
                    self.assertEqual(len(server.api_calls(send_path("c1"))), 1,
                                     "ушло один раз - без повтора")

    async def test_send_empty_after_403_is_sent(self):
        # Повтор после 403 помнит, что это отправка: пустой 200 - «ушло».
        client, server = make()
        server.reply("POST", send_path("c1"), (403, None), (200, None))
        self.assertEqual(await client.send_text("c1", "Здравствуйте"), {})
        self.assertEqual(len(server.api_calls(send_path("c1"))), 2)

    async def test_send_error_status_with_empty_body_is_an_error(self):
        client, server = make()
        server.reply("POST", send_path("c1"), (500, None))
        with self.assertRaises(AvitoError) as err:
            await client.send_text("c1", "Здравствуйте")
        self.assertEqual(err.exception.status, 500)

    async def test_token_body_not_an_object(self):
        # Ответ токена ни объектом (строка, список, число) - та же AvitoError,
        # а не AttributeError мимо `except AvitoError` в круге опроса.
        for status, payload in ((200, None), (200, "ok"), (200, ["tok"]), (200, 42),
                                (503, "Service Unavailable"), (503, [])):
            with self.subTest(status=status, payload=payload):
                client, server = make()
                server.token_reply = (status, payload)
                with self.assertRaises(AvitoError):
                    await client.self_id()
                self.assertEqual(server.api_calls(), [])
                self.assertIsNone(client._token)


class TestSelfId(unittest.IsolatedAsyncioTestCase):
    async def test_self_id_asked_once(self):
        client, server = make()
        server.reply("GET", CHATS_PATH, (200, {"chats": []}))
        server.reply("GET", msg_path("c1"), (200, []))
        server.reply("POST", send_path("c1"), (200, {"id": "m-1"}))
        self.assertEqual(await client.self_id(), OWN)
        await client.chats()
        await client.messages("c1")
        await client.send_text("c1", "ок")
        self.assertEqual(await client.self_id(), OWN)
        self.assertEqual(len(server.api_calls(SELF_PATH)), 1)

    async def test_self_id_from_string(self):
        client, server = make()
        server.reply("GET", SELF_PATH, (200, {"id": "777"}))
        self.assertEqual(await client.self_id(), 777)

    async def test_bad_self_is_an_error_and_not_cached(self):
        for payload in ({}, {"id": None}, {"id": "abc"}, [], None):
            with self.subTest(payload=payload):
                client, server = make()
                server.reply("GET", SELF_PATH, (200, payload), (200, {"id": OWN}))
                with self.assertRaises(AvitoError):
                    await client.self_id()
                self.assertEqual(await client.self_id(), OWN, "следующий круг спросит снова")


class TestChats(unittest.IsolatedAsyncioTestCase):
    async def test_chats_path_and_types(self):
        client, server = make()
        server.reply("GET", CHATS_PATH, (200, {"chats": []}))
        await client.chats()
        await client.chats(limit=5)
        first, second = server.api_calls(CHATS_PATH)
        self.assertEqual(first["method"], "GET")
        self.assertEqual(first["url"], f"{avito.API_URL}/messenger/v2/accounts/{OWN}/chats")
        self.assertEqual(first["params"],
                         {"chat_types": "u2i,u2u", "limit": "100", "offset": "0"})
        self.assertEqual(second["params"]["limit"], "5")
        await client.chats(offset=100)
        third = server.api_calls(CHATS_PATH)[2]
        self.assertEqual(third["params"]["offset"], "100")

    async def test_chats_are_parsed_with_own_id(self):
        client, server = make()
        server.reply("GET", CHATS_PATH, (200, {"chats": [
            {"id": "u2i-1", "updated": 1789000000,
             "users": [{"id": OWN, "name": "МАЙБАЙК"}, {"id": 555, "name": "Иван"}],
             "context": {"type": "item", "value": {"title": "Монстр 60В",
                                                    "url": "https://www.avito.ru/x_1"}},
             "last_message": {"id": "m-9"}},
            {"id": ""},                      # без id - мимо
            "мусор",
            {"users": []},
        ]}))
        got = await client.chats()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["id"], "u2i-1")
        self.assertEqual(got[0]["name"], "Иван", "собеседник, а не мы")
        self.assertEqual(got[0]["last_id"], "m-9")
        self.assertEqual(got[0]["subject"], "Монстр 60В")

    async def test_chats_odd_payload(self):
        for payload in ([], {"chats": None}, {"other": 1}):
            with self.subTest(payload=payload):
                client, server = make()
                server.reply("GET", CHATS_PATH, (200, payload))
                self.assertEqual(await client.chats(), [])
        client, server = make()
        server.reply("GET", CHATS_PATH, (200, None))
        with self.assertRaises(avito.AvitoError):
            await client.chats()


class TestMessages(unittest.IsolatedAsyncioTestCase):
    RAW = [
        {"id": "m-2", "author_id": 555, "created": 1789000100, "type": "text",
         "content": {"text": "Есть свободные?"}},
        {"id": "m-1", "author_id": OWN, "created": 1789000000, "type": "text",
         "content": {"text": "Здравствуйте"}},
        {"id": "", "type": "text", "content": {"text": "без id"}},
        "мусор",
    ]

    async def test_messages_path_v3_with_slash(self):
        client, server = make()
        server.reply("GET", msg_path("u2i-1"), (200, []))
        await client.messages("u2i-1")
        await client.messages("u2i-1", limit=20)
        first, second = server.api_calls(msg_path("u2i-1"))
        self.assertEqual(first["method"], "GET")
        self.assertTrue(first["url"].endswith("/messages/"), "без слэша Авито отвечает 404")
        self.assertIn("/messenger/v3/", first["url"])
        self.assertEqual(first["params"], {"limit": "100"})
        self.assertEqual(second["params"], {"limit": "20"})

    async def test_list_and_dict_answers_read_the_same(self):
        results = []
        for payload in (list(self.RAW), {"messages": list(self.RAW)}):
            client, server = make()
            server.reply("GET", msg_path("c1"), (200, payload))
            results.append(await client.messages("c1"))
        self.assertEqual(results[0], results[1])
        got = results[0]
        self.assertEqual([m["id"] for m in got], ["m-2", "m-1"])
        self.assertEqual([m["own"] for m in got], [False, True], "свой - по id аккаунта")
        self.assertEqual(got[0]["text"], "Есть свободные?")

    async def test_messages_odd_payload(self):
        for payload in ({}, {"messages": None}):
            with self.subTest(payload=payload):
                client, server = make()
                server.reply("GET", msg_path("c1"), (200, payload))
                self.assertEqual(await client.messages("c1"), [])

    async def test_messages_unreadable_payload_is_an_error(self):
        # Пустое тело или ответ ни списком, ни объектом - AvitoError, а не
        # пустая лента (опрос сдвинул бы курсор чата за непрочитанное) и не
        # AttributeError мимо `except AvitoError` в круге опроса.
        for payload in (None, "текст", 42, True):
            with self.subTest(payload=payload):
                client, server = make()
                server.reply("GET", msg_path("c1"), (200, payload))
                with self.assertRaises(avito.AvitoError):
                    await client.messages("c1")

    async def test_chat_id_is_one_path_segment(self):
        # Номер чата из чужих данных не уводит запрос на другой путь API.
        client, server = make()
        server.reply("GET", msg_path("..%2F..%2Fcore%2Fv1%2Faccounts%2Fself"), (200, []))
        await client.messages("../../core/v1/accounts/self")
        (call,) = server.api_calls(msg_path("..%2F..%2Fcore%2Fv1%2Faccounts%2Fself"))
        self.assertNotIn("/../", call["url"])

    async def test_chat_id_is_percent_encoded(self):
        # Номер чата - один сегмент: «?», «#», «/», пробел и «%» кодируются,
        # уже закодированное кодируется ещё раз и не раскрывается в «/».
        cases = {
            "u2i-Abc_1.2~3": "u2i-Abc_1.2~3",            # обычный номер как есть
            " u2i-1 ": "u2i-1",                           # пробелы по краям срезаны
            "a?limit=1#x": "a%3Flimit%3D1%23x",
            "a/b": "a%2Fb",
            "a b": "a%20b",
            "..%2F": "..%252F",
            "чат": "%D1%87%D0%B0%D1%82",
        }
        for chat_id, segment in cases.items():
            with self.subTest(chat_id=chat_id):
                client, server = make()
                server.reply("GET", msg_path(segment), (200, []))
                self.assertEqual(await client.messages(chat_id), [])
                (call,) = messenger_calls(server)
                self.assertEqual(call["path"], msg_path(segment))

    async def test_empty_chat_id_refused(self):
        for chat_id in ("", "   ", None):
            with self.subTest(chat_id=chat_id):
                client, server = make()
                with self.assertRaises(AvitoError):
                    await client.messages(chat_id)
                self.assertEqual(messenger_calls(server), [], "в сообщения не ходим")

    async def test_dot_chat_id_does_not_climb(self):
        # «.» и «..» quote не трогает (точка - незарезервированный знак), а
        # клиент по RFC 3986 схлопывает такие сегменты: chats/../messages/
        # ушёл бы на accounts/777/messages/. Такой номер - не один сегмент.
        for chat_id in (".", "..", " .. "):
            with self.subTest(chat_id=chat_id):
                client, server = make()
                with contextlib.suppress(AvitoError):
                    await client.messages(chat_id)
                for call in messenger_calls(server):
                    self.assertTrue({".", ".."}.isdisjoint(call["path"].split("/")),
                                    call["path"])


class TestSendText(unittest.IsolatedAsyncioTestCase):
    async def test_send_path_v1_and_body(self):
        client, server = make()
        server.reply("POST", send_path("u2i-1"), (200, {"id": "m-10", "type": "text"}))
        got = await client.send_text("u2i-1", "  Велосипед свободен  ")
        self.assertEqual(got["id"], "m-10")
        (call,) = server.api_calls(send_path("u2i-1"))
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"],
                         f"{avito.API_URL}/messenger/v1/accounts/{OWN}/chats/u2i-1/messages")
        self.assertEqual(call["json"], {"message": {"text": "Велосипед свободен"},
                                        "type": "text"})
        self.assertEqual(bearer(call), "Bearer tok-1")

    async def test_send_chat_id_is_one_path_segment(self):
        client, server = make()
        segment = "..%2F..%2Fcore%2Fv1%2Faccounts%2Fself"
        server.reply("POST", send_path(segment), (200, {"id": "m-1"}))
        self.assertEqual(await client.send_text("../../core/v1/accounts/self", "ок"),
                         {"id": "m-1"})
        (call,) = messenger_calls(server)
        self.assertEqual(call["path"], send_path(segment))
        self.assertNotIn("/../", call["url"])

    async def test_send_empty_chat_id_refused(self):
        for chat_id in ("", "  ", None):
            with self.subTest(chat_id=chat_id):
                client, server = make()
                with self.assertRaises(AvitoError):
                    await client.send_text(chat_id, "ок")
                self.assertEqual(messenger_calls(server), [], "ответ никуда не ушёл")

    async def test_send_dot_chat_id_does_not_climb(self):
        for chat_id in (".", ".."):
            with self.subTest(chat_id=chat_id):
                client, server = make()
                with contextlib.suppress(AvitoError):
                    await client.send_text(chat_id, "ок")
                for call in messenger_calls(server):
                    self.assertTrue({".", ".."}.isdisjoint(call["path"].split("/")),
                                    call["path"])

    async def test_send_answer_not_dict(self):
        client, server = make()
        server.reply("POST", send_path("c1"), (200, None))
        self.assertEqual(await client.send_text("c1", "ок"), {})

    async def test_empty_refused_before_network(self):
        for text in ("", "   \n\t", None):
            with self.subTest(text=text):
                client, server = make()
                with self.assertRaises(AvitoError):
                    await client.send_text("c1", text)
                self.assertEqual(server.calls, [])
                self.assertEqual(server.sessions, 0, "сеть даже не открывалась")

    async def test_too_long_refused_before_network(self):
        client, server = make()
        with self.assertRaises(AvitoError) as err:
            await client.send_text("c1", "а" * (avito.MESSAGE_LIMIT + 1))
        self.assertIn(str(avito.MESSAGE_LIMIT), str(err.exception))
        self.assertEqual(server.calls, [])
        self.assertEqual(server.sessions, 0)

    async def test_exactly_the_limit_is_sent(self):
        client, server = make()
        server.reply("POST", send_path("c1"), (200, {"id": "m-1"}))
        text = "б" * avito.MESSAGE_LIMIT
        await client.send_text("c1", "  " + text + "\n")
        (call,) = server.api_calls(send_path("c1"))
        self.assertEqual(call["json"]["message"]["text"], text, "предел - после обрезки")

    def test_limit_is_avitos(self):
        self.assertEqual(avito.MESSAGE_LIMIT, 1000)


class TestParseChat(unittest.TestCase):
    RAW = {
        "id": 123, "updated": 1789000000,
        "users": [{"id": OWN, "name": "МАЙБАЙК"}, {"id": 555, "name": "  Иван  "}],
        "context": {"type": "item", "value": {"id": 9, "title": "  Монстр 60В ",
                                              "url": " https://www.avito.ru/kazan/x_9 "}},
        "last_message": {"id": "m-5", "content": {"text": "привет"}},
    }

    def test_other_user_and_context(self):
        got = avito.parse_chat(self.RAW, OWN)
        self.assertEqual(got, {
            "id": "123",
            "updated": datetime.fromtimestamp(1789000000, UTC),
            "last_id": "m-5",
            "name": "Иван",
            "subject": "Монстр 60В",
            "url": "https://www.avito.ru/kazan/x_9",
        })

    def test_other_user_wherever_we_stand(self):
        raw = dict(self.RAW, users=[{"id": 555, "name": "Иван"}, {"id": OWN, "name": "Мы"}])
        self.assertEqual(avito.parse_chat(raw, OWN)["name"], "Иван")
        self.assertEqual(avito.parse_chat(self.RAW, str(OWN))["name"], "Иван",
                         "id аккаунта строкой - тот же аккаунт")
        raw = dict(self.RAW, users=[{"id": "777", "name": "Мы"}, {"id": "555", "name": "Иван"}])
        self.assertEqual(avito.parse_chat(raw, OWN)["name"], "Иван", "id строкой в ответе")

    def test_only_us_in_chat(self):
        raw = dict(self.RAW, users=[{"id": OWN, "name": "МАЙБАЙК"}])
        self.assertIsNone(avito.parse_chat(raw, OWN)["name"], "себя собеседником не зовём")

    def test_own_id_unknown_takes_first(self):
        self.assertEqual(avito.parse_chat(self.RAW, None)["name"], "МАЙБАЙК")

    def test_bare_chat(self):
        got = avito.parse_chat({"id": "c1"}, OWN)
        self.assertEqual(got, {"id": "c1", "updated": None, "last_id": None, "name": None,
                               "subject": None, "url": None})
        got = avito.parse_chat({"id": "c1", "context": {"value": None},
                                "last_message": None, "users": None}, OWN)
        self.assertIsNone(got["subject"])
        self.assertIsNone(got["last_id"])

    def test_subject_only_from_context_value(self):
        raw = {"id": "c1", "context": {"title": "не тут", "url": "https://avito.ru/no"}}
        got = avito.parse_chat(raw, OWN)
        self.assertIsNone(got["subject"])
        self.assertIsNone(got["url"])


class TestParseMessage(unittest.TestCase):
    @staticmethod
    def msg(kind="text", content=None, **over) -> dict:
        raw = {"id": "m-1", "author_id": 555, "created": 1789000000, "type": kind,
               "content": {"text": "Сколько стоит неделя?"} if content is None else content}
        raw.update(over)
        return raw

    def test_plain_text(self):
        got = avito.parse_message(self.msg(), OWN)
        self.assertEqual(got, {"id": "m-1", "author_id": 555,
                               "created": datetime.fromtimestamp(1789000000, UTC),
                               "kind": "text", "text": "Сколько стоит неделя?",
                               "noise": False, "own": False})

    def test_system_and_deleted_are_noise(self):
        for kind in ("system", "deleted"):
            with self.subTest(kind=kind):
                got = avito.parse_message(self.msg(kind, {"text": "Чат создан"}), OWN)
                self.assertTrue(got["noise"])
                self.assertEqual(got["kind"], "other")

    def test_flow_id_is_the_avito_bot(self):
        got = avito.parse_message(self.msg(content={"text": "Здравствуйте! Чем помочь?",
                                                    "flow_id": "f-1"}), OWN)
        self.assertTrue(got["noise"], "автоответ чат-бота Авито - не слова клиента")
        got = avito.parse_message(self.msg(content={"text": "Здравствуйте",
                                                    "flow_id": ""}), OWN)
        self.assertFalse(got["noise"])

    def test_placeholder_phrases_are_noise(self):
        for phrase in avito.NOISE_PHRASES:
            for text in (phrase, phrase.upper(), f"Чтобы ответить, {phrase.capitalize()}."):
                with self.subTest(text=text):
                    got = avito.parse_message(self.msg(content={"text": text}), OWN)
                    self.assertTrue(got["noise"], text)
        got = avito.parse_message(self.msg(content={
            "text": "Для просмотра сообщения перейдите на подписку с доступом к API"}), OWN)
        self.assertTrue(got["noise"])
        got = avito.parse_message(self.msg(content={"text": "Подписку на прокат можно?"}),
                                  OWN)
        self.assertFalse(got["noise"])

    def test_kinds(self):
        cases = {
            "text": ({"text": "привет"}, "text", "привет"),
            "link": ({"link": {"text": "смотри", "url": "https://x.ru"}}, "text",
                     "смотри https://x.ru"),
            "image": ({"image": {"sizes": {}}}, "image", None),
            "voice": ({"voice": {"voice_id": "v"}}, "voice", None),
            "call": ({"call": {"status": "missed"}}, "call", None),
            "file": ({"file": {"name": "a.pdf"}}, "file", None),
            "item": ({"item": {"title": "Монстр 60В"}}, "other", "Монстр 60В"),
            "location": ({"location": {"title": "Казань", "text": "ул. Павлюхина"}},
                         "other", "ул. Павлюхина"),
            "video": ({}, "other", None),
            "": ({"text": "без типа"}, "other", "без типа"),
        }
        for kind, (content, want_kind, want_text) in cases.items():
            with self.subTest(kind=kind):
                got = avito.parse_message(self.msg(kind, content), OWN)
                self.assertEqual(got["kind"], want_kind)
                self.assertEqual(got["text"], want_text)
                self.assertFalse(got["noise"])

    def test_link_without_text_is_the_url(self):
        got = avito.parse_message(self.msg("link", {"link": {"url": "https://x.ru"}}), OWN)
        self.assertEqual(got["text"], "https://x.ru")
        got = avito.parse_message(self.msg("location", {"location": {"title": "Казань"}}),
                                  OWN)
        self.assertEqual(got["text"], "Казань")

    def test_missing_type_and_content(self):
        got = avito.parse_message({"id": 5}, OWN)
        self.assertEqual(got["id"], "5")
        self.assertEqual(got["kind"], "other")
        self.assertIsNone(got["text"])
        self.assertIsNone(got["created"])
        self.assertFalse(got["noise"])
        self.assertFalse(got["own"])

    def test_blank_text_is_empty(self):
        # Пустая строка или None - запись всё равно кладёт None (inbox_in).
        got = avito.parse_message(self.msg(content={"text": "   "}), OWN)
        self.assertFalse(got["text"])
        self.assertFalse(got["noise"])

    def test_own_flag(self):
        self.assertTrue(avito.parse_message(self.msg(author_id=OWN), OWN)["own"])
        self.assertTrue(avito.parse_message(self.msg(author_id=str(OWN)), OWN)["own"])
        self.assertTrue(avito.parse_message(self.msg(author_id=OWN), str(OWN))["own"])
        self.assertFalse(avito.parse_message(self.msg(author_id=555), OWN)["own"])
        self.assertFalse(avito.parse_message(self.msg(author_id=OWN), None)["own"],
                         "без id аккаунта своё не опознать")
        self.assertFalse(avito.parse_message(self.msg(author_id=None), OWN)["own"])

    def test_author_id_digits_become_int(self):
        self.assertEqual(avito.parse_message(self.msg(author_id="555"), OWN)["author_id"], 555)
        self.assertEqual(avito.parse_message(self.msg(author_id="bot"), OWN)["author_id"],
                         "bot")
        self.assertIsNone(avito.parse_message(self.msg(author_id=None), OWN)["author_id"])

    def test_bad_created_is_none(self):
        for bad in (None, "", "вчера", "1.5", {}, [], 10**30, -10**15):
            with self.subTest(created=bad):
                self.assertIsNone(avito.parse_message(self.msg(created=bad), OWN)["created"])
        got = avito.parse_message(self.msg(created="1789000000"), OWN)["created"]
        self.assertEqual(got, datetime.fromtimestamp(1789000000, UTC))
        self.assertEqual(got.tzinfo, UTC)


if __name__ == "__main__":
    unittest.main()
