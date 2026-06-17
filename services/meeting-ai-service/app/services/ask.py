"""#162 PR-llm-03: post-meeting ask-AI.

Answer a question about a meeting strictly from its (redacted) transcript, then
ground the answer to a source sentence (citation) and flag it if unsupported
(hallucination guard) — the same honesty contract as the analyze path.

Backends mirror analyze.py: `mock` (deterministic, no LLM — picks the best
sentence) and `ollama` (local LLM, Option B #54). KVKK: transcript is redacted
before the LLM sees it; only redacted text is grounded.
"""

from __future__ import annotations

import time

import httpx

from app.core.config import Settings
from app.models.schemas import AskResponse, Citation
from app.services.citation import ground_claim, split_sentences
from app.services.redact import redact_pii

_ASK_PROMPT = """\
Sen bir toplantı asistanısın. SADECE aşağıdaki toplantı metnine dayanarak
soruyu Türkçe yanıtla. Metinde cevap yoksa "Metinde bu bilgi yok." de —
UYDURMA.

Toplantı metni:
{transcript}

Soru: {question}

Yanıt (tek-iki cümle, yalnız metne dayalı):"""


def _mock_answer(question: str, transcript: str) -> str:
    """Deterministic: the transcript sentence best matching the question."""
    sentences = split_sentences(transcript)
    best = ground_claim(question, sentences, threshold=0.0)
    return best.source_text if best.source_index >= 0 else ""


def _ollama_answer(question: str, transcript: str, settings: Settings) -> str:
    prompt = _ASK_PROMPT.format(transcript=transcript, question=question)
    resp = httpx.post(
        f"{settings.ollama_host}/api/generate",
        json={"model": settings.ollama_model, "prompt": prompt, "stream": False},
        timeout=settings.request_timeout,
    )
    resp.raise_for_status()
    return str(resp.json().get("response", "")).strip()


def answer_question(transcript: str, question: str, settings: Settings) -> AskResponse:
    """Answer from the transcript + ground it (citation / hallucination guard)."""
    start = time.perf_counter()
    if settings.redact_pii:
        redacted, _ = redact_pii(transcript)
    else:
        redacted = transcript

    if settings.backend == "ollama":
        answer = _ollama_answer(question, redacted, settings)
    else:  # mock (and unwired anthropic/openai fall back to mock-safe behaviour)
        answer = _mock_answer(question, redacted)

    # Ground the answer to a transcript sentence (skip the "no info" sentinel).
    sentences = split_sentences(redacted)
    citation = ground_claim(answer or "", sentences) if answer else None
    grounded = bool(citation and citation.grounded)

    return AskResponse(
        answer=answer or "Metinde bu bilgi yok.",
        citation=Citation(
            claim=answer,
            source_index=citation.source_index if citation else -1,
            source_text=citation.source_text if citation else "",
            similarity=citation.similarity if citation else 0.0,
            grounded=grounded,
        ),
        grounded=grounded,
        redacted=settings.redact_pii,
        backend=settings.backend,
        elapsed_ms=int((time.perf_counter() - start) * 1000),
    )
