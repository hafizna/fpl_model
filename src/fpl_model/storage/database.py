"""DuckDB schema for deadline-safe local snapshots and projections."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

DEFAULT_DATABASE_PATH = Path("data/processed/fpl_model.duckdb")
SCHEMA_VERSION = 8

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

CREATE TABLE IF NOT EXISTS player_status_snapshot (
    ingestion_run_id VARCHAR NOT NULL REFERENCES ingestion_run(ingestion_run_id),
    fpl_id INTEGER NOT NULL,
    can_select BOOLEAN NOT NULL,
    can_transact BOOLEAN NOT NULL,
    removed BOOLEAN NOT NULL,
    selected_by_percent DOUBLE NOT NULL,
    transfers_in BIGINT NOT NULL,
    transfers_in_event BIGINT NOT NULL,
    transfers_out BIGINT NOT NULL,
    transfers_out_event BIGINT NOT NULL,
    event_points INTEGER NOT NULL,
    total_points INTEGER NOT NULL,
    form DOUBLE NOT NULL,
    expected_points_this DOUBLE,
    expected_points_next DOUBLE,
    team_join_date DATE,
    PRIMARY KEY (ingestion_run_id, fpl_id),
    FOREIGN KEY (ingestion_run_id, fpl_id)
        REFERENCES player_snapshot(ingestion_run_id, fpl_id)
);

CREATE TABLE IF NOT EXISTS player_season_stat_snapshot (
    ingestion_run_id VARCHAR NOT NULL REFERENCES ingestion_run(ingestion_run_id),
    fpl_id INTEGER NOT NULL,
    minutes INTEGER NOT NULL,
    starts INTEGER NOT NULL,
    goals_scored INTEGER NOT NULL,
    assists INTEGER NOT NULL,
    clean_sheets INTEGER NOT NULL,
    goals_conceded INTEGER NOT NULL,
    saves INTEGER NOT NULL,
    yellow_cards INTEGER NOT NULL,
    red_cards INTEGER NOT NULL,
    bonus INTEGER NOT NULL,
    bps INTEGER NOT NULL,
    own_goals INTEGER NOT NULL,
    penalties_saved INTEGER NOT NULL,
    penalties_missed INTEGER NOT NULL,
    defensive_contribution INTEGER NOT NULL,
    clearances_blocks_interceptions INTEGER NOT NULL,
    recoveries INTEGER NOT NULL,
    tackles INTEGER NOT NULL,
    expected_goals DOUBLE NOT NULL,
    expected_assists DOUBLE NOT NULL,
    expected_goal_involvements DOUBLE NOT NULL,
    expected_goals_conceded DOUBLE NOT NULL,
    PRIMARY KEY (ingestion_run_id, fpl_id),
    FOREIGN KEY (ingestion_run_id, fpl_id)
        REFERENCES player_snapshot(ingestion_run_id, fpl_id)
);

CREATE TABLE IF NOT EXISTS team_snapshot (
    ingestion_run_id VARCHAR NOT NULL REFERENCES ingestion_run(ingestion_run_id),
    team_id INTEGER NOT NULL,
    team_code INTEGER NOT NULL,
    name VARCHAR NOT NULL,
    short_name VARCHAR NOT NULL,
    unavailable BOOLEAN NOT NULL,
    strength INTEGER,
    strength_overall_home INTEGER,
    strength_overall_away INTEGER,
    strength_attack_home INTEGER,
    strength_attack_away INTEGER,
    strength_defence_home INTEGER,
    strength_defence_away INTEGER,
    PRIMARY KEY (ingestion_run_id, team_id)
);

CREATE TABLE IF NOT EXISTS gameweek_snapshot (
    ingestion_run_id VARCHAR NOT NULL REFERENCES ingestion_run(ingestion_run_id),
    gameweek INTEGER NOT NULL,
    name VARCHAR NOT NULL,
    deadline_time TIMESTAMPTZ NOT NULL,
    release_time TIMESTAMPTZ,
    finished BOOLEAN NOT NULL,
    data_checked BOOLEAN NOT NULL,
    is_previous BOOLEAN NOT NULL,
    is_current BOOLEAN NOT NULL,
    is_next BOOLEAN NOT NULL,
    PRIMARY KEY (ingestion_run_id, gameweek)
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

CREATE TABLE IF NOT EXISTS availability_override (
    override_id VARCHAR PRIMARY KEY,
    player_code BIGINT NOT NULL,
    target_gameweek INTEGER NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    effective_until TIMESTAMPTZ,
    availability_probability DOUBLE,
    is_eligible BOOLEAN,
    source VARCHAR NOT NULL,
    rationale VARCHAR NOT NULL,
    CHECK (availability_probability BETWEEN 0.0 AND 1.0),
    CHECK (availability_probability IS NOT NULL OR is_eligible IS NOT NULL),
    CHECK (effective_until IS NULL OR effective_until >= observed_at)
);

CREATE TABLE IF NOT EXISTS availability_resolution_run (
    resolution_run_id VARCHAR PRIMARY KEY,
    source_ingestion_run_id VARCHAR NOT NULL REFERENCES ingestion_run(ingestion_run_id),
    target_gameweek INTEGER NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    deadline TIMESTAMPTZ NOT NULL,
    policy_version VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    UNIQUE (source_ingestion_run_id, target_gameweek, policy_version),
    CHECK (as_of <= deadline),
    CHECK (status IN ('completed', 'completed_with_gaps'))
);

CREATE TABLE IF NOT EXISTS player_availability_resolution (
    resolution_run_id VARCHAR NOT NULL
        REFERENCES availability_resolution_run(resolution_run_id),
    fpl_id INTEGER NOT NULL,
    player_code BIGINT,
    fpl_status VARCHAR NOT NULL,
    official_chance SMALLINT,
    availability_probability DOUBLE,
    is_eligible BOOLEAN,
    selected_source VARCHAR NOT NULL,
    selected_override_id VARCHAR REFERENCES availability_override(override_id),
    reason VARCHAR NOT NULL,
    data_quality_flags VARCHAR NOT NULL,
    PRIMARY KEY (resolution_run_id, fpl_id),
    CHECK (official_chance BETWEEN 0 AND 100),
    CHECK (availability_probability BETWEEN 0.0 AND 1.0)
);

CREATE TABLE IF NOT EXISTS appearance_history_import_run (
    import_run_id VARCHAR PRIMARY KEY,
    season VARCHAR NOT NULL,
    source_label VARCHAR NOT NULL,
    source_path VARCHAR NOT NULL,
    source_sha256 VARCHAR NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL,
    player_rows INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    CHECK (status = 'completed')
);

CREATE TABLE IF NOT EXISTS player_appearance_history (
    import_run_id VARCHAR NOT NULL
        REFERENCES appearance_history_import_run(import_run_id),
    player_code BIGINT NOT NULL,
    player_name VARCHAR NOT NULL,
    starts INTEGER NOT NULL,
    substitute_appearances INTEGER NOT NULL,
    unused_substitute INTEGER NOT NULL,
    minutes_per_start DOUBLE NOT NULL,
    minutes_per_substitute DOUBLE NOT NULL,
    PRIMARY KEY (import_run_id, player_code),
    CHECK (starts >= 0),
    CHECK (substitute_appearances >= 0),
    CHECK (unused_substitute >= 0),
    CHECK (minutes_per_start BETWEEN 0.0 AND 90.0),
    CHECK (minutes_per_substitute BETWEEN 0.0 AND 90.0)
);

CREATE TABLE IF NOT EXISTS appearance_scenario_override (
    override_id VARCHAR PRIMARY KEY,
    player_code BIGINT NOT NULL,
    target_gameweek INTEGER NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    effective_until TIMESTAMPTZ,
    start_probability_if_available DOUBLE NOT NULL,
    substitute_probability_if_available DOUBLE NOT NULL,
    sixty_probability_given_start DOUBLE NOT NULL,
    minutes_per_start DOUBLE NOT NULL,
    minutes_per_substitute DOUBLE NOT NULL,
    source VARCHAR NOT NULL,
    rationale VARCHAR NOT NULL,
    CHECK (start_probability_if_available BETWEEN 0.0 AND 1.0),
    CHECK (substitute_probability_if_available BETWEEN 0.0 AND 1.0),
    CHECK (
        start_probability_if_available + substitute_probability_if_available <= 1.0
    ),
    CHECK (sixty_probability_given_start BETWEEN 0.0 AND 1.0),
    CHECK (minutes_per_start BETWEEN 0.0 AND 90.0),
    CHECK (minutes_per_substitute BETWEEN 0.0 AND 90.0),
    CHECK (effective_until IS NULL OR effective_until >= observed_at)
);

CREATE TABLE IF NOT EXISTS player_fixture_history_import_run (
    import_run_id VARCHAR PRIMARY KEY,
    season VARCHAR NOT NULL,
    source VARCHAR NOT NULL,
    source_revision VARCHAR NOT NULL,
    source_committed_at TIMESTAMPTZ NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL,
    players_source_path VARCHAR NOT NULL,
    gameweeks_source_path VARCHAR NOT NULL,
    players_sha256 VARCHAR NOT NULL,
    gameweeks_sha256 VARCHAR NOT NULL,
    player_rows INTEGER NOT NULL,
    fixture_rows INTEGER NOT NULL,
    exact_duplicate_rows_removed INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    CHECK (player_rows > 0),
    CHECK (fixture_rows > 0),
    CHECK (exact_duplicate_rows_removed >= 0),
    CHECK (status = 'completed')
);

CREATE TABLE IF NOT EXISTS player_fixture_history (
    import_run_id VARCHAR NOT NULL
        REFERENCES player_fixture_history_import_run(import_run_id),
    player_code BIGINT NOT NULL,
    source_player_id INTEGER NOT NULL,
    player_name VARCHAR NOT NULL,
    position VARCHAR NOT NULL,
    team VARCHAR NOT NULL,
    gameweek INTEGER NOT NULL,
    fixture_id INTEGER NOT NULL,
    kickoff_time TIMESTAMPTZ NOT NULL,
    was_home BOOLEAN NOT NULL,
    opponent_team_id INTEGER NOT NULL,
    minutes INTEGER NOT NULL,
    starts INTEGER NOT NULL,
    expected_goals DOUBLE NOT NULL,
    expected_assists DOUBLE NOT NULL,
    saves INTEGER NOT NULL,
    yellow_cards INTEGER NOT NULL,
    red_cards INTEGER NOT NULL,
    bonus INTEGER NOT NULL,
    bps INTEGER NOT NULL,
    defensive_contribution INTEGER NOT NULL,
    PRIMARY KEY (import_run_id, player_code, fixture_id),
    CHECK (position IN ('GK', 'DEF', 'MID', 'FWD')),
    CHECK (gameweek BETWEEN 1 AND 38),
    CHECK (minutes BETWEEN 0 AND 90),
    CHECK (starts IN (0, 1)),
    CHECK (expected_goals >= 0.0),
    CHECK (expected_assists >= 0.0),
    CHECK (saves >= 0),
    CHECK (yellow_cards >= 0),
    CHECK (red_cards >= 0),
    CHECK (bonus >= 0),
    CHECK (defensive_contribution >= 0)
);

CREATE TABLE IF NOT EXISTS player_rate_history_run (
    rate_run_id VARCHAR PRIMARY KEY,
    source_import_run_id VARCHAR NOT NULL
        REFERENCES player_fixture_history_import_run(import_run_id),
    long_form_gameweeks INTEGER NOT NULL,
    short_form_gameweeks INTEGER NOT NULL,
    defcon_short_form_gameweeks INTEGER NOT NULL,
    policy_version VARCHAR NOT NULL,
    player_rows INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    UNIQUE (source_import_run_id, policy_version),
    CHECK (long_form_gameweeks BETWEEN 1 AND 38),
    CHECK (short_form_gameweeks BETWEEN 1 AND long_form_gameweeks),
    CHECK (
        defcon_short_form_gameweeks BETWEEN 1 AND long_form_gameweeks
    ),
    CHECK (player_rows > 0),
    CHECK (status = 'completed')
);

CREATE TABLE IF NOT EXISTS player_rate_history (
    rate_run_id VARCHAR NOT NULL REFERENCES player_rate_history_run(rate_run_id),
    player_code BIGINT NOT NULL,
    player_name VARCHAR NOT NULL,
    position VARCHAR NOT NULL,
    season_minutes INTEGER NOT NULL,
    season_starts INTEGER NOT NULL,
    season_saves INTEGER NOT NULL,
    season_yellow_cards INTEGER NOT NULL,
    season_red_cards INTEGER NOT NULL,
    season_bonus INTEGER NOT NULL,
    season_bps INTEGER NOT NULL,
    long_form_minutes INTEGER NOT NULL,
    long_form_expected_goals DOUBLE NOT NULL,
    long_form_expected_assists DOUBLE NOT NULL,
    short_form_minutes INTEGER NOT NULL,
    short_form_expected_goals DOUBLE NOT NULL,
    short_form_expected_assists DOUBLE NOT NULL,
    long_form_defcon_minutes INTEGER NOT NULL,
    long_form_defensive_contribution INTEGER NOT NULL,
    short_form_defcon_minutes INTEGER NOT NULL,
    short_form_defensive_contribution INTEGER NOT NULL,
    data_quality_flags VARCHAR NOT NULL,
    PRIMARY KEY (rate_run_id, player_code),
    CHECK (position IN ('GK', 'DEF', 'MID', 'FWD')),
    CHECK (season_minutes >= 0),
    CHECK (season_starts >= 0),
    CHECK (season_saves >= 0),
    CHECK (season_yellow_cards >= 0),
    CHECK (season_red_cards >= 0),
    CHECK (season_bonus >= 0),
    CHECK (long_form_minutes >= 0),
    CHECK (long_form_expected_goals >= 0.0),
    CHECK (long_form_expected_assists >= 0.0),
    CHECK (short_form_minutes >= 0),
    CHECK (short_form_expected_goals >= 0.0),
    CHECK (short_form_expected_assists >= 0.0),
    CHECK (long_form_defcon_minutes >= 0),
    CHECK (long_form_defensive_contribution >= 0),
    CHECK (short_form_defcon_minutes >= 0),
    CHECK (short_form_defensive_contribution >= 0)
);

CREATE TABLE IF NOT EXISTS team_strength_import_run (
    import_run_id VARCHAR PRIMARY KEY,
    target_season VARCHAR NOT NULL,
    previous_season VARCHAR NOT NULL,
    source_label VARCHAR NOT NULL,
    source_path VARCHAR NOT NULL,
    source_sha256 VARCHAR NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL,
    team_rows INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    CHECK (team_rows = 20),
    CHECK (status = 'completed')
);

CREATE TABLE IF NOT EXISTS team_strength_history (
    import_run_id VARCHAR NOT NULL REFERENCES team_strength_import_run(import_run_id),
    team_abbreviation VARCHAR NOT NULL,
    team_name VARCHAR NOT NULL,
    prior_type VARCHAR NOT NULL,
    long_form_matches INTEGER NOT NULL,
    long_form_xg DOUBLE NOT NULL,
    long_form_xgc DOUBLE NOT NULL,
    short_form_matches INTEGER NOT NULL,
    short_form_xg DOUBLE NOT NULL,
    short_form_xgc DOUBLE NOT NULL,
    league_average_xg_per_match DOUBLE NOT NULL,
    league_average_xgc_per_match DOUBLE NOT NULL,
    PRIMARY KEY (import_run_id, team_abbreviation),
    CHECK (prior_type IN ('observed_previous_pl', 'promoted_team_prior')),
    CHECK (long_form_matches > 0),
    CHECK (long_form_xg >= 0.0),
    CHECK (long_form_xgc >= 0.0),
    CHECK (short_form_matches > 0),
    CHECK (short_form_xg >= 0.0),
    CHECK (short_form_xgc >= 0.0),
    CHECK (league_average_xg_per_match > 0.0),
    CHECK (league_average_xgc_per_match > 0.0)
);

CREATE TABLE IF NOT EXISTS team_strength_run (
    strength_run_id VARCHAR PRIMARY KEY,
    source_import_run_id VARCHAR NOT NULL REFERENCES team_strength_import_run(import_run_id),
    source_ingestion_run_id VARCHAR NOT NULL REFERENCES ingestion_run(ingestion_run_id),
    target_gameweek INTEGER NOT NULL,
    policy_version VARCHAR NOT NULL,
    team_rows INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    UNIQUE (source_import_run_id, source_ingestion_run_id, target_gameweek, policy_version),
    CHECK (target_gameweek BETWEEN 1 AND 38),
    CHECK (team_rows = 20),
    CHECK (status = 'completed')
);

CREATE TABLE IF NOT EXISTS team_strength_projection (
    strength_run_id VARCHAR NOT NULL REFERENCES team_strength_run(strength_run_id),
    team_id INTEGER NOT NULL,
    team_code INTEGER NOT NULL,
    team_abbreviation VARCHAR NOT NULL,
    team_name VARCHAR NOT NULL,
    is_promoted_prior BOOLEAN NOT NULL,
    long_form_xg_per_match DOUBLE NOT NULL,
    short_form_xg_per_match DOUBLE NOT NULL,
    blended_xg_per_match DOUBLE NOT NULL,
    long_form_xgc_per_match DOUBLE NOT NULL,
    short_form_xgc_per_match DOUBLE NOT NULL,
    blended_xgc_per_match DOUBLE NOT NULL,
    corrected_xgc_per_match DOUBLE NOT NULL,
    league_average_xg_per_match DOUBLE NOT NULL,
    league_average_xgc_per_match DOUBLE NOT NULL,
    attack_ratio DOUBLE NOT NULL,
    defensive_weakness_ratio DOUBLE NOT NULL,
    workbook_defensive_bonus_multiplier DOUBLE NOT NULL,
    defensive_bonus_multiplier DOUBLE NOT NULL,
    data_quality_flags VARCHAR NOT NULL,
    PRIMARY KEY (strength_run_id, team_id),
    CHECK (long_form_xg_per_match >= 0.0),
    CHECK (short_form_xg_per_match >= 0.0),
    CHECK (blended_xg_per_match >= 0.0),
    CHECK (long_form_xgc_per_match >= 0.0),
    CHECK (short_form_xgc_per_match >= 0.0),
    CHECK (blended_xgc_per_match >= 0.0),
    CHECK (corrected_xgc_per_match >= 0.0),
    CHECK (league_average_xg_per_match > 0.0),
    CHECK (league_average_xgc_per_match > 0.0),
    CHECK (attack_ratio >= 0.0),
    CHECK (defensive_weakness_ratio >= 0.0),
    CHECK (defensive_bonus_multiplier >= 0.0)
);

CREATE TABLE IF NOT EXISTS appearance_projection_run (
    projection_run_id VARCHAR PRIMARY KEY,
    availability_resolution_run_id VARCHAR NOT NULL
        REFERENCES availability_resolution_run(resolution_run_id),
    appearance_history_import_run_id VARCHAR NOT NULL
        REFERENCES appearance_history_import_run(import_run_id),
    target_gameweek INTEGER NOT NULL,
    policy_version VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    UNIQUE (
        availability_resolution_run_id,
        appearance_history_import_run_id,
        policy_version
    ),
    CHECK (status IN ('completed', 'completed_with_gaps'))
);

CREATE TABLE IF NOT EXISTS player_appearance_projection (
    projection_run_id VARCHAR NOT NULL
        REFERENCES appearance_projection_run(projection_run_id),
    fpl_id INTEGER NOT NULL,
    player_code BIGINT,
    availability_probability DOUBLE,
    start_probability DOUBLE,
    substitute_appearance_probability DOUBLE,
    appearance_probability DOUBLE,
    sixty_minute_probability DOUBLE,
    expected_minutes DOUBLE,
    appearance_xpts DOUBLE,
    sixty_minute_xpts DOUBLE,
    total_xpts DOUBLE,
    data_quality_flags VARCHAR NOT NULL,
    PRIMARY KEY (projection_run_id, fpl_id),
    CHECK (availability_probability BETWEEN 0.0 AND 1.0),
    CHECK (start_probability BETWEEN 0.0 AND 1.0),
    CHECK (substitute_appearance_probability BETWEEN 0.0 AND 1.0),
    CHECK (appearance_probability BETWEEN 0.0 AND 1.0),
    CHECK (sixty_minute_probability BETWEEN 0.0 AND 1.0),
    CHECK (expected_minutes BETWEEN 0.0 AND 90.0)
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

V2_TO_V3_SQL = """
ALTER TABLE team_snapshot ALTER COLUMN strength DROP NOT NULL;
ALTER TABLE team_snapshot ALTER COLUMN strength_overall_home DROP NOT NULL;
ALTER TABLE team_snapshot ALTER COLUMN strength_overall_away DROP NOT NULL;
ALTER TABLE team_snapshot ALTER COLUMN strength_attack_home DROP NOT NULL;
ALTER TABLE team_snapshot ALTER COLUMN strength_attack_away DROP NOT NULL;
ALTER TABLE team_snapshot ALTER COLUMN strength_defence_home DROP NOT NULL;
ALTER TABLE team_snapshot ALTER COLUMN strength_defence_away DROP NOT NULL;
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
        current_version = connection.execute(
            "SELECT max(version) FROM schema_version"
        ).fetchone()[0]
        if current_version == 2:
            connection.execute(V2_TO_V3_SQL)
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
