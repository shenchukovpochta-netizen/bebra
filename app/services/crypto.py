"""Шифрование анкеты в базе.

Паспортные данные, адреса и телефоны нужны боту между шагами анкеты и до
момента выдачи договора - жить где-то они обязаны. Открытым текстом в bot.users
их держать нельзя: снапшот тома или дамп базы в этом случае равен утечке
паспортных данных всех клиентов сразу, а pg_dump в проекте делается руками
и складывается рядом с базой.

Поэтому в базу уезжает один текстовый столбец с шифротекстом, а ключ живёт
отдельным docker secret и в дамп не попадает. Расшифровать дамп без файла
ключа нечем.

Алгоритм - AES-256-GCM: аутентифицированное шифрование, порча шифротекста
обнаруживается при расшифровке, а не превращается в мусор в договоре.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)

NONCE_BYTES = 12          # рекомендованная длина для GCM
KEY_BYTES = 32            # AES-256
# Версия формата в начале токена: если алгоритм когда-нибудь сменится, старые
# записи надо будет уметь прочитать, а не молча отдать пустую анкету.
PREFIX = "v1:"


class KeyProblem(Exception):
    """Ключ отсутствует или непригоден."""


def load_key(raw: str | None) -> bytes:
    """Разбор ключа из secret-файла.

    Принимается base64 или hex - `openssl rand` умеет и то, и другое, и ошибка
    в выборе формата не должна стоить разбирательства на боевом сервере.
    """
    text = (raw or "").strip()
    if not text:
        raise KeyProblem("не задан ключ шифрования анкеты (PDN_KEY / PDN_KEY_FILE)")
    for decode in (base64.b64decode, bytes.fromhex):
        try:
            key = decode(text)
        except Exception:                                   # noqa: BLE001
            continue
        if len(key) == KEY_BYTES:
            return key
    raise KeyProblem(
        f"ключ анкеты должен быть {KEY_BYTES} байта в base64 или hex; "
        f"сгенерировать: openssl rand -base64 32"
    )


def generate_key() -> str:
    return base64.b64encode(os.urandom(KEY_BYTES)).decode("ascii")


class Vault:
    """Шифрование и расшифровка анкеты одним ключом."""

    def __init__(self, key: bytes) -> None:
        self._aead = AESGCM(key)

    @classmethod
    def from_raw(cls, raw: str | None) -> Vault:
        return cls(load_key(raw))

    def encrypt(self, data: dict[str, Any] | None) -> str | None:
        """dict -> строка для базы. None и пустой словарь дают None: пустая
        анкета должна выглядеть в базе как NULL, а не как валидный шифротекст."""
        if not data:
            return None
        nonce = os.urandom(NONCE_BYTES)
        raw = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        blob = nonce + self._aead.encrypt(nonce, raw, None)
        return PREFIX + base64.b64encode(blob).decode("ascii")

    def decrypt(self, token: str | None) -> dict[str, Any]:
        """Строка из базы -> dict. Пустой словарь, если расшифровать нечем.

        Ошибка расшифровки не роняет обработчик: анкета - не единственное,
        что есть у пользователя, и падение здесь заблокировало бы человеку
        вообще любое действие, включая /start. В лог пишется факт, но не
        содержимое: у логов нет срока удаления, который есть у анкеты.
        """
        if not token:
            return {}
        if not token.startswith(PREFIX):
            log.error("анкета в неизвестном формате, пропускаю")
            return {}
        try:
            blob = base64.b64decode(token[len(PREFIX):])
            raw = self._aead.decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], None)
            value = json.loads(raw.decode("utf-8"))
        except (InvalidTag, ValueError, TypeError) as exc:
            # InvalidTag - подмена ключа или порча данных. Разные ключи на двух
            # запусках бота дают ровно это, и без явного сообщения симптом
            # выглядит как «анкета внезапно опустела».
            log.error("не удалось расшифровать анкету: %s", type(exc).__name__)
            return {}
        return value if isinstance(value, dict) else {}
