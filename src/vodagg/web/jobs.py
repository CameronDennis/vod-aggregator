"""Long-running work (scans, syncs, exports) run in the background for the web UI.

Jobs live in memory: the page polls them for progress, and a restart forgets
them, which is fine because everything they produce is saved in the database
or on disk.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field


@dataclass
class Job:
    id: int
    kind: str
    label: str
    status: str = "waiting"  # waiting, running, done or failed
    done: int = 0
    total: int = 0
    message: str = ""
    error: str | None = None
    result: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def step(self, done: int | None = None, total: int | None = None, message: str | None = None) -> None:
        if done is not None:
            self.done = done
        if total is not None:
            self.total = total
        if message is not None:
            self.message = message

    def to_dict(self) -> dict:
        return asdict(self)


class Jobs:
    def __init__(self, workers: int = 2):
        self._pool = ThreadPoolExecutor(max_workers=workers)
        self._jobs: dict[int, Job] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def start(self, kind: str, label: str, work: Callable[[Job], dict | None]) -> Job:
        with self._lock:
            job = Job(next(self._ids), kind, label)
            self._jobs[job.id] = job

        def run() -> None:
            job.status = "running"
            try:
                job.result = work(job) or {}
                job.status = "done"
            except Exception as error:  # Reported on the page instead of lost in a thread.
                job.error = str(error) or type(error).__name__
                job.status = "failed"

        self._pool.submit(run)
        return job

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda job: job.id, reverse=True)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
