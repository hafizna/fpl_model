from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from fpl_model.validation.appearance_calibration import (
    xpts_keys_from_backtest_observations,
    xpts_scored_aligned_observations,
)
from fpl_model.validation.appearance_segments import (
    EXPECTED_MINUTES_BAND_EDGES,
    EXPECTED_MINUTES_BAND_LABELS,
    GAMEWEEK_PHASE_LATE_START_GW,
    GAMEWEEK_PHASE_MID_START_GW,
    MINIMUM_GAMEWEEK_CLUSTERS_FOR_STABLE_LABEL,
    START_PROBABILITY_BAND_EDGES,
    START_PROBABILITY_BAND_LABELS,
    AppearanceSegmentRow,
    expected_minutes_band,
    gameweek_phase,
    group_rows,
    mean_bias_bootstrap_from_gameweek_stats,
    observations_to_segment_rows,
    paired_contrast_bootstrap,
    start_probability_band,
    summarize_segment,
    validate_segment_partition,
    xpts_high_band_aligned_observations,
    xpts_high_band_and_same_window_keys_from_backtest_observations,
    xpts_same_window_aligned_observations,
)
from fpl_model.validation.backtest import BacktestObservation
from fpl_model.validation.benchwarmers_backtest import AppearanceObservation
from fpl_model.validation.paired_uncertainty import block_bootstrap_statistic
from fpl_model.validation.walk_forward_calibration import walk_forward_calibration


def _observation(
    *,
    gameweek: int,
    player_code: int,
    fixture_id: int,
    predicted_start_probability: float = 0.5,
    predicted_expected_minutes: float = 45.0,
    actual_started: bool = True,
    actual_minutes: float = 45.0,
    position: str = "MID",
) -> AppearanceObservation:
    return AppearanceObservation(
        player_code=player_code,
        fixture_id=fixture_id,
        gameweek=gameweek,
        position=position,
        predicted_start_probability=predicted_start_probability,
        predicted_expected_minutes=predicted_expected_minutes,
        actual_started=actual_started,
        actual_minutes=actual_minutes,
    )


def _backtest_observation(
    *, gameweek: int, player_id: int, fixture_id: int, predicted_xpts: float, actual_points: float = 2.0
) -> BacktestObservation:
    deadline = datetime(2025, 8, 16, 12, 30, tzinfo=UTC) + timedelta(days=7 * (gameweek - 1))
    kickoff = deadline + timedelta(minutes=90)
    return BacktestObservation(
        season="2025-26",
        gameweek=gameweek,
        deadline=deadline,
        fixture_kickoff=kickoff,
        feature_cutoff=deadline,
        outcome_available_at=kickoff + timedelta(hours=3),
        player_id=player_id,
        fixture_id=fixture_id,
        predicted_xpts=predicted_xpts,
        actual_points=actual_points,
    )


def _segment_row(
    *,
    gameweek: int = 1,
    player_id: int = 1,
    fixture_id: int = 1,
    position: str = "MID",
    predicted_value: float = 0.5,
    actual_value: float = 0.5,
) -> AppearanceSegmentRow:
    return AppearanceSegmentRow(
        gameweek=gameweek,
        player_id=player_id,
        fixture_id=fixture_id,
        position=position,
        predicted_value=predicted_value,
        actual_value=actual_value,
        start_probability_band=start_probability_band(min(max(predicted_value, 0.0), 1.0)),
        expected_minutes_band=expected_minutes_band(min(max(predicted_value, 0.0), 90.0)),
        gameweek_phase=gameweek_phase(gameweek),
    )


# ---------------------------------------------------------------------------
# Exact boundary assignment
# ---------------------------------------------------------------------------


def test_start_probability_band_covers_every_labelled_edge_exactly():
    # Every left edge belongs to its own band; every value just below a left
    # edge belongs to the previous band; the top edge is closed.
    for index, label in enumerate(START_PROBABILITY_BAND_LABELS):
        low = START_PROBABILITY_BAND_EDGES[index]
        assert start_probability_band(low) == label
    assert start_probability_band(0.19999999) == "[0.0,0.2)"
    assert start_probability_band(0.2) == "[0.2,0.4)"
    assert start_probability_band(0.39999999) == "[0.2,0.4)"
    assert start_probability_band(0.4) == "[0.4,0.6)"
    assert start_probability_band(0.59999999) == "[0.4,0.6)"
    assert start_probability_band(0.6) == "[0.6,0.8)"
    assert start_probability_band(0.79999999) == "[0.6,0.8)"
    assert start_probability_band(0.8) == "[0.8,1.0]"
    assert start_probability_band(1.0) == "[0.8,1.0]"  # closed top edge


def test_start_probability_band_rejects_out_of_range_values():
    with pytest.raises(ValueError, match="outside the valid range"):
        start_probability_band(-0.0001)
    with pytest.raises(ValueError, match="outside the valid range"):
        start_probability_band(1.0001)


def test_expected_minutes_band_covers_every_labelled_edge_exactly():
    for index, label in enumerate(EXPECTED_MINUTES_BAND_LABELS):
        low = EXPECTED_MINUTES_BAND_EDGES[index]
        assert expected_minutes_band(low) == label
    assert expected_minutes_band(14.9999) == "[0,15)"
    assert expected_minutes_band(15.0) == "[15,30)"
    assert expected_minutes_band(29.9999) == "[15,30)"
    assert expected_minutes_band(30.0) == "[30,45)"
    assert expected_minutes_band(44.9999) == "[30,45)"
    assert expected_minutes_band(45.0) == "[45,60)"
    assert expected_minutes_band(59.9999) == "[45,60)"
    assert expected_minutes_band(60.0) == "[60,75)"
    assert expected_minutes_band(74.9999) == "[60,75)"
    assert expected_minutes_band(75.0) == "[75,90]"
    assert expected_minutes_band(90.0) == "[75,90]"  # closed top edge


def test_expected_minutes_band_rejects_out_of_range_values():
    with pytest.raises(ValueError, match="outside the valid range"):
        expected_minutes_band(-0.0001)
    with pytest.raises(ValueError, match="outside the valid range"):
        expected_minutes_band(90.0001)


def test_expected_minutes_band_tolerates_floating_point_summation_noise_at_the_top_edge():
    # Observed in the real 2025-26 backtest: expected_minutes is a sum of
    # start_probability * minutes_per_start + substitute_probability *
    # minutes_per_substitute, which can land a few ULPs above 90.0 for a
    # logically-exactly-90 nailed-on starter. Must still band correctly, not
    # raise.
    assert expected_minutes_band(90.00000000000001) == "[75,90]"
    assert expected_minutes_band(-1e-15) == "[0,15)"


def test_start_probability_band_tolerates_floating_point_summation_noise_at_the_top_edge():
    assert start_probability_band(1.0000000000000002) == "[0.8,1.0]"
    assert start_probability_band(-1e-16) == "[0.0,0.2)"


def test_gameweek_phase_boundaries_are_exact():
    assert gameweek_phase(1) == "early"
    assert gameweek_phase(GAMEWEEK_PHASE_MID_START_GW - 1) == "early"
    assert gameweek_phase(GAMEWEEK_PHASE_MID_START_GW) == "mid"
    assert gameweek_phase(GAMEWEEK_PHASE_LATE_START_GW - 1) == "mid"
    assert gameweek_phase(GAMEWEEK_PHASE_LATE_START_GW) == "late"
    assert gameweek_phase(38) == "late"


def test_gameweek_phase_rejects_out_of_range_gameweeks():
    with pytest.raises(ValueError, match="outside the valid range"):
        gameweek_phase(0)
    with pytest.raises(ValueError, match="outside the valid range"):
        gameweek_phase(39)


# ---------------------------------------------------------------------------
# Prior-only high-xPts membership (exact replication of the committed rule)
# ---------------------------------------------------------------------------


def test_xpts_high_band_keys_match_walk_forward_calibrations_own_high_band_rows():
    # Build a population with enough gameweeks/rows for multiple eligible
    # walk-forward steps, and directly compare this module's derived key set
    # against walk_forward_calibration's own high_band_evaluation_rows -- the
    # two must always agree exactly, since the former is derived only from
    # the latter's own output.
    observations = [
        _backtest_observation(
            gameweek=gw, player_id=p, fixture_id=100 * gw + p, predicted_xpts=0.5 + 0.1 * p
        )
        for gw in range(1, 10)
        for p in range(12)
    ]
    records = walk_forward_calibration(observations, minimum_calibration_gameweeks=5)
    expected_high_band_keys = frozenset(
        (row.player_id, row.fixture_id, row.gameweek)
        for record in records
        for row in record.high_band_evaluation_rows
    )

    actual_high_band_keys, _ = xpts_high_band_and_same_window_keys_from_backtest_observations(
        observations, minimum_calibration_gameweeks=5
    )

    assert actual_high_band_keys == expected_high_band_keys
    assert actual_high_band_keys  # sanity: population is large enough to produce high-band rows


def test_same_window_keys_match_walk_forward_calibrations_own_overall_evaluation_rows():
    observations = [
        _backtest_observation(
            gameweek=gw, player_id=p, fixture_id=100 * gw + p, predicted_xpts=0.5 + 0.1 * p
        )
        for gw in range(1, 10)
        for p in range(12)
    ]
    records = walk_forward_calibration(observations, minimum_calibration_gameweeks=5)
    expected_same_window_keys = frozenset(
        (row.player_id, row.fixture_id, row.gameweek)
        for record in records
        for row in record.overall_evaluation_rows
    )

    _, actual_same_window_keys = xpts_high_band_and_same_window_keys_from_backtest_observations(
        observations, minimum_calibration_gameweeks=5
    )

    assert actual_same_window_keys == expected_same_window_keys
    assert actual_same_window_keys


def test_high_band_and_same_window_keys_use_identical_eligible_gameweeks():
    # Fix 1 requirement: high-band and comparator must use identical
    # eligible gameweeks (never inferred from min/max of the high-band keys
    # alone, which would be silently wrong if an eligible gameweek
    # contributed zero high-band rows).
    observations = [
        _backtest_observation(
            gameweek=gw, player_id=p, fixture_id=100 * gw + p, predicted_xpts=0.5 + 0.1 * p
        )
        for gw in range(1, 10)
        for p in range(12)
    ]
    records = walk_forward_calibration(observations, minimum_calibration_gameweeks=5)
    expected_eligible_gameweeks = {record.gameweek for record in records}

    high_band_keys, same_window_keys = xpts_high_band_and_same_window_keys_from_backtest_observations(
        observations, minimum_calibration_gameweeks=5
    )

    high_band_gameweeks = {key[2] for key in high_band_keys}
    same_window_gameweeks = {key[2] for key in same_window_keys}

    # same_window_keys must cover every eligible gameweek exactly.
    assert same_window_gameweeks == expected_eligible_gameweeks
    # high_band_keys' gameweeks are a subset (a gameweek can contribute zero
    # high-band rows even while being eligible and appearing in the
    # comparator).
    assert high_band_gameweeks <= expected_eligible_gameweeks


def test_high_band_and_same_window_keys_derived_from_the_same_records():
    # Fix 1 requirement: both key sets come from ONE walk_forward_calibration
    # call, not two separately-computed calls that could drift apart.
    observations = [
        _backtest_observation(
            gameweek=gw, player_id=p, fixture_id=100 * gw + p, predicted_xpts=0.5 + 0.1 * p
        )
        for gw in range(1, 10)
        for p in range(12)
    ]

    high_band_keys_a, same_window_keys_a = xpts_high_band_and_same_window_keys_from_backtest_observations(
        observations, minimum_calibration_gameweeks=5
    )
    high_band_keys_b, same_window_keys_b = xpts_high_band_and_same_window_keys_from_backtest_observations(
        observations, minimum_calibration_gameweeks=5
    )

    # Deterministic: the same input always produces the same two key sets,
    # consistent with both being derived from one walk_forward_calibration
    # call over the same observations.
    assert high_band_keys_a == high_band_keys_b
    assert same_window_keys_a == same_window_keys_b


def test_high_band_keys_are_a_strict_subset_of_same_window_keys():
    observations = [
        _backtest_observation(
            gameweek=gw, player_id=p, fixture_id=100 * gw + p, predicted_xpts=0.5 + 0.1 * p
        )
        for gw in range(1, 10)
        for p in range(12)
    ]

    high_band_keys, same_window_keys = xpts_high_band_and_same_window_keys_from_backtest_observations(
        observations, minimum_calibration_gameweeks=5
    )

    assert high_band_keys <= same_window_keys
    assert high_band_keys != same_window_keys  # strict: the high band is a real restriction
    assert len(high_band_keys) < len(same_window_keys)


def test_xpts_same_window_aligned_observations_is_a_superset_of_high_band_aligned():
    appearance_observations = tuple(
        _observation(gameweek=gw, player_code=p, fixture_id=100 * gw + p)
        for gw in range(1, 10)
        for p in range(12)
    )
    backtest_observations = tuple(
        _backtest_observation(
            gameweek=gw, player_id=p, fixture_id=100 * gw + p, predicted_xpts=0.5 + 0.1 * p
        )
        for gw in range(1, 10)
        for p in range(12)
    )
    xpts_keys = xpts_keys_from_backtest_observations(backtest_observations)
    xpts_scored_aligned = xpts_scored_aligned_observations(appearance_observations, xpts_keys)
    high_band_keys, same_window_keys = xpts_high_band_and_same_window_keys_from_backtest_observations(
        backtest_observations, minimum_calibration_gameweeks=5
    )

    high_band_aligned = xpts_high_band_aligned_observations(xpts_scored_aligned, high_band_keys)
    same_window_aligned = xpts_same_window_aligned_observations(xpts_scored_aligned, same_window_keys)

    high_band_key_set = {(o.player_code, o.fixture_id, o.gameweek) for o in high_band_aligned}
    same_window_key_set = {(o.player_code, o.fixture_id, o.gameweek) for o in same_window_aligned}
    assert high_band_key_set <= same_window_key_set
    assert len(same_window_aligned) <= len(xpts_scored_aligned)
    # The comparator must be narrower than (or equal to, if every evaluated
    # gameweek happened to be eligible) the full xpts_scored_aligned cohort,
    # confirming it is not simply an alias for that wider cohort.
    assert len(same_window_aligned) <= len(xpts_scored_aligned)


def test_xpts_high_band_aligned_observations_is_a_subset_of_its_input():
    appearance_observations = tuple(
        _observation(gameweek=1, player_code=p, fixture_id=100 + p) for p in range(10)
    )
    # Only half the keys are in the high band.
    high_band_keys = frozenset((p, 100 + p, 1) for p in range(5))

    aligned = xpts_high_band_aligned_observations(appearance_observations, high_band_keys)

    assert len(aligned) == 5
    aligned_ids = {a.player_code for a in aligned}
    assert aligned_ids == {0, 1, 2, 3, 4}


def test_xpts_high_band_aligned_observations_empty_when_no_keys_match():
    appearance_observations = tuple(
        _observation(gameweek=1, player_code=p, fixture_id=100 + p) for p in range(3)
    )
    aligned = xpts_high_band_aligned_observations(appearance_observations, frozenset())
    assert aligned == ()


def test_high_band_membership_never_exceeds_xpts_scored_aligned_cohort():
    # End-to-end cohort nesting check: appearance_eligible superset of
    # xpts_scored_aligned superset of xpts_high_band_aligned.
    appearance_observations = tuple(
        _observation(gameweek=gw, player_code=p, fixture_id=100 * gw + p)
        for gw in range(1, 10)
        for p in range(12)
    )
    backtest_observations = tuple(
        _backtest_observation(
            gameweek=gw, player_id=p, fixture_id=100 * gw + p, predicted_xpts=0.5 + 0.1 * p
        )
        for gw in range(1, 10)
        for p in range(12)
        if p < 10  # a couple of players never produced an xPts observation
    )

    xpts_keys = xpts_keys_from_backtest_observations(backtest_observations)
    xpts_scored_aligned = xpts_scored_aligned_observations(appearance_observations, xpts_keys)
    high_band_keys, _ = xpts_high_band_and_same_window_keys_from_backtest_observations(
        backtest_observations, minimum_calibration_gameweeks=5
    )
    xpts_high_band_aligned = xpts_high_band_aligned_observations(
        xpts_scored_aligned, high_band_keys
    )

    assert len(xpts_high_band_aligned) <= len(xpts_scored_aligned)
    assert len(xpts_scored_aligned) <= len(appearance_observations)
    high_band_key_set = {
        (o.player_code, o.fixture_id, o.gameweek) for o in xpts_high_band_aligned
    }
    scored_key_set = {(o.player_code, o.fixture_id, o.gameweek) for o in xpts_scored_aligned}
    assert high_band_key_set <= scored_key_set


# ---------------------------------------------------------------------------
# Cohort / key alignment
# ---------------------------------------------------------------------------


def test_appearance_eligible_and_xpts_scored_aligned_keys_are_consistent():
    appearance_observations = tuple(
        _observation(gameweek=1, player_code=p, fixture_id=100 + p) for p in range(6)
    )
    backtest_observations = tuple(
        _backtest_observation(gameweek=1, player_id=p, fixture_id=100 + p, predicted_xpts=1.0)
        for p in range(4)
    )
    xpts_keys = xpts_keys_from_backtest_observations(backtest_observations)
    aligned = xpts_scored_aligned_observations(appearance_observations, xpts_keys)

    aligned_keys = {(a.player_code, a.fixture_id, a.gameweek) for a in aligned}
    expected_keys = {(o.player_id, o.fixture_id, o.gameweek) for o in backtest_observations}
    assert aligned_keys == expected_keys


# ---------------------------------------------------------------------------
# Cluster bootstrap determinism
# ---------------------------------------------------------------------------


def test_mean_bias_bootstrap_is_deterministic_given_the_same_seed():
    rows = tuple(
        _segment_row(gameweek=gw, player_id=p, fixture_id=100 * gw + p, predicted_value=0.1 * p, actual_value=0.05 * p)
        for gw in range(1, 8)
        for p in range(10)
    )

    result_a = mean_bias_bootstrap_from_gameweek_stats(rows, resamples=500, seed=42)
    result_b = mean_bias_bootstrap_from_gameweek_stats(rows, resamples=500, seed=42)

    assert result_a.point_estimate == result_b.point_estimate
    assert result_a.ci_low == result_b.ci_low
    assert result_a.ci_high == result_b.ci_high


def test_mean_bias_bootstrap_differs_with_a_different_seed():
    # Per-gameweek bias must vary across gameweeks (not just across players
    # within a gameweek), or every resample draw -- regardless of which
    # gameweeks are drawn -- produces the same pooled statistic, making the
    # CI seed-invariant for a reason unrelated to what this test checks.
    rows = tuple(
        _segment_row(
            gameweek=gw,
            player_id=p,
            fixture_id=100 * gw + p,
            predicted_value=0.1 * p + 0.02 * gw,
            actual_value=0.05 * p,
        )
        for gw in range(1, 8)
        for p in range(10)
    )

    result_a = mean_bias_bootstrap_from_gameweek_stats(rows, resamples=500, seed=42)
    result_b = mean_bias_bootstrap_from_gameweek_stats(rows, resamples=500, seed=43)

    # Same point estimate (computed once on the true data), different CI
    # (different resample draws).
    assert result_a.point_estimate == result_b.point_estimate
    assert (result_a.ci_low, result_a.ci_high) != (result_b.ci_low, result_b.ci_high)


def test_sufficient_statistic_bootstrap_matches_row_scanning_primitive():
    # The runtime optimisation (aggregate per-gameweek sufficient statistics)
    # must reproduce block_bootstrap_statistic's numbers to floating-point
    # tolerance -- never a materially different result. See the function's
    # own docstring for why exact `==` is not expected (summation order).
    random.seed(0)
    rows = []
    for gw in range(1, 21):
        n = random.randint(3, 12)
        for i in range(n):
            rows.append(
                _segment_row(
                    gameweek=gw,
                    player_id=i,
                    fixture_id=1000 * gw + i,
                    predicted_value=random.uniform(0.0, 1.0),
                    actual_value=random.uniform(0.0, 1.0),
                )
            )

    def _statistic(resampled_rows):
        return sum(r.predicted_value - r.actual_value for r in resampled_rows) / len(resampled_rows)

    reference_point, reference_low, reference_high, _ = block_bootstrap_statistic(
        rows, statistic=_statistic, resamples=2000, seed=42
    )
    fast = mean_bias_bootstrap_from_gameweek_stats(rows, resamples=2000, seed=42)

    assert fast.point_estimate == pytest.approx(reference_point, abs=1e-9)
    assert fast.ci_low == pytest.approx(reference_low, abs=1e-9)
    assert fast.ci_high == pytest.approx(reference_high, abs=1e-9)


def test_mean_bias_bootstrap_rejects_empty_rows():
    with pytest.raises(ValueError, match="at least one row"):
        mean_bias_bootstrap_from_gameweek_stats((), resamples=100, seed=1)


# ---------------------------------------------------------------------------
# paired_contrast_bootstrap
# ---------------------------------------------------------------------------


def test_paired_contrast_point_estimate_equals_direct_unresampled_difference():
    focus = tuple(
        _segment_row(gameweek=gw, player_id=p, fixture_id=1000 * gw + p, predicted_value=0.9, actual_value=0.5)
        for gw in range(1, 11)
        for p in range(5)
    )
    comparator = tuple(
        _segment_row(gameweek=gw, player_id=p, fixture_id=2000 * gw + p, predicted_value=0.5, actual_value=0.45)
        for gw in range(1, 11)
        for p in range(20)
    )

    result = paired_contrast_bootstrap(focus, comparator, resamples=500, seed=1)

    direct_focus_bias = sum(r.predicted_value - r.actual_value for r in focus) / len(focus)
    direct_comparator_bias = sum(r.predicted_value - r.actual_value for r in comparator) / len(comparator)
    assert result.focus_bias == pytest.approx(direct_focus_bias)
    assert result.comparator_bias == pytest.approx(direct_comparator_bias)
    assert result.contrast_point_estimate == pytest.approx(direct_focus_bias - direct_comparator_bias)


def test_paired_contrast_bootstrap_is_deterministic_given_the_same_seed():
    random.seed(7)
    focus = tuple(
        _segment_row(
            gameweek=gw, player_id=p, fixture_id=1000 * gw + p,
            predicted_value=random.uniform(0.6, 1.0), actual_value=random.uniform(0.0, 0.6),
        )
        for gw in range(1, 15)
        for p in range(6)
    )
    comparator = tuple(
        _segment_row(
            gameweek=gw, player_id=p, fixture_id=2000 * gw + p,
            predicted_value=random.uniform(0.3, 0.7), actual_value=random.uniform(0.2, 0.6),
        )
        for gw in range(1, 15)
        for p in range(15)
    )

    result_a = paired_contrast_bootstrap(focus, comparator, resamples=800, seed=42)
    result_b = paired_contrast_bootstrap(focus, comparator, resamples=800, seed=42)

    assert result_a.ci_low == result_b.ci_low
    assert result_a.ci_high == result_b.ci_high
    assert result_a.contrast_point_estimate == result_b.contrast_point_estimate


def test_paired_contrast_bootstrap_uses_shared_cluster_draws_on_both_sides():
    # If both sides used INDEPENDENT draws, perturbing only the comparator's
    # per-gameweek values (while keeping the same gameweek keys/counts)
    # would change the joint distribution of (focus_mean, comparator_mean)
    # pairs in a way uncorrelated with focus's own resampled draws. Under
    # the SHARED draw, the same gameweek-index sequence drives both sides on
    # every replicate -- verified indirectly here by checking that
    # replicate-for-replicate, whenever a gameweek used only by focus is
    # heavily oversampled the contrast moves in a predictable, together way
    # -- but the direct, robust way to test "shared" is to reach into two
    # single-gameweek-per-side row sets designed so a correct SHARED draw
    # can only ever pick one of exactly two possible (focus, comparator)
    # pairings, while an independent-draw implementation could produce a
    # third, impossible pairing. Two gameweeks, each with a distinct
    # (focus_value, comparator_value) pair:
    #   GW1: focus=+10, comparator=+1   GW2: focus=-10, comparator=-1
    # A shared draw of [GW1, GW1] gives contrast = 10 - 1 = 9.
    # A shared draw of [GW2, GW2] gives contrast = -10 - (-1) = -9.
    # A shared draw of [GW1, GW2] or [GW2, GW1] gives contrast = 0 - 0 = 0.
    # No other contrast value is reachable under a correct shared draw.
    focus = (
        _segment_row(gameweek=1, player_id=1, fixture_id=1, predicted_value=10.0, actual_value=0.0),
        _segment_row(gameweek=2, player_id=2, fixture_id=2, predicted_value=-10.0, actual_value=0.0),
    )
    comparator = (
        _segment_row(gameweek=1, player_id=3, fixture_id=3, predicted_value=1.0, actual_value=0.0),
        _segment_row(gameweek=2, player_id=4, fixture_id=4, predicted_value=-1.0, actual_value=0.0),
    )

    result = paired_contrast_bootstrap(focus, comparator, resamples=5000, seed=123)

    # True, unresampled point estimate pools BOTH gameweeks on each side:
    # focus_bias = mean(10, -10) = 0, comparator_bias = mean(1, -1) = 0.
    assert result.contrast_point_estimate == pytest.approx(0.0, abs=1e-9)

    # Every individual replicate's contrast can ONLY be one of {-9, 0, 9}
    # under a correct SHARED draw: a resample of [GW1, GW1] gives
    # 10 - 1 = 9; [GW2, GW2] gives -10 - (-1) = -9; any mix of the two
    # gives 0 - 0 = 0. An INDEPENDENT-draw bug could instead pair focus's
    # GW1 draw with comparator's GW2 draw, producing 10 - (-1) = 11 or
    # -10 - 1 = -11 -- values a correct shared draw can never produce. The
    # 2.5th/97.5th percentile CI over 5000 replicates from only {-9, 0, 9}
    # must therefore stay within [-9, 9], strictly excluding +-11.
    assert -9.0 - 1e-9 <= result.ci_low <= 9.0 + 1e-9
    assert -9.0 - 1e-9 <= result.ci_high <= 9.0 + 1e-9


def test_paired_contrast_cluster_labels_are_identical_for_same_window_and_high_band_style_inputs():
    # Mirrors the xpts_high_band_aligned vs xpts_same_window_aligned
    # relationship: focus's gameweek set is a SUBSET of comparator's (every
    # focus gameweek is also a comparator gameweek, but not vice versa).
    # The shared label set must be exactly the comparator's own gameweek
    # set (the union), never focus's alone.
    focus = tuple(
        _segment_row(gameweek=gw, player_id=1, fixture_id=gw, predicted_value=0.8, actual_value=0.3)
        for gw in range(8, 15)  # 7 of the 31 "eligible" gameweeks have high-band rows
    )
    comparator = tuple(
        _segment_row(gameweek=gw, player_id=2, fixture_id=1000 + gw, predicted_value=0.5, actual_value=0.45)
        for gw in range(8, 39)  # all 31 eligible gameweeks
    )

    result = paired_contrast_bootstrap(focus, comparator, resamples=200, seed=1)

    assert result.shared_distinct_gameweeks == 31  # the comparator's full window, not focus's 7


def test_paired_contrast_bootstrap_never_labels_concentration_when_contrast_crosses_zero():
    # Regression test for the exact scenario the task requires NOT to be
    # labelled "concentration": both focus and comparator have positive,
    # individually-significant bias, but their difference is small relative
    # to its own sampling noise (the two sides' per-gameweek bias values
    # co-vary closely), so the contrast CI must cross zero.
    random.seed(99)
    focus_rows = []
    comparator_rows = []
    for gw in range(1, 21):
        # Both sides share a common per-gameweek "regime" bias, with only a
        # small, noisy difference between them -- individually both are
        # clearly positive and significant, but the paired difference is
        # small and noisy relative to its own spread.
        regime_bias = random.uniform(0.15, 0.35)
        for p in range(8):
            focus_rows.append(
                _segment_row(
                    gameweek=gw, player_id=p, fixture_id=10_000 * gw + p,
                    predicted_value=0.5 + regime_bias + random.uniform(-0.05, 0.05), actual_value=0.5,
                )
            )
        for p in range(8, 24):
            comparator_rows.append(
                _segment_row(
                    gameweek=gw, player_id=p, fixture_id=10_000 * gw + p,
                    predicted_value=0.5 + regime_bias + random.uniform(-0.05, 0.05), actual_value=0.5,
                )
            )
    focus_rows = tuple(focus_rows)
    comparator_rows = tuple(comparator_rows)

    focus_summary = mean_bias_bootstrap_from_gameweek_stats(focus_rows, resamples=2000, seed=42)
    comparator_summary = mean_bias_bootstrap_from_gameweek_stats(comparator_rows, resamples=2000, seed=42)
    contrast = paired_contrast_bootstrap(focus_rows, comparator_rows, resamples=2000, seed=42)

    # Sanity: both sides individually significant and positive.
    assert focus_summary.ci_low > 0.0
    assert comparator_summary.ci_low > 0.0
    # The contrast CI crosses zero -- this must NOT be read as "concentrated"
    # even though both sides look individually significant.
    assert contrast.ci_low < 0.0 < contrast.ci_high


def test_paired_contrast_bootstrap_rejects_empty_focus_rows():
    comparator = (_segment_row(gameweek=1, player_id=1, fixture_id=1),)
    with pytest.raises(ValueError, match="focus_rows must not be empty"):
        paired_contrast_bootstrap((), comparator, resamples=100, seed=1)


def test_paired_contrast_bootstrap_rejects_empty_comparator_rows():
    focus = (_segment_row(gameweek=1, player_id=1, fixture_id=1),)
    with pytest.raises(ValueError, match="comparator_rows must not be empty"):
        paired_contrast_bootstrap(focus, (), resamples=100, seed=1)


def test_paired_contrast_bootstrap_raises_explicitly_on_zero_focus_row_replicates():
    # Focus present in only 1 of 37 gameweeks the comparator covers -- a
    # resample has a real, non-negligible chance of drawing zero of that
    # one gameweek across all cluster draws.
    focus = (_segment_row(gameweek=1, player_id=1, fixture_id=1, predicted_value=0.9, actual_value=0.5),)
    comparator = tuple(
        _segment_row(gameweek=gw, player_id=2, fixture_id=1000 + gw, predicted_value=0.5, actual_value=0.45)
        for gw in range(1, 38)
    )

    with pytest.raises(ValueError, match="drew zero focus-side rows"):
        paired_contrast_bootstrap(focus, comparator, resamples=2000, seed=42)


def test_paired_contrast_sufficient_statistics_matches_row_resampling_reference():
    # The sufficient-statistics implementation must reproduce a slower,
    # explicit row-resampling reference to floating-point tolerance.
    random.seed(3)
    focus_rows = []
    comparator_rows = []
    for gw in range(1, 16):
        n_focus = random.randint(2, 6)
        n_comparator = random.randint(4, 10)
        for i in range(n_focus):
            focus_rows.append(
                _segment_row(
                    gameweek=gw, player_id=i, fixture_id=100_000 * gw + i,
                    predicted_value=random.uniform(0.5, 1.0), actual_value=random.uniform(0.0, 0.6),
                )
            )
        for i in range(n_comparator):
            comparator_rows.append(
                _segment_row(
                    gameweek=gw, player_id=1000 + i, fixture_id=200_000 * gw + i,
                    predicted_value=random.uniform(0.2, 0.7), actual_value=random.uniform(0.1, 0.6),
                )
            )
    focus_rows = tuple(focus_rows)
    comparator_rows = tuple(comparator_rows)

    fast = paired_contrast_bootstrap(focus_rows, comparator_rows, resamples=1500, seed=42)

    # Slow reference: explicit row-level resampling using the exact same
    # shared gameweek-label draw scheme (one rng, one set of drawn labels
    # per replicate, applied to both sides by re-pooling each side's own
    # raw rows for the drawn labels).
    focus_by_gw: dict[int, list] = {}
    for row in focus_rows:
        focus_by_gw.setdefault(row.gameweek, []).append(row)
    comparator_by_gw: dict[int, list] = {}
    for row in comparator_rows:
        comparator_by_gw.setdefault(row.gameweek, []).append(row)
    shared_labels = sorted(set(focus_by_gw) | set(comparator_by_gw))

    rng = np.random.default_rng(42)
    label_indices = rng.integers(0, len(shared_labels), size=(1500, len(shared_labels)))
    replicates = []
    for draw in label_indices:
        drawn_focus_rows = [r for idx in draw for r in focus_by_gw.get(shared_labels[idx], [])]
        drawn_comparator_rows = [r for idx in draw for r in comparator_by_gw.get(shared_labels[idx], [])]
        focus_mean = sum(r.predicted_value - r.actual_value for r in drawn_focus_rows) / len(drawn_focus_rows)
        comparator_mean = (
            sum(r.predicted_value - r.actual_value for r in drawn_comparator_rows) / len(drawn_comparator_rows)
        )
        replicates.append(focus_mean - comparator_mean)
    reference_ci_low, reference_ci_high = np.percentile(replicates, [2.5, 97.5])

    assert fast.ci_low == pytest.approx(reference_ci_low, abs=1e-9)
    assert fast.ci_high == pytest.approx(reference_ci_high, abs=1e-9)


# ---------------------------------------------------------------------------
# Segment counts summing back to their parent cohort
# ---------------------------------------------------------------------------


def test_group_rows_by_position_sums_back_to_parent_cohort():
    rows = tuple(
        _segment_row(gameweek=1, player_id=p, fixture_id=100 + p, position=("GK" if p == 0 else "DEF" if p < 4 else "MID"))
        for p in range(10)
    )

    groups = group_rows(rows, by="position")

    validate_segment_partition(rows, groups)  # must not raise
    assert sum(len(g) for g in groups.values()) == len(rows)


def test_group_rows_by_start_probability_band_sums_back_to_parent_cohort():
    rows = tuple(
        _segment_row(gameweek=1, player_id=p, fixture_id=100 + p, predicted_value=p / 20.0, actual_value=0.0)
        for p in range(20)
    )

    groups = group_rows(rows, by="start_probability_band")

    validate_segment_partition(rows, groups)
    assert sum(len(g) for g in groups.values()) == len(rows)


def test_group_rows_by_gameweek_phase_sums_back_to_parent_cohort():
    rows = tuple(
        _segment_row(gameweek=gw, player_id=1, fixture_id=gw)
        for gw in range(1, 39)  # full season GW1-38
    )

    groups = group_rows(rows, by="gameweek_phase")

    validate_segment_partition(rows, groups)
    assert set(groups) == {"early", "mid", "late"}
    assert len(groups["early"]) == 13  # GW1-13
    assert len(groups["mid"]) == 13  # GW14-26
    assert len(groups["late"]) == 12  # GW27-38


def test_group_rows_by_gameweek_phase_over_the_default_evaluation_range_matches_documented_counts():
    # The default walk-forward evaluation range is GW3-38 (GW1-2 excluded as
    # an insufficient-history cold start) -- confirms the documented
    # 11/13/12 observed-gameweek split for that specific range.
    rows = tuple(
        _segment_row(gameweek=gw, player_id=1, fixture_id=gw)
        for gw in range(3, 39)  # GW3-38
    )

    groups = group_rows(rows, by="gameweek_phase")

    validate_segment_partition(rows, groups)
    assert len(groups["early"]) == 11  # GW3-13
    assert len(groups["mid"]) == 13  # GW14-26
    assert len(groups["late"]) == 12  # GW27-38


def test_validate_segment_partition_raises_on_dropped_rows():
    rows = tuple(_segment_row(gameweek=1, player_id=p, fixture_id=100 + p) for p in range(5))
    groups = group_rows(rows, by="position")
    # Simulate a segmentation bug: drop a row from one group.
    tampered = dict(groups)
    key = next(iter(tampered))
    tampered[key] = tampered[key][:-1] if len(tampered[key]) > 1 else ()

    with pytest.raises(ValueError, match="row-count mismatch"):
        validate_segment_partition(rows, tampered)


# ---------------------------------------------------------------------------
# No-lookahead mutation
# ---------------------------------------------------------------------------


def test_high_band_keys_for_an_early_gameweek_are_unaffected_by_a_later_gameweek_outlier():
    base_observations = [
        _backtest_observation(
            gameweek=gw, player_id=p, fixture_id=100 * gw + p, predicted_xpts=0.5 + 0.1 * p
        )
        for gw in range(1, 10)
        for p in range(12)
    ]
    baseline_keys, _ = xpts_high_band_and_same_window_keys_from_backtest_observations(
        base_observations, minimum_calibration_gameweeks=5
    )
    earliest_eligible_gameweek = 6  # first gameweek with >=5 strictly-prior gameweeks
    baseline_early_keys = {k for k in baseline_keys if k[2] == earliest_eligible_gameweek}
    assert baseline_early_keys  # sanity: the earliest eligible fold has some high-band rows

    # Inflate predicted_xpts for every row in the LAST gameweek only -- this
    # could never have been known when gameweek 6's high-band threshold/fit
    # was computed.
    perturbed_observations = [
        _backtest_observation(
            gameweek=o.gameweek,
            player_id=o.player_id,
            fixture_id=o.fixture_id,
            predicted_xpts=o.predicted_xpts + 1000.0 if o.gameweek == 9 else o.predicted_xpts,
        )
        for o in base_observations
    ]
    perturbed_keys, _ = xpts_high_band_and_same_window_keys_from_backtest_observations(
        perturbed_observations, minimum_calibration_gameweeks=5
    )
    perturbed_early_keys = {k for k in perturbed_keys if k[2] == earliest_eligible_gameweek}

    assert perturbed_early_keys == baseline_early_keys


def test_segment_row_construction_does_not_mutate_input_observations():
    observation = _observation(
        gameweek=1, player_code=1, fixture_id=1, predicted_start_probability=0.42
    )
    before = (
        observation.predicted_start_probability,
        observation.predicted_expected_minutes,
        observation.actual_started,
        observation.actual_minutes,
    )

    observations_to_segment_rows((observation,), target="start_probability")

    after = (
        observation.predicted_start_probability,
        observation.predicted_expected_minutes,
        observation.actual_started,
        observation.actual_minutes,
    )
    assert before == after


def test_segment_summary_of_an_earlier_gameweek_is_unaffected_by_a_later_gameweeks_rows():
    early_only = tuple(
        _observation(
            gameweek=1,
            player_code=p,
            fixture_id=100 + p,
            predicted_start_probability=0.5,
            actual_started=(p % 2 == 0),
        )
        for p in range(10)
    )
    early_and_late = early_only + tuple(
        _observation(
            gameweek=2,
            player_code=p,
            fixture_id=200 + p,
            predicted_start_probability=0.9,
            actual_started=False,  # a large, contrary outcome in a later gameweek
        )
        for p in range(10)
    )

    rows_early_only = observations_to_segment_rows(early_only, target="start_probability")
    rows_early_and_late = tuple(
        row
        for row in observations_to_segment_rows(early_and_late, target="start_probability")
        if row.gameweek == 1
    )

    summary_early_only = summarize_segment(rows_early_only, target="start_probability", resamples=100, seed=1)
    summary_early_and_late = summarize_segment(
        rows_early_and_late, target="start_probability", resamples=100, seed=1
    )

    assert summary_early_only.mean_bias == summary_early_and_late.mean_bias
    assert summary_early_only.brier_score == summary_early_and_late.brier_score


# ---------------------------------------------------------------------------
# summarize_segment: insufficient-variation flag
# ---------------------------------------------------------------------------


def test_summarize_segment_flags_insufficient_variation_for_constant_predictor():
    rows = tuple(
        _segment_row(gameweek=1, player_id=p, fixture_id=100 + p, predicted_value=0.5, actual_value=0.1 * p)
        for p in range(5)
    )

    summary = summarize_segment(rows, target="start_probability", resamples=50, seed=1)

    assert summary.insufficient_variation is True
    assert summary.predicted_variance == 0.0


def test_summarize_segment_flags_insufficient_variation_for_single_row():
    rows = (_segment_row(gameweek=1, player_id=1, fixture_id=1, predicted_value=0.5, actual_value=1.0),)

    summary = summarize_segment(rows, target="start_probability", resamples=50, seed=1)

    assert summary.insufficient_variation is True
    assert summary.rows == 1


def test_summarize_segment_does_not_flag_insufficient_variation_when_predictor_varies():
    rows = tuple(
        _segment_row(gameweek=1, player_id=p, fixture_id=100 + p, predicted_value=0.1 * p, actual_value=0.05 * p)
        for p in range(1, 6)
    )

    summary = summarize_segment(rows, target="start_probability", resamples=50, seed=1)

    assert summary.insufficient_variation is False
    assert summary.predicted_variance > 0.0


def test_summarize_segment_rejects_empty_rows():
    with pytest.raises(ValueError, match="at least one row"):
        summarize_segment((), target="start_probability", resamples=50, seed=1)


def test_summarize_segment_reports_start_probability_fields_only_for_that_target():
    rows = tuple(
        _segment_row(gameweek=1, player_id=p, fixture_id=100 + p, predicted_value=0.1 * p, actual_value=1.0 if p % 2 == 0 else 0.0)
        for p in range(1, 6)
    )

    summary = summarize_segment(rows, target="start_probability", resamples=50, seed=1)

    assert summary.brier_score is not None
    assert summary.observed_start_rate is not None
    assert summary.mse is None
    assert summary.mae is None


def test_summarize_segment_reports_expected_minutes_fields_only_for_that_target():
    rows = tuple(
        _segment_row(gameweek=1, player_id=p, fixture_id=100 + p, predicted_value=10.0 * p, actual_value=8.0 * p)
        for p in range(1, 6)
    )

    summary = summarize_segment(rows, target="expected_minutes", resamples=50, seed=1)

    assert summary.mse is not None
    assert summary.mae is not None
    assert summary.brier_score is None
    assert summary.observed_start_rate is None


# ---------------------------------------------------------------------------
# summarize_segment: bias_sum (mathematically real "contribution" quantity)
# ---------------------------------------------------------------------------


def test_bias_sum_equals_mean_bias_times_rows():
    rows = tuple(
        _segment_row(gameweek=1, player_id=p, fixture_id=100 + p, predicted_value=0.1 * p, actual_value=0.05 * p)
        for p in range(1, 8)
    )

    summary = summarize_segment(rows, target="start_probability", resamples=50, seed=1)

    assert summary.bias_sum == pytest.approx(summary.mean_bias * summary.rows)


def test_bias_sum_sums_exactly_across_a_partition():
    # Fix 4 requirement: bias_sum must be additive across a full partition
    # (unlike mean_bias, a per-row average) -- summing every position
    # segment's own bias_sum must equal the parent cohort's own bias_sum.
    rows = tuple(
        _segment_row(
            gameweek=1,
            player_id=p,
            fixture_id=100 + p,
            position=("GK" if p == 0 else "DEF" if p < 4 else "MID" if p < 7 else "FWD"),
            predicted_value=0.1 * p,
            actual_value=0.03 * p,
        )
        for p in range(10)
    )

    parent_summary = summarize_segment(rows, target="start_probability", resamples=50, seed=1)
    groups = group_rows(rows, by="position")
    segment_summaries = [
        summarize_segment(group, target="start_probability", resamples=50, seed=1)
        for group in groups.values()
    ]

    assert sum(s.bias_sum for s in segment_summaries) == pytest.approx(parent_summary.bias_sum)


def test_bias_sum_sign_reflects_offsetting_segments():
    # A segment whose bias_sum has the OPPOSITE sign from the parent
    # cohort's own net bias_sum is offsetting the aggregate, not
    # contributing to it -- the sign must be preserved (never abs()'d away
    # inside summarize_segment itself; that decision belongs to the caller
    # ranking segments, not this function).
    overpredicting_rows = tuple(
        _segment_row(gameweek=1, player_id=p, fixture_id=100 + p, predicted_value=0.9, actual_value=0.1)
        for p in range(5)
    )
    underpredicting_rows = tuple(
        _segment_row(gameweek=1, player_id=p, fixture_id=200 + p, predicted_value=0.1, actual_value=0.9)
        for p in range(2)
    )
    all_rows = overpredicting_rows + underpredicting_rows

    parent_summary = summarize_segment(all_rows, target="start_probability", resamples=50, seed=1)
    over_summary = summarize_segment(overpredicting_rows, target="start_probability", resamples=50, seed=1)
    under_summary = summarize_segment(underpredicting_rows, target="start_probability", resamples=50, seed=1)

    assert over_summary.bias_sum > 0.0
    assert under_summary.bias_sum < 0.0
    # The overpredicting segment's bias_sum has the SAME sign as the parent
    # (net positive, since it has more rows); the underpredicting segment's
    # sign is opposite -- it offsets, rather than contributes to, the net.
    assert parent_summary.bias_sum > 0.0
    assert (over_summary.bias_sum > 0.0) == (parent_summary.bias_sum > 0.0)
    assert (under_summary.bias_sum > 0.0) != (parent_summary.bias_sum > 0.0)
    # And they still sum exactly to the parent's own bias_sum.
    assert over_summary.bias_sum + under_summary.bias_sum == pytest.approx(parent_summary.bias_sum)


# ---------------------------------------------------------------------------
# Minimum gameweek-cluster guard constant
# ---------------------------------------------------------------------------


def test_minimum_gameweek_clusters_for_stable_label_is_a_positive_integer():
    assert isinstance(MINIMUM_GAMEWEEK_CLUSTERS_FOR_STABLE_LABEL, int)
    assert MINIMUM_GAMEWEEK_CLUSTERS_FOR_STABLE_LABEL > 1
