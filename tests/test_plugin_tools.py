import pytest
from starlette.testclient import TestClient

from obsidian_thing.embeddings import Embedder
from obsidian_thing.server import create_app


@pytest.mark.parametrize('enabled', [True, False])
def test_map_requires_plugin_opt_in_and_embeddings(tmp_path, monkeypatch, enabled):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'note.md').write_text('Synthetic note')
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    monkeypatch.setenv('TEST_PLUGIN_TOKEN', 'example-token')
    monkeypatch.setattr(Embedder, 'embed', lambda self, text, operation: [1.0, 0.0])
    app = create_app({'vaults': {'notes': {
        'root': str(root), 'allowed': ['.'], 'token_env': 'TEST_PLUGIN_TOKEN',
        'embeddings': {'enabled': enabled, 'state_dir': str(state), 'provider': 'ollama',
                       'model': 'synthetic', 'dimensions': 2, 'min_chunk_words': 0},
    }}})
    with TestClient(app, base_url='http://localhost') as client:
        unauthorized = client.post('/vaults/notes/mcp', headers={'X-Obsidian-Thing-Client': 'plugin'},
                                   json=dict(jsonrpc='2.0', id=1, method='tools/list'))
        assert unauthorized.status_code == 401
        for plugin in (False, True, False):
            headers = {'Authorization': 'Bearer example-token', 'Accept': 'application/json, text/event-stream'}
            if plugin:
                headers['X-Obsidian-Thing-Client'] = 'plugin'

            def rpc(method, params):
                response = client.post('/vaults/notes/mcp', headers=headers,
                                       json=dict(jsonrpc='2.0', id=1, method=method, params=params))
                assert response.status_code == 200
                return response.json()['result']

            tools = {item['name'] for item in rpc('tools/list', {})['tools']}
            assert ('obsidian.related' in tools) is enabled
            assert ('obsidian.map' in tools) is (plugin and enabled)
            result = rpc('tools/call', {'name': 'obsidian.map', 'arguments': {'path': 'note.md'}})
            assert result['isError'] is not (plugin and enabled)
            if plugin and enabled:
                assert result['structuredContent']['points'][0]['path'] == 'note.md'
