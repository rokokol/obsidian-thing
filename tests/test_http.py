import asyncio
import json
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
        + ('writable = true\ntrash_dir = "/fixtures/trash"\n' if settings.get('writable') else '')
        + ('protect_frontmatter = true\n' if settings.get('protect_frontmatter') else '')
        for name, settings in vaults.items()
    ) + ('\n[vaults.first.checker]\nscript = "/fixtures/checker.py"\ntimeout_seconds = 1\n'
         if 'checker' in vaults['first'] else '')
        + ('\n[vaults.first.checks]\n' + ''.join(
            f'{name} = {json.dumps(value)}\n' for name, value in vaults['first'].get('checks', {}).items()
            if value) if vaults['first'].get('checks') else ''))
    env = [arg for settings in vaults.values() for arg in ('--env', settings['token_env'])]
    container = docker(
        'run', '--detach', '--pull=never', '--read-only', '--user', f'{os.getuid()}:{os.getgid()}',
        '--volume', f'{directory}:/fixtures', '--publish', '127.0.0.1::8765', *env,
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
def endpoint(tmp_path, monkeypatch, request):
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
    trash = tmp_path / 'trash'
    trash.mkdir(mode=0o700)
    vaults['first'].update(writable=True, trash_dir=str(trash))
    feature = getattr(request, 'param', None)
    if feature == 'frontmatter':
        vaults['first']['protect_frontmatter'] = True
    if feature == 'checks':
        vaults['first']['checks'] = {'default_quotes': '""', 'inner_quoter': '«»', 'callouts': True}
    if feature == 'periods':
        vaults['first']['checks'] = {'trailing_periods': True}
    if feature == 'links':
        vaults['first']['checks'] = {'broken_links': True}
    if feature is True:
        script = tmp_path / 'checker.py'
        script.write_text(CHECKER_SCRIPT)
        vaults['first']['checker'] = {'script': str(script), 'timeout_seconds': 1}
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


CHECKER_SCRIPT = '''\
import json
import os
import sys
import time

import markdown_it

request = json.load(sys.stdin)
assert request['version'] == 1
assert 'TEST_TOKEN_FIRST' not in os.environ
assert isinstance(request['links'], list)
before, after = request['before'], request['after']
path = request['path']
if path.endswith('/timeout.md'):
    time.sleep(30)
if path.endswith('/crash.md'):
    sys.stderr.write('private diagnostic')
    sys.exit(2)
if path.endswith('/invalid.md'):
    print('{"decision":"allow","after":"rewritten"}')
    sys.exit()
if path.endswith('/large.md'):
    print(' ' * 100_000 + '{"decision":"allow"}')
    sys.exit()
if path == 'notes/linked.md':
    assert request['links'] == [{'raw': '[[AGENTS]]', 'target': 'AGENTS', 'anchor': '', 'embed': False,
                                 'resolved': ['AGENTS.md'], 'self': False, 'anchor_ok': True}], request['links']
if path == 'notes/same.md' and request['operation'] == 'update':
    assert before == 'LEGACY\\r\\nold\\r\\n' and after == 'LEGACY\\r\\nновый\\r\\n'
if after.count('LEGACY') > (before or '').count('LEGACY'):
    print(json.dumps({'decision': 'reject', 'message': 'Remove the new marker'}))
elif 'LEGACY' in after:
    print(json.dumps({'decision': 'allow', 'warnings': ['Existing marker remains']}))
else:
    print(json.dumps({'decision': 'allow'}))
'''


@pytest.mark.parametrize('endpoint', [True], indirect=True)
def test_checker_warns_on_old_violations_and_rejects_new_ones(endpoint):
    url, directory = endpoint
    root = directory / 'first'
    path = 'notes/same.md'
    original = 'LEGACY\r\nold\r\n'.encode()
    expected = 'LEGACY\r\nновый\r\n'.encode()
    (root / path).write_bytes(original)

    async def check():
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer first'}) as http:
            transport = streamable_http_client(f'{url}/first/mcp', http_client=http)
            async with Client(transport, mode='2026-07-28') as session:
                created = await session.call_tool('obsidian.write', {'path': 'notes/new.md', 'content': '# New\n'})
                assert not created.is_error
                assert created.structured_content == {'path': 'notes/new.md', 'revision': sha256(b'# New\n').hexdigest()}
                linked = await session.call_tool('obsidian.write', {'path': 'notes/linked.md', 'content': 'См. [[AGENTS]]\n'})
                assert not linked.is_error, linked.content
                updated = await session.call_tool('obsidian.update', {
                    'path': path, 'old': 'old', 'new': 'новый', 'expected_revision': sha256(original).hexdigest(),
                })
                assert not updated.is_error
                assert updated.structured_content == {
                    'path': path, 'revision': sha256(expected).hexdigest(), 'warnings': ['Existing marker remains'],
                }
                assert (root / path).read_bytes() == expected
                for target, revision in ((path, sha256(expected).hexdigest()), ('notes/denied.md', None)):
                    rejected = await session.call_tool('obsidian.write', {
                        'path': target, 'content': 'LEGACY LEGACY', 'expected_revision': revision,
                    })
                    assert rejected.is_error
                    assert 'Remove the new marker' in str(rejected.content)
                assert (root / path).read_bytes() == expected
                assert not (root / 'notes/denied.md').exists()

    asyncio.run(check())


@pytest.mark.parametrize('endpoint', [True], indirect=True)
def test_checker_failure_blocks_writes(endpoint):
    url, directory = endpoint
    root = directory / 'first'

    async def check():
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer first'}) as http:
            transport = streamable_http_client(f'{url}/first/mcp', http_client=http)
            async with Client(transport, mode='2026-07-28') as session:
                for case in ('timeout', 'crash', 'invalid', 'large'):
                    path = f'notes/{case}.md'
                    result = await session.call_tool('obsidian.write', {'path': path, 'content': 'should not be saved'})
                    assert result.is_error, case
                    assert 'checker' in str(result.content).lower()
                    assert 'private diagnostic' not in str(result.content)
                    assert not (root / path).exists()
                assert not list((root / 'notes').glob('.obsidian-thing-*'))

    asyncio.run(check())


@pytest.mark.parametrize('endpoint', ['frontmatter'], indirect=True)
def test_frontmatter_guard_compares_complete_markdown_versions(endpoint):
    url, directory = endpoint
    path = 'notes/same.md'
    file = directory / 'first' / path
    clean = '---\r\ntags: [math, analysis]\r\n---\r\n# Title\r\n'
    revision = sha256(clean.encode()).hexdigest()
    file.write_bytes(clean.encode())

    async def check():
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer first'}) as http:
            transport = streamable_http_client(f'{url}/first/mcp', http_client=http)
            async with Client(transport, mode='2026-07-28') as session:
                async def write(content, expected_revision: str | None = revision, target=path):
                    return await session.call_tool('obsidian.write', {
                        'path': target, 'content': content, 'expected_revision': expected_revision,
                    })

                async def update(old, new, rev=revision):
                    return await session.call_tool('obsidian.update', {
                        'path': path, 'old': old, 'new': new, 'expected_revision': rev,
                    })

                for content in (
                    '---\ntags: [math\n---\n# Title\n',
                    '---\ntags: [math]\n"tags": [analysis]\n---\n# Title\n',
                    '---\n- math\n- analysis\n---\n# Title\n',
                    '---\ntags: [math]\n# Title\n',
                ):
                    rejected = await write(content)
                    assert rejected.is_error
                    assert 'frontmatter' in str(rejected.content).lower()
                    assert file.read_bytes() == clean.encode()
                rejected = await update('[math, analysis]', '[math, analysis')
                assert rejected.is_error and 'frontmatter' in str(rejected.content).lower()
                assert file.read_bytes() == clean.encode()

                edited = await update('[math, analysis]', '\r\n  - math\r\n  - notes')
                assert not edited.is_error and 'warnings' not in edited.structured_content
                stripped = await write('# Title\n', edited.structured_content['revision'])
                assert not stripped.is_error and stripped.structured_content['warnings']
                assert file.read_bytes() == b'# Title\n'
                draft = await write('---\ntags: [math\n---\n# Draft\n', None, 'notes/draft.md')
                assert not draft.is_error and draft.structured_content['warnings']

                broken = clean.replace('[math, analysis]', '[math')
                file.write_bytes(broken.encode())
                nearby = await update('# Title', '# New title', sha256(broken.encode()).hexdigest())
                assert not nearby.is_error and nearby.structured_content['warnings']
                fixed = await update('tags: [math\r\n', 'tags: [math]\r\n', nearby.structured_content['revision'])
                assert not fixed.is_error and 'warnings' not in fixed.structured_content
                text = await write('---\nnot YAML\n', None, 'notes/raw.txt')
                assert not text.is_error and 'warnings' not in text.structured_content

    asyncio.run(check())


@pytest.mark.parametrize('endpoint', ['checks'], indirect=True)
def test_quote_and_callout_checks_block_only_new_mistakes(endpoint):
    url, directory = endpoint
    path = 'notes/same.md'
    file = directory / 'first' / path
    original = 'Ёлочки «так» остаются\n\n> [!quorte] Чужой тип\n'
    file.write_bytes(original.encode())
    current = {'revision': sha256(original.encode()).hexdigest()}

    async def check():
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer first'}) as http:
            transport = streamable_http_client(f'{url}/first/mcp', http_client=http)
            async with Client(transport, mode='2026-07-28') as session:
                async def write(content, rev=None, target=path):
                    return await session.call_tool('obsidian.write', {
                        'path': target, 'content': content,
                        'expected_revision': current['revision'] if rev is None else rev,
                    })

                nearby = await write(original + '\nДописан абзац\n')
                assert not nearby.is_error
                assert any('Existing [default_quotes]' in warning for warning in nearby.structured_content['warnings'])
                assert any('callouts' in warning for warning in nearby.structured_content['warnings'])
                revision = nearby.structured_content['revision']
                current['revision'] = revision

                for content in ('Обычный текст «в ёлочках»\n', 'Текст с „лапками“\n'):
                    rejected = await write(content, revision)
                    assert rejected.is_error and 'default_quotes' in str(rejected.content)
                    assert file.read_bytes() == (original + '\nДописан абзац\n').encode()

                accepted = (
                    'Вложенная цитата "внешняя «внутренняя» вставка"\n\n'
                    'Код `«в бэктиках»` не считается\n\n'
                    '```\n«в фенсе» и [!nope]\n```\n\n'
                    '> [!note] Известный тип\n\n'
                    '> [!tip]- Свёрнутый\n'
                )
                allowed = await write(accepted, revision)
                assert not allowed.is_error, allowed.content

    asyncio.run(check())


@pytest.mark.parametrize('endpoint', ['periods', None], indirect=True)
def test_trailing_periods_are_opt_in(endpoint, request):
    url, directory = endpoint
    enabled = request.node.callspec.params['endpoint'] == 'periods'
    path = 'notes/same.md'
    file = directory / 'first' / path
    original = 'Старый абзац.\n'
    file.write_text(original)

    async def check():
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer first'}) as http:
            transport = streamable_http_client(f'{url}/first/mcp', http_client=http)
            async with Client(transport, mode='2026-07-28') as session:
                async def write(text):
                    return await session.call_tool('obsidian.write', {
                        'path': path, 'content': text, 'expected_revision': sha256(file.read_bytes()).hexdigest(),
                    })

                for new in ('Новый абзац\nс переносом.\n', '- Новый пункт.\n', '| Колонка |\n|---|\n| **Ячейка.** |\n'):
                    result = await write(original + '\n' + new)
                    assert result.is_error == enabled, result.content
                    if enabled:
                        assert 'trailing_periods' in str(result.content)
                        assert file.read_text() == original
                accepted = await write(original + '\n---\n\n**Фраза.** Продолжение\n\n`код.`\n\n'
                                       '```\nкод.\n```\n\n$$\nx.\n$$\n\n# Заголовок.\n\nОговорка и т.д.\n')
                assert not accepted.is_error, accepted.content
                warnings = accepted.structured_content.get('warnings', [])
                assert len(warnings) == int(enabled)

    asyncio.run(check())


@pytest.mark.parametrize('endpoint', ['links'], indirect=True)
def test_broken_links_block_new_ones_and_warning_limit(endpoint):
    url, directory = endpoint
    root = directory / 'first'
    path = 'notes/same.md'
    file = root / path
    original = 'Работает [[same]], сломана [[старая]]\n\n## Раздел\n'
    file.write_text(original)
    current = {'revision': sha256(original.encode()).hexdigest()}

    async def check():
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer first'}) as http:
            transport = streamable_http_client(f'{url}/first/mcp', http_client=http)
            async with Client(transport, mode='2026-07-28') as session:
                async def write(content):
                    return await session.call_tool('obsidian.write', {
                        'path': path, 'content': content, 'expected_revision': current['revision'],
                    })

                nearby = await write(original + '\nДописано\n')
                assert not nearby.is_error
                assert any('Existing [broken_links]' in warning for warning in nearby.structured_content['warnings'])
                current['revision'] = nearby.structured_content['revision']

                broken = await write(original + '\nНовая [[нет такой]] ссылка\n')
                assert broken.is_error and 'broken_links' in str(broken.content)
                anchor = await write(original + '\n[[same#Нет раздела]]\n')
                assert anchor.is_error and 'broken_links' in str(anchor.content)
                good = await write(original + '\n[[same#Раздел]] и ![[missing.png]]\n')
                assert good.is_error and 'attachment' in str(good.content)
                assert file.read_text() == original + '\nДописано\n'

                found = await session.call_tool('obsidian.links', {'path': path})
                resolved = {item['target']: item['resolved'] for item in found.structured_content['links']}
                assert resolved == {'same': [path], 'старая': []}

                many = '\n'.join(f'[[missing-{index}]]' for index in range(12))
                (root / 'notes' / 'many.md').write_text(many + '\n')
                revision = sha256((many + '\n').encode()).hexdigest()
                capped = await session.call_tool('obsidian.write', {
                    'path': 'notes/many.md', 'content': many + '\nДописано\n', 'expected_revision': revision,
                })
                assert not capped.is_error
                warnings = capped.structured_content['warnings']
                assert len(warnings) == 11
                assert warnings[-1].startswith('2 more warnings')
                everything = await session.call_tool('obsidian.update', {
                    'path': 'notes/many.md', 'old': 'Дописано', 'new': 'Дописано дважды',
                    'expected_revision': capped.structured_content['revision'], 'all_warnings': True,
                })
                assert len(everything.structured_content['warnings']) == 12

    asyncio.run(check())


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
                    expected = {'obsidian.allowed', 'obsidian.agentsmd', 'obsidian.read', 'obsidian.links'}
                    if name == 'first':
                        expected.update(('obsidian.write', 'obsidian.update', 'obsidian.delete'))
                    assert {tool.name for tool in tools.tools} == expected
                    allowed = await session.call_tool('obsidian.allowed')
                    assert set(allowed.structured_content['paths']) == {'notes', 'AGENTS.md'}
                    note = await session.call_tool('obsidian.read', {'path': 'notes/same.md'})
                    assert note.structured_content['content'] == name
                    assert note.structured_content['revision'] == sha256(name.encode()).hexdigest()
                    (directory / name / 'notes' / 'same.md').write_text(f'{name} changed')
                    recent = await session.call_tool('obsidian.read', {'path': 'notes/same.md'})
                    assert recent.structured_content['content'] == f'{name} changed'
                    rules = await session.call_tool('obsidian.agentsmd')
                    assert rules.structured_content['content'] == f'Rules for {name}'
                    for path in ('private.txt', 'notes-backup/file.md', 'notes/../private.txt', 'notes/link.md'):
                        denied = await session.call_tool('obsidian.read', {'path': path})
                        assert denied.is_error

    asyncio.run(check())


def test_write_requires_current_revision_and_respects_scope(endpoint):
    url, directory = endpoint

    async def check():
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer first'}) as http:
            transport = streamable_http_client(f'{url}/first/mcp', http_client=http)
            async with Client(transport, mode='2026-07-28') as session:
                async def write(path, content, **kwargs):
                    return await session.call_tool('obsidian.write', {'path': path, 'content': content, **kwargs})

                path = 'notes/new.md'
                created = await write(path, 'Новая заметка\n')
                assert not created.is_error
                revision = sha256('Новая заметка\n'.encode()).hexdigest()
                assert created.structured_content == {'path': path, 'revision': revision}
                assert (await write(path, 'overwrite without revision')).is_error
                assert (await write(path, 'overwrite stale revision', expected_revision='0' * 64)).is_error
                assert (directory / 'first' / path).read_bytes() == 'Новая заметка\n'.encode()

                results = await asyncio.gather(*(
                    write(path, content, expected_revision=revision) for content in ('alpha', 'beta')
                ))
                assert sum(not result.is_error for result in results) == 1
                winner = ('alpha', 'beta')[next(i for i, r in enumerate(results) if not r.is_error)]
                assert (directory / 'first' / path).read_text() == winner

                for denied in ('private.txt', 'notes/../private.txt', 'notes/.hidden', 'notes/link.md', 'notes/missing/new.md'):
                    assert (await write(denied, 'blocked')).is_error
                assert (directory / 'first' / 'notes/link.md').is_symlink()
                assert (directory / 'first' / 'private.txt').read_text() == 'private'
                assert not (directory / 'second' / path).exists()

    asyncio.run(check())


def test_update_replaces_only_a_unique_fragment_at_the_current_revision(endpoint):
    url, directory = endpoint
    path = 'notes/same.md'
    file = directory / 'first' / path
    original = 'aaaa\r\nunique\r\n'
    file.write_bytes(original.encode())
    revision = sha256(original.encode()).hexdigest()

    async def check():
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer first'}) as http:
            transport = streamable_http_client(f'{url}/first/mcp', http_client=http)
            async with Client(transport, mode='2026-07-28') as session:
                async def update(old, new, rev=revision):
                    return await session.call_tool('obsidian.update', {
                        'path': path, 'old': old, 'new': new, 'expected_revision': rev,
                    })

                for old in ('absent', 'a', 'aaa', ''):
                    assert (await update(old, 'wrong')).is_error
                    assert file.read_bytes() == original.encode()
                result = await update('unique', 'новый')
                expected = 'aaaa\r\nновый\r\n'.encode()
                assert not result.is_error
                assert result.structured_content == {'path': path, 'revision': sha256(expected).hexdigest()}
                assert (await update('новый', 'stale')).is_error
                assert file.read_bytes() == expected

    asyncio.run(check())


def test_delete_moves_current_text_to_trash(endpoint):
    url, directory = endpoint
    path = 'notes/same.md'
    file = directory / 'first' / path
    trash = directory / 'trash'
    revision = sha256(file.read_bytes()).hexdigest()

    async def check():
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer first'}) as http:
            transport = streamable_http_client(f'{url}/first/mcp', http_client=http)
            async with Client(transport, mode='2026-07-28') as session:
                async def delete(target, rev):
                    return await session.call_tool('obsidian.delete', {'path': target, 'expected_revision': rev})

                assert (await delete(path, '0' * 64)).is_error
                assert (await delete('notes/link.md', sha256(b'private').hexdigest())).is_error
                assert (await delete('AGENTS.md', sha256(b'Rules for first').hexdigest())).is_error
                assert list(trash.iterdir()) == []
                assert file.read_bytes() == b'first'

                trash.chmod(0o755)
                try:
                    assert (await delete(path, revision)).is_error
                    assert file.read_bytes() == b'first'
                    assert list(trash.iterdir()) == []
                finally:
                    trash.chmod(0o700)

                result = await delete(path, revision)
                assert not result.is_error
                assert result.structured_content['path'] == path
                assert result.structured_content['revision'] == revision
                (entry,) = trash.iterdir()
                assert result.structured_content['trash_id'] == entry.name
                assert (entry / 'path').read_text() == path
                assert (entry / 'file').read_bytes() == b'first'
                assert not file.exists()
                assert (directory / 'second' / path).read_bytes() == b'second'
                assert (await delete(path, revision)).is_error

    asyncio.run(check())
