import argparse
import tomllib
from pathlib import Path

import uvicorn

from .server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description='Serve one MCP endpoint per Obsidian vault.')
    parser.add_argument('--config', required=True, type=Path, help='Path to the TOML configuration')
    parser.add_argument('--host', default='127.0.0.1', help='Listen address (default: %(default)s)')
    parser.add_argument('--port', type=int, default=8765, help='Listen port (default: %(default)s)')
    args = parser.parse_args()
    try:
        with args.config.open('rb') as config_file:
            app = create_app(tomllib.load(config_file))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    uvicorn.run(app, host=args.host, port=args.port, ws='none')


if __name__ == '__main__':
    main()
