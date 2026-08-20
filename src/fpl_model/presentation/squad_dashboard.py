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
    :root { color-scheme: dark; --bg:#120018; --panel:#25002f; --panel2:#340043; --text:#fff; --muted:#c8b7cf; --line:#593066; --accent:#00f5d4; --accent2:#ffdf59; --bad:#ff6b8a; --ok:#69f0ae; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:Inter,Segoe UI,Arial,sans-serif; background:linear-gradient(145deg,#120018,#25002f 60%,#001c25); color:var(--text); }
    main { max-width:1180px; margin:auto; padding:24px; }
    h1,h2,h3,p { margin-top:0; } h1 { font-size:clamp(1.5rem,4vw,2.4rem); margin-bottom:6px; } h2 { font-size:1.15rem; }
    .muted { color:var(--muted); } .small { font-size:.84rem; } .mono { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; }
    .top { display:flex; justify-content:space-between; align-items:flex-start; gap:20px; flex-wrap:wrap; margin-bottom:20px; }
    .tabs { display:flex; gap:8px; flex-wrap:wrap; }
    button { border:1px solid var(--line); background:var(--panel); color:var(--text); border-radius:999px; padding:9px 14px; cursor:pointer; font:inherit; }
    button[aria-pressed="true"] { background:var(--accent); color:#002c2a; border-color:var(--accent); font-weight:700; }
    button:focus-visible { outline:3px solid var(--accent2); outline-offset:2px; }
    .alert { border-left:4px solid var(--accent2); background:rgba(255,223,89,.1); padding:12px 14px; margin:0 0 18px; border-radius:8px; }
    .alert.bad { border-color:var(--bad); background:rgba(255,107,138,.1); }
    .stats { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin-bottom:20px; }
    .stat,.section { background:rgba(37,0,47,.88); border:1px solid var(--line); border-radius:14px; }
    .stat { padding:14px; } .stat b { display:block; font-size:1.45rem; margin-top:4px; }
    .section { padding:18px; margin-bottom:16px; }
    .transaction { display:grid; grid-template-columns:1fr auto 1fr; gap:14px; align-items:center; }
    .names { display:flex; gap:7px; flex-wrap:wrap; } .pill { background:var(--panel2); border:1px solid var(--line); padding:6px 9px; border-radius:999px; }
    .arrow { color:var(--accent); font-size:1.4rem; }
    .players { display:grid; grid-template-columns:repeat(auto-fill,minmax(175px,1fr)); gap:10px; }
    .player { background:var(--panel2); border:1px solid var(--line); border-radius:11px; padding:11px; min-height:126px; }
    .player.gap { border-color:var(--bad); } .player-head { display:flex; justify-content:space-between; gap:8px; }
    .badge { color:#001d1a; background:var(--accent); padding:2px 6px; border-radius:999px; font-size:.72rem; white-space:nowrap; }
    .badge.vice { color:#2e2200; background:var(--accent2); }
    .xpts { display:grid; grid-template-columns:repeat(3,1fr); gap:5px; margin-top:10px; }
    .xpts span { text-align:center; padding:5px 2px; background:rgba(0,0,0,.18); border-radius:6px; font-size:.82rem; }
    .gap-text { color:var(--bad); margin-top:8px; font-size:.78rem; overflow-wrap:anywhere; }
    .evidence-text { color:var(--accent2); margin-top:8px; font-size:.78rem; }
    .group-label { margin:16px 0 8px; color:var(--accent); font-size:.86rem; text-transform:uppercase; letter-spacing:.08em; }
    table { width:100%; border-collapse:collapse; } th,td { text-align:left; padding:9px 7px; border-bottom:1px solid var(--line); } th { color:var(--muted); font-size:.8rem; } td.num,th.num { text-align:right; font-variant-numeric:tabular-nums; }
    .ok { color:var(--ok); } .bad-text { color:var(--bad); }
    footer { color:var(--muted); font-size:.78rem; padding:8px 2px 28px; }
    @media (max-width:760px) { main { padding:15px; } .stats { grid-template-columns:repeat(2,1fr); } .transaction { grid-template-columns:1fr; } .arrow { transform:rotate(90deg); } }
    @media (max-width:430px) { .stats { grid-template-columns:1fr; } .players { grid-template-columns:1fr; } }
  </style>
</head>
<body>
<main>
  <div class="top">
    <div><h1>FPL squad scenarios</h1><p class="muted" id="subtitle"></p></div>
    <div class="tabs" id="scenario-tabs" aria-label="Squad scenarios"></div>
  </div>
  <div id="alerts"></div>
  <div class="stats" id="stats"></div>
  <section class="section" id="transactions"></section>
  <section class="section"><h2>Current selection and projection coverage</h2><div id="squad"></div></section>
  <section class="section"><h2>Covered-player projection comparison</h2><div style="overflow-x:auto"><table id="projection-table"></table></div></section>
  <footer id="footer"></footer>
</main>
<script>
const DATA = __DATA__;
let active = 0;
const money = n => `£${(n/10).toFixed(1)}m`;
const pts = n => n == null ? 'gap' : Number(n).toFixed(2);
const esc = value => String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
function renderTabs(){
  const tabs=document.getElementById('scenario-tabs'); tabs.innerHTML='';
  DATA.scenarios.forEach((s,i)=>{ const b=document.createElement('button'); b.type='button'; b.textContent=s.label; b.setAttribute('aria-pressed',i===active); b.onclick=()=>{active=i;render();}; tabs.appendChild(b); });
}
function playerCard(p){
  const tags=[]; if(p.is_captain) tags.push('<span class="badge">C</span>'); if(p.is_vice_captain) tags.push('<span class="badge vice">V</span>');
  const gws=DATA.model_runs.map(r=>`<span>GW${r.gameweek}<br><b>${pts(p.xpts[String(r.gameweek)])}</b></span>`).join('');
  const flags=p.gap_flags.length ? `<div class="gap-text">${p.gap_flags.map(esc).join(' · ')}</div>` : '';
  const evidence=p.research_evidence ? `<div class="evidence-text">Research evidence: ${esc(p.research_evidence.source_competition)} · translation pending</div>` : '';
  return `<article class="player ${p.gap_flags.length?'gap':''}"><div class="player-head"><b>${esc(p.name)}</b><span>${tags.join('')}</span></div><div class="muted small">${esc(p.team)} · ${esc(p.position)} · ${money(p.current_price_tenths)}</div><div class="xpts">${gws}</div>${flags}${evidence}</article>`;
}
function render(){
  renderTabs(); const s=DATA.scenarios[active];
  document.getElementById('subtitle').textContent=`Public snapshot ${DATA.source_captured_at} · model as-of ${DATA.model_as_of}`;
  const alerts=[];
  if(!DATA.source_snapshot_matches_model) alerts.push(`<div class="alert">The public squad snapshot is newer than the frozen model source. Rebuild projections before treating this as a deadline-final recommendation.</div>`);
  if(s.gaps.length) alerts.push(`<div class="alert bad">${s.gaps.length} projection gaps: ${s.gaps.map(esc).join(', ')}. Research evidence captured for ${s.gap_evidence_count}; production translation remains pending. Lineup optimization is withheld.</div>`);
  if(!s.rules_legal) alerts.push(`<div class="alert bad">Squad validation: ${s.rule_reasons.map(esc).join('; ')}</div>`);
  document.getElementById('alerts').innerHTML=alerts.join('');
  const cumulative=Object.values(s.covered_owned_xpts).reduce((a,b)=>a+Number(b),0);
  document.getElementById('stats').innerHTML=`
    <div class="stat"><span class="muted small">Squad cost</span><b>${money(s.squad_cost_tenths)}</b><span class="small">Bank ${money(s.bank_tenths)}</span></div>
    <div class="stat"><span class="muted small">Projection coverage</span><b>${s.coverage}/15</b><span class="small">complete across all 3 GWs</span></div>
    <div class="stat"><span class="muted small">Covered-player xPts</span><b>${cumulative.toFixed(2)}</b><span class="small">not XI score</span></div>
    <div class="stat"><span class="muted small">Planning state</span><b class="${s.recommendation?'ok':'bad-text'}">${s.recommendation?'Ready':'Blocked'}</b><span class="small">${s.selection_complete?'selection captured':'selection not captured'}</span></div>`;
  const tx=s.transactions_from_first; const delta=Object.values(tx.covered_owned_xpts_delta).reduce((a,b)=>a+Number(b),0);
  document.getElementById('transactions').innerHTML=`<h2>Changes from ${esc(DATA.scenarios[0].label)}</h2><div class="transaction"><div><div class="muted small">Out</div><div class="names">${tx.out.length?tx.out.map(n=>`<span class="pill">${esc(n)}</span>`).join(''):'<span class="muted">None</span>'}</div></div><div class="arrow" aria-hidden="true">→</div><div><div class="muted small">In</div><div class="names">${tx.in.length?tx.in.map(n=>`<span class="pill">${esc(n)}</span>`).join(''):'<span class="muted">None</span>'}</div></div></div><p class="small muted" style="margin:12px 0 0">Covered-player GW1–3 delta: <b class="${delta>=0?'ok':'bad-text'}">${delta>=0?'+':''}${delta.toFixed(2)} xPts</b></p>`;
  const starters=s.players.filter(p=>p.squad_position!=null&&p.squad_position<=11); const bench=s.players.filter(p=>p.squad_position>11); const unassigned=s.players.filter(p=>p.squad_position==null);
  let squadHtml=''; if(starters.length) squadHtml+=`<div class="group-label">Starting XI</div><div class="players">${starters.map(playerCard).join('')}</div>`; if(bench.length) squadHtml+=`<div class="group-label">Bench order</div><div class="players">${bench.map(playerCard).join('')}</div>`; if(unassigned.length) squadHtml+=`<div class="group-label">Squad — lineup not captured</div><div class="players">${unassigned.map(playerCard).join('')}</div>`;
  document.getElementById('squad').innerHTML=squadHtml;
  const header=`<thead><tr><th>Player</th><th>Club</th>${DATA.model_runs.map(r=>`<th class="num">GW${r.gameweek}</th>`).join('')}<th>Status</th></tr></thead>`;
  const body=s.players.map(p=>`<tr><td>${esc(p.name)}</td><td>${esc(p.team)}</td>${DATA.model_runs.map(r=>`<td class="num">${pts(p.xpts[String(r.gameweek)])}</td>`).join('')}<td class="${p.gap_flags.length?'bad-text':'ok'}">${p.gap_flags.length?'Gap':'Covered'}</td></tr>`).join('');
  document.getElementById('projection-table').innerHTML=header+`<tbody>${body}</tbody>`;
  document.getElementById('footer').textContent=`Model ${DATA.model_version}. ${DATA.limitations.join(' ')}`;
}
render();
</script>
</body>
</html>
"""
