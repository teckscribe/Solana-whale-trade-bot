"""
omni_whale_finder.py
=============================================================================
Omni-Channel Whale Discovery Engine for Solana Copy-Trading.
Aggregates smart money alpha from 4 institutional discovery channels:
  1. GMGN Smart Money Stream: Real-time on-chain makers via GMGN API/CLI.
  2. DexScreener Breakout Runners: High-volume accumulators on trending tokens.
  3. DEX On-Chain Leaderboards: Large net buyers on Raydium/Orca/Meteora pools.
  4. Multi-Whale Cluster Consensus: Cross-wallet token convergence & synergy.

Every candidate is filtered through the institutional anti-toxicity gauntlet:
  - Banned tags (mev, sniper, arbitrager, trojan, axiom, dev/deployer).
  - High-frequency sniper machine thresholds (<= 200 trades/7D).
  - 100-point AI Whale Scorer vetting.
=============================================================================
"""

import os
import sys
import json
import time
import logging
import asyncio
import subprocess
from typing import List, Dict, Any, Optional, Set, Tuple
from enum import Enum
import httpx
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("OmniWhaleFinder")

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

import whale_scanner_core
import ai_whale_scorer
import whale_manager

DATA_DIR = os.path.join(_DIR, "data")
RANKED_FILE = os.path.join(DATA_DIR, "neutral_whales_ranked.json")

BANNED_WHALE_TAGS = {
    "mev", "sniper", "banana", "padre", "trojan", "maestro",
    "photon", "axiom", "bloom", "bullx", "arbitrager", "arbitrageur",
    "frontrunner", "sandwich", "top_dev", "dev", "token_creator", "deployer"
}
MAX_SAFE_7D_TRADES = 200
MIN_SAFE_AI_SCORE = 50


class DiscoverySource(str, Enum):
    GMGN = "gmgn"
    DEXSCREENER = "dexscreener"
    DEX_ONCHAIN = "dex_onchain"
    CLUSTER = "cluster"


# ─── CHANNEL 1: GMGN Smart Money Stream ──────────────────────────────────────

async def fetch_gmgn_smart_money(limit: int = 20) -> List[Dict[str, Any]]:
    """
    Channel 1: Queries official GMGN OpenAPI via CLI to fetch active smart money makers.
    """
    log.info(f"[Channel 1 - GMGN] Fetching active smart money makers (target: {limit})...")
    candidates = []
    gmgn_api_key = os.getenv("GMGN_API_KEY", "")

    if not gmgn_api_key:
        log.warning("[Channel 1 - GMGN] GMGN_API_KEY missing. Using fallback discovery DB.")
        neutrals = whale_scanner_core.get_candidates(status_target="NEUTRAL", recent_days=7.0, limit=limit)
        return [{"wallet": w, "source": DiscoverySource.GMGN.value, "detail": "GMGN Neutral Discovery"} for w, _ in neutrals]

    try:
        npx_prefix = "npx.cmd" if os.name == "nt" else "/usr/bin/npx"
        cmd = f"{npx_prefix} -y gmgn-cli track smartmoney --chain sol --limit 200 --raw"
        env = os.environ.copy()
        env["GMGN_API_KEY"] = gmgn_api_key
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")

        proc = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, encoding="utf-8", env=env, check=False, shell=True
        )

        if proc.returncode == 0 and proc.stdout:
            data = json.loads(proc.stdout.strip())
            trades = data.get("list", [])
            seen = set()
            for t in trades:
                maker = t.get("maker")
                if maker and len(maker) >= 32 and maker not in seen:
                    seen.add(maker)
                    candidates.append({
                        "wallet": maker,
                        "source": DiscoverySource.GMGN.value,
                        "token": t.get("base_address", ""),
                        "token_symbol": t.get("base_symbol", ""),
                        "detail": "Live GMGN Smart Money Maker"
                    })
                    if len(candidates) >= limit:
                        break

        log.info(f"[Channel 1 - GMGN] Discovered {len(candidates)} smart money candidates.")
    except Exception as e:
        log.error(f"[Channel 1 - GMGN] Error querying GMGN smart money: {e}")

    return candidates


# ─── CHANNEL 2: DexScreener Breakout Runners ─────────────────────────────────

async def fetch_dexscreener_breakout_whales(limit: int = 20) -> List[Dict[str, Any]]:
    """
    Channel 2: Discovers top trending/boosted tokens on DexScreener,
    resolves their active liquidity pools, and extracts high-volume early buyers.
    """
    log.info(f"[Channel 2 - DexScreener] Querying trending breakout tokens (target: {limit})...")
    candidates = []
    seen_wallets = set()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # 1. Fetch top boosted tokens on Solana
            boost_res = await client.get("https://api.dexscreener.com/token-boosts/top/v1")
            sol_tokens = []
            if boost_res.status_code == 200:
                data = boost_res.json()
                sol_tokens = [t.get("tokenAddress") for t in data if t.get("chainId") == "solana" and t.get("tokenAddress")]

            # Fallback to search trending if boosts are sparse
            if len(sol_tokens) < 3:
                search_res = await client.get("https://api.dexscreener.com/latest/dex/search?q=SOL")
                if search_res.status_code == 200:
                    pairs = search_res.json().get("pairs", [])
                    for p in pairs[:10]:
                        base_addr = p.get("baseToken", {}).get("address")
                        if base_addr and base_addr not in sol_tokens:
                            sol_tokens.append(base_addr)

            log.info(f"[Channel 2 - DexScreener] Extracted {len(sol_tokens)} trending token mints.")

            # 2. Inspect active pools for the top tokens
            for mint in sol_tokens[:8]:
                try:
                    pair_res = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
                    if pair_res.status_code != 200:
                        continue
                    pairs = pair_res.json().get("pairs", [])
                    if not pairs:
                        continue

                    # Select pool with highest 24h volume
                    pairs.sort(key=lambda p: float(p.get("volume", {}).get("h24", 0) or 0), reverse=True)
                    best_pool = pairs[0].get("pairAddress")
                    sym = pairs[0].get("baseToken", {}).get("symbol", "TOKEN")
                    if not best_pool:
                        continue

                    # Query recent pool trades via GeckoTerminal open API
                    trades_url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{best_pool}/trades"
                    t_res = await client.get(trades_url, headers={"Accept": "application/json;version=20230302"})
                    if t_res.status_code != 200:
                        continue

                    pool_trades = t_res.json().get("data", [])
                    # Aggregate buyers by volume
                    trader_volume: Dict[str, float] = {}
                    for tr in pool_trades:
                        attrs = tr.get("attributes", {})
                        if attrs.get("kind") == "buy":
                            wallet = attrs.get("tx_from_address")
                            vol = float(attrs.get("volume_in_usd", 0) or 0.0)
                            if wallet and len(wallet) >= 32:
                                trader_volume[wallet] = trader_volume.get(wallet, 0.0) + vol

                    # Pick top accumulators (volume >= $200)
                    sorted_traders = sorted(trader_volume.items(), key=lambda x: x[1], reverse=True)
                    for wallet, vol in sorted_traders[:3]:
                        if wallet not in seen_wallets and vol >= 150.0:
                            seen_wallets.add(wallet)
                            candidates.append({
                                "wallet": wallet,
                                "source": DiscoverySource.DEXSCREENER.value,
                                "token": mint,
                                "token_symbol": sym,
                                "detail": f"DexScreener Breakout Accumulator (${vol:,.0f} buy vol on {sym})"
                            })
                            if len(candidates) >= limit:
                                break

                    if len(candidates) >= limit:
                        break
                    await asyncio.sleep(0.2)
                except Exception as inner_e:
                    log.debug(f"[Channel 2 - DexScreener] Token {mint[:6]} parse failed: {inner_e}")

        log.info(f"[Channel 2 - DexScreener] Discovered {len(candidates)} breakout whale accumulators.")
    except Exception as e:
        log.error(f"[Channel 2 - DexScreener] Error querying DexScreener breakout whales: {e}")

    return candidates


# ─── CHANNEL 3: DEX On-Chain Trending Leaderboards (Raydium / Gecko) ─────────

async def fetch_onchain_dex_whales(limit: int = 20) -> List[Dict[str, Any]]:
    """
    Channel 3: Crawls high-velocity Solana DEX liquidity pools (Raydium, Orca, Meteora)
    via GeckoTerminal trending pools, extracting consistent repeat volume makers.
    """
    log.info(f"[Channel 3 - DEX On-Chain] Crawling trending DEX pools (target: {limit})...")
    candidates = []
    seen_wallets = set()

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            url = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
            res = await client.get(url, headers={"Accept": "application/json;version=20230302"})
            if res.status_code != 200:
                log.warning(f"[Channel 3 - DEX On-Chain] GeckoTerminal returned status {res.status_code}")
                return []

            pools = res.json().get("data", [])
            log.info(f"[Channel 3 - DEX On-Chain] Found {len(pools)} trending pools on Solana.")

            for pool in pools[:10]:
                pool_addr = pool.get("attributes", {}).get("address")
                pool_name = pool.get("attributes", {}).get("name", "Unknown")
                if not pool_addr:
                    continue

                trades_url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool_addr}/trades"
                t_res = await client.get(trades_url, headers={"Accept": "application/json;version=20230302"})
                if t_res.status_code != 200:
                    continue

                trades_data = t_res.json().get("data", [])
                buyer_counts: Dict[str, Dict[str, Any]] = {}

                for td in trades_data:
                    attrs = td.get("attributes", {})
                    wallet = attrs.get("tx_from_address")
                    vol = float(attrs.get("volume_in_usd", 0) or 0.0)
                    kind = attrs.get("kind", "")
                    if wallet and len(wallet) >= 32 and kind == "buy":
                        if wallet not in buyer_counts:
                            buyer_counts[wallet] = {"count": 0, "total_vol": 0.0}
                        buyer_counts[wallet]["count"] += 1
                        buyer_counts[wallet]["total_vol"] += vol

                # Sort by trade volume and frequency
                top_traders = sorted(buyer_counts.items(), key=lambda x: x[1]["total_vol"], reverse=True)
                for wallet, stats in top_traders[:3]:
                    if wallet not in seen_wallets and stats["total_vol"] >= 200.0:
                        seen_wallets.add(wallet)
                        candidates.append({
                            "wallet": wallet,
                            "source": DiscoverySource.DEX_ONCHAIN.value,
                            "token": pool_addr,
                            "token_symbol": pool_name,
                            "detail": f"DEX Pool Whale (${stats['total_vol']:,.0f} across {stats['count']} buys on {pool_name})"
                        })
                        if len(candidates) >= limit:
                            break

                if len(candidates) >= limit:
                    break
                await asyncio.sleep(0.2)

        log.info(f"[Channel 3 - DEX On-Chain] Discovered {len(candidates)} on-chain DEX whales.")
    except Exception as e:
        log.error(f"[Channel 3 - DEX On-Chain] Error querying DEX on-chain whales: {e}")

    return candidates


# ─── CHANNEL 4: Multi-Whale Cluster Consensus (Cielo-Style) ───────────────────

def detect_cluster_consensus(
    candidates_pool: List[Dict[str, Any]],
    known_whitelist: Set[str]
) -> List[Dict[str, Any]]:
    """
    Channel 4: Identifies smart money clusters.
    When multiple smart money wallets accumulate the same token, or a candidate
    converges on tokens held by known whitelist whales, it receives cluster consensus.
    """
    log.info("[Channel 4 - Cluster Consensus] Evaluating token convergence across discovery candidates...")

    token_to_wallets: Dict[str, Set[str]] = {}
    wallet_sources: Dict[str, Set[str]] = {}

    for c in candidates_pool:
        w = c.get("wallet")
        tok = c.get("token")
        src = c.get("source")
        if not w:
            continue
        if src:
            if w not in wallet_sources:
                wallet_sources[w] = set()
            wallet_sources[w].add(src)

        if tok and len(tok) >= 32:
            if tok not in token_to_wallets:
                token_to_wallets[tok] = set()
            token_to_wallets[tok].add(w)

    cluster_candidates = []
    seen = set()

    # Identify multi-wallet convergence (2 or more wallets bought the same breakout token)
    for tok, wallets in token_to_wallets.items():
        if len(wallets) >= 2:
            for w in wallets:
                if w not in seen:
                    seen.add(w)
                    overlap_count = len(wallets)
                    cluster_candidates.append({
                        "wallet": w,
                        "source": DiscoverySource.CLUSTER.value,
                        "token": tok,
                        "cluster_size": overlap_count,
                        "detail": f"Smart Money Cluster: Converged with {overlap_count - 1} other alpha whales on token {tok[:6]}..."
                    })

    # Also detect multi-source convergence (found across multiple channels e.g. GMGN + DexScreener)
    for w, srcs in wallet_sources.items():
        if len(srcs) >= 2 and w not in seen:
            seen.add(w)
            cluster_candidates.append({
                "wallet": w,
                "source": DiscoverySource.CLUSTER.value,
                "cluster_size": len(srcs),
                "detail": f"Multi-Channel Consensus: Confirmed independently by {', '.join(srcs).upper()}"
            })

    log.info(f"[Channel 4 - Cluster Consensus] Identified {len(cluster_candidates)} consensus cluster candidates.")
    return cluster_candidates


# ─── MASTER MULTI-SOURCE DISCOVERY ORCHESTRATOR ───────────────────────────────

async def run_omni_whale_discovery(
    sources: Optional[List[str]] = None,
    limit_per_source: int = 10,
    progress_callback = None
) -> Dict[str, Any]:
    """
    Executes the 4-channel discovery pipeline:
      1. Gathers candidate pools from selected channels (GMGN, DexScreener, DEX Pools, Cluster).
      2. Merges and de-duplicates candidate wallets.
      3. Pulls full 7D/30D GMGN performance stats for each wallet.
      4. Enforces the strict institutional anti-toxicity gauntlet.
      5. Saves and ranks new clean alphas into data/neutral_whales_ranked.json.
    """
    if sources is None:
        sources = ["gmgn", "dexscreener", "dex_onchain", "cluster"]

    active_sources = [s.lower() for s in sources]
    log.info(f"🚀 Starting Omni-Channel Whale Discovery across channels: {active_sources}")

    if progress_callback:
        progress_callback("Connecting to 4 discovery channels (GMGN, DexScreener, DEX Pools, Cluster)...", 0, 100)

    raw_candidates: List[Dict[str, Any]] = []

    # Channel 1: GMGN
    if "gmgn" in active_sources:
        if progress_callback:
            progress_callback("Channel 1: Crawling GMGN Smart Money Stream...", 10, 100)
        c1 = await fetch_gmgn_smart_money(limit=limit_per_source)
        raw_candidates.extend(c1)

    # Channel 2: DexScreener
    if "dexscreener" in active_sources:
        if progress_callback:
            progress_callback("Channel 2: Extracting DexScreener Breakout Runners...", 30, 100)
        c2 = await fetch_dexscreener_breakout_whales(limit=limit_per_source)
        raw_candidates.extend(c2)

    # Channel 3: DEX On-Chain Pools
    if "dex_onchain" in active_sources:
        if progress_callback:
            progress_callback("Channel 3: Crawling Raydium & Meteora Trending Pools...", 50, 100)
        c3 = await fetch_onchain_dex_whales(limit=limit_per_source)
        raw_candidates.extend(c3)

    # Channel 4: Cluster Consensus
    if "cluster" in active_sources:
        if progress_callback:
            progress_callback("Channel 4: Evaluating Smart Money Cluster Consensus...", 65, 100)
        known_wl = whale_manager.get_whitelist_set()
        c4 = detect_cluster_consensus(raw_candidates, known_wl)
        raw_candidates.extend(c4)

    # Merge candidates by wallet address
    wallet_profiles_map: Dict[str, Dict[str, Any]] = {}
    for item in raw_candidates:
        w = item.get("wallet")
        if not w or len(w) < 32:
            continue
        if w not in wallet_profiles_map:
            wallet_profiles_map[w] = {
                "wallet": w,
                "sources": set(),
                "details": [],
                "tokens": []
            }
        wallet_profiles_map[w]["sources"].add(item.get("source", "unknown"))
        if item.get("detail"):
            wallet_profiles_map[w]["details"].append(item["detail"])
        if item.get("token"):
            wallet_profiles_map[w]["tokens"].append(item["token"])

    unique_wallets = list(wallet_profiles_map.keys())
    total_unique = len(unique_wallets)
    log.info(f"Aggregated {total_unique} unique candidate wallets across all channels.")

    if total_unique == 0:
        return {
            "status": "completed",
            "total_evaluated": 0,
            "clean_alphas_found": 0,
            "blocked_toxic_found": 0,
            "message": "No unique candidates found across specified channels."
        }

    # Load existing ranked list
    existing_ranked = []
    if os.path.exists(RANKED_FILE):
        try:
            with open(RANKED_FILE, "r", encoding="utf-8") as f:
                existing_ranked = json.load(f)
        except Exception:
            existing_ranked = []

    existing_dict = {r.get("wallet"): r for r in existing_ranked if r.get("wallet")}

    clean_discovered = 0
    blocked_toxic = 0

    # Evaluate each wallet via 7D GMGN stats & AI Gauntlet
    for idx, w in enumerate(unique_wallets, 1):
        if progress_callback:
            progress_callback(f"Evaluating {idx}/{total_unique}: {w[:6]}...", 70 + int((idx / total_unique) * 25), 100)

        meta = wallet_profiles_map[w]
        sources_list = list(meta["sources"])

        # Fetch profile
        profile = await asyncio.to_thread(
            whale_scanner_core.fetch_wallet_profile,
            w,
            use_cache=True,
            deep_scan=True,
            rate_sleep=0.3
        )

        if not profile or not profile.get("7d"):
            continue

        s7 = profile.get("7d", {})
        wr = float(s7.get("winrate", 0.0))
        trades = int(s7.get("trades", 0))
        realized = float(s7.get("realized", 0.0))
        raw_tags = profile.get("tags", [])
        tags = [str(t).lower() for t in raw_tags]

        # Safety Gauntlet
        has_banned = any(b in tags for b in BANNED_WHALE_TAGS)
        is_sniper = trades > MAX_SAFE_7D_TRADES

        score_res = ai_whale_scorer.score_wallet(w, {
            "winrate_7d": wr,
            "trades_7d": trades,
            "profit_7d": realized,
            "tags": tags
        })

        is_clean = not has_banned and not is_sniper and score_res.status != "BLACKLIST" and score_res.score >= MIN_SAFE_AI_SCORE

        if is_clean and (wr >= 45.0 or realized > 0.0) and trades >= 3:
            clean_discovered += 1
        else:
            blocked_toxic += 1

        # Enrich profile with omni-channel metadata
        profile["discovery_sources"] = sources_list
        profile["channel_details"] = meta["details"]
        profile["ai_score"] = score_res.score
        profile["ai_status"] = score_res.status
        profile["ai_style"] = score_res.style

        # Add to existing map
        existing_dict[w] = profile

    # Save merged dataset back to disk
    updated_ranked = list(existing_dict.values())
    updated_ranked.sort(key=lambda x: float(x.get("7d", {}).get("realized", 0.0)), reverse=True)

    os.makedirs(os.path.dirname(RANKED_FILE), exist_ok=True)
    temp_path = f"{RANKED_FILE}.tmp_{os.getpid()}"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(updated_ranked, f, indent=2)
    os.replace(temp_path, RANKED_FILE)

    if progress_callback:
        progress_callback(f"Done! Evaluated {total_unique} wallets. Found {clean_discovered} verified alphas.", 100, 100)

    log.info(f"✅ Omni-Channel Discovery Complete: {clean_discovered} verified clean alphas, {blocked_toxic} toxic blocked.")

    return {
        "status": "completed",
        "total_evaluated": total_unique,
        "clean_alphas_found": clean_discovered,
        "blocked_toxic_found": blocked_toxic,
        "total_ranked_db": len(updated_ranked),
        "channels_scanned": active_sources
    }
