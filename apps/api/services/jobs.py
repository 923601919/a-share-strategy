from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from config import settings

logger = logging.getLogger("jobs")


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"  # queued | running | done | error | cancelled | lost
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
    user_id: int | None = None

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
    def __init__(
        self,
        *,
        max_jobs: int = 40,
        max_concurrent: int | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._max_jobs = max_jobs
        self._max_concurrent = max(1, int(max_concurrent if max_concurrent is not None else settings.scan_max_concurrent))
        self._slots = threading.Semaphore(self._max_concurrent)
        self._worker_threads: dict[str, threading.Thread] = {}

    def create(self, kind: str, params: dict[str, Any] | None = None, *, user_id: int | None = None) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, params=params or {}, user_id=user_id)
        with self._lock:
            self._jobs[job.id] = job
            self._prune_locked()
        return job

    def get(self, job_id: str, *, user_id: int | None = None) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if user_id is not None and job.user_id is not None and int(job.user_id) != int(user_id):
                return None
            return job

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

    def request_cancel(self, job_id: str, *, user_id: int | None = None) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if user_id is not None and job.user_id is not None and int(job.user_id) != int(user_id):
                return False
            if job.status in ("done", "error", "cancelled", "lost"):
                return False
            job.cancel_flag = True
            job.message = "正在取消…"
            job.updated_at = time.time()
            return True

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and job.cancel_flag)

    def mark_inflight_lost(self) -> int:
        """进程启动时：内存里不应有跨重启的 running/queued，标记为 lost。"""
        n = 0
        with self._lock:
            for job in self._jobs.values():
                if job.status in ("queued", "running"):
                    job.status = "lost"
                    job.stage = "lost"
                    job.progress = 1.0
                    job.message = "服务重启，任务已失效"
                    job.error_code = "lost"
                    job.updated_at = time.time()
                    n += 1
        if n:
            logger.info("marked %s in-flight jobs as lost after restart", n)
        return n

    def _prune_locked(self) -> None:
        if len(self._jobs) <= self._max_jobs:
            return
        finished = sorted(
            (
                j
                for j in self._jobs.values()
                if j.status in ("done", "error", "cancelled", "lost")
            ),
            key=lambda j: j.updated_at,
        )
        while len(self._jobs) > self._max_jobs and finished:
            old = finished.pop(0)
            self._jobs.pop(old.id, None)
            self._worker_threads.pop(old.id, None)

    def run_in_background(self, job_id: str, fn: Callable[[], None]) -> None:
        def _wrap() -> None:
            acquired = False
            try:
                self.update(
                    job_id,
                    status="queued",
                    stage="queued",
                    progress=0.0,
                    message=f"排队中（并发上限 {self._max_concurrent}）",
                )
                # 可取消地等待并发槽
                while True:
                    if self.is_cancelled(job_id):
                        self.update(
                            job_id,
                            status="cancelled",
                            stage="cancelled",
                            progress=1.0,
                            message="已取消",
                            error_code="cancelled",
                        )
                        return
                    acquired = self._slots.acquire(timeout=0.5)
                    if acquired:
                        break

                if self.is_cancelled(job_id):
                    self.update(
                        job_id,
                        status="cancelled",
                        stage="cancelled",
                        progress=1.0,
                        message="已取消",
                        error_code="cancelled",
                    )
                    return

                self.update(job_id, status="running", stage="start", progress=0.01, message="开始")
                fn()
                if self.is_cancelled(job_id):
                    cur = self.get(job_id)
                    if cur and cur.status == "running":
                        self.update(
                            job_id,
                            status="cancelled",
                            stage="cancelled",
                            progress=1.0,
                            message="已取消",
                            error_code="cancelled",
                        )
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
            finally:
                if acquired:
                    self._slots.release()
                with self._lock:
                    self._worker_threads.pop(job_id, None)

        t = threading.Thread(target=_wrap, name=f"job-{job_id}", daemon=True)
        with self._lock:
            self._worker_threads[job_id] = t
        t.start()


job_store = JobStore()
