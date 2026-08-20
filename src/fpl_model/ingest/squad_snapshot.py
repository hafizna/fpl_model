"""Validated manual import of an immutable FPL manager-squad snapshot."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import duckdb
import pandas as pd

from fpl_model.decision.squad import SquadPlayer, validate_squad
from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database

REQUIRED_COLUMNS = (
    "fpl_id",
    "purchase_price",
    "selling_price",
    "squad_position",
    "is_captain",
    "is_vice_captain",
)


@dataclass(frozen=True, slots=True)
class SquadSnapshotImportResult:
    squad_snapshot_id: str
    entry_id: int
    season: str
    target_gameweek: int
    source_sha256: str
    player_rows: int
    team_value_tenths: int
    constraint_flags: tuple[str, ...]


def _positive_integer(value: object, field: str) -> int:
    try:
        numeric = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if not numeric.is_finite() or numeric != numeric.to_integral_value() or numeric <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return int(numeric)


def _price_to_tenths(value: object, field: str, *, allow_zero: bool = False) -> int:
    try:
        numeric = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a price with at most one decimal place") from exc
    tenths = numeric * 10
    lower_bound = 0 if allow_zero else 1
    if (
        not numeric.is_finite()
        or tenths != tenths.to_integral_value()
        or tenths < lower_bound
    ):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{field} must be a {qualifier} price with at most one decimal place")
    return int(tenths)


def _boolean(value: object, field: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError(f"{field} must contain true/false values")


def validate_squad_snapshot_csv(frame: pd.DataFrame) -> pd.DataFrame:
    """Canonicalise the private manager-state columns before joining FPL identity data."""
    missing = set(REQUIRED_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError("squad snapshot missing columns: " + ", ".join(sorted(missing)))
    result = frame.loc[:, REQUIRED_COLUMNS].copy()
    if len(result) != 15:
        raise ValueError(f"squad snapshot CSV must contain exactly 15 rows, got {len(result)}")

    for column in ("fpl_id", "squad_position"):
        result[column] = [_positive_integer(value, column) for value in result[column]]
    if result["fpl_id"].duplicated().any():
        raise ValueError("squad snapshot contains duplicate fpl_id values")
    if sorted(result["squad_position"].tolist()) != list(range(1, 16)):
        raise ValueError("squad_position must contain every integer from 1 through 15 exactly once")

    for column in ("purchase_price", "selling_price"):
        result[f"{column}_tenths"] = [
            _price_to_tenths(value, column) for value in result[column]
        ]
    for column in ("is_captain", "is_vice_captain"):
        result[column] = [_boolean(value, column) for value in result[column]]

    return result.drop(columns=["purchase_price", "selling_price"]).sort_values(
        "squad_position"
    ).reset_index(drop=True)


def import_squad_snapshot_csv(
    csv_path: str | Path,
    *,
    entry_id: int,
    entry_name: str | None,
    season: str,
    target_gameweek: int,
    source_ingestion_run_id: str,
    captured_at: datetime,
    source_label: str,
    bank: object,
    free_transfers: int | None,
    unlimited_transfers: bool,
    chip_period: int,
    chip_states: Mapping[str, str],
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> SquadSnapshotImportResult:
    """Join private squad state to one official snapshot and store it transactionally."""
    path = Path(csv_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    if entry_id <= 0:
        raise ValueError("entry_id must be positive")
    if not season.strip() or not source_label.strip() or not source_ingestion_run_id.strip():
        raise ValueError("season, source_label, and source_ingestion_run_id must not be blank")
    if not 1 <= target_gameweek <= 38:
        raise ValueError("target_gameweek must be between 1 and 38")
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("captured_at must be timezone-aware")

    bank_tenths = _price_to_tenths(bank, "bank", allow_zero=True)
    expected_chip_period = 1 if target_gameweek <= 19 else 2
    if chip_period != expected_chip_period:
        raise ValueError(
            f"chip_period must be {expected_chip_period} for target gameweek {target_gameweek}"
        )
    source_bytes = path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    private_rows = validate_squad_snapshot_csv(pd.read_csv(path))
    initialize_database(database_path)

    with duckdb.connect(str(database_path)) as connection:
        ingestion = connection.execute(
            """
            SELECT captured_at, status
            FROM ingestion_run
            WHERE ingestion_run_id = ?
            """,
            [source_ingestion_run_id],
        ).fetchone()
        if ingestion is None:
            raise ValueError(f"unknown source_ingestion_run_id: {source_ingestion_run_id}")
        source_captured_at, source_status = ingestion
        if source_status != "completed":
            raise ValueError("source FPL ingestion run must be completed")
        if source_captured_at > captured_at:
            raise ValueError("source FPL snapshot cannot be newer than the squad snapshot")

        deadline_row = connection.execute(
            """
            SELECT deadline_time
            FROM gameweek_snapshot
            WHERE ingestion_run_id = ? AND gameweek = ?
            """,
            [source_ingestion_run_id, target_gameweek],
        ).fetchone()
        if deadline_row is None:
            raise ValueError(
                f"source FPL snapshot has no deadline for target gameweek {target_gameweek}"
            )
        if captured_at > deadline_row[0]:
            raise ValueError("captured_at must not be after the target gameweek deadline")

        official = connection.execute(
            """
            SELECT fpl_id, player_code, web_name, team_id, fpl_position, price
            FROM player_snapshot
            WHERE ingestion_run_id = ? AND season = ?
            """,
            [source_ingestion_run_id, season],
        ).fetchdf()
        official_by_id = {int(row.fpl_id): row for row in official.itertuples(index=False)}
        missing_ids = sorted(set(private_rows["fpl_id"]) - set(official_by_id))
        if missing_ids:
            raise ValueError(
                f"squad players absent from source FPL snapshot: {missing_ids}"
            )

        players = []
        for row in private_rows.itertuples(index=False):
            identity = official_by_id[int(row.fpl_id)]
            players.append(
                SquadPlayer(
                    fpl_id=int(row.fpl_id),
                    player_code=(
                        None if pd.isna(identity.player_code) else int(identity.player_code)
                    ),
                    player_name=str(identity.web_name),
                    team_id=int(identity.team_id),
                    position=str(identity.fpl_position),
                    current_price_tenths=_price_to_tenths(
                        identity.price, f"current price for fpl_id={row.fpl_id}"
                    ),
                    purchase_price_tenths=int(row.purchase_price_tenths),
                    selling_price_tenths=int(row.selling_price_tenths),
                    squad_position=int(row.squad_position),
                    is_captain=bool(row.is_captain),
                    is_vice_captain=bool(row.is_vice_captain),
                )
            )

        validated = validate_squad(
            players,
            bank_tenths=bank_tenths,
            free_transfers=free_transfers,
            unlimited_transfers=unlimited_transfers,
            chip_period=chip_period,
            chip_states=chip_states,
            allow_grandfathered_team_limit=True,
        )

        identity_text = "|".join(
            (
                str(entry_id),
                season,
                str(target_gameweek),
                captured_at.isoformat(),
                source_ingestion_run_id,
                source_sha256,
                str(bank_tenths),
                str(free_transfers),
                str(unlimited_transfers),
                str(chip_period),
                repr(validated.chip_states),
                repr(validated.constraint_flags),
            )
        )
        snapshot_id = f"squad_{hashlib.sha256(identity_text.encode()).hexdigest()[:16]}"
        existing = connection.execute(
            """
            SELECT entry_id, season, target_gameweek, source_sha256, player_rows
            FROM squad_snapshot WHERE squad_snapshot_id = ?
            """,
            [snapshot_id],
        ).fetchone()
        if existing is not None:
            expected = (entry_id, season, target_gameweek, source_sha256, 15)
            if existing != expected:
                raise ValueError(f"squad snapshot ID collision: {snapshot_id}")
            return SquadSnapshotImportResult(
                snapshot_id,
                entry_id,
                season,
                target_gameweek,
                source_sha256,
                15,
                validated.team_value_tenths,
                validated.constraint_flags,
            )

        existing_entry = connection.execute(
            "SELECT entry_name FROM manager_entry WHERE entry_id = ?", [entry_id]
        ).fetchone()
        normalized_entry_name = entry_name.strip() if entry_name and entry_name.strip() else None
        if existing_entry is not None and (
            normalized_entry_name is not None and existing_entry[0] not in (None, normalized_entry_name)
        ):
            raise ValueError(f"entry_id {entry_id} already has a different entry_name")

        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                "INSERT INTO manager_entry VALUES (?, ?, current_timestamp) ON CONFLICT DO NOTHING",
                [entry_id, normalized_entry_name],
            )
            connection.execute(
                """
                INSERT INTO squad_snapshot (
                    squad_snapshot_id, entry_id, source_ingestion_run_id, season,
                    target_gameweek, captured_at, source_label, source_path,
                    source_sha256, bank_tenths, free_transfers, unlimited_transfers,
                    chip_period, player_rows, constraint_flags, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 15, ?, 'completed')
                """,
                [
                    snapshot_id,
                    entry_id,
                    source_ingestion_run_id,
                    season,
                    target_gameweek,
                    captured_at,
                    source_label,
                    str(path.resolve()),
                    source_sha256,
                    bank_tenths,
                    free_transfers,
                    unlimited_transfers,
                    chip_period,
                    "|".join(validated.constraint_flags),
                ],
            )
            connection.executemany(
                "INSERT INTO squad_snapshot_player VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        snapshot_id,
                        player.fpl_id,
                        player.purchase_price_tenths,
                        player.selling_price_tenths,
                        player.squad_position,
                        player.is_captain,
                        player.is_vice_captain,
                    )
                    for player in validated.players
                ],
            )
            connection.executemany(
                "INSERT INTO squad_chip_state VALUES (?, ?, ?)",
                [(snapshot_id, chip, status) for chip, status in validated.chip_states],
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    return SquadSnapshotImportResult(
        snapshot_id,
        entry_id,
        season,
        target_gameweek,
        source_sha256,
        15,
        validated.team_value_tenths,
        validated.constraint_flags,
    )
