"use client";

import { useState } from "react";
import { addWatch, scan, type ScanItem, type ScanResult } from "@/lib/api";

type ScanMode = "fenshi" | "leader_dip";

type ModeFormState = {
  minPct: number;
  maxPct: number;
};

const MODE_DEFAULTS: Record<ScanMode, ModeFormState> = {
  fenshi: { minPct: 2, maxPct: 6 },
  leader_dip: { minPct: -3, maxPct: 2 },
};

function createModeState(): Record<ScanMode, ModeFormState> {
  return {
    fenshi: { ...MODE_DEFAULTS.fenshi },
    leader_dip: { ...MODE_DEFAULTS.leader_dip },
  };
}

export default function HomePage() {
  const [mode, setMode] = useState<ScanMode>("fenshi");
  const [formByMode, setFormByMode] = useState(createModeState);
  const [dataByMode, setDataByMode] = useState<Record<ScanMode, ScanResult | null>>({
    fenshi: null,
    leader_dip: null,
  });
  const [minAmount, setMinAmount] = useState(1);
  const [session, setSession] = useState("auto");
  const [topN, setTopN] = useState(20);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [msg, setMsg] = useState("");
  const [addingCode, setAddingCode] = useState("");

  const { minPct, maxPct } = formByMode[mode];
  const data = dataByMode[mode];

  function onModeChange(next: ScanMode) {
    setMode(next);
    setError("");
    setMsg("");
  }

  function updateForm(patch: Partial<ModeFormState>) {
    setFormByMode((prev) => ({
      ...prev,
      [mode]: { ...prev[mode], ...patch },
    }));
  }

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
        mode,
      });
      setDataByMode((prev) => ({ ...prev, [mode]: res }));
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  async function onAdd(item: ScanItem) {
    setError("");
    setMsg("");
    setAddingCode(item.code);
    const proxy = Boolean((item.fenshi as { proxy?: boolean })?.proxy);
    try {
      const res = await addWatch({
        code: item.code,
        name: item.name,
        source: mode === "leader_dip" ? "longtou" : "fenshi",
        note: (item.reasons || []).slice(0, 2).join("；"),
        entry_price: item.price,
        entry_pct: item.pct,
        entry_score: item.score,
        minute_confirmed: !proxy,
      });
      const sim = res?.sim;
      if (sim?.ok && sim.position) {
        setMsg(
          `已入自选并模拟开仓：${res.name}(${res.code}) ${sim.position.shares}股 @${sim.position.cost_price}，止盈${sim.position.take_profit_price}/止损${sim.position.stop_loss_price}`
        );
      } else {
        setMsg(
          `已加入自选：${res.name}(${res.code})` +
            (sim?.reason ? `（模拟盘：${sim.reason}）` : "")
        );
      }
    } catch (e: any) {
      setError(`加入自选失败：${e?.message || String(e)}`);
    } finally {
      setAddingCode("");
    }
  }

  const modeTitle = mode === "leader_dip" ? "龙头低吸扫描" : "进攻型分时扫描";
  const modeDesc =
    mode === "leader_dip"
      ? "强势板块龙头，水下/平盘贴近 MA5 低吸。默认涨幅 -3% ~ +2%。"
      : "强势板块成分内选股 + 回踩均价放量再攻 / 强势推升。默认涨幅 2% ~ <6%。";
  const maxPctLabel = mode === "leader_dip" ? "涨幅上限≤%" : "当前涨幅<%";

  return (
    <>
      <section className="panel">
        <h1 style={{ margin: "0 0 8px", fontSize: 20 }}>{modeTitle}</h1>
        <p className="muted" style={{ marginTop: 0 }}>
          {modeDesc}
        </p>
        <div className="row" style={{ marginBottom: 12 }}>
          <button
            className={mode === "fenshi" ? "" : "secondary"}
            onClick={() => onModeChange("fenshi")}
          >
            进攻型分时
          </button>
          <button
            className={mode === "leader_dip" ? "" : "secondary"}
            onClick={() => onModeChange("leader_dip")}
          >
            龙头低吸
          </button>
        </div>
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
            涨幅下限%
            <input
              type="number"
              step="0.1"
              value={minPct}
              onChange={(e) => updateForm({ minPct: Number(e.target.value) })}
            />
          </label>
          <label>
            {maxPctLabel}
            <input
              type="number"
              step="0.1"
              value={maxPct}
              onChange={(e) => updateForm({ maxPct: Number(e.target.value) })}
            />
          </label>
          <label>
            时段
            <select value={session} onChange={(e) => setSession(e.target.value)}>
              <option value="auto">自动</option>
              <option value="morning">上午重点</option>
              <option value="afternoon">午后重点</option>
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
        {msg ? <p className="good">{msg}</p> : null}
        {error ? <p className="err">{error}</p> : null}
      </section>

      {data ? (
        <>
          <section className="panel">
            <div className="muted">{data.session_note}</div>
            <div className="muted" style={{ marginTop: 6 }}>
              数据源 spot={data.data_source?.spot ?? "?"} · 候选池{" "}
              {data.data_source?.universe_size ?? "?"} · 分时确认{" "}
              {data.data_source?.fenshi_ok ?? 0}/{data.data_source?.candidates ?? "?"} · 回踩再攻{" "}
              {data.data_source?.reattack_ok ?? 0} · 强势推升{" "}
              {data.data_source?.strong_push_ok ?? 0} · 命中 {data.count} 只
            </div>
            <h3 style={{ marginBottom: 8 }}>强势板块（候选池来源）</h3>
            <div className="chips">
              {(data.universe_sectors || data.hot_boards || []).slice(0, 12).map((b) => (
                <div className="chip" key={`${b.name}-${b.type ?? ""}`}>
                  {b.name} {b.pct?.toFixed?.(2) ?? b.pct}%
                  {b.type ? ` (${b.type})` : ""}
                </div>
              ))}
            </div>
            <h3 style={{ marginTop: 12, marginBottom: 8 }}>同花顺行业参考</h3>
            <div className="chips">
              {(data.hot_boards || []).slice(0, 8).map((b) => (
                <div className="chip" key={`ref-${b.name}`}>
                  {b.name} {b.pct?.toFixed?.(2) ?? b.pct}%
                </div>
              ))}
            </div>
          </section>

          <section className="panel">
            {msg ? <p className="good">{msg}</p> : null}
            {error ? <p className="err">{error}</p> : null}
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
                {data.items.map((it) => {
                  const proxy = Boolean((it.fenshi as { proxy?: boolean })?.proxy);
                  return (
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
                        {proxy ? <div className="muted">代理分</div> : null}
                      </td>
                      <td className={it.pct > 0 ? "up" : it.pct < 0 ? "down" : ""}>
                        {it.pct}%
                      </td>
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
                        <button
                          className="secondary"
                          disabled={addingCode === it.code}
                          onClick={() => onAdd(it)}
                        >
                          {addingCode === it.code ? "加入中…" : "自选"}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </section>
        </>
      ) : null}
    </>
  );
}
