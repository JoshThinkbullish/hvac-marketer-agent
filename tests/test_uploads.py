import io
from pathlib import Path

from PIL import Image
from werkzeug.datastructures import FileStorage, MultiDict

from app import (
    _cleanup_brief_uploads,
    _parse_brief,
    _persist_brief_uploads,
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
