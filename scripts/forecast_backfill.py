#!/usr/bin/env python
"""回补历史大盘预判（**全部标记 origin='replay'**）。

这是一个**运维脚本**，不是接口。刻意不做成 API：任何"手动补一条"的入口
都等于允许事后挑一个能摇出好卦的日子，而准确率一旦可以被事后挑选，就失去全部意义。

它唯一能做的事是：让 /market 页在功能刚上线时不是一片空白，并可用来检查链路
（起卦 → 落库 → 展示）。**replay 记录不进准确率**——统计层只取 origin='live'。

起卦日不是人手挑的：脚本按交易日历取**目标日的上一个交易日**。
这样每一条回补记录的 as_of 都由规则决定，不由人决定。

用法（本地或服务器）：

    # 预演，不写库
    python scripts/forecast_backfill.py --from 2026-06-01 --to 2026-09-17 --dry-run

    # 实际写入
    python scripts/forecast_backfill.py --from 2026-06-01 --to 2026-09-17

在服务器上（docker）：
    docker compose exec api python /app/scripts/forecast_backfill.py --from ... --to ...

⚠ 不要用它去"补齐"某个漏跑的交易日。漏跑就该显示为漏跑——
   runs 页面的缺口列表正是为此存在的。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "apps" / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

# 最多向前找多少天来确定"上一个交易日"。A 股最长连休约 10 个自然日，30 天足够。
_PREV_LOOKBACK_DAYS = 30


def previous_trade_date(d: date, is_trade_date) -> date | None:
    """d 之前最近的一个交易日。越界返回 None（不猜）。"""
    for i in range(1, _PREV_LOOKBACK_DAYS + 1):
        cand = d - timedelta(days=i)
        if is_trade_date(cand):
            return cand
    return None


def trade_dates_between(start: date, end: date, is_trade_date) -> list[date]:
    out: list[date] = []
    cur = start
    while cur <= end:
        if is_trade_date(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="回补历史大盘预判（replay，不进准确率）")
    ap.add_argument("--from", dest="start", required=True, help="起始目标日 YYYY-MM-DD")
    ap.add_argument("--to", dest="end", required=True, help="结束目标日 YYYY-MM-DD")
    ap.add_argument("--symbol", default=None, help="标的，默认取配置 MARKET_FORECAST_SYMBOL")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要写入的内容，不落库")
    args = ap.parse_args()

    from db import init_db
    from services.trade_calendar import is_trade_date

    from forecast.service import backfill_replay, symbol_of

    init_db()
    symbol = args.symbol or symbol_of()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end < start:
        print(f"结束日 {end} 早于起始日 {start}", file=sys.stderr)
        return 2

    targets = trade_dates_between(start, end, is_trade_date)
    if not targets:
        print("区间内没有交易日，什么都不做。")
        return 0

    # 起卦日 = 目标日的上一个交易日。**不提供 --as-of 之类的覆盖参数**：
    # 允许调用方为每条记录挑起卦时点，正是这个功能最不能开的口子。
    as_of_map: dict[str, str] = {}
    skipped: list[str] = []
    for t in targets:
        prev = previous_trade_date(t, is_trade_date)
        if prev is None:
            skipped.append(t.isoformat())
            continue
        as_of_map[t.isoformat()] = prev.isoformat()

    print(f"标的 {symbol}；区间 {start} ~ {end}")
    print(f"交易日 {len(targets)} 个，可定出起卦日 {len(as_of_map)} 条，跳过 {len(skipped)} 条")
    if skipped:
        print(f"  跳过（算不出上一交易日，不猜）：{', '.join(skipped)}")

    if args.dry_run:
        for td, ao in sorted(as_of_map.items()):
            print(f"  [dry-run] target={td} as_of={ao} origin=replay")
        print("\n--dry-run：未写入任何记录。")
        return 0

    result = backfill_replay(symbol, sorted(as_of_map), as_of_map)
    print(f"新建 {len(result['created'])} 条，已存在跳过 {len(result['skipped'])} 条，失败 {len(result['failed'])} 条")
    for f in result["failed"]:
        print(f"  ✗ {f['target_date']}: {f['reason']}", file=sys.stderr)

    print("\n提醒：这些是 replay 记录，不进准确率。要产生可计入准确率的样本，")
    print("      只能等每个交易日 15:45 的定时作业自然产出（live）。")
    return 0 if not result["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
