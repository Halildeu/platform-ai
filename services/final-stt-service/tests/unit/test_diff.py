from __future__ import annotations

from app.services.diff import DiffOperation, diff_transcripts


def test_diff_detects_equal_replace_and_insert() -> None:
    operations = diff_transcripts(
        "Bugün hava güzel",
        "Bugün toplantı çok güzel",
    )

    assert [item.operation for item in operations] == [
        DiffOperation.EQUAL,
        DiffOperation.REPLACE,
        DiffOperation.EQUAL,
    ]
    assert operations[1].before_text == "hava"
    assert operations[1].after_text == "toplantı çok"


def test_diff_detects_delete() -> None:
    operations = diff_transcripts("bir iki üç", "bir üç")

    assert operations[1].operation == DiffOperation.DELETE
    assert operations[1].before_text == "iki"
    assert operations[1].after_text == ""


def test_diff_is_case_and_punctuation_insensitive_for_equal_words() -> None:
    operations = diff_transcripts("Merhaba, Dünya!", "merhaba dünya")

    assert len(operations) == 1
    assert operations[0].operation == DiffOperation.EQUAL
