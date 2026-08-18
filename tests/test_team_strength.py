from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pandas as pd
import pytest

from fpl_model.ingest.team_strength import (
    import_team_strength_history,
    materialize_preseason_team_strength,
    validate_team_strength_history,
)
from fpl_model.storage import initialize_database

TEAMS = (
    ("ARS", "Arsenal"),
    ("AVL", "Aston Villa"),
    ("BOU", "Bournemouth"),
    ("BRE", "Brentford"),
    ("BHA", "Brighton"),
    ("CHE", "Chelsea"),
    ("COV", "Coventry City"),
    ("CRY", "Crystal Palace"),
    ("EVE", "Everton"),
    ("FUL", "Fulham"),
    ("HUL", "Hull City"),
    ("IPS", "Ipswich Town"),
    ("LEE", "Leeds"),
    ("LIV", "Liverpool"),
    ("MCI", "Man City"),
    ("MUN", "Man Utd"),
    ("NEW", "Newcastle"),
    ("NFO", "Nott'm Forest"),
    ("TOT", "Spurs"),
    ("SUN", "Sunderland"),
)
PROMOTED = {"COV", "HUL", "IPS"}


def _team_history() -> pd.DataFrame:
    rows = []
    for index, (abbreviation, name) in enumerate(TEAMS, start=1):
        promoted = abbreviation in PROMOTED
        long_xg_rate = 1.0 + index / 100
        long_xgc_rate = 1.1 + index / 100
        if abbreviation == "IPS":
            long_xg_rate = 1.143972
            long_xgc_rate = 1.479448
        short_matches = 38 if promoted else 6
        short_xg_rate = long_xg_rate if promoted else long_xg_rate + 0.1
        short_xgc_rate = long_xgc_rate if promoted else long_xgc_rate + 0.05
        rows.append(
            {
                "team_abbreviation": abbreviation,
                "team_name": name,
                "prior_type": (
                    "promoted_team_prior" if promoted else "observed_previous_pl"
                ),
                "long_form_matches": 38,
                "long_form_xg": long_xg_rate * 38,
                "long_form_xgc": long_xgc_rate * 38,
                "short_form_matches": short_matches,
                "short_form_xg": short_xg_rate * short_matches,
                "short_form_xgc": short_xgc_rate * short_matches,
                "league_average_xg_per_match": 1.5294868421052636,
                "league_average_xgc_per_match": 1.5294605263157894,
            }
        )
    return pd.DataFrame(rows)


def _insert_fpl_snapshot(database_path) -> None:
    initialize_database(database_path)
    captured = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    deadline = datetime(2026, 8, 22, 17, 30, tzinfo=UTC)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('fpl-run', 'official_fpl_api', ?, 'completed')
            """,
            [captured],
        )
        connection.execute(
            """
            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES (
                'fpl-run', '2026-27', 1, 1001, 'Test', 'Player', 'Player',
                1, 'MID', 5.0, 'a'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO gameweek_snapshot VALUES (
                'fpl-run', 1, 'Gameweek 1', ?, NULL,
                false, false, false, false, true
            )
            """,
            [deadline],
        )
        connection.executemany(
            """
            INSERT INTO team_snapshot VALUES (
                'fpl-run', ?, ?, ?, ?, false,
                NULL, NULL, NULL, NULL, NULL, NULL, NULL
            )
            """,
            [
                (team_id, 100 + team_id, name, abbreviation)
                for team_id, (abbreviation, name) in enumerate(TEAMS, start=1)
            ],
        )


def test_team_history_validation_requires_explicit_promoted_priors():
    valid = validate_team_strength_history(_team_history())
    assert len(valid) == 20
    assert set(valid.loc[valid["prior_type"] == "promoted_team_prior", "team_abbreviation"]) == PROMOTED

    missing_prior = _team_history()
    missing_prior.loc[missing_prior["team_abbreviation"] == "COV", "prior_type"] = (
        "observed_previous_pl"
    )
    with pytest.raises(ValueError, match="exactly three promoted priors"):
        validate_team_strength_history(missing_prior)

    inconsistent = _team_history()
    inconsistent.loc[inconsistent["team_abbreviation"] == "IPS", "short_form_xg"] += 1
    with pytest.raises(ValueError, match="long/short xg rates"):
        validate_team_strength_history(inconsistent)


def test_team_strength_import_and_materialization_are_idempotent(tmp_path):
    csv_path = tmp_path / "team_strength.csv"
    _team_history().to_csv(csv_path, index=False)
    database_path = tmp_path / "fpl.duckdb"
    _insert_fpl_snapshot(database_path)
    imported_at = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)

    first = import_team_strength_history(
        csv_path,
        target_season="2026-27",
        previous_season="2025-26",
        source_label="MODEL.xlsx TABLES resolved team windows",
        imported_at=imported_at,
        database_path=database_path,
    )
    second = import_team_strength_history(
        csv_path,
        target_season="2026-27",
        previous_season="2025-26",
        source_label="MODEL.xlsx TABLES resolved team windows",
        imported_at=imported_at,
        database_path=database_path,
    )
    materialized = materialize_preseason_team_strength(
        source_import_run_id=first.import_run_id,
        database_path=database_path,
    )
    repeated = materialize_preseason_team_strength(
        source_import_run_id=first.import_run_id,
        database_path=database_path,
    )

    assert first == second
    assert materialized == repeated
    assert materialized.team_rows == 20
    with duckdb.connect(str(database_path), read_only=True) as connection:
        ipswich = connection.execute(
            """
            SELECT long_form_xg_per_match, short_form_xg_per_match,
                   long_form_xgc_per_match, short_form_xgc_per_match,
                   corrected_xgc_per_match, is_promoted_prior,
                   data_quality_flags
            FROM team_strength_projection
            WHERE strength_run_id = ? AND team_abbreviation = 'IPS'
            """,
            [materialized.strength_run_id],
        ).fetchone()

    assert ipswich[0] == pytest.approx(1.143972)
    assert ipswich[1] == pytest.approx(1.143972)
    assert ipswich[2] == pytest.approx(1.479448)
    assert ipswich[3] == pytest.approx(1.479448)
    assert ipswich[4] == pytest.approx(1.4094362167912726)
    assert ipswich[5] is True
    assert "PROMOTED_TEAM_PRIOR" in ipswich[6]


def test_materializer_rejects_team_mapping_gap(tmp_path):
    csv_path = tmp_path / "team_strength.csv"
    history = _team_history()
    history.loc[history["team_abbreviation"] == "ARS", "team_abbreviation"] = "ABC"
    history.to_csv(csv_path, index=False)
    database_path = tmp_path / "fpl.duckdb"
    _insert_fpl_snapshot(database_path)
    imported = import_team_strength_history(
        csv_path,
        target_season="2026-27",
        previous_season="2025-26",
        source_label="test",
        database_path=database_path,
    )

    with pytest.raises(ValueError, match="current-team mismatch"):
        materialize_preseason_team_strength(
            source_import_run_id=imported.import_run_id,
            database_path=database_path,
        )
