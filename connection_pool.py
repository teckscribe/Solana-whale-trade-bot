"""
connection_pool.py
Shared async HTTP connection pool and rate budget system for the WTB Solana trading bot.
"""

import asyncio
import logging
import time
from typing import Optional, Dict, Any, Callable, Coroutine
import httpx

try:
    from solana.rpc.async_api import AsyncClient
    SOLANA_INSTALLED = True
except ImportError:
    AsyncClient = Any
    SOLANA_INSTALLED = False

log = logging.getLogger("ConnectionPool")

# ---------------------------------------------------------------------------
# Rate Budget System
# ---------------------------------------------------------------------------

class RateBudget:
    """
    RateBudget enforces requests-per-minute limits using a token bucket / sliding window concept,
    with soft ceilings for low-priority calls and hard ceilings for high-priority ones.
    """
    def __init__(self, name: str, max_requests_per_minute: int, soft_ceiling_pct: float = 0.8):
        self._name = name
        self._max = max_requests_per_minute
        self._soft = int(max_requests_per_minute * soft_ceiling_pct)
        self._window_start = time.time()
        self._count = 0
        self._lock = asyncio.Lock()
        
        # Stats tracking
        self._stats = {
            "total_requests": 0,
            "throttle_events": 0,
            "errors": 0
        }

    def _reset_window_if_needed(self, now: float):
        """Reset the request counter if a minute has passed."""
        if now - self._window_start >= 60.0:
            self._window_start = now
            self._count = 0

    async def acquire(self, priority: str = 'normal'):
        """
        Wait until budget is available.
        priority='high' uses the hard ceiling (_max).
        priority='normal' uses the soft ceiling (_soft).
        """
        ceiling = self._max if priority == 'high' else self._soft
        warned = False
        
        while True:
            now = time.time()
            async with self._lock:
                self._reset_window_if_needed(now)
                
                if self._count < ceiling:
                    self._count += 1
                    self._stats["total_requests"] += 1
                    return
                
                wait_time = 60.0 - (now - self._window_start) + 0.1
                
                if not warned:
                    log.warning(f"[{self._name}] Rate limit approaching ({self._count}/{ceiling}). Throttling for {wait_time:.2f}s.")
                    self._stats["throttle_events"] += 1
                    warned = True

            # Sleep outside the lock so we don't block other tasks
            if wait_time > 0:
                await asyncio.sleep(wait_time)

    def observe(self):
        """Record a completed request if manually tracking outside acquire."""
        pass  # In this implementation, acquire increments the count directly.

    def observe_error(self):
        """Record an error associated with this API."""
        self._stats["errors"] += 1

    def get_stats(self) -> Dict[str, Any]:
        """Return statistics for the budget."""
        return {
            "name": self._name,
            "count_current_window": self._count,
            "total_requests": self._stats["total_requests"],
            "throttle_events": self._stats["throttle_events"],
            "errors": self._stats["errors"]
        }


DEXSCREENER_BUDGET = RateBudget('DexScreener', 300, soft_ceiling_pct=0.85)
JUPITER_BUDGET = RateBudget('Jupiter', 600, soft_ceiling_pct=0.8)
RPC_BUDGET = RateBudget('SolanaRPC', 100, soft_ceiling_pct=0.8)
JITO_BUDGET = RateBudget('JitoBlockEngine', 120, soft_ceiling_pct=0.85)


# ---------------------------------------------------------------------------
# Shared HTTP Client
# ---------------------------------------------------------------------------

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

async def get_client() -> httpx.AsyncClient:
    """Get or create the shared async HTTP client."""
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=30, max_keepalive_connections=10),
                timeout=httpx.Timeout(10.0, connect=5.0)
            )
        return _client


# ---------------------------------------------------------------------------
# Shared Solana RPC Client
# ---------------------------------------------------------------------------

_rpc_client: Optional[AsyncClient] = None
_rpc_lock = asyncio.Lock()
_rpc_semaphore = asyncio.Semaphore(5)

async def get_rpc_client() -> AsyncClient:
    """Get or create the shared Solana RPC client."""
    if not SOLANA_INSTALLED:
        raise ImportError("solana package is not installed.")
        
    global _rpc_client
    async with _rpc_lock:
        is_closed = False
        if _rpc_client is not None:
            closed_attr = getattr(_rpc_client, "is_closed", None)
            if closed_attr is not None:
                is_closed = closed_attr() if callable(closed_attr) else bool(closed_attr)

        if _rpc_client is None or is_closed:
            import os
            rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
            _rpc_client = AsyncClient(rpc_url)
            
        return _rpc_client

async def rpc_call(coro_factory: Callable[[], Coroutine], priority: str = 'normal'):
    """Execute an RPC call with semaphore gating."""
    await RPC_BUDGET.acquire(priority=priority)
    async with _rpc_semaphore:
        try:
            return await coro_factory()
        except Exception as e:
            RPC_BUDGET.observe_error()
            log.error(f"RPC call failed: {e}")
            raise


async def create_fallback_rpc_client() -> AsyncClient:
    """
    Create a one-shot AsyncClient pointing at the free public Solana RPC.
    Used as emergency fallback when the primary paid endpoint (Alchemy/Helius)
    fails to return transaction data after all retries.
    The caller is responsible for closing this client after use.
    """
    if not SOLANA_INSTALLED:
        raise ImportError("solana package is not installed.")
    return AsyncClient("https://api.mainnet-beta.solana.com")


# ---------------------------------------------------------------------------
# Convenience Functions
# ---------------------------------------------------------------------------

async def dex_get(url: str, params: Optional[Dict[str, Any]] = None, priority: str = 'normal') -> Optional[Dict[str, Any]]:
    """Rate-budgeted GET to DexScreener."""
    await DEXSCREENER_BUDGET.acquire(priority=priority)
    client = await get_client()
    try:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        DEXSCREENER_BUDGET.observe_error()
        log.error(f"DexScreener GET failed: {e}")
        return None

async def jupiter_get(url: str, params: Optional[Dict[str, Any]] = None, priority: str = 'normal') -> Optional[Dict[str, Any]]:
    """Rate-budgeted GET to Jupiter API."""
    await JUPITER_BUDGET.acquire(priority=priority)
    client = await get_client()
    try:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        JUPITER_BUDGET.observe_error()
        log.error(f"Jupiter GET failed: {e}")
        return None

async def jito_post(url: str, json_data: Dict[str, Any], priority: str = 'high') -> Optional[Dict[str, Any]]:
    """Rate-budgeted POST to Jito Block Engine."""
    await JITO_BUDGET.acquire(priority=priority)
    client = await get_client()
    try:
        response = await client.post(url, json=json_data, timeout=5.0)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        JITO_BUDGET.observe_error()
        log.error(f"Jito POST failed: {e}")
        return None

async def close_all():
    """Close all connection pools. Called during shutdown."""
    global _client, _rpc_client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None
        
    if _rpc_client is not None:
        try:
            await _rpc_client.close()
        except Exception as e:
            log.debug(f"Error closing RPC client: {e}")
        _rpc_client = None

def get_stats() -> Dict[str, Any]:
    """Return consolidated rate budget stats for dashboard display."""
    return {
        "DexScreener": DEXSCREENER_BUDGET.get_stats(),
        "Jupiter": JUPITER_BUDGET.get_stats(),
        "SolanaRPC": RPC_BUDGET.get_stats(),
        "JitoBlockEngine": JITO_BUDGET.get_stats(),
    }
