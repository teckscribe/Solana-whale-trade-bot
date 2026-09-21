"""
tests/test_ai_whale_scorer.py
=========================================================
Unit tests for AI Whale Scorer & Whitelist Auto-Pruning
(Adapted from FOMO Robinhood Radar scoring pipeline)
=========================================================
"""

import os
import sys
import json
import tempfile
import unittest
from unittest import mock

_DIR = os.path.dirname(os.path.abspath(__file__))
_WTB_DIR = os.path.dirname(_DIR)
if _WTB_DIR not in sys.path:
    sys.path.insert(0, _WTB_DIR)

import ai_whale_scorer
from ai_whale_scorer import score_wallet, _heuristic_score, audit_all_whales
import whale_manager


class TestAIWhaleScorer(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.orig_wl = whale_manager.WHITELIST_FILE
        self.orig_disc = whale_manager.DISCOVERY_FILE
        whale_manager.WHITELIST_FILE = os.path.join(self.test_dir.name, "test_whitelist.json")
        whale_manager.DISCOVERY_FILE = os.path.join(self.test_dir.name, "test_discovery.json")
        whale_manager._whitelist_data = {}
        whale_manager._discovery_data = {}
        whale_manager._rebuild_sets()

    def tearDown(self):
        whale_manager.WHITELIST_FILE = self.orig_wl
        whale_manager.DISCOVERY_FILE = self.orig_disc
        whale_manager._load_into_ram()
        self.test_dir.cleanup()

    def test_heuristic_flags_toxic_dev(self):
        """Wallets with 'top_dev' or dev tags must be classified as toxic_dev and BLACKLIST."""
        data = {
            "winrate_7d": 80.0,
            "trades_7d": 40,
            "profit_7d": 35000.0,
            "tags": ["top_dev", "smart_degen"],
        }
        res = _heuristic_score("DevWallet1111111111111111111111111111111111", data)
        self.assertIn("toxic_dev", res.red_flags)
        self.assertEqual(res.status, "BLACKLIST")
        self.assertLess(res.score, 45)

    def test_heuristic_flags_mev_sniper_bot(self):
        """Wallets with MEV/sniper tags or excessive frequency must be classified as bot/mev and BLACKLIST."""
        data = {
            "winrate_7d": 72.0,
            "trades_7d": 320,
            "profit_7d": 12000.0,
            "tags": ["mev", "sniper"],
        }
        res = _heuristic_score("MevBotWallet11111111111111111111111111111111", data)
        self.assertIn("mev", res.red_flags)
        self.assertIn("bot", res.red_flags)
        self.assertEqual(res.status, "BLACKLIST")
        self.assertLess(res.score, 45)

    def test_heuristic_rewards_smart_swing_trader(self):
        """Consistent swing traders with good winrate and clean history must be WHITELIST."""
        data = {
            "winrate_7d": 68.0,
            "trades_7d": 45,
            "profit_7d": 18500.0,
            "tags": ["smart_degen", "gmgn"],
        }
        res = _heuristic_score("GoodWhale1111111111111111111111111111111111", data)
        self.assertEqual(len(res.red_flags), 0)
        self.assertEqual(res.status, "WHITELIST")
        self.assertGreaterEqual(res.score, 70)
        self.assertIn("swing", res.style)

    def test_audit_auto_prune_removes_toxic_whales(self):
        """audit_all_whales with auto_prune=True must demote toxic whales to discovery_db.json."""
        test_wl_path = whale_manager.WHITELIST_FILE
        test_wl = {
            "GoodWhale1111111111111111111111111111111111": {
                "winrate_7d": 75.0,
                "trades_7d": 30,
                "profit_7d": 20000.0,
                "tags": ["smart_degen"],
            },
            "ToxicDev11111111111111111111111111111111111": {
                "winrate_7d": 50.0,
                "trades_7d": 15,
                "profit_7d": 1000.0,
                "tags": ["top_dev"],
            }
        }
        with open(test_wl_path, "w") as f:
            json.dump(test_wl, f)

        # Load into RAM
        whale_manager._load_into_ram()
        self.assertEqual(len(whale_manager.get_whitelist_data()), 2)

        # Run audit with auto_prune
        rep = audit_all_whales(whitelist_path=test_wl_path, auto_prune=True)

        self.assertEqual(rep["total_evaluated"], 2)
        self.assertEqual(rep["retained_count"], 1)
        self.assertEqual(rep["pruned_count"], 1)

        # Verify ToxicDev was pruned from whitelist and placed in discovery as BLACKLIST
        self.assertNotIn("ToxicDev11111111111111111111111111111111111", whale_manager.get_whitelist_set())
        self.assertEqual(whale_manager.get_whale_status("ToxicDev11111111111111111111111111111111111"), "BLACKLIST")
        self.assertIn("GoodWhale1111111111111111111111111111111111", whale_manager.get_whitelist_set())

    def test_heuristic_flags_photon_axiom_snipers(self):
        """Wallets with 'photon', 'axiom', or 'arbitrager' tags must be flagged as MEV/bot and BLACKLIST."""
        for tag in ["photon", "axiom", "bloom", "arbitrager"]:
            data = {
                "winrate_7d": 80.0,
                "trades_7d": 50,
                "profit_7d": 5000.0,
                "tags": ["smart_degen", tag],
            }
            res = _heuristic_score(f"Sniper_{tag}_Wallet1111111111111111111", data)
            self.assertIn("mev", res.red_flags, f"Tag {tag} should trigger mev flag")
            self.assertIn("bot", res.red_flags, f"Tag {tag} should trigger bot flag")
            self.assertEqual(res.status, "BLACKLIST")
            self.assertLessEqual(res.score, 30)

    @mock.patch("ai_whale_scorer._get_copy_performance")
    def test_heuristic_flags_toxic_copy_history(self, mock_copy_perf):
        """Whales with negative copy-trading PnL in WTB must be flagged as toxic_copy_history and BLACKLIST."""
        mock_copy_perf.return_value = {
            "trades": 10,
            "wins": 0,
            "winrate": 0.0,
            "total_pnl": -15.50
        }
        data = {
            "winrate_7d": 90.0,
            "trades_7d": 40,
            "profit_7d": 10000.0,
            "tags": ["smart_degen"],
        }
        res = _heuristic_score("LossWhale1111111111111111111111111111111111", data)
        self.assertIn("toxic_copy_history", res.red_flags)
        self.assertEqual(res.status, "BLACKLIST")
        self.assertLessEqual(res.score, 20)


if __name__ == "__main__":
    unittest.main()
