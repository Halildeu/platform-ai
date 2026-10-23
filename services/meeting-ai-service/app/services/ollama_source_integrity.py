"""Qualified complete-context transport. This is not semantic task validation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from app.core.config import Settings

# Qualification includes warm shift-on -> shift-off, input overflow rejection and
# incomplete generation rejection. Add profiles only with equivalent real probes.
# Native/non-llama backends and untested versions are intentionally not inferred.
QUALIFIED_PROFILES = frozenset(
    {("0.34.4", "46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e")}
)


def require_source_runtime(
    settings: Settings,
    remaining: Callable[[], float],
    *,
    client: httpx.Client | None,
) -> None:
    """Check the actual version/backend. Tag pin checks stay in the shared caller."""
    try:
        url = f"{settings.ollama_host}/api/version"
        timeout = min(3, remaining())
        response = (
            client.get(url, timeout=timeout)
            if client is not None
            else httpx.get(url, timeout=timeout)
        )
        response.raise_for_status()
        version = response.json()["version"]
        if (version, settings.ollama_expected_digest) not in QUALIFIED_PROFILES:
            raise ValueError("unqualified runtime profile")
        url = f"{settings.ollama_host}/api/show"
        payload = {"model": settings.ollama_model}
        timeout = min(3, remaining())
        response = (
            client.post(url, json=payload, timeout=timeout)
            if client is not None
            else httpx.post(url, json=payload, timeout=timeout)
        )
        response.raise_for_status()
        body = response.json()
        if (
            body["details"]["format"] != "gguf"
            or body["model_info"]["general.architecture"] != "llama"
        ):
            raise ValueError("unqualified runner architecture")
    except (KeyError, TypeError, ValueError) as exc:
        raise httpx.RequestError("Ollama source integrity runtime unverified") from exc


def require_complete_generation(settings: Settings, response: httpx.Response) -> None:
    """A syntactically valid partial object must never replace a live snapshot."""
    try:
        body: Any = response.json()
        aliases = {settings.ollama_model}
        if ":" not in settings.ollama_model:
            aliases.add(f"{settings.ollama_model}:latest")
        if (
            not isinstance(body, dict)
            or body.get("model") not in aliases
            or body.get("done") is not True
            or body.get("done_reason") != "stop"
            or "error" in body
            or not isinstance(body.get("response"), str)
        ):
            raise ValueError("generation incomplete")
    except (TypeError, ValueError) as exc:
        raise httpx.RequestError("Ollama complete source generation unverified") from exc
