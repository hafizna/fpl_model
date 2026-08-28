"""Operational latency contract for the materialized lineup/rating path."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fpl_model.webapp.service import recommend_web_lineups

DEFAULT_COLD_LIMIT_MS = 3_000.0
DEFAULT_CACHED_LIMIT_MS = 1_000.0


@dataclass(frozen=True, slots=True)
class WebLatencyResult:
    report: dict[str, Any]

    @property
    def passes(self) -> bool:
        return bool(self.report["passes"])


def validate_web_latency(
    fpl_ids: tuple[int, ...],
    *,
    release_path: str | Path,
    bank_tenths: int = 0,
    cold_limit_ms: float = DEFAULT_COLD_LIMIT_MS,
    cached_limit_ms: float = DEFAULT_CACHED_LIMIT_MS,
    recommend: Callable[..., dict[str, Any]] = recommend_web_lineups,
    clock: Callable[[], float] = time.perf_counter,
) -> WebLatencyResult:
    """Time two identical decisions and require a release-materialized benchmark."""

    if len(fpl_ids) != 15 or len(set(fpl_ids)) != 15:
        raise ValueError("latency validation requires 15 unique FPL IDs")
    if cold_limit_ms <= 0 or cached_limit_ms <= 0:
        raise ValueError("latency limits must be positive")

    cold_started = clock()
    cold = recommend(fpl_ids, bank_tenths=bank_tenths, release_path=release_path)
    cold_ms = (clock() - cold_started) * 1_000
    cached_started = clock()
    cached = recommend(fpl_ids, bank_tenths=bank_tenths, release_path=release_path)
    cached_ms = (clock() - cached_started) * 1_000

    cold_rating = cold["squad_rating"]
    cached_rating = cached["squad_rating"]
    modes = (
        cold_rating["performance_contract"]["benchmark_mode"],
        cached_rating["performance_contract"]["benchmark_mode"],
    )
    checks = {
        "cold_within_limit": cold_ms <= cold_limit_ms,
        "cached_within_limit": cached_ms <= cached_limit_ms,
        "rating_available": bool(cold_rating["available"] and cached_rating["available"]),
        "release_artifact_used": modes == ("release_artifact", "release_artifact"),
        "stable_benchmark": (
            cold_rating["benchmark"].get("benchmark_id")
            == cached_rating["benchmark"].get("benchmark_id")
        ),
        "stable_raw_xpts": cold["cumulative_xpts"] == cached["cumulative_xpts"],
    }
    return WebLatencyResult(
        report={
            "schema_version": "web_latency_contract_v1",
            "release_path": str(Path(release_path)),
            "release_id": cold.get("release_id"),
            "release_health": cold.get("health"),
            "squad_size": len(fpl_ids),
            "limits_ms": {"cold": cold_limit_ms, "cached": cached_limit_ms},
            "observed_ms": {"cold": round(cold_ms, 3), "cached": round(cached_ms, 3)},
            "benchmark_id": cold_rating["benchmark"].get("benchmark_id"),
            "benchmark_mode": modes[0],
            "checks": checks,
            "passes": all(checks.values()),
        }
    )
