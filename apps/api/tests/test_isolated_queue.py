"""隔离子进程取结果：必须边等边排空队列，不能 join 后再 get。

回归背景：全市场快照 pickle 后约 500KB，远超 mp.Queue 管道缓冲区(~64KB)。
若父进程先 proc.join() 再 result_q.get()，子进程会阻塞在 put() 等父进程读取，
父进程又在等子进程退出 -> 互等死锁，最终超时被 kill，表现为前端「没有拿到真实行情报价」。
"""
from __future__ import annotations

import multiprocessing as mp
import time

import numpy as np
import pandas as pd
import pytest

from providers.isolated import _wait_for_payload


class _FakeProc:
    """模拟一个"还活着"的子进程。"""

    def __init__(self, alive: bool = True):
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


def _put_big(q) -> None:
    """子进程入口：spawn 要求可 pickle，必须是模块级函数。"""
    q.put({"ok": True, "value": _big_df(rows=50000)})


def _big_df(rows: int = 20000) -> pd.DataFrame:
    """构造远大于管道缓冲区的数据（约 3MB+）。"""
    return pd.DataFrame(np.random.rand(rows, 20))


def test_wait_for_payload_returns_big_object():
    """大对象必须能取回（排空队列），而不是等到超时返回 None。"""
    q: mp.Queue = mp.get_context("spawn").Queue()
    payload = {"ok": True, "value": _big_df()}
    q.put(payload)

    got = _wait_for_payload(_FakeProc(alive=True), q, timeout=5)
    assert got is not None
    assert got["ok"] is True
    assert got["value"].shape == (20000, 20)


def test_wait_for_payload_deadlocks_if_join_first():
    """反例：先 join 再 get 会死锁（此测试固化该行为，证明修复确有必要）。

    子进程侧的 put 由 feeder 线程写入；父进程若霸占着不读，
    put 会在缓冲区满后挂起，子进程永不退出。
    """
    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()

    proc = ctx.Process(target=_put_big, args=(q,), daemon=True)
    proc.start()

    # 复刻旧实现：先 join（等子进程退出），再取结果
    proc.join(4)
    blocked = proc.is_alive()
    # 清理
    if proc.is_alive():
        proc.terminate()
        proc.join(3)

    # 旧行为下子进程被 put 卡住 -> 4 秒后仍活着
    assert blocked is True


def test_wait_for_payload_timeout_returns_none():
    """队列一直为空，应在 timeout 后返回 None。"""
    q: mp.Queue = mp.get_context("spawn").Queue()
    t0 = time.monotonic()
    got = _wait_for_payload(_FakeProc(alive=True), q, timeout=0.3)
    elapsed = time.monotonic() - t0
    assert got is None
    assert elapsed < 2.0


def test_wait_for_payload_reads_after_child_exit():
    """子进程已退出但结果还在队列里，应能取回。"""
    q: mp.Queue = mp.get_context("spawn").Queue()
    q.put({"ok": True, "value": "small"})
    got = _wait_for_payload(_FakeProc(alive=False), q, timeout=5)
    assert got == {"ok": True, "value": "small"}


def test_wait_for_payload_empty_and_child_dead():
    """队列空且子进程已退出 -> None（不算超时）。"""
    q: mp.Queue = mp.get_context("spawn").Queue()
    got = _wait_for_payload(_FakeProc(alive=False), q, timeout=5)
    assert got is None
