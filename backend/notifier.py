"""
Email notification after successful pipeline processing.
Reuses the already-authorised Gmail API credentials — no extra SMTP setup needed.
"""
import logging
from pathlib import Path

logger = logging.getLogger("notifier")


def send_processing_complete(
    to_email: str,
    subject: str,
    items_count: int,
    tariffs_assigned: int,
    drive_folder: str,
    excel_name: str = "",
    xml_name: str = "",
    suspect_count: int = 0,
) -> bool:
    """
    Send a short summary email via Gmail API after a shipment is processed.
    Returns True on success, False on failure (non-critical — pipeline continues either way).
    """
    if not to_email:
        return False

    try:
        import base64
        from email.mime.text import MIMEText
        from googleapiclient.discovery import build
        from google_auth import get_credentials

        creds = get_credentials()
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)

        body = (
            f"Pošiljka obrađena: {subject}\n\n"
            f"  Stavki: {items_count}\n"
            f"  Tarifnih brojeva dodijeljeno: {tariffs_assigned}/{items_count}\n"
            f"  Drive folder: {drive_folder}\n"
        )
        if excel_name:
            body += f"  Excel: {excel_name}\n"
        if xml_name:
            body += f"  XML: {xml_name}\n"
        if suspect_count:
            body += (
                f"\n  ⚠ {suspect_count} stavki treba provjeriti "
                f"(nizak/srednji AI confidence tarifnog broja)\n"
            )

        msg = MIMEText(body, "plain", "utf-8")
        msg["To"] = to_email
        msg["From"] = "me"
        prefix = "⚠" if suspect_count else "✅"
        msg["Subject"] = f"{prefix} Spedicija: {subject[:60]}"

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        logger.info(f"Notifikacija poslana: {to_email} — '{subject}'")
        return True

    except Exception as e:
        logger.warning(f"Notifikacija neuspješna za {to_email}: {e}")
        return False
