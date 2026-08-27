"""Integration coverage: recommend_lineup.py's role_state wiring is correct.

Mirrors test_decision_transparency_wiring.py's approach: exercises the exact
per-squad role state lookup construction scripts/recommend_lineup.py uses,
against real store outputs plus a seeded availability/appearance lineage, so a
gameweek/fpl_id mismatch in the CLI wiring (not in role_state itself, which has
its own unit tests) would be caught.
"""

from __future__ import annotations

import duckdb

from fpl_model.decision.lineup_store import load_lineup_inputs
from fpl_model.validation.role_state import (
    LIKELY_STARTER,
    UNAVAILABLE,
    load_role_states,
    role_state_report,
)
from tests.test_lineup_store import _model_run
from tests.test_squad_snapshot import _database, _import


def _add_role_state_lineage(
    database_path, *, model_run_id: str, source_ingestion_run_id: str, gameweek: int
) -> None:
    """Seed the availability/appearance/baseline lineage load_role_states needs,
    layered on top of _model_run's own player_fixture_projection rows."""
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO availability_resolution_run (
                resolution_run_id, source_ingestion_run_id, target_gameweek,
                as_of, deadline, policy_version, status
            ) VALUES (
                'availability_' || ?, ?, ?, '2026-08-20T00:00:00+00:00',
                '2026-08-29T00:00:00+00:00', 'test', 'completed'
            )
            """,
            [model_run_id, source_ingestion_run_id, gameweek],
        )
        # fpl_id=1 resolved eligible; fpl_id=2 resolved ineligible (e.g. a
        # confirmed injury), regardless of what its own projection says.
        connection.execute(
            """
            INSERT INTO player_availability_resolution VALUES
                ('availability_' || ?, 1, 1001, 'a', NULL, 1.0, TRUE,
                 'official_fpl_status', NULL, 'test', '[]'),
                ('availability_' || ?, 2, 1002, 'i', 0, 0.0, FALSE,
                 'official_fpl_status', NULL, 'test', '[]')
            """,
            [model_run_id, model_run_id],
        )
        connection.execute(
            """
            INSERT INTO appearance_history_import_run VALUES (
                'appearance_history_' || ?, '2025-26', 'test', 'test.csv', 'sha',
                '2026-08-20T00:00:00+00:00', 1, 'completed'
            )
            """,
            [model_run_id],
        )
        connection.execute(
            """
            INSERT INTO appearance_projection_run (
                projection_run_id, availability_resolution_run_id,
                appearance_history_import_run_id, target_gameweek,
                policy_version, status
            ) VALUES (
                'appearance_' || ?, 'availability_' || ?, 'appearance_history_' || ?,
                ?, 'test', 'completed'
            )
            """,
            [model_run_id, model_run_id, model_run_id, gameweek],
        )
        connection.execute(
            """
            INSERT INTO player_fixture_history_import_run VALUES (
                'fixture_history_' || ?, '2025-26', 'vaastav', 'revision',
                '2026-06-01T00:00:00+00:00', '2026-08-18T09:00:00+00:00',
                'players.csv', 'gws.csv', 'players-sha', 'gws-sha', 1, 1, 0,
                'completed'
            )
            """,
            [model_run_id],
        )
        connection.execute(
            """
            INSERT INTO player_rate_history_run (
                rate_run_id, source_import_run_id, long_form_gameweeks,
                short_form_gameweeks, defcon_short_form_gameweeks,
                policy_version, player_rows, status
            ) VALUES ('rates_' || ?, 'fixture_history_' || ?, 38, 6, 10, 'test', 1, 'completed')
            """,
            [model_run_id, model_run_id],
        )
        connection.execute(
            """
            INSERT INTO team_strength_import_run VALUES (
                'strength_import_' || ?, '2026-27', '2025-26', 'test', 'strength.csv',
                'strength-sha', '2026-08-18T09:00:00+00:00', 20, 'completed'
            )
            """,
            [model_run_id],
        )
        connection.execute(
            """
            INSERT INTO team_strength_run (
                strength_run_id, source_import_run_id, source_ingestion_run_id,
                target_gameweek, policy_version, team_rows, status
            ) VALUES (
                'strength_' || ?, 'strength_import_' || ?, ?, ?, 'test', 20, 'completed'
            )
            """,
            [model_run_id, model_run_id, source_ingestion_run_id, gameweek],
        )
        connection.execute(
            """
            INSERT INTO baseline_projection_run (
                model_run_id, appearance_projection_run_id, player_rate_run_id,
                team_strength_run_id, policy_version, current_players,
                candidate_fixture_rows, projected_fixture_rows, gap_players, status
            ) VALUES (
                ?, 'appearance_' || ?, 'rates_' || ?, 'strength_' || ?, 'test',
                15, 15, 15, 0, 'completed'
            )
            """,
            [model_run_id, model_run_id, model_run_id, model_run_id],
        )


def test_recommend_lineup_wiring_attaches_role_state_per_squad_player(tmp_path):
    database_path = _database(tmp_path)
    imported = _import(tmp_path, database_path=database_path)
    _model_run(database_path)
    _add_role_state_lineage(
        database_path, model_run_id="model_gw1", source_ingestion_run_id="official", gameweek=1
    )

    with duckdb.connect(str(database_path)) as connection:
        inputs = load_lineup_inputs(
            connection, squad_snapshot_id=imported.squad_snapshot_id, model_run_id="model_gw1"
        )
        # Mirrors scripts/recommend_lineup.py's role_state wiring exactly.
        role_state_by_id = load_role_states(
            connection,
            model_run_id=inputs.model_run_id,
            fpl_ids=tuple(player.fpl_id for player in inputs.squad.players),
        )

    # fpl_id=1 has start_probability=0.8 in _model_run's own seed data and is
    # resolved eligible above -> LIKELY_STARTER.
    report_1 = role_state_report(role_state_by_id.get(1))
    assert report_1 is not None
    assert report_1["role_state"] == LIKELY_STARTER

    # fpl_id=2 is resolved ineligible above, overriding its own projection.
    report_2 = role_state_report(role_state_by_id.get(2))
    assert report_2 is not None
    assert report_2["role_state"] == UNAVAILABLE
