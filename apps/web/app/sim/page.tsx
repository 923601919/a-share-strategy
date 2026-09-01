"use client";

import { useEffect, useState } from "react";
import {
  evaluateSim,
  getSim,
  resetSim,
  sellSim,
  type SimOverview,
} from "@/lib/api";

function fmtMoney(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return "-";
  return Number(v).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function fmtPct(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return "-";
  const n = Number(v);
  const cls = n > 0 ? "up" : n < 0 ? "down" : "";
  return (
    <span className={cls}>
      {n > 0 ? "+" : ""}
      {n.toFixed(2)}%
    </span>
  );
}

function fmtPnl(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return "-";
  const n = Number(v);
  const cls = n > 0 ? "up" : n < 0 ? "down" : "";
  return (
    <span className={cls}>
      {n > 0 ? "+" : ""}
      {fmtMoney(n)}
    </span>
  );
}

export default function SimPage() {
  const [data, setData] = useState<SimOverview | null>(null);
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");

  async function load() {
    setLoading(true);
    setError("");
    try {
      setData(await getSim());
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function onEvaluate() {
    setMsg("");
    setError("");
    try {
      const res = await evaluateSim();
      setData(res);
      setMsg("已检查止盈/止损条件单");
    } catch (e: any) {
      setError(e?.message || String(e));
    }
  }

  async function onSell(positionId: number) {
    if (!confirm("确认按现价卖出该持仓？")) return;
    setError("");
    try {
      await sellSim(positionId);
      setMsg("已卖出并记录盈亏");
      await load();
    } catch (e: any) {
      setError(e?.message || String(e));
    }
  }

  async function onReset() {
    if (!confirm("确认重置模拟盘？将清空持仓、条件单与交易记录，资金恢复为10万。")) return;
    setError("");
    try {
      setData(await resetSim(100000));
      setMsg("模拟盘已重置为 10 万");
    } catch (e: any) {
      setError(e?.message || String(e));
    }
  }

  const acct = data?.account;
  const stats = data?.stats;

  return (
    <>
      <section className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <h1 style={{ margin: "0 0 8px", fontSize: 20 }}>模拟盘</h1>
            <p className="muted" style={{ margin: 0 }}>
              初始资金 10 万。加入自选时自动开仓并挂止盈/止损；卖出遵循 A 股 T+1（当日买入不可卖）。
            </p>
          </div>
          <div className="row">
            <button className="secondary" onClick={onEvaluate} disabled={loading}>
              检查条件单
            </button>
            <button className="secondary" onClick={load} disabled={loading}>
              {loading ? "刷新中…" : "刷新"}
            </button>
            <button className="secondary" onClick={onReset}>
              重置
            </button>
          </div>
        </div>
        {msg ? <p className="up">{msg}</p> : null}
        {error ? <p className="err">{error}</p> : null}
      </section>

      {acct ? (
        <section className="panel">
          <div className="chips">
            <div className="chip">总权益 {fmtMoney(acct.equity)}</div>
            <div className="chip">现金 {fmtMoney(acct.cash)}</div>
            <div className="chip">市值 {fmtMoney(acct.market_value)}</div>
            <div className="chip">总盈亏 {fmtPnl(acct.total_pnl)} ({fmtPct(acct.total_pnl_pct)})</div>
            <div className="chip">已实现 {fmtPnl(acct.realized_pnl)}</div>
            <div className="chip">
              持仓 {stats?.open_count ?? 0}/{stats?.max_positions ?? 5}
            </div>
            <div className="chip">
              胜率 {stats?.win_rate != null ? `${stats.win_rate}%` : "-"} · 卖出均收益{" "}
              {stats?.avg_sell_pnl_pct != null ? `${stats.avg_sell_pnl_pct}%` : "-"}
            </div>
            <div className="chip">
              止盈策略 分时+{stats?.take_profit_by_source?.fenshi ?? stats?.take_profit_pct}% /
              龙头+{stats?.take_profit_by_source?.longtou ?? stats?.take_profit_pct}% /
              默认+{stats?.take_profit_pct}%
            </div>
          </div>
        </section>
      ) : null}

      <section className="panel">
        <h2 style={{ marginTop: 0, fontSize: 16 }}>持仓</h2>
        <table>
          <thead>
            <tr>
              <th>代码</th>
              <th>名称</th>
              <th>来源</th>
              <th>股数</th>
              <th>成本</th>
              <th>现价</th>
              <th>浮盈</th>
              <th>止盈价</th>
              <th>止损价</th>
              <th>市值</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {(data?.positions || []).length === 0 ? (
              <tr>
                <td colSpan={11} className="muted">
                  暂无持仓。在选股页点「自选」会自动按约 20% 仓位开仓。
                </td>
              </tr>
            ) : (
              (data?.positions || []).map((p) => (
                <tr key={p.id}>
                  <td>{p.code}</td>
                  <td>{p.name}</td>
                  <td className="muted">
                    {p.source === "fenshi"
                      ? "分时"
                      : p.source === "longtou"
                        ? "龙头"
                        : p.source || "默认"}
                    {p.take_profit_pct != null ? ` +${p.take_profit_pct}%` : ""}
                  </td>
                  <td>{p.shares}</td>
                  <td>{p.cost_price}</td>
                  <td>{p.quote_price ?? "-"}</td>
                  <td>
                    {fmtPnl(p.unrealized_pnl)} {fmtPct(p.unrealized_pct)}
                  </td>
                  <td className="up">{p.take_profit_price ?? "-"}</td>
                  <td className="down">{p.stop_loss_price ?? "-"}</td>
                  <td>{fmtMoney(p.market_value)}</td>
                  <td>
                    {p.t1_sellable === false ? (
                      <span className="muted" title={p.t1_lock_reason || "T+1"}>
                        T+1锁定
                      </span>
                    ) : (
                      <button className="secondary" onClick={() => onSell(p.id)}>
                        卖出
                      </button>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>

      <section className="panel">
        <h2 style={{ marginTop: 0, fontSize: 16 }}>活跃条件单</h2>
        <table>
          <thead>
            <tr>
              <th>类型</th>
              <th>代码</th>
              <th>触发价</th>
              <th>幅度</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            {(data?.orders || []).length === 0 ? (
              <tr>
                <td colSpan={5} className="muted">
                  暂无活跃条件单
                </td>
              </tr>
            ) : (
              (data?.orders || []).map((o) => (
                <tr key={o.id}>
                  <td>
                    <span className={o.order_type === "take_profit" ? "up" : "down"}>
                      {o.order_type === "take_profit" ? "止盈" : "止损"}
                    </span>
                  </td>
                  <td>
                    {o.name}
                    <div className="muted">{o.code}</div>
                  </td>
                  <td>{o.trigger_price}</td>
                  <td>
                    {o.trigger_pct != null
                      ? `${o.order_type === "take_profit" ? "+" : "-"}${Math.abs(Number(o.trigger_pct))}%`
                      : "-"}
                  </td>
                  <td className="muted">{o.reason}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>

      <section className="panel">
        <h2 style={{ marginTop: 0, fontSize: 16 }}>交易记录</h2>
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>方向</th>
              <th>代码</th>
              <th>股数</th>
              <th>价格</th>
              <th>金额</th>
              <th>盈亏</th>
              <th>原因</th>
            </tr>
          </thead>
          <tbody>
            {(data?.trades || []).length === 0 ? (
              <tr>
                <td colSpan={8} className="muted">
                  暂无成交
                </td>
              </tr>
            ) : (
              (data?.trades || []).map((t) => (
                <tr key={t.id}>
                  <td className="muted">{String(t.traded_at).replace("T", " ").slice(0, 19)}</td>
                  <td className={t.side === "buy" ? "up" : "down"}>
                    {t.side === "buy" ? "买" : "卖"}
                  </td>
                  <td>
                    {t.name}
                    <div className="muted">{t.code}</div>
                  </td>
                  <td>{t.shares}</td>
                  <td>{t.price}</td>
                  <td>{fmtMoney(t.amount)}</td>
                  <td>
                    {t.side === "sell" ? (
                      <>
                        {fmtPnl(t.pnl)} {fmtPct(t.pnl_pct)}
                      </>
                    ) : (
                      "-"
                    )}
                  </td>
                  <td className="muted">{t.reason}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>
    </>
  );
}
