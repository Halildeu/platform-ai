"""Config unit tests."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings


def test_defaults() -> None:
    s = Settings()
    assert s.backend == "mock"
    assert s.redact_pii is True
    assert s.request_timeout == 60
    assert s.ingestion_enabled is False
    assert s.ready_consumer_enabled is False


def test_backend_pattern_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        Settings(backend="gemini")


def test_request_timeout_bounds() -> None:
    with pytest.raises(ValidationError):
        Settings(request_timeout=0)


def test_redaction_cannot_be_disabled_on_real_backend() -> None:
    """Issue #49 KVKK boundary: redaction is mandatory for any non-mock backend."""
    for backend in ("anthropic", "openai", "ollama"):
        with pytest.raises(ValidationError):
            Settings(backend=backend, redact_pii=False)


def test_redaction_disable_allowed_only_on_mock() -> None:
    """Mock is in-process (no data leaves the service), so the dev switch is OK."""
    s = Settings(backend="mock", redact_pii=False)
    assert s.redact_pii is False
    # Real backends with redaction left on are fine too.
    assert Settings(backend="ollama", redact_pii=True).redact_pii is True


def _ingestion_values(tmp_path: Path) -> dict[str, object]:
    return {
        "ingestion_enabled": True,
        "meeting_service_base_url": "https://meeting.test",
        "meeting_service_token_url": "https://auth.test/token",
        "meeting_service_client_id": "meeting-ai",
        "meeting_service_client_secret": SecretStr("meeting-service-secret-value"),
        "ingestion_store_path": tmp_path / "outbox.sqlite3",
        "ingestion_active_key_id": "v1",
        "ingestion_encryption_keys_json": SecretStr(
            json.dumps({"v1": base64.b64encode(b"K" * 32).decode()})
        ),
        "ingestion_timeout_sec": 1.0,
        "ingestion_lease_sec": 3.0,
    }


def test_ingestion_enabled_requires_credentials_absolute_path_and_keyring(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(ingestion_enabled=True)

    values = _ingestion_values(tmp_path)
    values["ingestion_store_path"] = Path("relative.sqlite3")
    with pytest.raises(ValidationError):
        Settings(**values)

    values = _ingestion_values(tmp_path)
    values["ingestion_active_key_id"] = "missing"
    with pytest.raises(ValidationError):
        Settings(**values)


def test_ingestion_keyring_is_aes256_and_secret_repr_is_redacted(tmp_path: Path) -> None:
    values = _ingestion_values(tmp_path)
    settings = Settings(**values)
    assert settings.ingestion_encryption_keys() == {"v1": b"K" * 32}
    assert "meeting-service-secret-value" not in repr(settings)
    assert base64.b64encode(b"K" * 32).decode() not in repr(settings)

    values["ingestion_encryption_keys_json"] = SecretStr(
        json.dumps({"v1": base64.b64encode(b"short").decode()})
    )
    with pytest.raises(ValidationError):
        Settings(**values)


def test_ingestion_lease_must_cover_token_and_ingestion_timeouts(tmp_path: Path) -> None:
    values = _ingestion_values(tmp_path)
    values["ingestion_timeout_sec"] = 10.0
    values["ingestion_lease_sec"] = 20.0
    with pytest.raises(ValidationError, match="two HTTP timeout windows"):
        Settings(**values)


def test_ingestion_requires_https_without_embedded_credentials(tmp_path: Path) -> None:
    values = _ingestion_values(tmp_path)
    values["meeting_service_base_url"] = "http://meeting.test"
    with pytest.raises(ValidationError, match="absolute HTTPS URL"):
        Settings(**values)

    values = _ingestion_values(tmp_path)
    values["meeting_service_token_url"] = "https://user:secret@auth.test/token"
    with pytest.raises(ValidationError, match="without embedded credentials"):
        Settings(**values)


def test_mutual_tls_requires_readable_absolute_material(tmp_path: Path) -> None:
    values = _ingestion_values(tmp_path)
    values["meeting_service_tls_mode"] = "mutual"
    with pytest.raises(ValidationError, match="requires CA, client certificate"):
        Settings(**values)

    ca_path = tmp_path / "ca.pem"
    cert_path = tmp_path / "client.pem"
    key_path = tmp_path / "client.key"
    for path in (ca_path, cert_path, key_path):
        path.write_text("test-material", encoding="utf-8")
    values.update(
        {
            "meeting_service_tls_ca_path": ca_path,
            "meeting_service_tls_client_cert_path": cert_path,
            "meeting_service_tls_client_key_path": key_path,
        }
    )
    settings = Settings(**values)
    assert settings.meeting_service_tls_mode == "mutual"

    values["meeting_service_tls_client_key_path"] = tmp_path / "missing.key"
    with pytest.raises(ValidationError, match="readable file"):
        Settings(**values)


def _ready_values(tmp_path: Path) -> dict[str, object]:
    values = _ingestion_values(tmp_path)
    values.update(
        {
            "ready_consumer_enabled": True,
            "ready_producer_replay_horizon_sec": 604_800.0,
            "ready_redis_url": SecretStr("redis://redis.test:6379/0"),
            "transcript_service_base_url": "https://transcript.test",
            "transcript_service_snapshot_path_template": (
                "/api/v1/internal/tenants/{tenant_id}/meetings/{meeting_id}"
                "/sessions/{session_id}/finalizations/{finalization_version}"
            ),
            "transcript_service_token_url": "https://auth.test/token",
            "transcript_service_client_id": "meeting-ai-ready",
            "transcript_service_client_secret": SecretStr("transcript-secret-value"),
        }
    )
    return values


def test_ready_consumer_is_default_off_and_fails_closed_on_partial_config(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="requires MAI_INGESTION_ENABLED"):
        Settings(ready_consumer_enabled=True)

    values = _ready_values(tmp_path)
    values["transcript_service_snapshot_path_template"] = "/api/v1/internal/snapshot"
    with pytest.raises(ValidationError, match="placeholders"):
        Settings(**values)

    values = _ready_values(tmp_path)
    values["ready_consumer_lease_sec"] = 80.0
    with pytest.raises(ValidationError, match="analysis timeout"):
        Settings(**values)

    values = _ready_values(tmp_path)
    values["ready_redis_claim_idle_ms"] = 10_000
    with pytest.raises(ValidationError, match="claim idle time"):
        Settings(**values)

    values = _ready_values(tmp_path)
    values["ready_producer_replay_horizon_sec"] = 0.0
    with pytest.raises(ValidationError, match="explicitly configured"):
        Settings(**values)

    values = _ready_values(tmp_path)
    values["ready_producer_replay_horizon_sec"] = 3_000_000.0
    with pytest.raises(ValidationError, match="retention must be >="):
        Settings(**values)


def test_ready_consumer_secret_values_are_redacted_and_prod_requires_tls_redis(
    tmp_path: Path,
) -> None:
    values = _ready_values(tmp_path)
    settings = Settings(**values)
    assert settings.transcript_service_permissions == ["transcript:canonical:read"]
    assert "transcript-secret-value" not in repr(settings)
    assert "redis.test" not in repr(settings)

    values["app_env"] = "prod"
    values["backend"] = "ollama"
    with pytest.raises(ValidationError, match="rediss"):
        Settings(**values)
