# Формирование заявления на возврат

Windows CLI-приложение, которое заполняет HTML-шаблон данными возврата из JSON,
формирует заявление в PDF через xhtml2pdf и открывает готовый документ системной
программой просмотра.

## Требования

- Windows 10 или Windows 11
- Python 3.10+
- каталог `fonts` с файлами `DejaVuSerif.ttf` и `DejaVuSerif-Bold.ttf`

Браузер, Microsoft Word и внешние PDF-конвертеры не требуются.

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Настройка

Параметры задаются в `config.ini`:

```ini
[paths]
template = templates/refund_application_template_xhtml2pdf.html
logs_dir = logs
pdf_output_dir = output
```

- `template` — путь к HTML-шаблону заявления;
- `logs_dir` — каталог технических журналов;
- `pdf_output_dir` — каталог готовых PDF.

Можно использовать абсолютные пути. Относительные пути разрешаются относительно
каталога, в котором находится `config.ini`. Отсутствующие каталоги журналов и PDF
создаются автоматически.

## Формат JSON

```json
{
  "date": "20260720",
  "org": "ООО \"Клевер Урал\"",
  "recipient": "Генеральному директору ООО \"Клевер Урал\"",
  "address": "Тольятти, Автозаводское шоссе, 6",
  "items": [
    {
      "name": "Наименование товара",
      "price": 2932
    }
  ]
}
```

`date` — дата покупки в формате `YYYYMMDD`. `items` должен содержать хотя бы один
товар. Цена должна быть неотрицательным целым числом, а общая сумма — больше нуля.
Поддерживаются JSON-файлы в UTF-8, UTF-8 BOM и Windows-1251.

## Запуск

```powershell
python refund_application_generator.py "путь\к\файлу.json"
```

После успешного запуска программа:

1. проверит конфигурацию и JSON;
2. безопасно заполнит `templates/refund_application_template_xhtml2pdf.html`;
3. рассчитает сумму всех `items[].price`;
4. сохранит PDF в `pdf_output_dir`;
5. запишет технический журнал в `logs_dir`;
6. откроет PDF назначенной программой Windows.

Имя результата:

```text
Заявление_на_возврат_<имя_JSON>_<YYYY-MM-DD_HH-MM-SS>.pdf
```

Повторный запуск не перезаписывает существующий файл.

## Проверка

```powershell
python -m unittest discover -s tests -v
python -m py_compile refund_application_generator.py tests/test_refund_application_generator.py
```

## Типовые ошибки

- `Библиотека xhtml2pdf не установлена` — выполните
  `python -m pip install -r requirements.txt`.
- `Файл шрифта не найден` — проверьте наличие обоих TTF-файлов в `fonts`.
- `Не удалось сформировать PDF через xhtml2pdf` — проверьте шаблон и локальные шрифты.
- `HTML-шаблон не найден` — проверьте параметр `template` в `config.ini`.
- Ошибка поля JSON — проверьте обязательные поля и формат даты `YYYYMMDD`.

Актуальное техническое задание находится в
`ТЗ_переработка_генератора_PDF_под_xhtml2pdf.md`.
