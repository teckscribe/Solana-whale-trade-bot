import logging
from typing import Optional
import time

log = logging.getLogger("MomentumFilter")

import settings_manager
import connection_pool

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

async def _resolve_top_pool(token_address: str) -> Optional[str]:
    """
    Looks up the most liquid pool trading this token via
    /networks/{network}/tokens/{token_address}/pools using the shared
    rate-budgeted GeckoTerminal client, and caches the result.
    Returns the pool address, or None if it couldn't be resolved.
    """
    now = time.time()
    _prune_pool_cache()
    cached = _pool_cache.get(token_address)
    if cached and (now - cached[1] < POOL_CACHE_TTL):
        return cached[0]

    url = f"{GECKO_BASE}/tokens/{token_address}/pools"
    try:
        payload = await connection_pool.gecko_get(url, params={"page": 1}, priority="high")
        if not payload:
            return None

        pools = payload.get("data", [])
        if not pools:
            return None

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
        pool_address = await _resolve_top_pool(token_address)
        if not pool_address:
            log.debug(f"Momentum filter: no pool found for {token_address}, skipping check (fail-open).")
            return True

        url = f"{GECKO_BASE}/pools/{pool_address}/ohlcv/minute"
        payload = await connection_pool.gecko_get(url, params={"limit": 6}, priority="high")
        if payload:
            data = payload.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
            if not data or len(data) < 2:
                return True

            # data format: [timestamp, open, high, low, close, volume]
            # data[0] is most recent minute, data[-1] is oldest minute in window.
            # Evaluate both closed candle [t-1] (data[1]) and latest candle plus 5m low->high spike.
            closed_bar = data[1] if len(data) >= 3 else data[0]
            ref_close = max(float(closed_bar[4] or 0.0), float(data[0][4] or 0.0))
            oldest_open = float(data[-1][1] or 0.0)

            if oldest_open > 0:
                pump_pct = ((ref_close - oldest_open) / oldest_open) * 100.0
                if pump_pct > max_pump_pct:
                    log.warning(f"🚨 Momentum Filter: {token_address} pumped {pump_pct:.1f}% in 5m! Blocking trade.")
                    return False

            return True
    except Exception as e:
        log.error(f"Momentum filter error: {e}")

    return True
