import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from starlette.testclient import TestClient

from obsidian_thing.embeddings import Embedder
from obsidian_thing.search import SearchIndex
from obsidian_thing.server import create_app
from obsidian_thing.vault import Vault


VECTORS = {
    'source': [1.0, 0.0, 0.0],
    'near': [1.0, 0.1, 0.0],
    'middle': [0.6, 0.8, 0.0],
    'far': [0.0, 0.0, 1.0],
}


def _embed(self, text, operation):
    for name, vector in VECTORS.items():
        if name in text:
            return vector
    return [1.0, 0.0, 0.0]


def _index(tmp_path, monkeypatch, *, embeddings=None, files=None, indexing=None, vectors=True):
    root = tmp_path / 'vault'
    root.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    for path, content in (files or {'source.md': 'source word', 'near.md': 'near word'}).items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    monkeypatch.setattr(Embedder, 'embed', _embed)
    config = dict(provider='ollama', model='synthetic', dimensions=3, state_dir=str(state))
    index = SearchIndex(Vault(root, ['.']), (embeddings or {}) | config, state,
                        indexing=indexing or {}, vectors=vectors)
    return root, state, index


def test_map_lays_out_the_neighborhood_and_keeps_original_scores(tmp_path, monkeypatch):
    root, _, index = _index(tmp_path, monkeypatch, files={
        'source.md': 'source anchor', 'near.md': 'near note', 'middle.md': 'middle note',
        'far.md': 'far note', 'outside.md': 'outside note',
    })
    for path in ('near.md', 'middle.md', 'far.md', 'outside.md'):
        index.index(path)

    related = index.related('source.md', 10, -1)['matches']
    first = index.map('source.md', 10, -1)

    assert set(first) == {'points', 'explained', 'dimensions'}
    assert first['dimensions'] == 3
    assert len(first['explained']) == 2
    points = {point['path']: point for point in first['points']}
    # The source comes first and the neighbours keep the order of the rank score
    assert [point['path'] for point in first['points']] == ['source.md', *(m['path'] for m in related)]
    assert set(points) == {match['path'] for match in related} | {'source.md'}
    assert points['source.md'].get('source') is True
    assert points['source.md']['score'] == 1.0
    # The neighbourhood map ranks in the original space, so its scores are the related scores
    assert {path: point['score'] for path, point in points.items() if path != 'source.md'} == {
        match['path']: match['score'] for match in related}
    for point in points.values():
        assert -1 <= point['x'] <= 1 and -1 <= point['y'] <= 1
        assert all(isinstance(value, float) for value in (point['x'], point['y']))

    again = index.map('source.md', 10, -1)
    assert {(p['path'], p['x'], p['y']) for p in again['points']} == {
        (p['path'], p['x'], p['y']) for p in first['points']}


def test_map_uses_the_limit_and_rejects_a_bad_one(tmp_path, monkeypatch):
    root, _, index = _index(tmp_path, monkeypatch, files={
        'source.md': 'source anchor', 'near.md': 'near note', 'middle.md': 'middle note',
        'far.md': 'far note',
    })
    for path in ('near.md', 'middle.md', 'far.md'):
        index.index(path)
    assert len(index.map('source.md', 1, -1)['points']) == 2
    assert len(index.map('source.md', 2, -1)['points']) == 3
    with pytest.raises(ValueError, match='limit'):
        index.map('source.md', 0)


def test_map_filters_results_without_extending_the_scope(tmp_path, monkeypatch):
    root, _, index = _index(tmp_path, monkeypatch, files={
        'source.md': 'source anchor', 'notes/near.md': 'near note', 'middle.md': 'middle note',
    })
    for path in ('notes/near.md', 'middle.md'):
        index.index(path)
    points = index.map('source.md', 10, -1, include_paths=['notes'])['points']
    assert {point['path'] for point in points} == {'source.md', 'notes/near.md'}


def test_status_reports_coverage_queue_scan_and_profile(tmp_path, monkeypatch):
    root, state, index = _index(tmp_path, monkeypatch, files={
        'source.md': 'source anchor', 'near.md': 'near note', 'skip.tmp.md': 'skipped',
        'notes/in.md': 'near in scope',
    }, embeddings={'include_paths': ['source.md', 'near.md']}, indexing={'exclude': ['*.tmp.md']})
    index.reconcile_local()
    index.index('source.md')

    report = index.status()
    assert set(report) == {'index', 'embeddings', 'spend'}
    status = report['index']
    assert status['mode'] == 'sections'
    # The skipped pattern removes the third file from what the index can hold
    assert status['files'] == 3
    assert status['excluded'] == 1
    assert status['indexed'] == 3
    assert status['unindexed'] == 0
    assert status['embedded'] == 1
    assert status['pending'] == 1
    last_scan = status['last_full_scan']
    assert isinstance(last_scan, str)
    assert datetime.fromisoformat(last_scan).tzinfo is not None

    (root / 'fresh.md').write_text('fresh note')
    assert index.status()['index']['files'] == 4
    assert index.status()['index']['unindexed'] == 1

    profile = report.get('embeddings')
    assert profile is not None
    assert profile['provider'] == 'ollama'
    assert profile['model'] == 'synthetic'
    assert profile['dimensions'] == 3
    assert profile['remote'] is False
    assert profile['daily_budget_usd'] is None
    assert len(profile['profile']) == 12


def test_status_omits_embeddings_and_spend_when_they_are_off(tmp_path, monkeypatch):
    root, state, index = _index(tmp_path, monkeypatch, vectors=False)
    index.reconcile_local()
    report = index.status()
    assert set(report) == {'index'}
    assert index.query('source', 10)['matches']


def test_status_groups_costs_by_period_day_and_kind(tmp_path, monkeypatch):
    root, state, index = _index(tmp_path, monkeypatch)
    index.index('source.md')
    now = datetime.now(timezone.utc).replace(microsecond=0)
    rows = [
        ('reported', '0.25', now),
        ('estimated', '0.5', now),
        ('unknown', None, now),
        ('local', None, now),
        ('reported', '1.5', now - timedelta(days=3)),
        ('estimated', '2', now - timedelta(days=20)),
        ('reported', '9', now - timedelta(days=60)),
    ]
    with sqlite3.connect(state / 'spend.sqlite3') as db:
        for kind, usd, at in rows:
            db.execute('INSERT INTO calls (at, operation, provider, model, latency_ms, status, cost_kind, usd) '
                       "VALUES (?, 'query', 'ollama', 'synthetic', 1, 'ok', ?, ?)",
                       (at.strftime('%Y-%m-%d %H:%M:%S'), kind, usd))

    spend = index.status().get('spend')
    assert spend is not None
    periods = {period['name']: period for period in spend['periods']}
    assert list(periods) == ['24h', '7d', '30d', 'all']
    assert Decimal(periods['24h']['reported']) == Decimal('0.25')
    assert Decimal(periods['24h']['estimated']) == Decimal('0.5')
    assert periods['24h']['unknown_calls'] == 1
    # Indexing ran locally and the stub embedder never reached the journal
    assert periods['24h']['local_calls'] == 1
    assert Decimal(periods['7d']['reported']) == Decimal('1.75')
    assert Decimal(periods['30d']['estimated']) == Decimal('2.5')
    assert Decimal(periods['all']['reported']) == Decimal('10.75')
    # The first index call is local too, and it lands in today's bucket
    assert spend['daily'][-1]['date'] == now.strftime('%Y-%m-%d')
    assert Decimal(spend['daily'][-1]['reported']) == Decimal('0.25')
    assert len(spend['daily']) == 3
    assert spend['daily'][0]['date'] < spend['daily'][-1]['date']


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'note.md').write_text('synthetic note')
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    monkeypatch.setenv('TEST_STATUS_TOKEN', 'synthetic-token')
    monkeypatch.setattr(Embedder, 'embed', lambda self, text, operation: [1.0, 0.0, 0.0])
    settings = dict(root=str(root), allowed=['.'], token_env='TEST_STATUS_TOKEN',
                    embeddings=dict(provider='ollama', model='synthetic', dimensions=3, state_dir=str(state)))
    return {'vaults': {'notes': settings}}, settings


@contextmanager
def rpc(config):
    with TestClient(create_app(config), base_url='http://localhost') as client:
        def request(method, params):
            response = client.post('/vaults/notes/mcp', headers={
                'Authorization': 'Bearer synthetic-token',
                'Accept': 'application/json, text/event-stream',
                'MCP-Protocol-Version': '2025-11-25',
            }, json=dict(jsonrpc='2.0', id=1, method=method, params=params))
            assert response.status_code == 200
            return response.json()['result']

        yield request


def test_status_and_map_follow_the_server_settings(endpoint):
    config, settings = endpoint
    with rpc(config) as request:
        names = {tool['name'] for tool in request('tools/list', {})['tools']}
        assert {'obsidian.status', 'obsidian.map', 'obsidian.related'} <= names
        call = request('tools/call', dict(name='obsidian.status', arguments={}))
        assert not call.get('isError'), call
        assert call['structuredContent']['embeddings']['dimensions'] == 3
        result = request('tools/call', dict(name='obsidian.map', arguments={'path': 'note.md'}))
        assert not result.get('isError'), result
        points = result['structuredContent']['points']
        assert [point['path'] for point in points] == ['note.md']

    settings['embeddings']['enabled'] = False
    with rpc(config) as request:
        names = {tool['name'] for tool in request('tools/list', {})['tools']}
        assert 'obsidian.status' in names and 'obsidian.map' not in names and 'obsidian.related' not in names
        call = request('tools/call', dict(name='obsidian.status', arguments={}))
        assert not call.get('isError'), call
        assert set(call['structuredContent']) == {'index'}

    del settings['embeddings']
    with rpc(config) as request:
        names = {tool['name'] for tool in request('tools/list', {})['tools']}
        assert 'obsidian.status' not in names and 'obsidian.map' not in names


def test_partial_scan_keeps_the_previous_scan_time(tmp_path, monkeypatch):
    root, state, index = _index(tmp_path, monkeypatch)
    index.reconcile_local()
    with sqlite3.connect(index.database) as db:
        db.execute("UPDATE meta SET value = '2020-01-01T00:00:00Z' WHERE key = 'last_full_scan'")
    index.reconcile_local(['source.md'])
    assert index.status()['index']['last_full_scan'] == '2020-01-01T00:00:00Z'


def test_status_drops_every_count_of_a_deleted_file(tmp_path, monkeypatch):
    root, state, index = _index(tmp_path, monkeypatch)
    index.reconcile_local()
    index.index('source.md')
    assert index.status()['index']['pending'] == 1
    (root / 'near.md').unlink()
    status = index.status()['index']
    assert (status['files'], status['indexed'], status['embedded'], status['pending']) == (1, 1, 1, 0)


def test_status_counts_a_file_without_vectors_as_unembedded(tmp_path, monkeypatch):
    root, state, index = _index(tmp_path, monkeypatch, files={'source.md': 'source word', 'empty.md': ''})
    index.reconcile_local()
    assert index.index('source.md')['chunks'] == 1
    assert index.index('empty.md')['chunks'] == 0
    status = index.status()['index']
    assert (status['indexed'], status['embedded']) == (2, 1)


def test_map_keeps_the_source_whose_chunks_cancel_out(tmp_path, monkeypatch):
    def opposite(self, text, operation):
        return [1.0, 0.0, 0.0] if 'plus' in text else [-1.0, 0.0, 0.0]

    monkeypatch.setattr(Embedder, 'embed', opposite)
    root = tmp_path / 'vault'
    root.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    (root / 'source.md').write_text('# One\n\nplus\n\n# Two\n\nminus\n')
    (root / 'near.md').write_text('plus too')
    index = SearchIndex(Vault(root, ['.']), dict(provider='ollama', model='synthetic', dimensions=3,
                        state_dir=str(state)), state)
    index.index('near.md')
    points = index.map('source.md', 10, -1)['points']
    assert [point['path'] for point in points] == ['source.md', 'near.md']
    assert points[0].get('source') is True
    assert (points[0]['x'], points[0]['y']) == (0.0, 0.0)


def test_map_compares_mean_directions_not_vector_sums(tmp_path, monkeypatch):
    monkeypatch.setattr(Embedder, 'embed', lambda self, text, operation: [1.0, 0.0, 0.0])
    root = tmp_path / 'vault'
    root.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    (root / 'source.md').write_text('# One\n\nnear\n\n# Two\n\nnear\n')
    (root / 'near.md').write_text('near')
    index = SearchIndex(Vault(root, ['.']), dict(provider='ollama', model='synthetic', dimensions=3,
                        state_dir=str(state)), state)
    index.index('near.md')
    points = index.map('source.md', 10, -1)['points']
    assert [(point['x'], point['y']) for point in points] == [(0.0, 0.0), (0.0, 0.0)]


def test_status_reports_a_broken_gitignore_instead_of_hiding_files(tmp_path, monkeypatch):
    root, state, index = _index(tmp_path, monkeypatch, indexing={'respect_gitignore': True})
    (root / '.gitignore').write_text('!\n')
    with pytest.raises(ValueError, match='ignore rules'):
        index.status()


def test_centroids_refuse_a_vector_of_the_wrong_size(tmp_path, monkeypatch):
    root, state, index = _index(tmp_path, monkeypatch, files={'source.md': 'source word'})
    embedder = index.embedder
    assert embedder is not None
    with sqlite3.connect(index.database) as db:
        db.execute('INSERT OR REPLACE INTO vectors VALUES (?, ?, ?)', (embedder.profile, 'h', json.dumps([1.0, 0.0])))
        db.execute('INSERT INTO section_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                   (embedder.profile, 1, 'source.md', 'r', 'h', 0, '[]', 1, 1))
    with pytest.raises(ValueError, match='unexpected size'):
        index._centroids(['source.md'])
