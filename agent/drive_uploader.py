import io
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]
BASE_DIR = Path(__file__).parent.parent
TOKEN_PATH = BASE_DIR / "token.json"


def _get_service():
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("drive", "v3", credentials=creds)


def get_folders() -> list[dict]:
    """Return up to 500 Drive folders sorted by name."""
    service = _get_service()
    folders = []
    page_token = None
    while len(folders) < 500:
        resp = service.files().list(
            q="mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageSize=200,
            pageToken=page_token,
            orderBy="name",
        ).execute()
        folders.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return folders


def upload_to_folder(folder_id: str, image_bytes: bytes, filename: str) -> str:
    """Upload an image directly into the given Drive folder."""
    service = _get_service()
    media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype="image/png")
    uploaded = service.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media,
        fields="id, webViewLink",
    ).execute()
    service.permissions().create(
        fileId=uploaded["id"],
        body={"type": "anyone", "role": "reader"},
    ).execute()
    return uploaded.get("webViewLink", "")
