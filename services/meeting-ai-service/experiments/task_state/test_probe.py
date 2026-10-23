"""Offline probe safety and metric checks. Mock responses are NOT model evidence."""

import json

import httpx
import pytest

from experiments.task_state import probe
from experiments.task_state.prototype import CandidateLedger, InvalidProposalError


def fake_environment(monkeypatch):
    monkeypatch.setattr(probe, "cases", lambda: [("synthetic", "s", "Toplantı sürüyor.", [])])
    monkeypatch.setattr(probe, "fingerprint", lambda client: {"digest": probe.DIGEST})
    monkeypatch.setattr(
        probe,
        "generate",
        lambda client, prompt: {
            "response": '{"events":[],"unresolved":[]}',
            "prompt_eval_count": 100,
            "eval_count": 10,
        },
    )


def test_mock_success_is_never_semantic_or_phone_qualification(monkeypatch):
    fake_environment(monkeypatch)
    report = probe.run()
    assert report["candidate_gate_passed"]
    assert not report["semantic_qualification"]
    assert not report["phone_acceptance"]
    assert not report["server_context_coverage_verified"]
    assert "Toplantı" not in json.dumps(report)


def test_code_drift_invalidates_even_a_matching_candidate(monkeypatch):
    fake_environment(monkeypatch)
    values = iter([{"prototype.py": "before"}, {"prototype.py": "after"}])
    monkeypatch.setattr(probe, "code_hashes", lambda: next(values))
    report = probe.run()
    assert not report["code_stable"] and not report["candidate_gate_passed"]


def test_after_run_fingerprint_failure_preserves_rows_and_fails_gate(monkeypatch):
    fake_environment(monkeypatch)
    count = 0

    def changed(client):
        nonlocal count
        count += 1
        if count == 2:
            raise InvalidProposalError("model_fingerprint_mismatch")
        return {"digest": probe.DIGEST}

    monkeypatch.setattr(probe, "fingerprint", changed)
    report = probe.run()
    assert len(report["rows"]) == 1
    assert not report["fingerprint_stable"] and not report["candidate_gate_passed"]


def test_timeout_stops_all_further_inference(monkeypatch):
    fake_environment(monkeypatch)
    monkeypatch.setattr(
        probe,
        "cases",
        lambda: [
            ("one", "a", "Toplantı sürüyor.", []),
            ("two", "b", "Başka toplantı sürüyor.", []),
        ],
    )
    calls = 0

    def timeout(client, prompt):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("must not be copied to report")

    monkeypatch.setattr(probe, "generate", timeout)
    report = probe.run()
    assert calls == 1 and report["stopped_after_transport_timeout"]
    assert not report["candidate_gate_passed"]
    assert "must not" not in json.dumps(report)


def test_expected_labels_never_enter_the_model_prompt(monkeypatch):
    fake_environment(monkeypatch)
    monkeypatch.setattr(
        probe,
        "cases",
        lambda: [
            ("case", "s", "Toplantı sürüyor.", [probe.expected(["DO_NOT_LEAK"], "SECRET_ORACLE")]),
        ],
    )

    def check(client, prompt):
        assert "DO_NOT_LEAK" not in prompt and "SECRET_ORACLE" not in prompt
        return {"response": '{"events":[],"unresolved":[]}'}

    monkeypatch.setattr(probe, "generate", check)
    report = probe.run()
    assert report["rows"][0]["missing_or_ambiguous_work"] == 1


def test_fingerprint_requires_exact_existing_model_digest():
    def respond(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "test"})
        return httpx.Response(200, json={"models": [{"name": probe.MODEL, "digest": "different"}]})

    with (
        httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond)) as client,
        pytest.raises(InvalidProposalError, match="model_fingerprint_mismatch"),
    ):
        probe.fingerprint(client)


@pytest.mark.parametrize(
    "data,reason",
    [
        ({"done": True, "done_reason": "length", "model": probe.MODEL}, "generation_incomplete"),
        ({"done": True, "model": "different"}, "response_model_mismatch"),
    ],
)
def test_incomplete_or_different_model_generation_is_rejected(data, reason):
    with (
        httpx.Client(
            base_url="http://test",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=data),
            ),
        ) as client,
        pytest.raises(InvalidProposalError, match=reason),
    ):
        probe.generate(client, "synthetic")


def test_oversized_response_is_not_retained():
    with (
        httpx.Client(
            base_url="http://test",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"x" * 262145),
            ),
        ) as client,
        pytest.raises(InvalidProposalError, match="model_output_budget"),
    ):
        probe.generate(client, "synthetic")


def test_empty_output_cannot_pass_an_expected_task():
    result = probe.compare(CandidateLedger(), [probe.expected(["rapor"], "Derya")])
    assert not result["candidate_relation_pass"]
    assert result["missing_or_ambiguous_work"] == 1
