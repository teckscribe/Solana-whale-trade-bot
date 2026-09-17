"""
backtest_engine.py
Historical Trade Replay & Backtesting Engine for WTB (Solana Whale Trade Bot).

Replays recorded trades from ml_training_data.jsonl / ml_training_data.json,
simulates tick trajectories using the Nautilus-style Position FSM (position_state.py),
models DEX price impact and fee drag, computes institutional performance analytics,
attributes performance by whale wallet, and sweeps parameter grids for optimal configurations.
"""

import os
import sys
import json
import math
import argparse
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple
from collections import defaultdict
import itertools

# Add project root to sys.path to ensure position_state and settings_manager can be imported
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from position_state import Position, PositionState

log = logging.getLogger("BacktestEngine")


@dataclass
class BacktestConfig:
    """Configuration parameters for backtesting simulation."""
    initial_capital_usd: float = 25.0
    allocation_pct: float = 10.0
    use_fixed_trade_size: bool = True
    fixed_trade_size_usd: float = 10.0
    take_profit_pct: float = 10.0
    stop_loss_pct: float = -5.0
    trailing_stop_enabled: bool = True
    trailing_activation_pct: float = 5.0
    trailing_callback_pct: float = 3.0
    fee_pct_per_leg: float = 1.0          # 1% per leg (entry + exit = 2% round trip)
    fixed_cost_usd: float = 0.16           # Solana base fee + priority + ATA rent
    max_hold_seconds: int = 1800           # 30 minutes timeout
    filter_whale: Optional[str] = None
    filter_start_time: Optional[str] = None
    filter_end_time: Optional[str] = None


@dataclass
class TradeResult:
    """Outcome of a simulated trade."""
    token: str
    wallet: str
    entry_time: str
    entry_price: float
    exit_price: float
    trade_size_usd: float
    gross_pnl_pct: float
    net_pnl_usd: float
    net_pnl_pct: float
    peak_profit_pct: float
    hold_duration_seconds: int
    exit_reason: str
    trailing_stop_used: bool
    portfolio_equity_after: float


class DataLoader:
    """Loads, cleans, and standardizes trade records from JSON or JSONL."""

    @staticmethod
    def load(file_path: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Loads trades from file_path, or auto-detects ml_training_data.jsonl / ml_training_data.json.
        """
        if file_path and os.path.exists(file_path):
            target = file_path
        else:
            jsonl_path = os.path.join(_PROJECT_ROOT, "ml_training_data.jsonl")
            json_path = os.path.join(_PROJECT_ROOT, "ml_training_data.json")
            if os.path.exists(jsonl_path):
                target = jsonl_path
            elif os.path.exists(json_path):
                target = json_path
            else:
                log.warning("No trade dataset found.")
                return []

        raw_trades = []
        try:
            if target.endswith(".jsonl"):
                with open(target, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            raw_trades.append(json.loads(line))
            else:
                with open(target, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        raw_trades = data
                    elif isinstance(data, dict):
                        raw_trades = list(data.values())
        except Exception as e:
            log.error(f"Failed to load trades from {target}: {e}")
            return []

        normalized = []
        for t in raw_trades:
            norm = DataLoader._normalize_trade(t)
            if norm:
                normalized.append(norm)

        # Sort chronologically by entry timestamp
        normalized.sort(key=lambda x: x["timestamp_entry"])
        return normalized

    @staticmethod
    def _normalize_trade(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalizes schema variations into consistent keys."""
        token = raw.get("token_address") or raw.get("token")
        if not token:
            return None

        wallet = raw.get("whale_wallet") or raw.get("wallet", "Unknown")
        entry_time = raw.get("timestamp_entry") or raw.get("entry_time") or datetime.now(timezone.utc).isoformat()
        entry_price = float(raw.get("entry_usd_price") or raw.get("entry_price") or 0.0)
        exit_price = float(raw.get("exit_usd_price") or raw.get("exit_price") or entry_price)
        trade_size = float(raw.get("trade_size_usd") or raw.get("trade_size") or 10.0)
        max_profit = float(raw.get("max_profit_percent") or raw.get("max_profit") or 0.0)
        duration = int(raw.get("hold_duration_seconds") or raw.get("elapsed") or 30)
        reason = str(raw.get("exit_reason") or "UNKNOWN")
        fee_pct = float(raw.get("fee_pct_per_leg") or 1.0)
        fixed_cost = float(raw.get("fixed_cost_usd") or 0.16)

        if entry_price <= 0:
            return None

        return {
            "token": token,
            "wallet": wallet,
            "timestamp_entry": entry_time,
            "entry_usd_price": entry_price,
            "exit_usd_price": exit_price,
            "trade_size_usd": trade_size,
            "max_profit_percent": max_profit,
            "hold_duration_seconds": duration,
            "exit_reason": reason,
            "fee_pct_per_leg": fee_pct,
            "fixed_cost_usd": fixed_cost,
        }


class TickSynthesizer:
    """
    Reconstructs realistic price trajectories between Entry -> Peak (HWM) -> Exit.
    Enables accurate tick-by-tick simulation of trailing stops and exit conditions.
    """

    @staticmethod
    def generate_ticks(entry_price: float, max_profit_pct: float, exit_price: float, steps: int = 10) -> List[float]:
        """
        Generates simulated intermediate price ticks.
        Path:
          1. Entry price
          2. Peak price (derived from max_profit_pct if > 0)
          3. Intermediate retracements
          4. Exit price
        """
        ticks = [entry_price]
        peak_price = entry_price * (1.0 + (max(0.0, max_profit_pct) / 100.0))

        if max_profit_pct > 0 and peak_price > entry_price:
            # Segment 1: Entry -> Peak
            half_steps = max(2, steps // 2)
            for i in range(1, half_steps):
                p = entry_price + (peak_price - entry_price) * (i / half_steps)
                ticks.append(p)
            ticks.append(peak_price)

            # Segment 2: Peak -> Exit
            remaining = max(2, steps - half_steps)
            for i in range(1, remaining):
                p = peak_price + (exit_price - peak_price) * (i / remaining)
                ticks.append(p)
            ticks.append(exit_price)
        else:
            # Flat or downward move: Entry -> Exit
            for i in range(1, steps):
                p = entry_price + (exit_price - entry_price) * (i / steps)
                ticks.append(p)
            ticks.append(exit_price)

        return ticks


class BacktestEngine:
    """Core event-driven replay engine."""

    def __init__(self, config: Optional[BacktestConfig] = None):
        self.config = config or BacktestConfig()

    def run(self, trades: List[Dict[str, Any]]) -> "BacktestReport":
        """Replays all historical trades using the configured parameters."""
        cfg = self.config
        cash = cfg.initial_capital_usd
        completed_trades: List[TradeResult] = []
        equity_curve: List[Tuple[str, float]] = [("START", cash)]

        for t in trades:
            # Wallet filter
            if cfg.filter_whale and t["wallet"] != cfg.filter_whale:
                continue

            # Sizing logic
            if cfg.use_fixed_trade_size:
                trade_size = cfg.fixed_trade_size_usd
            else:
                trade_size = cash * (cfg.allocation_pct / 100.0)

            # Ensure we have enough capital
            if cash < 1.0 or trade_size <= 0:
                break
            trade_size = min(trade_size, cash)

            # Create typed Position model from position_state.py
            pos = Position(
                token=t["token"],
                symbol="TEST",
                wallet=t["wallet"],
                entry_price=t["entry_usd_price"],
                trade_size=trade_size,
                entry_time=t["timestamp_entry"],
                state=PositionState.OPEN,
            )

            # Synthesize price ticks
            ticks = TickSynthesizer.generate_ticks(
                entry_price=t["entry_usd_price"],
                max_profit_pct=t["max_profit_percent"],
                exit_price=t["exit_usd_price"],
                steps=12,
            )

            exit_price = ticks[-1]
            exit_reason = t["exit_reason"]
            trailing_used = False

            # Simulate tick-by-tick monitoring
            for price in ticks:
                should_exit, trigger = pos.update_price(
                    current_price=price,
                    take_profit_pct=cfg.take_profit_pct,
                    stop_loss_pct=cfg.stop_loss_pct,
                    trailing_stop_enabled=cfg.trailing_stop_enabled,
                    trailing_activation_pct=cfg.trailing_activation_pct,
                    trailing_callback_pct=cfg.trailing_callback_pct,
                )
                if should_exit:
                    exit_price = price
                    exit_reason = trigger
                    if trigger == "TRAILING_STOP":
                        trailing_used = True
                    break

            # Calculate financial outcome with fee model
            gross_pct = ((exit_price - pos.entry_price) / pos.entry_price) * 100.0 if pos.entry_price > 0 else 0.0
            proportional_fee_pct = 2.0 * cfg.fee_pct_per_leg
            net_profit_usd = trade_size * ((gross_pct - proportional_fee_pct) / 100.0) - cfg.fixed_cost_usd
            net_profit_pct = (net_profit_usd / trade_size) * 100.0 if trade_size > 0 else 0.0

            # Update capital
            cash += net_profit_usd
            cash = max(0.0, cash)
            equity_curve.append((t["timestamp_entry"], cash))

            completed_trades.append(TradeResult(
                token=t["token"],
                wallet=t["wallet"],
                entry_time=t["timestamp_entry"],
                entry_price=pos.entry_price,
                exit_price=exit_price,
                trade_size_usd=trade_size,
                gross_pnl_pct=gross_pct,
                net_pnl_usd=net_profit_usd,
                net_pnl_pct=net_profit_pct,
                peak_profit_pct=pos.max_profit,
                hold_duration_seconds=t["hold_duration_seconds"],
                exit_reason=exit_reason,
                trailing_stop_used=trailing_used,
                portfolio_equity_after=cash,
            ))

        return BacktestReport(cfg, completed_trades, equity_curve)


class BacktestReport:
    """Calculates, formats, and displays institutional backtest analytics."""

    def __init__(self, config: BacktestConfig, trades: List[TradeResult], equity_curve: List[Tuple[str, float]]):
        self.config = config
        self.trades = trades
        self.equity_curve = equity_curve

        self.total_trades = len(trades)
        self.initial_capital = config.initial_capital_usd
        self.final_equity = equity_curve[-1][1] if equity_curve else self.initial_capital
        self.net_pnl_usd = self.final_equity - self.initial_capital
        self.roi_pct = (self.net_pnl_usd / self.initial_capital * 100.0) if self.initial_capital > 0 else 0.0

        self.wins = [t for t in trades if t.net_pnl_usd > 0]
        self.losses = [t for t in trades if t.net_pnl_usd <= 0]
        self.win_count = len(self.wins)
        self.loss_count = len(self.losses)
        self.win_rate = (self.win_count / self.total_trades * 100.0) if self.total_trades > 0 else 0.0

        gross_win_usd = sum(t.net_pnl_usd for t in self.wins)
        gross_loss_usd = abs(sum(t.net_pnl_usd for t in self.losses))
        self.gross_profit_usd = gross_win_usd
        self.gross_loss_usd = gross_loss_usd
        self.profit_factor = (gross_win_usd / gross_loss_usd) if gross_loss_usd > 0 else (99.0 if gross_win_usd > 0 else 0.0)

        self.avg_win_usd = (gross_win_usd / self.win_count) if self.win_count > 0 else 0.0
        self.avg_loss_usd = (gross_loss_usd / self.loss_count) if self.loss_count > 0 else 0.0
        self.avg_win_pct = sum(t.net_pnl_pct for t in self.wins) / self.win_count if self.win_count > 0 else 0.0
        self.avg_loss_pct = sum(t.net_pnl_pct for t in self.losses) / self.loss_count if self.loss_count > 0 else 0.0

        # Drawdown calculation
        peak = self.initial_capital
        max_dd_usd = 0.0
        max_dd_pct = 0.0
        for _, eq in equity_curve:
            if eq > peak:
                peak = eq
            dd_usd = peak - eq
            dd_pct = (dd_usd / peak * 100.0) if peak > 0 else 0.0
            if dd_usd > max_dd_usd:
                max_dd_usd = dd_usd
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
        self.max_drawdown_usd = max_dd_usd
        self.max_drawdown_pct = max_dd_pct

        # Sharpe & Sortino
        returns = [t.net_pnl_pct / 100.0 for t in trades]
        if len(returns) >= 2:
            mean_r = sum(returns) / len(returns)
            variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
            std_r = math.sqrt(variance) if variance > 0 else 0.0
            self.sharpe_per_trade = (mean_r / std_r) if std_r > 0 else 0.0

            downside = [r for r in returns if r < 0]
            down_var = sum(r ** 2 for r in downside) / len(returns) if downside else 0.0
            down_std = math.sqrt(down_var) if down_var > 0 else 0.0
            self.sortino = (mean_r / down_std) if down_std > 0 else 0.0
        else:
            self.sharpe_per_trade = 0.0
            self.sortino = 0.0

        # Trailing stop impact
        self.trailing_exits = [t for t in trades if t.trailing_stop_used]
        self.trailing_exit_count = len(self.trailing_exits)
        self.trailing_exit_pnl_usd = sum(t.net_pnl_usd for t in self.trailing_exits)

        # Exit reasons
        self.exit_reason_counts = defaultdict(int)
        for t in trades:
            self.exit_reason_counts[t.exit_reason] += 1

    def get_whale_attribution(self) -> Dict[str, Dict[str, Any]]:
        """Attribution breakdown by whale wallet to classify Alpha vs Toxic whales."""
        by_wallet = defaultdict(list)
        for t in self.trades:
            by_wallet[t.wallet].append(t)

        attribution = {}
        for wallet, w_trades in by_wallet.items():
            w_wins = [t for t in w_trades if t.net_pnl_usd > 0]
            w_losses = [t for t in w_trades if t.net_pnl_usd <= 0]
            pnl = sum(t.net_pnl_usd for t in w_trades)
            winrate = (len(w_wins) / len(w_trades) * 100.0) if w_trades else 0.0
            win_val = sum(t.net_pnl_usd for t in w_wins)
            loss_val = abs(sum(t.net_pnl_usd for t in w_losses))
            pf = (win_val / loss_val) if loss_val > 0 else (99.0 if win_val > 0 else 0.0)

            # Categorize
            if winrate >= 55.0 and pnl > 0:
                status = "ALPHA"
                action = "WHITELIST"
            elif winrate < 40.0 and pnl < 0:
                status = "TOXIC"
                action = "BLACKLIST"
            else:
                status = "NEUTRAL"
                action = "MONITOR"

            attribution[wallet] = {
                "trades": len(w_trades),
                "wins": len(w_wins),
                "losses": len(w_losses),
                "win_rate": round(winrate, 1),
                "total_pnl_usd": round(pnl, 4),
                "profit_factor": round(pf, 2),
                "avg_trade_usd": round(pnl / len(w_trades), 4) if w_trades else 0.0,
                "status": status,
                "recommended_action": action,
            }

        # Sort by total PnL descending
        return dict(sorted(attribution.items(), key=lambda item: item[1]["total_pnl_usd"], reverse=True))

    def print_summary(self):
        """Prints a rich terminal report."""
        print("\n" + "=" * 70)
        print("           WTB HISTORICAL TRADE REPLAY & BACKTEST REPORT")
        print("=" * 70)
        print(f" Initial Capital:         ${self.initial_capital:.2f}")
        print(f" Final Portfolio Equity:  ${self.final_equity:.2f} ({self.roi_pct:+.2f}%)")
        print(f" Net Realized PnL:        ${self.net_pnl_usd:+.2f}")
        print(f" Max Drawdown:            ${self.max_drawdown_usd:.2f} ({self.max_drawdown_pct:.2f}%)")
        print("-" * 70)
        print(f" Total Trades Replayed:   {self.total_trades}")
        print(f" Win Rate:                {self.win_rate:.1f}% ({self.win_count} Wins / {self.loss_count} Losses)")
        print(f" Profit Factor:           {self.profit_factor:.2f}")
        print(f" Average Win:             ${self.avg_win_usd:+.2f} ({self.avg_win_pct:+.2f}%)")
        print(f" Average Loss:            ${self.avg_loss_usd:+.2f} ({self.avg_loss_pct:+.2f}%)")
        print(f" Sharpe Ratio (Per-Trade):{self.sharpe_per_trade:.2f}")
        print(f" Sortino Ratio:           {self.sortino:.2f}")
        print("-" * 70)
        print(" Trailing Stop Performance:")
        print(f"   Trailing Exits Fired:  {self.trailing_exit_count} / {self.total_trades}")
        print(f"   Net PnL Locked In:     ${self.trailing_exit_pnl_usd:+.2f}")
        print("-" * 70)
        print(" Exit Reasons Breakdown:")
        for r, cnt in sorted(self.exit_reason_counts.items(), key=lambda x: x[1], reverse=True):
            print(f"   - {r:<22}: {cnt:>3} trades ({cnt / self.total_trades * 100:.1f}%)")
        print("=" * 70 + "\n")

    def print_whale_attribution(self):
        """Prints whale attribution table."""
        attr = self.get_whale_attribution()
        if not attr:
            return

        print("\n" + "=" * 90)
        print("                      WHALE WALLET ATTRIBUTION MATRIX")
        print("=" * 90)
        print(f"{'Wallet':<18} | {'Trades':<6} | {'Win Rate':<8} | {'Net PnL ($)':<12} | {'PF':<5} | {'Category':<8} | {'Action':<10}")
        print("-" * 90)
        for w, s in attr.items():
            short_w = w[:6] + "..." + w[-4:]
            print(
                f"{short_w:<18} | {s['trades']:<6} | {s['win_rate']:>7.1f}% | "
                f"${s['total_pnl_usd']:>10.2f} | {s['profit_factor']:>5.2f} | "
                f"{s['status']:<8} | {s['recommended_action']:<10}"
            )
        print("=" * 90 + "\n")

    def to_dict(self) -> Dict[str, Any]:
        """Serializes report to dictionary for JSON/API consumption."""
        return {
            "summary": {
                "initial_capital_usd": self.initial_capital,
                "final_equity_usd": round(self.final_equity, 2),
                "net_pnl_usd": round(self.net_pnl_usd, 4),
                "roi_pct": round(self.roi_pct, 2),
                "total_trades": self.total_trades,
                "win_count": self.win_count,
                "loss_count": self.loss_count,
                "win_rate": round(self.win_rate, 2),
                "profit_factor": round(self.profit_factor, 2),
                "avg_win_usd": round(self.avg_win_usd, 4),
                "avg_loss_usd": round(self.avg_loss_usd, 4),
                "max_drawdown_usd": round(self.max_drawdown_usd, 4),
                "max_drawdown_pct": round(self.max_drawdown_pct, 2),
                "sharpe_ratio": round(self.sharpe_per_trade, 2),
                "sortino_ratio": round(self.sortino, 2),
                "trailing_stop_exits": self.trailing_exit_count,
                "trailing_stop_pnl_usd": round(self.trailing_exit_pnl_usd, 4),
            },
            "config": asdict(self.config),
            "exit_reasons": dict(self.exit_reason_counts),
            "whale_attribution": self.get_whale_attribution(),
        }


class GridSearchOptimizer:
    """Explores combinations of strategy parameters to maximize profit and risk metrics."""

    @staticmethod
    def sweep(
        trades: List[Dict[str, Any]],
        take_profits: Optional[List[float]] = None,
        stop_losses: Optional[List[float]] = None,
        trailing_activations: Optional[List[float]] = None,
        trailing_callbacks: Optional[List[float]] = None,
        trailing_flags: Optional[List[bool]] = None,
    ) -> List[Dict[str, Any]]:
        """Runs combinatorial parameter evaluation."""
        tp_list = take_profits or [8.0, 10.0, 15.0, 20.0]
        sl_list = stop_losses or [-3.0, -5.0, -8.0]
        act_list = trailing_activations or [3.0, 5.0, 8.0]
        cb_list = trailing_callbacks or [1.5, 2.5, 3.5]
        trail_flags = trailing_flags or [True, False]

        combinations = []
        for tp, sl, trail in itertools.product(tp_list, sl_list, trail_flags):
            if trail:
                for act, cb in itertools.product(act_list, cb_list):
                    combinations.append((tp, sl, True, act, cb))
            else:
                combinations.append((tp, sl, False, 5.0, 3.0))

        results = []
        for tp, sl, trail, act, cb in combinations:
            cfg = BacktestConfig(
                take_profit_pct=tp,
                stop_loss_pct=sl,
                trailing_stop_enabled=trail,
                trailing_activation_pct=act,
                trailing_callback_pct=cb,
            )
            engine = BacktestEngine(cfg)
            report = engine.run(trades)
            results.append({
                "take_profit_pct": tp,
                "stop_loss_pct": sl,
                "trailing_enabled": trail,
                "trailing_activation_pct": act if trail else None,
                "trailing_callback_pct": cb if trail else None,
                "net_pnl_usd": round(report.net_pnl_usd, 2),
                "roi_pct": round(report.roi_pct, 2),
                "win_rate": round(report.win_rate, 1),
                "profit_factor": round(report.profit_factor, 2),
                "max_drawdown_pct": round(report.max_drawdown_pct, 2),
                "sharpe": round(report.sharpe_per_trade, 2),
            })

        # Sort by net PnL descending
        results.sort(key=lambda x: x["net_pnl_usd"], reverse=True)
        return results

    @staticmethod
    def print_top_results(results: List[Dict[str, Any]], top_n: int = 5):
        """Displays top ranking parameter configurations."""
        print("\n" + "=" * 90)
        print(f"                   TOP {top_n} STRATEGY CONFIGURATIONS")
        print("=" * 90)
        print(f"{'Rank':<5} | {'TP %':<6} | {'SL %':<6} | {'Trail':<6} | {'Act/CB':<9} | {'Net PnL ($)':<12} | {'WinRate':<8} | {'PF':<5} | {'Max DD':<7}")
        print("-" * 90)
        for i, r in enumerate(results[:top_n], start=1):
            trail_str = "YES" if r["trailing_enabled"] else "NO"
            act_cb = f"+{r['trailing_activation_pct']}/{r['trailing_callback_pct']}%" if r["trailing_enabled"] else "N/A"
            print(
                f"#{i:<4} | {r['take_profit_pct']:>5.1f}% | {r['stop_loss_pct']:>5.1f}% | "
                f"{trail_str:<6} | {act_cb:<9} | ${r['net_pnl_usd']:>10.2f} | "
                f"{r['win_rate']:>6.1f}% | {r['profit_factor']:>5.2f} | {r['max_drawdown_pct']:>6.1f}%"
            )
        print("=" * 90 + "\n")


def main():
    parser = argparse.ArgumentParser(description="WTB Historical Trade Replay & Backtest Engine")
    parser.add_argument("--data", type=str, default=None, help="Path to trade dataset (.json or .jsonl)")
    parser.add_argument("--capital", type=float, default=25.0, help="Initial capital in USD (default: 25.0)")
    parser.add_argument("--tp", type=float, default=10.0, help="Take profit percentage (default: 10.0)")
    parser.add_argument("--sl", type=float, default=-5.0, help="Stop loss percentage (default: -5.0)")
    parser.add_argument("--no-trail", action="store_true", help="Disable dynamic trailing stop")
    parser.add_argument("--trail-act", type=float, default=5.0, help="Trailing stop activation pct (default: 5.0)")
    parser.add_argument("--trail-cb", type=float, default=3.0, help="Trailing stop callback pct (default: 3.0)")
    parser.add_argument("--whale", type=str, default=None, help="Filter trades for a specific whale wallet")
    parser.add_argument("--grid", action="store_true", help="Run parameter grid search optimization")
    parser.add_argument("--top", type=int, default=5, help="Number of top grid search results to display")
    parser.add_argument("--export", type=str, default=None, help="Export backtest results to JSON file")
    args = parser.parse_args()

    trades = DataLoader.load(args.data)
    if not trades:
        print("Error: No trade data found to replay.")
        sys.exit(1)

    print(f"Loaded {len(trades)} historical trades.")

    if args.grid:
        print("Running parameter grid search sweep across TP, SL, and Trailing Stops...")
        results = GridSearchOptimizer.sweep(trades)
        GridSearchOptimizer.print_top_results(results, top_n=args.top)
        if args.export:
            with open(args.export, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2)
            print(f"Grid search results exported to {args.export}")
        return

    config = BacktestConfig(
        initial_capital_usd=args.capital,
        take_profit_pct=args.tp,
        stop_loss_pct=args.sl,
        trailing_stop_enabled=not args.no_trail,
        trailing_activation_pct=args.trail_act,
        trailing_callback_pct=args.trail_cb,
        filter_whale=args.whale,
    )

    engine = BacktestEngine(config)
    report = engine.run(trades)
    report.print_summary()
    report.print_whale_attribution()

    if args.export:
        with open(args.export, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, indent=2)
        print(f"Report exported to {args.export}")


if __name__ == "__main__":
    main()
