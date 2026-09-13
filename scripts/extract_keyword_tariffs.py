"""
Ekstrahuje keyword→tarifni_broj mapiranja iz svih historijskih deklaracija
(tariff_records tabela) i sprema ih u keyword_tariff_map tabelu.

Pokretanje:
    cd /path/to/spedicija-app
    python scripts/extract_keyword_tariffs.py

Opcije:
    --min-freq N     Minimalna frekvencija za uključenje (default: 2)
    --min-conf F     Minimalni confidence score (default: 0.70)
    --dry-run        Samo ispiši statistiku, ne piši u DB
    --reset          Obriši i ponovo izgradi cijelu tabelu
"""
import sys
import os
import re
import json
import argparse
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# ── Putanja do DB-a (relativno od root-a projekta) ───────────────────────────
ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "spedicija.db"

# ── Stop riječi koje se uklanjaju iz naziva stavki ───────────────────────────
STOP_WORDS = {
    # Mjerne jedinice i opće oznake
    "kom", "kg", "pak", "pcs", "set", "br", "no", "lot", "rll",
    "pce", "pc", "box", "ctn", "bag", "roll", "pair", "pr", "bx",
    "m2", "m3", "ltr", "lit", "ml", "gr", "mg", "ton", "t",
    # Prijedlozi i veznici (česti u opisima)
    "za", "od", "na", "sa", "iz", "po", "do", "i", "ili", "te",
    "a", "s", "u", "o", "k", "g", "nk",
    # Ostale beznačajne riječi
    "ostalo", "ostali", "ostale", "razno", "ostala", "tip", "vrsta",
    "model", "modeli", "tip", "novo", "nova", "novi",
}

# ── Regex za uklanjanje mjera i kodova ───────────────────────────────────────
_RE_NUMBERS    = re.compile(r'\b\d+[\.,]?\d*\s*(cm|mm|m|kg|g|ml|l|v|w|hz|°c|kom|pcs|pc|pce|br|no)?\b', re.I)
_RE_PUNCT      = re.compile(r'[^\w\s]')          # sve osim slova, cifara, razmaka
_RE_MULTI_SP   = re.compile(r'\s+')


def _normalize(text: str) -> str:
    """Normalizuj naziv: uppercase, ukloni mjere/punct/stop words."""
    text = text.upper().strip()
    text = _RE_NUMBERS.sub(' ', text)
    text = _RE_PUNCT.sub(' ', text)
    text = _RE_MULTI_SP.sub(' ', text).strip()
    # Ukloni stop words
    words = [w for w in text.split() if w.lower() not in STOP_WORDS and len(w) >= 2]
    return ' '.join(words)


def _extract_ngrams(words: list[str]) -> dict[str, str]:
    """
    Izvuci unigrame (≥3 znaka), bigrame i trigrame iz liste riječi.
    Vraća {ngram_text: ngram_type}.
    """
    result = {}
    n = len(words)
    for w in words:
        if len(w) >= 3:
            result[w] = 'unigram'
    for i in range(n - 1):
        bigram = f"{words[i]} {words[i+1]}"
        if len(bigram) >= 5:
            result[bigram] = 'bigram'
    for i in range(n - 2):
        trigram = f"{words[i]} {words[i+1]} {words[i+2]}"
        result[trigram] = 'trigram'
    return result


def _confidence(dist: dict[str, int]) -> tuple[str, float]:
    """
    Iz distribucije {code: count} izračunaj dominant kod i confidence score.
    confidence = dominant_count / total_count
    """
    if not dist:
        return "", 0.0
    total = sum(dist.values())
    dominant_code = max(dist, key=dist.get)
    dominant_count = dist[dominant_code]
    score = dominant_count / total
    return dominant_code, round(score, 4)


def run(min_freq: int = 2, min_conf: float = 0.70, dry_run: bool = False, reset: bool = False):
    if not DB_PATH.exists():
        print(f"[ERROR] DB nije pronađen: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ── Kreira tabelu ako ne postoji ─────────────────────────────────────────
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS keyword_tariff_map (
            id INTEGER PRIMARY KEY,
            keyword TEXT NOT NULL,
            keyword_type TEXT NOT NULL,   -- 'unigram', 'bigram', 'trigram'
            dominant_tariff_code TEXT NOT NULL,
            official_desc TEXT,
            frequency INTEGER DEFAULT 1,
            confidence_score REAL DEFAULT 0.0,
            tariff_distribution TEXT,     -- JSON: {"code": count}
            last_seen TIMESTAMP,
            UNIQUE(keyword, keyword_type)
        );
        CREATE INDEX IF NOT EXISTS idx_ktm_keyword ON keyword_tariff_map(keyword);
        CREATE INDEX IF NOT EXISTS idx_ktm_code    ON keyword_tariff_map(dominant_tariff_code);
    """)
    conn.commit()

    if reset and not dry_run:
        cur.execute("DELETE FROM keyword_tariff_map")
        conn.commit()
        print("[RESET] Tabela keyword_tariff_map očišćena.")

    # ── Čitaj sve tariff_records ──────────────────────────────────────────────
    cur.execute("SELECT description, tariff_code, official_desc FROM tariff_records WHERE description IS NOT NULL AND tariff_code IS NOT NULL")
    rows = cur.fetchall()
    print(f"[INFO] Učitano {len(rows)} redova iz tariff_records.")

    # ── Akumuliraj distribucije po keywordu ──────────────────────────────────
    # {(keyword, kw_type): {tariff_code: count}}
    kw_dist: dict[tuple, dict[str, int]]     = defaultdict(lambda: defaultdict(int))
    kw_desc: dict[tuple, str]                = {}  # (kw, type) → official_desc zadnjeg hita
    kw_last: dict[tuple, str]                = {}

    skipped = 0
    processed = 0
    for row in rows:
        desc     = str(row["description"] or "").strip()
        code_raw = str(row["tariff_code"] or "").strip()
        odesc    = str(row["official_desc"] or "").strip()

        # Validacija koda — mora biti 6-10 cifara
        code = re.sub(r'[^\d]', '', code_raw)[:8]
        if len(code) < 6:
            skipped += 1
            continue

        norm = _normalize(desc)
        if not norm:
            skipped += 1
            continue

        words = norm.split()
        if not words:
            skipped += 1
            continue

        ngrams = _extract_ngrams(words)
        processed += 1

        for kw, kw_type in ngrams.items():
            key = (kw, kw_type)
            kw_dist[key][code] += 1
            kw_desc[key] = odesc
            kw_last[key] = datetime.utcnow().isoformat()

    print(f"[INFO] Procesovano: {processed}, preskočeno (invalid): {skipped}")
    print(f"[INFO] Ukupno unikatnih keyword kandidata: {len(kw_dist)}")

    # ── Filtriraj i pripremi za upis ─────────────────────────────────────────
    accepted   = []
    rejected_freq = 0
    rejected_conf = 0

    for (kw, kw_type), dist in kw_dist.items():
        freq = sum(dist.values())
        dominant_code, conf = _confidence(dist)

        if freq < min_freq:
            rejected_freq += 1
            continue
        if conf < min_conf:
            rejected_conf += 1
            continue

        accepted.append({
            "keyword":              kw,
            "keyword_type":         kw_type,
            "dominant_tariff_code": dominant_code,
            "official_desc":        kw_desc.get((kw, kw_type), ""),
            "frequency":            freq,
            "confidence_score":     conf,
            "tariff_distribution":  json.dumps(dict(sorted(dist.items(), key=lambda x: -x[1]))),
            "last_seen":            kw_last.get((kw, kw_type), ""),
        })

    print(f"\n[FILTER] Min freq={min_freq}, min conf={min_conf:.0%}")
    print(f"  Odbačeno (premalo pojava):  {rejected_freq}")
    print(f"  Odbačeno (nizak confidence): {rejected_conf}")
    print(f"  Prihvaćeno:                 {len(accepted)}")

    # ── Statistika po confidence razredima ───────────────────────────────────
    conf_90  = sum(1 for a in accepted if a["confidence_score"] >= 0.90)
    conf_80  = sum(1 for a in accepted if a["confidence_score"] >= 0.80)
    conf_70  = sum(1 for a in accepted if a["confidence_score"] >= 0.70)

    by_type = defaultdict(int)
    for a in accepted:
        by_type[a["keyword_type"]] += 1

    # ── Distribucija po HS poglavljima ───────────────────────────────────────
    by_chapter: dict[str, int] = defaultdict(int)
    for a in accepted:
        ch = a["dominant_tariff_code"][:2]
        by_chapter[ch] += 1

    print(f"\n[STATISTIKA]")
    print(f"  confidence ≥ 0.90: {conf_90} keywords")
    print(f"  confidence ≥ 0.80: {conf_80} keywords")
    print(f"  confidence ≥ 0.70: {conf_70} keywords")
    print(f"\n  Po tipu:")
    for ktype in ("unigram", "bigram", "trigram"):
        print(f"    {ktype:10s}: {by_type[ktype]}")
    print(f"\n  Top 15 poglavlja (po broju keywords):")
    for ch, cnt in sorted(by_chapter.items(), key=lambda x: -x[1])[:15]:
        print(f"    ch{ch}: {cnt}")

    # ── Primjeri s visokim confidence ────────────────────────────────────────
    top_examples = sorted(
        [a for a in accepted if a["confidence_score"] >= 0.90],
        key=lambda x: (-x["confidence_score"], -x["frequency"])
    )[:20]
    if top_examples:
        print(f"\n  Top primjeri (conf ≥ 0.90):")
        for ex in top_examples:
            print(f"    [{ex['keyword_type']:8s}] {ex['keyword']:30s} → {ex['dominant_tariff_code']}  "
                  f"(freq={ex['frequency']}, conf={ex['confidence_score']:.0%})")

    if dry_run:
        print("\n[DRY-RUN] Nije upisano u bazu.")
        conn.close()
        return

    # ── Upiši u keyword_tariff_map (INSERT OR REPLACE) ───────────────────────
    inserted = 0
    updated  = 0
    for a in accepted:
        cur.execute(
            "SELECT id, frequency, confidence_score FROM keyword_tariff_map WHERE keyword=? AND keyword_type=?",
            (a["keyword"], a["keyword_type"])
        )
        existing = cur.fetchone()
        if existing:
            # Ažuriraj samo ako imamo bolje podatke (veća frekvencija)
            if a["frequency"] > existing["frequency"]:
                cur.execute("""
                    UPDATE keyword_tariff_map
                    SET dominant_tariff_code=?, official_desc=?, frequency=?,
                        confidence_score=?, tariff_distribution=?, last_seen=?
                    WHERE keyword=? AND keyword_type=?
                """, (
                    a["dominant_tariff_code"], a["official_desc"], a["frequency"],
                    a["confidence_score"], a["tariff_distribution"], a["last_seen"],
                    a["keyword"], a["keyword_type"],
                ))
                updated += 1
        else:
            cur.execute("""
                INSERT INTO keyword_tariff_map
                    (keyword, keyword_type, dominant_tariff_code, official_desc,
                     frequency, confidence_score, tariff_distribution, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                a["keyword"], a["keyword_type"], a["dominant_tariff_code"],
                a["official_desc"], a["frequency"], a["confidence_score"],
                a["tariff_distribution"], a["last_seen"],
            ))
            inserted += 1

    conn.commit()
    conn.close()
    print(f"\n[DB] Upisano: {inserted} novih, {updated} ažuriranih.")
    print(f"[DONE] keyword_tariff_map spreman za upotrebu u klasifikatoru.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ekstrahuj keyword→tariff mapiranja iz historijskih deklaracija.")
    parser.add_argument("--min-freq", type=int, default=2,    help="Minimalna frekvencija (default: 2)")
    parser.add_argument("--min-conf", type=float, default=0.70, help="Minimalni confidence (default: 0.70)")
    parser.add_argument("--dry-run",  action="store_true",    help="Samo statistika, bez upisa")
    parser.add_argument("--reset",    action="store_true",    help="Obrisi i ponovo izgradi tabelu")
    args = parser.parse_args()
    run(min_freq=args.min_freq, min_conf=args.min_conf, dry_run=args.dry_run, reset=args.reset)
