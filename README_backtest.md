# Polymarket Backtest & Resolution System

Tracks every position from every monitored wallet over time, automatically
resolves markets after they end, and measures whether the wallets are
actually sharp.

---

## Files

| File | Purpose |
|---|---|
| `backtest_db.py` | Shared SQLite schema and DB helpers |
| `snapshot.py` | Daily (or intra-day) position snapshot job |
| `resolver.py` | End-of-day market resolution job |
| `performance.py` | Wallet performance aggregation (runs after resolver) |
| `backtest.py` | Point-in-time backtesting CLI + importable module |
| `test_backtest.py` | Unit and integration tests |
| `backtest.db` | Created automatically on first run |

---

## Database tables

### `tracked_wallets`
Wallet registry seeded from `.env WALLETS=` on first snapshot run.

### `position_snapshots`
Immutable — one row per (snapshot_run, wallet, token).
Never updated. Historical rows are never deleted.

### `resolved_positions`
One final P/L row per (wallet, token, condition). Written once by
`resolver.py`, never overwritten (UNIQUE guard).

### `wallet_performance_summary`
Aggregated win-rate / ROI per `(wallet, category)`. Fully recomputed by
`performance.py` after each resolver run. Safe to re-run repeatedly.

---

## Daily workflow

### 1. Snapshot positions (run as often as you like)

```bash
python snapshot.py
# → saves open positions for all .env wallets to backtest.db

python snapshot.py --dry-run   # preview without writing
```

Run once per day minimum. Running every hour gives better point-in-time
resolution for backtesting.

### 2. Resolve finished markets (run at end of day)

```bash
python resolver.py
# → finds markets with endDate <= today, hits gamma-api for resolution,
#   writes WIN/LOSS/PUSH/NEEDS_REVIEW to resolved_positions

python resolver.py --dry-run             # preview without writing
python resolver.py --since 2026-01-01    # only look at recent snapshots
```

Resolution sources (in priority order):
1. Gamma API (`gamma-api.polymarket.com/markets?conditionIds=...`) — authoritative
2. Snapshot price heuristic (`curPrice >= 99 → WIN`, `<= 1 → LOSS`)
3. Cannot determine → `NEEDS_REVIEW` (never guessed)

### 3. Update wallet performance summaries

```bash
python performance.py
# → aggregates resolved_positions into wallet_performance_summary

python performance.py --wallet @swisstony   # update one wallet only
```

---

## Running a backtest

```bash
# All positions, all time
python backtest.py

# Point-in-time: only use data visible on Jan 1 2026
python backtest.py --as-of 2026-01-01T00:00:00Z

# Category filter
python backtest.py --category "FIFA WC"

# Wallet filter (label or address substring)
python backtest.py --wallet @swisstony

# Only positions with cost basis >= $50
python backtest.py --min-size 50

# Only positions that resolved within 24h of first snapshot
python backtest.py --resolve-window 24

# Export results to JSON
python backtest.py --output results.json
```

### Entry price methods

| Flag | Behaviour |
|---|---|
| `--entry avg` (default) | Wallet's average entry price from the API |
| `--entry snapshot` | Price in the earliest snapshot before cutoff |

### Point-in-time guarantee

When `--as-of T` is given:
- Only snapshots taken **at or before T** are loaded.
- Position sizes, entry prices, and opening dates are all as-of T.
- Resolved outcomes come from `resolved_positions` (these we always know
  after the fact), but only for positions **first seen before T**.
- No future position opens, price movements, or market outcomes leak in.

---

## Dashboard API endpoints

The running `dashboard.py` exposes three read-only endpoints that read from
`backtest.db`. They return empty lists if the DB doesn't exist yet.

| Endpoint | Description |
|---|---|
| `GET /api/backtest/summary` | `wallet_performance_summary` rows |
| `GET /api/backtest/resolved` | Recent `resolved_positions` rows |
| `GET /api/backtest/snapshots` | Latest snapshot per (wallet, token) |

### Query parameters

**`/api/backtest/summary`**
- `wallet` — filter by label or address substring
- `category` — exact category match (e.g. `FIFA WC`)

**`/api/backtest/resolved`**
- `wallet` — filter by label or address substring
- `category` — exact category match
- `win_loss` — `WIN`, `LOSS`, `PUSH`, or `NEEDS_REVIEW`
- `limit` — max rows (default 50, max 500)

**`/api/backtest/snapshots`**
- `wallet`, `category`, `limit` (default 100, max 1000)

---

## P/L calculation

### Binary markets

| Outcome | Payout | P/L |
|---|---|---|
| WIN | `shares × $1.00` | `payout − cost_basis` |
| LOSS | `$0` | `−cost_basis` |
| PUSH | `shares × final_price` | `payout − cost_basis` |

```
roi_pct = realized_pnl / cost_basis × 100
```

### Multi-outcome / scalar markets

If `outcomePrices` is neither `["1","0"]` nor `["0","1"]`, the position is
classified as `PUSH` with `final_price = outcomePrices[side_index] × 100`.
Payout = `shares × (final_price / 100)`.

### Ambiguous / unresolved

If none of the sources can determine the winner, the position is marked
`NEEDS_REVIEW` and excluded from all P/L, win-rate, and ROI calculations.

---

## Running the tests

```bash
python -m pytest test_backtest.py -v
# or
python test_backtest.py
```

Tests create temporary databases in a system temp dir and clean up after
themselves. No network calls are made.

---

## Cron / scheduler suggestions

```
# Every 2 hours — position snapshots
0 */2 * * *  cd /path/to/tracker && python snapshot.py >> logs/snapshot.log 2>&1

# 11pm daily — resolve finished markets
0 23 * * *   cd /path/to/tracker && python resolver.py  >> logs/resolver.log 2>&1

# 11:30pm daily — aggregate performance
30 23 * * *  cd /path/to/tracker && python performance.py >> logs/perf.log 2>&1
```

On Windows Task Scheduler, create three Basic Tasks pointing to:
```
python "C:\...\Tracker\snapshot.py"
python "C:\...\Tracker\resolver.py"
python "C:\...\Tracker\performance.py"
```

---

## Known limitations

1. **Network dependency**: `resolver.py` calls `gamma-api.polymarket.com`.
   If the API is down, affected positions are deferred (not guessed).

2. **First snapshot = first known entry**: If you start tracking a wallet
   mid-position, `avg_entry_price` is whatever the wallet's historical
   average was, which is correct. But `first_seen_at` will be the date
   you started, not the date they originally opened it.

3. **Multi-leg / conditional markets**: Rare Polymarket market types where
   a single condition can have more than two outcomes. These go to
   `NEEDS_REVIEW` if gamma doesn't return a clear `["1","0"]` or `["0","1"]`.

4. **`seen.db` is separate**: `monitor.py` and `dashboard.py` continue to
   use `seen.db` (fills / alert dedup). The backtest system uses `backtest.db`
   only. The two databases are independent.

5. **Rolling ROI windows** (`rolling_7d_roi` etc.) are based on
   `resolved_at` timestamps, not position open dates.
