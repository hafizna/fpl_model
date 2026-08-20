"""DuckDB read boundary for a frozen three-Gameweek planning horizon."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import sqrt

import duckdb

from fpl_model.decision.lineup import PlayerGameweekProjection
from fpl_model.decision.lineup_store import StoredLineupInputs, _flags, load_lineup_inputs
from fpl_model.decision.rolling import GameweekProjectionPool
from fpl_model.decision.squad import SquadPlayer
from fpl_model.decision.transfer import TransferTarget


@dataclass(frozen=True, slots=True)
class RollingPoolDiagnostics:
    gameweek: int
    model_run_id: str
    projected_players: int
    excluded_missing_projection: int
    excluded_unavailable: int


@dataclass(frozen=True, slots=True)
class StoredRollingInputs:
    lineup_inputs: StoredLineupInputs
    model_run_ids: tuple[tuple[int, str], ...]
    planning_as_of: datetime
    model_version: str
    pools: tuple[GameweekProjectionPool, ...]
    diagnostics: tuple[RollingPoolDiagnostics, ...]


def _aggregate_projections(
    connection: duckdb.DuckDBPyConnection,
    model_run_id: str,
) -> dict[int, tuple[float, float | None, tuple[str, ...]]]:
    rows = connection.execute(
        """
        SELECT player_code, final_xpts, uncertainty, data_quality_flags
        FROM player_fixture_projection
        WHERE model_run_id = ?
        """,
        [model_run_id],
    ).fetchall()
    by_code: dict[int, list[tuple[float, float | None, str | None]]] = {}
    for player_code, final_xpts, uncertainty, flags in rows:
        by_code.setdefault(int(player_code), []).append(
            (
                float(final_xpts),
                None if uncertainty is None else float(uncertainty),
                flags,
            )
        )
    result = {}
    for player_code, fixture_rows in by_code.items():
        uncertainties = [row[1] for row in fixture_rows]
        combined_uncertainty = (
            None
            if any(value is None for value in uncertainties)
            else sqrt(sum(value**2 for value in uncertainties if value is not None))
        )
        result[player_code] = (
            sum(row[0] for row in fixture_rows),
            combined_uncertainty,
            tuple(sorted({flag for row in fixture_rows for flag in _flags(row[2])})),
        )
    return result


def load_rolling_inputs(
    connection: duckdb.DuckDBPyConnection,
    *,
    squad_snapshot_id: str,
    model_run_ids: dict[int, str],
) -> StoredRollingInputs:
    """Load three consecutive runs frozen to one source, version, and as-of time."""
    if len(model_run_ids) != 3:
        raise ValueError("exactly three distinct Gameweek model runs are required")
    gameweeks = tuple(sorted(model_run_ids))
    expected = tuple(range(gameweeks[0], gameweeks[0] + 3))
    if gameweeks != expected:
        raise ValueError(f"model-run Gameweeks must be consecutive, expected {expected}")
    if len(set(model_run_ids.values())) != 3:
        raise ValueError("model_run_id values must be distinct")

    first_model_run_id = model_run_ids[gameweeks[0]]
    lineup_inputs = load_lineup_inputs(
        connection,
        squad_snapshot_id=squad_snapshot_id,
        model_run_id=first_model_run_id,
    )
    if lineup_inputs.target_gameweek != gameweeks[0]:
        raise ValueError("squad snapshot must target the first planning Gameweek")

    metadata = []
    for gameweek in gameweeks:
        model_run_id = model_run_ids[gameweek]
        row = connection.execute(
            """
            SELECT target_gameweek, as_of, deadline, model_version,
                   source_ingestion_run_id, status, completed_at
            FROM model_run WHERE model_run_id = ?
            """,
            [model_run_id],
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown model_run_id: {model_run_id}")
        target, as_of, deadline, version, source_run, status, completed_at = row
        if int(target) != gameweek:
            raise ValueError(
                f"model run {model_run_id} targets GW{target}, not requested GW{gameweek}"
            )
        if status != "completed":
            raise ValueError(f"model run {model_run_id} must be completed")
        metadata.append(
            (
                gameweek,
                model_run_id,
                as_of,
                deadline,
                version,
                source_run,
                completed_at,
            )
        )

    source_runs = {row[5] for row in metadata}
    versions = {row[4] for row in metadata}
    as_of_values = {row[2] for row in metadata}
    if source_runs != {lineup_inputs.source_ingestion_run_id}:
        raise ValueError("all horizon model runs must use the squad's pinned FPL snapshot")
    if len(versions) != 1:
        raise ValueError("all horizon model runs must use one model_version")
    if len(as_of_values) != 1:
        raise ValueError("all horizon model runs must share one frozen as_of timestamp")
    planning_as_of = metadata[0][2]
    first_deadline = metadata[0][3]
    if planning_as_of > first_deadline:
        raise ValueError("planning as_of must not be after the first Gameweek deadline")
    if any(
        row[6] is None
        or row[6] > first_deadline
        for row in metadata
    ):
        raise ValueError("all horizon model runs must be completed by the first deadline")
    deadlines = tuple(row[3] for row in metadata)
    if any(
        later <= earlier
        for earlier, later in zip(deadlines, deadlines[1:], strict=False)
    ):
        raise ValueError("horizon model-run deadlines must be chronological")

    player_rows = connection.execute(
        """
        SELECT ps.fpl_id, ps.player_code, ps.web_name, ps.team_id,
               ps.fpl_position, ps.price, pss.can_transact, pss.removed
        FROM player_snapshot AS ps
        LEFT JOIN player_status_snapshot AS pss
          ON pss.ingestion_run_id = ps.ingestion_run_id
         AND pss.fpl_id = ps.fpl_id
        WHERE ps.ingestion_run_id = ?
        ORDER BY ps.fpl_id
        """,
        [lineup_inputs.source_ingestion_run_id],
    ).fetchall()
    owned_by_id = {player.fpl_id: player for player in lineup_inputs.squad.players}
    pools = []
    diagnostics = []
    for gameweek, model_run_id, *_ in metadata:
        projections = _aggregate_projections(connection, model_run_id)
        targets = []
        transferable_fpl_ids = []
        excluded_missing = 0
        excluded_unavailable = 0
        for fpl_id, player_code, name, team_id, position, price, can_transact, removed in player_rows:
            fpl_id = int(fpl_id)
            is_owned = fpl_id in owned_by_id
            is_transferable = can_transact is True and removed is False
            if not is_owned and not is_transferable:
                excluded_unavailable += 1
                continue
            if player_code is None or int(player_code) not in projections:
                excluded_missing += 1
                continue
            if is_owned:
                player = owned_by_id[fpl_id]
            else:
                current_price_tenths = int(Decimal(str(price)) * 10)
                player = SquadPlayer(
                    fpl_id=fpl_id,
                    player_code=int(player_code),
                    player_name=str(name),
                    team_id=int(team_id),
                    position=str(position),
                    current_price_tenths=current_price_tenths,
                    purchase_price_tenths=current_price_tenths,
                    selling_price_tenths=current_price_tenths,
                    squad_position=1,
                    is_captain=False,
                    is_vice_captain=False,
                )
            xpts, uncertainty, flags = projections[int(player_code)]
            targets.append(
                TransferTarget(
                    player=player,
                    projection=PlayerGameweekProjection(
                        fpl_id=fpl_id,
                        expected_points=xpts,
                        uncertainty=uncertainty,
                        data_quality_flags=flags,
                    ),
                )
            )
            if is_transferable:
                transferable_fpl_ids.append(fpl_id)
        pools.append(
            GameweekProjectionPool(
                gameweek=gameweek,
                players=tuple(targets),
                transferable_fpl_ids=tuple(transferable_fpl_ids),
            )
        )
        diagnostics.append(
            RollingPoolDiagnostics(
                gameweek=gameweek,
                model_run_id=model_run_id,
                projected_players=len(targets),
                excluded_missing_projection=excluded_missing,
                excluded_unavailable=excluded_unavailable,
            )
        )
    return StoredRollingInputs(
        lineup_inputs=lineup_inputs,
        model_run_ids=tuple((gameweek, model_run_ids[gameweek]) for gameweek in gameweeks),
        planning_as_of=planning_as_of,
        model_version=str(metadata[0][4]),
        pools=tuple(pools),
        diagnostics=tuple(diagnostics),
    )
