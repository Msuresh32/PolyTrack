"""
Fetch category-specific sharp-wallet candidates from PolymarketAnalytics.

Outputs:
  sharp_wallets_selected.json
  sharp_wallets_rejected.json

The script intentionally favors quality over count. It starts with the PMA
category leaderboard, then checks current Polymarket positions for open
category exposure and near-term resolution.
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

PMA_API = os.getenv(
    "POLYMARKET_ANALYTICS_API",
    "https://legacy.polymarketanalytics.com/api/traders-tag-performance",
)
POSITIONS_API = "https://data-api.polymarket.com/positions"

CATEGORIES = {
    "WNBA": ["wnba"],
    "MLB": ["mlb", "baseball"],
    "FIFA World Cup": ["fifwc", "world-cup", "world cup", "fifa world cup"],
}

DEFAULT_FILTERS = {
    "min_markets": 25,
    "min_positions": 50,
    "min_total_pnl": 10_000,
    "min_win_rate_pct": 52.0,
    "min_roi_pct": 2.0,
    "min_open_volume": 250.0,
    "near_term_days": 2,
    "candidate_limit": 100,
    "per_category_limit": 10,
}


def as_float(value, default=0.0):
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def category_match(category, title, event_slug):
    text = f"{event_slug} {title}".lower()
    return any(token in text for token in CATEGORIES[category])


def is_open_position(position):
    if bool(position.get("redeemable", False)):
        return False
    return as_float(position.get("curPrice")) > 0


def fetch_pma_candidates(category, limit):
    rows = []
    offset = 0
    page_size = min(100, limit)
    while len(rows) < limit:
        r = requests.get(
            PMA_API,
            params={
                "tag": category,
                "limit": page_size,
                "offset": offset,
                "sortColumn": "rank",
                "sortDirection": "ASC",
            },
            timeout=30,
            headers={"User-Agent": "polymarket-tracker/1.0"},
        )
        r.raise_for_status()
        data = r.json().get("data") or []
        if not data:
            break
        rows.extend(data)
        offset += len(data)
        if len(data) < page_size:
            break
    return rows[:limit]


def fetch_positions(address):
    r = requests.get(
        POSITIONS_API,
        params={"user": address, "limit": 500},
        timeout=30,
        headers={"User-Agent": "polymarket-tracker/1.0"},
    )
    if r.status_code != 200:
        return []
    return r.json()


def position_snapshot(category, positions, near_term_days):
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=near_term_days)
    open_positions = []
    near_term_times = []

    for position in positions:
        title = position.get("title", "") or ""
        event_slug = position.get("eventSlug", "") or ""
        if not category_match(category, title, event_slug):
            continue
        if not is_open_position(position):
            continue

        open_positions.append(position)
        end_raw = position.get("endDate") or ""
        if end_raw:
            try:
                end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                if now <= end_dt <= cutoff:
                    near_term_times.append(end_dt)
            except ValueError:
                pass

    open_volume = sum(as_float(p.get("currentValue")) for p in open_positions)
    next_resolution = min(near_term_times).isoformat() if near_term_times else ""
    return {
        "open_volume": round(open_volume, 2),
        "number_of_open_positions_live": len(open_positions),
        "next_resolution_time": next_resolution,
        "has_near_term_position": bool(next_resolution),
    }


def build_record(category, row, filters):
    wallet = (row.get("trader") or "").lower()
    positions = fetch_positions(wallet)
    snapshot = position_snapshot(category, positions, filters["near_term_days"])

    win_amount = as_float(row.get("win_amount"))
    loss_amount = abs(as_float(row.get("loss_amount")))
    resolved_volume = win_amount + loss_amount
    open_volume = snapshot["open_volume"]
    total_volume = resolved_volume + open_volume
    total_pnl = as_float(row.get("overall_gain"))
    roi_pct = total_pnl / total_volume * 100 if total_volume > 0 else 0.0
    win_rate_pct = as_float(row.get("win_rate")) * 100
    markets = int(as_float(row.get("event_ct")))
    positions_count = int(as_float(row.get("total_positions")))
    active_positions = int(as_float(row.get("active_positions")))
    label = row.get("trader_name") or wallet

    record = {
        "wallet_address": wallet,
        "wallet_label": label,
        "category": category,
        "total_volume": round(total_volume, 2),
        "resolved_volume": round(resolved_volume, 2),
        "open_volume": open_volume,
        "total_pnl": round(total_pnl, 2),
        "roi_pct": round(roi_pct, 2),
        "win_rate": round(win_rate_pct, 2),
        "number_of_trades": positions_count,
        "number_of_resolved_positions": markets,
        "number_of_open_positions": active_positions,
        "number_of_open_positions_live": snapshot["number_of_open_positions_live"],
        "next_resolution_time": snapshot["next_resolution_time"],
        "source_url": (
            "https://legacy.polymarketanalytics.com/traders"
            f"?overallCategory={category.replace(' ', '+')}&search={wallet}"
        ),
        "rank": row.get("rank"),
        "win_count": int(as_float(row.get("win_count"))),
        "reason_selected": "",
        "reason_rejected": "",
        "ingestion_timestamp": iso_now(),
    }
    return record


def rejection_reasons(record, filters):
    reasons = []
    if record["number_of_resolved_positions"] < filters["min_markets"]:
        reasons.append(f"resolved markets < {filters['min_markets']}")
    if record["number_of_trades"] < filters["min_positions"]:
        reasons.append(f"positions < {filters['min_positions']}")
    if record["total_pnl"] < filters["min_total_pnl"]:
        reasons.append(f"total P/L < ${filters['min_total_pnl']:,.0f}")
    if record["win_rate"] < filters["min_win_rate_pct"]:
        reasons.append(f"win rate < {filters['min_win_rate_pct']}%")
    if record["roi_pct"] < filters["min_roi_pct"]:
        reasons.append(f"ROI < {filters['min_roi_pct']}%")
    if record["open_volume"] < filters["min_open_volume"]:
        reasons.append(f"open category exposure < ${filters['min_open_volume']:,.0f}")
    if not record["next_resolution_time"]:
        reasons.append(f"no category position resolving within {filters['near_term_days']}d")
    return reasons


def select_wallets(filters):
    selected = []
    rejected = []
    seen = set()

    for category in CATEGORIES:
        category_selected = 0
        candidates = fetch_pma_candidates(category, filters["candidate_limit"])
        for row in candidates:
            record = build_record(category, row, filters)
            key = (record["wallet_address"], category)
            if key in seen:
                continue
            seen.add(key)

            reasons = rejection_reasons(record, filters)
            if reasons or category_selected >= filters["per_category_limit"]:
                record["reason_rejected"] = (
                    "; ".join(reasons)
                    if reasons else f"category cap {filters['per_category_limit']} already filled"
                )
                rejected.append(record)
                continue

            record["reason_selected"] = (
                f"rank #{record['rank']}; {record['number_of_resolved_positions']} markets; "
                f"{record['win_rate']}% win rate; ${record['total_pnl']:,.0f} P/L; "
                f"${record['open_volume']:,.0f} open category exposure"
            )
            selected.append(record)
            category_selected += 1

        time.sleep(0.25)

    return selected, rejected


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--selected", default="sharp_wallets_selected.json")
    parser.add_argument("--rejected", default="sharp_wallets_rejected.json")
    parser.add_argument("--candidate-limit", type=int, default=DEFAULT_FILTERS["candidate_limit"])
    parser.add_argument("--per-category-limit", type=int, default=DEFAULT_FILTERS["per_category_limit"])
    parser.add_argument("--min-markets", type=int, default=DEFAULT_FILTERS["min_markets"])
    parser.add_argument("--min-positions", type=int, default=DEFAULT_FILTERS["min_positions"])
    parser.add_argument("--min-total-pnl", type=float, default=DEFAULT_FILTERS["min_total_pnl"])
    parser.add_argument("--min-win-rate-pct", type=float, default=DEFAULT_FILTERS["min_win_rate_pct"])
    parser.add_argument("--min-roi-pct", type=float, default=DEFAULT_FILTERS["min_roi_pct"])
    parser.add_argument("--min-open-volume", type=float, default=DEFAULT_FILTERS["min_open_volume"])
    parser.add_argument("--near-term-days", type=int, default=DEFAULT_FILTERS["near_term_days"])
    args = parser.parse_args()

    filters = {
        "candidate_limit": args.candidate_limit,
        "per_category_limit": args.per_category_limit,
        "min_markets": args.min_markets,
        "min_positions": args.min_positions,
        "min_total_pnl": args.min_total_pnl,
        "min_win_rate_pct": args.min_win_rate_pct,
        "min_roi_pct": args.min_roi_pct,
        "min_open_volume": args.min_open_volume,
        "near_term_days": args.near_term_days,
    }

    selected, rejected = select_wallets(filters)
    output = {
        "source": PMA_API,
        "filters": filters,
        "ingestion_timestamp": iso_now(),
        "wallets": selected,
    }
    audit = {
        "source": PMA_API,
        "filters": filters,
        "ingestion_timestamp": iso_now(),
        "wallets": rejected,
    }
    write_json(args.selected, output)
    write_json(args.rejected, audit)

    by_category = {}
    for wallet in selected:
        by_category.setdefault(wallet["category"], 0)
        by_category[wallet["category"]] += 1
    print("Selected:", by_category)
    print(f"Wrote {args.selected} and {args.rejected}")


if __name__ == "__main__":
    main()
