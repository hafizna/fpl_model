from __future__ import annotations

import json
from datetime import UTC, datetime

import duckdb
import pandas as pd
import pytest

from fpl_model.ingest.player_rate_evidence import (
    REQUIRED_COLUMNS,
    import_player_rate_evidence,
    validate_player_rate_evidence,
)
from fpl_model.storage import initialize_database


def _row(**overrides):
    row = {
        "fpl_id": 1,
        "player_code": 1001,
        "player_name": "Academy Player",
        "position": "MID",
        "comparability_class": "academy_youth",
        "source_competition": "Premier League 2",
        "source_season": "2025-26",
        "sample_minutes": 600,
        "sample_starts": 7,
        "expected_goals": 2.1,
        "expected_assists": 1.4,
        "saves": None,
        "yellow_cards": 2,
        "red_cards": 0,
        "bonus": None,
        "bps": -3,
        "defensive_contribution": None,
        "observed_at": "2026-08-19T12:00:00+07:00",
        "source_reference": "https://example.test/academy-player",
        "rationale": "Targeted evidence for a new senior-squad candidate.",
    }
    row.update(overrides)
    return row


def _seed(database_path):
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('snapshot', 'fpl_api', '2026-08-20T09:00:00+07:00', 'completed')
            """
        )
        connection.executemany(
            """
            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES ('snapshot', '2026-27', ?, ?, ?, 'Player', ?, 1, ?, 5.0, 'a')
            """,
            [
                (1, 1001, "Academy", "Academy Player", "MID"),
                (2, 1002, "Senior", "Senior Player", "DEF"),
            ],
        )


def test_validation_preserves_partial_academy_evidence_and_signed_bps():
    result = validate_player_rate_evidence(pd.DataFrame([_row()]))

    assert tuple(result.columns) == REQUIRED_COLUMNS
    assert result.loc[0, "sample_minutes"] == 600
    assert result.loc[0, "bps"] == -3
    assert pd.isna(result.loc[0, "saves"])


def test_role_only_evidence_rejects_rate_statistics():
    row = _row(
        comparability_class="role_only",
        source_competition="",
        source_season="",
    )
    with pytest.raises(ValueError, match="role_only evidence must not contain rate statistics"):
        validate_player_rate_evidence(pd.DataFrame([row]))

    for column in (
        "sample_minutes",
        "sample_starts",
        "expected_goals",
        "expected_assists",
        "saves",
        "yellow_cards",
        "red_cards",
        "bonus",
        "bps",
        "defensive_contribution",
    ):
        row[column] = None
    result = validate_player_rate_evidence(pd.DataFrame([row]))
    assert result.loc[0, "comparability_class"] == "role_only"


def test_import_is_idempotent_snapshot_linked_and_does_not_create_production_rates(tmp_path):
    database_path = tmp_path / "fpl.duckdb"
    _seed(database_path)
    csv_path = tmp_path / "evidence.csv"
    pd.DataFrame([_row()]).to_csv(csv_path, index=False)
    imported_at = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

    first = import_player_rate_evidence(
        csv_path,
        source_ingestion_run_id="snapshot",
        target_gameweek=1,
        source_label="manual targeted research",
        database_path=database_path,
        imported_at=imported_at,
    )
    second = import_player_rate_evidence(
        csv_path,
        source_ingestion_run_id="snapshot",
        target_gameweek=1,
        source_label="manual targeted research",
        database_path=database_path,
        imported_at=imported_at,
    )

    assert first == second
    with duckdb.connect(str(database_path), read_only=True) as connection:
        row = connection.execute(
            """
            SELECT comparability_class, saves, bps, data_quality_flags
            FROM player_rate_evidence
            """
        ).fetchone()
        production_rate_rows = connection.execute(
            "SELECT count(*) FROM player_rate_history"
        ).fetchone()[0]
    assert row[:3] == ("academy_youth", None, -3)
    assert set(json.loads(row[3])) == {
        "ACADEMY_YOUTH_EVIDENCE_NOT_SENIOR_RATE",
        "PARTIAL_RATE_STATISTICS",
        "RESEARCH_EVIDENCE_NOT_PRODUCTION_RATE",
    }
    assert production_rate_rows == 0


def test_import_rejects_identity_drift_from_pinned_snapshot(tmp_path):
    database_path = tmp_path / "fpl.duckdb"
    _seed(database_path)
    csv_path = tmp_path / "evidence.csv"
    pd.DataFrame([_row(player_name="Wrong Name")]).to_csv(csv_path, index=False)

    with pytest.raises(ValueError, match="identity does not match"):
        import_player_rate_evidence(
            csv_path,
            source_ingestion_run_id="snapshot",
            target_gameweek=1,
            source_label="manual targeted research",
            database_path=database_path,
            imported_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        )

