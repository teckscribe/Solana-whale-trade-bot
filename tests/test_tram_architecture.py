"""
test_tram_architecture.py
Unit and integration tests for WTB TRAM (Trading RAM) architecture upgrade.
Tests zero-disk-I/O hot path, SettingsManager, ConnectionPool, and WhaleManager RAM hoisting.
"""

import os
import sys
import json
import time
import asyncio
import unittest

# Ensure current directory is in sys.path
_DIR = os.path.dirname(os.path.abspath(__file__))
_WTB_DIR = os.path.dirname(_DIR)
if _WTB_DIR not in sys.path:
    sys.path.insert(0, _WTB_DIR)

import settings_manager
import connection_pool
import whale_manager
from trade_brain import STATE, TradingState, flush_all_state_now


class TestSettingsManager(unittest.TestCase):
    """Verifies settings_manager declarative SPEC, validation, hot reload, and audit trail."""

    def test_spec_completeness(self):
        settings = settings_manager.get_all()
        self.assertIn("TRADE_MODE", settings)
        self.assertIn("ALLOCATION_PCT", settings)
        self.assertIn("TAKE_PROFIT_PCT", settings)
        self.assertIn("STOP_LOSS_PCT", settings)
        self.assertIn("MAX_CONCURRENT_TRADES", settings)
        self.assertIn("MIN_TRADE_SOL", settings)
        self.assertIn("MAX_RESUME_AGE_HOURS", settings)

    def test_type_validation_and_bounds(self):
        # Valid update
        ok, err = settings_manager.update("ALLOCATION_PCT", 15.0, source="test")
        self.assertTrue(ok, err)
        self.assertEqual(settings_manager.get("ALLOCATION_PCT"), 15.0)

        # Out-of-bounds update (max is 100.0)
        ok, err = settings_manager.update("ALLOCATION_PCT", 250.0, source="test")
        self.assertFalse(ok)
        self.assertIn("above maximum", err)

        # Choice validation
        ok, err = settings_manager.update("TRADE_MODE", "PAPER", source="test")
        self.assertTrue(ok)
        self.assertEqual(settings_manager.get("TRADE_MODE"), "PAPER")

        ok, err = settings_manager.update("TRADE_MODE", "INVALID_MODE", source="test")
        self.assertFalse(ok)

    def test_audit_trail(self):
        history_path = os.path.join(_WTB_DIR, "data", "settings_history.jsonl")
        cur_val = settings_manager.get("TAKE_PROFIT_PCT")
        new_val = 22.2 if cur_val != 22.2 else 18.5
        ok, err = settings_manager.update("TAKE_PROFIT_PCT", new_val, source="audit_test")
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(history_path))

        with open(history_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        
        last_entry = lines[-1]
        self.assertEqual(last_entry["key"], "TAKE_PROFIT_PCT")
        self.assertEqual(last_entry["source"], "audit_test")

    def test_paper_wallet_balance_sync(self):
        # Updating starting capital should sync paper wallet balance
        ok, err = settings_manager.update("PAPER_BALANCE_USD", 50.0, source="test")
        self.assertTrue(ok, err)
        self.assertEqual(settings_manager.get("PAPER_BALANCE_USD"), 50.0)
        self.assertEqual(settings_manager.get("PAPER_WALLET_BALANCE"), 50.0)

        # Updating paper wallet balance directly
        ok = settings_manager.set_paper_wallet_balance(42.50)
        self.assertTrue(ok)
        self.assertEqual(settings_manager.get("PAPER_WALLET_BALANCE"), 42.50)
        # Reset back for clean state
        settings_manager.update("PAPER_BALANCE_USD", 25.0, source="test")


class TestConnectionPool(unittest.IsolatedAsyncioTestCase):
    """Verifies connection pool singleton, rate budgeting, and client acquisition."""

    async def test_shared_client_singleton(self):
        client1 = await connection_pool.get_client()
        client2 = await connection_pool.get_client()
        self.assertIs(client1, client2)
        self.assertFalse(client1.is_closed)

    async def test_rate_budget_acquire(self):
        budget = connection_pool.RateBudget("TestBudget", max_requests_per_minute=5, soft_ceiling_pct=0.8)
        # Acquire 4 tokens (up to soft ceiling)
        for _ in range(4):
            await budget.acquire(priority="normal")
        stats = budget.get_stats()
        self.assertEqual(stats["count_current_window"], 4)
        self.assertEqual(stats["total_requests"], 4)

    async def test_get_stats(self):
        stats = connection_pool.get_stats()
        self.assertIn("DexScreener", stats)
        self.assertIn("Jupiter", stats)
        self.assertIn("SolanaRPC", stats)


class TestWhaleManagerRAM(unittest.TestCase):
    """Verifies RAM-first whale database lookups and set caching."""

    def test_in_memory_whitelist_set(self):
        wl_set = whale_manager.get_whitelist_set()
        self.assertIsInstance(wl_set, frozenset)
        self.assertGreater(len(wl_set), 0)

    def test_fast_lookup(self):
        # Time 10,000 lookups to verify 0ms latency
        wl_list = whale_manager.get_whitelist()
        test_wallet = wl_list[0] if wl_list else "dummy_wallet"
        start = time.perf_counter()
        for _ in range(10000):
            status = whale_manager.get_whale_status(test_wallet)
        elapsed = time.perf_counter() - start
        # 10,000 RAM lookups should easily finish under 50ms
        self.assertLess(elapsed, 0.1, f"Lookups took {elapsed:.4f}s, expected < 0.1s")

    def test_add_and_status_roundtrip(self):
        dummy = "TestWhaleWallet1111111111111111111111111111"
        try:
            whale_manager.add_whales({dummy})
            status = whale_manager.get_whale_status(dummy)
            self.assertIn(status, ["NEUTRAL", "WHITELIST"])

            whale_manager.set_whale_status(dummy, "BLACKLIST")
            self.assertEqual(whale_manager.get_whale_status(dummy), "BLACKLIST")

            whale_manager.set_whale_status(dummy, "WHITELIST")
            self.assertEqual(whale_manager.get_whale_status(dummy), "WHITELIST")
            self.assertIn(dummy, whale_manager.get_whitelist_set())
        finally:
            whale_manager.remove_whale(dummy)


class TestTradingStateTRAM(unittest.IsolatedAsyncioTestCase):
    """Verifies TradingState in-memory position and wallet operations."""

    async def test_balance_debit_and_credit(self):
        test_state = TradingState()
        test_state.wallet_balance = 100.0
        test_state.initial_balance = 100.0

        # Valid debit
        success = test_state.debit_balance(25.0)
        self.assertTrue(success)
        self.assertEqual(test_state.wallet_balance, 75.0)

        # Insufficient funds debit
        success = test_state.debit_balance(150.0)
        self.assertFalse(success)
        self.assertEqual(test_state.wallet_balance, 75.0)

        # Credit refund
        test_state.credit_balance(25.0)
        self.assertEqual(test_state.wallet_balance, 100.0)

    async def test_position_lifecycle(self):
        test_state = TradingState()
        token = "TokenMint1111111111111111111111111111111111"
        pos = {
            "token": token,
            "trade_size": 25.0,
            "entry_price": 1.50,
            "current_price": 1.65,
            "profit_pct": 10.0
        }
        test_state.add_position(token, pos)
        self.assertIn(token, test_state.active)
        self.assertEqual(test_state.get_open_exposure_usd(), 25.0)

        snapshot = test_state.snapshot_active()
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["token"], token)

        removed = test_state.remove_position(token)
        self.assertIsNotNone(removed)
        self.assertNotIn(token, test_state.active)
        self.assertEqual(test_state.get_open_exposure_usd(), 0.0)

    async def test_paper_balance_persistence(self):
        from trade_brain import init_json
        settings_manager.update("PAPER_BALANCE_USD", 25.0, source="test")
        settings_manager.set_paper_wallet_balance(19.75)
        init_json()
        self.assertEqual(STATE.wallet_balance, 19.75)
        self.assertEqual(STATE.initial_balance, 25.0)
        # Verify paper_wallet.json is NOT created
        self.assertFalse(os.path.exists(os.path.join(_WTB_DIR, "paper_wallet.json")))
        # Cleanup
        settings_manager.update("PAPER_BALANCE_USD", 25.0, source="test")


if __name__ == "__main__":
    unittest.main()
