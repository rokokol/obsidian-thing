import re
from enum import Enum, auto

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode


class FrontmatterState(Enum):
    ABSENT = auto()
    VALID = auto()
    INVALID = auto()


_DELIMITER = re.compile(r'^---[ \t]*\r?$', re.MULTILINE)


def frontmatter_state(markdown: str) -> FrontmatterState:
    text = markdown.removeprefix('\ufeff')
    opening = _DELIMITER.match(text)
    if opening is None:
        return FrontmatterState.ABSENT
    closing = _DELIMITER.search(text, opening.end() + 1)
    if closing is None:
        return FrontmatterState.INVALID
    try:
        # Property names are text. Read the node tree without constructing YAML values.
        root = yaml.compose(text[opening.end():closing.start()], Loader=yaml.BaseLoader)
    except (yaml.YAMLError, RecursionError):
        return FrontmatterState.INVALID
    if root is None:
        return FrontmatterState.VALID
    if not isinstance(root, MappingNode):
        return FrontmatterState.INVALID
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
                    return FrontmatterState.INVALID
                keys.add(key.value)
                pending.append(value)
        elif isinstance(node, SequenceNode):
            pending.extend(node.value)
    return FrontmatterState.VALID
