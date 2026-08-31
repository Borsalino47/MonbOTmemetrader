# CLAUDE.md — CRYPTO PULSE AI

Working notes for anyone (human or agent) continuing this project. Read this
before changing code; it records the decisions and the reasons, not just the
layout.

---

## 1. What this is

A crypto scanner that tries to answer **"which asset is changing behaviour right
now, before the rest of the market notices?"** — not "what went up the most
today".

Pipeline: `DATA → SCAN → FILTER → ENRICH → SCORE → VALIDATE → RANK → ALERT → BACKTEST`.

It now does this on **two horizons at once**, and keeps them apart on purpose:

* the **opportunity score** (`SCORE_ENGINE_V1`) — the next few hours, on the
  primary intraday timeframe;
* the **moonshot score** (`MOONSHOT_ENGINE_V1`, §11) — the next few weeks: how
  closely an asset resembles states that have preceded *large multiples*.

Both are rankings, neither is a probability, and the second one is the less
validated of the two. A single blended number would hide exactly the distinction
that matters — a 78 in EXHAUSTION and a 78 in ACCUMULATION are opposite
situations.

The scanned universe defaults to **assets believed tradable on Robinhood Crypto**
(§12). A signal on something you cannot buy is noise.

---

## 2. Current build status

Use these five words precisely. Do not upgrade a status without doing the work
that justifies it.

| Component | Status | Notes |
|---|---|---|
| Indicators (EMA/SMA/RSI/MACD/ATR/BB/VWAP/ROC) | **TESTED** | 21 unit tests + 17 truncation-stability tests |
| No-look-ahead guarantees | **TESTED** | `tests/test_no_lookahead.py`, 24 tests |
| Market structure (swings, S/R, breakout, retest) | **TESTED** | 15 tests |
| Volume features (RVOL, acceleration) | **TESTED** | |
| Opportunity Score V1 | **TESTED** | 21 tests. Weights are a hypothesis, NOT fitted |
| Pump Maturity V1 | **TESTED** | |
| Data Confidence | **TESTED** | |
| Liquidity + Safety gates | **TESTED** | 17 tests |
| Setup state machine | **TESTED** | |
| Score memory / acceleration | **TESTED** | |
| Alert engine (dedup + cooldown) | **TESTED** | 12 tests |
| Scanner pipeline | **TESTED** | Verified against the fixture provider only |
| Database + signal journal | **TESTED** | SQLite **and PostgreSQL 16** both verified end to end |
| Liveness / watchdog / housekeeping | **TESTED** | 16 tests. `/healthz`, SYSTEM alerts, score-memory rehydration, retention |
| Backtest engine + labels + walk-forward | **TESTED** | Never run on real market data |
| FastAPI API | **TESTED** | 16 endpoint tests |
| React dashboard | **IMPLEMENTED** | Builds clean, renders, screenshotted against fixture data |
| **Binance connector** | **IMPLEMENTED — NOT LIVE VERIFIED** | Contract cross-checked vs python-binance 1.0.37; still no live round-trip. See §3 |
| Fixture (synthetic) provider | **TESTED / MOCKED** | Time-anchored, Brownian. Generates data, never market data |
| **Kraken connector** | **IMPLEMENTED — NOT LIVE VERIFIED** | 23 tests incl. 2 full-pipeline. Contract cross-checked vs ccxt 4.5.73 |
| Order book / order flow | **IMPLEMENTED — NOT LIVE VERIFIED** | Parsing tested against mocks |
| DEX scanner | **NOT IMPLEMENTED** | Interface declared in `providers/base.py` |
| DEX safety scoring | **NOT IMPLEMENTED** | `risk/safety.py:dex_safety` raises deliberately |
| Score calibration / probabilities | **NOT IMPLEMENTED** | Needs a real signal history first |
| Machine learning | **NOT IMPLEMENTED** | Deliberately deferred, see §9 |
| Outcome tracker | **TESTED** | 23 tests; run end to end on 310 signals |
| Expansion features (base age, drawdown, VCP, spring, quiet accumulation) | **TESTED** | 13 tests + truncation stability |
| Accumulation indicators (OBV / A-D / CMF) | **TESTED** | 6 unit tests + 4 no-look-ahead |
| **Moonshot / ×10 layer** (`MOONSHOT_ENGINE_V1`) | **TESTED** | 20 tests. Weights are a hypothesis, NOT fitted, and no ×10 outcome has ever been graded. See §11 |
| Relative strength + cross-sectional volume rank | **TESTED** | Computed per scan, `None` when the scan cannot |
| **Robinhood universe filter** | **IMPLEMENTED — NOT LIVE VERIFIED** | 16 tests. The *listing* is a hand-maintained snapshot, never checked against Robinhood. See §12 |
| Alert delivery (console / jsonl / webhook / telegram / discord) | **TESTED** | 11 tests incl. secret-leak and crash isolation. Network channels tested against mocks only |
| Autonomous radar loop (`cryptopulse radar`) | **TESTED** | Full cycle end to end on the fixture provider |
| **CoinGecko valuation (market cap)** | **IMPLEMENTED — NOT LIVE VERIFIED** | 10 tests against mocks. OFF by default |
| Performance analytics (`outcomes/stats.py`) | **TESTED** | Buckets + component edge, `n` on every rate |
| Schema migration (`database/migrate.py`) | **TESTED** | Additive columns **and indexes**; refuses destructive changes |
| **×10 journal + grading** (`moon_outcome_*`, `MOONSHOT_LABEL_CONFIGS`) | **TESTED** | 16 tests. The reading is now written to disk and graded on its own horizon — see §11 |
| **Candle cache** (`providers/cache.py`) | **TESTED** | 19 tests. ~57% fewer kline requests over 10 passes, ~90% on the daily |

**Test suite: 402 tests, all passing.** Run `pytest -q`.

---

## 3. The most important open item

**The Binance connector has never talked to Binance.**

This project was built in a Claude Code cloud environment whose network
allowlist refuses every exchange host. Confirmed authoritatively: a request that
bypasses the local proxy gets `HTTP 403` with `x-deny-reason: host_not_allowed`
and the body `Host not in allowlist: api.binance.com`. DNS resolves and TCP
leaves the container — the environment is *choosing* to refuse. It is not a
Binance geo-block, not DNS, and not a bug in the connector.

**The fix is an environment setting, not code.** At claude.ai/code: cloud icon
above the message box -> hover the environment -> settings -> Network access
**Custom** -> add `api.binance.com`, `data-api.binance.vision` and
`api.coingecko.com` to **Allowed domains**, keeping "Also include default list
of common package managers" checked. (CoinGecko is what supplies market cap; the
×10 layer reports capacity as unknown without it — see §11.) Then start a NEW session; a running one keeps the policy it
booted with. `cryptopulse doctor` now prints these steps itself when it detects
this exact failure. So:

* the connector was written from the long-standing public Spot API v3 contract,
  not from the current docs (which were also unreachable);
* no number the scanner has ever produced came from a real exchange.

**What has since been cross-checked.** PyPI *is* reachable from this sandbox, so
`python-binance` 1.0.37 was downloaded and read as an independent reference. It
confirms, index for index, the 12-column kline layout in `KLINE_FIELDS`; the
1000-row klines cap; the base URL and `v3`; the five endpoint paths; the
ticker/24hr field names; and the exchangeInfo filter names. That is a second
opinion, not a verification — it proves the reference library and this connector
share an understanding, not that the live API matches it today.

**One fragility it exposed and fixed.** `quoteVolume` is the liquidity gate's
primary input, and a `KeyError` on it would have dropped the whole symbol through
the parse-error path — a silent failure in the most important input in the
system. The connector now falls back to `weightedAvgPrice * volume` (an identity,
since Binance defines `weightedAvgPrice` as `quoteVolume / volume`) and tags the
provenance so the substitution is visible. With neither field the row is skipped
rather than guessed.

**Before trusting this in production, run:**

```bash
python -m cryptopulse.cli doctor
```

It performs a real round-trip against every endpoint and cross-checks the parsed
values against independent fields in the same response (low ≤ last ≤ high, OHLC
invariants, kline close vs ticker price, bid < ask). It prints `LIVE VERIFIED`
or the exact mismatch. Until that passes, the connector's status stays
IMPLEMENTED.

If `doctor` finds a mismatch, fix `providers/binance.py` only. Nothing above the
provider layer knows the shape of an exchange payload.

---

## 4. Architecture

```
cryptopulse/
  config/settings.py     Pydantic settings, all tunables, .env-driven
  core/
    types.py             Candle, OHLCVSeries, OrderBook, Provenance, Timeframe
    errors.py            DATA_UNAVAILABLE / SOURCE_UNAVAILABLE / STALE_DATA ...
    clock.py             Injectable clock (SystemClock / FrozenClock)
    logging.py           structlog, JSON in prod
  providers/
    base.py              MarketDataProvider / OrderBookProvider / DEXProvider ABCs
    http.py              Weighted rate limiter, retry+jitter, circuit breaker
    binance.py           Binance Spot public REST      <- NOT LIVE VERIFIED
    kraken.py            Kraken public REST, 2nd venue <- NOT LIVE VERIFIED
    coingecko.py         Market cap / FDV / supply     <- NOT LIVE VERIFIED, off by default
    cache.py             Candle cache: a closed bar never changes  <- request budget
    fixture.py           SYNTHETIC generator for offline dev/tests
    registry.py          One switch: CP_PROVIDER_MARKET_DATA
  features/
    indicators.py        Pure numpy, nan warm-up, no look-ahead
    volume.py            RVOL, volume acceleration, climax
    structure.py         Fractal swings, level clustering, breakout geometry
    stats.py             z-score, percentile, cross-sectional rank
    regime.py            Bull/bear/range + volatility regime
    expansion.py         Long-horizon readings: base age, drawdown, VCP, spring,
                         quiet accumulation                       <- the ×10 inputs
    pipeline.py          TimeframeFeatures.build / AssetFeatures  <- closed() boundary
  scoring/
    components.py        The 8 weighted components, each self-explaining
    pump_maturity.py     0 = not started, 100 = probably late
    acceleration.py      Momentum acceleration + early move
    confidence.py        Data confidence, with a hard staleness cap
    moonshot.py          MOONSHOT_ENGINE_V1: headroom + capacity + ignition,
                         stage machine, ×10 ranking          <- separate axis
    states.py            IGNORE/OBSERVE/WATCH/ARMED/BREAKOUT/RETEST/...
    engine.py            SCORE_ENGINE_V1 orchestrator + weights fingerprint
  risk/
    liquidity.py         Liquidity gate, DANGEROUS => veto
    safety.py            CEX safety score; DEX surface declared, not implemented
    penalties.py         RAW - PENALTY = FINAL, every deduction named
  scanner/
    base.py              Scanner ABC + ScanReport
    cex.py               Two-pass pipeline (cheap filter, then order books)
    memory.py            Score history ring buffer, score acceleration
  universe/
    robinhood.py         Which assets Robinhood lists; resolution against a venue
    symbols.py           Ticker equivalences (XBT=BTC, XDG=DOGE, POL=MATIC)
  alerts/
    engine.py            Levels, gates, dedup, cooldown; SETUP and MOONSHOT kinds
    notifiers.py         Delivery: console / jsonl / webhook / telegram / discord
  outcomes/
    tracker.py           Grades signals on the label's own timeframe (5m or 1d)
    stats.py             Win rate by bucket, component edge, ×10 multiple distribution
  backtest/
    labels.py            Triple-barrier: ATR-scaled (hours) and multiple-based (weeks)
    metrics.py           Expectancy, PF, DD, Sharpe/Sortino (>=20 trades only)
    splits.py            Chronological split + walk-forward with embargo
    engine.py            Replays the live scorer over history via series.upto()
  database/
    models.py            signals / score_points / alerts / scan_runs
    migrate.py           Additive ALTER TABLE for columns create_all cannot add
    session.py           SQLAlchemy engine
    repo.py              The only module that writes
  api/
    service.py           Owns the scanner, the loop and shared state
    app.py               FastAPI routes, serves the built dashboard
  cli.py                 doctor / scan / radar / universe / resolve / serve / backtest

start.sh                 One-command launcher: install, verify feed, scan

scripts/
  simulate_journal.py    Scan across simulated time, grade, compare to baseline

frontend/                Vite + React 18 + TypeScript (strict)
tests/                   402 tests
pine/                    TradingView companion scripts
```

---

## 5. Non-negotiable invariants

Break these and the product is lying to its user.

1. **Never fabricate a value.** Missing data raises a typed error or is `None`.
   There is no zero-filling, no forward-filling, no "reasonable estimate".
   A `None` scores zero points *and* says the input was unavailable.

2. **Never look ahead.** `OHLCVSeries.closed()` drops the in-progress candle and
   is called at the single entry point `TimeframeFeatures.build`. Backtests use
   `series.upto(ts)` (strictly `close_time <= ts`). Every indicator satisfies:
   truncating the input does not change earlier outputs — enforced by
   `tests/test_no_lookahead.py`.

3. **Fractal swings are confirmed, never provisional.** `find_swings` will not
   return a pivot inside the last `right` bars. Levels do not repaint.

4. **A score is not a probability.** Show `84/100`, never `84%`. There is no
   calibration yet, so there is no probability to show.

5. **Every score is explainable.** Each component carries `reasons` (why it
   scored) and `caveats` (what argues against). `why()` uses reasons only;
   `risks()` collects caveats plus penalties. A test enforces that no component
   ever produces points with no explanation.

6. **Gates before scoring.** Liquidity DANGEROUS or safety below the floor sets
   a hard veto: the asset stays visible for information but can never be
   ARMED/BREAKOUT/premium.

7. **Synthetic data must be unmistakable.** The fixture provider's source string
   is `SYNTHETIC-FIXTURE`, its provenance note is
   `SYNTHETIC_DATA_NOT_REAL_MARKET`, the API returns `synthetic_data: true`, and
   the dashboard shows a permanent amber banner.

8. **One asset failing never stops a scan.** Errors are collected per symbol in
   `ScanReport.errors` and surfaced in the UI failure count.

9. **Paper mode.** `CP_PAPER_MODE=true` by default. No order-placing code exists
   anywhere in this repository.

10. **An outcome is graded once, never re-graded.** `save_resolutions` skips a
    row that already has a verdict. Re-grading under a different label config
    would silently rewrite history.

11. **A verdict that cannot be known is not invented.** Too early stays pending;
    unreachable bars become `UNRESOLVABLE` with a reason and are excluded from
    every rate rather than counted as losses.

12. **Every rate carries its `n`.** Buckets below `MIN_SAMPLE` (20) are flagged
    `insufficient_sample`. A 100% win rate over three signals is not a finding.

13. **Scoring is versioned.** `engine_version` + a `weights_fingerprint` hash go
    into every signal row. Change a weight and the fingerprint changes, so old
    signals are never reinterpreted under new rules. The fingerprint covers the
    **timeframe set** as well as the weights: multi-timeframe alignment is one of
    the eight components, so adding a timeframe changes what a 70 means.

14. **The ×10 axis never moves the opportunity score, and never lifts a gate.**
    `assess_moonshot` runs last, reads only what is already computed, and writes
    nothing back. A liquidity or safety veto still vetoes an asset with a
    moonshot score of 95 — enforced in `AlertEngine.evaluate_moonshots`.

15. **Capacity is unknown, never assumed.** Market cap cannot be derived from a
    candle at any price. With no valuation source the capacity reading is `None`,
    the composite renormalises over what remains, and `unknowns` says so in
    plain words. "Not in the top 500 by cap" is recorded as an *upper bound*,
    which is a fact — and is never promoted to a measurement.

16. **Lateness caps the score, it does not merely discount it.** Once pump
    maturity says the move is extended, the stage becomes EXHAUSTION and the
    score is capped. A late entry still scoring 90 is the most expensive thing a
    radar can show a user.

17. **A delivery failure never breaks a scan, and a secret never reaches a log.**
    Every notifier catches its own failures; detail strings carry a status code
    or an exception class, never a URL or a token (the Telegram token is part of
    the URL path). An unconfigured channel names the *setting* that is missing.

18. **Two theses, two verdicts, each graded once.** A signal carries an intraday
    claim and a multi-week one. They are graded against different labels on
    different timeframes into different columns (`outcome_*` and
    `moon_outcome_*`), and neither is ever rewritten. Sharing one verdict would
    force a choice between answering the first question and the second.

19. **A reading that is not journalled cannot be validated.** The persist filter
    keeps any row with a real ×10 reading, not only rows with an intraday setup —
    a dormant base scores IGNORE on the setup axis, and filtering on that alone
    guaranteed an empty moonshot journal forever.

20. **The cache may only assume that a closed bar never changes.** An entry is
    valid until the instant the next bar of its timeframe closes, and nothing
    else. Order books and 24h tickers are never cached; `doctor` always bypasses
    it, because proving the live API works cannot be done against a cache.

21. **"The process is up" is not "the radar is working".** `health_status()`
    answers OK / DEGRADED / DOWN with reasons, `/healthz` turns that into a
    status code an orchestrator can act on, and the watchdog says it out loud
    through the alert channels — once per outage, and again on recovery.

22. **The universe is stated, not implied.** Which assets were scanned, which
    listed ones the venue does not carry, and where the listing came from are in
    every scan report, the API and the dashboard banner.

---

## 6. Design decisions and why

**numpy, not pandas.** The workload is per-symbol columnar arrays of a few
hundred rows. numpy is faster here, has a smaller dependency surface, and makes
the no-look-ahead property easy to state and test (a function maps arrays to
arrays of the same length). Revisit only if cross-sectional joins become the
bottleneck.

**Vite + React, not Next.js.** No SSR requirement, no routing beyond a tab and a
drawer, no server components. Next.js would add a Node runtime to deploy for
zero benefit. FastAPI serves the built bundle directly.

**Synchronous SQLAlchemy.** One batch write per scan interval. An async driver
would add complexity for no measurable gain; writes go through
`asyncio.to_thread`.

**Two-pass scan.** Pass 1 scores everything from klines. Pass 2 fetches order
books only for the top 20 and rescores them. Depth is the most expensive data
per unit of signal, and fetching 120 books per minute would burn the rate limit.

**Rate limiting on request *weight*, not count.** Exchange limits are weight
based: one 500-candle kline call costs far more than a ping. Counting requests
would either under-use the budget or get the IP banned.

**Staleness is a cap on confidence, not a weighted term.** Discovered by a test:
with deep history and full timeframe coverage, six-hour-old data still scored 60.
Age is not one opinion among six — if the data is old, nothing computed from it
is trustworthy. See `scoring/confidence.py`.

**Data age is the primary timeframe's age.** Taking the max across timeframes
made every healthy feed look stale, because a 4h candle is legitimately hours
old. The reported figure is the median primary-timeframe age across assets.

**The ×10 layer is a separate axis, not a bigger opportunity score.** Blending
them into one number would make a 78 on a based, dormant micro-cap
indistinguishable from a 78 on an exhausted large-cap pump. They answer
different questions on different horizons and are shown side by side.

**The moonshot reading refuses to run below 4h.** A base that takes four months
to build is invisible in 300 five-minute candles. Given only intraday
timeframes, `assess_moonshot` returns UNKNOWN and says why, rather than scoring
a shape it cannot see. This costs one extra kline request per asset per scan —
the daily — which is why `CP_MOON_ENABLED=false` exists for tight rate budgets.

**The Robinhood universe does not apply the 24h volume floor.** The volume
universe needs one because it is picking 120 out of thousands. The Robinhood
universe is a fixed list of a few dozen, and dropping an asset for having a
quiet day would hide precisely the dormant, based assets the ×10 layer exists to
find. The liquidity gate still runs during scoring, so an untradable asset is
vetoed and visible rather than silently absent.

**Universe resolution matches against the venue's own symbol list.** Building
`BTC + USDT` would scan nothing on Kraken, which lists bitcoin as XBT. Aliases
(XBT/BTC, XDG/DOGE, POL/MATIC, RNDR/RENDER) are tried against what the venue
actually returned, so a rename in either direction resolves instead of silently
dropping the asset.

**Relative strength and volume rank are computed per scan, before scoring.**
Both are cross-asset facts: RVOL 2.5 means one thing on a dead Sunday and
another when the whole board is at 2.5. Computing them after the score would
mean the score could not use them.

**`enable_decoding=False` on every settings class holding a list.**
pydantic-settings JSON-decodes complex env values before validators run, so
`CP_SCAN_ALWAYS_INCLUDE=BTCUSDT,ETHUSDT` — the form `.env.example` documents —
raised a JSONDecodeError at import and killed the process. The documented
configuration must load.

**Ambiguous backtest bars resolve as LOSS.** When one candle touches both the
target and the stop, intrabar order is unknown. Assuming the win flatters every
result and the error compounds. See `backtest/labels.py`.

---

## 7. Commands

```bash
# setup
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env

# verify the live data feed (the important one)
python -m cryptopulse.cli doctor

# one scan, printed as a table (plus the ×10 block)
python -m cryptopulse.cli scan --limit 30

# what the Robinhood filter actually resolves to on this venue, and what it cannot
python -m cryptopulse.cli universe

# the autonomous radar: scan, alert, notify, repeat until stopped
python -m cryptopulse.cli radar
python -m cryptopulse.cli radar --once --provider fixture --universe robinhood --rank moonshot

# grade signals whose horizon has elapsed, then print realised performance
python -m cryptopulse.cli resolve

# full loop offline: scan across simulated time, grade, compare to random entry
python scripts/simulate_journal.py --scans 70 --step-bars 3
CP_PROVIDER_MARKET_DATA=fixture python -m cryptopulse.cli scan   # offline

# API + dashboard on http://localhost:8000
cd frontend && npm install && npm run build && cd ..
python -m cryptopulse.cli serve

# frontend dev server with hot reload (proxies /api to :8000)
cd frontend && npm run dev

# backtest
python -m cryptopulse.cli backtest --symbols BTCUSDT,ETHUSDT --bars 1000

# tests
pytest -q
pytest tests/test_no_lookahead.py -v    # the ones that matter most
```

---

## 8. Where to be careful

* `features/pipeline.py:TimeframeFeatures.build` — the `closed()` call on the
  first line is the whole no-look-ahead guarantee. Do not move it, do not add a
  second path that builds features from a raw series.
* `scoring/engine.py:_validate_weights` — weights must sum to 100. The engine
  refuses to construct otherwise, on purpose.
* `providers/binance.py` and `providers/kraken.py` — the only modules that know
  an exchange's payload shape. Keep it that way.
* `providers/kraken.py:_get` — Kraken reports failures with **HTTP 200** and a
  populated `error` array. Checking the status code alone reads a hard failure as
  success. Never bypass this unwrapping.
* `providers/kraken.py:_ensure_pairs` — every Kraken response is keyed by the
  legacy pair id (`XXBTZUSD`), not the tradable name (`XBTUSD`). The scanner
  reaches `get_tickers_24h` without calling `list_symbols`, so the map must build
  itself on first use. An integration test caught this exact bug.
* `database/repo.py` — signals are immutable historical facts. Duplicate
  `(symbol, timestamp, engine_version)` rows are skipped, never updated.
* `risk/safety.py:dex_safety` — raises `NotImplementedError` deliberately. Do
  not make it return a default to silence the error.
* `providers/fixture.py:_fbm` — the `hurst=0.5` default is load-bearing. At
  H = 1 the synthetic series mean-reverts hard and random entries win ~10% on a
  2:1 barrier instead of ~33%, making every synthetic outcome look catastrophic
  for reasons unrelated to the strategy. A regression test guards it.
* `outcomes/tracker.py:_resolve_one` — requires an *exact* close-time match.
  Resolving against the nearest bar would shift every barrier by a bar.
* `scoring/moonshot.py:_stage` — **order matters**. Lateness is checked first and
  overrides everything: an asset can be igniting on every measure and still be
  something you are late to. Reordering these branches would put exhausted pumps
  at the top of a ×10 list, which is the single worst output this system can
  produce.
* `scoring/moonshot.py:_composite` — renormalises over the readings that exist.
  Do not "simplify" it by defaulting a missing capacity to 0; that would rank an
  asset with an unknown market cap below one known to be a $40B large-cap.
* `features/expansion.py:base_run_length` — walks backwards and stops at the bar
  that would widen the range. It measures the base that is forming *now*, not the
  best base in history, because only the current one can break.
* `alerts/notifiers.py` — the Telegram token is inside the request URL. No code
  path may log or return a URL. `_post` deliberately returns a status code or an
  exception class and nothing else; a test asserts the secret never appears in a
  result.
* `config/settings.py` — the `enable_decoding=False` on classes with list fields
  is load-bearing, not cosmetic. Removing it makes every comma-separated
  environment variable in `.env.example` crash the process at import.
* `providers/cache.py:_valid_until` — the validity rule is the bar boundary, not
  a duration. Replacing it with a fixed TTL would serve a stale closed bar for
  the remainder of the TTL, which is exactly the class of silent error the whole
  project is built to avoid.
* `database/repo.py:_signal_record` — one builder for both the batch and the
  row-by-row paths. They used to construct the row twice, which is how a column
  ends up written on one path and silently NULL on the other.
* `outcomes/tracker.py:_entry_index` — the exact-bar rule still holds *within* a
  timeframe. The cross-timeframe branch exists only for a label graded on slower
  bars than the signal was scored on, and it writes the resulting lag into the
  resolution note rather than absorbing it.
* `database/models.py` — every `_ms` column is `BigInteger`. A millisecond epoch
  is ~1.8e12 and PostgreSQL's INTEGER stops at 2.1e9, so the original `Integer`
  meant *every insert failed on Postgres* while SQLite, which types integers
  dynamically, never noticed. `tests/test_postgres.py` compiles the DDL for the
  real dialect so this cannot regress.
* `api/service.py:run_maintenance` — purges score points only. Signals are the
  evidence, and a ×10 label can take 180 days to settle, so a retention timer
  shorter than the horizon would delete rows before they could be graded.
* `universe/robinhood.py:SNAPSHOT_BASES` — a hand-maintained list. Adding a
  symbol Robinhood does not list costs a permanent `missing` row; omitting one it
  does list means that asset is never scanned. Neither corrupts a score, and both
  are visible in `cryptopulse universe`.

---

## 9. Next steps, in order

1. **Run `doctor` in an environment with egress.** Fix any field-mapping
   mismatch it reports. Only then is anything else worth doing.
2. **Let it run and accumulate signals.** The journal is the point; nothing can
   be validated without a few thousand real rows.
3. ~~Build the outcome tracker.~~ **Done.** `outcomes/tracker.py` grades signals
   once their horizon elapses; `outcomes/stats.py` aggregates. Runs automatically
   after every scan, or on demand via `cryptopulse resolve` /
   `POST /api/outcomes/resolve`.
4. **Watch the baseline comparison.** `scripts/simulate_journal.py` prints the
   scanner's win rate against random entries on the same bars. On synthetic data
   the scanner currently lands ~7 points *below* random. That number says nothing
   about markets, but the same comparison on real history is the first thing that
   would reveal the V1 weights are actively harmful rather than merely
   unvalidated. Do not skip it.
5. **Backtest on real history**, walk-forward, with the embargo set to at least
   the label horizon.
6. **Then, and only then, revisit the weights.** Feature importance against real
   outcomes. Any change bumps the engine version.
7. **Calibration** — map score bands to observed frequencies. Only after this
   may the UI display anything resembling a probability.
8. Phase 2: DEX scanner, on-chain safety, multi-provider cross-validation.
9. ~~Grade the ×10 layer against a label that matches its horizon.~~ **The
   machinery is done** (§11): readings are journalled, `MOONSHOT_LABEL_CONFIGS`
   grades them on daily bars over 30/90/180 days, and every row records the
   multiple actually reached. What is still missing is the only thing that
   matters — *real* signals to grade. Until then `MOONSHOT_ENGINE_V1` remains an
   untested hypothesis with a stage machine, which is exactly how it describes
   itself.
10. Phase 3: ML, compared against the deterministic baseline. Not before.

---

## 10. Style conventions

* Type hints everywhere. `from __future__ import annotations` at the top.
* Comments explain *why*, never *what*. If a threshold is arbitrary, say so.
* Every module starts with a docstring stating what it guarantees.
* Tests are named as the behaviour they assert, not the function they call.
* No bare `except:`. Catch typed errors; a broad catch must log and continue
  with a stated reason.


---

## 11. The ×10 layer (`MOONSHOT_ENGINE_V1`)

Read `cryptopulse/scoring/moonshot.py` before changing anything here; its
docstring is the specification. The short version:

**Three separate readings, deliberately not merged into one comforting number.**

| Reading | Question | Source | Missing when |
|---|---|---|---|
| **Headroom** | Has it traded ×10 higher than this, in the history we can see? | Arithmetic on the daily candles | Only at the top of its own history |
| **Capacity** | Is a ×10 *payable* at this market cap? | Valuation provider | **Always, unless `CP_PROVIDER_VALUATION` is set** |
| **Ignition** | Is the behaviour changing right now? | 11 weighted sub-signals | Individually, each says so |

`score = weighted blend, renormalised over what exists`. Not a probability.

**The eleven ignition sub-signals**, weights in `_IGNITION_WEIGHTS`:
volume regime shift (daily volume vs its own 30-bar median), a multi-month level
breaking, the accumulation tape (CMF + A-D slope + quiet accumulation), base
length, trend reclaim over EMA50, relative strength vs the benchmark, volatility
compression, the contraction pattern (VCP), cross-sectional volume rank, faster
timeframes agreeing, and a spring (failed breakdown reclaimed).

**Stages**, in evaluation order — the order is the safety property:

`EXHAUSTION` (late, score capped at 45) → `EXPANSION` (markup under way, capped
at 70) → `IGNITION` → `ACCUMULATION` → `DORMANT` → `NEUTRAL`, with `UNKNOWN` when
there is no timeframe of 4h or slower. Only IGNITION and ACCUMULATION can raise
an alert.

### How it is graded

Every reading is written to the journal (`moonshot_*` columns) — including on
assets whose intraday setup state is IGNORE, which is the normal state of a
dormant base and precisely what this layer looks for. Filtering the journal on
the setup state alone, as V1 did, guaranteed an empty ×10 history and therefore a
permanently unvalidatable layer.

Verdicts land in a *second* column set (`moon_outcome_*`), graded by
`MOONSHOT_LABEL_CONFIGS` on **daily** bars:

| Label | Target | Stop | Horizon |
|---|---|---|---|
| `moon_2x_30d` (default) | ×2 | −35% | 30 daily bars |
| `moon_3x_90d` | ×3 | −50% | 90 daily bars |
| `moon_10x_180d` | ×10 | −60% | 180 daily bars |

One compromise, stated rather than hidden: grading directly at ×10 would settle
approximately nothing for years, which is not a feedback loop. So the ladder
settles earlier — and **every row records `max_multiple`**, the highest multiple
actually reached inside the horizon. "How many ever reached ×10" is therefore
answerable by reading the journal, without re-grading and without waiting three
years for a label to settle. `cryptopulse resolve --axis moonshot` prints that
distribution, and reports zero when it is zero.

Because the label resolves on daily bars while the signal was scored on a
5-minute close, no daily bar closes at the signal's timestamp. The tracker places
the signal in the daily bar it fired *inside*, entry is the open of the next one,
and the resulting lag is written into the resolution note. The exact-bar rule is
untouched for labels graded on the timeframe the signal was scored on.

**What it has never been:** validated. No ×10 outcome has ever been graded
against these weights on real data, because no real signal history exists yet
(§3). It ranks resemblance to a pre-expansion state. Most of what it flags will
not do ×10 — that is a property of the market, and the module says so in its own
payload.

---

## 12. The Robinhood universe

**Robinhood publishes no usable public market-data API.** Its Crypto Trading API
needs an API key and an Ed25519 request signature and serves your own account,
not a scannable historical feed. So this project does **not** get prices from
Robinhood. `universe/robinhood.py` decides *which symbols to scan*; the candles
still come from Binance or Kraken.

The consequence has to reach the user, and does — in the scan notes, the API and
a permanent dashboard banner: **the price on screen is the reference venue's
price, not Robinhood's.** Robinhood's spread and fill will differ, sometimes
materially on a thin asset during a fast move.

The listing itself is a **hand-maintained snapshot** (`SNAPSHOT_BASES`, dated),
never verified against Robinhood. Both ways it can be wrong are safe and
visible: an asset the venue does not carry appears in `missing` and is skipped;
one Robinhood lists but the snapshot omits is simply never scanned. To correct
it, set `CP_SCAN_ROBINHOOD_EXTRA` / `_EXCLUDE`, or point
`CP_SCAN_ROBINHOOD_FILE` at your own JSON — which is what
`cryptopulse universe --refresh` writes if Robinhood's catalogue is reachable
(it never has been from this sandbox).

---

## 13. Autonomy

`cryptopulse radar` is the daemon: scan → alert → notify → persist → grade,
until stopped. Three properties make it safe to leave running:

* **it survives anything a scan throws** — a failed cycle backs off
  exponentially (capped at 15 minutes) so a dead feed is not hammered;
* **it prints where alerts will go before it starts**, marking any configured
  but inert channel with the setting it is missing — a misconfiguration is found
  at 09:00, not at 03:14;
* **it stops cleanly** on SIGINT/SIGTERM, closing the provider and flushing the
  database rather than dying mid-write.

Delivery lives in `ScannerService`, not in the CLI, so the API's own scan loop
notifies through exactly the same path. Alerts are persisted *before* they are
delivered: a delivery crash can never lose the record.

**What makes the loop affordable.** Every pass rebuilds every asset's features,
which without a cache means re-downloading 400 daily candles per asset per
minute for a series whose newest bar changes once a day. `providers/cache.py`
holds a series until the next bar of its timeframe closes — the only assumption
being that a closed bar never changes. Measured over ten one-minute passes on the
Robinhood universe: 57% fewer kline requests overall, and the daily read once
instead of ten times. The hit rate is reported in `/api/health` under
`candle_cache`, because a collapsing hit rate is the first sign the scan interval
and the timeframe set have drifted out of step — and it shows up there before it
shows up as a rate-limit ban.
