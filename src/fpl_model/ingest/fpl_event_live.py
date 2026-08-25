"""Archive official per-gameweek FPL player statistics with finality gates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database

DEFAULT_RAW_ROOT = Path("data/raw/fpl_event_live")


@dataclass(frozen=True, slots=True)
class FPLEventLiveResult:
    live_run_id: str
    source_ingestion_run_id: str
    gameweek: int
    captured_at: datetime
    event_finished: bool
    data_checked: bool
    player_rows: int
    status: str
    source_path: Path


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _integer(value: object, field: str) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"FPL field {field!r} is not numeric: {value!r}") from error
    integer = int(number)
    if integer != number:
        raise ValueError(f"FPL field {field!r} is not an integer: {value!r}")
    return integer


def _number(value: object, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"FPL field {field!r} is not numeric: {value!r}") from error


def persist_fpl_event_live(
    *,
    payload: dict[str, Any],
    source_ingestion_run_id: str,
    gameweek: int,
    captured_at: datetime,
    season: str,
    require_final: bool = True,
    require_all_fixtures_finished: bool = False,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    raw_root: str | Path = DEFAULT_RAW_ROOT,
) -> FPLEventLiveResult:
    """Persist one event-live payload tied to an exact official snapshot."""
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("captured_at must be timezone-aware")
    if not 1 <= gameweek <= 38:
        raise ValueError("gameweek must be between 1 and 38")
    elements = payload.get("elements")
    if not isinstance(elements, list) or not elements:
        raise ValueError("event-live payload must contain non-empty elements")
    if not all(isinstance(item, dict) for item in elements):
        raise ValueError("event-live elements must contain objects")
    fpl_ids = [_integer(item.get("id"), "elements.id") for item in elements]
    if len(fpl_ids) != len(set(fpl_ids)):
        raise ValueError("event-live payload contains duplicate player IDs")

    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        event = connection.execute(
            """
            SELECT gw.finished, gw.data_checked, ir.captured_at
            FROM gameweek_snapshot AS gw
            JOIN ingestion_run AS ir USING (ingestion_run_id)
            WHERE gw.ingestion_run_id = ? AND gw.gameweek = ?
              AND ir.source = 'official_fpl_api' AND ir.status = 'completed'
            """,
            [source_ingestion_run_id, gameweek],
        ).fetchone()
        if event is None:
            raise ValueError("source snapshot does not contain the requested gameweek")
        event_finished, data_checked, snapshot_at = event
        if captured_at < snapshot_at:
            raise ValueError("event-live capture predates its source snapshot")
        fixture_counts = connection.execute(
            """
            SELECT count(*), count(*) FILTER (WHERE finished)
            FROM fixture_snapshot
            WHERE ingestion_run_id = ? AND gameweek = ?
            """,
            [source_ingestion_run_id, gameweek],
        ).fetchone()
        all_fixtures_finished = bool(
            fixture_counts[0] > 0 and fixture_counts[0] == fixture_counts[1]
        )
        if require_final and not (event_finished and data_checked):
            raise ValueError(
                f"GW{gameweek} is not final in source snapshot; "
                "finished and data_checked are required"
            )
        if require_all_fixtures_finished and not all_fixtures_finished:
            raise ValueError(
                f"GW{gameweek} is not analytically complete in source snapshot; "
                "all assigned fixtures must be finished"
            )

        players = {
            int(row[0]): row[1]
            for row in connection.execute(
                """
                SELECT fpl_id, player_code FROM player_snapshot
                WHERE ingestion_run_id = ?
                """,
                [source_ingestion_run_id],
            ).fetchall()
        }
        unknown = sorted(set(fpl_ids) - set(players))
        if unknown:
            raise ValueError(f"event-live players absent from source snapshot: {unknown[:10]}")

        payload_bytes = _canonical_json(payload)
        source_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        timestamp = captured_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        live_run_id = f"fpl_live_gw{gameweek}_{timestamp}_{source_sha256[:12]}"
        directory = Path(raw_root) / live_run_id
        source_path = (directory / f"event-{gameweek}-live.json").resolve()
        status = "completed" if event_finished and data_checked else "provisional"

        existing = connection.execute(
            "SELECT status, source_path FROM fpl_event_live_run WHERE live_run_id = ?",
            [live_run_id],
        ).fetchone()
        if existing is not None:
            return FPLEventLiveResult(
                live_run_id,
                source_ingestion_run_id,
                gameweek,
                captured_at,
                bool(event_finished),
                bool(data_checked),
                len(elements),
                str(existing[0]),
                Path(existing[1]),
            )

        rows = []
        for item in elements:
            fpl_id = _integer(item.get("id"), "elements.id")
            stats = item.get("stats")
            if not isinstance(stats, dict):
                raise ValueError(f"event-live player {fpl_id} has no stats object")
            flags: list[str] = []
            played = bool(stats.get("played"))
            minutes = _integer(stats.get("minutes"), "stats.minutes")
            starts = _integer(stats.get("starts"), "stats.starts")
            if not played and (minutes > 0 or starts > 0):
                flags.append("PLAYED_FLAG_CONFLICT")
            rows.append(
                (
                    live_run_id,
                    fpl_id,
                    players[fpl_id],
                    played,
                    minutes,
                    starts,
                    _integer(stats.get("goals_scored"), "stats.goals_scored"),
                    _integer(stats.get("assists"), "stats.assists"),
                    _integer(stats.get("saves"), "stats.saves"),
                    _integer(stats.get("yellow_cards"), "stats.yellow_cards"),
                    _integer(stats.get("red_cards"), "stats.red_cards"),
                    _integer(stats.get("bonus"), "stats.bonus"),
                    _integer(stats.get("bps"), "stats.bps"),
                    _integer(
                        stats.get("defensive_contribution"),
                        "stats.defensive_contribution",
                    ),
                    _number(stats.get("expected_goals"), "stats.expected_goals"),
                    _number(stats.get("expected_assists"), "stats.expected_assists"),
                    _number(
                        stats.get("expected_goals_conceded"),
                        "stats.expected_goals_conceded",
                    ),
                    _integer(stats.get("total_points"), "stats.total_points"),
                    bool(item.get("modified")),
                    json.dumps(flags),
                )
            )

        directory.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(payload_bytes + b"\n")
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """
                INSERT INTO fpl_event_live_run VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp
                )
                """,
                [
                    live_run_id,
                    source_ingestion_run_id,
                    season,
                    gameweek,
                    captured_at,
                    str(source_path),
                    source_sha256,
                    event_finished,
                    data_checked,
                    len(rows),
                    status,
                ],
            )
            connection.executemany(
                "INSERT INTO player_gameweek_stat VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    return FPLEventLiveResult(
        live_run_id,
        source_ingestion_run_id,
        gameweek,
        captured_at,
        bool(event_finished),
        bool(data_checked),
        len(elements),
        status,
        source_path,
    )
