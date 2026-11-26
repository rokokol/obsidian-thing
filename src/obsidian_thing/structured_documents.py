import json
from io import StringIO
from typing import Any, Literal, NotRequired, TypedDict, cast

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError


class CanvasPut(TypedDict):
    op: Literal['put_node', 'put_edge']
    id: str
    fields: dict[str, Any]


class CanvasRemove(TypedDict):
    op: Literal['remove_node', 'remove_edge']
    id: str


CanvasChange = CanvasPut | CanvasRemove


class BaseFilters(TypedDict):
    op: Literal['set_filters']
    value: str | dict[str, Any] | None
    view: NotRequired[str]


class BaseFormula(TypedDict):
    op: Literal['set_formula']
    name: str
    value: str


class BaseView(TypedDict):
    op: Literal['put_view']
    name: str
    fields: dict[str, Any]


class BaseRemove(TypedDict):
    op: Literal['remove_formula', 'remove_view']
    name: str


BaseChange = BaseFilters | BaseFormula | BaseView | BaseRemove


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError('Canvas object keys must be unique')
        data[key] = value
    return data


def _invalid_json_constant(value: str) -> None:
    raise ValueError(f'{value} is not a JSON value')


def load_canvas(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text, object_pairs_hook=_json_object, parse_constant=_invalid_json_constant)
    except (ValueError, RecursionError) as error:
        raise ValueError('The canvas is not valid JSON') from error
    if not isinstance(data, dict):
        raise ValueError('A canvas is a JSON object')
    return data


def _yaml() -> YAML:
    parser = YAML(typ='rt')
    parser.preserve_quotes = True
    parser.indent(mapping=2, sequence=4, offset=2)
    return parser


def load_base(text: str, parser: YAML | None = None) -> dict[str, Any]:
    try:
        data = (parser if parser is not None else _yaml()).load(text)
    except (YAMLError, RecursionError, ValueError) as error:
        raise ValueError('The base is not valid YAML') from error
    if not isinstance(data, dict):
        raise ValueError('A base is a YAML mapping')
    return data


def _named(items: list[dict[str, Any]], key: str, value: str) -> int | None:
    hits = [index for index, item in enumerate(items) if item.get(key) == value]
    if len(hits) > 1:
        raise ValueError(f'More than one entry has {key} {value}')
    return hits[0] if hits else None


def _records(root: dict[str, Any], key: str) -> list[dict[str, Any]]:
    items = root.setdefault(key, [])
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError(f'{key} must contain a list of objects')
    return items


def _remove(items: list[dict[str, Any]], index: int | None, label: str) -> None:
    if index is None:
        raise ValueError(f'No entry named {label}')
    del items[index]


def _put(items: list[dict[str, Any]], index: int | None, key: str, name: str, fields: dict[str, Any]) -> None:
    if key in fields:
        raise ValueError(f'Pass {key} separately from fields')
    if index is None:
        items.append({key: name, **fields})
    else:
        items[index].update(fields)


def edit_canvas(text: str, changes: list[CanvasChange]) -> str:
    if not changes:
        raise ValueError('Pass at least one Canvas change')
    canvas = load_canvas(text)
    for change in changes:
        operation = change['op']
        identifier = change['id']
        if not identifier.strip():
            raise ValueError('A Canvas ID must not be empty')
        entries = _records(canvas, 'nodes' if operation in ('put_node', 'remove_node') else 'edges')
        index = _named(entries, 'id', identifier)
        if operation in ('put_node', 'put_edge'):
            _put(entries, index, 'id', identifier, cast(CanvasPut, change)['fields'])
        else:
            _remove(entries, index, identifier)
    return json.dumps(canvas, indent=2, ensure_ascii=False, allow_nan=False) + '\n'


def edit_base(text: str, changes: list[BaseChange]) -> str:
    if not changes:
        raise ValueError('Pass at least one Base change')
    parser = _yaml()
    base = load_base(text, parser)
    for change in changes:
        if change['op'] == 'set_filters':
            scope = base
            if 'view' in change:
                views = _records(base, 'views')
                index = _named(views, 'name', change['view'])
                if index is None:
                    raise ValueError(f"No view named {change['view']}")
                scope = views[index]
            if change['value'] is None:
                scope.pop('filters', None)
            else:
                scope['filters'] = change['value']
        elif change['op'] in ('set_formula', 'remove_formula'):
            formulas = base.setdefault('formulas', {})
            if not isinstance(formulas, dict):
                raise ValueError('formulas must be a mapping')
            name = change['name']
            if not name.strip():
                raise ValueError('A formula name must not be empty')
            if change['op'] == 'set_formula':
                formulas[name] = change['value']
            elif name in formulas:
                del formulas[name]
            else:
                raise ValueError(f'No formula named {name}')
        else:
            views = _records(base, 'views')
            name = change['name']
            if not name.strip():
                raise ValueError('A view name must not be empty')
            index = _named(views, 'name', name)
            if change['op'] == 'put_view':
                _put(views, index, 'name', name, change['fields'])
            else:
                _remove(views, index, name)
    output = StringIO()
    try:
        parser.dump(base, output)
    except (YAMLError, RecursionError) as error:
        raise ValueError('Cannot write Base YAML') from error
    return output.getvalue()
