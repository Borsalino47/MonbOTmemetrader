export type SetupState =
  | 'IGNORE' | 'OBSERVE' | 'WATCH' | 'ARMED'
  | 'BREAKOUT' | 'RETEST' | 'CONTINUATION' | 'INVALIDATED';

export type LiquidityStatus =
  | 'EXCELLENT' | 'GOOD' | 'ACCEPTABLE' | 'POOR' | 'DANGEROUS' | 'UNKNOWN';

export interface Component {
  name: string;
  points: number;
  max_points: number;
  fraction: number;
  available: boolean;
  reasons: string[];
  detail: Record<string, unknown>;
}

export interface PenaltyItem { name: string; points: number; reason: string }

export type VerdictLevel = 'STRONG' | 'WATCH' | 'RISKY' | 'AVOID';

/** The four-level plain-language summary. Always carries its caveat. */
export interface Verdict {
  level: VerdictLevel;
  emoji: string;
  label: string;
  label_fr: string;
  headline: string;
  headline_fr: string;
  reasons: string[];
  reasons_fr: string[];
  caveat: string;
  caveat_fr: string;
}

export interface ScoreRow {
  symbol: string;
  price: number;
  timestamp_ms: number;
  engine_version: string;
  raw_score: number;
  risk_penalty: number;
  final_score: number;
  opportunity_label: string;
  score_acceleration: number | null;
  previous_score: number | null;
  components: Component[];
  penalties: { total: number; items: PenaltyItem[] };
  pump_maturity: { score: number; is_late: boolean; reasons: string[] };
  acceleration: { momentum_acceleration: number; early_move: number; reasons: string[] };
  data_confidence: { score: number; issues: string[]; max_age_seconds: number | null };
  liquidity: { status: LiquidityStatus; veto: boolean; reasons: string[] };
  safety: { score: number; hard_veto: boolean; reasons: string[] };
  setup: { state: SetupState; rationale: string; trigger: string | null; invalidation: string | null };
  is_premium: boolean;
  verdict: Verdict;
  /** True when the row was restored from SQLite rather than produced by a live
   *  scan. Such a row is a genuine subset: fields never journalled are null. */
  from_journal?: boolean;
  metrics: TableMetrics;
  why: string[];
  risks: string[];
  features?: AssetFeatures;
}

/** Compact per-row numbers, so the table does not need the full feature payload. */
export interface TableMetrics {
  change_primary_pct?: number | null;
  change_5m_pct?: number | null;
  change_15m_pct?: number | null;
  change_1h_pct?: number | null;
  change_4h_pct?: number | null;
  change_24h_pct?: number | null;
  rvol?: number | null;
  volume_acceleration_pct?: number | null;
  atr_pct?: number | null;
  rsi14?: number | null;
  quote_volume_24h?: number | null;
  spread_bps?: number | null;
  order_book_imbalance?: number | null;
  distance_to_breakout_atr?: number | null;
  resistance?: number | null;
  support?: number | null;
}

export interface TimeframeFeatures {
  timeframe: string;
  bias: 'BULLISH' | 'BEARISH' | 'NEUTRAL' | 'UNKNOWN';
  close: number;
  rsi14: number | null;
  atr14: number | null;
  atr_pct: number | null;
  rvol: number | null;
  volume_acceleration_pct: number | null;
  compression: number | null;
  ema20: number | null;
  ema50: number | null;
  vwap20: number | null;
  roc1: number | null;
  consecutive_green: number;
  structure: {
    trend: string;
    nearest_resistance: { price: number; touches: number } | null;
    nearest_support: { price: number; touches: number } | null;
    distance_to_resistance_atr: number | null;
    broke_out: boolean;
    in_retest: boolean;
    range_position: number | null;
  } | null;
}

export interface AssetFeatures {
  symbol: string;
  price: number;
  quote_volume_24h: number | null;
  price_change_pct_24h: number | null;
  order_book_imbalance: number | null;
  spread_bps: number | null;
  timeframes: Record<string, TimeframeFeatures>;
}

export interface AssetDetail extends ScoreRow {
  score_history: ScorePoint[];
  explainability: {
    raw_score: number;
    risk_penalty: number;
    final_score: number;
    breakdown: { component: string; points: number; max: number; reasons: string[] }[];
    penalties: PenaltyItem[];
  };
  why_this_asset: string[];
  what_can_invalidate_it: string[];
}

export interface ScorePoint {
  timestamp_ms: number;
  final_score: number;
  raw_score: number;
  price: number;
  state: string;
}

export interface Health {
  app: string;
  paper_mode: boolean;
  engine_version: string;
  provider: string;
  synthetic_data: boolean;
  /** Server-decided. The dashboard must never infer LIVE vs DEMO from a provider name. */
  data_mode: 'LIVE' | 'DEMO';
  data_mode_detail: string;
  displaying: 'live' | 'journal' | 'nothing';
  journal_snapshot: {
    age_seconds: number;
    provider: string;
    synthetic: boolean;
    data_mode: 'LIVE' | 'DEMO';
    scanned: number;
    succeeded: number;
  } | null;
  synthetic_warning: string | null;
  scan_count: number;
  consecutive_failures: number;
  scan_interval_seconds: number;
  market_regime: { trend: string; volatility: string; reference: string };
  last_scan: {
    finished_at_ms: number;
    age_seconds: number;
    duration_ms: number;
    universe_size: number;
    scanned: number;
    succeeded: number;
    failed: number;
    market_data_age_seconds: number | null;
    data_stale: boolean;
    notes: string[];
  } | null;
  provider_health: {
    name: string; available: boolean; status: string;
    latency_ms: number | null; detail: string | null;
    rate_limit_remaining_pct: number | null;
  }[];
  horizon_tracker: {
    horizons: string[];
    success_criterion: string;
    entry_rule: string;
    checks_signals_older_than: string | null;
    last_run: {
      checked_signals: number;
      resolved_horizons: number;
      pending_horizons: number;
      unresolvable_horizons: number;
    } | null;
  };
  server_time_ms: number;
}

export interface ScanResponse {
  meta: {
    duration_ms: number; universe_size: number; scanned: number;
    succeeded: number; failed: number; synthetic_data: boolean;
    notes: string[]; errors: Record<string, string>; matched: number;
    /** Which source answered: a scan run by this process, or the journal on disk. */
    source: 'live' | 'journal';
    live: boolean;
    stale: boolean;
    data_mode: 'LIVE' | 'DEMO';
    /** Only present on a journal answer: how old the restored rows are. */
    age_seconds?: number;
    message?: string;
    provider?: string;
  };
  results: ScoreRow[];
}

export interface AlertItem {
  symbol: string;
  level: 'INFO' | 'WATCH' | 'HIGH' | 'CRITICAL_SETUP';
  headline: string;
  timestamp_ms: number;
  price: number;
  opportunity_score: string;
  final_score: number;
  pump_maturity: number;
  data_confidence: number;
  safety: number;
  liquidity: string;
  state: string;
  score_acceleration: number | null;
  why: string[];
  risks: string[];
  trigger: string | null;
  invalidation: string | null;
}

// --------------------------------------------------------------- outcomes --

export interface Bucket {
  key: string;
  n: number;
  wins: number;
  losses: number;
  timeouts: number;
  win_rate: number | null;
  expectancy_pct: number | null;
  avg_win_pct: number | null;
  avg_loss_pct: number | null;
  profit_factor: number | null;
  avg_mfe_atr: number | null;
  avg_mae_atr: number | null;
  insufficient_sample: boolean;
}

export interface ComponentEdge {
  component: string;
  avg_points_winners: number;
  avg_points_losers: number;
  edge: number;
  n_winners: number;
  n_losers: number;
  insufficient_sample: boolean;
}

export interface PerformanceResponse {
  counts: {
    total_signals: number;
    pending_evaluation: number;
    unresolvable: number;
    settled: number;
    wins: number;
    losses: number;
    timeouts: number;
    synthetic_signals: number;
  };
  label: { config: string; definition: string };
  costs: Record<string, number | string>;
  performance: {
    overall: Bucket;
    by_score_band: Bucket[];
    by_state: Bucket[];
    by_maturity_band: Bucket[];
    by_liquidity: Bucket[];
    by_regime: Bucket[];
    by_symbol: Bucket[];
    component_edge: ComponentEdge[];
    return_basis: string;
    min_sample: number;
    synthetic_included: boolean;
    synthetic_count: number;
    notes: string[];
  };
}

// --------------------------------------------------------------- horizons --

export interface HorizonBucket {
  key: string;
  horizon: string;
  n: number;
  successes: number;
  success_rate: number | null;
  avg_change_pct: number | null;
  median_change_pct: number | null;
  best_change_pct: number | null;
  worst_change_pct: number | null;
  avg_max_gain_pct: number | null;
  avg_max_drawdown_pct: number | null;
  insufficient_sample: boolean;
}

export interface HorizonResponse {
  tracker: Health['horizon_tracker'];
  costs: Record<string, number | string>;
  synthetic_included: boolean;
  performance: {
    horizons: string[];
    overall: HorizonBucket[];
    by_score_band: HorizonBucket[];
    by_state: HorizonBucket[];
    by_provider: HorizonBucket[];
    return_basis: string;
    min_sample: number;
    synthetic_count: number;
    notes: string[];
  };
}
