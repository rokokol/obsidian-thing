import json
import math
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, TypedDict

from markdown_it import MarkdownIt
from mdit_py_plugins.front_matter import front_matter_plugin
from ruamel.yaml.nodes import MappingNode, Node, ScalarNode

from .frontmatter import frontmatter_nodes
from .vault import MutationResult, Vault, VaultError, _parts

DICTIONARY_PROPERTY = 'fsrs-vocabulary'
_PLUGIN = '.obsidian/plugins/fsrs-vocabulary'
_HISTORY_FILE = re.compile(r'history-(desktop|mobile)-[a-z0-9]+(?:\.[^/]*)?\.jsonl')
_CARD_ID = re.compile(r'[a-z0-9]{1,32}')
_MANAGED = re.compile(r'(?:srs|due)(?: [0-9]+)?')
_RULE = re.compile(r':?-+:?')
_PIPE = re.compile(r'(?<!\\)\|')
_MARKDOWN = MarkdownIt('commonmark').use(front_matter_plugin)
_MAX_HISTORY_BYTES = 32 * 1024 * 1024
_DEFAULT_DECAY = 0.1542


class Create(TypedDict):
    op: Literal['create']
    cells: dict[str, str]


class Update(TypedDict):
    op: Literal['update']
    row: int
    cells: dict[str, str]


class Delete(TypedDict):
    op: Literal['delete']
    row: int


Change = Create | Update | Delete


class Schedule(TypedDict):
    card_id: str | None
    state: int
    reps: int
    lapses: int
    stability: float
    difficulty: float
    scheduled_days: float
    due: str
    last_review: str | None
    recall_probability: float | None


class Card(TypedDict):
    row: int
    cells: dict[str, str]
    state: Schedule | None
    invalid_schedule: bool


class Dictionary(TypedDict):
    path: str
    revision: str
    muted: bool
    columns: list[str]
    total: int


class DictionaryPage(Dictionary):
    cards: list[Card]
    next_offset: int | None
    decay: float
    as_of: str


class DictionaryList(TypedDict):
    dictionaries: list[Dictionary]
    skipped: list[str]
    next_after: str | None


class Review(TypedDict):
    card_id: str
    timestamp_ms: int
    rating: int
    prior_state: int


class History(TypedDict):
    reviews: list[Review]
    next_offset: int | None
    files: int
    ignored_lines: int


def _cells(line: str) -> list[str]:
    value = line.strip()
    if value.startswith('|'):
        value = value[1:]
    if value.endswith('|') and not value.endswith('\\|'):
        value = value[:-1]
    return [cell.replace('\\|', '|').strip() for cell in _PIPE.split(value)]


def _encode_row(values: list[str]) -> str:
    return '| ' + ' | '.join(value.replace('|', '\\|') for value in values) + ' |'


def _get(mapping: MappingNode, key: str) -> Node | None:
    return next((value for name, value in mapping.value if isinstance(name, ScalarNode) and name.value == key), None)


def _dictionary_config(text: str) -> tuple[bool, bool]:
    _, root = frontmatter_nodes(text)
    config = _get(root, DICTIONARY_PROPERTY) if root is not None else None
    muted = _get(config, 'mute') if isinstance(config, MappingNode) else None
    return config is not None, isinstance(muted, ScalarNode) and muted.value.lower() == 'true'


@dataclass
class Table:
    columns: list[str]
    lines: list[str]
    start: int
    end: int
    rows: list[list[str]]

    @property
    def content_columns(self) -> list[str]:
        return [name for name in self.columns if not _MANAGED.fullmatch(name)]


def _table(text: str) -> Table:
    lines = text.splitlines(keepends=True)
    tokens = _MARKDOWN.parse(text.removeprefix('\ufeff'))
    headings = [token.map[1] for index, token in enumerate(tokens) if token.type == 'inline' and
                token.map and index and tokens[index - 1].type == 'heading_open' and token.level == 1
                and token.content.strip().lower() == 'words']
    if len(headings) != 1:
        raise VaultError('Expected one top-level Words heading in the dictionary')
    index = headings[0]
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index + 1 >= len(lines) or '|' not in lines[index] or '|' not in lines[index + 1]:
        raise VaultError('Expected a words table immediately after its heading')
    columns = _cells(lines[index])
    rule = _cells(lines[index + 1])
    if (not columns or not all(columns) or len(set(columns)) != len(columns) or
            len(rule) != len(columns) or not all(_RULE.fullmatch(cell) for cell in rule)):
        raise VaultError('The words table needs unique column names and a matching delimiter')
    start = index + 2
    end = start
    rows: list[list[str]] = []
    while end < len(lines) and '|' in lines[end]:
        values = _cells(lines[end])
        if len(values) > len(columns):
            raise VaultError('A dictionary row has too many cells; escape literal pipes')
        rows.append(values + [''] * (len(columns) - len(values)))
        end += 1
    table = Table(columns, lines, start, end, rows)
    if not table.content_columns:
        raise VaultError('The dictionary needs at least one content column')
    return table


def _load(vault: Vault, path: str) -> tuple[Dictionary, Table]:
    if not path.lower().endswith('.md') or path.startswith('.obsidian/'):
        raise VaultError('The path must name a Markdown dictionary')
    note = vault.read(path)
    marked, muted = _dictionary_config(note['content'])
    if not marked:
        raise VaultError('The note does not have an fsrs-vocabulary property')
    table = _table(note['content'])
    return {'path': path, 'revision': note['revision'], 'muted': muted,
            'columns': table.content_columns, 'total': len(table.rows)}, table


def _validate_page(offset: int, limit: int, maximum: int) -> None:
    if offset < 0 or not 1 <= limit <= maximum:
        raise VaultError(f'Use a nonnegative offset and a limit from 1 to {maximum}')


def list_dictionaries(vault: Vault, after: str = '', limit: int = 50) -> DictionaryList:
    _validate_page(0, limit, 100)
    dictionaries: list[Dictionary] = []
    skipped: list[str] = []
    next_after = None
    for path in vault.visible_files():
        if path <= after or not path.lower().endswith('.md'):
            continue
        try:
            note = vault.read(path)
            marked, muted = _dictionary_config(note['content'])
            if not marked:
                continue
            table = _table(note['content'])
        except VaultError:
            skipped.append(path)
            continue
        if len(dictionaries) == limit:
            next_after = dictionaries[-1]['path']
            break
        dictionaries.append({'path': path, 'revision': note['revision'], 'muted': muted,
                             'columns': table.content_columns, 'total': len(table.rows)})
    return {'dictionaries': dictionaries, 'skipped': skipped, 'next_after': next_after}


def _number(value: object) -> bool:
    return (type(value) is int and abs(value) < 10**30) or (type(value) is float and math.isfinite(value))


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def _decay(vault: Vault) -> float:
    parts = _parts(f'{_PLUGIN}/data.json', obsidian=True)
    try:
        with vault._open(parts) as fd:
            note, _ = vault._read_text(f'{_PLUGIN}/data.json', fd)
        data = json.loads(note['content'])
    except FileNotFoundError:
        return _DEFAULT_DECAY
    except (OSError, ValueError, RecursionError) as error:
        raise VaultError('Cannot read FSRS settings') from error
    if not isinstance(data, dict):
        raise VaultError('Expected FSRS settings to be an object')
    weights = data.get('fsrsWeights')
    if isinstance(weights, list) and len(weights) == 21 and all(_number(item) for item in weights):
        if 0.01 <= weights[20] <= 1:
            return float(weights[20])
        raise VaultError('The configured FSRS decay is outside the supported range')
    return _DEFAULT_DECAY


def _schedule(value: str, now: datetime, decay: float) -> Schedule | None:
    try:
        data = json.loads(value)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or any(not _number(data.get(key)) for key in ('S', 'D', 'r', 'l', 'c', 's')):
        return None
    state = data['s']
    if type(state) is not int or state not in range(4) or _timestamp(data.get('d')) is None:
        return None
    card_id = data.get('i')
    if not isinstance(card_id, str) or not _CARD_ID.fullmatch(card_id):
        card_id = None
    last = _timestamp(data.get('t'))
    probability = None
    if state == 0:
        probability = 0.0
    elif last is not None and data['S'] > 0:
        days = max(0, (now - last).days)
        factor = math.expm1(-math.log(0.9) / decay)
        probability = round((1 + factor * days / data['S']) ** -decay, 8)
    return {'card_id': card_id, 'state': state, 'reps': int(data['r']), 'lapses': int(data['l']),
            'stability': data['S'], 'difficulty': data['D'], 'scheduled_days': data['c'], 'due': data['d'],
            'last_review': data.get('t') if last is not None else None, 'recall_probability': probability}


def _srs(table: Table, row: list[str]) -> str:
    return row[table.columns.index('srs')] if 'srs' in table.columns else ''


def read_dictionary(vault: Vault, path: str, offset: int = 0, limit: int = 50) -> DictionaryPage:
    _validate_page(offset, limit, 100)
    info, table = _load(vault, path)
    now = datetime.now(timezone.utc)
    decay = _decay(vault)
    cards: list[Card] = []
    for index, row in enumerate(table.rows[offset:offset + limit], offset):
        raw = _srs(table, row)
        schedule = _schedule(raw, now, decay) if raw else None
        cells = {key: value for key, value in zip(table.columns, row, strict=True) if not _MANAGED.fullmatch(key)}
        cards.append({'row': index, 'cells': cells, 'state': schedule, 'invalid_schedule': bool(raw and schedule is None)})
    return {**info, 'cards': cards, 'next_offset': offset + limit if offset + limit < info['total'] else None,
            'decay': decay, 'as_of': now.isoformat()}


def _edit(text: str, changes: list[Change]) -> str:
    if not _dictionary_config(text)[0]:
        raise VaultError('The note does not have an fsrs-vocabulary property')
    if not changes:
        raise VaultError('Pass at least one dictionary change')
    table = _table(text)
    contents = set(table.content_columns)
    lines = table.lines[table.start:table.end]
    eol = '\r\n' if '\r\n' in text else '\n'
    for change in changes:
        if change['op'] != 'create':
            index = change['row']
            if not 0 <= index < len(table.rows):
                raise VaultError('The dictionary row does not exist')
            if change['op'] == 'delete':
                del table.rows[index]
                del lines[index]
                continue
            values = table.rows[index].copy()
        else:
            index = len(table.rows)
            values = [''] * len(table.columns)
        fields = change['cells']
        if not fields or fields.keys() - contents:
            raise VaultError('Edit only named content columns, not due or srs')
        for column, value in fields.items():
            if not value.strip(' \t\u200b\u200c\u200d\ufeff') or any(ch in value for ch in '\r\n\0'):
                raise VaultError('Each content cell must be nonempty and fit on one line')
            normalized = value.strip()
            if normalized.endswith('\\'):
                raise VaultError('A cell must not end in a backslash')
            values[table.columns.index(column)] = normalized
        if change['op'] == 'create' and contents - fields.keys():
            raise VaultError('Fill all content columns for a new card')
        line = _encode_row(values)
        if _cells(line) != values:
            raise VaultError('Cannot preserve the cells of this row')
        if change['op'] == 'create':
            table.rows.append(values)
            if lines and not lines[-1].endswith(('\n', '\r')):
                lines[-1] += eol
            lines.append(line + eol)
        elif values != table.rows[index]:
            table.rows[index] = values
            lines[index] = line + lines[index][len(lines[index].rstrip('\r\n')):]
    head = ''.join(table.lines[:table.start])
    if lines and not head.endswith(('\n', '\r')):
        head += eol
    return head + ''.join(lines) + ''.join(table.lines[table.end:])


def edit_dictionary(vault: Vault, path: str, revision: str, changes: list[Change]) -> MutationResult:
    if not path.lower().endswith('.md'):
        raise VaultError('The path must name a Markdown dictionary')
    return vault._update_with(path, revision, lambda text: _edit(text, changes))


def _history_entry(raw: bytes) -> tuple[str, int, int, int] | None:
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    card, stamp, rating, state = (data.get(key) for key in ('c', 't', 'r', 's'))
    if (not isinstance(card, str) or not _CARD_ID.fullmatch(card) or type(stamp) is not int or
            not 0 <= stamp <= 8_640_000_000_000_000 or type(rating) is not int or rating not in range(1, 5) or
            type(state) is not int or state not in range(4)):
        return None
    return card, stamp, rating, state


def history(vault: Vault, path: str, card_id: str | None = None, offset: int = 0, limit: int = 200) -> History:
    _validate_page(offset, limit, 1000)
    _, table = _load(vault, path)
    now = datetime.now(timezone.utc)
    ids = {state['card_id'] for row in table.rows if (state := _schedule(_srs(table, row), now, _DEFAULT_DECAY))}
    ids.discard(None)
    if card_id is not None:
        if card_id not in ids:
            raise VaultError('The card ID is not in this dictionary')
        ids = {card_id}
    reviews: dict[tuple[str, int, int], Review] = {}
    files = ignored = 0
    remaining = _MAX_HISTORY_BYTES
    try:
        with vault._directory(_parts(_PLUGIN, obsidian=True)) as directory:
            for name in sorted(os.listdir(directory)):
                if not _HISTORY_FILE.fullmatch(name):
                    continue
                with vault._entry(directory, name) as fd, os.fdopen(fd, 'rb', closefd=False) as file:
                    size = os.fstat(fd)
                    if not stat.S_ISREG(size.st_mode) or size.st_size > remaining:
                        raise VaultError('Cannot inspect the FSRS history within its scan limit')
                    raw = file.read(size.st_size)
                    remaining -= len(raw)
                files += 1
                for line in raw.split(b'\n')[:-1]:
                    entry = _history_entry(line)
                    if entry is None:
                        ignored += bool(line.strip())
                        continue
                    card, stamp, rating, state = entry
                    if card in ids:
                        reviews.setdefault((card, stamp, rating), {'card_id': card, 'timestamp_ms': stamp,
                                                                     'rating': rating, 'prior_state': state})
    except FileNotFoundError:
        if files:
            raise VaultError('FSRS history changed during the scan; try again') from None
    except OSError as error:
        raise VaultError('Cannot read FSRS history') from error
    ordered = sorted(reviews.values(), key=lambda row: (row['timestamp_ms'], row['card_id'], row['rating']))
    return {'reviews': ordered[offset:offset + limit], 'next_offset': offset + limit if offset + limit < len(ordered) else None,
            'files': files, 'ignored_lines': ignored}
