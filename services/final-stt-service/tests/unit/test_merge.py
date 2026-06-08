from __future__ import annotations

from app.services.merge import merge_transcripts


def test_merge_removes_longest_case_insensitive_overlap() -> None:
    result = merge_transcripts(
        "Bugün toplantıda bütçeyi değerlendirdik.",
        "Bütçeyi değerlendirdik ve kararı kaydettik.",
    )
    assert result.text == "Bugün toplantıda bütçeyi değerlendirdik. ve kararı kaydettik."
    assert result.overlap_words == 2


def test_merge_handles_empty_side() -> None:
    assert merge_transcripts("", "Yeni metin").text == "Yeni metin"
    assert merge_transcripts("Eski metin", "").text == "Eski metin"
