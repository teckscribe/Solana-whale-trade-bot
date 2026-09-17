"""
=========================================================
interactive_scan.py (GMGN Interactive Whale Scanner)
=========================================================

Short Brief:
This module runs in the background as a standalone script triggered by the Telegram bot.
It fetches the 1-day, 7-day, and 30-day GMGN performance metrics for a specific list 
of whales (either NEUTRAL or BLACKLIST). Because GMGN limits API requests to 1 per 
second, this script runs asynchronously to avoid blocking the main bot UI. Once the 
scan completes (~10 minutes), it compiles the Top 10 most profitable whales and sends 
a formatted Telegram message containing inline buttons that allow the user to instantly 
promote those wallets to the WHITELIST.

Key Features:
- Bypasses Telegram UI blocking during long network requests.
- Calculates dynamic ranking scores based on 7d win rates and realized PnL.
- Direct integration with `telegram_notifier.py` for rich interactive payloads.
"""

import sys
import os
import json
import subprocess
import time
from dotenv import load_dotenv

load_dotenv()
GMGN_API_KEY = os.getenv("GMGN_API_KEY", "")

# Setup paths to import telegram_notifier
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

from telegram_notifier import send_message

def run_cmd(cmd):
    env = os.environ.copy()
    env["GMGN_API_KEY"] = GMGN_API_KEY
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
    
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.IDLE_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
        
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, env=env, creationflags=creationflags
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()

def main():
    if len(sys.argv) < 2:
        print("Usage: python interactive_scan.py <STATUS>")
        return
        
    status_target = sys.argv[1].upper()
    
    import whale_manager
    if status_target == "WHITELIST":
        target_whales = whale_manager.get_whitelist()
    elif status_target == "BLACKLIST":
        target_whales = whale_manager.get_blacklist()
    else:
        # For NEUTRAL
        target_whales = [w for w, info in whale_manager._load_db(whale_manager.DISCOVERY_FILE).items() if info.get("status", "NEUTRAL").upper() == status_target]
    
    if not target_whales:
        send_message(f"No {status_target} whales found to scan.")
        return
        
    results = []
    periods = ["1d", "7d", "30d"]
    
    for idx, w in enumerate(target_whales, 1):
        wallet_stats = {"wallet": w}
        # Fetch 3 periods
        for period in periods:
            # Use 'nice -n 19' to give this process the lowest CPU priority, 
            # ensuring it doesn't starve the Telegram Bot's UI responsiveness.
            npx_prefix = "npx.cmd" if os.name == "nt" else "/usr/bin/npx"
            nice_prefix = "" if os.name == "nt" else "nice -n 19 "
            cmd = f"{nice_prefix}{npx_prefix} -y gmgn-cli portfolio stats --chain sol --period {period} --wallet {w} --raw"
            out = run_cmd(cmd)
            stats_parsed = {}
            if out:
                import re
                match = re.search(r'\{.*\}', out.replace('\n', ''))
                if match:
                    try:
                        s = json.loads(match.group(0))
                        stats_parsed['winrate'] = s.get("pnl_stat", {}).get("winrate", 0) * 100
                        stats_parsed['trades'] = s.get("buy", 0) + s.get("sell", 0)
                        try: stats_parsed['realized'] = float(s.get("realized_profit", 0))
                        except: stats_parsed['realized'] = 0.0
                    except Exception as e:
                        pass
            
            wallet_stats[period] = stats_parsed
            time.sleep(2.0) # API Rate limit + CPU breather
            
        # Calculate a primary ranking metric (using 7d winrate as anchor)
        s7d = wallet_stats.get("7d", {})
        wallet_stats['rank_score'] = s7d.get("winrate", 0) if s7d else 0
        wallet_stats['trades_7d'] = s7d.get("trades", 0) if s7d else 0
        wallet_stats['realized_7d'] = s7d.get("realized", 0.0) if s7d else 0.0
        
        # Only include if they had ANY activity in 30d
        if wallet_stats.get("30d", {}).get("trades", 0) > 0:
            results.append(wallet_stats)

    # Sort by 7d winrate descending
    results.sort(key=lambda x: (x['rank_score'], x['realized_7d']), reverse=True)
    
    # Top 10 limit (Except Whitelist which shows all)
    if status_target == "WHITELIST":
        top_results = results
    else:
        top_results = results[:10]
    
    if not top_results:
        send_message(f"Scan complete. No active {status_target} whales found.")
        return
        
    # Build Telegram Message text in chunks of 15
    CHUNK_SIZE = 15
    for chunk_start in range(0, len(top_results), CHUNK_SIZE):
        chunk = top_results[chunk_start:chunk_start + CHUNK_SIZE]
        
        part_text = f" (Part {chunk_start//CHUNK_SIZE + 1})" if len(top_results) > CHUNK_SIZE else ""
        msg_lines = [f"📊 <b>{status_target} Scan Results{part_text} (Total {len(top_results)})</b>\n"]
        
        inline_keyboard = []
        
        for i, r in enumerate(chunk, chunk_start + 1):
            w = r['wallet']
            w_short = f"{w[:4]}...{w[-4:]}"
            
            # Format stats safely
            s1d = r.get('1d', {})
            s7d = r.get('7d', {})
            s30d = r.get('30d', {})
            
            msg_lines.append(f"<b>{i}. {w_short}</b>")
            msg_lines.append(f"  • 1d:  WR {s1d.get('winrate', 0):.0f}% | {s1d.get('trades', 0)} trds | ${s1d.get('realized', 0):.0f}")
            msg_lines.append(f"  • 7d:  WR {s7d.get('winrate', 0):.0f}% | {s7d.get('trades', 0)} trds | ${s7d.get('realized', 0):.0f}")
            msg_lines.append(f"  • 30d: WR {s30d.get('winrate', 0):.0f}% | {s30d.get('trades', 0)} trds | ${s30d.get('realized', 0):.0f}")
            msg_lines.append("")
            
            # Add buttons for this wallet
            if status_target == "WHITELIST":
                inline_keyboard.append([
                    {"text": f"➖ Move {w_short} to Neutral", "callback_data": f"rem_wl_{w}"},
                    {"text": f"🛑 Blacklist {w_short}", "callback_data": f"prompt_bl_{w}"}
                ])
            else:
                inline_keyboard.append([
                    {"text": f"➕ Add {w_short} to Whitelist", "callback_data": f"add_wl_{w}"}
                ])
        
        # Only append footer on the last chunk
        if chunk_start + CHUNK_SIZE >= len(top_results):
            if status_target == "WHITELIST":
                msg_lines.append("<i>Click a button below to move a wallet to Neutral or Blacklist.</i>")
            else:
                msg_lines.append("<i>Click an Add button below to instantly move the wallet to the Whitelist.</i>")

        
        reply_markup = {"inline_keyboard": inline_keyboard}
        
        send_message("\n".join(msg_lines), reply_markup=reply_markup)
        
        # Sleep slightly between chunks to avoid Telegram API rate limits for sending messages
        if chunk_start + CHUNK_SIZE < len(top_results):
            time.sleep(1.0)

if __name__ == "__main__":
    main()


