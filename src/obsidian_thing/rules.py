import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal
from unicodedata import category

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.front_matter import front_matter_plugin

from .links import Resolver, iter_links
from .structured_documents import load_base, load_canvas

KNOWN_CHECKS = ('default_quotes', 'inner_quoter', 'trailing_periods', 'inline_code_equals',
                'heading_math', 'unicode_math', 'link_math', 'paragraph_lines', 'callouts',
                'prevent_inline_tags', 'broken_links', 'bases', 'canvas')
UNICODE_MATH = frozenset('≤≥≠≈∞∈∉⊂⊃⊆⊇∑∏∫∮√±×÷∂∇ℝℕℤℚℂ')
QUOTE_CHARS = frozenset('"“”„«»')
QUOTE_KEYS = ('default_quotes', 'inner_quoter')

def _markdown_parser() -> MarkdownIt:
    return (MarkdownIt('commonmark').enable('table')
            .use(front_matter_plugin).use(dollarmath_plugin, double_inline=True))


_MARKDOWN = _markdown_parser()
_SENTENCE_DOT = re.compile(r'(?<![.\d])\.$')
_ABBREVIATION = re.compile(r'\b(?:и\s*т\.\s*[дп]|т\.\s*[ек]|см|напр)\.$', re.I)

CALLOUT_TYPES = frozenset({
    'note', 'abstract', 'summary', 'tldr', 'info', 'todo', 'tip', 'hint',
    'important', 'success', 'check', 'done', 'question', 'help', 'faq',
    'warning', 'caution', 'attention', 'failure', 'fail', 'missing',
    'danger', 'error', 'bug', 'example', 'quote', 'cite',
})
CANVAS_TYPES = frozenset({'text', 'file', 'link', 'group'})
CANVAS_SIDES = frozenset({'top', 'right', 'bottom', 'left'})
CANVAS_ENDS = frozenset({'none', 'arrow'})
CANVAS_REQUIRED = {'text': ('text',), 'file': ('file',), 'link': ('url',), 'group': ()}
BASE_VIEWS = frozenset({'table', 'cards', 'list', 'map'})

_FENCE = re.compile(r'^\s*(?:>\s*)*(?:```|~~~)')
_INLINE_CODE = re.compile(r'`[^`\n]*`')
_BLOCKQUOTE_MARKER = re.compile(r'^(?:\s*>)+\s*\[!([^\]\s]+)\]')
_PROSE_MARKER = re.compile(r'\[!([^\]\s]+)\]')
_WIKILINK = re.compile(r'!?\[\[[^\]\n]*\]\]')


@dataclass(frozen=True)
class Finding:
    code: str
    payload: str
    line: int
    level: Literal['error', 'warning']
    message: str


def _tag_character(char: str) -> bool:
    return (char.isalnum() or char in '_-/'
            or not char.isascii() and category(char) in {'So', 'Sk', 'Mn', 'Mc'})


def _tag_inline(state: StateInline, silent: bool) -> bool:
    source, start, limit = state.src, state.pos, state.posMax
    tag = ''
    if source.startswith(('[[', '![['), start):
        link = _WIKILINK.match(source, start, limit)
        if link is None:
            return False
        end = link.end()
    elif source.startswith('%%', start):
        close = source.find('%%', start + 2, limit)
        if close < 0:
            return False
        end = close + 2
    elif source[start] == '#' and not state.linkLevel:
        if start and (source[start - 1].isalnum() or source[start - 1] in '-/_#?=&%'):
            return False
        end = start + 1
        while end < limit and _tag_character(source[end]):
            end += 1
        name = source[start + 1:end].rstrip('/')
        if not name or all(char.isdecimal() for char in name):
            return False
        tag = '#' + name
    else:
        return False
    if not silent:
        node = state.push('obsidian_tag' if tag else 'obsidian_literal', '', 0)
        node.content = tag
    state.pos = end
    return True


_TAG_MARKDOWN = _markdown_parser()
_TAG_MARKDOWN.inline.ruler.before('text', 'obsidian_tag', _tag_inline)


def iter_inline_tags(text: str) -> Iterator[tuple[str, int]]:
    for block in _TAG_MARKDOWN.parse(text.removeprefix('\ufeff')):
        if block.type != 'inline':
            continue
        line = (block.map or [0])[0] + 1
        for token in block.children or ():
            if token.type == 'obsidian_tag':
                yield token.content, line


def _tag_findings(text: str) -> list[Finding]:
    return [Finding('prevent_inline_tags', tag, line, 'error',
                    'Put tags in YAML or escape a literal hash or use inline code')
            for tag, line in iter_inline_tags(text)]


def _blank_inline_code(line: str) -> str:
    return _INLINE_CODE.sub(lambda m: ' ' * len(m.group()), line)


def _quote_pair(value: object, name: str) -> tuple[str, str] | None:
    if value is False:
        return None
    if not isinstance(value, str) or len(value) != 2 or set(value) - QUOTE_CHARS:
        raise ValueError(f'Configure {name} as an opening and a closing quote character')
    return value[0], value[1]


QUOTE_CLOSERS = {'«': '»', '“': '”', '„': '”'}


def _quote_findings(line: str, number: int, outer: tuple[str, str],
                    inner: tuple[str, str] | None) -> list[Finding]:
    if outer[0] == outer[1]:
        positions = [i for i, char in enumerate(line) if char == outer[0]]
        spans = list(zip(positions[0::2], positions[1::2]))
    else:
        opens = [i for i, char in enumerate(line) if char == outer[0]]
        closes = [i for i, char in enumerate(line) if char == outer[1]]
        spans = list(zip(opens, closes))
    found = []
    i = 0
    while i < len(line):
        char = line[i]
        if char not in QUOTE_CHARS or char in outer:
            i += 1
            continue
        if inner is not None and char in inner and any(a < i < b for a, b in spans):
            i += 1
            continue
        closer = QUOTE_CLOSERS.get(char)
        end = line.find(closer, i + 1) if closer else -1
        payload = line[i:end + 1] if end != -1 else char
        i = end + 1 if end != -1 else i + 1
        found.append(Finding('default_quotes', payload, number, 'error',
                             f'Use {outer[0]} for quotes, and other marks only for a nested quote'))
    return found


def _callout_findings(line: str, number: int) -> list[Finding]:
    marker = _BLOCKQUOTE_MARKER.match(line)
    if marker:
        kind = marker.group(1)
        if kind.lower() not in CALLOUT_TYPES:
            return [Finding('callouts', kind, number, 'warning', f'Unknown callout type "{kind}"')]
        return []
    prose = _PROSE_MARKER.search(line)
    if prose:
        return [Finding('callouts', prose.group(0), number, 'warning',
                        'A callout marker outside a blockquote renders as text')]
    return []


def _link_findings(text: str, path: str, resolver: Resolver) -> list[Finding]:
    found = []
    for number, raw in enumerate(text.split('\n'), 1):
        for link in iter_links(raw):
            if not link.target:
                if link.anchor and not resolver.resolves_anchor(path, link.anchor):
                    found.append(Finding('broken_links', link.payload, number, 'error',
                                         'The anchor does not exist in this note'))
                continue
            destinations = resolver.destinations(link.target, path)
            if not destinations:
                kind = 'attachment' if link.embed else 'note'
                found.append(Finding('broken_links', link.payload, number, 'error',
                                     f'The link points to no {kind} in the vault'))
            elif link.anchor and not any(resolver.resolves_anchor(item, link.anchor) for item in destinations):
                found.append(Finding('broken_links', link.payload, number, 'error',
                                     'The anchor does not exist in the target note'))
    return found


def _valid_filter(value: object) -> bool:
    pending = [value]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        if isinstance(node, str):
            if not node.strip():
                return False
            continue
        if not isinstance(node, dict) or len(node) != 1:
            return False
        key, children = next(iter(node.items()))
        if key not in ('and', 'or', 'not') or not isinstance(children, list):
            return False
        if id(node) not in seen:
            seen.add(id(node))
            pending.extend(children)
    return True


def _base_findings(text: str) -> list[Finding]:
    try:
        data = load_base(text)
    except ValueError as error:
        return [Finding('bases', 'yaml', 0, 'error', str(error))]
    found = []
    formulas = data.get('formulas', {})
    if not isinstance(formulas, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                             for key, value in formulas.items()):
        found.append(Finding('bases', 'formulas', 0, 'error', 'formulas must map names to strings'))
        formulas = {}
    if 'filters' in data and not _valid_filter(data['filters']):
        found.append(Finding('bases', 'filters', 0, 'error', 'Expected a filter string or filter group'))
    views = data.get('views', [])
    if not isinstance(views, list):
        found.append(Finding('bases', 'views', 0, 'error', 'views must be a list'))
        views = []
    for index, view in enumerate(views):
        prefix = f'views[{index}]'
        if not isinstance(view, dict):
            found.append(Finding('bases', prefix, 0, 'error', 'A view must be a mapping'))
            continue
        kind = view.get('type')
        if not isinstance(kind, str) or not kind.strip():
            found.append(Finding('bases', prefix + '.type', 0, 'error', 'A view needs a type'))
        elif kind not in BASE_VIEWS:
            found.append(Finding('bases', prefix + '.type', 0, 'warning', f'Unknown view type "{kind}"'))
        if 'name' in view and not isinstance(view['name'], str):
            found.append(Finding('bases', prefix + '.name', 0, 'error', 'A view name must be a string'))
        if 'filters' in view and not _valid_filter(view['filters']):
            found.append(Finding('bases', prefix + '.filters', 0, 'error', 'Expected a filter string or filter group'))
        for field in ('order', 'properties'):
            fields = view.get(field, [])
            if isinstance(fields, list):
                for name in fields:
                    if isinstance(name, str) and name.startswith('formula.') and name[8:] not in formulas:
                        found.append(Finding('bases', name, 0, 'warning', f'{name} is not defined in formulas'))
    return found


def _canvas_findings(text: str) -> list[Finding]:
    try:
        data = load_canvas(text)
    except ValueError as error:
        return [Finding('canvas', 'json', 0, 'error', str(error))]
    nodes = data.get('nodes', [])
    edges = data.get('edges', [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return [Finding('canvas', 'arrays', 0, 'error', 'nodes and edges are arrays')]
    found = []
    ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            found.append(Finding('canvas', 'node', 0, 'error', 'A node is an object'))
            continue
        identifier = node.get('id')
        if not isinstance(identifier, str) or not identifier:
            found.append(Finding('canvas', 'node.id', 0, 'error', 'A node has no id'))
            continue
        if identifier in ids:
            found.append(Finding('canvas', identifier, 0, 'error', f'Duplicate id {identifier}'))
        ids.add(identifier)
        kind = node.get('type')
        if not isinstance(kind, str) or kind not in CANVAS_TYPES:
            found.append(Finding('canvas', identifier, 0, 'error', f'Unknown node type "{kind}"'))
            continue
        for field in CANVAS_REQUIRED[kind]:
            if not isinstance(node.get(field), str):
                found.append(Finding('canvas', identifier, 0, 'error', f'A {kind} node needs "{field}"'))
        for field in ('x', 'y', 'width', 'height'):
            if not isinstance(node.get(field), int) or isinstance(node.get(field), bool):
                found.append(Finding('canvas', identifier, 0, 'error', f'A node needs integer "{field}"'))
        if '\\\\n' in str(node.get('text', '')):
            found.append(Finding('canvas', identifier, 0, 'warning', 'A literal \\\\n renders as text; use \\n'))
    node_ids = ids.copy()
    for edge in edges:
        if not isinstance(edge, dict):
            found.append(Finding('canvas', 'edge', 0, 'error', 'An edge is an object'))
            continue
        identifier = edge.get('id')
        if not isinstance(identifier, str) or not identifier:
            found.append(Finding('canvas', 'edge.id', 0, 'error', 'An edge has no id'))
            continue
        for field in ('fromNode', 'toNode'):
            endpoint = edge.get(field)
            if not isinstance(endpoint, str) or endpoint not in node_ids:
                found.append(Finding('canvas', identifier, 0, 'error', f'{field} names no node'))
        for field, allowed in (('fromSide', CANVAS_SIDES), ('toSide', CANVAS_SIDES),
                               ('fromEnd', CANVAS_ENDS), ('toEnd', CANVAS_ENDS)):
            if field in edge and (not isinstance(edge[field], str) or edge[field] not in allowed):
                found.append(Finding('canvas', identifier, 0, 'error', f'Unknown {field} "{edge[field]}"'))
        if identifier in ids:
            found.append(Finding('canvas', identifier, 0, 'error', f'Duplicate id {identifier}'))
        ids.add(identifier)
    return found


def structured_errors(path: str, text: str) -> list[str]:
    if path.lower().endswith('.base'):
        findings = _base_findings(text)
    elif path.lower().endswith('.canvas'):
        findings = _canvas_findings(text)
    else:
        return []
    return [f'{item.message}: {item.payload}' for item in findings if item.level == 'error']


def _period_findings(text: str) -> list[Finding]:
    found = []
    parent = None
    for token in _MARKDOWN.parse(text):
        if token.type != 'inline':
            parent = token
            continue
        if parent is None or parent.type not in {'paragraph_open', 'td_open', 'th_open'}:
            continue
        if _BLOCKQUOTE_MARKER.match('> ' + token.content) or token.content.startswith('![['):
            continue
        visible = ''.join(
            child.content if child.type == 'text' else ' ' if child.type in {'softbreak', 'hardbreak'}
            else '\ufffc' if child.type in {'code_inline', 'math_inline', 'math_inline_double', 'image'}
            else ''
            for child in token.children or ()
        ).rstrip()
        if _SENTENCE_DOT.search(visible) and not _ABBREVIATION.search(visible):
            key = ' '.join(visible.split())
            line = (token.map or parent.map or [0])[0] + 1
            found.append(Finding('trailing_periods', key, line, 'error', 'Omit the final period'))
    return found


def _link_math_findings(text: str) -> list[Finding]:
    found = []
    for number, raw in enumerate(text.split('\n'), 1):
        for link in iter_links(raw):
            if '$' in link.raw:
                found.append(Finding('link_math', link.raw, number, 'error',
                                     'A formula inside a wikilink breaks its target or caption'))
    return found


def _markdown_findings(text: str, options: dict[str, object]) -> list[Finding]:
    found: list[Finding] = []
    parent = None
    paragraph: str | None = None
    for token in _MARKDOWN.parse(text):
        if token.type in {'paragraph_open', 'heading_open', 'td_open', 'th_open'}:
            parent = token
            paragraph = None
            if token.type == 'paragraph_open' and token.map and token.map[1] - token.map[0] > 1:
                paragraph = str(token.map[0] + 1)
            continue
        if token.type != 'inline' or parent is None:
            continue
        line = (token.map or parent.map or [0])[0] + 1
        if paragraph and options.get('paragraph_lines'):
            payload = ' '.join(token.content.split())[:60]
            found.append(Finding('paragraph_lines', payload, int(paragraph), 'warning',
                                 'Keep one paragraph on one line'))
            paragraph = None
        heading = parent.type == 'heading_open'
        for child in token.children or ():
            if child.type == 'code_inline' and options.get('inline_code_equals'):
                if child.content.lstrip().startswith('='):
                    found.append(Finding('inline_code_equals', child.content, line, 'error',
                                         'Inline code must not start with an equals sign'))
                continue
            if child.type in {'code_inline', 'math_inline', 'math_inline_double'}:
                continue
            marks = sorted({char for char in _WIKILINK.sub(' ', child.content) if char in UNICODE_MATH})
            for char in marks:
                if heading and options.get('heading_math'):
                    found.append(Finding('heading_math', char, line, 'error',
                                         'Write a heading formula in TeX'))
                elif not heading and options.get('unicode_math'):
                    found.append(Finding('unicode_math', char, line, 'warning',
                                         'Prefer TeX for a math symbol'))
    return found


def scan(path: str, text: str, options: dict[str, object], resolver: Resolver | None) -> list[Finding]:
    suffix = path.rsplit('.', 1)[-1].lower() if '.' in path else ''
    found: list[Finding] = []
    outer = _quote_pair(options['default_quotes'], 'default_quotes') if 'default_quotes' in options else None
    inner = _quote_pair(options.get('inner_quoter', False), 'inner_quoter')
    if suffix == 'base' and options.get('bases'):
        found.extend(_base_findings(text))
    if suffix == 'canvas' and options.get('canvas'):
        found.extend(_canvas_findings(text))
    if suffix != 'md':
        return found
    in_fence = False
    for number, raw in enumerate(text.split('\n'), 1):
        if _FENCE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = _blank_inline_code(raw)
        if outer is not None:
            found.extend(_quote_findings(line, number, outer, inner))
        if options.get('callouts'):
            found.extend(_callout_findings(line, number))
    if options.get('trailing_periods'):
        found.extend(_period_findings(text))
    if any(options.get(name) for name in ('inline_code_equals', 'heading_math', 'unicode_math', 'paragraph_lines')):
        found.extend(_markdown_findings(text, options))
    if options.get('link_math'):
        found.extend(_link_math_findings(text))
    if options.get('prevent_inline_tags'):
        found.extend(_tag_findings(text))
    if options.get('broken_links') and resolver is not None:
        found.extend(_link_findings(text, path, resolver))
    return found


def compare(before: str | None, after: str, path: str, options: dict[str, object],
            resolver: Resolver | None) -> tuple[list[str], list[str]]:
    remaining = Counter((item.code, item.payload) for item in scan(path, before or '', options, resolver))
    errors: list[str] = []
    warnings: list[str] = []
    for item in scan(path, after, options, resolver):
        key = item.code, item.payload
        label = (f'[{item.code}, line {item.line}] {item.message}' if item.code == 'trailing_periods'
                 else f'[{item.code}] {item.message}: {item.payload}')
        if remaining[key]:
            remaining[key] -= 1
            warnings.append(f'Existing {label}')
            continue
        if item.level == 'error':
            errors.append(label)
        else:
            warnings.append(label)
    return errors, warnings
