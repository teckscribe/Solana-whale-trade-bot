import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock


_DIR = os.path.dirname(os.path.abspath(__file__))
_WTB_DIR = os.path.dirname(_DIR)
if _WTB_DIR not in sys.path:
    sys.path.insert(0, _WTB_DIR)

import trade_brain
from position_state import Position, PositionState


TOKEN = "TokenLive11111111111111111111111111111111111"
WALLET = "WhaleLive11111111111111111111111111111111111"


class TestLiveOrderLifecycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        trade_brain.STATE.active.clear()
        trade_brain.STATE.closed_trades.clear()

    def tearDown(self):
        trade_brain.STATE.active.clear()
        trade_brain.STATE.closed_trades.clear()

    async def test_live_close_refuses_unconfirmed_settlement(self):
        pos = Position(TOKEN, "LIVE", WALLET, 1.0, 10.0, mode="LIVE")
        trade_brain.STATE.add_position(TOKEN, pos)
        with mock.patch.object(trade_brain, "send_exit_alert") as alert, \
             mock.patch.object(trade_brain, "_persist_active_positions_now", mock.AsyncMock()):
            closed = await trade_brain.close_gmgn_trade(
                WALLET, TOKEN, datetime.now(timezone.utc), 1.0, 1.1,
                10.0, "TAKE_PROFIT", 1.0,
            )
        self.assertFalse(closed)
        self.assertIn(TOKEN, trade_brain.STATE.active)
        self.assertEqual(trade_brain.STATE.closed_trades, [])
        alert.assert_not_called()

    async def test_unconfirmed_sell_remains_pending_and_is_not_recorded(self):
        pos = Position(TOKEN, "LIVE", WALLET, 1.0, 10.0, mode="LIVE")
        trade_brain.STATE.add_position(TOKEN, pos)
        result = {"confirmed": False, "status": "poll_timeout", "order_id": "sell-1"}
        with mock.patch.object(trade_brain, "execute_gmgn_sell_all", mock.AsyncMock(return_value=result)), \
             mock.patch.object(trade_brain, "send_error_alert"), \
             mock.patch.object(trade_brain, "_persist_active_positions_now", mock.AsyncMock()):
            closed = await trade_brain._attempt_confirmed_gmgn_sell(
                WALLET, TOKEN, datetime.now(timezone.utc), 1.0, 0.9,
                10.0, "STOP_LOSS", "wallet-address",
            )
        self.assertFalse(closed)
        self.assertEqual(pos.state, PositionState.PENDING_EXIT)
        self.assertEqual(pos.exit_order_id, "sell-1")
        self.assertIn(TOKEN, trade_brain.STATE.active)
        self.assertEqual(trade_brain.STATE.closed_trades, [])

    async def test_pending_sell_is_reconciled_without_resubmission(self):
        pos = Position(TOKEN, "LIVE", WALLET, 1.0, 10.0, mode="LIVE")
        pos.begin_exit("STOP_LOSS")
        pos.exit_order_id = "sell-1"
        trade_brain.STATE.add_position(TOKEN, pos)
        confirmation = {
            "confirmed": True,
            "status": "confirmed",
            "report": {"price_usd": "0.90", "realized_profit": "-1.0"},
        }
        with mock.patch.object(trade_brain, "wait_for_order_confirmed", mock.AsyncMock(return_value=confirmation)), \
             mock.patch.object(trade_brain, "execute_gmgn_sell_all", mock.AsyncMock()) as submit, \
             mock.patch.object(trade_brain, "send_exit_alert"), \
             mock.patch.object(trade_brain, "_persist_active_positions_now", mock.AsyncMock()):
            closed = await trade_brain._attempt_confirmed_gmgn_sell(
                WALLET, TOKEN, datetime.now(timezone.utc), 1.0, 0.9,
                10.0, "STOP_LOSS", "wallet-address",
            )
        self.assertTrue(closed)
        submit.assert_not_awaited()
        self.assertNotIn(TOKEN, trade_brain.STATE.active)
        self.assertEqual(len(trade_brain.STATE.closed_trades), 1)

    async def test_unknown_sell_submission_is_not_resubmitted(self):
        pos = Position(TOKEN, "LIVE", WALLET, 1.0, 10.0, mode="LIVE")
        pos.begin_exit("STOP_LOSS")
        pos.exit_confirmation_status = "SUBMISSION_UNKNOWN"
        trade_brain.STATE.add_position(TOKEN, pos)
        with mock.patch.object(trade_brain, "get_spl_token_balance", mock.AsyncMock(return_value=5.0)), \
             mock.patch.object(trade_brain, "execute_gmgn_sell_all", mock.AsyncMock()) as submit:
            closed = await trade_brain._attempt_confirmed_gmgn_sell(
                WALLET, TOKEN, datetime.now(timezone.utc), 1.0, 0.9,
                10.0, "STOP_LOSS", "wallet-address",
            )
        self.assertFalse(closed)
        submit.assert_not_awaited()
        self.assertIn(TOKEN, trade_brain.STATE.active)

    async def test_unknown_buy_submission_reconciles_by_token_balance(self):
        pos = Position(
            TOKEN, "LIVE", WALLET, 1.0, 10.0,
            state=PositionState.PENDING_ENTRY,
            entry_order_id=None,
            entry_confirmation_status="SUBMISSION_UNKNOWN",
            mode="LIVE",
        )
        trade_brain.STATE.add_position(TOKEN, pos)
        with mock.patch.object(trade_brain, "get_spl_token_balance", mock.AsyncMock(return_value=5.0)), \
             mock.patch.object(trade_brain, "monitor_gmgn_position", mock.AsyncMock()) as monitor, \
             mock.patch.object(trade_brain, "send_trade_alert"), \
             mock.patch.object(trade_brain, "_persist_active_positions_now", mock.AsyncMock()):
            await trade_brain.monitor_pending_gmgn_buy(TOKEN, "wallet-address")
        self.assertEqual(pos.state, PositionState.OPEN)
        self.assertEqual(pos.entry_confirmation_status, "BALANCE_CONFIRMED")
        monitor.assert_awaited_once()

    def test_confirmation_fields_survive_position_round_trip(self):
        pos = Position(
            TOKEN, "LIVE", WALLET, 1.0, 10.0,
            state=PositionState.PENDING_ENTRY,
            entry_order_id="buy-1",
            entry_confirmation_status="PENDING",
            exit_order_id="sell-1",
            exit_confirmation_status="POLL_TIMEOUT",
        )
        restored = Position.from_dict(pos.to_dict())
        self.assertEqual(restored.entry_order_id, "buy-1")
        self.assertEqual(restored.entry_confirmation_status, "PENDING")
        self.assertEqual(restored.exit_order_id, "sell-1")
        self.assertEqual(restored.exit_confirmation_status, "POLL_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
