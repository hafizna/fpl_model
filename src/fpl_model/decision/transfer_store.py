"""DuckDB read boundary for single-transfer recommendation inputs."""

from __future__ import annotations

from decimal import Decimal
from math import sqrt

import duckdb

from fpl_model.decision.lineup import PlayerGameweekProjection
from fpl_model.decision.lineup_store import StoredLineupInputs, _flags, load_lineup_inputs
from fpl_model.decision.squad import SquadPlayer
from fpl_model.decision.transfer import TransferTarget


def load_transfer_inputs(
    connection: duckdb.DuckDBPyConnection,
    *,
    squad_snapshot_id: str,
    model_run_id: str,
) -> tuple[StoredLineupInputs, tuple[TransferTarget, ...], int, int]:
    """Load the current squad and all non-owned players with complete projections."""
    inputs = load_lineup_inputs(
        connection,
        squad_snapshot_id=squad_snapshot_id,
        model_run_id=model_run_id,
    )
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
        [inputs.source_ingestion_run_id],
    ).fetchall()
    fixture_rows = connection.execute(
        """
        SELECT player_code, final_xpts, uncertainty, data_quality_flags
        FROM player_fixture_projection
        WHERE model_run_id = ?
        """,
        [model_run_id],
    ).fetchall()
    by_code: dict[int, list[tuple[float, float | None, str | None]]] = {}
    for player_code, final_xpts, uncertainty, flags in fixture_rows:
        by_code.setdefault(int(player_code), []).append(
            (
                float(final_xpts),
                None if uncertainty is None else float(uncertainty),
                flags,
            )
        )

    owned_ids = {player.fpl_id for player in inputs.squad.players}
    targets: list[TransferTarget] = []
    excluded_missing_projection = 0
    excluded_unavailable = 0
    for fpl_id, player_code, name, team_id, position, price, can_transact, removed in player_rows:
        if int(fpl_id) in owned_ids:
            continue
        if can_transact is not True or removed is not False:
            excluded_unavailable += 1
            continue
        if player_code is None or int(player_code) not in by_code:
            excluded_missing_projection += 1
            continue
        rows = by_code[int(player_code)]
        uncertainties = [row[1] for row in rows]
        combined_uncertainty = (
            None
            if any(value is None for value in uncertainties)
            else sqrt(sum(value**2 for value in uncertainties if value is not None))
        )
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
        targets.append(
            TransferTarget(
                player=player,
                projection=PlayerGameweekProjection(
                    fpl_id=int(fpl_id),
                    expected_points=sum(row[0] for row in rows),
                    uncertainty=combined_uncertainty,
                    data_quality_flags=tuple(
                        sorted({flag for row in rows for flag in _flags(row[2])})
                    ),
                ),
            )
        )
    return inputs, tuple(targets), excluded_missing_projection, excluded_unavailable
