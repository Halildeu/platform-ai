"""Runner failures and metadata evidence must stay observable without transcripts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import meeting_semantic_eval as runner

from app.core.config import Settings
from app.models.schemas import AnalyzeResponse
from app.services.semantic_eval import load_corpus

FIXTURE = Path(__file__).parents[1] / "fixtures" / "meeting-semantic-gold-v1.json"


def test_backend_errors_count_even_for_no_decision_gold() -> None:
    corpus = load_corpus(FIXTURE)
    service = Mock()
    service.analyze.side_effect = RuntimeError("sensitive provider contents must not escape")
    report = runner.run_corpus(service, corpus)
    assert report["aggregate"]["error_count"] == 12
    assert runner.project_gate(report, True) is False
    encoded = json.dumps(report)
    assert "sensitive" not in encoded
    assert "RuntimeError" in encoded


def test_missing_fingerprint_fails_gate_even_when_metrics_pass() -> None:
    report = {
        "aggregate": {
            "error_count": 0,
            **{
                kind: {"precision": 1.0, "recall": 1.0}
                for kind in ("decision", "action", "action_with_metadata")
            },
        }
    }
    assert runner.project_gate(report, False) is False
    assert runner.project_gate(report, True) is True
    report["aggregate"]["decision"]["precision"] = 0.8
    assert runner.project_gate(report, True) is False


def test_fingerprint_uses_exact_tag_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    def get(url: str, **_: object) -> httpx.Response:
        data = (
            {"version": "0.9"}
            if url.endswith("version")
            else {
                "models": [
                    {"name": "llama3.1:8b", "digest": "sha256:exact"},
                    {"name": "other:latest", "digest": "sha256:wrong"},
                ]
            }
        )
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))

    monkeypatch.setattr(runner.httpx, "get", get)
    assert runner.fingerprint(Settings(backend="ollama")) == {
        "version": "0.9",
        "model_digest": "sha256:exact",
    }


def test_fingerprint_network_failure_does_not_invent_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.httpx, "get", Mock(side_effect=httpx.ConnectError("unavailable")))
    assert runner.fingerprint(Settings(backend="ollama"))["model_digest"] is None


@pytest.mark.parametrize("think", [None, False])
def test_main_writes_metadata_only_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    think: bool | None,
) -> None:
    output = tmp_path / "report.json"
    settings = Settings(backend="ollama", ollama_think=think)
    monkeypatch.setattr(runner, "Settings", lambda: settings)
    monkeypatch.setattr(
        runner, "fingerprint", lambda _: {"version": "0.9", "model_digest": "sha256:exact"}
    )
    service = Mock()
    service.analyze.return_value = AnalyzeResponse(
        summary="",
        decisions=[],
        action_items=[],
        redacted=True,
        redaction_count=0,
        backend="ollama",
        model="test",
        elapsed_ms=0,
    )
    monkeypatch.setattr(runner, "MeetingAnalysisService", lambda _: service)
    monkeypatch.setattr(
        sys, "argv", ["runner", "--eval-set", str(FIXTURE), "--output", str(output)]
    )
    assert runner.main() == 1
    report = json.loads(output.read_text())
    assert json.loads(capsys.readouterr().out) == report
    assert report["model_fingerprint_stable"] is True
    assert report["inputs_stable"] is True
    assert report["ollama_options"]["temperature"] == 0.0
    assert report["ollama_think"] is think
    assert report["request_timeout_sec"] == settings.request_timeout == 60
    assert report["effective_prompt_sha256"] != report["legacy_prompt_sha256"]
    assert report["aggregate"]["decision"]["false_negative"] > 0


def test_mock_backend_is_not_quality_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "Settings", lambda: Settings(backend="mock"))
    monkeypatch.setattr(sys, "argv", ["runner", "--eval-set", str(FIXTURE)])
    with pytest.raises(SystemExit, match="2"):
        runner.main()
