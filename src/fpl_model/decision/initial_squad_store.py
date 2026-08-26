"""DuckDB read boundary for public-data preseason initial-squad inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import duckdb

from fpl_model.decision.lineup import PlayerGameweekProjection
from fpl_model.decision.rolling import GameweekProjectionPool
from fpl_model.decision.rolling_store import (
    RollingPoolDiagnostics,
    aggregate_projections,
)
from fpl_model.decision.squad import SquadPlayer
from fpl_model.decision.transfer import TransferTarget


@dataclass(frozen=True, slots=True)
class StoredInitialSquadInputs:
    source_ingestion_run_id: str
    model_run_ids: tuple[tuple[int, str], ...]
    planning_as_of: datetime
    model_version: str
    pools: tuple[GameweekProjectionPool, ...]
    diagnostics: tuple[RollingPoolDiagnostics, ...]


def load_initial_squad_inputs(
    connection: duckdb.DuckDBPyConnection,
    *,
    model_run_ids: dict[int, str],
) -> StoredInitialSquadInputs:
    """Load three completed, pre-deadline runs without requiring a manager squad."""
    if len(model_run_ids) != 3:
        raise ValueError("exactly three distinct Gameweek model runs are required")
    gameweeks = tuple(sorted(model_run_ids))
    expected = tuple(range(gameweeks[0], gameweeks[0] + 3))
    if gameweeks != expected:
        raise ValueError(f"model-run Gameweeks must be consecutive, expected {expected}")
    if len(set(model_run_ids.values())) != 3:
        raise ValueError("model_run_id values must be distinct")

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
            (gameweek, model_run_id, as_of, deadline, version, source_run, completed_at)
        )

    source_runs = {row[5] for row in metadata}
    versions = {row[4] for row in metadata}
    as_of_values = {row[2] for row in metadata}
    if len(source_runs) != 1:
        raise ValueError("all horizon model runs must use one official FPL snapshot")
    if len(versions) != 1:
        raise ValueError("all horizon model runs must use one model_version")
    if len(as_of_values) != 1:
        raise ValueError("all horizon model runs must share one frozen as_of timestamp")
    planning_as_of = metadata[0][2]
    first_deadline = metadata[0][3]
    if planning_as_of > first_deadline:
        raise ValueError("planning as_of must not be after the first Gameweek deadline")
    if any(row[6] is None or row[6] > first_deadline for row in metadata):
        raise ValueError("all horizon model runs must be completed by the first deadline")
    deadlines = tuple(row[3] for row in metadata)
    if any(later <= earlier for earlier, later in zip(deadlines, deadlines[1:], strict=False)):
        raise ValueError("horizon model-run deadlines must be chronological")

    source_run = str(metadata[0][5])
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
        [source_run],
    ).fetchall()

    pools = []
    diagnostics = []
    for gameweek, model_run_id, *_ in metadata:
        projections = aggregate_projections(connection, model_run_id)
        targets = []
        excluded_missing = 0
        excluded_unavailable = 0
        for fpl_id, player_code, name, team_id, position, price, can_transact, removed in player_rows:
            if can_transact is not True or removed is not False:
                excluded_unavailable += 1
                continue
            if player_code is None or int(player_code) not in projections:
                excluded_missing += 1
                continue
            current_price_tenths = int(Decimal(str(price)) * 10)
            player = SquadPlayer(
                fpl_id=int(fpl_id),
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
            xpts, uncertainty, flags, appearance_probability = projections[int(player_code)]
            targets.append(
                TransferTarget(
                    player=player,
                    projection=PlayerGameweekProjection(
                        fpl_id=int(fpl_id),
                        expected_points=xpts,
                        uncertainty=uncertainty,
                        data_quality_flags=flags,
                        appearance_probability=appearance_probability,
                    ),
                )
            )
        transferable_ids = tuple(row.player.fpl_id for row in targets)
        pools.append(
            GameweekProjectionPool(
                gameweek=gameweek,
                players=tuple(targets),
                transferable_fpl_ids=transferable_ids,
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
    return StoredInitialSquadInputs(
        source_ingestion_run_id=source_run,
        model_run_ids=tuple((gameweek, model_run_ids[gameweek]) for gameweek in gameweeks),
        planning_as_of=planning_as_of,
        model_version=str(metadata[0][4]),
        pools=tuple(pools),
        diagnostics=tuple(diagnostics),
    )
