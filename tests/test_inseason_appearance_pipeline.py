from __future__ import annotations

import json
from datetime import UTC, datetime

import duckdb
import pytest

from fpl_model.context.pipeline import (
    ReviewedContextAnnotation,
    materialize_context_features,
    store_reviewed_context_annotation,
)
from fpl_model.model.appearance_pipeline import materialize_inseason_appearance
from fpl_model.storage import initialize_database


def _seed_inputs(database_path) -> None:
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('snapshot', 'official_fpl_api',
                      '2026-08-24T09:00:00+07:00', 'completed');
            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES
                ('snapshot', '2026-27', 1, 1001, 'Prior', 'Starter',
                 'Starter', 1, 'MID', 7.5, 'a'),
                ('snapshot', '2026-27', 2, 1002, 'New', 'Cameo',
                 'Cameo', 1, 'MID', 5.0, 'a');
            INSERT INTO player_status_snapshot VALUES
                ('snapshot', 1, TRUE, TRUE, FALSE, 0, 0, 0, 0, 0,
                 0, 0, 0, NULL, NULL, NULL),
                ('snapshot', 2, TRUE, TRUE, FALSE, 0, 0, 0, 0, 0,
                 0, 0, 0, NULL, NULL, NULL);
            INSERT INTO gameweek_snapshot VALUES
                ('snapshot', 1, 'Gameweek 1', '2026-08-22T00:30:00+07:00',
                 NULL, TRUE, TRUE, FALSE, TRUE, FALSE),
                ('snapshot', 2, 'Gameweek 2', '2026-08-29T00:30:00+07:00',
                 NULL, FALSE, FALSE, FALSE, FALSE, TRUE);
            INSERT INTO availability_resolution_run (
                resolution_run_id, source_ingestion_run_id, target_gameweek,
                as_of, deadline, policy_version, status
            ) VALUES (
                'availability', 'snapshot', 2,
                '2026-08-24T09:00:00+07:00',
                '2026-08-29T00:30:00+07:00', 'test', 'completed'
            );
            INSERT INTO player_availability_resolution VALUES
                ('availability', 1, 1001, 'a', NULL, 0.5, TRUE,
                 'official_fpl_status', NULL, 'test', '[]'),
                ('availability', 2, 1002, 'a', NULL, 1.0, TRUE,
                 'official_fpl_status', NULL, 'test', '[]');
            INSERT INTO appearance_history_import_run VALUES (
                'history', '2025-26', 'test', 'history.csv', 'sha',
                '2026-08-20T00:00:00+07:00', 1, 'completed'
            );
            INSERT INTO player_appearance_history VALUES (
                'history', 1001, 'Prior Starter', 30, 2, 2, 80.0, 15.0
            );
            INSERT INTO fpl_event_live_run VALUES (
                'live-gw1', 'snapshot', '2026-27', 1,
                '2026-08-24T10:00:00+07:00', 'event.json', 'sha-live',
                TRUE, TRUE, 2, 'completed', current_timestamp
            );
            INSERT INTO player_gameweek_stat VALUES
                ('live-gw1', 1, 1001, TRUE, 90, 1, 0, 0, 0, 0, 0,
                 0, 20, 5, 0.1, 0.1, 0.5, 3, FALSE, '[]'),
                ('live-gw1', 2, 1002, TRUE, 20, 0, 0, 0, 0, 0, 0,
                 0, 5, 1, 0.0, 0.0, 0.2, 1, FALSE, '[]');
            """
        )


def test_inseason_appearance_blends_final_history_and_preserves_lineage(tmp_path):
    database_path = tmp_path / "model.duckdb"
    _seed_inputs(database_path)

    result = materialize_inseason_appearance(
        target_gameweek=2,
        current_season="2026-27",
        previous_season="2025-26",
        previous_effective_fixtures=5.0,
        database_path=database_path,
    )
    repeated = materialize_inseason_appearance(
        target_gameweek=2,
        current_season="2026-27",
        previous_season="2025-26",
        previous_effective_fixtures=5.0,
        database_path=database_path,
    )

    assert repeated == result
    assert result.status == "completed"
    assert result.projected_players == 2
    with duckdb.connect(str(database_path), read_only=True) as connection:
        prior = connection.execute(
            """
            SELECT p.start_probability, p.expected_minutes, p.data_quality_flags,
                   c.current_fixture_rows, c.previous_weight, c.current_weight,
                   r.live_run_ids, r.as_of
            FROM player_appearance_projection AS p
            JOIN inseason_player_appearance_context AS c
              USING (projection_run_id, fpl_id)
            JOIN inseason_appearance_run AS r USING (projection_run_id)
            WHERE p.fpl_id = 1
            """
        ).fetchone()
        current_only = connection.execute(
            """
            SELECT start_probability, substitute_appearance_probability,
                   expected_minutes, data_quality_flags
            FROM player_appearance_projection WHERE fpl_id = 2
            """
        ).fetchone()

    assert prior[3:6] == pytest.approx((1, 5 / 6, 1 / 6))
    assert prior[6] == '["live-gw1"]'
    assert prior[7].astimezone(UTC).isoformat() == "2026-08-24T03:00:00+00:00"
    assert prior[0] <= 0.5
    assert prior[1] <= 45.0
    assert "SHRUNK_CURRENT_SEASON_APPEARANCE" in json.loads(prior[2])
    assert current_only[:3] == pytest.approx((0.0, 1.0, 20.0))
    assert "CURRENT_SEASON_APPEARANCE_ONLY" in json.loads(current_only[3])


def test_inseason_appearance_accepts_finished_fixture_provisional_run(tmp_path):
    database_path = tmp_path / "model.duckdb"
    _seed_inputs(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            UPDATE gameweek_snapshot
            SET finished = FALSE, data_checked = FALSE
            WHERE ingestion_run_id = 'snapshot' AND gameweek = 1;
            UPDATE fpl_event_live_run
            SET event_finished = FALSE, data_checked = FALSE, status = 'provisional'
            WHERE live_run_id = 'live-gw1';
            INSERT INTO fixture_snapshot VALUES (
                'snapshot', 100, 1, '2026-08-22T15:00:00+01:00',
                1, 2, TRUE, TRUE
            );
            """
        )

    appearance = materialize_inseason_appearance(
        target_gameweek=2,
        current_season="2026-27",
        previous_season="2025-26",
        database_path=database_path,
    )
    context = materialize_context_features(
        target_gameweek=2,
        appearance_projection_run_id=appearance.projection_run_id,
        database_path=database_path,
    )

    with duckdb.connect(str(database_path), read_only=True) as connection:
        appearance_flags = json.loads(
            connection.execute(
                """
                SELECT data_quality_flags FROM player_appearance_projection
                WHERE projection_run_id = ? AND fpl_id = 1
                """,
                [appearance.projection_run_id],
            ).fetchone()[0]
        )
        context_flags = json.loads(
            connection.execute(
                """
                SELECT data_quality_flags FROM player_context_feature
                WHERE context_run_id = ? AND fpl_id = 1
                """,
                [context.context_run_id],
            ).fetchone()[0]
        )

    expected_flag = "OFFICIAL_EVENT_ANALYTICALLY_COMPLETE_NOT_FINAL"
    assert expected_flag in appearance_flags
    assert expected_flag in context_flags


def test_inseason_appearance_requires_every_prior_analytically_complete_gameweek(
    tmp_path,
):
    database_path = tmp_path / "model.duckdb"
    _seed_inputs(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO availability_resolution_run (
                resolution_run_id, source_ingestion_run_id, target_gameweek,
                as_of, deadline, policy_version, status
            ) VALUES (
                'availability-gw3', 'snapshot', 3,
                '2026-08-24T09:00:00+07:00',
                '2026-09-05T00:30:00+07:00', 'test', 'completed'
            );
            INSERT INTO player_availability_resolution VALUES
                ('availability-gw3', 1, 1001, 'a', NULL, 1.0, TRUE,
                 'official_fpl_status', NULL, 'test', '[]'),
                ('availability-gw3', 2, 1002, 'a', NULL, 1.0, TRUE,
                 'official_fpl_status', NULL, 'test', '[]');
            """
        )

    with pytest.raises(ValueError, match=r"missing=\[2\]"):
        materialize_inseason_appearance(
            target_gameweek=3,
            current_season="2026-27",
            previous_season="2025-26",
            database_path=database_path,
        )


def test_context_features_are_deadline_safe_descriptive_and_idempotent(tmp_path):
    database_path = tmp_path / "model.duckdb"
    _seed_inputs(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO team_snapshot (
                ingestion_run_id, team_id, team_code, name, short_name, unavailable
            ) VALUES ('snapshot', 1, 101, 'Example FC', 'EXA', FALSE);
            INSERT INTO fixture_snapshot VALUES (
                'snapshot', 100, 1, '2026-08-22T15:00:00+01:00',
                1, 2, TRUE, TRUE
            );
            """
        )
    appearance = materialize_inseason_appearance(
        target_gameweek=2,
        current_season="2026-27",
        previous_season="2025-26",
        database_path=database_path,
    )

    base = {
        "source_reference": "https://example.test/evidence",
        "rationale": "reviewed test evidence",
    }
    annotations = (
        ReviewedContextAnnotation(
            subject_type="team",
            team_id=1,
            context_type="manager_regime",
            observed_at=datetime.fromisoformat("2026-08-20T09:00:00+07:00"),
            effective_from=datetime.fromisoformat("2026-06-01T00:00:00+07:00"),
            payload={"manager_name": "Manager One", "regime_start": "2026-06-01"},
            source_reference=base["source_reference"],
            rationale=base["rationale"],
        ),
        ReviewedContextAnnotation(
            subject_type="player",
            player_code=1001,
            context_type="readiness",
            observed_at=datetime.fromisoformat("2026-08-21T09:00:00+07:00"),
            effective_from=datetime.fromisoformat("2026-08-01T00:00:00+07:00"),
            payload={
                "tournament_minutes": 450,
                "last_tournament_match": "2026-07-19",
                "club_return_date": "2026-08-03",
                "preseason_minutes": 60,
            },
            source_reference=base["source_reference"],
            rationale=base["rationale"],
        ),
        ReviewedContextAnnotation(
            subject_type="player",
            player_code=1001,
            context_type="tactical_role",
            observed_at=datetime.fromisoformat("2026-08-10T09:00:00+07:00"),
            effective_from=datetime.fromisoformat("2026-08-01T00:00:00+07:00"),
            payload={
                "role_label": "wide midfielder",
                "nominal_position": "MID",
                "width": 0.8,
                "height": 0.6,
                "centrality": 0.2,
                "build_up": 0.5,
                "box_presence": 0.3,
                "defensive_load": 0.5,
            },
            source_reference=base["source_reference"],
            rationale=base["rationale"],
        ),
        ReviewedContextAnnotation(
            subject_type="player",
            player_code=1001,
            context_type="tactical_role",
            observed_at=datetime.fromisoformat("2026-08-25T09:00:00+07:00"),
            effective_from=datetime.fromisoformat("2026-08-23T00:00:00+07:00"),
            payload={
                "role_label": "inside forward",
                "nominal_position": "FWD",
                "width": 0.5,
                "height": 0.8,
                "centrality": 0.6,
                "build_up": 0.3,
                "box_presence": 0.8,
                "defensive_load": 0.2,
            },
            source_reference=base["source_reference"],
            rationale=base["rationale"],
        ),
    )
    for annotation in annotations:
        first = store_reviewed_context_annotation(annotation, database_path=database_path)
        repeated = store_reviewed_context_annotation(annotation, database_path=database_path)
        assert repeated == first

    result = materialize_context_features(
        target_gameweek=2,
        appearance_projection_run_id=appearance.projection_run_id,
        database_path=database_path,
    )
    repeated = materialize_context_features(
        target_gameweek=2,
        appearance_projection_run_id=appearance.projection_run_id,
        database_path=database_path,
    )

    assert repeated == result
    assert result.player_rows == 2
    assert result.fully_observed_rows == 1
    assert result.status == "completed_with_gaps"
    with duckdb.connect(str(database_path), read_only=True) as connection:
        row = connection.execute(
            """
            SELECT manager_name, tournament_minutes, preseason_minutes,
                   rest_days, minutes_last_7d, matches_last_7d,
                   tactical_role_label, tactical_role_distance,
                   nominal_position_changed, data_quality_flags
            FROM player_context_feature
            WHERE context_run_id = ? AND player_code = 1001
            """,
            [result.context_run_id],
        ).fetchone()

    assert row[0] == "Manager One"
    assert row[1:3] == pytest.approx((450.0, 60.0))
    assert row[3] > 5.0
    assert row[4:6] == pytest.approx((90.0, 1))
    assert row[6] == "inside forward"
    assert row[7] > 0.0
    assert row[8] is True
    assert "CONTEXT_FEATURES_DIAGNOSTIC_ONLY" in json.loads(row[9])
