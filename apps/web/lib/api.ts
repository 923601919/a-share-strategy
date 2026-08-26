const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
    cache: "no-store",
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
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
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
  risk: {
    level: string;
    messages: string[];
    anomaly_progress?: number;
    anomaly_pct?: number;
    ma5?: number;
  };
  fenshi: Record<string, unknown>;
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
  universe_sectors?: { name: string; pct: number; type?: string; members?: number }[];
  hot_boards: { name: string; pct: number; up_count?: number; leader?: string }[];
  params: Record<string, unknown>;
  count: number;
  items: ScanItem[];
};

export function scan(body?: {
  min_amount_yi?: number;
  min_pct?: number;
  max_pct?: number;
  session?: string;
  top_n?: number;
  mode?: "fenshi" | "leader_dip";
}) {
  return request<ScanResult>("/api/scan", {
    method: "POST",
    body: JSON.stringify(body || {}),
  });
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

export type WatchlistResponse = {
  items: WatchItem[];
  stats: WatchlistStats;
};

export function getWatchlist(opts?: { with_quotes?: boolean; refresh_returns?: boolean }) {
  const q = new URLSearchParams();
  if (opts?.with_quotes) q.set("with_quotes", "true");
  if (opts?.refresh_returns) q.set("refresh_returns", "true");
  const qs = q.toString();
  return request<WatchlistResponse>(`/api/watchlist${qs ? `?${qs}` : ""}`);
}

export function getWatchlistStats() {
  return request<WatchlistStats>("/api/watchlist/stats");
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
    risk?: { level?: string; messages?: string[]; anomaly_pct?: number };
    daily?: { ma5?: number; pct_from_low?: number };
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

export { API_BASE };
