from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pandas as pd
import pytest

from fpl_model.ingest.team_strength import (
    _current_season_team_xg,
    import_team_strength_history,
    materialize_inseason_team_strength,
    materialize_preseason_team_strength,
    validate_team_strength_history,
)
from fpl_model.storage import initialize_database

TEAMS = (
    ("ARS", "Arsenal"),
    ("AVL", "Aston Villa"),
    ("BOU", "Bournemouth"),
    ("BRE", "Brentford"),
    ("BHA", "Brighton"),
    ("CHE", "Chelsea"),
    ("COV", "Coventry City"),
    ("CRY", "Crystal Palace"),
    ("EVE", "Everton"),
    ("FUL", "Fulham"),
    ("HUL", "Hull City"),
    ("IPS", "Ipswich Town"),
    ("LEE", "Leeds"),
    ("LIV", "Liverpool"),
    ("MCI", "Man City"),
    ("MUN", "Man Utd"),
    ("NEW", "Newcastle"),
    ("NFO", "Nott'm Forest"),
    ("TOT", "Spurs"),
    ("SUN", "Sunderland"),
)
PROMOTED = {"COV", "HUL", "IPS"}


def _team_history() -> pd.DataFrame:
    rows = []
    for index, (abbreviation, name) in enumerate(TEAMS, start=1):
        promoted = abbreviation in PROMOTED
        long_xg_rate = 1.0 + index / 100
        long_xgc_rate = 1.1 + index / 100
        if abbreviation == "IPS":
            long_xg_rate = 1.143972
            long_xgc_rate = 1.479448
        short_matches = 38 if promoted else 6
        short_xg_rate = long_xg_rate if promoted else long_xg_rate + 0.1
        short_xgc_rate = long_xgc_rate if promoted else long_xgc_rate + 0.05
        rows.append(
            {
                "team_abbreviation": abbreviation,
                "team_name": name,
                "prior_type": (
                    "promoted_team_prior" if promoted else "observed_previous_pl"
                ),
                "long_form_matches": 38,
                "long_form_xg": long_xg_rate * 38,
                "long_form_xgc": long_xgc_rate * 38,
                "short_form_matches": short_matches,
                "short_form_xg": short_xg_rate * short_matches,
                "short_form_xgc": short_xgc_rate * short_matches,
                "league_average_xg_per_match": 1.5294868421052636,
                "league_average_xgc_per_match": 1.5294605263157894,
            }
        )
    return pd.DataFrame(rows)


def _insert_fpl_snapshot(database_path) -> None:
    initialize_database(database_path)
    captured = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    deadline = datetime(2026, 8, 22, 17, 30, tzinfo=UTC)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('fpl-run', 'official_fpl_api', ?, 'completed')
            """,
            [captured],
        )
        connection.execute(
            """
            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES (
                'fpl-run', '2026-27', 1, 1001, 'Test', 'Player', 'Player',
                1, 'MID', 5.0, 'a'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO gameweek_snapshot VALUES (
                'fpl-run', 1, 'Gameweek 1', ?, NULL,
                false, false, false, false, true
            )
            """,
            [deadline],
        )
        connection.executemany(
            """
            INSERT INTO team_snapshot VALUES (
                'fpl-run', ?, ?, ?, ?, false,
                NULL, NULL, NULL, NULL, NULL, NULL, NULL
            )
            """,
            [
                (team_id, 100 + team_id, name, abbreviation)
                for team_id, (abbreviation, name) in enumerate(TEAMS, start=1)
            ],
        )


def test_team_history_validation_requires_explicit_promoted_priors():
    valid = validate_team_strength_history(_team_history())
    assert len(valid) == 20
    assert set(valid.loc[valid["prior_type"] == "promoted_team_prior", "team_abbreviation"]) == PROMOTED

    missing_prior = _team_history()
    missing_prior.loc[missing_prior["team_abbreviation"] == "COV", "prior_type"] = (
        "observed_previous_pl"
    )
    with pytest.raises(ValueError, match="exactly three promoted priors"):
        validate_team_strength_history(missing_prior)

    inconsistent = _team_history()
    inconsistent.loc[inconsistent["team_abbreviation"] == "IPS", "short_form_xg"] += 1
    with pytest.raises(ValueError, match="long/short xg rates"):
        validate_team_strength_history(inconsistent)


def test_team_strength_import_and_materialization_are_idempotent(tmp_path):
    csv_path = tmp_path / "team_strength.csv"
    _team_history().to_csv(csv_path, index=False)
    database_path = tmp_path / "fpl.duckdb"
    _insert_fpl_snapshot(database_path)
    imported_at = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)

    first = import_team_strength_history(
        csv_path,
        target_season="2026-27",
        previous_season="2025-26",
        source_label="MODEL.xlsx TABLES resolved team windows",
        imported_at=imported_at,
        database_path=database_path,
    )
    second = import_team_strength_history(
        csv_path,
        target_season="2026-27",
        previous_season="2025-26",
        source_label="MODEL.xlsx TABLES resolved team windows",
        imported_at=imported_at,
        database_path=database_path,
    )
    materialized = materialize_preseason_team_strength(
        source_import_run_id=first.import_run_id,
        database_path=database_path,
    )
    repeated = materialize_preseason_team_strength(
        source_import_run_id=first.import_run_id,
        database_path=database_path,
    )

    assert first == second
    assert materialized == repeated
    assert materialized.team_rows == 20
    with duckdb.connect(str(database_path), read_only=True) as connection:
        ipswich = connection.execute(
            """
            SELECT long_form_xg_per_match, short_form_xg_per_match,
                   long_form_xgc_per_match, short_form_xgc_per_match,
                   corrected_xgc_per_match, is_promoted_prior,
                   data_quality_flags
            FROM team_strength_projection
            WHERE strength_run_id = ? AND team_abbreviation = 'IPS'
            """,
            [materialized.strength_run_id],
        ).fetchone()

    assert ipswich[0] == pytest.approx(1.143972)
    assert ipswich[1] == pytest.approx(1.143972)
    assert ipswich[2] == pytest.approx(1.479448)
    assert ipswich[3] == pytest.approx(1.479448)
    assert ipswich[4] == pytest.approx(1.4094362167912726)
    assert ipswich[5] is True
    assert "PROMOTED_TEAM_PRIOR" in ipswich[6]


def test_materializer_rejects_team_mapping_gap(tmp_path):
    csv_path = tmp_path / "team_strength.csv"
    history = _team_history()
    history.loc[history["team_abbreviation"] == "ARS", "team_abbreviation"] = "ABC"
    history.to_csv(csv_path, index=False)
    database_path = tmp_path / "fpl.duckdb"
    _insert_fpl_snapshot(database_path)
    imported = import_team_strength_history(
        csv_path,
        target_season="2026-27",
        previous_season="2025-26",
        source_label="test",
        database_path=database_path,
    )

    with pytest.raises(ValueError, match="current-team mismatch"):
        materialize_preseason_team_strength(
            source_import_run_id=imported.import_run_id,
            database_path=database_path,
        )


def _seed_inseason_events(database_path) -> None:
    """Add GW1-2 final events so a GW3 in-season blend has current evidence.

    Team 19 (Spurs) is given a leaky current season (opponents create a lot of
    xG against them); team 1 (Arsenal) keeps a tight one.
    """
    with duckdb.connect(str(database_path)) as connection:
        for gameweek, deadline in ((2, datetime(2026, 8, 29, 17, 30, tzinfo=UTC)),
                                   (3, datetime(2026, 9, 5, 17, 30, tzinfo=UTC))):
            connection.execute(
                "INSERT INTO gameweek_snapshot VALUES "
                "('fpl-run', ?, ?, ?, NULL, ?, ?, false, false, true)",
                [gameweek, f"Gameweek {gameweek}", deadline, gameweek < 3, gameweek < 3],
            )
        # GW1 and GW2 fixtures: Spurs (19) vs Arsenal (1), then Spurs (19) vs Chelsea (6).
        connection.executemany(
            "INSERT INTO fixture_snapshot VALUES ('fpl-run', ?, ?, ?, ?, ?, true, true)",
            [
                (901, 1, datetime(2026, 8, 22, 19, 0, tzinfo=UTC), 19, 1),
                (902, 2, datetime(2026, 8, 29, 19, 0, tzinfo=UTC), 6, 19),
            ],
        )
        for gameweek, live_run in ((1, "live-gw1"), (2, "live-gw2")):
            connection.execute(
                "INSERT INTO fpl_event_live_run VALUES "
                "(?, 'fpl-run', '2026-27', ?, ?, 'x.json', 'sha', true, true, 3, "
                "'completed', current_timestamp)",
                [live_run, gameweek, datetime(2026, 8, 20 + gameweek, tzinfo=UTC)],
            )
        connection.executemany(
            "INSERT INTO player_snapshot (ingestion_run_id, season, fpl_id, player_code, "
            "first_name, second_name, web_name, team_id, fpl_position, price, fpl_status) "
            "VALUES ('fpl-run', '2026-27', ?, ?, 'T', 'P', 'P', ?, 'MID', 5.0, 'a')",
            [(9019, 5019, 19), (9001, 5001, 1), (9006, 5006, 6), (9119, 5119, 19)],
        )
        # One "player" per team carrying that team's whole xG for the fixture.
        connection.executemany(
            "INSERT INTO player_gameweek_stat VALUES "
            "(?, ?, ?, true, 90, 1, 0, 0, 0, 0, 0, 0, 0, 0, ?, 0, 0, 2, false, '[]')",
            [
                # GW1: Spurs 0.3 xG, Arsenal 2.5 xG  -> Spurs concede 2.5
                ("live-gw1", 9019, 5019, 0.3),
                ("live-gw1", 9001, 5001, 2.5),
                # GW2: Chelsea 2.7 xG, Spurs 0.4 xG  -> Spurs concede 2.7
                ("live-gw2", 9006, 5006, 2.7),
                ("live-gw2", 9119, 5119, 0.4),
            ],
        )


def test_inseason_team_strength_blends_toward_current_and_is_idempotent(tmp_path):
    csv_path = tmp_path / "team_strength.csv"
    _team_history().to_csv(csv_path, index=False)
    database_path = tmp_path / "fpl.duckdb"
    _insert_fpl_snapshot(database_path)
    _seed_inseason_events(database_path)
    imported = import_team_strength_history(
        csv_path,
        target_season="2026-27",
        previous_season="2025-26",
        source_label="test",
        database_path=database_path,
    )
    as_of = datetime(2026, 9, 6, tzinfo=UTC)
    first = materialize_inseason_team_strength(
        source_import_run_id=imported.import_run_id,
        target_gameweek=3,
        current_season="2026-27",
        source_ingestion_run_id="fpl-run",
        as_of=as_of,
        prior_matches=5.0,
        database_path=database_path,
    )
    repeated = materialize_inseason_team_strength(
        source_import_run_id=imported.import_run_id,
        target_gameweek=3,
        current_season="2026-27",
        source_ingestion_run_id="fpl-run",
        as_of=as_of,
        prior_matches=5.0,
        database_path=database_path,
    )
    assert first == repeated

    preseason = materialize_preseason_team_strength(
        source_import_run_id=imported.import_run_id,
        target_gameweek=1,
        source_ingestion_run_id="fpl-run",
        database_path=database_path,
    )
    with duckdb.connect(str(database_path), read_only=True) as connection:
        rows = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT team_abbreviation, blended_xgc_per_match, "
                "defensive_weakness_ratio, data_quality_flags "
                "FROM team_strength_projection WHERE strength_run_id = ?",
                [first.strength_run_id],
            ).fetchall()
        }
        frozen = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT team_abbreviation, blended_xgc_per_match "
                "FROM team_strength_projection WHERE strength_run_id = ?",
                [preseason.strength_run_id],
            ).fetchall()
        }

    # Spurs conceded ~2.6 xG/match over the two current fixtures -> blended xGC
    # is pulled well above the frozen prior; the flag records the shrink.
    assert rows["TOT"][0] > frozen["TOT"] + 0.3
    assert "SHRUNK_CURRENT_SEASON_TEAM_STRENGTH" in rows["TOT"][2]
    assert "CURRENT_SEASON_TEAM_MATCHES=2" in rows["TOT"][2]
    # A team with no current fixture keeps the frozen prior.
    assert rows["LEE"][0] == pytest.approx(frozen["LEE"])
    assert "FROZEN_PRESEASON_TEAM_STRENGTH_PRIOR" in rows["LEE"][2]


def _current_team_xg(database_path, *, as_of=datetime(2026, 9, 6, tzinfo=UTC)):
    with duckdb.connect(str(database_path), read_only=True) as connection:
        result, _ = _current_season_team_xg(
            connection,
            season="2026-27",
            source_ingestion_run_id="fpl-run",
            as_of=as_of,
            as_of_gameweek=3,
        )
    return result


def test_current_team_xg_ignores_live_runs_captured_after_the_target_deadline(tmp_path):
    database_path = tmp_path / "fpl.duckdb"
    _insert_fpl_snapshot(database_path)
    _seed_inseason_events(database_path)
    before = _current_team_xg(database_path)
    assert before[19] == pytest.approx((0.35, 2.6, 2))

    # A GW2 capture taken after the GW3 deadline (e.g. a later stat correction)
    # must not reach a GW3 projection, even though it is the newest capture.
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            "INSERT INTO fpl_event_live_run VALUES "
            "('live-gw2-late', 'fpl-run', '2026-27', 2, ?, 'x.json', 'sha-late', true, true, 3, "
            "'completed', current_timestamp)",
            [datetime(2026, 9, 10, tzinfo=UTC)],
        )
        connection.executemany(
            "INSERT INTO player_gameweek_stat VALUES "
            "(?, ?, ?, true, 90, 1, 0, 0, 0, 0, 0, 0, 0, 0, ?, 0, 0, 2, false, '[]')",
            [("live-gw2-late", 9006, 5006, 9.0), ("live-gw2-late", 9119, 5119, 0.4)],
        )

    assert _current_team_xg(database_path) == before


def test_current_team_xg_does_not_reuse_a_double_gameweek_total_per_fixture(tmp_path):
    database_path = tmp_path / "fpl.duckdb"
    _insert_fpl_snapshot(database_path)
    _seed_inseason_events(database_path)
    # Spurs (19) also play Aston Villa (2) in GW2: a double. The event-live
    # stats only carry Spurs' GW2 total (0.4 + 0.6), which cannot be split.
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            "INSERT INTO fixture_snapshot VALUES ('fpl-run', 903, 2, ?, 19, 2, true, true)",
            [datetime(2026, 8, 31, 19, 0, tzinfo=UTC)],
        )
        connection.executemany(
            "INSERT INTO player_snapshot (ingestion_run_id, season, fpl_id, player_code, "
            "first_name, second_name, web_name, team_id, fpl_position, price, fpl_status) "
            "VALUES ('fpl-run', '2026-27', ?, ?, 'T', 'P', 'P', ?, 'MID', 5.0, 'a')",
            [(9219, 5219, 19), (9002, 5002, 2)],
        )
        connection.executemany(
            "INSERT INTO player_gameweek_stat VALUES "
            "(?, ?, ?, true, 90, 1, 0, 0, 0, 0, 0, 0, 0, 0, ?, 0, 0, 2, false, '[]')",
            [("live-gw2", 9219, 5219, 0.6), ("live-gw2", 9002, 5002, 1.0)],
        )

    result = _current_team_xg(database_path)

    # Only Spurs' single GW1 fixture is usable; GW2 is not double-counted.
    assert result[19] == pytest.approx((0.3, 2.5, 1))
    assert result[1] == pytest.approx((2.5, 0.3, 1))
    assert 6 not in result
    assert 2 not in result
