"""统计当前活跃自选里收益数据的真实覆盖情况（不依赖 TAT 的 sqlite 限制）。"""
from db import list_user_ids_with_active_watchlist, get_track_returns, list_watchlist
from user_ctx import user_scope

if __name__ == "__main__":
    only_t0 = []
    multi = 0
    none = 0
    total = 0
    for uid in list_user_ids_with_active_watchlist():
        with user_scope(uid):
            items = list_watchlist()
        for it in items:
            tid = it.get("track_id")
            if not tid:
                continue
            total += 1
            rets = get_track_returns(int(tid))
            if not rets:
                none += 1
                continue
            offsets = [r["day_offset"] for r in rets]
            if offsets == [0] and (rets[0]["return_pct"] == 0 or rets[0]["return_pct"] is None):
                only_t0.append((it.get("code"), it.get("name"), it.get("entry_price"), it.get("created_at")))
            else:
                multi += 1
    print("ACTIVE_TOTAL=%d  REAL(multi-day)=%d  ONLY_T0_PLACEHOLDER=%d  NO_RETURNS=%d" % (total, multi, len(only_t0), none))
    print("--- only_t0 active 明细(前40) ---")
    for c, n, ep, ca in only_t0[:40]:
        print("  code=%s name=%s entry_price=%s created=%s" % (c, n, ep, ca))
