"use client";

import { useEffect, useRef, useState } from "react";
import {
  getWatchlist,
  getWatchlistHistory,
  refreshWatchlistWithProgress,
  removeWatch,
  type WatchHistoryItem,
  type WatchlistResponse,
  type WatchlistStats,
  type WatchRefreshJob,
} from "@/lib/api";

type Tab = "active" | "history";

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

function fmtTime(v?: string | null) {
  if (!v) return "-";
  return v.slice(0, 16).replace("T", " ");
}

function reasonLabel(reason?: string | null) {
  if (!reason) return "-";
  if (reason === "auto_t3") return "超过T+3";
  if (reason === "manual") return "手动移除";
  return reason;
}

function retAt(item: WatchHistoryItem, day: number) {
  const row = item.returns?.find((r) => r.day_offset === day);
  return row?.return_pct;
}

export default function WatchPage() {
  const [tab, setTab] = useState<Tab>("active");
  const [data, setData] = useState<WatchlistResponse | null>(null);
  const [history, setHistory] = useState<WatchHistoryItem[]>([]);
  const [historyStats, setHistoryStats] = useState<WatchlistStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [refreshingQuotes, setRefreshingQuotes] = useState(false);
  const [refreshingReturns, setRefreshingReturns] = useState(false);
  const [progress, setProgress] = useState<WatchRefreshJob | null>(null);
  const [error, setError] = useState("");
  const [expiredNotice, setExpiredNotice] = useState("");
  const stopRefreshRef = useRef(false);

  function applyWatchResult(res: WatchlistResponse) {
    setData(res);
    if (res.expired?.length) {
      const names = res.expired.map((e) => `${e.code}${e.name ? ` ${e.name}` : ""}`).join("、");
      setExpiredNotice(`已自动归档并移出 ${res.expired.length} 只（超过 T+3）：${names}`);
      setHistoryLoaded(false);
    }
  }

  async function loadLight() {
    setLoading(true);
    setError("");
    setExpiredNotice("");
    try {
      const res = await getWatchlist();
      applyWatchResult(res);
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  async function loadHistory(force = false) {
    if (historyLoaded && !force) return;
    setHistoryLoading(true);
    setError("");
    try {
      const res = await getWatchlistHistory(200);
      setHistory(res.items || []);
      setHistoryStats(res.stats || null);
      setHistoryLoaded(true);
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setHistoryLoading(false);
    }
  }

  async function refreshQuotes() {
    setRefreshingQuotes(true);
    setError("");
    setExpiredNotice("");
    try {
      const res = await getWatchlist({ with_quotes: true });
      applyWatchResult(res);
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setRefreshingQuotes(false);
    }
  }

  async function refreshReturns() {
    stopRefreshRef.current = false;
    setRefreshingReturns(true);
    setError("");
    setExpiredNotice("");
    setProgress({
      job_id: "",
      kind: "watch_refresh",
      status: "queued",
      stage: "queued",
      progress: 0,
      message: "准备刷新…",
    });
    try {
      const res = await refreshWatchlistWithProgress({
        with_quotes: true,
        with_risk: true,
        onProgress: (job) => setProgress(job),
        shouldStop: () => stopRefreshRef.current,
      });
      applyWatchResult(res);
    } catch (e: any) {
      if (e?.code !== "cancelled") {
        setError(e?.message || String(e));
      }
    } finally {
      setRefreshingReturns(false);
      setProgress(null);
    }
  }

  function onCancelRefresh() {
    stopRefreshRef.current = true;
  }

  useEffect(() => {
    loadLight();
  }, []);

  useEffect(() => {
    if (tab === "history") {
      loadHistory();
    }
  }, [tab]);

  async function onRemove(code: string) {
    await removeWatch(code);
    setHistoryLoaded(false);
    await loadLight();
  }

  function switchTab(next: Tab) {
    setTab(next);
    setError("");
  }

  const items = data?.items || [];
  const stats = (tab === "history" ? historyStats : data?.stats) || data?.stats;
  const busy = loading || refreshingQuotes || refreshingReturns || historyLoading;
  const archived = history;
  const pct = Math.round((progress?.progress || 0) * 100);

  return (
    <>
      <section className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <h1 style={{ margin: "0 0 6px", fontSize: 20 }}>自选跟踪</h1>
            <p className="muted" style={{ margin: 0 }}>
              {tab === "active"
                ? `当前自选 ${items.length} 只 · 超过 T+3 会自动归档`
                : `履历 ${archived.length} 笔 · 含手动移除与超过 T+3 归档`}
              {refreshingQuotes ? " · 刷新现价中…" : ""}
            </p>
          </div>
          {tab === "active" ? (
            <div className="row">
              <button className="secondary" onClick={refreshQuotes} disabled={busy}>
                {refreshingQuotes ? "现价…" : "刷新现价"}
              </button>
              {refreshingReturns ? (
                <button className="secondary" onClick={onCancelRefresh}>
                  取消刷新
                </button>
              ) : (
                <button onClick={refreshReturns} disabled={busy}>
                  刷新收益/异动
                </button>
              )}
            </div>
          ) : (
            <button className="secondary" onClick={() => loadHistory(true)} disabled={busy}>
              {historyLoading ? "加载…" : "刷新履历"}
            </button>
          )}
        </div>

        <div className="row" style={{ marginTop: 14, gap: 8 }}>
          <button
            className={tab === "active" ? undefined : "secondary"}
            onClick={() => switchTab("active")}
            disabled={busy && tab !== "active"}
          >
            当前自选
          </button>
          <button
            className={tab === "history" ? undefined : "secondary"}
            onClick={() => switchTab("history")}
            disabled={busy && tab !== "history"}
          >
            履历
          </button>
        </div>

        {refreshingReturns && progress ? (
          <div style={{ marginTop: 12 }}>
            <div className="muted" style={{ marginBottom: 6 }}>
              {progress.message || progress.stage} · {pct}%
            </div>
            <div className="bar">
              <i style={{ width: `${Math.min(Math.max(pct, 2), 100)}%` }} />
            </div>
          </div>
        ) : null}

        {error ? <p className="err">{error}</p> : null}
        {expiredNotice && tab === "active" ? <p className="muted">{expiredNotice}</p> : null}
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

      {tab === "active" ? (
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
                      <td>{it.quote?.price ? it.quote.price : "-"}</td>
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
                          {fmtTime(it.created_at)}
                        </div>
                      </td>
                      <td>
                        <button className="secondary" onClick={() => onRemove(it.code)} disabled={busy}>
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
      ) : (
        <section className="panel">
          <table>
            <thead>
              <tr>
                <th>代码</th>
                <th>名称</th>
                <th>来源</th>
                <th>入池价</th>
                <th>入池分</th>
                <th>T+0</th>
                <th>T+1</th>
                <th>T+2</th>
                <th>T+3</th>
                <th>退出收益</th>
                <th>结束原因</th>
                <th>入池/归档</th>
              </tr>
            </thead>
            <tbody>
              {archived.length === 0 ? (
                <tr>
                  <td colSpan={12} className="muted">
                    {historyLoading
                      ? "加载履历中…"
                      : "暂无履历。手动移除或超过 T+3 自动归档后会出现在这里。"}
                  </td>
                </tr>
              ) : (
                archived.map((it) => (
                  <tr key={it.id}>
                    <td>{it.code}</td>
                    <td>{it.name}</td>
                    <td className="muted">{it.source || "-"}</td>
                    <td>{it.entry_price ?? "-"}</td>
                    <td>{it.entry_score ?? "-"}</td>
                    <td>{fmtRet(retAt(it, 0))}</td>
                    <td>{fmtRet(retAt(it, 1))}</td>
                    <td>{fmtRet(retAt(it, 2))}</td>
                    <td>{fmtRet(it.t3_return_pct ?? retAt(it, 3))}</td>
                    <td>{fmtRet(it.exit_return_pct)}</td>
                    <td>{reasonLabel(it.completion_reason)}</td>
                    <td className="muted" style={{ fontSize: 12, whiteSpace: "nowrap" }}>
                      <div>{fmtTime(it.created_at)}</div>
                      <div>→ {fmtTime(it.removed_at)}</div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </section>
      )}
    </>
  );
}
