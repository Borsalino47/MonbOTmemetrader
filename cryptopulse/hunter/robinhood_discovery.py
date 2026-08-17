"""Which tokens started trading on Robinhood Chain, and how long ago.

This is the discovery stage, and it is deliberately opinion-free: it finds
tokens, dates them, groups their pools and ranks them on figures the indexer
actually returned. No score, no threshold-based verdict, no BUY. Those are
later phases with their own versions and fingerprints, and mixing them in here
would make it impossible to tell later whether a bad call came from the search
or from the scoring.

THREE THINGS IT IS CAREFUL ABOUT

*Pool age is not token age.* `pool_created_at` says when this pool opened, not
when the contract was deployed. They differ when a token migrates or opens a
second pool, and calling the first one "token age" would be a quiet lie. The
field is `pool_age_seconds` and the token-age field stays None until a source
that actually knows it (Blockscout) is wired in.

*A token is its contract address.* Symbols repeat, deliberately: a copy of a
running token with the same ticker is the oldest trick on a new chain. Grouping
is by address; the symbol is a label shown to the user, never an identity.

*The window is not a census.* The API returns 20 pools a page and this reads a
couple of pages, so the report states how many pools it looked at. On a chain
minting thousands of tokens a day, presenting that as "the new tokens" would
overstate it, and the count is what stops it reading that way.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.logging import get_logger
from cryptopulse.providers.geckoterminal import GeckoTerminalClient, PoolSnapshot
from cryptopulse.providers.robinhood import ROBINHOOD_CHAIN_ID, ROBINHOOD_PROVIDER_ID

log = get_logger("hunter.robinhood")

__all__ = ["AGE_BUCKETS", "TokenCandidate", "RobinhoodDiscoveryReport", "discover_new_tokens"]

# (id, French label, upper bound in seconds). Ordered tightest first so a token
# falls into the narrowest bucket that contains it.
AGE_BUCKETS: tuple[tuple[str, str, float], ...] = (
    ("lt_15m", "< 15 min", 15 * 60),
    ("lt_30m", "< 30 min", 30 * 60),
    ("lt_1h", "< 1 h", 60 * 60),
    ("lt_6h", "< 6 h", 6 * 60 * 60),
    ("lt_24h", "< 24 h", 24 * 60 * 60),
)


def bucket_for(age_seconds: float | None) -> str | None:
    """The narrowest bucket containing this age, or None if unknown/older.

    None for an unknown age is the point: a token whose pool creation time the
    indexer did not return must not be filed under "< 15 min" by default.
    """
    if age_seconds is None:
        return None
    for key, _, limit in AGE_BUCKETS:
        if age_seconds < limit:
            return key
    return None


@dataclass(slots=True)
class TokenCandidate:
    """One token, with every pool of it this search saw."""

    address: str
    symbol: str | None
    name: str | None
    pools: list[PoolSnapshot] = field(default_factory=list)

    # Filled by `_finalise`, all optional by construction.
    pool_age_seconds: float | None = None
    price_usd: float | None = None
    liquidity_usd: float | None = None
    liquidity_is_partial: bool = False
    fdv_usd: float | None = None
    market_cap_usd: float | None = None

    @property
    def primary_pool(self) -> PoolSnapshot | None:
        """The deepest pool, which is the one a trade would actually touch.

        Pools with unknown liquidity cannot be compared, so they are only used
        when nothing else is available.
        """
        known = [p for p in self.pools if p.liquidity_usd is not None]
        if known:
            return max(known, key=lambda p: p.liquidity_usd or 0.0)
        return self.pools[0] if self.pools else None

    def _sum(self, attr: str, interval: str) -> float | None:
        """Sum one interval across pools, or None if no pool reported it."""
        values = [getattr(p, attr).get(interval) for p in self.pools]
        present = [v for v in values if v is not None]
        return sum(present) if present else None

    @property
    def volume_h1(self) -> float | None:
        return self._sum("volume_usd", "h1")

    @property
    def volume_h24(self) -> float | None:
        return self._sum("volume_usd", "h24")

    @property
    def buyers_h1(self) -> float | None:
        return self._sum("buyers", "h1")

    @property
    def buy_sell_ratio_h1(self) -> float | None:
        """Buys per sell over the last hour. None when there were no sells:
        a ratio with a zero denominator is undefined, not infinite."""
        buys = self._sum("buys", "h1")
        sells = self._sum("sells", "h1")
        if buys is None or sells is None or sells == 0:
            return None
        return buys / sells

    @property
    def volume_acceleration(self) -> float | None:
        """Is the last five minutes hotter than the last hour has been?

        `(volume_m5 * 12) / volume_h1`: above 1 means the current rate exceeds
        the hourly average, which is arithmetic on two returned figures rather
        than a prediction. None whenever either side is missing or the hour is
        empty — a token with no hourly volume has no rate to exceed.
        """
        m5 = self._sum("volume_usd", "m5")
        h1 = self._sum("volume_usd", "h1")
        if m5 is None or h1 is None or h1 <= 0:
            return None
        return (m5 * 12.0) / h1

    @property
    def age_bucket(self) -> str | None:
        return bucket_for(self.pool_age_seconds)

    def to_dict(self) -> dict:
        pool = self.primary_pool
        return {
            # Identity, always carried together (spec §4).
            "provider": ROBINHOOD_PROVIDER_ID,
            "chain_id": ROBINHOOD_CHAIN_ID,
            "contract_address": self.address,
            "symbol": self.symbol,
            "name": self.name,
            # Age. Pool age is measured; token age is not knowable from this
            # source and stays null rather than borrowing the pool's.
            "pool_age_seconds": self.pool_age_seconds,
            "age_bucket": self.age_bucket,
            "token_age_seconds": None,
            "price_usd": self.price_usd,
            "liquidity_usd": self.liquidity_usd,
            "liquidity_is_partial": self.liquidity_is_partial,
            "fdv_usd": self.fdv_usd,
            "market_cap_usd": self.market_cap_usd,
            "volume_h1": self.volume_h1,
            "volume_h24": self.volume_h24,
            "buyers_h1": self.buyers_h1,
            "buy_sell_ratio_h1": self.buy_sell_ratio_h1,
            "volume_acceleration": self.volume_acceleration,
            "pool_count": len(self.pools),
            "primary_pool": pool.to_dict() if pool else None,
            "dex": pool.dex if pool else None,
        }


@dataclass(slots=True)
class RobinhoodDiscoveryReport:
    """What one search saw, and what it cost."""

    candidates: list[TokenCandidate] = field(default_factory=list)
    pools_seen: int = 0
    pages_read: int = 0
    requests: int = 0
    filtered_illiquid: int = 0
    filtered_too_old: int = 0
    filtered_unknown_age: int = 0
    filtered_not_a_discovery: int = 0
    errors: list[str] = field(default_factory=list)
    took_ms: int = 0
    at_ms: int | None = None

    def by_bucket(self) -> dict[str, int]:
        counts = {key: 0 for key, _, _ in AGE_BUCKETS}
        for c in self.candidates:
            b = c.age_bucket
            if b:
                counts[b] += 1
        return counts

    def to_dict(self) -> dict:
        return {
            "provider": ROBINHOOD_PROVIDER_ID,
            "chain_id": ROBINHOOD_CHAIN_ID,
            "tokens": [c.to_dict() for c in self.candidates],
            "buckets": [
                {"id": key, "label_fr": label, "count": self.by_bucket()[key]}
                for key, label, _ in AGE_BUCKETS
            ],
            "pools_seen": self.pools_seen,
            "pages_read": self.pages_read,
            "requests": self.requests,
            "filtered": {
                "illiquid": self.filtered_illiquid,
                "too_old": self.filtered_too_old,
                "unknown_age": self.filtered_unknown_age,
                "not_a_discovery": self.filtered_not_a_discovery,
            },
            "errors": self.errors,
            "took_ms": self.took_ms,
            "at_ms": self.at_ms,
            # Said out loud so the list is never read as the whole chain.
            "coverage_note": (
                f"Fenêtre sur les {self.pools_seen} pools les plus récents indexés "
                f"({self.pages_read} page(s), {self.requests} requête(s)). "
                "Ce n'est pas un recensement de la chaîne."
            ),
        }


async def discover_new_tokens(
    client: GeckoTerminalClient,
    settings: RobinhoodSettings,
    *,
    clock: Clock = SYSTEM_CLOCK,
    max_age_hours: float | None = None,
    min_liquidity_usd: float | None = None,
) -> RobinhoodDiscoveryReport:
    """Read the newest pools, group them by token, date them. Never raises."""
    import time

    started = time.perf_counter()
    now_ms = clock.now_ms()
    max_age_s = (max_age_hours if max_age_hours is not None else settings.discovery_max_age_hours) * 3600.0
    min_liq = min_liquidity_usd if min_liquidity_usd is not None else settings.discovery_min_liquidity_usd

    report = RobinhoodDiscoveryReport(at_ms=now_ms)
    pools: list[PoolSnapshot] = []

    for page in range(1, max(1, settings.discovery_pages) + 1):
        try:
            batch = await client.new_pools(page=page)
            report.requests += 1
            report.pages_read += 1
            pools.extend(batch)
            if not batch:
                break  # the indexer has no more pages for this network
        except Exception as exc:
            # One failed page must not lose the pages that worked, exactly as
            # one failing symbol never stops a scan.
            report.requests += 1
            report.errors.append(f"page {page}: {type(exc).__name__}")
            log.warning("robinhood_discovery_page_failed", page=page, error=type(exc).__name__)
            break

    report.pools_seen = len(pools)

    grouped: dict[str, TokenCandidate] = {}
    for pool in pools:
        # A pool between two established quote assets (WETH/USDC and friends)
        # contains no new token. Without this it would be listed as a discovery
        # named after whichever side happened to be `base_token`.
        if not pool.is_discovery:
            report.filtered_not_a_discovery += 1
            continue
        token = pool.token
        if not token.address:
            continue
        age = pool.age_seconds(now_ms=now_ms)
        if age is None:
            report.filtered_unknown_age += 1
            continue
        if age > max_age_s:
            report.filtered_too_old += 1
            continue
        if pool.liquidity_usd is not None and pool.liquidity_usd < min_liq:
            report.filtered_illiquid += 1
            continue

        key = token.address.lower()
        candidate = grouped.get(key)
        if candidate is None:
            candidate = TokenCandidate(address=token.address, symbol=token.symbol, name=token.name)
            grouped[key] = candidate
        candidate.pools.append(pool)

    for candidate in grouped.values():
        _finalise(candidate, now_ms=now_ms)

    # Newest first: this screen exists to answer "what just appeared". Unknown
    # ages were dropped above, so every remaining candidate has one.
    report.candidates = sorted(
        grouped.values(), key=lambda c: c.pool_age_seconds if c.pool_age_seconds is not None else float("inf")
    )
    report.took_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "robinhood_discovery",
        tokens=len(report.candidates), pools=report.pools_seen,
        requests=report.requests, ms=report.took_ms,
    )
    return report


def _finalise(candidate: TokenCandidate, *, now_ms: int) -> None:
    ages = [p.age_seconds(now_ms=now_ms) for p in candidate.pools]
    known_ages = [a for a in ages if a is not None]
    # The oldest pool is the token's earliest appearance here — a second pool
    # opened minutes ago does not make a day-old token new again.
    candidate.pool_age_seconds = max(known_ages) if known_ages else None

    liquidity = [p.liquidity_usd for p in candidate.pools]
    known_liq = [v for v in liquidity if v is not None]
    candidate.liquidity_usd = sum(known_liq) if known_liq else None
    # A total assembled from some of the pools is not the token's liquidity, and
    # the flag is what stops it being read as one.
    candidate.liquidity_is_partial = len(known_liq) != len(liquidity)

    pool = candidate.primary_pool
    if pool is not None:
        candidate.price_usd = pool.token_price_usd
        candidate.fdv_usd = pool.fdv_usd
        candidate.market_cap_usd = pool.market_cap_usd
        # A pool that names the token better than the one we happened to see
        # first: prefer a present label over a missing one.
        candidate.symbol = candidate.symbol or pool.token.symbol
        candidate.name = candidate.name or pool.token.name
