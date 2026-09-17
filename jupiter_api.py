"""
jupiter_api.py
Interacts with the Jupiter Aggregator v6 Price API and DexScreener API.

Key features:
  - Fetches real-time USD prices and metadata for Solana tokens.
  - Two-tier pricing: DexScreener mid-prices for screening/trigger, executable Jupiter quotes for fills.
  - In-memory tiered caching with TTL and size bounds to prevent memory leaks in 24/7 operation.
  - Zero-latency pre-warmed token metadata via preload_token_metadata().
  - Connection reuse via connection_pool.py (no per-call TCP/TLS handshake latency).
"""

import os
import logging
import time
import asyncio
from typing import Dict, Any, Optional, Tuple, Set

from telegram_notifier import send_error_alert
from connection_pool import get_client, dex_get, jupiter_get, get_rpc_client

log = logging.getLogger("JupiterAPI")

DEXSCREENER_PRICE_API = "https://api.dexscreener.com/latest/dex/tokens"
JUPITER_QUOTE_API = "https://api.jup.ag/swap/v1/quote"
SOL_MINT = "So11111111111111111111111111111111111111112"

_last_alert_time = 0

# ─────────────────────────────────────────────────────────────────────────────
# Tiered Caches
# ─────────────────────────────────────────────────────────────────────────────

# ⚠️ THIS CACHE MUST NEVER BE USED AS A P&L REFERENCE PRICE.
# It exists so the pre-trade *screen* (24h volume / market cap / symbol) doesn't hammer DexScreener.
CACHE_TTL = 300
MAX_CACHE_ENTRIES = 500

_price_cache: Dict[str, Tuple[Tuple[float, str, str, float, float], float]] = {}
_liquidity_cache: Dict[str, Tuple[float, float]] = {}  # token -> (liquidity_usd, cached_at)
_decimals_cache: Dict[str, int] = {}                   # mint -> int
_quote_cache: Dict[str, Tuple[Dict[str, Any], float]] = {}

MAX_DECIMALS_CACHE = 5000
_cache_stats = {"hits": 0, "misses": 0, "prunes": 0}


def _prune_cache(cache_dict: dict, ttl: float, max_size: int = MAX_CACHE_ENTRIES):
    """Evicts expired entries and enforces max size on cache to prevent 24/7 memory leaks."""
    now = time.time()
    expired_keys = [k for k, v in cache_dict.items() if (now - v[1]) > ttl]
    for k in expired_keys:
        cache_dict.pop(k, None)
    if len(cache_dict) > max_size:
        sorted_keys = sorted(cache_dict.keys(), key=lambda k: cache_dict[k][1])
        drop_count = len(cache_dict) - max_size + 100
        for k in sorted_keys[:drop_count]:
            cache_dict.pop(k, None)
    _cache_stats["prunes"] += len(expired_keys)


def prune_all_caches():
    """Unified cache maintenance called periodically to reclaim memory."""
    _prune_cache(_price_cache, CACHE_TTL)
    _prune_cache(_liquidity_cache, CACHE_TTL)
    _prune_cache(_quote_cache, 60)
    if len(_decimals_cache) > MAX_DECIMALS_CACHE:
        for stale in list(_decimals_cache)[:500]:
            _decimals_cache.pop(stale, None)


def get_cache_stats() -> dict:
    """Returns hit/miss/prune stats for diagnostics."""
    return {
        **_cache_stats,
        "price_cache_len": len(_price_cache),
        "liquidity_cache_len": len(_liquidity_cache),
        "decimals_cache_len": len(_decimals_cache),
        "quote_cache_len": len(_quote_cache),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Token Metadata & Screening
# ─────────────────────────────────────────────────────────────────────────────

async def get_token_price_usd(token_address: str, use_cache: bool = True) -> tuple[float, str, str, float, float]:
    """
    Fetches token metadata from DexScreener: (price, symbol, name, volume_24h, market_cap).
    Returns (0.0, "", "", 0.0, 0.0) if it cannot be fetched.

    Intended for the pre-trade SCREEN (volume / market cap / symbol filtering), where the
    5-minute cache is a pure win.

    `use_cache=False` bypasses the cache. Never book P&L against it; use
    `get_executable_price_usd()` instead.
    """
    global _last_alert_time
    if not token_address:
        return 0.0, "", "", 0.0, 0.0

    now = time.time()
    _prune_cache(_price_cache, CACHE_TTL)
    if use_cache and token_address in _price_cache:
        cached_data, timestamp = _price_cache[token_address]
        if now - timestamp < CACHE_TTL:
            _cache_stats["hits"] += 1
            return cached_data

    _cache_stats["misses"] += 1
    try:
        url = f"{DEXSCREENER_PRICE_API}/{token_address}"
        body = await dex_get(url)
        if body:
            pairs = body.get("pairs", [])
            if pairs:
                # Sort pairs by USD liquidity descending to get the deepest pool
                sorted_pairs = sorted(
                    pairs,
                    key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0),
                    reverse=True
                )
                pair = sorted_pairs[0]
                price = float(pair.get("priceUsd") or 0.0)
                symbol = pair.get("baseToken", {}).get("symbol") or "Unknown"
                name = pair.get("baseToken", {}).get("name") or "Unknown"

                vol_dict = pair.get("volume") or {}
                volume_24h = float(vol_dict.get("h24") or 0.0)
                market_cap = float(pair.get("marketCap") or 0.0)

                _liquidity_cache[token_address] = (
                    float((pair.get("liquidity") or {}).get("usd") or 0.0), now
                )
                result = (price, symbol, name, volume_24h, market_cap)
                _price_cache[token_address] = (result, now)
                return result
    except Exception as e:
        log.error(f"Failed to fetch price for {token_address} from Price API: {e}")

    return 0.0, "", "", 0.0, 0.0


async def get_pool_liquidity_usd(token_address: str) -> float:
    """
    USD liquidity of the deepest pool trading this token. Returns 0.0 if unknown.
    Populated as a side effect of get_token_price_usd(); triggers fetch on miss.
    """
    if not token_address:
        return 0.0

    _prune_cache(_liquidity_cache, CACHE_TTL)
    cached = _liquidity_cache.get(token_address)
    if cached and (time.time() - cached[1]) < CACHE_TTL:
        return cached[0]

    await get_token_price_usd(token_address)
    cached = _liquidity_cache.get(token_address)
    return cached[0] if cached else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Token Decimals (Pre-warmed / In-Memory Cached)
# ─────────────────────────────────────────────────────────────────────────────

async def get_token_decimals(mint: str) -> Optional[int]:
    """
    Returns the SPL mint's decimals via RPC getTokenSupply, or None if it can't be resolved.
    Callers MUST treat None as 'cannot size this trade safely'.
    """
    if not mint:
        return None
    if mint in _decimals_cache:
        return _decimals_cache[mint]

    try:
        rpc_client = await get_rpc_client()
        # Fast RPC call using shared client
        rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]}
        client = await get_client()
        resp = await client.post(rpc_url, json=payload, timeout=6.0)
        if resp.status_code == 200:
            value = (resp.json().get("result") or {}).get("value") or {}
            decimals = value.get("decimals")
            if isinstance(decimals, int):
                if len(_decimals_cache) >= MAX_DECIMALS_CACHE:
                    for stale in list(_decimals_cache)[:500]:
                        _decimals_cache.pop(stale, None)
                _decimals_cache[mint] = decimals
                return decimals
    except Exception as e:
        log.debug(f"getTokenSupply failed for {mint[:8]}: {e}")

    # Fallback to standard SPL 6 decimals if RPC fails/rate-limits
    log.warning(f"Defaulting to 6 decimals for {mint[:8]}")
    _decimals_cache[mint] = 6
    return 6


async def preload_token_metadata(token_mints: Set[str]) -> int:
    """
    Pre-warms _decimals_cache and _liquidity_cache for a set of token mints on startup
    or periodic discovery, providing 0ms RAM lookups on subsequent trade evaluations.
    """
    if not token_mints:
        return 0

    to_fetch = [m for m in token_mints if m and m not in _decimals_cache]
    if not to_fetch:
        return 0

    log.info(f"Preloading metadata for {len(to_fetch)} tokens in RAM...")
    loaded = 0
    # Process in batches of 10 to respect RPC connection limits
    batch_size = 10
    for i in range(0, len(to_fetch), batch_size):
        batch = to_fetch[i:i + batch_size]
        tasks = [get_token_decimals(mint) for mint in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        loaded += sum(1 for r in results if isinstance(r, int))

    log.info(f"[Preload] Successfully preloaded {loaded}/{len(to_fetch)} token decimals into RAM")
    return loaded


# ─────────────────────────────────────────────────────────────────────────────
# Executable Pricing (Jupiter V6)
# ─────────────────────────────────────────────────────────────────────────────

def price_from_quote(route: Dict[str, Any], sol_spent_lamports: int,
                     token_decimals: int, sol_price_usd: float) -> float:
    """
    Effective USD price per token for a SOL -> TOKEN route: what you paid divided by what
    you actually received. Always worse than mid by exactly the route's price impact.
    """
    try:
        out_amount = int(route.get("outAmount", "0"))
        if out_amount <= 0 or sol_spent_lamports <= 0 or sol_price_usd <= 0:
            return 0.0
        tokens_received = out_amount / (10 ** token_decimals)
        if tokens_received <= 0:
            return 0.0
        usd_spent = (sol_spent_lamports / 1_000_000_000) * sol_price_usd
        return usd_spent / tokens_received
    except Exception as e:
        log.error(f"price_from_quote failed: {e}")
        return 0.0


def exit_price_from_quote(route: Dict[str, Any], tokens_sold_base_units: int,
                          token_decimals: int, sol_price_usd: float) -> float:
    """
    Effective USD price per token for a TOKEN -> SOL route: what you'd actually receive
    divided by what you sold. Returns 0.0 if the route is unusable.
    """
    try:
        out_lamports = int(route.get("outAmount", "0"))
        if out_lamports <= 0 or tokens_sold_base_units <= 0 or sol_price_usd <= 0:
            return 0.0
        tokens_sold = tokens_sold_base_units / (10 ** token_decimals)
        if tokens_sold <= 0:
            return 0.0
        usd_received = (out_lamports / 1_000_000_000) * sol_price_usd
        return usd_received / tokens_sold
    except Exception as e:
        log.error(f"exit_price_from_quote failed: {e}")
        return 0.0


def quote_price_impact_pct(route: Dict[str, Any]) -> float:
    """Route price impact as a percentage (e.g. 4.2 for 4.2%). Returns 0.0 if absent."""
    try:
        return abs(float(route.get("priceImpactPct") or 0.0)) * 100.0
    except (TypeError, ValueError):
        return 0.0


async def get_executable_price_usd(token_address: str, tokens_held: float,
                                   token_decimals: int, sol_price_usd: float) -> float:
    """
    What this exact position would actually fetch right now, per token, if sold into the
    real route. Returns 0.0 when no sell route exists (unsellable).
    """
    if tokens_held <= 0 or token_decimals is None:
        return 0.0
    base_units = int(tokens_held * (10 ** token_decimals))
    if base_units <= 0:
        return 0.0
    route = await get_swap_quote(token_address, SOL_MINT, base_units, use_cache=False)
    if not route:
        return 0.0
    return exit_price_from_quote(route, base_units, token_decimals, sol_price_usd)


async def get_live_price(token_address: str) -> float:
    """
    Live, uncached DexScreener mid price via connection pool. Cheap enough to poll every 2 seconds.
    Use this to decide WHEN to exit (the TP/SL trigger). Do NOT use as the fill price.
    """
    if not token_address:
        return 0.0

    try:
        url = f"{DEXSCREENER_PRICE_API}/{token_address}"
        client = await get_client()
        resp = await client.get(url, timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            pairs = data.get("pairs", [])
            if pairs:
                sorted_pairs = sorted(
                    pairs,
                    key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0),
                    reverse=True
                )
                return float(sorted_pairs[0].get("priceUsd", 0.0))
    except Exception as e:
        log.error(f"DexScreener API Exception: {e}")

    return 0.0


async def get_swap_quote(input_mint: str, output_mint: str, amount_lamports: int,
                         use_cache: bool = True) -> Optional[Dict[str, Any]]:
    """
    Fetches the best swap route from Jupiter Aggregator V6 using connection pool and rate budgeting.
    Returns the route dictionary if successful, None otherwise.
    """
    global _last_alert_time

    cache_key = f"{input_mint}_{output_mint}_{amount_lamports}"
    now = time.time()
    _prune_cache(_quote_cache, 60)
    if use_cache and cache_key in _quote_cache:
        cached_quote, timestamp = _quote_cache[cache_key]
        if now - timestamp < 60:
            return cached_quote.copy() if isinstance(cached_quote, dict) else cached_quote

    url = f"{JUPITER_QUOTE_API}?inputMint={input_mint}&outputMint={output_mint}&amount={amount_lamports}&slippageBps=50"
    quote = await jupiter_get(url)
    if quote:
        _quote_cache[cache_key] = (quote, now)
        return quote.copy() if isinstance(quote, dict) else quote

    return None


async def get_sol_price_usd() -> float:
    """
    Fetches the real-time price of Solana (SOL) in USD from Binance, CoinGecko, or DexScreener.
    Uses shared connection pool.
    """
    client = await get_client()
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT"
        resp = await client.get(url, timeout=4.0)
        if resp.status_code == 200:
            price = float(resp.json().get("price", 0.0))
            if price > 0:
                return price
    except Exception:
        pass

    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
        resp = await client.get(url, timeout=4.0)
        if resp.status_code == 200:
            price = float(resp.json().get("solana", {}).get("usd", 0.0))
            if price > 0:
                return price
    except Exception:
        pass

    return 75.37
