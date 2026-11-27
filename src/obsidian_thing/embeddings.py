import json
import math
import os
import sqlite3
import time
from base64 import b64encode
from contextlib import closing
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


from .metrics import Metrics
from .search_attachments import ATTACHMENT_TYPES, MEDIA_FORMATS, _validate_media


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


def _flag(config, key, default=False):
    value = config.get(key, default)
    if type(value) is not bool:
        raise ValueError(f'Configure {key} as a boolean')
    return value


def _endpoint(value, suffix, *, root_only=False):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 or c == '\\' for c in value):
        raise ValueError('Configure a valid embedding endpoint URL')
    try:
        base = urlsplit(value)
        port = base.port
    except ValueError:
        raise ValueError('Configure a valid embedding endpoint URL') from None
    if (not base.hostname or base.username is not None or '?' in value or '#' in value or port == 0
            or (root_only and base.path not in ('', '/'))):
        raise ValueError('Configure an embedding base URL without credentials, query, or fragment')
    local = base.hostname in ('127.0.0.1', '::1')
    if base.scheme != 'https' and not (local and base.scheme == 'http'):
        raise ValueError('Use HTTPS for an embedding endpoint outside loopback')
    return base.geturl().rstrip('/') + suffix, local


def resolve_endpoint(config: dict) -> tuple[str, bool]:
    provider = config.get('provider')
    if provider == 'ollama':
        return _endpoint(config.get('url', 'http://127.0.0.1:11434'), '/api/embed', root_only=True)
    if provider == 'openrouter':
        return 'https://openrouter.ai/api/v1/embeddings', False
    if provider in ('openai', 'lm_studio'):
        default = 'http://127.0.0.1:1234/v1' if provider == 'lm_studio' else None
        return _endpoint(config.get('base_url', default), '/embeddings')
    raise ValueError('Select ollama, openrouter, openai, or lm_studio for embeddings')


class Embedder:
    def __init__(self, config: dict, state: Path, *, metrics: Metrics | None = None):
        self.metrics = metrics
        provider = config.get('provider')
        if not isinstance(provider, str):
            raise ValueError('Select ollama, openrouter, openai, or lm_studio for embeddings')
        self.provider = provider
        self.model = config.get('model')
        self.dimensions = config.get('dimensions')
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError('Configure an embedding model')
        if type(self.dimensions) is not int or not 1 <= self.dimensions <= 8192:
            raise ValueError('Configure embedding dimensions between 1 and 8192')
        self.rate = _money(config.get('usd_per_million_tokens'))
        if 'usd_per_million_tokens' in config and self.rate is None:
            raise ValueError('Configure a valid USD token rate')
        self.daily_budget = _money(config.get('daily_budget_usd'))
        if 'daily_budget_usd' in config and self.daily_budget is None:
            raise ValueError('Configure a valid daily_budget_usd')
        rates = config.get('usd_per_attachment_unit', {})
        if not isinstance(rates, dict) or any(mime not in ATTACHMENT_TYPES for mime in rates):
            raise ValueError('Configure attachment rates by supported MIME type')
        self.attachment_rates = {mime: _money(value) for mime, value in rates.items()}
        if None in self.attachment_rates.values():
            raise ValueError('Configure valid USD attachment rates')
        allow_cloud = _flag(config, 'allow_cloud')
        self.route = None
        self.routing = None
        self.request_dimensions = False
        self.headers = {'Content-Type': 'application/json'}
        self.url, self.local = resolve_endpoint(config)
        self.can_send = self.local or allow_cloud
        if self.provider == 'openrouter':
            self.route = config.get('route')
            if not isinstance(self.route, str) or not self.route.strip():
                raise ValueError('Configure one OpenRouter route')
            zdr = _flag(config, 'zdr', True)
            data_collection = config.get('data_collection', 'deny')
            if data_collection not in ('allow', 'deny'):
                raise ValueError('Configure data_collection as allow or deny')
            self.routing = {'only': [self.route], 'allow_fallbacks': False,
                            'zdr': zdr, 'data_collection': data_collection}
            self.request_dimensions = True
        elif self.provider in ('openai', 'lm_studio'):
            self.request_dimensions = _flag(config, 'request_dimensions')
        if self.provider == 'openrouter' or 'key_env' in config:
            key_env = config.get('key_env')
            key = os.environ.get(key_env) if isinstance(key_env, str) else None
            if not key or not key.isascii() or not key.isprintable() or ' ' in key:
                raise ValueError('Set the embedding key environment variable')
            self.headers['Authorization'] = 'Bearer ' + key
        identity = [self.provider, self.url, self.model, self.route, self.dimensions, config.get('profile_version')]
        if self.routing is not None and (not self.routing['zdr'] or self.routing['data_collection'] != 'deny'):
            identity.append(self.routing)
        if self.routing is None and self.request_dimensions:
            identity.append(self.request_dimensions)
        self.profile = sha256(json.dumps(identity).encode()).hexdigest()
        self.http = build_opener(ProxyHandler({}), NoRedirect())
        self.journal = state / 'spend.sqlite3'
        with closing(sqlite3.connect(self.journal)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                operation TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, route TEXT,
                request_id TEXT, input_tokens INTEGER, latency_ms INTEGER NOT NULL,
                status TEXT NOT NULL, cost_kind TEXT NOT NULL, usd TEXT
            )''')

    def _reserve(self, operation: str, estimate: Decimal | None) -> int | None:
        if self.local or self.daily_budget is None:
            return None
        if estimate is None:
            raise EmbeddingError('The daily budget requires a cost estimate before sending input')
        with closing(sqlite3.connect(self.journal)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            at, = db.execute('SELECT CURRENT_TIMESTAMP').fetchone()
            total = estimate
            for (usd,) in db.execute("SELECT usd FROM calls WHERE at >= ? AND cost_kind != 'local'", (at[:10],)):
                if usd is None:
                    raise EmbeddingError('The daily budget cannot account for a call with unknown cost')
                total += Decimal(usd)
            if total > self.daily_budget:
                raise EmbeddingError('The request would exceed the daily embedding budget')
            cursor = db.execute('''INSERT INTO calls
                (at, operation, provider, model, route, latency_ms, status, cost_kind, usd)
                VALUES (?, ?, ?, ?, ?, 0, 'pending', 'estimated', ?)''',
                (at, operation, self.provider, self.model, self.route, str(estimate)))
            return cursor.lastrowid

    def _record(self, operation, status, start, result, estimate, reservation, *, text_input):
        usage = result.get('usage')
        if not isinstance(usage, dict):
            usage = {}
        tokens = usage.get('prompt_tokens', result.get('prompt_eval_count'))
        if type(tokens) is not int or not 0 <= tokens < 2**63:
            tokens = None
        cost = _money(usage.get('cost'))
        if self.local:
            kind, cost = 'local', None
        elif cost is not None:
            kind = 'reported'
        elif text_input and self.rate is not None and tokens is not None:
            kind, cost = 'estimated', self.rate * tokens / 1_000_000
        elif estimate is not None:
            kind, cost = 'estimated', estimate
        else:
            kind = 'unknown'
        request_id = result.get('id')
        elapsed = time.monotonic() - start
        values = (request_id[:256] if isinstance(request_id, str) else None, tokens,
                  round(elapsed * 1000), status, kind, str(cost) if cost is not None else None)
        with closing(sqlite3.connect(self.journal)) as db, db:
            if reservation is None:
                db.execute('''INSERT INTO calls
                    (operation, provider, model, route, request_id, input_tokens, latency_ms, status, cost_kind, usd)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (operation, self.provider, self.model, self.route, *values))
            else:
                db.execute('''UPDATE calls SET request_id = ?, input_tokens = ?, latency_ms = ?,
                    status = ?, cost_kind = ?, usd = ? WHERE id = ?''', (*values, reservation))
        if self.metrics is not None:
            self.metrics.record_provider(operation, self.provider, status, elapsed, kind, cost)

    def supports_attachment(self, mime: str) -> bool:
        return (self.provider == 'openrouter' and self.model == 'google/gemini-embedding-2'
                and self.route in ('google-vertex/us', 'google-vertex/global')
                and mime in ATTACHMENT_TYPES)

    def embed_attachment(self, mime: str, data: bytes, operation: str) -> list[float]:
        if not self.supports_attachment(mime):
            raise EmbeddingError('The configured embedding route does not support this attachment type')
        rate = self.attachment_rates.get(mime)
        units = (1 if mime not in MEDIA_FORMATS else _validate_media(mime, data)) if rate is not None else None
        estimate = rate * Decimal(str(units)) if rate is not None and units is not None else None
        uri = f'data:{mime};base64,' + b64encode(data).decode('ascii')
        if mime in MEDIA_FORMATS:
            kind = 'input_audio' if mime.startswith('audio/') else 'input_video'
            part = {'type': kind, kind: {'data': uri, 'format': MEDIA_FORMATS[mime][0]}}
        elif mime == 'application/pdf':
            part = {'type': 'input_file', 'input_file': {'data': uri, 'format': 'pdf'}}
        else:
            part = {'type': 'image_url', 'image_url': {'url': uri}}
        return self._embed([{'content': [part]}], operation, estimate)

    def embed(self, text: str, operation: str) -> list[float]:
        estimate = None if self.daily_budget is None or self.rate is None else self.rate * len(text.encode()) / 1_000_000
        return self._embed(text, operation, estimate)

    def _embed(self, value: str | list[dict], operation: str, estimate: Decimal | None) -> list[float]:
        if not self.can_send:
            raise EmbeddingError('Set allow_cloud = true to send input to a remote embedding endpoint')
        body = {'model': self.model, 'input': value}
        if self.provider == 'ollama':
            body['truncate'] = False
        if self.request_dimensions:
            body['dimensions'] = self.dimensions
        if self.routing is not None:
            body['provider'] = self.routing
        req = Request(self.url, data=json.dumps(body).encode(), headers=self.headers)
        reservation = self._reserve(operation, estimate)
        start = time.monotonic()
        result = {}
        status = 'error'
        try:
            with self.http.open(req, timeout=5 if operation == 'background' else 30) as response:
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
            self._record(operation, status, start, result, estimate, reservation, text_input=isinstance(value, str))
