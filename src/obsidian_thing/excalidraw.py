import json
import math
import os
import re
import resource
import subprocess
import sys
from pathlib import PurePosixPath

from ruamel.yaml.nodes import MappingNode, ScalarNode

from .frontmatter import frontmatter_nodes
from .vault import Vault, VaultError

DEFAULT_WIDTH = 1600
MAX_WIDTH = 8192
_MAX_PIXELS = 16_000_000
_MAX_SOURCE = 4 * 1024 * 1024
_MAX_SCENE = 32 * 1024 * 1024
_TIMEOUT = 20
_DRAWING = re.compile(r'^#{1,2} Drawing[ \t]*\n```(json|compressed-json)[ \t]*\n(.*?)^```[ \t]*$',
                      re.MULTILINE | re.DOTALL)
_EMBEDS = re.compile(r'^## Embedded Files[ \t]*$', re.MULTILINE)
_VIEW_BOX = re.compile(r'^<svg [^>]* viewBox="[^ ]+ [^ ]+ ([^ ]+) ([^" ]+)"')
_TYPES = frozenset({'rectangle', 'ellipse', 'diamond', 'arrow', 'line', 'text', 'freedraw', 'frame'})


def has_drawing_marker(root: MappingNode) -> bool:
    return any(
        key.value == 'excalidraw-plugin' and isinstance(value, ScalarNode) and value.value in {'parsed', 'raw'}
        for key, value in root.value)


def render_drawing(vault: Vault, path: str, width: int = DEFAULT_WIDTH) -> bytes:
    if type(width) is not int or not 1 <= width <= MAX_WIDTH:
        raise VaultError(f'The render width must be between 1 and {MAX_WIDTH} pixels')
    suffix = PurePosixPath(path).suffix.lower()
    if suffix not in {'.md', '.excalidraw'}:
        raise VaultError('The path must name a Markdown or Excalidraw file')
    _, data = vault.read_attachment(path, _MAX_SOURCE)
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, sys.path)))
    try:
        result = subprocess.run([sys.executable, '-m', __name__, suffix, str(width)], input=data,
                                capture_output=True, timeout=_TIMEOUT, check=False, env=environment)
    except subprocess.TimeoutExpired:
        raise VaultError('Excalidraw render timed out; use a smaller drawing') from None
    except OSError:
        raise VaultError('Cannot start the Excalidraw renderer') from None
    if result.returncode == 2:
        raise VaultError(result.stderr.decode('utf-8', errors='replace').strip())
    if result.returncode or not result.stdout.startswith(b'\x89PNG\r\n\x1a\n'):
        raise VaultError('Cannot render the Excalidraw drawing')
    return result.stdout


def _scene(source: bytes, suffix: str) -> dict:
    from lzstring import LZString

    text = source.decode('utf-8-sig').replace('\r\n', '\n')
    if suffix == '.md':
        _, root = frontmatter_nodes(text)
        if root is None or not has_drawing_marker(root):
            raise VaultError('The Markdown file is not an Excalidraw drawing')
        if _EMBEDS.search(text):
            raise VaultError('Excalidraw embedded files are not supported')
        drawing = _DRAWING.search(text)
        if drawing is None:
            raise VaultError('Cannot find the Excalidraw drawing block')
        encoding, text = drawing.groups()
        if encoding == 'compressed-json':
            text = LZString().decompressFromBase64(''.join(text.split()))
            if not text:
                raise VaultError('Cannot decompress the Excalidraw drawing')
            text = text.encode('utf-16-le', errors='surrogatepass').decode('utf-16-le')
    if len(text) > _MAX_SCENE:
        raise VaultError('The Excalidraw scene exceeds the render limit')
    scene = json.loads(text)
    if not isinstance(scene, dict) or scene.get('type') != 'excalidraw' or not isinstance(scene.get('elements'), list):
        raise VaultError('The file does not contain an Excalidraw scene')
    elements = [element for element in scene['elements'] if not element.get('isDeleted')]
    if any(element.get('type') == 'image' for element in elements):
        raise VaultError('Excalidraw embedded files are not supported')
    if any(element.get('type') not in _TYPES for element in elements):
        raise VaultError('The Excalidraw scene has an unsupported element type')
    return {**scene, 'elements': elements, 'files': {}}


def _deny_resource(_url: str, _resource_type: str) -> bytes:
    raise VaultError('Excalidraw external resources are not supported')


def _render(source: bytes, suffix: str, width: int) -> bytes:
    from cairosvg.surface import PNGSurface
    from excalidraw_render.element import parse_scene
    from excalidraw_render.render import render_svg

    svg = render_svg(parse_scene(_scene(source, suffix)))
    bounds = _VIEW_BOX.match(svg)
    if bounds is None:
        raise VaultError('Cannot determine the Excalidraw canvas size')
    w, h = map(float, bounds.groups())
    if not (math.isfinite(w) and math.isfinite(h) and w > 0 and h > 0):
        raise VaultError('The Excalidraw canvas size is invalid')
    height = max(1, round(width * h / w))
    if height > MAX_WIDTH or width * height > _MAX_PIXELS:
        raise VaultError('The Excalidraw image is too large; use a smaller width')
    png = PNGSurface.convert(bytestring=svg.encode('utf-8'), output_width=width, output_height=height,
                             url_fetcher=_deny_resource)
    if not isinstance(png, bytes):
        raise VaultError('Cannot encode the Excalidraw PNG')
    return png


def _main() -> None:
    if sys.platform == 'linux':
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = 512 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit if hard == resource.RLIM_INFINITY else min(hard, limit), hard))
    try:
        sys.stdout.buffer.write(_render(sys.stdin.buffer.read(_MAX_SOURCE + 1), sys.argv[1], int(sys.argv[2])))
    except VaultError as error:
        print(error, file=sys.stderr)
        raise SystemExit(2) from None
    except ImportError:
        print('Install the Excalidraw renderer and Cairo', file=sys.stderr)
        raise SystemExit(2) from None
    except (AttributeError, KeyError, MemoryError, OSError, RecursionError, TypeError, ValueError):
        print('Cannot render the Excalidraw scene; it is invalid or exceeds the render limits', file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == '__main__':
    _main()
