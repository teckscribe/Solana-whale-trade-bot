"""
apply_whitelist_hygiene.py
=========================================================
Executes automated whitelist hygiene:
1. Purges and blacklists the 16 identified toxic loss-making whales.
2. Selects and whitelists the Top 100 highest-performing Alpha whales
   discovered from the neutral scan.
3. Synchronizes whitelist.json and discovery_db.json atomically.
=========================================================
"""

import sys
import os
import json
import logging

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

logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
log = logging.getLogger("WhitelistHygiene")

# 1. Toxic whales to remove from Whitelist and Blacklist
TOXIC_WHALES = [
    "5GkwALoiXyGf8m6ccEhcwMUdjXMvi1Am2QuGcZeqsKzQ",
    "FeTfKvSgVzvnzu2EWaQqwgLTP5XAjxs1KDyMiXuWh1FR",
    "FpKnR8uayCm87Uu23SSvjKk28idq43tBbwfzgvEdCCx5",
    "GdpfRuN7CAaVQPczsqZ3SQMUv436bTHncLrMu2LbQEqR",
    "FfatC6tnc9kmSFP2juLedW8zBFApv64o5syH1C5iPXS7",
    "CUpvKWRBbzxfbXwwz4sVex3cYnwm3YG8WHnP18ifTj5x",
    "FRj7jSkViTCB5hm6aGbfYhGhCKdFPreXZxm5fC7sWeMz",
    "2Fem2yW7SvMLXTA2aMGviu39KmM8zZyVATvjughNMPBn",
    "6KrWrAJLRQTKyLTsLY7bJkGPt7RTK1FfFQZv14pu5PdJ",
    "E1cAo2NZYbrQMpsbnGeSz92J6ohFxGqrhfPsCXphrAiG",
    "4nBNtRX6Q3NAhtcaXfV5R2okHr2Cd6iur7EDQvNedujV",
    "HyYNVYmnFmi87NsQqWzLJhUTPBKQUfgfhdbBa554nMFF",
    "2yavzzNeqWjtUBXYR7Fx8RhdbWNHDfRcRhx6VuexLFjT",
    "6i88unHYdJqBr5Mh18cQQJHwjV5KVmdv2AQm9VCjQYpb",
    "HP1RWVj9rmt178W5dPT1Ujjt46spf69bJ6UXaAUD6sJx",
    "5SFgzLhLpjGKcm3iMaG3jFJS4fUVruHYQiphiBrQd2Qb"
]


def main():
    print("=" * 75)
    print(" [*] SOLANA WHALE TRACKER BOT -- WHITELIST HYGIENE & ALPHA PROMOTION")
    print("=" * 75)

    initial_wl_count = len(whale_manager.get_whitelist())
    print(f"📊 Initial Whitelist Count: {initial_wl_count} wallets")

    # Step 1: Blacklist toxic whales
    print("\n🛑 STEP 1: Removing & Blacklisting 16 Toxic Whales...")
    removed_toxic = 0
    for w in TOXIC_WHALES:
        if whale_manager.set_whale_status(w, "BLACKLIST"):
            removed_toxic += 1
            print(f"  [-] Blacklisted toxic whale: {w[:8]}...{w[-6:]}")
        else:
            print(f"  [!] Failed to blacklist: {w[:8]}")

    print(f"✅ Successfully blacklisted {removed_toxic}/{len(TOXIC_WHALES)} toxic whales.")

    # Step 2: Extract top 100 neutral alpha candidates from scan cache
    print("\n🔍 STEP 2: Selecting Top 100 Alpha Whales from Neutral Scan...")
    cache = scanner.load_cache()
    current_wl = set(whale_manager.get_whitelist())

    # Get non-whitelisted candidates from cache
    neutral_candidates = [
        d for w, d in cache.items()
        if w not in current_wl and w not in TOXIC_WHALES
    ]

    # Rank with quality standards (min 35% WR, positive profit, trade count, no banned tags)
    ranked = scanner.rank_and_filter_whales(
        neutral_candidates,
        min_wr=35.0,
        min_trades=3,
        min_profit=0.0,
        max_trades=500,
        filter_banned_tags=True
    )

    # Sort by alpha score descending
    top_100_to_add = ranked[:100]
    print(f"📋 Selected Top {len(top_100_to_add)} Alpha Whales to promote.")

    # Step 3: Promote to Whitelist
    print("\n🚀 STEP 3: Promoting to Whitelist...")
    promoted_count = 0
    for idx, cand in enumerate(top_100_to_add, 1):
        target = cand["wallet"]
        if whale_manager.set_whale_status(target, "WHITELIST"):
            promoted_count += 1
            s7d = cand.get("7d", {})
            realized = s7d.get("realized", 0.0)
            wr = s7d.get("winrate", 0.0)
            if idx <= 15 or idx % 20 == 0 or idx == len(top_100_to_add):
                print(f"  [+] [{idx:3d}/100] Whitelisted: {target[:8]}...{target[-6:]} | 7d WR: {wr:>5.1f}% | Profit: +${realized:>8,.0f}")

    print(f"\n✅ Successfully promoted {promoted_count}/{len(top_100_to_add)} Alpha whales to Whitelist.")

    # Final stats
    final_wl_count = len(whale_manager.get_whitelist())
    final_bl_count = len(whale_manager.get_blacklist())
    print("\n" + "=" * 75)
    print(" 🏁 WHITELIST HYGIENE SUMMARY")
    print("=" * 75)
    print(f"  • Previous Whitelist Count : {initial_wl_count}")
    print(f"  • Toxic Whales Blacklisted : -{removed_toxic}")
    print(f"  • Top Alpha Whales Added   : +{promoted_count}")
    print(f"  • New Active Whitelist Size: {final_wl_count}")
    print(f"  • Total Blacklisted Whales : {final_bl_count}")
    print("=" * 75)


if __name__ == "__main__":
    main()
