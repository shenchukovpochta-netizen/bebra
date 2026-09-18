"""Простая электронная подпись: соглашение, код, журнал.

Кнопка «подписываю» фиксирует согласие, но не доказывает его. Здесь
проверяется то, чем доказывают: что подписали (хэши пакета), когда,
каким кодом и с какого адреса. Код живёт минуты, попыток немного,
в базе только его хэш.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import esign, logic  # noqa: E402

try:
    import test_web as tw

    from app.crm import service
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
COMPANY = {"company_name": "ИП Гарипов Ильшат Рустемович",
           "company_inn": "166012345678", "company_ogrn": "321169000012345",
           "company_address": "420080, Казань, ул. Павлюхина, 91",
           "company_phone": "+7 917 000-11-22",
           "company_email": "prokat@example.ru"}


def request(**over) -> dict:
    row = {"id": 1, "no": "ПЭП-000001", "token": "a" * 32, "status": "code",
           "attempts": 0, "code_at": NOW - timedelta(minutes=2),
           "expires_at": NOW + timedelta(days=6),
           "docs": [{"kind": "esign", "title": "Соглашение", "sha256": "aa"},
                    {"kind": "contract", "title": "Договор", "sha256": "bb"}]}
    row.update(over)
    return row


class TestSignLogic(unittest.TestCase):
    def test_code_and_token_are_random_and_hashed_with_the_token(self):
        token, other = logic.make_sign_token(), logic.make_sign_token()
        self.assertEqual(len(token), 32)
        self.assertNotEqual(token, other)
        code = logic.make_sign_code()
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())
        # Один и тот же код в двух заявках даёт разные хэши.
        self.assertNotEqual(logic.hash_sign_code(token, code),
                            logic.hash_sign_code(other, code))
        self.assertEqual(logic.hash_sign_code(token, code),
                         logic.hash_sign_code(token, code))
        self.assertNotIn(code, logic.hash_sign_code(token, code))

    def test_code_from_the_form_is_digits_only(self):
        self.assertEqual(logic.clean_sign_code(" 29-39 33 "), "293933")
        self.assertEqual(logic.clean_sign_code("абв"), "")
        self.assertEqual(logic.clean_sign_code("1234567890"), "123456")

    def test_state_counts_minutes_attempts_and_expiry(self):
        state = logic.sign_state(request(), now=NOW)
        self.assertTrue(state["open"])
        self.assertTrue(state["code_valid"])
        self.assertEqual(state["code_left"], 8)
        self.assertEqual(state["attempts_left"], logic.SIGN_MAX_ATTEMPTS)

        old = logic.sign_state(request(code_at=NOW - timedelta(minutes=11)), now=NOW)
        self.assertFalse(old["code_valid"], "код живёт десять минут")
        self.assertTrue(old["open"], "ссылка ещё жива - можно взять новый код")

        burnt = logic.sign_state(request(attempts=logic.SIGN_MAX_ATTEMPTS), now=NOW)
        self.assertFalse(burnt["code_valid"])
        self.assertEqual(burnt["attempts_left"], 0)

        gone = logic.sign_state(request(expires_at=NOW - timedelta(minutes=1)),
                                now=NOW)
        self.assertTrue(gone["expired"])
        self.assertFalse(gone["open"])

        done = logic.sign_state(request(status="signed"), now=NOW)
        self.assertTrue(done["signed"])
        self.assertFalse(done["open"])

    def test_package_digest_changes_with_any_document(self):
        one = logic.sign_docs_digest(request()["docs"])
        same = logic.sign_docs_digest(request()["docs"])
        self.assertEqual(one, same)
        changed = logic.sign_docs_digest(
            [{"sha256": "aa"}, {"sha256": "bb-подменили"}])
        self.assertNotEqual(one, changed)
        swapped = logic.sign_docs_digest([{"sha256": "bb"}, {"sha256": "aa"}])
        self.assertNotEqual(one, swapped, "порядок документов тоже в хэше")

    def test_rows_put_open_requests_first(self):
        rows = logic.sign_rows([
            {"id": 1, "status": "signed", "created_at": NOW - timedelta(days=2),
             "expires_at": NOW + timedelta(days=5), "docs": []},
            {"id": 2, "status": "new", "created_at": NOW - timedelta(days=3),
             "expires_at": NOW + timedelta(days=4), "docs": [{}]},
            {"id": 3, "status": "signed", "created_at": NOW,
             "expires_at": NOW, "docs": []}], now=NOW)
        self.assertEqual([r["id"] for r in rows], [2, 3, 1])
        self.assertEqual(rows[0]["docs_count"], 1)
        summary = logic.sign_summary(rows)
        self.assertEqual((summary["open"], summary["signed"]), (1, 2))


class TestAgreement(unittest.TestCase):
    def test_agreement_carries_the_requisites_and_the_documents(self):
        text = esign.build_agreement(
            COMPANY, {"full_name": "Иванов Иван", "phone": "+79990000000"},
            no="ПЭП-000007",
            docs=[{"kind": "contract", "title": "Договор аренды № АВ-1"}],
            today=NOW.date())
        self.assertIn("ПЭП-000007", text)
        self.assertIn("166012345678", text)
        self.assertIn("Иванов Иван", text)
        self.assertIn("Договор аренды № АВ-1", text)
        self.assertIn("17.09.2026", text)
        self.assertIn("63-ФЗ", text)
        self.assertIn("prokat@example.ru", text)

    def test_empty_requisites_do_not_break_the_text(self):
        text = esign.build_agreement({}, {}, no="ПЭП-000001", docs=[],
                                     today=NOW.date())
        self.assertIn("Оператор", text)
        self.assertNotIn("{", text, "все подстановки закрыты")

    def test_hash_is_stable_and_sensitive(self):
        one = esign.sha256_text("текст")
        self.assertEqual(one, esign.sha256_text("текст"))
        self.assertNotEqual(one, esign.sha256_text("текст "))
        self.assertEqual(len(one), 64)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestSigningFlow(tw.WebCase):
    """Полный путь: оператор собирает ссылку, клиент подписывает кодом."""

    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        for code, value in COMPANY.items():
            tw.run(self.crm.set_setting(code, value, by="t"))
        self.db.users[5001] = {
            "tg_id": 5001, "phone": "+79990000000", "full_name": "Иванов Иван",
            "contract_no": "АВ-2026-000042", "contract_status": "signed",
            "contract_sha256": "c" * 64, "contract_path": "/tmp/contract.pdf",
            "soglasie_sha256": "d" * 64, "soglasie_path": "/tmp/soglasie.pdf"}

    def start(self):
        r = self.client.post(f"/clients/{self.client_id}/sign")
        self.assertEqual(r.status_code, 303)
        request_id = int(r.headers["location"].rsplit("/", 1)[1])
        return request_id, tw.run(self.crm.sign_request(request_id))

    def test_package_includes_agreement_and_bot_documents(self):
        request_id, row = self.start()
        kinds = [d["kind"] for d in row["docs"]]
        self.assertEqual(kinds, ["esign", "contract", "consent"])
        self.assertEqual(row["docs"][1]["sha256"], "c" * 64)
        self.assertIn(row["no"], row["agreement"])
        self.assertIn("Иванов Иван", row["agreement"])
        # Хэш соглашения снят с того текста, который лежит в заявке.
        self.assertEqual(row["docs"][0]["sha256"],
                         esign.sha256_text(row["agreement"]))
        card = self.get_ok(f"/signings/{request_id}")
        self.assertIn("Договор аренды", card)
        self.assertIn("Заявка создана", card)

    def test_operator_opens_a_document_from_the_card(self):
        """Ссылка «открыть» на карточке и на пятом шаге мастера ведёт
        на файл пакета - раньше маршрута не было, и ссылка была битой."""
        import tempfile
        with tempfile.NamedTemporaryFile("wb", suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 contract")
        self.db.users[5001]["contract_path"] = f.name
        request_id, row = self.start()
        card = self.get_ok(f"/signings/{request_id}")
        self.assertIn(f"/signings/{request_id}/doc/1", card)
        r = self.client.get(f"/signings/{request_id}/doc/1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"%PDF-1.4 contract")
        # Соглашение об ЭП живёт текстом в заявке - файла у него нет.
        self.assertEqual(self.client.get(f"/signings/{request_id}/doc/0").status_code, 404)
        self.assertEqual(self.client.get(f"/signings/{request_id}/doc/9").status_code, 404)
        self.assertEqual(self.client.get("/signings/999/doc/1").status_code, 404)

    def test_missing_file_is_a_404_not_a_crash(self):
        self.db.users[5001]["contract_path"] = "/tmp/no-such-contract.pdf"
        request_id, _ = self.start()
        self.assertEqual(self.client.get(f"/signings/{request_id}/doc/1").status_code, 404)

    def test_client_page_signs_with_the_code(self):
        request_id, row = self.start()
        page = self.client.get(f"/sign/{row['token']}").text
        self.assertIn("Подписание документов", page)
        self.assertIn("Соглашение об использовании ПЭП", page)
        # Открытие страницы попадает в журнал.
        kinds = [e["kind"] for e in tw.run(self.crm.sign_events(request_id))]
        self.assertIn("opened", kinds)

        self.client.post(f"/sign/{row['token']}/code")
        self.assertEqual(len(self.bot.sent), 1, "код ушёл клиенту в Telegram")
        code = "".join(ch for ch in self.bot.sent[0][1] if ch.isdigit())[:6]
        stored = tw.run(self.crm.sign_request(request_id))
        self.assertIsNotNone(stored["code_hash"])
        self.assertNotIn(code, str(stored["code_hash"]),
                         "в базе только хэш, не сам код")

        r = self.client.post(f"/sign/{row['token']}", data={"code": code})
        self.assertEqual(r.status_code, 303)
        signed = tw.run(self.crm.sign_request(request_id))
        self.assertEqual(signed["status"], "signed")
        self.assertIsNotNone(signed["signed_at"])
        self.assertIsNone(signed["code_hash"], "использованный код стёрт")
        events = [e["kind"] for e in tw.run(self.crm.sign_events(request_id))]
        self.assertEqual(events[-1], "signed")
        note = tw.run(self.crm.sign_events(request_id))[-1]["note"]
        self.assertIn(logic.sign_docs_digest(signed["docs"]), note)

    def test_wrong_code_burns_an_attempt_and_lands_in_the_journal(self):
        request_id, row = self.start()
        self.client.post(f"/sign/{row['token']}/code")
        r = self.client.post(f"/sign/{row['token']}", data={"code": "000000"})
        self.assertEqual(r.status_code, 303)
        page = self.client.get(f"/sign/{row['token']}").text
        self.assertIn("Неверный код", page)
        self.assertEqual(tw.run(self.crm.sign_request(request_id))["attempts"], 1)
        self.assertIn("code_wrong",
                      [e["kind"] for e in tw.run(self.crm.sign_events(request_id))])
        # Короткий код даже не считается попыткой - это опечатка.
        self.client.post(f"/sign/{row['token']}", data={"code": "12"})
        self.assertIn("шесть цифр", self.client.get(f"/sign/{row['token']}").text)
        self.assertEqual(tw.run(self.crm.sign_request(request_id))["attempts"], 1)

    def test_code_runs_out_of_attempts(self):
        request_id, row = self.start()
        self.client.post(f"/sign/{row['token']}/code")
        for _ in range(logic.SIGN_MAX_ATTEMPTS):
            self.client.post(f"/sign/{row['token']}", data={"code": "000000"})
        self.assertEqual(tw.run(self.crm.sign_request(request_id))["attempts"],
                         logic.SIGN_MAX_ATTEMPTS)
        # Попытки кончились: следующий ввод даже не проверяется.
        self.client.post(f"/sign/{row['token']}", data={"code": "000000"})
        page = self.client.get(f"/sign/{row['token']}").text
        self.assertIn("больше не действует", page)
        self.assertEqual(tw.run(self.crm.sign_request(request_id))["status"], "code")
        # Новый код обнуляет счётчик: старые промахи к нему не относятся.
        self.client.post(f"/sign/{row['token']}/code")
        self.assertEqual(tw.run(self.crm.sign_request(request_id))["attempts"], 0)

    def test_signing_without_a_code_is_refused(self):
        _, row = self.start()
        self.client.post(f"/sign/{row['token']}", data={"code": "123456"})
        self.assertIn("Сначала получите код",
                      self.client.get(f"/sign/{row['token']}").text)

    def test_cancelled_link_stops_working_and_signed_one_is_not_cancelled(self):
        request_id, row = self.start()
        self.client.post(f"/signings/{request_id}/cancel")
        self.assertEqual(tw.run(self.crm.sign_request(request_id))["status"],
                         "cancelled")
        page = self.client.get(f"/sign/{row['token']}").text
        self.assertIn("отменена оператором", page)
        self.client.post(f"/sign/{row['token']}/code")
        self.assertIn("недействительна", self.client.get(f"/sign/{row['token']}").text)

        second_id, second = self.start()
        self.client.post(f"/sign/{second['token']}/code")
        code = "".join(ch for ch in self.bot.sent[-1][1] if ch.isdigit())[:6]
        self.client.post(f"/sign/{second['token']}", data={"code": code})
        self.client.post(f"/signings/{second_id}/cancel")
        self.assertIn("не отменяется", self.get_ok(f"/signings/{second_id}"))
        self.assertEqual(tw.run(self.crm.sign_request(second_id))["status"],
                         "signed")

    def test_second_signature_is_refused(self):
        request_id, row = self.start()
        self.client.post(f"/sign/{row['token']}/code")
        code = "".join(ch for ch in self.bot.sent[-1][1] if ch.isdigit())[:6]
        self.client.post(f"/sign/{row['token']}", data={"code": code})
        self.client.post(f"/sign/{row['token']}", data={"code": code})
        self.assertIn("уже подписаны", self.client.get(f"/sign/{row['token']}").text)
        signed = [e for e in tw.run(self.crm.sign_events(request_id))
                  if e["kind"] == "signed"]
        self.assertEqual(len(signed), 1)

    def test_operator_can_dictate_the_code_when_there_is_no_telegram(self):
        tw.run(self.crm.update_client(self.client_id, tg_id=None))
        request_id, _ = self.start()
        self.client.post(f"/signings/{request_id}/code")
        page = self.get_ok(f"/signings/{request_id}")
        self.assertIn("продиктуйте код", page)
        self.assertEqual(self.bot.sent, [], "в бот писать некому")

    def test_client_page_needs_no_login_and_unknown_token_is_404(self):
        _, row = self.start()
        self.client.post("/logout")
        r = self.client.get(f"/sign/{row['token']}")
        self.assertEqual(r.status_code, 200, "клиент в панель не входит")
        self.assertEqual(self.client.get("/sign/" + "z" * 32).status_code, 404)
        self.assertEqual(self.client.get("/signings").status_code, 303,
                         "а журнал подписаний - только для своих")

    def test_blacklisted_client_gets_no_link(self):
        tw.run(self.crm.update_client(self.client_id, status="blacklist"))
        r = self.client.post(f"/clients/{self.client_id}/sign")
        self.assertEqual(r.headers["location"], f"/clients/{self.client_id}")
        self.assertIn("чёрном списке", self.get_ok(f"/clients/{self.client_id}"))
        self.assertEqual(tw.run(self.crm.sign_requests()), [])

    def test_journal_page_lists_requests(self):
        self.start()
        page = self.get_ok("/signings")
        self.assertIn("ПЭП-000001", page)
        self.assertIn("ждут клиента", page)

    def test_agreement_is_served_as_signed(self):
        _, row = self.start()
        page = self.client.get(f"/sign/{row['token']}/agreement")
        self.assertEqual(page.status_code, 200)
        self.assertIn("СОГЛАШЕНИЕ ОБ ИСПОЛЬЗОВАНИИ", page.text)
        stored = tw.run(self.crm.sign_request_by_token(row["token"]))
        self.assertIn("ИНН 166012345678", stored["agreement"])


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestSignExpiry(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.seed()

    def test_expired_link_cannot_be_signed(self):
        client = tw.run(self.crm.client(self.client_id))
        created = tw.run(service.start_signing(
            self.crm, client=client, rental=None, company=COMPANY,
            bot_user=None, by="t",
            now=datetime.now(UTC) - timedelta(days=logic.SIGN_LINK_DAYS + 1)))
        row = tw.run(self.crm.sign_request(created["id"]))
        with self.assertRaises(service.ServiceError):
            tw.run(service.issue_sign_code(self.crm, row))
        page = self.client.get(f"/sign/{row['token']}").text
        self.assertIn("Срок ссылки истёк", page)


if __name__ == "__main__":
    unittest.main()
