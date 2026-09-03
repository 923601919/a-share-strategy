from __future__ import annotations

import logging
import multiprocessing as mp
import queue as _queue
import time
import traceback
from typing import Any

logger = logging.getLogger("market.isolated")

# Windows 下必须用 spawn；模块级入口才能被 pickle
_CTX = mp.get_context("spawn")


def _worker(fn_name: str, args: tuple, kwargs: dict, result_q: Any) -> None:
    """子进程入口：执行 akshare_client 上的具名函数。"""
    try:
        from providers import akshare_client as mkt

        fn = getattr(mkt, fn_name, None)
        if fn is None:
            result_q.put({"ok": False, "error": f"unknown fn: {fn_name}"})
            return
        value = fn(*args, **kwargs)
        result_q.put({"ok": True, "value": value})
    except Exception as e:
        result_q.put(
            {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(limit=6),
            }
        )


def _terminate(proc: Any) -> None:
    """terminate -> join -> kill 的收尾。"""
    try:
        proc.terminate()
        proc.join(3)
        if proc.is_alive():
            proc.kill()
            proc.join(1)
    except Exception:
        pass


def _wait_for_payload(proc: Any, result_q: Any, timeout: float) -> Any:
    """
    边等边排空队列，返回子进程放入的结果（拿不到则返回 None）。

    不能写成 `proc.join(timeout)` 再 `result_q.get()`：结果超过管道缓冲区(~64KB)
    时子进程会阻塞在 put() 上等父进程读取，父进程又在等子进程退出 -> 互等死锁。
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            return result_q.get(timeout=0.05)
        except _queue.Empty:
            pass
        if not proc.is_alive():
            # 子进程已退出，最后再取一次它可能已写入的结果
            try:
                return result_q.get_nowait()
            except Exception:
                return None
        if time.monotonic() >= deadline:
            return None


def call_isolated(
    fn_name: str,
    *,
    timeout: float = 20.0,
    args: tuple = (),
    kwargs: dict | None = None,
) -> Any:
    """
    在独立进程执行行情函数。
    超时则 terminate；子进程崩溃不会拖垮主 API。
    """
    kwargs = kwargs or {}
    result_q: mp.Queue = _CTX.Queue()
    proc = _CTX.Process(
        target=_worker,
        args=(fn_name, args, kwargs, result_q),
        daemon=True,
        name=f"mkt-{fn_name}",
    )
    proc.start()

    # 关键：必须"边等边排空队列"，不能先 proc.join()。
    # 子进程 result_q.put() 大对象（如全市场快照 pickle 后 ~500KB）时，
    # 远超管道缓冲区(~64KB)，会阻塞在 put() 上等父进程读取；
    # 若父进程此时在 proc.join() 里等子进程退出，双方互等 -> 死锁到超时被 kill。
    payload = _wait_for_payload(proc, result_q, timeout)

    # 只有"等到截止时间仍无结果"才算超时；拿到结果的情况不该报 timeout
    if payload is None and proc.is_alive():
        logger.warning("isolated %s timeout after %.1fs, terminating", fn_name, timeout)
        _terminate(proc)
        raise TimeoutError(f"{fn_name} timed out after {timeout}s (subprocess killed)")

    # 拿到结果后子进程还需 flush 队列缓冲/atexit，给它短暂宽限期再判定为"滞留"
    proc.join(3)
    if proc.is_alive():
        logger.debug("isolated %s finished but process lingering, terminating", fn_name)
        _terminate(proc)

    if payload is None:
        if proc.exitcode not in (0, None):
            raise RuntimeError(f"{fn_name} subprocess exited with code {proc.exitcode}")
        raise RuntimeError(f"{fn_name} subprocess returned no result")

    if not payload.get("ok"):
        raise RuntimeError(payload.get("error") or f"{fn_name} failed")
    return payload.get("value")
