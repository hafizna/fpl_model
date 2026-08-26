"""Derive one explicit research/shadow/production approval state for a release.

Every Sprint 5 gate built so far (manifest, freshness, approval, coverage) reports
its own pass/fail. Nothing yet turns those into the one explicit state
`docs/PROJECTION_COVERAGE_AUDIT.md` and the Sprint 6/7/8 roadmap items already
assume exists: `RESEARCH_ONLY`, or eventually `shadow`/`production`. This module
is that derivation -- read-only composition over already-computed gate reports,
adding no new checks of its own.

State definitions:

- ``research`` -- the release is not yet safe to call anything but exploratory.
  Triggered by a failed manifest (lineage does not cohere), a failed freshness
  check (a lookahead hazard or a Gameweek missing from its own source
  snapshot -- never mere staleness/incompleteness, which freshness already
  reports as flags rather than failures), or -- when supplied -- any decision
  coverage gate below the documented 100% threshold.
- ``shadow`` -- manifest and freshness both pass, and every supplied coverage
  gate (if any) passes, but calibration/uncertainty are not yet APPROVED
  (`approval_status == "shadow_only"`). This is the expected state for every
  release today.
- ``production`` -- manifest, freshness, and every supplied coverage gate all
  pass, AND calibration/uncertainty are APPROVED. No release in this database
  currently qualifies; see `docs/SPRINT4_UNCERTAINTY_AND_CALIBRATION.md`.

A caller decides how to react to each state; this module only names it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

RESEARCH = "research"
SHADOW = "shadow"
PRODUCTION = "production"


@dataclass(frozen=True, slots=True)
class ReleaseHealth:
    report: dict[str, Any]

    @property
    def state(self) -> str:
        return str(self.report["state"])

    @property
    def label(self) -> str:
        """The exact user-facing label per docs/PROJECTION_COVERAGE_AUDIT.md."""
        return "RESEARCH_ONLY" if self.state == RESEARCH else self.state.upper()


def determine_release_health(
    *,
    orchestration_report: dict[str, Any],
    coverage_gates: tuple[dict[str, Any], ...] = (),
) -> ReleaseHealth:
    """Derive one research/shadow/production state from already-computed reports.

    ``orchestration_report`` is the ``.report`` from
    ``validation.release_orchestration.orchestrate_release_validation``.
    ``coverage_gates`` are zero or more ``.report``\\ -shaped dicts from
    ``validation.decision_coverage.evaluate_decision_coverage`` -- pass the
    gate(s) relevant to whatever decision output this health check is labelling
    (e.g. a lineup's single `owned_squad`-only gate, or a rolling plan's
    multi-shortlist gate). Passing none means coverage is simply not evaluated
    as part of this state -- it does not default to either passing or failing.
    """
    reasons: list[str] = []

    manifest_passes = bool(orchestration_report["manifest"]["linkage"]["passes"])
    if not manifest_passes:
        reasons.append("release manifest linkage fails")

    freshness_passes = bool(orchestration_report["freshness"]["passes"])
    if not freshness_passes:
        reasons.append("release freshness check fails (lookahead hazard or missing Gameweek data)")

    coverage_passes = True
    for gate in coverage_gates:
        if not bool(gate["passes"]):
            coverage_passes = False
            reasons.append(
                "decision coverage gate fails for pools: " + ", ".join(gate["failing_pools"])
            )

    approval_status = str(orchestration_report["approval_status"])

    if not manifest_passes or not freshness_passes or not coverage_passes:
        state = RESEARCH
    elif approval_status == "approved":
        state = PRODUCTION
    else:
        state = SHADOW

    payload: dict[str, Any] = {
        "label": "release_health_v1",
        "state": state,
        "reasons": reasons,
        "inputs": {
            "manifest_passes": manifest_passes,
            "freshness_passes": freshness_passes,
            "coverage_passes": coverage_passes,
            "coverage_gates_evaluated": len(coverage_gates),
            "approval_status": approval_status,
        },
        "limitations": [
            "This module only derives a state name from gate reports already computed "
            "elsewhere; it runs no query and performs no new check of its own.",
            "Omitting coverage_gates does not count as passing coverage -- it means coverage "
            "was not part of this particular state determination at all.",
            "'production' additionally requires the remaining Sprint 4 confirmatory calibration/"
            "uncertainty work and explicit production sign-off before operational trust.",
        ],
    }
    return ReleaseHealth(report=payload)
