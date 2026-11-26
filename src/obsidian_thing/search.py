import json
import math
import re
import sqlite3
from contextlib import closing
from hashlib import sha256
from itertools import islice
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import NotRequired, TypedDict

from .embeddings import Embedder, EmbeddingError
from .index_policy import IndexPolicy
from .search_chunks import Chunk, chunks
from .vault import Vault, VaultError, _parts


class Match(TypedDict):
    path: str
    score: float
    headings: list[str]
    line_start: int
    line_end: int


class Matches(TypedDict):
    matches: list[Match]
    vector_unavailable: NotRequired[bool]


def _scope(config: dict, key: str) -> list[tuple[str, ...]]:
    paths = config.get(key, [])
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise ValueError(f'Configure embedding {key} as a list of paths')
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

    def __init__(self, vault: Vault, config: dict, state: Path, *, indexing: object = None):
        self.vault = vault
        self.policy = indexing if isinstance(indexing, IndexPolicy) else IndexPolicy(vault, {} if indexing is None else indexing)
        self.include = _scope(config, 'include_paths')
        self.exclude = _scope(config, 'exclude_paths')
        self.section_chunks = config.get('section_chunks', True)
        if not isinstance(self.section_chunks, bool):
            raise ValueError('Configure embedding section_chunks as a boolean')
        if config.get('provider') == 'openrouter' and not self.include:
            raise ValueError('Configure include_paths before you send files to OpenRouter')
        self.embedder = Embedder(config, state)
        self.database = state / 'search.sqlite3'
        self.index_lock = Lock()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS vectors (
                    profile TEXT, hash TEXT, vector TEXT NOT NULL, PRIMARY KEY (profile, hash)
                );
                CREATE TABLE IF NOT EXISTS section_chunks (
                    profile TEXT, mode INTEGER, path TEXT, revision TEXT NOT NULL, hash TEXT NOT NULL,
                    ordinal INTEGER, headings TEXT NOT NULL, line_start INTEGER, line_end INTEGER,
                    PRIMARY KEY (profile, mode, path, ordinal)
                );
                CREATE TABLE IF NOT EXISTS lexical_chunks (
                    id INTEGER PRIMARY KEY, mode INTEGER NOT NULL, path TEXT NOT NULL,
                    revision TEXT NOT NULL, headings TEXT NOT NULL, line_start INTEGER NOT NULL,
                    line_end INTEGER NOT NULL, text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS lexical_paths ON lexical_chunks (mode, path);
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

    def _check_path(self, path: str) -> None:
        parts = _parts(path)
        self.vault._allowed_parts(path)
        self.policy.check(path)
        if PurePosixPath(path).suffix.lower() not in ('.md', '.txt'):
            raise VaultError('Only Markdown and plain text paths can use this index')
        if self.include and not any(parts[:len(prefix)] == prefix for prefix in self.include):
            raise VaultError('The path is outside the embedding scope')
        if any(parts[:len(prefix)] == prefix for prefix in self.exclude):
            raise VaultError('The path is excluded from embeddings')

    def index(self, path: str, *, operation: str = 'index') -> dict[str, str | int]:
        self._check_path(path)
        profile = self.embedder.profile
        with self.index_lock, closing(sqlite3.connect(self.database)) as db:
            note = self.vault.read(path)
            revision = note['revision']
            fragments = list(islice(chunks(note['content'], self.chunk_size,
                                           markdown=path.lower().endswith('.md'), sections=self.section_chunks),
                                    self.max_chunks + 1))
            if len(fragments) > self.max_chunks:
                raise VaultError('The file exceeds the semantic index size limit')
            self._index_lexical(db, path, revision, fragments)
            digests = [sha256(chunk.text.encode()).hexdigest() for chunk in fragments]
            hashes = {digest: chunk.text for digest, chunk in zip(digests, fragments, strict=True)}
            for digest, chunk in hashes.items():
                if not db.execute('SELECT 1 FROM vectors WHERE profile = ? AND hash = ?',
                                  (profile, digest)).fetchone():
                    if not self._is_current(path, revision):
                        raise VaultError('The file changed or left the index scope during indexing')
                    vector = self.embedder.embed(chunk, operation)
                    with db:
                        db.execute('INSERT OR IGNORE INTO vectors VALUES (?, ?, ?)',
                                   (profile, digest, json.dumps(vector)))
            if not self._is_current(path, revision):
                raise VaultError('The file changed during indexing; try again')
            with db:
                db.execute('DELETE FROM section_chunks WHERE profile = ? AND mode = ? AND path = ?',
                           (profile, self.section_chunks, path))
                db.executemany('INSERT INTO section_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (
                    (profile, self.section_chunks, path, revision, digest, ordinal,
                     json.dumps(chunk.headings), chunk.line_start, chunk.line_end)
                    for ordinal, (digest, chunk) in enumerate(zip(digests, fragments, strict=True))
                ))
        return {'path': path, 'revision': revision, 'chunks': len(fragments)}

    def _index_lexical(self, db: sqlite3.Connection, path: str, revision: str, fragments: list[Chunk]) -> None:
        key = self.section_chunks, path
        if db.execute('SELECT 1 FROM lexical_chunks WHERE mode = ? AND path = ? AND revision = ? LIMIT 1',
                      (*key, revision)).fetchone():
            return
        if not self._is_current(path, revision):
            raise VaultError('The file changed during indexing; try again')
        with db:
            db.execute('DELETE FROM lexical_chunks WHERE mode = ? AND path = ?', key)
            db.executemany('''
                INSERT INTO lexical_chunks (mode, path, revision, headings, line_start, line_end, text)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', ((*key, revision, json.dumps(chunk.headings), chunk.line_start, chunk.line_end, chunk.text)
                  for chunk in fragments))

    def _validate_limit(self, limit: int) -> None:
        if not 1 <= limit <= self.max_results:
            raise VaultError(f'Use a result limit between 1 and {self.max_results}')

    def related(self, path: str, limit: int, min_score: float) -> Matches:
        self._validate_limit(limit)
        if not -1 <= min_score <= 1:
            raise VaultError('Use a similarity threshold between -1 and 1')
        self.index(path, operation='related')
        with closing(sqlite3.connect(self.database)) as db:
            vectors = [json.loads(row[0]) for row in db.execute('''
                SELECT DISTINCT vector FROM section_chunks JOIN vectors USING (profile, hash)
                WHERE profile = ? AND mode = ? AND path = ?
            ''', (self.embedder.profile, self.section_chunks, path))]
        return self._match(vectors, limit, min_score, exclude=path)

    def query(self, text: str, limit: int) -> Matches:
        self._validate_limit(limit)
        if not text.strip() or len(text) > self.chunk_size or '\0' in text:
            raise VaultError('Use a short non-empty text query without NUL characters')
        try:
            vectors = [self.embedder.embed(text, 'query')]
        except EmbeddingError:
            return {'matches': _merge_ranks(self._lexical(text, limit)), 'vector_unavailable': True}
        lexical = self._lexical(text, self.max_results)
        semantic = self._match(vectors, self.max_results, -1)['matches']
        return {'matches': _merge_ranks(lexical, semantic)[:limit]}

    def _is_current(self, path: str, revision: str) -> bool:
        try:
            self._check_path(path)
            return self.vault.read(path)['revision'] == revision
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
                if not self._is_current(path, revision):
                    continue
                hits.append({'path': path, 'score': score, 'headings': json.loads(headings),
                             'line_start': start, 'line_end': end})
                if len(hits) == limit:
                    break
        return hits

    def _match(self, vectors: list[list[float]], limit: int, min_score: float,
               *, exclude: str | None = None) -> Matches:
        if not vectors:
            return {'matches': []}
        best: dict[tuple[str, str], Match] = {}
        with closing(sqlite3.connect(self.database)) as db:
            for path, revision, packed, headings, start, end in db.execute('''
                SELECT path, revision, vector, headings, line_start, line_end
                FROM section_chunks JOIN vectors USING (profile, hash)
                WHERE profile = ? AND mode = ? ORDER BY path, ordinal
            ''', (self.embedder.profile, self.section_chunks)):
                if path == exclude:
                    continue
                vector = json.loads(packed)
                score = max(math.fsum(x * y for x, y in zip(source, vector, strict=True)) for source in vectors)
                key = path, revision
                score = max(-1, min(1, score))
                if key not in best or score > best[key]['score']:
                    best[key] = {'path': path, 'score': score, 'headings': json.loads(headings),
                                 'line_start': start, 'line_end': end}
        hits: list[Match] = []
        for (path, revision), hit in sorted(best.items(), key=lambda item: (-item[1]['score'], item[0][0])):
            if len(hits) == limit or hit['score'] < min_score:
                break
            if self._is_current(path, revision):
                hits.append(hit)
        return {'matches': hits}
