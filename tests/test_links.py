from pathlib import Path

from obsidian_thing.links import Resolver, iter_links
from obsidian_thing.vault import Vault


def make_vault(tmp_path: Path) -> Vault:
    root = tmp_path / 'vault'
    (root / 'notes' / 'deep').mkdir(parents=True)
    (root / 'notes' / 'target.md').write_text('## О страхе [[Иван|отца]] и мамы\n\nАбзац ^block\n')
    (root / 'notes' / 'picture.png').write_bytes(b'png')
    (root / 'notes' / 'deep' / 'same.md').write_text('# Same\n')
    (root / 'notes' / 'same.md').write_text('# Same\n')
    return Vault(root, ['.'])


def test_link_extraction_skips_code_and_templates(tmp_path):
    text = '`[[inline]]` и [[real]]\n\n```\n[[fenced]]\n```\n\n[[{{date}}]]\n'
    assert [link.target for link in iter_links(text)] == ['real']


def test_destinations_follow_obsidian_rules(tmp_path):
    vault = make_vault(tmp_path)
    resolver = Resolver(vault)
    assert resolver.destinations('target', 'notes/deep/same.md') == ['notes/target.md']
    assert resolver.destinations('same', 'notes/target.md') == ['notes/deep/same.md', 'notes/same.md']
    assert resolver.destinations('./same', 'notes/deep/other.md') == ['notes/deep/same.md']
    assert resolver.destinations('picture.png', 'notes/target.md') == ['notes/picture.png']
    assert resolver.destinations('absent', 'notes/target.md') == []


def test_anchor_accepts_an_expanded_heading(tmp_path):
    vault = make_vault(tmp_path)
    resolver = Resolver(vault)
    assert resolver.resolves_anchor('notes/target.md', 'О страхе Иван отца и мамы')
    assert resolver.resolves_anchor('notes/target.md', '^block')
    assert not resolver.resolves_anchor('notes/target.md', 'Чужой раздел')
    assert not resolver.resolves_anchor('notes/target.md', '^missing')


def test_overlay_hides_stale_disk_content(tmp_path):
    vault = make_vault(tmp_path)
    text = vault.read('notes/target.md')['content'] + '\n## Новый раздел\n'
    resolver = Resolver(vault, {'notes/target.md': text})
    assert resolver.resolves_anchor('notes/target.md', 'Новый раздел')
