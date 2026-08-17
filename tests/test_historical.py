from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from fpl_model.validation.historical import (
    build_cross_season_player_bridge,
    infer_gameweek_deadlines,
    materialize_expanding_player_mean_baseline,
)


def _history_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "GW": 1,
                "element": 100,
                "fixture": 1,
                "kickoff_time": "2025-08-16T14:00:00Z",
                "total_points": 6,
            },
            {
                "GW": 1,
                "element": 100,
                "fixture": 2,
                "kickoff_time": "2025-08-25T14:00:00Z",
                "total_points": 10,
            },
            {
                "GW": 2,
                "element": 100,
                "fixture": 3,
                "kickoff_time": "2025-08-23T14:00:00Z",
                "total_points": 2,
            },
            {
                "GW": 2,
                "element": 200,
                "fixture": 3,
                "kickoff_time": "2025-08-23T14:00:00Z",
                "total_points": 1,
            },
        ]
    )


def test_cross_season_bridge_uses_stable_code_and_keeps_new_players():
    current = pd.DataFrame(
        {"id": [1, 2], "code": [154561, 999999], "web_name": ["Raya", "New"]}
    )
    previous = pd.DataFrame(
        {"id": [10, 20], "code": [154561, 123456], "web_name": ["Raya", "Gone"]}
    )

    bridge = build_cross_season_player_bridge(current, previous)

    assert bridge.loc[0, "previous_player_id"] == 10
    assert bool(bridge.loc[0, "has_previous_season"]) is True
    assert pd.isna(bridge.loc[1, "previous_player_id"])
    assert bool(bridge.loc[1, "has_previous_season"]) is False


def test_cross_season_bridge_rejects_ambiguous_codes():
    current = pd.DataFrame({"id": [1, 2], "code": [154561, 154561]})
    previous = pd.DataFrame({"id": [10], "code": [154561]})

    with pytest.raises(ValueError, match="duplicate player codes"):
        build_cross_season_player_bridge(current, previous)


def test_infers_deadline_from_earliest_kickoff():
    deadlines = infer_gameweek_deadlines(_history_frame())

    assert deadlines[1].to_pydatetime() == datetime(
        2025,
        8,
        16,
        12,
        30,
        tzinfo=UTC,
    )
    assert deadlines[2].to_pydatetime() == datetime(
        2025,
        8,
        23,
        12,
        30,
        tzinfo=UTC,
    )


def test_materializer_excludes_postponed_result_until_available():
    observations = materialize_expanding_player_mean_baseline(
        _history_frame(),
        season="2025-26",
    )
    player_100_gw2 = next(
        row
        for row in observations
        if row.player_id == 100 and row.gameweek == 2
    )
    player_200_gw2 = next(
        row
        for row in observations
        if row.player_id == 200 and row.gameweek == 2
    )

    assert player_100_gw2.predicted_xpts == pytest.approx(6.0)
    assert player_200_gw2.predicted_xpts == 0.0
    assert player_100_gw2.feature_cutoff <= player_100_gw2.deadline


def test_custom_deadline_buffer_is_explicit():
    deadlines = infer_gameweek_deadlines(
        _history_frame(),
        deadline_buffer=timedelta(hours=2),
    )

    assert deadlines[1].hour == 12


@pytest.mark.parametrize(
    "frame, message",
    [
        (pd.DataFrame({"GW": [1]}), "missing required columns"),
        (
            pd.DataFrame({"GW": [1], "kickoff_time": ["not-a-time"]}),
            "kickoff_time",
        ),
    ],
)
def test_deadline_inference_rejects_invalid_frames(
    frame: pd.DataFrame,
    message: str,
):
    with pytest.raises(ValueError, match=message):
        infer_gameweek_deadlines(frame)


def test_materializer_requires_outcome_columns():
    with pytest.raises(ValueError, match="total_points"):
        materialize_expanding_player_mean_baseline(
            _history_frame().drop(columns="total_points"),
            season="2025-26",
        )


def test_materializer_deduplicates_exact_rows_but_rejects_conflicts():
    frame = _history_frame()
    exact_duplicate = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)

    observations = materialize_expanding_player_mean_baseline(
        exact_duplicate,
        season="2025-26",
    )

    assert len(observations) == len(frame)

    conflict = frame.iloc[[0]].copy()
    conflict["total_points"] = 99
    conflicting_frame = pd.concat([frame, conflict], ignore_index=True)
    with pytest.raises(ValueError, match="conflicting duplicate"):
        materialize_expanding_player_mean_baseline(
            conflicting_frame,
            season="2025-26",
        )
