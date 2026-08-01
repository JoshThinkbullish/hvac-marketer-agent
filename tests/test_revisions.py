import io
import time
from pathlib import Path

import app as app_module
from PIL import Image
from werkzeug.datastructures import FileStorage, MultiDict


def image_bytes(colour):
    data = io.BytesIO()
    Image.new("RGB", (24, 24), colour).save(data, format="PNG")
    return data.getvalue()


def make_completed_job(monkeypatch, count=4):
    form = MultiDict([
        ("business", "Persistent Air"),
        ("callout", "London"),
        ("headline", "$79 Tune-Up"),
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
            filename="Original System.png",
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
    job = app_module._new_job(
        brief, app_module._build_all_prompts(brief)
    )
    app_module._run_job(job)
    return job


def wait_for_revision(client, status_url, headers=None):
    for _ in range(200):
        payload = client.get(status_url, headers=headers or {}).get_json()
        if payload["status"] in {"done", "error"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("Revision did not finish")


def test_selected_revision_preserves_unselected_files_and_hashes(monkeypatch):
    job = make_completed_job(monkeypatch, count=4)
    original = {
        item["item_id"]: (item["current_file"], item["sha256"], item["version"])
        for item in job["items"]
    }
    selected = job["items"][2]
    monkeypatch.setattr(
        app_module, "generate_image",
        lambda *_args, **_kwargs: image_bytes((180, 80, 40)),
    )
    monkeypatch.setattr(app_module, "_key_ok", lambda _name: True)

    client = app_module.app.test_client()
    client.get("/")
    response = client.post(
        f"/api/ui/jobs/{job['id']}/revisions",
        data={
            "mode": "edit_selected",
            "selected_item_ids": f'["{selected["item_id"]}"]',
            "instruction": "Make the sky warmer",
            "quality": "low",
            "reference_manifest": "[]",
            "text_replacements": "[]",
        },
        headers={"Idempotency-Key": "selected-once"},
    )
    assert response.status_code == 202
    result = wait_for_revision(client, response.get_json()["status_url"])

    assert result["status"] == "done"
    assert [item["position"] for item in result["items"]] == [1, 2, 3, 4]
    assert selected["version"] == 2
    assert selected["changed"] is True
    for item in job["items"]:
        if item is selected:
            continue
        path, digest, version = original[item["item_id"]]
        assert item["current_file"] == path
        assert item["sha256"] == digest
        assert item["version"] == version
        assert item["changed"] is False


def test_equipment_replacement_versions_whole_batch_and_is_idempotent(
    monkeypatch,
):
    job = make_completed_job(monkeypatch, count=3)
    original_brief = {
        key: value for key, value in job["_brief"].items()
        if key not in {"system_path", "active_equipment_path"}
    }
    monkeypatch.setattr(
        app_module, "generate_image",
        lambda *_args, **_kwargs: image_bytes((80, 180, 70)),
    )
    monkeypatch.setattr(app_module, "_key_ok", lambda _name: True)

    client = app_module.app.test_client()
    client.get("/")

    def post_replacement():
        return client.post(
            f"/api/ui/jobs/{job['id']}/revisions",
            data={
                "mode": "replace_equipment",
                "selected_item_ids": "[]",
                "instruction": "",
                "quality": "medium",
                "reference_manifest": "[]",
                "text_replacements": "[]",
                "equipment_file": (
                    io.BytesIO(image_bytes((220, 220, 220))),
                    "Lennox Replacement.png",
                ),
            },
            headers={"Idempotency-Key": "equipment-once"},
        )

    first = post_replacement()
    wait_for_revision(client, first.get_json()["status_url"])
    second = post_replacement()
    assert first.status_code == second.status_code == 202
    assert first.get_json()["revision_id"] == second.get_json()["revision_id"]
    assert all(item["version"] == 2 and item["changed"] for item in job["items"])
    assert Path(job["_brief"]["active_equipment_path"]).is_file()
    assert {
        key: value for key, value in job["_brief"].items()
        if key not in {"system_path", "active_equipment_path"}
    } == original_brief
    assert len(job["revision_history"]) == 1


def test_batch_can_be_restored_from_sqlite_after_memory_is_cleared(monkeypatch):
    job = make_completed_job(monkeypatch, count=2)
    batch_id = job["id"]
    expected_hashes = [item["sha256"] for item in job["items"]]
    app_module.JOBS.pop(batch_id)

    response = app_module.app.test_client().get(f"/api/jobs/{batch_id}")
    restored = response.get_json()

    assert response.status_code == 200
    assert restored["batch_id"] == batch_id
    assert [item["sha256"] for item in restored["items"]] == expected_hashes
    assert all("current_file" not in item for item in restored["items"])
    assert app_module.app.test_client().get(
        f"/api/jobs/{batch_id}/image/0"
    ).status_code == 200


def test_service_revisions_require_bearer_and_force_low_quality(monkeypatch):
    job = make_completed_job(monkeypatch, count=1)
    calls = []

    def fake_generate(*_args, **kwargs):
        calls.append(kwargs)
        return image_bytes((140, 60, 180))

    monkeypatch.setattr(app_module, "generate_image", fake_generate)
    monkeypatch.setattr(app_module, "_key_ok", lambda _name: True)
    monkeypatch.setattr(app_module, "SERVICE_API_KEY", "service-secret")
    client = app_module.app.test_client()
    data = {
        "mode": "edit_selected",
        "selected_item_ids": f'["{job["items"][0]["item_id"]}"]',
        "instruction": "Warm the lighting",
        "quality": "high",
        "reference_manifest": "[]",
        "text_replacements": "[]",
    }
    unauthorized = client.post(
        f"/api/jobs/{job['id']}/revisions",
        data=data,
        headers={"Idempotency-Key": "service-auth"},
    )
    assert unauthorized.status_code == 401

    accepted = client.post(
        f"/api/jobs/{job['id']}/revisions",
        data=data,
        headers={
            "Authorization": "Bearer service-secret",
            "Idempotency-Key": "service-auth",
        },
    )
    assert accepted.status_code == 202
    result = wait_for_revision(
        client,
        accepted.get_json()["status_url"],
        headers={"Authorization": "Bearer service-secret"},
    )
    assert result["status"] == "done"
    assert calls[-1]["quality"] == "low"
