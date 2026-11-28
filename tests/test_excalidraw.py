import asyncio
import io
import json
from base64 import b64decode

import pytest
from lzstring import LZString
from mcp.types import CallToolResult, ImageContent
from PIL import Image

from obsidian_thing.excalidraw import render_drawing
from obsidian_thing.server import vault_server
from obsidian_thing.vault import Vault, VaultError


def scene():
    return {'type': 'excalidraw', 'version': 2, 'elements': [
        {'type': 'rectangle', 'id': 'a', 'x': 0, 'y': 0, 'width': 100, 'height': 60,
         'strokeWidth': 2, 'strokeColor': '#222222', 'backgroundColor': '#ff0000',
         'fillStyle': 'solid', 'opacity': 100},
    ], 'appState': {}, 'files': {}}


def test_render_plain_and_compressed_with_custom_name(tmp_path):
    data = json.dumps(scene())
    (tmp_path / 'raw.excalidraw').write_text(data)
    compressed = LZString().compressToBase64(data)
    (tmp_path / 'renamed.md').write_text(
        f'---\nexcalidraw-plugin: parsed\n---\n\n## Drawing\n```compressed-json\n{compressed}\n```\n')
    vault = Vault(tmp_path, ['.'])
    png = render_drawing(vault, 'renamed.md', 240)
    assert png == render_drawing(vault, 'raw.excalidraw', 240)
    with Image.open(io.BytesIO(png)) as image:
        assert image.format == 'PNG'
        assert image.width == 240
        assert image.convert('RGB').getpixel((120, image.height // 2)) == (255, 0, 0)


def test_reject_unallowed_path_and_external_embed(tmp_path):
    (tmp_path / 'private.excalidraw').write_text(json.dumps(scene()))
    (tmp_path / 'visible.md').write_text(
        f'---\nexcalidraw-plugin: parsed\n---\n\n## Embedded Files\nabc: [[private.png]]\n'
        f'\n## Drawing\n```json\n{json.dumps(scene())}\n```\n')
    vault = Vault(tmp_path, ['visible.md'])
    with pytest.raises(VaultError, match='not allowed'):
        render_drawing(vault, 'private.excalidraw', 240)
    with pytest.raises(VaultError, match='embedded files'):
        render_drawing(vault, 'visible.md', 240)


def test_render_tool_is_opt_in_and_returns_png(tmp_path):
    (tmp_path / 'drawing.excalidraw').write_text(json.dumps(scene()))
    vault = Vault(tmp_path, ['.'])
    name = 'obsidian.excalidraw_render'

    async def check():
        default = vault_server('test', vault)
        enabled = vault_server('test', vault, excalidraw=True)
        assert name not in {tool.name for tool in await default.list_tools()}
        assert name in {tool.name for tool in await enabled.list_tools()}
        result = await enabled.call_tool(name, {'path': 'drawing.excalidraw', 'width': 240})
        assert isinstance(result, CallToolResult) and not result.is_error
        image, = result.content
        assert isinstance(image, ImageContent)
        assert image.mime_type == 'image/png'
        with Image.open(io.BytesIO(b64decode(image.data))) as png:
            assert png.width == 240

    asyncio.run(check())
