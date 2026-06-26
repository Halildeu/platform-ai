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
    if layer_id.startswith("minio."):
        bucket_by_layer = {
            "minio.meeting-audio": "meeting-audio",
            "minio.transcripts": "transcripts",
            "minio.audit-archive": "audit-archive",
        }
        layer.update(
            {
                "bucket_name": bucket_by_layer[layer_id],
                "observed_expiration_days": days,
                "observed_rule_status": "Enabled",
                "runtime_environment": "test",
                "lifecycle_export_ref": f"mc-ilm-export/{layer_id}",
                "runtime_evidence_ref": f"runtime/{layer_id}",
                "evidence_payload": "metadata-only",
            }
        )
    if layer_id.startswith("db."):
        timestamp_by_layer = {
            "db.transcript-records": "created_at",
            "db.meeting-intelligence": "created_at",
            "db.kvkk-access-log": "accessed_at",
        }
        layer.update(
            {
                "persistence_ref": f"schema/{layer_id}",
                "cleanup_job_ref": f"job/{layer_id}",
                "destruction_audit_ref": f"audit/{layer_id}",
                "tenant_id_field": "tenant_id",
                "retention_timestamp_field": timestamp_by_layer[layer_id],
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

    def test_current_source_only_state_blocks_without_minio_runtime_and_verbis(
        self,
    ) -> None:
        evidence = {
            "schema": retention_gate.SCHEMA,
            "evidence_id": "current",
            "verbis": {"status": "pending"},
            "layers": [
                {
                    "id": "minio.meeting-audio",
                    "status": "blocked",
                    "mechanism": "s3-lifecycle",
                    "retention_days": 7,
                    "evidence_ref": "source lifecycle script exists; runtime export pending",
                },
                {
                    "id": "minio.transcripts",
                    "status": "blocked",
                    "mechanism": "s3-lifecycle",
                    "retention_days": 365,
                    "evidence_ref": "source lifecycle script exists; runtime export pending",
                },
                {
                    "id": "minio.audit-archive",
                    "status": "blocked",
                    "mechanism": "s3-lifecycle",
                    "retention_days": 2557,
                    "evidence_ref": "source lifecycle script exists; runtime export pending",
                },
                {
                    "id": "db.transcript-records",
                    "status": "active",
                    "mechanism": "db-cleanup-job",
                    "retention_days": 365,
                    "evidence_ref": "gitops runtime smoke",
                    "persistence_ref": "transcript-service V1/V3",
                    "cleanup_job_ref": "TranscriptRetentionCleanupService",
                    "destruction_audit_ref": "gitops#2041",
                    "tenant_id_field": "tenant_id",
                    "retention_timestamp_field": "created_at",
                    "audit_payload": "metadata-only",
                },
                {
                    "id": "db.meeting-intelligence",
                    "status": "active",
                    "mechanism": "db-cleanup-job",
                    "retention_days": 730,
                    "evidence_ref": "gitops runtime smoke",
                    "persistence_ref": "meeting-service V1/V2",
                    "cleanup_job_ref": "MeetingRetentionCleanupService",
                    "destruction_audit_ref": "gitops#2041",
                    "tenant_id_field": "tenant_id",
                    "retention_timestamp_field": "created_at",
                    "audit_payload": "metadata-only",
                },
                {
                    "id": "db.kvkk-access-log",
                    "status": "active",
                    "mechanism": "db-cleanup-job",
                    "retention_days": 730,
                    "evidence_ref": "gitops runtime smoke",
                    "persistence_ref": "transcript-service V1/V3",
                    "cleanup_job_ref": "TranscriptRetentionCleanupService",
                    "destruction_audit_ref": "gitops#2041",
                    "tenant_id_field": "tenant_id",
                    "retention_timestamp_field": "accessed_at",
                    "audit_payload": "metadata-only",
                },
            ],
        }

        result = retention_gate.evaluate_gate(evidence)

        self.assertEqual(result["status"], "blocked")
        self.assertGreaterEqual(result["blockerCount"], 4)
        self.assertTrue(
            any(
                "minio.meeting-audio status=blocked" in item
                for item in result["blockers"]
            )
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
            any(
                "minio.meeting-audio retention_days" in item
                for item in result["findings"]
            )
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
        self.assertTrue(
            any("destruction_audit_ref" in item for item in result["findings"])
        )

    def test_active_minio_claim_without_runtime_export_fails(self) -> None:
        evidence = _full_evidence()
        layers = evidence["layers"]
        assert isinstance(layers, list)
        layers[0] = {
            "id": "minio.meeting-audio",
            "status": "active",
            "mechanism": "s3-lifecycle",
            "retention_days": 7,
            "evidence_ref": "deploy/minio/setup-lifecycle.sh",
        }

        result = retention_gate.evaluate_gate(evidence)

        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("bucket_name" in item for item in result["findings"]))
        self.assertTrue(
            any("observed_rule_status" in item for item in result["findings"])
        )
        self.assertTrue(
            any("lifecycle_export_ref" in item for item in result["findings"])
        )
        self.assertTrue(
            any("runtime_evidence_ref" in item for item in result["findings"])
        )

    def test_kvkk_access_log_requires_accessed_at_timestamp_field(self) -> None:
        evidence = _full_evidence()
        layers = evidence["layers"]
        assert isinstance(layers, list)
        layers[5]["retention_timestamp_field"] = "created_at"

        result = retention_gate.evaluate_gate(evidence)

        self.assertEqual(result["status"], "fail")
        self.assertTrue(
            any(
                "retention_timestamp_field=accessed_at" in item
                for item in result["findings"]
            )
        )

    def test_privacy_fields_fail_without_echoing_values(self) -> None:
        evidence = _full_evidence()
        evidence["debug"] = {"transcript": "ali@example.com toplantıda karar verdi"}

        result = retention_gate.evaluate_gate(evidence)

        self.assertEqual(result["status"], "fail")
        self.assertTrue(
            any("disallowed content field" in item for item in result["findings"])
        )
        self.assertTrue(any("PII-shaped" in item for item in result["findings"]))
        self.assertTrue(
            all("ali@example.com" not in item for item in result["findings"])
        )

    def test_secret_fields_and_values_fail_without_echoing_values(self) -> None:
        evidence = _full_evidence()
        evidence["minio"] = {
            "access_key_id": "AKIA1234567890ABCDEF",
            "secret_key_ref": "vault://safe/ref",
        }

        result = retention_gate.evaluate_gate(evidence)

        self.assertEqual(result["status"], "fail")
        self.assertTrue(
            any("disallowed content field" in item for item in result["findings"])
        )
        self.assertTrue(
            any("secret-shaped value" in item for item in result["findings"])
        )
        self.assertTrue(
            all("AKIA1234567890ABCDEF" not in item for item in result["findings"])
        )

    def test_expected_layer_without_evaluator_fails_closed(self) -> None:
        original = retention_gate.EXPECTED_LAYERS.copy()
        try:
            retention_gate.EXPECTED_LAYERS["future.layer"] = {
                "retention_days": 1,
                "mechanism": "future-mechanism",
            }
            evidence = _full_evidence()
            layers = evidence["layers"]
            assert isinstance(layers, list)
            layers.append(
                {
                    "id": "future.layer",
                    "status": "active",
                    "mechanism": "future-mechanism",
                    "retention_days": 1,
                    "evidence_ref": "future/evidence",
                }
            )

            result = retention_gate.evaluate_gate(evidence)
        finally:
            retention_gate.EXPECTED_LAYERS.clear()
            retention_gate.EXPECTED_LAYERS.update(original)

        self.assertEqual(result["status"], "fail")
        self.assertTrue(
            any("future.layer has no evaluator" in item for item in result["findings"])
        )

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
