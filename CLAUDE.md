# CLAUDE.md — CRYPTO PULSE AI

Working notes for anyone (human or agent) continuing this project. Read this
before changing code; it records the decisions and the reasons, not just the
layout.

---

## 1. What this is

A crypto scanner that tries to answer **"which asset is changing behaviour right
now, before the rest of the market notices?"** — not "what went up the most
today".

Pipeline: `DATA → SCAN → FILTER → SCORE → VALIDATE → RANK → ALERT → BACKTEST`.

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
| Database + signal journal | **TESTED** | SQLite verified; Postgres **NOT** verified |
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
| Performance analytics (`outcomes/stats.py`) | **TESTED** | Buckets + component edge, `n` on every rate |
| Schema migration (`database/migrate.py`) | **TESTED** | Additive columns only; refuses destructive changes |

**Test suite: 245 tests, all passing.** Run `pytest -q`.

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
**Custom** -> add `api.binance.com` and `data-api.binance.vision` to
**Allowed domains**, keeping "Also include default list of common package
managers" checked. Then start a NEW session; a running one keeps the policy it
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
    fixture.py           SYNTHETIC generator for offline dev/tests
    registry.py          One switch: CP_PROVIDER_MARKET_DATA
  features/
    indicators.py        Pure numpy, nan warm-up, no look-ahead
    volume.py            RVOL, volume acceleration, climax
    structure.py         Fractal swings, level clustering, breakout geometry
    stats.py             z-score, percentile, cross-sectional rank
    regime.py            Bull/bear/range + volatility regime
    pipeline.py          TimeframeFeatures.build / AssetFeatures  <- closed() boundary
  scoring/
    components.py        The 8 weighted components, each self-explaining
    pump_maturity.py     0 = not started, 100 = probably late
    acceleration.py      Momentum acceleration + early move
    confidence.py        Data confidence, with a hard staleness cap
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
  alerts/engine.py       Levels, gates, dedup, cooldown
  outcomes/
    tracker.py           Grades emitted signals against the bars that followed
    stats.py             Win rate / expectancy by bucket + per-component edge
  backtest/
    labels.py            Triple-barrier, ATR-scaled, pessimistic on ambiguity
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
  cli.py                 doctor / scan / resolve / serve / backtest (--provider)

start.sh                 One-command launcher: install, verify feed, scan

scripts/
  simulate_journal.py    Scan across simulated time, grade, compare to baseline

frontend/                Vite + React 18 + TypeScript (strict)
tests/                   245 tests
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
    signals are never reinterpreted under new rules.

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

# one scan, printed as a table
python -m cryptopulse.cli scan --limit 30

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
9. Phase 3: ML, compared against the deterministic baseline. Not before.

---

## 10. Style conventions

* Type hints everywhere. `from __future__ import annotations` at the top.
* Comments explain *why*, never *what*. If a threshold is arbitrary, say so.
* Every module starts with a docstring stating what it guarantees.
* Tests are named as the behaviour they assert, not the function they call.
* No bare `except:`. Catch typed errors; a broad catch must log and continue
  with a stated reason.
