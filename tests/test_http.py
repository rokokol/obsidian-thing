import asyncio
import os
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from hashlib import sha256

import httpx2 as httpx
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from obsidian_thing.server import create_app


def wait_for_http(url):
    deadline = time.monotonic() + 10
    with httpx.Client(timeout=0.3, trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                if client.get(f'{url}/first/mcp').status_code == 401:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.05)
    pytest.fail('MCP endpoint did not return HTTP 401 before the startup deadline')


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=30)
    if result.returncode:
        pytest.fail(f'docker {args[0]} failed: {result.stderr.strip()}')
    return result.stdout.strip()


@contextmanager
def docker_endpoint(image, directory, vaults):
    config = directory / 'config.toml'
    config.write_text('\n'.join(
        f'[vaults.{name}]\nroot = "/fixtures/{name}"\n'
        f'allowed = ["notes", "AGENTS.md"]\ntoken_env = "{settings["token_env"]}"\n'
        for name, settings in vaults.items()
    ))
    env = [arg for settings in vaults.values() for arg in ('--env', settings['token_env'])]
    container = docker(
        'run', '--detach', '--pull=never', '--read-only', '--user', f'{os.getuid()}:{os.getgid()}',
        '--volume', f'{directory}:/fixtures:ro', '--publish', '127.0.0.1::8765', *env,
        image, '--config', '/fixtures/config.toml', '--host', '0.0.0.0',
    )
    try:
        url = f'http://{docker("port", container, "8765/tcp")}/vaults'
        wait_for_http(url)
        yield url
    finally:
        try:
            print(docker('logs', container))
        finally:
            docker('rm', '--force', container)


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    vaults = {}
    for name in ('first', 'second'):
        root = tmp_path / name
        root.mkdir()
        (root / 'notes').mkdir()
        (root / 'notes' / 'same.md').write_text(name)
        (root / 'AGENTS.md').write_text(f'Rules for {name}')
        (root / 'private.txt').write_text('private')
        (root / 'notes' / 'link.md').symlink_to('../private.txt')
        var = f'TEST_TOKEN_{name.upper()}'
        monkeypatch.setenv(var, name)
        vaults[name] = {'root': str(root), 'allowed': ['notes', 'AGENTS.md'], 'token_env': var}
    if image := os.environ.get('OBSIDIAN_MCP_TEST_IMAGE'):
        with docker_endpoint(image, tmp_path, vaults) as url:
            yield url, tmp_path
        return
    app = create_app({'vaults': vaults})
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        url = f'http://127.0.0.1:{sock.getsockname()[1]}/vaults'
        server = uvicorn.Server(uvicorn.Config(app, lifespan='on', log_level='warning'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            wait_for_http(url)
            yield url, tmp_path
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_auth_is_per_vault(endpoint):
    url, _ = endpoint
    for headers in ({}, {'Authorization': 'Bearer first'}):
        response = httpx.post(f'{url}/second/mcp', headers=headers)
        assert response.status_code == 401
    response = httpx.post(f'{url}/first/mcp', headers={'Authorization': 'Bearer first', 'Origin': 'https://invalid.example'}, json={})
    assert response.status_code == 403


@pytest.mark.parametrize(('mode', 'version'), [('legacy', '2025-11-25'), ('2026-07-28', '2026-07-28')])
def test_tools_read_live_files_in_the_selected_vault(endpoint, mode, version):
    url, directory = endpoint

    async def check():
        for name in ('first', 'second'):
            async with httpx.AsyncClient(headers={'Authorization': f'Bearer {name}'}) as http:
                transport = streamable_http_client(f'{url}/{name}/mcp', http_client=http)
                async with Client(transport, mode=mode) as session:
                    assert session.protocol_version == version
                    tools = await session.list_tools()
                    assert {tool.name for tool in tools.tools} == {'wiki.allowed', 'wiki.agentsmd', 'wiki.read'}
                    allowed = await session.call_tool('wiki.allowed')
                    assert set(allowed.structured_content['paths']) == {'notes', 'AGENTS.md'}
                    note = await session.call_tool('wiki.read', {'path': 'notes/same.md'})
                    assert note.structured_content['content'] == name
                    assert note.structured_content['revision'] == sha256(name.encode()).hexdigest()
                    (directory / name / 'notes' / 'same.md').write_text(f'{name} changed')
                    recent = await session.call_tool('wiki.read', {'path': 'notes/same.md'})
                    assert recent.structured_content['content'] == f'{name} changed'
                    rules = await session.call_tool('wiki.agentsmd')
                    assert rules.structured_content['content'] == f'Rules for {name}'
                    for path in ('private.txt', 'notes-backup/file.md', 'notes/../private.txt', 'notes/link.md'):
                        denied = await session.call_tool('wiki.read', {'path': path})
                        assert denied.is_error

    asyncio.run(check())
