import json
import sqlite3
from decimal import Decimal
from io import BytesIO

import pytest

from obsidian_thing.embeddings import Embedder, EmbeddingError


def test_cloud_requires_opt_in_and_pins_privacy_policy(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_PROVIDER_KEY', 'synthetic-key')
    config = dict(provider='openrouter', model='test/model', route='test-route',
                  dimensions=2, key_env='TEST_PROVIDER_KEY', usd_per_million_tokens='0.4')
    with pytest.raises(ValueError, match='allow_cloud'):
        Embedder(config, tmp_path)
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
    for _ in range(3):
        assert embedder.embed('synthetic note', 'index') == pytest.approx([0.6, 0.8])
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
    with pytest.raises(ValueError, match='loopback'):
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
