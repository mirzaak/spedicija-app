"""Edge-case tests for claude_classifier.py tariff classification.

Focuses on deterministic logic (code normalization, correction/keyword lookup,
toy-subcode override, invoice-code trust rules) that runs BEFORE any Claude
API call, plus a couple of AI-path edge cases against a fake Anthropic client.
No real network/API calls are made in this file.
"""
import json
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from database import Base, OfficialTariff, TariffCorrection
import claude_classifier as cc


# ── fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def db():
    engine = sa.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("""
            CREATE TABLE keyword_tariff_map (
                id INTEGER PRIMARY KEY,
                keyword TEXT NOT NULL,
                keyword_type TEXT NOT NULL,
                dominant_tariff_code TEXT NOT NULL,
                official_desc TEXT,
                frequency INTEGER DEFAULT 1,
                confidence_score REAL DEFAULT 0.0,
                tariff_distribution TEXT,
                last_seen TIMESTAMP,
                UNIQUE(keyword, keyword_type)
            )
        """)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _add_tariff(db, code, desc="opis", heading="naslov", chapter=None):
    db.add(OfficialTariff(code=code, description_bs=desc, heading_bs=heading,
                           chapter=chapter or code[:2]))
    db.commit()


def _add_ktm(db, keyword, keyword_type, code, confidence=0.9, freq=5, desc=""):
    db.execute(sa.text(
        "INSERT INTO keyword_tariff_map "
        "(keyword, keyword_type, dominant_tariff_code, official_desc, frequency, confidence_score) "
        "VALUES (:kw, :kt, :code, :desc, :freq, :conf)"
    ), {"kw": keyword, "kt": keyword_type, "code": code, "desc": desc,
        "freq": freq, "conf": confidence})
    db.commit()


class FakeMessage:
    def __init__(self, text):
        self.content = [SimpleNamespace(type="text", text=text)]
        self.usage = SimpleNamespace(input_tokens=1, output_tokens=1,
                                      cache_creation_input_tokens=0,
                                      cache_read_input_tokens=0)


class FakeClient:
    """Replays canned responses in order; raises StopIteration if exhausted."""
    def __init__(self, texts):
        self._it = iter(texts)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        nxt = next(self._it)
        if isinstance(nxt, Exception):
            raise nxt
        return FakeMessage(nxt)


# ── _clean_code ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("8544.42.90", "85444290"),
    ("8544 42 90", "85444290"),
    ("85444290", "85444290"),
    ("854442901234", "85444290"),   # truncated to 8
    ("", ""),
    ("abcd", ""),
    ("ab84cd47", "8447"),
    (None, ""),
])
def test_clean_code(raw, expected):
    assert cc._clean_code(raw) == expected


# ── _normalize_name ─────────────────────────────────────────────────────────

def test_normalize_name_collapses_whitespace_and_uppercases():
    assert cc._normalize_name("  vijak   m6  ") == "VIJAK M6"


def test_normalize_name_empty_string():
    assert cc._normalize_name("") == ""


# ── _normalize_for_ktm ───────────────────────────────────────────────────────

def test_normalize_for_ktm_strips_units_and_numbers():
    # Leading-digit tokens with a unit suffix (10kg) and bare counts (5) are
    # stripped; a letter-prefixed token like "M6" is NOT (regex requires the
    # word to start with a digit), so it survives normalization.
    words = cc._normalize_for_ktm("Vijak M6 10kg 5 komada")
    assert "10KG" not in words
    assert "5" not in words
    assert "VIJAK" in words
    assert "M6" in words


def test_normalize_for_ktm_drops_stop_words_and_short_tokens():
    words = cc._normalize_for_ktm("ostalo za sa a kom")
    assert words == []


def test_normalize_for_ktm_empty_string():
    assert cc._normalize_for_ktm("") == []


def test_normalize_for_ktm_punctuation_only():
    assert cc._normalize_for_ktm("!!! ,, ...") == []


# ── _find_correction ─────────────────────────────────────────────────────────

def test_find_correction_ignores_unconfirmed_zero_confirmations(db):
    db.add(TariffCorrection(item_name_normalized="VIJAK M6", item_name_original="Vijak M6",
                             tariff_code="73181500", confirmations=0, source="ai_batch"))
    db.commit()
    assert cc._find_correction("Vijak M6", db) is None


def test_find_correction_exact_match_confirmed(db):
    db.add(TariffCorrection(item_name_normalized="VIJAK M6", item_name_original="Vijak M6",
                             tariff_code="73181500", confirmations=2, source="manual"))
    db.commit()
    result = cc._find_correction("vijak m6", db)
    assert result["tariff_code"] == "73181500"
    assert result["match_type"] == "exact"


def test_find_correction_word_overlap_requires_two_words(db):
    db.add(TariffCorrection(item_name_normalized="CRVENA KOZNA JAKNA", item_name_original="Crvena kozna jakna",
                             tariff_code="42032100", confirmations=3, source="manual"))
    db.commit()
    # Single-word query never triggers word-overlap path
    assert cc._find_correction("Jakna", db) is None


def test_find_correction_word_overlap_below_threshold_returns_none(db):
    db.add(TariffCorrection(item_name_normalized="CRVENA KOZNA JAKNA", item_name_original="Crvena kozna jakna",
                             tariff_code="42032100", confirmations=3, source="manual"))
    db.commit()
    # Only 1 of 3 words overlaps → score 1/3 < 0.75 threshold
    assert cc._find_correction("Crvena vunena kapa", db) is None


def test_find_correction_word_overlap_above_threshold_matches(db):
    db.add(TariffCorrection(item_name_normalized="CRVENA KOZNA JAKNA MUSKA", item_name_original="Crvena kozna jakna muska",
                             tariff_code="42032100", confirmations=3, source="manual"))
    db.commit()
    result = cc._find_correction("Crvena kozna jakna", db)
    assert result is not None
    assert result["tariff_code"] == "42032100"
    assert result["match_type"].startswith("word_overlap")


def test_find_correction_empty_name(db):
    assert cc._find_correction("", db) is None


# ── _find_keyword_tariff ──────────────────────────────────────────────────────

def test_find_keyword_tariff_prefers_trigram_over_unigram(db):
    # Stored keywords are matched against UPPERCASE normalized candidates —
    # _normalize_for_ktm() upper-cases everything before building n-grams.
    _add_ktm(db, "CRNA KOZNA JAKNA", "trigram", "42032100", confidence=0.95)
    _add_ktm(db, "JAKNA", "unigram", "61012000", confidence=0.85)
    result = cc._find_keyword_tariff("Crna kozna jakna", db)
    assert result["tariff_code"] == "42032100"
    assert result["keyword_type"] == "trigram"


def test_find_keyword_tariff_below_confidence_threshold_excluded(db):
    _add_ktm(db, "JAKNA", "unigram", "61012000", confidence=0.5)  # below _KTM_MIN_CONF
    assert cc._find_keyword_tariff("jakna", db) is None


def test_find_keyword_tariff_blacklisted_word_bypasses_lookup(db):
    _add_ktm(db, "DASKA", "unigram", "39241000", confidence=0.95)
    # "daska" is in _KTM_BLACKLIST — must not use the (historically wrong) mapping
    assert cc._find_keyword_tariff("Daska za sklekove", db) is None


def test_find_keyword_tariff_no_words_after_normalization(db):
    assert cc._find_keyword_tariff("kom kg pak", db) is None


def test_find_keyword_tariff_no_match_returns_none(db):
    _add_ktm(db, "CIZME", "unigram", "64039900", confidence=0.9)
    assert cc._find_keyword_tariff("Sesir za sunce", db) is None


# ── _find_auto_repeat ─────────────────────────────────────────────────────────

def test_find_auto_repeat_matches_source_prefix(db):
    db.add(TariffCorrection(item_name_normalized="LED TRAKA", item_name_original="LED traka 5m",
                             tariff_code="94054000", confirmations=0, source="auto_repeat(6)"))
    db.commit()
    result = cc._find_auto_repeat("led traka 5m", db)
    assert result["tariff_code"] == "94054000"
    assert result["source_label"] == "auto_repeat(6)"


def test_find_auto_repeat_does_not_match_manual_source(db):
    db.add(TariffCorrection(item_name_normalized="LED TRAKA 5M", item_name_original="LED traka 5m",
                             tariff_code="94054000", confirmations=1, source="manual"))
    db.commit()
    assert cc._find_auto_repeat("led traka 5m", db) is None


# ── classify_items_batch: deterministic override paths (no AI call) ──────────

def test_batch_correction_takes_priority_over_toy_keyword(db, monkeypatch):
    # Item name matches both a confirmed correction AND a toy keyword ("igracka").
    # Correction must win — it's human-verified, checked before the toy override.
    _add_tariff(db, "39241000")
    db.add(TariffCorrection(item_name_normalized="DJECIJA IGRACKA AUTO", item_name_original="Djecija igracka auto",
                             tariff_code="39241000", confirmations=2, source="manual"))
    db.commit()

    def _boom(*a, **k):
        raise AssertionError("Claude should not be called")
    monkeypatch.setattr(cc, "_get_client", _boom)

    result = cc.classify_items_batch(
        [{"name": "Djecija igracka auto", "value": 5, "net_weight": 0.1}], db=db
    )
    assert result[0]["tariff_code"] == "39241000"
    assert result[0]["tariff_source"].startswith("correction")


def test_batch_toy_plastelin_exception_routes_to_chapter_34(db, monkeypatch):
    _add_tariff(db, "3407000000", chapter="34")
    monkeypatch.setattr(cc, "_get_client", lambda: (_ for _ in ()).throw(AssertionError("no AI")))
    result = cc.classify_items_batch(
        [{"name": "Djecija plastelin kutija", "value": 3}], db=db
    )
    assert result[0]["tariff_code"] == "34070000"
    assert result[0]["tariff_source"] == "keyword_toy"


def test_batch_toy_defaults_to_other_toys_when_no_subcode_matches(db, monkeypatch):
    _add_tariff(db, "9503007000", chapter="95")
    _add_tariff(db, "9503009500", chapter="95")
    monkeypatch.setattr(cc, "_get_client", lambda: (_ for _ in ()).throw(AssertionError("no AI")))
    result = cc.classify_items_batch(
        [{"name": "Igracka nepoznatog tipa", "value": 3}], db=db
    )
    assert result[0]["tariff_code"] == "95030095"


def test_batch_toy_trusts_existing_9503_code_from_invoice(db, monkeypatch):
    _add_tariff(db, "9503006100", chapter="95")
    monkeypatch.setattr(cc, "_get_client", lambda: (_ for _ in ()).throw(AssertionError("no AI")))
    result = cc.classify_items_batch(
        [{"name": "Igracka puzzle", "tariff_code": "95030061", "value": 3}], db=db
    )
    assert result[0]["tariff_code"] == "95030061"
    assert result[0]["tariff_source"] == "invoice_toy"


def test_batch_invoice_exact_code_trusted_when_verified(db, monkeypatch):
    _add_tariff(db, "85444290")
    monkeypatch.setattr(cc, "_get_client", lambda: (_ for _ in ()).throw(AssertionError("no AI")))
    result = cc.classify_items_batch(
        [{"name": "Kabl", "tariff_code": "85444290", "tariff_from_invoice": True, "value": 3}],
        db=db,
    )
    assert result[0]["tariff_code"] == "85444290"
    assert result[0]["tariff_source"] == "invoice_exact"


def test_batch_invoice_code_wrong_but_hs6_matches(db, monkeypatch):
    # Invoice code's first 6 digits match a BiH code, but the full 8 digits don't exist
    _add_tariff(db, "85444299")
    monkeypatch.setattr(cc, "_get_client", lambda: (_ for _ in ()).throw(AssertionError("no AI")))
    result = cc.classify_items_batch(
        [{"name": "Kabl", "tariff_code": "85444201", "tariff_from_invoice": True, "value": 3}],
        db=db,
    )
    assert result[0]["tariff_code"] == "85444299"
    assert result[0]["tariff_source"] == "invoice_hs6"
    assert result[0]["confidence"] == "medium"


def test_batch_hallucinated_invoice_code_discarded_without_verification(db, monkeypatch):
    # tariff_from_invoice is False/missing → code must be discarded even though it
    # looks like a valid-format code; item then has no name so it can't be routed
    # anywhere and must NOT silently keep the hallucinated code.
    monkeypatch.setattr(cc, "_get_client", lambda: (_ for _ in ()).throw(AssertionError("no AI")))
    result = cc.classify_items_batch(
        [{"name": "", "tariff_code": "12345678", "value": 3}], db=db
    )
    assert result[0].get("tariff_code", "") != "12345678"


def test_batch_empty_items_list(db):
    assert cc.classify_items_batch([], db=db) == []


# ── AI-path edge cases (fake Claude client, deterministic canned replies) ────

def test_batch_ai_path_rejects_out_of_chapter_code_falls_back_low_confidence(db, monkeypatch):
    _add_tariff(db, "84XXXXXX".replace("X", "0"), chapter="84")  # 84000000
    _add_tariff(db, "84501000", chapter="84")
    fake = FakeClient([
        json.dumps([{"n": 1, "chapter": "84"}]),                       # step1: chapter select
        json.dumps([{"n": 1, "tariff_code": "99999999",                # step2: code NOT in chapter
                      "confidence": "high", "reason": "x"}]),
    ])
    monkeypatch.setattr(cc, "_get_client", lambda: fake)
    monkeypatch.setattr(cc, "_vs_code_in_chapter", lambda name, ch, valid: "84000000")
    result = cc.classify_items_batch(
        [{"name": "Neki nepoznat uredjaj", "value": 10, "net_weight": 1}], db=db
    )
    assert result[0]["confidence"] == "low"
    assert result[0]["tariff_code"] in {"84000000", "84501000"}


def test_batch_ai_path_parses_markdown_fenced_json(db, monkeypatch):
    _add_tariff(db, "85444290", chapter="85")
    fake = FakeClient([
        "```json\n" + json.dumps([{"n": 1, "chapter": "85"}]) + "\n```",
        "```json\n" + json.dumps([{"n": 1, "tariff_code": "85444290",
                                    "confidence": "high", "reason": "x"}]) + "\n```",
    ])
    monkeypatch.setattr(cc, "_get_client", lambda: fake)
    result = cc.classify_items_batch(
        [{"name": "Nepoznat kabl", "value": 10}], db=db
    )
    assert result[0]["tariff_code"] == "85444290"
    assert result[0]["confidence"] == "high"


def test_batch_chapter_selection_failure_falls_back_per_item(db, monkeypatch):
    _add_tariff(db, "85444290", chapter="85")

    call_count = {"n": 0}

    def _create(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError("boom")
        # per-item fallback: classify_item does step1 then step2
        if call_count["n"] == 2:
            return FakeMessage(json.dumps({"chapter": "85"}))
        return FakeMessage(json.dumps({"tariff_code": "85444290",
                                        "confidence": "medium", "reason": "x"}))

    fake = SimpleNamespace(messages=SimpleNamespace(create=_create))
    monkeypatch.setattr(cc, "_get_client", lambda: fake)
    result = cc.classify_items_batch(
        [{"name": "Sasvim nepoznata stavka", "value": 10}], db=db
    )
    assert result[0]["tariff_code"] == "85444290"
    assert result[0]["tariff_source"] == "ai_fallback"
