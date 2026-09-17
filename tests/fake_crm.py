"""Поведение схемы crm в памяти - для тестов кабинета и веб-панели.

Повторяет контракт app.crm.db.CrmDB: те же методы, те же поля в строках,
те же уникальные ограничения (одна активная аренда на клиента и на
велосипед, одно начисление на период). Живого Postgres в тестах нет.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.crm import logic as crm_logic


class FakeCrm:
    def __init__(self) -> None:
        self.staff: dict[int, dict] = {}
        self.profiles_: dict[int, dict] = {}
        self.tariffs_: dict[int, dict] = {}
        self.bikes_: dict[int, dict] = {}
        self.clients_: dict[int, dict] = {}
        self.rentals_: dict[int, dict] = {}
        self.ledger_: list[dict] = []
        self.claims_: dict[int, dict] = {}
        self.bike_log_: list[dict] = []
        self.status_log_: list[dict] = []
        self.repair_items_: list[dict] = []
        self.work_types_: dict[int, dict] = {}
        self.orders_: dict[int, dict] = {}
        self.order_items_: list[dict] = []
        self.takes_: dict[int, dict] = {}
        self.take_items_: list[dict] = []
        self.referrals_: dict[int, dict] = {}
        self.suppliers_: dict[int, dict] = {}
        self.parts_: dict[int, dict] = {}
        self.part_moves_: list[dict] = []
        self.part_docs_: dict[int, dict] = {}
        self.part_orders_: dict[int, dict] = {}
        self.part_order_items_: list[dict] = []
        self.rental_bikes_: list[dict] = []
        self.purchases_: dict[int, dict] = {}
        self.locations_: dict[int, dict] = {}
        self.bike_models_: dict[int, dict] = {}
        self.battery_models_: dict[int, dict] = {}
        self.compat_: dict[tuple, bool] = {}
        self.batteries_: dict[int, dict] = {}
        self.battery_log_: list[dict] = []
        self.signs_: dict[int, dict] = {}
        self.sign_events_: list[dict] = []
        self.templates_: dict[int, dict] = {}
        self.campaigns_: dict[int, dict] = {}
        self.sends_: dict[int, dict] = {}
        self.shifts_: dict[int, dict] = {}
        self.cash_moves_: list[dict] = []
        self.bank_: dict[int, dict] = {}
        self.pay_orders_: dict[int, dict] = {}
        self.notices_: dict[str, dict] = {}
        self.notice_log_: list[dict] = []
        self.cards_: dict[int, dict] = {}
        self.trackers_: dict[int, dict] = {}
        self.positions_: list[dict] = []
        self.alerts_: dict[int, dict] = {}
        self.settings_: dict[str, str] = {}
        self._seq = 0
        # Профили нумеруются отдельно: иначе встроенные съедали бы первые
        # id, и клиент из seed() перестал бы быть первым.
        self._profile_seq = 0
        # Виды работ нумеруются отдельно по той же причине, что и профили:
        # иначе каталог съедал бы первые id, и клиент из seed() перестал бы
        # быть первым.
        self._work_type_seq = 0
        self._seed_profiles()
        self._seed_work_types()

    def _id(self) -> int:
        self._seq += 1
        return self._seq

    def _profile_id(self) -> int:
        self._profile_seq += 1
        return self._profile_seq

    def _seed_work_types(self) -> None:
        """Пара видов работ, как их кладёт schema.sql: тестам нужен
        непустой каталог, полный список там ни к чему."""
        for title, category, minutes, price, node in (
                ("Замена камеры", "Ходовая", 15, Decimal(200), "tube_tire"),
                ("Диагностика электрики", "Электрика", 45, Decimal(600), "wiring")):
            self._work_type_seq += 1
            tid = self._work_type_seq
            self.work_types_[tid] = {
                "id": tid, "title": title, "category": category, "minutes": minutes,
                "price": price, "node": node, "active": True, "sort": 100,
                "created_at": self._now()}

    def _seed_profiles(self) -> None:
        """Те же встроенные профили, что кладёт schema.sql."""
        for code, name, perms, built_in in crm_logic.BUILT_IN_PROFILES:
            pid = self._profile_id()
            self.profiles_[pid] = {"id": pid, "code": code, "name": name,
                                   "perms": dict(perms), "built_in": built_in,
                                   "created_at": self._now(),
                                   "updated_at": self._now()}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    # ─── сотрудники ───
    async def staff_count(self):
        return len(self.staff)

    def _staff_row(self, s):
        """Сотрудник всегда с правами своего профиля - как join в CrmDB."""
        p = self.profiles_.get(s.get("profile_id")) or {}
        return {**s, "profile_name": p.get("name"), "profile_code": p.get("code"),
                "profile_built_in": bool(p.get("built_in")),
                "perms": dict(p.get("perms") or {})}

    async def staff_by_login(self, login):
        return next((self._staff_row(s) for s in self.staff.values()
                     if s["login"] == login), None)

    async def staff_by_id(self, staff_id):
        s = self.staff.get(staff_id)
        return self._staff_row(s) if s else None

    async def staff_all(self):
        rows = sorted(self.staff.values(), key=lambda s: (not s["active"], s["id"]))
        return [self._staff_row(s) for s in rows]

    async def create_staff(self, login, password_hash, name, role, profile_id=None):
        if any(s["login"] == login for s in self.staff.values()):
            raise UniqueError("login")
        sid = self._id()
        self.staff[sid] = {"id": sid, "login": login, "password_hash": password_hash,
                           "name": name, "role": role, "active": True,
                           "profile_id": profile_id, "tg_id": None,
                           "tg_username": None, "link_code": None, "linked_at": None,
                           "created_at": self._now()}
        return sid

    async def staff_by_tg(self, tg_id):
        return next((self._staff_row(s) for s in self.staff.values()
                     if s.get("tg_id") == tg_id), None)

    async def staff_by_link_code(self, code):
        return next((self._staff_row(s) for s in self.staff.values()
                     if s.get("link_code") == code), None)

    async def set_staff_link_code(self, staff_id, code):
        if code and any(s.get("link_code") == code for s in self.staff.values()):
            return False
        self.staff[staff_id]["link_code"] = code
        return True

    async def link_staff_tg(self, staff_id, tg_id, username):
        if any(s.get("tg_id") == tg_id and s["id"] != staff_id
               for s in self.staff.values()):
            return False
        self.staff[staff_id].update(tg_id=tg_id, tg_username=username,
                                    link_code=None, linked_at=self._now())
        return True

    async def unlink_staff_tg(self, staff_id):
        self.staff[staff_id].update(tg_id=None, tg_username=None, link_code=None,
                                    linked_at=None)

    async def set_staff_profile(self, staff_id, profile_id):
        self.staff[staff_id]["profile_id"] = profile_id

    async def set_staff_password(self, staff_id, password_hash):
        self.staff[staff_id]["password_hash"] = password_hash

    async def set_staff_active(self, staff_id, active):
        self.staff[staff_id]["active"] = active

    # ─── профили доступа ───
    async def access_profiles(self):
        rows = []
        for p in self.profiles_.values():
            staff_count = sum(1 for s in self.staff.values()
                              if s.get("profile_id") == p["id"] and s["active"])
            rows.append({**p, "perms": dict(p["perms"]), "staff_count": staff_count})
        return sorted(rows, key=lambda p: (not p["built_in"], p["name"]))

    async def access_profile(self, profile_id):
        p = self.profiles_.get(profile_id)
        return {**p, "perms": dict(p["perms"])} if p else None

    async def access_profile_by_code(self, code):
        p = next((p for p in self.profiles_.values() if p["code"] == code), None)
        return {**p, "perms": dict(p["perms"])} if p else None

    async def create_access_profile(self, name, perms):
        if any(p["name"] == name for p in self.profiles_.values()):
            raise UniqueError("name")
        pid = self._profile_id()
        self.profiles_[pid] = {"id": pid, "code": None, "name": name,
                               "perms": dict(perms), "built_in": False,
                               "created_at": self._now(), "updated_at": self._now()}
        return pid

    async def update_access_profile(self, profile_id, *, name, perms):
        p = self.profiles_.get(profile_id)
        if not p or p["built_in"]:
            return
        if any(o["name"] == name and o["id"] != profile_id
               for o in self.profiles_.values()):
            raise UniqueError("name")
        p.update(name=name, perms=dict(perms), updated_at=self._now())

    async def delete_access_profile(self, profile_id):
        p = self.profiles_.get(profile_id)
        if not p or p["built_in"]:
            return False
        if any(s.get("profile_id") == profile_id for s in self.staff.values()):
            return False
        del self.profiles_[profile_id]
        return True

    # ─── тарифы ───
    async def tariffs(self, *, active_only=False):
        rows = [dict(t) for t in self.tariffs_.values() if not active_only or t["active"]]
        return sorted(rows, key=lambda t: (t["sort"], t["period_days"], t["id"]))

    async def tariff(self, tariff_id):
        t = self.tariffs_.get(tariff_id)
        return dict(t) if t else None

    async def create_tariff(self, name, period_days, price, note, model=None):
        tid = self._id()
        if any(t["active"] and t["period_days"] == period_days
               and (t.get("model") or "") == (model or "")
               for t in self.tariffs_.values()):
            raise UniqueError("tariff model period")
        self.tariffs_[tid] = {"id": tid, "name": name, "period_days": period_days,
                              "price": Decimal(price), "note": note, "active": True,
                              "sort": 100, "model": model,
                              "created_at": self._now()}
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

    async def bikes(self, *, status=None, q=None, location=None, limit=500):
        rows = [self._bike_row(b) for b in self.bikes_.values()
                if (not status or b["status"] == status)
                and (not location or (b.get("location") is None if location == "none"
                                      else b.get("location") == location))
                and (not q or q.lower()
                     in f"{b['code']} {b['model']} {b.get('frame_no') or ''}".lower())]
        return sorted(rows, key=lambda b: b["code"])[:limit]

    def _log_status(self, bike_id, from_status, to_status, by=None):
        """Аналог триггера crm.log_bike_status."""
        self.status_log_.append({"id": self._id(), "bike_id": bike_id,
                                 "from_status": from_status, "to_status": to_status,
                                 "changed_at": self._now(), "changed_by": by or None})

    async def bike_status_log(self, bike_id, limit=30):
        rows = [dict(x) for x in self.status_log_ if x["bike_id"] == bike_id]
        return sorted(rows, key=lambda x: (x["changed_at"], x["id"]), reverse=True)[:limit]

    async def bike_status_since(self):
        out = {}
        for x in self.status_log_:
            if x["bike_id"] not in out or x["changed_at"] > out[x["bike_id"]]:
                out[x["bike_id"]] = x["changed_at"]
        return out

    async def bike_days_by_status(self, since, until):
        until = min(until, self._now())
        return crm_logic.days_by_status(self.status_log_, since, until)

    async def rental_revenue(self, since, until):
        return sum((x["amount"] for x in self.ledger_
                    if x["kind"] == "payment" and since <= x["created_at"] < until),
                   Decimal(0))

    async def repair_nodes(self):
        return [{"code": c, "title": t} for c, t in crm_logic.REPAIR_NODES.items()]

    async def create_repair(self, bike_id, *, items, note, created_by):
        total = sum((Decimal(str(i.get("parts_cost") or 0))
                     + Decimal(str(i.get("labor_cost") or 0)) for i in items), Decimal(0))
        log_id = await self.add_bike_log(bike_id, "repair", note, total, created_by)
        for i in items:
            self.repair_items_.append({
                "id": self._id(), "log_id": log_id, "bike_id": bike_id, "node": i["node"],
                "parts_cost": Decimal(str(i.get("parts_cost") or 0)),
                "labor_cost": Decimal(str(i.get("labor_cost") or 0)),
                "note": i.get("note"), "created_at": self._now()})
        return log_id

    async def repair_stats(self, since, until):
        by_node: dict[str, dict] = {}
        for i in self.repair_items_:
            if not since <= i["created_at"] < until:
                continue
            row = by_node.setdefault(i["node"], {"code": i["node"],
                                                 "title": crm_logic.REPAIR_NODES[i["node"]],
                                                 "n": 0, "cost": Decimal(0)})
            row["n"] += 1
            row["cost"] += i["parts_cost"] + i["labor_cost"]
        by_model: dict[str, dict] = {}
        for x in self.bike_log_:
            if x["kind"] != "repair" or not since <= x["created_at"] < until:
                continue
            model = self.bikes_[x["bike_id"]]["model"]
            row = by_model.setdefault(model, {"model": model, "n": 0, "bikes": set(),
                                              "cost": Decimal(0)})
            row["n"] += 1
            row["bikes"].add(x["bike_id"])
            row["cost"] += x["cost"] or Decimal(0)
        return {"by_node": sorted(by_node.values(), key=lambda r: -r["cost"]),
                "by_model": [dict(r, bikes=len(r["bikes"]))
                             for r in sorted(by_model.values(), key=lambda r: -r["cost"])]}

    async def bike(self, bike_id):
        b = self.bikes_.get(bike_id)
        return self._bike_row(b) if b else None

    async def bike_by_frame(self, frame_no):
        for b in self.bikes_.values():
            if b["frame_no"] and b["frame_no"].upper() == str(frame_no).upper():
                return dict(b)
        return None

    async def bike_by_motor(self, motor_no):
        for b in self.bikes_.values():
            if b["motor_no"] and b["motor_no"].upper() == str(motor_no).upper():
                return dict(b)
        return None

    async def bike_by_code(self, code):
        return next((dict(b) for b in self.bikes_.values() if b["code"] == code), None)

    async def create_bike(self, *, by=None, **fields):
        if any(b["code"] == fields.get("code") for b in self.bikes_.values()):
            raise UniqueError("code")
        if fields.get("frame_no") and any(b["frame_no"] == fields["frame_no"]
                                          for b in self.bikes_.values()):
            raise UniqueError("frame_no")
        bid = self._id()
        self.bikes_[bid] = {"id": bid, "code": None, "model": None, "frame_no": None,
                            "motor_no": None, "battery_count": 2, "status": "available",
                            "purchase_price": None, "purchased_on": None, "note": None,
                            "spare": False, "purchase_id": None,
                            "location": None, "service_months": 24,
                            "residual_price": Decimal(0), "battery_price": None,
                            "battery_service_months": 15, "mileage_km": 0,
                            "created_at": self._now(), "updated_at": self._now(),
                            **fields}
        self._log_status(bid, None, self.bikes_[bid]["status"], by)
        return bid

    async def update_bike(self, bike_id, *, by=None, **fields):
        before = self.bikes_[bike_id]["status"]
        self.bikes_[bike_id].update(fields)
        if "status" in fields and fields["status"] != before:
            self._log_status(bike_id, before, fields["status"], by)

    async def bike_counts(self):
        out: dict[str, int] = {}
        for b in self.bikes_.values():
            out[b["status"]] = out.get(b["status"], 0) + 1
        return out

    async def bike_log(self, bike_id, limit=50, kind=None):
        return [dict(x) for x in reversed(self.bike_log_)
                if x["bike_id"] == bike_id and (not kind or x["kind"] == kind)][:limit]

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
                              "channel": None, "ref_code": None, "max_id": None,
                              "invited_by": None, "invited_at": None,
                              "created_at": self._now(), "updated_at": self._now()}
        return cid

    async def clients_since(self, since):
        return [{"id": c["id"], "full_name": c["full_name"],
                 "channel": c.get("channel"), "source": c.get("source"),
                 "created_at": c["created_at"]}
                for c in self.clients_.values() if c["created_at"] >= since]

    async def update_client(self, client_id, **fields):
        # Частичные уникальные индексы базы: тот же аккаунт не может
        # принадлежать двум карточкам, и подмена его в панели должна
        # упираться в ту же ошибку, что и в Postgres.
        for column in ("tg_id", "max_id"):
            value = fields.get(column)
            if value is not None and any(
                    c.get(column) == value and c["id"] != client_id
                    for c in self.clients_.values()):
                raise UniqueError(f"clients {column}")
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
                            created_by, mileage_start=None):
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
                              "intent": None, "intent_until": None, "intent_by": None,
                              "intent_at": None, "snooze_until": None,
                              "mileage_start": mileage_start, "mileage_end": None,
                              "created_by": created_by, "created_at": self._now(),
                              "updated_at": self._now()}
        if bike_id is not None:
            self._log_status(bike_id, self.bikes_[bike_id]["status"], "rented", created_by)
            self.bikes_[bike_id]["status"] = "rented"
            if mileage_start is not None:
                self.bikes_[bike_id]["mileage_km"] = max(
                    self.bikes_[bike_id].get("mileage_km") or 0, int(mileage_start))
        return rid

    async def update_rental(self, rental_id, **fields):
        self.rentals_[rental_id].update(fields)

    async def close_rental(self, rental_id, *, closed_on, note, bike_status="available",
                           closed_by=None, mileage_end=None):
        r = self.rentals_.get(rental_id)
        if r is None or r["status"] != "active":
            return False
        r.update(status="closed", closed_on=closed_on, close_note=note)
        if mileage_end is not None:
            r["mileage_end"] = int(mileage_end)
        if r["bike_id"] is not None and self.bikes_[r["bike_id"]]["status"] == "rented":
            self._log_status(r["bike_id"], "rented", bike_status, closed_by)
            self.bikes_[r["bike_id"]]["status"] = bike_status
            if mileage_end is not None:
                self.bikes_[r["bike_id"]]["mileage_km"] = max(
                    self.bikes_[r["bike_id"]].get("mileage_km") or 0, int(mileage_end))
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
        for x in await self.ledger(since=since, until=until, limit=10**9):
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

    async def credit_claim(self, claim_id, *, client_id, amount, method, note, created_by):
        p = self.claims_.get(claim_id)
        if p is None or p["status"] != "pending":
            return None
        lid = await self.add_ledger(client_id=client_id, kind="payment", amount=amount,
                                    method=method, note=note, created_by=created_by)
        p.update(status="confirmed", resolved_by=created_by, ledger_id=lid,
                 resolved_at=self._now())
        return lid

    async def resolve_claim(self, claim_id, *, status, resolved_by, ledger_id=None):
        p = self.claims_.get(claim_id)
        if p is None or p["status"] != "pending":
            return False
        p.update(status=status, resolved_by=resolved_by, ledger_id=ledger_id,
                 resolved_at=self._now())
        return True

    # ─── сервис: виды работ ───
    async def work_types(self, *, active_only=False):
        rows = []
        for t in self.work_types_.values():
            if active_only and not t["active"]:
                continue
            used = sum(1 for i in self.order_items_ if i.get("work_type_id") == t["id"])
            rows.append({**t, "used": used})
        return sorted(rows, key=lambda t: (not t["active"], t["sort"], t["title"]))

    async def work_type(self, type_id):
        t = self.work_types_.get(type_id)
        return dict(t) if t else None

    async def create_work_type(self, *, title, category, minutes, price, node):
        if any(t["title"] == title for t in self.work_types_.values()):
            raise UniqueError("title")
        self._work_type_seq += 1
        tid = self._work_type_seq
        self.work_types_[tid] = {"id": tid, "title": title, "category": category,
                                 "minutes": minutes, "price": price, "node": node,
                                 "active": True, "sort": 100,
                                 "created_at": self._now()}
        return tid

    async def update_work_type(self, type_id, **fields):
        if fields:
            self.work_types_[type_id].update(fields)

    # ─── сервис: наряды ───
    def _order_row(self, o):
        bike = self.bikes_.get(o.get("bike_id")) or {}
        client = self.clients_.get(o.get("client_id")) or {}
        tech = self.staff.get(o.get("tech_id")) or {}
        return {**o, "bike_code": bike.get("code"), "bike_model": bike.get("model"),
                "bike_status": bike.get("status"), "client_name": client.get("full_name"),
                "client_phone": client.get("phone"), "tech_name": tech.get("name"),
                "tech_login": tech.get("login")}

    async def work_orders(self, *, status=None, payer=None, tech_id=None,
                          bike_id=None, open_only=False, limit=300):
        rows = []
        for o in self.orders_.values():
            if status and o["status"] != status:
                continue
            if payer and o["payer"] != payer:
                continue
            if tech_id and o.get("tech_id") != tech_id:
                continue
            if bike_id and o.get("bike_id") != bike_id:
                continue
            if open_only and o["status"] not in crm_logic.ORDER_OPEN:
                continue
            rows.append(self._order_row(o))
        rows.sort(key=lambda o: (o["opened_at"], o["id"]), reverse=True)
        return rows[:limit]

    async def work_order(self, order_id):
        o = self.orders_.get(order_id)
        return self._order_row(o) if o else None

    async def open_order_of(self, bike_id):
        o = next((o for o in self.orders_.values()
                  if o.get("bike_id") == bike_id and o["status"] in crm_logic.ORDER_OPEN),
                 None)
        return self._order_row(o) if o else None

    async def open_orders_by_bike(self):
        out = {}
        for o in self.orders_.values():
            if o.get("bike_id") and o["status"] in crm_logic.ORDER_OPEN:
                out[o["bike_id"]] = self._order_row(o)
        return out

    async def create_work_order(self, *, bike_id, payer, client_id, complaint,
                                object_note, tech_id, estimate, created_by):
        if bike_id and any(o.get("bike_id") == bike_id
                           and o["status"] in crm_logic.ORDER_OPEN
                           for o in self.orders_.values()):
            raise UniqueError("work_orders_one_open")
        oid = self._id()
        number = len(self.orders_) + 1
        self.orders_[oid] = {
            "id": oid, "no": crm_logic.order_no(number), "bike_id": bike_id,
            "object_note": object_note, "payer": payer, "client_id": client_id,
            "status": "new", "tech_id": tech_id, "complaint": complaint,
            "estimate": Decimal(str(estimate or 0)), "total": Decimal(0),
            "cost": Decimal(0), "paid_at": None, "note": None,
            "created_by": created_by, "opened_at": self._now(),
            "closed_at": None, "log_id": None}
        return oid

    async def update_work_order(self, order_id, **fields):
        if fields:
            self.orders_[order_id].update(fields)

    async def order_items(self, order_id):
        return [dict(i) for i in self.order_items_ if i["order_id"] == order_id]

    async def add_order_item(self, order_id, *, title, node, work_type_id, qty,
                             price, parts_cost, labor_cost, note=None):
        iid = self._id()
        self.order_items_.append({
            "id": iid, "order_id": order_id, "work_type_id": work_type_id,
            "title": title, "node": node, "qty": int(qty),
            "price": Decimal(str(price or 0)),
            "parts_cost": Decimal(str(parts_cost or 0)),
            "labor_cost": Decimal(str(labor_cost or 0)),
            "note": note, "created_at": self._now()})
        return iid

    async def delete_order_item(self, order_id, item_id):
        before = len(self.order_items_)
        self.order_items_ = [i for i in self.order_items_
                             if not (i["id"] == item_id and i["order_id"] == order_id)]
        return len(self.order_items_) < before

    async def order_stats(self, since, until):
        closed = [o for o in self.orders_.values()
                  if o["status"] == "done" and o.get("closed_at")
                  and since <= o["closed_at"] < until]
        return {
            "closed": len(closed),
            "client_orders": sum(1 for o in closed if o["payer"] == "client"),
            "revenue": sum((o["total"] for o in closed if o["payer"] == "client"),
                           Decimal(0)),
            "cost": sum((o["cost"] for o in closed), Decimal(0)),
        }

    # ─────────────────────── пересчёт техники ───────────────────────

    async def stock_takes(self, *, limit=100):
        rows = sorted(self.takes_.values(), key=lambda t: t["id"], reverse=True)
        return [dict(t) for t in rows[:limit]]

    async def stock_take(self, take_id):
        t = self.takes_.get(take_id)
        return dict(t) if t else None

    async def open_stock_take(self):
        rows = [t for t in self.takes_.values() if t["status"] == "open"]
        return dict(rows[-1]) if rows else None

    async def create_stock_take(self, *, scope, location, note, bike_ids, created_by):
        if any(t["status"] == "open" for t in self.takes_.values()):
            raise UniqueError("stock_takes_one_open")
        take_id = self._id()
        self.takes_[take_id] = {
            "id": take_id, "no": crm_logic.take_no(len(self.takes_) + 1), "scope": scope,
            "location": location, "status": "open", "expected": len(bike_ids),
            "found": 0, "missing": 0, "extra": 0, "note": note,
            "created_by": created_by, "started_at": self._now(), "closed_at": None}
        for bike_id in bike_ids:
            self.take_items_.append({
                "id": self._id(), "take_id": take_id, "bike_id": bike_id,
                "code": None, "state": "expected", "note": None,
                "created_at": self._now()})
        return take_id

    async def update_stock_take(self, take_id, **fields):
        if take_id in self.takes_:
            self.takes_[take_id].update(fields)

    def _take_item_row(self, item):
        bike = self.bikes_.get(item.get("bike_id"))
        return {**item,
                "bike_code": bike["code"] if bike else None,
                "bike_model": bike["model"] if bike else None,
                "bike_status": bike["status"] if bike else None,
                "bike_location": bike.get("location") if bike else None}

    async def take_items(self, take_id):
        rows = [self._take_item_row(i) for i in self.take_items_
                if i["take_id"] == take_id]
        return sorted(rows, key=lambda i: (i["bike_code"] or i["code"] or "", i["id"]))

    async def take_item(self, take_id, item_id):
        return next((self._take_item_row(i) for i in self.take_items_
                     if i["take_id"] == take_id and i["id"] == item_id), None)

    async def take_item_of_bike(self, take_id, bike_id):
        return next((self._take_item_row(i) for i in self.take_items_
                     if i["take_id"] == take_id and i["bike_id"] == bike_id), None)

    async def set_take_item(self, take_id, item_id, *, state, note=None):
        for item in self.take_items_:
            if item["take_id"] == take_id and item["id"] == item_id:
                item["state"] = state
                item["note"] = note or item["note"]
                return True
        return False

    async def mark_take_all(self, take_id, *, state):
        source = "found" if state == "expected" else "expected"
        hit = 0
        for item in self.take_items_:
            if item["take_id"] == take_id and item["state"] == source:
                item["state"] = state
                hit += 1
        return hit

    async def add_take_item(self, take_id, *, bike_id, code, state="extra", note=None):
        if bike_id is not None and any(
                i["take_id"] == take_id and i["bike_id"] == bike_id
                for i in self.take_items_):
            raise UniqueError("stock_take_items_one")
        item_id = self._id()
        self.take_items_.append({"id": item_id, "take_id": take_id, "bike_id": bike_id,
                                 "code": code, "state": state, "note": note,
                                 "created_at": self._now()})
        return item_id

    async def delete_take_item(self, take_id, item_id):
        before = len(self.take_items_)
        self.take_items_ = [i for i in self.take_items_
                            if not (i["take_id"] == take_id and i["id"] == item_id)]
        return len(self.take_items_) < before

    async def close_stock_take(self, take_id, *, counts, closed_at):
        missing = []
        for item in self.take_items_:
            if item["take_id"] == take_id and item["state"] == "expected":
                item["state"] = "missing"
                if item["bike_id"] is not None:
                    missing.append(int(item["bike_id"]))
        self.takes_[take_id].update({
            "status": "done", "closed_at": closed_at,
            "expected": int(counts.get("total") or 0),
            "found": int(counts.get("found") or 0),
            "missing": int(counts.get("missing") or 0),
            "extra": int(counts.get("extra") or 0)})
        return missing

    # ─────────────────────── окупаемость по моделям ───────────────────────

    async def model_money(self, since, until):
        out: dict[str, dict] = {}

        def cell(model):
            return out.setdefault(model, {"paid": Decimal(0), "charged": Decimal(0),
                                          "repair_cost": Decimal(0),
                                          "works": Decimal(0),
                                          "rented_days": Decimal(0)})

        def model_of(bike_id):
            bike = self.bikes_.get(bike_id)
            return bike["model"] if bike else None

        for entry in self.ledger_:
            if not since <= entry["created_at"] < until:
                continue
            rental = self.rentals_.get(entry.get("rental_id"))
            if rental is None:
                # Платёж без аренды (зачисление по заявке): берём аренду
                # клиента, шедшую в день платежа, а если её не было -
                # ближайшую по времени. Как и SQL.
                day = entry["created_at"].date()
                own = [r for r in self.rentals_.values()
                       if r["client_id"] == entry["client_id"]]
                covering = [r for r in own
                            if r["started_on"] <= day
                            and (r.get("closed_on") is None or r["closed_on"] >= day)]
                if covering:
                    rental = covering[-1]
                elif own:
                    rental = min(own, key=lambda r: (abs((r["started_on"] - day).days),
                                                     r["id"]))
            if rental is None:
                continue
            model = model_of(rental.get("bike_id"))
            if model is None:
                continue
            if entry["kind"] == "payment":
                cell(model)["paid"] += entry["amount"]
            elif entry["kind"] in ("charge", "fine"):
                cell(model)["charged"] -= entry["amount"]
        for row in self.bike_log_:
            if row["kind"] != "repair" or not since <= row["created_at"] < until:
                continue
            model = model_of(row["bike_id"])
            if model:
                cell(model)["repair_cost"] += Decimal(str(row.get("cost") or 0))
        for order in self.orders_.values():
            if (order["payer"] != "client" or order["status"] != "done"
                    or not order.get("closed_at")
                    or not since <= order["closed_at"] < until):
                continue
            model = model_of(order.get("bike_id"))
            if model:
                cell(model)["works"] += order["total"]
        for bike_id, rows in self._log_by_bike().items():
            model = model_of(bike_id)
            if not model:
                continue
            own = crm_logic.days_by_status(rows, since, min(until, self._now()))
            if own.get("rented"):
                cell(model)["rented_days"] += own["rented"]
        return out

    def _log_by_bike(self):
        out: dict[int, list[dict]] = {}
        for row in self.status_log_:
            out.setdefault(row["bike_id"], []).append(row)
        return out

    # ─────────────────────── настройки и приглашения ───────────────────────

    async def settings(self):
        return dict(self.settings_)

    async def set_setting(self, key, value, *, by):
        self.settings_[key] = value

    async def client_by_ref_code(self, code):
        return next((dict(c) for c in self.clients_.values()
                     if c.get("ref_code") == code), None)

    async def set_ref_code(self, client_id, code):
        if any(c.get("ref_code") == code for c in self.clients_.values()):
            return False
        self.clients_[client_id]["ref_code"] = code
        return True

    def _referral_row(self, ref):
        agent = self.clients_.get(ref["agent_id"]) or {}
        friend = self.clients_.get(ref.get("client_id")) or {}
        return {**ref, "agent_name": agent.get("full_name"),
                "agent_phone": agent.get("phone"), "ref_code": agent.get("ref_code"),
                "friend_name": friend.get("full_name"),
                "friend_phone": friend.get("phone")}

    async def referrals(self, *, agent_id=None, since=None, until=None, limit=1000):
        rows = [self._referral_row(r) for r in self.referrals_.values()
                if (not agent_id or r["agent_id"] == agent_id)
                and (since is None or r["created_at"] >= since)
                and (until is None or r["created_at"] < until)]
        rows.sort(key=lambda r: r["id"], reverse=True)
        return rows[:limit]

    async def referral_of_tg(self, tg_id):
        return next((self._referral_row(r) for r in self.referrals_.values()
                     if r["tg_id"] == tg_id), None)

    async def referral_of_client(self, client_id):
        return next((self._referral_row(r) for r in self.referrals_.values()
                     if r.get("client_id") == client_id), None)

    async def add_referral(self, *, agent_id, tg_id):
        if any(r["tg_id"] == tg_id for r in self.referrals_.values()):
            return None
        ref_id = self._id()
        self.referrals_[ref_id] = {
            "id": ref_id, "agent_id": agent_id, "tg_id": tg_id, "client_id": None,
            "status": "click", "bonus": Decimal(0), "ledger_id": None, "note": None,
            "created_at": self._now(), "signed_at": None, "rented_at": None,
            "paid_at": None}
        return ref_id

    async def update_referral(self, ref_id, **fields):
        if ref_id in self.referrals_:
            self.referrals_[ref_id].update(fields)

    async def pay_referral_bonus(self, ref_id, *, agent_id, amount, note, created_by):
        ref = self.referrals_.get(ref_id)
        if ref is None or ref["status"] == "paid":
            return None
        ref.update(status="paid", paid_at=self._now(), bonus=Decimal(str(amount)))
        ledger_id = await self.add_ledger(client_id=agent_id, kind="adjust",
                                          amount=Decimal(str(amount)), note=note,
                                          created_by=created_by)
        ref["ledger_id"] = ledger_id
        return ledger_id

    # ─────────────────────────── склад запчастей ───────────────────────────

    async def suppliers(self, *, active_only=False):
        rows = []
        for sup in self.suppliers_.values():
            if active_only and not sup["active"]:
                continue
            docs = [d for d in self.part_docs_.values()
                    if d["supplier_id"] == sup["id"] and d["kind"] == "receipt"]
            rows.append({**sup, "receipts": len(docs),
                         "spent": sum((d["total"] for d in docs), Decimal(0)),
                         "last_at": max((d["created_at"] for d in docs), default=None)})
        return sorted(rows, key=lambda s: (not s["active"], s["name"]))

    async def supplier(self, supplier_id):
        sup = self.suppliers_.get(supplier_id)
        return dict(sup) if sup else None

    async def create_supplier(self, *, name, phone, note):
        if any(s["name"] == name for s in self.suppliers_.values()):
            raise UniqueError("supplier name")
        sid = self._id()
        self.suppliers_[sid] = {"id": sid, "name": name, "phone": phone, "note": note,
                                "active": True, "created_at": self._now()}
        return sid

    async def update_supplier(self, supplier_id, **fields):
        if supplier_id in self.suppliers_:
            self.suppliers_[supplier_id].update(fields)

    def _part_row(self, part):
        return {**part, "node_title": crm_logic.REPAIR_NODES.get(part.get("node"))}

    async def parts(self, *, active_only=False, node=None, q=None):
        rows = [self._part_row(p) for p in self.parts_.values()
                if (not active_only or p["active"])
                and (not node or p.get("node") == node)
                and (not q or q.lower() in f"{p['title']} {p.get('model') or ''}".lower())]
        return sorted(rows, key=lambda p: p["title"])

    async def part(self, part_id):
        part = self.parts_.get(part_id)
        return self._part_row(part) if part else None

    async def part_by_title(self, title):
        return next((self._part_row(p) for p in self.parts_.values()
                     if p["title"].lower() == str(title).lower()), None)

    async def create_part(self, *, title, node, unit, cost, price, min_stock,
                          model, note):
        if any(p["title"].lower() == title.lower() for p in self.parts_.values()):
            raise UniqueError("part title")
        pid = self._id()
        self.parts_[pid] = {"id": pid, "title": title, "node": node, "unit": unit,
                            "cost": Decimal(str(cost)), "price": Decimal(str(price)),
                            "min_stock": int(min_stock), "model": model,
                            "active": True, "note": note, "created_at": self._now()}
        return pid

    async def update_part(self, part_id, **fields):
        if part_id in self.parts_:
            self.parts_[part_id].update(fields)

    async def stock_map(self):
        out: dict[int, int] = {}
        for move in self.part_moves_:
            out[move["part_id"]] = out.get(move["part_id"], 0) + int(move["qty"])
        return out

    async def part_stock(self, part_id):
        return sum(int(m["qty"]) for m in self.part_moves_ if m["part_id"] == part_id)

    def _move_row(self, move):
        part = self.parts_.get(move["part_id"]) or {}
        doc = self.part_docs_.get(move.get("doc_id")) or {}
        order = self.orders_.get(move.get("order_id")) or {}
        return {**move, "part_title": part.get("title"), "part_unit": part.get("unit"),
                "doc_no": doc.get("no"), "doc_kind": doc.get("kind"),
                "order_no": order.get("no")}

    async def part_moves(self, *, part_id=None, kind=None, order_id=None, limit=300):
        rows = [self._move_row(m) for m in self.part_moves_
                if (not part_id or m["part_id"] == part_id)
                and (not kind or m["kind"] == kind)
                and (not order_id or m.get("order_id") == order_id)]
        rows.sort(key=lambda m: m["id"], reverse=True)
        return rows[:limit]

    async def add_part_move(self, *, part_id, kind, qty, cost, doc_id=None,
                            order_id=None, note=None, created_by=None):
        move_id = self._id()
        self.part_moves_.append({"id": move_id, "part_id": part_id, "kind": kind,
                                 "qty": int(qty), "cost": Decimal(str(cost)),
                                 "doc_id": doc_id, "order_id": order_id, "note": note,
                                 "created_by": created_by, "created_at": self._now()})
        return move_id

    async def part_docs(self, *, kind=None, limit=200):
        rows = []
        for doc in self.part_docs_.values():
            if kind and doc["kind"] != kind:
                continue
            sup = self.suppliers_.get(doc.get("supplier_id")) or {}
            lines = sum(1 for m in self.part_moves_ if m.get("doc_id") == doc["id"])
            rows.append({**doc, "supplier_name": sup.get("name"), "lines": lines})
        rows.sort(key=lambda d: d["id"], reverse=True)
        return rows[:limit]

    async def part_doc(self, doc_id):
        doc = self.part_docs_.get(doc_id)
        if doc is None:
            return None
        sup = self.suppliers_.get(doc.get("supplier_id")) or {}
        return {**doc, "supplier_name": sup.get("name")}

    async def create_part_doc(self, *, kind, supplier_id, lines, note, created_by):
        doc_id = self._id()
        same = [d for d in self.part_docs_.values() if d["kind"] == kind]
        if kind == "receipt":
            total = sum((Decimal(str(line["price"])) * int(line["qty"])
                         for line in lines), Decimal(0))
        else:
            total = sum((Decimal(str(line.get("cost") or 0)) * int(line["qty"])
                         for line in lines), Decimal(0))
        self.part_docs_[doc_id] = {
            "id": doc_id, "no": crm_logic.doc_no(kind, len(same) + 1), "kind": kind,
            "supplier_id": supplier_id, "total": total, "note": note,
            "created_by": created_by, "created_at": self._now()}
        for line in lines:
            part_id, qty = int(line["part_id"]), int(line["qty"])
            part = self.parts_[part_id]
            if kind == "receipt":
                stock = await self.part_stock(part_id)
                part["cost"] = crm_logic.average_cost(stock, part["cost"], qty,
                                                      line["price"])
                move_qty, move_cost = qty, Decimal(str(line["price"]))
            else:
                move_qty = -qty
                move_cost = Decimal(str(line.get("cost") or 0))
            await self.add_part_move(part_id=part_id, kind=kind, qty=move_qty,
                                     cost=move_cost, doc_id=doc_id,
                                     note=line.get("note"), created_by=created_by)
        return doc_id

    def _part_order_row(self, order):
        sup = self.suppliers_.get(order.get("supplier_id")) or {}
        lines = sum(1 for i in self.part_order_items_ if i["order_id"] == order["id"])
        return {**order, "supplier_name": sup.get("name"), "lines": lines}

    async def part_orders(self, *, status=None, limit=200):
        rows = [self._part_order_row(o) for o in self.part_orders_.values()
                if not status or o["status"] == status]
        rows.sort(key=lambda o: o["id"], reverse=True)
        return rows[:limit]

    async def part_order(self, order_id):
        order = self.part_orders_.get(order_id)
        return self._part_order_row(order) if order else None

    async def open_part_order(self):
        rows = [o for o in self.part_orders_.values() if o["status"] == "new"]
        return self._part_order_row(rows[-1]) if rows else None

    async def create_part_order(self, *, supplier_id, note, created_by):
        order_id = self._id()
        self.part_orders_[order_id] = {
            "id": order_id, "no": crm_logic.part_order_no(len(self.part_orders_) + 1),
            "supplier_id": supplier_id, "status": "new", "total": Decimal(0),
            "note": note, "created_by": created_by, "created_at": self._now(),
            "ordered_at": None, "closed_at": None, "doc_id": None}
        return order_id

    async def update_part_order(self, order_id, **fields):
        if order_id in self.part_orders_:
            self.part_orders_[order_id].update(fields)

    async def part_order_items(self, order_id):
        rows = []
        for item in self.part_order_items_:
            if item["order_id"] != order_id:
                continue
            part = self.parts_.get(item["part_id"]) or {}
            work = self.orders_.get(item.get("work_order_id")) or {}
            rows.append({**item, "title": part.get("title"), "unit": part.get("unit"),
                         "node": part.get("node"), "work_order_no": work.get("no")})
        return sorted(rows, key=lambda i: i["title"] or "")

    async def add_part_order_item(self, order_id, *, part_id, qty, price, source,
                                  work_order_id=None):
        if any(i["order_id"] == order_id and i["part_id"] == part_id
               for i in self.part_order_items_):
            return None
        item_id = self._id()
        self.part_order_items_.append({
            "id": item_id, "order_id": order_id, "part_id": part_id, "qty": int(qty),
            "price": Decimal(str(price)), "source": source,
            "work_order_id": work_order_id, "created_at": self._now()})
        return item_id

    async def delete_part_order_item(self, order_id, item_id):
        before = len(self.part_order_items_)
        self.part_order_items_ = [i for i in self.part_order_items_
                                  if not (i["order_id"] == order_id
                                          and i["id"] == item_id)]
        return len(self.part_order_items_) < before

    async def waiting_orders_parts(self):
        rows = []
        for order in self.orders_.values():
            if order["status"] != "waiting":
                continue
            bike = self.bikes_.get(order.get("bike_id")) or {}
            items = [i for i in self.order_items_ if i["order_id"] == order["id"]]
            for item in items:
                part = next((p for p in self.parts_.values()
                             if p.get("node") and p["node"] == item.get("node")
                             and p["active"]), None)
                title = (part or {}).get("title") or crm_logic.REPAIR_NODES.get(
                    item.get("node"), "Без узла")
                rows.append({"work_order_id": order["id"], "work_order_no": order["no"],
                             "bike_code": bike.get("code"), "node": item.get("node"),
                             "part_id": (part or {}).get("id"), "title": title,
                             "qty": int(item.get("qty") or 1)})
        return rows

    # ─────────────────── замена велосипеда в аренде ───────────────────

    async def rental_bikes(self, rental_id):
        rows = []
        for row in self.rental_bikes_:
            if row["rental_id"] != rental_id:
                continue
            bike = self.bikes_.get(row["bike_id"]) or {}
            rows.append({**row, "bike_code": bike.get("code"),
                         "bike_model": bike.get("model"),
                         "bike_status": bike.get("status"),
                         "bike_mileage": bike.get("mileage_km")})
        return sorted(rows, key=lambda r: r["id"])

    async def open_rental_bike(self, rental_id):
        rows = [r for r in self.rental_bikes_
                if r["rental_id"] == rental_id and r["returned_on"] is None]
        return dict(rows[-1]) if rows else None

    async def add_rental_bike(self, rental_id, *, bike_id, issued_on, mileage_start,
                              reason, created_by):
        row_id = self._id()
        self.rental_bikes_.append({
            "id": row_id, "rental_id": rental_id, "bike_id": bike_id,
            "issued_on": issued_on, "returned_on": None,
            "mileage_start": mileage_start, "mileage_end": None, "reason": reason,
            "created_by": created_by, "created_at": self._now()})
        return row_id

    async def swap_rental_bike(self, rental_id, *, old_bike_id, new_bike_id,
                               old_status, mileage_old, mileage_new, reason, today, by):
        rental = self.rentals_.get(rental_id)
        if rental is None or rental["status"] != "active":
            return False
        if rental.get("bike_id") != old_bike_id:
            return False
        if old_bike_id is not None:
            open_row = await self.open_rental_bike(rental_id)
            if open_row is None:
                await self.add_rental_bike(
                    rental_id, bike_id=old_bike_id, issued_on=rental["started_on"],
                    mileage_start=rental.get("mileage_start"), reason="Выдача",
                    created_by=by)
            for row in self.rental_bikes_:
                if row["rental_id"] == rental_id and row["returned_on"] is None:
                    row["returned_on"] = today
                    row["mileage_end"] = mileage_old
            bike = self.bikes_[old_bike_id]
            before = bike["status"]
            bike["status"] = old_status
            if mileage_old is not None:
                bike["mileage_km"] = max(bike.get("mileage_km") or 0, int(mileage_old))
            if before != old_status:
                self._log_status(old_bike_id, before, old_status, by)
        await self.add_rental_bike(rental_id, bike_id=new_bike_id, issued_on=today,
                                   mileage_start=mileage_new, reason=reason,
                                   created_by=by)
        new_bike = self.bikes_[new_bike_id]
        before = new_bike["status"]
        new_bike["status"] = "rented"
        if mileage_new is not None:
            new_bike["mileage_km"] = max(new_bike.get("mileage_km") or 0,
                                         int(mileage_new))
        if before != "rented":
            self._log_status(new_bike_id, before, "rented", by)
        rental.update(bike_id=new_bike_id, mileage_start=mileage_new or 0,
                      mileage_end=None)
        return True

    # ─────────────────── закупки основных средств ───────────────────

    async def purchases(self, *, limit=200):
        rows = []
        for purchase in self.purchases_.values():
            sup = self.suppliers_.get(purchase.get("supplier_id")) or {}
            bikes = [b for b in self.bikes_.values()
                     if b.get("purchase_id") == purchase["id"]]
            rows.append({**purchase, "supplier_name": sup.get("name"),
                         "bikes": len(bikes),
                         "written_off": sum(1 for b in bikes
                                            if b["status"] == "written_off"),
                         "spent": sum((b.get("purchase_price") or Decimal(0)
                                       for b in bikes), Decimal(0))})
        rows.sort(key=lambda p: p["id"], reverse=True)
        return rows[:limit]

    async def purchase(self, purchase_id):
        purchase = self.purchases_.get(purchase_id)
        if purchase is None:
            return None
        sup = self.suppliers_.get(purchase.get("supplier_id")) or {}
        return {**purchase, "supplier_name": sup.get("name")}

    async def create_purchase(self, *, supplier_id, purchased_on, note, bikes,
                              created_by):
        purchase_id = self._id()
        self.purchases_[purchase_id] = {
            "id": purchase_id,
            "no": crm_logic.purchase_no(len(self.purchases_) + 1),
            "supplier_id": supplier_id, "purchased_on": purchased_on,
            "total": sum((Decimal(str(b.get("purchase_price") or 0)) for b in bikes),
                         Decimal(0)),
            "note": note, "created_by": created_by, "created_at": self._now()}
        for bike in bikes:
            bike_id = await self.create_bike(
                by=created_by, code=bike["code"], model=bike["model"],
                battery_count=bike["battery_count"],
                purchase_price=bike["purchase_price"], purchased_on=purchased_on,
                location=bike.get("location"), service_months=bike["service_months"],
                residual_price=bike["residual_price"],
                battery_price=bike.get("battery_price"),
                battery_service_months=bike["battery_service_months"],
                note=bike.get("note"))
            self.bikes_[bike_id]["purchase_id"] = purchase_id
        return purchase_id

    async def purchase_bikes(self, purchase_id):
        rows = [dict(b) for b in self.bikes_.values()
                if b.get("purchase_id") == purchase_id]
        return sorted(rows, key=lambda b: b["code"])

    # ───────────────── справочники и батареи ─────────────────

    async def locations(self, *, active_only=False):
        rows = [dict(x) for x in self.locations_.values()
                if not active_only or x["active"]]
        return sorted(rows, key=lambda x: (x["sort"], x["name"]))

    async def location_names(self):
        return [x["name"] for x in await self.locations(active_only=True)]

    async def create_location(self, *, name, city, address, note, **extra):
        if any(x["name"] == name for x in self.locations_.values()):
            raise UniqueError("location name")
        loc_id = self._id()
        self.locations_[loc_id] = {"id": loc_id, "name": name, "city": city,
                                   "address": address, "note": note, "active": True,
                                   "sort": 100, "public_title": None, "phone": None,
                                   "hours": None, "lat": None, "lon": None,
                                   "created_at": self._now(), **extra}
        return loc_id

    async def update_location(self, location_id, **fields):
        if location_id in self.locations_:
            self.locations_[location_id].update(fields)

    async def bike_models(self, *, active_only=False):
        rows = []
        for model in self.bike_models_.values():
            if active_only and not model["active"]:
                continue
            rows.append({**model, "bikes": sum(1 for b in self.bikes_.values()
                                               if b["model"] == model["title"])})
        return sorted(rows, key=lambda m: (not m["active"], m["title"]))

    async def bike_model(self, model_id):
        model = self.bike_models_.get(model_id)
        return dict(model) if model else None

    async def create_bike_model(self, *, title, brand, factory_title, battery_slots,
                                note, **specs):
        if any(m["title"] == title for m in self.bike_models_.values()):
            raise UniqueError("bike model")
        model_id = self._id()
        self.bike_models_[model_id] = {"id": model_id, "title": title, "brand": brand,
                                       "factory_title": factory_title,
                                       "battery_slots": int(battery_slots),
                                       "active": True, "note": note,
                                       "weight_kg": None, "speed_kmh": None,
                                       "range_km": None, "charge_hours": None,
                                       "wheel_size": None, "motor_watt": None,
                                       "max_load_kg": None, "size_note": None,
                                       "photo_url": None, "description": None,
                                       "created_at": self._now(), **specs}
        return model_id

    async def update_bike_model(self, model_id, **fields):
        if model_id in self.bike_models_:
            self.bike_models_[model_id].update(fields)

    async def battery_models(self, *, active_only=False):
        rows = []
        for model in self.battery_models_.values():
            if active_only and not model["active"]:
                continue
            rows.append({**model, "batteries": sum(
                1 for b in self.batteries_.values() if b.get("model_id") == model["id"])})
        return sorted(rows, key=lambda m: (not m["active"], m["title"]))

    async def battery_model(self, model_id):
        model = self.battery_models_.get(model_id)
        return dict(model) if model else None

    async def create_battery_model(self, *, title, brand, voltage, capacity, price,
                                   service_months, note):
        if any(m["title"] == title for m in self.battery_models_.values()):
            raise UniqueError("battery model")
        model_id = self._id()
        self.battery_models_[model_id] = {
            "id": model_id, "title": title, "brand": brand, "voltage": voltage,
            "capacity": capacity, "price": Decimal(str(price or 0)),
            "service_months": int(service_months), "active": True, "note": note,
            "created_at": self._now()}
        return model_id

    async def update_battery_model(self, model_id, **fields):
        if model_id in self.battery_models_:
            self.battery_models_[model_id].update(fields)

    async def compat_pairs(self):
        rows = []
        for (bike_id, battery_id), primary in self.compat_.items():
            bike = self.bike_models_.get(bike_id) or {}
            battery = self.battery_models_.get(battery_id) or {}
            rows.append({"bike_model_id": bike_id, "battery_model_id": battery_id,
                         "primary_fit": primary, "bike_title": bike.get("title"),
                         "battery_title": battery.get("title")})
        return sorted(rows, key=lambda r: (r["bike_title"] or "", r["battery_title"] or ""))

    async def set_compat(self, bike_model_id, battery_model_id, *, fits,
                         primary_fit=False):
        key = (bike_model_id, battery_model_id)
        if not fits:
            self.compat_.pop(key, None)
            return
        self.compat_[key] = bool(primary_fit)

    async def compat_for_bike_model(self, title):
        model = next((m for m in self.bike_models_.values() if m["title"] == title), None)
        if model is None:
            return []
        rows = []
        for (bike_id, battery_id), primary in self.compat_.items():
            if bike_id != model["id"]:
                continue
            battery = self.battery_models_.get(battery_id)
            if battery and battery["active"]:
                rows.append({**battery, "primary_fit": primary})
        return sorted(rows, key=lambda r: (not r["primary_fit"], r["title"]))

    def _battery_row(self, battery):
        model = self.battery_models_.get(battery.get("model_id")) or {}
        bike = self.bikes_.get(battery.get("bike_id")) or {}
        rental = self.rentals_.get(battery.get("rental_id")) or {}
        client = self.clients_.get(rental.get("client_id")) or {}
        return {**battery, "model_title": model.get("title"),
                "voltage": model.get("voltage"), "capacity": model.get("capacity"),
                "model_price": model.get("price"), "bike_code": bike.get("code"),
                "bike_model": bike.get("model"),
                "client_name": client.get("full_name") or None,
                "client_id": rental.get("client_id")
                if rental.get("status") == "active" else None}

    async def batteries(self, *, status=None, q=None, location=None, bike_id=None,
                        rental_id=None, limit=1000):
        rows = []
        for battery in self.batteries_.values():
            if status and battery["status"] != status:
                continue
            if location == "none" and battery.get("location") is not None:
                continue
            if location and location != "none" and battery.get("location") != location:
                continue
            if bike_id and battery.get("bike_id") != bike_id:
                continue
            if rental_id and battery.get("rental_id") != rental_id:
                continue
            if q and q.lower() not in \
                    f"{battery['code']} {battery.get('serial_no') or ''}".lower():
                continue
            rows.append(self._battery_row(battery))
        return sorted(rows, key=lambda b: b["code"])[:limit]

    async def battery(self, battery_id):
        battery = self.batteries_.get(battery_id)
        return self._battery_row(battery) if battery else None

    async def battery_by_code(self, code):
        return next((self._battery_row(b) for b in self.batteries_.values()
                     if b["code"] == code), None)

    def _log_battery(self, battery_id, from_status, to_status, by=None):
        self.battery_log_.append({"id": self._id(), "battery_id": battery_id,
                                  "from_status": from_status, "to_status": to_status,
                                  "changed_at": self._now(), "changed_by": by})

    async def create_battery(self, *, by=None, **fields):
        if any(b["code"] == fields.get("code") for b in self.batteries_.values()):
            raise UniqueError("battery code")
        battery_id = self._id()
        self.batteries_[battery_id] = {
            "id": battery_id, "code": None, "model_id": None, "serial_no": None,
            "status": "available", "location": None, "bike_id": None,
            "rental_id": None, "cycles": 0, "purchase_price": None,
            "purchased_on": None, "service_months": 15, "note": None,
            "created_at": self._now(), "updated_at": self._now(), **fields}
        self._log_battery(battery_id, None, self.batteries_[battery_id]["status"], by)
        return battery_id

    async def update_battery(self, battery_id, *, by=None, **fields):
        before = self.batteries_[battery_id]["status"]
        self.batteries_[battery_id].update(fields)
        if "status" in fields and fields["status"] != before:
            self._log_battery(battery_id, before, fields["status"], by)

    async def battery_status_log(self, battery_id, limit=30):
        rows = [dict(x) for x in self.battery_log_ if x["battery_id"] == battery_id]
        return sorted(rows, key=lambda x: x["id"], reverse=True)[:limit]

    async def battery_counts(self):
        out: dict[str, int] = {}
        for battery in self.batteries_.values():
            out[battery["status"]] = out.get(battery["status"], 0) + 1
        return out

    async def issue_batteries(self, rental_id, *, battery_ids, bike_id, by):
        for battery_id in battery_ids:
            battery = self.batteries_.get(battery_id)
            if battery is None or battery["status"] != "available":
                continue
            await self.update_battery(battery_id, status="rented", rental_id=rental_id,
                                      bike_id=bike_id or battery.get("bike_id"), by=by)

    async def return_batteries(self, rental_id, *, status="available", by):
        count = 0
        for battery in list(self.batteries_.values()):
            if battery.get("rental_id") == rental_id and battery["status"] == "rented":
                await self.update_battery(battery["id"], status=status, rental_id=None,
                                          cycles=int(battery.get("cycles") or 0) + 1,
                                          by=by)
                count += 1
        return count


    # ─── подписание документов (ПЭП) ───
    def _sign_row(self, request):
        client = self.clients_.get(request["client_id"]) or {}
        return {**request, "full_name": client.get("full_name"),
                "phone": client.get("phone"), "tg_id": client.get("tg_id")}

    async def sign_requests(self, *, client_id=None, limit=200):
        rows = [self._sign_row(r) for r in self.signs_.values()
                if client_id is None or r["client_id"] == client_id]
        return sorted(rows, key=lambda r: r["id"], reverse=True)[:limit]

    async def sign_request(self, request_id):
        request = self.signs_.get(request_id)
        return self._sign_row(request) if request else None

    async def sign_request_by_token(self, token):
        return next((self._sign_row(r) for r in self.signs_.values()
                     if r["token"] == token), None)

    async def create_sign_request(self, *, client_id, rental_id, token, docs,
                                  agreement, expires_at, by):
        request_id = self._id()
        number = crm_logic.sign_no(len(self.signs_) + 1)
        self.signs_[request_id] = {
            "id": request_id, "no": number, "client_id": client_id,
            "rental_id": rental_id, "token": token, "docs": list(docs),
            "agreement": agreement, "code_hash": None, "code_at": None,
            "attempts": 0, "status": "new", "expires_at": expires_at,
            "signed_at": None, "signed_ip": None, "signed_agent": None,
            "note": None, "created_by": by, "created_at": self._now()}
        await self.log_sign_event(request_id, kind="created", note=by)
        return {"id": request_id, "no": number}

    async def set_sign_agreement(self, request_id, *, agreement, docs):
        request = self.signs_.get(request_id)
        if request is not None:
            request["agreement"] = agreement
            request["docs"] = list(docs)

    async def set_sign_code(self, request_id, *, code_hash):
        request = self.signs_.get(request_id)
        if request is not None and request["status"] in ("new", "code"):
            request.update(code_hash=code_hash, code_at=self._now(), attempts=0,
                           status="code")

    async def bump_sign_attempt(self, request_id):
        request = self.signs_[request_id]
        request["attempts"] += 1
        return request["attempts"]

    async def mark_signed(self, request_id, *, ip, agent):
        request = self.signs_.get(request_id)
        if request is None or request["status"] not in ("new", "code"):
            return False
        request.update(status="signed", signed_at=self._now(), signed_ip=ip,
                       signed_agent=agent, code_hash=None)
        return True

    async def cancel_sign_request(self, request_id, *, by):
        request = self.signs_.get(request_id)
        if request is not None and request["status"] in ("new", "code"):
            request["status"] = "cancelled"
        await self.log_sign_event(request_id, kind="cancelled", note=by)

    async def log_sign_event(self, request_id, *, kind, ip=None, agent=None,
                             note=None):
        self.sign_events_.append({"id": self._id(), "request_id": request_id,
                                  "kind": kind, "at": self._now(), "ip": ip,
                                  "user_agent": agent, "note": note})

    async def sign_events(self, request_id, limit=100):
        return [dict(e) for e in self.sign_events_
                if e["request_id"] == request_id][:limit]

    # ─── рассылки ───
    async def templates(self, *, active_only=False):
        rows = [dict(t) for t in self.templates_.values()
                if not active_only or t["active"]]
        return sorted(rows, key=lambda t: t["title"])

    async def template(self, template_id):
        template = self.templates_.get(template_id)
        return dict(template) if template else None

    async def create_template(self, *, code, title, body, body_max, note):
        if any(t["code"] == code for t in self.templates_.values()):
            raise UniqueError("template code")
        template_id = self._id()
        self.templates_[template_id] = {
            "id": template_id, "code": code, "title": title, "body": body,
            "body_max": body_max, "active": True, "note": note,
            "created_at": self._now(), "updated_at": self._now()}
        return template_id

    async def update_template(self, template_id, **fields):
        if template_id in self.templates_:
            self.templates_[template_id].update(fields)
            self.templates_[template_id]["updated_at"] = self._now()

    async def clients_for_mailing(self, limit=10000):
        rows = []
        for client in self.clients_.values():
            rentals = [r for r in self.rentals_.values()
                       if r["client_id"] == client["id"]]
            last = None
            for rental in rentals:
                moment = rental.get("closed_on") or rental["started_on"]
                last = moment if last is None or moment > last else last
            rows.append({**client,
                         "balance": sum((x["amount"] for x in self.ledger_
                                         if x["client_id"] == client["id"]),
                                        Decimal(0)),
                         "last_rental_on": last})
        return sorted(rows, key=lambda c: c["full_name"])[:limit]

    async def campaigns(self, *, limit=100):
        rows = []
        for campaign in self.campaigns_.values():
            sends = [s for s in self.sends_.values()
                     if s["campaign_id"] == campaign["id"]]
            template = self.templates_.get(campaign.get("template_id")) or {}
            rows.append({**campaign, "template_title": template.get("title"),
                         "total": len(sends),
                         "sent": sum(1 for s in sends if s["status"] == "sent"),
                         "failed": sum(1 for s in sends if s["status"] == "failed")})
        return sorted(rows, key=lambda c: c["created_at"], reverse=True)[:limit]

    async def campaign(self, campaign_id):
        campaign = self.campaigns_.get(campaign_id)
        if campaign is None:
            return None
        template = self.templates_.get(campaign.get("template_id")) or {}
        return {**campaign, "template_title": template.get("title"),
                "body": template.get("body"), "body_max": template.get("body_max")}

    async def create_campaign(self, *, title, template_id, audience, note, by):
        campaign_id = self._id()
        self.campaigns_[campaign_id] = {
            "id": campaign_id, "no": crm_logic.campaign_no(len(self.campaigns_) + 1),
            "title": title, "template_id": template_id, "audience": audience,
            "status": "draft", "created_by": by, "created_at": self._now(),
            "started_at": None, "finished_at": None, "note": note}
        return campaign_id

    async def queue_sends(self, campaign_id, rows):
        added = 0
        for client_id, channel in rows:
            if any(s["campaign_id"] == campaign_id and s["client_id"] == client_id
                   for s in self.sends_.values()):
                continue
            send_id = self._id()
            self.sends_[send_id] = {"id": send_id, "campaign_id": campaign_id,
                                    "client_id": client_id, "channel": channel,
                                    "status": "queued", "error": None,
                                    "sent_at": None}
            added += 1
        return added

    async def campaign_sends(self, campaign_id, *, status=None, limit=1000):
        rows = []
        for send in self.sends_.values():
            if send["campaign_id"] != campaign_id:
                continue
            if status and send["status"] != status:
                continue
            client = self.clients_.get(send["client_id"]) or {}
            rows.append({**send, "full_name": client.get("full_name"),
                         "phone": client.get("phone"), "tg_id": client.get("tg_id"),
                         "max_id": client.get("max_id"),
                         "contract_no": client.get("contract_no")})
        return sorted(rows, key=lambda s: s["id"])[:limit]

    async def mark_send(self, send_id, *, status, error=None):
        send = self.sends_.get(send_id)
        if send is not None:
            send.update(status=status, error=error, sent_at=self._now())

    async def set_campaign_status(self, campaign_id, status):
        campaign = self.campaigns_.get(campaign_id)
        if campaign is None:
            return
        campaign["status"] = status
        if status == "sending":
            campaign["started_at"] = self._now()
        if status in ("done", "cancelled"):
            campaign["finished_at"] = self._now()

    async def sending_campaigns(self):
        return [dict(c) for c in self.campaigns_.values()
                if c["status"] == "sending"]

    async def link_client_max(self, phone, max_id):
        taken = next((c for c in self.clients_.values()
                      if c.get("max_id") == max_id and c["phone"] != phone), None)
        if taken is not None:
            return None
        client = next((c for c in self.clients_.values() if c["phone"] == phone), None)
        if client is None:
            return None
        client["max_id"] = max_id
        return client["id"]

    # ─── касса ───
    async def cash_shifts(self, *, limit=100):
        rows = [dict(x) for x in self.shifts_.values()]
        return sorted(rows, key=lambda s: s["opened_at"], reverse=True)[:limit]

    async def cash_shift(self, shift_id):
        shift = self.shifts_.get(shift_id)
        return dict(shift) if shift else None

    async def open_shift(self):
        rows = [dict(x) for x in self.shifts_.values() if x["status"] == "open"]
        return sorted(rows, key=lambda s: s["opened_at"])[0] if rows else None

    async def open_shift_at(self, location):
        return next((dict(x) for x in self.shifts_.values()
                     if x["status"] == "open"
                     and (x.get("location") or "") == (location or "")), None)

    async def create_shift(self, *, location, opening, note, by):
        shift_id = self._id()
        self.shifts_[shift_id] = {
            "id": shift_id, "no": crm_logic.shift_no(len(self.shifts_) + 1),
            "location": location, "status": "open", "opened_at": self._now(),
            "opened_by": by, "opening": Decimal(opening), "closed_at": None,
            "closed_by": None, "counted": None, "expected": None, "diff": None,
            "note": note}
        return shift_id

    async def add_cash_move(self, shift_id, *, kind, amount, reason, by,
                            ledger_id=None):
        move_id = self._id()
        self.cash_moves_.append({"id": move_id, "shift_id": shift_id, "kind": kind,
                                 "amount": Decimal(amount), "reason": reason,
                                 "ledger_id": ledger_id, "created_at": self._now(),
                                 "created_by": by})
        return move_id

    async def cash_moves(self, shift_id):
        return [dict(m) for m in self.cash_moves_ if m["shift_id"] == shift_id]

    async def shift_payments(self, shift_id):
        shift = self.shifts_.get(shift_id)
        if shift is None:
            return []
        until = shift.get("closed_at") or self._now()
        rows = []
        for entry in self.ledger_:
            if entry["kind"] not in ("payment", "refund") or entry.get("method") != "cash":
                continue
            if not shift["opened_at"] <= entry["created_at"] < until:
                continue
            client = self.clients_.get(entry["client_id"]) or {}
            rows.append({**entry, "full_name": client.get("full_name")})
        return sorted(rows, key=lambda x: x["id"])

    async def close_shift(self, shift_id, *, counted, expected, note, by):
        shift = self.shifts_.get(shift_id)
        if shift is None or shift["status"] != "open":
            return
        shift.update(status="closed", closed_at=self._now(), closed_by=by,
                     counted=Decimal(counted), expected=Decimal(expected),
                     diff=Decimal(counted) - Decimal(expected),
                     note=note or shift.get("note"))

    # ─── банк ───
    async def bank_txns(self, *, status=None, limit=200):
        rows = []
        for txn in self.bank_.values():
            if status and txn["status"] != status:
                continue
            client = self.clients_.get(txn.get("client_id")) or {}
            rows.append({**txn, "client_name": client.get("full_name")})
        return sorted(rows, key=lambda t: t["booked_at"], reverse=True)[:limit]

    async def bank_txn(self, txn_id):
        txn = self.bank_.get(txn_id)
        return dict(txn) if txn else None

    async def save_bank_txn(self, txn):
        if any(t["txn_id"] == txn["txn_id"] for t in self.bank_.values()):
            return None
        row_id = self._id()
        self.bank_[row_id] = {"id": row_id, "txn_id": txn["txn_id"],
                              "account": txn.get("account"),
                              "booked_at": txn["booked_at"],
                              "amount": Decimal(txn["amount"]),
                              "direction": txn["direction"],
                              "payer_name": txn.get("payer_name"),
                              "payer_inn": txn.get("payer_inn"),
                              "purpose": txn.get("purpose"), "status": "new",
                              "client_id": None, "ledger_id": None,
                              "handled_at": None, "handled_by": None,
                              "created_at": self._now()}
        return row_id

    async def mark_bank_txn(self, txn_id, *, status, client_id=None,
                            ledger_id=None, by):
        txn = self.bank_.get(txn_id)
        if txn is not None:
            txn.update(status=status, client_id=client_id, ledger_id=ledger_id,
                       handled_at=self._now(), handled_by=by)

    async def last_bank_txn_at(self):
        moments = [t["booked_at"] for t in self.bank_.values()]
        return max(moments) if moments else None

    # ─── трекеры ───
    def _tracker_row(self, tracker):
        bike = self.bikes_.get(tracker.get("bike_id")) or {}
        rental = next((r for r in self.rentals_.values()
                       if r.get("bike_id") == tracker.get("bike_id")
                       and r["status"] == "active"), None) or {}
        client = self.clients_.get(rental.get("client_id")) or {}
        return {**tracker, "bike_code": bike.get("code"),
                "bike_model": bike.get("model"), "bike_status": bike.get("status"),
                "rental_id": rental.get("id"),
                "client_id": rental.get("client_id"),
                "client_name": client.get("full_name"),
                "client_phone": client.get("phone")}

    async def trackers(self, *, active_only=False, unbound=False):
        rows = []
        for tracker in self.trackers_.values():
            if active_only and not tracker["active"]:
                continue
            if unbound and tracker.get("bike_id") is not None:
                continue
            rows.append(self._tracker_row(tracker))
        return sorted(rows, key=lambda t: (t["bike_code"] is None,
                                           t["bike_code"] or "", t["device_id"]))

    async def tracker(self, tracker_id):
        tracker = self.trackers_.get(tracker_id)
        return self._tracker_row(tracker) if tracker else None

    async def tracker_by_device(self, device_id):
        return next((self._tracker_row(t) for t in self.trackers_.values()
                     if t["device_id"] == device_id), None)

    async def tracker_of_bike(self, bike_id):
        return next((self._tracker_row(t) for t in self.trackers_.values()
                     if t.get("bike_id") == bike_id), None)

    async def create_tracker(self, **fields):
        if any(t["device_id"] == fields.get("device_id")
               for t in self.trackers_.values()):
            raise UniqueError("tracker device_id")
        tracker_id = self._id()
        self.trackers_[tracker_id] = {
            "id": tracker_id, "device_id": None, "alias": None, "bike_id": None,
            "active": True, "last_seen": None, "lat": None, "lon": None,
            "speed": None, "course": None, "voltage": None, "gsm_level": None,
            "alarm": False, "note": None, "created_at": self._now(),
            "updated_at": self._now(), **fields}
        return tracker_id

    async def update_tracker(self, tracker_id, **fields):
        if fields.get("bike_id") is not None and any(
                t["id"] != tracker_id and t.get("bike_id") == fields["bike_id"]
                for t in self.trackers_.values()):
            raise UniqueError("tracker bike_id")
        if tracker_id in self.trackers_:
            self.trackers_[tracker_id].update(fields)
            self.trackers_[tracker_id]["updated_at"] = self._now()

    async def save_tracker_state(self, device):
        tracker = next((t for t in self.trackers_.values()
                        if t["device_id"] == device["device_id"]), None)
        created = tracker is None
        if created:
            tracker_id = await self.create_tracker(device_id=device["device_id"],
                                                   alias=device.get("alias"))
            tracker = self.trackers_[tracker_id]
        for key in ("alias", "last_seen", "lat", "lon", "voltage"):
            source = "recorded_at" if key == "last_seen" else key
            if device.get(source) is not None:
                tracker[key] = device[source]
        tracker["speed"] = _num(device.get("speed"))
        tracker["course"] = device.get("course")
        tracker["gsm_level"] = device.get("gsm_level")
        tracker["alarm"] = bool(device.get("alarm"))
        tracker["voltage"] = _num(tracker.get("voltage"))
        tracker["updated_at"] = self._now()
        if device.get("lat") is not None and device.get("recorded_at") is not None:
            known = any(p["tracker_id"] == tracker["id"]
                        and p["recorded_at"] == device["recorded_at"]
                        for p in self.positions_)
            if not known:
                self.positions_.append({
                    "id": self._id(), "tracker_id": tracker["id"],
                    "lat": device["lat"], "lon": device["lon"],
                    "speed": _num(device.get("speed")), "course": device.get("course"),
                    "recorded_at": device["recorded_at"]})
        return {"id": tracker["id"], "created": created}

    async def tracker_positions(self, tracker_id, limit=200):
        rows = [dict(p) for p in self.positions_ if p["tracker_id"] == tracker_id]
        return sorted(rows, key=lambda p: p["recorded_at"], reverse=True)[:limit]

    async def purge_tracker_positions(self, days=30):
        edge = self._now() - timedelta(days=days)
        before = len(self.positions_)
        self.positions_ = [p for p in self.positions_ if p["recorded_at"] >= edge]
        return before - len(self.positions_)

    async def tracker_alerts(self, *, open_only=True, limit=200):
        rows = []
        for alert in self.alerts_.values():
            if open_only and alert["handled_at"] is not None:
                continue
            tracker = self.trackers_.get(alert["tracker_id"]) or {}
            bike = self.bikes_.get(alert.get("bike_id")) or {}
            rows.append({**alert, "device_id": tracker.get("device_id"),
                         "alias": tracker.get("alias"), "bike_code": bike.get("code"),
                         "bike_model": bike.get("model")})
        return sorted(rows, key=lambda a: a["id"], reverse=True)[:limit]

    async def raise_alert(self, *, tracker_id, kind, note, bike_id, lat, lon):
        if any(a["tracker_id"] == tracker_id and a["kind"] == kind
               and a["handled_at"] is None for a in self.alerts_.values()):
            return None
        alert_id = self._id()
        self.alerts_[alert_id] = {"id": alert_id, "tracker_id": tracker_id,
                                  "bike_id": bike_id, "kind": kind, "note": note,
                                  "lat": lat, "lon": lon, "created_at": self._now(),
                                  "handled_at": None, "handled_by": None}
        return alert_id

    async def close_alerts(self, tracker_id, kinds, *, by=None):
        count = 0
        for alert in self.alerts_.values():
            if (alert["tracker_id"] == tracker_id and alert["kind"] in kinds
                    and alert["handled_at"] is None):
                alert["handled_at"], alert["handled_by"] = self._now(), by
                count += 1
        return count

    async def handle_alert(self, alert_id, *, by):
        alert = self.alerts_.get(alert_id)
        if alert and alert["handled_at"] is None:
            alert["handled_at"], alert["handled_by"] = self._now(), by


    # ─────────────────── приём оплаты ───────────────────

    async def create_pay_order(self, *, client_id, rental_id, amount, purpose,
                               kind="link", created_by=None):
        oid = self._id()
        number = len(self.pay_orders_) + 1
        self.pay_orders_[oid] = {
            "id": oid, "no": crm_logic.pay_no(number), "client_id": client_id,
            "rental_id": rental_id, "amount": Decimal(amount), "purpose": purpose,
            "kind": kind, "status": "new", "provider": "tochka",
            "operation_id": None, "link": None, "error": None, "ledger_id": None,
            "created_by": created_by, "created_at": self._now(),
            "sent_at": None, "paid_at": None, "checked_at": None}
        return oid

    async def set_pay_link(self, order_id, *, link, operation_id):
        order = self.pay_orders_.get(order_id)
        if order is None or order["status"] != "new":
            return
        # Одна операция банка - один счёт, как частичный уникальный индекс.
        if operation_id and any(o["operation_id"] == operation_id
                                for o in self.pay_orders_.values()):
            raise UniqueError("operation_id")
        order.update(link=link, operation_id=operation_id, status="sent",
                     sent_at=self._now(), error=None)

    async def mark_pay_paid(self, order_id, *, method="card", by=None):
        order = self.pay_orders_.get(order_id)
        if order is None or order["status"] == "paid":
            return None
        ledger_id = await self.add_ledger(
            client_id=order["client_id"], rental_id=order["rental_id"],
            kind="payment", amount=order["amount"], method=method,
            note=f"Счёт {order['no']}", created_by=by or "эквайринг")
        order.update(status="paid", paid_at=self._now(), checked_at=self._now(),
                     ledger_id=ledger_id, error=None)
        return ledger_id

    async def mark_pay_failed(self, order_id, *, error):
        order = self.pay_orders_.get(order_id)
        if order is not None and order["status"] != "paid":
            order.update(status="failed", checked_at=self._now(), error=error[:500])

    async def touch_pay_order(self, order_id):
        order = self.pay_orders_.get(order_id)
        if order is not None:
            order["checked_at"] = self._now()

    async def cancel_pay_order(self, order_id, *, by):
        order = self.pay_orders_.get(order_id)
        if order is not None and order["status"] in ("new", "sent"):
            order.update(status="cancelled", checked_at=self._now(),
                         error=f"снял {by}")

    def _pay_row(self, order):
        client = self.clients_.get(order["client_id"], {})
        return {**order, "full_name": client.get("full_name"),
                "phone": client.get("phone"), "tg_id": client.get("tg_id"),
                "max_id": client.get("max_id"),
                "contract_no": client.get("contract_no")}

    async def pay_order(self, order_id):
        order = self.pay_orders_.get(order_id)
        return self._pay_row(order) if order else None

    async def pay_orders(self, *, client_id=None, status=None, limit=200):
        rows = [self._pay_row(o) for o in self.pay_orders_.values()
                if (client_id is None or o["client_id"] == client_id)
                and (status is None or o["status"] == status)]
        rows.sort(key=lambda o: o["id"], reverse=True)
        return rows[:limit]

    async def open_pay_orders(self, limit=200):
        rows = [self._pay_row(o) for o in self.pay_orders_.values()
                if o["status"] in ("new", "sent") and o["operation_id"]]
        rows.sort(key=lambda o: o["id"])
        return rows[:limit]

    async def save_card_token(self, *, client_id, token, mask=None, expires=None,
                              provider="tochka"):
        for card in self.cards_.values():
            if card["client_id"] == client_id and card["provider"] == provider:
                card["active"] = False
        cid = self._id()
        self.cards_[cid] = {"id": cid, "client_id": client_id, "provider": provider,
                            "token": token, "mask": mask, "expires": expires,
                            "active": True, "created_at": self._now(),
                            "used_at": None}
        return cid

    async def card_of(self, client_id, provider="tochka"):
        for card in self.cards_.values():
            if (card["client_id"] == client_id and card["provider"] == provider
                    and card["active"]):
                return dict(card)
        return None

    async def cards(self):
        return [dict(c) for c in self.cards_.values() if c["active"]]

    async def drop_card(self, client_id, provider="tochka"):
        for card in self.cards_.values():
            if (card["client_id"] == client_id and card["provider"] == provider
                    and card["active"]):
                card["active"] = False

    async def touch_card(self, card_id):
        card = self.cards_.get(card_id)
        if card is not None:
            card["used_at"] = self._now()


    # ─────────────────── уведомления ───────────────────

    async def notices(self):
        return [dict(n) for n in sorted(self.notices_.values(),
                                        key=lambda n: n["code"])]

    async def set_notice(self, code, *, enabled, at_hour, at_minute=0,
                         chat_id=None, extra=None, by=None):
        self.notices_[code] = {"code": code, "enabled": bool(enabled),
                               "at_hour": at_hour, "at_minute": at_minute,
                               "chat_id": chat_id, "extra": dict(extra or {}),
                               "updated_by": by, "updated_at": self._now()}

    async def log_notice(self, code, *, target, status, client_id=None,
                         detail=None):
        self.notice_log_.append({
            "id": self._id(), "code": code, "client_id": client_id,
            "target": target, "status": status,
            "detail": (detail or "")[:500] or None, "created_at": self._now()})

    async def notice_log(self, *, code=None, limit=200):
        rows = [dict(n, full_name=(self.clients_.get(n["client_id"]) or {}).get("full_name"))
                for n in reversed(self.notice_log_)
                if code is None or n["code"] == code]
        return rows[:limit]

    async def notice_counts(self, days=30):
        edge = self._now() - timedelta(days=days)
        out = {}
        for row in self.notice_log_:
            if row["status"] == "sent" and row["created_at"] >= edge:
                out[row["code"]] = out.get(row["code"], 0) + 1
        return out

    async def purge_notice_log(self, days):
        edge = self._now() - timedelta(days=days)
        before = len(self.notice_log_)
        self.notice_log_ = [n for n in self.notice_log_ if n["created_at"] >= edge]
        return before - len(self.notice_log_)

    async def repairs_since(self, bike_id, since):
        return sum(1 for x in self.bike_log_
                   if x["bike_id"] == bike_id and x["kind"] == "repair"
                   and x["created_at"].date() >= since)


def _num(value):
    """Число из внешнего API - в Decimal, как numeric в базе."""
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (ArithmeticError, ValueError):
        return None


class UniqueError(Exception):
    """Аналог asyncpg.UniqueViolationError: имя класса содержит «unique»."""


UniqueViolationError = UniqueError
