"""Explicit canonical player-ID bridge for official FPL and Vaastav data.

The official API's ``id`` (and Vaastav's mirrored ``id``/``element``) is a
season-local identifier.  The official ``code`` is the stable person key that
Vaastav preserves in ``players_raw.csv``.  This module makes that existing
relationship explicit and audited instead of allowing modelling code to infer
identity from names.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database

POLICY_VERSION = "official_fpl_vaastav_player_code_v1"
BRIDGE_COLUMNS = (
    "canonical_player_id",
    "provider",
    "provider_player_id",
    "player_name",
    "match_method",
    "data_quality_flags",
)


@dataclass(frozen=True, slots=True)
class PlayerIdentityBridgeBuild:
    rows: pd.DataFrame
    official_players: int
    vaastav_players: int
    matched_players: int
    official_only_player_ids: tuple[int, ...]
    vaastav_only_player_ids: tuple[int, ...]
    name_mismatch_player_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PlayerIdentityBridgeImportResult:
    bridge_run_id: str
    source_ingestion_run_id: str
    target_season: str
    vaastav_season: str
    source_revision: str
    source_sha256: str
    official_players: int
    vaastav_players: int
    matched_players: int
    official_only_players: int
    vaastav_only_players: int
    name_mismatch_players: int
    status: str


def _required_columns(frame: pd.DataFrame, columns: set[str], label: str) -> None:
    missing = columns - set(frame.columns)
    if missing:
        raise ValueError(f"{label} missing columns: {', '.join(sorted(missing))}")
    if frame.empty:
        raise ValueError(f"{label} must contain at least one player")


def _positive_integers(frame: pd.DataFrame, column: str, label: str) -> pd.Series:
    try:
        numeric = pd.to_numeric(frame[column], errors="raise")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}.{column} must contain positive integers") from exc
    if numeric.isna().any() or (numeric % 1 != 0).any() or (numeric <= 0).any():
        raise ValueError(f"{label}.{column} must contain positive integers")
    return numeric.astype("int64")


def _player_names(frame: pd.DataFrame, label: str) -> pd.Series:
    names = (
        frame["first_name"].fillna("").astype(str).str.strip()
        + " "
        + frame["second_name"].fillna("").astype(str).str.strip()
    ).str.replace(r"\s+", " ", regex=True).str.strip()
    if (names == "").any():
        raise ValueError(f"{label} contains a blank player name")
    return names


def _normalise_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _canonical_provider_rows(
    frame: pd.DataFrame,
    *,
    label: str,
    provider: str,
    provider_id_column: str,
    code_column: str,
) -> pd.DataFrame:
    _required_columns(
        frame,
        {provider_id_column, code_column, "first_name", "second_name"},
        label,
    )
    result = pd.DataFrame(
        {
            "canonical_player_id": _positive_integers(frame, code_column, label),
            "provider": provider,
            "provider_player_id": _positive_integers(
                frame, provider_id_column, label
            ).astype(str),
            "player_name": _player_names(frame, label),
        }
    )
    if result["provider_player_id"].duplicated().any():
        raise ValueError(f"{label} contains duplicate provider player IDs")
    if result["canonical_player_id"].duplicated().any():
        duplicates = sorted(
            result.loc[
                result["canonical_player_id"].duplicated(keep=False),
                "canonical_player_id",
            ].unique()
        )
        raise ValueError(f"{label} maps multiple players to canonical IDs: {duplicates}")
    return result.sort_values("canonical_player_id").reset_index(drop=True)


def build_player_identity_bridge(
    official_players: pd.DataFrame,
    vaastav_players_raw: pd.DataFrame,
) -> PlayerIdentityBridgeBuild:
    """Build provider-ID rows joined only by the shared stable player code.

    Names are diagnostics, never join keys.  Current-only and historical-only
    players remain in the bridge with explicit flags so a missing cross-provider
    link cannot silently become a name-based match.
    """
    official = _canonical_provider_rows(
        official_players,
        label="official FPL players",
        provider="official_fpl",
        provider_id_column="fpl_id",
        code_column="player_code",
    )
    vaastav = _canonical_provider_rows(
        vaastav_players_raw,
        label="Vaastav players_raw",
        provider="vaastav",
        provider_id_column="id",
        code_column="code",
    )

    official_ids = set(official["canonical_player_id"])
    vaastav_ids = set(vaastav["canonical_player_id"])
    matched = official_ids & vaastav_ids
    if not matched:
        raise ValueError("official FPL and Vaastav have no shared player_code values")
    official_only = tuple(sorted(official_ids - vaastav_ids))
    vaastav_only = tuple(sorted(vaastav_ids - official_ids))

    official_names = official.set_index("canonical_player_id")["player_name"].to_dict()
    vaastav_names = vaastav.set_index("canonical_player_id")["player_name"].to_dict()
    name_mismatches = tuple(
        sorted(
            player_id
            for player_id in matched
            if _normalise_name(official_names[player_id])
            != _normalise_name(vaastav_names[player_id])
        )
    )

    mismatch_set = set(name_mismatches)
    official_only_set = set(official_only)
    vaastav_only_set = set(vaastav_only)
    output_frames: list[pd.DataFrame] = []
    for source in (official, vaastav):
        provider = str(source["provider"].iloc[0])
        rows = source.copy()
        rows["match_method"] = rows["canonical_player_id"].map(
            lambda player_id: (
                "shared_player_code" if player_id in matched else "provider_code_only"
            )
        )

        def flags(player_id: int, provider_name: str = provider) -> str:
            values: list[str] = []
            if player_id in mismatch_set:
                values.append("NAME_MISMATCH")
            if provider_name == "official_fpl" and player_id in official_only_set:
                values.append("MISSING_VAASTAV_ID")
            if provider_name == "vaastav" and player_id in vaastav_only_set:
                values.append("MISSING_OFFICIAL_FPL_ID")
            return json.dumps(values, separators=(",", ":"))

        rows["data_quality_flags"] = rows["canonical_player_id"].map(flags)
        output_frames.append(rows.loc[:, BRIDGE_COLUMNS])

    bridge_rows = pd.concat(output_frames, ignore_index=True).sort_values(
        ["canonical_player_id", "provider"]
    ).reset_index(drop=True)
    return PlayerIdentityBridgeBuild(
        rows=bridge_rows,
        official_players=len(official),
        vaastav_players=len(vaastav),
        matched_players=len(matched),
        official_only_player_ids=official_only,
        vaastav_only_player_ids=vaastav_only,
        name_mismatch_player_ids=name_mismatches,
    )


def import_player_identity_bridge(
    vaastav_players_csv: str | Path,
    *,
    source_ingestion_run_id: str,
    target_season: str,
    vaastav_season: str,
    source_revision: str,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> PlayerIdentityBridgeImportResult:
    """Persist an immutable bridge from one FPL snapshot and pinned Vaastav file."""
    path = Path(vaastav_players_csv)
    if not path.is_file():
        raise FileNotFoundError(path)
    values = (source_ingestion_run_id, target_season, vaastav_season, source_revision)
    if any(not value.strip() for value in values):
        raise ValueError("run, season, and revision values must not be blank")

    source_bytes = path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    vaastav_players = pd.read_csv(path)
    initialize_database(database_path)

    with duckdb.connect(str(database_path)) as connection:
        run = connection.execute(
            """
            SELECT source, status FROM ingestion_run
            WHERE ingestion_run_id = ?
            """,
            [source_ingestion_run_id],
        ).fetchone()
        if run is None or run != ("official_fpl_api", "completed"):
            raise ValueError("source ingestion run must be a completed official FPL snapshot")
        official_players = connection.execute(
            """
            SELECT fpl_id, player_code, first_name, second_name
            FROM player_snapshot
            WHERE ingestion_run_id = ? AND season = ?
            ORDER BY fpl_id
            """,
            [source_ingestion_run_id, target_season],
        ).fetchdf()
        if official_players.empty:
            raise ValueError("source FPL snapshot has no players for target_season")

        built = build_player_identity_bridge(official_players, vaastav_players)
        identity = "|".join(
            (
                source_ingestion_run_id,
                target_season,
                vaastav_season,
                source_revision,
                source_sha256,
                POLICY_VERSION,
            )
        ).encode()
        bridge_run_id = f"player_bridge_{hashlib.sha256(identity).hexdigest()[:16]}"
        official_only = len(built.official_only_player_ids)
        vaastav_only = len(built.vaastav_only_player_ids)
        name_mismatches = len(built.name_mismatch_player_ids)
        status = (
            "completed"
            if official_only == 0 and name_mismatches == 0
            else "completed_with_gaps"
        )
        result = PlayerIdentityBridgeImportResult(
            bridge_run_id=bridge_run_id,
            source_ingestion_run_id=source_ingestion_run_id,
            target_season=target_season,
            vaastav_season=vaastav_season,
            source_revision=source_revision,
            source_sha256=source_sha256,
            official_players=built.official_players,
            vaastav_players=built.vaastav_players,
            matched_players=built.matched_players,
            official_only_players=official_only,
            vaastav_only_players=vaastav_only,
            name_mismatch_players=name_mismatches,
            status=status,
        )

        existing = connection.execute(
            """
            SELECT source_ingestion_run_id, target_season, vaastav_season,
                   source_revision, source_sha256, official_players,
                   vaastav_players, matched_players, official_only_players,
                   vaastav_only_players, name_mismatch_players, status
            FROM player_identity_bridge_run WHERE bridge_run_id = ?
            """,
            [bridge_run_id],
        ).fetchone()
        expected = (
            source_ingestion_run_id,
            target_season,
            vaastav_season,
            source_revision,
            source_sha256,
            built.official_players,
            built.vaastav_players,
            built.matched_players,
            official_only,
            vaastav_only,
            name_mismatches,
            status,
        )
        if existing is not None:
            if existing != expected:
                raise ValueError(f"player identity bridge ID collision: {bridge_run_id}")
            return result

        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """
                INSERT INTO player_identity_bridge_run VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp
                )
                """,
                [
                    bridge_run_id,
                    source_ingestion_run_id,
                    target_season,
                    vaastav_season,
                    source_revision,
                    str(path.resolve()),
                    source_sha256,
                    POLICY_VERSION,
                    built.official_players,
                    built.vaastav_players,
                    built.matched_players,
                    official_only,
                    vaastav_only,
                    name_mismatches,
                    status,
                ],
            )
            connection.executemany(
                "INSERT INTO player_identity_bridge VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (bridge_run_id, *row)
                    for row in built.rows.itertuples(index=False, name=None)
                ],
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")
    return result
