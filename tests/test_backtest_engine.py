"""
test_backtest_engine.py
Unit and integration tests for WTB Historical Trade Replay & Backtesting Engine.
"""

import os
import sys
import json
import tempfile
import unittest

# Ensure current directory is in sys.path
_DIR = os.path.dirname(os.path.abspath(__file__))
_WTB_DIR = os.path.dirname(_DIR)
if _WTB_DIR not in sys.path:
    sys.path.insert(0, _WTB_DIR)

from backtest_engine import (
    BacktestConfig,
    DataLoader,
    TickSynthesizer,
    BacktestEngine,
    GridSearchOptimizer,
)


class TestBacktestEngine(unittest.TestCase):
    """Test suite for DataLoader, TickSynthesizer, BacktestEngine, and GridSearchOptimizer."""

    def test_data_loader_normalization_and_sorting(self):
        sample_trades = [
            {
                "timestamp_entry": "2026-08-10T12:00:00+00:00",
                "whale_wallet": "WhaleB",
                "token_address": "Token2",
                "entry_usd_price": 0.05,
                "exit_usd_price": 0.06,
                "trade_size_usd": 10.0,
                "max_profit_percent": 20.0,
                "hold_duration_seconds": 60,
                "exit_reason": "TAKE_PROFIT",
            },
            {
                "timestamp_entry": "2026-08-10T10:00:00+00:00",
                "whale_wallet": "WhaleA",
                "token_address": "Token1",
                "entry_usd_price": 0.01,
                "exit_usd_price": 0.009,
                "trade_size_usd": 10.0,
                "max_profit_percent": 2.0,
                "hold_duration_seconds": 45,
                "exit_reason": "STOP_LOSS",
            },
        ]

        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as f:
            json.dump(sample_trades, f)
            temp_path = f.name

        try:
            loaded = DataLoader.load(temp_path)
            self.assertEqual(len(loaded), 2)
            # Verify chronological sorting: 10:00 comes before 12:00
            self.assertEqual(loaded[0]["wallet"], "WhaleA")
            self.assertEqual(loaded[1]["wallet"], "WhaleB")
            self.assertEqual(loaded[0]["entry_usd_price"], 0.01)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_data_loader_jsonl_support(self):
        records = [
            '{"token_address": "Token1", "whale_wallet": "Whale1", "entry_usd_price": 1.0, "exit_usd_price": 1.1, "trade_size_usd": 10.0, "timestamp_entry": "2026-08-10T10:00:00Z"}\n',
            '{"token_address": "Token2", "whale_wallet": "Whale2", "entry_usd_price": 2.0, "exit_usd_price": 1.9, "trade_size_usd": 10.0, "timestamp_entry": "2026-08-10T11:00:00Z"}\n',
        ]
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".jsonl") as f:
            f.writelines(records)
            temp_path = f.name

        try:
            loaded = DataLoader.load(temp_path)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0]["token"], "Token1")
            self.assertEqual(loaded[1]["token"], "Token2")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_tick_synthesizer(self):
        # Trade that pumped +20% then retraced to +10%
        ticks = TickSynthesizer.generate_ticks(
            entry_price=1.0,
            max_profit_pct=20.0,
            exit_price=1.10,
            steps=10,
        )
        self.assertGreater(len(ticks), 2)
        self.assertEqual(ticks[0], 1.0)
        # Peak price should reach 1.20
        self.assertIn(1.20, ticks)
        # Final price should be 1.10
        self.assertEqual(ticks[-1], 1.10)

    def test_trailing_stop_in_simulation(self):
        # A trade that pumped +15% but eventually ended at -5%
        # With trailing stop enabled (+5% act, 3% callback), it should lock in +11.5% profit!
        mock_trades = [
            {
                "token": "PUMP_TOKEN",
                "wallet": "AlphaWhale",
                "timestamp_entry": "2026-08-10T10:00:00Z",
                "entry_usd_price": 1.00,
                "exit_usd_price": 0.95,        # Ended in loss if held
                "trade_size_usd": 10.0,
                "max_profit_percent": 15.0,     # Hit +15% peak
                "hold_duration_seconds": 120,
                "exit_reason": "STOP_LOSS",
                "fee_pct_per_leg": 1.0,
                "fixed_cost_usd": 0.16,
            }
        ]

        # 1. Without trailing stop: hits stop loss
        cfg_no_trail = BacktestConfig(
            trailing_stop_enabled=False,
            take_profit_pct=20.0,
            stop_loss_pct=-5.0,
            initial_capital_usd=25.0,
        )
        engine_no_trail = BacktestEngine(cfg_no_trail)
        report_no_trail = engine_no_trail.run(mock_trades)
        self.assertEqual(report_no_trail.trades[0].exit_reason, "STOP_LOSS")
        self.assertLess(report_no_trail.net_pnl_usd, 0)

        # 2. With trailing stop: catches retracement from +15% peak and exits with profit!
        cfg_trail = BacktestConfig(
            trailing_stop_enabled=True,
            trailing_activation_pct=5.0,
            trailing_callback_pct=3.0,
            take_profit_pct=20.0,
            stop_loss_pct=-5.0,
            initial_capital_usd=25.0,
        )
        engine_trail = BacktestEngine(cfg_trail)
        report_trail = engine_trail.run(mock_trades)
        res = report_trail.trades[0]
        self.assertEqual(res.exit_reason, "TRAILING_STOP")
        self.assertTrue(res.trailing_stop_used)
        self.assertGreater(res.net_pnl_usd, 0)
        self.assertGreater(report_trail.net_pnl_usd, 0)

    def test_whale_attribution_categorization(self):
        mock_trades = [
            # Alpha whale: 3 wins out of 3 trades
            {"token": "T1", "wallet": "Alpha1", "timestamp_entry": "2026-08-10T10:00:00Z", "entry_usd_price": 1.0, "exit_usd_price": 1.2, "trade_size_usd": 10.0, "max_profit_percent": 20.0, "hold_duration_seconds": 60, "exit_reason": "TAKE_PROFIT", "fee_pct_per_leg": 1.0, "fixed_cost_usd": 0.1},
            {"token": "T2", "wallet": "Alpha1", "timestamp_entry": "2026-08-10T11:00:00Z", "entry_usd_price": 1.0, "exit_usd_price": 1.15, "trade_size_usd": 10.0, "max_profit_percent": 15.0, "hold_duration_seconds": 60, "exit_reason": "TAKE_PROFIT", "fee_pct_per_leg": 1.0, "fixed_cost_usd": 0.1},
            {"token": "T3", "wallet": "Alpha1", "timestamp_entry": "2026-08-10T12:00:00Z", "entry_usd_price": 1.0, "exit_usd_price": 1.18, "trade_size_usd": 10.0, "max_profit_percent": 18.0, "hold_duration_seconds": 60, "exit_reason": "TAKE_PROFIT", "fee_pct_per_leg": 1.0, "fixed_cost_usd": 0.1},
            # Toxic whale: 3 losses out of 3 trades
            {"token": "T4", "wallet": "Toxic1", "timestamp_entry": "2026-08-10T13:00:00Z", "entry_usd_price": 1.0, "exit_usd_price": 0.9, "trade_size_usd": 10.0, "max_profit_percent": 0.0, "hold_duration_seconds": 60, "exit_reason": "STOP_LOSS", "fee_pct_per_leg": 1.0, "fixed_cost_usd": 0.1},
            {"token": "T5", "wallet": "Toxic1", "timestamp_entry": "2026-08-10T14:00:00Z", "entry_usd_price": 1.0, "exit_usd_price": 0.92, "trade_size_usd": 10.0, "max_profit_percent": 0.0, "hold_duration_seconds": 60, "exit_reason": "STOP_LOSS", "fee_pct_per_leg": 1.0, "fixed_cost_usd": 0.1},
            {"token": "T6", "wallet": "Toxic1", "timestamp_entry": "2026-08-10T15:00:00Z", "entry_usd_price": 1.0, "exit_usd_price": 0.91, "trade_size_usd": 10.0, "max_profit_percent": 0.0, "hold_duration_seconds": 60, "exit_reason": "STOP_LOSS", "fee_pct_per_leg": 1.0, "fixed_cost_usd": 0.1},
        ]

        engine = BacktestEngine(BacktestConfig(initial_capital_usd=50.0))
        report = engine.run(mock_trades)
        attr = report.get_whale_attribution()

        self.assertIn("Alpha1", attr)
        self.assertIn("Toxic1", attr)
        self.assertEqual(attr["Alpha1"]["status"], "ALPHA")
        self.assertEqual(attr["Alpha1"]["recommended_action"], "WHITELIST")
        self.assertEqual(attr["Toxic1"]["status"], "TOXIC")
        self.assertEqual(attr["Toxic1"]["recommended_action"], "BLACKLIST")

    def test_grid_search_optimizer(self):
        mock_trades = [
            {"token": "T1", "wallet": "W1", "timestamp_entry": "2026-08-10T10:00:00Z", "entry_usd_price": 1.0, "exit_usd_price": 1.25, "trade_size_usd": 10.0, "max_profit_percent": 25.0, "hold_duration_seconds": 60, "exit_reason": "TAKE_PROFIT", "fee_pct_per_leg": 1.0, "fixed_cost_usd": 0.16},
            {"token": "T2", "wallet": "W2", "timestamp_entry": "2026-08-10T11:00:00Z", "entry_usd_price": 1.0, "exit_usd_price": 0.94, "trade_size_usd": 10.0, "max_profit_percent": 4.0, "hold_duration_seconds": 60, "exit_reason": "STOP_LOSS", "fee_pct_per_leg": 1.0, "fixed_cost_usd": 0.16},
        ]

        results = GridSearchOptimizer.sweep(
            trades=mock_trades,
            take_profits=[10.0, 20.0],
            stop_losses=[-5.0],
            trailing_activations=[5.0],
            trailing_callbacks=[2.5],
            trailing_flags=[True, False],
        )

        self.assertGreater(len(results), 0)
        # Verify results are sorted by net_pnl_usd descending
        for i in range(len(results) - 1):
            self.assertGreaterEqual(results[i]["net_pnl_usd"], results[i + 1]["net_pnl_usd"])


if __name__ == "__main__":
    unittest.main()
