"""Поведение схемы crm в памяти - для тестов кабинета и веб-панели.

Повторяет контракт app.crm.db.CrmDB: те же методы, те же поля в строках,
те же уникальные ограничения (одна активная аренда на клиента и на
велосипед, одно начисление на период). Живого Postgres в тестах нет.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal


class FakeCrm:
    def __init__(self) -> None:
        self.staff: dict[int, dict] = {}
        self.tariffs_: dict[int, dict] = {}
        self.bikes_: dict[int, dict] = {}
        self.clients_: dict[int, dict] = {}
        self.rentals_: dict[int, dict] = {}
        self.ledger_: list[dict] = []
        self.claims_: dict[int, dict] = {}
        self.bike_log_: list[dict] = []
        self._seq = 0

    def _id(self) -> int:
        self._seq += 1
        return self._seq

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    # ─── сотрудники ───
    async def staff_count(self):
        return len(self.staff)

    async def staff_by_login(self, login):
        return next((dict(s) for s in self.staff.values() if s["login"] == login), None)

    async def staff_by_id(self, staff_id):
        s = self.staff.get(staff_id)
        return dict(s) if s else None

    async def staff_all(self):
        return [dict(s) for s in self.staff.values()]

    async def create_staff(self, login, password_hash, name, role):
        if any(s["login"] == login for s in self.staff.values()):
            raise UniqueError("login")
        sid = self._id()
        self.staff[sid] = {"id": sid, "login": login, "password_hash": password_hash,
                           "name": name, "role": role, "active": True,
                           "created_at": self._now()}
        return sid

    async def set_staff_password(self, staff_id, password_hash):
        self.staff[staff_id]["password_hash"] = password_hash

    async def set_staff_active(self, staff_id, active):
        self.staff[staff_id]["active"] = active

    # ─── тарифы ───
    async def tariffs(self, *, active_only=False):
        rows = [dict(t) for t in self.tariffs_.values() if not active_only or t["active"]]
        return sorted(rows, key=lambda t: (t["sort"], t["period_days"], t["id"]))

    async def tariff(self, tariff_id):
        t = self.tariffs_.get(tariff_id)
        return dict(t) if t else None

    async def create_tariff(self, name, period_days, price, note):
        tid = self._id()
        self.tariffs_[tid] = {"id": tid, "name": name, "period_days": period_days,
                              "price": Decimal(price), "note": note, "active": True,
                              "sort": 100, "created_at": self._now()}
        return tid

    async def update_tariff(self, tariff_id, **fields):
        self.tariffs_[tariff_id].update(fields)

    # ─── парк ───
    def _bike_row(self, b):
        row = dict(b)
        rental = next((r for r in self.rentals_.values()
                       if r["bike_id"] == b["id"] and r["status"] == "active"), None)
        row["rental_id"] = rental["id"] if rental else None
        row["client_id"] = rental["client_id"] if rental else None
        row["full_name"] = (self.clients_[rental["client_id"]]["full_name"]
                            if rental else None)
        return row

    async def bikes(self, *, status=None, q=None, limit=500):
        rows = [self._bike_row(b) for b in self.bikes_.values()
                if (not status or b["status"] == status)
                and (not q or q.lower()
                     in f"{b['code']} {b['model']} {b.get('frame_no') or ''}".lower())]
        return sorted(rows, key=lambda b: b["code"])[:limit]

    async def bike(self, bike_id):
        b = self.bikes_.get(bike_id)
        return self._bike_row(b) if b else None

    async def bike_by_frame(self, frame_no):
        return next((dict(b) for b in self.bikes_.values() if b.get("frame_no") == frame_no),
                    None)

    async def bike_by_motor(self, motor_no):
        return next((dict(b) for b in self.bikes_.values() if b.get("motor_no") == motor_no),
                    None)

    async def bike_by_code(self, code):
        return next((dict(b) for b in self.bikes_.values() if b["code"] == code), None)

    async def create_bike(self, **fields):
        if any(b["code"] == fields.get("code") for b in self.bikes_.values()):
            raise UniqueError("code")
        bid = self._id()
        self.bikes_[bid] = {"id": bid, "code": None, "model": None, "frame_no": None,
                            "motor_no": None, "battery_count": 2, "status": "available",
                            "purchase_price": None, "purchased_on": None, "note": None,
                            "created_at": self._now(), "updated_at": self._now(),
                            **fields}
        return bid

    async def update_bike(self, bike_id, **fields):
        self.bikes_[bike_id].update(fields)

    async def bike_counts(self):
        out: dict[str, int] = {}
        for b in self.bikes_.values():
            out[b["status"]] = out.get(b["status"], 0) + 1
        return out

    async def bike_log(self, bike_id, limit=50):
        return [dict(x) for x in reversed(self.bike_log_) if x["bike_id"] == bike_id][:limit]

    async def add_bike_log(self, bike_id, kind, note, cost, created_by):
        lid = self._id()
        self.bike_log_.append({"id": lid, "bike_id": bike_id, "kind": kind, "note": note,
                               "cost": cost, "created_by": created_by,
                               "created_at": self._now()})
        return lid

    async def bike_rentals(self, bike_id, limit=30):
        rows = [dict(r, full_name=self.clients_[r["client_id"]]["full_name"])
                for r in self.rentals_.values() if r["bike_id"] == bike_id]
        return sorted(rows, key=lambda r: -r["id"])[:limit]

    # ─── клиенты ───
    def _balance(self, client_id) -> Decimal:
        return sum((x["amount"] for x in self.ledger_ if x["client_id"] == client_id),
                   Decimal(0))

    def _active(self, client_id):
        return next((r for r in self.rentals_.values()
                     if r["client_id"] == client_id and r["status"] == "active"), None)

    async def clients(self, *, q=None, status=None, limit=500):
        rows = []
        for c in self.clients_.values():
            if status and c["status"] != status:
                continue
            hay = (f"{c['full_name']} {c['phone']} {c.get('contract_no') or ''} "
                   f"{c.get('username') or ''}")
            digits = "".join(ch for ch in (q or "") if ch.isdigit())
            if len(digits) == 11 and digits[0] in "78":
                digits = digits[1:]
            phone_digits = "".join(ch for ch in c["phone"] if ch.isdigit())
            if q and q.lower() not in hay.lower() \
                    and not (len(digits) >= 3 and digits in phone_digits):
                continue
            r = self._active(c["id"])
            b = self.bikes_.get(r["bike_id"]) if r and r["bike_id"] else None
            rows.append({**c, "balance": self._balance(c["id"]),
                         "rental_id": r["id"] if r else None,
                         "billed_until": r["billed_until"] if r else None,
                         "price": r["price"] if r else None,
                         "period_days": r["period_days"] if r else None,
                         "tariff_name": r["tariff_name"] if r else None,
                         "bike_code": b["code"] if b else None,
                         "bike_model": b["model"] if b else None})
        return sorted(rows, key=lambda c: c["full_name"])[:limit]

    async def client(self, client_id):
        c = self.clients_.get(client_id)
        return dict(c) if c else None

    async def client_by_tg(self, tg_id):
        return next((dict(c) for c in self.clients_.values() if c["tg_id"] == tg_id), None)

    async def client_by_phone(self, phone):
        return next((dict(c) for c in self.clients_.values() if c["phone"] == phone), None)

    async def create_client(self, *, full_name, phone, tg_id=None, username=None,
                            note=None, source="manual", contract_no=None):
        if any(c["phone"] == phone for c in self.clients_.values()):
            raise UniqueError("phone")
        if tg_id is not None and any(c["tg_id"] == tg_id for c in self.clients_.values()):
            raise UniqueError("tg_id")
        cid = self._id()
        self.clients_[cid] = {"id": cid, "full_name": full_name, "phone": phone,
                              "tg_id": tg_id, "username": username, "status": "active",
                              "contract_no": contract_no, "note": note, "source": source,
                              "created_at": self._now(), "updated_at": self._now()}
        return cid

    async def update_client(self, client_id, **fields):
        self.clients_[client_id].update(fields)

    async def link_client_tg(self, client_id, tg_id, username):
        if any(c["tg_id"] == tg_id and c["id"] != client_id for c in self.clients_.values()):
            return False
        self.clients_[client_id].update(tg_id=tg_id, username=username)
        return True

    async def client_balance(self, client_id):
        return self._balance(client_id)

    async def client_rentals(self, client_id, limit=30):
        rows = []
        for r in self.rentals_.values():
            if r["client_id"] != client_id:
                continue
            b = self.bikes_.get(r["bike_id"]) if r["bike_id"] else None
            rows.append({**r, "bike_code": b["code"] if b else None,
                         "bike_model": b["model"] if b else None})
        return sorted(rows, key=lambda r: -r["id"])[:limit]

    # ─── аренды ───
    def _rental_row(self, r):
        c = self.clients_[r["client_id"]]
        b = self.bikes_.get(r["bike_id"]) if r["bike_id"] else None
        return {**r, "full_name": c["full_name"], "phone": c["phone"], "tg_id": c["tg_id"],
                "client_status": c["status"],
                "bike_code": b["code"] if b else None,
                "bike_model": b["model"] if b else None,
                "balance": self._balance(r["client_id"])}

    async def rentals(self, *, status=None, limit=500):
        rows = [self._rental_row(r) for r in self.rentals_.values()
                if not status or r["status"] == status]
        return sorted(rows, key=lambda r: (r["status"] != "active", -r["id"]))[:limit]

    async def rental(self, rental_id):
        r = self.rentals_.get(rental_id)
        return self._rental_row(r) if r else None

    async def active_rental_of(self, client_id):
        r = self._active(client_id)
        return self._rental_row(r) if r else None

    async def active_rentals(self):
        return [self._rental_row(r) for r in self.rentals_.values() if r["status"] == "active"]

    async def create_rental(self, *, client_id, bike_id, tariff_id, tariff_name,
                            period_days, price, billing, started_on, contract_no,
                            created_by):
        if self._active(client_id) is not None:
            raise UniqueError("rentals_active_client_idx")
        if bike_id is not None and any(r["bike_id"] == bike_id and r["status"] == "active"
                                       for r in self.rentals_.values()):
            raise UniqueError("rentals_active_bike_idx")
        rid = self._id()
        self.rentals_[rid] = {"id": rid, "client_id": client_id, "bike_id": bike_id,
                              "tariff_id": tariff_id, "tariff_name": tariff_name,
                              "period_days": period_days, "price": Decimal(price),
                              "billing": billing, "contract_no": contract_no,
                              "started_on": started_on, "billed_until": started_on,
                              "status": "active", "closed_on": None, "close_note": None,
                              "notified_on": None, "notified_kind": None,
                              "created_by": created_by, "created_at": self._now(),
                              "updated_at": self._now()}
        if bike_id is not None:
            self.bikes_[bike_id]["status"] = "rented"
        return rid

    async def update_rental(self, rental_id, **fields):
        self.rentals_[rental_id].update(fields)

    async def close_rental(self, rental_id, *, closed_on, note, bike_status="available"):
        r = self.rentals_.get(rental_id)
        if r is None or r["status"] != "active":
            return False
        r.update(status="closed", closed_on=closed_on, close_note=note)
        if r["bike_id"] is not None and self.bikes_[r["bike_id"]]["status"] == "rented":
            self.bikes_[r["bike_id"]]["status"] = bike_status
        return True

    async def charge_period(self, rental_id, client_id, *, period_from, period_to,
                            amount, note, created_by="billing", created_at=None):
        if any(x["kind"] == "charge" and x["rental_id"] == rental_id
               and x["period_from"] == period_from for x in self.ledger_):
            return False
        await self.add_ledger(client_id=client_id, rental_id=rental_id, kind="charge",
                              amount=Decimal(amount), note=note, created_by=created_by,
                              period_from=period_from, period_to=period_to,
                              created_at=created_at)
        r = self.rentals_[rental_id]
        r["billed_until"] = max(r["billed_until"], period_to)
        return True

    async def mark_notified(self, rental_id, today, kind):
        self.rentals_[rental_id].update(notified_on=today, notified_kind=kind)

    # ─── журнал ───
    async def add_ledger(self, *, client_id, kind, amount, rental_id=None, method=None,
                         note=None, created_by=None, period_from=None, period_to=None,
                         created_at=None):
        lid = self._id()
        self.ledger_.append({"id": lid, "client_id": client_id, "rental_id": rental_id,
                             "kind": kind, "amount": Decimal(amount), "method": method,
                             "period_from": period_from, "period_to": period_to,
                             "note": note, "created_by": created_by,
                             "created_at": created_at or self._now()})
        return lid

    async def ledger_of(self, client_id, limit=100):
        return [dict(x) for x in reversed(self.ledger_) if x["client_id"] == client_id][:limit]

    async def ledger(self, *, since=None, until=None, kind=None, limit=1000):
        rows = []
        for x in reversed(self.ledger_):
            d = x["created_at"].date()
            if since and d < since:
                continue
            if until and d > until:
                continue
            if kind and x["kind"] != kind:
                continue
            rows.append({**x, "full_name": self.clients_[x["client_id"]]["full_name"]})
        return rows[:limit]

    async def ledger_totals(self, *, since=None, until=None):
        out: dict[str, Decimal] = {}
        for x in await self.ledger(since=since, until=until):
            out[x["kind"]] = out.get(x["kind"], Decimal(0)) + x["amount"]
        return out

    async def revenue_by_month(self, months=12):
        out: dict[date, dict] = {}
        for x in self.ledger_:
            m = x["created_at"].date().replace(day=1)
            row = out.setdefault(m, {"month": m, "paid": Decimal(0), "charged": Decimal(0),
                                     "refunded": Decimal(0), "repairs": Decimal(0)})
            if x["kind"] == "payment":
                row["paid"] += x["amount"]
            elif x["kind"] in ("charge", "fine"):
                row["charged"] -= x["amount"]
            elif x["kind"] == "refund":
                row["refunded"] -= x["amount"]
        for x in self.bike_log_:
            if x.get("cost"):
                m = x["created_at"].date().replace(day=1)
                row = out.setdefault(m, {"month": m, "paid": Decimal(0),
                                         "charged": Decimal(0), "refunded": Decimal(0),
                                         "repairs": Decimal(0)})
                row["repairs"] += x["cost"]
        return sorted(out.values(), key=lambda r: r["month"], reverse=True)

    async def debtors(self, limit=50):
        rows = [{"id": c["id"], "full_name": c["full_name"], "phone": c["phone"],
                 "status": c["status"], "balance": self._balance(c["id"])}
                for c in self.clients_.values() if self._balance(c["id"]) < 0]
        return sorted(rows, key=lambda r: r["balance"])[:limit]

    async def counts(self):
        return {"clients": len(self.clients_),
                "rentals": sum(1 for r in self.rentals_.values() if r["status"] == "active"),
                "claims": sum(1 for p in self.claims_.values() if p["status"] == "pending"),
                "bikes": len(self.bikes_)}

    # ─── заявки ───
    def _claim_row(self, p):
        c = self.clients_[p["client_id"]]
        return {**p, "full_name": c["full_name"], "phone": c["phone"], "tg_id": c["tg_id"]}

    async def create_claim(self, client_id, amount_hint):
        pid = self._id()
        self.claims_[pid] = {"id": pid, "client_id": client_id, "amount_hint": amount_hint,
                             "receipt_file_id": None, "receipt_is_photo": True,
                             "status": "pending", "card_chat_id": None,
                             "card_message_id": None, "ledger_id": None,
                             "resolved_by": None, "resolved_at": None,
                             "created_at": self._now()}
        return pid

    async def claim(self, claim_id):
        p = self.claims_.get(claim_id)
        return self._claim_row(p) if p else None

    async def pending_claims(self):
        return [self._claim_row(p) for p in self.claims_.values() if p["status"] == "pending"]

    async def pending_claim_of(self, client_id):
        rows = [p for p in self.claims_.values()
                if p["client_id"] == client_id and p["status"] == "pending"]
        return self._claim_row(rows[-1]) if rows else None

    async def claim_by_card(self, chat_id, message_id):
        return next((self._claim_row(p) for p in self.claims_.values()
                     if p["card_chat_id"] == chat_id and p["card_message_id"] == message_id),
                    None)

    async def set_claim_card(self, claim_id, chat_id, message_id):
        self.claims_[claim_id].update(card_chat_id=chat_id, card_message_id=message_id)

    async def set_claim_receipt(self, claim_id, file_id, is_photo):
        self.claims_[claim_id].update(receipt_file_id=file_id, receipt_is_photo=is_photo)

    async def resolve_claim(self, claim_id, *, status, resolved_by, ledger_id=None):
        p = self.claims_.get(claim_id)
        if p is None or p["status"] != "pending":
            return False
        p.update(status=status, resolved_by=resolved_by, ledger_id=ledger_id,
                 resolved_at=self._now())
        return True


class UniqueError(Exception):
    """Аналог asyncpg.UniqueViolationError: имя класса содержит «unique»."""


UniqueViolationError = UniqueError
