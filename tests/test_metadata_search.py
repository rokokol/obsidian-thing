import sqlite3

import pytest
from starlette.testclient import TestClient

from obsidian_thing.search import SearchIndex
from obsidian_thing.server import create_app
from obsidian_thing.vault import Vault, VaultError


def test_find_combines_tags_aliases_and_properties_before_limiting(tmp_path):
    root = tmp_path / 'vault'
    root.mkdir()
    notes = {
        'a.md': '---\ntags: [project/sub]\naliases: [Other]\nstatus: active\n---\n',
        'b.md': '---\naliases: [Étoile]\nsynonyms: [Workspace, Plan]\nstatus: active\nauthors: [Илья, Анна]\nreviewed: null\n---\n#project/sub',
        'c.md': '---\ntags: [projects]\naliases: [Étoile]\nstatus: active\n---\n',
        'd.md': '---\ntags: [project]\naliases: [Étoile extra]\nstatus: active\n---\n',
    }
    for path, text in notes.items():
        (root / path).write_text(text)
    index = SearchIndex(Vault(root, ['.']), {}, tmp_path, vectors=False)
    index.reconcile_local()
    assert index.find(tags=['#PROJECT'], aliases=['e\u0301TOILE'], properties={
        'status': 'active', 'authors': 'Илья', 'reviewed': None}, limit=1) == {'paths': ['b.md']}
    assert index.find(tags=['project']) == {'paths': ['a.md', 'b.md', 'd.md']}
    assert index.find(aliases=['Étoile']) == {'paths': ['b.md', 'c.md']}
    assert index.find(properties={'authors': 'илья'}) == {'paths': []}
    assert index.find(aliases=['workspace']) == {'paths': []}
    assert index.find(aliases=['workspace'], alias_property='synonyms') == {'paths': ['b.md']}
    assert index.find(aliases=['Workspace ext'], alias_property='synonyms') == {'paths': []}
    with pytest.raises(VaultError):
        index.find()


def test_find_ignores_code_and_rejects_stale_or_revoked_hits(tmp_path):
    root = tmp_path / 'vault'
    root.mkdir()
    note = root / 'note.md'
    note.write_text('`#code`\n\n```md\n#fenced\n```\n\n[[page#heading]] #old')
    other = root / 'other.md'
    other.write_text('#old')
    index = SearchIndex(Vault(root, ['.']), {}, tmp_path, vectors=False)
    index.reconcile_local()
    for tag in ('code', 'fenced', 'heading'):
        assert index.find(tags=[tag]) == {'paths': []}
    assert index.find(tags=['old']) == {'paths': ['note.md', 'other.md']}
    note.write_text('#new')
    restricted = SearchIndex(Vault(root, ['note.md']), {}, tmp_path, vectors=False)
    assert restricted.find(tags=['old']) == {'paths': []}
    restricted.reconcile_local(['note.md'])
    assert restricted.find(tags=['new']) == {'paths': ['note.md']}
    note.unlink()
    assert restricted.find(tags=['new']) == {'paths': []}


def test_reconciliation_backfills_metadata_for_an_unchanged_note(tmp_path):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'note.md').write_text('Example #project')
    index = SearchIndex(Vault(root, ['.']), {}, tmp_path, vectors=False)
    index.reconcile_local()
    with sqlite3.connect(index.database) as db:
        db.execute('DROP TABLE note_metadata')
    upgraded = SearchIndex(Vault(root, ['.']), {}, tmp_path, vectors=False)
    assert upgraded.find(tags=['project']) == {'paths': []}
    upgraded.reconcile_local()
    assert upgraded.find(tags=['project']) == {'paths': ['note.md']}


def test_find_filename_regex_filters_basenames_before_limiting(tmp_path):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'Alpha.md').mkdir()
    for path, text in [('Alpha.md/z.md', '#project'), ('alpha-1.md', '#other'),
                       ('alpha-2.md', '#project'), ('alpha-3.txt', '#project')]:
        (root / path).write_text(text)
    index = SearchIndex(Vault(root, ['.']), {}, tmp_path, vectors=False)
    index.reconcile_local()
    assert index.find(filename_regex=r'(?i)^alpha[-\d.].*\.md$') == {'paths': ['alpha-1.md', 'alpha-2.md']}
    assert index.find(tags=['project'], filename_regex=r'^alpha-', limit=1) == {'paths': ['alpha-2.md']}
    with pytest.raises(VaultError, match='regular expression'):
        index.find(filename_regex='[')


def test_find_rejects_regex_that_exceeds_the_time_limit(tmp_path):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / ('a' * 200 + 'b.md')).write_text('#project')
    index = SearchIndex(Vault(root, ['.']), {}, tmp_path, vectors=False)
    index.reconcile_local()
    with pytest.raises(VaultError, match='time limit'):
        index.find(filename_regex=r'^(a|aa)+$')


def test_find_is_available_over_mcp_without_embeddings(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'only.md').write_text('---\ntags: [project]\n---\n')
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    monkeypatch.setenv('TEST_FIND_TOKEN', 'example-token')
    config = {'vaults': {'notes': {'root': str(root), 'allowed': ['.'], 'token_env': 'TEST_FIND_TOKEN',
                                  'embeddings': {'enabled': False, 'state_dir': str(state)}}}}
    with TestClient(create_app(config), base_url='http://localhost') as client:
        def call(name, arguments):
            response = client.post('/vaults/notes/mcp', headers={
                'Authorization': 'Bearer example-token', 'Accept': 'application/json, text/event-stream'},
                json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                      'params': {'name': name, 'arguments': arguments}})
            assert response.status_code == 200
            result = response.json()['result']
            assert not result.get('isError'), result
            return result['structuredContent']

        call('obsidian.index', {'path': 'only.md'})
        assert call('obsidian.find', {'tags': ['project']}) == {'paths': ['only.md']}
        assert call('obsidian.find', {'filename_regex': r'^only\.md$'}) == {'paths': ['only.md']}
