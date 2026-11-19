import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .vault import Vault

_FENCE = re.compile(r'^\s*(?:>\s*)*(?:```|~~~)')
_INLINE_CODE = re.compile(r'`[^`\n]*`')
_WIKILINK = re.compile(r'(!?)\[\[([^\[\]\n]+)\]\]')
_HEADING = re.compile(r'^#{1,6}\s+(.*?)\s*$')
_BLOCK_ID = re.compile(r'(?:^|\s)\^([A-Za-z0-9-]+)\s*$')


@dataclass(frozen=True)
class Link:
    target: str
    anchor: str
    embed: bool
    raw: str

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
            body = match.group(2).replace('\\|', '|').split('|')[0].strip()
            if '{{' in body:
                continue
            target, _, anchor = body.partition('#')
            found.append(Link(target.strip(), anchor.strip(), bool(match.group(1)), match.group(0)))
    return found


def _collapse(path: PurePosixPath) -> str:
    parts: list[str] = []
    for part in path.parts:
        if part == '..':
            if parts:
                parts.pop()
        elif part != '.':
            parts.append(part)
    return '/'.join(parts)


def _names(name: str) -> list[str]:
    names = [name.lower()]
    if '.' not in PurePosixPath(name).name:
        names.append(name.lower() + '.md')
    return names


class Resolver:
    """Resolve wikilinks the way Obsidian's getLinkpathDest does."""

    def __init__(self, vault: 'Vault', overlay: dict[str, str] | None = None):
        self.vault = vault
        self.overlay = overlay or {}
        self._files: list[str] | None = None
        self._anchors: dict[str, tuple[set[str], set[str]]] = {}

    def files(self) -> list[str]:
        if self._files is None:
            self._files = sorted(set(self.vault.visible_files()) | set(self.overlay))
        return self._files

    def read(self, path: str) -> str:
        if path in self.overlay:
            return self.overlay[path]
        return self.vault.read(path)['content']

    def destinations(self, target: str, source: str) -> list[str]:
        name = target.strip()
        if not name:
            return [source] if source else []
        relative = name.startswith(('./', '../'))
        if relative:
            base = PurePosixPath(source).parent if source else PurePosixPath('.')
            names = _names(_collapse(base / name))
        else:
            names = _names(name)
        matches = []
        for path in self.files():
            low = path.lower()
            for wanted in names:
                if low == wanted or (not relative and low.endswith('/' + wanted)):
                    matches.append(path)
                    break
        return sorted(set(matches))

    def anchors(self, path: str) -> tuple[set[str], set[str]]:
        if path not in self._anchors:
            headings: set[str] = set()
            blocks: set[str] = set()
            try:
                text = self.read(path)
            except Exception:
                text = ''
            in_fence = False
            for line in text.split('\n'):
                if _FENCE.match(line):
                    in_fence = not in_fence
                    continue
                if in_fence:
                    continue
                heading = _HEADING.match(line)
                if heading:
                    headings.add(_normalise(heading.group(1)))
                block = _BLOCK_ID.search(line)
                if block:
                    blocks.add(block.group(1).lower())
            self._anchors[path] = headings, blocks
        return self._anchors[path]

    def resolves_anchor(self, path: str, anchor: str) -> bool:
        if not anchor or not path.lower().endswith('.md'):
            return True
        headings, blocks = self.anchors(path)
        if anchor.startswith('^'):
            return anchor[1:].lower() in blocks
        return _normalise(anchor) in headings


_WIKILINK_IN_TEXT = re.compile(r'\[\[([^\]|]+)(?:\|([^\]]+))?\]\]')


def _normalise(heading: str) -> str:
    text = _WIKILINK_IN_TEXT.sub(
        lambda m: f'{m.group(1).strip()} {m.group(2).strip()}' if m.group(2) else m.group(1),
        heading.strip())
    for mark in '*_`$':
        text = text.replace(mark, '')
    return text.split('#')[-1].strip().lower()
