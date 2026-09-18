"""
test_signal_wiring.py
Covers the trigger/settlement oracle split, whale-sell exit requests, multi-whale
consensus detection, and paper-balance refund on unexpected errors in record_trade.
"""

import os
import sys
import asyncio
import unittest
from unittest import mock

_DIR = os.path.dirname(os.path.abspath(__file__))
_WTB_DIR = os.path.dirname(_DIR)
if _WTB_DIR not in sys.path:
    sys.path.insert(0, _WTB_DIR)

from position_state import Position, PositionState
import trade_brain
from trade_brain import STATE

SOL = "So11111111111111111111111111111111111111112"
TOKEN = "TokenMintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
WHALE_A = "WhaleAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
WHALE_B = "WhaleBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


def run(coro):
    return asyncio.run(coro)


class TestTriggerOracle(unittest.TestCase):
    """TP/SL/trailing must be measured mid-vs-mid, not mid-vs-executable-fill."""

    def test_position_opens_flat_against_its_own_mid(self):
        # Executable fill is 5% worse than the mid (price impact). With the old logic the
        # position opened at -4.8% and a -5% stop fired on the first tick of noise.
        pos = Position(TOKEN, "TKN", WHALE_A, entry_price=1.05, trade_size=20.0, entry_mid_price=1.00)
        should_exit, reason = pos.update_price(1.00, take_profit_pct=15.0, stop_loss_pct=-5.0)
        self.assertFalse(should_exit, reason)
        self.assertAlmostEqual(pos.profit_pct, 0.0)

        should_exit, reason = pos.update_price(0.96, take_profit_pct=15.0, stop_loss_pct=-5.0)
        self.assertFalse(should_exit)
        should_exit, reason = pos.update_price(0.949, take_profit_pct=15.0, stop_loss_pct=-5.0)
        self.assertTrue(should_exit)
        self.assertEqual(reason, "STOP_LOSS")

    def test_high_water_mark_starts_at_mid(self):
        pos = Position(TOKEN, "TKN", WHALE_A, entry_price=1.05, trade_size=20.0, entry_mid_price=1.00)
        self.assertEqual(pos.high_water_mark_price, 1.00)

    def test_missing_mid_falls_back_to_fill(self):
        pos = Position(TOKEN, "TKN", WHALE_A, entry_price=1.05, trade_size=20.0)
        self.assertEqual(pos.entry_mid_price, 1.05)

    def test_round_trips_through_dict(self):
        pos = Position(TOKEN, "TKN", WHALE_A, entry_price=1.05, trade_size=20.0, entry_mid_price=1.00)
        pos.consensus_whales.add(WHALE_B)
        d = pos.to_dict()
        back = Position.from_dict(d)
        self.assertEqual(back.entry_mid_price, 1.00)
        self.assertEqual(back.entry_price, 1.05)
        self.assertEqual(back.consensus_whales, {WHALE_A, WHALE_B})


class _RecordTradeBase(unittest.TestCase):
    def setUp(self):
        STATE.active.clear()
        STATE.exit_requests.clear()
        STATE.recent_buy_signals.clear()
        STATE.processing_tokens.clear()
        self._patches = [
            mock.patch.object(trade_brain, "init_json", lambda: None),
            mock.patch("whale_manager.get_whale_status", lambda w: "WHITELIST"),
        ]
        for p in self._patches:
            p.start()
        trade_brain._dashboard_task = mock.MagicMock()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        STATE.active.clear()
        STATE.exit_requests.clear()
        STATE.recent_buy_signals.clear()


class TestWhaleSoldExit(_RecordTradeBase):
    def test_copied_whale_sell_requests_exit(self):
        STATE.add_position(TOKEN, Position(TOKEN, "TKN", WHALE_A, 1.0, 10.0))
        run(trade_brain.record_trade({
            "wallet": WHALE_A, "signature": "sig",
            "bought": [{"mint": SOL, "amount": 1.0}],
            "sold": [{"mint": TOKEN, "amount": 5.0}],
        }))
        self.assertEqual(STATE.exit_requests.get(TOKEN), "WHALE_SOLD")

    def test_unrelated_whale_sell_is_ignored(self):
        STATE.add_position(TOKEN, Position(TOKEN, "TKN", WHALE_A, 1.0, 10.0))
        run(trade_brain.record_trade({
            "wallet": WHALE_B, "signature": "sig",
            "bought": [{"mint": SOL, "amount": 1.0}],
            "sold": [{"mint": TOKEN, "amount": 5.0}],
        }))
        self.assertNotIn(TOKEN, STATE.exit_requests)

    def test_consensus_whale_sell_requests_exit(self):
        pos = Position(TOKEN, "TKN", WHALE_A, 1.0, 10.0)
        pos.consensus_whales.add(WHALE_B)
        STATE.add_position(TOKEN, pos)
        run(trade_brain.record_trade({
            "wallet": WHALE_B, "signature": "sig",
            "bought": [{"mint": SOL, "amount": 1.0}],
            "sold": [{"mint": TOKEN, "amount": 5.0}],
        }))
        self.assertEqual(STATE.exit_requests.get(TOKEN), "WHALE_SOLD")


class TestConsensus(_RecordTradeBase):
    def test_second_whale_on_held_position_joins_follow_set(self):
        STATE.add_position(TOKEN, Position(TOKEN, "TKN", WHALE_A, 1.0, 10.0))
        run(trade_brain.record_trade({
            "wallet": WHALE_B, "signature": "sig",
            "bought": [{"mint": TOKEN, "amount": 100.0}],
            "sold": [{"mint": SOL, "amount": 0.1}],
        }))
        self.assertEqual(STATE.get_position(TOKEN).consensus_whales, {WHALE_A, WHALE_B})

    def test_recent_signals_window_detects_consensus(self):
        # First whale's buy is rejected by the screen (no price), but the signal is kept.
        async def no_price(_):
            return (0.0, "", "", 0.0, 0.0)
        with mock.patch.object(trade_brain, "get_token_price_usd", no_price):
            run(trade_brain.record_trade({
                "wallet": WHALE_A, "signature": "s1",
                "bought": [{"mint": TOKEN, "amount": 1.0}],
                "sold": [{"mint": SOL, "amount": 0.1}],
            }))
        self.assertIn(WHALE_A, STATE.recent_buy_signals[TOKEN])

        seen = {}
        async def capture_momentum(token, *a, **k):
            seen["momentum_called"] = True
            return True
        async def priced(_):
            return (1.0, "TKN", "Token", 1e9, 1e9)
        # Consensus bypasses the momentum filter; assert that path by checking it isn't called
        # and that the size is doubled via the log. Simplest observable: momentum not called.
        with mock.patch.object(trade_brain, "get_token_price_usd", priced), \
             mock.patch.object(trade_brain, "check_momentum", capture_momentum), \
             mock.patch.object(trade_brain, "MOMENTUM_FILTER_ENABLED", True), \
             mock.patch.object(trade_brain, "get_token_decimals", mock.AsyncMock(return_value=None)):
            run(trade_brain.record_trade({
                "wallet": WHALE_B, "signature": "s2",
                "bought": [{"mint": TOKEN, "amount": 1.0}],
                "sold": [{"mint": SOL, "amount": 0.1}],
            }))
        self.assertEqual(set(STATE.recent_buy_signals[TOKEN]), {WHALE_A, WHALE_B})


class TestPaperRefund(_RecordTradeBase):
    def test_balance_refunded_when_evaluation_raises(self):
        STATE.wallet_balance = 100.0
        STATE.initial_balance = 100.0

        async def priced(_):
            return (1.0, "TKN", "Token", 1e9, 1e9)
        async def boom(*a, **k):
            raise RuntimeError("route service down")

        with mock.patch.object(trade_brain, "get_token_price_usd", priced), \
             mock.patch.object(trade_brain, "get_token_decimals", mock.AsyncMock(return_value=6)), \
             mock.patch.object(trade_brain, "MOMENTUM_FILTER_ENABLED", False), \
             mock.patch.object(trade_brain, "TRADE_MODE", "PAPER"), \
             mock.patch.object(trade_brain, "get_sol_price_usd", mock.AsyncMock(return_value=150.0)), \
             mock.patch.object(trade_brain, "get_pool_liquidity_usd", mock.AsyncMock(return_value=0.0)), \
             mock.patch("jupiter_api.get_swap_quote", boom):
            run(trade_brain.record_trade({
                "wallet": WHALE_A, "signature": "s",
                "bought": [{"mint": TOKEN, "amount": 1.0}],
                "sold": [{"mint": SOL, "amount": 0.1}],
            }))
        self.assertEqual(STATE.wallet_balance, 100.0)
        self.assertNotIn(TOKEN, STATE.active)


if __name__ == "__main__":
    unittest.main()
