"""Segment the appearance model's own walk-forward calibration to locate bias.

``validation.appearance_calibration`` measures the appearance model's OVERALL
walk-forward calibration (one pooled slope per target, per cohort). It found
both ``start_probability`` and ``expected_minutes`` pooled slopes below 1.0 in
the primary ``appearance_eligible`` cohort. That finding alone cannot say
*where* the miscalibration concentrates -- whether it is spread evenly or
driven by a specific band of predictions, a specific position, or a specific
part of the season. This module answers that "where" question with plain
per-segment descriptive statistics (mean bias, Brier/MSE, MAE, gameweek-
cluster bootstrap CI), not a second regression-based calibration slope.

Deliberately NOT a per-segment OLS slope: several required segments (e.g. the
[.8, 1.0] start_probability band, or a thin gameweek-phase x position cell)
can have very low predictor variance -- OLS's ``slope = Sxy / Sxx`` becomes
numerically unstable (large variance) or degenerate (``Sxx == 0``, undefined)
exactly in the narrow bands most interesting to this diagnostic. Reporting an
unstable slope as "the calibration" for a narrow band would be misleading, so
this module reports mean bias (a stable, always-defined statistic) plus an
explicit ``insufficient_variation`` flag instead of ever fitting a per-segment
slope.

Four required cohorts (mirroring ``appearance_calibration``'s two, plus two
new ones):

- ``appearance_eligible`` (primary): every non-DGW player-fixture with a
  causal appearance prediction and a realised starts/minutes label -- exactly
  ``appearance_calibration``'s own primary cohort.
- ``xpts_scored_aligned`` (sensitivity): the subset that also produced a
  ``BacktestObservation`` -- exactly ``appearance_calibration``'s own
  sensitivity cohort, via the same ``xpts_scored_aligned_observations``. This
  spans every evaluated gameweek (GW3-38 by default) and remains in the
  general segment tables, but must NOT be used as the comparator for a
  high-band claim -- see ``xpts_same_window_aligned`` below.
- ``xpts_high_band_aligned`` (sensitivity, new): within ``xpts_scored_aligned``,
  the further subset whose ``(player_id, fixture_id, gameweek)`` key was a
  member of ``validation.walk_forward_calibration``'s own OUT-OF-SAMPLE
  high-predicted-xPts band at that gameweek -- i.e. exactly the rows the
  committed xPts calibration script itself scored as "high band". This
  module calls ``walk_forward_calibration`` directly (not a re-derived
  percentile) specifically so this membership rule can never silently drift
  from the committed one: a prior-only 75th-percentile-of-predicted_xpts
  threshold recomputed at each gameweek from strictly-prior gameweeks only,
  exactly as that module documents. This is the one departure from this
  codebase's usual per-module OLS/percentile duplication convention, made
  deliberately because the task requires bit-for-bit membership agreement
  with an already-committed result, not merely an equivalent methodology.
  ``xpts_high_band_aligned`` only exists for gameweeks with at least
  ``minimum_calibration_gameweeks`` strictly-prior gameweeks (GW8-38 by
  default, not GW3-38) -- comparing it against the full GW3-38
  ``xpts_scored_aligned`` cohort would conflate "the high band has more bias"
  with "the high band's window excludes several cold-start gameweeks the
  full cohort includes", two different claims.
- ``xpts_same_window_aligned`` (sensitivity, new -- the correct high-band
  comparator): within ``xpts_scored_aligned``, restricted to keys that were
  ANY eligible gameweek's ``overall_evaluation_rows`` in the same
  ``walk_forward_calibration`` call ``xpts_high_band_aligned`` was derived
  from -- i.e. exactly the same eligible-gameweek window, read directly from
  those records rather than inferred from ``xpts_high_band_aligned``'s own
  min/max gameweek (an eligible gameweek can contribute zero high-band rows,
  e.g. a thin prior high band, which would make a min/max-derived window
  silently wrong). ``xpts_high_band_aligned`` is always a subset of
  ``xpts_same_window_aligned``, which is always a subset of
  ``xpts_scored_aligned``.

Four required segment axes, all with FIXED boundaries (population-adaptive
quartile bands, as ``diagnose_backtest_segments.py`` uses for its own
unrelated xPts-band segmentation, are deliberately not used here -- fixed
bands make a band's meaning identical across cohorts/reruns/seasons, which is
what "is average overprediction concentrated among high start-probability
players" needs to be interpretable at all):

- ``start_probability`` band: [0,.2), [.2,.4), [.4,.6), [.6,.8), [.8,1] --
  five bands.
- ``expected_minutes`` band: [0,15), [15,30), [30,45), [45,60), [60,75),
  [75,90] -- six bands.
- ``position`` (GK/DEF/MID/FWD), read directly from ``AppearanceObservation``.
- ``gameweek_phase``: three fixed canonical season thirds, a new diagnostic
  convention documented here (NOT the preseason rate materialisation's
  GW29/GW33 short-form windows this module originally reused -- those are
  previous-season rate-history windows for an unrelated purpose, not a
  meaningful current-season phase boundary for this diagnostic, and produced
  a lopsided 26/4/6-gameweek split that made "stable across phases" too
  strong a claim from a 4-gameweek middle cluster):
    - "early": GW1-13
    - "mid": GW14-26
    - "late": GW27-38
  For the default GW3-38 evaluation range this yields 11/13/12 OBSERVED
  gameweeks per phase (GW3-13, GW14-26, GW27-38) -- still uneven, but far
  less so than the prior scheme, and every phase answer reports both row
  count and distinct gameweek-cluster count so a thin-cluster phase is
  never silently read as equally well-supported as a thick one (see
  ``MINIMUM_GAMEWEEK_CLUSTERS_FOR_STABLE_LABEL``).

Every segment table's row counts sum back exactly to its parent cohort's
total row count (``validate_segment_partition``) -- each axis is a strict
partition of its cohort, by construction (every row falls in exactly one
band/position/phase).

Gameweek-cluster bootstrap CIs reuse
``validation.paired_uncertainty.block_bootstrap_statistic``'s cluster-label
resampling scheme (``numpy.random.default_rng(seed).integers`` over the same
sorted gameweek labels, same percentile-CI formula), but
``mean_bias_bootstrap_from_gameweek_stats`` recomputes the per-resample
statistic from precomputed per-gameweek sufficient statistics (count, sum of
predicted-actual) rather than re-concatenating and re-scanning every row in
every resample: since mean bias is linear in rows, ``mean(concat of drawn
gameweeks) == sum(drawn sums) / sum(drawn counts)`` mathematically, so this
is a runtime optimisation only, not a methodology change -- it reproduces
``block_bootstrap_statistic``'s point estimate/CI to floating-point
summation-order noise (~1e-15 relative, from vectorised numpy array
summation vs. Python-level row-by-row summation, not a different
calculation; verified by a dedicated equivalence test using
``pytest.approx``), never a materially different numeric result.

A per-cohort/segment CI (``mean_bias_bootstrap_from_gameweek_stats``) shows
whether THAT side's own bias is reliably nonzero -- it does NOT show whether
a "concentration" claim comparing two sides (a top band vs the overall
cohort; the xPts high band vs its same-window comparator) is itself
reliable: both sides can have individually significant, same-signed bias
while their DIFFERENCE'S own sampling uncertainty still crosses zero.
``paired_contrast_bootstrap`` answers that directly with a PAIRED
gameweek-cluster bootstrap of the contrast itself (``focus_bias -
comparator_bias``, one shared cluster-label draw applied to both sides per
replicate, never two independently-bootstrapped CIs subtracted) -- this is
the only statistic this module treats as licensing a "concentrated"/"larger"
claim; a focus side's own CI excluding zero is necessary but not sufficient.

Read-only measurement. Does not change any xPts component, appearance
formula, or production projection/optimizer path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

from fpl_model.validation.appearance_calibration import AppearanceCalibrationTarget
from fpl_model.validation.backtest import BacktestObservation
from fpl_model.validation.benchwarmers_backtest import AppearanceObservation
from fpl_model.validation.paired_uncertainty import DEFAULT_RESAMPLES, DEFAULT_SEED
from fpl_model.validation.walk_forward_calibration import (
    DEFAULT_HIGH_BAND_PERCENTILE,
    walk_forward_calibration,
)
from fpl_model.validation.walk_forward_calibration import (
    DEFAULT_MINIMUM_CALIBRATION_GAMEWEEKS as DEFAULT_XPTS_MINIMUM_CALIBRATION_GAMEWEEKS,
)

CohortName = Literal[
    "appearance_eligible",
    "xpts_scored_aligned",
    "xpts_high_band_aligned",
    "xpts_same_window_aligned",
]
GameweekPhase = Literal["early", "mid", "late"]

COHORTS: tuple[CohortName, ...] = (
    "appearance_eligible",
    "xpts_scored_aligned",
    "xpts_high_band_aligned",
    "xpts_same_window_aligned",
)

# Fixed, non-adaptive band edges -- see the module docstring for why fixed
# (not population-quantile) bands are required here.
START_PROBABILITY_BAND_EDGES: tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
START_PROBABILITY_BAND_LABELS: tuple[str, ...] = (
    "[0.0,0.2)",
    "[0.2,0.4)",
    "[0.4,0.6)",
    "[0.6,0.8)",
    "[0.8,1.0]",
)

EXPECTED_MINUTES_BAND_EDGES: tuple[float, ...] = (0.0, 15.0, 30.0, 45.0, 60.0, 75.0, 90.0)
EXPECTED_MINUTES_BAND_LABELS: tuple[str, ...] = (
    "[0,15)",
    "[15,30)",
    "[30,45)",
    "[45,60)",
    "[60,75)",
    "[75,90]",
)

# Gameweek-phase boundaries -- fixed canonical season thirds, a new
# diagnostic convention documented in this module's own docstring (NOT
# docs/DATA_MODEL.md's short-form rate windows, which this module
# originally reused but which are a previous-season rate-history window for
# an unrelated purpose, not a meaningful current-season phase boundary).
GAMEWEEK_PHASE_MID_START_GW = 14  # season third 2 of 3 (GW14-26)
GAMEWEEK_PHASE_LATE_START_GW = 27  # season third 3 of 3 (GW27-38)

MINIMUM_SEGMENT_ROWS_FOR_VARIATION = 2

# A percentile bootstrap CI computed from very few gameweek clusters can
# exclude zero by chance alone -- resampling a handful of clusters with
# replacement has limited ability to produce a resample whose pooled
# statistic crosses zero, even when the true underlying effect is not
# reliably nonzero. "stable"/"excludes zero" wording is therefore withheld
# (reported as an explicit caveat instead) whenever a segment's own
# distinct_gameweeks is below this threshold, regardless of what the CI
# itself says. Matches this module's DEFAULT_MINIMUM_CALIBRATION_GAMEWEEKS-
# adjacent convention of a small, explicit, documented minimum rather than a
# statistical rule of thumb with no precedent in this codebase.
MINIMUM_GAMEWEEK_CLUSTERS_FOR_STABLE_LABEL = 5


# expected_minutes/start_probability are themselves sums/products of
# upstream probabilities and per-start/per-substitute minutes (see
# model/appearance.py's blend_conditional_appearance), so a value that is
# logically exactly at the valid range's edge (e.g. a nailed-on starter's
# expected_minutes at exactly 90) can land a few ULPs outside it after
# floating-point summation (observed in the real 2025-26 backtest: e.g.
# 90.00000000000001). This tolerance absorbs that summation noise without
# silently accepting a genuinely out-of-range value (a real data/formula
# bug, which would be off by far more than machine epsilon).
_RANGE_TOLERANCE = 1e-9


def start_probability_band(value: float) -> str:
    """Return the fixed ``[low, high)`` band label containing ``value``.

    ``value`` must lie in ``[0.0, 1.0]`` (start_probability's valid range,
    within ``_RANGE_TOLERANCE`` for floating-point summation noise at the
    edges); the top edge is closed (``[.8, 1.0]``) so a value of exactly 1.0
    is assigned, not left out of every band.
    """
    return _fixed_band(
        value,
        edges=START_PROBABILITY_BAND_EDGES,
        labels=START_PROBABILITY_BAND_LABELS,
        value_name="start_probability",
    )


def expected_minutes_band(value: float) -> str:
    """Return the fixed ``[low, high)`` band label containing ``value``.

    ``value`` must lie in ``[0.0, 90.0]`` (expected_minutes' valid range,
    within ``_RANGE_TOLERANCE`` for floating-point summation noise at the
    edges); the top edge is closed (``[75, 90]``) so a value of exactly 90.0
    is assigned, not left out of every band.
    """
    return _fixed_band(
        value,
        edges=EXPECTED_MINUTES_BAND_EDGES,
        labels=EXPECTED_MINUTES_BAND_LABELS,
        value_name="expected_minutes",
    )


def _fixed_band(
    value: float, *, edges: tuple[float, ...], labels: tuple[str, ...], value_name: str
) -> str:
    low, high = edges[0], edges[-1]
    if not low - _RANGE_TOLERANCE <= value <= high + _RANGE_TOLERANCE:
        raise ValueError(f"{value_name}={value!r} is outside the valid range [{low}, {high}]")
    # Clamp only for band assignment, never for the value reported elsewhere
    # (predicted_value on the row stays exactly what was passed in) -- this
    # keeps a same-side-of-the-edge value in its logically-correct band
    # instead of raising or silently matching no band.
    clamped = min(max(value, low), high)
    for index, label in enumerate(labels):
        band_low, band_high = edges[index], edges[index + 1]
        is_last = index == len(labels) - 1
        if band_low <= clamped < band_high or (is_last and clamped == band_high):
            return label
    raise AssertionError(f"unreachable: {value_name}={value!r} matched no band")  # pragma: no cover


def gameweek_phase(gameweek: int) -> GameweekPhase:
    """Return the fixed canonical season-third containing ``gameweek``.

    Boundaries are documented, fixed gameweek numbers -- GW1-13 "early",
    GW14-26 "mid", GW27-38 "late" (see module docstring for why these
    specific numbers, not the preseason short-form rate windows this module
    previously reused) -- not derived from the evaluated range or any
    population statistic -- a given gameweek always maps to the same phase
    regardless of ``evaluation_from_gw``/``evaluation_to_gw`` or which
    cohort is being segmented.
    """
    if gameweek < 1 or gameweek > 38:
        raise ValueError(f"gameweek={gameweek!r} is outside the valid range [1, 38]")
    if gameweek >= GAMEWEEK_PHASE_LATE_START_GW:
        return "late"
    if gameweek >= GAMEWEEK_PHASE_MID_START_GW:
        return "mid"
    return "early"


@dataclass(frozen=True, slots=True)
class AppearanceSegmentRow:
    """One player-fixture's appearance prediction/outcome pair, enriched for segmentation.

    Distinct from ``appearance_calibration.AppearanceCalibrationRow``: that
    type is deliberately minimal (no ``position``) since OLS calibration
    doesn't need it. This module segments by position, so it is carried here.
    Shares the ``.gameweek`` attribute
    ``validation.paired_uncertainty.block_bootstrap_statistic`` requires.
    """

    gameweek: int
    player_id: int
    fixture_id: int
    position: str
    predicted_value: float
    actual_value: float
    start_probability_band: str
    expected_minutes_band: str
    gameweek_phase: GameweekPhase


def observations_to_segment_rows(
    observations: Sequence[AppearanceObservation], *, target: AppearanceCalibrationTarget
) -> tuple[AppearanceSegmentRow, ...]:
    if target not in ("start_probability", "expected_minutes"):
        raise ValueError(
            f'target must be "start_probability" or "expected_minutes", got {target!r}'
        )
    rows = []
    for observation in observations:
        sp_band = start_probability_band(observation.predicted_start_probability)
        em_band = expected_minutes_band(observation.predicted_expected_minutes)
        phase = gameweek_phase(observation.gameweek)
        if target == "start_probability":
            predicted = observation.predicted_start_probability
            actual = 1.0 if observation.actual_started else 0.0
        else:
            predicted = observation.predicted_expected_minutes
            actual = observation.actual_minutes
        rows.append(
            AppearanceSegmentRow(
                gameweek=observation.gameweek,
                player_id=observation.player_code,
                fixture_id=observation.fixture_id,
                position=observation.position,
                predicted_value=predicted,
                actual_value=actual,
                start_probability_band=sp_band,
                expected_minutes_band=em_band,
                gameweek_phase=phase,
            )
        )
    return tuple(rows)


def xpts_high_band_and_same_window_keys_from_backtest_observations(
    backtest_observations: Sequence[BacktestObservation],
    *,
    minimum_calibration_gameweeks: int = DEFAULT_XPTS_MINIMUM_CALIBRATION_GAMEWEEKS,
    high_band_percentile: float = DEFAULT_HIGH_BAND_PERCENTILE,
) -> tuple[
    frozenset[tuple[int | str, int | str, int]], frozenset[tuple[int | str, int | str, int]]
]:
    """Return ``(high_band_keys, same_window_keys)`` from ONE ``walk_forward_calibration`` call.

    Calls ``validation.walk_forward_calibration.walk_forward_calibration``
    directly (see module docstring for why this is a deliberate exception to
    the duplicate-not-import convention) exactly once, and derives BOTH key
    sets from the same ``records`` so they are guaranteed to share the same
    eligible-gameweek window by construction, never two separately-computed
    windows that could silently drift apart:

    - ``high_band_keys``: every eligible gameweek's own
      ``high_band_evaluation_rows`` -- the OUT-OF-SAMPLE rows whose
      ``predicted_xpts`` was at or above that gameweek's prior-only 75th
      percentile threshold. A gameweek with no ``high_band_fit`` (too little
      or degenerate prior high-band data) contributes no keys for that
      gameweek, exactly matching that module's own handling.
    - ``same_window_keys``: every eligible gameweek's own
      ``overall_evaluation_rows`` -- i.e. exactly the rows the xPts
      calibration script itself scored at ANY point in its walk-forward run,
      over the SAME set of eligible gameweeks ``high_band_keys`` was drawn
      from. This is deliberately NOT inferred from
      ``min(gameweek)``/``max(gameweek)`` of ``high_band_keys`` -- an
      eligible gameweek can legitimately contribute zero high-band rows
      (e.g. a thin or degenerate prior high band), which would make a
      min/max-derived window silently wrong; reading ``overall_evaluation_rows``
      directly is exact regardless of any such gap.
    """
    records = walk_forward_calibration(
        backtest_observations,
        minimum_calibration_gameweeks=minimum_calibration_gameweeks,
        high_band_percentile=high_band_percentile,
    )
    high_band_keys = frozenset(
        (row.player_id, row.fixture_id, row.gameweek)
        for record in records
        for row in record.high_band_evaluation_rows
    )
    same_window_keys = frozenset(
        (row.player_id, row.fixture_id, row.gameweek)
        for record in records
        for row in record.overall_evaluation_rows
    )
    return high_band_keys, same_window_keys


def xpts_high_band_aligned_observations(
    xpts_scored_aligned: Sequence[AppearanceObservation],
    high_band_keys: frozenset[tuple[int | str, int | str, int]],
) -> tuple[AppearanceObservation, ...]:
    """Restrict ``xpts_scored_aligned`` to keys in the committed xPts high band.

    Always a subset of ``xpts_scored_aligned`` (never larger), which is
    itself always a subset of ``appearance_eligible`` -- so this cohort is
    always the smallest of the three primary cohorts.
    """
    return tuple(
        observation
        for observation in xpts_scored_aligned
        if (observation.player_code, observation.fixture_id, observation.gameweek)
        in high_band_keys
    )


def xpts_same_window_aligned_observations(
    xpts_scored_aligned: Sequence[AppearanceObservation],
    same_window_keys: frozenset[tuple[int | str, int | str, int]],
) -> tuple[AppearanceObservation, ...]:
    """Restrict ``xpts_scored_aligned`` to keys in the xPts calibration's own eligible window.

    This is the correct comparator for ``xpts_high_band_aligned`` claims:
    both are derived from the SAME ``walk_forward_calibration`` records'
    eligible-gameweek window (see
    ``xpts_high_band_and_same_window_keys_from_backtest_observations``), so
    a "the high band's bias is larger than the comparator's" claim compares
    two cohorts drawn from identical gameweeks -- never the raw
    ``xpts_scored_aligned`` cohort, which spans every evaluated gameweek
    (GW3-38) including the cold-start gameweeks before the xPts
    calibration's own ``minimum_calibration_gameweeks`` warm-up completes,
    where the high band cannot exist at all.
    """
    return tuple(
        observation
        for observation in xpts_scored_aligned
        if (observation.player_code, observation.fixture_id, observation.gameweek)
        in same_window_keys
    )


@dataclass(frozen=True, slots=True)
class SegmentBootstrap:
    point_estimate: float
    ci_low: float
    ci_high: float
    resamples: int
    seed: int


def mean_bias_bootstrap_from_gameweek_stats(
    rows: Sequence[AppearanceSegmentRow],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> SegmentBootstrap:
    """Gameweek-cluster bootstrap CI for mean bias, via per-gameweek sufficient statistics.

    Mean bias is linear and additive across rows:
    ``mean(predicted - actual)`` over a pooled resample of whole gameweeks
    equals ``sum(each drawn gameweek's own (predicted-actual) sum) /
    sum(each drawn gameweek's own row count)``. This lets each resample draw
    be answered from ``rows``' per-gameweek ``(count, sum)`` pair instead of
    re-concatenating and re-scanning every row in every resample -- a runtime
    optimisation only: see ``test_appearance_segments.py``'s dedicated
    equivalence test asserting this reproduces (via ``pytest.approx``, not
    ``==``: vectorised numpy summation and Python-level row-by-row summation
    do not associate in the same order, so results agree to floating-point
    tolerance, not bit-for-bit) the same point_estimate/ci_low/ci_high as
    ``paired_uncertainty.block_bootstrap_statistic`` given the same rows,
    resamples, and seed.

    Uses the same percentile-bootstrap scheme (2.5th/97.5th percentile,
    ``numpy.random.default_rng(seed).integers`` cluster-label draws) as
    ``block_bootstrap_statistic`` -- deliberately not calling that function
    directly, since its per-draw ``statistic`` callback re-pools raw rows
    every call and is exactly the per-row rescan this optimisation avoids.
    """
    if not rows:
        raise ValueError("at least one row is required")
    if resamples < 1:
        raise ValueError("resamples must be a positive integer")

    counts: dict[int, int] = {}
    sums: dict[int, float] = {}
    for row in rows:
        counts[row.gameweek] = counts.get(row.gameweek, 0) + 1
        sums[row.gameweek] = sums.get(row.gameweek, 0.0) + (row.predicted_value - row.actual_value)

    cluster_labels = sorted(counts)
    cluster_count = len(cluster_labels)
    count_arr = np.asarray([counts[gw] for gw in cluster_labels], dtype=np.float64)
    sum_arr = np.asarray([sums[gw] for gw in cluster_labels], dtype=np.float64)

    total_count = float(count_arr.sum())
    total_sum = float(sum_arr.sum())
    point_estimate = total_sum / total_count

    rng = np.random.default_rng(seed)
    label_indices = rng.integers(0, cluster_count, size=(resamples, cluster_count))
    drawn_counts = count_arr[label_indices]  # (resamples, cluster_count)
    drawn_sums = sum_arr[label_indices]
    resample_counts = drawn_counts.sum(axis=1)
    resample_sums = drawn_sums.sum(axis=1)
    replicates = resample_sums / resample_counts

    ci_low, ci_high = np.percentile(replicates, [2.5, 97.5])
    return SegmentBootstrap(
        point_estimate=float(point_estimate),
        ci_low=float(ci_low),
        ci_high=float(ci_high),
        resamples=resamples,
        seed=seed,
    )


@dataclass(frozen=True, slots=True)
class ContrastBootstrap:
    """Paired gameweek-cluster bootstrap for ``focus_bias - comparator_bias``.

    Distinct from independently bootstrapping each side and subtracting the
    two CIs (which would overstate the contrast's true uncertainty by
    ignoring that both sides share the same gameweeks' causally-derived
    inputs -- the same reasoning ``paired_uncertainty.py`` already applies to
    the model-vs-naive comparator): every replicate draws ONE set of
    gameweek-cluster labels and applies it to BOTH sides, so the two sides'
    resampled statistics are correlated exactly as the true data are.
    """

    focus_bias: float
    comparator_bias: float
    contrast_point_estimate: float
    ci_low: float
    ci_high: float
    shared_distinct_gameweeks: int
    resamples: int
    seed: int


def _gameweek_sufficient_stats(
    rows: Sequence[AppearanceSegmentRow],
) -> tuple[dict[int, int], dict[int, float]]:
    """Return ``(counts, sums)`` of ``(predicted - actual)`` keyed by gameweek."""
    counts: dict[int, int] = {}
    sums: dict[int, float] = {}
    for row in rows:
        counts[row.gameweek] = counts.get(row.gameweek, 0) + 1
        sums[row.gameweek] = sums.get(row.gameweek, 0.0) + (row.predicted_value - row.actual_value)
    return counts, sums


def paired_contrast_bootstrap(
    focus_rows: Sequence[AppearanceSegmentRow],
    comparator_rows: Sequence[AppearanceSegmentRow],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> ContrastBootstrap:
    """Paired gameweek-cluster bootstrap CI for ``mean_bias(focus) - mean_bias(comparator)``.

    This directly answers the question a focus-side-only CI and a
    comparator-side-only CI, read side by side, cannot: is the CONTRAST
    itself reliably above zero, not merely "does the focus side's own CI
    exclude zero" (which says nothing about whether the comparator's bias is
    reliably smaller -- both could be individually significant and positive
    while their difference is not).

    Uses per-gameweek sufficient statistics (count, sum of predicted-actual)
    for each side separately -- the same technique
    ``mean_bias_bootstrap_from_gameweek_stats`` uses -- but draws ONE shared
    set of gameweek-cluster labels per replicate (from the UNION of gameweek
    labels present on either side) and applies it to both sides' own
    per-gameweek stats, so:

    - a replicate's pooled focus mean and pooled comparator mean are
      computed from the SAME drawn gameweeks, never two independently
      resampled label sets (never ``focus_ci - comparator_ci`` after two
      separate ``mean_bias_bootstrap_from_gameweek_stats`` calls -- that
      would ignore the correlation between the two sides induced by sharing
      gameweeks, understating how tight or wide the contrast's true CI is,
      exactly as ``paired_uncertainty.py``'s own model-vs-naive contrast
      already avoids doing);
    - each side keeps its OWN row counts and bias sums for whichever drawn
      gameweeks it has data on -- a gameweek where only one side has rows
      (e.g. the focus band has zero rows in some gameweek the comparator
      covers) contributes 0 count/0 sum to the side lacking data, not a
      dropped draw;
    - ``contrast = pooled_focus_mean - pooled_comparator_mean`` is computed
      once per replicate from those two pooled means.

    Raises ``ValueError`` (never silently emits ``NaN`` or treats it as
    zero) if the TRUE unresampled ``focus_rows`` has zero total count on the
    shared label set (a caller bug: an empty focus side should never reach
    this function -- ``summarize_segment`` already rejects empty ``rows``),
    or if any individual bootstrap REPLICATE draws a combination of shared
    labels under which the focus side's resampled row count is zero (only
    possible when the focus side's own gameweek coverage is a strict, thin
    subset of the shared label set -- e.g. a segment present in only 2 of 36
    possible gameweeks has a real, small chance of a resample drawing zero
    of those 2). This is reported as an explicit, deterministic error naming
    how many of the ``resamples`` replicates were affected, rather than
    producing a CI silently built from fewer valid replicates than requested
    or contaminated by a fabricated 0.0/NaN contrast value.
    """
    if not focus_rows:
        raise ValueError("focus_rows must not be empty")
    if not comparator_rows:
        raise ValueError("comparator_rows must not be empty")
    if resamples < 1:
        raise ValueError("resamples must be a positive integer")

    focus_counts, focus_sums = _gameweek_sufficient_stats(focus_rows)
    comparator_counts, comparator_sums = _gameweek_sufficient_stats(comparator_rows)

    shared_labels = sorted(set(focus_counts) | set(comparator_counts))
    shared_distinct_gameweeks = len(shared_labels)
    cluster_count = len(shared_labels)

    focus_count_arr = np.asarray([focus_counts.get(gw, 0) for gw in shared_labels], dtype=np.float64)
    focus_sum_arr = np.asarray([focus_sums.get(gw, 0.0) for gw in shared_labels], dtype=np.float64)
    comparator_count_arr = np.asarray(
        [comparator_counts.get(gw, 0) for gw in shared_labels], dtype=np.float64
    )
    comparator_sum_arr = np.asarray(
        [comparator_sums.get(gw, 0.0) for gw in shared_labels], dtype=np.float64
    )

    total_focus_count = float(focus_count_arr.sum())
    total_comparator_count = float(comparator_count_arr.sum())
    focus_bias = float(focus_sum_arr.sum() / total_focus_count)
    comparator_bias = float(comparator_sum_arr.sum() / total_comparator_count)
    contrast_point_estimate = focus_bias - comparator_bias

    rng = np.random.default_rng(seed)
    # ONE shared draw of cluster-label indices per replicate, applied to
    # BOTH sides -- this is the pairing itself, not two independent draws.
    label_indices = rng.integers(0, cluster_count, size=(resamples, cluster_count))

    drawn_focus_counts = focus_count_arr[label_indices]
    drawn_focus_sums = focus_sum_arr[label_indices]
    drawn_comparator_counts = comparator_count_arr[label_indices]
    drawn_comparator_sums = comparator_sum_arr[label_indices]

    resample_focus_counts = drawn_focus_counts.sum(axis=1)
    resample_comparator_counts = drawn_comparator_counts.sum(axis=1)

    zero_focus_replicates = int(np.sum(resample_focus_counts == 0.0))
    zero_comparator_replicates = int(np.sum(resample_comparator_counts == 0.0))
    if zero_focus_replicates or zero_comparator_replicates:
        raise ValueError(
            "paired_contrast_bootstrap: cannot resolve a pooled mean for every replicate -- "
            f"{zero_focus_replicates} of {resamples} replicate(s) drew zero focus-side rows "
            f"and {zero_comparator_replicates} of {resamples} drew zero comparator-side rows "
            "across the shared gameweek-cluster labels. This happens when one side's own "
            "gameweek coverage is a thin subset of the shared label set; increase resamples' "
            "underlying data coverage or treat this segment as too thin for a contrast CI "
            "rather than silently dropping/zeroing/NaN-ing the affected replicates."
        )

    resample_focus_sums = drawn_focus_sums.sum(axis=1)
    resample_comparator_sums = drawn_comparator_sums.sum(axis=1)
    resample_focus_means = resample_focus_sums / resample_focus_counts
    resample_comparator_means = resample_comparator_sums / resample_comparator_counts
    replicates = resample_focus_means - resample_comparator_means

    ci_low, ci_high = np.percentile(replicates, [2.5, 97.5])
    return ContrastBootstrap(
        focus_bias=focus_bias,
        comparator_bias=comparator_bias,
        contrast_point_estimate=float(contrast_point_estimate),
        ci_low=float(ci_low),
        ci_high=float(ci_high),
        shared_distinct_gameweeks=shared_distinct_gameweeks,
        resamples=resamples,
        seed=seed,
    )


@dataclass(frozen=True, slots=True)
class SegmentSummary:
    """Descriptive statistics for one cohort/target/segment combination.

    Never includes a fitted slope -- see module docstring for why. When the
    segment's own ``predicted_value`` has fewer than
    ``MINIMUM_SEGMENT_ROWS_FOR_VARIATION`` rows, or is constant across all
    its rows, ``insufficient_variation`` is ``True`` and
    ``predicted_variance`` is reported as exactly ``0.0`` (or the row count
    is simply too small to say anything); no slope-like statistic is ever
    substituted.

    ``bias_sum`` (``mean_bias * rows``, i.e. ``sum(predicted - actual)``) is
    reported alongside ``mean_bias`` specifically so a caller ranking
    segments by "contribution to the cohort's aggregate bias" can use a
    mathematically real, additive quantity: unlike ``mean_bias``
    (per-row-average, not additive across segments) or ``abs(mean_bias)``
    (throws away sign, so a segment that partially OFFSETS the aggregate
    bias would rank as "contributing" alongside one that adds to it),
    ``bias_sum`` values across a full partition sum exactly to the parent
    cohort's own ``bias_sum`` -- see ``share_of_net_aggregate_bias``, added
    by the caller once the parent cohort's total ``bias_sum`` is known (not
    computed here, since a single segment's own summary has no way to know
    its parent cohort's total).
    """

    rows: int
    distinct_gameweeks: int
    mean_predicted: float
    mean_actual: float
    mean_bias: float
    bias_sum: float
    mean_bias_bootstrap: SegmentBootstrap
    insufficient_variation: bool
    predicted_variance: float
    # start_probability only:
    brier_score: float | None
    observed_start_rate: float | None
    # expected_minutes only:
    mse: float | None
    mae: float | None


def summarize_segment(
    rows: Sequence[AppearanceSegmentRow],
    *,
    target: AppearanceCalibrationTarget,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> SegmentSummary:
    """Return descriptive statistics for ``rows`` (one segment's rows for one target).

    Raises ``ValueError`` if ``rows`` is empty -- callers should skip empty
    segments rather than call this, so an empty result is always a caller
    bug, never a silently fabricated all-null row.
    """
    if not rows:
        raise ValueError("at least one row is required")

    predicted_arr = np.asarray([r.predicted_value for r in rows], dtype=np.float64)
    actual_arr = np.asarray([r.actual_value for r in rows], dtype=np.float64)
    distinct_gameweeks = len({r.gameweek for r in rows})

    predicted_variance = float(np.var(predicted_arr))
    insufficient_variation = len(rows) < MINIMUM_SEGMENT_ROWS_FOR_VARIATION or predicted_variance == 0.0

    mean_bias_bootstrap = mean_bias_bootstrap_from_gameweek_stats(rows, resamples=resamples, seed=seed)

    brier_score: float | None = None
    observed_start_rate: float | None = None
    mse: float | None = None
    mae: float | None = None
    if target == "start_probability":
        brier_score = float(np.mean((predicted_arr - actual_arr) ** 2))
        observed_start_rate = float(np.mean(actual_arr))
    else:
        mse = float(np.mean((predicted_arr - actual_arr) ** 2))
        mae = float(np.mean(np.abs(predicted_arr - actual_arr)))

    bias_sum = float(np.sum(predicted_arr - actual_arr))
    return SegmentSummary(
        rows=len(rows),
        distinct_gameweeks=distinct_gameweeks,
        mean_predicted=float(np.mean(predicted_arr)),
        mean_actual=float(np.mean(actual_arr)),
        mean_bias=float(np.mean(predicted_arr - actual_arr)),
        bias_sum=bias_sum,
        mean_bias_bootstrap=mean_bias_bootstrap,
        insufficient_variation=insufficient_variation,
        predicted_variance=predicted_variance,
        brier_score=brier_score,
        observed_start_rate=observed_start_rate,
        mse=mse,
        mae=mae,
    )


def group_rows(
    rows: Sequence[AppearanceSegmentRow], *, by: str
) -> dict[str, tuple[AppearanceSegmentRow, ...]]:
    """Partition ``rows`` by the given attribute name (one of the segment axes).

    Every row belongs to exactly one group (bands/position/phase are total,
    non-overlapping functions of the row's own fields), so the returned
    groups' row counts always sum to ``len(rows)`` -- see
    ``validate_segment_partition``, which checks this invariant explicitly
    rather than assuming it holds.
    """
    groups: dict[str, list[AppearanceSegmentRow]] = {}
    for row in rows:
        key = str(getattr(row, by))
        groups.setdefault(key, []).append(row)
    return {key: tuple(group) for key, group in groups.items()}


def validate_segment_partition(
    rows: Sequence[AppearanceSegmentRow], groups: dict[str, tuple[AppearanceSegmentRow, ...]]
) -> None:
    """Raise ``ValueError`` unless ``groups``' row counts sum exactly to ``len(rows)``.

    A segmentation bug that drops or double-counts rows (e.g. an off-by-one
    band edge) would otherwise silently under/over-report a cohort's total --
    this is checked at build time, not left to a test to catch after the
    fact.
    """
    total = sum(len(group) for group in groups.values())
    if total != len(rows):
        raise ValueError(
            f"segment partition row-count mismatch: groups sum to {total}, "
            f"expected {len(rows)} (parent cohort row count)"
        )
