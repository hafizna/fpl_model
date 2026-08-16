"""Tactical-role representations independent of nominal formation strings."""

from __future__ import annotations

from dataclasses import dataclass, fields
from math import sqrt


@dataclass(frozen=True, slots=True)
class RoleVector:
    """Continuous tactical-role fingerprint on 0..1 scales.

    These dimensions are intended to be estimated from spatial/event data and
    match annotation. They are more robust than a single provider label such as
    RB, RWB, or RM.
    """

    width: float
    height: float
    centrality: float
    build_up: float
    box_presence: float
    defensive_load: float

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{field.name} must be between 0 and 1")


def role_distance(left: RoleVector, right: RoleVector) -> float:
    """Euclidean distance for simple nearest-role exploration."""
    a = (
        left.width,
        left.height,
        left.centrality,
        left.build_up,
        left.box_presence,
        left.defensive_load,
    )
    b = (
        right.width,
        right.height,
        right.centrality,
        right.build_up,
        right.box_presence,
        right.defensive_load,
    )
    return sqrt(sum((x - y) ** 2 for x, y in zip(a, b, strict=True)))
