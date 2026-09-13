"""Импорт учётной таблицы (xlsx) в CRM: разбор ячеек, план и запись,
идемпотентность повторной загрузки, страница /import в панели.
Таблица собирается на лету через openpyxl - как у оператора: заголовки
с переносами, телефоны числом и текстом, «22к», «до 14.09», статусы
с местом в скобках.
"""

from __future__ import annotations

import asyncio
import io
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import openpyxl

    from app.crm import import_xlsx as ix
    from tests.fake_crm import FakeCrm
    HAVE_XLSX = True
except ImportError:                                    # pragma: no cover
    HAVE_XLSX = False

D = Decimal
HEADERS = ["№", "Модель", "ВИН КОЛЕСА", "ВИН РАМЫ", "ФИО", "Основной номер телефона",
           "Комплек-\nтация", "Доп номер телфона", "Ник в Tg", "Ссылка на контакт в WA",
           "когда брал", "ДЕНЬ НЕДЕЛИ", "Цена аренды по договору", "Тариф", "число дней",
           "Цена за день аренды (ср. знач)", "Сколько оплатил", "сумма долга",
           "Хронология звонков/\nпереписок", "До какого оплачена аренда?", "статус",
           "Адрес регистрации", "Адрес проживания", "Комментарий", "ТО", "GPS трекер"]


def sheet(rows: list[dict], headers: list[str] = HEADERS) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append([row.get(h) for h in headers])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def run(coro):
    return asyncio.run(coro)


ROWS = [
    {   # обычная аренда, всё заполнено
        "№": 1, "Модель": "Truck+ ", "ВИН КОЛЕСА": "240W25021881", "ВИН РАМЫ": 264022501706153,
        "ФИО": "Груздев Даниил Андреевич", "Основной номер телефона": 89600547202,
        "Комплек-\nтация": "АКБ - 2\nЗарядка - 1", "Доп номер телфона": "89172372469 Марина",
        "Ник в Tg": "@Diennt", "Ссылка на контакт в WA": "wa.me/89600547202",
        "когда брал": datetime(2026, 9, 1), "Цена аренды по договору": 4000, "Тариф": "7 дней",
        "число дней": "7", "Сколько оплатил": 4000, "сумма долга": None,
        "До какого оплачена аренда?": "до 08.09", "статус": "В аренде (долгов нет)",
        "Адрес регистрации": "Казань, Победы 72А", "GPS трекер": "Нет",
    },
    {   # аренда с долгом «22к», дата в конце года переходит на следующий
        "№": 2, "Модель": "Truck+ ", "ВИН КОЛЕСА": "240W25021882", "ВИН РАМЫ": 264022501706154,
        "ФИО": "Хасанов Тарик", "Основной номер телефона": "8 (999) 162-62-15",
        "когда брал": datetime(2025, 12, 20), "Цена аренды по договору": "3 500", "Тариф": 7,
        "Сколько оплатил": "10к", "сумма долга": "22к", "До какого оплачена аренда?": "до 10.01",
        "статус": "В аренде (есть долги)",
    },
    {   # заявление в полицию - велосипед утерян, клиент в чёрный список, долг записан
        "№": 3, "Модель": "Truck+ ", "ВИН КОЛЕСА": "240W25021883", "ВИН РАМЫ": 264022501706155,
        "ФИО": "Лобанов Владислав", "Основной номер телефона": 89274444863,
        "когда брал": datetime(2026, 1, 5), "Цена аренды по договору": 3500,
        "сумма долга": 178500, "статус": "Заявление в полицию",
    },
    {   # ремонт без арендатора, ошибка формулы в ячейке
        "№": 4, "Модель": "Truck+ ", "ВИН КОЛЕСА": "240W25021884", "ВИН РАМЫ": "ZQV202483465534\n",
        "Цена за день аренды (ср. знач)": "#VALUE!", "До какого оплачена аренда?": " ",
        "статус": "Ремонт (ГСК Строитель)", "Адрес регистрации": "замена провода 22.07",
    },
    {   # продан - покупатель не клиент
        "№": 5, "Модель": "Truck+ ", "ВИН КОЛЕСА": "240W25021885", "ВИН РАМЫ": 264022501706157,
        "ФИО": "Севастьянов Иван", "Основной номер телефона": "ПРОДАН 11.07", "статус": "Продан",
    },
    {   # повтор телефона первого клиента, второй велосипед - карточка одна, аренда одна
        "№": 6, "Модель": None, "ВИН КОЛЕСА": "240W25021886", "ВИН РАМЫ": 264022501706158,
        "ФИО": "Груздев Даниил Андреевич", "Основной номер телефона": "+7 960 054-72-02",
        "когда брал": datetime(2026, 9, 3), "Цена аренды по договору": 4000,
        "статус": "В аренде (долгов нет)",
    },
    {   # сотрудник без телефона - велосипед в аренде, клиента нет
        "№": 7, "Модель": "Truck+ ", "ВИН КОЛЕСА": "240W25021887", "ВИН РАМЫ": 264022501706159,
        "ФИО": "Ильгиз", "Основной номер телефона": "сотрудник", "статус": "В аренде (долгов нет)",
    },
    {   # ждёт сдачи - свободен на точке
        "№": 8, "Модель": "Kugoo V3 pro", "ВИН КОЛЕСА": "240W25021888",
        "ВИН РАМЫ": "JL20240715478", "статус": "Ждет сдачи (Павлюхина)",
    },
    {   # велосипед не определён, но аренда идёт
        "ФИО": "Дарбеида Алла", "Основной номер телефона": 89869092924,
        "когда брал": datetime(2026, 4, 16), "Цена аренды по договору": 2500, "Тариф": "7 дней",
        "Сколько оплатил": 20000, "До какого оплачена аренда?": "до 18.06",
        "статус": "В аренде (есть долги)",
    },
    {   # пустая строка-заметка без чего-либо опознаваемого
        "Комментарий": "поставить защиту цепи", "ТО": "да",
    },
]


@unittest.skipUnless(HAVE_XLSX, "openpyxl не установлен")
class TestCells(unittest.TestCase):
    def test_vin(self):
        self.assertEqual(ix._vin("ZQV202483465534\n"), "ZQV202483465534")
        self.assertEqual(ix._vin(264022501706153), "264022501706153")
        self.assertEqual(ix._vin("старая рама zqv2024834 новая 264022501706153"),
                         "264022501706153")
        self.assertIsNone(ix._vin("(БЕЗ РАМЫ)"))
        self.assertIsNone(ix._vin("велик у нас"))

    def test_money(self):
        self.assertEqual(ix._money_cell(4000), D("4000.00"))
        self.assertEqual(ix._money_cell("3 500"), D("3500.00"))
        self.assertEqual(ix._money_cell("22к"), D("22000.00"))
        self.assertEqual(ix._money_cell("10k"), D("10000.00"))
        self.assertEqual(ix._money_cell("2500 руб"), D("2500.00"))
        self.assertIsNone(ix._money_cell("7 дней"))
        self.assertIsNone(ix._money_cell(40001400))         # склеенные числа
        self.assertIsNone(ix._money_cell(None))

    def test_days(self):
        self.assertEqual(ix._days_cell("7 дней"), 7)
        self.assertEqual(ix._days_cell(30), 30)
        self.assertEqual(ix._days_cell("2 недели"), 14)
        self.assertEqual(ix._days_cell("1 месяц"), 30)
        self.assertEqual(ix._days_cell("неделя"), None)
        self.assertIsNone(ix._days_cell("#VALUE!"))
        self.assertIsNone(ix._days_cell(0))

    def test_date(self):
        self.assertEqual(ix._date_cell(datetime(2026, 9, 9)), date(2026, 9, 9))
        self.assertEqual(ix._date_cell("до 14.09", year_from=date(2026, 6, 1)),
                         date(2026, 9, 14))
        self.assertEqual(ix._date_cell("до 10.01", year_from=date(2025, 12, 20)),
                         date(2026, 1, 10))
        self.assertEqual(ix._date_cell("14.09.2025"), date(2025, 9, 14))
        self.assertEqual(ix._date_cell("на 28.03.26"), date(2026, 3, 28))
        self.assertIsNone(ix._date_cell("до 23.00"))
        self.assertIsNone(ix._date_cell("долг"))

    def test_phones(self):
        self.assertEqual(ix._phones(89600547202), ("+79600547202", []))
        self.assertEqual(ix._phones("8 (999) 162-62-15"), ("+79991626215", []))
        main, extra = ix._phones("89868236477\n89036175938 (основной)")
        self.assertEqual(main, "+79036175938")
        self.assertEqual(extra, ["+79868236477"])
        self.assertEqual(ix._phones("сотрудник"), (None, []))
        self.assertEqual(ix._phones("8950418525"), (None, []))   # не хватает цифры

    def test_bike_status(self):
        self.assertEqual(ix.bike_status("В аренде (долгов нет)"), ("rented", "долгов нет"))
        self.assertEqual(ix.bike_status("Ремонт (ГСК Строитель)"), ("repair", "ГСК Строитель"))
        self.assertEqual(ix.bike_status("Продан"), ("sold", ""))
        self.assertEqual(ix.bike_status("Заявление в полицию")[0], "lost")
        self.assertEqual(ix.bike_status("НЕ МОЖЕМ НАЙТИ ВЕЛИК")[0], "lost")
        self.assertEqual(ix.bike_status("Ждет сдачи (Павлюхина)"), ("available", "Павлюхина"))
        self.assertEqual(ix.bike_status("Аметьево"), ("available", "Аметьево"))
        self.assertEqual(ix.bike_status(""), ("available", ""))


@unittest.skipUnless(HAVE_XLSX, "openpyxl не установлен")
class TestRead(unittest.TestCase):
    def test_rows(self):
        rows = ix.read_rows(sheet(ROWS))
        self.assertEqual(len(rows), 9)                  # строка-заметка отброшена
        r = rows[0]
        self.assertEqual((r.line, r.no, r.model), (2, "1", "Truck+"))
        self.assertEqual((r.motor, r.frame), ("240W25021881", "264022501706153"))
        self.assertEqual((r.phone, r.extra_phones), ("+79600547202", ["+79172372469"]))
        self.assertEqual((r.started, r.paid_until), (date(2026, 9, 1), date(2026, 9, 8)))
        self.assertEqual((r.price, r.days, r.paid, r.debt), (D(4000), 7, D(4000), None))
        self.assertEqual(r.kit, "АКБ - 2\nЗарядка - 1")
        tarik = rows[1]
        self.assertEqual((tarik.price, tarik.paid, tarik.debt), (D(3500), D(10000), D(22000)))
        self.assertEqual(tarik.paid_until, date(2026, 1, 10))
        self.assertEqual(tarik.days, 7)                 # из колонки «Тариф»
        repair = rows[3]
        self.assertIsNone(repair.raw.get("paid_until"))  # пробел = пусто
        self.assertEqual(repair.frame, "ZQV202483465534")

    def test_header_not_first_line(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["ДЕЙСТВУЮЩИЕ АРЕНДАТОРЫ"])
        ws.append([])
        ws.append(["№", "ФИО", "Телефон", "Статус"])
        ws.append([1, "Иванов", 89001112233, "В аренде"])
        buf = io.BytesIO()
        wb.save(buf)
        rows = ix.read_rows(buf.getvalue())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].phone, "+79001112233")

    def test_bad_file(self):
        with self.assertRaises(ix.ImportError_):
            ix.read_rows(b"not a workbook")
        with self.assertRaises(ix.ImportError_):
            ix.read_rows(sheet([{"a": 1}], headers=["a", "b"]))


@unittest.skipUnless(HAVE_XLSX, "openpyxl не установлен")
class TestPlanApply(unittest.TestCase):
    def setUp(self):
        self.crm = FakeCrm()
        self.data = sheet(ROWS)

    def test_plan(self):
        plan, done = run(ix.run(self.crm, self.data, apply=False))
        self.assertIsNone(done)
        self.assertEqual(plan.rows, 9)
        self.assertEqual(len(plan.bikes), 8)
        self.assertEqual({b["code"] for b in plan.bikes}, {str(i) for i in range(1, 9)})
        self.assertEqual({c["full_name"] for c in plan.clients},
                         {"Груздев Даниил Андреевич", "Хасанов Тарик", "Лобанов Владислав",
                          "Дарбеида Алла"})
        self.assertEqual(len(plan.rentals), 3)
        self.assertEqual([d["amount"] for d in plan.debts], [D(178500)])
        skipped = "\n".join(plan.skipped_clients)
        self.assertIn("Севастьянов Иван - велосипед продан", skipped)
        self.assertIn("Ильгиз - телефон не разобран (сотрудник)", skipped)
        warnings = "\n".join(plan.warnings)
        self.assertIn("уже встречался выше", warnings)
        self.assertIn("модель не указана у 1 велосипедов (строки 7)", warnings)
        self.assertIn("Дарбеида Алла: велосипед не определён", warnings)
        # ничего не записано
        self.assertEqual(run(self.crm.clients()), [])
        self.assertEqual(run(self.crm.bike_counts()), {})

    def test_apply(self):
        plan, done = run(ix.run(self.crm, self.data, apply=True, by="import:test"))
        self.assertEqual(done, {"bikes": 8, "clients": 4, "rentals": 3, "ledger": 7})
        crm = self.crm
        self.assertEqual(run(crm.bike_counts()),
                         {"rented": 3, "lost": 1, "repair": 1, "sold": 1, "available": 2})
        # первый клиент: аренда с начислением 4000 и платежом 4000, оплачено до 08.09
        c = run(crm.client_by_phone("+79600547202"))
        self.assertEqual(c["source"], "import")
        self.assertEqual(c["username"], "Diennt")
        self.assertIn("Доп. телефоны: +79172372469", c["note"])
        self.assertIn("Адрес регистрации: Казань", c["note"])
        r = run(crm.active_rental_of(c["id"]))
        self.assertEqual((r["billing"], r["period_days"], r["price"]),
                         ("manual", 7, D("4000.00")))
        self.assertEqual((r["started_on"], r["billed_until"]), (date(2026, 9, 1), date(2026, 9, 8)))
        self.assertEqual(run(crm.client_balance(c["id"])), D(0))
        bike = run(crm.bike(r["bike_id"]))
        self.assertEqual((bike["code"], bike["status"], bike["frame_no"]),
                         ("1", "rented", "264022501706153"))
        # Тарик: начислено 32 000 (оплачено 10к + долг 22к), заплатил 10к -> баланс -22 000
        t = run(crm.client_by_phone("+79991626215"))
        self.assertEqual(run(crm.client_balance(t["id"])), D(-22000))
        rt = run(crm.active_rental_of(t["id"]))
        self.assertEqual(rt["billed_until"], date(2026, 1, 10))
        # полиция: чёрный список, долг в журнале корректировкой, велосипед утерян
        lost = run(crm.client_by_phone("+79274444863"))
        self.assertEqual(lost["status"], "blacklist")
        self.assertEqual(run(crm.client_balance(lost["id"])), D(-178500))
        self.assertIsNone(run(crm.active_rental_of(lost["id"])))
        entries = run(crm.ledger_of(lost["id"]))
        self.assertEqual((entries[0]["kind"], entries[0]["note"]),
                         ("adjust", "Долг по таблице (Заявление в полицию)"))
        self.assertEqual(run(crm.bike_by_motor("240W25021883"))["status"], "lost")
        # сотрудник: велосипед в аренде без карточки клиента
        staff_bike = run(crm.bike_by_motor("240W25021887"))
        self.assertEqual(staff_bike["status"], "rented")
        self.assertIn("Арендатор по таблице: Ильгиз", staff_bike["note"])
        # продан: покупатель в заметке велосипеда
        sold = run(crm.bike_by_motor("240W25021885"))
        self.assertEqual(sold["status"], "sold")
        self.assertIn("Покупатель по таблице: Севастьянов Иван", sold["note"])
        # аренда без велосипеда
        alla = run(crm.client_by_phone("+79869092924"))
        ra = run(crm.active_rental_of(alla["id"]))
        self.assertIsNone(ra["bike_id"])
        self.assertEqual(run(crm.client_balance(alla["id"])), D(0))   # начислено = оплачено
        # ждёт сдачи: свободен, место в заметке
        waiting = run(crm.bike_by_frame("JL20240715478"))
        self.assertEqual((waiting["status"], waiting["model"]), ("available", "Kugoo V3 pro"))
        self.assertIn("Место: Павлюхина", waiting["note"])
        # автор записей; деньги из таблицы датированы днём выдачи, а не загрузки
        self.assertEqual(entries[0]["created_by"], "import:test")
        self.assertEqual(entries[0]["created_at"].date(), date(2026, 1, 5))
        for x in run(crm.ledger_of(c["id"])):
            self.assertEqual(x["created_at"].date(), date(2026, 9, 1))

    def test_second_run_changes_nothing(self):
        run(ix.run(self.crm, self.data, apply=True))
        before = (run(self.crm.bike_counts()), len(run(self.crm.clients())),
                  len(run(self.crm.active_rentals())))
        plan, done = run(ix.run(self.crm, self.data, apply=True))
        self.assertEqual(done, {"bikes": 0, "clients": 0, "rentals": 0, "ledger": 0})
        self.assertEqual(len(plan.skipped_bikes), 9)
        self.assertEqual(len(plan.skipped_rentals), 4)
        self.assertEqual(plan.debts, [])
        after = (run(self.crm.bike_counts()), len(run(self.crm.clients())),
                 len(run(self.crm.active_rentals())))
        self.assertEqual(before, after)
        # клиент с идущей арендой в CRM не получает вторую
        self.assertIn("уже есть идущая аренда", "\n".join(plan.skipped_rentals))

    def test_existing_bike_and_client(self):
        """Велосипед и клиент заведены руками до импорта - используются они."""
        bike_id = run(self.crm.create_bike(code="B-7", model="Truck+", frame_no="264022501706153"))
        cid = run(self.crm.create_client(full_name="Груздев Д.", phone="+79600547202"))
        plan, done = run(ix.run(self.crm, self.data, apply=True))
        self.assertEqual(done["bikes"], 7)
        self.assertEqual(done["clients"], 3)
        self.assertIn("велосипед уже есть (B-7)", "\n".join(plan.skipped_bikes))
        self.assertIn("уже есть (Груздев Д.)", "\n".join(plan.skipped_clients))
        r = run(self.crm.active_rental_of(cid))
        self.assertEqual(r["bike_id"], bike_id)
        self.assertEqual(run(self.crm.bike(bike_id))["status"], "rented")

    def test_reused_number_with_new_vin_is_a_new_bike(self):
        """Перенумерованная таблица: № занят другим велосипедом, VIN новый -
        аренда не должна прицепиться к чужому велосипеду."""
        run(self.crm.create_bike(code="1", model="Truck+", frame_no="999999999999999"))
        plan, done = run(ix.run(self.crm, self.data, apply=True))
        self.assertEqual(done["bikes"], 8)
        new = run(self.crm.bike_by_frame("264022501706153"))
        self.assertEqual(new["code"], "1-2")
        self.assertEqual(run(self.crm.bike_by_code("1"))["status"], "available")

    def test_rented_row_without_renter_marks_bike_rented(self):
        rows = [{"№": 9, "Модель": "Truck+ ", "ВИН КОЛЕСА": "240W25021899",
                 "ВИН РАМЫ": 264022501706199, "статус": "В аренде (долгов нет)"}]
        run(ix.run(self.crm, sheet(rows), apply=True))
        self.assertEqual(run(self.crm.bike_by_motor("240W25021899"))["status"], "rented")

    def test_report(self):
        plan, done = run(ix.run(self.crm, self.data, apply=True))
        text = ix.report_text(plan, done)
        self.assertIn("Строк с данными: 9", text)
        self.assertIn("Долги без аренды (полиция, невозврат): 1 на 178 500 ₽", text)
        self.assertIn("Записано: велосипедов 8, клиентов 4, аренд 3, записей журнала 7", text)
        self.assertIn("Пропущенные клиенты (2):", text)


try:
    from tests.test_web import HAVE_WEB, WebCase
except ImportError:                                    # pragma: no cover
    HAVE_WEB, WebCase = False, unittest.TestCase


@unittest.skipUnless(HAVE_XLSX and HAVE_WEB, "openpyxl/fastapi не установлены")
class TestImportPage(WebCase):
    def upload(self, data: bytes, apply: bool = False, name: str = "t.xlsx"):
        form = {"apply": "1"} if apply else {}
        return self.client.post("/import", data=form, files={"file": (name, data)})

    def test_admin_only(self):
        from app.crm import logic
        run(self.crm.create_staff("ivan", logic.hash_password("password-1"), "Иван", "manager"))
        self.assertEqual(self.login("ivan", "password-1").status_code, 303)
        self.assertEqual(self.client.get("/import").status_code, 403)
        self.assertEqual(self.upload(sheet(ROWS)).status_code, 403)
        self.assertNotIn('href="/import"', self.client.get("/clients").text)

    def test_dry_run_then_apply(self):
        self.login()
        page = self.get_ok("/import")
        self.assertIn('enctype="multipart/form-data"', page)
        self.assertIn('href="/import"', page)
        r = self.upload(sheet(ROWS))
        self.assertEqual(r.status_code, 200)
        self.assertIn("Это сухой прогон", r.text)
        self.assertIn("Велосипеды: добавить 8", r.text)
        self.assertEqual(run(self.crm.bike_counts()), {})
        r = self.upload(sheet(ROWS), apply=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Записано: велосипедов 8, клиентов 4", r.text)
        self.assertEqual(len(run(self.crm.clients())), 4)
        # карточка импортированного клиента открывается и помечена источником
        c = run(self.crm.client_by_phone("+79600547202"))
        self.assertIn("импорт из таблицы", self.get_ok(f"/clients/{c['id']}"))

    def test_bad_upload(self):
        self.login()
        r = self.upload(b"garbage", name="t.xlsx")
        self.assertEqual(r.status_code, 303)
        self.assertIn("не читается", self.client.get("/import").text)
        r = self.upload(sheet(ROWS), name="table.csv")
        self.assertEqual(r.status_code, 303)
        self.assertIn("формате .xlsx", self.client.get("/import").text)
        r = self.client.post("/import", data={})
        self.assertEqual(r.status_code, 303)
        self.assertIn("Выберите файл", self.client.get("/import").text)


if __name__ == "__main__":
    unittest.main()
