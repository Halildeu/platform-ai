"""Tests for the Faz 24 diarization backend decision gate."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services/diarization-service/scripts"))

import diar_decision_gate as gate  # noqa: E402


def _sha(char: str = "d") -> str:
    return "sha256:" + (char * 64)


def _pilot_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "tag": "pilot-pyannote-collar025",
        "dataset_kind": "pilot-meeting",
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
        "evidence_hash": _sha(),
        "biometric_processing": False,
        "speaker_identity_mapping": False,
        "voiceprint_enabled": False,
    }
    row.update(overrides)
    return row


def _threshold_kwargs() -> dict[str, object]:
    return {
        "max_der": 0.30,
        "max_rtf": 0.15,
        "max_latency_ms": 2500.0,
        "max_peak_vram_delta_mb": 4096.0,
        "min_samples": 8,
    }


class DiarDecisionGateTests(unittest.TestCase):
    def test_gate_passes_with_complete_redacted_pilot_candidate(self) -> None:
        result = gate.evaluate_gate(rows=[_pilot_row()], **_threshold_kwargs())

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["selectedDiarization"]["backend"], "pyannote")
        self.assertEqual(result["selectedDiarization"]["evidence_hash"], _sha())

    def test_synthetic_snapshot_stays_blocked(self) -> None:
        result = gate.evaluate_gate(
            rows=[
                {
                    "tag": "pyannote-collar025",
                    "fixture_kind": "synthetic-smoke",
                    "backend": "pyannote",
                    "model": "pyannote/speaker-diarization-3.1",
                    "n_samples": 6,
                    "der_corpus": 0.478,
                    "lat_max_ms": 1734,
                    "rtf": 0.024,
                    "peak_vram_delta_mb": 2155,
                }
            ],
            **_threshold_kwargs(),
        )

        self.assertEqual(result["status"], "blocked")
        self.assertTrue(
            any("synthetic/lab evidence" in item for item in result["findings"])
        )

    def test_missing_thresholds_block_decision(self) -> None:
        result = gate.evaluate_gate(
            rows=[_pilot_row()],
            max_der=None,
            max_rtf=0.15,
            max_latency_ms=2500.0,
            max_peak_vram_delta_mb=4096.0,
            min_samples=8,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertTrue(
            any("thresholds are required" in item for item in result["findings"])
        )

    def test_raw_content_rejected_without_echo(self) -> None:
        result = gate.evaluate_gate(
            rows=[_pilot_row(rttm="SPEAKER file 1 0 1 <NA> <NA> Ali <NA> <NA>")],
            **_threshold_kwargs(),
        )

        self.assertEqual(result["status"], "fail")
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertIn("rttm", rendered)
        self.assertNotIn("Ali", rendered)

    def test_non_biometric_boundary_is_enforced(self) -> None:
        result = gate.evaluate_gate(
            rows=[_pilot_row(voiceprint_enabled=True)],
            **_threshold_kwargs(),
        )

        self.assertEqual(result["status"], "fail")
        self.assertTrue(
            any("voiceprint_enabled=true" in item for item in result["findings"])
        )

    def test_biometric_processing_and_identity_mapping_fail(self) -> None:
        for key in ("biometric_processing", "speaker_identity_mapping"):
            with self.subTest(key=key):
                result = gate.evaluate_gate(
                    rows=[_pilot_row(**{key: True})],
                    **_threshold_kwargs(),
                )

                self.assertEqual(result["status"], "fail")
                self.assertTrue(
                    any(f"{key}=true" in item for item in result["findings"])
                )

    def test_rejected_license_fails(self) -> None:
        result = gate.evaluate_gate(
            rows=[_pilot_row(license_status="forbidden")],
            **_threshold_kwargs(),
        )

        self.assertEqual(result["status"], "fail")
        self.assertTrue(
            any("license_status=forbidden" in item for item in result["findings"])
        )

    def test_public_cloud_deployment_fails(self) -> None:
        result = gate.evaluate_gate(
            rows=[_pilot_row(deployment_mode="public-cloud")],
            **_threshold_kwargs(),
        )

        self.assertEqual(result["status"], "fail")
        self.assertTrue(
            any("deployment_mode=public-cloud" in item for item in result["findings"])
        )

    def test_invalid_evidence_hash_blocks_pilot_decision(self) -> None:
        result = gate.evaluate_gate(
            rows=[_pilot_row(evidence_hash="sha256:not-a-real-hash")],
            **_threshold_kwargs(),
        )

        self.assertEqual(result["status"], "blocked")
        self.assertTrue(
            any("evidence_hash must be sha256" in item for item in result["findings"])
        )

    def test_pii_shaped_value_rejected_without_echo(self) -> None:
        result = gate.evaluate_gate(
            rows=[_pilot_row(operator_note="ali@example.com")],
            **_threshold_kwargs(),
        )

        self.assertEqual(result["status"], "fail")
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertIn("PII-shaped", rendered)
        self.assertNotIn("ali@example.com", rendered)

    def test_above_threshold_pilot_fails(self) -> None:
        result = gate.evaluate_gate(
            rows=[_pilot_row(der_corpus=0.44)],
            **_threshold_kwargs(),
        )

        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("DER" in item for item in result["findings"]))

    def test_cli_writes_blocked_result_for_current_synthetic_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            evidence_path = temp_path / "diar.jsonl"
            evidence_path.write_text(
                json.dumps(
                    {
                        "tag": "pyannote-collar025",
                        "fixture_kind": "synthetic-smoke",
                        "backend": "pyannote",
                        "model": "pyannote/speaker-diarization-3.1",
                        "n_samples": 6,
                        "der_corpus": 0.478,
                        "lat_max_ms": 1734,
                        "rtf": 0.024,
                        "peak_vram_delta_mb": 2155,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            original_argv = sys.argv
            try:
                sys.argv = [
                    "diar_decision_gate.py",
                    "--evidence",
                    str(evidence_path),
                    "--max-der",
                    "0.30",
                    "--max-rtf",
                    "0.15",
                    "--max-latency-ms",
                    "2500",
                    "--max-peak-vram-delta-mb",
                    "4096",
                    "--min-samples",
                    "8",
                ]
                with redirect_stdout(StringIO()) as stdout:
                    exit_code = gate.main()
            finally:
                sys.argv = original_argv

            self.assertEqual(exit_code, 1)
            saved = json.loads(stdout.getvalue())
            self.assertEqual(saved["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
