"""speaker_mapping tests — contextual stats + human-confirmed overlay (#161).

KVKK: these prove the module keeps anonymous labels canonical and never invents
identities — names appear only via an explicit human-supplied mapping.
"""

from __future__ import annotations

from app.models.schemas import SpeakerSegment
from app.services.speaker_mapping import (
    apply_mapping,
    suggest_mapping,
    summarize_speakers,
)


def _segs() -> list[SpeakerSegment]:
    # SPEAKER_01 speaks first (0.0); SPEAKER_00 talks longest overall.
    return [
        SpeakerSegment(speaker="SPEAKER_01", start=0.0, end=1.0),
        SpeakerSegment(speaker="SPEAKER_00", start=1.0, end=4.0),
        SpeakerSegment(speaker="SPEAKER_01", start=4.0, end=4.5),
        SpeakerSegment(speaker="SPEAKER_00", start=4.5, end=6.0),
    ]


def test_summarize_orders_by_first_seen_and_aggregates() -> None:
    stats = summarize_speakers(_segs())
    assert [s.speaker for s in stats] == ["SPEAKER_01", "SPEAKER_00"]
    by_label = {s.speaker: s for s in stats}
    assert by_label["SPEAKER_00"].total_sec == 4.5  # 3.0 + 1.5
    assert by_label["SPEAKER_00"].turn_count == 2
    assert by_label["SPEAKER_01"].first_seen_sec == 0.0


def test_summarize_empty() -> None:
    assert summarize_speakers([]) == []


def test_apply_mapping_keeps_anonymous_and_overlays_name() -> None:
    mapped = apply_mapping(_segs(), {"SPEAKER_00": "Ayşe"})
    assert mapped[0].speaker == "SPEAKER_01"  # canonical label preserved
    assert mapped[0].display_name is None  # unmapped → stays anonymous
    assert mapped[1].speaker == "SPEAKER_00"
    assert mapped[1].display_name == "Ayşe"


def test_suggest_by_first_seen() -> None:
    stats = summarize_speakers(_segs())
    # roster[0] → first to speak (SPEAKER_01)
    assert suggest_mapping(stats, ["Mehmet", "Ayşe"]) == {
        "SPEAKER_01": "Mehmet",
        "SPEAKER_00": "Ayşe",
    }


def test_suggest_by_talk_time_differs_from_first_seen() -> None:
    stats = summarize_speakers(_segs())
    # roster[0] → most talkative (SPEAKER_00), so ordering flips vs first_seen
    assert suggest_mapping(stats, ["Mehmet", "Ayşe"], by="talk_time") == {
        "SPEAKER_00": "Mehmet",
        "SPEAKER_01": "Ayşe",
    }


def test_suggest_truncates_to_roster_length() -> None:
    stats = summarize_speakers(_segs())
    assert suggest_mapping(stats, ["Solo"]) == {"SPEAKER_01": "Solo"}
