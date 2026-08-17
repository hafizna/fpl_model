from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pandas as pd
import pytest

from fpl_model.context.minutes import (
    create_appearance_scenario_override,
    store_appearance_scenario_override,
)
from fpl_model.ingest.appearance_history import (
    import_appearance_history_csv,
    validate_appearance_history,
)
from fpl_model.model.appearance import ConditionalAppearanceScenario
from fpl_model.model.appearance_pipeline import materialize_preseason_appearance
from fpl_model.storage import initialize_database


def _history_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "player_code": 154561,
                "player_name": "David Raya",
                "starts": 37,
                "substitute_appearances": 0,
                "unused_substitute": 0,
                "minutes_per_start": 90,
                "minutes_per_substitute": 0,
            },
            {
                "player_code": 201666,
                "player_name": "Harvey Barnes",
                "starts": 19,
                "substitute_appearances": 18,
                "unused_substitute": 1,
                "minutes_per_start": 79,
                "minutes_per_substitute": 26,
            },
        ]
    )


def test_appearance_history_validation_rejects_ambiguous_or_invalid_rows():
    duplicate = pd.concat([_history_frame(), _history_frame().iloc[[0]]])
    with pytest.raises(ValueError, match="duplicate player_code"):
        validate_appearance_history(duplicate)

    missing = _history_frame().drop(columns="unused_substitute")
    with pytest.raises(ValueError, match="missing columns"):
        validate_appearance_history(missing)

    invalid_minutes = _history_frame()
    invalid_minutes.loc[0, "minutes_per_start"] = 91
    with pytest.raises(ValueError, match="between 0 and 90"):
        validate_appearance_history(invalid_minutes)


def test_import_is_content_addressed_and_idempotent(tmp_path):
    csv_path = tmp_path / "appearance.csv"
    _history_frame().to_csv(csv_path, index=False)
    database_path = tmp_path / "fpl.duckdb"
    imported_at = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)

    first = import_appearance_history_csv(
        csv_path,
        season="2025-26",
        source_label="MODEL.xlsx resolved appearance fields",
        database_path=database_path,
        imported_at=imported_at,
    )
    second = import_appearance_history_csv(
        csv_path,
        season="2025-26",
        source_label="MODEL.xlsx resolved appearance fields",
        database_path=database_path,
        imported_at=imported_at,
    )

    assert first.import_run_id == second.import_run_id
    assert first.player_rows == 2
    with duckdb.connect(str(database_path), read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM appearance_history_import_run"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM player_appearance_history"
            ).fetchone()[0]
            == 2
        )


def _insert_availability_run(database_path) -> None:
    initialize_database(database_path)
    as_of = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    deadline = datetime(2026, 8, 22, 17, 30, tzinfo=UTC)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('fpl-source', 'official_fpl_api', ?, 'completed')
            """,
            [as_of],
        )
        connection.execute(
            """
            INSERT INTO availability_resolution_run (
                resolution_run_id, source_ingestion_run_id, target_gameweek,
                as_of, deadline, policy_version, status
            ) VALUES (
                'availability-run', 'fpl-source', 1, ?, ?, 'test-policy', 'completed'
            )
            """,
            [as_of, deadline],
        )
        connection.executemany(
            """
            INSERT INTO player_availability_resolution VALUES (
                'availability-run', ?, ?, 'a', NULL, ?, true,
                'official_fpl_status', NULL, 'test', '[]'
            )
            """,
            [
                (1, 154561, 1.0),
                (2, 201666, 0.75),
                (3, 999999, 1.0),
            ],
        )


def test_preseason_materializer_projects_known_history_and_flags_missing(tmp_path):
    csv_path = tmp_path / "appearance.csv"
    _history_frame().to_csv(csv_path, index=False)
    database_path = tmp_path / "fpl.duckdb"
    _insert_availability_run(database_path)
    import_appearance_history_csv(
        csv_path,
        season="2025-26",
        source_label="MODEL.xlsx resolved appearance fields",
        database_path=database_path,
        imported_at=datetime(2026, 8, 17, 10, 0, tzinfo=UTC),
    )

    result = materialize_preseason_appearance(
        target_gameweek=1,
        previous_season="2025-26",
        database_path=database_path,
    )

    assert result.players == 3
    assert result.projected_players == 2
    assert result.missing_players == 1
    assert result.status == "completed_with_gaps"
    with duckdb.connect(str(database_path), read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT player_code, start_probability, appearance_probability,
                   expected_minutes, data_quality_flags
            FROM player_appearance_projection
            ORDER BY player_code
            """
        ).fetchall()

    assert rows[0][0] == 154561
    assert rows[0][1] == pytest.approx(0.99)
    assert rows[0][2] == pytest.approx(0.99)
    assert rows[1][0] == 201666
    assert rows[1][1] == pytest.approx(0.375)
    assert rows[1][2] == pytest.approx(0.75 * (1 - 1 / 38) * 0.99)
    assert rows[1][3] > 0
    assert rows[2][0] == 999999
    assert rows[2][1] is None
    assert "NO_WORKBOOK_APPEARANCE_HISTORY" in rows[2][4]


def test_preseason_materializer_refuses_inseason_zero_history_assumption(tmp_path):
    with pytest.raises(ValueError, match="supports GW1 only"):
        materialize_preseason_appearance(
            target_gameweek=2,
            previous_season="2025-26",
            database_path=tmp_path / "fpl.duckdb",
        )


def test_reviewed_scenario_projects_player_missing_workbook_history(tmp_path):
    csv_path = tmp_path / "appearance.csv"
    _history_frame().to_csv(csv_path, index=False)
    database_path = tmp_path / "fpl.duckdb"
    _insert_availability_run(database_path)
    import_appearance_history_csv(
        csv_path,
        season="2025-26",
        source_label="MODEL.xlsx resolved appearance fields",
        database_path=database_path,
        imported_at=datetime(2026, 8, 17, 10, 0, tzinfo=UTC),
    )
    override = create_appearance_scenario_override(
        player_code=999999,
        target_gameweek=1,
        observed_at=datetime(2026, 8, 17, 8, 30, tzinfo=UTC),
        scenario=ConditionalAppearanceScenario(
            start_probability_if_available=0.6,
            substitute_probability_if_available=0.2,
            sixty_probability_given_start=0.8,
            minutes_per_start=75.0,
            minutes_per_substitute=20.0,
        ),
        source="reviewed_lineup_evidence",
        rationale="New signing absent from the workbook snapshot.",
    )
    stored = store_appearance_scenario_override(
        override,
        database_path=database_path,
    )

    result = materialize_preseason_appearance(
        target_gameweek=1,
        previous_season="2025-26",
        database_path=database_path,
    )

    assert stored.requires_pipeline_refresh is False
    assert result.projected_players == 3
    assert result.missing_players == 0
    assert result.status == "completed"
    with duckdb.connect(str(database_path), read_only=True) as connection:
        row = connection.execute(
            """
            SELECT start_probability, expected_minutes, data_quality_flags
            FROM player_appearance_projection WHERE player_code = 999999
            """
        ).fetchone()
    assert row[0] == pytest.approx(0.6)
    assert row[1] == pytest.approx(49.0)
    assert "REVIEWED_APPEARANCE_SCENARIO_OVERRIDE" in row[2]
    assert override.override_id in row[2]
