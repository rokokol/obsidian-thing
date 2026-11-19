import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Literal

import yaml

from .links import Resolver, iter_links

KNOWN_CHECKS = ('quotes', 'callouts', 'broken_links', 'bases', 'canvas')

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


@dataclass(frozen=True)
class Finding:
    code: str
    payload: str
    line: int
    level: Literal['error', 'warning']
    message: str


def _blank_inline_code(line: str) -> str:
    return _INLINE_CODE.sub(lambda m: ' ' * len(m.group()), line)


def _quote_findings(line: str, number: int) -> list[Finding]:
    straight = [i for i, char in enumerate(line) if char == '"']
    spans = list(zip(straight[0::2], straight[1::2]))
    found = []
    open_angle = None
    for i, char in enumerate(line):
        if char in '“”„':
            found.append(Finding('quotes', char, number, 'error', 'Use a plain " instead of a typographic quote'))
        if char == '«' and open_angle is None:
            open_angle = i
        if char == '»' and open_angle is not None:
            span = line[open_angle:i + 1]
            start = open_angle
            open_angle = None
            if not any(a < i < b for a, b in spans):
                found.append(Finding('quotes', span, number, 'error',
                                     'Use plain quotes; keep them only for a nested quote'))
    if open_angle is not None and not any(a < open_angle < b for a, b in spans):
        found.append(Finding('quotes', line[open_angle:].strip(), number, 'error',
                             'Use plain quotes; keep them only for a nested quote'))
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


def _base_findings(text: str) -> list[Finding]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return [Finding('bases', 'yaml', 0, 'error', 'The base is not valid YAML')]
    if not isinstance(data, dict):
        return [Finding('bases', 'mapping', 0, 'error', 'A base is a YAML mapping')]
    found = []
    raw_formulas = data.get('formulas')
    formulas: dict[str, object] = raw_formulas if isinstance(raw_formulas, dict) else {}
    views = data.get('views')
    if views is not None and not isinstance(views, list):
        found.append(Finding('bases', 'views', 0, 'error', 'views is a list'))
        views = []
    for index, view in enumerate(views or []):
        kind = view.get('type') if isinstance(view, dict) else None
        if kind not in BASE_VIEWS:
            found.append(Finding('bases', f'views[{index}].type', 0, 'warning',
                                 f'Unknown view type "{kind}"'))
        if isinstance(view, dict):
            for field in ('order', 'properties'):
                for name in view.get(field) or []:
                    if isinstance(name, str) and name.startswith('formula.') and name[8:] not in formulas:
                        found.append(Finding('bases', name, 0, 'warning', f'{name} is not defined in formulas'))
    for name in data.get('order') or []:
        if isinstance(name, str) and name.startswith('formula.') and name[8:] not in formulas:
            found.append(Finding('bases', name, 0, 'warning', f'{name} is not defined in formulas'))
    return found


def _canvas_findings(text: str) -> list[Finding]:
    try:
        data = json.loads(text)
    except ValueError:
        return [Finding('canvas', 'json', 0, 'error', 'The canvas is not valid JSON')]
    if not isinstance(data, dict):
        return [Finding('canvas', 'mapping', 0, 'error', 'A canvas is a JSON object')]
    nodes = data.get('nodes') or []
    edges = data.get('edges') or []
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
        if kind not in CANVAS_TYPES:
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
    for edge in edges:
        if not isinstance(edge, dict):
            found.append(Finding('canvas', 'edge', 0, 'error', 'An edge is an object'))
            continue
        identifier = edge.get('id')
        for field in ('fromNode', 'toNode'):
            if edge.get(field) not in ids:
                found.append(Finding('canvas', str(identifier), 0, 'error', f'{field} names no node'))
        for field, allowed in (('fromSide', CANVAS_SIDES), ('toSide', CANVAS_SIDES),
                               ('fromEnd', CANVAS_ENDS), ('toEnd', CANVAS_ENDS)):
            if field in edge and edge[field] not in allowed:
                found.append(Finding('canvas', str(identifier), 0, 'warning', f'Unknown {field} "{edge[field]}"'))
        if identifier in ids:
            found.append(Finding('canvas', str(identifier), 0, 'error', f'Duplicate id {identifier}'))
        ids.add(str(identifier))
    return found


def scan(path: str, text: str, options: dict[str, bool], resolver: Resolver | None) -> list[Finding]:
    suffix = path.rsplit('.', 1)[-1].lower() if '.' in path else ''
    found: list[Finding] = []
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
        if options.get('quotes'):
            found.extend(_quote_findings(line, number))
        if options.get('callouts'):
            found.extend(_callout_findings(line, number))
    if options.get('broken_links') and resolver is not None:
        found.extend(_link_findings(text, path, resolver))
    return found


def compare(before: str | None, after: str, path: str, options: dict[str, bool],
            resolver: Resolver | None) -> tuple[list[str], list[str]]:
    remaining = Counter((item.code, item.payload) for item in scan(path, before or '', options, resolver))
    errors: list[str] = []
    warnings: list[str] = []
    for item in scan(path, after, options, resolver):
        key = item.code, item.payload
        label = f'[{item.code}] {item.message}: {item.payload}'
        if remaining[key]:
            remaining[key] -= 1
            warnings.append(f'Existing {label}')
            continue
        if item.level == 'error':
            errors.append(label)
        else:
            warnings.append(label)
    return errors, warnings
