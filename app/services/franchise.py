"""Запрос метрик у франчайзи: сеть и пределы, без базы и без разбора.

Отвечает чужой сервер, и доверять ему нельзя ни в чём: ни в размере, ни
в скорости, ни в адресе переадресации. Поэтому здесь - только https (http
лишь на localhost, для тестов), общий таймаут на весь запрос, чтение с
пределом (заявленная длина врёт, считаем полученное), без переадресаций
и только JSON. Разбор и проверка содержимого - logic.parse_metrics_bytes:
чистая функция, её проверяет тест без сети.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from ..crm import logic

CHUNK = 16 * 1024


class MetricsError(Exception):
    """Франчайзи не ответил как надо; текст - для карточки франчайзи."""


def _status_text(status: int) -> str:
    if status in (401, 403):
        return f"токен не принят ({status}): сверьте с secrets/metrics_token франчайзи"
    if status == 404:
        return ("метрики у франчайзи выключены (404): пустой secrets/metrics_token "
                "или адрес панели неверный")
    if status == 429:
        return "франчайзи просит реже (429)"
    if 300 <= status < 400:
        return f"переадресация ({status}): укажите точный адрес панели с https"
    return f"франчайзи ответил {status}"


def _session(timeout: float) -> Any:
    import aiohttp
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))


async def fetch_metrics(url: str, token: str, *, timeout: float = logic.METRICS_TIMEOUT,
                        max_bytes: int = logic.METRICS_MAX_BYTES,
                        session_factory: Any = None) -> bytes:
    """Тело ответа /hook/metrics. MetricsError - всё, что не «200, JSON, в
    пределах»: сеть, TLS, таймаут, статус, тип, размер."""
    base = url[:-len("/hook/metrics")] if url.endswith("/hook/metrics") else ""
    if not base or not logic.check_base_url(base).ok or urlsplit(url).query:
        raise MetricsError("адрес франчайзи негоден: только https://…")
    factory = session_factory or (lambda: _session(timeout))
    try:
        async with factory() as session, session.get(
                url, allow_redirects=False,
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/json"}) as response:
            if response.status != 200:
                raise MetricsError(_status_text(response.status))
            kind = (response.headers.get("Content-Type") or "").lower()
            if not kind.startswith("application/json"):
                raise MetricsError("ответ не JSON: проверьте адрес панели")
            declared = response.content_length
            if declared is not None and declared > max_bytes:
                raise MetricsError(f"ответ больше {max_bytes // 1024} КБ")
            body = bytearray()
            async for chunk in response.content.iter_chunked(CHUNK):
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise MetricsError(f"ответ больше {max_bytes // 1024} КБ")
            return bytes(body)
    except MetricsError:
        raise
    except (OSError, TimeoutError, ValueError) as exc:
        raise MetricsError(f"франчайзи не отвечает: {type(exc).__name__}") from exc
    except Exception as exc:                            # noqa: BLE001
        # aiohttp.ClientError и родня: обрыв, TLS, битый ответ. Для опроса
        # это то же «не отвечает», а не падение круга.
        if type(exc).__module__.startswith("aiohttp"):
            raise MetricsError(f"франчайзи не отвечает: {type(exc).__name__}") from exc
        raise
