# Running CRYPTO PULSE AI unattended

Three ways to keep the radar alive, in increasing order of ceremony. All three
assume the feed has been verified first:

```bash
python -m cryptopulse.cli doctor        # must print LIVE VERIFIED
```

A radar on an unverified feed produces a table built on a silent source, which
is worse than no table.

---

## 1. A terminal

```bash
./start.sh radar
```

Stops cleanly on Ctrl-C, closing the provider and flushing the database rather
than dying mid-write. Good for a first day of watching what it does.

## 2. systemd

```bash
sudo useradd --system --home /opt/cryptopulse cryptopulse
sudo cp -r . /opt/cryptopulse && cd /opt/cryptopulse
sudo -u cryptopulse python3 -m venv .venv
sudo -u cryptopulse .venv/bin/pip install -e ".[postgres]"
sudo cp deploy/cryptopulse-radar.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cryptopulse-radar
journalctl -u cryptopulse-radar -f
```

`Restart=always` with `TimeoutStopSec=30`: the service comes back after a crash
or a reboot, and a stop gives the current pass time to finish instead of killing
it half-way through a write.

## 3. Docker

```bash
docker compose up -d --build
docker compose logs -f api
```

Brings up the API, the dashboard and PostgreSQL. The container's health check
polls `/healthz`, which answers **503 once the radar has stopped scanning** —
not merely when the process has died. A process that is up and no longer
scanning is the failure that matters, because you keep trusting a screen that
stopped updating.

---

## Knowing it is working

| Signal | Where | Means |
|---|---|---|
| `/healthz` | HTTP status | 200 scanning, 503 stopped |
| `/api/health` → `health.status` | payload | `OK` / `DEGRADED` / `DOWN` + reasons |
| `/api/health` → `candle_cache.hit_rate` | payload | A collapsing hit rate means requests are being spent on data already known |
| A `SYSTEM` alert | your alert channels | The radar itself telling you it stopped, once per outage, and again when it recovers |
| `data/alerts.jsonl` | disk | Every alert, append-only, independent of the database |

The watchdog fires after five scan intervals without a successful pass
(`CP_ALERT_WATCHDOG_AFTER_SECONDS` to override). It says it **once** per outage:
an alert that repeats every minute is one you mute, and then you have no
watchdog at all.

## Keeping the journal

The journal is the point of the whole system — it is the only thing that can
eventually say whether any of the scoring works.

* **Signals are never deleted on a timer.** A ×10 label can take 180 days to
  settle, so a retention window shorter than the horizon would delete rows
  before they could ever be graded. Only score points (a hot-path convenience
  that regenerates every scan) are purged, after `CP_DB_RETENTION_DAYS`.
* **Back it up.** With SQLite: `sqlite3 data/cryptopulse.db ".backup data/backup.db"`.
  With PostgreSQL: `pg_dump`. Losing the journal means starting the evidence
  from zero.
* **Grade it.** The radar grades both axes automatically after every pass. By
  hand:

```bash
python -m cryptopulse.cli resolve                  # intraday, hours
python -m cryptopulse.cli resolve --axis moonshot  # ×10, weeks
```

## Costs to keep an eye on

Enabling the ×10 layer adds one daily kline request per asset per scan. The
candle cache absorbs almost all of it — a daily bar is read once a day rather
than once a minute — but on a large `volume` universe the arithmetic still
matters:

| Universe | Assets | Requests/scan without cache | With cache (steady state) |
|---|---:|---:|---:|
| robinhood | ~35 | ~210 | ~40 |
| volume | 120 | ~720 | ~150 |

`CP_MOON_ENABLED=false` removes the daily entirely if the budget is tight.
