import json

import duckdb
import pytest

from fpl_model.model.baseline_pipeline import materialize_inseason_baseline
from tests.test_baseline_pipeline import _seed_inseason_baseline


def test_final_current_rates_change_baseline_and_are_pinned_to_model_identity(tmp_path):
    database = _seed_inseason_baseline(tmp_path)
    args = dict(target_gameweek=2, appearance_projection_run_id='appearance2',
                player_rate_run_id='rates', team_strength_run_id='strength2',
                context_feature_run_id='context2', database_path=database)
    # Move the test anchor after GW1 and before GW2's deadline.
    with duckdb.connect(str(database)) as c:
        c.execute("UPDATE context_feature_run SET as_of='2026-08-26T09:00:00+07:00' WHERE context_run_id='context2'")
        c.execute("UPDATE inseason_appearance_run SET as_of='2026-08-26T09:00:00+07:00' WHERE projection_run_id='appearance2'")
    frozen = materialize_inseason_baseline(**args)
    with duckdb.connect(str(database)) as c:
        c.execute("""INSERT INTO fpl_event_live_run VALUES (
            'official1', 'snapshot', '2026-27', 1, '2026-08-25T00:00:00Z',
            'test.json', 'sha', TRUE, TRUE, 1, 'completed', current_timestamp)
        """)
        c.execute("""INSERT INTO player_gameweek_stat VALUES (
            'official1', 1, 519634, TRUE, 90, 1, 1, 0, 0, 0, 0, 5, 20, 1,
            1.1, 0.4, 0.2, 12, FALSE, '[]')""")
    updated = materialize_inseason_baseline(**args)
    assert updated.model_run_id != frozen.model_run_id
    assert materialize_inseason_baseline(**args).model_run_id == updated.model_run_id
    with duckdb.connect(str(database), read_only=True) as c:
        rows = c.execute("SELECT model_run_id, final_xpts FROM player_fixture_projection WHERE player_code=519634 AND model_run_id IN (?, ?)",
                         [frozen.model_run_id, updated.model_run_id]).fetchall()
        scores = dict(rows)
        assert scores[updated.model_run_id] > scores[frozen.model_run_id]
        lineage = c.execute('SELECT rate_run_id, previous_rate_run_id, final_live_run_ids FROM baseline_current_rate_lineage WHERE model_run_id=?', [updated.model_run_id]).fetchone()
        assert lineage[1] == 'rates'
        assert json.loads(lineage[2]) == ['official1']
        rate = c.execute('SELECT shrunk_expected_goals_per_90 FROM current_season_player_rate WHERE rate_run_id=?', [lineage[0]]).fetchone()[0]
        assert rate == pytest.approx(1.1 / 11)
    # A later/future capture cannot alter the frozen anchor or its identity.
    with duckdb.connect(str(database)) as c:
        c.execute("""INSERT INTO fpl_event_live_run VALUES (
            'future', 'snapshot', '2026-27', 1, '2026-08-27T00:00:00Z',
            'future.json', 'sha2', TRUE, TRUE, 1, 'completed', current_timestamp)""")
    assert materialize_inseason_baseline(**args).model_run_id == updated.model_run_id
