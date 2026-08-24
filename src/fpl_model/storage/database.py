"""DuckDB schema for deadline-safe local snapshots and projections."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

DEFAULT_DATABASE_PATH = Path("data/processed/fpl_model.duckdb")
SCHEMA_VERSION = 14

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

CREATE TABLE IF NOT EXISTS player_identity_bridge_run (
    bridge_run_id VARCHAR PRIMARY KEY,
    source_ingestion_run_id VARCHAR NOT NULL REFERENCES ingestion_run(ingestion_run_id),
    target_season VARCHAR NOT NULL,
    vaastav_season VARCHAR NOT NULL,
    source_revision VARCHAR NOT NULL,
    source_path VARCHAR NOT NULL,
    source_sha256 VARCHAR NOT NULL,
    policy_version VARCHAR NOT NULL,
    official_players INTEGER NOT NULL,
    vaastav_players INTEGER NOT NULL,
    matched_players INTEGER NOT NULL,
    official_only_players INTEGER NOT NULL,
    vaastav_only_players INTEGER NOT NULL,
    name_mismatch_players INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    CHECK (official_players > 0),
    CHECK (vaastav_players > 0),
    CHECK (matched_players >= 0),
    CHECK (official_only_players >= 0),
    CHECK (vaastav_only_players >= 0),
    CHECK (name_mismatch_players >= 0),
    CHECK (matched_players + official_only_players = official_players),
    CHECK (matched_players + vaastav_only_players = vaastav_players),
    CHECK (name_mismatch_players <= matched_players),
    CHECK (status IN ('completed', 'completed_with_gaps'))
);

CREATE TABLE IF NOT EXISTS player_identity_bridge (
    bridge_run_id VARCHAR NOT NULL
        REFERENCES player_identity_bridge_run(bridge_run_id),
    canonical_player_id BIGINT NOT NULL,
    provider VARCHAR NOT NULL,
    provider_player_id VARCHAR NOT NULL,
    player_name VARCHAR NOT NULL,
    match_method VARCHAR NOT NULL,
    data_quality_flags VARCHAR NOT NULL,
    PRIMARY KEY (bridge_run_id, provider, provider_player_id),
    UNIQUE (bridge_run_id, canonical_player_id, provider),
    CHECK (canonical_player_id > 0),
    CHECK (provider IN ('official_fpl', 'vaastav')),
    CHECK (match_method IN ('shared_player_code', 'provider_code_only'))
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

CREATE TABLE IF NOT EXISTS fpl_event_live_run (
    live_run_id VARCHAR PRIMARY KEY,
    source_ingestion_run_id VARCHAR NOT NULL REFERENCES ingestion_run(ingestion_run_id),
    season VARCHAR NOT NULL,
    gameweek INTEGER NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    source_path VARCHAR NOT NULL,
    source_sha256 VARCHAR NOT NULL,
    event_finished BOOLEAN NOT NULL,
    data_checked BOOLEAN NOT NULL,
    player_rows INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    UNIQUE (source_ingestion_run_id, gameweek, source_sha256),
    CHECK (gameweek BETWEEN 1 AND 38),
    CHECK (player_rows > 0),
    CHECK (status IN ('completed', 'provisional'))
);

CREATE TABLE IF NOT EXISTS player_gameweek_stat (
    live_run_id VARCHAR NOT NULL REFERENCES fpl_event_live_run(live_run_id),
    fpl_id INTEGER NOT NULL,
    player_code BIGINT,
    played BOOLEAN NOT NULL,
    minutes INTEGER NOT NULL,
    starts INTEGER NOT NULL,
    goals_scored INTEGER NOT NULL,
    assists INTEGER NOT NULL,
    saves INTEGER NOT NULL,
    yellow_cards INTEGER NOT NULL,
    red_cards INTEGER NOT NULL,
    bonus INTEGER NOT NULL,
    bps INTEGER NOT NULL,
    defensive_contribution INTEGER NOT NULL,
    expected_goals DOUBLE NOT NULL,
    expected_assists DOUBLE NOT NULL,
    expected_goals_conceded DOUBLE NOT NULL,
    total_points INTEGER NOT NULL,
    modified BOOLEAN NOT NULL,
    data_quality_flags VARCHAR NOT NULL,
    PRIMARY KEY (live_run_id, fpl_id),
    CHECK (minutes BETWEEN 0 AND 260),
    CHECK (starts BETWEEN 0 AND 2),
    CHECK (goals_scored >= 0),
    CHECK (assists >= 0),
    CHECK (saves >= 0),
    CHECK (yellow_cards >= 0),
    CHECK (red_cards >= 0),
    CHECK (bonus >= 0),
    CHECK (defensive_contribution >= 0),
    CHECK (expected_goals >= 0.0),
    CHECK (expected_assists >= 0.0),
    CHECK (expected_goals_conceded >= 0.0)
);

CREATE TABLE IF NOT EXISTS event_penalty_review (
    review_id VARCHAR PRIMARY KEY,
    live_run_id VARCHAR NOT NULL UNIQUE REFERENCES fpl_event_live_run(live_run_id),
    observed_at TIMESTAMPTZ NOT NULL,
    source_reference VARCHAR NOT NULL,
    rationale VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    CHECK (status = 'completed')
);

CREATE TABLE IF NOT EXISTS player_penalty_event (
    review_id VARCHAR NOT NULL REFERENCES event_penalty_review(review_id),
    fpl_id INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    goals INTEGER NOT NULL,
    penalty_xg DOUBLE NOT NULL,
    PRIMARY KEY (review_id, fpl_id),
    CHECK (attempts > 0),
    CHECK (goals BETWEEN 0 AND attempts),
    CHECK (penalty_xg >= 0.0)
);

CREATE TABLE IF NOT EXISTS player_gameweek_attacking_decomposition (
    live_run_id VARCHAR NOT NULL REFERENCES fpl_event_live_run(live_run_id),
    fpl_id INTEGER NOT NULL,
    review_id VARCHAR NOT NULL REFERENCES event_penalty_review(review_id),
    total_expected_goals DOUBLE NOT NULL,
    penalty_expected_goals DOUBLE NOT NULL,
    non_penalty_expected_goals DOUBLE NOT NULL,
    penalty_attempts INTEGER NOT NULL,
    penalty_goals INTEGER NOT NULL,
    data_quality_flags VARCHAR NOT NULL,
    PRIMARY KEY (live_run_id, fpl_id),
    CHECK (total_expected_goals >= 0.0),
    CHECK (penalty_expected_goals >= 0.0),
    CHECK (non_penalty_expected_goals >= 0.0),
    CHECK (penalty_attempts >= 0),
    CHECK (penalty_goals BETWEEN 0 AND penalty_attempts),
    CHECK (
        abs(
            total_expected_goals
            - penalty_expected_goals
            - non_penalty_expected_goals
        ) <= 0.000001
    )
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

CREATE TABLE IF NOT EXISTS player_rate_evidence_import_run (
    evidence_import_run_id VARCHAR PRIMARY KEY,
    source_ingestion_run_id VARCHAR NOT NULL
        REFERENCES ingestion_run(ingestion_run_id),
    target_gameweek INTEGER NOT NULL,
    source_label VARCHAR NOT NULL,
    source_path VARCHAR NOT NULL,
    source_sha256 VARCHAR NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL,
    evidence_rows INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    CHECK (target_gameweek BETWEEN 1 AND 38),
    CHECK (evidence_rows > 0),
    CHECK (status = 'completed')
);

CREATE TABLE IF NOT EXISTS player_rate_evidence (
    evidence_import_run_id VARCHAR NOT NULL
        REFERENCES player_rate_evidence_import_run(evidence_import_run_id),
    source_ingestion_run_id VARCHAR NOT NULL,
    fpl_id INTEGER NOT NULL,
    player_code BIGINT NOT NULL,
    player_name VARCHAR NOT NULL,
    position VARCHAR NOT NULL,
    comparability_class VARCHAR NOT NULL,
    source_competition VARCHAR,
    source_season VARCHAR,
    sample_minutes INTEGER,
    sample_starts INTEGER,
    expected_goals DOUBLE,
    expected_assists DOUBLE,
    saves INTEGER,
    yellow_cards INTEGER,
    red_cards INTEGER,
    bonus INTEGER,
    bps INTEGER,
    defensive_contribution INTEGER,
    observed_at TIMESTAMPTZ NOT NULL,
    source_reference VARCHAR NOT NULL,
    rationale VARCHAR NOT NULL,
    data_quality_flags VARCHAR NOT NULL,
    PRIMARY KEY (evidence_import_run_id, player_code),
    FOREIGN KEY (source_ingestion_run_id, fpl_id)
        REFERENCES player_snapshot(ingestion_run_id, fpl_id),
    CHECK (position IN ('GK', 'DEF', 'MID', 'FWD')),
    CHECK (
        comparability_class IN (
            'senior_comparable', 'senior_non_comparable',
            'academy_youth', 'role_only'
        )
    ),
    CHECK (sample_minutes IS NULL OR sample_minutes >= 0),
    CHECK (sample_starts IS NULL OR sample_starts >= 0),
    CHECK (expected_goals IS NULL OR expected_goals >= 0.0),
    CHECK (expected_assists IS NULL OR expected_assists >= 0.0),
    CHECK (saves IS NULL OR saves >= 0),
    CHECK (yellow_cards IS NULL OR yellow_cards >= 0),
    CHECK (red_cards IS NULL OR red_cards >= 0),
    CHECK (bonus IS NULL OR bonus >= 0),
    CHECK (defensive_contribution IS NULL OR defensive_contribution >= 0)
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

CREATE TABLE IF NOT EXISTS inseason_appearance_run (
    projection_run_id VARCHAR PRIMARY KEY
        REFERENCES appearance_projection_run(projection_run_id),
    current_season VARCHAR NOT NULL,
    previous_season VARCHAR NOT NULL,
    first_history_gameweek INTEGER NOT NULL,
    last_history_gameweek INTEGER NOT NULL,
    live_run_ids VARCHAR NOT NULL,
    previous_effective_fixtures DOUBLE NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    policy_version VARCHAR NOT NULL,
    CHECK (first_history_gameweek BETWEEN 1 AND 38),
    CHECK (last_history_gameweek BETWEEN first_history_gameweek AND 38),
    CHECK (previous_effective_fixtures > 0.0)
);

CREATE TABLE IF NOT EXISTS inseason_player_appearance_context (
    projection_run_id VARCHAR NOT NULL
        REFERENCES inseason_appearance_run(projection_run_id),
    fpl_id INTEGER NOT NULL,
    current_fixture_rows INTEGER NOT NULL,
    previous_weight DOUBLE NOT NULL,
    current_weight DOUBLE NOT NULL,
    minutes_per_start DOUBLE NOT NULL,
    minutes_per_substitute DOUBLE NOT NULL,
    data_quality_flags VARCHAR NOT NULL,
    PRIMARY KEY (projection_run_id, fpl_id),
    CHECK (current_fixture_rows >= 0),
    CHECK (previous_weight BETWEEN 0.0 AND 1.0),
    CHECK (current_weight BETWEEN 0.0 AND 1.0),
    CHECK (abs(previous_weight + current_weight - 1.0) <= 0.000001),
    CHECK (minutes_per_start BETWEEN 0.0 AND 90.0),
    CHECK (minutes_per_substitute BETWEEN 0.0 AND 90.0)
);

CREATE TABLE IF NOT EXISTS reviewed_context_annotation (
    annotation_id VARCHAR PRIMARY KEY,
    subject_type VARCHAR NOT NULL,
    player_code BIGINT,
    team_id INTEGER,
    context_type VARCHAR NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    effective_from TIMESTAMPTZ NOT NULL,
    effective_until TIMESTAMPTZ,
    payload VARCHAR NOT NULL,
    source_reference VARCHAR NOT NULL,
    rationale VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    CHECK (subject_type IN ('player', 'team')),
    CHECK (context_type IN ('manager_regime', 'readiness', 'tactical_role')),
    CHECK (
        (subject_type = 'player' AND player_code IS NOT NULL AND team_id IS NULL)
        OR
        (subject_type = 'team' AND team_id IS NOT NULL AND player_code IS NULL)
    ),
    CHECK (effective_until IS NULL OR effective_until >= effective_from)
);

CREATE TABLE IF NOT EXISTS context_feature_run (
    context_run_id VARCHAR PRIMARY KEY,
    source_ingestion_run_id VARCHAR NOT NULL REFERENCES ingestion_run(ingestion_run_id),
    appearance_projection_run_id VARCHAR NOT NULL
        REFERENCES inseason_appearance_run(projection_run_id),
    target_gameweek INTEGER NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    deadline TIMESTAMPTZ NOT NULL,
    policy_version VARCHAR NOT NULL,
    player_rows INTEGER NOT NULL,
    fully_observed_rows INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    CHECK (target_gameweek BETWEEN 2 AND 38),
    CHECK (as_of <= deadline),
    CHECK (player_rows > 0),
    CHECK (fully_observed_rows BETWEEN 0 AND player_rows),
    CHECK (status IN ('completed', 'completed_with_gaps'))
);

CREATE TABLE IF NOT EXISTS uncertainty_artifact (
    artifact_id VARCHAR PRIMARY KEY,
    source_season VARCHAR NOT NULL,
    source_model_version VARCHAR NOT NULL,
    source_reference VARCHAR NOT NULL,
    policy_version VARCHAR NOT NULL,
    interval_mass DOUBLE NOT NULL,
    minimum_segment_rows INTEGER NOT NULL,
    minimum_segment_gameweeks INTEGER NOT NULL,
    low_risk_rmse_threshold DOUBLE NOT NULL,
    high_risk_rmse_threshold DOUBLE NOT NULL,
    segment_rows INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    CHECK (interval_mass > 0.0 AND interval_mass < 1.0),
    CHECK (minimum_segment_rows > 0),
    CHECK (minimum_segment_gameweeks > 0),
    CHECK (low_risk_rmse_threshold >= 0.0),
    CHECK (high_risk_rmse_threshold >= low_risk_rmse_threshold),
    CHECK (segment_rows > 0),
    CHECK (status IN ('shadow', 'approved', 'rejected'))
);

CREATE TABLE IF NOT EXISTS uncertainty_segment (
    artifact_id VARCHAR NOT NULL REFERENCES uncertainty_artifact(artifact_id),
    scope VARCHAR NOT NULL,
    position VARCHAR NOT NULL,
    xpts_band VARCHAR NOT NULL,
    start_band VARCHAR NOT NULL,
    sample_rows INTEGER NOT NULL,
    sample_gameweeks INTEGER NOT NULL,
    mean_error DOUBLE NOT NULL,
    predictive_rmse DOUBLE NOT NULL,
    residual_lower DOUBLE NOT NULL,
    residual_upper DOUBLE NOT NULL,
    PRIMARY KEY (artifact_id, scope, position, xpts_band, start_band),
    CHECK (scope IN ('overall', 'position', 'position_xpts', 'position_xpts_start')),
    CHECK (position IN ('ALL', 'GK', 'DEF', 'MID', 'FWD')),
    CHECK (sample_rows > 0),
    CHECK (sample_gameweeks > 0),
    CHECK (predictive_rmse >= 0.0),
    CHECK (residual_upper >= residual_lower)
);

CREATE TABLE IF NOT EXISTS shadow_calibration_artifact (
    artifact_id VARCHAR PRIMARY KEY,
    calibration_type VARCHAR NOT NULL,
    source_season VARCHAR NOT NULL,
    source_model_version VARCHAR NOT NULL,
    source_reference VARCHAR NOT NULL,
    training_rows INTEGER NOT NULL,
    training_gameweeks INTEGER NOT NULL,
    slope DOUBLE NOT NULL,
    intercept DOUBLE NOT NULL,
    policy_version VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    CHECK (calibration_type IN ('xpts')),
    CHECK (training_rows > 0),
    CHECK (training_gameweeks > 0),
    CHECK (slope >= 0.0),
    CHECK (status IN ('shadow', 'approved', 'rejected'))
);

CREATE TABLE IF NOT EXISTS player_context_feature (
    context_run_id VARCHAR NOT NULL REFERENCES context_feature_run(context_run_id),
    fpl_id INTEGER NOT NULL,
    player_code BIGINT,
    manager_name VARCHAR,
    manager_tenure_days INTEGER,
    manager_changed_since_previous_deadline BOOLEAN,
    tournament_minutes DOUBLE,
    days_since_last_tournament_match INTEGER,
    training_days INTEGER,
    preseason_minutes DOUBLE,
    rest_days DOUBLE,
    minutes_last_7d DOUBLE NOT NULL,
    minutes_last_14d DOUBLE NOT NULL,
    matches_last_7d INTEGER NOT NULL,
    matches_last_14d INTEGER NOT NULL,
    tactical_role_label VARCHAR,
    tactical_role_distance DOUBLE,
    nominal_position_changed BOOLEAN,
    data_quality_flags VARCHAR NOT NULL,
    PRIMARY KEY (context_run_id, fpl_id),
    CHECK (manager_tenure_days IS NULL OR manager_tenure_days >= 0),
    CHECK (tournament_minutes IS NULL OR tournament_minutes >= 0.0),
    CHECK (training_days IS NULL OR training_days >= 0),
    CHECK (preseason_minutes IS NULL OR preseason_minutes >= 0.0),
    CHECK (rest_days IS NULL OR rest_days >= 0.0),
    CHECK (minutes_last_7d >= 0.0),
    CHECK (minutes_last_14d >= minutes_last_7d),
    CHECK (matches_last_7d >= 0),
    CHECK (matches_last_14d >= matches_last_7d),
    CHECK (tactical_role_distance IS NULL OR tactical_role_distance >= 0.0)
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

CREATE TABLE IF NOT EXISTS baseline_context_lineage (
    model_run_id VARCHAR PRIMARY KEY REFERENCES model_run(model_run_id),
    context_run_id VARCHAR NOT NULL REFERENCES context_feature_run(context_run_id)
);

CREATE TABLE IF NOT EXISTS model_uncertainty_lineage (
    model_run_id VARCHAR PRIMARY KEY REFERENCES model_run(model_run_id),
    artifact_id VARCHAR NOT NULL REFERENCES uncertainty_artifact(artifact_id),
    application_mode VARCHAR NOT NULL,
    CHECK (application_mode IN ('shadow', 'active'))
);

CREATE TABLE IF NOT EXISTS model_shadow_calibration_lineage (
    model_run_id VARCHAR PRIMARY KEY REFERENCES model_run(model_run_id),
    artifact_id VARCHAR NOT NULL REFERENCES shadow_calibration_artifact(artifact_id)
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

CREATE TABLE IF NOT EXISTS player_fixture_uncertainty (
    model_run_id VARCHAR NOT NULL,
    player_code BIGINT NOT NULL,
    fixture_id INTEGER NOT NULL,
    artifact_id VARCHAR NOT NULL REFERENCES uncertainty_artifact(artifact_id),
    lower_xpts DOUBLE NOT NULL,
    upper_xpts DOUBLE NOT NULL,
    predictive_rmse DOUBLE NOT NULL,
    relative_uncertainty DOUBLE NOT NULL,
    risk_band VARCHAR NOT NULL,
    segment_scope VARCHAR NOT NULL,
    segment_sample_rows INTEGER NOT NULL,
    data_quality_flags VARCHAR NOT NULL,
    PRIMARY KEY (model_run_id, player_code, fixture_id),
    FOREIGN KEY (model_run_id, player_code, fixture_id)
        REFERENCES player_fixture_projection(model_run_id, player_code, fixture_id),
    CHECK (lower_xpts >= 0.0),
    CHECK (upper_xpts >= lower_xpts),
    CHECK (predictive_rmse >= 0.0),
    CHECK (relative_uncertainty >= 0.0),
    CHECK (risk_band IN ('low', 'medium', 'high')),
    CHECK (segment_sample_rows > 0)
);

CREATE TABLE IF NOT EXISTS player_fixture_shadow_projection (
    model_run_id VARCHAR NOT NULL,
    player_code BIGINT NOT NULL,
    fixture_id INTEGER NOT NULL,
    artifact_id VARCHAR NOT NULL REFERENCES shadow_calibration_artifact(artifact_id),
    raw_xpts DOUBLE NOT NULL,
    shadow_calibrated_xpts DOUBLE NOT NULL,
    clipped_at_zero BOOLEAN NOT NULL,
    PRIMARY KEY (model_run_id, player_code, fixture_id),
    FOREIGN KEY (model_run_id, player_code, fixture_id)
        REFERENCES player_fixture_projection(model_run_id, player_code, fixture_id),
    CHECK (shadow_calibrated_xpts >= 0.0)
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

CREATE TABLE IF NOT EXISTS baseline_projection_run (
    model_run_id VARCHAR PRIMARY KEY REFERENCES model_run(model_run_id),
    appearance_projection_run_id VARCHAR NOT NULL
        REFERENCES appearance_projection_run(projection_run_id),
    player_rate_run_id VARCHAR NOT NULL
        REFERENCES player_rate_history_run(rate_run_id),
    team_strength_run_id VARCHAR NOT NULL
        REFERENCES team_strength_run(strength_run_id),
    policy_version VARCHAR NOT NULL,
    current_players INTEGER NOT NULL,
    candidate_fixture_rows INTEGER NOT NULL,
    projected_fixture_rows INTEGER NOT NULL,
    gap_players INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    CHECK (current_players > 0),
    CHECK (candidate_fixture_rows >= 0),
    CHECK (projected_fixture_rows >= 0),
    CHECK (projected_fixture_rows <= candidate_fixture_rows),
    CHECK (gap_players >= 0),
    CHECK (status IN ('completed', 'completed_with_gaps'))
);

CREATE TABLE IF NOT EXISTS baseline_projection_gap (
    model_run_id VARCHAR NOT NULL REFERENCES baseline_projection_run(model_run_id),
    fpl_id INTEGER NOT NULL,
    player_code BIGINT,
    team_id INTEGER NOT NULL,
    data_quality_flags VARCHAR NOT NULL,
    PRIMARY KEY (model_run_id, fpl_id)
);

CREATE TABLE IF NOT EXISTS manager_entry (
    entry_id BIGINT PRIMARY KEY,
    entry_name VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    CHECK (entry_id > 0)
);

CREATE TABLE IF NOT EXISTS squad_snapshot (
    squad_snapshot_id VARCHAR PRIMARY KEY,
    entry_id BIGINT NOT NULL REFERENCES manager_entry(entry_id),
    source_ingestion_run_id VARCHAR NOT NULL REFERENCES ingestion_run(ingestion_run_id),
    season VARCHAR NOT NULL,
    target_gameweek INTEGER NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    source_label VARCHAR NOT NULL,
    source_path VARCHAR NOT NULL,
    source_sha256 VARCHAR NOT NULL,
    bank_tenths SMALLINT NOT NULL,
    free_transfers SMALLINT,
    unlimited_transfers BOOLEAN NOT NULL,
    chip_period SMALLINT NOT NULL,
    player_rows SMALLINT NOT NULL,
    constraint_flags VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    CHECK (target_gameweek BETWEEN 1 AND 38),
    CHECK (bank_tenths >= 0),
    CHECK (
        (unlimited_transfers AND free_transfers IS NULL)
        OR
        (NOT unlimited_transfers AND free_transfers BETWEEN 0 AND 5)
    ),
    CHECK (chip_period IN (1, 2)),
    CHECK (player_rows = 15),
    CHECK (status = 'completed')
);

CREATE TABLE IF NOT EXISTS squad_snapshot_player (
    squad_snapshot_id VARCHAR NOT NULL
        REFERENCES squad_snapshot(squad_snapshot_id),
    fpl_id INTEGER NOT NULL,
    purchase_price_tenths SMALLINT NOT NULL,
    selling_price_tenths SMALLINT NOT NULL,
    squad_position SMALLINT NOT NULL,
    is_captain BOOLEAN NOT NULL,
    is_vice_captain BOOLEAN NOT NULL,
    PRIMARY KEY (squad_snapshot_id, fpl_id),
    UNIQUE (squad_snapshot_id, squad_position),
    CHECK (purchase_price_tenths > 0),
    CHECK (selling_price_tenths > 0),
    CHECK (squad_position BETWEEN 1 AND 15),
    CHECK (NOT (is_captain AND is_vice_captain))
);

CREATE TABLE IF NOT EXISTS squad_chip_state (
    squad_snapshot_id VARCHAR NOT NULL
        REFERENCES squad_snapshot(squad_snapshot_id),
    chip_name VARCHAR NOT NULL,
    chip_status VARCHAR NOT NULL,
    PRIMARY KEY (squad_snapshot_id, chip_name),
    CHECK (chip_name IN ('wildcard', 'free_hit', 'bench_boost', 'triple_captain')),
    CHECK (chip_status IN ('available', 'active', 'played', 'expired'))
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
