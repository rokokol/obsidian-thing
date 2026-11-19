import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from hashlib import sha256
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import TypedDict

from .embeddings import Embedder
from .vault import Vault, VaultError, _parts


class Match(TypedDict):
    path: str
    score: float


class Matches(TypedDict):
    matches: list[Match]


def _scope(config: dict, key: str) -> list[tuple[str, ...]]:
    paths = config.get(key, [])
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise ValueError(f'Configure embedding {key} as a list of paths')
    return [() if path == '.' else _parts(path) for path in paths]


def _chunks(content: str, size: int) -> Iterator[str]:
    for start in range(0, len(content), size):
        chunk = content[start:start + size]
        if chunk.strip():
            yield chunk


class SearchIndex:
    chunk_size = 1500
    max_chunks = 32

    def __init__(self, vault: Vault, config: dict, state: Path):
        self.vault = vault
        self.include = _scope(config, 'include_paths')
        self.exclude = _scope(config, 'exclude_paths')
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
                CREATE TABLE IF NOT EXISTS chunks (
                    profile TEXT, path TEXT, revision TEXT NOT NULL, hash TEXT,
                    PRIMARY KEY (profile, path, hash)
                );
            ''')

    def _check_path(self, path: str) -> None:
        parts = _parts(path)
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
            chunks = list(_chunks(note['content'], self.chunk_size))
            if len(chunks) > self.max_chunks:
                raise VaultError('The file exceeds the semantic index size limit')
            hashes = {}
            for chunk in chunks:
                digest = sha256(chunk.encode()).hexdigest()
                hashes[digest] = chunk
            for digest, chunk in hashes.items():
                if not db.execute('SELECT 1 FROM vectors WHERE profile = ? AND hash = ?',
                                  (profile, digest)).fetchone():
                    vector = self.embedder.embed(chunk, operation)
                    with db:
                        db.execute('INSERT OR IGNORE INTO vectors VALUES (?, ?, ?)',
                                   (profile, digest, json.dumps(vector)))
            if self.vault.read(path)['revision'] != revision:
                raise VaultError('The file changed during indexing; try again')
            with db:
                db.execute('DELETE FROM chunks WHERE profile = ? AND path = ?', (profile, path))
                db.executemany('INSERT INTO chunks VALUES (?, ?, ?, ?)',
                               ((profile, path, revision, digest) for digest in hashes))
        return {'path': path, 'revision': revision, 'chunks': len(chunks)}

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if not 1 <= limit <= 100:
            raise VaultError('Use a result limit between 1 and 100')

    def related(self, path: str, limit: int, min_score: float) -> Matches:
        self._validate_limit(limit)
        if not -1 <= min_score <= 1:
            raise VaultError('Use a similarity threshold between -1 and 1')
        self.index(path, operation='related')
        with closing(sqlite3.connect(self.database)) as db:
            vectors = [json.loads(row[0]) for row in db.execute('''
                SELECT vector FROM chunks JOIN vectors USING (profile, hash)
                WHERE profile = ? AND path = ?
            ''', (self.embedder.profile, path))]
        return self._match(vectors, limit, min_score, exclude=path)

    def query(self, text: str, limit: int) -> Matches:
        self._validate_limit(limit)
        if not text.strip() or len(text) > self.chunk_size or '\0' in text:
            raise VaultError('Use a short non-empty text query without NUL characters')
        return self._match([self.embedder.embed(text, 'query')], limit, -1)

    def _match(self, vectors: list[list[float]], limit: int, min_score: float,
               *, exclude: str | None = None) -> Matches:
        if not vectors:
            return {'matches': []}
        best: dict[tuple[str, str], float] = {}
        with closing(sqlite3.connect(self.database)) as db:
            for path, revision, packed in db.execute('''
                SELECT path, revision, vector FROM chunks JOIN vectors USING (profile, hash)
                WHERE profile = ?
            ''', (self.embedder.profile,)):
                if path == exclude:
                    continue
                vector = json.loads(packed)
                score = max(math.fsum(x * y for x, y in zip(source, vector, strict=True)) for source in vectors)
                key = path, revision
                best[key] = max(best.get(key, -1), min(1, score))
        hits: list[Match] = []
        for (path, revision), score in sorted(best.items(), key=lambda item: (-item[1], item[0][0])):
            if len(hits) == limit or score < min_score:
                break
            try:
                self._check_path(path)
                if self.vault.read(path)['revision'] == revision:
                    hits.append({'path': path, 'score': score})
            except VaultError:
                continue
        return {'matches': hits}
