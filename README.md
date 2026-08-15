# CRYPTO PULSE AI

An early-acceleration crypto scanner. It tries to answer one question:

> **Which asset is changing behaviour right now, before the rest of the market notices?**

Not "what pumped the most today" — that list is always full of moves you have
already missed. This scanner is built around the opposite bias: it measures
*derivatives* (is volume accelerating? is the score rising?) and actively
penalises moves that are already extended.

```
DATA → SCAN → FILTER → SCORE → VALIDATE → RANK → ALERT → BACKTEST
```

---

## ⚠️ Read this before you trust a single number

**1. The Binance connector has never been run against Binance.**

This system was developed in a sandbox whose network policy blocks
`api.binance.com`, `data-api.binance.vision`, `api.coingecko.com` and
`api.dexscreener.com` (HTTP 403 on CONNECT). The official API documentation was
blocked too. The connector is written against the long-standing public Spot API
v3 contract, but that contract is an **assumption**, not something that was
verified.

Before using this for anything, run:

```bash
python -m cryptopulse.cli doctor
```

It round-trips every endpoint and cross-checks the parsed values against
independent fields in the same response. It prints `LIVE VERIFIED` or the exact
mismatch.

**2. No backtest in this repository was run on real market data.** The backtest
engine is tested and works; it has only ever been fed synthetic candles. There
are no performance claims here because there is no evidence for any.

**3. The score is not a probability.** `84/100` means this setup ranks above one
scoring `60` under the current fixed weights. It does not mean 84% of anything.
The weighting is a starting hypothesis that has not been statistically fitted.

**4. This is not financial advice and places no orders.** `PAPER_MODE` is on by
default and there is no order-placing code in the repository.

---

## What it does

**Finds acceleration, not altitude.** RVOL going `1.0 → 1.2 → 1.5 → 1.9 → 2.6`
scores higher than a flat `2.5`. The second derivative is the signal.

**Refuses to chase.** A separate **Pump Maturity** score (0 = not started,
100 = probably late) is computed on every asset and applied as a penalty. A
setup can be strong *and* late, and you see both numbers.

**Fires before the breakout.** Distance to the relevant resistance is measured in
ATR, so the `ARMED` state marks assets coiled below a level rather than assets
that already cleared it.

**Says how much it trusts itself.** A **Data Confidence** score reports feed
freshness, history depth, timeframe coverage and order-book availability. Old
data hard-caps confidence, so a stale feed can never produce a premium signal.

**Explains every point.** Each of the eight components reports what it awarded
and why, split into reasons (arguments for) and caveats (arguments against).
`RAW − PENALTY = FINAL`, all three displayed.

**Remembers.** Score history per symbol means a coin going `50 → 80` in twenty
minutes ranks above one that has sat at `80` for three hours.

---

## Scoring model — `SCORE_ENGINE_V1`

| Component | Max | Measures |
|---|---:|---|
| Volume & acceleration | 20 | RVOL level, short-vs-medium volume ratio, RVOL slope |
| Momentum | 15 | ROC, MACD histogram direction, EMA20 distance, RSI band |
| Structure | 15 | HH/HL trend, range position, defined invalidation |
| Breakout proximity | 15 | ATR-normalised distance to the nearest tested resistance |
| Volatility expansion | 10 | Bollinger compression percentile releasing on volume |
| Order flow | 10 | Book imbalance within 0.5% of mid |
| Multi-timeframe | 10 | Weighted bias agreement (4h counts more than 5m) |
| Market quality | 5 | Liquidity tier |
| **RAW_SCORE** | **100** | |

Then, computed separately and subtracted:

**RISK_PENALTY** (capped at 45) — pump maturity, poor liquidity, wide spread,
volume climax, higher-timeframe conflict, heavy overhead resistance, failed
breakout, irrational volatility, structural downtrend, proxy divergence.

```
FINAL_SCORE = max(0, RAW_SCORE − RISK_PENALTY)
```

Alongside, never blended in: **Pump Maturity**, **Data Confidence**,
**Safety**, **Liquidity Status**, **Momentum Acceleration**, **Early Move**.

### Setup states

`IGNORE` · `OBSERVE` · `WATCH` · `ARMED` · `BREAKOUT` · `RETEST` ·
`CONTINUATION` · `INVALIDATED`

`ARMED` is the interesting one: a strong setup within 1 ATR of its trigger level
that has *not* yet broken out, with maturity still acceptable.

---

## How look-ahead bias is prevented

This is the part most scanners get wrong, so it is enforced structurally rather
than by convention.

1. **The in-progress candle is dropped.** `OHLCVSeries.closed()` is called at the
   single entry point where features are built. No feature ever sees a bar whose
   high, low or close can still change. A test proves that a violent unfinished
   spike moves no indicator and no score.

2. **Every indicator is prefix-stable.** For any indicator `f` and any `k`,
   `f(x[:k]) == f(x)[:k]`. Truncating the input cannot change earlier outputs.
   `tests/test_no_lookahead.py` asserts this for all 17 indicators.

3. **Swing pivots wait for confirmation.** A fractal high needs `right` bars
   after it, so `find_swings` never returns a pivot inside the unconfirmed tail.
   Levels, once emitted, never move.

4. **Backtests slice by close time.** `series.upto(ts)` keeps only candles with
   `close_time <= ts`. The scorer has no other data source, so it is structurally
   incapable of reading a bar it would not have had live.

5. **Entries are at the next bar's open**, never at the signal bar's close.

6. **Warm-up is `nan`**, never zero and never back-filled.

---

## Installation

Requires Python 3.11+ and Node 18+.

```bash
git clone <this repo> && cd MonbOTmemetrader

python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env                # edit if you want; defaults work

cd frontend && npm install && npm run build && cd ..
```

No API key is required. The scanner reads only public market-data endpoints.

---

## Running

```bash
# 1. Verify the data feed FIRST
python -m cryptopulse.cli doctor

# 2. One scan, printed as a ranked table
python -m cryptopulse.cli scan --limit 30

# 3. API + dashboard at http://localhost:8000
python -m cryptopulse.cli serve

# Offline development (SYNTHETIC data, clearly labelled everywhere)
CP_PROVIDER_MARKET_DATA=fixture python -m cryptopulse.cli serve
```

Frontend hot reload during development:

```bash
cd frontend && npm run dev      # :5173, proxies /api to :8000
```

### Docker

```bash
docker compose up --build       # API on :8000, Postgres on :5432
```

---

## Dashboard

**Live market scanner** — every column sortable:

| Rank | Asset | Price | 5m | 1h | RVOL | Opportunity | Accel | Maturity | Safety | Conf | Status |

**Top opportunities now** — ranked by setup quality, not by price change. When
nothing clears the gates it says so, rather than promoting the best of a bad list.

**Asset detail** (click any row) — full score explainability with per-component
bars and the `RAW − PENALTY = FINAL` arithmetic, score history sparkline,
multi-timeframe bias grid, market data, and two explicit sections:
**Why this asset?** and **What can invalidate it?**

**Freshness bar** — always visible: API status, data source, last update, scan
age, market data age, scanned/failed counts, market regime, paper mode. A stale
feed or a synthetic source raises a permanent banner.

---

## Data sources

| Provider | Type | Status |
|---|---|---|
| Binance Spot public REST | Market data + order book | IMPLEMENTED, **not live verified** |
| Fixture generator | Synthetic candles | For tests and offline dev only |
| CoinGecko / DexScreener / Birdeye | — | NOT IMPLEMENTED |

Endpoints used (all public, unauthenticated): `/api/v3/ping`,
`/api/v3/exchangeInfo`, `/api/v3/ticker/24hr`, `/api/v3/klines`, `/api/v3/depth`.

Swapping providers means writing one class against `MarketDataProvider` in
`providers/base.py` and flipping `CP_PROVIDER_MARKET_DATA`. Nothing above the
provider layer knows the name of an exchange.

### Provider resilience

Weighted rate limiter (budget on request *weight*, not count), exponential
backoff with full jitter, automatic failover to the market-data mirror, and a
circuit breaker that stops hammering a provider that is down. A 4xx is never
retried — it is our bug, not a transient failure.

---

## Testing

```bash
pytest -q                            # 189 tests
pytest tests/test_no_lookahead.py -v # the ones that matter most
```

| File | Tests | Covers |
|---|---:|---|
| `test_indicators.py` | 21 | Indicators against hand-computed references |
| `test_no_lookahead.py` | 24 | Prefix stability, swing confirmation, forming-candle isolation |
| `test_structure.py` | 15 | Swings, level clustering, breakout, fake breakout, compression |
| `test_scoring.py` | 21 | Weight validation, arithmetic, explainability, maturity, states |
| `test_risk.py` | 17 | Liquidity tiers, vetoes, safety, penalty caps |
| `test_providers.py` | 19 | Rate limiter, circuit breaker, retries, Binance parsing, synthetic labelling |
| `test_stale_data.py` | 12 | Provenance age, confidence decay, staleness cap |
| `test_scanner_alerts.py` | 23 | Pipeline resilience, score memory, alert dedup and cooldown |
| `test_backtest.py` | 22 | Labels, metrics, splits, costs, replay isolation |
| `test_api.py` | 16 | Endpoint contracts, filters, freshness, no-probability rule |

---

## Backtesting

```bash
python -m cryptopulse.cli backtest --symbols BTCUSDT,ETHUSDT --bars 1000 --label standard_2R
```

**Labels** (triple barrier, ATR-scaled, three horizons):

| Name | Target | Stop | Horizon |
|---|---|---|---|
| `fast_2R` | +2 ATR | −1 ATR | 12 bars |
| `standard_2R` | +2 ATR | −1 ATR | 24 bars |
| `patient_3R` | +3 ATR | −1 ATR | 48 bars |

A candle that touches both barriers resolves as **LOSS** — intrabar order is
unknown, and assuming the win flatters every result.

**Metrics**: signal count, win rate, average win/loss, expectancy, profit factor,
max drawdown, Sharpe and Sortino (per-trade, only above 20 trades, never
annualised), MAE, MFE, average hold, and a per-regime breakdown.

**Costs**: 10 bps taker fee + 8 bps slippage + 2 bps half-spread, applied on both
legs (40 bps round trip). The assumptions are printed with every result.

**Splits**: chronological 60/20/20 plus walk-forward windows with a configurable
embargo between train and test — needed because overlapping labels would
otherwise leak the test window into training.

---

## Known limits

* **The Binance connector is unverified against the live API.** Nothing else
  matters until `doctor` passes.
* **No real backtest results exist.** The engine works; it has not been fed real
  history.
* **The V1 weights are a hypothesis.** Chosen from trading reasoning, not fitted
  to outcomes. They will almost certainly change once real data arrives.
* **No probability calibration.** Scores are ranks. There is no evidence base to
  convert them into likelihoods yet.
* **No outcome tracker.** The `signals` table has outcome columns and they stay
  NULL. `/api/signals` correctly reports `win_rate: null` rather than inventing one.
* **DEX scanning is not implemented.** The interface is declared;
  `risk/safety.py:dex_safety` raises rather than returning a plausible default.
* **Order flow is REST snapshots**, not a streamed book. Depth carries no venue
  timestamp, so fetch time is recorded as the observation time and labelled as such.
* **Postgres is untested.** SQLite is verified; the Postgres path is configuration
  only.
* **Single-venue.** Cross-provider validation is Phase 2.

---

## Troubleshooting

**`doctor` fails with 403 / connection refused** — your network or region blocks
the exchange. Try `CP_PROVIDER_BINANCE_BASE_URL=https://data-api.binance.vision`,
or run from a permitted network. Do not disable TLS verification.

**`SOURCE_UNAVAILABLE` in the scan output** — the provider is down or the circuit
breaker is open. `/api/health` shows provider status and the rate-limit budget.

**Everything shows `INSUFFICIENT_HISTORY`** — raise
`CP_SCAN_CANDLES_PER_TIMEFRAME` (needs ≥ 60 closed bars, 200+ for EMA200).

**Rate limited (429)** — lower `CP_PROVIDER_REQUEST_WEIGHT_PER_MINUTE` and
`CP_SCAN_MAX_SYMBOLS`.

**`scoring weights must sum to 100`** — the engine refuses to start with weights
that do not total 100, so the score stays on a 0–100 scale. Fix your `CP_SCORE_W_*`.

**Dashboard says "no scan yet"** — press *Scan now*, or wait for
`CP_SCAN_SCAN_INTERVAL_SECONDS`.

**Amber SYNTHETIC banner** — you are on the fixture provider. Set
`CP_PROVIDER_MARKET_DATA=binance`.

---

## The previous app in this repository (MemeTrader Pro V3)

This repo previously held only **MemeTrader Pro V3**, a Node/Express bot that
proxied Binance orders from a single-page front end. Those files are untouched
and still run:

```
server.js          Express server + Binance order proxy
public/index.html  Front end
package.json       Node dependencies, `npm start`
```

```bash
npm install && npm start        # http://localhost:3000
```

It needs `BINANCE_API_KEY` and `BINANCE_API_SECRET` in `.env`. It is unrelated to
CRYPTO PULSE AI, which is Python, places no orders, and needs no key. Note that
`server.js` exposes an unauthenticated `POST /api/order` that places **real
market orders** — do not deploy it on a public URL as it stands.

---

## Security note

**If you used the Binance keys that were previously committed to this
repository, rotate them now.** They were present in plaintext in both `.env` and
the old `README.md`, and this repository is on GitHub. This change untracks
`.env` and removes the keys from the working tree, but **git history retains
them permanently** — anyone who has ever cloned or forked the repo still has
them. Deleting a file does not revoke a credential; only rotating the key does.

Rotate at <https://www.binance.com/en/my/settings/api-management>: delete the
exposed key, create a new one, restrict it by IP, and do not enable withdrawals.
To purge the values from history as well, rewrite it with
[`git filter-repo`](https://github.com/newren/git-filter-repo) and force-push —
but rotate first, because history rewriting does not help with copies that
already exist.

CRYPTO PULSE AI needs no API key at all — it reads only public market data.

---

## Licence

MIT. Use at your own risk. Nothing here is financial advice.
