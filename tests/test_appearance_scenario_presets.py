from __future__ import annotations

import pytest

from fpl_model.context.appearance_scenario_presets import (
    APPEARANCE_SCENARIO_PRESETS,
    LIKELY_BENCH,
    LIKELY_STARTER,
    ROTATION_RISK,
    apply_appearance_scenario_preset,
)
from fpl_model.validation.role_state import LIKELY_STARTER_THRESHOLD, ROTATION_THRESHOLD


def test_every_named_preset_constructs_a_valid_scenario():
    for preset in APPEARANCE_SCENARIO_PRESETS:
        scenario = apply_appearance_scenario_preset(preset)
        assert 0.0 <= scenario.start_probability_if_available <= 1.0
        assert (
            scenario.start_probability_if_available
            + scenario.substitute_probability_if_available
            <= 1.0
        )


def test_likely_starter_preset_sits_above_the_role_state_threshold():
    scenario = apply_appearance_scenario_preset(LIKELY_STARTER)
    assert scenario.start_probability_if_available > LIKELY_STARTER_THRESHOLD


def test_likely_bench_preset_sits_below_the_role_state_threshold():
    scenario = apply_appearance_scenario_preset(LIKELY_BENCH)
    assert scenario.start_probability_if_available < ROTATION_THRESHOLD


def test_rotation_risk_preset_sits_inside_the_role_state_rotation_band():
    scenario = apply_appearance_scenario_preset(ROTATION_RISK)
    assert ROTATION_THRESHOLD < scenario.start_probability_if_available < LIKELY_STARTER_THRESHOLD


def test_unknown_preset_is_rejected():
    with pytest.raises(ValueError, match="unknown preset"):
        apply_appearance_scenario_preset("nailed_on")


def test_field_override_adjusts_only_the_named_field():
    base = apply_appearance_scenario_preset(LIKELY_STARTER)
    adjusted = apply_appearance_scenario_preset(LIKELY_STARTER, minutes_per_start=70.0)
    assert adjusted.minutes_per_start == 70.0
    assert adjusted.start_probability_if_available == base.start_probability_if_available
    assert adjusted.substitute_probability_if_available == (
        base.substitute_probability_if_available
    )


def test_override_still_goes_through_scenario_validation():
    with pytest.raises(ValueError, match="cannot exceed 1"):
        apply_appearance_scenario_preset(
            LIKELY_BENCH, start_probability_if_available=0.9, substitute_probability_if_available=0.9
        )


def test_preset_names_are_stable_and_exhaustive():
    assert APPEARANCE_SCENARIO_PRESETS == (LIKELY_STARTER, ROTATION_RISK, LIKELY_BENCH)
