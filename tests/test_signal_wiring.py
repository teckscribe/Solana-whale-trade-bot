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
        STATE.token_cooldown_until.clear()
        STATE.token_entry_timestamps.clear()
        STATE.token_loss_timestamps.clear()
        STATE.token_consecutive_losses.clear()
        STATE.daily_realized_pnl_usd = 0.0
        STATE.daily_pnl_date = ""
        STATE.signal_journal_buffer.clear()
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
            return False
        async def priced(_):
            return (1.0, "TKN", "Token", 1e9, 1e9)
        # Consensus remains subject to the same momentum safety gate.
        with mock.patch.object(trade_brain, "get_token_price_usd", priced), \
             mock.patch.object(trade_brain, "check_momentum", capture_momentum), \
             mock.patch.object(trade_brain, "MOMENTUM_FILTER_ENABLED", True), \
             mock.patch.object(trade_brain, "WAVE_FILTER_ENABLED", False), \
             mock.patch.object(trade_brain, "get_token_decimals", mock.AsyncMock(return_value=6)):
            run(trade_brain.record_trade({
                "wallet": WHALE_B, "signature": "s2",
                "bought": [{"mint": TOKEN, "amount": 1.0}],
                "sold": [{"mint": SOL, "amount": 0.1}],
            }))
        self.assertEqual(set(STATE.recent_buy_signals[TOKEN]), {WHALE_A, WHALE_B})
        self.assertTrue(seen.get("momentum_called"))


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


class TestCooldownAndCircuitBreaker(_RecordTradeBase):
    def test_token_cooldown_blocks_immediate_reentry(self):
        now = 1_700_000_000.0
        # 1st loss: 2-minute anti-whipsaw pause (overridden by multi-whale consensus)
        STATE.register_token_exit(TOKEN, cooldown_minutes=15, now_ts=now, net_profit_usd=-1.0)
        blocked, reason = STATE.is_token_on_cooldown(TOKEN, now_ts=now + 30.0, max_entries_24h=2)
        self.assertTrue(blocked)
        self.assertIn("cooldown", reason.lower())

        # Multi-whale consensus overrides a single-loss anti-whipsaw pause
        consensus_blocked, _ = STATE.is_token_on_cooldown(TOKEN, now_ts=now + 30.0, max_entries_24h=2, is_consensus=True)
        self.assertFalse(consensus_blocked)

        # After 2m (121s), 1st-loss pause expires so bounce re-entries aren't missed
        blocked_after, _ = STATE.is_token_on_cooldown(TOKEN, now_ts=now + 121.0, max_entries_24h=2)
        self.assertFalse(blocked_after)

    def test_winning_exits_never_trigger_cooldown_or_cap_and_reset_loss_streak(self):
        now = 1_700_000_000.0
        # 1st trade loses (-$1.00) -> streak=1
        STATE.register_token_exit(TOKEN, cooldown_minutes=15, now_ts=now, net_profit_usd=-1.0)
        self.assertEqual(STATE.token_consecutive_losses.get(TOKEN), 1)

        # 2nd trade (3m later) wins (+$2.50) -> 0 cooldown, resets streak to 0!
        STATE.register_token_exit(TOKEN, cooldown_minutes=15, now_ts=now + 180.0, net_profit_usd=2.50)
        self.assertEqual(STATE.token_consecutive_losses.get(TOKEN), 0)

        # Immediate re-entry 5 seconds after a win is allowed (Wave 2 / Wave 3 runner!)
        blocked, _ = STATE.is_token_on_cooldown(TOKEN, now_ts=now + 185.0, max_entries_24h=2)
        self.assertFalse(blocked)

        # Multiple subsequent wins in 24h are never capped
        for i in range(4):
            STATE.register_token_exit(TOKEN, cooldown_minutes=15, now_ts=now + 300.0 + i * 60.0, net_profit_usd=1.25)
            blocked_win, _ = STATE.is_token_on_cooldown(TOKEN, now_ts=now + 305.0 + i * 60.0, max_entries_24h=2)
            self.assertFalse(blocked_win)

    def test_token_24h_loss_cap_blocks_consecutive_losing_reentries(self):
        now = 1_700_000_000.0
        STATE.register_token_exit(TOKEN, cooldown_minutes=15, now_ts=now, net_profit_usd=-1.0)
        STATE.register_token_exit(TOKEN, cooldown_minutes=15, now_ts=now + 4000.0, net_profit_usd=-1.2)
        blocked, reason = STATE.is_token_on_cooldown(TOKEN, now_ts=now + 8000.0, max_entries_24h=2)
        self.assertTrue(blocked)
        self.assertIn("2/2", reason)

    def test_daily_circuit_breaker_trips_on_excessive_loss(self):
        STATE.record_daily_pnl(-3.0)
        tripped, reason = STATE.is_circuit_breaker_tripped(max_daily_loss_pct=5.0, reference_equity=50.0)
        self.assertTrue(tripped)
        self.assertIn("circuit breaker", reason.lower())

    def test_stale_buy_signal_rejected_and_journaled(self):
        called = {"price": False}

        async def fake_price(_):
            called["price"] = True
            return (1.0, "TKN", "Token", 1e9, 1e9)

        with mock.patch.object(trade_brain, "get_token_price_usd", fake_price), \
             mock.patch.object(trade_brain, "MAX_SIGNAL_AGE_SECONDS", 12.0), \
             mock.patch.object(trade_brain, "WAVE_FILTER_ENABLED", False):
            run(trade_brain.record_trade({
                "wallet": WHALE_A,
                "signature": "stale_sig",
                "bought": [{"mint": TOKEN, "amount": 100.0}],
                "sold": [{"mint": SOL, "amount": 0.5}],
                "signal_age_seconds": 25.4,
            }))

        self.assertFalse(called["price"], "Stale buy signal must be rejected before pricing API calls")
        self.assertEqual(len(STATE.signal_journal_buffer), 1)
        self.assertEqual(STATE.signal_journal_buffer[0]["decision"], "REJECTED")
        self.assertEqual(STATE.signal_journal_buffer[0]["reason"], "STALE_SIGNAL")
        self.assertAlmostEqual(STATE.signal_journal_buffer[0]["signal_age_seconds"], 25.4, places=1)


if __name__ == "__main__":
    unittest.main()
