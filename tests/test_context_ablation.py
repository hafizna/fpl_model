from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fpl_model.validation.backtest import BacktestObservation
from fpl_model.validation.context_ablation import evaluate_context_ablations


def _observations(*, error: float, gameweeks: int = 6) -> tuple[BacktestObservation, ...]:
    rows = []
    for gameweek in range(1, gameweeks + 1):
        deadline = datetime(2025, 8, 1, 12, tzinfo=UTC) + timedelta(days=7 * gameweek)
        kickoff = deadline + timedelta(hours=2)
        for player_id in range(3):
            rows.append(
                BacktestObservation(
                    season="2025-26",
                    gameweek=gameweek,
                    deadline=deadline,
                    fixture_kickoff=kickoff,
                    feature_cutoff=deadline,
                    outcome_available_at=kickoff + timedelta(hours=3),
                    player_id=player_id,
                    fixture_id=gameweek * 100 + player_id,
                    predicted_xpts=5.0 + error,
                    actual_points=5.0,
                )
            )
    return tuple(rows)


def test_context_ablation_supports_consistent_out_of_sample_improvement():
    result = evaluate_context_ablations(
        _observations(error=0.5),
        {"congestion": _observations(error=2.0)},
        resamples=300,
        seed=7,
    )[0]

    assert result.layer == "congestion"
    assert result.gameweeks == 6
    assert result.supported is True
    assert result.verdict == "supported"
    assert result.uncertainty.mae_bootstrap.ci_low > 0.0


def test_context_ablation_refuses_a_short_sample_even_when_effect_is_consistent():
    result = evaluate_context_ablations(
        _observations(error=0.5, gameweeks=3),
        {"readiness": _observations(error=2.0, gameweeks=3)},
        minimum_gameweeks=6,
        resamples=100,
    )[0]

    assert result.supported is False
    assert result.verdict == "insufficient_gameweeks"
