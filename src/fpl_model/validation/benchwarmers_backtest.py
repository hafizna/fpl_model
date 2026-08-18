"""End-to-end, deadline-safe walk-forward backtest of the replicated Benchwarmers model.

For each evaluation gameweek this module reconstructs team strength (Vaastav-
only, ``team_strength_asof.py``), player rate windows (``player_rates_asof.py``),
and appearance projections (``appearance_asof.py``) using strictly earlier
gameweeks, then wires them through the same component functions
``baseline_pipeline.py`` uses for the GW1 workbook baseline --
``project_benchwarmers_*``/``weight_*`` and ``compose_baseline_projection`` --
to score every eligible single-fixture player-fixture. It never persists to
DuckDB; results are returned as in-memory dataclasses for
``validation/backtest.py``'s existing fold/metric primitives to consume.

Double/blank-gameweek player-fixtures are excluded from evaluation entirely, as
a counted gap, to avoid a DGW-aggregation sub-project. Missing or unusable
history at deadline time is also a counted, flagged gap -- never a fabricated
default value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import duckdb
import pandas as pd

from fpl_model.model.attacking import (
    AttackingWindow,
    project_benchwarmers_attacking_rates,
    weight_attacking_rates,
)
from fpl_model.model.baseline import BaselineComponentProjections, compose_baseline_projection
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
from fpl_model.validation.appearance_asof import appearance_as_of
from fpl_model.validation.backtest import BacktestObservation
from fpl_model.validation.historical import infer_gameweek_deadlines
from fpl_model.validation.player_rates_asof import (
    has_usable_rate_history,
    league_average_bonus_rates_as_of,
    player_rates_as_of,
)
from fpl_model.validation.team_fixture_results import (
    build_team_fixture_results,
    build_team_name_to_id,
)
from fpl_model.validation.team_strength_asof import team_strength_as_of

POLICY_VERSION = "benchwarmers_replica_walk_forward_backtest_v1"
TEAM_STRENGTH_POLICY_VERSION = "vaastav_expanding_team_strength_v1"
REGULATION_MINUTES = 90.0
SPARSE_APPEARANCE_THRESHOLD = 5

MISSING_APPEARANCE_HISTORY = "MISSING_APPEARANCE_HISTORY"
NO_USABLE_PLAYER_RATE_HISTORY = "NO_USABLE_PLAYER_RATE_HISTORY"
MISSING_TEAM_STRENGTH = "MISSING_TEAM_STRENGTH"
MISSING_TEAM_ID = "MISSING_TEAM_ID"
DOUBLE_GAMEWEEK_FIXTURE = "DOUBLE_GAMEWEEK_FIXTURE"
NO_LEAGUE_AVERAGE_BONUS_RATES = "NO_LEAGUE_AVERAGE_BONUS_RATES"
NEGATIVE_BONUS_SIGNAL = "NEGATIVE_BONUS_SIGNAL"


@dataclass(frozen=True, slots=True)
class BacktestGapSummary:
    """One counted, categorised reason a player-fixture was excluded from a GW."""

    gameweek: int
    fixture_id: int | None
    player_code: int
    team: str
    position: str
    flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScoredObservationDiagnostics:
    """Per-observation context discarded by ``BacktestObservation``'s minimal shape.

    Built alongside (never instead of) each ``BacktestObservation``, from
    values the scoring loop already computes -- no new component calls, no
    new rate/appearance/team-strength queries. Exists purely to let a
    read-only diagnostic script segment the backtest's own scored rows by
    position, expected minutes, start probability, predicted xPts, and each
    of the 11 component contributions, without touching
    ``validation/backtest.py``'s shared, minimal observation contract (also
    used by the unrelated naive-mean smoke test).

    ``component_*`` fields are ``baseline.components``'s raw, *pre-home-away*
    values (matching ``ScoringComponents``); they sum to ``pre_home_away_xpts``,
    not to ``predicted_xpts`` -- ``predicted_xpts`` already has the fixture's
    1.05/0.95 home/away multiplier applied once by ``compose_baseline_projection``.
    """

    player_code: int
    fixture_id: int
    gameweek: int
    position: str
    start_probability: float
    expected_minutes: float
    predicted_xpts: float
    actual_points: float
    component_appearance: float
    component_sixty_minutes: float
    component_saves: float
    component_yellow_cards: float
    component_red_cards: float
    component_bonus: float
    component_assists: float
    component_goals: float
    component_clean_sheet: float
    component_goals_conceded: float
    component_defcon: float


@dataclass(frozen=True, slots=True)
class BenchwarmersBacktestResult:
    observations: tuple[BacktestObservation, ...]
    diagnostics: tuple[ScoredObservationDiagnostics, ...]
    gaps: tuple[BacktestGapSummary, ...]
    evaluated_gameweeks: tuple[int, ...]
    candidate_player_fixture_rows: int
    scored_player_fixture_rows: int


def _fixture_row_columns(gameweeks_frame: pd.DataFrame) -> None:
    required = {
        "element",
        "code",
        "position",
        "team",
        "GW",
        "fixture",
        "kickoff_time",
        "was_home",
        "opponent_team",
        "total_points",
    }
    missing = required - set(gameweeks_frame.columns)
    if missing:
        raise ValueError(f"gameweeks_frame missing columns: {', '.join(sorted(missing))}")


def materialize_benchwarmers_walk_forward_backtest(
    *,
    season: str,
    import_run_id: str,
    connection: duckdb.DuckDBPyConnection,
    gameweeks_frame: pd.DataFrame,
    players_raw_frame: pd.DataFrame,
    evaluation_from_gw: int = 3,
    evaluation_to_gw: int = 38,
    deadline_buffer: timedelta = timedelta(minutes=90),
    outcome_delay: timedelta = timedelta(hours=3),
    short_form_gameweeks: int = 6,
    defcon_short_form_gameweeks: int = 10,
    long_form_weight: float = 0.8,
) -> BenchwarmersBacktestResult:
    """Score every eligible single-fixture player-fixture for GW evaluation range.

    ``gameweeks_frame`` must be the raw ``merged_gw.csv``-shaped DataFrame
    (carrying ``code`` merged in from ``players_raw.csv``, see
    ``scripts/backtest_benchwarmers.py``); ``players_raw_frame`` supplies the
    ``id``/``team`` integer mapping needed to resolve each player's own team id
    for ``FixtureContext``. ``connection`` must already have ``import_run_id``'s
    rows materialised in ``player_fixture_history`` via
    ``import_player_fixture_history``. All predictive inputs for gameweek ``N``
    use only ``gameweek < N`` data; the row for gameweek ``N`` itself is read
    only for fixture identity, kickoff, and the realised ``total_points`` label.
    """
    if not season.strip():
        raise ValueError("season must not be blank")
    if not 1 <= evaluation_from_gw <= evaluation_to_gw <= 38:
        raise ValueError("evaluation_from_gw must be <= evaluation_to_gw, both in 1..38")
    _fixture_row_columns(gameweeks_frame)

    gameweeks_frame = gameweeks_frame.copy()
    gameweeks_frame["kickoff_time"] = pd.to_datetime(
        gameweeks_frame["kickoff_time"], utc=True, errors="coerce"
    )
    if gameweeks_frame["kickoff_time"].isna().any():
        raise ValueError("kickoff_time contains missing or invalid values")
    if gameweeks_frame["was_home"].dtype != bool:
        gameweeks_frame["was_home"] = (
            gameweeks_frame["was_home"].astype(str).str.strip().str.lower().map(
                {"true": True, "false": False}
            )
        )
        if gameweeks_frame["was_home"].isna().any():
            raise ValueError("was_home must be a boolean")

    # Mirror build_player_fixture_history's duplicate policy: drop exact
    # byte-for-byte duplicate rows, but reject conflicting (code, fixture)
    # duplicates rather than silently picking one.
    gameweeks_frame = gameweeks_frame.drop_duplicates().copy()
    if gameweeks_frame.duplicated(["code", "fixture"], keep=False).any():
        raise ValueError("gameweeks_frame contains conflicting player-fixture duplicates")

    team_fixture_results = build_team_fixture_results(gameweeks_frame)
    team_name_to_id = build_team_name_to_id(players_raw_frame, gameweeks_frame)
    deadlines = infer_gameweek_deadlines(gameweeks_frame, deadline_buffer=deadline_buffer)

    observations: list[BacktestObservation] = []
    diagnostics: list[ScoredObservationDiagnostics] = []
    gaps: list[BacktestGapSummary] = []
    candidate_rows = 0
    evaluated: list[int] = []

    for gameweek in range(evaluation_from_gw, evaluation_to_gw + 1):
        if gameweek not in deadlines:
            continue
        evaluated.append(gameweek)
        deadline = deadlines[gameweek].to_pydatetime()

        strengths = team_strength_as_of(
            team_fixture_results,
            as_of_gameweek=gameweek,
            short_form_gameweeks=short_form_gameweeks,
            long_form_weight=long_form_weight,
            target_deadline=deadline,
            outcome_delay=outcome_delay,
        )
        rates = player_rates_as_of(
            connection,
            import_run_id=import_run_id,
            as_of_gameweek=gameweek,
            short_form_gameweeks=short_form_gameweeks,
            defcon_short_form_gameweeks=defcon_short_form_gameweeks,
            target_deadline=deadline,
            outcome_delay=outcome_delay,
        )
        appearances = appearance_as_of(
            connection,
            import_run_id=import_run_id,
            as_of_gameweek=gameweek,
            target_deadline=deadline,
            outcome_delay=outcome_delay,
        )
        try:
            avg_bps, avg_bonus, avg_bonus_per_bps = league_average_bonus_rates_as_of(
                connection,
                import_run_id=import_run_id,
                as_of_gameweek=gameweek,
                target_deadline=deadline,
                outcome_delay=outcome_delay,
            )
        except ValueError:
            avg_bps = avg_bonus = avg_bonus_per_bps = None

        gw_rows = gameweeks_frame.loc[gameweeks_frame["GW"] == gameweek]
        fixtures_per_player = gw_rows.groupby("code")["fixture"].nunique()
        double_gameweek_players = set(
            fixtures_per_player.loc[fixtures_per_player > 1].index
        )

        for row in gw_rows.itertuples(index=False):
            player_code = int(row.code)
            team = str(row.team)
            fixture_id = int(row.fixture)
            position = str(row.position)
            candidate_rows += 1
            flags: set[str] = set()

            if player_code in double_gameweek_players:
                flags.add(DOUBLE_GAMEWEEK_FIXTURE)
                gaps.append(
                    BacktestGapSummary(
                        gameweek, fixture_id, player_code, team, position, tuple(sorted(flags))
                    )
                )
                continue

            appearance_entry = appearances.get(player_code)
            rate_entry = rates.get(player_code)
            own_strength = strengths.get(team)

            # The opponent is resolved by team name via team_fixture_results
            # (the other team sharing this fixture_id), not opponent_team
            # (Vaastav's own integer code, a different id space).
            opponent_row = team_fixture_results.loc[
                (team_fixture_results["fixture_id"] == fixture_id)
                & (team_fixture_results["team"] != team)
            ]
            opponent_strength = None
            opponent_team_id = team_name_to_id.get(team)
            if not opponent_row.empty:
                opponent_team_name = str(opponent_row.iloc[0]["team"])
                opponent_strength = strengths.get(opponent_team_name)
                opponent_team_id = team_name_to_id.get(opponent_team_name)

            own_team_id = team_name_to_id.get(team)

            if appearance_entry is None:
                flags.add(MISSING_APPEARANCE_HISTORY)
            if rate_entry is None or not has_usable_rate_history(rate_entry):
                flags.add(NO_USABLE_PLAYER_RATE_HISTORY)
            if own_strength is None or opponent_strength is None:
                flags.add(MISSING_TEAM_STRENGTH)
            if own_team_id is None or opponent_team_id is None:
                flags.add(MISSING_TEAM_ID)
            if avg_bps is None:
                flags.add(NO_LEAGUE_AVERAGE_BONUS_RATES)

            if flags:
                gaps.append(
                    BacktestGapSummary(
                        gameweek, fixture_id, player_code, team, position, tuple(sorted(flags))
                    )
                )
                continue

            is_home = bool(row.was_home)
            fixture = FixtureContext(
                fixture_id=fixture_id,
                gameweek=gameweek,
                slot=1,
                team_id=own_team_id,
                opponent_id=opponent_team_id,
                is_home=is_home,
                kickoff=row.kickoff_time.to_pydatetime(),
            )

            appearance = appearance_entry.appearance
            rate = rate_entry
            minutes_per_start = appearance_entry.mean_minutes_per_start
            minutes_fraction = minutes_per_start / REGULATION_MINUTES
            cameo_ratio = appearance_entry.mean_minutes_per_substitute / minutes_per_start
            saves_per_90 = (
                rate.season_saves / rate.season_minutes * REGULATION_MINUTES
                if rate.season_minutes > 0
                else 0.0
            )
            yellow_rate = (
                rate.season_yellow_cards
                / rate.season_minutes
                * REGULATION_MINUTES
                * minutes_fraction
                if rate.season_minutes > 0
                else 0.0
            )
            red_rate = (
                rate.season_red_cards
                / rate.season_minutes
                * REGULATION_MINUTES
                * minutes_fraction
                if rate.season_minutes > 0
                else 0.0
            )
            bonus_per_start = (
                rate.season_bonus / rate.season_starts if rate.season_starts > 0 else avg_bonus
            )
            bps_per_start = (
                rate.season_bps / rate.season_starts if rate.season_starts > 0 else avg_bps
            )
            if bps_per_start < 0.0 or bonus_per_start < 0.0:
                # A genuinely poor small sample can average out to negative
                # BPS/start; project_benchwarmers_bonus requires non-negative
                # rates, so this is a counted gap rather than a crash or a
                # silent clamp to zero.
                gaps.append(
                    BacktestGapSummary(
                        gameweek,
                        fixture_id,
                        player_code,
                        team,
                        position,
                        (NEGATIVE_BONUS_SIGNAL,),
                    )
                )
                continue
            long_defcon_lambda = (
                rate.long_form_defensive_contribution
                / rate.long_form_defcon_minutes
                * REGULATION_MINUTES
                * minutes_fraction
                if rate.long_form_defcon_minutes > 0
                else 0.0
            )
            short_defcon_lambda = (
                rate.short_form_defensive_contribution
                / rate.short_form_defcon_minutes
                * REGULATION_MINUTES
                * minutes_fraction
                if rate.short_form_defcon_minutes > 0
                else 0.0
            )

            attacking = weight_attacking_rates(
                project_benchwarmers_attacking_rates(
                    AttackingWindow(
                        rate.long_form_minutes,
                        rate.long_form_expected_goals,
                        rate.long_form_expected_assists,
                    ),
                    AttackingWindow(
                        rate.short_form_minutes,
                        rate.short_form_expected_goals,
                        rate.short_form_expected_assists,
                    ),
                    minutes_per_start_fraction=minutes_fraction,
                    position=position,
                    opponent_defensive_multiplier=opponent_strength.strength.opponent_defensive_weakness_ratio,
                    long_form_weight=long_form_weight,
                ),
                appearance,
                substitute_to_start_minutes_ratio=cameo_ratio,
            )
            defensive = weight_defensive_rates(
                project_benchwarmers_defensive_rates(
                    corrected_team_xgc_per_match=own_strength.strength.opponent_xgc_per_match,
                    opponent_xg_per_match=opponent_strength.strength.opponent_xg_per_match,
                    league_average_xg_per_match=own_strength.strength.league_average_xg_per_match,
                    position=position,
                ),
                appearance,
                substitute_to_start_minutes_ratio=cameo_ratio,
                position=position,
            )
            saves = weight_saves(
                project_benchwarmers_saves(
                    saves_per_90=saves_per_90,
                    opponent_xg_per_match=opponent_strength.strength.opponent_xg_per_match,
                    league_average_xg_per_match=own_strength.strength.league_average_xg_per_match,
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
                previous_starts=rate.season_starts,
                previous_bonus_per_start=bonus_per_start,
                previous_bps_per_start=bps_per_start,
                current_starts=0,
                current_bonus_per_start=avg_bonus,
                current_bps_per_start=avg_bps,
                league_average_bonus_per_bps=avg_bonus_per_bps,
                defensive_fixture_multiplier=opponent_strength.strength.defensive_bonus_multiplier,
                attacking_fixture_multiplier=opponent_strength.strength.opponent_defensive_weakness_ratio,
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

            kickoff = row.kickoff_time.to_pydatetime()
            actual_points = float(row.total_points)
            observations.append(
                BacktestObservation(
                    season=season,
                    gameweek=gameweek,
                    deadline=deadline,
                    fixture_kickoff=kickoff,
                    feature_cutoff=deadline,
                    outcome_available_at=kickoff + outcome_delay,
                    player_id=player_code,
                    fixture_id=fixture_id,
                    predicted_xpts=baseline.total_xpts,
                    actual_points=actual_points,
                )
            )
            diagnostics.append(
                ScoredObservationDiagnostics(
                    player_code=player_code,
                    fixture_id=fixture_id,
                    gameweek=gameweek,
                    position=position,
                    start_probability=appearance.start_probability,
                    expected_minutes=appearance.expected_minutes,
                    predicted_xpts=baseline.total_xpts,
                    actual_points=actual_points,
                    component_appearance=baseline.components.appearance,
                    component_sixty_minutes=baseline.components.sixty_minutes,
                    component_saves=baseline.components.saves,
                    component_yellow_cards=baseline.components.yellow_cards,
                    component_red_cards=baseline.components.red_cards,
                    component_bonus=baseline.components.bonus,
                    component_assists=baseline.components.assists,
                    component_goals=baseline.components.goals,
                    component_clean_sheet=baseline.components.clean_sheet,
                    component_goals_conceded=baseline.components.goals_conceded,
                    component_defcon=baseline.components.defcon,
                )
            )

    return BenchwarmersBacktestResult(
        observations=tuple(observations),
        diagnostics=tuple(diagnostics),
        gaps=tuple(gaps),
        evaluated_gameweeks=tuple(evaluated),
        candidate_player_fixture_rows=candidate_rows,
        scored_player_fixture_rows=len(observations),
    )
