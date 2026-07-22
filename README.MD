# Формирование заявления на возврат

Windows CLI-приложение, которое заполняет HTML-шаблон данными возврата из JSON,
формирует заявление в PDF через Chromium и открывает готовый документ системной
программой просмотра.

## Требования

- Windows 10 или Windows 11
- Python 3.10+
- Chromium, установленный через Playwright

Microsoft Word не требуется и не используется.

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Настройка

Параметры задаются в `config.ini`:

```ini
[paths]
template = templates/refund_application_template.html
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
2. безопасно заполнит `templates/refund_application_template.html`;
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
python -m unittest -v
python -m py_compile refund_application_generator.py test_refund_application_generator.py
```

## Типовые ошибки

- `Библиотека Playwright не установлена` — выполните
  `python -m pip install -r requirements.txt`.
- `Не удалось сформировать PDF через Chromium` — выполните
  `python -m playwright install chromium`.
- `HTML-шаблон не найден` — проверьте параметр `template` в `config.ini`.
- Ошибка поля JSON — проверьте обязательные поля и формат даты `YYYYMMDD`.

Полное техническое задание находится в
`ТЗ_формирование_заявления_на_возврат.md`.
