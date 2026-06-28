"""
Polymarket multi-wallet tracker -> Discord alerts.

Polls data-api.polymarket.com/activity for each target wallet, diffs against
what it has already seen, and posts new fills to a Discord channel via webhook.

NOTE: the original script used clob.polymarket.com/trades?maker_address= which
now returns 401 (requires a CLOB API key).  Switched to the public data-api
activity endpoint which returns richer fields (usdcSize, title, eventSlug).

Setup:
    pip install aiohttp python-dotenv
    cp .env.example .env   # fill in webhook, wallets, and bankroll
    python monitor.py
"""

import os
import asyncio
import sqlite3
import random
import statistics
from datetime import datetime, timezone

import aiohttp
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
POLL_SECONDS    = int(os.getenv("POLL_SECONDS",    "60"))
MIN_ALERT_USD   = float(os.getenv("MIN_ALERT_USD", "200"))
SPORTS_ONLY     = os.getenv("SPORTS_ONLY", "true").lower() == "true"
MY_BANKROLL     = float(os.getenv("MY_BANKROLL",   "1000"))
MY_BASE_UNIT    = float(os.getenv("MY_BASE_UNIT",  "50"))

# Switched from clob.polymarket.com/trades (requires API key) to the
# public data-api endpoint.  Query param is "user", not "maker_address".
DATA_API = "https://data-api.polymarket.com/activity"

DB = "seen.db"

WALLETS: dict = {}
for _pair in os.getenv("WALLETS", "").split(","):
    _pair = _pair.strip()
    if "=" in _pair:
        _label, _addr = _pair.split("=", 1)
        WALLETS[_addr.strip().lower()] = _label.strip()

# ── Category classifier ────────────────────────────────────────────────────────
# (slug_or_title_substring, category_name) — first match wins.
CATEGORY_RULES = [
    ("wnba",               "WNBA"),
    ("nba",                "NBA"),
    ("nfl",                "NFL"),
    ("mlb",                "MLB"),
    ("nhl",                "NHL"),
    ("fifwc",              "FIFA WC"),
    ("world-cup",          "FIFA WC"),
    ("world cup",          "FIFA WC"),
    ("epl",                "Soccer"),
    ("ucl",                "Soccer"),
    ("laliga",             "Soccer"),
    ("premier-league",     "Soccer"),
    ("premier league",     "Soccer"),
    ("champions-league",   "Soccer"),
    ("champions league",   "Soccer"),
    ("mls",                "Soccer"),
    ("serie-a",            "Soccer"),
    ("bundesliga",         "Soccer"),
    ("ligue-1",            "Soccer"),
    ("ufc",                "UFC/MMA"),
    ("mma",                "UFC/MMA"),
    ("bellator",           "UFC/MMA"),
    ("tennis",             "Tennis"),
    ("wimbledon",          "Tennis"),
    ("atp",                "Tennis"),
    ("wta",                "Tennis"),
    ("us-open",            "Tennis"),
    ("french-open",        "Tennis"),
    ("australian-open",    "Tennis"),
    ("formula-1",          "F1"),
    ("formula1",           "F1"),
    ("grand-prix",         "F1"),
    ("/f1-",               "F1"),
    ("pga",                "Golf"),
    (" golf",              "Golf"),
    ("masters",            "Golf"),
    ("ryder",              "Golf"),
    ("ncaab",              "NCAAB"),
    ("march-madness",      "NCAAB"),
    ("ncaaf",              "NCAAF"),
    ("college-football",   "NCAAF"),
    ("valorant",           "Esports"),
    ("csgo",               "Esports"),
    ("cs2",                "Esports"),
    ("league-of-legends",  "Esports"),
    ("dota",               "Esports"),
    ("boxing",             "Boxing"),
]

SPORTS_TITLE_HINTS = (
    " vs ", "vs.", " @ ", "winner", "champion", "score:",
    "playoff", "championship", "tournament", "series",
)


def classify_category(title: str, event_slug: str) -> str:
    text = f"{event_slug} {title}".lower()
    for kw, cat in CATEGORY_RULES:
        if kw in text:
            return cat
    return "Other"


def looks_sporty(category: str, title: str) -> bool:
    if not SPORTS_ONLY:
        return True
    if category != "Other":
        return True
    return any(h in title.lower() for h in SPORTS_TITLE_HINTS)


# ── Database ───────────────────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    # Existing dedup table — untouched so restarts don't re-alert old fills.
    con.execute("CREATE TABLE IF NOT EXISTS seen (trade_id TEXT PRIMARY KEY)")
    # Per-fill history used for all computed stats.
    con.execute("""
        CREATE TABLE IF NOT EXISTS fills (
            trade_id     TEXT PRIMARY KEY,
            wallet       TEXT NOT NULL,
            condition_id TEXT,
            event_slug   TEXT,
            title        TEXT,
            category     TEXT,
            side         TEXT,
            outcome      TEXT,
            price        REAL,
            shares       REAL,
            usd_size     REAL,
            ts           INTEGER
        )
    """)
    # Stub table for settlement data needed by arb score / CLV.
    # See arb_score() below for the full TODO.
    con.execute("""
        CREATE TABLE IF NOT EXISTS market_settle (
            condition_id     TEXT PRIMARY KEY,
            resolved_price   REAL,    -- 1.0 = YES won, 0.0 = NO won
            resolved_at      INTEGER, -- unix ts
            clob_close_price REAL     -- last traded price before close (for CLV)
        )
    """)
    con.commit()
    return con


def already_seen(con: sqlite3.Connection, trade_id: str) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM seen WHERE trade_id=?", (trade_id,)
    ).fetchone())


def mark_seen(con: sqlite3.Connection, trade_id: str) -> None:
    con.execute("INSERT OR IGNORE INTO seen (trade_id) VALUES (?)", (trade_id,))
    con.commit()


def store_fill(con: sqlite3.Connection, tr: dict, wallet: str) -> None:
    con.execute("""
        INSERT OR IGNORE INTO fills
          (trade_id, wallet, condition_id, event_slug, title, category,
           side, outcome, price, shares, usd_size, ts)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (tr["id"], wallet, tr["condition_id"], tr["event_slug"], tr["market"],
          tr["category"], tr["side"], tr["outcome"], tr["price"],
          tr["shares"], tr["usd"], tr["ts"]))
    con.commit()


# ── Per-wallet statistics ──────────────────────────────────────────────────────
def wallet_stats(con: sqlite3.Connection, wallet: str, days: int = None) -> dict:
    """All-time (days=None) or rolling-N-day size + price stats for a wallet."""
    q    = "SELECT usd_size, price FROM fills WHERE wallet=?"
    args = (wallet,)
    if days:
        cutoff = int(datetime.now(timezone.utc).timestamp()) - days * 86400
        q    += " AND ts>?"
        args  = (wallet, cutoff)
    rows = con.execute(q, args).fetchall()
    if not rows:
        return {"count": 0, "total_usd": 0.0, "mean_usd": 0.0,
                "median_usd": 0.0, "std_usd": 0.0}
    sizes = [r[0] for r in rows]
    return {
        "count":      len(sizes),
        "total_usd":  sum(sizes),
        "mean_usd":   statistics.mean(sizes),
        "median_usd": statistics.median(sizes),
        "std_usd":    statistics.stdev(sizes) if len(sizes) > 1 else 0.0,
    }


def category_stats(con: sqlite3.Connection, wallet: str, category: str) -> dict:
    """Fill count, total USDC placed, and price distribution for (wallet, category)."""
    rows = con.execute(
        "SELECT usd_size, price FROM fills WHERE wallet=? AND category=?",
        (wallet, category)
    ).fetchall()
    if not rows:
        return {"count": 0, "total_usd": 0.0, "mean_price": 0.0, "std_price": 0.0}
    sizes  = [r[0] for r in rows]
    prices = [r[1] for r in rows]
    return {
        "count":      len(sizes),
        "total_usd":  sum(sizes),
        "mean_price": statistics.mean(prices),
        "std_price":  statistics.stdev(prices) if len(prices) > 1 else 0.0,
    }


# ── Computed signals ───────────────────────────────────────────────────────────
def conviction_score(usd_size: float, stats: dict) -> int:
    """
    CONVICTION SCORE (0–100), wallet-normalized:
        ratio = usd_size / wallet_median_usd
        score = clamp(round(ratio / 3.0 * 100), 0, 100)

    At 3× median → 100 | 1× median → 33 | 0.5× → 17.
    Linear in ratio, capped at 3×.  Ratio-based so a $10 bettor and a
    $10 000 bettor yield the same score for the same relative sizing.
    Returns 50 when no history exists (neutral / unknown).
    """
    med = stats.get("median_usd", 0.0)
    if med <= 0:
        return 50
    return min(100, max(0, round((usd_size / med) / 3.0 * 100)))


def tail_stake_calc(conviction: int, base_unit: float) -> float:
    """
    TAIL STAKE — fractional-Kelly-style:
        frac  = conviction / 100          (maps 0–100 → 0.0–1.0)
        stake = MY_BASE_UNIT * frac

    MY_BASE_UNIT is your full-conviction (score=100) unit size.
    At 100 → tail 1 full base unit; at 50 → half; at 33 (1× wallet median) → a third.
    Scale MY_BASE_UNIT to whatever fraction of bankroll you consider one unit.
    """
    return base_unit * (conviction / 100.0)


def price_deviation(price: float, cat_st: dict) -> tuple:
    """
    Returns (delta_cents, z_score) of this entry vs the wallet's historical
    average entry price in this category.
        delta_cents = (price - mean_price) * 100
        z_score     = delta_cents / (std_price * 100)   [None if std=0 or n<2]
    """
    mean_p = cat_st.get("mean_price", 0.0)
    std_p  = cat_st.get("std_price",  0.0)
    delta  = (price - mean_p) * 100.0
    z      = (delta / (std_p * 100.0)) if std_p > 0.0 else None
    return delta, z


# ── Arb score (CLV-style) — STUB ───────────────────────────────────────────────
# Definition:
#   For each SETTLED fill where wallet bought YES at entry_price p:
#       CLV (cents) = (resolved_price − p) × 100
#   resolved_price = 1.0 if YES won, 0.0 if it lost.
#   wallet arb_score = rolling mean CLV across all settled fills.
#   Positive arb_score → wallet consistently buys underpriced shares
#   (gets better prices than where the market eventually lands).
#
# TODO: implement once settlement data is available.
#   Source needed : gamma-api.polymarket.com/markets/{conditionId}
#     Fields: `resolved` (bool), `resolvedPrice` (1.0 / 0.0), `closedTime` (unix ts)
#   That endpoint currently requires a Polymarket CLOB API key.
#   Fallback options:
#     • Poll polymarket.com/api/markets/{conditionId} periodically (no known auth).
#     • Read on-chain UMA resolution events from Polygon (fully trustless).
#   Implementation sketch:
#     1. Nightly job: for each conditionId in fills NOT yet in market_settle,
#        query resolution endpoint; if resolved, write to market_settle table.
#     2. arb_score(wallet) = AVG((resolved_price − price) × 100)
#        across fills JOIN market_settle ON condition_id, filtered to BUY-YES only.
def arb_score(_con: sqlite3.Connection, _wallet: str):
    return None   # returns None until settle data source is wired up


# ── Network ────────────────────────────────────────────────────────────────────
async def fetch_trades(session, addr: str, limit: int = 50) -> list:
    """
    Fetch recent activity for addr from data-api.polymarket.com/activity.
    Retries up to 3× with exponential back-off + jitter on 429 and transient
    errors.  At 6 wallets / 10 s the sustained rate is ~36 req/min — well
    below typical API thresholds — but back-off protects against bursts.
    """
    params = {"user": addr, "limit": limit}
    for attempt in range(3):
        try:
            async with session.get(
                DATA_API, params=params,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                if r.status == 429:
                    wait = 5 * (2 ** attempt) + random.uniform(0, 2)
                    print(f"[{addr[:8]}] 429 rate-limited — back-off {wait:.1f}s")
                    await asyncio.sleep(wait)
                    continue
                if r.status != 200:
                    print(f"[{addr[:8]}] HTTP {r.status}")
                    return []
                return await r.json()
        except Exception as exc:
            wait = 2 ** attempt + random.uniform(0, 1)
            print(f"[{addr[:8]}] fetch error ({exc}) — retry in {wait:.1f}s")
            if attempt < 2:
                await asyncio.sleep(wait)
    return []


# ── Trade parsing ──────────────────────────────────────────────────────────────
def parse_trade(t: dict) -> dict:
    """
    Parse a data-api.polymarket.com/activity row.

    Confirmed live fields (verified 2026-06-26 against swisstony feed):
        transactionHash  → unique trade ID   (original code used "id" — wrong)
        timestamp        → unix seconds      (original used "match_time" — missing)
        conditionId      → market unique ID
        title            → full market question
        eventSlug        → parent event slug (used for category classification)
        side             → "BUY" | "SELL"
        outcome          → "Yes" | "No"
        price            → price per share (0.0–1.0)
        size             → shares purchased
        usdcSize         → USDC actually spent — USE THIS for dollar amount;
                           price * size gives the wrong number because size is
                           fractional shares, not a round dollar value.
        outcomeIndex     → 0 or 1

    No settlement / resolution field in this endpoint (see arb_score TODO).
    """
    price   = float(t.get("price",    0))
    shares  = float(t.get("size",     0))
    usd     = float(t.get("usdcSize", 0)) or (price * shares)
    title   = t.get("title") or t.get("market") or t.get("slug", "?")
    eslug   = t.get("eventSlug") or ""
    side    = (t.get("side") or "").upper()
    outcome = t.get("outcome") or ""
    cat     = classify_category(title, eslug)
    return {
        "id":           str(t.get("transactionHash") or t.get("id") or t.get("trade_id") or ""),
        "market":       title,
        "condition_id": t.get("conditionId", ""),
        "event_slug":   eslug,
        "category":     cat,
        "side":         side,
        "outcome":      outcome,
        "price":        price,
        "shares":       shares,
        "usd":          usd,
        "ts":           t.get("timestamp"),
    }


# ── Discord alert ──────────────────────────────────────────────────────────────
async def post_discord(
    session,
    label:        str,
    addr:         str,
    tr:           dict,
    prior_stats:  dict,   # wallet all-time stats captured BEFORE this fill stored
    prior_30d:    dict,
    prior_cat:    dict,   # category stats captured BEFORE this fill stored
) -> None:
    conv  = conviction_score(tr["usd"], prior_stats)
    stake = tail_stake_calc(conv, MY_BASE_UNIT)
    delta_c, z_price = price_deviation(tr["price"], prior_cat)

    icon = "🟢" if tr["side"] == "BUY" else "🔴"
    when = tr["ts"]
    try:
        when = datetime.fromtimestamp(int(when), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        pass

    conv_bar = "█" * (conv // 10) + "░" * (10 - conv // 10)

    if prior_cat["count"] == 0:
        price_dev_str = "n/a (first fill in category)"
    elif z_price is not None:
        price_dev_str = f"{delta_c:+.1f}¢  z={z_price:+.2f}"
    else:
        price_dev_str = f"{delta_c:+.1f}¢  (n=1, no σ yet)"

    # Tail stake math shown explicitly in the embed
    stake_str = (
        f"${MY_BASE_UNIT:.0f} base × {conv/100:.2f} "
        f"= **${stake:.2f}**"
    )

    cat_hist = (
        f"{prior_cat['count']} fills · ${prior_cat['total_usd']:,.0f} placed"
        if prior_cat["count"] > 0 else "first fill in category"
    )
    # Realized P/L per category is stubbed — needs settle data (see arb_score TODO).
    cat_hist += "\nRealized P/L: pending settle data"

    arb = arb_score(None, addr)
    arb_str = f"{arb:.2f} avg CLV¢" if arb is not None else "pending settle data"

    embed = {
        "title": f"{icon} {label} — {tr['category']}",
        "color": 0x2ECC71 if tr["side"] == "BUY" else 0xE74C3C,
        "fields": [
            {"name": "Market",
             "value": str(tr["market"])[:240], "inline": False},
            {"name": "Side / Outcome",
             "value": f"{tr['side']} {tr['outcome']}", "inline": True},
            {"name": "Entry",
             "value": f"{tr['price']*100:.1f}¢", "inline": True},
            {"name": "Stake",
             "value": f"${tr['usd']:,.2f}", "inline": True},
            {"name": f"Conviction  [{conv_bar}]",
             "value": f"{conv}/100", "inline": True},
            {"name": "Tail stake",
             "value": stake_str, "inline": True},
            {"name": "Price vs my norm",
             "value": price_dev_str, "inline": True},
            {"name": f"{tr['category']} history",
             "value": cat_hist, "inline": False},
            {"name": "30d activity",
             "value": f"${prior_30d['total_usd']:,.0f} · {prior_30d['count']} fills",
             "inline": True},
            {"name": "Arb score",
             "value": arb_str, "inline": True},
        ],
        "footer": {"text": (
            f"{addr[:10]}…  •  {when}  •  "
            f"median unit ${prior_stats['median_usd']:.0f}  •  "
            f"all-time ${prior_stats['total_usd']:,.0f}"
        )},
    }
    payload = {"embeds": [embed]}
    try:
        async with session.post(
            DISCORD_WEBHOOK, json=payload,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            if r.status not in (200, 204):
                print(f"discord post failed HTTP {r.status}: {await r.text()}")
    except Exception as exc:
        print(f"discord error: {exc}")


# ── Poll loop ──────────────────────────────────────────────────────────────────
async def poll_once(session, con: sqlite3.Connection) -> None:
    for addr, label in WALLETS.items():
        rows = await fetch_trades(session, addr)
        for raw in rows:
            tr = parse_trade(raw)
            if not tr["id"] or tr["id"] == "None":
                continue

            if already_seen(con, tr["id"]):
                # Idempotent: ensure fill is in history even if seen before
                store_fill(con, tr, addr)
                continue

            # Capture prior history BEFORE storing so stats exclude this fill.
            # (Avoids off-by-one in conviction/z-score for small-history wallets.)
            prior_stats = wallet_stats(con, addr)
            prior_30d   = wallet_stats(con, addr, days=30)
            prior_cat   = category_stats(con, addr, tr["category"])

            store_fill(con, tr, addr)
            mark_seen(con, tr["id"])

            if tr["usd"] < MIN_ALERT_USD:
                continue
            if not looks_sporty(tr["category"], tr["market"]):
                continue

            print(f"ALERT {label} | {tr['category']} | {tr['market'][:60]} | ${tr['usd']:.0f}")
            await post_discord(session, label, addr, tr,
                               prior_stats, prior_30d, prior_cat)

        # Jitter between wallet polls to avoid bursting all 6 requests at once
        await asyncio.sleep(random.uniform(0.3, 1.5))


# ── Main ───────────────────────────────────────────────────────────────────────
async def main() -> None:
    if not WALLETS:
        raise SystemExit("No WALLETS configured in .env")
    con = init_db()
    print(
        f"Tracking {len(WALLETS)} wallets · poll={POLL_SECONDS}s · "
        f"min=${MIN_ALERT_USD:.0f} · sports_only={SPORTS_ONLY} · "
        f"bankroll=${MY_BANKROLL:.0f} · base_unit=${MY_BASE_UNIT:.0f}"
    )

    async with aiohttp.ClientSession() as session:
        # Seed pass: fetch last 500 fills per wallet, store history, mark all seen.
        # No alerts fire on startup — only fills that arrive AFTER this point alert.
        print("Seeding historical fills (builds baseline stats)…")
        for addr, label in WALLETS.items():
            rows = await fetch_trades(session, addr, limit=500)
            for raw in rows:
                tr = parse_trade(raw)
                if tr["id"]:
                    store_fill(con, tr, addr)
                    mark_seen(con, tr["id"])
            print(f"  {label}: {len(rows)} fills seeded")
            await asyncio.sleep(random.uniform(0.5, 1.5))
        print("Seeded. Watching for new fills…\n")

        while True:
            await poll_once(session, con)
            await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
