"""#62 producer-side contract gate for /ws/stream events.

Web/mobile clients consume these events; this test validates every event shape
against docs/contracts/ws-stream-events.schema.json so contract drift fails CI
instead of surfacing in production.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from app.main import app
from app.services import streaming_models

SCHEMA_PATH = (
    Path(__file__).resolve().parents[4] / "docs" / "contracts" / "ws-stream-events.schema.json"
)
VALIDATOR = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def assert_valid(event: dict[str, Any]) -> None:
    errors = sorted(VALIDATOR.iter_errors(event), key=str)
    assert not errors, f"contract violation for {event!r}: {[e.message for e in errors]}"


def test_schema_file_is_valid_jsonschema() -> None:
    Draft202012Validator.check_schema(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def test_handshake_events_match_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real WS handshake: loading + loading + ready, each schema-valid."""
    monkeypatch.setattr(
        streaming_models.DirectWhisperService, "ensure_model", lambda self: None
    )
    with TestClient(app) as client, client.websocket_connect("/ws/stream") as ws:
        first = ws.receive_json()
        second = ws.receive_json()
        ready = ws.receive_json()

    for event in (first, second, ready):
        assert_valid(event)
    assert [first["type"], second["type"], ready["type"]] == ["loading", "loading", "ready"]
    assert first["stage"] == "live_model"
    assert second["stage"] == "final_model"


def test_partial_and_final_payload_shapes_match_contract() -> None:
    """Mirror of the exact payloads stream.py emits (keys must stay in sync)."""
    partial = {
        "type": "partial",
        "seq": 0,
        "confirmed": "",
        "tentative": "merhaba",
        "elapsed_ms": 250,
        "rms": 0.01234,
        "source": "medium",
    }
    final = {
        "type": "final",
        "seq": 0,
        "text": "Merhaba, toplanti basliyor.",
        "reason": "silence",
        "elapsed_ms": 600,
        "rms": 0.01234,
    }
    error = {"type": "error", "msg": "RuntimeError"}
    for event in (partial, final, error):
        assert_valid(event)


def test_contract_rejects_unknown_event_type() -> None:
    errors = list(VALIDATOR.iter_errors({"type": "telemetry", "x": 1}))
    assert errors, "schema must reject unknown event types (drift gate)"
