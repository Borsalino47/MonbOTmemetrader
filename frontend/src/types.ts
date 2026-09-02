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

export type MoonshotStage =
  | 'UNKNOWN' | 'NEUTRAL' | 'DORMANT' | 'ACCUMULATION' | 'IGNITION' | 'EXPANSION' | 'EXHAUSTION';

/**
 * The ×10 reading. Every field can be null: `capacity` is null whenever no
 * valuation source is configured, and the UI must render that as "unknown"
 * rather than as a zero, which would read as "no room to run".
 */
export interface Moonshot {
  engine_version: string;
  score: number;
  label: string;
  ignition: number | null;
  headroom: number | null;
  capacity: number | null;
  stage: MoonshotStage;
  timeframe: string | null;
  target_multiple: number;
  multiple_to_window_high: number | null;
  is_candidate: boolean;
  coverage: number;
  components: Record<string, number>;
  reasons: string[];
  caveats: string[];
  unknowns: string[];
  disclaimer: string;
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
  moonshot: Moonshot | null;
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

export interface Valuation {
  symbol: string;
  market_cap_usd: number | null;
  market_cap_upper_bound_usd: number | null;
  fully_diluted_valuation_usd: number | null;
  circulating_ratio: number | null;
  ath_change_pct: number | null;
  rank: number | null;
  ambiguous_symbol: boolean;
}

export interface AssetFeatures {
  symbol: string;
  price: number;
  quote_volume_24h: number | null;
  price_change_pct_24h: number | null;
  order_book_imbalance: number | null;
  spread_bps: number | null;
  valuation: Valuation | null;
  benchmark_symbol: string | null;
  rs_vs_benchmark_pct: number | null;
  rvol_percentile_universe: number | null;
  timeframes: Record<string, TimeframeFeatures>;
}

export interface MoonshotResponse {
  meta: {
    engine_version: string;
    timeframe: string;
    target_multiple: number;
    valuation_source: string;
    matched: number;
    no_reading: string[];
    disclaimer: string;
  };
  results: {
    symbol: string;
    price: number;
    moonshot: Moonshot;
    final_score: number;
    pump_maturity: number;
    data_confidence: number;
    liquidity: LiquidityStatus;
    setup_state: SetupState;
    valuation: Valuation | null;
  }[];
}

export interface UniverseResponse {
  mode: 'volume' | 'robinhood';
  quote_asset: string;
  venue: string;
  benchmark: string;
  note: string | null;
  resolution: {
    count: number;
    symbols: string[];
    by_base: Record<string, string>;
    missing: string[];
    source: string;
    as_of: string;
    notes: string[];
  } | null;
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
  synthetic_warning: string | null;
  scan_count: number;
  consecutive_failures: number;
  scan_interval_seconds: number;
  market_regime: { trend: string; volatility: string; reference: string };
  universe: {
    mode: string;
    benchmark: string;
    rank_mode: string;
    count?: number;
    missing?: string[];
    source?: string;
    as_of?: string;
    notes?: string[];
  };
  moonshot: {
    enabled: boolean;
    engine_version: string;
    timeframe: string;
    target_multiple: number;
    valuation_source: string;
    candidates_last_scan: number;
  };
  alert_delivery: {
    channels: { channel: string; configured: boolean; missing_setting: string | null }[];
    last_results: { channel: string; delivered: number; failed: number; detail: string | null }[];
  };
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
  /** Is the radar actually scanning? Not the same as "the process is up". */
  health?: HealthVerdict;
  candle_cache: { hits: number; misses: number; entries: number; hit_rate: number | null } | null;
  server_time_ms: number;
}

export interface ScanResponse {
  meta: {
    duration_ms: number; universe_size: number; scanned: number;
    succeeded: number; failed: number; synthetic_data: boolean;
    notes: string[]; errors: Record<string, string>; matched: number;
  };
  results: ScoreRow[];
}

export interface AlertItem {
  symbol: string;
  kind: 'SETUP' | 'MOONSHOT';
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
  moonshot_score: number | null;
  moonshot_stage: string | null;
  moonshot_multiple_to_window_high: number | null;
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

// ------------------------------------------------------ ×10 axis outcomes --

export interface MoonshotPerformanceResponse {
  counts: {
    readings_journalled: number;
    pending_evaluation: number;
    settled: number;
    wins: number;
    losses: number;
    timeouts: number;
    unresolvable: number;
    candidates_journalled: number;
    reached_10x: number;
  };
  label: { config: string; definition: string; timeframe: string };
  performance: {
    overall: Bucket;
    by_score_band: Bucket[];
    by_stage: Bucket[];
    by_capacity_known: Bucket[];
    /** How far the readings actually went — the headline for this axis. */
    multiple_distribution: { at_least: number; n: number; share: number | null }[];
    best_multiple: number | null;
    median_multiple: number | null;
    label_config: string;
    return_basis: string;
    min_sample: number;
    synthetic_included: boolean;
    synthetic_count: number;
    notes: string[];
  };
}

export interface HealthVerdict {
  status: 'OK' | 'DEGRADED' | 'DOWN' | 'STARTING';
  reasons: string[];
  since_success_seconds: number | null;
}
