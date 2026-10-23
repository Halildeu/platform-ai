"""Local synthetic experiment only: deadline covers connect and every read."""

import asyncio
import json
from typing import Any

import httpx

from experiments.task_state.indexed_probe import MODELS
from experiments.task_state.prototype import InvalidProposalError


async def _generate(
    model: str,
    prompt: str,
    *,
    schema: dict[str, Any],
    options: dict[str, Any],
    deadline_seconds: float,
    think: bool | None,
) -> dict[str, Any]:
    if model not in MODELS or not 0 < deadline_seconds <= 90:
        raise InvalidProposalError("unsupported_inference_profile")
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "format": schema,
        "options": options,
        "stream": True,
        "keep_alive": "2m",
    }
    if think is not None:
        payload["think"] = think
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    if len(encoded) > 32000:
        raise InvalidProposalError("probe_context_budget")
    accumulated: list[str] = []

    def accept(line: bytes) -> dict[str, Any] | None:
        if not line.strip():
            return None
        chunk = json.loads(line)
        if not isinstance(chunk, dict) or chunk.get("model") != model:
            raise InvalidProposalError("response_model_mismatch")
        part = chunk.get("response", "")
        if not isinstance(part, str):
            raise InvalidProposalError("generation_invalid_fragment")
        accumulated.append(part)
        if chunk.get("done") is not True:
            return None
        if chunk.get("done_reason") == "length":
            raise InvalidProposalError("generation_incomplete")
        if chunk.get("done_reason") != "stop":
            raise InvalidProposalError("generation_stop_unverified")
        chunk["response"] = "".join(accumulated)
        return chunk

    try:
        async with asyncio.timeout(deadline_seconds):
            async with httpx.AsyncClient(
                base_url="http://127.0.0.1:11434",
                timeout=deadline_seconds,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    "/api/generate",
                    content=encoded,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    response.raise_for_status()
                    pending = b""
                    size = 0
                    async for data in response.aiter_bytes():
                        size += len(data)
                        if size > 524288:
                            raise InvalidProposalError("model_output_budget")
                        pending += data
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            result = accept(line)
                            if result is not None:
                                return result
                    if pending:
                        result = accept(pending)
                        if result is not None:
                            return result
    except TimeoutError:
        raise InvalidProposalError("generation_deadline_unobserved") from None
    raise InvalidProposalError("generation_end_unobserved")


def generate(client: httpx.Client, model: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
    # Same call shape as the existing probe; do not accept arbitrary endpoints.
    if str(client.base_url).rstrip("/") != "http://127.0.0.1:11434":
        raise InvalidProposalError("probe_endpoint_not_allowed")
    return asyncio.run(_generate(model, prompt, **kwargs))
