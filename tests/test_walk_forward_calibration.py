from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fpl_model.validation.backtest import BacktestObservation
from fpl_model.validation.walk_forward_calibration import (
    CalibrationRow,
    OlsFit,
    apply_calibration,
    causal_walk_forward_predictions,
    fit_ols,
    pooled_ols_from_rows,
    pooled_walk_forward_slope,
    walk_forward_calibration,
)


def _observation(
    *, gameweek: int, player_id: int, fixture_id: int, predicted_xpts: float, actual_points: float
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
    fit = fit_ols([1.0, 2.0, 3.0, 4.0, 5.0], [1.0, 2.0, 3.0, 4.0, 5.0])

    assert fit.slope == pytest.approx(1.0)
    assert fit.intercept == pytest.approx(0.0, abs=1e-9)
    assert fit.training_rows == 5
    assert fit.training_gameweeks == 1  # default when not derived from CalibrationRow


def test_fit_ols_recovers_known_20_percent_overprediction():
    predicted = [1.0, 2.0, 3.0, 4.0, 5.0]
    actual = [0.8 * p for p in predicted]

    fit = fit_ols(predicted, actual)

    assert fit.slope == pytest.approx(0.8)
    assert fit.intercept == pytest.approx(0.0, abs=1e-9)


def test_fit_ols_rejects_constant_predictor():
    with pytest.raises(ValueError, match="constant"):
        fit_ols([2.0, 2.0, 2.0], [1.0, 2.0, 3.0])


def test_fit_ols_rejects_fewer_than_two_rows():
    with pytest.raises(ValueError, match="at least 2 rows"):
        fit_ols([1.0], [1.0])


def test_fit_ols_rejects_mismatched_lengths():
    with pytest.raises(ValueError, match="same length"):
        fit_ols([1.0, 2.0], [1.0])


def test_fit_ols_accepts_explicit_training_gameweeks():
    fit = fit_ols([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], training_gameweeks=7)

    assert fit.training_gameweeks == 7


# ---------------------------------------------------------------------------
# pooled_ols_from_rows / apply_calibration
# ---------------------------------------------------------------------------


def test_pooled_ols_from_rows_derives_training_gameweeks_from_row_tags():
    rows = (
        CalibrationRow(gameweek=1, player_id=1, fixture_id=10, predicted_xpts=1.0, actual_points=1.0),
        CalibrationRow(gameweek=1, player_id=2, fixture_id=11, predicted_xpts=2.0, actual_points=2.0),
        CalibrationRow(gameweek=2, player_id=3, fixture_id=12, predicted_xpts=3.0, actual_points=3.0),
    )

    fit = pooled_ols_from_rows(rows)

    assert fit.training_rows == 3
    assert fit.training_gameweeks == 2  # gameweeks {1, 2}
    assert fit.slope == pytest.approx(1.0)


def test_apply_calibration_applies_slope_and_intercept():
    rows = (
        CalibrationRow(gameweek=1, player_id=1, fixture_id=10, predicted_xpts=2.0, actual_points=0.0),
        CalibrationRow(gameweek=1, player_id=2, fixture_id=11, predicted_xpts=4.0, actual_points=0.0),
    )
    fit = fit_ols([1.0, 2.0], [1.0, 2.0])  # slope=1, intercept=0 baseline
    custom_fit = type(fit)(slope=0.5, intercept=1.0, training_rows=2, training_gameweeks=1)

    calibrated = apply_calibration(rows, custom_fit)

    assert calibrated == (1.0 + 0.5 * 2.0, 1.0 + 0.5 * 4.0)


# ---------------------------------------------------------------------------
# walk_forward_calibration: no-lookahead correctness
# ---------------------------------------------------------------------------


def _uniform_observations(*, gameweeks: int, players: int, slope: float) -> tuple[BacktestObservation, ...]:
    observations = []
    for gw in range(1, gameweeks + 1):
        for p in range(players):
            predicted = 2.0 + p * 0.5
            observations.append(
                _observation(
                    gameweek=gw,
                    player_id=p,
                    fixture_id=100 * gw + p,
                    predicted_xpts=predicted,
                    actual_points=slope * predicted,
                )
            )
    return tuple(observations)


def test_walk_forward_calibration_respects_minimum_calibration_gameweeks():
    observations = _uniform_observations(gameweeks=8, players=10, slope=0.8)

    records = walk_forward_calibration(observations, minimum_calibration_gameweeks=5)

    # GW1-5 have fewer than 5 distinct prior gameweeks; GW6 is the first with
    # exactly 5 prior gameweeks (1-5).
    assert [record.gameweek for record in records] == [6, 7, 8]


def test_walk_forward_calibration_never_uses_current_or_future_gameweek_rows():
    # An extreme outlier placed only in GW7 must not affect GW6's fit (GW6's
    # prior pool is GW1-5, which never includes GW7) or GW7's own fit (which
    # must use GW1-6 as training, not include GW7's own outlier row).
    observations = list(_uniform_observations(gameweeks=8, players=10, slope=0.8))
    observations.append(
        _observation(gameweek=7, player_id=999, fixture_id=9999, predicted_xpts=1000.0, actual_points=1.0)
    )

    records = walk_forward_calibration(observations, minimum_calibration_gameweeks=5)
    by_gameweek = {record.gameweek: record for record in records}

    # GW6's training pool is GW1-5 (50 rows: 5 gameweeks x 10 players) --
    # completely unaffected by the GW7 outlier.
    assert by_gameweek[6].overall_fit.training_rows == 50
    assert by_gameweek[6].overall_fit.slope == pytest.approx(0.8)

    # GW7's training pool is GW1-6 (60 rows), which does not include GW7's
    # own outlier row (the outlier is in GW7's *evaluation* rows, not
    # training) -- so GW7's fitted slope must also still be exactly 0.8, not
    # distorted by its own gameweek's outlier.
    assert by_gameweek[7].overall_fit.training_rows == 60
    assert by_gameweek[7].overall_fit.slope == pytest.approx(0.8)
    # The outlier does appear in GW7's own evaluation rows, confirming it
    # was correctly excluded from training but not silently dropped.
    assert any(row.player_id == 999 for row in by_gameweek[7].overall_evaluation_rows)


def test_high_band_threshold_uses_only_prior_gameweeks():
    # GW1-5 (prior pool for GW6) use predicted_xpts in [2.0, 6.5]; GW6 itself
    # (evaluation only) uses a much higher range. If the threshold leaked
    # GW6's own distribution, it would be pulled far higher than the correct,
    # prior-only 75th percentile.
    observations = []
    for gw in range(1, 6):
        for p in range(10):
            predicted = 2.0 + p * 0.5  # 2.0 .. 6.5
            observations.append(
                _observation(
                    gameweek=gw, player_id=p, fixture_id=100 * gw + p,
                    predicted_xpts=predicted, actual_points=0.8 * predicted,
                )
            )
    # GW6: much higher predicted_xpts range, would corrupt the threshold if leaked.
    for p in range(10):
        predicted = 100.0 + p
        observations.append(
            _observation(
                gameweek=6, player_id=p, fixture_id=600 + p,
                predicted_xpts=predicted, actual_points=0.8 * predicted,
            )
        )

    records = walk_forward_calibration(observations, minimum_calibration_gameweeks=5)
    record_gw6 = next(r for r in records if r.gameweek == 6)

    # Correct: 75th percentile of GW1-5's predicted_xpts (2.0..6.5), not of
    # GW6's own much-higher range.
    assert record_gw6.high_band_threshold < 10.0


def test_high_band_fit_is_none_when_prior_pool_too_thin():
    # GW1-5 (the prior pool for GW6) have only 2 rows/gameweek at nearly
    # identical predicted_xpts values -- at a 99.9th percentile threshold,
    # fewer than 2 of the 10 prior rows land at/above it, forcing
    # high_band_fit to be None rather than fabricated from too few points.
    # GW6 itself carries data too, so it is a real eligible gameweek.
    observations = []
    for gw in range(1, 7):
        for p in range(2):
            predicted = 1.0 + p * 0.01  # nearly identical values
            observations.append(
                _observation(
                    gameweek=gw, player_id=p, fixture_id=100 * gw + p,
                    predicted_xpts=predicted, actual_points=predicted,
                )
            )

    records = walk_forward_calibration(
        observations, minimum_calibration_gameweeks=5, high_band_percentile=99.9
    )
    record = next(r for r in records if r.gameweek == 6)

    assert record.high_band_fit is None


def test_walk_forward_calibration_rejects_empty_observations():
    with pytest.raises(ValueError, match="at least one observation"):
        walk_forward_calibration(())


def test_walk_forward_calibration_rejects_invalid_minimum_gameweeks():
    observations = _uniform_observations(gameweeks=3, players=2, slope=1.0)
    with pytest.raises(ValueError, match="minimum_calibration_gameweeks"):
        walk_forward_calibration(observations, minimum_calibration_gameweeks=0)


def test_walk_forward_calibration_rejects_invalid_high_band_percentile():
    observations = _uniform_observations(gameweeks=3, players=2, slope=1.0)
    with pytest.raises(ValueError, match="high_band_percentile"):
        walk_forward_calibration(observations, high_band_percentile=100.0)


# ---------------------------------------------------------------------------
# pooled_walk_forward_slope
# ---------------------------------------------------------------------------


def test_pooled_walk_forward_slope_overall_pools_all_eligible_evaluation_rows():
    observations = _uniform_observations(gameweeks=8, players=10, slope=0.8)
    records = walk_forward_calibration(observations, minimum_calibration_gameweeks=5)

    pooled = pooled_walk_forward_slope(records, band="overall")

    expected_rows = sum(len(record.overall_evaluation_rows) for record in records)
    assert pooled["evaluation_rows"] == expected_rows
    assert pooled["eligible_gameweeks"] == len(records)
    assert pooled["slope"] == pytest.approx(0.8)


def test_pooled_walk_forward_slope_high_band_skips_records_without_high_band_fit():
    observations = _uniform_observations(gameweeks=8, players=10, slope=0.8)
    records = walk_forward_calibration(observations, minimum_calibration_gameweeks=5)

    pooled_high = pooled_walk_forward_slope(records, band="high")

    eligible_high_records = [r for r in records if r.high_band_fit is not None]
    expected_rows = sum(len(r.high_band_evaluation_rows) for r in eligible_high_records)
    assert pooled_high["evaluation_rows"] == expected_rows
    assert pooled_high["eligible_gameweeks"] == len(eligible_high_records)


def test_pooled_walk_forward_slope_rejects_empty_records():
    with pytest.raises(ValueError, match="at least one calibration record"):
        pooled_walk_forward_slope((), band="overall")


def test_pooled_walk_forward_slope_rejects_invalid_band():
    observations = _uniform_observations(gameweeks=8, players=10, slope=0.8)
    records = walk_forward_calibration(observations, minimum_calibration_gameweeks=5)

    with pytest.raises(ValueError, match="band"):
        pooled_walk_forward_slope(records, band="unknown")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# causal_walk_forward_predictions
# ---------------------------------------------------------------------------


def _regime_shift_observations(
    *, gameweeks: int, players: int, shift_gameweek: int, early_slope: float, late_slope: float
) -> tuple[BacktestObservation, ...]:
    """Observations whose true predicted->actual relationship changes mid-season.

    Rows before ``shift_gameweek`` follow ``actual = early_slope * predicted``;
    rows from ``shift_gameweek`` onward follow ``actual = late_slope *
    predicted``. This means no single fold's own prior-gameweeks-only fit can
    equal the fit pooled across *all* eligible gameweeks' evaluation rows
    (which mixes both regimes) -- exactly the condition needed to make the
    pooled-fit-applied-to-all-rows bug produce a different, wrong
    ``mae_calibrated`` than causal per-fold application.
    """
    observations = []
    for gw in range(1, gameweeks + 1):
        slope = early_slope if gw < shift_gameweek else late_slope
        for p in range(players):
            predicted = 2.0 + p * 0.5
            observations.append(
                _observation(
                    gameweek=gw,
                    player_id=p,
                    fixture_id=100 * gw + p,
                    predicted_xpts=predicted,
                    actual_points=slope * predicted,
                )
            )
    return tuple(observations)


def test_causal_predictions_differ_from_naively_applying_the_pooled_fit():
    # Regression test for the in-sample pooled-fit bug: applying ONE pooled
    # OLS fit (fit across every eligible gameweek's own evaluation rows) back
    # onto those same rows is a materially different -- and wrong -- MAE than
    # applying each gameweek's own prior-gameweeks-only fit. This test fails
    # under the old _band_summary wiring, which used pooled_walk_forward_slope's
    # fit to produce "calibrated" predictions instead of causal per-fold fits.
    observations = _regime_shift_observations(
        gameweeks=12, players=10, shift_gameweek=8, early_slope=0.5, late_slope=1.3
    )
    records = walk_forward_calibration(observations, minimum_calibration_gameweeks=5)

    rows, causal_predictions = causal_walk_forward_predictions(records, band="overall")

    # The (buggy) pooled-fit-applied-to-everything predictions, reproduced
    # exactly as the old _band_summary computed them.
    pooled = pooled_walk_forward_slope(records, band="overall")
    pooled_fit = OlsFit(
        slope=pooled["slope"],
        intercept=pooled["intercept"],
        training_rows=pooled["evaluation_rows"],
        training_gameweeks=pooled["eligible_gameweeks"],
    )
    pooled_applied_predictions = apply_calibration(pooled["rows"], pooled_fit)

    # Same row set/order (both draw from every eligible record's own
    # overall_evaluation_rows in record order), so a direct positional MAE
    # comparison is valid.
    assert rows == pooled["rows"]

    actual = [row.actual_points for row in rows]
    causal_mae = sum(abs(p - a) for p, a in zip(causal_predictions, actual, strict=True)) / len(rows)
    pooled_mae = sum(
        abs(p - a) for p, a in zip(pooled_applied_predictions, actual, strict=True)
    ) / len(rows)

    assert causal_mae != pytest.approx(pooled_mae)
    # The causal, genuinely out-of-sample MAE must be worse (or at least not
    # better) than the in-sample pooled-fit MAE: the pooled fit has an unfair
    # advantage from being derived from the very rows it is scored against.
    assert causal_mae > pooled_mae


def test_causal_predictions_overall_use_every_records_own_prior_only_fit():
    observations = _regime_shift_observations(
        gameweeks=12, players=10, shift_gameweek=8, early_slope=0.5, late_slope=1.3
    )
    records = walk_forward_calibration(observations, minimum_calibration_gameweeks=5)

    rows, causal_predictions = causal_walk_forward_predictions(records, band="overall")

    # Rebuild expected predictions by hand: each record's own overall_fit
    # applied to that record's own overall_evaluation_rows, concatenated in
    # record order.
    expected_rows: list[CalibrationRow] = []
    expected_predictions: list[float] = []
    for record in records:
        expected_rows.extend(record.overall_evaluation_rows)
        expected_predictions.extend(apply_calibration(record.overall_evaluation_rows, record.overall_fit))

    assert rows == tuple(expected_rows)
    assert causal_predictions == pytest.approx(tuple(expected_predictions))


def test_causal_predictions_current_or_future_outcomes_cannot_change_an_earlier_folds_prediction():
    # Build a baseline run, then perturb ONLY a later gameweek's actual_points
    # (an outcome that, chronologically, is not yet known at an earlier
    # fold's deadline) and confirm the earlier fold's causal calibrated
    # predictions are byte-identical before and after the perturbation.
    baseline_observations = list(
        _regime_shift_observations(
            gameweeks=12, players=10, shift_gameweek=8, early_slope=0.5, late_slope=1.3
        )
    )
    baseline_records = walk_forward_calibration(baseline_observations, minimum_calibration_gameweeks=5)
    baseline_rows, baseline_predictions = causal_walk_forward_predictions(baseline_records, band="overall")
    earliest_eligible_gameweek = baseline_records[0].gameweek
    baseline_early_predictions = {
        (row.player_id, row.fixture_id): pred
        for row, pred in zip(baseline_rows, baseline_predictions, strict=True)
        if row.gameweek == earliest_eligible_gameweek
    }
    assert baseline_early_predictions  # sanity: the earliest fold has rows

    # Perturb the actual_points of a gameweek strictly AFTER the earliest
    # eligible gameweek -- this could never have been known when the earliest
    # fold's fit was made.
    perturbed_observations = [
        _observation(
            gameweek=o.gameweek,
            player_id=o.player_id,
            fixture_id=o.fixture_id,
            predicted_xpts=o.predicted_xpts,
            actual_points=o.actual_points + 1000.0 if o.gameweek == 12 else o.actual_points,
        )
        for o in baseline_observations
    ]
    perturbed_records = walk_forward_calibration(perturbed_observations, minimum_calibration_gameweeks=5)
    perturbed_rows, perturbed_predictions = causal_walk_forward_predictions(perturbed_records, band="overall")
    perturbed_early_predictions = {
        (row.player_id, row.fixture_id): pred
        for row, pred in zip(perturbed_rows, perturbed_predictions, strict=True)
        if row.gameweek == earliest_eligible_gameweek
    }

    assert perturbed_early_predictions == pytest.approx(baseline_early_predictions)


def test_causal_predictions_high_band_uses_each_folds_own_prior_only_threshold_and_fit():
    observations = _regime_shift_observations(
        gameweeks=12, players=10, shift_gameweek=8, early_slope=0.5, late_slope=1.3
    )
    records = walk_forward_calibration(observations, minimum_calibration_gameweeks=5)

    rows, causal_predictions = causal_walk_forward_predictions(records, band="high")

    eligible_records = [r for r in records if r.high_band_fit is not None]
    expected_rows: list[CalibrationRow] = []
    expected_predictions: list[float] = []
    for record in eligible_records:
        expected_rows.extend(record.high_band_evaluation_rows)
        expected_predictions.extend(
            apply_calibration(record.high_band_evaluation_rows, record.high_band_fit)
        )

    assert rows == tuple(expected_rows)
    assert causal_predictions == pytest.approx(tuple(expected_predictions))
    # Every returned row must actually be at/above that row's OWN fold's
    # prior-only threshold (not a leaked full-season or other fold's
    # threshold).
    threshold_by_gameweek = {record.gameweek: record.high_band_threshold for record in eligible_records}
    assert all(row.predicted_xpts >= threshold_by_gameweek[row.gameweek] for row in rows)


def test_causal_predictions_high_band_skips_records_with_no_prior_high_band_fit():
    observations = _regime_shift_observations(
        gameweeks=12, players=10, shift_gameweek=8, early_slope=0.5, late_slope=1.3
    )
    records = walk_forward_calibration(observations, minimum_calibration_gameweeks=5)
    records_missing_high_band_fit = [r for r in records if r.high_band_fit is None]

    rows, _ = causal_walk_forward_predictions(records, band="high")

    covered_gameweeks = {row.gameweek for row in rows}
    assert not covered_gameweeks & {r.gameweek for r in records_missing_high_band_fit}


def test_causal_predictions_rejects_invalid_band():
    observations = _uniform_observations(gameweeks=8, players=10, slope=1.0)
    records = walk_forward_calibration(observations, minimum_calibration_gameweeks=5)

    with pytest.raises(ValueError, match="band"):
        causal_walk_forward_predictions(records, band="unknown")  # type: ignore[arg-type]


def test_causal_raw_actual_and_naive_arrays_share_identical_row_keys():
    # Simulates the script's _band_summary array construction: raw/actual
    # come directly from `rows`, calibrated predictions are positionally
    # aligned to `rows`, and a naive lookup dict keyed by
    # (player_id, fixture_id, gameweek) must cover every row with no gaps.
    observations = _regime_shift_observations(
        gameweeks=12, players=10, shift_gameweek=8, early_slope=0.5, late_slope=1.3
    )
    records = walk_forward_calibration(observations, minimum_calibration_gameweeks=5)

    rows, calibrated_predictions = causal_walk_forward_predictions(records, band="overall")
    assert len(rows) == len(calibrated_predictions)

    naive_by_key = {(row.player_id, row.fixture_id, row.gameweek): row.predicted_xpts * 0.9 for row in rows}
    raw_keys = {(row.player_id, row.fixture_id, row.gameweek) for row in rows}
    actual_keys = {(row.player_id, row.fixture_id, row.gameweek) for row in rows}
    calibrated_keys = {(row.player_id, row.fixture_id, row.gameweek) for row in rows}

    assert raw_keys == actual_keys == calibrated_keys == set(naive_by_key)
    # No duplicate keys within one band's causal row set.
    assert len(raw_keys) == len(rows)
