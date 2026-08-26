from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pytest

from fpl_model.storage import initialize_database
from fpl_model.validation.release_freshness import check_release_freshness


def _seed_model_run(
    database_path,
    *,
    model_run_id: str = "baseline_gw1",
    gameweek: int = 1,
    as_of: str = "2026-08-18T09:00:00+07:00",
    deadline: str = "2026-08-22T00:30:00+07:00",
    gw_finished: bool = False,
    gw_data_checked: bool = False,
    fixtures_finished: int = 0,
    fixtures_total: int = 2,
    ingestion_run_id: str = "snapshot",
    captured_at: str = "2026-08-18T09:00:00+07:00",
) -> None:
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES (?, 'fpl_api', ?, 'completed')
            """,
            [ingestion_run_id, captured_at],
        )
        connection.execute(
            """
            INSERT INTO gameweek_snapshot (
                ingestion_run_id, gameweek, name, deadline_time, release_time,
                finished, data_checked, is_previous, is_current, is_next
            ) VALUES (?, ?, ?, ?, NULL, ?, ?, FALSE, TRUE, FALSE)
            """,
            [ingestion_run_id, gameweek, f"Gameweek {gameweek}", deadline, gw_finished, gw_data_checked],
        )
        connection.execute(
            """
            INSERT INTO team_snapshot (
                ingestion_run_id, team_id, team_code, name, short_name, unavailable
            ) VALUES
                (?, 1, 101, 'Sunderland', 'SUN', false),
                (?, 2, 102, 'Opponent', 'OPP', false)
            """,
            [ingestion_run_id, ingestion_run_id],
        )
        for fixture_id in range(fixtures_total):
            finished = fixture_id < fixtures_finished
            connection.execute(
                """
                INSERT INTO fixture_snapshot VALUES (
                    ?, ?, ?, '2026-08-22T15:00:00+01:00', 1, 2, ?, ?
                )
                """,
                [ingestion_run_id, 100 + fixture_id, gameweek, finished, finished],
            )
        connection.execute(
            """
            INSERT INTO model_run (
                model_run_id, target_gameweek, as_of, deadline, model_version,
                source_ingestion_run_id, status
            ) VALUES (?, ?, ?, ?, 'test_v1', ?, 'completed')
            """,
            [model_run_id, gameweek, as_of, deadline, ingestion_run_id],
        )


def test_reports_finished_gameweek_as_final_and_not_drift_eligible(tmp_path):
    database_path = tmp_path / "freshness.duckdb"
    _seed_model_run(
        database_path,
        gw_finished=True,
        gw_data_checked=True,
        fixtures_finished=2,
        fixtures_total=2,
    )

    result = check_release_freshness(
        model_run_ids=("baseline_gw1",),
        database_path=database_path,
        now=datetime(2026, 8, 30, tzinfo=UTC),
    )

    assert result.passes is True
    gw1 = result.report["gameweeks"][0]
    assert gw1["fixtures"] == {"total": 2, "finished": 2, "analytically_complete": True}
    assert gw1["fpl_finality"] == {"finished": True, "data_checked": True, "is_final": True}
    assert gw1["drift_check_eligible"] is False
    assert "GAMEWEEK_NOT_FINISHED" not in gw1["flags"]
    assert "FIXTURES_INCOMPLETE" not in gw1["flags"]


def test_flags_analytically_complete_but_not_final_as_drift_eligible(tmp_path):
    database_path = tmp_path / "freshness.duckdb"
    _seed_model_run(
        database_path,
        gw_finished=True,
        gw_data_checked=False,
        fixtures_finished=2,
        fixtures_total=2,
    )

    result = check_release_freshness(
        model_run_ids=("baseline_gw1",),
        database_path=database_path,
        now=datetime(2026, 8, 30, tzinfo=UTC),
    )

    assert result.passes is True
    gw1 = result.report["gameweeks"][0]
    assert gw1["drift_check_eligible"] is True
    assert "PROVISIONAL_DRIFT_CHECK_ELIGIBLE" in gw1["flags"]
    assert "GAMEWEEK_FINISHED_NOT_DATA_CHECKED" in gw1["flags"]


def test_flags_incomplete_fixtures_and_unfinished_gameweek(tmp_path):
    database_path = tmp_path / "freshness.duckdb"
    _seed_model_run(database_path, fixtures_finished=0, fixtures_total=2)

    result = check_release_freshness(
        model_run_ids=("baseline_gw1",),
        database_path=database_path,
        now=datetime(2026, 8, 30, tzinfo=UTC),
    )

    assert result.passes is True
    gw1 = result.report["gameweeks"][0]
    assert gw1["fixtures"]["analytically_complete"] is False
    assert "FIXTURES_INCOMPLETE" in gw1["flags"]
    assert "GAMEWEEK_NOT_FINISHED" in gw1["flags"]
    assert gw1["drift_check_eligible"] is False


def test_flags_stale_snapshot_only_before_deadline(tmp_path):
    database_path = tmp_path / "freshness.duckdb"
    _seed_model_run(
        database_path,
        as_of="2026-08-18T09:00:00+07:00",
        deadline="2026-08-29T00:30:00+07:00",
        captured_at="2026-08-18T09:00:00+07:00",
    )

    before_deadline = check_release_freshness(
        model_run_ids=("baseline_gw1",),
        database_path=database_path,
        now=datetime(2026, 8, 25, tzinfo=UTC),
        stale_after_hours=24.0,
    )
    gw1 = before_deadline.report["gameweeks"][0]
    assert "SNAPSHOT_STALE_RELATIVE_TO_NOW" in gw1["flags"]
    assert gw1["deadline_passed_relative_to_now"] is False

    after_deadline = check_release_freshness(
        model_run_ids=("baseline_gw1",),
        database_path=database_path,
        now=datetime(2026, 9, 5, tzinfo=UTC),
        stale_after_hours=24.0,
    )
    gw1_after = after_deadline.report["gameweeks"][0]
    assert "SNAPSHOT_STALE_RELATIVE_TO_NOW" not in gw1_after["flags"]
    assert gw1_after["deadline_passed_relative_to_now"] is True


def test_fails_closed_when_snapshot_captured_after_its_own_deadline(tmp_path):
    database_path = tmp_path / "freshness.duckdb"
    _seed_model_run(
        database_path,
        as_of="2026-08-18T09:00:00+07:00",
        deadline="2026-08-22T00:30:00+07:00",
        captured_at="2026-08-23T09:00:00+07:00",
    )

    result = check_release_freshness(
        model_run_ids=("baseline_gw1",),
        database_path=database_path,
        now=datetime(2026, 8, 30, tzinfo=UTC),
    )

    assert result.passes is False
    assert any("lookahead hazard" in problem for problem in result.report["problems"])


def test_rejects_empty_and_duplicate_run_ids(tmp_path):
    database_path = tmp_path / "freshness.duckdb"
    initialize_database(database_path)

    with pytest.raises(ValueError, match="at least one model_run_id"):
        check_release_freshness(model_run_ids=(), database_path=database_path)

    _seed_model_run(database_path)
    with pytest.raises(ValueError, match="duplicates"):
        check_release_freshness(
            model_run_ids=("baseline_gw1", "baseline_gw1"), database_path=database_path
        )


def test_rejects_non_positive_stale_after_hours(tmp_path):
    database_path = tmp_path / "freshness.duckdb"
    _seed_model_run(database_path)

    with pytest.raises(ValueError, match="stale_after_hours must be positive"):
        check_release_freshness(
            model_run_ids=("baseline_gw1",),
            database_path=database_path,
            stale_after_hours=0,
        )


def test_raises_on_unknown_run_id(tmp_path):
    database_path = tmp_path / "freshness.duckdb"
    initialize_database(database_path)

    with pytest.raises(ValueError, match="unknown model_run_id"):
        check_release_freshness(model_run_ids=("nope",), database_path=database_path)
