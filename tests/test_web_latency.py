from __future__ import annotations

from pathlib import Path

from fpl_model.validation.web_latency import validate_web_latency


def _response() -> dict:
    return {
        "release_id": "web_release_test",
        "health": "production",
        "cumulative_xpts": 150.0,
        "squad_rating": {
            "available": True,
            "benchmark": {"benchmark_id": "squad_benchmark_test"},
            "performance_contract": {"benchmark_mode": "release_artifact"},
        },
    }


def test_latency_contract_requires_fast_stable_materialized_results(tmp_path: Path):
    times = iter((10.0, 10.2, 20.0, 20.4))

    result = validate_web_latency(
        tuple(range(1, 16)),
        release_path=tmp_path / "release.json",
        cold_limit_ms=300.0,
        cached_limit_ms=500.0,
        recommend=lambda *args, **kwargs: _response(),
        clock=lambda: next(times),
    )

    assert result.passes
    assert result.report["observed_ms"] == {"cold": 200.0, "cached": 400.0}
    assert all(result.report["checks"].values())


def test_latency_contract_fails_runtime_fallback_even_when_fast(tmp_path: Path):
    response = _response()
    response["squad_rating"]["performance_contract"]["benchmark_mode"] = "runtime_cache"
    times = iter((1.0, 1.01, 2.0, 2.01))

    result = validate_web_latency(
        tuple(range(1, 16)),
        release_path=tmp_path / "release.json",
        recommend=lambda *args, **kwargs: response,
        clock=lambda: next(times),
    )

    assert result.passes is False
    assert result.report["checks"]["release_artifact_used"] is False
