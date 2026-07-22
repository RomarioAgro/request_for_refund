"""Минимальные проверки генератора заявления на возврат."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from refund_application_generator import (
    ApplicationError,
    build_pdf_path,
    load_config,
    load_json_file,
    render_template,
    validate_data,
)


class RefundApplicationGeneratorTest(unittest.TestCase):
    """Проверяет обработку входных данных без запуска Chromium."""

    def test_cp1251_validation_sum_and_safe_rendering(self) -> None:
        """Читает CP1251, считает сумму и экранирует значения ровно один раз."""
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

        self.assertEqual(total, 4664)
        self.assertEqual(values["date"], "20.07.2026")
        self.assertEqual(values["total_amount"], "4 664")
        template = "{recipient}|{org}|{address}|{date}|{items}|{total_amount}|{application_date}"
        rendered = render_template(template, values)
        self.assertIn("&lt;Главному&gt;", rendered)
        self.assertIn("Улица {org}", rendered)
        self.assertIn("1. Товар &lt;1&gt;\n2. Товар &amp; 2", rendered)

    def test_rejects_fractional_price(self) -> None:
        """Отклоняет цену с копейками."""
        data = {
            "date": "20260720",
            "org": "Организация",
            "recipient": "Получатель",
            "address": "Адрес",
            "items": [{"name": "Товар", "price": 1.5}],
        }
        with self.assertRaisesRegex(ApplicationError, "неотрицательным целым"):
            validate_data(data)

    def test_config_allows_percent_in_paths(self) -> None:
        """Не интерпретирует знак процента в путях config.ini."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template%20.html"
            template.write_text("template", encoding="utf-8")
            config = root / "config.ini"
            config.write_text(
                "[paths]\n"
                "template = template%20.html\n"
                "logs_dir = logs%20dir\n"
                "pdf_output_dir = output%20dir\n",
                encoding="utf-8",
            )
            paths = load_config(config)
        self.assertEqual(paths.template.name, "template%20.html")

    def test_rejects_unknown_or_double_braced_markers(self) -> None:
        """Не допускает служебные метки вне утверждённого формата."""
        values = {field: field for field in (
            "recipient", "org", "address", "date", "items", "total_amount", "application_date"
        )}
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


if __name__ == "__main__":
    unittest.main()
