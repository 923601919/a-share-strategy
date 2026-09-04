import sqlite3, os, json
from datetime import datetime, date, timedelta

# 定位 db
for cand in ["apps/api/data/app.db", "data/app.db"]:
    p = os.path.join(os.path.dirname(__file__), cand)
    if os.path.exists(p):
        DB = p
        break
else:
    DB = None
print("DB PATH:", DB)
if not DB:
    raise SystemExit("no db found")

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

def q(sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]

print("\n=== 表清单 ===")
print([r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()])

today = date.today().isoformat()
print("\n=== 今日(诊断机本地) ===", today)

# 所有 watch_tracks（跨用户，诊断用）
tracks = q("SELECT id, code, name, source, entry_price, entry_pct, created_at, removed_at, exit_return_pct FROM watch_tracks ORDER BY created_at DESC")
print(f"\n=== watch_tracks 总数: {len(tracks)} | 已移除: {sum(1 for t in tracks if t['removed_at'])} | 仍在: {sum(1 for t in tracks if not t['removed_at'])} ===")

# 当前 watchlist
wl = q("SELECT code, name, track_id, entry_price, created_at, user_id FROM watchlist")
print(f"=== watchlist 当前条目: {len(wl)} ===")

# 收益落库统计
only_t0 = []      # 只有 day_offset=0 且 return_pct=0 的"占位"签名
no_returns = []   # 完全无收益行
has_t3 = []
stale_t3 = []     # T+3 trade_date 早于今日很多（疑似未更新/未拉全）
t3_unsettled = [] # T+3 落库时间早于其 trade_date 15:00（收盘价可能非最终）

def parse_entry_date(created_at):
    try:
        return datetime.fromisoformat(str(created_at).replace("Z","+00:00")).date().isoformat()
    except Exception:
        return None

for t in tracks:
    tid = t["id"]
    rets = q("SELECT day_offset, trade_date, close_price, return_pct, recorded_at FROM watch_track_returns WHERE track_id=? ORDER BY day_offset", (tid,))
    if not rets:
        no_returns.append(t)
        continue
    offsets = [r["day_offset"] for r in rets]
    # 占位签名：仅 [0] 且 return_pct==0
    if offsets == [0] and (rets[0]["return_pct"] == 0 or rets[0]["return_pct"] is None):
        only_t0.append({"track": t, "row": rets[0]})
    if 3 in offsets:
        has_t3.append(t)
        t3 = next(r for r in rets if r["day_offset"] == 3)
        td = str(t3["trade_date"])[:10] if t3["trade_date"] else ""
        rec = str(t3["recorded_at"] or "")
        # 收盘价未定判定：recorded_at 早于 trade_date 当日 15:00
        if td and rec:
            try:
                recdt = datetime.fromisoformat(rec.replace("Z","+00:00"))
                closedt = datetime.fromisoformat(f"{td}T15:00:00").astimezone()
                if recdt < closedt:
                    t3_unsettled.append({"code": t["code"], "entry": parse_entry_date(t["created_at"]), "t3_trade": td, "recorded": rec, "ret": t3["return_pct"]})
            except Exception:
                pass
        # 陈旧：T+3 trade_date 早于今日超过 5 个自然日 且 仍未被移除（孤立）
        if td and td < today:
            try:
                if (date.fromisoformat(today) - date.fromisoformat(td)).days > 7 and not t["removed_at"]:
                    stale_t3.append({"code": t["code"], "t3_trade": td, "ret": t3["return_pct"]})
            except Exception:
                pass

print(f"\n=== 收益落库诊断 ===")
print(f"完全无收益行(no_returns): {len(no_returns)}")
print(f"仅 T+0=0 占位(only_t0):   {len(only_t0)}   <-- 这是'很多数值为空'的核心信号")
print(f"有 T+3 行:                {len(has_t3)}")
print(f"T+3 收盘价未定(unsettled): {len(t3_unsettled)}")
print(f"T+3 陈旧未归档(>7天):     {len(stale_t3)}")

print("\n--- only_t0 明细(前30) ---")
for o in only_t0[:30]:
    t = o["track"]; r = o["row"]
    print(f"  code={t['code']} name={t['name']} src={t['source']} entry_price={t['entry_price']} created_at={t['created_at']} t0_trade={r['trade_date']} removed={t['removed_at']}")

print("\n--- no_returns 明细(前30) ---")
for t in no_returns[:30]:
    print(f"  code={t['code']} name={t['name']} entry_price={t['entry_price']} created_at={t['created_at']} removed={t['removed_at']}")

print("\n--- t3_unsettled 明细 ---")
for u in t3_unsettled[:30]:
    print("  ", u)

print("\n--- stale_t3 明细(前20) ---")
for s in stale_t3[:20]:
    print("  ", s)

# watchlist 中 track 的收益覆盖情况
print("\n=== watchlist 当前条目的收益覆盖 ===")
miss = 0
for w in wl:
    tid = w["track_id"]
    if tid:
        cnt = con.execute("SELECT COUNT(*) c FROM watch_track_returns WHERE track_id=?", (tid,)).fetchone()["c"]
    else:
        cnt = -1
    if cnt == 0:
        miss += 1
print(f"当前自选 {len(wl)} 只，其中 track_id 有但收益行=0 的: {miss}；track_id 为空的: {sum(1 for w in wl if not w['track_id'])}")

con.close()
print("\nDONE")
