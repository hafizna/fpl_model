"""Read-only web boundary over the existing deterministic decision engine.

The web app deliberately does not duplicate lineup or transfer scoring in
JavaScript. Browser requests are resolved against one explicit, frozen set of
model runs and then passed to the same Python rules used by the CLI tools.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from math import sqrt
from pathlib import Path
from typing import Any

import duckdb

from fpl_model.decision.autosub import compute_expected_autosub_value
from fpl_model.decision.lineup import (
    PlayerGameweekProjection,
    is_legal_starting_xi,
    recommend_lineup,
)
from fpl_model.decision.lineup_store import combine_appearance_probability
from fpl_model.decision.role_scenario_sensitivity import evaluate_role_scenario_sensitivity
from fpl_model.decision.rolling import GameweekProjectionPool
from fpl_model.decision.squad import CHIP_NAMES, SquadPlayer, validate_squad
from fpl_model.decision.squad_rating import (
    RATING_FORMULA_VERSION,
    RATING_SCHEMA_VERSION,
    SquadBenchmark,
    build_squad_benchmark,
    rate_squad,
)
from fpl_model.decision.transfer import TransferTarget
from fpl_model.ingest.squad_snapshot import validate_entry_picks_payload
from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.role_state import RoleStateResult

_SQUAD_BENCHMARK_CACHE: dict[tuple[str, int], SquadBenchmark] = {}


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


@dataclass(frozen=True, slots=True)
class CurrentSquadSetup:
    """The manager's submitted picks for the first Gameweek in the horizon.

    This is not recommendation history. It is the public FPL picks snapshot
    loaded by Team ID, carried back with a lineup request so the decision
    service can score the manager's actual XI on the same projections as the
    recommendation.
    """

    gameweek: int
    starter_fpl_ids: tuple[int, ...]
    bench_fpl_ids: tuple[int, ...]
    captain_fpl_id: int
    vice_captain_fpl_id: int

    def __post_init__(self) -> None:
        if not 1 <= self.gameweek <= 38:
            raise ValueError("current setup gameweek must be between 1 and 38")
        if len(self.starter_fpl_ids) != 11 or len(set(self.starter_fpl_ids)) != 11:
            raise ValueError("current setup must contain 11 unique starters")
        if len(self.bench_fpl_ids) != 4 or len(set(self.bench_fpl_ids)) != 4:
            raise ValueError("current setup must contain 4 unique bench players")
        all_ids = (*self.starter_fpl_ids, *self.bench_fpl_ids)
        if len(set(all_ids)) != 15 or any(fpl_id <= 0 for fpl_id in all_ids):
            raise ValueError("current setup must contain 15 unique positive player IDs")
        if self.captain_fpl_id not in self.starter_fpl_ids:
            raise ValueError("current setup captain must be a starter")
        if self.vice_captain_fpl_id not in self.starter_fpl_ids:
            raise ValueError("current setup vice-captain must be a starter")
        if self.captain_fpl_id == self.vice_captain_fpl_id:
            raise ValueError("current setup captain and vice-captain must differ")


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
               p.start_probability, p.substitute_appearance_probability,
               p.opponent_team_id, p.is_home,
               COALESCE(ots.short_name, CAST(p.opponent_team_id AS VARCHAR))
        FROM player_fixture_projection AS p
        JOIN model_run AS mr USING (model_run_id)
        JOIN player_snapshot AS ps
          ON ps.ingestion_run_id = mr.source_ingestion_run_id
         AND ps.player_code = p.player_code
        LEFT JOIN team_snapshot AS ts
          ON ts.ingestion_run_id = ps.ingestion_run_id
         AND ts.team_id = ps.team_id
        LEFT JOIN team_snapshot AS ots
          ON ots.ingestion_run_id = ps.ingestion_run_id
         AND ots.team_id = p.opponent_team_id
        WHERE p.model_run_id IN ({placeholders})
        ORDER BY ps.fpl_id, mr.target_gameweek, p.fixture_id
        """,
        list(run_ids),
    ).fetchall()

    catalog: dict[int, dict[str, Any]] = {}
    # Kept at exactly the 5-element shape combine_appearance_probability's own
    # shared, positionally-unpacking contract requires (also used by
    # decision/lineup_store.py, rolling_store.py, transfer_store.py) --
    # opponent/fixture metadata is tracked in the separate opponent_rows dict
    # below rather than widening this tuple, which would break every other
    # caller of that shared function.
    fixture_rows: dict[
        tuple[int, int], list[tuple[float, float | None, str | None, float, float]]
    ] = {}
    opponent_rows: dict[tuple[int, int], list[dict[str, Any]]] = {}
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
            opponent_team_id,
            is_home,
            opponent_short,
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
        opponent_rows.setdefault((int(gameweek), fpl_id), []).append(
            {
                "opponent_team_id": int(opponent_team_id),
                "opponent": str(opponent_short),
                "is_home": bool(is_home),
            }
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
            "fixtures": opponent_rows[(gameweek, fpl_id)],
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


@dataclass(frozen=True, slots=True)
class RoleScenarioOverride:
    """One reviewed 'treat this player's this-Gameweek xPts as X' override.

    This is deliberately narrower than a full appearance-scenario override
    (`context/minutes.py`): the web app has no live re-projection pipeline to
    call (projections are baked into the release at export time, and
    re-running the model is DB-only and expensive), so a reviewed scenario
    here can only replace one already-projected number, not recompute it
    from a start/substitute/sixty-minute distribution. It is applied
    entirely in memory for one request; it never writes back to the release
    file or any database.
    """

    fpl_id: int
    gameweek: int
    xpts: float

    def __post_init__(self) -> None:
        if self.fpl_id <= 0:
            raise ValueError("fpl_id must be positive")
        if not 1 <= self.gameweek <= 38:
            raise ValueError("gameweek must be between 1 and 38")
        if self.xpts < 0.0:
            raise ValueError("xpts must be non-negative")


def apply_role_scenario_overrides(
    projections: dict[int, dict[int, PlayerGameweekProjection]],
    overrides: tuple[RoleScenarioOverride, ...],
    *,
    horizon: ResearchHorizon,
) -> dict[int, dict[int, PlayerGameweekProjection]]:
    """Return a NEW projections mapping with each override's xPts applied.

    Never mutates ``projections`` in place -- the base release/horizon a
    request loaded stays exactly as it was; only this one request's working
    copy changes. Every field of `PlayerGameweekProjection` other than
    `expected_points` is left untouched (uncertainty, appearance
    probability, and quality flags still describe the ORIGINAL projection,
    since this is a reviewed points override, not a new projection run).
    """
    if not overrides:
        return projections
    horizon_gameweeks = {gameweek for gameweek, _ in horizon.model_runs}
    result = {gameweek: dict(by_id) for gameweek, by_id in projections.items()}
    for override in overrides:
        if override.gameweek not in horizon_gameweeks:
            raise ValueError(
                f"override targets GW{override.gameweek}, outside this release's horizon "
                f"{sorted(horizon_gameweeks)}"
            )
        existing = result[override.gameweek].get(override.fpl_id)
        if existing is None:
            raise ValueError(
                f"fpl_id {override.fpl_id} has no GW{override.gameweek} projection to override"
            )
        result[override.gameweek][override.fpl_id] = replace(
            existing, expected_points=override.xpts
        )
    return result


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
    dict[str, Any] | None,
]:
    """Return ``(horizon, catalog, projections, health, release_id, release_metadata)``.

    ``release_metadata`` (``coverage``/``freshness``, see
    `release_export.build_web_release`) is only computed at export time into
    the compact release JSON -- it is ``None`` in database-connected mode,
    which is for local research/dev use and does not carry the same
    immutable-release freshness/coverage guarantee a shipped release does.
    """
    if release_path is not None:
        horizon, catalog, projections, health, release_id = load_release_catalog(release_path)
        payload = json.loads(Path(release_path).read_text(encoding="utf-8"))
        release_metadata = {
            key: payload["release"][key]
            for key in ("coverage", "freshness")
            if key in payload["release"]
        }
        return horizon, catalog, projections, health, release_id, release_metadata or None
    with duckdb.connect(str(database_path), read_only=True) as connection:
        horizon = resolve_research_horizon(connection)
        catalog, projections = load_horizon_catalog(connection, horizon)
    return horizon, catalog, projections, "research", None, None


def load_web_bootstrap(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    *,
    release_path: str | Path | None = None,
) -> dict[str, Any]:
    horizon, catalog, _, health, release_id, release_metadata = _load_web_inputs(
        database_path=database_path,
        release_path=release_path,
    )
    release_metadata = release_metadata or {}
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
            "coverage": release_metadata.get("coverage"),
            "freshness": release_metadata.get("freshness"),
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

    _, catalog, _, _, _, _ = _load_web_inputs(
        database_path=database_path,
        release_path=release_path,
    )
    private_rows = private_rows.sort_values("squad_position")
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
        "starter_fpl_ids": fpl_ids[:11],
        "bench_fpl_ids": fpl_ids[11:],
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


def _player_payload(
    player: SquadPlayer,
    projection: PlayerGameweekProjection,
    *,
    fixtures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "fpl_id": player.fpl_id,
        "name": player.player_name,
        "team_id": player.team_id,
        "position": player.position,
        "price_tenths": player.current_price_tenths,
        "xpts": projection.expected_points,
        "uncertainty": projection.uncertainty,
        "appearance_probability": projection.appearance_probability,
        "fixtures": fixtures or [],
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


def _fixtures_for_gameweek(
    catalog: dict[int, dict[str, Any]] | None, fpl_id: int, gameweek: int
) -> list[dict[str, Any]]:
    if catalog is None:
        return []
    return catalog.get(fpl_id, {}).get("gameweeks", {}).get(str(gameweek), {}).get(
        "fixtures", []
    )


def _lineup_payload(
    squad,
    projection_by_id,
    gameweek: int,
    *,
    catalog: dict[int, dict[str, Any]] | None = None,
    current_setup: CurrentSquadSetup | None = None,
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

    def _payload(player: SquadPlayer) -> dict[str, Any]:
        return _player_payload(
            player,
            projection_by_id[player.fpl_id],
            fixtures=_fixtures_for_gameweek(catalog, player.fpl_id, gameweek),
        )

    current_setup_comparison = None
    if current_setup is not None:
        player_by_id = {player.fpl_id: player for player in squad.players}
        current_starters = tuple(player_by_id[fpl_id] for fpl_id in current_setup.starter_fpl_ids)
        current_bench = tuple(player_by_id[fpl_id] for fpl_id in current_setup.bench_fpl_ids)
        if not is_legal_starting_xi(current_starters):
            raise ValueError("current setup does not contain a legal FPL starting XI")
        if current_bench[0].position != "GK" or any(
            player.position == "GK" for player in current_bench[1:]
        ):
            raise ValueError("current setup bench must place its goalkeeper first")

        current_starting_xpts = sum(
            projection_by_id[player.fpl_id].expected_points for player in current_starters
        )
        current_captain = player_by_id[current_setup.captain_fpl_id]
        current_vice = player_by_id[current_setup.vice_captain_fpl_id]
        current_captain_bonus = projection_by_id[current_captain.fpl_id].expected_points
        current_total_xpts = current_starting_xpts + current_captain_bonus
        recommended_starter_ids = {player.fpl_id for player in recommendation.starters}
        current_starter_ids = set(current_setup.starter_fpl_ids)
        recommended_bench = (
            recommendation.bench_goalkeeper,
            *recommendation.outfield_bench_order,
        )
        formation_counts = {
            position: sum(player.position == position for player in current_starters)
            for position in ("DEF", "MID", "FWD")
        }
        current_setup_comparison = {
            "basis": "loaded_fpl_picks",
            "current_formation": (
                f"{formation_counts['DEF']}-{formation_counts['MID']}-{formation_counts['FWD']}"
            ),
            "current_starting_xpts": current_starting_xpts,
            "current_captain_bonus_xpts": current_captain_bonus,
            "current_total_xpts": current_total_xpts,
            "recommended_total_xpts": recommendation.total_xpts,
            "marginal_xpts": recommendation.total_xpts - current_total_xpts,
            "starting_xpts_gain": recommendation.starting_xpts - current_starting_xpts,
            "captain_xpts_gain": recommendation.captain_bonus_xpts - current_captain_bonus,
            "started": [
                _payload(player)
                for player in recommendation.starters
                if player.fpl_id not in current_starter_ids
            ],
            "benched": [
                _payload(player)
                for player in current_starters
                if player.fpl_id not in recommended_starter_ids
            ],
            "captain_change": (
                None
                if recommendation.captain.fpl_id == current_captain.fpl_id
                else {"from": _payload(current_captain), "to": _payload(recommendation.captain)}
            ),
            "vice_captain_change": (
                None
                if recommendation.vice_captain.fpl_id == current_vice.fpl_id
                else {"from": _payload(current_vice), "to": _payload(recommendation.vice_captain)}
            ),
            "bench_order_changed": tuple(player.fpl_id for player in current_bench)
            != tuple(player.fpl_id for player in recommended_bench),
            "current_bench": [_payload(player) for player in current_bench],
        }

    return {
        "gameweek": gameweek,
        "formation": recommendation.formation,
        "starting_xpts": recommendation.starting_xpts,
        "captain_bonus_xpts": recommendation.captain_bonus_xpts,
        "total_xpts": recommendation.total_xpts,
        "uncertainty": recommendation.uncertainty,
        "captain": _payload(recommendation.captain),
        "vice_captain": _payload(recommendation.vice_captain),
        "starters": [_payload(player) for player in recommendation.starters],
        "bench": [
            _payload(recommendation.bench_goalkeeper),
            *[_payload(player) for player in recommendation.outfield_bench_order],
        ],
        "expected_autosub_value": autosub.total_expected_bench_value,
        "quality_flags": list(recommendation.data_quality_flags),
        "role_scenario_sensitivity": sensitivity_report,
        "current_setup_comparison": current_setup_comparison,
    }


def _rating_source_identity(
    horizon: ResearchHorizon,
    catalog: dict[int, dict[str, Any]],
    projections: dict[int, dict[int, PlayerGameweekProjection]],
    release_id: str | None,
) -> str:
    """Stable identity even in DB/local mode where no web release ID exists."""

    if release_id is not None:
        return release_id
    payload = {
        "source_ingestion_run_id": horizon.source_ingestion_run_id,
        "model_version": horizon.model_version,
        "planning_as_of": horizon.planning_as_of,
        "model_runs": horizon.model_runs,
        "players": [
            {
                "fpl_id": fpl_id,
                "team_id": row["team_id"],
                "position": row["position"],
                "price_tenths": row["price_tenths"],
                "status": row["status"],
                "xpts": [
                    projections[gameweek][fpl_id].expected_points
                    for gameweek, _ in horizon.model_runs
                ],
            }
            for fpl_id, row in sorted(catalog.items())
        ],
    }
    digest = hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"local_horizon_{digest[:16]}"


def _rating_pools(
    horizon: ResearchHorizon,
    catalog: dict[int, dict[str, Any]],
    projections: dict[int, dict[int, PlayerGameweekProjection]],
) -> tuple[GameweekProjectionPool, ...]:
    transferable = tuple(
        sorted(fpl_id for fpl_id, row in catalog.items() if row["status"] in {"a", "d"})
    )
    targets_by_gameweek: list[GameweekProjectionPool] = []
    for gameweek, _ in horizon.model_runs:
        targets = tuple(
            TransferTarget(
                player=SquadPlayer(
                    fpl_id=fpl_id,
                    player_code=row.get("player_code"),
                    player_name=row["name"],
                    team_id=row["team_id"],
                    position=row["position"],
                    current_price_tenths=row["price_tenths"],
                    purchase_price_tenths=row["price_tenths"],
                    selling_price_tenths=row["price_tenths"],
                    squad_position=1,
                    is_captain=False,
                    is_vice_captain=False,
                ),
                projection=projections[gameweek][fpl_id],
            )
            for fpl_id, row in sorted(catalog.items())
        )
        targets_by_gameweek.append(
            GameweekProjectionPool(
                gameweek=gameweek,
                players=targets,
                transferable_fpl_ids=transferable,
            )
        )
    return tuple(targets_by_gameweek)


def _rating_unavailable(
    *,
    source_identity: str,
    budget_tenths: int,
    lineups: list[dict[str, Any]],
    release_health: str,
    reviewed_scenario: bool,
    squad_rule_flags: tuple[str, ...],
    reason: str,
) -> dict[str, Any]:
    quality_flags = sorted({flag for row in lineups for flag in row["quality_flags"]})
    uncertainty_values = tuple(row["uncertainty"] for row in lineups)
    return {
        "schema_version": RATING_SCHEMA_VERSION,
        "display_label": "Model Score" if release_health == "production" else "Model Preview",
        "formula_version": RATING_FORMULA_VERSION,
        "available": False,
        "benchmark": {
            "benchmark_id": None,
            "source_identity": source_identity,
            "budget_tenths": budget_tenths,
            "population_size": 0,
        },
        "input": {
            "gameweeks": [row["gameweek"] for row in lineups],
            "raw_gameweek_xpts": [row["total_xpts"] for row in lineups],
            "raw_cumulative_xpts": sum(row["total_xpts"] for row in lineups),
            "reviewed_scenario": reviewed_scenario,
        },
        "model_strength": None,
        "release_gate": {
            "health": release_health,
            "production_approved": release_health == "production",
        },
        "data_confidence": {
            "state": "review" if quality_flags else "clean",
            "quality_flags": quality_flags,
        },
        "projection_uncertainty": {
            "gameweek": [
                {"gameweek": row["gameweek"], "uncertainty": row["uncertainty"]}
                for row in lineups
            ],
            "cumulative_rss": (
                None
                if any(value is None for value in uncertainty_values)
                else sqrt(sum(value**2 for value in uncertainty_values))
            ),
        },
        "squad_rule_health": {
            "state": "pass" if not squad_rule_flags else "review",
            "flags": list(squad_rule_flags),
        },
        "explanation": f"Rating withheld: {reason}. Raw xPts remain available.",
    }


def _squad_rating_payload(
    *,
    horizon: ResearchHorizon,
    catalog: dict[int, dict[str, Any]],
    base_projections: dict[int, dict[int, PlayerGameweekProjection]],
    release_id: str | None,
    release_health: str,
    fpl_ids: tuple[int, ...],
    bank_tenths: int,
    squad,
    lineups: list[dict[str, Any]],
    reviewed_scenario: bool,
) -> dict[str, Any]:
    source_identity = _rating_source_identity(
        horizon, catalog, base_projections, release_id
    )
    benchmark_budget_tenths = (
        sum(catalog[fpl_id]["price_tenths"] for fpl_id in fpl_ids) + bank_tenths
    )
    try:
        cache_key = (source_identity, benchmark_budget_tenths)
        benchmark = _SQUAD_BENCHMARK_CACHE.get(cache_key)
        if benchmark is None:
            benchmark = build_squad_benchmark(
                _rating_pools(horizon, catalog, base_projections),
                source_identity=source_identity,
                budget_tenths=benchmark_budget_tenths,
            )
            _SQUAD_BENCHMARK_CACHE[cache_key] = benchmark
        rating = rate_squad(
            benchmark,
            raw_gameweek_xpts=tuple(row["total_xpts"] for row in lineups),
            gameweek_uncertainty=tuple(row["uncertainty"] for row in lineups),
            quality_flags=tuple(
                sorted({flag for row in lineups for flag in row["quality_flags"]})
            ),
            squad_rule_flags=squad.constraint_flags,
            release_health=release_health,
            reviewed_scenario=reviewed_scenario,
        )
    except ValueError as error:
        rating = _rating_unavailable(
            source_identity=source_identity,
            budget_tenths=benchmark_budget_tenths,
            lineups=lineups,
            release_health=release_health,
            reviewed_scenario=reviewed_scenario,
            squad_rule_flags=squad.constraint_flags,
            reason=str(error),
        )
    rating["input"]["squad_fpl_ids"] = sorted(fpl_ids)
    rating["input"]["optimized_decisions"] = [
        {
            "gameweek": row["gameweek"],
            "starter_fpl_ids": sorted(player["fpl_id"] for player in row["starters"]),
            "captain_fpl_id": row["captain"]["fpl_id"],
            "vice_captain_fpl_id": row["vice_captain"]["fpl_id"],
            "formation": row["formation"],
        }
        for row in lineups
    ]
    return rating


def recommend_web_lineups(
    fpl_ids: tuple[int, ...],
    *,
    bank_tenths: int = 0,
    free_transfers: int = 1,
    selling_prices: dict[int, int] | None = None,
    role_scenario_overrides: tuple[RoleScenarioOverride, ...] = (),
    current_setup: CurrentSquadSetup | None = None,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    release_path: str | Path | None = None,
) -> dict[str, Any]:
    horizon, catalog, projections, health, release_id, release_metadata = _load_web_inputs(
        database_path=database_path,
        release_path=release_path,
    )
    # The benchmark always uses the frozen base release. Reviewed what-if
    # overrides may alter the submitted squad's score, but never move the
    # comparison population or redefine the scale.
    base_projections = projections
    projections = apply_role_scenario_overrides(
        base_projections, role_scenario_overrides, horizon=horizon
    )
    squad = _validated_web_squad(
        catalog,
        fpl_ids,
        bank_tenths=bank_tenths,
        free_transfers=free_transfers,
        selling_prices={} if selling_prices is None else selling_prices,
    )
    if current_setup is not None:
        if current_setup.gameweek != horizon.start_gameweek:
            raise ValueError(
                "current setup gameweek must match the first Gameweek in the projection horizon"
            )
        setup_ids = set((*current_setup.starter_fpl_ids, *current_setup.bench_fpl_ids))
        if setup_ids != set(fpl_ids):
            raise ValueError("current setup players must exactly match the submitted squad")
    lineups = [
        _lineup_payload(
            squad,
            projections[gameweek],
            gameweek,
            catalog=catalog,
            current_setup=current_setup if gameweek == horizon.start_gameweek else None,
        )
        for gameweek, _ in horizon.model_runs
    ]
    rating = _squad_rating_payload(
        horizon=horizon,
        catalog=catalog,
        base_projections=base_projections,
        release_id=release_id,
        release_health=health,
        fpl_ids=fpl_ids,
        bank_tenths=bank_tenths,
        squad=squad,
        lineups=lineups,
        reviewed_scenario=bool(role_scenario_overrides),
    )
    release_metadata = release_metadata or {}
    return {
        "health": health,
        "release_id": release_id,
        "is_reviewed_scenario": bool(role_scenario_overrides),
        "coverage": release_metadata.get("coverage"),
        "freshness": release_metadata.get("freshness"),
        "horizon": [gameweek for gameweek, _ in horizon.model_runs],
        "lineups": lineups,
        "cumulative_xpts": sum(row["total_xpts"] for row in lineups),
        "squad_rating": rating,
        "method_note": (
            "Exhaustive legal XI and captain search over one frozen research horizon."
            if not role_scenario_overrides
            else "Exhaustive legal XI and captain search recomputed from one or more reviewed "
            "xPts overrides over the same frozen research horizon. The underlying release is "
            "unchanged; this is a what-if scenario, not a new projection run."
        ),
    }


def recommend_web_transfers(
    fpl_ids: tuple[int, ...],
    *,
    bank_tenths: int = 0,
    free_transfers: int = 1,
    selling_prices: dict[int, int] | None = None,
    role_scenario_overrides: tuple[RoleScenarioOverride, ...] = (),
    top_n: int = 8,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    release_path: str | Path | None = None,
) -> dict[str, Any]:
    selling_prices = {} if selling_prices is None else selling_prices
    horizon, catalog, projections, health, release_id, release_metadata = _load_web_inputs(
        database_path=database_path,
        release_path=release_path,
    )
    base_projections = projections
    projections = apply_role_scenario_overrides(
        base_projections, role_scenario_overrides, horizon=horizon
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
    baseline_rating = _squad_rating_payload(
        horizon=horizon,
        catalog=catalog,
        base_projections=base_projections,
        release_id=release_id,
        release_health=health,
        fpl_ids=fpl_ids,
        bank_tenths=bank_tenths,
        squad=baseline_squad,
        lineups=baseline_lineups,
        reviewed_scenario=bool(role_scenario_overrides),
    )
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
    release_metadata = release_metadata or {}
    return {
        "health": health,
        "release_id": release_id,
        "is_reviewed_scenario": bool(role_scenario_overrides),
        "coverage": release_metadata.get("coverage"),
        "freshness": release_metadata.get("freshness"),
        "baseline_cumulative_xpts": baseline_xpts,
        "baseline_squad_rating": baseline_rating,
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
