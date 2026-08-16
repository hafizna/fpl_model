"""Fixture-congestion feature construction.

This module creates descriptive workload features only. Any effect on start
probability, expected minutes, or per-90 output must be estimated separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class PriorAppearance:
    kickoff: datetime
    minutes: float


def workload_features(
    prior_appearances: list[PriorAppearance],
    *,
    deadline: datetime,
) -> dict[str, float | int | None]:
    eligible = [appearance for appearance in prior_appearances if appearance.kickoff < deadline]
    eligible.sort(key=lambda item: item.kickoff)

    last = eligible[-1] if eligible else None
    rest_days = (
        (deadline - last.kickoff).total_seconds() / 86_400.0 if last is not None else None
    )

    seven_days = deadline - timedelta(days=7)
    fourteen_days = deadline - timedelta(days=14)

    within_7 = [appearance for appearance in eligible if appearance.kickoff >= seven_days]
    within_14 = [appearance for appearance in eligible if appearance.kickoff >= fourteen_days]

    return {
        "rest_days": rest_days,
        "minutes_last_7d": float(sum(item.minutes for item in within_7)),
        "minutes_last_14d": float(sum(item.minutes for item in within_14)),
        "matches_last_7d": len(within_7),
        "matches_last_14d": len(within_14),
    }
