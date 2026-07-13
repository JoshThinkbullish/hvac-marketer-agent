"""ElevenLabs text-to-speech wrapper.

Plain HTTP via httpx so we avoid a separate SDK dependency. Reads
ELEVENLABS_API_KEY from the environment; voice ID is supplied by the
caller (the pipeline defaults to the operator's preferred voice).
"""
from __future__ import annotations

import logging
import os
import time

import httpx

log = logging.getLogger("hvac-marketer.voice")


DEFAULT_MODEL_ID = "eleven_multilingual_v2"
DEFAULT_VOICE_ID = "dtSEyYGNJqjrtBArPCVZ"
TIMEOUT_SECONDS = 120.0


def _api_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "ELEVENLABS_API_KEY is not set. Add it to .env and restart."
        )
    return key


def generate_voiceover(text: str, voice_id: str = DEFAULT_VOICE_ID,
                        model_id: str = DEFAULT_MODEL_ID,
                        max_attempts: int = 3) -> bytes:
    """Synthesize `text` with ElevenLabs and return MP3 bytes.

    Retries transient failures (connection/timeout, 429, 5xx) with backoff
    so one blip doesn't cost a full paid pipeline re-run; other 4xx (bad
    voice id, exhausted quota) fail fast.
    """
    if not text or not text.strip():
        raise ValueError("Cannot synthesize empty script.")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": _api_key(),
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        },
    }

    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                resp = client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as e:
            last_err = e
        else:
            if resp.status_code < 400:
                return resp.content
            err = RuntimeError(
                f"ElevenLabs error {resp.status_code}: {resp.text[:500]}")
            if resp.status_code != 429 and resp.status_code < 500:
                raise err
            last_err = err
        if attempt < max_attempts:
            delay = 2 ** attempt
            log.warning("ElevenLabs attempt %d/%d failed (%s); retrying in %ds",
                        attempt, max_attempts, last_err, delay)
            time.sleep(delay)
    raise RuntimeError(
        f"ElevenLabs failed after {max_attempts} attempts: {last_err}")
