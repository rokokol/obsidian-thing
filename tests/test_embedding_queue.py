from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

from starlette.testclient import TestClient

from obsidian_thing.embedding_schedule import EmbeddingSchedule
from obsidian_thing.embeddings import Embedder, EmbeddingError
from obsidian_thing.search import SearchIndex
from obsidian_thing.server import create_app
from obsidian_thing.vault import Vault


def test_queue_survives_restart_and_only_sends_current_uncached_fragments(tmp_path: Path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    calls = []

    def embed(self, text, operation):
        calls.append((text, operation))
        return [1.0, 0.0]

    monkeypatch.setattr(Embedder, 'embed', embed)
    config = dict(provider='ollama', model='synthetic', dimensions=2, background=True)
    def open_index():
        return SearchIndex(Vault(root, ['.']), config, state, indexing={'respect_gitignore': True})

    index = open_index()
    note = root / 'note.md'
    note.write_text('original')
    index.index('note.md')
    calls.clear()
    note.write_text('intermediate')
    index.reconcile_local(['note.md'])
    note.write_text('final')
    (root / 'removed.md').write_text('will be deleted')
    (root / 'excluded.md').write_text('will be excluded')
    index.reconcile_local()
    (root / 'removed.md').unlink()
    (root / '.gitignore').write_text('excluded.md\n')
    assert calls == []

    index = open_index()
    assert {path for path, _ in index.pending()} == {'note.md'}
    index.run_pending()
    assert calls == [('final', 'background')]
    assert index.pending() == []
    note.rename(root / 'moved.md')
    index.reconcile_local()
    assert {path for path, _ in index.pending()} == {'moved.md'}
    index.run_pending()
    assert calls == [('final', 'background')]
    assert index.pending() == []
    assert [hit['path'] for hit in index.query('final', 10)['matches']] == ['moved.md']


def test_queue_retains_failed_work_and_manual_index_drains_it(tmp_path: Path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    (root / 'note.md').write_text('text')
    config = dict(provider='ollama', model='synthetic', dimensions=2)
    index = SearchIndex(Vault(root, ['.']), config, state)
    index.reconcile_local()

    def offline(self, text, operation):
        raise EmbeddingError('offline')

    monkeypatch.setattr(Embedder, 'embed', offline)
    assert not index.run_pending()
    assert [path for path, _ in index.pending()] == ['note.md']
    monkeypatch.setattr(Embedder, 'embed', lambda self, text, operation: [1.0, 0.0])
    index.index('note.md')
    assert index.pending() == []


def test_http_lifespan_runs_enabled_schedule(tmp_path: Path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    (root / 'note.md').write_text('scheduled text')
    monkeypatch.setenv('TEST_QUEUE_TOKEN', 'synthetic-token')
    calls = []
    ran = Event()
    next_run = EmbeddingSchedule.next_run

    def trigger_once(self, now):
        assert next_run(self, datetime(2026, 1, 1, tzinfo=timezone.utc)).hour == 3
        return now + timedelta(days=1) if calls else now

    def embed(self, text, operation):
        calls.append((text, operation))
        ran.set()
        return [1.0, 0.0]

    monkeypatch.setattr(EmbeddingSchedule, 'next_run', trigger_once)
    monkeypatch.setattr(Embedder, 'embed', embed)
    embeddings = dict(provider='ollama', model='synthetic', dimensions=2, background=True, state_dir=str(state))
    app = create_app({'vaults': {'queue': dict(root=str(root), allowed=['.'], token_env='TEST_QUEUE_TOKEN',
                                                embeddings=embeddings)}})
    with TestClient(app, base_url='http://localhost'):
        assert ran.wait(5)
    assert calls == [('scheduled text', 'background')]
