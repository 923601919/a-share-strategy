# -*- coding: utf-8 -*-
"""多基准下的策略超额收益重算。

为什么有这个脚本：早先用上证指数（`000001.SH`）做基准，但它与策略选出的中小盘
股票不同质，会**高估 beta**。现在库里补了 `000852.SH`（中证1000）、`000905.SH`
（中证500）、`000300.SH`（沪深300），这里对四个基准各算一遍，用于对照。

口径：
  · 个股收益 = `watch_tracks.exit_return_pct`（入池价 → T+3 收盘）
  · 指数收益 = 入池日收盘 → T+3 交易日收盘（T+3 日期取 `watch_track_returns`
    `day_offset=3` 的实际 `trade_date`，不用自然日推算，避免跨节假日错位）
  · 超额 = 个股 − 指数

两种 beta 估计：
  (a) 入池日聚合：按入池日取均值，20 个点做 OLS。与上一版报告同口径，可比。
  (b) 逐日面板：用 `watch_track_returns` 每个 offset 的日间收益对指数当日收益做
      OLS，样本量大得多，是更强的估计。

用法：
    python scripts/analyze_benchmark.py --db /opt/a-share-strategy/data/app.db
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys

BENCH = ("000001.SH", "000300.SH", "000905.SH", "000852.SH")
LABEL = {
    "000001.SH": "上证指数",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
}

# (user_id, source) -> 真实策略。老三账户的 source 一律沿用 fenshi，
# 只有 verify(uid=8) 的 source 是三值，所以必须用二元组映射。
STRAT_MAP = {
    (3, "fenshi"): "soft",
    (8, "soft"): "soft",
    (4, "fenshi"): "fenshi",
    (8, "fenshi"): "fenshi",
    (7, "fenshi"): "soft_opt",
    (8, "soft_opt"): "soft_opt",
}


def ols(xs: list[float], ys: list[float]) -> tuple[float, float, float, float]:
    """返回 (beta, t_beta, alpha, t_alpha)。"""
    n = len(xs)
    if n < 3:
        return (float("nan"),) * 4
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return (float("nan"),) * 4
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = my - b * mx
    s2 = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys)) / (n - 2)
    if s2 <= 0:
        return (b, float("inf"), a, float("inf"))
    seb = math.sqrt(s2 / sxx)
    sea = math.sqrt(s2 * (1.0 / n + mx * mx / sxx))
    return b, b / seb, a, a / sea


def describe(vals: list[float]) -> tuple[int, float, float, float, float, float]:
    """返回 (n, mean, se, t, win_pct)。"""
    n = len(vals)
    if n < 2:
        return n, float("nan"), float("nan"), float("nan"), float("nan")
    m = sum(vals) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))
    se = sd / math.sqrt(n)
    win = 100.0 * sum(1 for v in vals if v > 0) / n
    return n, m, se, m / se, win


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--from", dest="date_from", default="2026-08-01")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    idx: dict[tuple[str, str], float] = {}
    for s, d, cl in conn.execute(
        "SELECT symbol,trade_date,close FROM market_index_daily WHERE trade_date>=?",
        (args.date_from,),
    ):
        idx[(s, d)] = cl
    all_dates = sorted({d for (_, d) in idx})
    prev_day = {d: (all_dates[i - 1] if i > 0 else None) for i, d in enumerate(all_dates)}

    t3_date: dict[int, str] = {}
    for tid, td in conn.execute(
        "SELECT track_id,trade_date FROM watch_track_returns WHERE day_offset=3"
    ):
        t3_date[tid] = td

    rows: list[dict] = []
    strat_of: dict[int, str] = {}
    # 丢样原因要能说清楚，否则 n 的变化会被当成"数据少了"而不是"口径变了"
    drop = {"no_strategy": 0, "not_t3": 0, "no_ret": 0, "no_t3row": 0, "no_index": 0}
    sample_no_index: list[tuple] = []
    q = (
        "SELECT id,user_id,source,entry_score,day_position,date(created_at),"
        "exit_return_pct,completion_reason FROM watch_tracks"
    )
    for tid, uid, src, score, pos, ed, er, cr in conn.execute(q):
        st = STRAT_MAP.get((uid, src))
        if st is None:
            drop["no_strategy"] += 1
            continue
        strat_of[tid] = st
        if cr != "auto_t3":
            drop["not_t3"] += 1
            continue
        if er is None:
            drop["no_ret"] += 1
            continue
        td = t3_date.get(tid)
        if not td:
            drop["no_t3row"] += 1
            continue
        rec: dict = {"st": st, "ed": ed, "td": td, "r": er, "score": score, "pos": pos}
        ok = True
        for b in BENCH:
            a = idx.get((b, ed))
            z = idx.get((b, td))
            if not a or not z:
                ok = False
                if len(sample_no_index) < 8:
                    sample_no_index.append((tid, st, ed, td, b))
                break
            rec[b] = (z / a - 1) * 100.0
        if not ok:
            drop["no_index"] += 1
            continue
        rows.append(rec)

    print(f"N_VALID={len(rows)}")
    print(f"丢弃明细: {drop}")
    if sample_no_index:
        print(f"缺指数样本(前8): {sample_no_index}")

    print("\n=== 全样本：各基准下的超额收益 ===")
    print(f"{'基准':<10}{'n':>6}{'个股':>9}{'指数':>9}{'超额':>9}{'se':>7}"
          f"{'t':>7}{'95%CI':>18}{'胜率':>8}")
    for b in BENCH:
        stk = [r["r"] for r in rows]
        iv = [r[b] for r in rows]
        ex = [r["r"] - r[b] for r in rows]
        n, m, se, t, win = describe(ex)
        lo, hi = m - 1.96 * se, m + 1.96 * se
        print(f"{LABEL[b]:<10}{n:>6}{sum(stk)/n:>9.2f}{sum(iv)/n:>9.2f}"
              f"{m:>9.2f}{se:>7.3f}{t:>7.2f}{f'[{lo:.2f},{hi:.2f}]':>18}{win:>7.1f}%")

    print("\n=== 分策略：各基准下的超额 / 胜率 ===")
    print(f"{'策略':<10}{'n':>6}" + "".join(f"{LABEL[b]:>12}" for b in BENCH))
    for st in ("soft", "fenshi", "soft_opt"):
        sub = [r for r in rows if r["st"] == st]
        if not sub:
            continue
        cells = []
        for b in BENCH:
            ex = [r["r"] - r[b] for r in sub]
            n, m, se, t, win = describe(ex)
            cells.append(f"{m:>7.2f}/{win:>3.0f}%")
        print(f"{st:<10}{len(sub):>6}" + "".join(f"{c:>12}" for c in cells))

    # ---- (a) 入池日聚合 OLS（与上一版报告同口径）----
    print("\n=== beta/alpha 口径A：按入池日聚合（20 点）===")
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["ed"], []).append(r)
    days = sorted(by_day)
    print(f"{'基准':<10}{'天数':>6}{'beta':>9}{'t(beta)':>9}{'alpha':>9}{'t(alpha)':>10}")
    for b in BENCH:
        xs = [sum(r[b] for r in by_day[d]) / len(by_day[d]) for d in days]
        ys = [sum(r["r"] for r in by_day[d]) / len(by_day[d]) for d in days]
        beta, tb, alpha, ta = ols(xs, ys)
        print(f"{LABEL[b]:<10}{len(days):>6}{beta:>9.2f}{tb:>9.2f}"
              f"{alpha:>9.2f}{ta:>10.2f}")

    # ---- (b) 逐日面板 OLS ----
    print("\n=== beta/alpha 口径B：逐日面板（offset 间日收益，样本大）===")
    panel: dict[int, list[tuple[int, str, float]]] = {}
    for tid, off, td, rp in conn.execute(
        "SELECT track_id,day_offset,trade_date,return_pct "
        "FROM watch_track_returns WHERE return_pct IS NOT NULL ORDER BY track_id,day_offset"
    ):
        if tid in strat_of:
            panel.setdefault(tid, []).append((off, td, rp))
    print(f"{'基准':<10}{'n':>8}{'beta':>9}{'t(beta)':>9}{'alpha':>9}{'t(alpha)':>10}")
    for b in BENCH:
        xs: list[float] = []
        ys: list[float] = []
        for tid, seq in panel.items():
            for i in range(1, len(seq)):
                if seq[i][0] > 3 or seq[i - 1][0] != seq[i][0] - 1:
                    continue
                d = seq[i][1]
                p = prev_day.get(d)
                if not p:
                    continue
                a = idx.get((b, p))
                z = idx.get((b, d))
                if not a or not z:
                    continue
                xs.append((z / a - 1) * 100.0)
                ys.append(seq[i][2] - seq[i - 1][2])
        beta, tb, alpha, ta = ols(xs, ys)
        print(f"{LABEL[b]:<10}{len(xs):>8}{beta:>9.2f}{tb:>9.2f}"
              f"{alpha:>9.2f}{ta:>10.2f}")

    # ---- 市态拆分（以中证1000 为准，并与上证对照）----
    print("\n=== 市态拆分：上涨日 vs 下跌日 ===")
    print(f"{'基准':<10}{'市态':<8}{'n':>6}{'个股':>9}{'指数':>9}{'超额':>9}{'胜率':>8}")
    for b in BENCH:
        for tag, pick in (("上涨日", lambda r: r[b] > 0), ("下跌日", lambda r: r[b] <= 0)):
            sub = [r for r in rows if pick(r)]
            if not sub:
                continue
            ex = [r["r"] - r[b] for r in sub]
            n, m, se, t, win = describe(ex)
            print(f"{LABEL[b]:<10}{tag:<8}{n:>6}{sum(r['r'] for r in sub)/n:>9.2f}"
                  f"{sum(r[b] for r in sub)/n:>9.2f}{m:>9.2f}{win:>7.1f}%")

    # ---- soft 分数桶（中证1000 基准下复查是否仍倒挂）----
    print("\n=== soft 策略分数分桶（中证1000 基准）===")
    b = "000852.SH"
    print(f"{'分数桶':<10}{'n':>6}{'个股':>9}{'指数':>9}{'超额':>9}{'胜率':>8}")
    for lo, hi, name in ((0, 50, "<50"), (50, 70, "50-69"), (70, 999, ">=70")):
        sub = [r for r in rows if r["st"] == "soft" and r["score"] is not None
               and lo <= r["score"] < hi]
        if not sub:
            continue
        ex = [r["r"] - r[b] for r in sub]
        n, m, se, t, win = describe(ex)
        print(f"{name:<10}{n:>6}{sum(r['r'] for r in sub)/n:>9.2f}"
              f"{sum(r[b] for r in sub)/n:>9.2f}{m:>9.2f}{win:>7.1f}%")

    # ---- 日内位置桶（中证1000 基准）----
    print("\n=== 日内位置分桶（中证1000 基准）===")
    print(f"{'位置':<10}{'n':>6}{'个股':>9}{'超额':>9}{'胜率':>8}")
    for lo, hi, name in ((0.9, 9, ">=0.9"), (0.6, 0.9, "0.6-0.9"),
                         (0.3, 0.6, "0.3-0.6"), (-1, 0.3, "<0.3")):
        sub = [r for r in rows if r["pos"] is not None and lo <= r["pos"] < hi]
        if not sub:
            continue
        ex = [r["r"] - r[b] for r in sub]
        n, m, se, t, win = describe(ex)
        print(f"{name:<10}{n:>6}{sum(r['r'] for r in sub)/n:>9.2f}{m:>9.2f}{win:>7.1f}%")

    return 0


if __name__ == "__main__":
    sys.exit(main())
