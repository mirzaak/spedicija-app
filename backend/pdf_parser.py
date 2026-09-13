"""
PDF invoice parser.
Uses Claude AI (vision/PDF) to extract invoice items reliably.
Falls back to text-based extraction if API unavailable.
"""
import os
import re
import base64
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger("pdf_parser")

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None

try:
    import pytesseract
    from PIL import Image
    import io
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False


@dataclass
class InvoiceItem:
    name: str
    quantity: float = 0.0
    unit_price: float = 0.0
    total_value: float = 0.0
    gross_weight: float = 0.0
    net_weight: float = 0.0
    country_origin: str = "CN"
    tariff_code: str = ""
    currency: str = "EUR"


PRICE_PATTERN = re.compile(r"[\d,]+\.?\d*")
COUNTRY_PATTERN = re.compile(r"\b(CN|US|DE|IT|FR|GB|TR|PL|CZ|HR|RS|SI|SK|HU|RO|AT|NL|BE)\b")


def _parse_number(s: str) -> float:
    s = s.strip().replace(",", "").replace(" ", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _extract_currency(text: str) -> str:
    if re.search(r"\bEUR\b|\b€\b", text):
        return "EUR"
    if re.search(r"\bUSD\b|\b\$\b", text):
        return "USD"
    if re.search(r"\bBAM\b", text):
        return "BAM"
    return "EUR"


def _parse_with_claude(filepath: str) -> tuple[list[InvoiceItem], str]:
    """
    Send PDF to Claude API for invoice extraction.
    Returns (items, currency).
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    import anthropic

    with open(filepath, "rb") as f:
        pdf_data = base64.standard_b64encode(f.read()).decode("utf-8")

    client = anthropic.Anthropic(api_key=api_key)

    prompt = """Analiziraj ovu fakturu i izvuci sve stavke robe. Faktura može biti na bilo kojem jeziku (turski, engleski, kineski, njemački, itd.).

Za svaku stavku robe vrati JSON objekt sa poljima:
- "name": OBAVEZNO prevedi naziv na BOSANSKI jezik i ispiši VELIKIM SLOVIMA, max 4 riječi. Primjeri prevoda: turski "YAĞ FİLTRE" → "FILTER ULJA", "KAPI TOZ LASTİĞİ" → "GUMENA TRAKA VRATA", "EGZOZ SUSTURUCU" → "PRIGUŠIVAČ ISPUŠNIH PLINOVA", "SİLECEK KOLU" → "BRISAČ VJETROBRANA", engleski "wiper blade" → "BRISAČ VJETROBRANA", kineski → bosanski ekvivalent. Budi koncizan i generaliziraj.
- "quantity": količina (broj, decimalni)
- "unit_price": jedinična cijena (broj)
- "total_value": ukupna vrijednost stavke (broj)
- "gross_weight": bruto težina u kg (0 ako nije navedena)
- "net_weight": neto težina u kg (0 ako nije navedena)
- "currency": valuta (EUR, USD, BAM...)
- "country_origin": zemlja porijekla kao 2-slovni ISO kod (CN, TR, DE, IT... za Tursku uvijek TR)
- "tariff_code": ako faktura sadrži HS/tarifni broj za tu stavku (u kolonama kao "CTİP", "HS Code", "Tarifni broj", "Tariff No"...), uzmi PRVIH 8 CIFARA. Ako nema, ostavi "".

PRAVILA:
- Ignoriraj zaglavlja, adrese, napomene, ukupne sume - samo stavke robe
- Ignoriraj redove koji su B/L, teretnica, packing list zaglavlja
- Tarifni brojevi na fakturi su prioritet — ne izmišljaj ih ako nisu navedeni

Vrati SAMO JSON array, bez ikakvog teksta oko:
[{"name": "...", "quantity": 1.0, "unit_price": 0.0, "total_value": 0.0, "gross_weight": 0.0, "net_weight": 0.0, "currency": "EUR", "country_origin": "CN", "tariff_code": ""}, ...]"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            }
        ],
    )

    raw = message.content[0].text.strip()

    # Strip markdown fences
    if "```" in raw:
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json\n"):
            raw = raw[5:]
    raw = raw.strip()

    data = json.loads(raw)

    items = []
    currency = "EUR"
    for row in data:
        if not row.get("name"):
            continue
        currency = row.get("currency", currency) or currency
        raw_code = re.sub(r"[^\d]", "", str(row.get("tariff_code") or ""))[:8]
        items.append(InvoiceItem(
            name=str(row["name"]).upper().strip(),
            quantity=float(row.get("quantity") or 1.0),
            unit_price=float(row.get("unit_price") or 0.0),
            total_value=float(row.get("total_value") or 0.0),
            gross_weight=float(row.get("gross_weight") or 0.0),
            net_weight=float(row.get("net_weight") or 0.0),
            currency=currency,
            country_origin=str(row.get("country_origin") or "CN"),
            tariff_code=raw_code,
        ))

    return items, currency


# ── Fallback text-based extraction ────────────────────────────────────────

INVOICE_KEYWORDS = ["invoice", "faktura", "rechnung", "facture", "fattura",
                    "commercial invoice", "proforma"]
PACKING_LIST_KEYWORDS = ["packing list", "packing", "pakirana lista"]
BOL_KEYWORDS = ["bill of lading", "b/l", "bl number", "shipper", "consignee",
                "notify party", "vessel", "port of loading", "port of discharge"]


def _classify_page(text: str) -> str:
    lower = text.lower()
    bol_score = sum(1 for kw in BOL_KEYWORDS if kw in lower)
    inv_score = sum(1 for kw in INVOICE_KEYWORDS if kw in lower)
    pack_score = sum(1 for kw in PACKING_LIST_KEYWORDS if kw in lower)
    if bol_score >= 3:
        return "bol"
    if pack_score >= 2 and inv_score == 0:
        return "packing_list"
    if inv_score >= 1:
        return "invoice"
    if pack_score >= 1:
        return "packing_list"
    return "other"


def _ocr_page(page) -> str:
    if not TESSERACT_AVAILABLE:
        return ""
    mat = fitz.Matrix(2.0, 2.0)
    pix = page.get_pixmap(matrix=mat)
    img_data = pix.tobytes("png")
    img = Image.open(io.BytesIO(img_data))
    return pytesseract.image_to_string(img, lang="eng")


def _get_page_text(page) -> str:
    text = page.get_text()
    if len(text.strip()) < 50:
        text = _ocr_page(page)
    return text


def _extract_items_from_invoice_text(text: str, currency: str) -> list[InvoiceItem]:
    items = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    header_idx = -1
    for i, line in enumerate(lines):
        ll = line.lower()
        if ("item" in ll or "no." in ll) and ("description" in ll or "goods" in ll or "naziv" in ll):
            header_idx = i
            break
        if "qty" in ll and ("price" in ll or "amount" in ll):
            header_idx = i
            break

    if header_idx == -1:
        return _extract_items_fallback(lines, currency)

    for line in lines[header_idx + 1:]:
        ll = line.lower()
        if any(kw in ll for kw in ["total", "subtotal", "ukupno", "grand total",
                                    "bank", "payment", "tel:", "fax:", "www.", "http"]):
            break
        if len(line) < 5:
            continue
        numbers = PRICE_PATTERN.findall(line)
        if len(numbers) < 2:
            continue
        desc_match = re.match(r"^(\d+\.?\d*\s+)?([A-Za-z][^0-9]{5,})", line)
        if not desc_match:
            continue
        name = desc_match.group(2).strip()
        name = re.sub(r"\s+", " ", name).strip(" -.,")
        if len(name) < 3:
            continue
        floats = [_parse_number(n) for n in numbers if _parse_number(n) > 0]
        qty, unit_price, total_value = 1.0, 0.0, 0.0
        if len(floats) >= 3:
            total_value = floats[-1]
            unit_price = floats[-2]
            qty = floats[-3]
        elif len(floats) == 2:
            total_value = floats[-1]
            unit_price = floats[-2]
        elif len(floats) == 1:
            total_value = floats[0]
        country = "CN"
        cm = COUNTRY_PATTERN.search(line)
        if cm:
            country = cm.group(1)
        items.append(InvoiceItem(
            name=name, quantity=qty, unit_price=unit_price,
            total_value=total_value, country_origin=country, currency=currency,
        ))
    return items


def _extract_items_fallback(lines: list[str], currency: str) -> list[InvoiceItem]:
    items = []
    for line in lines:
        ll = line.lower()
        if any(kw in ll for kw in ["total", "bank", "payment", "address", "tel",
                                    "invoice", "packing", "date", "page"]):
            continue
        numbers = PRICE_PATTERN.findall(line)
        floats = [_parse_number(n) for n in numbers if _parse_number(n) > 0]
        if not floats:
            continue
        desc_match = re.match(r"^(\d+\s+)?([A-Za-z][A-Za-z0-9 \-/,]{4,})", line)
        if not desc_match:
            continue
        name = desc_match.group(2).strip()
        total_value = floats[-1] if floats else 0.0
        items.append(InvoiceItem(name=name, quantity=1.0, total_value=total_value, currency=currency))
    return items


def _parse_text_fallback(filepath: str) -> tuple[list[InvoiceItem], str]:
    """Text-based extraction fallback when Claude API unavailable."""
    if fitz is None:
        raise ImportError("PyMuPDF (fitz) is not installed.")

    doc = fitz.open(filepath)
    pages_by_type: dict[str, list[str]] = {"invoice": [], "packing_list": [], "bol": [], "other": []}

    for page in doc:
        text = _get_page_text(page)
        page_type = _classify_page(text)
        pages_by_type[page_type].append(text)
    doc.close()

    invoice_text = "\n".join(pages_by_type["invoice"])
    pack_text = "\n".join(pages_by_type["packing_list"])
    if not invoice_text:
        invoice_text = "\n".join(pages_by_type["other"] + pages_by_type["packing_list"])

    currency = _extract_currency(invoice_text or pack_text)
    items = _extract_items_from_invoice_text(invoice_text, currency)

    if pack_text and items:
        if any(i.gross_weight == 0 for i in items):
            _enrich_with_packing_list(items, pack_text)

    return items, currency


def _enrich_with_packing_list(items: list[InvoiceItem], pack_text: str):
    lines = pack_text.splitlines()
    for item in items:
        name_words = set(item.name.lower().split()[:3])
        for line in lines:
            if not name_words.intersection(set(line.lower().split())):
                continue
            numbers = PRICE_PATTERN.findall(line)
            floats = [_parse_number(n) for n in numbers if _parse_number(n) > 0]
            if len(floats) >= 2:
                item.net_weight = floats[-2]
                item.gross_weight = floats[-1]
                break
            elif len(floats) == 1:
                item.gross_weight = floats[0]
                item.net_weight = floats[0]
                break


def parse_pdf(filepath: str) -> tuple[list[InvoiceItem], str]:
    """
    Parse a PDF invoice. Tries Claude AI first, falls back to text extraction.
    Returns (items, currency).
    """
    try:
        items, currency = _parse_with_claude(filepath)
        if items:
            logger.info(f"Claude PDF parser: {len(items)} stavki izvučeno")
            return items, currency
        logger.warning("Claude vratio praznu listu, koristim text fallback")
    except Exception as e:
        logger.warning(f"Claude PDF parser neuspješan ({e}), koristim text fallback")

    return _parse_text_fallback(filepath)


def parse_excel_invoice(filepath: str) -> tuple[list[InvoiceItem], str]:
    """Parse an Excel (.xlsx/.xls) invoice file."""
    try:
        import openpyxl
    except ImportError:
        raise ImportError("openpyxl is not installed.")

    wb = openpyxl.load_workbook(filepath, data_only=True)
    ws = wb.active

    items = []
    currency = "EUR"
    header_row = -1

    rows = list(ws.iter_rows(values_only=True))

    for i, row in enumerate(rows):
        row_str = " ".join(str(c).lower() for c in row if c is not None)
        if "description" in row_str or "naziv" in row_str or "qty" in row_str:
            header_row = i
            break

    if header_row == -1:
        header_row = 0

    headers = [str(c).lower() if c else "" for c in rows[header_row]]

    def col(name_parts):
        for i, h in enumerate(headers):
            if any(p in h for p in name_parts):
                return i
        return -1

    desc_col = col(["description", "naziv", "goods", "item", "opis"])
    qty_col = col(["qty", "quantity", "kol"])
    price_col = col(["unit price", "unit_price", "cijena", "price"])
    total_col = col(["total", "amount", "iznos", "ukupno"])
    gw_col = col(["gross", "bruto"])
    nw_col = col(["net", "neto"])

    for row in rows[header_row + 1:]:
        if all(c is None for c in row):
            continue
        try:
            name = str(row[desc_col]).strip() if desc_col >= 0 and row[desc_col] else ""
            if not name or name.lower() in ("none", "total", "ukupno", "grand total"):
                continue
            qty = float(row[qty_col]) if qty_col >= 0 and row[qty_col] else 1.0
            unit_price = float(row[price_col]) if price_col >= 0 and row[price_col] else 0.0
            total_value = float(row[total_col]) if total_col >= 0 and row[total_col] else unit_price * qty
            gross_w = float(row[gw_col]) if gw_col >= 0 and row[gw_col] else 0.0
            net_w = float(row[nw_col]) if nw_col >= 0 and row[nw_col] else 0.0
            items.append(InvoiceItem(
                name=name, quantity=qty, unit_price=unit_price, total_value=total_value,
                gross_weight=gross_w, net_weight=net_w, currency=currency,
            ))
        except (TypeError, ValueError, IndexError):
            continue

    return items, currency
