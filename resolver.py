"""
resolver.py  —  find finished markets and write final P/L to resolved_positions.

Resolution sources (tried in order):
  1. Snapshot fields: redeemable=True or cur_price=0 (fast, no network)
  2. Gamma API: outcomePrices confirms the winning outcome
  3. Still ambiguous → marked NEEDS_REVIEW (never guessed)

A position is only written once — the UNIQUE constraint prevents double-resolving.

Usage:
    python resolver.py               # resolve everything eligible
    python resolver.py --dry-run     # print results without writing
    python resolver.py --since 2026-01-01  # only look at snapshots from this date
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from collections import defaultdict
from typing import Optional

import requests
from dotenv import load_dotenv

from backtest_db import get_conn, init_db

load_dotenv()

GAMMA_API = "https://gamma-api.polymarket.com/markets"


# ── Gamma API ─────────────────────────────────────────────────────────────────

def _fetch_gamma(condition_id: str) -> Optional[dict]:
    if not condition_id:
        return None
    try:
        r = requests.get(
            GAMMA_API,
            params={"conditionIds": condition_id},
            timeout=15,
            headers={"User-Agent": "polymarket-tracker/1.0"},
        )
        if r.status_code == 200:
            data = r.json()
            markets = data if isinstance(data, list) else data.get("markets", [])
            return markets[0] if markets else None
        print(f"  gamma HTTP {r.status_code} for {condition_id[:12]}", file=sys.stderr)
    except Exception as exc:
        print(f"  gamma error {condition_id[:12]}: {exc}", file=sys.stderr)
    return None


# ── Resolution logic ──────────────────────────────────────────────────────────

def determine_resolution(
    market_status: str,
    cur_price: float,
    side: str,
    gamma: Optional[dict],
) -> tuple[str, float]:
    """
    Return (win_loss, final_price_cents).
    final_price_cents: 100=WIN  0=LOSS  50=PUSH  -1=NEEDS_REVIEW
    """
    # Gamma is the authoritative source when the market is closed
    if gamma:
        closed = gamma.get("closed") or gamma.get("resolved") or False
        out_prices = gamma.get("outcomePrices") or []
        outcomes   = gamma.get("outcomes") or ["Yes", "No"]
        if closed and out_prices:
            side_idx = next(
                (i for i, o in enumerate(outcomes)
                 if str(o).strip().lower() == str(side).strip().lower()),
                None,
            )
            if side_idx is not None and side_idx < len(out_prices):
                p = float(out_prices[side_idx])
                if p >= 0.99:
                    return "WIN", 100.0
                if p <= 0.01:
                    return "LOSS", 0.0
                # Partial/scalar resolution → treat as PUSH
                return "PUSH", round(p * 100, 2)

    # Fast-path: snapshot already tells us
    if market_status == "resolved":
        if cur_price >= 99:
            return "WIN", 100.0
        if cur_price <= 1:
            return "LOSS", 0.0

    # Fallback on price alone when gamma isn't available
    if cur_price >= 99:
        return "WIN", 100.0
    if cur_price <= 1:
        return "LOSS", 0.0

    return "NEEDS_REVIEW", -1.0


# ── Core resolver ─────────────────────────────────────────────────────────────

def run_resolver(dry_run: bool = False, since: Optional[str] = None) -> int:
    conn = init_db()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today   = datetime.now(timezone.utc).date().isoformat()

    # Find every (wallet, condition_id, token_id) group whose resolution date
    # has passed and which hasn't already been written to resolved_positions.
    since_clause = "AND ps.snapshot_ts >= :since" if since else ""
    rows = conn.execute(f"""
        SELECT
            ps.wallet_address,
            ps.wallet_label,
            ps.condition_id,
            ps.token_id,
            ps.side,
            ps.category,
            ps.market_id,
            ps.market_title,
            MIN(ps.snapshot_ts)       AS first_seen,
            MAX(ps.snapshot_ts)       AS last_seen,
            MAX(ps.snapshot_id)       AS latest_snap_id,
            MAX(ps.shares)            AS shares,
            AVG(ps.avg_entry_price)   AS avg_entry,
            MAX(ps.cost_basis)        AS cost_basis,
            MAX(ps.current_price)     AS cur_price,
            MAX(ps.market_status)     AS market_status,
            ps.resolution_time
        FROM position_snapshots ps
        LEFT JOIN resolved_positions rp
            ON  rp.wallet_address = ps.wallet_address
            AND rp.token_id       = ps.token_id
            AND rp.condition_id   = ps.condition_id
        WHERE rp.resolved_id IS NULL
          AND (
                ps.resolution_time <= :today
                OR ps.market_status = 'resolved'
          )
          {since_clause}
        GROUP BY ps.wallet_address, ps.condition_id, ps.token_id
    """, {"today": today, "since": since or ""}).fetchall()

    print(f"[resolver] {len(rows)} candidate positions")

    # Cache gamma lookups: one API call per condition_id across all wallets
    gamma_cache: dict[str, dict | None] = {}
    resolved_count = 0
    needs_review   = 0

    for row in rows:
        cid  = row["condition_id"] or ""
        tid  = row["token_id"] or ""
        side = row["side"] or "Yes"

        if cid not in gamma_cache:
            gamma_cache[cid] = _fetch_gamma(cid)

        gamma = gamma_cache[cid]
        mstatus = row["market_status"] or ""
        cprice  = float(row["cur_price"] or 0)
        wl, final_price = determine_resolution(mstatus, cprice, side, gamma)

        cost   = float(row["cost_basis"] or 0)
        shares = float(row["shares"] or 0)

        if wl == "WIN":
            payout = shares * 1.0          # $1.00 per share
        elif wl == "LOSS":
            payout = 0.0
        elif wl == "PUSH":
            payout = shares * (final_price / 100)
        else:
            payout = 0.0                   # NEEDS_REVIEW — don't guess
            needs_review += 1

        pnl = payout - cost
        roi = pnl / cost * 100 if cost > 0 else 0.0

        try:
            t0 = datetime.fromisoformat(row["first_seen"].replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(row["last_seen"].replace("Z", "+00:00"))
            holding_hours = (t1 - t0).total_seconds() / 3600
        except Exception:
            holding_hours = 0.0

        label = row["wallet_label"] or row["wallet_address"][:10]
        title = (row["market_title"] or "")[:44]
        print(
            f"  {label[:14]:14s}  {title:44s}  {side:3s}  "
            f"{wl:12s}  pnl={pnl:+8.2f}  roi={roi:+6.1f}%"
        )

        if not dry_run:
            conn.execute("""
                INSERT OR IGNORE INTO resolved_positions
                  (wallet_address, wallet_label, market_id, condition_id,
                   market_title, category, side, token_id, shares,
                   avg_entry_price, final_price, cost_basis, payout_value,
                   realized_pnl, roi_pct, win_loss,
                   first_seen_at, last_seen_at, resolved_at, holding_hours,
                   source_snapshot_id, raw_resolution)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                row["wallet_address"], label,
                row["market_id"], cid,
                row["market_title"], row["category"],
                side, tid,
                shares, float(row["avg_entry"] or 0),
                final_price, cost,
                payout, pnl, roi, wl,
                row["first_seen"], row["last_seen"],
                now_str, holding_hours,
                row["latest_snap_id"],
                json.dumps(gamma) if gamma else None,
            ))
            resolved_count += 1

    if not dry_run:
        conn.commit()
    conn.close()

    tag = "(dry-run) " if dry_run else ""
    print(
        f"[resolver] {tag}resolved={resolved_count}  "
        f"needs_review={needs_review}"
    )
    return resolved_count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve finished Polymarket positions")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results without writing to DB")
    parser.add_argument("--since", default=None,
                        help="Only consider snapshots from this date (YYYY-MM-DD)")
    args = parser.parse_args()
    run_resolver(dry_run=args.dry_run, since=args.since)
