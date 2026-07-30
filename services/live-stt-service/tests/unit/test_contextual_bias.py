from __future__ import annotations

import pytest

from app.services.contextual_bias import (
    MAX_CONTEXT_TERM_CHARS,
    MAX_CONTEXT_TERMS,
    ContextualBiasError,
    normalize_context_terms,
)


def test_normalizes_deduplicates_and_preserves_turkish_names() -> None:
    context = normalize_context_terms(["  Çağrı   Öztürk ", "çağrı öztürk", "Proje-24"])

    assert context.terms == ("Çağrı Öztürk", "Proje-24")
    assert context.hotwords == "Çağrı Öztürk, Proje-24"


def test_empty_context_disables_bias() -> None:
    assert normalize_context_terms([]).hotwords is None


@pytest.mark.parametrize(
    "terms",
    [
        ["satır\nsonu"],
        ["isim, komut"],
        ["x" * (MAX_CONTEXT_TERM_CHARS + 1)],
        ["ok"] * (MAX_CONTEXT_TERMS + 1),
        ["ok", 7],
    ],
)
def test_rejects_unsafe_or_unbounded_context(terms: list[object]) -> None:
    with pytest.raises(ContextualBiasError):
        normalize_context_terms(terms)
