"""
gmgn_discovery.py
Brief: GMGN.ai Smart Money Discovery Engine (Official OpenAPI Upgrade). Discovers trending tokens, pulls top traders, and runs them through a brutal .env filtration gauntlet (Max Trades, Winrate, Banned Tags) to reject Sniper/MEV bots.
Wirings: Runs as a background loop in main.py. Sends rich approval requests to the user via telegram_notifier.py.
  - Discovers active Smart Money wallets using `npx gmgn-cli track smartmoney`
  - Fetches wallet stats using `npx gmgn-cli portfolio stats`
  - Filters by win rate, trade count, and PnL.
  - Sends wallet details to Telegram for manual approval.
"""

import os
import json
import logging
import time
import asyncio
import subprocess
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("GMGNDiscovery")

# ─── Configuration ────────────────────────────────────────────────────────────

GMGN_DISCOVERY_ENABLED = os.getenv("GMGN_DISCOVERY_ENABLED", "TRUE").strip().upper() == "TRUE"
GMGN_MIN_WINRATE = float(os.getenv("GMGN_MIN_WINRATE", "70.0"))
GMGN_MIN_TRADES = int(float(os.getenv("GMGN_MIN_TRADES", "10")))
GMGN_MAX_TRADES_7D = int(float(os.getenv("GMGN_MAX_TRADES_7D", "200")))
GMGN_MIN_SOL_BALANCE = float(os.getenv("GMGN_MIN_SOL_BALANCE", "1.0"))
GMGN_BANNED_TAGS = [t.strip().lower() for t in os.getenv("GMGN_BANNED_TAGS", "sniper, mev, padre, banana, trojan, maestro").split(",") if t.strip()]
GMGN_API_KEY = os.getenv("GMGN_API_KEY", "")

# Track wallets already sent for approval to avoid duplicate notifications
_pending_approvals = set()
_already_notified = {}  # wallet -> timestamp, prevents re-notifying within 24h
_NOTIFICATION_COOLDOWN = 86400  # 24 hours


# ─── Public API Functions ─────────────────────────────────────────────────────

async def fetch_top_wallets(limit: int = 20) -> list:
    """
    Discovers active smart money wallets by tracking recent trades via official GMGN API.
    """
    if not GMGN_API_KEY:
        log.error("GMGN_API_KEY is not set in .env! GMGN Discovery cannot run.")
        return []
        
    log.info("Fetching recent smartmoney trades via gmgn-cli...")
    
    try:
        # Request 200 trades to ensure we get a good pool of unique wallets
        npx_prefix = "npx.cmd" if os.name == "nt" else "/usr/bin/npx"
        cmd_str = f"{npx_prefix} -y gmgn-cli track smartmoney --chain sol --limit 200 --raw"
        env = os.environ.copy()
        env["GMGN_API_KEY"] = GMGN_API_KEY
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
        
        proc = await asyncio.to_thread(
            subprocess.run, cmd_str, capture_output=True, text=True, encoding="utf-8", env=env, check=False, shell=True
        )
        
        if proc.returncode != 0:
            log.error(f"gmgn-cli track failed: {proc.stderr.strip()}")
            return []
            
        data = json.loads(proc.stdout.strip())
        trades = data.get("list", [])
        
        seen_wallets = set()
        unique_wallets = []
        for t in trades:
            w = t.get("maker")
            if w and w not in seen_wallets:
                seen_wallets.add(w)
                unique_wallets.append({"address": w})
                
        log.info(f"Extracted {len(unique_wallets)} unique smart money wallets.")
        return unique_wallets[:limit]
        
    except Exception as e:
        log.error(f"Failed to fetch smartmoney wallets: {e}")
        return []


async def get_wallet_stats(wallet_address: str) -> dict | None:
    """
    Fetches detailed official stats for a specific wallet from GMGN.ai CLI.
    """
    log.info(f"Fetching official GMGN stats for wallet {wallet_address[:8]}...")
    
    try:
        npx_prefix = "npx.cmd" if os.name == "nt" else "/usr/bin/npx"
        cmd_str = f"{npx_prefix} -y gmgn-cli portfolio stats --chain sol --wallet {wallet_address} --raw"
        env = os.environ.copy()
        env["GMGN_API_KEY"] = GMGN_API_KEY
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
        
        proc = await asyncio.to_thread(
            subprocess.run, cmd_str, capture_output=True, text=True, encoding="utf-8", env=env, check=False, shell=True
        )
        
        if proc.returncode != 0:
            # 429 rate limit is handled gracefully by cli, but if it fails completely we log it
            log.error(f"gmgn-cli portfolio failed for {wallet_address[:8]}: {proc.stderr.strip()}")
            return None
            
        data = json.loads(proc.stdout.strip())
        
        total_trades = _safe_int(data.get("buy", 0)) + _safe_int(data.get("sell", 0))
        winrate = _safe_float(data.get("pnl_stat", {}).get("winrate", 0)) * 100
        
        # Structure the response to match what the rest of the bot expects
        stats = {
            "address": wallet_address,
            "winrate_1d": 0.0,
            "winrate_7d": winrate,
            "winrate_30d": winrate,
            "winrate_all": 0.0,
            "total_trades_1d": 0,
            "total_trades_7d": total_trades,
            "total_trades_30d": total_trades,
            "total_trades_all": total_trades,
            "wins_7d": round((winrate / 100) * total_trades),
            "wins_30d": round((winrate / 100) * total_trades),
            "wins_all": 0,
            "realized_profit": _safe_float(data.get("realized_profit", 0)),
            "unrealized_profit": _safe_float(data.get("unrealized_profit", 0)),
            "sol_balance": _safe_float(data.get("native_balance", 0.0)),
            "tags": data.get("common", {}).get("tags", []),
        }
        
        # Only return if we got meaningful data (or high realized profit as a fallback for missing winrate)
        if stats["winrate_7d"] > 0 or stats["winrate_30d"] > 0 or stats["realized_profit"] >= 10000.0:
            return stats
        
        log.warning(f"No meaningful GMGN data found for wallet {wallet_address[:8]}")
        return None
        
    except Exception as e:
        log.error(f"Failed to fetch stats for {wallet_address[:8]}: {e}")
        return None


async def run_gmgn_discovery() -> set:
    """
    Main GMGN discovery function.
    
    Fetches active smartmoney wallets, filters by winrate and trade count,
    checks if already tracked, and sends new discoveries to Telegram.
    """
    if not GMGN_DISCOVERY_ENABLED:
        log.debug("GMGN Discovery is disabled.")
        return set()
        
    if not GMGN_API_KEY:
        log.error("GMGN_API_KEY missing! Set it in .env. GMGN Discovery paused.")
        return set()
    
    log.info("Starting GMGN Discovery Cycle...")
    sent_for_approval = set()
    
    try:
        import whale_manager
        
        # Clean up old notification cooldowns
        _cleanup_notification_cooldowns()
        
        # 1. Fetch active unique wallets (we ask for 20 unique ones from the latest 200 trades)
        unique_candidates = await fetch_top_wallets(limit=20)
        
        if not unique_candidates:
            return set()
            
        log.info(f"GMGN Discovery found {len(unique_candidates)} unique candidates to evaluate.")
        
        # 2. Check which are already tracked (whitelist + discovery_db) to save API calls
        all_whales_data = whale_manager._load_whales()
        
        new_candidates = []
        for w in unique_candidates:
            addr = w["address"]
            if addr in all_whales_data:
                continue
            if addr in _already_notified and (time.time() - _already_notified[addr] < _NOTIFICATION_COOLDOWN):
                continue
            if addr in _pending_approvals:
                continue
            new_candidates.append(addr)
            
        log.info(f"After duplicate check, {len(new_candidates)} wallets need stat evaluation.")
        
        # 3. Fetch detailed stats for new candidates
        for addr in new_candidates:
            detailed_stats = await get_wallet_stats(addr)
            
            if detailed_stats:
                # 4. Filter by minimum winrate and trade count (with high profit fallback)
                winrate = detailed_stats.get("winrate_7d", 0)
                total_trades = detailed_stats.get("total_trades_7d", 0)
                realized_profit = detailed_stats.get("realized_profit", 0)
                sol_balance = detailed_stats.get("sol_balance", 0.0)
                wallet_tags = detailed_stats.get("tags", [])
                
                has_high_winrate = winrate >= GMGN_MIN_WINRATE
                has_high_profit = realized_profit >= 10000.0
                has_enough_sol = sol_balance >= GMGN_MIN_SOL_BALANCE
                is_not_sniper_bot = total_trades <= GMGN_MAX_TRADES_7D
                
                # Check for banned tags (Sniper bots, MEV, etc.)
                has_banned_tag = any(banned in [t.lower() for t in wallet_tags] for banned in GMGN_BANNED_TAGS)
                
                if has_banned_tag:
                    log.info(f"Rejected GMGN wallet {addr[:8]}... (Banned Tag found: {wallet_tags})")
                    continue
                    
                if not is_not_sniper_bot:
                    log.info(f"Rejected GMGN wallet {addr[:8]}... (Sniper Bot: {total_trades} trades in 7D)")
                    continue
                
                if (has_high_winrate or has_high_profit) and total_trades >= GMGN_MIN_TRADES and has_enough_sol:
                    
                    # 5. Send to Telegram for approval
                    from telegram_notifier import send_gmgn_wallet_approval
                    send_gmgn_wallet_approval(detailed_stats)
                    
                    _pending_approvals.add(addr)
                    _already_notified[addr] = time.time()
                    sent_for_approval.add(addr)
                    
                    log.info(f"Sent GMGN wallet {addr[:8]}... for Telegram approval (WR: {winrate:.0f}%, Trades: {total_trades}, SOL: {sol_balance:.2f})")
            
            # Limit to 3 new wallet notifications per cycle to avoid spam
            if len(sent_for_approval) >= 3:
                break
                
            # Pace between wallet detail fetches to respect CLI rate limits
            await asyncio.sleep(1.0)
        
    except Exception as e:
        log.error(f"Error in GMGN Discovery: {e}")
        import traceback
        log.debug(traceback.format_exc())
    
    log.info(f"GMGN Discovery Cycle Complete. Sent {len(sent_for_approval)} wallets for approval.")
    return sent_for_approval


def mark_wallet_approved(wallet_address: str):
    """Called by telegram_bot when user approves a wallet."""
    _pending_approvals.discard(wallet_address)
    log.info(f"Wallet {wallet_address[:8]}... approved and removed from pending.")


def mark_wallet_rejected(wallet_address: str):
    """Called by telegram_bot when user rejects a wallet."""
    _pending_approvals.discard(wallet_address)
    log.info(f"Wallet {wallet_address[:8]}... rejected and removed from pending.")


def _cleanup_notification_cooldowns():
    """Remove expired notification cooldowns to prevent memory growth."""
    now = time.time()
    expired = [addr for addr, ts in _already_notified.items() if now - ts > _NOTIFICATION_COOLDOWN]
    for addr in expired:
        del _already_notified[addr]


def _safe_float(val) -> float:
    """Safely convert a value to float, returning 0.0 on failure."""
    try:
        if val is None:
            return 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _safe_int(val) -> int:
    """Safely convert a value to int, returning 0 on failure."""
    try:
        if val is None:
            return 0
        return int(val)
    except (ValueError, TypeError):
        return 0


