"""Интеграция со StarLine: блокировка единицы при неоплате.

Велосипеды проката оснащены сигнализациями StarLine («дистанционное
включение с сигнализацией от брелка»). Через облачный API StarLine прокат
управляет СВОИМИ устройствами: ставит единицу на охрану - и она обездвижена,
мотор не включить - и снимает охрану. Это обычная телематика собственного
парка, как у каршеринга: владелец блокирует свою технику у неплательщика
и разблокирует сразу после оплаты или возврата.

Безопасность. Блокировка = постановка на охрану: StarLine обездвиживает
СТОЯЩУЮ единицу, а не глушит на ходу. Каждое действие пишется
в fleet.starline_log (кто, когда, чем закончилось), а автоблокировка
при просрочке по умолчанию ВЫКЛЮЧЕНА - включается флагом STARLINE_AUTO_BLOCK.

Адреса и формат запросов следуют публичному API StarLine
(id.starline.ru + developer.starline.ru). Команда «охрана» - параметр arm;
если конкретный блок мотора у ваших устройств называется иначе, это
один аргумент block_param, а не переписывание клиента.

Модуль изолирует сеть в двух методах (_get_json, _post) - их подменяет тест,
и вся цепочка авторизации и команд проверяется без единого запроса наружу.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

ID_BASE = "https://id.starline.ru/apiV3"
DEV_BASE = "https://developer.starline.ru/json"
APP_TOKEN_TTL = 3 * 3600          # StarLine отдаёт app-токен на 4 часа
TIMEOUT_SECONDS = 15
VOLTAGE_TTL_SECONDS = 300         # заряд меняется медленно, телеметрию кэшируем


class StarLineError(Exception):
    """Ошибка обращения к StarLine. Наружу пользователю не показывается."""


def md5_hex(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


# Ключи телеметрии, в которых устройства отдают напряжение батареи.
# У разных блоков поле называется по-разному; ищем рекурсивно первое
# значение, похожее на напряжение ТЯГОВОЙ батареи (диапазон отсекает
# бортовые 12 В, которые тоже зовутся battery).
_VOLTAGE_KEYS = frozenset({"battery", "voltage", "battery_voltage", "power_v"})
_VOLTAGE_MIN, _VOLTAGE_MAX = 30.0, 100.0


def extract_voltage(payload) -> float | None:
    """Первое похожее на напряжение тяговой батареи значение из телеметрии."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in _VOLTAGE_KEYS:
                try:
                    volts = float(value)
                except (TypeError, ValueError):
                    continue
                if _VOLTAGE_MIN <= volts <= _VOLTAGE_MAX:
                    return volts
        for value in payload.values():
            found = extract_voltage(value)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = extract_voltage(item)
            if found is not None:
                return found
    return None


class StarLine:
    """Клиент облака StarLine. Один на процесс; токены кэшируются и
    переполучаются при протухании."""

    def __init__(self, app_id: str, secret: str, login: str, password: str,
                 *, block_param: str = "arm") -> None:
        self.app_id = app_id
        self.secret = secret
        self.login = login
        self.password = password
        # Параметр команды «охрана/блокировка». arm - постановка на охрану,
        # обездвиживает единицу; при особой прошивке блока мотора поменяйте
        # здесь (или прокиньте из конфигурации), не трогая остальной код.
        self.block_param = block_param
        self._slnet: str | None = None
        self._app_token: str | None = None
        self._app_token_at: float = 0.0
        self._voltage_cache: dict[str, tuple[float, float | None]] = {}

    @classmethod
    def from_config(cls, cfg: Any) -> StarLine | None:
        """Клиент либо None, если StarLine в конфиге не настроен."""
        if not getattr(cfg, "starline_enabled", False):
            return None
        return cls(cfg.starline_app_id, cfg.starline_secret,
                   cfg.starline_login, cfg.starline_password)

    # ─────────────────────── публичные команды ───────────────────────

    async def block(self, device_id: str) -> bool:
        """Поставить единицу на охрану (обездвижить). True - команда принята."""
        return await self._control(device_id, 1)

    async def unblock(self, device_id: str) -> bool:
        """Снять охрану. True - команда принята."""
        return await self._control(device_id, 0)

    async def check(self) -> bool:
        """Проверка связи: пройти авторизацию. Для кнопки «проверить StarLine»."""
        try:
            await self._authenticate()
            return True
        except Exception:                               # noqa: BLE001
            log.exception("StarLine: проверка связи не удалась")
            return False

    async def voltage(self, device_id: str) -> float | None:
        """Напряжение тяговой батареи из телеметрии устройства.

        None - данных нет или они не похожи на напряжение: клиент увидит
        «заряд неизвестен», а не выдуманный процент. Ответ кэшируется
        на VOLTAGE_TTL: заряд меняется медленно, а каждый вход в Mini App
        не должен превращаться в запрос к StarLine.
        """
        cached = self._voltage_cache.get(device_id)
        if cached and time.monotonic() - cached[0] < VOLTAGE_TTL_SECONDS:
            return cached[1]
        data: dict = {}
        for attempt in (1, 2):
            try:
                if not self._slnet:
                    await self._authenticate()
                data = await self._get_json(
                    f"{DEV_BASE}/v3/device/{device_id}/data", {},
                    cookies={"slnet": self._slnet})
                break
            except Exception:                           # noqa: BLE001
                log.exception("StarLine: телеметрия %s не получена (попытка %s)",
                              device_id, attempt)
                self._slnet = None
        volts = extract_voltage(data)
        self._voltage_cache[device_id] = (time.monotonic(), volts)
        return volts

    # ─────────────────────── авторизация ───────────────────────

    async def _application_token(self) -> str:
        if self._app_token and time.monotonic() - self._app_token_at < APP_TOKEN_TTL:
            return self._app_token
        code_resp = await self._get_json(
            f"{ID_BASE}/application/getCode/",
            {"appId": self.app_id, "secret": md5_hex(self.secret)})
        code = (code_resp.get("desc") or {}).get("code")
        if not code:
            raise StarLineError(f"getCode вернул {code_resp!r}")
        token_resp = await self._get_json(
            f"{ID_BASE}/application/getToken/",
            {"appId": self.app_id, "secret": md5_hex(self.secret + code)})
        token = (token_resp.get("desc") or {}).get("token")
        if not token:
            raise StarLineError(f"getToken вернул {token_resp!r}")
        self._app_token, self._app_token_at = token, time.monotonic()
        return token

    async def _user_token(self, app_token: str) -> str:
        resp = await self._post(
            f"{ID_BASE}/user/login/", params={"token": app_token},
            data={"login": self.login, "pass": md5_hex(self.password)})
        token = resp.get("user_token") or (resp.get("desc") or {}).get("user_token")
        if not token:
            raise StarLineError(f"user/login вернул {resp!r}")
        return token

    async def _authenticate(self) -> None:
        app_token = await self._application_token()
        user_token = await self._user_token(app_token)
        resp = await self._post(f"{DEV_BASE}/v2/auth.slid",
                                json={"slid_token": user_token})
        desc = resp.get("desc") or {}
        self._slnet = resp.get("slnet") or desc.get("slnet")
        if not self._slnet:
            raise StarLineError(f"auth.slid вернул {resp!r}")

    # ─────────────────────── команда с переавторизацией ───────────────────────

    async def _control(self, device_id: str, value: int) -> bool:
        """set_param с одной попыткой переавторизации: slnet живёт недолго,
        и первый промах по протухшему токену - это не сбой, а сигнал
        авторизоваться заново."""
        for attempt in (1, 2):
            try:
                if not self._slnet:
                    await self._authenticate()
                resp = await self._post(
                    f"{DEV_BASE}/v1/device/{device_id}/set_param",
                    json={"type": self.block_param, self.block_param: value},
                    cookies={"slnet": self._slnet})
                if resp.get("code") in (200, None) or resp.get("state") == 1:
                    return True
                log.warning("StarLine set_param %s вернул %r", device_id, resp)
                self._slnet = None            # похоже на протухший токен
            except StarLineError as exc:
                log.warning("StarLine: %s (попытка %s)", exc, attempt)
                self._slnet = None
            except Exception:                           # noqa: BLE001
                log.exception("StarLine: команда устройству %s не прошла", device_id)
                self._slnet = None
        return False

    # ─────────────────────── сеть (подменяется в тестах) ───────────────────────

    async def _get_json(self, url: str, params: dict,
                        cookies: dict | None = None) -> dict:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout, cookies=cookies) as session:
            async with session.get(url, params=params) as resp:
                return await resp.json(content_type=None)

    async def _post(self, url: str, *, json: dict | None = None,
                    data: dict | None = None, params: dict | None = None,
                    cookies: dict | None = None) -> dict:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout, cookies=cookies) as session:
            async with session.post(url, json=json, data=data, params=params) as resp:
                return await resp.json(content_type=None)
