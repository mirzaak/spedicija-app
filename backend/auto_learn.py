"""
Auto-promocija tarifnih kodova nakon ponovljene, konzistentne pojave u
historijskim deklaracijama (tariff_records), plus periodični trigger koji
ovo (i postojeći keyword_tariff_map rebuild) pokreće bez ručne intervencije.

Princip (isti kao KG hard-evidence gating u tariff_graph.py): samo TVRD dokaz
promoviše — isti normalizovan naziv (EXACT, ne fuzzy/n-gram), isti tarifni kod
u SVIM pojavama, iz >= MIN_DISTINCT_SOURCES odvojenih deklaracija. Bilo koja
kontradikcija (2+ različita koda za isti naziv) trajno diskvalifikuje grupu iz
auto-promocije — ostaje na AI/čovjeku, nikad se ne "riješi" glasanjem većine.

Auto-promovisani red se upisuje sa confirmations=0 (NE 1) — _find_correction()
u claude_classifier.py namjerno filtrira confirmations>=1 kao "ljudski
potvrđeno" (vidi komentar tamo). Da auto-promocija piše confirmations=1,
postala bi nerazlučiva od ljudske korekcije baš na mjestu koje određuje
ponašanje sistema. Umjesto toga, klasifikator ima ZASEBAN korak koji traži
source LIKE 'auto_repeat%' — AI se preskače, ali zapis ostaje vidljivo odvojen.
"""
import logging
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from database import SessionLocal, TariffRecord, TariffCorrection, OfficialTariff, get_setting, set_setting
from tariff_learning_utils import feed_learning_stores

logger = logging.getLogger("auto_learn")

MIN_DISTINCT_SOURCES = 3
_SOURCE_PREFIX = "auto_repeat"

ROOT = Path(__file__).parent.parent
_SCRIPTS_DIR = str(ROOT / "scripts")


def find_and_promote_repeated_items(db=None) -> dict:
    """
    B1-B3: grupiši tariff_records po tačnom normalizovanom nazivu, promoviši
    grupe koje zadovoljavaju prag u TariffCorrection (confirmations=0,
    source="auto_repeat(N)"). Idempotentno — update umjesto insert.
    """
    from claude_classifier import _normalize_repeat_name, _clean_code

    own = db is None
    db = db or SessionLocal()
    stats = {"promoted": 0, "updated": 0, "skipped_human": 0, "skipped_contradiction": 0}
    try:
        rows = db.query(
            TariffRecord.description, TariffRecord.tariff_code, TariffRecord.source_file
        ).all()

        groups: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        for desc, code, src in rows:
            if not desc or not code or not src:
                continue
            norm = _normalize_repeat_name(desc)
            clean = _clean_code(code)
            if norm and clean:
                groups[norm].append((clean, src, desc))

        for norm, entries in groups.items():
            distinct_sources = {src for _, src, _ in entries}
            distinct_codes = {code for code, _, _ in entries}

            if len(distinct_sources) < MIN_DISTINCT_SOURCES:
                continue
            if len(distinct_codes) != 1:
                # Kontradikcija — nikad ne "riješi" glasanjem, ostaje na AI/čovjeku.
                stats["skipped_contradiction"] += 1
                continue

            code = next(iter(distinct_codes))
            original_name = entries[0][2]

            # Ljudska korekcija uvijek pobjeđuje — ako postoji sa DRUGAČIJIM kodom,
            # ne promovišemo (ne prepisujemo eksplicitnu ljudsku odluku).
            human = (
                db.query(TariffCorrection)
                .filter(TariffCorrection.item_name_normalized == norm)
                .filter(TariffCorrection.source.in_(("manual", "selection")))
                .first()
            )
            if human and human.tariff_code != code:
                stats["skipped_human"] += 1
                continue
            if human and human.tariff_code == code:
                # Već pokriveno ljudskom potvrdom (confirmations>=1) — auto red nepotreban.
                continue

            source_label = f"{_SOURCE_PREFIX}({len(distinct_sources)})"
            existing_auto = (
                db.query(TariffCorrection)
                .filter(TariffCorrection.item_name_normalized == norm)
                .filter(TariffCorrection.source.like(f"{_SOURCE_PREFIX}%"))
                .first()
            )

            if existing_auto:
                if existing_auto.source != source_label or existing_auto.tariff_code != code:
                    existing_auto.source = source_label
                    existing_auto.tariff_code = code
                    existing_auto.updated_at = datetime.utcnow()
                    db.commit()
                    stats["updated"] += 1
                    feed_learning_stores(original_name, code, existing_auto.official_desc or "")
                continue

            ot = db.query(OfficialTariff).filter(OfficialTariff.code.like(f"{code}%")).first()
            official_desc = ot.description_bs if ot else ""

            corr = TariffCorrection(
                item_name_normalized=norm,
                item_name_original=original_name.strip(),
                tariff_code=code,
                official_desc=official_desc,
                confirmations=0,
                source=source_label,
            )
            db.add(corr)
            db.commit()
            stats["promoted"] += 1
            feed_learning_stores(original_name, code, official_desc)

        logger.info(f"Auto-promocija ponavljanja: {stats}")
        return stats
    finally:
        if own:
            db.close()


def _rebuild_ktm() -> None:
    """
    C1: scripts/extract_keyword_tariffs.py koristi sirovi sqlite3, nema import
    iz backend/ — nema rizika od cirkularnog uvoza, samo dodaj scripts/ na sys.path.
    """
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    from extract_keyword_tariffs import run as rebuild_ktm
    rebuild_ktm(reset=True)


def run_auto_learn_if_due(db=None, min_interval_minutes: int = 30) -> dict:
    """
    C2: throttled trigger — čita AppSettings.last_auto_learn_run, jeftin no-op
    ako je prošlo manje od min_interval_minutes (izbjegava skeniranje cijele
    tariff_records tabele nakon svake pošiljke, uključujući burst dolazak
    više emailova odjednom).
    """
    own = db is None
    db = db or SessionLocal()
    try:
        last = get_setting(db, "last_auto_learn_run", "")
        if last:
            try:
                elapsed = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds()
                if elapsed < min_interval_minutes * 60:
                    return {"skipped": True, "reason": "debounce"}
            except ValueError:
                pass  # neispravan format u settings — tretiraj kao "nikad pokrenuto"

        promo_stats = find_and_promote_repeated_items(db)
        try:
            _rebuild_ktm()
            ktm_ok = True
        except Exception as e:
            # sqlite3 direktna konekcija paralelno sa SQLAlchemy engine-om na istom
            # fajlu — non-critical, nikad ne smije oboriti pozivaoca (fire-and-forget).
            logger.warning(f"KTM rebuild neuspješan (nekritično): {e}")
            ktm_ok = False

        set_setting(db, "last_auto_learn_run", datetime.utcnow().isoformat())
        db.commit()
        return {"skipped": False, "promotion": promo_stats, "ktm_rebuilt": ktm_ok}
    finally:
        if own:
            db.close()
