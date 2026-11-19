import os
import re
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from hmac import compare_digest
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from .vault import FileRevision, TextFile, TrashedFile, Vault, VaultError


@contextmanager
def _tool_errors():
    try:
        yield
    except VaultError as exc:
        raise ToolError(str(exc)) from None


class RequireBearer:
    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.token = token.encode('ascii')

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scheme, _, token = Headers(scope=scope).get('authorization', '').partition(' ')
        if scheme.lower() != 'bearer' or not compare_digest(token.encode('latin-1'), self.token):
            response = Response(status_code=401, headers={'WWW-Authenticate': 'Bearer'})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def vault_server(
    name: str, vault: Vault, *, writable: bool = False, trash_dir: Path | None = None,
) -> MCPServer:
    server = MCPServer(name)

    @server.tool(name='wiki.allowed', description='List allowed paths relative to this vault.')
    def allowed() -> dict[str, list[str]]:
        return {'paths': vault.allowed}

    @server.tool(name='wiki.agentsmd', description='Read this vault’s instructions before your first edit.')
    def agentsmd() -> TextFile:
        with _tool_errors():
            return vault.read('AGENTS.md')

    @server.tool(name='wiki.read', description='Read a UTF-8 file at a vault-relative path and return its revision.')
    def read(path: str) -> TextFile:
        with _tool_errors():
            return vault.read(path)

    if writable:
        @server.tool(name='wiki.write', description=(
            'Call wiki.agentsmd before your first edit. Create a UTF-8 file if expected_revision '
            'is absent or null. To replace a file, pass its current revision from wiki.read.'
        ))
        def write(path: str, content: str, expected_revision: str | None = None) -> FileRevision:
            with _tool_errors():
                return vault.write(path, content, expected_revision)

        @server.tool(name='wiki.update', description=(
            'Call wiki.agentsmd before your first edit. Replace one exact match for old with new. '
            'Pass the current revision from wiki.read.'
        ))
        def update(path: str, old: str, new: str, expected_revision: str) -> FileRevision:
            with _tool_errors():
                return vault.update(path, old, new, expected_revision)

    if writable and trash_dir is not None:
        @server.tool(name='wiki.delete', description=(
            'Call wiki.agentsmd before your first edit. Move a UTF-8 file to the configured trash. '
            'Pass the current revision from wiki.read.'
        ))
        def delete(path: str, expected_revision: str) -> TrashedFile:
            with _tool_errors():
                return vault.delete(path, expected_revision, trash_dir)

    return server


def create_app(config: dict) -> Starlette:
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=config.get('allowed_hosts', ['localhost', 'localhost:*', '127.0.0.1', '127.0.0.1:*', '[::1]', '[::1]:*']),
        allowed_origins=config.get('allowed_origins', []),
    )
    if not isinstance(config.get('vaults'), dict) or not config['vaults']:
        raise ValueError('Configure at least one vault')
    routes = []
    servers = []
    roots = []
    trash_dirs = set()
    for name, settings in config['vaults'].items():
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
            raise ValueError('A vault ID must use ASCII letters, digits, underscores, or hyphens')
        env = settings['token_env']
        token = os.environ.get(env, '')
        if not re.fullmatch(r'[a-zA-Z0-9._~+/=-]+', token):
            raise ValueError(f'Set a valid bearer token in {env}')
        allowed = settings['allowed']
        if not isinstance(allowed, list) or not all(isinstance(path, str) for path in allowed):
            raise ValueError('Configure allowed paths as a list of strings')
        vault = Vault(Path(settings['root']), allowed)
        writable = settings.get('writable', False)
        if not isinstance(writable, bool):
            raise ValueError('Configure writable as a boolean')
        roots.append(vault.root)
        trash_dir = None
        if 'trash_dir' in settings:
            value = settings['trash_dir']
            if not writable or not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError('Configure trash_dir as an absolute directory for a writable vault')
            trash_dir = Path(value).resolve(strict=True)
            if not trash_dir.is_dir() or trash_dir in trash_dirs:
                raise ValueError('Configure a separate trash directory for each vault')
            trash_dirs.add(trash_dir)
        server = vault_server(name, vault, writable=writable, trash_dir=trash_dir)
        app = server.streamable_http_app(stateless_http=True, json_response=True, transport_security=security)
        routes.append(Mount(f'/vaults/{name}', app=RequireBearer(app, token)))
        servers.append(server)

    if any(trash.is_relative_to(root) or root.is_relative_to(trash) for trash in trash_dirs for root in roots):
        raise ValueError('The trash and vault directories must not overlap')

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with AsyncExitStack() as stack:
            for server in servers:
                await stack.enter_async_context(server.session_manager.run())
            yield

    return Starlette(routes=routes, lifespan=lifespan)
