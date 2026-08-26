"""Build a browser-readable squad scenario and projection dashboard."""

from __future__ import annotations

import html
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import duckdb
import pandas as pd

from fpl_model.decision.lineup import PlayerGameweekProjection, recommend_lineup
from fpl_model.decision.lineup_store import _flags
from fpl_model.decision.rolling_store import aggregate_projections
from fpl_model.decision.squad import POSITION_COUNTS, SquadPlayer, validate_squad

INITIAL_BUDGET_TENTHS = 1_000
_CHIPS_AVAILABLE = {
    "wildcard": "available",
    "free_hit": "available",
    "bench_boost": "available",
    "triple_captain": "available",
}


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    label: str
    csv_path: Path
    bank_tenths: int = 0


def _tenths(value: object, field: str, *, allow_zero: bool = False) -> int:
    try:
        numeric = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a price with at most one decimal place") from exc
    tenths = numeric * 10
    minimum = 0 if allow_zero else 1
    if not numeric.is_finite() or tenths != tenths.to_integral_value() or tenths < minimum:
        raise ValueError(f"{field} must be a valid price with at most one decimal place")
    return int(tenths)


def _optional_boolean(value: object, field: str) -> bool | None:
    if pd.isna(value) or str(value).strip() == "":
        return None
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError(f"{field} must be true, false, or blank")


def _optional_position(value: object) -> int | None:
    if pd.isna(value) or str(value).strip() == "":
        return None
    numeric = Decimal(str(value))
    if numeric != numeric.to_integral_value() or not 1 <= numeric <= 15:
        raise ValueError("squad_position must be an integer from 1 through 15 or blank")
    return int(numeric)


def _load_scenario_rows(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    frame = pd.read_csv(path)
    required = {"fpl_id", "purchase_price", "selling_price"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"scenario {path} missing columns: {', '.join(sorted(missing))}")
    if len(frame) != 15:
        raise ValueError(f"scenario {path} must contain exactly 15 players")
    if frame["fpl_id"].duplicated().any():
        raise ValueError(f"scenario {path} contains duplicate fpl_id values")

    result = []
    for raw in frame.to_dict(orient="records"):
        fpl_id = int(raw["fpl_id"])
        if fpl_id <= 0:
            raise ValueError("fpl_id must be positive")
        result.append(
            {
                "fpl_id": fpl_id,
                "purchase_price_tenths": _tenths(raw["purchase_price"], "purchase_price"),
                "selling_price_tenths": _tenths(raw["selling_price"], "selling_price"),
                "squad_position": _optional_position(raw.get("squad_position")),
                "is_captain": _optional_boolean(raw.get("is_captain"), "is_captain"),
                "is_vice_captain": _optional_boolean(
                    raw.get("is_vice_captain"), "is_vice_captain"
                ),
            }
        )
    return result


def _selection_complete(rows: list[dict[str, object]]) -> bool:
    fields = ("squad_position", "is_captain", "is_vice_captain")
    filled = [all(row[field] is not None for field in fields) for row in rows]
    if any(filled) and not all(filled):
        raise ValueError("scenario selection fields must be either all completed or all blank")
    return all(filled)


def _model_metadata(
    connection: duckdb.DuckDBPyConnection,
    model_run_ids: dict[int, str],
) -> tuple[list[dict[str, object]], str, str, datetime]:
    if len(model_run_ids) != 3:
        raise ValueError("dashboard requires exactly three model runs")
    gameweeks = sorted(model_run_ids)
    if gameweeks != list(range(gameweeks[0], gameweeks[0] + 3)):
        raise ValueError("dashboard model runs must cover three consecutive Gameweeks")
    rows = []
    for gameweek in gameweeks:
        model_run_id = model_run_ids[gameweek]
        row = connection.execute(
            """
            SELECT target_gameweek, as_of, deadline, model_version,
                   source_ingestion_run_id, status
            FROM model_run WHERE model_run_id = ?
            """,
            [model_run_id],
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown model_run_id: {model_run_id}")
        target, as_of, deadline, version, source_run, status = row
        if int(target) != gameweek or status != "completed":
            raise ValueError(f"model run {model_run_id} is not a completed GW{gameweek} run")
        rows.append(
            {
                "gameweek": gameweek,
                "model_run_id": model_run_id,
                "as_of": as_of.isoformat(),
                "deadline": deadline.isoformat(),
                "model_version": str(version),
                "source_ingestion_run_id": str(source_run),
            }
        )
    sources = {str(row["source_ingestion_run_id"]) for row in rows}
    versions = {str(row["model_version"]) for row in rows}
    as_of_values = {str(row["as_of"]) for row in rows}
    if len(sources) != 1 or len(versions) != 1 or len(as_of_values) != 1:
        raise ValueError("dashboard model runs must share source, version, and frozen as_of")
    return rows, sources.pop(), versions.pop(), datetime.fromisoformat(as_of_values.pop())


def _official_players(
    connection: duckdb.DuckDBPyConnection,
    source_ingestion_run_id: str,
) -> tuple[dict[int, dict[str, object]], datetime]:
    run = connection.execute(
        "SELECT captured_at, status FROM ingestion_run WHERE ingestion_run_id = ?",
        [source_ingestion_run_id],
    ).fetchone()
    if run is None or run[1] != "completed":
        raise ValueError("dashboard source must identify a completed official snapshot")
    rows = connection.execute(
        """
        SELECT p.fpl_id, p.player_code, p.web_name, t.short_name,
               p.team_id, p.fpl_position, p.price, p.fpl_status,
               s.can_select, s.can_transact, s.removed, p.news
        FROM player_snapshot AS p
        JOIN team_snapshot AS t USING (ingestion_run_id, team_id)
        JOIN player_status_snapshot AS s USING (ingestion_run_id, fpl_id)
        WHERE p.ingestion_run_id = ?
        ORDER BY p.fpl_id
        """,
        [source_ingestion_run_id],
    ).fetchall()
    return (
        {
            int(row[0]): {
                "fpl_id": int(row[0]),
                "player_code": None if row[1] is None else int(row[1]),
                "name": str(row[2]),
                "team": str(row[3]),
                "team_id": int(row[4]),
                "position": str(row[5]),
                "current_price_tenths": _tenths(row[6], "current_price"),
                "fpl_status": str(row[7]),
                "can_select": bool(row[8]),
                "can_transact": bool(row[9]),
                "removed": bool(row[10]),
                "news": "" if row[11] is None else str(row[11]),
            }
            for row in rows
        },
        run[0],
    )


def _projection_maps(
    connection: duckdb.DuckDBPyConnection,
    model_run_ids: dict[int, str],
) -> tuple[
    dict[int, dict[int, tuple[float, float | None, tuple[str, ...]]]],
    dict[int, dict[int, tuple[str, ...]]],
]:
    projections = {
        gameweek: aggregate_projections(connection, model_run_id)
        for gameweek, model_run_id in model_run_ids.items()
    }
    gaps = {}
    for gameweek, model_run_id in model_run_ids.items():
        rows = connection.execute(
            """
            SELECT player_code, data_quality_flags
            FROM baseline_projection_gap
            WHERE model_run_id = ? AND player_code IS NOT NULL
            """,
            [model_run_id],
        ).fetchall()
        gaps[gameweek] = {int(code): tuple(sorted(_flags(flags))) for code, flags in rows}
    return projections, gaps


def _research_evidence_map(
    connection: duckdb.DuckDBPyConnection,
    *,
    source_ingestion_run_id: str,
    target_gameweek: int,
) -> dict[int, dict[str, object]]:
    rows = connection.execute(
        """
        SELECT e.player_code, e.comparability_class, e.source_competition,
               e.sample_minutes, e.sample_starts, e.source_reference,
               e.data_quality_flags
        FROM player_rate_evidence AS e
        JOIN player_rate_evidence_import_run AS r USING (evidence_import_run_id)
        WHERE e.source_ingestion_run_id = ? AND r.target_gameweek = ?
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY e.player_code
            ORDER BY r.imported_at DESC, e.evidence_import_run_id DESC
        ) = 1
        """,
        [source_ingestion_run_id, target_gameweek],
    ).fetchall()
    return {
        int(code): {
            "comparability_class": str(comparability),
            "source_competition": "" if competition is None else str(competition),
            "sample_minutes": None if minutes is None else int(minutes),
            "sample_starts": None if starts is None else int(starts),
            "source_reference": str(reference),
            "data_quality_flags": sorted(_flags(flags)),
        }
        for code, comparability, competition, minutes, starts, reference, flags in rows
    }


def _squad_rules(players: list[dict[str, object]], bank_tenths: int) -> tuple[bool, list[str]]:
    reasons = []
    position_counts = Counter(str(player["position"]) for player in players)
    if dict(position_counts) != POSITION_COUNTS:
        reasons.append(f"position counts are {dict(position_counts)}, expected {POSITION_COUNTS}")
    club_counts = Counter(int(player["team_id"]) for player in players)
    if any(count > 3 for count in club_counts.values()):
        reasons.append("more than three players from one club")
    cost = sum(int(player["current_price_tenths"]) for player in players)
    if cost + bank_tenths > INITIAL_BUDGET_TENTHS:
        reasons.append("current squad cost plus bank exceeds £100.0m")
    if any(not bool(player["can_select"]) for player in players):
        reasons.append("one or more players are not selectable")
    return not reasons, reasons


def _recommendations(
    players: list[dict[str, object]],
    projections: dict[int, dict[int, tuple[float, float | None, tuple[str, ...]]]],
    bank_tenths: int,
) -> dict[str, object] | None:
    if any(player["squad_position"] is None for player in players):
        return None
    if any(
        player["player_code"] is None
        or any(int(player["player_code"]) not in rows for rows in projections.values())
        for player in players
    ):
        return None
    squad_players = tuple(
        SquadPlayer(
            fpl_id=int(player["fpl_id"]),
            player_code=int(player["player_code"]),
            player_name=str(player["name"]),
            team_id=int(player["team_id"]),
            position=str(player["position"]),
            current_price_tenths=int(player["current_price_tenths"]),
            purchase_price_tenths=int(player["purchase_price_tenths"]),
            selling_price_tenths=int(player["selling_price_tenths"]),
            squad_position=int(player["squad_position"]),
            is_captain=bool(player["is_captain"]),
            is_vice_captain=bool(player["is_vice_captain"]),
        )
        for player in players
    )
    squad = validate_squad(
        squad_players,
        bank_tenths=bank_tenths,
        free_transfers=None,
        unlimited_transfers=True,
        chip_period=1,
        chip_states=_CHIPS_AVAILABLE,
    )
    gameweeks = []
    for gameweek in sorted(projections):
        rows = projections[gameweek]
        lineup = recommend_lineup(
            squad,
            tuple(
                PlayerGameweekProjection(
                    fpl_id=player.fpl_id,
                    expected_points=rows[int(player.player_code)][0],
                    uncertainty=rows[int(player.player_code)][1],
                    data_quality_flags=rows[int(player.player_code)][2],
                    appearance_probability=rows[int(player.player_code)][3],
                )
                for player in squad.players
            ),
        )
        gameweeks.append(
            {
                "gameweek": gameweek,
                "formation": lineup.formation,
                "total_xpts": lineup.total_xpts,
                "captain": lineup.captain.player_name,
                "vice_captain": lineup.vice_captain.player_name,
                "starters": [player.fpl_id for player in lineup.starters],
                "bench": [
                    lineup.bench_goalkeeper.fpl_id,
                    *(player.fpl_id for player in lineup.outfield_bench_order),
                ],
            }
        )
    return {
        "cumulative_xpts": sum(float(row["total_xpts"]) for row in gameweeks),
        "gameweeks": gameweeks,
    }


def build_squad_dashboard_data(
    connection: duckdb.DuckDBPyConnection,
    *,
    scenarios: tuple[ScenarioSpec, ...],
    model_run_ids: dict[int, str],
    source_ingestion_run_id: str,
) -> dict[str, object]:
    """Reconcile scenario CSVs and projection coverage into serializable dashboard data."""
    if not scenarios:
        raise ValueError("at least one scenario is required")
    labels = [scenario.label.strip() for scenario in scenarios]
    if any(not label for label in labels) or len(set(labels)) != len(labels):
        raise ValueError("scenario labels must be non-blank and unique")
    if any(scenario.bank_tenths < 0 for scenario in scenarios):
        raise ValueError("scenario bank must be non-negative")

    model_rows, model_source_run, model_version, model_as_of = _model_metadata(
        connection, model_run_ids
    )
    official, source_captured_at = _official_players(connection, source_ingestion_run_id)
    projection_maps, gap_maps = _projection_maps(connection, model_run_ids)
    evidence_map = _research_evidence_map(
        connection,
        source_ingestion_run_id=source_ingestion_run_id,
        target_gameweek=min(model_run_ids),
    )
    scenario_rows = []
    for spec in scenarios:
        raw_rows = _load_scenario_rows(spec.csv_path)
        selection_complete = _selection_complete(raw_rows)
        players = []
        for raw in raw_rows:
            fpl_id = int(raw["fpl_id"])
            identity = official.get(fpl_id)
            if identity is None:
                raise ValueError(
                    f"scenario {spec.label} player fpl_id={fpl_id} is absent from official snapshot"
                )
            player = {
                **identity,
                **raw,
                "xpts": {},
                "gap_flags": [],
                "research_evidence": (
                    None if identity["player_code"] is None else evidence_map.get(
                        int(identity["player_code"])
                    )
                ),
            }
            code = player["player_code"]
            player_flags: set[str] = set()
            for gameweek in sorted(model_run_ids):
                projection = None if code is None else projection_maps[gameweek].get(int(code))
                player["xpts"][str(gameweek)] = None if projection is None else projection[0]
                if projection is None and code is not None:
                    player_flags.update(gap_maps[gameweek].get(int(code), ()))
            player["gap_flags"] = sorted(player_flags)
            players.append(player)

        players.sort(
            key=lambda player: (
                player["squad_position"] is None,
                player["squad_position"] or 99,
                player["position"],
                player["name"],
            )
        )
        rules_legal, rule_reasons = _squad_rules(players, spec.bank_tenths)
        covered = [
            player
            for player in players
            if all(player["xpts"][str(gameweek)] is not None for gameweek in model_run_ids)
        ]
        covered_xpts = {
            str(gameweek): sum(float(player["xpts"][str(gameweek)]) for player in covered)
            for gameweek in sorted(model_run_ids)
        }
        scenario_rows.append(
            {
                "label": spec.label,
                "source_path": str(spec.csv_path.resolve()),
                "selection_complete": selection_complete,
                "rules_legal": rules_legal,
                "rule_reasons": rule_reasons,
                "bank_tenths": spec.bank_tenths,
                "squad_cost_tenths": sum(
                    int(player["current_price_tenths"]) for player in players
                ),
                "coverage": len(covered),
                "gaps": [player["name"] for player in players if player not in covered],
                "gap_evidence_count": sum(
                    player not in covered and player["research_evidence"] is not None
                    for player in players
                ),
                "covered_owned_xpts": covered_xpts,
                "players": players,
                "recommendation": (
                    _recommendations(players, projection_maps, spec.bank_tenths)
                    if selection_complete and rules_legal
                    else None
                ),
            }
        )

    baseline_ids = {int(player["fpl_id"]) for player in scenario_rows[0]["players"]}
    baseline_names = {
        int(player["fpl_id"]): str(player["name"]) for player in scenario_rows[0]["players"]
    }
    baseline_xpts = scenario_rows[0]["covered_owned_xpts"]
    for scenario in scenario_rows:
        ids = {int(player["fpl_id"]) for player in scenario["players"]}
        names = {int(player["fpl_id"]): str(player["name"]) for player in scenario["players"]}
        scenario["transactions_from_first"] = {
            "out": [baseline_names[value] for value in sorted(baseline_ids - ids)],
            "in": [names[value] for value in sorted(ids - baseline_ids)],
            "covered_owned_xpts_delta": {
                str(gameweek): float(scenario["covered_owned_xpts"][str(gameweek)])
                - float(baseline_xpts[str(gameweek)])
                for gameweek in sorted(model_run_ids)
            },
        }

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_ingestion_run_id": source_ingestion_run_id,
        "source_captured_at": source_captured_at.isoformat(),
        "model_source_ingestion_run_id": model_source_run,
        "model_version": model_version,
        "model_as_of": model_as_of.isoformat(),
        "source_snapshot_matches_model": source_ingestion_run_id == model_source_run,
        "model_runs": model_rows,
        "scenarios": scenario_rows,
        "limitations": [
            "Covered-owned xPts sums every covered squad player and is not a starting-XI score.",
            "A lineup recommendation is withheld until all 15 players have projections.",
            "Scenario transaction differences do not model future price changes or hits.",
        ],
    }


def render_squad_dashboard(data: dict[str, object], output_path: str | Path) -> Path:
    """Render one dependency-free interactive HTML document."""
    serialized = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    title = html.escape("FPL Squad Scenario Dashboard")
    document = _HTML_TEMPLATE.replace("__TITLE__", title).replace("__DATA__", serialized)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    return path.resolve()


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <style>
    :root{color-scheme:dark;--ink:#fff;--muted:#c9bdd1;--page:#100015;--panel:#21002a;--panel2:#2d0736;--line:#563260;--brand:#00ffbf;--yellow:#ffe45c;--bad:#ff6689;--ok:#6ff5ae;--pitch:#087b50;--pitch2:#056641}
    *{box-sizing:border-box}body{margin:0;min-width:320px;font-family:Inter,Segoe UI,Arial,sans-serif;color:var(--ink);background:radial-gradient(circle at 85% 0,#352047 0,transparent 31rem),linear-gradient(145deg,#100015,#1c0224 65%,#06252b)}
    button{font:inherit}button:focus-visible,summary:focus-visible{outline:3px solid var(--yellow);outline-offset:3px}main{width:min(1220px,100%);margin:auto;padding:24px}h1,h2,p{margin-top:0}h1{margin-bottom:5px;font-size:clamp(1.7rem,4vw,2.55rem);line-height:1.05;letter-spacing:-.035em}h2{margin-bottom:4px;font-size:1.08rem}.muted{color:var(--muted)}.small{font-size:.82rem}.eyebrow{color:var(--brand);font-size:.73rem;font-weight:800;letter-spacing:.12em;text-transform:uppercase}
    .top{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:17px}.tabs{display:flex;gap:7px;flex-wrap:wrap}.tab{border:1px solid var(--line);border-radius:999px;padding:8px 13px;color:var(--ink);background:rgba(33,0,42,.85);cursor:pointer}.tab[aria-selected="true"]{border-color:var(--brand);color:#00261c;background:var(--brand);font-weight:800}
    .alert{border:1px solid rgba(255,228,92,.35);border-left:4px solid var(--yellow);border-radius:10px;margin:0 0 12px;padding:11px 13px;color:#fff7c7;background:rgba(255,228,92,.08)}.alert.bad{border-color:rgba(255,102,137,.4);border-left-color:var(--bad);color:#ffd6df;background:rgba(255,102,137,.09)}
    .stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:9px;margin:0 0 14px}.stat{min-height:86px;padding:13px 14px;border:1px solid var(--line);border-radius:12px;background:rgba(33,0,42,.9)}.stat strong{display:block;margin:4px 0 2px;font-size:1.35rem;line-height:1.1;font-variant-numeric:tabular-nums}.stat .label{color:var(--muted);font-size:.75rem}
    .team-card,.section{border:1px solid var(--line);border-radius:16px;background:rgba(33,0,42,.94);box-shadow:0 20px 50px rgba(0,0,0,.18)}.team-card{overflow:hidden;margin-bottom:14px}.team-toolbar{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:15px 17px}.gw-switcher{display:flex;gap:5px;padding:4px;border-radius:999px;background:#15001b}.gw-switcher .tab{min-width:54px;padding:6px 11px;border-color:transparent;background:transparent}
    .pitch-wrap{padding:0 14px 14px}.pitch{position:relative;min-height:610px;overflow:hidden;display:grid;grid-template-rows:repeat(4,1fr);align-items:center;gap:1px;padding:28px 14px;border:2px solid rgba(255,255,255,.5);border-radius:12px;background:repeating-linear-gradient(90deg,var(--pitch) 0,var(--pitch) 12.5%,var(--pitch2) 12.5%,var(--pitch2) 25%);isolation:isolate}.pitch:before{content:"";position:absolute;z-index:-1;inset:50% 0 auto;border-top:2px solid rgba(255,255,255,.45)}.pitch:after{content:"";position:absolute;z-index:-1;width:128px;aspect-ratio:1;left:50%;top:50%;border:2px solid rgba(255,255,255,.45);border-radius:50%;transform:translate(-50%,-50%)}.goal-box{position:absolute;z-index:-1;left:50%;width:40%;height:12%;border:2px solid rgba(255,255,255,.42);transform:translateX(-50%);pointer-events:none}.goal-box.top{top:-2px;border-top:0}.goal-box.bottom{bottom:-2px;border-bottom:0}
    .position-row{display:grid;grid-template-columns:repeat(var(--slots),minmax(64px,126px));justify-content:space-evenly;align-items:center;gap:10px}.player-card{position:relative;width:100%;max-width:126px;min-width:0;border:0;padding:0;color:var(--ink);background:transparent;cursor:pointer;filter:drop-shadow(0 5px 5px rgba(0,0,0,.26));transition:transform .15s ease}.player-card:hover{transform:translateY(-2px)}.shirt{position:relative;width:45px;height:47px;margin:0 auto -4px;border:2px solid rgba(255,255,255,.72);border-radius:8px 8px 13px 13px;background:linear-gradient(135deg,var(--shirt-a),var(--shirt-a) 49%,var(--shirt-b) 50%);clip-path:polygon(20% 0,37% 7%,63% 7%,80% 0,100% 20%,84% 37%,78% 29%,78% 100%,22% 100%,22% 29%,16% 37%,0 20%)}.shirt span{position:absolute;inset:14px 6px auto;overflow:hidden;color:#fff;font-size:.66rem;font-weight:900;text-align:center;text-shadow:0 1px 3px #000}.captain,.risk-dot{position:absolute;z-index:2;top:-7px;display:grid;place-items:center;width:23px;height:23px;border:2px solid #fff;border-radius:50%;font-size:.68rem;font-weight:900}.captain{right:7px;color:#14001a;background:var(--yellow)}.captain.vice{background:#fff}.risk-dot{left:7px;color:#fff;background:var(--bad)}
    .nameplate{overflow:hidden;padding:5px 7px 4px;border-radius:7px 7px 0 0;color:#1b0720;background:#fff;font-size:.76rem;font-weight:850;text-overflow:ellipsis;white-space:nowrap}.scoreplate{padding:4px 6px;border-radius:0 0 7px 7px;color:#fff;background:#24102a;font-size:.72rem}.scoreplate b{color:var(--brand);font-size:.82rem;font-variant-numeric:tabular-nums}.player-card.gap .scoreplate b{color:#ffd0db}
    .bench{margin:0 14px 14px;padding:13px;border:1px solid var(--line);border-radius:12px;background:#18001f}.bench-head{display:flex;justify-content:space-between;gap:10px;margin-bottom:10px}.bench-grid{display:grid;grid-template-columns:repeat(4,minmax(70px,126px));justify-content:space-evenly;gap:10px}.bench-slot{position:relative;padding-top:18px}.bench-number{position:absolute;z-index:3;top:0;left:50%;transform:translateX(-50%);padding:2px 7px;border-radius:999px;color:#211027;background:#d9cde0;font-size:.64rem;font-weight:900}.empty-state{margin:0 14px 14px;padding:25px;border:1px dashed var(--line);border-radius:12px;color:var(--muted);text-align:center}
    .section{margin-bottom:14px;padding:17px}.section-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;margin-bottom:14px}.transaction{display:grid;grid-template-columns:1fr auto 1fr;gap:14px;align-items:center}.names{display:flex;flex-wrap:wrap;gap:7px}.pill{padding:6px 9px;border:1px solid var(--line);border-radius:999px;background:var(--panel2)}.arrow{color:var(--brand);font-size:1.25rem}
    details.audit{margin-bottom:14px;border:1px solid var(--line);border-radius:14px;background:rgba(33,0,42,.9)}details.audit>summary{display:flex;justify-content:space-between;gap:12px;padding:15px 17px;cursor:pointer;list-style:none;font-weight:800}details.audit>summary::-webkit-details-marker{display:none}details.audit>summary:after{content:"+";color:var(--brand);font-size:1.2rem}details.audit[open]>summary:after{content:"−"}.table-wrap{overflow-x:auto;padding:0 17px 16px}table{width:100%;border-collapse:collapse}th,td{padding:9px 7px;border-bottom:1px solid var(--line);text-align:left}th{color:var(--muted);font-size:.76rem}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}.ok{color:var(--ok)}.bad-text{color:var(--bad)}
    dialog{width:min(470px,calc(100% - 28px));padding:0;border:1px solid #785d82;border-radius:16px;color:var(--ink);background:#21002a;box-shadow:0 28px 90px #000}dialog::backdrop{background:rgba(8,0,12,.78);backdrop-filter:blur(3px)}.dialog-head{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;padding:18px;border-bottom:1px solid var(--line)}.close{width:35px;height:35px;flex:0 0 auto;border:1px solid var(--line);border-radius:50%;color:#fff;background:transparent;cursor:pointer}.dialog-body{padding:18px}.detail-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:0 0 14px}.detail-gw{padding:10px;border-radius:9px;text-align:center;background:#15001b}.detail-gw b{display:block;margin-top:3px;font-size:1.05rem}.data-note{margin-top:10px;padding:10px;border-radius:9px;color:#ffd6df;background:rgba(255,102,137,.1);overflow-wrap:anywhere}footer{padding:5px 2px 28px;color:var(--muted);font-size:.76rem}
    @media(max-width:760px){main{padding:14px}.top{display:block}.top .tabs{margin-top:14px}.stats{grid-template-columns:repeat(2,1fr)}.team-toolbar{align-items:flex-start}.pitch-wrap{padding:0 7px 7px}.pitch{min-height:560px;padding-inline:5px}.position-row{gap:5px}.bench{margin:0 7px 7px;padding:10px 5px}.bench-grid{gap:5px}.shirt{width:40px;height:43px}.captain{right:2px}.risk-dot{left:2px}}
    @media(max-width:480px){.team-toolbar{display:block}.gw-switcher{width:max-content;margin-top:11px}.pitch{min-height:530px}.position-row{grid-template-columns:repeat(var(--slots),minmax(54px,82px))}.nameplate{padding-inline:3px;font-size:.66rem}.scoreplate{font-size:.64rem}.bench-grid{grid-template-columns:repeat(4,minmax(54px,82px))}.transaction{grid-template-columns:1fr}.arrow{transform:rotate(90deg)}.detail-grid{grid-template-columns:1fr}}
  </style>
</head>
<body>
<main>
  <div class="top">
    <div><div class="eyebrow">Decision workspace</div><h1>Your squad, on the pitch</h1><p class="muted" id="subtitle"></p></div>
    <div class="tabs" id="scenario-tabs" role="tablist" aria-label="Squad scenarios"></div>
  </div>
  <div id="alerts"></div>
  <div class="stats" id="stats"></div>
  <section class="team-card" aria-labelledby="squad-heading">
    <div class="team-toolbar">
      <div><div class="eyebrow">Selected lineup</div><h2 id="squad-heading">Starting XI</h2><span class="small muted" id="lineup-summary"></span></div>
      <div class="gw-switcher" id="gw-tabs" role="tablist" aria-label="Projection Gameweek"></div>
    </div>
    <div id="squad"></div>
  </section>
  <section class="section" id="transactions"></section>
  <details class="audit"><summary><span>Projection audit</span><span class="small muted">All 15 players · GW horizon</span></summary><div class="table-wrap"><table id="projection-table"></table></div></details>
  <footer id="footer"></footer>
</main>
<dialog id="player-dialog" aria-labelledby="dialog-title"><div class="dialog-head"><div><div class="eyebrow">Player detail</div><h2 id="dialog-title"></h2><div class="small muted" id="dialog-meta"></div></div><button class="close" type="button" aria-label="Close player detail">×</button></div><div class="dialog-body" id="dialog-body"></div></dialog>
<script>
const DATA = __DATA__;let active=0;let activeGw=DATA.model_runs[0].gameweek;
const money=n=>`£${(n/10).toFixed(1)}m`;const pts=n=>n==null?'gap':Number(n).toFixed(2);const esc=value=>String(value).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const shirtPalette=[['#e52333','#94111c'],['#16a3db','#0a4d89'],['#f5c927','#b84b10'],['#fff','#68657a'],['#25a55b','#073d27'],['#7b42b5','#34115a'],['#ff7e20','#9d2609'],['#ec6d9e','#7d1641']];
function renderTabs(){const tabs=document.getElementById('scenario-tabs');tabs.innerHTML='';DATA.scenarios.forEach((s,i)=>{const b=document.createElement('button');b.type='button';b.className='tab';b.role='tab';b.textContent=s.label;b.setAttribute('aria-selected',i===active);b.onclick=()=>{active=i;render()};tabs.appendChild(b)})}
function renderGwTabs(){const tabs=document.getElementById('gw-tabs');tabs.innerHTML='';DATA.model_runs.forEach(r=>{const b=document.createElement('button');b.type='button';b.className='tab';b.role='tab';b.textContent=`GW${r.gameweek}`;b.setAttribute('aria-selected',r.gameweek===activeGw);b.onclick=()=>{activeGw=r.gameweek;render()};tabs.appendChild(b)})}
function playerCard(p){const palette=shirtPalette[(Number(p.team_id)-1)%shirtPalette.length];const captain=p.is_captain?'<span class="captain">C</span>':p.is_vice_captain?'<span class="captain vice">V</span>':'';const value=p.xpts[String(activeGw)];const hasHorizonGap=Object.values(p.xpts).some(row=>row==null);const risk=hasHorizonGap?'<span class="risk-dot" aria-label="Projection data gap">!</span>':'';return `<button type="button" class="player-card ${value==null?'gap':''}" style="--shirt-a:${palette[0]};--shirt-b:${palette[1]}" data-player="${p.fpl_id}" aria-label="${esc(p.name)}, ${value==null?'projection gap':pts(value)+' expected points'}, open details">${captain}${risk}<div class="shirt" aria-hidden="true"><span>${esc(p.team)}</span></div><div class="nameplate">${esc(p.name)}</div><div class="scoreplate"><b>${pts(value)}</b> xPts</div></button>`}
function positionRow(players,position){return players.length?`<div class="position-row" style="--slots:${players.length}" aria-label="${position}">${players.map(playerCard).join('')}</div>`:''}
function openPlayer(fplId){const s=DATA.scenarios[active];const p=s.players.find(row=>Number(row.fpl_id)===Number(fplId));if(!p)return;document.getElementById('dialog-title').textContent=p.name;document.getElementById('dialog-meta').textContent=`${p.team} · ${p.position} · ${money(p.current_price_tenths)}`;const values=DATA.model_runs.map(r=>`<div class="detail-gw"><span class="small muted">GW${r.gameweek}</span><b class="${p.xpts[String(r.gameweek)]==null?'bad-text':''}">${pts(p.xpts[String(r.gameweek)])} xPts</b></div>`).join('');const flags=p.gap_flags.length?`<div class="data-note"><b>Projection unavailable</b><br><span class="small">${p.gap_flags.map(esc).join(' · ')}</span></div>`:'<div class="data-note" style="color:var(--ok);background:rgba(111,245,174,.08)"><b>Projection covered across the horizon</b></div>';const evidence=p.research_evidence?`<div class="data-note" style="color:var(--yellow);background:rgba(255,228,92,.08)"><b>Research evidence only</b><br><span class="small">${esc(p.research_evidence.source_competition||'External competition')} · translation into production rate pending</span></div>`:'';document.getElementById('dialog-body').innerHTML=`<div class="detail-grid">${values}</div>${flags}${evidence}`;document.getElementById('player-dialog').showModal()}
function render(){renderTabs();renderGwTabs();const s=DATA.scenarios[active];document.getElementById('subtitle').textContent=`Public snapshot ${DATA.source_captured_at} · model as-of ${DATA.model_as_of}`;const alerts=[];if(!DATA.source_snapshot_matches_model)alerts.push('<div class="alert">The public squad snapshot is newer than the frozen model source. Rebuild projections before treating this as a deadline-final recommendation.</div>');if(s.gaps.length)alerts.push(`<div class="alert bad"><b>Lineup decision blocked:</b> ${s.gaps.length} player projection${s.gaps.length===1?' is':'s are'} incomplete. Risk markers on the pitch open the underlying evidence.</div>`);if(!s.rules_legal)alerts.push(`<div class="alert bad">Squad validation: ${s.rule_reasons.map(esc).join('; ')}</div>`);document.getElementById('alerts').innerHTML=alerts.join('');
  const cumulative=Object.values(s.covered_owned_xpts).reduce((a,b)=>a+Number(b),0);document.getElementById('stats').innerHTML=`<div class="stat"><span class="label">Squad value</span><strong>${money(s.squad_cost_tenths)}</strong><span class="small muted">${money(s.bank_tenths)} in bank</span></div><div class="stat"><span class="label">3-GW coverage</span><strong>${s.coverage}/15</strong><span class="small muted">complete players</span></div><div class="stat"><span class="label">Covered squad xPts</span><strong>${cumulative.toFixed(2)}</strong><span class="small muted">diagnostic, not XI</span></div><div class="stat"><span class="label">Decision status</span><strong class="${s.recommendation?'ok':'bad-text'}">${s.recommendation?'Ready':'Blocked'}</strong><span class="small muted">${s.selection_complete?'lineup captured':'lineup not captured'}</span></div>`;
  const tx=s.transactions_from_first;const delta=Object.values(tx.covered_owned_xpts_delta).reduce((a,b)=>a+Number(b),0);document.getElementById('transactions').innerHTML=`<div class="section-heading"><div><div class="eyebrow">Scenario movement</div><h2>Changes from ${esc(DATA.scenarios[0].label)}</h2></div><span class="small muted">Covered GW horizon <b class="${delta>=0?'ok':'bad-text'}">${delta>=0?'+':''}${delta.toFixed(2)} xPts</b></span></div><div class="transaction"><div><div class="muted small">Out</div><div class="names">${tx.out.length?tx.out.map(n=>`<span class="pill">${esc(n)}</span>`).join(''):'<span class="muted">None</span>'}</div></div><div class="arrow" aria-hidden="true">→</div><div><div class="muted small">In</div><div class="names">${tx.in.length?tx.in.map(n=>`<span class="pill">${esc(n)}</span>`).join(''):'<span class="muted">None</span>'}</div></div></div>`;
  const starters=s.players.filter(p=>p.squad_position!=null&&p.squad_position<=11);const bench=s.players.filter(p=>p.squad_position>11);const unassigned=s.players.filter(p=>p.squad_position==null);const byPosition=position=>starters.filter(p=>p.position===position);const formation=['DEF','MID','FWD'].map(pos=>byPosition(pos).length).join('-');const coveredStarters=starters.filter(p=>p.xpts[String(activeGw)]!=null);const captain=starters.find(p=>p.is_captain);const xiBase=coveredStarters.reduce((sum,p)=>sum+Number(p.xpts[String(activeGw)]),0);const xiTotal=xiBase+(captain&&captain.xpts[String(activeGw)]!=null?Number(captain.xpts[String(activeGw)]):0);const xiComplete=starters.length===11&&coveredStarters.length===11;document.getElementById('lineup-summary').innerHTML=starters.length?`${formation} · GW${activeGw} XI xPts <b class="${xiComplete?'ok':'bad-text'}">${xiComplete?xiTotal.toFixed(2):'blocked'}</b> · ${coveredStarters.length}/11 covered`:'Lineup order has not been captured';
  let squadHtml='';if(starters.length)squadHtml=`<div class="pitch-wrap"><div class="pitch"><span class="goal-box top"></span>${positionRow(byPosition('GK'),'Goalkeepers')}${positionRow(byPosition('DEF'),'Defenders')}${positionRow(byPosition('MID'),'Midfielders')}${positionRow(byPosition('FWD'),'Forwards')}<span class="goal-box bottom"></span></div></div>`;if(bench.length)squadHtml+=`<div class="bench"><div class="bench-head"><b>Bench</b><span class="small muted">Substitution order</span></div><div class="bench-grid">${bench.map((p,i)=>`<div class="bench-slot"><span class="bench-number">${i===0?'GK':i}</span>${playerCard(p)}</div>`).join('')}</div></div>`;if(unassigned.length)squadHtml+=`<div class="empty-state"><b>Lineup not captured</b><br><span class="small">Add squad positions to place these 15 players on the pitch.</span><div class="bench-grid" style="margin-top:18px">${unassigned.map(playerCard).join('')}</div></div>`;document.getElementById('squad').innerHTML=squadHtml;document.querySelectorAll('[data-player]').forEach(button=>button.addEventListener('click',()=>openPlayer(button.dataset.player)));
  const header=`<thead><tr><th>Player</th><th>Club</th>${DATA.model_runs.map(r=>`<th class="num">GW${r.gameweek}</th>`).join('')}<th>Status</th></tr></thead>`;const body=s.players.map(p=>`<tr><td>${esc(p.name)}</td><td>${esc(p.team)}</td>${DATA.model_runs.map(r=>`<td class="num">${pts(p.xpts[String(r.gameweek)])}</td>`).join('')}<td class="${p.gap_flags.length?'bad-text':'ok'}">${p.gap_flags.length?'Gap':'Covered'}</td></tr>`).join('');document.getElementById('projection-table').innerHTML=header+`<tbody>${body}</tbody>`;document.getElementById('footer').textContent=`Model ${DATA.model_version}. ${DATA.limitations.join(' ')}`}
const dialog=document.getElementById('player-dialog');dialog.querySelector('.close').onclick=()=>dialog.close();dialog.addEventListener('click',event=>{if(event.target===dialog)dialog.close()});render();
</script>
</body>
</html>
"""
