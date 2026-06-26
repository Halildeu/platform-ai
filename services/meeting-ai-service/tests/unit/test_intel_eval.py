"""Tests for metadata-only G-INT eval metrics."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import intel_eval  # noqa: E402


def _citation(claim: str, *, status: str = "PASSED") -> SimpleNamespace:
    return SimpleNamespace(
        claim=claim,
        grounded=status == "PASSED",
        status=status,
    )


def test_citation_metrics_require_passed_citation_per_visible_output() -> None:
    result = SimpleNamespace(
        summary="Bütçe artışı onaylandı.",
        decisions=["Lansman eylülde yapılacak"],
        action_items=[SimpleNamespace(text="Ali raporu hazırlayacak")],
        summary_citations=[_citation("Bütçe artışı onaylandı.")],
        citations=[
            _citation("Lansman eylülde yapılacak"),
            _citation("Ali raporu hazırlayacak"),
        ],
    )

    assert intel_eval._citation_metrics(result) == {
        "citation_coverage": 1.0,
        "summary_verified_rate": 1.0,
    }


def test_citation_metrics_penalize_empty_summary_and_missing_output_citation() -> None:
    result = SimpleNamespace(
        summary="",
        decisions=["Lansman eylülde yapılacak"],
        action_items=[SimpleNamespace(text="Ali raporu hazırlayacak")],
        summary_citations=[],
        citations=[_citation("Lansman eylülde yapılacak")],
    )

    assert intel_eval._citation_metrics(result) == {
        "citation_coverage": 0.5,
        "summary_verified_rate": 0.0,
    }


def test_citation_metrics_ignore_failed_citations() -> None:
    result = SimpleNamespace(
        summary="Bütçe artışı onaylandı.",
        decisions=[],
        action_items=[],
        summary_citations=[_citation("Bütçe artışı onaylandı.", status="FAILED")],
        citations=[],
    )

    assert intel_eval._citation_metrics(result) == {
        "citation_coverage": 0.0,
        "summary_verified_rate": 0.0,
    }


def test_citation_metrics_no_outputs_edge_is_vacuous_for_coverage_only() -> None:
    result = SimpleNamespace(
        summary="",
        decisions=[],
        action_items=[],
        summary_citations=[],
        citations=[],
    )

    assert intel_eval._citation_metrics(result) == {
        "citation_coverage": 1.0,
        "summary_verified_rate": 0.0,
    }
