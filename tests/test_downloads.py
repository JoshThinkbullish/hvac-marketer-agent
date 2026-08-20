import io
import zipfile

from PIL import Image
from werkzeug.datastructures import FileStorage, MultiDict

import app as app_module
from agent.batch_store import BatchStore


def image_bytes(colour):
    data = io.BytesIO()
    Image.new("RGB", (24, 24), colour).save(data, format="PNG")
    return data.getvalue()


def completed_job(monkeypatch, tmp_path, count=3):
    monkeypatch.setattr(app_module, "STORE", BatchStore(tmp_path / "data"))
    form = MultiDict([
        ("business", "Download Test Air"),
        ("callout", "Dallas"),
        ("headline", "$99 Tune-Up"),
        ("styles", "home_install"),
        ("setting", "suburban"),
        ("quality", "low"),
        ("mode", "images"),
        ("count", str(count)),
        ("logo_mode", "overlay"),
    ])
    files = MultiDict([
        ("system_file", FileStorage(
            stream=io.BytesIO(image_bytes((20, 90, 160))),
            filename="System.png",
            content_type="image/png",
        )),
    ])
    brief, error = app_module._parse_brief(form, files)
    assert error is None
    assert app_module._persist_brief_uploads(brief, files) is None
    monkeypatch.setattr(
        app_module, "generate_image",
        lambda *_args, **_kwargs: image_bytes((30, 120, 190)),
    )
    job = app_module._new_job(brief, app_module._build_all_prompts(brief))
    app_module._run_job(job)
    return job


def read_zip(response):
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        return {
            name: archive.read(name)
            for name in archive.namelist()
        }


def test_download_all_contains_every_latest_image_and_survives_restore(
    monkeypatch, tmp_path,
):
    job = completed_job(monkeypatch, tmp_path, count=3)
    client = app_module.app.test_client()

    first = client.get(f"/api/jobs/{job['id']}/images.zip")
    first_files = read_zip(first)
    assert first.status_code == 200
    assert sorted(first_files) == [
        "01_home_install_v1.png",
        "02_home_install_v1.png",
        "03_home_install_v1.png",
    ]

    revised = image_bytes((180, 80, 40))
    monkeypatch.setattr(
        app_module, "generate_image", lambda *_args, **_kwargs: revised
    )
    app_module._run_revision_item(job, {
        "mode": "edit_selected",
        "instruction": "Warm the sky",
        "quality": "low",
        "reference_manifest": [],
        "reference_paths": [],
        "text_replacements": [],
    }, job["items"][1])
    app_module._persist_job(job)
    app_module.JOBS.pop(job["id"])

    restored = client.get(f"/api/jobs/{job['id']}/images.zip")
    restored_files = read_zip(restored)
    assert restored.status_code == 200
    assert sorted(restored_files) == [
        "01_home_install_v1.png",
        "02_home_install_v2.png",
        "03_home_install_v1.png",
    ]
    assert restored_files["02_home_install_v2.png"] == revised
    assert "download-test-air_images.zip" in restored.headers[
        "Content-Disposition"
    ]
    app_module.JOBS.pop(job["id"], None)


def test_download_all_includes_completed_images_from_partial_run(
    monkeypatch, tmp_path,
):
    job = completed_job(monkeypatch, tmp_path, count=3)
    unavailable = job["items"][1]
    unavailable["current_file"] = ""
    unavailable["local_image"] = False
    unavailable["status"] = "failed"

    response = app_module.app.test_client().get(
        f"/api/jobs/{job['id']}/images.zip"
    )

    assert response.status_code == 200
    assert sorted(read_zip(response)) == [
        "01_home_install_v1.png", "03_home_install_v1.png",
    ]
    app_module.JOBS.pop(job["id"], None)


def test_download_all_requires_two_completed_images(monkeypatch, tmp_path):
    job = completed_job(monkeypatch, tmp_path, count=1)

    response = app_module.app.test_client().get(
        f"/api/jobs/{job['id']}/images.zip"
    )

    assert response.status_code == 400
    assert "At least two" in response.get_json()["error"]
    app_module.JOBS.pop(job["id"], None)


def test_finished_run_ui_offers_download_all():
    html = app_module.app.test_client().get("/").get_data(as_text=True)

    assert "Download all ${downloadableCount} images (.zip)" in html
    assert "/images.zip" in html
