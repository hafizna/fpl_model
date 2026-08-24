"""Validated workbook boundary for preseason team-strength priors."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isclose
from pathlib import Path

import duckdb
import pandas as pd

from fpl_model.model.defence import DefensiveWindow, corrected_team_xgc_per_match
from fpl_model.model.fixture import FixtureStrength
from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database

REQUIRED_COLUMNS = (
    "team_abbreviation",
    "team_name",
    "prior_type",
    "long_form_matches",
    "long_form_xg",
    "long_form_xgc",
    "short_form_matches",
    "short_form_xg",
    "short_form_xgc",
    "league_average_xg_per_match",
    "league_average_xgc_per_match",
)
PRIOR_TYPES = {"observed_previous_pl", "promoted_team_prior"}
TEAM_COUNT = 20
PROMOTED_TEAM_COUNT = 3
POLICY_VERSION = "benchwarmers_preseason_team_strength_v1"
LONG_FORM_WEIGHT = 0.8


@dataclass(frozen=True, slots=True)
class TeamStrengthImportResult:
    import_run_id: str
    target_season: str
    previous_season: str
    source_sha256: str
    team_rows: int


@dataclass(frozen=True, slots=True)
class TeamStrengthRunResult:
    strength_run_id: str
    source_import_run_id: str
    source_ingestion_run_id: str
    target_gameweek: int
    team_rows: int


def _integer_series(frame: pd.DataFrame, column: str) -> pd.Series:
    numeric = pd.to_numeric(frame[column], errors="raise")
    if numeric.isna().any() or (numeric % 1 != 0).any():
        raise ValueError(f"{column} must contain non-missing integers")
    result = numeric.astype("int64")
    if (result <= 0).any():
        raise ValueError(f"{column} must be positive")
    return result


def _non_negative_series(frame: pd.DataFrame, column: str) -> pd.Series:
    result = pd.to_numeric(frame[column], errors="raise").astype("float64")
    if result.isna().any() or (result < 0.0).any():
        raise ValueError(f"{column} must contain non-negative numbers")
    return result


def validate_team_strength_history(frame: pd.DataFrame) -> pd.DataFrame:
    """Return canonical 20-team rows or reject ambiguous workbook output."""
    missing = set(REQUIRED_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(
            "team strength history missing columns: " + ", ".join(sorted(missing))
        )
    result = frame.loc[:, REQUIRED_COLUMNS].copy()
    if len(result) != TEAM_COUNT:
        raise ValueError("team strength history must contain exactly 20 teams")

    for column in ("team_abbreviation", "team_name", "prior_type"):
        result[column] = result[column].fillna("").astype(str).str.strip()
        if (result[column] == "").any():
            raise ValueError(f"{column} must not be blank")
    result["team_abbreviation"] = result["team_abbreviation"].str.upper()
    if not result["team_abbreviation"].map(
        lambda value: bool(re.fullmatch(r"[A-Z]{3}", value))
    ).all():
        raise ValueError("team_abbreviation must contain three uppercase letters")
    if result["team_abbreviation"].duplicated().any():
        raise ValueError("team strength history contains duplicate abbreviations")
    if result["team_name"].duplicated().any():
        raise ValueError("team strength history contains duplicate team names")
    if not result["prior_type"].isin(PRIOR_TYPES).all():
        raise ValueError("prior_type must identify observed or promoted-team history")
    if (result["prior_type"] == "promoted_team_prior").sum() != PROMOTED_TEAM_COUNT:
        raise ValueError("team strength history must identify exactly three promoted priors")

    for column in ("long_form_matches", "short_form_matches"):
        result[column] = _integer_series(result, column)
    for column in (
        "long_form_xg",
        "long_form_xgc",
        "short_form_xg",
        "short_form_xgc",
        "league_average_xg_per_match",
        "league_average_xgc_per_match",
    ):
        result[column] = _non_negative_series(result, column)
    for column in (
        "league_average_xg_per_match",
        "league_average_xgc_per_match",
    ):
        if (result[column] <= 0.0).any():
            raise ValueError(f"{column} must be positive")
        if float(result[column].max() - result[column].min()) > 1e-12:
            raise ValueError(f"{column} must be identical for every team")

    promoted = result.loc[result["prior_type"] == "promoted_team_prior"]
    for metric in ("xg", "xgc"):
        long_rate = promoted[f"long_form_{metric}"] / promoted["long_form_matches"]
        short_rate = promoted[f"short_form_{metric}"] / promoted["short_form_matches"]
        if not all(isclose(a, b, rel_tol=1e-10, abs_tol=1e-10) for a, b in zip(long_rate, short_rate, strict=True)):
            raise ValueError(
                f"promoted-team long/short {metric} rates must match the workbook prior"
            )
    return result.sort_values("team_abbreviation").reset_index(drop=True)


def import_team_strength_history(
    csv_path: str | Path,
    *,
    target_season: str,
    previous_season: str,
    source_label: str,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    imported_at: datetime | None = None,
) -> TeamStrengthImportResult:
    """Import one immutable, content-addressed workbook team-strength export."""
    path = Path(csv_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    if not target_season.strip() or not previous_season.strip() or not source_label.strip():
        raise ValueError("season and source_label values must not be blank")
    timestamp = imported_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("imported_at must be timezone-aware")

    source_bytes = path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    rows = validate_team_strength_history(pd.read_csv(path))
    identity = f"{target_season}|{previous_season}|{source_sha256}".encode()
    import_run_id = f"team_strength_{hashlib.sha256(identity).hexdigest()[:16]}"
    result = TeamStrengthImportResult(
        import_run_id,
        target_season,
        previous_season,
        source_sha256,
        len(rows),
    )
    initialize_database(database_path)

    with duckdb.connect(str(database_path)) as connection:
        existing = connection.execute(
            """
            SELECT target_season, previous_season, source_sha256, team_rows
            FROM team_strength_import_run WHERE import_run_id = ?
            """,
            [import_run_id],
        ).fetchone()
        if existing is not None:
            expected = (target_season, previous_season, source_sha256, len(rows))
            if existing != expected:
                raise ValueError(f"team-strength import ID collision: {import_run_id}")
            return result

        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """
                INSERT INTO team_strength_import_run VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, 'completed'
                )
                """,
                [
                    import_run_id,
                    target_season,
                    previous_season,
                    source_label,
                    str(path.resolve()),
                    source_sha256,
                    timestamp,
                    len(rows),
                ],
            )
            connection.executemany(
                "INSERT INTO team_strength_history VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        import_run_id,
                        row.team_abbreviation,
                        row.team_name,
                        row.prior_type,
                        int(row.long_form_matches),
                        float(row.long_form_xg),
                        float(row.long_form_xgc),
                        int(row.short_form_matches),
                        float(row.short_form_xg),
                        float(row.short_form_xgc),
                        float(row.league_average_xg_per_match),
                        float(row.league_average_xgc_per_match),
                    )
                    for row in rows.itertuples(index=False)
                ],
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")
    return result


def _causal_fpl_run(
    connection: duckdb.DuckDBPyConnection,
    *,
    target_season: str,
    target_gameweek: int,
    source_ingestion_run_id: str | None,
) -> tuple[str, datetime, datetime]:
    filter_clause = "AND ir.ingestion_run_id = ?" if source_ingestion_run_id else ""
    parameters: list[object] = [target_gameweek, target_season]
    if source_ingestion_run_id:
        parameters.append(source_ingestion_run_id)
    row = connection.execute(
        f"""
        SELECT ir.ingestion_run_id, ir.captured_at, gw.deadline_time
        FROM ingestion_run AS ir
        JOIN gameweek_snapshot AS gw
          ON gw.ingestion_run_id = ir.ingestion_run_id
         AND gw.gameweek = ?
        WHERE ir.status = 'completed'
          AND ir.source = 'official_fpl_api'
          AND ir.captured_at <= gw.deadline_time
          AND EXISTS (
              SELECT 1 FROM player_snapshot AS ps
              WHERE ps.ingestion_run_id = ir.ingestion_run_id
                AND ps.season = ?
          )
          {filter_clause}
        ORDER BY ir.captured_at DESC, ir.ingestion_run_id DESC
        LIMIT 1
        """,
        parameters,
    ).fetchone()
    if row is None:
        raise ValueError("no causal official FPL snapshot found for team-strength mapping")
    return row


def materialize_preseason_team_strength(
    *,
    source_import_run_id: str,
    target_gameweek: int = 1,
    source_ingestion_run_id: str | None = None,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> TeamStrengthRunResult:
    """Map the frozen reviewed team prior onto a causal current-team snapshot."""
    if not 1 <= target_gameweek <= 38:
        raise ValueError("target_gameweek must be between 1 and 38")
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        imported = connection.execute(
            """
            SELECT target_season FROM team_strength_import_run
            WHERE import_run_id = ? AND status = 'completed'
            """,
            [source_import_run_id],
        ).fetchone()
        if imported is None:
            raise ValueError(f"unknown team-strength import: {source_import_run_id}")
        target_season = imported[0]
        fpl_run_id, _, _ = _causal_fpl_run(
            connection,
            target_season=target_season,
            target_gameweek=target_gameweek,
            source_ingestion_run_id=source_ingestion_run_id,
        )
        identity = (
            f"{source_import_run_id}|{fpl_run_id}|{target_gameweek}|{POLICY_VERSION}"
        ).encode()
        strength_run_id = f"team_strength_run_{hashlib.sha256(identity).hexdigest()[:16]}"
        existing = connection.execute(
            "SELECT team_rows FROM team_strength_run WHERE strength_run_id = ?",
            [strength_run_id],
        ).fetchone()
        if existing is not None:
            return TeamStrengthRunResult(
                strength_run_id,
                source_import_run_id,
                fpl_run_id,
                target_gameweek,
                int(existing[0]),
            )

        current_teams = connection.execute(
            """
            SELECT team_id, team_code, name, short_name
            FROM team_snapshot WHERE ingestion_run_id = ? ORDER BY team_id
            """,
            [fpl_run_id],
        ).fetchall()
        history_rows = connection.execute(
            """
            SELECT team_abbreviation, prior_type,
                   long_form_matches, long_form_xg, long_form_xgc,
                   short_form_matches, short_form_xg, short_form_xgc,
                   league_average_xg_per_match,
                   league_average_xgc_per_match
            FROM team_strength_history WHERE import_run_id = ?
            """,
            [source_import_run_id],
        ).fetchall()
        history = {row[0]: row[1:] for row in history_rows}
        current_abbreviations = {row[3] for row in current_teams}
        if len(current_teams) != TEAM_COUNT or current_abbreviations != set(history):
            missing = sorted(current_abbreviations - set(history))
            extra = sorted(set(history) - current_abbreviations)
            raise ValueError(
                f"team-strength/current-team mismatch; missing={missing}, extra={extra}"
            )

        output_rows = []
        for team_id, team_code, team_name, abbreviation in current_teams:
            (
                prior_type,
                long_matches,
                long_xg,
                long_xgc,
                short_matches,
                short_xg,
                short_xgc,
                league_xg,
                league_xgc,
            ) = history[abbreviation]
            long_xg_rate = long_xg / long_matches
            short_xg_rate = short_xg / short_matches
            blended_xg = (
                LONG_FORM_WEIGHT * long_xg_rate
                + (1.0 - LONG_FORM_WEIGHT) * short_xg_rate
            )
            long_xgc_rate = long_xgc / long_matches
            short_xgc_rate = short_xgc / short_matches
            blended_xgc = (
                LONG_FORM_WEIGHT * long_xgc_rate
                + (1.0 - LONG_FORM_WEIGHT) * short_xgc_rate
            )
            corrected_xgc = corrected_team_xgc_per_match(
                DefensiveWindow(long_xgc, long_matches),
                DefensiveWindow(short_xgc, short_matches),
                long_form_weight=LONG_FORM_WEIGHT,
            )
            strength = FixtureStrength(
                opponent_xg_per_match=blended_xg,
                opponent_xgc_per_match=blended_xgc,
                league_average_xg_per_match=league_xg,
                league_average_xgc_per_match=league_xgc,
            )
            promoted = prior_type == "promoted_team_prior"
            flags = ["PROMOTED_TEAM_PRIOR"] if promoted else []
            if target_gameweek > 1:
                flags.append("FROZEN_PRESEASON_TEAM_STRENGTH_PRIOR")
            output_rows.append(
                (
                    strength_run_id,
                    team_id,
                    team_code,
                    abbreviation,
                    team_name,
                    promoted,
                    long_xg_rate,
                    short_xg_rate,
                    blended_xg,
                    long_xgc_rate,
                    short_xgc_rate,
                    blended_xgc,
                    corrected_xgc,
                    league_xg,
                    league_xgc,
                    strength.opponent_attack_ratio,
                    strength.opponent_defensive_weakness_ratio,
                    strength.workbook_defensive_bonus_multiplier,
                    strength.defensive_bonus_multiplier,
                    json.dumps(flags),
                )
            )

        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """
                INSERT INTO team_strength_run VALUES (
                    ?, ?, ?, ?, ?, ?, 'completed', current_timestamp
                )
                """,
                [
                    strength_run_id,
                    source_import_run_id,
                    fpl_run_id,
                    target_gameweek,
                    POLICY_VERSION,
                    len(output_rows),
                ],
            )
            connection.executemany(
                "INSERT INTO team_strength_projection VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                output_rows,
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    return TeamStrengthRunResult(
        strength_run_id,
        source_import_run_id,
        fpl_run_id,
        target_gameweek,
        len(output_rows),
    )
