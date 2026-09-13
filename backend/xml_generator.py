"""
Generate ASYCUDA World XML for BiH customs declarations.
Format matches el nedex 2602033.xml and okovi.xml (confirmed working files).
"""
import re
from lxml import etree
from datetime import date
from typing import Optional

from classification_utils import is_suspect as _is_suspect

# Heading safety-net: catches future PDF parser artifacts that slipped past the cleaner
_PURE_NUMBER_HEADING_RE = re.compile(r"^[\d\s\.,]+$")


def _sub(parent, tag: str, text: Optional[str] = None, attrib: dict = None) -> etree._Element:
    el = etree.SubElement(parent, tag, attrib or {})
    if text is not None:
        el.text = str(text)
    return el


def _null(parent, tag: str) -> etree._Element:
    """Element with <null/> child."""
    el = etree.SubElement(parent, tag)
    etree.SubElement(el, "null")
    return el


def _empty(parent, tag: str) -> etree._Element:
    """Self-closing empty element."""
    return etree.SubElement(parent, tag)


def _wrap_text(text: str, width: int = 55) -> str:
    """Wrap text at word boundary where possible, hard-cut if word exceeds width."""
    lines = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        current = ""
        for word in words:
            if not current:
                current = word[:width]
                remainder = word[width:]
                while remainder:
                    lines.append(current)
                    current = remainder[:width]
                    remainder = remainder[width:]
            elif len(current) + 1 + len(word) <= width:
                current += " " + word
            else:
                lines.append(current)
                current = word[:width]
                remainder = word[width:]
                while remainder:
                    lines.append(current)
                    current = remainder[:width]
                    remainder = remainder[width:]
        if current:
            lines.append(current)
    return "\n".join(lines) if lines else ""


def _wrap_address(text: str, width: int = 35) -> str:
    """Word-wrap address line at 35 chars (ASYCUDA field 2 limit)."""
    return _wrap_text(text, width)


def _format_exporter(name: str, city: str, country_code: str, street: str) -> str:
    """Format exporter address for field 2: NAME / CITY/COUNTRY / STREET."""
    parts = []
    if name:
        parts.append(_wrap_address(name.upper()))
    city_country = ""
    if city and country_code:
        city_country = f"{city.upper()} / {_country_name(country_code)}"
    elif city:
        city_country = city.upper()
    elif country_code:
        city_country = _country_name(country_code)
    if city_country:
        parts.append(_wrap_address(city_country))
    if street:
        parts.append(_wrap_address(street.upper()))
    return "\n".join(parts)


def _currency_name(code: str) -> str:
    names = {
        "EUR": "EURO", "USD": "US DOLAR", "BAM": "KONVERTIBILNA MARKA",
        "GBP": "BRITANSKA FUNTA", "CHF": "ŠVICARSKI FRANAK",
        "CNY": "KINESKI YUAN", "TRY": "TURSKA LIRA",
    }
    return names.get((code or "").upper(), code or "")


def _resolve_heading(heading_bs: str, description_bs: str) -> str:
    """Return safe heading text for Commercial_Description.

    Falls back to description_bs (without leading dashes) when heading_bs
    looks like a stale PDF artifact: empty, fewer than 3 chars, or pure number.
    """
    h = (heading_bs or "").strip()
    if not h or len(h) < 3 or _PURE_NUMBER_HEADING_RE.match(h):
        return re.sub(r"^[\-\s]+", "", description_bs or "").strip()
    return h


def _resolve_description(official_desc: str, tariff_code: str, db) -> str:
    """Return safe text for Description_of_goods.

    Safety net for items that reach the generator without a fresh DB-derived
    description. Falls back to a live official_tariffs.description_bs lookup
    when the incoming text is empty or contains the old em-dash join format
    (legacy artifact from pre-reimport generations).
    """
    text = (official_desc or "").strip()
    is_legacy = " — " in text
    if (text and not is_legacy) or db is None or not tariff_code:
        return text
    code = re.sub(r"[^\d]", "", tariff_code)[:8]
    if not code:
        return text
    db_code = code.ljust(10, "0")
    from database import OfficialTariff
    row = db.query(OfficialTariff).filter_by(code=db_code).first()
    if row and row.description_bs:
        return row.description_bs.strip()
    return text


def _merge_names(names: list[tuple]) -> list[tuple]:
    """Merge items with identical names by summing quantities."""
    merged: dict[str, list] = {}
    order: list[str] = []
    for name, qty in names:
        key = name.upper().strip()
        if key in merged:
            merged[key][1] += float(qty)
        else:
            merged[key] = [name, float(qty)]
            order.append(key)
    return [(merged[k][0], merged[k][1]) for k in order]


def _names_str(names: list[tuple], upper: bool = False) -> str:
    """Build the names section: NAME QTY KOM per line, merging duplicates."""
    parts = []
    for name, qty in _merge_names(names):
        qty_f = float(qty)
        qty_s = str(int(qty_f)) if qty_f == int(qty_f) else str(qty_f)
        n = name.upper() if upper else name
        parts.append(f"{n} {qty_s} KOM")
    return "\n".join(parts)


def _wrap_items_inline(names: list[tuple], width: int = 55) -> list[str]:
    """Comma-separated 'NAME QTY KOM' tokens packed into lines of `width` chars."""
    entries = []
    for name, qty in _merge_names(names):
        qty_f = float(qty)
        qty_s = str(int(qty_f)) if qty_f == int(qty_f) else str(qty_f)
        entries.append(f"{name.upper()} {qty_s} KOM")

    lines: list[str] = []
    current = ""
    for i, entry in enumerate(entries):
        is_last = (i == len(entries) - 1)
        token = entry if is_last else entry + ","
        if not current:
            current = token[:width]
        elif len(current) + 1 + len(token) <= width:
            current += " " + token
        else:
            lines.append(current)
            current = token[:width]
    if current:
        lines.append(current)
    return lines


def _names_fitting_in_lines(names: list[tuple], max_lines: int,
                             width: int = 55) -> tuple[list, list]:
    """Return (fits, remainder): max prefix of names that wraps in <= max_lines."""
    if not names:
        return [], []
    for i in range(1, len(names) + 1):
        if len(_wrap_items_inline(names[:i], width)) > max_lines:
            # i names overflow — take i-1 (but always at least 1)
            cut = max(1, i - 1)
            return names[:cut], names[cut:]
    return names, []


def _hard_wrap(text: str, width: int) -> list[str]:
    """Hard-cut every `width` characters (matches špediter's ASYCUDA tool behaviour
    seen in ispravno.xml — wraps mid-word at column 55)."""
    return [text[i:i + width] for i in range(0, len(text), width)] or [""]


def _ensure_trailing_colon(lines: list[str], width: int) -> list[str]:
    """Ensure the last heading line ends with ':' even after truncation."""
    if not lines:
        return lines
    last = lines[-1].rstrip(" .:,;")
    if len(last) >= width:
        last = last[: width - 1]
    lines[-1] = last + ":"
    return lines


def _format_commercial_desc(heading: str, names: list[tuple],
                             max_lines: int = 3, width: int = 55,
                             include_heading: bool = True) -> str:
    """
    Format Commercial_Description for field 31 (mixed case).

    Layout:
      <heading wrapped at hard 55-char boundary, last line ends with ':'>
      <item names: NAME1 QTY KOM, NAME2 QTY KOM, ...>

    Heading gets max_lines-1 lines (items get at least 1). When items overflow,
    items get more lines and heading shrinks. include_heading=False skips heading.
    """
    item_lines = _wrap_items_inline(names, width)

    if not include_heading or not heading:
        return "\n".join(item_lines[:max_lines])

    heading_with_colon = heading.rstrip(" .:,;") + ":"
    all_heading_lines = _hard_wrap(heading_with_colon, width)

    items_needed = len(item_lines)
    max_item_lines = max_lines - 1          # items get at most this many
    items_used = min(items_needed, max_item_lines)
    heading_used = max_lines - items_used   # heading fills the rest (≥1)
    desc_lines = _ensure_trailing_colon(all_heading_lines[:heading_used], width)
    return "\n".join(desc_lines + item_lines[:items_used])


def _format_container_desc(heading: str, names: list[tuple],
                            max_lines: int = 3, width: int = 55,
                            include_heading: bool = True) -> str:
    """Format Container/Goods_description for box 31 (UPPERCASE heading,
    hard-wrap at 55 chars)."""
    item_lines = _wrap_items_inline(names, width)

    if not include_heading or not heading:
        return "\n".join(item_lines[:max_lines])

    heading_with_colon = heading.upper().rstrip(" .:,;") + ":"
    all_heading_lines = _hard_wrap(heading_with_colon, width)

    items_needed = len(item_lines)
    max_item_lines = max_lines - 1
    items_used = min(items_needed, max_item_lines)
    heading_used = max_lines - items_used
    desc_lines = _ensure_trailing_colon(all_heading_lines[:heading_used], width)
    return "\n".join(desc_lines + item_lines[:items_used])


def _consolidate_by_tariff(items: list[dict]) -> list[dict]:
    """
    Group items that share the same tariff code into one ASYCUDA item.
    Summed: quantity, value, gross_weight, net_weight.
    The '_names' key holds [(name, qty), ...] for all original items in the group.
    Items without a tariff code are kept separate (never merged).

    Sumnjive stavke (_is_suspect) se TAKOĐE nikad ne stapaju — svaka dobija vlastito
    naimenovanje s istim tarifnim brojem, da je špediter vidi odvojeno na SAD-u i može
    je pregledati. Da se stopi s pouzdanima, nesigurna klasifikacija bi nestala u grupi.
    """
    groups: list[list[dict]] = []
    code_index: dict[str, int] = {}

    for item in items:
        code = (item.get("tariff_code") or "").strip()
        mergeable = bool(code) and not _is_suspect(item)
        if mergeable and code in code_index:
            groups[code_index[code]].append(item)
        else:
            idx = len(groups)
            groups.append([item])
            if mergeable:
                code_index[code] = idx

    result = []
    for group in groups:
        merged = dict(group[0])
        if len(group) > 1:
            merged["quantity"]     = sum(float(i.get("quantity", 1))     for i in group)
            merged["value"]        = sum(float(i.get("value", 0))        for i in group)
            merged["gross_weight"] = sum(float(i.get("gross_weight", 0)) for i in group)
            merged["net_weight"]   = sum(float(i.get("net_weight", 0))   for i in group)
            # Use supplementary_unit from any item that has it
            for i in group:
                if i.get("supplementary_unit"):
                    merged["supplementary_unit"] = i["supplementary_unit"]
                    break
        merged["_names"] = [(i.get("name", ""), float(i.get("quantity", 1)))
                            for i in group]
        result.append(merged)
    return result


def _split_overflowing_groups(consolidated: list[dict],
                               max_lines: int = 3, width: int = 55) -> list[dict]:
    """
    Split merged groups when item names overflow field 31.

    Pravila:
    - Svako naimenovanje (uključujući overflow) uvijek ima heading na liniji 1.
    - Heading rezerviše 1 red → stavke dobijaju max max_lines-1 redova.
    - Ako se sve stavke ne mogu smjestiti → pravi se novo naimenovanje s istim pravilima.
    - Vrijednosti/težine dijele se proporcionalno broju item-name unosa u chunku.
    """
    result = []
    for group in consolidated:
        all_names = group.get("_names", [(group.get("name", ""),
                                          float(group.get("quantity", 1)))])
        total_names = len(all_names)
        total_qty   = float(group.get("quantity", 0))
        total_val   = float(group.get("value", 0))
        total_gross = float(group.get("gross_weight", 0))
        total_net   = float(group.get("net_weight", 0))

        # Svakom naimenovanju heading zauzima 1 red → stavke dobijaju max_lines-1 redova
        item_budget = max_lines - 1
        chunks: list[list] = []
        remaining = all_names[:]

        while remaining:
            fits, rest = _names_fitting_in_lines(remaining, item_budget, width)
            chunks.append(fits)
            remaining = rest

        if len(chunks) == 1:
            result.append(group)
            continue

        for chunk_names in chunks:
            frac = len(chunk_names) / total_names
            sub = dict(group)
            sub["_names"]           = chunk_names
            sub["_include_heading"] = True   # uvijek heading na liniji 1
            sub["quantity"]         = round(total_qty   * frac, 3)
            sub["value"]            = round(total_val   * frac, 2)
            sub["gross_weight"]     = round(total_gross * frac, 3)
            sub["net_weight"]       = round(total_net   * frac, 3)
            result.append(sub)

    return result


def _add_attached_doc(parent, code: str, name: str, reference: str,
                      from_rule: str = "", doc_date: str = ""):
    """Add a single <Attached_documents> block."""
    ad = _sub(parent, "Attached_documents")
    _sub(ad, "Attached_document_code", code)
    _sub(ad, "Attached_document_name", name)
    _sub(ad, "Attached_document_reference", reference)
    if from_rule:
        _sub(ad, "Attached_document_from_rule", from_rule)
    if doc_date:
        _sub(ad, "Attached_document_date", doc_date)


def generate_asycuda_xml(
    items: list[dict],
    consignee_name: str = "",
    consignee_address: str = "",
    consignee_country: str = "BA",
    consignee_code: str = "",
    exporter_name: str = "",
    exporter_city: str = "",
    exporter_street: str = "",
    exporter_country: str = "CN",
    currency: str = "EUR",
    currency_rate: float = 0.0,
    declaration_date: str = "",
    office_code: str = "BA010301",
    office_name: str = "CI Tuzla",
    transport_mode: str = "30",
    transport_identity: str = "",       # truck plates, e.g. "34NF1924 / 34NF1914"
    transport_nationality: str = "BA",  # nationality of transport vehicle
    border_office_code: str = "",       # e.g. "BA097098"
    border_office_name: str = "",       # e.g. "CR/GP Rača"
    transit_doc: str = "",              # full T1 ref, e.g. "26BA097098P37410J0"
    container_number: str = "",
    delivery_terms: str = "",   # uslovi isporuke — uvijek prazno po defaultu
    delivery_place: str = "",
    declarant_code: str = "",
    declarant_name: str = "",
    declarant_ref: str = "",
    invoice_number: str = "",
    preference_code: str = "",
    eur1_reference: str = "",
    external_freight: float = 0.0,          # freight amount in foreign currency
    external_freight_currency: str = "EUR", # currency of external freight
    internal_freight: float = 0.0,          # internal freight in BAM
    insurance: float = 0.0,                 # insurance in BAM
    packages_count: int = 0,               # total cartons/CTNS from packing list or B/L
    output_path: str = "",
    db=None,                                # optional Session for description safety-net lookup
) -> str:
    if not declaration_date:
        declaration_date = date.today().strftime("%Y%m%d")

    # Auto-derive preference code from exporter country when not provided
    actual_pref = preference_code or ("TRPR" if exporter_country == "TR" else "")

    # Consolidate items sharing the same tariff code into single ASYCUDA items
    items = _consolidate_by_tariff(items)
    # Split groups with too many item names so field 31 doesn't overflow
    items = _split_overflowing_groups(items)

    year_2d = declaration_date[2:4] if len(declaration_date) >= 4 else "26"
    total_gross = sum(float(i.get("gross_weight", 0)) for i in items)
    total_value = sum(float(i.get("value", 0)) for i in items)
    total_qty = sum(float(i.get("quantity", 1)) for i in items)
    # Use CTNS/cartons count for package fields; fall back to piece count only if not provided
    pkg_total = packages_count if packages_count > 0 else int(total_qty)

    root = etree.Element("ASYCUDA")

    # ── Assessment_notice ──────────────────────────────────────────────────
    an = _sub(root, "Assessment_notice")
    for _ in items:
        _empty(an, "Item_tax_total")

    # ── Global_taxes ──────────────────────────────────────────────────────
    gt = _sub(root, "Global_taxes")
    for _ in range(8):
        _empty(gt, "Global_tax_item")

    # ── Property ──────────────────────────────────────────────────────────
    prop = _sub(root, "Property")
    _sub(prop, "Sad_flow", "I")
    forms = _sub(prop, "Forms")
    _sub(forms, "Number_of_the_form", "1")
    _sub(forms, "Total_number_of_forms", "1")
    nbers = _sub(prop, "Nbers")
    _null(nbers, "Number_of_loading_lists")
    _sub(nbers, "Total_number_of_items", str(len(items)))
    _null(prop, "Place_of_declaration")
    _empty(prop, "Date_of_declaration")
    _sub(prop, "Selected_page", "1")

    # ── Identification ────────────────────────────────────────────────────
    ident = _sub(root, "Identification")
    os_el = _sub(ident, "Office_segment")
    _sub(os_el, "Customs_clearance_office_code", office_code)
    _sub(os_el, "Customs_Clearance_office_name", office_name)
    type_el = _sub(ident, "Type")
    _sub(type_el, "Type_of_declaration", "IM")
    _sub(type_el, "Type_of_Declaration_X", "A")
    _sub(type_el, "Declaration_gen_procedure_code", "H")
    _null(type_el, "Type_of_transit_document")
    _null(ident, "Manifest_reference_number")

    reg = _sub(ident, "Registration")
    _null(reg, "Serial_number")
    _null(reg, "Number")
    _empty(reg, "Date")

    assess = _sub(ident, "Assessment")
    _null(assess, "Serial_number")
    _empty(assess, "Number")   # empty, not null — matches reference
    _empty(assess, "Date")

    rec = _sub(ident, "receipt")
    _null(rec, "Serial_number")
    _empty(rec, "Number")      # empty, not null
    _empty(rec, "Date")

    # ── Traders ────────────────────────────────────────────────────────────
    traders = _sub(root, "Traders")
    exp = _sub(traders, "Exporter")
    _empty(exp, "Exporter_code")
    formatted_exporter = _format_exporter(exporter_name, exporter_city, exporter_country, exporter_street)
    if formatted_exporter.strip():
        _sub(exp, "Exporter_name", formatted_exporter)
    else:
        _null(exp, "Exporter_name")

    con = _sub(traders, "Consignee")
    if consignee_code:
        _sub(con, "Consignee_code", consignee_code)
    else:
        _null(con, "Consignee_code")
    full_name = consignee_name + ("\n" + consignee_address if consignee_address else "")
    if full_name.strip():
        _sub(con, "Consignee_name", full_name)
    else:
        _null(con, "Consignee_name")

    fin0 = _sub(traders, "Financial")
    _empty(fin0, "Financial_code")
    _null(fin0, "Financial_name")

    # ── Representative ────────────────────────────────────────────────────
    rep = _sub(root, "Representative")
    _sub(rep, "Representative_code", "2")

    # ── Declarant ────────────────────────────────────────────────────────
    decl = _sub(root, "Declarant")
    _sub(decl, "Declarant_code", declarant_code)
    _sub(decl, "Declarant_name", declarant_name or declarant_code)
    _null(decl, "Declarant_representative")
    ref_el = _sub(decl, "Reference")
    ref_num = declarant_ref or invoice_number or ""
    if ref_num:
        _sub(ref_el, "Number", ref_num)
    else:
        _null(ref_el, "Number")

    # ── General_information ───────────────────────────────────────────────
    gi = _sub(root, "General_information")
    country_el = _sub(gi, "Country")
    _null(country_el, "Country_first_destination")
    _null(country_el, "Trading_country")
    exp_c = _sub(country_el, "Export")
    if exporter_country:
        _sub(exp_c, "Export_country_code", exporter_country)
        _sub(exp_c, "Export_country_name", _country_name(exporter_country))
    else:
        _null(exp_c, "Export_country_code")
        _null(exp_c, "Export_country_name")
    _empty(exp_c, "Export_country_region")
    dest_c = _sub(country_el, "Destination")
    _null(dest_c, "Destination_country_code")
    _null(dest_c, "Destination_country_name")
    _empty(dest_c, "Destination_country_region")
    _sub(country_el, "Country_of_origin_name", _country_name(exporter_country))
    _sub(gi, "Value_details", f"{total_value:.2f}")
    _empty(gi, "CAP")
    _null(gi, "Additional_information")
    _null(gi, "Comments_free_text")

    # ── Transport ─────────────────────────────────────────────────────────
    transport = _sub(root, "Transport")
    mot = _sub(transport, "Means_of_transport")
    dai = _sub(mot, "Departure_arrival_information")
    _sub(dai, "Identity", transport_identity or ",")
    _sub(dai, "Nationality", transport_nationality)
    bi = _sub(mot, "Border_information")
    _sub(bi, "Identity", transport_identity or ",")
    _sub(bi, "Nationality", transport_nationality)
    _sub(bi, "Mode", transport_mode)
    _sub(mot, "Inland_mode_of_transport", transport_mode)

    # Container_flag: "true" only when an actual container number is provided
    _sub(transport, "Container_flag", "true" if container_number else "false")
    dt = _sub(transport, "Delivery_terms")
    if delivery_terms:
        _sub(dt, "Code", delivery_terms)
    else:
        _null(dt, "Code")
    if delivery_place:
        _sub(dt, "Place", delivery_place)
    else:
        _null(dt, "Place")
    _null(dt, "Situation")

    bo = _sub(transport, "Border_office")
    if border_office_code:
        _sub(bo, "Code", border_office_code)
        _sub(bo, "Name", border_office_name)
    else:
        _null(bo, "Code")
        _null(bo, "Name")

    pol = _sub(transport, "Place_of_loading")
    _null(pol, "Code")
    _null(pol, "Name")
    _empty(pol, "Country")

    _null(transport, "Location_of_goods")

    # ── Financial ─────────────────────────────────────────────────────────
    financial = _sub(root, "Financial")
    ft = _sub(financial, "Financial_transaction")
    _sub(ft, "code1", "1")   # "1" matches reference
    _sub(ft, "code2", "1")
    bank = _sub(financial, "Bank")
    _null(bank, "Code")
    _null(bank, "Name")
    _null(bank, "Branch")
    _null(bank, "Reference")
    terms = _sub(financial, "Terms")
    _null(terms, "Code")
    _null(terms, "Description")
    _empty(financial, "Total_invoice")
    _null(financial, "Deffered_payment_reference")
    _sub(financial, "Mode_of_payment", "")
    amounts = _sub(financial, "Amounts")
    _empty(amounts, "Total_manual_taxes")
    _sub(amounts, "Global_taxes", "0.0")
    _sub(amounts, "Totals_taxes", "")
    guar = _sub(financial, "Guarantee")
    _null(guar, "Name")
    _sub(guar, "Amount", "0.0")
    _empty(guar, "Date")
    exc = _sub(guar, "Excluded_country")
    _null(exc, "Code")
    _null(exc, "Name")

    # ── Warehouse ─────────────────────────────────────────────────────────
    wh = _sub(root, "Warehouse")
    _null(wh, "Identification")
    _empty(wh, "Delay")

    # ── Transit ───────────────────────────────────────────────────────────
    transit = _sub(root, "Transit")
    princ = _sub(transit, "Principal")
    _null(princ, "Code")
    _null(princ, "Name")
    _null(princ, "Representative")
    sig = _sub(transit, "Signature")
    _null(sig, "Place")
    _empty(sig, "Date")
    tdest = _sub(transit, "Destination")
    _null(tdest, "Office")
    _null(tdest, "Country")
    seals = _sub(transit, "Seals")
    _empty(seals, "Number")
    _null(seals, "Identity")
    _null(transit, "Result_of_control")
    _empty(transit, "Time_limit")
    _null(transit, "Officer_name")

    # ── Valuation (global) ────────────────────────────────────────────────
    valuation = _sub(root, "Valuation")
    _sub(valuation, "Calculation_working_mode", "0")
    wt = _sub(valuation, "Weight")
    _sub(wt, "Gross_weight", f"{total_gross:.1f}" if total_gross else "")
    _empty(valuation, "Total_cost")
    _empty(valuation, "Total_CIF")

    gs_inv = _sub(valuation, "Gs_Invoice")
    _sub(gs_inv, "Amount_national_currency", "")
    _sub(gs_inv, "Amount_foreign_currency", f"{total_value:.2f}")
    _sub(gs_inv, "Currency_code", currency)
    _sub(gs_inv, "Currency_name", _currency_name(currency))
    _sub(gs_inv, "Currency_rate", f"{currency_rate:.6f}" if currency_rate else "")

    # Gs_external_freight — foreign currency (e.g. EUR), convert to BAM
    ext_rate = currency_rate if external_freight_currency == currency and currency_rate else 1.0
    ext_bam_total = round(external_freight * ext_rate, 2)
    gs_ext = _sub(valuation, "Gs_external_freight")
    if external_freight:
        _sub(gs_ext, "Amount_national_currency", f"{ext_bam_total:.2f}")
        _sub(gs_ext, "Amount_foreign_currency", f"{external_freight:.2f}")
        _sub(gs_ext, "Currency_code", external_freight_currency)
        _sub(gs_ext, "Currency_name", _currency_name(external_freight_currency))
        _sub(gs_ext, "Currency_rate", f"{ext_rate:.6f}")
    else:
        _sub(gs_ext, "Amount_national_currency", "")
        _sub(gs_ext, "Amount_foreign_currency", "")
        _sub(gs_ext, "Currency_code", "")
        _sub(gs_ext, "Currency_name", "Nema stranih valuta")
        _sub(gs_ext, "Currency_rate", "")
    # Gs_internal_freight — already in BAM
    gs_int = _sub(valuation, "Gs_internal_freight")
    if internal_freight:
        _sub(gs_int, "Amount_national_currency", f"{internal_freight:.2f}")
        _sub(gs_int, "Amount_foreign_currency", "")
        _sub(gs_int, "Currency_code", "")
        _sub(gs_int, "Currency_name", "Nema stranih valuta")
        _sub(gs_int, "Currency_rate", "")
    else:
        _sub(gs_int, "Amount_national_currency", "")
        _sub(gs_int, "Amount_foreign_currency", "")
        _sub(gs_int, "Currency_code", "")
        _sub(gs_int, "Currency_name", "Nema stranih valuta")
        _sub(gs_int, "Currency_rate", "")
    # Gs_insurance — already in BAM
    gs_ins = _sub(valuation, "Gs_insurance")
    if insurance:
        _sub(gs_ins, "Amount_national_currency", f"{insurance:.2f}")
        _sub(gs_ins, "Amount_foreign_currency", "")
        _sub(gs_ins, "Currency_code", "")
        _sub(gs_ins, "Currency_name", "Nema stranih valuta")
        _sub(gs_ins, "Currency_rate", "")
    else:
        _sub(gs_ins, "Amount_national_currency", "")
        _sub(gs_ins, "Amount_foreign_currency", "")
        _sub(gs_ins, "Currency_code", "")
        _sub(gs_ins, "Currency_name", "Nema stranih valuta")
        _sub(gs_ins, "Currency_rate", "")
    gs_oth = _sub(valuation, "Gs_other_cost")
    _sub(gs_oth, "Amount_national_currency", "")
    _sub(gs_oth, "Amount_foreign_currency", "")
    _sub(gs_oth, "Currency_code", "")
    _sub(gs_oth, "Currency_name", "Nema stranih valuta")
    _sub(gs_oth, "Currency_rate", "")

    gs_ded = _sub(valuation, "Gs_deduction")
    _sub(gs_ded, "Amount_national_currency", "")
    _sub(gs_ded, "Amount_foreign_currency", "0.00")
    _sub(gs_ded, "Currency_code", "")
    _sub(gs_ded, "Currency_name", "Nema stranih valuta")
    _sub(gs_ded, "Currency_rate", "")

    total_el = _sub(valuation, "Total")
    _empty(total_el, "Total_invoice")
    _empty(total_el, "Total_weight")

    # ── Container blocks — only when a real container number is present
    if container_number:
        for idx, item in enumerate(items, start=1):
            gross_w = float(item.get("gross_weight", 0))
            official_desc = item.get("official_desc", "")
            heading = _resolve_heading(item.get("heading_bs", ""), official_desc)
            group_names = item.get("_names", [(item.get("name", ""),
                                               float(item.get("quantity", 1)))])

            ct = _sub(root, "Container")
            _sub(ct, "Item_Number", str(idx))
            _sub(ct, "Container_identity", container_number)
            _sub(ct, "Container_type", "B0")
            _sub(ct, "Empty_full_indicator", "BBE")
            _sub(ct, "Gross_weight", f"{gross_w:.1f}" if gross_w else "0")
            _sub(ct, "Goods_description", _format_container_desc(
                heading, group_names,
                include_heading=item.get("_include_heading", True)))
            _sub(ct, "Packages_type", "PK")
            _sub(ct, "Packages_number", str(pkg_total) if idx == 1 else "0")
            _sub(ct, "Packages_weight", f"{gross_w:.1f}" if gross_w else "0")

    # ── Item blocks ────────────────────────────────────────────────────────
    for idx, item in enumerate(items, start=1):
        item_el = _sub(root, "Item")

        qty = float(item.get("quantity", 1))
        qty_str = str(int(qty)) if qty == int(qty) else str(qty)
        item_value = float(item.get("value", 0))
        gross_w = float(item.get("gross_weight", 0))
        net_w = float(item.get("net_weight", 0))
        official_desc = item.get("official_desc", "")
        name = item.get("name", "")
        group_names = item.get("_names", [(name, qty)])

        # Attached_documents — item 1 gets full doc set
        # Preferential (Turkey EUR-1): VOZ + EX + N730 + N380 + DIS + DV1
        # Standard: OST + N380 + DIS + DV1
        # Every item gets PE1 when eur1_reference is provided
        if idx == 1:
            if actual_pref or eur1_reference:
                _add_attached_doc(item_el, "VOZ", "Vozarina", "VOZ")
                _add_attached_doc(item_el, "EX", "Izvozna deklaracija", "EX")
                _add_attached_doc(
                    item_el, "N730", "Tovarni list CMR", "N730"
                )
            else:
                _add_attached_doc(
                    item_el, "OST", "Posebna dokumenta za carinjenje",
                    "PAKING LISTA"
                )
            _add_attached_doc(
                item_el, "N380", "Faktura komercijalna (račun)",
                invoice_number or "—", from_rule="1"
            )
            _add_attached_doc(
                item_el, "DIS", "Dispozicija",
                "GENERALNO OVLAŠTENJE", from_rule="1"
            )
            _add_attached_doc(
                item_el, "DV1", "Prijava o carinskoj vrijednosti",
                "DV1", from_rule="1"
            )
        if eur1_reference:
            _add_attached_doc(
                item_el, "PE1", "Potvrda o porijeklu EUR.1",
                eur1_reference, from_rule="1"
            )

        # Sumnjiv tarifni broj (AI low/medium) → marker za špediterov pregled.
        # Marker ide SAMO u polja bez pravne težine: Marks2 (slobodan tekst, vidljiv u
        # box 31) i Free_text_1 (per-item, inače uvijek prazan). Country_of_origin_code,
        # HScode i Kind_of_packages_code se NE diraju — ta polja određuju carinsku stopu.
        suspect = _is_suspect(item)

        # Packages — box 31
        pkg = _sub(item_el, "Packages")
        # Item 1: total cartons/CTNS; items 2+: 0
        pkg_count = str(pkg_total) if idx == 1 else "0"
        _sub(pkg, "Number_of_packages", pkg_count)
        _sub(pkg, "Marks1_of_packages", "1")
        _sub(pkg, "Marks2_of_packages",
             "POŠILJKA - PROVJERITI TARIFU" if suspect else "POŠILJKA")
        _sub(pkg, "Kind_of_packages_code", "PK")
        _sub(pkg, "Kind_of_packages_name", "Pakovanje")

        # IncoTerms (box 20 per item) — place goes on every item
        inco = _sub(item_el, "IncoTerms")
        if delivery_terms:
            _sub(inco, "Code", delivery_terms)
        else:
            _null(inco, "Code")
        if delivery_place:
            _sub(inco, "Place", delivery_place)
        else:
            _null(inco, "Place")

        # Tarification — boxes 33, 37, 41, 42, 43
        tarif = _sub(item_el, "Tarification")
        _null(tarif, "Tarification_data")

        hscode = _sub(tarif, "HScode")
        raw_code = item.get("tariff_code", "").replace(" ", "").replace(".", "")
        # Box 33: 8-digit commodity code
        commodity_code = raw_code[:8] if len(raw_code) >= 8 else raw_code.ljust(8, "0")
        # Precision_1 always "000" (box 33 extension)
        precision_1 = (raw_code[8:] + "000")[:3] if len(raw_code) > 8 else "000"
        _sub(hscode, "Commodity_code", commodity_code)
        _sub(hscode, "Precision_1", precision_1)
        _null(hscode, "Precision_2")
        _null(hscode, "Precision_3")
        _null(hscode, "Precision_4")

        if actual_pref:
            _sub(tarif, "Preference_code", actual_pref)
        else:
            _null(tarif, "Preference_code")
        # Box 37: 4000 (import) + 000 (national)
        _sub(tarif, "Extended_customs_procedure", "4000")
        _sub(tarif, "National_customs_procedure", "000")
        _null(tarif, "Quota_code")
        quota = _sub(tarif, "Quota")
        _null(quota, "QuotaCode")
        _null(quota, "QuotaId")
        qi = _sub(quota, "QuotaItem")
        _null(qi, "ItmNbr")

        # Box 41: Supplementary units
        SU_NAMES = {
            "PCE": "(kd) Broj komada",
            "MTK": "(m2) Kvadratni metri",
            "MTQ": "(m3) Kubni metri",
            "MTR": "(m) Metri",
            "LTR": "(l) Litri",
            "PR":  "(par) Parovi",
            "KGM": "(kg) Kilogrami",
            "GRM": "(g) Grami",
        }
        item_su = item.get("supplementary_unit", "")
        # Use supplementary_quantity from invoice if available, else fall back to piece count
        su_raw_qty = float(item.get("supplementary_quantity") or 0)
        su_qty = su_raw_qty if su_raw_qty > 0 else qty
        su_qty_str = f"{int(su_qty)}.0" if su_qty == int(su_qty) else f"{su_qty:.1f}"
        write_su = bool(item_su) and su_qty > 0
        su0 = _sub(tarif, "Supplementary_unit")
        if write_su:
            _sub(su0, "Suppplementary_unit_code", item_su)
            _sub(su0, "Suppplementary_unit_name", SU_NAMES.get(item_su, item_su))
            _sub(su0, "Suppplementary_unit_quantity", su_qty_str)
        else:
            _null(su0, "Suppplementary_unit_code")
            _empty(su0, "Suppplementary_unit_name")
            _empty(su0, "Suppplementary_unit_quantity")
        for _ in range(2):
            su = _sub(tarif, "Supplementary_unit")
            _null(su, "Suppplementary_unit_code")
            _empty(su, "Suppplementary_unit_name")
            _empty(su, "Suppplementary_unit_quantity")

        # Box 42: Item price
        _sub(tarif, "Item_price", f"{item_value:.2f}")
        # Box 43: Valuation method
        _sub(tarif, "Valuation_method_code", "1")
        # Value_item = ext_freight_bam + int_freight_bam + insurance_bam + 0.00 - deduction
        alpha = (item_value / total_value) if total_value > 0 else (1.0 / len(items))
        vi_ext = round(ext_bam_total * alpha, 2)
        vi_int = round(internal_freight * alpha, 2)
        vi_ins = round(insurance * alpha, 2)
        _sub(tarif, "Value_item", f"{vi_ext:.2f}+{vi_int:.2f}+{vi_ins:.2f}+0.00-0.00")
        # Attached doc codes reference
        if idx == 1:
            doc_codes = "N380 DIS DV1 "
            if eur1_reference:
                doc_codes += "PE1 "
            _sub(tarif, "Attached_doc_item", doc_codes)
        else:
            if eur1_reference:
                _sub(tarif, "Attached_doc_item", "PE1 ")
            else:
                _empty(tarif, "Attached_doc_item")
        _null(tarif, "A.I._code")

        # Goods description — box 31 description part
        goods = _sub(item_el, "Goods_description")
        _sub(goods, "Country_of_origin_code", item.get("country_origin", "CN"))
        _null(goods, "Country_of_origin_region")   # null matches reference
        # Description_of_goods: description_bs literally (leaf text with hierarchy dashes).
        # Safety net falls back to DB lookup if incoming text is empty or contains
        # legacy em-dash join format.
        _sub(
            goods, "Description_of_goods",
            _resolve_description(official_desc, item.get("tariff_code", ""), db)
        )
        # Commercial_Description: heading line(s) ending in ':' then item-name lines
        heading_for_cd = _resolve_heading(item.get("heading_bs", ""), official_desc)
        include_hdg = item.get("_include_heading", True)
        _sub(goods, "Commercial_Description",
             _format_commercial_desc(heading_for_cd, group_names, include_heading=include_hdg))

        # Previous document
        prev = _sub(item_el, "Previous_doc")
        if idx == 1:
            _sub(prev, "Previous_category", "Z")
            _sub(prev, "Previous_type", "N821")
        else:
            _null(prev, "Previous_category")
            _null(prev, "Previous_type")
        summary_ref = transit_doc if transit_doc else f"{year_2d}BA"
        _sub(prev, "Summary_declaration", summary_ref)
        _sub(prev, "Summary_declaration_sl", "1")
        _empty(prev, "Previous_document_reference")
        _null(prev, "Previous_warehouse_code")

        _null(item_el, "Licence_number")
        _empty(item_el, "Amount_deducted_from_licence")
        _empty(item_el, "Quantity_deducted_from_licence")
        if suspect:
            conf = (item.get("confidence") or "?").strip().lower()
            src  = (item.get("tariff_source") or "?").strip()
            _sub(item_el, "Free_text_1", f"PROVJERITI TARIFU: AI {conf} / {src}")
        else:
            _null(item_el, "Free_text_1")
        if eur1_reference:
            _sub(item_el, "Free_text_2", eur1_reference)
        else:
            _null(item_el, "Free_text_2")

        # Taxation — box 47
        tax = _sub(item_el, "Taxation")
        _empty(tax, "Item_taxes_amount")
        _empty(tax, "Item_taxes_guaranted_amount")
        _sub(tax, "Item_taxes_mode_of_payment", "1")   # must be "1" — not null
        _empty(tax, "Counter_of_normal_mode_of_payment")
        _empty(tax, "Displayed_item_taxes_amount")
        for _ in range(8):
            tl = _sub(tax, "Taxation_line")
            _null(tl, "Duty_tax_code")
            _empty(tl, "Duty_tax_Base")
            _empty(tl, "Duty_tax_rate")
            _empty(tl, "Duty_tax_amount")
            _null(tl, "Duty_tax_MP")
            _null(tl, "Duty_tax_Type_of_calculation")

        # Valuation per item — box 35, 46
        val_item = _sub(item_el, "Valuation_item")
        w_itm = _sub(val_item, "Weight_itm")
        _sub(w_itm, "Gross_weight_itm", f"{gross_w:.1f}" if gross_w else "0")
        _sub(w_itm, "Net_weight_itm",   f"{net_w:.1f}" if net_w else "0")
        _empty(val_item, "Total_cost_itm")
        _empty(val_item, "Total_CIF_itm")
        _sub(val_item, "Rate_of_adjustement", "1")
        _sub(val_item, "Statistical_value", "0.0")
        _empty(val_item, "Alpha_coeficient_of_apportionment")

        item_inv = _sub(val_item, "Item_Invoice")
        _sub(item_inv, "Amount_national_currency", "")
        _sub(item_inv, "Amount_foreign_currency", f"{item_value:.2f}")
        _sub(item_inv, "Currency_code", currency)
        _null(item_inv, "Currency_name")
        _sub(item_inv, "Currency_rate", f"{currency_rate:.6f}" if currency_rate else "")

        # Per-item freight apportionment
        fi_ext = _sub(val_item, "item_external_freight")
        if external_freight and vi_ext:
            _sub(fi_ext, "Amount_national_currency", f"{vi_ext:.2f}")
            _sub(fi_ext, "Amount_foreign_currency",
                 f"{external_freight * alpha:.2f}")
            _sub(fi_ext, "Currency_code", external_freight_currency)
            _sub(fi_ext, "Currency_name", _currency_name(external_freight_currency))
            _sub(fi_ext, "Currency_rate", f"{ext_rate:.6f}")
        else:
            _sub(fi_ext, "Amount_national_currency", "0.0")
            _sub(fi_ext, "Amount_foreign_currency", "0.0")
            _empty(fi_ext, "Currency_code")
            _sub(fi_ext, "Currency_name", "Nema stranih valuta")
            _sub(fi_ext, "Currency_rate", "0")
        fi_int = _sub(val_item, "item_internal_freight")
        if internal_freight and vi_int:
            _sub(fi_int, "Amount_national_currency", f"{vi_int:.2f}")
            _sub(fi_int, "Amount_foreign_currency", "0.0")
            _empty(fi_int, "Currency_code")
            _sub(fi_int, "Currency_name", "Nema stranih valuta")
            _sub(fi_int, "Currency_rate", "0")
        else:
            _sub(fi_int, "Amount_national_currency", "0.0")
            _sub(fi_int, "Amount_foreign_currency", "0.0")
            _empty(fi_int, "Currency_code")
            _sub(fi_int, "Currency_name", "Nema stranih valuta")
            _sub(fi_int, "Currency_rate", "0")
        fi_ins = _sub(val_item, "item_insurance")
        if insurance and vi_ins:
            _sub(fi_ins, "Amount_national_currency", f"{vi_ins:.2f}")
            _sub(fi_ins, "Amount_foreign_currency", "0.0")
            _empty(fi_ins, "Currency_code")
            _sub(fi_ins, "Currency_name", "Nema stranih valuta")
            _sub(fi_ins, "Currency_rate", "0")
        else:
            _sub(fi_ins, "Amount_national_currency", "0.0")
            _sub(fi_ins, "Amount_foreign_currency", "0.0")
            _empty(fi_ins, "Currency_code")
            _sub(fi_ins, "Currency_name", "Nema stranih valuta")
            _sub(fi_ins, "Currency_rate", "0")
        for freight_tag in ("item_other_cost", "item_deduction"):
            fi = _sub(val_item, freight_tag)
            _sub(fi, "Amount_national_currency", "0.0")
            _sub(fi, "Amount_foreign_currency", "0.0")
            _empty(fi, "Currency_code")
            _sub(fi, "Currency_name", "Nema stranih valuta")
            _sub(fi, "Currency_rate", "0")

        mv = _sub(val_item, "Market_valuer")
        _empty(mv, "Rate")
        _null(mv, "Currency_code")
        _empty(mv, "Currency_amount")
        _null(mv, "Basis_description")
        _empty(mv, "Basis_amount")

    # Serialise — lxml uses single quotes in XML declaration, ASYCUDA expects double quotes
    xml_bytes = etree.tostring(
        root, pretty_print=True, xml_declaration=True,
        encoding="UTF-8", standalone=False
    )
    xml_str = xml_bytes.decode("utf-8")

    # Fix single-quote XML declaration → double quotes (ASYCUDA requirement)
    xml_str = xml_str.replace(
        "<?xml version='1.0' encoding='UTF-8' standalone='no'?>",
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
    )

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(xml_str)

    return xml_str


def _country_name(code: str) -> str:
    names = {
        "CN": "KINA", "DE": "NJEMAČKA", "TR": "TURSKA", "IT": "ITALIJA",
        "AT": "AUSTRIJA", "US": "SAD", "GB": "VELIKA BRITANIJA",
        "HR": "HRVATSKA", "RS": "SRBIJA", "SI": "SLOVENIJA",
        "BA": "BOSNA I HERCEGOVINA", "HU": "MAĐARSKA", "PL": "POLJSKA",
        "CZ": "ČEŠKA", "SK": "SLOVAČKA", "RO": "RUMUNIJA",
        "FR": "FRANCUSKA", "ES": "ŠPANIJA", "NL": "HOLANDIJA",
        "BE": "BELGIJA", "SE": "ŠVEDSKA", "DK": "DANSKA",
        "CH": "ŠVICARSKA", "JP": "JAPAN", "KR": "KOREJA",
    }
    return names.get(code.upper(), code)
