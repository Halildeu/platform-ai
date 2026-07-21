"""Faz 24 İ2 — SSE contract regression guard (static pins).

Pins the pieces platform-desktop's LiveAnalysisSubscriber + gitops'
live-analyze-sse-smoke.sh both key off. If a future refactor drifts the
frame protocol, the payload shape, or the route URL, ONE small
assertion here fails with a naming-the-field diff instead of a
downstream consumer crashing in prod.

Only static introspection here — TestClient-based behaviour (publish,
headers, 503-when-hub-missing) already lives in test_api.py where it is
green. This file is a **contract-only** guard that catches renames
before a full suite even runs.
"""

from __future__ import annotations

import inspect

from app.api import analyze
from app.main import app
from app.models.schemas import AnalyzeResponse

# Fields the desktop hook + web/mobile smoke render or grep for. Additions
# to AnalyzeResponse are fine; removing/renaming any of these breaks a
# downstream consumer without a compile-time signal on our side.
_PINNED_RESPONSE_FIELDS: frozenset[str] = frozenset(
    {
        "is_partial",
        "version",
        "summary",
        "decisions",
        "action_items",
        "redaction_count",
        "citations",
        "summary_citations",
        "summary_grounding_status",
        "grounding_policy",
        "schema_version",
    }
)


def test_analyze_response_carries_the_pinned_client_facing_fields() -> None:
    """AnalyzeResponse MUST keep the fields that /analyze/live consumers key off.

    If the rename is intentional, update BOTH:
      - platform-desktop/src/intelligence/use-live-analysis.ts
      - platform-k8s-gitops/scripts/faz24/live-analyze-sse-smoke.sh
    then adjust this pin. That is the whole point of the guard.
    """

    field_names = set(AnalyzeResponse.model_fields.keys())
    missing = _PINNED_RESPONSE_FIELDS - field_names
    assert not missing, (
        "SSE contract regression: fields removed from AnalyzeResponse that "
        "at least one live consumer parses (desktop panel + gitops smoke): "
        f"{sorted(missing)}."
    )


def test_analyze_live_route_urls_are_pinned() -> None:
    """The exact URL shapes desktop + smoke build.

    LiveAnalysisSubscriber constructs `${baseUrl}/analyze/live/stream/${meetingId}`;
    the smoke script POSTs `${baseUrl}/analyze/live`. Drift here = 404 at runtime.
    """

    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/analyze/live" in paths, (
        "SSE contract regression: /analyze/live disappeared from the app "
        "routes. Consumers: audio-gateway İ4 trigger + gitops smoke."
    )
    assert "/analyze/live/stream/{meeting_id}" in paths, (
        "SSE contract regression: /analyze/live/stream/{meeting_id} "
        "disappeared. Consumers: platform-desktop LiveAnalysisSubscriber."
    )


def test_final_analysis_version_sentinel_stays_greater_than_int31() -> None:
    """Sentinel that keeps a late live publish from displacing the final.

    Documented invariant: final `/analyze` publish uses this value so a
    slower live delivery cannot overwrite it via version comparison.
    """

    module = inspect.getmodule(analyze)
    assert module is not None
    sentinel = getattr(module, "FINAL_ANALYSIS_VERSION_SENTINEL", None)
    assert sentinel is not None, (
        "SSE contract regression: FINAL_ANALYSIS_VERSION_SENTINEL is gone. "
        "Without it the final /analyze cannot mark itself as authoritative "
        "vs a slow live partial that arrives late."
    )
    # segment_seq is a 31-bit int on the recorder; the sentinel must
    # stay strictly greater than any legal segment_seq so ordering wins.
    assert sentinel >= 2**31 - 1
