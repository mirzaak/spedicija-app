"""
Automation pipeline:
Gmail → download all attachments from one email → Claude analyzes all files
as ONE shipment → generate one Excel + one XML → upload to Drive

Runs automatically via scheduler every 5 minutes.
"""
import os
import re
import base64
import json
import logging
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from database import SessionLocal, init_db
from gmail_watcher import fetch_new_invoices
from excel_generator import generate_excel
from xml_generator import generate_asycuda_xml
from drive_uploader import upload_declaration_files

logger = logging.getLogger("pipeline")

OUTPUT_DIR = Path(__file__).parent.parent / "data" / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# In-memory log of pipeline runs (last 100 entries, shown in UI)
pipeline_log: list[dict] = []


def _log(msg: str, level: str = "info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
    pipeline_log.append(entry)
    if len(pipeline_log) > 100:
        pipeline_log.pop(0)
    getattr(logger, level)(msg)


def _extract_pdf_text_if_sufficient(filepath: str) -> str | None:
    """
    Try PyMuPDF text extraction (+ OCR per page if needed).
    Returns extracted text if quality is sufficient (>=100 chars/page + decimal numbers).
    Returns None for scanned/image PDFs that need Claude Vision.
    """
    try:
        import fitz
        from pdf_parser import _get_page_text
        doc = fitz.open(filepath)
        page_texts = [_get_page_text(page) for page in doc]
        doc.close()
        full_text = "\n".join(page_texts)
        num_pages = max(len(page_texts), 1)
        chars_per_page = len(full_text.strip()) / num_pages
        has_numbers = bool(re.search(r'\d+[\.,]\d+', full_text))
        if chars_per_page >= 100 and has_numbers:
            return full_text
        return None
    except Exception:
        return None


def _file_to_base64_doc(filepath: str) -> dict | None:
    """Convert a file to a Claude document block (PDF or text for Excel)."""
    ext = Path(filepath).suffix.lower()
    if ext == ".pdf":
        # Strategy A: try text extraction first — much cheaper than PDF Vision
        text = _extract_pdf_text_if_sufficient(filepath)
        if text:
            logger.debug(f"PDF tekst ekstrahiran ({len(text)} chars): {Path(filepath).name}")
            return {
                "type": "text",
                "text": f"[PDF: {Path(filepath).name}]\n{text}",
            }
        # Fallback: scanned/image PDF → Claude Vision
        logger.debug(f"PDF skeniran, koristim Vision: {Path(filepath).name}")
        with open(filepath, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode("utf-8")
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": data},
        }
    elif ext in (".xlsx", ".xls"):
        # Extract text from Excel and send as text block
        try:
            import openpyxl
            wb = openpyxl.load_workbook(filepath, data_only=True)
            lines = []
            for sheet in wb.worksheets:
                lines.append(f"[Sheet: {sheet.title}]")
                for row in sheet.iter_rows(values_only=True):
                    if any(c is not None for c in row):
                        lines.append("\t".join(str(c) if c is not None else "" for c in row))
            text = "\n".join(lines)
            return {
                "type": "text",
                "text": f"[EXCEL FILE: {Path(filepath).name}]\n{text}",
            }
        except Exception as e:
            logger.warning(f"Could not read Excel {filepath}: {e}")
            return None
    elif ext in (".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp"):
        # Scanned image invoice → Claude Vision (image block)
        import mimetypes  # noqa: F811
        _media_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".tiff": "image/tiff", ".tif": "image/tiff",
            ".webp": "image/webp",
        }
        media_type = _media_map.get(ext, "image/jpeg")
        with open(filepath, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode("utf-8")
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    return None


def _repair_truncated_json(raw: str) -> dict:
    """
    Recover truncated JSON from Claude when max_tokens is hit mid-response.
    Finds the last fully closed item in the 'items' array and closes the JSON.
    """
    # Find last '}' at item level (depth==2: outer obj + items array)
    depth = 0
    in_string = False
    escape_next = False
    last_complete_item_end = -1

    for i, c in enumerate(raw):
        if escape_next:
            escape_next = False
            continue
        if c == '\\' and in_string:
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 2:  # just closed an item inside items array
                last_complete_item_end = i

    if last_complete_item_end > 0:
        repaired = raw[:last_complete_item_end + 1] + ']}'
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

    raise ValueError(f"JSON ne može biti popravljen (truncated at char {len(raw)})")


def _analyze_shipment_with_claude(files: list[dict], email_subject: str) -> dict:
    """
    Send all files from one email to Claude.
    Optimizations:
      - Haiku for text PDFs/Excel (no Vision needed) — ~4x cheaper
      - Sonnet only for scanned/image PDFs (Vision required)
      - Static prompt cached with cache_control ephemeral — saves ~10% on input
      - max_tokens starts at 4500, retries at 8000 then 16000 on truncation
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    # Build file blocks; track if any base64 Vision block (scanned PDF or image) is present.
    # NB: model is always Sonnet below — has_vision is diagnostic (logged), not a routing switch.
    file_blocks: list[dict] = []
    has_vision = False
    for f in files:
        doc_block = _file_to_base64_doc(f["filepath"])
        if doc_block:
            file_blocks.append({"type": "text", "text": f"--- FAJL: {f['filename']} ---"})
            file_blocks.append(doc_block)
            if doc_block.get("type") in ("document", "image"):
                has_vision = True

    if not file_blocks:
        raise ValueError("Nema fajlova za analizu")

    # Sonnet za sve — prijevod i ekstrakcija na Haiku nisu dovoljno tačni za carinske deklaracije
    model = "claude-sonnet-4-6"
    logger.info(f"  Analiza: model={model}, vision={has_vision}, fajlova={len(files)}")

    # ── Optimization 2: static prompt cached ────────────────────────────────
    # This block is identical on every call — Anthropic caches it after 1st use.
    # Cache read costs $0.30/M instead of $3.00/M input — 10x cheaper on this part.
    STATIC_INSTRUCTIONS = """Ti si carinski stručnjak. Analiziraš dokumente jedne uvozne pošiljke (faktura, packing lista, B/L...).
Dokumenti mogu biti na bilo kojem jeziku (turski, engleski, kineski, njemački, arapski, itd.).

PRAVILO NAZIVA ROBE — naziv vraćaj BOSANSKI, VELIKA SLOVA, max 4 riječi. SVAKU stavku MORAŠ prevesti:
- IZNIMKA: Ako je naziv stavke u dokumentu VEĆ na bosanskom/srpskom/hrvatskom jeziku
  (latinicom, razumljive bosanske/srpske/hrvatske riječi, nema stranih znakova),
  koristi taj naziv DOSLOVNO bez ikakvih izmjena — ne prevodi ono što je već prevedeno.
  Primjer: "IGR. AUTO NA BATERIJE" ostaje "IGR. AUTO NA BATERIJE" (ne mijenjaj!)
- Igračke (toy/oyuncak/jouet/Spielzeug/玩具) → OBAVEZAN format "IGR. [vrsta]"
  TOY CAR→IGR. AUTO | TOY TRUCK→IGR. KAMION | DOLL→IGR. LUTKA | BABY WALKER→IGR. PROHODALICA
  BUILDING BLOCKS→IGR. KOCKE | PUZZLE→IGR. PUZZLE | PLUSH TOY→IGR. PLIŠANA
  RC CAR→IGR. AUTO DALJINSKI | ELECTRIC TRAIN→IGR. VOZ | TOY KITCHEN→IGR. KUHINJA
  BOARD GAME→IGR. DRUŠTVENA | INFLATABLE TOY→IGR. NAPUHLJIV | RIDE-ON→IGR. AUTO NA AKU
  IZNIMKA: PLASTELIN/PLAY-DOH→PLASTELIN | BABY MAT→PODLOGA ZA BEBE
- Turski: YAĞ FİLTRE→FILTER ULJA | AMORTISÖR→AMORTIZER | SİLECEK→BRISAČ | YEDEK PARÇA→REZERVNI DIO
  STOP LAMBASI→STOP SVJETLO | AYNA KAPAĞI→POKLOPAC RETROVIZORA | KAPI KİLİDİ→BRAVA VRATA
  DEBRİYAJ→KVAČILO | FREN→KOČNICA | YAY→OPRUGA | EGZOZ→ISPUH | LASTİK→GUMA | KOLTUK→SJEDIŠTE
  HALAT→UŽE | KAPAK→POKLOPAC | BORU→CIJEV | VANA→VENTIL | MOTOR→MOTOR | POMPA→PUMPA
- Kineski / engleski opći: CARPET→TEPIH | RUG→TEPIH | CURTAIN→ZAVJESA | BLANKET→DEKA
  CHAIR→STOLICA | TABLE→STOL | LAMP→LAMPA | BAG→TORBA | SHOE→CIPELA | GARMENT→ODJEĆA
  TOOL→ALAT | PIPE→CIJEV | VALVE→VENTIL | BEARING→LEŽAJ | SPRING→OPRUGA | BOLT→VIJAK
  WHEEL→KOTAČ | CABLE→KABEL | SENSOR→SENZOR | PUMP→PUMPA | FILTER→FILTER | FAN→VENTILATOR
- Ako ne znaš prijevod: napiši engleski naziv velikim slovima (bolje od praznog polja)

PRAVILO TEŽINA — prioritet: B/L > packing lista > faktura > procjena:
- UVIJEK traži UKUPNI/SUMMARY red na dnu: "TOTAL", "GRAND TOTAL", "TOPLAM", "TOPLAM AĞIRLIK",
  "合計", "GESAMT", "TOTAAL", "TOTAL POIDS", "UKUPNO" i sl.
- total_gross_weight i total_net_weight: ISKLJUČIVO iz summary reda — nikad računaj iz stavki.
- Više kontejnera/batcheva: SABERI sve u ukupni total. NIKAD jedan kontejner kao ukupno.
- Per-item težine ostavi 0 ako nisu navedene — backend raspoređuje. Total NIKAD ne smije biti 0.
- gross_weight ≈ net_weight × 1.05–1.15.

PRAVILO VRIJEDNOSTI:
- total_invoice_value: uzimaj ISKLJUČIVO iz summary/total reda fakture (ne računaj iz stavki).
- Redove fakture s "Freight", "Shipping", "Transport", "Handling", "Insurance", "Commission",
  "Bank charge", "Service fee", "Logistics" — IGNORIŠI kao robu, stavi u external_freight/insurance.
- consignee_pib: PDV broj, PIB, JIB, Tax ID, VAT No, Vergi No primatelja — OBAVEZNO izvuci.
- delivery_place: format "Grad, Bosna i Hercegovina" (npr. "Lukavac, Bosna i Hercegovina").

POLJA ZA SVAKU STAVKU: name, quantity, unit_price, total_value, gross_weight, net_weight,
supplementary_quantity (m², m, l, par — iz fakture/packing liste ako navedeno, inače 0),
currency, country_origin (2-slova, default CN, Turska=TR),
tariff_code, tariff_from_invoice.

PRAVILO TARIFNI KOD — strogo:
- tariff_code: OSTAVI PRAZNO ("") OSIM ako faktura EKSPLICITNO sadrži ispisan
  HS/CTİP/carinski broj uz tu stavku (8-10 cifara, npr. "8516.10.80" ili "84713000").
- Ako faktura NEMA ispisane tarifne brojeve uz stavke → tariff_code = "" za SVE stavke.
- NIKAD ne izmišljaj, ne pogađaj, ne izvodi kod iz naziva robe.
- NIKAD ne kopiraj kod iz prethodnih deklaracija — gledaj SAMO ovaj dokument.
- Prazno je ISPRAVNO kada faktura nema kod — sistem klasifikatora će ga odrediti.
- tariff_from_invoice: true SAMO kada si kod stvarno PROČITAO iz fakture; inače false.
  (Bez ovog flaga klasifikator ne smije vjerovati kodu — služi protiv halucinacija.)

OPĆA POLJA: consignee, consignee_pib, invoice_number, currency, total_invoice_value,
total_gross_weight, total_net_weight, total_packages (CTNS/kartoni, NE komadi),
exporter_name, exporter_city, exporter_street, exporter_country, eur1_reference,
delivery_place, container_number, transport_identity, transport_nationality (default BA),
border_office_code, border_office_name, transit_doc,
external_freight, external_freight_currency (default EUR), internal_freight, insurance, currency_rate.

PRAVILO RAZDVAJANJA STAVKI:
- Svaki različit proizvod = zasebna stavka, čak i ako su na fakturi u istom redu ili istoj ćeliji.
- NIKAD ne spajaj dva različita artikla u jedan name (npr. "ZIDNA LAMPA, LAMPA KOMARCI").
- Ako jedan red fakture sadrži više vrsta robe → razdvoji u više stavki.
- Artikli koji idu u različite tarifne brojeve MORAJU biti odvojeni.

PRAVILA: Ignoriraj freight/transport fakture. Ne dupliciraj stavke iz više dokumenata.
Tarifne kodove izvuci SAMO ako su EKSPLICITNO ispisani u fakturi (vidi pravilo gore).
Vrati SAMO čisti JSON bez ikakvog teksta oko njega."""

    # ── Content assembly: cached static first, then documents, then task ────
    content: list[dict] = [
        {
            "type": "text",
            "text": STATIC_INSTRUCTIONS,
            "cache_control": {"type": "ephemeral"},
        },
        *file_blocks,
        {
            "type": "text",
            "text": (
                'Izvuci sve stavke robe i vrati SAMO čisti JSON u ovom formatu:\n'
                '{"consignee":"","consignee_pib":"","invoice_number":"","currency":"EUR",'
                '"total_invoice_value":0.0,'
                '"exporter_name":"","exporter_city":"","exporter_street":"","exporter_country":"CN",'
                '"eur1_reference":"","delivery_place":"","container_number":"",'
                '"transport_identity":"","transport_nationality":"BA","border_office_code":"",'
                '"border_office_name":"","transit_doc":"","external_freight":0,'
                '"external_freight_currency":"EUR","internal_freight":0,"insurance":0,'
                '"currency_rate":0,"total_packages":0,"total_gross_weight":0.0,'
                '"total_net_weight":0.0,'
                '"items":[{"name":"","quantity":0,"unit_price":0,"total_value":0,'
                '"gross_weight":0,"net_weight":0,"supplementary_quantity":0,'
                '"currency":"EUR","country_origin":"CN","tariff_code":"",'
                '"tariff_from_invoice":false}]}'
            ),
        },
    ]

    # ── Optimization 3+4: right-sized max_tokens + truncation-aware retry ─────
    # A3: start max_tokens from input size — large invoices (many line items)
    # need more output headroom, so starting low then retrying doubles cost.
    # Text length of the doc blocks is a decent proxy for item count.
    # (Vision/base64 docs carry no text here → fall back to the small schedule.)
    _doc_chars = sum(len(b.get("text", "")) for b in file_blocks if b.get("type") == "text")
    if _doc_chars > 12000:
        schedule = [8000, 16000]
    elif _doc_chars > 5000:
        schedule = [6000, 12000]
    else:
        schedule = [4500, 9000, 16000]

    # A2: only retry with more tokens when the response was genuinely cut off
    # (stop_reason == "max_tokens"). A parse failure for any other reason won't be
    # fixed by more tokens — repair locally instead of burning another full call.
    data: dict | None = None
    raw = ""
    for i, max_tok in enumerate(schedule):
        last = i == len(schedule) - 1
        message = client.messages.create(
            model=model,
            max_tokens=max_tok,
            messages=[{"role": "user", "content": content}],
        )
        u = message.usage
        logger.info(
            f"  Claude usage: in={u.input_tokens} out={u.output_tokens} "
            f"cache_w={getattr(u,'cache_creation_input_tokens',0)} "
            f"cache_r={getattr(u,'cache_read_input_tokens',0)} "
            f"max_tok={max_tok} stop={getattr(message,'stop_reason',None)}"
        )
        try:
            from database import log_api_usage
            log_api_usage(None, model, "extract", u)
        except Exception as _lue:
            logger.warning(f"  log_api_usage greška: {_lue}")

        raw = message.content[0].text.strip()
        # Strip markdown fences
        if "```" in raw:
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else parts[0]
            if raw.startswith("json\n"):
                raw = raw[5:]
        raw = raw.strip()

        try:
            data = json.loads(raw)
            break  # success — stop retrying
        except json.JSONDecodeError:
            truncated = getattr(message, "stop_reason", None) == "max_tokens"
            if truncated and not last:
                logger.warning(
                    f"  JSON truncated at max_tokens={max_tok} "
                    f"(output={u.output_tokens}), retrying with {schedule[i + 1]}..."
                )
                continue
            # Not a length problem, or out of retries → recover locally.
            data = _repair_truncated_json(raw)
            break

    # Normalize items
    items = []
    currency = data.get("currency", "EUR")

    # Auto-fetch currency rate if Claude didn't extract it
    raw_rate = float(data.get("currency_rate") or 0)
    if raw_rate == 0 and currency:
        try:
            from currency_api import fetch_rate
            raw_rate = fetch_rate(currency)
            if raw_rate:
                logger.info(f"  Auto-kurs {currency}/BAM: {raw_rate}")
        except Exception as e:
            logger.warning(f"  Auto-kurs greška: {e}")

    for row in data.get("items", []):
        if not row.get("name"):
            continue
        items.append({
            "name": str(row["name"]).upper().strip(),
            "quantity": float(row.get("quantity") or 1.0),
            "value": float(row.get("total_value") or 0.0),
            "gross_weight": float(row.get("gross_weight") or 0.0),
            "net_weight": float(row.get("net_weight") or 0.0),
            "supplementary_quantity": float(row.get("supplementary_quantity") or 0.0),
            "tariff_code": str(row.get("tariff_code") or ""),
            "country_origin": str(row.get("country_origin") or "CN"),
            "currency": str(row.get("currency") or currency),
        })

    total_gross         = float(data.get("total_gross_weight") or 0.0)
    total_net           = float(data.get("total_net_weight")   or 0.0)
    total_invoice_value = float(data.get("total_invoice_value") or 0.0)

    # Value reconciliation — ista logika kao za težine
    VALUE_TOLERANCE = 0.01  # 1%
    items_value_sum = sum(i["value"] for i in items)
    if total_invoice_value > 0 and items_value_sum > 0:
        diff = abs(items_value_sum - total_invoice_value) / total_invoice_value
        if diff > VALUE_TOLERANCE:
            logger.warning(
                f"  ⚠ Vrijednost neslaganje: suma stavki={items_value_sum:.2f}, "
                f"total fakture={total_invoice_value:.2f} ({diff*100:.1f}% razlika) — skalirano."
            )
            scale_val = total_invoice_value / items_value_sum
            for i in items:
                i["value"] = round(i["value"] * scale_val, 2)

    # delivery_place: osiguraj format "Grad, Bosna i Hercegovina"
    delivery_place = data.get("delivery_place", "")
    if delivery_place and "bosna" not in delivery_place.lower() and "herzegovina" not in delivery_place.lower():
        delivery_place = delivery_place.rstrip(", ") + ", Bosna i Hercegovina"

    WEIGHT_TOLERANCE = 0.05  # 5% — razlika veća od ovoga → skaliraj

    items_gross_sum = sum(i["gross_weight"] for i in items)
    items_net_sum   = sum(i["net_weight"]   for i in items)

    if total_gross > 0 and items:
        if items_gross_sum == 0:
            # Nema per-item težina — rasporedi po (qty × vrijednost)
            total_qv = sum(i["quantity"] * i["value"] for i in items) or len(items)
            for i in items:
                share = (i["quantity"] * i["value"] / total_qv) if total_qv else (1.0 / len(items))
                i["gross_weight"] = round(total_gross * share, 3)
                if total_net > 0:
                    i["net_weight"] = round(total_net * share, 3)
        elif abs(items_gross_sum - total_gross) / total_gross > WEIGHT_TOLERANCE:
            # Per-item suma značajno odstupa od totala (Claude čitao samo dio dokumenta)
            diff_pct = abs(items_gross_sum - total_gross) / total_gross * 100
            logger.warning(
                f"  ⚠ Težina neslaganje: suma stavki={items_gross_sum:.1f} kg, "
                f"total={total_gross:.1f} kg ({diff_pct:.1f}% razlika) — skalirano na total."
            )
            scale_gross = total_gross / items_gross_sum
            for i in items:
                i["gross_weight"] = round(i["gross_weight"] * scale_gross, 3)
            if total_net > 0 and items_net_sum > 0:
                scale_net = total_net / items_net_sum
                for i in items:
                    i["net_weight"] = round(i["net_weight"] * scale_net, 3)

    # Fallback: stavke s 0 težinom nakon svega dobiju minimalni udio
    items_gross_sum = sum(i["gross_weight"] for i in items)
    for i in items:
        if i["gross_weight"] == 0 and items_gross_sum > 0:
            i["gross_weight"] = round(items_gross_sum / len(items), 3)
        elif i["gross_weight"] == 0 and total_gross > 0:
            i["gross_weight"] = round(total_gross / len(items), 3)

    # Derive exporter_country: use Claude's answer, or fall back to majority of item origins
    exporter_country = data.get("exporter_country", "")
    if not exporter_country and items:
        from collections import Counter
        origin_counts = Counter(i["country_origin"] for i in items)
        exporter_country = origin_counts.most_common(1)[0][0]

    return {
        "items": items,
        "currency": currency,
        "consignee": data.get("consignee", ""),
        "consignee_pib": str(data.get("consignee_pib") or ""),
        "invoice_number": data.get("invoice_number", ""),
        "exporter_name": data.get("exporter_name", ""),
        "exporter_city": data.get("exporter_city", ""),
        "exporter_street": data.get("exporter_street", ""),
        "exporter_country": exporter_country or "CN",
        "eur1_reference": data.get("eur1_reference", ""),
        "delivery_place": delivery_place,
        "container_number": data.get("container_number", ""),
        "transport_identity": data.get("transport_identity", ""),
        "transport_nationality": data.get("transport_nationality", "BA") or "BA",
        "border_office_code": data.get("border_office_code", ""),
        "border_office_name": data.get("border_office_name", ""),
        "transit_doc": data.get("transit_doc", ""),
        "external_freight": float(data.get("external_freight") or 0),
        "external_freight_currency": data.get("external_freight_currency", "EUR") or "EUR",
        "internal_freight": float(data.get("internal_freight") or 0),
        "insurance": float(data.get("insurance") or 0),
        "currency_rate": raw_rate,
        "total_packages": int(data.get("total_packages") or 0),
        "total_gross_weight": total_gross,
        "total_net_weight": total_net,
    }



_SU_UNIT_MAP = {
    # Pieces
    "p/st": "PCE", "pce": "PCE", "u": "PCE", "piece": "PCE",
    "nos": "PCE", "no": "PCE", "st": "PCE", "kom": "PCE", "kos": "PCE",
    # Square metres
    "m2": "MTK", "m²": "MTK", "m 2": "MTK", "sqm": "MTK",
    # Linear metres
    "m": "MTR", "m¹": "MTR", "lm": "MTR",
    # Litres
    "l": "LTR", "ltr": "LTR", "liter": "LTR", "litre": "LTR",
    # Pairs
    "par": "PR", "pair": "PR", "pr": "PR",
    # Kilograms
    "kg": "KGM",
}


_ASYCUDA_SU_CODES = {"PCE", "MTK", "MTR", "MTQ", "LTR", "PR", "KGM", "GRM"}


def assign_supplementary_units(items: list[dict], db) -> None:
    """
    Popuni polje 41 (Dopunske jedinice) po stavci — in-place, VOĐENO TARIFOM.

    Jedinica se uzima iz OfficialTariff.unit (propisana "Dopunska jedinica" iz
    Carinske tarife BiH, importovana pozicijski iz PDF-a). Kad tarifa za taj kod
    NE propisuje dopunsku jedinicu (većina roba), box 41 ostaje prazan — tako i
    treba. Broj (komada/m²/…) generator uzima iz supplementary_quantity ili
    glavne količine (`xml_generator.py`).

    Kodovi u OfficialTariff su 10-cifreni → match po prefiksu 8-cifrenog box 33.
    Poziva se i iz ekstrakcije i iz finalize (nakon agentovih izmjena koda).
    """
    from database import OfficialTariff
    for item in items:
        if item.get("supplementary_unit"):
            continue
        code = (item.get("tariff_code") or "").replace(" ", "").replace(".", "")[:8]
        if not code:
            continue
        row = (db.query(OfficialTariff)
                 .filter(OfficialTariff.code.like(f"{code}%"))
                 .filter(OfficialTariff.unit.isnot(None))
                 .first())
        if not (row and row.unit):
            continue
        u = row.unit.strip()
        asy = u if u.upper() in _ASYCUDA_SU_CODES else _SU_UNIT_MAP.get(u.lower())
        if asy:
            item["supplementary_unit"] = asy


def _auto_assign_tariffs(items: list[dict], db) -> list[dict]:
    """Fill in missing tariff codes from knowledge base, then Claude classifier."""
    from xml_parser import search_tariff

    for item in items:
        if item.get("tariff_code"):
            continue
        suggestions = search_tariff(item["name"], db, limit=1)
        if suggestions:
            item["tariff_code"] = suggestions[0].tariff_code

    # Try Claude classifier for remaining
    try:
        from claude_classifier import classify_items_batch
        items = classify_items_batch(items, db=db)
    except Exception as e:
        logger.warning(f"Claude classifier unavailable: {e}")

    # Popuni polje 41 (Dopunske jedinice) — OfficialTariff.unit ili PCE iz komada
    assign_supplementary_units(items, db)

    return items


def _safe_folder_name(subject: str, received_at: str) -> str:
    stem = re.sub(r"[^\w\s-]", "", subject).strip()[:50]
    return f"{received_at}_{stem}"


def process_shipment(shipment: dict) -> dict:
    """
    Full pipeline for one email (shipment).
    All files analyzed together as one shipment → one Excel + one XML.
    """
    subject = shipment["email_subject"]
    received_at = shipment["received_at"]
    files = shipment["files"]

    filenames = [f["filename"] for f in files]
    _log(f"Pošiljka: '{subject}' — {len(files)} fajlova: {', '.join(filenames)}")

    # 1. Claude analyzes all files together
    try:
        shipment_data = _analyze_shipment_with_claude(files, subject)
    except Exception as e:
        _log(f"Claude analiza neuspješna: {e}", "error")
        return {"status": "error", "subject": subject, "error": str(e)}

    items = shipment_data["items"]
    if not items:
        _log(f"Nema stavki robe u pošiljci '{subject}'", "warning")
        return {"status": "skipped", "subject": subject, "reason": "no items extracted"}

    currency = shipment_data["currency"]
    consignee = shipment_data["consignee"] or subject[:60]
    _log(f"Izvučeno {len(items)} stavki, valuta {currency}, primatelj: {consignee}")

    # 2. Auto-assign tariff codes + load settings
    db = SessionLocal()
    try:
        items = _auto_assign_tariffs(items, db)
        from database import get_setting
        declarant_code = get_setting(db, "declarant_code")
        declarant_name = get_setting(db, "declarant_name")
        declarant_ref  = get_setting(db, "declarant_ref")
        office_code    = get_setting(db, "office_code", "BA010301")
        office_name    = get_setting(db, "office_name", "CI Tuzla")
        notify_email   = get_setting(db, "notify_email")
    finally:
        db.close()

    assigned = sum(1 for i in items if i.get("tariff_code"))
    _log(f"Tarifni brojevi: {assigned}/{len(items)} dodijeljeni")

    from classification_utils import count_suspects
    suspect_count, suspect_names = count_suspects(items)
    if suspect_count:
        _log(f"⚠ {suspect_count} stavki sa niskim/srednjim AI confidence — za pregled", "warning")

    # 3. Generate ONE Excel + ONE XML
    ts = received_at
    inv_num = re.sub(r"[^\w-]", "", shipment_data.get("invoice_number", "")).strip()
    safe_subject = re.sub(r"[^\w\s-]", "", subject).strip()[:40]
    excel_name = f"{ts}_{safe_subject}.xlsx"
    xml_name = f"{inv_num}.xml" if inv_num else f"{ts}_{safe_subject}.xml"
    excel_path = str(OUTPUT_DIR / excel_name)
    xml_path = str(OUTPUT_DIR / xml_name)

    try:
        generate_excel(items, excel_path, consignee=consignee)
        _log(f"Excel generisan: {excel_name}")
    except Exception as e:
        _log(f"Excel greška: {e}", "error")
        excel_path = None

    try:
        generate_asycuda_xml(
            items=items,
            consignee_name=consignee,
            consignee_code=shipment_data.get("consignee_pib", ""),
            consignee_address=shipment_data.get("consignee_address", ""),
            exporter_name=shipment_data.get("exporter_name", ""),
            exporter_city=shipment_data.get("exporter_city", ""),
            exporter_street=shipment_data.get("exporter_street", ""),
            exporter_country=shipment_data.get("exporter_country", "CN"),
            currency=currency,
            currency_rate=shipment_data.get("currency_rate", 0.0),
            declaration_date=ts[:8],
            container_number=shipment_data.get("container_number", ""),
            transport_identity=shipment_data.get("transport_identity", ""),
            transport_nationality=shipment_data.get("transport_nationality", "BA"),
            border_office_code=shipment_data.get("border_office_code", ""),
            border_office_name=shipment_data.get("border_office_name", ""),
            transit_doc=shipment_data.get("transit_doc", ""),
            delivery_terms="",   # uslovi isporuke uvijek prazni (agentov zahtjev)
            delivery_place="",
            invoice_number=shipment_data.get("invoice_number", ""),
            eur1_reference=shipment_data.get("eur1_reference", ""),
            external_freight=shipment_data.get("external_freight", 0.0),
            external_freight_currency=shipment_data.get("external_freight_currency", "EUR"),
            internal_freight=shipment_data.get("internal_freight", 0.0),
            insurance=shipment_data.get("insurance", 0.0),
            packages_count=shipment_data.get("total_packages", 0),
            declarant_code=declarant_code,
            declarant_name=declarant_name,
            declarant_ref=declarant_ref,
            office_code=office_code or "BA010301",
            office_name=office_name or "CI Tuzla",
            output_path=xml_path,
        )
        _log(f"XML generisan: {xml_name}")
    except Exception as e:
        _log(f"XML greška: {e}", "error")
        xml_path = None

    # 3b. Save generated XML to Declaration DB (grows knowledge base automatically)
    if xml_path and Path(xml_path).exists():
        try:
            from xml_parser import parse_xml_file
            db2 = SessionLocal()
            try:
                new_records = parse_xml_file(xml_path, db2)
                _log(f"KB ažuriran: {new_records} novih tarifnih zapisa iz {xml_name}")
            finally:
                db2.close()
        except Exception as e:
            _log(f"KB ažuriranje greška (nije kritično): {e}", "warning")

    # 4. Upload to Drive — one folder per shipment, all original files + Excel + XML
    folder_name = _safe_folder_name(subject, ts)
    _log(f"Upload na Drive: Spedicija/{folder_name}/")

    try:
        drive_links = upload_declaration_files(
            subfolder_name=folder_name,
            original_path=files[0]["filepath"],  # first file as "original"
            excel_path=excel_path,
            xml_path=xml_path,
        )
        _log(f"Drive upload završen: {folder_name}")
    except Exception as e:
        _log(f"Drive upload greška: {e}", "error")
        drive_links = {"error": str(e)}

    _log(f"✓ Pošiljka '{subject}' → {len(items)} stavki, Drive: {folder_name}")

    # Email notification (non-critical)
    if notify_email:
        try:
            from notifier import send_processing_complete
            send_processing_complete(
                to_email=notify_email,
                subject=subject,
                items_count=len(items),
                tariffs_assigned=assigned,
                drive_folder=folder_name,
                excel_name=excel_name,
                xml_name=xml_name,
                suspect_count=suspect_count,
            )
        except Exception as e:
            _log(f"Notifikacija greška: {e}", "warning")

    return {
        "status": "ok",
        "subject": subject,
        "items_count": len(items),
        "tariffs_assigned": assigned,
        "currency": currency,
        "consignee": consignee,
        "drive_folder": folder_name,
        "drive_links": drive_links,
        "excel": excel_name,
        "xml": xml_name,
        # A1: perzistuju se na PendingShipment red TEK nakon što run_pipeline() vidi
        # status="ok" — ranije su se ove stavke (sa confidence/tariff_source) računale
        # i bacale, pa je full-auto tok bio nevidljiv u dashboardu.
        "items": items,
        "suspect_count": suspect_count,
        "suspect_items": suspect_names,
    }


def process_shipment_extract(shipment: dict) -> dict:
    """
    SEMI-AUTO TOK — korak 1 od 2: SAMO ekstrakcija + klasifikacija.

    Pandan prvog dijela process_shipment() (koraci A + B), bez ikakvog
    Drive uploada, XML/Excel generacije, KB ingestiona ni email notifikacije.
    Drive NIKAD nije dotaknut iz ove funkcije — to je sigurnosna kapija
    za semi-auto tok (agent pregleda + edituje + tek onda klikne Potvrdi).

    Vraća dict: {status, items, metadata, subject}
      status='ok'      → uspješna ekstrakcija
      status='error'   → Claude analiza pala
      status='skipped' → nema stavki u pošiljci
    """
    subject = shipment["email_subject"]
    files = shipment["files"]
    filenames = [f["filename"] for f in files]
    _log(f"[EXTRACT] '{subject}' — {len(files)} fajlova: {', '.join(filenames)}")

    # A. Claude AI ekstrakcija
    try:
        shipment_data = _analyze_shipment_with_claude(files, subject)
    except Exception as e:
        _log(f"[EXTRACT] Claude analiza pala: {e}", "error")
        return {"status": "error", "subject": subject, "error": str(e)}

    items = shipment_data.get("items", [])
    if not items:
        _log(f"[EXTRACT] Nema stavki u '{subject}'", "warning")
        return {"status": "skipped", "subject": subject, "reason": "no items extracted"}

    currency = shipment_data.get("currency", "EUR")
    consignee = shipment_data.get("consignee") or subject[:60]
    _log(f"[EXTRACT] {len(items)} stavki, valuta {currency}, primalac: {consignee}")

    # B. Auto-dodjela tarifnih kodova (klasifikacija)
    db = SessionLocal()
    try:
        items = _auto_assign_tariffs(items, db)
    finally:
        db.close()

    assigned = sum(1 for i in items if i.get("tariff_code"))
    _log(f"[EXTRACT] Tarifni brojevi: {assigned}/{len(items)} dodijeljeni")

    # Sve ostalo (metadata) odvojeno od items radi UI uređivanja
    metadata = {k: v for k, v in shipment_data.items() if k != "items"}

    return {
        "status": "ok",
        "subject": subject,
        "items": items,
        "metadata": metadata,
        "items_count": len(items),
        "tariffs_assigned": assigned,
    }


def process_shipment_finalize(
    shipment: dict,
    items: list,
    metadata: dict,
) -> dict:
    """
    SEMI-AUTO TOK — korak 2 od 2: generacija + Drive upload.

    Pandan drugog dijela process_shipment() (koraci C-G). Koristi
    `items` i `metadata` koji su VEROVATNO EDITOVANI od strane agenta
    kroz UI (nije isto što i originalni AI output).

    Ovo je JEDINI put u sistemu koji dotiče drive_uploader iz semi-auto toka.
    Zove se isključivo iz /pipeline/finalize/{id} endpoint-a (eksplicitan
    agentov klik).

    Vraća dict: {status, drive_folder, drive_url, xml_name, excel_name}
    """
    subject = shipment["email_subject"]
    received_at = shipment["received_at"]
    files = shipment["files"]
    _log(f"[FINALIZE] '{subject}' — {len(items)} stavki za XML/Excel/Drive")

    currency = metadata.get("currency", "EUR")
    consignee = metadata.get("consignee") or subject[:60]

    # Load deklarant postavki
    db = SessionLocal()
    try:
        from database import get_setting
        declarant_code = get_setting(db, "declarant_code")
        declarant_name = get_setting(db, "declarant_name")
        declarant_ref  = get_setting(db, "declarant_ref")
        office_code    = get_setting(db, "office_code", "BA010301")
        office_name    = get_setting(db, "office_name", "CI Tuzla")
        notify_email   = get_setting(db, "notify_email")
    finally:
        db.close()

    # C. Excel
    ts = received_at
    inv_num = re.sub(r"[^\w-]", "", metadata.get("invoice_number", "")).strip()
    safe_subject = re.sub(r"[^\w\s-]", "", subject).strip()[:40]
    excel_name = f"{ts}_{safe_subject}.xlsx"
    xml_name = f"{inv_num}.xml" if inv_num else f"{ts}_{safe_subject}.xml"
    excel_path = str(OUTPUT_DIR / excel_name)
    xml_path = str(OUTPUT_DIR / xml_name)

    try:
        generate_excel(items, excel_path, consignee=consignee)
        _log(f"[FINALIZE] Excel generisan: {excel_name}")
    except Exception as e:
        _log(f"[FINALIZE] Excel greška: {e}", "error")
        excel_path = None

    # D. XML
    try:
        db2 = SessionLocal()
        try:
            # Polje 41 nakon agentovih izmjena (kod/količina su mogli biti promijenjeni u UI)
            assign_supplementary_units(items, db2)
            generate_asycuda_xml(
                items=items,
                consignee_name=consignee,
                consignee_code=metadata.get("consignee_pib", ""),
                consignee_address=metadata.get("consignee_address", ""),
                exporter_name=metadata.get("exporter_name", ""),
                exporter_city=metadata.get("exporter_city", ""),
                exporter_street=metadata.get("exporter_street", ""),
                exporter_country=metadata.get("exporter_country", "CN"),
                currency=currency,
                currency_rate=metadata.get("currency_rate", 0.0),
                declaration_date=ts[:8],
                container_number=metadata.get("container_number", ""),
                transport_identity=metadata.get("transport_identity", ""),
                transport_nationality=metadata.get("transport_nationality", "BA"),
                border_office_code=metadata.get("border_office_code", ""),
                border_office_name=metadata.get("border_office_name", ""),
                transit_doc=metadata.get("transit_doc", ""),
                delivery_terms="",   # uslovi isporuke uvijek prazni (agentov zahtjev)
                delivery_place="",
                invoice_number=metadata.get("invoice_number", ""),
                eur1_reference=metadata.get("eur1_reference", ""),
                external_freight=metadata.get("external_freight", 0.0),
                external_freight_currency=metadata.get("external_freight_currency", "EUR"),
                internal_freight=metadata.get("internal_freight", 0.0),
                insurance=metadata.get("insurance", 0.0),
                packages_count=metadata.get("total_packages", 0),
                declarant_code=declarant_code,
                declarant_name=declarant_name,
                declarant_ref=declarant_ref,
                office_code=office_code or "BA010301",
                office_name=office_name or "CI Tuzla",
                output_path=xml_path,
                db=db2,
            )
            _log(f"[FINALIZE] XML generisan: {xml_name}")
        finally:
            db2.close()
    except Exception as e:
        _log(f"[FINALIZE] XML greška: {e}", "error")
        xml_path = None

    # E. KB ingestion (automatski rast tariff_records iz svake potvrđene deklaracije)
    if xml_path and Path(xml_path).exists():
        try:
            from xml_parser import parse_xml_file
            db3 = SessionLocal()
            try:
                new_records = parse_xml_file(xml_path, db3)
                _log(f"[FINALIZE] KB ažuriran: {new_records} novih zapisa iz {xml_name}")
            finally:
                db3.close()
        except Exception as e:
            _log(f"[FINALIZE] KB greška (nije kritično): {e}", "warning")

    # F. DRIVE UPLOAD — jedini poziv iz semi-auto toka, samo poslije agentove potvrde
    folder_name = _safe_folder_name(subject, ts)
    _log(f"[FINALIZE] Drive upload: Spedicija/{folder_name}/")
    drive_links = None
    try:
        drive_links = upload_declaration_files(
            subfolder_name=folder_name,
            original_path=files[0]["filepath"],
            excel_path=excel_path,
            xml_path=xml_path,
        )
        _log(f"[FINALIZE] Drive upload završen: {folder_name}")
    except Exception as e:
        _log(f"[FINALIZE] Drive greška: {e}", "error")
        drive_links = {"error": str(e)}

    # G. Email notifikacija (opcionalno)
    if notify_email:
        try:
            from notifier import send_processing_complete
            send_processing_complete(
                to_email=notify_email,
                subject=subject,
                items_count=len(items),
                tariffs_assigned=sum(1 for i in items if i.get("tariff_code")),
                drive_folder=folder_name,
                excel_name=excel_name,
                xml_name=xml_name,
            )
        except Exception as e:
            _log(f"[FINALIZE] Email greška (nije kritično): {e}", "warning")

    return {
        "status": "ok",
        "subject": subject,
        "items_count": len(items),
        "drive_folder": folder_name,
        "drive_links": drive_links,
        "excel": excel_name,
        "xml": xml_name,
        "xml_path": xml_path,
    }


def run_pipeline():
    """
    Main pipeline entry point. Called by scheduler every 5 minutes.
    Fetches new Gmail emails and immediately processes them (no manual approval needed).
    """
    _log("--- Gmail provjera ---")
    try:
        new_count = fetch_new_invoices(log_cb=_log)
    except Exception as e:
        _log(f"Gmail greška: {e}", "error")
        return

    if new_count:
        _log(f"Pronađeno {new_count} novih pošiljki — kreće automatska obrada...")
    else:
        _log("Nema novih mejlova — provjeravam postoje li neobrađene pošiljke...")

    db = SessionLocal()
    try:
        from database import PendingShipment
        pending = db.query(PendingShipment).filter_by(status="pending").order_by(PendingShipment.created_at).all()
        if not pending:
            _log("Nema neobrađenih pošiljki.")
            return
        _log(f"Obrađujem {len(pending)} pošiljki automatski...")
        for row in pending:
            try:
                files = json.loads(row.files_json or "[]")
                shipment = {
                    "id": row.id,
                    "gmail_message_id": row.gmail_message_id,
                    "email_subject": row.email_subject,
                    "received_at": row.received_at,
                    "files": files,
                }
                # A1/A7: status se NE mijenja prije obrade — ranije se "approved"
                # commit-ovao ovdje, pa je izuzetak u process_shipment() ostavljao
                # pošiljku trajno zaglavljenu na "approved" bez XML-a/Drive-a
                # (rollback() ne poništava već commit-ovan status). Sad se cijeli
                # rezultat piše u JEDNOJ transakciji tek nakon uspjeha — ako bilo
                # šta padne prije toga, red ostaje "pending" i biva pokupljen opet.
                result = process_shipment(shipment)
                if result.get("status") == "ok":
                    drive_links = result.get("drive_links") or {}
                    row.extracted_items_json = json.dumps(result.get("items", []), ensure_ascii=False)
                    row.drive_folder = result.get("drive_folder", "")
                    row.drive_url = drive_links.get("folder_url") or drive_links.get("xml") or ""
                    row.extracted_at = datetime.utcnow()
                    row.finalized_at = datetime.utcnow()
                    row.status = "approved"
                    db.commit()
                    _log(f"✅ Obrađeno: {row.email_subject}")
                    # Label as processed in Gmail
                    try:
                        from googleapiclient.discovery import build
                        from google_auth import get_credentials
                        from gmail_watcher import mark_gmail_processed
                        service = build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)
                        mark_gmail_processed(service, row.gmail_message_id)
                    except Exception as e:
                        _log(f"Gmail labeling greška: {e}", "warning")
                    # C1-C2: auto-promocija ponavljanja + KTM rebuild, throttled.
                    # run_pipeline() već radi u scheduler-ovom vlastitom pozadinskom
                    # threadu, pa je siguran sinhroni poziv — debounce čini ga jeftinim.
                    try:
                        from auto_learn import run_auto_learn_if_due
                        run_auto_learn_if_due()
                    except Exception as e:
                        _log(f"Auto-learn greška (nekritično): {e}", "warning")
                else:
                    _log(f"⚠ Greška pri obradi: {row.email_subject} — {result.get('error','')}", "error")
                    # status je i dalje "pending" (nikad nije mijenjan) — sljedeći
                    # ciklus će je pokupiti ponovo, ništa eksplicitno ne treba resetovati.
            except Exception as e:
                _log(f"Greška: {row.email_subject}: {e}", "error")
                db.rollback()
    finally:
        db.close()
