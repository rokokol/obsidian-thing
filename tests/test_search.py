import json
import sqlite3
from io import BytesIO

import pytest

from starlette.testclient import TestClient

from obsidian_thing.embeddings import Embedder
from obsidian_thing.search import SearchIndex
from obsidian_thing.server import create_app
from obsidian_thing.vault import Vault, VaultError


def test_index_reuses_vectors_and_hides_stale_or_excluded_paths(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    for name, text in [('a.md', 'apple'), ('b.txt', 'apple'), ('c.md', 'pear'),
                       ('excluded.md', 'secret'), ('picture.png', 'text inside an image')]:
        (root / name).write_text(text)
    calls = []

    def respond(request, *, timeout):
        text = json.loads(request.data)['input']
        calls.append(text)
        vector = [0, 1] if text == 'pear' else [1, 0]
        return BytesIO(json.dumps({'embeddings': [vector]}).encode())

    index = SearchIndex(Vault(root, ['.']), dict(provider='ollama', model='synthetic',
                        dimensions=2, exclude_paths=['excluded.md']), state)
    monkeypatch.setattr(index.embedder.http, 'open', respond)
    for path in ('excluded.md', 'picture.png', '../secrets.md'):
        with pytest.raises(VaultError):
            index.index(path)
    assert not calls
    for path in ('a.md', 'b.txt', 'c.md', 'a.md'):
        index.index(path)
    assert calls == ['apple', 'pear']
    assert [hit['path'] for hit in index.related('a.md', 5, 0.1)['matches']] == ['b.txt']
    (root / 'b.txt').write_text('edited')
    assert [hit['path'] for hit in index.query('fruit', 5)['matches']] == ['a.md', 'c.md']
    (root / 'a.md').write_text('pear')
    assert [hit['path'] for hit in index.related('a.md', 5, 0.1)['matches']] == ['c.md']
    assert calls == ['apple', 'pear', 'fruit']
    with sqlite3.connect(state / 'spend.sqlite3') as db:
        assert db.execute('SELECT operation, count(*) FROM calls GROUP BY operation ORDER BY operation').fetchall() == [
            ('index', 2), ('query', 1),
        ]


def test_search_tools_use_the_authenticated_vault(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'note.md').write_text('synthetic')
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    monkeypatch.setenv('TEST_SEARCH_TOKEN', 'synthetic-token')
    monkeypatch.setattr(Embedder, 'embed', lambda self, text, operation: [1.0, 0.0])
    config = {'vaults': {'notes': dict(root=str(root), allowed=['.'], token_env='TEST_SEARCH_TOKEN',
              embeddings=dict(provider='ollama', model='synthetic', dimensions=2, state_dir=str(state)))}}
    app = create_app(config)
    url = '/vaults/notes/mcp'
    headers = {'Authorization': 'Bearer synthetic-token', 'Accept': 'application/json, text/event-stream',
               'MCP-Protocol-Version': '2025-11-25'}
    with TestClient(app, base_url='http://localhost') as client:
        def call(name, arguments):
            response = client.post(url, headers=headers, json={'jsonrpc': '2.0', 'id': 1,
                                   'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})
            assert response.status_code == 200
            return response.json()['result']['structuredContent']

        assert client.post(url, json={}).status_code == 401
        assert call('wiki.index', {'path': 'note.md'})['chunks'] == 1
        assert call('wiki.query', {'text': 'related text'}) == {'matches': [{'path': 'note.md', 'score': 1.0}]}
        assert call('wiki.related', {'path': 'note.md'}) == {'matches': []}
