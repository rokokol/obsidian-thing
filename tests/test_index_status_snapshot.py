import sqlite3

from obsidian_thing.embeddings import Embedder
from obsidian_thing.search import SearchIndex
from obsidian_thing.vault import Vault


def _embed(self, text, operation):
    return [1.0, 0.0, 0.0]


def _index(tmp_path, monkeypatch, *, files, embeddings=None, indexing=None, vectors=True,
           model='synthetic', section_chunks=True):
    root = tmp_path / 'vault'
    root.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    monkeypatch.setattr(Embedder, 'embed', _embed)
    config = dict(provider='ollama', model=model, dimensions=2, state_dir=str(state),
                  section_chunks=section_chunks)
    index = SearchIndex(Vault(root, ['.']), (embeddings or {}) | config, state,
                        indexing=indexing or {}, vectors=vectors)
    return root, state, index


def _forbid_vault_access(monkeypatch, index):
    def forbidden(*args, **kwargs):
        raise AssertionError('status touched the vault')

    monkeypatch.setattr(index.vault, 'visible_files', forbidden)
    monkeypatch.setattr(index.vault, 'read', forbidden)
    monkeypatch.setattr(index.vault, 'read_attachment', forbidden)
    monkeypatch.setattr(index.vault, 'attachment_stamp', forbidden)
    monkeypatch.setattr(index.policy, 'check', forbidden)
    monkeypatch.setattr(index, '_size', forbidden)


def _coverage(status):
    return {key: status[key] for key in
            ('files', 'excluded', 'indexed', 'unindexed', 'embedded', 'last_full_scan')}


def test_status_reads_only_the_database_snapshot(tmp_path, monkeypatch):
    root, _, index = _index(tmp_path, monkeypatch, files={'note.md': 'word'})
    index.reconcile_local()
    _forbid_vault_access(monkeypatch, index)

    status = index.status()['index']
    assert (status['files'], status['excluded'], status['indexed'], status['unindexed']) == (1, 0, 1, 0)
    assert isinstance(status['last_full_scan'], str)


def test_coverage_is_unknown_until_a_full_scan_completes(tmp_path, monkeypatch):
    root, _, index = _index(tmp_path, monkeypatch, files={'note.md': 'word', 'skip.tmp.md': 'skip'},
                            indexing={'exclude': ['*.tmp.md']}, vectors=False)
    unknown = index.status()['index']
    assert _coverage(unknown) == {
        'files': None, 'excluded': None, 'indexed': None, 'unindexed': None,
        'embedded': None, 'last_full_scan': None}
    assert unknown['pending'] == 0

    index.reconcile_local(['note.md'])
    assert index.status()['index'] == unknown

    index.reconcile_local()
    status = index.status()['index']
    assert (status['files'], status['excluded'], status['indexed'], status['unindexed']) == (1, 1, 1, 0)
    assert status['embedded'] == 0
    assert status['pending'] == 0
    assert isinstance(status['last_full_scan'], str)


def test_snapshot_survives_later_vault_changes_until_a_full_scan(tmp_path, monkeypatch):
    root, _, index = _index(tmp_path, monkeypatch, files={
        'source.md': 'source word', 'near.md': 'near word'})
    index.index('source.md')
    index.reconcile_local()
    first = index.status()['index']
    assert (first['files'], first['indexed'], first['embedded'], first['pending']) == (2, 2, 1, 1)

    (root / 'fresh.md').write_text('fresh word')
    (root / 'near.md').unlink()
    assert index.status()['index'] == first

    index.reconcile_local(['fresh.md'])
    partial = index.status()['index']
    assert _coverage(partial) == _coverage(first)
    assert partial['pending'] == 2

    index.reconcile_local()
    current = index.status()['index']
    assert (current['files'], current['indexed'], current['embedded'], current['pending']) == (2, 2, 1, 1)


def test_a_stopped_full_scan_keeps_the_previous_snapshot(tmp_path, monkeypatch):
    root, _, index = _index(tmp_path, monkeypatch, files={'note.md': 'word'}, vectors=False)
    index.reconcile_local()
    first = index.status()['index']

    (root / 'fresh.md').write_text('fresh')
    index.reconcile_local(stop=lambda: True)
    assert index.status()['index'] == first


def test_snapshot_is_isolated_by_split_mode(tmp_path, monkeypatch):
    root, state, sections = _index(tmp_path, monkeypatch, files={'note.md': 'word'})
    sizes = SearchIndex(sections.vault, dict(
        provider='ollama', model='synthetic', dimensions=2, state_dir=str(state), section_chunks=False), state)
    sections.reconcile_local()
    sizes.reconcile_local()
    assert sections.status()['index']['mode'] == 'sections'
    assert sizes.status()['index']['mode'] == 'size'
    assert sizes.status()['index']['files'] == sections.status()['index']['files'] == 1

    (root / 'fresh.md').write_text('fresh')
    sections.reconcile_local()
    assert sections.status()['index']['files'] == 2
    assert sizes.status()['index']['files'] == 1


def test_snapshot_is_isolated_by_embedding_profile(tmp_path, monkeypatch):
    root, state, first = _index(tmp_path, monkeypatch, files={'note.md': 'word'}, model='first')
    first.index('note.md')
    first.reconcile_local()
    assert (first.status()['index']['files'], first.status()['index']['embedded']) == (1, 1)

    second = SearchIndex(Vault(root, ['.']), dict(
        provider='ollama', model='second', dimensions=2, state_dir=str(state)), state)
    unknown = second.status()['index']
    assert unknown['files'] is None
    assert unknown['embedded'] is None
    assert unknown['last_full_scan'] is None

    second.reconcile_local()
    second_status = second.status()['index']
    assert (second_status['files'], second_status['embedded']) == (1, 0)

    (root / 'fresh.md').write_text('fresh')
    second.reconcile_local()
    assert second.status()['index']['files'] == 2
    assert first.status()['index']['files'] == 1
    assert first.status()['index']['embedded'] == 1


def test_pending_is_the_database_queue_and_status_never_checks_files(tmp_path, monkeypatch):
    root, _, index = _index(tmp_path, monkeypatch, files={'note.md': 'word'})
    index.reconcile_local()
    assert index.status()['index']['pending'] == 1

    (root / 'note.md').unlink()

    def forbidden(*args, **kwargs):
        raise AssertionError('status checked a file')

    monkeypatch.setattr(index.vault, 'read', forbidden)
    monkeypatch.setattr(index.vault, 'attachment_stamp', forbidden)
    monkeypatch.setattr(index.policy, 'check', forbidden)
    monkeypatch.setattr(index, '_size', forbidden)
    stale = index.status()['index']
    assert (stale['files'], stale['pending']) == (1, 1)

    monkeypatch.undo()
    index.reconcile_local()
    current = index.status()['index']
    assert (current['files'], current['pending']) == (0, 0)
    assert isinstance(current['last_full_scan'], str)


def test_a_failed_full_scan_keeps_the_previous_snapshot(tmp_path, monkeypatch):
    root, _, index = _index(tmp_path, monkeypatch, files={'note.md': 'word'},
                            indexing={'respect_gitignore': True}, vectors=False)
    index.reconcile_local()
    first = index.status()['index']

    (root / '.gitignore').write_text('!\n')
    index.reconcile_local()
    assert index.status()['index'] == first


def test_the_snapshot_is_written_in_the_index_database(tmp_path, monkeypatch):
    root, _, index = _index(tmp_path, monkeypatch, files={'note.md': 'word'})
    assert index.embedder is not None
    index.reconcile_local()
    with sqlite3.connect(index.database) as db:
        row = db.execute('SELECT files, excluded, indexed, embedded FROM coverage '
                          'WHERE mode = ? AND profile = ?',
                          (int(index.section_chunks), index.embedder.profile)).fetchone()
    assert row == (1, 0, 1, 0)
