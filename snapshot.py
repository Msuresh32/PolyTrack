"""
snapshot.py  —  pull open positions for all tracked wallets and save to backtest.db.

Each invocation appends a new batch of immutable rows.
Exact duplicate rows within a single run are silently dropped (UNIQUE constraint).
Rows from different runs are all kept — that IS the historical record.

Usage:
    python snapshot.py               # snapshot once now
    python snapshot.py --dry-run     # print what would be saved, don't write
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from backtest_db import init_db

load_dotenv()

POSITIONS_API = "https://data-api.polymarket.com/positions"

# Parse wallets from .env  (same format as dashboard.py / monitor.py)
WALLETS: dict[str, str] = {}
for _pair in os.getenv("WALLETS", "").split(","):
    _pair = _pair.strip()
    if "=" in _pair:
        _label, _addr = _pair.split("=", 1)
        WALLETS[_addr.strip().lower()] = _label.strip()

# Category classifier — kept identical to dashboard.py so categories match
CATEGORY_RULES = [
    ("wnba", "WNBA"), ("nba", "NBA"), ("nfl", "NFL"), ("mlb", "MLB"),
    ("nhl", "NHL"), ("fifwc", "FIFA WC"), ("world-cup", "FIFA WC"),
    ("world cup", "FIFA WC"), ("epl", "Soccer"), ("ucl", "Soccer"),
    ("laliga", "Soccer"), ("premier-league", "Soccer"),
    ("premier league", "Soccer"), ("champions-league", "Soccer"),
    ("champions league", "Soccer"), ("mls", "Soccer"),
    ("serie-a", "Soccer"), ("bundesliga", "Soccer"), ("ligue-1", "Soccer"),
    ("ufc", "UFC/MMA"), ("mma", "UFC/MMA"), ("bellator", "UFC/MMA"),
    ("tennis", "Tennis"), ("wimbledon", "Tennis"), ("atp", "Tennis"),
    ("wta", "Tennis"), ("us-open", "Tennis"), ("french-open", "Tennis"),
    ("australian-open", "Tennis"), ("formula-1", "F1"), ("formula1", "F1"),
    ("grand-prix", "F1"), ("/f1-", "F1"), ("pga", "Golf"), (" golf", "Golf"),
    ("masters", "Golf"), ("ryder", "Golf"), ("ncaab", "NCAAB"),
    ("march-madness", "NCAAB"), ("ncaaf", "NCAAF"),
    ("college-football", "NCAAF"), ("valorant", "Esports"),
    ("csgo", "Esports"), ("cs2", "Esports"),
    ("league-of-legends", "Esports"), ("dota", "Esports"),
    ("boxing", "Boxing"),
]


def _classify(title: str, slug: str) -> str:
    text = f"{slug} {title}".lower()
    for kw, cat in CATEGORY_RULES:
        if kw in text:
            return cat
    return "Other"


def _as_float(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _fetch_positions(addr: str) -> list:
    try:
        r = requests.get(
            POSITIONS_API,
            params={"user": addr, "limit": 500},
            timeout=20,
            headers={"User-Agent": "polymarket-tracker/1.0"},
        )
        if r.status_code == 200:
            return r.json() or []
        print(f"[{addr[:10]}] HTTP {r.status_code}", file=sys.stderr)
    except Exception as exc:
        print(f"[{addr[:10]}] fetch error: {exc}", file=sys.stderr)
    return []


def _seed_wallets(conn) -> None:
    for addr, label in WALLETS.items():
        conn.execute("""
            INSERT OR IGNORE INTO tracked_wallets
              (wallet_address, wallet_label, source)
            VALUES (?, ?, 'env')
        """, (addr, label))
    conn.commit()


def _position_to_row(snap_ts: str, addr: str, label: str, p: dict) -> tuple:
    title = p.get("title") or p.get("market") or ""
    slug  = p.get("eventSlug") or p.get("event_slug") or ""
    cat   = _classify(title, slug)

    # Prices: positions API returns fractions (0.47), convert to cents (47)
    avg_price = _as_float(p.get("avgPrice")) * 100
    cur_price = _as_float(p.get("curPrice")) * 100
    cost      = _as_float(p.get("initialValue"))
    cur_val   = _as_float(p.get("currentValue"))
    shares    = _as_float(p.get("size") or p.get("shares"))
    unreal    = cur_val - cost

    # Outcome / side
    side = p.get("outcome") or p.get("side") or (
        "Yes" if _as_float(p.get("outcomeIndex", 0)) == 0 else "No"
    )

    # IDs
    condition_id = p.get("conditionId") or p.get("condition_id")
    token_id     = p.get("asset") or p.get("assetId") or p.get("tokenId")
    event_id     = p.get("eventId") or p.get("event_id") or slug
    market_id    = condition_id or event_id or p.get("slug") or title

    redeemable = bool(p.get("redeemable", False))
    status     = "resolved" if (redeemable or cur_price <= 0) else "open"

    end_date = (p.get("endDate") or "")[:10] or None

    return (
        snap_ts, addr, label,
        str(market_id or ""), str(condition_id or ""),
        str(event_id or ""), slug, title, cat, side,
        str(token_id or ""),
        shares, avg_price, cur_price, cur_val,
        cost, unreal,
        end_date, status,
        json.dumps(p),
    )


def run_snapshot(dry_run: bool = False) -> int:
    conn = init_db()
    _seed_wallets(conn)

    snap_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    saved = 0
    skipped = 0

    raw: dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(WALLETS))) as ex:
        futures = {ex.submit(_fetch_positions, addr): addr for addr in WALLETS}
        for f in as_completed(futures):
            raw[futures[f]] = f.result()

    for addr, positions in raw.items():
        label = WALLETS[addr]
        print(f"  [{label}] {len(positions)} positions")
        for p in positions:
            row = _position_to_row(snap_ts, addr, label, p)
            if dry_run:
                print(f"    DRY  {row[8]:12s}  {row[9]:3s}  {row[6][:50]}")
                saved += 1
                continue
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO position_snapshots
                      (snapshot_ts, wallet_address, wallet_label,
                       market_id, condition_id, event_id,
                       market_slug, market_title, category, side, token_id,
                       shares, avg_entry_price, current_price, current_value,
                       cost_basis, unrealized_pnl, resolution_time, market_status,
                       raw_payload)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, row)
                if conn.execute("SELECT changes()").fetchone()[0]:
                    saved += 1
                else:
                    skipped += 1
            except Exception as exc:
                print(f"    insert error: {exc}", file=sys.stderr)

    if not dry_run:
        conn.commit()
    conn.close()

    tag = "(dry-run) " if dry_run else ""
    print(f"[snapshot] {tag}{snap_ts}  saved={saved}  skipped_dups={skipped}")
    return saved


if __name__ == "__main__":
    if not WALLETS:
        sys.exit("No WALLETS configured in .env")

    parser = argparse.ArgumentParser(description="Snapshot tracked wallet positions")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print rows without writing to DB")
    args = parser.parse_args()
    run_snapshot(dry_run=args.dry_run)
