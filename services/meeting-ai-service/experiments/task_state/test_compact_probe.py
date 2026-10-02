"""Failure gates and transport settings, without network/model inference."""

import json

import httpx
import pytest

from experiments.task_state import compact_probe as probe
from experiments.task_state.prototype import InvalidProposalError


@pytest.mark.parametrize("failure", ["relation", "length", "timeout", "schema"])
def test_first_failure_stops_all_later_calls_and_is_not_qualified(monkeypatch, failure):
    from experiments.task_state.probe import expected

    monkeypatch.setattr(
        probe,
        "cases",
        lambda: [
            ("first", "a", "Ece raporu hazırlayacak.", [expected(["raporu"], "Ece")]),
            ("second", "b", "Yeni konu yok.", []),
        ],
    )
    monkeypatch.setattr(probe, "fingerprint", lambda *a: {"model": probe.MODEL})
    calls = []

    def generate(*args, **kwargs):
        calls.append(kwargs)
        if failure == "timeout":
            raise httpx.ReadTimeout("private source must not be reported")
        if failure == "length":
            raise InvalidProposalError("generation_incomplete")
        return {"response": '{"e":[],"u":[]}' if failure == "relation" else "private invalid"}

    monkeypatch.setattr(probe, "generate", generate)
    monkeypatch.setattr(
        httpx.Client,
        "get",
        lambda *a, **k: httpx.Response(
            200,
            json={"models": []},
            request=httpx.Request("GET", "http://local"),
        ),
    )
    report = probe.run()
    assert len(calls) == 1 and report["stopped_after_failure"]
    assert calls[0]["options"]["num_predict"] == 768
    assert calls[0]["deadline_seconds"] == 120
    assert not report["candidate_gate_passed"] and not report["semantic_qualification"]
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize(
    "overrides",
    [
        {"size_vram": 1},
        {"context_length": 2048},
        {"digest": "drifted"},
        {"name": "wrong"},
    ],
)
def test_alternative_runtime_cannot_silently_change_device_context_or_weights(overrides):
    row = {
        "name": probe.ALTERNATIVE,
        "digest": probe.MODELS[probe.ALTERNATIVE],
        "size_vram": 0,
        "context_length": 8192,
    }
    row.update(overrides)
    with (
        httpx.Client(
            base_url="http://local",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"models": [row]})
            ),
        ) as client,
        pytest.raises(InvalidProposalError, match="candidate_runtime_mismatch"),
    ):
        probe.require_cpu_runtime(client, probe.ALTERNATIVE)


def test_thinking_control_is_top_level_and_does_not_enter_decoding_options():
    from experiments.task_state.indexed_probe import generate

    received = []

    def respond(request):
        received.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": probe.ALTERNATIVE,
                "done": True,
                "done_reason": "stop",
                "response": "{}",
            },
        )

    with httpx.Client(base_url="http://local", transport=httpx.MockTransport(respond)) as client:
        generate(
            client, probe.ALTERNATIVE, "synthetic", options=probe.ALTERNATIVE_OPTIONS, think=False
        )
    assert received[0]["think"] is False
    assert "think" not in received[0]["options"]
    assert received[0]["options"]["num_gpu"] == 0
    assert received[0]["options"]["num_predict"] == 1024
