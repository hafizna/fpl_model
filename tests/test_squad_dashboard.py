from __future__ import annotations

import json

import duckdb
import pandas as pd

from fpl_model.presentation.squad_dashboard import (
    ScenarioSpec,
    build_squad_dashboard_data,
    render_squad_dashboard,
)
from scripts.build_squad_dashboard import _console_safe
from tests.test_rolling_store import _seed_horizon


def _scenario_csv(path, *, replacement: bool = False, selection: bool = True):
    rows = []
    for fpl_id in range(1, 16):
        selected_id = 16 if replacement and fpl_id == 10 else fpl_id
        rows.append(
            {
                "fpl_id": selected_id,
                "purchase_price": 5.0,
                "selling_price": 5.0,
                "squad_position": fpl_id if selection else None,
                "is_captain": fpl_id == 9 if selection else None,
                "is_vice_captain": fpl_id == 5 if selection else None,
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False)


def _seed(tmp_path):
    _, database_path, model_runs = _seed_horizon(tmp_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.executemany(
            """
            INSERT INTO team_snapshot (
                ingestion_run_id, team_id, team_code, name, short_name, unavailable
            ) VALUES ('official', ?, ?, ?, ?, FALSE)
            """,
            [
                (team_id, 100 + team_id, f"Team {team_id}", f"T{team_id}")
                for team_id in range(1, 6)
            ],
        )
        connection.executemany(
            """
            INSERT INTO player_status_snapshot VALUES (
                'official', ?, TRUE, TRUE, FALSE, 0.0, 0, 0, 0, 0,
                0, 0, 0.0, NULL, NULL, NULL
            )
            """,
            [(fpl_id,) for fpl_id in range(1, 16)],
        )
        # Replacing FWD 10 (team 5) with FWD 16 must preserve the club cap.
        connection.execute(
            "UPDATE player_snapshot SET team_id = 5 WHERE ingestion_run_id = 'official' AND fpl_id = 16"
        )
    return database_path, model_runs


def test_builds_scenario_transactions_coverage_and_lineup_recommendations(tmp_path):
    database_path, model_runs = _seed(tmp_path)
    first_path = tmp_path / "first.csv"
    second_path = tmp_path / "second.csv"
    _scenario_csv(first_path)
    _scenario_csv(second_path, replacement=True)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        data = build_squad_dashboard_data(
            connection,
            scenarios=(
                ScenarioSpec("Scenario A", first_path),
                ScenarioSpec("Scenario B", second_path),
            ),
            model_run_ids=model_runs,
            source_ingestion_run_id="official",
        )

    assert data["source_snapshot_matches_model"] is True
    assert [row["coverage"] for row in data["scenarios"]] == [15, 15]
    assert all(row["recommendation"] is not None for row in data["scenarios"])
    transaction = data["scenarios"][1]["transactions_from_first"]
    assert transaction["out"] == ["Player 10"]
    assert transaction["in"] == ["Player 16"]
    assert set(transaction["covered_owned_xpts_delta"]) == {"1", "2", "3"}


def test_withholds_lineup_recommendation_when_one_projection_is_missing(tmp_path):
    database_path, model_runs = _seed(tmp_path)
    scenario_path = tmp_path / "scenario.csv"
    _scenario_csv(scenario_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            "DELETE FROM player_fixture_projection WHERE model_run_id = 'model_gw2' AND player_code = 1007"
        )

    with duckdb.connect(str(database_path), read_only=True) as connection:
        data = build_squad_dashboard_data(
            connection,
            scenarios=(ScenarioSpec("Incomplete", scenario_path),),
            model_run_ids=model_runs,
            source_ingestion_run_id="official",
        )

    scenario = data["scenarios"][0]
    assert scenario["coverage"] == 14
    assert scenario["gaps"] == ["Player 7"]
    assert scenario["recommendation"] is None


def test_surfaces_research_evidence_without_closing_a_projection_gap(tmp_path):
    database_path, model_runs = _seed(tmp_path)
    scenario_path = tmp_path / "scenario.csv"
    _scenario_csv(scenario_path)
    with duckdb.connect(str(database_path)) as connection:
        position = connection.execute(
            "SELECT fpl_position FROM player_snapshot WHERE ingestion_run_id = 'official' AND fpl_id = 7"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO player_rate_evidence_import_run VALUES (
                'evidence', 'official', 1, 'test', 'test.csv', 'abc',
                TIMESTAMPTZ '2026-08-20 10:00:00+00:00', 1, 'completed'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO player_rate_evidence VALUES (
                'evidence', 'official', 7, 1007, 'Player 7', ?,
                'senior_comparable', 'Serie A', '2025-26', 2000, 22,
                NULL, NULL, NULL, 2, 0, NULL, NULL, NULL,
                TIMESTAMPTZ '2026-08-19 10:00:00+00:00',
                'https://example.test', 'research only',
                '["RESEARCH_EVIDENCE_NOT_PRODUCTION_RATE"]'
            )
            """,
            [position],
        )
        connection.execute(
            "DELETE FROM player_fixture_projection WHERE model_run_id = 'model_gw2' AND player_code = 1007"
        )

    with duckdb.connect(str(database_path), read_only=True) as connection:
        data = build_squad_dashboard_data(
            connection,
            scenarios=(ScenarioSpec("Incomplete", scenario_path),),
            model_run_ids=model_runs,
            source_ingestion_run_id="official",
        )

    scenario = data["scenarios"][0]
    player = next(row for row in scenario["players"] if row["fpl_id"] == 7)
    assert scenario["coverage"] == 14
    assert scenario["gap_evidence_count"] == 1
    assert player["research_evidence"]["source_competition"] == "Serie A"
    assert player["research_evidence"]["sample_minutes"] == 2000
    assert scenario["recommendation"] is None


def test_accepts_a_squad_only_scenario_with_blank_selection_fields(tmp_path):
    database_path, model_runs = _seed(tmp_path)
    scenario_path = tmp_path / "draft.csv"
    _scenario_csv(scenario_path, selection=False)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        data = build_squad_dashboard_data(
            connection,
            scenarios=(ScenarioSpec("Draft", scenario_path),),
            model_run_ids=model_runs,
            source_ingestion_run_id="official",
        )

    assert data["scenarios"][0]["selection_complete"] is False
    assert data["scenarios"][0]["recommendation"] is None


def test_renders_one_dependency_free_interactive_html_document(tmp_path):
    output = tmp_path / "dashboard.html"
    data = {
        "source_captured_at": "2026-08-20T12:00:00+00:00",
        "model_as_of": "2026-08-17T09:00:00+00:00",
        "model_version": "test",
        "source_snapshot_matches_model": False,
        "model_runs": [{"gameweek": 1}, {"gameweek": 2}, {"gameweek": 3}],
        "scenarios": [
            {
                "label": "A </script> scenario",
                "selection_complete": False,
                "rules_legal": True,
                "rule_reasons": [],
                "bank_tenths": 0,
                "squad_cost_tenths": 1000,
                "coverage": 0,
                "gaps": [],
                "covered_owned_xpts": {"1": 0, "2": 0, "3": 0},
                "players": [],
                "recommendation": None,
                "transactions_from_first": {
                    "out": [],
                    "in": [],
                    "covered_owned_xpts_delta": {"1": 0, "2": 0, "3": 0},
                },
            }
        ],
        "limitations": [],
    }

    result = render_squad_dashboard(data, output)
    text = result.read_text(encoding="utf-8")

    assert text.startswith("<!doctype html>")
    assert "const DATA =" in text
    assert "A <\\/script> scenario" in text
    assert "fetch(" not in text
    assert json.loads(json.dumps(data))["scenarios"][0]["label"] in text.replace(
        "<\\/", "</"
    )


def test_console_summary_survives_a_legacy_windows_encoding():
    assert _console_safe("Muharemović", "cp1252") == "Muharemovi\\u0107"
