"""END-TO-END PROOF: real abstractive LLM (Ollama) → verified-citation pipeline.

The product differentiator — verified span-grounding over a REAL LLM's abstractive
output — is only exercised when an actual LLM runs (the mock backend is extractive,
so its citations are trivial). This opt-in integration test wires `backend=ollama`
against a live Ollama and proves, on real model output:

  1. real abstractive output is produced (non-empty summary);
  2. EVERY shipped decision/action carries a PASSED citation;
  3. EVERY citation's hash/offset round-trips into the (redacted) transcript — the
     span is real, not model-invented;
  4. the explicit contract holds (grounding_policy=verified_only, summary prose is
     verified/partial/withheld before user exposure);
  5. any claim the LLM produced that is NOT grounded is WITHHELD into rejected_claims
     (the hallucination guard no competitor ships).

Opt-in: `pytest -m integration` with a reachable Ollama (env MAI_OLLAMA_HOST,
MAI_OLLAMA_MODEL). Skips cleanly when Ollama is absent (CI stays green).
"""

from __future__ import annotations

import hashlib
import os
from collections import Counter

import httpx
import pytest

pytestmark = pytest.mark.integration

OLLAMA_HOST = os.environ.get("MAI_OLLAMA_HOST", "http://localhost:11435")
OLLAMA_MODEL = os.environ.get("MAI_OLLAMA_MODEL", "qwen2.5:7b")

# Realistic Turkish meeting: an approved budget (+number), a REJECTED proposal
# (negative polarity), two owned actions. Exercises every gate on real LLM output.
TRANSCRIPT_CLEAN = (
    "Toplantı saat 14:00'te başladı. Mali müdür bütçe artışını sundu. "
    "Yönetim kurulu bütçenin yüzde 15 artırılmasını onayladı. "
    "Pazarlama ekibi yeni bir ofis açılmasını önerdi ancak bu öneri reddedildi. "
    "Ayşe Yılmaz tedarikçi sözleşmesini cuma gününe kadar hazırlayacak. "
    "Mehmet sunucu maliyetlerini bir sonraki toplantıya kadar analiz edecek. "
    "Toplantı saat 15:30'da sona erdi."
)


def _ollama_ready() -> bool:
    try:
        r = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=3)
        if r.status_code != 200:
            return False
        names = [m.get("name", "") for m in r.json().get("models", [])]
        return any(OLLAMA_MODEL.split(":")[0] in n for n in names)
    except httpx.HTTPError:
        return False


requires_ollama = pytest.mark.skipif(
    not _ollama_ready(), reason=f"Ollama+{OLLAMA_MODEL} not reachable at {OLLAMA_HOST}"
)


@requires_ollama
def test_real_ollama_every_shipped_claim_is_verified(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MAI_BACKEND", "ollama")
    monkeypatch.setenv("MAI_OLLAMA_HOST", OLLAMA_HOST)
    monkeypatch.setenv("MAI_OLLAMA_MODEL", OLLAMA_MODEL)
    monkeypatch.setenv("MAI_REQUEST_TIMEOUT", "180")

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.post("/analyze", json={"transcript": TRANSCRIPT_CLEAN})

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # (1) real abstractive output from the LLM
    assert body["backend"] == "ollama"
    assert len(body["summary"]) > 0

    # (4) explicit verified-only contract
    assert body["grounding_policy"] == "verified_only"
    assert body["summary_grounding_status"] in {"verified", "partial_verified", "withheld"}
    for c in body["summary_citations"]:
        assert c["status"] == "PASSED"
        assert c["grounded"] is True

    # (2)+(3) every shipped decision/action is backed by a PASSED citation whose
    # hash/offset round-trips into the transcript — proof the span is REAL.
    redacted = TRANSCRIPT_CLEAN  # no PII here → redaction is identity
    n_shipped = len(body["decisions"]) + len(body["action_items"])
    assert n_shipped >= 1, "expected the LLM to extract at least one grounded claim"
    # Codex 019ee9d1: count is not enough — the citation set must be EXACTLY the shipped
    # claim set (each shipped decision/action is the citation's claim), so no shipped
    # claim can be uncited and no citation can be orphaned.
    shipped_claims = body["decisions"] + [a["text"] for a in body["action_items"]]
    assert Counter(c["claim"] for c in body["citations"]) == Counter(shipped_claims)

    for c in body["citations"]:
        assert c["status"] == "PASSED"
        assert c["grounded"] is True
        start, end = c["source_char_start"], c["source_char_end"]
        assert 0 <= start < end <= len(redacted)
        span = redacted[start:end]
        assert span == c["source_text"], "citation offset must point at the cited span"
        assert hashlib.sha256(span.encode()).hexdigest() == c["source_hash"]

    # (5) any ungrounded LLM claim was WITHHELD, not shipped. `ungrounded_count`
    # preserves the v2 decision/action count; summary rejections are exposed by kind.
    assert body["ungrounded_count"] == len(
        [rc for rc in body["rejected_claims"] if rc["kind"] != "summary"]
    )
    for rc in body["rejected_claims"]:
        if rc["kind"] == "summary":
            assert rc["claim"] not in body["summary"]
        else:
            assert rc["claim"] not in body["decisions"]
            assert rc["claim"] not in [a["text"] for a in body["action_items"]]
