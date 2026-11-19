import os
import stat
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import TypedDict
from uuid import uuid4


class FileRevision(TypedDict):
    path: str
    revision: str


class TextFile(FileRevision):
    content: str


class VaultError(ValueError):
    pass


def _parts(path: str) -> tuple[str, ...]:
    parts = tuple(path.split('/'))
    if '\\' in path or '\0' in path or any(not p or p.startswith('.') for p in parts):
        raise VaultError('Use a non-hidden path relative to the vault root')
    return parts


class Vault:
    max_read_bytes = 1_048_576

    def __init__(self, root: Path, allowed: list[str]):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError('The vault root must be a directory')
        self._mutation_lock = Lock()
        self.rules: dict[tuple[str, ...], bool] = {}
        for path in allowed:
            parts = () if path == '.' else _parts(path)
            try:
                with self._open(parts) as fd:
                    mode = os.fstat(fd).st_mode
                    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                        raise VaultError('An allowed path must name a file or directory')
                    self.rules[parts] = stat.S_ISDIR(mode)
            except OSError:
                raise VaultError('An allowed path does not exist or is not accessible') from None

    @property
    def allowed(self) -> list[str]:
        return sorted('/'.join(parts) or '.' for parts in self.rules)

    @contextmanager
    def _directory(self, parts: tuple[str, ...]):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
        fd = os.open(self.root, flags)
        try:
            for name in parts:
                child = os.open(name, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def _open(self, parts: tuple[str, ...]):
        with self._directory(parts[:-1]) as parent:
            if not parts:
                yield parent
            else:
                with self._entry(parent, parts[-1]) as fd:
                    yield fd

    @staticmethod
    @contextmanager
    def _entry(parent: int, name: str):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            yield fd
        finally:
            os.close(fd)

    def _allowed_parts(self, path: str) -> tuple[str, ...]:
        parts = _parts(path)
        if parts not in self.rules and not any(self.rules.get(parts[:i]) for i in range(len(parts))):
            raise VaultError('The path is not allowed')
        return parts

    def _read_text(self, path: str, fd: int) -> tuple[TextFile, int]:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise VaultError('The path must name a regular file')
        with os.fdopen(fd, 'rb', closefd=False) as file:
            data = file.read(self.max_read_bytes + 1)
        if len(data) > self.max_read_bytes:
            raise VaultError('The file exceeds the read limit')
        try:
            content = data.decode('utf-8')
        except UnicodeDecodeError:
            raise VaultError('The file is not UTF-8 text') from None
        if '\0' in content:
            raise VaultError('The file is not UTF-8 text')
        return {'path': path, 'content': content, 'revision': sha256(data).hexdigest()}, mode

    def read(self, path: str) -> TextFile:
        parts = self._allowed_parts(path)
        try:
            with self._open(parts) as fd:
                note, _ = self._read_text(path, fd)
                return note
        except OSError:
            raise VaultError('The file does not exist or is not accessible') from None

    def write(self, path: str, content: str, expected_revision: str | None) -> FileRevision:
        parts = self._allowed_parts(path)
        name = parts[-1]
        try:
            with self._mutation_lock, self._directory(parts[:-1]) as parent:
                mode = None
                if expected_revision is not None:
                    with self._entry(parent, name) as fd:
                        note, mode = self._read_text(path, fd)
                    if note['revision'] != expected_revision:
                        raise VaultError('The revision does not match; read the file again')
                return self._store(path, parent, name, content, mode)
        except FileExistsError:
            raise VaultError('The path already exists; read it before you replace it') from None
        except OSError:
            raise VaultError('Cannot write to this path') from None

    def _store(self, path: str, parent: int, name: str, content: str, mode: int | None) -> FileRevision:
        if '\0' in content:
            raise VaultError('The content must be UTF-8 text without NUL characters')
        try:
            data = content.encode('utf-8')
        except UnicodeEncodeError:
            raise VaultError('The content must be UTF-8 text') from None
        if len(data) > self.max_read_bytes:
            raise VaultError('The content exceeds the file size limit')
        temp = f'.obsidian-thing-{uuid4().hex}'
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(temp, flags, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as file:
                file.write(data)
                file.flush()
                if mode is not None:
                    os.fchmod(file.fileno(), mode & 0o777)
                os.fsync(file.fileno())
            if mode is None:
                # A hard link fails if another writer creates the target first.
                os.link(temp, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            else:
                os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
                temp = None
        finally:
            if temp is not None:
                os.unlink(temp, dir_fd=parent)
        return {'path': path, 'revision': sha256(data).hexdigest()}
