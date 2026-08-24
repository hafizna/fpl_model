"""Paired, gameweek-clustered acceptance gate for Sprint 3 context layers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from fpl_model.validation.backtest import BacktestObservation
from fpl_model.validation.paired_uncertainty import (
    PairedUncertaintyResult,
    estimate_paired_uncertainty,
)


@dataclass(frozen=True, slots=True)
class ContextAblationResult:
    layer: str
    paired_rows: int
    gameweeks: int
    uncertainty: PairedUncertaintyResult
    supported: bool
    verdict: str


def evaluate_context_ablations(
    full_model: Sequence[BacktestObservation],
    without_layer: Mapping[str, Sequence[BacktestObservation]],
    *,
    minimum_gameweeks: int = 6,
    resamples: int = 10_000,
    seed: int = 42,
) -> tuple[ContextAblationResult, ...]:
    """Test each layer against the otherwise-identical model with that layer removed.

    A layer is supported only when both MAE and RMSE improvements have positive
    95% gameweek-clustered bootstrap lower bounds. This utility measures the
    evidence; it never activates an adjustment in production by itself.
    """
    if minimum_gameweeks < 2:
        raise ValueError("minimum_gameweeks must be at least 2")
    if not without_layer:
        raise ValueError("at least one context layer is required")
    results = []
    gameweeks = len({row.gameweek for row in full_model})
    for layer in sorted(without_layer):
        if not layer.strip():
            raise ValueError("context layer names must not be blank")
        uncertainty = estimate_paired_uncertainty(
            full_model,
            without_layer[layer],
            resamples=resamples,
            seed=seed,
            include_fixture_sensitivity=False,
        )
        enough_history = gameweeks >= minimum_gameweeks
        supported = (
            enough_history
            and uncertainty.mae_bootstrap.ci_low > 0.0
            and uncertainty.rmse_bootstrap.ci_low > 0.0
        )
        if not enough_history:
            verdict = "insufficient_gameweeks"
        elif supported:
            verdict = "supported"
        else:
            verdict = "not_supported"
        results.append(
            ContextAblationResult(
                layer=layer,
                paired_rows=uncertainty.paired_rows,
                gameweeks=gameweeks,
                uncertainty=uncertainty,
                supported=supported,
                verdict=verdict,
            )
        )
    return tuple(results)
