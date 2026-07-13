"""Drive uploader backed by per-user OAuth.

The operator clicks Connect, completes Google's consent screen, and the
returned token is persisted at TOKEN_PATH. Uploaded files are owned by
that user account and count against their personal Drive quota (which
fixes the service-account "no storage quota" failure).

The full `https://www.googleapis.com/auth/drive` scope is used so the
custom folder dropdown can list every folder the user can see, and the
app can create files inside any of them.
"""
import io
import os
import threading
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# Full drive scope: the folder picker lists arbitrary pre-existing folders
# and the pipeline creates subfolders/files inside them — drive.file alone
# cannot write into folders the app didn't create.
SCOPES = ["https://www.googleapis.com/auth/drive"]
BASE_DIR = Path(__file__).parent.parent
TOKEN_PATH = BASE_DIR / "token.json"

# Serializes the read/refresh/rewrite/unlink of token.json across the
# parallel image-upload workers and the copy-pipeline thread.
_TOKEN_LOCK = threading.Lock()


def _get_service():
    with _TOKEN_LOCK:
        if not TOKEN_PATH.exists():
            raise RuntimeError(
                "Google Drive not connected. Click 'Connect Google Drive' on "
                "the form to authorize."
            )
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                # Token revoked or expired beyond refresh — drop it so the UI
                # goes back to the Connect state instead of erroring forever.
                TOKEN_PATH.unlink(missing_ok=True)
                raise RuntimeError(
                    "Google Drive session expired. Click 'Connect Google "
                    "Drive' to re-authorize."
                ) from e
            with open(TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def is_available() -> bool:
    """True iff the user has completed the OAuth handshake."""
    return TOKEN_PATH.exists()


def get_folders() -> list[dict]:
    """Up to 500 folders the connected user can see."""
    service = _get_service()
    folders: list[dict] = []
    page_token = None
    while len(folders) < 500:
        resp = service.files().list(
            q="mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageSize=200,
            pageToken=page_token,
            orderBy="name",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        folders.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return folders


def create_subfolder(parent_id: str, name: str) -> dict:
    """Create a folder under parent_id. Returns {id, webViewLink}."""
    service = _get_service()
    created = service.files().create(
        body={
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        },
        fields="id, webViewLink",
        supportsAllDrives=True,
    ).execute()
    return {"id": created["id"], "webViewLink": created.get("webViewLink", "")}


def upload_bytes(folder_id: str, data: bytes, filename: str,
                 mime_type: str = "application/octet-stream") -> str:
    """Upload raw bytes to a Drive folder; returns webViewLink."""
    service = _get_service()
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type)
    uploaded = service.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True,
    ).execute()
    return uploaded.get("webViewLink", "")


def upload_to_folder(folder_id: str, image_bytes: bytes, filename: str) -> str:
    """Backwards-compatible PNG uploader."""
    return upload_bytes(folder_id, image_bytes, filename, mime_type="image/png")


def upload_text(folder_id: str, text: str, filename: str,
                mime_type: str = "text/markdown") -> str:
    return upload_bytes(folder_id, text.encode("utf-8"), filename, mime_type=mime_type)


def upload_doc(folder_id: str, text: str, name: str) -> str:
    """Upload markdown text as a native Google Doc.

    Drive's import flow converts text/markdown to Google Docs server-side.
    Falls back to text/plain if markdown import is rejected. Returns the
    webViewLink.
    """
    service = _get_service()
    body = {
        "name": name,
        "parents": [folder_id],
        "mimeType": "application/vnd.google-apps.document",
    }
    encoded = text.encode("utf-8")
    last_err: Exception | None = None
    for source_mime in ("text/markdown", "text/plain"):
        try:
            media = MediaIoBaseUpload(io.BytesIO(encoded), mimetype=source_mime)
            uploaded = service.files().create(
                body=body,
                media_body=media,
                fields="id, webViewLink",
                supportsAllDrives=True,
            ).execute()
            return uploaded.get("webViewLink", "")
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Drive doc upload failed for '{name}': {last_err}")
