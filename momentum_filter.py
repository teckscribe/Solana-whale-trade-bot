import asyncio
import httpx
import logging
import os
from datetime import datetime, timezone
from typing import Optional
import time

log = logging.getLogger("MomentumFilter")

import settings_manager

def _max_pump_pct() -> float:
    return float(settings_manager.get("MAX_M5_PUMP_PCT"))

GECKO_BASE = "https://api.geckoterminal.com/api/v2/networks/solana"

# GeckoTerminal's OHLCV endpoint only accepts a POOL address, not a token/mint
# address. Resolve + cache the top (most liquid) pool for each token so we
# aren't doing an extra lookup call on every poll.
_pool_cache = {}  # token_address -> (pool_address, cached_at)
POOL_CACHE_TTL = 3600  # pool for a given token essentially never changes; 1h is plenty
MAX_POOL_CACHE = 500

def _prune_pool_cache():
    """Evicts expired pool entries and bounds cache size."""
    now = time.time()
    expired = [k for k, v in _pool_cache.items() if (now - v[1]) > POOL_CACHE_TTL]
    for k in expired:
        _pool_cache.pop(k, None)
    if len(_pool_cache) > MAX_POOL_CACHE:
        sorted_keys = sorted(_pool_cache.keys(), key=lambda k: _pool_cache[k][1])
        for k in sorted_keys[:100]:
            _pool_cache.pop(k, None)

async def _resolve_top_pool(client: httpx.AsyncClient, token_address: str) -> Optional[str]:
    """
    Looks up the most liquid pool trading this token via
    /networks/{network}/tokens/{token_address}/pools, and caches the result.
    Returns the pool address, or None if it couldn't be resolved.
    """
    now = time.time()
    _prune_pool_cache()
    cached = _pool_cache.get(token_address)
    if cached and (now - cached[1] < POOL_CACHE_TTL):
        return cached[0]

    url = f"{GECKO_BASE}/tokens/{token_address}/pools"
    try:
        resp = await client.get(url, timeout=5.0, params={"page": 1})
        if resp.status_code != 200:
            log.warning(f"Momentum filter: pool lookup for {token_address} returned {resp.status_code}")
            return None

        pools = resp.json().get("data", [])
        if not pools:
            return None

        # Response is generally ordered by liquidity/volume already; take the first entry.
        pool_id = pools[0].get("attributes", {}).get("address")
        if not pool_id:
            return None

        _pool_cache[token_address] = (pool_id, now)
        return pool_id
    except Exception as e:
        log.error(f"Momentum filter: failed to resolve pool for {token_address}: {e}")
        return None


async def check_momentum(token_address: str, max_pump_pct: Optional[float] = None) -> bool:
    """
    Returns True if the token is SAFE to buy.
    Returns False if the token has pumped more than max_pump_pct in the last 5 minutes.
    If max_pump_pct <= 0, 5-minute pump check is DISABLED.
    """
    if max_pump_pct is None:
        max_pump_pct = _max_pump_pct()
    if max_pump_pct <= 0:
        return True
    try:
        async with httpx.AsyncClient() as client:
            pool_address = await _resolve_top_pool(client, token_address)
            if not pool_address:
                log.debug(f"Momentum filter: no pool found for {token_address}, skipping check (fail-open).")
                return True

            url = f"{GECKO_BASE}/pools/{pool_address}/ohlcv/minute"
            resp = await client.get(url, timeout=5.0, params={"limit": 5})

            if resp.status_code == 200:
                data = resp.json().get("data", {}).get("attributes", {}).get("ohlcv_list", [])

                if not data or len(data) < 2:
                    # Not enough data to determine momentum, assume safe
                    return True

                # data format: [timestamp, open, high, low, close, volume]
                # data[0] is the most recent minute, data[-1] is 5 minutes ago
                current_price = float(data[0][4])  # close of most recent minute
                oldest_price = float(data[-1][1])  # open of the oldest minute

                if oldest_price > 0:
                    pump_pct = ((current_price - oldest_price) / oldest_price) * 100
                    if pump_pct > max_pump_pct:
                        log.warning(f"🚨 Momentum Filter: {token_address} pumped {pump_pct:.1f}% in 5m! Blocking trade.")
                        return False

                return True
            elif resp.status_code == 429:
                # Rate limited, default to safe to avoid blocking good trades
                return True
            else:
                log.warning(f"Momentum filter: OHLCV lookup for pool {pool_address} returned {resp.status_code}")

    except Exception as e:
        log.error(f"Momentum filter error: {e}")

    return True
