"""Coverage for fixture/opponent and confidence (uncertainty) data flowing
through webapp.service into lineup/outlook responses.

Reuses test_webapp_service.py's own `_database` fixture (now extended with
opponent_team_id/is_home columns), so this exercises the real
load_horizon_catalog SQL query rather than a hand-built payload.
"""

from __future__ import annotations

from pathlib import Path

from fpl_model.webapp.service import load_web_bootstrap, recommend_web_lineups
from tests.test_webapp_service import _database


def test_bootstrap_players_carry_a_fixture_per_gameweek(tmp_path: Path):
    database_path = tmp_path / "web.duckdb"
    _database(database_path)

    bootstrap = load_web_bootstrap(database_path)

    player = next(row for row in bootstrap["players"] if row["fpl_id"] == 1)
    fixtures = player["gameweeks"]["2"]["fixtures"]
    assert len(fixtures) == 1
    assert set(fixtures[0]) == {"opponent_team_id", "opponent", "is_home"}
    assert fixtures[0]["opponent"].startswith("T")


def test_lineup_players_carry_fixtures_and_uncertainty(tmp_path: Path):
    database_path = tmp_path / "web.duckdb"
    fpl_ids = _database(database_path)

    result = recommend_web_lineups(fpl_ids, database_path=database_path)

    lineup = result["lineups"][0]
    captain = lineup["captain"]
    assert "fixtures" in captain
    assert len(captain["fixtures"]) == 1
    assert "uncertainty" in captain
    # _database's own fixture rows never set uncertainty -- NULL in this
    # fixture, matching the current production release's own shadow-stage
    # state (no calibrated uncertainty yet).
    assert captain["uncertainty"] is None
    for player in [*lineup["starters"], *lineup["bench"]]:
        assert "fixtures" in player
        assert "uncertainty" in player


def test_fixture_is_home_and_away_are_both_represented(tmp_path: Path):
    database_path = tmp_path / "web.duckdb"
    fpl_ids = _database(database_path)

    result = recommend_web_lineups(fpl_ids, database_path=database_path)
    lineup = result["lineups"][0]
    all_players = [*lineup["starters"], *lineup["bench"]]

    home_values = {player["fixtures"][0]["is_home"] for player in all_players}
    # _database alternates is_home by fpl_id parity -- both must appear.
    assert home_values == {True, False}
