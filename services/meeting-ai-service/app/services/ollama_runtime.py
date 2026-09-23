"""Shared on-prem model identity guard for analysis and follow-up questions."""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.core.config import Settings


def create_client() -> httpx.Client:
    """App-owned pool; TLS verification and environment behavior stay enabled."""
    return httpx.Client(limits=httpx.Limits(keepalive_expiry=60.0))


def require_model_identity(
    settings: Settings, timeout: float = 3.0, *, client: httpx.Client | None = None
) -> None:
    url = f"{settings.ollama_host}/api/tags"
    response = (
        client.get(url, timeout=timeout) if client is not None else httpx.get(url, timeout=timeout)
    )
    response.raise_for_status()
    try:
        models = response.json()["models"]
        if not isinstance(models, list):
            raise ValueError("invalid inventory")
        name = settings.ollama_model
        aliases = {name} if ":" in name else {name, f"{name}:latest"}
        matches = [
            item for item in models if isinstance(item, dict) and item.get("name") in aliases
        ]
        if len(matches) != 1:
            raise ValueError("missing or ambiguous model")
        expected = settings.ollama_expected_digest
        if expected and matches[0].get("digest") != expected:
            raise ValueError("model identity mismatch")
    except (KeyError, TypeError, ValueError) as exc:
        # Do not leak inventory/provider bodies into errors or durable retries.
        raise httpx.RequestError("Ollama configured model identity unavailable") from exc


def generate(
    settings: Settings, payload: dict[str, Any], *, client: httpx.Client | None = None
) -> httpx.Response:
    """Pinned deployments check the selected tag before and after generation.

    This detects ordinary tag replacement; it is not an atomic registry lock.
    The checks share the existing request budget rather than extending its lease.
    """
    deadline = time.monotonic() + settings.request_timeout
    if settings.ollama_think is not None:
        payload = {**payload, "think": settings.ollama_think}

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise httpx.TimeoutException("Ollama generation deadline exceeded")
        return budget

    if settings.ollama_expected_digest:
        require_model_identity(settings, min(3.0, remaining()), client=client)
    url = f"{settings.ollama_host}/api/generate"
    response = (
        client.post(url, json=payload, timeout=remaining())
        if client is not None
        else httpx.post(url, json=payload, timeout=remaining())
    )
    response.raise_for_status()
    if settings.ollama_expected_digest:
        require_model_identity(settings, min(3.0, remaining()), client=client)
    return response
