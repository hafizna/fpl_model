"""Provider-agnostic spatial primitives for heatmap/event coordinates.

The goal is to convert provider-specific coordinates into auditable 0..1
features. These features are *inputs* to tactical-role inference; they do not
apply an xPts multiplier directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

Point = tuple[float, float]


@dataclass(frozen=True, slots=True)
class SpatialFingerprint:
    sample_size: int
    avg_x: float
    avg_y: float
    final_third_share: float
    box_share: float
    left_share: float
    centre_share: float
    right_share: float
    role_attack_index: float


def _clamp01(value: float) -> float:
    return float(min(1.0, max(0.0, value)))


def normalise_points(
    points: Iterable[Point],
    *,
    x_max: float,
    y_max: float,
    flip_x: bool = False,
    flip_y: bool = False,
) -> np.ndarray:
    """Normalise provider coordinates to an attacking-left-to-right 0..1 pitch.

    Parameters are explicit because providers use different coordinate systems.
    A provider adapter should be responsible for choosing x_max/y_max and flips.
    """
    if x_max <= 0 or y_max <= 0:
        raise ValueError("x_max and y_max must be positive")

    array = np.asarray(list(points), dtype=float)
    if array.size == 0:
        return np.empty((0, 2), dtype=float)
    if array.ndim != 2 or array.shape[1] != 2:
        raise ValueError("points must be an iterable of (x, y) pairs")

    result = array.copy()
    result[:, 0] /= x_max
    result[:, 1] /= y_max

    if flip_x:
        result[:, 0] = 1.0 - result[:, 0]
    if flip_y:
        result[:, 1] = 1.0 - result[:, 1]

    return np.clip(result, 0.0, 1.0)


def fingerprint(
    points: Iterable[Point] | np.ndarray,
    *,
    final_third_start: float = 2 / 3,
    box_x_start: float = 0.83,
    box_half_width: float = 0.20,
) -> SpatialFingerprint:
    """Summarise normalised points into a first-pass tactical fingerprint.

    `role_attack_index` is intentionally provisional. It is a compact diagnostic
    for exploration and must be calibrated before being used as a model feature.
    """
    array = np.asarray(list(points) if not isinstance(points, np.ndarray) else points, dtype=float)
    if array.size == 0:
        raise ValueError("at least one spatial point is required")
    if array.ndim != 2 or array.shape[1] != 2:
        raise ValueError("points must have shape (n, 2)")
    if np.any(array < 0) or np.any(array > 1):
        raise ValueError("fingerprint expects already-normalised 0..1 coordinates")

    x = array[:, 0]
    y = array[:, 1]

    avg_x = float(x.mean())
    avg_y = float(y.mean())
    final_third_share = float(np.mean(x >= final_third_start))
    box_share = float(
        np.mean((x >= box_x_start) & (np.abs(y - 0.5) <= box_half_width))
    )
    left_share = float(np.mean(y < 1 / 3))
    centre_share = float(np.mean((y >= 1 / 3) & (y <= 2 / 3)))
    right_share = float(np.mean(y > 2 / 3))

    # Exploratory index only. Avoid feeding this straight into xPts until
    # historical calibration establishes useful weights.
    role_attack_index = _clamp01(
        0.45 * avg_x + 0.35 * final_third_share + 0.20 * box_share
    )

    return SpatialFingerprint(
        sample_size=len(array),
        avg_x=avg_x,
        avg_y=avg_y,
        final_third_share=final_third_share,
        box_share=box_share,
        left_share=left_share,
        centre_share=centre_share,
        right_share=right_share,
        role_attack_index=role_attack_index,
    )


def relative_height(player_avg_x: float, team_reference_avg_x: float) -> float:
    """Player height relative to a match/team reference to reduce game-state bias."""
    return float(player_avg_x - team_reference_avg_x)
