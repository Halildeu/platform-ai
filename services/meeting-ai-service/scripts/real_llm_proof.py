#!/usr/bin/env python3
"""Human-facing PROOF of the differentiator: real LLM → verified-citation pipeline.

Runs the meeting-ai pipeline with a REAL Ollama backend on two Turkish transcripts
and prints, for each, what the LLM produced vs what the verifier SHIPPED (with a
real transcript-span citation) vs what it WITHHELD (the hallucination guard). No
competitor machine-checks this — they timestamp-link and leave verification to a human.

Usage:
    MAI_OLLAMA_HOST=http://localhost:11435 MAI_OLLAMA_MODEL=qwen2.5:7b \
      .venv/bin/python scripts/real_llm_proof.py
"""

# ruff: noqa: E402, T201 - proof CLI: env must be set before importing app; prints are the output.
from __future__ import annotations

import os

os.environ.setdefault("MAI_BACKEND", "ollama")
os.environ.setdefault("MAI_OLLAMA_HOST", "http://localhost:11435")
os.environ.setdefault("MAI_OLLAMA_MODEL", "qwen2.5:7b")
os.environ.setdefault("MAI_REQUEST_TIMEOUT", "240")

from app.core.config import Settings
from app.services.analyze import build_analyzer
from app.services.citation import ground_claim, split_sentences

# A: clean meeting — approved budget (+number), REJECTED proposal, two owned actions.
TRANSCRIPT_A = (
    "Toplantı saat 14:00'te başladı. Mali müdür bütçe artışını sundu. "
    "Yönetim kurulu bütçenin yüzde 15 artırılmasını onayladı. "
    "Pazarlama ekibi yeni bir ofis açılmasını önerdi ancak bu öneri reddedildi. "
    "Ayşe Yılmaz tedarikçi sözleşmesini cuma gününe kadar hazırlayacak. "
    "Mehmet sunucu maliyetlerini bir sonraki toplantıya kadar analiz edecek. "
    "Toplantı saat 15:30'da sona erdi."
)

# B: adversarial — a topic is DISCUSSED but explicitly NOT decided (postponed). A
# small LLM tends to assert a decision anyway → the guard must withhold it.
TRANSCRIPT_B = (
    "Toplantıda yeni bir CRM sistemine geçiş konuşuldu. "
    "Ekip maliyetleri ve faydaları uzun uzun tartıştı. "
    "Bazı üyeler endişelerini dile getirdi. "
    "Net bir karara varılamadı ve konu bir sonraki toplantıya ertelendi."
)


def run(label: str, transcript: str) -> None:
    settings = Settings(backend="ollama", redact_pii=True)
    analyzer = build_analyzer(settings)
    draft = analyzer.analyze(transcript)  # REAL LLM call

    sentences = split_sentences(transcript)
    claims = [("decision", d) for d in draft.decisions] + [
        ("action", a.text) for a in draft.action_items
    ]

    print(f"\n{'=' * 78}\n{label}\n{'=' * 78}")
    print(f"TRANSCRIPT:\n  {transcript}\n")
    print(f"LLM ({settings.ollama_model}) abstractive SUMMARY [UNVERIFIED narrative]:")
    print(f"  {draft.summary}\n")
    print(f"LLM produced {len(claims)} discrete claim(s). Verifier verdict per claim:")
    shipped = 0
    withheld = 0
    for kind, text in claims:
        v = ground_claim(text, sentences)
        if v.grounded:
            shipped += 1
            span = transcript[v.source_char_start : v.source_char_end]
            print(f"  ✓ SHIPPED  [{kind}] {text}")
            print(
                f"      └─ cited span (chars {v.source_char_start}-{v.source_char_end}): “{span}”"
            )
        else:
            withheld += 1
            print(f"  ✗ WITHHELD [{kind}] {text}")
            print(f"      └─ {v.status.value}: {v.reason} (best coverage {v.similarity})")
    print(f"\n  → {shipped} verified+cited, {withheld} withheld by the guard.")


def main() -> None:
    print("REAL-LLM + VERIFIED-CITATION PROOF (on-prem Ollama, model-free verifier)")
    run("A — CLEAN MEETING (faithful claims must ship WITH a real cited span)", TRANSCRIPT_A)
    run(
        "B — ADVERSARIAL (a non-decided topic — any asserted decision must be WITHHELD)",
        TRANSCRIPT_B,
    )
    print(f"\n{'=' * 78}\nDONE. Every shipped claim points at a real transcript span; ungrounded")
    print("LLM claims are withheld — the verification no competitor ships.")


if __name__ == "__main__":
    main()
