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

    market_data: Literal["binance", "fixture"] = "binance"
    binance_base_url: str = "https://api.binance.com"
    # Binance publishes a market-data-only mirror that carries the same public
    # endpoints; useful when the main host is geo-restricted.
    binance_fallback_base_url: str = "https://data-api.binance.vision"
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
    scanner: ScannerSettings = Field(default_factory=ScannerSettings)
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
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
