"""
tests/test_phantom_profit_defense.py
================================================================================
Unit tests for Phantom Profit Defense in trade_brain.py.
Verifies that fake TAKE_PROFIT or TRAILING_STOP exits triggered by DexScreener
mid-price spikes are rejected when Jupiter executable sell quotes yield net losses
due to pool illiquidity, bid-ask spread, or network/trading fees.
================================================================================
"""

import os
import sys
import asyncio
import unittest
from unittest import mock
from datetime import datetime, timezone

_DIR = os.path.dirname(os.path.abspath(__file__))
_WTB_DIR = os.path.dirname(_DIR)
if _WTB_DIR not in sys.path:
    sys.path.insert(0, _WTB_DIR)

import trade_brain
from position_state import Position, PositionState


class TestPhantomProfitDefense(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        trade_brain._refresh_settings()
        trade_brain.STATE.active.clear()

    @mock.patch("trade_brain.send_exit_alert")
    @mock.patch("trade_brain.get_sol_price_usd", return_value=150.0)
    @mock.patch("trade_brain.get_executable_price_usd")
    async def test_phantom_take_profit_rejected_when_fill_is_underwater(
        self, mock_exec_price, mock_sol_price, mock_alert
    ):
        """
        If DexScreener mid triggered TAKE_PROFIT, but Jupiter executable quote
        nets a negative return, close_trade must return False and refuse to exit.
        """
        # Bought at $0.0010, trade_size $25
        entry_price = 0.0010
        trade_size = 25.0
        tokens_held = 25000.0
        token_decimals = 6
        entry_time = datetime.now(timezone.utc)

        # Executable price is 0.00095 (underwater due to spread/slippage)
        mock_exec_price.return_value = 0.00095

        closed = await trade_brain.close_trade(
            wallet="Whale1111111111111111111111111111111111111",
            token="Token1111111111111111111111111111111111111",
            entry_time=entry_time,
            entry_price=entry_price,
            exit_price=0.0012,  # DexScreener mid was +20%
            max_profit=20.0,
            reason="TAKE_PROFIT",
            trade_size=trade_size,
            tokens_held=tokens_held,
            token_decimals=token_decimals,
        )

        self.assertFalse(closed, "close_trade must reject phantom TAKE_PROFIT when executable quote is negative")
        mock_alert.assert_not_called()

    @mock.patch("trade_brain.send_exit_alert")
    @mock.patch("trade_brain.get_sol_price_usd", return_value=150.0)
    @mock.patch("trade_brain.get_executable_price_usd")
    async def test_real_take_profit_accepted_when_fill_is_profitable(
        self, mock_exec_price, mock_sol_price, mock_alert
    ):
        """
        If executable quote nets a positive return after fees, TAKE_PROFIT must succeed.
        """
        entry_price = 0.0010
        trade_size = 25.0
        tokens_held = 25000.0
        token_decimals = 6
        entry_time = datetime.now(timezone.utc)

        # Executable price is 0.0013 (+30% gross, well above fees)
        mock_exec_price.return_value = 0.0013

        closed = await trade_brain.close_trade(
            wallet="Whale1111111111111111111111111111111111111",
            token="Token1111111111111111111111111111111111111",
            entry_time=entry_time,
            entry_price=entry_price,
            exit_price=0.0013,
            max_profit=30.0,
            reason="TAKE_PROFIT",
            trade_size=trade_size,
            tokens_held=tokens_held,
            token_decimals=token_decimals,
        )

        self.assertTrue(closed, "close_trade must succeed when executable quote is profitable")
        mock_alert.assert_called_once()

    @mock.patch("trade_brain.send_exit_alert")
    @mock.patch("trade_brain.get_sol_price_usd", return_value=150.0)
    @mock.patch("trade_brain.get_executable_price_usd")
    async def test_stop_loss_always_executes_even_when_negative(
        self, mock_exec_price, mock_sol_price, mock_alert
    ):
        """
        STOP_LOSS must always execute to protect downside risk, regardless of negative return.
        """
        entry_price = 0.0010
        trade_size = 25.0
        tokens_held = 25000.0
        token_decimals = 6
        entry_time = datetime.now(timezone.utc)

        # Executable price is 0.00085 (-15% stop loss)
        mock_exec_price.return_value = 0.00085

        closed = await trade_brain.close_trade(
            wallet="Whale1111111111111111111111111111111111111",
            token="Token1111111111111111111111111111111111111",
            entry_time=entry_time,
            entry_price=entry_price,
            exit_price=0.00085,
            max_profit=0.0,
            reason="STOP_LOSS",
            trade_size=trade_size,
            tokens_held=tokens_held,
            token_decimals=token_decimals,
        )

        self.assertTrue(closed, "close_trade must always execute STOP_LOSS")
        mock_alert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
