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
| React dashboard | **IMPLEMENTED** | Mobile-first. Builds clean, screenshotted at 412px and 1500px, zero horizontal scroll |
| **Binance connector** | **IMPLEMENTED — NOT LIVE VERIFIED** | Contract cross-checked vs python-binance 1.0.37; still no live round-trip. See §3 |
| Fixture (synthetic) provider | **TESTED / MOCKED** | Time-anchored, Brownian. Generates data, never market data |
| **Kraken connector** | **IMPLEMENTED — NOT LIVE VERIFIED** | 23 tests incl. 2 full-pipeline. Contract cross-checked vs ccxt 4.5.73 |
| Order book / order flow | **IMPLEMENTED — NOT LIVE VERIFIED** | Parsing tested against mocks |
| DEX scanner | **NOT IMPLEMENTED** | Interface declared in `providers/base.py` |
| DEX safety scoring | **NOT IMPLEMENTED** | `risk/safety.py:dex_safety` raises deliberately |
| Score calibration / probabilities | **NOT IMPLEMENTED** | Needs a real signal history first |
| Machine learning | **NOT IMPLEMENTED** | Deliberately deferred, see §9 |
| Outcome tracker | **TESTED** | 23 tests; run end to end on 310 signals |
| Multi-horizon verification (`outcomes/horizons.py`) | **TESTED** | 25 tests; run end to end on 1240 windows |
| Performance analytics (`outcomes/stats.py`) | **TESTED** | Buckets + component edge + horizon buckets, `n` on every rate |
| Verdict (`scoring/verdict.py`) | **TESTED** | 15 tests. Compression of existing gates; introduces no new opinion |
| Android notifications (`alerts/notify.py`) | **IMPLEMENTED — NOT DEVICE VERIFIED** | 22 tests against a stand-in binary. Never run on Android; `cryptopulse notify` is the check |
| Alert delivery (`alerts/delivery.py`) | **TESTED** | 14 tests + one real-socket round trip. Discord / Slack / generic JSON |
| Retention (`repo.prune`) | **TESTED** | 12 tests. Never prunes a signal that still owes an answer |
| Warm start (`repo.last_scan_snapshot`) | **TESTED** | 11 tests. Restored rows keep their age and their provenance |
| Candle cache (`providers/cache.py`) | **TESTED** | 20 tests. 88.8 % of kline requests removed, output proven identical |
| Token Hunter pre-scan (`hunter/discovery.py`) | **TESTED** | 21 tests. Whole venue ranked by anomaly, 0 extra requests |
| `TokenDiscoveryProvider` | **IMPLEMENTED** | Binance / Kraken / fixture. The seat Robinhood Chain plugs into |
| Deep scan (`hunter/deep.py`) | **TESTED** | Reuses the scan's own results; states its request cost |
| `TOKEN_DISCOVERY_SCORE` (`scoring/discovery.py`) | **TESTED** | 18 tests. DISCOVERY_ENGINE_V1, weights are a hypothesis |
| Explosion 15m (`scoring/explosion.py`) | **TESTED** | 18 tests. EXPLOSION_ENGINE_V1. The only score whose window is already measured |
| Manual validation (`repo.save_validation`) | **TESTED** | 13 tests. Append-only; the row is a photograph of the screen |
| Pump history (`pumps/detect.py`) | **TESTED** | 18 tests. Threshold-free episodes on 1h; 23-38 found per token |
| Pump similarity (`pumps/stats.py`) | **TESTED** | Refuses any rate below n=20, which is the common case |
| PWA (manifest + service worker) | **TESTED** | 11 tests. Verified installable in a real browser on 127.0.0.1 |
| Trade decision (`trading/decision.py`) | **TESTED** | 30 tests. TRADE_DECISION_V1. Six decisions; convergence required |
| Position health + exit risk | **TESTED** | 22 tests. Health is not PnL; invalidation outranks everything |
| Positions + hysteresis | **TESTED** | 30 tests. Append-only journal; twenty alternating readings move nothing |
| Position watcher + trading API | **TESTED** | 28 tests. Open positions only; states its request cost |
| Trading statistics (`trading/stats.py`) | **TESTED** | Taken vs skipped, per strength, sell quality. No rate below n=20 |
| Startup sequencing (`api/startup.py`) | **TESTED** | 16 tests. Phase timings; the screen never waits for the network |
| Feed verification (`providers/verify.py`) | **TESTED** | One definition of LIVE VERIFIED, shared by `doctor` and the service |
| Schema migration (`database/migrate.py`) | **TESTED** | Additive columns only; refuses destructive changes |
| Hybrid Android scripts (`scripts/android_env.sh`) | **TESTED — NOT DEVICE VERIFIED** | 32 tests incl. a fake `proot-distro` recording argv. Termux build / PRoot backend split. Never run on Android |
| **Robinhood Chain RPC (`providers/robinhood.py`)** | **IMPLEMENTED — NOT LIVE VERIFIED** | 17 tests vs mocked JSON-RPC. Chain id 4663. Host refused by this sandbox (403, same as Binance was); `doctor-robinhood` is the check |
| Robinhood Chain doctor (`providers/robinhood_verify.py`) | **TESTED** | 🟢 VERIFIED / 🟡 PARTIAL / 🔴 FAILED — core vs liveness split. Independent of Binance by construction |
| **GeckoTerminal client (`providers/geckoterminal.py`)** | **IMPLEMENTED — NOT LIVE VERIFIED** | Contract cross-checked vs `geckoterminal-api` 0.9.0 + published docs. Host refused by this sandbox |
| Robinhood token discovery (`hunter/robinhood_discovery.py`) | **TESTED** | 25 tests. Age buckets, address identity, NULL never 0. Run end to end against a stubbed indexer only |
| **GoPlus client (`providers/goplus.py`)** | **IMPLEMENTED — NOT LIVE VERIFIED** | Contract read from GoPlus's own Swagger SDK (`goplus` 0.2.5). Host refused by this sandbox |
| `ROBINHOOD_SAFETY_V1` + RUG_RISK (`risk/robinhood_safety.py`) | **TESTED** | 37 tests. Tri-state flags, veto zeroes the score, unknown never safe. Weights are a hypothesis |
| **DexScreener client (`providers/dexscreener.py`)** | **IMPLEMENTED — NOT LIVE VERIFIED** | Contract cross-checked vs `dexscreener` 1.3. Host refused by this sandbox |
| Source cross-check (`hunter/robinhood_detail.py`) | **TESTED** | 16 tests. Two sources side by side, disagreement surfaced and never averaged |
| `ROBINHOOD_EARLY_SCORE_V1` (`scoring/robinhood_early.py`) | **TESTED** | 25 tests. Flow, not candles. Favours the start of a move; weights are a hypothesis |
| Robinhood maturity + data confidence | **TESTED** | Three separate readings of one snapshot, allowed to disagree (spec §52) |
| `ROBINHOOD_EXPLOSION_15M_V1` (`scoring/robinhood_explosion.py`) | **TESTED** | 15 tests. Horizon in the fingerprint; a selling burst never scores as upside |

| `ROBINHOOD_TRADE_DECISION_V1` (`trading/robinhood_decision.py`) | **TESTED** | 45 tests. Same six decisions and colours as Binance, own floors and own fingerprint. Security outranks every score |
| Robinhood position health + exit risk (`trading/robinhood_health.py`, `robinhood_exit_risk.py`) | **TESTED** | 34 tests. Sellability and pool depth have no CEX analogue; a catastrophic reading caps the score |
| Robinhood position watcher (`trading/robinhood_watcher.py`) | **TESTED** | Held tokens only. One indexer request per token, one batched safety request. States its cost |
| Robinhood outcomes (`outcomes/robinhood_outcomes.py`) | **TESTED** | 24 tests. Resolves exactly from pool OHLCV, so a decision from last week is gradeable today |
| Robinhood statistics (`outcomes/robinhood_stats.py`) | **TESTED** | `by_action` is the table that matters: 🟢 must beat 🟡 must beat ⚫. Every rate carries its `n` |
| Robinhood notifications (`alerts/robinhood_notify.py`) | **TESTED — NOT DEVICE VERIFIED** | 30 tests against a stand-in binary. 🟢 once per token, 🔴/🟠 never rate-limited |
| Robinhood retention + migration | **TESTED** | 12 tests. Never prunes a decision still owing a window; new tables land on an old database |
| **French localisation (`cryptopulse/i18n/`, `frontend/src/i18n/fr.ts`)** | **TESTED** | 46 anti-English tests. 240 catalogue entries + 50 enum labels + 42 UI labels. Rendered at emission, no network, no model |

**Test suite: 1010 tests, all passing.** Run `pytest -q`.

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
    cache.py             Closed candles only. Never the forming one. `doctor` bypasses it
    fixture.py           SYNTHETIC generator for offline dev/tests
    verify.py            Is this feed really live? One definition, two callers
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
    verdict.py           🟢/🟡/🟠/🔴 in four words, computed last, caveat attached
    discovery.py         TOKEN_DISCOVERY_SCORE — has this behaviour just changed?
    explosion.py         EXPLOSION_15M_SCORE — is it about to move, within 15 min?
    engine.py            SCORE_ENGINE_V1 orchestrator + weights fingerprint
  risk/
    liquidity.py         Liquidity gate, DANGEROUS => veto
    safety.py            CEX safety score; DEX surface declared, not implemented
    penalties.py         RAW - PENALTY = FINAL, every deduction named
  scanner/
    base.py              Scanner ABC + ScanReport
    cex.py               Two-pass pipeline (cheap filter, then order books)
    memory.py            Score history ring buffer, score acceleration
  alerts/
    engine.py            Levels, gates, dedup, cooldown
    delivery.py          Webhook delivery. The URL is a credential and is never logged
    notify.py            Android notifications via Termux:API. Escalation-only anti-spam
    robinhood_notify.py  🟢 once per token, 🔴/🟠 never delayed. Builds the notice
  hunter/
    discovery.py         Wide pre-scan. Reads the scan's own ticker call, costs nothing
    deep.py              The expensive look, only on selected candidates
  pumps/
    detect.py            Episodes, not thresholds. 1h = the shortest usable sample
    stats.py             Descriptive stats + similarity, both carrying their n
  trading/
    decision.py          TRADE_DECISION_V1 — six decisions, convergence not a maximum
    health.py            POSITION_HEALTH — is the buy thesis still standing?
    exit_risk.py         Named deterioration signals, counted by severity not summed
    hysteresis.py        Confirmation + cooldown + escalation exemption
    watcher.py           Open positions only, 15s, states its request cost
    stats.py             Engine quality vs user results, deliberately kept apart
    robinhood_decision.py  ROBINHOOD_TRADE_DECISION_V1 — same six words, other evidence
    robinhood_health.py    Sellability + pool depth. A catastrophe caps, never deducts
    robinhood_exit_risk.py Named signals. Two of them have no CEX analogue at all
    robinhood_watcher.py   Held tokens only. Discovery never looks at them again
  outcomes/
    robinhood_outcomes.py  Grades Robinhood decisions from pool candles, exactly
    robinhood_stats.py     Does 🟢 beat 🟡 beat ⚫? The only falsifying table here
    tracker.py           Grades emitted signals against the bars that followed
    horizons.py          What the price did 15m/1h/4h/24h later — path, not verdict
    stats.py             Win rate / expectancy by bucket, per-component edge,
                         and the per-horizon success/median/best/worst tables
  backtest/
    labels.py            Triple-barrier, ATR-scaled, pessimistic on ambiguity
    metrics.py           Expectancy, PF, DD, Sharpe/Sortino (>=20 trades only)
    splits.py            Chronological split + walk-forward with embargo
    engine.py            Replays the live scorer over history via series.upto()
  database/
    models.py            signals / score_points / alerts / scan_runs / validations
                         trade_signals / positions / position_events
    migrate.py           Additive ALTER TABLE for columns create_all cannot add
    session.py           SQLAlchemy engine
    repo.py              The only module that writes; `prune` applies retention
  api/
    startup.py           Phase timings. An absent phase is null, never zero
    service.py           Owns the scanner, the loop and shared state
    app.py               FastAPI routes, serves the built dashboard
  i18n/
    __init__.py          Text (str + .code + .params), msg(), num/pct/mult/money/price
    reasons.py           240 French templates, one per reason code, keyed by engine
    labels.py            Enum -> French. Keyed on plain strings, so it imports nothing
  cli.py                 doctor / scan / resolve / verify / notify / serve / backtest

start.sh                 One-command launcher: install, verify feed, scan
scripts/android_env.sh   The Termux/PRoot split. cp_run_in_ubuntu / cp_run_in_termux
android-install.sh       Android, once: Ubuntu, venv, deps, icons, build, blocking doctor
android-update.sh        Android, after a git pull: backup, deps, rebuild if needed
android-start.sh         Android, daily: starts. Nothing else. ~1s to HTTP

frontend/public/
  manifest.webmanifest   PWA manifest. standalone, maskable icons, fr
  sw.js                  Shell cache only. NEVER caches /api
  icons/                 Generated by scripts/make_icons.py, no image library

scripts/
  simulate_journal.py    Scan across simulated time, grade, compare to baseline

frontend/                Vite + React 18 + TypeScript (strict), mobile-first
  src/i18n/fr.ts         Chrome labels + labelFor(). No fallback to the identifier
  src/format.ts          The one place the decimal comma and the narrow space live
  HomeView               The five-second view: can I trust it, and what moved
  AssetCards             The scanner as cards; the table is wide-screen only
  bottom-nav             Thumb-reachable navigation, phones only
tests/                   1010 tests
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

14. **A horizon window that has not fully elapsed is absent, never settled.**
    `HorizonTracker` returns PENDING and `save_horizons` refuses to write it.
    Reporting the current price for an unfinished window turns "not yet" into a
    result.

15. **The horizon success rule lives in exactly one place** —
    `HorizonResult.is_success`, net change above zero after costs. Storage and
    statistics both read it, so the criterion cannot drift between them. A
    window with no verdict returns `None`, never `False`.

16. **Every verdict carries its caveat, including the green one.** A coloured
    badge with no disclaimer is precisely how a ranking gets read as a
    prediction. A test asserts the caveat on all four levels.

17. **A webhook URL is a credential and never leaves `alerts/delivery.py`.**
    A Discord or Slack webhook URL contains its own bearer token. Only
    `redacted()` output — scheme and host — appears in a log, an error, or an
    API response. `str(exc)` from httpx embeds the request URL, so failures
    report the exception *type* and deliberately not its message.

18. **Notification failure costs a notification, never a scan.** Delivery is the
    last step of the cycle and every path returns a `DeliveryReport` instead of
    raising. A failure is recorded and surfaced on `/api/health` — never silent,
    because a dead webhook otherwise looks exactly like a quiet market.

19. **Pruning never deletes a signal that still owes an answer.** A row without a
    verdict, or missing any of its four horizon windows, is kept however old it
    is. Those are precisely the rows about to become evidence, and their loss
    would look like a quiet journal rather than a bug.

20. **A restored scan keeps the provenance of its rows, not of the process.**
    `/api/scan` serves the journal until this process has scanned, so the screen
    is never blank after a restart. The banner follows what is *displayed*: a
    snapshot written by the synthetic provider stays DEMO even when a real feed
    is configured. Restored rows are a genuine subset — fields never journalled
    are `None`, never zero — and are marked `from_journal`.

21. **The candle cache stores closed candles and nothing else.** A closed candle
    is immutable, so serving it from memory is the same answer, not an
    approximation. The forming candle never enters the cache, an entry is
    refused the moment a new bar could have closed, and age is still derived
    from each candle's own `close_time_ms` — so cached data gets older on its
    own and can never look fresh. A test asserts a full scan produces output
    identical with and without the cache, field by field.

22. **The cache key includes the requested `limit`.** A 300-bar fetch yields 299
    closed bars, so one entry cannot correctly answer both a 300-bar and a
    100-bar request — slicing the tail would return a window one bar longer than
    the network would have. Keying on the limit makes equality hold by
    construction.

23. **The DEMO warning is present on every screen, in some form.** The home tab
    drops the banner only because its own trust line, rendered unconditionally
    and in the same amber, says the same thing immediately above — repeating it
    was noise on a phone. Every other tab keeps the full banner. Verified across
    all five tabs before shipping; if a future change removes that trust line,
    the banner suppression must go with it.

24. **A rolling-counter delta is named for what it measures.** `quoteVolume` and
    `count` are 24-hour rolling figures, so the difference between two readings
    is the excess over *the same moment yesterday*, not volume in the last
    minute. The field is `volume_excess_vs_yesterday` and a test forbids any
    `volume_1m`-style name. It is a genuine anomaly detector; calling it recent
    volume would be a fabrication.

25. **Reading the search never advances its memory.** Found by running the
    service: an on-demand hunt that recorded a snapshot overwrote the previous
    reading, so tapping "search again" seconds after a scan erased the very
    acceleration signal being asked for. `/api/hunt` serves the cycle's stored
    report; only the scan cycle records.

26. **The hunter's `priority` is a selection heuristic, not a score.** It answers
    "is this worth four kline requests?" and carries no version or weights
    fingerprint, because it is not an opinion about the asset. `TOKEN_DISCOVERY_SCORE`
    (phase 05) will be, and will read the price history this stage never fetches.

27. **Discovery and opportunity are two engines, never one number.** They answer
    different questions — "has this token's behaviour just changed?" versus "is
    this a good setup?" — over different horizons, and a large-cap in a clean
    retest legitimately scores high on one and near zero on the other. Blending
    them would make it impossible to learn later which of the two carried any
    signal. Each has its own version and weights fingerprint, and the UI shows
    both with their question underneath.

28. **The deep scan never pays twice.** A candidate the classic scan already
    analysed this cycle is reused, and the report states `kline_requests`
    exactly. A search that quietly spent hundreds of requests would be
    discovered as a rate-limit ban rather than as a number.

29. **Pump detection records size instead of testing a threshold.** One pass
    over the candles answers "+3%?" and "+20%?" alike, so the threshold never has
    to be chosen before anything is known.

30. **1h is the pump timeframe because it is the shortest that yields a sample.**
    Binance caps a request at 1000 bars, so the timeframe *is* the depth: 5m
    gives 3.5 days, 15m gives 10.4, 1h gives 41.7. Below 1h the statistics would
    read "insufficient sample" forever. The cost is timing resolution, so every
    episode carries `resolution_minutes` and nothing renders minute precision it
    does not have.

31. **A past setup is described only by what was knowable then.** The context on
    a `PumpEpisode` — RVOL, volume change, range position, ATR — is computed
    strictly from bars at or before the trough. Using anything from during the
    run would make similarity circular: it would discover that pumps are
    preceded by pumps, and it would look convincing. A test changes only the
    future and asserts the recorded context does not move.

32. **Below `MIN_SAMPLE` the similarity block returns no rate at all** — not a
    greyed-out one. A number on screen gets read however it is styled, and on a
    few weeks of history an insufficient sample is the ordinary outcome.

33. **The service worker never caches `/api`.** Not stale-while-revalidate, not
    network-first-with-fallback — never. Every API response is a price, a score
    or a data age, and a cached one would be neither current nor labelled old,
    which is the exact failure this project exists to prevent. Only the
    content-hashed build shell is cached, where a hit is byte-identical by
    construction. A test asserts the API bail-out precedes any caching logic,
    and a browser run asserts no `/api` entry ever lands in the cache.

34. **PWA installability needs a secure context, and `127.0.0.1` is one.** The
    spec treats loopback as secure, which is what makes installation work on the
    phone with no certificate. Reaching the same server over the LAN
    (192.168.x.x) is *not* secure, so the app runs but cannot be installed —
    `installability()` reports which case you are in rather than failing
    silently. `android-start.sh` binds 127.0.0.1 for this reason, and because it
    keeps the scanner off the Wi-Fi.

35. **`/sw.js` is served with its real content type, before the SPA catch-all.**
    A worker delivered as `text/html` is rejected by the browser with no error
    the user ever sees: the app simply never becomes installable, and nothing
    anywhere explains why.

36. **LIVE vs DEMO is decided server-side.** `status()["data_mode"]` is the
    single source; the dashboard never infers it from a provider name it happens
    to recognise, or a new synthetic source would slip past the banner.

37. **Three engines, three questions, three fingerprints — never one number.**
    Opportunity asks "is this a good setup?", Discovery "has this behaviour just
    changed?", Explosion "is this about to move within fifteen minutes?". They
    disagree constantly and that is the point. Averaging any two would produce a
    number describing neither, and would make it impossible to learn later which
    of the three carried signal.

38. **The explosion score's horizon is part of its identity, not a parameter.**
    `EXPLOSION_HORIZON_MINUTES` goes into the weights fingerprint, because the
    same weights over a different window are a different claim. It is 15 minutes
    because `outcomes/horizons.py` has been measuring exactly that window since
    phase 15 — this is the only score in the project that can be shown to be
    wrong, and `build_horizon_performance()["by_explosion_band"]` is where that
    happens.

39. **The explosion engine reads 5m and 15m and nothing slower.** A 4h indicator
    cannot say anything about the next quarter hour. A test adds 4h and 1d
    timeframes to an asset and asserts the score does not move by a point; that
    is the guard against this quietly becoming a second opportunity score.

40. **A hard gate zeroes the explosion score rather than reducing it.** On a
    fifteen-minute horizon an illiquid token's ten-percent candle is a trap, and
    a merely-reduced score would still let it outrank a tradeable one. The zero
    always carries its reason — a silent zero is indistinguishable from a calm
    market.

41. **A validation row is a photograph of the screen, not a pointer to a scan.**
    The price, all three scores, the verdict, the reasons and the invalidation
    are copied in at the moment of the decision. Joining to `signals` instead
    would look tidier and would be wrong: a token in IGNORE has no signal row to
    join to, the scan behind the decision may be pruned, and a reweighted engine
    would re-explain a past decision in words the user never read.

42. **Validations are append-only and never pruned.** Changing one's mind writes
    a second row, because the sequence is the part worth studying. `prune` does
    not touch the table at all: a person's judgement is low-volume, cannot be
    recomputed, and is the one thing here that re-running the scanner could not
    regenerate.

43. **No success rate is shown for the user's own decisions.** Not even above
    n=20. A percentage over a handful of judgements would be read as a verdict on
    the reader's own skill, which is the most misleading number this product
    could display. The Verification tab says what the price did; the Decisions
    tab says only what was decided, and when.

44. **Notifications escalate or stay silent.** The alert engine deduplicates
    and applies a cooldown; the gate in `alerts/notify.py` adds the rule that a
    symbol buzzes again only when its level *rises*. A phone that vibrates for
    the same thing every cycle gets its notifications switched off, and then the
    CRITICAL one is missed too — the anti-spam rule exists to protect the loud
    alert, not to be polite. A drop lowers the bar to the new level rather than
    resetting it, so a symbol oscillating around a threshold is not silenced.

45. **An unavailable notification channel says which half is missing.** Termux:API
    needs both the *app* and the `termux-api` *package*, and people routinely
    install one. Without the package the command does not exist; without the app
    it exists and hangs. `availability()` names the case, and the hang is turned
    into a reported failure by an 8-second timeout rather than a stalled scan.

46. **A hung notification is killed by process group, not by pid.** Found by
    running the timeout test: signalling only the direct child left its
    grandchild holding the stderr pipe, and `wait()` then blocked for the full
    30-second sleep — turning a 0.4 second timeout into a 30 second stall inside
    the scan cycle. `start_new_session=True` plus `killpg` is what makes the
    timeout mean what it says.

47. **A decision is an instruction, so it needs convergence rather than a
    maximum.** Seven floors must clear at once before a BUY — a single spiking
    score never produces one, and a test sets one to 100 with the rest at the
    floor to prove it. This is the only engine here whose output gets acted on
    rather than interpreted, which changes what a wrong answer costs.

48. **Green means open, blue means hold, and they are never the same colour.**
    Sharing one would mean a glance no longer separates "there is something to
    do" from "there is nothing to do". Every rendering carries icon + text +
    colour together; colour alone fails for anyone who cannot distinguish these
    hues, and for everyone on a phone in sunlight.

49. **Position health is not PnL.** A position up 30% on a broken setup is
    unhealthy, and saying so is the point — that is the moment the gain is about
    to be given back. A health score that tracked profit would fall silent
    exactly then. PnL is shown beside it so the two can visibly disagree.

50. **An unknown health is not a bad one.** Below half coverage the score is
    `None` and the decision becomes WATCH, never SELL. Telling someone to sell
    because a request failed is the worst false alarm this system could produce.

51. **The invalidation recorded at entry outranks every other signal.** It was
    agreed before the position existed and before any of this was emotional, so
    a close through it produces SELL whatever momentum, volume and the scores
    are doing. Everything else is evidence; this is the terms of the trade.

52. **Exit signals are counted by severity, never summed.** Five cosmetic
    warnings must not outweigh one broken support, and a total would let them.
    A test asserts the report has no total at all.

53. **A move toward the exit is never delayed by the anti-noise cooldown.**
    Confirmation and cooldown exist so the screen does not oscillate — twenty
    alternating readings move it zero times — but getting out late because a
    timer was running is not a trade-off worth making. Four conditions bypass
    the gate entirely: broken invalidation, safety veto, liquidity collapse,
    critical rug risk.

54. **A skipped signal is followed exactly like a taken one.** Recording only
    what the user acted on would compare the engine against a sample the user
    pre-selected. `taken` has three states and NULL is one of them: an
    unanswered prompt is not a refusal.

55. **Peak and trough are seeded from the price the returns use.** Found by
    running it: seeding from the observed price while computing returns from the
    user's fill rendered "perte max" as +75.79% — a maximum loss that was a gain.

56. **No endpoint can place an order, and a test walks the routing table to
    prove it.** The workflow is analyse -> recommend -> alert -> *the user acts
    manually* -> the user confirms -> measure. There is no exchange key, no
    signing and no order path anywhere in this repository.

57. **The launcher starts the server before anything else, and the screen never
    waits for the network.** `android-start.sh` used to run pip, npm and a
    blocking `doctor` before uvicorn — measured here at 3.6s for the doctor and
    4.9s for an npm build, on a desktop. On a phone under PRoot those are far
    worse, and every second of them was a blank screen showing data that was
    already in SQLite and did not depend on any of it. Install, update and start
    are now three scripts because they happen at three different frequencies.

58. **The feed check runs in the background and has three states, not two.**
    PENDING is not a failure — it is the honest answer for the first seconds,
    and rendering it as "not verified" would make every launch look broken for
    as long as the check takes. 🟡 / 🟢 / 🔴, and `live_verified` is false for
    both 🟡 and 🔴 because neither licenses presenting a fresh BUY as live.

59. **`providers/verify.py` is the only definition of LIVE VERIFIED.** Moving
    the check in-process raised the risk that the launcher would claim verified
    on weaker evidence than `doctor` demands. Verified means real data
    cross-checked against itself — ticker inside its own 24h range, OHLC
    invariants, candle spacing, klines agreeing with the ticker, bid < ask — and
    *every* check must pass. "Mostly verified" is not a state worth displaying.

60. **A scan that produced nothing has not replaced the last one that produced
    something.** Found by running the app against an unreachable Binance: the
    scan completed with zero successes, `last_report` stopped being None, and
    the screen went from a populated journal to blank. `/api/scan` now falls
    back on `not report.results` as well as on `report is None`, and says which
    of the two it was.

61. **The trust line, the topbar and the feed badge must never contradict each
    other.** Same run: the header said LIVE and the trust line said "Flux à
    jour" while the badge said FLUX LIVE NON VÉRIFIÉ. Three statements about
    one feed, one of them false. They now all read the same verification state.

62. **A startup phase that has not happened is null, never zero.** A zero would
    read as "instant" when it means "not yet", and on a launch timeline that is
    the difference between working and broken. Python's own import time is
    reported rather than hidden: on a phone it is the larger half of a launch,
    and a timeline starting after it would claim the app was ready long before
    anything reached the screen.

63. **On Android the frontend is built in Termux and the backend runs in Ubuntu
    under PRoot, and neither may cross.** `npx vite build` inside the
    distribution dies with a BUS ERROR — esbuild issues instructions PRoot's
    syscall translation does not survive — and numpy and pydantic-core do not
    build cleanly in Termux natively. So a command on the wrong side is not a
    slow path, it is a crash. `cp_run_in_ubuntu` and `cp_run_in_termux` make the
    side explicit at every call, and `tests/test_android_scripts.py` reads the
    scripts and fails if a build token reaches Ubuntu or a heavy pip reaches
    Termux. A stand-in `proot-distro` on PATH records its argv, so the
    delegation is exercised rather than inspected — what no test here can prove
    is that a real phone behaves as documented.

64. **The user never types `proot-distro login`.** Not having to know which side
    a command belongs to is the whole design: three scripts, all launched from
    Termux, each routing its own steps. A script run from the wrong side stops
    with the sentence to type rather than attempting the step anyway — under
    PRoot the failure would arrive minutes later and no longer resemble its
    cause.

65. **The environment is detected by a marker file, not by sniffing
    `/etc/os-release`.** The first version reasoned "no Termux prefix and
    os-release says Ubuntu, therefore inside PRoot", which is equally true of
    any ordinary Ubuntu machine — a desktop or CI run was misidentified as the
    container and refused to build the frontend. `android-install.sh` writes
    `/etc/cryptopulse-inside-distro` inside the distribution: a fact created
    rather than a coincidence interpreted. `CP_FORCE_ENV` overrides it, because
    an override beats a wrong guess with no way out.

66. **The database path is asked of the application, never expanded in the
    shell.** `CP_DB_URL` lives in `.env` and normally never reaches the shell
    environment, so `${CP_DB_URL##*sqlite:///}` under `set -u` aborted
    `android-update.sh` with `unbound variable` *before the backup* — the backup
    step becoming the reason there was no backup. `cryptopulse db-path` reads
    the same settings the server reads, prints nothing and exits non-zero when
    there is no file (Postgres, in-memory), and the backup's existence is
    checked before the migration rather than assumed.

67. **A pool between two quote assets is not a discovery.** Found by running the
    search: `new_pools` returns WETH/USDC-style infrastructure pools, which have
    no new side. `token` had to return something and returned WETH — presenting
    a blue chip as a fresh find, priced correctly and completely wrong. Such
    pools are filtered and counted in `filtered.not_a_discovery`.

68. **The new token is not always the base token.** Address ordering decides
    base vs quote in an AMM, so a discovery sits on the quote side about half
    the time. Reading `base_token` blindly would misprice half the list at
    WETH's price. The side is chosen by which one is a known quote asset, and
    the price follows the side.

69. **Pool age is not token age.** `pool_created_at` says when this pool opened,
    not when the contract was deployed. The field is `pool_age_seconds`, and
    `token_age_seconds` stays `None` until a source that actually knows it
    (Blockscout) is wired in. An unknown age is never bucketed at all — filing
    it under "< 15 min" would put it at the top of the list people act on
    fastest.

70. **A failed search and a quiet chain never render the same.** `_discovery_state`
    has four values, and EMPTY is its own: a search that completed and found
    nothing is a fact about the chain, while one that failed is a fact about our
    connection. Collapsing them would let an outage look like a calm market.

71. **A missing security check is never a passing one.** GoPlus omits checks it
    could not run rather than returning a negative, so every flag is tri-state
    and `None` propagates to the screen. Reading an absent `is_honeypot` as "not
    a honeypot" would turn "we did not look" into "it is safe" — on a token
    minted four minutes ago, the most expensive mistake this system could make.
    Below `safety_min_coverage` the verdict is UNKNOWN, which forbids a BUY
    without claiming the token is bad; those are different sentences and the UI
    renders them differently.

72. **A safety veto zeroes the score rather than reducing it.** Found by running
    the scorer: a proven honeypot came out at 70/100 because only the
    sellability group was lost, and 70 reads as "fine" at a glance. Same rule as
    the explosion engine's hard gate (invariant 40), and for the same reason — a
    merely reduced score still outranks a sound token in a sorted list.

73. **Every veto carries its own reason.** Found by running the UI: a token
    vetoed for HIGH rug risk had an empty `blocking` list, so the screen fell
    back to "Sécurité non analysée" on a token that had been fully analysed. The
    engine now guarantees at least one blocking finding whenever `hard_veto` is
    true, and the UI has no fallback text — inventing a reason would only ever
    paper over a bug.

74. **A share is normalised to percent exactly once, at parse time.** GoPlus
    returns fractions (`"0.92"` means 92 %). Comparing those against thresholds
    expressed in percent broke both directions at once: fully burned liquidity
    read as 0.92 and was flagged "non verrouillée", while ten holders at 9 %
    each summed to 0.9 and never tripped a 60 % concentration limit. A clean
    token looked dangerous and a captured one looked clean. `_percent()` is the
    only place the conversion happens.

75. **Two sources are reported side by side and never merged.** GeckoTerminal
    and DexScreener index the same pools from the same chain, so a price they
    disagree about is a price nobody should act on — the same reasoning that
    makes `doctor` cross-check the ticker against the klines. An average of two
    prices 40 % apart describes neither and destroys the only signal that
    mattered. `FieldComparison` keeps both values and has no merged field, so
    no caller can quietly pick one.

76. **Agreement is a statement about our data, not about the token.** Two
    indexers agreeing does not make a token safe, and a token only one of them
    lists is not suspicious — it is thinly indexed, which is ordinary twenty
    minutes after minting. AGREE / DISAGREE / SINGLE_SOURCE / NO_DATA are four
    different sentences and render differently.

77. **Drift is symmetric between the two sources.** Which one is the reference
    is not a question this project is entitled to answer, so the difference is
    divided by the larger magnitude rather than by either source's value —
    otherwise the reported drift would depend on an arbitrary choice.

78. **A window that has not elapsed is not data, per input rather than in bulk.**
    An indexer will happily return an `h1` volume for a pool ninety seconds old,
    but that hour mostly predates the pool. Counting it would make the youngest
    tokens — the ones the screen exists to surface — look like the best-measured
    ones. The rule is applied input by input, because at ten minutes the
    five-minute figures are genuinely real and the hourly ones are not; a single
    age cap would either discard the first or accept the second.

79. **The early score and its confidence stay two numbers.** Spec §52 is
    explicit: a token minted two minutes ago may legitimately look early *and*
    be barely measurable. Blending them would hide whichever fact the reader
    needed. A component resting on an unelapsed window still scores, but says so
    in its caveats, so the reasons list is never read as pure evidence.

80. **Maturity with no data is 50 and `known=False`, never 0.** "This move has
    probably not started" is a claim, and it cannot be made from an absence of
    data — that is exactly how a dead token looks brand new.

81. **One repository, mounted at the same path on both sides.** The bind is
    `--bind "$ROOT:$ROOT"`, so `frontend/dist`, `.env` and
    `data/cryptopulse.db` are the same files from Termux and from Ubuntu — no
    copy step, and no way for two checkouts to drift while the user cannot tell
    which one the server read. `CP_UBUNTU_PROJECT` supports a deliberately
    separate checkout by syncing `dist` into it; it is not the recommended
    shape.

82. **A sentence is assembled in French where the numbers are, never translated
    on the way to the screen.** The component that knows RVOL is 2.59 is the
    only place that can say so. Shipping English prose to React and translating
    it there would mean parsing sentences to recover the figures, and every new
    reason would arrive in English until somebody noticed. `Text` is a `str`
    subclass carrying `.code` and `.params`, so `list[str]`, JSON, the database
    and `"x" in reasons` all keep working while the structured form travels
    alongside for anything that would rather branch on a code than on wording.

83. **Nothing is translated at display time — no model, no service, no lookup.**
    Templates are module constants and rendering is one `str.format`: measured
    at 1.8 µs per reason, about 2 ms for a full 120-asset scan, and 1 ms of
    import. That is why this can sit inside the scan loop. A startup A/B against
    the pre-francisation tree measured 1394 ms versus 1411 ms over five cold
    launches each, which is inside the run-to-run spread of either.

84. **Enum values stay English on the wire and a French label travels beside
    them.** `BREAKOUT` is an identifier stored in SQLite, compared in code, and
    hashed into the weights fingerprint — renaming it would silently reinterpret
    every past signal (invariant 13). So `state` stays and `state_label_fr`
    joins it. `labelFor()` refuses to fall back to the key: `BREAKOUT` is
    readable enough that an untranslated value would go unnoticed for months,
    whereas an em dash is visibly missing.

85. **The decimal comma and the narrow no-break space live in exactly two
    files** — `cryptopulse/i18n/__init__.py` and `frontend/src/format.ts`.
    Found by running the app: the score breakdown read `+0.6 / 20` and the
    penalty line `-5.0` while every reason beside them read `+3,42 %`. Two
    conventions on one screen is worse than either alone. The separator before
    `%` is U+202F specifically because it is *no-break*: a plain space lets the
    sign wrap to the next line on a phone. A test forbids `toFixed` outside the
    formatter.

86. **The anti-English test flags words, not sentiment, and its marker list is
    deliberately narrow.** The first version flagged "volume", "prix",
    "support", "momentum" and "acceptable" — all correct French — and a test
    that cries wolf gets switched off within a week, which is worse than not
    having it. Loanwords French traders actually use (`retest`, `setup`,
    `momentum`) are stripped before matching, and so are `{placeholder}` names,
    which are parameters rather than words on screen.

87. **Two markets, two decision engines, one vocabulary.** The six decisions,
    the six colours and `PRESENTATION` are shared, because a user who learns
    what 🟢 means on one screen must not relearn it on the other (spec §21).
    Everything else is separate: the Robinhood engine reads flow instead of
    candles, dollars of depth instead of a liquidity rank, and GoPlus instead
    of a CEX safety score. One engine with a mode switch would mean a single
    fingerprint covering both — and changing a Binance floor would silently
    reinterpret a Robinhood decision taken last week.

88. **On a DEX the liquidity floor is dollars, and its margin never reaches the
    strength.** Score points and dollars are not the same unit: feeding a
    60 000 dollar margin into a calculation whose other margins are single
    digits would make every deep pool look TRÈS FORTE. `_liquidity_floor`
    deliberately returns `None` for its margin.

89. **The safety veto is stated once, not relayed twice.** Found by running the
    search against the honeypot fixture: the card read "veto de sécurité :
    HONEYPOT" immediately above "veto sécurité : HONEYPOT", because the
    explosion engine relays the safety gate as its own. The same fact in two
    wordings reads as a bug rather than as emphasis, so the relay is dropped —
    while a veto genuinely belonging to the explosion engine, such as a pool
    too thin for a fifteen-minute move, is still listed.

90. **An undecided token is counted apart from a refused one.** `decision_counts`
    has its own `UNDECIDED` key rather than folding those rows into ⚫. "Nobody
    has looked" is ordinary before the safety pass has run; "we refuse" is a
    conclusion. Merging them would make an incomplete cycle look like a chain
    full of scams.

91. **The Robinhood decision is computed last, after safety and explosion.**
    `attach_safety` runs safety, then explosion, then the decision, in that
    order and in that function. A decision produced before the veto existed
    would have been taken on less evidence than the screen shows beside it —
    the same reasoning that already put explosion after safety.

92. **A catastrophic health reading caps the score instead of costing it
    points.** Found by writing the invariant-49 test and watching it fail: a
    position up 300 % whose pool had lost 90 % of its depth scored 73.5 and
    rendered SAINE, because liquidity is only 25 of the 100 points and
    everything else was perfect. A withdrawn pool is not a quarter of a
    problem — it is the reason a DEX position goes to zero. `caps_at` applies
    the same rule the explosion gate and the safety veto already follow
    (invariants 40, 72); the lowest cap wins and caps are never averaged.

93. **The discovery search never looks at a token you already hold.**
    `new_pools` returns recently created pools, so a position opened two days
    ago fell out of that window long ago. The watcher reads the held addresses
    directly through `/tokens/{address}/pools`, which is a different endpoint
    for a different question — and the reason the watcher cannot be folded into
    the search.

94. **The watcher's cost is one request per held token, and it says so.**
    GeckoTerminal has no token-to-pools batch: `/pools/multi/` takes *pool*
    addresses. Claiming "two requests whatever you hold" — as this module's
    docstring first did — would have been a figure the connector cannot
    deliver. GoPlus genuinely is batched, which is why safety is the cheap
    half. `CP_ROBINHOOD_MAX_TRACKED_POSITIONS` bounds the total.

95. **A token the indexer stopped listing is reported, never sold.** The pool
    vanishing from the index is a fact about our data, not about the token.
    Verified by running it: the watcher returned `pool absent de l'indexeur ce
    cycle` and left the decision untouched. Inventing a SELL from a failed
    lookup is the worst false alarm this loop could produce.

96. **A Robinhood opening is routed by its chain, never by symbol lookup.**
    `_merge_robinhood_context` fills the entry from the discovery candidate;
    passing a contract address through the Binance path would find nothing and
    would stamp the position with a Binance provider name and market regime.
    The three baselines it records — depth, rug level, early score — are what
    the health engine measures *change* against, and without them a pool that
    halved is indistinguishable from one that was always small.

97. **A Robinhood window is resolved from candles, never by looking again
    later.** The obvious implementation — note the price now, read it again in
    fifteen minutes — is wrong: a window observed three minutes late is not the
    window the engine made a claim about, and the error is silent and always in
    the direction of whenever the loop happened to run. GeckoTerminal's pool
    OHLCV with a five-minute aggregate makes the window exact *and* resolvable
    retroactively, which is what lets a decision from last Tuesday be graded
    today.

98. **`by_action` is the table the Robinhood half exists to produce.** 🟢
    ACHETER must beat 🟡 SURVEILLER, which must beat ⚫ NE PAS ACHETER. If that
    ordering is absent, the floors in `RobinhoodSettings` are wrong — and no
    other table here can show that. It is rendered first, ordered BUY / WATCH /
    AVOID rather than alphabetically, so the comparison reads top to bottom.

99. **The Robinhood journal is a separate table, not a column on `signals`.**
    Pooling the two markets would produce a win rate that moves when either
    engine changes and identifies neither. The columns differ too — there is no
    ATR, no setup state and no liquidity rank on a token four minutes old.

100. **The DEX cost model is deliberately pessimistic and says it is modelled.**
    A swap pays the pool fee twice, gas twice, and slippage against a pool that
    is often thin. 3 % is a placeholder, not a measurement, and
    `RobinhoodCosts.describe()` says so — flattering it would make every rate
    look better than anything achievable, which is the opposite of the point.

101. **Only 🟢 and 🔴/🟠 are worth a Robinhood notification.** ⚫ is the ordinary
    answer for most of a permissionless chain and 🟡 is an invitation to look
    rather than an instruction; notifying either would be a phone that vibrates
    all day, and then the one that mattered is muted too. The entry gate is
    keyed on the **contract address**, not the symbol: symbols are not unique
    on-chain, and two tokens calling themselves FRONG silencing each other is
    exactly the copycat's trick.

102. **The three decision words never share an icon on a lock screen.** Found by
    running it: `_decision_args` branched on `action == "SELL"` alone, so a
    🟠 RÉDUIRE rendered as "🔥 🟢 ACHETER" — telling someone to buy at exactly
    the moment the engine wanted them to take profit off the table. One
    presentation table now, three vibration patterns, so the three are
    distinguishable without looking (invariant 48).

103. **The Robinhood engines format their numbers through `num()` like
    everything else.** They were written before `cryptopulse/i18n` existed and
    used `f"{x:.1f}"`, which writes `5.3`. Found on a lock screen: "volume
    actuel 5.3× le rythme horaire" beside reasons reading "+3,42 %". Two
    parametrized tests now walk all twelve Robinhood modules for a decimal
    point and for a plain space before `%`.

104. **Retention treats the Robinhood journal exactly like the Binance one.** A
    decision is droppable only once all four of its windows exist. The rows
    still owing an answer are precisely the ones about to become evidence, and
    losing them would look like a quiet chain rather than a bug (invariant 19).

105. **The Robinhood half costs nothing until someone opens it.** No client, no
    watcher and no tracker is built in `__init__`; each has a `_get_*` built on
    first use. Measured across three cold launches after every Robinhood phase
    landed: 1328 ms to first HTTP, with zero Robinhood, GeckoTerminal, GoPlus
    or DexScreener lines in the boot log (spec §41-42).

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

# send a real Android notification, to confirm the round trip on the phone
python -m cryptopulse.cli notify

# what the price actually did 15m / 1h / 4h / 24h after each signal
python -m cryptopulse.cli verify

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
* `outcomes/horizons.py:_track_one` — same exact-match rule, and the PENDING
  branch is load-bearing: it is what stops an unfinished 24h window from being
  settled at whatever price is current. Do not "fill in" a pending window.
* `database/session.py:_is_sqlite_memory` — an in-memory SQLite database lives
  inside its *connection*, and the default pool gives one connection per thread.
  Every write goes through `asyncio.to_thread`, so without `StaticPool` the
  worker opens a second, empty database: the scan reports success and the
  journal is silently lost. Found by running the service, not by a unit test.
* `providers/cache.py` — the wrapper forwards `name` from the provider it wraps.
  This is not cosmetic: `is_synthetic()` identifies generated data by that name,
  so a wrapper shadowing it would make a fixture-backed scan report itself LIVE
  and drop the DEMO banner. A test pins it.
* `providers/registry.py:build_market_provider` — `cached=False` exists for
  `doctor`. A diagnostic answered from memory would report success without
  having contacted anything.
* `alerts/delivery.py` — the only module that may hold the webhook URL. If you
  find yourself passing it outward, you are about to leak a credential.
* `database/repo.py:prune` — the only destructive operation in the system. The
  `outcome_label IS NOT NULL AND all horizons recorded` filter is what makes it
  safe; loosening it silently deletes the evidence the journal exists to collect.
* `pumps/detect.py:find_pumps` — two passes on purpose. A greedy chronological
  walk lets a small early wiggle block the large run right behind it; candidates
  are collected, sorted by size, and accepted only when they do not overlap. An
  exact-local-minimum trough rule was also tried and was far too brittle — it
  accepted 29 bars in 400 of ordinary noise and missed real runs whose low was a
  bar early. Hence `trough_tolerance_pct`.
* `scripts/android_env.sh` — every command in the three Android scripts goes
  through `cp_run_in_ubuntu` or `cp_run_in_termux`. Adding a bare command that
  runs "wherever" is how a `vite build` reaches PRoot; the tests scan for the
  tokens, not for the helper, so a bare command is also invisible to them.
* `scoring/verdict.py` — reads only what the score already computed. If you find
  yourself adding a new threshold here, it belongs in a component instead; the
  verdict is a compression, not a ninth opinion.
* `cryptopulse/i18n/labels.py` — the label tables are keyed on **plain strings**,
  not on the enum members. Keying them on the enums created an import cycle
  (`labels` -> `alerts.engine` -> `labels`) the moment the alert engine wanted a
  French liquidity label. The catalogue must keep importing nothing from the
  engines.
* `cryptopulse/trading/robinhood_decision.py` — compares the cross-check's
  agreement on `.value` strings rather than importing `Agreement`. Importing it
  creates the cycle `robinhood_decision` -> `robinhood_detail` ->
  `robinhood_discovery` -> `robinhood_decision`, which is the same cycle
  `i18n/labels.py` had to break, and the same fix: an engine that reads a value
  does not need to import the module that defines it.
* `cryptopulse/i18n/reasons.py` — a reason is added here and called from the
  engine, never written as a literal at the call site. `tests/test_french_only.py`
  greps for `reasons.append("` across ten engine files, so a literal is caught,
  but only in those files: adding an engine means adding it to `ENGINE_FILES`.

---

## 9. Next steps, in order

1. **Run `doctor` and `doctor-robinhood` in an environment with egress.** Fix
   any field-mapping mismatch they report. Only then is anything else worth
   doing. Five connectors are now IMPLEMENTED — NOT LIVE VERIFIED for the same
   single reason (this sandbox refuses their hosts): Binance, Kraken, the
   Robinhood Chain RPC, GeckoTerminal, GoPlus and DexScreener. Each contract
   was cross-checked against the vendor's own published client read from PyPI,
   which proves the connector and that client agree — not that the live API
   matches either.
2. **Let it run and accumulate signals.** The journal is the point; nothing can
   be validated without a few thousand real rows.
3. ~~Build the outcome tracker.~~ **Done.** `outcomes/tracker.py` grades signals
   once their horizon elapses; `outcomes/stats.py` aggregates. Runs automatically
   after every scan, or on demand via `cryptopulse resolve` /
   `POST /api/outcomes/resolve`.

   ~~Multi-horizon verification.~~ **Done.** `outcomes/horizons.py` records what
   the price actually did 15m / 1h / 4h / 24h after each signal — price, change,
   max gain, max drawdown, success — as a complement to the barrier verdict.
   `cryptopulse verify` / `POST /api/horizons/track`, Verification tab in the UI.
4. **Watch the baseline comparison.** `scripts/simulate_journal.py` prints the
   scanner's win rate against random entries on the same bars, and now does the
   same at each of the four horizons. On synthetic data the scanner currently
   lands ~6 points *below* random on the barrier label, and 1.4 to 5.1 points
   below at every horizon. That number says nothing
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
