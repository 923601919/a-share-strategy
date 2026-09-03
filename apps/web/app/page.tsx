"use client";

import { useEffect, useRef, useState } from "react";
import {
  addWatch,
  explainScanError,
  scanWithProgress,
  type ScanItem,
  type ScanJob,
  type ScanResult,
} from "@/lib/api";

/** 页面 Tab：后两个为测试策略，不改现网进攻型/龙头低吸 */
type UiTab = "fenshi" | "leader_dip" | "fenshi_quota" | "fenshi_soft";
type ScanMode = "fenshi" | "leader_dip";
type UniversePolicy = "hot_only" | "quota" | "soft";

type ModeFormState = {
  minPct: number;
  maxPct: number;
};

const TAB_DEFAULTS: Record<UiTab, ModeFormState> = {
  fenshi: { minPct: 2, maxPct: 6 },
  leader_dip: { minPct: -3, maxPct: 2 },
  fenshi_quota: { minPct: 2, maxPct: 6 },
  fenshi_soft: { minPct: 2, maxPct: 6 },
};

const TAB_META: Record<
  UiTab,
  { title: string; desc: string; mode: ScanMode; policy: UniversePolicy; test?: boolean }
> = {
  fenshi: {
    title: "进攻型分时扫描",
    desc: "强势板块成分内选股 + 回踩均价放量再攻 / 强势推升。默认涨幅 2% ~ <6%。",
    mode: "fenshi",
    policy: "hot_only",
  },
  leader_dip: {
    title: "龙头低吸扫描",
    desc: "强势板块龙头，水下/平盘贴近 MA5 低吸。默认涨幅 -3% ~ +2%。",
    mode: "leader_dip",
    policy: "hot_only",
  },
  fenshi_quota: {
    title: "测试·配额制",
    desc: "主池仍用强势板块；结果约 25% 名额留给形态过关的非主线票（测试，不影响现网）。",
    mode: "fenshi",
    policy: "quota",
    test: true,
  },
  fenshi_soft: {
    title: "测试·软加权",
    desc: "全市场涨幅/成交额初筛，热门板块只加分不加硬过滤（测试，不影响现网）。",
    mode: "fenshi",
    policy: "soft",
    test: true,
  },
};

const STORAGE_KEY = "ashare.scan.v2";

function createTabState(): Record<UiTab, ModeFormState> {
  return {
    fenshi: { ...TAB_DEFAULTS.fenshi },
    leader_dip: { ...TAB_DEFAULTS.leader_dip },
    fenshi_quota: { ...TAB_DEFAULTS.fenshi_quota },
    fenshi_soft: { ...TAB_DEFAULTS.fenshi_soft },
  };
}

function emptyData(): Record<UiTab, ScanResult | null> {
  return { fenshi: null, leader_dip: null, fenshi_quota: null, fenshi_soft: null };
}

function loadPersisted(): {
  tab?: UiTab;
  formByTab?: Record<UiTab, ModeFormState>;
  dataByTab?: Record<UiTab, ScanResult | null>;
  minAmount?: number;
  session?: string;
  topN?: number;
} | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export default function HomePage() {
  const [tab, setTab] = useState<UiTab>("fenshi");
  const [formByTab, setFormByTab] = useState(createTabState);
  const [dataByTab, setDataByTab] = useState(emptyData);
  const [minAmount, setMinAmount] = useState(1);
  const [session, setSession] = useState("auto");
  const [topN, setTopN] = useState(20);
  const [loading, setLoading] = useState(false);
  const [progress, setProgress] = useState<ScanJob | null>(null);
  const [error, setError] = useState("");
  const [errorKind, setErrorKind] = useState<"session" | "data" | "network" | "">("");
  const [msg, setMsg] = useState("");
  const [addingCode, setAddingCode] = useState("");
  const stopRef = useRef(false);
  const [storageReady, setStorageReady] = useState(false);

  const meta = TAB_META[tab];
  const { minPct, maxPct } = formByTab[tab];
  const data = dataByTab[tab];

  useEffect(() => {
    const persisted = loadPersisted();
    if (persisted) {
      if (persisted.tab && persisted.tab in TAB_META) setTab(persisted.tab);
      if (persisted.formByTab) setFormByTab({ ...createTabState(), ...persisted.formByTab });
      if (persisted.dataByTab) setDataByTab({ ...emptyData(), ...persisted.dataByTab });
      if (persisted.minAmount != null) setMinAmount(persisted.minAmount);
      if (persisted.session) setSession(persisted.session);
      if (persisted.topN != null) setTopN(persisted.topN);
    }
    setStorageReady(true);
  }, []);

  useEffect(() => {
    if (!storageReady) return;
    try {
      sessionStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ tab, formByTab, dataByTab, minAmount, session, topN })
      );
    } catch {
      /* ignore quota */
    }
  }, [tab, formByTab, dataByTab, minAmount, session, topN, storageReady]);

  function onTabChange(next: UiTab) {
    setTab(next);
    setError("");
    setErrorKind("");
    setMsg("");
  }

  function updateForm(patch: Partial<ModeFormState>) {
    setFormByTab((prev) => ({
      ...prev,
      [tab]: { ...prev[tab], ...patch },
    }));
  }

  async function onScan() {
    stopRef.current = false;
    setLoading(true);
    setError("");
    setErrorKind("");
    setMsg("");
    setProgress({
      job_id: "",
      kind: "scan",
      status: "queued",
      stage: "queued",
      progress: 0,
      message: "排队中…",
    });
    try {
      const res = await scanWithProgress(
        {
          min_amount_yi: minAmount,
          min_pct: minPct,
          max_pct: maxPct,
          session,
          top_n: topN,
          mode: meta.mode,
          universe_policy: meta.policy,
        },
        {
          shouldStop: () => stopRef.current,
          onProgress: (job) => setProgress(job),
        }
      );
      setDataByTab((prev) => ({ ...prev, [tab]: res }));
      if (res.error_code) {
        setErrorKind(res.error_code === "session_blocked" ? "session" : "data");
        setError(explainScanError(res.error_code, res.session_note));
      } else if (res.count === 0) {
        setErrorKind("data");
        setError(res.session_note || "本次无命中标的");
      }
    } catch (e: any) {
      const text = e?.message || String(e);
      setError(text);
      if (text.includes("时段") || text.includes("session")) setErrorKind("session");
      else if (text.includes("取消")) setErrorKind("");
      else setErrorKind("network");
    } finally {
      setLoading(false);
      setProgress(null);
    }
  }

  function onCancel() {
    stopRef.current = true;
    setMsg("正在取消扫描…");
  }

  async function onAdd(item: ScanItem) {
    setError("");
    setErrorKind("");
    setMsg("");
    setAddingCode(item.code);
    const proxy = Boolean((item.fenshi as { proxy?: boolean })?.proxy);
    const f = item.fenshi as { day_position?: number | null; vwap_deviation?: number | null };
    try {
      const res = await addWatch({
        code: item.code,
        name: item.name,
        source: meta.mode === "leader_dip" ? "longtou" : "fenshi",
        note: (item.reasons || []).slice(0, 2).join("；"),
        entry_price: item.price,
        entry_pct: item.pct,
        entry_score: item.score,
        minute_confirmed: !proxy,
        day_position: f?.day_position ?? null,
        vwap_deviation: f?.vwap_deviation ?? null,
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
      setErrorKind("network");
      setError(`加入自选失败：${e?.message || String(e)}`);
    } finally {
      setAddingCode("");
    }
  }

  const maxPctLabel = meta.mode === "leader_dip" ? "涨幅上限≤%" : "当前涨幅<%";
  const pct = Math.round((progress?.progress || 0) * 100);
  const tabs: UiTab[] = ["fenshi", "leader_dip", "fenshi_quota", "fenshi_soft"];

  return (
    <>
      <section className="panel">
        <h1 style={{ margin: "0 0 8px", fontSize: 20 }}>{meta.title}</h1>
        <p className="muted" style={{ marginTop: 0 }}>
          {meta.desc}
        </p>
        <div className="row" style={{ marginBottom: 12, flexWrap: "wrap" }}>
          {tabs.map((id) => (
            <button
              key={id}
              className={tab === id ? "" : "secondary"}
              onClick={() => onTabChange(id)}
            >
              {id === "fenshi"
                ? "进攻型分时"
                : id === "leader_dip"
                  ? "龙头低吸"
                  : id === "fenshi_quota"
                    ? "测试·配额"
                    : "测试·软加权"}
            </button>
          ))}
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
          {loading ? (
            <button className="secondary" onClick={onCancel}>
              取消
            </button>
          ) : null}
        </div>
        {loading && progress ? (
          <div style={{ marginTop: 12 }}>
            <div className="muted" style={{ marginBottom: 6 }}>
              {progress.message || progress.stage} · {pct}%
            </div>
            <div className="bar">
              <i style={{ width: `${Math.min(Math.max(pct, 2), 100)}%` }} />
            </div>
          </div>
        ) : null}
        {msg ? <p className="good">{msg}</p> : null}
        {error ? (
          <p className="err">
            {errorKind === "session" ? "时段限制：" : null}
            {errorKind === "data" ? "数据提示：" : null}
            {errorKind === "network" ? "请求异常：" : null}
            {error}
          </p>
        ) : null}
      </section>

      {data ? (
        <>
          <section className="panel">
            <div className="muted">{data.session_note}</div>
            {data.market_env && data.market_env.pct != null ? (
              <div
                className="row"
                style={{
                  marginTop: 8,
                  padding: "8px 12px",
                  borderRadius: 8,
                  background:
                    data.market_env.level === "warn"
                      ? "rgba(230,184,77,0.12)"
                      : data.market_env.level === "block"
                        ? "rgba(232,93,93,0.12)"
                        : "rgba(139,155,176,0.08)",
                  border: "1px solid var(--line)",
                }}
              >
                <strong>大盘环境</strong>
                <span>
                  {data.market_env.ref_name || data.market_env.ref_index}{" "}
                  <span
                    className={
                      data.market_env.pct > 0 ? "up" : data.market_env.pct < 0 ? "down" : ""
                    }
                  >
                    {data.market_env.pct > 0 ? "+" : ""}
                    {data.market_env.pct}%
                  </span>
                </span>
                {data.market_env.level === "warn" ? (
                  <span className="muted">大盘偏弱，进攻型分数已折减</span>
                ) : null}
              </div>
            ) : null}
            <div className="muted" style={{ marginTop: 6 }}>
              数据源 spot={data.data_source?.spot ?? "?"} · 候选池{" "}
              {data.data_source?.universe_size ?? "?"} · 分时确认{" "}
              {data.data_source?.fenshi_ok ?? 0}/{data.data_source?.candidates ?? "?"} · 回踩再攻{" "}
              {data.data_source?.reattack_ok ?? 0} · 强势推升{" "}
              {data.data_source?.strong_push_ok ?? 0} · 命中 {data.count} 只
              {data.timings?.total_ms != null ? ` · 耗时 ${Math.round(data.timings.total_ms)}ms` : ""}
              {data.params?.strategy_version
                ? ` · 策略 ${String(data.params.strategy_version)}`
                : ""}
              {data.params?.universe_policy
                ? ` · 池策略 ${String(data.params.universe_policy)}`
                : ""}
            </div>
            <h3 style={{ marginBottom: 8 }}>强势板块（候选池来源）</h3>
            <div className="chips">
              {(data.universe_sectors || data.hot_boards || []).slice(0, 12).map((b) => {
                const type = "type" in b ? (b as { type?: string }).type : undefined;
                return (
                  <div className="chip" key={`${b.name}-${type ?? ""}`}>
                    {b.name} {b.pct?.toFixed?.(2) ?? b.pct}%
                    {type ? ` (${type})` : ""}
                  </div>
                );
              })}
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
            <table>
              <thead>
                <tr>
                  <th>代码</th>
                  <th>名称</th>
                  <th>涨幅</th>
                  <th>得分</th>
                  <th>日内位置</th>
                  <th>量比</th>
                  <th>异动</th>
                  <th>原因</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((it) => {
                  const proxy = Boolean((it.fenshi as { proxy?: boolean })?.proxy);
                  const fenshi = (it.fenshi ?? {}) as {
                    proxy?: boolean;
                    day_position?: number | null;
                    vwap_deviation?: number | null;
                    chase_penalty?: number | null;
                  };
                  const pos = fenshi.day_position;
                  const chase = Boolean((fenshi.chase_penalty ?? 0) > 0);
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
                        ) : meta.test ? (
                          <div className="muted">非主线</div>
                        ) : null}
                        {proxy ? <div className="muted">代理分</div> : null}
                      </td>
                      <td className={it.pct > 0 ? "up" : it.pct < 0 ? "down" : ""}>
                        {it.pct}%
                      </td>
                      <td>{it.score}</td>
                      <td className={chase ? "down" : ""}>
                        {pos != null ? (
                          <>
                            {(pos * 100).toFixed(0)}%
                            {chase && fenshi.chase_penalty ? (
                              <div className="muted">追高-{fenshi.chase_penalty}</div>
                            ) : null}
                          </>
                        ) : (
                          "-"
                        )}
                      </td>
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
