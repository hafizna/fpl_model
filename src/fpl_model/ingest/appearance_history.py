"""Validated import boundary for workbook-derived appearance history."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database

REQUIRED_COLUMNS = (
    "player_code",
    "player_name",
    "starts",
    "substitute_appearances",
    "unused_substitute",
    "minutes_per_start",
    "minutes_per_substitute",
)


@dataclass(frozen=True, slots=True)
class AppearanceHistoryImportResult:
    import_run_id: str
    season: str
    source_sha256: str
    player_rows: int


def _integer_series(frame: pd.DataFrame, column: str) -> pd.Series:
    numeric = pd.to_numeric(frame[column], errors="raise")
    if numeric.isna().any() or (numeric % 1 != 0).any():
        raise ValueError(f"{column} must contain non-missing integers")
    result = numeric.astype("int64")
    if (result < 0).any():
        raise ValueError(f"{column} must be non-negative")
    return result


def validate_appearance_history(frame: pd.DataFrame) -> pd.DataFrame:
    """Return canonical rows or reject ambiguous workbook exports."""
    missing = set(REQUIRED_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(
            "appearance history missing columns: " + ", ".join(sorted(missing))
        )
    result = frame.loc[:, REQUIRED_COLUMNS].copy()
    if result.empty:
        raise ValueError("appearance history must contain at least one player")

    result["player_code"] = _integer_series(result, "player_code")
    if (result["player_code"] <= 0).any():
        raise ValueError("player_code must be positive")
    if result["player_code"].duplicated().any():
        raise ValueError("appearance history contains duplicate player_code values")

    result["player_name"] = result["player_name"].fillna("").astype(str).str.strip()
    if (result["player_name"] == "").any():
        raise ValueError("player_name must not be blank")

    for column in ("starts", "substitute_appearances", "unused_substitute"):
        result[column] = _integer_series(result, column)
    for column in ("minutes_per_start", "minutes_per_substitute"):
        result[column] = pd.to_numeric(result[column], errors="raise")
        if result[column].isna().any() or not result[column].between(0.0, 90.0).all():
            raise ValueError(f"{column} must be between 0 and 90")

    starts_without_minutes = (result["starts"] > 0) & (
        result["minutes_per_start"] <= 0
    )
    substitutes_without_minutes = (result["substitute_appearances"] > 0) & (
        result["minutes_per_substitute"] <= 0
    )
    if starts_without_minutes.any():
        raise ValueError("players with starts must have positive minutes_per_start")
    if substitutes_without_minutes.any():
        raise ValueError(
            "players with substitute appearances must have positive minutes_per_substitute"
        )
    return result.sort_values("player_code").reset_index(drop=True)


def import_appearance_history_csv(
    csv_path: str | Path,
    *,
    season: str,
    source_label: str,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    imported_at: datetime | None = None,
) -> AppearanceHistoryImportResult:
    """Validate and transactionally import one immutable workbook export."""
    path = Path(csv_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    if not season.strip() or not source_label.strip():
        raise ValueError("season and source_label must not be blank")
    timestamp = imported_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("imported_at must be timezone-aware")

    source_bytes = path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    frame = validate_appearance_history(pd.read_csv(path))
    identity = f"{season}|{source_sha256}".encode()
    import_run_id = f"appearance_history_{hashlib.sha256(identity).hexdigest()[:16]}"
    initialize_database(database_path)

    with duckdb.connect(str(database_path)) as connection:
        existing = connection.execute(
            """
            SELECT season, source_sha256, player_rows
            FROM appearance_history_import_run WHERE import_run_id = ?
            """,
            [import_run_id],
        ).fetchone()
        if existing is not None:
            if existing != (season, source_sha256, len(frame)):
                raise ValueError(f"appearance import ID collision: {import_run_id}")
            return AppearanceHistoryImportResult(
                import_run_id,
                season,
                source_sha256,
                len(frame),
            )

        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """
                INSERT INTO appearance_history_import_run VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 'completed'
                )
                """,
                [
                    import_run_id,
                    season,
                    source_label,
                    str(path.resolve()),
                    source_sha256,
                    timestamp,
                    len(frame),
                ],
            )
            connection.executemany(
                "INSERT INTO player_appearance_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        import_run_id,
                        int(row.player_code),
                        str(row.player_name),
                        int(row.starts),
                        int(row.substitute_appearances),
                        int(row.unused_substitute),
                        float(row.minutes_per_start),
                        float(row.minutes_per_substitute),
                    )
                    for row in frame.itertuples(index=False)
                ],
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    return AppearanceHistoryImportResult(
        import_run_id,
        season,
        source_sha256,
        len(frame),
    )
