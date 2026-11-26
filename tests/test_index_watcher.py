import sys
from pathlib import Path
from threading import Event
from time import monotonic

import pytest
from starlette.testclient import TestClient

from obsidian_thing.embeddings import Embedder, EmbeddingError
from obsidian_thing.server import create_app


def _until(check, seconds=5):
    end = monotonic() + seconds
    while not check():
        assert monotonic() < end, 'The search index did not converge'
        Event().wait(0.02)


@pytest.mark.skipif(sys.platform != 'linux', reason='This test requires Linux file events')
def test_local_watcher_indexes_without_embedding_requests(tmp_path: Path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    (root / 'old.md').write_text('firsttoken')
    monkeypatch.setenv('TEST_WATCH_TOKEN', 'synthetic-token')
    operations = []

    def offline(self, text, operation):
        operations.append(operation)
        raise EmbeddingError('offline')

    monkeypatch.setattr(Embedder, 'embed', offline)
    app = create_app({'vaults': {'notes': dict(root=str(root), allowed=['.'], token_env='TEST_WATCH_TOKEN',
        indexing=dict(auto=True, respect_gitignore=True, scan_seconds=300),
        embeddings=dict(provider='ollama', model='synthetic', dimensions=2, state_dir=str(state)))}})
    url = '/vaults/notes/mcp'
    headers = {'Authorization': 'Bearer synthetic-token', 'Accept': 'application/json, text/event-stream',
               'MCP-Protocol-Version': '2025-11-25'}
    with TestClient(app, base_url='http://localhost') as client:
        def search(term):
            response = client.post(url, headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'obsidian.query', 'arguments': {'text': term}}})
            return [hit['path'] for hit in response.json()['result']['structuredContent']['matches']]

        _until(lambda: search('firsttoken') == ['old.md'])
        (root / '.editor.tmp').write_text('secondtoken')
        (root / '.editor.tmp').replace(root / 'old.md')
        _until(lambda: search('secondtoken') == ['old.md'])
        (root / '.gitignore').write_text('new.md\n')
        (root / 'old.md').rename(root / 'new.md')
        _until(lambda: not search('secondtoken'))
        (root / '.gitignore').write_text('')
        _until(lambda: search('secondtoken') == ['new.md'])
        root.rename(tmp_path / 'detached')
        root.mkdir()
        (root / 'replacement.md').write_text('thirdtoken')
        _until(lambda: search('thirdtoken') == ['replacement.md'])
        assert not search('secondtoken')
    assert set(operations) == {'query'}
