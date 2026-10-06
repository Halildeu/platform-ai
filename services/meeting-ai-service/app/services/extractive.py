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
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.citation import Sentence, is_groundable_evidence

# A summary is a handful of sentences, not a transcript replay. The cap also
# bounds the prompt's answer size for a small local model.
MAX_SUMMARY_SENTENCES = 3
MAX_DECISION_SENTENCES = 10
MAX_ACTION_ITEMS = 10


class SelectedAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sentence: int = Field(ge=1)
    owner: str | None
    due_date: str | None


class SelectedActionStateEvent(BaseModel):
    """A grounded change to an earlier task, expressed only with source indices."""

    model_config = ConfigDict(extra="forbid", strict=True)

    sentence: int = Field(ge=1)
    target_sentence: int = Field(ge=1)
    operation: Literal["replace", "cancel"]
    owner: str | None
    due_date: str | None


class SentenceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    summary_sentences: list[Annotated[int, Field(ge=1)]] = Field(max_length=MAX_SUMMARY_SENTENCES)
    decision_sentences: list[Annotated[int, Field(ge=1)]] = Field(max_length=MAX_DECISION_SENTENCES)
    action_item_sentences: list[SelectedAction] = Field(max_length=MAX_ACTION_ITEMS)
    action_state_events: list[SelectedActionStateEvent] = Field(
        default_factory=list, max_length=MAX_ACTION_ITEMS
    )


def selection_schema(sentence_count: int) -> dict[str, Any]:
    """Constrain generation to real menu indices and explicit nullable metadata."""
    schema = SentenceSelection.model_json_schema()
    for field in ("summary_sentences", "decision_sentences"):
        schema["properties"][field]["items"]["maximum"] = sentence_count
    schema["$defs"]["SelectedAction"]["properties"]["sentence"]["maximum"] = sentence_count
    event = schema["$defs"]["SelectedActionStateEvent"]["properties"]
    event["sentence"]["maximum"] = sentence_count
    event["target_sentence"]["maximum"] = sentence_count
    # The Python parser accepts pre-state-engine responses for safe rollout,
    # while the Ollama constrained-generation contract always asks new
    # responses to make the event list explicit (including an empty list).
    required = schema.setdefault("required", [])
    if "action_state_events" not in required:
        required.append("action_state_events")
    return schema


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


def materialize_action_state(
    raw_items: object, raw_events: object, sentences: list[Sentence]
) -> list[tuple[str, str | None, str | None]]:
    """Apply grounded replacement/cancellation events to selected task sentences.

    Both the task and every mutation remain source-index selections.  The
    service never invents a merged sentence: a replacement is displayed with
    the exact later source sentence, while cancellation removes the targeted
    task.  Invalid, backwards or dangling events fail closed.
    """
    if not isinstance(raw_items, list):
        raw_items = []
    if not isinstance(raw_events, list):
        raw_events = []

    active: dict[int, tuple[str, str | None, str | None]] = {}
    current: dict[int, int] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        index = item.get("sentence")
        if isinstance(index, bool) or not isinstance(index, int):
            continue
        zero_based = index - 1
        if zero_based < 0 or zero_based >= len(sentences) or zero_based in active:
            continue
        owner = item.get("owner")
        due_date = item.get("due_date")
        active[zero_based] = (
            sentences[zero_based].text.strip(),
            owner if isinstance(owner, str) and owner.strip() else None,
            due_date if isinstance(due_date, str) and due_date.strip() else None,
        )
        current[zero_based] = zero_based

    valid_events: list[tuple[int, dict[str, object]]] = []
    seen_event_sources: set[int] = set()
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        index = event.get("sentence")
        target = event.get("target_sentence")
        operation = event.get("operation")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or isinstance(target, bool)
            or not isinstance(target, int)
            or operation not in {"replace", "cancel"}
        ):
            continue
        source_index, target_index = index - 1, target - 1
        if (
            source_index < 0
            or source_index >= len(sentences)
            or target_index < 0
            or target_index >= len(sentences)
            or source_index <= target_index
            or source_index in seen_event_sources
        ):
            continue
        seen_event_sources.add(source_index)
        valid_events.append((source_index, event))

    for source_index, event in sorted(valid_events, key=lambda value: value[0]):
        target_index = int(event["target_sentence"]) - 1
        current_index = current.get(target_index)
        # The relation is model-selected. Only let it mutate a task that was
        # independently selected as an assignment (or a prior valid
        # replacement); a dangling target must never delete arbitrary work.
        if current_index is None or current_index < 0 or current_index not in active:
            continue
        active.pop(current_index, None)
        operation = event["operation"]
        # Every historical reference belongs to the same task. Updating only
        # the event's direct target strands older references after two edits.
        next_index = -1 if operation == "cancel" else source_index
        for reference, resolved in current.items():
            if resolved == current_index:
                current[reference] = next_index
        if operation == "cancel":
            continue
        owner = event.get("owner")
        due_date = event.get("due_date")
        active[source_index] = (
            sentences[source_index].text.strip(),
            owner if isinstance(owner, str) and owner.strip() else None,
            due_date if isinstance(due_date, str) and due_date.strip() else None,
        )
        # Future events can name this revision as well as any earlier one.
        current[source_index] = source_index

    return [active[index] for index in sorted(active)][:MAX_ACTION_ITEMS]


def looks_like_selection(data: object) -> bool:
    """Whether the model answered in the index contract at all.

    Used to decide between the structural path and the legacy free-text path;
    a model that ignores the new prompt must not crash the analysis.
    """
    if not isinstance(data, dict):
        return False
    return any(
        key in data
        for key in (
            "summary_sentences",
            "decision_sentences",
            "action_item_sentences",
            "action_state_events",
        )
    )
