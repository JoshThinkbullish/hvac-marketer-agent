import io
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from PIL import Image
from werkzeug.datastructures import FileStorage, MultiDict

import app as app_module
from agent.batch_store import BatchStore
from agent.drive_uploader import (
    SCOPES,
    DriveFolderError,
    DriveNotConnected,
    DriveUploader,
)


def credentials(token="access-token", expired=False):
    return Credentials(
        token=token,
        refresh_token=f"refresh-{token}",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client-id",
        client_secret="client-secret",
        scopes=SCOPES,
        expiry=(datetime.now(timezone.utc) - timedelta(minutes=5)
                if expired else datetime.now(timezone.utc) + timedelta(hours=1)),
    )


class RequestResult:
    def __init__(self, callback):
        self.callback = callback

    def execute(self, num_retries=0):
        return self.callback(num_retries)


class FakeDriveState:
    def __init__(self):
        self.files = []
        self.create_calls = 0
        self.fail_after_commit = 0


class FakeFiles:
    def __init__(self, state):
        self.state = state

    def list(self, **_kwargs):
        return RequestResult(lambda _retries: {"files": list(self.state.files)})

    def create(self, body, **_kwargs):
        def execute(_retries):
            self.state.create_calls += 1
            created = {
                "id": f"file-{self.state.create_calls}",
                "name": body["name"],
                "mimeType": body.get("mimeType", "image/png"),
                "webViewLink": f"https://drive.test/file-{self.state.create_calls}",
            }
            self.state.files.append(created)
            if self.state.fail_after_commit:
                self.state.fail_after_commit -= 1
                raise OSError("response lost after commit")
            return created

        return RequestResult(execute)


class FakeAbout:
    def __init__(self, email):
        self.email = email

    def get(self, **_kwargs):
        return RequestResult(lambda _retries: {
            "user": {"emailAddress": self.email, "displayName": self.email.split("@")[0]}
        })


class FakeService:
    def __init__(self, state, email):
        self.state = state
        self.email = email

    def about(self):
        return FakeAbout(self.email)

    def files(self):
        return FakeFiles(self.state)


def service_builder(state):
    def build(_name, _version, credentials, cache_discovery=False):
        del cache_discovery
        return FakeService(state, f"{credentials.token}@example.com")
    return build


def test_connections_are_encrypted_and_isolated_by_browser(tmp_path):
    store = BatchStore(tmp_path)
    drive = DriveUploader(store, "stable-app-secret", service_builder(FakeDriveState()))

    drive.save_connection("browser-a", credentials("alice"))
    drive.save_connection("browser-b", credentials("bob"))

    alice_record = store.load_drive_connection("browser-a")
    assert b"alice" not in alice_record["credential_blob"]
    assert drive.connection_status("browser-a")["account_email"] == "alice@example.com"
    assert drive.connection_status("browser-b")["account_email"] == "bob@example.com"

    drive.disconnect("browser-a", revoke=False)
    assert drive.connection_status("browser-a")["connected"] is False
    assert drive.connection_status("browser-b")["connected"] is True


def test_retry_after_committed_upload_returns_existing_file_without_duplicate(tmp_path):
    state = FakeDriveState()
    state.fail_after_commit = 1
    store = BatchStore(tmp_path)
    drive = DriveUploader(
        store, "stable-app-secret", service_builder(state), sleep=lambda _delay: None
    )
    drive.save_connection("browser", credentials())

    uploaded = drive.upload_image(
        "browser", "folder-1", b"png", "creative.png", "batch:item:v1"
    )

    assert uploaded["id"] == "file-1"
    assert state.create_calls == 1
    assert len(state.files) == 1


def test_concurrent_equal_export_keys_create_one_file(tmp_path):
    state = FakeDriveState()
    store = BatchStore(tmp_path)
    drive = DriveUploader(
        store, "stable-app-secret", service_builder(state), sleep=lambda _delay: None
    )
    drive.save_connection("browser", credentials())

    with ThreadPoolExecutor(max_workers=8) as pool:
        uploaded = list(pool.map(
            lambda _index: drive.upload_image(
                "browser", "folder-1", b"png", "creative.png", "same-key"
            ),
            range(20),
        ))

    assert {item["id"] for item in uploaded} == {"file-1"}
    assert state.create_calls == 1


class FolderFiles:
    def __init__(self):
        self.records = {
            "root": {
                "id": "root-id", "name": "Root", "mimeType":
                "application/vnd.google-apps.folder", "trashed": False,
                "capabilities": {"canAddChildren": True},
            },
            "writable": {
                "id": "writable", "name": "Campaigns", "mimeType":
                "application/vnd.google-apps.folder", "trashed": False,
                "capabilities": {"canAddChildren": True},
            },
            "readonly": {
                "id": "readonly", "name": "Archive", "mimeType":
                "application/vnd.google-apps.folder", "trashed": False,
                "capabilities": {"canAddChildren": False},
            },
            "not-folder": {
                "id": "not-folder", "name": "File", "mimeType": "text/plain",
                "trashed": False, "capabilities": {"canAddChildren": True},
            },
        }

    def get(self, fileId, **_kwargs):
        key = "root" if fileId == "root" else fileId
        return RequestResult(lambda _retries: dict(self.records[key]))

    def list(self, **_kwargs):
        return RequestResult(lambda _retries: {
            "files": [self.records["writable"], self.records["readonly"]]
        })


class FolderService(FakeService):
    def __init__(self, files):
        self.folder_files = files

    def about(self):
        return FakeAbout("folders@example.com")

    def files(self):
        return self.folder_files


def test_folder_listing_filters_read_only_and_validates_selection(tmp_path):
    files = FolderFiles()
    store = BatchStore(tmp_path)
    drive = DriveUploader(
        store, "stable-app-secret",
        lambda *_args, **_kwargs: FolderService(files),
    )
    drive.save_connection("browser", credentials())

    assert drive.get_folders("browser") == [
        {"id": "root-id", "name": "My Drive", "is_root": True},
        {"id": "writable", "name": "Campaigns", "shared_drive": False},
    ]
    assert drive.validate_folder("browser", "writable")["name"] == "Campaigns"
    with pytest.raises(DriveFolderError, match="cannot add files"):
        drive.validate_folder("browser", "readonly")
    with pytest.raises(DriveFolderError, match="not an active folder"):
        drive.validate_folder("browser", "not-folder")


def test_revoked_refresh_token_removes_saved_connection(tmp_path, monkeypatch):
    store = BatchStore(tmp_path)
    drive = DriveUploader(store, "stable-app-secret", service_builder(FakeDriveState()))
    drive.save_connection("browser", credentials(expired=True))

    def revoked(_self, _request):
        raise RefreshError("revoked")

    monkeypatch.setattr(Credentials, "refresh", revoked)

    assert drive.connection_status("browser")["connected"] is False
    assert store.load_drive_connection("browser") is None
    with pytest.raises(DriveNotConnected):
        drive.upload_image("browser", "folder", b"x", "x.png", "key")


class PreflightDrive:
    def __init__(self, reject=False):
        self.reject = reject
        self.validated = []
        self.disconnected = []

    def connection_status(self, connection_id):
        return {
            "connected": connection_id in {"browser-a", "browser-b"},
            "account_email": f"{connection_id}@example.com" if connection_id else "",
            "display_name": connection_id or "",
        }

    def validate_folder(self, connection_id, folder_id):
        self.validated.append((connection_id, folder_id))
        if self.reject:
            raise DriveFolderError("The folder is read-only.")
        return {"id": folder_id, "name": "Server-verified folder"}

    def disconnect(self, connection_id, revoke=True):
        self.disconnected.append((connection_id, revoke))


def generate_form():
    return {
        "business": "Drive Test Air",
        "callout": "Dallas",
        "headline": "$99 Tune-Up",
        "styles": "home_install",
        "setting": "suburban",
        "quality": "low",
        "mode": "images",
        "count": "1",
        "logo_mode": "overlay",
        "folder_id": "chosen-folder",
        "folder_name": "Untrusted browser name",
        "system_file": (io.BytesIO(image_bytes()), "System.png"),
    }


def test_generate_preflights_folder_and_uses_server_name(
    monkeypatch, tmp_path,
):
    drive = PreflightDrive()
    monkeypatch.setattr(app_module, "STORE", BatchStore(tmp_path / "data"))
    monkeypatch.setattr(app_module, "DRIVE", drive)
    monkeypatch.setattr(app_module, "_key_ok", lambda _name: True)
    started = []
    monkeypatch.setattr(
        app_module.threading.Thread, "start", lambda thread: started.append(thread)
    )
    client = app_module.app.test_client()
    with client.session_transaction() as browser_session:
        browser_session["drive_connection_id"] = "browser-a"

    response = client.post("/api/generate", data=generate_form())
    payload = response.get_json()

    assert response.status_code == 200
    assert drive.validated == [("browser-a", "chosen-folder")]
    assert started
    job = app_module.JOBS.pop(payload["job_id"])
    assert job["folder_name"] == "Server-verified folder"
    app_module._cleanup_brief_uploads(job["_brief"])


def test_read_only_folder_is_rejected_before_background_generation(
    monkeypatch, tmp_path,
):
    drive = PreflightDrive(reject=True)
    monkeypatch.setattr(app_module, "STORE", BatchStore(tmp_path / "data"))
    monkeypatch.setattr(app_module, "DRIVE", drive)
    monkeypatch.setattr(app_module, "_key_ok", lambda _name: True)
    started = []
    monkeypatch.setattr(
        app_module.threading.Thread, "start", lambda thread: started.append(thread)
    )
    client = app_module.app.test_client()
    with client.session_transaction() as browser_session:
        browser_session["drive_connection_id"] = "browser-a"

    response = client.post("/api/generate", data=generate_form())

    assert response.status_code == 400
    assert "read-only" in response.get_json()["error"]
    assert not started


def test_drive_status_and_disconnect_are_scoped_to_current_browser(monkeypatch):
    drive = PreflightDrive()
    monkeypatch.setattr(app_module, "DRIVE", drive)
    browser_a = app_module.app.test_client()
    browser_b = app_module.app.test_client()
    with browser_a.session_transaction() as browser_session:
        browser_session["drive_connection_id"] = "browser-a"
    with browser_b.session_transaction() as browser_session:
        browser_session["drive_connection_id"] = "browser-b"

    assert browser_a.get("/auth/status").get_json()["account_email"] == (
        "browser-a@example.com"
    )
    assert browser_b.get("/auth/status").get_json()["account_email"] == (
        "browser-b@example.com"
    )
    assert browser_a.get("/auth/disconnect").status_code == 405
    assert browser_a.post("/auth/disconnect").status_code == 302
    assert drive.disconnected == [("browser-a", True)]
    assert browser_b.get("/auth/status").get_json()["connected"] is True


def test_oauth_callback_rejects_missing_state_before_token_exchange(monkeypatch):
    called = []
    monkeypatch.setattr(
        app_module, "_make_flow", lambda *_args, **_kwargs: called.append(True)
    )

    response = app_module.app.test_client().get(
        "/auth/callback?state=attacker&code=fake"
    )

    assert response.status_code == 302
    assert "google_auth_failed" in response.location
    assert not called


def image_bytes():
    data = io.BytesIO()
    Image.new("RGB", (24, 24), (20, 90, 160)).save(data, format="PNG")
    return data.getvalue()


def make_brief(tmp_path, mode="images_copy"):
    form = MultiDict([
        ("business", "Drive Test Air"),
        ("website", "https://example.com"),
        ("callout", "Dallas"),
        ("headline", "$99 Tune-Up"),
        ("styles", "home_install"),
        ("setting", "suburban"),
        ("quality", "low"),
        ("mode", mode),
        ("count", "1"),
        ("logo_mode", "overlay"),
    ])
    files = MultiDict([
        ("system_file", FileStorage(
            stream=io.BytesIO(image_bytes()), filename="System.png",
            content_type="image/png",
        )),
    ])
    brief, error = app_module._parse_brief(form, files)
    assert error is None
    assert app_module._persist_brief_uploads(brief, files) is None
    return brief


def generated_copy():
    return {
        "angle": "Reliable comfort",
        "meta_primary_text": "Dallas homeowners can save today.",
        "brainrot_scripts": [
            {"title": "One", "body": "First script."},
            {"title": "Two", "body": "Second script."},
        ],
        "story_script": {"title": "A Cool Home", "body": "Story body."},
        "story_image_prompts": [
            {"line_from_script": "Story body.", "prompt": "A comfortable home."}
        ],
    }


class PipelineDrive:
    def __init__(self, fail_uploads=False):
        self.fail_uploads = fail_uploads
        self.folders = []
        self.uploads = []

    def create_subfolder(self, connection_id, parent_id, name, export_key):
        self.folders.append((connection_id, parent_id, name, export_key))
        return {
            "id": f"folder-{name.lower()}",
            "webViewLink": f"https://drive.test/{name.lower()}",
        }

    def _upload(self, kind, filename, export_key):
        if self.fail_uploads:
            raise OSError("Drive temporarily unavailable")
        self.uploads.append((kind, filename, export_key))
        return {
            "id": f"file-{len(self.uploads)}",
            "webViewLink": f"https://drive.test/file-{len(self.uploads)}",
        }

    def upload_doc(self, _connection_id, _folder_id, _text, name, export_key):
        return self._upload("doc", name, export_key)

    def upload_image(self, _connection_id, _folder_id, _data, name, export_key):
        return self._upload("image", name, export_key)

    def upload_bytes(self, _connection_id, _folder_id, _data, name,
                     _mime_type, export_key):
        return self._upload("bytes", name, export_key)


def prepare_pipeline(monkeypatch, tmp_path, fail_uploads=False, mode="images_copy"):
    store = BatchStore(tmp_path / "data")
    drive = PipelineDrive(fail_uploads=fail_uploads)
    monkeypatch.setattr(app_module, "STORE", store)
    monkeypatch.setattr(app_module, "DRIVE", drive)
    monkeypatch.setattr(app_module, "generate_image", lambda *_a, **_k: image_bytes())
    monkeypatch.setattr(app_module, "generate_copy", lambda **_kwargs: generated_copy())
    monkeypatch.setattr(app_module, "generate_voiceover", lambda *_a, **_k: b"mp3")
    brief = make_brief(tmp_path, mode=mode)
    job = app_module._new_job(brief, app_module._build_all_prompts(brief))
    job["folder_name"] = "Marketing"
    return store, drive, job


def test_pipeline_automatically_exports_images_and_copy(monkeypatch, tmp_path):
    _store, drive, job = prepare_pipeline(monkeypatch, tmp_path)

    app_module._run_job(job, "selected-folder", "browser-connection")

    assert job["status"] == "done"
    assert job["items"][0]["drive_status"] == "done"
    assert {asset["asset_id"] for asset in job["copy"]["assets"]} == {
        "ad-copy", "landing-page-prompt",
    }
    assert all(asset["drive_status"] == "done" for asset in job["copy"]["assets"])
    assert [folder[2] for folder in drive.folders] == [
        job["run_folder_name"], "Images",
    ]
    assert any(upload[0] == "image" for upload in drive.uploads)
    assert sum(upload[0] == "doc" for upload in drive.uploads) == 3

    client = app_module.app.test_client()
    for asset in job["copy"]["assets"]:
        response = client.get(f"/api/jobs/{job['id']}/assets/{asset['asset_id']}")
        assert response.status_code == 200
        assert response.data


def test_drive_outage_keeps_generated_images_and_copy_downloadable(
    monkeypatch, tmp_path,
):
    _store, _drive, job = prepare_pipeline(
        monkeypatch, tmp_path, fail_uploads=True
    )

    app_module._run_job(job, "selected-folder", "browser-connection")

    assert job["status"] == "done"
    assert job["items"][0]["status"] == "done"
    assert job["items"][0]["drive_status"] == "failed"
    assert all(asset["drive_status"] == "failed" for asset in job["copy"]["assets"])
    assert len(job["warnings"]) >= 4
    client = app_module.app.test_client()
    assert client.get(f"/api/jobs/{job['id']}/image/0").status_code == 200
    assert client.get(
        f"/api/jobs/{job['id']}/assets/ad-copy"
    ).status_code == 200


def test_full_pipeline_exports_scripts_voiceovers_and_b_roll(monkeypatch, tmp_path):
    _store, drive, job = prepare_pipeline(
        monkeypatch, tmp_path, mode="full"
    )

    app_module._run_job(job, "selected-folder", "browser-connection")

    asset_ids = {asset["asset_id"] for asset in job["copy"]["assets"]}
    assert asset_ids == {
        "ad-copy", "landing-page-prompt",
        "brainrot-1-script", "brainrot-1-voiceover",
        "brainrot-2-script", "brainrot-2-voiceover",
        "story-script", "story-voiceover", "story-b-roll",
    }
    assert [folder[2] for folder in drive.folders][-2:] == ["Images", "Videos"]
