import os
import stat
from collections.abc import Callable
from contextlib import ExitStack, contextmanager, suppress
from hashlib import file_digest, sha256
from pathlib import Path
from threading import Lock
from typing import NotRequired, TypedDict
from uuid import uuid4

from .checker import Checker
from .checker_context import build_context
from .frontmatter import FrontmatterState, frontmatter_state
from .links import Resolver
from .rules import compare, structured_errors
from .structured_documents import BaseChange, CanvasChange, edit_base, edit_canvas


class FileRevision(TypedDict):
    path: str
    revision: str


class MutationResult(FileRevision):
    warnings: NotRequired[list[str]]


class TextFile(FileRevision):
    content: str


class TrashedFile(FileRevision):
    trash_id: str


class FileInfo(FileRevision):
    size_bytes: int


class RenamedFile(FileRevision):
    previous_path: str
    updated: list[MutationResult]


class VaultError(ValueError):
    pass


def _parts(path: str, *, obsidian: bool = False) -> tuple[str, ...]:
    parts = tuple(path.split('/'))
    if '\\' in path or '\0' in path or any(not part or (part.startswith('.') and not (obsidian and index == 0 and part == '.obsidian'))
                                          for index, part in enumerate(parts)):
        raise VaultError('Use a non-hidden path relative to the vault root')
    return parts


class Vault:
    max_read_bytes = 1_048_576

    def __init__(self, root: Path, allowed: list[str], *, checker: Checker | None = None,
                 protect_frontmatter: bool = False, checks: dict[str, object] | None = None,
                 read_obsidian: bool = True, canvas: bool = True, bases: bool = True,
                 media_gallery: bool = False):
        from .graph import LinkGraph

        self.canvas = canvas
        self.bases = bases
        self.media_gallery = media_gallery
        self.read_obsidian = read_obsidian
        self.graph = LinkGraph(self)
        self.checker = checker
        self.protect_frontmatter = protect_frontmatter
        self.checks = {key: value for key, value in (checks or {}).items()
                       if (key != 'canvas' or canvas) and (key != 'bases' or bases)}
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

    def _read_parts(self, path: str) -> tuple[str, ...]:
        if self.read_obsidian and path.startswith('.obsidian/'):
            return _parts(path, obsidian=True)
        return self._allowed_parts(path)

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

    def visible_files(self) -> list[str]:
        found: list[str] = []
        for base, dirs, names in os.walk(self.root, followlinks=False):
            directory = Path(base)
            dirs[:] = [name for name in dirs
                       if not name.startswith('.') and not (directory / name).is_symlink()]
            for name in names:
                file = directory / name
                if name.startswith('.') or file.is_symlink() or not file.is_file():
                    continue
                relative = file.relative_to(self.root).as_posix()
                try:
                    self._allowed_parts(relative)
                except VaultError:
                    continue
                found.append(relative)
        return sorted(found)

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
        parts = self._read_parts(path)
        try:
            with self._open(parts) as fd:
                note, _ = self._read_text(path, fd)
                return note
        except OSError:
            raise VaultError('The file does not exist or is not accessible') from None

    def file_size(self, path: str) -> int:
        parts = self._allowed_parts(path)
        try:
            with self._open(parts) as fd:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise VaultError('The path must name a regular file')
                return info.st_size
        except OSError:
            raise VaultError('The file does not exist or is not accessible') from None

    @staticmethod
    def _binary_stamp(fd: int, limit: int) -> str:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise VaultError('The path must name a regular file')
        if not 0 < info.st_size <= limit:
            raise VaultError('The attachment is empty or exceeds the size limit')
        stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        return sha256(repr(stamp).encode('ascii')).hexdigest()

    def attachment_stamp(self, path: str, limit: int) -> str:
        try:
            with self._open(self._allowed_parts(path)) as fd:
                return self._binary_stamp(fd, limit)
        except OSError:
            raise VaultError('The attachment does not exist or is not accessible') from None

    def read_attachment(self, path: str, limit: int) -> tuple[str, bytes]:
        try:
            with self._open(self._allowed_parts(path)) as fd:
                stamp = self._binary_stamp(fd, limit)
                with os.fdopen(fd, 'rb', closefd=False) as file:
                    data = file.read(limit + 1)
                if len(data) > limit or self._binary_stamp(fd, limit) != stamp:
                    raise VaultError('The attachment changed during indexing')
        except OSError:
            raise VaultError('The attachment does not exist or is not accessible') from None
        if self.attachment_stamp(path, limit) != stamp:
            raise VaultError('The attachment changed during indexing')
        return stamp, data

    def stat(self, path: str) -> FileInfo:
        parts = self._read_parts(path)
        try:
            with self._directory(parts[:-1]) as parent:
                revision, mode, size = self._binary_revision(parent, parts[-1])
        except OSError:
            raise VaultError('The file does not exist or is not accessible') from None
        return {'path': path, 'revision': revision, 'size_bytes': size}

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
                elif self.checker is not None or self.protect_frontmatter:
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
        if not old:
            raise VaultError('The old fragment must not be empty')

        def replace(content: str) -> str:
            start = content.find(old)
            if start == -1 or content.find(old, start + 1) != -1:
                raise VaultError('The old fragment must occur exactly once')
            return content[:start] + new + content[start + len(old):]

        return self._update_with(path, expected_revision, replace)

    def canvas_edit(self, path: str, expected_revision: str, changes: list[CanvasChange]) -> MutationResult:
        if not path.lower().endswith('.canvas'):
            raise VaultError('The path must name a Canvas')
        return self._update_with(path, expected_revision, lambda text: edit_canvas(text, changes))

    def base_edit(self, path: str, expected_revision: str, changes: list[BaseChange]) -> MutationResult:
        if not path.lower().endswith('.base'):
            raise VaultError('The path must name a Base')
        return self._update_with(path, expected_revision, lambda text: edit_base(text, changes))

    def _update_with(self, path: str, expected_revision: str, transform: Callable[[str], str]) -> MutationResult:
        parts = self._allowed_parts(path)
        name = parts[-1]
        try:
            with self._mutation_lock, self._directory(parts[:-1]) as parent:
                note, mode = self._check_revision(path, parent, name, expected_revision)
                content = note['content']
                try:
                    after = transform(content)
                except ValueError as error:
                    raise VaultError(str(error)) from None
                return self._store(path, parent, name, after, mode, operation='update', before=content)
        except OSError:
            raise VaultError('Cannot update this path') from None

    def _edit_plugin_settings(self, path: str, transform: Callable[[str], str]) -> None:
        if not self.read_obsidian or not path.startswith('.obsidian/plugins/'):
            raise VaultError('Plugin settings are not accessible')
        parts = _parts(path, obsidian=True)
        try:
            with self._mutation_lock, self._directory(parts[:-1]) as parent:
                name = parts[-1]
                with self._entry(parent, name) as fd:
                    note, mode = self._read_text(path, fd)
                data = transform(note['content']).encode('utf-8')
                if len(data) > self.max_read_bytes:
                    raise VaultError('The plugin settings exceed the size limit')
                temporary = self._stage(parent, data, mode)
                try:
                    self._check_revision(path, parent, name, note['revision'])
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                finally:
                    with suppress(FileNotFoundError):
                        os.unlink(temporary, dir_fd=parent)
        except (OSError, UnicodeError) as error:
            raise VaultError('Cannot update the plugin settings') from error
        except (ValueError, RecursionError) as error:
            if isinstance(error, VaultError):
                raise
            raise VaultError('Cannot parse or serialize the plugin settings') from error

    def delete(self, path: str, expected_revision: str, trash_dir: Path) -> TrashedFile:
        parts = self._allowed_parts(path)
        if parts in self.rules:
            raise VaultError('Cannot delete a path named in the allowlist')
        name = parts[-1]
        try:
            with self._mutation_lock, self._directory(parts[:-1]) as parent:
                self._binary_revision(parent, name, expected_revision)
                self.graph.check_removal(path)
                self._binary_revision(parent, name, expected_revision)
                trash_id = self._move_to_trash(parent, name, path, trash_dir)
        except OSError:
            raise VaultError('Cannot move this file to the trash; check the directory and mount') from None
        return {'path': path, 'revision': expected_revision, 'trash_id': trash_id}

    def _binary_revision(self, parent: int, name: str, expected: str | None = None) -> tuple[str, int, int]:
        with self._entry(parent, name) as fd, os.fdopen(fd, 'rb', closefd=False) as file:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise VaultError('The path must name a regular file')
            revision = file_digest(file, 'sha256').hexdigest()
            if expected is not None and revision != expected:
                raise VaultError('The revision does not match; inspect the file again')
            return revision, info.st_mode, info.st_size

    def rename(self, path: str, new_name: str, expected_revision: str) -> RenamedFile:
        parts = self._allowed_parts(path)
        if parts in self.rules:
            raise VaultError('Cannot rename a path named in the allowlist')
        names = _parts(new_name)
        if len(names) != 1 or new_name != new_name.strip() or any(ord(char) < 32 for char in new_name):
            raise VaultError('Pass a file name, not a path')
        name = parts[-1]
        if name == new_name or Path(name).suffix.lower() != Path(new_name).suffix.lower():
            raise VaultError('Rename must change the name and keep the file extension')
        new_path = '/'.join((*parts[:-1], new_name))
        self._allowed_parts(new_path)
        try:
            with self._mutation_lock, self._directory(parts[:-1]) as parent:
                self._binary_revision(parent, name, expected_revision)
                try:
                    os.stat(new_name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise VaultError('The new path already exists')
                plan = self.graph.plan_rename(path, new_path)
                return self._apply_rename(parent, name, new_name, path, new_path, expected_revision, plan)
        except OSError:
            raise VaultError('Cannot rename this file or update its references') from None

    def _apply_rename(self, parent: int, name: str, new_name: str, path: str, new_path: str,
                      revision: str, plan: dict[str, tuple[TextFile, str]]) -> RenamedFile:
        overlay = {new_path: ''}
        overlay.update({new_path if source == path else source: text for source, (_, text) in plan.items()})
        if path not in plan and path.lower().endswith(('.md', '.base')):
            overlay[new_path] = self.read(path)['content']
        resolver = Resolver(self, overlay, removed=frozenset({path}))
        updated: list[MutationResult] = []
        staged: list[tuple[str, int, str, str, str]] = []
        retain: set[str] = set()
        renamed_stage = name
        new_revision = revision
        with ExitStack() as stack:
            def cleanup(fd: int, temporary: str) -> None:
                if temporary not in retain:
                    with suppress(FileNotFoundError):
                        os.unlink(temporary, dir_fd=fd)

            for source, (note, content) in plan.items():
                destination = new_path if source == path else source
                data, warnings = self._validate_write(destination, content, operation='update',
                                                      before=note['content'], resolver=resolver)
                source_parts = self._allowed_parts(source)
                fd = parent if source == path else stack.enter_context(self._directory(source_parts[:-1]))
                entry = source_parts[-1]
                _, mode, _ = self._binary_revision(fd, entry, note['revision'])
                temporary = self._stage(fd, data, mode)
                stack.callback(cleanup, fd, temporary)
                digest = sha256(data).hexdigest()
                result: MutationResult = {'path': destination, 'revision': digest}
                if warnings:
                    result['warnings'] = warnings
                updated.append(result)
                if source == path:
                    renamed_stage, new_revision = temporary, digest
                    continue
                backup = self._stage(fd, note['content'].encode('utf-8'), mode)
                stack.callback(cleanup, fd, backup)
                staged.append((source, fd, entry, temporary, backup))

            self._binary_revision(parent, name, revision)
            for source, fd, entry, _, _ in staged:
                self._binary_revision(fd, entry, plan[source][0]['revision'])
            os.link(renamed_stage, new_name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            applied: list[tuple[str, int, str, str, str]] = []
            try:
                for item in staged:
                    _, fd, entry, temporary, _ = item
                    os.replace(temporary, entry, src_dir_fd=fd, dst_dir_fd=fd)
                    applied.append(item)
                os.unlink(name, dir_fd=parent)
            except OSError:
                recovery: list[str] = []
                for source, fd, entry, _, backup in reversed(applied):
                    try:
                        os.replace(backup, entry, src_dir_fd=fd, dst_dir_fd=fd)
                    except OSError:
                        retain.add(backup)
                        recovery.append(str(Path(source).with_name(backup)))
                if not recovery:
                    try:
                        os.unlink(new_name, dir_fd=parent)
                    except OSError:
                        recovery.append(new_path)
                if recovery:
                    raise VaultError('Cannot roll back rename. Check: ' + ', '.join(recovery)) from None
                raise
        return {'previous_path': path, 'path': new_path, 'revision': new_revision, 'updated': updated}

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

    @staticmethod
    def _frontmatter_warnings(before: str | None, after: str) -> list[str]:
        old = frontmatter_state(before or '')
        new = frontmatter_state(after)
        if new is FrontmatterState.INVALID:
            if old is FrontmatterState.VALID:
                raise VaultError('The change would make the YAML frontmatter invalid')
            return ['The YAML frontmatter is invalid']
        if old is FrontmatterState.VALID and new is FrontmatterState.ABSENT:
            return ['The change removes the YAML frontmatter']
        return []

    def _validate_write(self, path: str, content: str, *, operation: str, before: str | None,
                        resolver: Resolver | None = None) -> tuple[bytes, list[str]]:
        if '\0' in content:
            raise VaultError('The content must be UTF-8 text without NUL characters')
        try:
            data = content.encode('utf-8')
        except UnicodeEncodeError:
            raise VaultError('The content must be UTF-8 text') from None
        if len(data) > self.max_read_bytes:
            raise VaultError('The content exceeds the file size limit')
        enabled = (self.canvas or not path.lower().endswith('.canvas')) and (self.bases or not path.lower().endswith('.base'))
        malformed = structured_errors(path, content) if enabled else []
        if malformed:
            raise VaultError('The change would produce an invalid document: ' + '; '.join(malformed[:6]))
        warnings = []
        if self.protect_frontmatter and path.lower().endswith('.md'):
            warnings.extend(self._frontmatter_warnings(before, content))
        if resolver is None and (self.checker or self.checks.get('broken_links')):
            resolver = Resolver(self, {path: content})
        if any(self.checks.values()):
            errors, found = compare(before, content, path, self.checks, resolver)
            if errors:
                raise VaultError('The change breaks a vault rule: ' + '; '.join(errors[:6]))
            warnings.extend(found)
        if self.checker and resolver is not None:
            links = resolver.context(path, content)
            properties = self.checker.vault_context_properties
            context = (build_context(resolver, path, before, content, properties)
                       if properties and path.lower().endswith('.md') else None)
            warnings.extend(self.checker.check(operation, path, before, content, links,
                                               vault_context=context))
        return data, warnings

    @staticmethod
    def _stage(parent: int, data: bytes, mode: int | None) -> str:
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
        except BaseException:
            os.unlink(temp, dir_fd=parent)
            raise
        return temp

    def _store(self, path: str, parent: int, name: str, content: str, mode: int | None,
               *, operation: str, before: str | None) -> MutationResult:
        data, warnings = self._validate_write(path, content, operation=operation, before=before)
        temp = self._stage(parent, data, mode)
        try:
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
