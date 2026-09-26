# -*- coding: utf-8 -*-
"""OpenRouter model discovery and free-model selection.

Resolves a working model at runtime instead of pinning a name that goes stale.
The public OpenRouter ``/models`` endpoint needs no credentials, so model
discovery works even before an API key is configured.

Also runnable directly to list the current free tier:

    python SeeOpenRouterFreeModels.py
    python SeeOpenRouterFreeModels.py --json
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, Iterable, List, Optional

MODELS_URL = "https://openrouter.ai/api/v1/models"

# Cache TTL - the free tier churns constantly, but not per-second.
CACHE_TTL_SECONDS = 3600
CACHE_PATH = ".model_cache.json"

# Preference order for automatic selection. The first live, free model wins.
# These are stable, widely-mirrored families; anything not in this list is
# still usable via HEXSEC_MODEL, it just is not auto-picked.
PREFERRED_FAMILIES = (
    "nemotron",
    "gemma",
    "qwen",
    "llama",
    "mistral",
    "deepseek",
    "lfm",
    "llama-3",
)


class ModelDiscoveryError(RuntimeError):
    """Raised when the model catalogue cannot be retrieved."""


def _to_float(value) -> Optional[float]:
    """Coerce a pricing field to float.

    OpenRouter returns pricing as *strings* (e.g. "0", "-1"), so a naive
    ``== 0`` comparison silently never matches. That bug hid every
    zero-priced model behind the ``:free`` suffix alone.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_free_model(model: Dict) -> bool:
    """True if a model is usable at zero cost.

    Accepts either the ``:free`` suffix or zero prompt+completion pricing.
    A model priced at -1 (OpenRouter's "no data available" sentinel) is not
    free.
    """
    if model.get("id", "").endswith(":free"):
        return True
    pricing = model.get("pricing") or {}
    prompt = _to_float(pricing.get("prompt"))
    completion = _to_float(pricing.get("completion"))
    return prompt == 0.0 and completion == 0.0


def is_chat_model(model: Dict) -> bool:
    """True if the model accepts plain text input and produces text.

    Filters out the audio/video/image-only and embedding-style entries that
    are also priced at zero but cannot serve a chat completion.
    """
    arch = model.get("architecture") or {}
    inputs = arch.get("input_modalities")
    outputs = arch.get("output_modalities")
    if outputs and "text" not in outputs:
        return False
    if inputs and not ({"text", "image"} & set(inputs)):
        return False
    return True


def _cache_is_fresh(path: str = CACHE_PATH) -> bool:
    try:
        return (time.time() - os.path.getmtime(path)) < CACHE_TTL_SECONDS
    except OSError:
        return False


def _read_cache(path: str = CACHE_PATH) -> Optional[List[Dict]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        models = data.get("models")
        return models if isinstance(models, list) else None
    except (OSError, ValueError, AttributeError):
        return None


def _write_cache(models: List[Dict], path: str = CACHE_PATH) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time(), "models": models}, f)
    except OSError:
        # A read-only directory must not break model selection.
        pass


def fetch_models(
    force_refresh: bool = False,
    cache_path: str = CACHE_PATH,
    timeout: int = 15,
) -> List[Dict]:
    """Return the OpenRouter model catalogue, using an on-disk cache.

    The cache means a network failure degrades to the last known-good list
    instead of breaking the app outright.
    """
    if not force_refresh and _cache_is_fresh(cache_path):
        cached = _read_cache(cache_path)
        if cached is not None:
            return cached

    try:
        import requests
    except ImportError:
        cached = _read_cache(cache_path)
        if cached is not None:
            return cached
        raise ModelDiscoveryError(
            "requests is required for model discovery: pip install requests"
        )

    try:
        response = requests.get(MODELS_URL, timeout=timeout)
        response.raise_for_status()
        models = response.json().get("data", [])
    except Exception as exc:  # network, JSON, HTTP - all non-fatal here
        cached = _read_cache(cache_path)
        if cached is not None:
            return cached
        raise ModelDiscoveryError(f"Could not reach OpenRouter: {exc}") from exc

    if not isinstance(models, list):
        raise ModelDiscoveryError("Unexpected catalogue payload from OpenRouter")

    _write_cache(models, cache_path)
    return models


def list_free_models(
    force_refresh: bool = False,
    cache_path: str = CACHE_PATH,
) -> List[str]:
    """Return the ids of every currently-free, chat-capable model."""
    models = fetch_models(force_refresh=force_refresh, cache_path=cache_path)
    return sorted(
        m["id"]
        for m in models
        if isinstance(m, dict)
        and m.get("id")
        and is_free_model(m)
        and is_chat_model(m)
        and is_suitable_for_chat(m["id"])
    )


def model_exists(model_id: str, force_refresh: bool = False) -> bool:
    """True if the model id is present in the current catalogue."""
    try:
        models = fetch_models(force_refresh=force_refresh)
    except ModelDiscoveryError:
        return False
    return any(m.get("id") == model_id for m in models)


# Model id fragments marking a free entry as unsuitable for chat even though it
# is priced at zero. Auto-selecting one of these yields a broken assistant.
EXCLUDED_FRAGMENTS = (
    "content-safety",   # classifier, not a chat model
    "moderation",
    "guard",
    "router",           # meta-routing models
    "lyria",            # music generation
    "embedding",
    "whisper",
    "tts",
)


def is_suitable_for_chat(model_id: str) -> bool:
    """True if a free model id is a plausible general-purpose assistant."""
    lowered = model_id.lower()
    return not any(fragment in lowered for fragment in EXCLUDED_FRAGMENTS)


def _score(model_id: str) -> tuple:
    """Rank free models so selection is stable and prefers capable models."""
    family_rank = len(PREFERRED_FAMILIES)
    for index, family in enumerate(PREFERRED_FAMILIES):
        if family in model_id:
            family_rank = index
            break
    # Prefer models without a "preview" marker, then sort by id for determinism.
    return (family_rank, 1 if "preview" in model_id else 0, model_id)


def resolve_free_model(
    preferred: Optional[str] = None,
    force_refresh: bool = False,
    cache_path: str = CACHE_PATH,
) -> Optional[str]:
    """Pick a usable model id.

    Order: explicit ``preferred`` (if still live) -> persisted override ->
    best-ranked free model. Returns ``None`` when nothing free is available.
    """
    if preferred:
        if model_exists(preferred, force_refresh=force_refresh):
            return preferred
        # Fall through: a stale pin must never be fatal.

    override = load_model_override()
    if override and override != preferred:
        if model_exists(override, force_refresh=force_refresh):
            return override

    free = list_free_models(force_refresh=force_refresh, cache_path=cache_path)
    if not free:
        return None
    return min(free, key=_score)


def load_model_override() -> Optional[str]:
    """Read the persisted model choice.

    ``HEXSEC_MODEL`` in the environment or ``.HexSec`` wins, so a user can pin
    a model without editing source.
    """
    env_model = os.environ.get("HEXSEC_MODEL")
    if env_model:
        return env_model.strip()

    try:
        from dotenv import dotenv_values

        values = dotenv_values(ConfigEnvPath())
        stored = values.get("HEXSEC_MODEL")
        if stored:
            return stored.strip()
    except Exception:
        # dotenv is optional here; a missing key is not an error.
        pass
    return None


def ConfigEnvPath() -> str:
    """Resolve the env file next to the application, not the CWD."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".HexSec")


def save_model_override(model_id: str) -> bool:
    """Persist a model choice into ``.HexSec`` so it survives restarts."""
    try:
        from dotenv import set_key

        path = ConfigEnvPath()
        if not os.path.exists(path):
            open(path, "a", encoding="utf-8").close()
        return bool(set_key(path, "HEXSEC_MODEL", model_id))
    except Exception:
        return False


def main(argv: Optional[Iterable[str]] = None) -> int:
    """CLI entry point: list the current free models."""
    import argparse

    parser = argparse.ArgumentParser(description="List free OpenRouter models")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--refresh", action="store_true", help="bypass the cache")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        models = fetch_models(force_refresh=args.refresh)
    except ModelDiscoveryError as exc:
        print(f"error: {exc}")
        return 1

    free = [
        m
        for m in models
        if isinstance(m, dict)
        and m.get("id")
        and is_free_model(m)
        and is_chat_model(m)
        and is_suitable_for_chat(m["id"])
    ]
    free.sort(key=lambda m: m["id"])

    if args.json:
        print(json.dumps([m["id"] for m in free], indent=2))
    else:
        for m in free:
            ctx = m.get("context_length")
            suffix = f"  (ctx {ctx:,})" if ctx else ""
            print(f"{m['id']}{suffix}")
        print(f"\n{len(free)} free model(s) available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
