from __future__ import annotations

import json

import pytest

from fpl_model.validation.backtest import BacktestMetrics
from fpl_model.validation.backtest_self_check import load_reference, verify_self_check


def _reference() -> dict[str, object]:
    return {
        "import_run_id": "player_history_ae118d1588eeaf84",
        "evaluation_from_gw": 3,
        "evaluation_to_gw": 38,
        "metrics": {
            "observations": 16496,
            "mean_absolute_error": 1.7126034199015057,
            "root_mean_squared_error": 2.5477971497530763,
            "mean_error": 0.11756044026669629,
            "mean_predicted_xpts": 2.0091099068040386,
            "mean_actual_points": 1.8915494665373425,
        },
    }


def _matching_metrics() -> BacktestMetrics:
    return BacktestMetrics(
        observations=16496,
        mean_predicted_xpts=2.0091099068040386,
        mean_actual_points=1.8915494665373425,
        mean_error=0.11756044026669629,
        mean_absolute_error=1.7126034199015057,
        root_mean_squared_error=2.5477971497530763,
    )


def test_verify_self_check_passes_with_matching_reference():
    result = verify_self_check(
        reference=_reference(),
        reference_path="ref.json",
        import_run_id="player_history_ae118d1588eeaf84",
        evaluation_from_gw=3,
        evaluation_to_gw=38,
        model_metrics=_matching_metrics(),
    )

    assert result["status"] == "passed"
    assert "metrics.mean_absolute_error" in result["checked_fields"]


def test_verify_self_check_passes_within_float_tolerance():
    # A difference far below FLOAT_METRIC_TOLERANCE (1e-9) must still pass --
    # this is the machine-noise allowance, not a real methodology drift.
    metrics = _matching_metrics()
    nudged = BacktestMetrics(
        observations=metrics.observations,
        mean_predicted_xpts=metrics.mean_predicted_xpts,
        mean_actual_points=metrics.mean_actual_points,
        mean_error=metrics.mean_error,
        mean_absolute_error=metrics.mean_absolute_error + 1e-12,
        root_mean_squared_error=metrics.root_mean_squared_error,
    )

    result = verify_self_check(
        reference=_reference(),
        reference_path="ref.json",
        import_run_id="player_history_ae118d1588eeaf84",
        evaluation_from_gw=3,
        evaluation_to_gw=38,
        model_metrics=nudged,
    )

    assert result["status"] == "passed"


def test_verify_self_check_fails_loudly_on_changed_mae():
    metrics = _matching_metrics()
    changed = BacktestMetrics(
        observations=metrics.observations,
        mean_predicted_xpts=metrics.mean_predicted_xpts,
        mean_actual_points=metrics.mean_actual_points,
        mean_error=metrics.mean_error,
        mean_absolute_error=999.0,
        root_mean_squared_error=metrics.root_mean_squared_error,
    )

    with pytest.raises(ValueError, match="mean_absolute_error"):
        verify_self_check(
            reference=_reference(),
            reference_path="ref.json",
            import_run_id="player_history_ae118d1588eeaf84",
            evaluation_from_gw=3,
            evaluation_to_gw=38,
            model_metrics=changed,
        )


def test_verify_self_check_fails_loudly_on_changed_observation_count():
    metrics = _matching_metrics()
    changed = BacktestMetrics(
        observations=1,
        mean_predicted_xpts=metrics.mean_predicted_xpts,
        mean_actual_points=metrics.mean_actual_points,
        mean_error=metrics.mean_error,
        mean_absolute_error=metrics.mean_absolute_error,
        root_mean_squared_error=metrics.root_mean_squared_error,
    )

    with pytest.raises(ValueError, match="observations"):
        verify_self_check(
            reference=_reference(),
            reference_path="ref.json",
            import_run_id="player_history_ae118d1588eeaf84",
            evaluation_from_gw=3,
            evaluation_to_gw=38,
            model_metrics=changed,
        )


def test_verify_self_check_fails_loudly_on_mismatched_import_run_id():
    with pytest.raises(ValueError, match="import_run_id"):
        verify_self_check(
            reference=_reference(),
            reference_path="ref.json",
            import_run_id="a_completely_different_run",
            evaluation_from_gw=3,
            evaluation_to_gw=38,
            model_metrics=_matching_metrics(),
        )


def test_verify_self_check_fails_loudly_on_mismatched_evaluation_range():
    with pytest.raises(ValueError, match="evaluation_to_gw"):
        verify_self_check(
            reference=_reference(),
            reference_path="ref.json",
            import_run_id="player_history_ae118d1588eeaf84",
            evaluation_from_gw=3,
            evaluation_to_gw=20,
            model_metrics=_matching_metrics(),
        )


def test_verify_self_check_rejects_reference_without_metrics():
    with pytest.raises(ValueError, match="metrics"):
        verify_self_check(
            reference={"import_run_id": "x", "evaluation_from_gw": 3, "evaluation_to_gw": 38},
            reference_path="ref.json",
            import_run_id="x",
            evaluation_from_gw=3,
            evaluation_to_gw=38,
            model_metrics=_matching_metrics(),
        )


def test_load_reference_rejects_missing_file(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        load_reference(tmp_path / "does_not_exist.json")


def test_load_reference_reads_real_json(tmp_path):
    path = tmp_path / "ref.json"
    path.write_text(json.dumps(_reference()), encoding="utf-8")

    loaded = load_reference(path)

    assert loaded == _reference()
