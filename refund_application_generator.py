"""Формирование заявления на возврат из JSON-файла."""

from __future__ import annotations

import argparse
import configparser
import html
import json
import logging
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = {"recipient", "org", "address", "date", "items", "total_amount", "application_date"}
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
RECIPIENT_NAME_RE = re.compile(
    r"[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z-]*(?:\s+(?:[А-ЯЁA-Z]\.|[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z-]*)){1,2}"
)


class ApplicationError(Exception):
    """Понятная пользователю ошибка формирования заявления."""


@dataclass(frozen=True)
class Paths:
    """Пути проекта, загруженные из config.ini.

    Attributes:
        template: HTML-шаблон заявления.
        logs_dir: Каталог журналов.
        pdf_output_dir: Каталог готовых PDF.
    """

    template: Path
    logs_dir: Path
    pdf_output_dir: Path


def load_config(config_path: Path) -> Paths:
    """Загружает и проверяет пути из config.ini.

    Args:
        config_path: Путь к файлу конфигурации.

    Returns:
        Разрешённые относительно config.ini пути проекта.

    Raises:
        ApplicationError: Конфигурация отсутствует, повреждена или неполна.
    """
    if not config_path.is_file():
        raise ApplicationError(f"Файл конфигурации не найден: {config_path}")

    parser = configparser.ConfigParser(interpolation=None)
    try:
        with config_path.open(encoding="utf-8-sig") as config_file:
            parser.read_file(config_file)
    except (OSError, configparser.Error, UnicodeError) as exc:
        raise ApplicationError(f"Не удалось прочитать конфигурацию {config_path}: {exc}") from exc

    if not parser.has_section("paths"):
        raise ApplicationError(f"В конфигурации {config_path} отсутствует секция [paths]")

    base_dir = config_path.resolve().parent
    values: dict[str, Path] = {}
    for key in ("template", "logs_dir", "pdf_output_dir"):
        value = parser.get("paths", key, fallback="").strip()
        if not value:
            raise ApplicationError(f"В секции [paths] отсутствует параметр {key}")
        path = Path(value)
        values[key] = path if path.is_absolute() else base_dir / path

    if not values["template"].is_file():
        raise ApplicationError(f"HTML-шаблон не найден: {values['template']}")

    for key in ("logs_dir", "pdf_output_dir"):
        try:
            values[key].mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ApplicationError(f"Не удалось создать каталог {values[key]}: {exc}") from exc

    return Paths(**values)


def load_json_file(json_path: Path) -> dict[str, Any]:
    """Читает JSON в UTF-8/UTF-8 BOM или Windows-1251.

    Args:
        json_path: Путь к входному JSON.

    Returns:
        Корневой JSON-объект.

    Raises:
        ApplicationError: Файл недоступен, имеет неподдерживаемую кодировку или
            содержит некорректный JSON.
    """
    if not json_path.is_file():
        raise ApplicationError(f"JSON-файл не найден: {json_path}")

    try:
        raw = json_path.read_bytes()
    except OSError as exc:
        raise ApplicationError(f"Не удалось прочитать JSON-файл {json_path}: {exc}") from exc

    text = None
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ApplicationError(f"Не удалось определить кодировку JSON-файла {json_path}")

    try:
        data = json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ApplicationError(f"Некорректный JSON в файле {json_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ApplicationError(f"Корневой элемент JSON-файла {json_path} должен быть объектом")
    return data


def validate_data(data: dict[str, Any]) -> tuple[dict[str, str], int]:
    """Проверяет данные возврата и готовит значения шаблона.

    Args:
        data: Корневой JSON-объект.

    Returns:
        Пару из текстовых значений полей и общей суммы возврата.

    Raises:
        ApplicationError: Обязательное поле отсутствует или имеет неверный тип.
    """
    strings: dict[str, str] = {}
    for field in ("recipient", "org", "address", "date"):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ApplicationError(f"Поле {field} должно быть непустой строкой")
        strings[field] = value

    try:
        purchase_date = datetime.strptime(strings["date"], "%Y%m%d").strftime("%d.%m.%Y")
    except ValueError as exc:
        raise ApplicationError("Поле date должно содержать корректную дату в формате YYYYMMDD") from exc

    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ApplicationError("Поле items должно быть непустым массивом")

    names: list[str] = []
    total = 0
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ApplicationError(f"Элемент items[{index - 1}] должен быть объектом")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ApplicationError(f"Поле items[{index - 1}].name должно быть непустой строкой")
        price = item.get("price")
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            raise ApplicationError(f"Поле items[{index - 1}].price должно быть целым числом")
        if not math.isfinite(price) or not float(price).is_integer() or price < 0:
            raise ApplicationError(f"Поле items[{index - 1}].price должно быть неотрицательным целым числом")
        names.append(f"{index}. {name.strip()}" if len(items) > 1 else name.strip())
        total += int(price)

    if total <= 0:
        raise ApplicationError("Общая сумма возврата должна быть больше нуля")

    recipient = strings["recipient"]
    quote_index = max(recipient.rfind(quote) for quote in ('"', "»", "”"))
    suffix = recipient[quote_index + 1 :].lstrip() if quote_index >= 0 else ""
    if suffix:
        if RECIPIENT_NAME_RE.fullmatch(suffix):
            suffix = suffix.replace(" ", chr(160))
        recipient = f"{recipient[:quote_index + 1]}\u200b {suffix}"

    return {
        "recipient": recipient,
        "org": strings["org"],
        "address": strings["address"],
        "date": purchase_date,
        "items": "\n".join(names),
        "total_amount": f"{total:,}".replace(",", " "),
        "application_date": datetime.now().strftime("%d.%m.%Y"),
    }, total


def render_template(template: str, values: dict[str, str]) -> str:
    """Безопасно подставляет значения в известные метки HTML-шаблона.

    Args:
        template: Исходный HTML.
        values: Значения меток без фигурных скобок.

    Returns:
        Заполненный HTML.

    Raises:
        ApplicationError: Набор меток шаблона не соответствует требованиям.
    """
    placeholders = set(PLACEHOLDER_RE.findall(template))
    if "{{" in template or "}}" in template:
        raise ApplicationError("HTML-шаблон содержит метку с двойными фигурными скобками")
    if placeholders != REQUIRED_FIELDS:
        missing = sorted(REQUIRED_FIELDS - placeholders)
        unknown = sorted(placeholders - REQUIRED_FIELDS)
        details = []
        if missing:
            details.append(f"отсутствуют: {', '.join(missing)}")
        if unknown:
            details.append(f"неизвестны: {', '.join(unknown)}")
        raise ApplicationError(f"Некорректные метки HTML-шаблона ({'; '.join(details)})")

    escaped = {key: html.escape(value, quote=True) for key, value in values.items()}
    return PLACEHOLDER_RE.sub(lambda match: escaped[match.group(1)], template)


def build_pdf_path(output_dir: Path, json_path: Path, now: datetime | None = None) -> Path:
    """Создаёт не занятое имя итогового PDF.

    Args:
        output_dir: Каталог результатов.
        json_path: Исходный JSON, имя которого входит в имя PDF.
        now: Время формирования; параметр полезен для проверки.

    Returns:
        Свободный путь с отметкой времени.
    """
    timestamp = (now or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
    base = output_dir / f"Заявление_на_возврат_{json_path.stem}_{timestamp}.pdf"
    candidate = base
    counter = 1
    while candidate.exists():
        candidate = base.with_stem(f"{base.stem}_{counter}")
        counter += 1
    return candidate


def create_pdf(document_html: str, pdf_path: Path) -> None:
    """Формирует PDF из HTML через Chromium/Playwright.

    Args:
        document_html: Заполненный HTML-документ.
        pdf_path: Путь создаваемого PDF.

    Raises:
        ApplicationError: Playwright или Chromium недоступны либо PDF не создан.
    """
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as exc:
        raise ApplicationError("Библиотека Playwright не установлена. Выполните: pip install -r requirements.txt") from exc

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                page.set_content(document_html, wait_until="load")
                page.pdf(
                    path=str(pdf_path),
                    format="A4",
                    scale=1,
                    print_background=True,
                    display_header_footer=False,
                    prefer_css_page_size=True,
                )
            finally:
                browser.close()
    except Error as exc:
        raise ApplicationError(
            f"Не удалось сформировать PDF через Chromium: {exc}. Выполните: playwright install chromium"
        ) from exc

    if not pdf_path.is_file():
        raise ApplicationError(f"PDF не был создан: {pdf_path}")


def configure_logging(logs_dir: Path) -> Path:
    """Настраивает запись технического журнала в файл.

    Args:
        logs_dir: Каталог журналов.

    Returns:
        Путь к созданному файлу журнала.
    """
    log_path = logs_dir / f"refund_application_{datetime.now():%Y-%m-%d_%H-%M-%S}.log"
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        encoding="utf-8",
        force=True,
    )
    return log_path


def generate(json_path: Path, config_path: Path) -> Path:
    """Выполняет полный цикл формирования и открытия заявления.

    Args:
        json_path: Путь к входному JSON.
        config_path: Путь к config.ini.

    Returns:
        Путь к созданному PDF.

    Raises:
        ApplicationError: Проверка данных или создание PDF завершились ошибкой.

    Side Effects:
        Создаёт каталог журналов, PDF и открывает его системной программой Windows.
    """
    paths = load_config(config_path)
    configure_logging(paths.logs_dir)
    logger.info("Запуск. Входной JSON: %s", json_path.resolve())
    logger.info("HTML-шаблон: %s", paths.template.resolve())

    data = load_json_file(json_path)
    values, total = validate_data(data)
    try:
        template = paths.template.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ApplicationError(f"Не удалось прочитать HTML-шаблон {paths.template}: {exc}") from exc

    document_html = render_template(template, values)
    pdf_path = build_pdf_path(paths.pdf_output_dir, json_path)
    logger.info("Количество товаров: %d", len(data["items"]))
    logger.info("Общая сумма: %d", total)
    create_pdf(document_html, pdf_path)
    logger.info("PDF создан: %s", pdf_path.resolve())

    try:
        os.startfile(pdf_path.resolve())
    except OSError as exc:
        raise ApplicationError(f"PDF создан, но не удалось открыть файл {pdf_path}: {exc}") from exc
    return pdf_path.resolve()


def main(argv: list[str] | None = None) -> int:
    """Обрабатывает аргументы командной строки и запускает генератор.

    Args:
        argv: Аргументы без имени программы; по умолчанию используются sys.argv.

    Returns:
        Код завершения процесса: 0 при успехе, 1 при ошибке.
    """
    parser = argparse.ArgumentParser(description="Формирование заявления на возврат в PDF")
    parser.add_argument("json_file", type=Path, help="путь к JSON-файлу возврата")
    args = parser.parse_args(argv)
    config_path = Path(__file__).resolve().with_name("config.ini")

    try:
        pdf_path = generate(args.json_file, config_path)
    except ApplicationError as exc:
        logger.exception("Ошибка формирования заявления: %s", exc)
        sys.stderr.write(f"Ошибка: {exc}\n")
        return 1

    sys.stdout.write(f"PDF создан: {pdf_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
