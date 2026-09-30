"""«Входящие» одной лентой: всё, что клиент прислал сам, - сообщение,
заявка на аренду из кабинета, «Я оплатил».

Для человека на точке это одно и то же: клиент чего-то ждёт, и ответить
надо тому, кто ждёт дольше. Раньше это были три раздела меню («Входящие»,
«Брони», «Заявки»), и ждущий в соседнем разделе оставался незамеченным.
Данные при этом остаются разными и лежат в своих таблицах: обращение -
разговор (inbox_threads), заявка - намерение взять велосипед (bookings),
«Я оплатил» - деньги, которые надо сверить с банком (payment_claims).
Склеивать их в одну таблицу нельзя: у каждого своя жизнь и свои права
(переписка - ПДн, по умолчанию только владельцу). Лента - только экран:
строка ведёт туда, где с этим работают, а часть ленты видна только с
правом на свой раздел.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from . import logic

# Вид строки: подпись вкладки, раздел профиля, адрес его полного экрана.
KINDS: dict[str, tuple[str, str, str]] = {
    "message": ("Сообщения", "inbox", "/inbox"),
    "booking": ("Заявки на аренду", "issue", "/bookings"),
    "claim": ("«Я оплатил»", "claims", "/claims"),
}
TAGS = {"message": "сообщение", "booking": "заявка", "claim": "оплата"}


def visible_kinds(staff: Mapping[str, Any] | None) -> list[str]:
    return [k for k, (_, section, _) in KINDS.items() if logic.can_view(staff, section)]


def _moment(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return None


def incoming_rows(*, threads: Iterable[Mapping[str, Any]] = (),
                  bookings: Iterable[Mapping[str, Any]] = (),
                  claims: Iterable[Mapping[str, Any]] = (),
                  today: date | None = None, money_ok: bool = False,
                  booking_url: Callable[[Mapping[str, Any]], str] | None = None
                  ) -> list[dict[str, Any]]:
    """Строки ленты: кто ждёт дольше - выше. `threads` - строки
    logic.inbox_rows с `preview`; `bookings` - только открытые; `claims` -
    ждущие зачисления. Сумма «Я оплатил» - только с правом на финансы."""
    today = today or date.today()
    rows: list[dict[str, Any]] = []
    for t in threads:
        # Ждёт тот, у кого есть «ждёт с» (или новое обращение): ответ из
        # панели или из чата его снимает. Отвеченное «в работе» остаётся в
        # ленте, но ниже ждущих и без времени ожидания - иначе разговор,
        # отвеченный три дня назад, стоял бы над тем, кто пишет сейчас.
        waiting = t.get("waiting_since") is not None or t.get("status") == "new"
        since = (_moment(t.get("waiting_since") or t.get("last_in_at") or t.get("created_at"))
                 if waiting else None)
        rows.append({
            "kind": "message", "tag": t.get("channel_label") or TAGS["message"],
            "who": t.get("who") or "—",
            "what": t.get("preview") or t.get("subject") or "",
            "since": since, "url": f"/inbox/{t['id']}", "hot": waiting,
            "state": t.get("status_label") or ""})
    for b in bookings:
        if b.get("status", "new") != "new":
            continue
        wanted = b.get("wanted_on")
        due = wanted is None or wanted <= today or logic.waitlist_coming_today(b, today=today)
        what = " · ".join(x for x in (
            b.get("model"), b.get("tariff_name"), b.get("location_title"),
            (f"на {wanted:%d.%m}" if isinstance(wanted, date) else None)) if x)
        rows.append({
            "kind": "booking", "tag": TAGS["booking"], "who": b.get("full_name") or "—",
            "phone": b.get("phone"), "what": what,
            "since": _moment(b.get("created_at")),
            "url": booking_url(b) if booking_url else "/bookings",
            "hot": due, "state": "выдать сегодня" if due else "позже"})
    for c in claims:
        hint = c.get("amount_hint")
        rows.append({
            "kind": "claim", "tag": TAGS["claim"], "who": c.get("full_name") or "—",
            "phone": c.get("phone"),
            "what": "нажал «Я оплатил»" + (f" · {logic.money(hint)}"
                                          if money_ok and hint else ""),
            "since": _moment(c.get("created_at")), "url": "/claims",
            "hot": True, "state": "сверить с банком"})
    far = datetime.max.replace(tzinfo=UTC)
    rows.sort(key=lambda r: (not r["hot"], r["since"] or far))
    return rows


def counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    out = dict.fromkeys(KINDS, 0)
    for r in rows:
        out[r["kind"]] += 1
    out["all"] = sum(out[k] for k in KINDS)
    return out
