"""Named regression cases for three real, previously-reviewed projection shapes.

P0 (`README.md`'s "Production critical path") asks for named regression cases
covering:

- a "Tzolis-like starter": zero previous-Premier-League rate/appearance
  history, but heavy foreign-league playing time (the real Christos Tzolis,
  557/439509, reviewed in
  `docs/research/selected_squad_player_rate_evidence_2026_27.json`: 34/36
  Belgian Pro League starts, 3068 minutes). The 2026-08-26 coverage audit
  (`docs/research/projection_coverage_audit_2026_27_gw1.json`) shows this
  currently resolves to a near-zero `start_probability`/`expected_minutes`
  via the appearance/rate cohort-average fallback, NOT a confident high
  number and NOT a silent exclusion. That research doc is explicit that
  fixing this number requires "an external-league translation and shrinkage
  policy" that has not yet been backtested -- this regression suite locks in
  the CURRENT, already-reviewed behaviour (low projection, clearly flagged)
  as a contract, not a claim that the number itself is correct.
- a "current-only Fulham starter": a new-to-the-Premier-League signing at an
  already-established club (real examples: Palacios/570, Charles/580,
  Gonzalo/569 at Fulham) with NO appearance data anywhere in the pipeline
  (`MISSING_APPEARANCE_PROJECTION` + `NO_PREVIOUS_PL_PLAYER_RATE_HISTORY` +
  `NO_WORKBOOK_APPEARANCE_HISTORY`). This must be excluded from
  `player_fixture_projection` entirely (`baseline_projection_gap`) rather
  than defaulted to a confident low number -- the opposite failure direction
  from the Tzolis case.
- a "Henderson/Welbeck-like non-appearance": an established Premier League
  player with a real current-season doubt (injury news, rotation risk) that
  a raw historical rate/appearance history alone would not capture. A
  reviewed appearance-scenario override (`context/minutes.py`,
  `context/appearance_scenario_presets.py`) must be able to override the
  history-derived projection -- this is the decision-safety boundary these
  two players' real fpl_status/news fields (captured in the local database,
  `chance_of_playing_this_round` and a wrist-injury `news` string) actually
  exercise, not a claim about their specific real-world minutes this season.

Fixture identity (fpl_id/player_code) matches the real players named above
where the outcome under test does not depend on data specific to their
actual current season -- so a maintainer recognises which real case each
test protects -- but every numeric input is a constructed, minimal fixture,
not a replay of live snapshot data that would make this test fragile to a
future re-ingest.
"""

from __future__ import annotations

import json
from datetime import datetime

import duckdb

from fpl_model.context.appearance_scenario_presets import (
    ROTATION_RISK,
    apply_appearance_scenario_preset,
)
from fpl_model.context.minutes import (
    create_appearance_scenario_override,
    store_appearance_scenario_override,
)
from fpl_model.model.appearance_pipeline import materialize_preseason_appearance
from fpl_model.model.baseline_pipeline import materialize_preseason_baseline
from fpl_model.storage import initialize_database

_DEADLINE = "2026-08-22T00:30:00+07:00"
_AS_OF = "2026-08-18T09:00:00+07:00"


def _seed_common(database_path) -> None:
    """A minimal shared skeleton: one ingestion run, one fixture, one team pair.

    Timestamps are inlined as literals (rather than passed as prepared
    parameters) because DuckDB rejects prepared parameters in a multi-
    statement batch -- these are fixed test constants, not untrusted input.
    """
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            f"""
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('snapshot', 'fpl_api', '{_AS_OF}', 'completed');

            INSERT INTO team_snapshot (
                ingestion_run_id, team_id, team_code, name, short_name, unavailable
            ) VALUES
                ('snapshot', 1, 101, 'Home', 'HOM', false),
                ('snapshot', 2, 102, 'Away', 'AWY', false);

            INSERT INTO fixture_snapshot VALUES (
                'snapshot', 100, 1, '2026-08-23T15:00:00+01:00',
                1, 2, false, false
            );

            INSERT INTO availability_resolution_run (
                resolution_run_id, source_ingestion_run_id, target_gameweek,
                as_of, deadline, policy_version, status
            ) VALUES ('availability', 'snapshot', 1, '{_AS_OF}', '{_DEADLINE}', 'test', 'completed');

            INSERT INTO appearance_history_import_run VALUES (
                'appearance_history', '2025-26', 'test', 'test.csv', 'sha',
                '{_AS_OF}', 1, 'completed'
            );

            INSERT INTO appearance_projection_run (
                projection_run_id, availability_resolution_run_id,
                appearance_history_import_run_id, target_gameweek,
                policy_version, status
            ) VALUES ('appearance', 'availability', 'appearance_history', 1, 'test', 'completed');

            INSERT INTO player_fixture_history_import_run VALUES (
                'fixture_history', '2025-26', 'vaastav', 'revision',
                '2026-06-01T00:00:00+00:00', '{_AS_OF}',
                'players.csv', 'gws.csv', 'players-sha', 'gws-sha', 1, 1, 0,
                'completed'
            );
            INSERT INTO player_rate_history_run (
                rate_run_id, source_import_run_id, long_form_gameweeks,
                short_form_gameweeks, defcon_short_form_gameweeks,
                policy_version, player_rows, status
            ) VALUES ('rates', 'fixture_history', 38, 6, 10, 'test', 1, 'completed');

            INSERT INTO team_strength_import_run VALUES (
                'strength_import', '2026-27', '2025-26', 'test', 'strength.csv',
                'strength-sha', '{_AS_OF}', 20, 'completed'
            );
            INSERT INTO team_strength_run (
                strength_run_id, source_import_run_id, source_ingestion_run_id,
                target_gameweek, policy_version, team_rows, status
            ) VALUES ('strength', 'strength_import', 'snapshot', 1, 'test', 20, 'completed');
            INSERT INTO team_strength_projection VALUES
                ('strength', 1, 101, 'HOM', 'Home', false,
                 1.4, 1.5, 1.42, 1.3, 1.4, 1.32, 1.28, 1.5, 1.5,
                 0.95, 0.9, 1.1, 1.05, '[]'),
                ('strength', 2, 102, 'AWY', 'Away', false,
                 1.2, 1.3, 1.22, 1.5, 1.6, 1.52, 1.48, 1.5, 1.5,
                 0.81, 1.1, 0.95, 0.98, '[]');
            """
        )


def _cohort_player(connection, *, fpl_id: int, player_code: int, name: str) -> None:
    """One established MID with full appearance + rate history, so a broad
    (position, price-band) empirical-prior cohort can form around the
    regression cases below -- MIN_APPEARANCE_PRIOR_COHORT/MIN_RATE_PRIOR_COHORT
    both require several such players before any fallback prior activates.

    Values are inlined as literals rather than prepared parameters because
    DuckDB rejects prepared parameters in a multi-statement batch -- name and
    player_code are fixed, test-generated identifiers, not untrusted input.
    """
    connection.execute(
        f"""
        INSERT INTO player_snapshot (
            ingestion_run_id, season, fpl_id, player_code, first_name,
            second_name, web_name, team_id, fpl_position, price, fpl_status
        ) VALUES ('snapshot', '2026-27', {fpl_id}, {player_code}, 'Cohort',
                   '{name}', '{name}', 1, 'MID', 6.5, 'a');

        INSERT INTO player_appearance_history VALUES (
            'appearance_history', {player_code}, '{name}', 30, 3, 2, 75.0, 20.0
        );
        INSERT INTO player_appearance_projection VALUES (
            'appearance', {fpl_id}, {player_code}, 1.0, 0.8, 0.1, 0.9, 0.7,
            65.0, 0.8, 0.4, 1.2, '[]'
        );
        INSERT INTO player_rate_history VALUES (
            'rates', {player_code}, '{name}', 'MID', 2850, 30, 0, 3, 0, 8, 350,
            2850, 5.5, 4.0, 2850, 5.5, 4.0,
            0, 0, 0, 0, '[]'
        );
        """
    )


def _seed_cohort(database_path, count: int = 5) -> None:
    with duckdb.connect(str(database_path)) as connection:
        for index in range(count):
            _cohort_player(
                connection,
                fpl_id=1000 + index,
                player_code=900000 + index,
                name=f"Cohort{index}",
            )


def test_tzolis_like_starter_gets_a_flagged_low_projection_not_a_confident_one(tmp_path):
    """No previous-PL rate or appearance history, but a real, priced, selectable
    squad member -- must be RESCUED by the broad empirical-prior cohort (never
    silently excluded), and the resulting low number must carry the flags that
    explain why it is low, so it is never presented as a confident zero."""
    database_path = tmp_path / "baseline.duckdb"
    _seed_common(database_path)
    # MIN_RATE_PRIOR_COHORT (10) is stricter than MIN_APPEARANCE_PRIOR_COHORT
    # (5) -- both empirical-prior fallbacks (appearance AND rate) must
    # activate for Tzolis to be rescued rather than excluded, since he is
    # missing history on both sides.
    _seed_cohort(database_path, count=10)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES (
                'snapshot', '2026-27', 557, 439509, 'Christos', 'Tzolis', 'Tzolis',
                1, 'MID', 6.5, 'a'
            );

            -- The upstream appearance projection pipeline DOES produce a row
            -- for this player (the real Tzolis has appearance_status =
            -- "appearance_available" in the reviewed coverage audit) with a
            -- resolved availability_probability, but with the derived
            -- start/substitute/sixty/expected-minutes fields left NULL --
            -- there is no previous-PL appearance HISTORY row to compute them
            -- from (mirrors `docs/PIPELINE_ARCHITECTURE.md`'s own note:
            -- "unmatched history produces null projection fields"). This is
            -- exactly what makes baseline_pipeline.py's own
            -- EMPIRICAL_APPEARANCE_PRIOR cohort rescue activate below, rather
            -- than an invented confident number. Deliberately no
            -- player_appearance_history and no player_rate_history row at
            -- all for player_code 439509 -- the real Tzolis shape: zero
            -- previous-PL rate history, zero previous-PL appearance history.
            INSERT INTO player_appearance_projection VALUES (
                'appearance', 557, 439509, 1.0, NULL, NULL, NULL, NULL,
                NULL, NULL, NULL, NULL, '["NO_WORKBOOK_APPEARANCE_HISTORY"]'
            );
            """
        )

    result = materialize_preseason_baseline(
        appearance_projection_run_id="appearance",
        player_rate_run_id="rates",
        team_strength_run_id="strength",
        database_path=database_path,
    )

    with duckdb.connect(str(database_path), read_only=True) as connection:
        row = connection.execute(
            "SELECT expected_minutes, data_quality_flags FROM player_fixture_projection "
            "WHERE model_run_id = ? AND player_code = 439509",
            [result.model_run_id],
        ).fetchone()
        gap = connection.execute(
            "SELECT 1 FROM baseline_projection_gap WHERE model_run_id = ? AND fpl_id = 557",
            [result.model_run_id],
        ).fetchone()

    assert gap is None, "a rescuable player must not be silently excluded"
    assert row is not None
    expected_minutes, flags_json = row
    flags = set(json.loads(flags_json))
    # The projection must exist and be non-negative, but this test does NOT
    # assert it is "correct" -- docs/research/selected_squad_player_rate_evidence_2026_27.json
    # explicitly defers that until an external-league translation policy is
    # backtested. What IS asserted is that a manager can never see this
    # number without also seeing why it is unreliable.
    assert expected_minutes is not None and expected_minutes >= 0.0
    assert "NO_PREVIOUS_PL_PLAYER_RATE_HISTORY" in flags
    assert "EMPIRICAL_APPEARANCE_PRIOR" in flags or "EMPIRICAL_PLAYER_RATE_PRIOR" in flags


def test_current_only_fulham_starter_is_excluded_not_defaulted(tmp_path):
    """A new-to-the-Premier-League signing with NO appearance data anywhere
    (not even a workbook history row) must be excluded from
    player_fixture_projection entirely -- never given a fabricated minutes
    figure. Mirrors the real Palacios/Charles/Gonzalo (Fulham) shape from the
    2026-08-20 coverage audit: MISSING_APPEARANCE_PROJECTION plus
    NO_PREVIOUS_PL_PLAYER_RATE_HISTORY."""
    database_path = tmp_path / "baseline.duckdb"
    _seed_common(database_path)
    _seed_cohort(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES (
                'snapshot', '2026-27', 570, 571779, 'Josh', 'Palacios', 'Palacios',
                1, 'MID', 5.5, 'a'
            )
            """
        )
        # No player_appearance_projection row at all for fpl_id 570 -- the
        # real MISSING_APPEARANCE_PROJECTION shape (the appearance projection
        # pipeline itself produced nothing for this player, not merely an
        # unusable one).

    result = materialize_preseason_baseline(
        appearance_projection_run_id="appearance",
        player_rate_run_id="rates",
        team_strength_run_id="strength",
        database_path=database_path,
    )

    with duckdb.connect(str(database_path), read_only=True) as connection:
        row = connection.execute(
            "SELECT 1 FROM player_fixture_projection WHERE model_run_id = ? AND player_code = 571779",
            [result.model_run_id],
        ).fetchone()
        gap = connection.execute(
            "SELECT data_quality_flags FROM baseline_projection_gap "
            "WHERE model_run_id = ? AND fpl_id = 570",
            [result.model_run_id],
        ).fetchone()

    assert row is None, "a player with no appearance data anywhere must never get a fabricated row"
    assert gap is not None
    flags = set(json.loads(gap[0]))
    assert "MISSING_APPEARANCE_PROJECTION" in flags
    assert "NO_PREVIOUS_PL_PLAYER_RATE_HISTORY" in flags


def test_henderson_welbeck_like_reviewed_override_beats_stale_history(tmp_path):
    """An established player with strong historical rate/appearance data can
    still carry a genuine current-season doubt (injury, rotation) that a raw
    history-derived projection alone would not capture. A reviewed
    appearance-scenario override, applied through the real appearance
    projection materialiser, must override the history-derived scenario --
    this is the decision-safety mechanism, not a claim about either real
    player's current standing."""
    database_path = tmp_path / "baseline.duckdb"
    _seed_common(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES (
                'snapshot', '2026-27', 136, 100136, 'Danny', 'Welbeck', 'Welbeck',
                1, 'FWD', 6.0, 'd'
            );

            -- Strong historical rate/appearance data -- exactly the trap: a
            -- naive history-only model would read this player as a nailed-on
            -- starter, ignoring this season's real rotation/injury doubt.
            INSERT INTO player_appearance_history VALUES (
                'appearance_history', 100136, 'Danny Welbeck', 32, 2, 1, 78.0, 15.0
            );
            INSERT INTO player_rate_history VALUES (
                'rates', 100136, 'Danny Welbeck', 'FWD', 2900, 32, 0, 2, 0, 12, 400,
                2900, 8.0, 3.0, 2900, 8.0, 3.0, 0, 0, 0, 0, '[]'
            );

            INSERT INTO player_availability_resolution VALUES (
                'availability', 136, 100136, 'd', 75, 0.75, TRUE,
                'official_fpl_status', NULL, 'Wrist injury - 75% chance of playing', '[]'
            );
            """
        )

    baseline_without_override = materialize_preseason_appearance(
        target_gameweek=1, previous_season="2025-26", database_path=database_path
    )
    with duckdb.connect(str(database_path), read_only=True) as connection:
        history_only_minutes = connection.execute(
            "SELECT expected_minutes FROM player_appearance_projection "
            "WHERE projection_run_id = ? AND fpl_id = 136",
            [baseline_without_override.projection_run_id],
        ).fetchone()[0]
    # 32 starts at 78 minutes each with no reviewed doubt reads as a
    # near-certain heavy-minutes starter -- the trap this regression case
    # protects against.
    assert history_only_minutes > 45.0

    # A manager reviews current news (a wrist injury, or a documented
    # rotation pattern) and stores an appearance-scenario override BEFORE the
    # SAME availability snapshot's own appearance projection is (re)built --
    # exactly the flow context/minutes.py and the `rotation_risk` preset
    # exist for.
    override = create_appearance_scenario_override(
        player_code=100136,
        target_gameweek=1,
        observed_at=datetime.fromisoformat("2026-08-18T09:30:00+07:00"),
        scenario=apply_appearance_scenario_preset(ROTATION_RISK),
        source="reviewed_team_news",
        rationale="Managed carefully after a lay-off; not a guaranteed starter despite history.",
    )
    store_result = store_appearance_scenario_override(override, database_path=database_path)
    assert store_result.requires_pipeline_refresh is True

    # Rebuild appearance projection under a NEW availability resolution run
    # (an override never mutates an existing immutable projection run) so the
    # override is actually picked up.
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            f"""
            INSERT INTO availability_resolution_run (
                resolution_run_id, source_ingestion_run_id, target_gameweek,
                as_of, deadline, policy_version, status
            ) VALUES (
                'availability_v2', 'snapshot', 1, '2026-08-18T10:00:00+07:00',
                '{_DEADLINE}', 'test_v2', 'completed'
            );
            INSERT INTO player_availability_resolution VALUES (
                'availability_v2', 136, 100136, 'd', 75, 0.75, TRUE,
                'official_fpl_status', NULL, 'Wrist injury - 75% chance of playing', '[]'
            );
            """
        )
    refreshed = materialize_preseason_appearance(
        target_gameweek=1, previous_season="2025-26", database_path=database_path
    )

    with duckdb.connect(str(database_path), read_only=True) as connection:
        overridden_minutes, flags_json = connection.execute(
            "SELECT expected_minutes, data_quality_flags FROM player_appearance_projection "
            "WHERE projection_run_id = ? AND fpl_id = 136",
            [refreshed.projection_run_id],
        ).fetchone()

    assert store_result.override_id == override.override_id
    flags = set(json.loads(flags_json))
    assert "REVIEWED_APPEARANCE_SCENARIO_OVERRIDE" in flags
    assert f"APPEARANCE_SCENARIO_OVERRIDE_ID={override.override_id}" in flags
    # The reviewed rotation-risk preset's expected minutes must actually
    # replace the confident history-only figure, not merely be recorded
    # alongside it.
    assert overridden_minutes < history_only_minutes
