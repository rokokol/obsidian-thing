import logging
import os
import sqlite3
import sys
from pathlib import Path
from random import uniform
from threading import Event, Thread
from time import monotonic

from .search import SearchIndex

if sys.platform == 'linux':
    from inotify_simple import INotify, flags

_log = logging.getLogger(__name__)


class IndexWatcher:
    def __init__(self, index: SearchIndex):
        self.index = index
        self.stopping = Event()
        self.thread = Thread(target=self._run, name='vault index watcher', daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.thread.join()

    def _delay(self) -> float:
        return self.index.policy.scan_seconds * uniform(0.9, 1.1)

    def _reconcile(self, paths: set[str] | None = None) -> None:
        try:
            self.index.reconcile_local(paths, stop=self.stopping.is_set)
            if self.index.metrics is not None and paths is None and not self.stopping.is_set():
                self.index.metrics.last_scan.set_to_current_time()
        except (OSError, sqlite3.Error):
            if self.index.metrics is not None:
                self.index.metrics.scan_errors.labels('full' if paths is None else 'partial').inc()
            _log.warning('Vault index reconciliation failed; the next full scan will retry')

    def _run(self) -> None:
        while not self.stopping.is_set():
            if sys.platform == 'linux':
                try:
                    self._watch_linux()
                    continue
                except OSError:
                    _log.warning('Vault event watch failed; use a full scan until the watch can restart')
            self._reconcile()
            self.stopping.wait(self._delay())

    def _watch_linux(self) -> None:
        if sys.platform != 'linux':
            return
        root = self.index.vault.root
        changes = flags.CREATE | flags.DELETE | flags.MOVED_FROM | flags.MOVED_TO
        self_change = flags.MOVE_SELF | flags.DELETE_SELF | flags.UNMOUNT
        directory = flags.ONLYDIR | flags.DONT_FOLLOW
        with INotify() as watch:
            parent = watch.add_watch(str(root.parent), changes | self_change | directory)
            mask = changes | self_change | directory | flags.CLOSE_WRITE | flags.ATTRIB
            watched: dict[int, Path] = {}
            for base, dirs, _ in os.walk(root, followlinks=False):
                if self.stopping.is_set():
                    return
                here = Path(base)
                prefix = here.relative_to(root).parts
                dirs[:] = [name for name in dirs if not name.startswith('.') and
                           self._can_descend((*prefix, name)) and not (here / name).is_symlink()]
                try:
                    watched[watch.add_watch(base, mask)] = here
                except FileNotFoundError:
                    continue
            self._reconcile()
            deadline = monotonic() + self._delay()
            while not self.stopping.is_set():
                if monotonic() >= deadline:
                    return
                paths: set[str] = set()
                for event in watch.read(timeout=250, read_delay=50):
                    if event.mask & (flags.Q_OVERFLOW | flags.IGNORED | self_change):
                        return
                    if event.wd == parent:
                        if event.name == root.name:
                            return
                        continue
                    if event.name == '.gitignore' and self.index.policy.respect_gitignore:
                        return
                    if not event.name or event.name.startswith('.'):
                        continue
                    if event.mask & flags.ISDIR:
                        return
                    base = watched.get(event.wd)
                    if base is not None:
                        paths.add((base / event.name).relative_to(root).as_posix())
                if paths:
                    self._reconcile(paths)

    def _can_descend(self, path: tuple[str, ...]) -> bool:
        return any(rule[:len(path)] == path or (recursive and path[:len(rule)] == rule)
                   for rule, recursive in self.index.vault.rules.items())
