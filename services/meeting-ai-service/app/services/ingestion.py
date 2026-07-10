"""meeting-service aggregate-ingestion client — #244 AI-1 (Verdict A).

Calls platform-backend meeting-service's `POST /internal/v1/meetings/{meetingId}
/analysis-results` (BE-1) after `/analyze` completes, so the analysis becomes
durable in meeting-service instead of living only in the synchronous HTTP
response. Best-effort by design (see `submit_analysis_result`): a persistence
failure is logged/metriced but does NOT fail the `/analyze` response the
caller is waiting on — the analysis itself is still valid and useful even if
not yet durably stored.

KVKK: only metadata + the analyzer's already-redacted/grounded output crosses
this call. `transcript_revision` is a SHA-256 hash of the (pre-redaction)
transcript text, not the text itself — a hash pins "which transcript version"
without carrying content, matching the hash-only provenance pattern used
elsewhere in Faz 24 (diarization/STT evidence).
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

import httpx

from app.core.config import Settings
from app.models.schemas import AnalyzeResponse

logger = logging.getLogger(__name__)

# 4xx other than 401 (token expired mid-flight, worth one retry after refresh)
# are the caller's/payload's fault — retrying an unchanged payload cannot fix
# a 400/409/422, so retry only on network errors, 401, and 5xx.
_RETRYABLE_STATUS = {401, 429, 500, 502, 503, 504}


class IngestionOutcome(str, Enum):
    SUCCESS = "success"
    REPLAYED = "replayed"  # BE-1 idempotent replay of an already-accepted run
    SKIPPED_NO_MEETING_ID = "skipped_no_meeting_id"
    SKIPPED_NO_SESSION_ID = "skipped_no_session_id"
    SKIPPED_DISABLED = "skipped_disabled"
    FAILED = "failed"


@dataclass
class _CachedToken:
    access_token: str
    expires_at_monotonic: float


class ServiceTokenClient:
    """OAuth2 client_credentials token fetch + cache for the meeting-service call.

    The Keycloak client itself (id/secret, `meeting:analysis-result:write`
    scope grant) is provisioned by #244's GW/SEC slice (gitops/infra) — this
    class only consumes whatever credentials Settings is configured with.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cached: _CachedToken | None = None

    def get_token(self) -> str:
        now = time.monotonic()
        if self._cached is not None and self._cached.expires_at_monotonic > now:
            return self._cached.access_token

        resp = httpx.post(
            self._settings.meeting_service_token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._settings.meeting_service_client_id,
                "client_secret": self._settings.meeting_service_client_secret,
                "scope": self._settings.meeting_service_scope,
            },
            timeout=self._settings.ingestion_timeout_sec,
        )
        resp.raise_for_status()
        data = resp.json()
        access_token = str(data["access_token"])
        expires_in = float(data.get("expires_in", 60))
        # Refresh a bit early so an in-flight call never races token expiry.
        self._cached = _CachedToken(
            access_token=access_token,
            expires_at_monotonic=now + max(expires_in - 10.0, 5.0),
        )
        return access_token

    def invalidate(self) -> None:
        """Drop the cached token (called after a 401, before the retry)."""
        self._cached = None


def _transcript_revision(transcript: str) -> str:
    return hashlib.sha256(transcript.encode("utf-8")).hexdigest()


def _build_payload(
    *,
    settings: Settings,
    meeting_id: str,
    session_id: str,
    transcript: str,
    result: AnalyzeResponse,
    analysis_run_id: str,
    generated_at: datetime,
) -> dict[str, object]:
    return {
        "meetingId": meeting_id,
        "analysis_run_id": analysis_run_id,
        "transcript_id": session_id,
        "transcript_revision": _transcript_revision(transcript),
        "analyzer_contract_version": result.schema_version,
        "model_version": result.model,
        "prompt_version": settings.effective_prompt_version,
        "summary": result.summary,
        "decisions": result.decisions,
        "actions": [item.model_dump() for item in result.action_items],
        "citations": [c.model_dump() for c in result.citations],
        "rejected_claims": [r.model_dump() for r in result.rejected_claims],
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
    }


def submit_analysis_result(
    settings: Settings,
    token_client: ServiceTokenClient,
    *,
    meeting_id: str | None,
    session_id: str | None,
    transcript: str,
    result: AnalyzeResponse,
) -> IngestionOutcome:
    """Best-effort call to BE-1 after a successful `/analyze`.

    Never raises: a persistence failure is logged + returned as
    `IngestionOutcome.FAILED`, the caller (the `/analyze` endpoint) does not
    fail the user-visible response because of it. Retries reuse the SAME
    `analysis_run_id` across attempts (generated once, before the loop) so
    BE-1's idempotency contract treats them as one logical analysis, not N.
    """
    if not settings.ingestion_enabled:
        return IngestionOutcome.SKIPPED_DISABLED
    if not meeting_id:
        return IngestionOutcome.SKIPPED_NO_MEETING_ID
    if not session_id:
        # BE-1 requires a non-blank transcript_id; without a session id there
        # is nothing stable to key it on.
        logger.warning(
            "Ingestion skipped: no session_id to use as transcript_id",
            extra={"meeting_id": meeting_id},
        )
        return IngestionOutcome.SKIPPED_NO_SESSION_ID

    analysis_run_id = str(uuid.uuid4())
    generated_at = datetime.now(UTC)
    payload = _build_payload(
        settings=settings,
        meeting_id=meeting_id,
        session_id=session_id,
        transcript=transcript,
        result=result,
        analysis_run_id=analysis_run_id,
        generated_at=generated_at,
    )
    url = f"{settings.meeting_service_base_url}/internal/v1/meetings/{meeting_id}/analysis-results"

    last_error: Exception | None = None
    for attempt in range(1, settings.ingestion_max_attempts + 1):
        try:
            token = token_client.get_token()
            resp = httpx.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": analysis_run_id,
                },
                timeout=settings.ingestion_timeout_sec,
            )
        except httpx.HTTPError as exc:
            last_error = exc
            logger.warning(
                "Ingestion attempt %d/%d: network error (%s)",
                attempt,
                settings.ingestion_max_attempts,
                type(exc).__name__,
                extra={"meeting_id": meeting_id, "analysis_run_id": analysis_run_id},
            )
            continue

        if resp.status_code == 200:
            body = resp.json()
            replayed = bool(body.get("replayed", False))
            return IngestionOutcome.REPLAYED if replayed else IngestionOutcome.SUCCESS

        if resp.status_code == 401:
            # Token may have expired between cache-check and send; refresh
            # once and retry immediately (still counts against max_attempts).
            token_client.invalidate()

        if resp.status_code not in _RETRYABLE_STATUS:
            # 400 / 409 IDEMPOTENCY_CONFLICT / 409 STALE_TRANSCRIPT_ANALYSIS /
            # 422 — retrying an unchanged payload cannot fix these.
            logger.warning(
                "Ingestion terminal failure: HTTP %d",
                resp.status_code,
                extra={
                    "meeting_id": meeting_id,
                    "analysis_run_id": analysis_run_id,
                    "response_body": resp.text[:500],
                },
            )
            return IngestionOutcome.FAILED

        last_error = RuntimeError(f"HTTP {resp.status_code}")
        logger.warning(
            "Ingestion attempt %d/%d: retryable HTTP %d",
            attempt,
            settings.ingestion_max_attempts,
            resp.status_code,
            extra={"meeting_id": meeting_id, "analysis_run_id": analysis_run_id},
        )

    logger.error(
        "Ingestion failed after %d attempts (%s)",
        settings.ingestion_max_attempts,
        type(last_error).__name__ if last_error else "unknown",
        extra={"meeting_id": meeting_id, "analysis_run_id": analysis_run_id},
    )
    return IngestionOutcome.FAILED


_token_client: ServiceTokenClient | None = None


def get_token_client(settings: Settings) -> ServiceTokenClient:
    """Cached singleton so the token cache actually persists across requests."""
    global _token_client
    if _token_client is None:
        _token_client = ServiceTokenClient(settings)
    return _token_client
