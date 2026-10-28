from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

import fpl_model.webapp.service as webapp_service
from fpl_model.validation.release_drift import compare_web_releases
from fpl_model.webapp.service import (
    ChipPlan,
    CurrentSquadSetup,
    PendingTransfer,
    compare_web_wildcard,
    load_web_bootstrap,
    recommend_web_lineups,
    recommend_web_transfers,
)


def _database(path: Path) -> tuple[int, ...]:
    connection = duckdb.connect(str(path))
    connection.execute(
        """
        CREATE TABLE model_run (
            target_gameweek INTEGER,
            model_run_id VARCHAR,
            source_ingestion_run_id VARCHAR,
            model_version VARCHAR,
            as_of TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            status VARCHAR
        );
        CREATE TABLE player_snapshot (
            ingestion_run_id VARCHAR,
            fpl_id INTEGER,
            player_code BIGINT,
            web_name VARCHAR,
            team_id INTEGER,
            fpl_position VARCHAR,
            price DECIMAL(5,1),
            fpl_status VARCHAR
        );
        CREATE TABLE team_snapshot (
            ingestion_run_id VARCHAR,
            team_id INTEGER,
            short_name VARCHAR
        );
        CREATE TABLE player_fixture_projection (
            model_run_id VARCHAR,
            player_code BIGINT,
            fixture_id INTEGER,
            final_xpts DOUBLE,
            uncertainty DOUBLE,
            data_quality_flags VARCHAR,
            start_probability DOUBLE,
            substitute_appearance_probability DOUBLE,
            opponent_team_id INTEGER,
            is_home BOOLEAN
        );
        """
    )
    source_id = "snapshot_web_test"
    as_of = datetime(2026, 8, 25, 8, tzinfo=UTC)
    for gameweek in (2, 3, 4):
        connection.execute(
            "INSERT INTO model_run VALUES (?, ?, ?, ?, ?, ?, 'completed')",
            [
                gameweek,
                f"run_gw{gameweek}",
                source_id,
                "web_test_v1",
                as_of,
                as_of + timedelta(minutes=gameweek),
            ],
        )
    for team_id in range(1, 7):
        connection.execute(
            "INSERT INTO team_snapshot VALUES (?, ?, ?)",
            [source_id, team_id, f"T{team_id}"],
        )

    positions = ("GK", "GK", *("DEF",) * 5, *("MID",) * 5, *("FWD",) * 3)
    fpl_ids = tuple(range(1, 16))
    for fpl_id, position in zip(fpl_ids, positions, strict=True):
        team_id = ((fpl_id - 1) % 6) + 1
        opponent_team_id = (team_id % 6) + 1  # a distinct team in the same 6-team pool
        connection.execute(
            "INSERT INTO player_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, 'a')",
            [source_id, fpl_id, 10_000 + fpl_id, f"Player {fpl_id}", team_id, position, 5.0],
        )
        for gameweek in (2, 3, 4):
            connection.execute(
                "INSERT INTO player_fixture_projection VALUES "
                "(?, ?, ?, ?, NULL, '[]', 0.9, 0.05, ?, ?)",
                [
                    f"run_gw{gameweek}",
                    10_000 + fpl_id,
                    gameweek * 100 + fpl_id,
                    fpl_id / 2,
                    opponent_team_id,
                    fpl_id % 2 == 0,
                ],
            )
    connection.close()
    return fpl_ids


def test_web_bootstrap_and_lineups_use_latest_compatible_horizon(tmp_path: Path):
    database_path = tmp_path / "web.duckdb"
    fpl_ids = _database(database_path)

    bootstrap = load_web_bootstrap(database_path)
    result = recommend_web_lineups(fpl_ids, database_path=database_path)

    assert bootstrap["release"]["health"] == "research"
    assert [row["gameweek"] for row in bootstrap["release"]["model_runs"]] == [2, 3, 4]
    assert len(bootstrap["players"]) == 15
    assert result["horizon"] == [2, 3, 4]
    assert len(result["lineups"]) == 3
    assert all(row["formation"] == "3-4-3" for row in result["lineups"])
    assert all(len(row["starters"]) == 11 for row in result["lineups"])
    assert result["squad_rating"]["schema_version"] == "squad_rating_v1"
    assert result["squad_rating"]["input"]["raw_cumulative_xpts"] == pytest.approx(
        result["cumulative_xpts"]
    )
    assert result["squad_rating"]["input"]["squad_fpl_ids"] == sorted(fpl_ids)
    assert len(result["squad_rating"]["input"]["optimized_decisions"]) == 3
    # This deliberately tiny fixture has exactly one legal squad, below the
    # production contract's minimum benchmark population.
    assert result["squad_rating"]["available"] is False
    assert result["squad_rating"]["model_strength"] is None


def test_web_lineup_rejects_duplicate_squad_players(tmp_path: Path):
    database_path = tmp_path / "web.duckdb"
    fpl_ids = _database(database_path)

    with pytest.raises(ValueError, match="15 unique players"):
        recommend_web_lineups((*fpl_ids[:-1], fpl_ids[0]), database_path=database_path)


def test_weekly_lineup_reports_marginal_xpts_against_loaded_current_setup(tmp_path: Path):
    release_path = tmp_path / "release.json"
    fpl_ids = _release_file(release_path)
    setup = CurrentSquadSetup(
        gameweek=2,
        starter_fpl_ids=(1, 3, 4, 5, 8, 9, 10, 11, 12, 13, 14),
        bench_fpl_ids=(2, 6, 7, 15),
        captain_fpl_id=9,
        vice_captain_fpl_id=5,
    )

    result = recommend_web_lineups(fpl_ids, release_path=release_path, current_setup=setup)

    comparison = result["lineups"][0]["current_setup_comparison"]
    assert comparison["basis"] == "loaded_fpl_picks"
    assert comparison["current_formation"] == "3-5-2"
    assert comparison["current_total_xpts"] == pytest.approx(49.5)
    assert comparison["recommended_total_xpts"] == pytest.approx(59.5)
    assert comparison["marginal_xpts"] == pytest.approx(10.0)
    assert {row["fpl_id"] for row in comparison["started"]} == {2, 6, 7, 15}
    assert {row["fpl_id"] for row in comparison["benched"]} == {1, 3, 4, 8}
    assert comparison["captain_change"]["from"]["fpl_id"] == 9
    assert comparison["captain_change"]["to"]["fpl_id"] == 15
    assert comparison["bench_order_changed"] is True
    assert result["lineups"][1]["current_setup_comparison"] is None


def _release_file(path: Path, *, player_one_xpts: float = 0.5) -> tuple[int, ...]:
    positions = ("GK", "GK", *("DEF",) * 5, *("MID",) * 5, *("FWD",) * 3)
    players = []
    for fpl_id, position in enumerate(positions, start=1):
        xpts = player_one_xpts if fpl_id == 1 else fpl_id / 2
        players.append(
            {
                "fpl_id": fpl_id,
                "player_code": 10_000 + fpl_id,
                "name": f"Player {fpl_id}",
                "team_id": ((fpl_id - 1) % 6) + 1,
                "team": f"T{((fpl_id - 1) % 6) + 1}",
                "position": position,
                "price_tenths": 50,
                "status": "a",
                "gameweeks": {
                    str(gameweek): {
                        "xpts": xpts,
                        "appearance_probability": 0.95,
                        "uncertainty": None,
                        "quality_flags": [],
                    }
                    for gameweek in (2, 3, 4)
                },
            }
        )
    payload = {
        "schema_version": "fpl_web_release_v1",
        "release": {
            "release_id": f"web_release_{path.stem}",
            "health": "shadow",
            "source_ingestion_run_id": path.stem,
            "model_version": "web_test_v1",
            "planning_as_of": "2026-08-26T08:00:00+00:00",
            "model_runs": [
                {"gameweek": gameweek, "model_run_id": f"run_gw{gameweek}"}
                for gameweek in (2, 3, 4)
            ],
        },
        "players": players,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return tuple(range(1, 16))


def _release_with_transfer_candidates(path: Path) -> tuple[int, ...]:
    fpl_ids = _release_file(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    for fpl_id, position, team_id in zip(
        range(16, 20),
        ("GK", "DEF", "MID", "FWD"),
        range(7, 11),
        strict=True,
    ):
        payload["players"].append(
            {
                "fpl_id": fpl_id,
                "player_code": 10_000 + fpl_id,
                "name": f"Candidate {fpl_id}",
                "team_id": team_id,
                "team": f"T{team_id}",
                "position": position,
                "price_tenths": 50,
                "status": "a",
                "gameweeks": {
                    str(gameweek): {
                        "xpts": 9.0,
                        "appearance_probability": 0.95,
                        "uncertainty": None,
                        "quality_flags": [],
                    }
                    for gameweek in (2, 3, 4)
                },
            }
        )
    path.write_text(json.dumps(payload), encoding="utf-8")
    return fpl_ids


def test_transfer_paths_score_hold_free_transfer_and_hit_choices(tmp_path: Path):
    release_path = tmp_path / "paths.json"
    fpl_ids = _release_with_transfer_candidates(release_path)

    one_free_transfer = recommend_web_transfers(
        fpl_ids,
        free_transfers=1,
        release_path=release_path,
    )
    one_paths = {row["id"]: row for row in one_free_transfer["paths"]}
    assert {"hold", "one_ft", "hit_minus4", "roll"} <= set(one_paths)
    assert one_paths["hold"]["net_xpts"] == pytest.approx(
        one_free_transfer["baseline_cumulative_xpts"]
    )
    assert one_paths["one_ft"]["hit"] == 0.0
    assert len(one_paths["one_ft"]["transfers"]) == 1
    assert one_paths["hit_minus4"]["hit"] == 4.0
    assert len(one_paths["hit_minus4"]["transfers"]) == 2
    assert one_paths["hit_minus4"]["delta_xpts_vs_hold"] > 0
    assert one_free_transfer["recommended_path_id"] == "hit_minus4"
    assert "does not score GW+1" in one_paths["roll"]["note"]

    no_free_transfers = recommend_web_transfers(
        fpl_ids,
        free_transfers=0,
        release_path=release_path,
    )
    zero_paths = {row["id"]: row for row in no_free_transfers["paths"]}
    assert "one_ft" not in zero_paths
    assert zero_paths["hit_minus4"]["hit"] == 4.0
    assert len(zero_paths["hit_minus4"]["transfers"]) == 1
    assert zero_paths["hit_minus4"]["net_xpts"] > zero_paths["hold"]["net_xpts"]

    two_free_transfers = recommend_web_transfers(
        fpl_ids,
        free_transfers=2,
        release_path=release_path,
    )
    two_paths = {row["id"]: row for row in two_free_transfers["paths"]}
    assert two_paths["one_ft"]["hit"] == 0.0
    assert two_paths["two_ft"]["hit"] == 0.0
    assert len(two_paths["two_ft"]["transfers"]) == 2
    assert two_free_transfers["recommended_path_id"] == "two_ft"


def _ready_rating_artifact() -> dict:
    return {
        "schema_version": "squad_benchmark_master_v1",
        "formula_version": "optimized_xi_captain_percentile_v1",
        "population_policy_version": "deterministic_rank_weighted_legal_sampler_v1",
        "artifact_id": "squad_benchmark_master_test",
        "status": "ready",
        "source_identity": "rating_source_test",
        "gameweeks": [2, 3, 4],
        "budget_anchors_tenths": [750],
        "target_population_per_anchor": 128,
        "minimum_runtime_population": 100,
        "max_attempts_per_anchor": 20_000,
        "spend_band_tenths": 50,
        "eligible_player_count": 500,
        "anchor_reports": [
            {"budget_tenths": 750, "population_size": 128, "status": "ready"}
        ],
        "population": [
            {
                "fpl_ids": list(range(index * 20 + 1, index * 20 + 16)),
                "squad_cost_tenths": 700 + index % 51,
                "gameweek_xpts": [40.0 + index / 10] * 3,
                "cumulative_xpts": 120.0 + index * 0.3,
            }
            for index in range(128)
        ],
        "problems": [],
    }


def test_compact_release_runs_without_database_and_reports_drift(tmp_path: Path):
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    fpl_ids = _release_file(before_path)
    _release_file(after_path, player_one_xpts=3.0)

    bootstrap = load_web_bootstrap(
        tmp_path / "missing.duckdb",
        release_path=before_path,
    )
    lineups = recommend_web_lineups(fpl_ids, release_path=before_path)
    drift = compare_web_releases(before_path=before_path, after_path=after_path)

    assert bootstrap["release"]["health"] == "shadow"
    assert lineups["health"] == "shadow"
    assert len(lineups["lineups"]) == 3
    assert drift.material_change
    assert drift.report["players"]["material_change_count"] == 3


def test_production_release_withholds_rating_without_materialized_benchmark(tmp_path: Path):
    release_path = tmp_path / "production.json"
    fpl_ids = _release_file(release_path)
    payload = json.loads(release_path.read_text(encoding="utf-8"))
    payload["release"]["health"] = "production"
    release_path.write_text(json.dumps(payload), encoding="utf-8")

    result = recommend_web_lineups(fpl_ids, release_path=release_path)

    rating = result["squad_rating"]
    assert rating["available"] is False
    assert "lacks a ready materialized" in rating["explanation"]
    assert rating["performance_contract"]["cold_request_build_allowed"] is False
    assert rating["performance_contract"]["passes"] is False


def test_production_release_uses_materialized_benchmark_without_runtime_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    release_path = tmp_path / "production_materialized.json"
    fpl_ids = _release_file(release_path)
    payload = json.loads(release_path.read_text(encoding="utf-8"))
    payload["release"]["health"] = "production"
    payload["release"]["rating_benchmark"] = _ready_rating_artifact()
    release_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        webapp_service,
        "build_squad_benchmark",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("runtime benchmark build must not run")
        ),
    )

    result = recommend_web_lineups(fpl_ids, release_path=release_path)

    rating = result["squad_rating"]
    assert rating["available"] is True
    assert rating["benchmark"]["materialization_mode"] == "release_artifact"
    assert rating["performance_contract"]["passes"] is True


def test_release_drift_can_validate_lineup_and_rating_without_expensive_transfer_scan(
    tmp_path: Path,
):
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    fpl_ids = _release_file(before_path)
    _release_file(after_path, player_one_xpts=0.6)

    result = compare_web_releases(
        before_path=before_path,
        after_path=after_path,
        owned_fpl_ids=fpl_ids,
        include_transfer_scan=False,
    )

    assert result.report["decisions"]["evaluated"] is True
    assert result.report["decisions"]["transfer"]["evaluated"] is False
    assert result.report["thresholds"]["include_transfer_scan"] is False


def _release_with_chip_candidates(path: Path) -> tuple[int, ...]:
    """Transfer candidates plus cheap fillers from four more clubs.

    The Wildcard/Free Hit squad search is a bounded beam; like the real
    20-club release it needs more clubs than the base fixture to find legal
    rebuilds under the three-per-club limit.
    """

    fpl_ids = _release_with_transfer_candidates(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    next_id = 20
    for team_id in range(11, 15):
        for position in ("GK", "DEF", "MID", "FWD"):
            payload["players"].append(
                {
                    "fpl_id": next_id,
                    "player_code": 10_000 + next_id,
                    "name": f"Filler {next_id}",
                    "team_id": team_id,
                    "team": f"T{team_id}",
                    "position": position,
                    "price_tenths": 45,
                    "status": "a",
                    "gameweeks": {
                        str(gameweek): {
                            "xpts": 0.25,
                            "appearance_probability": 0.95,
                            "uncertainty": None,
                            "quality_flags": [],
                        }
                        for gameweek in (2, 3, 4)
                    },
                }
            )
            next_id += 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    return fpl_ids


def _starting_and_bench_ids(lineup: dict) -> set[int]:
    return {player["fpl_id"] for player in (*lineup["starters"], *lineup["bench"])}


def test_points_chips_score_only_the_first_gameweek_and_never_move_the_rating(tmp_path: Path):
    release_path = tmp_path / "points_chips.json"
    fpl_ids = _release_file(release_path)
    payload = json.loads(release_path.read_text(encoding="utf-8"))
    payload["release"]["rating_benchmark"] = _ready_rating_artifact()
    release_path.write_text(json.dumps(payload), encoding="utf-8")

    base = recommend_web_lineups(fpl_ids, release_path=release_path)
    bench_boost = recommend_web_lineups(
        fpl_ids, release_path=release_path, chips=ChipPlan(active="bench_boost")
    )
    triple_captain = recommend_web_lineups(
        fpl_ids, release_path=release_path, chips=ChipPlan(active="triple_captain")
    )

    first = base["lineups"][0]
    bench_xpts = sum(player["xpts"] for player in first["bench"])
    captain_xpts = first["captain"]["xpts"]
    assert bench_xpts > 0
    assert bench_boost["lineups"][0]["total_xpts"] == pytest.approx(
        first["total_xpts"] + bench_xpts
    )
    assert bench_boost["lineups"][0]["chip_effect"]["delta_xpts"] == pytest.approx(bench_xpts)
    assert triple_captain["lineups"][0]["total_xpts"] == pytest.approx(
        first["total_xpts"] + captain_xpts
    )
    assert triple_captain["lineups"][0]["chip_effect"]["delta_xpts"] == pytest.approx(
        captain_xpts
    )
    for result, delta in ((bench_boost, bench_xpts), (triple_captain, captain_xpts)):
        # The chip is played once; later Gameweeks are the no-chip lineups.
        assert [row["total_xpts"] for row in result["lineups"][1:]] == pytest.approx(
            [row["total_xpts"] for row in base["lineups"][1:]]
        )
        assert all(row["chip_effect"] is None for row in result["lineups"][1:])
        assert result["chip_scenario"] is True
        assert result["plan_summary"]["net_xpts_vs_holding"] == pytest.approx(delta)
        assert result["plan_summary"]["chip_effect_xpts"] == pytest.approx(delta)
        assert result["baseline_cumulative_xpts"] == pytest.approx(base["cumulative_xpts"])
        # The benchmark never receives a chip, so the rating ignores it.
        assert result["squad_rating"]["available"] is True
        assert result["squad_rating"]["model_strength"] == base["squad_rating"]["model_strength"]
    assert base["chip_scenario"] is False
    assert base["lineups"][0]["chip_effect"] is None

    transfers = recommend_web_transfers(
        fpl_ids, release_path=release_path, chips=ChipPlan(active="triple_captain")
    )
    assert transfers["baseline_cumulative_xpts"] == pytest.approx(
        base["cumulative_xpts"] + captain_xpts
    )
    assert transfers["chip"] == "triple_captain"


def test_a_chip_marked_used_cannot_be_played():
    with pytest.raises(ValueError, match="marked used"):
        ChipPlan(active="bench_boost", used=frozenset({"bench_boost"}))
    with pytest.raises(ValueError, match="unknown chip"):
        ChipPlan(active="double_captain")
    assert ChipPlan(used=frozenset({"wildcard"})).status["wildcard"] == "used"


def test_wildcard_makes_staged_moves_free_and_preserves_free_transfers(tmp_path: Path):
    release_path = tmp_path / "wildcard_chip.json"
    fpl_ids = _release_with_chip_candidates(release_path)
    staged = (
        PendingTransfer(out_fpl_id=1, in_fpl_id=16),
        PendingTransfer(out_fpl_id=3, in_fpl_id=17),
        PendingTransfer(out_fpl_id=8, in_fpl_id=18),
    )

    no_chip = recommend_web_lineups(
        fpl_ids, free_transfers=1, pending_transfers=staged, release_path=release_path
    )
    wildcard = recommend_web_lineups(
        fpl_ids,
        free_transfers=1,
        pending_transfers=staged,
        chips=ChipPlan(active="wildcard"),
        release_path=release_path,
    )

    assert no_chip["plan_summary"]["pending_hit_cost"] == 8.0
    assert no_chip["plan_summary"]["effective_free_transfers"] == 0
    summary = wildcard["plan_summary"]
    assert summary["pending_hit_cost"] == 0.0
    assert summary["effective_free_transfers"] == 1
    assert summary["commit_allowed"] is True
    assert summary["net_xpts_vs_holding"] == pytest.approx(
        no_chip["plan_summary"]["net_xpts_vs_holding"] + 8.0
    )
    assert all({16, 17, 18} <= _starting_and_bench_ids(row) for row in wildcard["lineups"])

    scan = recommend_web_transfers(
        fpl_ids,
        free_transfers=0,
        chips=ChipPlan(active="wildcard"),
        release_path=release_path,
    )
    assert all(row["hit_cost"] == 0 for row in scan["suggestions"])
    paths = {row["id"]: row for row in scan["paths"]}
    assert set(paths) == {"hold", "one_move", "chip_squad"}
    assert paths["chip_squad"]["hit"] == 0.0
    assert len(paths["chip_squad"]["transfers"]) >= 2
    assert paths["chip_squad"]["net_xpts"] > paths["one_move"]["net_xpts"]
    assert scan["recommended_path_id"] == "chip_squad"

    # Staging the returned rebuild reproduces the path's server score.
    rebuild = tuple(
        PendingTransfer(out_fpl_id=row["out_fpl_id"], in_fpl_id=row["in_fpl_id"])
        for row in paths["chip_squad"]["transfers"]
    )
    staged_rebuild = recommend_web_lineups(
        fpl_ids,
        free_transfers=0,
        pending_transfers=rebuild,
        chips=ChipPlan(active="wildcard"),
        release_path=release_path,
    )
    assert staged_rebuild["plan_summary"]["net_xpts_vs_holding"] == pytest.approx(
        paths["chip_squad"]["delta_xpts_vs_hold"]
    )


def test_free_hit_scores_the_chip_gameweek_then_reverts_to_the_committed_squad(
    tmp_path: Path,
):
    release_path = tmp_path / "free_hit.json"
    fpl_ids = _release_with_chip_candidates(release_path)
    free_hit = ChipPlan(active="free_hit")
    staged = (PendingTransfer(out_fpl_id=8, in_fpl_id=18),)

    base = recommend_web_lineups(fpl_ids, free_transfers=0, release_path=release_path)
    result = recommend_web_lineups(
        fpl_ids,
        free_transfers=0,
        pending_transfers=staged,
        chips=free_hit,
        release_path=release_path,
    )

    assert 18 in _starting_and_bench_ids(result["lineups"][0])
    for later in result["lineups"][1:]:
        assert 18 not in _starting_and_bench_ids(later)
        assert 8 in _starting_and_bench_ids(later)
    assert [row["total_xpts"] for row in result["lineups"][1:]] == pytest.approx(
        [row["total_xpts"] for row in base["lineups"][1:]]
    )
    summary = result["plan_summary"]
    assert summary["pending_hit_cost"] == 0.0
    assert summary["commit_allowed"] is False
    assert summary["squad_reverts_after_gameweek"] == 2
    assert summary["effective_free_transfers"] == 0
    assert summary["net_xpts_vs_holding"] == pytest.approx(
        result["lineups"][0]["total_xpts"] - base["lineups"][0]["total_xpts"]
    )
    assert summary["net_xpts_vs_holding"] > 0
    # Free Hit is rated on the squad it reverts to.
    assert result["squad_rating"]["input"]["squad_fpl_ids"] == sorted(fpl_ids)

    no_chip_scan = recommend_web_transfers(fpl_ids, free_transfers=1, release_path=release_path)
    free_hit_scan = recommend_web_transfers(
        fpl_ids, free_transfers=0, chips=free_hit, release_path=release_path
    )
    move = {
        (row["out"]["fpl_id"], row["in"]["fpl_id"]): row["net_xpts_gain"]
        for row in no_chip_scan["suggestions"]
    }
    free_hit_move = {
        (row["out"]["fpl_id"], row["in"]["fpl_id"]): row["net_xpts_gain"]
        for row in free_hit_scan["suggestions"]
    }
    shared = set(move) & set(free_hit_move)
    assert shared
    for key in shared:
        # Identical projections each Gameweek: one scored week of three.
        assert free_hit_move[key] == pytest.approx(move[key] / 3)
    assert {row["id"] for row in free_hit_scan["paths"]} == {"hold", "one_move", "chip_squad"}
    assert all(row["hit"] == 0.0 for row in free_hit_scan["paths"])


def test_staged_moves_are_validated_as_one_confirmed_set(tmp_path: Path):
    release_path = tmp_path / "batch.json"
    fpl_ids = _release_with_transfer_candidates(release_path)
    payload = json.loads(release_path.read_text(encoding="utf-8"))
    for player in payload["players"]:
        if player["fpl_id"] == 17:
            player["price_tenths"] = 55
        if player["fpl_id"] == 18:
            player["price_tenths"] = 45
    release_path.write_text(json.dumps(payload), encoding="utf-8")

    # The first move alone needs £0.5m more than the bank holds; the second
    # frees it. FPL confirms both together, so the set is affordable.
    result = recommend_web_lineups(
        fpl_ids,
        free_transfers=2,
        pending_transfers=(
            PendingTransfer(out_fpl_id=3, in_fpl_id=17),
            PendingTransfer(out_fpl_id=8, in_fpl_id=18),
        ),
        release_path=release_path,
    )
    assert result["plan_summary"]["effective_bank_tenths"] == 0
    assert result["plan_summary"]["pending_hit_cost"] == 0.0

    with pytest.raises(ValueError, match="not affordable"):
        recommend_web_lineups(
            fpl_ids,
            pending_transfers=(PendingTransfer(out_fpl_id=3, in_fpl_id=17),),
            release_path=release_path,
        )


def test_wildcard_comparison_requires_an_unused_wildcard_and_no_active_chip(tmp_path: Path):
    release_path = tmp_path / "wildcard_used.json"
    fpl_ids = _release_file(release_path)

    with pytest.raises(ValueError, match="marked used"):
        compare_web_wildcard(
            fpl_ids,
            horizon_length=3,
            roll_after_wildcard=1,
            chips=ChipPlan(used=frozenset({"wildcard"})),
            release_path=release_path,
        )
    with pytest.raises(ValueError, match="clear the active chip"):
        compare_web_wildcard(
            fpl_ids,
            horizon_length=3,
            roll_after_wildcard=1,
            chips=ChipPlan(active="bench_boost"),
            release_path=release_path,
        )
