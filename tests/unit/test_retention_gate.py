"""Tests for the Faz 24 KVKK retention readiness gate."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import retention_gate  # noqa: E402


def _active_layer(layer_id: str, days: int, mechanism: str) -> dict[str, object]:
    layer: dict[str, object] = {
        "id": layer_id,
        "status": "active",
        "mechanism": mechanism,
        "retention_days": days,
        "evidence_ref": f"evidence/{layer_id}",
    }
    if layer_id.startswith("db."):
        layer.update(
            {
                "persistence_ref": f"schema/{layer_id}",
                "cleanup_job_ref": f"job/{layer_id}",
                "destruction_audit_ref": f"audit/{layer_id}",
                "tenant_id_field": "tenant_id",
                "created_at_field": "created_at",
                "audit_payload": "metadata-only",
            }
        )
    return layer


def _full_evidence() -> dict[str, object]:
    return {
        "schema": retention_gate.SCHEMA,
        "evidence_id": "retention-gate-test",
        "verbis": {"status": "recorded", "reference": "operator-log"},
        "layers": [
            _active_layer("minio.meeting-audio", 7, "s3-lifecycle"),
            _active_layer("minio.transcripts", 365, "s3-lifecycle"),
            _active_layer("minio.audit-archive", 2557, "s3-lifecycle"),
            _active_layer("db.transcript-records", 365, "db-cleanup-job"),
            _active_layer("db.meeting-intelligence", 730, "db-cleanup-job"),
            _active_layer("db.kvkk-access-log", 730, "db-cleanup-job"),
        ],
    }


class RetentionGateTest(unittest.TestCase):
    def test_gate_passes_with_all_required_layers(self) -> None:
        result = retention_gate.evaluate_gate(_full_evidence())

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["findingCount"], 0)
        self.assertEqual(result["blockerCount"], 0)

    def test_current_minio_only_state_blocks_without_db_and_verbis(self) -> None:
        evidence = {
            "schema": retention_gate.SCHEMA,
            "evidence_id": "current",
            "verbis": {"status": "pending"},
            "layers": [
                _active_layer("minio.meeting-audio", 7, "s3-lifecycle"),
                _active_layer("minio.transcripts", 365, "s3-lifecycle"),
                _active_layer("minio.audit-archive", 2557, "s3-lifecycle"),
                {
                    "id": "db.transcript-records",
                    "status": "pending",
                    "mechanism": "db-cleanup-job",
                    "retention_days": 365,
                },
                {
                    "id": "db.meeting-intelligence",
                    "status": "pending",
                    "mechanism": "db-cleanup-job",
                    "retention_days": 730,
                },
                {
                    "id": "db.kvkk-access-log",
                    "status": "pending",
                    "mechanism": "db-cleanup-job",
                    "retention_days": 730,
                },
            ],
        }

        result = retention_gate.evaluate_gate(evidence)

        self.assertEqual(result["status"], "blocked")
        self.assertGreaterEqual(result["blockerCount"], 4)
        self.assertTrue(
            any("db.transcript-records status=pending" in item for item in result["blockers"])
        )
        self.assertTrue(any("VERBIS" in item for item in result["blockers"]))

    def test_duration_mismatch_fails(self) -> None:
        evidence = _full_evidence()
        layers = evidence["layers"]
        assert isinstance(layers, list)
        layers[0]["retention_days"] = 30

        result = retention_gate.evaluate_gate(evidence)

        self.assertEqual(result["status"], "fail")
        self.assertTrue(
            any("minio.meeting-audio retention_days" in item for item in result["findings"])
        )

    def test_active_db_claim_without_cleanup_and_audit_refs_fails(self) -> None:
        evidence = _full_evidence()
        layers = evidence["layers"]
        assert isinstance(layers, list)
        layers[3] = {
            "id": "db.transcript-records",
            "status": "active",
            "mechanism": "db-cleanup-job",
            "retention_days": 365,
            "evidence_ref": "schema/transcript",
        }

        result = retention_gate.evaluate_gate(evidence)

        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("cleanup_job_ref" in item for item in result["findings"]))
        self.assertTrue(any("destruction_audit_ref" in item for item in result["findings"]))

    def test_privacy_fields_fail_without_echoing_values(self) -> None:
        evidence = _full_evidence()
        evidence["debug"] = {"transcript": "ali@example.com toplantıda karar verdi"}

        result = retention_gate.evaluate_gate(evidence)

        self.assertEqual(result["status"], "fail")
        self.assertTrue(
            any("disallowed content field" in item for item in result["findings"])
        )
        self.assertTrue(any("PII-shaped" in item for item in result["findings"]))
        self.assertTrue(all("ali@example.com" not in item for item in result["findings"]))

    def test_repo_source_assertions_check_minio_lifecycle_script(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        result = retention_gate.evaluate_gate(_full_evidence(), repo_root=repo_root)

        self.assertEqual(result["status"], "pass")
        self.assertEqual(len(result["sourceAssertions"]), 3)
        self.assertTrue(
            all(item["status"] == "pass" for item in result["sourceAssertions"])
        )


if __name__ == "__main__":
    unittest.main()
