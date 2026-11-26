import re
from bisect import bisect_left
from collections.abc import Iterator
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from unicodedata import normalize

from markdown_it import MarkdownIt
from mdit_py_plugins.front_matter import front_matter_plugin

if TYPE_CHECKING:
    from .vault import Vault

_FENCE = re.compile(r'^\s*(?:>\s*)*(?:```|~~~)')
_INLINE_CODE = re.compile(r'`[^`\n]*`')
_WIKILINK = re.compile(r'(!?)\[\[([^\[\]\n]+)\]\]')
_BLOCK_ID = re.compile(r'(?:^|\s)\^([A-Za-z0-9-]+)\s*$')


@dataclass(frozen=True)
class Scalar:
    start: int
    end: int
    value: str
    editable: bool = True


@dataclass(frozen=True)
class Link:
    target: str
    anchor: str
    embed: bool
    raw: str
    syntax: str = 'wiki'
    origin: str = 'markdown'
    property_key: str | None = None
    offset: int | None = None
    title: str = ''
    uri: str = ''
    scalar: Scalar | None = None

    @property
    def payload(self) -> str:
        mark = '!' if self.embed else ''
        anchor = f'#{self.anchor}' if self.anchor else ''
        return f'{mark}[[{self.target}{anchor}]]'


def iter_links(text: str) -> list[Link]:
    found: list[Link] = []
    in_fence = False
    for raw in text.split('\n'):
        if _FENCE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = _INLINE_CODE.sub(lambda m: ' ' * len(m.group()), raw)
        for match in _WIKILINK.finditer(line):
            link = wiki_link(match)
            if link is not None:
                found.append(link)
    return found


def wiki_link(match: re.Match[str], origin: str = 'markdown') -> Link | None:
    body = match.group(2).replace('\\|', '|').split('|')[0].strip()
    if '{{' in body:
        return None
    target, _, anchor = body.partition('#')
    return Link(target.strip(), anchor.strip(), bool(match.group(1)), match.group(0), origin=origin)


def _collapse(path: PurePosixPath) -> str | None:
    parts: list[str] = []
    for part in path.parts:
        if part == '..':
            if not parts:
                return None
            parts.pop()
        elif part != '.':
            parts.append(part)
    return '/'.join(parts)


def _key(path: str) -> str:
    return normalize('NFC', path.lower())


def _names(name: str) -> list[str]:
    names = [_key(name)]
    if not names[0].endswith('.md'):
        names.append(names[0] + '.md')
    return names


class Resolver:
    """Resolve wikilinks the way Obsidian's getLinkpathDest does."""

    def __init__(self, vault: 'Vault', overlay: dict[str, str] | None = None,
                 *, removed: frozenset[str] = frozenset()):
        self.vault = vault
        self.overlay = overlay or {}
        self.removed = removed
        self._files: list[str] | None = None
        self._anchors: dict[str, tuple[Counter[str], Counter[str]]] = {}
        self._paths: dict[str, list[str]] | None = None
        self._suffixes: dict[str, list[str]] | None = None

    def files(self) -> list[str]:
        if self._files is None:
            self._files = sorted((set(self.vault.visible_files()) - self.removed) | set(self.overlay))
        return self._files

    def has_file(self, path: str) -> bool:
        paths = self.files()
        index = bisect_left(paths, path)
        return index < len(paths) and paths[index] == path

    def files_in(self, folder: str, *, recursive: bool) -> Iterator[str]:
        paths = self.files()
        prefix = folder + '/' if folder else ''
        for index in range(bisect_left(paths, prefix), len(paths)):
            path = paths[index]
            if not path.startswith(prefix):
                break
            if recursive or '/' not in path[len(prefix):]:
                yield path

    def read(self, path: str) -> str:
        if path in self.overlay:
            return self.overlay[path]
        return self.vault.read(path)['content']

    def destinations(self, target: str, source: str) -> list[str]:
        name = target.strip()
        if not name:
            return [source] if source else []
        if self._paths is None or self._suffixes is None:
            paths: dict[str, list[str]] = defaultdict(list)
            suffixes: dict[str, list[str]] = defaultdict(list)
            for path in self.files():
                key = _key(path)
                paths[key].append(path)
                parts = key.split('/')
                for offset in range(len(parts)):
                    suffixes['/'.join(parts[offset:])].append(path)
            self._paths, self._suffixes = paths, suffixes
        relative = name.startswith(('./', '../'))
        if relative or name.startswith('/'):
            base = PurePosixPath(source).parent
            path = PurePosixPath(name.lstrip('/')) if name.startswith('/') else base / name
            collapsed = _collapse(path)
            if not collapsed:
                return []
            return sorted({path for item in _names(collapsed) for path in self._paths.get(item, ())})
        return sorted({path for item in _names(name) for path in self._suffixes.get(item, ())})

    def context(self, path: str, text: str, limit: int = 200) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        for link in iter_links(text)[:limit]:
            destinations = self.destinations(link.target, path) if link.target else [path]
            found.append({
                'raw': link.raw,
                'target': link.target,
                'anchor': link.anchor,
                'embed': link.embed,
                'resolved': destinations[:5],
                'self': destinations == [path],
                'anchor_ok': (any(self.resolves_anchor(item, link.anchor) for item in destinations)
                              if destinations else False),
            })
        return found

    def _read_anchors(self, path: str) -> tuple[Counter[str], Counter[str]]:
        headings: Counter[str] = Counter()
        blocks: Counter[str] = Counter()
        try:
            content = self.read(path)
        except ValueError:
            return headings, blocks
        if path.lower().endswith('.base'):
            from .structured_links import base_views

            try:
                headings.update(_key(name) for name in base_views(content))
            except ValueError:
                headings.clear()
            return headings, blocks
        tokens = _BLOCK_PARSER.parse(content.removeprefix('\ufeff'))
        hierarchy: list[tuple[int, str]] = []
        for index, token in enumerate(tokens):
            if token.type != 'inline':
                continue
            opening = tokens[index - 1]
            if opening.type == 'heading_open':
                level = int(opening.tag[1:])
                while hierarchy and hierarchy[-1][0] >= level:
                    hierarchy.pop()
                hierarchy.append((level, _normalise(token.content)))
                for offset in range(len(hierarchy)):
                    headings['#'.join(title for _, title in hierarchy[offset:])] += 1
            for line in token.content.splitlines():
                block = _BLOCK_ID.search(line)
                if block:
                    blocks[_key(block.group(1))] += 1
        return headings, blocks

    def anchor_count(self, path: str, anchor: str) -> int:
        if not anchor or not path.lower().endswith(('.md', '.base')):
            return 1
        if path.lower().endswith('.base') and not self.vault.bases:
            return 1
        if path not in self._anchors:
            self._anchors[path] = self._read_anchors(path)
        headings, blocks = self._anchors[path]
        if path.lower().endswith('.base'):
            return headings[_key(anchor)]
        if anchor.startswith('^'):
            return blocks[_key(anchor[1:])]
        return headings['#'.join(_normalise(part) for part in anchor.split('#'))]

    def resolves_anchor(self, path: str, anchor: str) -> bool:
        return self.anchor_count(path, anchor) > 0


_BLOCK_PARSER = MarkdownIt('commonmark').use(front_matter_plugin)
_WIKILINK_IN_TEXT = re.compile(r'\[\[([^\]|]+)(?:\|([^\]]+))?\]\]')


def _normalise(heading: str) -> str:
    text = _WIKILINK_IN_TEXT.sub(
        lambda m: f'{m.group(1).strip()} {m.group(2).strip()}' if m.group(2) else m.group(1),
        heading.strip())
    for mark in '*_`$':
        text = text.replace(mark, '')
    return _key(text.strip())
