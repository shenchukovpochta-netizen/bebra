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
# Статусы операции эквайринга. Банк пишет их по-разному в разных версиях
# ответа, поэтому сравниваем в верхнем регистре и без дефисов.
PAID = ("APPROVED", "CONFIRMED", "SUCCESS", "PAID")
DEAD = ("EXPIRED", "DECLINED", "REJECTED", "CANCELLED", "FAILED", "ERROR")


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

    async def read_statement(self, session,
                             statement_id: str) -> tuple[list[dict], bool]:
        """Прочитать выписку: строки и готовность.

        Готовность берётся из статуса банка, а не из числа строк: за
        выходные по счёту может не быть ни одной операции, и собранная
        пустая выписка читалась как «ещё собирается» - круг заказывал её
        заново и не читал никогда.
        """
        data = await self._json(
            session, "GET",
            f"{BANKING}/statements/{self.account_id}/{statement_id}")
        statement = (data.get("Data") or {}).get("Statement") or {}
        if isinstance(statement, list):
            statement = statement[0] if statement else {}
        if str(statement.get("status") or "") not in READY:
            return [], False
        rows = []
        for raw in statement.get("Transaction") or []:
            parsed = parse_transaction(raw, account=self.account_id)
            if parsed is not None:
                rows.append(parsed)
        return rows, True

    async def statement(self, *, since: date, until: date,
                        statement_id: str | None = None) -> dict:
        """Заказать и сразу попытаться прочитать.

        Банк собирает выписку не мгновенно. Если она не готова, номер
        возвращается наружу: следующий заход прочитает её по номеру, не
        заказывая заново. Заказывать каждый круг новую и читать её тут же
        значит не прочитать выписку никогда.
        """
        if not self.ready:
            return {"rows": [], "statement_id": None, "ready": False}
        session = self._session()
        try:
            number = statement_id or await self.request_statement(
                session, since=since, until=until)
            rows, ready = await self.read_statement(session, number)
            return {"rows": rows, "statement_id": number, "ready": ready}
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

    async def ping(self) -> dict:
        """Проверить, что токен и код клиента приняты банком: список
        торговых точек эквайринга - самый лёгкий запрос, который требует
        и того, и другого. Ошибка - TochkaError с ответом банка."""
        async with self._session() as session:
            data = await self._json(session, "GET", "acquiring/v1.0/retailers",
                                    params={"customerCode": self.customer_code})
        retailers = (data.get("Data") or {}).get("Retailer") or []
        return {"retailers": len(retailers)}

    async def payment_status(self, operation_id: str) -> dict:
        """Что стало со ссылкой: оплатили, протухла или ещё ждём."""
        if not self.token:
            raise TochkaError("эквайринг Точки не настроен")
        session = self._session()
        try:
            data = await self._json(
                session, "GET", f"{ACQUIRING}/payments/{operation_id}")
            return payment_state(data)
        finally:
            close = getattr(session, "close", None)
            if close is not None:
                await close()

    async def charge_saved_card(self, *, token: str, amount: Decimal,
                                purpose: str, client_email: str | None = None,
                                client_phone: str | None = None) -> dict:
        """Списать с ранее сохранённой карты.

        Рекуррентные платежи банк включает магазину отдельно. Пока он их
        не включил, этот вызов вернёт ошибку банка - и она уйдёт на счёт
        как есть: молчаливое «ничего не произошло» оператор не увидит, а
        текст отказа он покажет в банк и включит.
        """
        if not self.token or not self.customer_code:
            raise TochkaError("эквайринг Точки не настроен")
        if not token:
            raise TochkaError("карта клиента не сохранена")
        session = self._session()
        try:
            data = await self._json(
                session, "POST", f"{ACQUIRING}/payments_with_receipt",
                json={"Data": {
                    "customerCode": self.customer_code,
                    "amount": str(amount), "purpose": purpose[:210],
                    "paymentMode": ["card"],
                    "Recurrent": {"token": token},
                    "Client": {"email": client_email, "phone": client_phone},
                    "Items": receipt_items(purpose, amount)}})
            state = payment_state(data)
            payment = data.get("Data") or {}
            state["operation_id"] = (state.get("operation_id")
                                     or payment.get("operationId"))
            return state
        finally:
            close = getattr(session, "close", None)
            if close is not None:
                await close()


def payment_state(raw: Any) -> dict:
    """Ответ банка об операции - к трём словам: paid, dead, pending.

    Разбор отделён от сети, как и у выписки: проверять его без банка
    иначе нечем, а именно здесь легче всего ошибиться.
    """
    data = raw if isinstance(raw, dict) else {}
    body = data.get("Data") if isinstance(data.get("Data"), dict) else data
    operation = body.get("Operation") if isinstance(body, dict) else None
    if isinstance(operation, list):
        operation = operation[0] if operation else {}
    if not isinstance(operation, dict):
        operation = body if isinstance(body, dict) else {}
    status = str(operation.get("status") or operation.get("state") or "")
    flat = status.upper().replace("-", "").replace("_", "")
    state = "paid" if flat in PAID else "dead" if flat in DEAD else "pending"
    return {"state": state, "status": status,
            "operation_id": operation.get("operationId") or operation.get("id"),
            "amount": _money(operation.get("amount")),
            "paid_at": _moment(operation.get("paymentDate")
                               or operation.get("createdAt")),
            "card": _card(operation)}


def _card(operation: dict) -> dict:
    """Что банк рассказал о карте: токен для автосписания и хвост номера.

    Токен приходит не всегда - он появляется только когда эквайринг
    настроен на сохранение карты. Нет токена - автосписания не будет, и
    это честнее, чем придумывать его самим.
    """
    card = operation.get("Card") if isinstance(operation.get("Card"), dict) else {}
    token = (card.get("token") or card.get("cardToken")
             or operation.get("cardToken") or operation.get("rebillId"))
    pan = str(card.get("pan") or card.get("maskedPan") or "")
    return {"token": str(token) if token else "",
            "mask": pan[-4:] if len(pan) >= 4 else "",
            "expires": str(card.get("expDate") or card.get("expiry") or "")}


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
