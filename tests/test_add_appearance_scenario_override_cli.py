"""Coverage for scripts/add_appearance_scenario_override.py's --preset wiring.

scripts/ is not an importable package (no __init__.py, and tests/ is pytest's
only configured testpath), so the module is loaded directly from its file
path -- the same technique `python -m` uses -- rather than via a normal
`import`. This exercises the exact `_build_scenario`/`parse_args` logic the
CLI runs, without needing a full seeded database (that mapping is the only
new logic `--preset` added; `create_appearance_scenario_override` and
`store_appearance_scenario_override` already have their own test coverage).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "add_appearance_scenario_override.py"
)
_spec = importlib.util.spec_from_file_location("add_appearance_scenario_override_cli", _SCRIPT_PATH)
_module = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_module)


def _namespace(**overrides):
    arguments = {
        "preset": None,
        "start_if_available": None,
        "sub_if_available": None,
        "sixty_given_start": None,
        "minutes_per_start": None,
        "minutes_per_substitute": None,
    }
    arguments.update(overrides)
    return argparse.Namespace(**arguments)


def test_preset_alone_builds_the_named_preset_scenario():
    scenario = _module._build_scenario(_namespace(preset="likely_starter"))
    from fpl_model.context.appearance_scenario_presets import apply_appearance_scenario_preset

    assert scenario == apply_appearance_scenario_preset("likely_starter")


def test_preset_with_one_manual_field_overrides_only_that_field():
    scenario = _module._build_scenario(
        _namespace(preset="rotation_risk", minutes_per_start=75.0)
    )
    from fpl_model.context.appearance_scenario_presets import apply_appearance_scenario_preset

    base = apply_appearance_scenario_preset("rotation_risk")
    assert scenario.minutes_per_start == 75.0
    assert scenario.start_probability_if_available == base.start_probability_if_available


def test_fully_manual_fields_build_the_scenario_without_a_preset():
    scenario = _module._build_scenario(
        _namespace(
            start_if_available=0.5,
            sub_if_available=0.1,
            sixty_given_start=0.5,
            minutes_per_start=60.0,
            minutes_per_substitute=15.0,
        )
    )
    assert scenario.start_probability_if_available == 0.5
    assert scenario.minutes_per_start == 60.0


def test_parse_args_rejects_neither_preset_nor_complete_manual_fields():
    argv = [
        "--player-code",
        "1",
        "--gameweek",
        "1",
        "--observed-at",
        "2026-08-20T10:00:00+00:00",
        "--source",
        "test",
        "--rationale",
        "test",
    ]
    original_argv = sys.argv
    sys.argv = ["add_appearance_scenario_override.py", *argv]
    try:
        with pytest.raises(SystemExit) as excinfo:
            _module.parse_args()
        assert excinfo.value.code == 2
    finally:
        sys.argv = original_argv


def test_parse_args_accepts_preset_without_manual_fields():
    argv = [
        "--player-code",
        "1",
        "--gameweek",
        "1",
        "--observed-at",
        "2026-08-20T10:00:00+00:00",
        "--preset",
        "likely_bench",
        "--source",
        "test",
        "--rationale",
        "test",
    ]
    original_argv = sys.argv
    sys.argv = ["add_appearance_scenario_override.py", *argv]
    try:
        args = _module.parse_args()
    finally:
        sys.argv = original_argv
    assert args.preset == "likely_bench"
