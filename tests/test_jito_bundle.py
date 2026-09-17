"""
test_jito_bundle.py
Unit and integration tests for WTB Jito MEV Bundle Engine.
"""

import os
import sys
import unittest
import asyncio

# Ensure current directory is in sys.path
_DIR = os.path.dirname(os.path.abspath(__file__))
_WTB_DIR = os.path.dirname(_DIR)
if _WTB_DIR not in sys.path:
    sys.path.insert(0, _WTB_DIR)

import settings_manager
import connection_pool
from jito_bundle import (
    JITO_TIP_ACCOUNTS,
    BLOCK_ENGINES,
    MIN_TIP_LAMPORTS,
    MAX_TIP_LAMPORTS,
    get_random_tip_account,
    get_block_engine_url,
    get_tip_floor,
    get_effective_tip_lamports,
    send_bundle,
    simulate_bundle_execution,
)


class TestJitoBundle(unittest.IsolatedAsyncioTestCase):
    """Test suite for Jito MEV Bundle Engine."""

    def test_canonical_tip_accounts(self):
        self.assertEqual(len(JITO_TIP_ACCOUNTS), 8)
        for _ in range(20):
            acct = get_random_tip_account()
            self.assertIn(acct, JITO_TIP_ACCOUNTS)
            self.assertGreaterEqual(len(acct), 43)

    def test_block_engine_regions(self):
        self.assertIn("mainnet", BLOCK_ENGINES)
        self.assertIn("amsterdam", BLOCK_ENGINES)
        self.assertIn("frankfurt", BLOCK_ENGINES)
        self.assertIn("ny", BLOCK_ENGINES)
        self.assertIn("tokyo", BLOCK_ENGINES)

        # Region resolution
        self.assertIn("amsterdam", get_block_engine_url("amsterdam"))
        self.assertIn("tokyo", get_block_engine_url("tokyo"))
        # Unknown region should fall back to mainnet
        self.assertEqual(get_block_engine_url("mars_orbit"), BLOCK_ENGINES["mainnet"])

    async def test_tip_floor_and_effective_tip(self):
        tip_lamports = await get_effective_tip_lamports()
        self.assertIsInstance(tip_lamports, int)
        self.assertGreaterEqual(tip_lamports, MIN_TIP_LAMPORTS)
        self.assertLessEqual(tip_lamports, MAX_TIP_LAMPORTS)

    async def test_bundle_validation_guards(self):
        # Empty bundle
        res = await send_bundle([])
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "empty_bundle")

        # Exceeds max 5 transactions
        oversized = [f"tx_data_{i}" for i in range(6)]
        res = await send_bundle(oversized)
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "bundle_exceeds_max_5_transactions")

    async def test_bundle_simulation_mode(self):
        sim = await simulate_bundle_execution(
            token="PumpCoin1111111111111111111111111111111111",
            side="BUY",
            amount_usd=10.0,
        )
        self.assertTrue(sim["success"])
        self.assertTrue(sim["simulation"])
        self.assertTrue(sim["anti_mev_protection"])
        self.assertTrue(sim["atomic_execution"])
        self.assertIn(sim["tip_account"], JITO_TIP_ACCOUNTS)
        self.assertGreater(sim["tip_lamports"], 0)
        self.assertGreater(sim["simulated_latency_ms"], 0)

    async def test_settings_hot_reload_for_jito(self):
        # Update JITO_TIP_SOL
        ok, err = settings_manager.update("JITO_TIP_SOL", 0.0005, source="test")
        self.assertTrue(ok, err)
        self.assertEqual(settings_manager.get("JITO_TIP_SOL"), 0.0005)

        # Disable dynamic tip to test static fallback
        ok, err = settings_manager.update("JITO_DYNAMIC_TIP", False, source="test")
        self.assertTrue(ok, err)

        effective_tip = await get_effective_tip_lamports()
        # 0.0005 SOL = 500,000 lamports
        self.assertEqual(effective_tip, 500_000)

        # Disable Jito completely
        ok, err = settings_manager.update("JITO_ENABLED", False, source="test")
        self.assertTrue(ok, err)
        self.assertEqual(await get_effective_tip_lamports(), 0)

        # Reset clean state
        settings_manager.update("JITO_ENABLED", True, source="test")
        settings_manager.update("JITO_DYNAMIC_TIP", True, source="test")
        settings_manager.update("JITO_TIP_SOL", 0.0001, source="test")

    async def test_connection_pool_stats_has_jito(self):
        stats = connection_pool.get_stats()
        self.assertIn("JitoBlockEngine", stats)
        self.assertEqual(stats["JitoBlockEngine"]["name"], "JitoBlockEngine")


if __name__ == "__main__":
    unittest.main()
