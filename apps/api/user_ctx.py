"""按请求隔离的当前用户上下文（同步路由 + 后台线程需显式 user_scope）。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token, copy_context
from typing import Any, Callable, Iterator, TypeVar

_user_id: ContextVar[int | None] = ContextVar("user_id", default=None)

_T = TypeVar("_T")


def get_user_id() -> int | None:
    return _user_id.get()


def require_user_id() -> int:
    uid = _user_id.get()
    if uid is None:
        raise RuntimeError("missing user context")
    return int(uid)


def use_user_id(user_id: int) -> Token:
    return _user_id.set(int(user_id))


def reset_user_id(token: Token) -> None:
    _user_id.reset(token)


@contextmanager
def user_scope(user_id: int) -> Iterator[int]:
    token = use_user_id(user_id)
    try:
        yield int(user_id)
    finally:
        reset_user_id(token)


def run_in_user_context(user_id: int, fn: Callable[[], _T]) -> _T:
    with user_scope(user_id):
        return fn()


def map_with_user_context(fn: Callable[[Any], _T], items: list[Any]) -> list[_T]:
    """在线程池外预拷贝 ContextVar，供 ThreadPoolExecutor 使用。"""
    ctx = copy_context()
    return [ctx.run(fn, it) for it in items]
