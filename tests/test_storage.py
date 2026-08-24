from __future__ import annotations

from datetime import UTC, datetime

import duckdb

from fpl_model.storage import SCHEMA_VERSION, initialize_database


def test_database_initialisation_is_persistent_and_idempotent(tmp_path):
    database_path = tmp_path / "nested" / "fpl_model.duckdb"

    first = initialize_database(database_path)
    second = initialize_database(database_path)

    assert first.path == database_path.resolve()
    assert first.schema_version == SCHEMA_VERSION
    assert first.tables == second.tables
    assert {
        "availability_signal",
        "baseline_projection_gap",
        "baseline_projection_run",
        "baseline_context_lineage",
        "context_feature_run",
        "event_penalty_review",
        "fixture_snapshot",
        "fpl_event_live_run",
        "ingestion_run",
        "model_run",
        "model_shadow_calibration_lineage",
        "model_uncertainty_lineage",
        "player_fixture_projection",
        "player_fixture_shadow_projection",
        "player_fixture_uncertainty",
        "player_gameweek_attacking_decomposition",
        "player_gameweek_stat",
        "player_context_feature",
        "player_identity_bridge",
        "player_identity_bridge_run",
        "player_penalty_event",
        "player_rate_evidence",
        "player_rate_evidence_import_run",
        "player_snapshot",
        "projection_component",
        "reviewed_context_annotation",
        "schema_version",
        "shadow_calibration_artifact",
        "squad_chip_state",
        "squad_snapshot",
        "squad_snapshot_player",
        "uncertainty_artifact",
        "uncertainty_segment",
    }.issubset(first.tables)


def test_player_state_is_versioned_by_ingestion_run(tmp_path):
    database_path = tmp_path / "fpl_model.duckdb"
    initialize_database(database_path)
    captured_at = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)

    with duckdb.connect(str(database_path)) as connection:
        for run_id in ("morning", "deadline"):
            connection.execute(
                """
                INSERT INTO ingestion_run (
                    ingestion_run_id, source, captured_at, status
                ) VALUES (?, 'fpl_api', ?, 'completed')
                """,
                [run_id, captured_at],
            )
        connection.execute(
            """
            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status,
                chance_of_playing_next_round, news
            ) VALUES
                ('morning', '2026-27', 1, 1001, 'A', 'Player', 'Player', 1,
                 'MID', 7.5, 'd', 50, 'Knock'),
                ('deadline', '2026-27', 1, 1001, 'A', 'Player', 'Player', 1,
                 'MID', 7.5, 'a', NULL, '')
            """
        )

        rows = connection.execute(
            """
            SELECT ingestion_run_id, fpl_status, chance_of_playing_next_round
            FROM player_snapshot
            WHERE player_code = 1001
            ORDER BY ingestion_run_id
            """
        ).fetchall()

    assert rows == [("deadline", "a", None), ("morning", "d", 50)]


def test_version_two_database_migrates_nullable_preseason_team_strength(tmp_path):
    database_path = tmp_path / "fpl_model.duckdb"
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ)"
        )
        connection.execute("INSERT INTO schema_version VALUES (2, current_timestamp)")
        connection.execute(
            """
            CREATE TABLE team_snapshot (
                ingestion_run_id VARCHAR,
                team_id INTEGER,
                team_code INTEGER,
                name VARCHAR,
                short_name VARCHAR,
                unavailable BOOLEAN,
                strength INTEGER NOT NULL,
                strength_overall_home INTEGER NOT NULL,
                strength_overall_away INTEGER NOT NULL,
                strength_attack_home INTEGER NOT NULL,
                strength_attack_away INTEGER NOT NULL,
                strength_defence_home INTEGER NOT NULL,
                strength_defence_away INTEGER NOT NULL
            )
            """
        )

    info = initialize_database(database_path)

    assert info.schema_version == SCHEMA_VERSION
    with duckdb.connect(str(database_path), read_only=True) as connection:
        nullable = connection.execute(
            """
            SELECT is_nullable
            FROM information_schema.columns
            WHERE table_name = 'team_snapshot' AND column_name = 'strength'
            """
        ).fetchone()[0]
    assert nullable == "YES"
