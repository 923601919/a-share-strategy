#!/usr/bin/env python3
"""验证新规则：触发扫描（auto session同步返回），检查是否带"进攻后强势整理"加分。"""
import json
import os
import urllib.request

BASE = os.environ.get("ASHARE_BASE_URL", "http://124.222.194.44")
USER = os.environ.get("ASHARE_USER", "")
PWD = os.environ.get("ASHARE_PASSWORD", "")


def http(method, path, body=None, token=None, timeout=60):
    req = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main():
    if not USER or not PWD:
        raise SystemExit(
            "缺少账密环境变量（账密不入库 —— 本仓库是公开的）。用法：\n"
            "  ASHARE_USER=soft ASHARE_PASSWORD=xxx python scripts/verify_consolidation_rule.py\n"
            "可选：ASHARE_BASE_URL 覆盖服务器地址"
        )
    token = http("POST", "/api/auth/login",
                 {"username": USER, "password": PWD})["access_token"]
    print(f"登录 ok，token_len={len(token)}")

    # auto session 同步返回（带结果）
    # 当前 13:14，未到下午窗 13:30。改用 any 不限时段
    body = {"session": "any", "top_n": 10, "mode": "fenshi",
            "universe_policy": "hot_only"}
    print(f"请求: {body}")
    resp = http("POST", "/api/scan/jobs", body, token=token)
    print(f"\n顶层 keys: {list(resp.keys())}")
    print(f"status: {resp.get('status')}, error_code: {resp.get('error_code')}, "
          f"message: {resp.get('message')}")

    jid = resp.get("job_id")
    print(f"\n轮询 job={jid}")
    import time
    for i in range(30):
        time.sleep(4)
        d = http("GET", f"/api/scan/jobs/{jid}", token=token)
        st = d.get("status")
        msg = d.get("message") or ""
        prog = d.get("progress")
        print(f"  poll#{i+1} status={st} progress={prog} message={msg}", end="\r")
        if st in ("done", "failed", "error"):
            print()
            break
    else:
        print("\n超时")

    r = d.get("result") or {}
    print(f"\nresult keys: {list(r.keys())}")
    print(f"session_note: {r.get('session_note')}")
    print(f"count: {r.get('count')}, scanned: {r.get('scanned')}")

    items = r.get("items") or []
    print(f"\n命中 {len(items)} 只，前 8 条:")
    triggered = 0
    for it in items[:8]:
        code = it.get("code")
        name = it.get("name")
        sc = it.get("score")
        rs = it.get("reasons") or []
        cons = [x for x in rs if "强势整理" in x or "进攻后" in x]
        if cons:
            triggered += 1
        mark = "[新规则 ✓]" if cons else "            "
        print(f"  {mark} {code} {name} score={sc}")
        if cons:
            print(f"           {cons[0]}")
        else:
            print(f"           理由={rs[:3]}")

    if items:
        print(f"\n>>> 本次命中 {len(items)} 只里 {triggered} 只触发『进攻后强势整理』加分")


if __name__ == "__main__":
    main()