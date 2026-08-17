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

/** The six decisions. Internal English; the screen shows the French label. */
export type TradeAction = 'BUY' | 'HOLD' | 'WATCH' | 'REDUCE' | 'SELL' | 'AVOID';

export interface DecisionCheck {
  name: string;
  passed: boolean;
  value: number | null;
  threshold: number | null;
  unavailable: boolean;
  why: string;
}

/** One recommendation. `emoji`, `label_fr` and `tone` travel together — a
 *  decision is never rendered as a colour alone. */
export interface TradeDecision {
  symbol: string;
  action: TradeAction;
  emoji: string;
  label_fr: string;
  label_en: string;
  tone: string;
  strength: 'STANDARD' | 'STRONG' | 'VERY_STRONG' | null;
  strength_fr: string | null;
  timestamp_ms: number;
  price: number;
  reasons: string[];
  risks: string[];
  /** Prices, or null when the structure could not produce one. Never invented. */
  trigger_price: number | null;
  invalidation_price: number | null;
  checks: DecisionCheck[];
  blocking: string[];
  is_actionable: boolean;
  engine_version: string;
  weights_fingerprint: string;
  disclaimer: string;
  /** What the engine said before the noise gate, when they differ. */
  proposed?: TradeAction;
}

export interface Position {
  id: number;
  symbol: string;
  /** CEX or ROBINHOOD. The two universes are never blended (spec §4), so this
   *  is what tells a position which engines judged it. */
  chain: string;
  chain_id: number | null;
  contract_address: string | null;
  /** Robinhood baselines. Null on a Binance position; on a Robinhood one they
   *  are what its health engine measures change against. */
  entry_early: number | null;
  entry_liquidity_usd: number | null;
  entry_rug_risk: string | null;
  signal_id: number | null;
  status: 'OPEN' | 'CLOSED';
  opened_at: string | null;
  opened_ms: number;
  entry_price: number;
  actual_entry_price: number | null;
  /** Which price the returns come from. Screen prices and fills are different
   *  numbers and must never be read as the same one. */
  pnl_basis: 'actual_fill' | 'observed_price';
  amount_invested: number | null;
  quantity: number | null;
  trigger_price: number | null;
  invalidation_price: number | null;
  entry_opportunity: number | null;
  entry_explosion: number | null;
  entry_safety: number | null;
  entry_confidence: number | null;
  entry_maturity: number | null;
  entry_state: string | null;
  entry_regime: string | null;
  entry_reasons: string[];
  last_price: number | null;
  peak_price: number | null;
  trough_price: number | null;
  pnl_pct: number | null;
  drawdown_from_peak_pct: number | null;
  mfe_pct: number | null;
  mae_pct: number | null;
  health_score: number | null;
  current_decision: TradeAction | null;
  decision_changed_ms: number | null;
  closed_at: string | null;
  exit_price: number | null;
  actual_exit_price: number | null;
  realised_pnl_pct: number | null;
  close_reason: string | null;
  synthetic: boolean;
}

export interface TradeSignal {
  id: number;
  symbol: string;
  action: 'BUY' | 'SELL';
  strength: string | null;
  emitted_at: string | null;
  timestamp_ms: number;
  price: number;
  /** null = not answered yet. Never rendered as a "no". */
  taken: boolean | null;
  position_id: number | null;
  opportunity_score: number | null;
  explosion_score: number | null;
  safety_score: number | null;
  data_confidence: number | null;
  pump_maturity: number | null;
  trigger_price: number | null;
  invalidation_price: number | null;
  reasons: string[];
  risks: string[];
  synthetic: boolean;
}

export interface DecisionsResponse {
  entries: TradeDecision[];
  positions: Position[];
  awaiting_answer: TradeSignal[];
  counts: Record<TradeAction, number>;
  data_mode: 'LIVE' | 'DEMO';
  engine: { version: string; weights_fingerprint: string };
  disclaimer: string;
}

export interface PositionEvent {
  id: number;
  position_id: number;
  symbol: string;
  at: string | null;
  at_ms: number;
  decision: TradeAction;
  previous_decision: TradeAction | null;
  price: number;
  pnl_pct: number | null;
  health_score: number | null;
  reasons: string[];
  risks: string[];
}

export interface TradeBucket {
  key: string;
  n: number;
  wins: number;
  /** Null below the sample floor. Absent, not greyed out. */
  win_rate: number | null;
  mean_pct: number | null;
  median_pct: number | null;
  best_pct: number | null;
  worst_pct: number | null;
  mean_win_pct: number | null;
  mean_loss_pct: number | null;
  profit_factor: number | null;
  mean_mfe_pct: number | null;
  mean_mae_pct: number | null;
  median_minutes_held: number | null;
  insufficient_sample: boolean;
  min_sample: number;
  notes: string[];
}

export interface ResultsResponse {
  positions: { opened: number; closed: number; overall: TradeBucket; by_strength: TradeBucket[] };
  signals: {
    buy_total: number;
    taken: number;
    skipped: number;
    /** Neither taken nor skipped. Never folded into "skipped". */
    unanswered: number;
    follow_rate: number | null;
  };
  taken_vs_skipped: {
    taken: TradeBucket;
    skipped: TradeBucket;
    mean_difference_pct: number | null;
    note: string;
  };
  sell_signals: { total: number; by_horizon: Record<string, TradeBucket>; note: string };
  min_sample: number;
  notes: string[];
  data_mode: 'LIVE' | 'DEMO';
}

export type FeedState = 'PENDING' | 'VERIFIED' | 'FAILED' | 'SKIPPED_SYNTHETIC';

/** Whether the live feed has been proven live. PENDING is not a failure — it is
 *  the honest answer for the first seconds, and showing it as "not verified"
 *  would make every launch look broken for as long as the check takes. */
export interface FeedVerification {
  state: FeedState;
  emoji: string;
  label_fr: string;
  provider: string;
  verified: boolean;
  passed: number;
  total: number;
  checks: { name: string; ok: boolean; detail: string }[];
  duration_ms: number;
  checked_at_ms: number | null;
  error: string | null;
  note: string;
}

export interface StartupResponse {
  startup: Record<string, number | null | string | string[] | Record<string, string>>;
  feed_verification: FeedVerification;
  live_verified: boolean;
}

// --------------------------------------------------------------------- markets
// Two universes, never blended (spec §4). The market a row belongs to is part
// of its identity, so it travels with every payload rather than being inferred
// from a symbol that might exist on both.

export type MarketId = 'BINANCE_SPOT' | 'ROBINHOOD_CHAIN';

/** Chain verification has four states, not three: PARTIAL is reachable-and-
 *  right-chain but not live-proven, and it licenses nothing. */
export type ChainState = 'PENDING' | 'VERIFIED' | 'PARTIAL' | 'FAILED';

export interface ProviderSummary {
  id: MarketId;
  label: string;
  emoji: string;
  kind: 'cex' | 'onchain';
  chain_id?: number;
  state: string;
  state_emoji: string;
  state_label_fr: string;
  live_verified: boolean;
  data_mode?: string;
  market_data_available: boolean;
}

export interface ProvidersResponse {
  providers: ProviderSummary[];
}

export interface ChainVerification {
  state: ChainState;
  emoji: string;
  label_fr: string;
  provider: string;
  endpoint_host: string;
  chain_id: number | null;
  latest_block: number | null;
  latency_ms: number | null;
  verified: boolean;
  passed: number;
  total: number;
  checks: { name: string; ok: boolean; detail: string; core: boolean }[];
  duration_ms: number;
  checked_at_ms: number | null;
  error: string | null;
  note: string;
}

export interface RobinhoodStatus {
  provider: MarketId;
  network: string;
  chain_id: number;
  verification: ChainVerification;
  live_verified: boolean;
  /** False until a real source is integrated. The UI renders NON DISPONIBLE
   *  from this rather than an empty list, which would read as a quiet market. */
  market_data_available: boolean;
  note: string;
}

export interface RobinhoodToken {
  provider: MarketId;
  chain_id: number;
  contract_address: string;
  symbol: string | null;
  name: string | null;
  /** When this token's earliest pool opened. NOT the contract's age, which
   *  this source cannot know and which stays null until Blockscout is wired. */
  pool_age_seconds: number | null;
  age_bucket: string | null;
  token_age_seconds: number | null;
  price_usd: number | null;
  liquidity_usd: number | null;
  liquidity_is_partial: boolean;
  fdv_usd: number | null;
  market_cap_usd: number | null;
  volume_h1: number | null;
  volume_h24: number | null;
  buyers_h1: number | null;
  buy_sell_ratio_h1: number | null;
  volume_acceleration: number | null;
  pool_count: number;
  dex: string | null;
  /** Null only if the safety pass never ran; the API always attaches a report,
   *  including an "unanalysed" one. */
  safety: SafetyReport | null;
  /** True whenever a purchase is forbidden — including "not analysed". */
  hard_veto: boolean;
  /** Three separate readings of the same snapshot. They are allowed to
   *  disagree; that is why they are three objects and not one number. */
  early: EarlyScore | null;
  maturity: PumpMaturityRH | null;
  confidence: DataConfidenceRH | null;
  explosion: RobinhoodExplosionScore | null;
  /** The instruction, computed last from all four readings above. Null until
   *  the safety pass has run — undecided is not the same as refused. */
  decision: RobinhoodTradeDecision | null;
}

/** Same six decisions and six colours as the Binance engine (spec §21), from
 *  entirely different evidence. Deliberately its own type: it carries no
 *  trigger or invalidation price, because a DEX token has no measured setup. */
export interface RobinhoodTradeDecision {
  address: string;
  symbol: string | null;
  action: TradeAction;
  emoji: string;
  label_fr: string;
  tone: string;
  strength: 'STANDARD' | 'STRONG' | 'VERY_STRONG' | null;
  strength_fr: string | null;
  timestamp_ms: number;
  price_usd: number | null;
  reasons: string[];
  risks: string[];
  checks: DecisionCheck[];
  blocking: string[];
  is_actionable: boolean;
  engine_version: string;
  weights_fingerprint: string;
  disclaimer: string;
}

export interface RobinhoodExplosionScore {
  score: number;
  label: string;
  horizon_minutes: number;
  components: Record<string, number>;
  reasons: string[];
  caveats: string[];
  unavailable: string[];
  vetoed: boolean;
  veto_reason: string | null;
  version: string;
  weights_fingerprint: string;
  note: string;
}

export interface RobinhoodTokensResponse {
  state?: 'PENDING' | 'OK' | 'EMPTY' | 'FAILED';
  tokens: RobinhoodToken[];
  buckets?: { id: string; label_fr: string; count: number }[];
  pools_seen?: number;
  pages_read?: number;
  requests?: number;
  safety_requests?: number;
  safety_analysed?: number;
  vetoed?: number;
  filtered?: { illiquid: number; too_old: number; unknown_age: number; not_a_discovery: number };
  errors?: string[];
  took_ms?: number;
  at_ms?: number | null;
  coverage_note?: string;
  note?: string;
  sorted_by?: 'age' | 'early' | 'decision';
  /** Counts per decision, plus UNDECIDED as its own key: a token nobody has
   *  looked at and a token that was refused are different facts. */
  decisions?: Record<string, number>;
  decision_fingerprint?: string;
}

export type RugRisk = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL' | 'UNKNOWN';

export interface SafetyFinding {
  code: string;
  severity: 'CRITICAL' | 'MAJOR' | 'MINOR' | 'UNKNOWN';
  label_fr: string;
  detail: string;
  blocking: boolean;
}

export interface SafetyReport {
  address: string;
  /** null when the analysis is too thin or absent — never 0, which would sort
   *  an unexamined token next to a proven honeypot. */
  score: number | null;
  score_label: string;
  rug_risk: RugRisk;
  rug_emoji: string;
  rug_label_fr: string;
  hard_veto: boolean;
  blocking: SafetyFinding[];
  findings: SafetyFinding[];
  counts: Record<string, number>;
  coverage: number;
  analysed: boolean;
  engine_version: string;
  weights_fingerprint: string;
}

export type Agreement = 'AGREE' | 'DISAGREE' | 'SINGLE_SOURCE' | 'NO_DATA';

export interface FieldComparison {
  field: string;
  label_fr: string;
  /** Both values are always kept. There is deliberately no merged value: an
   *  average of two prices 40 % apart describes neither. */
  geckoterminal: number | null;
  dexscreener: number | null;
  agreement: Agreement;
  drift_pct: number | null;
}

export interface CrossCheck {
  agreement: Agreement;
  emoji: string;
  label_fr: string;
  comparisons: FieldComparison[];
  disagreements: FieldComparison[];
  caveats: string[];
  sources_seen: string[];
}

export interface TokenDetail extends RobinhoodToken {
  pools: {
    geckoterminal: Record<string, unknown>[];
    dexscreener: Record<string, unknown>[];
  };
  crosscheck: CrossCheck | null;
  errors: string[];
  requests: number;
}

export interface EarlyComponent {
  name: string;
  label_fr: string;
  points: number;
  max_points: number;
  reasons: string[];
  caveats: string[];
  /** True when the inputs were absent altogether. Scores zero either way, but
   *  only one of the two is the token's fault. */
  unavailable: boolean;
}

export interface EarlyScore {
  score: number;
  label: string;
  components: EarlyComponent[];
  why: string[];
  risks: string[];
  unavailable: string[];
  engine_version: string;
  weights_fingerprint: string;
}

export interface PumpMaturityRH {
  score: number;
  label: string;
  is_late: boolean;
  reasons: string[];
  /** False when nothing could be measured — the score is a neutral placeholder,
   *  not a finding. */
  known: boolean;
  version: string;
}

export interface DataConfidenceRH {
  score: number;
  label: string;
  present: number;
  total: number;
  missing: string[];
  version: string;
}


/** One row of one Robinhood performance table. A rate never travels without
 *  the count it came from (invariant 12). */
export interface RobinhoodBucket {
  bucket: string;
  horizon: string;
  n: number;
  success_rate: number | null;
  insufficient_sample: boolean;
  median_change_pct: number | null;
  mean_change_pct: number | null;
  best_pct: number | null;
  worst_pct: number | null;
  median_max_gain_pct: number | null;
  median_max_drawdown_pct: number | null;
}

export interface RobinhoodPerformanceResponse {
  counts: {
    decisions: number;
    graded_windows: number;
    unresolvable_windows: number;
    by_action: Record<string, number>;
  };
  costs: { round_trip_pct: number; note: string };
  last_run: Record<string, unknown> | null;
  performance: {
    market: string;
    chain_id: number;
    horizons: string[];
    overall: RobinhoodBucket[];
    by_action: RobinhoodBucket[];
    by_early_band: RobinhoodBucket[];
    by_explosion_band: RobinhoodBucket[];
    by_rug_risk: RobinhoodBucket[];
    by_age_bucket: RobinhoodBucket[];
    explosion_claim_horizon: string;
    return_basis: string;
    min_sample: number;
    notes: string[];
  };
}
