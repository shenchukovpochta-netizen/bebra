"""Распознавание документа.

По умолчанию Yandex Vision OCR: облако в российской юрисдикции, оплата
по запросам. Вендор-зависимого кода здесь минимум - формирование запроса;
разбор ответа лежит в logic.extract_yandex_vision и заменяется отдельно.

Альтернативы: SmartEngines Smart IDReader (on-premise SDK, данные не покидают
периметр), Cloud.ru и VK Cloud, self-hosted PaddleOCR/EasyOCR - последним
нужен препроцессинг в OpenCV.
"""

from __future__ import annotations

import base64
import logging

import aiohttp

from .. import logic
from ..config import Config

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=30)


async def recognize_raw(cfg: Config, image: bytes) -> dict | None:
    """Сырой ответ сервиса распознавания, либо None.

    None означает «распознать не удалось», а не «не совпало»: модератору важно
    различать эти случаи - в первом виноват сервис, во втором заявитель.
    """
    if not cfg.ocr_enabled:
        return None
    headers = {"Authorization": f"Api-Key {cfg.ocr_api_key}"}
    if cfg.ocr_folder_id:
        headers["x-folder-id"] = cfg.ocr_folder_id
    body = {
        "mimeType": "JPEG",
        "languageCodes": ["ru", "en"],
        "model": cfg.ocr_model,
        "content": base64.b64encode(image).decode("ascii"),
    }
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.post(cfg.ocr_url, json=body, headers=headers) as resp:
                if resp.status != 200:
                    # Тело ответа не логируем: при части ошибок провайдер
                    # возвращает эхо запроса, и в лог попали бы фрагменты
                    # распознанного документа. У логов нет срока удаления,
                    # который есть у самих сканов.
                    log.warning("OCR ответил %s", resp.status)
                    return None
                return await resp.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:  # ValueError = битый JSON
        log.warning("OCR недоступен: %s", exc)
        return None


async def recognize_and_match(cfg: Config, image: bytes,
                              full_name: str | None) -> logic.OcrResult | None:
    """Возвращает logic.OcrResult либо None, если распознавание не состоялось."""
    payload = await recognize_raw(cfg, image)
    if payload is None:
        return None
    return logic.match_name(full_name, payload)
