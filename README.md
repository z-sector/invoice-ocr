# Invoice OCR (FastAPI + PaddleOCR, CPU)

Веб-приложение на FastAPI: загружаете фото или PDF инвойса через браузер, PaddleOCR (PaddlePaddle, только CPU)
распознаёт текст, а парсер заполняет поля и возвращает JSON. Модель распознаёт только английский язык:

```json
{
  "vendor_name": "Blue Ridge Supply Co.",
  "invoice_number": "2026-1142",
  "amount": "2290.50",
  "currency": "USD",
  "bank_name_on_invoice": "Wells Fargo Bank, N.A.",
  "account_number_on_invoice": "4000123456789",
  "routing_on_invoice": "121000248"
}
```

Поля соответствуют датаклассу `ParsedInvoiceDS` в `app/parser.py`.

## Запуск

Зависимости управляются через [uv](https://docs.astral.sh/uv/): `pyproject.toml` + `uv.lock`. Нужен Python 3.11–3.13
(uv скачает подходящий интерпретатор сам, если его нет). Все пакеты, включая CPU-сборку `paddlepaddle`, ставятся
с PyPI — отдельный индекс не нужен (GPU-сборка — это другой пакет, `paddlepaddle-gpu`).

```bash
cd invoice-ocr
uv sync                                   # создаст .venv и установит всё из uv.lock
uv run uvicorn app.main:app --host 0.0.0.0 --port 8741
```

Откройте http://localhost:8741. При первом запуске PaddleOCR скачает модели (~140 МБ) в `~/.paddlex/official_models/`;
дальше они берутся из кэша. Модель загружается при старте сервера.

Добавить зависимость: `uv add <пакет>`; обновить lock после правки `pyproject.toml`: `uv lock`.

## Запуск в Docker

Модели скачиваются при сборке образа и лежат внутри него, поэтому контейнер стартует без доступа к сети
и ничего не докачивает.

```bash
docker build -t invoice-ocr .
docker run --rm -p 8741:8741 invoice-ocr
```

После изменения блока «Модель» в `app/config.py` образ нужно пересобрать — сборка скачает модели заново.

## Как пользоваться

1. Перетащите изображение (JPG, PNG, WEBP, BMP, TIFF) или PDF (до 15 МБ) в поле загрузки, выберите файл или вставьте изображение из буфера (Ctrl+V).
2. Нажмите «Распознать». На CPU обработка занимает 2–20 секунд на страницу.
3. Справа появится JSON, таблица полей с указанием строки OCR, из которой взято значение, и полный распознанный текст.

## API

`POST /api/parse` — `multipart/form-data` с полем `file` (изображение или PDF).

```bash
curl -s -F "file=@invoice.jpg" http://localhost:8741/api/parse
curl -s -F "file=@invoice.pdf" http://localhost:8741/api/parse
```

Ответ:

```json
{
  "invoice": { "...": "поля ParsedInvoiceDS" },
  "sources": { "amount": "Amount Due: $2,290.50", "...": "строка OCR для каждого найденного поля" },
  "ocr": { "lines": ["..."], "line_count": 14, "page_count": 1, "pages_in_file": 1, "mean_confidence": 0.99, "ocr_ms": 2000, "total_ms": 2010, "engine": "PaddleOCR (CPU, lang=en)" },
  "file": { "name": "invoice.jpg", "size": 123456, "content_type": "image/jpeg", "kind": "image" }
}
```

Документация OpenAPI: `/docs`. Проверка состояния: `GET /health`.

## Настройки

Настройки OCR — константы в `app/config.py`; чтобы изменить значение, отредактируйте файл и перезапустите сервер.

| Константа | Значение | Назначение |
| --- | --- | --- |
| `OCR_LANG` | `"en"` | Язык модели. Сервис рассчитан только на английский |
| `OCR_DEVICE` | `"cpu"` | Устройство, на котором работает PaddleOCR |
| `USE_DOC_ORIENTATION_CLASSIFY` | `False` | Определять поворот всего документа (отдельная модель) |
| `USE_DOC_UNWARPING` | `False` | Выпрямлять изогнутые страницы (отдельная модель) |
| `USE_TEXTLINE_ORIENTATION` | `True` | Определять ориентацию отдельных строк |
| `PRELOAD_MODEL` | `True` | Загружать модель при старте сервера, а не при первом запросе |
| `MAX_IMAGE_SIDE` | `2200` | Большие фото и страницы PDF уменьшаются до этой длины по большей стороне |
| `PDF_RENDER_DPI` | `170` | Разрешение, с которым страницы PDF рисуются перед распознаванием |
| `MAX_PDF_PAGES` | `10` | Сколько первых страниц PDF распознавать; остальные пропускаются, счётчик `pages_in_file` показывает полный объём |

Лимит размера загружаемого файла задаётся переменной окружения `MAX_UPLOAD_MB` (по умолчанию `15`).

## Как работает разбор

`app/ocr.py` запускает PaddleOCR (`device="cpu"`, детекция + классификация ориентации строк + распознавание) и возвращает
текстовые блоки с координатами. PDF сначала растеризуется через `pypdfium2` (страница за страницей, до `MAX_PDF_PAGES`);
координаты следующих страниц сдвигаются вниз, и документ разбирается как один. `app/parser.py` группирует блоки в строки по вертикальному положению, после чего
правилами ищет:

- `invoice_number` — метки `Invoice No / # / № / N°`, `Счет на оплату №`, `Facture N°` и т. п.;
- `amount` и `currency` — строки `Total due / Amount due / Grand total / Итого к оплате`, затем любые `Total`,
  исключая `Subtotal`, `Tax/VAT/НДС`; при отсутствии меток берётся максимальная денежная сумма; валюта по символу
  или коду (`$`, `€`, `USD`, `руб.` …), сумма нормализуется к виду `1234.56`;
- `bank_name_on_invoice` — метки `Bank / Bank name / Beneficiary bank / Банк получателя`, список известных банков,
  фразы вида `… Bank, N.A.`;
- `account_number_on_invoice` — `Account # / Acct / A/C / IBAN / Р/с`;
- `routing_on_invoice` — `Routing / ABA / БИК`, затем `Sort code / SWIFT / BIC / IFSC / BSB`;
- `vendor_name` — метки `From / Remit to / Beneficiary / Поставщик`, иначе самый крупный текст в верхней части
  страницы с учётом юридических форм (`LLC`, `Ltd.`, `GmbH`, `ООО` …) и исключением блока `Bill To`.

Поля, которые не удалось найти, остаются пустыми строками.
