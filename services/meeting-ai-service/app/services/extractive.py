"""gitops#3444 — extractive-by-construction analysis (sentence-index selection).

Why this exists
===============

The verifier in `citation.py` requires every summary/decision/action claim to be
covered by ONE transcript sentence. The prompt already forbids paraphrase in
capital letters ("cümleleri metinden AYNEN kopyala … paraphrase YAPMA"), yet the
measured reality on k3d-test (2026-08-05, 89 rejected claims from 23 meetings)
was:

    median claim coverage 0.33   (threshold 0.65)
    26 of 69 failures below 0.20
    53 "no transcript sentence covers the claim", 14 fact-fusion

i.e. `llama3.1:8b` does not obey the extractive instruction, and the pipeline
silently withheld 29% of summaries as a result.

Instructing harder does not fix a probabilistic generator. The industry answer
for citation-required summarization is to remove the freedom instead: the model
never writes prose, it **selects sentence indices** from a numbered transcript,
and the service materializes the text from its own sentence list. Grounding then
stops being a post-hoc filter and becomes a structural property — a selected
claim IS a transcript sentence, so coverage is 1.0 and fact fusion is
unrepresentable.

The alignment invariant
=======================

Numbering MUST come from `citation.split_sentences` — the very function the
verifier later re-runs. Any second splitter (even a "compatible" one) would let
index *i* mean different text on the two sides and silently reproduce the bug
this module removes. `number_transcript` therefore takes `Sentence` objects, and
`materialize_selection` returns their exact `.text`.

Failure policy
==============

Selection is validated, never repaired: out-of-range, duplicate, non-integer and
over-budget indices are DROPPED (a smaller, fully-grounded answer), never
clamped into a neighbouring sentence — clamping would fabricate attribution.
An empty selection is a legitimate answer ("bu toplantıda karar yok").
"""

from __future__ import annotations

from collections.abc import Iterable

from app.services.citation import Sentence, is_groundable_evidence

# A summary is a handful of sentences, not a transcript replay. The cap also
# bounds the prompt's answer size for a small local model.
MAX_SUMMARY_SENTENCES = 3
MAX_DECISION_SENTENCES = 10
MAX_ACTION_ITEMS = 10


def selectable_sentences(sentences: list[Sentence]) -> list[Sentence]:
    """Sentences the verifier can actually ground, i.e. the legal menu.

    The filter is `citation.is_groundable_evidence` — the verifier's OWN
    criterion — not a character-count approximation. Measured on 23 real
    meetings: a 12-character floor still let through 15 single-content-token
    spans ("Yunanistan için.", "Transkripsiyon.") that came back
    LOW_CONFIDENCE when selected verbatim, which would have made the structural
    guarantee merely a 97% tendency.
    """
    return [s for s in sentences if is_groundable_evidence(s.text)]


def number_transcript(sentences: Iterable[Sentence]) -> str:
    """Render the numbered menu the model selects from.

    Indices are the caller-visible contract; they are the position in the list
    passed here (1-based for the model, because a 0-based menu measurably
    confuses small models into off-by-one selections).
    """
    return "\n".join(f"[{i}] {s.text.strip()}" for i, s in enumerate(sentences, start=1))


def _valid_indices(raw: object, count: int, limit: int) -> list[int]:
    """1-based indices → validated, de-duplicated, order-preserving 0-based list."""
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    seen: set[int] = set()
    for value in raw:
        # bool is an int subclass; a JSON `true` must not become index 1.
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        zero_based = value - 1
        if zero_based < 0 or zero_based >= count or zero_based in seen:
            continue
        seen.add(zero_based)
        out.append(zero_based)
        if len(out) >= limit:
            break
    return out


def materialize_selection(raw_indices: object, sentences: list[Sentence], limit: int) -> list[str]:
    """Selected indices → the EXACT transcript sentences they name."""
    return [sentences[i].text.strip() for i in _valid_indices(raw_indices, len(sentences), limit)]


def materialize_action_items(
    raw_items: object, sentences: list[Sentence]
) -> list[tuple[str, str | None, str | None]]:
    """Action selections → (exact sentence text, owner, due_date) triples.

    Owner and due-date stay free-text: they are attribution METADATA extracted
    from the same sentence, and `citation.owner_supported_by_source` /
    `due_date_supported_by_source` already gate them against exactly that
    sentence. An item whose `sentence` index is invalid is dropped whole — a
    dangling owner with no action text would be worse than no item.
    """
    if not isinstance(raw_items, list):
        return []
    out: list[tuple[str, str | None, str | None]] = []
    seen: set[int] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        index = item.get("sentence")
        if isinstance(index, bool) or not isinstance(index, int):
            continue
        zero_based = index - 1
        if zero_based < 0 or zero_based >= len(sentences) or zero_based in seen:
            continue
        seen.add(zero_based)
        owner = item.get("owner")
        due_date = item.get("due_date")
        out.append(
            (
                sentences[zero_based].text.strip(),
                owner if isinstance(owner, str) and owner.strip() else None,
                due_date if isinstance(due_date, str) and due_date.strip() else None,
            )
        )
        if len(out) >= MAX_ACTION_ITEMS:
            break
    return out


def looks_like_selection(data: object) -> bool:
    """Whether the model answered in the index contract at all.

    Used to decide between the structural path and the legacy free-text path;
    a model that ignores the new prompt must not crash the analysis.
    """
    if not isinstance(data, dict):
        return False
    return any(
        key in data for key in ("summary_sentences", "decision_sentences", "action_item_sentences")
    )
