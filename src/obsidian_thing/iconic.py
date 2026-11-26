import json
from typing import NotRequired, TypedDict

from .vault import Vault, VaultError

SETTINGS = '.obsidian/plugins/iconic/data.json'


class IconChanges(TypedDict):
    icon: NotRequired[str | None]
    color: NotRequired[str | None]


class IconResult(TypedDict):
    path: str
    icon: str | None
    color: str | None


def set_icon(vault: Vault, path: str, changes: IconChanges) -> IconResult:
    if not changes or set(changes) - {'icon', 'color'}:
        raise VaultError('Pass icon, color, or both')
    for value in changes.values():
        if value is not None and (not isinstance(value, str) or not value.strip() or
                                  len(value) > 256 or any(ord(c) < 32 for c in value)):
            raise VaultError('Use a non-empty icon or color, or null to clear it')
    result: IconResult = {'path': path, 'icon': None, 'color': None}

    def transform(content: str) -> str:
        vault.file_size(path)
        data = json.loads(content)
        if not isinstance(data, dict):
            raise VaultError('Expected an Iconic settings object')
        icons = data.setdefault('fileIcons', {})
        if not isinstance(icons, dict):
            raise VaultError('Expected an Iconic fileIcons object')
        entry = icons.get(path, {})
        if not isinstance(entry, dict):
            raise VaultError('Invalid Iconic entry')
        for field in ('icon', 'color'):
            if field in changes:
                if (value := changes.get(field)) is None:
                    entry.pop(field, None)
                else:
                    entry[field] = value
        if entry.get('icon') or entry.get('color'):
            icons[path] = entry
        else:
            icons.pop(path, None)
        result.update(icon=entry.get('icon'), color=entry.get('color'))
        return json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n'

    vault._edit_plugin_settings(SETTINGS, transform)
    return result
