"""
trade_brain.py (Dual-Mode: Paper & Live Engine)
"""
import os
import json
import logging
import asyncio
import base64
import httpx
import random
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from dotenv import load_dotenv

from telegram_notifier import send_trade_alert, send_exit_alert, send_error_alert
from jupiter_api import (
    get_token_price_usd, get_live_price, get_sol_price_usd,
    get_token_decimals, get_pool_liquidity_usd,
    price_from_quote, get_executable_price_usd, quote_price_impact_pct,
)
from gmgn_trader import (
    execute_gmgn_buy, wait_for_order_confirmed,
    get_strategy_status, cancel_strategy_order, execute_gmgn_sell_all
)

# Load Environment Variables & Settings
load_dotenv()
import settings_manager
settings_manager.migrate_from_env()

WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "").strip()
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
JITO_BLOCK_ENGINE_URL = os.getenv("JITO_BLOCK_ENGINE_URL", "https://mainnet.block-engine.jito.wtf").strip()

# Dynamic Runtime Settings (managed and hot-reloaded by settings_manager)
TRADE_MODE = settings_manager.get("TRADE_MODE")
ALLOCATION_PCT = float(settings_manager.get("ALLOCATION_PCT"))
ML_ENGINE_ENABLED = bool(settings_manager.get("ML_ENGINE"))
MOMENTUM_FILTER_ENABLED = bool(settings_manager.get("MOMENTUM_FILTER_ENABLED"))
ML_CONFIDENCE = float(settings_manager.get("ML_CONFIDENCE"))
MIN_TRADE_SOL = float(settings_manager.get("MIN_TRADE_SOL"))
LIVE_FEE_RESERVE_SOL = float(settings_manager.get("LIVE_FEE_RESERVE_SOL"))
TAKE_PROFIT_PCT = float(settings_manager.get("TAKE_PROFIT_PCT"))
STOP_LOSS_PCT = -abs(float(settings_manager.get("STOP_LOSS_PCT")))
TIMEOUT_ENABLED = bool(settings_manager.get("TIMEOUT_ENABLED"))
TIMEOUT_MINUTES = int(settings_manager.get("TIMEOUT_MINUTES"))
MAX_HOLD_SECONDS = TIMEOUT_MINUTES * 60
MAX_CONCURRENT_TRADES = int(settings_manager.get("MAX_CONCURRENT_TRADES"))
MIN_24H_VOLUME = float(settings_manager.get("MIN_24H_VOLUME"))
MIN_MARKET_CAP = float(settings_manager.get("MIN_MARKET_CAP"))
PAPER_FEE_PCT_PER_LEG = float(settings_manager.get("PAPER_FEE_PCT_PER_LEG"))
MAX_PRICE_IMPACT_PCT = float(settings_manager.get("MAX_PRICE_IMPACT_PCT"))
MAX_POOL_SHARE_PCT = float(settings_manager.get("MAX_POOL_SHARE_PCT"))
MAX_PORTFOLIO_EXPOSURE_PCT = float(settings_manager.get("MAX_PORTFOLIO_EXPOSURE_PCT"))

SOL_BASE_TX_FEE = 0.000005
PRIORITY_FEE_SOL = float(os.getenv("GMGN_PRIORITY_FEE", "0.00001"))
TIP_FEE_SOL = float(os.getenv("GMGN_TIP_FEE", "0.00001"))
ATA_RENT_SOL = float(os.getenv("ATA_RENT_SOL", "0.00204"))
ATA_RENT_RECLAIMED = os.getenv("ATA_RENT_RECLAIMED", "FALSE").strip().upper() == "TRUE"

POLL_INTERVAL = max(2.0, (60 * MAX_CONCURRENT_TRADES) / 280.0)

def _refresh_settings():
    """Hot-reloads settings from settings_manager without needing a bot restart."""
    global TRADE_MODE, ALLOCATION_PCT, ML_ENGINE_ENABLED, MOMENTUM_FILTER_ENABLED
    global ML_CONFIDENCE, MIN_TRADE_SOL, LIVE_FEE_RESERVE_SOL, TAKE_PROFIT_PCT
    global STOP_LOSS_PCT, TIMEOUT_ENABLED, TIMEOUT_MINUTES, MAX_HOLD_SECONDS
    global MAX_CONCURRENT_TRADES, MIN_24H_VOLUME, MIN_MARKET_CAP
    global PAPER_FEE_PCT_PER_LEG, MAX_PRICE_IMPACT_PCT, MAX_POOL_SHARE_PCT
    global MAX_PORTFOLIO_EXPOSURE_PCT, POLL_INTERVAL, LIVE_TRADES_FILE, WALLET_FILE
    global TRAILING_STOP_ENABLED, TRAILING_STOP_ACTIVATION_PCT, TRAILING_STOP_CALLBACK_PCT
    try:
        TRADE_MODE = settings_manager.get("TRADE_MODE")
        ALLOCATION_PCT = float(settings_manager.get("ALLOCATION_PCT"))
        ML_ENGINE_ENABLED = bool(settings_manager.get("ML_ENGINE"))
        MOMENTUM_FILTER_ENABLED = bool(settings_manager.get("MOMENTUM_FILTER_ENABLED"))
        ML_CONFIDENCE = float(settings_manager.get("ML_CONFIDENCE"))
        MIN_TRADE_SOL = float(settings_manager.get("MIN_TRADE_SOL"))
        LIVE_FEE_RESERVE_SOL = float(settings_manager.get("LIVE_FEE_RESERVE_SOL"))
        TAKE_PROFIT_PCT = float(settings_manager.get("TAKE_PROFIT_PCT"))
        STOP_LOSS_PCT = -abs(float(settings_manager.get("STOP_LOSS_PCT")))
        TIMEOUT_ENABLED = bool(settings_manager.get("TIMEOUT_ENABLED"))
        TIMEOUT_MINUTES = int(settings_manager.get("TIMEOUT_MINUTES"))
        MAX_HOLD_SECONDS = TIMEOUT_MINUTES * 60
        MAX_CONCURRENT_TRADES = int(settings_manager.get("MAX_CONCURRENT_TRADES"))
        MIN_24H_VOLUME = float(settings_manager.get("MIN_24H_VOLUME"))
        MIN_MARKET_CAP = float(settings_manager.get("MIN_MARKET_CAP"))
        PAPER_FEE_PCT_PER_LEG = float(settings_manager.get("PAPER_FEE_PCT_PER_LEG"))
        MAX_PRICE_IMPACT_PCT = float(settings_manager.get("MAX_PRICE_IMPACT_PCT"))
        MAX_POOL_SHARE_PCT = float(settings_manager.get("MAX_POOL_SHARE_PCT"))
        MAX_PORTFOLIO_EXPOSURE_PCT = float(settings_manager.get("MAX_PORTFOLIO_EXPOSURE_PCT"))
        TRAILING_STOP_ENABLED = bool(settings_manager.get("TRAILING_STOP_ENABLED"))
        TRAILING_STOP_ACTIVATION_PCT = float(settings_manager.get("TRAILING_STOP_ACTIVATION_PCT"))
        TRAILING_STOP_CALLBACK_PCT = float(settings_manager.get("TRAILING_STOP_CALLBACK_PCT"))
        POLL_INTERVAL = max(2.0, (60 * MAX_CONCURRENT_TRADES) / 280.0)

        if TRADE_MODE == "PAPER":
            LIVE_TRADES_FILE = "paper_trades.json"
            WALLET_FILE = None
        else:
            LIVE_TRADES_FILE = "live_trades.json"
            WALLET_FILE = "live_wallet.json"
    except Exception as e:
        log.warning(f"Failed to refresh settings: {e}")

from position_state import Position, PositionState
from ml_engine import predict_trade
from momentum_filter import check_momentum

# Optional imports for live trading
try:
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction, Transaction as LegacyTransaction
    from solders.pubkey import Pubkey
    from solders.message import Message as LegacyMessage
    from solders.system_program import TransferParams, transfer as sol_transfer
    from solana.rpc.async_api import AsyncClient
    from solana.rpc.types import TokenAccountOpts, TxOpts
    LIVE_TRADING_AVAILABLE = True
except ImportError:
    LIVE_TRADING_AVAILABLE = False

log = logging.getLogger("TradeBrain")
JSON_FILE = "ml_training_data.json"

def fixed_round_trip_cost_sol() -> float:
    """Size-independent SOL cost of one complete buy+sell cycle."""
    per_leg = SOL_BASE_TX_FEE + PRIORITY_FEE_SOL + TIP_FEE_SOL
    cost = 2 * per_leg
    if not ATA_RENT_RECLAIMED:
        cost += ATA_RENT_SOL
    return cost

# ── TRAM Master In-Memory State ──────────────────────────────────────────────

class TradingState:
    """
    Master in-memory state store for high-frequency trading (TRAM Architecture).
    Keeps all active positions, cash balance, and closed trades in volatile RAM.
    Zero disk I/O on hot trading paths.
    """
    def __init__(self):
        self.active: Dict[str, Position] = {}
        self.processing_tokens: set = set()
        self.wallet_balance: float = 0.0
        self.initial_balance: float = 0.0
        self.paper_wallet_version: int = 0
        self.closed_trades: list = []
        self._lock = asyncio.Lock()

    def get_open_exposure_usd(self) -> float:
        return sum(
            pos.trade_size if isinstance(pos, Position) else pos.get("trade_size", pos.get("trade_size_usd", 0.0))
            for pos in self.active.values()
        )

    def add_position(self, token: str, position: Any):
        if isinstance(position, dict):
            position = Position.from_dict(position)
        self.active[token] = position

    def get_position(self, token: str) -> Optional[Position]:
        return self.active.get(token)

    def remove_position(self, token: str) -> Optional[Any]:
        return self.active.pop(token, None)

    def update_position(self, token: str, updates: dict):
        if token in self.active:
            pos = self.active[token]
            if isinstance(pos, Position):
                for k, v in updates.items():
                    if hasattr(pos, k):
                        setattr(pos, k, v)
            else:
                pos.update(updates)

    def debit_balance(self, amount: float) -> bool:
        if self.wallet_balance < amount or amount <= 0:
            return False
        self.wallet_balance -= amount
        return True

    def credit_balance(self, amount: float):
        self.wallet_balance += amount

    def record_closed_trade(self, record: dict):
        self.closed_trades.append(record)

    def snapshot_active(self) -> list:
        return [
            pos.to_dict() if isinstance(pos, Position) else pos
            for pos in self.active.values()
        ]

STATE = TradingState()

# Aliases for backward compatibility
_active_trades = STATE.active
_processing_tokens = STATE.processing_tokens
_dashboard_lock = STATE._lock
_wallet_lock = STATE._lock

_background_tasks = set()
_pending_trades = 0
_dashboard_task = None

if TRADE_MODE == "PAPER":
    LIVE_TRADES_FILE = "paper_trades.json"
    WALLET_FILE = None
else:
    LIVE_TRADES_FILE = "live_trades.json"
    WALLET_FILE = "live_wallet.json"

IGNORE_MINTS = [
    "So11111111111111111111111111111111111111112", # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", # USDT
]

# --- System Recovery ---

async def restore_active_trades():
    """Called on startup to reload active trades into RAM and restart monitor loops."""
    import dateutil.parser
    init_json()
    
    file_to_load = LIVE_TRADES_FILE
    if not os.path.exists(file_to_load):
        log.info(f"No active trades file found ({file_to_load}). Starting fresh.")
        return
    try:
        with open(file_to_load, 'r') as f:
            trades = json.load(f)
            
        if isinstance(trades, dict):
            trades = list(trades.values())

        max_resume_hours = float(settings_manager.get("MAX_RESUME_AGE_HOURS"))
        now_utc = datetime.now(timezone.utc)
            
        for t in trades:
            token = t.get("token_address") or t.get("token")
            if not token: continue

            # Mode safety check: do not resume PAPER trades into a LIVE engine run
            trade_mode_recorded = t.get("mode", "").upper()
            if TRADE_MODE in ["LIVE", "TRUE"] and trade_mode_recorded == "PAPER":
                log.warning(f"Safety gate: Refusing to resume PAPER position {token[:8]} into LIVE trading run.")
                continue

            entry_time_str = t.get("timestamp_entry") or t.get("entry_time")
            try:
                entry_time = dateutil.parser.isoparse(entry_time_str)
            except Exception:
                entry_time = now_utc

            # Age check: drop positions older than MAX_RESUME_AGE_HOURS
            age_hours = (now_utc - entry_time).total_seconds() / 3600.0
            if age_hours > max_resume_hours:
                log.warning(
                    f"Dropping stale orphaned trade {token[:8]} (age {age_hours:.1f}h > {max_resume_hours}h limit)."
                )
                continue
            
            async with STATE._lock:
                STATE.add_position(token, t)

            wallet = t.get("whale_wallet") or t.get("wallet", "")
            entry_price = t.get("entry_price", 0.0)
            trade_size = t.get("trade_size_usd") or t.get("trade_size", 0.0)
            actual_entry_cost_usd = t.get("actual_entry_cost_usd")
            strategy_order_id = t.get("strategy_order_id")
                
            wallet_address = ""
            if LIVE_TRADING_AVAILABLE and WALLET_PRIVATE_KEY:
                try:
                    wallet_address = str(Keypair.from_base58_string(WALLET_PRIVATE_KEY).pubkey())
                except Exception:
                    wallet_address = os.getenv("WALLET_ADDRESS", "")

            if TRADE_MODE in ["LIVE", "TRUE"]:
                task = asyncio.create_task(
                    monitor_gmgn_position(wallet, token, entry_price, entry_time, trade_size, strategy_order_id, wallet_address)
                )
            else:
                task = asyncio.create_task(
                    monitor_position(wallet, token, entry_price, entry_time, trade_size,
                                     actual_entry_cost_usd=actual_entry_cost_usd,
                                     tokens_held=t.get("tokens_held", 0.0),
                                     token_decimals=t.get("token_decimals"))
                )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
            
        log.info(f"Restored {len(STATE.active)} active trades in RAM.")
        
        global _dashboard_task
        if _dashboard_task is None:
            _dashboard_task = asyncio.create_task(_live_dashboard_loop())
            
    except Exception as e:
        log.error(f"Failed to restore active trades: {e}")


async def flush_all_state_now():
    """Immediately flushes all in-memory state to disk on bot shutdown."""
    log.info("Flushing all TRAM state to disk for clean shutdown...")
    try:
        async with STATE._lock:
            trades_list = STATE.snapshot_active()
            wallet_bal = STATE.wallet_balance
            wallet_init = STATE.initial_balance
            pending_closed = STATE.closed_trades[:]
            STATE.closed_trades.clear()

        _save_json_atomic(LIVE_TRADES_FILE, trades_list)
        if TRADE_MODE == "PAPER":
            settings_manager.set_paper_wallet_balance(wallet_bal)
        elif WALLET_FILE:
            _save_json_atomic(WALLET_FILE, {"balance": wallet_bal, "initial": wallet_init})

        if pending_closed:
            try:
                with open("ml_training_data.jsonl", "a", encoding="utf-8") as f:
                    for r in pending_closed:
                        f.write(json.dumps(r) + "\n")
            except Exception as ne:
                log.error(f"Failed to append to ml_training_data.jsonl on shutdown: {ne}")

            try:
                existing = []
                if os.path.exists(JSON_FILE):
                    with open(JSON_FILE, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                existing.extend(pending_closed)
                _save_json_atomic(JSON_FILE, existing)
            except Exception as je:
                log.error(f"Failed to mirror to {JSON_FILE} on shutdown: {je}")

        log.info("Shutdown flush completed successfully.")
    except Exception as e:
        log.error(f"Error during shutdown flush: {e}")

# --- Live Trading Core Functions ---


async def get_sol_balance() -> int:
    """Gets the native SOL balance of the wallet in lamports."""
    if not LIVE_TRADING_AVAILABLE or not WALLET_PRIVATE_KEY:
        return 0
    try:
        keypair = Keypair.from_base58_string(WALLET_PRIVATE_KEY)
        wallet_pubkey = keypair.pubkey()
        solana_client = AsyncClient(SOLANA_RPC_URL)
        resp = await solana_client.get_balance(wallet_pubkey)
        return resp.value
    except Exception as e:
        log.error(f"Error fetching SOL balance: {e}")
        return 0

async def get_spl_token_balance(wallet_pubkey_str: str, token_mint_str: str) -> float:
    """Returns the token balance for a wallet and mint address. Returns 0.0 if balance is 0 or missing."""
    if not LIVE_TRADING_AVAILABLE or not wallet_pubkey_str or not token_mint_str:
        return -1.0
    try:
        solana_client = AsyncClient(SOLANA_RPC_URL)
        resp = await solana_client.get_token_accounts_by_owner_json_parsed(
            Pubkey.from_string(wallet_pubkey_str),
            TokenAccountOpts(mint=Pubkey.from_string(token_mint_str))
        )
        accounts = resp.value
        if not accounts:
            return 0.0
        amount = 0.0
        for acc in accounts:
            info = acc.account.data.parsed.get("info", {})
            token_amount = info.get("tokenAmount", {})
            amount += float(token_amount.get("uiAmount") or 0.0)
        return amount
    except Exception as e:
        log.debug(f"Error checking SPL token balance: {e}")
        return -1.0

# ─────────────────────────────────────────────────────────────────────────────
# GMGN Live Trade Management (LIVE mode only)
# ─────────────────────────────────────────────────────────────────────────────

async def close_gmgn_trade(
    wallet: str,
    token: str,
    entry_time: datetime,
    entry_price: float,
    exit_price: float,
    trade_size: float,
    reason: str,
    real_profit_usd=None,
):
    """
    Record a GMGN-executed live trade close to the ML dataset and send Telegram exit alert.
    Called by monitor_gmgn_position when the strategy fires or a timeout sell completes.
    """
    hold_duration = (datetime.now(timezone.utc) - entry_time).total_seconds()
    
    fill_available = real_profit_usd is not None
    if fill_available:
        net_profit_usd = float(real_profit_usd)
        net_profit_pct = (net_profit_usd / trade_size) * 100.0 if trade_size > 0 else 0.0
        real_pct = round(net_profit_pct, 2)
    else:
        # Deduct 1.0% for realistic DEX swap fee & slippage if raw price was used
        net_profit_pct = (((exit_price - entry_price) / entry_price) * 100) - 1.0 if entry_price > 0 else 0.0
        net_profit_usd = trade_size * (net_profit_pct / 100.0)
        real_pct = None

    # Remove from the live dashboard
    async with _dashboard_lock:
        pos = STATE.get_position(token)
        if pos and isinstance(pos, Position):
            pos.mark_closed(exit_price, reason)
        _active_trades.pop(token, None)

    if fill_available:
        log.info(
            f"GMGN close {token[:8]} → {reason} | "
            f"REAL GMGN FILL: ${real_profit_usd:.2f} ({real_pct:.2f}%)"
        )
    else:
        log.info(f"GMGN close {token[:8]} → {reason} | Quoted: {net_profit_pct:.2f}% (${net_profit_usd:.2f})")

    trade_record = {
        "timestamp_entry": entry_time.isoformat(),
        "whale_wallet": wallet,
        "token_address": token,
        "entry_usd_price": entry_price,
        "exit_usd_price": exit_price,
        "trade_size_usd": round(trade_size, 2),
        "hold_duration_seconds": int(hold_duration),
        "net_profit_percent": round(net_profit_pct, 2),
        "net_profit_usd": round(net_profit_usd, 4),
        "max_profit_percent": 0.0,   # not tracked in GMGN strategy mode
        "exit_reason": reason,
        "trade_mode": TRADE_MODE,
        "real_entry_cost_usd": None,
        "real_exit_proceeds_usd": None,
        "real_net_profit_usd": round(real_profit_usd, 4) if fill_available else None,
        "real_net_profit_percent": real_pct,
        "fill_data_available": fill_available,
        "execution_engine": "GMGN",
    }

    # Record in RAM (flushed asynchronously by state flush loop)
    STATE.record_closed_trade(trade_record)
    send_exit_alert(trade_record)


async def monitor_gmgn_position(
    wallet: str,
    token: str,
    entry_price: float,
    entry_time: datetime,
    trade_size: float,
    strategy_order_id,   # str | None
    wallet_address: str,
):
    """
    Monitor a live GMGN position by polling the strategy (TP/SL) order status.
    """
    log.info(
        f"GMGN MONITOR: {token[:8]}... | strategy={strategy_order_id or 'N/A (fallback mode)'}"
    )

    # ── Fallback: no strategy_order_id → price-polling + GMGN sell ───────────
    if not strategy_order_id:
        log.warning(
            f"GMGN: No strategy_order_id for {token[:8]}. "
            f"Falling back to price-polling. Sell will use GMGN sell-all (NOT Jupiter)."
        )
        last_price = entry_price
        max_profit = 0.0
        last_balance_check = 0.0
        import time
        while True:
            await asyncio.sleep(POLL_INTERVAL + random.uniform(0.1, 1.5))
            elapsed = (datetime.now(timezone.utc) - entry_time).total_seconds()

            # Check if GMGN natively executed the Stop-Loss / Take-Profit on-chain (balance = 0)
            if elapsed > 8.0 and (time.time() - last_balance_check > 10.0) and wallet_address:
                last_balance_check = time.time()
                bal = await get_spl_token_balance(wallet_address, token)
                if bal == 0.0:
                    # GMGN closed this on its own, but the balance going to zero does not say
                    # WHICH side fired. Labelling it STOP_LOSS unconditionally (as this used to)
                    # mislabels take-profits as losses and poisons every win-rate stat and any
                    # future ML training set. Infer from the last observed price instead, and
                    # mark it as inferred so it can never be mistaken for a confirmed reason.
                    if entry_price > 0 and last_price > 0:
                        move_pct = ((last_price - entry_price) / entry_price) * 100
                        inferred = "TAKE_PROFIT" if move_pct >= 0 else "STOP_LOSS"
                    else:
                        inferred = "GMGN_CLOSED"
                    log.info(
                        f"GMGN: On-chain token balance for {token[:8]} is 0 — GMGN closed the position. "
                        f"Reason inferred from last price as {inferred}."
                    )
                    async with _dashboard_lock:
                        _active_trades.pop(token, None)
                    await close_gmgn_trade(
                        wallet, token, entry_time, entry_price, last_price,
                        trade_size, f"{inferred}_INFERRED", None
                    )
                    break

            exit_reason = None
            exit_price = last_price

            if TIMEOUT_ENABLED and elapsed >= MAX_HOLD_SECONDS:
                exit_reason = "TIMEOUT"
            else:
                current_price = await get_live_price(token)
                if current_price > 0:
                    last_price = current_price
                    profit_pct = ((current_price - entry_price) / entry_price) * 100
                    if profit_pct > max_profit:
                        max_profit = profit_pct
                    async with _dashboard_lock:
                        if token in _active_trades:
                            _active_trades[token]["current_price"] = current_price
                            _active_trades[token]["profit_pct"] = profit_pct
                            _active_trades[token]["max_profit"] = max_profit
                            _active_trades[token]["elapsed"] = int(elapsed)
                    if profit_pct >= TAKE_PROFIT_PCT:
                        exit_reason = "TAKE_PROFIT"
                        exit_price = current_price
                    elif profit_pct <= STOP_LOSS_PCT:
                        exit_reason = "STOP_LOSS"
                        exit_price = current_price

            if exit_reason:
                log.info(f"GMGN fallback: {exit_reason} for {token[:8]}. Selling via GMGN...")
                sell_result = await execute_gmgn_sell_all(wallet_address, token)
                real_profit = None
                if sell_result.get("confirmed"):
                    report = sell_result.get("report", {})
                    price_str = report.get("price_usd", "")
                    if price_str:
                        try:
                            exit_price = float(price_str)
                        except ValueError:
                            pass
                    profit_str = report.get("realized_profit") or report.get("usdt_profit") or report.get("profit_usd")
                    if profit_str:
                        try:
                            real_profit = float(profit_str)
                        except ValueError:
                            pass
                    if real_profit is None and exit_price > 0 and entry_price > 0:
                        real_profit = trade_size * ((exit_price - entry_price) / entry_price)

                await close_gmgn_trade(
                    wallet, token, entry_time, entry_price, exit_price,
                    trade_size, exit_reason, real_profit
                )
                break
        return

    # ── Strategy mode: poll GMGN every 30 seconds ─────────────────────────────
    last_gmgn_poll = 0
    max_profit = 0.0
    
    while True:
        await asyncio.sleep(POLL_INTERVAL + random.uniform(0.1, 1.5))
        
        current_time = datetime.now(timezone.utc)
        elapsed = (current_time - entry_time).total_seconds()
        
        # 1. Update the UI Dashboard frequently
        current_price = await get_live_price(token)
        if current_price > 0:
            profit_pct = ((current_price - entry_price) / entry_price) * 100
            profit_usd = trade_size * (profit_pct / 100)
            if profit_pct > max_profit:
                max_profit = profit_pct
                
            async with _dashboard_lock:
                if token in _active_trades:
                    _active_trades[token]["current_price"] = current_price
                    _active_trades[token]["profit_pct"] = profit_pct
                    _active_trades[token]["profit_usd"] = profit_usd
                    _active_trades[token]["max_profit"] = max_profit
                    _active_trades[token]["elapsed"] = int(elapsed)

        # Timeout check
        if TIMEOUT_ENABLED and elapsed >= MAX_HOLD_SECONDS:
            log.info(f"GMGN: Timeout for {token[:8]}. Cancelling strategy + force selling...")
            await cancel_strategy_order(wallet_address, strategy_order_id)
            sell_result = await execute_gmgn_sell_all(wallet_address, token)

            exit_price = entry_price
            real_profit = None
            if sell_result.get("confirmed"):
                report = sell_result.get("report", {})
                price_str = report.get("price_usd", "")
                if price_str:
                    try:
                        exit_price = float(price_str)
                    except ValueError:
                        pass

            async with _dashboard_lock:
                _active_trades.pop(token, None)
            await close_gmgn_trade(
                wallet, token, entry_time, entry_price, exit_price,
                trade_size, "TIMEOUT", real_profit
            )
            break

        # Panic sell check (triggered by Telegram UI → gmgn_panic.json)
        if os.path.exists("gmgn_panic.json"):
            try:
                with open("gmgn_panic.json", "r") as f:
                    panic_data = json.load(f)
                if panic_data.get("panic_timestamp", 0) > entry_time.timestamp():
                    log.warning(f"GMGN: PANIC SELL triggered for {token[:8]}!")
                    await cancel_strategy_order(wallet_address, strategy_order_id)
                    sell_result = await execute_gmgn_sell_all(wallet_address, token)
                    exit_price = entry_price
                    real_profit = None
                    if sell_result.get("confirmed"):
                        report = sell_result.get("report", {})
                        price_str = report.get("price_usd", "")
                        if price_str:
                            try:
                                exit_price = float(price_str)
                            except ValueError:
                                pass
                    async with _dashboard_lock:
                        _active_trades.pop(token, None)
                    await close_gmgn_trade(
                        wallet, token, entry_time, entry_price, exit_price,
                        trade_size, "PANIC_SELL", real_profit
                    )
                    break
            except Exception as e:
                log.warning(f"GMGN: Failed to read gmgn_panic.json: {e}")

        # Fallback Python price-polling when strategy_order_id is missing or POLLING_FALLBACK
        if not strategy_order_id or "POLLING" in str(strategy_order_id):
            current_price = await get_live_price(token)
            if current_price > 0 and entry_price > 0:
                profit_pct = ((current_price - entry_price) / entry_price) * 100
                profit_usd = trade_size * (profit_pct / 100.0)
                
                async with _dashboard_lock:
                    if token in _active_trades:
                        _active_trades[token]["current_price"] = current_price
                        _active_trades[token]["profit_pct"] = profit_pct
                        _active_trades[token]["profit_usd"] = profit_usd
                        _active_trades[token]["elapsed"] = int(elapsed)

                if profit_pct >= TAKE_PROFIT_PCT:
                    log.info(f"GMGN POLLING FALLBACK: Take Profit (+{profit_pct:.1f}%) hit for {token[:8]}! Executing sell...")
                    sell_res = await execute_gmgn_sell_all(wallet_address, token)
                    exit_price = current_price
                    real_pnl = None
                    if sell_res.get("confirmed"):
                        report = sell_res.get("report", {})
                        p_str = report.get("price_usd", "")
                        if p_str:
                            try: exit_price = float(p_str)
                            except: pass
                        real_pnl = report.get("realized_profit") or report.get("usdt_profit")
                        if real_pnl:
                            try: real_pnl = float(real_pnl)
                            except: real_pnl = None
                    async with _dashboard_lock:
                        _active_trades.pop(token, None)
                    await close_gmgn_trade(wallet, token, entry_time, entry_price, exit_price, trade_size, "TAKE_PROFIT", real_pnl)
                    break
                elif profit_pct <= STOP_LOSS_PCT:
                    log.info(f"GMGN POLLING FALLBACK: Stop Loss ({profit_pct:.1f}%) hit for {token[:8]}! Executing sell...")
                    sell_res = await execute_gmgn_sell_all(wallet_address, token)
                    exit_price = current_price
                    real_pnl = None
                    if sell_res.get("confirmed"):
                        report = sell_res.get("report", {})
                        p_str = report.get("price_usd", "")
                        if p_str:
                            try: exit_price = float(p_str)
                            except: pass
                        real_pnl = report.get("realized_profit") or report.get("usdt_profit")
                        if real_pnl:
                            try: real_pnl = float(real_pnl)
                            except: real_pnl = None
                    async with _dashboard_lock:
                        _active_trades.pop(token, None)
                    await close_gmgn_trade(wallet, token, entry_time, entry_price, exit_price, trade_size, "STOP_LOSS", real_pnl)
                    break
            continue

        # Poll strategy status every 30s to avoid rate limits
        if current_time.timestamp() - last_gmgn_poll >= 30.0:
            last_gmgn_poll = current_time.timestamp()
            try:
                strategy = await get_strategy_status(strategy_order_id, token)
            except Exception as e:
                log.warning(f"GMGN: Error polling strategy {strategy_order_id}: {e}. Retrying next cycle.")
                continue
    
            if strategy is None:
                log.debug(f"GMGN: Strategy {strategy_order_id[:8]} not found yet. Elapsed {elapsed:.0f}s.")
                continue
    
            status = strategy.get("status", "")
    
            if status == "closed":
                # Strategy executed — extract exit data
                close_price_str = strategy.get("close_price", "")
                exit_price = float(close_price_str) if close_price_str else entry_price
    
                reason_code = strategy.get("reason_code", "").lower()
                if "profit" in reason_code:
                    exit_reason = "TAKE_PROFIT"
                elif "loss" in reason_code:
                    exit_reason = "STOP_LOSS"
                else:
                    exit_reason = reason_code.upper() or "GMGN_CLOSED"
    
                # Real P&L from GMGN order_statistic
                stat = strategy.get("order_statistic", {})
                profit_str = stat.get("usdt_profit", "")
                real_profit_usd = float(profit_str) if profit_str else None
    
                log.info(
                    f"GMGN: Strategy {strategy_order_id[:8]} CLOSED → {exit_reason} | "
                    f"exit_price={exit_price} | real_profit=${real_profit_usd}"
                )
    
                await close_gmgn_trade(
                    wallet, token, entry_time, entry_price, exit_price,
                    trade_size, exit_reason, real_profit_usd
                )
                break
    
            log.debug(
                f"GMGN strategy {strategy_order_id[:8]} for {token[:8]} → {status} | "
                f"elapsed {elapsed:.0f}s"
            )


# --- Core ML Data Engine ---

import tempfile

def _save_json_atomic(file_path: str, data: Any):
    """Safely saves data to JSON file using an atomic replace to prevent 24/7 race conditions."""
    try:
        dir_name = os.path.dirname(os.path.abspath(file_path)) or "."
        temp_fd, temp_path = tempfile.mkstemp(dir=dir_name)
        with os.fdopen(temp_fd, "w") as f:
            json.dump(data, f, indent=4)
        os.replace(temp_path, file_path)
    except Exception as e:
        log.error(f"Failed to atomically save {file_path}: {e}")

def init_json():
    """Initializes in-memory wallet balance from disk or settings, and ensures files exist."""
    _refresh_settings()
    for file_path in [JSON_FILE, LIVE_TRADES_FILE]:
        if not os.path.exists(file_path):
            _save_json_atomic(file_path, [])

    if TRADE_MODE == "PAPER":
        legacy_paper_file = "paper_wallet.json"
        if os.path.exists(legacy_paper_file):
            try:
                with open(legacy_paper_file, "r") as f:
                    legacy_data = json.load(f)
                bal = float(legacy_data.get("balance", 0.0))
                if bal > 0:
                    settings_manager.set_paper_wallet_balance(bal)
                os.remove(legacy_paper_file)
                log.info(f"Migrated legacy {legacy_paper_file} into settings_manager and deleted file.")
            except Exception as e:
                log.debug(f"Could not migrate legacy {legacy_paper_file}: {e}")

        init_bal = float(settings_manager.get("PAPER_BALANCE_USD"))
        current_bal = float(settings_manager.get("PAPER_WALLET_BALANCE"))
        if current_bal <= 0.0:
            current_bal = init_bal
            settings_manager.set_paper_wallet_balance(current_bal)
        STATE.initial_balance = init_bal
        STATE.wallet_balance = current_bal
        STATE.paper_wallet_version = settings_manager.get_paper_wallet_version()
    else:
        if WALLET_FILE and os.path.exists(WALLET_FILE):
            try:
                with open(WALLET_FILE, "r") as f:
                    wdata = json.load(f)
                STATE.wallet_balance = float(wdata.get("balance", 0.0))
                STATE.initial_balance = float(wdata.get("initial", STATE.wallet_balance))
            except Exception as e:
                log.error(f"Failed to load wallet file {WALLET_FILE}: {e}")

async def _live_dashboard_loop():
    """
    TRAM Periodic State Flush:
    Flushes RAM state to disk snapshots every 2s for UI (Telegram / Web) and crash recovery.
    Appends completed trades to ml_training_data.jsonl without blocking the trading loop.
    """
    while True:
        try:
            await asyncio.sleep(2)
            _refresh_settings()
            
            # 0. Sync external paper wallet updates if any occurred outside trade_brain
            if TRADE_MODE == "PAPER":
                cur_version = settings_manager.get_paper_wallet_version()
                cfg_init = float(settings_manager.get("PAPER_BALANCE_USD"))
                last_ver = getattr(STATE, "paper_wallet_version", 0)
                if cur_version != last_ver or STATE.initial_balance != cfg_init:
                    async with STATE._lock:
                        external_bal = float(settings_manager.get("PAPER_WALLET_BALANCE"))
                        if STATE.active and STATE.initial_balance > 0:
                            delta = cfg_init - STATE.initial_balance
                            STATE.wallet_balance = max(0.0, STATE.wallet_balance + delta)
                        else:
                            STATE.wallet_balance = external_bal
                        STATE.initial_balance = cfg_init
                        STATE.paper_wallet_version = cur_version
                    log.info(f"🔄 Synced external paper wallet update: initial=${cfg_init:.2f}, balance=${STATE.wallet_balance:.2f} (v{cur_version})")

            # 1. Flush active positions to disk for Web Dashboard / Telegram Bot
            async with STATE._lock:
                trades_list = STATE.snapshot_active()
                wallet_bal = STATE.wallet_balance
                wallet_init = STATE.initial_balance
                pending_closed = STATE.closed_trades[:]
                STATE.closed_trades.clear()

            _save_json_atomic(LIVE_TRADES_FILE, trades_list)

            # 2. Flush wallet state
            if TRADE_MODE == "PAPER":
                settings_manager.set_paper_wallet_balance(wallet_bal)
            elif WALLET_FILE:
                _save_json_atomic(WALLET_FILE, {"balance": wallet_bal, "initial": wallet_init})

            # 3. Flush completed trades to append-only NDJSON and mirror to JSON
            if pending_closed:
                # Append to NDJSON (crash-proof, zero read-modify-write)
                try:
                    with open("ml_training_data.jsonl", "a", encoding="utf-8") as f:
                        for record in pending_closed:
                            f.write(json.dumps(record) + "\n")
                except Exception as ne:
                    log.error(f"Failed to append to ml_training_data.jsonl: {ne}")

                # Mirror to JSON file for backward compatibility with external tools
                try:
                    existing = []
                    if os.path.exists(JSON_FILE):
                        with open(JSON_FILE, "r", encoding="utf-8") as f:
                            existing = json.load(f)
                    existing.extend(pending_closed)
                    _save_json_atomic(JSON_FILE, existing)
                except Exception as je:
                    log.error(f"Failed to mirror to {JSON_FILE}: {je}")

        except Exception as e:
            log.error(f"Error in state flush loop: {e}")

async def close_trade(wallet, token, entry_time, entry_price, exit_price, max_profit, reason, trade_size,
                      actual_entry_cost_usd=None, tokens_held: float = 0.0, token_decimals=None):
    """
    Writes the completed simulated trade to the ML dataset and updates wallet.
    Returns True if the trade was actually closed (position exited / books settled),
    False if a live sell attempt failed and the position is still open on-chain.
    Callers MUST check the return value: on False, the trade is still live and
    must keep being monitored, not treated as closed.

    `exit_price` is only the TRIGGER price (a DexScreener mid). The realised fill is
    re-derived here by quoting the actual position through a real route, so a position too
    large for its pool books the loss it would really take instead of filling at mid.
    """
    hold_duration = (datetime.now(timezone.utc) - entry_time).total_seconds()

    real_net_profit_usd = None
    real_net_profit_pct = None
    real_exit_proceeds_usd = None
    fill_data_available = False

    current_mode = os.getenv("TRADE_MODE", "PAPER").strip().strip('"\'').upper()
    if current_mode in ["LIVE", "TRUE"]:
        log.error(
            f"close_trade() called for LIVE trade {token[:8]}. "
            f"Live trades must be closed via close_gmgn_trade() or monitor_gmgn_position(). "
            f"This is a bug — not recording to avoid duplicate ML entries."
        )
        return False

    # ── PAPER MODE ────────────────────────────────────────────────────────────
    sol_price = await get_sol_price_usd()
    if sol_price <= 0:
        sol_price = 150.0

    # What the position would ACTUALLY fetch right now, at its real size.
    # 0.0 means no sell route exists — the position is stuck, not worthless-by-price.
    executable_exit_price = 0.0
    if tokens_held > 0 and token_decimals is not None:
        executable_exit_price = await get_executable_price_usd(
            token, tokens_held, token_decimals, sol_price
        )

    fixed_cost_sol = fixed_round_trip_cost_sol()
    fixed_cost_usd = fixed_cost_sol * sol_price
    fixed_cost_pct = (fixed_cost_usd / trade_size * 100) if trade_size > 0 else 0.0

    if executable_exit_price <= 0:
        log.warning(
            f"PAPER EXIT: no sell route for {tokens_held:.4f} of {token[:8]} "
            f"(size ${trade_size:.2f}). Position is unsellable — booking -100%."
        )
        net_profit_pct = -100.0
        net_profit_usd = -trade_size
        exit_price = 0.0
        reason = reason + "_UNSELLABLE"
    else:
        # Gross move measured entry-fill -> exit-fill, both size-aware and impact-inclusive.
        exit_price = executable_exit_price
        gross_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0.0

        # Proportional fees on both legs (the entry leg is charged here rather than at open
        # so a single record shows the complete cost of the round trip).
        proportional_pct = 2 * PAPER_FEE_PCT_PER_LEG

        # Fixed lamport costs, converted to a percentage OF THIS TRADE. On a $2 trade an
        # unreclaimed ATA rent alone is ~7.7%; on a $200 trade it is ~0.08%. Charging it as
        # a flat percentage would hide exactly the effect that matters to a small account.
        net_profit_usd = trade_size * ((gross_pct - proportional_pct) / 100) - fixed_cost_usd
        net_profit_pct = (net_profit_usd / trade_size * 100) if trade_size > 0 else 0.0
        real_exit_proceeds_usd = trade_size + net_profit_usd

        if fixed_cost_pct > 2.0:
            log.warning(
                f"Fixed costs are {fixed_cost_pct:.2f}% of this ${trade_size:.2f} trade "
                f"({fixed_cost_sol:.5f} SOL{'' if ATA_RENT_RECLAIMED else ', incl. unreclaimed ATA rent'}). "
                f"Trade size is too small for the fee structure — the strategy needs to clear "
                f"{proportional_pct + fixed_cost_pct:.1f}% per round trip just to break even."
            )

    trade_record = {
        "timestamp_entry": entry_time.isoformat(),
        "whale_wallet": wallet,
        "token_address": token,
        "entry_usd_price": entry_price,
        "exit_usd_price": exit_price,
        "trade_size_usd": round(trade_size, 2),
        "hold_duration_seconds": int(hold_duration),
        "net_profit_percent": round(net_profit_pct, 2),
        "net_profit_usd": round(net_profit_usd, 4),
        "max_profit_percent": round(max_profit, 2),
        "exit_reason": reason,
        "trade_mode": current_mode,
        "real_entry_cost_usd": round(actual_entry_cost_usd, 4) if actual_entry_cost_usd is not None else None,
        "real_exit_proceeds_usd": round(real_exit_proceeds_usd, 4) if real_exit_proceeds_usd is not None else None,
        "real_net_profit_usd": round(real_net_profit_usd, 4) if real_net_profit_usd is not None else None,
        "real_net_profit_percent": round(real_net_profit_pct, 2) if real_net_profit_pct is not None else None,
        "fill_data_available": fill_data_available,
        "pricing_source": "jupiter_executable_quote",
        "tokens_held": tokens_held,
        "token_decimals": token_decimals,
        "fee_pct_per_leg": PAPER_FEE_PCT_PER_LEG,
        "fixed_cost_usd": round(fixed_cost_usd, 6),
        "fixed_cost_pct_of_trade": round(fixed_cost_pct, 3),
        "ata_rent_assumed_reclaimed": ATA_RENT_RECLAIMED,
    }

    # TRAM In-Memory Close: credit balance & pop position in RAM
    async with STATE._lock:
        STATE.credit_balance(trade_size + net_profit_usd)
        if token in _active_trades:
            del _active_trades[token]
        STATE.record_closed_trade(trade_record)

    if fill_data_available:
        log.info(
            f"Closing trade for {token[:8]} (Wallet: {wallet[:8]}) -> {reason}. "
            f"Quoted: {net_profit_pct:.2f}% (${net_profit_usd:.2f}) | "
            f"REAL: {real_net_profit_pct:.2f}% (${real_net_profit_usd:.2f})"
        )
    else:
        log.info(f"Closing trade for {token[:8]} (Wallet: {wallet[:8]}) -> {reason}. Quoted: {net_profit_pct:.2f}% (${net_profit_usd:.2f}) [no real fill data]")

    send_exit_alert(trade_record)
    return True


async def monitor_position(wallet: str, token: str, entry_price: float, entry_time: datetime, trade_size: float,
                           actual_entry_cost_usd=None, tokens_held: float = 0.0, token_decimals=None):
    """
    Background loop that polls DexScreener to simulate holding a position.

    DexScreener mid prices drive the TP/SL *trigger* only — they're cheap enough to poll
    every couple of seconds. The realised fill is priced inside close_trade() off a real
    sell-side route for `tokens_held`, so exits carry their true price impact.
    """
    log.info(
        f"Started monitoring position: {token} from entry {entry_price:.8f} "
        f"with ${trade_size:.2f} ({tokens_held:.4f} tokens)"
    )
    max_profit = 0.0
    last_valid_price = entry_price
    failed_api_calls = 0
    stagnant_loops = 0
    close_retry_count = 0
    MAX_CLOSE_RETRIES = 15  # ~ a few minutes of retrying at POLL_INTERVAL before giving up automated retries

    async def _attempt_close(reason: str, exit_price_val: float) -> bool:
        """
        Wraps close_trade(). Returns True if the caller should stop monitoring
        (either the trade genuinely closed, or we've given up after repeated
        live-sell failures). Returns False if the caller should keep polling
        and retry the close next loop.
        """
        nonlocal close_retry_count
        pos = STATE.get_position(token)
        if pos and isinstance(pos, Position):
            if not pos.begin_exit(reason):
                log.info(f"Exit already in progress for {token[:8]} ({pos.state.value}). Skipping duplicate attempt.")
                return False

        closed = await close_trade(wallet, token, entry_time, entry_price, exit_price_val, max_profit, reason, trade_size,
                                   actual_entry_cost_usd=actual_entry_cost_usd,
                                   tokens_held=tokens_held, token_decimals=token_decimals)
        if closed:
            if pos and isinstance(pos, Position):
                pos.mark_closed(exit_price_val, reason)
            return True

        if pos and isinstance(pos, Position):
            pos.revert_exit()

        close_retry_count += 1
        if close_retry_count >= MAX_CLOSE_RETRIES:
            log.critical(
                f"LIVE SELL FAILED {close_retry_count}x for {token}. Giving up automated retries — "
                f"wallet likely still holds this position. MANUAL INTERVENTION REQUIRED."
            )
            send_error_alert(
                f"🚨 CRITICAL: Could not sell {token[:8]}... after {close_retry_count} attempts.\n"
                f"Wallet: {wallet[:8]}...\n"
                f"The bot is giving up automated retries. This position is likely still held on-chain "
                f"and was NOT recorded in the ML dataset. Please check manually."
            )
            async with _dashboard_lock:
                _active_trades.pop(token, None)
            return True  # stop the monitor loop even though we couldn't confirm a real close

        return False

    while True:
        await asyncio.sleep(POLL_INTERVAL + random.uniform(0.1, 1.5))
        current_time = datetime.now(timezone.utc)
        elapsed = (current_time - entry_time).total_seconds()
        
        if TIMEOUT_ENABLED and elapsed >= MAX_HOLD_SECONDS:
            if await _attempt_close("TIMEOUT", last_valid_price):
                break
            continue
            
        current_price = await get_live_price(token)
        if current_price <= 0:
            failed_api_calls += 1
            # If API fails for 2.5 minutes straight (30 loops), assume rug pull / dead token
            if failed_api_calls >= 30:
                if await _attempt_close("DEAD_TOKEN_API", 0.0):
                    break
                continue
            continue
            
        failed_api_calls = 0
        
        # --- PANIC SELL CHECK ---
        if os.path.exists("panic.json"):
            try:
                with open("panic.json", "r") as f:
                    panic_data = json.load(f)
                if panic_data.get("panic_timestamp", 0) > entry_time.timestamp():
                    log.warning(f"🚨 PANIC SELL TRIGGERED FOR {token}!")
                    if await _attempt_close("PANIC_SELL", current_price):
                        break
                    continue
            except Exception as e:
                log.error(f"Failed to read panic.json: {e}")
                
        if current_price == last_valid_price:
            stagnant_loops += 1
            # If price doesn't change for 10 minutes (120 loops), exit due to dead volume
            if stagnant_loops >= 120:
                if await _attempt_close("DEAD_VOLUME", current_price):
                    break
                continue
        else:
            stagnant_loops = 0
            
        last_valid_price = current_price
            
        pos = STATE.get_position(token)
        should_exit = False
        exit_trigger = ""
        if pos and isinstance(pos, Position):
            pos.elapsed = int(elapsed)
            should_exit, exit_trigger = pos.update_price(
                current_price,
                TAKE_PROFIT_PCT,
                STOP_LOSS_PCT,
                trailing_stop_enabled=TRAILING_STOP_ENABLED,
                trailing_activation_pct=TRAILING_STOP_ACTIVATION_PCT,
                trailing_callback_pct=TRAILING_STOP_CALLBACK_PCT,
            )
            max_profit = pos.max_profit
            profit_pct = pos.profit_pct
            profit_usd = pos.profit_usd
        else:
            profit_pct = ((current_price - entry_price) / entry_price) * 100
            profit_usd = trade_size * (profit_pct / 100)
            if profit_pct > max_profit:
                max_profit = profit_pct
            
        async with _dashboard_lock:
            if token in _active_trades:
                _active_trades[token]["current_price"] = current_price
                _active_trades[token]["max_profit"] = max_profit
                _active_trades[token]["profit_pct"] = profit_pct
                _active_trades[token]["profit_usd"] = profit_usd
                _active_trades[token]["elapsed"] = int(elapsed)
            
        if should_exit:
            if await _attempt_close(exit_trigger, current_price):
                break
            continue

        if profit_pct >= TAKE_PROFIT_PCT:
            if await _attempt_close("TAKE_PROFIT", current_price):
                break
            continue
            
        if profit_pct <= STOP_LOSS_PCT:
            if await _attempt_close("STOP_LOSS", current_price):
                break
            continue

async def record_trade(trade_data: dict):
    """Called by main.py when a decoded trade is found."""
    global _dashboard_task, _pending_trades
    init_json()
    if _dashboard_task is None:
        _dashboard_task = asyncio.create_task(_live_dashboard_loop())
    
    wallet = trade_data.get("wallet", "Unknown")
    import whale_manager
    status = whale_manager.get_whale_status(wallet)
    
    if status in ["BLACKLIST", "NEUTRAL"]:
        log.info(f"Ignoring trade from {status} whale: {wallet[:8]}... Bot only trades for WHITELIST.")
        return
        
    if (len(_background_tasks) + _pending_trades) >= MAX_CONCURRENT_TRADES:
        log.warning(f"Max concurrent trades reached ({MAX_CONCURRENT_TRADES}). Skipping.")
        return
    
    bought_list = trade_data.get("bought", [])
    
    if not bought_list:
        return
        
    for item in bought_list:
        if (len(_background_tasks) + _pending_trades) >= MAX_CONCURRENT_TRADES:
            log.warning(f"Max concurrent trades reached ({MAX_CONCURRENT_TRADES}). Skipping remaining tokens in batch.")
            break
            
        token_address = item.get("mint")
        if token_address in IGNORE_MINTS:
            log.info(f"Skipping {token_address} - Matched stablecoin/WSOL keyword.")
            continue
            
        if token_address in _processing_tokens:
            log.info(f"Token {token_address[:8]} is already being processed. Skipping duplicate API event.")
            continue
            
        async with _dashboard_lock:
            if token_address in _active_trades:
                log.info(f"Token {token_address[:8]} is already being tracked. Skipping.")
                continue

        _processing_tokens.add(token_address)
        _pending_trades += 1
        try:
            entry_price, symbol, name, volume_24h, market_cap = await get_token_price_usd(token_address)
            symbol_upper = symbol.upper()
            if any(keyword in symbol_upper for keyword in ["USD", "BTC", "ETH", "WRAPPED", "EUR", "STABLE"]):
                log.info(f"Skipping {token_address} ({symbol}) - Matched stablecoin/wrapped keyword.")
                continue
                
            if entry_price > 0:
                if volume_24h < MIN_24H_VOLUME:
                    log.warning(f"Trade Rejected: {token_address[:8]} has 24h volume (${volume_24h:,.0f}) < MIN (${MIN_24H_VOLUME:,.0f})")
                    continue
                    
                if market_cap < MIN_MARKET_CAP:
                    log.warning(f"Trade Rejected: {token_address[:8]} has Market Cap (${market_cap:,.0f}) < MIN (${MIN_MARKET_CAP:,.0f})")
                    continue

                # Real decimals are required to size any quote correctly. Refusing the trade
                # is the safe failure: guessing 6 decimals on a 9-decimal token mis-sizes every
                # downstream route by 1000x and silently defeats the exit-liquidity check.
                token_decimals = await get_token_decimals(token_address)
                if token_decimals is None:
                    log.warning(
                        f"Trade Rejected: could not resolve decimals for {token_address[:8]}. "
                        f"Refusing to size a trade on a guessed value."
                    )
                    continue

                # Check for Multi-Whale Consensus Signal (2+ whitelisted whales bought same token)
                #
                # Snapshot under the lock. Iterating _active_trades.values() directly races with
                # the monitor tasks that add/remove positions, and "dictionary changed size
                # during iteration" here propagates out of record_trade (there is no except on
                # the enclosing try) and silently kills the whole signal.
                is_consensus = False
                recent_whales = {wallet}
                async with _dashboard_lock:
                    active_snapshot = list(_active_trades.values())
                for active_t in active_snapshot:
                    if active_t.get("token_address") == token_address or active_t.get("token") == token_address:
                        w_addr = active_t.get("whale_wallet") or active_t.get("wallet", "")
                        if w_addr:
                            recent_whales.add(w_addr)

                if len(recent_whales) >= 2:
                    is_consensus = True
                    log.info(f"🔥 MULTI-WHALE CONSENSUS SIGNAL DETECTED for {token_address[:8]}! Whales: {list(recent_whales)}")
                    
                if MOMENTUM_FILTER_ENABLED and not is_consensus:
                    is_safe = await check_momentum(token_address)
                    if not is_safe:
                        continue
                
                sol_mint = "So11111111111111111111111111111111111111112"

                # Dynamic Allocation: double size for Multi-Whale Consensus signals (up to 50% max)
                effective_alloc_pct = min(50.0, ALLOCATION_PCT * 2.0) if is_consensus else ALLOCATION_PCT

                # Portfolio-level exposure cap. ALLOCATION_PCT alone is a PER-TRADE limit, so
                # MAX_CONCURRENT_TRADES trades could each take their full share and put the
                # whole wallet into simultaneous illiquid micro-caps.
                async with _dashboard_lock:
                    open_exposure_usd = sum(
                        t.get("trade_size", t.get("trade_size_usd", 0.0)) for t in _active_trades.values()
                    )

                # Liquidity ceiling: what fraction of the deepest pool we're willing to be.
                #
                # When DexScreener has no liquidity figure we do NOT invent one. A previous
                # version substituted `max(market_cap * 0.15, 10000)`, which is a guess that
                # happens to authorise trading precisely on the tokens nobody can measure —
                # exactly the ones most likely to be unsellable.
                #
                # Instead the pre-size cap is skipped and the decision is deferred to
                # MAX_PRICE_IMPACT_PCT, which is computed from the real Jupiter route for this
                # exact order size a few lines below. That is a direct measurement of whether
                # the pool can absorb us, and it is strictly better evidence than any proxy.
                pool_liquidity_usd = await get_pool_liquidity_usd(token_address)
                if pool_liquidity_usd > 0:
                    liquidity_cap_usd = pool_liquidity_usd * (MAX_POOL_SHARE_PCT / 100.0)
                else:
                    liquidity_cap_usd = float("inf")
                    log.info(
                        f"Pool liquidity unknown for {token_address[:8]} — skipping the pool-share "
                        f"cap and relying on the {MAX_PRICE_IMPACT_PCT}% price-impact gate instead."
                    )

                # Calculate trade size dynamically using effective_alloc_pct
                trade_size = 0.0
                lamports = 0
                # MIN_TRADE_SOL is honoured as configured. It was previously clamped down with
                # min(MIN_TRADE_SOL, 0.001), which silently reduced a configured 0.02 floor by
                # 20x and let through trades where the fixed lamport costs are ~209% of the
                # trade value. If trades are being skipped for being too small, the balance is
                # too low to trade — lowering the floor converts that signal into slow bleed.
                min_trade_lamports = int(MIN_TRADE_SOL * 1_000_000_000)


                current_mode = os.getenv("TRADE_MODE", "PAPER").strip().strip('"\'').upper()
                if current_mode == "PAPER":
                    sol_price = await get_sol_price_usd()
                    if sol_price <= 0: sol_price = 150.0

                    async with STATE._lock:
                        equity = STATE.wallet_balance + open_exposure_usd
                        trade_size = STATE.wallet_balance * (effective_alloc_pct / 100.0)

                        # Cap 1: total equity at risk across all open positions.
                        exposure_room = (equity * (MAX_PORTFOLIO_EXPOSURE_PCT / 100.0)) - open_exposure_usd
                        if exposure_room <= 0:
                            log.warning(
                                f"Trade Rejected: portfolio exposure ${open_exposure_usd:.2f} already at the "
                                f"{MAX_PORTFOLIO_EXPOSURE_PCT}% cap of ${equity:.2f} equity."
                            )
                            continue
                        if trade_size > exposure_room:
                            log.info(f"Trimming size ${trade_size:.2f} -> ${exposure_room:.2f} (portfolio exposure cap).")
                            trade_size = exposure_room

                        # Cap 2: never be more than MAX_POOL_SHARE_PCT of the pool.
                        if trade_size > liquidity_cap_usd:
                            log.info(
                                f"Trimming size ${trade_size:.2f} -> ${liquidity_cap_usd:.2f} "
                                f"({MAX_POOL_SHARE_PCT}% of ${pool_liquidity_usd:,.0f} pool liquidity)."
                            )
                            trade_size = liquidity_cap_usd

                        if (trade_size / sol_price) < MIN_TRADE_SOL:
                            log.warning(
                                f"Skipping trade: computed size (${trade_size:.4f} ≈ "
                                f"{trade_size / sol_price:.5f} SOL) is below MIN_TRADE_SOL "
                                f"({MIN_TRADE_SOL} SOL) — fees would dominate a trade this small. "
                                f"Persistent skips mean the balance is too low to trade sensibly."
                            )
                            continue

                        if not STATE.debit_balance(trade_size):
                            log.warning("Insufficient paper balance in RAM.")
                            continue
                            
                    # For paper mode, we need to know how many lamports we are simulating buying to check liquidity
                    sol_amount = trade_size / sol_price
                    lamports = int(sol_amount * 1_000_000_000)
                    
                elif current_mode in ["LIVE", "TRUE"]:
                    real_balance_lamports = await get_sol_balance()

                    # Always leave LIVE_FEE_RESERVE_SOL un-allocated so ALLOCATION_PCT
                    # (even at 100) can never spend the wallet down to a point where
                    # there's nothing left to pay this transaction's own fees.
                    reserve_lamports = int(LIVE_FEE_RESERVE_SOL * 1_000_000_000)
                    allocatable_lamports = max(0, real_balance_lamports - reserve_lamports)
                    lamports = int(allocatable_lamports * (ALLOCATION_PCT / 100.0))

                    if lamports <= 0:
                        log.warning("Insufficient real SOL balance for LIVE trade (after fee reserve).")
                        continue

                    sol_price_for_caps = await get_sol_price_usd()
                    if sol_price_for_caps <= 0: sol_price_for_caps = 150.0

                    # Same two caps as PAPER, so live sizing can't exceed what paper validated.
                    live_equity_usd = (real_balance_lamports / 1_000_000_000) * sol_price_for_caps + open_exposure_usd
                    exposure_room = (live_equity_usd * (MAX_PORTFOLIO_EXPOSURE_PCT / 100.0)) - open_exposure_usd
                    if exposure_room <= 0:
                        log.warning(
                            f"Trade Rejected: portfolio exposure ${open_exposure_usd:.2f} already at the "
                            f"{MAX_PORTFOLIO_EXPOSURE_PCT}% cap of ${live_equity_usd:.2f} equity."
                        )
                        continue

                    size_ceiling_usd = min(exposure_room, liquidity_cap_usd)
                    ceiling_lamports = int((size_ceiling_usd / sol_price_for_caps) * 1_000_000_000)
                    if lamports > ceiling_lamports:
                        pool_cap_str = "n/a" if liquidity_cap_usd == float("inf") else f"${liquidity_cap_usd:.2f}"
                        log.info(
                            f"Trimming LIVE size {lamports / 1e9:.5f} -> {ceiling_lamports / 1e9:.5f} SOL "
                            f"(exposure cap ${exposure_room:.2f} / pool cap {pool_cap_str})."
                        )
                        lamports = max(0, ceiling_lamports)

                    if lamports < min_trade_lamports:
                        log.warning(
                            f"Skipping trade: computed size ({lamports / 1_000_000_000:.5f} SOL) is "
                            f"below MIN_TRADE_SOL ({MIN_TRADE_SOL} SOL) — fees would dominate a trade this small."
                        )
                        continue
                        
                    sol_price = await get_sol_price_usd()
                    if sol_price <= 0: sol_price = 150.0
                    trade_size_sol = lamports / 1_000_000_000
                    trade_size = trade_size_sol * sol_price
                else:
                    log.info(f"TRADE_MODE is FALSE/Watch-Only. Bot is in watch-only mode, skipping trade for {token_address}")
                    continue
                
                from jupiter_api import get_swap_quote
                log.info(f"PRE-TRADE: Checking buy liquidity for {token_address}...")
                # Uncached: this route is the entry FILL, not just an existence check.
                buy_route = await get_swap_quote(sol_mint, token_address, lamports, use_cache=False)
                if not buy_route:
                    log.warning(f"Trade Rejected: No liquidity to buy {token_address}.")
                    if current_mode == "PAPER":
                        async with STATE._lock:
                            STATE.credit_balance(trade_size)
                    continue
                    
                expected_output = int(buy_route.get("outAmount", "0"))
                if expected_output > 0:
                    log.info(f"PRE-TRADE: Checking Honeypot sell liquidity for {token_address}...")
                    sell_route = await get_swap_quote(token_address, sol_mint, expected_output)
                    if not sell_route:
                        log.error(f"HONEYPOT DETECTED: {token_address} allows buying but blocks selling! Trade rejected.")
                        if current_mode == "PAPER":
                            async with STATE._lock:
                                STATE.credit_balance(trade_size)
                        continue
                else:
                    log.warning(f"Trade Rejected: Zero output expected for {token_address}.")
                    if current_mode == "PAPER":
                        async with STATE._lock:
                            STATE.credit_balance(trade_size)
                    continue

                # ── Price impact gate ─────────────────────────────────────────────────
                impact_pct = quote_price_impact_pct(buy_route)
                if impact_pct > MAX_PRICE_IMPACT_PCT:
                    log.warning(
                        f"Trade Rejected: {token_address[:8]} entry price impact {impact_pct:.2f}% "
                        f"> MAX_PRICE_IMPACT_PCT ({MAX_PRICE_IMPACT_PCT}%). Position too large for this pool."
                    )
                    if current_mode == "PAPER":
                        async with STATE._lock:
                            STATE.credit_balance(trade_size)
                    continue

                # ── Entry price: the executable fill, not a DexScreener mid ───────────
                tokens_held = expected_output / (10 ** token_decimals)
                quoted_entry_price = price_from_quote(buy_route, lamports, token_decimals, sol_price)
                if quoted_entry_price <= 0:
                    log.warning(f"Trade Rejected: could not derive an executable entry price for {token_address[:8]}.")
                    if current_mode == "PAPER":
                        async with STATE._lock:
                            STATE.credit_balance(trade_size)
                    continue

                if entry_price > 0:
                    drift_pct = ((quoted_entry_price - entry_price) / entry_price) * 100
                    if abs(drift_pct) > 10.0:
                        log.info(
                            f"Entry price correction for {token_address[:8]}: screen mid ${entry_price:.8f} -> "
                            f"executable ${quoted_entry_price:.8f} ({drift_pct:+.1f}%). "
                            f"Booking the executable price."
                        )
                entry_price = quoted_entry_price

                # --- ML Engine Filter ---
                if ML_ENGINE_ENABLED:
                    prob = predict_trade(wallet, trade_size)
                    confidence = prob * 100.0
                    if confidence < ML_CONFIDENCE:
                        log.info(f"ML ENGINE REJECTED TRADE: {token_address}. Confidence {confidence:.2f}% < {ML_CONFIDENCE}% threshold.")
                        if current_mode == "PAPER":
                            async with STATE._lock:
                                STATE.credit_balance(trade_size)
                        continue
                    else:
                        log.info(f"ML ENGINE APPROVED TRADE: {token_address}. Confidence {confidence:.2f}% >= {ML_CONFIDENCE}%.")

                # ── LIVE: GMGN Execution ──────────────────────────────────────────────
                if current_mode in ["LIVE", "TRUE"]:

                    try:
                        wallet_address = str(Keypair.from_base58_string(WALLET_PRIVATE_KEY).pubkey())
                    except Exception as e:
                        log.error(f"Failed to derive wallet address: {e}. Skipping trade.")
                        continue

                    log.info(f"GMGN LIVE: Executing buy for {token_address[:8]}... investing {lamports/1e9:.4f} SOL")
                    buy_result = await execute_gmgn_buy(
                        wallet_address,
                        token_address,
                        lamports,
                        TAKE_PROFIT_PCT,
                        abs(STOP_LOSS_PCT),
                    )

                    if not buy_result.get("success"):
                        log.warning(
                            f"GMGN buy failed for {token_address[:8]}: "
                            f"{buy_result.get('_error', 'unknown error')}. Skipping."
                        )
                        continue

                    # Wait for the buy order to be confirmed on-chain before recording position.
                    # This prevents phantom positions where the order was submitted but never filled.
                    log.info(f"GMGN: Waiting for order {buy_result['order_id']} to confirm...")
                    confirmed = await wait_for_order_confirmed(buy_result["order_id"], max_wait_seconds=90)
                    if not confirmed.get("confirmed"):
                        log.error(
                            f"GMGN order {buy_result['order_id']} did not confirm "
                            f"(status={confirmed.get('status')}). Not opening position."
                        )
                        send_error_alert(
                            f"⚠️ GMGN BUY NOT CONFIRMED\n"
                            f"Token: {token_address[:8]}...\n"
                            f"Order: {buy_result['order_id']}\n"
                            f"Status: {confirmed.get('status')}\n"
                            f"No position opened. No capital lost."
                        )
                        continue

                    strategy_order_id = buy_result.get("strategy_order_id")
                    
                    # Extract actual fill price if available from GMGN order response
                    actual_price_str = confirmed.get("price_usd") or confirmed.get("price") or (confirmed.get("report") or {}).get("price_usd")
                    if actual_price_str:
                        try:
                            entry_price = float(actual_price_str)
                            log.info(f"GMGN actual buy fill price for {token_address[:8]}: ${entry_price:.8f}")
                        except ValueError:
                            pass

                    # 🛡️ UNPROTECTED TRADE PROTECTION: If strategy creation failed, execute immediate market sell
                    if not strategy_order_id:
                        strategy_order_id = "POLLING_FALLBACK"
                        log.info(f"GMGN buy confirmed for {token_address[:8]} (strategy_order_id missing). Falling back to Python price-polling monitor.")
                    else:
                        log.info(
                            f"GMGN BUY CONFIRMED: {token_address[:8]}... | "
                            f"Whale {wallet[:8]} | ${trade_size:.2f} | "
                            f"strategy={strategy_order_id}"
                        )

                    alert_data = trade_data.copy()
                    alert_data["token"] = token_address
                    alert_data["side"] = "BUY"
                    alert_data["mode"] = TRADE_MODE
                    alert_data["amount"] = f"${trade_size:.2f} via GMGN (TP+{int(TAKE_PROFIT_PCT)}% / SL-{int(abs(STOP_LOSS_PCT))}%)"
                    send_trade_alert(alert_data)

                    entry_time = datetime.now(timezone.utc)
                    pos = Position(
                        token=token_address,
                        symbol=symbol,
                        wallet=wallet,
                        entry_price=entry_price,
                        trade_size=trade_size,
                        entry_time=entry_time.isoformat(),
                        mode=TRADE_MODE,
                        execution_engine="GMGN",
                        strategy_order_id=strategy_order_id,
                    )
                    async with _dashboard_lock:
                        STATE.add_position(token_address, pos)

                    task = asyncio.create_task(
                        monitor_gmgn_position(
                            wallet, token_address, entry_price, entry_time,
                            trade_size, strategy_order_id, wallet_address
                        )
                    )
                    _background_tasks.add(task)
                    task.add_done_callback(_background_tasks.discard)

                # ── PAPER: Jupiter route simulation ───────────────────────────────────
                else:
                    # Entry cost including the entry-leg fee. The exit leg is charged at close.
                    actual_entry_cost_usd = trade_size * (1 + PAPER_FEE_PCT_PER_LEG / 100.0)
                    log.info(
                        f"Whale {wallet[:8]} bought {token_address[:8]}. Mode: PAPER. "
                        f"Investing ${trade_size:.2f} at executable ${entry_price:.8f} "
                        f"({tokens_held:.4f} tokens, impact {impact_pct:.2f}%)"
                    )

                    alert_data = trade_data.copy()
                    alert_data["token"] = token_address
                    alert_data["side"] = "BUY"
                    alert_data["mode"] = TRADE_MODE
                    alert_data["amount"] = f"{item.get('amount')} (Fill ${entry_price:.8f}, impact {impact_pct:.2f}%)"
                    send_trade_alert(alert_data)

                    entry_time = datetime.now(timezone.utc)
                    pos = Position(
                        token=token_address,
                        symbol=symbol,
                        wallet=wallet,
                        entry_price=entry_price,
                        trade_size=trade_size,
                        tokens_held=tokens_held,
                        token_decimals=token_decimals,
                        entry_price_impact_pct=impact_pct,
                        actual_entry_cost_usd=actual_entry_cost_usd,
                        entry_time=entry_time.isoformat(),
                        mode=TRADE_MODE,
                        execution_engine="JUPITER",
                    )
                    async with _dashboard_lock:
                        STATE.add_position(token_address, pos)

                    task = asyncio.create_task(
                        monitor_position(wallet, token_address, entry_price, entry_time,
                                         trade_size, actual_entry_cost_usd=actual_entry_cost_usd,
                                         tokens_held=tokens_held, token_decimals=token_decimals)
                    )
                    _background_tasks.add(task)
                    task.add_done_callback(_background_tasks.discard)

            else:
                log.warning(f"Could not get Jupiter entry price for {token_address}.")
        finally:
            _processing_tokens.discard(token_address)
            _pending_trades -= 1



