"""Speaker → person mapping (#161 T-B: "speaker→kişi eşleme").

KVKK boundary (ADR-0030): diarization produces ANONYMOUS labels (SPEAKER_00…).
This module never infers identity from the voice — there is **no voiceprint,
embedding, or biometric** here. It only:

  1. summarizes each anonymous speaker with *contextual* facts (how long / how
     many turns / when they first spoke), and
  2. applies a mapping that a HUMAN supplies (e.g. the meeting organizer confirms
     "SPEAKER_00 is Ayşe"). The anonymous label is always kept as the canonical
     field, so identity is an additive, reversible, human-authored overlay.

`suggest_mapping` offers a best-effort ordering heuristic (first-to-speak or
most-talkative ↔ a known attendee roster) purely as a starting point for the
human to confirm — it is explicitly NOT an automatic identification and must be
reviewed before use. Without a consented enrolment phase, no automatic
voice→person link is permitted.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.schemas import SpeakerSegment


class SpeakerStats(BaseModel):
    """Contextual, non-biometric summary of one anonymous speaker."""

    speaker: str = Field(description="Anonymous label, e.g. SPEAKER_00")
    total_sec: float = Field(description="Total speaking time (seconds)", ge=0.0)
    turn_count: int = Field(description="Number of turns", ge=0)
    first_seen_sec: float = Field(description="Start of this speaker's first turn", ge=0.0)


class IdentifiedSegment(SpeakerSegment):
    """A diarized turn plus an OPTIONAL human-confirmed display name.

    `speaker` (anonymous) remains the canonical identity; `display_name` is a
    reversible overlay that is only set when a human supplied a mapping.
    """

    display_name: str | None = Field(default=None, description="Human-confirmed name, if any")


def summarize_speakers(segments: list[SpeakerSegment]) -> list[SpeakerStats]:
    """Aggregate per-speaker contextual stats, ordered by who spoke first."""
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    first_seen: dict[str, float] = {}
    for seg in segments:
        dur = max(0.0, seg.end - seg.start)
        totals[seg.speaker] = totals.get(seg.speaker, 0.0) + dur
        counts[seg.speaker] = counts.get(seg.speaker, 0) + 1
        if seg.speaker not in first_seen or seg.start < first_seen[seg.speaker]:
            first_seen[seg.speaker] = seg.start
    stats = [
        SpeakerStats(
            speaker=spk,
            total_sec=round(totals[spk], 3),
            turn_count=counts[spk],
            first_seen_sec=first_seen[spk],
        )
        for spk in totals
    ]
    stats.sort(key=lambda s: (s.first_seen_sec, s.speaker))
    return stats


def apply_mapping(
    segments: list[SpeakerSegment], mapping: dict[str, str]
) -> list[IdentifiedSegment]:
    """Overlay human-confirmed names onto segments; unknown labels stay anonymous.

    The anonymous `speaker` field is preserved verbatim — `display_name` is set
    only for labels present in `mapping`. Pure and reversible (drop the overlay
    to return to fully anonymous data).
    """
    return [
        IdentifiedSegment(
            speaker=seg.speaker,
            start=seg.start,
            end=seg.end,
            display_name=mapping.get(seg.speaker),
        )
        for seg in segments
    ]


def suggest_mapping(
    stats: list[SpeakerStats], roster: list[str], by: str = "first_seen"
) -> dict[str, str]:
    """Best-effort SUGGESTION only — pair anonymous speakers with a known roster.

    `by="first_seen"` pairs the first person to speak with roster[0], etc.;
    `by="talk_time"` pairs the most-talkative with roster[0]. This is a heuristic
    convenience for a human to review — it is NOT identification and carries no
    confidence. Returns at most `len(roster)` pairs. KVKK: the result must be
    confirmed by a person before any name is attached to real data.
    """
    if by == "talk_time":
        ordered = sorted(stats, key=lambda s: (-s.total_sec, s.first_seen_sec, s.speaker))
    else:
        ordered = sorted(stats, key=lambda s: (s.first_seen_sec, s.speaker))
    return {s.speaker: roster[i] for i, s in enumerate(ordered) if i < len(roster)}
