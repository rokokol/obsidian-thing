import json
import math
import os
import sqlite3
import time
from contextlib import closing
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class EmbeddingError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _money(value):
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and 0 <= amount <= 1_000_000:
            return amount
    except InvalidOperation:
        pass
    return None


class Embedder:
    def __init__(self, config: dict, state: Path):
        self.provider = config.get('provider')
        self.model = config.get('model')
        self.dimensions = config.get('dimensions')
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError('Configure an embedding model')
        if type(self.dimensions) is not int or not 1 <= self.dimensions <= 8192:
            raise ValueError('Configure embedding dimensions between 1 and 8192')
        self.rate = _money(config.get('usd_per_million_tokens'))
        if 'usd_per_million_tokens' in config and self.rate is None:
            raise ValueError('Configure a valid USD token rate')
        self.route = None
        self.headers = {'Content-Type': 'application/json'}
        if self.provider == 'ollama':
            base = urlsplit(config.get('url', 'http://127.0.0.1:11434'))
            if (base.scheme != 'http' or base.hostname not in ('127.0.0.1', '::1')
                    or base.username is not None or base.path not in ('', '/') or base.query or base.fragment):
                raise ValueError('Use a loopback HTTP URL for Ollama')
            self.url = base.geturl().rstrip('/') + '/api/embed'
        elif self.provider == 'openrouter':
            if config.get('allow_cloud') is not True:
                raise ValueError('Set allow_cloud = true to send input to OpenRouter')
            self.route = config.get('route')
            if not isinstance(self.route, str) or not self.route.strip():
                raise ValueError('Configure one OpenRouter route')
            key_env = config.get('key_env')
            key = os.environ.get(key_env) if isinstance(key_env, str) else None
            if not key or not key.isascii() or not key.isprintable() or ' ' in key:
                raise ValueError('Set the OpenRouter key environment variable')
            self.headers['Authorization'] = 'Bearer ' + key
            self.url = 'https://openrouter.ai/api/v1/embeddings'
        else:
            raise ValueError('Select ollama or openrouter for embeddings')
        self.profile = sha256(json.dumps([
            self.provider, self.url, self.model, self.route, self.dimensions, config.get('profile_version'),
        ]).encode()).hexdigest()
        self.http = build_opener(ProxyHandler({}), NoRedirect())
        self.journal = state / 'spend.sqlite3'
        with closing(sqlite3.connect(self.journal)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                operation TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, route TEXT,
                request_id TEXT, input_tokens INTEGER, latency_ms INTEGER NOT NULL,
                status TEXT NOT NULL, cost_kind TEXT NOT NULL, usd TEXT
            )''')

    def _record(self, operation, status, start, result):
        usage = result.get('usage')
        if not isinstance(usage, dict):
            usage = {}
        tokens = usage.get('prompt_tokens', result.get('prompt_eval_count'))
        if type(tokens) is not int or not 0 <= tokens < 2**63:
            tokens = None
        cost = _money(usage.get('cost'))
        if self.provider == 'ollama':
            kind, cost = 'local', None
        elif cost is not None:
            kind = 'reported'
        elif self.rate is not None and tokens is not None:
            kind, cost = 'estimated', self.rate * tokens / 1_000_000
        else:
            kind = 'unknown'
        request_id = result.get('id')
        with closing(sqlite3.connect(self.journal)) as db, db:
            db.execute('''INSERT INTO calls
                (operation, provider, model, route, request_id, input_tokens, latency_ms, status, cost_kind, usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
                operation, self.provider, self.model, self.route,
                request_id[:256] if isinstance(request_id, str) else None, tokens,
                round((time.monotonic() - start) * 1000), status, kind, str(cost) if cost is not None else None,
            ))

    def embed(self, text: str, operation: str) -> list[float]:
        body = {'model': self.model, 'input': text}
        if self.provider == 'ollama':
            body['truncate'] = False
        else:
            body['dimensions'] = self.dimensions
            body['provider'] = {
                'only': [self.route], 'allow_fallbacks': False, 'zdr': True, 'data_collection': 'deny',
            }
        req = Request(self.url, data=json.dumps(body).encode(), headers=self.headers)
        start = time.monotonic()
        result = {}
        status = 'error'
        try:
            with self.http.open(req, timeout=30) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError('Response exceeds the limit')
            response_json = json.loads(raw, parse_float=Decimal)
            if not isinstance(response_json, dict):
                raise ValueError('Expected an object')
            result = response_json
            if self.provider == 'ollama':
                vector, = result['embeddings']
            else:
                item, = result['data']
                vector = item['embedding']
            if (not isinstance(vector, list) or len(vector) != self.dimensions
                    or any(type(v) not in (int, Decimal) for v in vector)):
                raise ValueError('Invalid vector')
            length = math.hypot(*vector)
            if not length or not math.isfinite(length):
                raise ValueError('Invalid vector norm')
            normalized = [float(v) / length for v in vector]
            status = 'ok'
            return normalized
        except (OSError, HTTPException, ValueError, TypeError, KeyError, OverflowError):
            raise EmbeddingError('Embedding provider request failed or returned an invalid vector') from None
        finally:
            self._record(operation, status, start, result)
