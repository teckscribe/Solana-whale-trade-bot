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

import settings_manager

# ─── Configuration ────────────────────────────────────────────────────────────

GMGN_DISCOVERY_ENABLED = bool(settings_manager.get("GMGN_DISCOVERY_ENABLED"))
GMGN_MIN_WINRATE = float(settings_manager.get("GMGN_MIN_WINRATE"))
GMGN_MIN_TRADES = int(settings_manager.get("GMGN_MIN_TRADES"))
GMGN_MAX_TRADES_7D = int(settings_manager.get("GMGN_MAX_TRADES_7D"))
GMGN_MIN_SOL_BALANCE = float(settings_manager.get("GMGN_MIN_SOL_BALANCE"))
GMGN_MIN_ROI_PCT = float(settings_manager.get("GMGN_MIN_ROI_PCT"))
GMGN_MAX_FEE_TO_PROFIT_PCT = float(settings_manager.get("GMGN_MAX_FEE_TO_PROFIT_PCT"))
GMGN_BANNED_TAGS = [t.strip().lower() for t in os.getenv("GMGN_BANNED_TAGS", "sniper, mev, padre, banana, trojan, maestro").split(",") if t.strip()]
GMGN_API_KEY = os.getenv("GMGN_API_KEY", "")


def _refresh_gmgn_settings():
    global GMGN_DISCOVERY_ENABLED, GMGN_MIN_WINRATE, GMGN_MIN_TRADES, GMGN_MAX_TRADES_7D, GMGN_MIN_SOL_BALANCE
    global GMGN_MIN_ROI_PCT, GMGN_MAX_FEE_TO_PROFIT_PCT, GMGN_BANNED_TAGS
    try:
        GMGN_DISCOVERY_ENABLED = bool(settings_manager.get("GMGN_DISCOVERY_ENABLED"))
        GMGN_MIN_WINRATE = float(settings_manager.get("GMGN_MIN_WINRATE"))
        GMGN_MIN_TRADES = int(settings_manager.get("GMGN_MIN_TRADES"))
        GMGN_MAX_TRADES_7D = int(settings_manager.get("GMGN_MAX_TRADES_7D"))
        GMGN_MIN_SOL_BALANCE = float(settings_manager.get("GMGN_MIN_SOL_BALANCE"))
        GMGN_MIN_ROI_PCT = float(settings_manager.get("GMGN_MIN_ROI_PCT"))
        GMGN_MAX_FEE_TO_PROFIT_PCT = float(settings_manager.get("GMGN_MAX_FEE_TO_PROFIT_PCT"))
        GMGN_BANNED_TAGS = [t.strip().lower() for t in os.getenv("GMGN_BANNED_TAGS", "sniper, mev, padre, banana, trojan, maestro").split(",") if t.strip()]
    except Exception:
        pass

# Track wallets already sent for approval to avoid duplicate notifications across restarts
_NOTIFIED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "gmgn_notified_wallets.json")
_pending_approvals = set()
_already_notified = {}  # wallet -> timestamp, prevents re-notifying within 7 days
_NOTIFICATION_COOLDOWN = 7 * 86400  # 7 days


def _load_notified_history():
    global _already_notified
    if os.path.exists(_NOTIFIED_FILE):
        try:
            with open(_NOTIFIED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    _already_notified.update({k: float(v) for k, v in data.items()})
        except Exception:
            pass


def _save_notified_history():
    try:
        os.makedirs(os.path.dirname(_NOTIFIED_FILE), exist_ok=True)
        with open(_NOTIFIED_FILE, "w", encoding="utf-8") as f:
            json.dump(_already_notified, f, indent=2)
    except Exception:
        pass


_load_notified_history()


# ─── Public API Functions ─────────────────────────────────────────────────────

async def fetch_top_wallets(limit: int = 20) -> list:
    """
    Discovers active smart money wallets via omni_whale_finder's pre-filtered
    smart_degen and low-frequency smartmoney stream.
    """
    if not GMGN_API_KEY:
        log.error("GMGN_API_KEY is not set in .env! GMGN Discovery cannot run.")
        return []

    log.info("Fetching recent smartmoney candidates via gmgn-cli...")
    try:
        import omni_whale_finder
        cands = await omni_whale_finder.fetch_gmgn_smart_money(limit=limit)
        if cands:
            return [{"address": c["wallet"]} for c in cands if c.get("wallet")]
    except Exception as e:
        log.debug(f"Fallback to direct gmgn-cli smartmoney stream: {e}")

    try:
        import shutil
        exe = shutil.which("gmgn-cli.cmd" if os.name == "nt" else "gmgn-cli")
        cmd_str = (
            f'"{exe}" track smartmoney --chain sol --limit 200 --raw'
            if exe
            else f'{"npx.cmd" if os.name == "nt" else "/usr/bin/npx"} -y gmgn-cli track smartmoney --chain sol --limit 200 --raw'
        )
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
    Fetches detailed official 7D stats for a specific wallet via whale_scanner_core
    (respecting 1.5s CLI pacing and 429 ban protection).
    """
    log.info(f"Fetching official GMGN 7D stats for wallet {wallet_address[:8]}...")
    try:
        import whale_scanner_core
        s7 = await asyncio.to_thread(whale_scanner_core.query_gmgn_stats, wallet_address, "7d")
        if not s7:
            return None

        total_trades = int(s7.get("trades", 0) or 0)
        buy_count = int(s7.get("buy", 0) or 0)
        sell_count = int(s7.get("sell", 0) or 0)
        token_num = int(s7.get("token_num", 0) or 0)
        winrate = float(s7.get("winrate", 0.0) or 0.0)
        winning_tokens = round((winrate / 100.0) * token_num) if token_num > 0 else round((winrate / 100.0) * total_trades)

        stats = {
            "address": wallet_address,
            "winrate_1d": 0.0,
            "winrate_7d": winrate,
            "winrate_30d": 0.0,
            "winrate_all": 0.0,
            "total_trades_1d": 0,
            "total_trades_7d": total_trades,
            "total_trades_30d": 0,
            "total_trades_all": 0,
            "buy_7d": buy_count,
            "sell_7d": sell_count,
            "token_num_7d": token_num,
            "winning_tokens_7d": winning_tokens,
            "wins_7d": winning_tokens if token_num > 0 else round((winrate / 100.0) * total_trades),
            "wins_30d": 0,
            "wins_all": 0,
            "realized_profit": float(s7.get("realized", 0.0) or 0.0),
            "unrealized_profit": 0.0,
            "roi_pct": float(s7.get("roi_pct", 0.0) or 0.0),
            "bought_cost": float(s7.get("bought_cost", 0.0) or 0.0),
            "bought_fee": float(s7.get("bought_fee", 0.0) or 0.0),
            "fee_to_profit_pct": float(s7.get("fee_to_profit_pct", 0.0) or 0.0),
            "runners_2x_plus": int(s7.get("runners_2x_plus", 0) or 0),
            "avg_holding_seconds": float(s7.get("avg_holding_seconds", 0.0) or 0.0),
            "sol_balance": float(s7.get("native_balance", 0.0) or 0.0),
            "tags": list(s7.get("tags", []) or []),
            "raw_7d": s7,
        }
        if total_trades > 0 or stats["realized_profit"] != 0.0:
            return stats
        return None
    except Exception as e:
        log.error(f"Failed to fetch stats for {wallet_address[:8]}: {e}")
        return None


def cross_check_wallet_parameters(wallet_address: str, detailed_stats: dict | None = None) -> tuple[bool, list[str], dict]:
    """
    Unified parameter cross-check & anti-toxicity gauntlet for Telegram suggestions and approvals.
    Verifies a wallet against all configured settings_manager parameters AND omni_whale_finder safety gates:
    - Banned tags (sniper, mev, arbitrager, bot, dev, etc.)
    - 7D trade count window (GMGN_MIN_TRADES <= trades <= GMGN_MAX_TRADES_7D)
    - 7D win rate >= GMGN_MIN_WINRATE
    - 7D realized profit > $0
    - Live SOL balance >= GMGN_MIN_SOL_BALANCE
    - 7D realized ROI % >= GMGN_MIN_ROI_PCT (rejects +3% high-volume micro-scalpers)
    - Buy-side priority/bribe fee ratio <= GMGN_MAX_FEE_TO_PROFIT_PCT (rejects untagged Jito/MEV bribers)
    - Buy/Sell symmetry (sell <= buy * 2.5, rejecting transfer-in dumpers / clip-splitters)
    - AI Whale Scorer score >= 50 and status != BLACKLIST
    """
    _refresh_gmgn_settings()
    import whale_scanner_core
    import omni_whale_finder

    if not detailed_stats:
        s7 = whale_scanner_core.query_gmgn_stats(wallet_address, period="7d")
        if not s7:
            return False, ["Could not fetch live 7D GMGN stats (CLI rate-limited or wallet inactive)"], {}
        token_num = int(s7.get("token_num", 0) or 0)
        winrate_val = float(s7.get("winrate", 0.0) or 0.0)
        total_trades_val = int(s7.get("trades", 0) or 0)
        detailed_stats = {
            "address": wallet_address,
            "winrate_7d": winrate_val,
            "total_trades_7d": total_trades_val,
            "buy_7d": int(s7.get("buy", 0) or 0),
            "sell_7d": int(s7.get("sell", 0) or 0),
            "token_num_7d": token_num,
            "winning_tokens_7d": round((winrate_val / 100.0) * token_num) if token_num > 0 else round((winrate_val / 100.0) * total_trades_val),
            "wins_7d": round((winrate_val / 100.0) * token_num) if token_num > 0 else round((winrate_val / 100.0) * total_trades_val),
            "realized_profit": float(s7.get("realized", 0.0) or 0.0),
            "roi_pct": float(s7.get("roi_pct", 0.0) or 0.0),
            "bought_cost": float(s7.get("bought_cost", 0.0) or 0.0),
            "bought_fee": float(s7.get("bought_fee", 0.0) or 0.0),
            "fee_to_profit_pct": float(s7.get("fee_to_profit_pct", 0.0) or 0.0),
            "runners_2x_plus": int(s7.get("runners_2x_plus", 0) or 0),
            "avg_holding_seconds": float(s7.get("avg_holding_seconds", 0.0) or 0.0),
            "sol_balance": float(s7.get("native_balance", 0.0) or 0.0),
            "tags": list(s7.get("tags", []) or []),
            "raw_7d": s7,
        }

    winrate = float(detailed_stats.get("winrate_7d", 0.0) or 0.0)
    total_trades = int(detailed_stats.get("total_trades_7d", 0) or 0)
    buy_cnt = int(detailed_stats.get("buy_7d", 0) or 0)
    sell_cnt = int(detailed_stats.get("sell_7d", 0) or 0)
    realized_profit = float(detailed_stats.get("realized_profit", 0.0) or 0.0)
    sol_balance = float(detailed_stats.get("sol_balance", 0.0) or 0.0)
    roi_pct = float(detailed_stats.get("roi_pct", 0.0) or 0.0)
    bought_cost = float(detailed_stats.get("bought_cost", 0.0) or 0.0)
    bought_fee = float(detailed_stats.get("bought_fee", 0.0) or 0.0)
    fee_to_profit_pct = float(detailed_stats.get("fee_to_profit_pct", 0.0) or 0.0)
    wallet_tags = [str(t).strip().lower() for t in (detailed_stats.get("tags", []) or [])]

    rejections: list[str] = []

    all_banned = set(GMGN_BANNED_TAGS) | set(omni_whale_finder.BANNED_WHALE_TAGS)
    matched_banned = sorted(set(wallet_tags) & all_banned)
    if matched_banned:
        rejections.append(f"Banned tag(s): {', '.join(matched_banned)}")

    if total_trades < GMGN_MIN_TRADES:
        rejections.append(f"7D trades ({total_trades}) < min ({GMGN_MIN_TRADES})")
    if total_trades > GMGN_MAX_TRADES_7D:
        rejections.append(f"7D trades ({total_trades}) > max ({GMGN_MAX_TRADES_7D} anti-sniper cap)")
    if winrate < GMGN_MIN_WINRATE:
        rejections.append(f"7D win rate ({winrate:.1f}%) < min ({GMGN_MIN_WINRATE:.1f}%)")
    if realized_profit <= 0.0:
        rejections.append(f"7D realized PnL (${realized_profit:,.2f}) must be positive")
    if sol_balance < GMGN_MIN_SOL_BALANCE:
        rejections.append(f"SOL balance ({sol_balance:.2f} SOL) < min ({GMGN_MIN_SOL_BALANCE:.2f} SOL)")
    if bought_cost > 0 and roi_pct < GMGN_MIN_ROI_PCT:
        rejections.append(
            f"7D realized ROI ({roi_pct:.2f}% on ${bought_cost:,.0f} volume) < min ({GMGN_MIN_ROI_PCT:.1f}% — micro-scalper)"
        )
    if bought_fee > 0 and fee_to_profit_pct > GMGN_MAX_FEE_TO_PROFIT_PCT:
        rejections.append(
            f"Priority/bribe fee ratio ({fee_to_profit_pct:.1f}% = ${bought_fee:,.0f} fees / ${realized_profit:,.0f} PnL) > max ({GMGN_MAX_FEE_TO_PROFIT_PCT:.1f}% — Jito/MEV briber)"
        )
    if buy_cnt > 0 and sell_cnt > buy_cnt * 2.5:
        rejections.append(
            f"Sell/Buy asymmetry ({sell_cnt} sells vs {buy_cnt} buys = {sell_cnt / buy_cnt:.1f}x > 2.5x max — dump/clip-splitter)"
        )

    s7_dict = detailed_stats.get("raw_7d") or {
        "winrate": winrate,
        "trades": total_trades,
        "buy": buy_cnt,
        "sell": sell_cnt,
        "realized": realized_profit,
        "native_balance": sol_balance,
        "roi_pct": roi_pct,
        "bought_cost": bought_cost,
        "bought_fee": bought_fee,
        "fee_to_profit_pct": fee_to_profit_pct,
        "tags": wallet_tags,
    }
    safety = omni_whale_finder.evaluate_wallet_safety(wallet_address, {
        "wallet": wallet_address,
        "7d": s7_dict,
        "native_balance": sol_balance,
        "tags": wallet_tags,
    })
    detailed_stats["ai_score"] = safety.get("ai_score", 0)
    detailed_stats["ai_status"] = safety.get("ai_status", "WATCH")
    detailed_stats["ai_style"] = safety.get("ai_style", [])
    detailed_stats["ai_flags"] = safety.get("ai_red_flags", [])
    detailed_stats["ai_summary"] = safety.get("ai_summary", "")

    if not safety.get("passed_filters"):
        for r in safety.get("rejections", []):
            # Skip redundant messages already covered by the detailed cross-check lines above
            if any(prefix in r for prefix in ("Low 7d realized ROI:", "Excessive priority/bribe fee ratio:", "Asymmetric sell/buy ratio:", "Banned tag:", "Trade frequency", "Insufficient history:", "Win rate", "7d realized PnL", "Insufficient SOL balance:")):
                continue
            if r not in rejections:
                rejections.append(r)

    return (len(rejections) == 0), rejections, detailed_stats


async def run_gmgn_discovery() -> set:
    """
    Main GMGN discovery function.
    
    Fetches active smartmoney wallets, filters by winrate and trade count,
    checks if already tracked, and sends new discoveries to Telegram.
    """
    _refresh_gmgn_settings()
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
        
        # 3. Fetch detailed stats and run full parameter cross-check on new candidates
        import whale_scanner_core
        await asyncio.sleep(2.0)
        for addr in new_candidates:
            if whale_scanner_core.is_gmgn_rate_limited():
                log.info("GMGN CLI is in rate-limit cooldown; deferring remaining candidates to next cycle.")
                break

            detailed_stats = await get_wallet_stats(addr)

            if detailed_stats:
                # Only record in discovery DB once stats were actually fetched
                whale_manager.add_whales({addr})
                passed, rejections, detailed_stats = cross_check_wallet_parameters(addr, detailed_stats)
                winrate = detailed_stats.get("winrate_7d", 0.0)
                total_trades = detailed_stats.get("total_trades_7d", 0)
                sol_balance = detailed_stats.get("sol_balance", 0.0)
                roi_pct = detailed_stats.get("roi_pct", 0.0)
                fee_ratio = detailed_stats.get("fee_to_profit_pct", 0.0)

                if not passed:
                    _already_notified[addr] = time.time()
                    _save_notified_history()
                    log.info(
                        f"Rejected GMGN wallet {addr[:8]}... via parameter cross-check: {'; '.join(rejections)}"
                    )
                    continue

                # 5. Send only verified, parameter-compliant wallets to Telegram for approval
                from telegram_notifier import send_gmgn_wallet_approval
                send_gmgn_wallet_approval(detailed_stats)

                _pending_approvals.add(addr)
                _already_notified[addr] = time.time()
                _save_notified_history()
                sent_for_approval.add(addr)

                log.info(
                    f"Sent verified GMGN wallet {addr[:8]}... for Telegram approval "
                    f"(WR: {winrate:.0f}%, ROI: +{roi_pct:.1f}%, FeeRatio: {fee_ratio:.1f}%, "
                    f"Trades: {total_trades}, SOL: {sol_balance:.2f}, AI Score: {detailed_stats.get('ai_score', 'N/A')})"
                )

            # Limit to 3 new wallet notifications per cycle to avoid spam
            if len(sent_for_approval) >= 3:
                break

            # Pace between wallet detail fetches to respect CLI rate limits
            await asyncio.sleep(1.5)

    except Exception as e:
        log.error(f"Error in GMGN Discovery: {e}")
        import traceback
        log.debug(traceback.format_exc())

    log.info(f"GMGN Discovery Cycle Complete. Sent {len(sent_for_approval)} wallets for approval.")
    return sent_for_approval


def mark_wallet_approved(wallet_address: str):
    """Called by telegram_bot when user approves a wallet."""
    _pending_approvals.discard(wallet_address)
    _already_notified[wallet_address] = time.time()
    _save_notified_history()
    log.info(f"Wallet {wallet_address[:8]}... approved and removed from pending.")


def mark_wallet_rejected(wallet_address: str):
    """Called by telegram_bot when user rejects a wallet."""
    _pending_approvals.discard(wallet_address)
    _already_notified[wallet_address] = time.time()
    _save_notified_history()
    log.info(f"Wallet {wallet_address[:8]}... rejected and removed from pending.")


def _cleanup_notification_cooldowns():
    """Remove expired notification cooldowns to prevent memory growth."""
    now = time.time()
    expired = [addr for addr, ts in _already_notified.items() if now - ts > _NOTIFICATION_COOLDOWN]
    if expired:
        for addr in expired:
            del _already_notified[addr]
        _save_notified_history()


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


