import json
import re
from collections.abc import Iterator
from dataclasses import replace
from mimetypes import guess_type
from posixpath import dirname, relpath
from threading import Lock
from typing import Literal, NotRequired, TypedDict
from urllib.parse import quote, unquote

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline, autolink, image, link
from markdown_it.token import Token
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.front_matter import front_matter_plugin

from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from .frontmatter import frontmatter_nodes
from .links import Link, Resolver, Scalar, _WIKILINK, wiki_link
from .structured_links import base_links, canvas_links
from .vault import TextFile, Vault, VaultError

_SCHEME = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.-]*:|^//')
_GALLERY_FENCES = {'media-gallery', 'img-gallery', 'img-gal'}
_GALLERY_IMAGE = re.compile(r'!\[[^\]]*\]\((.+?)\)')
_GALLERY_MEDIA = re.compile(
    r'\.(?:jpeg|jpg|gif|png|webp|tiff|tif|bmp|svg|avif|mp4|webm|mov|m4v|ogv|'
    r'mp3|m4a|wav|ogg|oga|flac|aac|opus)$', re.IGNORECASE,
)
_GALLERY_FOLDER = re.compile(r'path\s*:\s*(\S.*)', re.IGNORECASE)
_GALLERY_ROOT = {'.', '/', '*', '**'}

LinkStatus = Literal['resolved', 'unresolved', 'ambiguous', 'unresolved_anchor', 'ambiguous_anchor', 'external']


class Edge(TypedDict):
    source: str
    raw: str
    target: str
    anchor: str
    embed: bool
    syntax: str
    origin: str
    property: NotRequired[str]
    status: LinkStatus
    resolved: list[str]


class Attachment(TypedDict):
    path: str
    mime_type: str
    size_bytes: int
    links: list[Edge]


class Backlinks(TypedDict):
    links: list[Edge]
    skipped: list[str]


def _target(raw: str, url: str, *, embed: bool, syntax: str = 'markdown',
            origin: str = 'markdown') -> Link:
    if _SCHEME.match(url):
        return Link(url, '', embed, raw, syntax, origin, uri=url)
    target, _, anchor = url.partition('#')
    if syntax == 'markdown' and origin in ('markdown', 'property'):
        target, anchor = unquote(target.split('?', 1)[0]), unquote(anchor)
    return Link(target, anchor, embed, raw, syntax, origin, uri=url)


def _wiki_inline(state: StateInline, silent: bool) -> bool:
    match = _WIKILINK.match(state.src, state.pos, state.posMax)
    if match is None:
        return False
    if not silent:
        token = state.push('wiki_link', '', 0)
        edge = wiki_link(match)
        if edge is not None:
            token.meta['edge'] = replace(edge, offset=state.pos)
    state.pos = match.end()
    return True


def _capture(rule, attr: str, *, embed: bool = False):
    def capture(state: StateInline, silent: bool) -> bool:
        start, first = state.pos, len(state.tokens)
        if not rule(state, silent):
            return False
        if not silent:
            for token in state.tokens[first:]:
                url = token.attrGet(attr)
                if isinstance(url, str):
                    edge = _target(state.src[start:state.pos], url, embed=embed)
                    title = token.attrGet('title')
                    token.meta['edge'] = replace(edge, offset=start, title=title if isinstance(title, str) else '')
                    break
        return True

    return capture


def _gallery_links(content: str, origin: str) -> Iterator[Link]:
    folder = None
    for line in content.splitlines():
        raw = line.strip()
        setting = _GALLERY_FOLDER.fullmatch(raw)
        if setting is not None:
            folder = Link(setting[1], '', True, raw, 'gallery_path', origin)
            continue
        wiki = _WIKILINK.fullmatch(raw)
        image_match = _GALLERY_IMAGE.fullmatch(raw)
        if wiki:
            parsed = wiki_link(wiki, origin)
            if parsed is not None:
                yield parsed
        elif image_match:
            yield _target(raw, image_match[1], embed=True, origin=origin)
        elif raw.startswith(('http://', 'https://')) or (':' not in raw and _GALLERY_MEDIA.search(raw)):
            yield _target(raw, raw, embed=True, syntax='path', origin=origin)
    if folder is not None:
        yield folder


def _gallery_files(resolver: Resolver, selector: str) -> Iterator[str]:
    recursive = not selector.endswith('/*')
    if selector in _GALLERY_ROOT:
        folder = ''
    else:
        folder = selector.removesuffix('/**').removesuffix('/*').strip('/')
        if any(not part or part.startswith('.') for part in folder.split('/')):
            return
        if any(char in folder for char in ('*', '?', '\\', '\0')):
            return
    for path in resolver.files_in(folder, recursive=recursive):
        if _GALLERY_MEDIA.search(path):
            yield path


def _inline_links(tokens: list[Token]) -> Iterator[Link]:
    for token in tokens:
        edge = token.meta.get('edge')
        if edge is not None:
            yield edge


def _property_links(text: str) -> Iterator[Link]:
    _, root = frontmatter_nodes(text)
    if root is None:
        return
    yaml_start = text.index('\n')
    pending: list[tuple[Node, tuple[str, ...]]] = [(root, ())]
    expanded: set[Node] = set()
    while pending:
        node, key = pending.pop()
        if isinstance(node, ScalarNode):
            value = node.value.strip()
            if not value.startswith('['):
                continue
            tokens = _PARSER.parseInline(value)[0].children or []
            for edge in _inline_links(tokens):
                if edge.raw == value and not edge.embed:
                    start = yaml_start + node.start_mark.index
                    end = yaml_start + node.end_mark.index
                    offset = _unique_offset(text, edge.raw, start, end)
                    yield replace(edge, origin='property', property_key='.'.join(key), offset=offset)
        elif node not in expanded:
            expanded.add(node)
            if isinstance(node, MappingNode):
                pending.extend((value, (*key, name.value)) for name, value in reversed(node.value))
            elif isinstance(node, SequenceNode):
                pending.extend((node.value[index], (*key, str(index))) for index in reversed(range(len(node.value))))


def _unique_offset(text: str, part: str, start: int, end: int) -> int | None:
    offset = text.find(part, start, end)
    if offset < 0 or text.find(part, offset + len(part), end) >= 0:
        return None
    return offset


def extract_edges(text: str, *, media_gallery: bool = False) -> tuple[Link, ...]:
    original = text.removeprefix('\ufeff')
    bom = len(text) - len(original)
    lines = [0]
    for match in re.finditer(r'\r\n?|\n', original):
        lines.append(match.end())
    lines.append(len(original))
    result = list(_property_links(original))
    for token in _PARSER.parse(original):
        start, end = (lines[token.map[0]], lines[token.map[1]]) if token.map else (0, len(original))
        if token.type == 'inline':
            edges = _inline_links(token.children or [])
            block = _unique_offset(original, token.content, start, end)
        elif media_gallery and token.type == 'fence' and token.info.strip() in _GALLERY_FENCES:
            edges = _gallery_links(token.content, token.info.strip())
            block = None
        else:
            continue
        for edge in edges:
            offset = block + edge.offset if block is not None and edge.offset is not None else None
            if offset is None or not original.startswith(edge.raw, offset):
                offset = _unique_offset(original, edge.raw, start, end)
            result.append(replace(edge, offset=offset))
    if bom:
        result = [replace(edge, offset=edge.offset + bom) if edge.offset is not None else edge for edge in result]
    return tuple(result)


_PARSER = (MarkdownIt('commonmark').enable('table').use(front_matter_plugin)
           .use(dollarmath_plugin, double_inline=True))
_PARSER.inline.ruler.before('link', 'wikilink', _wiki_inline)
_PARSER.inline.ruler.at('link', _capture(link, 'href'))
_PARSER.inline.ruler.at('autolink', _capture(autolink, 'href'))
_PARSER.inline.ruler.at('image', _capture(image, 'src', embed=True))


def _is_note(path: str) -> bool:
    return path.lower().endswith('.md')


def _external(link: Link) -> bool:
    return link.syntax == 'canvas_url' or (link.syntax != 'canvas_file' and bool(_SCHEME.match(link.target)))


def _destinations(resolver: Resolver, source: str, link: Link) -> list[str]:
    if link.syntax == 'canvas_file':
        return [link.target] if resolver.has_file(link.target) else []
    return resolver.destinations(link.target, source)


def _document_links(path: str, content: str, *, media_gallery: bool) -> tuple[Link, ...]:
    try:
        if path.lower().endswith('.canvas'):
            return tuple(canvas_links(content, lambda text: extract_edges(text, media_gallery=media_gallery)))
        if path.lower().endswith('.base'):
            return tuple(base_links(content))
        return extract_edges(content, media_gallery=media_gallery)
    except ValueError as error:
        raise VaultError(f'Cannot inspect links in {path}: {error}') from None


def _resolve(resolver: Resolver, source: str, links: tuple[Link, ...], *,
             destination: str | None = None) -> Iterator[Edge]:
    for link in links:
        if link.syntax == 'gallery_path':
            found = False
            for path in _gallery_files(resolver, link.target):
                found = True
                if destination is None or path == destination:
                    yield _edge(source, link, 'resolved', [path])
                    if destination is not None:
                        break
            if not found and destination is None:
                yield _edge(source, link, 'unresolved', [])
            continue
        external = _external(link)
        resolved = [] if external else _destinations(resolver, source, link)
        if destination is not None and resolved != [destination]:
            continue
        status: LinkStatus
        if external:
            status = 'external'
        elif not resolved:
            status = 'unresolved'
        elif len(resolved) > 1:
            status = 'ambiguous'
        else:
            count = resolver.anchor_count(resolved[0], link.anchor)
            status = 'resolved' if count == 1 else 'ambiguous_anchor' if count else 'unresolved_anchor'
        yield _edge(source, link, status, resolved)


def _edge(source: str, link: Link, status: LinkStatus, resolved: list[str]) -> Edge:
    result: Edge = {'source': source, 'raw': link.raw, 'target': link.target, 'anchor': link.anchor,
                    'embed': link.embed, 'syntax': link.syntax, 'origin': link.origin,
                    'status': status, 'resolved': resolved}
    if link.property_key is not None:
        result['property'] = link.property_key
    return result


class LinkGraph:
    def __init__(self, vault: Vault):
        self.vault = vault
        self._cache: dict[str, tuple[str, tuple[Link, ...]]] = {}
        self._lock = Lock()

    def _is_source(self, path: str) -> bool:
        lower = path.lower()
        return _is_note(lower) or (self.vault.canvas and lower.endswith('.canvas')) or (self.vault.bases and lower.endswith('.base'))

    def _extract(self, note: TextFile) -> tuple[Link, ...]:
        path, revision = note['path'], note['revision']
        with self._lock:
            cached = self._cache.get(path)
            if cached is not None and cached[0] == revision:
                return cached[1]
            edges = _document_links(path, note['content'], media_gallery=self.vault.media_gallery)
            self._cache[path] = revision, edges
            return edges

    def outgoing(self, path: str) -> list[Edge]:
        if not self._is_source(path) or path.startswith('.obsidian/'):
            raise VaultError('The path must name a Markdown note, Canvas or Base')
        note = self.vault.read(path)
        resolver = Resolver(self.vault, {path: note['content']})
        return list(_resolve(resolver, path, self._extract(note)))

    def incoming(self, path: str) -> Backlinks:
        self.vault.file_size(path)
        resolver = Resolver(self.vault)
        notes = [source for source in resolver.files() if self._is_source(source)]
        with self._lock:
            for deleted in set(self._cache).difference(notes):
                del self._cache[deleted]
        links: list[Edge] = []
        skipped: list[str] = []
        for source in notes:
            try:
                note = self.vault.read(source)
                edges = self._extract(note)
            except VaultError:
                skipped.append(source)
                continue
            links.extend(_resolve(resolver, source, edges, destination=path))
        return {'links': links, 'skipped': skipped}

    def plan_rename(self, path: str, new_path: str) -> dict[str, tuple[TextFile, str]]:
        before = Resolver(self.vault)
        files = before.files()
        overlay = {new_path: ''}
        after = Resolver(self.vault, overlay, removed=frozenset({path}))
        plan: dict[str, tuple[TextFile, str]] = {}
        anchors: list[tuple[str, str, str]] = []
        for source in files:
            if not self._is_source(source):
                continue
            try:
                note = self.vault.read(source)
                links = self._extract(note)
            except VaultError as error:
                raise VaultError(f'Cannot verify backlinks in {source}: {error}') from None
            next_source = new_path if source == path else source
            changes = [edge for edge in links if edge.target and edge.syntax != 'gallery_path' and
                       not _external(edge) and _destinations(before, source, edge) == [path]]
            content = _rewrite_links(note['content'], source, next_source, new_path, changes)
            replaced = _document_links(next_source, content, media_gallery=self.vault.media_gallery) if changes else links
            if _path_connections(before, source, links, rename=(path, new_path)) != _path_connections(after, next_source, replaced):
                raise VaultError(f'The new name would change link resolution in {source}')
            for edge in links:
                if edge.anchor and not _external(edge):
                    candidates = _destinations(before, source, edge)
                    if len(candidates) == 1:
                        anchors.append((source, candidates[0], edge.anchor))
            if changes:
                if _is_note(source) and frontmatter_nodes(note['content'])[1] is not None and frontmatter_nodes(content)[1] is None:
                    raise VaultError(f'The new name would damage properties in {source}')
                plan[source] = (note, content)
            if changes or source == path:
                overlay[next_source] = content
        for source, destination, anchor in anchors:
            if destination == path or destination in plan:
                next_destination = new_path if destination == path else destination
                if before.anchor_count(destination, anchor) != after.anchor_count(next_destination, anchor):
                    raise VaultError(f'The new name would change anchor resolution in {source}')
        return plan

    def check_removal(self, path: str) -> None:
        backlinks = self.incoming(path)
        if backlinks['skipped']:
            raise VaultError('Cannot verify backlinks in: ' + ', '.join(backlinks['skipped'][:6]))
        sources = sorted({edge['source'] for edge in backlinks['links']
                          if edge['source'] != path and edge['syntax'] != 'gallery_path'})
        if sources:
            raise VaultError('Deletion would leave broken links in: ' + ', '.join(sources[:6]))

    def media(self, path: str) -> list[Attachment]:
        media: dict[str, Attachment] = {}
        for edge in self.outgoing(path):
            if edge['status'] != 'resolved':
                continue
            target, = edge['resolved']
            if _is_note(target):
                continue
            if target not in media:
                media[target] = {'path': target, 'mime_type': guess_type(target)[0] or 'application/octet-stream',
                                 'size_bytes': self.vault.file_size(target), 'links': []}
            media[target]['links'].append(edge)
        return list(media.values())


def _path_connections(resolver: Resolver, source: str, links: tuple[Link, ...],
                      *, rename: tuple[str, str] | None = None) -> list[tuple[object, ...]]:
    results: list[tuple[object, ...]] = []
    for edge in links:
        if edge.syntax == 'gallery_path':
            files = list(_gallery_files(resolver, edge.target))
        elif _external(edge):
            files = []
        else:
            files = _destinations(resolver, source, edge)
        if rename is not None:
            old, new = rename
            files = [new if target == old else target for target in files]
        results.append((edge.origin, edge.property_key, edge.embed, edge.anchor, tuple(sorted(files))))
    return results


def _apply_link_edits(text: str, source: str, edits: dict[int, tuple[str, str]]) -> str:
    end = len(text)
    for offset, (old, new) in sorted(edits.items(), reverse=True):
        if offset + len(old) > end or text[offset:offset + len(old)] != old:
            raise VaultError(f'Conflicting link edits in {source}')
        text = text[:offset] + new + text[offset + len(old):]
        end = offset
    return text


def _rewrite_links(text: str, source: str, next_source: str, destination: str, links: list[Link]) -> str:
    grouped: dict[Scalar | None, dict[int, tuple[str, str]]] = {}
    for edge in links:
        if edge.offset is None or (edge.scalar is not None and not edge.scalar.editable):
            raise VaultError(f'Cannot locate a link safely in {source}: {edge.raw}')
        edits = grouped.setdefault(edge.scalar, {})
        change = edge.raw, _rename_link(edge, next_source, destination)
        if edits.setdefault(edge.offset, change) != change:
            raise VaultError(f'Conflicting link edits in {source}')
    outer = grouped.pop(None, {})
    for scalar, edits in grouped.items():
        if scalar is None:
            continue
        original = text[scalar.start:scalar.end]
        value = _apply_link_edits(scalar.value, source, edits)
        encoded = "'" + value.replace("'", "''") + "'" if original.startswith("'") and '\n' not in value else json.dumps(value, ensure_ascii=False)
        if outer.setdefault(scalar.start, (original, encoded)) != (original, encoded):
            raise VaultError(f'Conflicting scalar edits in {source}')
    return _apply_link_edits(text, source, outer)


def _replace_wiki_target(raw: str, target: str) -> str:
    start = raw.index('[[') + 2
    separator = re.search(r'#|\\?\|', raw[start:-2])
    end = start + separator.start() if separator else len(raw) - 2
    if any(char in target for char in '#|[]'):
        raise VaultError('The new name cannot be used in a wikilink')
    return raw[:start] + target + raw[end:]


def _rename_link(edge: Link, source: str, destination: str) -> str:
    relative = relpath(destination, dirname(source) or '.')
    if not relative.startswith('../'):
        relative = './' + relative
    raw = edge.raw
    if edge.syntax == 'canvas_file':
        return destination
    if edge.syntax == 'base_literal':
        return json.dumps(destination + ('#' + edge.anchor if edge.anchor else ''), ensure_ascii=False)
    if edge.syntax == 'base_wiki':
        return json.dumps(_replace_wiki_target(edge.uri, destination), ensure_ascii=False)
    if edge.syntax == 'wiki':
        return _replace_wiki_target(raw, relative)
    if edge.syntax == 'path':
        return relative
    if edge.origin in _GALLERY_FENCES:
        match = _GALLERY_IMAGE.fullmatch(raw)
        if match is not None:
            suffix = edge.uri[len(edge.uri.split('#', 1)[0]):]
            return raw[:match.start(1)] + relative + suffix + raw[match.end(1):]
    suffix = edge.uri[len(re.split(r'[?#]', edge.uri, maxsplit=1)[0]):]
    uri = quote(relative) + suffix
    if raw.startswith('<'):
        return '<' + uri + '>'
    start = int(edge.embed)
    state = StateInline(raw, _PARSER, {}, [])
    end = _PARSER.helpers.parseLinkLabel(state, start) + 1
    if end <= start:
        raise VaultError(f'Cannot preserve the label in {raw}')
    if raw[end:].startswith('('):
        index = end + 1
        while index < len(raw) and raw[index] in ' \t\n\r':
            index += 1
        target = _PARSER.helpers.parseLinkDestination(raw, index, len(raw))
        if target.ok:
            if raw[index:index + 1] == '<':
                uri = '<' + uri + '>'
            return raw[:index] + uri + raw[target.pos:]
    title = edge.title.replace('\\', '\\\\').replace('"', '\\"')
    return raw[:end] + '(' + uri + (f' "{title}"' if title else '') + ')'
