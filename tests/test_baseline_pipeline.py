from __future__ import annotations

import json

import duckdb
import pandas as pd
import pytest

from fpl_model.model.baseline_pipeline import (
    materialize_frozen_projection_horizon,
    materialize_preseason_baseline,
)
from fpl_model.storage import initialize_database
from fpl_model.validation.gap_triage import (
    export_player_rate_evidence_template,
    export_preseason_rate_gap_triage,
    preseason_rate_gap_triage,
)


def _seed_baseline_inputs(database_path) -> None:
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('snapshot', 'fpl_api', '2026-08-18T09:00:00+07:00', 'completed');

            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES
                ('snapshot', '2026-27', 1, 519634, 'Jenson', 'Seelt', 'Seelt',
                 1, 'DEF', 4.0, 'a'),
                ('snapshot', '2026-27', 2, 999999, 'New', 'Player', 'New Player',
                 1, 'MID', 5.0, 'a');

            INSERT INTO team_snapshot (
                ingestion_run_id, team_id, team_code, name, short_name, unavailable
            ) VALUES
                ('snapshot', 1, 101, 'Sunderland', 'SUN', false),
                ('snapshot', 2, 102, 'Opponent', 'OPP', false);

            INSERT INTO fixture_snapshot VALUES (
                'snapshot', 100, 1, '2026-08-22T15:00:00+01:00',
                1, 2, false, false
            );

            INSERT INTO availability_resolution_run (
                resolution_run_id, source_ingestion_run_id, target_gameweek,
                as_of, deadline, policy_version, status
            ) VALUES (
                'availability', 'snapshot', 1, '2026-08-18T09:00:00+07:00',
                '2026-08-22T00:30:00+07:00', 'test', 'completed'
            );

            INSERT INTO appearance_history_import_run VALUES (
                'appearance_history', '2025-26', 'test', 'test.csv', 'sha',
                '2026-08-18T09:00:00+07:00', 1, 'completed'
            );
            INSERT INTO player_appearance_history VALUES (
                'appearance_history', 519634, 'Jenson Seelt', 1, 1, 1, 52.0, 82.0
            );

            INSERT INTO appearance_projection_run (
                projection_run_id, availability_resolution_run_id,
                appearance_history_import_run_id, target_gameweek,
                policy_version, status
            ) VALUES (
                'appearance', 'availability', 'appearance_history', 1,
                'test', 'completed'
            );
            INSERT INTO player_appearance_projection VALUES
                ('appearance', 1, 519634, 1.0, 0.5, 0.25, 0.75, 0.3,
                 46.5, 0.75, 0.3, 1.05, '[]'),
                ('appearance', 2, 999999, 1.0, 0.5, 0.25, 0.75, 0.3,
                 35.0, 0.75, 0.3, 1.05, '[]');

            INSERT INTO player_fixture_history_import_run VALUES (
                'fixture_history', '2025-26', 'vaastav', 'revision',
                '2026-06-01T00:00:00+00:00', '2026-08-18T09:00:00+07:00',
                'players.csv', 'gws.csv', 'players-sha', 'gws-sha', 1, 1, 0,
                'completed'
            );
            INSERT INTO player_rate_history_run (
                rate_run_id, source_import_run_id, long_form_gameweeks,
                short_form_gameweeks, defcon_short_form_gameweeks,
                policy_version, player_rows, status
            ) VALUES ('rates', 'fixture_history', 38, 6, 10, 'test', 1, 'completed');
            INSERT INTO player_rate_history VALUES (
                'rates', 519634, 'Jenson Seelt', 'DEF', 133, 1, 0, 0, 0, 0, 10,
                133, 0.0, 0.02, 133, 0.0, 0.02,
                133, 10, 133, 10, '[]'
            );

            INSERT INTO team_strength_import_run VALUES (
                'strength_import', '2026-27', '2025-26', 'test', 'strength.csv',
                'strength-sha', '2026-08-18T09:00:00+07:00', 20, 'completed'
            );
            INSERT INTO team_strength_run (
                strength_run_id, source_import_run_id, source_ingestion_run_id,
                target_gameweek, policy_version, team_rows, status
            ) VALUES (
                'strength', 'strength_import', 'snapshot', 1, 'test', 20, 'completed'
            );
            INSERT INTO team_strength_projection VALUES
                ('strength', 1, 101, 'SUN', 'Sunderland', false,
                 1.4, 1.5, 1.42, 1.3, 1.4, 1.32, 1.28, 1.5, 1.5,
                 0.95, 0.9, 1.1, 1.05, '[]'),
                ('strength', 2, 102, 'OPP', 'Opponent', true,
                 1.2, 1.3, 1.22, 1.5, 1.6, 1.52, 1.48, 1.5, 1.5,
                 0.81, 1.1, 0.95, 0.98, '[]');
            """
        )


def test_materialize_preseason_baseline_records_lineage_components_and_gaps(
    tmp_path,
):
    database_path = tmp_path / "baseline.duckdb"
    _seed_baseline_inputs(database_path)

    result = materialize_preseason_baseline(
        appearance_projection_run_id="appearance",
        player_rate_run_id="rates",
        team_strength_run_id="strength",
        database_path=database_path,
    )
    repeated = materialize_preseason_baseline(
        appearance_projection_run_id="appearance",
        player_rate_run_id="rates",
        team_strength_run_id="strength",
        database_path=database_path,
    )

    assert repeated == result
    assert result.current_players == 2
    assert result.candidate_fixture_rows == 2
    assert result.projected_fixture_rows == 1
    assert result.gap_players == 1
    assert result.status == "completed_with_gaps"

    with duckdb.connect(str(database_path), read_only=True) as connection:
        projection = connection.execute(
            """
            SELECT expected_minutes, baseline_xpts, data_quality_flags
            FROM player_fixture_projection WHERE model_run_id = ?
            """,
            [result.model_run_id],
        ).fetchone()
        component_total, component_count = connection.execute(
            """
            SELECT sum(expected_points), count(*) FROM projection_component
            WHERE model_run_id = ?
            """,
            [result.model_run_id],
        ).fetchone()
        gap = connection.execute(
            """
            SELECT fpl_id, data_quality_flags FROM baseline_projection_gap
            WHERE model_run_id = ?
            """,
            [result.model_run_id],
        ).fetchone()

    assert projection[0] == pytest.approx(46.5)
    assert projection[1] == pytest.approx(component_total * 1.05)
    assert component_count == 11
    assert {
        "CAMEO_MINUTES_EXCEED_START_MINUTES",
        "OPPONENT_PROMOTED_PRIOR",
        "SPARSE_APPEARANCE_HISTORY",
    }.issubset(json.loads(projection[2]))
    assert gap[0] == 2
    assert set(json.loads(gap[1])) == {
        "MISSING_MINUTES_SCENARIO",
        "NO_PREVIOUS_PL_PLAYER_RATE_HISTORY",
    }


def test_preseason_baseline_rejects_in_season_gameweeks(tmp_path):
    with pytest.raises(ValueError, match="supports GW1 only"):
        materialize_preseason_baseline(
            target_gameweek=2,
            database_path=tmp_path / "baseline.duckdb",
        )


def test_materializes_three_fixture_gameweeks_from_one_frozen_preseason_input(tmp_path):
    database_path = tmp_path / "baseline.duckdb"
    _seed_baseline_inputs(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO gameweek_snapshot VALUES
                ('snapshot', 2, 'Gameweek 2', '2026-08-29T00:30:00+07:00',
                 NULL, FALSE, FALSE, FALSE, FALSE, TRUE),
                ('snapshot', 3, 'Gameweek 3', '2026-09-12T00:30:00+07:00',
                 NULL, FALSE, FALSE, FALSE, FALSE, TRUE);
            INSERT INTO fixture_snapshot VALUES
                ('snapshot', 101, 2, '2026-08-29T15:00:00+01:00',
                 2, 1, FALSE, FALSE),
                ('snapshot', 102, 3, '2026-09-12T15:00:00+01:00',
                 1, 2, FALSE, FALSE);
            """
        )
    anchor = materialize_preseason_baseline(
        appearance_projection_run_id="appearance",
        player_rate_run_id="rates",
        team_strength_run_id="strength",
        database_path=database_path,
    )

    horizon = materialize_frozen_projection_horizon(
        anchor_model_run_id=anchor.model_run_id,
        database_path=database_path,
    )
    repeated = materialize_frozen_projection_horizon(
        anchor_model_run_id=anchor.model_run_id,
        database_path=database_path,
    )

    assert repeated == horizon
    assert horizon.model_run_ids[0] == anchor.model_run_id
    assert [run.projected_fixture_rows for run in horizon.runs] == [1, 1, 1]
    with duckdb.connect(str(database_path), read_only=True) as connection:
        metadata = connection.execute(
            """
            SELECT target_gameweek, as_of, deadline, model_version,
                   source_ingestion_run_id
            FROM model_run
            WHERE model_run_id IN (?, ?, ?)
            ORDER BY target_gameweek
            """,
            list(horizon.model_run_ids),
        ).fetchall()
        projections = connection.execute(
            """
            SELECT m.target_gameweek, p.final_xpts, p.data_quality_flags
            FROM player_fixture_projection AS p
            JOIN model_run AS m USING (model_run_id)
            WHERE p.player_code = 519634
              AND p.model_run_id IN (?, ?, ?)
            ORDER BY m.target_gameweek
            """,
            list(horizon.model_run_ids),
        ).fetchall()

    assert [row[0] for row in metadata] == [1, 2, 3]
    assert len({row[1] for row in metadata}) == 1
    assert [row[2].date().isoformat() for row in metadata] == [
        "2026-08-22",
        "2026-08-29",
        "2026-09-12",
    ]
    assert len({row[3] for row in metadata}) == 1
    assert {row[4] for row in metadata} == {"snapshot"}
    assert projections[0][1] != pytest.approx(projections[1][1])
    assert "FROZEN_PRESEASON_INPUTS_FROM_GW1" not in json.loads(projections[0][2])
    assert "FROZEN_PRESEASON_INPUTS_FROM_GW1" in json.loads(projections[1][2])
    assert "FROZEN_PRESEASON_INPUTS_FROM_GW1" in json.loads(projections[2][2])


def test_frozen_horizon_requires_future_deadline_metadata(tmp_path):
    database_path = tmp_path / "baseline.duckdb"
    _seed_baseline_inputs(database_path)
    anchor = materialize_preseason_baseline(
        appearance_projection_run_id="appearance",
        player_rate_run_id="rates",
        team_strength_run_id="strength",
        database_path=database_path,
    )

    with pytest.raises(ValueError, match="no deadline for horizon GW2"):
        materialize_frozen_projection_horizon(
            anchor_model_run_id=anchor.model_run_id,
            database_path=database_path,
        )


def test_frozen_horizon_rejects_nonbaseline_or_old_policy_anchor_before_writing(tmp_path):
    database_path = tmp_path / "baseline.duckdb"
    _seed_baseline_inputs(database_path)
    anchor = materialize_preseason_baseline(
        appearance_projection_run_id="appearance",
        player_rate_run_id="rates",
        team_strength_run_id="strength",
        database_path=database_path,
    )
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            "UPDATE model_run SET model_version = 'old' WHERE model_run_id = ?",
            [anchor.model_run_id],
        )

    with pytest.raises(ValueError, match="current baseline policy"):
        materialize_frozen_projection_horizon(
            anchor_model_run_id=anchor.model_run_id,
            database_path=database_path,
        )
    with duckdb.connect(str(database_path), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM model_run").fetchone()[0] == 1

    with pytest.raises(ValueError, match="not a baseline model run"):
        materialize_frozen_projection_horizon(
            anchor_model_run_id="missing",
            database_path=database_path,
        )


def test_preseason_baseline_treats_zero_minute_rate_row_as_gap(tmp_path):
    database_path = tmp_path / "baseline.duckdb"
    _seed_baseline_inputs(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES (
                'snapshot', '2026-27', 3, 577974, 'Harry', 'Amass', 'Amass',
                1, 'DEF', 4.0, 'a'
            );
            INSERT INTO player_appearance_history VALUES (
                'appearance_history', 577974, 'Harry Amass', 0, 0, 0, 0.0, 0.0
            );
            INSERT INTO player_appearance_projection VALUES (
                'appearance', 3, 577974, 1.0, 0.0, 0.01, 0.01, 0.0,
                0.18, 0.01, 0.0, 0.01, '["ZERO_PRIOR_STARTS"]'
            );
            INSERT INTO player_rate_history VALUES (
                'rates', 577974, 'Harry Amass', 'DEF', 0, 0, 0, 0, 0, 0, 0,
                0, 0.0, 0.0, 0, 0.0, 0.0,
                0, 0, 0, 0,
                '["ZERO_DEFCON_HISTORY_MINUTES", "ZERO_LONG_FORM_MINUTES", "ZERO_PRIOR_STARTS"]'
            );
            """
        )

    result = materialize_preseason_baseline(
        appearance_projection_run_id="appearance",
        player_rate_run_id="rates",
        team_strength_run_id="strength",
        database_path=database_path,
    )

    with duckdb.connect(str(database_path), read_only=True) as connection:
        flags = connection.execute(
            """
            SELECT data_quality_flags FROM baseline_projection_gap
            WHERE model_run_id = ? AND fpl_id = 3
            """,
            [result.model_run_id],
        ).fetchone()[0]

    assert result.current_players == 3
    assert result.projected_fixture_rows == 1
    assert result.gap_players == 2
    assert set(json.loads(flags)) == {
        "NO_USABLE_PLAYER_RATE_HISTORY",
        "ZERO_DEFCON_HISTORY_MINUTES",
        "ZERO_LONG_FORM_MINUTES",
        "ZERO_PRIOR_STARTS",
    }

    triage = preseason_rate_gap_triage(
        database_path=database_path,
        model_run_id=result.model_run_id,
    )
    assert triage[["player_name", "rate_history_status"]].values.tolist() == [
        ["New Player", "missing_rate_row"],
        ["Amass", "zero_minute_placeholder"],
    ]
    output_path = tmp_path / "outputs" / "triage.csv"
    exported = export_preseason_rate_gap_triage(
        output_path,
        database_path=database_path,
        model_run_id=result.model_run_id,
    )
    assert exported.equals(triage)
    assert pd.read_csv(output_path)["player_name"].tolist() == [
        "New Player",
        "Amass",
    ]
    template_path = tmp_path / "outputs" / "evidence_template.csv"
    template = export_player_rate_evidence_template(
        template_path,
        database_path=database_path,
        model_run_id=result.model_run_id,
        limit=1,
    )
    assert template["player_name"].tolist() == ["New Player"]
    assert template["source_ingestion_run_id"].tolist() == ["snapshot"]
    assert pd.isna(template.loc[0, "comparability_class"])
    assert template_path.is_file()
    excluded = export_player_rate_evidence_template(
        tmp_path / "outputs" / "excluded.csv",
        database_path=database_path,
        model_run_id=result.model_run_id,
        excluded_teams=("sun",),
    )
    assert excluded.empty
