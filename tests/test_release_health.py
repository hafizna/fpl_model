from __future__ import annotations

import pytest

from fpl_model.validation.decision_coverage import CoverageCount, evaluate_decision_coverage
from fpl_model.validation.release_health import (
    PRODUCTION,
    RESEARCH,
    SHADOW,
    determine_release_health,
)


def _orchestration_report(*, manifest_passes: bool, freshness_passes: bool, approval_status: str):
    return {
        "manifest": {"linkage": {"passes": manifest_passes}},
        "freshness": {"passes": freshness_passes},
        "approval_status": approval_status,
    }


def test_shadow_when_manifest_and_freshness_pass_but_approval_is_shadow_only():
    report = _orchestration_report(
        manifest_passes=True, freshness_passes=True, approval_status="shadow_only"
    )

    health = determine_release_health(orchestration_report=report)

    assert health.state == SHADOW
    assert health.label == "SHADOW"
    assert health.report["reasons"] == []


def test_production_when_everything_passes_and_approval_is_approved():
    report = _orchestration_report(
        manifest_passes=True, freshness_passes=True, approval_status="approved"
    )

    health = determine_release_health(orchestration_report=report)

    assert health.state == PRODUCTION
    assert health.label == "PRODUCTION"


def test_research_when_manifest_fails():
    report = _orchestration_report(
        manifest_passes=False, freshness_passes=True, approval_status="approved"
    )

    health = determine_release_health(orchestration_report=report)

    assert health.state == RESEARCH
    assert health.label == "RESEARCH_ONLY"
    assert "manifest" in health.report["reasons"][0]


def test_research_when_freshness_fails():
    report = _orchestration_report(
        manifest_passes=True, freshness_passes=False, approval_status="approved"
    )

    health = determine_release_health(orchestration_report=report)

    assert health.state == RESEARCH
    assert any("freshness" in reason for reason in health.report["reasons"])


def test_research_when_a_supplied_coverage_gate_fails():
    report = _orchestration_report(
        manifest_passes=True, freshness_passes=True, approval_status="approved"
    )
    failing_gate = evaluate_decision_coverage(
        shortlists=(
            CoverageCount(label="gw1_pool", covered=380, excluded_missing_projection=20),
        )
    )

    health = determine_release_health(
        orchestration_report=report, coverage_gates=(failing_gate,)
    )

    assert health.state == RESEARCH
    assert any("gw1_pool" in reason for reason in health.report["reasons"])
    assert health.report["inputs"]["coverage_passes"] is False


def test_passing_coverage_gate_does_not_block_shadow_or_production():
    report = _orchestration_report(
        manifest_passes=True, freshness_passes=True, approval_status="shadow_only"
    )
    passing_gate = evaluate_decision_coverage(
        owned_squad=CoverageCount(label="owned_squad", covered=15, excluded_missing_projection=0)
    )

    health = determine_release_health(
        orchestration_report=report, coverage_gates=(passing_gate,)
    )

    assert health.state == SHADOW
    assert health.report["inputs"]["coverage_passes"] is True
    assert health.report["inputs"]["coverage_gates_evaluated"] == 1


def test_omitting_coverage_gates_does_not_count_as_passing_or_failing():
    report = _orchestration_report(
        manifest_passes=True, freshness_passes=True, approval_status="approved"
    )

    health = determine_release_health(orchestration_report=report)

    assert health.state == PRODUCTION
    assert health.report["inputs"]["coverage_gates_evaluated"] == 0
    assert health.report["inputs"]["coverage_passes"] is True


@pytest.mark.parametrize(
    ("manifest_passes", "freshness_passes"),
    [(False, False), (False, True), (True, False)],
)
def test_manifest_or_freshness_failure_always_yields_research(manifest_passes, freshness_passes):
    report = _orchestration_report(
        manifest_passes=manifest_passes,
        freshness_passes=freshness_passes,
        approval_status="approved",
    )

    health = determine_release_health(orchestration_report=report)

    assert health.state == RESEARCH
