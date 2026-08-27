from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("jobs")


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"  # queued | running | done | error | cancelled
    stage: str = "queued"
    progress: float = 0.0
    message: str = ""
    error: str | None = None
    error_code: str | None = None
    result: Any = None
    timings: dict[str, float] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_flag: bool = False
    params: dict[str, Any] = field(default_factory=dict)

    def to_public(self, *, include_result: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "job_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress, 3),
            "message": self.message,
            "error": self.error,
            "error_code": self.error_code,
            "timings": self.timings,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "params": self.params,
        }
        if include_result and self.status == "done":
            out["result"] = self.result
        return out


class JobStore:
    def __init__(self, *, max_jobs: int = 40) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._max_jobs = max_jobs
        self._executor_lock = threading.Lock()

    def create(self, kind: str, params: dict[str, Any] | None = None) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, params=params or {})
        with self._lock:
            self._jobs[job.id] = job
            self._prune_locked()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **fields: Any) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            for k, v in fields.items():
                if hasattr(job, k):
                    setattr(job, k, v)
            job.updated_at = time.time()
            return job

    def request_cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job.status in ("done", "error", "cancelled"):
                return False
            job.cancel_flag = True
            job.message = "正在取消…"
            job.updated_at = time.time()
            return True

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and job.cancel_flag)

    def _prune_locked(self) -> None:
        if len(self._jobs) <= self._max_jobs:
            return
        # 丢掉最旧的已完成任务
        finished = sorted(
            (j for j in self._jobs.values() if j.status in ("done", "error", "cancelled")),
            key=lambda j: j.updated_at,
        )
        while len(self._jobs) > self._max_jobs and finished:
            old = finished.pop(0)
            self._jobs.pop(old.id, None)

    def run_in_background(self, job_id: str, fn: Callable[[], None]) -> None:
        def _wrap() -> None:
            try:
                self.update(job_id, status="running", stage="start", progress=0.01, message="开始")
                fn()
            except Exception as e:
                logger.exception("job %s failed", job_id)
                self.update(
                    job_id,
                    status="error",
                    stage="error",
                    progress=1.0,
                    error=str(e),
                    error_code="internal",
                    message=f"失败: {e}",
                )

        t = threading.Thread(target=_wrap, name=f"job-{job_id}", daemon=True)
        t.start()


job_store = JobStore()
