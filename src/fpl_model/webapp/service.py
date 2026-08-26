"""Read-only web boundary over the existing deterministic decision engine.

The web app deliberately does not duplicate lineup or transfer scoring in
JavaScript. Browser requests are resolved against one explicit, frozen set of
model runs and then passed to the same Python rules used by the CLI tools.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from math import sqrt
from pathlib import Path
from typing import Any

import duckdb

from fpl_model.decision.autosub import compute_expected_autosub_value
from fpl_model.decision.lineup import PlayerGameweekProjection, recommend_lineup
from fpl_model.decision.lineup_store import combine_appearance_probability
from fpl_model.decision.squad import CHIP_NAMES, SquadPlayer, validate_squad
from fpl_model.storage import DEFAULT_DATABASE_PATH


@dataclass(frozen=True, slots=True)
class ResearchHorizon:
    source_ingestion_run_id: str
    model_version: str
    planning_as_of: str
    model_runs: tuple[tuple[int, str], ...]

    @property
    def start_gameweek(self) -> int:
        return self.model_runs[0][0]

    @property
    def end_gameweek(self) -> int:
        return self.model_runs[-1][0]


def _flags(value: str | None) -> set[str]:
    if value is None or not value.strip():
        return set()
    if value.lstrip().startswith("["):
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError("projection data_quality_flags must be a JSON list")
        return {str(flag).strip() for flag in parsed if str(flag).strip()}
    return {flag.strip() for flag in value.split("|") if flag.strip()}


def resolve_research_horizon(connection: duckdb.DuckDBPyConnection) -> ResearchHorizon:
    """Return the newest compatible completed three-Gameweek horizon.

    The app never silently combines unrelated runs. All three must share the
    same official snapshot, model version, and causal ``as_of`` timestamp.
    """

    rows = connection.execute(
        """
        SELECT target_gameweek, model_run_id, source_ingestion_run_id,
               model_version, as_of, completed_at
        FROM model_run
        WHERE status = 'completed'
        ORDER BY as_of DESC, target_gameweek ASC, completed_at DESC
        """
    ).fetchall()
    groups: dict[tuple[str, str, str], dict[int, tuple[str, object]]] = {}
    for gameweek, run_id, source_id, version, as_of, completed_at in rows:
        key = (str(source_id), str(version), as_of.isoformat())
        groups.setdefault(key, {}).setdefault(int(gameweek), (str(run_id), completed_at))

    candidates: list[tuple[object, int, tuple[str, str, str], tuple[tuple[int, str], ...]]] = []
    for key, by_gameweek in groups.items():
        for start in sorted(by_gameweek):
            gameweeks = (start, start + 1, start + 2)
            if not all(gameweek in by_gameweek for gameweek in gameweeks):
                continue
            run_ids = tuple((gameweek, by_gameweek[gameweek][0]) for gameweek in gameweeks)
            completed_at = max(by_gameweek[gameweek][1] for gameweek in gameweeks)
            candidates.append((completed_at, start, key, run_ids))
    if not candidates:
        raise ValueError("no compatible completed three-Gameweek model horizon exists")

    _, _, key, model_runs = max(candidates, key=lambda row: (row[0], row[1]))
    source_id, version, planning_as_of = key
    return ResearchHorizon(
        source_ingestion_run_id=source_id,
        model_version=version,
        planning_as_of=planning_as_of,
        model_runs=model_runs,
    )


def _catalog_rows(
    connection: duckdb.DuckDBPyConnection,
    horizon: ResearchHorizon,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[int, PlayerGameweekProjection]]]:
    run_by_gameweek = dict(horizon.model_runs)
    run_ids = tuple(run_by_gameweek.values())
    placeholders = ",".join("?" for _ in run_ids)
    rows = connection.execute(
        f"""
        SELECT mr.target_gameweek, ps.fpl_id, ps.player_code, ps.web_name,
               ps.team_id, COALESCE(ts.short_name, CAST(ps.team_id AS VARCHAR)),
               ps.fpl_position, ps.price, ps.fpl_status,
               p.final_xpts, p.uncertainty, p.data_quality_flags,
               p.start_probability, p.substitute_appearance_probability
        FROM player_fixture_projection AS p
        JOIN model_run AS mr USING (model_run_id)
        JOIN player_snapshot AS ps
          ON ps.ingestion_run_id = mr.source_ingestion_run_id
         AND ps.player_code = p.player_code
        LEFT JOIN team_snapshot AS ts
          ON ts.ingestion_run_id = ps.ingestion_run_id
         AND ts.team_id = ps.team_id
        WHERE p.model_run_id IN ({placeholders})
        ORDER BY ps.fpl_id, mr.target_gameweek, p.fixture_id
        """,
        list(run_ids),
    ).fetchall()

    catalog: dict[int, dict[str, Any]] = {}
    fixture_rows: dict[tuple[int, int], list[tuple[float, float | None, str | None, float, float]]] = {}
    for row in rows:
        (
            gameweek,
            fpl_id,
            player_code,
            name,
            team_id,
            team,
            position,
            price,
            status,
            xpts,
            uncertainty,
            flags,
            start_probability,
            substitute_probability,
        ) = row
        fpl_id = int(fpl_id)
        catalog.setdefault(
            fpl_id,
            {
                "fpl_id": fpl_id,
                "player_code": None if player_code is None else int(player_code),
                "name": str(name),
                "team_id": int(team_id),
                "team": str(team),
                "position": str(position),
                "price_tenths": int(Decimal(str(price)) * 10),
                "status": str(status),
                "gameweeks": {},
            },
        )
        fixture_rows.setdefault((int(gameweek), fpl_id), []).append(
            (
                float(xpts),
                None if uncertainty is None else float(uncertainty),
                None if flags is None else str(flags),
                float(start_probability),
                float(substitute_probability),
            )
        )

    projections: dict[int, dict[int, PlayerGameweekProjection]] = {
        gameweek: {} for gameweek in run_by_gameweek
    }
    for (gameweek, fpl_id), player_fixture_rows in fixture_rows.items():
        uncertainty_values = [row[1] for row in player_fixture_rows]
        combined_uncertainty = (
            None
            if any(value is None for value in uncertainty_values)
            else sqrt(sum(value**2 for value in uncertainty_values if value is not None))
        )
        projection = PlayerGameweekProjection(
            fpl_id=fpl_id,
            expected_points=sum(row[0] for row in player_fixture_rows),
            uncertainty=combined_uncertainty,
            data_quality_flags=tuple(
                sorted({flag for row in player_fixture_rows for flag in _flags(row[2])})
            ),
            appearance_probability=combine_appearance_probability(player_fixture_rows),
        )
        projections[gameweek][fpl_id] = projection
        catalog[fpl_id]["gameweeks"][str(gameweek)] = {
            "xpts": projection.expected_points,
            "appearance_probability": projection.appearance_probability,
        }
    return catalog, projections


def load_web_bootstrap(
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> dict[str, Any]:
    with duckdb.connect(str(database_path), read_only=True) as connection:
        horizon = resolve_research_horizon(connection)
        catalog, _ = _catalog_rows(connection, horizon)
    return {
        "release": {
            "health": "research",
            "label": "RESEARCH",
            "source_ingestion_run_id": horizon.source_ingestion_run_id,
            "model_version": horizon.model_version,
            "planning_as_of": horizon.planning_as_of,
            "model_runs": [
                {"gameweek": gameweek, "model_run_id": run_id}
                for gameweek, run_id in horizon.model_runs
            ],
        },
        "players": sorted(
            catalog.values(),
            key=lambda row: (row["position"], -row["price_tenths"], row["name"]),
        ),
    }


def _validated_web_squad(
    catalog: dict[int, dict[str, Any]],
    fpl_ids: tuple[int, ...],
    *,
    bank_tenths: int,
    free_transfers: int,
    selling_prices: dict[int, int],
):
    if len(fpl_ids) != 15 or len(set(fpl_ids)) != 15:
        raise ValueError("fpl_ids must contain 15 unique players")
    missing = sorted(set(fpl_ids) - set(catalog))
    if missing:
        raise ValueError(f"players lack complete horizon projections: {missing}")

    selected = [catalog[fpl_id] for fpl_id in fpl_ids]
    by_position = {
        position: [row for row in selected if row["position"] == position]
        for position in ("GK", "DEF", "MID", "FWD")
    }
    seed_starters = (
        by_position["GK"][:1]
        + by_position["DEF"][:3]
        + by_position["MID"][:4]
        + by_position["FWD"][:3]
    )
    starter_ids = {row["fpl_id"] for row in seed_starters}
    ordered = seed_starters + [row for row in selected if row["fpl_id"] not in starter_ids]
    captain_id = seed_starters[0]["fpl_id"]
    vice_id = seed_starters[1]["fpl_id"]
    players = tuple(
        SquadPlayer(
            fpl_id=row["fpl_id"],
            player_code=row["player_code"],
            player_name=row["name"],
            team_id=row["team_id"],
            position=row["position"],
            current_price_tenths=row["price_tenths"],
            purchase_price_tenths=row["price_tenths"],
            selling_price_tenths=selling_prices.get(row["fpl_id"], row["price_tenths"]),
            squad_position=index,
            is_captain=row["fpl_id"] == captain_id,
            is_vice_captain=row["fpl_id"] == vice_id,
        )
        for index, row in enumerate(ordered, start=1)
    )
    return validate_squad(
        players,
        bank_tenths=bank_tenths,
        free_transfers=free_transfers,
        unlimited_transfers=False,
        chip_period=1,
        chip_states=dict.fromkeys(CHIP_NAMES, "available"),
    )


def _player_payload(player: SquadPlayer, projection: PlayerGameweekProjection) -> dict[str, Any]:
    return {
        "fpl_id": player.fpl_id,
        "name": player.player_name,
        "team_id": player.team_id,
        "position": player.position,
        "price_tenths": player.current_price_tenths,
        "xpts": projection.expected_points,
        "appearance_probability": projection.appearance_probability,
    }


def _lineup_payload(squad, projection_by_id, gameweek: int) -> dict[str, Any]:
    squad_ids = {player.fpl_id for player in squad.players}
    projection_by_id = {
        fpl_id: projection
        for fpl_id, projection in projection_by_id.items()
        if fpl_id in squad_ids
    }
    recommendation = recommend_lineup(squad, projection_by_id.values())
    autosub = compute_expected_autosub_value(recommendation, projection_by_id)
    return {
        "gameweek": gameweek,
        "formation": recommendation.formation,
        "starting_xpts": recommendation.starting_xpts,
        "captain_bonus_xpts": recommendation.captain_bonus_xpts,
        "total_xpts": recommendation.total_xpts,
        "captain": _player_payload(
            recommendation.captain, projection_by_id[recommendation.captain.fpl_id]
        ),
        "vice_captain": _player_payload(
            recommendation.vice_captain,
            projection_by_id[recommendation.vice_captain.fpl_id],
        ),
        "starters": [
            _player_payload(player, projection_by_id[player.fpl_id])
            for player in recommendation.starters
        ],
        "bench": [
            _player_payload(recommendation.bench_goalkeeper, projection_by_id[recommendation.bench_goalkeeper.fpl_id]),
            *[
                _player_payload(player, projection_by_id[player.fpl_id])
                for player in recommendation.outfield_bench_order
            ],
        ],
        "expected_autosub_value": autosub.total_expected_bench_value,
        "quality_flags": list(recommendation.data_quality_flags),
    }


def recommend_web_lineups(
    fpl_ids: tuple[int, ...],
    *,
    bank_tenths: int = 0,
    free_transfers: int = 1,
    selling_prices: dict[int, int] | None = None,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> dict[str, Any]:
    with duckdb.connect(str(database_path), read_only=True) as connection:
        horizon = resolve_research_horizon(connection)
        catalog, projections = _catalog_rows(connection, horizon)
    squad = _validated_web_squad(
        catalog,
        fpl_ids,
        bank_tenths=bank_tenths,
        free_transfers=free_transfers,
        selling_prices={} if selling_prices is None else selling_prices,
    )
    lineups = [
        _lineup_payload(squad, projections[gameweek], gameweek)
        for gameweek, _ in horizon.model_runs
    ]
    return {
        "health": "research",
        "horizon": [gameweek for gameweek, _ in horizon.model_runs],
        "lineups": lineups,
        "cumulative_xpts": sum(row["total_xpts"] for row in lineups),
        "method_note": "Exhaustive legal XI and captain search over one frozen research horizon.",
    }


def recommend_web_transfers(
    fpl_ids: tuple[int, ...],
    *,
    bank_tenths: int = 0,
    free_transfers: int = 1,
    selling_prices: dict[int, int] | None = None,
    top_n: int = 8,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> dict[str, Any]:
    selling_prices = {} if selling_prices is None else selling_prices
    with duckdb.connect(str(database_path), read_only=True) as connection:
        horizon = resolve_research_horizon(connection)
        catalog, projections = _catalog_rows(connection, horizon)
    baseline_squad = _validated_web_squad(
        catalog,
        fpl_ids,
        bank_tenths=bank_tenths,
        free_transfers=free_transfers,
        selling_prices=selling_prices,
    )
    baseline_lineups = [
        _lineup_payload(baseline_squad, projections[gameweek], gameweek)
        for gameweek, _ in horizon.model_runs
    ]
    baseline_xpts = sum(row["total_xpts"] for row in baseline_lineups)
    owned = set(fpl_ids)
    suggestions: list[dict[str, Any]] = []
    for out_id in fpl_ids:
        outgoing = catalog[out_id]
        available = selling_prices.get(out_id, outgoing["price_tenths"]) + bank_tenths
        for incoming in catalog.values():
            in_id = incoming["fpl_id"]
            if (
                in_id in owned
                or incoming["position"] != outgoing["position"]
                or incoming["price_tenths"] > available
                or incoming["status"] not in {"a", "d"}
            ):
                continue
            candidate_ids = tuple(in_id if value == out_id else value for value in fpl_ids)
            try:
                candidate_squad = _validated_web_squad(
                    catalog,
                    candidate_ids,
                    bank_tenths=available - incoming["price_tenths"],
                    free_transfers=max(0, free_transfers - 1),
                    selling_prices=selling_prices,
                )
            except ValueError:
                continue
            candidate_lineups = [
                _lineup_payload(candidate_squad, projections[gameweek], gameweek)
                for gameweek, _ in horizon.model_runs
            ]
            candidate_xpts = sum(row["total_xpts"] for row in candidate_lineups)
            hit_cost = 0 if free_transfers >= 1 else 4
            suggestions.append(
                {
                    "out": outgoing,
                    "in": incoming,
                    "gross_xpts_gain": candidate_xpts - baseline_xpts,
                    "hit_cost": hit_cost,
                    "net_xpts_gain": candidate_xpts - baseline_xpts - hit_cost,
                    "remaining_bank_tenths": available - incoming["price_tenths"],
                    "lineups": candidate_lineups,
                }
            )
    suggestions.sort(
        key=lambda row: (-row["net_xpts_gain"], -row["gross_xpts_gain"], row["in"]["name"])
    )
    return {
        "health": "research",
        "baseline_cumulative_xpts": baseline_xpts,
        "recommendation": "hold"
        if not suggestions or suggestions[0]["net_xpts_gain"] <= 0
        else "transfer",
        "suggestions": suggestions[:top_n],
        "method_note": (
            "Every legal affordable same-position single transfer is rescored over the frozen "
            "three-Gameweek horizon. Future transfer value and price changes are excluded."
        ),
    }
