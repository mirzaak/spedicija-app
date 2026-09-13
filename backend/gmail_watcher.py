"""
Gmail watcher: polls for new emails with PDF/Excel attachments.
Saves them as pending shipments (DB) for user review before processing.
Labels:
  Spedicija-Pending   — fetched, waiting for user action
  Spedicija-Processed — processed or skipped
  Spedicija-Ignored   — detected as non-declaration, auto-skipped
"""
import base64
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from datetime import datetime

from googleapiclient.discovery import build
from google_auth import get_credentials
from database import PendingShipment, SessionLocal, init_db, log_detection

logger = logging.getLogger("gmail_watcher")

UPLOADS_DIR = Path(__file__).parent.parent / "data" / "uploads"
PENDING_LABEL   = "Spedicija-Pending"
PROCESSED_LABEL = "Spedicija-Processed"

IGNORED_LABEL = "Spedicija-Ignored"

# Keywords that CONFIRM it is a shipment/declaration email (subject or filenames)
_SUBJECT_ACCEPT = [
    # Jaki špedicijski signali — svaki od ovih samostalno je dovoljno
    "packing list", "bill of lading", "b/l", "cmr", "tovarni list",
    "commercial invoice", "avizo", "awb",
    "deklaracija", "carina", "customs",
    "uvoz", "import", "shipment", "pošiljka", "posiljka",
    "container", "kontejner", "lcl", "fcl",
    "špedicija", "spedicija",
    # Slabiji signali — potrebno 2+ za prihvatanje
    "invoice", "faktura", "packing", "proforma", "pro forma",
    "fwd", "fw:", "najava",
    "loading", "utovar", "isporuka",
    "freight", "prevoz",
    "xlsx", "xls",
]

# Jaki pojedinačni signali — email se odmah prihvata bez čekanja na 2+ match-a
_STRONG_ACCEPT = {
    "packing list", "bill of lading", "b/l", "cmr", "tovarni list",
    "commercial invoice", "avizo", "awb", "deklaracija", "carina",
    "customs", "uvoz", "import", "shipment", "pošiljka", "posiljka",
    "container", "kontejner", "lcl", "fcl", "špedicija", "spedicija",
}

# Hard/soft sender logika sada živi unutar _is_shipment_email (v2 refaktor).


def _extract_body_text(message: dict, max_chars: int = 800) -> str:
    """Extract plain text from email body (first max_chars chars)."""
    def _decode_part(data: str) -> str:
        try:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _walk(parts) -> str:
        for part in parts:
            mime = part.get("mimeType", "")
            if mime == "text/plain":
                body_data = part.get("body", {}).get("data", "")
                if body_data:
                    return _decode_part(body_data)
            if part.get("parts"):
                result = _walk(part["parts"])
                if result:
                    return result
        return ""

    payload = message.get("payload", {})
    # Single-part message
    if payload.get("body", {}).get("data"):
        text = _decode_part(payload["body"]["data"])
        return text[:max_chars]
    # Multi-part
    text = _walk(payload.get("parts", []))
    return text[:max_chars]


_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp"}
_BUSINESS_EXTENSIONS = {".pdf", ".xlsx", ".xls"} | _IMAGE_EXTENSIONS

# Soft-reject subject patterns — these move to LLM gate when a business attachment is present,
# instead of being hard-rejected as before.
_SOFT_SUBJECT_SIGNALS = [
    # Komunalije / lokalni troškovi
    "račun za struj", "račun za vod", "račun za telefon", "račun za internet",
    "račun za goriv", "faktura za struj", "faktura za vod", "faktura za telefon",
    "faktura za internet", "faktura za goriv", "faktura za kirij",
    "komunalije", "komunalna", "režije", "rezije",
    "uplatnica", "virman",
    # Potvrde o plaćanju / e-računi (nisu uvozne fakture za robu)
    "potvrda o izvršenom plaćanju", "potvrda o izvrsenom placanju",
    "potvrda o uplati", "potvrda o plaćanju", "potvrda o placanju",
    "e-račun", "eracun", "e-racun", "e račun", "e racun", "taksa",
    # Lične app-forward poruke
    "sent from viber", "letsuseviber", "vb.me",
    # SaaS billing subject patterns
    "your subscription", "subscription invoice", "subscription renewal",
    "payment successful", "payment receipt", "payment confirmation",
    "billing statement", "account statement",
    "api usage", "cloud invoice", "software license", "license renewal",
    "trial ended", "plan renewal", "upgrade your plan", "auto-renewal",
    # Auto-responses
    "out of office", "van ureda", "automatski odgovor",
    "unsubscribe", "verify your email", "confirm your email",
    "reset your password", "welcome to", "getting started",
    # Admin / academic / internal
    "raspored", "sedmica", "semestar", "spiskovi", "spisak",
    "programiranje", "predavanja", "nastava", "ispit", "kolokvij",
    "minuta sa sastanka", "zapisnik", "odluka", "rješenje",
    "godišnji odmor", "bolovanje", "ugovor o radu", "plaća", "placa",
    "obavještenje", "obavijest za", "interni akt",
]

# Soft sender patterns — only move to LLM gate, do NOT hard-reject when attachment present
_SOFT_SENDER_SIGNALS = ["noreply", "no-reply", "donotreply", "notifications@", "newsletter"]


def _run_llm_gate(subject: str, sender: str, body: str,
                  attachment_names: list[str]) -> tuple[bool, str]:
    """
    Claude Haiku gate for ambiguous emails.
    Returns (is_shipment, reason). Defaults to REJECT on any failure.

    Ranije je default bio ACCEPT (recall priority), ali kad API padne (nema
    kredita / auth) svaki dvosmislen email prođe i pokrene SKUPU Sonnet
    ekstrakciju — 50 smeća = realan novčani gubitak. Propuštena prava pošiljka
    se lako vrati preko "Resetuj Ignored"; potrošeni novac ne. Zato: odbij.
    Logs token usage to DB (non-blocking).
    """
    try:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            return False, "nema API ključa — odbijeno (Resetuj Ignored kad se API vrati)"
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        att_list = ", ".join(attachment_names) if attachment_names else "(nema)"
        prompt = f"""Radi se o špedicijskoj firmi koja uvozi robu iz inostranstva.

Da li ovaj email sadrži dokumente vezane za uvoz robe ili carinjenje?
Odgovori DA ako je to: faktura za uvoz robe, packing lista, narudžba, avizo, izjava o porijeklu, carinski dokument, bill of lading, CMR, AWB, ili sličan dokument vezan za uvoz/špediciju.
Odgovori NE ako je to: komunalni račun, SaaS pretplata, interni akt, akademski dokument, raspored, automatski odgovor, marketing email, ili slično.

Subject: {subject}
Pošiljalac: {sender}
Fajlovi: {att_list}
Email (dio): {body[:300]}

Odgovori SAMO: DA ili NE."""
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = msg.content[0].text.strip().upper()
        is_shipment = answer.startswith("DA")
        # Log usage non-blocking
        try:
            from database import log_api_usage
            log_api_usage(None, "claude-haiku-4-5-20251001", "detect_gate", msg.usage)
        except Exception as _lue:
            logger.debug(f"log_api_usage (gate) greška: {_lue}")
        return is_shipment, f"Haiku gate: {answer}"
    except Exception as e:
        logger.warning(f"Haiku gate failed ({e}), defaulting to REJECT")
        return False, f"gate greška ({e}) — odbijeno da se ne troši na ekstrakciju"


def _is_shipment_email(subject: str, sender: str, body: str,
                       attachment_names: list[str]) -> tuple[bool, str, str]:
    """
    Filter: odbaci samo evidentni SaaS/tech billing. Sve ostalo s poslovnim prilogom
    šalje se kroz LLM gate.
    Logika: bolje obraditi jedan lažni email nego preskočiti pravu pošiljku.
    Returns (is_shipment: bool, reason: str, stage: str)  — stage in {"rule", "llm"}
    """
    subject_l = subject.lower()
    sender_l  = sender.lower()

    # ── Stage 1: Hard reject — unambiguous SaaS/tech billing senders only ─────
    # These services NEVER send shipping documents.
    _HARD_SENDER_REJECT = [
        "mailer-daemon", "postmaster",
        # Poznati SaaS billing sistemi koji NIKAD ne šalju špedicijske dokumente
        "anthropic.com", "openai.com", "stripe.com", "paddle.com",
        "chargebee.com", "recurly.com", "zuora.com", "braintree",
        "paypal.com", "aws.amazon", "azure.com",
        "slack.com", "notion.so", "hubspot.com", "salesforce.com",
        "dropbox.com", "zoom.us", "github.com", "gitlab.com",
        "atlassian.com", "adobe.com", "billing@google", "info@linkedin",
        # Lokalni servisi/komunalije koji NIKAD ne šalju špedicijske dokumente
        "bhtelecom.ba", "eracun.bht", "proton.me", "canva.com",
        "ipi-akademija.ba", "cinestar", "viber.com",
    ]
    for pat in _HARD_SENDER_REJECT:
        if pat in sender_l:
            return False, f"sender pattern '{pat}'", "rule"

    # ── Stage 2: Keyword signals ───────────────────────────────────────────
    all_text = subject_l + " " + " ".join(a.lower() for a in attachment_names)

    # Strong shipping signal → accept immediately (rule)
    strong_hit = [kw for kw in _STRONG_ACCEPT if kw in all_text]
    if strong_hit:
        return True, f"strong keyword: {strong_hit[0]}", "rule"

    # 2+ weak signals → accept (rule)
    matched_accept = [kw for kw in _SUBJECT_ACCEPT if kw in all_text]
    if len(matched_accept) >= 2:
        non_generic = [k for k in matched_accept if k not in ("invoice", "faktura", "xlsx", "xls")]
        if non_generic or len(matched_accept) >= 3:
            return True, f"keywords: {matched_accept[:3]}", "rule"

    # ── Stage 3: Check for business attachment ─────────────────────────────
    has_business_att = (
        any(Path(fn).suffix.lower() in _BUSINESS_EXTENSIONS for fn in attachment_names)
        if attachment_names else False
    )

    # Check soft-reject signals (subject patterns and generic sender patterns)
    soft_subject_hit = next((kw for kw in _SOFT_SUBJECT_SIGNALS if kw in subject_l), None)
    soft_sender_hit = next((pat for pat in _SOFT_SENDER_SIGNALS if pat in sender_l), None)
    has_soft_signal = soft_subject_hit or soft_sender_hit

    # Route to LLM gate if: has any business attachment, OR hit a soft-reject signal
    if has_business_att or has_soft_signal:
        is_ship, gate_reason = _run_llm_gate(subject, sender, body, attachment_names)
        return is_ship, gate_reason, "llm"

    # No business attachment and no signals → reject (rule)
    return False, "nema poslovnih priloga niti špedicijskih ključnih riječi", "rule"


ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/octet-stream",
    # Scanned / image invoices
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/webp",
}
ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".xls", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp"}
# TODO: .docx extraction — deferred (parser cannot read Word files; accepting them creates empty shipments)


def _get_or_create_label(service, name: str) -> str:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == name:
            return lbl["id"]
    new_label = service.users().labels().create(
        userId="me",
        body={"name": name, "labelListVisibility": "labelShow",
              "messageListVisibility": "show"}
    ).execute()
    return new_label["id"]


def _download_attachment(service, msg_id: str, attachment_id: str) -> bytes:
    att = service.users().messages().attachments().get(
        userId="me", messageId=msg_id, id=attachment_id
    ).execute()
    return base64.urlsafe_b64decode(att.get("data", "") + "==")


def _extract_attachments(service, message: dict) -> list[dict]:
    attachments = []

    def walk(parts):
        for part in parts:
            filename = part.get("filename", "")
            mime = part.get("mimeType", "")
            ext = Path(filename).suffix.lower() if filename else ""
            if filename and (mime in ALLOWED_MIME_TYPES or ext in ALLOWED_EXTENSIONS):
                body = part.get("body", {})
                att_id = body.get("attachmentId")
                if att_id:
                    data = _download_attachment(service, message["id"], att_id)
                    attachments.append({"filename": filename, "data": data})
                elif body.get("data"):
                    data = base64.urlsafe_b64decode(body["data"] + "==")
                    attachments.append({"filename": filename, "data": data})
            if part.get("parts"):
                walk(part["parts"])

    walk(message.get("payload", {}).get("parts", []))
    return attachments


def _get_header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _ingest_message(service, message: dict, db, label_ids: dict, _cb) -> str:
    """
    Process one Gmail message through the full ingestion pipeline.

    label_ids must contain keys: "pending", "processed", "ignored"
    Returns decision string: "accepted" | "rejected" | "duplicate" | "no_files"

    Side-effects: saves files to disk, creates PendingShipment in DB, applies Gmail labels,
    writes to detection_log.  Caller is responsible for rollback on exception.
    """
    msg_id = message["id"]

    subject     = _get_header(message, "subject") or "(bez naslova)"
    sender      = _get_header(message, "from") or "—"
    received_ts = int(message.get("internalDate", 0)) // 1000
    received_at = datetime.fromtimestamp(received_ts).strftime("%Y%m%d_%H%M%S")

    # ── Stage 0: quick attachment name peek (before download) ───
    att_names_preview = []
    def _peek_names(parts):
        for p in parts:
            fn = p.get("filename", "")
            if fn:
                att_names_preview.append(fn)
            if p.get("parts"):
                _peek_names(p["parts"])
    _peek_names(message.get("payload", {}).get("parts", []))

    # ── Shipment filter ─────────────────────────────────────────
    body_preview = _extract_body_text(message)
    is_shipment, reason, stage = _is_shipment_email(
        subject, sender, body_preview, att_names_preview
    )
    if not is_shipment:
        _cb(f"⛔ Ignorirano: '{subject}' — razlog: {reason}")
        service.users().messages().modify(
            userId="me", id=msg_id,
            body={"addLabelIds": [label_ids["ignored"]]}
        ).execute()
        log_detection(db, msg_id, subject, sender, att_names_preview,
                      "rejected", reason, stage)
        return "rejected"

    attachments = _extract_attachments(service, message)
    files = []
    for att in attachments:
        ext = Path(att["filename"]).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            continue
        safe_name = att["filename"].replace("/", "_").replace("\\", "_")
        save_path = UPLOADS_DIR / f"{received_at}_{safe_name}"
        save_path.write_bytes(att["data"])
        files.append({"filepath": str(save_path), "filename": att["filename"]})

    if not files:
        _cb(f"⚠ Nema PDF/XLS priloženih fajlova u: '{subject}'", "warning")
        service.users().messages().modify(
            userId="me", id=msg_id,
            body={"addLabelIds": [label_ids["processed"]]}
        ).execute()
        log_detection(db, msg_id, subject, sender, att_names_preview,
                      "no_files", "nema PDF/XLS/slika priloga", "rule")
        return "no_files"

    # ── Duplicate attachment detection ──────────────────────────
    att_hash = hashlib.sha256(
        b"".join(a["data"] for a in sorted(attachments, key=lambda x: x["filename"]))
    ).hexdigest()
    existing_hash = db.query(PendingShipment).filter_by(attachment_hash=att_hash).first()
    if existing_hash:
        dup_reason = f"duplikat pošiljke #{existing_hash.id}"
        _cb(f"⚠ Duplikat: '{subject}' ima iste fajlove kao pošiljka #{existing_hash.id} — preskočeno", "warning")
        service.users().messages().modify(
            userId="me", id=msg_id,
            body={"addLabelIds": [label_ids["ignored"]]}
        ).execute()
        log_detection(db, msg_id, subject, sender, att_names_preview,
                      "duplicate", dup_reason, "rule")
        return "duplicate"

    _cb(f"✅ Prihvaćen email: '{subject}' — {reason}")

    # Save to DB as pending
    ps = PendingShipment(
        gmail_message_id=msg_id,
        email_subject=subject,
        sender=sender,
        received_at=received_at,
        files_json=json.dumps(files),
        attachment_hash=att_hash,
        status="pending",
    )
    db.add(ps)
    db.commit()

    # Label as Spedicija-Pending in Gmail
    service.users().messages().modify(
        userId="me", id=msg_id,
        body={"addLabelIds": [label_ids["pending"]]}
    ).execute()

    log_detection(db, msg_id, subject, sender, att_names_preview,
                  "accepted", reason, stage)
    return "accepted"


def fetch_new_invoices(log_cb=None) -> int:
    """
    Poll Gmail for new emails with attachments.
    Saves attachments to disk and creates PendingShipment records.
    Returns number of new shipments added.
    log_cb: optional callable(msg, level) for pipeline-visible logging
    """
    def _cb(msg, level="info"):
        if log_cb:
            log_cb(msg, level)
        getattr(logger, level)(msg)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    init_db()

    try:
        creds = get_credentials()
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as e:
        _cb(f"Gmail auth greška: {e}", "error")
        return 0

    pending_label_id   = _get_or_create_label(service, PENDING_LABEL)
    processed_label_id = _get_or_create_label(service, PROCESSED_LABEL)
    ignored_label_id   = _get_or_create_label(service, IGNORED_LABEL)
    label_ids = {
        "pending":   pending_label_id,
        "processed": processed_label_id,
        "ignored":   ignored_label_id,
    }

    # Fetch emails not yet processed, pending, or ignored
    # Allow custom base query from settings (e.g. "from:supplier@example.com has:attachment")
    _mandatory_exclusions = (
        f"-label:{PENDING_LABEL} -label:{PROCESSED_LABEL} -label:{IGNORED_LABEL}"
    )
    from database import get_setting
    _db_for_setting = SessionLocal()
    try:
        _custom_filter = get_setting(_db_for_setting, "gmail_filter", "").strip()
    finally:
        _db_for_setting.close()
    if _custom_filter:
        query = f"{_custom_filter} {_mandatory_exclusions}"
    else:
        query = f"has:attachment {_mandatory_exclusions}"
    _cb(f"Gmail query: {query}")
    try:
        result = service.users().messages().list(
            userId="me", q=query, maxResults=50
        ).execute()
    except Exception as e:
        _cb(f"Gmail list greška: {e}", "error")
        return 0

    messages = result.get("messages", [])
    if not messages:
        _cb("Gmail: nema novih mejlova bez labela (svi su već obrađeni/ignorirani)")
        return 0

    _cb(f"Gmail: pronađeno {len(messages)} mejlova za provjeru...")
    db = SessionLocal()
    new_count = 0

    try:
        for msg_ref in messages:
            try:
                message = service.users().messages().get(
                    userId="me", id=msg_ref["id"], format="full"
                ).execute()

                msg_id = message["id"]

                # Skip if already in DB
                if db.query(PendingShipment).filter_by(gmail_message_id=msg_id).first():
                    continue

                decision = _ingest_message(service, message, db, label_ids, _cb)
                if decision == "accepted":
                    new_count += 1

            except Exception as e:
                _cb(f"Greška pri čitanju mejla {msg_ref['id']}: {e}", "error")
                db.rollback()
                continue
    finally:
        db.close()

    return new_count


def mark_gmail_processed(service, gmail_message_id: str):
    """Move email from Spedicija-Pending to Spedicija-Processed label."""
    try:
        pending_label_id   = _get_or_create_label(service, PENDING_LABEL)
        processed_label_id = _get_or_create_label(service, PROCESSED_LABEL)
        service.users().messages().modify(
            userId="me", id=gmail_message_id,
            body={
                "addLabelIds": [processed_label_id],
                "removeLabelIds": [pending_label_id],
            }
        ).execute()
    except Exception as e:
        logger.warning(f"Could not update Gmail label for {gmail_message_id}: {e}")
