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


def ols2(
    x1: list[float], x2: list[float], ys: list[float]
) -> tuple[float, float, float, float, float, float]:
    """二元 OLS：y = a + b1*x1 + b2*x2。返回 (b1, t(b1), b2, t(b2), a, n)。

    为什么需要它：单变量斜率会被「选择效应」污染。追高惩罚会给高位置票扣 15 分，
    于是**只有更强的**高位置票才挤得进 top-N —— 高位置组的收益里混进了
    「被惩罚逼出来的强度」。把 entry_score 一起放进回归，斜率才问得清
    「同样强度的票，位置越高是更好还是更差」。
    """
    n = len(ys)
    if n < 4 or len(x1) != n or len(x2) != n:
        return (float("nan"),) * 6
    m1 = sum(x1) / n
    m2 = sum(x2) / n
    my = sum(ys) / n
    c1 = [v - m1 for v in x1]
    c2 = [v - m2 for v in x2]
    cy = [v - my for v in ys]
    s11 = sum(v * v for v in c1)
    s22 = sum(v * v for v in c2)
    s12 = sum(a * b for a, b in zip(c1, c2))
    s1y = sum(a * b for a, b in zip(c1, cy))
    s2y = sum(a * b for a, b in zip(c2, cy))
    det = s11 * s22 - s12 * s12
    if abs(det) < 1e-12:
        return (float("nan"),) * 6
    b1 = (s22 * s1y - s12 * s2y) / det
    b2 = (s11 * s2y - s12 * s1y) / det
    a0 = my - b1 * m1 - b2 * m2
    resid = [y - (a0 + b1 * p + b2 * q) for p, q, y in zip(x1, x2, ys)]
    s2 = sum(v * v for v in resid) / (n - 3)
    if s2 <= 0:
        return b1, float("inf"), b2, float("inf"), a0, n
    var1 = s2 * s22 / det
    var2 = s2 * s11 / det
    se1 = math.sqrt(var1) if var1 > 0 else float("inf")
    se2 = math.sqrt(var2) if var2 > 0 else float("inf")
    return b1, b1 / se1, b2, b2 / se2, a0, n


def welch(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Welch 两样本 t 检验。返回 (差值, t, 近似自由度)。

    为什么不用等方差 t：两组样本量与方差都差得远（如 496 vs 1601），
    等方差假设不成立，用它会低估标准误、虚高显著性。
    """
    n1, n2 = len(xs), len(ys)
    if n1 < 2 or n2 < 2:
        return float("nan"), float("nan"), float("nan")
    m1 = sum(xs) / n1
    m2 = sum(ys) / n2
    v1 = sum((v - m1) ** 2 for v in xs) / (n1 - 1)
    v2 = sum((v - m2) ** 2 for v in ys) / (n2 - 1)
    se = math.sqrt(v1 / n1 + v2 / n2)
    if se <= 0:
        return m1 - m2, float("inf"), float("nan")
    t = (m1 - m2) / se
    df = (v1 / n1 + v2 / n2) ** 2 / (
        (v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1)
    )
    return m1 - m2, t, df


def describe(vals: list[float]) -> tuple[int, float, float, float, float]:
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

    # 各指数的**当日涨跌幅**（用上一根收盘作基准）。
    # 用途一：生产市态闸门（scan.py:_market_env）判的就是「参考指数当日跌幅」，
    #         这里复刻同一口径，才能评估闸门阈值放得对不对。
    # 用途二：追高惩罚的回归/分桶都以它为背景。
    idx_pct: dict[tuple[str, str], float] = {}
    for b in BENCH:
        for d in all_dates:
            p = prev_day.get(d)
            if not p:
                continue
            a, z = idx.get((b, p)), idx.get((b, d))
            if a and z:
                idx_pct[(b, d)] = (z / a - 1) * 100.0

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
        "SELECT id,user_id,source,entry_score,day_position,vwap_deviation,date(created_at),"
        "exit_return_pct,completion_reason FROM watch_tracks"
    )
    for tid, uid, src, score, pos, dev, ed, er, cr in conn.execute(q):
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
        rec: dict = {
            "st": st, "ed": ed, "td": td, "r": er,
            "score": score, "pos": pos, "dev": dev,
            "sp": idx_pct.get(("000001.SH", ed)),  # 入池日上证涨跌幅（闸门口径）
        }
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

    # ================= 市态闸门评估 =================
    # 生产闸门（scan.py:_market_env）判的是 market_env_ref_index（默认上证）的**当日跌幅**：
    #     pct <= -1.5% → warn（分数 ×0.8，仍出结果）
    #     pct <= -2.5% → block（进攻型直接空结果 → 当天没有入池样本）
    # 要问的是「阈值放对了没有」。这里按上证当日跌幅分档，看每档的实际超额。
    #
    # 口径说明（务必连着结论一起读）：闸门在**盘中**判（10:40/14:20 的实时快照），
    # 而这里用的是**收盘**涨跌幅。两者在震荡日会不一致——某天 10:40 只跌 0.5%
    # （不触发）、收盘却跌 1.7%（该触发），本表会把它算进 warn 档。
    # 所以下面另附一张 scan_quality 的**实际触发记录**，用来校准这个偏差。
    print("\n=== 市态闸门评估：按入池日上证涨跌幅分档（中证1000 基准）===")
    print(f"{'上证当日':<14}{'档位':<8}{'n':>6}{'个股':>9}{'指数':>9}"
          f"{'超额':>9}{'胜率':>8}")
    GATE_BANDS = (
        (-99.0, -2.5, "block"),
        (-2.5, -1.5, "warn"),
        (-1.5, -0.5, ""),
        (-0.5, 0.5, ""),
        (0.5, 99.0, ""),
    )
    for lo, hi, tag in GATE_BANDS:
        sub = [r for r in rows if r["sp"] is not None and lo < r["sp"] <= hi]
        if not sub:
            print(f"{f'({lo:+.1f},{hi:+.1f}]':<14}{tag:<8}{0:>6}")
            continue
        ex = [r["r"] - r[b] for r in sub]
        n, m, se, t, win = describe(ex)
        print(f"{f'({lo:+.1f},{hi:+.1f}]':<14}{tag:<8}{n:>6}"
              f"{sum(r['r'] for r in sub)/n:>9.2f}{sum(r[b] for r in sub)/n:>9.2f}"
              f"{m:>9.2f}{win:>7.1f}%")

    no_sp = sum(1 for r in rows if r["sp"] is None)
    print(f"（缺入池日上证涨跌幅的样本：{no_sp} 条，未计入上表）")
    warn_n = sum(1 for r in rows if r["sp"] is not None and -2.5 < r["sp"] <= -1.5)
    print(f"（warn 档有 {warn_n} 条入池样本；block 档样本数应为 0 —— "
          f"闸门生效的话当天不会出结果，非 0 说明闸门没拦住）")

    # 闸门的**作用路径**是什么？想清楚这点才能问对问题。
    #
    # 闸门读的是「入池日上证跌了多少」，它的效力只能通过「入池日的市场状态是否
    # 预示后续 T+3 的超额」来体现。所以这里要回归的是
    #         组合 T+3 平均收益  ~  入池日上证涨跌幅
    # ——注意它**不等于**上面的口径A：口径A 的自变量是「上证在同期（入池日→T+3）
    # 的涨跌幅」，那是个事后变量，拿它算出的 beta 再大也不能用在闸门上。
    # 上一版报告里那句「按 beta 折算阈值」正是混用了这两个变量，这里订正。
    print("\n--- 闸门效力：入池日上证涨跌幅能否预示后续 T+3 超额 ---")
    by_day_g: dict[str, list[dict]] = {}
    for r in rows:
        by_day_g.setdefault(r["ed"], []).append(r)
    gdays = sorted(by_day_g)
    xs_sp = [idx_pct.get(("000001.SH", d)) for d in gdays]
    ys_pt = [sum(r["r"] for r in by_day_g[d]) / len(by_day_g[d]) for d in gdays]
    pairs = [(x, y) for x, y in zip(xs_sp, ys_pt) if x is not None]
    if len(pairs) >= 3:
        bx, tx, _, _ = ols([p[0] for p in pairs], [p[1] for p in pairs])
        print(f"beta(组合 T+3 收益 ~ 入池日上证涨跌幅) = {bx:.2f}  "
              f"(n={len(pairs)} 天, t={tx:.2f})")
        if tx <= 1.96:
            print("  t 不显著 → 入池日上证的涨跌**不足以**预示后续超额。"
                  "这并不自动说明闸门没用（见下面的选择效应说明），")
            print("  但它说明「按 beta 折算阈值」这条路走不通：斜率本身就不显著，"
                  "折算出来的阈值没有含义。")
        else:
            print(f"  斜率显著 → 入池日上证跌 1% 对应组合 T+3 少赚 {-bx:.2f}pp；"
                  f"若要让组合在 -1.5pp 时才折分，阈值约 {-1.5 / bx:+.2f}%")
    else:
        print("上证涨跌幅样本不足，跳过")

    # ★ 选择效应（读上面所有分组结论时都必须带着它）★
    # 闸门在 warn 档会把全体进攻型分数 ×0.8，**再**做 top-N 截断。于是
    # 「warn 档的入池样本」= 扛过折分的那批最强的票。它们表现不差，
    # 既可能是「闸门筛得好」，也可能是「根本不需要闸门」——两种解释都自洽，
    # 现有数据分不开。要分开必须留住每个候选的**折分前**分数，
    # 而 scan_snapshots 只存了入选后的 items。
    print("（选择效应提醒：warn 档样本是「扛过 ×0.8 折分」的幸存者，"
          "其表现好既可能是闸门筛得好、也可能是闸门不必要。"
          "要分辨必须落库折分前的全候选分成，当前库做不到。）")

    # scan_quality 里的**实际触发记录**：闸门真的按什么频率开火。
    # 这张表与上表口径不同（盘中 vs 收盘），放在一起才能看出收盘代理差多少。
    print("\n--- scan_quality 实际触发分布（盘中口径，库内原始记录）---")
    try:
        recs = list(conn.execute(
            "SELECT COALESCE(market_env_level,'(null)'), COUNT(*), "
            "AVG(market_pct), MIN(market_pct), MAX(market_pct) "
            "FROM scan_quality GROUP BY 1 ORDER BY 2 DESC"
        ))
        print(f"{'档位':<10}{'扫描轮次':>10}{'均值%':>9}{'最小%':>9}{'最大%':>9}")

        def _cell(v: object) -> str:
            return f"{v:>9.2f}" if isinstance(v, (int, float)) else f"{'-':>9}"

        for lv, cnt, avg, mn, mx in recs:
            print(f"{str(lv):<10}{cnt:>10}{_cell(avg)}{_cell(mn)}{_cell(mx)}")
    except sqlite3.Error as e:
        print(f"scan_quality 读取失败（老库可能没这张表）：{e}")

    # ================= 追高惩罚评估 =================
    # 生产参数（config.py）：
    #     chase_pos_high = 0.90 / chase_pos_penalty = 15
    #     chase_dev_high = 2.5% / chase_dev_penalty  = 12
    # 即「日内位置 ≥0.9」或「正乖离均价 ≥2.5%」各扣 15 / 12 分。这两条是本轮唯一
    # 在换基准后仍然显著的分组差异，所以要把它拆细、并且**量化方向**。
    print("\n=== 追高惩罚评估 A：日内位置细分（中证1000 基准）===")
    print(f"{'位置':<12}{'n':>6}{'个股':>9}{'超额':>9}{'胜率':>8}{'标记':>8}")
    for lo, hi, name, mark in ((0.9, 9, ">=0.90", "<-罚15"),
                               (0.7, 0.9, "0.70-0.90", ""),
                               (0.5, 0.7, "0.50-0.70", ""),
                               (0.3, 0.5, "0.30-0.50", ""),
                               (-1, 0.3, "<0.30", "")):
        sub = [r for r in rows if r["pos"] is not None and lo <= r["pos"] < hi]
        if not sub:
            continue
        ex = [r["r"] - r[b] for r in sub]
        n, m, se, t, win = describe(ex)
        print(f"{name:<12}{n:>6}{sum(r['r'] for r in sub)/n:>9.2f}{m:>9.2f}"
              f"{win:>7.1f}%{mark:>8}")

    print("\n=== 追高惩罚评估 B：均价乖离细分（中证1000 基准）===")
    print(f"{'乖离%':<12}{'n':>6}{'个股':>9}{'超额':>9}{'胜率':>8}{'标记':>8}")
    for lo, hi, name, mark in ((5.0, 999, ">=5.0", ""),
                               (2.5, 5.0, "2.5-5.0", "<-罚12"),
                               (1.0, 2.5, "1.0-2.5", ""),
                               (0.0, 1.0, "0.0-1.0", ""),
                               (-999, 0.0, "<0.0", "")):
        sub = [r for r in rows if r["dev"] is not None and lo <= r["dev"] < hi]
        if not sub:
            continue
        ex = [r["r"] - r[b] for r in sub]
        n, m, se, t, win = describe(ex)
        print(f"{name:<12}{n:>6}{sum(r['r'] for r in sub)/n:>9.2f}{m:>9.2f}"
              f"{win:>7.1f}%{mark:>8}")

    # 两条触发条件交叉：只罚了位置 / 只罚了乖离 / 两个都罚（共 -27 分）
    print("\n=== 追高惩罚评估 C：触发组合（中证1000 基准）===")
    print(f"{'触发情形':<14}{'扣分':>6}{'n':>6}{'超额':>9}{'t':>7}{'胜率':>8}")

    def trig_pos(r: dict) -> bool:
        return r["pos"] is not None and r["pos"] >= 0.9

    def trig_dev(r: dict) -> bool:
        return r["dev"] is not None and r["dev"] >= 2.5

    for name, pick, deduction in (
        ("两者都触发", lambda r: trig_pos(r) and trig_dev(r), 27),
        ("仅位置>=0.9", lambda r: trig_pos(r) and not trig_dev(r), 15),
        ("仅乖离>=2.5%", lambda r: trig_dev(r) and not trig_pos(r), 12),
        ("都没触发", lambda r: not trig_pos(r) and not trig_dev(r), 0),
    ):
        sub = [r for r in rows if pick(r)]
        if not sub:
            continue
        ex = [r["r"] - r[b] for r in sub]
        n, m, se, t, win = describe(ex)
        print(f"{name:<14}{deduction:>6}{n:>6}{m:>9.2f}{t:>7.2f}{win:>7.1f}%")

    # 方向量化：把 pos / dev 当连续变量回归到超额上。
    # 这是「惩罚方向对不对」的直接检验——斜率为负才说明「位置越高越差」。
    print("\n--- 方向量化：超额 ~ 自变量（中证1000 基准）---")
    print(f"{'自变量':<16}{'n':>6}{'斜率':>10}{'t(斜率)':>10}{'截距':>9}")
    for vname, key in (("day_position", "pos"), ("vwap_deviation", "dev")):
        sel = [r for r in rows if r[key] is not None]
        if len(sel) < 5:
            continue
        beta_v, t_v, alpha_v, _ = ols([r[key] for r in sel], [r["r"] - r[b] for r in sel])
        print(f"{vname:<16}{len(sel):>6}{beta_v:>10.3f}{t_v:>10.2f}{alpha_v:>9.2f}")
    print("（斜率显著为负才算「追高确实更差」；接近 0 或为正说明惩罚方向无依据。")
    print("  注意这只是**已入池样本内**的条件相关：惩罚会让高分位置票落到 top-N 之外，")
    print("  我们看不到那些被筛掉的票——真正的消融需要候选池，而 scan_snapshots 只存了")
    print("  入选后的 items。下面的组间检验同样受此限制。）")

    # 选择效应体检：高位置组是不是被惩罚"逼"得更强了？
    # entry_score 是**折分后**的分，所以把罚掉的分加回去就是折分前的强度。
    # 若高位置组的「折分前均值」明显更高，说明该组混进了选择效应，
    # 单变量斜率为正就不足以推出「位置越高越好」。
    print("\n--- 选择效应体检：各组折分前强度（entry_score + 已扣分）---")
    print(f"{'位置':<12}{'n':>6}{'折分前均分':>12}{'折分后均分':>12}{'已扣分':>9}")
    for lo, hi, name in ((0.9, 9, ">=0.90"), (0.7, 0.9, "0.70-0.90"),
                         (0.5, 0.7, "0.50-0.70"), (0.3, 0.5, "0.30-0.50"),
                         (-1, 0.3, "<0.30")):
        sub = [r for r in rows if r["pos"] is not None and r["score"] is not None
               and lo <= r["pos"] < hi]
        if not sub:
            continue
        pre = [r["score"] + 15.0 if r["pos"] >= 0.9 else r["score"] for r in sub]
        post = [r["score"] for r in sub]
        print(f"{name:<12}{len(sub):>6}{sum(pre)/len(pre):>12.1f}"
              f"{sum(post)/len(post):>12.1f}{sum(pre)/len(pre)-sum(post)/len(post):>9.1f}")

    print("\n--- 方向量化：控制 entry_score 后的斜率 ---")
    print(f"{'模型':<24}{'n':>6}{'自变量斜率':>12}{'t(斜率)':>10}"
          f"{'score斜率':>11}{'t(score)':>10}")
    sel = [r for r in rows if r["pos"] is not None and r["score"] is not None]
    if len(sel) >= 5:
        b1, t1, _, _ = ols([r["pos"] for r in sel], [r["r"] - r[b] for r in sel])
        print(f"{'单变量 pos':<24}{len(sel):>6}{b1:>12.3f}{t1:>10.2f}{'-':>11}{'-':>10}")
        b1, t1, b2, t2, _, n2 = ols2(
            [r["pos"] for r in sel], [r["score"] for r in sel],
            [r["r"] - r[b] for r in sel],
        )
        print(f"{'pos + entry_score':<24}{n2:>6}{b1:>12.3f}{t1:>10.2f}"
              f"{b2:>11.3f}{t2:>10.2f}")
        print("  → 两行对比是关键：斜率若在加入 entry_score 后掉到不显著，"
              "说明「位置越高越好」里很大一部分")
        print("    其实是「被 -15 分惩罚筛出来的强度」，不是位置本身。")
        # 二元回归的共线性诊断：pos 与 score 的相关系数
        mp = sum(r["pos"] for r in sel) / len(sel)
        ms = sum(r["score"] for r in sel) / len(sel)
        sp = math.sqrt(sum((r["pos"] - mp) ** 2 for r in sel) / (len(sel) - 1))
        ss = math.sqrt(sum((r["score"] - ms) ** 2 for r in sel) / (len(sel) - 1))
        cov = sum((r["pos"] - mp) * (r["score"] - ms) for r in sel) / (len(sel) - 1)
        if sp > 0 and ss > 0:
            print(f"  （共线性参考：corr(pos, entry_score) = {cov / (sp * ss):+.3f}）")

    # 乖离同样做一遍控制（它是另一条惩罚触发条件）
    sel_d = [r for r in rows if r["dev"] is not None and r["score"] is not None]
    if len(sel_d) >= 5:
        b1, t1, _, _ = ols([r["dev"] for r in sel_d], [r["r"] - r[b] for r in sel_d])
        print(f"{'单变量 dev':<24}{len(sel_d):>6}{b1:>12.3f}{t1:>10.2f}{'-':>11}{'-':>10}")
        b1, t1, b2, t2, _, n2 = ols2(
            [r["dev"] for r in sel_d], [r["score"] for r in sel_d],
            [r["r"] - r[b] for r in sel_d],
        )
        print(f"{'dev + entry_score':<24}{n2:>6}{b1:>12.3f}{t1:>10.2f}"
              f"{b2:>11.3f}{t2:>10.2f}")

    # ---- 组间差异检验 ----
    # 分桶均值看着有差别不等于差别显著。这里对想下结论的对比逐个做 Welch 检验。
    print("\n=== 组间差异检验（中证1000 基准，Welch t，|t|>1.96 约 p<0.05）===")
    print(f"{'对比':<30}{'n1':>6}{'n2':>6}{'差值':>9}{'t':>8}{'df':>9}")

    def cmp(name: str, g1, g2) -> None:
        x1 = [r["r"] - r[b] for r in rows if g1(r)]
        x2 = [r["r"] - r[b] for r in rows if g2(r)]
        d, t, df = welch(x1, x2)
        print(f"{name:<30}{len(x1):>6}{len(x2):>6}{d:>9.2f}{t:>8.2f}{df:>9.0f}")

    cmp("fenshi - soft",
        lambda r: r["st"] == "fenshi",
        lambda r: r["st"] == "soft")
    cmp("pos>=0.9 - pos<0.9",
        lambda r: r["pos"] is not None and r["pos"] >= 0.9,
        lambda r: r["pos"] is not None and r["pos"] < 0.9)
    cmp("dev>=2.5 - dev<2.5",
        lambda r: r["dev"] is not None and r["dev"] >= 2.5,
        lambda r: r["dev"] is not None and r["dev"] < 2.5)
    cmp("被追高罚 - 未被罚",
        lambda r: trig_pos(r) or trig_dev(r),
        lambda r: not trig_pos(r) and not trig_dev(r))
    cmp("soft score>=70 - score<50",
        lambda r: r["st"] == "soft" and r["score"] is not None and r["score"] >= 70,
        lambda r: r["st"] == "soft" and r["score"] is not None and r["score"] < 50)
    cmp("warn档 - 正常档",
        lambda r: r["sp"] is not None and r["sp"] <= -1.5,
        lambda r: r["sp"] is not None and r["sp"] > -1.5)

    return 0


if __name__ == "__main__":
    sys.exit(main())
