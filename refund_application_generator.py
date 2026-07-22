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
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = {"recipient", "org", "address", "date", "items", "total_amount", "application_date"}
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
RECIPIENT_NAME_RE = re.compile(
    r"[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z-]*(?:\s+(?:[А-ЯЁA-Z]\.|[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z-]*)){1,2}"
)
FONT_FILES = ("DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf")
TemplateValue = str | list[str]
_XHTML2PDF_LOCK = threading.Lock()


class ApplicationError(Exception):
    """Понятная пользователю ошибка формирования заявления."""


@dataclass(frozen=True)
class Paths:
    """Пути проекта, загруженные из config.ini.

    Attributes:
        template: HTML-шаблон заявления.
        fonts_dir: Каталог TTF-шрифтов.
        logs_dir: Каталог журналов.
        pdf_output_dir: Каталог готовых PDF.
    """

    template: Path
    fonts_dir: Path
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
    for key in ("template", "fonts_dir", "logs_dir", "pdf_output_dir"):
        value = parser.get("paths", key, fallback="").strip()
        if not value:
            raise ApplicationError(f"В секции [paths] отсутствует параметр {key}")
        path = Path(value)
        values[key] = path if path.is_absolute() else base_dir / path

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


def validate_data(data: dict[str, Any]) -> tuple[dict[str, TemplateValue], int]:
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
        names.append(name.strip())
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
        "items": names,
        "total_amount": f"{total:,}".replace(",", " "),
        "application_date": datetime.now().strftime("%d.%m.%Y"),
    }, total


def render_template(template: str, values: dict[str, TemplateValue]) -> str:
    """Безопасно подставляет значения в известные метки HTML-шаблона.

    Args:
        template: Исходный HTML.
        values: Значения меток без фигурных скобок; items содержит список имён.

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

    items = values.get("items")
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise ApplicationError("Значение items для шаблона должно быть списком строк")

    escaped = {
        key: html.escape(value, quote=True)
        for key, value in values.items()
        if key != "items" and isinstance(value, str)
    }
    escaped["items"] = "".join(
        f'<div class="item">{f"{index}. " if len(items) > 1 else ""}{html.escape(item, quote=True)}</div>'
        for index, item in enumerate(items, start=1)
    )
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


def validate_template_resources(template_path: Path, fonts_dir: Path) -> None:
    """Проверяет наличие HTML-шаблона и обязательных TTF-шрифтов.

    Args:
        template_path: Путь к HTML-шаблону.
        fonts_dir: Каталог обязательных TTF-шрифтов.

    Raises:
        ApplicationError: Шаблон или один из шрифтов отсутствует.
    """
    if not template_path.is_file():
        raise ApplicationError(f"HTML-шаблон не найден: {template_path}")
    if not fonts_dir.is_dir():
        raise ApplicationError(f"Каталог шрифтов не найден: {fonts_dir}")
    for font_name in FONT_FILES:
        font_path = fonts_dir / font_name
        if not font_path.is_file():
            raise ApplicationError(f"Файл шрифта не найден: {font_path}")


def create_link_callback(template_path: Path, fonts_dir: Path):
    """Создаёт безопасный resolver локальных ресурсов xhtml2pdf.

    Args:
        template_path: Путь исходного HTML-шаблона.
        fonts_dir: Настроенный каталог TTF-шрифтов.

    Returns:
        Callback, преобразующий URI ресурса в абсолютный локальный путь.

    Raises:
        ApplicationError: Callback отклоняет сеть, выход за корень и отсутствующие файлы.
    """
    template_dir = template_path.resolve().parent
    resolved_fonts = {name: (fonts_dir / name).resolve() for name in FONT_FILES}
    expected_font_uris = {f"../fonts/{name}": path for name, path in resolved_fonts.items()}
    expected_font_paths = {
        (template_dir / uri).resolve(): path for uri, path in expected_font_uris.items()
    }

    def resolve_resource(uri: str, _rel: str | None = None) -> str:
        parsed = urlparse(str(uri))
        if parsed.scheme and parsed.scheme != "file":
            raise ApplicationError(f"Сетевой или неподдерживаемый ресурс запрещён: {uri}")
        if parsed.netloc:
            raise ApplicationError(f"Сетевой ресурс запрещён: {uri}")
        normalized_uri = unquote(parsed.path).replace("\\", "/")
        if not parsed.scheme and normalized_uri in expected_font_uris:
            resource_path = expected_font_uris[normalized_uri]
        elif parsed.scheme == "file":
            path_text = unquote(parsed.path)
            if re.match(r"^/[A-Za-z]:/", path_text):
                path_text = path_text[1:]
            resource_path = Path(path_text)
            resource_path = expected_font_paths.get(resource_path.resolve(), resource_path)
        else:
            resource_path = template_dir / normalized_uri
        resource_path = resource_path.resolve()
        allowed_font = resource_path in resolved_fonts.values()
        if not allowed_font and not resource_path.is_relative_to(template_dir):
            raise ApplicationError(f"Путь к ресурсу выходит за каталог разрешённых ресурсов: {uri}")
        if not resource_path.is_file():
            raise ApplicationError(f"Локальный ресурс не найден: {resource_path}")
        return str(resource_path)

    return resolve_resource


def create_pdf(document_html: str, pdf_path: Path, template_path: Path, fonts_dir: Path) -> None:
    """Формирует PDF из HTML через xhtml2pdf.

    Args:
        document_html: Заполненный HTML-документ.
        pdf_path: Путь создаваемого PDF.
        template_path: Путь исходного шаблона для разрешения ресурсов.
        fonts_dir: Настроенный каталог TTF-шрифтов.

    Raises:
        ApplicationError: xhtml2pdf недоступен, сообщил об ошибке или PDF повреждён.
    """
    try:
        from xhtml2pdf import files as pisa_files
        from xhtml2pdf import pisa
    except ImportError as exc:
        raise ApplicationError("Библиотека xhtml2pdf не установлена. Выполните: pip install -r requirements.txt") from exc

    temp_paths: list[Path] = []
    original_named_temp = pisa_files.tempfile.NamedTemporaryFile

    def windows_named_temp(*args, **kwargs):
        kwargs["delete"] = False
        temp_file = original_named_temp(*args, **kwargs)
        temp_paths.append(Path(temp_file.name))
        return temp_file

    with _XHTML2PDF_LOCK:
        try:
            # ponytail: xhtml2pdf 0.2.17 не переоткрывает delete=True TTF на Windows;
            # lock и workaround удалить после исправления upstream.
            if os.name == "nt":
                pisa_files.tempfile.NamedTemporaryFile = windows_named_temp
            with pdf_path.open("wb") as pdf_file:
                status = pisa.CreatePDF(
                    src=document_html,
                    dest=pdf_file,
                    encoding="utf-8",
                    path=template_path.resolve().as_uri(),
                    link_callback=create_link_callback(template_path, fonts_dir),
                )
            if status.err:
                raise ApplicationError("xhtml2pdf сообщил об ошибке преобразования HTML в PDF")
            if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
                raise ApplicationError(f"Создан пустой PDF: {pdf_path}")
            if pdf_path.read_bytes()[:5] != b"%PDF-":
                raise ApplicationError(f"Созданный файл не имеет сигнатуру PDF: {pdf_path}")
        except Exception as exc:
            cleanup_error = None
            try:
                pdf_path.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                cleanup_error = cleanup_exc
            message = str(exc) if isinstance(exc, ApplicationError) else f"Не удалось сформировать PDF через xhtml2pdf: {exc}"
            if cleanup_error:
                message += f"; неполный PDF не удалось удалить: {cleanup_error}"
            raise ApplicationError(message) from exc
        finally:
            pisa_files.tempfile.NamedTemporaryFile = original_named_temp
            try:
                pisa_files.cleanFiles()
            except Exception as cleanup_exc:
                logger.warning("Не удалось закрыть временные файлы xhtml2pdf: %s", cleanup_exc)
            for temp_path in temp_paths:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError as cleanup_exc:
                    logger.warning("Не удалось удалить временный файл xhtml2pdf %s: %s", temp_path, cleanup_exc)


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
    logger.info("Каталог шрифтов: %s", paths.fonts_dir.resolve())

    validate_template_resources(paths.template, paths.fonts_dir)
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
    started_at = time.monotonic()
    create_pdf(document_html, pdf_path, paths.template, paths.fonts_dir)
    logger.info("PDF сформирован за %.3f с", time.monotonic() - started_at)
    logger.info("PDF создан: %s", pdf_path.resolve())

    try:
        os.startfile(pdf_path.resolve())
    except OSError as exc:
        logger.warning("PDF создан, но не удалось открыть файл %s: %s", pdf_path, exc)
        sys.stderr.write(f"PDF создан: {pdf_path.resolve()}\nАвтоматическое открытие не удалось: {exc}\n")
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
