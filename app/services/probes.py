"""Замеры здоровья сервера: место на диске, живость панели, сертификат.

Только замер, без суждений: что считать бедой, решает
`crm.logic.health_problems`, а когда и кому писать - `crm.health`. Так
пороги проверяются тестом без сети и без диска.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import ssl
from datetime import UTC, datetime
from pathlib import Path

TIMEOUT = 10


def disk_usage(path: str | Path) -> tuple[int, int]:
    """Свободно и всего байт на файловой системе пути.

    `f_bavail`, а не `f_bfree`: запас, отложенный для root, процессу в
    контейнере не достанется, и считать его свободным - обманывать себя.
    """
    st = os.statvfs(str(path))
    return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize


async def http_status(url: str, *, timeout: float = TIMEOUT) -> int:
    """Код ответа страницы. Сеть не ответила - исключение."""
    import aiohttp  # локально: панели он не нужен

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.get(url, allow_redirects=False) as response:
            return response.status


async def cert_not_after(host: str, *, port: int = 443, timeout: float = TIMEOUT,
                         context: ssl.SSLContext | None = None) -> datetime:
    """До какого момента действует сертификат, который видит клиент.

    Проверка - та же, что у браузера (цепочка и имя): просроченный или
    чужой сертификат даёт исключение, и это тоже ответ на вопрос.
    `context` - только для тестов с самоподписанным сертификатом.
    """
    context = context or ssl.create_default_context()
    _, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=context, server_hostname=host),
        timeout)
    try:
        cert = writer.get_extra_info("peercert") or {}
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), 2)
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), UTC)
