"""gpt-image-1 wrapper + logo compositor.

Prompts are built deterministically in agent/ad_templates.py; this module
handles the mechanical bits: PNG-ifying reference images, calling the edit
endpoint (with retries), overlaying the operator's logo, and producing
small thumbnails for the live progress UI.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import time
import urllib.request

from PIL import Image
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)

log = logging.getLogger("hvac-marketer.images")

# gpt-image-2 (ChatGPT Images 2.0, April 2026) — same model family the team
# uses manually in ChatGPT. It processes reference images at high fidelity
# automatically, so input_fidelity is only sent to gpt-image-1 fallbacks.
DEFAULT_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-2").strip() or "gpt-image-2"
FALLBACK_MODEL = "gpt-image-1"

_client: OpenAI | None = None
_active_model: str | None = None   # settles after the first call resolves access


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set. Add it to .env and restart.")
        _client = OpenAI(api_key=api_key)
    return _client


def _to_png_file(image_src: str | bytes, name: str) -> io.BytesIO:
    """Accepts a file path or raw image bytes; returns a named PNG buffer."""
    src = io.BytesIO(image_src) if isinstance(image_src, (bytes, bytearray)) \
        else image_src
    with Image.open(src) as img:
        img = img.convert("RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
    buf.seek(0)
    buf.name = name
    return buf


def composite_logo(image_bytes: bytes, logo_path: str,
                   width_ratio: float = 0.22,
                   pad_ratio: float = 0.04) -> bytes:
    """Overlay a logo onto the bottom-left of an image and return PNG bytes."""
    base = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    logo = Image.open(logo_path).convert("RGBA")

    target_w = max(1, int(base.width * width_ratio))
    target_h = max(1, int(logo.height * (target_w / logo.width)))
    logo = logo.resize((target_w, target_h), Image.LANCZOS)

    pad = int(base.width * pad_ratio)
    x = pad
    y = base.height - target_h - pad

    base.alpha_composite(logo, (x, y))

    buf = io.BytesIO()
    base.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def make_thumbnail(image_bytes: bytes, max_px: int = 384) -> bytes:
    """Downscale to a JPEG thumbnail for the progress UI."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


_RETRYABLE = (APIConnectionError, APITimeoutError, RateLimitError)


def _is_model_access_error(e: APIStatusError) -> bool:
    """400/403/404 responses that mean 'this org can't use this model'."""
    if e.status_code not in (400, 403, 404):
        return False
    msg = str(e).lower()
    return any(t in msg for t in ("model", "verif", "does not exist",
                                  "not found", "access"))


def _call_edit(client: OpenAI, model: str, prompt: str,
               reference_image_paths: list[str | bytes], size: str,
               quality: str):
    files = [
        _to_png_file(p, f"reference_{i}.png")
        for i, p in enumerate(reference_image_paths)
    ]
    kwargs = {}
    if model.startswith("gpt-image-1"):
        # gpt-image-2 processes references at high fidelity automatically
        # and rejects the parameter.
        kwargs["input_fidelity"] = "high"
    return client.images.edit(
        model=model,
        image=files if len(files) > 1 else files[0],
        prompt=prompt,
        n=1,
        size=size,
        quality=quality,
        **kwargs,
    )


def generate_image(prompt: str, reference_image_paths: list[str | bytes],
                   size: str = "1024x1024", quality: str = "high",
                   max_attempts: int = 3) -> bytes:
    """Generate one image via the images.edit endpoint.

    Uses gpt-image-2 (the ChatGPT Images 2.0 model); if the org can't
    access it (e.g. unverified), falls back to gpt-image-1 once and
    remembers that choice for the rest of the session.

    `reference_image_paths` — first entry is the HVAC unit photo; an
    optional second entry is the client logo.

    Retries transient failures (connection, timeout, rate limit, 5xx)
    with exponential backoff. Other 4xx errors (e.g. content policy)
    fail fast.
    """
    global _active_model
    client = _get_client()
    model = _active_model or DEFAULT_MODEL

    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = _call_edit(client, model, prompt,
                                  reference_image_paths, size, quality)
            _active_model = model
            break
        except _RETRYABLE as e:
            # Checked first so RateLimitError (an APIStatusError subclass)
            # is retried instead of failing fast as a 4xx.
            last_err = e
        except APIStatusError as e:
            if (model != FALLBACK_MODEL and _is_model_access_error(e)):
                log.warning("%s unavailable for this org (%s); falling back "
                            "to %s for this session.", model, e, FALLBACK_MODEL)
                _active_model = model = FALLBACK_MODEL
                last_err = e
                continue    # immediate retry on the fallback model
            if e.status_code < 500:
                raise
            last_err = e
        if attempt < max_attempts:
            delay = 2 ** attempt
            log.warning("%s attempt %d/%d failed (%s); retrying in %ds",
                        model, attempt, max_attempts, last_err, delay)
            time.sleep(delay)
    else:
        raise RuntimeError(
            f"{model} failed after {max_attempts} attempts: {last_err}"
        )

    item = response.data[0]
    if item.b64_json:
        return base64.b64decode(item.b64_json)
    if item.url:
        with urllib.request.urlopen(item.url, timeout=120) as r:
            return r.read()
    raise RuntimeError(f"{model} returned no image data.")
