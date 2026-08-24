"""Walk-forward backtesting, calibration, and ablation utilities."""

from fpl_model.validation.backtest import (
    BacktestMetrics,
    BacktestObservation,
    WalkForwardFold,
    score_predictions,
    walk_forward_folds,
)
from fpl_model.validation.context_ablation import (
    ContextAblationResult,
    evaluate_context_ablations,
)
from fpl_model.validation.historical import (
    build_cross_season_player_bridge,
    infer_gameweek_deadlines,
    materialize_expanding_player_mean_baseline,
)
from fpl_model.validation.projection_uncertainty import (
    apply_uncertainty_artifact,
    build_residual_rows,
    evaluate_intervals,
    store_uncertainty_artifact,
    walk_forward_intervals,
)

__all__ = [
    "BacktestMetrics",
    "BacktestObservation",
    "ContextAblationResult",
    "WalkForwardFold",
    "build_cross_season_player_bridge",
    "build_residual_rows",
    "evaluate_context_ablations",
    "evaluate_intervals",
    "infer_gameweek_deadlines",
    "materialize_expanding_player_mean_baseline",
    "score_predictions",
    "store_uncertainty_artifact",
    "apply_uncertainty_artifact",
    "walk_forward_folds",
    "walk_forward_intervals",
]
