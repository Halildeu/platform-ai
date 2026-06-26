"""Tests for the Faz 24 AI gate evidence ingest wrapper."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import faz24_ai_gate_ingest as ingest  # noqa: E402


def _sha(char: str) -> str:
    return "sha256:" + (char * 64)


def _gwer_envelope(*, dataset_kind: str = "pilot-meeting") -> dict[str, object]:
    return {
        "schema": "faz24.ai-gate-ingest.v1",
        "gate": "gwer",
        "thresholds": {"maxWer": 0.25, "maxDer": 0.30},
        "evidence": {
            "werRows": [
                {
                    "tag": "pilot-large-v3-turbo",
                    "dataset_kind": dataset_kind,
                    "model": "deepdml/faster-whisper-large-v3-turbo-ct2",
                    "compute": "float16",
                    "evidence_hash": _sha("e"),
                    "eval_set_hash": _sha("f"),
                    "n_samples": 8,
                    "wer": 0.18,
                    "ref_words": 1200,
                    "rtf": 0.08,
                    "p50_ms": 420,
                }
            ],
            "derRows": [
                {
                    "tag": "pilot-pyannote",
                    "fixture_kind": dataset_kind,
                    "backend": "pyannote",
                    "model": "pyannote/speaker-diarization-3.1",
                    "evidence_hash": _sha("a"),
                    "eval_set_hash": _sha("b"),
                    "n_samples": 8,
                    "der_corpus": 0.22,
                    "collar": 0.25,
                    "rtf": 0.04,
                    "p50_ms": 1200,
                }
            ],
        },
    }


def _glat_cost_envelope() -> dict[str, object]:
    return {
        "schema": "faz24.ai-gate-ingest.v1",
        "gate": "glat-cost",
        "thresholds": {
            "maxLatencyP95Ms": 2500.0,
            "maxQueueLagP95Ms": 500.0,
            "maxCostPerAudioMinute": 0.01,
            "maxRealtimeFactor": 0.35,
            "maxErrorRate": 0.01,
            "minAudioMinutes": 30.0,
            "minAudioMinutesPerWallHour": 600.0,
        },
        "evidence": {
            "rows": [
                {
                    "tag": "pilot-gpu-live-stt-k2",
                    "dataset_kind": "pilot-meeting",
                    "backend": "live-stt-service",
                    "model": "deepdml/faster-whisper-large-v3-turbo-ct2",
                    "compute": "float16",
                    "n_samples": 12,
                    "audio_minutes": 48.0,
                    "latency_p50_ms": 980.0,
                    "latency_p95_ms": 1800.0,
                    "queue_lag_p95_ms": 220.0,
                    "realtime_factor": 0.08,
                    "audio_minutes_per_wall_hour": 1500.0,
                    "cost_per_audio_minute": 0.0012,
                    "gpu_utilization_p95_pct": 76.0,
                    "error_rate": 0.0,
                    "evidence_hash": _sha("a"),
                }
            ]
        },
    }


def _gint_envelope() -> dict[str, object]:
    return {
        "schema": "faz24.ai-gate-ingest.v1",
        "gate": "gint",
        "thresholds": {
            "minGroundingRate": 0.95,
            "minActionPrecision": 0.80,
            "minActionRecall": 0.80,
            "minDecisionPrecision": 0.75,
            "minDecisionRecall": 0.75,
            "maxSchemaInvalidRate": 0.0,
            "maxFormatInvalidRate": 0.0,
            "maxBackendErrorRate": 0.0,
            "maxTruncationRiskRate": 0.0,
            "minSamples": 3,
        },
        "evidence": {
            "rows": [
                {
                    "tag": "ollama-pilot",
                    "backend": "ollama",
                    "model": "llama3.1:8b",
                    "dataset_kind": "pilot-meeting",
                    "eval_set": "C:/faz24-pilot/intel-pilot-2026-06-25.json",
                    "eval_set_hash": _sha("c"),
                    "prompt_hash": _sha("d"),
                    "n_samples": 8,
                    "grounding_rate": 1.0,
                    "action_precision": 0.86,
                    "action_recall": 0.83,
                    "action_f1": 0.845,
                    "decision_precision": 0.82,
                    "decision_recall": 0.80,
                    "decision_f1": 0.81,
                    "schema_invalid_rate": 0.0,
                    "format_invalid_rate": 0.0,
                    "backend_error_rate": 0.0,
                    "truncation_risk_rate": 0.0,
                    "p50_ms": 12516,
                }
            ]
        },
    }


def _diar_decision_envelope(
    *, dataset_kind: str = "pilot-meeting"
) -> dict[str, object]:
    return {
        "schema": "faz24.ai-gate-ingest.v1",
        "gate": "diar-decision",
        "thresholds": {
            "maxDer": 0.30,
            "maxRtf": 0.15,
            "maxLatencyMs": 2500.0,
            "maxPeakVramDeltaMb": 4096.0,
            "minSamples": 8,
        },
        "evidence": {
            "rows": [
                {
                    "tag": "pilot-pyannote",
                    "dataset_kind": dataset_kind,
                    "backend": "pyannote",
                    "model": "pyannote/speaker-diarization-3.1",
                    "revision": "abc123",
                    "device": "cuda",
                    "deployment_mode": "self-host",
                    "license_status": "commercial-approved",
                    "n_samples": 12,
                    "der_corpus": 0.21,
                    "der": 0.23,
                    "collar": 0.25,
                    "skip_overlap": False,
                    "lat_max_ms": 1900.0,
                    "rtf": 0.05,
                    "peak_vram_delta_mb": 2300.0,
                    "evidence_hash": _sha("d"),
                    "biometric_processing": False,
                    "speaker_identity_mapping": False,
                    "voiceprint_enabled": False,
                }
            ]
        },
    }


class Faz24AiGateIngestTests(unittest.TestCase):
    def test_gwer_envelope_passes_with_redacted_pilot_evidence(self) -> None:
        result = ingest.evaluate_envelope(_gwer_envelope())

        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["sourceInvoked"])
        self.assertEqual(result["sourceStatus"], "pass")
        self.assertEqual(result["sourceReport"]["selectedWer"]["value"], 0.18)

    def test_glat_cost_envelope_passes_with_redacted_pilot_evidence(self) -> None:
        result = ingest.evaluate_envelope(_glat_cost_envelope())

        self.assertEqual(result["status"], "pass")
        self.assertEqual(
            result["sourceReport"]["selectedGlatCost"]["evidence_hash"], _sha("a")
        )

    def test_gint_envelope_passes_with_redacted_pilot_evidence(self) -> None:
        result = ingest.evaluate_envelope(_gint_envelope())

        self.assertEqual(result["status"], "pass")
        self.assertEqual(
            result["sourceReport"]["selectedGint"]["kind"], "pilot-meeting"
        )

    def test_diar_decision_envelope_passes_with_redacted_pilot_evidence(self) -> None:
        result = ingest.evaluate_envelope(_diar_decision_envelope())

        self.assertEqual(result["status"], "pass")
        self.assertEqual(
            result["sourceReport"]["selectedDiarization"]["backend"], "pyannote"
        )

    def test_synthetic_evidence_stays_blocked(self) -> None:
        envelope = _gwer_envelope(dataset_kind="synthetic-smoke")

        result = ingest.evaluate_envelope(envelope)

        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["sourceInvoked"])
        self.assertTrue(
            any("no pilot-meeting WER row" in item for item in result["findings"])
        )

    def test_raw_content_rejected_before_source_dispatch_without_echo(self) -> None:
        envelope = _gint_envelope()
        rows = envelope["evidence"]["rows"]  # type: ignore[index]
        rows[0]["transcript"] = "must not be echoed"  # type: ignore[index]

        result = ingest.evaluate_envelope(envelope)

        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["sourceInvoked"])
        self.assertTrue(any("transcript" in item for item in result["findings"]))
        self.assertNotIn("must not be echoed", json.dumps(result, ensure_ascii=False))

    def test_secret_and_pii_rejected_before_source_dispatch_without_echo(self) -> None:
        envelope = _glat_cost_envelope()
        rows = envelope["evidence"]["rows"]  # type: ignore[index]
        rows[0]["operator_note"] = "Authorization: Bearer secret-token"  # type: ignore[index]
        rows[0]["support_ref"] = "ali@example.com"  # type: ignore[index]

        result = ingest.evaluate_envelope(envelope)

        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["sourceInvoked"])
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertIn("secret-shaped", rendered)
        self.assertIn("PII-shaped", rendered)
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("ali@example.com", rendered)

    def test_common_token_shapes_rejected_before_source_dispatch_without_echo(
        self,
    ) -> None:
        token_samples = [
            "sk-abcdefghijklmnopqrstuvwxyz123456",
            "xoxb-123456789012-ABCDEFGHIJKL",
            "ghp_abcdefghijklmnopqrstuvwxyz123456",
            "github_pat_1234567890abcdefghijklmnopqrstuvwxyz",
        ]

        for token in token_samples:
            with self.subTest(token_prefix=token[:4]):
                envelope = _glat_cost_envelope()
                rows = envelope["evidence"]["rows"]  # type: ignore[index]
                rows[0]["operator_note"] = f"token={token}"  # type: ignore[index]

                result = ingest.evaluate_envelope(envelope)

                self.assertEqual(result["status"], "fail")
                self.assertFalse(result["sourceInvoked"])
                rendered = json.dumps(result, ensure_ascii=False)
                self.assertIn("secret-shaped", rendered)
                self.assertNotIn(token, rendered)

    def test_unsupported_gate_is_rejected(self) -> None:
        envelope = _gwer_envelope()
        envelope["gate"] = "unknown"

        result = ingest.evaluate_envelope(envelope)

        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["sourceInvoked"])
        self.assertTrue(any("unsupported gate" in item for item in result["findings"]))

    def test_diar_decision_synthetic_evidence_stays_blocked(self) -> None:
        result = ingest.evaluate_envelope(
            _diar_decision_envelope(dataset_kind="synthetic-smoke")
        )

        self.assertEqual(result["status"], "blocked")
        self.assertTrue(
            any("synthetic/lab evidence" in item for item in result["findings"])
        )

    def test_cli_writes_redacted_result_file_and_returns_nonzero_for_blocked(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            evidence_path = temp_path / "evidence.json"
            output_path = temp_path / "result.json"
            evidence_path.write_text(
                json.dumps(_gwer_envelope(dataset_kind="perf-lab")),
                encoding="utf-8",
            )

            original_argv = sys.argv
            try:
                sys.argv = [
                    "faz24_ai_gate_ingest.py",
                    "--evidence-file",
                    str(evidence_path),
                    "--output-file",
                    str(output_path),
                ]
                with redirect_stdout(StringIO()):
                    exit_code = ingest.main()
            finally:
                sys.argv = original_argv

            self.assertEqual(exit_code, 1)
            saved = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "blocked")

    def test_workflow_upload_is_guarded_by_secret_scan_success(self) -> None:
        workflow = (
            ROOT / ".github/workflows/faz24-ai-gate-evidence-ingest.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("evidence_json_base64", workflow)
        self.assertIn("steps.secret_scan.outcome == 'success'", workflow)
        self.assertIn(
            "path: ${{ steps.decode.outputs.artifact_dir }}/result.json", workflow
        )
        self.assertIn("-e 'Bearer[[:space:]]+", workflow)
        self.assertIn("-- \\", workflow)
        self.assertNotIn("path: ${{ steps.decode.outputs.input_dir }}", workflow)


if __name__ == "__main__":
    unittest.main()
