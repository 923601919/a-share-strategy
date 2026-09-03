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
  { title: string; desc: string; mode: ScanMode; policy: UniversePolicy; test?: boolean; tabLabel: string }
> = {
  fenshi: {
    title: "进攻型分时扫描",
    desc: "强势板块成分内选股 + 回踩均价放量再攻 / 强势推升。默认涨幅 2% ~ <6%。",
    mode: "fenshi",
    policy: "hot_only",
    tabLabel: "进攻型分时",
  },
  leader_dip: {
    title: "龙头低吸扫描",
    desc: "强势板块龙头，水下/平盘贴近 MA5 低吸。默认涨幅 -3% ~ +2%。",
    mode: "leader_dip",
    policy: "hot_only",
    tabLabel: "龙头低吸",
  },
  fenshi_quota: {
    title: "测试·配额制",
    desc: "主池仍用强势板块；结果约 25% 名额留给形态过关的非主线票（测试，不影响现网）。",
    mode: "fenshi",
    policy: "quota",
    test: true,
    tabLabel: "测试·配额",
  },
  fenshi_soft: {
    title: "测试·软加权",
    desc: "全市场涨幅/成交额初筛，热门板块只加分不加硬过滤（测试，不影响现网）。",
    mode: "fenshi",
    policy: "soft",
    test: true,
    tabLabel: "测试·软加权",
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
      {/* ==================== 1. 扫描配置卡 ==================== */}
      <section className="panel">
        <div className="panel-head">
          <div>
            <h1 className="panel-title">
              {meta.title}
              {meta.test ? <span className="pill" style={{ background: "#fff7ed", color: "#c2410c", borderColor: "rgba(234,88,12,0.3)" }}>测试</span> : null}
            </h1>
            <p className="panel-sub">{meta.desc}</p>
          </div>
        </div>

        <div className="section-h">
          <span>选择策略</span>
        </div>
        <div className="tabs mb-16" role="tablist">
          {tabs.map((id) => (
            <button
              key={id}
              role="tab"
              className={tab === id ? "active" : ""}
              onClick={() => onTabChange(id)}
            >
              {TAB_META[id].tabLabel}
            </button>
          ))}
        </div>

        <div className="section-h">
          <span>扫描参数</span>
        </div>
        <div className="grid-4">
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
            涨幅下限 %
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
        </div>
        <div className="row" style={{ marginTop: 16, alignItems: "center" }}>
          <label style={{ flex: "0 0 200px" }}>
            返回条数
            <input
              type="number"
              value={topN}
              onChange={(e) => setTopN(Number(e.target.value))}
            />
          </label>
          <button onClick={onScan} disabled={loading} style={{ minWidth: 120 }}>
            {loading ? "扫描中…" : "立即扫描"}
          </button>
          {loading ? (
            <button className="secondary" onClick={onCancel}>
              取消
            </button>
          ) : null}
          {loading && progress ? (
            <div style={{ flex: 1, minWidth: 200 }}>
              <div className="muted-sm" style={{ marginBottom: 4 }}>
                {progress.message || progress.stage} · {pct}%
              </div>
              <div className="progress">
                <i style={{ width: `${Math.min(Math.max(pct, 2), 100)}%` }} />
              </div>
            </div>
          ) : null}
        </div>

        {msg ? <div className="banner ok mt-12">{msg}</div> : null}
        {error ? (
          <div
            className={
              errorKind === "session" ? "banner warn mt-12" :
              errorKind === "data" ? "banner info mt-12" :
              errorKind === "network" ? "banner err mt-12" : "banner info mt-12"
            }
          >
            <span className="banner-icon">!</span>
            <span>
              {errorKind === "session" ? "时段限制：" :
                errorKind === "data" ? "数据提示：" :
                errorKind === "network" ? "请求异常：" : ""}
              {error}
            </span>
          </div>
        ) : null}
      </section>

      {/* ==================== 2. 结果概览 ==================== */}
      {data ? (
        <section className="panel">
          <div className="panel-head">
            <div>
              <h2 className="panel-title">本次扫描概览</h2>
              <p className="panel-sub">{data.session_note || "—"}</p>
            </div>
            <div className="muted-sm">
              策略 {String(data.params?.strategy_version || "")} · 池策略 {String(data.params?.universe_policy || "")}
            </div>
          </div>

          {/* 大盘环境横幅 */}
          {data.market_env && data.market_env.pct != null ? (
            <div
              className={
                data.market_env.level === "warn" ? "banner warn" :
                data.market_env.level === "block" ? "banner err" :
                "banner info"
              }
            >
              <span className="banner-icon">
                {data.market_env.level === "block" ? "×" :
                  data.market_env.level === "warn" ? "!" : "·"}
              </span>
              <span style={{ flex: 1 }}>
                <strong>大盘环境 · {data.market_env.ref_name || data.market_env.ref_index}</strong>
                &nbsp;
                <span className={`num ${data.market_env.pct > 0 ? "up" : data.market_env.pct < 0 ? "down" : ""}`}>
                  {data.market_env.pct > 0 ? "+" : ""}
                  {Number(data.market_env.pct).toFixed(2)}%
                </span>
                {data.market_env.level === "warn" ? (
                  <span className="muted-sm" style={{ marginLeft: 8 }}>
                    · 大盘偏弱，进攻型分数已折减
                  </span>
                ) : null}
                {data.market_env.level === "block" ? (
                  <span className="muted-sm" style={{ marginLeft: 8 }}>
                    · 大盘急跌，建议观望
                  </span>
                ) : null}
              </span>
            </div>
          ) : null}

          {/* KPI 数字 */}
          <div className="grid-4 mt-16">
            <div className="kpi">
              <span className="kpi-label">命中</span>
              <span className={`kpi-value ${data.count > 0 ? "" : "muted"}`}>
                {data.count ?? 0}<span className="kpi-sub" style={{ marginLeft: 4 }}>只</span>
              </span>
              <span className="kpi-sub">候选池 {data.data_source?.universe_size ?? "—"}</span>
            </div>
            <div className="kpi">
              <span className="kpi-label">分时确认</span>
              <span className="kpi-value">
                {data.data_source?.fenshi_ok ?? 0}<span className="kpi-sub" style={{ marginLeft: 2 }}>/</span>
                <span className="kpi-sub">{data.data_source?.candidates ?? "—"}</span>
              </span>
              <span className="kpi-sub">
                代理 {data.items.filter((it) => Boolean((it.fenshi as { proxy?: boolean } | undefined)?.proxy)).length}
              </span>
            </div>
            <div className="kpi">
              <span className="kpi-label">回踩再攻 / 强势推升</span>
              <span className="kpi-value">
                {data.data_source?.reattack_ok ?? 0}
                <span className="kpi-sub"> / {data.data_source?.strong_push_ok ?? 0}</span>
              </span>
              <span className="kpi-sub">命中形态</span>
            </div>
            <div className="kpi">
              <span className="kpi-label">耗时</span>
              <span className="kpi-value num">
                {data.timings?.total_ms != null ? `${(data.timings.total_ms / 1000).toFixed(1)}` : "—"}
                <span className="kpi-sub" style={{ marginLeft: 2 }}>s</span>
              </span>
              <span className="kpi-sub truncate-1">数据源 {data.data_source?.spot ?? "—"}</span>
            </div>
          </div>

          {/* 板块 chips */}
          <div className="section-h" style={{ marginTop: 20 }}>
            <span>强势板块（候选池来源）</span>
            <span className="muted-sm">前 12</span>
          </div>
          <div className="chips">
            {(data.universe_sectors || data.hot_boards || []).slice(0, 12).map((b) => {
              const type = "type" in b ? (b as { type?: string }).type : undefined;
              const life = "life_note" in b ? (b as { life_note?: string; consecutive?: number }).life_note : undefined;
              const n = "consecutive" in b ? (b as { consecutive?: number }).consecutive : undefined;
              const pctVal = Number(b.pct ?? 0);
              return (
                <div className="chip" key={`${b.name}-${type ?? ""}`}>
                  <strong>{b.name}</strong>
                  <span className={`pct ${pctVal > 0 ? "up" : pctVal < 0 ? "down" : ""}`}>
                    {pctVal > 0 ? "+" : ""}
                    {pctVal.toFixed(2)}%
                  </span>
                  {type ? <span className="muted-sm">· {type}</span> : null}
                  {n && n > 1 ? <span className="muted-sm">· {n}日</span> : null}
                  {life ? <span className="muted-sm">· {life}</span> : null}
                </div>
              );
            })}
          </div>

          <div className="section-h">
            <span>同花顺行业参考</span>
            <span className="muted-sm">前 8</span>
          </div>
          <div className="chips">
            {(data.hot_boards || []).slice(0, 8).map((b) => {
              const pctVal = Number(b.pct ?? 0);
              return (
                <div className="chip" key={`ref-${b.name}`}>
                  <strong>{b.name}</strong>
                  <span className={`pct ${pctVal > 0 ? "up" : pctVal < 0 ? "down" : ""}`}>
                    {pctVal > 0 ? "+" : ""}
                    {pctVal.toFixed(2)}%
                  </span>
                </div>
              );
            })}
          </div>
        </section>
      ) : null}

      {/* ==================== 3. 命中结果表 ==================== */}
      {data ? (
        <section className="panel">
          <div className="panel-head">
            <h2 className="panel-title">
              命中结果
              <span className="muted-sm" style={{ marginLeft: 8, fontWeight: 400 }}>
                {data.count} 只
              </span>
            </h2>
            <div className="muted-sm truncate-1">
              数据源 {data.data_source?.spot ?? "—"}
            </div>
          </div>

          {msg ? <div className="banner ok mb-12">{msg}</div> : null}

          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>代码</th>
                  <th>名称</th>
                  <th style={{ textAlign: "right" }}>涨幅</th>
                  <th style={{ textAlign: "right" }}>得分</th>
                  <th style={{ textAlign: "right" }}>日内位置</th>
                  <th style={{ textAlign: "right" }}>量比</th>
                  <th>异动 / 监管</th>
                  <th>原因</th>
                  <th style={{ textAlign: "right" }}></th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((it, idx) => {
                  const proxy = Boolean((it.fenshi as { proxy?: boolean })?.proxy);
                  const fenshi = (it.fenshi ?? {}) as {
                    proxy?: boolean;
                    day_position?: number | null;
                    vwap_deviation?: number | null;
                    chase_penalty?: number | null;
                  };
                  const pos = fenshi.day_position;
                  const chase = Boolean((fenshi.chase_penalty ?? 0) > 0);
                  const isTop = idx === 0;
                  const rowCls = chase ? "row-chase" : isTop ? "row-top" : "";
                  const pctNum = Number(it.pct ?? 0);
                  return (
                    <tr key={it.code} className={rowCls}>
                      <td className="num">
                        <a
                          href={`https://quote.eastmoney.com/${it.code.startsWith("6") ? "sh" : "sz"}${it.code}.html`}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {it.code}
                        </a>
                      </td>
                      <td>
                        <div className="cell-strong">
                          {it.name}
                          {it.in_hot_board ? (
                            <span className="pill hot" style={{ marginLeft: 6 }}>热门板块</span>
                          ) : meta.test ? (
                            <span className="pill" style={{ marginLeft: 6 }}>非主线</span>
                          ) : null}
                        </div>
                        {(it.selection?.tags || []).length > 0 ? (
                          <div className="cell-sub">{(it.selection?.tags || []).join(" · ")}</div>
                        ) : null}
                        {proxy ? <div className="cell-sub">代理分</div> : null}
                      </td>
                      <td className={`num ${pctNum > 0 ? "up" : pctNum < 0 ? "down" : ""}`} style={{ textAlign: "right" }}>
                        {pctNum > 0 ? "+" : ""}
                        {pctNum.toFixed(2)}%
                      </td>
                      <td className="num cell-strong" style={{ textAlign: "right" }}>
                        {it.score}
                      </td>
                      <td style={{ textAlign: "right" }}>
                        {pos != null ? (
                          <div>
                            <span className={chase ? "down num cell-strong" : "num cell-strong"}>
                              {(Number(pos) * 100).toFixed(0)}%
                            </span>
                            {chase && fenshi.chase_penalty ? (
                              <div>
                                <span className="pill chase" style={{ fontSize: 11 }}>
                                  追高 -{fenshi.chase_penalty}
                                </span>
                              </div>
                            ) : null}
                          </div>
                        ) : (
                          <span className="muted">-</span>
                        )}
                      </td>
                      <td className="num muted" style={{ textAlign: "right" }}>
                        {it.volume_ratio ?? "-"}
                      </td>
                      <td>
                        <span className={`pill ${it.risk?.level || "ok"}`}>
                          {it.risk?.anomaly_pct ?? "-"}%
                        </span>
                        <div className="bar" style={{ marginTop: 4, width: 80 }}>
                          <i
                            style={{
                              width: `${Math.min(Number(it.risk?.anomaly_progress || 0), 100)}%`,
                            }}
                          />
                        </div>
                        {it.risk?.days_to_regulatory_exit != null &&
                        it.risk.days_to_regulatory_exit <= 5 ? (
                          <div className="cell-sub">
                            出监管约{it.risk.days_to_regulatory_exit}日
                            {it.risk.regulatory_window_end
                              ? ` (${it.risk.regulatory_window_end})`
                              : ""}
                          </div>
                        ) : null}
                      </td>
                      <td className="muted" style={{ maxWidth: 360 }}>
                        <div className="truncate-2">{(it.reasons || []).join("；")}</div>
                      </td>
                      <td style={{ textAlign: "right" }}>
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
          </div>
        </section>
      ) : null}
    </>
  );
}
