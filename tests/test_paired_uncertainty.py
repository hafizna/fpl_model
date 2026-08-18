from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from fpl_model.validation.backtest import BacktestObservation
from fpl_model.validation.paired_uncertainty import (
    DEFAULT_RESAMPLES,
    DEFAULT_SEED,
    BootstrapResult,
    PairedRow,
    _pooled_mae,
    _pooled_rmse,
    build_method_notes,
    build_paired_rows,
    cluster_bootstrap,
    estimate_paired_uncertainty,
    interpret_paired_verdict,
)

GW_DEADLINES = {
    1: datetime(2025, 8, 16, 12, 30, tzinfo=UTC),
    2: datetime(2025, 8, 23, 12, 30, tzinfo=UTC),
    3: datetime(2025, 8, 30, 12, 30, tzinfo=UTC),
    4: datetime(2025, 9, 13, 12, 30, tzinfo=UTC),
    5: datetime(2025, 9, 20, 12, 30, tzinfo=UTC),
    6: datetime(2025, 9, 27, 12, 30, tzinfo=UTC),
}


def _observation(
    *, player_id: int, fixture_id: int, gameweek: int, predicted_xpts: float, actual_points: float
) -> BacktestObservation:
    deadline = GW_DEADLINES[gameweek]
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
# build_paired_rows
# ---------------------------------------------------------------------------


def test_build_paired_rows_golden_case():
    model = (
        _observation(player_id=1, fixture_id=100, gameweek=1, predicted_xpts=5.0, actual_points=2.0),
        _observation(player_id=2, fixture_id=101, gameweek=1, predicted_xpts=3.0, actual_points=4.0),
    )
    naive = (
        _observation(player_id=1, fixture_id=100, gameweek=1, predicted_xpts=1.0, actual_points=2.0),
        _observation(player_id=2, fixture_id=101, gameweek=1, predicted_xpts=6.0, actual_points=4.0),
    )

    rows = build_paired_rows(model, naive)

    assert len(rows) == 2
    by_player = {row.player_id: row for row in rows}
    assert by_player[1].model_error == pytest.approx(3.0)  # 5.0 - 2.0
    assert by_player[1].naive_error == pytest.approx(-1.0)  # 1.0 - 2.0
    assert by_player[2].model_error == pytest.approx(-1.0)  # 3.0 - 4.0
    assert by_player[2].naive_error == pytest.approx(2.0)  # 6.0 - 4.0


def test_build_paired_rows_rejects_key_missing_from_naive():
    model = (
        _observation(player_id=1, fixture_id=100, gameweek=1, predicted_xpts=5.0, actual_points=2.0),
    )
    naive: tuple[BacktestObservation, ...] = ()

    with pytest.raises(ValueError, match="key sets differ"):
        build_paired_rows(model, naive)


def test_build_paired_rows_rejects_duplicate_key_within_one_side():
    model = (
        _observation(player_id=1, fixture_id=100, gameweek=1, predicted_xpts=5.0, actual_points=2.0),
        _observation(player_id=1, fixture_id=100, gameweek=1, predicted_xpts=6.0, actual_points=2.0),
    )
    naive = (
        _observation(player_id=1, fixture_id=100, gameweek=1, predicted_xpts=1.0, actual_points=2.0),
    )

    with pytest.raises(ValueError, match="duplicate key"):
        build_paired_rows(model, naive)


def test_build_paired_rows_rejects_mismatched_actual_points():
    model = (
        _observation(player_id=1, fixture_id=100, gameweek=1, predicted_xpts=5.0, actual_points=2.0),
    )
    naive = (
        _observation(player_id=1, fixture_id=100, gameweek=1, predicted_xpts=1.0, actual_points=9.0),
    )

    with pytest.raises(ValueError, match="disagree on actual_points"):
        build_paired_rows(model, naive)


# ---------------------------------------------------------------------------
# _pooled_mae / _pooled_rmse golden values
# ---------------------------------------------------------------------------


def test_pooled_mae_and_rmse_golden_values():
    rows = (
        PairedRow(player_id=1, fixture_id=100, gameweek=1, model_error=1.0, naive_error=2.0),
        PairedRow(player_id=2, fixture_id=101, gameweek=1, model_error=-3.0, naive_error=4.0),
    )

    model_mae, naive_mae = _pooled_mae(rows)
    model_rmse, naive_rmse = _pooled_rmse(rows)

    assert model_mae == pytest.approx((1.0 + 3.0) / 2)
    assert naive_mae == pytest.approx((2.0 + 4.0) / 2)
    assert model_rmse == pytest.approx(((1.0**2 + 3.0**2) / 2) ** 0.5)
    assert naive_rmse == pytest.approx(((2.0**2 + 4.0**2) / 2) ** 0.5)


def test_pooled_mae_rejects_empty_rows():
    with pytest.raises(ValueError, match="at least one"):
        _pooled_mae(())


# ---------------------------------------------------------------------------
# cluster_bootstrap: synthetic significance cases
# ---------------------------------------------------------------------------


def _synthetic_rows(
    *, gameweek_margins: dict[int, float], rows_per_gameweek: int = 20
) -> tuple[PairedRow, ...]:
    """Build rows where, within gameweek G, model_error is always exactly
    ``gameweek_margins[G]`` closer to zero than naive_error -- i.e. every row
    in a gameweek contributes exactly ``gameweek_margins[G]`` to the per-row
    MAE improvement (``|naive_error| - |model_error|``), so the true
    per-gameweek-margin structure is fully known by construction. Both errors
    are kept non-negative (``naive_error`` fixed at a magnitude comfortably
    larger than the largest margin used) so ``|x| == x`` throughout and the
    margin arithmetic stays exactly linear -- avoids the absolute-value
    nonlinearity that would otherwise distort the intended margin.
    """
    naive_error = 100.0  # fixed magnitude, large relative to any margin used below
    rows = []
    for gameweek, margin in gameweek_margins.items():
        for i in range(rows_per_gameweek):
            model_error = naive_error - margin  # |naive|-|model| == margin exactly
            rows.append(
                PairedRow(
                    player_id=i,
                    fixture_id=1000 * gameweek + i,
                    gameweek=gameweek,
                    model_error=model_error,
                    naive_error=naive_error,
                )
            )
    return tuple(rows)


def test_cluster_bootstrap_ci_excludes_zero_when_model_is_consistently_better():
    # Model's |error| is always 1.5 smaller than naive's, across every
    # gameweek and every row -- an unambiguous, noise-free advantage.
    rows = _synthetic_rows(gameweek_margins={1: 1.5, 2: 1.5, 3: 1.5, 4: 1.5, 5: 1.5, 6: 1.5})

    mae_result, _ = cluster_bootstrap(
        rows, cluster_key=lambda row: row.gameweek, cluster_label="gameweek", resamples=2000, seed=1
    )

    assert mae_result.point_estimate == pytest.approx(1.5)
    assert mae_result.ci_low > 0.0
    assert mae_result.ci_high > 0.0
    assert mae_result.p_improvement_positive == pytest.approx(1.0)


def test_cluster_bootstrap_ci_excludes_zero_when_model_is_consistently_worse():
    # Model's |error| is always 1.5 LARGER than naive's (negative margin).
    rows = _synthetic_rows(
        gameweek_margins={1: -1.5, 2: -1.5, 3: -1.5, 4: -1.5, 5: -1.5, 6: -1.5}
    )

    mae_result, _ = cluster_bootstrap(
        rows, cluster_key=lambda row: row.gameweek, cluster_label="gameweek", resamples=2000, seed=1
    )

    assert mae_result.point_estimate == pytest.approx(-1.5)
    assert mae_result.ci_low < 0.0
    assert mae_result.ci_high < 0.0
    assert mae_result.p_improvement_positive == pytest.approx(0.0)


def test_cluster_bootstrap_ci_includes_zero_when_gameweeks_disagree():
    # Half the gameweeks strongly favour the model, half strongly favour
    # naive, by equal and opposite margins -- the true pooled effect is zero
    # and which "half" gets oversampled varies resample to resample, so the
    # CI must straddle zero.
    rows = _synthetic_rows(
        gameweek_margins={1: 5.0, 2: -5.0, 3: 5.0, 4: -5.0, 5: 5.0, 6: -5.0}
    )

    mae_result, _ = cluster_bootstrap(
        rows, cluster_key=lambda row: row.gameweek, cluster_label="gameweek", resamples=5000, seed=1
    )

    assert mae_result.ci_low < 0.0 < mae_result.ci_high


def test_cluster_bootstrap_is_deterministic_for_a_fixed_seed():
    rows = _synthetic_rows(gameweek_margins={1: 1.0, 2: -0.5, 3: 2.0, 4: 0.0, 5: 0.5, 6: -1.0})

    first_mae, first_rmse = cluster_bootstrap(
        rows, cluster_key=lambda row: row.gameweek, cluster_label="gameweek", resamples=500, seed=7
    )
    second_mae, second_rmse = cluster_bootstrap(
        rows, cluster_key=lambda row: row.gameweek, cluster_label="gameweek", resamples=500, seed=7
    )

    assert first_mae == second_mae
    assert first_rmse == second_rmse


def test_cluster_bootstrap_different_seeds_can_differ():
    rows = _synthetic_rows(
        gameweek_margins={1: 5.0, 2: -5.0, 3: 5.0, 4: -5.0, 5: 5.0, 6: -5.0}
    )

    result_seed_1, _ = cluster_bootstrap(
        rows, cluster_key=lambda row: row.gameweek, cluster_label="gameweek", resamples=200, seed=1
    )
    result_seed_2, _ = cluster_bootstrap(
        rows, cluster_key=lambda row: row.gameweek, cluster_label="gameweek", resamples=200, seed=2
    )

    # Point estimate (computed on the true, unresampled pool) is seed-independent...
    assert result_seed_1.point_estimate == result_seed_2.point_estimate
    # ...but the resampled CI bounds are not guaranteed identical across seeds.
    assert (result_seed_1.ci_low, result_seed_1.ci_high) != (
        result_seed_2.ci_low,
        result_seed_2.ci_high,
    )


def test_cluster_bootstrap_retains_whole_clusters_not_individual_rows():
    # Two equal-sized gameweeks with sharply different, internally-uniform
    # per-row margins (10.0 vs -10.0). With only 2 clusters and 2 draws per
    # resample, the pooled MAE improvement can only take one of 3 exact
    # values, determined entirely by which whole gameweek(s) were drawn:
    # both draws = gw1 -> 10.0; both draws = gw2 -> -10.0; one of each -> 0.0.
    # A row-level (non-cluster) bootstrap could instead produce almost any
    # value in between, so observing only these 3 exact values across many
    # resamples proves clusters are resampled intact, not shuffled per-row.
    rows = _synthetic_rows(gameweek_margins={1: 10.0, 2: -10.0}, rows_per_gameweek=20)
    clusters = {1: [row for row in rows if row.gameweek == 1], 2: [row for row in rows if row.gameweek == 2]}

    # Independently enumerate every achievable pooled value for 2 draws from
    # 2 clusters (ground truth, computed without calling cluster_bootstrap).
    expected_achievable_values = set()
    for first, second in itertools.product((1, 2), repeat=2):
        pooled = clusters[first] + clusters[second]
        model_mae, naive_mae = _pooled_mae(pooled)
        expected_achievable_values.add(round(naive_mae - model_mae, 6))
    assert expected_achievable_values == {-10.0, 0.0, 10.0}

    mae_result, _ = cluster_bootstrap(
        rows, cluster_key=lambda row: row.gameweek, cluster_label="gameweek", resamples=500, seed=3
    )

    # The bootstrap's own CI bounds must fall within the achievable set
    # (they are percentiles of a distribution supported only on {-10, 0, 10}).
    assert mae_result.ci_low in expected_achievable_values
    assert mae_result.ci_high in expected_achievable_values


# ---------------------------------------------------------------------------
# estimate_paired_uncertainty: end-to-end wiring
# ---------------------------------------------------------------------------


def test_estimate_paired_uncertainty_end_to_end():
    model = tuple(
        _observation(
            player_id=i, fixture_id=100 + i, gameweek=gw, predicted_xpts=3.0, actual_points=2.0
        )
        for gw in (1, 2, 3)
        for i in range(5)
    )
    naive = tuple(
        _observation(
            player_id=i, fixture_id=100 + i, gameweek=gw, predicted_xpts=4.0, actual_points=2.0
        )
        for gw in (1, 2, 3)
        for i in range(5)
    )

    result = estimate_paired_uncertainty(model, naive, resamples=200, seed=1)

    assert result.paired_rows == 15
    assert result.clusters == 3
    assert result.mae_bootstrap.point_estimate == pytest.approx(1.0)  # |2|-|1| naive-model
    assert isinstance(result.mae_bootstrap, BootstrapResult)
    assert result.fixture_cluster_mae_bootstrap is not None
    assert result.fixture_cluster_mae_bootstrap.cluster_unit == "fixture_id"


def test_estimate_paired_uncertainty_can_skip_fixture_sensitivity():
    model = tuple(
        _observation(
            player_id=i, fixture_id=100 + i, gameweek=1, predicted_xpts=3.0, actual_points=2.0
        )
        for i in range(5)
    )
    naive = tuple(
        _observation(
            player_id=i, fixture_id=100 + i, gameweek=1, predicted_xpts=4.0, actual_points=2.0
        )
        for i in range(5)
    )

    result = estimate_paired_uncertainty(
        model, naive, resamples=50, seed=1, include_fixture_sensitivity=False
    )

    assert result.fixture_cluster_mae_bootstrap is None
    assert result.fixture_cluster_rmse_bootstrap is None


# ---------------------------------------------------------------------------
# interpret_paired_verdict: direction-aware classification
# ---------------------------------------------------------------------------


def _bootstrap_result(*, ci_low: float, ci_high: float, point_estimate: float | None = None) -> BootstrapResult:
    return BootstrapResult(
        point_estimate=point_estimate if point_estimate is not None else (ci_low + ci_high) / 2,
        ci_low=ci_low,
        ci_high=ci_high,
        p_improvement_positive=0.5,
        resamples=100,
        seed=1,
        cluster_unit="gameweek",
    )


def test_interpret_paired_verdict_advantage_when_both_ci_low_positive():
    mae = _bootstrap_result(ci_low=0.01, ci_high=0.03)
    rmse = _bootstrap_result(ci_low=0.02, ci_high=0.09)

    state, sentence = interpret_paired_verdict(mae, rmse)

    assert state == "advantage"
    assert "advantage" in sentence.lower()
    assert "worse" not in sentence.lower()


def test_interpret_paired_verdict_disadvantage_when_both_ci_high_negative():
    # Both intervals lie entirely below zero -- naive beats the model. The
    # old logic ("any CI excluding zero => advantage") would have wrongly
    # reported "advantage" here since both intervals do exclude zero.
    mae = _bootstrap_result(ci_low=-0.05, ci_high=-0.01)
    rmse = _bootstrap_result(ci_low=-0.09, ci_high=-0.02)

    state, sentence = interpret_paired_verdict(mae, rmse)

    assert state == "disadvantage"
    assert "worse" in sentence.lower()


def test_interpret_paired_verdict_mixed_when_one_ci_straddles_zero():
    mae = _bootstrap_result(ci_low=-0.01, ci_high=0.03)  # straddles zero
    rmse = _bootstrap_result(ci_low=0.02, ci_high=0.09)  # entirely positive

    state, sentence = interpret_paired_verdict(mae, rmse)

    assert state == "mixed"
    assert "unconfirmed" in sentence.lower()


def test_interpret_paired_verdict_mixed_when_metrics_disagree_on_direction():
    # MAE entirely positive, RMSE entirely negative -- the two metrics
    # disagree on direction, which must not collapse to either "advantage"
    # or "disadvantage".
    mae = _bootstrap_result(ci_low=0.01, ci_high=0.03)
    rmse = _bootstrap_result(ci_low=-0.09, ci_high=-0.02)

    state, sentence = interpret_paired_verdict(mae, rmse)

    assert state == "mixed"


def test_interpret_paired_verdict_mixed_when_both_ci_straddle_zero():
    mae = _bootstrap_result(ci_low=-0.02, ci_high=0.02)
    rmse = _bootstrap_result(ci_low=-0.03, ci_high=0.05)

    state, sentence = interpret_paired_verdict(mae, rmse)

    assert state == "mixed"


# ---------------------------------------------------------------------------
# build_method_notes: runtime metadata must reflect actual CLI arguments
# ---------------------------------------------------------------------------


def test_build_method_notes_reflects_non_default_resamples_and_seed():
    non_default_resamples = 500
    non_default_seed = 12345
    assert non_default_resamples != DEFAULT_RESAMPLES
    assert non_default_seed != DEFAULT_SEED

    notes = build_method_notes(resamples=non_default_resamples, seed=non_default_seed)
    bootstrap_note = notes[-1]

    assert str(non_default_resamples) in bootstrap_note
    assert str(non_default_seed) in bootstrap_note
    # The stale DEFAULT_* values must not be presented as this run's own
    # parameters (they may still appear once, labelled explicitly as
    # "defaults are ...", which is why this checks for the wrong values
    # NOT appearing where the actual resamples/seed values are expected).
    assert f"of {DEFAULT_RESAMPLES} resamples" not in bootstrap_note
    assert f"seed {DEFAULT_SEED} for this run" not in bootstrap_note


def test_build_method_notes_reflects_default_resamples_and_seed():
    notes = build_method_notes(resamples=DEFAULT_RESAMPLES, seed=DEFAULT_SEED)
    bootstrap_note = notes[-1]

    assert f"of {DEFAULT_RESAMPLES} resamples" in bootstrap_note
    assert f"seed {DEFAULT_SEED} for this run" in bootstrap_note


def test_build_method_notes_returns_four_sentences():
    notes = build_method_notes(resamples=100, seed=1)

    assert len(notes) == 4
    assert all(isinstance(note, str) and note for note in notes)
