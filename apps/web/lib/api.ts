// 同源相对路径（空串）或绝对地址；?? 而非 ||，避免空串被回退成默认值
const API_BASE = (process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
const API_KEY = process.env.NEXT_PUBLIC_API_KEY || "";
const DEFAULT_TIMEOUT_MS = 30_000;
const SCAN_POLL_MAX_MS = 5 * 60_000;
const TOKEN_KEY = "ashare_access_token";

export class ApiError extends Error {
  kind: "network" | "timeout" | "http" | "business";
  status?: number;
  code?: string | null;

  constructor(
    message: string,
    opts?: { kind?: ApiError["kind"]; status?: number; code?: string | null }
  ) {
    super(message);
    this.name = "ApiError";
    this.kind = opts?.kind || "http";
    this.status = opts?.status;
    this.code = opts?.code;
  }
}

export function getAccessToken(): string {
  if (typeof window === "undefined") return "";
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

export function setAccessToken(token: string | null) {
  if (typeof window === "undefined") return;
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

function redirectToLogin() {
  if (typeof window === "undefined") return;
  const path = window.location.pathname || "";
  if (path.startsWith("/login")) return;
  const next = encodeURIComponent(path + window.location.search);
  window.location.href = `/login?next=${next}`;
}

async function request<T>(
  path: string,
  init?: RequestInit & { timeoutMs?: number; skipAuthRedirect?: boolean }
): Promise<T> {
  const timeoutMs = init?.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const skipAuthRedirect = init?.skipAuthRedirect;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string> | undefined),
  };
  if (API_KEY) headers["X-API-Key"] = API_KEY;
  const token = getAccessToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  try {
    const res = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers,
      cache: "no-store",
      signal: controller.signal,
    });
    if (!res.ok) {
      const text = await res.text();
      let detail = text || res.statusText;
      try {
        const j = JSON.parse(text);
        if (j?.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      } catch {
        /* keep raw */
      }
      if (res.status === 401 && !skipAuthRedirect && !path.startsWith("/api/auth/")) {
        setAccessToken(null);
        redirectToLogin();
      }
      throw new ApiError(detail, { kind: "http", status: res.status });
    }
    return res.json() as Promise<T>;
  } catch (e: unknown) {
    if (e instanceof ApiError) throw e;
    if (e instanceof DOMException && e.name === "AbortError") {
      throw new ApiError(`请求超时（${Math.round(timeoutMs / 1000)}s）`, { kind: "timeout" });
    }
    const msg = e instanceof Error ? e.message : String(e);
    if (/Failed to fetch|NetworkError|fetch/i.test(msg)) {
      throw new ApiError(
        `网络失败或 CORS 被拒（API=${API_BASE}）。请确认后端已启动且 CORS_ORIGINS 包含当前前端地址。`,
        { kind: "network" }
      );
    }
    throw new ApiError(msg, { kind: "network" });
  } finally {
    clearTimeout(timer);
  }
}

export type ScanItem = {
  code: string;
  name: string;
  pct: number;
  price: number;
  amount: number;
  turnover: number;
  volume_ratio: number;
  score: number;
  in_hot_board: boolean;
  reasons: string[];
  selection?: {
    tags?: string[];
    zt?: { zt_count?: number; max_lianban?: number; leader_candidate?: boolean };
    trapped_ratio?: number | null;
    life?: { name?: string; consecutive?: number; coefficient?: number; note?: string } | null;
    coeff?: number;
  };
  risk: {
    level: string;
    messages: string[];
    anomaly_progress?: number;
    anomaly_pct?: number;
    ma5?: number;
    days_to_regulatory_exit?: number | null;
    regulatory_window_end?: string | null;
  };
  fenshi: Record<string, unknown>;
};

export type MarketEnv = {
  level: "normal" | "warn" | "block" | string;
  ref_index?: string;
  ref_name?: string;
  pct?: number | null;
  note?: string;
  sentiment?: {
    phase?: string;
    label?: string;
    hint?: string;
    temperature?: number;
    ice?: boolean;
    euphoria?: boolean;
    metrics?: Record<string, number | null>;
  } | null;
};

export type ScanResult = {
  session_note: string;
  data_source?: {
    spot?: string;
    minute?: string;
    candidates?: number;
    scored?: number;
    fenshi_ok?: number;
    reattack_ok?: number;
    strong_push_ok?: number;
    universe_size?: number;
  };
  universe_sectors?: { name: string; pct: number; type?: string; members?: number; consecutive?: number; life_coeff?: number; life_note?: string }[];
  hot_boards: { name: string; pct: number; up_count?: number; leader?: string }[];
  params: Record<string, unknown>;
  count: number;
  items: ScanItem[];
  timings?: Record<string, number>;
  error_code?: string | null;
  market_env?: MarketEnv;
};

export type ScanJob = {
  job_id: string;
  kind: string;
  status: "queued" | "running" | "done" | "error" | "cancelled" | "lost" | string;
  stage: string;
  progress: number;
  message: string;
  error?: string | null;
  error_code?: string | null;
  timings?: Record<string, number>;
  result?: ScanResult;
  params?: Record<string, unknown>;
};

export type SoftOptBody = {
  open_window?: number;
  vol_mode?: "avg" | "peak";
  min_ratio?: number;
  rally_min_pct?: number;
  rally_window?: number;
  hard_filter?: boolean;
};

export type ScanBody = {
  min_amount_yi?: number;
  min_pct?: number;
  max_pct?: number;
  session?: string;
  top_n?: number;
  mode?: "fenshi" | "leader_dip";
  universe_policy?: "hot_only" | "quota" | "soft" | "soft_opt";
  softopt?: SoftOptBody;
};

export function scan(body?: ScanBody) {
  return request<ScanResult>("/api/scan", {
    method: "POST",
    body: JSON.stringify(body || {}),
  });
}

export function startScanJob(body?: ScanBody) {
  return request<ScanJob>("/api/scan/jobs", {
    method: "POST",
    body: JSON.stringify(body || {}),
  });
}

export function getScanJob(jobId: string) {
  return request<ScanJob>(`/api/scan/jobs/${jobId}`);
}

export function cancelScanJob(jobId: string) {
  return request<{ ok: boolean } & ScanJob>(`/api/scan/jobs/${jobId}/cancel`, {
    method: "POST",
    body: "{}",
  });
}

const ERROR_HINTS: Record<string, string> = {
  session_blocked: "当前不在扫描时段。可把时段改为「不限」再试。",
  empty_universe: "强势板块成分池为空，暂无候选。",
  universe_failed: "板块成分池拉取失败（网络或数据源）。稍后重试。",
  no_quotes: "没有拿到真实行情报价。",
  market_blocked: "大盘环境走弱，进攻型策略已暂停推荐。",
  cancelled: "扫描已取消。",
  lost: "服务重启，扫描任务已失效，请重新扫描。",
  scan_timeout: "扫描超时，请稍后重试或缩小扫描范围。",
};

export function explainScanError(code?: string | null, fallback?: string) {
  if (code && ERROR_HINTS[code]) return ERROR_HINTS[code];
  return fallback || "扫描失败";
}

/** 异步扫描：轮询进度，可中止；总时长上限默认 5 分钟。 */
export async function scanWithProgress(
  body: ScanBody | undefined,
  opts: {
    onProgress?: (job: ScanJob) => void;
    shouldStop?: () => boolean;
    intervalMs?: number;
    maxMs?: number;
  } = {}
): Promise<ScanResult> {
  const started = await startScanJob(body);
  const interval = opts.intervalMs ?? 800;
  const maxMs = opts.maxMs ?? SCAN_POLL_MAX_MS;
  const t0 = Date.now();
  while (true) {
    if (Date.now() - t0 > maxMs) {
      try {
        await cancelScanJob(started.job_id);
      } catch {
        /* ignore */
      }
      throw new ApiError(`扫描超时（>${Math.round(maxMs / 1000)}s），已请求取消`, {
        kind: "timeout",
        code: "scan_timeout",
      });
    }
    if (opts.shouldStop?.()) {
      try {
        await cancelScanJob(started.job_id);
      } catch {
        /* ignore */
      }
      throw new ApiError(explainScanError("cancelled"), { kind: "business", code: "cancelled" });
    }
    const job = await getScanJob(started.job_id);
    opts.onProgress?.(job);
    if (job.status === "done") {
      if (!job.result) throw new ApiError("扫描完成但无结果", { kind: "business" });
      return job.result;
    }
    if (job.status === "cancelled" || job.status === "lost") {
      throw new ApiError(explainScanError(job.status === "lost" ? "lost" : "cancelled"), {
        kind: "business",
        code: job.status,
      });
    }
    if (job.status === "error") {
      throw new ApiError(explainScanError(job.error_code, job.error || job.message || "扫描失败"), {
        kind: "business",
        code: job.error_code,
      });
    }
    await new Promise((r) => setTimeout(r, interval));
  }
}

/**
 * 自选/持仓来源 → 中文名。
 * 必须与后端 apps/api/services/stats.py 的 SOURCE_LABELS 保持一致，
 * 否则统计页（用后端返回的 label）与自选/持仓页（前端查表）会显示不一致。
 * soft / soft_opt 为 verify 汇总账户区分三策略来源而新增（2026-09-10）。
 */
export const SOURCE_LABELS: Record<string, string> = {
  fenshi: "进攻型分时",
  longtou: "龙头低吸",
  manual: "手工",
  soft: "软加权",
  soft_opt: "优化软加权",
};

/** 取来源中文名；未知来源原样返回（新策略上线时不会显示成空白）。 */
export function sourceLabel(source?: string | null): string {
  if (!source) return "-";
  return SOURCE_LABELS[source] || source;
}

export type WatchTrackDay = {
  day_offset: number;
  trade_date: string;
  close_price?: number;
  return_pct?: number;
};

export type WatchItem = {
  code: string;
  name: string;
  source: string;
  note: string;
  created_at?: string;
  entry_price?: number;
  entry_pct?: number;
  entry_score?: number;
  track_id?: number;
  quote?: {
    price?: number;
    pct?: number;
    risk?: {
      level?: string;
      messages?: string[];
      anomaly_progress?: number;
      anomaly_pct?: number;
      ma5?: number;
    };
  };
  track?: {
    entry_price?: number;
    entry_pct?: number;
    entry_score?: number;
    t0?: WatchTrackDay;
    t1?: WatchTrackDay;
    t2?: WatchTrackDay;
    t3?: WatchTrackDay;
    latest_return_pct?: number;
    latest_day_offset?: number;
  };
  returns?: WatchTrackDay[];
};

export type WatchlistStats = {
  total: number;
  with_t3: number;
  win_rate_t3: number;
  avg_return_t3: number;
  by_source: Record<string, { count: number; win_rate: number; avg_return: number }>;
  by_score_bucket: Record<string, { count: number; win_rate: number; avg_return: number }>;
};

export type WatchlistExpired = {
  code: string;
  name?: string;
  reason?: string;
  t3_return_pct?: number | null;
  exit_return_pct?: number | null;
  completed_at?: string;
};

export type WatchlistResponse = {
  items: WatchItem[];
  stats: WatchlistStats;
  expired?: WatchlistExpired[];
};

export function getWatchlist(opts?: {
  with_quotes?: boolean;
  refresh_returns?: boolean;
  with_risk?: boolean;
}) {
  const q = new URLSearchParams();
  if (opts?.with_quotes) q.set("with_quotes", "true");
  if (opts?.refresh_returns) q.set("refresh_returns", "true");
  if (opts?.with_risk) q.set("with_risk", "true");
  const qs = q.toString();
  return request<WatchlistResponse>(`/api/watchlist${qs ? `?${qs}` : ""}`);
}

export function getWatchlistStats() {
  return request<WatchlistStats>("/api/watchlist/stats");
}

export type DayStat = {
  day: string;
  count: number;
  win_rate: number | null;
  avg_return: number | null;
  median_return?: number | null;
  avg_win?: number | null;
  avg_loss?: number | null;
  best?: number | null;
  worst?: number | null;
};

export type StatGroup = {
  bucket?: string;
  source?: string;
  label: string;
  tracks: number;
  sufficient?: boolean;
  days: DayStat[];
};

export type ScoreEffectiveness = {
  generated_at: string;
  days_window: number | null;
  min_samples: number;
  summary: {
    total_tracks: number;
    tracks_with_score: number;
    tracks_without_score: number;
    with_t3: number;
  };
  buckets: StatGroup[];
  by_source: StatGroup[];
  bucket_by_source_t3: Array<{
    bucket: string;
    source: string;
    label: string;
    count: number;
    win_rate: number | null;
    avg_return: number | null;
  }>;
  position_buckets: StatGroup[];
  monthly: Array<{
    month: string;
    tracks: number;
    t1: DayStat;
    t3: DayStat;
  }>;
  exits: DayStat;
};

export function getScoreEffectiveness(days?: number) {
  const q = days ? `?days=${days}` : "";
  return request<ScoreEffectiveness>(`/api/stats/score-effectiveness${q}`, {
    timeoutMs: 60_000,
  });
}

export type ScanQualityRow = {
  id: number;
  created_at: string;
  mode: string;
  universe_policy: string;
  candidates?: number | null;
  scored?: number | null;
  fenshi_ok?: number | null;
  proxy_count?: number | null;
  timed_out?: number | null;
  total_ms?: number | null;
  market_env_level?: string | null;
  market_pct?: number | null;
  spot_source?: string | null;
  strategy_version?: string | null;
  top_avg_day_position?: number | null;
};

export function getScanQuality(limit = 200, mode?: string) {
  const qs = new URLSearchParams({ limit: String(limit) });
  if (mode) qs.set("mode", mode);
  return request<{ count: number; items: ScanQualityRow[] }>(`/api/stats/scan-quality?${qs}`, {
    timeoutMs: 30_000,
  });
}

export type WatchHistoryItem = {
  id: number;
  code: string;
  name: string;
  source?: string;
  note?: string;
  entry_price?: number | null;
  entry_pct?: number | null;
  entry_score?: number | null;
  created_at?: string;
  removed_at?: string | null;
  exit_price?: number | null;
  exit_return_pct?: number | null;
  completion_reason?: string | null;
  completion_snapshot?: Record<string, unknown> | string | null;
  returns?: WatchTrackDay[];
  t3_return_pct?: number | null;
};

export function getWatchlistHistory(limit = 100) {
  // 纯本地库，但默认超时仍给足余量，避免与扫描等长任务抢 worker 时误杀
  return request<{ items: WatchHistoryItem[]; stats: WatchlistStats }>(
    `/api/watchlist/history?limit=${limit}`,
    { timeoutMs: 60_000 }
  );
}

export type WatchRefreshJob = {
  job_id: string;
  kind: string;
  status: "queued" | "running" | "done" | "error" | "cancelled" | "lost" | string;
  stage: string;
  progress: number;
  message: string;
  error?: string | null;
  error_code?: string | null;
  result?: WatchlistResponse;
};

export function startWatchRefreshJob(opts?: { with_quotes?: boolean; with_risk?: boolean }) {
  const q = new URLSearchParams();
  if (opts?.with_quotes === false) q.set("with_quotes", "false");
  if (opts?.with_risk === false) q.set("with_risk", "false");
  const qs = q.toString();
  return request<WatchRefreshJob>(`/api/watchlist/refresh/jobs${qs ? `?${qs}` : ""}`, {
    method: "POST",
    body: "{}",
  });
}

export function getWatchRefreshJob(jobId: string) {
  return request<WatchRefreshJob>(`/api/watchlist/refresh/jobs/${jobId}`);
}

export function cancelWatchRefreshJob(jobId: string) {
  return request<{ ok: boolean } & WatchRefreshJob>(`/api/watchlist/refresh/jobs/${jobId}/cancel`, {
    method: "POST",
    body: "{}",
  });
}

/** 异步刷新收益/异动：轮询进度，可中止。 */
export async function refreshWatchlistWithProgress(
  opts: {
    with_quotes?: boolean;
    with_risk?: boolean;
    onProgress?: (job: WatchRefreshJob) => void;
    shouldStop?: () => boolean;
    intervalMs?: number;
    maxMs?: number;
  } = {}
): Promise<WatchlistResponse> {
  const started = await startWatchRefreshJob({
    with_quotes: opts.with_quotes !== false,
    with_risk: opts.with_risk !== false,
  });
  const interval = opts.intervalMs ?? 600;
  const maxMs = opts.maxMs ?? SCAN_POLL_MAX_MS;
  const t0 = Date.now();
  while (true) {
    if (Date.now() - t0 > maxMs) {
      try {
        await cancelWatchRefreshJob(started.job_id);
      } catch {
        /* ignore */
      }
      throw new ApiError(`刷新超时（>${Math.round(maxMs / 1000)}s），已请求取消`, {
        kind: "timeout",
        code: "watch_refresh_timeout",
      });
    }
    if (opts.shouldStop?.()) {
      try {
        await cancelWatchRefreshJob(started.job_id);
      } catch {
        /* ignore */
      }
      throw new ApiError("已取消刷新", { kind: "business", code: "cancelled" });
    }
    const job = await getWatchRefreshJob(started.job_id);
    opts.onProgress?.(job);
    if (job.status === "done") {
      if (!job.result) throw new ApiError("刷新完成但无结果", { kind: "business" });
      return job.result;
    }
    if (job.status === "cancelled" || job.status === "lost") {
      throw new ApiError(
        job.status === "lost" ? "服务重启，刷新任务已失效" : "已取消刷新",
        { kind: "business", code: job.status }
      );
    }
    if (job.status === "error") {
      throw new ApiError(job.error || job.message || "刷新失败", {
        kind: "business",
        code: job.error_code,
      });
    }
    await new Promise((r) => setTimeout(r, interval));
  }
}

export function addWatch(payload: {
  code: string;
  name: string;
  source?: string;
  note?: string;
  entry_price?: number;
  entry_pct?: number;
  entry_score?: number;
  minute_confirmed?: boolean;
  day_position?: number | null;
  vwap_deviation?: number | null;
}) {
  return request<{
    code: string;
    name: string;
    entry_price?: number;
    sim?: {
      ok?: boolean;
      skipped?: boolean;
      reason?: string;
      position?: { shares?: number; cost_price?: number; take_profit_price?: number; stop_loss_price?: number };
      sizing?: { shares?: number; pct?: number; reason?: string };
    };
  }>("/api/watchlist", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function removeWatch(code: string) {
  return request(`/api/watchlist/${code}`, { method: "DELETE" });
}

export type ConditionOrder = {
  side: "buy" | "sell";
  priority: number;
  code: string;
  name: string;
  title: string;
  trigger: string;
  action: string;
  price_hint?: number;
  window?: string;
  reason?: string;
  playbook?: string;
};

export type ReviewResult = {
  id?: number;
  trade_date: string;
  created_at?: string;
  saved_at?: string;
  summary: {
    trade_date: string;
    watch_count: number;
    watch_up: number;
    watch_down: number;
    reattack_count: number;
    buy_orders: number;
    sell_orders: number;
    top_boards: { name?: string; pct?: number }[];
    macro_weak?: boolean;
    global_macro?: {
      indices?: { name: string; pct: number; market?: string }[];
      avg_pct?: number | null;
      weak?: boolean;
      weak_reason?: string;
    };
    notes: string[];
    verdict: string;
  };
  boards: { name: string; pct: number }[];
  universe_sectors?: { name: string; pct: number; type?: string }[];
  watch_reviews: {
    code: string;
    name: string;
    price?: number;
    entry_price?: number;
    day_return_pct?: number | null;
    entry_score?: number;
    fenshi?: Record<string, unknown>;
    risk?: {
      level?: string;
      messages?: string[];
      anomaly_pct?: number;
      days_to_regulatory_exit?: number | null;
      regulatory_window_end?: string | null;
    };
    daily?: { ma5?: number; pct_from_low?: number; days_to_regulatory_exit?: number; regulatory_window_end?: string };
  }[];
  orders: ConditionOrder[];
  next_day_checklist: string[];
};

export function runReview(body?: { trade_date?: string; persist?: boolean }) {
  return request<ReviewResult>("/api/review/run", {
    method: "POST",
    body: JSON.stringify(body || {}),
  });
}

export function getLatestReview(trade_date?: string) {
  const q = trade_date ? `?trade_date=${encodeURIComponent(trade_date)}` : "";
  return request<ReviewResult>(`/api/review/latest${q}`);
}

export function getReviewHistory() {
  return request<{ items: { id: number; trade_date: string; created_at: string }[] }>(
    "/api/review/history"
  );
}

export type SimPosition = {
  id: number;
  code: string;
  name: string;
  shares: number;
  cost_price: number;
  opened_at: string;
  source?: string;
  take_profit_pct?: number;
  stop_loss_pct?: number;
  take_profit_price?: number;
  stop_loss_price?: number;
  quote_price?: number;
  market_value?: number;
  unrealized_pnl?: number;
  unrealized_pct?: number;
  quote_pct?: number;
  t1_sellable?: boolean;
  t1_lock_reason?: string | null;
};

export type SimOrder = {
  id: number;
  position_id?: number;
  code: string;
  name: string;
  side: string;
  order_type: string;
  trigger_price: number;
  trigger_pct?: number;
  status: string;
  reason?: string;
};

export type SimTrade = {
  id: number;
  code: string;
  name: string;
  side: string;
  shares: number;
  price: number;
  amount: number;
  fee: number;
  pnl?: number | null;
  pnl_pct?: number | null;
  reason?: string;
  traded_at: string;
};

export type SimOverview = {
  account: {
    cash: number;
    initial_capital: number;
    market_value: number;
    equity: number;
    total_pnl: number;
    total_pnl_pct: number;
    realized_pnl: number;
    updated_at?: string;
  };
  positions: SimPosition[];
  orders: SimOrder[];
  trades: SimTrade[];
  stats: {
    open_count: number;
    max_positions: number;
    trade_count: number;
    sell_count: number;
    win_count: number;
    win_rate?: number | null;
    avg_sell_pnl_pct?: number | null;
    take_profit_pct: number;
    stop_loss_pct: number;
    take_profit_by_source?: Record<string, number>;
    stop_loss_by_source?: Record<string, number>;
  };
};

export function getSim() {
  return request<SimOverview>("/api/sim");
}

export function evaluateSim() {
  return request<SimOverview & { evaluate?: unknown }>("/api/sim/evaluate", {
    method: "POST",
    body: "{}",
  });
}

export function sellSim(position_id: number, price?: number) {
  return request("/api/sim/sell", {
    method: "POST",
    body: JSON.stringify({ position_id, price }),
  });
}

export function resetSim(initial_capital?: number) {
  return request<SimOverview>("/api/sim/reset", {
    method: "POST",
    body: JSON.stringify({ initial_capital }),
  });
}

export type AuthUser = {
  id: number;
  username: string;
  role: string;
  created_at?: string;
};

export type AuthSession = {
  access_token: string;
  token_type: string;
  user: AuthUser;
};

export function getAuthStatus() {
  return request<{
    auth_required: boolean;
    bootstrap_available: boolean;
    user_count: number;
  }>("/api/auth/status", { skipAuthRedirect: true });
}

export function login(username: string, password: string) {
  return request<AuthSession>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
    skipAuthRedirect: true,
  });
}

export function register(username: string, password: string, invite_code: string) {
  return request<AuthSession>("/api/auth/register", {
    method: "POST",
    body: JSON.stringify({ username, password, invite_code }),
    skipAuthRedirect: true,
  });
}

export function bootstrapAdmin(username: string, password: string) {
  return request<AuthSession>("/api/auth/bootstrap", {
    method: "POST",
    body: JSON.stringify({ username, password }),
    skipAuthRedirect: true,
  });
}

export function getMe() {
  return request<AuthUser>("/api/auth/me");
}

export function createInvite(note = "") {
  return request<{ code: string; created_at: string; note: string }>("/api/auth/invites", {
    method: "POST",
    body: JSON.stringify({ note }),
  });
}

export function listInvites() {
  return request<{
    items: Array<{
      code: string;
      created_by: number | null;
      created_at: string;
      used_by: number | null;
      used_at: string | null;
      note: string;
    }>;
  }>("/api/auth/invites");
}

export function logout() {
  setAccessToken(null);
}

// ============================================================ 大盘预判（六爻）
//
// ⚠ 这一组接口**只有读**：预测只能由服务端 15:45 的定时作业产生。
// 任何"手动补一条"的入口都会让准确率失去意义——因为它证明不了
// 起卦时点是我们事前选的，而不是事后挑的。
//
// 展示层有一条硬要求：**不要只显示一个百分比**。报告的完整结构里带基线、
// 置信区间、p 值、随样本量变化的门槛与样本充足性标记；把 rate 单独拎出来展示，
// 正是在这个功能上最容易误导人的做法。

export type ForecastDirection = "up" | "down" | "flat";

export type ForecastOutcome = {
  actual_direction: ForecastDirection;
  actual_change: number;
  close: number;
  base_close: number;
  basis: string;
  /** bar=官方日线 / sina_rt=收盘后实时行情先结掉 / rtQuote=实时口径 */
  source: string;
  /** true 命中 / false 未命中 / null 不计入二元统计（任一方为平盘） */
  hit: boolean | null;
  settled_at: string;
};

export type ForecastItem = {
  id: string;
  symbol: string;
  target_date: string;
  /** 起卦时点（必须早于 target_date，否则就是未来函数） */
  as_of: string;
  generated_at: string;
  /** live=当时真实产出 / replay=事后回补（不进准确率） */
  origin: "live" | "replay";
  direction: ForecastDirection;
  strength: number;
  hexagram: string | null;
  changed_hexagram: string | null;
  engine_ver: string;
  ruleset_ver: string;
  outcome: ForecastOutcome | null;
};

export type ForecastLatest = {
  symbol: string;
  symbol_label: string;
  server_time: string;
  upcoming: ForecastItem | null;
  recent_settled: ForecastItem | null;
  disclaimer: string;
};

export type ForecastHistory = {
  symbol: string;
  items: ForecastItem[];
  disclaimer: string;
};

export type ForecastPermutation = {
  iterations: number;
  observed_hits: number;
  observed_rate: number;
  null_mean: number;
  null_std: number;
  null_p95: number;
  p_value: number;
  edge: number;
};

export type ForecastAccuracy = {
  symbol: string;
  window_days: number;
  /** 有效样本数（平盘已剔除） */
  n: number;
  hits: number;
  rate: number;
  ci: { low: number; high: number };
  p_value: number;
  baselines: { random: number; always_up: number; always_down: number };
  significant: boolean;
  /** 当前样本量下"优于随机"所需的准确率门槛（随 n 变化） */
  threshold: number;
  flat_count: number;
  sample_sufficient: boolean;
  note: string;
  /** 偏置匹配置换检验：样本 < 20 时为 null */
  permutation: ForecastPermutation | null;
  pending_count: number;
  disclaimer: string;
};

export type ForecastRun = {
  run_date: string;
  status: string;
  detail: string;
  fired_at: string | null;
};

export type ForecastRuns = {
  symbol: string;
  runs: ForecastRun[];
  /** 交易日但没有任何预测覆盖的那些日子（如实列出，不伪造补跑） */
  gaps: string[];
  gap_note: string;
  disclaimer: string;
};

export function getForecastLatest() {
  return request<ForecastLatest>("/api/market/forecast/latest");
}

export function getForecastHistory(limit = 30) {
  return request<ForecastHistory>(`/api/market/forecast/history?limit=${encodeURIComponent(limit)}`);
}

export function getForecastAccuracy(windowDays = 0) {
  return request<ForecastAccuracy>(
    `/api/market/forecast/accuracy?window_days=${encodeURIComponent(windowDays)}`
  );
}

export function getForecastRuns(limit = 30, gapDays = 60) {
  return request<ForecastRuns>(
    `/api/market/forecast/runs?limit=${encodeURIComponent(limit)}&gap_days=${encodeURIComponent(gapDays)}`
  );
}

/** 方向的中文标签。flat 只可能出现在**结算结果**里，预测侧不会输出它。 */
export function directionLabel(d: ForecastDirection | string | null | undefined): string {
  if (d === "up") return "涨";
  if (d === "down") return "跌";
  if (d === "flat") return "平盘";
  return "-";
}

/** 结算依据来源的中文说明。区分它是有意义的：实时口径是可以被官方日线修正的。 */
export function outcomeSourceLabel(s: string | null | undefined): string {
  if (s === "bar") return "官方日线";
  if (s === "sina_rt" || s === "rtQuote") return "实时行情（待官方日线确认）";
  return s || "-";
}

/** 作业状态的中文说明。failed 必须显眼——那代表当天样本永久缺了一条。 */
export function forecastRunStatusLabel(s: string | null | undefined): string {
  if (s === "ok") return "正常";
  if (s === "partial") return "部分完成";
  if (s === "failed") return "失败（当天无产出）";
  if (s === "holiday") return "非交易日";
  return s || "-";
}

export { API_BASE };
