from pathlib import Path

import pytest

from obsidian_thing import checker_context
from obsidian_thing.checker import Checker, CheckerError
from obsidian_thing.vault import Vault


def put(root: Path, path: str, text: str):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text, encoding='utf-8')


def make_checker(tmp_path: Path, assertions: str, **options):
    script = tmp_path / 'checker.py'
    script.write_text('import json, sys\nc = json.load(sys.stdin)\n' + assertions
                      + '\nprint(json.dumps({"decision": "allow"}))\n', encoding='utf-8')
    return Checker({'script': str(script), **options})


def test_checker_snapshot_uses_selected_properties_and_scoped_paths(tmp_path):
    root = tmp_path / 'vault'
    put(root, 'Open/Previous.md', '---\naliases: ["Previous", 08]\nnext: "[[Current]]"\n'
        'secret: REDACT\n---\nPRIVATE BODY\n')
    put(root, 'Open/Parent.md', '# Heading\n')
    put(root, 'Open/asset.pdf', 'no text needed')
    put(root, 'Open/Bad.md', '---\naliases: [\n---\n')
    put(root, 'Closed/Parent.md', '---\naliases: [Hidden]\n---\n')
    put(root, 'Open/.Hidden.md', '---\naliases: [Hidden]\n---\n')
    (root / 'Open' / 'Alias.md').symlink_to(root / 'Closed' / 'Parent.md')
    checker = make_checker(tmp_path, '''
s = c['vault_context']
assert s['property_names'] == ['aliases', 'next', 'up']
assert set(s['files']) == {'Open/Previous.md', 'Open/Parent.md', 'Open/asset.pdf',
                           'Open/Bad.md', 'Open/Current.md'}
assert set(s['notes']) == {'Open/Previous.md', 'Open/Parent.md', 'Open/Bad.md'}
previous = s['notes']['Open/Previous.md']
assert previous['properties'] == {'aliases': ['Previous', '08'], 'next': '[[Current]]'}
assert previous['property_links']['next'][0]['resolved'] == ['Open/Current.md']
assert s['notes']['Open/Bad.md']['frontmatter'] == 'invalid'
assert s['notes']['Open/Parent.md']['frontmatter'] == 'absent'
assert s['after']['property_links']['up'][0]['resolved'] == ['Open/Parent.md']
assert s['before'] is None
assert s['links_before'] == []
assert not any(marker in str(s) for marker in ['REDACT', 'PRIVATE BODY'])
''', vault_context_properties=['aliases', 'up', 'next'])
    vault = Vault(root, ['Open'], checker=checker)
    vault.write('Open/Current.md', '---\nup: "[[./Parent]]"\n---\n# Current\n', None)


def test_checker_snapshot_pairs_before_and_after_at_update(tmp_path):
    root = tmp_path / 'vault'
    put(root, 'First.md', '# First')
    put(root, 'Second.md', '# Second')
    before = '---\nup: "[[First]]"\n---\n[[First]]\n'
    put(root, 'Note.md', before)
    checker = make_checker(tmp_path, '''
s = c['vault_context']
assert 'Note.md' not in s['notes']
assert s['before']['property_links']['up'][0]['resolved'] == ['First.md']
assert s['after']['property_links']['up'][0]['resolved'] == ['Second.md']
assert s['links_before'][0]['resolved'] == ['First.md']
assert c['links'][0]['resolved'] == ['Second.md']
''', vault_context_properties=['up'])
    vault = Vault(root, ['.'], checker=checker)
    vault.write('Note.md', before.replace('First', 'Second'), vault.read('Note.md')['revision'])


def test_checker_without_opt_in_gets_no_snapshot(tmp_path):
    root = tmp_path / 'vault'
    root.mkdir()
    checker = make_checker(tmp_path, "assert 'vault_context' not in c")
    vault = Vault(root, ['.'], checker=checker)
    vault.write('Note.md', '# Note', None)


def test_checker_rejects_an_oversized_snapshot_before_mutation(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    put(root, 'Other.md', '---\naliases: [Other]\n---\n')
    monkeypatch.setattr(checker_context, 'MAX_CONTEXT_BYTES', 200)
    checker = make_checker(tmp_path, '', vault_context_properties=['aliases'])
    with pytest.raises(CheckerError, match='context is too large'):
        Vault(root, ['.'], checker=checker).write('Note.md', '# Note', None)
    assert not (root / 'Note.md').exists()


@pytest.mark.parametrize('properties', ['tags', [''], [5], ['tags', 'tags']])
def test_checker_rejects_invalid_property_selection(tmp_path, properties):
    with pytest.raises(ValueError, match='vault_context_properties'):
        make_checker(tmp_path, '', vault_context_properties=properties)
