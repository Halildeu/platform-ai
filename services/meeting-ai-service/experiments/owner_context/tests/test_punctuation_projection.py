from dataclasses import replace

import pytest

from experiments.owner_context.boundary_gold import acceptance_passed, expected_positions
from experiments.owner_context.candidate import digest
from experiments.owner_context.local_compare import artificial
from experiments.owner_context.option_catalog import prepare_options
from experiments.owner_context.punctuation_projection import (
    BoundarySelection,
    project,
    source_map,
)


def test_only_selected_period_changes_and_original_mapping_is_explicit() -> None:
    snapshot = artificial([("Zeynep.", "S1"), ("Sunumu 25 Eylül 2026'da hazırlayacak.", "S1")])
    (option,) = prepare_options(snapshot)
    result = project(snapshot, BoundarySelection(remove_full_stops=[option.option_id]))
    differences = [
        (i, a, b) for i, (a, b) in enumerate(zip(snapshot.text, result.text, strict=True)) if a != b
    ]
    assert differences == [(6, ".", " ")]
    assert len(result.text) == len(snapshot.text)
    assert result.original_sha256 == snapshot.sha256
    assert result.projection_sha256 != snapshot.sha256
    assert result.text.endswith("hazırlayacak.")
    rows = source_map(snapshot, result)
    assert len(rows) == 1
    assert rows[0]["originalText"] == snapshot.text
    assert rows[0]["originalSliceSha256"] == digest(snapshot.text)
    assert rows[0]["projectionText"] == "Zeynep  Sunumu 25 Eylül 2026'da hazırlayacak."
    assert [s["source_index"] for s in rows[0]["originalSources"]] == [0, 1]


def test_null_selection_retains_every_byte() -> None:
    snapshot = artificial([("Mehmet.", "S1"), ("Bütçeyi ben kontrol edeceğim.", "S1")])
    result = project(snapshot, BoundarySelection(remove_full_stops=[]))
    assert result.text == snapshot.text
    assert result.projection_sha256 == result.original_sha256
    assert len(source_map(snapshot, result)) == 2


def test_duplicate_unknown_and_wrong_metadata_choices_fail_closed() -> None:
    snapshot = artificial([("Mehmet.", "S1"), ("Raporu hazırlayacak.", "S1")])
    (option,) = prepare_options(snapshot)
    for invalid in [[option.option_id, option.option_id], ["unknown"]]:
        with pytest.raises(ValueError, match="boundary"):
            project(snapshot, BoundarySelection(remove_full_stops=invalid))
    changed = replace(
        snapshot, spans=tuple(replace(span, scope="another-session") for span in snapshot.spans)
    )
    with pytest.raises(ValueError, match="boundary"):
        project(changed, BoundarySelection(remove_full_stops=[option.option_id]))


def test_missing_attribution_and_other_speaker_cannot_be_overridden() -> None:
    snapshot = artificial([("Mehmet.", "S1"), ("Raporu hazırlayacak.", "S1")])
    (option,) = prepare_options(snapshot)
    for other in [
        replace(snapshot, spans=()),
        replace(snapshot, spans=(snapshot.spans[0], replace(snapshot.spans[1], speaker="S2"))),
    ]:
        with pytest.raises(ValueError, match="boundary"):
            project(other, BoundarySelection(remove_full_stops=[option.option_id]))


def test_source_table_tampering_never_emits_projection_or_evidence() -> None:
    snapshot = artificial([("Mehmet.", "S1"), ("Raporu Ayşe hazırlayacak.", "S1")])
    result = project(snapshot, BoundarySelection(remove_full_stops=[]))
    altered = replace(snapshot.sources[1], text="Sözleşmeyi Ayşe imzalayacak.")
    for bad in [
        replace(snapshot, sha256="0" * 64),
        replace(snapshot, sources=(snapshot.sources[0], altered)),
    ]:
        with pytest.raises(ValueError, match="invalid"):
            project(bad, BoundarySelection(remove_full_stops=[]))
        with pytest.raises(ValueError, match="invalid"):
            source_map(bad, result)


def test_projection_word_mutation_cannot_hide_behind_a_new_hash() -> None:
    snapshot = artificial([("Mehmet.", "S1"), ("Raporu Ayşe hazırlayacak.", "S1")])
    result = project(snapshot, BoundarySelection(remove_full_stops=[]))
    mutation = result.text.replace("Ayşe", "Eren")
    with pytest.raises(ValueError, match="edits"):
        source_map(snapshot, replace(result, text=mutation, projection_sha256=digest(mutation)))


def test_unicode_offsets_remain_python_codepoints() -> None:
    snapshot = artificial(
        [("😀 Toplantı başladı.", "S1"), ("İlknur Şen.", "S1"), ("Özeti hazırlayacak.", "S1")]
    )
    option = next(o for o in prepare_options(snapshot) if o.owner == "İlknur Şen")
    projected = project(snapshot, BoundarySelection(remove_full_stops=[option.option_id]))
    rows = source_map(snapshot, projected)
    assert rows[-1]["originalText"] == "İlknur Şen. Özeti hazırlayacak."
    assert (
        snapshot.text[rows[-1]["originalStart"] : rows[-1]["originalEnd"]]
        == rows[-1]["originalText"]
    )


def test_projection_cannot_remove_genuine_end_or_replay_other_context() -> None:
    snapshot = artificial([("Mehmet.", "S1"), ("Raporu hazırlayacak.", "S1")])
    (option,) = prepare_options(snapshot)
    result = project(snapshot, BoundarySelection(remove_full_stops=[option.option_id]))
    final_removed = snapshot.text[:-1] + " "
    for forged in [
        replace(result, contract="verified_only"),
        replace(result, changed_positions=(6, 6)),
        replace(result, changed_positions=(True,)),
        replace(result, changed_positions=(-1,)),
        replace(result, projection_sha256="0" * 64),
        replace(
            result,
            text=final_removed,
            projection_sha256=digest(final_removed),
            changed_positions=(len(snapshot.text) - 1,),
        ),
    ]:
        with pytest.raises(ValueError, match="invalid"):
            source_map(snapshot, forged)
    other = replace(snapshot, spans=tuple(replace(s, scope="other") for s in snapshot.spans))
    with pytest.raises(ValueError, match="context"):
        source_map(other, result)


def test_event_sequence_tampering_or_rebinding_invalidates_projection() -> None:
    snapshot = artificial([("Mehmet.", "S1"), ("Raporu hazırlayacak.", "S1")])
    (option,) = prepare_options(snapshot)
    result = project(snapshot, BoundarySelection(remove_full_stops=[option.option_id]))
    forged_source = replace(snapshot.sources[0], event_sequences=(999,))
    forged = replace(snapshot, sources=(forged_source, snapshot.sources[1]))
    assert not prepare_options(forged)
    with pytest.raises(ValueError, match="original"):
        source_map(forged, result)
    # Even a consistently rebound event table cannot reuse an old projection.
    rebound = replace(
        snapshot,
        sources=tuple(
            replace(s, event_sequences=tuple(q + 10 for q in s.event_sequences))
            for s in snapshot.sources
        ),
        spans=tuple(replace(s, seq=s.seq + 10) for s in snapshot.spans),
        event_ranges=tuple((a, b, seq + 10) for a, b, seq in snapshot.event_ranges),
    )
    assert prepare_options(rebound)
    with pytest.raises(ValueError, match="context"):
        source_map(rebound, result)


def test_missing_eligibility_cannot_turn_positive_gold_into_a_pass() -> None:
    snapshot = artificial([("Zeynep.", "S1"), ("Sunum dosyasını hazırlayacak.", "S1")])
    no_metadata = replace(snapshot, spans=())
    assert not prepare_options(no_metadata)
    assert expected_positions("zeynep-separated", no_metadata) == (6,)
    projected = project(no_metadata, BoundarySelection(remove_full_stops=[]))
    assert projected.changed_positions != expected_positions("zeynep-separated", no_metadata)


@pytest.mark.parametrize("source_ok,model_ok", [(False, True), (True, False), (False, False)])
def test_changed_source_or_model_always_fails_acceptance(source_ok: bool, model_ok: bool) -> None:
    assert not acceptance_passed(True, source_ok, model_ok)
