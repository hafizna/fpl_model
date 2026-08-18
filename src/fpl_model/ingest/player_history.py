"""Validated Vaastav player-fixture history and preseason rate windows."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database

SOURCE_NAME = "vaastav/Fantasy-Premier-League"
RATE_POLICY_VERSION = "benchwarmers_preseason_rate_windows_v1"
POSITION_BY_ELEMENT_TYPE = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
SEASON_GAMEWEEKS = 38

PLAYER_COLUMNS = (
    "id",
    "code",
    "web_name",
    "minutes",
    "starts",
    "expected_goals",
    "expected_assists",
    "saves",
    "yellow_cards",
    "red_cards",
    "bonus",
    "bps",
    "defensive_contribution",
)
GAMEWEEK_COLUMNS = (
    "element",
    "position",
    "team",
    "GW",
    "fixture",
    "kickoff_time",
    "was_home",
    "opponent_team",
    "minutes",
    "starts",
    "expected_goals",
    "expected_assists",
    "saves",
    "yellow_cards",
    "red_cards",
    "bonus",
    "bps",
    "defensive_contribution",
)
TOTAL_COLUMNS = (
    "minutes",
    "starts",
    "expected_goals",
    "expected_assists",
    "saves",
    "yellow_cards",
    "red_cards",
    "bonus",
    "bps",
    "defensive_contribution",
)


@dataclass(frozen=True, slots=True)
class PlayerFixtureHistoryImportResult:
    import_run_id: str
    season: str
    source_revision: str
    players_sha256: str
    gameweeks_sha256: str
    player_rows: int
    fixture_rows: int
    exact_duplicate_rows_removed: int


@dataclass(frozen=True, slots=True)
class PlayerRateHistoryResult:
    rate_run_id: str
    source_import_run_id: str
    player_rows: int


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _required(frame: pd.DataFrame, columns: tuple[str, ...], label: str) -> None:
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f"{label} missing columns: {', '.join(sorted(missing))}")


def _integers(
    frame: pd.DataFrame,
    column: str,
    *,
    non_negative: bool = True,
) -> pd.Series:
    numeric = pd.to_numeric(frame[column], errors="raise")
    if numeric.isna().any() or (numeric % 1 != 0).any():
        raise ValueError(f"{column} must contain non-missing integers")
    result = numeric.astype("int64")
    if non_negative and (result < 0).any():
        raise ValueError(f"{column} must be non-negative")
    return result


def _non_negative_floats(frame: pd.DataFrame, column: str) -> pd.Series:
    result = pd.to_numeric(frame[column], errors="raise").astype("float64")
    if result.isna().any() or (result < 0.0).any():
        raise ValueError(f"{column} must contain non-negative numbers")
    return result


def _booleans(frame: pd.DataFrame, column: str) -> pd.Series:
    values = frame[column]
    if values.dtype == bool:
        return values.astype(bool)
    normalised = values.astype(str).str.strip().str.lower()
    if not normalised.isin({"true", "false"}).all():
        raise ValueError(f"{column} must contain booleans")
    return normalised.eq("true")


def build_player_fixture_history(
    players: pd.DataFrame,
    gameweeks: pd.DataFrame,
) -> tuple[pd.DataFrame, int]:
    """Join stable player codes to exact, validated player-fixture rows."""
    _required(players, PLAYER_COLUMNS, "players")
    _required(gameweeks, GAMEWEEK_COLUMNS, "gameweeks")
    player_frame = players.loc[:, PLAYER_COLUMNS].copy()
    gameweek_frame = gameweeks.loc[:, GAMEWEEK_COLUMNS].copy()
    if player_frame.empty or gameweek_frame.empty:
        raise ValueError("players and gameweeks must not be empty")

    for column in ("id", "code"):
        player_frame[column] = _integers(player_frame, column)
    if (player_frame[["id", "code"]] <= 0).any().any():
        raise ValueError("player id and code must be positive")
    if player_frame["id"].duplicated().any():
        raise ValueError("players contains duplicate ids")
    if player_frame["code"].duplicated().any():
        raise ValueError("players contains duplicate player codes")
    player_frame["web_name"] = (
        player_frame["web_name"].fillna("").astype(str).str.strip()
    )
    if (player_frame["web_name"] == "").any():
        raise ValueError("web_name must not be blank")

    original_rows = len(gameweek_frame)
    gameweek_frame = gameweek_frame.drop_duplicates().copy()
    duplicates_removed = original_rows - len(gameweek_frame)
    if gameweek_frame.duplicated(["element", "fixture"], keep=False).any():
        raise ValueError("gameweeks contains conflicting player-fixture duplicates")

    for column in (
        "element",
        "GW",
        "fixture",
        "opponent_team",
        "minutes",
        "starts",
        "saves",
        "yellow_cards",
        "red_cards",
        "bonus",
        "defensive_contribution",
    ):
        gameweek_frame[column] = _integers(gameweek_frame, column)
    gameweek_frame["bps"] = _integers(
        gameweek_frame,
        "bps",
        non_negative=False,
    )
    for column in ("expected_goals", "expected_assists"):
        gameweek_frame[column] = _non_negative_floats(gameweek_frame, column)
    if not gameweek_frame["GW"].between(1, SEASON_GAMEWEEKS).all():
        raise ValueError("GW must be between 1 and 38")
    if not gameweek_frame["minutes"].between(0, 90).all():
        raise ValueError("minutes must be between 0 and 90")
    if not gameweek_frame["starts"].isin({0, 1}).all():
        raise ValueError("starts must be 0 or 1")
    gameweek_frame["position"] = (
        gameweek_frame["position"].fillna("").astype(str).str.strip()
    )
    if not gameweek_frame["position"].isin(POSITION_BY_ELEMENT_TYPE.values()).all():
        raise ValueError("position must be GK, DEF, MID, or FWD")
    gameweek_frame["team"] = (
        gameweek_frame["team"].fillna("").astype(str).str.strip()
    )
    if (gameweek_frame["team"] == "").any():
        raise ValueError("team must not be blank")
    gameweek_frame["kickoff_time"] = pd.to_datetime(
        gameweek_frame["kickoff_time"],
        utc=True,
        errors="coerce",
    )
    if gameweek_frame["kickoff_time"].isna().any():
        raise ValueError("kickoff_time contains missing or invalid values")
    gameweek_frame["was_home"] = _booleans(gameweek_frame, "was_home")

    identity = player_frame[["id", "code", "web_name"]]
    result = gameweek_frame.merge(
        identity,
        left_on="element",
        right_on="id",
        how="left",
        validate="many_to_one",
    )
    if result["code"].isna().any():
        raise ValueError("gameweeks contains elements missing from players")
    aggregated = result.groupby("element", sort=False)[list(TOTAL_COLUMNS)].sum()
    expected = player_frame.set_index("id").loc[aggregated.index, list(TOTAL_COLUMNS)]
    for column in TOTAL_COLUMNS:
        actual_values = pd.to_numeric(aggregated[column]).astype(float)
        expected_values = pd.to_numeric(expected[column]).astype(float)
        if not (actual_values - expected_values).abs().le(1e-8).all():
            raise ValueError(
                f"deduplicated gameweek {column} totals do not match players"
            )

    canonical = result.rename(
        columns={
            "code": "player_code",
            "element": "source_player_id",
            "web_name": "player_name",
            "GW": "gameweek",
            "fixture": "fixture_id",
            "opponent_team": "opponent_team_id",
        }
    )
    canonical = canonical[
        [
            "player_code",
            "source_player_id",
            "player_name",
            "position",
            "team",
            "gameweek",
            "fixture_id",
            "kickoff_time",
            "was_home",
            "opponent_team_id",
            "minutes",
            "starts",
            "expected_goals",
            "expected_assists",
            "saves",
            "yellow_cards",
            "red_cards",
            "bonus",
            "bps",
            "defensive_contribution",
        ]
    ].sort_values(["player_code", "gameweek", "fixture_id"])
    return canonical.reset_index(drop=True), duplicates_removed


def import_player_fixture_history(
    players_csv_path: str | Path,
    gameweeks_csv_path: str | Path,
    *,
    season: str,
    source_revision: str,
    source_committed_at: datetime,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    imported_at: datetime | None = None,
) -> PlayerFixtureHistoryImportResult:
    """Import one pinned Vaastav revision transactionally and idempotently."""
    players_path = Path(players_csv_path)
    gameweeks_path = Path(gameweeks_csv_path)
    if not players_path.is_file():
        raise FileNotFoundError(players_path)
    if not gameweeks_path.is_file():
        raise FileNotFoundError(gameweeks_path)
    if not season.strip() or not source_revision.strip():
        raise ValueError("season and source_revision must not be blank")
    _aware(source_committed_at, "source_committed_at")
    timestamp = imported_at or datetime.now(UTC)
    _aware(timestamp, "imported_at")

    players_bytes = players_path.read_bytes()
    gameweeks_bytes = gameweeks_path.read_bytes()
    players_sha256 = hashlib.sha256(players_bytes).hexdigest()
    gameweeks_sha256 = hashlib.sha256(gameweeks_bytes).hexdigest()
    history, duplicates_removed = build_player_fixture_history(
        pd.read_csv(players_path),
        pd.read_csv(gameweeks_path),
    )
    identity = "|".join(
        (season, source_revision, players_sha256, gameweeks_sha256)
    ).encode()
    import_run_id = f"player_history_{hashlib.sha256(identity).hexdigest()[:16]}"
    player_rows = int(history["player_code"].nunique())
    initialize_database(database_path)

    result = PlayerFixtureHistoryImportResult(
        import_run_id=import_run_id,
        season=season,
        source_revision=source_revision,
        players_sha256=players_sha256,
        gameweeks_sha256=gameweeks_sha256,
        player_rows=player_rows,
        fixture_rows=len(history),
        exact_duplicate_rows_removed=duplicates_removed,
    )
    with duckdb.connect(str(database_path)) as connection:
        existing = connection.execute(
            """
            SELECT season, source_revision, players_sha256, gameweeks_sha256,
                   player_rows, fixture_rows, exact_duplicate_rows_removed
            FROM player_fixture_history_import_run WHERE import_run_id = ?
            """,
            [import_run_id],
        ).fetchone()
        expected_existing = (
            season,
            source_revision,
            players_sha256,
            gameweeks_sha256,
            player_rows,
            len(history),
            duplicates_removed,
        )
        if existing is not None:
            if existing != expected_existing:
                raise ValueError(f"player-history import ID collision: {import_run_id}")
            return result

        connection.register("history_import_frame", history)
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """
                INSERT INTO player_fixture_history_import_run VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed'
                )
                """,
                [
                    import_run_id,
                    season,
                    SOURCE_NAME,
                    source_revision,
                    source_committed_at,
                    timestamp,
                    str(players_path.resolve()),
                    str(gameweeks_path.resolve()),
                    players_sha256,
                    gameweeks_sha256,
                    player_rows,
                    len(history),
                    duplicates_removed,
                ],
            )
            connection.execute(
                """
                INSERT INTO player_fixture_history
                SELECT ?, player_code, source_player_id, player_name, position,
                        team, gameweek, fixture_id, kickoff_time, was_home,
                       opponent_team_id, minutes, starts, expected_goals,
                       expected_assists, saves, yellow_cards, red_cards, bonus,
                       bps, defensive_contribution
                FROM history_import_frame
                """,
                [import_run_id],
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")
        finally:
            connection.unregister("history_import_frame")
    return result


def materialize_preseason_rate_history(
    *,
    source_import_run_id: str,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    long_form_gameweeks: int = 38,
    short_form_gameweeks: int = 6,
    defcon_short_form_gameweeks: int = 10,
) -> PlayerRateHistoryResult:
    """Aggregate prior-season windows needed by the Benchwarmers components."""
    if not 1 <= short_form_gameweeks <= long_form_gameweeks <= SEASON_GAMEWEEKS:
        raise ValueError("rate windows must satisfy 1 <= short <= long <= 38")
    if not 1 <= defcon_short_form_gameweeks <= long_form_gameweeks:
        raise ValueError("DefCon short window must be between 1 and long form")
    initialize_database(database_path)
    identity = (
        f"{source_import_run_id}|{long_form_gameweeks}|{short_form_gameweeks}|"
        f"{defcon_short_form_gameweeks}|{RATE_POLICY_VERSION}"
    ).encode()
    rate_run_id = f"player_rates_{hashlib.sha256(identity).hexdigest()[:16]}"
    long_start = SEASON_GAMEWEEKS - long_form_gameweeks + 1
    short_start = SEASON_GAMEWEEKS - short_form_gameweeks + 1
    defcon_short_start = SEASON_GAMEWEEKS - defcon_short_form_gameweeks + 1

    with duckdb.connect(str(database_path)) as connection:
        source_exists = connection.execute(
            "SELECT count(*) FROM player_fixture_history_import_run WHERE import_run_id = ?",
            [source_import_run_id],
        ).fetchone()[0]
        if not source_exists:
            raise ValueError(f"unknown player-history import: {source_import_run_id}")
        existing = connection.execute(
            "SELECT player_rows FROM player_rate_history_run WHERE rate_run_id = ?",
            [rate_run_id],
        ).fetchone()
        if existing is not None:
            return PlayerRateHistoryResult(
                rate_run_id,
                source_import_run_id,
                int(existing[0]),
            )

        rows = connection.execute(
            """
            SELECT player_code, any_value(player_name), any_value(position),
                   sum(minutes), sum(starts), sum(saves), sum(yellow_cards),
                   sum(red_cards), sum(bonus), sum(bps),
                   sum(minutes) FILTER (WHERE gameweek >= ?),
                   sum(expected_goals) FILTER (WHERE gameweek >= ?),
                   sum(expected_assists) FILTER (WHERE gameweek >= ?),
                   sum(minutes) FILTER (WHERE gameweek >= ?),
                   sum(expected_goals) FILTER (WHERE gameweek >= ?),
                   sum(expected_assists) FILTER (WHERE gameweek >= ?),
                   sum(minutes) FILTER (WHERE gameweek >= ?),
                   sum(defensive_contribution) FILTER (WHERE gameweek >= ?),
                   sum(minutes) FILTER (WHERE gameweek >= ?),
                   sum(defensive_contribution) FILTER (WHERE gameweek >= ?)
            FROM player_fixture_history
            WHERE import_run_id = ?
            GROUP BY player_code
            ORDER BY player_code
            """,
            [
                long_start,
                long_start,
                long_start,
                short_start,
                short_start,
                short_start,
                long_start,
                long_start,
                defcon_short_start,
                defcon_short_start,
                source_import_run_id,
            ],
        ).fetchall()
        if not rows:
            raise ValueError("player-history import contains no fixture rows")

        output_rows = []
        for row in rows:
            values = list(row)
            flags: set[str] = set()
            if values[10] == 0:
                flags.add("ZERO_LONG_FORM_MINUTES")
            if values[16] == 0:
                flags.add("ZERO_DEFCON_HISTORY_MINUTES")
            if values[4] == 0:
                flags.add("ZERO_PRIOR_STARTS")
            output_rows.append(
                (rate_run_id, *values, json.dumps(sorted(flags)))
            )

        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """
                INSERT INTO player_rate_history_run VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 'completed', current_timestamp
                )
                """,
                [
                    rate_run_id,
                    source_import_run_id,
                    long_form_gameweeks,
                    short_form_gameweeks,
                    defcon_short_form_gameweeks,
                    RATE_POLICY_VERSION,
                    len(output_rows),
                ],
            )
            connection.executemany(
                "INSERT INTO player_rate_history VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                output_rows,
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    return PlayerRateHistoryResult(
        rate_run_id=rate_run_id,
        source_import_run_id=source_import_run_id,
        player_rows=len(output_rows),
    )
