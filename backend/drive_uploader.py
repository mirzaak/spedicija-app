"""
Google Drive uploader.
Creates 'Spedicija' root folder and per-declaration subfolders.
Uploads: original invoice, generated Excel, generated XML.
"""
import logging
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth import get_credentials

logger = logging.getLogger("drive_uploader")

ROOT_FOLDER_NAME = "Spedicija"

MIME_TYPES = {
    ".pdf":  "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls":  "application/vnd.ms-excel",
    ".xml":  "application/xml",
}


def _get_or_create_folder(service, name: str, parent_id: str = None) -> str:
    """Get or create a Drive folder by name. Returns folder ID."""
    query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"

    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    body = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        body["parents"] = [parent_id]

    folder = service.files().create(body=body, fields="id").execute()
    return folder["id"]


def _upload_file(service, filepath: str, parent_id: str) -> str:
    """Upload a file to Drive folder. Returns file ID."""
    path = Path(filepath)
    mime = MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")

    media = MediaFileUpload(filepath, mimetype=mime, resumable=False)
    file_meta = {"name": path.name, "parents": [parent_id]}

    uploaded = service.files().create(
        body=file_meta, media_body=media, fields="id,webViewLink"
    ).execute()

    return uploaded.get("webViewLink", uploaded.get("id", ""))


def upload_declaration_files(
    subfolder_name: str,
    original_path: str = None,
    excel_path: str = None,
    xml_path: str = None,
) -> dict:
    """
    Upload declaration files to Drive.
    Creates: Spedicija/{subfolder_name}/ and uploads provided files.

    Returns dict with Drive links for each uploaded file.
    """
    try:
        creds = get_credentials()
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.error(f"Drive auth failed: {e}")
        return {"error": str(e)}

    # Get/create root Spedicija folder
    root_id = _get_or_create_folder(service, ROOT_FOLDER_NAME)

    # Create subfolder for this declaration
    sub_id = _get_or_create_folder(service, subfolder_name, parent_id=root_id)

    links = {}

    for label, path in [("original", original_path), ("excel", excel_path), ("xml", xml_path)]:
        if not path or not Path(path).exists():
            continue
        try:
            link = _upload_file(service, path, sub_id)
            links[label] = link
            logger.info(f"Uploaded {label}: {Path(path).name} → Drive/{ROOT_FOLDER_NAME}/{subfolder_name}/")
        except Exception as e:
            logger.error(f"Failed to upload {label} ({path}): {e}")
            links[label] = f"ERROR: {e}"

    return links
