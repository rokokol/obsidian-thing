import sqlite3
from decimal import Decimal

from obsidian_thing.embeddings import Embedder


def test_spend_report_excludes_open_reservations_but_keeps_failed_calls(tmp_path):
    embedder = Embedder(dict(provider='ollama', model='synthetic', dimensions=2), tmp_path)
    with sqlite3.connect(embedder.journal) as db:
        db.executemany('''INSERT INTO calls (operation, provider, model, latency_ms, status, cost_kind, usd)
                       VALUES ('query', 'synthetic', 'synthetic', 0, ?, ?, ?)''', [
            ('pending', 'estimated', '99'),
            ('error', 'estimated', '0.000000000000000001'),
            ('ok', 'reported', '0.000000000000000003'),
            ('error', 'unknown', None),
        ])

    report = embedder.spend_report()
    assert len(report['daily']) == 1
    for row in [*report['periods'], *report['daily']]:
        assert row['calls'] == 3
        assert row['unknown_calls'] == 1
        assert Decimal(row['estimated']) == Decimal('0.000000000000000001')
        assert Decimal(row['reported']) == Decimal('0.000000000000000003')
