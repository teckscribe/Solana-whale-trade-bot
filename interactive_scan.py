"""
=========================================================
interactive_scan.py (GMGN Interactive Whale Scanner)
=========================================================

Short Brief:
This module runs in the background as a standalone script triggered by the Telegram bot
or CLI. It evaluates GMGN performance metrics for a specific list of whales (NEUTRAL,
BLACKLIST, or WHITELIST).

Key Features:
- 2-Tier Fast Evaluation: checks 7d first to eliminate inactive/negative PnL wallets,
  speeding up broad scans by 3x-5x.
- Persistent Disk Caching: saves queries to data/whale_scan_cache.json with 12h TTL.
- Chronological Sorting: evaluates most recently active whales first.
- No 10-Whale Limit: delivers top results in readable Telegram chunks (up to Top 30)
  with instant inline whitelist buttons.
- Saves full ranked results to data/neutral_whales_ranked.json.
"""

import sys
import os
import time
import argparse
from dotenv import load_dotenv

load_dotenv()

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

import whale_manager
import whale_scanner_core as scanner
from telegram_notifier import send_message


def main():
    parser = argparse.ArgumentParser(description="Interactive Whale Scanner for Telegram Bot and Background Jobs")
    parser.add_argument("status", nargs="?", default="NEUTRAL", help="Target status: NEUTRAL, WHITELIST, or BLACKLIST")
    parser.add_argument("--top", type=int, default=30, help="Maximum results to return in Telegram messages (default: 30)")
    parser.add_argument("--recent-days", type=float, default=None, help="Filter by last active days (default: 7.0 for neutral, None for whitelist/blacklist)")
    parser.add_argument("--limit", type=int, default=150, help="Max candidates to evaluate in this run (default: 150)")
    parser.add_argument("--min-wr", type=float, default=50.0, help="Minimum 7d winrate percentage (default: 50.0)")
    parser.add_argument("--min-profit", type=float, default=0.0, help="Minimum 7d realized profit (default: 0.0)")

    args, _ = parser.parse_known_args()
    status_target = args.status.upper()

    # If recent-days not explicitly specified for NEUTRAL, default to 14 days to keep scan responsive
    recent_days = args.recent_days
    if status_target == "NEUTRAL" and recent_days is None:
        recent_days = 14.0

    candidates = scanner.get_candidates(
        status_target=status_target,
        recent_days=recent_days,
        limit=args.limit
    )

    if not candidates:
        send_message(f"ℹ️ No {status_target} whales found matching scan criteria.")
        return

    send_message(
        f"⏳ <b>Starting {status_target} Whale Scan</b>\n"
        f"Evaluating up to {len(candidates)} recently active candidates via GMGN.\n"
        f"<i>Results will arrive automatically once analysis completes.</i>"
    )

    profiles = []
    for idx, (w, info) in enumerate(candidates, 1):
        try:
            profile = scanner.fetch_wallet_profile(w, use_cache=True, deep_scan=True, rate_sleep=1.0)
            profiles.append(profile)
        except Exception:
            pass

    # Filter and rank
    ranked = scanner.rank_and_filter_whales(
        profiles,
        min_wr=args.min_wr,
        min_trades=3,
        min_profit=args.min_profit,
        max_trades=300,
        filter_banned_tags=True
    )

    # If no whales passed strict filter, fallback to any wallets with positive profit or trades
    if not ranked:
        for p in profiles:
            s7d = p.get("7d", {})
            if s7d.get("trades", 0) > 0:
                p["alpha_score"] = s7d.get("winrate", 0)
                p["winrate_7d"] = s7d.get("winrate", 0)
                p["trades_7d"] = s7d.get("trades", 0)
                p["realized_7d"] = s7d.get("realized", 0.0)
                ranked.append(p)
        ranked.sort(key=lambda x: (x.get("winrate_7d", 0), x.get("realized_7d", 0)), reverse=True)

    # Save to disk
    if ranked:
        scanner.export_ranked_json(ranked)

    top_results = ranked[:args.top]

    if not top_results:
        send_message(f"Scan complete. No active {status_target} whales with trade history found.")
        return

    # Build Telegram Message in chunks of 10
    CHUNK_SIZE = 10
    total_parts = (len(top_results) + CHUNK_SIZE - 1) // CHUNK_SIZE

    for chunk_idx in range(0, len(top_results), CHUNK_SIZE):
        chunk = top_results[chunk_idx:chunk_idx + CHUNK_SIZE]
        part_num = (chunk_idx // CHUNK_SIZE) + 1
        part_header = f" (Part {part_num}/{total_parts})" if total_parts > 1 else ""

        msg_lines = [
            f"🐋 <b>{status_target} Whale Scan Results{part_header}</b>",
            f"<i>Showing Top {len(top_results)} of {len(candidates)} scanned</i>\n"
        ]

        inline_keyboard = []

        for i, r in enumerate(chunk, chunk_idx + 1):
            w = r.get("wallet", "")
            w_short = f"{w[:4]}...{w[-4:]}"

            s1d = r.get("1d", {})
            s7d = r.get("7d", {})
            s30d = r.get("30d", {})
            sol_bal = r.get("native_balance", 0.0)
            score = r.get("alpha_score", 0.0)

            sign7d = "+" if s7d.get("realized", 0) >= 0 else ""
            msg_lines.append(f"<b>{i}. <code>{w}</code></b>")
            msg_lines.append(f"  • <b>7d:</b>  WR {s7d.get('winrate', 0):.0f}% | {s7d.get('trades', 0)} trds | {sign7d}${s7d.get('realized', 0):,.0f}")
            msg_lines.append(f"  • <b>30d:</b> WR {s30d.get('winrate', 0):.0f}% | {s30d.get('trades', 0)} trds | ${s30d.get('realized', 0):,.0f}")
            msg_lines.append(f"  • <b>SOL:</b> {sol_bal:.2f} SOL | <b>Score:</b> {score:.1f}")
            msg_lines.append("")

            # Action buttons
            if status_target == "WHITELIST":
                inline_keyboard.append([
                    {"text": f"➖ Move {w_short} to Neutral", "callback_data": f"rem_wl_{w}"},
                    {"text": f"🛑 Blacklist {w_short}", "callback_data": f"prompt_bl_{w}"}
                ])
            else:
                inline_keyboard.append([
                    {"text": f"➕ Whitelist {w_short} (WR {s7d.get('winrate', 0):.0f}%)", "callback_data": f"add_wl_{w}"}
                ])

        if chunk_idx + CHUNK_SIZE >= len(top_results):
            if status_target != "WHITELIST":
                msg_lines.append("<i>Click any button above to immediately add to your live Whitelist.</i>")

        reply_markup = {"inline_keyboard": inline_keyboard} if inline_keyboard else None
        send_message("\n".join(msg_lines), reply_markup=reply_markup)

        if chunk_idx + CHUNK_SIZE < len(top_results):
            time.sleep(1.2)


if __name__ == "__main__":
    main()
