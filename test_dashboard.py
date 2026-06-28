import unittest

from dashboard import (
    MY_BASE_UNIT,
    MANUAL_WALLETS,
    SHARP_WALLET_META,
    attach_analytics_stats,
    attach_category_aggregates,
    attach_conviction_scores,
    attach_sharp_wallet_counts,
    american_odds_from_cents,
    build_position_row,
    collapse_position_cards,
    getPositionWalletDetails,
    attach_position_details,
    attach_selected_wallet_stats,
    odds_in_range,
)


def sample_row(wallet, addr, category, market, cost, current_value, cash_pnl, outcome="Yes"):
    return build_position_row(
        addr=addr,
        label=wallet,
        category=category,
        median=100,
        port_total=1000,
        position={
            "conditionId": market,
            "title": market,
            "outcome": outcome,
            "avgPrice": 0.50,
            "curPrice": 0.60,
            "initialValue": cost,
            "currentValue": current_value,
            "cashPnl": cash_pnl,
            "endDate": "2026-06-26T00:00:00Z",
            "redeemable": False,
        },
    )


class CategoryAggregationTest(unittest.TestCase):
    def test_wallet_category_metrics_repeat_on_each_matching_row(self):
        rows = [
            sample_row("swisstony", "0xswiss", "FIFA WC", "World Cup position A", 100, 110, 10),
            sample_row("swisstony", "0xswiss", "FIFA WC", "World Cup position B", 200, 170, -30),
            sample_row("swisstony", "0xswiss", "FIFA WC", "World Cup position C", 50, 75, 25),
            sample_row("swisstony", "0xswiss", "NBA", "NBA position A", 80, 88, 8),
            sample_row("other", "0xother", "FIFA WC", "Other World Cup A", 100, 70, -30),
            sample_row("other", "0xother", "FIFA WC", "Other World Cup B", 50, 55, 5),
        ]

        attach_category_aggregates(rows, rows)

        swisstony_wc = [
            r for r in rows
            if r["wallet"] == "swisstony" and r["category"] == "FIFA WC"
        ]
        self.assertEqual(len(swisstony_wc), 3)
        self.assertEqual({r["category_total_pl"] for r in swisstony_wc}, {5.0})
        self.assertEqual({r["category_roi_pct"] for r in swisstony_wc}, {1.43})
        self.assertEqual({r["category_total_cost"] for r in swisstony_wc}, {350.0})

        swisstony_nba = next(
            r for r in rows
            if r["wallet"] == "swisstony" and r["category"] == "NBA"
        )
        self.assertEqual(swisstony_nba["category_total_pl"], 8.0)
        self.assertEqual(swisstony_nba["category_roi_pct"], 10.0)

        other_wc = [
            r for r in rows
            if r["wallet"] == "other" and r["category"] == "FIFA WC"
        ]
        self.assertEqual({r["category_total_pl"] for r in other_wc}, {-25.0})
        self.assertEqual({r["category_roi_pct"] for r in other_wc}, {-16.67})

    def test_analytics_win_rate_overlays_by_wallet_and_category(self):
        rows = [
            sample_row("swisstony", "0xswiss", "FIFA WC", "World Cup position A", 100, 110, 10),
            sample_row("swisstony", "0xswiss", "FIFA WC", "World Cup position B", 200, 170, -30),
            sample_row("other", "0xother", "FIFA WC", "Other World Cup A", 100, 70, -30),
        ]
        attach_category_aggregates(rows, rows)
        attach_analytics_stats(rows, {
            ("0xswiss", "FIFA WC"): {
                "category_analytics_tag": "FIFA World Cup",
                "category_analytics_source": "polymarketanalytics",
                "category_markets": 248,
                "category_win_count": 140,
                "category_loss_count": 108,
                "category_win_rate_pct": 56.45,
                "category_total_positions": 5341,
                "category_active_positions": 874,
            },
            ("0xother", "FIFA WC"): {
                "category_analytics_tag": "FIFA World Cup",
                "category_analytics_source": "polymarketanalytics",
                "category_markets": 10,
                "category_win_count": 4,
                "category_loss_count": 6,
                "category_win_rate_pct": 40.0,
                "category_total_positions": 12,
                "category_active_positions": 1,
            },
        })

        swisstony_wc = [
            r for r in rows
            if r["wallet"] == "swisstony" and r["category"] == "FIFA WC"
        ]
        self.assertEqual({r["category_win_rate_pct"] for r in swisstony_wc}, {56.45})
        self.assertEqual({r["category_wins_losses"] for r in swisstony_wc}, {"140-108"})
        self.assertEqual({r["category_markets"] for r in swisstony_wc}, {248})

        other_wc = next(r for r in rows if r["wallet"] == "other")
        self.assertEqual(other_wc["category_win_rate_pct"], 40.0)
        self.assertEqual(other_wc["category_wins_losses"], "4-6")

    def test_american_odds_conversion(self):
        self.assertEqual(american_odds_from_cents(36.5), "+174")
        self.assertEqual(american_odds_from_cents(80.5), "-413")
        self.assertEqual(american_odds_from_cents(50), "-100")
        self.assertTrue(odds_in_range(36.5))
        self.assertTrue(odds_in_range(71.4))
        self.assertFalse(odds_in_range(80.5))
        self.assertFalse(odds_in_range(20.0))

    def test_tail_stake_tracks_final_conviction(self):
        rows = [
            sample_row("swisstony", "0xswiss", "FIFA WC", "World Cup position A", 100, 110, 10),
            sample_row("swisstony", "0xswiss", "FIFA WC", "World Cup position B", 200, 170, -30),
        ]
        attach_category_aggregates(rows, rows)
        attach_analytics_stats(rows, {
            ("0xswiss", "FIFA WC"): {
                "category_analytics_tag": "FIFA World Cup",
                "category_analytics_source": "polymarketanalytics",
                "category_markets": 248,
                "category_win_count": 140,
                "category_loss_count": 108,
                "category_win_rate_pct": 56.45,
                "category_total_positions": 5341,
                "category_active_positions": 874,
            },
        })
        attach_conviction_scores(rows, rows)

        for row in rows:
            self.assertIn("size_score", row)
            self.assertIn("skill_score", row)
            self.assertIn("sharp_consensus_score", row)
            self.assertEqual(
                row["tail_stake"],
                round(MY_BASE_UNIT * row["conviction"] / 100, 2),
            )

    def test_sharp_wallet_count_groups_same_outcome(self):
        original_manual = set(MANUAL_WALLETS)
        MANUAL_WALLETS.update({"0xswiss", "0xother", "0xthird"})
        rows = [
            sample_row("swisstony", "0xswiss", "FIFA WC", "World Cup position A", 100, 110, 10),
            sample_row("other", "0xother", "FIFA WC", "World Cup position A", 80, 90, 10),
            sample_row("third", "0xthird", "FIFA WC", "World Cup position B", 80, 90, 10),
        ]
        try:
            attach_sharp_wallet_counts(rows, rows)

            shared = [r for r in rows if r["row_id"] == "World Cup position A"]
            self.assertEqual({r["sharp_wallet_count"] for r in shared}, {2})
            self.assertTrue(all("swisstony" in r["sharp_wallets"] for r in shared))
            self.assertEqual(rows[2]["sharp_wallet_count"], 1)
        finally:
            MANUAL_WALLETS.clear()
            MANUAL_WALLETS.update(original_manual)

    def test_position_detail_aggregates_aligned_and_opposing_wallets(self):
        rows = [
            sample_row("swisstony", "0xswiss", "FIFA WC", "World Cup Final", 100, 120, 20, "Yes"),
            sample_row("swisstony", "0xswiss", "FIFA WC", "World Cup Final", 50, 60, 10, "Yes"),
            sample_row("sharpA", "0xaaa", "FIFA WC", "World Cup Final", 200, 230, 30, "Yes"),
            sample_row("sharpB", "0xbbb", "FIFA WC", "World Cup Final", 150, 120, -30, "Yes"),
            sample_row("sharpC", "0xccc", "FIFA WC", "World Cup Final", 90, 100, 10, "No"),
        ]
        attach_category_aggregates(rows, rows)
        attach_analytics_stats(rows, {
            ("0xswiss", "FIFA WC"): {
                "category_analytics_tag": "FIFA World Cup",
                "category_analytics_source": "polymarketanalytics",
                "category_markets": 248,
                "category_win_count": 140,
                "category_loss_count": 108,
                "category_win_rate_pct": 56.45,
                "category_total_positions": 5341,
                "category_active_positions": 874,
            }
        })
        attach_sharp_wallet_counts(rows, rows)
        attach_conviction_scores(rows, rows)

        details = getPositionWalletDetails(rows, "World Cup Final", "Yes", category="FIFA WC")
        swisstony = next(w for w in details["aligned_wallets"] if w["wallet"] == "swisstony")

        self.assertEqual(details["summary"]["aligned_wallet_count"], 3)
        self.assertEqual(details["summary"]["opposing_wallet_count"], 1)
        self.assertEqual(swisstony["duplicate_fill_count"], 2)
        self.assertEqual(swisstony["cost_basis"], 150.0)
        self.assertEqual(swisstony["current_value"], 180.0)
        self.assertIn("portfolio_size", swisstony)
        self.assertEqual(swisstony["portfolio_pct"], 18.0)
        self.assertEqual(swisstony["wallet_avg_position_size"], 75.0)
        self.assertEqual(swisstony["position_size_multiple"], 2.0)
        self.assertIn("wallet_conviction_contribution", swisstony)

        visible = [rows[0].copy()]
        attach_position_details(visible, rows)
        self.assertEqual(
            visible[0]["conviction"],
            visible[0]["position_details"]["summary"]["final_conviction_score"],
        )

    def test_position_cards_collapse_duplicate_market_side_rows(self):
        rows = [
            sample_row("swisstony", "0xswiss", "FIFA WC", "World Cup Final", 100, 120, 20, "Yes"),
            sample_row("swisstony", "0xswiss", "FIFA WC", "World Cup Final", 50, 60, 10, "Yes"),
            sample_row("sharpA", "0xaaa", "FIFA WC", "World Cup Final", 200, 230, 30, "Yes"),
            sample_row("sharpB", "0xbbb", "FIFA WC", "World Cup Final", 150, 120, -30, "Yes"),
            sample_row("sharpC", "0xccc", "FIFA WC", "World Cup Final", 90, 100, 10, "No"),
        ]
        attach_category_aggregates(rows, rows)
        attach_sharp_wallet_counts(rows, rows)
        attach_conviction_scores(rows, rows)
        attach_position_details(rows, rows)

        cards = collapse_position_cards(rows)
        yes_card = next(card for card in cards if card["outcome"] == "Yes")
        no_card = next(card for card in cards if card["outcome"] == "No")

        self.assertEqual(len(cards), 2)
        self.assertTrue(yes_card["position_card"])
        self.assertEqual(yes_card["wallet"], "3 aligned")
        self.assertEqual(yes_card["aligned_sharp_wallet_count"], 3)
        self.assertEqual(yes_card["opposing_sharp_wallet_count"], 1)
        self.assertEqual(yes_card["row_pl"], 30.0)
        self.assertEqual(yes_card["size_usd"], 500.0)
        self.assertEqual(yes_card["wallets"], ["sharpA", "sharpB", "swisstony"])
        swisstony = next(
            wallet for wallet in yes_card["position_details"]["aligned_wallets"]
            if wallet["wallet"] == "swisstony"
        )
        self.assertEqual(swisstony["duplicate_fill_count"], 2)
        self.assertEqual(no_card["aligned_sharp_wallet_count"], 1)

    def test_selected_wallet_metadata_populates_modal_category_stats(self):
        original_meta = dict(SHARP_WALLET_META)
        SHARP_WALLET_META[("0xmeta", "FIFA WC")] = {
            "total_pnl": 123456.78,
            "roi_pct": 18.5,
            "open_volume": 9876.54,
            "total_volume": 667000.0,
            "resolved_volume": 657123.46,
            "win_rate": 61.2,
            "number_of_trades": 500,
            "number_of_open_positions": 12,
            "number_of_resolved_positions": 100,
            "win_count": 61,
            "source_url": "https://example.test/pma",
        }
        rows = [
            sample_row("meta", "0xmeta", "FIFA WC", "World Cup Final", 100, 100, 0, "Yes"),
        ]
        try:
            attach_category_aggregates(rows, rows)
            attach_selected_wallet_stats(rows)
            details = getPositionWalletDetails(rows, "World Cup Final", "Yes", category="FIFA WC")
            wallet = details["aligned_wallets"][0]
            self.assertEqual(wallet["wallet_category_pl"], 123456.78)
            self.assertEqual(wallet["wallet_category_roi_pct"], 18.5)
            self.assertEqual(wallet["wallet_category_portfolio_value"], 9876.54)
            self.assertEqual(wallet["wallet_category_total_volume"], 667000.0)
            self.assertEqual(wallet["wallet_historical_win_rate"], 61.2)
            self.assertEqual(wallet["source_link"], "https://example.test/pma")
            self.assertEqual(rows[0]["category_total_pl"], 123456.78)
            self.assertEqual(rows[0]["category_roi_pct"], 18.5)
        finally:
            SHARP_WALLET_META.clear()
            SHARP_WALLET_META.update(original_meta)


if __name__ == "__main__":
    unittest.main()
