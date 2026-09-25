"""Evidence checks with recorded synthetic events and controlled proposals.

These do not measure LLM recall/precision. The semantic counterexample at the
end deliberately proves the boundary of a deterministic evidence verifier.
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from experiments.owner_context.candidate import (
    FinalEvent,
    OwnerProposal,
    Snapshot,
    digest,
    from_events,
    prompt,
    resolve,
)

TASK = "Bütçe tablosunu 26 Eylül 2026 günü saat 12'ye kadar kontrol edecek."


def recorded(name: str) -> Snapshot:
    fixture = json.loads((Path(__file__).parents[1] / f"fixtures/{name}.json").read_text("utf-8"))
    snapshot = from_events([FinalEvent.model_validate(event) for event in fixture["finalEvents"]])
    assert fixture["synthetic"] is True
    assert snapshot.sha256 == fixture["transcriptSha256"]
    assert snapshot.text == fixture["transcript"]
    return snapshot


def event(
    seq: int, text: str, start_ms: int, *, speaker: str = "S1", scope: str = "test"
) -> FinalEvent:
    return FinalEvent.model_validate(
        {
            "seq": seq,
            "text": text,
            "source_start_sample": start_ms * 16,
            "source_end_sample": (start_ms + 1000) * 16,
            "speakerAttribution": {
                "scope": scope,
                "turns": [
                    {
                        "speaker": speaker,
                        "textStart": 0,
                        "textEnd": len(text.encode("utf-16-le")) // 2,
                        "startMs": 0,
                        "endMs": 1000,
                    }
                ],
            },
        }
    )


def two_sources(first: str = "Mehmet.", second: str = TASK) -> Snapshot:
    return from_events([event(0, first, 0), event(1, second, 1100)])


def proposal(
    snapshot: Snapshot, *, task: int = 1, owner_source: int = 0, owner: str = "Mehmet"
) -> OwnerProposal:
    return OwnerProposal(
        transcript_sha256=snapshot.sha256,
        task_source=task,
        owner_source=owner_source,
        owner=owner,
        relationship="subject_continuation",
    )


@pytest.mark.parametrize("name", ["normal", "paused"])
def test_recorded_text_and_all_source_coordinates_survive(name: str) -> None:
    snapshot = recorded(name)
    for source in snapshot.sources:
        assert snapshot.text[source.char_start : source.char_end] == source.text
        assert digest(source.text) == source.quote_sha256
        assert source.event_sequences
    rendered = json.loads(prompt(snapshot).split("\n", 1)[1])
    assert [row["source_index"] for row in rendered["sources"]] == list(
        range(len(snapshot.sources))
    )
    assert rendered["transcript_sha256"] == snapshot.sha256


def test_context_keeps_detached_name_without_promoting_it_to_a_task() -> None:
    snapshot = recorded("normal")
    rendered = json.loads(prompt(snapshot).split("\n", 1)[1])
    assert rendered["sources"][3] == {
        "source_index": 3,
        "text": "Mehmet.",
        "selectable_task": False,
    }
    assert rendered["sources"][4]["selectable_task"] is True


def test_controlled_normal_proposals_preserve_both_owners_and_original_evidence() -> None:
    snapshot = recorded("normal")
    zeynep = proposal(snapshot, task=2, owner_source=2, owner="Zeynep").model_copy(
        update={"relationship": "explicit"}
    )
    mehmet = proposal(snapshot, task=4, owner_source=3)
    results = [resolve(snapshot, value) for value in [zeynep, mehmet]]
    assert [result.owner for result in results] == ["Zeynep", "Mehmet"]
    assert [source.source_index for source in results[1].evidence] == [3, 4]
    assert results[1].evidence[0].event_sequences == (23,)
    assert results[1].evidence[1].event_sequences == tuple(range(24, 35))
    assert results[1].evidence[0].text == "Mehmet."
    assert results[1].evidence[1].text == TASK
    assert all(not result.semantic_quality_verified for result in results)


def test_controlled_paused_proposal_still_uses_its_original_single_source() -> None:
    snapshot = recorded("paused")
    source = next(s for s in snapshot.sources if "Mehmet" in s.text)
    value = proposal(
        snapshot, task=source.source_index, owner_source=source.source_index
    ).model_copy(update={"relationship": "explicit"})
    result = resolve(snapshot, value)
    assert result.owner == "Mehmet"
    assert result.evidence == (source,)


@pytest.mark.parametrize(
    ("speaker", "scope", "reason"),
    [
        ("S2", "test", "different-speaker-or-scope"),
        ("S1", "another-session", "different-speaker-or-scope"),
        ("UU", "test", "incomplete-speaker-or-time-evidence"),
        ("unknown", "test", "incomplete-speaker-or-time-evidence"),
        ("", "test", "incomplete-speaker-or-time-evidence"),
        ("S1", "", "incomplete-speaker-or-time-evidence"),
    ],
)
def test_no_cross_speaker_scope_or_unknown_attribution(
    speaker: str, scope: str, reason: str
) -> None:
    snapshot = from_events(
        [event(0, "Mehmet.", 0), event(1, TASK, 1100, speaker=speaker, scope=scope)]
    )
    result = resolve(snapshot, proposal(snapshot))
    assert result.owner is None
    assert result.reason == reason


@pytest.mark.parametrize("missing", [0, 1])
def test_missing_metadata_does_not_borrow_neighbor_attribution(missing: int) -> None:
    events = [event(0, "Mehmet.", 0), event(1, TASK, 1100)]
    events[missing] = events[missing].model_copy(update={"speakerAttribution": None})
    snapshot = from_events(events)
    assert resolve(snapshot, proposal(snapshot)).owner is None


def test_uncovered_and_overlapping_text_offsets_abstain() -> None:
    original = two_sources()
    first, second = original.spans
    for spans in [(replace(first, end=3), second), (first, first, second)]:
        snapshot = replace(original, spans=spans)
        assert resolve(snapshot, proposal(snapshot)).reason == "incomplete-speaker-or-time-evidence"


def test_mixed_speakers_inside_task_abstain() -> None:
    original = two_sources()
    first, second = original.spans
    spans = (
        first,
        replace(second, end=second.start + 5),
        replace(second, start=second.start + 5, speaker="S2"),
    )
    snapshot = replace(original, spans=spans)
    assert resolve(snapshot, proposal(snapshot)).owner is None


def test_long_pause_does_not_prove_subject_continuation() -> None:
    snapshot = from_events([event(0, "Mehmet.", 0), event(1, TASK, 5000)])
    assert resolve(snapshot, proposal(snapshot)).reason == "outside-time-bound"


def test_intervening_sentence_is_not_silently_skipped() -> None:
    snapshot = from_events(
        [event(0, "Mehmet.", 0), event(1, "Yeni konu.", 1100), event(2, TASK, 2200)]
    )
    assert resolve(snapshot, proposal(snapshot, task=2)).reason == "non-adjacent-sources"


def test_unrelated_mention_cannot_supply_a_standalone_subject() -> None:
    snapshot = two_sources("Mehmet toplantıya katılmadı.")
    assert resolve(snapshot, proposal(snapshot)).reason == "not-standalone-subject"


@pytest.mark.parametrize(
    "text",
    [
        "Bütçe tablosunu ben kontrol edeceğim.",
        "Bütçe tablosunu kontrol etmeyecek.",
        "Bütçe kontrolü iptal edildi.",
        "Görev artık Ayşe'ye verildi.",
    ],
)
def test_semantically_ambiguous_or_retracted_proposals_abstain(text: str) -> None:
    snapshot = two_sources(second=text)
    for relationship in ("ambiguous", "unrelated", "retracted"):
        value = proposal(snapshot).model_copy(update={"relationship": relationship})
        assert resolve(snapshot, value).reason == "semantic-abstention"


def test_existing_different_owner_cannot_be_overridden() -> None:
    snapshot = two_sources(second="Ayşe bütçe tablosunu kontrol edecek.")
    result = resolve(snapshot, proposal(snapshot), existing_owner="Ayşe")
    assert result.owner == "Ayşe"
    assert result.reason == "existing-owner-preserved"
    assert [source.source_index for source in result.evidence] == [1]
    assert resolve(snapshot, proposal(snapshot), existing_owner="Zeynep").owner is None
    assert resolve(snapshot, proposal(snapshot), existing_owner=" ").owner is None


def test_case_folded_or_padded_names_are_not_silently_rewritten() -> None:
    snapshot = two_sources()
    for owner in ("mehmet", "Mehmet "):
        assert resolve(snapshot, proposal(snapshot, owner=owner)).reason == "owner-not-verbatim"


def test_same_source_cannot_use_cross_source_relationship() -> None:
    snapshot = two_sources(second="Mehmet bütçe tablosunu kontrol edecek.")
    value = proposal(snapshot, owner_source=1)
    assert resolve(snapshot, value).reason == "invalid-relationship"


def test_non_monotonic_word_timing_is_not_a_continuous_turn() -> None:
    original = two_sources()
    first, second = original.spans
    spans = (first, replace(second, end=second.start + 5), replace(second, start=second.start + 5))
    snapshot = replace(original, spans=spans)
    assert resolve(snapshot, proposal(snapshot)).reason == "incomplete-speaker-or-time-evidence"


@pytest.mark.parametrize("owner", ["Meh", "Ahmet", "Ben"])
def test_missing_partial_or_pronoun_owner_is_not_grounded(owner: str) -> None:
    snapshot = two_sources()
    assert resolve(snapshot, proposal(snapshot, owner=owner)).reason == "unsupported-owner"


def test_wrong_hash_and_source_tampering_are_rejected() -> None:
    snapshot = two_sources()
    value = proposal(snapshot)
    assert resolve(snapshot, value.model_copy(update={"transcript_sha256": "stale"})).owner is None
    assert resolve(replace(snapshot, text=snapshot.text + " More."), value).owner is None
    first, second = snapshot.sources
    altered = replace(snapshot, sources=(replace(first, char_start=1), second))
    assert resolve(altered, value).reason == "invalid-source-reference"


def test_invalid_source_and_relationship_proposals_are_rejected() -> None:
    snapshot = two_sources()
    value = proposal(snapshot)
    for task in (0, 99):
        assert resolve(snapshot, value.model_copy(update={"task_source": task})).owner is None
    for source in (None, 99):
        assert resolve(snapshot, value.model_copy(update={"owner_source": source})).owner is None
    assert resolve(snapshot, value.model_copy(update={"relationship": "explicit"})).owner is None
    assert resolve(snapshot, value.model_copy(update={"owner": None})).owner is None


def test_unicode_coordinates_keep_original_source_and_reject_surrogate_midpoint() -> None:
    events = [event(0, "😀 Merhaba.", 0), event(1, "Mehmet.", 1100), event(2, TASK, 2200)]
    snapshot = from_events(events)
    result = resolve(snapshot, proposal(snapshot, task=2, owner_source=1))
    assert result.owner == "Mehmet"
    assert snapshot.text[result.evidence[0].char_start : result.evidence[0].char_end] == "Mehmet."
    raw = events[0].model_dump()
    raw["speakerAttribution"]["turns"][0]["textStart"] = 1
    with pytest.raises(ValueError, match="invalid-utf16-boundary"):
        from_events([FinalEvent.model_validate(raw)])


@pytest.mark.parametrize("kind", ["duplicate", "backward", "duration", "empty", "whitespace"])
def test_malformed_events_fail_closed(kind: str) -> None:
    events = [event(0, "Mehmet.", 0), event(1, TASK, 1100)]
    updates: dict[str, dict[str, Any]] = {
        "duplicate": {"seq": 0},
        "backward": {"source_start_sample": 100},
        "duration": {"source_end_sample": 18000},
        "empty": {"text": ""},
        "whitespace": {"text": " " + TASK},
    }
    events[1] = events[1].model_copy(update=updates[kind])
    with pytest.raises(ValueError):
        from_events(events)


def test_invalid_configuration_and_proposal_types_fail_closed() -> None:
    with pytest.raises(ValueError):
        from_events([])
    with pytest.raises(ValueError):
        from_events([event(0, "Mehmet.", 0)], sample_rate=0)
    snapshot = two_sources()
    for gap in (-1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            resolve(snapshot, proposal(snapshot), max_gap_ms=gap)
    for updates in ({"task_source": True}, {"task_source": -1}, {"owner_source": "0"}):
        raw = proposal(snapshot).model_dump() | updates
        with pytest.raises(ValidationError):
            OwnerProposal.model_validate(raw)


def test_known_limit_structure_does_not_verify_semantics() -> None:
    # A vocative can look identical to a fragmented subject in timing/diarization.
    # An incorrect semantic proposal must NOT be reported as model quality proof.
    snapshot = two_sources(second="Bütçe tablosunu ben kontrol edeceğim.")
    result = resolve(snapshot, proposal(snapshot))
    assert result.owner == "Mehmet"  # deliberately incorrect controlled proposal
    assert result.semantic_quality_verified is False
