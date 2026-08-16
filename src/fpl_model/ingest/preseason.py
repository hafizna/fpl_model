"""Preseason canonicalisation helpers.

Provider-specific scraping belongs in a dedicated adapter. This module defines
what the rest of the model expects after provider data has been collected.
"""

from __future__ import annotations

import pandas as pd

PRESEASON_COLUMNS = [
    "player_id",
    "match_id",
    "started",
    "minutes",
    "sub_on_minute",
    "sub_off_minute",
    "nominal_position",
    "nominal_formation",
    "manager",
]

SPATIAL_COLUMNS = [
    "player_id",
    "match_id",
    "avg_x",
    "avg_y",
    "final_third_share",
    "box_share",
    "left_share",
    "centre_share",
    "right_share",
    "relative_height",
    "role_attack_index",
    "spatial_confidence",
]


def canonicalise_preseason_appearances(frame: pd.DataFrame) -> pd.DataFrame:
    missing = set(PRESEASON_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"missing preseason columns: {sorted(missing)}")

    result = frame[PRESEASON_COLUMNS].copy()
    result["minutes"] = pd.to_numeric(result["minutes"], errors="raise")
    if ((result["minutes"] < 0) | (result["minutes"] > 130)).any():
        raise ValueError("preseason minutes must be between 0 and 130")
    result["started"] = result["started"].astype(bool)
    return result


def canonicalise_spatial_features(frame: pd.DataFrame) -> pd.DataFrame:
    missing = set(SPATIAL_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"missing spatial columns: {sorted(missing)}")

    result = frame[SPATIAL_COLUMNS].copy()
    bounded = [
        "avg_x",
        "avg_y",
        "final_third_share",
        "box_share",
        "left_share",
        "centre_share",
        "right_share",
        "role_attack_index",
        "spatial_confidence",
    ]
    for column in bounded:
        result[column] = pd.to_numeric(result[column], errors="raise")
        if ((result[column] < 0) | (result[column] > 1)).any():
            raise ValueError(f"{column} must be between 0 and 1")

    result["relative_height"] = pd.to_numeric(result["relative_height"], errors="raise")
    return result
