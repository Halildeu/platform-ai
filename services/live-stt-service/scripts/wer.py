"""Word Error Rate for the #43 performance matrix.

Pure functions (no I/O, no GPU): Turkish-aware normalization + word-level
Levenshtein alignment with substitution/deletion/insertion counts. Unit-tested
on the laptop so the GPU run only has to feed in (reference, hypothesis) pairs.

WER = (S + D + I) / N_ref, the standard speech-recognition accuracy metric.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import cast

# `\w` already covers Turkish letters under re.UNICODE; replace everything else
# (punctuation) with a space before splitting into words.
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_tr(text: str) -> list[str]:
    """Lowercase Turkish-correctly, strip punctuation, split into words.

    `I`/`İ` are mapped explicitly before lowercasing because Python's default
    `str.lower()` does not apply Turkish dotless/dotted-i rules.
    """
    text = text.replace("İ", "i").replace("I", "ı")
    text = text.lower()
    text = _NON_WORD.sub(" ", text)
    return text.split()


def _align(ref: list[str], hyp: list[str]) -> tuple[int, int, int, int]:
    """Return (substitutions, deletions, insertions, hits) via edit-distance DP."""
    n, m = len(ref), len(hyp)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if ref[i - 1] == hyp[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j - 1], dp[i - 1][j], dp[i][j - 1])

    i, j = n, m
    sub = dele = ins = hit = 0
    while i > 0 or j > 0:
        if i > 0 and j > 0 and ref[i - 1] == hyp[j - 1] and dp[i][j] == dp[i - 1][j - 1]:
            hit += 1
            i -= 1
            j -= 1
        elif i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + 1:
            sub += 1
            i -= 1
            j -= 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            dele += 1
            i -= 1
        else:
            ins += 1
            j -= 1
    return sub, dele, ins, hit


def word_error_rate(reference: str, hypothesis: str) -> dict[str, object]:
    """WER for one (reference, hypothesis) pair."""
    ref = normalize_tr(reference)
    hyp = normalize_tr(hypothesis)
    sub, dele, ins, hit = _align(ref, hyp)
    ref_n = len(ref)
    wer = (0.0 if not hyp else 1.0) if ref_n == 0 else (sub + dele + ins) / ref_n
    return {
        "wer": wer,
        "substitutions": sub,
        "deletions": dele,
        "insertions": ins,
        "hits": hit,
        "ref_words": ref_n,
    }


def corpus_wer(pairs: Sequence[tuple[str, str]]) -> dict[str, object]:
    """Aggregate WER over many pairs (micro-average: total edits / total words)."""
    tot_s = tot_d = tot_i = tot_ref = 0
    per_sample: list[dict[str, object]] = []
    for reference, hypothesis in pairs:
        r = word_error_rate(reference, hypothesis)
        tot_s += cast(int, r["substitutions"])
        tot_d += cast(int, r["deletions"])
        tot_i += cast(int, r["insertions"])
        tot_ref += cast(int, r["ref_words"])
        per_sample.append(r)
    corpus = (tot_s + tot_d + tot_i) / tot_ref if tot_ref else 0.0
    return {
        "corpus_wer": corpus,
        "substitutions": tot_s,
        "deletions": tot_d,
        "insertions": tot_i,
        "ref_words": tot_ref,
        "samples": len(pairs),
        "per_sample": per_sample,
    }
