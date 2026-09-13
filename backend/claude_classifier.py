"""
Claude AI tariff classifier.

Strategija:
1. Claude odabere HS poglavlje (2 cifre) na osnovu opisa robe
2. Sistem učita sve kodove tog poglavlja
3. Claude odabere tačan 8-cifarski kod iz te liste
4. Format se normalizuje na 8 cifara bez razmaka/tačaka

Koristi kodove isključivo iz official_tariffs tabele (BiH Carinska tarifa 2026).
"""
import os
import re
import json
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

import anthropic
from sqlalchemy.orm import Session

from database import SessionLocal, OfficialTariff, TariffCorrection, TariffRecord

logger = logging.getLogger("claude_classifier")

_client = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set in .env")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _clean_code(code: str) -> str:
    """Normalize tariff code to exactly 8 digits, no spaces/dots/commas."""
    return re.sub(r"[^\d]", "", str(code))[:8]


def _normalize_name(name: str) -> str:
    """Normalize item name for correction lookup (uppercase, collapse spaces)."""
    return re.sub(r"\s+", " ", str(name).upper().strip())


# Jedinice/pakovanja koji su KOLIČINA, ne dio identiteta robe. Skidaju se SAMO
# na auto_repeat stazi (ponavljanje u historiji) — human korekcije ostaju stroge.
_REPEAT_UNIT = r"(kom|par|set|pak|paket|kg|g|m2|m3|m|mt|cm|mm|l|lit|ml|rol|pce|pcs|pc|koma|komada|kut|bal)"
_REPEAT_RE_PACK = re.compile(r"\b\d+[.,]?\d*\s*/\s*\d+\b")            # 6/1, 3/1, 24/1
_REPEAT_RE_QTY  = re.compile(r"\b\d+[.,]?\d*\s*" + _REPEAT_UNIT + r"\b", re.I)  # 1008 par
_REPEAT_RE_DASH = re.compile(r"-\s*\d+[.,]?\d*")                     # -80, -108
_REPEAT_RE_NUM  = re.compile(r"\b\d+[.,]?\d*[a-zA-Z]{0,3}\b")        # zaostali brojevi i spec-sufiksi (12V, 220W, 18A)


def _normalize_repeat_name(name: str) -> str:
    """
    Normalizacija za AUTO_REPEAT lookup/promociju: skida količinski/pakovni šum
    iz naziva (npr. "papuce 1008 par" i "papuce 500 par" → "PAPUCE") pa se isti
    proizvod iz više deklaracija grupiše zajedno. Uzima prvi proizvod prije zareza
    (naziv kolone često nabraja više artikala u fallback ekstrakciji).

    Unicode folding (š→S, č→C...) ujednačava nazive iz XML-a (bez dijakritika)
    s nazivima s faktura (s dijakritikama) pa se isti artikal uvijek pogodi.

    Mora biti IDENTIČNA na strani promocije (auto_learn) i lookupa (_find_auto_repeat),
    inače se promovisani red nikad ne pogodi u runtime-u.
    """
    import unicodedata
    s = str(name).lower()
    s = re.split(r"[;,]", s)[0]           # prvi artikal ako ih linija nabraja više
    s = _REPEAT_RE_PACK.sub(" ", s)
    s = _REPEAT_RE_QTY.sub(" ", s)
    s = _REPEAT_RE_DASH.sub(" ", s)
    s = _REPEAT_RE_NUM.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" -.,/")
    # Strip diacritics: š→s, č→c, ć→c, ž→z, đ→d itd.
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return _normalize_name(s)


# Stop words koje se uklanjaju pri keyword normalizaciji (mora biti identično skripti)
_KTM_STOP_WORDS = {
    "kom", "kg", "pak", "pcs", "set", "br", "no", "lot", "rll",
    "pce", "pc", "box", "ctn", "bag", "roll", "pair", "pr", "bx",
    "m2", "m3", "ltr", "lit", "ml", "gr", "mg", "ton", "t",
    "za", "od", "na", "sa", "iz", "po", "do", "i", "ili", "te",
    "a", "s", "u", "o", "k", "g", "nk",
    "ostalo", "ostali", "ostale", "razno", "ostala", "tip", "vrsta",
    "model", "modeli", "novo", "nova", "novi",
}
_KTM_RE_NUMBERS = re.compile(r'\b\d+[\.,]?\d*\s*(cm|mm|m|kg|g|ml|l|v|w|hz|°c|kom|pcs|pc|pce|br|no)?\b', re.I)
_KTM_RE_PUNCT   = re.compile(r'[^\w\s]')
_KTM_MIN_CONF   = 0.80  # Prag confidence za keyword match (viši = konzervativniji)

# Keywords koji su istorijski naučili POGREŠAN tarifni broj kroz KTM
# (npr. "daska" povučena na WC dasku, "masažer" na poljoprivredni kombajn).
# Stavke koje sadrže ovakve riječi PRESKAČU KTM lookup i idu direktno na AI,
# gdje _CLASSIFICATION_RULES eksplicitno definiše tačan tarifni broj.
_KTM_BLACKLIST = {
    "daska", "daske", "sklekov",                  # daska za sklekove → ch95 (ne WC daska ch39)
    "masažer", "masazer", "masažeri", "masazeri",  # masažer → ch90 (ne kombajn ch84)
    "žarulja", "zarulja", "žarulje", "zarulje",   # žarulja → ch85 (ne kutije ch48)
    "klijesta", "kliješta", "noktarica",          # nokte/noktarica → ch82 (ne plastika ch39)
    "tricikl", "tricikli",                         # tricikl igračka → ch95 (specifični subcode)
}


def _normalize_for_ktm(name: str) -> list[str]:
    """Normalizuj naziv za keyword_tariff_map lookup — vraća listu uppercase words."""
    text = name.upper().strip()
    text = _KTM_RE_NUMBERS.sub(' ', text)
    text = _KTM_RE_PUNCT.sub(' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return [w for w in text.split() if w.lower() not in _KTM_STOP_WORDS and len(w) >= 2]


def _find_keyword_tariff(name: str, db: Session) -> dict | None:
    """
    Pretraži keyword_tariff_map tabelu za dati naziv stavke.
    Strategija: trigram → bigram → unigram (od najspecifičnijeg ka opštijem).
    Vraća {tariff_code, official_desc, confidence_score, keyword, keyword_type} ili None.

    Koristi se kao brzi lookup PRIJE AI klasifikacije — štedi Haiku+Sonnet pozive
    za stavke koje su se već pojavile u historijskim deklaracijama.
    """
    words = _normalize_for_ktm(name)
    if not words:
        return None

    # Blacklist bypass: ako naziv sadrži riječ za koju KTM historijski daje krivi kod,
    # preskoči lookup pa stavka ide na AI gdje pravila djeluju.
    name_lower_norm = name.lower()
    if any(bad in name_lower_norm for bad in _KTM_BLACKLIST):
        return None

    n = len(words)
    # Generiši kandidate od najspecifičnijeg (trigram) ka najopštijem (unigram)
    candidates: list[tuple[str, str]] = []  # (keyword, type)
    for i in range(n - 2):
        candidates.append((f"{words[i]} {words[i+1]} {words[i+2]}", 'trigram'))
    for i in range(n - 1):
        candidates.append((f"{words[i]} {words[i+1]}", 'bigram'))
    for w in words:
        if len(w) >= 3:
            candidates.append((w, 'unigram'))

    if not candidates:
        return None

    # Jedan SQL poziv s IN clause za sve kandidate
    placeholders = ','.join('?' for _ in candidates)
    kw_list   = [c[0] for c in candidates]
    type_list = [c[1] for c in candidates]

    # SQLite ne podržava WHERE (a,b) IN ((a1,b1),...) bez workarounda,
    # pa koristimo CASE-based filter kroz Python
    all_kws = list({c[0] for c in candidates})
    if not all_kws:
        return None

    ph = ','.join(f':k{i}' for i in range(len(all_kws)))
    try:
        from sqlalchemy import text as _sa_text
        params = {f"k{i}": kw for i, kw in enumerate(all_kws)}
        params["min_conf"] = _KTM_MIN_CONF
        rows = db.execute(
            _sa_text(
                f"SELECT keyword, keyword_type, dominant_tariff_code, official_desc, "
                f"confidence_score, frequency FROM keyword_tariff_map "
                f"WHERE keyword IN ({ph}) AND confidence_score >= :min_conf"
            ),
            params,
        ).fetchall()
    except Exception:
        # Tabela možda ne postoji (stara instanca) — tiho ignorišemo
        return None

    if not rows:
        return None

    # Indeksiraj po (keyword, type)
    row_map: dict[tuple, object] = {}
    for r in rows:
        row_map[(r[0], r[1])] = r

    # Vrati prvi pogodak u prioritetnom redoslijedu (trigram > bigram > unigram)
    for kw, kw_type in candidates:
        hit = row_map.get((kw, kw_type))
        if hit:
            return {
                "tariff_code":      hit[2],
                "official_desc":    hit[3] or "",
                "confidence_score": hit[4],
                "frequency":        hit[5],
                "keyword":          kw,
                "keyword_type":     kw_type,
            }
    return None


def _find_correction(name: str, db: Session) -> dict | None:
    """
    Look up AGENT-CONFIRMED corrections for this item name (confirmations >= 1).

    Auto-cached AI results (confirmations=0, source='ai_batch') are IGNORED here
    because they would block the classifier's rules from re-evaluating items.
    Only human-verified corrections are allowed to short-circuit the AI path.
    """
    normalized = _normalize_name(name)
    if not normalized:
        return None

    # 1. Exact match — only confirmed (confirmations >= 1)
    row = (
        db.query(TariffCorrection)
        .filter(TariffCorrection.item_name_normalized == normalized)
        .filter(TariffCorrection.confirmations >= 1)
        .order_by(TariffCorrection.confirmations.desc())
        .first()
    )
    if row:
        return {
            "tariff_code": row.tariff_code,
            "official_desc": row.official_desc or "",
            "confirmations": row.confirmations,
            "matched_name": row.item_name_original,
            "match_type": "exact",
        }

    # 2. Word-overlap match — only confirmed candidates
    query_words = set(normalized.split())
    if len(query_words) < 2:
        return None

    candidates = (
        db.query(TariffCorrection)
        .filter(TariffCorrection.confirmations >= 1)
        .order_by(TariffCorrection.confirmations.desc())
        .limit(200)
        .all()
    )
    best = None
    best_score = 0.0
    for c in candidates:
        c_words = set((c.item_name_normalized or "").split())
        if not c_words:
            continue
        overlap = len(query_words & c_words)
        score = overlap / max(len(query_words), len(c_words))
        # Weight by number of confirmations (more confirmations = more trustworthy)
        weighted = score * (1 + 0.1 * min(c.confirmations, 10))
        if weighted > best_score and score >= 0.75:
            best_score = weighted
            best = c

    if best:
        return {
            "tariff_code": best.tariff_code,
            "official_desc": best.official_desc or "",
            "confirmations": best.confirmations,
            "matched_name": best.item_name_original,
            "match_type": f"word_overlap({best_score:.0%})",
        }

    return None


def _find_auto_repeat(name: str, db: Session) -> dict | None:
    """
    Auto-promovisan kod nakon 5+ konzistentnih ponavljanja u historiji
    (backend/auto_learn.py, source="auto_repeat(N)"). NAMJERNO odvojeno od
    _find_correction() — auto-promocija piše confirmations=0 baš zato da se
    ne miješa s ljudski potvrđenim korekcijama (confirmations>=1). Ovaj korak
    postoji da AI ipak bude preskočen za takve stavke, dok zapis u bazi ostaje
    vidljivo označen kao "sistem naučio ponavljanjem", ne "čovjek potvrdio".
    """
    normalized = _normalize_repeat_name(name)
    if not normalized:
        return None
    row = (
        db.query(TariffCorrection)
        .filter(TariffCorrection.item_name_normalized == normalized)
        .filter(TariffCorrection.source.like("auto_repeat%"))
        .first()
    )
    if row:
        return {
            "tariff_code": row.tariff_code,
            "official_desc": row.official_desc or "",
            "source_label": row.source,
        }
    return None


def _find_historical(name: str, db: Session, limit: int = 5) -> list[dict]:
    """
    RAG lookup: find historically used tariff codes for similar item names.
    Uses vectorstore semantic search first (better quality), falls back to SQL LIKE.
    Returns list of {description, tariff_code, official_desc, score} sorted by relevance.
    """
    # Primary: vectorstore semantic search over TariffRecord descriptions
    try:
        from tariff_vectorstore import search as vs_search
        vs_hits = vs_search(name, chapter=None, n=limit * 3)
        if vs_hits:
            # Map vectorstore results back to TariffRecord data via the code
            from database import TariffRecord
            seen: dict[str, dict] = {}
            for h in vs_hits:
                code = _clean_code(h.get("code", ""))
                if not code or h.get("distance", 1.0) > 0.75:
                    continue
                # Look up a TariffRecord for this code to get historical description
                record = db.query(TariffRecord).filter_by(tariff_code=code).first()
                if record and code not in seen:
                    seen[code] = {
                        "description": record.description or h.get("description", ""),
                        "tariff_code": code,
                        "official_desc": h.get("description", "") or record.official_desc or "",
                        "score": 1.0 - h.get("distance", 0.5),
                    }
            if seen:
                sorted_hits = sorted(seen.values(), key=lambda x: -x["score"])
                return sorted_hits[:limit]
    except Exception as e:
        logger.debug(f"  _find_historical vectorstore greška: {e}, fallback na SQL")

    # Fallback: SQL LIKE keyword search
    from xml_parser import search_tariff
    hits = search_tariff(name, db, limit=limit * 2)
    if not hits:
        return []

    seen2: dict[str, dict] = {}
    for r in hits:
        words_q = set(name.lower().split())
        words_r = set(r.description.split())
        overlap = len(words_q & words_r)
        score = overlap / len(words_q | words_r) if (words_q | words_r) else 0
        if r.tariff_code not in seen2 or score > seen2[r.tariff_code]["score"]:
            seen2[r.tariff_code] = {
                "description": r.description,
                "tariff_code": r.tariff_code,
                "official_desc": r.official_desc or "",
                "score": score,
            }

    sorted_hits2 = sorted(seen2.values(), key=lambda x: -x["score"])
    return sorted_hits2[:limit]


def _get_all_chapters(db: Session) -> list[dict]:
    """Return list of {chapter, first_desc} for chapter selection prompt."""
    rows = db.query(OfficialTariff.chapter, OfficialTariff.description_bs).all()
    seen = {}
    for chapter, desc in rows:
        if chapter not in seen:
            seen[chapter] = desc
    result = []
    for ch in sorted(seen.keys()):
        result.append({"chapter": ch, "desc": seen[ch]})
    return result


def _get_chapter_codes(chapter: str, db: Session) -> list[OfficialTariff]:
    """Return all codes for a given chapter."""
    return db.query(OfficialTariff).filter(OfficialTariff.chapter == chapter).order_by(OfficialTariff.code).all()


# Step 1 (chapter selection, 68 options) — Haiku is accurate and cheap
_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
# Step 2 (exact code within chapter, ~30-40 candidates) — Sonnet required for precision
_CLASSIFIER_MODEL_S2 = "claude-sonnet-4-6"

# Max codes to send to Claude per chapter. Vectorstore filters down to this.
# Prevents sending 1000+ codes for large chapters like 84, 85, 29.
_MAX_CODES_PER_CALL = 40

# --- Safety floor za KG filter (_filter_codes_for_items) ---
# Svaki od ovih pragova, kad se prekrši, vraća CIJELO poglavlje. Izgladnjivanje
# Sonneta (slanje premalo kodova) je tačno ono što je ubilo stari VS filter, pa je
# default uvijek "pošalji sve". Vrijednosti verifikovane u scripts/backtest_kg_filter.py.
_MIN_VS_CODES   = 10    # familija manja od ovoga = premalo signala za povjerenje
_SMALL_CHAPTER  = 120   # poglavlja do ove veličine su jeftina — ne filtriraj ih uopšte
_MAX_KEEP_RATIO = 0.6   # ako bi zadržali >60% poglavlja, filter se ne isplati

# Vectorstore-gated chapter forcing thresholds.
# When VS returns ≥_VS_FORCE_MIN_HITS hits all in the same chapter with
# distance < _VS_FORCE_THRESHOLD, that chapter is forced (Claude step 1 bypassed).
# Self-improving: every agent correction → VS upsert → next similar item auto-forced.
_VS_FORCE_THRESHOLD = 0.35   # distance below this = confident VS hit
_VS_FORCE_MIN_HITS  = 3      # need ≥3 confident hits in same chapter to force
_VS_POSTVAL_THRESHOLD = 0.25  # override low-confidence AI result if VS top-1 < this

# Chapter keyword overrides — items that Claude reliably misclassifies at the chapter level.
# Each entry: (set_of_keywords_any_of_which_trigger, forced_chapter_2digit)
# Applied BEFORE batch chapter selection — bypasses Claude step 1 entirely for matched items.
#
# Why needed: Claude picks chapter by item description, but short translated names lose context.
# "BRITVA ZA LICE" → Claude picks ch39 (plastics) instead of ch82 (metal cutting tools).
# Forcing ch82 lets Claude correctly pick 82121010 in step 2.
_CLASSIFICATION_RULES = """PRAVILA KLASIFIKACIJE (primijeni redom):

1. SPECIFIČNOST: Beri najspecifičniji kod koji opisuje robu, ne generički.
   - "FILTER ULJA" → kod za filter ulja motora, NE generički filter
   - "PODLOGA SAOBRAĆAJ" → tekstilna podna obloga (ch57), NE auto-dio (ch87)
   - "DEKORATIVNA ŽARULJA" → električna sijalica ch85, NE papirnate kutije ch48

2. MATERIJAL vs NAMJENA — za gotove proizvode NAMJENA ima prednost:
   - "DASKA ZA SKLEKOVE" → sportska oprema 9506 (ch95), NE 9503 (igračka), NE ch39 (WC daska)
   - "MASAŽER" → medicinski/masažni aparat 9019 (ch90), NE ch84 (kombajn)
   - "KLIJEŠTA ZA NOKTE / NOKTARICA" → manikir set 8214 (ch82), NE ch39 (plastika)
   - "ŽARULJA" / "SIJALICA" sam izvor svjetla → 8539 (ch85)
   - "LAMPA" / "SVJETILJKA" / "LUSTER" kućište+sijalica → 9405 (ch94)
   - Tekstilna podloga/prostirka za auto → ch57 (tekstil), ne ch87 (vozila)
   - Plastična kutija za alat → ch39 (plastika), ne ch82 (alati)
   - Gumeni poklopac → ch40 (guma), ne ch84 (strojevi)

3. NAMJENA određuje kod kad materijal nije presudan:
   - "SIGURNOSNI POJAS" → ch87 (auto-dijelovi), ne ch63 (tekstil)
   - "ZAŠTITNA RUKAVICA" → ch39/40 ovisno o materijalu, ne ch62 (odjeća)

4. KONTEKST POŠILJKE koristi za razjašnjavanje ambigviteta:
   - Pošiljka sadrži cipele/obuću → "ĐON" je potplat cipele (ch64)
   - Pošiljka sadrži namještaj → "OBLOGA" je presvlaka namještaja (ch94)
   - Pošiljka sadrži igračke → "AUTO" je igračka (ch95), ne vozilo (ch87)

5. NE biraj kod po jednoj sličnoj riječi — pročitaj CIJELI naziv I opis robe.
   - "daska" ne znači automatski WC daska — provjeri o čemu se radi
   - "kombajn" se ne odnosi na masažer (slična riječ, drugačija roba)
   - Provjeri da kod STVARNO opisuje TU robu, ne nešto slično po imenu
   - Ako roba ima više funkcija, klasificiraj po GLAVNOJ namjeni

6. CIJENA KAO SIGNAL (igračka vs prava roba):
   - Niska jedinična cijena (ispod ~10 EUR) za "kamion", "kolica", "motor" →
     vjerovatno IGRAČKA (ch95), ne prava roba
   - Visoka cijena (preko ~30 EUR) → može biti prava roba:
     * "kolica za bebe" skupo → prava dječija kolica (8715), ne igračka
     * "kamion/auto na akumulator" skupo → ride-on igračka (95030075)
   - Kombinuj cijenu SA nazivom — cijena je hint, naziv je glavni:
     * jeftino + "kolica za lutke" → igračka 95030010
     * skupo + "dječija kolica" → prava 8715
   - Ako je cijena niska a naziv dvosmislen → defaultuj na IGRAČKU (ch95)"""


# Per-chapter classification hints — injected only when batch is for that chapter.
# Use for ambiguities where one chapter has multiple competing tariff families.
_CHAPTER_HINTS: dict[str, str] = {
    "84": """RAZLIKOVANJE alata u poglavlju 84 (mašine i mehanički uređaji):
- 8467 = ALATI koji se DRŽE U RUCI sa motorom (pneumatski/hidraulični/električni):
    - 8467 11/19 = PNEUMATSKI: PIŠTOLJ ZA EKSERE, čekić, klamerica, nail gun
    - 8467 21/22 = ELEKTRIČNI bušilice / pile / brusilice (drill, saw, grinder)
    - 8467 29 = ostali električni ručni alati
    - 8467 81/89 = neelektrični ostali
- 8479 = mašine sa vlastitim funkcijama koje nisu spomenute drugdje (CAUTION:
    ne biraj 8479 za stavku koja se kvalificira kao 8467 — 8479 je posljednja opcija)
- 8508 = USISIVAČI (NIJE ovdje — to je ch85)
- 8509 = ELEKTRIČNI KUHINJSKI/HOUSEHOLD aparati (NIJE ovdje — to je ch85)""",
    "82": """RAZLIKOVANJE unutar poglavlja 82 (nožarija, oštrice):
- 8211 = NOŽEVI s rezilom (kuhinjski, dezertni, mesarski, voćni):
    NOŽ ZA PIZZU, NOŽ SET, NOŽ KUHINJSKI, NOŽ MESARSKI, NOŽ ZA HLJEB, NOŽ ZA SIR
- 8212 = BRITVE i britvice (shave razors)
- 8213 = MAKAZE i škare
- 8214 = ostali nožarski proizvodi (manikir setovi, turpije za nokte, papir sjekire)
- 8215 = SET PRIBORA ZA JELO (nož + vilica + kašika u jednom paketu)
Razlika 8211 vs 8215: 8211 = SAMO noževi (čak i set noževa); 8215 = mješoviti pribor.
"NOŽ SET 6/1" = 6 noževa u 1 setu → 8211 (set noževa, NE 8215).""",
    "95": """SPORTSKA OPREMA vs IGRAČKA — ključna razlika unutar poglavlja 95:
- 9506 = SPORTSKA OPREMA za ODRASLE (vježbanje, fitness, gimnastika):
    DASKA ZA SKLEKOVE, BUČICE, TEGOVI, ELASTIČNE TRAKE, PODLOGA ZA JOGU,
    STEPER, UTEG, KETTLEBELL, LOPTA ZA FITNES, FITNES TRAKE → 95069190
- 9503 = DJEČIJE IGRAČKE (samo za djecu):
    AUTO/KAMION/AVION igračka, LUTKA, KOCKE, FIGURE, ANIMALI igračke
- 9504 = ZABAVNE IGRE:
    društvene igre, igraće karte, video konzole
- Pitaj se: koristi li ovo ODRASLA OSOBA za vježbu? → 9506 (NE 9503!)
  Da li je to dječija igračka? → 9503.""",
}


def _filter_codes_for_items(
    item_names: list[str],
    chapter: str,
    chapter_codes: list[OfficialTariff],
) -> list[OfficialTariff]:
    """
    Suzi kodove poglavlja na FAMILIJE (4-cifrene tarifne brojeve) koje su relevantne
    za date stavke, preko knowledge grafa (backend/tariff_graph.py).

    Zašto je ovo sigurno, a stari VS filter nije: stari filter je birao POJEDINAČNE
    kodove po semantičkoj sličnosti, pa je gubio sibling-e (ŠTAPNI MIKSER → cijela
    8509 familija odsječena; DASKA ZA SKLEKOVE → 9506 9190 odsječen). Ovaj filter
    vraća uniju KOMPLETNIH heading-a, pa se sibling ne može pojedinačno izgubiti.
    Dokazano: scripts/backtest_kg_filter.py, recall 100% nad 3.002 ground-truth para.

    Safety floor — kad god nema dovoljno signala, vrati CIJELO poglavlje. Bolje skupo
    nego pogrešno; izgladnjivanje Sonneta je tačno ono što je ubilo prethodni filter.
    """
    try:
        import tariff_graph

        # Mala poglavlja su jeftina — filtriranje ne donosi ništa, a nosi rizik.
        if len(chapter_codes) <= _SMALL_CHAPTER:
            return chapter_codes

        headings, strong = tariff_graph.seed_headings_for(
            item_names, chapter, with_strength=True
        )
        # TVRDI DOKAZ je uslov za filtriranje: agentska korekcija, keyword_tariff_map, ili
        # historijski zapis s istim nazivom ("ovu stavku smo već vidjeli"). Meki VS/graf
        # seed NE generalizuje na neviđene stavke — holdout backtest: recall pada na 90-98%,
        # tj. tačan kod se tiho baca. To je tačno ono što je ubilo stari VS filter.
        if not strong:
            logger.info(f"  KG filter ch{chapter}: nema tvrdog dokaza → cijelo poglavlje ({len(chapter_codes)})")
            return chapter_codes

        family = tariff_graph.expand_to_families(headings, chapter)
        if len(family) < _MIN_VS_CODES:
            logger.info(f"  KG filter ch{chapter}: familija premala ({len(family)}) → cijelo poglavlje")
            return chapter_codes
        if len(family) / len(chapter_codes) > _MAX_KEEP_RATIO:
            logger.info(f"  KG filter ch{chapter}: {len(family)}/{len(chapter_codes)} — filter se ne isplati → cijelo poglavlje")
            return chapter_codes

        kept = [c for c in chapter_codes if c.code in family]
        if len(kept) < _MIN_VS_CODES:
            return chapter_codes

        logger.info(
            f"  KG filter ch{chapter}: {len(chapter_codes)} → {len(kept)} kodova, "
            f"{len(headings)} familija: {sorted(headings)}"
        )
        return kept
    except Exception as e:
        # Graf pao / DB nedostupan → nikad ne obaraj klasifikaciju, vrati sve.
        logger.warning(f"  KG filter ch{chapter} pao ({e}) → cijelo poglavlje")
        return chapter_codes


def classify_item(
    item_name: str,
    item_value: float = 0.0,
    item_weight: float = 0.0,
    db: Session = None,
    shipment_context: list | None = None,
) -> dict:
    """
    Classify a single item using Claude AI (2-step: chapter → exact code).
    shipment_context: list of other item names in the same shipment (for better context).
    Returns: {tariff_code (8 digits), confidence, reason}
    """
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
        total_official = db.query(OfficialTariff).count()
        if total_official == 0:
            return {"tariff_code": "", "confidence": "low", "reason": "Tarifa nije učitana"}

        name_lower = item_name.lower()

        # ── Kontekst pošiljke — ostale stavke pomažu Claude-u da razumije prirodu robe
        context_block = ""
        if shipment_context:
            others = [n for n in shipment_context if n.lower() != name_lower][:20]
            if others:
                context_block = "\nKONTEKST POŠILJKE (ostale stavke u deklaraciji): " + ", ".join(others) + "\n"

        # ── Agent correction ──────────────────────────────────────────────
        correction = _find_correction(item_name, db)
        if correction:
            code_c = _clean_code(correction["tariff_code"])
            total_official = db.query(OfficialTariff).count()
            valid = {_clean_code(r.code) for r in db.query(OfficialTariff).all()} if total_official else set()
            if code_c in valid:
                logger.info(f"  '{item_name[:45]}' → {code_c} [correction {correction['confirmations']}x, individual path]")
                return {"tariff_code": code_c, "confidence": "high",
                        "reason": f"correction({correction['confirmations']}x)"}

        # ── Keyword tariff map lookup ─────────────────────────────────────
        ktm = _find_keyword_tariff(item_name, db)
        if ktm:
            code_k = _clean_code(ktm["tariff_code"])
            valid_k = {_clean_code(r.code) for r in db.query(OfficialTariff).all()}
            if code_k in valid_k:
                logger.info(
                    f"  '{item_name[:45]}' → {code_k} "
                    f"[ktm '{ktm['keyword']}' {ktm['keyword_type']}, conf={ktm['confidence_score']:.0%}, individual]"
                )
                return {"tariff_code": code_k, "confidence": "high",
                        "reason": f"ktm_{ktm['keyword_type']}(conf={ktm['confidence_score']:.0%})"}

        client = _get_client()
        value_hint = f", vrijednost: {item_value:.2f} EUR" if item_value > 0 else ""
        weight_hint = f", težina: {item_weight:.3f} kg" if item_weight > 0 else ""

        # ── RAG: historijski kontekst iz prošlih deklaracija ─────────────
        historical = _find_historical(item_name, db)
        historical_text = ""
        if historical:
            lines = [f"  {h['description'].upper()} → {h['tariff_code']} ({h['official_desc'][:60]})"
                     for h in historical]
            historical_text = "HISTORIJSKI SLUČAJEVI IZ PROŠLIH DEKLARACIJA (visoki prioritet):\n" + "\n".join(lines) + "\n\n"

        # ━━━ GRANA A: IGRAČKE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # Za sve što sadrži toy/igračka keyword: preskoči odabir poglavlja,
        # idi direktno na sve kodove poglavlja 95 i pusti Claudea da izabere.
        # Ovo garantuje da plastične/drvene/električne igračke NIKAD ne idu u 39/44/85.
        _TOY_KW = {
            "igračka", "igracka", "igr.", "toy", "toys",
            "puzzle", "puzle", "slagalica", "plastelin", "plastilin",
            "baby walker", "hodalica", "prohodalica", "guralica",
            "inflatable", "napuhljiv", "doll", "plush", "stuffed",
        }
        is_toy = any(kw in name_lower for kw in _TOY_KW)

        if is_toy:
            ch95_codes = _get_chapter_codes("95", db)
            if not ch95_codes:
                return {"tariff_code": "", "confidence": "low", "reason": "Poglavlje 95 nije u tarifi"}

            codes_text_95 = "\n".join(f"  {c.code} — {c.description_bs}" for c in ch95_codes)

            toy_prompt = f"""Ti si carinski stručnjak. Roba je IGRAČKA i mora biti klasificirana u poglavlju 95.

ROBA: "{item_name}"{value_hint}{weight_hint}
{context_block}
{historical_text}Odaberi NAJTAČNIJI tarifni broj iz liste svih kodova poglavlja 95 Carinske tarife BiH 2026:

{codes_text_95}

SMJERNICE:
- Plastelin/modelling clay → 3407000000 (iznimka: ide u poglavlje 34, a ne 95)
- Slagalice/puzzle → 95030061XX ili 95030069XX
- Električni vlakovi/vozovi → 95030030XX
- Auto/motor/kamion/bager/traktor na akumulator (ride-on) → 95030075XX
- Guralice/kolica za bebe/baby walker/prohodalice → 95030010XX
- Napuhljive igračke za jahanje (konj) → 95030049XX
- Lutke (beba, dojenče, djevojčica, baby doll, igr. beba) → 95030021XX ili 95030029XX
  VAŽNO: "beba na rolama", "beba hodajuća" i sl. → 95030021XX (LUTKA, nije oružje!)
- Kocke/konstruktor → 95030035XX ili 95030039XX
- Igračke životinje (medvjed, pas, mačka, dinosaur...) → 95030041XX ili 95030049XX
- Set/komplet igračaka (policijski set, vatrogasni set, doktorski set...) → 95030070XX
  VAŽNO: "policijski set", "set igracaka" → 95030070XX (NIJE životinja/neljudski lik!)
- Ostale igračke (kamion, motorka, avion, kuhinja...) → 95030095XX
- Oružje-igračke (pištolet, puška, luk i strijela) → 95030081XX
- Društvene igre, igraće karte → 95040000XX
- Video igre/konzole → 95045000XX

Vrati SAMO JSON:
{{"tariff_code": "XXXXXXXX", "confidence": "high|medium|low", "reason": "max 10 rijeci"}}"""

            raw, msg = _call_claude_text(
                client, _CLASSIFIER_MODEL_S2, 400,
                [{"role": "user", "content": toy_prompt}],
                context="classify",
            )
            result = json.loads(raw) if raw else {}
            code = _clean_code(result.get("tariff_code", ""))

            valid_95 = {_clean_code(c.code) for c in ch95_codes}
            # Plastelin exception: chapter 34
            all_valid = {_clean_code(r.code) for r in db.query(OfficialTariff).all()}
            if code == "34070000" and code in all_valid:
                logger.info(f"  '{item_name[:45]}' → {code} [toy/plastelin → ch.34]")
                return {"tariff_code": code, "confidence": "high", "reason": "plastelin → ch.34"}

            if code not in valid_95:
                # Fallback: 95030095
                code = "95030095" if "95030095" in valid_95 else _clean_code(ch95_codes[0].code)
                result["confidence"] = "medium"

            logger.info(f"  '{item_name[:45]}' → {code} [toy→ch95 direct, {result.get('confidence')}]")
            return {
                "tariff_code": code,
                "confidence": result.get("confidence", "medium"),
                "reason": result.get("reason", ""),
            }

        # ━━━ GRANA B: OSTALA ROBA — vector search + 2-step Claude ━━━━━━━━
        # Vectorstore nam daje top-3 poglavlja kao hint za Step 1.
        vector_hint = ""
        try:
            from tariff_vectorstore import search as vs_search
            vs_hits = vs_search(item_name, chapter=None, n=10)
            if vs_hits:
                # Top chapters by frequency among low-distance hits
                from collections import Counter
                hit_chapters = [h["chapter"] for h in vs_hits if h["distance"] < 0.6]
                if hit_chapters:
                    top_ch = Counter(hit_chapters).most_common(3)
                    chapter_suggestions = ", ".join(f"poglavlje {c}" for c, _ in top_ch)
                    vector_hint = f"\nSemanička pretraga sugerira: {chapter_suggestions}.\n"
        except Exception as e:
            logger.debug(f"Vector search preskočen: {e}")

        # ── Korak 1: odaberi poglavlje ────────────────────────────────────
        chapters = _get_all_chapters(db)
        chapters_text = "\n".join(f"  {c['chapter']} — {c['desc']}" for c in chapters)

        chapter_hint = vector_hint
        if historical:
            hist_chapters = [h["tariff_code"][:2] for h in historical if h["tariff_code"]]
            if hist_chapters:
                most_common = max(set(hist_chapters), key=hist_chapters.count)
                chapter_hint += f"\nNapomena: slična roba historijski klasificirana u poglavlju {most_common}."

        step1_prompt = f"""Ti si stručnjak za carinsku klasifikaciju po HS nomenklaturi BiH.

ROBA: "{item_name}"{value_hint}{weight_hint}
{context_block}
{historical_text}{chapter_hint}

Odaberi JEDNO HS poglavlje (2-cifarski broj) iz liste ispod koje NAJBOLJE odgovara ovoj robi.

POGLAVLJA:
{chapters_text}

Vrati SAMO JSON:
{{"chapter": "XX", "reason": "kratko objasnjenje max 10 rijeci"}}"""

        raw1, msg1 = _call_claude_text(
            client, _CLASSIFIER_MODEL, 300,
            [{"role": "user", "content": step1_prompt}],
            context="classify",
        )
        step1 = json.loads(raw1) if raw1 else {}
        chapter = str(step1.get("chapter", "")).zfill(2)

        # ── Korak 2: odaberi tačan kod unutar poglavlja ───────────────────
        chapter_codes = _get_chapter_codes(chapter, db)
        if not chapter_codes:
            logger.warning(f"Poglavlje {chapter} prazno za '{item_name}', pokušavam širu pretragu")
            return {"tariff_code": "", "confidence": "low", "reason": f"Poglavlje {chapter} nije pronađeno"}

        # Filter codes to relevant subset — avoids sending 1000+ codes for large chapters
        filtered_codes = _filter_codes_for_items([item_name], chapter, chapter_codes)
        codes_text = "\n".join(f"  {c.code} — {c.description_bs}" for c in filtered_codes)

        hist_codes_in_chapter = [h for h in historical if h["tariff_code"][:2] == chapter]
        hist_highlight = ""
        if hist_codes_in_chapter:
            hist_highlight = "\nHistorijski korišteni kodovi za sličnu robu: " + \
                ", ".join(h["tariff_code"] for h in hist_codes_in_chapter) + "\n"

        step2_prompt = f"""{_CLASSIFICATION_RULES}

Roba: "{item_name}"{value_hint}{weight_hint}
{context_block}
{historical_text}{hist_highlight}
Odaberi TAČAN tarifni broj iz poglavlja {chapter} Carinske tarife BiH 2026.

POSTUPAK:
1. Šta je ova roba? (materijal, namjena, oblik)
2. Koji kodovi odgovaraju?
3. Koji je NAJTAČNIJI?

DOSTUPNI KODOVI:
{codes_text}

Vrati SAMO JSON:
{{"tariff_code": "XXXXXXXX", "confidence": "high|medium|low", "reason": "max 8 rijeci"}}"""

        raw2, msg2 = _call_claude_text(
            client, _CLASSIFIER_MODEL_S2, 500,
            [{"role": "user", "content": step2_prompt}],
            context="classify",
        )
        result = json.loads(raw2) if raw2 else {"tariff_code": "", "confidence": "low"}
        code = _clean_code(result.get("tariff_code", ""))

        valid_codes = {_clean_code(c.code) for c in chapter_codes}
        if code not in valid_codes:
            # NE dodjeljuj proizvoljan prvi kod poglavlja — VS top-1 u poglavlju,
            # inače prazno + low (post-val / suspect flag downstream).
            vs_code = _vs_code_in_chapter(item_name, chapter, valid_codes)
            logger.warning(
                f"'{item_name}': Claude vratio '{code}' van poglavlja {chapter} → "
                f"{'VS ' + vs_code if vs_code else 'prazno (za ručnu provjeru)'}"
            )
            code = vs_code
            result["confidence"] = "low"

        # Post-validacija: igračka u pogrešnom poglavlju (safety net)
        is_toy_name = any(kw in name_lower for kw in _TOY_KW)
        wrong_chapter = code[:2] in {"84", "85", "87", "39", "44", "73", "94", "48", "63", "93", "92"}
        toy_codes_list = db.query(OfficialTariff).filter(OfficialTariff.code.like("9503%")).all()
        if is_toy_name and wrong_chapter and toy_codes_list:
            code_9503_95 = next((c for c in toy_codes_list if _clean_code(c.code) == "95030095"), None)
            forced = _clean_code((code_9503_95 or toy_codes_list[0]).code)
            logger.warning(
                f"  '{item_name[:45]}' → POST-VALID override {code}→{forced} "
                f"(igračka u poglavlju {code[:2]})"
            )
            code = forced
            result["confidence"] = "high"

        return {
            "tariff_code": code,
            "confidence": result.get("confidence", "medium"),
            "reason": result.get("reason", ""),
        }

    except Exception as e:
        logger.error(f"Klasifikacija neuspješna za '{item_name}': {e}")
        return {"tariff_code": "", "confidence": "low", "reason": str(e)[:80]}
    finally:
        if close_db:
            db.close()


def _fill_official_desc_from_db(item_dict: dict, db: Session) -> dict:
    """Populate official_desc and heading_bs by querying official_tariffs.

    Module-level counterpart to the in-classify_items_batch closure version,
    callable from any path (batch success, per-item fallback) that finishes
    classification without having those fields filled. No-op when the fields
    are already present or the code is unknown.
    """
    code = _clean_code(item_dict.get("tariff_code", ""))
    if not code:
        return item_dict
    if item_dict.get("official_desc") and item_dict.get("heading_bs"):
        return item_dict
    db_code = code.ljust(10, "0")
    row = db.query(OfficialTariff).filter_by(code=db_code).first()
    if not row:
        return item_dict
    if not item_dict.get("official_desc"):
        item_dict["official_desc"] = row.description_bs or ""
    if not item_dict.get("heading_bs"):
        item_dict["heading_bs"] = row.heading_bs or ""
    return item_dict


def _batch_select_chapters(
    items: list[tuple],  # (result_idx, name, value, weight)
    chapters: list[dict],
    client,
    vector_hints: dict,  # {result_idx: "poglavlje XX, YY"}
) -> dict:
    """One Claude call to select HS chapter for ALL items. Returns {result_idx: chapter_2digit}."""
    chapters_text = "\n".join(f"  {c['chapter']} — {c['desc']}" for c in chapters)
    numbered = []
    for seq, (ridx, name, value, weight) in enumerate(items, 1):
        parts = [f"{seq}. {name}"]
        if value > 0:
            parts[0] += f", {value:.2f} EUR"
        if weight > 0:
            parts[0] += f", {weight:.3f} kg"
        hint = vector_hints.get(ridx, "")
        if hint:
            parts[0] += f" [hint: {hint}]"
        numbered.append(parts[0])
    items_text = "\n".join(numbered)

    prompt = f"""Ti si stručnjak za carinsku klasifikaciju. Za SVAKU robu ispod odaberi JEDNO HS poglavlje (2 cifre).

PRAVILA ZA ODABIR POGLAVLJA (gleda NAMJENU robe, ne samo sličnu riječ):
- DASKA ZA SKLEKOVE → ch95 (sport oprema), NE ch39 (WC daska)
- MASAŽER → ch90 (medicinski aparat), NE ch84 (poljopriv. kombajn)
- KLIJEŠTA ZA NOKTE / NOKTARICA → ch82 (manikir), NE ch39 (plastika)
- ŽARULJA / SIJALICA (sam izvor svjetlosti) → ch85, čak i kad piše "DEKORATIVNA"
- LAMPA / SVJETILJKA / LUSTER (kućište + sijalica) → ch94
- PODLOGA / PROSTIRKA SAOBRAĆAJ → ch57 (tepih), NE ch87 (auto)
- Igračke (TRICIKL, AUTO, KAMION ako su dječije) → ch95, NE ch87 (vozila)
- MIKSER / BLENDER / SOKOVNIK / FEN / PEGLA / TOSTER / FRITEZA / KUHINJSKI
  aparat sa elektromotorom → ch85 (8509 household appliances), NE ch84
- USISIVAČ (ručni, ručno-vođen, ROBOT USISIVAČ) → ch85 (8508), NE ch84
- "DASKA", "KOMBAJN", "AUTO" mogu značiti različite stvari — pročitaj CIJELI naziv

STAVKE:
{items_text}

Vrati SAMO JSON array s tačno {len(items)} stavki (redosljed mora biti isti):
[{{"n": 1, "chapter": "XX"}}, {{"n": 2, "chapter": "XX"}}, ...]"""

    msg = client.messages.create(
        model=_CLASSIFIER_MODEL,
        max_tokens=len(items) * 20 + 100,
        messages=[{"role": "user", "content": [
            {
                "type": "text",
                "text": f"POGLAVLJA CARINSKE TARIFE BiH 2026:\n{chapters_text}",
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": prompt},
        ]}],
    )
    usage = msg.usage
    logger.info(
        f"  Batch ch-select [{_CLASSIFIER_MODEL.split('-')[1]}]: "
        f"in={usage.input_tokens} out={usage.output_tokens} "
        f"cache_w={getattr(usage, 'cache_creation_input_tokens', 0)} "
        f"cache_r={getattr(usage, 'cache_read_input_tokens', 0)}"
    )
    try:
        from database import log_api_usage
        log_api_usage(None, _CLASSIFIER_MODEL, "classify", usage)
    except Exception as _lue:
        logger.warning(f"  log_api_usage greška: {_lue}")
    raw = msg.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json\n"):
            raw = raw[5:]
    assignments = json.loads(raw.strip())
    result = {}
    for a in assignments:
        seq = int(a["n"]) - 1
        if 0 <= seq < len(items):
            ridx = items[seq][0]
            result[ridx] = str(a.get("chapter", "")).zfill(2)
    return result


def _call_claude_text(client, model, max_tokens, messages, context="classify"):
    """
    Zovi Claude s JEDNIM retryjem (2× max_tokens) kad je tekst blok prazan ili je
    odgovor odsječen na max_tokens. Sonnet adaptivno "misli" pa pretijesan budžet
    zna pojesti sve tokene i ostaviti prazan tekst → json.loads('') pukne
    ("Expecting value: line 1 column 1"). Loguje usage po pokušaju.
    Vraća (text, msg); text ima skinute ``` ograde, može biti '' ako oba padnu.
    """
    from database import log_api_usage
    text, msg = "", None
    for attempt in range(2):
        mt = max_tokens * 2 if attempt else max_tokens
        msg = client.messages.create(model=model, max_tokens=mt, messages=messages)
        try:
            log_api_usage(None, model, context, msg.usage)
        except Exception:
            pass
        text = next((b.text for b in msg.content
                     if getattr(b, "type", None) == "text"), "").strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json\n"):
                text = text[5:]
            text = text.strip()
        if text and getattr(msg, "stop_reason", None) != "max_tokens":
            break
        if attempt == 0:
            logger.warning(
                f"  {context}: prazan/odsječen odgovor "
                f"(stop={getattr(msg, 'stop_reason', None)}, len={len(text)}) — retry {mt}→{mt * 2}"
            )
    return text, msg


def _vs_code_in_chapter(name: str, chapter: str, valid_codes: set) -> str:
    """VS top-1 kod za `name` unutar `chapter` koji je u `valid_codes`, ili ''.
    Data-driven zamjena za tihi 'prvi kod poglavlja' fallback (izvor besmislenih
    dodjela poput 94015900 za neigračku u pogrešnom poglavlju)."""
    try:
        from tariff_vectorstore import search as vs_search
        for h in vs_search(name, chapter=chapter, n=5):
            c = _clean_code(h.get("code", ""))
            if c and c in valid_codes:
                return c
    except Exception:
        pass
    return ""


def _batch_select_codes(
    chapter: str,
    items: list[tuple],  # (result_idx, name, value, weight)
    chapter_codes: list,
    historical_map: dict,  # {result_idx: [hits]}
    client,
    shipment_context: list | None = None,
) -> dict:
    """One Claude call to select exact code for all items in one chapter. Returns {result_idx: {tariff_code, confidence}}."""
    # Filter codes to relevant subset using vectorstore.
    # Critical for large chapters: ch84=1044 codes (~52k tokens) → filtered to ~35 (~1.5k tokens)
    item_names = [name for (_, name, _, _) in items]
    filtered_codes = _filter_codes_for_items(item_names, chapter, chapter_codes)
    codes_text = "\n".join(f"  {c.code} — {c.description_bs}" for c in filtered_codes)

    numbered = []
    for seq, (ridx, name, value, weight) in enumerate(items, 1):
        line = f"{seq}. {name}"
        if value > 0:
            line += f", {value:.2f} EUR"
        if weight > 0:
            line += f", {weight:.3f} kg"
        hist = historical_map.get(ridx, [])
        hist_in_ch = [h["tariff_code"] for h in hist if h["tariff_code"][:2] == chapter]
        if hist_in_ch:
            line += f" [historijski: {', '.join(hist_in_ch[:2])}]"
        numbered.append(line)
    items_text = "\n".join(numbered)

    context_line = ""
    if shipment_context:
        item_names_in_ch = {name for (_, name, _, _) in items}
        others = [n for n in shipment_context if n not in item_names_in_ch][:15]
        if others:
            context_line = f"\nKONTEKST POŠILJKE — ostale stavke u istoj deklaraciji: {', '.join(others)}\n"

    chapter_hint = _CHAPTER_HINTS.get(chapter, "")
    if chapter_hint:
        chapter_hint = "\n" + chapter_hint + "\n"

    prompt = f"""{_CLASSIFICATION_RULES}
{chapter_hint}{context_line}
Odaberi TAČAN tarifni broj iz poglavlja {chapter} Carinske tarife BiH 2026 za svaku stavku.

STAVKE:
{items_text}

DOSTUPNI KODOVI: koristi ISKLJUČIVO listu kodova poglavlja {chapter} datu iznad.

POSTUPAK za svaku stavku:
- Pročitaj naziv i razmisli šta je roba (materijal, namjena, oblik)
- Eliminiraj kodove koji ne odgovaraju
- Odaberi NAJTAČNIJI preostali kod
- Ako nisi siguran između 2 koda → odaberi specifičniji

Vrati SAMO JSON array s tačno {len(items)} stavki:
[{{"n": 1, "tariff_code": "XXXXXXXX", "confidence": "high|medium|low", "reason": "max 8 rijeci"}}, ...]"""

    text, msg = _call_claude_text(
        client, _CLASSIFIER_MODEL_S2, len(items) * 120 + 400,
        [{"role": "user", "content": [
            {
                "type": "text",
                "text": f"KODOVI POGLAVLJA {chapter} — CARINSKA TARIFA BiH 2026:\n{codes_text}",
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": prompt},
        ]}],
        context="classify",
    )
    usage = msg.usage
    logger.info(
        f"  Batch code-select ch{chapter} [{len(filtered_codes)}/{len(chapter_codes)} kodova, {_CLASSIFIER_MODEL_S2.split('-')[1]}]: "
        f"in={usage.input_tokens} out={usage.output_tokens} "
        f"cache_r={getattr(usage, 'cache_read_input_tokens', 0)}"
    )
    if not text:
        raise ValueError(f"prazan AI odgovor za ch{chapter} nakon retryja")
    assignments = json.loads(text)
    valid = {_clean_code(c.code) for c in chapter_codes}
    result = {}
    for a in assignments:
        seq = int(a["n"]) - 1
        if 0 <= seq < len(items):
            ridx = items[seq][0]
            code = _clean_code(a.get("tariff_code", ""))
            conf = a.get("confidence", "low")
            if code not in valid:
                # NE dodjeljuj proizvoljan prvi kod poglavlja (izvor besmislica) —
                # probaj VS top-1 u poglavlju, inače ostavi prazno + low (post-val hvata).
                code = _vs_code_in_chapter(items[seq][1], chapter, valid)
                conf = "low"
            result[ridx] = {"tariff_code": code, "confidence": conf}
    return result


def _classify_batch_and_fill(
    needs_claude: list[tuple],  # (result_idx, item_dict)
    results: list,              # in-place: fill None placeholders
    db: Session,
    shipment_context: list | None = None,
) -> None:
    """
    Batch-classify all items in needs_claude, fill their result slots.
    shipment_context: all item names in the shipment for better classification.
    Falls back to per-item classify_item() on any failure.
    """
    client = _get_client()
    chapters = _get_all_chapters(db)

    forced_chapters: dict[int, str] = {}

    # Collect vectorstore hints for AI chapter selection.
    vector_hints = {}
    needs_chapter_selection = list(needs_claude)
    for ridx, item_dict in needs_chapter_selection:
        name = item_dict.get("name", "")
        try:
            from tariff_vectorstore import search as vs_search
            from collections import Counter
            vs_hits = vs_search(name, chapter=None, n=15)

            # Hint text for Claude (as before)
            hit_chapters = [h["chapter"] for h in vs_hits if h["distance"] < 0.6]
            if hit_chapters:
                top_ch = Counter(hit_chapters).most_common(3)
                vector_hints[ridx] = ", ".join(f"poglavlje {c}" for c, _ in top_ch)

            # VS-forced chapter: ≥_VS_FORCE_MIN_HITS confident hits in same chapter
            # → bypass Claude step 1 entirely (self-improving: corrections feed VS)
            strong = [h for h in vs_hits if h.get("distance", 1.0) < _VS_FORCE_THRESHOLD]
            if len(strong) >= _VS_FORCE_MIN_HITS:
                top_ch_2, count = Counter(h["chapter"] for h in strong).most_common(1)[0]
                if count >= _VS_FORCE_MIN_HITS:
                    forced_chapters[ridx] = top_ch_2
                    logger.info(
                        f"  Chapter vs-forced: '{name[:45]}' → ch{top_ch_2} "
                        f"[{count} hits, dist<{_VS_FORCE_THRESHOLD}]"
                    )
        except Exception:
            pass

    # Build indexed list for chapter selection — only items not already forced
    indexed = [
        (ridx, d.get("name", ""),
         float(d.get("_value", 0) or 0),
         float(d.get("_weight", 0) or 0))
        for ridx, d in needs_chapter_selection
    ]

    # ── Pass 1: batch chapter selection (only for non-forced items) ──
    chapter_assignments: dict[int, str] = dict(forced_chapters)  # start with forced
    if indexed:
        try:
            claude_assignments = _batch_select_chapters(indexed, chapters, client, vector_hints)
            chapter_assignments.update(claude_assignments)
        except Exception as e:
            logger.warning(f"Batch chapter selection pao ({e}), fallback na per-item")
            _fallback_per_item(needs_claude, results, db)
            return

    # ── Pass 2: batch code selection per chapter ─────────────────────
    historical_map = {
        ridx: _find_historical(d["name"], db)
        for ridx, d in needs_claude
    }

    # Group by chapter
    by_chapter: dict[str, list] = {}
    for ridx, item_dict in needs_claude:
        ch = chapter_assignments.get(ridx, "")
        by_chapter.setdefault(ch, []).append((ridx, item_dict))

    for chapter, ch_items in by_chapter.items():
        chapter_codes = _get_chapter_codes(chapter, db)
        if not chapter_codes:
            logger.warning(f"Poglavlje {chapter} prazno, per-item fallback")
            _fallback_per_item(ch_items, results, db)
            continue

        indexed_ch = [
            (ridx, d.get("name", ""),
             float(d.get("_value", 0) or 0),
             float(d.get("_weight", 0) or 0))
            for ridx, d in ch_items
        ]

        try:
            code_results = _batch_select_codes(
                chapter, indexed_ch, chapter_codes, historical_map, client,
                shipment_context=shipment_context,
            )
        except Exception as e:
            logger.warning(f"Batch code-select ch{chapter} pao ({e}), per-item fallback")
            _fallback_per_item(ch_items, results, db, shipment_context=shipment_context)
            continue

        for ridx, item_dict in ch_items:
            cls = code_results.get(ridx, {"tariff_code": "", "confidence": "low"})
            updated = {k: v for k, v in item_dict.items() if not k.startswith("_")}
            updated["tariff_code"] = cls["tariff_code"]
            conf = cls["confidence"]
            updated["confidence"] = conf
            updated["tariff_source"] = f"ai_ch{chapter}"
            logger.info(f"  '{updated.get('name', '')[:45]}' → {cls['tariff_code']} [batch AI ch{chapter}, {conf}]")
            results[ridx] = _fill_official_desc_from_db(updated, db)


def _fallback_per_item(
    items_with_ridx: list[tuple],
    results: list,
    db: Session,
    shipment_context: list | None = None,
) -> None:
    """Per-item fallback for when batch fails."""
    for ridx, item_dict in items_with_ridx:
        name = item_dict.get("name", "")
        value = float(item_dict.get("_value", 0) or 0)
        weight = float(item_dict.get("_weight", 0) or 0)
        classification = classify_item(name, value, weight, db=db,
                                       shipment_context=shipment_context)
        updated = {k: v for k, v in item_dict.items() if not k.startswith("_")}
        updated["tariff_code"] = classification.get("tariff_code", "")
        conf = classification.get("confidence", "low")
        updated["confidence"] = conf
        updated["tariff_source"] = "ai_fallback"
        logger.info(f"  '{name[:45]}' → {updated['tariff_code'] or '???'} [per-item AI fallback, {conf}]")
        results[ridx] = _fill_official_desc_from_db(updated, db)


def _post_validate_low_confidence(results: list, db: Session) -> None:
    """
    For items classified with confidence=low: if VS top-1 hit has distance
    below _VS_POSTVAL_THRESHOLD, replace the AI result with the VS result.
    Elevates confidence to "medium" and logs every substitution.
    """
    all_valid = {_clean_code(r.code) for r in db.query(OfficialTariff).all()}
    for i, item in enumerate(results):
        if item is None or item.get("confidence") != "low":
            continue
        name = item.get("name", "")
        if not name:
            continue
        try:
            from tariff_vectorstore import search as vs_search
            hits = vs_search(name, chapter=None, n=5)
            if not hits:
                continue
            top = hits[0]
            dist = top.get("distance", 1.0)
            code = _clean_code(top.get("code", ""))
            if dist < _VS_POSTVAL_THRESHOLD and code in all_valid:
                old = item.get("tariff_code", "")
                results[i]["tariff_code"]   = code
                results[i]["confidence"]    = "medium"
                results[i]["tariff_source"] = "vs_postval"
                logger.info(
                    f"  Post-val: '{name[:45]}' {old}→{code} [vs dist={dist:.3f}]"
                )
        except Exception:
            pass


def classify_items_batch(items: list[dict], db: Session = None,
                          shipment_context: list | None = None,
                          skip_lookups: bool = False) -> list[dict]:
    """
    Classify list of item dicts. Skips items that already have tariff_code.
    shipment_context: all item names in shipment (auto-extracted from items if None).
    skip_lookups: bypass correction/keyword-map/auto-repeat DB shortcuts and force
        every item through the AI/rules path. Only for scripts/eval_classifier.py —
        without this, evaluating against ground truth already stored in those tables
        would trivially "succeed" via the lookup, not via actual classification.
    Returns updated list with tariff_code in 8-digit format.
    """
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    # Učitaj sve validne kodove jednom za validaciju
    all_official = db.query(OfficialTariff).all()
    valid_codes_in_db = {_clean_code(r.code) for r in all_official}
    def _clean_desc(raw: str) -> str:
        """Strip any leading junk headings like 'Montažne zgrade — ' that crept in during tariff import."""
        bad_prefixes = ["Montažne zgrade — ", "Montazne zgrade — "]
        for bp in bad_prefixes:
            if raw.startswith(bp):
                raw = raw[len(bp):]
        return raw

    # Mapa kod → opis za brzi lookup (with sanity-cleaned descriptions)
    official_desc_map: dict[str, str] = {
        _clean_code(r.code): _clean_desc(r.description_bs or "") for r in all_official
    }
    # Mapa kod → heading (4-cif. naslov bez crtica) — koristi se u XML Commercial_Description
    heading_bs_map: dict[str, str] = {
        _clean_code(r.code): (r.heading_bs or "") for r in all_official
    }

    def _find_by_hs6(hs6: str) -> str:
        """Nađi najbliži BiH kod po prvih 6 cifara (HS međunarodni standard)."""
        matches = [_clean_code(r.code) for r in all_official if _clean_code(r.code).startswith(hs6)]
        return matches[0] if matches else ""

    def _fill_official_desc(item_dict: dict) -> dict:
        """Popuni official_desc (leaf sa crticama) i heading_bs (4-cif naslov) iz tariff tabele."""
        code = _clean_code(item_dict.get("tariff_code", ""))
        if not item_dict.get("official_desc") and code:
            item_dict["official_desc"] = official_desc_map.get(code, "")
        if not item_dict.get("heading_bs") and code:
            item_dict["heading_bs"] = heading_bs_map.get(code, "")
        return item_dict

    # Auto-extract shipment context from items if not provided
    if shipment_context is None:
        shipment_context = [i.get("name", "") for i in items if i.get("name")]

    results = []
    needs_claude: list[tuple] = []  # (result_idx, item_dict) — for batch AI classification
    try:
        # Keyword → specific 9503 subcode mapping (ordered: more specific first)
        _TOY_SUBCODE_MAP = [
            # Plastelin/clay → chapter 34, NOT 95
            ({"plastelin", "plastilin", "plastelín", "clay", "modelling clay"}, "34070000"),
            # Puzzles
            ({"puzzle", "puzle", "slagalica", "mozaik"}, "95030061"),
            # Sets/collections of toys (policijski, vatrogasni, frizerski, doktorski...)
            ({"policijski set", "vatrogasni set", "frizerski set", "doktorski set",
              "policijsk", "vatrogasn", "frizersk", "doktorsk",
              "alat set", "set igr", "igracki set", "muzicki set", "kuhinjski set"}, "9503007000"),
            # Dolls — babies, baby dolls
            ({"beba na rola", "beba lutka", "lutka beba", "igr. beba", "igracka beba",
              "baby doll", "dojenče"}, "9503002100"),
            # Electric trains
            ({"voz baterij", "voz elektr", "vlak elektr", "electric train",
              "igr. voz", "igracka voz", "igr voz"}, "95030030"),
            # Battery/pedal ride-on (large ride-on cars)
            ({"auto na akumulator", "auto na aku", "auto na baterij", "motocikl na aku",
              "motor na aku", "ride on", "ride-on"}, "95030075"),
            # Push-along, prams, baby walkers
            ({"guralica", "prohodalica", "kolica za bebe", "kolica s bebom",
              "kolica sa bebom", "baby walker", "hodalica"}, "95030010"),
            # Inflatable ride-on toys (horse, etc.)
            ({"napuhljiv konj", "napuhljivi konj", "inflatable horse",
              "napuhljiv", "inflatable"}, "95030049"),
            # Tricikli, romobili, autići sa pedalama — bez motora
            ({"tricikl", "tricikli", "tricycle", "romobil", "romobili",
              "scooter za djecu", "auto na pedale"}, "95030095"),
        ]
        _DEFAULT_TOY_CODE = "95030095"  # other toys

        _TOY_KEYWORDS_BATCH = {
            "igračka", "igracka", "igr.", "igr ",
            "puzzle", "puzle", "slagalica",
            "plastelin", "plastilin", "plastelín",
            "hodalica", "prohodalica", "guralica",
            "lutka", "plišan",
            "toy", "toys", "doll", "dolls", "stuffed", "plush",
            "baby walker", "inflatable",
            "za djecu", "za bebe", "dječij",
            "tricikl", "tricikli", "tricycle", "romobil",
        }

        # Load all 9503 codes once
        _tc_all = db.query(OfficialTariff).filter(
            OfficialTariff.code.like("9503%")
        ).order_by(OfficialTariff.code).all()
        _valid_toy_codes = {_clean_code(c.code) for c in _tc_all}

        def _best_toy_code(name_lc: str) -> str:
            """Return the most specific 9503 subcode for this toy name."""
            for kw_set, code in _TOY_SUBCODE_MAP:
                if any(kw in name_lc for kw in kw_set):
                    if code in valid_codes_in_db:
                        return code
            fallback = _DEFAULT_TOY_CODE
            if fallback not in valid_codes_in_db:
                fallback = _clean_code(_tc_all[0].code) if _tc_all else ""
            return fallback

        for item in items:
            updated = dict(item)
            existing = _clean_code(updated.get("tariff_code", ""))
            name = updated.get("name", "").strip()
            name_lower = name.lower()

            # 1. Agent-confirmed correction — VISOKI PRIORITET (human-verified)
            # Mora biti PRIJE toy override — korekcija agenta je specifičnija od keyword guessa
            correction = None if skip_lookups else _find_correction(name, db)
            if correction and _clean_code(correction["tariff_code"]) in valid_codes_in_db:
                updated["tariff_code"] = _clean_code(correction["tariff_code"])
                if correction.get("official_desc") and not updated.get("official_desc"):
                    updated["official_desc"] = correction["official_desc"]
                updated["confidence"] = "high"
                updated["tariff_source"] = f"correction({correction['confirmations']}x)"
                logger.info(
                    f"  '{name[:45]}' → {updated['tariff_code']} "
                    f"[korekcija agenta, {correction['confirmations']}x potvrđeno, "
                    f"{correction['match_type']}]"
                )
                results.append(_fill_official_desc(updated))
                continue

            # 2. Keyword tariff map — historijski lookup iz 100+ deklaracija
            # Trigram → bigram → unigram match, min confidence 0.80.
            # Štedi AI pozive za stavke koje su se već klasificirale.
            ktm = None if skip_lookups else _find_keyword_tariff(name, db)
            if ktm and _clean_code(ktm["tariff_code"]) in valid_codes_in_db:
                updated["tariff_code"] = _clean_code(ktm["tariff_code"])
                if ktm.get("official_desc") and not updated.get("official_desc"):
                    updated["official_desc"] = ktm["official_desc"]
                updated["confidence"] = "high"
                updated["tariff_source"] = (
                    f"ktm_{ktm['keyword_type']}({ktm['frequency']}x,"
                    f"conf={ktm['confidence_score']:.0%})"
                )
                logger.info(
                    f"  '{name[:45]}' → {updated['tariff_code']} "
                    f"[keyword_map '{ktm['keyword']}' {ktm['keyword_type']}, "
                    f"freq={ktm['frequency']}, conf={ktm['confidence_score']:.0%}]"
                )
                results.append(_fill_official_desc(updated))
                continue

            # 2b. Auto-promovisan kod nakon 5+ konzistentnih ponavljanja (auto_learn.py).
            # Zaseban od koraka 1 (correction) — confirmations=0, source="auto_repeat(N)",
            # pa AI se ipak preskače ali zapis ostaje razlučiv od ljudske potvrde.
            auto_repeat = None if skip_lookups else _find_auto_repeat(name, db)
            if auto_repeat and _clean_code(auto_repeat["tariff_code"]) in valid_codes_in_db:
                updated["tariff_code"] = _clean_code(auto_repeat["tariff_code"])
                if auto_repeat.get("official_desc") and not updated.get("official_desc"):
                    updated["official_desc"] = auto_repeat["official_desc"]
                updated["confidence"] = "high"
                updated["tariff_source"] = auto_repeat["source_label"]
                logger.info(
                    f"  '{name[:45]}' → {updated['tariff_code']} "
                    f"[{auto_repeat['source_label']}, naučeno ponavljanjem]"
                )
                results.append(_fill_official_desc(updated))
                continue

            # 3. Toy / special-material override
            is_toy = any(kw in name_lower for kw in _TOY_KEYWORDS_BATCH)
            if is_toy:
                # If invoice already carries a valid 9503 code, trust it
                if existing.startswith("9503") and existing in valid_codes_in_db:
                    updated["tariff_code"] = existing
                    updated["confidence"] = "high"
                    updated["tariff_source"] = "invoice_toy"
                    logger.info(f"  '{name[:45]}' → {existing} [toy, kod iz fakture OK]")
                    results.append(_fill_official_desc(updated))
                    continue
                # Otherwise assign the most specific subcode
                toy_code = _best_toy_code(name_lower)
                updated["tariff_code"] = toy_code
                updated["confidence"] = "high"
                updated["tariff_source"] = "keyword_toy"
                logger.info(f"  '{name[:45]}' → {toy_code} [toy subcode override]")
                results.append(_fill_official_desc(updated))
                continue

            # Anti-halucinacija: vjeruj invoice kodu SAMO ako extraction step
            # eksplicitno potvrđuje da je kod stvarno ispisan na fakturi
            # (tariff_from_invoice=True). Bez toga AI zna izmisliti validan-format
            # ali pogrešan kod, pa zaobiđe novu klasifikaciju.
            invoice_verified = bool(item.get("tariff_from_invoice"))

            # 4. Tačan match u BiH tarifi iz fakture — koristi direktno
            if invoice_verified and len(existing) >= 8 and existing[:8] in valid_codes_in_db:
                updated["tariff_code"] = existing[:8]
                updated["confidence"] = "high"
                updated["tariff_source"] = "invoice_exact"
                logger.info(f"  '{name[:45]}' → {existing[:8]} [iz fakture, tačan, potvrđeno]")
                results.append(_fill_official_desc(updated))
                continue

            # 5. Kod s fakture postoji ali nije tačan u BiH — traži po prvih 6 cifara (HS standard)
            if invoice_verified and len(existing) >= 6:
                hs6 = existing[:6]
                matched = _find_by_hs6(hs6)
                if matched:
                    updated["tariff_code"] = matched
                    updated["confidence"] = "medium"
                    updated["tariff_source"] = "invoice_hs6"
                    logger.info(f"  '{name[:45]}' → {matched} [iz fakture, HS6 match na {hs6}]")
                    results.append(_fill_official_desc(updated))
                    continue
                else:
                    logger.warning(f"  Kod {existing} iz fakture — HS6 {hs6} nije u BiH tarifi, klasificiram")

            # Halucinirani kod (extraction postavio bez potvrde) — odbaci ga
            # i pošalji stavku na AI klasifikaciju.
            if existing and not invoice_verified:
                logger.info(f"  '{name[:45]}' → kod {existing} odbačen (tariff_from_invoice=false), idemo na AI")
                updated["tariff_code"] = ""
                existing = ""

            # 6. Nema koda ili nema HS6 matcha → spremi za batch klasifikaciju
            if not name:
                results.append(updated)
                continue

            updated["_value"] = float(updated.get("value", 0) or 0)
            updated["_weight"] = float(updated.get("net_weight") or updated.get("gross_weight") or 0)
            results.append(None)  # placeholder
            needs_claude.append((len(results) - 1, updated))

        # ── BATCH CLAUDE KLASIFIKACIJA (sve stavke bez koda u jednom pozivu) ──
        if needs_claude:
            logger.info(f"Batch AI klasifikacija: {len(needs_claude)} stavki")
            _classify_batch_and_fill(needs_claude, results, db,
                                     shipment_context=shipment_context)
            # Post-validate remaining low-confidence results with VS top-1
            _post_validate_low_confidence(results, db)

        return [r if r is not None else {} for r in results]
    finally:
        if close_db:
            db.close()
