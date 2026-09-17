"""
telegram_bot.py
Brief: Whale Tracker Telegram Control Bot. The interactive Mobile Command Center. Renders the 7 submenus, intercepts inline button clicks, and automatically rewrites the server's .env file on the fly.
Wirings: Runs completely isolated as a separate systemd service (wtb-ui.service). Communicates with the bot engine by modifying the shared .env file and JSON databases.

Commands:
  /start  - show main menu
  /status - live bot status
  /menu   - show control buttons

Inline buttons:
   Start      sudo systemctl start wtb
   Stop       sudo systemctl stop wtb
   Restart    sudo systemctl restart wtb
   Status     service status
   Whales     list currently tracked whales
   Add Whale  manually add a whale to .env
   Toggle Mode  toggle TRADE_MODE between PAPER and TRUE
  
Security: all input rejected from any chat_id other than TELEGRAM_CHAT_ID.

Credentials loaded from .env:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""

import os
import json
import logging
import subprocess
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

import urllib.request
import urllib.error

# Load env early
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
SERVICE = "wtb.service"
UI_SERVICE = "wtb-ui.service"
NGROK_SERVICE = "wtb-ngrok.service"

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(_BOT_DIR, ".env")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("TelegramControlBot")

# Conversation state - tracks what input we are waiting for
_waiting_for = {}

#  Security 

def _allowed(update: Update) -> bool:
    cid = update.effective_chat.id
    if cid != CHAT_ID:
        log.warning(f"Rejected message from unknown chat_id={cid}")
        return False
    return True

#  Systemctl Helpers 

def _systemctl(action: str, target_service: str = SERVICE) -> tuple[bool, str]:
    try:
        timeout = 20
        result = subprocess.run(
            ["/usr/bin/sudo", "/usr/bin/systemctl", action, target_service],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            return True, f"systemctl {action} {target_service}  OK"
        return False, result.stderr.strip() or result.stdout.strip()
    except Exception as exc:
        return False, str(exc)

def _service_status() -> str:
    try:
        import subprocess
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", SERVICE],
            capture_output=True, text=True, timeout=5,
        )
        state = result.stdout.strip()
        emoji = {"active": "", "inactive": "", "failed": ""}.get(state, "")
        return f"{emoji} Scanner: <b>{state}</b>"
    except Exception:
        return " Scanner: <b>unknown (run on linux to test)</b>"

def _is_service_active(service_name: str) -> bool:
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", service_name],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() in ["active", "activating"]
    except Exception:
        return False

def _get_current_mode() -> str:
    """Reads TRADE_MODE from settings_manager / .env to determine if PAPER or LIVE."""
    try:
        import settings_manager
        val = str(settings_manager.get("TRADE_MODE")).strip().upper()
        return "LIVE (Real Money)" if val in ["TRUE", "LIVE"] else "PAPER (Simulation)"
    except Exception:
        pass
    try:
        with open(ENV_FILE, "r") as f:
            for line in f:
                if line.strip().startswith("TRADE_MODE="):
                    val = line.strip().split("=", 1)[1].strip().strip('"\'').upper()
                    return "LIVE (Real Money)" if val in ["TRUE", "LIVE"] else "PAPER (Simulation)"
    except Exception:
        pass
    return "PAPER (Simulation)"

def _toggle_trade_mode() -> str:
    """Toggles TRADE_MODE in settings_manager and .env between PAPER and TRUE."""
    try:
        import settings_manager
        cur = str(settings_manager.get("TRADE_MODE")).strip().upper()
        new_mode = "LIVE" if cur in ["PAPER", "FALSE"] else "PAPER"
        new_val = "TRUE" if new_mode == "LIVE" else "PAPER"
        settings_manager.update("TRADE_MODE", new_val, source="telegram")
        
        # Also sync to .env for persistence
        try:
            with open(ENV_FILE, "r") as f:
                lines = f.readlines()
            found = False
            new_lines = []
            for line in lines:
                if line.strip().startswith("TRADE_MODE="):
                    found = True
                    new_lines.append(f'TRADE_MODE={new_val}\n')
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f'\nTRADE_MODE={new_val}\n')
            with open(ENV_FILE, "w") as f:
                f.writelines(new_lines)
        except Exception:
            pass
            
        return new_mode
    except Exception as e:
        log.error(f"Failed to toggle trade mode: {e}")
        return "ERROR"


def _update_env_var(key: str, value: str) -> bool:
    """Updates key in settings_manager (hot-reloaded) and persists to .env."""
    try:
        import settings_manager
        ok, _ = settings_manager.update(key, value, source="telegram")
        if ok:
            # Also keep .env in sync
            pass
    except Exception as e:
        log.warning(f"settings_manager.update failed for {key}: {e}")

    try:
        with open(ENV_FILE, "r") as f:
            lines = f.readlines()
        found = False
        new_lines = []
        for line in lines:
            if line.strip().startswith(f"{key}="):
                found = True
                new_lines.append(f"{key}={value}\n")
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"\n{key}={value}\n")
        with open(ENV_FILE, "w") as f:
            f.writelines(new_lines)
        return True
    except Exception as e:
        log.error(f"Failed to update {key}: {e}")
        return False

def _get_env_paper_balance() -> float:
    """Reads PAPER_BALANCE_USD directly from settings_manager or .env file."""
    try:
        import settings_manager
        return float(settings_manager.get("PAPER_BALANCE_USD"))
    except Exception:
        pass
    try:
        with open(ENV_FILE, "r") as f:
            for line in f:
                if line.strip().startswith("PAPER_BALANCE_USD="):
                    val = line.strip().split("=")[1].replace('"', '').strip()
                    return float(val)
    except Exception:
        pass
    return float(os.getenv("PAPER_BALANCE_USD", "100.0"))

def _trade_pnl(t: dict) -> float:
    """
    Realised P&L for a trade record, preferring the verified on-chain fill.

    Must NOT be written as `t.get("real_net_profit_usd") or t.get("net_profit_usd")`:
    a genuinely break-even real fill is 0.0, which is falsy, so `or` would silently
    discard the verified number and fall back to the quoted estimate.
    """
    real = t.get("real_net_profit_usd")
    if real is not None:
        return float(real)
    return float(t.get("net_profit_usd", 0.0) or 0.0)


def _analyze_trades() -> str:
    """Reads ml_training_data.json and active trades to return a statistical summary."""
    json_path = os.path.join(_BOT_DIR, "ml_training_data.json")
    
    try:
        data = []
        if os.path.exists(json_path):
            with open(json_path, 'r') as f:
                data = json.load(f)
                
        real_trades = [t for t in data if t.get("trade_mode", "") != "HISTORICAL"]
        
        def _get_stats(trades_subset, mode_name, wallet_filename, active_filename):
            total_trades = len(trades_subset)
            actioned = [t for t in trades_subset if t.get("exit_reason", "") != "TIMEOUT"]
            total_actioned = len(actioned)
            wins = sum(1 for t in actioned if _trade_pnl(t) > 0)
            win_rate = (wins / total_actioned) * 100 if total_actioned > 0 else 0

            total_profit_usd = sum(_trade_pnl(t) for t in trades_subset)
            avg_profit_usd = total_profit_usd / total_actioned if total_actioned > 0 else 0
            
            # --- Active Trades ---
            active_count = 0
            active_pnl = 0.0
            active_path = os.path.join(_BOT_DIR, active_filename)
            if os.path.exists(active_path):
                try:
                    with open(active_path, 'r') as af:
                        active_data = json.load(af)
                        if isinstance(active_data, dict):
                            active_data = list(active_data.values())
                        active_count = len(active_data)
                        for t in active_data:
                            pnl = t.get("profit_usd", 0.0)
                            if pnl == 0.0 and t.get("entry_price", 0.0) > 0:
                                pnl = ((t.get("current_price", 0.0) - t.get("entry_price", 0.0)) / t.get("entry_price", 1.0)) * t.get("trade_size", t.get("trade_size_usd", 0.0))
                            active_pnl += pnl
                except:
                    pass
            
            # --- Wallet PnL ---
            wallet_path = os.path.join(_BOT_DIR, wallet_filename)
            wallet_str = f" <b>Actual Wallet PnL:</b> N/A"
            try:
                env_paper_bal = _get_env_paper_balance()
                if os.path.exists(wallet_path):
                    with open(wallet_path, 'r') as wf:
                        wdata = json.load(wf)
                    
                    # READ-ONLY from here down.
                    #
                    # This block used to recompute the paper balance and WRITE it back on every
                    # stats view, while trade_brain.record_trade was independently debiting the
                    # same file with no shared lock. Two consequences:
                    #
                    #  1. Viewing stats could restore capital the engine had just committed to an
                    #     open position, letting the bot allocate the same money twice.
                    #  2. `active_data` is only bound inside the try/except above, so if
                    #     paper_trades.json was missing or unreadable the guard
                    #     `'active_data' in locals()` silently made active_invested = 0 and
                    #     credited back EVERY open position's capital.
                    #
                    # trade_brain is the single writer of the wallet file. Report what it says.
                    w_initial = float(wdata.get("initial") or env_paper_bal)
                    if w_initial <= 0.0:
                        w_initial = env_paper_bal

                    w_balance = wdata.get("balance", w_initial)

                    if mode_name == "PAPER" and wdata.get("initial") != env_paper_bal and env_paper_bal > 0:
                        # Surface the drift instead of silently resetting the running account.
                        wallet_str_note = (
                            f"\n <i>Note: PAPER_BALANCE_USD in .env is ${env_paper_bal:.2f} but this "
                            f"account started at ${w_initial:.2f}. Use the settings menu to reset.</i>"
                        )
                    else:
                        wallet_str_note = ""

                    w_profit = w_balance - w_initial
                    wallet_str = (f" <b>Actual Wallet PnL:</b> ${w_profit:.2f} "
                                  f"(Cash: ${w_balance:.2f}){wallet_str_note}")
            except Exception as e:
                log.warning(f"Could not read {wallet_filename}: {e}")
                
            return (
                f" <b>{mode_name} Trade Analysis</b>\n"
                f" <b>Active Open Trades:</b> {active_count}\n"
                f" <b>Unrealized PnL:</b> ${active_pnl:.2f}\n"
                f"\n"
                f" <b>Closed Trades:</b> {total_trades} (Includes Timeouts)\n"
                f" <b>Actioned:</b> {total_actioned} (TP/SL Only)\n"
                f" <b>Wins:</b> {wins} ({win_rate:.1f}%)\n"
                f" <b>Realized Profit (All-Time):</b> ${total_profit_usd:.2f}\n"
                f" <b>Avg Profit/Trade:</b> ${avg_profit_usd:.2f}\n\n"
                f"{wallet_str}"
            )

        paper_trades = [t for t in real_trades if t.get("trade_mode", "PAPER") == "PAPER"]
        live_trades = [t for t in real_trades if t.get("trade_mode", "") in ["LIVE", "TRUE"]]
        
        paper_text = _get_stats(paper_trades, "PAPER", "paper_wallet.json", "paper_trades.json")
        live_text = _get_stats(live_trades, "LIVE", "live_wallet.json", "live_trades.json")
        
        return (
            f" <b>Whale Tracker Performance Data</b>\n\n"
            f"{paper_text}\n\n"
            f"\n\n"
            f"{live_text}"
        )
    except Exception as e:
        return f" <i>Failed to analyze dataset: {e}</i>"

def _get_leaderboard(category="main", page=1) -> tuple[str, __import__('telegram').InlineKeyboardMarkup]:
    """Reads ml_training_data.json and returns the top/bottom whales and their keyboard."""
    import json
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    json_path = os.path.join(_BOT_DIR, "ml_training_data.json")
    if not os.path.exists(json_path):
        return "<i>No trade data found yet.</i>", _main_keyboard()
        
    try:
        with open(json_path, 'r') as f:
            data = json.load(f)
            
        real_trades = [t for t in data if t.get("trade_mode", "") != "HISTORICAL"]
        
        wallets = {}
        for t in real_trades:
            w = t.get("whale_wallet")
            if not w: continue
            
            if w not in wallets:
                wallets[w] = {"total": 0, "wins": 0, "losses": 0, "profit": 0.0, "actioned": 0}
                
            wallets[w]["total"] += 1
            if t.get("exit_reason", "") != "TIMEOUT":
                wallets[w]["actioned"] += 1
            
            profit = t.get("real_net_profit_usd")
            if profit is None:
                profit = t.get("net_profit_usd", 0)
            wallets[w]["profit"] += float(profit)
            
            if float(profit) > 0:
                wallets[w]["wins"] += 1
            elif float(profit) < 0:
                wallets[w]["losses"] += 1
                    
        sorted_wallets = sorted(wallets.items(), key=lambda x: x[1]["profit"], reverse=True)
        winners = [w for w in sorted_wallets if w[1]["profit"] > 0]
        losers = [w for w in sorted_wallets if w[1]["profit"] < 0]
        
        if category == "main":
            text = " <b>WHALE LEADERBOARD</b>\n\nChoose a category to view all trades:"
            buttons = [
                [InlineKeyboardButton(f"🟢 All Winners ({len(winners)})", callback_data="leaderboard_winners_1")],
                [InlineKeyboardButton(f"🔴 All Losers ({len(losers)})", callback_data="leaderboard_losers_1")],
                [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_main")]
            ]
            return text, InlineKeyboardMarkup(buttons)
            
        items = winners if category == "winners" else list(reversed(losers))
        per_page = 30
        total_pages = max(1, (len(items) + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        page_items = items[start_idx:end_idx]
        
        title = "🟢 All Winners" if category == "winners" else "🔴 All Losers"
        lines = [f" <b>{title} (Page {page}/{total_pages})</b>\n"]
        buttons = []
        
        if not page_items:
            lines.append("<i>No data in this category yet.</i>")
            
        for w, stats in page_items:
            sign = "+" if stats['profit'] > 0 else "-"
            lines.append(f" <code>{w}</code>: {sign}${abs(stats['profit']):.2f} ({stats['wins']}W / {stats['losses']}L)")
            buttons.append([
                InlineKeyboardButton(f"➕ WL {w[:4]}..{w[-4:]}", callback_data=f"setwl_{w}"),
                InlineKeyboardButton(f"➖ BL {w[:4]}..{w[-4:]}", callback_data=f"setbl_{w}")
            ])
            
        nav_buttons = []
        if page > 1:
            nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"leaderboard_{category}_{page-1}"))
        if page < total_pages:
            nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"leaderboard_{category}_{page+1}"))
            
        if nav_buttons:
            buttons.append(nav_buttons)
            
        buttons.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="leaderboard")])
        
        return "\n".join(lines), InlineKeyboardMarkup(buttons)
    except Exception as e:
        import traceback
        return f" <i>Error: {e}\n{traceback.format_exc()}</i>", _main_keyboard()

def _get_ngrok_url(target_port: int = None) -> str:
    """Queries active Ngrok public URL strictly matching target_port (8101) across local API ports (4040-4045)."""
    import json, urllib.request
    if target_port is None:
        target_port = int(os.getenv("WEB_PORT", os.getenv("NGROK_TARGET_PORT", "8101")))
    
    env_port = os.getenv("NGROK_API_PORT", "4040")
    ports_to_try = [4040, 4041, 4042, 4043, 4044, 4045]
    if env_port.isdigit() and int(env_port) in ports_to_try:
        ports_to_try.remove(int(env_port))
        ports_to_try.insert(0, int(env_port))

    for port in ports_to_try:
        try:
            url_endpoint = f"http://127.0.0.1:{port}/api/tunnels"
            req = urllib.request.Request(url_endpoint, headers={"User-Agent": "WhaleBot"})
            with urllib.request.urlopen(req, timeout=1.5) as response:
                data = json.loads(response.read().decode("utf-8"))
                for t in data.get("tunnels", []):
                    config_addr = str(t.get("config", {}).get("addr", ""))
                    pub_url = t.get("public_url", "")
                    # Strict Port Matching: Only accept tunnel if target_port (8101) is in config_addr!
                    if str(target_port) in config_addr and pub_url:
                        if pub_url.startswith("http://"):
                            pub_url = pub_url.replace("http://", "https://")
                        return pub_url
        except Exception:
            continue
    return ""



#  Keyboards 

def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 View Status", callback_data="status"), InlineKeyboardButton("🤖 Bot Control", callback_data="menu_bot_control")],
        [InlineKeyboardButton("📈 Analyze Trades", callback_data="analyze_trades"), InlineKeyboardButton("🌐 Dashboard", callback_data="menu_dashboard")],
        [InlineKeyboardButton("🎮 Trade Control", callback_data="menu_trade_control"), InlineKeyboardButton("🐋 Whales", callback_data="menu_whale_management")],
        [InlineKeyboardButton("🛡️ Filters", callback_data="menu_pre_trade"), InlineKeyboardButton("⚙️ GMGN", callback_data="menu_gmgn_settings")],
        [InlineKeyboardButton("🧠 ML Engine", callback_data="menu_ml_engine")],
        [InlineKeyboardButton("❌ Close Panel", callback_data="close_panel")]
    ])

def _bot_control_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Start Scanner", callback_data="confirm_start_scanner")],
        [InlineKeyboardButton("🛑 Stop Scanner", callback_data="confirm_stop_scanner")],
        [InlineKeyboardButton("🔄 Restart Scanner", callback_data="confirm_restart_scanner")],
        [InlineKeyboardButton("🚨 Panic Sell All", callback_data="panic_sell_prompt")],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="back_main")]
    ])

def _dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Start Dashboard", callback_data="confirm_start_ngrok")],
        [InlineKeyboardButton("🛑 Stop Dashboard", callback_data="confirm_stop_ngrok")],
        [InlineKeyboardButton("🌐 Get Web URL", callback_data="ngrok_url")],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="back_main")]
    ])

def _trade_control_keyboard() -> InlineKeyboardMarkup:
    mode_text = "LIVE" if "LIVE" in _get_current_mode() else "PAPER"
    paper_usd = os.getenv("PAPER_BALANCE_USD", "100.0")
    max_trades = os.getenv("MAX_CONCURRENT_TRADES", "5")
    tp_pct = os.getenv("TAKE_PROFIT_PCT", "15.0")
    sl_pct = os.getenv("STOP_LOSS_PCT", "-5.0")
    alloc_pct = os.getenv("ALLOCATION_PCT", "10.0")
    
    timeout_enabled = os.getenv("TIMEOUT_ENABLED", "TRUE").strip().upper() == "TRUE"
    timeout_text = "ON" if timeout_enabled else "OFF"
    timeout_mins = os.getenv("TIMEOUT_MINUTES", "30")
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔄 Trade Mode: {mode_text}", callback_data="confirm_toggle_mode")],
        [InlineKeyboardButton(f"⏳ Timeout: {timeout_text}", callback_data="confirm_toggle_timeout")],
        [InlineKeyboardButton(f"⏱️ Timeout Time: {timeout_mins}m", callback_data="edit_TIMEOUT_MINUTES")],
        [InlineKeyboardButton(f"💵 Paper Balance: ${paper_usd}", callback_data="edit_PAPER_BALANCE_USD")],
        [InlineKeyboardButton(f"🔢 Max Trades: {max_trades}", callback_data="edit_MAX_CONCURRENT_TRADES")],
        [InlineKeyboardButton(f"📈 Take Profit: {tp_pct}%", callback_data="edit_TAKE_PROFIT_PCT")],
        [InlineKeyboardButton(f"🛑 Stop Loss: {sl_pct}%", callback_data="edit_STOP_LOSS_PCT")],
        [InlineKeyboardButton(f"💼 Allocation: {alloc_pct}%", callback_data="edit_ALLOCATION_PCT")],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="back_main")]
    ])

def _whale_management_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🐋 Manage Whales", callback_data="manage_whales")],
        [InlineKeyboardButton("🔍 Scan Whitelist", callback_data="scan_whitelist")],
        [InlineKeyboardButton("🔍 Scan Neutral", callback_data="scan_neutral"), InlineKeyboardButton("🔍 Scan Blacklist", callback_data="scan_blacklist")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")],
        [InlineKeyboardButton("📋 Manage Lists", callback_data="manage_lists")],
        [InlineKeyboardButton("➕ Add Whale", callback_data="add_whale")],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="back_main")]
    ])

def _lists_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Whitelist", callback_data="view_whitelist"), InlineKeyboardButton("➕ Add WL", callback_data="add_whitelist")],
        [InlineKeyboardButton("📋 Blacklist", callback_data="view_blacklist"), InlineKeyboardButton("➕ Add BL", callback_data="add_blacklist")],
        [InlineKeyboardButton("🐋 Tracked Whales", callback_data="list_whales")],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="back_main")]
    ])

def _pretrade_keyboard() -> InlineKeyboardMarkup:
    mom = os.getenv("MOMENTUM_FILTER_ENABLED", "FALSE").strip().upper()
    mom_display = "ON" if mom == "TRUE" else "OFF"
    max_pump = os.getenv("MAX_M5_PUMP_PCT", "300.0")
    min_vol = os.getenv("MIN_24H_VOLUME", "5000.0")
    min_mc = os.getenv("MIN_MARKET_CAP", "10000.0")
    disc_int = os.getenv("DISCOVERY_INTERVAL_MINUTES", "10")
    min_whale_vol = os.getenv("MIN_WHALE_SOL_VOLUME", "1.0")
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔥 Momentum Filter: {mom_display}", callback_data="confirm_toggle_momentum_filter")],
        [InlineKeyboardButton(f"🚀 Max 5m Pump: {max_pump}%", callback_data="edit_MAX_M5_PUMP_PCT")],
        [InlineKeyboardButton(f"📊 Min Vol 24h: ${min_vol}", callback_data="edit_MIN_24H_VOLUME")],
        [InlineKeyboardButton(f"🏛️ Min Market Cap: ${min_mc}", callback_data="edit_MIN_MARKET_CAP")],
        [InlineKeyboardButton(f"⏱️ Discovery Rate: {disc_int}m", callback_data="edit_DISCOVERY_INTERVAL_MINUTES")],
        [InlineKeyboardButton(f"🐋 Min Whale Vol: {min_whale_vol} SOL", callback_data="edit_MIN_WHALE_SOL_VOLUME")],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="back_main")]
    ])

def _gmgn_keyboard() -> InlineKeyboardMarkup:
    enabled = os.getenv("GMGN_DISCOVERY_ENABLED", "TRUE").strip().upper()
    enabled_display = "ON" if enabled == "TRUE" else "OFF"
    winrate = os.getenv("GMGN_MIN_WINRATE", "70.0")
    min_trades = os.getenv("GMGN_MIN_TRADES", "10")
    gmgn_max = os.environ.get("GMGN_MAX_TRADES_7D", "ERROR")
    gmgn_sol = os.getenv("GMGN_MIN_SOL_BALANCE", "1.0")
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🟢 GMGN Discovery: {enabled_display}", callback_data="confirm_toggle_gmgn_discovery")],
        [InlineKeyboardButton(f"🏆 Min Winrate: {winrate}%", callback_data="edit_GMGN_MIN_WINRATE")],
        [InlineKeyboardButton(f"🔢 Min Trades: {min_trades}", callback_data="edit_GMGN_MIN_TRADES")],
        [InlineKeyboardButton(f"⏱️ Max Trades 7D: {gmgn_max}", callback_data="edit_GMGN_MAX_TRADES_7D")],
        [InlineKeyboardButton(f"💰 Min Balance: {gmgn_sol} SOL", callback_data="edit_GMGN_MIN_SOL_BALANCE")],
        [InlineKeyboardButton("🚫 Edit Banned Tags", callback_data="edit_GMGN_BANNED_TAGS")],
        [InlineKeyboardButton("🏋️ Run GMGN ML Training", callback_data="run_ml_training")],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="back_main")]
    ])

def _ml_keyboard() -> InlineKeyboardMarkup:
    enabled = os.getenv("ML_ENGINE", "FALSE").strip().upper()
    enabled_display = "ON" if enabled == "TRUE" else "OFF"
    ml_conf = os.getenv("ML_CONFIDENCE", "45")
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🧠 ML Engine: {enabled_display}", callback_data="confirm_toggle_ml_engine")],
        [InlineKeyboardButton(f"🎯 ML Confidence: {ml_conf}%", callback_data="edit_ML_CONFIDENCE")],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="back_main")]
    ])

# ==========================================
# Message Handler (for adding whales/editing config)
# ==========================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    await update.message.reply_text("🤖 <b>Control Panel</b>", parse_mode="HTML", reply_markup=_main_keyboard())

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
        
    cid = str(update.message.chat_id)
    text = update.message.text
    
    if cid not in _waiting_for:
        return
        
    if text == "/cancel":
        _waiting_for.pop(cid, None)
        await update.message.reply_text("Cancelled.", reply_markup=_main_keyboard())
        return

    if _waiting_for[cid] == "add_whale":
        _waiting_for.pop(cid, None)
        wallet = text.strip()
        if len(wallet) > 30: 
            import whale_manager
            if whale_manager.add_whale(wallet):
                await update.message.reply_text(f"✅ Added {wallet[:4]}..{wallet[-4:]} to tracked whales!", reply_markup=_main_keyboard())
            else:
                await update.message.reply_text("❌ Failed or already exists.", reply_markup=_main_keyboard())
        return

    elif _waiting_for[cid] in ["add_whitelist", "add_blacklist"]:
        action = _waiting_for.pop(cid)
        wallet = text.strip()
        if len(wallet) > 30:
            import whale_manager
            status = "WHITELIST" if action == "add_whitelist" else "BLACKLIST"
            if whale_manager.set_whale_status(wallet, status):
                await update.message.reply_text(f"✅ Added {wallet[:4]}..{wallet[-4:]} to {status}!", reply_markup=_lists_keyboard())
            else:
                await update.message.reply_text("❌ Failed.", reply_markup=_lists_keyboard())
        return

    elif _waiting_for.get(cid, "").startswith("edit_"):
        key = _waiting_for.pop(cid)[5:]
        
        if key in ["GMGN_BANNED_TAGS"]:
            val = text.strip()
        else:
            try:
                val = float(text)
                if key in ["MAX_CONCURRENT_TRADES", "GMGN_MIN_TRADES", "GMGN_MAX_TRADES_7D", "DISCOVERY_INTERVAL_MINUTES", "TIMEOUT_MINUTES"]:
                    val = int(val)
                elif key == "STOP_LOSS_PCT":
                    val = -abs(val)
                elif key == "TAKE_PROFIT_PCT":
                    val = abs(val)
                
                if key == "PAPER_BALANCE_USD":
                    try:
                        import json
                        w_path = os.path.join(_BOT_DIR, "paper_wallet.json")
                        with open(w_path, "w") as f:
                            json.dump({"balance": float(val), "initial": float(val)}, f, indent=4)
                        log.info(f"Updated paper_wallet.json to balance: {val}")
                    except Exception as w_err:
                        log.error(f"Failed to update paper_wallet.json: {w_err}")
            except ValueError:
                await update.message.reply_text("Failed. Please enter a valid number.", reply_markup=_main_keyboard())
                return
                    
        if _update_env_var(key, str(val)):
            os.environ[key] = str(val)
            await update.message.reply_text(f"✅ Success! {key} updated to {val}.\n🔄 Press Restart to apply.", parse_mode="HTML", reply_markup=_main_keyboard())

        else:
            await update.message.reply_text("❌ Failed to update .env", reply_markup=_main_keyboard())
        return

    await update.message.reply_text("Use /start to open the menu.", reply_markup=_main_keyboard())

# ==========================================
# Callback Handler
# ==========================================

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
        
    query = update.callback_query
    await query.answer()
    
    cid = str(query.message.chat_id)
    data = query.data
    
    try:
        if data == "cancel":
            _waiting_for.pop(cid, None)
            await query.edit_message_text("Cancelled.", reply_markup=_main_keyboard())
            
        elif data == "delete_msg":
            try:
                await query.message.delete()
            except:
                await query.edit_message_text("Cancelled.")
                
        elif data == "menu_bot_control":
            _waiting_for.pop(cid, None)
            await query.edit_message_text("🤖 <b>Bot Control</b>", parse_mode="HTML", reply_markup=_bot_control_keyboard())
        elif data == "menu_dashboard":
            _waiting_for.pop(cid, None)
            await query.edit_message_text("🌐 <b>Dashboard</b>", parse_mode="HTML", reply_markup=_dashboard_keyboard())
        elif data == "menu_trade_control":
            _waiting_for.pop(cid, None)
            await query.edit_message_text("🎮 <b>Trade Control</b>", parse_mode="HTML", reply_markup=_trade_control_keyboard())
        elif data == "menu_whale_management":
            _waiting_for.pop(cid, None)
            await query.edit_message_text("🐋 <b>Whale Management</b>", parse_mode="HTML", reply_markup=_whale_management_keyboard())
        elif data == "menu_pre_trade":
            _waiting_for.pop(cid, None)
            await query.edit_message_text("🛡️ <b>Pre-Trade Filters</b>", parse_mode="HTML", reply_markup=_pretrade_keyboard())
        elif data == "menu_gmgn_settings":
            _waiting_for.pop(cid, None)
            await query.edit_message_text("⚙️ <b>GMGN Settings</b>", parse_mode="HTML", reply_markup=_gmgn_keyboard())
        elif data == "menu_ml_engine":
            _waiting_for.pop(cid, None)
            await query.edit_message_text("🧠 <b>ML Engine</b>", parse_mode="HTML", reply_markup=_ml_keyboard())
            
        elif data.startswith("confirm_") and not data.startswith("confirm_wl_") and not data.startswith("confirm_bl_"):
            action = data.replace("confirm_", "")
            confirm_map = {
                "start_scanner": ("▶️ Start the Scanner", "menu_bot_control"),
                "stop_scanner": ("🛑 Stop the Scanner", "menu_bot_control"),
                "restart_scanner": ("🔄 Restart the Scanner", "menu_bot_control"),
                "start_ngrok": ("▶️ Start the Dashboard", "menu_dashboard"),
                "stop_ngrok": ("🛑 Stop the Dashboard", "menu_dashboard"),
                "toggle_mode": ("🔄 Toggle Trade Mode (LIVE/PAPER)", "menu_trade_control"),
                "toggle_timeout": ("⏳ Toggle Trade Timeout", "menu_trade_control"),
                "toggle_momentum_filter": ("🔥 Toggle Momentum Filter", "menu_pre_trade"),
                "toggle_gmgn_discovery": ("🟢 Toggle GMGN Discovery", "menu_gmgn_settings"),
                "toggle_ml_engine": ("🧠 Toggle ML Engine", "menu_ml_engine")
            }
            if action in confirm_map:
                display_text, back_menu = confirm_map[action]
                _waiting_for.pop(cid, None)
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ YES, CONFIRM", callback_data=action)],
                    [InlineKeyboardButton("❌ NO, CANCEL", callback_data=back_menu)]
                ])
                await query.edit_message_text(f"⚠️ <b>Confirmation Required</b>\n━━━━━━━━━━━━━━━━━━━━━\nAre you sure you want to <b>{display_text}</b>?", parse_mode="HTML", reply_markup=kb)

        elif data == "status":
            _waiting_for.pop(cid, None)
            try:
                import whale_manager
                tracked_count = len(whale_manager.get_active_whales())
            except:
                tracked_count = 0
                
            try:
                import json
                with open(os.path.join(_BOT_DIR, "whale_db.json"), "r") as f:
                    db_count = len(json.load(f))
            except:
                db_count = 0
                
            mom = os.getenv("MOMENTUM_FILTER_ENABLED", "FALSE").strip().upper()
            ml = os.getenv("ML_ENGINE", "FALSE").strip().upper()
            gmgn = os.getenv("GMGN_DISCOVERY_ENABLED", "TRUE").strip().upper()
            
            text = (
                f"📊 <b>Bot Status</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"{_service_status()}\n"
                f"🤖 Trade Mode: <b>{_get_current_mode()}</b>\n"
                f"⏳ Timeout: <b>{'ON' if os.getenv('TIMEOUT_ENABLED', 'TRUE').strip().upper() == 'TRUE' else 'OFF'}</b> ({os.getenv('TIMEOUT_MINUTES', '30')}m)\n"
                f"📈 Momentum Filter: <b>{'ON' if mom == 'TRUE' else 'OFF'}</b>\n"
                f"🧠 ML Engine: <b>{'ON' if ml == 'TRUE' else 'OFF'}</b>\n"
                f"🟢 GMGN Discovery: <b>{'ON' if gmgn == 'TRUE' else 'OFF'}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"🐋 Active Tracked Whales: <b>{tracked_count}</b>\n"
                f"📂 Total Whales in DB: <b>{db_count}</b>\n"
            )
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=_main_keyboard())

        elif data == "analyze_trades":
            _waiting_for.pop(cid, None)
            await query.edit_message_text(_analyze_trades(), parse_mode="HTML", reply_markup=_main_keyboard())
            
        elif data.startswith("leaderboard"):
            _waiting_for.pop(cid, None)
            
            if data == "leaderboard":
                text, kb = _get_leaderboard("main", 1)
            else:
                # e.g., "leaderboard_winners_1"
                parts = data.split("_")
                category = parts[1]
                page = int(parts[2]) if len(parts) > 2 else 1
                text, kb = _get_leaderboard(category, page)
                
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)

        elif data == "manage_whales":
            _waiting_for.pop(cid, None)
            try:
                import whale_manager
                whales = whale_manager.get_active_whales()
                if not whales:
                    await query.edit_message_text("No whales tracked yet.", reply_markup=_whale_management_keyboard())
                    return
                kb = []
                for w in list(whales)[:20]:
                    short = f"{w[:4]}..{w[-4:]}"
                    kb.append([InlineKeyboardButton(f"❌ Stop Tracking {short}", callback_data=f"del_whale_{w}")])
                kb.append([InlineKeyboardButton("🔙 Back", callback_data="menu_whale_management")])
                await query.edit_message_text("🐋 <b>Manage Active Whales:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
            except Exception:
                await query.edit_message_text("Error loading whales.", reply_markup=_whale_management_keyboard())

        elif data.startswith("del_whale_"):
            _waiting_for.pop(cid, None)
            wallet = data.replace("del_whale_", "")
            try:
                import whale_manager
                if whale_manager.remove_whale(wallet):
                    await query.edit_message_text(f"✅ Removed {wallet[:4]}..{wallet[-4:]}", reply_markup=_whale_management_keyboard())
                else:
                    await query.edit_message_text("❌ Failed to remove.", reply_markup=_whale_management_keyboard())
            except Exception:
                await query.edit_message_text("❌ Error.", reply_markup=_whale_management_keyboard())

        elif data == "manage_lists":
            _waiting_for.pop(cid, None)
            await query.edit_message_text("📋 <b>Lists Management</b>", parse_mode="HTML", reply_markup=_lists_keyboard())

        elif data in ["add_whale", "add_whitelist", "add_blacklist"]:
            _waiting_for[cid] = data
            await query.edit_message_text(f"✏️ <b>Enter Wallet Address</b>\n━━━━━━━━━━━━━━━━━━━━━\nPlease enter the Solana wallet address in chat.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="menu_whale_management")]]))

        elif data == "clear_blacklist_confirm":
            _waiting_for.pop(cid, None)
            try:
                import whale_manager
                if whale_manager.clear_blacklist():
                    await query.edit_message_text("✅ Blacklist cleared successfully.", reply_markup=_lists_keyboard())
                else:
                    await query.edit_message_text("❌ Failed to clear Blacklist.", reply_markup=_lists_keyboard())
            except Exception as e:
                await query.edit_message_text(f"❌ Error clearing Blacklist:\n{e}", reply_markup=_lists_keyboard())

        # --- Interactive Scan Handlers ---
        elif data in ("scan_neutral", "scan_blacklist", "scan_whitelist"):
            if data == "scan_neutral":
                status_target = "NEUTRAL"
            elif data == "scan_blacklist":
                status_target = "BLACKLIST"
            else:
                status_target = "WHITELIST"
            import subprocess
            subprocess.Popen(["python", "interactive_scan.py", status_target])
            await query.answer(f"Scanning {status_target} whales...", show_alert=True)
            await query.edit_message_text(
                f"⏳ Scanning all {status_target} whales (1d, 7d, 30d).\n\n"
                f"Because this checks 3 timeframes for dozens of wallets, it will take ~10 minutes due to API limits. You will receive a new message with the top 10 results when finished.",
                reply_markup=_whale_management_keyboard()
            )

        elif data.startswith("add_wl_") or data.startswith("setwl_"):
            wallet = data.split("_")[2] if "add_wl_" in data else data.replace("setwl_", "")
            kb = [
                [InlineKeyboardButton("✅ Proceed", callback_data=f"confirm_wl_{wallet}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="delete_msg")]
            ]
            await ctx.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"🐋 <b>Confirm Whitelist Operation</b>\n━━━━━━━━━━━━━━━━━━━━━\nAre you sure you want to add <code>{wallet}</code> to your <b>WHITELIST</b>?\n\n<i>This will allow the bot to start Live Trading on this wallet's signals.</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            await query.answer()


        elif data.startswith("confirm_wl_"):
            wallet = data.replace("confirm_wl_", "")
            try:
                import whale_manager
                if whale_manager.set_whale_status(wallet, "WHITELIST"):
                    await query.edit_message_text(f"✅ Successfully added <code>{wallet}</code> to Whitelist!", parse_mode="HTML")
                else:
                    await query.edit_message_text("❌ Wallet not found in database.", parse_mode="HTML")
            except Exception as e:
                await query.edit_message_text(f"❌ Error: {e}", parse_mode="HTML")

        elif data.startswith("rem_wl_"):
            wallet = data.split("_")[2]
            try:
                import whale_manager
                if whale_manager.set_whale_status(wallet, "NEUTRAL"):
                    await query.answer(f"Removed {wallet[:8]} from Whitelist! (now NEUTRAL)", show_alert=True)
                else:
                    await query.answer("Wallet not found in database.", show_alert=True)
            except Exception as e:
                await query.answer(f"Error: {e}", show_alert=True)

        elif data == "panic_sell_prompt":
            _waiting_for.pop(cid, None)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⚠️ YES, SELL ALL!", callback_data="panic_sell_confirm")],
                [InlineKeyboardButton("❌ NO, CANCEL", callback_data="menu_bot_control")]
            ])
            await query.edit_message_text("🚨 <b>WARNING!</b>\nAre you absolutely sure you want to force close ALL active trades at market price?", parse_mode="HTML", reply_markup=kb)

        elif data == "panic_sell_confirm":
            _waiting_for.pop(cid, None)
            try:
                import json
                import time
                ts = time.time()
                # Paper mode: panic.json triggers monitor_position loop
                with open(os.path.join(_BOT_DIR, "panic.json"), "w") as f:
                    json.dump({"panic_timestamp": ts}, f)
                # Live GMGN mode: gmgn_panic.json triggers monitor_gmgn_position loop
                with open(os.path.join(_BOT_DIR, "gmgn_panic.json"), "w") as f:
                    json.dump({"panic_timestamp": ts}, f)
                await query.edit_message_text(
                    "🚨 <b>PANIC SELL INITIATED</b>\n"
                    "Paper trades: closing at current price.\n"
                    "Live GMGN trades: cancelling strategy + force selling.\n"
                    "Check logs for confirmation.",
                    parse_mode="HTML", reply_markup=_bot_control_keyboard()
                )
            except Exception as e:
                await query.edit_message_text(f"❌ Failed to initiate: {e}", reply_markup=_bot_control_keyboard())

        elif data == "start_scanner":
            _waiting_for.pop(cid, None)
            await query.edit_message_text("⏳ Starting Scanner...", reply_markup=_bot_control_keyboard())
            import asyncio, subprocess
            proc = await asyncio.to_thread(subprocess.run, ["/usr/bin/sudo", "systemctl", "start", SERVICE], capture_output=True, text=True)
            if proc.returncode == 0:
                await query.edit_message_text("✅ Scanner started successfully.", reply_markup=_bot_control_keyboard())
            else:
                await query.edit_message_text(f"❌ Failed to start:\n{proc.stderr}", reply_markup=_bot_control_keyboard())

        elif data == "stop_scanner":
            _waiting_for.pop(cid, None)
            await query.edit_message_text("⏳ Stopping Scanner...", reply_markup=_bot_control_keyboard())
            import asyncio, subprocess
            proc = await asyncio.to_thread(subprocess.run, ["/usr/bin/sudo", "systemctl", "stop", SERVICE], capture_output=True, text=True)
            if proc.returncode == 0:
                await query.edit_message_text("✅ Scanner stopped successfully.", reply_markup=_bot_control_keyboard())
            else:
                await query.edit_message_text(f"❌ Failed to stop:\n{proc.stderr}", reply_markup=_bot_control_keyboard())

        elif data == "restart_scanner":
            _waiting_for.pop(cid, None)
            await query.edit_message_text("🔄 Scanner restarting... Please wait.", reply_markup=_bot_control_keyboard())
            import asyncio, subprocess
            proc = await asyncio.to_thread(subprocess.run, ["/usr/bin/sudo", "systemctl", "restart", SERVICE], capture_output=True, text=True)
            if proc.returncode == 0:
                await query.edit_message_text("✅ Scanner restarted successfully!", reply_markup=_bot_control_keyboard())
            else:
                await query.edit_message_text(f"❌ Failed to restart:\n{proc.stderr}", reply_markup=_bot_control_keyboard())

        elif data == "start_ngrok":
            _waiting_for.pop(cid, None)
            import subprocess
            subprocess.Popen(["/usr/bin/sudo", "/usr/bin/systemctl", "start", NGROK_SERVICE])
            await query.edit_message_text("▶️ Ngrok Dashboard starting... please wait 5s and click 'Get Web URL'.", reply_markup=_dashboard_keyboard())

        elif data == "stop_ngrok":
            _waiting_for.pop(cid, None)
            import subprocess
            subprocess.Popen(["/usr/bin/sudo", "/usr/bin/systemctl", "stop", NGROK_SERVICE])
            await query.edit_message_text("🛑 Ngrok Dashboard stopped.", reply_markup=_dashboard_keyboard())

        elif data == "ngrok_url":
            _waiting_for.pop(cid, None)
            url = _get_ngrok_url()
            if url:
                auth_token = os.getenv("WEB_AUTH_TOKEN", "").strip()
                if not auth_token and os.path.exists(os.path.join(_BOT_DIR, ".web_auth_token")):
                    try:
                        with open(os.path.join(_BOT_DIR, ".web_auth_token"), "r") as f:
                            auth_token = f.read().strip()
                    except: pass
                if auth_token and auth_token.upper() not in ["NONE", "DISABLED", "FALSE", "OFF"]:
                    url = f"{url}/?token={auth_token}"
                await query.edit_message_text(f"🌐 <b>Dashboard is Live!</b>\n\nURL: {url}", parse_mode="HTML", reply_markup=_dashboard_keyboard())

            elif not _is_service_active(NGROK_SERVICE):
                await query.edit_message_text(
                    "🛑 <b>Dashboard Web Service is STOPPED.</b>\n\n"
                    "Press ▶️ <b>Start Dashboard</b> to launch the tunnel.",
                    parse_mode="HTML",
                    reply_markup=_dashboard_keyboard()
                )
            else:
                await query.edit_message_text(
                    "⏳ <b>Dashboard is starting...</b>\n\n"
                    "Please wait a few seconds and click <b>Get Web URL</b> again.",
                    parse_mode="HTML",
                    reply_markup=_dashboard_keyboard()
                )
                
        elif data == "toggle_mode":
            _waiting_for.pop(cid, None)
            current = _get_current_mode()
            new_mode = "LIVE" if "PAPER" in current else "PAPER"
            if _update_env_var("TRADE_MODE", new_mode):
                os.environ["TRADE_MODE"] = new_mode
                await query.edit_message_text(f"🔀 <b>Trade Mode changed to {new_mode}!</b>\n🔄 Press <b>🔄 Restart</b> to apply.", parse_mode="HTML", reply_markup=_main_keyboard())
            else:
                await query.edit_message_text("❌ Failed to update Trade Mode.", reply_markup=_main_keyboard())
                
        elif data == "toggle_ml_engine":
            _waiting_for.pop(cid, None)
            current = os.getenv("ML_ENGINE", "FALSE").strip().upper()
            new_val = "TRUE" if current == "FALSE" else "FALSE"
            if _update_env_var("ML_ENGINE", new_val):
                os.environ["ML_ENGINE"] = new_val
                await query.edit_message_text(f"🧠 <b>ML Engine is now {'ON' if new_val == 'TRUE' else 'OFF'}!</b>\n🔄 Press <b>🔄 Restart</b> to apply.", parse_mode="HTML", reply_markup=_main_keyboard())
            else:
                await query.edit_message_text("❌ Failed to update status.", reply_markup=_main_keyboard())
                
        elif data == "toggle_timeout":
            _waiting_for.pop(cid, None)
            current = os.getenv("TIMEOUT_ENABLED", "TRUE").strip().upper()
            new_val = "TRUE" if current == "FALSE" else "FALSE"
            if _update_env_var("TIMEOUT_ENABLED", new_val):
                os.environ["TIMEOUT_ENABLED"] = new_val
                await query.edit_message_text(f"✅ <b>Timeout is now {'ON' if new_val == 'TRUE' else 'OFF'}!</b>\n⚠️ Press <b>🔄 Restart</b> to apply.", parse_mode="HTML", reply_markup=_main_keyboard())
            else:
                await query.edit_message_text("❌ Failed to update status.", reply_markup=_main_keyboard())
                
        elif data == "toggle_momentum_filter":
            _waiting_for.pop(cid, None)
            current = os.getenv("MOMENTUM_FILTER_ENABLED", "TRUE").strip().upper()
            new_val = "TRUE" if current == "FALSE" else "FALSE"
            if _update_env_var("MOMENTUM_FILTER_ENABLED", new_val):
                os.environ["MOMENTUM_FILTER_ENABLED"] = new_val
                await query.edit_message_text(f"🔥 <b>Momentum Filter is now {'ON' if new_val == 'TRUE' else 'OFF'}!</b>\n🔄 Press <b>🔄 Restart</b> to apply.", parse_mode="HTML", reply_markup=_main_keyboard())
            else:
                await query.edit_message_text("❌ Failed to update status.", reply_markup=_main_keyboard())

        elif data == "toggle_gmgn_discovery":
            _waiting_for.pop(cid, None)
            current = os.getenv("GMGN_DISCOVERY_ENABLED", "TRUE").strip().upper()
            new_val = "TRUE" if current == "FALSE" else "FALSE"
            if _update_env_var("GMGN_DISCOVERY_ENABLED", new_val):
                os.environ["GMGN_DISCOVERY_ENABLED"] = new_val
                await query.edit_message_text(f"🟢 <b>GMGN Discovery is now {'ON' if new_val == 'TRUE' else 'OFF'}!</b>\n🔄 Press <b>🔄 Restart</b> to apply.", parse_mode="HTML", reply_markup=_main_keyboard())
            else:
                await query.edit_message_text("❌ Failed to update status.", reply_markup=_main_keyboard())

        elif data.startswith("edit_"):
            key = data[5:]
            _waiting_for[cid] = data
            await query.edit_message_text(f"✏️ <b>Edit {key}</b>\n━━━━━━━━━━━━━━━━━━━━━\nPlease enter the new numerical value in the chat.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="menu_trade_control")]]))
            
        elif data == "run_ml_training":
            _waiting_for.pop(cid, None)
            try:
                import subprocess
                # Run the GMGN ML data generator script in the background
                subprocess.Popen(["python3", "generate_ml_data.py"])
                await query.edit_message_text("🏃 <b>Starting GMGN ML Training in background...</b>\n\nDownloading historical trades and recalculating AI weights. This will take a few minutes. You will receive a notification when it's done!", parse_mode="HTML", reply_markup=_main_keyboard())
            except Exception as e:
                await query.edit_message_text(f"❌ Failed to start ML Training: {e}", reply_markup=_main_keyboard())
        elif data == "view_whitelist":
            _waiting_for.pop(cid, None)
            import whale_manager
            wlist = list(whale_manager.get_whitelist())
            suffix = ""
            if len(wlist) > 50:
                suffix = f"\n\n<i>... and {len(wlist) - 50} more</i>"
                wlist = wlist[:50]
            text = f"📋 <b>Whitelist ({len(whale_manager.get_whitelist())})</b>\n\n" + ("\n".join([f"<code>{w}</code>" for w in wlist]) if wlist else "<i>Empty</i>") + suffix
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=_lists_keyboard())
            
        elif data == "view_blacklist":
            _waiting_for.pop(cid, None)
            import whale_manager
            blist = list(whale_manager.get_blacklist())
            suffix = ""
            if len(blist) > 50:
                suffix = f"\n\n<i>... and {len(blist) - 50} more</i>"
                blist = blist[:50]
            text = f"📋 <b>Blacklist ({len(whale_manager.get_blacklist())})</b>\n\n" + ("\n".join([f"<code>{w}</code>" for w in blist]) if blist else "<i>Empty</i>") + suffix
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=_lists_keyboard())
            
        elif data == "list_whales":
            _waiting_for.pop(cid, None)
            import whale_manager
            whales = list(whale_manager.get_active_whales())
            suffix = ""
            if len(whales) > 50:
                suffix = f"\n\n<i>... and {len(whales) - 50} more</i>"
                whales = whales[:50]
            text = f"🐋 <b>Tracked Whales ({len(whale_manager.get_active_whales())})</b>\n\n" + ("\n".join([f"<code>{w}</code>" for w in whales]) if whales else "<i>None tracked yet.</i>") + suffix
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=_lists_keyboard())

        elif data.startswith("prompt_bl_") or data.startswith("setbl_"):
            wallet = data.replace("prompt_bl_", "").replace("setbl_", "")
            kb = [
                [InlineKeyboardButton("✅ Proceed", callback_data=f"confirm_bl_{wallet}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="delete_msg")]
            ]
            await ctx.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"🛑 <b>Confirm Blacklist Operation</b>\n━━━━━━━━━━━━━━━━━━━━━\nAre you sure you want to add <code>{wallet}</code> to your <b>BLACKLIST</b>?\n\n<i>This whale will be permanently ignored by the scanner.</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            await query.answer()


        elif data.startswith("confirm_bl_"):
            wallet = data.replace("confirm_bl_", "")
            try:
                import whale_manager
                if whale_manager.set_whale_status(wallet, "BLACKLIST"):
                    await query.edit_message_text(f"🛑 Successfully added <code>{wallet}</code> to Blacklist!", parse_mode="HTML")
                else:
                    await query.edit_message_text("❌ Wallet not found in database.", parse_mode="HTML")
            except Exception as e:
                await query.edit_message_text(f"❌ Error: {e}", parse_mode="HTML")

        elif data.startswith("gmgn_approve_"):
            wallet = data.replace("gmgn_approve_", "")
            try:
                import whale_manager
                from gmgn_discovery import mark_wallet_approved
                whale_manager.add_whales({wallet})
                whale_manager.set_whale_status(wallet, "WHITELIST")
                mark_wallet_approved(wallet)
                await query.edit_message_text(f"✅ Approved and whitelisted {wallet[:4]}..{wallet[-4:]}", reply_markup=_main_keyboard())
            except Exception as e:
                log.error(f"Error approving wallet: {e}")
                
        elif data.startswith("gmgn_reject_"):
            wallet = data.replace("gmgn_reject_", "")
            try:
                from gmgn_discovery import mark_wallet_rejected
                mark_wallet_rejected(wallet)
                await query.edit_message_text(f"❌ Ignored {wallet[:4]}..{wallet[-4:]}", reply_markup=_main_keyboard())
            except Exception as e:
                log.error(f"Error skipping wallet: {e}")

        elif data == "back_main":
            _waiting_for.pop(cid, None)
            await query.edit_message_text(" <b>Control Panel</b>", parse_mode="HTML", reply_markup=_main_keyboard())

        elif data == "close_panel":
            _waiting_for.pop(cid, None)
            try:
                await query.message.delete()
            except Exception:
                await query.edit_message_text("Panel Closed.", reply_markup=None)

    except Exception as e:
        import traceback
        err_msg = str(e)
        if "Message is not modified" not in err_msg:
            await query.edit_message_text(f"❌ <b>CRITICAL UI ERROR:</b>\n<pre>{traceback.format_exc()}</pre>", parse_mode="HTML")

def main() -> None:
    import sys
    # Add dummy check so compile doesn't fail if already exist
    from telegram.ext import Application
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(callback_handler))
    
    log.info("Starting Telegram Bot...")
    app.run_polling()

if __name__ == "__main__":
    main()






