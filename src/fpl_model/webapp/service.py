"""Read-only web boundary over the existing deterministic decision engine.

The web app deliberately does not duplicate lineup or transfer scoring in
JavaScript. Browser requests are resolved against one explicit, frozen set of
model runs and then passed to the same Python rules used by the CLI tools.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from math import sqrt
from pathlib import Path
from typing import Any

import duckdb

from fpl_model.decision.autosub import compute_expected_autosub_value
from fpl_model.decision.lineup import PlayerGameweekProjection, recommend_lineup
from fpl_model.decision.lineup_store import combine_appearance_probability
from fpl_model.decision.role_scenario_sensitivity import evaluate_role_scenario_sensitivity
from fpl_model.decision.squad import CHIP_NAMES, SquadPlayer, validate_squad
from fpl_model.ingest.squad_snapshot import validate_entry_picks_payload
from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.role_state import RoleStateResult


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


def load_horizon_catalog(
    connection: duckdb.DuckDBPyConnection,
    horizon: ResearchHorizon,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[int, PlayerGameweekProjection]]]:
    """Load one explicit horizon into application-facing player/projection maps."""
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


def load_release_catalog(
    release_path: str | Path,
) -> tuple[
    ResearchHorizon,
    dict[int, dict[str, Any]],
    dict[int, dict[int, PlayerGameweekProjection]],
    str,
    str | None,
]:
    """Load the compact, validated release used by stateless deployments."""

    payload = json.loads(Path(release_path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "fpl_web_release_v1":
        raise ValueError("unsupported or missing web release schema_version")
    release = payload.get("release")
    players = payload.get("players")
    if not isinstance(release, dict) or not isinstance(players, list):
        raise ValueError("web release must contain release metadata and players")
    stored_digest = release.get("content_sha256")
    release_id = release.get("release_id")
    if stored_digest is not None:
        unsigned_release = dict(release)
        unsigned_release.pop("content_sha256", None)
        unsigned_release.pop("release_id", None)
        unsigned_payload = {
            "schema_version": payload["schema_version"],
            "release": unsigned_release,
            "players": players,
        }
        calculated_digest = hashlib.sha256(
            json.dumps(
                unsigned_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if calculated_digest != str(stored_digest):
            raise ValueError("web release content_sha256 does not match its payload")
        expected_id = f"web_release_{calculated_digest[:16]}"
        if release_id != expected_id:
            raise ValueError("web release release_id does not match its payload")
    model_runs = tuple(
        (int(row["gameweek"]), str(row["model_run_id"]))
        for row in release.get("model_runs", [])
    )
    if len(model_runs) != 3 or tuple(row[0] for row in model_runs) != tuple(
        range(model_runs[0][0], model_runs[0][0] + 3)
    ):
        raise ValueError("web release must contain three consecutive Gameweeks")
    horizon = ResearchHorizon(
        source_ingestion_run_id=str(release["source_ingestion_run_id"]),
        model_version=str(release["model_version"]),
        planning_as_of=str(release["planning_as_of"]),
        model_runs=model_runs,
    )
    catalog: dict[int, dict[str, Any]] = {}
    projections: dict[int, dict[int, PlayerGameweekProjection]] = {
        gameweek: {} for gameweek, _ in model_runs
    }
    for player in players:
        row = dict(player)
        fpl_id = int(row["fpl_id"])
        if fpl_id in catalog:
            raise ValueError(f"duplicate player in web release: {fpl_id}")
        gameweek_rows = row.get("gameweeks")
        if not isinstance(gameweek_rows, dict):
            raise ValueError(f"player {fpl_id} has no Gameweek projections")
        for gameweek, _ in model_runs:
            projection_row = gameweek_rows.get(str(gameweek))
            if not isinstance(projection_row, dict):
                raise ValueError(f"player {fpl_id} lacks GW{gameweek} projection")
            projections[gameweek][fpl_id] = PlayerGameweekProjection(
                fpl_id=fpl_id,
                expected_points=float(projection_row["xpts"]),
                uncertainty=(
                    None
                    if projection_row.get("uncertainty") is None
                    else float(projection_row["uncertainty"])
                ),
                data_quality_flags=tuple(
                    str(flag) for flag in projection_row.get("quality_flags", [])
                ),
                appearance_probability=float(projection_row["appearance_probability"]),
            )
        catalog[fpl_id] = row
    if not catalog:
        raise ValueError("web release contains no players")
    return (
        horizon,
        catalog,
        projections,
        str(release.get("health", "research")),
        None if release_id is None else str(release_id),
    )


def _load_web_inputs(
    *,
    database_path: str | Path,
    release_path: str | Path | None,
) -> tuple[
    ResearchHorizon,
    dict[int, dict[str, Any]],
    dict[int, dict[int, PlayerGameweekProjection]],
    str,
    str | None,
]:
    if release_path is not None:
        return load_release_catalog(release_path)
    with duckdb.connect(str(database_path), read_only=True) as connection:
        horizon = resolve_research_horizon(connection)
        catalog, projections = load_horizon_catalog(connection, horizon)
    return horizon, catalog, projections, "research", None


def load_web_bootstrap(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    *,
    release_path: str | Path | None = None,
) -> dict[str, Any]:
    horizon, catalog, _, health, release_id = _load_web_inputs(
        database_path=database_path,
        release_path=release_path,
    )
    return {
        "release": {
            "health": health,
            "label": "RESEARCH_ONLY" if health == "research" else health.upper(),
            "release_id": release_id,
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


def resolve_entry_picks(
    picks_payload: Mapping[str, Any],
    *,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    release_path: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve one FPL public entry-picks payload against the bootstrap catalog.

    Read-only and server-side-storage-free, matching this app's own
    "browser local storage only, no server-side private writes" boundary
    (`docs/WEB_APP.md`): unlike `ingest.squad_snapshot.import_squad_snapshot_from_entry`
    (the CLI/persistence path, which writes an immutable `squad_snapshot`
    row), this resolves ``picks_payload`` against the ALREADY-LOADED
    bootstrap catalog (``load_web_bootstrap``'s own ``players`` list, which
    already carries ``price_tenths``) and returns exactly the shape the
    frontend's existing squad-state contract expects
    (``fpl_ids``/``bank_tenths``/``selling_prices``) -- no second database or
    release read.

    FPL's public picks payload has no per-player purchase/selling price;
    ``selling_prices`` here is always the CURRENT market price from the
    catalog, and ``selling_price_is_estimated`` is always ``True`` -- the
    caller must surface that caveat rather than imply an FPL-exact sell
    value, exactly the same caveat `import_squad_snapshot_from_entry` makes.
    """
    private_rows = validate_entry_picks_payload(picks_payload)
    entry_history = picks_payload.get("entry_history")
    if not isinstance(entry_history, Mapping) or "bank" not in entry_history:
        raise ValueError("picks_payload is missing entry_history.bank")
    # FPL's payload carries `bank` as an integer number of tenths of a
    # million, matching this app's own `bank_tenths` unit directly -- no
    # conversion needed here (contrast `import_squad_snapshot_from_entry`,
    # which must convert down to the CSV-import path's whole-millions
    # contract before re-multiplying by ten internally).
    bank_tenths = int(entry_history["bank"])

    _, catalog, _, _, _ = _load_web_inputs(
        database_path=database_path,
        release_path=release_path,
    )
    fpl_ids = [int(value) for value in private_rows["fpl_id"]]
    missing = sorted(set(fpl_ids) - set(catalog))
    if missing:
        raise ValueError(f"squad players lack complete horizon projections: {missing}")

    selling_prices = {fpl_id: catalog[fpl_id]["price_tenths"] for fpl_id in fpl_ids}
    captain_row = private_rows.loc[private_rows["is_captain"]].iloc[0]
    vice_captain_row = private_rows.loc[private_rows["is_vice_captain"]].iloc[0]
    return {
        "fpl_ids": fpl_ids,
        "bank_tenths": bank_tenths,
        "selling_prices": selling_prices,
        "captain_fpl_id": int(captain_row["fpl_id"]),
        "vice_captain_fpl_id": int(vice_captain_row["fpl_id"]),
        "selling_price_is_estimated": True,
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


def _role_state_by_id_for_gameweek(
    catalog: dict[int, dict[str, Any]], fpl_ids: set[int], gameweek: int
) -> dict[int, RoleStateResult]:
    """Read back the role_state release_export.py already bakes into
    catalog[fpl_id]["gameweeks"][str(gameweek)]["role_state"] -- no second
    database/release read, matching resolve_entry_picks's own boundary. A
    player missing a role_state entry (an older release built before this
    field existed) is simply absent from the result rather than raising, so
    evaluate_role_scenario_sensitivity treats them as not rotation-risk
    instead of failing the whole lineup request over a diagnostic gap.
    """
    result: dict[int, RoleStateResult] = {}
    for fpl_id in fpl_ids:
        row = catalog.get(fpl_id, {}).get("gameweeks", {}).get(str(gameweek), {}).get("role_state")
        if row is None:
            continue
        result[fpl_id] = RoleStateResult(role_state=row["role_state"], reason=row["reason"])
    return result


def _lineup_payload(
    squad,
    projection_by_id,
    gameweek: int,
    *,
    catalog: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    squad_ids = {player.fpl_id for player in squad.players}
    projection_by_id = {
        fpl_id: projection
        for fpl_id, projection in projection_by_id.items()
        if fpl_id in squad_ids
    }
    recommendation = recommend_lineup(squad, projection_by_id.values())
    autosub = compute_expected_autosub_value(recommendation, projection_by_id)
    sensitivity_report = None
    if catalog is not None:
        role_state_by_id = _role_state_by_id_for_gameweek(catalog, squad_ids, gameweek)
        if role_state_by_id:
            sensitivity = evaluate_role_scenario_sensitivity(
                squad,
                tuple(projection_by_id.values()),
                role_state_by_id=role_state_by_id,
                base_recommendation=recommendation,
            )
            sensitivity_report = sensitivity.report
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
        "role_scenario_sensitivity": sensitivity_report,
    }


def recommend_web_lineups(
    fpl_ids: tuple[int, ...],
    *,
    bank_tenths: int = 0,
    free_transfers: int = 1,
    selling_prices: dict[int, int] | None = None,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    release_path: str | Path | None = None,
) -> dict[str, Any]:
    horizon, catalog, projections, health, release_id = _load_web_inputs(
        database_path=database_path,
        release_path=release_path,
    )
    squad = _validated_web_squad(
        catalog,
        fpl_ids,
        bank_tenths=bank_tenths,
        free_transfers=free_transfers,
        selling_prices={} if selling_prices is None else selling_prices,
    )
    lineups = [
        _lineup_payload(squad, projections[gameweek], gameweek, catalog=catalog)
        for gameweek, _ in horizon.model_runs
    ]
    return {
        "health": health,
        "release_id": release_id,
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
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    release_path: str | Path | None = None,
) -> dict[str, Any]:
    selling_prices = {} if selling_prices is None else selling_prices
    horizon, catalog, projections, health, release_id = _load_web_inputs(
        database_path=database_path,
        release_path=release_path,
    )
    baseline_squad = _validated_web_squad(
        catalog,
        fpl_ids,
        bank_tenths=bank_tenths,
        free_transfers=free_transfers,
        selling_prices=selling_prices,
    )
    baseline_lineups = [
        # role_scenario_sensitivity only for the baseline (current, pre-
        # transfer) squad -- computing it for every candidate transfer too
        # would multiply the cost of an already-expensive brute-force scan
        # (recommend_lineup already re-runs once per rotation-risk player).
        _lineup_payload(baseline_squad, projections[gameweek], gameweek, catalog=catalog)
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
        "health": health,
        "release_id": release_id,
        "baseline_cumulative_xpts": baseline_xpts,
        "baseline_lineups": baseline_lineups,
        "recommendation": "hold"
        if not suggestions or suggestions[0]["net_xpts_gain"] <= 0
        else "transfer",
        "suggestions": suggestions[:top_n],
        "method_note": (
            "Every legal affordable same-position single transfer is rescored over the frozen "
            "three-Gameweek horizon. Future transfer value and price changes are excluded."
        ),
    }
