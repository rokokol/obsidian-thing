import json

from yaml.nodes import ScalarNode, SequenceNode

from .checker import CheckerError
from .frontmatter import frontmatter_nodes
from .links import Resolver, iter_links

MAX_PROPERTY_CHARS = 1_048_576
MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_CONTEXT_BYTES = 4 * 1024 * 1024
MAX_PROPERTY_LINKS = 200


def _note(resolver: Resolver, path: str, text: str, keys: frozenset[str]) -> dict[str, object]:
    state, mapping = frontmatter_nodes(text)
    properties: dict[str, object] = {}
    links: dict[str, list[dict[str, object]]] = {}
    remaining = MAX_PROPERTY_LINKS
    chars = 0
    if mapping is not None:
        for key, node in mapping.value:
            if not isinstance(key, ScalarNode) or key.value not in keys:
                continue
            values: list[str]
            if isinstance(node, ScalarNode):
                values = [node.value]
                properties[key.value] = node.value
            elif isinstance(node, SequenceNode) and all(isinstance(v, ScalarNode) for v in node.value):
                values = [v.value for v in node.value if isinstance(v, ScalarNode)]
                properties[key.value] = values
            else:
                properties[key.value] = None
                continue
            chars += sum(len(value) + 1 for value in values)
            if chars > MAX_PROPERTY_CHARS:
                raise CheckerError('A note exceeds the write checker property size limit')
            for link in iter_links('\n'.join(values)):
                remaining -= 1
                if remaining < 0:
                    raise CheckerError('A note exceeds the write checker property link limit')
                links.setdefault(key.value, []).append({
                    'raw': link.raw, 'target': link.target, 'anchor': link.anchor,
                    'resolved': resolver.destinations(link.target, path)[:5],
                })
    return {'frontmatter': state.name.lower(), 'properties': properties, 'property_links': links}


def build_context(resolver: Resolver, path: str, before: str | None, after: str,
                  properties: frozenset[str]) -> dict[str, object]:
    from .vault import VaultError

    files = resolver.files()
    previous = Resolver(resolver.vault, {path: before}) if before is not None else None
    notes: dict[str, object] = {}
    context: dict[str, object] = {
        'property_names': sorted(properties),
        'files': files,
        'notes': notes,
        'before': _note(resolver, path, before, properties) if before is not None else None,
        'after': _note(resolver, path, after, properties),
        'links_before': previous.context(path, before) if previous is not None and before is not None else [],
    }
    size = len(json.dumps(context))
    if size > MAX_CONTEXT_BYTES:
        raise CheckerError('The write checker vault context is too large')
    read_bytes = 0
    for candidate in files:
        if candidate == path or not candidate.lower().endswith('.md'):
            continue
        try:
            text = resolver.read(candidate)
        except VaultError:
            metadata: dict[str, object] = {
                'frontmatter': 'unreadable', 'properties': {}, 'property_links': {},
            }
        else:
            read_bytes += len(text.encode('utf-8'))
            if read_bytes > MAX_SCAN_BYTES:
                raise CheckerError('The write checker vault context exceeds the scan limit')
            metadata = _note(resolver, candidate, text, properties)
        size += len(json.dumps(candidate)) + len(json.dumps(metadata)) + 4
        if size > MAX_CONTEXT_BYTES:
            raise CheckerError('The write checker vault context is too large')
        notes[candidate] = metadata
    return context
