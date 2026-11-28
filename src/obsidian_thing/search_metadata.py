from unicodedata import normalize

from ruamel.yaml.nodes import ScalarNode, SequenceNode

from .frontmatter import frontmatter_nodes
from .rules import iter_inline_tags


def fold(value: str) -> str:
    return normalize('NFC', normalize('NFC', value).casefold())


def tag_key(value: str) -> str:
    return fold(value.strip().removeprefix('#').rstrip('/'))


def metadata_rows(text: str) -> set[tuple[str, str, str]]:
    rows = {('revision-marker', '', '')}
    rows.update(('tag', '', tag_key(tag)) for tag, _ in iter_inline_tags(text))
    _, root = frontmatter_nodes(text)
    if root is None:
        return rows
    for key, node in root.value:
        name = key.value
        rows.add(('present', name, ''))
        for item in node.value if isinstance(node, SequenceNode) else (node,):
            if not isinstance(item, ScalarNode):
                continue
            rows.add(('value', name, item.value))
            rows.add(('folded', name, fold(item.value)))
            if name == 'tags' and (tag := tag_key(item.value)):
                rows.add(('tag', '', tag))
    return rows
