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
    if feature == 'integrations':
        vaults['first']['integrations'] = {'canvas': False, 'bases': False, 'iconic_offline': True}
        vaults['second']['read_obsidian'] = False
        for name in vaults:
            plugin = tmp_path / name / '.obsidian' / 'plugins' / 'iconic'
            plugin.mkdir(parents=True)
            (plugin / 'data.json').write_text('{"fileIcons":{"notes/same.md":{"color":"red","unsynced":["device"]}},"keep":true}')
            (plugin / 'link.json').symlink_to('../../../private.txt')
    if feature == 'media-gallery':
        vaults['first']['integrations'] = {'media_gallery': True}
    if feature == 'fsrs':
        vaults['first']['integrations'] = {'fsrs_dictionary': True}
    if feature == 'frontmatter':
        vaults['first']['protect_frontmatter'] = True
    if feature == 'checks':
        vaults['first']['checks'] = {'default_quotes': '""', 'inner_quoter': '«»', 'callouts': True}
    if feature == 'periods':
        vaults['first']['checks'] = {'trailing_periods': True}
    if feature == 'links':
        vaults['first']['checks'] = {'broken_links': True}
    if feature == 'inline-tags':
        vaults['first']['checks'] = {'prevent_inline_tags': True}
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


@pytest.mark.parametrize('endpoint', ['inline-tags', None], indirect=True)
def test_prevent_inline_tags_is_opt_in(endpoint, request):
    url, directory = endpoint
    enabled = request.node.callspec.params['endpoint'] == 'inline-tags'
    path = 'notes/same.md'
    file = directory / 'first' / path
    original = 'Old #existing\n'
    file.write_text(original)

    async def check():
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer first'}) as http:
            transport = streamable_http_client(f'{url}/first/mcp', http_client=http)
            async with Client(transport, mode='2026-07-28') as session:
                written = await session.call_tool('obsidian.write', {
                    'path': path, 'content': original + 'Literal: `#abcd`\n',
                    'expected_revision': sha256(original.encode()).hexdigest(),
                })
                assert not written.is_error, written.content
                warnings = written.structured_content.get('warnings', [])
                assert len(warnings) == int(enabled)
                result = await session.call_tool('obsidian.update', {
                    'path': path, 'old': '`#abcd`', 'new': '#abcd',
                    'expected_revision': written.structured_content['revision'],
                })
                assert result.is_error == enabled, result.content
                if enabled:
                    assert 'prevent_inline_tags' in str(result.content) and '#abcd' in str(result.content)
                assert file.read_text() == original + ('Literal: `#abcd`\n' if enabled else 'Literal: #abcd\n')

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

                found = await session.call_tool('obsidian.links_out', {'path': path})
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
                    expected = {'obsidian.allowed', 'obsidian.agentsmd', 'obsidian.read', 'obsidian.stat',
                                'obsidian.links_out', 'obsidian.links_in', 'obsidian.media'}
                    if name == 'first':
                        expected.update(('obsidian.write', 'obsidian.update', 'obsidian.delete', 'obsidian.rename',
                                         'obsidian.canvas_edit', 'obsidian.base_edit'))
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


def test_graph_follows_live_paths_and_respects_vault_scope(endpoint):
    url, directory = endpoint
    notes = directory / 'first' / 'notes'
    (notes / 'same.md').write_text(
        '[[future#Section]] [here](./Here%20and%20now.md) ![alt](pic.png) '
        '[[private.txt]] [[link.md]] [web](https://example.com)\n'
    )
    (notes / 'Here and now.md').write_text('Here')
    (notes / 'pic.png').write_bytes(b'png')
    (directory / 'second' / 'notes' / 'future.md').write_text('# Section')

    async def check():
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer first'}) as http:
            transport = streamable_http_client(f'{url}/first/mcp', http_client=http)
            async with Client(transport, mode='2026-07-28') as session:
                async def call(tool, path='notes/same.md'):
                    result = await session.call_tool(f'obsidian.{tool}', {'path': path})
                    assert not result.is_error, result.content
                    return result.structured_content

                async def outgoing():
                    return {row['raw']: row for row in (await call('links_out'))['links']}

                out = await outgoing()
                assert out['[[future#Section]]']['status'] == 'unresolved'
                assert out['[here](./Here%20and%20now.md)']['resolved'] == ['notes/Here and now.md']
                assert out['[[private.txt]]']['resolved'] == []
                assert out['[[link.md]]']['resolved'] == []
                assert out['[web](https://example.com)']['status'] == 'external'
                media = (await call('media'))['media']
                assert [(item['path'], item['mime_type'], item['size_bytes']) for item in media] == [
                    ('notes/pic.png', 'image/png', 3),
                ]
                assert media[0]['links'][0]['raw'] == '![alt](pic.png)'
                (notes / 'future.md').write_text('# Section')
                assert (await outgoing())['[[future#Section]]']['status'] == 'resolved'
                incoming = (await call('links_in', 'notes/future.md'))['links']
                assert [(row['source'], row['anchor']) for row in incoming] == [('notes/same.md', 'Section')]
                (notes / 'other').mkdir()
                (notes / 'other' / 'future.md').write_text('# Section')
                assert (await outgoing())['[[future#Section]]']['status'] == 'ambiguous'
                assert (await call('links_in', 'notes/future.md'))['links'] == []

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


@pytest.mark.parametrize('endpoint', ['fsrs'], indirect=True)
def test_fsrs_dictionary_edits_and_scoped_history(endpoint):
    url, directory = endpoint
    root = directory / 'first'
    path = 'notes/Dictionary.md'
    schedule = '{"i":"card1","s":2,"r":3,"l":0,"S":4,"D":5,"c":7,"d":"2025-01-08T00:00:00Z","t":"2025-01-01T00:00:00Z"}'
    original = f'---\nfsrs-vocabulary: {{mute: true}}\n---\nTheory stays\n\n## Words\n\n| word | translation | due | srs |\n| --- | --- | --- | --- |\n| cat | gato | 2025-01-08 | {schedule} |\n\nAfter the table\n'
    dictionary = root / path
    dictionary.write_text(original)
    plugins = root / '.obsidian' / 'plugins' / 'fsrs-vocabulary'
    plugins.mkdir(parents=True)
    entry = '{"c":"card1","t":1735689600000,"r":3,"s":0}'
    (plugins / 'history-desktop-test.jsonl').write_text(entry + '\n{"c":"private","t":42,"r":2,"s":0}\npartial')
    (plugins / 'history-mobile-sync.jsonl').write_text(entry + '\n')

    async def check():
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer first'}) as http:
            transport = streamable_http_client(f'{url}/first/mcp', http_client=http)
            async with Client(transport, mode='2026-07-28') as session:
                async def call(tool, **kwargs):
                    return await session.call_tool(f'obsidian.fsrs_{tool}', kwargs)

                listing = await call('list')
                assert not listing.is_error, listing.content
                assert listing.structured_content['dictionaries'][0]['path'] == path
                current = await call('read', path=path)
                assert not current.is_error, current.content
                data = current.structured_content
                assert data['muted'] is True and data['cards'][0]['state']['stability'] == 4
                assert data['cards'][0]['state']['difficulty'] == 5
                assert data['cards'][0]['cells'] == {'word': 'cat', 'translation': 'gato'}
                history = await call('history', path=path)
                assert not history.is_error, history.content
                assert [item['card_id'] for item in history.structured_content['reviews']] == ['card1']
                denied = await call('edit', path=path, expected_revision=data['revision'], changes=[
                    {'op': 'update', 'row': 0, 'cells': {'srs': 'bad'}},
                ])
                assert denied.is_error and dictionary.read_text() == original
                edited = await call('edit', path=path, expected_revision=data['revision'], changes=[
                    {'op': 'update', 'row': 0, 'cells': {'word': 'cat | kitten'}},
                    {'op': 'create', 'cells': {'word': 'dog', 'translation': 'perro'}},
                ])
                assert not edited.is_error, edited.content
                assert schedule in dictionary.read_text() and 'Theory stays' in dictionary.read_text()
                next_data = (await call('read', path=path)).structured_content
                assert [item['cells']['word'] for item in next_data['cards']] == ['cat | kitten', 'dog']
                assert next_data['cards'][1]['state'] is None
                deleted = await call('edit', path=path, expected_revision=next_data['revision'], changes=[
                    {'op': 'delete', 'row': 1},
                ])
                assert not deleted.is_error, deleted.content
                assert 'perro' not in dictionary.read_text() and dictionary.read_text().endswith('After the table\n')

    asyncio.run(check())


@pytest.mark.parametrize('endpoint', ['integrations'], indirect=True)
def test_integrations_switch_and_obsidian_read_scope(endpoint):
    url, directory = endpoint
    relative = '.obsidian/plugins/iconic/data.json'
    settings = directory / 'first' / relative

    async def check():
        for name in ('first', 'second'):
            async with httpx.AsyncClient(headers={'Authorization': f'Bearer {name}'}) as http:
                transport = streamable_http_client(f'{url}/{name}/mcp', http_client=http)
                async with Client(transport, mode='2026-07-28') as session:
                    async def call(tool, path, **kwargs):
                        return await session.call_tool(f'obsidian.{tool}', {'path': path, **kwargs})

                    tools = {tool.name for tool in (await session.list_tools()).tools}
                    assert not {'obsidian.canvas_edit', 'obsidian.base_edit', 'obsidian.fsrs_list'} & tools
                    current = await call('read', relative)
                    if name == 'second':
                        assert current.is_error and 'obsidian.iconic_set' not in tools
                        continue
                    assert not current.is_error
                    rev = current.structured_content['revision']
                    assert (await call('stat', relative)).structured_content['revision'] == rev
                    assert (await call('read', '.obsidian/plugins/iconic/link.json')).is_error
                    assert (await call('write', relative, content='{}', expected_revision=rev)).is_error
                    assert (await call('iconic_set', 'private.txt', changes={'icon': '🧠'})).is_error
                    result = await call('iconic_set', 'notes/same.md', changes={'icon': '🧠'})
                    assert not result.is_error, result.content
                    document = json.loads(settings.read_text())
                    assert document['keep'] is True
                    assert document['fileIcons']['notes/same.md'] == {'icon': '🧠', 'color': 'red', 'unsynced': ['device']}
                    root = directory / 'first' / 'notes'
                    (root / 'bad.canvas').write_text('broken')
                    (root / 'example.md').write_text('```media-gallery\n[[same]]\n```')
                    assert (await call('links_out', 'notes/bad.canvas')).is_error
                    assert (await call('links_out', 'notes/example.md')).structured_content['links'] == []
                    assert (await call('links_in', 'notes/same.md')).structured_content['skipped'] == []

    asyncio.run(check())


def test_structured_edits_share_validation_with_text_tools(endpoint):
    url, directory = endpoint
    root = directory / 'first'
    board = 'notes/Board.canvas'
    base = 'notes/Table.base'

    async def check():
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer first'}) as http:
            transport = streamable_http_client(f'{url}/first/mcp', http_client=http)
            async with Client(transport, mode='2026-07-28') as session:
                async def call(tool, path, **kwargs):
                    return await session.call_tool('obsidian.' + tool, {'path': path, **kwargs})

                invalid = await call('write', board, content='{"edges":[{"id":"e","fromNode":"a","toNode":"b"}]}')
                assert invalid.is_error and not (root / board).exists()
                created = await call('write', board, content='{"nodes":[],"edges":[],"plugin":{"keep":true}}')
                assert not created.is_error, created.content
                board_rev = created.structured_content['revision']
                fields = {'type': 'text', 'text': 'Hello', 'x': 0, 'y': 0, 'width': 200, 'height': 100}
                changes = [{'op': 'put_node', 'id': 'a', 'fields': fields},
                           {'op': 'put_node', 'id': 'b', 'fields': {**fields, 'x': 250}},
                           {'op': 'put_edge', 'id': 'e', 'fields': {'fromNode': 'a', 'toNode': 'b'}}]
                edited = await call('canvas_edit', board, expected_revision=board_rev, changes=changes)
                assert not edited.is_error, edited.content
                before = (root / board).read_text()
                content = json.loads(before)
                assert [node['id'] for node in content['nodes']] == ['a', 'b']
                assert content['plugin'] == {'keep': True}
                stale = await call('canvas_edit', board, expected_revision=board_rev, changes=changes)
                assert stale.is_error
                denied = await call('canvas_edit', board, expected_revision=edited.structured_content['revision'],
                                    changes=[{'op': 'put_node', 'id': 'a', 'fields': {'text': 'Changed'}},
                                             {'op': 'remove_node', 'id': 'b'}])
                assert denied.is_error and (root / board).read_text() == before
                denied = await call('update', board, expected_revision=edited.structured_content['revision'],
                                    old='"toNode": "b"', new='"toNode": "missing"')
                assert denied.is_error and (root / board).read_text() == before

                invalid = await call('write', base, content='views: not-a-list\n')
                assert invalid.is_error and not (root / base).exists()
                original = '# Keep me\nproperties:\n  note.status:\n    displayName: "Status" # keep the quote and comment\nviews:\n  - type: table\n    name: Main\n'
                created = await call('write', base, content=original)
                assert not created.is_error, created.content
                edited = await call('base_edit', base, expected_revision=created.structured_content['revision'], changes=[
                    {'op': 'set_formula', 'name': 'label', 'value': 'link("same")'},
                    {'op': 'put_view', 'name': 'Gallery', 'fields': {'type': 'cards', 'order': ['formula.label']}},
                    {'op': 'set_filters', 'view': 'Gallery', 'value': 'file.hasTag("public")'},
                ])
                assert not edited.is_error, edited.content
                text = (root / base).read_text()
                assert '# Keep me' in text and '"Status" # keep the quote and comment' in text
                assert 'name: Main' in text and 'name: Gallery' in text and 'formula.label' in text
                result = await call('links_out', base)
                assert result.structured_content['links'][0]['resolved'] == ['notes/same.md']
                denied = await call('base_edit', base, expected_revision=edited.structured_content['revision'], changes=[
                    {'op': 'remove_formula', 'name': 'label'}, {'op': 'set_filters', 'view': 'Missing', 'value': 'true'},
                ])
                assert denied.is_error and (root / base).read_text() == text

    asyncio.run(check())


def test_canvas_and_base_links_protect_and_follow_renames(endpoint):
    url, directory = endpoint
    notes = directory / 'first' / 'notes'
    target = notes / 'same.md'
    target.write_text('# Topic')
    (notes / 'pic.png').write_bytes(b'png')
    canvas = notes / 'Board.canvas'
    canvas.write_text('''{
  "nodes": [
    {"id":"a", "x":0,"y":0,"width":200,"height":100,"type":"file", "file":"notes/same.md", "subpath":"#Topic"},
    {"id":"b", "x":0,"y":100,"width":200,"height":100,"type":"text", "text":"[[same|caption]]\\n`[[same]]`"},
    {"id":"c", "x":0,"y":200,"width":200,"height":100,"type":"group", "label":"notes/same.md", "background":"notes/pic.png"},
    {"id":"d", "x":0,"y":300,"width":200,"height":100,"type":"link", "url":"https://example.com"}
  ],
  "edges": []
}''')
    base = notes / 'Table.base'
    base.write_text(r'''filters:
  and:
    - 'file.hasLink("same")'
    - 'file.inFolder("notes")'
formulas:
  pair: 'list(link("same#Topic", "Caption"), file("notes/same.md"))'
  dynamic: 'link(file.name + ".md")'
  sample: '"link(\"same\")"'
views:
  - type: table
    name: 'file("notes/same.md")'
    filters: 'file.hasLink("same")'
  - type: table
    name: Overview
''')

    async def check():
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer first'}) as http:
            transport = streamable_http_client(f'{url}/first/mcp', http_client=http)
            async with Client(transport, mode='2026-07-28') as session:
                async def call(tool, path, **kwargs):
                    return await session.call_tool(f'obsidian.{tool}', {'path': path, **kwargs})

                c = await call('links_out', 'notes/Board.canvas')
                assert not c.is_error, c.content
                assert [(edge['syntax'], edge['resolved']) for edge in c.structured_content['links']] == [
                    ('canvas_file', ['notes/same.md']), ('wiki', ['notes/same.md']),
                    ('canvas_file', ['notes/pic.png']), ('canvas_url', []),
                ]
                assert c.structured_content['links'][0]['anchor'] == 'Topic'
                b = await call('links_out', 'notes/Table.base')
                assert not b.is_error, b.content
                assert len(b.structured_content['links']) == 4
                assert all(edge['resolved'] == ['notes/same.md'] for edge in b.structured_content['links'])
                revision = sha256(target.read_bytes()).hexdigest()
                blocked = await call('delete', 'notes/same.md', expected_revision=revision)
                assert blocked.is_error and 'Board.canvas' in str(blocked.content) and 'Table.base' in str(blocked.content)
                result = await call('rename', 'notes/same.md', new_name='New name.md', expected_revision=revision)
                assert not result.is_error, result.content
                assert {entry['path'] for entry in result.structured_content['updated']} == {'notes/Board.canvas', 'notes/Table.base'}
                incoming = await call('links_in', 'notes/New name.md')
                assert len(incoming.structured_content['links']) == 6
                assert '"file":"notes/New name.md", "subpath":"#Topic"' in canvas.read_text()
                assert '`[[same]]`' in canvas.read_text()
                assert '"label":"notes/same.md"' in canvas.read_text()
                assert '    name: \'file("notes/same.md")\'' in base.read_text()
                assert '  dynamic: \'link(file.name + ".md")\'' in base.read_text()

                index = notes / 'Index.md'
                index.write_text('![[Table.base#Overview]] ![[Table.base#Missing]]')
                views = await call('links_out', 'notes/Index.md')
                assert [edge['status'] for edge in views.structured_content['links']] == ['resolved', 'unresolved_anchor']
                (notes / 'Broken.canvas').write_text('{"nodes": [')
                unseen = await call('links_in', 'notes/pic.png')
                assert unseen.structured_content['skipped'] == ['notes/Broken.canvas']
                old_picture = sha256(b'png').hexdigest()
                denied = await call('rename', 'notes/pic.png', new_name='new.png', expected_revision=old_picture)
                assert denied.is_error and 'Broken.canvas' in str(denied.content)
                assert (notes / 'pic.png').exists() and not (notes / 'new.png').exists()

    asyncio.run(check())


@pytest.mark.parametrize('endpoint', ['media-gallery'], indirect=True)
def test_rename_note_and_attachment_preserves_links_and_non_links(endpoint):
    url, directory = endpoint
    notes = directory / 'first' / 'notes'
    target = notes / 'same.md'
    target.write_text('# Topic\n\n[[same#Topic]]')
    picture = notes / 'image.png'
    picture.write_bytes(b'\x89PNG\0')
    source = notes / 'referrer.md'
    source.write_text('''---
related: "[[same#Topic]]"
---

[[same#Topic|caption]] [hello](./same.md#Topic "title")

`[[same#Topic|caption]]`

```media-gallery
[[image.png]]
path: notes
```
''')
    (notes / 'taken.md').write_text('Keep this')

    async def check():
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer first'}) as http:
            transport = streamable_http_client(f'{url}/first/mcp', http_client=http)
            async with Client(transport, mode='2026-07-28') as session:
                async def call(tool, **arguments):
                    return await session.call_tool(f'obsidian.{tool}', arguments)

                async def rename(name, new_name, expected_revision):
                    return await call('rename', path=f'notes/{name}', new_name=new_name,
                                      expected_revision=expected_revision)

                revision = sha256(target.read_bytes()).hexdigest()
                old_source = source.read_text()
                assert (await rename('same.md', 'taken.md', revision)).is_error
                assert (await rename('same.md', 'New name.md', '0' * 64)).is_error
                assert source.read_text() == old_source and target.exists()
                result = await rename('same.md', 'New name.md', revision)
                assert not result.is_error, result.content
                assert not target.exists()
                assert result.structured_content['path'] == 'notes/New name.md'
                assert '`[[same#Topic|caption]]`' in source.read_text()
                assert '|caption]]' in source.read_text() and '"title"' in source.read_text()
                incoming = await call('links_in', path='notes/New name.md')
                assert [link['source'] for link in incoming.structured_content['links']] == [
                    'notes/New name.md', 'notes/referrer.md', 'notes/referrer.md', 'notes/referrer.md',
                ]
                info = await call('stat', path='notes/image.png')
                result = await rename('image.png', 'cover.png', info.structured_content['revision'])
                assert not result.is_error, result.content
                assert not picture.exists() and (notes / 'cover.png').read_bytes() == b'\x89PNG\0'
                media = await call('media', path='notes/referrer.md')
                item, = media.structured_content['media']
                assert item['path'] == 'notes/cover.png'
                assert {link['syntax'] for link in item['links']} == {'wiki', 'gallery_path'}

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

                source = file.with_name('referrer.md')
                source.write_text('---\nrelated: "[[same]]"\n---\n')
                denied = await delete(path, revision)
                assert denied.is_error and 'notes/referrer.md' in str(denied.content)
                assert file.read_bytes() == b'first'
                assert list(trash.iterdir()) == []
                source.write_bytes(b'\xff')
                denied = await delete(path, revision)
                assert denied.is_error and 'notes/referrer.md' in str(denied.content)
                source.write_text('`[[same]]`')

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
