"""Deadline-safe availability and eligibility resolution.

This layer resolves evidence into a causal input for the appearance model. It
does not modify expected points directly and does not infer probabilities from
free-text injury news.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path

import duckdb

from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database

POLICY_VERSION = "fpl_availability_v1"
KNOWN_FPL_STATUSES = frozenset({"a", "d", "i", "s", "u", "n"})
HARD_BLOCK_STATUSES = frozenset({"s", "u", "n"})


def _validate_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _validate_probability(value: float | None, field: str) -> None:
    if value is not None and (not isfinite(value) or not 0.0 <= value <= 1.0):
        raise ValueError(f"{field} must be finite and between 0 and 1")


@dataclass(frozen=True, slots=True)
class AvailabilityInput:
    fpl_id: int
    player_code: int | None
    fpl_status: str
    official_chance: int | None
    chance_horizon_available: bool
    can_select: bool
    removed: bool
    news: str = ""
    news_added: datetime | None = None

    def __post_init__(self) -> None:
        if self.fpl_id <= 0:
            raise ValueError("fpl_id must be positive")
        if self.official_chance is not None and not 0 <= self.official_chance <= 100:
            raise ValueError("official_chance must be between 0 and 100")
        if self.news_added is not None:
            _validate_aware(self.news_added, "news_added")


@dataclass(frozen=True, slots=True)
class ReviewedAvailabilityOverride:
    override_id: str
    player_code: int
    target_gameweek: int
    observed_at: datetime
    source: str
    rationale: str
    availability_probability: float | None = None
    is_eligible: bool | None = None
    effective_until: datetime | None = None

    def __post_init__(self) -> None:
        if not self.override_id.strip() or not self.source.strip() or not self.rationale.strip():
            raise ValueError("override ID, source, and rationale must not be blank")
        if self.player_code <= 0 or self.target_gameweek <= 0:
            raise ValueError("player_code and target_gameweek must be positive")
        _validate_aware(self.observed_at, "observed_at")
        if self.effective_until is not None:
            _validate_aware(self.effective_until, "effective_until")
            if self.effective_until < self.observed_at:
                raise ValueError("effective_until cannot precede observed_at")
        _validate_probability(
            self.availability_probability,
            "availability_probability",
        )
        if self.availability_probability is None and self.is_eligible is None:
            raise ValueError("an override must set probability or eligibility")
        if self.is_eligible is False and self.availability_probability not in (None, 0.0):
            raise ValueError("an ineligible override cannot have positive availability")


@dataclass(frozen=True, slots=True)
class AvailabilityResolution:
    fpl_id: int
    player_code: int | None
    fpl_status: str
    official_chance: int | None
    availability_probability: float | None
    is_eligible: bool | None
    selected_source: str
    selected_override_id: str | None
    reason: str
    data_quality_flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AvailabilityRunResult:
    resolution_run_id: str
    source_ingestion_run_id: str
    target_gameweek: int
    as_of: datetime
    deadline: datetime
    players: int
    resolved_players: int
    unresolved_players: int
    blocked_players: int
    status: str


@dataclass(frozen=True, slots=True)
class AvailabilityOverrideStoreResult:
    override_id: str
    latest_causal_snapshot_at: datetime
    deadline: datetime
    requires_fpl_refresh: bool


def resolve_availability(
    player: AvailabilityInput,
    *,
    as_of: datetime,
    target_gameweek: int,
    override: ReviewedAvailabilityOverride | None = None,
) -> AvailabilityResolution:
    """Resolve one player without interpreting injury-news prose."""
    _validate_aware(as_of, "as_of")
    if target_gameweek <= 0:
        raise ValueError("target_gameweek must be positive")
    if player.news_added is not None and player.news_added > as_of:
        flags = {"NEWS_TIMESTAMP_AFTER_SNAPSHOT"}
    else:
        flags = set()
    if player.news and player.news_added is None:
        flags.add("FPL_NEWS_WITHOUT_TIMESTAMP")
    if player.fpl_status not in KNOWN_FPL_STATUSES:
        flags.add("UNKNOWN_FPL_STATUS")
    if not player.chance_horizon_available:
        flags.add("TARGET_OUTSIDE_FPL_CHANCE_HORIZON")

    if override is not None:
        if override.player_code != player.player_code:
            raise ValueError("override player_code does not match player")
        if override.target_gameweek != target_gameweek:
            raise ValueError("override target_gameweek does not match target")
        if override.observed_at > as_of:
            raise ValueError("override observed after as_of")
        if override.effective_until is not None and override.effective_until < as_of:
            raise ValueError("override expired before as_of")
        flags.add("REVIEWED_OVERRIDE")

    override_eligibility = override.is_eligible if override is not None else None
    if override_eligibility is not None:
        is_eligible = override_eligibility
        eligibility_source = "reviewed_override"
    elif player.removed or not player.can_select:
        is_eligible = False
        eligibility_source = "fpl_roster_state"
        flags.add("FPL_ROSTER_BLOCKED")
    elif player.fpl_status in HARD_BLOCK_STATUSES:
        is_eligible = False
        eligibility_source = "fpl_status"
        flags.add("FPL_STATUS_BLOCKED")
    elif player.fpl_status in {"a", "d", "i"}:
        is_eligible = True
        eligibility_source = "fpl_status"
    else:
        is_eligible = None
        eligibility_source = "unresolved"

    override_probability = (
        override.availability_probability if override is not None else None
    )
    if is_eligible is False:
        probability = 0.0
        selected_source = eligibility_source
        reason = "Player is blocked by reviewed eligibility or official roster status."
        if override_probability not in (None, 0.0):
            flags.add("OVERRIDE_PROBABILITY_SUPERSEDED_BY_INELIGIBILITY")
    elif override_probability is not None:
        probability = override_probability
        selected_source = "reviewed_override"
        reason = "A reviewed, deadline-safe override supplies availability probability."
    elif player.chance_horizon_available and player.official_chance is not None:
        probability = player.official_chance / 100.0
        selected_source = "official_fpl_chance"
        reason = "Official FPL chance field supplies availability probability."
    elif player.chance_horizon_available and player.fpl_status == "a":
        probability = 1.0
        selected_source = "official_fpl_status"
        reason = "Official FPL status is available and its chance field is blank."
    else:
        probability = None
        selected_source = "unresolved"
        reason = "No explicit deadline-relevant probability or reviewed override is available."
        flags.add("MISSING_AVAILABILITY_PROBABILITY")

    if player.fpl_status in HARD_BLOCK_STATUSES and player.official_chance not in (None, 0):
        flags.add("FPL_STATUS_CHANCE_CONFLICT")
    if player.fpl_status == "a" and player.official_chance not in (None, 100):
        flags.add("FPL_STATUS_CHANCE_CONFLICT")

    return AvailabilityResolution(
        fpl_id=player.fpl_id,
        player_code=player.player_code,
        fpl_status=player.fpl_status,
        official_chance=player.official_chance,
        availability_probability=probability,
        is_eligible=is_eligible,
        selected_source=selected_source,
        selected_override_id=override.override_id if override is not None else None,
        reason=reason,
        data_quality_flags=tuple(sorted(flags)),
    )


def create_reviewed_override(
    *,
    player_code: int,
    target_gameweek: int,
    observed_at: datetime,
    source: str,
    rationale: str,
    availability_probability: float | None = None,
    is_eligible: bool | None = None,
    effective_until: datetime | None = None,
) -> ReviewedAvailabilityOverride:
    """Create a content-addressed reviewed override for idempotent storage."""
    _validate_aware(observed_at, "observed_at")
    if effective_until is not None:
        _validate_aware(effective_until, "effective_until")
    identity = {
        "player_code": player_code,
        "target_gameweek": target_gameweek,
        "observed_at": observed_at.astimezone(UTC).isoformat(),
        "source": source,
        "rationale": rationale,
        "availability_probability": availability_probability,
        "is_eligible": is_eligible,
        "effective_until": (
            effective_until.astimezone(UTC).isoformat()
            if effective_until is not None
            else None
        ),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ReviewedAvailabilityOverride(
        override_id=f"availability_override_{digest[:16]}",
        player_code=player_code,
        target_gameweek=target_gameweek,
        observed_at=observed_at,
        source=source,
        rationale=rationale,
        availability_probability=availability_probability,
        is_eligible=is_eligible,
        effective_until=effective_until,
    )


def store_reviewed_override(
    override: ReviewedAvailabilityOverride,
    *,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> AvailabilityOverrideStoreResult:
    """Validate and append one reviewed override without rewriting old evidence."""
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        snapshot = connection.execute(
            """
            SELECT ir.captured_at, gw.deadline_time
            FROM ingestion_run AS ir
            JOIN gameweek_snapshot AS gw
              ON gw.ingestion_run_id = ir.ingestion_run_id
            JOIN player_snapshot AS p
              ON p.ingestion_run_id = ir.ingestion_run_id
            WHERE ir.source = 'official_fpl_api'
              AND ir.status = 'completed'
              AND gw.gameweek = ?
              AND p.player_code = ?
              AND ir.captured_at <= gw.deadline_time
            ORDER BY ir.captured_at DESC
            LIMIT 1
            """,
            [override.target_gameweek, override.player_code],
        ).fetchone()
        if snapshot is None:
            raise ValueError(
                "player/gameweek not found in a causal official FPL snapshot"
            )
        snapshot_at, deadline = snapshot
        if override.observed_at > deadline:
            raise ValueError("override observed after the target deadline")

        resolved_already = connection.execute(
            """
            SELECT count(*)
            FROM availability_resolution_run AS rr
            JOIN ingestion_run AS ir
              ON ir.ingestion_run_id = rr.source_ingestion_run_id
            WHERE rr.target_gameweek = ? AND ir.captured_at >= ?
            """,
            [override.target_gameweek, override.observed_at],
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO availability_override (
                override_id, player_code, target_gameweek, observed_at,
                effective_until, availability_probability, is_eligible,
                source, rationale
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            [
                override.override_id,
                override.player_code,
                override.target_gameweek,
                override.observed_at,
                override.effective_until,
                override.availability_probability,
                override.is_eligible,
                override.source,
                override.rationale,
            ],
        )

    return AvailabilityOverrideStoreResult(
        override_id=override.override_id,
        latest_causal_snapshot_at=snapshot_at,
        deadline=deadline,
        requires_fpl_refresh=(
            override.observed_at > snapshot_at or bool(resolved_already)
        ),
    )


def _resolution_run_id(source_run_id: str, target_gameweek: int) -> str:
    identity = f"{source_run_id}|{target_gameweek}|{POLICY_VERSION}".encode()
    return f"availability_{hashlib.sha256(identity).hexdigest()[:16]}"


def materialize_latest_fpl_availability(
    *,
    target_gameweek: int,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> AvailabilityRunResult:
    """Resolve the newest official FPL snapshot that existed before a deadline."""
    if target_gameweek <= 0:
        raise ValueError("target_gameweek must be positive")
    initialize_database(database_path)

    with duckdb.connect(str(database_path)) as connection:
        source = connection.execute(
            """
            SELECT ir.ingestion_run_id, ir.captured_at, gw.deadline_time,
                   gw.is_current, gw.is_next
            FROM ingestion_run AS ir
            JOIN gameweek_snapshot AS gw
              ON gw.ingestion_run_id = ir.ingestion_run_id
            WHERE ir.source = 'official_fpl_api'
              AND ir.status = 'completed'
              AND gw.gameweek = ?
              AND ir.captured_at <= gw.deadline_time
            ORDER BY ir.captured_at DESC
            LIMIT 1
            """,
            [target_gameweek],
        ).fetchone()
        if source is None:
            raise ValueError(
                f"no causal official FPL snapshot found for GW{target_gameweek}"
            )
        source_run_id, as_of, deadline, is_current, is_next = source
        resolution_run_id = _resolution_run_id(source_run_id, target_gameweek)
        chance_column = None
        if is_current:
            chance_column = "chance_of_playing_this_round"
        elif is_next:
            chance_column = "chance_of_playing_next_round"

        existing = connection.execute(
            """
            SELECT status FROM availability_resolution_run
            WHERE resolution_run_id = ?
            """,
            [resolution_run_id],
        ).fetchone()
        if existing is not None:
            counts = connection.execute(
                """
                SELECT count(*), count(availability_probability),
                       count(*) - count(availability_probability),
                       count(*) FILTER (WHERE is_eligible = false)
                FROM player_availability_resolution
                WHERE resolution_run_id = ?
                """,
                [resolution_run_id],
            ).fetchone()
            return AvailabilityRunResult(
                resolution_run_id,
                source_run_id,
                target_gameweek,
                as_of,
                deadline,
                *map(int, counts),
                existing[0],
            )

        chance_expression = (
            f"p.{chance_column}" if chance_column is not None else "NULL"
        )
        players = connection.execute(
            f"""
            SELECT p.fpl_id, p.player_code, p.fpl_status,
                   {chance_expression} AS official_chance,
                   s.can_select, s.removed, p.news, p.news_added
            FROM player_snapshot AS p
            JOIN player_status_snapshot AS s
              ON s.ingestion_run_id = p.ingestion_run_id AND s.fpl_id = p.fpl_id
            WHERE p.ingestion_run_id = ?
            ORDER BY p.fpl_id
            """,
            [source_run_id],
        ).fetchall()
        overrides = {
            row[1]: ReviewedAvailabilityOverride(*row)
            for row in connection.execute(
                """
                SELECT override_id, player_code, target_gameweek, observed_at,
                       source, rationale, availability_probability, is_eligible,
                       effective_until
                FROM availability_override
                WHERE target_gameweek = ?
                  AND observed_at <= ?
                  AND (effective_until IS NULL OR effective_until >= ?)
                QUALIFY row_number() OVER (
                    PARTITION BY player_code ORDER BY observed_at DESC, override_id DESC
                ) = 1
                """,
                [target_gameweek, as_of, as_of],
            ).fetchall()
        }

        resolutions = []
        for row in players:
            player = AvailabilityInput(
                fpl_id=row[0],
                player_code=row[1],
                fpl_status=row[2],
                official_chance=row[3],
                chance_horizon_available=chance_column is not None,
                can_select=row[4],
                removed=row[5],
                news=row[6] or "",
                news_added=row[7],
            )
            resolutions.append(
                resolve_availability(
                    player,
                    as_of=as_of,
                    target_gameweek=target_gameweek,
                    override=overrides.get(player.player_code),
                )
            )

        unresolved = sum(
            item.availability_probability is None for item in resolutions
        )
        status = "completed_with_gaps" if unresolved else "completed"
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """
                INSERT INTO availability_resolution_run (
                    resolution_run_id, source_ingestion_run_id, target_gameweek,
                    as_of, deadline, policy_version, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    resolution_run_id,
                    source_run_id,
                    target_gameweek,
                    as_of,
                    deadline,
                    POLICY_VERSION,
                    status,
                ],
            )
            connection.executemany(
                """
                INSERT INTO player_availability_resolution VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    (
                        resolution_run_id,
                        item.fpl_id,
                        item.player_code,
                        item.fpl_status,
                        item.official_chance,
                        item.availability_probability,
                        item.is_eligible,
                        item.selected_source,
                        item.selected_override_id,
                        item.reason,
                        json.dumps(item.data_quality_flags),
                    )
                    for item in resolutions
                ],
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    return AvailabilityRunResult(
        resolution_run_id=resolution_run_id,
        source_ingestion_run_id=source_run_id,
        target_gameweek=target_gameweek,
        as_of=as_of,
        deadline=deadline,
        players=len(resolutions),
        resolved_players=len(resolutions) - unresolved,
        unresolved_players=unresolved,
        blocked_players=sum(item.is_eligible is False for item in resolutions),
        status=status,
    )
