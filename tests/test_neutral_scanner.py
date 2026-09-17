"""
tests/test_neutral_scanner.py
=========================================================
Unit and integration tests for the Neutral Whale Scanner,
2-Tier GMGN evaluation, disk caching, and alpha ranking.
=========================================================
"""

import os
import sys
import json
import time
import tempfile
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TESTS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import whale_scanner_core as scanner
import whale_manager


class TestNeutralWhaleScanner(unittest.TestCase):

    def setUp(self):
        # Use temporary directory for isolated cache and DB testing
        self.test_dir = tempfile.TemporaryDirectory()
        self.orig_cache_file = scanner.CACHE_FILE
        self.orig_data_dir = scanner.DATA_DIR
        scanner.CACHE_FILE = os.path.join(self.test_dir.name, "test_scan_cache.json")
        scanner.DATA_DIR = self.test_dir.name
        scanner.clear_cache()

    def tearDown(self):
        scanner.CACHE_FILE = self.orig_cache_file
        scanner.DATA_DIR = self.orig_data_dir
        scanner.clear_cache()
        self.test_dir.cleanup()

    def test_parse_gmgn_stats_output(self):
        """Test robust parsing of raw gmgn-cli JSON outputs."""
        sample_raw = json.dumps({
            "wallet_address": "TestWallet11111111111111111111111111111111",
            "native_balance": "4.25",
            "realized_profit": "1450.50",
            "buy": 25,
            "sell": 20,
            "pnl_stat": {
                "winrate": 0.65,
                "token_num": 15
            },
            "common": {
                "tags": ["smart_money", "dex_trader"]
            }
        })

        parsed = scanner.parse_gmgn_stats_output(sample_raw)
        self.assertEqual(parsed["winrate"], 65.0)
        self.assertEqual(parsed["trades"], 45)
        self.assertEqual(parsed["realized"], 1450.50)
        self.assertEqual(parsed["native_balance"], 4.25)
        self.assertIn("smart_money", parsed["tags"])

        # Test empty or malformed output
        self.assertEqual(scanner.parse_gmgn_stats_output(""), {})
        self.assertEqual(scanner.parse_gmgn_stats_output("Error: 404 Not Found"), {})

    def test_cache_save_and_load(self):
        """Test persistent disk caching and retrieval."""
        test_cache = {
            "wallet_abc": {
                "cached_at": time.time(),
                "7d": {"winrate": 72.0, "trades": 30, "realized": 500.0}
            }
        }
        scanner.save_cache(test_cache)

        loaded = scanner.load_cache()
        self.assertIn("wallet_abc", loaded)
        self.assertEqual(loaded["wallet_abc"]["7d"]["winrate"], 72.0)

    def test_rank_and_filter_whales(self):
        """Test alpha ranking, profit bonuses, and sniper bot filtering."""
        profiles = [
            # 1. Alpha whale: High WR, good trades, profitable
            {
                "wallet": "AlphaWhale111111111111111111111111111111111",
                "7d": {"winrate": 75.0, "trades": 35, "realized": 2500.0},
                "native_balance": 10.5,
                "tags": ["smart_money"]
            },
            # 2. Sniper bot with banned tag: Should be rejected
            {
                "wallet": "SniperBot222222222222222222222222222222222",
                "7d": {"winrate": 90.0, "trades": 40, "realized": 3000.0},
                "native_balance": 5.0,
                "tags": ["sniper", "fast_bot"]
            },
            # 3. Hyperactive bot: > 250 trades
            {
                "wallet": "HyperBot3333333333333333333333333333333333",
                "7d": {"winrate": 80.0, "trades": 450, "realized": 5000.0},
                "native_balance": 2.0,
                "tags": []
            },
            # 4. Low win rate whale: < 50%
            {
                "wallet": "LossWhale444444444444444444444444444444444",
                "7d": {"winrate": 30.0, "trades": 20, "realized": -150.0},
                "native_balance": 1.2,
                "tags": []
            },
            # 5. Moderate alpha: 60% WR, $800 profit
            {
                "wallet": "GoodWhale555555555555555555555555555555555",
                "7d": {"winrate": 60.0, "trades": 18, "realized": 800.0},
                "native_balance": 3.4,
                "tags": []
            }
        ]

        ranked = scanner.rank_and_filter_whales(
            profiles,
            min_wr=50.0,
            min_trades=5,
            min_profit=0.0,
            max_trades=250,
            filter_banned_tags=True
        )

        # SniperBot (tag), HyperBot (450 trades), and LossWhale (30% WR) must be rejected
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0]["wallet"], "AlphaWhale111111111111111111111111111111111")
        self.assertEqual(ranked[1]["wallet"], "GoodWhale555555555555555555555555555555555")
        self.assertGreater(ranked[0]["alpha_score"], ranked[1]["alpha_score"])

    def test_export_csv_and_json(self):
        """Test exporting ranked results to JSON and CSV formats."""
        ranked = [
            {
                "wallet": "TestAlpha12345678901234567890123456789012",
                "alpha_score": 85.5,
                "winrate_7d": 70.0,
                "trades_7d": 25,
                "realized_7d": 1200.0,
                "native_balance": 5.2,
                "tags": ["smart_money"],
                "1d": {"winrate": 65.0, "trades": 8},
                "7d": {"winrate": 70.0, "trades": 25, "realized": 1200.0},
                "30d": {"winrate": 68.0, "trades": 75}
            }
        ]

        json_file = os.path.join(self.test_dir.name, "ranked.json")
        csv_file = os.path.join(self.test_dir.name, "ranked.csv")

        scanner.export_ranked_json(ranked, json_file)
        scanner.export_ranked_csv(ranked, csv_file)

        self.assertTrue(os.path.exists(json_file))
        self.assertTrue(os.path.exists(csv_file))

        with open(csv_file, "r") as f:
            lines = f.readlines()
            self.assertGreaterEqual(len(lines), 2)
            self.assertIn("TestAlpha1234567890", lines[1])

    def test_candidate_chronological_sorting(self):
        """Test that get_candidates sorts wallets by last_active descending."""
        candidates = scanner.get_candidates("NEUTRAL", limit=10)
        if len(candidates) >= 2:
            for i in range(len(candidates) - 1):
                self.assertGreaterEqual(
                    candidates[i][1].get("last_active", 0),
                    candidates[i+1][1].get("last_active", 0)
                )


if __name__ == "__main__":
    unittest.main()
