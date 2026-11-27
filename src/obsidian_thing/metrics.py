from collections.abc import Callable
from decimal import Decimal
from time import monotonic
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


_METHODS = frozenset(('initialize', 'ping', 'tools/list', 'tools/call'))
_OPERATIONS = frozenset(('index', 'related', 'query', 'background'))
_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 15, 30, 60)


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.queue_sources: list[Callable[[], int]] = []

        def counter(name: str, help_text: str, *labels: str) -> Counter:
            return Counter('obsidian_thing_' + name, help_text, labels, registry=self.registry)

        def histogram(name: str, help_text: str, *labels: str) -> Histogram:
            return Histogram('obsidian_thing_' + name, help_text, labels, registry=self.registry, buckets=_BUCKETS)

        self.mcp_requests = counter('mcp_requests', 'MCP requests by method and outcome.', 'method', 'status')
        self.mcp_latency = histogram('mcp_request_duration_seconds', 'MCP request duration.', 'method')
        self.provider_requests = counter('embedding_requests', 'Embedding provider calls by outcome.',
                                         'operation', 'provider', 'status')
        self.provider_latency = histogram('embedding_request_duration_seconds', 'Embedding provider call duration.',
                                          'operation', 'provider')
        self.cost_records = counter('embedding_cost_records', 'Completed calls by cost source.', 'kind')
        self.cost_usd = counter('embedding_cost_usd', 'Cost of completed calls in USD by cost source.', 'kind')
        for kind in ('local', 'reported', 'estimated', 'unknown'):
            self.cost_records.labels(kind)
        for kind in ('reported', 'estimated'):
            self.cost_usd.labels(kind)
        self.cache = counter('embedding_cache', 'Vector cache lookups during indexing.', 'result')
        for result in ('hit', 'miss'):
            self.cache.labels(result)
        self.queue_files = Gauge('obsidian_thing_embedding_queue_files', 'Files pending according to the index snapshot.',
                                 registry=self.registry)
        self.queue_files.set_function(lambda: sum(source() for source in self.queue_sources))
        self.last_scan = Gauge('obsidian_thing_last_full_index_scan_timestamp_seconds',
                               'Unix time of the latest successful full index scan.', registry=self.registry)
        self.scan_errors = counter('index_scan_errors', 'Failed index scans.', 'scope')

    async def observe_mcp(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        if ctx.request_id is None:
            return await call_next(ctx)
        method = ctx.method if ctx.method in _METHODS else 'other'
        started = monotonic()
        status = 'error'
        try:
            result = await call_next(ctx)
            failed = result.get('isError') if isinstance(result, dict) else getattr(result, 'is_error', False)
            if not failed:
                status = 'ok'
            return result
        finally:
            self.mcp_requests.labels(method, status).inc()
            self.mcp_latency.labels(method).observe(monotonic() - started)

    def record_provider(self, operation: str, provider: str, status: str, elapsed: float,
                        kind: str, cost: Decimal | None) -> None:
        operation = operation if operation in _OPERATIONS else 'other'
        self.provider_requests.labels(operation, provider, status).inc()
        self.provider_latency.labels(operation, provider).observe(elapsed)
        self.cost_records.labels(kind).inc()
        if cost is not None:
            self.cost_usd.labels(kind).inc(float(cost))
