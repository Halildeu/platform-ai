"""Unit tests for the #43 WER helpers (GPU-free)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import wer  # noqa: E402


def test_normalize_lowercases_turkish() -> None:
    assert wer.normalize_tr("İSTANBUL Ilık") == ["istanbul", "ılık"]


def test_normalize_strips_punctuation() -> None:
    assert wer.normalize_tr("Merhaba, dünya!") == ["merhaba", "dünya"]


def test_perfect_match_is_zero() -> None:
    r = wer.word_error_rate("bir iki üç", "bir iki üç")
    assert r["wer"] == 0.0
    assert r["hits"] == 3
    assert r["ref_words"] == 3


def test_single_substitution() -> None:
    r = wer.word_error_rate("bir iki üç", "bir dört üç")
    assert r["substitutions"] == 1
    assert r["wer"] == pytest.approx(1 / 3)


def test_deletion() -> None:
    r = wer.word_error_rate("bir iki üç", "bir üç")
    assert r["deletions"] == 1
    assert r["wer"] == pytest.approx(1 / 3)


def test_insertion() -> None:
    r = wer.word_error_rate("bir iki", "bir iki üç")
    assert r["insertions"] == 1
    assert r["wer"] == pytest.approx(1 / 2)


def test_case_and_punctuation_ignored() -> None:
    r = wer.word_error_rate("Geçiş ülkelerinde.", "geçiş ülkelerinde")
    assert r["wer"] == 0.0


def test_empty_reference_empty_hyp() -> None:
    assert wer.word_error_rate("", "")["wer"] == 0.0


def test_empty_reference_nonempty_hyp_is_one() -> None:
    assert wer.word_error_rate("", "fazladan kelime")["wer"] == 1.0


def test_corpus_micro_average() -> None:
    pairs = [
        ("bir iki üç", "bir iki üç"),  # 0 errors, 3 words
        ("dört beş", "dört altı"),  # 1 sub, 2 words
    ]
    agg = wer.corpus_wer(pairs)
    assert agg["ref_words"] == 5
    assert agg["substitutions"] == 1
    assert agg["corpus_wer"] == pytest.approx(1 / 5)
    assert agg["samples"] == 2
