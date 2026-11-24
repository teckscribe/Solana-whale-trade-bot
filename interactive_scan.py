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
    from gmgn_discovery import cross_check_wallet_parameters
    for idx, (w, info) in enumerate(candidates, 1):
        try:
            profile = scanner.fetch_wallet_profile(w, use_cache=True, deep_scan=False, rate_sleep=1.5)
            s7 = profile.get("7d", {})
            if not s7:
                continue
            passed, rejections, stats = cross_check_wallet_parameters(w, {
                "address": w,
                "winrate_7d": float(s7.get("winrate", 0.0) or 0.0),
                "total_trades_7d": int(s7.get("trades", 0) or 0),
                "buy_7d": int(s7.get("buy", 0) or 0),
                "sell_7d": int(s7.get("sell", 0) or 0),
                "token_num_7d": int(s7.get("token_num", 0) or 0),
                "realized_profit": float(s7.get("realized", 0.0) or 0.0),
                "roi_pct": float(s7.get("roi_pct", 0.0) or 0.0),
                "bought_cost": float(s7.get("bought_cost", 0.0) or 0.0),
                "bought_fee": float(s7.get("bought_fee", 0.0) or 0.0),
                "fee_to_profit_pct": float(s7.get("fee_to_profit_pct", 0.0) or 0.0),
                "runners_2x_plus": int(s7.get("runners_2x_plus", 0) or 0),
                "sol_balance": float(profile.get("native_balance", s7.get("native_balance", 0.0)) or 0.0),
                "tags": list(profile.get("tags", s7.get("tags", [])) or []),
                "raw_7d": s7,
            })
            profile["passed_filters"] = passed
            profile["rejections"] = rejections
            profile["alpha_score"] = float(stats.get("ai_score", 0.0) or 0.0)
            profile["winrate_7d"] = float(s7.get("winrate", 0.0) or 0.0)
            profile["trades_7d"] = int(s7.get("trades", 0) or 0)
            profile["realized_7d"] = float(s7.get("realized", 0.0) or 0.0)
            if status_target == "WHITELIST" or passed:
                profiles.append(profile)
        except Exception:
            pass

    ranked = sorted(
        profiles,
        key=lambda x: (
            bool(x.get("passed_filters")),
            float(x.get("alpha_score", 0.0) or 0.0),
            float((x.get("7d") or {}).get("roi_pct", 0.0) or 0.0),
            float(x.get("realized_7d", 0.0) or 0.0),
        ),
        reverse=True,
    )

    top_results = ranked[:args.top]

    if not top_results:
        send_message(
            f"🛡️ <b>{status_target} Scan Complete</b>\n"
            f"Evaluated {len(candidates)} candidates — 0 wallets passed all configured safety & ROI parameters."
        )
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

            s7d = r.get("7d", {})
            sol_bal = r.get("native_balance", 0.0)
            score = r.get("alpha_score", 0.0)
            roi_pct = float(s7d.get("roi_pct", 0.0) or 0.0)
            fee_ratio = float(s7d.get("fee_to_profit_pct", 0.0) or 0.0)
            runners_2x = int(s7d.get("runners_2x_plus", 0) or 0)

            sign7d = "+" if s7d.get("realized", 0) >= 0 else ""
            msg_lines.append(f"<b>{i}. <code>{w}</code></b>")
            msg_lines.append(
                f"  • <b>7D:</b> WR {s7d.get('winrate', 0):.0f}% | {s7d.get('trades', 0)} swaps | "
                f"{sign7d}${s7d.get('realized', 0):,.0f} (<b>ROI {roi_pct:+.1f}%</b>)"
            )
            msg_lines.append(
                f"  • <b>SOL:</b> {sol_bal:.2f} | <b>2x+ Runners:</b> {runners_2x} | "
                f"<b>Fee/PnL:</b> {fee_ratio:.1f}% | <b>AI Score:</b> {score:.0f}"
            )
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
