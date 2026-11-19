"""Heuristic extraction of invoice fields from positioned OCR text."""

from __future__ import annotations

import re
import statistics
from dataclasses import asdict, dataclass, field

from .ocr import OcrResult, TextSegment


@dataclass
class ParsedInvoiceDS:
    vendor_name: str = ""
    invoice_number: str = ""
    amount: str = ""
    currency: str = ""
    bank_name_on_invoice: str = ""
    account_number_on_invoice: str = ""
    routing_on_invoice: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class Line:
    """Text boxes that share a row, merged left-to-right."""

    cells: list[TextSegment]
    text: str
    y: float
    height: float
    index: int = 0


@dataclass
class ParseOutput:
    invoice: ParsedInvoiceDS
    lines: list[Line]
    # Which OCR line each field came from, returned to the client as evidence.
    sources: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Row grouping
# ---------------------------------------------------------------------------


def group_lines(result: OcrResult) -> list[Line]:
    segments = sorted(result.segments, key=lambda s: (s.cy, s.x0))
    if not segments:
        return []
    median_h = statistics.median(s.height for s in segments) or 1.0

    rows: list[list[TextSegment]] = []
    for seg in segments:
        if rows:
            row = rows[-1]
            row_cy = sum(s.cy for s in row) / len(row)
            row_h = max(s.height for s in row)
            tolerance = 0.5 * min(row_h, max(seg.height, median_h))
            if abs(seg.cy - row_cy) <= tolerance:
                row.append(seg)
                continue
        rows.append([seg])

    lines: list[Line] = []
    for idx, row in enumerate(rows):
        row.sort(key=lambda s: s.x0)
        text = " ".join(s.text for s in row)
        lines.append(
            Line(
                cells=row,
                text=_clean_spaces(text),
                y=sum(s.cy for s in row) / len(row),
                height=max(s.height for s in row),
                index=idx,
            )
        )
    return lines


def _clean_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Shared patterns
# ---------------------------------------------------------------------------

SEP = r"[\s:#№.\-–—]*"
CURRENCY_CODES = (
    "USD|EUR|GBP|RUB|BYN|UAH|KZT|CAD|AUD|NZD|CHF|PLN|CZK|SEK|NOK|DKK|CNY|JPY|INR|HKD|SGD|AED|TRY|ILS|ZAR|BRL|MXN"
)
# OCR often mixes Latin look-alikes into Cyrillic ("pу6."), so the Russian words tolerate them.
CURRENCY_WORDS = r"[рp][уy][б6](?:\.|лей|ля|ль)?|грн\.?|тг\.?|р\.|zł|Kč|kr\.?"
CURRENCY_SYMBOLS = r"[$€£₽¥₴₸₹]"
CURRENCY_ANY = rf"(?:{CURRENCY_SYMBOLS}|\b(?:{CURRENCY_CODES})\b|\b(?:{CURRENCY_WORDS})(?!\w))"

SYMBOL_TO_CODE = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "₽": "RUB",
    "¥": "JPY",
    "₴": "UAH",
    "₸": "KZT",
    "₹": "INR",
    "zł": "PLN",
    "kč": "CZK",
    "kr": "SEK",
    "руб": "RUB",
    "р.": "RUB",
    "грн": "UAH",
    "тг": "KZT",
}

NUMBER = r"\d{1,3}(?:[ ,.'’]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
MONEY_RE = re.compile(
    rf"(?P<cur1>{CURRENCY_ANY})?\s*(?P<num>{NUMBER})(?!\d)\s*(?P<cur2>{CURRENCY_ANY})?",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"\b\d{1,4}[./-]\d{1,2}[./-]\d{1,4}\b")


# ---------------------------------------------------------------------------
# Invoice number
# ---------------------------------------------------------------------------

INVOICE_WORD = (
    r"(?:tax\s+)?(?:invoice|inv|bill|receipt|facture|rechnung|factura|fattura|faktura|сч[её]т(?:[- ]?фактура)?|инвойс|рахунок)"
    r"(?:\s+(?:на\s+оплату|for\s+payment|of))?"
)
NUMBER_WORD = r"(?:no\.?|number|num\.?|nr\.?|ref\.?|reference|id|#|№|n[°º])"
INVOICE_LABEL_RE = re.compile(rf"\b{INVOICE_WORD}\s*\.?\s*{NUMBER_WORD}", re.IGNORECASE)
INVOICE_LOOSE_RE = re.compile(rf"\b{INVOICE_WORD}\b", re.IGNORECASE)
ID_VALUE_RE = re.compile(r"(?=[A-Za-z0-9/._-]*\d)[A-Za-z0-9№#]?[A-Za-z0-9/._-]{2,}")
BARE_HASH_RE = re.compile(r"(?:#|№|\bn[°º])\s*([A-Za-z0-9][A-Za-z0-9/._-]{1,})", re.IGNORECASE)


def _valid_invoice_id(value: str) -> bool:
    value = value.strip(".-_/#№ ")
    if len(value) < 2 or len(value) > 40:
        return False
    if DATE_RE.fullmatch(value):
        return False
    if not any(ch.isdigit() for ch in value):
        return False
    return True


def extract_invoice_number(lines: list[Line]) -> tuple[str, str]:
    for line in lines:
        m = INVOICE_LABEL_RE.search(line.text)
        if not m:
            continue
        rest = line.text[m.end() :].lstrip(" :#№.-–—")
        candidate = _first_id(rest)
        if candidate:
            return candidate, line.text
        nxt = _next_line(lines, line.index)
        if nxt:
            candidate = _first_id(nxt.text)
            if candidate and len(nxt.text) <= 40:
                return candidate, nxt.text

    for line in lines:
        m = BARE_HASH_RE.search(line.text)
        if m and _valid_invoice_id(m.group(1)) and not re.search(r"account|acct|routing|order|po\b|p\.o\.", line.text, re.I):
            return m.group(1).strip(".-_/"), line.text

    for line in lines:
        m = INVOICE_LOOSE_RE.search(line.text)
        if not m or re.search(r"\bdate\b|дата", line.text, re.I):
            continue
        rest = line.text[m.end() :].lstrip(" :#№.-–—")
        candidate = _first_id(rest)
        if candidate:
            return candidate, line.text
    return "", ""


def _first_id(text: str) -> str:
    m = ID_VALUE_RE.match(text.strip())
    if m and _valid_invoice_id(m.group(0)):
        return m.group(0).strip(".-_/#№")
    return ""


def _next_line(lines: list[Line], index: int) -> Line | None:
    return lines[index + 1] if index + 1 < len(lines) else None


# ---------------------------------------------------------------------------
# Amount and currency
# ---------------------------------------------------------------------------

TOTAL_STRONG_RE = re.compile(
    r"total\s*(?:amount\s*)?due|amount\s*due|balance\s*due|grand\s*total|total\s*amount|amount\s*payable|"
    r"total\s*payable|please\s*pay|итого\s*к\s*оплате|всего\s*к\s*оплате|к\s*оплате|сумма\s*к\s*оплате|total\s*to\s*pay|net\s*payable",
    re.IGNORECASE,
)
TOTAL_WEAK_RE = re.compile(
    r"\btotal\b|\bитого\b|\bвсего\b|\bсумма\b|\bamount\b|\bgesamt(?:betrag)?\b|\bmontant\b|\bimporte\b|\btotale\b|\bрazem\b|\bsum\b",
    re.IGNORECASE,
)
SUBTOTAL_RE = re.compile(r"sub[\s-]?total|промежуточ|без\s*ндс|net\s*amount", re.IGNORECASE)
TAX_RE = re.compile(r"\b(?:tax|vat|gst|hst|ндс|пдв|discount|скидка|shipping|deposit|paid|оплачено)\b", re.IGNORECASE)


def _parse_number(raw: str) -> float | None:
    s = raw.replace(" ", "").replace("'", "").replace("’", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = head.replace(",", "") + "." + tail if len(tail) != 3 or "," in head else s.replace(",", "")
    elif "." in s:
        head, _, tail = s.rpartition(".")
        s = head.replace(".", "") + "." + tail if len(tail) != 3 or "." in head else s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def _format_amount(value: float) -> str:
    return f"{value:.2f}"


def _money_candidates(text: str) -> list[tuple[float, str, str]]:
    """Return (value, raw_number, currency_code) for money-like numbers in text."""
    out: list[tuple[float, str, str]] = []
    masked = DATE_RE.sub(lambda m: " " * len(m.group(0)), text)
    for m in MONEY_RE.finditer(masked):
        raw = m.group("num")
        currency = _currency_code(m.group("cur1") or m.group("cur2") or "")
        digits = re.sub(r"\D", "", raw)
        if len(digits) > 12 or not digits:
            continue
        has_decimals = bool(re.search(r"[.,]\d{1,2}$", raw))
        has_groups = bool(re.search(r"\d[ ,.'’]\d{3}", raw))
        # A bare integer without a currency is more likely a quantity, date part or ID.
        if not (currency or has_decimals or has_groups):
            continue
        if len(digits) >= 7 and not (has_decimals or has_groups):
            continue
        value = _parse_number(raw)
        if value is None or value <= 0:
            continue
        out.append((value, raw, currency))
    return out


def _currency_code(token: str) -> str:
    token = token.strip()
    if not token:
        return ""
    upper = token.upper()
    if re.fullmatch(CURRENCY_CODES, upper):
        return upper
    lowered = token.lower().rstrip(".")
    if re.fullmatch(r"[рp][уy][б6](?:лей|ля|ль)?", lowered):
        return "RUB"
    for key, code in SYMBOL_TO_CODE.items():
        if lowered == key.rstrip(".") or token == key:
            return code
    return ""


def extract_amount(lines: list[Line]) -> tuple[str, str, str]:
    """Return (amount, currency_hint, source_line)."""
    best: tuple[int, float, int, str, str, str] | None = None  # score, value, index, amount, currency, source

    def consider(score: int, line: Line, source: Line) -> None:
        nonlocal best
        for value, _raw, currency in _money_candidates(line.text):
            key = (score, value, line.index, _format_amount(value), currency, source.text)
            if best is None or key[:3] > best[:3]:
                best = key

    for line in lines:
        text = line.text
        if SUBTOTAL_RE.search(text):
            continue
        strong = TOTAL_STRONG_RE.search(text)
        weak = TOTAL_WEAK_RE.search(text)
        if not strong and not weak:
            continue
        if not strong and TAX_RE.search(text):
            continue
        score = 100 if strong else 60
        if _money_candidates(text):
            consider(score, line, line)
        else:
            nxt = _next_line(lines, line.index)
            if nxt and len(nxt.text) <= 30 and _money_candidates(nxt.text):
                consider(score - 5, nxt, line)

    if best is None:
        for line in lines:
            if TAX_RE.search(line.text) or SUBTOTAL_RE.search(line.text):
                continue
            for value, _raw, currency in _money_candidates(line.text):
                if not currency and not re.search(r"[.,]\d{2}\b", line.text):
                    continue
                key = (0, value, line.index, _format_amount(value), currency, line.text)
                if best is None or key[:3] > best[:3]:
                    best = key

    if best is None:
        return "", "", ""
    return best[3], best[4], best[5]


CURRENCY_LABEL_RE = re.compile(rf"(?:currency|валюта)\s*[:\-]?\s*({CURRENCY_ANY})", re.IGNORECASE)
CURRENCY_SCAN_RE = re.compile(CURRENCY_ANY, re.IGNORECASE)


def extract_currency(lines: list[Line], hint: str) -> tuple[str, str]:
    if hint:
        return hint, ""
    for line in lines:
        m = CURRENCY_LABEL_RE.search(line.text)
        if m:
            code = _currency_code(m.group(1))
            if code:
                return code, line.text
    counts: dict[str, int] = {}
    sources: dict[str, str] = {}
    for line in lines:
        for m in CURRENCY_SCAN_RE.finditer(line.text):
            code = _currency_code(m.group(0))
            if code:
                counts[code] = counts.get(code, 0) + 1
                sources.setdefault(code, line.text)
    if counts:
        code = max(counts, key=counts.get)
        return code, sources[code]
    return "", ""


# ---------------------------------------------------------------------------
# Bank details
# ---------------------------------------------------------------------------

BANK_LABEL_RE = re.compile(
    r"\b(?:bank\s*name|beneficiary(?:'s)?\s*bank(?:\s*name)?|receiving\s*bank|payee\s*bank|bank|banque|banco|banca|"
    r"банк(?:\s*(?:получателя|бенефициара))?|наименование\s*банка|назва\s*банку)\s*[:\-–—]\s*(.+)",
    re.IGNORECASE,
)
KNOWN_BANKS_RE = re.compile(
    r"\b(?:JP\s?Morgan(?:\s+Chase)?|Chase|Bank\s+of\s+America|Wells\s+Fargo|Citibank|Citi\b|U\.?S\.?\s+Bank|PNC|Truist|Capital\s+One|"
    r"TD\s+Bank|HSBC|Barclays|Lloyds|NatWest|Santander|Deutsche\s+Bank|Commerzbank|BNP\s+Paribas|Soci[eé]t[eé]\s+G[eé]n[eé]rale|"
    r"ING\s+Bank|Rabobank|UBS|Credit\s+Suisse|Royal\s+Bank\s+of\s+Canada|RBC|Scotiabank|BMO|CIBC|Silicon\s+Valley\s+Bank|"
    r"Mercury|Brex|Wise|Revolut|Payoneer|Goldman\s+Sachs|Morgan\s+Stanley|Fifth\s+Third|KeyBank|Regions\s+Bank|Citizens\s+Bank|"
    r"Huntington|M&T\s+Bank|Ally\s+Bank|Standard\s+Chartered|Nordea|Swedbank|Danske\s+Bank|DBS|OCBC|UOB|"
    r"Сбербанк|Сбер|Тинькофф|Т-Банк|Альфа[- ]Банк|ВТБ|Газпромбанк|Райффайзен(?:банк)?|Беларусбанк|Приорбанк|Белагропромбанк|"
    r"Белинвестбанк|БПС-Сбербанк|Монобанк|ПриватБанк|Ощадбанк|Kaspi|Халык)\b",
    re.IGNORECASE,
)
GENERIC_BANK_PHRASE_RE = re.compile(
    r"((?:[A-ZА-ЯЁ][\w&.'’-]*\s+){0,4}(?:Bank|Банк)(?:\s+(?:of|and|&|für|de)\s+[A-ZА-ЯЁ][\w.'’-]*)*(?:,?\s*N\.?A\.?)?(?:\s+(?:plc|PLC|AG|SA|S\.A\.|NV|N\.V\.|Ltd\.?|Limited|Inc\.?))?)",
)
BANK_PHRASE_STOP_RE = re.compile(
    r"\b(?:name|details|information|info|instructions|transfer|wire|account|acct|routing|statement|charges|fees|"
    r"реквизиты|перевод|счет|счёт)\b",
    re.IGNORECASE,
)


def extract_bank_name(lines: list[Line]) -> tuple[str, str]:
    for line in lines:
        m = BANK_LABEL_RE.search(line.text)
        if m:
            value = _cut_at_next_label(m.group(1))
            if _has_letters(value):
                return value, line.text
            nxt = _next_line(lines, line.index)
            if nxt and _has_letters(nxt.text) and ":" not in nxt.text and len(nxt.text) <= 60:
                return nxt.text, nxt.text
        elif re.search(r"\b(?:bank\s*name|beneficiary\s*bank|банк)\b\s*$", line.text, re.I):
            nxt = _next_line(lines, line.index)
            if nxt and _has_letters(nxt.text) and ":" not in nxt.text and len(nxt.text) <= 60:
                return nxt.text, line.text + " / " + nxt.text

    for line in lines:
        m = KNOWN_BANKS_RE.search(line.text)
        if not m:
            continue
        phrase = _expand_bank_phrase(line.text, m)
        return phrase, line.text

    for line in lines:
        if re.search(r"\baccount|acct|routing|swift|iban|маршрут", line.text, re.I):
            continue
        for m in GENERIC_BANK_PHRASE_RE.finditer(line.text):
            phrase = m.group(1).strip(" ,.-")
            if BANK_PHRASE_STOP_RE.search(phrase) or len(phrase) < 5:
                continue
            return phrase, line.text
    return "", ""


def _expand_bank_phrase(text: str, m: re.Match) -> str:
    # Grow the match to include trailing words like "Bank, N.A." or "Bank Limited".
    tail = text[m.end() :]
    ext = re.match(r"(?:\s+(?:Bank|Банк)\b)?(?:,?\s*N\.?A\.?)?(?:\s+(?:plc|PLC|AG|SA|S\.A\.|NV|N\.V\.|Ltd\.?|Limited|Inc\.?))?", tail)
    phrase = m.group(0) + (ext.group(0) if ext else "")
    return _tidy(phrase)


def _cut_at_next_label(value: str) -> str:
    # "Chase Bank Account: 123" -> "Chase Bank"
    value = re.split(r"\s+(?=[A-Za-zА-Яа-яЁё/ .]{2,30}\s*[:№#])", value, maxsplit=1)[0]
    return _tidy(value)


def _tidy(value: str) -> str:
    """Trim punctuation but keep a trailing period that belongs to an abbreviation (Inc., Co., N.A.)."""
    value = value.strip(" ,;:-–—")
    if value.endswith(".") and not re.search(r"(?:\b\w{1,4}|\.\w)\.$", value):
        value = value[:-1].rstrip(" ,;")
    return value


def _has_letters(text: str) -> bool:
    return len(re.findall(r"[A-Za-zА-Яа-яЁё]", text)) >= 3


ACCOUNT_LABEL_RE = re.compile(
    r"\b(?:account\s*(?:no\.?|number|num\.?|#|№)|acct\.?\s*(?:no\.?|number|#|№)?|a/?c(?:\s*(?:no\.?|number|#|№))?|iban|"
    r"beneficiary(?:'s)?\s*(?:account|acct)(?:\s*(?:no\.?|number|#|№))?|account|"
    r"расч[её]тный\s*сч[её]т|[рp]/[сc]ч?\.?|номер\s*сч[её]та|сч[её]т\s*(?:получателя)?\s*№?|рахунок|IBAN)"
    r"\s*[:#№.\-–—]*\s*(.*)",
    re.IGNORECASE,
)
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?\b")
ACCOUNT_VALUE_RE = re.compile(r"\d(?:[\d \-]{4,32})\d")


def extract_account_number(lines: list[Line]) -> tuple[str, str]:
    for line in lines:
        m = ACCOUNT_LABEL_RE.search(line.text)
        if not m:
            continue
        rest = m.group(1)
        value = _account_value(rest)
        if value:
            return value, line.text
        if not rest.strip() or len(rest.strip()) <= 3:
            nxt = _next_line(lines, line.index)
            if nxt:
                value = _account_value(nxt.text)
                if value and len(nxt.text) <= 45:
                    return value, nxt.text

    for line in lines:
        m = IBAN_RE.search(line.text)
        if m:
            return m.group(0).replace(" ", ""), line.text
    return "", ""


def _account_value(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    if re.match(r"(?:name|holder|title|type|владел|наимен)", text, re.I):
        return ""
    m = IBAN_RE.match(text)
    if m:
        return m.group(0).replace(" ", "")
    m = ACCOUNT_VALUE_RE.match(text)
    if m:
        digits = m.group(0).replace(" ", "")
        if 6 <= len(re.sub(r"\D", "", digits)) <= 34:
            return digits
    return ""


ROUTING_LABEL_RE = re.compile(
    r"\b(?:(?:ach\s*|wire\s*|fedwire\s*)?routing\s*(?:transit\s*)?(?:no\.?|number|num\.?|#|№)?|aba(?:\s*(?:no\.?|number|routing|#))?|rtn|"
    r"transit\s*(?:no\.?|number|#)?|sort\s*code|swift(?:\s*/\s*bic)?(?:\s*code)?|bic(?:\s*/\s*swift)?(?:\s*code)?|бик|ifsc(?:\s*code)?|bsb|"
    r"clabe|institution\s*(?:no\.?|number)|bank\s*code|МФО)"
    r"\s*[:#№.\-–—]*\s*(.*)",
    re.IGNORECASE,
)
ROUTING_US_RE = re.compile(r"\b\d{9}\b|\b\d{3}[ -]?\d{3}[ -]?\d{3}\b")
SORT_CODE_RE = re.compile(r"\b\d{2}[- ]\d{2}[- ]\d{2}\b")
SWIFT_RE = re.compile(r"\b[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b")
GENERIC_CODE_RE = re.compile(r"\b[A-Z0-9]{4,11}\b")


def extract_routing(lines: list[Line]) -> tuple[str, str]:
    fallback: tuple[str, str] = ("", "")
    for line in lines:
        m = ROUTING_LABEL_RE.search(line.text)
        if not m:
            continue
        label = m.group(0)[: m.start(1) - m.start(0)].lower()
        rest = m.group(1).strip()
        value = _routing_value(rest)
        if not value and len(rest) <= 3:
            nxt = _next_line(lines, line.index)
            if nxt and len(nxt.text) <= 30:
                value = _routing_value(nxt.text)
                if value:
                    line = nxt
        if not value:
            continue
        if re.search(r"routing|aba|rtn|бик", label):
            return value, line.text
        if not fallback[0]:
            fallback = (value, line.text)
    return fallback


def _routing_value(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    for pattern in (ROUTING_US_RE, SORT_CODE_RE, SWIFT_RE):
        m = pattern.match(text)
        if m:
            value = m.group(0)
            return re.sub(r"[ -]", "", value) if pattern is ROUTING_US_RE else value
    m = GENERIC_CODE_RE.match(text)
    if m and any(ch.isdigit() for ch in m.group(0)):
        return m.group(0)
    return ""


# ---------------------------------------------------------------------------
# Vendor name
# ---------------------------------------------------------------------------

VENDOR_LABEL_RE = re.compile(
    r"\b(?:from|vendor(?:\s*name)?|seller|supplier|sold\s*by|issued\s*by|remit\s*to|pay\s*to|payable\s*to|make\s*(?:checks?|payment)\s*payable\s*to|"
    r"beneficiary(?:\s*name)?|account\s*(?:holder|name)|company(?:\s*name)?|поставщик|продавец|исполнитель|получатель(?:\s*платежа)?|бенефициар|"
    r"постачальник|компания)\s*[:\-–—]\s*(.+)",
    re.IGNORECASE,
)
COMPANY_SUFFIX_RE = re.compile(
    r"(?:\b(?:LLC|L\.L\.C\.|Inc\.?|Incorporated|Ltd\.?|Limited|LLP|L\.P\.|PLC|Corp\.?|Corporation|Company|GmbH|S\.?A\.?R\.?L\.?|S\.?R\.?L\.?|"
    r"S\.?p\.?A\.?|B\.?V\.?|N\.?V\.?|Oy|Pty\.?|Pte\.?|ApS|A/S|AG|SE|KG|ООО|ОАО|ЗАО|ПАО|АО|ИП|ТОВ|ФОП|УП|ОДО|ЧУП|ИООО|СООО|ПП|ТДВ|ЧП)(?=[\s,.)]|$))|\bCo\.(?=\s|$)"
)
GENERIC_WORDS_RE = re.compile(
    r"\b(?:invoice|tax\s+invoice|receipt|statement|bill\s*to|ship\s*to|sold\s*to|customer|client|buyer|date|due|total|subtotal|page|"
    r"payment|description|qty|quantity|amount|price|unit|balance|terms|thank\s+you|purchase\s+order|p\.?o\.?\s*(?:no|number|#)|"
    r"сч[её]т|фактура|инвойс|квитанция|покупатель|заказчик|плательщик|дата|итого|всего|сумма|оплата|наименование|кол-во|цена)\b",
    re.IGNORECASE,
)
CUSTOMER_BLOCK_RE = re.compile(
    r"\b(?:bill\s*to|billed\s*to|ship\s*to|sold\s*to|invoice\s*to|customer|client|buyer|attn|attention|покупатель|заказчик|плательщик|грузополучатель)\b",
    re.IGNORECASE,
)
EMAIL_URL_PHONE_RE = re.compile(r"@|https?://|www\.|\.(?:com|net|org|io|ru|by|ua)\b|\+?\d[\d\s().-]{8,}\d", re.IGNORECASE)


def extract_vendor_name(lines: list[Line], page_height: float) -> tuple[str, str]:
    for line in lines:
        m = VENDOR_LABEL_RE.search(line.text)
        if m and not CUSTOMER_BLOCK_RE.search(line.text[: m.start()]):
            value = _cut_at_next_label(m.group(1))
            if _looks_like_name(value):
                return value, line.text
            if not value.strip():
                nxt = _next_line(lines, line.index)
                if nxt and _looks_like_name(nxt.text):
                    return nxt.text, line.text + " / " + nxt.text

    if not lines:
        return "", ""
    median_h = statistics.median(line.height for line in lines) or 1.0
    customer_zone: set[int] = set()
    for line in lines:
        if CUSTOMER_BLOCK_RE.search(line.text):
            customer_zone.update(range(line.index, line.index + 4))

    # Candidates are whole rows and, when a row has several boxes (e.g. "Acme Ltd" | "INVOICE"), each box on its own.
    candidates: list[tuple[str, float, float, Line]] = []
    for line in lines:
        candidates.append((line.text, line.height, line.y, line))
        if len(line.cells) > 1:
            for cell in line.cells:
                candidates.append((cell.text, cell.height, cell.cy, line))

    best_score = -1e9
    best: tuple[str, Line] | None = None
    for text, height, y, line in candidates:
        text = _clean_spaces(text)
        if not _looks_like_name(text):
            continue
        score = min(40.0, (height / median_h - 1.0) * 30.0)
        if COMPANY_SUFFIX_RE.search(text):
            score += 35.0
        if GENERIC_WORDS_RE.search(text) or ":" in text:
            score -= 100.0
        if EMAIL_URL_PHONE_RE.search(text):
            score -= 60.0
        if _money_candidates(text):
            score -= 60.0
        elif re.search(r"\d", text):
            score -= 25.0
        if line.index in customer_zone:
            score -= 50.0
        if page_height:
            score -= (y / page_height) * 40.0
        if len(text) > 60:
            score -= 30.0
        if score > best_score:
            best_score = score
            best = (text, line)

    if best is not None and best_score > -40:
        return _strip_company_noise(best[0]), best[1].text
    return "", ""


def _looks_like_name(text: str) -> bool:
    text = text.strip()
    if len(text) < 3 or len(text) > 80:
        return False
    letters = len(re.findall(r"[A-Za-zА-Яа-яЁё]", text))
    if letters < 3 or letters / max(len(text), 1) < 0.5:
        return False
    if text[0].isdigit() or DATE_RE.search(text):
        return False
    return True


def _strip_company_noise(text: str) -> str:
    # Drop trailing address fragments after the legal suffix, e.g. "Acme LLC 123 Main St" -> "Acme LLC".
    m = COMPANY_SUFFIX_RE.search(text)
    if m:
        return _tidy(text[: m.end()])
    return _tidy(text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_invoice(result: OcrResult) -> ParseOutput:
    lines = group_lines(result)
    invoice = ParsedInvoiceDS()
    sources: dict[str, str] = {}

    invoice.vendor_name, src = extract_vendor_name(lines, float(result.image_height))
    if src:
        sources["vendor_name"] = src

    invoice.invoice_number, src = extract_invoice_number(lines)
    if src:
        sources["invoice_number"] = src

    invoice.amount, currency_hint, src = extract_amount(lines)
    if src:
        sources["amount"] = src

    invoice.currency, src = extract_currency(lines, currency_hint)
    if invoice.currency:
        sources["currency"] = src or sources.get("amount", "")

    invoice.bank_name_on_invoice, src = extract_bank_name(lines)
    if src:
        sources["bank_name_on_invoice"] = src

    invoice.account_number_on_invoice, src = extract_account_number(lines)
    if src:
        sources["account_number_on_invoice"] = src

    invoice.routing_on_invoice, src = extract_routing(lines)
    if src:
        sources["routing_on_invoice"] = src

    return ParseOutput(invoice=invoice, lines=lines, sources=sources)
