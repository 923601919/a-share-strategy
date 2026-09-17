"use client";

import { useEffect, useState } from "react";
import {
  directionLabel,
  forecastRunStatusLabel,
  getForecastAccuracy,
  getForecastHistory,
  getForecastLatest,
  getForecastRuns,
  outcomeSourceLabel,
  type ForecastAccuracy,
  type ForecastHistory,
  type ForecastLatest,
  type ForecastRuns,
} from "@/lib/api";

// ============================================================ 大盘预判页
//
// ★ 这一页的展示原则（比样式重要）★
//
// 1. **不出现"孤零零的百分比"。** 准确率永远与有效样本数、置信区间、
//    显著性门槛、"永远猜涨"基线一起出现。只显示 58% 是这一页最容易骗人的做法：
//    A 股指数长期有上行倾向，一个只会猜涨的模型稳定就能拿到 52%~55%。
//
// 2. **「没拿到」与「没有」必须能分辨。** 目标日行情缺失的样本显示为「待结算」，
//    不显示成未命中 —— 前者是数据问题，后者是预测错了，两件事完全不同。
//
// 3. **实时口径要标明。** 收盘后用实时行情先结掉的结果会被次日官方日线修正，
//    页面上必须让用户看得出这条依据是哪一个（`outcomeSourceLabel`）。
//
// 4. **页面上不提供任何"手动补一条"的入口。** 预测只能由服务端定时作业产生。
//    能手动补，就等于允许事后挑一个能摇出好卦的日子，准确率随即失去意义。

function dirClass(d: string | null | undefined): string {
  if (d === "up") return "up";
  if (d === "down") return "down";
  return "muted";
}

function pct(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(Number(v))) return <span className="muted">-</span>;
  const n = Number(v) * 100;
  return <span>{n > 0 ? "+" : ""}{n.toFixed(digits)}%</span>;
}

function ratio(v: number | null | undefined, digits = 1) {
  if (v == null || Number.isNaN(Number(v))) return "-";
  return `${(Number(v) * 100).toFixed(digits)}%`;
}

function StrengthBar({ v }: { v: number }) {
  const w = Math.max(0, Math.min(1, Number(v) || 0)) * 100;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <span className="bar" style={{ width: 60, display: "inline-block" }}>
        <span style={{ width: `${w}%`, display: "block", height: 6, background: "var(--primary)" }} />
      </span>
      <span className="num">{Number(v).toFixed(2)}</span>
    </span>
  );
}

function UpcomingCard({ item, label }: { item: ForecastLatest["upcoming"]; label: string }) {
  if (!item) {
    return (
      <p className="muted" style={{ margin: 0 }}>
        暂无生效中的预测。作业在每个交易日 15:45 产出次日预测；若刚部署，等到下一个交易日收盘后即可看到。
      </p>
    );
  }
  return (
    <div className="grid-4">
      <div className="kpi">
        <div className="kpi-label">目标交易日</div>
        <div className="kpi-value num">{item.target_date}</div>
        <div className="kpi-sub">起卦于 {item.as_of ? String(item.as_of).slice(0, 10) : "-"} 收盘</div>
      </div>
      <div className="kpi">
        <div className="kpi-label">判为</div>
        <div className={`kpi-value ${dirClass(item.direction)}`}>{directionLabel(item.direction)}</div>
        <div className="kpi-sub">{label}</div>
      </div>
      <div className="kpi">
        <div className="kpi-label">强度</div>
        <div className="kpi-value">
          <StrengthBar v={item.strength} />
        </div>
        <div className="kpi-sub">0 = 中性（得分 0.5），1 = 最偏</div>
      </div>
      <div className="kpi">
        <div className="kpi-label">卦象</div>
        <div className="kpi-value">
          {item.hexagram || "-"}
          {item.changed_hexagram ? <span className="muted"> → {item.changed_hexagram}</span> : null}
        </div>
        <div className="kpi-sub">
          引擎 {item.engine_ver} / 规则 {item.ruleset_ver}
        </div>
      </div>
    </div>
  );
}

function OutcomeCell({ item }: { item: ForecastHistory["items"][number] }) {
  if (!item.outcome) {
    return (
      <span className="pill warn" title="目标日行情未发布或缺失。既不算命中也不算未命中，下一次作业会补结算。">
        待结算
      </span>
    );
  }
  const o = item.outcome;
  if (o.hit === null) {
    return (
      <span className="pill" title="任一方为平盘 → 不计入二元统计（对称口径）">
        平盘不计
      </span>
    );
  }
  return <span className={o.hit ? "up" : "down"}>{o.hit ? "命中" : "未中"}</span>;
}

export default function MarketForecastPage() {
  const [latest, setLatest] = useState<ForecastLatest | null>(null);
  const [history, setHistory] = useState<ForecastHistory | null>(null);
  const [accuracy, setAccuracy] = useState<ForecastAccuracy | null>(null);
  const [runs, setRuns] = useState<ForecastRuns | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [windowDays, setWindowDays] = useState(0);

  async function load(days?: number) {
    setLoading(true);
    setError("");
    const d = days ?? windowDays;
    try {
      const [l, h, a, r] = await Promise.all([
        getForecastLatest(),
        getForecastHistory(60),
        getForecastAccuracy(d),
        getForecastRuns(30, 60),
      ]);
      setLatest(l);
      setHistory(h);
      setAccuracy(a);
      setRuns(r);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const acc = accuracy;
  const perm = acc?.permutation || null;

  return (
    <>
      <section className="panel">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <h1 style={{ margin: "0 0 6px", fontSize: 20 }}>大盘预判（六爻）</h1>
            <p className="muted" style={{ margin: 0 }}>
              每个交易日 15:45 由服务端定时作业起卦，预判上证指数**下一个交易日**的涨跌方向。
              起卦完全确定：由「标的 + 目标日 + 盐值」推出，重跑不会得到不同的卦。
              <strong> 仅供娱乐，不构成投资建议。</strong>
            </p>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button className="secondary" onClick={() => load()} disabled={loading}>
              {loading ? "加载中…" : "刷新"}
            </button>
          </div>
        </div>
        {error ? <p className="err">{error}</p> : null}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">当前生效的预测</h2>
          {latest?.recent_settled ? (
            <span className="panel-sub">
              最近一次已结算：{latest.recent_settled.target_date}{" "}
              <span className={dirClass(latest.recent_settled.direction)}>
                {directionLabel(latest.recent_settled.direction)}
              </span>
              {" → 实际 "}
              <span className={dirClass(latest.recent_settled.outcome?.actual_direction)}>
                {directionLabel(latest.recent_settled.outcome?.actual_direction)}
              </span>
              {latest.recent_settled.outcome ? (
                <>
                  {"（"}
                  {pct(latest.recent_settled.outcome.actual_change)}
                  {"）"}
                </>
              ) : null}
            </span>
          ) : (
            <span className="panel-sub">尚无已结算记录</span>
          )}
        </div>
        <UpcomingCard item={latest?.upcoming || null} label={latest?.symbol_label || "上证指数"} />
        {latest ? <p className="muted-sm">服务器时间 {String(latest.server_time).replace("T", " ").slice(0, 19)}</p> : null}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">准确率验证</h2>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <select
              value={windowDays}
              onChange={(e) => {
                const v = Number(e.target.value);
                setWindowDays(v);
                load(v);
              }}
            >
              <option value={0}>全部历史</option>
              <option value={30}>最近 30 天</option>
              <option value={90}>最近 90 天</option>
              <option value={180}>最近 180 天</option>
            </select>
          </div>
        </div>

        {acc ? (
          <>
            <div className="chips">
              <div className="chip">
                有效样本 <span className="num">{acc.n}</span> 个交易日
              </div>
              <div className="chip">
                命中 <span className="num">{acc.hits}</span>
              </div>
              <div className="chip">
                准确率 <span className="num">{ratio(acc.rate)}</span>
              </div>
              <div className="chip">
                95% 置信区间 <span className="num">{ratio(acc.ci.low)} ~ {ratio(acc.ci.high)}</span>
              </div>
              <div className="chip">
                当前门槛 <span className="num">{ratio(acc.threshold)}</span>
              </div>
              <div className="chip">
                永远猜涨基线 <span className="num">{ratio(acc.baselines.always_up)}</span>
              </div>
              <div className="chip">
                平盘剔除 <span className="num">{acc.flat_count}</span>
              </div>
              <div className="chip">
                待结算 <span className="num">{acc.pending_count}</span>
              </div>
            </div>

            {!acc.sample_sufficient ? (
              <div className="banner" style={{ marginTop: 12 }}>
                <span className="banner-icon">!</span>
                <span>样本不足，当前数字没有统计意义。{acc.note}</span>
              </div>
            ) : (
              <p className={acc.significant ? "up" : "muted"} style={{ marginTop: 12 }}>
                {acc.note}
              </p>
            )}

            <div className="grid-2" style={{ marginTop: 14 }}>
              <div>
                <h4 style={{ margin: "0 0 6px" }}>相对「随机方向」的检验</h4>
                <table>
                  <tbody>
                    <tr>
                      <td className="muted">单尾二项检验 p 值</td>
                      <td className="num">{acc.p_value.toFixed(4)}</td>
                    </tr>
                    <tr>
                      <td className="muted">是否显著（p &lt; 0.05 且 ≥ 门槛）</td>
                      <td>{acc.significant ? <span className="up">是</span> : <span className="muted">否</span>}</td>
                    </tr>
                    <tr>
                      <td className="muted">「永远猜跌」基线</td>
                      <td className="num">{ratio(acc.baselines.always_down)}</td>
                    </tr>
                  </tbody>
                </table>
                <p className="muted-sm" style={{ marginTop: 8 }}>
                  这里的零假设是「相对随机 50%」。指数长期有上行倾向，所以这个检验通过
                  <strong>不能</strong>说明有预测能力 —— 要看下面那个。
                </p>
              </div>

              <div>
                <h4 style={{ margin: "0 0 6px" }}>偏置匹配置换检验</h4>
                {perm ? (
                  <>
                    <table>
                      <tbody>
                        <tr>
                          <td className="muted">观测命中率</td>
                          <td className="num">{ratio(perm.observed_rate)}</td>
                        </tr>
                        <tr>
                          <td className="muted">零分布均值（保持涨跌偏好随机重排）</td>
                          <td className="num">{ratio(perm.null_mean)}</td>
                        </tr>
                        <tr>
                          <td className="muted">超出零分布</td>
                          <td className="num">{(perm.edge * 100).toFixed(1)} pp</td>
                        </tr>
                        <tr>
                          <td className="muted">p 值（{perm.iterations} 次重排）</td>
                          <td className="num">
                            {perm.p_value.toFixed(4)}{" "}
                            {perm.p_value < 0.05 ? <span className="up">显著</span> : <span className="muted">不显著</span>}
                          </td>
                        </tr>
                      </tbody>
                    </table>
                    <p className="muted-sm" style={{ marginTop: 8 }}>
                      这个检验的零假设是「预测与结果相互独立，但模型保持自己的涨跌偏好」。
                      只有观测命中率明显超出零分布，才能说明预测本身带了信息。
                    </p>
                  </>
                ) : (
                  <p className="muted-sm" style={{ margin: 0 }}>
                    有效样本不足 20 条，暂不做置换检验。样本越多，这个检验越有意义。
                  </p>
                )}
              </div>
            </div>
            <p className="footer-note">{acc.disclaimer}</p>
          </>
        ) : (
          <p className="muted">{loading ? "加载中…" : "暂无数据"}</p>
        )}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">历史预测</h2>
          <span className="panel-sub">「待结算」= 目标日行情未发布或缺失，不是未命中</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>目标日</th>
                <th>起卦时点</th>
                <th>预测</th>
                <th>强度</th>
                <th>卦象</th>
                <th>实际</th>
                <th>涨跌</th>
                <th>结果</th>
                <th>结算依据</th>
              </tr>
            </thead>
            <tbody>
              {(history?.items || []).length === 0 ? (
                <tr>
                  <td colSpan={9} className="muted">
                    暂无记录。作业在每个交易日 15:45 产出次日预测。
                  </td>
                </tr>
              ) : (
                (history?.items || []).map((it) => (
                  <tr key={it.id}>
                    <td className="num">{it.target_date}</td>
                    {/* 起卦时点列是「这条预测是不是事前产出的」的直接证据：它必须早于目标日 */}
                    <td className="muted-sm num">{String(it.as_of).slice(0, 10)}</td>
                    <td className={dirClass(it.direction)}>{directionLabel(it.direction)}</td>
                    <td>{Number(it.strength).toFixed(2)}</td>
                    <td>
                      {it.hexagram || "-"}
                      {it.changed_hexagram ? (
                        <span className="muted"> → {it.changed_hexagram}</span>
                      ) : null}
                    </td>
                    <td className={dirClass(it.outcome?.actual_direction)}>
                      {it.outcome ? directionLabel(it.outcome.actual_direction) : "-"}
                    </td>
                    <td>{it.outcome ? pct(it.outcome.actual_change) : "-"}</td>
                    <td>
                      <OutcomeCell item={it} />
                    </td>
                    <td className="muted-sm">
                      {it.outcome ? outcomeSourceLabel(it.outcome.source) : "-"}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
        <p className="footer-note">{history?.disclaimer}</p>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">作业运行记录</h2>
          <span className="panel-sub">
            漏跑的交易日会如实列在缺口里 —— 缺口不会被伪造补跑
          </span>
        </div>
        {runs && runs.gaps.length > 0 ? (
          <div className="banner" style={{ marginBottom: 12 }}>
            <span className="banner-icon">!</span>
            <span>
              近 60 天有 {runs.gaps.length} 个交易日没有预测覆盖：
              {runs.gaps.slice(0, 12).join("、")}
              {runs.gaps.length > 12 ? " …" : ""}
              。{runs.gap_note}
            </span>
          </div>
        ) : (
          <p className="muted-sm">{runs?.gap_note}</p>
        )}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>运行日</th>
                <th>状态</th>
                <th>触发时刻</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              {(runs?.runs || []).length === 0 ? (
                <tr>
                  <td colSpan={4} className="muted">
                    暂无运行记录（作业在每个交易日 15:45 触发）
                  </td>
                </tr>
              ) : (
                (runs?.runs || []).map((r) => (
                  <tr key={r.run_date}>
                    <td className="num">{r.run_date}</td>
                    <td className={r.status === "failed" ? "down" : r.status === "ok" ? "" : "muted"}>
                      {forecastRunStatusLabel(r.status)}
                    </td>
                    <td className="muted-sm">
                      {r.fired_at ? String(r.fired_at).replace("T", " ").slice(0, 19) : "-"}
                    </td>
                    <td className="muted-sm">{r.detail || "-"}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
        <p className="footer-note">{runs?.disclaimer}</p>
      </section>
    </>
  );
}
