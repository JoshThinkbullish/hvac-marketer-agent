import io
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, UnidentifiedImageError
from flask import (
    Flask, jsonify, redirect, render_template, request, send_file, session,
    url_for,
)

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
    TOKEN_PATH,
    create_subfolder,
    get_folders,
    is_available as drive_is_available,
    upload_bytes,
    upload_doc,
    upload_to_folder,
)
from agent.image_generator import (
    composite_logo,
    generate_image,
    make_thumbnail,
)
from agent.voice_generator import DEFAULT_VOICE_ID, generate_voiceover

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-me")

# Browser uploads supported by Pillow and the image API.
SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
SUPPORTED_FORMATS = {"PNG", "JPEG", "WEBP"}
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_UPLOAD_PIXELS = 50_000_000
app.config["MAX_CONTENT_LENGTH"] = (MAX_UPLOAD_BYTES * 2) + (2 * 1024 * 1024)

MAX_IMAGES = 20
IMAGE_CONCURRENCY = 4
QUALITIES = {"low", "medium", "high"}
MODES = {"images", "images_copy", "full"}


@app.errorhandler(413)
def upload_too_large(_error):
    return jsonify({
        "error": "Uploads are too large. Keep each image at 15 MB or less."
    }), 413


def _key_ok(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def _slugify(value: str, fallback: str = "client") -> str:
    cleaned = re.sub(r"[^\w\s-]", "", value or "").strip().lower()
    cleaned = re.sub(r"[\s_-]+", "-", cleaned)
    return cleaned or fallback


def _lines(raw: str) -> list[str]:
    return [l.strip() for l in (raw or "").splitlines() if l.strip()]


# ── Main form ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template(
        "index.html",
        styles=list(STYLES.values()),
        settings=SETTINGS,
        default_styles=DEFAULT_STYLE_KEYS,
        max_images=MAX_IMAGES,
        connected=drive_is_available(),
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
    if not drive_is_available():
        return jsonify({"error": "Not connected to Google Drive"}), 401
    try:
        return jsonify({"folders": get_folders()})
    except Exception as e:
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
            "redirect_uris": [url_for("auth_callback", _external=True)],
        }},
        scopes=DRIVE_SCOPES,
        state=state,
        redirect_uri=url_for("auth_callback", _external=True),
    )


@app.route("/auth/google")
def auth_google():
    if not GOOGLE_CLIENT_ID:
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
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    # Google may return more scopes than requested (previously granted);
    # don't hard-fail the exchange over it.
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    flow = _make_flow(state=session.get("oauth_state"))
    try:
        flow.fetch_token(authorization_response=request.url)
    except Exception:
        log.warning("Google OAuth callback failed:\n%s", traceback.format_exc())
        return redirect(url_for("index", error="google_auth_failed"))
    session.pop("oauth_state", None)
    with open(TOKEN_PATH, "w") as f:
        f.write(flow.credentials.to_json())
    return redirect(url_for("index", connected="true"))


@app.route("/auth/disconnect")
def auth_disconnect():
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()
    return redirect(url_for("index"))


@app.route("/auth/status")
def auth_status():
    return jsonify({
        "connected": TOKEN_PATH.exists(),
        "creds_present": bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET),
    })


# ── Brief parsing (shared by preview + generate) ──────────────────────────────

def _validate_image_upload(upload, label: str, required: bool = False) -> str | None:
    """Validate a browser upload and return a friendly error, if any."""
    if not upload or not (upload.filename or "").strip():
        return f"Upload an {label.lower()}." if required else None

    suffix = Path(upload.filename).suffix.lower()
    if suffix not in SUPPORTED_EXTS:
        return f"{label} must be a PNG, JPG, JPEG, or WebP image."

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


def _new_job(brief: dict, items: list[dict]) -> dict:
    job = {
        "id": uuid.uuid4().hex[:12],
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
                "style_key": it["style_key"],
                "style_label": it["style_label"],
                "status": "queued",
                "error": "",
                "drive_link": "",
                "has_thumb": False,
                "local_image": False,
            }
            for it in items
        ],
        "copy": {
            "status": "skipped" if brief["mode"] == "images" else "pending",
            "links": [],
            "error": "",
            "angle": "",
        },
        "warnings": [],
        "error": "",
        "_brief": brief,
        "_prompts": {it["idx"]: it["prompt"] for it in items},
        "_thumbs": {},
        "_full_images": {},
        "_finished_at": 0.0,
        "_main_done": False,
        "_refines": 0,
        "_images_folder_id": "",
        "_cancel": threading.Event(),
        "_lock": threading.Lock(),
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
    return job


def _public_job(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


# ── Pipeline ───────────────────────────────────────────────────────────────────

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


def _run_copy_pipeline(job: dict, run_folder_id: str,
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

    links: list[dict] = []

    def _add_warning(msg: str):
        with job["_lock"]:
            job["warnings"].append(msg)

    try:
        md = render_meta_copy_md(brief["client_name"], copy["angle"],
                                 copy["meta_primary_text"])
        links.append({"label": "Ad Copy (Meta primary text)", "kind": "doc",
                      "url": upload_doc(run_folder_id, md, "Ad Copy")})
    except Exception as e:
        _add_warning(f"Ad Copy doc upload failed: {e}")

    try:
        landing_md = render_landing_page_prompt(
            client_name=brief["client_name"],
            website=brief["website"],
            meta_primary_text=copy["meta_primary_text"],
        )
        links.append({"label": "Landing Page Prompt", "kind": "doc",
                      "url": upload_doc(run_folder_id, landing_md,
                                        "Landing Page Prompt")})
    except Exception as e:
        _add_warning(f"Landing page prompt upload failed: {e}")

    if brief["mode"] == "full" and videos_folder_id:
        def _push_script(kind_label: str, doc_name: str, body: str,
                         audio_filename: str):
            try:
                md = render_script_md(brief["client_name"], kind_label,
                                      doc_name, body, copy["angle"])
                links.append({"label": doc_name, "kind": "script",
                              "url": upload_doc(videos_folder_id, md, doc_name)})
            except Exception as e:
                _add_warning(f"{doc_name} doc upload failed: {e}")
            try:
                audio = generate_voiceover(body, voice_id=DEFAULT_VOICE_ID)
                links.append({
                    "label": f"{doc_name} (voiceover)", "kind": "audio",
                    "url": upload_bytes(videos_folder_id, audio,
                                        audio_filename, mime_type="audio/mpeg"),
                })
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
            )

        story = copy["story_script"]
        story_title = story.get("title", "Story")
        _push_script(
            kind_label="Story Script",
            doc_name=f"Story Script - {story_title}",
            body=story.get("body", ""),
            audio_filename=f"voiceover_story_{_slugify(story_title, 'story')}.mp3",
        )

        if copy["story_image_prompts"]:
            try:
                b_roll_md = render_story_b_roll_md(
                    brief["client_name"], story_title,
                    copy["story_image_prompts"],
                )
                links.append({
                    "label": "Story B-Roll Image Prompts", "kind": "doc",
                    "url": upload_doc(videos_folder_id, b_roll_md,
                                      "Story B-Roll Image Prompts"),
                })
            except Exception as e:
                _add_warning(f"Story B-roll prompts upload failed: {e}")

    with job["_lock"]:
        copy_state["links"] = links
        copy_state["angle"] = copy.get("angle", "")
        copy_state["status"] = "done"


def _run_one_image(job: dict, item: dict, images_folder_id: str = ""):
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

    # Kept for click-to-refine and the download button on every image tile.
    job["_full_images"][item["idx"]] = img_bytes
    with job["_lock"]:
        item["local_image"] = True

    try:
        job["_thumbs"][item["idx"]] = make_thumbnail(img_bytes)
        with job["_lock"]:
            item["has_thumb"] = True
    except Exception:
        pass

    if not images_folder_id:
        with job["_lock"]:
            item["status"] = "done"
        return

    with job["_lock"]:
        item["status"] = "uploading"
    filename = f"{item['idx'] + 1:02d}_{item['style_key']}.png"
    try:
        link = upload_to_folder(images_folder_id, img_bytes, filename)
        with job["_lock"]:
            item["drive_link"] = link
            item["status"] = "done"
    except Exception as e:
        # The image is paid for and already kept in _full_images — surface
        # a download link instead of discarding it.
        with job["_lock"]:
            item["status"] = "failed"
            item["local_image"] = True
            item["error"] = f"Drive upload failed: {e}"
            job["warnings"].append(
                f"Upload failed for image {item['idx'] + 1}: {e} — the "
                f"image is still downloadable from its tile.")


def _run_job(job: dict, folder_id: str = ""):
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
            run_folder = create_subfolder(folder_id, run_folder_name)
            run_folder_id = run_folder["id"]
            images_folder_id = create_subfolder(
                run_folder_id, "Images")["id"]
            job["_images_folder_id"] = images_folder_id
            if brief["mode"] == "full":
                videos_folder_id = create_subfolder(
                    run_folder_id, "Videos")["id"]

            _set(run_folder_link=run_folder.get("webViewLink", ""),
                 run_folder_name=run_folder_name)

            try:
                sheet = _render_prompt_sheet(
                    brief, job["_prompts"], job["items"])
                link = upload_doc(run_folder_id, sheet, "Image Prompts")
                _set(prompt_doc_link=link)
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
                target=_run_copy_pipeline,
                args=(job, run_folder_id, videos_folder_id),
                daemon=True,
            )
            copy_thread.start()

        _set(phase="Generating images")
        with ThreadPoolExecutor(max_workers=IMAGE_CONCURRENCY) as pool:
            futures = [
                pool.submit(_run_one_image, job, item, images_folder_id)
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
        _cleanup_brief_uploads(brief)


# ── Refine ─────────────────────────────────────────────────────────────────────

def _finalize_refine(job: dict):
    with job["_lock"]:
        job["_refines"] -= 1
        if job["_refines"] <= 0 and job["_main_done"]:
            done_images = sum(1 for i in job["items"] if i["status"] == "done")
            job["status"] = "done" if done_images else "error"
            if not done_images and not job["error"]:
                job["error"] = "Nothing was generated — see warnings."
            job["phase"] = "Complete"
            job["_finished_at"] = time.time()


def _run_refine(job: dict, item: dict, source_idx: int):
    brief = job["_brief"]
    prompt = job["_prompts"][item["idx"]]
    src = job["_full_images"].get(source_idx)
    try:
        if src is None:
            raise RuntimeError("Source image is no longer available.")
        with job["_lock"]:
            item["status"] = "generating"
        img_bytes = generate_image(prompt, [src], quality=brief["quality"])
        job["_full_images"][item["idx"]] = img_bytes
        with job["_lock"]:
            item["local_image"] = True
        try:
            job["_thumbs"][item["idx"]] = make_thumbnail(img_bytes)
            with job["_lock"]:
                item["has_thumb"] = True
        except Exception:
            pass
        folder = job["_images_folder_id"]
        if folder:
            with job["_lock"]:
                item["status"] = "uploading"
            filename = f"{item['idx'] + 1:02d}_{item['style_key']}_refined.png"
            link = upload_to_folder(folder, img_bytes, filename)
            with job["_lock"]:
                item["drive_link"] = link
                item["status"] = "done"
        else:
            with job["_lock"]:
                item["local_image"] = True
                item["status"] = "done"
    except Exception as e:
        log.error("Refine of image %d failed: %s", source_idx + 1, e)
        with job["_lock"]:
            item["status"] = "failed"
            item["error"] = str(e)
            if item["idx"] in job["_full_images"]:
                item["local_image"] = True
            job["warnings"].append(
                f"Refine of image {source_idx + 1} failed: {e}")
    finally:
        _finalize_refine(job)


@app.route("/api/jobs/<job_id>/refine/<int:idx>", methods=["POST"])
def api_job_refine(job_id, idx):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    data = request.get_json(silent=True) or {}
    instruction = (data.get("instruction") or "").strip()
    if not instruction:
        return jsonify({"error": "Type what you want changed."}), 400
    if idx not in job["_full_images"]:
        return jsonify({"error": "That image isn't available to refine "
                                 "anymore."}), 400
    if not _key_ok("OPENAI_API_KEY"):
        return jsonify({"error": "OPENAI_API_KEY is missing from .env."}), 400

    src_item = job["items"][idx] if idx < len(job["items"]) else {}
    base_label = (src_item.get("style_label") or "Image").split(" · refined")[0]
    prompt = (
        f"Here is a finished HVAC ad image. Apply this revision: {instruction}\n"
        "Keep every other element of the image exactly the same — same "
        "layout, same text and offer wording, same colours, same HVAC unit, "
        "same logo. Square 1:1 aspect ratio, fully in English."
    )
    with job["_lock"]:
        new_idx = len(job["items"])
        item = {
            "idx": new_idx,
            "style_key": src_item.get("style_key", "refined"),
            "style_label": f"{base_label} · refined",
            "status": "queued",
            "error": "",
            "drive_link": "",
            "has_thumb": False,
            "local_image": False,
            "refine_of": idx,
        }
        job["items"].append(item)
        job["_prompts"][new_idx] = prompt
        job["_refines"] += 1
        job["status"] = "running"
        job["phase"] = f"Refining image {idx + 1}"
    threading.Thread(target=_run_refine, args=(job, item, idx),
                     daemon=True).start()
    return jsonify({"job_id": job["id"], "idx": new_idx})


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
    folder_name = request.form.get("folder_name", "").strip()
    if brief["mode"] != "images" and not folder_id:
        return jsonify({
            "error": "Connect Google Drive and pick a folder for ad copy "
                     "or the full pipeline. Images only can run without Drive."
        }), 400
    if folder_id and not drive_is_available():
        return jsonify({"error": "Google Drive not connected. Click "
                                 "'Connect Google Drive' first."}), 401

    err = _persist_brief_uploads(brief, request.files)
    if err:
        return jsonify({"error": err}), 400

    items = _build_all_prompts(brief)
    job = _new_job(brief, items)
    job["folder_name"] = (folder_name or "Google Drive"
                          if folder_id else "Local downloads")
    job["warnings"].extend(_brief_warnings(brief))

    threading.Thread(target=_run_job, args=(job, folder_id), daemon=True).start()
    return jsonify({"job_id": job["id"]})


@app.route("/api/jobs/<job_id>")
def api_job(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    with job["_lock"]:
        return jsonify(_public_job(job))


@app.route("/api/jobs/<job_id>/thumb/<int:idx>")
def api_job_thumb(job_id, idx):
    job = JOBS.get(job_id)
    if not job or idx not in job["_thumbs"]:
        return jsonify({"error": "No thumbnail."}), 404
    # Long cache: thumbnail bytes are immutable per (job, idx).
    return send_file(io.BytesIO(job["_thumbs"][idx]), mimetype="image/jpeg",
                     max_age=86400)


@app.route("/api/jobs/<job_id>/image/<int:idx>")
def api_job_image(job_id, idx):
    """Download any generated image at full resolution."""
    job = JOBS.get(job_id)
    if not job or idx not in job["_full_images"]:
        return jsonify({"error": "No image."}), 404
    item = job["items"][idx]
    return send_file(
        io.BytesIO(job["_full_images"][idx]), mimetype="image/png",
        as_attachment=True,
        download_name=f"{idx + 1:02d}_{item['style_key']}.png",
    )


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def api_job_cancel(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    job["_cancel"].set()
    with job["_lock"]:
        if job["status"] == "running":
            job["phase"] = "Cancelling — finishing in-flight images"
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
