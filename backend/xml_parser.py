"""
Parse existing ASYCUDA XML declarations and populate the tariff knowledge base.
"""
import os
import re
import glob
from lxml import etree
from sqlalchemy.orm import Session
from database import TariffRecord, Declaration, SessionLocal, init_db


DECLARATIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "declarations")
OUTPUT_DIR       = os.path.join(os.path.dirname(__file__), "..", "data", "output")


def _extract_names_from_commercial(commercial_desc: str) -> list[str]:
    """
    Extract individual item names from Commercial_Description block.
    Handles all known formats:

    Format A (new, our generator):
        "Official desc...\n-\nNAZIV1-100 KOM\nNAZIV2-50 KOM"

    Format B (old ASYCUDA, single item):
        "Official desc (multiple lines)\nNAZIV QTY KOM"
        "Official desc:\nNAZIV-QTY KOM"

    Returns list of short uppercase product names.
    """
    text = commercial_desc.strip()
    names = []

    # ── Format A: our separator "\n-\n" ──────────────────────────────────
    if "\n-\n" in text:
        names_section = text.split("\n-\n", 1)[1]
        for line in names_section.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^(.+?)-(\d[\d.,]*)\s*KOM\s*$", line, re.IGNORECASE)
            if m:
                name = m.group(1).strip().upper()
                if len(name) >= 2:
                    names.append(name)
        if names:
            return names

    # ── Format B: scan every line for "NAME-QTY KOM" or "NAME QTY KOM" ──
    lines = text.splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # "NAME-123 KOM" or "NAME-1.234 KOM" — dash before number
        m = re.match(r"^(.+?)-(\d[\d.,]*)\s*KOM\s*$", line, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip().upper()
            # Skip if it looks like an official tariff description (lowercase/mixed, long)
            if len(candidate) >= 2 and len(candidate) <= 60:
                names.append(candidate)
            continue
        # "NAME 123 KOM" — space before number
        m2 = re.match(r"^([^\d\n]{2,60}?)\s+(\d[\d.,]*)\s*KOM\s*$",
                      line, re.IGNORECASE)
        if m2:
            candidate = m2.group(1).strip().upper()
            if len(candidate) >= 2:
                names.append(candidate)

    if names:
        return names

    # ── Fallback: take the last non-empty line (usually the product name) ─
    for line in reversed(lines):
        line = line.strip().upper()
        # Skip lines that are just official tariff text (contain digits at end = quantity leftover)
        if line and len(line) >= 2 and len(line) <= 80:
            # Remove trailing quantity/unit if any
            cleaned = re.sub(r"\s+\d[\d.,]*\s*(KOM|KG|M|L|PCE)?\s*$", "", line, flags=re.IGNORECASE).strip()
            if cleaned:
                return [cleaned]

    return [text[:60].upper().strip()]


def parse_xml_file(filepath: str, db: Session) -> int:
    """Parse a single XML declaration file and insert tariff records. Returns count inserted."""
    try:
        tree = etree.parse(filepath)
        root = tree.getroot()
    except Exception as e:
        print(f"[xml_parser] Failed to parse {filepath}: {e}")
        return 0

    filename = os.path.basename(filepath)
    pending_records: list[dict] = []

    # Extract consignee name
    consignee = ""
    consignee_el = root.find(".//Consignee/Name")
    if consignee_el is not None and consignee_el.text:
        consignee = consignee_el.text.strip()

    # Extract declaration date
    decl_date = ""
    date_el = root.find(".//Registration_date")
    if date_el is not None and date_el.text:
        decl_date = date_el.text.strip()

    # Extract currency
    currency = "EUR"
    currency_el = root.find(".//Total_CIF_currency_code")
    if currency_el is not None and currency_el.text:
        currency = currency_el.text.strip()

    items = root.findall(".//Item")
    inserted = 0

    for item in items:
        # Tariff / commodity code
        commodity_el = item.find(".//Commodity_code")
        tariff_code = commodity_el.text.strip() if commodity_el is not None and commodity_el.text else None

        # Official goods description
        official_desc = ""
        desc_el = item.find(".//Description_of_goods")
        if desc_el is not None and desc_el.text:
            official_desc = desc_el.text.strip()

        # Commercial description (invoice text)
        commercial_desc = ""
        comm_el = item.find(".//Commercial_Description")
        if comm_el is not None and comm_el.text:
            commercial_desc = comm_el.text.strip()

        # Country of origin
        country_origin = ""
        country_el = item.find(".//Country_of_origin_code")
        if country_el is not None and country_el.text:
            country_origin = country_el.text.strip()

        # Gross / net weight for unit value estimation
        gross_w = 0.0
        net_w = 0.0
        try:
            gw_el = item.find(".//Gross_weight_itm")
            if gw_el is not None and gw_el.text:
                gross_w = float(gw_el.text.strip())
            nw_el = item.find(".//Net_weight_itm")
            if nw_el is not None and nw_el.text:
                net_w = float(nw_el.text.strip())
        except ValueError:
            pass

        item_price = 0.0
        try:
            price_el = item.find(".//Item_price")
            if price_el is not None and price_el.text:
                item_price = float(price_el.text.strip())
        except ValueError:
            pass

        unit_value = None
        if net_w > 0 and item_price > 0:
            unit_value = round(item_price / net_w, 4)

        if not tariff_code or not commercial_desc:
            continue

        names = _extract_names_from_commercial(commercial_desc)
        for name in names:
            pending_records.append({
                "description": name.lower(),
                "tariff_code": tariff_code,
                "official_desc": official_desc,
                "country_origin": country_origin,
                "unit_value": unit_value,
                "source_file": filename,
            })

    # Bulk duplicate check: fetch existing (description, tariff_code) pairs once
    if pending_records:
        existing_pairs = {
            (row[0], row[1])
            for row in db.query(TariffRecord.description, TariffRecord.tariff_code).all()
        }
        for rec in pending_records:
            key = (rec["description"], rec["tariff_code"])
            if key not in existing_pairs:
                db.add(TariffRecord(**rec))
                existing_pairs.add(key)
                inserted += 1

    # Store declaration summary
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        xml_content = f.read()
    decl = Declaration(
        filename=filename,
        consignee=consignee,
        declaration_date=decl_date,
        currency=currency,
        total_items=len(items),
        xml_content=xml_content,
    )
    db.add(decl)
    db.commit()
    return inserted


def load_all_declarations(include_output: bool = True) -> dict:
    """
    Load only NEW XML files from data/declarations/ (and optionally data/output/).
    Files already recorded in the Declaration table are skipped entirely — no re-parsing.
    Returns {"files_scanned": N, "files_new": N, "records": N}.
    """
    init_db()
    db = SessionLocal()
    dirs = [DECLARATIONS_DIR]
    if include_output:
        dirs.append(OUTPUT_DIR)
    try:
        # Build set of already-imported filenames in one query
        imported = {row[0] for row in db.query(Declaration.filename).all()}

        total_scanned = 0
        total_new = 0
        total_records = 0
        for d in dirs:
            for filepath in sorted(glob.glob(os.path.join(d, "*.xml"))):
                total_scanned += 1
                fname = os.path.basename(filepath)
                if fname in imported:
                    continue   # already imported — skip without opening the file
                count = parse_xml_file(filepath, db)
                total_new += 1
                total_records += count
                if count > 0:
                    print(f"[xml_parser] {fname}: {count} records")

        if total_records > 0:
            print(f"[xml_parser] Done. {total_new} new files, {total_records} records added.")
        else:
            print(f"[xml_parser] Sve deklaracije već uvežene ({total_scanned} fajlova).")
        return {"files_scanned": total_scanned, "files_new": total_new, "records": total_records}
    finally:
        db.close()


def search_tariff(query: str, db: Session, limit: int = 10) -> list:
    """
    Search tariff knowledge base by description keywords.
    Scores by word overlap ratio (not just count) to handle short vs long names fairly.
    """
    words = set(query.lower().split())
    if not words:
        return []

    results = db.query(TariffRecord).all()
    scored = []
    for r in results:
        r_words = set(r.description.split())
        overlap = len(words & r_words)
        if overlap == 0:
            continue
        # Jaccard-like: overlap / union — rewards exact/close matches
        score = overlap / len(words | r_words)
        scored.append((score, r))

    scored.sort(key=lambda x: -x[0])
    # De-duplicate by tariff_code — keep best score per code
    seen_codes: dict[str, float] = {}
    deduped = []
    for score, r in scored:
        if r.tariff_code not in seen_codes:
            seen_codes[r.tariff_code] = score
            deduped.append((score, r))
    return [r for _, r in deduped[:limit]]


if __name__ == "__main__":
    load_all_declarations()
