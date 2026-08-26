"use client";

import { useEffect, useState } from "react";
import {
  getLatestReview,
  getReviewHistory,
  runReview,
  type ConditionOrder,
  type ReviewResult,
} from "@/lib/api";

function fmtPct(v: number | null | undefined) {
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

export default function ReviewPage() {
  const [data, setData] = useState<ReviewResult | null>(null);
  const [history, setHistory] = useState<{ trade_date: string }[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [side, setSide] = useState<"all" | "buy" | "sell">("all");

  async function loadLatest() {
    setError("");
    try {
      const res = await getLatestReview();
      setData(res);
    } catch {
      setData(null);
    }
    try {
      const h = await getReviewHistory();
      setHistory(h.items || []);
    } catch {
      setHistory([]);
    }
  }

  useEffect(() => {
    loadLatest();
  }, []);

  async function onRun() {
    setLoading(true);
    setError("");
    try {
      const res = await runReview({ persist: true });
      setData(res);
      const h = await getReviewHistory();
      setHistory(h.items || []);
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  const allOrders = data?.orders || [];
  const orders = allOrders.filter((o) => side === "all" || o.side === side);
  const sellCount = allOrders.filter((o) => o.side === "sell").length;
  const buyCount = allOrders.filter((o) => o.side === "buy").length;

  return (
    <>
      <section className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <h1 style={{ margin: "0 0 6px", fontSize: 20 }}>收盘复盘</h1>
            <p className="muted" style={{ margin: 0 }}>
              复盘当日自选/板块，并生成次日买卖条件单（竞价破五日线卖、回踩再攻买）。
            </p>
          </div>
          <button onClick={onRun} disabled={loading}>
            {loading ? "复盘生成中…" : "生成今日复盘"}
          </button>
        </div>
        {error ? <p className="err">{error}</p> : null}
        {history.length > 0 ? (
          <div className="chips" style={{ marginTop: 12 }}>
            {history.slice(0, 8).map((h) => (
              <div className="chip" key={h.trade_date}>
                {h.trade_date}
              </div>
            ))}
          </div>
        ) : null}
      </section>

      {!data ? (
        <section className="panel muted">
          暂无复盘记录。收盘后点击「生成今日复盘」（约 1–2 分钟，会拉行情与分时）。
        </section>
      ) : (
        <>
          <section className="panel">
            <h3 style={{ marginTop: 0 }}>
              {data.trade_date} 结论
            </h3>
            {data.summary?.macro_weak ? (
              <div className="err" style={{ marginBottom: 12, padding: "10px 12px", borderRadius: 8 }}>
                <strong>外盘偏弱</strong>
                <div style={{ marginTop: 4 }}>
                  {data.summary?.global_macro?.weak_reason ||
                    "隔夜外盘走弱，次日竞价优先考虑卖出/减仓。"}
                </div>
                {(data.summary?.global_macro?.indices || []).length > 0 ? (
                  <div className="chips" style={{ marginTop: 8 }}>
                    {(data.summary?.global_macro?.indices || []).slice(0, 5).map((i) => (
                      <div className="chip" key={i.name}>
                        {i.name} {i.pct > 0 ? "+" : ""}
                        {i.pct}%
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : (data.summary?.global_macro?.indices || []).length > 0 ? (
              <div className="chips" style={{ marginBottom: 12 }}>
                {(data.summary?.global_macro?.indices || []).slice(0, 5).map((i) => (
                  <div className="chip" key={i.name}>
                    {i.name} {i.pct > 0 ? "+" : ""}
                    {i.pct}%
                  </div>
                ))}
              </div>
            ) : null}
            <p style={{ marginTop: 0 }}>{data.summary?.verdict}</p>
            <div className="chips">
              <div className="chip">自选 {data.summary?.watch_count ?? 0}</div>
              <div className="chip">
                涨 {data.summary?.watch_up ?? 0} / 跌 {data.summary?.watch_down ?? 0}
              </div>
              <div className="chip">回踩再攻 {data.summary?.reattack_count ?? 0}</div>
              <div className="chip">
                条件单 卖{data.summary?.sell_orders ?? 0} / 买{data.summary?.buy_orders ?? 0}
              </div>
            </div>
            <h4 style={{ marginBottom: 8 }}>次日执行清单</h4>
            <ul className="muted" style={{ marginTop: 0 }}>
              {(data.next_day_checklist || []).map((x) => (
                <li key={x}>{x}</li>
              ))}
            </ul>
          </section>

          <section className="panel">
            <h3 style={{ marginTop: 0 }}>主线板块</h3>
            <div className="chips">
              {(data.boards || []).slice(0, 10).map((b) => (
                <div className="chip" key={b.name}>
                  {b.name} {b.pct?.toFixed?.(2) ?? b.pct}%
                </div>
              ))}
            </div>
          </section>

          <section className="panel">
            <h3 style={{ marginTop: 0 }}>自选当日复盘</h3>
            <table>
              <thead>
                <tr>
                  <th>代码</th>
                  <th>名称</th>
                  <th>入池价</th>
                  <th>现价</th>
                  <th>当日相对入池</th>
                  <th>分时</th>
                  <th>MA5</th>
                  <th>异动</th>
                </tr>
              </thead>
              <tbody>
                {(data.watch_reviews || []).length === 0 ? (
                  <tr>
                    <td colSpan={8} className="muted">
                      暂无自选，复盘以板块与扫描参考为主。
                    </td>
                  </tr>
                ) : (
                  data.watch_reviews.map((w) => (
                    <tr key={w.code}>
                      <td>{w.code}</td>
                      <td>{w.name}</td>
                      <td>{w.entry_price ?? "-"}</td>
                      <td>{w.price ?? "-"}</td>
                      <td>{fmtPct(w.day_return_pct)}</td>
                      <td>
                        {w.fenshi?.pullback && w.fenshi?.reattack
                          ? "回踩再攻"
                          : w.fenshi?.score != null
                            ? `分${w.fenshi.score}`
                            : "-"}
                      </td>
                      <td>{w.daily?.ma5 ?? "-"}</td>
                      <td>
                        <span className={`pill ${w.risk?.level || "ok"}`}>
                          {w.risk?.anomaly_pct ?? w.daily?.pct_from_low ?? "-"}%
                        </span>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </section>

          <section className="panel">
            <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
              <h3 style={{ margin: 0 }}>次日条件单推荐</h3>
              <div className="row">
                <button
                  className={side === "all" ? "" : "secondary"}
                  onClick={() => setSide("all")}
                >
                  全部
                </button>
                <button
                  className={side === "sell" ? "" : "secondary"}
                  onClick={() => setSide("sell")}
                >
                  卖 ({sellCount})
                </button>
                <button
                  className={side === "buy" ? "" : "secondary"}
                  onClick={() => setSide("buy")}
                >
                  买 ({buyCount})
                </button>
              </div>
            </div>
            <table>
              <thead>
                <tr>
                  <th>方向</th>
                  <th>标的</th>
                  <th>条件单</th>
                  <th>触发条件</th>
                  <th>参考价</th>
                  <th>时段</th>
                  <th>理由</th>
                </tr>
              </thead>
              <tbody>
                {orders.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="muted">
                      暂无条件单。先加入自选或先扫描再复盘。
                    </td>
                  </tr>
                ) : (
                  orders.map((o: ConditionOrder) => (
                    <tr key={`${o.side}-${o.code}-${o.title}`}>
                      <td>
                        <span className={o.side === "buy" ? "good" : "bad"}>
                          {o.side === "buy" ? "买" : "卖"}
                        </span>
                      </td>
                      <td>
                        {o.name}
                        <div className="muted">{o.code}</div>
                      </td>
                      <td>{o.title}</td>
                      <td className="muted">{o.trigger}</td>
                      <td>{o.price_hint ?? "-"}</td>
                      <td>{o.window ?? "-"}</td>
                      <td className="muted">{o.reason}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </section>
        </>
      )}
    </>
  );
}
