"""
Dry-run pregled uvoza historijskih ASYCUDA XML deklaracija — NULA upisa u DB.

Za svaki NOVI .xml u data/declarations/ (koji nije već u Declaration tabeli)
ispisuje izvučene parove (naziv -> tarifni kod), tačno onako kako bi ih
xml_parser.parse_xml_file upisao u TariffRecord — ali bez ijednog INSERT-a.

Posebno flaguje fajlove/stavke gdje je _extract_names_from_commercial pao na
"zadnja linija" fallback (neuredan Commercial_Description) — te treba pregledati
ručno prije pravog importa, jer fallback ubacuje šumne nazive.

Pokretanje:
    python scripts/preview_import.py            # samo novi fajlovi
    python scripts/preview_import.py --all       # svi fajlovi (i već uvezeni)
    python scripts/preview_import.py --show-ok    # ispiši i uredne parove
"""
import argparse
import glob
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from lxml import etree                                        # noqa: E402
from database import SessionLocal, Declaration                # noqa: E402
from xml_parser import _extract_names_from_commercial, DECLARATIONS_DIR  # noqa: E402

# Iste regexe koje _extract_names_from_commercial smatra "strukturiranim".
# Ako nijedna linija ne matchuje, ekstrakcija je pala na reverse-line fallback.
_KOM_DASH  = re.compile(r"^(.+?)-(\d[\d.,]*)\s*KOM\s*$", re.IGNORECASE)
_KOM_SPACE = re.compile(r"^([^\d\n]{2,60}?)\s+(\d[\d.,]*)\s*KOM\s*$", re.IGNORECASE)


def _is_structured(commercial_desc: str) -> bool:
    text = commercial_desc.strip()
    if "\n-\n" in text:
        return True
    for line in text.splitlines():
        line = line.strip()
        if _KOM_DASH.match(line) or _KOM_SPACE.match(line):
            return True
    return False


def preview_file(filepath: str) -> dict:
    """Parsira XML u memoriji i vraća {items:[(naziv,kod,fallback)], errors}."""
    try:
        root = etree.parse(filepath).getroot()
    except Exception as e:
        return {"parse_error": str(e), "items": []}

    out = []
    for item in root.findall(".//Item"):
        code_el = item.find(".//Commodity_code")
        code = code_el.text.strip() if code_el is not None and code_el.text else None
        comm_el = item.find(".//Commercial_Description")
        comm = comm_el.text.strip() if comm_el is not None and comm_el.text else ""
        if not code or not comm:
            continue
        fallback = not _is_structured(comm)
        for name in _extract_names_from_commercial(comm):
            out.append((name.lower(), code, fallback))
    return {"parse_error": None, "items": out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="uključi i već uvezene fajlove")
    ap.add_argument("--show-ok", action="store_true", help="ispiši i uredno izvučene parove")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        imported = {row[0] for row in db.query(Declaration.filename).all()}
    finally:
        db.close()

    files = sorted(glob.glob(os.path.join(DECLARATIONS_DIR, "*.xml")))
    n_files = n_new = n_items = n_fallback = 0
    flagged_files = []

    for fp in files:
        fname = os.path.basename(fp)
        already = fname in imported
        if already and not args.all:
            continue
        n_files += 1
        if not already:
            n_new += 1

        res = preview_file(fp)
        if res["parse_error"]:
            print(f"\n!! PARSE ERROR  {fname}: {res['parse_error']}")
            continue

        items = res["items"]
        n_items += len(items)
        file_fallbacks = [it for it in items if it[2]]
        n_fallback += len(file_fallbacks)
        tag = " [VEĆ UVEZEN]" if already else ""

        if file_fallbacks:
            flagged_files.append(fname)
            print(f"\n⚠  {fname}{tag} — {len(file_fallbacks)}/{len(items)} stavki fallback (provjeri format):")
            for name, code, _ in file_fallbacks:
                print(f"     ? {code}  <-  {name}")
        elif args.show_ok:
            print(f"\n✓  {fname}{tag} — {len(items)} stavki:")
            for name, code, _ in items:
                print(f"     {code}  <-  {name}")

    print("\n" + "=" * 60)
    print(f"Fajlova pregledano:   {n_files}  (novih: {n_new})")
    print(f"Stavki izvučeno:      {n_items}")
    print(f"Fallback stavki:      {n_fallback}  u {len(flagged_files)} fajlova")
    if flagged_files:
        print("Fajlovi za ručnu provjeru:")
        for f in flagged_files:
            print(f"  - {f}")
    print("NAPOMENA: nijedan red nije upisan u DB (dry-run).")


if __name__ == "__main__":
    main()
