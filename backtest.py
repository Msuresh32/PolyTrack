"""
backtest.py  --  point-in-time backtesting for tracked wallet positions.

Point-in-time guarantee
-----------------------
When --as-of is given, only snapshots taken AT OR BEFORE that timestamp are
loaded.  The join to resolved_positions uses the actual outcome (we need to
know whether the bet won) but only for positions whose FIRST SNAPSHOT was
before the cutoff -- so no future position entries leak in.

Entry price methods
-------------------
  avg      (default) -- wallet's avg_entry_price from the API (most realistic)
  snapshot            -- price in the earliest snapshot before cutoff

Usage examples
--------------
    python backtest.py                                  # all positions, all time
    python backtest.py --as-of 2026-01-01T00:00:00Z    # point-in-time
    python backtest.py --category "FIFA WC"
    python backtest.py --wallet @swisstony
    python backtest.py --min-size 50
    python backtest.py --resolve-window 48              # positions resolving <=48h from first snap
    python backtest.py --only-sharp                     # skip NEEDS_REVIEW
    python backtest.py --output results.json
"""

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

from backtest_db import get_conn, init_db


# ── Data loader ───────────────────────────────────────────────────────────────

def load_positions(
    conn,
    as_of: Optional[str] = None,
    category: Optional[str] = None,
    wallet: Optional[str] = None,
    min_size: float = 0.0,
    resolve_window_hours: Optional[float] = None,
    only_resolved: bool = False,
) -> list[dict]:
    """
    Return one row per (wallet, condition_id, token_id).

    Each row represents the first time we saw that position up to `as_of`,
    joined with the final resolved outcome when available.
    """
    wheres: list[str] = []
    params: list = []

    if as_of:
        wheres.append("ps.snapshot_ts <= ?")
        params.append(as_of)

    if category:
        wheres.append("ps.category = ?")
        params.append(category)

    if wallet:
        wheres.append("(ps.wallet_label LIKE ? OR ps.wallet_address LIKE ?)")
        params.extend([f"%{wallet}%", f"%{wallet}%"])

    if only_resolved:
        wheres.append("rp.win_loss IN ('WIN','LOSS','PUSH')")

    where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""

    rows = conn.execute(f"""
        SELECT
            ps.wallet_address,
            ps.wallet_label,
            ps.market_id,
            ps.condition_id,
            ps.market_title,
            ps.category,
            ps.side,
            ps.token_id,
            MIN(ps.snapshot_ts)      AS first_seen,
            MAX(ps.snapshot_ts)      AS last_seen,
            ps.avg_entry_price       AS avg_entry,
            MIN(ps.current_price)    AS snap_entry_price,
            MAX(ps.shares)           AS shares,
            MAX(ps.cost_basis)       AS cost_basis,
            ps.resolution_time,
            rp.win_loss,
            rp.payout_value,
            rp.realized_pnl,
            rp.roi_pct               AS resolved_roi,
            rp.resolved_at,
            rp.holding_hours
        FROM position_snapshots ps
        LEFT JOIN resolved_positions rp
            ON  rp.wallet_address = ps.wallet_address
            AND rp.token_id       = ps.token_id
            AND rp.condition_id   = ps.condition_id
        {where_sql}
        GROUP BY ps.wallet_address, ps.token_id, ps.condition_id
        HAVING MAX(ps.cost_basis) >= ?
    """, [*params, min_size]).fetchall()

    result: list[dict] = []
    for r in rows:
        row = dict(r)
        # Filter by resolve window: how far away was resolution from first snapshot?
        if (
            resolve_window_hours is not None
            and row["resolution_time"]
            and row["first_seen"]
        ):
            try:
                t0  = datetime.fromisoformat(row["first_seen"].replace("Z", "+00:00"))
                res = datetime.fromisoformat(
                    row["resolution_time"] + "T00:00:00+00:00"
                )
                if (res - t0).total_seconds() / 3600 > resolve_window_hours:
                    continue
            except Exception:
                pass
        result.append(row)

    return result


# ── Metrics engine ────────────────────────────────────────────────────────────

def compute_metrics(
    positions: list[dict],
    entry_method: str = "avg",
    as_of_label: str = "all-time",
) -> dict:
    """
    Compute backtest performance metrics from a flat list of position rows.

    Unresolved positions are counted in totals but excluded from P/L metrics
    (we don't know their outcome yet).
    NEEDS_REVIEW positions are also excluded from win/loss counts.
    """
    resolved   = [p for p in positions if p["win_loss"] in ("WIN", "LOSS", "PUSH")]
    wins       = [p for p in resolved if p["win_loss"] == "WIN"]
    losses     = [p for p in resolved if p["win_loss"] == "LOSS"]
    unresolved = [p for p in positions if p["win_loss"] not in ("WIN", "LOSS", "PUSH")]

    decisions  = len(wins) + len(losses)  # PUSH excluded from win-rate denominator
    win_rate   = len(wins) / decisions * 100 if decisions > 0 else 0.0

    costs      = [float(p["cost_basis"]   or 0) for p in positions]
    res_costs  = [float(p["cost_basis"]   or 0) for p in resolved]
    payouts    = [float(p["payout_value"] or 0) for p in resolved]
    pnls       = [float(p["realized_pnl"] or 0) for p in resolved]

    total_cost   = sum(costs)
    total_payout = sum(payouts)
    total_pnl    = sum(pnls)
    roi          = total_pnl / sum(res_costs) * 100 if sum(res_costs) > 0 else 0.0

    def _entry(p: dict) -> float:
        if entry_method == "avg":
            return float(p["avg_entry"] or p["snap_entry_price"] or 0) / 100
        return float(p["snap_entry_price"] or p["avg_entry"] or 0) / 100

    avg_entry_price = (
        sum(_entry(p) for p in positions) / len(positions) if positions else 0.0
    )

    # ── By wallet ─────────────────────────────────────────────────────────────
    by_wallet: dict[str, dict] = defaultdict(lambda: {
        "wins": 0, "losses": 0, "pushes": 0,
        "cost": 0.0, "payout": 0.0, "pnl": 0.0,
    })
    for p in resolved:
        b = by_wallet[p["wallet_label"] or p["wallet_address"][:10]]
        b["wins"]   += 1 if p["win_loss"] == "WIN"  else 0
        b["losses"] += 1 if p["win_loss"] == "LOSS" else 0
        b["pushes"] += 1 if p["win_loss"] == "PUSH" else 0
        b["cost"]   += float(p["cost_basis"]   or 0)
        b["payout"] += float(p["payout_value"] or 0)
        b["pnl"]    += float(p["realized_pnl"] or 0)
    for b in by_wallet.values():
        d = b["wins"] + b["losses"]
        b["roi"]      = b["pnl"] / b["cost"] * 100 if b["cost"] > 0 else 0.0
        b["win_rate"] = b["wins"] / d * 100 if d > 0 else 0.0

    # ── By category ───────────────────────────────────────────────────────────
    by_category: dict[str, dict] = defaultdict(lambda: {
        "wins": 0, "losses": 0, "pushes": 0,
        "cost": 0.0, "payout": 0.0, "pnl": 0.0,
    })
    for p in resolved:
        b = by_category[p["category"] or "Other"]
        b["wins"]   += 1 if p["win_loss"] == "WIN"  else 0
        b["losses"] += 1 if p["win_loss"] == "LOSS" else 0
        b["pushes"] += 1 if p["win_loss"] == "PUSH" else 0
        b["cost"]   += float(p["cost_basis"]   or 0)
        b["payout"] += float(p["payout_value"] or 0)
        b["pnl"]    += float(p["realized_pnl"] or 0)
    for b in by_category.values():
        d = b["wins"] + b["losses"]
        b["roi"]      = b["pnl"] / b["cost"] * 100 if b["cost"] > 0 else 0.0
        b["win_rate"] = b["wins"] / d * 100 if d > 0 else 0.0

    # ── By market ─────────────────────────────────────────────────────────────
    by_market: dict[str, dict] = {}
    for p in resolved:
        key   = p["condition_id"] or p["market_id"] or p["market_title"] or "?"
        title = p["market_title"] or key
        b = by_market.setdefault(key, {
            "title": title, "category": p["category"],
            "wallets_win": 0, "wallets_loss": 0,
            "cost": 0.0, "payout": 0.0, "pnl": 0.0,
        })
        b["wallets_win"]  += 1 if p["win_loss"] == "WIN"  else 0
        b["wallets_loss"] += 1 if p["win_loss"] == "LOSS" else 0
        b["cost"]   += float(p["cost_basis"]   or 0)
        b["payout"] += float(p["payout_value"] or 0)
        b["pnl"]    += float(p["realized_pnl"] or 0)

    # ── Rolling ROI from resolved_at timestamps ───────────────────────────────
    def rolling_roi_from_rows(days: int) -> Optional[float]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        recent = [
            p for p in resolved
            if (p["resolved_at"] or "") >= cutoff
        ]
        rc = sum(float(p["cost_basis"] or 0) for p in recent)
        rp = sum(float(p["payout_value"] or 0) for p in recent)
        return (rp - rc) / rc * 100 if rc > 0 else None

    return {
        "as_of":               as_of_label,
        "total_positions":     len(positions),
        "resolved":            len(resolved),
        "unresolved":          len(unresolved),
        "wins":                len(wins),
        "losses":              len(losses),
        "win_rate_pct":        round(win_rate, 2),
        "total_cost_basis":    round(total_cost, 2),
        "total_payout":        round(total_payout, 2),
        "total_pnl":           round(total_pnl, 2),
        "roi_pct":             round(roi, 2),
        "avg_entry_price":     round(avg_entry_price, 4),
        "avg_position_size":   round(total_cost / len(positions), 2) if positions else 0,
        "median_position_size": round(statistics.median(costs), 2) if costs else 0,
        "rolling_7d_roi":      rolling_roi_from_rows(7),
        "rolling_30d_roi":     rolling_roi_from_rows(30),
        "rolling_90d_roi":     rolling_roi_from_rows(90),
        "by_wallet":           dict(by_wallet),
        "by_category":         dict(by_category),
        "by_market":           by_market,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _print_summary(m: dict) -> None:
    print(f"\n{'═'*70}")
    print(f"  Polymarket Backtest  --  {m['as_of']}")
    print(f"{'═'*70}")
    print(f"  Positions : {m['total_positions']:4d}  "
          f"({m['resolved']} resolved,  {m['unresolved']} open)")
    print(f"  Win/Loss  : {m['wins']}-{m['losses']}  ({m['win_rate_pct']:.1f}% win rate)")
    print(f"  Cost      : ${m['total_cost_basis']:>10,.2f}")
    print(f"  Payout    : ${m['total_payout']:>10,.2f}")
    print(f"  P/L       : ${m['total_pnl']:>+10,.2f}  ({m['roi_pct']:+.1f}% ROI)")
    print(f"  Avg size  : ${m['avg_position_size']:>8,.2f}")

    for label, (r7, r30, r90) in [
        ("7d", (m.get("rolling_7d_roi"), None, None)),
        ("30d", (None, m.get("rolling_30d_roi"), None)),
        ("90d", (None, None, m.get("rolling_90d_roi"))),
    ]:
        val = r7 or r30 or r90
        if val is not None:
            print(f"  ROI {label:3s}   : {val:+.1f}%")

    if m.get("by_wallet"):
        print(f"\n  {'Wallet':20s}  {'W-L':5s}  {'WR%':5s}  {'P/L':>10s}  {'ROI%':>7s}")
        print(f"  {'─'*55}")
        for w, b in sorted(m["by_wallet"].items(), key=lambda x: x[1]["pnl"], reverse=True):
            print(
                f"  {w[:20]:20s}  "
                f"{b['wins']}-{b['losses']:1d}   "
                f"{b['win_rate']:4.0f}%  "
                f"${b['pnl']:>+9,.2f}  "
                f"{b['roi']:+6.1f}%"
            )

    if m.get("by_category"):
        print(f"\n  {'Category':16s}  {'W-L':5s}  {'WR%':5s}  {'P/L':>10s}  {'ROI%':>7s}")
        print(f"  {'─'*55}")
        for c, b in sorted(m["by_category"].items(), key=lambda x: x[1]["pnl"], reverse=True):
            print(
                f"  {c[:16]:16s}  "
                f"{b['wins']}-{b['losses']:1d}   "
                f"{b['win_rate']:4.0f}%  "
                f"${b['pnl']:>+9,.2f}  "
                f"{b['roi']:+6.1f}%"
            )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket position backtester")
    parser.add_argument(
        "--as-of", default=None,
        help="Point-in-time cutoff ISO-8601 UTC  (e.g. 2026-01-01T00:00:00Z)",
    )
    parser.add_argument("--category", default=None,
                        help='Category filter e.g. "FIFA WC"')
    parser.add_argument("--wallet", default=None,
                        help="Wallet label or address substring")
    parser.add_argument("--min-size", type=float, default=0.0,
                        help="Minimum position cost basis ($)")
    parser.add_argument(
        "--resolve-window", type=float, default=None,
        help="Only include positions resolving within N hours of first snapshot",
    )
    parser.add_argument("--only-resolved", action="store_true",
                        help="Exclude open / unresolved positions entirely")
    parser.add_argument("--entry", default="avg",
                        choices=["avg", "snapshot"],
                        help="Entry price method (default: avg)")
    parser.add_argument("--output", default=None,
                        help="Write JSON results to this file")
    args = parser.parse_args()

    conn = init_db()
    positions = load_positions(
        conn,
        as_of=args.as_of,
        category=args.category,
        wallet=args.wallet,
        min_size=args.min_size,
        resolve_window_hours=args.resolve_window,
        only_resolved=args.only_resolved,
    )
    conn.close()

    if not positions:
        print("No positions match the given filters.")
        sys.exit(0)

    metrics = compute_metrics(
        positions,
        entry_method=args.entry,
        as_of_label=args.as_of or "all-time",
    )
    _print_summary(metrics)

    if args.output:
        with open(args.output, "w") as fh:
            json.dump(metrics, fh, indent=2)
        print(f"  Results written → {args.output}")


if __name__ == "__main__":
    main()
