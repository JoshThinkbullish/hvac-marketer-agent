# HVAC Marketer Agent

One brief in → bulk ad creatives, Meta ad copy, video scripts, and voiceovers out — dropped straight into a Google Drive folder.

## What it does

1. Fill in the client brief: name, callout location, offer (**Headline / Sub-headline / Also include / Don't include**), then upload the HVAC equipment image and optional client logo.
2. Pick **ad styles** (11 named templates), **image count**, **quality**, and a **scene setting** (modern suburban / luxury estate / beach house / country house / mountain home, or auto-vary). The callout grounds the scene's architecture, landscaping, climate and light in the real location without rendering the location name as artwork text. The summary panel shows a live estimated image cost per run.
3. Pick an output mode:
   - **Images only** — ad creatives with a full-resolution download button on every image tile; Google Drive is optional
   - **Images + Ad Copy** — adds Meta primary text + landing page prompt
   - **Full pipeline** — adds 2 brainrot scripts, 1 story script, ElevenLabs voiceovers, and story B-roll prompts
4. Hit **Generate** and download completed images directly from their tiles. If Google Drive is connected and a folder is selected, the app also saves the run in a timestamped folder (`Images/`, `Videos/`). Drive remains required for the ad-copy and full-pipeline document outputs.

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
FLASK_SECRET_KEY=<random>
IMAGE_MODEL=gpt-image-2   # optional override
HVAC_SERVICE_API_KEY=<random>  # bearer token for Hermes/service revisions
HVAC_DATA_DIR=./data            # optional persistent-volume location
HVAC_RETENTION_DAYS=7           # minimum is seven days
```

Run — double-click **start.bat** (or `.venv\Scripts\python app.py`). The browser opens to http://127.0.0.1:5000 automatically; click **Connect Google Drive** the first time, upload a PNG/JPG/WebP equipment image (and optionally a logo), and generate. Each upload can be up to 15 MB. Use a descriptive equipment filename such as `Lennox System.png`, because its filename identifies the equipment brand in the original generation prompt.

Batch metadata, original uploads, generated images, hashes, and revision history are stored durably in `HVAC_DATA_DIR`. Point that variable at a persistent volume in production. Previous batches can be reopened from the UI after a page reload or application restart. Browser file selections themselves are intentionally not retained.

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
agent/drive_uploader.py   Drive OAuth uploads (files + native Google Docs)
```
