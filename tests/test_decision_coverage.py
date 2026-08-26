from __future__ import annotations

import pytest

from fpl_model.validation.decision_coverage import (
    CoverageCount,
    evaluate_decision_coverage,
)


def test_coverage_count_computes_ratio_and_pass():
    complete = CoverageCount(label="owned_squad", covered=15, excluded_missing_projection=0)
    assert complete.total == 15
    assert complete.coverage == pytest.approx(1.0)
    assert complete.passes is True

    incomplete = CoverageCount(label="shortlist", covered=380, excluded_missing_projection=20)
    assert incomplete.total == 400
    assert incomplete.coverage == pytest.approx(0.95)
    assert incomplete.passes is False


def test_coverage_count_rejects_negative_values():
    with pytest.raises(ValueError, match="must not be negative"):
        CoverageCount(label="x", covered=-1, excluded_missing_projection=0)
    with pytest.raises(ValueError, match="must not be negative"):
        CoverageCount(label="x", covered=0, excluded_missing_projection=-1)


def test_empty_pool_reports_full_coverage_by_convention():
    empty = CoverageCount(label="x", covered=0, excluded_missing_projection=0)
    assert empty.coverage == 1.0
    assert empty.passes is True


def test_evaluate_passes_when_owned_squad_and_all_shortlists_are_complete():
    owned_squad = CoverageCount(label="owned_squad", covered=15, excluded_missing_projection=0)
    shortlists = (
        CoverageCount(label="gw1_shortlist", covered=400, excluded_missing_projection=0),
        CoverageCount(label="gw2_shortlist", covered=402, excluded_missing_projection=0),
    )

    result = evaluate_decision_coverage(owned_squad=owned_squad, shortlists=shortlists)

    assert result["passes"] is True
    assert result["failing_pools"] == []
    assert result["owned_squad"]["passes"] is True
    assert all(row["passes"] for row in result["shortlists"])


def test_evaluate_fails_and_names_every_incomplete_shortlist():
    owned_squad = CoverageCount(label="owned_squad", covered=15, excluded_missing_projection=0)
    shortlists = (
        CoverageCount(label="gw1_shortlist", covered=380, excluded_missing_projection=20),
        CoverageCount(label="gw2_shortlist", covered=402, excluded_missing_projection=0),
        CoverageCount(label="gw3_shortlist", covered=390, excluded_missing_projection=12),
    )

    result = evaluate_decision_coverage(owned_squad=owned_squad, shortlists=shortlists)

    assert result["passes"] is False
    assert result["failing_pools"] == ["gw1_shortlist", "gw3_shortlist"]
    assert "RESEARCH_ONLY" in result["recommendation"]


def test_evaluate_fails_when_owned_squad_itself_is_incomplete():
    # load_lineup_inputs is expected to already prevent this upstream, but the
    # gate must still catch it rather than assume the invariant always holds.
    owned_squad = CoverageCount(label="owned_squad", covered=14, excluded_missing_projection=1)

    result = evaluate_decision_coverage(owned_squad=owned_squad)

    assert result["passes"] is False
    assert result["failing_pools"] == ["owned_squad"]


def test_evaluate_with_no_pools_passes_trivially():
    result = evaluate_decision_coverage()

    assert result["passes"] is True
    assert result["owned_squad"] is None
    assert result["shortlists"] == []
