"""No inference: transport bounds, fingerprint drift and report integrity."""

import json

import httpx
import pytest

from experiments.task_state import indexed_probe as probe
from experiments.task_state.prototype import InvalidProposalError

MODEL = "qwen2.5:3b-instruct"


def client_for(chunks):
    return httpx.Client(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content="\n".join(json.dumps(c) for c in chunks))
        ),
    )


def test_stream_waits_for_done_and_materializes_response_without_logging(capsys):
    with client_for(
        [
            {"model": MODEL, "response": '{"events":', "done": False},
            {
                "model": MODEL,
                "response": '[],"unresolved":[]}',
                "done": True,
                "done_reason": "stop",
                "eval_count": 9,
            },
        ]
    ) as client:
        output = probe.generate(client, MODEL, "synthetic")
    assert json.loads(output["response"]) == {"events": [], "unresolved": []}
    assert output["eval_count"] == 9
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "chunks,reason",
    [
        ([], "generation_end_unobserved"),
        ([{"model": MODEL, "done": True, "done_reason": "length"}], "generation_incomplete"),
        ([{"model": MODEL, "done": True}], "generation_stop_unverified"),
        ([{"model": "other", "done": True}], "response_model_mismatch"),
        ([{"model": MODEL, "response": "x" * 524289}], "model_output_budget"),
    ],
)
def test_invalid_or_unobserved_generation_rejects(chunks, reason):
    with client_for(chunks) as client, pytest.raises(InvalidProposalError, match=reason):
        probe.generate(client, MODEL, "synthetic")


def test_absolute_generation_deadline(monkeypatch):
    readings = iter([0, 181])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(readings))
    with (
        client_for([{"model": MODEL, "done": True}]) as client,
        pytest.raises(InvalidProposalError, match="generation_deadline_unobserved"),
    ):
        probe.generate(client, MODEL, "synthetic")


def test_pin_is_checked_before_inference():
    def respond(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "test"})
        return httpx.Response(200, json={"models": [{"name": MODEL, "digest": "wrong"}]})

    with (
        httpx.Client(base_url="http://local", transport=httpx.MockTransport(respond)) as client,
        pytest.raises(InvalidProposalError, match="model_fingerprint_mismatch"),
    ):
        probe.fingerprint(client, MODEL)


def test_timeout_stops_following_inference_and_report_is_unqualified(monkeypatch):
    monkeypatch.setattr(
        probe,
        "cases",
        lambda: [("first", "a", "Ece çalışacak.", []), ("second", "b", "Ada çalışacak.", [])],
    )
    monkeypatch.setattr(probe, "fingerprint", lambda client, model: {"model": model})
    calls = []

    def timed_out(*args):
        calls.append(True)
        raise httpx.ReadTimeout("sensitive source must never enter output")

    monkeypatch.setattr(probe, "generate", timed_out)
    # Final runtime metadata is mocked as well: no network in this unit test.
    monkeypatch.setattr(
        httpx.Client,
        "get",
        lambda *a, **k: httpx.Response(
            200, json={"models": []}, request=httpx.Request("GET", "http://local")
        ),
    )
    report = probe.run(MODEL, "all")
    assert len(calls) == 1 and report["stopped_after_error"]
    assert not report["candidate_gate_passed"] and not report["semantic_qualification"]
    assert "sensitive source" not in json.dumps(report)


def test_code_drift_invalidates_even_a_relation_match(monkeypatch):
    monkeypatch.setattr(probe, "cases", lambda: [("only", "a", "Bugün görev yok.", [])])
    monkeypatch.setattr(probe, "fingerprint", lambda client, model: {"model": model})
    monkeypatch.setattr(
        probe, "generate", lambda *args: {"response": '{"events":[],"unresolved":[]}'}
    )
    hashes = iter([{"code": "before"}, {"code": "after"}])
    monkeypatch.setattr(probe, "code_hashes", lambda: next(hashes))
    monkeypatch.setattr(
        httpx.Client,
        "get",
        lambda *a, **k: httpx.Response(
            200, json={"models": []}, request=httpx.Request("GET", "http://local")
        ),
    )
    report = probe.run(MODEL, "all")
    assert report["rows"][0]["candidate_relation_pass"]
    assert not report["code_stable"] and not report["local_relation_cases_passed"]


def test_replay_mode_revisits_failed_prior_source_without_publishing_state(monkeypatch):
    from experiments.task_state.probe import expected

    monkeypatch.setattr(
        probe,
        "cases",
        lambda: [
            ("missed", "same", "Ece raporu hazırlayacak.", [expected(["raporu"], "Ece")]),
            (
                "recover",
                "same",
                "Ece raporu hazırlayacak. Başka konu yok.",
                [expected(["raporu"], "Ece")],
            ),
        ],
    )
    monkeypatch.setattr(probe, "fingerprint", lambda client, model: {"model": model})
    prompts = []

    def generate(client, model, prompt):
        prompts.append(prompt)
        events = (
            []
            if len(prompts) == 1
            else [
                {
                    "op": "create",
                    "target": None,
                    "at": 1,
                    "support": [1],
                    "work": [1, 2, 2],
                    "owner": [1, 1, 1],
                    "date": None,
                    "time": None,
                }
            ]
        )
        return {"response": json.dumps({"events": events, "unresolved": []})}

    monkeypatch.setattr(probe, "generate", generate)
    monkeypatch.setattr(
        httpx.Client,
        "get",
        lambda *a, **k: httpx.Response(
            200, json={"models": []}, request=httpx.Request("GET", "http://local")
        ),
    )
    report = probe.run(MODEL, "all", "replay")
    assert len(prompts) == 2 and all("TASKS=[]" in p for p in prompts)
    assert not report["rows"][0]["candidate_relation_pass"]
    assert report["rows"][1]["candidate_relation_pass"]
    assert report["mode"] == "replay" and not report["semantic_qualification"]
