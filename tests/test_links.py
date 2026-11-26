import os
from pathlib import Path

import pytest

from obsidian_thing.graph import LinkGraph
from obsidian_thing.links import Resolver, iter_links
from obsidian_thing.vault import Vault, VaultError


def make_vault(tmp_path: Path) -> Vault:
    root = tmp_path / 'vault'
    (root / 'notes' / 'deep').mkdir(parents=True)
    (root / 'notes' / 'target.md').write_text('## О страхе [[Иван|отца]] и мамы\n\nАбзац ^block\n')
    (root / 'notes' / 'picture.png').write_bytes(b'png')
    (root / 'notes' / 'deep' / 'same.md').write_text('# Same\n')
    (root / 'notes' / 'same.md').write_text('# Same\n')
    return Vault(root, ['.'], media_gallery=True)


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


def test_graph_reads_links_in_properties_and_media_gallery(tmp_path):
    vault = make_vault(tmp_path)
    source = 'notes/deep/source.md'
    (vault.root / source).write_text('''---
related:
  - "[[target#^block]]"
  - "description with [[same]]"
  - "![[picture.png]]"
attachment:
  file: "[cover](../picture.png)"
---

````python
```media-gallery
[[hidden]]
```
````

> ```media-gallery
> ![[picture.png]]
> picture.png
> ![cover](picture.png)
> [not a gallery entry](hidden.png)
> path: elsewhere
> ```

```img-gal
https://example.com/photo.png
```

``[[inline code]]`` and [reference][target]

[target]: ../target.md#%5Eblock
''')
    graph = LinkGraph(vault)
    outgoing = graph.outgoing(source)
    assert [(row['origin'], row['raw']) for row in outgoing] == [
        ('property', '[[target#^block]]'), ('property', '[cover](../picture.png)'),
        ('media-gallery', '![[picture.png]]'), ('media-gallery', 'picture.png'),
        ('media-gallery', '![cover](picture.png)'), ('media-gallery', 'path: elsewhere'),
        ('img-gal', 'https://example.com/photo.png'),
        ('markdown', '[reference][target]'),
    ]
    assert [row.get('property') for row in outgoing[:2]] == ['related.0', 'attachment.file']
    assert {row['raw']: row['status'] for row in outgoing}['path: elsewhere'] == 'unresolved'
    assert {row['raw']: row['status'] for row in outgoing}['https://example.com/photo.png'] == 'external'
    assert all(row['status'] == 'resolved' for row in outgoing
               if row['raw'] not in {'path: elsewhere', 'https://example.com/photo.png'})
    incoming = graph.incoming('notes/target.md')['links']
    assert [(row['source'], row['origin']) for row in incoming] == [(source, 'property'), (source, 'markdown')]
    media, = graph.media(source)
    assert media['path'] == 'notes/picture.png'
    assert len(media['links']) == 4


def test_gallery_folder_expands_visible_media_and_updates_without_note_edits(tmp_path):
    vault = make_vault(tmp_path)
    root = vault.root
    folder = root / 'notes' / 'Gallery dir'
    nested = folder / 'sub'
    nested.mkdir(parents=True)
    (folder / 'first.JPG').write_bytes(b'jpg')
    (nested / 'clip.mp4').write_bytes(b'video')
    (folder / 'note.md').write_text('Not media')
    (folder / '.hidden.png').write_bytes(b'png')
    (root / 'private.png').write_bytes(b'private')
    (folder / 'symlink.png').symlink_to(root / 'private.png')
    (root / 'notes' / 'Gallery dir-extra').mkdir()
    (root / 'notes' / 'Gallery dir-extra' / 'wrong.png').write_bytes(b'png')
    source = 'notes/same.md'
    (root / source).write_text('''```media-gallery
columns: 2
type: vertical
path: notes/Gallery dir
```

```img-gallery
path: notes/Gallery dir/*
```

```media-gallery
path: ../private
```
''')
    graph = LinkGraph(Vault(root, ['notes'], media_gallery=True))
    media = {item['path']: item for item in graph.media(source)}
    assert set(media) == {'notes/Gallery dir/first.JPG', 'notes/Gallery dir/sub/clip.mp4'}
    assert {link['raw'] for link in media['notes/Gallery dir/first.JPG']['links']} == {
        'path: notes/Gallery dir', 'path: notes/Gallery dir/*',
    }
    assert [link['raw'] for link in media['notes/Gallery dir/sub/clip.mp4']['links']] == ['path: notes/Gallery dir']
    assert {row['raw']: row['status'] for row in graph.outgoing(source)}['path: ../private'] == 'unresolved'
    (nested / 'clip.mp4').rename(nested / 'renamed.mp4')
    backlinks = graph.incoming('notes/Gallery dir/sub/renamed.mp4')['links']
    assert [(link['source'], link['raw']) for link in backlinks] == [(source, 'path: notes/Gallery dir')]
    assert {item['path'] for item in graph.media(source)} == {
        'notes/Gallery dir/first.JPG', 'notes/Gallery dir/sub/renamed.mp4',
    }


def test_graph_matches_normalized_paths_but_keeps_ambiguity(tmp_path):
    vault = make_vault(tmp_path)
    (vault.root / 'notes' / 'Café.md').write_text(
        '---\naliases: [Other name]\n---\n## One\n### Repeat\n## Two\n### Repeat\n'
    )
    (vault.root / 'notes' / 'nested').mkdir()
    (vault.root / 'notes' / 'nested' / 'deep').mkdir()
    (vault.root / 'notes' / 'nested' / 'deep' / 'same.md').write_text('Same')
    source = 'notes/source.md'
    (vault.root / source).write_text(
        '[[Cafe\u0301#Repeat]] [[Café#Two#Repeat]] [[Café#Missing#Repeat]] '
        '[[Caf%C3%A9]] [[Other name]] [[DEEP/SAME]] '
        '[implicit](same.md) [explicit](./deep/same.md) [absolute](/notes/same.md) '
        '[encoded](Caf%C3%A9.md) [[../../../notes/same]]'
    )
    rows = {row['raw']: row for row in LinkGraph(vault).outgoing(source)}
    assert {raw: row['status'] for raw, row in rows.items()} == {
        '[[Cafe\u0301#Repeat]]': 'ambiguous_anchor', '[[Café#Two#Repeat]]': 'resolved',
        '[[Café#Missing#Repeat]]': 'unresolved_anchor', '[[Caf%C3%A9]]': 'unresolved',
        '[[Other name]]': 'unresolved', '[[DEEP/SAME]]': 'ambiguous',
        '[implicit](same.md)': 'ambiguous', '[explicit](./deep/same.md)': 'resolved',
        '[absolute](/notes/same.md)': 'resolved', '[encoded](Caf%C3%A9.md)': 'resolved',
        '[[../../../notes/same]]': 'unresolved',
    }
    assert rows['[explicit](./deep/same.md)']['resolved'] == ['notes/deep/same.md']
    assert rows['[absolute](/notes/same.md)']['resolved'] == ['notes/same.md']


def test_rename_rolls_back_if_a_reference_cannot_be_replaced(tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    directory = vault.root / 'notes'
    target = directory / 'target.md'
    original = target.read_bytes()
    for name in ('a.md', 'z.md'):
        (directory / name).write_text('[[target]]')
    before = sorted(path.name for path in directory.iterdir())
    replace = os.replace
    applied = []

    def fail_on_second_note(source, destination, **kwargs):
        if destination == 'z.md':
            assert (directory / 'a.md').read_text() != '[[target]]'
            assert (directory / 'new.md').exists()
            raise OSError('synthetic failure')
        applied.append(destination)
        return replace(source, destination, **kwargs)

    monkeypatch.setattr(os, 'replace', fail_on_second_note)
    with pytest.raises(VaultError, match='Cannot rename'):
        vault.rename('notes/target.md', 'new.md', vault.stat('notes/target.md')['revision'])
    assert applied == ['a.md', 'a.md']
    assert sorted(path.name for path in directory.iterdir()) == before
    assert target.read_bytes() == original
    assert all((directory / name).read_text() == '[[target]]' for name in ('a.md', 'z.md'))


def test_overlay_hides_stale_disk_content(tmp_path):
    vault = make_vault(tmp_path)
    text = vault.read('notes/target.md')['content'] + '\n## Новый раздел\n'
    resolver = Resolver(vault, {'notes/target.md': text})
    assert resolver.resolves_anchor('notes/target.md', 'Новый раздел')
