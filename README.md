# CRYPTO PULSE AI

An early-acceleration crypto scanner. It tries to answer one question:

> **Which asset is changing behaviour right now, before the rest of the market notices?**

Not "what pumped the most today" — that list is always full of moves you have
already missed. This scanner is built around the opposite bias: it measures
*derivatives* (is volume accelerating? is the score rising?) and actively
penalises moves that are already extended.

```
DATA → SCAN → FILTER → ENRICH → SCORE → VALIDATE → RANK → ALERT → BACKTEST
```

It scans **only what you can actually buy on Robinhood Crypto**, and it reads two
horizons at once, kept deliberately apart:

* the **opportunity score** — the next few hours: a level about to give way;
* the **×10 radar** (`MOONSHOT_ENGINE_V1`) — the next few weeks: an asset that
  currently *looks like* assets have looked before large expansions.

It runs unattended (`cryptopulse radar`) and pushes alerts to your phone.

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

**5. The ×10 radar is the least validated part of this system.** A ten-fold move
is a rare event: a year passes with none, then two happen in a fortnight.
Nothing here changes that base rate. The moonshot score ranks *resemblance to a
pre-expansion state* under weights that were reasoned about, never fitted, and
never graded against a single real ×10 outcome. **Most of what it flags will not
do ×10.** That is a property of the market, not a defect — and a scanner
implying otherwise would be lying to you.

**6. Prices do not come from Robinhood.** Robinhood publishes no usable public
market-data API, so the *universe* is filtered to what Robinhood lists while the
*candles* come from Binance or Kraken. Robinhood's spread and fill will differ,
sometimes materially on a thin asset during a fast move. The listing itself is
hand-maintained and has never been verified against Robinhood — run
`cryptopulse universe` to see exactly what is and is not being scanned.

**7. This is not financial advice and places no orders.** `PAPER_MODE` is on by
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

**Scans only what you can buy.** The universe defaults to assets believed
tradable on Robinhood Crypto, resolved against the venue's own naming (Kraken
calls bitcoin XBT and dogecoin XDG; MATIC became POL). Anything the venue does
not carry is reported by name, never silently dropped.

**Hunts large multiples on a separate axis.** The ×10 radar reads the *daily*
chart and answers three questions it keeps apart: has this traded ten times
higher than it does now (arithmetic, not a forecast)? Is a ×10 payable at this
market cap (unknown — and clearly labelled unknown — unless a valuation source
is configured)? Is the behaviour changing right now: volume arriving on a base
that has been quiet for months, a multi-month level giving way, pullbacks
tightening, strength against the market? Then it names a stage —
ACCUMULATION / IGNITION / EXPANSION / EXHAUSTION — because a 78 that is late and
a 78 that is early are opposite situations.

**Knows the difference between quiet and dead.** OBV, the Chaikin A/D line and
CMF are read together with volume slope: volume building while price refuses to
move, with bars closing in the upper half of their ranges, is the tape that
precedes a markup. Volume building on closes at the lows is not.

**Compares against the market, not just against itself.** Relative strength
versus the benchmark and a cross-sectional RVOL percentile are computed each
scan, so "2.5x volume" is judged against what every other asset is doing today.

**Runs without you.** `cryptopulse radar` loops unattended, surviving provider
outages with exponential backoff, and delivers alerts to console, a JSONL
journal, a webhook, Telegram or Discord. It prints where alerts will go *before*
it starts, so a misconfigured channel is discovered at 09:00 and not at 03:14.

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
python -m cryptopulse.cli resolve                      # the intraday axis (hours)
python -m cryptopulse.cli resolve --axis moonshot      # the ×10 axis (weeks, daily bars)

# 5. THE AUTONOMOUS RADAR — scan, alert, notify, repeat until stopped
python -m cryptopulse.cli radar
python -m cryptopulse.cli radar --interval 300 --rank moonshot
python -m cryptopulse.cli radar --once            # a single cycle, then exit

# 6. What the Robinhood filter actually resolves to on your venue
python -m cryptopulse.cli universe
python -m cryptopulse.cli universe --refresh      # try Robinhood's own catalogue

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

| Rank | Asset | Price | 5m | 1h | RVOL | Opportunity | ×10 | Accel | Maturity | Safety | Conf | Status |

**Top opportunities now** — ranked by setup quality, not by price change. When
nothing clears the gates it says so, rather than promoting the best of a bad list.

**×10 Radar tab** — one card per asset: the stage as prominently as the score,
three separate bars for ignition / headroom / capacity (with `unknown` rendered
as unknown, never as an empty bar), the multiple to the window high, market cap
where known, the reasons, the caveats, and an explicit list of what could not be
measured. A permanent banner states that it is a ranking and not a forecast, and
a second one appears whenever market cap is missing for every row.

**Asset detail** (click any row) — full score explainability with per-component
bars and the `RAW − PENALTY = FINAL` arithmetic, score history sparkline,
multi-timeframe bias grid, market data, and two explicit sections:
**Why this asset?** and **What can invalidate it?**

**Freshness bar** — always visible: API status, data source, last update, scan
age, market data age, scanned/failed counts, universe mode and size, market
regime, paper mode. A stale feed or a synthetic source raises a permanent
banner, and so does the Robinhood universe — stating that prices come from the
data venue and not from Robinhood.

---

## Data sources

| Provider | Type | Status |
|---|---|---|
| Binance Spot public REST | Market data + order book | IMPLEMENTED, **not live verified** |
| Kraken public REST | Market data + order book | IMPLEMENTED, **not live verified** |
| Fixture generator | Synthetic candles | For tests and offline dev only |
| CoinGecko `/coins/markets` | Market cap, FDV, supply | IMPLEMENTED, **not live verified**, OFF by default |
| Robinhood `/currency_pairs/` | Tradable listing (universe only, no prices) | Best-effort refresh; has never succeeded from this sandbox |
| DexScreener / Birdeye | — | NOT IMPLEMENTED |

Endpoints used (all public, unauthenticated): `/api/v3/ping`,
`/api/v3/exchangeInfo`, `/api/v3/ticker/24hr`, `/api/v3/klines`, `/api/v3/depth`.

Swapping providers means writing one class against `MarketDataProvider` in
`providers/base.py` and flipping `CP_PROVIDER_MARKET_DATA`. Nothing above the
provider layer knows the name of an exchange.

### The candle cache

A closed bar never changes, so a series is re-read only once the next bar of its
timeframe has closed — not on a timer. Over ten one-minute passes on the
Robinhood universe that removes **57% of kline requests**, and the daily
timeframe is read once instead of ten times. Without it, enabling the ×10 layer
would mean re-downloading 400 daily candles per asset every minute for data that
changes once a day.

Never cached: order books (a depth snapshot is a statement about *now*), 24h
tickers (one request covers the whole venue), and `doctor` (proving the live API
works cannot be done against a cache). The hit rate is in `/api/health` under
`candle_cache`.

### Running it unattended

`deploy/README.md` covers systemd and Docker. The short version of what makes it
safe to leave alone:

* **`/healthz` answers 503 once the radar stops scanning** — not merely when the
  process dies. A process that is up and no longer scanning is the failure that
  matters, because you keep trusting a screen that stopped updating.
* **The watchdog tells you itself**, through the same alert channels as a
  signal, once per outage and again on recovery.
* **Score history survives a restart.** It is reloaded from the database at
  startup, because score acceleration is a difference between two passes and a
  cold process would report every asset as flat.
* **Signals are never deleted on a retention timer.** A ×10 label can take 180
  days to settle; only score points are purged.

### Provider resilience

Weighted rate limiter (budget on request *weight*, not count), exponential
backoff with full jitter, automatic failover to the market-data mirror, and a
circuit breaker that stops hammering a provider that is down. A 4xx is never
retried — it is our bug, not a transient failure.

---

## Testing

```bash
pytest -q                            # 406 tests
CP_TEST_POSTGRES_URL=... pytest -q   # + the PostgreSQL round trip
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
| `test_expansion.py` | 13 | Base length, drawdown multiple, VCP, spring, quiet accumulation, truncation stability |
| `test_moonshot.py` | 20 | What the ×10 layer refuses to claim: no daily base from a 5m chart, no guessed market cap, no early-entry label on an exhausted move |
| `test_universe.py` | 16 | Snapshot integrity, alias resolution (XBT/XDG/POL), missing assets reported, both universe modes end to end |
| `test_notifiers.py` | 11 | Delivery per channel, crash isolation, and that no secret reaches a log or a result |
| `test_valuation.py` | 10 | CoinGecko parsing, ticker collisions, cap upper bound, scan survives an outage |
| `test_radar.py` | 18 | Env-list parsing, enrichment, moonshot alerts and cooldown, a full radar cycle, the new endpoints |
| `test_moonshot_outcomes.py` | 16 | Multiple-based labels, cross-timeframe grading, the ×10 journal, two independent verdicts |
| `test_cache.py` | 19 | A cached series is identical to an uncached one; bar-boundary validity; what must never be cached |
| `test_operations.py` | 16 | Liveness verdicts, the watchdog firing once per outage, score memory across a restart, retention |
| `test_postgres.py` | 8 | Millisecond columns are wide enough for a real timestamp; full round trip on a live server |

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

## The ×10 radar — `MOONSHOT_ENGINE_V1`

A separate score on a separate horizon, shown next to the opportunity score and
never blended into it.

### Three readings, kept apart

| Reading | The question | Where it comes from |
|---|---|---|
| **Headroom** | Has this traded ×10 above the current price, inside the history we can see? | Arithmetic on the daily candles. Not a forecast: a token 92% below a price it printed last cycle needs the market to change its mind, not to invent a valuation. |
| **Capacity** | Is a ×10 *payable*? | Market cap. ×10 on $20M is $200M — that happens somewhere most weeks. ×10 on $40B is $400B — almost nothing has ever reached it. This cannot be derived from a candle, so **without a valuation source it reads `unknown`, never zero.** |
| **Ignition** | Is the behaviour changing *right now*? | Eleven weighted sub-signals (below). |

### The eleven ignition sub-signals

| Sub-signal | What it looks for |
|---|---|
| Volume regime | Daily volume vs its own 30-bar median — a shift, not a busy bar |
| Level break | A close above the 60-day high, or the top of the current base |
| Accumulation | CMF + A/D slope + volume building into flat price with upper-half closes |
| Base maturity | How many daily bars the price has spent inside one range |
| Trend reclaim | Price back above EMA50 with EMA20 crossed up |
| Relative strength | Out- or under-performance vs the benchmark over the same window |
| Compression | Bollinger width in the tightest part of its own recent range |
| VCP | Successive pullbacks getting shallower — supply drying up |
| Volume rank | RVOL as a percentile of every asset in the same scan |
| MTF alignment | Whether the faster timeframes agree |
| Spring | A failed breakdown: lost the base floor, closed back above it |

### Stages — the order is the safety property

`EXHAUSTION` → `EXPANSION` → `IGNITION` → `ACCUMULATION` → `DORMANT` → `NEUTRAL`
(`UNKNOWN` when no timeframe of 4h or slower is available).

Lateness is evaluated **first** and overrides everything else: an asset can be
igniting on every measure and still be something you are late to. EXHAUSTION
caps the score at 45, EXPANSION at 70. Only **IGNITION** and **ACCUMULATION**
can raise an alert — the two stages where an entry is still early.

### What it refuses to do

* score a daily base off a five-minute chart (returns `UNKNOWN` and says why);
* treat a missing market cap as a small one;
* call a coin already up 400% an early entry;
* override the liquidity or safety gates. A vetoed asset can carry a moonshot
  score of 95 and still never alert.

### How it gets graded

Every reading is written to the journal — including on assets whose intraday
setup state is IGNORE, which is the normal state of a dormant base and exactly
what this layer looks for. Verdicts land in their own column set, graded on
**daily** bars by a ladder of multiple-based labels:

| Label | Target | Stop | Horizon |
|---|---|---|---|
| `moon_2x_30d` (default) | ×2 | −35% | 30 days |
| `moon_3x_90d` | ×3 | −50% | 90 days |
| `moon_10x_180d` | ×10 | −60% | 180 days |

Grading directly at ×10 would settle nothing for years, so the ladder settles
earlier — and every row records the **highest multiple actually reached**. That
one field keeps "how many ever reached ×10" answerable from the journal:

```
  How far did they actually go?
    reached x1.5       1     0.5%
    reached x2         0     0.0%
    reached x10        0     0.0%

  ** No reading has reached x10. The best was x1.52 — which is the honest
     headline for this layer until one does.
```

**It has never been validated.** The machinery to grade it now exists and is
tested; what does not exist is real signals to grade. Read it as a watchlist
ranking, and nothing more.

Once the feed is verified, the layer is validated in this order — backtest on
real daily history, compare against random daily entry, then accumulate a live
journal:

```bash
CP_SCAN_PRIMARY_TIMEFRAME=1d CP_SCAN_TIMEFRAMES=4h,1d \
  python -m cryptopulse.cli backtest --label moon_2x_30d --bars 800
```

The decision timeframe has to be slow too: 1000 five-minute candles reach back
three days, which cannot host a 30-day horizon. The backtest says so explicitly
rather than returning an empty result that reads as "found nothing".

---

## Alerts that actually reach you

Two kinds, on two clocks:

* **SETUP** — the intraday setup engine: thresholds, gates, 30-minute cooldown.
* **MOONSHOT** — the daily ×10 reading: its own threshold and a six-hour
  cooldown, because a multi-month base is not news again fifteen minutes later.

Both pass the same gates — liquidity, safety and data confidence are not
negotiable on either horizon.

Delivery fans out to any of five channels, each isolated so one failure cannot
break a scan or the others:

```bash
CP_ALERT_CHANNELS=console,jsonl,telegram
CP_ALERT_TELEGRAM_BOT_TOKEN=...      # @BotFather
CP_ALERT_TELEGRAM_CHAT_ID=...        # @userinfobot
```

`jsonl` appends one JSON object per alert to `data/alerts.jsonl` — tailable
while the radar runs, and a record independent of the database. Secrets never
reach a log, an error string or an API response; an unconfigured channel is
reported by naming the *setting* that is missing, and `cryptopulse radar` prints
that at startup.

---

## The Robinhood universe

```bash
python -m cryptopulse.cli universe
```

```
Robinhood universe — 34 base assets   (source: snapshot, as of 2026-08-31)
Resolving against kraken / USD...
  34 tradable here:
    BTC      XBTUSD        24h vol    1,204,882,001   (listed as XBTUSD)
    DOGE     XDGUSD        24h vol       88,120,433   (listed as XDGUSD)
    ...
  3 not carried by this venue against USD: PENGU, TRUMP, ONDO
```

Aliases are resolved against the symbols the venue actually returned, so a
rename in either direction (XBT/BTC, XDG/DOGE, POL/MATIC, RNDR/RENDER) is
absorbed rather than silently dropping the asset.

The listing is a hand-maintained snapshot. Correct it with
`CP_SCAN_ROBINHOOD_EXTRA` / `CP_SCAN_ROBINHOOD_EXCLUDE`, or maintain your own
file:

```bash
python -m cryptopulse.cli universe --refresh   # writes data/robinhood_universe.json
export CP_SCAN_ROBINHOOD_FILE=data/robinhood_universe.json
```

Set `CP_SCAN_UNIVERSE=volume` to get the V1 behaviour back — the venue's most
liquid pairs, ignoring Robinhood entirely.

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
* **The ×10 layer has never been graded on real data.** The machinery now exists
  — readings are journalled, a daily ladder grades them over 30/90/180 days, and
  every row records the multiple reached — but it has only ever run on synthetic
  candles, where nothing doubles in a month and the layer therefore scores level
  with random daily entry. `MOONSHOT_ENGINE_V1` stays an untested hypothesis
  with a stage machine until real signals settle.
* **Market cap is missing unless you enable a valuation source.** With
  `CP_PROVIDER_VALUATION=none` (the default) the capacity reading — arguably the
  most important single input to "can this actually do ×10?" — is unknown for
  every asset, and the score is computed from the other two readings.
* **The Robinhood listing is hand-maintained and unverified.** Robinhood has no
  usable public market-data API. `cryptopulse universe` shows exactly what is
  being scanned; correct it with `CP_SCAN_ROBINHOOD_FILE`.
* **Telegram, Discord and webhook delivery are tested against mocks only.** No
  message has ever left this sandbox.
* **Enabling the ×10 layer costs one extra kline request per asset per scan** —
  the daily. On a 120-symbol volume universe that is 120 more requests a minute;
  on the ~35-asset Robinhood universe it is negligible. Set
  `CP_MOON_ENABLED=false` if the budget is tight.
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
