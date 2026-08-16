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

**3. On synthetic data, the scanner currently selects *worse than random.***
`scripts/simulate_journal.py` compares the scanner's win rate against entries
taken every fifth bar with no scoring at all. On the synthetic feed the scanner
lands roughly 7 points below that baseline. This says nothing about real markets
— it is generated data — but it is reported rather than hidden, and it is exactly
the comparison you must run on real history before trusting the weights.

**4. The score is not a probability.** `84/100` means this setup ranks above one
scoring `60` under the current fixed weights. It does not mean 84% of anything.
The weighting is a starting hypothesis that has not been statistically fitted.

**5. This is not financial advice and places no orders.** `PAPER_MODE` is on by
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

**Grades itself.** Every emitted signal is written to a journal with NULL outcome
columns. Once its horizon elapses, the outcome tracker fetches the bars that
actually followed and records WIN / LOSS / TIMEOUT with the realised return, MFE
and MAE. The dashboard then reports win rate, expectancy and profit factor by
score band, setup state, pump maturity, regime and liquidity — plus which scoring
components separated winners from losers. Until a signal has a verdict, the win
rate is `null`, not zero.

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

**Not a developer?** Read [INSTALL.md](INSTALL.md) instead — same thing in three
commands, in French, with the Android/Termux path and what to do when it fails.

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

The fastest path — installs what is missing, verifies the feed, then scans:

```bash
./start.sh            # your configured provider
./start.sh kraken     # via Kraken
./start.sh binance    # via Binance
./start.sh demo       # offline, SYNTHETIC data, clearly labelled
./start.sh serve      # API + dashboard on :8000
```

It refuses to scan on an unverified feed and tells you why, rather than printing
a table built on a silent source.

Or drive the CLI directly:

```bash
# 1. Verify the data feed FIRST
python -m cryptopulse.cli doctor

# 2. One scan, printed as a ranked table
python -m cryptopulse.cli scan --limit 30

# 3. API + dashboard at http://localhost:8000
python -m cryptopulse.cli serve

# 4. Grade signals whose horizon has elapsed
python -m cryptopulse.cli resolve

# Switch venue for one run, no config change
python -m cryptopulse.cli scan --provider kraken
python -m cryptopulse.cli doctor --provider kraken

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

| Rank | Asset | Price | 5m | 1h | RVOL | Opportunity | Accel | Maturity | Safety | Conf | Status | Verdict |

**Verdict** — the four-level plain-language summary, for reading the table
without reading eight components:

| | Meaning |
|---|---|
| 🟢 **FORTE OPPORTUNITÉ** | Good score, clean gates, young move, triggered setup. All four, or it is not this level. |
| 🟡 **À SURVEILLER** | A genuine setup that has not triggered, or one that has but does not clear the bar. The normal state for most rows. |
| 🟠 **RISQUÉ** | The signal is real, the entry is bad: the move is already mature, or large risk penalties applied. |
| 🔴 **ÉVITER** | A hard gate rejected it, or the data is not trustworthy enough to judge. |

A verdict compresses filters already computed; it introduces no new opinion and
sees no data the score did not. **Every verdict carries its caveat, including the
green one** — a coloured badge with no disclaimer is exactly how a ranking gets
read as a prediction.

**Top opportunities now** — ranked by setup quality, not by price change. When
nothing clears the gates it says so, rather than promoting the best of a bad list.

**Asset detail** (click any row) — full score explainability with per-component
bars and the `RAW − PENALTY = FINAL` arithmetic, score history sparkline,
multi-timeframe bias grid, market data, and two explicit sections:
**Why this asset?** and **What can invalidate it?**

**Verification tab** — what the price did 15m / 1h / 4h / 24h after each signal,
with success rate, median, average, best, worst and average max drawdown per
window, and the same broken down by score band. Before any window has closed it
says so rather than showing zeros.

**LIVE vs DEMO** — decided server-side and exposed as `data_mode` on
`/api/health`, so the dashboard never infers it from a provider name it happens
to recognise. `DEMO` turns the header indicator red and raises a permanent banner
naming the synthetic source and how to switch to a real feed. There is no
intermediate state and the app never switches between them on its own.

**Freshness bar** — always visible: API status, data mode, source, last update,
scan age, market data age, scanned/failed counts, market regime, paper mode. A
stale feed raises its own permanent banner.

---

## Alerts on your phone

Alerts that only exist inside a dashboard nobody is watching are not alerts. Set
one variable and they get delivered:

```bash
CP_ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

The payload shape is inferred from the host — Discord and Slack incoming
webhooks get readable text, anything else gets a JSON POST carrying the full
alert. Nothing to configure beyond the URL.

**That URL is a credential.** A Discord or Slack webhook URL contains its own
bearer token: anyone holding the string can post as you. It never appears in a
log line, an error message, an exception trace or an API response — only its
host, redacted. `/api/health` reports whether delivery is configured and how the
last attempt went, so a webhook that quietly stopped working is visible rather
than mistaken for a quiet market. A delivery failure costs a notification, never
a scan.

Notifications carry the same caveats as the dashboard: the score goes out as
`84/100` and never `84%`, and a DEMO alert is labelled as synthetic on its first
line, before anything a reader might act on.

---

## Retention

`CP_DB_RETENTION_DAYS` (default 90) is applied automatically every six hours, and
on demand via `POST /api/maintenance/prune`. Score points and scan runs are cut
sooner — a quarter of that window — because they are high-volume operational
noise that nothing reads for long.

**A signal that still owes an answer is never pruned**, however old it is: no
verdict recorded, or any of its four horizon windows missing, and it stays. Those
are exactly the rows about to become evidence, and losing them would look like a
quiet journal rather than a bug. `/api/maintenance/prune` reports how many rows
were held back for this reason. Set the value to `0` to disable pruning entirely.

---

## Data sources

| Provider | Type | Status |
|---|---|---|
| Binance Spot public REST | Market data + order book | IMPLEMENTED, **not live verified** |
| Kraken public REST | Market data + order book | IMPLEMENTED, **not live verified** |
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
pytest -q                            # 322 tests
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
| `test_api.py` | 21 | Endpoint contracts, filters, freshness, no-probability rule |
| `test_outcomes.py` | 23 | Fixture stability, resolution correctness, honest non-answers, analytics |
| `test_kraken.py` | 23 | Error envelope, field mapping, pair rename, 2 full-pipeline runs |

---

## Outcome tracking — does the scanner actually work?

A scanner that cannot be graded is an opinion generator. Every signal above
`OBSERVE` is journalled with its full score breakdown and NULL outcome columns.

```bash
# grade every signal whose horizon has elapsed, then print realised performance
python -m cryptopulse.cli resolve

# or over HTTP
curl -X POST localhost:8000/api/outcomes/resolve
curl localhost:8000/api/performance
```

Resolution also runs automatically after every scan.

**Why this is not look-ahead.** Look-ahead is using future data to *make* a
decision. This uses future data to *grade* a decision already written to disk
with its timestamp. The score came from bars closed at or before the signal's
timestamp; resolution reads only bars strictly after it.

Rules the tracker holds to:

| Situation | Behaviour |
|---|---|
| Horizon has not elapsed and no barrier touched | stays **pending** — not settled at the current price |
| A barrier is touched early | settled immediately; the trade is over |
| Signal bar missing from the feed | **UNRESOLVABLE** with a reason — never grafted onto a neighbouring bar |
| Bars needed have fallen out of reach | **UNRESOLVABLE**, excluded from every rate, never counted as a loss |
| Signal has no recorded ATR | **UNRESOLVABLE** — ATR-scaled barriers cannot be placed |
| Already graded | never re-graded; a verdict is written once |

The ATR used is the one recorded *at signal time*, never recomputed from data the
signal did not have. Entry is the next bar's open.

**Performance view** breaks results down by opportunity score band, setup state,
pump maturity band, market regime and liquidity — with `n` beside every rate and
buckets under 20 samples flagged `insufficient_sample`. **Component edge**
compares the average points each component awarded to eventual winners versus
losers: a component that scores both the same is contributing noise to the final
score, however sensible it looked when it was written.

### Multi-horizon verification — what the price actually did

The barrier label answers *would this trade have won?* It collapses a whole price
path into one word, and it cannot tell you a signal was up 3% after an hour and
gave it all back by morning. So a second, complementary record is kept: for every
signal, what the price did **15 minutes, 1 hour, 4 hours and 24 hours** later.

```bash
python -m cryptopulse.cli verify        # or ./start.sh verify

curl -X POST localhost:8000/api/horizons/track
curl localhost:8000/api/horizons
curl localhost:8000/api/signals/42/horizons
```

Per window it records the price reached, the change from entry, the **maximum
gain**, the **maximum drawdown**, and whether it succeeded. Tracking runs
automatically after every scan; the **Verification** tab in the dashboard shows
the table, sliced by score band, setup state and data source.

**Success criterion, stated once and applied everywhere:** a horizon succeeds
when the change from entry, *after the modelled round-trip cost*, is above zero.
Flat is not a win — paying the spread means flat is a small loss. The rule lives
in `HorizonResult.is_success` and nowhere else, so storage and statistics cannot
drift apart.

Every rule from the barrier tracker applies here too — entry at the next bar's
open, exact close-time match, UNRESOLVABLE with a reason rather than a guess —
plus one of its own:

| Situation | Behaviour |
|---|---|
| Window has not fully elapsed | **PENDING**, absent from the table and never written to the database. Reporting the current price would turn "not yet" into a result. |
| Window has no verdict | `success` is `null`, never `false`. An unfinished window is not a failure. |

### Verifying the whole loop offline

```bash
python scripts/simulate_journal.py --scans 70 --step-bars 3
```

Runs real scans across simulated time, grades the resulting journal, and compares
the scanner against a random-entry baseline on the same bars. This works offline
only because the fixture provider is **time-anchored**: the candle at a given
timestamp is identical whenever you ask for it, so a signal recorded at T can
genuinely be graded against T+1, T+2, …

The synthetic feed is also generated with Brownian scaling (Hurst 0.5) so its
increments behave like a market. That detail matters more than it sounds: an
earlier version produced white noise around a trend, which mean-reverts so hard
that random entries won only ~10% on a 2:1 barrier instead of the ~33% a
martingale implies — making every synthetic result look catastrophic for reasons
that had nothing to do with the strategy. A regression test guards it.

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
* **No real outcomes yet.** The outcome tracker works and has been run end to end
  on 310 signals — but all of them came from the synthetic feed. No signal has
  ever been graded against a real market.
* **Signals older than 1000 primary-timeframe bars become UNRESOLVABLE**, because
  that is the deepest a single klines request reaches. At the 5m default that is
  roughly 3.5 days: run resolution at least that often or verdicts are lost.
* **DEX scanning is not implemented.** The interface is declared;
  `risk/safety.py:dex_safety` raises rather than returning a plausible default.
* **Order flow is REST snapshots**, not a streamed book. Depth carries no venue
  timestamp, so fetch time is recorded as the observation time and labelled as such.
* **Postgres is untested.** SQLite is verified; the Postgres path is configuration
  only.
* **Two venues, no cross-validation yet.** Binance and Kraken are both
  implemented and switchable with `--provider`, but nothing yet compares their
  quotes against each other to catch a bad feed. That is Phase 2.

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
