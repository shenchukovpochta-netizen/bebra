"""StarLine Open API: где сейчас велосипед.

Цепочка авторизации у StarLine длиннее обычной и описана в их документации
для разработчиков. Она такая:

1. `application/getCode`  - код приложения по appId и md5(secret);
2. `application/getToken` - токен приложения, md5(secret + code), живёт 4 часа;
3. `user/login`           - токен пользователя (slid), sha1(пароль), сутки;
4. `auth.slid`            - обмен slid на cookie `slnet`, с ней ходят все
   остальные запросы;
5. `user/{id}/user_info`  - список устройств с координатами.

Каждый шаг отдаёт `state: 1` при успехе и код ошибки при неудаче, поэтому
ошибка разбирается одинаково для всех шагов.

Модуль умышленно ничего не знает про базу и про велосипеды: он отдаёт
нормализованные словари, а связь «устройство - велосипед» живёт в CRM.
Разбор ответа (`parse_device`) отделён от сети, чтобы его можно было
проверить тестом без интернета.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

ID_URL = "https://id.starline.ru/apiV3"
API_URL = "https://developer.starline.ru/json"
# Токен приложения живёт 4 часа, пользовательский - сутки. Берём с запасом:
# протухший токен стоит лишнего круга авторизации на каждом опросе.
APP_TOKEN_TTL = 3 * 3600
USER_TOKEN_TTL = 20 * 3600
TIMEOUT = 20
# Параметр set_param, которым StarLine блокирует мотор: режим
# «антиограбление». Блокировка вступает после остановки, не на ходу.
BLOCK_PARAM = "hijack"


class StarlineError(Exception):
    """Ответ StarLine с кодом ошибки или неожиданной формы."""


def md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()   # noqa: S324


def sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()  # noqa: S324


def check_state(data: Any, *, what: str) -> dict:
    """Разбор конверта StarLine: state = 1 и полезная часть в desc.

    Ошибка приходит не HTTP-статусом, а телом с кодом: без этой проверки
    «не авторизован» выглядел бы как пустой список устройств.
    """
    if not isinstance(data, dict):
        raise StarlineError(f"{what}: ответ не разобрать")
    state = data.get("state")
    desc = data.get("desc")
    if state != 1:
        # Скобки не для красоты: без них условное выражение забирало
        # весь «or», и при неожиданной форме ответа код ошибки терялся.
        code = data.get("code") or ((desc or {}).get("code")
                                    if isinstance(desc, dict) else None)
        raise StarlineError(f"{what}: StarLine отказал (state={state}, code={code})")
    return desc if isinstance(desc, dict) else {}


def parse_device(raw: dict) -> dict:
    """Устройство StarLine - в плоский словарь CRM.

    Координаты у StarLine лежат как x (долгота) и y (широта); перепутать
    их - значит увезти весь парк в Сомали, поэтому переименованы сразу.
    Отсутствующие поля остаются None: половина параметров зависит от
    модели устройства, и выдумывать нули за него нечестно.
    """
    position = raw.get("position") or {}
    alarm_state = raw.get("alarm_state") or {}
    common = raw.get("common") or {}
    recorded = position.get("ts") or common.get("gps_ts") or raw.get("ts")
    return {
        "device_id": str(raw.get("device_id") or raw.get("id") or "").strip(),
        "alias": (raw.get("alias") or raw.get("name") or "").strip() or None,
        "lat": _float(position.get("y")),
        "lon": _float(position.get("x")),
        "speed": _float(position.get("s")),
        "course": _int(position.get("dir")),
        "recorded_at": _moment(recorded),
        "voltage": _float(common.get("battery") or raw.get("battery")),
        "gsm_level": _int(common.get("gsm_lvl") or raw.get("gsm_lvl")),
        # Тревога - любая из поднятых StarLine: удар, наклон, движение
        # при охране. Разбирать их по отдельности смысла нет: оператору
        # всё равно ехать смотреть.
        "alarm": any(bool(v) for v in alarm_state.values()) if alarm_state else False,
        "online": bool(raw.get("status")) if "status" in raw else None,
    }


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _moment(value: Any) -> datetime | None:
    """Метка времени StarLine - секунды epoch в UTC."""
    seconds = _float(value)
    if not seconds:
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass
class StarlineClient:
    """Клиент StarLine с кэшем токенов.

    `session_factory` - фабрика aiohttp-сессии; в тестах подменяется
    заглушкой, чтобы проверить цепочку без интернета.
    """

    app_id: str
    app_secret: str
    login: str
    password: str
    id_url: str = ID_URL
    api_url: str = API_URL
    session_factory: Any = None
    _app_token: str = field(default="", repr=False)
    _app_token_at: float = 0.0
    _user_token: str = field(default="", repr=False)
    _user_token_at: float = 0.0
    _slnet: str = field(default="", repr=False)
    _user_id: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.app_id and self.app_secret and self.login and self.password)

    def _session(self):
        if self.session_factory is not None:
            return self.session_factory()
        import aiohttp  # локально: панели он не нужен
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT))

    async def _json(self, session, method: str, url: str, **kwargs) -> tuple[Any, Any]:
        response = await session.request(method, url, **kwargs)
        data = await response.json(content_type=None)
        return data, response

    async def app_token(self, session, *, now: float) -> str:
        if self._app_token and now - self._app_token_at < APP_TOKEN_TTL:
            return self._app_token
        data, _ = await self._json(
            session, "GET", f"{self.id_url}/application/getCode/",
            params={"appId": self.app_id, "secret": md5(self.app_secret)})
        code = check_state(data, what="getCode").get("code")
        if not code:
            raise StarlineError("getCode: StarLine не вернул код приложения")
        data, _ = await self._json(
            session, "GET", f"{self.id_url}/application/getToken/",
            params={"appId": self.app_id, "secret": md5(self.app_secret + str(code))})
        token = check_state(data, what="getToken").get("token")
        if not token:
            raise StarlineError("getToken: StarLine не вернул токен приложения")
        self._app_token, self._app_token_at = str(token), now
        return self._app_token

    async def user_token(self, session, *, now: float) -> str:
        if self._user_token and now - self._user_token_at < USER_TOKEN_TTL:
            return self._user_token
        token = await self.app_token(session, now=now)
        data, _ = await self._json(
            session, "POST", f"{self.id_url}/user/login/", params={"token": token},
            data={"login": self.login, "pass": sha1(self.password)})
        user_token = check_state(data, what="user/login").get("user_token")
        if not user_token:
            raise StarlineError("user/login: StarLine не вернул токен пользователя")
        self._user_token, self._user_token_at = str(user_token), now
        return self._user_token

    async def connect(self, session, *, now: float) -> str:
        """Обмен пользовательского токена на cookie slnet."""
        user_token = await self.user_token(session, now=now)
        data, response = await self._json(
            session, "POST", f"{self.api_url}/v2/auth.slid",
            json={"slid_token": user_token})
        if not isinstance(data, dict) or "user_id" not in data:
            raise StarlineError("auth.slid: StarLine не вернул пользователя")
        self._user_id = str(data["user_id"])
        cookie = _slnet_from(response)
        if not cookie:
            raise StarlineError("auth.slid: StarLine не отдал cookie slnet")
        self._slnet = cookie
        return cookie

    async def _call(self, session, method: str, path: str, *, now: float,
                    **kwargs) -> Any:
        """Запрос к API с cookie slnet и одним повтором при отказе авторизации.

        Cookie протухает молча, и первый же запрос после этого возвращает
        403. Повторять дальше нечего - это уже неверный пароль или
        блокировка кабинета.
        """
        for attempt in (1, 2):
            if not self._slnet:
                await self.connect(session, now=now)
            data, response = await self._json(
                session, method, f"{self.api_url}{path}",
                headers={"Cookie": f"slnet={self._slnet}"}, **kwargs)
            if getattr(response, "status", 200) in (401, 403) and attempt == 1:
                self._slnet = ""
                continue
            return data
        return None

    async def devices(self, *, now: float | None = None) -> list[dict]:
        """Все устройства кабинета в виде плоских словарей."""
        if not self.ready:
            return []
        now = now if now is not None else datetime.now(UTC).timestamp()
        session = self._session()
        try:
            data = await self._call(session, "GET",
                                    f"/v2/user/{self._user_id}/user_info", now=now)
            devices = (data or {}).get("devices") if isinstance(data, dict) else None
            if devices is None:
                raise StarlineError("user_info: StarLine не вернул устройства")
            return [d for d in (parse_device(x) for x in devices) if d["device_id"]]
        finally:
            close = getattr(session, "close", None)
            if close is not None:
                await close()

    async def set_param(self, device_id: str, name: str, value: int, *,
                        now: float | None = None) -> dict:
        """Команда устройству: `set_param` с телом {"type": имя, имя: 0|1}.

        Ответ v1 - не конверт state/desc, а {"code": 200, "codestring":
        "OK"}: всё, кроме 200, - отказ, и его текст уходит оператору как
        есть - StarLine пишет причину словами.
        """
        if not self.ready:
            raise StarlineError("StarLine не настроен: команду отправить нечем")
        now = now if now is not None else datetime.now(UTC).timestamp()
        session = self._session()
        try:
            data = await self._call(session, "POST",
                                    f"/v1/device/{device_id}/set_param", now=now,
                                    json={"type": name, name: int(value)})
            if not isinstance(data, dict):
                raise StarlineError("set_param: ответ StarLine не разобрать")
            code = data.get("code")
            if str(code) != "200":
                raise StarlineError(
                    f"set_param: StarLine отказал ({data.get('codestring') or code})")
            return data
        finally:
            close = getattr(session, "close", None)
            if close is not None:
                await close()

    async def block_motor(self, device_id: str, on: bool, *,
                          now: float | None = None) -> dict:
        """Заблокировать или разблокировать мотор.

        У StarLine это режим «антиограбление» (`hijack`): блокировка
        включается не на ходу, а после того, как велосипед остановился, -
        мотор перестаёт тянуть, когда тот уже стоит. Поэтому команда
        из панели безопасна для курьера на дороге.
        """
        return await self.set_param(device_id, BLOCK_PARAM, 1 if on else 0, now=now)


def _slnet_from(response: Any) -> str:
    """Cookie slnet из ответа: заголовком или через cookie_jar aiohttp."""
    cookies = getattr(response, "cookies", None)
    if cookies:
        value = cookies.get("slnet")
        if value is not None:
            return getattr(value, "value", str(value))
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("Set-Cookie") or headers.get("set-cookie") or ""
    for part in str(raw).split(";"):
        name, _, value = part.strip().partition("=")
        if name == "slnet" and value:
            return value
    return ""
