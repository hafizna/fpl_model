"""Apples-to-apples naive comparator for the walk-forward backtest.

Extracted from ``scripts/backtest_benchwarmers.py`` into an importable module
so both that script and ``scripts/diagnose_backtest_segments.py`` can share
one implementation without a cross-script import hack (``scripts/`` has no
``__init__.py`` and is not an installed package).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pandas as pd

from fpl_model.validation.backtest import BacktestObservation


def matched_naive_observations(
    observations: tuple[BacktestObservation, ...],
    gameweeks_frame: pd.DataFrame,
    *,
    outcome_delay: timedelta = timedelta(hours=3),
    cold_start_xpts: float = 0.0,
) -> tuple[BacktestObservation, ...]:
    """Predict the model's own scored rows with an expanding player-points mean.

    Evaluated on exactly the same ``(player_code, fixture_id, gameweek)`` set
    the 11-component model scored -- not the full player pool -- so its
    metrics are directly comparable to the model's own metrics. For each
    observation, the prediction is the mean ``total_points`` of that player's
    fixtures whose outcome was available (``kickoff_time + outcome_delay <=
    deadline``) strictly before that observation's own deadline -- the same
    causal rule ``materialize_expanding_player_mean_baseline`` uses, just
    restricted to the model's scored keys instead of the full population.
    Cold start (no causally-available prior points for that player) predicts
    ``cold_start_xpts``, matching that function's own documented default.
    """
    # Mirror materialize_benchwarmers_walk_forward_backtest's own duplicate
    # policy: drop exact byte-for-byte duplicate raw rows before building
    # history (observed in the live 2025-26 archive), so a raw-source
    # duplicate is not double-counted here while the model's own window
    # construction already dropped it once.
    deduplicated = gameweeks_frame.drop_duplicates()
    history = deduplicated.loc[:, ["code", "kickoff_time", "total_points"]].copy()
    history["code"] = pd.to_numeric(history["code"], errors="raise").astype(int)
    history["kickoff_time"] = pd.to_datetime(
        history["kickoff_time"], utc=True, errors="raise"
    )
    history["total_points"] = pd.to_numeric(history["total_points"], errors="raise")
    history["outcome_available_at"] = history["kickoff_time"] + outcome_delay

    matched: list[BacktestObservation] = []
    for deadline, group in _group_observations_by_deadline(observations):
        # <=, not <, matching every other causal gate in this codebase
        # (walk_forward_folds' own outcome_available_at <= deadline rule,
        # and the model's own kickoff_time + outcome_delay <= target_deadline
        # SQL predicate in player_rates_as_of/appearance_as_of/team_strength_as_of).
        available = history.loc[history["outcome_available_at"] <= deadline]
        player_means = available.groupby("code")["total_points"].mean().to_dict()
        for observation in group:
            predicted = player_means.get(observation.player_id, cold_start_xpts)
            matched.append(replace(observation, predicted_xpts=float(predicted)))
    return tuple(matched)


def _group_observations_by_deadline(
    observations: tuple[BacktestObservation, ...],
):
    """Yield ``(deadline, observations_at_that_deadline)`` for reuse of one
    causal history slice across every player scored at the same deadline,
    rather than re-filtering the whole frame per observation."""
    by_deadline: dict[object, list[BacktestObservation]] = {}
    for observation in observations:
        by_deadline.setdefault(observation.deadline, []).append(observation)
    for deadline in sorted(by_deadline):
        yield deadline, by_deadline[deadline]
