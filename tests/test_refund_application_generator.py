"""Проверки генератора заявления на возврат."""

from __future__ import annotations

import logging
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pypdf import PdfReader

from refund_application_generator import (
    ApplicationError,
    build_pdf_path,
    create_link_callback,
    create_pdf,
    generate,
    load_config,
    load_json_file,
    render_template,
    validate_data,
    validate_template_resources,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = PROJECT_ROOT / "templates" / "refund_application_template_xhtml2pdf.html"


def sample_data(items: list[dict] | None = None) -> dict:
    """Возвращает корректный минимальный JSON-объект для тестов."""
    return {
        "date": "20260720",
        "org": 'ООО "Клевер Урал"',
        "recipient": 'Генеральному директору ООО "Клевер Урал" Валейня И. Л.',
        "address": "Тольятти, Автозаводское шоссе, 6",
        "items": items or [{"name": "Товар", "price": 100}],
    }


def rendered_document(data: dict | None = None) -> str:
    """Заполняет реальный xhtml2pdf-шаблон тестовыми данными."""
    values, _ = validate_data(data or sample_data())
    return render_template(TEMPLATE_PATH.read_text(encoding="utf-8-sig"), values)


class RefundApplicationGeneratorTest(unittest.TestCase):
    """Проверяет бизнес-логику и реальную конвертацию xhtml2pdf."""

    def test_cp1251_validation_sum_and_safe_rendering(self) -> None:
        """Читает CP1251, считает сумму и экранирует пользовательские значения."""
        source = (
            '{"date":"20260720","org":"ООО \\\"Клевер & Ко\\\"",'
            '"recipient":"Директору <Главному>","address":"Улица {org}",'
            '"items":[{"name":"Товар <1>","price":2932.0},'
            '{"name":"Товар & 2","price":1732}]}'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "return.json"
            path.write_bytes(source.encode("cp1251"))
            values, total = validate_data(load_json_file(path))

        rendered = render_template(TEMPLATE_PATH.read_text(encoding="utf-8-sig"), values)
        self.assertEqual(total, 4664)
        self.assertIn("&lt;Главному&gt;", rendered)
        self.assertIn("Улица {org}", rendered)
        self.assertIn('<div class="item">1. Товар &lt;1&gt;</div>', rendered)
        self.assertIn('<div class="item">2. Товар &amp; 2</div>', rendered)

    def test_one_item_has_no_number(self) -> None:
        """Выводит единственный товар без номера."""
        document = rendered_document(sample_data([{"name": "Один товар", "price": 1}]))
        self.assertIn('<div class="item">Один товар</div>', document)
        self.assertNotIn('<div class="item">1. Один товар</div>', document)

    def test_multiple_items_are_numbered_in_separate_divs(self) -> None:
        """Выводит несколько товаров отдельными нумерованными строками."""
        document = rendered_document(sample_data([
            {"name": "Первый", "price": 1},
            {"name": "Второй", "price": 2},
        ]))
        self.assertIn('<div class="item">1. Первый</div><div class="item">2. Второй</div>', document)

    def test_item_html_is_escaped(self) -> None:
        """Не допускает HTML-разметку из названия товара."""
        document = rendered_document(sample_data([{"name": '<b>"A" & B</b>', "price": 1}]))
        self.assertIn("&lt;b&gt;&quot;A&quot; &amp; B&lt;/b&gt;", document)
        self.assertNotIn("<b>\"A\"", document)

    def test_real_pdf_has_signature_and_russian_text(self) -> None:
        """Создаёт реальный PDF с извлекаемым русским текстом."""
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "result.pdf"
            create_pdf(rendered_document(), pdf_path, TEMPLATE_PATH)
            text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
            self.assertTrue(pdf_path.read_bytes().startswith(b"%PDF-"))
            self.assertGreater(pdf_path.stat().st_size, 1000)
            self.assertIn("заявление", text.lower())
            self.assertIn("Клевер Урал", text)

    def test_typical_document_has_one_page(self) -> None:
        """Помещает типичный документ на одну страницу A4."""
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "typical.pdf"
            create_pdf(rendered_document(sample_data([
                {"name": "Товар один", "price": 1},
                {"name": "Товар два", "price": 2},
                {"name": "Товар три", "price": 3},
            ])), pdf_path, TEMPLATE_PATH)
            self.assertEqual(len(PdfReader(pdf_path).pages), 1)

    def test_many_items_create_multiple_readable_pages(self) -> None:
        """Создаёт читаемый многостраничный PDF для большого списка."""
        items = [{"name": f"Длинное наименование товара номер {index} " * 3, "price": 1} for index in range(1, 61)]
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "many.pdf"
            create_pdf(rendered_document(sample_data(items)), pdf_path, TEMPLATE_PATH)
            reader = PdfReader(pdf_path)
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            self.assertGreater(len(reader.pages), 1)
            self.assertIn("номер 60", text)

    def test_parallel_pdf_creation_is_serialized_safely(self) -> None:
        """Создаёт два PDF параллельными вызовами без конфликта временных TTF."""
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)

            def create(index: int) -> Path:
                pdf_path = output_dir / f"parallel-{index}.pdf"
                create_pdf(rendered_document(), pdf_path, TEMPLATE_PATH)
                return pdf_path

            with ThreadPoolExecutor(max_workers=2) as executor:
                pdf_paths = list(executor.map(create, (1, 2)))
            self.assertTrue(all(path.read_bytes().startswith(b"%PDF-") for path in pdf_paths))

    def test_long_item_is_present_in_pdf(self) -> None:
        """Сохраняет длинное название товара в PDF."""
        name = "Очень длинное наименование товара " * 12
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "long.pdf"
            create_pdf(rendered_document(sample_data([{"name": name, "price": 1}])), pdf_path, TEMPLATE_PATH)
            text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
            self.assertIn("Очень длинное наименование", text)

    def test_missing_regular_font_is_rejected(self) -> None:
        """Отклоняет проект без обычного TTF-шрифта."""
        self._assert_missing_font("DejaVuSerif.ttf")

    def test_missing_bold_font_is_rejected(self) -> None:
        """Отклоняет проект без жирного TTF-шрифта."""
        self._assert_missing_font("DejaVuSerif-Bold.ttf")

    def _assert_missing_font(self, missing_name: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template_dir = root / "templates"
            fonts_dir = root / "fonts"
            template_dir.mkdir()
            fonts_dir.mkdir()
            shutil.copy2(TEMPLATE_PATH, template_dir / TEMPLATE_PATH.name)
            for font_name in ("DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf"):
                if font_name != missing_name:
                    shutil.copy2(PROJECT_ROOT / "fonts" / font_name, fonts_dir / font_name)
            with self.assertRaisesRegex(ApplicationError, missing_name):
                validate_template_resources(template_dir / TEMPLATE_PATH.name)

    def test_converter_error_removes_partial_pdf(self) -> None:
        """Удаляет неполный PDF, если xhtml2pdf сообщил об ошибке."""
        def failed_create_pdf(*, dest, **_kwargs):
            dest.write(b"partial")
            return SimpleNamespace(err=1)

        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "partial.pdf"
            with patch("xhtml2pdf.pisa.CreatePDF", side_effect=failed_create_pdf):
                with self.assertRaisesRegex(ApplicationError, "сообщил об ошибке"):
                    create_pdf("<html></html>", pdf_path, TEMPLATE_PATH)
            self.assertFalse(pdf_path.exists())

    def test_network_resource_is_rejected(self) -> None:
        """Запрещает сетевые ресурсы."""
        callback = create_link_callback(TEMPLATE_PATH)
        with self.assertRaisesRegex(ApplicationError, "запрещён"):
            callback("https://example.com/font.ttf", None)

    def test_resource_cannot_escape_project(self) -> None:
        """Запрещает выход за корневой каталог проекта через .. ."""
        callback = create_link_callback(TEMPLATE_PATH)
        with self.assertRaisesRegex(ApplicationError, "выходит за каталог"):
            callback("../../outside.ttf", None)

    def test_recipient_wrap_regression(self) -> None:
        """Сохраняет условный перенос получателя после закрывающей кавычки."""
        values, _ = validate_data(sample_data())
        self.assertEqual(
            values["recipient"],
            'Генеральному директору ООО "Клевер Урал"\u200b Валейня\u00a0И.\u00a0Л.',
        )

    def test_rejects_fractional_price(self) -> None:
        """Отклоняет цену с копейками."""
        data = sample_data([{"name": "Товар", "price": 1.5}])
        with self.assertRaisesRegex(ApplicationError, "неотрицательным целым"):
            validate_data(data)

    def test_config_allows_percent_in_paths(self) -> None:
        """Не интерпретирует знак процента в путях config.ini."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.ini"
            config.write_text(
                "[paths]\ntemplate = template%20.html\nlogs_dir = logs%20dir\npdf_output_dir = output%20dir\n",
                encoding="utf-8",
            )
            paths = load_config(config)
        self.assertEqual(paths.template.name, "template%20.html")

    def test_rejects_unknown_or_double_braced_markers(self) -> None:
        """Не допускает служебные метки вне утверждённого формата."""
        values = {field: field for field in (
            "recipient", "org", "address", "date", "total_amount", "application_date"
        )}
        values["items"] = ["item"]
        valid = "|".join(f"{{{field}}}" for field in values)
        with self.assertRaisesRegex(ApplicationError, "неизвестны"):
            render_template(f"{valid}|{{bad1}}", values)
        with self.assertRaisesRegex(ApplicationError, "двойными"):
            render_template(valid.replace("{org}", "{{org}}"), values)

    def test_pdf_name_does_not_overwrite_existing_file(self) -> None:
        """Добавляет суффикс, если имя с той же секундой уже занято."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            moment = datetime(2026, 7, 22, 10, 30, 15)
            first = build_pdf_path(output, Path("return.json"), moment)
            first.touch()
            second = build_pdf_path(output, Path("return.json"), moment)
        self.assertEqual(second.stem, f"{first.stem}_1")

    def test_open_failure_keeps_created_pdf(self) -> None:
        """Сохраняет PDF и успешный результат при ошибке автоматического открытия."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.ini"
            config.write_text(
                "[paths]\n"
                f"template = {TEMPLATE_PATH}\n"
                "logs_dir = logs\n"
                "pdf_output_dir = output\n",
                encoding="utf-8",
            )
            json_path = root / "return.json"
            json_path.write_text(__import__("json").dumps(sample_data(), ensure_ascii=False), encoding="utf-8")
            with patch("refund_application_generator.os.startfile", side_effect=OSError("viewer error")):
                pdf_path = generate(json_path, config)
            self.assertTrue(pdf_path.is_file())
            logging.shutdown()

    def test_source_does_not_use_playwright(self) -> None:
        """Не оставляет рабочих импортов и запусков Playwright/Chromium."""
        source = (PROJECT_ROOT / "refund_application_generator.py").read_text(encoding="utf-8")
        self.assertNotIn("playwright", source.lower())
        self.assertNotIn("chromium", source.lower())


if __name__ == "__main__":
    unittest.main()
