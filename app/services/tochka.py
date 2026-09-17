"""Точка Банк: выписка по счёту и платёжная ссылка с чеком 54-ФЗ.

Открытое API Точки (`enter.tochka.com/uapi`) работает по токену: его
выдают в личном кабинете банка, он живёт долго и ходит заголовком
`Authorization: Bearer`. Отдельной цепочки авторизации, как у StarLine,
здесь нет - поэтому и модуль короче.

Выписка берётся в два шага: сначала запрос на период, потом чтение
готового документа - банк собирает его не мгновенно. Оба шага здесь,
ожидание - у вызывающего: это его дело, ждать в цикле или прийти
следующим проходом.

Разбор ответов (`parse_transaction`) отделён от сети и проверяется
тестом без интернета - как и у StarLine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

log = logging.getLogger(__name__)

API_URL = "https://enter.tochka.com/uapi"
BANKING = "open-banking/v1.0"
ACQUIRING = "acquiring/v1.0"
TIMEOUT = 30
# Статусы готовности выписки у банка.
READY = ("Ready", "Complete", "Completed")


class TochkaError(Exception):
    """Банк ответил ошибкой или чем-то неожиданным."""


def _money(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _moment(value: Any) -> datetime | None:
    """Метка времени банка - ISO-8601, иногда с Z вместо смещения."""
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromisoformat(text[:10])
        except ValueError:
            return None


def parse_transaction(raw: dict, *, account: str | None = None) -> dict | None:
    """Операция выписки - в плоский словарь CRM.

    Плательщик лежит в разных местах в зависимости от того, кто кому
    платил: у поступления это `payer`, у списания - `receiver`. Нас
    интересуют поступления, но разбираем и то и другое: списания нужны,
    чтобы оператор видел движение по счёту целиком.
    """
    txn_id = str(raw.get("transactionId") or raw.get("documentId") or "").strip()
    amount = _money(raw.get("transactionAmount") or raw.get("amount"))
    booked = _moment(raw.get("documentDate") or raw.get("bookingTimestamp")
                     or raw.get("date"))
    if not txn_id or amount is None or booked is None:
        return None
    side = str(raw.get("creditDebitIndicator") or "").lower()
    direction = "credit" if side.startswith("credit") else "debit"
    counterparty = (raw.get("sidePayer") if direction == "credit"
                    else raw.get("sideRecipient")) or {}
    return {"txn_id": txn_id, "account": account or raw.get("accountId"),
            "booked_at": booked, "amount": abs(amount), "direction": direction,
            "payer_name": (counterparty.get("name")
                           or raw.get("payerName") or "").strip() or None,
            "payer_inn": (counterparty.get("inn")
                          or raw.get("payerInn") or "").strip() or None,
            "purpose": (raw.get("paymentPurpose")
                        or raw.get("description") or "").strip() or None}


def receipt_items(title: str, amount: Decimal, *, vat: str = "none") -> list[dict]:
    """Позиция чека: аренда одной строкой.

    Разбивать аренду по дням в чеке незачем: услуга одна, и ФНС ждёт
    именно её. НДС по умолчанию «без НДС» - прокат на УСН.
    """
    return [{"name": title[:128], "amount": float(amount), "quantity": 1,
             "vatType": vat, "paymentMethod": "full_payment",
             "paymentObject": "service", "measure": "pc"}]


@dataclass
class TochkaClient:
    """Клиент Точки. `session_factory` подменяется в тестах."""

    token: str
    customer_code: str = ""
    account_id: str = ""
    api_url: str = API_URL
    session_factory: Any = None

    @property
    def ready(self) -> bool:
        return bool(self.token and self.account_id)

    def _session(self):
        if self.session_factory is not None:
            return self.session_factory()
        import aiohttp
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT))

    async def _json(self, session, method: str, path: str, **kwargs) -> Any:
        response = await session.request(
            method, f"{self.api_url}/{path}",
            headers={"Authorization": f"Bearer {self.token}"}, **kwargs)
        data = await response.json(content_type=None)
        status = getattr(response, "status", 200)
        if status >= 400:
            raise TochkaError(f"{path}: банк ответил {status} ({_error(data)})")
        if not isinstance(data, dict):
            raise TochkaError(f"{path}: ответ не разобрать")
        return data

    async def request_statement(self, session, *, since: date, until: date) -> str:
        """Заказать выписку за период. Возвращает её номер."""
        data = await self._json(
            session, "POST", f"{BANKING}/statements",
            json={"Data": {"Statement": {"accountId": self.account_id,
                                         "startDateTime": since.isoformat(),
                                         "endDateTime": until.isoformat()}}})
        statement = (data.get("Data") or {}).get("Statement") or {}
        number = statement.get("statementId")
        if not number:
            raise TochkaError("выписка заказана, но банк не вернул её номер")
        return str(number)

    async def read_statement(self, session, statement_id: str) -> list[dict]:
        """Прочитать готовую выписку. Пусто - ещё собирается."""
        data = await self._json(
            session, "GET",
            f"{BANKING}/statements/{self.account_id}/{statement_id}")
        statement = (data.get("Data") or {}).get("Statement") or {}
        if isinstance(statement, list):
            statement = statement[0] if statement else {}
        if str(statement.get("status") or "") not in READY:
            return []
        rows = []
        for raw in statement.get("Transaction") or []:
            parsed = parse_transaction(raw, account=self.account_id)
            if parsed is not None:
                rows.append(parsed)
        return rows

    async def statement(self, *, since: date, until: date,
                        statement_id: str | None = None) -> dict:
        """Заказать и сразу попытаться прочитать.

        Банк собирает выписку не мгновенно. Если она не готова, номер
        возвращается наружу: следующий заход прочитает её по номеру, не
        заказывая заново.
        """
        if not self.ready:
            return {"rows": [], "statement_id": None, "ready": False}
        session = self._session()
        try:
            number = statement_id or await self.request_statement(
                session, since=since, until=until)
            rows = await self.read_statement(session, number)
            return {"rows": rows, "statement_id": number, "ready": bool(rows)}
        finally:
            close = getattr(session, "close", None)
            if close is not None:
                await close()

    async def payment_link(self, *, amount: Decimal, purpose: str,
                           client_email: str | None = None,
                           client_phone: str | None = None) -> dict:
        """Ссылка на оплату с чеком 54-ФЗ.

        Чек пробивает банк: это его эквайринг принимает деньги. Наличные
        так не фискализируются - для них нужна касса на точке, и делать
        вид, что банк пробьёт чек за наличные, нечестно.
        """
        if not self.token or not self.customer_code:
            raise TochkaError("эквайринг Точки не настроен")
        if not (client_email or client_phone):
            raise TochkaError("для чека нужен телефон или почта клиента")
        session = self._session()
        try:
            data = await self._json(
                session, "POST", f"{ACQUIRING}/payments_with_receipt",
                json={"Data": {
                    "customerCode": self.customer_code,
                    "amount": str(amount), "purpose": purpose[:210],
                    "paymentMode": ["sbp", "card"],
                    "Client": {"email": client_email, "phone": client_phone},
                    "Items": receipt_items(purpose, amount)}})
            payment = (data.get("Data") or {})
            link = payment.get("paymentLink")
            if not link:
                raise TochkaError("банк не вернул ссылку на оплату")
            return {"link": link, "operation_id": payment.get("operationId")}
        finally:
            close = getattr(session, "close", None)
            if close is not None:
                await close()


def _error(data: Any) -> str:
    """Человеческая часть ошибки банка, если она там есть."""
    if isinstance(data, dict):
        errors = data.get("Errors") or data.get("errors") or []
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return str(first.get("message") or first.get("errorCode") or first)
        return str(data.get("message") or data.get("error") or "")[:200]
    return str(data)[:200]
