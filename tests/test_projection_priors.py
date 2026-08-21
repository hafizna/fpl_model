from __future__ import annotations

import pytest

from fpl_model.model.baseline_pipeline import (
    _derive_appearance_priors,
    _derive_rate_priors,
    _TeamStrength,
)


def _rate(position: str, expected_goals: float) -> tuple[object, ...]:
    return (
        position,
        900,
        10,
        0,
        2,
        0,
        2,
        140,
        900,
        expected_goals,
        1.0,
        450,
        expected_goals / 2,
        0.5,
        900,
        80,
        450,
        40,
        "[]",
    )


def _strength(*, promoted: bool) -> _TeamStrength:
    return _TeamStrength(1.4, 1.4, 1.4, 1.4, 1.4, 1.0, 1.0, promoted)


def test_rate_prior_requires_population_and_uses_position_price_median():
    players = [
        (index, 1000 + index, 1, "MID", 5.5) for index in range(1, 11)
    ]
    rates = {
        1000 + index: _rate("MID", float(index)) for index in range(1, 11)
    }

    priors = _derive_rate_priors(players, rates)

    prior = priors[("MID", "cheap")]
    assert prior.sample_size == 10
    assert prior.rate[9] == pytest.approx(5.5)
    assert _derive_rate_priors(players[:9], rates) == {}


def test_appearance_prior_prefers_explicit_promoted_missing_rate_cohort():
    players = [
        (index, 2000 + index, 1, "MID", 5.0) for index in range(1, 6)
    ]
    appearance_rows = {
        index: (
            2000 + index,
            1.0,
            0.6,
            0.2,
            0.8,
            0.5,
            51.0,
            0.8,
            0.5,
            1.3,
            "[]",
        )
        for index in range(1, 6)
    }
    histories = {2000 + index: (20, 10, 80.0, 15.0) for index in range(1, 6)}

    priors = _derive_appearance_priors(
        players=players,
        appearance_rows=appearance_rows,
        rates={},
        strengths={1: _strength(promoted=True)},
        histories=histories,
        overrides={},
    )

    prior = priors[("MID", "cheap", "promoted_no_previous_pl_rate")]
    assert prior.scope == "exact_cohort"
    assert prior.sample_size == 5
    assert prior.scenario.start_probability_if_available == pytest.approx(0.6)
    assert prior.scenario.substitute_probability_if_available == pytest.approx(0.2)
    assert prior.scenario.sixty_probability_given_start == pytest.approx(5 / 6)
    assert prior.scenario.minutes_per_start == pytest.approx(80.0)
