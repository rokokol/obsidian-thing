import logging
import sqlite3
from datetime import datetime, timezone
from threading import Event, Thread

from croniter import croniter

from .search import SearchIndex

_log = logging.getLogger(__name__)


class EmbeddingSchedule:
    def __init__(self, index: SearchIndex, config: dict):
        enabled = config.get('background', False)
        expression = config.get('schedule', '0 3 * * *')
        if not isinstance(enabled, bool):
            raise ValueError('Configure embeddings.background as a boolean')
        if not isinstance(expression, str) or len(expression.split()) != 5 or not croniter.is_valid(expression):
            raise ValueError('Configure embeddings.schedule as a five-field cron expression')
        self.index = index
        self.enabled = enabled
        self.expression = expression
        self.stopping = Event()
        self.thread = Thread(target=self._run, name='vault embedding schedule')

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.thread.join()

    def next_run(self, now: datetime) -> datetime:
        return croniter(self.expression, now).get_next(datetime)

    def _run(self) -> None:
        while not self.stopping.is_set():
            try:
                due = self.next_run(datetime.now(timezone.utc))
            except ValueError:
                _log.error('The embedding schedule has no next run')
                return
            while not self.stopping.is_set():
                remaining = (due - datetime.now(timezone.utc)).total_seconds()
                if remaining <= 0:
                    break
                self.stopping.wait(min(remaining, 60))
            if self.stopping.is_set():
                return
            try:
                self.index.reconcile_local(stop=self.stopping.is_set)
                if not self.index.run_pending(stop=self.stopping.is_set) and not self.stopping.is_set():
                    _log.warning('The background embedding run did not finish; pending work remains')
            except (OSError, sqlite3.Error):
                _log.warning('The background embedding run failed; pending work remains')
