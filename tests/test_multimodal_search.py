import json
import shutil
import subprocess
from io import BytesIO
from wave import open as wave_open

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

from obsidian_thing.search import SearchIndex
from obsidian_thing.search_attachments import attachment_parts
from obsidian_thing.vault import Vault, VaultError


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='Requires local FFmpeg tools')
def test_audio_and_video_validation_and_indexing(tmp_path, monkeypatch):
    wav = tmp_path / 'sample.wav'
    with wave_open(str(wav), 'wb') as stream:
        stream.setparams((1, 1, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x80' * 8000)
    mp3 = tmp_path / 'sample.mp3'
    mp4 = tmp_path / 'sample.mp4'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(wav), str(mp3)], check=True, timeout=10)
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=s=32x32:r=1',
                    '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(mp4)], check=True, timeout=10)
    for mime, path in (('audio/wav', wav), ('audio/mpeg', mp3), ('video/mp4', mp4)):
        data = path.read_bytes()
        assert list(attachment_parts(mime, data)) == [data]
    with pytest.raises(VaultError):
        list(attachment_parts('video/mp4', wav.read_bytes()))
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setenv('TEST_MULTIMODAL_KEY', 'synthetic-key')
    index = SearchIndex(Vault(tmp_path, ['.']), dict(provider='openrouter', model='google/gemini-embedding-2',
                        dimensions=2, route='google-vertex/us', key_env='TEST_MULTIMODAL_KEY', allow_cloud=True,
                        include_paths=['.'], allowed_mime_types=['audio/wav', 'audio/mpeg', 'video/mp4']), state)
    assert index.embedder is not None
    sent = []

    def respond(request, *, timeout):
        value = json.loads(request.data)['input']
        if not isinstance(value, str):
            sent.append(value[0]['content'][0]['type'])
        return BytesIO(b'{"data":[{"embedding":[1,0]}]}')

    monkeypatch.setattr(index.embedder.http, 'open', respond)
    index.reconcile_local()
    assert not sent
    assert index.run_pending()
    assert sorted(sent) == ['input_audio', 'input_audio', 'input_video']
    assert {hit['path'] for hit in index.query('synthetic', 10)['matches']} == {
        'sample.wav', 'sample.mp3', 'sample.mp4',
    }
    with wave_open(str(wav), 'wb') as stream:
        stream.setparams((1, 1, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x80' * (181 * 8000))
    with pytest.raises(VaultError, match='duration'):
        index.index('sample.wav')
    assert len(sent) == 3


def test_media_queue_and_page_matches(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    state.mkdir()
    root = tmp_path / 'vault'
    root.mkdir()
    Image.new('RGB', (16, 16), 'red').save(root / 'red.png')
    writer = PdfWriter()
    writer.add_blank_page(64, 64)
    writer.add_blank_page(128, 128)
    writer.write(root / 'pages.pdf')
    (root / 'fake.jpg').write_bytes((root / 'red.png').read_bytes())
    monkeypatch.setenv('TEST_MULTIMODAL_KEY', 'synthetic-key')
    config = dict(provider='openrouter', model='google/gemini-embedding-2', dimensions=2,
                  route='google-vertex/us', key_env='TEST_MULTIMODAL_KEY', allow_cloud=True,
                  include_paths=['.'])
    vault = Vault(root, ['.'])
    disabled = SearchIndex(vault, config, state)
    with pytest.raises(VaultError):
        disabled.index('red.png')
    config['allowed_mime_types'] = ['text/markdown', 'image/png', 'image/jpeg', 'application/pdf']
    index = SearchIndex(vault, config, state)
    assert index.embedder is not None
    calls = []

    def respond(request, *, timeout):
        from base64 import b64decode

        body = json.loads(request.data)['input']
        calls.append(body)
        vector = [1, 0]
        if not isinstance(body, str):
            part, = body[0]['content']
            if part['type'] == 'input_file':
                pdf = PdfReader(BytesIO(b64decode(part['input_file']['data'].split(',', 1)[1])))
                page, = pdf.pages
                if page.mediabox.width == 64:
                    vector = [0, 1]
        return BytesIO(json.dumps({'data': [{'embedding': vector}]}).encode())

    monkeypatch.setattr(index.embedder.http, 'open', respond)
    index.reconcile_local()
    assert not calls
    assert index.run_pending()
    assert not index.pending()
    assert len(calls) == 3
    hits = {hit['path']: hit for hit in index.query('red square', 10)['matches']}
    assert set(hits) == {'pages.pdf', 'red.png'}
    assert hits['pages.pdf'].get('page') == 2
    assert 'line_start' not in hits['pages.pdf']
    index.related('red.png', 10, 0)
    assert len(calls) == 4
    with pytest.raises(VaultError):
        index.index('fake.jpg')
    (root / 'red.png').write_bytes(b'changed')
    assert [hit['path'] for hit in index.query('red square', 10)['matches']] == ['pages.pdf']


def test_full_scan_detects_changed_attachment_with_unchanged_stamp(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    path = root / 'color.png'
    Image.new('RGB', (16, 16), 'red').save(path)
    monkeypatch.setenv('TEST_MULTIMODAL_KEY', 'synthetic-key')
    config = dict(provider='openrouter', model='google/gemini-embedding-2', dimensions=2,
                  route='google-vertex/us', key_env='TEST_MULTIMODAL_KEY', allow_cloud=True,
                  include_paths=['.'], allowed_mime_types=['image/png'])
    vault = Vault(root, ['.'])
    monkeypatch.setattr(vault, 'attachment_stamp', lambda *args: 'same')
    monkeypatch.setattr(vault, 'read_attachment', lambda *args: ('same', path.read_bytes()))
    index = SearchIndex(vault, config, tmp_path)
    assert index.embedder is not None
    monkeypatch.setattr(index.embedder, 'embed_attachment', lambda *args: [1, 0])
    index.reconcile_local()
    assert index.run_pending()
    assert not index.pending()
    Image.new('RGB', (16, 16), 'blue').save(path)
    index.reconcile_local(['color.png'])
    assert not index.pending()
    index.reconcile_local()
    assert [path for path, _ in index.pending()] == ['color.png']
    assert index.run_pending()
    assert not index.pending()


def test_attachment_consent_and_scope_are_checked_before_open(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setenv('TEST_MULTIMODAL_KEY', 'synthetic-key')
    config = dict(provider='openrouter', model='google/gemini-embedding-2', dimensions=2,
                  route='google-vertex/us', key_env='TEST_MULTIMODAL_KEY', include_paths=['.'],
                  exclude_paths=['private'], allowed_mime_types=['image/png'])
    vault = Vault(root, ['.'])
    denied = SearchIndex(vault, config, state)
    permitted = SearchIndex(vault, config | {'allow_cloud': True}, state)
    monkeypatch.setattr(vault, 'read_attachment', lambda *args: pytest.fail('Unexpected attachment read'))
    with pytest.raises(ValueError, match='allow_cloud'):
        denied.index('red.png')
    with pytest.raises(VaultError, match='excluded'):
        permitted.related('private/red.png', 10, 0)
