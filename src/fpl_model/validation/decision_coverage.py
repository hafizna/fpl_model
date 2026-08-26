"""Enforce the documented 100% owned-squad/optimizer-shortlist coverage gate.

`docs/PROJECTION_COVERAGE_AUDIT.md` already states the Sprint 3 policy: at least
95% coverage among selectable players (checked by
`validation.projection_coverage`) **and 100% coverage for every optimizer
shortlist and selected squad**. The 95% half has a script
(`scripts/audit_projection_coverage.py`); the 100% half was only ever prose --
`decision/lineup_store.py` already fails closed for an owned squad, and
`decision/transfer_store.py`/`rolling_store.py`/`initial_squad_store.py` already
count excluded-missing-projection players, but nothing turned those counts into a
checked, reportable pass/fail verdict. This module is that check.

It does not change what any decision command computes. It reads the same
diagnostics those commands already produce and renders a verdict plus a
`RESEARCH_ONLY` recommendation, so a decision output can honestly say whether it
met the documented coverage bar instead of only printing raw counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

REQUIRED_SHORTLIST_COVERAGE = 1.0


@dataclass(frozen=True, slots=True)
class CoverageCount:
    """One pool's coverage: how many candidates had a usable projection."""

    label: str
    covered: int
    excluded_missing_projection: int

    def __post_init__(self) -> None:
        if self.covered < 0:
            raise ValueError(f"{self.label}: covered must not be negative")
        if self.excluded_missing_projection < 0:
            raise ValueError(f"{self.label}: excluded_missing_projection must not be negative")

    @property
    def total(self) -> int:
        return self.covered + self.excluded_missing_projection

    @property
    def coverage(self) -> float:
        return self.covered / self.total if self.total else 1.0

    @property
    def passes(self) -> bool:
        return self.excluded_missing_projection == 0


def _count_report(count: CoverageCount) -> dict[str, Any]:
    return {
        "label": count.label,
        "covered": count.covered,
        "excluded_missing_projection": count.excluded_missing_projection,
        "total": count.total,
        "coverage": count.coverage,
        "passes": count.passes,
    }


def evaluate_decision_coverage(
    *,
    owned_squad: CoverageCount | None = None,
    shortlists: tuple[CoverageCount, ...] = (),
) -> dict[str, Any]:
    """Render a pass/fail coverage verdict for one decision output.

    ``owned_squad`` covers a manager's fixed 15 players -- already enforced as a
    hard failure upstream by ``decision.lineup_store.load_lineup_inputs``, so it
    is expected to always report ``passes=True`` here; it is still checked
    explicitly so this function is the one place the documented policy is
    verified, rather than trusting that invariant silently.

    ``shortlists`` covers every candidate pool a decision searches over --
    transfer targets, a rolling-planner Gameweek pool, or an initial-squad
    Gameweek pool. Each requires the documented 100% coverage; a shortlist that
    silently excluded missing-projection players (the current behaviour of
    ``transfer_store``/``rolling_store``/``initial_squad_store``) is exactly the
    Sprint 3 failure mode this gate is meant to catch and surface, not hide.
    """
    counts: list[CoverageCount] = []
    if owned_squad is not None:
        counts.append(owned_squad)
    counts.extend(shortlists)

    failing = [count.label for count in counts if not count.passes]
    passes = not failing

    return {
        "label": "decision_coverage_gate_v1",
        "required_shortlist_coverage": REQUIRED_SHORTLIST_COVERAGE,
        "passes": passes,
        "failing_pools": failing,
        "owned_squad": None if owned_squad is None else _count_report(owned_squad),
        "shortlists": [_count_report(count) for count in shortlists],
        "recommendation": (
            "Coverage gate passes; this alone does not clear calibration, uncertainty, or "
            "squad-economics gates."
            if passes
            else "Coverage gate fails: label this output RESEARCH_ONLY per "
            "docs/PROJECTION_COVERAGE_AUDIT.md until every listed pool reaches 100%."
        ),
        "limitations": [
            "This gate only checks whether every candidate in a pool had a usable projection. "
            "It does not check calibration, uncertainty, freshness, or squad-legality gates.",
            "owned_squad is expected to always pass because load_lineup_inputs already fails "
            "closed on any missing owned-player projection before this gate ever runs.",
        ],
    }
