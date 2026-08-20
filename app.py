from __future__ import annotations

import io
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, UnidentifiedImageError
from flask import (
    Flask, jsonify, redirect, render_template, request, send_file, session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env", override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hvac-marketer")

from agent.ad_templates import (
    DEFAULT_STYLE_KEYS,
    SETTINGS,
    STYLES,
    OfferContext,
    brand_name,
    build_prompt,
)
from agent.copy_generator import (
    generate_copy,
    render_landing_page_prompt,
    render_meta_copy_md,
    render_script_md,
    render_story_b_roll_md,
)
from agent.drive_uploader import (
    SCOPES as DRIVE_SCOPES,
    DriveError,
    DriveFolderError,
    DriveNotConnected,
    DriveUploader,
)
from agent.image_generator import (
    composite_logo,
    generate_image,
    make_thumbnail,
)
from agent.batch_store import BatchStore, default_data_root
from agent.voice_generator import DEFAULT_VOICE_ID, generate_voiceover

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-me")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
secure_cookie_setting = os.environ.get("SESSION_COOKIE_SECURE", "").lower()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(
        secure_cookie_setting in {"1", "true", "yes"}
        if secure_cookie_setting
        else bool(os.environ.get("RENDER") or GOOGLE_REDIRECT_URI.startswith("https://"))
    ),
)
if app.secret_key == "dev-secret-key-change-me":
    log.warning(
        "FLASK_SECRET_KEY is using the development default. Set a stable, "
        "random value before connecting Google Drive in production."
    )

# Browser uploads supported by Pillow and the image API.
SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
SUPPORTED_FORMATS = {"PNG", "JPEG", "WEBP"}
SUPPORTED_MIMES = {"image/png", "image/jpeg", "image/webp"}
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_UPLOAD_PIXELS = 50_000_000
app.config["MAX_CONTENT_LENGTH"] = (MAX_UPLOAD_BYTES * 10) + (2 * 1024 * 1024)

MAX_IMAGES = 20
IMAGE_CONCURRENCY = 4
QUALITIES = {"low", "medium", "high"}
MODES = {"images", "images_copy", "full"}
REFERENCE_ROLES = {
    "primary_equipment", "supporting_product", "logo", "general_reference",
}
RETENTION_DAYS = max(7, int(os.environ.get("HVAC_RETENTION_DAYS", "7")))
SERVICE_API_KEY = os.environ.get("HVAC_SERVICE_API_KEY", "").strip()
STORE = BatchStore(default_data_root(BASE_DIR))
STORE.prune(RETENTION_DAYS)
DRIVE = DriveUploader(STORE, app.secret_key)


@app.errorhandler(413)
def upload_too_large(_error):
    return jsonify({
        "error": "Uploads are too large. Keep each image at 15 MB or less.",
        "code": "uploads_too_large",
    }), 413


def _key_ok(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def _slugify(value: str, fallback: str = "client") -> str:
    cleaned = re.sub(r"[^\w\s-]", "", value or "").strip().lower()
    cleaned = re.sub(r"[\s_-]+", "-", cleaned)
    return cleaned or fallback


def _lines(raw: str) -> list[str]:
    return [l.strip() for l in (raw or "").splitlines() if l.strip()]


def _drive_connection_id() -> str:
    return str(session.get("drive_connection_id", ""))


def _drive_status() -> dict:
    return DRIVE.connection_status(_drive_connection_id())


def drive_is_available() -> bool:
    """Compatibility helper used by request preflight and existing callers."""
    return bool(_drive_status()["connected"])


def _oauth_redirect_uri() -> str:
    return GOOGLE_REDIRECT_URI or url_for("auth_callback", _external=True)


# ── Main form ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # A signed Flask session distinguishes the browser UI from external
    # service callers. External callers must use the service bearer token.
    session["ui_access"] = True
    drive_status = _drive_status()
    return render_template(
        "index.html",
        styles=list(STYLES.values()),
        settings=SETTINGS,
        default_styles=DEFAULT_STYLE_KEYS,
        max_images=MAX_IMAGES,
        connected=drive_status["connected"],
        drive_account=drive_status.get("account_email", ""),
        creds_present=bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET),
        openai_ok=_key_ok("OPENAI_API_KEY"),
        anthropic_ok=_key_ok("ANTHROPIC_API_KEY"),
        elevenlabs_ok=_key_ok("ELEVENLABS_API_KEY"),
        connected_msg=request.args.get("connected"),
        error_msg=request.args.get("error"),
    )


# ── Drive folders API ──────────────────────────────────────────────────────────

@app.route("/api/drive/folders")
def api_drive_folders():
    connection_id = _drive_connection_id()
    if not connection_id or not drive_is_available():
        return jsonify({"error": "Not connected to Google Drive"}), 401
    try:
        return jsonify({"folders": DRIVE.get_folders(connection_id)})
    except DriveNotConnected as e:
        session.pop("drive_connection_id", None)
        return jsonify({"error": str(e)}), 401
    except DriveError as e:
        return jsonify({"error": str(e)}), 500


# ── Google OAuth ───────────────────────────────────────────────────────────────

def _make_flow(state=None):
    from google_auth_oauthlib.flow import Flow
    return Flow.from_client_config(
        {"web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [_oauth_redirect_uri()],
        }},
        scopes=DRIVE_SCOPES,
        state=state,
        redirect_uri=_oauth_redirect_uri(),
    )


@app.route("/auth/google")
def auth_google():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return redirect(url_for("index", error="missing_credentials"))
    flow = _make_flow()
    auth_url, state = flow.authorization_url(
        prompt="consent",
        access_type="offline",
        include_granted_scopes="true",
    )
    session["oauth_state"] = state
    return redirect(auth_url)


@app.route("/auth/callback")
def auth_callback():
    expected_state = session.pop("oauth_state", "")
    supplied_state = request.args.get("state", "")
    if (not expected_state or not supplied_state
            or not hmac.compare_digest(expected_state, supplied_state)):
        log.warning("Rejected Google OAuth callback with invalid state.")
        return redirect(url_for("index", error="google_auth_failed"))
    if request.host.split(":", 1)[0] in {"127.0.0.1", "localhost"}:
        # OAuthlib requires this opt-in for the permitted loopback HTTP flow.
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    # Google may return more scopes than requested (previously granted);
    # don't hard-fail the exchange over it.
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    flow = _make_flow(state=expected_state)
    try:
        flow.fetch_token(authorization_response=request.url)
        connection_id = uuid.uuid4().hex
        account = DRIVE.save_connection(connection_id, flow.credentials)
    except Exception:
        log.warning("Google OAuth callback failed:\n%s", traceback.format_exc())
        return redirect(url_for("index", error="google_auth_failed"))
    old_connection_id = session.get("drive_connection_id")
    session["drive_connection_id"] = connection_id
    if old_connection_id and old_connection_id != connection_id:
        DRIVE.disconnect(str(old_connection_id), revoke=False)
    session["drive_account"] = account.get("account_email", "")
    return redirect(url_for("index", connected="true"))


@app.route("/auth/disconnect", methods=["POST"])
def auth_disconnect():
    connection_id = session.pop("drive_connection_id", None)
    session.pop("drive_account", None)
    DRIVE.disconnect(connection_id)
    return redirect(url_for("index"))


@app.route("/auth/status")
def auth_status():
    status = _drive_status()
    status["creds_present"] = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
    return jsonify(status)


# ── Brief parsing (shared by preview + generate) ──────────────────────────────

def _validate_image_upload(upload, label: str, required: bool = False) -> str | None:
    """Validate a browser upload and return a friendly error, if any."""
    if not upload or not (upload.filename or "").strip():
        return f"Upload an {label.lower()}." if required else None

    suffix = Path(upload.filename).suffix.lower()
    if suffix not in SUPPORTED_EXTS:
        return f"{label} must be a PNG, JPG, JPEG, or WebP image."
    if (upload.mimetype or "").lower() not in SUPPORTED_MIMES:
        return f"{label} has an unsupported MIME type."

    try:
        upload.stream.seek(0, os.SEEK_END)
        size = upload.stream.tell()
        upload.stream.seek(0)
        if size <= 0:
            return f"{label} is empty."
        if size > MAX_UPLOAD_BYTES:
            return f"{label} must be 15 MB or smaller."
        with Image.open(upload.stream) as image:
            if image.format not in SUPPORTED_FORMATS:
                return f"{label} must be a PNG, JPG, JPEG, or WebP image."
            if image.width * image.height > MAX_UPLOAD_PIXELS:
                return f"{label} is too large; use an image under 50 megapixels."
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        return f"{label} is not a valid image file."
    finally:
        upload.stream.seek(0)
    return None


def _upload_name(upload, fallback: str) -> str:
    stem = Path(upload.filename or "").stem.strip()
    stem = re.sub(r"\s+", " ", stem)
    return stem[:120] or fallback


def _persist_brief_uploads(brief: dict, files) -> str | None:
    """Copy request-scoped uploads to a job-owned temporary directory."""
    upload_dir = Path(tempfile.mkdtemp(prefix="hvac-marketer-"))
    try:
        system_upload = files.get("system_file")
        logo_upload = files.get("logo_file")

        system_suffix = Path(system_upload.filename).suffix.lower()
        system_path = upload_dir / f"equipment{system_suffix}"
        system_upload.save(system_path)
        brief["system_path"] = str(system_path)

        if logo_upload and (logo_upload.filename or "").strip():
            logo_suffix = Path(logo_upload.filename).suffix.lower()
            logo_path = upload_dir / f"logo{logo_suffix}"
            logo_upload.save(logo_path)
            brief["logo_path"] = str(logo_path)

        brief["_upload_dir"] = str(upload_dir)
        return None
    except Exception as exc:
        shutil.rmtree(upload_dir, ignore_errors=True)
        return f"Could not save the uploaded images: {exc}"


def _cleanup_brief_uploads(brief: dict) -> None:
    upload_dir = brief.get("_upload_dir", "")
    if upload_dir:
        shutil.rmtree(upload_dir, ignore_errors=True)


def _parse_brief(form, files) -> tuple[dict | None, str | None]:
    client_name = form.get("business", "").strip()
    website     = form.get("website", "").strip()
    callout     = form.get("callout", "").strip()
    headline    = form.get("headline", "").strip()
    subheadline = form.get("subheadline", "").strip()
    features     = _lines(form.get("features", ""))
    dont_include = _lines(form.get("dont_include", ""))
    system_upload = files.get("system_file")
    logo_upload = files.get("logo_file")
    logo_mode   = form.get("logo_mode", "overlay").strip()
    setting     = form.get("setting", "vary").strip()
    quality     = form.get("quality", "high").strip()
    mode        = form.get("mode", "images").strip()
    style_keys  = [s for s in form.getlist("styles") if s in STYLES]

    try:
        count = int(round(float((form.get("count") or "5").strip())))
    except (ValueError, OverflowError):
        return None, "Image count must be a number."

    if not client_name:
        return None, "Client name is required."
    if not headline:
        return None, "Headline (the main offer) is required."
    upload_err = _validate_image_upload(
        system_upload, "Equipment image", required=True)
    if upload_err:
        return None, upload_err
    upload_err = _validate_image_upload(logo_upload, "Logo image")
    if upload_err:
        return None, upload_err
    if not style_keys:
        return None, "Pick at least one ad style."
    if not 1 <= count <= MAX_IMAGES:
        return None, f"Image count must be between 1 and {MAX_IMAGES}."
    if quality not in QUALITIES:
        return None, "Quality must be low, medium, or high."
    if mode not in MODES:
        return None, "Unknown output mode."
    if mode != "images" and not callout:
        return None, ("Callout (city / region) is required when generating "
                      "ad copy or the full pipeline.")
    if setting not in SETTINGS:
        setting = "vary"

    system_name = _upload_name(system_upload, "HVAC equipment")
    logo_name = (_upload_name(logo_upload, "Client logo")
                 if logo_upload and (logo_upload.filename or "").strip()
                 else "")
    if not logo_name:
        logo_mode = "none"
    elif logo_mode not in {"ai", "overlay"}:
        logo_mode = "overlay"

    return {
        "client_name": client_name,
        "website": website,
        "callout": callout,
        "headline": headline,
        "subheadline": subheadline,
        "features": features,
        "dont_include": dont_include,
        "system_name": system_name,
        "system_path": "",
        "logo_name": logo_name,
        "logo_path": "",
        "logo_mode": logo_mode,
        "setting": setting,
        "quality": quality,
        "mode": mode,
        "style_keys": style_keys,
        "count": count,
    }, None


def _build_all_prompts(brief: dict) -> list[dict]:
    """One entry per image: cycle the selected styles, varying each repeat."""
    style_keys = brief["style_keys"]
    occurrences: dict[str, int] = {}
    items = []
    for i in range(brief["count"]):
        key = style_keys[i % len(style_keys)]
        variant = occurrences.get(key, 0)
        occurrences[key] = variant + 1
        ctx = OfferContext(
            client_name=brief["client_name"],
            system_name=brief["system_name"],
            headline=brief["headline"],
            subheadline=brief["subheadline"],
            features=brief["features"],
            dont_include=brief["dont_include"],
            callout=brief["callout"],
            setting=brief["setting"],
            logo_mode=brief["logo_mode"],
            variant=variant,
            image_index=i,
        )
        items.append({
            "idx": i,
            "style_key": key,
            "style_label": STYLES[key].label,
            "prompt": build_prompt(key, ctx),
        })
    return items


def _brief_warnings(brief: dict) -> list[str]:
    """Non-blocking heads-ups shown in preview and attached to the job."""
    warnings: list[str] = []
    brand = brand_name(brief["system_name"])
    organic = [k for k in brief["style_keys"] if STYLES[k].family == "organic"]
    offer_text = " ".join(
        [brief["headline"], brief["subheadline"], *brief["features"]])
    if organic and brand and re.search(
            rf"\b{re.escape(brand)}\b", offer_text, re.IGNORECASE):
        labels = ", ".join(STYLES[k].label for k in organic)
        warnings.append(
            f'Your offer text names "{brand}", but the organic styles '
            f'({labels}) forbid rendering the HVAC brand as text. Use '
            f'"new AC" / "new system" wording for those styles.')
    if brief["count"] < len(brief["style_keys"]):
        warnings.append(
            f"Only the first {brief['count']} of {len(brief['style_keys'])} "
            f"selected styles will be used — raise the image count to "
            f"{len(brief['style_keys'])} to cover all of them.")
    return warnings


@app.route("/api/preview", methods=["POST"])
def api_preview():
    """Build the exact prompts a run would use, without spending credits."""
    brief, err = _parse_brief(request.form, request.files)
    if err:
        return jsonify({"error": err}), 400
    items = _build_all_prompts(brief)
    return jsonify({
        "prompts": [
            {"idx": it["idx"] + 1, "style": it["style_label"],
             "prompt": it["prompt"]}
            for it in items
        ],
        "warnings": _brief_warnings(brief),
    })


# ── Job store ──────────────────────────────────────────────────────────────────

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
MAX_JOBS = 30
PRUNE_GRACE_SECONDS = 120   # keep finished jobs pollable for at least this long
SHUTTING_DOWN = threading.Event()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _durabilize_inputs(brief: dict, batch_id: str) -> None:
    """Copy request-owned inputs into the durable batch directory."""
    assets = STORE.batch_dir(batch_id) / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    source_system = Path(brief["system_path"])
    system_path = assets / f"original-equipment{source_system.suffix.lower()}"
    shutil.copy2(source_system, system_path)
    brief["system_path"] = str(system_path)
    brief["original_equipment_path"] = str(system_path)
    brief["active_equipment_path"] = str(system_path)

    if brief.get("logo_path"):
        source_logo = Path(brief["logo_path"])
        logo_path = assets / f"logo{source_logo.suffix.lower()}"
        shutil.copy2(source_logo, logo_path)
        brief["logo_path"] = str(logo_path)


def _batch_payload(job: dict) -> dict:
    public = _public_job(job)
    # The durable snapshot retains server-only file paths; API responses use
    # image URLs and never expose the host filesystem.
    public["items"] = [dict(item) for item in job["items"]]
    public["_brief"] = {
        k: v for k, v in job["_brief"].items() if k != "_upload_dir"
    }
    public["_prompts"] = {str(k): v for k, v in job["_prompts"].items()}
    public["_images_folder_id"] = job.get("_images_folder_id", "")
    public["_drive_connection_id"] = job.get("_drive_connection_id", "")
    public["_main_done"] = job.get("_main_done", False)
    return public


def _persist_job(job: dict) -> None:
    STORE.save_batch(
        job["id"], job["status"], job["created"], _batch_payload(job)
    )


def _restore_job(payload: dict) -> dict:
    brief = payload.pop("_brief")
    prompts = {
        int(k): v for k, v in payload.pop("_prompts", {}).items()
    }
    images_folder_id = payload.pop("_images_folder_id", "")
    drive_connection_id = payload.pop("_drive_connection_id", "")
    main_done = payload.pop("_main_done", True)
    job = dict(payload)
    job["_brief"] = brief
    job["_prompts"] = prompts
    job["_thumbs"] = {}
    job["_full_images"] = {}
    job["_finished_at"] = time.time()
    job["_main_done"] = main_done
    job["_refines"] = 0
    job["_images_folder_id"] = images_folder_id
    job["_drive_connection_id"] = drive_connection_id
    job.setdefault("copy", {}).setdefault("assets", [])
    for item in job.get("items", []):
        item.setdefault("drive_file_id", "")
        item.setdefault("drive_status", "done" if item.get("drive_link") else "skipped")
        item.setdefault("drive_error", "")
    job["_cancel"] = threading.Event()
    job["_lock"] = threading.RLock()
    if job["status"] == "running":
        job["status"] = "error"
        job["phase"] = "Interrupted"
        job["error"] = (
            "The server restarted before this operation completed. "
            "Completed images remain available."
        )
    return job


def _get_job(job_id: str) -> dict | None:
    job = JOBS.get(job_id)
    if job:
        return job
    payload = STORE.load_batch(job_id)
    if not payload:
        return None
    job = _restore_job(payload)
    with JOBS_LOCK:
        JOBS.setdefault(job_id, job)
        job = JOBS[job_id]
    _persist_job(job)
    return job


def _new_job(brief: dict, items: list[dict]) -> dict:
    batch_id = uuid.uuid4().hex[:12]
    _durabilize_inputs(brief, batch_id)
    job = {
        "id": batch_id,
        "batch_id": batch_id,
        "status": "running",
        "phase": "Starting",
        "created": datetime.now().isoformat(timespec="seconds"),
        "client_name": brief["client_name"],
        "mode": brief["mode"],
        "run_folder_link": "",
        "run_folder_name": "",
        "prompt_doc_link": "",
        "items": [
            {
                "idx": it["idx"],
                "item_id": f"item_{uuid.uuid4().hex[:16]}",
                "position": it["idx"] + 1,
                "style_key": it["style_key"],
                "style_label": it["style_label"],
                "status": "queued",
                "error": "",
                "drive_link": "",
                "drive_file_id": "",
                "drive_status": "pending",
                "drive_error": "",
                "has_thumb": False,
                "local_image": False,
                "current_file": "",
                "sha256": "",
                "version": 1,
                "parent_item_id": None,
                "parent_version": None,
                "changed": False,
            }
            for it in items
        ],
        "copy": {
            "status": "skipped" if brief["mode"] == "images" else "pending",
            "links": [],
            "assets": [],
            "error": "",
            "angle": "",
        },
        "warnings": [],
        "error": "",
        "revision_history": [],
        "_brief": brief,
        "_prompts": {it["idx"]: it["prompt"] for it in items},
        "_thumbs": {},
        "_full_images": {},
        "_finished_at": 0.0,
        "_main_done": False,
        "_refines": 0,
        "_images_folder_id": "",
        "_drive_connection_id": "",
        "_cancel": threading.Event(),
        "_lock": threading.RLock(),
    }
    with JOBS_LOCK:
        JOBS[job["id"]] = job
        if len(JOBS) > MAX_JOBS:
            now = time.time()
            # Only evict jobs that finished long enough ago that the browser
            # has definitely seen the final poll; JOBS may transiently exceed
            # MAX_JOBS, which is harmless.
            finished = [
                j for j in JOBS.values()
                if j["status"] != "running"
                and now - j["_finished_at"] > PRUNE_GRACE_SECONDS
            ]
            finished.sort(key=lambda j: j["_finished_at"])
            for old in finished[: len(JOBS) - MAX_JOBS]:
                JOBS.pop(old["id"], None)
        # Full-res PNGs are kept for click-to-refine; cap the memory by
        # keeping them only on the 3 most recently finished jobs.
        keep = sorted(
            (j for j in JOBS.values() if j["status"] != "running"),
            key=lambda j: j["_finished_at"], reverse=True,
        )[3:]
        for old in keep:
            old["_full_images"] = {}
    _persist_job(job)
    return job


def _public_job(job: dict) -> dict:
    public = {k: v for k, v in job.items() if not k.startswith("_")}
    public["copy"] = dict(job["copy"])
    public["copy"]["links"] = [dict(link) for link in job["copy"].get("links", [])]
    public["copy"]["assets"] = [
        {
            **asset,
            "download_url": (
                f"/api/jobs/{job['id']}/assets/{asset['asset_id']}"
            ),
        }
        for asset in job["copy"].get("assets", [])
    ]
    public["items"] = []
    for item in job["items"]:
        current_file = Path(item.get("current_file", ""))
        is_downloadable = current_file.is_file()
        exposed = {
            key: value for key, value in item.items()
            if key != "current_file"
        }
        exposed["local_image"] = is_downloadable
        exposed["image_url"] = (
            f"/api/jobs/{job['id']}/image/{item['idx']}"
            if is_downloadable else ""
        )
        public["items"].append(exposed)
    return public


# ── Pipeline ───────────────────────────────────────────────────────────────────

def _save_copy_asset(job: dict, asset_id: str, label: str, kind: str,
                     filename: str, data: bytes, mime_type: str) -> dict:
    """Durably save a generated copy/audio asset before any Drive upload."""
    export_dir = STORE.batch_dir(job["id"]) / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / filename
    path.write_bytes(data)
    asset = {
        "asset_id": asset_id,
        "label": label,
        "kind": kind,
        "filename": filename,
        "mime_type": mime_type,
        "drive_link": "",
        "drive_file_id": "",
        "drive_status": "pending",
        "drive_error": "",
    }
    with job["_lock"]:
        existing = next(
            (item for item in job["copy"].setdefault("assets", [])
             if item["asset_id"] == asset_id),
            None,
        )
        if existing:
            existing.update(asset)
            asset = existing
        else:
            job["copy"]["assets"].append(asset)
        _persist_job(job)
    return asset


def _mark_asset_uploaded(job: dict, asset: dict, uploaded: dict) -> None:
    with job["_lock"]:
        asset.update({
            "drive_link": uploaded.get("webViewLink", ""),
            "drive_file_id": uploaded.get("id", ""),
            "drive_status": "done",
            "drive_error": "",
        })
        job["copy"]["links"].append({
            "label": asset["label"],
            "kind": asset["kind"],
            "url": asset["drive_link"],
        })
        _persist_job(job)

def _render_prompt_sheet(brief: dict, prompts: dict[int, str],
                         items: list[dict]) -> str:
    lines = [
        f"# {brief['client_name']} — Image Prompts",
        "",
        f"**Headline:** {brief['headline']}",
    ]
    if brief["subheadline"]:
        lines.append(f"**Sub-headline:** {brief['subheadline']}")
    if brief["features"]:
        lines.append("**Also include:** " + " · ".join(brief["features"]))
    if brief["dont_include"]:
        lines.append("**Don't include:** " + " · ".join(brief["dont_include"]))
    lines += [
        f"**System:** {brief['system_name']}   **Quality:** {brief['quality']}",
        "",
        "Each block below is the exact prompt used for that image. Paste "
        "into any image tool to re-run or tweak.",
        "",
    ]
    for it in items:
        # Fenced so Drive's markdown→Doc conversion keeps the prompt's line
        # breaks intact — the sheet's whole point is copy-paste re-runs.
        lines += [
            f"## {it['idx'] + 1:02d} · {it['style_label']}",
            "",
            "```",
            prompts[it["idx"]],
            "```",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def _run_copy_pipeline(job: dict, connection_id: str, run_folder_id: str,
                       videos_folder_id: str | None):
    brief = job["_brief"]
    copy_state = job["copy"]
    if job["_cancel"].is_set():
        with job["_lock"]:
            copy_state["error"] = "Cancelled"
            copy_state["status"] = "failed"
        return
    with job["_lock"]:
        copy_state["status"] = "running"

    offer_text = f"Main offer (headline): {brief['headline']}"
    if brief["subheadline"]:
        offer_text += f"\nSecondary offer (sub-headline): {brief['subheadline']}"
    if brief["features"]:
        offer_text += "\nAlso include: " + "; ".join(brief["features"])

    try:
        copy = generate_copy(
            client_name=brief["client_name"],
            website=brief["website"],
            callout=brief["callout"],
            offer=offer_text,
            system_name=brief["system_name"],
        )
    except Exception as e:
        log.error("Claude copy generation failed:\n%s", traceback.format_exc())
        with job["_lock"]:
            copy_state["error"] = str(e)
            copy_state["status"] = "failed"
            job["warnings"].append(f"Ad copy generation failed: {e}")
        return

    def _add_warning(msg: str):
        with job["_lock"]:
            job["warnings"].append(msg)

    def _save_and_upload_doc(asset_id: str, label: str, filename: str,
                             doc_name: str, markdown: str,
                             folder_id: str, kind: str = "doc"):
        asset = _save_copy_asset(
            job, asset_id, label, kind, filename,
            markdown.encode("utf-8"), "text/markdown",
        )
        try:
            with job["_lock"]:
                asset["drive_status"] = "uploading"
            uploaded = DRIVE.upload_doc(
                connection_id, folder_id, markdown, doc_name,
                export_key=f"{job['id']}:copy:{asset_id}",
            )
            _mark_asset_uploaded(job, asset, uploaded)
        except Exception as e:
            with job["_lock"]:
                asset["drive_status"] = "failed"
                asset["drive_error"] = str(e)
                _persist_job(job)
            _add_warning(
                f"{label} Drive upload failed: {e} — a local download is available."
            )

    def _save_and_upload_audio(asset_id: str, label: str, filename: str,
                               audio: bytes, folder_id: str):
        asset = _save_copy_asset(
            job, asset_id, label, "audio", filename, audio, "audio/mpeg"
        )
        try:
            with job["_lock"]:
                asset["drive_status"] = "uploading"
            uploaded = DRIVE.upload_bytes(
                connection_id, folder_id, audio, filename, "audio/mpeg",
                export_key=f"{job['id']}:copy:{asset_id}",
            )
            _mark_asset_uploaded(job, asset, uploaded)
        except Exception as e:
            with job["_lock"]:
                asset["drive_status"] = "failed"
                asset["drive_error"] = str(e)
                _persist_job(job)
            _add_warning(
                f"{label} Drive upload failed: {e} — a local download is available."
            )

    md = render_meta_copy_md(
        brief["client_name"], copy["angle"], copy["meta_primary_text"]
    )
    _save_and_upload_doc(
        "ad-copy", "Ad Copy (Meta primary text)", "ad-copy.md", "Ad Copy",
        md, run_folder_id,
    )

    landing_md = render_landing_page_prompt(
        client_name=brief["client_name"],
        website=brief["website"],
        meta_primary_text=copy["meta_primary_text"],
    )
    _save_and_upload_doc(
        "landing-page-prompt", "Landing Page Prompt",
        "landing-page-prompt.md", "Landing Page Prompt", landing_md,
        run_folder_id,
    )

    if brief["mode"] == "full" and videos_folder_id:
        def _push_script(kind_label: str, doc_name: str, body: str,
                         audio_filename: str, asset_slug: str):
            script_md = render_script_md(
                brief["client_name"], kind_label, doc_name, body, copy["angle"]
            )
            _save_and_upload_doc(
                f"{asset_slug}-script", doc_name, f"{asset_slug}-script.md",
                doc_name, script_md, videos_folder_id, kind="script",
            )
            try:
                audio = generate_voiceover(body, voice_id=DEFAULT_VOICE_ID)
                _save_and_upload_audio(
                    f"{asset_slug}-voiceover", f"{doc_name} (voiceover)",
                    audio_filename, audio, videos_folder_id,
                )
            except Exception as e:
                _add_warning(f"Voiceover for {doc_name} failed: {e}")

        for idx, script in enumerate(copy["brainrot_scripts"], start=1):
            title = script.get("title", f"Brainrot {idx}")
            slug = _slugify(title, fallback=f"brainrot-{idx}")
            _push_script(
                kind_label=f"Brainrot Script {idx}",
                doc_name=f"Brainrot {idx} - {title}",
                body=script.get("body", ""),
                audio_filename=f"voiceover_brainrot_{idx}_{slug}.mp3",
                asset_slug=f"brainrot-{idx}",
            )

        story = copy["story_script"]
        story_title = story.get("title", "Story")
        _push_script(
            kind_label="Story Script",
            doc_name=f"Story Script - {story_title}",
            body=story.get("body", ""),
            audio_filename=f"voiceover_story_{_slugify(story_title, 'story')}.mp3",
            asset_slug="story",
        )

        if copy["story_image_prompts"]:
            b_roll_md = render_story_b_roll_md(
                brief["client_name"], story_title,
                copy["story_image_prompts"],
            )
            _save_and_upload_doc(
                "story-b-roll", "Story B-Roll Image Prompts",
                "story-b-roll-prompts.md", "Story B-Roll Image Prompts",
                b_roll_md, videos_folder_id,
            )

    with job["_lock"]:
        copy_state["angle"] = copy.get("angle", "")
        copy_state["status"] = "done"
        _persist_job(job)


def _run_copy_pipeline_guarded(job: dict, connection_id: str,
                               run_folder_id: str,
                               videos_folder_id: str | None):
    try:
        _run_copy_pipeline(
            job, connection_id, run_folder_id, videos_folder_id
        )
    except Exception as error:
        log.error("Copy pipeline failed:\n%s", traceback.format_exc())
        with job["_lock"]:
            job["copy"]["error"] = str(error)
            job["copy"]["status"] = "failed"
            job["warnings"].append(f"Ad copy pipeline failed: {error}")
            _persist_job(job)


def _run_one_image(job: dict, item: dict, images_folder_id: str = "",
                   connection_id: str = ""):
    if SHUTTING_DOWN.is_set() or job["_cancel"].is_set():
        with job["_lock"]:
            item["status"] = "failed"
            item["error"] = ("Cancelled" if job["_cancel"].is_set()
                             else "Server shut down before this image started.")
        return

    brief = job["_brief"]
    prompt = job["_prompts"][item["idx"]]

    refs = [brief["system_path"]]
    if brief["logo_path"] and brief["logo_mode"] == "ai":
        refs.append(brief["logo_path"])
    elif (brief["logo_path"] and brief["logo_mode"] == "overlay"
          and STYLES[item["style_key"]].family == "designed"):
        # Designed styles key their palette to the logo — supply it as a
        # colour reference even though it gets composited afterwards.
        refs.append(brief["logo_path"])

    with job["_lock"]:
        item["status"] = "generating"

    try:
        img_bytes = generate_image(prompt, refs, quality=brief["quality"])
    except Exception as e:
        log.error("Image %d failed: %s", item["idx"] + 1, e)
        with job["_lock"]:
            item["status"] = "failed"
            item["error"] = str(e)
            job["warnings"].append(
                f"Image {item['idx'] + 1} ({item['style_label']}) failed: {e}")
        return

    if brief["logo_mode"] == "overlay" and brief["logo_path"]:
        try:
            img_bytes = composite_logo(img_bytes, brief["logo_path"])
        except Exception as e:
            with job["_lock"]:
                job["warnings"].append(
                    f"Logo overlay failed for image {item['idx'] + 1}: {e}")

    item_dir = STORE.batch_dir(job["id"]) / "items" / item["item_id"]
    item_dir.mkdir(parents=True, exist_ok=True)
    image_path = item_dir / "v1.png"
    image_path.write_bytes(img_bytes)

    # Kept as a hot cache; the durable file is the source of truth.
    job["_full_images"][item["idx"]] = img_bytes
    with job["_lock"]:
        item["local_image"] = True
        item["current_file"] = str(image_path)
        item["sha256"] = _sha256(img_bytes)
        item["version"] = 1
        item["changed"] = False

    try:
        job["_thumbs"][item["idx"]] = make_thumbnail(img_bytes)
        with job["_lock"]:
            item["has_thumb"] = True
    except Exception:
        pass

    if not images_folder_id:
        with job["_lock"]:
            item["status"] = "done"
            item["drive_status"] = "skipped"
            _persist_job(job)
        return

    with job["_lock"]:
        item["status"] = "uploading"
        item["drive_status"] = "uploading"
    filename = f"{item['idx'] + 1:02d}_{item['style_key']}.png"
    try:
        uploaded = DRIVE.upload_image(
            connection_id, images_folder_id, img_bytes, filename,
            export_key=f"{job['id']}:image:{item['item_id']}:v1",
        )
        with job["_lock"]:
            item["drive_link"] = uploaded.get("webViewLink", "")
            item["drive_file_id"] = uploaded.get("id", "")
            item["drive_status"] = "done"
            item["status"] = "done"
            _persist_job(job)
    except Exception as e:
        # Generation succeeded and the durable local file is the source of
        # truth. A Drive outage must not turn a paid-for image into a failure.
        with job["_lock"]:
            item["status"] = "done"
            item["local_image"] = True
            item["drive_status"] = "failed"
            item["drive_error"] = str(e)
            job["warnings"].append(
                f"Upload failed for image {item['idx'] + 1}: {e} — the "
                f"image is still downloadable from its tile.")
            _persist_job(job)


def _run_job(job: dict, folder_id: str = "", connection_id: str = ""):
    brief = job["_brief"]

    def _set(**fields):
        with job["_lock"]:
            job.update(fields)

    try:
        run_folder_id = ""
        images_folder_id = ""
        videos_folder_id = None
        if folder_id:
            _set(phase="Creating Drive folders")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_folder_name = f"{_slugify(brief['client_name'])}_{timestamp}"
            run_folder = DRIVE.create_subfolder(
                connection_id, folder_id, run_folder_name,
                export_key=f"{job['id']}:folder:run",
            )
            run_folder_id = run_folder["id"]
            images_folder_id = DRIVE.create_subfolder(
                connection_id, run_folder_id, "Images",
                export_key=f"{job['id']}:folder:images",
            )["id"]
            job["_images_folder_id"] = images_folder_id
            job["_drive_connection_id"] = connection_id
            if brief["mode"] == "full":
                videos_folder_id = DRIVE.create_subfolder(
                    connection_id, run_folder_id, "Videos",
                    export_key=f"{job['id']}:folder:videos",
                )["id"]

            _set(run_folder_link=run_folder.get("webViewLink", ""),
                 run_folder_name=run_folder_name)

            try:
                sheet = _render_prompt_sheet(
                    brief, job["_prompts"], job["items"])
                uploaded = DRIVE.upload_doc(
                    connection_id, run_folder_id, sheet, "Image Prompts",
                    export_key=f"{job['id']}:prompt-sheet",
                )
                _set(prompt_doc_link=uploaded.get("webViewLink", ""))
            except Exception as e:
                with job["_lock"]:
                    job["warnings"].append(
                        f"Prompt sheet upload failed: {e}")
        else:
            _set(phase="Preparing local downloads",
                 run_folder_name="")

        copy_thread = None
        if brief["mode"] != "images":
            copy_thread = threading.Thread(
                target=_run_copy_pipeline_guarded,
                args=(job, connection_id, run_folder_id, videos_folder_id),
                daemon=True,
            )
            copy_thread.start()

        _set(phase="Generating images")
        with ThreadPoolExecutor(max_workers=IMAGE_CONCURRENCY) as pool:
            futures = [
                pool.submit(
                    _run_one_image, job, item, images_folder_id, connection_id
                )
                for item in job["items"]
            ]
            for f in futures:
                f.result()

        if copy_thread:
            _set(phase="Finishing ad copy & scripts")
            copy_thread.join()

        with job["_lock"]:
            job["_main_done"] = True
            done_images = sum(1 for i in job["items"] if i["status"] == "done")
            copy_ok = job["copy"]["status"] in {"done", "skipped"}
            cancelled = job["_cancel"].is_set()
            if job["_refines"] > 0:
                job["phase"] = "Refining"       # a refine keeps the job live
            elif done_images == 0 and not (copy_ok and job["copy"]["links"]):
                job["error"] = ("Run cancelled before anything finished."
                                if cancelled else
                                "Nothing was generated — see warnings.")
                job["status"] = "error"
                job["phase"] = "Cancelled" if cancelled else "Complete"
                job["_finished_at"] = time.time()
            else:
                job["status"] = "done"
                job["phase"] = "Cancelled" if cancelled else "Complete"
                job["_finished_at"] = time.time()
    except Exception as e:
        log.error("Job %s crashed:\n%s", job["id"], traceback.format_exc())
        with job["_lock"]:
            job["_main_done"] = True
            for item in job["items"]:
                if item["status"] not in {"done", "failed"}:
                    item["status"] = "failed"
                    item["error"] = item["error"] or str(e)
            if job["copy"]["status"] in {"pending", "running"}:
                job["copy"]["error"] = job["copy"]["error"] or str(e)
                job["copy"]["status"] = "failed"
            job["error"] = str(e)
            job["status"] = "error"
            job["phase"] = "Failed"
            job["_finished_at"] = time.time()
    finally:
        _persist_job(job)
        _cleanup_brief_uploads(brief)


# ── Durable batch revisions ────────────────────────────────────────────────────

REVISION_LOCKS: dict[str, threading.Lock] = {}
REVISION_LOCKS_GUARD = threading.Lock()


def _revision_lock(batch_id: str) -> threading.Lock:
    with REVISION_LOCKS_GUARD:
        return REVISION_LOCKS.setdefault(batch_id, threading.Lock())


def _service_caller() -> bool:
    header = request.headers.get("Authorization", "")
    supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
    return bool(
        SERVICE_API_KEY and supplied
        and hmac.compare_digest(supplied, SERVICE_API_KEY)
    )


def _revision_authorized(service_endpoint: bool) -> bool:
    if service_endpoint:
        return _service_caller()
    return bool(session.get("ui_access"))


def _revision_error(message: str, code: str, status: int):
    return jsonify({"error": message, "code": code}), status


def _revision_public_items(job: dict) -> list[dict]:
    return [
        {
            "item_id": item["item_id"],
            "position": item["position"],
            "style_id": item["style_key"],
            "style_label": item["style_label"],
            "changed": bool(item.get("changed")),
            "version": item.get("version", 1),
            "image_url": (
                f"/api/jobs/{job['id']}/image/{item['idx']}"
                if item.get("current_file") else ""
            ),
            "sha256": item.get("sha256", ""),
            "parent_item_id": item.get("parent_item_id"),
            "parent_version": item.get("parent_version"),
        }
        for item in sorted(job["items"], key=lambda value: value["position"])
    ]


def _revision_prompt(revision: dict, item: dict) -> str:
    replacements = revision.get("text_replacements", [])
    if revision["mode"] == "replace_equipment":
        return (
            "Edit the first reference, which is a finished HVAC advertising "
            "creative. Replace only the HVAC equipment shown with the HVAC "
            "equipment in the second reference. Preserve the composition, "
            "background, client logo, offer, headline, subheadline, feature "
            "copy, exclusions, colours, spacing, and every other visible "
            "element as closely as possible. Preserve all advertising wording "
            "exactly. Do not add, remove, or rewrite any advertising text, "
            "headline, badge, feature, manufacturer name, or model name. The "
            "equipment reference is visual only. Square 1:1, English only."
        )

    parts = [
        "Edit the first reference, which is the existing finished HVAC ad.",
        f"Requested edit: {revision['instruction']}",
        "Preserve everything not explicitly requested: the existing HVAC "
        "equipment, client logo, offer wording, all other text, background, "
        "layout, colours, and overall design. Do not introduce any additional "
        "headline, subheadline, badge, feature, manufacturer, or model copy.",
    ]
    if revision.get("reference_manifest"):
        descriptions = []
        for entry in revision["reference_manifest"]:
            descriptions.append(
                f"reference {entry['file_index'] + 2} is "
                f"{entry['role']} labelled {entry.get('label') or 'unlabelled'}"
            )
        parts.append(
            "Use the additional visual references only for their stated roles: "
            + "; ".join(descriptions) + ". A supporting product must be added "
            "naturally and must never replace the HVAC equipment."
        )
    for replacement in replacements:
        parts.append(
            f'Replace only the exact text "{replacement["from"]}" with '
            f'"{replacement["to"]}". Preserve all other text exactly.'
        )
    parts.append("Square 1:1 aspect ratio, fully in English.")
    return "\n".join(parts)


def _run_revision_item(job: dict, revision: dict, item: dict) -> None:
    source_path = Path(item.get("current_file", ""))
    if not source_path.is_file():
        raise RuntimeError(f"Source image {item['position']} is unavailable.")
    source_version = item.get("version", 1)
    refs: list[str | bytes] = [str(source_path)]
    if revision["mode"] == "replace_equipment":
        refs.append(revision["equipment_path"])
    else:
        refs.extend(revision.get("reference_paths", []))

    prompt = _revision_prompt(revision, item)
    revision.setdefault("prompts", {})[item["item_id"]] = prompt
    with job["_lock"]:
        item["status"] = "generating"
        item["error"] = ""
    image_bytes = generate_image(
        prompt, refs, quality=revision["quality"]
    )
    version = source_version + 1
    item_dir = STORE.batch_dir(job["id"]) / "items" / item["item_id"]
    item_dir.mkdir(parents=True, exist_ok=True)
    image_path = item_dir / f"v{version}.png"
    image_path.write_bytes(image_bytes)

    drive_link = item.get("drive_link", "")
    drive_file_id = item.get("drive_file_id", "")
    drive_status = "skipped"
    drive_error = ""
    if job.get("_images_folder_id"):
        with job["_lock"]:
            item["status"] = "uploading"
            item["drive_status"] = "uploading"
        filename = (
            f"{item['position']:02d}_{item['style_key']}_v{version}.png"
        )
        try:
            uploaded = DRIVE.upload_image(
                job.get("_drive_connection_id", ""),
                job["_images_folder_id"], image_bytes, filename,
                export_key=(
                    f"{job['id']}:image:{item['item_id']}:v{version}"
                ),
            )
            drive_link = uploaded.get("webViewLink", "")
            drive_file_id = uploaded.get("id", "")
            drive_status = "done"
        except Exception as error:
            drive_link = ""
            drive_file_id = ""
            drive_status = "failed"
            drive_error = str(error)
            with job["_lock"]:
                job["warnings"].append(
                    f"Revision image {item['position']} Drive upload failed: "
                    f"{error} — the revision is still available locally."
                )

    with job["_lock"]:
        item.update({
            "status": "done",
            "local_image": True,
            "has_thumb": True,
            "current_file": str(image_path),
            "sha256": _sha256(image_bytes),
            "version": version,
            "parent_item_id": item["item_id"],
            "parent_version": source_version,
            "changed": True,
            "drive_link": drive_link,
            "drive_file_id": drive_file_id,
            "drive_status": drive_status,
            "drive_error": drive_error,
        })
        job["_full_images"][item["idx"]] = image_bytes
        try:
            job["_thumbs"][item["idx"]] = make_thumbnail(image_bytes)
        except Exception:
            pass


def _run_revision(job: dict, revision: dict) -> None:
    with _revision_lock(job["id"]):
        revision["status"] = "running"
        STORE.save_revision(revision)
        with job["_lock"]:
            job["status"] = "running"
            job["phase"] = (
                "Replacing equipment"
                if revision["mode"] == "replace_equipment"
                else "Editing selected images"
            )
            job["error"] = ""
            for item in job["items"]:
                item["changed"] = False
        _persist_job(job)

        selected = (
            list(job["items"])
            if revision["mode"] == "replace_equipment"
            else [
                item for item in job["items"]
                if item["item_id"] in revision["selected_item_ids"]
            ]
        )
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=IMAGE_CONCURRENCY) as pool:
            futures = {
                pool.submit(_run_revision_item, job, revision, item): item
                for item in selected
            }
            for future, item in futures.items():
                try:
                    future.result()
                except Exception as exc:
                    log.error(
                        "Revision %s image %s failed: %s",
                        revision["revision_id"], item["position"], exc,
                    )
                    errors.append(f"Image {item['position']}: {exc}")
                    with job["_lock"]:
                        item["status"] = "done"
                        item["error"] = str(exc)
                        item["changed"] = False

        if revision["mode"] == "replace_equipment" and not errors:
            with job["_lock"]:
                job["_brief"]["active_equipment_path"] = revision[
                    "equipment_path"
                ]
                job["_brief"]["system_path"] = revision["equipment_path"]

        revision["status"] = "error" if errors else "done"
        revision["error"] = "; ".join(errors)
        revision["items"] = _revision_public_items(job)
        with job["_lock"]:
            job["status"] = "error" if errors else "done"
            job["phase"] = "Revision failed" if errors else "Complete"
            job["error"] = revision["error"]
            job["_finished_at"] = time.time()
            for entry in job["revision_history"]:
                if entry["revision_id"] == revision["revision_id"]:
                    entry["status"] = revision["status"]
                    entry["error"] = revision["error"]
                    break
        _persist_job(job)
        STORE.save_revision(revision)


def _parse_json_field(name: str, default):
    raw = request.form.get(name, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise ValueError(f"{name} must be valid JSON.")


def _save_revision_upload(upload, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    upload.save(path)
    return str(path)


def _queue_revision(
    job: dict, *, mode: str, selected_item_ids: list[str],
    instruction: str, quality: str, idempotency_key: str,
    equipment_upload=None, reference_uploads=None,
    reference_manifest=None, text_replacements=None,
) -> tuple[dict, bool]:
    revision_id = f"rev_{uuid.uuid4().hex[:16]}"
    revision_dir = STORE.revision_dir(job["id"], revision_id)
    equipment_path = ""
    equipment_name = ""
    if equipment_upload:
        suffix = Path(equipment_upload.filename).suffix.lower()
        equipment_path = _save_revision_upload(
            equipment_upload, revision_dir / f"equipment{suffix}"
        )
        equipment_name = _upload_name(equipment_upload, "HVAC equipment")

    reference_paths = []
    for index, upload in enumerate(reference_uploads or []):
        suffix = Path(upload.filename).suffix.lower()
        reference_paths.append(_save_revision_upload(
            upload, revision_dir / f"reference-{index}{suffix}"
        ))

    revision = {
        "revision_id": revision_id,
        "batch_id": job["id"],
        "status": "queued",
        "mode": mode,
        "selected_item_ids": selected_item_ids,
        "instruction": instruction,
        "quality": quality,
        "equipment_path": equipment_path,
        "equipment_name": equipment_name,
        "reference_paths": reference_paths,
        "reference_manifest": reference_manifest or [],
        "text_replacements": text_replacements or [],
        "idempotency_key": idempotency_key,
        "created_at": datetime.now().astimezone().isoformat(),
        "error": "",
        "items": _revision_public_items(job),
        "prompts": {},
    }
    revision, created = STORE.create_revision(revision)
    if not created:
        return revision, False

    with job["_lock"]:
        job["revision_history"].append({
            "revision_id": revision["revision_id"],
            "status": "queued",
            "mode": mode,
            "created_at": revision["created_at"],
            "error": "",
        })
        job["status"] = "running"
        job["phase"] = "Revision queued"
        job["error"] = ""
        _persist_job(job)
    threading.Thread(
        target=_run_revision, args=(job, revision), daemon=True
    ).start()
    return revision, True


@app.route("/api/jobs/<job_id>/revisions", methods=["POST"])
@app.route("/api/ui/jobs/<job_id>/revisions", methods=["POST"])
def api_create_revision(job_id):
    service_endpoint = not request.path.startswith("/api/ui/")
    if not _revision_authorized(service_endpoint):
        return _revision_error(
            (
                "Use a valid service bearer token."
                if service_endpoint else "Open the application before editing."
            ),
            "unauthorized", 401,
        )
    job = _get_job(job_id)
    if not job:
        return _revision_error("Unknown job.", "unknown_job", 404)
    if not _key_ok("OPENAI_API_KEY"):
        return _revision_error(
            "OPENAI_API_KEY is missing from .env.", "missing_api_key", 400
        )

    mode = request.form.get("mode", "").strip()
    if mode not in {"replace_equipment", "edit_selected"}:
        return _revision_error(
            "mode must be replace_equipment or edit_selected.",
            "invalid_mode", 400,
        )
    try:
        selected_ids = _parse_json_field("selected_item_ids", [])
        manifest = _parse_json_field("reference_manifest", [])
        replacements = _parse_json_field("text_replacements", [])
    except ValueError as exc:
        return _revision_error(str(exc), "invalid_json", 400)
    if not isinstance(selected_ids, list) or not all(
        isinstance(value, str) for value in selected_ids
    ):
        return _revision_error(
            "selected_item_ids must be a JSON array of strings.",
            "invalid_selection", 400,
        )
    valid_ids = {item["item_id"] for item in job["items"]}
    if mode == "edit_selected" and (
        not selected_ids or not set(selected_ids).issubset(valid_ids)
    ):
        return _revision_error(
            "Select one or more valid image item IDs.",
            "invalid_selection", 400,
        )

    equipment_upload = request.files.get("equipment_file")
    if mode == "replace_equipment":
        error = _validate_image_upload(
            equipment_upload, "Replacement equipment image", required=True
        )
        if error:
            return _revision_error(error, "invalid_equipment", 400)

    reference_uploads = [
        upload for upload in request.files.getlist("reference_files")
        if (upload.filename or "").strip()
    ]
    if len(reference_uploads) > 8:
        return _revision_error(
            "A revision can include at most eight reference images.",
            "too_many_references", 400,
        )
    for upload in reference_uploads:
        error = _validate_image_upload(upload, "Reference image")
        if error:
            return _revision_error(error, "invalid_reference", 400)
    if not isinstance(manifest, list) or len(manifest) != len(reference_uploads):
        return _revision_error(
            "reference_manifest must contain one entry per reference file.",
            "invalid_manifest", 400,
        )
    for entry in manifest:
        if (
            not isinstance(entry, dict)
            or entry.get("role") not in REFERENCE_ROLES
            or not isinstance(entry.get("file_index"), int)
            or not 0 <= entry["file_index"] < len(reference_uploads)
        ):
            return _revision_error(
                "Every reference manifest entry needs a valid file_index and role.",
                "invalid_manifest", 400,
            )
    if {entry["file_index"] for entry in manifest} != set(
        range(len(reference_uploads))
    ):
        return _revision_error(
            "reference_manifest must describe each reference file exactly once.",
            "invalid_manifest", 400,
        )
    if not isinstance(replacements, list) or any(
        not isinstance(entry, dict)
        or not isinstance(entry.get("from"), str)
        or not entry.get("from")
        or not isinstance(entry.get("to"), str)
        for entry in replacements
    ):
        return _revision_error(
            "text_replacements must contain non-empty from/to strings.",
            "invalid_text_replacements", 400,
        )

    instruction = request.form.get("instruction", "").strip()
    if mode == "edit_selected" and not instruction and not replacements:
        return _revision_error(
            "Provide an instruction or an exact text replacement.",
            "missing_instruction", 400,
        )
    quality = request.form.get("quality", job["_brief"]["quality"]).strip()
    if service_endpoint:
        quality = "low"
    if quality not in QUALITIES:
        return _revision_error(
            "quality must be low, medium, or high.", "invalid_quality", 400
        )
    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if not idempotency_key:
        return _revision_error(
            "Idempotency-Key header is required.", "missing_idempotency_key", 400
        )
    if len(idempotency_key) > 200:
        return _revision_error(
            "Idempotency-Key is too long.", "invalid_idempotency_key", 400
        )

    revision, _created = _queue_revision(
        job,
        mode=mode,
        selected_item_ids=selected_ids,
        instruction=instruction,
        quality=quality,
        idempotency_key=idempotency_key,
        equipment_upload=equipment_upload,
        reference_uploads=reference_uploads,
        reference_manifest=manifest,
        text_replacements=replacements,
    )
    return jsonify({
        "revision_id": revision["revision_id"],
        "batch_id": job["id"],
        "status": revision["status"],
        "status_url": (
            f"/api/revisions/{revision['revision_id']}"
            if service_endpoint else
            f"/api/ui/revisions/{revision['revision_id']}"
        ),
    }), 202


@app.route("/api/revisions/<revision_id>")
@app.route("/api/ui/revisions/<revision_id>")
def api_revision(revision_id):
    service_endpoint = not request.path.startswith("/api/ui/")
    if not _revision_authorized(service_endpoint):
        return _revision_error(
            (
                "Use a valid service bearer token."
                if service_endpoint else "Open the application before editing."
            ),
            "unauthorized", 401,
        )
    revision = STORE.load_revision(revision_id)
    if not revision:
        return _revision_error("Unknown revision.", "unknown_revision", 404)
    return jsonify({
        "revision_id": revision["revision_id"],
        "batch_id": revision["batch_id"],
        "status": revision["status"],
        "error": revision.get("error", ""),
        "items": revision.get("items", []),
    })


@app.route("/api/jobs/<job_id>/refine/<int:idx>", methods=["POST"])
def api_job_refine(job_id, idx):
    """Backward-compatible text-only refine routed through revisions."""
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    data = request.get_json(silent=True) or {}
    instruction = (data.get("instruction") or "").strip()
    if not instruction:
        return jsonify({"error": "Type what you want changed."}), 400
    if not 0 <= idx < len(job["items"]):
        return jsonify({"error": "That image is unavailable."}), 400
    revision, _created = _queue_revision(
        job,
        mode="edit_selected",
        selected_item_ids=[job["items"][idx]["item_id"]],
        instruction=instruction,
        quality=job["_brief"]["quality"],
        idempotency_key=f"legacy-{uuid.uuid4().hex}",
    )
    return jsonify({
        "job_id": job["id"],
        "idx": idx,
        "revision_id": revision["revision_id"],
    }), 202


# ── Generate API ───────────────────────────────────────────────────────────────

@app.route("/api/generate", methods=["POST"])
def api_generate():
    brief, err = _parse_brief(request.form, request.files)
    if err:
        return jsonify({"error": err}), 400

    # Preflight the keys each mode needs BEFORE any money is spent —
    # otherwise images generate and the copy/voiceover legs fail later.
    if not _key_ok("OPENAI_API_KEY"):
        return jsonify({"error": "OPENAI_API_KEY is missing from .env — "
                                 "add it and restart the app."}), 400
    if brief["mode"] != "images" and not _key_ok("ANTHROPIC_API_KEY"):
        return jsonify({"error": "ANTHROPIC_API_KEY is missing from .env — "
                                 "it's required for ad copy. Add it and "
                                 "restart, or switch to Images only."}), 400
    if brief["mode"] == "full" and not _key_ok("ELEVENLABS_API_KEY"):
        return jsonify({"error": "ELEVENLABS_API_KEY is missing from .env — "
                                 "it's required for voiceovers. Add it and "
                                 "restart, or use Images + Ad Copy."}), 400

    folder_id = request.form.get("folder_id", "").strip()
    folder_name = ""
    connection_id = _drive_connection_id()
    if brief["mode"] != "images" and not folder_id:
        return jsonify({
            "error": "Connect Google Drive and pick a folder for ad copy "
                     "or the full pipeline. Images only can run without Drive."
        }), 400
    if folder_id and not drive_is_available():
        return jsonify({"error": "Google Drive not connected. Click "
                                 "'Connect Google Drive' first."}), 401
    if folder_id:
        try:
            folder = DRIVE.validate_folder(connection_id, folder_id)
            folder_name = folder["name"]
        except DriveNotConnected as e:
            session.pop("drive_connection_id", None)
            return jsonify({"error": str(e)}), 401
        except DriveFolderError as e:
            return jsonify({"error": str(e)}), 400
        except DriveError as e:
            return jsonify({"error": str(e)}), 502

    err = _persist_brief_uploads(brief, request.files)
    if err:
        return jsonify({"error": err}), 400

    items = _build_all_prompts(brief)
    job = _new_job(brief, items)
    job["folder_name"] = (folder_name or "Google Drive"
                          if folder_id else "Local downloads")
    job["warnings"].extend(_brief_warnings(brief))
    _persist_job(job)

    threading.Thread(
        target=_run_job, args=(job, folder_id, connection_id), daemon=True
    ).start()
    return jsonify({"job_id": job["id"], "batch_id": job["id"]})


@app.route("/api/jobs")
def api_jobs():
    try:
        limit = int(request.args.get("limit", "20"))
    except ValueError:
        limit = 20
    return jsonify({"jobs": STORE.list_batches(limit)})


@app.route("/api/jobs/<job_id>")
def api_job(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    with job["_lock"]:
        return jsonify(_public_job(job))


@app.route("/api/jobs/<job_id>/thumb/<int:idx>")
def api_job_thumb(job_id, idx):
    job = _get_job(job_id)
    if not job or not 0 <= idx < len(job["items"]):
        return jsonify({"error": "No thumbnail."}), 404
    if idx not in job["_thumbs"]:
        current = Path(job["items"][idx].get("current_file", ""))
        if not current.is_file():
            return jsonify({"error": "No thumbnail."}), 404
        try:
            job["_thumbs"][idx] = make_thumbnail(current.read_bytes())
        except Exception:
            return jsonify({"error": "No thumbnail."}), 404
    # Long cache: thumbnail bytes are immutable per (job, idx).
    return send_file(io.BytesIO(job["_thumbs"][idx]), mimetype="image/jpeg",
                     max_age=0)


@app.route("/api/jobs/<job_id>/image/<int:idx>")
def api_job_image(job_id, idx):
    """Download any generated image at full resolution."""
    job = _get_job(job_id)
    if not job or not 0 <= idx < len(job["items"]):
        return jsonify({"error": "No image."}), 404
    item = job["items"][idx]
    image_bytes = job["_full_images"].get(idx)
    if image_bytes is None:
        current = Path(item.get("current_file", ""))
        if not current.is_file():
            return jsonify({"error": "No image."}), 404
        image_bytes = current.read_bytes()
    return send_file(
        io.BytesIO(image_bytes), mimetype="image/png",
        as_attachment=True,
        download_name=(
            f"{item.get('position', idx + 1):02d}_{item['style_key']}"
            f"_v{item.get('version', 1)}.png"
        ),
    )


@app.route("/api/jobs/<job_id>/images.zip")
def api_job_images_zip(job_id):
    """Download the latest available version of every image in one ZIP."""
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404

    with job["_lock"]:
        available = []
        for item in job["items"]:
            source = Path(item.get("current_file", ""))
            if not source.is_file():
                continue
            available.append({
                "source": source,
                "position": int(item.get("position", item["idx"] + 1)),
                "style_key": item["style_key"],
                "version": int(item.get("version", 1)),
                "sha256": item.get("sha256", ""),
            })

    if len(available) < 2:
        return jsonify({
            "error": "At least two completed images are required for Download all."
        }), 400

    manifest = "|".join(
        f"{entry['position']}:{entry['version']}:{entry['sha256']}"
        for entry in available
    )
    digest = hashlib.sha256(manifest.encode("utf-8")).hexdigest()[:16]
    download_dir = STORE.batch_dir(job_id) / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    archive_path = download_dir / f"images-{digest}.zip"

    if not archive_path.is_file():
        temporary_path = download_dir / f".{archive_path.name}.{uuid.uuid4().hex}.tmp"
        try:
            with zipfile.ZipFile(temporary_path, "w", zipfile.ZIP_STORED) as archive:
                for entry in available:
                    archive.write(
                        entry["source"],
                        arcname=(
                            f"{entry['position']:02d}_{entry['style_key']}"
                            f"_v{entry['version']}.png"
                        ),
                    )
            os.replace(temporary_path, archive_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    return send_file(
        archive_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{_slugify(job['client_name'])}_images.zip",
        conditional=True,
    )


@app.route("/api/jobs/<job_id>/assets/<asset_id>")
def api_job_asset(job_id, asset_id):
    """Download generated ad copy, scripts, or voiceovers from local storage."""
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    asset = next(
        (entry for entry in job["copy"].get("assets", [])
         if entry.get("asset_id") == asset_id),
        None,
    )
    if not asset:
        return jsonify({"error": "Unknown asset."}), 404
    export_dir = (STORE.batch_dir(job_id) / "exports").resolve()
    path = (export_dir / asset["filename"]).resolve()
    if path.parent != export_dir or not path.is_file():
        return jsonify({"error": "Asset is unavailable."}), 404
    return send_file(
        path,
        mimetype=asset.get("mime_type", "application/octet-stream"),
        as_attachment=True,
        download_name=asset["filename"],
    )


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def api_job_cancel(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    job["_cancel"].set()
    with job["_lock"]:
        if job["status"] == "running":
            job["phase"] = "Cancelling — finishing in-flight images"
        _persist_job(job)
    return jsonify({"ok": True})


if __name__ == "__main__":
    import webbrowser
    url = "http://127.0.0.1:5000"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        app.run(debug=True, use_reloader=False, port=5000)
    except OSError:
        # Most likely a second launch while the app is already running.
        print(f"\nHVAC Marketer is already running (or port 5000 is taken).")
        print(f"Opening the existing app at {url}\n")
    finally:
        # Lets queued image workers exit as fast no-ops instead of the
        # executor draining the whole backlog during interpreter shutdown.
        SHUTTING_DOWN.set()
