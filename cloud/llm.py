"""Pluggable vision-LLM provider layer for place identification.

Lets each user run cloud place-ID with their OWN key from their provider of
choice - OpenAI, Anthropic (Claude), Google (Gemini), or any OpenAI-compatible
endpoint. Only the selected provider's path is exercised, and the one SDK it
needs (`openai` or `anthropic`) is imported lazily so the others aren't required.

Adapters take image bytes + a prompt and return the model's raw text (expected to
be a JSON object); cloud/place_id.py owns the prompt wording and the JSON parsing
/ confidence threshold. Gemini is reached through Google's official
OpenAI-compatible endpoint, so it reuses the OpenAI Chat Completions path with no
extra dependency.
"""

import base64

from config import get_setting
from secret_store import get_api_key

# Google's official OpenAI-compatible base URL (vision + chat.completions).
GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"


class LLMConfigError(Exception):
    """Configuration/usage problem the user can fix (missing key, missing SDK,
    unknown provider). Distinct from a transient API error."""


def _cfg() -> dict:
    return {
        "provider": get_setting("llm_provider", "openai"),
        "model": get_setting("llm_model", "gpt-5.6-terra"),
        # OS keychain > personal_gpt_apikey env var > legacy plaintext settings.
        "api_key": get_api_key(),
        "base_url": get_setting("llm_base_url", ""),
    }


def _b64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("utf-8")


def _openai_responses(cfg: dict, image_bytes: bytes, prompt: str) -> str:
    """Native OpenAI via the Responses API (the model here does internal
    reasoning; Chat Completions truncated it - see place_id.py history)."""
    from openai import OpenAI

    client = OpenAI(api_key=cfg["api_key"])
    resp = client.responses.create(
        model=cfg["model"],
        reasoning={"effort": "medium"},
        input=[{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image",
                 "image_url": f"data:image/jpeg;base64,{_b64(image_bytes)}",
                 "detail": "high"},
            ],
        }],
        text={"format": {"type": "json_object"}},
    )
    return resp.output_text


def _openai_chat(cfg: dict, image_bytes: bytes, prompt: str, base_url: str) -> str:
    """Chat Completions vision - used for custom OpenAI-compatible endpoints and
    for Gemini's OpenAI-compatible endpoint."""
    from openai import OpenAI

    client = OpenAI(api_key=cfg["api_key"], base_url=base_url or None)
    resp = client.chat.completions.create(
        model=cfg["model"],
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{_b64(image_bytes)}"}},
            ],
        }],
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content


def _anthropic(cfg: dict, image_bytes: bytes, prompt: str) -> str:
    try:
        import anthropic
    except ImportError as e:
        raise LLMConfigError(
            "Claude needs the 'anthropic' package. Run: pip install anthropic"
        ) from e

    client = anthropic.Anthropic(api_key=cfg["api_key"])
    msg = client.messages.create(
        model=cfg["model"],
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/jpeg",
                            "data": _b64(image_bytes)}},
                {"type": "text",
                 "text": prompt + "\n\nRespond with ONLY the JSON object, no other text."},
            ],
        }],
    )
    return "".join(
        getattr(block, "text", "") for block in msg.content
        if getattr(block, "type", None) == "text"
    )


def vision_json(image_bytes: bytes, prompt: str) -> str:
    """Dispatch to the configured provider; returns the model's raw text output
    (expected JSON). Raises LLMConfigError for fixable config problems; any other
    exception is a provider/network error the caller should treat as a failure."""
    cfg = _cfg()
    if not cfg["api_key"]:
        raise LLMConfigError("No LLM API key set. Add one in Setup → Settings.")

    provider = cfg["provider"]
    if provider == "openai":
        return _openai_responses(cfg, image_bytes, prompt)
    if provider == "openai_compatible":
        if not cfg["base_url"]:
            raise LLMConfigError("The 'openai_compatible' provider needs a base URL.")
        return _openai_chat(cfg, image_bytes, prompt, cfg["base_url"])
    if provider == "gemini":
        return _openai_chat(cfg, image_bytes, prompt, GEMINI_OPENAI_BASE)
    if provider == "anthropic":
        return _anthropic(cfg, image_bytes, prompt)
    raise LLMConfigError(f"Unknown LLM provider: {provider!r}")


def test_llm() -> tuple[bool, str]:
    """Tiny vision probe used by Setup to validate the key/model/provider before
    a real run: sends a small generated image and asks for JSON. Returns
    (ok, message) - never raises - so the UI can show a plain-language result and
    still let the user fall back to manual place tagging."""
    try:
        import io
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (16, 16), (200, 60, 60)).save(buf, format="JPEG")
        vision_json(
            buf.getvalue(),
            'Reply with only this JSON and nothing else: {"ok": true}',
        )
        return True, "Vision call succeeded."
    except LLMConfigError as e:
        return False, str(e)
    except Exception as e:  # provider/network/auth error
        return False, f"{type(e).__name__}: {e}"
