"""
Google OAuth2 authentication manager.
Handles token creation, refresh, and persistence.

Usage:
  Place credentials.json in: spedicija-app/credentials.json
  On first run, a browser window opens for Google login.
  Token is saved to: spedicija-app/data/token.json (auto-refreshed).
"""
import os
import logging
from pathlib import Path
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger("google_auth")

BASE_DIR = Path(__file__).parent.parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "data" / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",   # for labeling processed emails
    "https://www.googleapis.com/auth/drive.file",      # create/upload files on Drive
]


def get_credentials() -> Credentials:
    """
    Load or refresh Google OAuth2 credentials.
    Opens browser on first run to authorize.
    """
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                # Token revoked or expired beyond refresh — delete and re-auth
                logger.warning("Refresh token revoked/expired — deleting token.json, starting fresh OAuth flow")
                TOKEN_FILE.unlink(missing_ok=True)
                creds = None

        if not creds or not creds.valid:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"credentials.json not found at {CREDENTIALS_FILE}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(creds.to_json())

    return creds
