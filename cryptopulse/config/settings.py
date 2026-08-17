"""Configuration. Everything tunable lives here and is overridable from .env.

No secret is ever hardcoded. `CryptoPulseSettings` is the single source of truth;
modules receive the settings object rather than reading os.environ themselves.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from cryptopulse.core.types import Timeframe


class ProviderSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CP_PROVIDER_", env_file=".env", extra="ignore")

    market_data: Literal["binance", "kraken", "fixture"] = "binance"
    binance_base_url: str = "https://api.binance.com"
    # Binance publishes a market-data-only mirror that carries the same public
    # endpoints; useful when the main host is geo-restricted.
    binance_fallback_base_url: str = "https://data-api.binance.vision"
    # Kraken: reachable in jurisdictions where Binance returns 451, and an
    # independent quote source rather than a mirror.
    kraken_base_url: str = "https://api.kraken.com"

    # There is deliberately no api_key / api_secret field here. The scanner reads
    # only public market-data endpoints, which require no account, so a key field
    # would accept a credential that nothing could ever use — pure downside, and
    # a standing invitation to paste a secret into a file that might get
    # committed. `extra="ignore"` means an existing CP_PROVIDER_BINANCE_API_KEY
    # in someone's .env is silently ignored rather than crashing them.
    # If an authenticated module is ever added, it introduces its own settings
    # class, so the credential surface arrives with the feature that needs it.

    # Closed candles are immutable, so caching them costs no freshness at all.
    # Measured saving on a 60s loop: 92.8 % of kline requests. Disable only to
    # diagnose the cache itself — `doctor` bypasses it regardless.
    candle_cache: bool = True
    candle_cache_max_entries: int = 600

    # Conservative default: Binance documents a 6000 request-weight/minute IP
    # budget for spot REST. We target a fraction of it so a bug cannot get the
    # deployment IP banned.
    request_weight_per_minute: int = 2400
    max_concurrent_requests: int = 8
    request_timeout_seconds: float = 15.0
    max_retries: int = 3
    retry_base_delay_seconds: float = 0.5

    circuit_failure_threshold: int = 5
    circuit_reset_seconds: float = 60.0


class RobinhoodSettings(BaseSettings):
    """Robinhood Chain (the EVM L2, chain id 4663) — a separate market universe.

    Deliberately its own section rather than more fields on ProviderSettings:
    the two universes must never share a knob by accident, and every value here
    is meaningless to Binance. The RPC URL is a setting because the official
    public endpoint is rate-limited and documented as not-for-production; a
    user moving to Alchemy/QuickNode/dRPC changes one line of .env, never code.
    """

    model_config = SettingsConfigDict(env_prefix="CP_ROBINHOOD_", env_file=".env", extra="ignore")

    # Official public RPC. Rate-limited; fine for the doctor and light polling.
    rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    # Optional second endpoint (Alchemy, QuickNode, dRPC, ...). Failover only —
    # never a silent primary, so a misconfigured URL is noticed.
    rpc_fallback_url: str | None = None

    # Conservative: the public endpoint's exact budget is not published, so we
    # stay far below any plausible limit. ~5 req/s average.
    rpc_weight_per_minute: int = 300
    request_timeout_seconds: float = 10.0
    max_retries: int = 2
    retry_base_delay_seconds: float = 0.5

    # Optional known contract for the doctor's eth_getCode check ("si
    # disponible" in the spec). No address is hardcoded here because none could
    # be verified from the build environment; set one once confirmed on the
    # explorer and the doctor starts checking it.
    known_contract_address: str | None = None

    # How far a "latest" block timestamp may lag the local clock before the
    # chain view is considered incoherent. Generous: covers clock skew without
    # masking a stalled RPC serving hours-old state.
    max_block_lag_seconds: float = 120.0
    # A block from the future beyond this margin means a broken clock somewhere.
    max_block_future_seconds: float = 30.0

    # --- token discovery (GeckoTerminal) ------------------------------------ #
    # The raw RPC cannot answer "which tokens are new and trading": that needs
    # an indexer that has already decoded pool creations and swaps. GeckoTerminal
    # indexes Robinhood Chain under the network id below and is free.
    geckoterminal_base_url: str = "https://api.geckoterminal.com/api/v2"
    geckoterminal_network: str = "robinhood"
    # Published public limit is 30 calls/min. We sit under it rather than on it:
    # a 429 costs the whole cycle, and the discovery loop is not urgent enough
    # to be worth the risk.
    geckoterminal_calls_per_minute: int = 24
    # Each page is 20 pools. Two pages is 40 of the newest pools per cycle,
    # which on a chain minting thousands of tokens a day is a window, not a
    # census — and the report says so rather than implying completeness.
    discovery_pages: int = 2
    # Anything older than this is not "new" for the purpose of this screen.
    discovery_max_age_hours: float = 24.0
    # Below this the pool is noise: a token with a few dollars of liquidity
    # cannot be entered or exited, whatever its numbers look like.
    discovery_min_liquidity_usd: float = 1_000.0

    # --- cross-check (DexScreener) ------------------------------------------- #
    # A second, independent index of the same pools. Its purpose is not more
    # data but disagreement: two sources differing about a price is the one
    # thing neither can tell you on its own.
    dexscreener_base_url: str = "https://api.dexscreener.com"
    dexscreener_chain: str = "robinhood"
    # Published limit on this endpoint is 300/min; we use a fraction of it.
    dexscreener_calls_per_minute: int = 120
    # How far the two sources may differ before the figure is flagged. Generous:
    # they sample at slightly different moments and a real mapping error is off
    # by orders of magnitude, not by a few percent.
    crosscheck_max_price_drift_pct: float = 5.0
    crosscheck_max_liquidity_drift_pct: float = 25.0

    # --- safety (GoPlus) ----------------------------------------------------- #
    goplus_base_url: str = "https://api.gopluslabs.io"
    goplus_calls_per_minute: int = 24
    # One request covers many tokens, which is what makes safety affordable on
    # every discovery rather than on a hand-picked few.
    goplus_batch_size: int = 30
    # Below this share of the security checks, the analysis is too thin to
    # authorise anything. It does not make a token bad — it makes it unknown,
    # and unknown never becomes a BUY (spec §53).
    safety_min_coverage: float = 0.5
    # Taxes above these are treated as hostile rather than merely expensive.
    safety_max_buy_tax: float = 10.0
    safety_max_sell_tax: float = 10.0
    # Concentration at which a single exit moves the price more than the market.
    safety_max_top10_percent: float = 60.0
    safety_max_creator_percent: float = 20.0
    # Below this share of LP locked or burned, the floor can be removed.
    safety_min_lp_locked_percent: float = 50.0

    # --- trade decision (ROBINHOOD_TRADE_DECISION_V1) ------------------------ #
    # Deliberately its own set rather than reusing the Binance floors: the
    # inputs are different objects measuring different things, and a token
    # minted twenty minutes ago is not judged on the same evidence as a
    # large-cap in a retest. Every one of these goes into the engine's weights
    # fingerprint, so a decision taken today is never reread under new floors.
    #
    # Higher than the Binance equivalents on purpose. A new token on a DEX has
    # a worse downside than a listed pair — the exit can disappear — so the bar
    # for spending money on one is set above, not level with, the CEX bar.
    buy_min_early: float = 75.0
    buy_min_explosion: float = 80.0
    buy_min_confidence: float = 70.0
    buy_min_safety: float = 85.0
    buy_max_maturity: float = 40.0
    # An absolute floor in dollars, not a rank: on a DEX the question is
    # whether a position can be closed at all, and that is a depth in dollars.
    buy_min_liquidity_usd: float = 25_000.0
    # Under this, a pool is not thin — it is a trap. Treated as a hard veto
    # rather than a failed floor, same rule as liquidity DANGEROUS on Binance.
    danger_liquidity_usd: float = 2_000.0
    # HIGH, CRITICAL and UNKNOWN already veto inside ROBINHOOD_SAFETY_V1. This
    # is what decides whether MEDIUM is tolerable for a purchase.
    buy_max_rug_risk: str = "LOW"

    # --- position watcher (Robinhood) ---------------------------------------- #
    # Bounded because the pool lookup is one request per held token: the indexer
    # has no token->pools batch endpoint, so an unbounded list would turn a long
    # portfolio into a rate-limit ban.
    max_tracked_positions: int = 20
    position_watch_interval_seconds: int = 30

    # SURVEILLER, for tokens that are not buys but are worth a second look.
    watch_min_early: float = 55.0
    watch_min_confidence: float = 40.0


class ScannerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CP_SCAN_", env_file=".env", extra="ignore")

    quote_asset: str = "USDT"
    max_symbols: int = 120
    scan_interval_seconds: int = 60

    # Universe pre-filter, applied before any network cost is spent on klines.
    min_quote_volume_24h: float = 3_000_000.0
    always_include: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"])
    exclude_patterns: list[str] = Field(default_factory=lambda: ["UP", "DOWN", "BULL", "BEAR"])
    exclude_stable_bases: list[str] = Field(
        default_factory=lambda: ["USDC", "FDUSD", "TUSD", "DAI", "BUSD", "EUR", "USDP", "USD1"]
    )

    primary_timeframe: Timeframe = Timeframe.M5
    timeframes: list[Timeframe] = Field(
        default_factory=lambda: [Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4]
    )
    candles_per_timeframe: int = 300
    min_candles_required: int = 60

    # A 5m candle closed more than this long ago means the feed is behind.
    stale_after_seconds: int = 300

    @field_validator("timeframes", mode="before")
    @classmethod
    def _parse_tfs(cls, v):
        if isinstance(v, str):
            return [Timeframe.parse(x.strip()) for x in v.split(",") if x.strip()]
        return v

    @field_validator("always_include", "exclude_patterns", "exclude_stable_bases", mode="before")
    @classmethod
    def _parse_list(cls, v):
        if isinstance(v, str):
            return [x.strip().upper() for x in v.split(",") if x.strip()]
        return v


class ScoringSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CP_SCORE_", env_file=".env", extra="ignore")

    engine_version: str = "SCORE_ENGINE_V1"

    # Component maxima. Sum must be 100; enforced by the engine at import time.
    w_volume: float = 20.0
    w_momentum: float = 15.0
    w_structure: float = 15.0
    w_breakout: float = 15.0
    w_volatility: float = 10.0
    w_orderflow: float = 10.0
    w_mtf: float = 10.0
    w_liquidity: float = 5.0

    # Setup-state thresholds on FINAL_SCORE.
    threshold_observe: float = 35.0
    threshold_watch: float = 50.0
    threshold_armed: float = 65.0

    # Pump maturity above this is treated as "late" and penalised hard.
    pump_maturity_late: float = 70.0
    pump_maturity_max_for_premium: float = 65.0

    # Breakout / retest geometry, in ATR units.
    breakout_confirm_atr: float = 0.15
    armed_max_distance_atr: float = 1.0
    retest_band_atr: float = 0.5


class RiskSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CP_RISK_", env_file=".env", extra="ignore")

    # Liquidity gate thresholds, quote-currency 24h volume.
    liq_excellent_volume: float = 100_000_000.0
    liq_good_volume: float = 20_000_000.0
    liq_acceptable_volume: float = 3_000_000.0
    liq_poor_volume: float = 500_000.0

    # Spread thresholds in basis points (order book, when available).
    spread_excellent_bps: float = 5.0
    spread_good_bps: float = 15.0
    spread_acceptable_bps: float = 40.0
    spread_dangerous_bps: float = 120.0

    max_risk_penalty: float = 45.0
    safety_hard_veto_floor: float = 35.0


class AlertSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CP_ALERT_", env_file=".env", extra="ignore")

    enabled: bool = True
    min_score_info: float = 50.0
    min_score_watch: float = 60.0
    min_score_high: float = 72.0
    min_score_critical: float = 82.0
    min_score_acceleration: float = 6.0
    cooldown_seconds: int = 1800
    max_alerts_per_scan: int = 12
    webhook_url: str | None = None

    # Android notifications via Termux:API. On by default because it costs
    # nothing where it is unavailable — `build_notifier` returns a channel that
    # states why rather than one that fails at send time.
    android_notifications: bool = True
    android_notification_binary: str = "termux-notification"
    # Levels quiet enough that a phone should not be interrupted for them.
    android_min_level: str = "HIGH"


class TradeSettings(BaseSettings):
    """Thresholds for the decision engine. Every one is a stated hypothesis.

    None of these were fitted. They are a starting point chosen from trading
    reasoning, and they are settings rather than constants precisely because the
    journal is meant to replace them with measured values later. Changing any of
    them changes the engine's weights fingerprint, so past decisions are never
    reinterpreted under new rules.
    """

    model_config = SettingsConfigDict(env_prefix="CP_TRADE_", env_file=".env", extra="ignore")

    # --- what it takes to say ACHETER --------------------------------------- #
    buy_min_opportunity: float = 75.0
    buy_min_explosion: float = 80.0
    buy_min_discovery: float = 70.0
    buy_min_confidence: float = 85.0
    buy_min_safety: float = 80.0
    buy_max_pump_maturity: float = 40.0
    buy_min_liquidity: str = "ACCEPTABLE"
    # A setup that has not triggered is a WATCH, never a BUY.
    buy_requires_triggered_setup: bool = True

    # --- what it takes to say SURVEILLER rather than NE PAS ACHETER ---------- #
    watch_min_opportunity: float = 55.0
    watch_min_confidence: float = 50.0

    # --- anti-noise ---------------------------------------------------------- #
    # Consecutive cycles a decision must repeat before it takes effect. Applies
    # to the decisions that cost something to act on; WATCH and HOLD are free.
    confirmations_required: int = 2
    # After a decision changes, how long before it may change again. Bypassed by
    # a broken invalidation or a hard safety veto — see trading/hysteresis.py.
    min_seconds_between_changes: int = 300

    # --- position watcher ----------------------------------------------------- #
    position_watch_interval_seconds: int = 15
    max_tracked_positions: int = 20


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CP_DB_", env_file=".env", extra="ignore")

    url: str = "sqlite:///data/cryptopulse.db"
    echo: bool = False
    retention_days: int = 90


class CryptoPulseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CP_", env_file=".env", extra="ignore")

    app_name: str = "CRYPTO PULSE AI"
    environment: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"
    log_json: bool = False

    # Hard safety switch. V1 never places an order; this flag exists so that any
    # future execution module has to be turned on deliberately.
    paper_mode: bool = True

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])

    providers: ProviderSettings = Field(default_factory=ProviderSettings)
    robinhood: RobinhoodSettings = Field(default_factory=RobinhoodSettings)
    scanner: ScannerSettings = Field(default_factory=ScannerSettings)
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    trade: TradeSettings = Field(default_factory=TradeSettings)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_origins(cls, v):
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v


@lru_cache
def get_settings() -> CryptoPulseSettings:
    return CryptoPulseSettings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
