"""Reviewed start/cameo scenario overrides for uncertain player roles."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from fpl_model.model.appearance import ConditionalAppearanceScenario
from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database


def _aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ReviewedAppearanceScenarioOverride:
    override_id: str
    player_code: int
    target_gameweek: int
    observed_at: datetime
    scenario: ConditionalAppearanceScenario
    source: str
    rationale: str
    effective_until: datetime | None = None

    def __post_init__(self) -> None:
        if not self.override_id.strip() or not self.source.strip() or not self.rationale.strip():
            raise ValueError("override ID, source, and rationale must not be blank")
        if self.player_code <= 0 or self.target_gameweek <= 0:
            raise ValueError("player_code and target_gameweek must be positive")
        _aware(self.observed_at, "observed_at")
        if self.effective_until is not None:
            _aware(self.effective_until, "effective_until")
            if self.effective_until < self.observed_at:
                raise ValueError("effective_until cannot precede observed_at")


@dataclass(frozen=True, slots=True)
class AppearanceScenarioOverrideStoreResult:
    override_id: str
    availability_as_of: datetime
    deadline: datetime
    requires_pipeline_refresh: bool


def create_appearance_scenario_override(
    *,
    player_code: int,
    target_gameweek: int,
    observed_at: datetime,
    scenario: ConditionalAppearanceScenario,
    source: str,
    rationale: str,
    effective_until: datetime | None = None,
) -> ReviewedAppearanceScenarioOverride:
    _aware(observed_at, "observed_at")
    if effective_until is not None:
        _aware(effective_until, "effective_until")
    identity = {
        "player_code": player_code,
        "target_gameweek": target_gameweek,
        "observed_at": observed_at.astimezone(UTC).isoformat(),
        "scenario": {
            "start": scenario.start_probability_if_available,
            "substitute": scenario.substitute_probability_if_available,
            "sixty_given_start": scenario.sixty_probability_given_start,
            "minutes_start": scenario.minutes_per_start,
            "minutes_substitute": scenario.minutes_per_substitute,
        },
        "source": source,
        "rationale": rationale,
        "effective_until": (
            effective_until.astimezone(UTC).isoformat()
            if effective_until is not None
            else None
        ),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ReviewedAppearanceScenarioOverride(
        override_id=f"appearance_scenario_{digest[:16]}",
        player_code=player_code,
        target_gameweek=target_gameweek,
        observed_at=observed_at,
        scenario=scenario,
        source=source,
        rationale=rationale,
        effective_until=effective_until,
    )


def store_appearance_scenario_override(
    override: ReviewedAppearanceScenarioOverride,
    *,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> AppearanceScenarioOverrideStoreResult:
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        state = connection.execute(
            """
            SELECT rr.as_of, rr.deadline
            FROM availability_resolution_run AS rr
            JOIN player_availability_resolution AS p
              ON p.resolution_run_id = rr.resolution_run_id
            WHERE rr.target_gameweek = ? AND p.player_code = ?
            ORDER BY rr.as_of DESC, rr.created_at DESC
            LIMIT 1
            """,
            [override.target_gameweek, override.player_code],
        ).fetchone()
        if state is None:
            raise ValueError("player/gameweek has no availability resolution")
        availability_as_of, deadline = state
        if override.observed_at > deadline:
            raise ValueError("override observed after the target deadline")
        projection_exists = connection.execute(
            """
            SELECT count(*) FROM appearance_projection_run
            WHERE target_gameweek = ?
            """,
            [override.target_gameweek],
        ).fetchone()[0]
        scenario = override.scenario
        connection.execute(
            """
            INSERT INTO appearance_scenario_override VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            ) ON CONFLICT DO NOTHING
            """,
            [
                override.override_id,
                override.player_code,
                override.target_gameweek,
                override.observed_at,
                override.effective_until,
                scenario.start_probability_if_available,
                scenario.substitute_probability_if_available,
                scenario.sixty_probability_given_start,
                scenario.minutes_per_start,
                scenario.minutes_per_substitute,
                override.source,
                override.rationale,
            ],
        )
    return AppearanceScenarioOverrideStoreResult(
        override_id=override.override_id,
        availability_as_of=availability_as_of,
        deadline=deadline,
        requires_pipeline_refresh=(
            override.observed_at > availability_as_of or bool(projection_exists)
        ),
    )
