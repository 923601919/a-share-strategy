"use client";

import { useState } from "react";
import { addWatch, scan, type ScanItem, type ScanResult } from "@/lib/api";

export default function HomePage() {
  const [minAmount, setMinAmount] = useState(1);
  const [minPct, setMinPct] = useState(2);
  const [maxPct, setMaxPct] = useState(6);
  const [session, setSession] = useState("auto");
  const [topN, setTopN] = useState(20);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [data, setData] = useState<ScanResult | null>(null);
  const [msg, setMsg] = useState("");

  async function onScan() {
    setLoading(true);
    setError("");
    setMsg("");
    try {
      const res = await scan({
        min_amount_yi: minAmount,
        min_pct: minPct,
        max_pct: maxPct,
        session,
        top_n: topN,
      });
      setData(res);
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  async function onAdd(item: ScanItem) {
    try {
      await addWatch({
        code: item.code,
        name: item.name,
        source: "fenshi",
        note: item.reasons.slice(0, 2).join("；"),
      });
      setMsg(`已加入自选：${item.name}(${item.code})`);
    } catch (e: any) {
      setError(e?.message || String(e));
    }
  }

  return (
    <>
      <section className="panel">
        <h1 style={{ margin: "0 0 8px", fontSize: 20 }}>进攻型分时扫描</h1>
        <p className="muted" style={{ marginTop: 0 }}>
          板块强度 + 分时站上均价/放量上攻 + 异动红线过滤。扫描可能需要数十秒。
        </p>
        <div className="row">
          <label>
            成交额下限（亿）
            <input
              type="number"
              step="0.1"
              value={minAmount}
              onChange={(e) => setMinAmount(Number(e.target.value))}
            />
          </label>
          <label>
            最小涨幅%
            <input
              type="number"
              step="0.1"
              value={minPct}
              onChange={(e) => setMinPct(Number(e.target.value))}
            />
          </label>
          <label>
            最大涨幅%
            <input
              type="number"
              step="0.1"
              value={maxPct}
              onChange={(e) => setMaxPct(Number(e.target.value))}
            />
          </label>
          <label>
            时段
            <select value={session} onChange={(e) => setSession(e.target.value)}>
              <option value="auto">自动</option>
              <option value="morning">上午重点</option>
              <option value="afternoon">午安重点</option>
              <option value="any">不限</option>
            </select>
          </label>
          <label>
            返回条数
            <input
              type="number"
              value={topN}
              onChange={(e) => setTopN(Number(e.target.value))}
            />
          </label>
          <button onClick={onScan} disabled={loading}>
            {loading ? "扫描中…" : "立即扫描"}
          </button>
        </div>
        {msg ? <p className="muted">{msg}</p> : null}
        {error ? <p className="err">{error}</p> : null}
      </section>

      {data ? (
        <>
          <section className="panel">
            <div className="muted">{data.session_note}</div>
            <div className="muted" style={{ marginTop: 6 }}>
              数据源 spot={data.data_source?.spot ?? "?"} · 分时有效{" "}
              {data.data_source?.fenshi_ok ?? 0}/{data.data_source?.candidates ?? "?"} · 命中{" "}
              {data.count} 只
            </div>
            <h3 style={{ marginBottom: 8 }}>热门概念（参考）</h3>
            <div className="chips">
              {(data.hot_boards || []).slice(0, 12).map((b) => (
                <div className="chip" key={b.name}>
                  {b.name} {b.pct?.toFixed?.(2) ?? b.pct}%
                </div>
              ))}
            </div>
          </section>

          <section className="panel">
            <table>
              <thead>
                <tr>
                  <th>代码</th>
                  <th>名称</th>
                  <th>涨幅</th>
                  <th>得分</th>
                  <th>量比</th>
                  <th>异动</th>
                  <th>原因</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((it) => (
                  <tr key={it.code}>
                    <td>
                      <a
                        href={`https://quote.eastmoney.com/${it.code.startsWith("6") ? "sh" : "sz"}${it.code}.html`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {it.code}
                      </a>
                    </td>
                    <td>
                      {it.name}
                      {it.in_hot_board ? (
                        <div className="muted">热门板块</div>
                      ) : null}
                    </td>
                    <td>{it.pct}%</td>
                    <td>{it.score}</td>
                    <td>{it.volume_ratio}</td>
                    <td>
                      <span className={`pill ${it.risk?.level || "ok"}`}>
                        {it.risk?.anomaly_pct ?? "-"}%
                      </span>
                      <div className="bar" style={{ marginTop: 6 }}>
                        <i
                          style={{
                            width: `${Math.min(it.risk?.anomaly_progress || 0, 100)}%`,
                          }}
                        />
                      </div>
                    </td>
                    <td className="muted">{(it.reasons || []).join("；")}</td>
                    <td>
                      <button className="secondary" onClick={() => onAdd(it)}>
                        自选
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      ) : null}
    </>
  );
}
