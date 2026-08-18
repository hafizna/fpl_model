"""Materialise the explainable preseason baseline for current player-fixtures."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb

from fpl_model.model.appearance import (
    DEFAULT_START_MINUTES,
    DEFAULT_SUBSTITUTE_MINUTES,
    AppearanceProjection,
)
from fpl_model.model.attacking import (
    AttackingWindow,
    project_benchwarmers_attacking_rates,
    weight_attacking_rates,
)
from fpl_model.model.baseline import (
    BaselineComponentProjections,
    compose_baseline_projection,
)
from fpl_model.model.defence import (
    project_benchwarmers_defensive_rates,
    weight_defensive_rates,
)
from fpl_model.model.fixture import FixtureContext
from fpl_model.model.secondary import (
    project_benchwarmers_bonus,
    project_benchwarmers_defcon,
    project_benchwarmers_saves,
    project_discipline,
    weight_defcon,
    weight_linear_component,
    weight_saves,
)
from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database

POLICY_VERSION = "coherent_benchwarmers_preseason_baseline_v4"
REGULATION_MINUTES = 90.0
AVERAGE_BPS_PER_START = 14.862318964622457
AVERAGE_BONUS_PER_START = 0.29431670795024134
AVERAGE_BONUS_PER_BPS = 0.019802879257995912
SPARSE_APPEARANCE_THRESHOLD = 5


@dataclass(frozen=True, slots=True)
class BaselineRunResult:
    model_run_id: str
    appearance_projection_run_id: str
    player_rate_run_id: str
    team_strength_run_id: str
    current_players: int
    candidate_fixture_rows: int
    projected_fixture_rows: int
    gap_players: int
    status: str


@dataclass(frozen=True, slots=True)
class _MinutesInputs:
    minutes_per_start: float
    minutes_per_substitute: float
    sample_appearances: int | None
    used_default_start_minutes: bool


@dataclass(frozen=True, slots=True)
class _TeamStrength:
    corrected_xgc: float
    blended_xg: float
    blended_xgc: float
    league_xg: float
    league_xgc: float
    defensive_bonus_multiplier: float
    defensive_weakness_ratio: float
    is_promoted_prior: bool


def _choose_input_runs(
    connection: duckdb.DuckDBPyConnection,
    *,
    target_gameweek: int,
    appearance_projection_run_id: str | None,
    player_rate_run_id: str | None,
    team_strength_run_id: str | None,
) -> tuple[str, str, str, str, str, datetime, datetime]:
    strength_parameters: list[object] = [target_gameweek]
    strength_filter = ""
    if team_strength_run_id is not None:
        strength_filter = "AND strength_run_id = ?"
        strength_parameters.append(team_strength_run_id)
    strength = connection.execute(
        f"""
        SELECT strength_run_id, source_ingestion_run_id
        FROM team_strength_run
        WHERE target_gameweek = ? AND status = 'completed' {strength_filter}
        ORDER BY created_at DESC, strength_run_id DESC LIMIT 1
        """,
        strength_parameters,
    ).fetchone()
    if strength is None:
        raise ValueError("no completed team-strength run found")
    selected_strength_run, source_ingestion_run = strength

    appearance_parameters: list[object] = [
        target_gameweek,
        source_ingestion_run,
    ]
    appearance_filter = ""
    if appearance_projection_run_id is not None:
        appearance_filter = "AND apr.projection_run_id = ?"
        appearance_parameters.append(appearance_projection_run_id)
    appearance = connection.execute(
        f"""
        SELECT apr.projection_run_id, apr.appearance_history_import_run_id,
               ar.as_of, ar.deadline
        FROM appearance_projection_run AS apr
        JOIN availability_resolution_run AS ar
          ON ar.resolution_run_id = apr.availability_resolution_run_id
        WHERE apr.target_gameweek = ?
          AND ar.source_ingestion_run_id = ?
          {appearance_filter}
        ORDER BY apr.created_at DESC, apr.projection_run_id DESC LIMIT 1
        """,
        appearance_parameters,
    ).fetchone()
    if appearance is None:
        raise ValueError("no compatible appearance projection run found")
    selected_appearance_run, history_import_run, as_of, deadline = appearance

    rate_parameters: list[object] = []
    rate_filter = ""
    if player_rate_run_id is not None:
        rate_filter = "WHERE rate_run_id = ?"
        rate_parameters.append(player_rate_run_id)
    rate = connection.execute(
        f"""
        SELECT rate_run_id FROM player_rate_history_run
        {rate_filter}
        ORDER BY created_at DESC, rate_run_id DESC LIMIT 1
        """,
        rate_parameters,
    ).fetchone()
    if rate is None:
        raise ValueError("no player-rate history run found")
    return (
        selected_appearance_run,
        history_import_run,
        rate[0],
        selected_strength_run,
        source_ingestion_run,
        as_of,
        deadline,
    )


def _appearance_projection(row: tuple[object, ...]) -> AppearanceProjection | None:
    values = row[1:9]
    if any(value is None for value in values):
        return None
    return AppearanceProjection(*map(float, values))


def _minutes_inputs(
    *,
    player_code: int,
    flags: set[str],
    history: dict[int, tuple[int, int, float, float]],
    overrides: dict[str, tuple[float, float]],
) -> _MinutesInputs | None:
    override_prefix = "APPEARANCE_SCENARIO_OVERRIDE_ID="
    override_ids = [
        flag.removeprefix(override_prefix)
        for flag in flags
        if flag.startswith(override_prefix)
    ]
    if override_ids:
        override = overrides.get(override_ids[0])
        if override is None:
            return None
        return _MinutesInputs(
            *override,
            sample_appearances=None,
            used_default_start_minutes=False,
        )
    player_history = history.get(player_code)
    if player_history is None:
        return None
    starts, substitutes, minutes_per_start, minutes_per_substitute = player_history
    return _MinutesInputs(
        minutes_per_start or DEFAULT_START_MINUTES,
        minutes_per_substitute or DEFAULT_SUBSTITUTE_MINUTES,
        starts + substitutes,
        minutes_per_start == 0.0,
    )


def _fixture_contexts(
    rows: list[tuple[object, ...]],
) -> dict[int, tuple[FixtureContext, ...]]:
    contexts: dict[int, list[FixtureContext]] = {}
    slots: dict[tuple[int, int], int] = {}
    for fixture_id, gameweek, kickoff, home_team, away_team in rows:
        for team_id, opponent_id, is_home in (
            (home_team, away_team, True),
            (away_team, home_team, False),
        ):
            key = (team_id, gameweek)
            slot = slots.get(key, 0) + 1
            slots[key] = slot
            contexts.setdefault(team_id, []).append(
                FixtureContext(
                    fixture_id=fixture_id,
                    gameweek=gameweek,
                    slot=slot,
                    team_id=team_id,
                    opponent_id=opponent_id,
                    is_home=is_home,
                    kickoff=kickoff,
                )
            )
    return {team_id: tuple(items) for team_id, items in contexts.items()}


def _component_rows(model_run_id: str, player_code: int, fixture_id: int, result):
    components = result.components
    return [
        (model_run_id, player_code, fixture_id, name, value)
        for name, value in (
            ("appearance", components.appearance),
            ("sixty_minutes", components.sixty_minutes),
            ("goals", components.goals),
            ("assists", components.assists),
            ("clean_sheets", components.clean_sheet),
            ("goals_conceded", components.goals_conceded),
            ("saves", components.saves),
            ("yellow_cards", components.yellow_cards),
            ("red_cards", components.red_cards),
            ("bonus", components.bonus),
            ("defcon", components.defcon),
        )
    ]


def materialize_preseason_baseline(
    *,
    target_gameweek: int = 1,
    appearance_projection_run_id: str | None = None,
    player_rate_run_id: str | None = None,
    team_strength_run_id: str | None = None,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> BaselineRunResult:
    """Compose all implemented Benchwarmers components for GW1 fixtures."""
    if target_gameweek != 1:
        raise ValueError("preseason baseline materialisation currently supports GW1 only")
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        (
            appearance_run,
            history_import_run,
            rate_run,
            strength_run,
            source_ingestion_run,
            as_of,
            deadline,
        ) = _choose_input_runs(
            connection,
            target_gameweek=target_gameweek,
            appearance_projection_run_id=appearance_projection_run_id,
            player_rate_run_id=player_rate_run_id,
            team_strength_run_id=team_strength_run_id,
        )
        identity = (
            f"{appearance_run}|{rate_run}|{strength_run}|{POLICY_VERSION}"
        ).encode()
        model_run_id = f"baseline_{hashlib.sha256(identity).hexdigest()[:16]}"
        existing = connection.execute(
            """
            SELECT current_players, candidate_fixture_rows,
                   projected_fixture_rows, gap_players, status
            FROM baseline_projection_run WHERE model_run_id = ?
            """,
            [model_run_id],
        ).fetchone()
        if existing is not None:
            return BaselineRunResult(
                model_run_id,
                appearance_run,
                rate_run,
                strength_run,
                *map(int, existing[:4]),
                existing[4],
            )

        players = connection.execute(
            """
            SELECT fpl_id, player_code, team_id, fpl_position
            FROM player_snapshot WHERE ingestion_run_id = ? ORDER BY fpl_id
            """,
            [source_ingestion_run],
        ).fetchall()
        appearance_rows = {
            row[0]: row[1:]
            for row in connection.execute(
                """
                SELECT fpl_id, player_code, start_probability,
                       substitute_appearance_probability, appearance_probability,
                       sixty_minute_probability, expected_minutes,
                       appearance_xpts, sixty_minute_xpts, total_xpts,
                       data_quality_flags
                FROM player_appearance_projection WHERE projection_run_id = ?
                """,
                [appearance_run],
            ).fetchall()
        }
        histories = {
            row[0]: row[1:]
            for row in connection.execute(
                """
                SELECT player_code, starts, substitute_appearances,
                       minutes_per_start, minutes_per_substitute
                FROM player_appearance_history WHERE import_run_id = ?
                """,
                [history_import_run],
            ).fetchall()
        }
        overrides = {
            row[0]: row[1:]
            for row in connection.execute(
                """
                SELECT override_id, minutes_per_start, minutes_per_substitute
                FROM appearance_scenario_override WHERE target_gameweek = ?
                """,
                [target_gameweek],
            ).fetchall()
        }
        rates = {
            row[0]: row[1:]
            for row in connection.execute(
                """
                SELECT player_code, position, season_minutes, season_starts,
                       season_saves, season_yellow_cards, season_red_cards,
                       season_bonus, season_bps, long_form_minutes,
                       long_form_expected_goals, long_form_expected_assists,
                       short_form_minutes, short_form_expected_goals,
                       short_form_expected_assists, long_form_defcon_minutes,
                       long_form_defensive_contribution,
                       short_form_defcon_minutes,
                       short_form_defensive_contribution, data_quality_flags
                FROM player_rate_history WHERE rate_run_id = ?
                """,
                [rate_run],
            ).fetchall()
        }
        strengths = {
            row[0]: _TeamStrength(*row[1:])
            for row in connection.execute(
                """
                SELECT team_id, corrected_xgc_per_match,
                       blended_xg_per_match, blended_xgc_per_match,
                       league_average_xg_per_match,
                       league_average_xgc_per_match,
                       defensive_bonus_multiplier, defensive_weakness_ratio,
                       is_promoted_prior
                FROM team_strength_projection WHERE strength_run_id = ?
                """,
                [strength_run],
            ).fetchall()
        }
        fixture_rows = connection.execute(
            """
            SELECT fixture_id, gameweek, kickoff_time, home_team_id, away_team_id
            FROM fixture_snapshot
            WHERE ingestion_run_id = ? AND gameweek = ?
            ORDER BY kickoff_time, fixture_id
            """,
            [source_ingestion_run, target_gameweek],
        ).fetchall()
        fixtures = _fixture_contexts(fixture_rows)

        projection_rows = []
        component_rows = []
        gap_rows = []
        candidate_fixture_rows = 0
        for fpl_id, player_code, team_id, position in players:
            player_fixtures = fixtures.get(team_id, ())
            candidate_fixture_rows += len(player_fixtures)
            flags: set[str] = set()
            raw_appearance = appearance_rows.get(fpl_id)
            appearance = None
            minutes = None
            rate = rates.get(player_code)
            own_strength = strengths.get(team_id)
            if player_code is None:
                flags.add("MISSING_PLAYER_CODE")
            if raw_appearance is None:
                flags.add("MISSING_APPEARANCE_PROJECTION")
            else:
                flags.update(json.loads(raw_appearance[-1]))
                appearance = _appearance_projection(raw_appearance)
                if appearance is None:
                    flags.add("MISSING_APPEARANCE_PROJECTION")
                elif player_code is not None:
                    minutes = _minutes_inputs(
                        player_code=player_code,
                        flags=flags,
                        history=histories,
                        overrides=overrides,
                    )
                    if minutes is None:
                        flags.add("MISSING_MINUTES_SCENARIO")
            if rate is None:
                flags.add("NO_PREVIOUS_PL_PLAYER_RATE_HISTORY")
            if own_strength is None:
                flags.add("MISSING_TEAM_STRENGTH")
            elif own_strength.is_promoted_prior:
                flags.add("OWN_TEAM_PROMOTED_PRIOR")
            if not player_fixtures:
                flags.add("NO_GAMEWEEK_FIXTURE")
            if (
                appearance is None
                or minutes is None
                or rate is None
                or own_strength is None
                or not player_fixtures
            ):
                gap_rows.append(
                    (
                        model_run_id,
                        fpl_id,
                        player_code,
                        team_id,
                        json.dumps(sorted(flags)),
                    )
                )
                continue

            (
                prior_position,
                season_minutes,
                season_starts,
                season_saves,
                season_yellow_cards,
                season_red_cards,
                season_bonus,
                season_bps,
                long_minutes,
                long_xg,
                long_xa,
                short_minutes,
                short_xg,
                short_xa,
                long_defcon_minutes,
                long_defcon,
                short_defcon_minutes,
                short_defcon,
                rate_flags,
            ) = rate
            flags.update(json.loads(rate_flags))
            if prior_position != position:
                flags.add(f"POSITION_CHANGED_{prior_position}_TO_{position}")
            if (
                minutes.sample_appearances is not None
                and minutes.sample_appearances < SPARSE_APPEARANCE_THRESHOLD
            ):
                flags.add("SPARSE_APPEARANCE_HISTORY")
            if minutes.used_default_start_minutes:
                flags.add("DEFAULT_START_MINUTES_PRIOR")
            if minutes.minutes_per_substitute > minutes.minutes_per_start:
                flags.add("CAMEO_MINUTES_EXCEED_START_MINUTES")
            minutes_fraction = minutes.minutes_per_start / REGULATION_MINUTES
            cameo_ratio = (
                minutes.minutes_per_substitute / minutes.minutes_per_start
                if minutes.minutes_per_start > 0.0
                else 0.0
            )
            saves_per_90 = (
                season_saves / season_minutes * REGULATION_MINUTES
                if season_minutes > 0
                else 0.0
            )
            yellow_rate = (
                season_yellow_cards
                / season_minutes
                * REGULATION_MINUTES
                * minutes_fraction
                if season_minutes > 0
                else 0.0
            )
            red_rate = (
                season_red_cards
                / season_minutes
                * REGULATION_MINUTES
                * minutes_fraction
                if season_minutes > 0
                else 0.0
            )
            bonus_per_start = (
                season_bonus / season_starts
                if season_starts > 0
                else AVERAGE_BONUS_PER_START
            )
            bps_per_start = (
                season_bps / season_starts
                if season_starts > 0
                else AVERAGE_BPS_PER_START
            )
            long_defcon_lambda = (
                long_defcon
                / long_defcon_minutes
                * REGULATION_MINUTES
                * minutes_fraction
                if long_defcon_minutes > 0
                else 0.0
            )
            short_defcon_lambda = (
                short_defcon
                / short_defcon_minutes
                * REGULATION_MINUTES
                * minutes_fraction
                if short_defcon_minutes > 0
                else 0.0
            )

            for fixture in player_fixtures:
                opponent_strength = strengths[fixture.opponent_id]
                fixture_flags = set(flags)
                if opponent_strength.is_promoted_prior:
                    fixture_flags.add("OPPONENT_PROMOTED_PRIOR")
                attacking = weight_attacking_rates(
                    project_benchwarmers_attacking_rates(
                        AttackingWindow(long_minutes, long_xg, long_xa),
                        AttackingWindow(short_minutes, short_xg, short_xa),
                        minutes_per_start_fraction=minutes_fraction,
                        position=position,
                        opponent_defensive_multiplier=(
                            opponent_strength.defensive_weakness_ratio
                        ),
                    ),
                    appearance,
                    substitute_to_start_minutes_ratio=cameo_ratio,
                )
                defensive = weight_defensive_rates(
                    project_benchwarmers_defensive_rates(
                        corrected_team_xgc_per_match=own_strength.corrected_xgc,
                        opponent_xg_per_match=opponent_strength.blended_xg,
                        league_average_xg_per_match=own_strength.league_xg,
                        position=position,
                    ),
                    appearance,
                    substitute_to_start_minutes_ratio=cameo_ratio,
                    position=position,
                )
                saves = weight_saves(
                    project_benchwarmers_saves(
                        saves_per_90=saves_per_90,
                        opponent_xg_per_match=opponent_strength.blended_xg,
                        league_average_xg_per_match=own_strength.league_xg,
                        position=position,
                    ),
                    appearance,
                    substitute_to_start_minutes_ratio=cameo_ratio,
                )
                discipline = project_discipline(
                    yellow_card_rate_if_start=yellow_rate,
                    red_card_rate_if_start=red_rate,
                )
                yellow_cards = weight_linear_component(
                    discipline.yellow_card_xpts_if_start,
                    appearance,
                    substitute_to_start_minutes_ratio=cameo_ratio,
                )
                red_cards = weight_linear_component(
                    discipline.red_card_xpts_if_start,
                    appearance,
                    substitute_to_start_minutes_ratio=cameo_ratio,
                )
                bonus_rate = project_benchwarmers_bonus(
                    previous_starts=season_starts,
                    previous_bonus_per_start=bonus_per_start,
                    previous_bps_per_start=bps_per_start,
                    current_starts=0,
                    current_bonus_per_start=AVERAGE_BONUS_PER_START,
                    current_bps_per_start=AVERAGE_BPS_PER_START,
                    league_average_bonus_per_bps=AVERAGE_BONUS_PER_BPS,
                    defensive_fixture_multiplier=(
                        opponent_strength.defensive_bonus_multiplier
                    ),
                    attacking_fixture_multiplier=(
                        opponent_strength.defensive_weakness_ratio
                    ),
                    position=position,
                )
                bonus = weight_linear_component(
                    bonus_rate.bounded_xpts_if_start,
                    appearance,
                    substitute_to_start_minutes_ratio=cameo_ratio,
                )
                defcon = weight_defcon(
                    project_benchwarmers_defcon(
                        long_form_lambda_if_start=long_defcon_lambda,
                        short_form_lambda_if_start=short_defcon_lambda,
                        position=position,
                    ),
                    appearance,
                    substitute_to_start_minutes_ratio=cameo_ratio,
                    position=position,
                )
                baseline = compose_baseline_projection(
                    fixture,
                    BaselineComponentProjections(
                        appearance=appearance,
                        attacking=attacking,
                        defensive=defensive,
                        saves=saves,
                        yellow_cards=yellow_cards,
                        red_cards=red_cards,
                        bonus=bonus,
                        defcon=defcon,
                    ),
                )
                projection_rows.append(
                    (
                        model_run_id,
                        player_code,
                        fixture.fixture_id,
                        team_id,
                        fixture.opponent_id,
                        fixture.is_home,
                        appearance.start_probability,
                        appearance.substitute_appearance_probability,
                        appearance.expected_minutes,
                        baseline.total_xpts,
                        0.0,
                        baseline.total_xpts,
                        None,
                        json.dumps(sorted(fixture_flags)),
                    )
                )
                component_rows.extend(
                    _component_rows(
                        model_run_id,
                        player_code,
                        fixture.fixture_id,
                        baseline,
                    )
                )

        status = "completed_with_gaps" if gap_rows else "completed"
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """
                INSERT INTO model_run (
                    model_run_id, target_gameweek, as_of, deadline,
                    model_version, source_ingestion_run_id, status, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'completed', current_timestamp)
                """,
                [
                    model_run_id,
                    target_gameweek,
                    as_of,
                    deadline,
                    POLICY_VERSION,
                    source_ingestion_run,
                ],
            )
            connection.execute(
                """
                INSERT INTO baseline_projection_run VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    model_run_id,
                    appearance_run,
                    rate_run,
                    strength_run,
                    POLICY_VERSION,
                    len(players),
                    candidate_fixture_rows,
                    len(projection_rows),
                    len(gap_rows),
                    status,
                ],
            )
            if projection_rows:
                connection.executemany(
                    "INSERT INTO player_fixture_projection VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    projection_rows,
                )
                connection.executemany(
                    "INSERT INTO projection_component VALUES (?, ?, ?, ?, ?)",
                    component_rows,
                )
            if gap_rows:
                connection.executemany(
                    "INSERT INTO baseline_projection_gap VALUES (?, ?, ?, ?, ?)",
                    gap_rows,
                )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    return BaselineRunResult(
        model_run_id,
        appearance_run,
        rate_run,
        strength_run,
        len(players),
        candidate_fixture_rows,
        len(projection_rows),
        len(gap_rows),
        status,
    )
