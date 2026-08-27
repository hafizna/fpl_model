"""Derive an explicit playing-time role state from appearance projection inputs.

P0 (`README.md`'s "Production critical path") requires distinguishing starter,
substitute, unused substitute, not-in-squad, unavailable, and not-yet-eligible
observations, and exposing a role state (`unknown`, `likely_starter`,
`rotation`, `likely_bench`, `unavailable`) derived from official starts,
minutes, availability, and reviewed evidence -- rather than collapsing every
non-starter into one undifferentiated "not projected to start" bucket a manager
has to reverse-engineer from a raw `start_probability` number.

This module is deliberately narrow: it maps already-computed projection
inputs (`start_probability`, `appearance_probability`, eligibility, and data
quality flags already produced by `model/baseline_pipeline.py` and
`context/availability.py`) onto one explicit category, plus a short reason a
manager can read directly. It does not compute or alter `start_probability`,
`expected_minutes`, or any xPts component -- it is read-only on top of
projection inputs that already exist.

Thresholds are deliberately coarse and documented rather than tuned to any
backtest: this is a decision-safety presentation layer, not a scoring
component, so a threshold change here can never move any projection or
recommendation objective.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import duckdb

UNAVAILABLE: Final = "unavailable"
UNKNOWN: Final = "unknown"
LIKELY_BENCH: Final = "likely_bench"
ROTATION: Final = "rotation"
LIKELY_STARTER: Final = "likely_starter"

ROLE_STATES: Final = (UNAVAILABLE, UNKNOWN, LIKELY_BENCH, ROTATION, LIKELY_STARTER)

# A starter-probability band, not a start-vs-bench boundary in isolation --
# combined with appearance_probability below to also catch a player who is
# rarely a starter but frequently used as an impactful substitute (e.g. an
# out-and-out impact sub), who should read as ROTATION, not LIKELY_BENCH.
# Public (not underscore-prefixed): validation.material_conflict reuses these
# exact thresholds so "low projected start probability" and "rotation" mean
# the same number in both modules rather than drifting independently.
LIKELY_STARTER_THRESHOLD: Final = 0.75
ROTATION_THRESHOLD: Final = 0.30
LIKELY_BENCH_APPEARANCE_THRESHOLD: Final = 0.30


@dataclass(frozen=True, slots=True)
class RoleStateResult:
    role_state: str
    reason: str

    def __post_init__(self) -> None:
        if self.role_state not in ROLE_STATES:
            raise ValueError(f"unknown role_state: {self.role_state!r}")
        if not self.reason.strip():
            raise ValueError("reason must not be blank")


def derive_role_state(
    *,
    is_eligible: bool | None,
    start_probability: float | None,
    appearance_probability: float | None,
) -> RoleStateResult:
    """Map projection inputs onto one explicit role state plus a plain reason.

    ``is_eligible`` is the resolved availability outcome (``None`` means the
    availability policy could not resolve it -- see
    `docs/PIPELINE_ARCHITECTURE.md`'s availability resolution boundary).
    ``start_probability``/``appearance_probability`` are read directly from
    the player's own projection; ``None`` means the upstream appearance
    projection itself is unavailable (`MISSING_APPEARANCE_PROJECTION`),
    which is a strictly different condition from a resolved 0.0 probability
    and must not be silently treated as one.
    """
    if is_eligible is False:
        return RoleStateResult(
            role_state=UNAVAILABLE,
            reason="Resolved unavailable this Gameweek (injury, suspension, or eligibility block).",
        )
    if is_eligible is None or start_probability is None or appearance_probability is None:
        return RoleStateResult(
            role_state=UNKNOWN,
            reason="Availability or appearance evidence is incomplete for this Gameweek.",
        )
    if start_probability >= LIKELY_STARTER_THRESHOLD:
        return RoleStateResult(
            role_state=LIKELY_STARTER,
            reason=f"Projected to start with probability {start_probability:.0%}.",
        )
    if (
        start_probability >= ROTATION_THRESHOLD
        or appearance_probability >= LIKELY_BENCH_APPEARANCE_THRESHOLD
    ):
        return RoleStateResult(
            role_state=ROTATION,
            reason=(
                f"Uncertain starting role: start probability {start_probability:.0%}, "
                f"appearance probability {appearance_probability:.0%}."
            ),
        )
    return RoleStateResult(
        role_state=LIKELY_BENCH,
        reason=(
            f"Low projected involvement: start probability {start_probability:.0%}, "
            f"appearance probability {appearance_probability:.0%}."
        ),
    )


def _combine_start_probability(start_probabilities: list[float]) -> float:
    """Probability of starting in >=1 fixture, for a double Gameweek.

    Mirrors `decision.lineup_store.combine_appearance_probability`'s own
    independent-events combination: the probability of starting neither
    fixture is the product of each fixture's own non-start probability.
    """
    non_start_probability = 1.0
    for probability in start_probabilities:
        non_start_probability *= max(0.0, 1.0 - probability)
    return 1.0 - non_start_probability


def load_role_states(
    connection: duckdb.DuckDBPyConnection,
    *,
    model_run_id: str,
    fpl_ids: tuple[int, ...],
) -> dict[int, RoleStateResult]:
    """Derive one role state per player from this run's own projection inputs.

    Resolves each player's eligibility from the SAME availability resolution
    run the model run's own appearance projection used (`model_run` ->
    `baseline_projection_run.appearance_projection_run_id` ->
    `appearance_projection_run.availability_resolution_run_id`), and
    start/appearance probability from `player_fixture_projection`, combined
    across fixtures for a double Gameweek the same way
    `combine_appearance_probability` already does. A player entirely absent
    from `player_fixture_projection` for this run gets ``UNKNOWN`` rather than
    raising -- this is a display-only diagnostic layered on production
    projections, not a replacement for the hard coverage gate those
    projections must already pass.
    """
    if not fpl_ids:
        return {}

    metadata = connection.execute(
        """
        SELECT apr.availability_resolution_run_id
        FROM baseline_projection_run AS b
        JOIN appearance_projection_run AS apr
          ON apr.projection_run_id = b.appearance_projection_run_id
        WHERE b.model_run_id = ?
        """,
        [model_run_id],
    ).fetchone()
    if metadata is None:
        raise ValueError(f"no baseline_projection_run/appearance lineage for {model_run_id}")
    resolution_run_id = metadata[0]

    placeholders = ",".join("?" * len(fpl_ids))
    eligibility_rows = connection.execute(
        f"""
        SELECT fpl_id, is_eligible
        FROM player_availability_resolution
        WHERE resolution_run_id = ? AND fpl_id IN ({placeholders})
        """,
        [resolution_run_id, *fpl_ids],
    ).fetchall()
    eligibility_by_fpl_id: dict[int, bool | None] = {
        int(fpl_id): (None if is_eligible is None else bool(is_eligible))
        for fpl_id, is_eligible in eligibility_rows
    }

    code_rows = connection.execute(
        f"""
        SELECT ps.fpl_id, ps.player_code
        FROM player_snapshot AS ps
        JOIN model_run AS m ON m.source_ingestion_run_id = ps.ingestion_run_id
        WHERE m.model_run_id = ? AND ps.fpl_id IN ({placeholders})
        """,
        [model_run_id, *fpl_ids],
    ).fetchall()
    code_to_fpl_id = {int(code): int(fpl_id) for fpl_id, code in code_rows if code is not None}

    projection_rows: list[dict[str, Any]] = []
    if code_to_fpl_id:
        codes = tuple(code_to_fpl_id)
        code_placeholders = ",".join("?" * len(codes))
        projection_rows = connection.execute(
            f"""
            SELECT player_code, start_probability, substitute_appearance_probability
            FROM player_fixture_projection
            WHERE model_run_id = ? AND player_code IN ({code_placeholders})
            """,
            [model_run_id, *codes],
        ).fetchall()

    starts_by_code: dict[int, list[float]] = {}
    appearances_by_code: dict[int, list[float]] = {}
    for player_code, start_probability, sub_probability in projection_rows:
        starts_by_code.setdefault(int(player_code), []).append(float(start_probability))
        appearances_by_code.setdefault(int(player_code), []).append(
            float(start_probability) + float(sub_probability)
        )

    result: dict[int, RoleStateResult] = {}
    for fpl_id in fpl_ids:
        player_code = next(
            (code for code, mapped_id in code_to_fpl_id.items() if mapped_id == fpl_id), None
        )
        start_rows = starts_by_code.get(player_code) if player_code is not None else None
        appearance_rows = appearances_by_code.get(player_code) if player_code is not None else None
        result[fpl_id] = derive_role_state(
            is_eligible=eligibility_by_fpl_id.get(fpl_id),
            start_probability=(
                None if not start_rows else _combine_start_probability(start_rows)
            ),
            appearance_probability=(
                None if not appearance_rows else _combine_start_probability(appearance_rows)
            ),
        )
    return result


def role_state_report(result: RoleStateResult | None) -> dict[str, Any] | None:
    """Render one player's role state as a JSON-serialisable dict, or None."""
    if result is None:
        return None
    return {"role_state": result.role_state, "reason": result.reason}
