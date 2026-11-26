import re
from enum import Enum, auto

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.nodes import MappingNode, ScalarNode, SequenceNode


class FrontmatterState(Enum):
    ABSENT = auto()
    VALID = auto()
    INVALID = auto()


_DELIMITER = re.compile(r'^---[ \t]*\r?$', re.MULTILINE)


def frontmatter_state(markdown: str) -> FrontmatterState:
    return frontmatter_nodes(markdown)[0]


def frontmatter_span(text: str) -> tuple[FrontmatterState, tuple[int, int, int] | None]:
    bom = int(text.startswith('\ufeff'))
    opening = _DELIMITER.match(text[bom:])
    if opening is None:
        return FrontmatterState.ABSENT, None
    start = opening.end() + bom
    closing = _DELIMITER.search(text, start + 1)
    if closing is None:
        return FrontmatterState.INVALID, None
    return FrontmatterState.VALID, (start, closing.start(), closing.end())


def frontmatter_nodes(markdown: str) -> tuple[FrontmatterState, MappingNode | None]:
    state, span = frontmatter_span(markdown)
    if span is None:
        return state, None
    try:
        # Property names are text. Read the node tree without constructing YAML values.
        root = YAML(typ='base').compose(markdown[span[0]:span[1]])
    except (YAMLError, RecursionError):
        return FrontmatterState.INVALID, None
    if root is None:
        return FrontmatterState.VALID, None
    if not isinstance(root, MappingNode):
        return FrontmatterState.INVALID, None
    pending = [root]
    seen = set()
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        if isinstance(node, MappingNode):
            keys: set[str] = set()
            for key, value in node.value:
                if not isinstance(key, ScalarNode) or key.value in keys:
                    return FrontmatterState.INVALID, None
                keys.add(key.value)
                pending.append(value)
        elif isinstance(node, SequenceNode):
            pending.extend(node.value)
    return FrontmatterState.VALID, root
