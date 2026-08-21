"use client";

import { useEffect, useState } from "react";
import { getWatchlist, removeWatch } from "@/lib/api";

type WatchItem = {
  code: string;
  name: string;
  source: string;
  note: string;
  quote?: {
    price?: number;
    pct?: number;
    risk?: {
      level?: string;
      messages?: string[];
      anomaly_progress?: number;
      anomaly_pct?: number;
      ma5?: number;
      auction_sell_hint?: boolean;
      below_ma5?: boolean;
    };
  };
};

export default function WatchPage() {
  const [items, setItems] = useState<WatchItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    setLoading(true);
    setError("");
    try {
      const res = await getWatchlist();
      setItems(res.items || []);
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function onRemove(code: string) {
    await removeWatch(code);
    await load();
  }

  return (
    <>
      <section className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <h1 style={{ margin: "0 0 6px", fontSize: 20 }}>自选跟踪</h1>
            <p className="muted" style={{ margin: 0 }}>
              异动红线进度、五日线位置、竞价卖点提示。
            </p>
          </div>
          <button onClick={load} disabled={loading}>
            {loading ? "刷新中…" : "刷新行情"}
          </button>
        </div>
        {error ? <p className="err">{error}</p> : null}
      </section>

      <section className="panel">
        <table>
          <thead>
            <tr>
              <th>代码</th>
              <th>名称</th>
              <th>现价</th>
              <th>涨幅</th>
              <th>来源</th>
              <th>异动进度</th>
              <th>提示</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.length === 0 ? (
              <tr>
                <td colSpan={8} className="muted">
                  暂无自选。去选股页扫描后加入。
                </td>
              </tr>
            ) : (
              items.map((it) => {
                const risk = it.quote?.risk;
                return (
                  <tr key={it.code}>
                    <td>{it.code}</td>
                    <td>{it.name}</td>
                    <td>{it.quote?.price ?? "-"}</td>
                    <td>{it.quote?.pct != null ? `${it.quote.pct}%` : "-"}</td>
                    <td>{it.source}</td>
                    <td>
                      <span className={`pill ${risk?.level || "ok"}`}>
                        {risk?.anomaly_pct ?? "-"}% / 200%
                      </span>
                      <div className="bar" style={{ marginTop: 6 }}>
                        <i
                          style={{
                            width: `${Math.min(risk?.anomaly_progress || 0, 100)}%`,
                          }}
                        />
                      </div>
                      {risk?.ma5 != null ? (
                        <div className="muted">MA5 {risk.ma5}</div>
                      ) : null}
                    </td>
                    <td className="muted">
                      {(risk?.messages || []).join("；") || it.note || "-"}
                      {risk?.auction_sell_hint ? (
                        <div style={{ color: "var(--bad)" }}>竞价卖点提示</div>
                      ) : null}
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
