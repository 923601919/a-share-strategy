from __future__ import annotations

import logging
import multiprocessing as mp
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
    proc.join(timeout)

    if proc.is_alive():
        logger.warning("isolated %s timeout after %.1fs, terminating", fn_name, timeout)
        proc.terminate()
        proc.join(3)
        if proc.is_alive():
            proc.kill()
            proc.join(1)
        raise TimeoutError(f"{fn_name} timed out after {timeout}s (subprocess killed)")

    if proc.exitcode not in (0, None) and result_q.empty():
        raise RuntimeError(f"{fn_name} subprocess exited with code {proc.exitcode}")

    try:
        payload = result_q.get_nowait()
    except Exception as e:
        raise RuntimeError(f"{fn_name} subprocess returned no result: {e}") from e

    if not payload.get("ok"):
        raise RuntimeError(payload.get("error") or f"{fn_name} failed")
    return payload.get("value")
