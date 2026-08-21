from __future__ import annotations

import pandas as pd
import pytest

from fpl_model.validation.projection_coverage import classify_projection_gaps


def _row(fpl_id: int, flags: list[str], **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "fpl_id": fpl_id,
        "player_code": 1000 + fpl_id,
        "player_name": f"Player {fpl_id}",
        "team": "TST",
        "position": "MID",
        "price": 6.0,
        "can_select": True,
        "selected_by_percent": 1.0,
        "expected_minutes": 60.0,
        "official_only": False,
        "data_quality_flags": flags,
    }
    row.update(overrides)
    return row


def test_classifies_mutually_exclusive_root_causes_and_cohorts():
    frame = pd.DataFrame(
        [
            _row(
                1,
                ["FPL_ROSTER_BLOCKED", "NO_PREVIOUS_PL_PLAYER_RATE_HISTORY"],
                official_only=True,
                can_select=False,
            ),
            _row(
                2,
                [
                    "MISSING_APPEARANCE_PROJECTION",
                    "NO_PREVIOUS_PL_PLAYER_RATE_HISTORY",
                ],
                official_only=True,
            ),
            _row(3, ["MISSING_APPEARANCE_PROJECTION"]),
            _row(4, ["NO_USABLE_PLAYER_RATE_HISTORY"]),
            _row(
                5,
                ["NO_PREVIOUS_PL_PLAYER_RATE_HISTORY", "OWN_TEAM_PROMOTED_PRIOR"],
                official_only=True,
            ),
            _row(
                6,
                ["NO_PREVIOUS_PL_PLAYER_RATE_HISTORY"],
                official_only=True,
            ),
            _row(7, ["NO_PREVIOUS_PL_PLAYER_RATE_HISTORY"]),
            _row(8, ["MISSING_TEAM_STRENGTH"]),
        ]
    )

    result = classify_projection_gaps(frame).set_index("fpl_id")

    assert result.loc[1, "primary_reason"] == "roster_blocked"
    assert result.loc[2, "primary_reason"] == "missing_appearance_and_rate"
    assert result.loc[3, "primary_reason"] == "missing_appearance_only"
    assert result.loc[4, "primary_reason"] == "unusable_previous_pl_rate"
    assert result.loc[5, "primary_reason"] == "promoted_no_previous_pl_rate"
    assert result.loc[6, "primary_reason"] == "current_only_no_previous_pl_rate"
    assert result.loc[7, "primary_reason"] == "unclassified_missing_previous_pl_rate"
    assert result.loc[8, "primary_reason"] == "other_projection_gap"
    assert result.loc[5, "identity_cohort"] == "current_only_promoted"
    assert result.loc[4, "identity_cohort"] == "previous_pl_linked"


def test_cheap_enabler_is_an_orthogonal_position_price_cohort():
    frame = pd.DataFrame(
        [
            _row(1, ["NO_USABLE_PLAYER_RATE_HISTORY"], position="GK", price=4.5),
            _row(2, ["NO_USABLE_PLAYER_RATE_HISTORY"], position="DEF", price=5.0),
            _row(3, ["NO_USABLE_PLAYER_RATE_HISTORY"], position="MID", price=5.5),
            _row(4, ["NO_USABLE_PLAYER_RATE_HISTORY"], position="FWD", price=5.5),
        ]
    )

    result = classify_projection_gaps(frame).set_index("fpl_id")

    assert bool(result.loc[1, "cheap_enabler"]) is True
    assert bool(result.loc[2, "cheap_enabler"]) is False
    assert bool(result.loc[3, "cheap_enabler"]) is True
    assert bool(result.loc[4, "cheap_enabler"]) is True


def test_rejects_malformed_flags_and_missing_columns():
    frame = pd.DataFrame([_row(1, ["NO_USABLE_PLAYER_RATE_HISTORY"])])
    frame.loc[0, "data_quality_flags"] = "not-json"
    with pytest.raises(ValueError):
        classify_projection_gaps(frame)

    with pytest.raises(ValueError, match="missing columns"):
        classify_projection_gaps(pd.DataFrame([{"fpl_id": 1}]))
