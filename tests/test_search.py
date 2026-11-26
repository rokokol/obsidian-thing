import json
import sqlite3
from io import BytesIO

import pytest

from starlette.testclient import TestClient

from obsidian_thing.embeddings import Embedder, EmbeddingError
from obsidian_thing.search import SearchIndex
from obsidian_thing.server import create_app
from obsidian_thing.vault import Vault, VaultError


def test_query_combines_exact_and_semantic_matches_and_falls_back(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    exact = root / 'z-exact.md'
    exact.write_text('# Setup\n\nSet section_chunks to false.\n')
    (root / 'a-semantic.md').write_text('Split the document into paragraphs.')
    (root / 'partial.md').write_text('Set section mode.')

    def embed(self, text, operation):
        if operation == 'query' or 'paragraphs' in text:
            return [1.0, 0.0]
        if 'section_chunks' in text:
            return [0.8, 0.6]
        return [0.0, 1.0]

    monkeypatch.setattr(Embedder, 'embed', embed)
    config = dict(provider='ollama', model='synthetic', dimensions=2)
    index = SearchIndex(Vault(root, ['.']), config, state)
    for path in ('z-exact.md', 'a-semantic.md', 'partial.md'):
        index.index(path)
    matches = index.query('section_chunks', 2)['matches']
    assert [item['path'] for item in matches] == ['z-exact.md', 'a-semantic.md']
    assert matches[0]['headings'] == ['Setup'] and matches[0]['line_start'] == 3

    def offline(self, text, operation):
        raise EmbeddingError('offline')

    monkeypatch.setattr(Embedder, 'embed', offline)
    result = index.query('section_chunks "', 10)
    assert result.get('vector_unavailable') is True
    assert [hit['path'] for hit in result['matches']] == ['z-exact.md']
    restricted = SearchIndex(Vault(root, ['a-semantic.md']), config, state)
    assert restricted.query('section_chunks', 10)['matches'] == []
    exact.write_text('Changed.')
    assert index.query('section_chunks', 10)['matches'] == []
    exact.write_text('FTS works without a model.')
    with pytest.raises(EmbeddingError):
        index.index('z-exact.md')
    assert [hit['path'] for hit in index.query('FTS', 10)['matches']] == ['z-exact.md']


def test_index_embeds_section_context_and_returns_best_location(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    (root / 'note.md').write_text(
        '---\ntags: [pediatric]\naliases: [handbook]\nsecret: do not send\n---\n'
        '# Treatment\nIntro\n\n## Dosage\n\n```markdown\n# Not a section\n```\n\n'
        + 'paragraph ' * 85 + '\n\n' + 'target ' * 120 + '\n'
    )
    calls = []

    def embed(self, text, operation):
        calls.append((text, operation))
        return [0.0, 1.0] if 'target' in text else [1.0, 0.0]

    monkeypatch.setattr(Embedder, 'embed', embed)
    config = dict(provider='ollama', model='synthetic', dimensions=2)
    vault = Vault(root, ['.'])
    index = SearchIndex(vault, config, state)
    index.index('note.md')
    payloads = [text for text, operation in calls if operation == 'index']
    assert len(payloads) == 3
    assert all('pediatric' in text and 'handbook' in text for text in payloads)
    assert all('do not send' not in text and len(text) <= index.chunk_size for text in payloads)
    assert 'Treatment' in payloads[-1] and 'Dosage' in payloads[-1]
    assert 'paragraph' not in payloads[-1]
    hit, = index.query('target', 5)['matches']
    assert hit == {'path': 'note.md', 'score': 1.0, 'headings': ['Treatment', 'Dosage'],
                   'line_start': 17, 'line_end': 17}
    flat = SearchIndex(vault, dict(config, section_chunks=False), state)
    assert flat.query('target', 5) == {'matches': []}
    calls.clear()
    flat.index('note.md')
    flat_payloads = [text for text, operation in calls if operation == 'index']
    assert len(flat_payloads) == 2
    assert all('pediatric' in text and 'do not send' not in text for text in flat_payloads)
    flat_hit, = flat.query('target', 5)['matches']
    assert flat_hit['headings'] == [] and flat_hit['line_end'] == 17
    assert index.query('target', 5)['matches'] == [hit]


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


def test_index_and_search_enforce_the_current_vault_allowlist(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    for name in ('allowed.md', 'revoked.md', 'replacement.md'):
        (root / name).write_text(name)
    calls = []

    def embed(self, text, operation):
        calls.append((text, operation))
        return [1.0, 0.0]

    monkeypatch.setattr(Embedder, 'embed', embed)
    config = dict(provider='ollama', model='synthetic', dimensions=2)
    original = SearchIndex(Vault(root, ['.']), config, state)
    for name in ('allowed.md', 'revoked.md', 'replacement.md'):
        original.index(name)
    index = SearchIndex(Vault(root, ['allowed.md', 'replacement.md']), config, state)
    (root / 'replacement.md').rename(root / 'private.md')
    (root / 'replacement.md').symlink_to('private.md')
    calls.clear()

    for name in ('revoked.md', 'replacement.md'):
        with pytest.raises(VaultError):
            index.index(name)
        with pytest.raises(VaultError):
            index.related(name, 10, 0)
    assert not calls
    assert index.query('query', 10) == {'matches': [{'path': 'allowed.md', 'score': 1.0, 'headings': [], 'line_start': 1, 'line_end': 1}]}
    assert index.related('allowed.md', 10, 0) == {'matches': []}
    assert calls == [('query', 'query')]


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
        assert call('obsidian.index', {'path': 'note.md'})['chunks'] == 1
        assert call('obsidian.query', {'text': 'related text'}) == {'matches': [{'path': 'note.md', 'score': 1.0, 'headings': [], 'line_start': 1, 'line_end': 1}]}
        assert call('obsidian.related', {'path': 'note.md'}) == {'matches': []}
