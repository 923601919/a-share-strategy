"""JobStore concurrency / cancel smoke tests."""
from __future__ import annotations

import threading
import time

from services.jobs import JobStore


def test_max_concurrent_queues_second_job():
    store = JobStore(max_jobs=20, max_concurrent=1)
    started: list[str] = []
    gate = threading.Event()

    def slow():
        started.append("a")
        gate.wait(2)
        store.update(job_a.id, status="done", stage="done", progress=1.0, message="ok")

    def fast():
        started.append("b")
        store.update(job_b.id, status="done", stage="done", progress=1.0, message="ok")

    job_a = store.create("scan", {})
    job_b = store.create("scan", {})
    store.run_in_background(job_a.id, slow)
    store.run_in_background(job_b.id, fast)

    deadline = time.time() + 2
    while time.time() < deadline and not started:
        time.sleep(0.02)
    time.sleep(0.1)
    ja = store.get(job_a.id)
    assert ja and ja.status == "running"
    assert started == ["a"]

    gate.set()
    deadline = time.time() + 2
    while time.time() < deadline:
        if store.get(job_a.id).status == "done" and store.get(job_b.id).status == "done":
            break
        time.sleep(0.05)
    assert store.get(job_a.id).status == "done"
    assert store.get(job_b.id).status == "done"
    assert started == ["a", "b"]


def test_cancel_while_queued():
    store = JobStore(max_jobs=10, max_concurrent=1)
    hold = threading.Event()

    def blocker():
        hold.wait(2)
        store.update(job1.id, status="done", stage="done", progress=1.0, message="ok")

    job1 = store.create("scan", {})
    job2 = store.create("scan", {})
    store.run_in_background(job1.id, blocker)
    store.run_in_background(job2.id, lambda: store.update(job2.id, status="done", stage="done", progress=1.0))
    deadline = time.time() + 2
    while time.time() < deadline:
        j1 = store.get(job1.id)
        if j1 and j1.status == "running":
            break
        time.sleep(0.02)
    assert store.request_cancel(job2.id)
    hold.set()
    deadline = time.time() + 2
    while time.time() < deadline:
        if store.get(job2.id).status in ("cancelled", "done"):
            break
        time.sleep(0.05)
    assert store.get(job2.id).status == "cancelled"


def test_mark_inflight_lost():
    store = JobStore(max_jobs=10, max_concurrent=2)
    job = store.create("scan", {})
    store.update(job.id, status="running", stage="x")
    assert store.mark_inflight_lost() == 1
    assert store.get(job.id).status == "lost"
