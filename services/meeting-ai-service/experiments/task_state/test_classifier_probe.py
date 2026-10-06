"""Harness safety and frozen oracle checks, not model accuracy."""

import json

import pytest

from experiments.task_state import classifier_probe as probe


def test_existing_report_prevents_any_model_call(monkeypatch, tmp_path):
    path = tmp_path / "report.json"
    path.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(probe, "run", lambda: pytest.fail("inference must not run"))
    with pytest.raises(SystemExit, match="already exists"):
        probe.main(path)
    assert path.read_text(encoding="utf-8") == "existing"


def test_expected_labels_never_enter_prompt():
    for case in probe.cases():
        mutated = {**case, "expected": "SECRET_ORACLE"}
        assert "SECRET_ORACLE" not in probe.prompt(mutated)
        assert case["text"] in probe.prompt(case)
    assert len(probe.cases()) == 20


@pytest.mark.parametrize("failure", ["mismatch", "invalid", "timeout"])
def test_first_failure_stops_before_next_call_without_ledger_or_content(monkeypatch, failure):
    monkeypatch.setattr(probe, "fingerprint", lambda *a: {})
    monkeypatch.setattr(probe, "require_cpu_runtime", lambda *a: None)
    calls = []

    def generate(*args, **kwargs):
        calls.append(kwargs)
        if failure == "timeout":
            raise TimeoutError("PRIVATE SOURCE")
        return {"response": '{"kind":"none"}' if failure == "mismatch" else "PRIVATE SOURCE"}

    monkeypatch.setattr(probe, "generate", generate)
    report = probe.run()
    assert len(calls) == 1
    assert report["stopped_after_failure"] and not report["classifier_cases_passed"]
    assert not report["semantic_qualification"] and not report["phone_acceptance"]
    assert calls[0]["deadline_seconds"] <= 60
    assert calls[0]["options"]["num_gpu"] == 0
    assert "PRIVATE SOURCE" not in json.dumps(report)


def test_all_coarse_labels_still_do_not_qualify_task_extraction(monkeypatch):
    monkeypatch.setattr(probe, "fingerprint", lambda *a: {})
    monkeypatch.setattr(probe, "require_cpu_runtime", lambda *a: None)
    labels = iter(case["expected"] for case in probe.cases())
    monkeypatch.setattr(
        probe, "generate", lambda *a, **k: {"response": json.dumps({"kind": next(labels)})}
    )
    report = probe.run()
    assert report["classifier_cases_passed"]
    assert not report["semantic_qualification"] and not report["phone_acceptance"]
