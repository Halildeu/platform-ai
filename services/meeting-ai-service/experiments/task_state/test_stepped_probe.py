"""No inference: runner stops on its first failed case and exports no content."""

import json
from types import SimpleNamespace

import httpx
import pytest

from experiments.task_state import stepped_probe as probe
from experiments.task_state.probe import expected
from experiments.task_state.prototype import InvalidProposalError


@pytest.mark.parametrize("failure", ["relations", "timeout", "schema", "quote", "unresolved"])
def test_failure_never_queues_next_case_or_accepts_a_partial_result(monkeypatch, failure):
    monkeypatch.setattr(
        probe,
        "selected_cases",
        lambda: [
            ("a", "a", "Aylin raporu hazırlayacak.", [expected(["rapor"], "Aylin")]),
            ("b", "b", "Başka konu yok.", []),
        ],
    )
    if failure == "unresolved":
        monkeypatch.setattr(
            probe,
            "selected_cases",
            lambda: [
                ("a", "a", "Onun görevi değişti. Sonra yeni konu açıldı.", []),
                ("b", "b", "Başka konu yok.", []),
            ],
        )
    monkeypatch.setattr(probe, "fingerprint", lambda *a: {"model": probe.ALTERNATIVE})
    monkeypatch.setattr(probe, "require_cpu_runtime", lambda *a: None)
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda *a, **k: httpx.Response(
            200,
            json={"capabilities": ["thinking"]},
            request=httpx.Request("POST", "http://local"),
        ),
    )
    calls = []

    def generate(*args, **kwargs):
        calls.append(kwargs)
        embedded = args[2].split("\nYANIT ŞEMASI=", 1)[1].split("\nGÖREVLER=", 1)[0]
        assert json.loads(embedded) == kwargs["schema"]
        if failure == "timeout":
            raise InvalidProposalError("generation_deadline_unobserved")
        if failure == "schema":
            return {"response": "private unparseable content"}
        result = {"status": "no_event", "events": []}
        if failure == "unresolved":
            result["status"] = "unresolved"
        if failure == "quote":
            result = {
                "status": "events",
                "events": [
                    {
                        "kind": "create",
                        "target": 0,
                        "work": "private invalid quote",
                        "owner": None,
                        "date": None,
                        "time": None,
                    }
                ],
            }
        return {"response": json.dumps(result)}

    monkeypatch.setattr(probe, "generate", generate)
    result = probe.run()
    assert len(calls) == 1 and result["stopped_after_failure"]
    assert len(result["rows"]) == 1 and not result["local_relation_cases_passed"]
    assert not result["semantic_qualification"] and not result["phone_acceptance"]
    assert "private" not in json.dumps(result)
    assert calls[0]["options"]["num_gpu"] == 0 and calls[0]["deadline_seconds"] == 90
    attempt = result["rows"][0]["steps"][0]
    assert attempt["focus"] == 1 and attempt["prompt_sha256"]
    if failure == "timeout":
        assert result["rows"][0]["failure_stage"] == "generation"
    if failure == "quote":
        assert result["rows"][0]["quote_diagnostics"][0]["reason"] == "missing"


@pytest.mark.parametrize("elapsed, expected_budget", [(850, 50), (899.8, None)])
def test_remaining_overall_budget_limits_or_prevents_the_next_call(
    monkeypatch, elapsed, expected_budget
):
    clock_calls = []

    def now():
        clock_calls.append(True)
        return 0 if len(clock_calls) == 1 else elapsed

    monkeypatch.setattr(probe, "time", SimpleNamespace(monotonic=now))
    monkeypatch.setattr(probe, "selected_cases", lambda: [("a", "a", "Toplantı açıldı.", [])])
    monkeypatch.setattr(probe, "fingerprint", lambda *a: {})
    monkeypatch.setattr(probe, "require_cpu_runtime", lambda *a: None)
    budgets = []

    def generate(*args, **kwargs):
        budgets.append(kwargs["deadline_seconds"])
        assert args[0].timeout.read == kwargs["deadline_seconds"]
        assert kwargs["think"] is None
        return {"response": '{"status":"no_event","events":[]}'}

    monkeypatch.setattr(probe, "generate", generate)
    result = probe.run("llama3.1:8b")
    assert budgets == ([] if expected_budget is None else [expected_budget])
    if expected_budget is None:
        assert result["rows"][0]["reason_code"] == "run_time_budget"


def test_unsupported_model_is_rejected_before_any_runtime_call():
    with pytest.raises(InvalidProposalError, match="unsupported_probe_profile"):
        probe.run("unapproved-model")
