# -*- coding: utf-8 -*-
"""抓取基准指数日线，落成本地 CSV（供分析/回灌使用）。

用法：
    python scripts/fetch_bench_index.py 000852 000905

输出：data/_bench_<code>.csv，列：date,close,prev_close,source
只依赖 akshare，不碰数据库（落库由 apps/api 侧的 sync_index_daily 负责）。
"""
from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data"

# 距今多久的历史。样本期约 1 个月，抓半年足够（且能覆盖 prev_close 基准）。
START = "20260101"


def _fetch_em(code: str):
    import akshare as ak

    df = ak.index_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=START,
        end_date=date.today().strftime("%Y%m%d"),
    )
    if df is None or len(df) == 0:
        raise RuntimeError("em 空结果")
    df = df.rename(columns={"日期": "date", "收盘": "close"})
    return df[["date", "close"]], "em"


def _fetch_sina(code: str):
    import akshare as ak

    sym = f"sh{code}" if code.startswith(("000", "999")) else f"sz{code}"
    df = ak.stock_zh_index_daily(symbol=sym)
    if df is None or len(df) == 0:
        raise RuntimeError("sina 空结果")
    if "date" not in df.columns and "日期" in df.columns:
        df = df.rename(columns={"日期": "date"})
    out = df[["date", "close"]].copy()
    out["date"] = out["date"].astype(str).str[:10]
    cutoff = f"{START[:4]}-{START[4:6]}-{START[6:]}"
    out = out[out["date"] >= cutoff]
    return out, "sina"


def main() -> int:
    codes = sys.argv[1:] or ["000852"]
    OUT_DIR.mkdir(exist_ok=True)
    for code in codes:
        last_err = ""
        for label, fn in (("em", _fetch_em), ("sina", _fetch_sina)):
            try:
                df, source = fn(code)
                break
            except Exception as e:  # noqa: BLE001
                last_err = f"{label}: {e}"
                df = None
        if df is None:
            print(f"[FAIL] {code} 全部源失败 -> {last_err}")
            continue

        rows = []
        for _, r in df.iterrows():
            d = r["date"]
            iso = d.date().isoformat() if hasattr(d, "date") else str(d)[:10]
            try:
                close = float(r["close"])
            except Exception:  # noqa: BLE001
                continue
            if close <= 0:
                continue
            rows.append((iso, close))
        rows.sort(key=lambda x: x[0])

        path = OUT_DIR / f"_bench_{code}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "close", "prev_close", "source"])
            prev = ""
            for iso, close in rows:
                w.writerow([iso, f"{close:.4f}", prev, source])
                prev = f"{close:.4f}"
        print(f"[OK] {code} source={source} n={len(rows)} "
              f"{rows[0][0]}~{rows[-1][0]} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
