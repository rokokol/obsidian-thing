import re
from collections.abc import Sequence
from typing import NotRequired, TypedDict

from ruamel.yaml.nodes import MappingNode, ScalarNode

from .frontmatter import FrontmatterState, frontmatter_nodes
from .vault import MutationResult, TextFile, Vault, VaultError

DEFAULT_INSTRUCTIONS = ('AGENTS.md', 'GEMINI.md', 'CLAUDE.md')
DEFAULT_SKILLS_DIR = '.agents/skills'
DEFAULT_MEMORY_DIR = '.agents/memory'
MEMORY_INDEX = 'MEMORY.md'

_INDEX_LINE = re.compile(r'^- \[(?P<title>[^\]]*)\]\((?P<target>[^)]*)\)(?: — .*)?$')


class Skill(TypedDict):
    name: str
    description: str
    path: str


class Skills(TypedDict):
    skills: list[Skill]


class MemoryItem(TypedDict):
    name: str
    description: str | None
    type: str | None
    path: str
    revision: str


class Memories(TypedDict):
    memories: list[MemoryItem]


class MemoryWrite(MutationResult):
    index_revision: NotRequired[str]


class MemoryDelete(MutationResult):
    trash: str
    index_revision: NotRequired[str]


def _fields(text: str) -> dict[str, object]:
    state, root = frontmatter_nodes(text)
    if state is not FrontmatterState.VALID or root is None:
        return {}
    values: dict[str, object] = {}
    for key, node in root.value:
        if isinstance(node, ScalarNode):
            values[key.value] = node.value
        elif isinstance(node, MappingNode):
            values[key.value] = {inner.value: value.value for inner, value in node.value
                                 if isinstance(value, ScalarNode)}
    return values


def _text(fields: dict[str, object], key: str) -> str | None:
    value = fields.get(key)
    if not isinstance(value, str):
        return None
    return value.strip() or None


def read_instructions(vault: Vault, names: Sequence[str]) -> TextFile:
    for name in names:
        try:
            return vault.read(name)
        except VaultError:
            continue
    raise VaultError('No instruction file is readable; looked for ' + ', '.join(names))


def list_skills(vault: Vault, directory: str) -> Skills:
    skills: list[Skill] = []
    for name, is_dir, _ in vault.agent_entries(directory):
        if not is_dir:
            continue
        path = f'{directory}/{name}/SKILL.md'
        try:
            note = vault.agent_read(path)
        except VaultError:
            continue
        fields = _fields(note['content'])
        skills.append({'name': str(fields.get('name') or name),
                       'description': _text(fields, 'description') or '',
                       'path': path})
    return {'skills': sorted(skills, key=lambda skill: skill['name'])}


def _skill_dir(vault: Vault, directory: str, name: str) -> str:
    if (not name or '/' in name or '\\' in name or '\0' in name or name.startswith('.')
            or '..' in name):
        raise VaultError('Pass a skill name, not a path')
    entries = vault.agent_entries(directory)
    for entry, is_dir, _ in entries:
        if is_dir and entry == name:
            return f'{directory}/{entry}'
    for entry, is_dir, _ in entries:
        if not is_dir:
            continue
        try:
            note = vault.agent_read(f'{directory}/{entry}/SKILL.md')
        except VaultError:
            continue
        if _fields(note['content']).get('name') == name:
            return f'{directory}/{entry}'
    raise VaultError(f'Skill not found: {name}')


def read_skill(vault: Vault, directory: str, name: str, path: str = 'SKILL.md') -> TextFile:
    return vault.agent_read(f'{_skill_dir(vault, directory, name)}/{path or "SKILL.md"}')


def _memory_file(name: str, *, index: bool = False) -> str:
    stem = name[:-3] if name.endswith('.md') else name
    if (not stem or '/' in name or '\\' in name or '\0' in name or stem.startswith('.') or '..' in stem
            or (stem == MEMORY_INDEX[:-3] and not index)):
        raise VaultError('Pass a memory name, not a path')
    return f'{stem}.md'


def list_memory(vault: Vault, directory: str) -> Memories:
    memories: list[MemoryItem] = []
    for name, _, is_file in vault.agent_entries(directory):
        if not is_file or not name.endswith('.md') or name == MEMORY_INDEX:
            continue
        path = f'{directory}/{name}'
        note = vault.agent_read(path)
        fields = _fields(note['content'])
        metadata = fields.get('metadata')
        kind = metadata.get('type') if isinstance(metadata, dict) else None
        memories.append({'name': str(fields.get('name') or name[:-3]),
                         'description': _text(fields, 'description'),
                         'type': kind if isinstance(kind, str) else None,
                         'path': path,
                         'revision': note['revision']})
    return {'memories': memories}


def read_memory(vault: Vault, directory: str, name: str) -> TextFile:
    return vault.agent_read(f'{directory}/{_memory_file(name, index=True)}')


def _index_text(text: str | None, filename: str, name: str, description: str) -> str:
    summary = ' '.join(description.split())
    title = ' '.join(name.split()) or filename[:-3]
    line = f'- [{title}]({filename}) — {summary}'
    if text is None:
        return f'# Memory index\n\n{line}\n'
    lines = text.splitlines()
    for position, existing in enumerate(lines):
        match = _INDEX_LINE.match(existing)
        if match and match['target'] == filename:
            lines[position] = f'- [{match["title"] or title}]({filename}) — {summary}'
            return '\n'.join(lines) + '\n'
    while lines and not lines[-1].strip():
        lines.pop()
    lines.extend(('', line))
    return '\n'.join(lines) + '\n'


def _read_index(vault: Vault, path: str) -> TextFile | None:
    try:
        return vault.agent_read(path)
    except VaultError:
        return None


def _entries(text: str) -> list[tuple[str, str]]:
    found = []
    for line in text.splitlines():
        match = _INDEX_LINE.match(line)
        if match:
            found.append((match['title'], match['target']))
    return found


def _drop_line(text: str, filename: str) -> str | None:
    lines = text.splitlines()
    kept = []
    for line in lines:
        match = _INDEX_LINE.match(line)
        if match is not None and match['target'] == filename:
            continue
        kept.append(line)
    if len(kept) == len(lines):
        return None
    while kept and not kept[-1].strip():
        kept.pop()
    return '\n'.join(kept) + '\n'


def _memory_names(vault: Vault, directory: str) -> list[str]:
    return [name for name, _, is_file in vault.agent_entries(directory)
            if is_file and name.endswith('.md') and name != MEMORY_INDEX]


def delete_memory(vault: Vault, directory: str, name: str,
                  expected_revision: str) -> MemoryDelete:
    filename = _memory_file(name)
    result = vault.agent_delete(f'{directory}/{filename}', expected_revision)
    index_path = f'{directory}/{MEMORY_INDEX}'
    index = _read_index(vault, index_path)
    if index is not None:
        text = _drop_line(index['content'], filename)
        if text is not None:
            written = vault.agent_write(index_path, text, index['revision'])
            return {'path': result['path'], 'revision': result['revision'], 'trash': result['trash'],
                    'index_revision': written['revision']}
    return {'path': result['path'], 'revision': result['revision'], 'trash': result['trash']}


def write_memory_index(vault: Vault, directory: str, content: str,
                       expected_revision: str | None = None) -> MutationResult:
    entries = _entries(content)
    targets = [target for _, target in entries]
    names = _memory_names(vault, directory)
    duplicates = sorted({target for target in targets if targets.count(target) > 1})
    if duplicates:
        raise VaultError('These notes appear more than once in the index: ' + ', '.join(duplicates))
    unknown = sorted(set(targets) - set(names))
    if unknown:
        raise VaultError('These entries name no memory note: ' + ', '.join(unknown))
    missing = sorted(set(names) - set(targets))
    if missing:
        raise VaultError('These notes have no line in the index: ' + ', '.join(missing))
    return vault.agent_write(f'{directory}/{MEMORY_INDEX}', content, expected_revision)


def write_memory(vault: Vault, directory: str, name: str, content: str,
                 expected_revision: str | None = None) -> MemoryWrite:
    filename = _memory_file(name)
    fields = _fields(content)
    description = _text(fields, 'description')
    if description is None:
        raise VaultError('The memory note needs YAML frontmatter with a description')
    vault.ensure_agent_dir(directory)
    result = vault.agent_write(f'{directory}/{filename}', content, expected_revision)
    index_path = f'{directory}/{MEMORY_INDEX}'
    index = _read_index(vault, index_path)
    text = _index_text(index['content'] if index is not None else None, filename,
                       str(fields.get('name') or filename[:-3]), description)
    try:
        written = vault.agent_write(index_path, text, index['revision'] if index is not None else None)
    except VaultError as error:
        raise VaultError(f'The note is saved, but its line in {MEMORY_INDEX} is not: {error}') from None
    return {**result, 'index_revision': written['revision']}
