from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from fpl_model.model.appearance import (
    ConditionalAppearanceScenario,
    MinutesScenario,
    SeasonAppearanceHistory,
    benchwarmers_appearance_probability,
    benchwarmers_sixty_minute_given_start_probability,
    benchwarmers_start_probability,
    project_appearance,
    project_benchwarmers_appearance,
    project_conditional_appearance,
)

REFERENCE_PATH = (
    Path(__file__).parents[1]
    / "docs"
    / "research"
    / "benchwarmers_appearance_reference.json"
)
REFERENCE_CASES = {
    case["label"]: case
    for case in json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))["golden_cases"]
}


@pytest.mark.parametrize(
    (
        "scenarios",
        "expected_start_probability",
        "expected_appearance_probability",
        "expected_sixty_probability",
        "expected_minutes",
        "expected_xpts",
    ),
    [
        (
            [MinutesScenario(1.0, 90.0, started=True, label="starts and completes")],
            1.0,
            1.0,
            1.0,
            90.0,
            2.0,
        ),
        (
            [MinutesScenario(1.0, 20.0, started=False, label="substitute cameo")],
            0.0,
            1.0,
            0.0,
            20.0,
            1.0,
        ),
        (
            [MinutesScenario(1.0, 59.0, started=True, label="below threshold")],
            1.0,
            1.0,
            0.0,
            59.0,
            1.0,
        ),
        (
            [MinutesScenario(1.0, 60.0, started=True, label="at threshold")],
            1.0,
            1.0,
            1.0,
            60.0,
            2.0,
        ),
        (
            [MinutesScenario(1.0, 0.0, started=False, label="unused")],
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
        ),
        (
            [
                MinutesScenario(0.55, 72.0, started=True, label="starts"),
                MinutesScenario(0.25, 18.0, started=False, label="cameo"),
                MinutesScenario(0.20, 0.0, started=False, label="unused"),
            ],
            0.55,
            0.80,
            0.55,
            44.1,
            1.35,
        ),
    ],
)
def test_appearance_golden_scenarios(
    scenarios: list[MinutesScenario],
    expected_start_probability: float,
    expected_appearance_probability: float,
    expected_sixty_probability: float,
    expected_minutes: float,
    expected_xpts: float,
):
    result = project_appearance(scenarios)

    assert result.start_probability == pytest.approx(expected_start_probability)
    assert result.appearance_probability == pytest.approx(expected_appearance_probability)
    assert result.sixty_minute_probability == pytest.approx(expected_sixty_probability)
    assert result.expected_minutes == pytest.approx(expected_minutes)
    assert result.appearance_xpts == pytest.approx(expected_appearance_probability)
    assert result.sixty_minute_xpts == pytest.approx(expected_sixty_probability)
    assert result.total_xpts == pytest.approx(expected_xpts)


def test_substitute_probability_is_reported_separately():
    result = project_appearance(
        [
            MinutesScenario(0.60, 75.0, started=True),
            MinutesScenario(0.30, 15.0, started=False),
            MinutesScenario(0.10, 0.0, started=False),
        ]
    )

    assert result.substitute_appearance_probability == pytest.approx(0.30)


def test_reviewed_conditional_scenario_preserves_availability_as_causal_input():
    result = project_conditional_appearance(
        ConditionalAppearanceScenario(
            start_probability_if_available=0.6,
            substitute_probability_if_available=0.25,
            sixty_probability_given_start=0.8,
            minutes_per_start=75.0,
            minutes_per_substitute=20.0,
        ),
        availability_probability=0.5,
    )

    assert result.start_probability == pytest.approx(0.3)
    assert result.substitute_appearance_probability == pytest.approx(0.125)
    assert result.appearance_probability == pytest.approx(0.425)
    assert result.sixty_minute_probability == pytest.approx(0.24)
    assert result.expected_minutes == pytest.approx(25.0)
    assert result.total_xpts == pytest.approx(0.665)


def test_conditional_scenario_rejects_incoherent_probabilities():
    with pytest.raises(ValueError, match="cannot exceed 1"):
        ConditionalAppearanceScenario(
            start_probability_if_available=0.8,
            substitute_probability_if_available=0.3,
            sixty_probability_given_start=0.8,
            minutes_per_start=75.0,
            minutes_per_substitute=20.0,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"probability": -0.1, "minutes": 0.0, "started": False},
        {"probability": 1.1, "minutes": 0.0, "started": False},
        {"probability": math.nan, "minutes": 0.0, "started": False},
        {"probability": 1.0, "minutes": -1.0, "started": False},
        {"probability": 1.0, "minutes": 91.0, "started": True},
        {"probability": 1.0, "minutes": math.inf, "started": True},
        {"probability": 1.0, "minutes": 0.0, "started": True},
    ],
)
def test_minutes_scenario_rejects_invalid_values(kwargs: dict[str, float | bool]):
    with pytest.raises(ValueError):
        MinutesScenario(**kwargs)  # type: ignore[arg-type]


def test_projection_requires_a_complete_probability_distribution():
    with pytest.raises(ValueError, match="sum to 1"):
        project_appearance(
            [
                MinutesScenario(0.70, 70.0, started=True),
                MinutesScenario(0.20, 10.0, started=False),
            ]
        )


def test_projection_rejects_empty_scenarios():
    with pytest.raises(ValueError, match="at least one"):
        project_appearance([])


@pytest.mark.parametrize("tolerance", [-1.0, math.nan, math.inf])
def test_projection_rejects_invalid_probability_tolerance(tolerance: float):
    with pytest.raises(ValueError, match="probability_tolerance"):
        project_appearance(
            [MinutesScenario(1.0, 0.0, started=False)],
            probability_tolerance=tolerance,
        )


def test_projection_accepts_small_floating_point_sum_error():
    result = project_appearance(
        [
            MinutesScenario(0.1, 90.0, started=True),
            MinutesScenario(0.2, 20.0, started=False),
            MinutesScenario(0.7, 0.0, started=False),
        ]
    )

    assert result.appearance_probability == pytest.approx(0.3)


@pytest.mark.parametrize(
    ("label", "history", "expected_start_probability"),
    [
        (
            "certain_starter",
            SeasonAppearanceHistory(37, 0, 0, minutes_per_start=90.0),
            1.0,
        ),
        (
            "rotation_prone_starter",
            SeasonAppearanceHistory(
                19,
                18,
                1,
                minutes_per_start=79.0,
                minutes_per_substitute=26.0,
            ),
            0.5,
        ),
        (
            "frequent_substitute",
            SeasonAppearanceHistory(
                1,
                25,
                10,
                minutes_per_start=60.0,
                minutes_per_substitute=11.0,
            ),
            1 / 36,
        ),
        (
            "likely_non_appearance",
            SeasonAppearanceHistory(0, 0, 46),
            0.0,
        ),
    ],
)
def test_benchwarmers_reference_probabilities_match_extracted_golden_cases(
    label: str,
    history: SeasonAppearanceHistory,
    expected_start_probability: float,
):
    reference = REFERENCE_CASES[label]["expected_outputs"]

    assert benchwarmers_appearance_probability(history) == pytest.approx(
        reference["appearance_probability_MODEL_1"]
    )
    assert benchwarmers_start_probability(history) == pytest.approx(
        expected_start_probability
    )
    assert benchwarmers_sixty_minute_given_start_probability(
        history
    ) == pytest.approx(reference["sixty_minute_probability_MODEL_2"])


@pytest.mark.parametrize(
    ("label", "history", "expected_start_probability"),
    [
        (
            "certain_starter",
            SeasonAppearanceHistory(37, 0, 0, minutes_per_start=90.0),
            0.99,
        ),
        (
            "rotation_prone_starter",
            SeasonAppearanceHistory(
                19,
                18,
                1,
                minutes_per_start=79.0,
                minutes_per_substitute=26.0,
            ),
            0.5,
        ),
        (
            "frequent_substitute",
            SeasonAppearanceHistory(
                1,
                25,
                10,
                minutes_per_start=60.0,
                minutes_per_substitute=11.0,
            ),
            1 / 36,
        ),
        (
            "likely_non_appearance",
            SeasonAppearanceHistory(0, 0, 46),
            0.0,
        ),
    ],
)
def test_benchwarmers_projection_converts_conditional_sixty_probability(
    label: str,
    history: SeasonAppearanceHistory,
    expected_start_probability: float,
):
    reference = REFERENCE_CASES[label]["expected_outputs"]
    current_history = SeasonAppearanceHistory(0, 0, 0)

    result = project_benchwarmers_appearance(history, current_history)

    expected_appearance = reference["appearance_probability_MODEL_1"]
    expected_conditional_sixty = reference["sixty_minute_probability_MODEL_2"]
    assert result.start_probability == pytest.approx(expected_start_probability)
    assert result.substitute_appearance_probability == pytest.approx(
        expected_appearance - expected_start_probability
    )
    assert result.appearance_probability == pytest.approx(expected_appearance)
    assert result.sixty_minute_probability == pytest.approx(
        expected_start_probability * expected_conditional_sixty
    )
    assert result.total_xpts == pytest.approx(
        expected_appearance + expected_start_probability * expected_conditional_sixty
    )


def test_manual_start_override_is_not_used_as_the_workbook_t1_point_boost():
    saka = REFERENCE_CASES["manual_start_override_example"]
    previous_history = SeasonAppearanceHistory(25, 6, 1)

    result = project_benchwarmers_appearance(
        previous_history,
        SeasonAppearanceHistory(0, 0, 0),
        manual_start_probability=0.6,
    )

    expected_appearance = saka["cached_intermediate_values"]["MODEL!CU13 '1'"]
    workbook_t1 = saka["cached_intermediate_values"]["PPts!BE13 T1"]
    assert result.start_probability == pytest.approx(0.6)
    assert result.appearance_xpts == pytest.approx(expected_appearance)
    assert result.appearance_xpts != pytest.approx(workbook_t1)


def test_certain_starter_is_capped_at_total_appearance_probability():
    result = project_benchwarmers_appearance(
        SeasonAppearanceHistory(37, 0, 0, minutes_per_start=90.0),
        SeasonAppearanceHistory(0, 0, 0),
    )

    assert result.appearance_probability == pytest.approx(0.99)
    assert result.start_probability == pytest.approx(0.99)
    assert result.substitute_appearance_probability == pytest.approx(0.0)


def test_zero_history_floor_produces_a_small_cameo_minutes_prior():
    result = project_benchwarmers_appearance(
        SeasonAppearanceHistory(0, 0, 46),
        SeasonAppearanceHistory(0, 0, 0),
    )

    assert result.appearance_probability == pytest.approx(0.01)
    assert result.expected_minutes == pytest.approx(0.01 * 18.0)
    assert result.total_xpts == pytest.approx(0.01)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"starts": -1, "substitute_appearances": 0, "unused_substitute": 0},
        {"starts": 0.5, "substitute_appearances": 0, "unused_substitute": 0},
        {"starts": 0, "substitute_appearances": True, "unused_substitute": 0},
        {
            "starts": 0,
            "substitute_appearances": 0,
            "unused_substitute": 0,
            "minutes_per_start": math.nan,
        },
        {
            "starts": 0,
            "substitute_appearances": 0,
            "unused_substitute": 0,
            "minutes_per_substitute": 91.0,
        },
    ],
)
def test_season_history_rejects_invalid_values(kwargs: dict[str, int | float | bool]):
    with pytest.raises(ValueError):
        SeasonAppearanceHistory(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("availability_probability", -0.1),
        ("manual_start_probability", 1.1),
        ("appearance_previous_weight", math.nan),
        ("sixty_previous_weight", 2.0),
    ],
)
def test_benchwarmers_projection_rejects_invalid_probabilities(
    argument: str,
    value: float,
):
    with pytest.raises(ValueError):
        project_benchwarmers_appearance(
            SeasonAppearanceHistory(1, 0, 0),
            SeasonAppearanceHistory(0, 0, 0),
            **{argument: value},
        )
