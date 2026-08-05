"""Чистые разборы апдейтов MAX - без сети и без базы, сюда смотрят тесты."""

from __future__ import annotations

import hashlib
import re

VCF_PHONE = re.compile(r"^TEL[^:]*:\s*(\+?[\d\-() ]{7,20})\s*$", re.M | re.I)


def phone_from_vcf(vcf: str | None) -> str | None:
    """Телефон из vCard, которую MAX кладёт в контакт-вложение."""
    if not vcf:
        return None
    m = VCF_PHONE.search(vcf)
    return m.group(1).strip() if m else None


def dedup_id(update: dict) -> int | None:
    """Устойчивый идентификатор апдейта для журнала bot.updates_log.

    У MAX нет сквозного update_id, как в Telegram, - только маркер пачки.
    Ключ собирается из содержимого: mid сообщения либо callback_id уникальны,
    и повторная доставка того же события даёт тот же идентификатор - клейм
    в базе отсеет дубль. Хэш режется до 60 бит, чтобы влезать в bigint.
    """
    kind = update.get("update_type")
    if kind == "message_created":
        key = "m:" + str(update.get("message", {}).get("body", {}).get("mid"))
    elif kind == "message_callback":
        key = "c:" + str(update.get("callback", {}).get("callback_id"))
    elif kind == "bot_started":
        key = f"s:{update.get('chat_id')}:{update.get('user', {}).get('user_id')}:" \
              f"{update.get('timestamp')}"
    else:
        return None
    return int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:15], 16)


def describe(update: dict) -> dict:
    """Плоский слепок апдейта: кто, где, что. Без текстов и телефонов -
    слепок уходит в журнал апдейтов, где другой срок хранения, чем у ПДн."""
    kind = update.get("update_type")
    if kind == "bot_started":
        user = update.get("user") or {}
        return {"kind": "start", "user_id": user.get("user_id"),
                "username": user.get("username"),
                "chat_id": update.get("chat_id"), "chat_type": "dialog",
                "mid": None, "callback_id": None, "payload_cb": None}
    if kind == "message_callback":
        cb = update.get("callback") or {}
        user = cb.get("user") or {}
        msg = update.get("message") or {}
        recipient = msg.get("recipient") or {}
        return {"kind": "callback", "user_id": user.get("user_id"),
                "username": user.get("username"),
                "chat_id": recipient.get("chat_id"),
                "chat_type": recipient.get("chat_type") or "dialog",
                "mid": (msg.get("body") or {}).get("mid"),
                "callback_id": cb.get("callback_id"),
                "payload_cb": cb.get("payload")}
    if kind == "message_created":
        msg = update.get("message") or {}
        sender = msg.get("sender") or {}
        recipient = msg.get("recipient") or {}
        body = msg.get("body") or {}
        link = msg.get("link") or {}
        return {"kind": "message", "user_id": sender.get("user_id"),
                "username": sender.get("username"),
                "sender_is_bot": bool(sender.get("is_bot")),
                "chat_id": recipient.get("chat_id"),
                "chat_type": recipient.get("chat_type") or "dialog",
                "mid": body.get("mid"),
                "text": body.get("text"),
                "attachments": body.get("attachments") or [],
                "reply_to_mid": ((link.get("message") or {}).get("mid")
                                 if link.get("type") == "reply" else None),
                "callback_id": None, "payload_cb": None}
    return {"kind": kind or "unknown", "user_id": None, "chat_id": None,
            "chat_type": None, "mid": None, "callback_id": None,
            "payload_cb": None}


def first_attachment(attachments: list, kind: str) -> dict | None:
    for att in attachments or []:
        if att.get("type") == kind:
            return att.get("payload") or {}
    return None


def contact_from(attachments: list) -> tuple[str | None, int | None]:
    """(телефон, user_id владельца) из контакт-вложения.

    user_id нужен той же проверке, что в Telegram: MAX позволяет отправить
    ЧУЖОЙ контакт, а в базу должен попасть только собственный номер.
    """
    payload = first_attachment(attachments, "contact")
    if payload is None:
        return None, None
    phone = phone_from_vcf(payload.get("vcf_info") or payload.get("vcfInfo"))
    info = payload.get("max_info") or payload.get("tam_info") or {}
    return phone, info.get("user_id")
