import json

from fastapi.testclient import TestClient

from api.index import _bootstrap, app
from tests.test_webapp_service import _release_file


def test_wildcard_api_respects_horizon_and_preserves_ft(tmp_path, monkeypatch):
    path = tmp_path / 'release.json'
    _release_file(path)
    # Fixture release has GW2-4; extend it with two fixture-only weeks.
    payload = json.loads(path.read_text(encoding='utf-8'))
    payload['release']['model_runs'].extend([
        {'gameweek': 5, 'model_run_id': 'run_gw5'}, {'gameweek': 6, 'model_run_id': 'run_gw6'},
    ])
    for player in payload['players']:
        player['gameweeks']['5'] = player['gameweeks']['4']
        player['gameweeks']['6'] = player['gameweeks']['4']
    path.write_text(json.dumps(payload), encoding='utf-8')
    monkeypatch.setenv('FPL_WEB_RELEASE_PATH', str(path))
    monkeypatch.setenv('FPL_DATABASE_PATH', str(tmp_path / 'absent.duckdb'))
    _bootstrap.cache_clear()
    try:
        with TestClient(app) as client:
            response = client.post('/api/recommend/wildcard', json={
                'fpl_ids': list(range(1, 16)), 'free_transfers': 2,
                'horizon_length': 5, 'roll_after_wildcard': 3,
                'locked_fpl_ids': list(range(1, 16)),
            })
            assert response.status_code == 200, response.text
            result = response.json()
            assert result['horizon'] == [2, 3, 4, 5, 6]
            assert result['decision_status'] == 'RESEARCH_ONLY'
            assert result['decision_receipt']['decision_type'] == 'wildcard_comparison'
            steps = result['paths'][-1]['steps']
            assert [s['free_transfers_after'] for s in steps[:4]] == [2, 3, 4, 5]
            too_short = client.post('/api/recommend/wildcard', json={
                'fpl_ids': list(range(1, 16)), 'horizon_length': 2, 'roll_after_wildcard': 3,
            })
            assert too_short.status_code == 422
            spent = client.post('/api/recommend/wildcard', json={
                'fpl_ids': list(range(1, 16)), 'horizon_length': 3, 'roll_after_wildcard': 1,
                'chip_status': {'wildcard': 'used'},
            })
            assert spent.status_code == 422
            assert 'marked used' in spent.json()['detail']
    finally:
        _bootstrap.cache_clear()
