"""DuckDB schema for deadline-safe local snapshots and projections."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

DEFAULT_DATABASE_PATH = Path("data/processed/fpl_model.duckdb")
SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS ingestion_run (
    ingestion_run_id VARCHAR PRIMARY KEY,
    source VARCHAR NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    source_as_of TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    status VARCHAR NOT NULL,
    raw_payload_path VARCHAR,
    payload_sha256 VARCHAR,
    CHECK (status IN ('running', 'completed', 'failed'))
);

CREATE TABLE IF NOT EXISTS player_snapshot (
    ingestion_run_id VARCHAR NOT NULL REFERENCES ingestion_run(ingestion_run_id),
    season VARCHAR NOT NULL,
    fpl_id INTEGER NOT NULL,
    player_code BIGINT,
    first_name VARCHAR NOT NULL,
    second_name VARCHAR NOT NULL,
    web_name VARCHAR NOT NULL,
    team_id INTEGER NOT NULL,
    fpl_position VARCHAR NOT NULL,
    price DECIMAL(5, 1) NOT NULL,
    fpl_status VARCHAR NOT NULL,
    chance_of_playing_this_round SMALLINT,
    chance_of_playing_next_round SMALLINT,
    news VARCHAR,
    news_added TIMESTAMPTZ,
    PRIMARY KEY (ingestion_run_id, fpl_id),
    CHECK (chance_of_playing_this_round BETWEEN 0 AND 100),
    CHECK (chance_of_playing_next_round BETWEEN 0 AND 100)
);

CREATE TABLE IF NOT EXISTS fixture_snapshot (
    ingestion_run_id VARCHAR NOT NULL REFERENCES ingestion_run(ingestion_run_id),
    fixture_id INTEGER NOT NULL,
    gameweek INTEGER,
    kickoff_time TIMESTAMPTZ,
    home_team_id INTEGER NOT NULL,
    away_team_id INTEGER NOT NULL,
    started BOOLEAN NOT NULL,
    finished BOOLEAN NOT NULL,
    PRIMARY KEY (ingestion_run_id, fixture_id)
);

CREATE TABLE IF NOT EXISTS availability_signal (
    signal_id VARCHAR PRIMARY KEY,
    player_code BIGINT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    effective_from TIMESTAMPTZ,
    effective_until TIMESTAMPTZ,
    source VARCHAR NOT NULL,
    signal_type VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    availability_probability DOUBLE,
    note VARCHAR,
    source_reference VARCHAR,
    CHECK (signal_type IN ('injury', 'suspension', 'eligibility', 'registration', 'selection')),
    CHECK (availability_probability BETWEEN 0.0 AND 1.0)
);

CREATE TABLE IF NOT EXISTS model_run (
    model_run_id VARCHAR PRIMARY KEY,
    target_gameweek INTEGER NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    deadline TIMESTAMPTZ NOT NULL,
    model_version VARCHAR NOT NULL,
    source_ingestion_run_id VARCHAR NOT NULL REFERENCES ingestion_run(ingestion_run_id),
    status VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    completed_at TIMESTAMPTZ,
    CHECK (as_of <= deadline),
    CHECK (status IN ('running', 'completed', 'failed'))
);

CREATE TABLE IF NOT EXISTS player_fixture_projection (
    model_run_id VARCHAR NOT NULL REFERENCES model_run(model_run_id),
    player_code BIGINT NOT NULL,
    fixture_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    opponent_team_id INTEGER NOT NULL,
    is_home BOOLEAN NOT NULL,
    start_probability DOUBLE NOT NULL,
    substitute_appearance_probability DOUBLE NOT NULL,
    expected_minutes DOUBLE NOT NULL,
    baseline_xpts DOUBLE NOT NULL,
    context_adjustment DOUBLE NOT NULL DEFAULT 0.0,
    final_xpts DOUBLE NOT NULL,
    uncertainty DOUBLE,
    data_quality_flags VARCHAR,
    PRIMARY KEY (model_run_id, player_code, fixture_id),
    CHECK (start_probability BETWEEN 0.0 AND 1.0),
    CHECK (substitute_appearance_probability BETWEEN 0.0 AND 1.0),
    CHECK (start_probability + substitute_appearance_probability <= 1.0),
    CHECK (expected_minutes BETWEEN 0.0 AND 130.0)
);

CREATE TABLE IF NOT EXISTS projection_component (
    model_run_id VARCHAR NOT NULL,
    player_code BIGINT NOT NULL,
    fixture_id INTEGER NOT NULL,
    component VARCHAR NOT NULL,
    expected_points DOUBLE NOT NULL,
    PRIMARY KEY (model_run_id, player_code, fixture_id, component),
    FOREIGN KEY (model_run_id, player_code, fixture_id)
        REFERENCES player_fixture_projection(model_run_id, player_code, fixture_id),
    CHECK (component IN (
        'appearance', 'sixty_minutes', 'goals', 'assists', 'clean_sheets',
        'goals_conceded', 'saves', 'yellow_cards', 'red_cards', 'bonus', 'defcon'
    ))
);
"""


@dataclass(frozen=True, slots=True)
class DatabaseInfo:
    path: Path
    schema_version: int
    tables: tuple[str, ...]


def initialize_database(path: str | Path = DEFAULT_DATABASE_PATH) -> DatabaseInfo:
    """Create or upgrade the local database without loading any live data."""
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    with duckdb.connect(str(database_path)) as connection:
        connection.execute(SCHEMA_SQL)
        connection.execute(
            "INSERT INTO schema_version (version) VALUES (?) ON CONFLICT DO NOTHING",
            [SCHEMA_VERSION],
        )
        tables = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'main' AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """
            ).fetchall()
        )
        version = connection.execute("SELECT max(version) FROM schema_version").fetchone()[0]

    return DatabaseInfo(
        path=database_path.resolve(),
        schema_version=int(version),
        tables=tables,
    )
