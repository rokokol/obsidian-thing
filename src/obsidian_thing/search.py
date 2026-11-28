import fcntl
import json
import math
import os
import re
import sqlite3
import stat as stat_module
from collections.abc import Callable, Iterable, Sequence, Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from itertools import islice
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import NotRequired, TypedDict

import numpy as np
import regex

from .embeddings import Embedder, EmbeddingDescription, EmbeddingError, SpendReport
from .index_policy import ExclusionError, IndexPolicy
from .metrics import Metrics
from .projection import project

_VECTOR = np.dtype('<f4')
from .search_attachments import ATTACHMENT_LIMIT, SUFFIX_TYPES, TEXT_TYPES, attachment_parts, mime_type
from .search_chunks import Chunk, chunks, section_lines
from .search_metadata import fold, metadata_rows, tag_key
from .vault import TextFile, Vault, VaultError, _parts


class SectionMatch(TypedDict):
    score: float
    headings: NotRequired[list[str]]
    line_start: NotRequired[int]
    line_end: NotRequired[int]
    page: NotRequired[int]


class Match(SectionMatch):
    path: str
    sections: NotRequired[list[SectionMatch]]


class Matches(TypedDict):
    matches: list[Match]
    vector_unavailable: NotRequired[bool]


class MapPoint(TypedDict):
    path: str
    x: float
    y: float
    score: float
    source: NotRequired[bool]


class Map(TypedDict):
    points: list[MapPoint]
    explained: list[float]
    dimensions: int


# Coverage counts describe one successful full reconciliation. They stay null until
# one completes for the current split and embedding profile.
class IndexStatus(TypedDict):
    mode: str
    files: int | None
    excluded: int | None
    indexed: int | None
    unindexed: int | None
    embedded: int | None
    pending: int
    last_full_scan: str | None


class EmbeddingStatus(EmbeddingDescription):
    min_chunk_words: int
    related_min_score: float
    allowed_mime_types: list[str]


class Spend(SpendReport):
    pass


class Status(TypedDict):
    index: IndexStatus
    embeddings: NotRequired[EmbeddingStatus]
    spend: NotRequired[Spend]


def _pack(vector: list[float]) -> bytes:
    return np.asarray(vector, dtype=_VECTOR).tobytes()


def _unpack(value: bytes | str) -> np.ndarray:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return np.frombuffer(value, dtype=_VECTOR)
    return np.asarray(json.loads(value), dtype=_VECTOR)


def _attachment_revision(stamp: str, data: bytes) -> str:
    return f'{stamp}:{sha256(data).hexdigest()}'


def _scope(config: dict, key: str) -> list[tuple[str, ...]]:
    try:
        return _path_prefixes(config.get(key, []))
    except VaultError:
        raise ValueError(f'Configure embedding {key} as a list of vault-relative paths') from None


def _path_prefixes(paths: list[str]) -> list[tuple[str, ...]]:
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise VaultError('Use a list of vault-relative paths')
    return [() if path == '.' else _parts(path) for path in paths]


def _merge_ranks(*rankings: list[Match]) -> list[Match]:
    scores: dict[str, float] = {}
    best: dict[str, tuple[int, Match]] = {}
    normalization = sum(bool(items) for items in rankings) or 1
    for items in rankings:
        for rank, hit in enumerate(items, 1):
            path = hit['path']
            scores[path] = scores.get(path, 0.0) + 61 / (60 + rank)
            if path not in best or rank < best[path][0]:
                best[path] = rank, hit
    return [{**best[path][1], 'score': scores[path] / normalization}
            for path in sorted(scores, key=lambda path: (-scores[path], path))]


class SearchIndex:
    max_results = 100
    chunk_size = 1500
    max_chunks = 32

    def __init__(self, vault: Vault, config: dict, state: Path, *, indexing: object = None, vectors: bool = True,
                 metrics: Metrics | None = None):
        self.metrics = metrics
        self.vault = vault
        self.policy = indexing if isinstance(indexing, IndexPolicy) else IndexPolicy(vault, {} if indexing is None else indexing)
        self.include = _scope(config, 'include_paths') if vectors else []
        self.exclude = _scope(config, 'exclude_paths') if vectors else []
        self.section_chunks = config.get('section_chunks', True)
        if not isinstance(self.section_chunks, bool):
            raise ValueError('Configure embedding section_chunks as a boolean')
        words = config.get('min_chunk_words', 24)
        if type(words) is not int or not 0 <= words <= 1000:
            raise ValueError('Configure embedding min_chunk_words as an integer from 0 to 1000')
        self.min_chunk_words: int = words
        threshold = config.get('related_min_score', 0.0) if vectors else 0.0
        if type(threshold) not in (int, float) or not -1 <= threshold <= 1:
            raise ValueError('Configure related_min_score between -1 and 1')
        self.related_min_score: float = threshold
        self.embedder = Embedder(config, state, metrics=metrics) if vectors else None
        types = config.get('allowed_mime_types', list(TEXT_TYPES)) if vectors else []
        if not isinstance(types, list) or not all(isinstance(t, str) and t in SUFFIX_TYPES.values() for t in types):
            raise ValueError('Configure allowed_mime_types as a list of supported MIME types')
        self.allowed_mime_types = frozenset(types)
        if self.embedder and any(not self.embedder.supports_attachment(t) for t in self.allowed_mime_types - TEXT_TYPES):
            raise ValueError('The configured embedding route does not support an allowed attachment type')
        if self.embedder and not self.embedder.local and self.embedder.can_send and not self.include:
            raise ValueError('Configure include_paths before you send files to a remote embedding endpoint')
        self.database = state / 'search.sqlite3'
        self.write_lock_path = state / 'search-write.lock'
        self.embedding_lock_path = state / 'embedding.lock'
        self.run_lock_path = state / 'embedding-run.lock'
        self.index_lock = Lock()
        self.local_lock = Lock()
        with closing(sqlite3.connect(self.database)) as db, self._write(db):
            db.execute('PRAGMA journal_mode = WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS vectors (
                    profile TEXT, hash TEXT, vector BLOB NOT NULL, PRIMARY KEY (profile, hash)
                );
                CREATE TABLE IF NOT EXISTS section_chunks (
                    profile TEXT, mode INTEGER, path TEXT, revision TEXT NOT NULL, hash TEXT NOT NULL,
                    ordinal INTEGER, headings TEXT NOT NULL, line_start INTEGER, line_end INTEGER,
                    PRIMARY KEY (profile, mode, path, ordinal)
                );
                CREATE TABLE IF NOT EXISTS local_files (
                    mode INTEGER NOT NULL, path TEXT NOT NULL, revision TEXT NOT NULL,
                    PRIMARY KEY (mode, path)
                );
                CREATE TABLE IF NOT EXISTS embedded_files (
                    profile TEXT NOT NULL, mode INTEGER NOT NULL, path TEXT NOT NULL, revision TEXT NOT NULL,
                    PRIMARY KEY (profile, mode, path)
                );
                CREATE TABLE IF NOT EXISTS coverage (
                    mode INTEGER NOT NULL, profile TEXT NOT NULL,
                    files INTEGER NOT NULL, excluded INTEGER NOT NULL,
                    indexed INTEGER NOT NULL, embedded INTEGER NOT NULL,
                    at TEXT NOT NULL, PRIMARY KEY (mode, profile)
                );
                CREATE TABLE IF NOT EXISTS lexical_chunks (
                    id INTEGER PRIMARY KEY, mode INTEGER NOT NULL, path TEXT NOT NULL,
                    revision TEXT NOT NULL, headings TEXT NOT NULL, line_start INTEGER NOT NULL,
                    line_end INTEGER NOT NULL, text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS lexical_paths ON lexical_chunks (mode, path);
                CREATE TABLE IF NOT EXISTS note_metadata (
                    mode INTEGER NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,
                    name TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY (mode, path, kind, name, value)
                );
                CREATE INDEX IF NOT EXISTS metadata_lookup ON note_metadata (mode, kind, name, value, path);
                CREATE VIRTUAL TABLE IF NOT EXISTS lexical_fts USING fts5(
                    text, content='lexical_chunks', content_rowid='id', tokenize='unicode61 tokenchars _'
                );
                CREATE TRIGGER IF NOT EXISTS lexical_insert AFTER INSERT ON lexical_chunks BEGIN
                    INSERT INTO lexical_fts (rowid, text) VALUES (new.id, new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS lexical_delete AFTER DELETE ON lexical_chunks BEGIN
                    INSERT INTO lexical_fts (lexical_fts, rowid, text) VALUES ('delete', old.id, old.text);
                END;
            ''')
            self._pack_stored_vectors(db)

    def _pack_stored_vectors(self, db: sqlite3.Connection) -> None:
        # Vectors written before this format are JSON text; convert in batches so a large cache
        # does not load at once, and drop a row that cannot be read because the cache refills itself
        while True:
            rows = db.execute("SELECT rowid, vector FROM vectors WHERE typeof(vector) = 'text' "
                              "LIMIT 512").fetchall()
            if not rows:
                return
            packed = []
            broken = []
            for rowid, value in rows:
                try:
                    packed.append((sqlite3.Binary(_pack(json.loads(value))), rowid))
                except (TypeError, ValueError):
                    broken.append((rowid,))
            # Startup runs before the server serves a request, so the write lease is not needed
            with db:
                db.executemany('UPDATE vectors SET vector = ? WHERE rowid = ?', packed)
                db.executemany('DELETE FROM vectors WHERE rowid = ?', broken)

    @contextmanager
    def _write(self, db: sqlite3.Connection) -> Iterator[None]:
        with self.write_lock_path.open('ab') as lease, db:
            fcntl.flock(lease, fcntl.LOCK_EX)
            yield

    @staticmethod
    def _supports_text(path: str) -> bool:
        return mime_type(path) in TEXT_TYPES

    def _check_local_path(self, path: str) -> None:
        self.vault._allowed_parts(path)
        self.policy.check(path)
        if not self._supports_text(path):
            raise VaultError('Only Markdown and plain text paths can use this index')

    def _check_path(self, path: str) -> str:
        self.vault._allowed_parts(path)
        self.policy.check(path)
        mime = mime_type(path)
        if mime is None or mime not in self.allowed_mime_types:
            raise VaultError('This file type is not enabled for embeddings')
        parts = _parts(path)
        if self.include and not any(parts[:len(prefix)] == prefix for prefix in self.include):
            raise VaultError('The path is outside the embedding scope')
        if any(parts[:len(prefix)] == prefix for prefix in self.exclude):
            raise VaultError('The path is excluded from embeddings')
        return mime

    def _index_text(self, db: sqlite3.Connection, path: str,
                    queued_revision: str | None) -> tuple[str, list[Chunk]]:
        note = self.vault.read(path)
        self.policy.check_note(note)
        revision = note['revision']
        if queued_revision is not None and revision != queued_revision:
            raise VaultError('The queued revision is no longer current')
        fragments = self._fragments(note)
        with self.local_lock:
            self._index_lexical(db, note, fragments)
        return revision, fragments

    def index(self, path: str, *, operation: str = 'index', queued_revision: str | None = None,
              stop: Callable[[], bool] | None = None) -> dict[str, str | int]:
        embedder = self.embedder
        if embedder is None:
            self._check_local_path(path)
            with closing(sqlite3.connect(self.database)) as db:
                revision, fragments = self._index_text(db, path, queued_revision)
            return {'path': path, 'revision': revision, 'chunks': len(fragments)}
        mime = self._check_path(path)
        if mime not in TEXT_TYPES and not embedder.can_send:
            raise EmbeddingError('Set allow_cloud = true to send input to a remote embedding endpoint')
        profile = embedder.profile
        with self.index_lock, self.embedding_lock_path.open('ab') as lease, closing(sqlite3.connect(self.database)) as db:
            fcntl.flock(lease, fcntl.LOCK_EX)
            if mime in TEXT_TYPES:
                revision, fragments = self._index_text(db, path, queued_revision)
                embedded = [chunk for chunk in fragments if chunk.words >= self.min_chunk_words]
                payloads = [chunk.text for chunk in embedded]
                locations = [(json.dumps(c.headings), c.line_start, c.line_end) for c in embedded]
            else:
                stamp, data = self.vault.read_attachment(path, ATTACHMENT_LIMIT)
                revision = _attachment_revision(stamp, data)
                if queued_revision is not None and revision != queued_revision:
                    raise VaultError('The queued revision is no longer current')
                payloads = list(attachment_parts(mime, data))
                locations = [('[]', None, None)] * len(payloads)
            digests = [sha256(p.encode() if isinstance(p, str) else f'attachment-v1:{mime}:'.encode() + p).hexdigest()
                       for p in payloads]
            for digest, payload in dict(zip(digests, payloads, strict=True)).items():
                if stop is not None and stop():
                    raise VaultError('Background indexing stopped')
                cached = db.execute('SELECT 1 FROM vectors WHERE profile = ? AND hash = ?',
                                    (profile, digest)).fetchone() is not None
                if self.metrics is not None:
                    self.metrics.cache.labels('hit' if cached else 'miss').inc()
                if not cached:
                    if not self._is_current(path, revision):
                        raise VaultError('The file changed or left the index scope during indexing')
                    vector = (embedder.embed(payload, operation) if isinstance(payload, str)
                              else embedder.embed_attachment(mime, payload, operation))
                    with self._write(db):
                        db.execute('INSERT OR IGNORE INTO vectors VALUES (?, ?, ?)',
                                   (profile, digest, _pack(vector)))
            with self.local_lock, self._write(db):
                if not self._is_current(path, revision):
                    raise VaultError('The file changed during indexing; try again')
                db.execute('DELETE FROM section_chunks WHERE profile = ? AND mode = ? AND path = ?',
                           (profile, self.section_chunks, path))
                db.executemany('INSERT INTO section_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (
                    (profile, self.section_chunks, path, revision, digest, ordinal, *location)
                    for ordinal, (digest, location) in enumerate(zip(digests, locations, strict=True))
                ))
                if mime not in TEXT_TYPES:
                    db.execute('INSERT OR REPLACE INTO local_files VALUES (?, ?, ?)',
                               (self.section_chunks, path, revision))
                db.execute('INSERT OR REPLACE INTO embedded_files VALUES (?, ?, ?, ?)',
                           (profile, self.section_chunks, path, revision))
        return {'path': path, 'revision': revision, 'chunks': len(payloads)}

    _pending_rows = '''
        SELECT l.path, l.revision FROM local_files AS l
        WHERE l.mode = ? AND NOT EXISTS (
            SELECT 1 FROM embedded_files AS e WHERE e.profile = ? AND e.mode = l.mode
            AND e.path = l.path AND e.revision = l.revision
        )
    '''

    def queue_depth(self) -> int:
        if self.embedder is None or not self.embedder.can_send:
            return 0
        with closing(sqlite3.connect(self.database)) as db:
            return self._queue_depth(db)

    def pending(self) -> list[tuple[str, str]]:
        if self.embedder is None or not self.embedder.can_send:
            return []
        with closing(sqlite3.connect(self.database)) as db:
            rows = db.execute(self._pending_rows + ' ORDER BY l.path',
                              (self.section_chunks, self.embedder.profile)).fetchall()
        return [(path, revision) for path, revision in rows if self._is_current(path, revision)]

    def run_pending(self, *, stop: Callable[[], bool] | None = None) -> bool:
        if self.embedder is None:
            return True
        with self.run_lock_path.open('ab') as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            for path, revision in self.pending():
                if stop is not None and stop():
                    return False
                try:
                    self.index(path, operation='background', queued_revision=revision, stop=stop)
                except VaultError:
                    continue
                except EmbeddingError:
                    return False
            return True

    def _fragments(self, note: TextFile) -> list[Chunk]:
        fragments = list(islice(chunks(note['content'], self.chunk_size,
                                       markdown=note['path'].lower().endswith('.md'), sections=self.section_chunks),
                                self.max_chunks + 1))
        if len(fragments) > self.max_chunks:
            raise VaultError('The file exceeds the semantic index size limit')
        return fragments

    def reconcile_local(self, paths: Iterable[str] | None = None, *,
                        stop: Callable[[], bool] | None = None) -> dict[str, str]:
        full_scan = paths is None
        completed = True
        visible: set[str] = set()
        candidates: set[str] = set()
        excluded = 0
        with closing(sqlite3.connect(self.database)) as db:
            if paths is None:
                visible = set(self.vault.visible_files())
                self.vault.graph.prune(visible)
                saved = {path for path, in db.execute('''
                    SELECT path FROM local_files UNION SELECT path FROM lexical_chunks
                    UNION SELECT path FROM section_chunks
                ''')}
                paths = visible | saved
            revisions = {}
            for path in sorted(set(paths)):
                if stop is not None and stop():
                    completed = False
                    break
                if full_scan and path in visible:
                    try:
                        kind = self._coverage_kind(path)
                    except VaultError:
                        completed = False
                        break
                    else:
                        if kind == 'candidate':
                            candidates.add(path)
                        elif kind == 'excluded':
                            excluded += 1
                if not self._supports_text(path):
                    self.vault.graph.refresh(path)
                    if self.embedder is None or not self.embedder.can_send:
                        continue
                    with self.local_lock:
                        try:
                            mime = self._check_path(path)
                            stamp = self.vault.attachment_stamp(path, ATTACHMENT_LIMIT)
                            row = db.execute('SELECT revision FROM local_files WHERE mode = ? AND path = ?',
                                             (self.section_chunks, path)).fetchone()
                            if not full_scan and row and row[0].partition(':')[0] == stamp:
                                revisions[path] = row[0]
                                continue
                            stamp, data = self.vault.read_attachment(path, ATTACHMENT_LIMIT)
                            revision = _attachment_revision(stamp, data)
                            if not row or row[0] != revision:
                                for _ in attachment_parts(mime, data):
                                    pass
                                with self._write(db):
                                    self._delete_path(db, path)
                                    db.execute('INSERT INTO local_files VALUES (?, ?, ?)',
                                               (self.section_chunks, path, revision))
                            revisions[path] = revision
                        except VaultError:
                            self._drop_path(db, path)
                    continue
                with self.local_lock:
                    try:
                        self.vault._allowed_parts(path)
                        note = self.vault.read(path)
                    except VaultError:
                        self.vault.graph.refresh(path)
                        self._drop_path(db, path)
                        continue
                    self.vault.graph.refresh(path, note)
                    try:
                        self._check_local_path(path)
                        self.policy.check_note(note)
                        revision = note['revision']
                        if not self._lexical_is_current(db, path, revision):
                            self._index_lexical(db, note, self._fragments(note))
                        revisions[path] = revision
                    except ExclusionError:
                        if path in candidates:
                            candidates.remove(path)
                            excluded += 1
                        self._drop_path(db, path)
                    except VaultError:
                        self._drop_path(db, path)
            if full_scan and completed:
                indexed, embedded = self._coverage_counts(db, candidates)
                profile = self.embedder.profile if self.embedder is not None else ''
                with self._write(db):
                    db.execute('INSERT OR REPLACE INTO coverage VALUES (?, ?, ?, ?, ?, ?, ?)', (
                        self.section_chunks, profile, len(candidates), excluded, indexed, embedded,
                        datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')))
            return revisions

    def _drop_path(self, db: sqlite3.Connection, path: str) -> None:
        with self._write(db):
            self._delete_path(db, path)

    @staticmethod
    def _delete_path(db: sqlite3.Connection, path: str) -> None:
        db.execute('DELETE FROM local_files WHERE path = ?', (path,))
        db.execute('DELETE FROM embedded_files WHERE path = ?', (path,))
        db.execute('DELETE FROM lexical_chunks WHERE path = ?', (path,))
        db.execute('DELETE FROM note_metadata WHERE path = ?', (path,))
        db.execute('DELETE FROM section_chunks WHERE path = ?', (path,))

    def _lexical_is_current(self, db: sqlite3.Connection, path: str, revision: str) -> bool:
        return db.execute('''
            SELECT 1 FROM local_files AS f WHERE mode = ? AND path = ? AND revision = ?
            AND (NOT ? OR EXISTS (SELECT 1 FROM note_metadata AS m
                WHERE m.mode = f.mode AND m.path = f.path AND m.kind = 'revision-marker'))
        ''', (self.section_chunks, path, revision, mime_type(path) == 'text/markdown')).fetchone() is not None

    def _index_lexical(self, db: sqlite3.Connection, note: TextFile, fragments: list[Chunk]) -> None:
        path, revision = note['path'], note['revision']
        key = self.section_chunks, path
        if self._lexical_is_current(db, path, revision):
            return
        rows = metadata_rows(note['content']) if mime_type(path) == 'text/markdown' else ()
        with self._write(db):
            if not self._is_current(path, revision, local=True):
                raise VaultError('The file changed during indexing; try again')
            db.execute('DELETE FROM note_metadata WHERE mode = ? AND path = ?', key)
            db.executemany('INSERT INTO note_metadata VALUES (?, ?, ?, ?, ?)', ((*key, *row) for row in rows))
            db.execute('DELETE FROM lexical_chunks WHERE mode = ? AND path = ?', key)
            db.executemany('''
                INSERT INTO lexical_chunks (mode, path, revision, headings, line_start, line_end, text)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', ((*key, revision, json.dumps(chunk.headings), chunk.line_start, chunk.line_end, chunk.text)
                  for chunk in fragments))
            db.execute('INSERT OR REPLACE INTO local_files VALUES (?, ?, ?)', (*key, revision))
            db.execute('DELETE FROM section_chunks WHERE path = ? AND revision != ?', (path, revision))
            db.execute('DELETE FROM embedded_files WHERE path = ? AND revision != ?', (path, revision))

    def _validate_limit(self, limit: int) -> None:
        if not 1 <= limit <= self.max_results:
            raise VaultError(f'Use a result limit between 1 and {self.max_results}')

    def related(self, path: str, limit: int, min_score: float | None = None, *,
                section_line: int | None = None, include_paths: list[str] | None = None,
                mime_types: list[str] | None = None, exclude_linked: bool = False) -> Matches:
        if self.embedder is None:
            raise VaultError('Enable embeddings to find related files')
        self._validate_limit(limit)
        threshold = self.related_min_score if min_score is None else min_score
        if type(threshold) not in (int, float) or not -1 <= threshold <= 1:
            raise VaultError('Use a similarity threshold between -1 and 1')
        prefixes = None if include_paths is None else _path_prefixes(include_paths)
        if mime_types is not None and any(mime not in SUFFIX_TYPES.values() for mime in mime_types):
            raise VaultError('Use supported MIME types for related results')
        mime = self._check_path(path)
        bounds = None
        expected_revision = None
        if section_line is not None:
            if mime != 'text/markdown' or not self.section_chunks:
                raise VaultError('Select a line in a Markdown note with section chunks enabled')
            note = self.vault.read(path)
            bounds = section_lines(note['content'], section_line)
            if bounds is None:
                raise VaultError('Select a line in a Markdown section outside frontmatter')
            expected_revision = note['revision']
        excluded = {path}
        if exclude_linked and mime == 'text/markdown':
            excluded.update(edge['resolved'][0] for edge in self.vault.graph.outgoing(path)
                            if len(edge['resolved']) == 1)
        source = self.index(path, operation='related', queued_revision=expected_revision)
        with closing(sqlite3.connect(self.database)) as db:
            vectors = [_unpack(packed) for packed, in db.execute('''
                SELECT DISTINCT vector FROM section_chunks JOIN vectors USING (profile, hash)
                WHERE profile = ? AND mode = ? AND path = ? AND revision = ?
                  AND (? IS NULL OR (line_end >= ? AND line_start <= ?))
            ''', (self.embedder.profile, self.section_chunks, path, source['revision'],
                  *((None, None, None) if bounds is None else (bounds[0], *bounds))))]
        if not vectors:
            if bounds is None:
                raise VaultError('The source has no fragments of at least '
                                 f'{self.min_chunk_words} words; lower min_chunk_words to index it')
            raise VaultError('The selected section has no fragments of at least '
                             f'{self.min_chunk_words} words; select a longer section or lower min_chunk_words')
        if not self._is_current(path, str(source['revision'])):
            raise VaultError('The source changed during related search; try again')

        def accepts(candidate: str) -> bool:
            if candidate in excluded or (mime_types is not None and mime_type(candidate) not in mime_types):
                return False
            if prefixes is None:
                return True
            parts = _parts(candidate)
            return any(parts[:len(prefix)] == prefix for prefix in prefixes)

        return self._match(self.embedder.profile, vectors, limit, threshold, accepts=accepts, group_sections=True)

    def status(self) -> Status:
        mode = self.section_chunks
        profile = self.embedder.profile if self.embedder is not None else ''
        with closing(sqlite3.connect(self.database)) as db:
            row = db.execute('SELECT files, excluded, indexed, embedded, at FROM coverage '
                             'WHERE mode = ? AND profile = ?', (mode, profile)).fetchone()
            pending = self._queue_depth(db)
        if row is None:
            files = excluded = indexed = unindexed = embedded = last = None
        else:
            files, excluded, indexed, embedded, last = row
            unindexed = files - indexed
        report: Status = {'index': {
            'mode': 'sections' if mode else 'size',
            'files': files,
            'excluded': excluded,
            'indexed': indexed,
            'unindexed': unindexed,
            'embedded': embedded,
            'pending': pending,
            'last_full_scan': last,
        }}
        if self.embedder is not None:
            report['embeddings'] = {
                **self.embedder.description(),
                'min_chunk_words': self.min_chunk_words,
                'related_min_score': self.related_min_score,
                'allowed_mime_types': sorted(self.allowed_mime_types),
            }
            report['spend'] = self.embedder.spend_report()
        return report

    def _in_scope(self, parts: tuple[str, ...]) -> bool:
        if self.include and not any(parts[:len(prefix)] == prefix for prefix in self.include):
            return False
        return not any(parts[:len(prefix)] == prefix for prefix in self.exclude)

    def _size(self, path: str) -> int | None:
        # visible_files already rejects symlinks; lstat keeps a stale queue row from following one
        try:
            info = os.lstat(self.vault.root / path)
        except OSError:
            return None
        return info.st_size if stat_module.S_ISREG(info.st_mode) else None

    def _coverage_kind(self, path: str) -> str:
        # Type and scope decide the denominator; policy exclusion is counted apart.
        mime = mime_type(path)
        text = mime in TEXT_TYPES
        can_embed = self.embedder is not None and self.embedder.can_send
        if not text and not (can_embed and mime in self.allowed_mime_types
                             and self._in_scope(_parts(path))):
            return 'skipped'
        try:
            self.policy.check(path)
        except ExclusionError:
            return 'excluded'
        size = self._size(path)
        limit = self.vault.max_read_bytes if text else ATTACHMENT_LIMIT
        if size is None or size > limit or (not text and not size):
            return 'skipped'
        return 'candidate'

    def _coverage_counts(self, db: sqlite3.Connection, candidates: set[str]) -> tuple[int, int]:
        if not candidates:
            return 0, 0
        local = {path for path, in db.execute(
            'SELECT path FROM local_files WHERE mode = ?', (self.section_chunks,))}
        embedded = 0
        if self.embedder is not None:
            embedded = len(candidates & {path for path, in db.execute('''
                SELECT e.path FROM embedded_files AS e
                WHERE e.profile = ? AND e.mode = ? AND EXISTS (
                    SELECT 1 FROM section_chunks AS c
                    WHERE c.profile = e.profile AND c.mode = e.mode AND c.path = e.path
                      AND c.revision = e.revision
                )
            ''', (self.embedder.profile, self.section_chunks))})
        return len(candidates & local), embedded

    def _queue_depth(self, db: sqlite3.Connection) -> int:
        # Count database rows in the configured embedding scope, not files on disk.
        # An unreconciled deleted or policy-excluded path can stay in this count.
        if self.embedder is None or not self.embedder.can_send:
            return 0
        count = 0
        for path, _ in db.execute(self._pending_rows, (self.section_chunks, self.embedder.profile)):
            try:
                if mime_type(path) in self.allowed_mime_types and self._in_scope(_parts(path)):
                    count += 1
            except VaultError:
                continue
        return count

    def map(self, path: str, limit: int = 10, min_score: float | None = None, *,
            section_line: int | None = None, include_paths: list[str] | None = None,
            mime_types: list[str] | None = None, exclude_linked: bool = False) -> Map:
        if self.embedder is None:
            raise VaultError('Enable embeddings to draw a similarity map')
        result = self.related(path, limit, min_score, section_line=section_line, include_paths=include_paths,
                              mime_types=mime_types, exclude_linked=exclude_linked)
        matches = {match['path']: match for match in result['matches']}
        try:
            coordinates, explained = project(self._centroids([path, *matches]))
        except ValueError:
            raise VaultError('The vectors of these files cannot form a map') from None
        x, y = coordinates.get(path, (0.0, 0.0))
        points: list[MapPoint] = [{'path': path, 'x': x, 'y': y, 'score': 1.0, 'source': True}]
        for target, match in matches.items():
            x, y = coordinates.get(target, (0.0, 0.0))
            points.append({'path': target, 'x': x, 'y': y, 'score': match['score']})
        return {'points': points, 'explained': explained, 'dimensions': self.embedder.dimensions}

    def _centroids(self, paths: list[str]) -> dict[str, list[float]]:
        assert self.embedder is not None
        placeholders = ', '.join('?' * len(paths))
        with closing(sqlite3.connect(self.database)) as db:
            rows = db.execute(f'''
                SELECT path, vector FROM section_chunks JOIN vectors USING (profile, hash)
                WHERE profile = ? AND mode = ? AND path IN ({placeholders})
            ''', (self.embedder.profile, self.section_chunks, *paths)).fetchall()
        sums: dict[str, list[float]] = {}
        for path, packed in rows:
            vector = _unpack(packed)
            if len(vector) != self.embedder.dimensions:
                raise VaultError('The vector index holds a vector of an unexpected size')
            total = sums.setdefault(path, [0.0] * len(vector))
            for index, value in enumerate(vector):
                total[index] += value
        centroids = {}
        for path, total in sums.items():
            length = math.hypot(*total)
            if length:
                centroids[path] = [value / length for value in total]
        return centroids

    def find(self, *, tags: list[str] | None = None, aliases: list[str] | None = None,
             properties: dict[str, str | None] | None = None, alias_property: str = 'aliases',
             filename_regex: str | None = None, limit: int = 10) -> dict[str, list[str]]:
        self._validate_limit(limit)
        tags, aliases, properties = tags or [], aliases or [], properties or {}
        if not 1 <= len(tags) + len(aliases) + len(properties) + (filename_regex is not None) <= 32:
            raise VaultError('Supply between 1 and 32 search filters')
        pattern = None
        if filename_regex is not None:
            if not filename_regex or len(filename_regex) > 512:
                raise VaultError('Use a non-empty filename regular expression of at most 512 characters')
            try:
                pattern = regex.compile(filename_regex)
            except (regex.error, RecursionError):
                raise VaultError('Invalid filename regular expression') from None
        if not alias_property.strip() or any(not name.strip() for name in properties):
            raise VaultError('Use non-empty property names')
        clauses: list[str] = []
        args: list[int | str] = []

        def select(kind: str, name: str, value: str) -> None:
            condition = 'value = ?'
            keys = [value]
            if kind == 'tag':
                condition = '(value = ? OR (value >= ? AND value < ?))'
                keys.extend((value + '/', value + '0'))
            clauses.append('SELECT path FROM note_metadata WHERE mode = ? AND kind = ? AND name = ? AND ' + condition)
            args.extend((int(self.section_chunks), kind, name, *keys))

        for value in tags:
            tag = tag_key(value)
            if not tag:
                raise VaultError('Use non-empty tags')
            select('tag', '', tag)
        for alias in aliases:
            if not alias.strip():
                raise VaultError('Use non-empty aliases')
            select('folded', alias_property, fold(alias))
        for name, value in properties.items():
            select('present', name, '') if value is None else select('value', name, value)

        if not clauses:
            select('revision-marker', '', '')

        paths: list[str] = []
        regex_budget = 0.25
        with closing(sqlite3.connect(self.database)) as db:
            for path, revision in db.execute(f'''
                WITH hits AS ({' INTERSECT '.join(clauses)})
                SELECT f.path, f.revision FROM hits JOIN local_files AS f USING (path)
                WHERE f.mode = ? ORDER BY f.path
            ''', (*args, self.section_chunks)):
                if pattern is not None:
                    started = monotonic()
                    try:
                        match = pattern.search(path.rsplit('/', 1)[-1], timeout=regex_budget)
                    except TimeoutError:
                        raise VaultError('Filename regular expression exceeded the time limit') from None
                    regex_budget -= monotonic() - started
                    if regex_budget <= 0:
                        raise VaultError('Filename regular expression exceeded the time limit')
                    if match is None:
                        continue
                if self._is_current(path, revision, local=True):
                    paths.append(path)
                    if len(paths) == limit:
                        break
        return {'paths': paths}

    def query(self, text: str, limit: int) -> Matches:
        self._validate_limit(limit)
        if not text.strip() or len(text) > self.chunk_size or '\0' in text:
            raise VaultError('Use a short non-empty text query without NUL characters')
        if self.embedder is None:
            return {'matches': _merge_ranks(self._lexical(text, limit))}
        digest = sha256(text.encode()).hexdigest()
        profile = self.embedder.profile
        with closing(sqlite3.connect(self.database)) as db:
            cached = db.execute('SELECT vector FROM vectors WHERE profile = ? AND hash = ?',
                                (profile, digest)).fetchone()
        if cached is not None:
            vector = _unpack(cached[0])
        else:
            try:
                vector = self.embedder.embed(text, 'query')
            except EmbeddingError:
                return {'matches': _merge_ranks(self._lexical(text, limit)), 'vector_unavailable': True}
            with closing(sqlite3.connect(self.database)) as db, self._write(db):
                db.execute('INSERT OR IGNORE INTO vectors VALUES (?, ?, ?)', (profile, digest, _pack(vector)))
        vectors = [vector]
        lexical = self._lexical(text, self.max_results)
        semantic = self._match(self.embedder.profile, vectors, self.max_results, -1)['matches']
        return {'matches': _merge_ranks(lexical, semantic)[:limit]}

    def _is_current(self, path: str, revision: str, *, local: bool = False) -> bool:
        try:
            if local:
                self._check_local_path(path)
            elif self._check_path(path) not in TEXT_TYPES:
                return self.vault.attachment_stamp(path, ATTACHMENT_LIMIT) == revision.partition(':')[0]
            note = self.vault.read(path)
            if note['revision'] != revision:
                return False
            self.policy.check_note(note)
            return True
        except VaultError:
            return False

    def _lexical(self, text: str, limit: int) -> list[Match]:
        terms = dict.fromkeys(re.findall(r'\w+', text.lower()))
        if not terms:
            return []
        expression = ' OR '.join(f'"{term}"' for term in terms)
        hits: list[Match] = []
        seen: set[str] = set()
        with closing(sqlite3.connect(self.database)) as db:
            for path, revision, headings, start, end, score in db.execute('''
                SELECT c.path, c.revision, c.headings, c.line_start, c.line_end, bm25(lexical_fts)
                FROM lexical_fts JOIN lexical_chunks AS c ON c.id = lexical_fts.rowid
                WHERE lexical_fts MATCH ? AND c.mode = ? ORDER BY 6, c.path, c.id
            ''', (expression, self.section_chunks)):
                if path in seen:
                    continue
                seen.add(path)
                if not self._is_current(path, revision, local=True):
                    continue
                hits.append({'path': path, 'score': score, 'headings': json.loads(headings),
                             'line_start': start, 'line_end': end})
                if len(hits) == limit:
                    break
        return hits

    def _match(self, profile: str, vectors: Sequence[Sequence[float] | np.ndarray], limit: int,
               min_score: float,
               *, accepts: Callable[[str], bool] | None = None, group_sections: bool = False) -> Matches:
        if not vectors:
            return {'matches': []}
        source = np.asarray(vectors, dtype=_VECTOR)
        best: dict[tuple[str, str], Match] = {}
        sections: dict[tuple[str, str], list[SectionMatch]] = {}
        batch: list[tuple[str, str, np.ndarray, str, int | None, int | None, int]] = []

        def score_batch() -> None:
            matrix = np.vstack([item[2] for item in batch])
            scores = (matrix @ source.T).max(axis=1)
            for (path, revision, _, headings, start, end, ordinal), value in zip(batch, scores, strict=True):
                score = max(-1.0, min(1.0, float(value)))
                key = path, revision
                if score < min_score:
                    continue
                is_best = key not in best or score > best[key]['score']
                if not group_sections and not is_best:
                    continue
                location: SectionMatch = {'score': score}
                if start is not None and end is not None:
                    location.update(headings=json.loads(headings), line_start=start, line_end=end)
                elif mime_type(path) == 'application/pdf':
                    location['page'] = ordinal + 1
                if group_sections and ('line_start' in location or 'page' in location):
                    sections.setdefault(key, []).append(location)
                if is_best:
                    hit: Match = {'path': path, 'score': score}
                    if 'headings' in location:
                        hit['headings'] = location['headings']
                    if 'line_start' in location:
                        hit['line_start'] = location['line_start']
                    if 'line_end' in location:
                        hit['line_end'] = location['line_end']
                    if 'page' in location:
                        hit['page'] = location['page']
                    best[key] = hit
            batch.clear()

        with closing(sqlite3.connect(self.database)) as db:
            for path, revision, packed, headings, start, end, ordinal in db.execute('''
                SELECT path, revision, vector, headings, line_start, line_end, ordinal
                FROM section_chunks JOIN vectors USING (profile, hash)
                WHERE profile = ? AND mode = ? ORDER BY path, ordinal
            ''', (profile, self.section_chunks)):
                if accepts is not None and not accepts(path):
                    continue
                batch.append((path, revision, _unpack(packed), headings, start, end, ordinal))
                if len(batch) >= 2048:
                    score_batch()
        if batch:
            score_batch()
        hits: list[Match] = []
        for (path, revision), hit in sorted(best.items(), key=lambda item: (-item[1]['score'], item[0][0])):
            if len(hits) == limit:
                break
            if self._is_current(path, revision):
                locations = sections.get((path, revision))
                if locations:
                    hit['sections'] = sorted(locations, key=lambda item: -item['score'])
                hits.append(hit)
        return {'matches': hits}
