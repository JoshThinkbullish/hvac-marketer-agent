import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

load_dotenv()

from agent.drive_uploader import SCOPES, TOKEN_PATH, get_folders, upload_to_folder
from agent.image_generator import generate_ad_images

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-me")

HVAC_DIR = Path(__file__).parent / "hvac_systems"
SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def get_hvac_systems() -> list[dict]:
    if not HVAC_DIR.exists():
        return []
    return [
        {"name": f.stem, "path": str(f)}
        for f in sorted(HVAC_DIR.iterdir())
        if f.suffix.lower() in SUPPORTED_EXTS
    ]


# ── Main form ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    systems = get_hvac_systems()
    connected    = TOKEN_PATH.exists()
    creds_present = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
    return render_template(
        "index.html",
        systems=systems,
        connected=connected,
        creds_present=creds_present,
        connected_msg=request.args.get("connected"),
        error_msg=request.args.get("error"),
    )


# ── Drive folders API ──────────────────────────────────────────────────────────

@app.route("/api/drive/folders")
def api_drive_folders():
    if not TOKEN_PATH.exists():
        return jsonify({"error": "Not connected to Google Drive"}), 401
    try:
        folders = get_folders()
        return jsonify({"folders": folders})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Generate ───────────────────────────────────────────────────────────────────

@app.route("/generate", methods=["POST"])
def generate():
    business    = request.form.get("business", "").strip()
    offer       = request.form.get("offer", "").strip()
    system_name = request.form.get("system", "").strip()
    location    = request.form.get("location", "").strip()
    folder_id   = request.form.get("folder_id", "").strip()
    folder_name = request.form.get("folder_name", "Unknown Folder").strip()
    variants    = int(request.form.get("variants", 1))

    ad_prompts = [
        v.strip()
        for k, v in sorted(request.form.items())
        if k.startswith("ad_prompt_") and v.strip()
    ]
    if not ad_prompts:
        ad_prompts = ["Professional marketing ad"]

    if not all([business, offer, system_name, location]):
        return jsonify({"error": "All fields are required."}), 400
    if not folder_id:
        return jsonify({"error": "Please select a Drive folder to save into."}), 400
    if not TOKEN_PATH.exists():
        return jsonify({"error": "Google Drive not connected. Use the Connect button first."}), 400

    system_path = next(
        (HVAC_DIR / f.name for f in HVAC_DIR.iterdir() if f.stem == system_name),
        None,
    )
    if not system_path or not system_path.exists():
        return jsonify({"error": f"System image not found for '{system_name}'."}), 400

    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    drive_links = []
    errors      = []

    for ad_index, ad_prompt in enumerate(ad_prompts, start=1):
        try:
            images = generate_ad_images(
                business=business,
                offer=offer,
                system_name=system_name,
                system_image_path=str(system_path),
                location=location,
                ad_type=ad_prompt,
                n_images=variants,
            )
        except Exception as e:
            errors.append(f"Ad type {ad_index} failed: {str(e)}")
            continue

        for v_index, img_bytes in enumerate(images, start=1):
            variant_tag = f"_v{v_index}" if variants > 1 else ""
            filename = f"{timestamp}_type{ad_index}{variant_tag}_{system_name}.png"
            try:
                link = upload_to_folder(folder_id, img_bytes, filename)
                drive_links.append({"label": f"Ad {ad_index}{variant_tag}", "url": link})
            except Exception as e:
                errors.append(f"Upload failed for ad {ad_index} v{v_index}: {str(e)}")

    if not drive_links and errors:
        return jsonify({"error": errors[0]}), 500

    return jsonify({
        "success": True,
        "business": business,
        "folder": folder_name,
        "links": drive_links,
        "count": len(drive_links),
        "warnings": errors,
    })


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
        scopes=SCOPES,
        state=state,
        redirect_uri=url_for("auth_callback", _external=True),
    )


@app.route("/auth/google")
def auth_google():
    if not GOOGLE_CLIENT_ID:
        return redirect(url_for("index", error="missing_credentials"))
    flow = _make_flow()
    auth_url, state = flow.authorization_url(prompt="consent", access_type="offline")
    session["oauth_state"] = state
    return redirect(auth_url)


@app.route("/auth/callback")
def auth_callback():
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    flow = _make_flow(state=session.get("oauth_state"))
    flow.fetch_token(authorization_response=request.url)
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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
