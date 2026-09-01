"""邀请制账号：注册 / 登录 / JWT。"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import auth_is_required, settings
from db import (
    consume_invite,
    count_users,
    create_invite,
    create_user,
    ensure_local_user,
    get_user_by_id,
    get_user_by_username,
    verify_invite_code,
)
from user_ctx import reset_user_id, use_user_id

_bearer = HTTPBearer(auto_error=False)
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def create_access_token(*, user_id: int, username: str, role: str) -> str:
    secret = (settings.jwt_secret or "").strip()
    if not secret:
        raise HTTPException(500, "JWT_SECRET 未配置")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=max(1, settings.jwt_expire_hours))).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_access_token(token: str) -> dict[str, Any]:
    secret = (settings.jwt_secret or "").strip()
    if not secret:
        raise HTTPException(500, "JWT_SECRET 未配置")
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(401, "登录已过期，请重新登录") from e
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, "无效登录凭证") from e


def public_user(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "role": row.get("role") or "user",
        "created_at": row.get("created_at"),
    }


def register_with_invite(*, username: str, password: str, invite_code: str) -> dict[str, Any]:
    username = (username or "").strip()
    password = password or ""
    invite_code = (invite_code or "").strip().upper()
    if not _USERNAME_RE.match(username):
        raise HTTPException(400, "用户名仅允许字母数字和下划线，长度 2–32")
    if len(password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if not invite_code:
        raise HTTPException(400, "需要邀请码")
    invite = verify_invite_code(invite_code)
    if not invite:
        raise HTTPException(400, "邀请码无效或已使用")
    if get_user_by_username(username):
        raise HTTPException(409, "用户名已存在")
    user = create_user(
        username=username,
        password_hash=hash_password(password),
        role="user",
    )
    consume_invite(invite_code, user_id=int(user["id"]))
    token = create_access_token(
        user_id=int(user["id"]),
        username=user["username"],
        role=user["role"],
    )
    return {"access_token": token, "token_type": "bearer", "user": public_user(user)}


def login(*, username: str, password: str) -> dict[str, Any]:
    username = (username or "").strip()
    user = get_user_by_username(username)
    if not user or not verify_password(password, user["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    if user.get("disabled"):
        raise HTTPException(403, "账号已禁用")
    token = create_access_token(
        user_id=int(user["id"]),
        username=user["username"],
        role=user.get("role") or "user",
    )
    return {"access_token": token, "token_type": "bearer", "user": public_user(user)}


def admin_create_invite(*, created_by: int, note: str = "") -> dict[str, Any]:
    code = secrets.token_urlsafe(9).upper().replace("-", "").replace("_", "")[:12]
    return create_invite(code=code, created_by=created_by, note=note)


def bootstrap_admin(*, username: str, password: str) -> dict[str, Any]:
    if count_users() > 0:
        raise HTTPException(409, "已有用户，请用邀请码注册或直接登录")
    if not (settings.jwt_secret or "").strip():
        raise HTTPException(400, "请先配置 JWT_SECRET")
    username = (username or "").strip()
    if len(username) < 2:
        raise HTTPException(400, "用户名太短")
    if len(password or "") < 6:
        raise HTTPException(400, "密码至少 6 位")
    user = create_user(
        username=username,
        password_hash=hash_password(password),
        role="admin",
    )
    token = create_access_token(
        user_id=int(user["id"]),
        username=user["username"],
        role="admin",
    )
    return {"access_token": token, "token_type": "bearer", "user": public_user(user)}


async def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
):
    if not auth_is_required():
        user = ensure_local_user()
        token = use_user_id(int(user["id"]))
        request.state.user = user
        try:
            yield user
        finally:
            reset_user_id(token)
        return

    if not creds or not creds.credentials:
        raise HTTPException(401, "请先登录")
    payload = decode_access_token(creds.credentials)
    try:
        uid = int(payload.get("sub"))
    except (TypeError, ValueError) as e:
        raise HTTPException(401, "无效登录凭证") from e
    user = get_user_by_id(uid)
    if not user or user.get("disabled"):
        raise HTTPException(401, "账号不存在或已禁用")
    token = use_user_id(int(user["id"]))
    request.state.user = user
    try:
        yield user
    finally:
        reset_user_id(token)


async def require_admin(user: dict[str, Any] = Depends(get_current_user)):
    if (user.get("role") or "") != "admin":
        raise HTTPException(403, "需要管理员权限")
    yield user
