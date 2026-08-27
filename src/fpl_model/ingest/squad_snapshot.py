"""Validated manual import of an immutable FPL manager-squad snapshot."""

from __future__ import annotations

import hashlib
import json
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
    selling_price_is_estimated: bool = False
    """``True`` only for a snapshot built by `import_squad_snapshot_from_entry`:
    FPL's public entry-picks API does not expose per-player purchase price, so
    `selling_price_tenths` is approximated from the CURRENT market price
    rather than FPL's own profit-sharing sale rule. A caller presenting this
    result to a user must surface that caveat rather than imply an exact
    FPL-matching sell value."""


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
    source_bytes = path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    private_rows = validate_squad_snapshot_csv(pd.read_csv(path))

    return _store_squad_snapshot(
        private_rows,
        entry_id=entry_id,
        entry_name=entry_name,
        season=season,
        target_gameweek=target_gameweek,
        source_ingestion_run_id=source_ingestion_run_id,
        captured_at=captured_at,
        source_label=source_label,
        source_path=str(path.resolve()),
        source_sha256=source_sha256,
        bank=bank,
        free_transfers=free_transfers,
        unlimited_transfers=unlimited_transfers,
        chip_period=chip_period,
        chip_states=chip_states,
        database_path=database_path,
    )


def _store_squad_snapshot(
    private_rows: pd.DataFrame,
    *,
    entry_id: int,
    entry_name: str | None,
    season: str,
    target_gameweek: int,
    source_ingestion_run_id: str,
    captured_at: datetime,
    source_label: str,
    source_path: str,
    source_sha256: str,
    bank: object,
    free_transfers: int | None,
    unlimited_transfers: bool,
    selling_price_is_estimated: bool = False,
    chip_period: int,
    chip_states: Mapping[str, str],
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> SquadSnapshotImportResult:
    """Join already-validated private squad rows to one official snapshot and store them.

    Shared storage core for both `import_squad_snapshot_csv` (manual CSV
    export) and `import_squad_snapshot_from_entry` (live fetch from FPL's
    public entry API) -- everything downstream of "I have 15 validated rows
    with an identity, source label, and source hash" is identical between
    the two, and duplicating it would risk the two paths drifting apart on
    what counts as a legal squad.
    """
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
                selling_price_is_estimated,
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
                    source_path,
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
        selling_price_is_estimated,
    )


def validate_entry_picks_payload(payload: Mapping[str, object]) -> pd.DataFrame:
    """Canonicalise one FPL public entry-picks payload into the private-rows contract.

    Mirrors `validate_squad_snapshot_csv`'s output columns exactly
    (`fpl_id`, `squad_position`, `is_captain`, `is_vice_captain`,
    `purchase_price_tenths`, `selling_price_tenths`) EXCEPT the two price
    columns are left as ``None`` here -- FPL's public
    `entry/{id}/event/{gw}/picks/` endpoint carries no per-player purchase or
    selling price at all (only `element`/`position`/`multiplier`/
    `is_captain`/`is_vice_captain`/`element_type`), so the caller must fill
    them in from a current market price before this can reach
    `_store_squad_snapshot`.
    """
    picks = payload.get("picks")
    if not isinstance(picks, list) or len(picks) != 15:
        raise ValueError(f"entry picks payload must contain exactly 15 picks, got {picks!r}")

    rows = []
    for pick in picks:
        if not isinstance(pick, Mapping):
            raise ValueError("entry picks payload contains a non-object pick")
        rows.append(
            {
                "fpl_id": _positive_integer(pick.get("element"), "element"),
                "squad_position": _positive_integer(pick.get("position"), "position"),
                "is_captain": bool(pick.get("is_captain")),
                "is_vice_captain": bool(pick.get("is_vice_captain")),
            }
        )
    result = pd.DataFrame(rows)
    if result["fpl_id"].duplicated().any():
        raise ValueError("entry picks payload contains duplicate element values")
    if sorted(result["squad_position"].tolist()) != list(range(1, 16)):
        raise ValueError(
            "entry picks payload's position values must contain every integer "
            "from 1 through 15 exactly once"
        )
    captains = result.index[result["is_captain"]].tolist()
    if len(captains) != 1:
        raise ValueError(f"entry picks payload must name exactly one captain, got {len(captains)}")
    vice_captains = result.index[result["is_vice_captain"]].tolist()
    if len(vice_captains) != 1:
        raise ValueError(
            f"entry picks payload must name exactly one vice-captain, got {len(vice_captains)}"
        )
    return result


def import_squad_snapshot_from_entry(
    picks_payload: Mapping[str, object],
    *,
    entry_id: int,
    entry_name: str | None,
    season: str,
    target_gameweek: int,
    source_ingestion_run_id: str,
    captured_at: datetime,
    free_transfers: int | None,
    unlimited_transfers: bool,
    chip_period: int,
    chip_states: Mapping[str, str],
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> SquadSnapshotImportResult:
    """Import a squad fetched live from FPL's public entry-picks API.

    ``picks_payload`` is the raw dict from `FPLClient.entry_picks` (or an
    equivalent object with the same shape) -- this function does not perform
    the HTTP fetch itself, keeping network I/O and storage separate the same
    way every other `ingest/*.py` module does.

    Bank comes from ``picks_payload["entry_history"]["bank"]`` (FPL's own
    tenths-of-a-million unit, matching this project's own convention
    directly). Free transfers are NOT derivable from this public payload --
    FPL exposes no "free transfers remaining" field on it -- so the caller
    must supply ``free_transfers``/``unlimited_transfers`` explicitly, the
    same contract `import_squad_snapshot_csv` already has.

    Purchase and selling price are NOT available from this public payload
    either. Both are approximated here from the player's CURRENT market
    price in `player_snapshot` (the same official snapshot
    ``source_ingestion_run_id`` points at) -- the returned result's
    ``selling_price_is_estimated`` is always ``True`` for this function, and
    a caller presenting this to a user MUST surface that caveat rather than
    imply an FPL-exact sell value (FPL's real selling price follows a
    profit-sharing rule on price rises that cannot be reconstructed from a
    single picks snapshot).
    """
    if not isinstance(picks_payload, Mapping):
        raise ValueError("picks_payload must be a mapping")
    entry_history = picks_payload.get("entry_history")
    if not isinstance(entry_history, Mapping) or "bank" not in entry_history:
        raise ValueError("picks_payload is missing entry_history.bank")
    # FPL's own payload already carries `bank` as an integer number of
    # tenths of a million (matching `bank_tenths` directly), but
    # `_store_squad_snapshot`'s `bank` parameter expects a whole-decimal
    # millions figure the same way a CSV import's `bank` column would --
    # dividing by 10 here converts FPL's unit into that shared contract.
    bank = Decimal(str(entry_history["bank"])) / 10

    private_rows = validate_entry_picks_payload(picks_payload)
    initialize_database(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        prices = connection.execute(
            """
            SELECT fpl_id, price
            FROM player_snapshot
            WHERE ingestion_run_id = ?
            """,
            [source_ingestion_run_id],
        ).fetchdf()
    price_by_fpl_id = {int(row.fpl_id): row.price for row in prices.itertuples(index=False)}
    missing_prices = sorted(set(private_rows["fpl_id"]) - set(price_by_fpl_id))
    if missing_prices:
        raise ValueError(
            f"squad players absent from source FPL snapshot: {missing_prices}"
        )
    estimated_price_tenths = [
        _price_to_tenths(price_by_fpl_id[fpl_id], f"current price for fpl_id={fpl_id}")
        for fpl_id in private_rows["fpl_id"]
    ]
    private_rows = private_rows.assign(
        purchase_price_tenths=estimated_price_tenths,
        selling_price_tenths=estimated_price_tenths,
    )

    return _store_squad_snapshot(
        private_rows,
        entry_id=entry_id,
        entry_name=entry_name,
        season=season,
        target_gameweek=target_gameweek,
        source_ingestion_run_id=source_ingestion_run_id,
        captured_at=captured_at,
        source_label=(
            "live FPL entry fetch (purchase/selling price estimated from "
            "current market price, not FPL's own profit-sharing sale rule)"
        ),
        source_path=(
            f"https://fantasy.premierleague.com/api/entry/{entry_id}"
            f"/event/{target_gameweek}/picks/"
        ),
        source_sha256=hashlib.sha256(
            json.dumps(picks_payload, sort_keys=True, default=str).encode()
        ).hexdigest(),
        bank=bank,
        free_transfers=free_transfers,
        unlimited_transfers=unlimited_transfers,
        chip_period=chip_period,
        chip_states=chip_states,
        selling_price_is_estimated=True,
        database_path=database_path,
    )
