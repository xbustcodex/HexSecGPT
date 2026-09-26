# -*- coding: utf-8 -*-
"""Local (Ollama) model discovery and selection.

Ollama exposes an OpenAI-compatible endpoint, so the app can talk to a
model running on this machine: no API key, no cost, works offline. This
module finds which local models exist and picks one to use.

The endpoint is probed over plain HTTP. A machine without Ollama running
is a normal, expected state - not an error - so the probe never raises
into the caller; it returns an empty list and the caller reports it.

Run directly to see what is available locally:

    python SeeLocalModels.py
    python SeeLocalModels.py --json
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

# Probed first; both may be overridden so a non-default Ollama install or
# a reverse proxy in front of it still works.
TAGS_URL = os.environ.get("HEXSEC_OLLAMA_TAGS_URL", "http://localhost:11434/api/tags")
OPENAI_MODELS_URL = os.environ.get("HEXSEC_OLLAMA_MODELS_URL", "http://localhost:11434/v1/models")

PROBE_TIMEOUT = 2.0

# Local ids that cannot serve a text chat. Reuses the OpenRouter exclusion
# list so both paths agree on what "a usable assistant" means.
from SeeOpenRouterFreeModels import EXCLUDED_FRAGMENTS  # noqa: E402


def is_suitable_local(model_id: str) -> bool:
    """True if a local model id is a plausible text chat model."""
    lowered = model_id.lower()
    if any(fragment in lowered for fragment in EXCLUDED_FRAGMENTS):
        return False
    # Embedding and rerank models are pulled under names that the shared
    # exclusion list does not cover.
    return not any(
        marker in lowered for marker in ("embed", "rerank", "minilm", "bge-", "e5-")
    )


def list_local_models(timeout: float = PROBE_TIMEOUT) -> List[str]:
    """Return every usable local model id. Empty when Ollama is not running.

    Tries ``/api/tags`` first (the native endpoint, no auth) and falls back
    to the OpenAI-compatible ``/v1/models`` for proxies that expose only that.
    """
    try:
        import requests
    except ImportError:
        return []

    for url, extract in ((TAGS_URL, _ids_from_tags), (OPENAI_MODELS_URL, _ids_from_openai)):
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
        except Exception:
            # A refused connection or a 404 just means try the next endpoint.
            continue
        try:
            models = extract(response.json())
        except (ValueError, AttributeError):
            continue
        usable = sorted(m for m in models if is_suitable_local(m))
        if usable:
            return usable
    return []


def _ids_from_tags(payload) -> List[str]:
    models = payload.get("models", []) if isinstance(payload, dict) else []
    return [m["name"] for m in models if isinstance(m, dict) and m.get("name")]


def _ids_from_openai(payload) -> List[str]:
    data = payload.get("data", []) if isinstance(payload, dict) else []
    return [m["id"] for m in data if isinstance(m, dict) and m.get("id")]


def local_model_exists(model_id: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """True if the id is currently pulled locally."""
    return model_id in list_local_models(timeout=timeout)


def resolve_local_model(
    preferred: Optional[str] = None,
    timeout: float = PROBE_TIMEOUT,
) -> Optional[str]:
    """Pick a local model id.

    Order: explicit ``preferred`` (if present) -> the persisted override
    (if present) -> any usable model. Returns ``None`` when Ollama is not
    running or has nothing usable pulled, which the caller must handle.
    """
    available = list_local_models(timeout=timeout)
    if not available:
        return None

    if preferred and preferred in available:
        return preferred

    override = load_local_override()
    if override and override in available:
        return override

    return available[0]


def load_local_override() -> Optional[str]:
    """Read a pinned local model, honouring HEXSEC_LOCAL_MODEL.

    Shares the ``.HexSec`` store with the API key and HEXSEC_MODEL, so a
    user configures everything in one file.
    """
    env_model = os.environ.get("HEXSEC_LOCAL_MODEL")
    if env_model:
        return env_model.strip()

    try:
        from dotenv import dotenv_values

        from SeeOpenRouterFreeModels import ConfigEnvPath

        values = dotenv_values(ConfigEnvPath())
        stored = values.get("HEXSEC_LOCAL_MODEL")
        if stored:
            return stored.strip()
    except Exception:
        # dotenv is optional here; a missing pin is not an error.
        pass
    return None


def save_local_override(model_id: str) -> bool:
    """Persist a local model choice into ``.HexSec``."""
    try:
        from dotenv import set_key

        from SeeOpenRouterFreeModels import ConfigEnvPath

        path = ConfigEnvPath()
        if not os.path.exists(path):
            open(path, "a", encoding="utf-8").close()
        return bool(set_key(path, "HEXSEC_LOCAL_MODEL", model_id))
    except Exception:
        return False


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point: list the models available locally."""
    import argparse

    parser = argparse.ArgumentParser(description="List local Ollama models")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    models = list_local_models()
    if args.json:
        print(json.dumps(models, indent=2))
        return 0

    if not models:
        print("No local models found.")
        print("Install Ollama (https://ollama.com), start it, then run:")
        print("  ollama pull qwen2.5-coder:7b")
        return 1

    for model_id in models:
        print(model_id)
    print(f"\n{len(models)} local model(s) available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
