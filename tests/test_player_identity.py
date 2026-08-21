from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pandas as pd
import pytest

from fpl_model.ingest.player_identity import (
    build_player_identity_bridge,
    import_player_identity_bridge,
)
from fpl_model.storage import initialize_database


def _official_players() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "fpl_id": 1,
                "player_code": 101,
                "first_name": "Cole",
                "second_name": "Palmer",
            },
            {
                "fpl_id": 2,
                "player_code": 102,
                "first_name": "Reece",
                "second_name": "James",
            },
            {
                "fpl_id": 3,
                "player_code": 103,
                "first_name": "New",
                "second_name": "Signing",
            },
        ]
    )


def _vaastav_players() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"id": 700, "code": 101, "first_name": "Cole", "second_name": "Palmer"},
            {"id": 701, "code": 102, "first_name": "R.", "second_name": "James"},
            {"id": 702, "code": 104, "first_name": "Former", "second_name": "Player"},
        ]
    )


def test_bridge_uses_shared_code_and_keeps_gaps_explicit():
    result = build_player_identity_bridge(_official_players(), _vaastav_players())

    assert result.official_players == 3
    assert result.vaastav_players == 3
    assert result.matched_players == 2
    assert result.official_only_player_ids == (103,)
    assert result.vaastav_only_player_ids == (104,)
    assert result.name_mismatch_player_ids == (102,)

    matched = result.rows[result.rows["canonical_player_id"] == 101]
    assert matched["provider"].tolist() == ["official_fpl", "vaastav"]
    assert matched["provider_player_id"].tolist() == ["1", "700"]
    assert set(matched["match_method"]) == {"shared_player_code"}

    official_only = result.rows[
        (result.rows["canonical_player_id"] == 103)
        & (result.rows["provider"] == "official_fpl")
    ].iloc[0]
    assert official_only["match_method"] == "provider_code_only"
    assert official_only["data_quality_flags"] == '["MISSING_VAASTAV_ID"]'


def test_bridge_rejects_ambiguous_or_unrelated_ids():
    duplicated = _official_players()
    duplicated.loc[2, "player_code"] = 101
    with pytest.raises(ValueError, match="maps multiple players"):
        build_player_identity_bridge(duplicated, _vaastav_players())

    unrelated = _vaastav_players().copy()
    unrelated["code"] = [201, 202, 203]
    with pytest.raises(ValueError, match="no shared player_code"):
        build_player_identity_bridge(_official_players(), unrelated)


def test_bridge_import_is_immutable_and_idempotent(tmp_path):
    database_path = tmp_path / "model.duckdb"
    csv_path = tmp_path / "players_raw.csv"
    _vaastav_players().to_csv(csv_path, index=False)
    initialize_database(database_path)

    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, completed_at, status
            ) VALUES (?, 'official_fpl_api', ?, ?, 'completed')
            """,
            [
                "fpl_snapshot",
                datetime(2026, 8, 1, tzinfo=UTC),
                datetime(2026, 8, 1, tzinfo=UTC),
            ],
        )
        connection.executemany(
            """
            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES ('fpl_snapshot', '2026-27', ?, ?, ?, ?, ?, 1, 'MID', 7.5, 'a')
            """,
            [
                (1, 101, "Cole", "Palmer", "Palmer"),
                (2, 102, "Reece", "James", "James"),
                (3, 103, "New", "Signing", "Signing"),
            ],
        )

    first = import_player_identity_bridge(
        csv_path,
        source_ingestion_run_id="fpl_snapshot",
        target_season="2026-27",
        vaastav_season="2025-26",
        source_revision="abc123",
        database_path=database_path,
    )
    second = import_player_identity_bridge(
        csv_path,
        source_ingestion_run_id="fpl_snapshot",
        target_season="2026-27",
        vaastav_season="2025-26",
        source_revision="abc123",
        database_path=database_path,
    )

    assert first == second
    assert first.status == "completed_with_gaps"
    assert first.matched_players == 2
    with duckdb.connect(str(database_path), read_only=True) as connection:
        run_count = connection.execute(
            "SELECT count(*) FROM player_identity_bridge_run"
        ).fetchone()[0]
        bridge_rows = connection.execute(
            "SELECT count(*) FROM player_identity_bridge"
        ).fetchone()[0]
    assert run_count == 1
    assert bridge_rows == 6
