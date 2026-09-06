"""Cloud place-name identification via OpenAI vision (Premise 1, 12, 13, 21, 24).

Only ever called on a representative photo already confirmed to contain no
enrolled face (Premise 12) - face recognition itself never leaves the
machine. API failures are the caller's responsibility to catch and route to
the manual-tagging fallback (Premise 13) - this module does not retry.

Uses the Responses API (not Chat Completions) - this model does internal
reasoning before producing visible output, and Chat Completions kept
returning empty/UNKNOWN answers that were likely truncation (reasoning
tokens consuming the whole budget), not genuine refusals. The confidence
score below is real, model-reported confidence, not a scoring heuristic
of ours - previously there was no confidence check at all.
"""

import json
import os

from config import get_setting
from cloud.llm import vision_json, LLMConfigError

CONFIDENCE_THRESHOLD = 0.5  # model-reported confidence (0.0-1.0); below this, treated as unresolved

_PROMPT = (
    "This is my own travel photo that I'm organizing into a personal photo "
    "album. Identify any recognizable landmark, building, shrine, market, or "
    "named place visible in it, based on its architecture and visual features - "
    "the kind of caption you'd write under a photo in a travel journal.\n\n"
    "Return only valid JSON with this schema:\n"
    "{\n"
    '  "status": "resolved | uncertain | unknown",\n'
    '  "place_name": "string or null",\n'
    '  "confidence": "number 0.0 to 1.0"\n'
    "}\n\n"
    'Set status to "unknown" only if there is truly nothing visually '
    "distinctive enough to name."
)


def _build_prompt() -> str:
    """Prepends the trip country (if configured) so the model can disambiguate
    similar-looking places and prefer landmarks actually in that country."""
    country = (get_setting("trip_country", "") or "").strip()
    if not country:
        return _PROMPT
    context = (
        f"This photo was taken in {country}. Use that as geographic context to "
        f"disambiguate similar-looking places, and prefer place names located in "
        f"{country}.\n\n"
    )
    return context + _PROMPT


class MissingApiKeyError(Exception):
    pass


class MissingCountryError(Exception):
    pass


class PlaceIdApiError(Exception):
    pass


def check_api_key_present() -> None:
    """Fails loudly and immediately if no LLM key is configured (Premise 24) -
    called once at ingest startup, before any per-cluster work begins, so a
    missing key doesn't waste an entire run's worth of place-ID attempts. The
    provider/model/key are chosen in Setup → Settings (or, for back-compat, the
    personal_gpt_apikey environment variable)."""
    from secret_store import has_api_key
    if not has_api_key():
        raise MissingApiKeyError(
            "No LLM API key configured. Set one in Setup → Settings "
            "(or the personal_gpt_apikey environment variable)."
        )


def check_country_present() -> None:
    """Fails loudly if the trip country isn't set - called once at ingest startup
    (alongside the API-key check) so location detection always has the country
    context. The user sets it in Setup → Settings."""
    if not (get_setting("trip_country", "") or "").strip():
        raise MissingCountryError(
            "No trip country set. Add it in Setup → Settings so location "
            "detection knows which country your photos are in."
        )


def identify_place(image_path: str) -> str:
    """Sends one photo to the configured vision LLM (OpenAI / Claude / Gemini /
    OpenAI-compatible) and asks for a place name + a real confidence score.
    Raises PlaceIdApiError on any failure - network, rate limit, bad key, missing
    SDK, low confidence, or a genuine "unknown" answer. Callers must catch this
    and route the cluster to manual tagging (Premise 13) - no retry/backoff."""
    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        raw = vision_json(image_bytes, _build_prompt())
        data = json.loads(raw)
        status = data.get("status", "unknown")
        place_name = data.get("place_name")
        try:
            confidence = float(data.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            confidence = 0.0

        if status == "unknown" or not place_name or confidence < CONFIDENCE_THRESHOLD:
            raise PlaceIdApiError(
                f"{image_path}: status={status!r} place_name={place_name!r} "
                f"confidence={confidence}"
            )
        return place_name
    except PlaceIdApiError:
        raise
    except LLMConfigError as e:
        raise PlaceIdApiError(f"{image_path}: {e}") from e
    except Exception as e:
        raise PlaceIdApiError(f"{image_path}: {e}") from e
