"""Secure, per-browser Google Drive connections and idempotent exports."""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
import urllib.parse
import urllib.request
from base64 import urlsafe_b64encode
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

# A custom picker lists arbitrary existing folders and the app writes into the
# chosen folder. That workflow currently needs the full Drive scope. See the
# README for the Google Cloud verification implications.
SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"
EXPORT_KEY_PROPERTY = "hvacExportKey"
READ_RETRIES = 3
WRITE_ATTEMPTS = 4


class DriveError(RuntimeError):
    """User-facing Drive integration failure."""


class DriveNotConnected(DriveError):
    pass


class DriveFolderError(DriveError):
    pass


def _escape_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _export_key(value: str) -> str:
    # appProperties are searchable, but their values are size-limited. A hash
    # also avoids putting client names or other run details into Drive metadata.
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, HttpError):
        status = int(getattr(error.resp, "status", 0) or 0)
        if status in {429, 500, 502, 503, 504}:
            return True
        if status == 403:
            content = error.content.decode("utf-8", errors="ignore")
            return any(reason in content for reason in (
                "rateLimitExceeded", "userRateLimitExceeded", "backendError",
            ))
        return False
    return isinstance(error, (ConnectionError, OSError, TimeoutError))


class DriveUploader:
    """Own encrypted OAuth credentials and Drive API operations.

    ``store`` is a :class:`BatchStore`-compatible object. Credentials are
    encrypted before they enter SQLite, and every method is scoped by a random
    connection id kept in the caller's signed Flask session.
    """

    def __init__(self, store, secret_key: str,
                 service_builder: Callable[..., Any] = build,
                 sleep: Callable[[float], None] = time.sleep):
        if not secret_key:
            raise ValueError("A stable secret key is required for Drive OAuth storage.")
        key = urlsafe_b64encode(hashlib.sha256(secret_key.encode("utf-8")).digest())
        self._fernet = Fernet(key)
        self._store = store
        self._service_builder = service_builder
        self._sleep = sleep
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}
        self._operation_locks: dict[str, list] = {}

    def _lock(self, connection_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(connection_id, threading.RLock())

    @contextmanager
    def _operation_lock(self, key: str):
        """Serialize equal idempotency keys without retaining locks forever."""
        with self._locks_guard:
            entry = self._operation_locks.setdefault(
                key, [threading.RLock(), 0]
            )
            entry[1] += 1
        lock = entry[0]
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._locks_guard:
                entry[1] -= 1
                if entry[1] == 0:
                    self._operation_locks.pop(key, None)

    def _encrypt(self, credentials: Credentials) -> bytes:
        return self._fernet.encrypt(credentials.to_json().encode("utf-8"))

    def _load(self, connection_id: str) -> tuple[Credentials, dict]:
        record = self._store.load_drive_connection(connection_id)
        if not record:
            raise DriveNotConnected(
                "Google Drive is not connected. Connect Drive and try again."
            )
        try:
            raw = self._fernet.decrypt(record["credential_blob"])
            info = json.loads(raw.decode("utf-8"))
            credentials = Credentials.from_authorized_user_info(info, SCOPES)
        except (InvalidToken, ValueError, KeyError, json.JSONDecodeError) as error:
            self._store.delete_drive_connection(connection_id)
            raise DriveNotConnected(
                "The saved Google Drive connection cannot be read. Reconnect Drive."
            ) from error
        return credentials, record

    def _save(self, connection_id: str, credentials: Credentials,
              record: dict | None = None, account_email: str = "",
              display_name: str = "") -> None:
        record = record or {}
        self._store.save_drive_connection(
            connection_id,
            self._encrypt(credentials),
            account_email or record.get("account_email", ""),
            display_name or record.get("display_name", ""),
        )

    def _credentials(self, connection_id: str) -> tuple[Credentials, dict]:
        with self._lock(connection_id):
            credentials, record = self._load(connection_id)
            if credentials.expired:
                if not credentials.refresh_token:
                    self._store.delete_drive_connection(connection_id)
                    raise DriveNotConnected(
                        "The Google Drive session expired. Reconnect Drive."
                    )
                try:
                    credentials.refresh(Request())
                except RefreshError as error:
                    self._store.delete_drive_connection(connection_id)
                    raise DriveNotConnected(
                        "The Google Drive session expired or was revoked. "
                        "Reconnect Drive."
                    ) from error
                self._save(connection_id, credentials, record)
            return credentials, record

    def _service(self, connection_id: str):
        credentials, _record = self._credentials(connection_id)
        return self._service_builder(
            "drive", "v3", credentials=credentials, cache_discovery=False
        )

    def save_connection(self, connection_id: str,
                        credentials: Credentials) -> dict[str, str]:
        """Save a completed OAuth exchange and return connected account info."""
        service = self._service_builder(
            "drive", "v3", credentials=credentials, cache_discovery=False
        )
        about = service.about().get(
            fields="user(displayName,emailAddress)"
        ).execute(num_retries=READ_RETRIES)
        user = about.get("user", {})
        account = {
            "account_email": user.get("emailAddress", ""),
            "display_name": user.get("displayName", ""),
        }
        with self._lock(connection_id):
            self._save(
                connection_id, credentials,
                account_email=account["account_email"],
                display_name=account["display_name"],
            )
        return account

    def connection_status(self, connection_id: str | None) -> dict[str, Any]:
        if not connection_id:
            return {"connected": False, "account_email": "", "display_name": ""}
        try:
            _credentials, record = self._credentials(connection_id)
        except DriveNotConnected:
            return {"connected": False, "account_email": "", "display_name": ""}
        return {
            "connected": True,
            "account_email": record.get("account_email", ""),
            "display_name": record.get("display_name", ""),
        }

    def disconnect(self, connection_id: str | None, revoke: bool = True) -> None:
        if not connection_id:
            return
        token = ""
        if revoke:
            try:
                credentials, _record = self._load(connection_id)
                token = credentials.refresh_token or credentials.token or ""
            except DriveNotConnected:
                pass
        self._store.delete_drive_connection(connection_id)
        with self._locks_guard:
            self._locks.pop(connection_id, None)
        if token:
            try:
                request = urllib.request.Request(
                    "https://oauth2.googleapis.com/revoke?" + urllib.parse.urlencode(
                        {"token": token}
                    ),
                    method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                urllib.request.urlopen(request, timeout=5).close()
            except OSError:
                # Local deletion is the important disconnect guarantee. Google
                # may be temporarily unreachable or the token already revoked.
                return

    def get_folders(self, connection_id: str) -> list[dict]:
        """Return up to 500 writable folders, including My Drive."""
        service = self._service(connection_id)
        folders: list[dict] = []
        root = service.files().get(
            fileId="root",
            fields="id,name,mimeType,trashed,capabilities(canAddChildren),driveId",
            supportsAllDrives=True,
        ).execute(num_retries=READ_RETRIES)
        if root.get("capabilities", {}).get("canAddChildren", True):
            folders.append({"id": root["id"], "name": "My Drive", "is_root": True})

        page_token = None
        while len(folders) < 500:
            response = service.files().list(
                q=f"mimeType='{FOLDER_MIME}' and trashed=false",
                fields=("nextPageToken,files(id,name,driveId,"
                        "capabilities(canAddChildren))"),
                pageSize=200,
                pageToken=page_token,
                orderBy="name_natural",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute(num_retries=READ_RETRIES)
            for folder in response.get("files", []):
                if not folder.get("capabilities", {}).get("canAddChildren", True):
                    continue
                folders.append({
                    "id": folder["id"],
                    "name": folder["name"],
                    "shared_drive": bool(folder.get("driveId")),
                })
                if len(folders) >= 500:
                    break
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        seen: set[str] = set()
        unique: list[dict] = []
        for folder in folders:
            if folder["id"] not in seen:
                unique.append(folder)
                seen.add(folder["id"])
        return unique

    def validate_folder(self, connection_id: str, folder_id: str) -> dict:
        """Verify the selected folder exists and accepts new children."""
        try:
            folder = self._service(connection_id).files().get(
                fileId=folder_id,
                fields=("id,name,mimeType,trashed,webViewLink,driveId,"
                        "capabilities(canAddChildren)"),
                supportsAllDrives=True,
            ).execute(num_retries=READ_RETRIES)
        except HttpError as error:
            status = int(getattr(error.resp, "status", 0) or 0)
            if status in {403, 404}:
                raise DriveFolderError(
                    "That Drive folder is unavailable to the connected account. "
                    "Choose another folder."
                ) from error
            raise DriveError(f"Could not verify the Drive folder: {error}") from error
        if folder.get("trashed") or folder.get("mimeType") != FOLDER_MIME:
            raise DriveFolderError("The selected Drive item is not an active folder.")
        if not folder.get("capabilities", {}).get("canAddChildren", True):
            raise DriveFolderError(
                "The connected account cannot add files to that Drive folder."
            )
        return {
            "id": folder["id"],
            "name": folder.get("name", "Google Drive"),
            "webViewLink": folder.get("webViewLink", ""),
            "shared_drive": bool(folder.get("driveId")),
        }

    def _find_existing(self, service, folder_id: str,
                       export_key: str) -> dict | None:
        digest = _export_key(export_key)
        query = (
            f"'{_escape_query(folder_id)}' in parents and trashed=false and "
            f"appProperties has {{ key='{EXPORT_KEY_PROPERTY}' and "
            f"value='{digest}' }}"
        )
        response = service.files().list(
            q=query,
            fields="files(id,name,mimeType,webViewLink)",
            pageSize=1,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute(num_retries=READ_RETRIES)
        files = response.get("files", [])
        return files[0] if files else None

    def _create_idempotent(self, connection_id: str, folder_id: str,
                           export_key: str, request_factory) -> dict:
        digest = _export_key(export_key)
        with self._operation_lock(f"{connection_id}:{digest}"):
            service = self._service(connection_id)
            last_error: Exception | None = None
            for attempt in range(WRITE_ATTEMPTS):
                existing = self._find_existing(service, folder_id, export_key)
                if existing:
                    return existing
                try:
                    return request_factory(service, digest).execute(num_retries=0)
                except Exception as error:
                    last_error = error
                    if not _is_retryable(error) or attempt == WRITE_ATTEMPTS - 1:
                        raise
                    self._sleep(0.25 * (2 ** attempt))
            raise DriveError(f"Drive upload failed: {last_error}")

    def create_subfolder(self, connection_id: str, parent_id: str, name: str,
                         export_key: str) -> dict:
        def request_factory(service, digest):
            return service.files().create(
                body={
                    "name": name,
                    "mimeType": FOLDER_MIME,
                    "parents": [parent_id],
                    "appProperties": {EXPORT_KEY_PROPERTY: digest},
                },
                fields="id,name,mimeType,webViewLink",
                supportsAllDrives=True,
            )

        return self._create_idempotent(
            connection_id, parent_id, export_key, request_factory
        )

    def upload_bytes(self, connection_id: str, folder_id: str, data: bytes,
                     filename: str, mime_type: str, export_key: str) -> dict:
        def request_factory(service, digest):
            media = MediaIoBaseUpload(
                io.BytesIO(data), mimetype=mime_type, resumable=False
            )
            return service.files().create(
                body={
                    "name": filename,
                    "parents": [folder_id],
                    "appProperties": {EXPORT_KEY_PROPERTY: digest},
                },
                media_body=media,
                fields="id,name,mimeType,webViewLink",
                supportsAllDrives=True,
            )

        return self._create_idempotent(
            connection_id, folder_id, export_key, request_factory
        )

    def upload_image(self, connection_id: str, folder_id: str, data: bytes,
                     filename: str, export_key: str) -> dict:
        return self.upload_bytes(
            connection_id, folder_id, data, filename, "image/png", export_key
        )

    def upload_doc(self, connection_id: str, folder_id: str, text: str,
                   name: str, export_key: str) -> dict:
        encoded = text.encode("utf-8")
        last_error: Exception | None = None
        for source_mime in ("text/markdown", "text/plain"):
            def request_factory(service, digest, source_mime=source_mime):
                media = MediaIoBaseUpload(
                    io.BytesIO(encoded), mimetype=source_mime, resumable=False
                )
                return service.files().create(
                    body={
                        "name": name,
                        "parents": [folder_id],
                        "mimeType": DOC_MIME,
                        "appProperties": {EXPORT_KEY_PROPERTY: digest},
                    },
                    media_body=media,
                    fields="id,name,mimeType,webViewLink",
                    supportsAllDrives=True,
                )

            try:
                return self._create_idempotent(
                    connection_id, folder_id, export_key, request_factory
                )
            except (HttpError, ConnectionError, OSError, TimeoutError) as error:
                last_error = error
        raise DriveError(f"Drive doc upload failed for '{name}': {last_error}")
