"use client";

import { useEffect, useState } from "react";
import { getWatchlist, removeWatch, type WatchlistResponse } from "@/lib/api";

function fmtRet(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return "-";
  const n = Number(v);
  const cls = n > 0 ? "good" : n < 0 ? "bad" : "";
  return (
    <span className={cls}>
      {n > 0 ? "+" : ""}
      {n.toFixed(2)}%
    </span>
  );
}

export default function WatchPage() {
  const [data, setData] = useState<WatchlistResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");

  async function load(heavy = false) {
    if (heavy) setRefreshing(true);
    else setLoading(true);
    setError("");
    try {
      const res = await getWatchlist({
        with_quotes: heavy,
        refresh_returns: heavy,
      });
      setData(res);
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  useEffect(() => {
    // 先秒开库内自选，再后台拉行情/收益
    (async () => {
      await load(false);
      await load(true);
    })();
  }, []);

  async function onRemove(code: string) {
    await removeWatch(code);
    await load(false);
  }

  const items = data?.items || [];
  const stats = data?.stats;

  return (
    <>
      <section className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <h1 style={{ margin: "0 0 6px", fontSize: 20 }}>自选跟踪</h1>
            <p className="muted" style={{ margin: 0 }}>
              记录入池价，跟踪 T+0~T+3 涨跌幅。共 {items.length} 只
              {refreshing ? " · 正在刷新行情…" : ""}
            </p>
          </div>
          <button onClick={() => load(true)} disabled={loading || refreshing}>
            {refreshing ? "刷新中…" : "刷新行情/收益"}
          </button>
        </div>
        {error ? <p className="err">{error}</p> : null}
        {loading && items.length === 0 ? <p className="muted">加载自选中…</p> : null}
      </section>

      {stats && stats.with_t3 > 0 ? (
        <section className="panel">
          <h3 style={{ marginTop: 0 }}>策略验证统计（T+3 收盘相对入池价）</h3>
          <div className="row">
            <div className="chip">
              样本 {stats.with_t3} 笔 · 胜率 {stats.win_rate_t3}% · 均收益 {stats.avg_return_t3}%
            </div>
          </div>
          <div className="chips" style={{ marginTop: 10 }}>
            {Object.entries(stats.by_source || {}).map(([k, v]) => (
              <div className="chip" key={k}>
                {k}: 胜率{v.win_rate}% / 均{v.avg_return}% (n={v.count})
              </div>
            ))}
            {Object.entries(stats.by_score_bucket || {}).map(([k, v]) => (
              <div className="chip" key={k}>
                {k}: 胜率{v.win_rate}% / 均{v.avg_return}% (n={v.count})
              </div>
            ))}
          </div>
        </section>
      ) : (
        <section className="panel muted">
          暂无足够 T+3 历史样本。加入自选并等待 3 个交易日后可统计胜率。
        </section>
      )}

      <section className="panel">
        <table>
          <thead>
            <tr>
              <th>代码</th>
              <th>名称</th>
              <th>入池价</th>
              <th>现价</th>
              <th>入池分</th>
              <th>T+0</th>
              <th>T+1</th>
              <th>T+2</th>
              <th>T+3</th>
              <th>异动</th>
              <th>备注</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.length === 0 ? (
              <tr>
                <td colSpan={12} className="muted">
                  {loading ? "加载中…" : "暂无自选。去选股页扫描后加入。"}
                </td>
              </tr>
            ) : (
              items.map((it) => {
                const risk = it.quote?.risk;
                const tr = it.track;
                return (
                  <tr key={it.code}>
                    <td>{it.code}</td>
                    <td>{it.name}</td>
                    <td>{tr?.entry_price ?? it.entry_price ?? "-"}</td>
                    <td>
                      {it.quote?.price
                        ? it.quote.price
                        : refreshing
                          ? "…"
                          : "-"}
                    </td>
                    <td>{tr?.entry_score ?? it.entry_score ?? "-"}</td>
                    <td>{fmtRet(tr?.t0?.return_pct)}</td>
                    <td>{fmtRet(tr?.t1?.return_pct)}</td>
                    <td>{fmtRet(tr?.t2?.return_pct)}</td>
                    <td>{fmtRet(tr?.t3?.return_pct)}</td>
                    <td>
                      <span className={`pill ${risk?.level || "ok"}`}>
                        {risk?.anomaly_pct ?? "-"}%
                      </span>
                    </td>
                    <td className="muted" style={{ maxWidth: 200 }}>
                      {it.note || "-"}
                      <div className="muted" style={{ fontSize: 12 }}>
                        {it.created_at?.slice(0, 16)}
                      </div>
                    </td>
                    <td>
                      <button className="secondary" onClick={() => onRemove(it.code)}>
                        移除
                      </button>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </section>
    </>
  );
}
