# HVAC Marketer Agent

One brief in → bulk ad creatives, Meta ad copy, video scripts, and voiceovers out — dropped straight into a Google Drive folder.

## What it does

1. Fill in the client brief: name, callout location, offer (**Headline / Sub-headline / Also include / Don't include**), then upload the HVAC equipment image and optional client logo.
2. Pick **ad styles** (11 named templates), **image count**, **quality**, and a **scene setting** (modern suburban / luxury estate / beach house / country house / mountain home, or auto-vary). The callout grounds the scene's architecture, landscaping, climate and light in the real location without rendering the location name as artwork text. The summary panel shows a live estimated image cost per run.
3. Pick an output mode:
   - **Images only** — ad creatives with a full-resolution download button on every image tile and a ZIP download for multi-image runs; Google Drive is optional
   - **Images + Ad Copy** — adds Meta primary text + landing page prompt
   - **Full pipeline** — adds 2 brainrot scripts, 1 story script, ElevenLabs voiceovers, and story B-roll prompts
4. Hit **Generate** and download completed images directly from their tiles. Runs with at least two completed images also show **Download all images**, which packages the latest revision of each image into one ZIP. If Google Drive is connected and a folder is selected, the app also saves the run in a timestamped folder (`Images/`, `Videos/`). Drive remains required for the ad-copy and full-pipeline document outputs.

Use **Preview prompts** to inspect the exact prompts before spending image credits.

## Ad styles

| Style | Family | Feel |
|---|---|---|
| Bold Offer Blast | designed | Graphic-heavy, big typography, offer front and center |
| Offer-First Badges | designed | Offer-first layout, large readable text, colourful badges in logo colours |
| Premium Polished | designed | High-end, clean, trustworthy, conversion-focused |
| Luxury Lifestyle | designed | Upscale home scene, white/cool-blue luxury palette |
| Neat Install | designed | Beautiful home, unit neatly installed at the side |
| Minimal Editorial | designed | Bright, airy, white; logo colors as accents only |
| Vibrant Backyard | designed | Lush backyard, sunshine, logo-flipped color scheme |
| Fire & Ice | designed | Split-frame hot/cold — heat pumps & all-season systems |
| POV Body Cam | organic | Tech POV holding a handwritten offer note on the unit |
| Marketer Quit | organic | Ugly white background + red/black marker scrawl |
| Product Close-Up | organic | DSLR macro of the unit with a clean caption |

The 8 designed styles are the team's proven manual prompts used **word-for-word** — code only fills the slots (client, system, offer lines, don't-includes), so dollar amounts and terms never get paraphrased. Every style enforces: 1:1 aspect ratio, English-only text, no visible location names (the location is visual scene guidance and the Marketer Quit style retains its geo-tag), no phone/website/"Call Today" CTAs, and the HVAC brand appearing only as the badge on the unit.

## Setup

Python 3.11 or newer is recommended.

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Create `.env`:

```
OPENAI_API_KEY=...        # image generation (gpt-image-2, ChatGPT Images 2.0)
ANTHROPIC_API_KEY=...     # ad copy + scripts (Claude)
ELEVENLABS_API_KEY=...    # voiceovers (full pipeline only)
GOOGLE_CLIENT_ID=...      # Drive OAuth
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=https://your-app.example.com/auth/callback  # production
FLASK_SECRET_KEY=<stable-random-value>  # signs sessions + encrypts Drive tokens
SESSION_COOKIE_SECURE=true              # production HTTPS deployments
IMAGE_MODEL=gpt-image-2   # optional override
HVAC_SERVICE_API_KEY=<random>  # bearer token for Hermes/service revisions
HVAC_DATA_DIR=./data            # optional persistent-volume location
HVAC_RETENTION_DAYS=7           # minimum is seven days
```

Run — double-click **start.bat** (or `.venv\Scripts\python app.py`). The browser opens to http://127.0.0.1:5000 automatically; click **Connect Google Drive** the first time, upload a PNG/JPG/WebP equipment image (and optionally a logo), and generate. Each upload can be up to 15 MB. Use a descriptive equipment filename such as `Lennox System.png`, because its filename identifies the equipment brand in the original generation prompt.

Batch metadata, original uploads, generated images, copy files, hashes, revision history, and encrypted Drive credentials are stored durably in `HVAC_DATA_DIR`. Point that variable at a persistent volume in production and keep `FLASK_SECRET_KEY` stable; changing the secret intentionally makes existing Drive connections unreadable. Previous batches can be reopened from the UI after a page reload or application restart. Browser file selections themselves are intentionally not retained.

## Google Drive setup and behavior

1. In Google Cloud, enable the Google Drive API and create a **Web application** OAuth client.
2. Add `http://127.0.0.1:5000/auth/callback` as a redirect URI for local use. Add the exact HTTPS value configured in `GOOGLE_REDIRECT_URI` for production.
3. Configure the OAuth consent screen and add the Drive scope requested by the app.
4. Start the app, click **Connect Google Drive**, approve access, and choose a writable folder from the searchable selector. **My Drive** and writable shared-drive folders are supported; a folder URL can also be pasted.

Connections are isolated per signed browser session, so different users or browser profiles can connect different Google accounts at the same time. One browser session has one active Drive account; disconnect and reconnect to switch it. Refresh tokens are encrypted at rest in SQLite, refreshed automatically, and revoked on disconnect. Before a paid generation starts, the app verifies that the selected folder still exists and accepts uploads. Each run creates a timestamped client folder with `Images/` and, for the full pipeline, `Videos/`. Stable private export keys make folder and file creation idempotent across rate limits, server errors, and ambiguous network timeouts.

Generated images and copy are always saved to local durable storage before Drive upload. If Drive is temporarily unavailable, the run remains successful, every completed image and copy asset stays downloadable, and the UI reports which exports need attention.

The built-in searchable folder browser currently requests the full `https://www.googleapis.com/auth/drive` scope so it can enumerate arbitrary existing folders and create output inside the selected one. Google classifies that as a restricted scope; a public production OAuth app may require verification and a security assessment. A future Google Picker migration can use narrower per-file access, but requires Picker-specific Cloud configuration.

Run the automated suite with:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

## Batch revisions

Completed tiles have stable item IDs, positions, hashes, and version numbers. Select one or more tiles to edit them together, optionally adding supporting visual references or exact text replacements. Unselected items keep the same file and SHA-256 hash. Equipment replacement has its own control and versions every item in the batch.

External services use:

```text
POST /api/jobs/{job_id}/revisions
Authorization: Bearer <HVAC_SERVICE_API_KEY>
Idempotency-Key: <unique-request-id>
Content-Type: multipart/form-data
```

The multipart fields are `mode`, `selected_item_ids`, `instruction`, optional
`equipment_file`, repeated `reference_files`, `reference_manifest`,
`text_replacements`, and `quality`. Service-authenticated requests always use
low image quality. Poll the returned `/api/revisions/{revision_id}` URL; its
terminal response contains the complete ordered batch, including unchanged
items. The old `/api/jobs/{job_id}/refine/{index}` route remains available.

## Logo placement

- **Blended into design** — the logo image is sent to the model alongside the unit photo and integrated into the layout (best for designed styles).
- **Clean corner overlay** — the logo is composited pixel-perfectly onto the bottom-left after generation (best when logo fidelity matters most).

## Architecture

```
app.py                  Flask routes, background job runner (parallel images + polling API)
agent/ad_templates.py   Named style templates; deterministic offer slot-filling
agent/image_generator.py  gpt-image-2 edit calls (multi-reference, retries; auto-falls back to gpt-image-1 if the org lacks access — override with IMAGE_MODEL in .env)
agent/copy_generator.py   Claude creative bundle (copy, scripts, B-roll prompts)
agent/voice_generator.py  ElevenLabs TTS
agent/drive_uploader.py   Encrypted per-browser OAuth, folder validation, retry-safe Drive exports
agent/batch_store.py      SQLite batches, revisions, and encrypted Drive connection records
```
