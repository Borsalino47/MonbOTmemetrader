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
  /** The explosion engine's separate answer, never folded into final_score.
   *  Null means the engine did not run for this row — different from a zero,
   *  which is a statement about the token. */
  explosion: Explosion | null;
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

// ---------------------------------------------------------------- hunter --

export interface Candidate {
  symbol: string;
  price: number;
  quote_volume_24h: number;
  change_pct_24h: number | null;
  range_position_24h: number | null;
  spread_bps: number | null;
  trades_24h: number;
  avg_trade_size: number | null;
  seconds_since_previous: number | null;
  /** Excess over the same moment yesterday, NOT volume in the last minute:
   *  it is the difference between two 24h rolling counters. */
  volume_excess_vs_yesterday: number | null;
  trade_excess_vs_yesterday: number | null;
  price_change_since_previous_pct: number | null;
  priority: number;
  reasons: string[];
  caveats: string[];
}

export interface HuntResponse {
  prescan: {
    universe_size: number;
    eligible: number;
    returned: number;
    rejected: Record<string, number>;
    has_previous_reading: boolean;
    requests_used: number;
    notes: string[];
    candidates: Candidate[];
  };
  data_mode: 'LIVE' | 'DEMO';
  disclaimer: string;
}

export interface DeepResult {
  candidate: Candidate;
  discovery: {
    symbol: string;
    /** A 0-100 ranking of behavioural change. Never a probability. */
    discovery_score: number;
    discovery_label: string;
    engine_version: string;
    weights_fingerprint: string;
    components: Component[];
    why: string[];
    risks: string[];
    disclaimer: string;
  };
  /** Present only when the classic scan also covered this symbol. */
  opportunity: {
    final_score: number;
    opportunity_label: string;
    state: string;
    pump_maturity: number;
    data_confidence: number;
    liquidity: string;
    verdict: Verdict;
  } | null;
  reused_from_scan: boolean;
}

export interface DeepScanResponse {
  deep_scan: {
    examined: number;
    reused_from_scan: number;
    newly_fetched: number;
    kline_requests: number;
    errors: Record<string, string>;
    duration_ms: number;
    notes: string[];
    results: DeepResult[];
  };
  data_mode: 'LIVE' | 'DEMO';
  engine: { discovery_engine: string; weights_fingerprint: string; weights: Record<string, number> };
  disclaimer: string;
}

/** One acceleration this token actually had, with the state that preceded it.
 *  `resolution_minutes` is carried because timing is known to the bar and no
 *  finer — the UI must never render a precision the detection does not have. */
export interface PumpEpisode {
  symbol: string;
  timeframe: string;
  resolution_minutes: number;
  start_ms: number;
  start_price: number;
  peak_ms: number;
  peak_price: number;
  gain_pct: number;
  size_bucket: string;
  bars_to_peak: number;
  minutes_to_peak: number;
  drawdown_after_pct: number | null;
  rvol_at_start: number | null;
  volume_change_before_pct: number | null;
  range_position_at_start: number | null;
  atr_pct_at_start: number | null;
}

export interface PumpResponse {
  history: {
    symbol: string;
    timeframe: string;
    bars_examined: number;
    days_covered: number;
    resolution_minutes: number | null;
    definition: string;
    definition_fr: string;
    episodes_found: number;
    notes: string[];
    episodes: PumpEpisode[];
  };
  stats: {
    n: number;
    mean_gain_pct: number | null;
    median_gain_pct: number | null;
    largest_gain_pct: number | null;
    smallest_gain_pct: number | null;
    median_minutes_to_peak: number | null;
    mean_drawdown_after_pct: number | null;
    median_rvol_at_start: number | null;
    median_volume_change_before_pct: number | null;
    resolution_minutes: number | null;
    by_size: Record<string, number>;
    insufficient_sample: boolean;
    min_sample: number;
  };
  similarity: {
    comparable: number;
    examined: number;
    not_comparable: number;
    similarity_threshold: number;
    /** Empty below the sample floor — the API omits rates rather than greying them. */
    reached: Record<string, number>;
    median_gain_pct: number | null;
    median_minutes_to_peak: number | null;
    median_drawdown_after_pct: number | null;
    insufficient_sample: boolean;
    min_sample: number;
    notes: string[];
  };
  current_setup: {
    rvol: number | null;
    volume_change_pct: number | null;
    range_position: number | null;
    atr_pct: number | null;
  };
  data_mode: 'LIVE' | 'DEMO';
}

/** EXPLOSION_15M_SCORE — a ranking of how likely a move is inside a stated
 *  window. The only score in this project whose claim is already measured: the
 *  15m row of the horizon table is what the price actually did over exactly
 *  this window. */
export interface Explosion {
  symbol: string;
  explosion_score: number;
  /** IMMINENT / EN FORMATION / CALME / BLOQUÉ. */
  explosion_label: string;
  horizon_minutes: number;
  vetoed: boolean;
  veto_reason: string | null;
  engine_version: string;
  weights_fingerprint: string;
  components: Component[];
  why: string[];
  risks: string[];
  disclaimer: string;
}

export type Decision = 'VALIDATED' | 'REJECTED' | 'WATCHLIST' | 'ANALYSE';

/** One decision the user made, stored with the screen that produced it. */
export interface Validation {
  id: number;
  symbol: string;
  decision: Decision;
  decided_at: string | null;
  signal_timestamp_ms: number;
  price: number;
  final_score: number | null;
  explosion_score: number | null;
  discovery_score: number | null;
  pump_maturity: number | null;
  data_confidence: number | null;
  setup_state: string | null;
  verdict_level: string | null;
  engine_version: string | null;
  why: string[];
  risks: string[];
  trigger: string | null;
  invalidation: string | null;
  note: string | null;
  data_source: string;
  synthetic: boolean;
  outcome: {
    evaluated: boolean;
    horizon_minutes: number | null;
    price: number | null;
    change_pct: number | null;
    note: string | null;
  };
}

export interface ValidationsResponse {
  validations: Validation[];
  counts: Record<Decision, number>;
  total: number;
  /** Decisions taken on generated candles. Counted apart, never pooled. */
  synthetic: number;
  decisions: Decision[];
  note: string;
}
