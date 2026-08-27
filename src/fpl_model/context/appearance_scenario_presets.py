"""Named presets for the reviewed appearance-scenario override boundary.

`context/minutes.py`'s `create_appearance_scenario_override` requires a
reviewer to supply five numeric fields
(`start_probability_if_available`/`substitute_probability_if_available`/
`sixty_probability_given_start`/`minutes_per_start`/`minutes_per_substitute`)
by hand. P0 (`README.md`'s "Production critical path") asks for simple named
presets -- `likely_starter`, `rotation_risk`, `likely_bench` -- on top of that
existing boundary, so a manager reviewing a squad update ("this player looks
like a rotation risk this week") does not have to invent five numbers from
scratch.

Preset probability bands deliberately reuse `validation/role_state.py`'s own
`ROTATION_THRESHOLD`/`LIKELY_STARTER_THRESHOLD` constants: a `likely_starter`
preset sits comfortably above `LIKELY_STARTER_THRESHOLD`, and `likely_bench`
sits comfortably below `ROTATION_THRESHOLD`, so a preset's name means the same
thing a manager already sees in a player's role state, not an independently
drifting definition. Preset minutes reuse `model/appearance.py`'s own
`DEFAULT_START_MINUTES`/`DEFAULT_SUBSTITUTE_MINUTES` for the same reason.

A preset is a STARTING POINT, not a substitute for the reviewer's own
judgement: `apply_appearance_scenario_preset` returns an ordinary
`ConditionalAppearanceScenario` the caller can further override field-by-field
before creating the reviewed override.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final

from fpl_model.model.appearance import (
    DEFAULT_START_MINUTES,
    DEFAULT_SUBSTITUTE_MINUTES,
    ConditionalAppearanceScenario,
)
from fpl_model.validation.role_state import LIKELY_STARTER_THRESHOLD, ROTATION_THRESHOLD

LIKELY_STARTER: Final = "likely_starter"
ROTATION_RISK: Final = "rotation_risk"
LIKELY_BENCH: Final = "likely_bench"

APPEARANCE_SCENARIO_PRESETS: Final = (LIKELY_STARTER, ROTATION_RISK, LIKELY_BENCH)

# Comfortably above LIKELY_STARTER_THRESHOLD (0.75): a manager choosing this
# preset is asserting the player is a near-certain starter, not merely on the
# right side of the role-state boundary.
_LIKELY_STARTER_START_PROBABILITY = 0.90
# The midpoint of role_state's own ROTATION band (0.30 to 0.75).
_ROTATION_RISK_START_PROBABILITY = (ROTATION_THRESHOLD + LIKELY_STARTER_THRESHOLD) / 2.0
# Comfortably below ROTATION_THRESHOLD (0.30): rarely starts, but a
# likely_bench preset still allows for late-game substitute involvement
# rather than assuming a total blank -- that total-blank case is
# role_scenario_sensitivity's own "blanks entirely" counterfactual, not this
# preset's job to assert as a reviewed fact.
_LIKELY_BENCH_START_PROBABILITY = 0.10

_PRESET_SCENARIOS: Final[dict[str, ConditionalAppearanceScenario]] = {
    LIKELY_STARTER: ConditionalAppearanceScenario(
        start_probability_if_available=_LIKELY_STARTER_START_PROBABILITY,
        substitute_probability_if_available=0.05,
        sixty_probability_given_start=0.90,
        minutes_per_start=DEFAULT_START_MINUTES,
        minutes_per_substitute=DEFAULT_SUBSTITUTE_MINUTES,
    ),
    ROTATION_RISK: ConditionalAppearanceScenario(
        start_probability_if_available=_ROTATION_RISK_START_PROBABILITY,
        substitute_probability_if_available=0.20,
        sixty_probability_given_start=0.70,
        minutes_per_start=DEFAULT_START_MINUTES,
        minutes_per_substitute=DEFAULT_SUBSTITUTE_MINUTES,
    ),
    LIKELY_BENCH: ConditionalAppearanceScenario(
        start_probability_if_available=_LIKELY_BENCH_START_PROBABILITY,
        substitute_probability_if_available=0.25,
        sixty_probability_given_start=0.40,
        minutes_per_start=DEFAULT_START_MINUTES,
        minutes_per_substitute=DEFAULT_SUBSTITUTE_MINUTES,
    ),
}


def apply_appearance_scenario_preset(
    preset: str, **overrides: float
) -> ConditionalAppearanceScenario:
    """Return one named preset's `ConditionalAppearanceScenario`, optionally adjusted.

    ``overrides`` accepts any of `ConditionalAppearanceScenario`'s own field
    names (e.g. ``minutes_per_start=70.0``) to adjust one field of a preset
    without hand-building the whole scenario -- the result still passes
    through the dataclass's own validation, so an incoherent override (for
    example ``start_probability_if_available`` pushed past 1.0 combined with
    ``substitute_probability_if_available``) is rejected the same way a
    fully hand-built scenario would be.
    """
    if preset not in _PRESET_SCENARIOS:
        raise ValueError(
            f"unknown preset: {preset!r} (expected one of {APPEARANCE_SCENARIO_PRESETS})"
        )
    scenario = _PRESET_SCENARIOS[preset]
    if not overrides:
        return scenario
    return replace(scenario, **overrides)
