import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import Iterator

from markdown_it import MarkdownIt
from ruamel.yaml.nodes import ScalarNode, SequenceNode

from .frontmatter import frontmatter_nodes, frontmatter_span

_MARKDOWN = MarkdownIt('commonmark')
_PARAGRAPH_END = re.compile(r'\n[ \t]*\n')


@dataclass(frozen=True)
class Chunk:
    text: str
    headings: tuple[str, ...]
    line_start: int
    line_end: int


def _properties(text: str) -> str:
    _, root = frontmatter_nodes(text)
    if root is None:
        return ''
    values: dict[str, str] = {}
    for name, node in root.value:
        if not isinstance(name, ScalarNode) or name.value not in ('tags', 'aliases'):
            continue
        items = node.value if isinstance(node, SequenceNode) else [node]
        value = ', '.join(' '.join(item.value.split()) for item in items
                          if isinstance(item, ScalarNode) and item.value.strip())
        if value:
            values[name.value] = value
    return '\n'.join(f'{name}: {values[name][:160]}' for name in ('tags', 'aliases') if name in values)


def _sections(text: str, markdown: bool) -> Iterator[tuple[tuple[str, ...], int, int, int]]:
    stack: list[tuple[int, str]] = []
    start = anchor = 0
    if markdown:
        tokens = _MARKDOWN.parse(text)
        for index, token in enumerate(tokens):
            if token.type != 'heading_open' or token.level != 0 or token.map is None:
                continue
            heading_start, heading_end = token.map
            yield tuple(title for _, title in stack), start, heading_start, anchor
            level = int(token.tag[1:])
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, tokens[index + 1].content))
            start, anchor = heading_end, heading_start
    yield tuple(title for _, title in stack), start, text.count('\n') + 1, anchor


def _paragraphs(text: str, size: int) -> Iterator[tuple[str, int, int]]:
    start = 0
    pending_start = pending_end = 0
    for end in (*[match.start() for match in _PARAGRAPH_END.finditer(text)], len(text)):
        raw = text[start:end]
        left = start + len(raw) - len(raw.lstrip())
        right = start + len(raw.rstrip())
        start = end
        if left >= right:
            continue
        if pending_end > pending_start and right - pending_start > size:
            yield text[pending_start:pending_end], pending_start, pending_end
            pending_start = pending_end = 0
        while right - left > size:
            stop = left + size
            yield text[left:stop], left, stop
            left = stop
        if pending_start == pending_end:
            pending_start = left
        pending_end = right
    if pending_end > pending_start:
        yield text[pending_start:pending_end], pending_start, pending_end


def _fixed(text: str, size: int) -> Iterator[tuple[str, int, int]]:
    for start in range(0, len(text), size):
        raw = text[start:start + size]
        fragment = raw.strip()
        if fragment:
            left = start + len(raw) - len(raw.lstrip())
            yield fragment, left, left + len(fragment)


def chunks(content: str, size: int, *, markdown: bool, sections: bool) -> Iterator[Chunk]:
    text = content.replace('\r\n', '\n').replace('\r', '\n').removeprefix('\ufeff')
    properties = ''
    frontmatter_lines = 0
    if markdown:
        _, span = frontmatter_span(text)
        if span is not None:
            properties = _properties(text)
            frontmatter_lines = text.count('\n', 0, span[2]) + 1
            text = text[span[2]:].removeprefix('\n')
    offsets = [0, *(match.end() for match in re.finditer('\n', text)), len(text)]
    emitted = False
    split = _paragraphs if markdown and sections else _fixed
    for headings, start, end, anchor in _sections(text, markdown and sections):
        trail = ' > '.join(headings)[:320]
        context = '\n'.join(part for part in (trail, properties) if part)
        prefix = context + '\n\n' if context else ''
        start_offset = offsets[start]
        body = text[start_offset:offsets[end]]
        found = False
        for fragment, left, right in split(body, size - len(prefix)):
            found = emitted = True
            yield Chunk(prefix + fragment, headings,
                        frontmatter_lines + bisect_right(offsets, start_offset + left),
                        frontmatter_lines + bisect_right(offsets, start_offset + right - 1))
        if headings and not found:
            emitted = True
            line = frontmatter_lines + anchor + 1
            yield Chunk(context, headings, line, line)
    if properties and not emitted:
        yield Chunk(properties, (), 1, frontmatter_lines)
