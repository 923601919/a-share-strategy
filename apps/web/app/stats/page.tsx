"use client";

import { useEffect, useState } from "react";
import {
  getScanQuality,
  getScoreEffectiveness,
  type DayStat,
  type ScanQualityRow,
  type ScoreEffectiveness,
  type StatGroup,
} from "@/lib/api";

function pct(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return <span className="muted">-</span>;
  const n = Number(v);
  const cls = n > 0 ? "good" : n < 0 ? "bad" : "";
  return (
    <span className={cls}>
      {n > 0 ? "+" : ""}
      {n.toFixed(2)}%
    </span>
  );
}

function wr(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return <span className="muted">-</span>;
  const n = Number(v);
  return <span className={n >= 50 ? "good" : n > 0 ? "bad" : ""}>{n.toFixed(1)}%</span>;
}

function DayTable({ days, focus }: { days: DayStat[]; focus?: number }) {
  return (
    <table>
      <thead>
        <tr>
          <th>持有</th>
          <th>样本</th>
          <th>胜率</th>
          <th>平均收益</th>
          <th>中位数</th>
          <th>均盈 / 均亏</th>
          <th>最好 / 最差</th>
        </tr>
      </thead>
      <tbody>
        {days.map((d) => {
          const key = parseInt(d.day.replace("T+", ""), 10);
          const hl = focus === key;
          return (
            <tr key={d.day} style={hl ? { background: "rgba(61,156,240,0.08)" } : undefined}>
              <td>
                {d.day}
                {hl ? " ★" : ""}
              </td>
              <td>{d.count}</td>
              <td>{wr(d.win_rate)}</td>
              <td>{pct(d.avg_return)}</td>
              <td>{pct(d.median_return)}</td>
              <td>
                {pct(d.avg_win)} <span className="muted">/</span> {pct(d.avg_loss)}
              </td>
              <td>
                {pct(d.best)} <span className="muted">/</span> {pct(d.worst)}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function GroupSection({
  title,
  hint,
  groups,
}: {
  title: string;
  hint?: string;
  groups: StatGroup[];
}) {
  return (
    <section className="panel">
      <h3 style={{ marginTop: 0 }}>{title}</h3>
      {hint ? <p className="muted" style={{ marginTop: -6 }}>{hint}</p> : null}
      {groups.map((g) => (
        <div key={g.bucket || g.source} style={{ marginBottom: 18 }}>
          <div className="row" style={{ alignItems: "baseline", gap: 10 }}>
            <strong>{g.label}</strong>
            <span className="muted">入池 {g.tracks} 只</span>
            {g.tracks > 0 && g.sufficient === false ? (
              <span className="pill warn">样本不足</span>
            ) : null}
          </div>
          {g.tracks > 0 ? (
            <DayTable days={g.days} />
          ) : (
            <p className="muted" style={{ margin: "6px 0 0" }}>
              暂无记录（分数在入池时记录，历史数据越多越准）
            </p>
          )}
        </div>
      ))}
    </section>
  );
}

export default function StatsPage() {
  const [data, setData] = useState<ScoreEffectiveness | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [days, setDays] = useState<number | 0>(0);
  const [quality, setQuality] = useState<ScanQualityRow[] | null>(null);

  async function load(d?: number) {
    setLoading(true);
    setError("");
    try {
      const res = await getScoreEffectiveness(d || undefined);
      setData(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  async function loadQuality() {
    try {
      const res = await getScanQuality(200);
      setQuality(res.items);
    } catch {
      /* 质量摘要加载失败不影响主统计 */
    }
  }

  useEffect(() => {
    load();
    loadQuality();
  }, []);

  const s = data?.summary;
  const matrix = data?.bucket_by_source_t3 || [];
  const matrixBuckets = [...new Set(matrix.map((m) => m.bucket))];
  const matrixSources = [...new Set(matrix.map((m) => m.source))];
  const bucketLabel = (k: string) =>
    data?.buckets.find((b) => b.bucket === k)?.label || k;
  const sourceLabel = (k: string) =>
    data?.by_source.find((b) => b.source === k)?.label || k;

  return (
    <>
      <section className="panel">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <h1 style={{ margin: "0 0 6px", fontSize: 20 }}>分数有效性验证</h1>
            <p className="muted" style={{ margin: 0 }}>
              打分高的票真的更容易赚钱吗？按入池分数分桶统计 T+0~T+3 胜率与收益。数据来自历史入池记录，纯本地计算。
            </p>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <select
              value={days}
              onChange={(e) => {
                const v = Number(e.target.value);
                setDays(v);
                load(v);
              }}
            >
              <option value={0}>全部历史</option>
              <option value={30}>最近 30 天</option>
              <option value={90}>最近 90 天</option>
              <option value={180}>最近 180 天</option>
            </select>
            <button className="secondary" onClick={() => load(days)} disabled={loading}>
              {loading ? "统计中…" : "刷新"}
            </button>
          </div>
        </div>
        {error ? <p className="err">{error}</p> : null}
        {s ? (
          <div className="chips" style={{ marginTop: 12 }}>
            <div className="chip">入池记录 {s.total_tracks}</div>
            <div className="chip">有分数 {s.tracks_with_score}</div>
            <div className="chip">无分数 {s.tracks_without_score}</div>
            <div className="chip">已有 T+3 数据 {s.with_t3}</div>
            {data?.exits?.count ? <div className="chip">实际退出 {data.exits.count} 笔</div> : null}
          </div>
        ) : null}
        {s && s.total_tracks > 0 && s.with_t3 < 5 ? (
          <p className="muted" style={{ marginBottom: 0 }}>
            提示：当前样本很少，结论暂不具统计意义。入池记录会在 T+3 归档后自动进入统计。
          </p>
        ) : null}
      </section>

      {data ? (
        <>
          <GroupSection
            title="按入池分数分桶"
            hint="若高分数桶的 T+1/T+3 胜率与平均收益没有系统性高于低分桶，说明打分权重需要调整。"
            groups={data.buckets}
          />

          <GroupSection
            title="按策略来源"
            hint="进攻型分时 vs 龙头低吸 vs 手工入池的表现对比。"
            groups={data.by_source}
          />

          <GroupSection
            title="按日内位置（追高参数校准）"
            hint="入池时买在日内什么位置，直接检验追高惩罚阈值是否合理：若 ≥90% 高位桶的 T+N 收益明显更差，说明惩罚方向正确；若 60-90% 桶也普遍亏损，可考虑收严 chase_pos_high。"
            groups={data.position_buckets}
          />

          {matrix.length > 0 ? (
            <section className="panel">
              <h3 style={{ marginTop: 0 }}>分数 × 来源（T+3 交叉）</h3>
              <table>
                <thead>
                  <tr>
                    <th>分数桶</th>
                    {matrixSources.map((src) => (
                      <th key={src}>{sourceLabel(src)}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {matrixBuckets.map((bk) => (
                    <tr key={bk}>
                      <td>{bucketLabel(bk)}</td>
                      {matrixSources.map((src) => {
                        const cell = matrix.find((m) => m.bucket === bk && m.source === src);
                        return (
                          <td key={src}>
                            {cell ? (
                              <>
                                {pct(cell.avg_return)} · 胜率 {wr(cell.win_rate)} ·{" "}
                                <span className="muted">{cell.count} 样本</span>
                              </>
                            ) : (
                              <span className="muted">-</span>
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          ) : null}

          {data.monthly.length > 0 ? (
            <section className="panel">
              <h3 style={{ marginTop: 0 }}>按入池月份（策略衰减监测）</h3>
              <p className="muted" style={{ marginTop: -6 }}>
                若近月胜率/收益持续下滑，说明策略在当前市况下可能失效。
              </p>
              <table>
                <thead>
                  <tr>
                    <th>月份</th>
                    <th>入池</th>
                    <th>T+1 胜率</th>
                    <th>T+1 均收</th>
                    <th>T+3 胜率</th>
                    <th>T+3 均收</th>
                  </tr>
                </thead>
                <tbody>
                  {data.monthly.map((m) => (
                    <tr key={m.month}>
                      <td>{m.month}</td>
                      <td>{m.tracks}</td>
                      <td>{wr(m.t1.win_rate)}</td>
                      <td>{pct(m.t1.avg_return)}</td>
                      <td>{wr(m.t3.win_rate)}</td>
                      <td>{pct(m.t3.avg_return)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          ) : null}

          {data.exits.count > 0 ? (
            <section className="panel">
              <h3 style={{ marginTop: 0 }}>实际退出收益（T+3 归档/手动移除）</h3>
              <table>
                <tbody>
                  <tr>
                    <td>退出笔数 {data.exits.count}</td>
                    <td>胜率 {wr(data.exits.win_rate)}</td>
                    <td>平均 {pct(data.exits.avg_return)}</td>
                    <td>中位数 {pct(data.exits.median_return)}</td>
                    <td>
                      均盈 {pct(data.exits.avg_win)} / 均亏 {pct(data.exits.avg_loss)}
                    </td>
                  </tr>
                </tbody>
              </table>
            </section>
          ) : null}
        </>
      ) : null}

      <section className="panel">
        <h3 style={{ marginTop: 0 }}>扫描质量（近期）</h3>
        <p className="muted" style={{ marginTop: -6 }}>
          每次扫描的候选/真分时/代理占比/耗时/大盘环境。用于对比数据源健康与调参效果，数据随扫描自动积累。
        </p>
        {quality && quality.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>模式</th>
                <th>候选</th>
                <th>真分时</th>
                <th>代理</th>
                <th>超时</th>
                <th>耗时</th>
                <th>大盘</th>
                <th>日内位置均值</th>
              </tr>
            </thead>
            <tbody>
              {quality.slice(0, 30).map((q) => (
                <tr key={q.id}>
                  <td className="muted">{q.created_at?.replace("T", " ").slice(5, 16)}</td>
                  <td>
                    {q.mode === "leader_dip" ? "龙头低吸" : "进攻分时"}
                    {q.universe_policy === "soft"
                      ? "·软"
                      : q.universe_policy === "quota"
                        ? "·配额"
                        : ""}
                  </td>
                  <td>{q.candidates ?? "-"}</td>
                  <td>{q.fenshi_ok ?? "-"}</td>
                  <td className={q.proxy_count ? "bad" : ""}>{q.proxy_count ?? 0}</td>
                  <td className={q.timed_out ? "bad" : ""}>{q.timed_out ?? 0}</td>
                  <td>{q.total_ms != null ? `${Math.round(q.total_ms / 1000)}s` : "-"}</td>
                  <td>
                    {q.market_pct != null ? (
                      <span className={q.market_pct < 0 ? "down" : "up"}>
                        {q.market_pct > 0 ? "+" : ""}
                        {q.market_pct}%
                      </span>
                    ) : (
                      "-"
                    )}
                    {q.market_env_level === "warn" ? (
                      <span className="muted"> 偏弱</span>
                    ) : q.market_env_level === "block" ? (
                      <span className="bad"> 观望</span>
                    ) : null}
                  </td>
                  <td>
                    {q.top_avg_day_position != null
                      ? `${Math.round(q.top_avg_day_position * 100)}%`
                      : "-"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted" style={{ marginBottom: 0 }}>
            暂无扫描质量数据。跑一次扫描后会自动记录。
          </p>
        )}
      </section>
    </>
  );
}
