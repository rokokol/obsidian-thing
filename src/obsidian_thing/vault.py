import os
import stat
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import NotRequired, TypedDict
from uuid import uuid4

from .checker import Checker


class FileRevision(TypedDict):
    path: str
    revision: str


class MutationResult(FileRevision):
    warnings: NotRequired[list[str]]


class TextFile(FileRevision):
    content: str


class TrashedFile(FileRevision):
    trash_id: str


class VaultError(ValueError):
    pass


def _parts(path: str) -> tuple[str, ...]:
    parts = tuple(path.split('/'))
    if '\\' in path or '\0' in path or any(not p or p.startswith('.') for p in parts):
        raise VaultError('Use a non-hidden path relative to the vault root')
    return parts


class Vault:
    max_read_bytes = 1_048_576

    def __init__(self, root: Path, allowed: list[str], *, checker: Checker | None = None):
        self.checker = checker
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

    def write(self, path: str, content: str, expected_revision: str | None) -> MutationResult:
        parts = self._allowed_parts(path)
        name = parts[-1]
        try:
            with self._mutation_lock, self._directory(parts[:-1]) as parent:
                mode = None
                before = None
                if expected_revision is not None:
                    note, mode = self._check_revision(path, parent, name, expected_revision)
                    before = note['content']
                elif self.checker is not None:
                    try:
                        os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise FileExistsError
                return self._store(path, parent, name, content, mode, operation='write', before=before)
        except FileExistsError:
            raise VaultError('The path already exists; read it before you replace it') from None
        except OSError:
            raise VaultError('Cannot write to this path') from None

    def _check_revision(self, path: str, parent: int, name: str, expected: str) -> tuple[TextFile, int]:
        with self._entry(parent, name) as fd:
            note, mode = self._read_text(path, fd)
        if note['revision'] != expected:
            raise VaultError('The revision does not match; read the file again')
        return note, mode

    def update(self, path: str, old: str, new: str, expected_revision: str) -> MutationResult:
        parts = self._allowed_parts(path)
        name = parts[-1]
        if not old:
            raise VaultError('The old fragment must not be empty')
        try:
            with self._mutation_lock, self._directory(parts[:-1]) as parent:
                note, mode = self._check_revision(path, parent, name, expected_revision)
                content = note['content']
                start = content.find(old)
                if start == -1 or content.find(old, start + 1) != -1:
                    raise VaultError('The old fragment must occur exactly once')
                after = content[:start] + new + content[start + len(old):]
                return self._store(path, parent, name, after, mode, operation='update', before=content)
        except OSError:
            raise VaultError('Cannot update this path') from None

    def delete(self, path: str, expected_revision: str, trash_dir: Path) -> TrashedFile:
        parts = self._allowed_parts(path)
        if parts in self.rules:
            raise VaultError('Cannot delete a path named in the allowlist')
        name = parts[-1]
        try:
            with self._mutation_lock, self._directory(parts[:-1]) as parent:
                note, _ = self._check_revision(path, parent, name, expected_revision)
                trash_id = self._move_to_trash(parent, name, path, trash_dir)
        except OSError:
            raise VaultError('Cannot move this file to the trash; check the directory and mount') from None
        return {'path': path, 'revision': note['revision'], 'trash_id': trash_id}

    @staticmethod
    def _move_to_trash(parent: int, name: str, path: str, trash_dir: Path) -> str:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        trash = os.open(trash_dir, flags)
        try:
            info = os.fstat(trash)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise VaultError('The trash directory must be private and owned by the server user')
            trash_id = uuid4().hex
            os.mkdir(trash_id, 0o700, dir_fd=trash)
            try:
                entry = os.open(trash_id, flags, dir_fd=trash)
                try:
                    metadata = os.open('path', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=entry)
                    try:
                        with os.fdopen(metadata, 'wb') as file:
                            file.write(os.fsencode(path))
                            file.flush()
                            os.fsync(file.fileno())
                        os.rename(name, 'file', src_dir_fd=parent, dst_dir_fd=entry)
                    except OSError:
                        os.unlink('path', dir_fd=entry)
                        raise
                finally:
                    os.close(entry)
            except OSError:
                os.rmdir(trash_id, dir_fd=trash)
                raise
        finally:
            os.close(trash)
        return trash_id

    def _store(self, path: str, parent: int, name: str, content: str, mode: int | None,
               *, operation: str, before: str | None) -> MutationResult:
        if '\0' in content:
            raise VaultError('The content must be UTF-8 text without NUL characters')
        try:
            data = content.encode('utf-8')
        except UnicodeEncodeError:
            raise VaultError('The content must be UTF-8 text') from None
        if len(data) > self.max_read_bytes:
            raise VaultError('The content exceeds the file size limit')
        warnings = self.checker.check(operation, path, before, content) if self.checker else []
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
        result: MutationResult = {'path': path, 'revision': sha256(data).hexdigest()}
        if warnings:
            result['warnings'] = warnings
        return result
