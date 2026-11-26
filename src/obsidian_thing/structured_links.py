import json
import re
from collections.abc import Callable, Iterator
from dataclasses import replace

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from .links import Link, Scalar, _WIKILINK, wiki_link
from .structured_documents import load_canvas

_EXPR_TOKEN = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|/(?:\\.|[^/\\\n])+/[a-z]*|[A-Za-z_$][\w$]*|[^\s]''')
_LITERAL_CALLS = {'link', 'file', 'image'}


def _fields(node: Node) -> dict[str, Node]:
    if not isinstance(node, MappingNode):
        raise ValueError('Expected a mapping')
    result: dict[str, Node] = {}
    for key, value in node.value:
        if not isinstance(key, ScalarNode) or key.value in result:
            raise ValueError('Expected distinct field names')
        result[key.value] = value
    return result


def _yaml_fields(text: str) -> dict[str, Node]:
    try:
        root = YAML(typ='base').compose(text)
    except (YAMLError, RecursionError) as error:
        raise ValueError('Cannot parse document') from error
    return {} if root is None else _fields(root)


def _sequence(node: Node | None) -> list[Node]:
    if node is None:
        return []
    if isinstance(node, SequenceNode):
        return node.value
    raise ValueError('Expected a list')


def _canvas_scalar(text: str, node: Node | None) -> Scalar:
    if not isinstance(node, ScalarNode):
        raise ValueError('Expected a Canvas string')
    start, end = node.start_mark.index, node.end_mark.index
    value = json.loads(text[start:end])
    if not isinstance(value, str):
        raise ValueError('Expected a Canvas string')
    return Scalar(start, end, value)


def canvas_links(text: str, markdown: Callable[[str], tuple[Link, ...]]) -> Iterator[Link]:
    load_canvas(text)
    for node in _sequence(_yaml_fields(text).get('nodes')):
        fields = _fields(node)
        kind = _canvas_scalar(text, fields.get('type')).value
        if kind == 'text':
            scalar = _canvas_scalar(text, fields.get('text'))
            for edge in markdown(scalar.value):
                origin = 'canvas_text' if edge.origin == 'markdown' else edge.origin
                yield replace(edge, scalar=scalar, origin=origin)
        elif kind == 'file' or (kind == 'group' and 'background' in fields):
            key = 'file' if kind == 'file' else 'background'
            scalar = _canvas_scalar(text, fields.get(key))
            anchor = _canvas_scalar(text, fields['subpath']).value.removeprefix('#') if 'subpath' in fields else ''
            yield Link(scalar.value, anchor, True, scalar.value, 'canvas_file', f'canvas_{key}', offset=0, scalar=scalar)
        elif kind == 'link':
            url = _canvas_scalar(text, fields.get('url')).value
            yield Link(url, '', False, url, 'canvas_url', 'canvas_url')


def _base_views(fields: dict[str, Node]) -> Iterator[dict[str, Node]]:
    for view in _sequence(fields.get('views')):
        yield _fields(view)


def base_views(text: str) -> Iterator[str]:
    for view in _base_views(_yaml_fields(text)):
        name = view.get('name')
        if isinstance(name, ScalarNode):
            yield name.value


def _filter_scalars(root: Node) -> Iterator[ScalarNode]:
    pending = [root]
    seen: set[Node] = set()
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        if isinstance(node, ScalarNode):
            yield node
        elif isinstance(node, MappingNode):
            fields = _fields(node)
            pending.extend(value for key, value in reversed(list(fields.items())) if key in {'and', 'or', 'not'})
        elif isinstance(node, SequenceNode):
            pending.extend(reversed(node.value))


def _expression_links(scalar: Scalar) -> Iterator[Link]:
    tokens = list(_EXPR_TOKEN.finditer(scalar.value))
    for index, (function, opening, literal, closing) in enumerate(zip(tokens, tokens[1:], tokens[2:], tokens[3:])):
        name = function[0]
        method = index > 0 and tokens[index - 1][0] == '.'
        if name not in ({'hasLink'} if method else _LITERAL_CALLS):
            continue
        raw = literal[0]
        if opening[0] != '(' or closing[0] not in (',', ')') or len(raw) < 2 or raw[0] not in '\'"':
            continue
        quoted = raw[0]
        body = raw[1:-1]
        if quoted == "'":
            body = re.sub(r'''\\'|"''', lambda match: "'" if match[0] == "\\'" else '\\"', body)
        try:
            value = json.loads('"' + body + '"')
        except ValueError:
            continue
        origin = 'base_' + name
        wiki = _WIKILINK.fullmatch(value)
        if wiki:
            parsed = wiki_link(wiki, origin)
            if parsed is None:
                continue
            edge = replace(parsed, raw=raw, syntax='base_wiki', uri=value, embed=name == 'image')
        else:
            target, _, anchor = value.partition('#')
            edge = Link(target, anchor, name == 'image', raw, 'base_literal', origin, uri=value)
        yield replace(edge, offset=literal.start(), scalar=scalar)


def base_links(text: str) -> Iterator[Link]:
    fields = _yaml_fields(text)
    expressions: list[ScalarNode] = []
    if 'filters' in fields:
        expressions.extend(_filter_scalars(fields['filters']))
    for name in ('formulas', 'summaries'):
        if name in fields:
            expressions.extend(node for node in _fields(fields[name]).values() if isinstance(node, ScalarNode))
    for view in _base_views(fields):
        if 'filters' in view:
            expressions.extend(_filter_scalars(view['filters']))
    for node in dict.fromkeys(expressions):
        start, end = node.start_mark.index, node.end_mark.index
        yield from _expression_links(Scalar(start, end, node.value, editable=text[start:start + 1] not in ('&', '!')))
