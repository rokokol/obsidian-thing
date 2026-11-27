import time
from contextlib import contextmanager

import pytest
from starlette.testclient import TestClient

from obsidian_thing.embedding_schedule import EmbeddingSchedule
from obsidian_thing.server import create_app


@pytest.fixture
def vault_config(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'note.md').write_text('synthetic note')
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    monkeypatch.setenv('TEST_SEARCH_MODE_TOKEN', 'synthetic-token')
    settings = dict(root=str(root), allowed=['.'], token_env='TEST_SEARCH_MODE_TOKEN')
    return {'vaults': {'notes': settings}}, settings, state


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
            result = response.json()['result']
            assert not result.get('isError'), result
            return result

        yield request


def call(request, name, **arguments):
    return request('tools/call', dict(name='obsidian.' + name, arguments=arguments))['structuredContent']


@pytest.mark.parametrize('auto', [False, True])
def test_disabled_embeddings_keep_lexical_search(vault_config, monkeypatch, auto):
    config, settings, state = vault_config
    settings['indexing'] = dict(auto=auto)
    settings['embeddings'] = dict(enabled=False, state_dir=str(state), provider='openrouter',
                                  model='test', dimensions=2, route='test', key_env='MISSING_SEARCH_MODE_KEY',
                                  allow_cloud=True, background=True,
                                  exclude_paths=['note.md'])
    monkeypatch.delenv('MISSING_SEARCH_MODE_KEY', raising=False)
    monkeypatch.setattr(EmbeddingSchedule, 'start', lambda self: pytest.fail('Unexpected embedding schedule'))
    with rpc(config) as request:
        names = {tool['name'] for tool in request('tools/list', {})['tools']}
        assert {'obsidian.read', 'obsidian.index', 'obsidian.query'} <= names
        assert 'obsidian.related' not in names
        if not auto:
            assert call(request, 'index', path='note.md')['chunks'] == 1
        deadline = time.monotonic() + 5
        while True:
            result = call(request, 'query', text='synthetic')
            if not auto or result['matches'] or time.monotonic() >= deadline:
                break
            time.sleep(0.025)
        assert [hit['path'] for hit in result['matches']] == ['note.md']
        assert 'vector_unavailable' not in result
    assert not (state / 'spend.sqlite3').exists()
    assert not (state / 'embedding.lock').exists()


def test_disabled_embeddings_start_without_index_state(vault_config):
    config, settings, _ = vault_config
    settings['embeddings'] = dict(enabled=False)
    with rpc(config) as request:
        assert call(request, 'read', path='note.md')['content'] == 'synthetic note'
        assert 'obsidian.index' not in {tool['name'] for tool in request('tools/list', {})['tools']}


def test_enabled_must_be_a_boolean(vault_config):
    config, settings, _ = vault_config
    settings['embeddings'] = {'enabled': 'false'}
    with pytest.raises(ValueError, match=r'embeddings\.enabled'):
        create_app(config)
