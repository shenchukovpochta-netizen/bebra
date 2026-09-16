"""Машиночитаемая зона паспорта: контрольные цифры, разбор, сверка с анкетой.

Чистая логика - без tesseract и без картинок. Распознаватель (app/services/ocr.py)
проверяется отдельно на том, что он молчит, когда tesseract недоступен:
запускать его в тестах нечем, а его единственный контракт - «не падать».
"""

from __future__ import annotations

import io
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import mrz, ocr  # noqa: E402

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    HAVE_OCR = ocr.available()
except ImportError:                                    # pragma: no cover
    HAVE_OCR = False

TODAY = date(2026, 9, 16)

# Паспорт 1234 567890, Иванов Иван Иванович, род. 15.03.1995,
# выдан 20.06.2015, код подразделения 160-002. Контрольные цифры посчитаны
# по ICAO 9303 и вписаны в строку, а не взяты из проверяемого кода.
LINE1 = "PNRUSIVANOV<<IVAN<IVANOVICH<<<<<<<<<<<<<<<<<"
LINE2 = "1235678909RUS9503157M<<<<<<04160002150620<98"
GOOD = f"{LINE1}\n{LINE2}\n"

ANKETA = {"fio": "Иванов Иван Иванович", "passport_number": "1234 567890",
          "birth_date": "15.03.1995", "passport_date": "20.06.2015",
          "passport_code": "160-002"}


class TestCheckDigit(unittest.TestCase):
    def test_icao_vectors(self):
        """Контрольные примеры из ICAO 9303 - независимо от нашего разбора."""
        self.assertEqual(mrz.check_digit("L898902C3"), "6")
        self.assertEqual(mrz.check_digit("740812"), "2")
        self.assertEqual(mrz.check_digit("120415"), "9")
        self.assertEqual(mrz.check_digit("<<<<<<"), "0")

    def test_junk_never_matches(self):
        self.assertEqual(mrz.check_digit("12*45"), "<")

    def test_digit_correction_is_one_way(self):
        self.assertEqual(mrz.digits_only("I2O4"), "1204")
        self.assertEqual(mrz.digits_only("<<<"), "<<<", "заполнитель не трогаем")


class TestParse(unittest.TestCase):
    def test_reads_every_field(self):
        m = mrz.parse(GOOD, today=TODAY)
        self.assertEqual(m.surname, "IVANOV")
        self.assertEqual(m.given, "IVAN IVANOVICH")
        self.assertEqual(m.passport_number, "1234 567890")
        self.assertEqual(m.number_digits, "123567890")
        self.assertEqual(m.birth_date, date(1995, 3, 15))
        self.assertEqual(m.sex, "М")
        self.assertEqual(m.passport_date, date(2015, 6, 20))
        self.assertEqual(m.passport_code, "160-002")
        self.assertEqual(m.failed, ())
        self.assertTrue(m.ok)

    def test_finds_lines_among_other_text(self):
        noisy = f"ROSSIYSKAYA FEDERATSIYA\n1234 5678\n{LINE1}\n{LINE2}\nхвост"
        self.assertEqual(mrz.parse(noisy, today=TODAY).passport_number, "1234 567890")
        self.assertIsNone(mrz.parse("совсем не паспорт", today=TODAY))
        self.assertIsNone(mrz.parse("", today=TODAY))

    def test_survives_letter_for_digit_misreads(self):
        # тессеракт читает 0 как O, 1 как I: в числовых полях это чинится,
        # и контрольная цифра подтверждает, что починилось верно
        broken = LINE2.replace("1235678909", "I235678909", 1)
        m = mrz.parse(f"{LINE1}\n{broken}\n", today=TODAY)
        self.assertEqual(m.passport_number, "1234 567890")
        self.assertEqual(m.failed, ())

    def test_bad_check_digit_drops_the_field(self):
        # одна цифра номера прочитана неверно - поле не отдаётся вовсе
        broken = "1235678919" + LINE2[10:]
        m = mrz.parse(f"{LINE1}\n{broken}\n", today=TODAY)
        self.assertEqual(m.number_digits, "")
        self.assertEqual(m.passport_number, "")
        self.assertIn("серия и номер", m.failed)
        self.assertEqual(m.birth_date, date(1995, 3, 15), "остальные поля целы")

    def test_broken_personal_number_keeps_nine_digits(self):
        """Личный номер не сошёлся: четвёртой цифры серии нет, девять - есть."""
        broken = LINE2[:42] + "7" + LINE2[43]
        m = mrz.parse(f"{LINE1}\n{broken}\n", today=TODAY)
        self.assertEqual(m.passport_number, "")
        self.assertEqual(m.number_digits, "123567890")
        self.assertEqual(m.passport_code, "")
        self.assertIsNone(m.passport_date)
        self.assertTrue(m.ok)

    def test_century_is_chosen_so_the_date_is_not_in_the_future(self):
        # «50» в дате рождения - это 1950, а не 2050
        old_birth = "500315"
        line = LINE2[:13] + old_birth + mrz.check_digit(old_birth) + LINE2[20:]
        m = mrz.parse(f"{LINE1}\n{line}\n", today=TODAY)
        self.assertEqual(m.birth_date, date(1950, 3, 15))

    def test_names_without_patronymic(self):
        line1 = "PNRUSPETROV<<PETR".ljust(44, "<")
        m = mrz.parse(f"{line1}\n{LINE2}\n", today=TODAY)
        self.assertEqual(m.surname, "PETROV")
        self.assertEqual(m.given, "PETR")


class TestCompare(unittest.TestCase):
    def test_everything_matches(self):
        result = mrz.compare(mrz.parse(GOOD, today=TODAY), ANKETA)
        self.assertEqual(result["differ"], [])
        self.assertEqual(result["unknown"], [])
        self.assertEqual(len(result["same"]), 4)

    def test_typo_in_the_anketa_is_shown_with_both_values(self):
        typo = {**ANKETA, "passport_number": "1234 567891"}
        result = mrz.compare(mrz.parse(GOOD, today=TODAY), typo)
        self.assertEqual(result["differ"],
                         ["серия и номер: в МЧЗ 1234 567890, в анкете 1234 567891"])
        self.assertEqual(len(result["same"]), 3)

    def test_empty_anketa_field_counts_as_a_difference(self):
        result = mrz.compare(mrz.parse(GOOD, today=TODAY), {**ANKETA, "passport_code": ""})
        self.assertIn("код подразделения: в МЧЗ 160-002, в анкете пусто", result["differ"])

    def test_nine_digits_compared_when_personal_number_failed(self):
        broken = LINE2[:42] + "7" + LINE2[43]
        m = mrz.parse(f"{LINE1}\n{broken}\n", today=TODAY)
        result = mrz.compare(m, ANKETA)
        self.assertIn("серия и номер (без 4-й цифры серии)", result["same"])
        self.assertIn("код подразделения", result["unknown"])
        # опечатка в шести цифрах номера видна и без четвёртой цифры серии
        wrong = mrz.compare(m, {**ANKETA, "passport_number": "1234 567899"})
        self.assertEqual(wrong["differ"],
                         ["серия и номер (без 4-й цифры серии): "
                          "в МЧЗ 123567890, в анкете 123567899"])

    def test_no_mrz_at_all(self):
        result = mrz.compare(None, ANKETA)
        self.assertEqual(len(result["unknown"]), 4)
        self.assertEqual(result["differ"], [])


class TestSummary(unittest.TestCase):
    def test_match(self):
        matched, line = mrz.summary(mrz.parse(GOOD, today=TODAY), ANKETA)
        self.assertTrue(matched)
        self.assertTrue(line.startswith("сходится — "))
        self.assertIn("дата выдачи", line)

    def test_mismatch(self):
        matched, line = mrz.summary(mrz.parse(GOOD, today=TODAY),
                                    {**ANKETA, "birth_date": "16.03.1995"})
        self.assertFalse(matched)
        self.assertIn("в МЧЗ 15.03.1995, в анкете 16.03.1995", line)
        self.assertNotIn("сходится", line)

    def test_long_mismatch_is_clipped(self):
        """Разошлись все четыре поля - строка всё равно короткая: подпись
        к фотографии делится с реквизитами договора."""
        other = {"passport_number": "9999 999999", "birth_date": "01.01.1980",
                 "passport_date": "02.02.2000", "passport_code": "999-999"}
        matched, line = mrz.summary(mrz.parse(GOOD, today=TODAY), other)
        self.assertFalse(matched)
        self.assertLessEqual(len(line), mrz.SUMMARY_LIMIT)
        self.assertTrue(line.endswith("…"))
        self.assertIn("серия и номер", line, "первое расхождение видно целиком")

    def test_nothing_to_show(self):
        self.assertIsNone(mrz.summary(None, ANKETA))
        self.assertIsNone(mrz.summary(mrz.Mrz(), ANKETA), "пустая МЧЗ - молчим")

    def test_partial_read_mentions_what_was_missed(self):
        broken = LINE2[:42] + "7" + LINE2[43]
        matched, line = mrz.summary(mrz.parse(f"{LINE1}\n{broken}\n", today=TODAY), ANKETA)
        self.assertTrue(matched)
        self.assertIn("не прочиталось: дата выдачи, код подразделения", line)


@unittest.skipUnless(HAVE_OCR, "нет tesseract или Pillow")
class TestOcrEndToEnd(unittest.TestCase):
    """Картинка -> tesseract -> разбор -> сверка, целиком и на своём железе.

    Рисуется разворот с посторонним текстом сверху и МЧЗ внизу: так проверяется
    и поиск нужных строк среди прочего, и подготовка картинки.
    """

    FONTS = "/usr/share/fonts/truetype/dejavu"

    def page(self, width: int = 1100, height: int = 760) -> bytes:
        image = Image.new("RGB", (width, height), (236, 232, 220))
        draw = ImageDraw.Draw(image)
        head = ImageFont.truetype(f"{self.FONTS}/DejaVuSans.ttf", 26)
        mono = ImageFont.truetype(f"{self.FONTS}/DejaVuSansMono.ttf", 30)
        for i, line in enumerate(("ROSSIYSKAYA FEDERATSIYA", "PASPORT VYDAN OTDELOM",
                                  "IVANOV", "IVAN IVANOVICH", "15.03.1995")):
            draw.text((60, 40 + i * 52), line, font=head, fill=(40, 40, 60))
        draw.text((40, height - 120), LINE1, font=mono, fill=(20, 20, 20))
        draw.text((40, height - 70), LINE2, font=mono, fill=(20, 20, 20))
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=82)
        return buf.getvalue()

    def test_reads_a_rendered_page(self):
        text = ocr.read(self.page())
        self.assertIsNotNone(text, "МЧЗ не прочиталась с чистой картинки")
        m = mrz.parse(text, today=TODAY)
        self.assertEqual(m.passport_number, "1234 567890")
        self.assertEqual(m.birth_date, date(1995, 3, 15))
        self.assertEqual(mrz.summary(m, ANKETA)[0], True)

    def test_small_photo_is_enlarged_before_reading(self):
        """Снимок с телефона приходит мелким - его растягивают перед чтением.

        Проверяется подготовка, а не результат распознавания: «прочиталось ли»
        зависит от версии tesseract и загрузки машины, и такой тест ронял бы
        деплой на ровном месте. Само чтение проверяет тест выше на чистой
        картинке, а на мелкой - руками, когда меняют подготовку.
        """
        with Image.open(io.BytesIO(self.page())) as big:
            small = big.resize((550, 380))
            buf = io.BytesIO()
            small.save(buf, "JPEG", quality=70)
        variants = ocr._prepared(buf.getvalue())
        self.assertEqual(len(variants), 2, "нижняя полоса и вся страница")
        for raw in variants:
            with Image.open(io.BytesIO(raw)) as prepared:
                self.assertGreaterEqual(prepared.width, ocr.TARGET_WIDTH)
                self.assertEqual(prepared.mode, "L", "обесцвечено")
        with Image.open(io.BytesIO(variants[0])) as strip, \
                Image.open(io.BytesIO(variants[1])) as whole:
            self.assertLess(strip.height, whole.height, "полоса - часть страницы")

    def test_unreadable_photo_stays_silent(self):
        """Размытый снимок - молчание, а не выдуманные цифры."""
        with Image.open(io.BytesIO(self.page())) as big:
            blurred = big.resize((550, 380)).filter(ImageFilter.GaussianBlur(1.2))
            buf = io.BytesIO()
            blurred.save(buf, "JPEG", quality=60)
        m = mrz.parse(ocr.read(buf.getvalue()) or "", today=TODAY)
        self.assertIsNone(mrz.summary(m, ANKETA))


class TestCardLine(unittest.IsolatedAsyncioTestCase):
    """Строка карточки: одна на оба мессенджера, и она никогда не падает."""

    async def test_match_and_mismatch(self):
        original = ocr.read_file
        try:
            ocr.read_file = lambda path: GOOD
            line = await ocr.card_line({"doc_path": "/есть.jpg"}, ANKETA)
            self.assertIn("МЧЗ", line)
            self.assertNotIn("расходится", line)
            line = await ocr.card_line({"doc_path": "/есть.jpg"},
                                       {**ANKETA, "passport_code": "999-999"})
            self.assertIn("расходится", line)
            self.assertIn("160-002", line)
        finally:
            ocr.read_file = original

    async def test_unreadable_gives_no_line(self):
        original = ocr.read_file
        try:
            ocr.read_file = lambda path: None
            self.assertEqual(await ocr.card_line({"doc_path": None}, ANKETA), "")
        finally:
            ocr.read_file = original

    async def test_broken_recognizer_does_not_break_the_card(self):
        original = ocr.read_file

        def boom(path):
            raise RuntimeError("tesseract сломался")

        try:
            ocr.read_file = boom
            self.assertEqual(await ocr.card_line({"doc_path": "/есть.jpg"}, ANKETA), "")
        finally:
            ocr.read_file = original


class TestOcrDegradesQuietly(unittest.TestCase):
    """Распознаватель обязан молчать, а не падать: он подсказка, и его
    отсутствие не должно ломать ни регистрацию, ни карточку модератора."""

    def test_missing_file(self):
        self.assertIsNone(ocr.read_file(None))
        self.assertIsNone(ocr.read_file("/нет/такого/файла.jpg"))

    def test_garbage_bytes(self):
        self.assertIsNone(ocr.read(b"not an image at all"))

    def test_without_tesseract(self):
        original = ocr.available
        ocr.available = lambda: False
        try:
            self.assertIsNone(ocr.read(b"\xff\xd8\xff"))
        finally:
            ocr.available = original


if __name__ == "__main__":
    unittest.main()
