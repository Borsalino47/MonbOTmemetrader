"""Configuration. Everything tunable lives here and is overridable from .env.

No secret is ever hardcoded. `CryptoPulseSettings` is the single source of truth;
modules receive the settings object rather than reading os.environ themselves.

ONE NON-OBVIOUS SETTING: `enable_decoding=False`

Every class here that holds a list field turns pydantic-settings' complex-value
decoding off. By default it JSON-decodes an environment variable before any
validator sees it, so `CP_SCAN_ALWAYS_INCLUDE=BTCUSDT,ETHUSDT` — the form
documented in .env.example — raises a JSONDecodeError at import and takes the
whole process down before a single line of scanner code runs. Turning decoding
off hands the raw string to the `mode="before"` validators below, which is what
they were always written to receive.
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

    # Valuation (market cap) enrichment. Deliberately OFF by default: it is a
    # second network dependency, and the moonshot layer reports an unknown market
    # cap as unknown rather than degrading quietly, so "none" costs honesty
    # nothing. Turn it on to get the capacity reading.
    valuation: Literal["none", "coingecko"] = "none"
    coingecko_base_url: str = "https://api.coingecko.com"
    coingecko_api_key: str | None = None
    # Pages of 250 assets ranked by market cap. Two pages = the top 500, which is
    # also what makes "not in the ranking" a usable upper bound on the cap.
    valuation_pages: int = 2
    valuation_ttl_seconds: int = 3600

    binance_base_url: str = "https://api.binance.com"
    # Binance publishes a market-data-only mirror that carries the same public
    # endpoints; useful when the main host is geo-restricted.
    binance_fallback_base_url: str = "https://data-api.binance.vision"
    # Kraken: reachable in jurisdictions where Binance returns 451, and an
    # independent quote source rather than a mirror.
    kraken_base_url: str = "https://api.kraken.com"

    binance_api_key: str | None = None
    binance_api_secret: str | None = None

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

    fixture_dir: str = "data/fixtures"


class ScannerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CP_SCAN_", env_file=".env", extra="ignore", enable_decoding=False
    )

    quote_asset: str = "USDT"
    max_symbols: int = 120
    scan_interval_seconds: int = 60

    # Which assets a scan may look at.
    #   volume     — the venue's most liquid pairs (the V1 behaviour)
    #   robinhood  — only assets believed tradable on Robinhood Crypto
    # `robinhood` is what the radar ships with; a signal on something you cannot
    # buy is noise. See universe/robinhood.py for what that list is and is not.
    universe: Literal["volume", "robinhood"] = "volume"
    robinhood_file: str | None = None
    robinhood_extra: list[str] = Field(default_factory=list)
    robinhood_exclude: list[str] = Field(default_factory=list)

    # How the ranked table is ordered.
    #   setup     — best trade setup right now (V1 behaviour)
    #   moonshot  — best candidate for a large multiple
    #   blend     — setup quality weighted by moonshot score
    rank_mode: Literal["setup", "moonshot", "blend"] = "setup"

    # Benchmark for the market regime and for relative strength. Empty means
    # "ask the provider for a symbol it is certain to carry", which is what makes
    # this work on Kraken (XBT) as well as Binance (BTC).
    benchmark_symbol: str = ""

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

    @field_validator(
        "always_include",
        "exclude_patterns",
        "exclude_stable_bases",
        "robinhood_extra",
        "robinhood_exclude",
        mode="before",
    )
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


class MoonshotSettings(BaseSettings):
    """The ×10 layer. Every number here is a hypothesis — see scoring/moonshot.py."""

    model_config = SettingsConfigDict(env_prefix="CP_MOON_", env_file=".env", extra="ignore")

    enabled: bool = True

    # A base that takes months to build is invisible on an intraday chart, so the
    # reading is taken on the daily and refuses to run below 4h.
    timeframe: Timeframe = Timeframe.D1
    candles: int = 400
    min_bars: int = 80

    # The multiple being hunted. Everything scales off this, so setting it to 5
    # or 20 re-tunes the whole layer coherently rather than just renaming it.
    target_multiple: float = 10.0

    # Composite weights. Renormalised over whatever is available, so a missing
    # market cap redistributes rather than scoring zero. NOT fitted.
    w_ignition: float = 55.0
    w_headroom: float = 20.0
    w_capacity: float = 25.0

    # Market cap ends of the capacity scale, in USD. At 20M a ×10 implies 200M,
    # which happens somewhere most weeks; at 20B it implies 200B, which almost
    # nothing has ever reached.
    cap_full_capacity_usd: float = 20_000_000.0
    cap_no_capacity_usd: float = 20_000_000_000.0

    # Stage thresholds.
    ignition_threshold: float = 60.0
    exhaustion_maturity: float = 70.0
    # "The markup is already under way", measured two ways because either alone
    # misses cases: a fast run shows up in the 12-bar return, a slow grinding one
    # only in how far price has pulled away from its own mean.
    expansion_roc_pct: float = 60.0  # over 12 bars of the moonshot timeframe
    expansion_extension_atr: float = 4.0  # ATR above the EMA50 of that timeframe
    min_base_bars: int = 20

    # Caps applied once the move is no longer early. A late entry that still
    # scores 90 is the single most expensive thing a radar can show you.
    expansion_score_cap: float = 70.0
    exhaustion_score_cap: float = 45.0

    @field_validator("timeframe", mode="before")
    @classmethod
    def _parse_tf(cls, v):
        return Timeframe.parse(v) if isinstance(v, str) else v


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
    model_config = SettingsConfigDict(
        env_prefix="CP_ALERT_", env_file=".env", extra="ignore", enable_decoding=False
    )

    enabled: bool = True
    min_score_info: float = 50.0
    min_score_watch: float = 60.0
    min_score_high: float = 72.0
    min_score_critical: float = 82.0
    min_score_acceleration: float = 6.0
    cooldown_seconds: int = 1800
    max_alerts_per_scan: int = 12

    # Moonshot alerts are a different animal from setup alerts: a base does not
    # change between two scans, so they get their own threshold and a cooldown
    # measured in hours rather than minutes.
    min_score_moonshot: float = 68.0
    moonshot_cooldown_seconds: int = 21_600

    # Delivery. An alert nobody sees is not an alert — this is what makes the
    # radar autonomous rather than something you have to sit and watch.
    # Any of: console, jsonl, webhook, telegram, discord.
    channels: list[str] = Field(default_factory=lambda: ["console"])
    jsonl_path: str = "data/alerts.jsonl"
    webhook_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    discord_webhook_url: str | None = None
    delivery_timeout_seconds: float = 10.0

    @field_validator("channels", mode="before")
    @classmethod
    def _parse_channels(cls, v):
        if isinstance(v, str):
            return [x.strip().lower() for x in v.split(",") if x.strip()]
        return v


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CP_DB_", env_file=".env", extra="ignore")

    url: str = "sqlite:///data/cryptopulse.db"
    echo: bool = False
    retention_days: int = 90


class CryptoPulseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CP_", env_file=".env", extra="ignore", enable_decoding=False)

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
    scanner: ScannerSettings = Field(default_factory=ScannerSettings)
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
    moonshot: MoonshotSettings = Field(default_factory=MoonshotSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)

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
