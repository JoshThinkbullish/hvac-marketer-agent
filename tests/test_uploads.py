import io
from pathlib import Path

import app as app_module
from PIL import Image
from werkzeug.datastructures import FileStorage, MultiDict

from app import (
    JOBS,
    _build_all_prompts,
    _cleanup_brief_uploads,
    _new_job,
    _parse_brief,
    _persist_brief_uploads,
    _run_job,
    _run_one_image,
    app,
)


def image_bytes(fmt="PNG"):
    data = io.BytesIO()
    Image.new("RGB", (24, 24), (20, 90, 160)).save(data, format=fmt)
    return data.getvalue()


def form_data(**overrides):
    values = MultiDict([
        ("business", "Dallas Air"),
        ("callout", "Dallas"),
        ("headline", "$99 Tune-Up"),
        ("styles", "home_install"),
        ("setting", "suburban"),
        ("quality", "low"),
        ("mode", "images"),
        ("count", "1"),
        ("logo_mode", "overlay"),
    ])
    for key, value in overrides.items():
        values.setlist(key, value if isinstance(value, list) else [value])
    return values


def upload_files(with_logo=True):
    files = MultiDict([
        ("system_file", FileStorage(
            stream=io.BytesIO(image_bytes()),
            filename="Lennox System.png",
            content_type="image/png",
        )),
    ])
    if with_logo:
        files.add("logo_file", FileStorage(
            stream=io.BytesIO(image_bytes()),
            filename="Dallas Air Logo.png",
            content_type="image/png",
        ))
    return files


def test_parse_brief_uses_uploaded_filenames_and_location():
    brief, error = _parse_brief(form_data(), upload_files())

    assert error is None
    assert brief["system_name"] == "Lennox System"
    assert brief["logo_name"] == "Dallas Air Logo"
    assert brief["logo_mode"] == "overlay"
    assert brief["callout"] == "Dallas"


def test_logo_upload_is_optional():
    brief, error = _parse_brief(form_data(), upload_files(with_logo=False))

    assert error is None
    assert brief["logo_name"] == ""
    assert brief["logo_mode"] == "none"


def test_equipment_upload_is_required_and_must_be_an_image():
    brief, error = _parse_brief(form_data(), MultiDict())
    assert brief is None
    assert error == "Upload an equipment image."

    bad_files = MultiDict([
        ("system_file", FileStorage(
            stream=io.BytesIO(b"not an image"),
            filename="equipment.png",
            content_type="image/png",
        )),
    ])
    brief, error = _parse_brief(form_data(), bad_files)
    assert brief is None
    assert error == "Equipment image is not a valid image file."


def test_uploaded_assets_are_job_owned_and_cleaned_up():
    files = upload_files()
    brief, error = _parse_brief(form_data(), files)
    assert error is None

    assert _persist_brief_uploads(brief, files) is None
    upload_dir = Path(brief["_upload_dir"])
    assert Path(brief["system_path"]).read_bytes() == image_bytes()
    assert Path(brief["logo_path"]).read_bytes() == image_bytes()

    _cleanup_brief_uploads(brief)
    assert not upload_dir.exists()


def test_preview_accepts_uploads_and_includes_dallas_grounding():
    client = app.test_client()
    data = dict(form_data().items())
    data["system_file"] = (io.BytesIO(image_bytes()), "Lennox System.png")
    data["logo_file"] = (io.BytesIO(image_bytes()), "Dallas Air Logo.png")

    response = client.post("/api/preview", data=data)
    payload = response.get_json()

    assert response.status_code == 200
    assert 'callout "Dallas"' in payload["prompts"][0]["prompt"]
    assert "Do not display the location name as text" in payload["prompts"][0]["prompt"]


def test_index_uses_upload_controls_instead_of_asset_dropdowns():
    response = app.test_client().get("/")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'name="system_file"' in html
    assert 'name="logo_file"' in html
    assert 'id="combo_system"' not in html
    assert 'id="combo_logo"' not in html
    assert 'id="submitBtn" class="btn btn--primary" disabled' not in html
    assert "optional for Images only" in html
    assert 'class="tile__download"' in html


def test_images_only_can_start_without_drive(monkeypatch):
    monkeypatch.setattr(app_module, "_key_ok", lambda _name: True)
    monkeypatch.setattr(app_module, "drive_is_available", lambda: False)
    started = []
    monkeypatch.setattr(
        app_module.threading.Thread, "start", lambda thread: started.append(thread))

    data = dict(form_data().items())
    data["system_file"] = (io.BytesIO(image_bytes()), "Lennox System.png")
    response = app.test_client().post("/api/generate", data=data)
    payload = response.get_json()

    assert response.status_code == 200
    assert started
    job = JOBS.pop(payload["job_id"])
    assert job["folder_name"] == "Local downloads"
    _cleanup_brief_uploads(job["_brief"])


def test_copy_modes_still_require_drive(monkeypatch):
    monkeypatch.setattr(app_module, "_key_ok", lambda _name: True)
    monkeypatch.setattr(app_module, "drive_is_available", lambda: False)

    data = dict(form_data(mode="images_copy").items())
    data["system_file"] = (io.BytesIO(image_bytes()), "Lennox System.png")
    response = app.test_client().post("/api/generate", data=data)

    assert response.status_code == 400
    assert "Images only can run without Drive" in response.get_json()["error"]


def test_local_image_run_finishes_with_full_resolution_download(monkeypatch):
    files = upload_files(with_logo=False)
    brief, error = _parse_brief(form_data(), files)
    assert error is None
    assert _persist_brief_uploads(brief, files) is None

    output = image_bytes()
    monkeypatch.setattr(app_module, "generate_image", lambda *_args, **_kwargs: output)
    prompts = _build_all_prompts(brief)
    job = _new_job(brief, prompts)
    item = job["items"][0]

    _run_one_image(job, item)

    assert item["status"] == "done"
    assert item["local_image"] is True
    response = app.test_client().get(f"/api/jobs/{job['id']}/image/0")
    assert response.status_code == 200
    assert response.data == output
    assert "attachment" in response.headers["Content-Disposition"]

    JOBS.pop(job["id"], None)
    _cleanup_brief_uploads(brief)


def test_complete_local_job_never_calls_drive(monkeypatch):
    files = upload_files(with_logo=False)
    brief, error = _parse_brief(form_data(), files)
    assert error is None
    assert _persist_brief_uploads(brief, files) is None

    monkeypatch.setattr(
        app_module, "generate_image", lambda *_args, **_kwargs: image_bytes())
    monkeypatch.setattr(
        app_module,
        "create_subfolder",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Drive should not be used for a local-only job")),
    )
    job = _new_job(brief, _build_all_prompts(brief))
    job["folder_name"] = "Local downloads"

    _run_job(job)

    assert job["status"] == "done"
    assert job["phase"] == "Complete"
    assert job["items"][0]["status"] == "done"
    assert job["items"][0]["local_image"] is True
    assert not Path(brief["_upload_dir"]).exists()
    JOBS.pop(job["id"], None)
