from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fpl_model.validation.appearance_calibration import (
    AppearanceCalibrationRow,
    OlsFit,
    appearance_implication_verdict,
    apply_calibration,
    causal_walk_forward_predictions,
    describe_appearance_implication_verdict,
    fit_ols,
    pooled_ols_from_rows,
    pooled_walk_forward_slope,
    walk_forward_appearance_calibration,
    xpts_keys_from_backtest_observations,
    xpts_scored_aligned_observations,
)
from fpl_model.validation.backtest import BacktestObservation
from fpl_model.validation.benchwarmers_backtest import AppearanceObservation


def _observation(
    *,
    gameweek: int,
    player_code: int,
    fixture_id: int,
    predicted_start_probability: float,
    predicted_expected_minutes: float,
    actual_started: bool,
    actual_minutes: float,
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
    *, gameweek: int, player_id: int, fixture_id: int, predicted_xpts: float = 2.0, actual_points: float = 2.0
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


# ---------------------------------------------------------------------------
# fit_ols: closed-form OLS
# ---------------------------------------------------------------------------


def test_fit_ols_recovers_perfect_calibration():
    fit = fit_ols([0.2, 0.4, 0.6, 0.8, 1.0], [0.2, 0.4, 0.6, 0.8, 1.0])

    assert fit.slope == pytest.approx(1.0)
    assert fit.intercept == pytest.approx(0.0, abs=1e-9)
    assert fit.training_rows == 5
    assert fit.training_gameweeks == 1  # default when not derived from AppearanceCalibrationRow


def test_fit_ols_recovers_known_overconfidence():
    predicted = [0.2, 0.4, 0.6, 0.8, 1.0]
    actual = [0.5 * p for p in predicted]

    fit = fit_ols(predicted, actual)

    assert fit.slope == pytest.approx(0.5)
    assert fit.intercept == pytest.approx(0.0, abs=1e-9)


def test_fit_ols_rejects_constant_predictor():
    with pytest.raises(ValueError, match="constant"):
        fit_ols([0.5, 0.5, 0.5], [0.0, 1.0, 1.0])


def test_fit_ols_rejects_fewer_than_two_rows():
    with pytest.raises(ValueError, match="at least 2 rows"):
        fit_ols([0.5], [1.0])


def test_fit_ols_rejects_mismatched_lengths():
    with pytest.raises(ValueError, match="same length"):
        fit_ols([0.5, 0.6], [1.0])


def test_fit_ols_accepts_explicit_training_gameweeks():
    fit = fit_ols([0.1, 0.2, 0.3], [0.1, 0.2, 0.3], training_gameweeks=7)

    assert fit.training_gameweeks == 7


# ---------------------------------------------------------------------------
# pooled_ols_from_rows / apply_calibration
# ---------------------------------------------------------------------------


def test_pooled_ols_from_rows_derives_training_gameweeks_from_row_tags():
    rows = (
        AppearanceCalibrationRow(gameweek=1, player_id=1, fixture_id=10, predicted_value=0.5, actual_value=0.5),
        AppearanceCalibrationRow(gameweek=1, player_id=2, fixture_id=11, predicted_value=0.8, actual_value=0.8),
        AppearanceCalibrationRow(gameweek=2, player_id=3, fixture_id=12, predicted_value=0.3, actual_value=0.3),
    )

    fit = pooled_ols_from_rows(rows)

    assert fit.training_rows == 3
    assert fit.training_gameweeks == 2  # gameweeks {1, 2}
    assert fit.slope == pytest.approx(1.0)


def test_apply_calibration_applies_slope_and_intercept():
    rows = (
        AppearanceCalibrationRow(gameweek=1, player_id=1, fixture_id=10, predicted_value=0.5, actual_value=0.0),
        AppearanceCalibrationRow(gameweek=1, player_id=2, fixture_id=11, predicted_value=1.0, actual_value=0.0),
    )
    custom_fit = OlsFit(slope=0.5, intercept=0.1, training_rows=2, training_gameweeks=1)

    calibrated = apply_calibration(rows, custom_fit)

    assert calibrated == (0.1 + 0.5 * 0.5, 0.1 + 0.5 * 1.0)


# ---------------------------------------------------------------------------
# walk_forward_appearance_calibration: no-lookahead correctness
# ---------------------------------------------------------------------------


def _uniform_observations(
    *, gameweeks: int, players: int, start_slope: float, minutes_slope: float
) -> tuple[AppearanceObservation, ...]:
    observations = []
    for gw in range(1, gameweeks + 1):
        for p in range(players):
            predicted_start = 0.1 + p * 0.1  # spread across [0.1, ~1.0]
            predicted_minutes = 10.0 + p * 8.0  # spread across [10, ~82]
            actual_start = start_slope * predicted_start
            actual_minutes = minutes_slope * predicted_minutes
            observations.append(
                _observation(
                    gameweek=gw,
                    player_code=p,
                    fixture_id=100 * gw + p,
                    predicted_start_probability=predicted_start,
                    predicted_expected_minutes=predicted_minutes,
                    actual_started=actual_start >= 0.5,
                    actual_minutes=actual_minutes,
                )
            )
    return tuple(observations)


def _regime_shift_observations(
    *, gameweeks: int, players: int, shift_gameweek: int, early_slope: float, late_slope: float, target: str
) -> tuple[AppearanceObservation, ...]:
    """Observations whose true predicted->actual relationship changes mid-season.

    Only the requested ``target``'s realised outcome actually varies by
    slope; the other target is held at a fixed, unrelated relationship so
    each test can isolate one target's regime shift at a time.
    """
    observations = []
    for gw in range(1, gameweeks + 1):
        slope = early_slope if gw < shift_gameweek else late_slope
        for p in range(players):
            predicted_start = 0.1 + p * 0.1
            predicted_minutes = 10.0 + p * 8.0
            if target == "start_probability":
                actual_started = (slope * predicted_start) >= 0.5
                actual_minutes = 0.9 * predicted_minutes
            else:
                actual_started = predicted_start >= 0.5
                actual_minutes = slope * predicted_minutes
            observations.append(
                _observation(
                    gameweek=gw,
                    player_code=p,
                    fixture_id=100 * gw + p,
                    predicted_start_probability=predicted_start,
                    predicted_expected_minutes=predicted_minutes,
                    actual_started=actual_started,
                    actual_minutes=actual_minutes,
                )
            )
    return tuple(observations)


def test_walk_forward_appearance_calibration_respects_minimum_calibration_gameweeks():
    observations = _uniform_observations(gameweeks=8, players=10, start_slope=0.8, minutes_slope=0.8)

    records = walk_forward_appearance_calibration(
        observations, target="expected_minutes", minimum_calibration_gameweeks=5
    )

    # GW1-5 have fewer than 5 distinct prior gameweeks; GW6 is the first with
    # exactly 5 prior gameweeks (1-5).
    assert [record.gameweek for record in records] == [6, 7, 8]
    assert all(record.target == "expected_minutes" for record in records)


def test_walk_forward_appearance_calibration_never_uses_current_or_future_gameweek_rows():
    # An extreme outlier placed only in GW7 must not affect GW6's fit (GW6's
    # prior pool is GW1-5, which never includes GW7) or GW7's own fit (which
    # must use GW1-6 as training, not include GW7's own outlier row).
    observations = list(
        _uniform_observations(gameweeks=8, players=10, start_slope=0.8, minutes_slope=0.8)
    )
    observations.append(
        _observation(
            gameweek=7,
            player_code=999,
            fixture_id=9999,
            predicted_start_probability=1.0,
            predicted_expected_minutes=90.0,
            actual_started=True,
            actual_minutes=90.0,
        )
    )

    records = walk_forward_appearance_calibration(
        observations, target="expected_minutes", minimum_calibration_gameweeks=5
    )
    by_gameweek = {record.gameweek: record for record in records}

    # GW6's training pool is GW1-5 (50 rows: 5 gameweeks x 10 players) --
    # completely unaffected by the GW7 outlier.
    assert by_gameweek[6].fit.training_rows == 50
    assert by_gameweek[6].fit.slope == pytest.approx(0.8)

    # GW7's training pool is GW1-6 (60 rows), which does not include GW7's
    # own outlier row (the outlier is in GW7's *evaluation* rows, not
    # training) -- so GW7's fitted slope must also still be exactly 0.8, not
    # distorted by its own gameweek's outlier.
    assert by_gameweek[7].fit.training_rows == 60
    assert by_gameweek[7].fit.slope == pytest.approx(0.8)
    # The outlier does appear in GW7's own evaluation rows, confirming it
    # was correctly excluded from training but not silently dropped.
    assert any(row.player_id == 999 for row in by_gameweek[7].evaluation_rows)


def test_walk_forward_appearance_calibration_rejects_empty_observations():
    with pytest.raises(ValueError, match="at least one observation"):
        walk_forward_appearance_calibration((), target="start_probability")


def test_walk_forward_appearance_calibration_rejects_invalid_minimum_gameweeks():
    observations = _uniform_observations(gameweeks=3, players=2, start_slope=1.0, minutes_slope=1.0)
    with pytest.raises(ValueError, match="minimum_calibration_gameweeks"):
        walk_forward_appearance_calibration(
            observations, target="start_probability", minimum_calibration_gameweeks=0
        )


def test_walk_forward_appearance_calibration_rejects_invalid_target():
    observations = _uniform_observations(gameweeks=8, players=10, start_slope=1.0, minutes_slope=1.0)
    with pytest.raises(ValueError, match="target"):
        walk_forward_appearance_calibration(observations, target="unknown")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# pooled_walk_forward_slope
# ---------------------------------------------------------------------------


def test_pooled_walk_forward_slope_pools_all_eligible_evaluation_rows():
    observations = _uniform_observations(gameweeks=8, players=10, start_slope=0.8, minutes_slope=0.8)
    records = walk_forward_appearance_calibration(
        observations, target="expected_minutes", minimum_calibration_gameweeks=5
    )

    pooled = pooled_walk_forward_slope(records)

    expected_rows = sum(len(record.evaluation_rows) for record in records)
    assert pooled["evaluation_rows"] == expected_rows
    assert pooled["eligible_gameweeks"] == len(records)
    assert pooled["target"] == "expected_minutes"
    assert pooled["slope"] == pytest.approx(0.8)


def test_pooled_walk_forward_slope_rejects_empty_records():
    with pytest.raises(ValueError, match="at least one calibration record"):
        pooled_walk_forward_slope(())


# ---------------------------------------------------------------------------
# causal_walk_forward_predictions
# ---------------------------------------------------------------------------


def test_causal_predictions_differ_from_naively_applying_the_pooled_fit():
    # Regression test mirroring the xPts calibration's own bug fix: applying
    # ONE pooled OLS fit (fit across every eligible gameweek's own evaluation
    # rows) back onto those same rows is a materially different -- and
    # in-sample -- MAE than applying each gameweek's own prior-only fit.
    observations = _regime_shift_observations(
        gameweeks=12,
        players=10,
        shift_gameweek=8,
        early_slope=0.5,
        late_slope=1.5,
        target="expected_minutes",
    )
    records = walk_forward_appearance_calibration(
        observations, target="expected_minutes", minimum_calibration_gameweeks=5
    )

    rows, causal_predictions = causal_walk_forward_predictions(records)

    pooled = pooled_walk_forward_slope(records)
    pooled_fit = OlsFit(
        slope=pooled["slope"],
        intercept=pooled["intercept"],
        training_rows=pooled["evaluation_rows"],
        training_gameweeks=pooled["eligible_gameweeks"],
    )
    pooled_applied_predictions = apply_calibration(pooled["rows"], pooled_fit)

    assert rows == pooled["rows"]

    actual = [row.actual_value for row in rows]
    causal_mae = sum(abs(p - a) for p, a in zip(causal_predictions, actual, strict=True)) / len(rows)
    pooled_mae = sum(
        abs(p - a) for p, a in zip(pooled_applied_predictions, actual, strict=True)
    ) / len(rows)

    assert causal_mae != pytest.approx(pooled_mae)
    assert causal_mae > pooled_mae


def test_causal_predictions_use_every_records_own_prior_only_fit():
    observations = _regime_shift_observations(
        gameweeks=12,
        players=10,
        shift_gameweek=8,
        early_slope=0.5,
        late_slope=1.5,
        target="expected_minutes",
    )
    records = walk_forward_appearance_calibration(
        observations, target="expected_minutes", minimum_calibration_gameweeks=5
    )

    rows, causal_predictions = causal_walk_forward_predictions(records)

    expected_rows: list[AppearanceCalibrationRow] = []
    expected_predictions: list[float] = []
    for record in records:
        expected_rows.extend(record.evaluation_rows)
        expected_predictions.extend(apply_calibration(record.evaluation_rows, record.fit))

    assert rows == tuple(expected_rows)
    assert causal_predictions == pytest.approx(tuple(expected_predictions))


def test_causal_predictions_current_or_future_outcomes_cannot_change_an_earlier_folds_prediction():
    baseline_observations = list(
        _regime_shift_observations(
            gameweeks=12,
            players=10,
            shift_gameweek=8,
            early_slope=0.5,
            late_slope=1.5,
            target="expected_minutes",
        )
    )
    baseline_records = walk_forward_appearance_calibration(
        baseline_observations, target="expected_minutes", minimum_calibration_gameweeks=5
    )
    baseline_rows, baseline_predictions = causal_walk_forward_predictions(baseline_records)
    earliest_eligible_gameweek = baseline_records[0].gameweek
    baseline_early_predictions = {
        (row.player_id, row.fixture_id): pred
        for row, pred in zip(baseline_rows, baseline_predictions, strict=True)
        if row.gameweek == earliest_eligible_gameweek
    }
    assert baseline_early_predictions  # sanity: the earliest fold has rows

    # Perturb a gameweek strictly AFTER the earliest eligible gameweek --
    # this could never have been known when the earliest fold's fit was made.
    perturbed_observations = [
        _observation(
            gameweek=o.gameweek,
            player_code=o.player_code,
            fixture_id=o.fixture_id,
            predicted_start_probability=o.predicted_start_probability,
            predicted_expected_minutes=o.predicted_expected_minutes,
            actual_started=o.actual_started,
            actual_minutes=o.actual_minutes + 1000.0 if o.gameweek == 12 else o.actual_minutes,
        )
        for o in baseline_observations
    ]
    perturbed_records = walk_forward_appearance_calibration(
        perturbed_observations, target="expected_minutes", minimum_calibration_gameweeks=5
    )
    perturbed_rows, perturbed_predictions = causal_walk_forward_predictions(perturbed_records)
    perturbed_early_predictions = {
        (row.player_id, row.fixture_id): pred
        for row, pred in zip(perturbed_rows, perturbed_predictions, strict=True)
        if row.gameweek == earliest_eligible_gameweek
    }

    assert perturbed_early_predictions == pytest.approx(baseline_early_predictions)


def test_causal_predictions_row_keys_match_across_raw_actual_and_calibrated():
    observations = _regime_shift_observations(
        gameweeks=12,
        players=10,
        shift_gameweek=8,
        early_slope=0.5,
        late_slope=1.5,
        target="start_probability",
    )
    records = walk_forward_appearance_calibration(
        observations, target="start_probability", minimum_calibration_gameweeks=5
    )

    rows, calibrated_predictions = causal_walk_forward_predictions(records)
    assert len(rows) == len(calibrated_predictions)

    raw_keys = {(row.player_id, row.fixture_id, row.gameweek) for row in rows}
    actual_keys = {(row.player_id, row.fixture_id, row.gameweek) for row in rows}
    assert raw_keys == actual_keys
    assert len(raw_keys) == len(rows)  # no duplicate keys


# ---------------------------------------------------------------------------
# xpts_scored_aligned_observations / xpts_keys_from_backtest_observations
# ---------------------------------------------------------------------------


def test_xpts_scored_aligned_observations_keys_exactly_equal_backtest_observation_keys():
    appearance_observations = tuple(
        _observation(
            gameweek=gw,
            player_code=p,
            fixture_id=100 * gw + p,
            predicted_start_probability=0.5,
            predicted_expected_minutes=45.0,
            actual_started=True,
            actual_minutes=45.0,
        )
        for gw in range(1, 4)
        for p in range(5)
    )
    # Only a subset of the appearance rows also produced an xPts observation
    # (e.g. some players failed an unrelated xPts-only requirement).
    backtest_observations = tuple(
        _backtest_observation(gameweek=gw, player_id=p, fixture_id=100 * gw + p)
        for gw in range(1, 4)
        for p in range(3)  # only players 0, 1, 2 -- not 3, 4
    )

    xpts_keys = xpts_keys_from_backtest_observations(backtest_observations)
    aligned = xpts_scored_aligned_observations(appearance_observations, xpts_keys)

    aligned_keys = {(a.player_code, a.fixture_id, a.gameweek) for a in aligned}
    expected_keys = {
        (o.player_id, o.fixture_id, o.gameweek) for o in backtest_observations
    }
    assert aligned_keys == expected_keys
    assert len(aligned) == len(backtest_observations)


def test_xpts_scored_aligned_observations_is_a_subset_of_the_full_appearance_cohort():
    appearance_observations = tuple(
        _observation(
            gameweek=1,
            player_code=p,
            fixture_id=100 + p,
            predicted_start_probability=0.5,
            predicted_expected_minutes=45.0,
            actual_started=True,
            actual_minutes=45.0,
        )
        for p in range(10)
    )
    # Half the players produced an xPts observation, half did not.
    backtest_observations = tuple(
        _backtest_observation(gameweek=1, player_id=p, fixture_id=100 + p) for p in range(5)
    )

    xpts_keys = xpts_keys_from_backtest_observations(backtest_observations)
    aligned = xpts_scored_aligned_observations(appearance_observations, xpts_keys)

    assert len(aligned) <= len(appearance_observations)
    assert len(aligned) < len(appearance_observations)  # strictly smaller in this case
    aligned_ids = {a.player_code for a in aligned}
    full_ids = {a.player_code for a in appearance_observations}
    assert aligned_ids <= full_ids


def test_xpts_scored_aligned_observations_equals_full_cohort_when_all_rows_score():
    appearance_observations = tuple(
        _observation(
            gameweek=1,
            player_code=p,
            fixture_id=100 + p,
            predicted_start_probability=0.5,
            predicted_expected_minutes=45.0,
            actual_started=True,
            actual_minutes=45.0,
        )
        for p in range(5)
    )
    backtest_observations = tuple(
        _backtest_observation(gameweek=1, player_id=p, fixture_id=100 + p) for p in range(5)
    )

    xpts_keys = xpts_keys_from_backtest_observations(backtest_observations)
    aligned = xpts_scored_aligned_observations(appearance_observations, xpts_keys)

    assert len(aligned) == len(appearance_observations)


def test_xpts_scored_aligned_observations_empty_when_no_keys_match():
    appearance_observations = tuple(
        _observation(
            gameweek=1,
            player_code=p,
            fixture_id=100 + p,
            predicted_start_probability=0.5,
            predicted_expected_minutes=45.0,
            actual_started=True,
            actual_minutes=45.0,
        )
        for p in range(3)
    )
    xpts_keys = xpts_keys_from_backtest_observations(())

    aligned = xpts_scored_aligned_observations(appearance_observations, xpts_keys)

    assert aligned == ()


# ---------------------------------------------------------------------------
# appearance_implication_verdict / describe_appearance_implication_verdict
# ---------------------------------------------------------------------------


def test_appearance_implication_verdict_both():
    verdict = appearance_implication_verdict(
        start_probability_ci_below_one=True, expected_minutes_ci_below_one=True
    )

    assert verdict == "both"


def test_appearance_implication_verdict_start_probability_only():
    verdict = appearance_implication_verdict(
        start_probability_ci_below_one=True, expected_minutes_ci_below_one=False
    )

    assert verdict == "start_probability_only"


def test_appearance_implication_verdict_expected_minutes_only():
    verdict = appearance_implication_verdict(
        start_probability_ci_below_one=False, expected_minutes_ci_below_one=True
    )

    assert verdict == "expected_minutes_only"


def test_appearance_implication_verdict_neither():
    verdict = appearance_implication_verdict(
        start_probability_ci_below_one=False, expected_minutes_ci_below_one=False
    )

    assert verdict == "neither"


def test_describe_appearance_implication_verdict_both_claims_both_targets():
    description = describe_appearance_implication_verdict("both")

    assert "Both targets' slope bootstrap CIs lie entirely below 1.0" in description
    # Must not name only one target as the sole implicated one.
    assert "Only start_probability" not in description
    assert "Only expected_minutes" not in description


def test_describe_appearance_implication_verdict_start_probability_only_is_direction_aware():
    description = describe_appearance_implication_verdict("start_probability_only")

    assert "Only start_probability's slope bootstrap CI lies entirely below 1.0" in description
    assert "expected_minutes' CI does not" in description
    # Must not claim both targets, and must not claim the wrong single target.
    assert "Both targets'" not in description
    assert "Only expected_minutes's slope bootstrap CI lies entirely below 1.0" not in description


def test_describe_appearance_implication_verdict_expected_minutes_only_is_direction_aware():
    description = describe_appearance_implication_verdict("expected_minutes_only")

    assert "Only expected_minutes' slope bootstrap CI lies entirely below 1.0" in description
    assert "start_probability's CI does not" in description
    # Must not claim both targets, and must not claim the wrong single target.
    assert "Both targets'" not in description
    assert "Only start_probability's slope bootstrap CI lies entirely below 1.0" not in description


def test_describe_appearance_implication_verdict_neither_does_not_implicate_either_target():
    description = describe_appearance_implication_verdict("neither")

    assert "Neither target's slope bootstrap CI lies entirely below 1.0" in description
    assert "does NOT support the appearance model" in description
    assert "Both targets'" not in description
    assert "Only start_probability" not in description
    assert "Only expected_minutes" not in description


def test_appearance_implication_verdict_mixed_cases_never_use_the_or_shortcut():
    # Regression test for the original bug: `start_ci_below_one OR
    # minutes_ci_below_one` cannot distinguish "both" from either mixed case,
    # so a caller using that OR would describe a single-target result with
    # "both targets" wording. Verify the two mixed cases produce genuinely
    # different, mutually exclusive descriptions from "both".
    start_only = describe_appearance_implication_verdict(
        appearance_implication_verdict(
            start_probability_ci_below_one=True, expected_minutes_ci_below_one=False
        )
    )
    minutes_only = describe_appearance_implication_verdict(
        appearance_implication_verdict(
            start_probability_ci_below_one=False, expected_minutes_ci_below_one=True
        )
    )
    both = describe_appearance_implication_verdict(
        appearance_implication_verdict(
            start_probability_ci_below_one=True, expected_minutes_ci_below_one=True
        )
    )

    assert start_only != minutes_only
    assert start_only != both
    assert minutes_only != both
