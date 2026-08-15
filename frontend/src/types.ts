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
