"""Coverage for webapp.release_export.build_web_release.

No prior test file exercised this module at all. This builds a full,
genuinely valid three-Gameweek release fixture (manifest linkage, freshness,
shadow calibration/uncertainty lineage, official identity, and the same
availability/appearance/role_state lineage tests/test_role_state_wiring.py
already established) rather than mocking `orchestrate_release_validation`,
so a real regression in any of those gates would be caught here too.
"""

from __future__ import annotations

import duckdb
import pytest

from fpl_model.storage import initialize_database
from fpl_model.validation.role_state import LIKELY_STARTER, UNAVAILABLE
from fpl_model.webapp.release_export import build_web_release
from tests.test_role_state_wiring import _add_role_state_lineage


def _seed_one_gameweek(connection, *, gameweek: int, model_run_id: str) -> None:
    """One Gameweek's manifest/freshness/calibration/uncertainty lineage,
    mirroring tests/test_release_manifest.py's own single-Gameweek fixture --
    generalised so build_web_release's three-Gameweek requirement can reuse
    it once per Gameweek. Availability/appearance/baseline lineage (which
    load_role_states needs) is seeded separately by
    tests.test_role_state_wiring._add_role_state_lineage, which opens its
    own connection and so must run after this one closes."""
    deadline = f"2026-08-{18 + 4 * gameweek:02d}T00:30:00+07:00"
    connection.execute(
        f"""
        INSERT INTO gameweek_snapshot (
            ingestion_run_id, gameweek, name, deadline_time, release_time,
            finished, data_checked, is_previous, is_current, is_next
        ) VALUES ('official', {gameweek}, 'Gameweek {gameweek}', '{deadline}', NULL,
                  TRUE, TRUE, FALSE, {"TRUE" if gameweek == 1 else "FALSE"}, FALSE);

        INSERT INTO player_identity_bridge_run (
            bridge_run_id, source_ingestion_run_id, target_season,
            vaastav_season, source_revision, source_path, source_sha256,
            policy_version, official_players, vaastav_players,
            matched_players, official_only_players, vaastav_only_players,
            name_mismatch_players, status
        ) VALUES (
            'bridge_{gameweek}', 'official', '2026-27', '2025-26', 'rev', 'players.csv',
            'sha', 'test', 2, 2, 2, 0, 0, 0, 'completed'
        );

        INSERT INTO fixture_snapshot VALUES (
            'official', {100 + gameweek}, {gameweek}, '{deadline}', 1, 2, TRUE, TRUE
        );

        INSERT INTO uncertainty_artifact (
            artifact_id, source_season, source_model_version, source_reference,
            policy_version, interval_mass, minimum_segment_rows,
            minimum_segment_gameweeks, low_risk_rmse_threshold,
            high_risk_rmse_threshold, segment_rows, status
        ) VALUES (
            'uncertainty_{gameweek}', '2025-26', 'test_v1', 'ref.json', 'test', 0.8, 100, 5,
            2.4, 3.1, 100, 'shadow'
        );
        INSERT INTO model_uncertainty_lineage (
            model_run_id, artifact_id, application_mode
        ) VALUES ('{model_run_id}', 'uncertainty_{gameweek}', 'shadow');

        INSERT INTO shadow_calibration_artifact (
            artifact_id, calibration_type, source_season, source_model_version,
            source_reference, training_rows, training_gameweeks, slope,
            intercept, policy_version, status
        ) VALUES (
            'calibration_{gameweek}', 'xpts', '2025-26', 'test_v1', 'ref.json', 100, 5,
            0.9, 0.1, 'test', 'shadow'
        );
        INSERT INTO model_shadow_calibration_lineage (
            model_run_id, artifact_id
        ) VALUES ('{model_run_id}', 'calibration_{gameweek}');

        INSERT INTO player_fixture_projection VALUES
            ('{model_run_id}', 1001, {100 + gameweek}, 1, 2, TRUE,
             0.9, 0.05, 85.0, 5.0, 0.0, 5.0, 0.5, '[]'),
            ('{model_run_id}', 1002, {100 + gameweek}, 2, 1, FALSE,
             0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, '[]');
        """
    )


@pytest.fixture
def three_gameweek_release_db(tmp_path):
    database_path = tmp_path / "release.duckdb"
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('official', 'official_fpl_api', '2026-08-18T09:00:00+07:00', 'completed');

            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES
                ('official', '2026-27', 1, 1001, 'A', 'Starter', 'Starter', 1, 'MID', 8.0, 'a'),
                ('official', '2026-27', 2, 1002, 'B', 'Injured', 'Injured', 2, 'DEF', 5.0, 'i');

            INSERT INTO team_snapshot (
                ingestion_run_id, team_id, team_code, name, short_name, unavailable
            ) VALUES
                ('official', 1, 101, 'Home', 'HOM', false),
                ('official', 2, 102, 'Away', 'AWY', false);
            """
        )
        for gameweek, model_run_id in ((1, "model_gw1"), (2, "model_gw2"), (3, "model_gw3")):
            connection.execute(
                """
                INSERT INTO model_run (
                    model_run_id, target_gameweek, as_of, deadline, model_version,
                    source_ingestion_run_id, status
                ) VALUES (?, ?, '2026-08-18T09:00:00+07:00', ?, 'test_v1', 'official', 'completed')
                """,
                [model_run_id, gameweek, f"2026-08-{18 + 4 * gameweek:02d}T00:30:00+07:00"],
            )
            _seed_one_gameweek(connection, gameweek=gameweek, model_run_id=model_run_id)

    # _add_role_state_lineage opens its own connection, so it must run after
    # the block above releases this one -- also seeds baseline_projection_run,
    # which player_fixture_projection above does not depend on (it only
    # references model_run directly).
    for gameweek, model_run_id in ((1, "model_gw1"), (2, "model_gw2"), (3, "model_gw3")):
        _add_role_state_lineage(
            database_path,
            model_run_id=model_run_id,
            source_ingestion_run_id="official",
            gameweek=gameweek,
        )
    return database_path


def test_build_web_release_attaches_role_state_per_gameweek(three_gameweek_release_db):
    export = build_web_release(
        model_run_ids=("model_gw1", "model_gw2", "model_gw3"),
        database_path=three_gameweek_release_db,
    )

    players = {row["fpl_id"]: row for row in export.payload["players"]}
    starter_gw1 = players[1]["gameweeks"]["1"]["role_state"]
    injured_gw1 = players[2]["gameweeks"]["1"]["role_state"]
    assert starter_gw1["role_state"] == LIKELY_STARTER
    assert injured_gw1["role_state"] == UNAVAILABLE
    # role_state is attached independently for every Gameweek in the horizon.
    for gameweek in ("1", "2", "3"):
        assert players[1]["gameweeks"][gameweek]["role_state"] is not None


def test_build_web_release_is_a_valid_signed_release(three_gameweek_release_db):
    export = build_web_release(
        model_run_ids=("model_gw1", "model_gw2", "model_gw3"),
        database_path=three_gameweek_release_db,
    )

    assert export.payload["schema_version"] == "fpl_web_release_v1"
    assert export.health == "shadow"
    assert export.release_id.startswith("web_release_")
    assert export.payload["release"]["start_gameweek"] == 1
    assert export.payload["release"]["end_gameweek"] == 3


def test_build_web_release_reports_coverage_and_freshness(three_gameweek_release_db):
    export = build_web_release(
        model_run_ids=("model_gw1", "model_gw2", "model_gw3"),
        database_path=three_gameweek_release_db,
    )

    coverage = export.payload["release"]["coverage"]
    # Both fixture players (fpl_id 1 and 2) have a projection in all three
    # horizon Gameweeks -- see _seed_one_gameweek's own player_fixture_projection
    # rows, inserted once per Gameweek for both players.
    assert coverage["total_registered_players"] == 2
    assert coverage["fully_covered_players"] == 2
    assert coverage["excluded_missing_projection"] == 0
    assert coverage["excluded_partial_horizon_coverage"] == 0

    freshness = export.payload["release"]["freshness"]
    assert freshness["passes"] is True
    assert freshness["problems"] == []
    assert len(freshness["gameweeks"]) == 3


def test_build_web_release_excludes_a_player_with_partial_horizon_coverage(
    three_gameweek_release_db,
):
    """Regression case: a player projected in only 2 of 3 horizon Gameweeks
    (a postponed or blank fixture in the third) must be excluded from the
    release rather than crash the export or ship an incomplete entry
    load_release_catalog's own read side would reject."""
    with duckdb.connect(str(three_gameweek_release_db)) as connection:
        connection.execute(
            """
            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES ('official', '2026-27', 3, 1003, 'C', 'Partial', 'Partial', 1, 'FWD', 6.0, 'a');

            -- Only GW1 and GW2 -- no GW3 row for player_code 1003 at all.
            INSERT INTO player_fixture_projection VALUES
                ('model_gw1', 1003, 101, 1, 2, TRUE, 0.9, 0.05, 85.0, 5.0, 0.0, 5.0, 0.5, '[]'),
                ('model_gw2', 1003, 102, 1, 2, TRUE, 0.9, 0.05, 85.0, 5.0, 0.0, 5.0, 0.5, '[]');
            """
        )

    export = build_web_release(
        model_run_ids=("model_gw1", "model_gw2", "model_gw3"),
        database_path=three_gameweek_release_db,
    )

    fpl_ids = {row["fpl_id"] for row in export.payload["players"]}
    assert 3 not in fpl_ids
    coverage = export.payload["release"]["coverage"]
    assert coverage["total_registered_players"] == 3
    assert coverage["fully_covered_players"] == 2
    assert coverage["excluded_partial_horizon_coverage"] == 1
