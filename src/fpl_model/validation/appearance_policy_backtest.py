"""Causal, out-of-sample head-to-head backtest of appearance calibration policies.

``scripts/diagnose_appearance_segments.py`` found the appearance model's
average overprediction bias is reliably concentrated in the highest
``start_probability``/``expected_minutes`` bands (paired contrast CIs above
zero) and reliably larger in the xPts high band than in a same-window
comparator. That diagnostic is READ-ONLY: it never recomputed ``predicted_xpts``
under any calibration, and therefore cannot say whether actually applying a
calibration policy would improve out-of-sample xPts error. This module answers
that question directly with three policies, causally applied and re-scored:

- ``raw``: no calibration (the existing, committed model -- the control).
- ``global``: apply the causal walk-forward ``start_probability`` calibration
  (``appearance_calibration.fit_ols``, refit every gameweek on strictly-prior,
  deadline-safe gameweeks only) to EVERY row.
- ``high_end_shrinkage``: apply that SAME per-gameweek fit, but ONLY to rows
  whose RAW ``start_probability`` is at or above 0.8 -- the fixed band edge
  ``validation.appearance_segments.START_PROBABILITY_BAND_EDGES`` already
  uses, reused here (not re-derived) so the policy's threshold is the exact
  band the segment diagnostic found concentrated bias in. Every other row
  keeps its raw, uncalibrated prediction.

IMPORTANT: none of these three policies is a pure ``start_probability``
substitution. Each applies an OLS transform to ``start_probability`` and then
PROPORTIONALLY RESCALES every field that depends on it
(``substitute_appearance_probability``, ``sixty_minute_probability``, and
``expected_minutes``) so the resulting ``AppearanceProjection`` stays
internally consistent -- see ``rescale_appearance_projection``. The
out-of-sample performance results this module produces evaluate that COMPLETE
reconstruction rule (calibrated start_probability + proportional rescale of
its dependents), not a calibrated start_probability held in isolation against
otherwise-untouched dependent fields.

``expected_minutes`` OLS calibration specifically is deliberately OUT OF
SCOPE: tracing every ``weight_*``/``project_benchwarmers_*`` function in
``fpl_model.model``, only ``AppearanceProjection.start_probability`` (and the
fields derived from it) are ever read by the scoring chain -- fitting or
applying a SEPARATE ``expected_minutes``-target calibration could not change
any policy's score, so this module never does that (see module-level
``EXPECTED_MINUTES_OUT_OF_SCOPE_NOTE``). This is distinct from
``rescale_appearance_projection`` RECOMPUTING ``expected_minutes`` from the
already-calibrated start/substitute probabilities for internal consistency
(see below) -- that recomputation is not a calibration of ``expected_minutes``
itself, has no independent fitted parameters, and still cannot move any
score, since ``expected_minutes`` remains causally inert for scoring purposes
either way.

Calibrating ``start_probability`` alone leaves ``AppearanceProjection``'s other
derived fields internally INCONSISTENT if left untouched (e.g.
``sixty_minute_probability`` would no longer correspond to the calibrated
``start_probability`` that produced it, and ``expected_minutes`` would still
reflect the RAW, uncalibrated probabilities). ``rescale_appearance_projection``
rebuilds a fully self-consistent projection: every quantity DEPENDENT on
``start_probability`` (``substitute_appearance_probability`` -- itself
dependent despite describing the non-start branch -- and
``sixty_minute_probability``) is scaled by
``calibrated_start_probability / raw_start_probability``, clamped to its own
natural ceiling; ``appearance_probability``/``appearance_xpts``/
``sixty_minute_xpts``/``total_xpts``/``expected_minutes`` are then recomputed
from those rescaled values using the SAME arithmetic
``model.appearance.project_appearance``/``blend_conditional_appearance``
already use (``appearance_xpts = appearance_probability``, ``sixty_minute_xpts
= sixty_minute_probability``, ``expected_minutes = calibrated_start_probability
* mean_minutes_per_start + rescaled_substitute_probability *
mean_minutes_per_substitute``) -- not a new scoring rule, just re-deriving the
already-existing relationships from calibrated/rescaled source values instead
of the raw ones.

This module duplicates the SHAPE of
``benchwarmers_backtest.materialize_benchwarmers_walk_forward_backtest``'s
walk-forward loop (per this codebase's established convention of small local
duplication over cross-module coupling for validation code -- see
``appearance_calibration.py``'s own docstring for the same reasoning applied to
``fit_ols``), but never modifies that function, ``baseline_pipeline.py``, or
any ``project_benchwarmers_*``/``weight_*`` component formula. Unlike that
duplication precedent, this module does NOT duplicate the per-gameweek
upstream queries (``team_strength_as_of``, ``player_rates_as_of``,
``appearance_as_of``, league-average bonus rates) three times -- those are
policy-independent and computed ONCE per gameweek; only the appearance
projection substitution and the downstream ``weight_*``/
``compose_baseline_projection`` calls are repeated per policy, using the
UNCHANGED component functions every other backtest script in this codebase
calls.

No-lookahead: the ``start_probability`` calibration fit at gameweek ``G`` is
refit from strictly-prior, DEADLINE-SAFE calibration rows only (mirroring
``appearance_calibration.walk_forward_appearance_calibration``'s own causal
boundary) -- refreshed every step, never a single season-long fit.
``global``/``high_end_shrinkage`` predictions for gameweek ``G`` use ONLY that
step's own prior-gameweeks-only fit, never a fit derived from ``G``'s own or
any later gameweek's outcomes, and never a fit pooled across the whole
evaluated range (which would be in-sample with respect to later folds -- the
same bug already fixed in ``walk_forward_calibration.py``'s own pooled fit).

"Strictly-prior" is deliberately TWO conditions, not one:
``row.gameweek < G`` alone is not sufficient, since a postponed fixture can
carry an earlier gameweek label while kicking off (and having its outcome
known) only after a LATER gameweek's deadline. ``fit_for_gameweek`` therefore
also requires ``row.outcome_available_at <= target_deadline`` -- the same
``kickoff_time + outcome_delay <= target_deadline`` deadline-safety rule
``benchwarmers_backtest.py`` already applies to the primary xPts backtest,
here applied a second time to the CALIBRATION rows specifically (a fixture
could satisfy the primary backtest's own deadline-safety rule for the
appearance PREDICTION it produced, yet still have its realised OUTCOME
arrive too late to safely enter a particular gameweek's calibration FIT --
two related but distinct causal boundaries).

Read-only measurement: this module computes candidate policies and their
out-of-sample scores; it does not persist to DuckDB, does not change
``baseline_pipeline.py`` or any production projection path, and applying a
policy to production remains a separate, explicit decision this module does
not make.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import duckdb
import pandas as pd

from fpl_model.model.appearance import AppearanceProjection
from fpl_model.model.attacking import (
    AttackingWindow,
    project_benchwarmers_attacking_rates,
    weight_attacking_rates,
)
from fpl_model.model.baseline import BaselineComponentProjections, compose_baseline_projection
from fpl_model.model.defence import (
    project_benchwarmers_defensive_rates,
    weight_defensive_rates,
)
from fpl_model.model.fixture import FixtureContext
from fpl_model.model.secondary import (
    project_benchwarmers_bonus,
    project_benchwarmers_defcon,
    project_benchwarmers_saves,
    project_discipline,
    weight_defcon,
    weight_linear_component,
    weight_saves,
)
from fpl_model.validation.appearance_asof import appearance_as_of
from fpl_model.validation.appearance_calibration import OlsFit, fit_ols
from fpl_model.validation.backtest import BacktestObservation
from fpl_model.validation.benchwarmers_backtest import BenchwarmersBacktestResult
from fpl_model.validation.historical import infer_gameweek_deadlines
from fpl_model.validation.player_rates_asof import (
    has_usable_rate_history,
    league_average_bonus_rates_as_of,
    player_rates_as_of,
)
from fpl_model.validation.team_fixture_results import (
    build_team_fixture_results,
    build_team_name_to_id,
)
from fpl_model.validation.team_strength_asof import team_strength_as_of

REGULATION_MINUTES = 90.0

DOUBLE_GAMEWEEK_FIXTURE = "DOUBLE_GAMEWEEK_FIXTURE"
MISSING_APPEARANCE_HISTORY = "MISSING_APPEARANCE_HISTORY"
NO_USABLE_PLAYER_RATE_HISTORY = "NO_USABLE_PLAYER_RATE_HISTORY"
MISSING_TEAM_STRENGTH = "MISSING_TEAM_STRENGTH"
MISSING_TEAM_ID = "MISSING_TEAM_ID"
NO_LEAGUE_AVERAGE_BONUS_RATES = "NO_LEAGUE_AVERAGE_BONUS_RATES"
NEGATIVE_BONUS_SIGNAL = "NEGATIVE_BONUS_SIGNAL"

DEFAULT_MINIMUM_CALIBRATION_GAMEWEEKS = 5
HIGH_END_START_PROBABILITY_THRESHOLD = 0.8  # validation.appearance_segments' own [.8,1] band edge

PolicyName = Literal["raw", "global", "high_end_shrinkage"]
POLICIES: tuple[PolicyName, ...] = ("raw", "global", "high_end_shrinkage")

EXPECTED_MINUTES_OUT_OF_SCOPE_NOTE = (
    "A separate expected_minutes-target OLS calibration is out of scope for this policy "
    "backtest: tracing every weight_*/project_benchwarmers_* function in fpl_model.model, "
    "only AppearanceProjection.start_probability (and the fields dependent on it -- "
    "substitute_appearance_probability, sixty_minute_probability, appearance_xpts, "
    "sixty_minute_xpts) are ever read by the scoring chain -- fitting or applying an "
    "independent expected_minutes calibration could not change any policy's score, so this "
    "module never does that. This is distinct from rescale_appearance_projection "
    "RECOMPUTING expected_minutes from the already-calibrated start/substitute "
    "probabilities for internal consistency (calibrated_start_probability * "
    "mean_minutes_per_start + rescaled_substitute_probability * "
    "mean_minutes_per_substitute) -- that recomputation has no independently fitted "
    "parameters and still cannot move any score, since expected_minutes remains causally "
    "inert for scoring purposes either way. Both facts (no independent expected_minutes "
    "calibration; expected_minutes itself has no causal path to predicted_xpts) are "
    "notable findings in their own right, surfaced here explicitly, not a silent omission."
)


@dataclass(frozen=True, slots=True)
class AppearancePolicyCalibrationRow:
    """One prior-gameweek player-fixture's start_probability prediction/outcome pair.

    Deliberately minimal (only what ``fit_ols`` needs) and specific to
    ``start_probability`` -- unlike ``appearance_calibration.AppearanceCalibrationRow``,
    this module never builds an ``expected_minutes`` row (see
    ``EXPECTED_MINUTES_OUT_OF_SCOPE_NOTE``).

    ``outcome_available_at`` (``kickoff_time + outcome_delay``) records when
    this row's own ``actual_started`` outcome became knowable -- required so
    ``fit_for_gameweek`` can reject a row whose GAMEWEEK LABEL is earlier
    than the target gameweek but whose real-world OUTCOME was not yet
    available by the target gameweek's own deadline (a postponed fixture
    carrying an earlier gameweek label but kicking off, and having its
    outcome known, only after a later gameweek's deadline -- the same
    structural hazard ``benchwarmers_backtest.py``'s own deadline-safety fix
    already closed for the primary xPts backtest and
    ``appearance_calibration.py``'s own causal boundary does not yet guard
    against, since ``gameweek < target`` alone is not sufficient).
    """

    gameweek: int
    predicted_start_probability: float
    actual_started: bool
    outcome_available_at: datetime


def rescale_appearance_projection(
    raw: AppearanceProjection,
    *,
    calibrated_start_probability: float,
    mean_minutes_per_start: float,
    mean_minutes_per_substitute: float,
) -> AppearanceProjection:
    """Rebuild a fully self-consistent ``AppearanceProjection`` from a calibrated start_probability.

    ``calibrated_start_probability`` must itself already be clamped to
    ``[0, 1]`` by the caller (``apply_calibration_policy`` does this) -- this
    function trusts its input range and only handles the RATIO-based rescale
    of the raw projection's other DEPENDENT fields (i.e. every field derived
    from ``start_probability``, not just the ones conditional on having
    started -- ``substitute_appearance_probability`` is itself one such
    dependent field, even though it describes the NON-start branch, since
    the rescale ratio is applied to it too).

    Every dependent quantity on ``raw`` (``substitute_appearance_probability``,
    ``sixty_minute_probability``) is scaled by
    ``ratio = calibrated_start_probability / raw.start_probability`` and then
    clamped to its own natural ceiling:

    - ``substitute_appearance_probability`` cannot exceed
      ``1.0 - calibrated_start_probability`` (the two are mutually exclusive
      appearance outcomes; their sum is ``appearance_probability`` and must
      stay ``<= 1.0``).
    - ``sixty_minute_probability`` cannot exceed the resulting
      ``appearance_probability`` (a player cannot reach 60 minutes without
      having appeared at all).

    ``appearance_probability``, ``appearance_xpts``, ``sixty_minute_xpts``, and
    ``total_xpts`` are then recomputed from the (possibly clamped) rescaled
    values using the SAME arithmetic ``model.appearance.project_appearance``
    already uses -- ``appearance_xpts = appearance_probability``,
    ``sixty_minute_xpts = sixty_minute_probability``, ``total_xpts =
    appearance_xpts + sixty_minute_xpts`` -- never a new scoring rule.

    ``expected_minutes`` is likewise RECOMPUTED, not passed through
    unchanged, so the returned projection is genuinely self-consistent:
    ``calibrated_start_probability * mean_minutes_per_start +
    (rescaled/clamped) substitute_probability * mean_minutes_per_substitute``
    -- the SAME weighted-sum formula
    ``model.appearance.blend_conditional_appearance``/``project_appearance``
    already use to derive ``expected_minutes`` from start/substitute
    probabilities and per-appearance-type mean minutes, just fed the
    calibrated/rescaled probabilities as the source values instead of the
    raw ones. ``mean_minutes_per_start``/``mean_minutes_per_substitute`` are
    themselves POLICY-INDEPENDENT (the same per-player empirical minutes
    averages ``AppearanceHistoryAsOf`` already carries, unaffected by which
    calibration policy is being scored) -- passed in explicitly by the
    caller rather than read from ``raw`` itself, since ``AppearanceProjection``
    does not carry them.

    Recomputing ``expected_minutes`` this way is still governed by
    ``EXPECTED_MINUTES_OUT_OF_SCOPE_NOTE``: this module never fits or
    applies an ``expected_minutes``-target OLS calibration (the calibrated
    value is derived entirely from the already-calibrated start/substitute
    PROBABILITIES, not from any expected_minutes-specific fit), and
    ``expected_minutes`` still has no causal path to ``predicted_xpts`` --
    so this recomputation cannot change any policy's score. It exists
    purely so the returned ``AppearanceProjection`` is not internally
    inconsistent (a calibrated start_probability paired with an
    expected_minutes value implicitly derived from the RAW, uncalibrated
    probabilities) even though ``expected_minutes`` itself is inert for
    scoring purposes.

    Raises ``ValueError`` if ``raw.start_probability`` is exactly ``0.0``
    (the rescale ratio is undefined) -- callers should skip calibration for
    such a row (its raw prediction already has no dependent-field exposure
    to rescale) rather than dividing by zero.
    """
    if not 0.0 <= calibrated_start_probability <= 1.0:
        raise ValueError(
            f"calibrated_start_probability={calibrated_start_probability!r} must be "
            "clamped to [0, 1] by the caller before calling this function"
        )
    if raw.start_probability == 0.0:
        raise ValueError(
            "raw.start_probability is 0.0; the rescale ratio calibrated/raw is undefined -- "
            "callers should skip calibration for this row"
        )

    ratio = calibrated_start_probability / raw.start_probability
    substitute_probability = min(raw.substitute_appearance_probability * ratio, 1.0 - calibrated_start_probability)
    substitute_probability = max(substitute_probability, 0.0)
    appearance_probability = calibrated_start_probability + substitute_probability
    sixty_minute_probability = min(raw.sixty_minute_probability * ratio, appearance_probability)
    sixty_minute_probability = max(sixty_minute_probability, 0.0)

    appearance_xpts = appearance_probability
    sixty_minute_xpts = sixty_minute_probability
    expected_minutes = (
        calibrated_start_probability * mean_minutes_per_start
        + substitute_probability * mean_minutes_per_substitute
    )

    return AppearanceProjection(
        start_probability=calibrated_start_probability,
        substitute_appearance_probability=substitute_probability,
        appearance_probability=appearance_probability,
        sixty_minute_probability=sixty_minute_probability,
        expected_minutes=expected_minutes,
        appearance_xpts=appearance_xpts,
        sixty_minute_xpts=sixty_minute_xpts,
        total_xpts=appearance_xpts + sixty_minute_xpts,
    )


def apply_calibration_policy(
    raw: AppearanceProjection,
    *,
    policy: PolicyName,
    fit: OlsFit | None,
    mean_minutes_per_start: float,
    mean_minutes_per_substitute: float,
) -> AppearanceProjection:
    """Return the ``AppearanceProjection`` a given policy would use for one row.

    - ``"raw"``: returns ``raw`` unchanged (``fit`` is ignored, may be ``None``).
    - ``"global"``: applies ``fit`` to ``raw.start_probability`` unconditionally.
    - ``"high_end_shrinkage"``: applies ``fit`` ONLY when
      ``raw.start_probability >= HIGH_END_START_PROBABILITY_THRESHOLD``; below
      that, returns ``raw`` unchanged.

    This policy is NOT a pure start_probability substitution: it applies an
    OLS transform to ``start_probability`` and then PROPORTIONALLY RESCALES
    every dependent field (``substitute_appearance_probability``,
    ``sixty_minute_probability``, and now ``expected_minutes``) to keep the
    returned projection internally consistent (see
    ``rescale_appearance_projection``). The performance results this policy
    produces evaluate that COMPLETE reconstruction rule, not calibrated
    ``start_probability`` in isolation.

    ``fit`` must not be ``None`` for ``"global"``/``"high_end_shrinkage"`` --
    the caller (``materialize_appearance_policy_backtest``) never calls this
    for a gameweek without an eligible prior-gameweeks-only fit. The
    calibrated value is clamped to ``[0, 1]`` (start_probability's valid
    range) before being passed to ``rescale_appearance_projection`` -- an OLS
    fit is not itself bounded, so a fit extrapolating beyond the training
    data's range could otherwise produce an out-of-range calibrated
    probability.

    ``mean_minutes_per_start``/``mean_minutes_per_substitute`` are passed
    straight through to ``rescale_appearance_projection`` -- see that
    function's own docstring for why ``expected_minutes`` is recomputed
    from them rather than passed through unchanged.
    """
    if policy == "raw":
        return raw
    if fit is None:
        raise ValueError(f'fit must not be None for policy={policy!r}')
    if policy == "global":
        calibrated = fit.intercept + fit.slope * raw.start_probability
    elif policy == "high_end_shrinkage":
        if raw.start_probability < HIGH_END_START_PROBABILITY_THRESHOLD:
            return raw
        calibrated = fit.intercept + fit.slope * raw.start_probability
    else:
        raise ValueError(f"unknown policy: {policy!r}")

    clamped = min(max(calibrated, 0.0), 1.0)
    if raw.start_probability == 0.0:
        # Nothing to rescale against; a raw start_probability of exactly 0.0
        # has no dependent-field exposure for any policy to shrink/inflate.
        return raw
    return rescale_appearance_projection(
        raw,
        calibrated_start_probability=clamped,
        mean_minutes_per_start=mean_minutes_per_start,
        mean_minutes_per_substitute=mean_minutes_per_substitute,
    )


def _calibration_rows_by_gameweek(
    rows: list[AppearancePolicyCalibrationRow],
) -> dict[int, list[AppearancePolicyCalibrationRow]]:
    by_gameweek: dict[int, list[AppearancePolicyCalibrationRow]] = {}
    for row in rows:
        by_gameweek.setdefault(row.gameweek, []).append(row)
    return by_gameweek


def fit_for_gameweek(
    calibration_rows_by_gameweek: dict[int, list[AppearancePolicyCalibrationRow]],
    *,
    gameweek: int,
    target_deadline: datetime,
    minimum_calibration_gameweeks: int,
) -> OlsFit | None:
    """Fit start_probability OLS on deadline-safe prior rows only, or ``None`` if ineligible.

    A calibration row is eligible for gameweek ``G``'s fit only when BOTH:

    - ``row.gameweek < G`` (an earlier gameweek LABEL), and
    - ``row.outcome_available_at <= target_deadline`` (its own realised
      outcome was actually knowable by ``G``'s deadline).

    ``row.gameweek < G`` alone is NOT sufficient: a postponed fixture can
    carry an earlier gameweek label while kicking off, and having its
    outcome known, only after a LATER gameweek's deadline -- exactly the
    hazard ``benchwarmers_backtest.py``'s own deadline-safety fix already
    closed for the primary xPts backtest (``kickoff_time + outcome_delay <=
    target_deadline``, not just ``gameweek < N``). Without this second
    filter, such a fixture's outcome could leak into a fit for a gameweek
    whose own deadline preceded that outcome actually being known.

    A gameweek is eligible once at least ``minimum_calibration_gameweeks``
    distinct gameweeks have contributed AVAILABILITY-FILTERED rows -- the
    distinct-gameweek count is taken AFTER applying both filters above, not
    before, so a postponement that strips a gameweek's rows down to zero
    available rows correctly does not count that gameweek toward the
    minimum. Returns ``None`` (never a fabricated or pooled-across-all-
    gameweeks fit) when ineligible or when the eligible rows have
    degenerate (constant) ``predicted_start_probability`` and cannot
    support a slope.
    """
    eligible_rows: list[AppearancePolicyCalibrationRow] = []
    eligible_gameweeks: set[int] = set()
    for row_gameweek, rows in calibration_rows_by_gameweek.items():
        if row_gameweek >= gameweek:
            continue
        for row in rows:
            if row.outcome_available_at <= target_deadline:
                eligible_rows.append(row)
                eligible_gameweeks.add(row_gameweek)

    if len(eligible_gameweeks) < minimum_calibration_gameweeks:
        return None
    try:
        return fit_ols(
            [row.predicted_start_probability for row in eligible_rows],
            [1.0 if row.actual_started else 0.0 for row in eligible_rows],
            training_gameweeks=len(eligible_gameweeks),
        )
    except ValueError:
        # Fewer than 2 eligible rows, or degenerate (constant)
        # predicted_start_probability -- treated the same as "not yet
        # eligible": omitted, not fabricated or raised.
        return None


@dataclass(frozen=True, slots=True)
class AppearancePolicyBacktestResult:
    """One policy's full set of out-of-sample scored observations, plus fit trajectory."""

    policy: PolicyName
    observations: tuple[BacktestObservation, ...]
    fit_trajectory: tuple[tuple[int, OlsFit | None], ...]  # (gameweek, fit-used-that-step)


@dataclass(frozen=True, slots=True)
class AppearancePolicyBacktestGap:
    gameweek: int
    fixture_id: int | None
    player_code: int
    team: str
    position: str
    flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AppearancePolicyBacktestBundle:
    """All three policies' results from ONE shared walk-forward pass over the same data."""

    results_by_policy: dict[PolicyName, AppearancePolicyBacktestResult]
    gaps: tuple[AppearancePolicyBacktestGap, ...]
    evaluated_gameweeks: tuple[int, ...]
    candidate_player_fixture_rows: int
    minimum_calibration_gameweeks: int


def materialize_appearance_policy_backtest(
    *,
    season: str,
    import_run_id: str,
    connection: duckdb.DuckDBPyConnection,
    gameweeks_frame: pd.DataFrame,
    players_raw_frame: pd.DataFrame,
    evaluation_from_gw: int = 3,
    evaluation_to_gw: int = 38,
    deadline_buffer: timedelta = timedelta(minutes=90),
    outcome_delay: timedelta = timedelta(hours=3),
    short_form_gameweeks: int = 6,
    defcon_short_form_gameweeks: int = 10,
    long_form_weight: float = 0.8,
    minimum_calibration_gameweeks: int = DEFAULT_MINIMUM_CALIBRATION_GAMEWEEKS,
) -> AppearancePolicyBacktestBundle:
    """Score every eligible player-fixture under all three appearance calibration policies.

    Duplicates the SHAPE of
    ``benchwarmers_backtest.materialize_benchwarmers_walk_forward_backtest``'s
    walk-forward loop (see module docstring for why), but computes each
    gameweek's upstream queries (team strength, player rates, appearance
    history, league-average bonus rates) ONCE, then scores all three
    policies from that same shared state -- never three independent passes
    over the data, and never a different set of candidate rows per policy
    (a row excluded as a gap under one policy is excluded identically under
    all three, since gap eligibility depends only on POLICY-INDEPENDENT
    inputs).

    The ``start_probability`` calibration fit used for gameweek ``G`` is
    refit at each step from strictly-prior gameweeks' realised
    ``actual_started`` outcomes (``fit_for_gameweek``) -- the SAME causal
    boundary already enforced by every other calibration in this codebase.
    A gameweek without an eligible fit (fewer than
    ``minimum_calibration_gameweeks`` strictly-prior gameweeks, or a
    degenerate prior predicted_start_probability) scores ``global``/
    ``high_end_shrinkage`` identically to ``raw`` for that gameweek's rows
    (recorded in ``fit_trajectory`` as ``(gameweek, None)``) -- never a
    fabricated calibration.
    """
    if not season.strip():
        raise ValueError("season must not be blank")
    if not 1 <= evaluation_from_gw <= evaluation_to_gw <= 38:
        raise ValueError("evaluation_from_gw must be <= evaluation_to_gw, both in 1..38")
    if minimum_calibration_gameweeks < 1:
        raise ValueError("minimum_calibration_gameweeks must be a positive integer")

    required_columns = {
        "code", "team", "fixture", "position", "GW", "was_home", "kickoff_time",
        "starts", "minutes", "total_points",
    }
    missing = required_columns - set(gameweeks_frame.columns)
    if missing:
        raise ValueError(f"gameweeks_frame is missing required columns: {sorted(missing)}")

    gameweeks_frame = gameweeks_frame.copy()
    gameweeks_frame["kickoff_time"] = pd.to_datetime(
        gameweeks_frame["kickoff_time"], utc=True, errors="coerce"
    )
    if gameweeks_frame["kickoff_time"].isna().any():
        raise ValueError("kickoff_time contains missing or invalid values")
    if gameweeks_frame["was_home"].dtype != bool:
        gameweeks_frame["was_home"] = (
            gameweeks_frame["was_home"].astype(str).str.strip().str.lower().map(
                {"true": True, "false": False}
            )
        )
        if gameweeks_frame["was_home"].isna().any():
            raise ValueError("was_home must be a boolean")

    gameweeks_frame = gameweeks_frame.drop_duplicates().copy()
    if gameweeks_frame.duplicated(["code", "fixture"], keep=False).any():
        raise ValueError("gameweeks_frame contains conflicting player-fixture duplicates")

    team_fixture_results = build_team_fixture_results(gameweeks_frame)
    team_name_to_id = build_team_name_to_id(players_raw_frame, gameweeks_frame)
    deadlines = infer_gameweek_deadlines(gameweeks_frame, deadline_buffer=deadline_buffer)

    observations_by_policy: dict[PolicyName, list[BacktestObservation]] = {
        policy: [] for policy in POLICIES
    }
    fit_trajectory_by_policy: dict[PolicyName, list[tuple[int, OlsFit | None]]] = {
        policy: [] for policy in POLICIES
    }
    calibration_rows: list[AppearancePolicyCalibrationRow] = []
    gaps: list[AppearancePolicyBacktestGap] = []
    candidate_rows = 0
    evaluated: list[int] = []

    for gameweek in range(evaluation_from_gw, evaluation_to_gw + 1):
        if gameweek not in deadlines:
            continue
        evaluated.append(gameweek)
        deadline = deadlines[gameweek].to_pydatetime()

        calibration_rows_by_gameweek = _calibration_rows_by_gameweek(calibration_rows)
        fit = fit_for_gameweek(
            calibration_rows_by_gameweek,
            gameweek=gameweek,
            target_deadline=deadline,
            minimum_calibration_gameweeks=minimum_calibration_gameweeks,
        )
        for policy in ("global", "high_end_shrinkage"):
            fit_trajectory_by_policy[policy].append((gameweek, fit))

        strengths = team_strength_as_of(
            team_fixture_results,
            as_of_gameweek=gameweek,
            short_form_gameweeks=short_form_gameweeks,
            long_form_weight=long_form_weight,
            target_deadline=deadline,
            outcome_delay=outcome_delay,
        )
        rates = player_rates_as_of(
            connection,
            import_run_id=import_run_id,
            as_of_gameweek=gameweek,
            short_form_gameweeks=short_form_gameweeks,
            defcon_short_form_gameweeks=defcon_short_form_gameweeks,
            target_deadline=deadline,
            outcome_delay=outcome_delay,
        )
        appearances = appearance_as_of(
            connection,
            import_run_id=import_run_id,
            as_of_gameweek=gameweek,
            target_deadline=deadline,
            outcome_delay=outcome_delay,
        )
        try:
            avg_bps, avg_bonus, avg_bonus_per_bps = league_average_bonus_rates_as_of(
                connection,
                import_run_id=import_run_id,
                as_of_gameweek=gameweek,
                target_deadline=deadline,
                outcome_delay=outcome_delay,
            )
        except ValueError:
            avg_bps = avg_bonus = avg_bonus_per_bps = None

        gw_rows = gameweeks_frame.loc[gameweeks_frame["GW"] == gameweek]
        fixtures_per_player = gw_rows.groupby("code")["fixture"].nunique()
        double_gameweek_players = set(fixtures_per_player.loc[fixtures_per_player > 1].index)

        for row in gw_rows.itertuples(index=False):
            player_code = int(row.code)
            team = str(row.team)
            fixture_id = int(row.fixture)
            position = str(row.position)
            candidate_rows += 1
            flags: set[str] = set()

            if player_code in double_gameweek_players:
                flags.add(DOUBLE_GAMEWEEK_FIXTURE)
                gaps.append(
                    AppearancePolicyBacktestGap(
                        gameweek, fixture_id, player_code, team, position, tuple(sorted(flags))
                    )
                )
                continue

            appearance_entry = appearances.get(player_code)
            rate_entry = rates.get(player_code)
            own_strength = strengths.get(team)

            opponent_row = team_fixture_results.loc[
                (team_fixture_results["fixture_id"] == fixture_id)
                & (team_fixture_results["team"] != team)
            ]
            opponent_strength = None
            opponent_team_id = team_name_to_id.get(team)
            if not opponent_row.empty:
                opponent_team_name = str(opponent_row.iloc[0]["team"])
                opponent_strength = strengths.get(opponent_team_name)
                opponent_team_id = team_name_to_id.get(opponent_team_name)

            own_team_id = team_name_to_id.get(team)

            if appearance_entry is None:
                flags.add(MISSING_APPEARANCE_HISTORY)
            if rate_entry is None or not has_usable_rate_history(rate_entry):
                flags.add(NO_USABLE_PLAYER_RATE_HISTORY)
            if own_strength is None or opponent_strength is None:
                flags.add(MISSING_TEAM_STRENGTH)
            if own_team_id is None or opponent_team_id is None:
                flags.add(MISSING_TEAM_ID)
            if avg_bps is None:
                flags.add(NO_LEAGUE_AVERAGE_BONUS_RATES)

            # Calibration training rows are appended for EVERY gameweek that
            # has a causal appearance prediction, regardless of whether this
            # row is later excluded from xPts scoring as a gap -- exactly
            # mirroring appearance_calibration.py's own appearance_eligible
            # cohort gating (never conditioned on the unrelated xPts
            # requirements), so a later gameweek's calibration fit is not
            # biased toward players/fixtures with complete xPts inputs.
            if appearance_entry is not None:
                calibration_rows.append(
                    AppearancePolicyCalibrationRow(
                        gameweek=gameweek,
                        predicted_start_probability=appearance_entry.appearance.start_probability,
                        actual_started=bool(row.starts),
                        outcome_available_at=row.kickoff_time.to_pydatetime() + outcome_delay,
                    )
                )

            if flags:
                gaps.append(
                    AppearancePolicyBacktestGap(
                        gameweek, fixture_id, player_code, team, position, tuple(sorted(flags))
                    )
                )
                continue

            is_home = bool(row.was_home)
            fixture = FixtureContext(
                fixture_id=fixture_id,
                gameweek=gameweek,
                slot=1,
                team_id=own_team_id,
                opponent_id=opponent_team_id,
                is_home=is_home,
                kickoff=row.kickoff_time.to_pydatetime(),
            )

            raw_appearance = appearance_entry.appearance
            rate = rate_entry
            minutes_per_start = appearance_entry.mean_minutes_per_start
            minutes_fraction = minutes_per_start / REGULATION_MINUTES
            cameo_ratio = appearance_entry.mean_minutes_per_substitute / minutes_per_start
            saves_per_90 = (
                rate.season_saves / rate.season_minutes * REGULATION_MINUTES
                if rate.season_minutes > 0
                else 0.0
            )
            yellow_rate = (
                rate.season_yellow_cards / rate.season_minutes * REGULATION_MINUTES * minutes_fraction
                if rate.season_minutes > 0
                else 0.0
            )
            red_rate = (
                rate.season_red_cards / rate.season_minutes * REGULATION_MINUTES * minutes_fraction
                if rate.season_minutes > 0
                else 0.0
            )
            bonus_per_start = (
                rate.season_bonus / rate.season_starts if rate.season_starts > 0 else avg_bonus
            )
            bps_per_start = (
                rate.season_bps / rate.season_starts if rate.season_starts > 0 else avg_bps
            )
            if bps_per_start < 0.0 or bonus_per_start < 0.0:
                gaps.append(
                    AppearancePolicyBacktestGap(
                        gameweek, fixture_id, player_code, team, position, (NEGATIVE_BONUS_SIGNAL,)
                    )
                )
                continue
            long_defcon_lambda = (
                rate.long_form_defensive_contribution
                / rate.long_form_defcon_minutes
                * REGULATION_MINUTES
                * minutes_fraction
                if rate.long_form_defcon_minutes > 0
                else 0.0
            )
            short_defcon_lambda = (
                rate.short_form_defensive_contribution
                / rate.short_form_defcon_minutes
                * REGULATION_MINUTES
                * minutes_fraction
                if rate.short_form_defcon_minutes > 0
                else 0.0
            )

            # Policy-INDEPENDENT rate projections -- computed once per row,
            # reused for all three policies below. Only the `appearance`
            # argument threaded through weight_* changes per policy.
            attacking_rates = project_benchwarmers_attacking_rates(
                AttackingWindow(rate.long_form_minutes, rate.long_form_expected_goals, rate.long_form_expected_assists),
                AttackingWindow(rate.short_form_minutes, rate.short_form_expected_goals, rate.short_form_expected_assists),
                minutes_per_start_fraction=minutes_fraction,
                position=position,
                opponent_defensive_multiplier=opponent_strength.strength.opponent_defensive_weakness_ratio,
                long_form_weight=long_form_weight,
            )
            defensive_rates = project_benchwarmers_defensive_rates(
                corrected_team_xgc_per_match=own_strength.strength.opponent_xgc_per_match,
                opponent_xg_per_match=opponent_strength.strength.opponent_xg_per_match,
                league_average_xg_per_match=own_strength.strength.league_average_xg_per_match,
                position=position,
            )
            saves_rate = project_benchwarmers_saves(
                saves_per_90=saves_per_90,
                opponent_xg_per_match=opponent_strength.strength.opponent_xg_per_match,
                league_average_xg_per_match=own_strength.strength.league_average_xg_per_match,
                position=position,
            )
            discipline = project_discipline(
                yellow_card_rate_if_start=yellow_rate,
                red_card_rate_if_start=red_rate,
            )
            bonus_rate = project_benchwarmers_bonus(
                previous_starts=rate.season_starts,
                previous_bonus_per_start=bonus_per_start,
                previous_bps_per_start=bps_per_start,
                current_starts=0,
                current_bonus_per_start=avg_bonus,
                current_bps_per_start=avg_bps,
                league_average_bonus_per_bps=avg_bonus_per_bps,
                defensive_fixture_multiplier=opponent_strength.strength.defensive_bonus_multiplier,
                attacking_fixture_multiplier=opponent_strength.strength.opponent_defensive_weakness_ratio,
                position=position,
            )
            defcon_rate = project_benchwarmers_defcon(
                long_form_lambda_if_start=long_defcon_lambda,
                short_form_lambda_if_start=short_defcon_lambda,
                position=position,
            )

            kickoff = row.kickoff_time.to_pydatetime()
            actual_points = float(row.total_points)

            for policy in POLICIES:
                # A gameweek without an eligible prior-gameweeks-only fit
                # (fit is None) scores global/high_end_shrinkage identically
                # to raw for that gameweek's rows -- never a fabricated
                # calibration (see fit_for_gameweek's own docstring and this
                # module's docstring). apply_calibration_policy itself
                # requires a non-None fit for those two policies, so the
                # fallback is applied here, at the call site, not inside it.
                effective_policy = "raw" if (policy != "raw" and fit is None) else policy
                appearance = apply_calibration_policy(
                    raw_appearance,
                    policy=effective_policy,
                    fit=fit,
                    mean_minutes_per_start=minutes_per_start,
                    mean_minutes_per_substitute=appearance_entry.mean_minutes_per_substitute,
                )

                attacking = weight_attacking_rates(
                    attacking_rates, appearance, substitute_to_start_minutes_ratio=cameo_ratio
                )
                defensive = weight_defensive_rates(
                    defensive_rates, appearance,
                    substitute_to_start_minutes_ratio=cameo_ratio, position=position,
                )
                saves = weight_saves(
                    saves_rate, appearance, substitute_to_start_minutes_ratio=cameo_ratio
                )
                yellow_cards = weight_linear_component(
                    discipline.yellow_card_xpts_if_start, appearance,
                    substitute_to_start_minutes_ratio=cameo_ratio,
                )
                red_cards = weight_linear_component(
                    discipline.red_card_xpts_if_start, appearance,
                    substitute_to_start_minutes_ratio=cameo_ratio,
                )
                bonus = weight_linear_component(
                    bonus_rate.bounded_xpts_if_start, appearance,
                    substitute_to_start_minutes_ratio=cameo_ratio,
                )
                defcon = weight_defcon(
                    defcon_rate, appearance,
                    substitute_to_start_minutes_ratio=cameo_ratio, position=position,
                )
                baseline = compose_baseline_projection(
                    fixture,
                    BaselineComponentProjections(
                        appearance=appearance,
                        attacking=attacking,
                        defensive=defensive,
                        saves=saves,
                        yellow_cards=yellow_cards,
                        red_cards=red_cards,
                        bonus=bonus,
                        defcon=defcon,
                    ),
                )
                observations_by_policy[policy].append(
                    BacktestObservation(
                        season=season,
                        gameweek=gameweek,
                        deadline=deadline,
                        fixture_kickoff=kickoff,
                        feature_cutoff=deadline,
                        outcome_available_at=kickoff + outcome_delay,
                        player_id=player_code,
                        fixture_id=fixture_id,
                        predicted_xpts=baseline.total_xpts,
                        actual_points=actual_points,
                    )
                )

    results_by_policy = {
        policy: AppearancePolicyBacktestResult(
            policy=policy,
            observations=tuple(observations_by_policy[policy]),
            fit_trajectory=tuple(fit_trajectory_by_policy[policy]),
        )
        for policy in POLICIES
    }

    return AppearancePolicyBacktestBundle(
        results_by_policy=results_by_policy,
        gaps=tuple(gaps),
        evaluated_gameweeks=tuple(evaluated),
        candidate_player_fixture_rows=candidate_rows,
        minimum_calibration_gameweeks=minimum_calibration_gameweeks,
    )


@dataclass(frozen=True, slots=True)
class RawRowLevelParityResult:
    """Row-by-row comparison of this module's ``raw`` policy against the canonical materializer.

    The aggregate ``self_check`` (matching ``raw``'s pooled MAE/RMSE/mean_error
    against the committed reference JSON) is NOT sufficient evidence that this
    module's duplicated walk-forward loop is identical to
    ``benchwarmers_backtest.materialize_benchwarmers_walk_forward_backtest``:
    aggregate metrics can match by coincidence (e.g. two different sets of
    per-row errors with the same mean and the same sum-of-squares) even when
    individual rows disagree. This performs the row-level check the aggregate
    self-check cannot: EVERY field of every canonical
    ``BacktestObservation``/``BacktestGapSummary`` is compared, by exact
    ``(player_id, fixture_id, gameweek)`` key, against this module's own
    ``raw`` policy output over the SAME inputs.

    ``matches`` is ``True`` only when ALL of the following hold:

    - the two observation key sets are identical (no row present in one
      output but missing from the other), and NEITHER side has a duplicate
      key (checked BEFORE either side is converted to a dict -- a dict
      construction would otherwise silently collapse a duplicate key to
      whichever row happened to be built last, hiding a real materializer
      bug behind an apparent match);
    - every matched observation pair agrees EXACTLY on EVERY field
      ``BacktestObservation`` carries -- ``season``, ``actual_points``,
      ``predicted_xpts``, ``deadline``, ``fixture_kickoff``,
      ``feature_cutoff``, and ``outcome_available_at`` (``season`` is
      caller-supplied, but a caller bug passing mismatched season strings to
      the two materializers is exactly the kind of divergence this check
      exists to catch, so it is compared like any other field, not
      exempted);
    - the two gap key sets are identical, neither side has a duplicate gap
      key, and every matched gap pair agrees on EVERY field
      ``BacktestGapSummary``/``AppearancePolicyBacktestGap`` carries --
      ``team``, ``position``, and ``flags`` (not flags alone: two gaps with
      the same flags but different position, e.g. from a positions-frame
      bug, would otherwise pass undetected);
    - ``candidate_player_fixture_rows`` and ``evaluated_gameweeks`` agree
      exactly.

    ``mismatches`` lists every disagreement found (never truncated silently
    -- see ``materialize_appearance_policy_backtest``'s own caller, which
    raises before writing any output when ``matches`` is ``False``, so this
    field's content is what a human would need to diagnose the divergence).
    """

    matches: bool
    canonical_rows: int
    policy_backtest_rows: int
    mismatches: tuple[str, ...]


def _duplicate_keys(keys: Sequence[tuple]) -> tuple[tuple, ...]:
    """Return every key in ``keys`` that occurs more than once, each listed once."""
    seen: set[tuple] = set()
    duplicates: set[tuple] = set()
    for key in keys:
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return tuple(sorted(duplicates))


def verify_raw_row_level_parity(
    canonical: BenchwarmersBacktestResult,
    policy_backtest_raw_observations: Sequence[BacktestObservation],
    policy_backtest_gaps: Sequence[AppearancePolicyBacktestGap],
    *,
    candidate_player_fixture_rows: int,
    evaluated_gameweeks: Sequence[int],
) -> RawRowLevelParityResult:
    """Compare the canonical materializer's own result against this module's ``raw`` policy, row by row.

    Both ``canonical`` and the ``policy_backtest_*`` arguments must have been
    produced from the SAME inputs (season, import_run_id, gameweeks_frame,
    players_raw_frame, evaluation range, and every other keyword argument
    both materializers accept) -- this function only compares; it does not
    re-run either materializer itself.
    """
    mismatches: list[str] = []

    canonical_obs_keys = [(o.player_id, o.fixture_id, o.gameweek) for o in canonical.observations]
    policy_obs_keys = [
        (o.player_id, o.fixture_id, o.gameweek) for o in policy_backtest_raw_observations
    ]
    # Checked BEFORE building either dict below -- a dict comprehension over
    # a duplicate-containing sequence silently keeps only the last row for
    # that key, which would hide a real duplicate-row materializer bug
    # behind an apparent (but spurious) match.
    canonical_obs_duplicates = _duplicate_keys(canonical_obs_keys)
    policy_obs_duplicates = _duplicate_keys(policy_obs_keys)
    if canonical_obs_duplicates:
        mismatches.append(
            f"{len(canonical_obs_duplicates)} duplicate observation key(s) in canonical "
            f"(dict construction would silently collapse these), e.g. "
            f"{canonical_obs_duplicates[:3]!r}"
        )
    if policy_obs_duplicates:
        mismatches.append(
            f"{len(policy_obs_duplicates)} duplicate observation key(s) in the policy "
            f"backtest's raw observations (dict construction would silently collapse "
            f"these), e.g. {policy_obs_duplicates[:3]!r}"
        )

    canonical_by_key = {
        (o.player_id, o.fixture_id, o.gameweek): o for o in canonical.observations
    }
    policy_by_key = {
        (o.player_id, o.fixture_id, o.gameweek): o for o in policy_backtest_raw_observations
    }
    canonical_keys = set(canonical_by_key)
    policy_keys = set(policy_by_key)

    only_in_canonical = canonical_keys - policy_keys
    only_in_policy_backtest = policy_keys - canonical_keys
    if only_in_canonical:
        mismatches.append(
            f"{len(only_in_canonical)} key(s) present in canonical but missing from the "
            f"policy backtest's raw observations, e.g. {sorted(only_in_canonical)[:3]!r}"
        )
    if only_in_policy_backtest:
        mismatches.append(
            f"{len(only_in_policy_backtest)} key(s) present in the policy backtest's raw "
            f"observations but missing from canonical, e.g. {sorted(only_in_policy_backtest)[:3]!r}"
        )

    # Every field BacktestObservation carries -- including season, compared
    # like any other field rather than exempted as "caller-supplied", since
    # a caller passing mismatched season strings to the two materializers is
    # exactly the kind of divergence this check exists to catch.
    observation_fields = (
        "season",
        "actual_points",
        "predicted_xpts",
        "deadline",
        "fixture_kickoff",
        "feature_cutoff",
        "outcome_available_at",
    )
    for key in sorted(canonical_keys & policy_keys):
        canonical_observation = canonical_by_key[key]
        policy_observation = policy_by_key[key]
        for field in observation_fields:
            canonical_value = getattr(canonical_observation, field)
            policy_value = getattr(policy_observation, field)
            if canonical_value != policy_value:
                mismatches.append(
                    f"observation {key!r}.{field}: canonical={canonical_value!r}, "
                    f"policy_backtest={policy_value!r}"
                )

    canonical_gap_keys_list = [(g.player_code, g.fixture_id, g.gameweek) for g in canonical.gaps]
    policy_gap_keys_list = [
        (g.player_code, g.fixture_id, g.gameweek) for g in policy_backtest_gaps
    ]
    canonical_gap_duplicates = _duplicate_keys(canonical_gap_keys_list)
    policy_gap_duplicates = _duplicate_keys(policy_gap_keys_list)
    if canonical_gap_duplicates:
        mismatches.append(
            f"{len(canonical_gap_duplicates)} duplicate gap key(s) in canonical (dict "
            f"construction would silently collapse these), e.g. {canonical_gap_duplicates[:3]!r}"
        )
    if policy_gap_duplicates:
        mismatches.append(
            f"{len(policy_gap_duplicates)} duplicate gap key(s) in the policy backtest "
            f"(dict construction would silently collapse these), e.g. "
            f"{policy_gap_duplicates[:3]!r}"
        )

    canonical_gaps_by_key = {
        (g.player_code, g.fixture_id, g.gameweek): g for g in canonical.gaps
    }
    policy_gaps_by_key = {
        (g.player_code, g.fixture_id, g.gameweek): g for g in policy_backtest_gaps
    }
    canonical_gap_keys = set(canonical_gaps_by_key)
    policy_gap_keys = set(policy_gaps_by_key)
    only_canonical_gaps = canonical_gap_keys - policy_gap_keys
    only_policy_gaps = policy_gap_keys - canonical_gap_keys
    if only_canonical_gaps:
        mismatches.append(
            f"{len(only_canonical_gaps)} gap key(s) present in canonical but missing from "
            f"the policy backtest, e.g. {sorted(only_canonical_gaps)[:3]!r}"
        )
    if only_policy_gaps:
        mismatches.append(
            f"{len(only_policy_gaps)} gap key(s) present in the policy backtest but missing "
            f"from canonical, e.g. {sorted(only_policy_gaps)[:3]!r}"
        )

    # Every field both BacktestGapSummary and AppearancePolicyBacktestGap
    # carry besides the key itself -- team and position, not just flags: two
    # gaps sharing the same flags but disagreeing on position (e.g. from a
    # positions-frame bug) would otherwise pass this check undetected.
    gap_fields = ("team", "position", "flags")
    for key in sorted(canonical_gap_keys & policy_gap_keys):
        canonical_gap = canonical_gaps_by_key[key]
        policy_gap = policy_gaps_by_key[key]
        for field in gap_fields:
            canonical_value = getattr(canonical_gap, field)
            policy_value = getattr(policy_gap, field)
            if canonical_value != policy_value:
                mismatches.append(
                    f"gap {key!r}.{field}: canonical={canonical_value!r}, "
                    f"policy_backtest={policy_value!r}"
                )

    if canonical.candidate_player_fixture_rows != candidate_player_fixture_rows:
        mismatches.append(
            "candidate_player_fixture_rows: canonical="
            f"{canonical.candidate_player_fixture_rows!r}, "
            f"policy_backtest={candidate_player_fixture_rows!r}"
        )
    if tuple(canonical.evaluated_gameweeks) != tuple(evaluated_gameweeks):
        mismatches.append(
            f"evaluated_gameweeks: canonical={canonical.evaluated_gameweeks!r}, "
            f"policy_backtest={tuple(evaluated_gameweeks)!r}"
        )

    return RawRowLevelParityResult(
        matches=not mismatches,
        canonical_rows=len(canonical.observations),
        policy_backtest_rows=len(policy_backtest_raw_observations),
        mismatches=tuple(mismatches),
    )
