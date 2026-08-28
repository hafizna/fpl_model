"""Compare provisional and final compact releases without mutating either."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fpl_model.webapp.service import (
    load_release_catalog,
    recommend_web_lineups,
    recommend_web_transfers,
)


@dataclass(frozen=True, slots=True)
class ReleaseDriftResult:
    report: dict[str, Any]

    @property
    def material_change(self) -> bool:
        return bool(self.report["summary"]["material_change"])


def _top_transfer(result: dict[str, Any]) -> dict[str, Any] | None:
    if result["recommendation"] != "transfer" or not result["suggestions"]:
        return None
    row = result["suggestions"][0]
    return {
        "out_fpl_id": row["out"]["fpl_id"],
        "out_name": row["out"]["name"],
        "in_fpl_id": row["in"]["fpl_id"],
        "in_name": row["in"]["name"],
        "net_xpts_gain": row["net_xpts_gain"],
    }


def _overall_percentile(result: dict[str, Any]) -> float | None:
    rating = result.get("squad_rating")
    if not rating or not rating.get("available"):
        return None
    return float(rating["model_strength"]["overall_3gw"]["percentile"])


def compare_web_releases(
    *,
    before_path: str | Path,
    after_path: str | Path,
    owned_fpl_ids: tuple[int, ...] | None = None,
    bank_tenths: int = 0,
    free_transfers: int = 1,
    selling_prices: dict[int, int] | None = None,
    xpts_threshold: float = 0.25,
    appearance_threshold: float = 0.05,
    include_transfer_scan: bool = True,
) -> ReleaseDriftResult:
    """Report material player and optional manager-decision changes."""

    before_horizon, before_catalog, _, before_health, before_release_id = load_release_catalog(
        before_path
    )
    after_horizon, after_catalog, _, after_health, after_release_id = load_release_catalog(
        after_path
    )
    before_gameweeks = tuple(gameweek for gameweek, _ in before_horizon.model_runs)
    after_gameweeks = tuple(gameweek for gameweek, _ in after_horizon.model_runs)
    if before_gameweeks != after_gameweeks:
        raise ValueError("release horizons differ; drift requires identical Gameweeks")
    if xpts_threshold < 0.0 or appearance_threshold < 0.0:
        raise ValueError("materiality thresholds must be non-negative")

    player_changes: list[dict[str, Any]] = []
    common_ids = sorted(set(before_catalog) & set(after_catalog))
    for fpl_id in common_ids:
        before_player = before_catalog[fpl_id]
        after_player = after_catalog[fpl_id]
        for gameweek in before_gameweeks:
            before_row = before_player["gameweeks"][str(gameweek)]
            after_row = after_player["gameweeks"][str(gameweek)]
            xpts_delta = float(after_row["xpts"]) - float(before_row["xpts"])
            appearance_delta = float(after_row["appearance_probability"]) - float(
                before_row["appearance_probability"]
            )
            if (
                abs(xpts_delta) < xpts_threshold
                and abs(appearance_delta) < appearance_threshold
            ):
                continue
            player_changes.append(
                {
                    "fpl_id": fpl_id,
                    "name": after_player["name"],
                    "position": after_player["position"],
                    "gameweek": gameweek,
                    "before_xpts": float(before_row["xpts"]),
                    "after_xpts": float(after_row["xpts"]),
                    "xpts_delta": xpts_delta,
                    "appearance_probability_delta": appearance_delta,
                }
            )
    player_changes.sort(
        key=lambda row: (-abs(row["xpts_delta"]), row["gameweek"], row["name"])
    )

    added = sorted(set(after_catalog) - set(before_catalog))
    removed = sorted(set(before_catalog) - set(after_catalog))
    decisions: dict[str, Any] = {"evaluated": owned_fpl_ids is not None}
    decision_changed = False
    if owned_fpl_ids is not None:
        selling_prices = {} if selling_prices is None else selling_prices
        before_lineups = recommend_web_lineups(
            owned_fpl_ids,
            bank_tenths=bank_tenths,
            free_transfers=free_transfers,
            selling_prices=selling_prices,
            release_path=before_path,
        )
        after_lineups = recommend_web_lineups(
            owned_fpl_ids,
            bank_tenths=bank_tenths,
            free_transfers=free_transfers,
            selling_prices=selling_prices,
            release_path=after_path,
        )
        lineup_changes = []
        for before_row, after_row in zip(
            before_lineups["lineups"], after_lineups["lineups"], strict=True
        ):
            before_starters = {row["fpl_id"] for row in before_row["starters"]}
            after_starters = {row["fpl_id"] for row in after_row["starters"]}
            changed = (
                before_starters != after_starters
                or before_row["captain"]["fpl_id"] != after_row["captain"]["fpl_id"]
                or before_row["formation"] != after_row["formation"]
            )
            decision_changed = decision_changed or changed
            lineup_changes.append(
                {
                    "gameweek": before_row["gameweek"],
                    "changed": changed,
                    "players_out": sorted(before_starters - after_starters),
                    "players_in": sorted(after_starters - before_starters),
                    "before_captain": before_row["captain"]["name"],
                    "after_captain": after_row["captain"]["name"],
                    "before_formation": before_row["formation"],
                    "after_formation": after_row["formation"],
                }
            )
        before_top = None
        after_top = None
        transfer_changed = False
        if include_transfer_scan:
            before_transfers = recommend_web_transfers(
                owned_fpl_ids,
                bank_tenths=bank_tenths,
                free_transfers=free_transfers,
                selling_prices=selling_prices,
                top_n=3,
                release_path=before_path,
            )
            after_transfers = recommend_web_transfers(
                owned_fpl_ids,
                bank_tenths=bank_tenths,
                free_transfers=free_transfers,
                selling_prices=selling_prices,
                top_n=3,
                release_path=after_path,
            )
            before_top = _top_transfer(before_transfers)
            after_top = _top_transfer(after_transfers)
            transfer_changed = (
                None
                if before_top is None
                else (before_top["out_fpl_id"], before_top["in_fpl_id"])
            ) != (
                None
                if after_top is None
                else (after_top["out_fpl_id"], after_top["in_fpl_id"])
            )
        decision_changed = decision_changed or transfer_changed
        decisions.update(
            {
                "lineups": lineup_changes,
                "squad_rating": {
                    "before_cumulative_xpts": before_lineups["cumulative_xpts"],
                    "after_cumulative_xpts": after_lineups["cumulative_xpts"],
                    "delta": (
                        after_lineups["cumulative_xpts"]
                        - before_lineups["cumulative_xpts"]
                    ),
                    "before_percentile": _overall_percentile(before_lineups),
                    "after_percentile": _overall_percentile(after_lineups),
                    "before_benchmark_id": before_lineups["squad_rating"]["benchmark"].get(
                        "benchmark_id"
                    ),
                    "after_benchmark_id": after_lineups["squad_rating"]["benchmark"].get(
                        "benchmark_id"
                    ),
                },
                "transfer": {
                    "evaluated": include_transfer_scan,
                    "changed": transfer_changed,
                    "before": before_top,
                    "after": after_top,
                },
            }
        )

    report = {
        "schema_version": "release_drift_v1",
        "before": {
            "path": str(Path(before_path)),
            "source_ingestion_run_id": before_horizon.source_ingestion_run_id,
            "health": before_health,
            "release_id": before_release_id,
        },
        "after": {
            "path": str(Path(after_path)),
            "source_ingestion_run_id": after_horizon.source_ingestion_run_id,
            "health": after_health,
            "release_id": after_release_id,
        },
        "thresholds": {
            "xpts": xpts_threshold,
            "appearance_probability": appearance_threshold,
            "include_transfer_scan": include_transfer_scan,
        },
        "players": {
            "material_change_count": len(player_changes),
            "added_fpl_ids": added,
            "removed_fpl_ids": removed,
            "changes": player_changes,
        },
        "decisions": decisions,
        "summary": {
            "material_change": bool(player_changes or added or removed or decision_changed),
            "decision_changed": decision_changed,
        },
    }
    return ReleaseDriftResult(report=report)
