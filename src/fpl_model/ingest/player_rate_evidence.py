"""Validated research-only evidence for players lacking prior-PL rates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database

COMPARABILITY_CLASSES = {
    "senior_comparable",
    "senior_non_comparable",
    "academy_youth",
    "role_only",
}
IDENTITY_COLUMNS = (
    "fpl_id",
    "player_code",
    "player_name",
    "position",
)
EVIDENCE_COLUMNS = (
    "comparability_class",
    "source_competition",
    "source_season",
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
    "observed_at",
    "source_reference",
    "rationale",
)
REQUIRED_COLUMNS = (*IDENTITY_COLUMNS, *EVIDENCE_COLUMNS)
INTEGER_STAT_COLUMNS = (
    "sample_minutes",
    "sample_starts",
    "saves",
    "yellow_cards",
    "red_cards",
    "bonus",
    "defensive_contribution",
)
SIGNED_INTEGER_STAT_COLUMNS = ("bps",)
FLOAT_STAT_COLUMNS = ("expected_goals", "expected_assists")
RATE_STAT_COLUMNS = (*INTEGER_STAT_COLUMNS, *SIGNED_INTEGER_STAT_COLUMNS, *FLOAT_STAT_COLUMNS)


@dataclass(frozen=True, slots=True)
class PlayerRateEvidenceImportResult:
    evidence_import_run_id: str
    source_ingestion_run_id: str
    target_gameweek: int
    source_sha256: str
    evidence_rows: int


def _optional_integers(
    frame: pd.DataFrame,
    column: str,
    *,
    non_negative: bool = True,
) -> pd.Series:
    numeric = pd.to_numeric(frame[column], errors="raise")
    non_missing = numeric.dropna()
    if (non_missing % 1 != 0).any() or (non_negative and (non_missing < 0).any()):
        qualifier = "non-negative whole numbers" if non_negative else "whole numbers"
        raise ValueError(f"{column} must contain {qualifier} or blanks")
    return numeric.astype("Int64")


def _optional_floats(frame: pd.DataFrame, column: str) -> pd.Series:
    numeric = pd.to_numeric(frame[column], errors="raise").astype("float64")
    if (numeric.dropna() < 0.0).any():
        raise ValueError(f"{column} must contain non-negative numbers or blanks")
    return numeric


def validate_player_rate_evidence(frame: pd.DataFrame) -> pd.DataFrame:
    """Canonicalize targeted evidence without translating it into an FPL rate."""
    missing = set(REQUIRED_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError("player-rate evidence missing columns: " + ", ".join(sorted(missing)))
    result = frame.loc[:, REQUIRED_COLUMNS].copy()
    if result.empty:
        raise ValueError("player-rate evidence must contain at least one row")

    for column in IDENTITY_COLUMNS[:3]:
        if column in {"fpl_id", "player_code"}:
            result[column] = _optional_integers(result, column)
            if result[column].isna().any() or (result[column] <= 0).any():
                raise ValueError(f"{column} must contain positive integers")
            result[column] = result[column].astype("int64")
        else:
            result[column] = result[column].fillna("").astype(str).str.strip()
            if (result[column] == "").any():
                raise ValueError(f"{column} must not be blank")
    result["position"] = result["position"].fillna("").astype(str).str.strip().str.upper()
    if not result["position"].isin({"GK", "DEF", "MID", "FWD"}).all():
        raise ValueError("position must be GK, DEF, MID, or FWD")
    if result["fpl_id"].duplicated().any() or result["player_code"].duplicated().any():
        raise ValueError("player-rate evidence contains duplicate player identities")

    result["comparability_class"] = (
        result["comparability_class"].fillna("").astype(str).str.strip().str.lower()
    )
    invalid_classes = sorted(set(result["comparability_class"]) - COMPARABILITY_CLASSES)
    if invalid_classes:
        raise ValueError("invalid comparability_class values: " + ", ".join(invalid_classes))
    for column in (
        "source_competition",
        "source_season",
        "source_reference",
        "rationale",
    ):
        result[column] = result[column].fillna("").astype(str).str.strip()
    if (result["source_reference"] == "").any() or (result["rationale"] == "").any():
        raise ValueError("source_reference and rationale must not be blank")

    for column in INTEGER_STAT_COLUMNS:
        result[column] = _optional_integers(result, column)
    for column in SIGNED_INTEGER_STAT_COLUMNS:
        result[column] = _optional_integers(result, column, non_negative=False)
    for column in FLOAT_STAT_COLUMNS:
        result[column] = _optional_floats(result, column)
    result["observed_at"] = pd.to_datetime(result["observed_at"], errors="raise", utc=True)

    role_only = result["comparability_class"] == "role_only"
    if result.loc[role_only, RATE_STAT_COLUMNS].notna().any(axis=None):
        raise ValueError("role_only evidence must not contain rate statistics")
    statistical = ~role_only
    if result.loc[statistical, "sample_minutes"].isna().any() or (
        result.loc[statistical, "sample_minutes"] <= 0
    ).any():
        raise ValueError("statistical evidence requires positive sample_minutes")
    if (
        (result.loc[statistical, "source_competition"] == "").any()
        or (result.loc[statistical, "source_season"] == "").any()
    ):
        raise ValueError(
            "statistical evidence requires source_competition and source_season"
        )
    return result.sort_values("player_code").reset_index(drop=True)


def _quality_flags(row: Any) -> tuple[str, ...]:
    flags = {"RESEARCH_EVIDENCE_NOT_PRODUCTION_RATE"}
    comparability = str(row.comparability_class)
    if comparability == "senior_comparable":
        flags.add("EXTERNAL_SENIOR_EVIDENCE_REQUIRES_TRANSLATION_VALIDATION")
    elif comparability == "senior_non_comparable":
        flags.add("NON_COMPARABLE_COMPETITION")
    elif comparability == "academy_youth":
        flags.add("ACADEMY_YOUTH_EVIDENCE_NOT_SENIOR_RATE")
    else:
        flags.add("ROLE_ONLY_NO_RATE_STATISTICS")
    if any(pd.isna(getattr(row, column)) for column in RATE_STAT_COLUMNS):
        flags.add("PARTIAL_RATE_STATISTICS")
    return tuple(sorted(flags))


def _nullable(value: object, caster: type[int] | type[float]) -> int | float | None:
    return None if pd.isna(value) else caster(value)


def import_player_rate_evidence(
    csv_path: str | Path,
    *,
    source_ingestion_run_id: str,
    target_gameweek: int,
    source_label: str,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    imported_at: datetime | None = None,
) -> PlayerRateEvidenceImportResult:
    """Persist reviewed evidence separately from production player-rate history."""
    path = Path(csv_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    if not source_ingestion_run_id.strip() or not source_label.strip():
        raise ValueError("source_ingestion_run_id and source_label must not be blank")
    if not 1 <= target_gameweek <= 38:
        raise ValueError("target_gameweek must be between 1 and 38")
    timestamp = imported_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("imported_at must be timezone-aware")

    source_bytes = path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    rows = validate_player_rate_evidence(pd.read_csv(path))
    if any(value.to_pydatetime() > timestamp for value in rows["observed_at"]):
        raise ValueError("evidence observed_at cannot be after imported_at")
    identity = (
        f"{source_ingestion_run_id}|{target_gameweek}|{source_sha256}".encode()
    )
    import_run_id = f"player_rate_evidence_{hashlib.sha256(identity).hexdigest()[:16]}"
    result = PlayerRateEvidenceImportResult(
        evidence_import_run_id=import_run_id,
        source_ingestion_run_id=source_ingestion_run_id,
        target_gameweek=target_gameweek,
        source_sha256=source_sha256,
        evidence_rows=len(rows),
    )
    initialize_database(database_path)

    with duckdb.connect(str(database_path)) as connection:
        source = connection.execute(
            "SELECT status FROM ingestion_run WHERE ingestion_run_id = ?",
            [source_ingestion_run_id],
        ).fetchone()
        if source is None or source[0] != "completed":
            raise ValueError("source_ingestion_run_id must identify a completed FPL snapshot")
        identities = {
            int(fpl_id): (int(player_code), str(name), str(position))
            for fpl_id, player_code, name, position in connection.execute(
                """
                SELECT fpl_id, player_code, web_name, fpl_position
                FROM player_snapshot WHERE ingestion_run_id = ?
                """,
                [source_ingestion_run_id],
            ).fetchall()
            if player_code is not None
        }
        for row in rows.itertuples(index=False):
            expected = identities.get(int(row.fpl_id))
            supplied = (int(row.player_code), str(row.player_name), str(row.position))
            if expected != supplied:
                raise ValueError(
                    f"evidence identity does not match pinned FPL snapshot for fpl_id={row.fpl_id}"
                )

        existing = connection.execute(
            """
            SELECT source_ingestion_run_id, target_gameweek, source_sha256, evidence_rows
            FROM player_rate_evidence_import_run WHERE evidence_import_run_id = ?
            """,
            [import_run_id],
        ).fetchone()
        if existing is not None:
            expected = (source_ingestion_run_id, target_gameweek, source_sha256, len(rows))
            if existing != expected:
                raise ValueError(f"player-rate evidence import ID collision: {import_run_id}")
            return result

        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """
                INSERT INTO player_rate_evidence_import_run VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, 'completed'
                )
                """,
                [
                    import_run_id,
                    source_ingestion_run_id,
                    target_gameweek,
                    source_label,
                    str(path.resolve()),
                    source_sha256,
                    timestamp,
                    len(rows),
                ],
            )
            connection.executemany(
                """
                INSERT INTO player_rate_evidence VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    (
                        import_run_id,
                        source_ingestion_run_id,
                        int(row.fpl_id),
                        int(row.player_code),
                        str(row.player_name),
                        str(row.position),
                        str(row.comparability_class),
                        str(row.source_competition) or None,
                        str(row.source_season) or None,
                        _nullable(row.sample_minutes, int),
                        _nullable(row.sample_starts, int),
                        _nullable(row.expected_goals, float),
                        _nullable(row.expected_assists, float),
                        _nullable(row.saves, int),
                        _nullable(row.yellow_cards, int),
                        _nullable(row.red_cards, int),
                        _nullable(row.bonus, int),
                        _nullable(row.bps, int),
                        _nullable(row.defensive_contribution, int),
                        row.observed_at.to_pydatetime(),
                        str(row.source_reference),
                        str(row.rationale),
                        json.dumps(_quality_flags(row)),
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
