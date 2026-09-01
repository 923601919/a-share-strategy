#!/usr/bin/env python3
"""创建首个管理员，或为已有管理员生成邀请码。

用法（在 apps/api 目录、已装依赖）:
  python ../../scripts/create_admin.py --username alice --password 'secret12'
  python ../../scripts/create_admin.py --invite --username alice --password 'secret12'
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1] / "apps" / "api"
sys.path.insert(0, str(API_ROOT))

from auth import admin_create_invite, hash_password, login  # noqa: E402
from config import settings  # noqa: E402
from db import count_users, create_user, get_user_by_username, init_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap admin / create invite")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument(
        "--invite",
        action="store_true",
        help="为已有管理员生成邀请码（需正确密码）",
    )
    parser.add_argument("--note", default="")
    args = parser.parse_args()

    if not (settings.jwt_secret or "").strip():
        print("请先在 .env 设置 JWT_SECRET（非空随机串）", file=sys.stderr)
        return 1

    init_db()

    if args.invite:
        try:
            session = login(username=args.username, password=args.password)
        except Exception as e:
            print(f"登录失败: {e}", file=sys.stderr)
            return 1
        user = session["user"]
        if user.get("role") != "admin":
            print("需要管理员账号", file=sys.stderr)
            return 1
        inv = admin_create_invite(created_by=int(user["id"]), note=args.note)
        print(f"邀请码: {inv['code']}")
        return 0

    if count_users() > 0:
        if get_user_by_username(args.username):
            print("用户已存在；生成邀请码请加 --invite", file=sys.stderr)
            return 1
        print("已有用户。请用管理员 --invite 生成邀请码后再注册。", file=sys.stderr)
        return 1

    user = create_user(
        username=args.username.strip(),
        password_hash=hash_password(args.password),
        role="admin",
    )
    print(f"已创建管理员 id={user['id']} username={user['username']}")
    print("请用该账号登录前端，并在「邀请」或本脚本 --invite 生成邀请码给朋友。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
