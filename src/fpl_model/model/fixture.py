"""Fixture identity, strength signals, and one-time home/away adjustment.

The live Benchwarmers workbook encodes venue through opponent-name casing and
materialises two rows for every player/gameweek. The Python model uses explicit
fixture records instead: a double gameweek has two records and a blank has none.

Component-specific opponent multipliers remain inputs to their respective rate
models. The global home/away scalar is applied once, after component exposure.
An isolated workbook diagnostic preserves the spreadsheet's BA/BG/BI formulas,
including its double application on the not-start branch.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from math import isfinite
from typing import Any

HOME_XPTS_MULTIPLIER = 1.05
AWAY_XPTS_MULTIPLIER = 0.95


def _validate_probability(value: float, name: str) -> None:
    if not isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be finite and between 0 and 1")


def _validate_non_negative(value: float, name: str) -> None:
    if not isfinite(value) or value < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")


def _parse_kickoff(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"invalid kickoff_time: {value}") from error
    else:
        raise ValueError("kickoff_time must be an ISO timestamp, datetime, or None")
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result


@dataclass(frozen=True, slots=True)
class FixtureContext:
    """One team's view of one scheduled FPL fixture."""

    fixture_id: int
    gameweek: int
    slot: int
    team_id: int
    opponent_id: int
    is_home: bool
    kickoff: datetime | None

    def __post_init__(self) -> None:
        for name in ("fixture_id", "team_id", "opponent_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(self.gameweek, bool) or not isinstance(self.gameweek, int):
            raise ValueError("gameweek must be an integer")
        if not 1 <= self.gameweek <= 38:
            raise ValueError("gameweek must be between 1 and 38")
        if isinstance(self.slot, bool) or not isinstance(self.slot, int) or self.slot < 1:
            raise ValueError("slot must be a positive integer")
        if not isinstance(self.is_home, bool):
            raise ValueError("is_home must be a boolean")


@dataclass(frozen=True, slots=True)
class FixtureStrength:
    """Opponent and league-average rates used by component-specific formulas."""

    opponent_xg_per_match: float
    opponent_xgc_per_match: float
    league_average_xg_per_match: float
    league_average_xgc_per_match: float

    def __post_init__(self) -> None:
        _validate_non_negative(self.opponent_xg_per_match, "opponent_xg_per_match")
        _validate_non_negative(self.opponent_xgc_per_match, "opponent_xgc_per_match")
        for name in (
            "league_average_xg_per_match",
            "league_average_xgc_per_match",
        ):
            value = getattr(self, name)
            if not isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")

    @property
    def opponent_attack_ratio(self) -> float:
        """Raw xG/league-average ratio used by saves and defensive lambda."""
        return self.opponent_xg_per_match / self.league_average_xg_per_match

    @property
    def opponent_defensive_weakness_ratio(self) -> float:
        """Raw xGC/league-average ratio used by goals, assists, and attacking bonus."""
        return self.opponent_xgc_per_match / self.league_average_xgc_per_match

    @property
    def workbook_defensive_bonus_multiplier(self) -> float:
        """Return the workbook's inverted ``1 + (1 - attack_ratio)`` signal."""
        return 2.0 - self.opponent_attack_ratio

    @property
    def defensive_bonus_multiplier(self) -> float:
        """Return the inverted workbook signal with negative xPts prevented."""
        return max(0.0, self.workbook_defensive_bonus_multiplier)


@dataclass(frozen=True, slots=True)
class ScoringComponents:
    """The eleven auditable workbook component columns for one fixture."""

    appearance: float = 0.0
    sixty_minutes: float = 0.0
    saves: float = 0.0
    yellow_cards: float = 0.0
    red_cards: float = 0.0
    bonus: float = 0.0
    assists: float = 0.0
    goals: float = 0.0
    clean_sheet: float = 0.0
    goals_conceded: float = 0.0
    defcon: float = 0.0

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if not isfinite(value):
                raise ValueError(f"{item.name} must be finite")

    @property
    def total_xpts(self) -> float:
        return float(sum(getattr(self, item.name) for item in fields(self)))

    @property
    def total_excluding_appearance_xpts(self) -> float:
        return self.total_xpts - self.appearance

    @property
    def cameo_rate_xpts(self) -> float:
        """Columns included in the workbook's not-start branch."""
        return float(
            self.yellow_cards
            + self.red_cards
            + self.bonus
            + self.assists
            + self.goals
            + self.goals_conceded
        )


@dataclass(frozen=True, slots=True)
class HomeAwayProjection:
    """An unconditional component total with home/away applied exactly once."""

    pre_home_away_xpts: float
    home_away_multiplier: float
    total_xpts: float


@dataclass(frozen=True, slots=True)
class WorkbookFixtureProjection:
    """Exact BA/BG/BI diagnostics plus the single-application comparison."""

    has_fixture: bool
    pre_home_away_xpts: float
    home_away_multiplier: float
    if_start_total_xpts: float
    if_not_start_total_xpts: float
    workbook_true_total_xpts: float
    single_application_true_total_xpts: float
    double_application_delta: float


def fixture_contexts_for_team(
    fixtures: Iterable[Mapping[str, Any]],
    *,
    team_id: int,
) -> tuple[FixtureContext, ...]:
    """Convert raw FPL fixtures to explicit team fixtures and numbered GW slots.

    Fixtures without an assigned ``event`` are omitted until the FPL API assigns
    them to a gameweek. No synthetic blank slot is created. More than two
    fixtures are retained rather than inheriting the workbook's two-slot cap.
    """
    if isinstance(team_id, bool) or not isinstance(team_id, int) or team_id <= 0:
        raise ValueError("team_id must be a positive integer")

    selected: list[tuple[int, int, int, int, bool, datetime | None]] = []
    seen_fixture_ids: set[int] = set()
    for raw in fixtures:
        home_team = raw.get("team_h")
        away_team = raw.get("team_a")
        if team_id not in {home_team, away_team}:
            continue
        if home_team == away_team:
            raise ValueError("fixture home and away teams must differ")
        gameweek = raw.get("event")
        if gameweek is None:
            continue
        fixture_id = raw.get("id")
        opponent_id = away_team if team_id == home_team else home_team
        for name, value in (
            ("id", fixture_id),
            ("event", gameweek),
            ("opponent_id", opponent_id),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"fixture {name} must be an integer")
        if fixture_id in seen_fixture_ids:
            raise ValueError(f"duplicate fixture id for team: {fixture_id}")
        seen_fixture_ids.add(fixture_id)
        selected.append(
            (
                gameweek,
                fixture_id,
                team_id,
                opponent_id,
                team_id == home_team,
                _parse_kickoff(raw.get("kickoff_time")),
            )
        )

    selected.sort(
        key=lambda item: (
            item[0],
            item[5].timestamp() if item[5] is not None else float("inf"),
            item[1],
        )
    )
    slots_by_gameweek: dict[int, int] = {}
    contexts: list[FixtureContext] = []
    for gameweek, fixture_id, own_team, opponent, is_home, kickoff in selected:
        slot = slots_by_gameweek.get(gameweek, 0) + 1
        slots_by_gameweek[gameweek] = slot
        contexts.append(
            FixtureContext(
                fixture_id=fixture_id,
                gameweek=gameweek,
                slot=slot,
                team_id=own_team,
                opponent_id=opponent,
                is_home=is_home,
                kickoff=kickoff,
            )
        )
    return tuple(contexts)


def home_away_multiplier(is_home: bool) -> float:
    """Return the extracted global home/away scalar."""
    if not isinstance(is_home, bool):
        raise ValueError("is_home must be a boolean")
    return HOME_XPTS_MULTIPLIER if is_home else AWAY_XPTS_MULTIPLIER


def apply_home_away_once(
    pre_home_away_xpts: float,
    *,
    is_home: bool,
) -> HomeAwayProjection:
    """Apply the workbook's global scalar once to an unconditional xPts total."""
    if not isfinite(pre_home_away_xpts):
        raise ValueError("pre_home_away_xpts must be finite")
    multiplier = home_away_multiplier(is_home)
    return HomeAwayProjection(
        pre_home_away_xpts=float(pre_home_away_xpts),
        home_away_multiplier=multiplier,
        total_xpts=float(pre_home_away_xpts * multiplier),
    )


def project_workbook_fixture_totals(
    components: ScoringComponents,
    *,
    is_home: bool | None,
    t1_appearance_xpts: float,
    start_probability: float,
    substitute_to_start_minutes_ratio: float,
) -> WorkbookFixtureProjection:
    """Reproduce BA/BG/BI and expose a corrected single-H/A comparison.

    ``is_home=None`` represents the workbook's ``VS='-'`` blank slot. BA and BI
    are zero, while BG is intentionally still calculated with the workbook's
    otherwise-falling-through away scalar.
    """
    if not isfinite(t1_appearance_xpts):
        raise ValueError("t1_appearance_xpts must be finite")
    _validate_probability(start_probability, "start_probability")
    _validate_probability(
        substitute_to_start_minutes_ratio,
        "substitute_to_start_minutes_ratio",
    )
    if is_home is not None and not isinstance(is_home, bool):
        raise ValueError("is_home must be a boolean or None")

    has_fixture = is_home is not None
    multiplier = home_away_multiplier(is_home) if has_fixture else AWAY_XPTS_MULTIPLIER
    pre_home_away = components.total_xpts
    if_start_total = pre_home_away * multiplier if has_fixture else 0.0
    cameo_before_home_away = (
        components.cameo_rate_xpts * substitute_to_start_minutes_ratio
    )
    if_not_start_total = cameo_before_home_away * multiplier
    pre_outer_total = (
        t1_appearance_xpts
        + components.total_excluding_appearance_xpts * start_probability
        + if_not_start_total * (1.0 - start_probability)
    )
    single_application_pre_outer_total = (
        t1_appearance_xpts
        + components.total_excluding_appearance_xpts * start_probability
        + cameo_before_home_away * (1.0 - start_probability)
    )
    workbook_true_total = pre_outer_total * multiplier if has_fixture else 0.0
    single_application_true_total = (
        single_application_pre_outer_total * multiplier if has_fixture else 0.0
    )
    return WorkbookFixtureProjection(
        has_fixture=has_fixture,
        pre_home_away_xpts=float(pre_home_away),
        home_away_multiplier=multiplier,
        if_start_total_xpts=float(if_start_total),
        if_not_start_total_xpts=float(if_not_start_total),
        workbook_true_total_xpts=float(workbook_true_total),
        single_application_true_total_xpts=float(single_application_true_total),
        double_application_delta=float(
            workbook_true_total - single_application_true_total
        ),
    )


def aggregate_fixture_xpts_by_gameweek(
    projections: Iterable[tuple[FixtureContext, float]],
) -> dict[int, float]:
    """Sum independently projected fixture rows, including DGW slots."""
    totals: dict[int, float] = {}
    for context, xpts in projections:
        if not isfinite(xpts):
            raise ValueError("fixture xPts must be finite")
        totals[context.gameweek] = totals.get(context.gameweek, 0.0) + xpts
    return dict(sorted(totals.items()))
