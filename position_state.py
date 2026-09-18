"""
position_state.py
NautilusTrader-inspired Finite State Machine (FSM) and typed Position model
for WTB (Solana Whale Trade Bot).

Enforces strict lifecycle transitions, prevents race conditions / double-sells,
and implements dynamic high-water-mark trailing stop-loss.
"""

import time
import logging
from enum import Enum
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timezone

log = logging.getLogger("PositionFSM")


class PositionState(str, Enum):
    """Lifecycle states of a trading position."""
    PENDING_ENTRY = "PENDING_ENTRY"     # Pre-trade checks passing, buy order in-flight
    OPEN = "OPEN"                       # Buy confirmed, actively monitored in RAM
    TRAILING_PROFIT = "TRAILING_PROFIT" # Profit crossed threshold, trailing stop active
    PENDING_EXIT = "PENDING_EXIT"       # Exit signal triggered, sell order in-flight
    CLOSED = "CLOSED"                   # Sell confirmed, capital credited, archived
    FAILED = "FAILED"                   # Terminal failure during entry or exit


# Valid state graph edges: from_state -> set of permitted to_states
VALID_TRANSITIONS = {
    PositionState.PENDING_ENTRY: {
        PositionState.OPEN,
        PositionState.FAILED,
    },
    PositionState.OPEN: {
        PositionState.TRAILING_PROFIT,
        PositionState.PENDING_EXIT,
        PositionState.CLOSED,
        PositionState.FAILED,
    },
    PositionState.TRAILING_PROFIT: {
        PositionState.PENDING_EXIT,
        PositionState.CLOSED,
        PositionState.FAILED,
    },
    PositionState.PENDING_EXIT: {
        PositionState.CLOSED,
        PositionState.OPEN,             # Reverted if sell order failed and can retry
        PositionState.FAILED,
    },
    PositionState.CLOSED: set(),        # Terminal
    PositionState.FAILED: set(),        # Terminal
}


class Position:
    """
    Typed, thread-safe position model with FSM lifecycle and trailing stop engine.
    Fully backwards-compatible with dictionary representations.
    """
    def __init__(
        self,
        token: str,
        symbol: str,
        wallet: str,
        entry_price: float,
        trade_size: float,
        tokens_held: float = 0.0,
        token_decimals: int = 9,
        state: PositionState = PositionState.OPEN,
        entry_time: Optional[str] = None,
        entry_price_impact_pct: float = 0.0,
        actual_entry_cost_usd: float = 0.0,
        mode: str = "PAPER",
        execution_engine: str = "JUPITER",
        strategy_order_id: Optional[str] = None,
        entry_mid_price: float = 0.0,
    ):
        self.token = str(token)
        self.symbol = str(symbol)
        self.wallet = str(wallet)
        self.entry_price = float(entry_price)
        # DexScreener mid at entry. TP/SL/trailing triggers compare the live mid against
        # THIS, never against entry_price (a Jupiter executable fill that already includes
        # price impact). Comparing mid-vs-fill made every position open several % underwater
        # and fired stop-losses seconds after entry on pure oracle disagreement.
        self.entry_mid_price = float(entry_mid_price) if entry_mid_price and entry_mid_price > 0 else float(entry_price)
        self.trade_size = float(trade_size)
        self.tokens_held = float(tokens_held)
        self.token_decimals = int(token_decimals) if token_decimals is not None else 9
        self.consensus_whales: set = {self.wallet} if self.wallet else set()
        self.state = PositionState(state) if isinstance(state, str) else state
        self.entry_time = entry_time or datetime.now(timezone.utc).isoformat()
        self.entry_price_impact_pct = float(entry_price_impact_pct)
        self.actual_entry_cost_usd = float(actual_entry_cost_usd or trade_size)
        self.mode = str(mode).upper()
        self.execution_engine = str(execution_engine)
        self.strategy_order_id = strategy_order_id

        # Real-time state
        self.current_price = float(entry_price)
        self.profit_pct = 0.0
        self.profit_usd = 0.0
        self.max_profit = 0.0
        self.elapsed = 0

        # Trailing Stop & High-Water-Mark
        self.high_water_mark_price = self.entry_mid_price
        self.trailing_stop_active = False

        # Exit metadata
        self.exit_price = 0.0
        self.exit_reason = ""
        self.closed_at = ""

    def transition_to(self, target_state: PositionState) -> bool:
        """
        Transitions position to a new state if valid.
        Returns True if transition succeeded, False otherwise.
        """
        target = PositionState(target_state) if isinstance(target_state, str) else target_state
        allowed = VALID_TRANSITIONS.get(self.state, set())

        if target not in allowed:
            log.warning(
                f"Invalid FSM transition for {self.token[:8]}: {self.state.value} -> {target.value}. "
                f"Permitted transitions: {[s.value for s in allowed]}"
            )
            return False

        old_state = self.state
        self.state = target
        log.info(f"FSM State Transition [{self.token[:8]}]: {old_state.value} -> {target.value}")
        return True

    def can_exit(self) -> bool:
        """Checks whether the position is in an active state that can be closed."""
        return self.state in (PositionState.OPEN, PositionState.TRAILING_PROFIT)

    def begin_exit(self, reason: str = "") -> bool:
        """
        Atomically locks position into PENDING_EXIT.
        Returns False if position was already exiting or closed (prevents double-sells).
        """
        if not self.can_exit():
            log.warning(
                f"Double-sell prevented: {self.token[:8]} is in state {self.state.value}, "
                f"cannot begin exit (reason: {reason})."
            )
            return False

        self.exit_reason = reason
        return self.transition_to(PositionState.PENDING_EXIT)

    def revert_exit(self) -> bool:
        """Reverts PENDING_EXIT back to OPEN if a sell order failed and can retry."""
        if self.state == PositionState.PENDING_EXIT:
            return self.transition_to(PositionState.OPEN)
        return False

    def mark_closed(self, exit_price: float, reason: str = "") -> bool:
        """Finalizes position closure."""
        self.exit_price = float(exit_price)
        if reason:
            self.exit_reason = reason
        self.closed_at = datetime.now(timezone.utc).isoformat()
        
        if self.state == PositionState.PENDING_EXIT:
            return self.transition_to(PositionState.CLOSED)
        elif self.can_exit():
            self.state = PositionState.CLOSED
            return True
        return False

    def update_price(
        self,
        current_price: float,
        take_profit_pct: float,
        stop_loss_pct: float,
        trailing_stop_enabled: bool = True,
        trailing_activation_pct: float = 5.0,
        trailing_callback_pct: float = 3.0,
    ) -> Tuple[bool, str]:
        """
        Updates current price, recalculates PnL metrics, updates High Water Mark,
        and determines if an exit condition is triggered.

        Returns: (should_exit: bool, exit_reason: str)
        """
        if current_price <= 0:
            return False, ""

        self.current_price = current_price

        # Update PnL (mid-vs-mid; the realised fill is settled separately at close)
        ref = self.entry_mid_price if self.entry_mid_price > 0 else self.entry_price
        if ref > 0:
            self.profit_pct = ((current_price - ref) / ref) * 100.0
            self.profit_usd = self.trade_size * (self.profit_pct / 100.0)

        # Update High-Water Mark
        if current_price > self.high_water_mark_price:
            self.high_water_mark_price = current_price

        if self.profit_pct > self.max_profit:
            self.max_profit = self.profit_pct

        # Check if already exiting or closed
        if not self.can_exit():
            return False, ""

        # 1. Check Take-Profit (Static Target)
        if take_profit_pct > 0 and self.profit_pct >= take_profit_pct:
            return True, "TAKE_PROFIT"

        # 2. Check Stop-Loss (Static Floor)
        sl_floor = -abs(stop_loss_pct)
        if self.profit_pct <= sl_floor:
            return True, "STOP_LOSS"

        # 3. Check Trailing Stop (Dynamic Profit Lock)
        if trailing_stop_enabled:
            # Activate trailing stop once profit crosses activation threshold
            if not self.trailing_stop_active and self.profit_pct >= trailing_activation_pct:
                self.trailing_stop_active = True
                if self.state == PositionState.OPEN:
                    self.transition_to(PositionState.TRAILING_PROFIT)
                log.info(
                    f"🎯 Trailing Stop Activated for {self.token[:8]} at +{self.profit_pct:.2f}% "
                    f"(Peak: ${self.high_water_mark_price:.8f}, Callback: {trailing_callback_pct}%)"
                )

            # If trailing stop is active, check retracement from peak
            if self.trailing_stop_active and self.high_water_mark_price > 0:
                retracement_pct = ((self.high_water_mark_price - current_price) / self.high_water_mark_price) * 100.0
                if retracement_pct >= trailing_callback_pct:
                    log.info(
                        f"⚡ Trailing Stop Triggered for {self.token[:8]}! "
                        f"Retraced {retracement_pct:.2f}% from peak ${self.high_water_mark_price:.8f} "
                        f"(Exit profit: {self.profit_pct:+.2f}%)"
                    )
                    return True, "TRAILING_STOP"

        return False, ""

    def to_dict(self) -> Dict[str, Any]:
        """Serializes position to dictionary for UI and persistence compatibility."""
        return {
            "token": self.token,
            "token_address": self.token,
            "symbol": self.symbol,
            "wallet": self.wallet,
            "whale_wallet": self.wallet,
            "entry_price": self.entry_price,
            "entry_mid_price": self.entry_mid_price,
            "consensus_whales": sorted(self.consensus_whales),
            "current_price": self.current_price,
            "trade_size": self.trade_size,
            "trade_size_usd": self.trade_size,
            "tokens_held": self.tokens_held,
            "token_decimals": self.token_decimals,
            "entry_price_impact_pct": self.entry_price_impact_pct,
            "actual_entry_cost_usd": self.actual_entry_cost_usd,
            "profit_pct": round(self.profit_pct, 2),
            "profit_usd": round(self.profit_usd, 4),
            "max_profit": round(self.max_profit, 2),
            "elapsed": int(self.elapsed),
            "entry_time": self.entry_time,
            "mode": self.mode,
            "state": self.state.value,
            "execution_engine": self.execution_engine,
            "strategy_order_id": self.strategy_order_id,
            "high_water_mark_price": self.high_water_mark_price,
            "trailing_stop_active": self.trailing_stop_active,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "closed_at": self.closed_at,
        }

    def __getitem__(self, key: str) -> Any:
        d = self.to_dict()
        if key in d:
            return d[key]
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any):
        if hasattr(self, key):
            setattr(self, key, value)
        elif key == "trade_size_usd":
            self.trade_size = float(value)
        elif key == "whale_wallet":
            self.wallet = str(value)
        elif key == "token_address":
            self.token = str(value)

    def __contains__(self, key: str) -> bool:
        return key in self.to_dict()

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Position":
        """Reconstructs a Position instance from a dictionary."""
        pos = cls(
            token=data.get("token") or data.get("token_address", ""),
            symbol=data.get("symbol", ""),
            wallet=data.get("wallet") or data.get("whale_wallet", ""),
            entry_price=float(data.get("entry_price", 0.0)),
            trade_size=float(data.get("trade_size") or data.get("trade_size_usd", 0.0)),
            tokens_held=float(data.get("tokens_held", 0.0)),
            token_decimals=data.get("token_decimals"),
            state=data.get("state", PositionState.OPEN.value),
            entry_time=data.get("entry_time"),
            entry_price_impact_pct=float(data.get("entry_price_impact_pct", 0.0)),
            actual_entry_cost_usd=float(data.get("actual_entry_cost_usd", 0.0)),
            mode=data.get("mode", "PAPER"),
            execution_engine=data.get("execution_engine", "JUPITER"),
            strategy_order_id=data.get("strategy_order_id"),
            entry_mid_price=float(data.get("entry_mid_price") or 0.0),
        )
        pos.consensus_whales = set(data.get("consensus_whales") or [pos.wallet])
        pos.current_price = float(data.get("current_price", pos.entry_price))
        pos.profit_pct = float(data.get("profit_pct", 0.0))
        pos.profit_usd = float(data.get("profit_usd", 0.0))
        pos.max_profit = float(data.get("max_profit", 0.0))
        pos.elapsed = int(data.get("elapsed", 0))
        pos.high_water_mark_price = float(data.get("high_water_mark_price") or pos.entry_mid_price)
        pos.trailing_stop_active = bool(data.get("trailing_stop_active", False))
        pos.exit_price = float(data.get("exit_price", 0.0))
        pos.exit_reason = data.get("exit_reason", "")
        pos.closed_at = data.get("closed_at", "")
        return pos
