"""Свои шаблоны документов, подпись и печать.

Правило, которое здесь стерегут: у каждого вида документа всегда включён
ровно один шаблон. Выключить оба нельзя — выдачу тогда нечем оформить, и
это не настройка, а поломка. Сломанный свой шаблон тоже не должен
оставлять клиента без договора: система молча возвращается к нашему.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import doctemplates, logic  # noqa: E402
from app.services import contract  # noqa: E402

try:
    import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

OURS = Path(__file__).resolve().parent.parent / "app" / "contract_template.docx"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def a_docx(text: str = "Договор {{ fio }} {{ signature }}") -> bytes:
    """Минимальный docx: zip с word/document.xml. Настоящий Word такой
    откроет, а нам хватает для проверки правил."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types></Types>")
        zf.writestr("word/_rels/document.xml.rels",
                    "<Relationships></Relationships>")
        zf.writestr("word/document.xml",
                    f"<w:document><w:body><w:p><w:r><w:t>{text}"
                    f"</w:t></w:r></w:p></w:body></w:document>")
    return out.getvalue()


class TestDocLogic(unittest.TestCase):
    def test_every_kind_defaults_to_ours(self):
        rows = {r["kind"]: r for r in logic.doc_rows([])}
        self.assertEqual(set(rows), set(logic.DOC_TEMPLATES))
        self.assertTrue(all(not r["mine"] for r in rows.values()))
        self.assertEqual(rows["contract"]["source"], "наш шаблон")

    def test_esign_is_code_only(self):
        rows = {r["kind"]: r for r in logic.doc_rows([])}
        self.assertTrue(rows["esign"]["code_only"],
                        "текст соглашения хранится в заявке, файлом его "
                        "подменить нельзя")
        self.assertEqual(logic.doc_summary([])["total"],
                         len(logic.DOC_TEMPLATES) - len(logic.DOC_CODE_ONLY))

    def test_active_wins_and_the_rest_is_archive(self):
        rows = {r["kind"]: r for r in logic.doc_rows([
            {"kind": "contract", "id": 2, "active": True, "filename": "b.docx"},
            {"kind": "contract", "id": 1, "active": False, "filename": "a.docx"}])}
        self.assertTrue(rows["contract"]["mine"])
        self.assertEqual(rows["contract"]["active"]["id"], 2)
        self.assertEqual([a["id"] for a in rows["contract"]["archive"]], [1])

    def test_filename_is_ours_not_the_browsers(self):
        self.assertEqual(logic.doc_filename("contract", 7), "contract-0007.docx")
        self.assertEqual(logic.mark_filename("stamp"), "stamp.png")


class TestUploadChecks(unittest.TestCase):
    def test_only_docx(self):
        with self.assertRaises(contract.TemplateProblem):
            doctemplates.check_upload(a_docx(), "договор.pdf")

    def test_not_a_zip_is_refused(self):
        with self.assertRaises(contract.TemplateProblem) as got:
            doctemplates.check_upload("это не docx".encode(), "договор.docx")
        self.assertIn("не docx", str(got.exception))

    def test_template_without_placeholders_is_refused(self):
        with self.assertRaises(contract.TemplateProblem) as got:
            doctemplates.check_upload(a_docx("просто текст"), "договор.docx")
        self.assertIn("подстановки", str(got.exception))

    def test_too_big_is_refused(self):
        big = a_docx("{{ fio }}" + "я" * logic.DOC_MAX_BYTES)
        if len(big) <= logic.DOC_MAX_BYTES:
            self.skipTest("сжатие уложилось в лимит")
        with self.assertRaises(contract.TemplateProblem):
            doctemplates.check_upload(big, "договор.docx")

    def test_good_template_passes(self):
        doctemplates.check_upload(a_docx(), "договор.docx")


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        doctemplates.reset()
        self.addCleanup(doctemplates.reset)
        self.dir = Path(tempfile.mkdtemp())

    def test_without_snapshot_ours_is_used(self):
        self.assertEqual(doctemplates.path_for("contract", OURS, None), OURS)

    def test_code_only_kind_never_takes_a_file(self):
        doctemplates.set_snapshot([{"kind": "esign", "active": True,
                                    "filename": "esign-0001.docx"}])
        self.assertEqual(doctemplates.path_for("esign", OURS, self.dir), OURS)

    def test_uploaded_template_wins(self):
        (self.dir / "contract-0001.docx").write_bytes(a_docx())
        doctemplates.set_snapshot([{"kind": "contract", "active": True,
                                    "filename": "contract-0001.docx"}])
        got = doctemplates.path_for("contract", OURS, self.dir)
        self.assertEqual(got.name, "contract-0001.docx")

    def test_broken_template_falls_back_to_ours(self):
        (self.dir / "contract-0002.docx").write_bytes("мусор".encode())
        doctemplates.set_snapshot([{"kind": "contract", "active": True,
                                    "filename": "contract-0002.docx"}])
        self.assertEqual(doctemplates.path_for("contract", OURS, self.dir), OURS,
                         "сломанный шаблон не должен оставить клиента "
                         "без договора")

    def test_missing_file_falls_back_to_ours(self):
        doctemplates.set_snapshot([{"kind": "contract", "active": True,
                                    "filename": "нет-такого.docx"}])
        self.assertEqual(doctemplates.path_for("contract", OURS, self.dir), OURS)


class TestMarksInDocx(unittest.TestCase):
    def ctx(self):
        return {k: "x" for k in contract.placeholders(OURS.read_bytes())}

    def test_picture_lands_in_the_package(self):
        data, _ = contract.build(OURS, self.ctx(), {"signature": PNG,
                                                    "stamp": PNG})
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertIn("word/media/signature.png", zf.namelist())
            self.assertIn('Extension="png"',
                          zf.read("[Content_Types].xml").decode())
            self.assertIn("rIdMarksignature",
                          zf.read(contract.RELS_XML).decode())
            self.assertIsNone(zf.testzip(), "docx остался читаемым zip")

    def test_digest_does_not_depend_on_the_picture(self):
        _, with_mark = contract.build(OURS, self.ctx(), {"stamp": PNG})
        _, without = contract.build(OURS, self.ctx())
        self.assertEqual(with_mark, without,
                         "иначе подписанный договор нечем было бы проверить")

    def test_missing_mark_removes_the_placeholder(self):
        data, _ = contract.build(OURS, self.ctx())
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read(contract.DOCUMENT_XML).decode()
        self.assertNotIn("signature }}", xml)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestDocPages(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.folder = Path(tempfile.mkdtemp())
        object.__setattr__(self.cfg, "doc_dir", self.folder)

    def upload(self, kind="contract", data=None, name="договор.docx"):
        return self.client.post(
            f"/documents/{kind}",
            files={"template": (name, data if data is not None else a_docx(),
                                "application/vnd.openxmlformats-officedocument"
                                ".wordprocessingml.document")},
            data={"action": "upload"})

    def test_page_lists_kinds_and_marks_ours(self):
        text = self.get_ok("/documents")
        self.assertIn("Договор аренды", text)
        self.assertIn("Соглашение об ЭП", text)
        self.assertIn("собирается кодом", text)

    def test_upload_switches_to_mine(self):
        r = self.upload()
        self.assertEqual(r.status_code, 303)
        rows = _run(self.crm.doc_templates("contract"))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["active"])
        self.assertEqual(rows[0]["original"], "договор.docx")
        self.assertTrue((self.folder / rows[0]["filename"]).is_file())
        self.assertIn("ваш", self.get_ok("/documents"))

    def test_bad_file_is_refused_and_nothing_is_saved(self):
        self.upload(data="мусор".encode())
        self.assertEqual(_run(self.crm.doc_templates("contract")), [])
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_returning_to_ours_keeps_the_archive(self):
        self.upload()
        self.client.post("/documents/contract", data={"action": "ours"})
        self.assertIsNone(_run(self.crm.active_doc_template("contract")))
        self.assertEqual(len(_run(self.crm.doc_templates("contract"))), 1,
                         "прошлая редакция остаётся в архиве")

    def test_second_upload_replaces_the_first_as_active(self):
        self.upload()
        self.upload(name="договор-2.docx")
        rows = _run(self.crm.doc_templates("contract"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(1 for r in rows if r["active"]), 1,
                         "включённый шаблон у вида ровно один")

    def test_archive_can_be_enabled_back(self):
        self.upload()
        self.upload(name="договор-2.docx")
        old = [r for r in _run(self.crm.doc_templates("contract"))
               if not r["active"]][0]
        self.client.post("/documents/contract",
                         data={"action": "enable", "template_id": str(old["id"])})
        self.assertEqual(_run(self.crm.active_doc_template("contract"))["id"],
                         old["id"])

    def test_active_template_is_not_dropped(self):
        self.upload()
        row = _run(self.crm.active_doc_template("contract"))
        self.client.post("/documents/contract",
                         data={"action": "drop", "template_id": str(row["id"])})
        self.assertIsNotNone(_run(self.crm.doc_template(row["id"])),
                             "иначе вид документа остался бы без шаблона")

    def test_dropping_from_archive_removes_the_file(self):
        self.upload()
        self.client.post("/documents/contract", data={"action": "ours"})
        row = _run(self.crm.doc_templates("contract"))[0]
        path = self.folder / row["filename"]
        self.client.post("/documents/contract",
                         data={"action": "drop", "template_id": str(row["id"])})
        self.assertIsNone(_run(self.crm.doc_template(row["id"])))
        self.assertFalse(path.exists())

    def test_esign_cannot_be_uploaded(self):
        r = self.upload(kind="esign")
        self.assertEqual(r.status_code, 404)

    def test_download_ours_and_mine(self):
        self.assertEqual(self.client.get("/documents/ours/contract").status_code,
                         200)
        self.upload()
        row = _run(self.crm.active_doc_template("contract"))
        got = self.client.get(f"/documents/mine/{row['id']}")
        self.assertEqual(got.status_code, 200)

    def test_marks_upload_and_drop(self):
        r = self.client.post("/documents/marks/stamp",
                             files={"mark": ("печать.png", PNG, "image/png")})
        self.assertEqual(r.status_code, 303)
        marks = {m["kind"]: m for m in _run(self.crm.company_marks())}
        self.assertIn("stamp", marks)
        self.assertTrue((self.folder / "stamp.png").is_file())
        self.assertEqual(self.client.get("/documents/marks/stamp").status_code,
                         200)
        self.client.post("/documents/marks/stamp", data={"action": "drop"})
        self.assertEqual(_run(self.crm.company_marks()), [])
        self.assertFalse((self.folder / "stamp.png").exists())

    def test_only_png_marks(self):
        self.client.post("/documents/marks/stamp",
                         files={"mark": ("печать.jpg", PNG, "image/jpeg")})
        self.assertEqual(_run(self.crm.company_marks()), [])


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
