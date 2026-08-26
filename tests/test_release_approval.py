from __future__ import annotations

import duckdb
import pytest

from fpl_model.model.shadow_calibration import (
    materialize_shadow_calibration,
    store_shadow_calibration_artifact,
)
from fpl_model.storage import initialize_database
from fpl_model.validation.projection_uncertainty import (
    apply_uncertainty_artifact,
    build_residual_rows,
    store_uncertainty_artifact,
)
from fpl_model.validation.release_approval import check_release_approval
from tests.test_sprint4_uncertainty import _history, _seed_projection


def test_fails_closed_when_no_lineage_is_linked_at_all(tmp_path):
    database_path = tmp_path / "approval.duckdb"
    _seed_projection(database_path)

    result = check_release_approval(model_run_ids=("model",), database_path=database_path)

    assert result.passes is False
    problems = "\n".join(result.report["problems"])
    assert "no uncertainty artifact is linked" in problems
    assert "no shadow-calibration artifact is linked" in problems


def test_fails_closed_when_lineage_is_shadow_only(tmp_path):
    database_path = tmp_path / "approval.duckdb"
    _seed_projection(database_path)
    observations, diagnostics = _history()
    residuals = build_residual_rows(observations, diagnostics)
    uncertainty_artifact = store_uncertainty_artifact(
        residuals,
        source_season="2025-26",
        source_model_version="test",
        source_reference="ref.json",
        minimum_segment_rows=12,
        minimum_segment_gameweeks=3,
        database_path=database_path,
    )
    apply_uncertainty_artifact(
        model_run_id="model",
        artifact_id=uncertainty_artifact.artifact_id,
        database_path=database_path,
    )
    calibration_artifact = store_shadow_calibration_artifact(
        source_season="2025-26",
        source_model_version="test",
        source_reference="ref.json",
        training_rows=100,
        training_gameweeks=10,
        slope=0.8,
        intercept=0.2,
        database_path=database_path,
    )
    materialize_shadow_calibration(
        model_run_id="model",
        artifact_id=calibration_artifact.artifact_id,
        database_path=database_path,
    )

    result = check_release_approval(model_run_ids=("model",), database_path=database_path)

    assert result.passes is False
    problems = "\n".join(result.report["problems"])
    assert "not an approved+active artifact" in problems
    assert "not approved" in problems
    assert "shadow" in result.report["note"]


def test_passes_once_both_artifacts_are_approved_and_uncertainty_is_fully_applied(tmp_path):
    database_path = tmp_path / "approval.duckdb"
    _seed_projection(database_path)
    observations, diagnostics = _history()
    residuals = build_residual_rows(observations, diagnostics)
    uncertainty_artifact = store_uncertainty_artifact(
        residuals,
        source_season="2025-26",
        source_model_version="test",
        source_reference="ref.json",
        minimum_segment_rows=12,
        minimum_segment_gameweeks=3,
        status="approved",
        database_path=database_path,
    )
    applied = apply_uncertainty_artifact(
        model_run_id="model",
        artifact_id=uncertainty_artifact.artifact_id,
        database_path=database_path,
    )
    assert applied.application_mode == "active"
    calibration_artifact = store_shadow_calibration_artifact(
        source_season="2025-26",
        source_model_version="test",
        source_reference="ref.json",
        training_rows=100,
        training_gameweeks=10,
        slope=0.8,
        intercept=0.2,
        status="approved",
        database_path=database_path,
    )
    materialize_shadow_calibration(
        model_run_id="model",
        artifact_id=calibration_artifact.artifact_id,
        database_path=database_path,
    )

    result = check_release_approval(model_run_ids=("model",), database_path=database_path)

    assert result.passes is True
    assert result.report["problems"] == []
    gw = result.report["gameweeks"][0]
    assert gw["uncertainty"]["status"] == "approved"
    assert gw["uncertainty"]["application_mode"] == "active"
    assert gw["uncertainty"]["populated_rows"] == gw["uncertainty"]["total_rows"] == 1
    assert gw["calibration"]["status"] == "approved"

    with duckdb.connect(str(database_path), read_only=True) as connection:
        scalar = connection.execute(
            "SELECT uncertainty FROM player_fixture_projection WHERE model_run_id = 'model'"
        ).fetchone()[0]
    assert scalar is not None


def test_rejects_empty_and_duplicate_run_ids(tmp_path):
    database_path = tmp_path / "approval.duckdb"
    initialize_database(database_path)

    with pytest.raises(ValueError, match="at least one model_run_id"):
        check_release_approval(model_run_ids=(), database_path=database_path)

    _seed_projection(database_path)
    with pytest.raises(ValueError, match="duplicates"):
        check_release_approval(model_run_ids=("model", "model"), database_path=database_path)


def test_raises_on_unknown_run_id(tmp_path):
    database_path = tmp_path / "approval.duckdb"
    initialize_database(database_path)

    with pytest.raises(ValueError, match="unknown model_run_id"):
        check_release_approval(model_run_ids=("nope",), database_path=database_path)
