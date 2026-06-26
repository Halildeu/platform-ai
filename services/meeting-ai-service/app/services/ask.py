"""#162 PR-llm-03: post-meeting ask-AI.

Answer a question about a meeting strictly from its (redacted) transcript, then
ground the answer to a source sentence (citation) and flag it if unsupported
(hallucination guard) — the same honesty contract as the analyze path.

Backends mirror analyze.py: `mock` (deterministic, no LLM — picks the best
sentence) and `ollama` (local LLM, Option B #54). KVKK: transcript and question
are redacted before a real LLM sees them; only redacted text is grounded.
"""

from __future__ import annotations

import time

import httpx

from app.core.config import Settings
from app.models.schemas import AskResponse, Citation
from app.services.citation import best_matching_sentence, ground_claim, split_sentences
from app.services.redact import assert_no_residual_pii, redact_pii

_UNSUPPORTED_ANSWER = "Metinde bu bilgi yok."

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
    best = best_matching_sentence(question, sentences)
    return best.text if best is not None else ""


def _ollama_answer(question: str, transcript: str, settings: Settings) -> str:
    prompt = _ASK_PROMPT.format(transcript=transcript, question=question)
    resp = httpx.post(
        f"{settings.ollama_host}/api/generate",
        json={
            "model": settings.ollama_model,
            "prompt": prompt,
            "stream": False,
            # Same decoding contract as analyze: deterministic + full transcript in
            # context (no 2048-default truncation). No format=json here — ask returns
            # prose, not a JSON object.
            "options": settings.ollama_options(),
            "keep_alive": settings.ollama_keep_alive,
        },
        timeout=settings.request_timeout,
    )
    resp.raise_for_status()
    return str(resp.json().get("response", "")).strip()


def _is_no_info_answer(answer: str) -> bool:
    return answer.strip().casefold().startswith(_UNSUPPORTED_ANSWER.casefold())


def _unsupported_citation(reason: str) -> Citation:
    return Citation(
        claim="",
        source_index=-1,
        source_text="",
        similarity=0.0,
        grounded=False,
        status="FAILED",
        reason=reason,
    )


def answer_question(transcript: str, question: str, settings: Settings) -> AskResponse:
    """Answer from the transcript + ground it (citation / hallucination guard)."""
    start = time.perf_counter()
    if settings.redact_pii:
        redacted, _ = redact_pii(transcript)
        redacted_question, _ = redact_pii(question)
        assert_no_residual_pii(redacted)
        assert_no_residual_pii(redacted_question)
    else:
        redacted = transcript
        redacted_question = question

    if settings.backend == "ollama":
        answer = _ollama_answer(redacted_question, redacted, settings)
    elif settings.backend == "mock":
        answer = _mock_answer(redacted_question, redacted)
    else:
        raise NotImplementedError(
            f"backend '{settings.backend}' requires ADR-0030 legal approval (#52). "
            "Use MAI_BACKEND=mock or MAI_BACKEND=ollama."
        )

    # Ground the answer to a transcript sentence. If a real LLM produces unsupported
    # prose, never show that prose to the user with a weak `grounded=false` flag.
    sentences = split_sentences(redacted)
    if not answer or _is_no_info_answer(answer):
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return AskResponse(
            answer=_UNSUPPORTED_ANSWER,
            citation=_unsupported_citation("answer does not claim transcript support"),
            grounded=False,
            redacted=settings.redact_pii,
            backend=settings.backend,
            elapsed_ms=elapsed_ms,
        )

    citation = ground_claim(answer, sentences)
    grounded = citation.grounded
    if not grounded:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return AskResponse(
            answer=_UNSUPPORTED_ANSWER,
            citation=_unsupported_citation("generated answer not grounded to transcript"),
            grounded=False,
            redacted=settings.redact_pii,
            backend=settings.backend,
            elapsed_ms=elapsed_ms,
        )

    return AskResponse(
        answer=answer,
        citation=Citation(
            claim=answer,
            source_index=citation.source_index,
            source_text=citation.source_text,
            similarity=citation.similarity,
            grounded=True,
            status=citation.status.value,
            reason=citation.reason,
            start_sec=citation.start_sec,
            source_char_start=citation.source_char_start,
            source_char_end=citation.source_char_end,
            source_hash=citation.source_hash,
            quote_hash=citation.quote_hash,
        ),
        grounded=True,
        redacted=settings.redact_pii,
        backend=settings.backend,
        elapsed_ms=int((time.perf_counter() - start) * 1000),
    )
