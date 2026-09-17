"""
scan_neutral_whales.py
=========================================================
CLI Alpha Whale Discovery Tool for Solana Whale Tracker Bot.
Scans neutral whales, evaluates performance via GMGN OpenAPI,
identifies alpha copy-trading candidates, and allows direct
promotion to Whitelist.
=========================================================
"""

import sys
import os
import argparse
import time

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

import whale_manager
import whale_scanner_core as scanner

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def print_banner():
    print("=" * 78)
    print(" [*] SOLANA WHALE TRACKER BOT -- NEUTRAL WHALE SCANNER & ALPHA DISCOVERY")
    print("=" * 78)


def main():
    parser = argparse.ArgumentParser(
        description="Scan neutral whales to identify top-performing alpha traders."
    )
    parser.add_argument(
        "--status", type=str, default="NEUTRAL",
        help="Target database to scan: WHITELIST, NEUTRAL, or BLACKLIST (default: NEUTRAL)."
    )
    parser.add_argument(
        "--all", action="store_true", help="Scan all whales regardless of last active date."
    )
    parser.add_argument(
        "--recent-days", type=float, default=None,
        help="Only scan whales active in the last N days (e.g. 7 for last week, 30 for last month)."
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum number of candidate wallets to evaluate (e.g. 50 or 100)."
    )
    parser.add_argument(
        "--top", type=int, default=25,
        help="Number of top-ranked whales to display in the terminal table (default: 25)."
    )
    parser.add_argument(
        "--min-wr", type=float, default=40.0,
        help="Minimum 7-day win rate percentage to qualify as alpha (default: 40.0%%)."
    )
    parser.add_argument(
        "--min-profit", type=float, default=0.0,
        help="Minimum 7-day realized profit in USD to qualify as alpha (default: $0)."
    )
    parser.add_argument(
        "--min-trades", type=int, default=3,
        help="Minimum 7-day trade count to qualify (default: 3)."
    )
    parser.add_argument(
        "--max-trades", type=int, default=350,
        help="Maximum 7-day trade count to reject high-frequency sniper bots (default: 350)."
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Force fresh GMGN API calls, ignoring persistent disk cache."
    )
    parser.add_argument(
        "--auto-whitelist", type=int, default=0,
        help="Automatically promote the Top N qualifying alpha whales directly into Whitelist."
    )
    parser.add_argument(
        "--export-csv", type=str, default=None,
        help="Path to export ranked CSV results."
    )
    parser.add_argument(
        "--export-json", type=str, default=None,
        help="Path to export ranked JSON results."
    )

    args = parser.parse_args()
    status_target = args.status.upper()

    print_banner()

    # Determine filter
    recent_days = args.recent_days
    if not args.all and recent_days is None:
        if status_target == "NEUTRAL":
            recent_days = 7.0
            print(f"ℹ️  Filtering to wallets active in the last {recent_days:.0f} days. (Use --all to scan entire database)")
        else:
            recent_days = None  # Default to scanning all whitelist wallets

    print(f"🔍 Discovering {status_target} candidates in database...")
    candidates = scanner.get_candidates(
        status_target=status_target,
        recent_days=recent_days,
        limit=args.limit
    )

    total_candidates = len(candidates)
    if total_candidates == 0:
        print(f"❌ No {status_target} candidates matched the specified filter criteria.")
        return

    print(f"📋 Found {total_candidates} {status_target} candidates to evaluate (sorted by recent activity).")
    print(f"⚙️  Filters: Min WR: {args.min_wr}% | Min Profit: ${args.min_profit} | Min Trades: {args.min_trades} | Max Trades: {args.max_trades}")
    print("-" * 78)

    cache = scanner.load_cache() if not args.no_cache else {}
    cached_count = sum(1 for w, _ in candidates if w in cache)
    print(f"⚡ Disk Cache: {cached_count}/{total_candidates} already cached locally.")
    print("-" * 78)

    profiles = []
    start_time = time.time()

    for idx, (wallet, info) in enumerate(candidates, 1):
        w_short = f"{wallet[:4]}...{wallet[-4:]}"
        is_cached = (wallet in cache) and not args.no_cache
        cache_marker = "[CACHE]" if is_cached else "[LIVE] "

        sys.stdout.write(f"\r⏳ [{idx}/{total_candidates}] {cache_marker} Evaluating {w_short}...")
        sys.stdout.flush()

        try:
            profile = scanner.fetch_wallet_profile(
                wallet,
                use_cache=not args.no_cache,
                deep_scan=True,
                rate_sleep=1.0
            )
            profiles.append(profile)

            # Live alert if wallet shows strong alpha metrics
            s7d = profile.get("7d", {})
            if s7d.get("winrate", 0) >= args.min_wr and s7d.get("realized", 0) >= args.min_profit and s7d.get("trades", 0) >= args.min_trades:
                print(f"\n  ⭐ [{idx}/{total_candidates}] Alpha candidate found: {w_short} | WR: {s7d.get('winrate', 0):.0f}% | Trades: {s7d.get('trades', 0)} | Profit: +${s7d.get('realized', 0):,.0f}")
            elif idx % 25 == 0:
                print(f"\n  📊 Milestone: Scanned {idx}/{total_candidates} wallets ({(idx/total_candidates)*100:.0f}% complete)...")

        except Exception as e:
            sys.stdout.write(f"\n⚠️ Error evaluating {w_short}: {e}\n")

    sys.stdout.write("\n")
    elapsed = time.time() - start_time
    print(f"✅ Completed evaluation of {len(profiles)} wallets in {elapsed:.1f}s.")
    print("-" * 78)

    # Rank and filter
    ranked = scanner.rank_and_filter_whales(
        profiles,
        min_wr=args.min_wr,
        min_trades=args.min_trades,
        min_profit=args.min_profit,
        max_trades=args.max_trades,
        filter_banned_tags=True
    )

    print(f"🏆 Found {len(ranked)} qualifying Alpha whales meeting all quality standards.")
    print("=" * 78)

    if not ranked:
        print("⚠️ No whales passed the strict alpha filters. Try lowering --min-wr or --min-profit.")
        return

    # Display Top N Table
    top_n = min(args.top, len(ranked))
    print(f"🥇 TOP {top_n} ALPHA CANDIDATES:")
    print(f"{'Rank':<5} {'Wallet Address':<46} {'7d WR':<8} {'Trades':<8} {'Realized ($)':<14} {'SOL Bal':<10} {'Score':<8} {'Tags'}")
    print("-" * 125)

    for i, w in enumerate(ranked[:top_n], 1):
        addr = w.get("wallet", "")
        wr = w.get("winrate_7d", 0.0)
        trades = w.get("trades_7d", 0)
        realized = w.get("realized_7d", 0.0)
        sol = w.get("native_balance", 0.0)
        score = w.get("alpha_score", 0.0)
        tags = ", ".join(w.get("tags", []))[:20]

        sign = "+" if realized >= 0 else ""
        profit_str = f"{sign}${realized:,.0f}"

        print(f"{i:<5} {addr:<46} {wr:>5.1f}%  {trades:>6}   {profit_str:>12}   {sol:>8.2f}   {score:>6.1f}   {tags}")

    print("-" * 125)

    # Save exports
    export_csv = args.export_csv
    export_json = args.export_json
    if not export_csv:
        prefix = "whitelist" if status_target == "WHITELIST" else "neutral"
        export_csv = os.path.join(scanner.DATA_DIR, f"{prefix}_whales_ranked.csv")
    if not export_json:
        prefix = "whitelist" if status_target == "WHITELIST" else "neutral"
        export_json = os.path.join(scanner.DATA_DIR, f"{prefix}_whales_ranked.json")

    scanner.export_ranked_json(ranked, export_json)
    print(f"💾 Full results saved to JSON: {export_json}")

    scanner.export_ranked_csv(ranked, export_csv)
    print(f"📊 Spreadsheet exported to CSV: {export_csv}")

    # Auto-whitelist promotion
    if args.auto_whitelist > 0:
        promote_count = min(args.auto_whitelist, len(ranked))
        print("\n🚀 PROMOTING TOP ALPHA WHALES TO WHITELIST:")
        for idx in range(promote_count):
            target = ranked[idx]["wallet"]
            success = whale_manager.set_whale_status(target, "WHITELIST")
            status_text = "✅ Whitelisted" if success else "❌ Failed"
            print(f"  [{idx+1}/{promote_count}] {target} -> {status_text} (7d WR: {ranked[idx]['winrate_7d']:.1f}%, PnL: ${ranked[idx]['realized_7d']:,.0f})")
        print("Whales updated in memory and synced to whitelist.json.")


if __name__ == "__main__":
    main()
