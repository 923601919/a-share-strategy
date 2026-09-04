# 收益刷新为空 / 收盘后仍刷不出来 —— 根因诊断

> 诊断日期：2026-09-04（本地）｜数据库：`data/app.db`｜模式：仅定位 + 原因 + 修复建议（尚未改代码）

> ⚠️ **部署后修订（2026-09-04 晚）**：本文"缓存导致收盘后刷不出"的核心结论**对收益计算路径是错的**，实际修复时踩了坑并已改正，详见末尾「八、部署后修订」。A/C 修复方向正确，B 的初始实现改错了地方（详见第八节）。

---

## 一、数据层实况（直连 SQLite 统计）

| 指标 | 数量 | 说明 |
|---|---|---|
| `watch_tracks` 总数 | 85 | 历史+在册 |
| 已归档(removed) / 在册 | 42 / 43 | |
| 当前 `watchlist` 条目 | 33 | |
| **仅有 T+0=0 占位、T+1/T+2/T+3 全空** | **29** | 🔴 "很多数值为空"的直接证据 |
| 完全无收益行 | 0 | 占位已落库，所以不是"没算"，而是"算成了假值" |
| 有 T+3 行 | 37 | |
| **T+3 收盘价在交易时段内被捕获（盘中价当收盘）** | **12** | 🔴 数值本身错误 |

典型坏样本（活跃、仍在自选里，却只显示 T+0=0）：

- `300864 南大环境` 入池 2026-09-03，收益仅 `[T+0=0 @2026-09-03]`
- `601083 锦江航运` 入池 2026-09-03，同上
- `000811 冰轮环境` 入池 2026-09-03，同上
- `600367 红星发展` 入池 2026-08-24，多次入池，均只有 T+0=0

即：**大部分当前在册自选的短线收益都是"空白/0"**，与用户描述的"很多数值都无法正常显示出来"完全吻合。

---

## 二、调用链路（刷新收益时到底发生了什么）

两个前端入口最终都走到同一条路：

```
点击「刷新收益」
  ├─ GET  /api/watchlist?refresh_returns=true   (main.py:336)
  └─ POST /api/watchlist/refresh/jobs          (main.py:404 → run_watchlist_refresh)
        ↓
  enrich_watch_item(it, q, force_refresh=True)  (main.py:367 / track.py:603)
        ↓
  refresh_track_returns(track, persist=True, force=True)   (track.py:342)
        ↓
  compute_short_term_returns(code, entry_price, entry_date) (track.py:133)
        ↓
  _daily_bars → mkt.fetch_daily(code, limit=40)   (market.py:337) ← 带进程内缓存！
        ↓
  upsert_track_returns(track_id, rows)            (db.py:753) ← 把结果写进库
```

关键点：**收益重算全程被 `fetch_daily` 的进程内缓存卡住**（market.py:337-364）。是否拿到"最新的收盘价"，取决于缓存里那一份日线快照新不新。

---

## 三、根因（按可能性排序）

### 根因 1（主因）：降级逻辑把"算不全"当成了"算完了"
`compute_short_term_returns`（track.py:133-184）有两种"只产出 T+0=0"的路径，且**都会被 `upsert_track_returns` 落库、覆盖掉原有数据**：

1. **入场日当天/数据滞后**：找不到 `date >= entry_date` 的 K 线（`start_idx is None`）→ 直接返回 `[{day_offset:0, return_pct:0}]`（track.py:154-164）。
2. **未来 K 线还没产生**：找到入场日那根，但 T+1/T+2/T+3 的交易日 K 线尚未出现（`idx >= len(daily)` → `break`，track.py:168-169）→ 只返回 T+0（及已存在的那几天）。

对"几天前才入池"的票，正常应已能算出 T+1/T+2。但数据库里它们只有 T+0=0——说明**重算时拿到的日线数据根本没包含后续交易日**（见根因 2）。一旦这份"残缺结果"被写入库，该 track 就被标记成"已有收益"，`refresh_track_returns(force=False)`（进页面默认）直接返回残缺结果、永不重算。

### 根因 2（导致"收盘后仍刷不出来"）：收盘后日线缓存 TTL 过长，陈旧快照冻结一整晚
`_daily_cache_ttl()`（market.py:326-334）：

- 盘中(09:15–15:05)：短 TTL（默认 `daily_cache_intraday_ttl = 1800s`）。
- **收盘后(≥15:05)或周末：TTL = 到下一交易日 09:15 的秒数**（可能 18 小时）。

后果：只要在某次"收盘后但数据源还没放出当日收盘 / 或盘中"的时点发生过一次 `fetch_daily`，那份**不含最新收盘价的快照就会被缓存一整晚**。此后无论用户点多少次"刷新收益"，`compute_short_term_returns` 都从这份陈旧缓存里算 → 始终缺 T+1/T+2/T+3 → **"即使已经收盘了，仍然刷新不出来"**。

### 根因 3：`enrich_watch_item` 会把空结果写成 T+0=0 占位并落库
track.py:448-461：当 `returns` 为空且 `entry_price` 存在时，构造一个 T+0=0 占位并 `upsert_track_returns` 写库。这会把一个 track **永久冻结在 T+0=0**，即便日后数据源恢复正常，`force=False` 路径也只会读回这个占位、不再重算。

### 根因 4：T+3 收盘价在盘中就被捕获，且永不修正
12 条 T+3 的 `recorded_at` 早于其 `trade_date` 当日 15:00（如 `002456` 在 `2026-09-01T10:55` 捕获"收盘"）。`compute_short_term_returns` 用的是当日实时 K 线的收盘价，并非最终收盘。`_t3_close_settled`（track.py:100-122）据此判定"收盘价不可信"，于是 `expire_past_t3_watchlist(force_refresh=False)` 跳过归档——**错值因此滞留**；而 `force=False` 又不会重算，必须用户手动强刷且该次强刷恰好拿到当日最终收盘才能纠正。

### 根因 5：没有"跨交易日自动回填"机制
T+1/T+2/T+3 的完善完全依赖用户点击"刷新收益"。而上面 1–4 又可能让这次点击失效。项目已有 APScheduler（scheduler.py），但只跑了 10:40/14:20 的扫描，**没有"收盘后回填收益"的任务**。

---

## 四、排查方向（若要在线上进一步坐实）

1. **实拉一次数据源**，确认 akshare 是否滞后/偶发空：
   对一只 only_t0 的票（如 `300864`）手动 `akshare_client.fetch_daily("300864", limit=40)`，看返回的最后一根日期是否 < 今日，或是否偶发空 DataFrame（scheduler.py 注释已指出 soft 扫描时 akshare 会"返回空 DataFrame 且不抛异常"）。
2. **打印 `fetch_daily` 缓存命中**：在 market.py 的 `_cache_get` 命中处打日志，确认收益重算时是否命中了陈旧缓存。
3. **核对 `entry_date` 与最新日线日期差**：若 `entry_date` 比最新 K 线日期还新（周末/节假日/数据源滞后），`start_idx is None` 必然触发。
4. **看 `recorded_at` 分布**：确认 only_t0 行的写入时间是否集中在收盘后到次日开盘前（印证根因 2 的缓存冻结）。

---

## 五、修复建议（按性价比排序）

### A. 占位不当"完成"、不覆盖已有真实数据（改 track.py）
- `compute_short_term_returns` 在 `start_idx is None` 或 `idx >= len(daily)` 时，**返回 `[]` 或带 `incomplete=True` 标记**，不要返回伪造的 `T+0=0`。
- `upsert_track_returns` 改为**只更新实际算出的行**；当本次重算行数少于已落库行数时，**保留**原有 T+1/T+2/T+3，不删除（避免脏数据覆盖好数据）。
- `refresh_track_returns` 的 except 兜底占位（track.py:388-397）同样不要落库成 T+0=0；否则保留空、让前端显示"待更新"。
- `enrich_watch_item` 的自动 T+0=0 写库（track.py:448-461）**移除或加开关**，防止冻结好 track。

### B. 收益重算路径绕过日线缓存（改 market.py / track.py）
- 给 `fetch_daily` 增加 `no_cache: bool = False` 参数；`compute_short_term_returns` 调用时传 `no_cache=True`，保证每次重算都拿到最新收盘。
- 或退一步：收盘后（≥15:05）把日线缓存 TTL 从"到次日开盘"改为**短窗口（如 5–10 分钟）持续刷新**，直到确认当日收盘已放出。

### C. 增加"收盘后回填"定时任务（复用 scheduler.py 的 APScheduler）
- 新增一个 job（如每个交易日 15:30）调用 `run_watchlist_refresh` 或专门的"对所有在册 track 重算 T+0..T+3（no_cache）"。这样**收盘后无需用户点，数值会自动补齐**，直接解决"收盘后仍刷不出来"。

### D. 修正盘中捕获的 T+3（改 track.py）
- 在 `expire_past_t3_watchlist(force_refresh=True)` 路径里，对 `_t3_close_settled=False` 的 track 强制 `refresh_track_returns(force=True, no_cache=True)` 重算，拿到真实收盘后再判定归档。

### E. 一次性数据修复脚本（立即见效）
- 写一个脚本：对全部**在册** track 执行 `refresh_track_returns(force=True, no_cache=True)`；对仍只产出 T+0 的，直接删除其伪造 T+0 占位行（或标记为 incomplete），避免误导。
- 注意：必须先落地 A/B，否则脚本重算结果仍可能被陈旧缓存污染。

---

## 六、结论

"很多数值为空"是**数据层真实存在**的问题：29/85 个 track 的收益只有一条伪造的 T+0=0 占位行。"即使收盘了仍刷不出来"的根因是 **`fetch_daily` 收盘后超长缓存 TTL 把陈旧日线快照冻结了一整晚**，叠加 `compute_short_term_returns`/ `enrich_watch_item` 会把残缺结果当完成并落库、且 `force=False` 永不重算。修复核心是：**收益重算绕过日线缓存 + 占位不再当完成 + 增加收盘后自动回填任务**。

建议下一步：先落地 A+B（改动小、收益大），再部署 C（根治"收盘后刷不出"），最后用 E 脚本修复存量脏数据。

---

## 八、部署后修订（2026-09-04 晚，重要更正）

A+C 修复方向正确并已上线（`2026.09.04-returns-fix2`），但 B 的初始实现**改错了地方且会崩溃**，部署时线上验证发现并已改正：

1. **收益计算路径根本没用缓存**。`track.py::_daily_bars` 调的是 `from providers import akshare_client as mkt` —— **原始 `akshare_client.fetch_daily`（无缓存、无 `no_cache` 形参）**，而非带缓存的 `providers.market.fetch_daily`。所以"日线缓存 TTL 过长把陈旧日线冻结一晚"对收益路径**不成立**——每次都是实时 akshare。本文第二节据此得出的"缓存导致收盘后刷不出"结论需撤回。
2. **B 初始实现崩溃**：给 `market.fetch_daily` 加的 `no_cache` 对收益路径无效；`_daily_bars` 又把 `no_cache=True` 传给原始客户端 → `TypeError: fetch_daily() got an unexpected keyword argument 'no_cache'`，导致 backfill 全量崩溃、只回吐旧值（早期"refreshed=57"是假象）。
3. **真正修复**：`_daily_bars` 改为调用 **`providers.market.fetch_daily`**（带进程缓存 + 支持 `no_cache`），B 才真正生效、崩溃消失。提交 `82ab1b0`。
4. **线上实测结论**：
   - compute 现已算真实值（如 300427 用 09-03 入池价 6.97 / 当日收盘 6.64 → T+0=-4.73%）。
   - 早期状态脚本报"57 只 only_t0 占位"是**误报**：多数 09-03 入池的票入池价≈当日收盘，T+0=0% 是**正确真实值**，不是脏占位。
   - **真正还剩"算不出"的是今天(09-04)新加的 track**：akshare 此刻只返回到 2026-09-03 的日线（东财对"当日"bar 常延迟到次交易日才进 daily 接口），故 09-04 的 T+0 要等明天补齐——属数据源延迟，非代码 bug；15:30 回填 job 次日会自动补上。
5. **教训**：改收益/日线相关代码前，先 `grep` 确认调用链走 `akshare_client`（原始）还是 `providers.market`（缓存封装）——两者都有 `fetch_daily` 但签名不同。`providers/__init__.py` 为空，`mkt = akshare_client` 指向原始客户端。
