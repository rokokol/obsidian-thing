import json
import sqlite3
from decimal import Decimal
from io import BytesIO

import pytest

from obsidian_thing.embeddings import Embedder, EmbeddingError


def test_daily_budget_reserves_before_http_and_uses_reported_cost(tmp_path, monkeypatch):
    config = dict(provider='openai', base_url='https://provider.example/v1', model='test', dimensions=2,
                  allow_cloud=True, daily_budget_usd='0.01', usd_per_million_tokens='1000')
    first = Embedder(config, tmp_path)
    second = Embedder(config, tmp_path)
    monkeypatch.setattr(second.http, 'open', lambda *args, **kwargs: pytest.fail('Budget allowed a second request'))

    def respond(request, *, timeout):
        with pytest.raises(EmbeddingError, match='budget'):
            second.embed('123456', 'query')
        return BytesIO(b'{"id":"test-id","data":[{"embedding":[3,4]}],"usage":{"cost":0.008}}')

    monkeypatch.setattr(first.http, 'open', respond)
    first.embed('123456', 'background')
    with pytest.raises(EmbeddingError, match='budget'):
        second.embed('123', 'related')
    with sqlite3.connect(first.journal) as db:
        assert db.execute('SELECT usd, cost_kind, status FROM calls').fetchall() == [('0.008', 'reported', 'ok')]


def test_attachment_reservation_keeps_estimate_on_transport_failure(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_PROVIDER_KEY', 'synthetic-key')
    embedder = Embedder(dict(provider='openrouter', model='google/gemini-embedding-2', route='google-vertex/us',
                            key_env='TEST_PROVIDER_KEY', dimensions=2, allow_cloud=True, daily_budget_usd='0.01',
                            usd_per_attachment_unit={'application/pdf': '0.007'}), tmp_path)
    sends = []

    def timeout(request, **kwargs):
        sends.append(request)
        raise TimeoutError

    monkeypatch.setattr(embedder.http, 'open', timeout)
    with pytest.raises(EmbeddingError, match='provider request failed'):
        embedder.embed_attachment('application/pdf', b'synthetic page', 'background')
    with pytest.raises(EmbeddingError, match='estimate'):
        embedder.embed_attachment('image/png', b'synthetic image', 'index')
    with pytest.raises(EmbeddingError, match='budget'):
        embedder.embed_attachment('application/pdf', b'another synthetic page', 'related')
    assert len(sends) == 1
    with sqlite3.connect(embedder.journal) as db:
        assert db.execute('SELECT usd, cost_kind, status FROM calls').fetchall() == [('0.007', 'estimated', 'error')]


def test_daily_budget_rejects_missing_price_before_send(tmp_path, monkeypatch):
    embedder = Embedder(dict(provider='openai', base_url='https://provider.example/v1', model='test', dimensions=2,
                            allow_cloud=True, daily_budget_usd='1'), tmp_path)
    monkeypatch.setattr(embedder.http, 'open', lambda *args, **kwargs: pytest.fail('Sent unpriced input'))
    with pytest.raises(EmbeddingError, match='estimate'):
        embedder.embed('synthetic', 'index')


def test_cloud_requires_opt_in_and_pins_privacy_policy(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_PROVIDER_KEY', 'synthetic-key')
    config = dict(provider='openrouter', model='test/model', route='test-route',
                  dimensions=2, key_env='TEST_PROVIDER_KEY', usd_per_million_tokens='0.4')
    denied = Embedder(config, tmp_path)
    monkeypatch.setattr(denied.http, 'open', lambda *args, **kwargs: pytest.fail('Unexpected HTTP request'))
    for operation in ('index', 'background', 'related', 'query'):
        with pytest.raises(EmbeddingError, match='allow_cloud'):
            denied.embed('synthetic note', operation)
    embedder = Embedder(config | {'allow_cloud': True}, tmp_path)
    replies = iter([{'cost': 0}, {}, {'prompt_tokens': 10}, None])

    def respond(request, *, timeout):
        assert request.full_url == 'https://openrouter.ai/api/v1/embeddings'
        assert request.get_header('Authorization') == 'Bearer synthetic-key'
        body = json.loads(request.data)
        assert body['input'] == 'synthetic note'
        assert body['provider'] == {'only': ['test-route'], 'allow_fallbacks': False,
                                    'zdr': True, 'data_collection': 'deny'}
        usage = next(replies)
        if usage is None:
            raise TimeoutError('synthetic note')
        return BytesIO(json.dumps({'data': [{'embedding': [3, 4]}], 'usage': usage}).encode())

    monkeypatch.setattr(embedder.http, 'open', respond)
    for operation in ('index', 'query', 'related'):
        assert embedder.embed('synthetic note', operation) == pytest.approx([0.6, 0.8])
    with pytest.raises(EmbeddingError, match='provider request failed'):
        embedder.embed('synthetic note', 'index')
    with sqlite3.connect(tmp_path / 'spend.sqlite3') as db:
        rows = db.execute('SELECT status, input_tokens, cost_kind, usd FROM calls ORDER BY id').fetchall()
        assert 'synthetic note' not in '\n'.join(db.iterdump())
        assert 'synthetic-key' not in '\n'.join(db.iterdump())
    assert rows == [('ok', None, 'reported', '0'), ('ok', None, 'unknown', None),
                    ('ok', 10, 'estimated', str(Decimal('0.000004'))), ('error', None, 'unknown', None)]


def test_ollama_rejects_remote_hosts_and_wrong_dimensions(tmp_path, monkeypatch):
    config = dict(provider='ollama', model='test', dimensions=2)
    with pytest.raises(ValueError, match='HTTPS'):
        Embedder(config | {'url': 'http://example.com'}, tmp_path)
    embedder = Embedder(config, tmp_path)

    def respond(request, *, timeout):
        assert request.full_url == 'http://127.0.0.1:11434/api/embed'
        assert json.loads(request.data) == {'model': 'test', 'input': 'synthetic query', 'truncate': False}
        return BytesIO(b'{"embeddings":[[1]],"prompt_eval_count":2}')

    monkeypatch.setattr(embedder.http, 'open', respond)
    with pytest.raises(EmbeddingError):
        embedder.embed('synthetic query', 'query')
    with sqlite3.connect(tmp_path / 'spend.sqlite3') as db:
        assert db.execute('SELECT cost_kind, usd, status FROM calls').fetchone() == ('local', None, 'error')


def test_openrouter_accepts_explicit_privacy_policy(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_PROVIDER_KEY', 'synthetic-key')
    config = dict(provider='openrouter', model='test/model', route='test-route', dimensions=2,
                  key_env='TEST_PROVIDER_KEY', allow_cloud=True)
    embedder = Embedder(config | {'zdr': False, 'data_collection': 'allow'}, tmp_path)

    def respond(request, *, timeout):
        assert json.loads(request.data)['provider'] == {
            'only': ['test-route'], 'allow_fallbacks': False, 'zdr': False, 'data_collection': 'allow',
        }
        return BytesIO(b'{"data":[{"embedding":[3,4]}]}')

    monkeypatch.setattr(embedder.http, 'open', respond)
    assert embedder.embed('synthetic query', 'query') == pytest.approx([0.6, 0.8])
    for invalid in ({'zdr': 1}, {'data_collection': 'maybe'}, {'allow_cloud': 'true'}):
        with pytest.raises(ValueError):
            Embedder(config | invalid, tmp_path)


@pytest.mark.parametrize(('provider', 'base_url', 'url', 'request_dimensions'), [
    ('lm_studio', None, 'http://127.0.0.1:1234/v1/embeddings', False),
    ('openai', 'https://example.com/api/v1/', 'https://example.com/api/v1/embeddings', True),
])
def test_openai_wire_format(tmp_path, monkeypatch, provider, base_url, url, request_dimensions):
    config = dict(provider=provider, model='test', dimensions=2)
    if base_url is not None:
        monkeypatch.setenv('TEST_PROVIDER_KEY', 'synthetic-key')
        config.update(base_url=base_url, key_env='TEST_PROVIDER_KEY', request_dimensions=request_dimensions)
        denied = Embedder(config | {'allow_cloud': False}, tmp_path)
        monkeypatch.setattr(denied.http, 'open', lambda *args, **kwargs: pytest.fail('Unexpected HTTP request'))
        with pytest.raises(EmbeddingError, match='allow_cloud'):
            denied.embed('synthetic query', 'query')
        config['allow_cloud'] = True
    embedder = Embedder(config, tmp_path)

    def respond(request, *, timeout):
        assert request.full_url == url
        assert request.get_header('Authorization') == ('Bearer synthetic-key' if base_url else None)
        expected: dict[str, str | int] = {'model': 'test', 'input': 'synthetic query'}
        if request_dimensions:
            expected['dimensions'] = 2
        assert json.loads(request.data) == expected
        return BytesIO(b'{"data":[{"embedding":[3,4]}]}')

    monkeypatch.setattr(embedder.http, 'open', respond)
    assert embedder.embed('synthetic query', 'query') == pytest.approx([0.6, 0.8])
    other = Embedder(config | {'base_url': 'https://other.example.com/v1'}, tmp_path)
    assert other.profile != embedder.profile


@pytest.mark.parametrize('base_url', [
    'http://example.com/v1', 'https://user@example.com/v1', 'https://example.com/v1?token=x',
    'https://example.com/v1#fragment', 'https://example.com:invalid/v1',
])
def test_openai_rejects_unsafe_base_url(tmp_path, base_url):
    with pytest.raises(ValueError):
        Embedder(dict(provider='openai', model='test', dimensions=2, base_url=base_url), tmp_path)


@pytest.mark.parametrize(('mime', 'part'), [
    ('image/png', {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,YWJj'}}),
    ('image/jpeg', {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,YWJj'}}),
    ('audio/wav', {'type': 'input_audio', 'input_audio': {'data': 'data:audio/wav;base64,YWJj', 'format': 'wav'}}),
    ('audio/mpeg', {'type': 'input_audio', 'input_audio': {'data': 'data:audio/mpeg;base64,YWJj', 'format': 'mp3'}}),
    ('video/mp4', {'type': 'input_video', 'input_video': {'data': 'data:video/mp4;base64,YWJj', 'format': 'mp4'}}),
    ('application/pdf', {'type': 'input_file', 'input_file': {
        'data': 'data:application/pdf;base64,YWJj', 'format': 'pdf',
    }}),
])
def test_gemini_attachment_transport(tmp_path, monkeypatch, mime, part):
    monkeypatch.setenv('TEST_PROVIDER_KEY', 'synthetic-key')
    config = dict(provider='openrouter', model='google/gemini-embedding-2', dimensions=2,
                  route='google-vertex/us', key_env='TEST_PROVIDER_KEY')
    denied = Embedder(config, tmp_path)
    monkeypatch.setattr(denied.http, 'open', lambda *args, **kwargs: pytest.fail('Unexpected HTTP request'))
    with pytest.raises(EmbeddingError, match='allow_cloud'):
        denied.embed_attachment(mime, b'abc', 'index')
    embedder = Embedder(config | {'allow_cloud': True}, tmp_path)

    def respond(request, *, timeout):
        body = json.loads(request.data)
        assert body['input'] == [{'content': [part]}]
        assert body['provider']['only'] == ['google-vertex/us']
        return BytesIO(b'{"data":[{"embedding":[3,4]}]}')

    monkeypatch.setattr(embedder.http, 'open', respond)
    assert embedder.embed_attachment(mime, b'abc', 'index') == pytest.approx([0.6, 0.8])
    with pytest.raises(EmbeddingError, match='does not support'):
        embedder.embed_attachment('video/webm', b'abc', 'index')


def test_remote_ollama_uses_native_api(tmp_path, monkeypatch):
    embedder = Embedder(dict(provider='ollama', model='test', dimensions=2, url='https://example.com',
                             allow_cloud=True), tmp_path)

    def respond(request, *, timeout):
        assert request.full_url == 'https://example.com/api/embed'
        assert json.loads(request.data) == {'model': 'test', 'input': 'synthetic note', 'truncate': False}
        return BytesIO(b'{"embeddings":[[3,4]]}')

    monkeypatch.setattr(embedder.http, 'open', respond)
    assert embedder.embed('synthetic note', 'index') == pytest.approx([0.6, 0.8])
