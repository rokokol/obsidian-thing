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

from .checker import Checker, CheckerError
from .embeddings import EmbeddingError
from .links import Resolver, iter_links
from .rules import KNOWN_CHECKS, QUOTE_KEYS, _quote_pair
from .search import Matches, SearchIndex
from .vault import MutationResult, TextFile, TrashedFile, Vault, VaultError


@contextmanager
def _tool_errors():
    try:
        yield
    except (VaultError, EmbeddingError, CheckerError) as exc:
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
    search: SearchIndex | None = None,
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

    @server.tool(name='wiki.links', description='Resolve every wikilink in a note against this vault.')
    def links(path: str) -> dict[str, list[dict[str, object]]]:
        with _tool_errors():
            note = vault.read(path)
            resolver = Resolver(vault, {path: note['content']})
            return {'links': [
                {'raw': link.raw, 'target': link.target, 'anchor': link.anchor,
                 'embed': link.embed, 'resolved': resolver.destinations(link.target, path)}
                for link in iter_links(note['content'])
            ]}

    if writable:
        @server.tool(name='wiki.write', description=(
            'Call wiki.agentsmd before your first edit. Create a UTF-8 file if expected_revision '
            'is absent or null. To replace a file, pass its current revision from wiki.read.'
        ))
        def write(path: str, content: str, expected_revision: str | None = None,
                  all_warnings: bool = False) -> MutationResult:
            with _tool_errors():
                return _with_limit(vault.write(path, content, expected_revision), all_warnings)

        @server.tool(name='wiki.update', description=(
            'Call wiki.agentsmd before your first edit. Replace one exact match for old with new. '
            'Pass the current revision from wiki.read.'
        ))
        def update(path: str, old: str, new: str, expected_revision: str,
                   all_warnings: bool = False) -> MutationResult:
            with _tool_errors():
                return _with_limit(vault.update(path, old, new, expected_revision), all_warnings)

    if writable and trash_dir is not None:
        @server.tool(name='wiki.delete', description=(
            'Call wiki.agentsmd before your first edit. Move a UTF-8 file to the configured trash. '
            'Pass the current revision from wiki.read.'
        ))
        def delete(path: str, expected_revision: str) -> TrashedFile:
            with _tool_errors():
                return vault.delete(path, expected_revision, trash_dir)

    if search is not None:
        @server.tool(name='wiki.index', description='Index one Markdown or text file. May call the configured embedding provider.')
        def index(path: str) -> dict[str, str | int]:
            with _tool_errors():
                return search.index(path)

        @server.tool(name='wiki.query', description='Search indexed files by semantic similarity to a text query.')
        def query(text: str, limit: int = 10) -> Matches:
            with _tool_errors():
                return search.query(text, limit)

        @server.tool(name='wiki.related', description='Find indexed neighbors of a file. Index its current revision if needed.')
        def related(path: str, limit: int = 10, min_score: float = 0.0) -> Matches:
            with _tool_errors():
                return search.related(path, limit, min_score)

    return server


def _with_limit(result: MutationResult, show_all: bool) -> MutationResult:
    if 'warnings' in result:
        result = {**result, 'warnings': _limited(list(result['warnings']), show_all)}
    return result


def _checks(settings: object, writable: bool) -> dict[str, object]:
    if settings is None:
        return {}
    if not isinstance(settings, dict) or set(settings) - set(KNOWN_CHECKS):
        raise ValueError(f'Configure checks as a table of {", ".join(KNOWN_CHECKS)}')
    for name, value in settings.items():
        if name in QUOTE_KEYS:
            _quote_pair(value, name)
        elif not isinstance(value, bool):
            raise ValueError(f'Configure {name} as a boolean')
    if 'inner_quoter' in settings and 'default_quotes' not in settings:
        raise ValueError('Configure default_quotes before inner_quoter')
    if settings and not writable:
        raise ValueError('Configure checks only for a writable vault')
    return dict(settings)


def _limited(items: list[str], show_all: bool) -> list[str]:
    limit = 10
    if show_all or len(items) <= limit:
        return items
    return [*items[:limit], f'{len(items) - limit} more warnings; pass all_warnings=true to see them']


def _overlap(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def _state_dir(settings: dict) -> Path:
    value = settings.get('state_dir')
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('Configure an absolute embedding state_dir')
    state = Path(value).resolve(strict=True)
    info = state.stat()
    if not state.is_dir() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('The embedding state directory must be private and owned by the server user')
    return state


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
    state_dirs: list[Path] = []
    checker_scripts: set[Path] = set()
    mounted = []
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
        writable = settings.get('writable', False)
        if not isinstance(writable, bool):
            raise ValueError('Configure writable as a boolean')
        checker = None
        if 'checker' in settings:
            if not writable:
                raise ValueError('Configure a checker only for a writable vault')
            checker = Checker(settings['checker'])
            checker_scripts.add(checker.script)
        protect_frontmatter = settings.get('protect_frontmatter', False)
        if not isinstance(protect_frontmatter, bool) or (protect_frontmatter and not writable):
            raise ValueError('Configure protect_frontmatter as a boolean for a writable vault')
        checks = _checks(settings.get('checks'), writable)
        vault = Vault(Path(settings['root']), allowed, checker=checker, protect_frontmatter=protect_frontmatter,
                      checks=checks)
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
        embeddings = settings.get('embeddings')
        state = None
        if 'embeddings' in settings:
            if not isinstance(embeddings, dict):
                raise ValueError('Configure embeddings as a table')
            state = _state_dir(embeddings)
            if any(_overlap(state, other) for other in state_dirs):
                raise ValueError('Configure separate embedding state directories for each vault')
            state_dirs.append(state)
        mounted.append((name, token, vault, writable, trash_dir, embeddings, state))

    if any(_overlap(trash, root) for trash in trash_dirs for root in roots):
        raise ValueError('The trash and vault directories must not overlap')
    if any(_overlap(state, other) for state in state_dirs for other in (*roots, *trash_dirs)):
        raise ValueError('The embedding state directory must not overlap vaults or trash')

    if any(script.is_relative_to(directory) for script in checker_scripts
           for directory in (*roots, *trash_dirs, *state_dirs)):
        raise ValueError('Keep checker scripts outside vaults, trash and embedding state')

    for name, token, vault, writable, trash_dir, embeddings, state in mounted:
        search = SearchIndex(vault, embeddings, state) if state is not None else None
        server = vault_server(name, vault, writable=writable, trash_dir=trash_dir, search=search)
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
