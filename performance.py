"""
performance.py  —  aggregate resolved_positions into wallet_performance_summary.

Run this after resolver.py finishes.  Safe to re-run: rows are fully replaced
via UPSERT so stale numbers are never left in the table.

Usage:
    python performance.py
    python performance.py --wallet @swisstony    # update one wallet only
"""

import argparse
import statistics
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

from backtest_db import get_conn, init_db


def _rolling_roi(conn, wallet: str, category: str, days: int) -> Optional[float]:
    """ROI over the last N days for one wallet+category bucket."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    cat_filter = "" if category == "__all__" else "AND category = ?"
    params: list = [wallet, cutoff]
    if category != "__all__":
        params.append(category)
    params.append("NEEDS_REVIEW")

    rows = conn.execute(f"""
        SELECT cost_basis, payout_value
        FROM resolved_positions
        WHERE wallet_address = ?
          AND resolved_at >= ?
          {cat_filter}
          AND win_loss != ?
    """, params).fetchall()

    if not rows:
        return None
    total_cost   = sum(float(r["cost_basis"]   or 0) for r in rows)
    total_payout = sum(float(r["payout_value"] or 0) for r in rows)
    return (total_payout - total_cost) / total_cost * 100 if total_cost > 0 else None


def _upsert_summary(conn, wallet_address: str, category: str) -> bool:
    """Recompute and UPSERT one (wallet, category) bucket. Returns True if row written."""
    cat_filter = "" if category == "__all__" else "AND category = ?"
    params: list = [wallet_address]
    if category != "__all__":
        params.append(category)
    params.append("NEEDS_REVIEW")

    rows = conn.execute(f"""
        SELECT wallet_label, cost_basis, payout_value, realized_pnl,
               win_loss, resolved_at
        FROM resolved_positions
        WHERE wallet_address = ?
          {cat_filter}
          AND win_loss != ?
        ORDER BY resolved_at
    """, params).fetchall()

    if not rows:
        return False

    label     = rows[0]["wallet_label"]
    costs     = [float(r["cost_basis"]   or 0) for r in rows]
    payouts   = [float(r["payout_value"] or 0) for r in rows]
    wins      = sum(1 for r in rows if r["win_loss"] == "WIN")
    losses    = sum(1 for r in rows if r["win_loss"] == "LOSS")
    pushes    = sum(1 for r in rows if r["win_loss"] == "PUSH")
    decisions = wins + losses + pushes

    total_cost  = sum(costs)
    total_pay   = sum(payouts)
    total_pnl   = total_pay - total_cost
    roi         = total_pnl / total_cost * 100 if total_cost > 0 else 0.0
    avg_size    = total_cost / len(costs) if costs else 0.0
    med_size    = statistics.median(costs) if costs else 0.0
    win_rate    = wins / decisions * 100 if decisions > 0 else 0.0

    r7  = _rolling_roi(conn, wallet_address, category, 7)
    r30 = _rolling_roi(conn, wallet_address, category, 30)
    r90 = _rolling_roi(conn, wallet_address, category, 90)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn.execute("""
        INSERT INTO wallet_performance_summary
          (wallet_address, category, total_positions, resolved_positions,
           wins, losses, pushes, win_rate,
           total_cost_basis, total_payout, total_pnl, roi_pct,
           avg_position_size, median_position_size,
           rolling_7d_roi, rolling_30d_roi, rolling_90d_roi,
           last_updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(wallet_address, category) DO UPDATE SET
          total_positions      = excluded.total_positions,
          resolved_positions   = excluded.resolved_positions,
          wins                 = excluded.wins,
          losses               = excluded.losses,
          pushes               = excluded.pushes,
          win_rate             = excluded.win_rate,
          total_cost_basis     = excluded.total_cost_basis,
          total_payout         = excluded.total_payout,
          total_pnl            = excluded.total_pnl,
          roi_pct              = excluded.roi_pct,
          avg_position_size    = excluded.avg_position_size,
          median_position_size = excluded.median_position_size,
          rolling_7d_roi       = excluded.rolling_7d_roi,
          rolling_30d_roi      = excluded.rolling_30d_roi,
          rolling_90d_roi      = excluded.rolling_90d_roi,
          last_updated_at      = excluded.last_updated_at
    """, (
        wallet_address, category,
        len(rows), decisions,
        wins, losses, pushes, win_rate,
        total_cost, total_pay, total_pnl, roi,
        avg_size, med_size,
        r7, r30, r90, now,
    ))
    return True


def run_performance(wallet_filter: Optional[str] = None) -> int:
    conn = init_db()

    # Find all (wallet, category) pairs that have resolved data
    params: list = []
    filter_clause = ""
    if wallet_filter:
        filter_clause = "AND (wallet_label LIKE ? OR wallet_address LIKE ?)"
        params = [f"%{wallet_filter}%", f"%{wallet_filter}%"]

    buckets = conn.execute(f"""
        SELECT DISTINCT wallet_address, category
        FROM resolved_positions
        WHERE win_loss != 'NEEDS_REVIEW'
          {filter_clause}
    """, params).fetchall()

    updated = 0
    wallets_seen: set[str] = set()

    for row in buckets:
        if _upsert_summary(conn, row["wallet_address"], row["category"]):
            wallets_seen.add(row["wallet_address"])
            updated += 1

    # Also recompute the __all__ aggregate for every wallet touched
    for addr in wallets_seen:
        if _upsert_summary(conn, addr, "__all__"):
            updated += 1

    conn.commit()

    # Print a quick summary table
    if wallets_seen:
        print(f"\n{'Wallet':16s}  {'Cat':12s}  {'W-L':6s}  {'WR%':5s}  {'ROI%':7s}  {'P/L':>9s}")
        print("─" * 64)
        for row in conn.execute("""
            SELECT wps.*, tw.wallet_label
            FROM wallet_performance_summary wps
            LEFT JOIN tracked_wallets tw ON tw.wallet_address = wps.wallet_address
            WHERE wps.wallet_address IN ({})
            ORDER BY wps.roi_pct DESC
        """.format(",".join("?" * len(wallets_seen))), list(wallets_seen)).fetchall():
            label = row["wallet_label"] or row["wallet_address"][:10]
            cat   = row["category"]
            print(
                f"{label[:16]:16s}  {cat[:12]:12s}  "
                f"{row['wins']}-{row['losses']:1d}  "
                f"{row['win_rate']:5.1f}%  "
                f"{row['roi_pct']:+6.1f}%  "
                f"${row['total_pnl']:+8.2f}"
            )

    conn.close()
    print(f"\n[performance] updated {updated} wallet-category buckets")
    return updated


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aggregate wallet performance from resolved positions")
    parser.add_argument("--wallet", default=None,
                        help="Only update this wallet (label or address substring)")
    args = parser.parse_args()
    run_performance(wallet_filter=args.wallet)
