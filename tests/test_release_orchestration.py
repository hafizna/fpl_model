from __future__ import annotations

import duckdb
import pytest

from fpl_model.validation.release_orchestration import (
    ReleaseGateFailure,
    enforce_release_gate,
    orchestrate_release_validation,
)
from tests.test_release_manifest import _seed_horizon


def _add_freshness_prerequisites(database_path) -> None:
    """check_release_freshness also needs gameweek_snapshot/fixture_snapshot rows
    that _seed_horizon (built only for the manifest test) does not provide."""
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO gameweek_snapshot (
                ingestion_run_id, gameweek, name, deadline_time, release_time,
                finished, data_checked, is_previous, is_current, is_next
            ) VALUES ('snapshot', 1, 'Gameweek 1', '2026-08-22T00:30:00+07:00', NULL,
                      TRUE, TRUE, FALSE, TRUE, FALSE);
            INSERT INTO team_snapshot (
                ingestion_run_id, team_id, team_code, name, short_name, unavailable
            ) VALUES
                ('snapshot', 1, 101, 'Sunderland', 'SUN', false),
                ('snapshot', 2, 102, 'Opponent', 'OPP', false);
            INSERT INTO fixture_snapshot VALUES (
                'snapshot', 100, 1, '2026-08-22T15:00:00+01:00', 1, 2, TRUE, TRUE
            );
            """
        )


def test_passes_and_reports_shadow_only_approval_for_a_healthy_release(tmp_path):
    database_path = tmp_path / "orchestration.duckdb"
    _seed_horizon(database_path)
    _add_freshness_prerequisites(database_path)

    result = orchestrate_release_validation(
        model_run_ids=("baseline_gw1",), database_path=database_path
    )

    assert result.passes is True
    assert result.approval_status == "shadow_only"
    assert result.report["manifest"]["linkage"]["passes"] is True
    assert result.report["freshness"]["passes"] is True
    assert result.report["approval"]["passes"] is False


def test_fails_when_manifest_linkage_fails(tmp_path):
    database_path = tmp_path / "orchestration.duckdb"
    _seed_horizon(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('snapshot2', 'fpl_api', '2026-08-25T09:00:00+07:00', 'completed');
            INSERT INTO model_run (
                model_run_id, target_gameweek, as_of, deadline, model_version,
                source_ingestion_run_id, status
            ) VALUES (
                'baseline_gw2', 2, '2026-08-25T09:00:00+07:00',
                '2026-08-29T00:30:00+07:00', 'test_v1', 'snapshot2', 'completed'
            );
            """
        )

    result = orchestrate_release_validation(
        model_run_ids=("baseline_gw1", "baseline_gw2"), database_path=database_path
    )

    assert result.passes is False
    assert result.report["manifest"]["linkage"]["passes"] is False


def test_approval_status_reads_approved_once_promoted(tmp_path):
    database_path = tmp_path / "orchestration.duckdb"
    _seed_horizon(database_path)
    _add_freshness_prerequisites(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            "UPDATE uncertainty_artifact SET status = 'approved' WHERE artifact_id = 'uncertainty_1'"
        )
        connection.execute(
            "UPDATE model_uncertainty_lineage SET application_mode = 'active' "
            "WHERE model_run_id = 'baseline_gw1'"
        )
        connection.execute(
            "UPDATE player_fixture_projection SET uncertainty = 1.0 "
            "WHERE model_run_id = 'baseline_gw1'"
        )
        connection.execute(
            "UPDATE shadow_calibration_artifact SET status = 'approved' "
            "WHERE artifact_id = 'calibration_1'"
        )

    result = orchestrate_release_validation(
        model_run_ids=("baseline_gw1",), database_path=database_path
    )

    assert result.passes is True
    assert result.approval_status == "approved"


def test_propagates_manifest_errors_for_unknown_run_id(tmp_path):
    database_path = tmp_path / "orchestration.duckdb"
    _seed_horizon(database_path)

    with pytest.raises(ValueError, match="unknown model_run_id"):
        orchestrate_release_validation(model_run_ids=("nope",), database_path=database_path)


def test_enforce_release_gate_returns_the_report_when_it_passes(tmp_path):
    database_path = tmp_path / "orchestration.duckdb"
    _seed_horizon(database_path)
    _add_freshness_prerequisites(database_path)

    result = enforce_release_gate(model_run_ids=("baseline_gw1",), database_path=database_path)

    assert result.passes is True


def test_enforce_release_gate_raises_with_the_full_report_when_it_fails(tmp_path):
    database_path = tmp_path / "orchestration.duckdb"
    _seed_horizon(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('snapshot2', 'fpl_api', '2026-08-25T09:00:00+07:00', 'completed');
            INSERT INTO model_run (
                model_run_id, target_gameweek, as_of, deadline, model_version,
                source_ingestion_run_id, status
            ) VALUES (
                'baseline_gw2', 2, '2026-08-25T09:00:00+07:00',
                '2026-08-29T00:30:00+07:00', 'test_v1', 'snapshot2', 'completed'
            );
            """
        )

    with pytest.raises(ReleaseGateFailure) as excinfo:
        enforce_release_gate(
            model_run_ids=("baseline_gw1", "baseline_gw2"), database_path=database_path
        )

    assert excinfo.value.result.passes is False
    assert "do not share one official snapshot" in str(excinfo.value)


def test_enforce_release_gate_propagates_lookup_errors_unchanged(tmp_path):
    database_path = tmp_path / "orchestration.duckdb"
    _seed_horizon(database_path)

    with pytest.raises(ValueError, match="unknown model_run_id"):
        enforce_release_gate(model_run_ids=("nope",), database_path=database_path)
