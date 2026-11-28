import sqlite3
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import pytest
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
    config = dict(provider='ollama', model='synthetic', dimensions=2, min_chunk_words=0, background=True)
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


def test_bulk_directory_move_reuses_vectors_and_updates_paths(tmp_path: Path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    (root / 'Anchor.md').write_text('Destination')
    count = 512
    paths = [f'Old/Group-{i // 64}/Set-{i // 16}/Note-{i}.md' for i in range(count)]
    for i, path in enumerate(paths):
        note = root / path
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(f'# Notes\n\nword{i} [[Anchor]] [[Note-{(i + 1) % count}]]\n')
    calls = []

    def embed(self, text, operation):
        calls.append(operation)
        return [1.0, 0.0]

    monkeypatch.setattr(Embedder, 'embed', embed)
    vault = Vault(root, ['.'])
    index = SearchIndex(vault, dict(provider='ollama', model='synthetic', dimensions=2, min_chunk_words=0), state)
    index.reconcile_local()
    assert index.run_pending()
    assert calls.count('background') == count + 1
    calls.clear()

    (root / 'Old').rename(root / 'New')
    index.reconcile_local()
    moved = {path.replace('Old/', 'New/', 1) for path in paths}
    assert {path for path, _ in index.pending()} == moved
    assert index.run_pending() and not index.pending()
    assert calls == []
    with sqlite3.connect(index.database) as db:
        for table in ('local_files', 'lexical_chunks', 'embedded_files', 'section_chunks'):
            assert {path for path, in db.execute(f'SELECT DISTINCT path FROM {table}')} == moved | {'Anchor.md'}
    assert {link['source'] for link in vault.graph.incoming('Anchor.md')['links']} == moved
    sample = paths[0].replace('Old/', 'New/', 1)
    assert [link['resolved'] for link in vault.graph.outgoing(sample)] == [['Anchor.md'], [paths[1].replace('Old/', 'New/', 1)]]
    assert index.query('word0', 1)['matches'][0]['path'] == sample


def test_queue_retains_failed_work_and_manual_index_drains_it(tmp_path: Path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    (root / 'note.md').write_text('text')
    config = dict(provider='ollama', model='synthetic', dimensions=2, min_chunk_words=0)
    index = SearchIndex(Vault(root, ['.']), config, state)
    index.reconcile_local()

    error = EmbeddingError('offline')

    def offline(self, text, operation):
        raise error

    monkeypatch.setattr(Embedder, 'embed', offline)
    with pytest.raises(EmbeddingError) as caught:
        index.index('note.md')
    assert caught.value is error
    assert not index.run_pending()
    assert [path for path, _ in index.pending()] == ['note.md']
    monkeypatch.setattr(Embedder, 'embed', lambda self, text, operation: [1.0, 0.0])
    index.index('note.md')
    assert index.pending() == []


def test_manual_index_writes_when_background_scan_holds_sqlite_transaction(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    (root / 'manual.md').write_text('manual text')
    (root / 'scan.md').write_text('scan text')
    config = dict(provider='ollama', model='synthetic', dimensions=2, min_chunk_words=0)
    manual = SearchIndex(Vault(root, ['.']), config, state)
    background = SearchIndex(Vault(root, ['.']), config, state)
    provider_started = Event()
    scan_writes = Event()
    release_scan = Event()
    connect = sqlite3.connect

    def trace(sql):
        if sql.startswith('DELETE FROM lexical_chunks') and "'scan.md'" in sql:
            scan_writes.set()
            release_scan.wait(10)

    def connect_without_busy_wait(*args, **kwargs):
        db = connect(*args, **(kwargs | {'timeout': 0}))
        db.set_trace_callback(trace)
        return db

    def embed(self, text, operation):
        if operation == 'index':
            provider_started.set()
            assert scan_writes.wait(5)
        return [1.0, 0.0]

    monkeypatch.setattr(sqlite3, 'connect', connect_without_busy_wait)
    monkeypatch.setattr(Embedder, 'embed', embed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        indexing = pool.submit(manual.index, 'manual.md')
        try:
            assert provider_started.wait(5)
            scanning = pool.submit(background.reconcile_local)
            assert scan_writes.wait(5)
            with pytest.raises(sqlite3.OperationalError) as caught, connect(manual.database, timeout=0) as probe:
                probe.execute('BEGIN IMMEDIATE')
            assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
            wait([indexing], timeout=0.5)
        finally:
            release_scan.set()
        assert indexing.result(5)['chunks'] == 1
        assert set(scanning.result(5)) == {'manual.md', 'scan.md'}

    assert background.run_pending()
    assert background.pending() == []
    with connect(manual.database) as db:
        assert {path for path, in db.execute('SELECT path FROM section_chunks')} == {'manual.md', 'scan.md'}


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
    embeddings = dict(provider='ollama', model='synthetic', dimensions=2, min_chunk_words=0, background=True, state_dir=str(state))
    app = create_app({'vaults': {'queue': dict(root=str(root), allowed=['.'], token_env='TEST_QUEUE_TOKEN',
                                                embeddings=embeddings)}})
    with TestClient(app, base_url='http://localhost'):
        assert ran.wait(5)
    assert calls == [('scheduled text', 'background')]
