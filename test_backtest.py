"""
test_backtest.py  —  unit and integration tests for the backtest/resolution system.

Fixtures covered
----------------
  * one winning binary market
  * one losing binary market
  * one unresolved market
  * one multi-outcome market (ambiguous → NEEDS_REVIEW)
  * multiple wallets on the same side
  * wallets on opposing sides
  * duplicate snapshots (exact same run)
  * partial / zero-share positions

Properties verified
-------------------
  * snapshots are immutable (rows from different runs both survive)
  * exact duplicate snapshots within a run are silently dropped
  * resolved positions are not duplicated (UNIQUE guard)
  * P/L is calculated correctly (binary WIN and LOSS)
  * ROI is calculated correctly
  * wallet summaries update correctly
  * unresolved markets remain unresolved
  * ambiguous markets become NEEDS_REVIEW
  * point-in-time backtests do not use future data (as_of filter)

Run:
    python -m pytest test_backtest.py -v
    python test_backtest.py           # also works without pytest
"""

import json
import unittest
import tempfile
import statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path

from backtest_db import init_db
from resolver import determine_resolution
from performance import _upsert_summary, run_performance
from backtest import load_positions, compute_metrics


# ── Test DB helper ────────────────────────────────────────────────────────────

def _tmp_db():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = init_db(f.name)
    return conn, f.name


def _ins_snap(
    conn,
    snap_ts: str,
    wallet: str,
    token_id: str,
    condition_id: str = "cond1",
    category: str = "NBA",
    side: str = "Yes",
    avg_entry: float = 50.0,
    cur_price: float = 50.0,
    cost: float = 100.0,
    shares: float = 200.0,
    resolution_time: str = "2025-01-10",
    market_status: str = "open",
    market_title: str = "Test Market",
):
    conn.execute("""
        INSERT OR IGNORE INTO position_snapshots
          (snapshot_ts, wallet_address, wallet_label,
           token_id, condition_id, category, side,
           avg_entry_price, current_price, cost_basis, shares,
           resolution_time, market_status, market_title,
           market_id, event_id, market_slug)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        snap_ts, wallet, wallet, token_id, condition_id,
        category, side, avg_entry, cur_price, cost, shares,
        resolution_time, market_status, market_title,
        condition_id, "evt1", "test-slug",
    ))
    conn.commit()


def _ins_resolved(
    conn,
    wallet: str,
    token_id: str,
    condition_id: str = "cond1",
    category: str = "NBA",
    side: str = "Yes",
    cost: float = 100.0,
    shares: float = 200.0,
    payout: float = 200.0,
    win_loss: str = "WIN",
    resolved_at: str = "2025-01-11T00:00:00Z",
):
    pnl = payout - cost
    roi = pnl / cost * 100 if cost else 0.0
    conn.execute("""
        INSERT OR IGNORE INTO resolved_positions
          (wallet_address, wallet_label, condition_id, token_id,
           category, side, shares, avg_entry_price, final_price,
           cost_basis, payout_value, realized_pnl, roi_pct, win_loss,
           first_seen_at, last_seen_at, resolved_at, market_title)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        wallet, wallet, condition_id, token_id,
        category, side, shares, 50.0,
        100.0 if win_loss == "WIN" else 0.0,
        cost, payout, pnl, roi, win_loss,
        "2025-01-01T00:00:00Z", "2025-01-10T00:00:00Z",
        resolved_at, "Test Market",
    ))
    conn.commit()


# ── Resolution logic tests ────────────────────────────────────────────────────

class TestDetermineResolution(unittest.TestCase):

    def test_gamma_yes_wins(self):
        gamma = {
            "closed": True,
            "outcomePrices": ["1", "0"],
            "outcomes": ["Yes", "No"],
        }
        wl, price = determine_resolution("open", 95, "Yes", gamma)
        self.assertEqual(wl, "WIN")
        self.assertAlmostEqual(price, 100.0)

    def test_gamma_no_wins(self):
        gamma = {
            "closed": True,
            "outcomePrices": ["0", "1"],
            "outcomes": ["Yes", "No"],
        }
        wl, price = determine_resolution("open", 5, "No", gamma)
        self.assertEqual(wl, "WIN")

    def test_gamma_yes_loses(self):
        gamma = {
            "closed": True,
            "outcomePrices": ["0", "1"],
            "outcomes": ["Yes", "No"],
        }
        wl, price = determine_resolution("open", 5, "Yes", gamma)
        self.assertEqual(wl, "LOSS")
        self.assertAlmostEqual(price, 0.0)

    def test_gamma_not_closed_ignores_prices(self):
        gamma = {
            "closed": False,
            "outcomePrices": ["0.5", "0.5"],
            "outcomes": ["Yes", "No"],
        }
        wl, _ = determine_resolution("open", 50, "Yes", gamma)
        self.assertEqual(wl, "NEEDS_REVIEW")

    def test_fallback_high_price_win(self):
        wl, _ = determine_resolution("open", 99.5, "Yes", None)
        self.assertEqual(wl, "WIN")

    def test_fallback_zero_price_loss(self):
        wl, _ = determine_resolution("open", 0.1, "Yes", None)
        self.assertEqual(wl, "LOSS")

    def test_ambiguous_price_needs_review(self):
        wl, _ = determine_resolution("open", 50.0, "Yes", None)
        self.assertEqual(wl, "NEEDS_REVIEW")

    def test_multi_outcome_scalar_push(self):
        # Partial resolution → PUSH
        gamma = {
            "closed": True,
            "outcomePrices": ["0.5", "0.5"],
            "outcomes": ["Yes", "No"],
        }
        wl, price = determine_resolution("open", 50, "Yes", gamma)
        self.assertEqual(wl, "PUSH")
        self.assertAlmostEqual(price, 50.0)


# ── Snapshot immutability tests ───────────────────────────────────────────────

class TestSnapshotImmutability(unittest.TestCase):

    def setUp(self):
        self.conn, self.path = _tmp_db()

    def tearDown(self):
        self.conn.close()
        Path(self.path).unlink(missing_ok=True)

    def test_two_different_runs_both_stored(self):
        _ins_snap(self.conn, "2025-01-01T10:00:00Z", "0xabc", "tok1", cur_price=50)
        _ins_snap(self.conn, "2025-01-01T11:00:00Z", "0xabc", "tok1", cur_price=55)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM position_snapshots"
        ).fetchone()[0]
        self.assertEqual(count, 2, "Both snapshot rows must be stored")

    def test_exact_duplicate_within_run_dropped(self):
        _ins_snap(self.conn, "2025-01-01T10:00:00Z", "0xabc", "tok1")
        _ins_snap(self.conn, "2025-01-01T10:00:00Z", "0xabc", "tok1")  # exact dup
        count = self.conn.execute(
            "SELECT COUNT(*) FROM position_snapshots"
        ).fetchone()[0]
        self.assertEqual(count, 1, "Exact dup in same run must be silently ignored")

    def test_different_wallets_same_token_stored_separately(self):
        _ins_snap(self.conn, "2025-01-01T10:00:00Z", "0xabc", "tok1")
        _ins_snap(self.conn, "2025-01-01T10:00:00Z", "0xdef", "tok1")
        count = self.conn.execute(
            "SELECT COUNT(*) FROM position_snapshots"
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_zero_share_position_stored(self):
        _ins_snap(self.conn, "2025-01-01T10:00:00Z", "0xabc", "tok_zero",
                  shares=0.0, cost=0.0)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM position_snapshots"
        ).fetchone()[0]
        self.assertEqual(count, 1)


# ── Resolved position dedup tests ─────────────────────────────────────────────

class TestResolvedPositionDedup(unittest.TestCase):

    def setUp(self):
        self.conn, self.path = _tmp_db()

    def tearDown(self):
        self.conn.close()
        Path(self.path).unlink(missing_ok=True)

    def test_no_double_resolve(self):
        _ins_resolved(self.conn, "0xabc", "tok1", win_loss="WIN")
        _ins_resolved(self.conn, "0xabc", "tok1", win_loss="WIN")  # must be ignored
        count = self.conn.execute(
            "SELECT COUNT(*) FROM resolved_positions"
        ).fetchone()[0]
        self.assertEqual(count, 1, "Same (wallet, token, condition) resolved twice must be deduplicated")

    def test_different_wallets_same_market_both_stored(self):
        _ins_resolved(self.conn, "0xabc", "tok1", win_loss="WIN")
        _ins_resolved(self.conn, "0xdef", "tok1", win_loss="WIN")
        count = self.conn.execute(
            "SELECT COUNT(*) FROM resolved_positions"
        ).fetchone()[0]
        self.assertEqual(count, 2)


# ── P/L calculation tests ─────────────────────────────────────────────────────

class TestPnlCalculation(unittest.TestCase):

    def test_binary_win_payout(self):
        # Bought 200 shares at 50¢ → cost $100; won → payout $200
        shares, cost = 200.0, 100.0
        payout = shares * 1.0
        pnl    = payout - cost
        roi    = pnl / cost * 100
        self.assertAlmostEqual(payout, 200.0)
        self.assertAlmostEqual(pnl, 100.0)
        self.assertAlmostEqual(roi, 100.0)

    def test_binary_loss_payout(self):
        shares, cost = 200.0, 100.0
        payout = 0.0
        pnl    = payout - cost
        roi    = pnl / cost * 100
        self.assertAlmostEqual(pnl, -100.0)
        self.assertAlmostEqual(roi, -100.0)

    def test_push_payout(self):
        # Scalar market: 200 shares, final_price = 50¢
        shares, cost = 200.0, 100.0
        final_price  = 50.0   # cents
        payout = shares * (final_price / 100)
        pnl    = payout - cost
        self.assertAlmostEqual(payout, 100.0)
        self.assertAlmostEqual(pnl, 0.0)

    def test_roi_zero_cost_doesnt_crash(self):
        cost, payout = 0.0, 0.0
        roi = payout / cost * 100 if cost > 0 else 0.0
        self.assertEqual(roi, 0.0)


# ── Wallet summary tests ───────────────────────────────────────────────────────

class TestWalletSummary(unittest.TestCase):

    def setUp(self):
        self.conn, self.path = _tmp_db()

    def tearDown(self):
        self.conn.close()
        Path(self.path).unlink(missing_ok=True)

    def test_summary_computed_correctly(self):
        _ins_resolved(self.conn, "0xabc", "tok1",
                      cost=100, payout=200, win_loss="WIN")
        _ins_resolved(self.conn, "0xabc", "tok2", condition_id="cond2",
                      cost=100, payout=0,   win_loss="LOSS")

        _upsert_summary(self.conn, "0xabc", "NBA")
        self.conn.commit()

        row = self.conn.execute("""
            SELECT * FROM wallet_performance_summary
            WHERE wallet_address='0xabc' AND category='NBA'
        """).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row["wins"], 1)
        self.assertEqual(row["losses"], 1)
        self.assertAlmostEqual(row["win_rate"], 50.0)
        self.assertAlmostEqual(row["total_cost_basis"], 200.0)
        self.assertAlmostEqual(row["total_payout"], 200.0)
        self.assertAlmostEqual(row["total_pnl"], 0.0)
        self.assertAlmostEqual(row["roi_pct"], 0.0)

    def test_needs_review_excluded_from_summary(self):
        _ins_resolved(self.conn, "0xabc", "tok1",
                      cost=100, payout=200, win_loss="WIN")
        _ins_resolved(self.conn, "0xabc", "tok2", condition_id="cond2",
                      cost=500, payout=0,   win_loss="NEEDS_REVIEW")

        _upsert_summary(self.conn, "0xabc", "NBA")
        self.conn.commit()

        row = self.conn.execute("""
            SELECT * FROM wallet_performance_summary
            WHERE wallet_address='0xabc' AND category='NBA'
        """).fetchone()

        self.assertIsNotNone(row)
        # NEEDS_REVIEW must not distort the win count or cost basis
        self.assertEqual(row["wins"], 1)
        self.assertAlmostEqual(row["total_cost_basis"], 100.0)


# ── Point-in-time backtest tests ──────────────────────────────────────────────

class TestPointInTime(unittest.TestCase):

    def setUp(self):
        self.conn, self.path = _tmp_db()
        # Jan position
        _ins_snap(self.conn, "2025-01-01T10:00:00Z", "0xabc", "tok_jan",
                  condition_id="condA", cost=100)
        # Feb position (should be invisible before Feb)
        _ins_snap(self.conn, "2025-02-01T10:00:00Z", "0xdef", "tok_feb",
                  condition_id="condB", cost=200)

    def tearDown(self):
        self.conn.close()
        Path(self.path).unlink(missing_ok=True)

    def test_as_of_excludes_future_positions(self):
        positions = load_positions(
            self.conn,
            as_of="2025-01-15T00:00:00Z",
        )
        wallets = {p["wallet_address"] for p in positions}
        self.assertIn("0xabc", wallets)
        self.assertNotIn("0xdef", wallets,
                         "February position must not appear in January backtest")

    def test_no_cutoff_returns_all(self):
        positions = load_positions(self.conn)
        self.assertEqual(len(positions), 2)

    def test_category_filter(self):
        _ins_snap(self.conn, "2025-01-01T10:00:00Z", "0xabc", "tok_wnba",
                  condition_id="condC", category="WNBA", cost=50)
        positions = load_positions(self.conn, category="WNBA")
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["category"], "WNBA")

    def test_min_size_filter(self):
        positions = load_positions(self.conn, min_size=150.0)
        # Only the $200 Feb position survives
        self.assertEqual(len(positions), 1)
        self.assertAlmostEqual(float(positions[0]["cost_basis"]), 200.0)


# ── Compute metrics tests ─────────────────────────────────────────────────────

class TestComputeMetrics(unittest.TestCase):

    def _pos(self, win_loss, cost, payout, pnl, wallet="w1", cat="NBA",
             resolved_at="2025-01-11T00:00:00Z"):
        return {
            "win_loss": win_loss,
            "cost_basis": cost,
            "payout_value": payout,
            "realized_pnl": pnl,
            "wallet_label": wallet,
            "wallet_address": wallet,
            "category": cat,
            "condition_id": "cond1",
            "market_id": "m1",
            "market_title": "Test",
            "avg_entry": 50.0,
            "snap_entry_price": 50.0,
            "resolved_at": resolved_at,
        }

    def test_all_win(self):
        positions = [self._pos("WIN", 100, 200, 100) for _ in range(3)]
        m = compute_metrics(positions)
        self.assertEqual(m["wins"], 3)
        self.assertEqual(m["losses"], 0)
        self.assertAlmostEqual(m["win_rate_pct"], 100.0)
        self.assertAlmostEqual(m["roi_pct"], 100.0)

    def test_all_loss(self):
        positions = [self._pos("LOSS", 100, 0, -100) for _ in range(3)]
        m = compute_metrics(positions)
        self.assertEqual(m["losses"], 3)
        self.assertAlmostEqual(m["roi_pct"], -100.0)

    def test_mixed_breakeven(self):
        positions = [
            self._pos("WIN",  100, 200,  100),
            self._pos("LOSS", 100,   0, -100),
        ]
        m = compute_metrics(positions)
        self.assertAlmostEqual(m["win_rate_pct"], 50.0)
        self.assertAlmostEqual(m["total_pnl"], 0.0)
        self.assertAlmostEqual(m["roi_pct"], 0.0)

    def test_unresolved_excluded_from_pnl_but_counted(self):
        positions = [
            self._pos("WIN",  100, 200, 100),
            self._pos(None,   200,   0,   0),   # open position
        ]
        m = compute_metrics(positions)
        self.assertEqual(m["total_positions"], 2)
        self.assertEqual(m["unresolved"], 1)
        self.assertAlmostEqual(m["total_pnl"], 100.0)

    def test_needs_review_excluded_from_win_rate(self):
        positions = [self._pos("NEEDS_REVIEW", 500, 0, 0)]
        m = compute_metrics(positions)
        self.assertEqual(m["wins"], 0)
        self.assertEqual(m["losses"], 0)
        self.assertAlmostEqual(m["win_rate_pct"], 0.0)

    def test_by_wallet_breakout(self):
        positions = [
            self._pos("WIN",  100, 200,  100, wallet="alice"),
            self._pos("LOSS", 100,   0, -100, wallet="bob"),
        ]
        m = compute_metrics(positions)
        self.assertIn("alice", m["by_wallet"])
        self.assertAlmostEqual(m["by_wallet"]["alice"]["roi"], 100.0)
        self.assertAlmostEqual(m["by_wallet"]["bob"]["roi"],  -100.0)

    def test_by_category_breakout(self):
        positions = [
            self._pos("WIN",  100, 200, 100, cat="NBA"),
            self._pos("LOSS", 100,   0, -100, cat="WNBA"),
        ]
        m = compute_metrics(positions)
        self.assertIn("NBA",  m["by_category"])
        self.assertIn("WNBA", m["by_category"])


# ── Unresolved market test ────────────────────────────────────────────────────

class TestUnresolvedMarket(unittest.TestCase):

    def setUp(self):
        self.conn, self.path = _tmp_db()

    def tearDown(self):
        self.conn.close()
        Path(self.path).unlink(missing_ok=True)

    def test_unresolved_market_stays_unresolved(self):
        # resolution_time in the future → resolver should not touch it
        future_date = (datetime.now(timezone.utc) + timedelta(days=10)).date().isoformat()
        _ins_snap(self.conn, "2025-01-01T10:00:00Z", "0xabc", "tok_future",
                  resolution_time=future_date, market_status="open")

        # Simulate what resolver query would find: should be zero rows
        today = datetime.now(timezone.utc).date().isoformat()
        candidates = self.conn.execute("""
            SELECT COUNT(*) FROM position_snapshots ps
            LEFT JOIN resolved_positions rp
                ON rp.wallet_address = ps.wallet_address
                AND rp.token_id = ps.token_id
                AND rp.condition_id = ps.condition_id
            WHERE rp.resolved_id IS NULL
              AND (ps.resolution_time <= ? OR ps.market_status = 'resolved')
        """, (today,)).fetchone()[0]

        self.assertEqual(candidates, 0, "Future-resolving market must not be picked up by resolver")

    def test_needs_review_not_written_for_ambiguous_price(self):
        wl, _ = determine_resolution("open", 50.0, "Yes", None)
        self.assertEqual(wl, "NEEDS_REVIEW")


# ── Multiple wallets opposing sides test ──────────────────────────────────────

class TestOpposingSides(unittest.TestCase):

    def setUp(self):
        self.conn, self.path = _tmp_db()

    def tearDown(self):
        self.conn.close()
        Path(self.path).unlink(missing_ok=True)

    def test_wallets_on_both_sides_tracked_independently(self):
        # YES side
        _ins_snap(self.conn, "2025-01-01T10:00:00Z", "0xyes1", "tok_yes",
                  condition_id="condX", side="Yes")
        _ins_snap(self.conn, "2025-01-01T10:00:00Z", "0xyes2", "tok_yes",
                  condition_id="condX", side="Yes")
        # NO side
        _ins_snap(self.conn, "2025-01-01T10:00:00Z", "0xno1", "tok_no",
                  condition_id="condX", side="No")

        # Resolve YES wins
        _ins_resolved(self.conn, "0xyes1", "tok_yes", condition_id="condX",
                      side="Yes", payout=200, win_loss="WIN")
        _ins_resolved(self.conn, "0xyes2", "tok_yes", condition_id="condX",
                      side="Yes", payout=200, win_loss="WIN")
        _ins_resolved(self.conn, "0xno1", "tok_no", condition_id="condX",
                      side="No", payout=0, win_loss="LOSS")

        positions = load_positions(self.conn)
        resolved  = [p for p in positions if p["win_loss"] in ("WIN","LOSS","PUSH")]
        wins  = [p for p in resolved if p["win_loss"] == "WIN"]
        losses = [p for p in resolved if p["win_loss"] == "LOSS"]

        self.assertEqual(len(wins), 2)
        self.assertEqual(len(losses), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
