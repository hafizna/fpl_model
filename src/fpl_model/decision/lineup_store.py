"""DuckDB read boundary for squad plus player-Gameweek projection inputs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from math import sqrt

import duckdb

from fpl_model.decision.lineup import PlayerGameweekProjection
from fpl_model.decision.squad import SquadPlayer, ValidatedSquad, validate_squad


@dataclass(frozen=True, slots=True)
class StoredLineupInputs:
    squad_snapshot_id: str
    model_run_id: str
    source_ingestion_run_id: str
    target_gameweek: int
    squad: ValidatedSquad
    projections: tuple[PlayerGameweekProjection, ...]


def _flags(value: str | None) -> set[str]:
    if value is None or not value.strip():
        return set()
    if value.lstrip().startswith("["):
        parsed = json.loads(value)
        if not isinstance(parsed, list) or any(not isinstance(flag, str) for flag in parsed):
            raise ValueError("projection data_quality_flags JSON must be a list of strings")
        return {flag.strip() for flag in parsed if flag.strip()}
    return {flag.strip() for flag in value.split("|") if flag.strip()}


def combine_appearance_probability(
    fixture_rows: list[tuple[float, float | None, str | None, float, float]],
) -> float:
    """Combine per-fixture start+substitute appearance probability into one
    Gameweek-level probability of playing >=1 minute, for autosub value.

    Each fixture's own appearance probability is
    ``start_probability + substitute_appearance_probability`` (mutually
    exclusive per player_fixture_projection's own CHECK constraint). For a
    double Gameweek, fixtures are combined as independent events: the
    probability of blanking BOTH fixtures is the product of each fixture's own
    blank probability, so the Gameweek appearance probability is
    ``1 - product(1 - appearance_i)``.
    """
    blank_probability = 1.0
    for _, _, _, start_probability, sub_probability in fixture_rows:
        appearance = start_probability + sub_probability
        blank_probability *= max(0.0, 1.0 - appearance)
    return 1.0 - blank_probability


def load_lineup_inputs(
    connection: duckdb.DuckDBPyConnection,
    *,
    squad_snapshot_id: str,
    model_run_id: str,
) -> StoredLineupInputs:
    """Load and validate one squad against one same-GW model run."""
    snapshot = connection.execute(
        """
        SELECT source_ingestion_run_id, target_gameweek, bank_tenths,
               free_transfers, unlimited_transfers, chip_period, status
        FROM squad_snapshot
        WHERE squad_snapshot_id = ?
        """,
        [squad_snapshot_id],
    ).fetchone()
    if snapshot is None:
        raise ValueError(f"unknown squad_snapshot_id: {squad_snapshot_id}")
    (
        source_ingestion_run_id,
        squad_gameweek,
        bank_tenths,
        free_transfers,
        unlimited_transfers,
        chip_period,
        squad_status,
    ) = snapshot
    if squad_status != "completed":
        raise ValueError("squad snapshot must be completed")

    model = connection.execute(
        "SELECT target_gameweek, status FROM model_run WHERE model_run_id = ?",
        [model_run_id],
    ).fetchone()
    if model is None:
        raise ValueError(f"unknown model_run_id: {model_run_id}")
    model_gameweek, model_status = model
    if model_status != "completed":
        raise ValueError("model run must be completed")
    if model_gameweek != squad_gameweek:
        raise ValueError(
            f"squad targets GW{squad_gameweek} but model run targets GW{model_gameweek}"
        )

    player_rows = connection.execute(
        """
        SELECT ssp.fpl_id, ps.player_code, ps.web_name, ps.team_id,
               ps.fpl_position, ps.price, ssp.purchase_price_tenths,
               ssp.selling_price_tenths, ssp.squad_position,
               ssp.is_captain, ssp.is_vice_captain
        FROM squad_snapshot_player AS ssp
        JOIN squad_snapshot AS ss USING (squad_snapshot_id)
        JOIN player_snapshot AS ps
          ON ps.ingestion_run_id = ss.source_ingestion_run_id
         AND ps.fpl_id = ssp.fpl_id
        WHERE ssp.squad_snapshot_id = ?
        ORDER BY ssp.squad_position
        """,
        [squad_snapshot_id],
    ).fetchall()
    if len(player_rows) != 15:
        raise ValueError(f"squad snapshot must resolve exactly 15 players, got {len(player_rows)}")

    squad_players = tuple(
        SquadPlayer(
            fpl_id=int(row[0]),
            player_code=None if row[1] is None else int(row[1]),
            player_name=str(row[2]),
            team_id=int(row[3]),
            position=str(row[4]),
            current_price_tenths=int(Decimal(str(row[5])) * 10),
            purchase_price_tenths=int(row[6]),
            selling_price_tenths=int(row[7]),
            squad_position=int(row[8]),
            is_captain=bool(row[9]),
            is_vice_captain=bool(row[10]),
        )
        for row in player_rows
    )
    chip_states = dict(
        connection.execute(
            """
            SELECT chip_name, chip_status
            FROM squad_chip_state
            WHERE squad_snapshot_id = ?
            """,
            [squad_snapshot_id],
        ).fetchall()
    )
    squad = validate_squad(
        squad_players,
        bank_tenths=int(bank_tenths),
        free_transfers=None if free_transfers is None else int(free_transfers),
        unlimited_transfers=bool(unlimited_transfers),
        chip_period=int(chip_period),
        chip_states=chip_states,
        allow_grandfathered_team_limit=True,
    )

    code_to_fpl_id = {
        player.player_code: player.fpl_id
        for player in squad.players
        if player.player_code is not None
    }
    projection_rows = connection.execute(
        """
        SELECT player_code, final_xpts, uncertainty, data_quality_flags,
               start_probability, substitute_appearance_probability
        FROM player_fixture_projection
        WHERE model_run_id = ?
        """,
        [model_run_id],
    ).fetchall()
    by_fpl_id: dict[
        int, list[tuple[float, float | None, str | None, float, float]]
    ] = {}
    for player_code, final_xpts, uncertainty, flags, start_probability, sub_probability in (
        projection_rows
    ):
        fpl_id = code_to_fpl_id.get(int(player_code))
        if fpl_id is None:
            continue
        by_fpl_id.setdefault(fpl_id, []).append(
            (
                float(final_xpts),
                None if uncertainty is None else float(uncertainty),
                flags,
                float(start_probability),
                float(sub_probability),
            )
        )

    missing = sorted(player.fpl_id for player in squad.players if player.fpl_id not in by_fpl_id)
    if missing:
        raise ValueError(
            "model run is missing projections for squad fpl_id values: " + repr(missing)
        )

    projections = []
    for player in squad.players:
        fixture_rows = by_fpl_id[player.fpl_id]
        uncertainty_values = [row[1] for row in fixture_rows]
        combined_uncertainty = (
            None
            if any(value is None for value in uncertainty_values)
            else sqrt(sum(value**2 for value in uncertainty_values if value is not None))
        )
        projections.append(
            PlayerGameweekProjection(
                fpl_id=player.fpl_id,
                expected_points=sum(row[0] for row in fixture_rows),
                uncertainty=combined_uncertainty,
                data_quality_flags=tuple(
                    sorted({flag for row in fixture_rows for flag in _flags(row[2])})
                ),
                appearance_probability=combine_appearance_probability(fixture_rows),
            )
        )

    return StoredLineupInputs(
        squad_snapshot_id=squad_snapshot_id,
        model_run_id=model_run_id,
        source_ingestion_run_id=str(source_ingestion_run_id),
        target_gameweek=int(squad_gameweek),
        squad=squad,
        projections=tuple(projections),
    )
