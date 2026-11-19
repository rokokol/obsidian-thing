import os
import stat
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import TypedDict


class TextFile(TypedDict):
    path: str
    content: str
    revision: str


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
    def _open(self, parts: tuple[str, ...]):
        flags = os.O_RDONLY | os.O_NOFOLLOW
        fd = os.open(self.root, flags | os.O_DIRECTORY)
        try:
            for i, name in enumerate(parts):
                kind = os.O_DIRECTORY if i < len(parts) - 1 else os.O_NONBLOCK
                child = os.open(name, flags | kind, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    def read(self, path: str) -> TextFile:
        parts = _parts(path)
        if parts not in self.rules and not any(self.rules.get(parts[:i]) for i in range(len(parts))):
            raise VaultError('The path is not allowed')
        try:
            with self._open(parts) as fd:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise VaultError('The path must name a regular file')
                with os.fdopen(fd, 'rb', closefd=False) as file:
                    data = file.read(self.max_read_bytes + 1)
        except OSError:
            raise VaultError('The file does not exist or is not accessible') from None
        if len(data) > self.max_read_bytes:
            raise VaultError('The file exceeds the read limit')
        try:
            content = data.decode('utf-8')
        except UnicodeDecodeError:
            raise VaultError('The file is not UTF-8 text') from None
        if '\0' in content:
            raise VaultError('The file is not UTF-8 text')
        return {'path': path, 'content': content, 'revision': sha256(data).hexdigest()}
