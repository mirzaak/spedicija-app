from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
import json
import logging
import os

logger = logging.getLogger("database")

# ── Per-model pricing (USD per million tokens) ────────────────────────────
_MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {
        "input":       3.00,
        "output":     15.00,
        "cache_read":  0.30,
        "cache_write": 3.75,
    },
    "claude-haiku-4-5-20251001": {
        "input":       1.00,
        "output":      5.00,
        "cache_read":  0.10,
        "cache_write": 1.25,
    },
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "..", "data", "spedicija.db")

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class TariffRecord(Base):
    __tablename__ = "tariff_records"

    id = Column(Integer, primary_key=True, index=True)
    description = Column(String, index=True)       # Commercial description (lowercase)
    tariff_code = Column(String(10), index=True)   # e.g. 94018000
    official_desc = Column(String)                  # Official goods description
    country_origin = Column(String(2))             # CN, US, DE...
    unit_value = Column(Float, nullable=True)      # value per kg or unit (signal)
    source_file = Column(String)                   # which XML it came from
    created_at = Column(DateTime, default=datetime.utcnow)


class OfficialTariff(Base):
    """BiH official customs tariff (Carinska tarifa 2026)."""
    __tablename__ = "official_tariffs"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(10), index=True, unique=True)  # e.g. 94018000
    description_bs = Column(Text)                        # leaf text WITH hierarchy dashes, e.g. "- - ostalo"
    heading_bs = Column(Text, nullable=True)             # full 4-digit heading text (no dashes)
    description_en = Column(Text, nullable=True)         # English if available
    unit = Column(String(20), nullable=True)             # kg, pce, etc.
    duty_rate = Column(String(20), nullable=True)        # e.g. "5%" or "0"
    chapter = Column(String(4), nullable=True)           # first 2 digits


class Declaration(Base):
    __tablename__ = "declarations"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String)
    consignee = Column(String)
    declaration_date = Column(String)
    currency = Column(String(3))
    total_items = Column(Integer)
    xml_content = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class PendingShipment(Base):
    """Emails fetched from Gmail, waiting for user approval before processing.

    Status lifecycle (legacy "approved" jednim potezom + novi semi-auto tok):
      pending     — Gmail uvezao, čeka ekstrakciju
      extracted   — A+B (extract+classify) gotovo, čeka agentov pregled (NOVI)
      approved    — legacy: stari /approve endpoint odradio cijeli pipeline u jednom potezu
      uploaded    — agent potvrdio "Za pregled", Drive sync gotov (NOVI)
      skipped     — agent odbio
    """
    __tablename__ = "pending_shipments"

    id = Column(Integer, primary_key=True, index=True)
    gmail_message_id = Column(String, unique=True, index=True)
    email_subject = Column(String)
    sender = Column(String)
    received_at = Column(String)       # "20260406_143022"
    files_json = Column(Text)          # JSON: [{filepath, filename}, ...]
    status = Column(String(20), default="pending")
    attachment_hash = Column(String(64), nullable=True, index=True)  # SHA256 of all attachment bytes
    created_at = Column(DateTime, default=datetime.utcnow)

    # ── Semi-auto pipeline polja (NULL za sve legacy "approved" redove) ────
    extracted_items_json     = Column(Text, nullable=True)   # JSON: rezultat _analyze_shipment_with_claude.items
    extracted_metadata_json  = Column(Text, nullable=True)   # JSON: ostala polja (consignee, exporter, totals)
    declaration_xml          = Column(Text, nullable=True)   # snapshot generisanog XML-a (nakon finalize)
    drive_folder             = Column(String, nullable=True) # naziv foldera na Drive-u
    drive_url                = Column(String, nullable=True) # share link na Drive folder
    extracted_at             = Column(DateTime, nullable=True)
    finalized_at             = Column(DateTime, nullable=True)
    # Trajno neaktivno — nigdje se ne piše niti čita. Full-auto (run_pipeline)
    # je danas eksplicitni default za SVE pošiljke, nema više "eligible vs not"
    # grananja koje bi ovo polje trebalo podržavati. Rezervisano za eventualni
    # budući whitelist filter; ne referencirati u novom kodu bez novog razloga.
    auto_extract_eligible    = Column(Integer, default=0)
    extract_error            = Column(Text, nullable=True)   # poruka ako auto-extract pao


class TariffCorrection(Base):
    """
    Agent-confirmed tariff corrections. These take priority over AI in classifier.
    Every time an agent edits a tariff code and clicks "Potvrdi", it saves here.
    Over time this becomes the primary knowledge source for tariff assignment.
    """
    __tablename__ = "tariff_corrections"

    id = Column(Integer, primary_key=True, index=True)
    # Normalised item name (uppercase, stripped) — used for lookup
    item_name_normalized = Column(String, index=True)
    # Original name as entered (for display)
    item_name_original = Column(String)
    # The confirmed correct tariff code (8 digits)
    tariff_code = Column(String(10), index=True)
    # Official description from BiH tariff DB
    official_desc = Column(Text, nullable=True)
    # Country of origin at time of correction
    country_origin = Column(String(2), nullable=True)
    # How many times this correction has been confirmed (reinforcement)
    confirmations = Column(Integer, default=1)
    # Source: 'manual' (agent typed), 'selection' (agent picked from suggestions)
    source = Column(String(20), default="manual")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AppSettings(Base):
    """
    Key-value store for app configuration.
    Keys: declarant_code, declarant_name, declarant_ref, office_code, office_name,
          gmail_filter, notify_email
    """
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, index=True, nullable=False)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DetectionLog(Base):
    """Audit log for every email detection decision (accepted/rejected/duplicate/no_files)."""
    __tablename__ = "detection_log"

    id               = Column(Integer, primary_key=True, index=True)
    ts               = Column(DateTime, default=datetime.utcnow)
    gmail_message_id = Column(String, index=True)
    subject          = Column(String)
    sender           = Column(String)
    attachment_names = Column(Text)      # JSON array of filenames
    decision         = Column(String)    # accepted | rejected | duplicate | no_files
    reason           = Column(String)
    stage            = Column(String)    # rule | llm


class ApiUsage(Base):
    """Per-call Claude API usage log for cost tracking."""
    __tablename__ = "api_usage"

    id          = Column(Integer, primary_key=True, index=True)
    ts          = Column(DateTime, default=datetime.utcnow)
    model       = Column(String, index=True)
    context     = Column(String)          # "extract", "classify", "chat_assist", "chat_declaration"
    input_tokens  = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cache_read    = Column(Integer, default=0)
    cache_write   = Column(Integer, default=0)
    cost_est      = Column(Float,   default=0.0)


def log_api_usage(db, model: str, context: str, usage) -> None:
    """
    Persist one Claude API call to api_usage.
    usage: anthropic message.usage object (has input_tokens, output_tokens, and optionally
           cache_read_input_tokens / cache_creation_input_tokens).
    Uses its own session if db is None; always commits independently so that a failure
    in the main pipeline does not prevent usage being recorded.
    """
    try:
        inp   = getattr(usage, "input_tokens", 0) or 0
        out   = getattr(usage, "output_tokens", 0) or 0
        c_r   = getattr(usage, "cache_read_input_tokens", 0) or 0
        c_w   = getattr(usage, "cache_creation_input_tokens", 0) or 0

        pricing = _MODEL_PRICING.get(model, _MODEL_PRICING["claude-sonnet-4-6"])
        cost = (
            inp   * pricing["input"]       / 1_000_000
            + out   * pricing["output"]      / 1_000_000
            + c_r   * pricing["cache_read"]  / 1_000_000
            + c_w   * pricing["cache_write"] / 1_000_000
        )

        own_session = db is None
        if own_session:
            db = SessionLocal()
        try:
            db.add(ApiUsage(
                model=model,
                context=context,
                input_tokens=inp,
                output_tokens=out,
                cache_read=c_r,
                cache_write=c_w,
                cost_est=round(cost, 8),
            ))
            db.commit()
        finally:
            if own_session:
                db.close()
    except Exception as e:
        logger.warning(f"log_api_usage failed (non-critical): {e}")


def log_detection(db, gmail_message_id: str, subject: str, sender: str,
                  attachment_names: list, decision: str, reason: str, stage: str) -> None:
    """
    Persist one email detection decision to detection_log.
    attachment_names is a list — stored as JSON string.
    Uses its own session if db is None; always commits independently.
    """
    try:
        own_session = db is None
        if own_session:
            db = SessionLocal()
        try:
            db.add(DetectionLog(
                gmail_message_id=gmail_message_id,
                subject=subject,
                sender=sender,
                attachment_names=json.dumps(attachment_names),
                decision=decision,
                reason=reason,
                stage=stage,
            ))
            db.commit()
        finally:
            if own_session:
                db.close()
    except Exception as e:
        logger.warning(f"log_detection failed (non-critical): {e}")


def _migrate():
    """Apply schema migrations that create_all() cannot handle (ALTER TABLE ADD COLUMN)."""
    from sqlalchemy import text
    migrations = [
        # v2: duplicate-detection hash on pending_shipments
        "ALTER TABLE pending_shipments ADD COLUMN attachment_hash TEXT",
        # v3: settings table already handled by create_all, but safe to list
        # v4: heading_bs on official_tariffs (4-digit heading text without dashes)
        "ALTER TABLE official_tariffs ADD COLUMN heading_bs TEXT",
        # v5: semi-auto Gmail pipeline polja (extract → review → finalize tok)
        "ALTER TABLE pending_shipments ADD COLUMN extracted_items_json TEXT",
        "ALTER TABLE pending_shipments ADD COLUMN extracted_metadata_json TEXT",
        "ALTER TABLE pending_shipments ADD COLUMN declaration_xml TEXT",
        "ALTER TABLE pending_shipments ADD COLUMN drive_folder TEXT",
        "ALTER TABLE pending_shipments ADD COLUMN drive_url TEXT",
        "ALTER TABLE pending_shipments ADD COLUMN extracted_at TIMESTAMP",
        "ALTER TABLE pending_shipments ADD COLUMN finalized_at TIMESTAMP",
        "ALTER TABLE pending_shipments ADD COLUMN auto_extract_eligible INTEGER DEFAULT 0",
        "ALTER TABLE pending_shipments ADD COLUMN extract_error TEXT",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                # Column already exists — ignore
                pass


def get_setting(db, key: str, default: str = "") -> str:
    row = db.query(AppSettings).filter_by(key=key).first()
    return row.value if row and row.value is not None else default


def set_setting(db, key: str, value: str):
    row = db.query(AppSettings).filter_by(key=key).first()
    if row:
        row.value = value
        row.updated_at = datetime.utcnow()
    else:
        db.add(AppSettings(key=key, value=value))
    db.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine, checkfirst=True)
    # Run lightweight migrations for columns added after initial schema creation.
    # SQLAlchemy create_all() does not ALTER existing tables.
    _migrate()
