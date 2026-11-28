import json
from io import BytesIO
from urllib.request import OpenerDirector

import pytest
from prometheus_client import generate_latest
from starlette.testclient import TestClient

from obsidian_thing.index_watcher import IndexWatcher
from obsidian_thing.metrics import Metrics
from obsidian_thing.search import SearchIndex
from obsidian_thing.server import create_app
from obsidian_thing.vault import Vault


def test_metrics_export_is_opt_in_and_separately_authenticated(tmp_path, monkeypatch):
    monkeypatch.setenv('VAULT_TOKEN', 'vault-test-token')
    monkeypatch.setenv('METRICS_TOKEN', 'metrics-test-token')
    vault = {'root': str(tmp_path), 'allowed': ['.'], 'token_env': 'VAULT_TOKEN'}
    config = {'vaults': {'private-vault': vault}}
    with TestClient(create_app(config)) as client:
        assert client.get('/metrics').status_code == 404
    with TestClient(create_app(config | {'metrics_token_env': 'METRICS_TOKEN'})) as client:
        assert client.get('/metrics').status_code == 401
        assert client.get('/metrics', headers={'Authorization': 'Bearer vault-test-token'}).status_code == 401
        response = client.get('/metrics', headers={'Authorization': 'Bearer metrics-test-token'})
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/plain')
        assert client.get('/vaults/private-vault/mcp', headers={'Authorization': 'Bearer metrics-test-token'}).status_code == 401
    with pytest.raises(ValueError, match='different bearer token'):
        create_app(config | {'metrics_token_env': 'VAULT_TOKEN'})


def test_metrics_count_provider_calls_cache_and_mcp_errors(tmp_path, monkeypatch):
    monkeypatch.setenv('VAULT_TOKEN', 'vault-test-token')
    monkeypatch.setenv('METRICS_TOKEN', 'metrics-test-token')
    vault = tmp_path / 'vault'
    vault.mkdir()
    (vault / 'private-path.md').write_text('private text')
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    config = {'metrics_token_env': 'METRICS_TOKEN', 'vaults': {'private-vault': {
        'root': str(vault), 'allowed': ['.'], 'token_env': 'VAULT_TOKEN',
        'embeddings': {'provider': 'openai', 'base_url': 'https://provider.example/v1', 'model': 'private-model',
                       'state_dir': str(state), 'allow_cloud': True, 'include_paths': ['.'], 'dimensions': 2, 'min_chunk_words': 0},
    }}}
    replies = iter([{'id': 'private-id', 'data': [{'embedding': [1, 0]}], 'usage': {'cost': 0.25}}, None,
                    {'data': [{'embedding': [1, 0]}]}])

    def respond(*args, **kwargs):
        result = next(replies)
        if result is None:
            raise TimeoutError
        return BytesIO(json.dumps(result).encode())

    app = create_app(config)
    monkeypatch.setattr(OpenerDirector, 'open', respond)
    with TestClient(app, base_url='http://localhost') as client:
        def call(method, params):
            response = client.post('/vaults/private-vault/mcp', headers={
                'Authorization': 'Bearer vault-test-token', 'Accept': 'application/json, text/event-stream',
                'MCP-Protocol-Version': '2025-11-25',
            }, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
            assert response.status_code == 200
            return response.json()

        for _ in range(2):
            call('tools/call', {'name': 'obsidian.index', 'arguments': {'path': 'private-path.md'}})
        for _ in range(2):
            call('tools/call', {'name': 'obsidian.query', 'arguments': {'text': 'another query'}})
        assert call('tools/call', {'name': 'private-tool', 'arguments': {}})['result']['isError']
        assert 'error' in call('private-method', {})
        response = client.get('/metrics', headers={'Authorization': 'Bearer metrics-test-token'})
        assert response.status_code == 200
        metrics = response.text
    assert 'obsidian_thing_mcp_requests_total{method="tools/call",status="ok"} 4.0' in metrics
    assert 'obsidian_thing_mcp_requests_total{method="tools/call",status="error"} 1.0' in metrics
    assert 'obsidian_thing_embedding_requests_total{operation="query",provider="openai",status="error"} 1.0' in metrics
    assert 'obsidian_thing_embedding_cost_records_total{kind="unknown"} 2.0' in metrics
    assert 'obsidian_thing_embedding_cost_usd_total{kind="reported"} 0.25' in metrics
    assert 'obsidian_thing_embedding_cache_total{result="hit"} 1.0' in metrics
    assert 'obsidian_thing_embedding_cache_total{result="miss"} 1.0' in metrics
    assert 'obsidian_thing_embedding_queue_files 0.0' in metrics
    assert 'obsidian_thing_mcp_requests_total{method="other",status="error"} 1.0' in metrics
    assert 'obsidian_thing_mcp_request_duration_seconds_count{method="tools/call"} 5.0' in metrics
    assert 'obsidian_thing_embedding_request_duration_seconds_count{operation="query",provider="openai"} 2.0' in metrics
    for secret in ('private-vault', 'private-path', 'private text', 'private-model', 'private-id', 'private-tool', 'private-method'):
        assert secret not in metrics


def test_queue_snapshot_and_full_scan_timestamp(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'queued.md').write_text('synthetic content')
    state = tmp_path / 'state'
    state.mkdir()
    metrics = Metrics()
    search = SearchIndex(Vault(root, ['.']), dict(provider='ollama', model='test', dimensions=2, min_chunk_words=0), state, metrics=metrics)
    metrics.queue_sources.append(search.queue_depth)
    watcher = IndexWatcher(search)
    registry = metrics.registry
    name = 'obsidian_thing_last_full_index_scan_timestamp_seconds'
    watcher._reconcile({'queued.md'})
    assert registry.get_sample_value(name) == 0
    assert b'obsidian_thing_embedding_queue_files 1.0' in generate_latest(registry)
    watcher._reconcile()
    last_scan = registry.get_sample_value(name)
    assert last_scan is not None and last_scan > 0

    def fail(*args, **kwargs):
        raise OSError

    monkeypatch.setattr(search, 'reconcile_local', fail)
    watcher._reconcile()
    assert registry.get_sample_value(name) == last_scan
    assert registry.get_sample_value('obsidian_thing_index_scan_errors_total', {'scope': 'full'}) == 1
