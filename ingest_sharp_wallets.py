"""
Comprehensive sharp-wallet ingestion for all major Polymarket sports.

Data source: PolymarketAnalytics leaderboard + Polymarket positions API.

Scoring uses available fields only. Fields not in PMA (true CLV, arb %,
bet-size std-dev, market timing) are estimated from heuristics and marked
as such. Do not treat those estimates as ground truth.

Passes
------
1. Strict  — established wallets with large samples and strong metrics.
2. Relaxed — catches emerging sharps; only added if sharpness_score >= 68.
3. Esports — separate relaxed pass for LoL/CS2/Valorant/Dota; only added
             if sharpness_score >= 65 (lower liquidity means smaller samples).

Output files
------------
sharp_wallets_selected.json  — dashboard-compatible (SHARP_WALLETS_FILE).
sharp_wallets_rejected.json  — audit trail for all candidates.
sharp_wallets_master.json    — full database: scores, clusters, flags.
"""

import argparse
import json
import math
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

# ── Sport configs ──────────────────────────────────────────────────────────────
# pma_tag          : exact tag string the PMA API accepts
# dashboard_cat    : category label used by dashboard.py
# keywords         : substrings to match event/title text
# priority         : 1 = highest (MLB, NBA …) — used to order the output
# per_limit        : max wallets kept per sport
# candidate_limit  : how many PMA rows to fetch before filtering
# min_markets      : minimum resolved markets
# min_positions    : minimum total positions
# min_pnl          : minimum total P&L (USD)
# min_win_rate     : minimum win rate %
# min_roi          : minimum ROI %
# esports          : True = use esports relaxed pass thresholds

SPORT_CONFIGS = {
    "MLB": {
        "pma_tag": "MLB", "dashboard_cat": "MLB",
        "keywords": ["mlb", "baseball"],
        "priority": 1, "per_limit": 15, "candidate_limit": 200,
        "min_markets": 25, "min_positions": 50, "min_pnl": 8_000,
        "min_win_rate": 52.0, "min_roi": 2.0, "esports": False,
    },
    "NBA": {
        "pma_tag": "NBA", "dashboard_cat": "NBA",
        "keywords": ["nba", "basketball"],
        "priority": 2, "per_limit": 15, "candidate_limit": 200,
        "min_markets": 25, "min_positions": 50, "min_pnl": 8_000,
        "min_win_rate": 52.0, "min_roi": 2.0, "esports": False,
    },
    "NFL": {
        "pma_tag": "NFL", "dashboard_cat": "NFL",
        "keywords": ["nfl", "football"],
        "priority": 3, "per_limit": 15, "candidate_limit": 200,
        "min_markets": 20, "min_positions": 40, "min_pnl": 8_000,
        "min_win_rate": 52.0, "min_roi": 2.0, "esports": False,
    },
    "NHL": {
        "pma_tag": "NHL", "dashboard_cat": "NHL",
        "keywords": ["nhl", "hockey"],
        "priority": 4, "per_limit": 12, "candidate_limit": 150,
        "min_markets": 20, "min_positions": 40, "min_pnl": 5_000,
        "min_win_rate": 52.0, "min_roi": 2.0, "esports": False,
    },
    "Soccer": {
        "pma_tag": "Soccer", "dashboard_cat": "Soccer",
        "keywords": ["epl", "ucl", "laliga", "premier-league", "premier league",
                     "champions-league", "champions league", "mls", "serie-a",
                     "bundesliga", "ligue-1", "soccer"],
        "priority": 5, "per_limit": 12, "candidate_limit": 150,
        "min_markets": 20, "min_positions": 40, "min_pnl": 5_000,
        "min_win_rate": 52.0, "min_roi": 2.0, "esports": False,
    },
    "League of Legends": {
        "pma_tag": "League of Legends", "dashboard_cat": "Esports",
        "keywords": ["league-of-legends", "league of legends", "lol"],
        "priority": 6, "per_limit": 8, "candidate_limit": 100,
        "min_markets": 10, "min_positions": 20, "min_pnl": 1_000,
        "min_win_rate": 53.0, "min_roi": 2.0, "esports": True,
    },
    "CS2": {
        "pma_tag": "CS2", "dashboard_cat": "Esports",
        "keywords": ["csgo", "cs2", "counter-strike"],
        "priority": 7, "per_limit": 8, "candidate_limit": 100,
        "min_markets": 10, "min_positions": 20, "min_pnl": 1_000,
        "min_win_rate": 53.0, "min_roi": 2.0, "esports": True,
    },
    "Valorant": {
        "pma_tag": "Valorant", "dashboard_cat": "Esports",
        "keywords": ["valorant"],
        "priority": 8, "per_limit": 8, "candidate_limit": 100,
        "min_markets": 8, "min_positions": 15, "min_pnl": 500,
        "min_win_rate": 53.0, "min_roi": 2.0, "esports": True,
    },
    "Dota 2": {
        "pma_tag": "Dota 2", "dashboard_cat": "Esports",
        "keywords": ["dota"],
        "priority": 9, "per_limit": 8, "candidate_limit": 100,
        "min_markets": 8, "min_positions": 15, "min_pnl": 500,
        "min_win_rate": 53.0, "min_roi": 2.0, "esports": True,
    },
    "Tennis": {
        "pma_tag": "Tennis", "dashboard_cat": "Tennis",
        "keywords": ["tennis", "wimbledon", "atp", "wta", "us-open",
                     "french-open", "australian-open"],
        "priority": 10, "per_limit": 10, "candidate_limit": 150,
        "min_markets": 20, "min_positions": 40, "min_pnl": 3_000,
        "min_win_rate": 52.0, "min_roi": 2.0, "esports": False,
    },
    "Golf": {
        "pma_tag": "Golf", "dashboard_cat": "Golf",
        "keywords": ["pga", " golf", "masters", "ryder"],
        "priority": 11, "per_limit": 8, "candidate_limit": 100,
        "min_markets": 15, "min_positions": 30, "min_pnl": 3_000,
        "min_win_rate": 52.0, "min_roi": 2.0, "esports": False,
    },
    "UFC": {
        "pma_tag": "UFC", "dashboard_cat": "UFC/MMA",
        "keywords": ["ufc", "mma", "bellator"],
        "priority": 12, "per_limit": 10, "candidate_limit": 150,
        "min_markets": 15, "min_positions": 30, "min_pnl": 3_000,
        "min_win_rate": 52.0, "min_roi": 2.0, "esports": False,
    },
    "Boxing": {
        "pma_tag": "Boxing", "dashboard_cat": "Boxing",
        "keywords": ["boxing"],
        "priority": 13, "per_limit": 6, "candidate_limit": 75,
        "min_markets": 10, "min_positions": 20, "min_pnl": 2_000,
        "min_win_rate": 52.0, "min_roi": 2.0, "esports": False,
    },
    "Cricket": {
        "pma_tag": "Cricket", "dashboard_cat": "Cricket",
        "keywords": ["cricket"],
        "priority": 14, "per_limit": 6, "candidate_limit": 75,
        "min_markets": 10, "min_positions": 20, "min_pnl": 2_000,
        "min_win_rate": 52.0, "min_roi": 2.0, "esports": False,
    },
    "Formula 1": {
        "pma_tag": "Formula 1", "dashboard_cat": "F1",
        "keywords": ["formula-1", "formula1", "grand-prix"],
        "priority": 15, "per_limit": 6, "candidate_limit": 75,
        "min_markets": 10, "min_positions": 20, "min_pnl": 2_000,
        "min_win_rate": 52.0, "min_roi": 2.0, "esports": False,
    },
    "NCAAF": {
        "pma_tag": "NCAAF", "dashboard_cat": "NCAAF",
        "keywords": ["ncaaf", "college-football", "college football"],
        "priority": 16, "per_limit": 8, "candidate_limit": 100,
        "min_markets": 15, "min_positions": 30, "min_pnl": 3_000,
        "min_win_rate": 52.0, "min_roi": 2.0, "esports": False,
    },
    "NCAAB": {
        "pma_tag": "NCAAB", "dashboard_cat": "NCAAB",
        "keywords": ["ncaab", "march-madness", "march madness"],
        "priority": 17, "per_limit": 8, "candidate_limit": 100,
        "min_markets": 15, "min_positions": 30, "min_pnl": 3_000,
        "min_win_rate": 52.0, "min_roi": 2.0, "esports": False,
    },
    "WNBA": {
        "pma_tag": "WNBA", "dashboard_cat": "WNBA",
        "keywords": ["wnba"],
        "priority": 18, "per_limit": 8, "candidate_limit": 100,
        "min_markets": 20, "min_positions": 40, "min_pnl": 5_000,
        "min_win_rate": 52.0, "min_roi": 2.0, "esports": False,
    },
    "FIFA World Cup": {
        "pma_tag": "FIFA World Cup", "dashboard_cat": "FIFA WC",
        "keywords": ["fifwc", "world-cup", "world cup", "fifa world cup"],
        "priority": 19, "per_limit": 8, "candidate_limit": 100,
        "min_markets": 15, "min_positions": 30, "min_pnl": 3_000,
        "min_win_rate": 52.0, "min_roi": 2.0, "esports": False,
    },
}

ESPORTS_SPORTS = {k for k, v in SPORT_CONFIGS.items() if v["esports"]}


# ── Sharpness scoring ──────────────────────────────────────────────────────────

def clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def skill_score(win_rate: float) -> float:
    """Win rate → 0–100. Baseline 50% = 0, 65%+ = 100."""
    return clamp((win_rate - 50.0) / 15.0 * 100.0)


def sample_score(markets: int) -> float:
    """Log-scaled market count. 500 resolved markets = 100."""
    if markets <= 0:
        return 0.0
    return clamp(math.log10(markets) / math.log10(500) * 100.0)


def clv_proxy_score(roi_pct: float) -> float:
    """
    True CLV needs pre-close prices which PMA doesn't expose.
    Proxy: sustained positive ROI at scale implies positive CLV.
    15% ROI → 100.
    """
    return clamp(roi_pct / 15.0 * 100.0)


def roi_score(roi_pct: float) -> float:
    """20% ROI → 100."""
    return clamp(roi_pct / 20.0 * 100.0)


def consistency_score(positions: int, markets: int) -> float:
    """
    Lower positions-per-market ratio → more consistent sizing.
    Ratio 1 = 100, ratio 20+ = 0.
    """
    if markets <= 0:
        return 50.0
    ratio = positions / markets
    return clamp(100.0 - (ratio - 1.0) / 19.0 * 100.0)


def low_arb_score(win_rate: float, markets: int) -> float:
    """
    Heuristic arb detection via win rate:
    clean sharps win 53–67% of resolved markets.
    Very high win rates on large samples are almost always arb or CLV gaming.
    """
    if win_rate > 78 and markets >= 20:
        return 5.0
    if win_rate > 72 and markets >= 15:
        return 30.0
    if win_rate > 67:
        return 65.0
    if win_rate >= 52:
        return 90.0
    return 45.0


def compute_sharpness(record: dict) -> dict:
    wr  = record["win_rate"]
    mkt = record["number_of_resolved_positions"]
    pos = record["number_of_trades"]
    roi = record["roi_pct"]

    ss  = skill_score(wr)
    sa  = sample_score(mkt)
    clv = clv_proxy_score(roi)
    rs  = roi_score(roi)
    cs  = consistency_score(pos, mkt)
    ts  = 50.0          # market timing — not available from PMA
    las = low_arb_score(wr, mkt)

    composite = (
        0.35 * ss
        + 0.20 * sa
        + 0.15 * clv
        + 0.10 * rs
        + 0.10 * cs
        + 0.05 * ts
        + 0.05 * las
    )

    return {
        "sharpness_score":        round(composite),
        "score_skill":            round(ss),
        "score_sample_size":      round(sa),
        "score_clv_proxy":        round(clv),
        "score_roi":              round(rs),
        "score_consistency":      round(cs),
        "score_timing_estimated": round(ts),
        "score_low_arb":          round(las),
        "clv_estimated":          True,
        "arb_pct_estimated":      True,
        "timing_estimated":       True,
    }


def suspicious_flags(record: dict) -> list:
    flags = []
    wr  = record["win_rate"]
    mkt = record["number_of_resolved_positions"]
    pos = record["number_of_trades"]
    pnl = record["total_pnl"]

    if wr > 78 and mkt >= 20:
        flags.append("arb_suspect_high_winrate")
    if wr > 72 and mkt >= 15:
        flags.append("winrate_above_sharp_ceiling")
    if mkt < 10 and pnl > 50_000:
        flags.append("hot_streak_small_sample")
    if mkt > 0 and pos / mkt > 30:
        flags.append("farming_high_position_ratio")
    if record.get("roi_pct", 0) > 60:
        flags.append("roi_exceeds_sustainable_threshold")
    return flags


# ── API helpers ────────────────────────────────────────────────────────────────

def as_float(value, default=0.0):
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def fetch_pma_candidates(pma_tag: str, limit: int) -> list:
    rows = []
    offset = 0
    page_size = min(100, limit)
    while len(rows) < limit:
        try:
            r = requests.get(
                PMA_API,
                params={
                    "tag": pma_tag,
                    "limit": page_size,
                    "offset": offset,
                    "sortColumn": "rank",
                    "sortDirection": "ASC",
                },
                timeout=30,
                headers={"User-Agent": "polymarket-tracker/1.0"},
            )
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f"  [PMA] fetch error for {pma_tag}: {exc}")
            break
        data = r.json().get("data") or []
        if not data:
            break
        rows.extend(data)
        offset += len(data)
        if len(data) < page_size:
            break
    return rows[:limit]


def fetch_positions(address: str) -> list:
    try:
        r = requests.get(
            POSITIONS_API,
            params={"user": address, "limit": 500},
            timeout=30,
            headers={"User-Agent": "polymarket-tracker/1.0"},
        )
        if r.status_code == 200:
            return r.json()
    except requests.RequestException:
        pass
    return []


def category_match(keywords: list, title: str, event_slug: str) -> bool:
    text = f"{event_slug} {title}".lower()
    return any(kw in text for kw in keywords)


def is_open_position(position: dict) -> bool:
    if bool(position.get("redeemable", False)):
        return False
    return as_float(position.get("curPrice")) > 0


def live_snapshot(sport_name: str, keywords: list, positions: list, near_term_days: int) -> dict:
    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=near_term_days)
    open_positions = []
    near_term_times = []

    for p in positions:
        if not category_match(keywords, p.get("title", ""), p.get("eventSlug", "")):
            continue
        if not is_open_position(p):
            continue
        open_positions.append(p)
        end_raw = p.get("endDate") or ""
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
    return {
        "open_volume":                 round(open_volume, 2),
        "number_of_open_positions_live": len(open_positions),
        "next_resolution_time":        min(near_term_times).isoformat() if near_term_times else "",
        "has_near_term_position":      bool(near_term_times),
    }


def build_record(sport_name: str, cfg: dict, row: dict, near_term_days: int) -> dict:
    wallet    = (row.get("trader") or "").lower()
    positions = fetch_positions(wallet)
    snapshot  = live_snapshot(sport_name, cfg["keywords"], positions, near_term_days)

    win_amount    = as_float(row.get("win_amount"))
    loss_amount   = abs(as_float(row.get("loss_amount")))
    resolved_vol  = win_amount + loss_amount
    open_vol      = snapshot["open_volume"]
    total_vol     = resolved_vol + open_vol
    total_pnl     = as_float(row.get("overall_gain"))
    roi_pct       = total_pnl / total_vol * 100 if total_vol > 0 else 0.0
    win_rate_pct  = as_float(row.get("win_rate")) * 100
    markets       = int(as_float(row.get("event_ct")))
    pos_count     = int(as_float(row.get("total_positions")))
    active_pos    = int(as_float(row.get("active_positions")))
    win_count     = int(as_float(row.get("win_count")))
    label         = row.get("trader_name") or wallet

    record = {
        # identity
        "wallet_address":                    wallet,
        "wallet_label":                      label,
        "category":                          cfg["dashboard_cat"],
        "pma_sport":                         sport_name,
        # volume / P&L
        "total_volume":                      round(total_vol, 2),
        "resolved_volume":                   round(resolved_vol, 2),
        "open_volume":                       round(open_vol, 2),
        "total_pnl":                         round(total_pnl, 2),
        "roi_pct":                           round(roi_pct, 2),
        # skill
        "win_rate":                          round(win_rate_pct, 2),
        "win_count":                         win_count,
        "number_of_resolved_positions":      markets,
        # sizing / activity
        "number_of_trades":                  pos_count,
        "number_of_open_positions":          active_pos,
        "number_of_open_positions_live":     snapshot["number_of_open_positions_live"],
        # fields not available from PMA (documented)
        "avg_bet_size_usd":                  round(resolved_vol / max(pos_count, 1), 2),
        "avg_market_liquidity_usd":          None,   # requires on-chain data
        "clv_estimated":                     True,
        "arb_pct_estimated":                 True,
        "avg_hold_time_hours":               None,   # requires fill timestamps
        # live data
        "next_resolution_time":              snapshot["next_resolution_time"],
        "has_near_term_position":            snapshot["has_near_term_position"],
        # source
        "rank":                              row.get("rank"),
        "source_url": (
            "https://legacy.polymarketanalytics.com/traders"
            f"?overallCategory={sport_name.replace(' ', '+')}&search={wallet}"
        ),
        "ingestion_timestamp":               iso_now(),
        # set by caller
        "reason_selected":                   "",
        "reason_rejected":                   "",
        "pass_label":                        "",
    }
    return record


# ── Selection logic ────────────────────────────────────────────────────────────

def meets_filters(record: dict, cfg: dict, scale: float = 1.0) -> list:
    """Return list of rejection reasons, empty if record passes."""
    reasons = []
    if record["number_of_resolved_positions"] < cfg["min_markets"] * scale:
        reasons.append(f"resolved markets < {cfg['min_markets'] * scale:.0f}")
    if record["number_of_trades"] < cfg["min_positions"] * scale:
        reasons.append(f"positions < {cfg['min_positions'] * scale:.0f}")
    if record["total_pnl"] < cfg["min_pnl"] * scale:
        reasons.append(f"total P/L < ${cfg['min_pnl'] * scale:,.0f}")
    if record["win_rate"] < cfg["min_win_rate"]:
        reasons.append(f"win rate < {cfg['min_win_rate']}%")
    if record["roi_pct"] < cfg["min_roi"]:
        reasons.append(f"ROI < {cfg['min_roi']}%")
    return reasons


def run_pass(
    sport_name: str,
    cfg: dict,
    filter_scale: float,
    min_sharpness: int,
    near_term_days: int,
    already_selected: set,
    require_near_term: bool,
) -> tuple[list, list]:
    """Fetch candidates for one sport on one pass. Returns (selected, rejected)."""
    selected = []
    rejected = []

    print(f"  [{sport_name}] fetching up to {cfg['candidate_limit']} candidates …")
    candidates = fetch_pma_candidates(cfg["pma_tag"], cfg["candidate_limit"])
    print(f"  [{sport_name}] {len(candidates)} candidates returned")

    sport_selected = 0
    for row in candidates:
        record = build_record(sport_name, cfg, row, near_term_days)
        key = (record["wallet_address"], sport_name)
        if key in already_selected:
            continue

        scores = compute_sharpness(record)
        record.update(scores)
        record["flags"] = suspicious_flags(record)

        reasons = meets_filters(record, cfg, filter_scale)
        if require_near_term and not record["has_near_term_position"]:
            reasons.append(f"no position resolving within {near_term_days}d")

        sharp = record["sharpness_score"]
        if reasons or sharp < min_sharpness or sport_selected >= cfg["per_limit"]:
            if not reasons and sharp < min_sharpness:
                reasons.append(f"sharpness_score {sharp} < {min_sharpness}")
            if not reasons:
                reasons.append(f"per-sport cap {cfg['per_limit']} reached")
            record["reason_rejected"] = "; ".join(reasons)
            rejected.append(record)
            continue

        record["reason_selected"] = (
            f"rank #{record['rank']}; {record['number_of_resolved_positions']} markets; "
            f"{record['win_rate']}% win; ${record['total_pnl']:,.0f} P/L; "
            f"sharpness {sharp}"
        )
        selected.append(record)
        already_selected.add(key)
        sport_selected += 1

        time.sleep(0.1)

    return selected, rejected


# ── Clustering ─────────────────────────────────────────────────────────────────

def cluster_wallets(all_selected: list) -> dict:
    """Group wallets by sport, find multi-sport, find esports specialists."""
    by_sport: dict[str, list] = {}
    addr_sports: dict[str, set] = {}

    for rec in all_selected:
        sport = rec["pma_sport"]
        addr  = rec["wallet_address"]
        by_sport.setdefault(sport, []).append(rec)
        addr_sports.setdefault(addr, set()).add(sport)

    multi_sport = [
        {"wallet_address": addr, "sports": sorted(sports)}
        for addr, sports in addr_sports.items()
        if len(sports) >= 2
    ]
    multi_sport.sort(key=lambda x: len(x["sports"]), reverse=True)

    esports_specialists = [
        {"wallet_address": addr, "sports": sorted(sports)}
        for addr, sports in addr_sports.items()
        if sports.issubset(ESPORTS_SPORTS) and len(sports) >= 1
    ]

    suspicious = [
        {"wallet_address": r["wallet_address"], "sport": r["pma_sport"], "flags": r["flags"]}
        for r in all_selected
        if r.get("flags")
    ]

    top10_by_sport = {
        sport: sorted(recs, key=lambda r: r["sharpness_score"], reverse=True)[:10]
        for sport, recs in by_sport.items()
    }

    return {
        "by_sport":           by_sport,
        "multi_sport_wallets": multi_sport,
        "esports_specialists": esports_specialists,
        "suspicious_wallets":  suspicious,
        "top10_by_sport":     {
            sport: [{"wallet_address": r["wallet_address"], "sharpness_score": r["sharpness_score"]}
                    for r in recs]
            for sport, recs in top10_by_sport.items()
        },
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def write_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--selected",       default="sharp_wallets_selected.json")
    parser.add_argument("--rejected",       default="sharp_wallets_rejected.json")
    parser.add_argument("--master",         default="sharp_wallets_master.json")
    parser.add_argument("--near-term-days", type=int,   default=2)
    parser.add_argument("--sports",         nargs="*",  default=None,
                        help="Limit to specific sport names (default: all)")
    parser.add_argument("--no-near-term-filter", action="store_true",
                        help="Don't require a position resolving soon (builds fuller database)")
    args = parser.parse_args()

    target_sports = set(args.sports) if args.sports else set(SPORT_CONFIGS)
    require_near_term = not args.no_near_term_filter

    all_selected: list = []
    all_rejected: list = []
    seen: set           = set()      # (wallet_address, sport_name) dedup

    sport_order = sorted(
        [s for s in SPORT_CONFIGS if s in target_sports],
        key=lambda s: SPORT_CONFIGS[s]["priority"],
    )

    # ── Pass 1: strict ────────────────────────────────────────────────────────
    print("\n=== Pass 1: strict filters ===")
    for sport_name in sport_order:
        cfg = SPORT_CONFIGS[sport_name]
        sel, rej = run_pass(
            sport_name, cfg,
            filter_scale=1.0,
            min_sharpness=55,
            near_term_days=args.near_term_days,
            already_selected=seen,
            require_near_term=require_near_term,
        )
        for r in sel:
            r["pass_label"] = "strict"
        all_selected.extend(sel)
        all_rejected.extend(rej)
        print(f"  [{sport_name}] selected {len(sel)}, rejected {len(rej)}")

    # ── Pass 2: relaxed (non-esports) ─────────────────────────────────────────
    print("\n=== Pass 2: relaxed filters (non-esports) ===")
    for sport_name in sport_order:
        if SPORT_CONFIGS[sport_name]["esports"]:
            continue
        cfg = SPORT_CONFIGS[sport_name]
        sel, rej = run_pass(
            sport_name, cfg,
            filter_scale=0.6,
            min_sharpness=68,
            near_term_days=args.near_term_days,
            already_selected=seen,
            require_near_term=False,       # relaxed: no near-term requirement
        )
        for r in sel:
            r["pass_label"] = "relaxed"
        all_selected.extend(sel)
        all_rejected.extend(rej)
        if sel:
            print(f"  [{sport_name}] added {len(sel)} from relaxed pass")

    # ── Pass 3: esports relaxed ───────────────────────────────────────────────
    print("\n=== Pass 3: esports relaxed filters ===")
    for sport_name in sport_order:
        if not SPORT_CONFIGS[sport_name]["esports"]:
            continue
        cfg = SPORT_CONFIGS[sport_name]
        sel, rej = run_pass(
            sport_name, cfg,
            filter_scale=0.5,
            min_sharpness=65,
            near_term_days=args.near_term_days,
            already_selected=seen,
            require_near_term=False,
        )
        for r in sel:
            r["pass_label"] = "esports_relaxed"
        all_selected.extend(sel)
        all_rejected.extend(rej)
        if sel:
            print(f"  [{sport_name}] added {len(sel)} from esports-relaxed pass")

    # ── Cluster + summarise ───────────────────────────────────────────────────
    clusters = cluster_wallets(all_selected)

    by_sport_count = {
        sport: len(recs) for sport, recs in clusters["by_sport"].items()
    }
    print(f"\n=== Results ===")
    print(f"Total selected: {len(all_selected)}")
    for sport, count in sorted(by_sport_count.items(), key=lambda x: SPORT_CONFIGS.get(x[0], {}).get("priority", 99)):
        print(f"  {sport}: {count}")
    print(f"Multi-sport wallets: {len(clusters['multi_sport_wallets'])}")
    print(f"Esports specialists: {len(clusters['esports_specialists'])}")
    print(f"Flagged suspicious:  {len(clusters['suspicious_wallets'])}")

    # ── dashboard-compatible output (sharp_wallets_selected.json) ─────────────
    dashboard_wallets = []
    for r in sorted(all_selected, key=lambda x: x.get("sharpness_score", 0), reverse=True):
        dashboard_wallets.append({
            "wallet_address":                r["wallet_address"],
            "wallet_label":                  r["wallet_label"],
            "category":                      r["category"],
            "total_volume":                  r["total_volume"],
            "resolved_volume":               r["resolved_volume"],
            "open_volume":                   r["open_volume"],
            "total_pnl":                     r["total_pnl"],
            "roi_pct":                       r["roi_pct"],
            "win_rate":                      r["win_rate"],
            "number_of_trades":              r["number_of_trades"],
            "number_of_resolved_positions":  r["number_of_resolved_positions"],
            "number_of_open_positions":      r["number_of_open_positions"],
            "number_of_open_positions_live": r["number_of_open_positions_live"],
            "next_resolution_time":          r["next_resolution_time"],
            "source_url":                    r["source_url"],
            "rank":                          r["rank"],
            "win_count":                     r["win_count"],
            "reason_selected":               r["reason_selected"],
            "reason_rejected":               r["reason_rejected"],
            "ingestion_timestamp":           r["ingestion_timestamp"],
        })

    write_json(args.selected, {
        "source":               PMA_API,
        "ingestion_timestamp":  iso_now(),
        "total_wallets":        len(dashboard_wallets),
        "wallets":              dashboard_wallets,
    })

    write_json(args.rejected, {
        "source":               PMA_API,
        "ingestion_timestamp":  iso_now(),
        "total_rejected":       len(all_rejected),
        "wallets":              all_rejected,
    })

    write_json(args.master, {
        "source":               PMA_API,
        "ingestion_timestamp":  iso_now(),
        "total_wallets":        len(all_selected),
        "scoring_weights": {
            "predictive_skill":         "35%",
            "sample_size":              "20%",
            "clv_proxy":                "15% (estimated — no pre-close prices from PMA)",
            "roi":                      "10%",
            "position_sizing_consistency": "10%",
            "market_timing":            "5% (estimated — not available from PMA)",
            "low_arbitrage_rate":       "5% (heuristic only)",
        },
        "clusters":             {
            "multi_sport_wallets": clusters["multi_sport_wallets"],
            "esports_specialists": clusters["esports_specialists"],
            "suspicious_wallets":  clusters["suspicious_wallets"],
            "top10_by_sport":      clusters["top10_by_sport"],
        },
        "wallets": sorted(all_selected, key=lambda x: x.get("sharpness_score", 0), reverse=True),
    })

    print(f"\nWrote {args.selected}, {args.rejected}, {args.master}")


if __name__ == "__main__":
    main()
