"""
omni_whale_finder.py
=============================================================================
Omni-Channel Whale Discovery Engine for Solana Copy-Trading.
Aggregates smart money alpha from 5 institutional discovery channels:
  1. GMGN Smart Money Stream: Real-time on-chain makers via GMGN API/CLI.
  2. DexScreener Breakout Runners: High-volume accumulators on trending tokens.
  3. DEX On-Chain Leaderboards: Large net buyers on Raydium/Orca/Meteora pools.
  4. Nansen Smart Money DEX Trades: Labeled Solana smart-trader activity.
  5. Multi-Whale Cluster Consensus: Cross-wallet token convergence & synergy.

Every candidate is filtered through the institutional anti-toxicity gauntlet:
  - Banned tags (mev, sniper, arbitrager, trojan, axiom, dev/deployer).
  - High-frequency sniper machine thresholds (<= 200 trades/7D).
  - 100-point AI Whale Scorer vetting.
=============================================================================
"""

import os
import re
import sys
import json
import time
import logging
import asyncio
import subprocess
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Set, Tuple
from enum import Enum
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("OmniWhaleFinder")

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

import whale_scanner_core
import ai_whale_scorer
import whale_manager
import connection_pool

DATA_DIR = os.path.join(_DIR, "data")
RANKED_FILE = os.path.join(DATA_DIR, "neutral_whales_ranked.json")

BANNED_WHALE_TAGS = {
    "mev", "sniper", "banana", "padre", "trojan", "maestro",
    "photon", "axiom", "bloom", "bullx", "arbitrager", "arbitrageur",
    "frontrunner", "sandwich", "top_dev", "dev", "token_creator", "deployer"
}
MAX_SAFE_7D_TRADES = 200
MIN_SAFE_AI_SCORE = 50
MIN_SAFE_7D_TRADES = 3
MIN_SAFE_7D_WINRATE = 45.0
MIN_REPEAT_BUYS = 2
MIN_ACCUMULATION_SPAN_SECONDS = 30.0
CLUSTER_WINDOW_SECONDS = 60 * 60
MIN_INDEPENDENT_DISCOVERY_SOURCES = 2
INDEPENDENT_DISCOVERY_SOURCES = {"gmgn", "dexscreener", "dex_onchain", "nansen"}
ALLOWED_SOURCES = INDEPENDENT_DISCOVERY_SOURCES | {"cluster"}
SOLANA_ADDRESS_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class DiscoverySource(str, Enum):
    GMGN = "gmgn"
    DEXSCREENER = "dexscreener"
    DEX_ONCHAIN = "dex_onchain"
    CLUSTER = "cluster"
    NANSEN = "nansen"
    LOCAL_HISTORY = "local_history"


def _parse_timestamp(value: Any) -> Optional[float]:
    """Normalize unix seconds/milliseconds or ISO timestamps to unix seconds."""
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
        return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _relationship_address(resource: Dict[str, Any], name: str) -> str:
    """Extract a chain address from a JSON:API relationship id."""
    rel_id = (
        resource.get("relationships", {})
        .get(name, {})
        .get("data", {})
        .get("id", "")
    )
    if not rel_id:
        return ""
    prefix = "solana_"
    return rel_id[len(prefix):] if rel_id.startswith(prefix) else rel_id


def evaluate_wallet_safety(wallet: str, profile: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the single authoritative whitelist eligibility policy."""
    s7 = profile.get("7d", {}) or {}
    winrate = float(s7.get("winrate", 0.0) or 0.0)
    trades = int(s7.get("trades", 0) or 0)
    realized = float(s7.get("realized", 0.0) or 0.0)
    raw_tags = profile.get("tags", []) or s7.get("tags", []) or []
    tags = [str(tag).strip().lower() for tag in raw_tags]

    score = ai_whale_scorer.score_wallet(wallet, {
        "winrate_7d": winrate,
        "trades_7d": trades,
        "profit_7d": realized,
        "tags": tags,
    }, force_heuristic=True)

    rejections: List[str] = []
    banned = sorted({tag for tag in tags if tag in BANNED_WHALE_TAGS})
    if banned:
        rejections.append(f"Banned tag: {', '.join(banned)}")
    if trades > MAX_SAFE_7D_TRADES:
        rejections.append(f"Trade frequency {trades}/7d exceeds {MAX_SAFE_7D_TRADES}")
    if trades < MIN_SAFE_7D_TRADES:
        rejections.append(f"Insufficient history: {trades} trades/7d; minimum {MIN_SAFE_7D_TRADES}")
    if winrate < MIN_SAFE_7D_WINRATE:
        rejections.append(f"Win rate {winrate:.1f}% is below {MIN_SAFE_7D_WINRATE:.0f}%")
    if realized <= 0.0:
        rejections.append(f"7d realized PnL must be positive; found ${realized:,.2f}")
    if score.status == "BLACKLIST":
        rejections.append(f"AI blacklist: {', '.join(score.red_flags) or 'toxic pattern'}")
    elif score.score < MIN_SAFE_AI_SCORE:
        rejections.append(f"AI score {score.score} is below {MIN_SAFE_AI_SCORE}")

    return {
        "passed_filters": not rejections,
        "rejections": rejections,
        "ai_score": score.score,
        "ai_status": score.status,
        "ai_style": score.style,
        "ai_summary": score.summary,
        "ai_red_flags": score.red_flags,
    }


def evaluate_discovery_consensus(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Require corroboration by distinct external discovery feeds."""
    raw_sources = profile.get("discovery_sources", []) or []
    independent_sources = sorted({
        str(source).strip().lower()
        for source in raw_sources
        if str(source).strip().lower() in INDEPENDENT_DISCOVERY_SOURCES
    })
    source_count = len(independent_sources)
    passed = source_count >= MIN_INDEPENDENT_DISCOVERY_SOURCES
    rejection = ""
    if not passed:
        rejection = (
            f"Discovery consensus requires {MIN_INDEPENDENT_DISCOVERY_SOURCES} independent sources; "
            f"found {source_count}"
        )
    return {
        "source_consensus_passed": passed,
        "independent_source_count": source_count,
        "independent_sources": independent_sources,
        "source_consensus_rejection": rejection,
    }


# ─── CHANNEL 1: GMGN Smart Money Stream ──────────────────────────────────────

async def fetch_gmgn_smart_money(
    limit: int = 20,
    status_target: str = "NEUTRAL",
    recent_days: float = 7.0,
) -> List[Dict[str, Any]]:
    """
    Channel 1: Queries official GMGN OpenAPI via CLI to fetch active smart money makers.
    """
    log.info(f"[Channel 1 - GMGN] Fetching active smart money makers (target: {limit})...")
    candidates = []
    gmgn_api_key = os.getenv("GMGN_API_KEY", "")

    if not gmgn_api_key:
        log.warning("[Channel 1 - GMGN] GMGN_API_KEY missing. Using fallback discovery DB.")
        neutrals = whale_scanner_core.get_candidates(
            status_target=status_target,
            recent_days=recent_days,
            limit=limit,
        )
        return [{
            "wallet": w,
            "source": DiscoverySource.LOCAL_HISTORY.value,
            "detail": "Local discovery database fallback",
            "observed_at": time.time(),
        } for w, _ in neutrals]

    try:
        executable = shutil.which("gmgn-cli.cmd" if os.name == "nt" else "gmgn-cli")
        if not executable:
            log.error("[Channel 1 - GMGN] Installed gmgn-cli executable was not found.")
            return []
        env = os.environ.copy()
        env["GMGN_API_KEY"] = gmgn_api_key
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")

        run_options = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": env,
            "check": False,
            "shell": False,
        }
        if os.name == "nt":
            run_options["creationflags"] = subprocess.CREATE_NO_WINDOW

        config_check = await asyncio.to_thread(
            subprocess.run,
            [executable, "config", "--check"],
            timeout=15.0,
            **run_options,
        )
        if config_check.returncode != 0:
            log.error("[Channel 1 - GMGN] gmgn-cli config check failed; skipping GMGN channel.")
            return []

        proc = await asyncio.to_thread(
            subprocess.run,
            [executable, "track", "smartmoney", "--chain", "sol", "--limit", "200", "--raw"],
            timeout=30.0,
            **run_options,
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
                        "detail": "Live GMGN Smart Money Maker",
                        "observed_at": _parse_timestamp(
                            t.get("timestamp") or t.get("created_at") or t.get("block_timestamp")
                        ) or time.time(),
                    })
                    if len(candidates) >= limit:
                        break

        log.info(f"[Channel 1 - GMGN] Discovered {len(candidates)} smart money candidates.")
    except subprocess.TimeoutExpired:
        log.error("[Channel 1 - GMGN] gmgn-cli timed out; channel stopped after its safety deadline.")
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
        # 1. Fetch top boosted tokens on Solana. Boosting is paid promotion, so
        # it is only a candidate source; the safety gauntlet remains mandatory.
        data = await connection_pool.dex_get("https://api.dexscreener.com/token-boosts/top/v1")
        sol_tokens = []
        if isinstance(data, list):
            sol_tokens = [t.get("tokenAddress") for t in data if t.get("chainId") == "solana" and t.get("tokenAddress")]

        # Search is a broad fallback, not a claim that the results are trending.
        if len(sol_tokens) < 3:
            search_data = await connection_pool.dex_get(
                "https://api.dexscreener.com/latest/dex/search",
                params={"q": "SOL"},
            )
            for pair in (search_data or {}).get("pairs", [])[:10]:
                base_addr = pair.get("baseToken", {}).get("address")
                if base_addr and base_addr not in sol_tokens:
                    sol_tokens.append(base_addr)

        log.info(f"[Channel 2 - DexScreener] Extracted {len(sol_tokens)} promoted candidate token mints.")

        # 2. Inspect active pools for the top tokens
        for mint in sol_tokens[:8]:
            try:
                pair_data = await connection_pool.dex_get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
                )
                pairs = (pair_data or {}).get("pairs", [])
                if not pairs:
                    continue

                # Prefer executable depth; break ties by 24h volume.
                pairs.sort(key=lambda p: (
                    float(p.get("liquidity", {}).get("usd", 0) or 0),
                    float(p.get("volume", {}).get("h24", 0) or 0),
                ), reverse=True)
                best_pool = pairs[0].get("pairAddress")
                sym = pairs[0].get("baseToken", {}).get("symbol", "TOKEN")
                if not best_pool:
                    continue

                trades_url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{best_pool}/trades"
                trades_data = await connection_pool.gecko_get(trades_url)
                pool_trades = (trades_data or {}).get("data", [])

                trader_volume: Dict[str, Dict[str, Any]] = {}
                for tr in pool_trades:
                    attrs = tr.get("attributes", {})
                    if attrs.get("kind") == "buy":
                        wallet = attrs.get("tx_from_address")
                        vol = float(attrs.get("volume_in_usd", 0) or 0.0)
                        if wallet and len(wallet) >= 32:
                            record = trader_volume.setdefault(wallet, {"volume": 0.0, "timestamps": []})
                            record["volume"] += vol
                            ts = _parse_timestamp(attrs.get("block_timestamp"))
                            if ts is not None:
                                record["timestamps"].append(ts)

                sorted_traders = sorted(
                    trader_volume.items(),
                    key=lambda item: item[1]["volume"],
                    reverse=True,
                )
                for wallet, stats in sorted_traders[:3]:
                    vol = stats["volume"]
                    if wallet not in seen_wallets and vol >= 200.0:
                        seen_wallets.add(wallet)
                        observed_at = max(stats["timestamps"], default=time.time())
                        candidates.append({
                            "wallet": wallet,
                            "source": DiscoverySource.DEXSCREENER.value,
                            "token": mint,
                            "token_symbol": sym,
                            "detail": f"Promoted-token accumulator (${vol:,.0f} buy volume on {sym})",
                            "observed_at": observed_at,
                        })
                        if len(candidates) >= limit:
                            break

                if len(candidates) >= limit:
                    break
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
        url = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
        pool_data = await connection_pool.gecko_get(url)
        pools = (pool_data or {}).get("data", [])
        log.info(f"[Channel 3 - DEX On-Chain] Found {len(pools)} trending pools on Solana.")

        for pool in pools[:10]:
            pool_addr = pool.get("attributes", {}).get("address")
            pool_name = pool.get("attributes", {}).get("name", "Unknown")
            token_mint = _relationship_address(pool, "base_token")
            if not pool_addr or not token_mint:
                log.debug(f"[Channel 3 - DEX On-Chain] Pool {pool_addr or 'unknown'} has no base-token mint.")
                continue

            trades_url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool_addr}/trades"
            trade_payload = await connection_pool.gecko_get(trades_url)
            trades_data = (trade_payload or {}).get("data", [])
            buyer_counts: Dict[str, Dict[str, Any]] = {}

            for td in trades_data:
                attrs = td.get("attributes", {})
                wallet = attrs.get("tx_from_address")
                vol = float(attrs.get("volume_in_usd", 0) or 0.0)
                kind = attrs.get("kind", "")
                if wallet and len(wallet) >= 32 and kind == "buy":
                    record = buyer_counts.setdefault(
                        wallet,
                        {"count": 0, "total_vol": 0.0, "timestamps": []},
                    )
                    record["count"] += 1
                    record["total_vol"] += vol
                    ts = _parse_timestamp(attrs.get("block_timestamp"))
                    if ts is not None:
                        record["timestamps"].append(ts)

            top_traders = sorted(
                buyer_counts.items(),
                key=lambda item: (item[1]["total_vol"], item[1]["count"]),
                reverse=True,
            )
            for wallet, stats in top_traders[:3]:
                timestamps = stats["timestamps"]
                span_seconds = max(timestamps) - min(timestamps) if len(timestamps) >= 2 else 0.0
                is_repeat_accumulator = (
                    stats["count"] >= MIN_REPEAT_BUYS
                    and span_seconds >= MIN_ACCUMULATION_SPAN_SECONDS
                )
                if (
                    wallet not in seen_wallets
                    and stats["total_vol"] >= 200.0
                    and is_repeat_accumulator
                ):
                    seen_wallets.add(wallet)
                    candidates.append({
                        "wallet": wallet,
                        "source": DiscoverySource.DEX_ONCHAIN.value,
                        "token": token_mint,
                        "pool": pool_addr,
                        "token_symbol": pool_name,
                        "detail": (
                            f"Repeat DEX accumulator (${stats['total_vol']:,.0f} across "
                            f"{stats['count']} buys over {span_seconds:.0f}s on {pool_name})"
                        ),
                        "observed_at": max(timestamps),
                    })
                    if len(candidates) >= limit:
                        break

            if len(candidates) >= limit:
                break

        log.info(f"[Channel 3 - DEX On-Chain] Discovered {len(candidates)} on-chain DEX whales.")
    except Exception as e:
        log.error(f"[Channel 3 - DEX On-Chain] Error querying DEX on-chain whales: {e}")

    return candidates


# ─── CHANNEL 4: Nansen Smart Money DEX Trades ────────────────────────────────

async def fetch_nansen_smart_money(limit: int = 20) -> List[Dict[str, Any]]:
    """Fetch labeled Solana smart traders observed by Nansen in the last 24 hours."""
    if os.getenv("NANSEN_DISCOVERY_ENABLED", "TRUE").strip().upper() not in {"1", "TRUE", "YES", "ON"}:
        log.info("[Channel 4 - Nansen] Discovery is disabled by configuration.")
        return []
    if not os.getenv("NANSEN_API_KEY", "").strip():
        log.info("[Channel 4 - Nansen] NANSEN_API_KEY missing; skipping premium channel.")
        return []

    labels = [
        label.strip()
        for label in os.getenv(
            "NANSEN_SMART_MONEY_LABELS",
            "30D Smart Trader,90D Smart Trader,180D Smart Trader",
        ).split(",")
        if label.strip()
    ]
    try:
        min_trade_usd = max(0.0, float(os.getenv("NANSEN_MIN_TRADE_USD", "1000") or 1000))
    except ValueError:
        min_trade_usd = 1000.0

    request_limit = max(1, min(int(limit), 50))
    payload = {
        "chains": ["solana"],
        "filters": {
            "include_smart_money_labels": labels,
            "trade_value_usd": {"min": min_trade_usd},
        },
        "pagination": {"page": 1, "per_page": min(max(request_limit * 5, 50), 1000)},
        "order_by": [{"field": "block_timestamp", "direction": "DESC"}],
    }
    response = await connection_pool.nansen_post(
        "https://api.nansen.ai/api/v1/smart-money/dex-trades",
        json_data=payload,
    )
    rows = response.get("data", []) if isinstance(response, dict) else []
    if isinstance(rows, dict):
        rows = rows.get("data", []) or rows.get("items", []) or []

    candidates: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        wallet = str(row.get("trader_address", "")).strip()
        if not SOLANA_ADDRESS_PATTERN.fullmatch(wallet) or wallet in seen:
            continue
        seen.add(wallet)
        label = str(row.get("trader_address_label", "Smart Money")).strip() or "Smart Money"
        try:
            trade_value = float(row.get("trade_value_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            trade_value = 0.0
        candidates.append({
            "wallet": wallet,
            "source": DiscoverySource.NANSEN.value,
            "token": row.get("token_bought_address", ""),
            "token_symbol": row.get("token_bought_symbol", ""),
            "detail": f"Nansen {label} DEX buy (${trade_value:,.0f})",
            "observed_at": _parse_timestamp(row.get("block_timestamp")) or time.time(),
            "transaction_hash": row.get("transaction_hash", ""),
        })
        if len(candidates) >= request_limit:
            break

    log.info(f"[Channel 4 - Nansen] Discovered {len(candidates)} labeled smart traders.")
    return candidates


async def fetch_nansen_wallet_profile(wallet: str, days: int = 7) -> Optional[Dict[str, Any]]:
    """Fetch a fail-closed PnL summary when the primary wallet profiler has no 7D data."""
    if not os.getenv("NANSEN_API_KEY", "").strip():
        return None
    if not SOLANA_ADDRESS_PATTERN.fullmatch(str(wallet).strip()):
        return None

    days = max(1, min(int(days), 30))
    end_at = datetime.now(timezone.utc)
    start_at = end_at - timedelta(days=days)
    payload = {
        "address": wallet,
        "chain": "solana",
        "date": {
            "from": start_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "to": end_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        },
    }
    response = await connection_pool.nansen_post(
        "https://api.nansen.ai/api/v1/profiler/address/pnl-summary",
        json_data=payload,
    )
    if not isinstance(response, dict):
        return None

    summary = response.get("data") if isinstance(response.get("data"), dict) else response
    try:
        trades = int(float(summary.get("traded_times", 0) or 0))
        winrate = float(summary.get("win_rate", 0.0) or 0.0)
        realized = float(summary.get("realized_pnl_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        log.warning("[Nansen Profiler] Invalid PnL summary for %s", wallet[:8])
        return None

    if trades <= 0:
        log.info("[Nansen Profiler] No %s-day trades for %s", days, wallet[:8])
        return None

    # Nansen documents win_rate as a percentage. Clamp invalid provider output
    # rather than converting an ambiguous sub-1 value into an optimistic score.
    winrate = max(0.0, min(winrate, 100.0))
    log.info(
        "[Nansen Profiler] %s: %s trades, %.1f%% win rate, $%.2f realized over %sd",
        wallet[:8],
        trades,
        winrate,
        realized,
        days,
    )
    return {
        "wallet": wallet,
        "1d": {},
        "7d": {
            "winrate": winrate,
            "trades": trades,
            "realized": realized,
            "native_balance": 0.0,
        },
        "30d": {},
        "tags": ["nansen_smart_trader"],
        "performance_source": "nansen_profiler_pnl_summary",
        "performance_fetched_at": time.time(),
        "nansen_top_tokens": summary.get("top5_tokens", []) or [],
    }


# ─── CHANNEL 5: Multi-Whale Cluster Consensus (Cielo-Style) ───────────────────

def detect_cluster_consensus(
    candidates_pool: List[Dict[str, Any]],
    known_whitelist: Set[str]
) -> List[Dict[str, Any]]:
    """
    Channel 5: Identifies same-token and same-wallet observations from distinct
    inputs inside a bounded time window. Known whitelist participation is recorded
    as supporting evidence when a whitelisted wallet appears in the same cluster.
    """
    log.info("[Channel 5 - Cluster Consensus] Evaluating token convergence across discovery candidates...")

    token_observations: Dict[str, List[Tuple[str, float]]] = {}
    wallet_sources: Dict[str, Dict[str, float]] = {}
    now = time.time()

    for c in candidates_pool:
        w = c.get("wallet")
        tok = c.get("token")
        src = c.get("source")
        observed_at = _parse_timestamp(c.get("observed_at")) or now
        if not w:
            continue
        if src:
            wallet_sources.setdefault(w, {})[src] = observed_at

        if tok and len(tok) >= 32:
            token_observations.setdefault(tok, []).append((w, observed_at))

    cluster_candidates = []
    seen = set()

    # Identify multi-wallet convergence (2 or more wallets bought the same breakout token)
    for tok, observations in token_observations.items():
        ordered = sorted(observations, key=lambda item: item[1])
        best_wallets: Set[str] = set()
        best_time = now
        left = 0
        for right, (_, window_end) in enumerate(ordered):
            while window_end - ordered[left][1] > CLUSTER_WINDOW_SECONDS:
                left += 1
            window_wallets = {wallet for wallet, _ in ordered[left:right + 1]}
            if len(window_wallets) > len(best_wallets):
                best_wallets = window_wallets
                best_time = window_end

        if len(best_wallets) >= 2:
            known_count = len(best_wallets.intersection(known_whitelist))
            for w in best_wallets:
                if w not in seen:
                    seen.add(w)
                    overlap_count = len(best_wallets)
                    whitelist_note = f"; {known_count} already whitelisted" if known_count else ""
                    cluster_candidates.append({
                        "wallet": w,
                        "source": DiscoverySource.CLUSTER.value,
                        "token": tok,
                        "cluster_size": overlap_count,
                        "detail": (
                            f"Smart Money Cluster: {overlap_count} distinct wallets converged "
                            f"within {CLUSTER_WINDOW_SECONDS // 60}m on token {tok[:6]}...{whitelist_note}"
                        ),
                        "observed_at": best_time,
                    })

    # Also detect multi-source convergence (found across multiple channels e.g. GMGN + DexScreener)
    for w, source_times in wallet_sources.items():
        srcs = set(source_times)
        timestamps = list(source_times.values())
        inside_window = max(timestamps) - min(timestamps) <= CLUSTER_WINDOW_SECONDS
        if len(srcs) >= 2 and inside_window and w not in seen:
            seen.add(w)
            cluster_candidates.append({
                "wallet": w,
                "source": DiscoverySource.CLUSTER.value,
                "cluster_size": len(srcs),
                "detail": f"Multi-Channel Consensus: observed by {', '.join(sorted(srcs)).upper()}",
                "observed_at": max(timestamps),
            })

    log.info(f"[Channel 5 - Cluster Consensus] Identified {len(cluster_candidates)} consensus cluster candidates.")
    return cluster_candidates


# ─── MASTER MULTI-SOURCE DISCOVERY ORCHESTRATOR ───────────────────────────────

async def run_omni_whale_discovery(
    sources: Optional[List[str]] = None,
    limit_per_source: int = 10,
    status_target: str = "NEUTRAL",
    recent_days: float = 14.0,
    progress_callback = None
) -> Dict[str, Any]:
    """
    Executes the 5-channel discovery pipeline:
      1. Gathers candidates from GMGN, DexScreener, DEX pools, Nansen, and Cluster.
      2. Merges and de-duplicates candidate wallets.
      3. Pulls GMGN performance stats, with bounded Nansen PnL fallback for empty profiles.
      4. Enforces the strict institutional anti-toxicity gauntlet.
      5. Saves and ranks new clean alphas into data/neutral_whales_ranked.json.
    """
    if sources is None:
        sources = ["gmgn", "dexscreener", "dex_onchain", "nansen", "cluster"]

    active_sources = list(dict.fromkeys(s.lower() for s in sources if s.lower() in ALLOWED_SOURCES))
    limit_per_source = max(1, min(int(limit_per_source), 50))
    log.info(f"🚀 Starting Omni-Channel Whale Discovery across channels: {active_sources}")

    if progress_callback:
        progress_callback("Connecting to 5 discovery channels (GMGN, DexScreener, DEX Pools, Nansen, Cluster)...", 0, 100)

    raw_candidates: List[Dict[str, Any]] = []

    source_jobs: List[Tuple[str, Any]] = []
    if "gmgn" in active_sources:
        source_jobs.append(("gmgn", fetch_gmgn_smart_money(
            limit=limit_per_source,
            status_target=status_target,
            recent_days=recent_days,
        )))
    if "dexscreener" in active_sources:
        source_jobs.append(("dexscreener", fetch_dexscreener_breakout_whales(limit=limit_per_source)))
    if "dex_onchain" in active_sources:
        source_jobs.append(("dex_onchain", fetch_onchain_dex_whales(limit=limit_per_source)))
    if "nansen" in active_sources:
        source_jobs.append(("nansen", fetch_nansen_smart_money(limit=limit_per_source)))

    if source_jobs:
        if progress_callback:
            progress_callback("Scanning GMGN, DexScreener, DEX pools, and Nansen concurrently...", 10, 100)
        results = await asyncio.gather(*(job for _, job in source_jobs), return_exceptions=True)
        for (source_name, _), result in zip(source_jobs, results):
            if isinstance(result, Exception):
                log.error(f"[{source_name}] discovery failed: {result}")
                continue
            raw_candidates.extend(result)

    # Channel 5: Cluster Consensus
    if "cluster" in active_sources:
        if progress_callback:
            progress_callback("Channel 5: Evaluating Smart Money Cluster Consensus...", 65, 100)
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
                "tokens": [],
                "observed_at": [],
            }
        wallet_profiles_map[w]["sources"].add(item.get("source", "unknown"))
        if item.get("detail"):
            wallet_profiles_map[w]["details"].append(item["detail"])
        if item.get("token"):
            wallet_profiles_map[w]["tokens"].append(item["token"])
        if item.get("observed_at"):
            wallet_profiles_map[w]["observed_at"].append(item["observed_at"])

    unique_wallets = list(wallet_profiles_map.keys())
    total_unique = len(unique_wallets)
    log.info(f"Aggregated {total_unique} unique candidate wallets across all channels.")

    if total_unique == 0:
        return {
            "status": "completed",
            "total_evaluated": 0,
            "clean_alphas_found": 0,
            "blocked_toxic_found": 0,
            "consensus_rejected_found": 0,
            "nansen_profiles_used": 0,
            "nansen_profile_attempts": 0,
            "nansen_profile_cache_hits": 0,
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
    consensus_rejected = 0
    nansen_profiles_used = 0
    nansen_profile_attempts = 0
    nansen_profile_cache_hits = 0
    try:
        nansen_profile_limit = max(
            0,
            min(int(os.getenv("NANSEN_PROFILE_FALLBACK_LIMIT", "5") or 5), 20),
        )
    except ValueError:
        nansen_profile_limit = 5
    try:
        nansen_profile_cache_ttl = max(
            0.0,
            min(float(os.getenv("NANSEN_PROFILE_CACHE_TTL_HOURS", "6") or 6), 168.0),
        ) * 3600.0
    except ValueError:
        nansen_profile_cache_ttl = 6 * 3600.0
    nansen_profile_enabled = (
        os.getenv("NANSEN_PROFILE_FALLBACK_ENABLED", "TRUE").strip().upper()
        in {"1", "TRUE", "YES", "ON"}
    )

    # Evaluate each wallet via 7D GMGN stats & AI Gauntlet
    for idx, w in enumerate(unique_wallets, 1):
        if progress_callback:
            progress_callback(f"Evaluating {idx}/{total_unique}: {w[:6]}...", 70 + int((idx / total_unique) * 25), 100)

        meta = wallet_profiles_map[w]
        sources_list = sorted(meta["sources"])

        # Fetch profile
        profile = await asyncio.to_thread(
            whale_scanner_core.fetch_wallet_profile,
            w,
            use_cache=True,
            deep_scan=True,
            rate_sleep=0.3
        )

        if not profile:
            profile = {"wallet": w, "1d": {}, "7d": {}, "30d": {}, "tags": []}

        try:
            primary_trades = int(float((profile.get("7d", {}) or {}).get("trades", 0) or 0))
        except (TypeError, ValueError):
            primary_trades = 0
        should_consider_nansen_fallback = (
            nansen_profile_enabled
            and DiscoverySource.NANSEN.value in sources_list
            and primary_trades <= 0
        )
        if should_consider_nansen_fallback:
            primary_tags = list(profile.get("tags", []) or [])
            profile["nansen_profile_fallback_attempted"] = True
            cached_profile = existing_dict.get(w, {}) or {}
            try:
                cached_fetched_at = float(
                    cached_profile.get("performance_fetched_at", 0.0) or 0.0
                )
                cached_trades = int(float(
                    (cached_profile.get("7d", {}) or {}).get("trades", 0) or 0
                ))
            except (TypeError, ValueError):
                cached_fetched_at = 0.0
                cached_trades = 0
            cached_age = time.time() - cached_fetched_at
            cache_valid = (
                cached_profile.get("performance_source") == "nansen_profiler_pnl_summary"
                and cached_trades > 0
                and nansen_profile_cache_ttl > 0
                and 0 <= cached_age <= nansen_profile_cache_ttl
            )
            if cache_valid:
                profile = dict(cached_profile)
                profile["tags"] = sorted(set(
                    primary_tags + list(profile.get("tags", []) or [])
                ))
                profile["nansen_profile_cache_hit"] = True
                nansen_profiles_used += 1
                nansen_profile_cache_hits += 1
            elif nansen_profile_attempts < nansen_profile_limit:
                nansen_profile_attempts += 1
                nansen_profile = await fetch_nansen_wallet_profile(w, days=7)
                if nansen_profile:
                    nansen_profile["tags"] = sorted(set(
                        primary_tags + list(nansen_profile.get("tags", []) or [])
                    ))
                    nansen_profile["nansen_profile_fallback_attempted"] = True
                    profile = nansen_profile
                    nansen_profiles_used += 1

        # Enrich profile with omni-channel metadata
        profile["discovery_sources"] = sources_list
        profile["channel_details"] = list(meta["details"])
        if profile.get("performance_source") == "nansen_profiler_pnl_summary":
            profile["channel_details"].append("7D performance verified by Nansen Profiler")
        profile["discovery_tokens"] = sorted(set(meta["tokens"]))
        profile["last_discovered_at"] = max(meta["observed_at"], default=time.time())
        profile["evaluated_at"] = time.time()
        safety = evaluate_wallet_safety(w, profile)
        consensus = evaluate_discovery_consensus(profile)
        profile.update(safety)
        profile.update(consensus)
        profile["whitelist_eligible"] = bool(
            safety["passed_filters"] and consensus["source_consensus_passed"]
        )
        if profile["whitelist_eligible"]:
            clean_discovered += 1
        elif not safety["passed_filters"]:
            blocked_toxic += 1
        else:
            consensus_rejected += 1

        # Add to existing map
        existing_dict[w] = profile

    # Re-evaluate legacy rows with the same policy so the dashboard and scanner agree.
    for wallet, profile in existing_dict.items():
        profile.update(evaluate_wallet_safety(wallet, profile))
        if not profile.get("discovery_sources"):
            profile["discovery_sources"] = [DiscoverySource.LOCAL_HISTORY.value]
        profile.update(evaluate_discovery_consensus(profile))
        profile["whitelist_eligible"] = bool(
            profile.get("passed_filters") and profile.get("source_consensus_passed")
        )

    # Save clean and quarantined profiles for dashboard audit.
    updated_ranked = list(existing_dict.values())
    updated_ranked.sort(key=lambda x: (
        bool(x.get("whitelist_eligible")),
        int(x.get("independent_source_count", 0) or 0),
        DiscoverySource.CLUSTER.value in x.get("discovery_sources", []),
        float(x.get("7d", {}).get("realized", 0.0) or 0.0),
    ), reverse=True)

    os.makedirs(os.path.dirname(RANKED_FILE), exist_ok=True)
    temp_fd, temp_path = tempfile.mkstemp(
        dir=os.path.dirname(RANKED_FILE),
        prefix="neutral_whales_ranked_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            json.dump(updated_ranked, f, indent=2)
        os.replace(temp_path, RANKED_FILE)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    if progress_callback:
        progress_callback(f"Done! Evaluated {total_unique} wallets. Found {clean_discovered} verified alphas.", 100, 100)

    log.info(
        "Omni-Channel Discovery Complete: %s whitelist-eligible, %s toxic blocked, "
        "%s awaiting source consensus.",
        clean_discovered,
        blocked_toxic,
        consensus_rejected,
    )

    return {
        "status": "completed",
        "total_evaluated": total_unique,
        "clean_alphas_found": clean_discovered,
        "blocked_toxic_found": blocked_toxic,
        "consensus_rejected_found": consensus_rejected,
        "nansen_profiles_used": nansen_profiles_used,
        "nansen_profile_attempts": nansen_profile_attempts,
        "nansen_profile_cache_hits": nansen_profile_cache_hits,
        "total_ranked_db": len(updated_ranked),
        "channels_scanned": active_sources
    }
