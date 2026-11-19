import os
import re
from contextlib import AsyncExitStack, asynccontextmanager
from hmac import compare_digest
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from .vault import TextFile, Vault


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


def vault_server(name: str, vault: Vault) -> MCPServer:
    server = MCPServer(name)

    @server.tool(name='wiki.allowed', description='List allowed paths relative to this vault.')
    def allowed() -> dict[str, list[str]]:
        return {'paths': vault.allowed}

    @server.tool(name='wiki.agentsmd', description='Read this vault’s instructions before your first edit.')
    def agentsmd() -> TextFile:
        return vault.read('AGENTS.md')

    @server.tool(name='wiki.read', description='Read a UTF-8 file at a vault-relative path and return its revision.')
    def read(path: str) -> TextFile:
        return vault.read(path)

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
        server = vault_server(name, vault)
        app = server.streamable_http_app(stateless_http=True, json_response=True, transport_security=security)
        routes.append(Mount(f'/vaults/{name}', app=RequireBearer(app, token)))
        servers.append(server)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with AsyncExitStack() as stack:
            for server in servers:
                await stack.enter_async_context(server.session_manager.run())
            yield

    return Starlette(routes=routes, lifespan=lifespan)
