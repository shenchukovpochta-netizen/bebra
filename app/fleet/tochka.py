"""Оплата СБП через API Точка-банка: динамические QR-счета.

Вместо статичной ссылки на счёт (PAY_URL) - персональный счёт на точную
сумму: CRM регистрирует динамический QR НСПК через открытое API Точки
(enter.tochka.com/uapi), клиент платит по ссылке, а фоновая задача
опрашивает статус и сама помечает счёт оплаченным - и снимает блокировку
StarLine, если единица была обездвижена за неоплату.

Опрос, а не вебхук - сознательно, в духе long polling самого бота:
вебхуку нужен публичный адрес и подпись, а статус СБП-платежа меняется
за секунды и опрос раз в минуту его не теряет.

Реквизиты - из личного кабинета Точки: токен доступа к API, merchantId
СБП и accountId (номер счёта/БИК). Суммы в API уходят в копейках.

Сеть изолирована в _get/_post - их подменяет тест, и регистрация счёта
со статусами проверяется без банка. Имена статусов СБП сведены
в PAID/REJECTED: если Точка добавит новое написание, это правка
константы, а не логики.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

BASE = "https://enter.tochka.com/uapi"
TIMEOUT_SECONDS = 20
# Сколько живёт неоплаченный счёт: и в API (ttl в минутах), и в опросе -
# старше суток помечается просроченным и перестаёт опрашиваться.
QR_TTL_MINUTES = 24 * 60

# Статусы платежа по динамическому QR. НСПК отвечает кодами ISO
# (ACWP/ACSC - исполнен, RJCT - отвергнут), Точка местами отдаёт и словами.
PAID_STATUSES = frozenset({"ACWP", "ACSC", "Accepted", "Confirmed", "PAID"})
REJECTED_STATUSES = frozenset({"RJCT", "Rejected", "REJECTED"})


class TochkaError(Exception):
    """Ошибка обращения к API Точки. Наружу клиенту не показывается."""


class Tochka:
    def __init__(self, token: str, merchant_id: str, account_id: str) -> None:
        self.token = token
        self.merchant_id = merchant_id
        self.account_id = account_id

    @classmethod
    def from_config(cls, cfg: Any) -> Tochka | None:
        if not getattr(cfg, "tochka_enabled", False):
            return None
        return cls(cfg.tochka_token, cfg.tochka_merchant_id, cfg.tochka_account_id)

    # ─────────────────────── операции ───────────────────────

    async def create_qr(self, amount_rub: int, purpose: str) -> dict:
        """Зарегистрировать динамический QR на сумму. Возвращает
        {"qrc_id", "payload"} - payload и есть ссылка оплаты СБП."""
        body = {"Data": {
            "amount": int(amount_rub) * 100,          # API считает в копейках
            "currency": "RUB",
            "paymentPurpose": purpose[:140],
            "qrcType": "02",                          # динамический, на одну оплату
            "ttl": QR_TTL_MINUTES,
            "sourceName": "mybike-crm",
        }}
        resp = await self._post(
            f"{BASE}/sbp/v1.0/qr-code/merchant/{self.merchant_id}/{self.account_id}",
            json=body)
        data = resp.get("Data") or {}
        qrc_id, payload = data.get("qrcId"), data.get("payload")
        if not qrc_id or not payload:
            raise TochkaError(f"регистрация QR вернула {resp!r}")
        return {"qrc_id": qrc_id, "payload": payload}

    async def payment_status(self, qrc_id: str) -> str:
        """'paid' | 'rejected' | 'pending'. Неизвестный статус - pending:
        деньги не подтверждены, значит, счёт не оплачен."""
        try:
            resp = await self._get(
                f"{BASE}/sbp/v1.0/qr-codes/{qrc_id}/payment-status")
        except TochkaError as exc:
            log.warning("Точка: статус %s не получен: %s", qrc_id, exc)
            return "pending"
        payments = (resp.get("Data") or {}).get("paymentList") or []
        for payment in payments:
            status = str(payment.get("status") or "")
            if status in PAID_STATUSES:
                return "paid"
            if status in REJECTED_STATUSES:
                return "rejected"
        return "pending"

    async def check(self) -> bool:
        """Проверка реквизитов: доступен ли счёт. Для лога на старте."""
        try:
            await self._get(f"{BASE}/open-banking/v1.0/accounts")
            return True
        except Exception:                               # noqa: BLE001
            log.exception("Точка: проверка доступа не прошла")
            return False

    # ─────────────────────── сеть (подменяется в тестах) ───────────────────────

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def _get(self, url: str) -> dict:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=self._headers()) as resp:
                if resp.status >= 400:
                    raise TochkaError(f"{url} -> HTTP {resp.status}")
                return await resp.json(content_type=None)

    async def _post(self, url: str, *, json: dict) -> dict:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=json, headers=self._headers()) as resp:
                if resp.status >= 400:
                    raise TochkaError(f"{url} -> HTTP {resp.status}: "
                                      f"{await resp.text()}")
                return await resp.json(content_type=None)
