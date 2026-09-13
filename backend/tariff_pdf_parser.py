"""
Parses the official BiH Customs Tariff PDF (2026).

PDF line format:
  - 4-digit code alone on a line, description on NEXT line
  - 6-digit code alone, description on next line
  - 10-digit code + description on SAME line: "0101 21 00 00 – – čistokrvne priplodne životinje"

Strategy: parse all lines, look-ahead for descriptions after short codes.
Build hierarchical full description for each 10-digit leaf.
"""
import re
import logging
import urllib.request
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from sqlalchemy.orm import Session
from database import OfficialTariff, SessionLocal, init_db

logger = logging.getLogger("tariff_pdf_parser")

TARIFF_PDF_URL = (
    "https://www.mvteo.gov.ba/attachments/bs_Home/Ostale_stranice/"
    "Carinska_politika_i_tarife/Akti/Podzakonski_akti_carina/"
    "Carinska_tarifa_za_2026_-_bosanski.pdf"
)

DATA_DIR = Path(__file__).parent.parent / "data"
TARIFF_PDF_PATH = DATA_DIR / "carinska_tarifa_2026.pdf"

# Matches: "0101 21 00 00 rest..." or "0101 21 rest..." or "0101 rest..." or "0101"
CODE_START_RE = re.compile(r"^(\d{4}(?:\s\d{2}){0,3})((?:\s.*)?)$")
# Standalone code (no description on same line)
ALONE_CODE_RE = re.compile(r"^(\d{4}(?:\s\d{2}){0,3})\s*$")
# Skip lines that are only numbers (duty rates)
ONLY_NUMBERS_RE = re.compile(r"^\d+[\d\s,\.]*$")
# Dash prefix for sub-items: "– konji:" or "– – ostali:"
DASH_DESC_RE = re.compile(r"^[–\-]\s")


def _strip_spaces(raw: str) -> str:
    return re.sub(r"\s", "", raw)


def _count_digits(raw: str) -> int:
    return len(_strip_spaces(raw))


def _is_continuation(line: str) -> bool:
    """Return True if this line is likely a continuation of the previous description line."""
    if not line:
        return False
    # Starts with lowercase or parenthetical
    if line[0].islower() or line[0] == '(':
        return True
    # Starts with a percentage value like "10% po masi" or "2,8% ili više"
    if re.match(r'^\d[\d,\.]*%', line):
        return True
    return False


# Tokens that appear as standalone artifacts from PDF table columns (NOT real headings)
_COLUMN_NOISE = {"kd", "kg", "g", "l", "m2", "m²", "m³", "m3", "pce", "par", "kom", "ltr"}

# Kanonski 4-cifarski headings za chapter-e gdje PDF parser sistemski fail-uje
# (heading je u redovima koje parser ne hvata jer 4-cif. kod ne postoji kao
# samostalna linija — npr. ch9503 ima samo "9503 00" + wrap-an heading na 5
# linija). Tekst preuzet doslovno iz BiH Carinske tarife 2026.
_KNOWN_CHAPTER_HEADINGS: dict[str, str] = {
    "9503": (
        "Tricikli; romobili, automobili sa pedalama i slične igračke sa "
        "točkovima; kolica za lutke; lutke; druge igračke; umanjeni modeli "
        "i slični modeli za igre, sa pogonom ili bez pogona; slagalice "
        "(puzzles) svih vrsta"
    ),
    # Buduće dodavanje (TODO): "1002", "1004", "1006" sa kanonskim tekstom
    # (Raž / Zob / Riža) kada se odluči adresirati.
}
_PURE_NUMBER_RE = re.compile(r"^\d+([\.,]\d+)?$")
# Duty-rate units stripped before pure-symbol check (no \b — digit↔letter has no boundary)
# Excludes plain 'l' to avoid stripping it from real words; 'l' alone is in _COLUMN_NOISE
_DUTY_UNIT_RE = re.compile(
    r"(KM|EUR|kd|kg|m²|m³|m2|m3|pce|par|kom|ltr)", re.IGNORECASE
)
# Sentence fragment: "ili 6602", "i 8530", "do 8705" — text bleed from continuation lines
_FRAGMENT_REF_RE = re.compile(r"^(ili|i|do|sa|s|u|na|za|od|iz)\s+\d{4}\b", re.IGNORECASE)
# After stripping units, what remains for a duty rate is digits + symbols only
_PURE_SYMBOLS_RE = re.compile(r"^[\d\s\+\-\,\.\/\%]+$")
# PDF table column-header text
_COLUMN_HEADER_TEXTS = {
    "masa", "dopunska", "jedinica", "carinska stopa", "naziv",
    "tarifna oznaka", "napomena", "opis", "podbrojevi",
}


def _is_artifact_heading(text: str) -> bool:
    """True when text is a PDF-table artifact (page/column number, unit name,
    duty-rate column, or continuation fragment) rather than a real tariff
    heading. Legitimate short headings like 'Raž', 'Žive ribe', 'Vanilija'
    return False."""
    t = (text or "").strip()
    if not t:
        return True
    if _PURE_NUMBER_RE.match(t):
        return True
    if t.lower() in _COLUMN_NOISE:
        return True
    # Duty-rate column: short heading where after stripping units only symbols remain.
    # Length cap prevents accidental match on long real headings that happen to contain "kg" etc.
    if len(t) < 30:
        stripped = _DUTY_UNIT_RE.sub("", t).strip()
        if stripped and _PURE_SYMBOLS_RE.match(stripped):
            return True
    if _FRAGMENT_REF_RE.match(t):
        return True
    # Column header text, possibly with trailing junk like "Masa (g/m2)"
    head = re.split(r"[\s(]", t.lower(), maxsplit=1)[0]
    if head in _COLUMN_HEADER_TEXTS:
        return True
    return False


def _clean_desc(text: str) -> str:
    # Remove leading dash indicators (– – –)
    text = re.sub(r"^[\-–\s]+", "", text)
    # Remove trailing duty rate numbers (columns after description)
    text = re.sub(r"\s+(?:kd|kg|l|m2|m²|pce|par|kom)\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+\d[\d\s]*$", "", text)
    # Remove footnotes like (¹) (²)
    text = re.sub(r"\s*[\(（][¹²³⁴\d\s,]+[\)）]", "", text)
    text = text.strip(" :-–.,;")
    # Reject PDF-table artifacts (pure numbers, column unit names)
    if _is_artifact_heading(text):
        return ""
    return text


def _extract_dash_prefix(raw: str) -> tuple[str, str]:
    """Extract leading hierarchy dashes (handles both U+002D '-' and U+2013 '–').
    Returns (normalized_prefix, remaining_text).

    Format: each dash level becomes "- " (dash + space). Trailing space included.
    Example: '– – ostalo' → ('- - ', 'ostalo')
             '– slogovi'  → ('- ',   'slogovi')
             'plastika'   → ('',     'plastika')
    """
    text = (raw or "").lstrip()
    dash_count = 0
    while text and text[0] in ('-', '–', '—'):
        dash_count += 1
        text = text[1:]
        if text.startswith(' '):
            text = text[1:]
    if dash_count == 0:
        return "", text.strip()
    prefix = "- " * dash_count
    return prefix, text.strip()


def _clean_leaf_desc(raw: str) -> str:
    """Clean a 10-digit leaf description WHILE preserving hierarchy dashes.
    Returns: 'normalized_prefix' + 'body', e.g. '- - ostalo'.

    Trailing duty rates / units / footnotes are stripped but punctuation
    inside the text (incl. trailing period) is preserved verbatim.
    """
    text = raw or ""
    # Strip trailing duty rate units
    text = re.sub(r"\s+(?:kd|kg|l|m2|m²|pce|par|kom)\s*$", "", text, flags=re.IGNORECASE)
    # Strip trailing numeric duty rate columns
    text = re.sub(r"\s+\d[\d\s,]*$", "", text)
    # Strip footnote markers
    text = re.sub(r"\s*[\(（][¹²³⁴\d\s,]+[\)）]", "", text)
    text = text.rstrip()

    prefix, body = _extract_dash_prefix(text)
    body = body.rstrip(" :,;")   # strip trailing separators but keep period
    return (prefix + body).strip() if (prefix or body) else ""


def _is_noise(line: str) -> bool:
    """Lines to skip: column headers, page numbers, only digits."""
    if ONLY_NUMBERS_RE.match(line):
        return True
    noise = {"Tarifna oznaka", "Naziv", "Dopunska", "jedinica", "Carinska stopa",
             "EU", "CEFTA", "IRN", "TUR", "EFTA", "CHE, LIE", "ISL", "NOR",
             "Napomena", "Podbrojevi", "Opis", "KRATICE I SIMBOLI", "Napomene"}
    if line in noise:
        return True
    if re.match(r"^[1-9]\d?$", line):  # single/double digit column numbers
        return True
    return False


# ── Dopunska jedinica (polje 41) — pozicijsko vađenje iz PDF tabele ──────────
# Na redovima s podacima kolona "Dopunska jedinica" stoji na x≈344 (između Naziv
# koji završava ~320 i Carinska stopa koja počinje ~382). Vadimo je pozicijski
# jer ravni tekst-tok miješa unit s duty-rate kolonom.
_UNIT_BAND = (322.0, 381.0)
_FOOTNOTE_RE = re.compile(r"\(\s*[¹²³⁴\d]+\s*\)")
# BiH oznaka → ASYCUDA kod dopunske jedinice (samo pouzdano mapljive; egzotične
# poput "l alc. 100%", "kg N", "1000 kd", "ce/el" → None, box 41 ostaje prazan)
_BIH_UNIT_TO_ASYCUDA = {
    "kd": "PCE", "m²": "MTK", "m2": "MTK", "m³": "MTQ", "m3": "MTQ",
    "pa": "PR", "pár": "PR", "par": "PR",
}


def _map_bih_unit(band_tokens: list[str]) -> str | None:
    """Mapiraj tokene iz unit-kolone jednog reda u ASYCUDA kod ili None."""
    if not band_tokens:
        return None
    joined = _FOOTNOTE_RE.sub("", " ".join(band_tokens)).strip()
    if not joined or joined[0] in "–-":
        return None            # nema jedinice (crtica) ili duty-rate bleed
    # Egzaktni jednoznaci — da "l alc. 100%" ne padne na LTR, "m" ≠ "m²" itd.
    if joined == "l":
        return "LTR"
    if joined == "m":
        return "MTR"
    if joined == "g":
        return "GRM"
    return _BIH_UNIT_TO_ASYCUDA.get(joined.split()[0])


def _extract_units_by_code(pdf_path: str) -> dict:
    """Vrati {10-cifreni kod → ASYCUDA jedinica} pozicijskim čitanjem tabele."""
    doc = fitz.open(pdf_path)
    units: dict[str, str] = {}
    for pn in range(len(doc)):
        rows: dict[int, list] = {}
        for w in doc[pn].get_text("words"):   # (x0,y0,x1,y1,word,...)
            rows.setdefault(round(w[1]), []).append(w)
        for ws in rows.values():
            ws.sort(key=lambda w: w[0])
            code = "".join(w[4] for w in ws
                           if w[0] < 125 and re.fullmatch(r"\d{2,4}", w[4]))
            if len(code) != 10:
                continue
            band = [w[4] for w in ws if _UNIT_BAND[0] <= w[0] < _UNIT_BAND[1]]
            asy = _map_bih_unit(band)
            if asy:
                units[code] = asy
    doc.close()
    return units


def _download_pdf():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if TARIFF_PDF_PATH.exists() and TARIFF_PDF_PATH.stat().st_size > 100_000:
        return True
    logger.info("Downloading tariff PDF...")
    try:
        urllib.request.urlretrieve(TARIFF_PDF_URL, str(TARIFF_PDF_PATH))
        return True
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return False


def _parse_tariff_rows(pdf_path: str) -> list[dict]:
    doc = fitz.open(pdf_path)

    # Collect all lines from all pages
    all_lines = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text")
        for raw in text.splitlines():
            line = raw.strip()
            if line:
                all_lines.append(line)
    doc.close()

    rows = []
    seen = set()

    # Hierarchy: desc at each level
    h = {4: "", 6: "", 8: ""}
    # Tracks the 4-digit code that produced h[4]; lets us detect implicit chapter
    # transitions where a 10-digit code appears without a standalone 4-digit header.
    current_h4_code = ""
    # dash-level descriptions (between 4 and 6 digit codes): "– konji:"
    dash_ctx = ""

    i = 0
    while i < len(all_lines):
        line = all_lines[i]

        m = CODE_START_RE.match(line)
        if not m:
            if _is_noise(line):
                i += 1
                continue
            # Could be a dash description between codes: "– konji:"
            if DASH_DESC_RE.match(line):
                dash_ctx = _clean_desc(line)
            i += 1
            continue

        raw_code = m.group(1)
        rest = m.group(2).strip()
        digits = _strip_spaces(raw_code)
        dlen = len(digits)

        if dlen not in (4, 6, 8, 10):
            i += 1
            continue

        # Description: either on same line (rest) or on next non-noise line(s)
        desc = ""           # dashes-stripped (used for 4/6/8-digit headings)
        raw_desc = ""       # dashes preserved (used only for 10-digit leaves)
        if rest:
            raw_desc = rest.strip()
            # Check if description continues on the next line (starts lowercase or '(')
            j = i + 1
            while j < len(all_lines) and _is_noise(all_lines[j]):
                j += 1
            if j < len(all_lines):
                nxt = all_lines[j]
                if _is_continuation(nxt) and not CODE_START_RE.match(nxt):
                    raw_desc = raw_desc + " " + nxt.strip()
            desc = _clean_desc(raw_desc)

        if not desc:
            # Look ahead for description on next non-noise line(s)
            j = i + 1
            while j < len(all_lines) and _is_noise(all_lines[j]):
                j += 1
            if j < len(all_lines):
                next_line = all_lines[j]
                # Next line is description if it doesn't start with a new code
                if not CODE_START_RE.match(next_line) or DASH_DESC_RE.match(next_line):
                    parts = [next_line.strip()]
                    # Collect continuation lines (max 2 extra lines)
                    k = j + 1
                    for _ in range(2):
                        while k < len(all_lines) and _is_noise(all_lines[k]):
                            k += 1
                        if k < len(all_lines) and _is_continuation(all_lines[k]) and not CODE_START_RE.match(all_lines[k]):
                            parts.append(all_lines[k].strip())
                            k += 1
                        else:
                            break
                    raw_desc = " ".join(parts)
                    desc = _clean_desc(raw_desc)

        if not desc:
            i += 1
            continue

        if dlen == 4:
            h[4] = desc
            h[6] = ""
            h[8] = ""
            current_h4_code = digits
            dash_ctx = ""
            i += 1
            continue

        if dlen == 6:
            h[6] = desc
            h[8] = ""
            dash_ctx = ""
            i += 1
            continue

        if dlen == 8:
            h[8] = desc
            i += 1
            continue

        # 10-digit leaf
        if digits in seen:
            i += 1
            continue
        seen.add(digits)

        # description_bs: leaf text with hierarchy dashes preserved, e.g. "- - ostalo"
        leaf_desc = _clean_leaf_desc(raw_desc)
        if len(leaf_desc) < 3:
            i += 1
            continue

        # Detect implicit chapter transition (10-digit code without preceding
        # standalone 4-digit header): empty heading triggers post-process fallback.
        heading_bs = h[4] if digits[:4] == current_h4_code else ""

        rows.append({
            "code": digits,
            "description_bs": leaf_desc[:600],
            "heading_bs": heading_bs[:600],
            "unit": None,
            "duty_rate": None,
            "chapter": digits[:2],
        })
        i += 1

    # ── Post-process: fix junk heading_bs (pre-existing PDF table artifacts) ──
    # Junk = empty, pure number, or column unit. _clean_desc() already rejects
    # most via _is_artifact_heading(), but when the artifact was set into h[4]
    # before this fix or a heading was lost to bad lookahead, we patch here.
    def _is_junk_heading(h: str) -> bool:
        return _is_artifact_heading(h) or len(h.strip()) < 5

    # First pass: best valid heading per 4-digit chapter group
    h4_best: dict[str, str] = {}
    for row in rows:
        h = row.get("heading_bs", "") or ""
        if not _is_junk_heading(h):
            h4_best.setdefault(row["code"][:4], h)

    # Second pass: replace junk with best peer or strip-dashes fallback
    patched = 0
    for row in rows:
        if _is_junk_heading(row.get("heading_bs", "") or ""):
            h4 = row["code"][:4]
            fallback = h4_best.get(h4) or re.sub(
                r"^[\-\s]+", "", row.get("description_bs", "")
            ).strip()
            if fallback and fallback != (row.get("heading_bs") or ""):
                row["heading_bs"] = fallback
                patched += 1

    # Third pass: canonical heading override for chapters where parser
    # systemically misses the 4-digit heading line (e.g. ch9503 — heading
    # exists in PDF but only attached to a 6-digit "9503 00" code).
    overridden = 0
    for row in rows:
        canonical = _KNOWN_CHAPTER_HEADINGS.get(row["code"][:4])
        if canonical and row.get("heading_bs") != canonical:
            row["heading_bs"] = canonical
            overridden += 1

    # ── Dopunska jedinica (polje 41): pozicijski map code→ASYCUDA jedinica ──
    units = _extract_units_by_code(pdf_path)
    unit_matched = 0
    for row in rows:
        u = units.get(row["code"])
        if u:
            row["unit"] = u
            unit_matched += 1

    logger.info(
        f"Parsed {len(rows)} tariff entries "
        f"(patched {patched} junk, overridden {overridden} via canonical map, "
        f"{unit_matched} s dopunskom jedinicom)"
    )
    return rows


def load_official_tariffs(force_reload: bool = False) -> int:
    init_db()
    db = SessionLocal()
    try:
        existing = db.query(OfficialTariff).count()
        if existing > 0 and not force_reload:
            return existing

        if not _download_pdf():
            return 0

        rows = _parse_tariff_rows(str(TARIFF_PDF_PATH))
        if not rows:
            logger.warning("No rows parsed")
            return 0

        db.query(OfficialTariff).delete()
        db.commit()
        for row in rows:
            db.add(OfficialTariff(**row))
        db.commit()
        logger.info(f"Inserted {len(rows)} records")
        return len(rows)
    finally:
        db.close()


def search_official_tariffs(query: str, db: Session, limit: int = 15) -> list:
    words = [w.lower() for w in query.split() if len(w) > 2]
    if not words:
        return []
    prefixes = [w[:5] for w in words]
    all_tariffs = db.query(OfficialTariff).all()
    scored = []
    for t in all_tariffs:
        desc = (t.description_bs or "").lower()
        score = sum(2 if w in desc else (1 if p in desc else 0)
                    for w, p in zip(words, prefixes))
        if score > 0:
            scored.append((score, t))
    scored.sort(key=lambda x: -x[0])
    return [t for _, t in scored[:limit]]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    count = load_official_tariffs(force_reload=True)
    print(f"Loaded {count} records")
