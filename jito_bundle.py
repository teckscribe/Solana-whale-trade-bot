"""
jito_bundle.py
Jito MEV Bundle Engine for WTB (Solana Whale Trade Bot).

Provides:
1. Multi-region Jito Block Engine communication (Global, Amsterdam, Frankfurt, NY, Tokyo)
2. Dynamic tip floor querying and percentile-based bidding (p25, p50, p75, p95)
3. Load-balanced tip account distribution across canonical Jito validators
4. Anti-sandwich and zero-loss revert protection for Solana transactions
5. Seamless dual-mode execution (Real on-chain bundles when keys present, simulation in paper mode)
"""

import os
import sys
import json
import time
import random
import logging
from typing import List, Dict, Any, Optional, Tuple

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import settings_manager
import connection_pool

log = logging.getLogger("JitoBundle")

# ---------------------------------------------------------------------------
# Canonical Jito Tip Accounts (Official Mainnet)
# ---------------------------------------------------------------------------
JITO_TIP_ACCOUNTS: List[str] = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]

# ---------------------------------------------------------------------------
# Jito Regional Block Engines
# ---------------------------------------------------------------------------
BLOCK_ENGINES: Dict[str, str] = {
    "mainnet": "https://mainnet.block-engine.jito.wtf/api/v1/bundles",
    "amsterdam": "https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "frankfurt": "https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "ny": "https://ny.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "tokyo": "https://tokyo.mainnet.block-engine.jito.wtf/api/v1/bundles",
}

TIP_FLOOR_URL = "https://bundles.jito.wtf/api/v1/bundles/tip_floor"

# Fallback tip: 10,000 lamports = 0.00001 SOL
DEFAULT_TIP_LAMPORTS = 10_000
MIN_TIP_LAMPORTS = 10_000
MAX_TIP_LAMPORTS = 50_000_000  # 0.05 SOL max ceiling

# Defensive imports for solders
try:
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.instruction import Instruction
    SOLDERS_AVAILABLE = True
except ImportError:
    SOLDERS_AVAILABLE = False


# In-memory tip floor cache (max 1 refresh every 10 seconds)
_tip_floor_cache: Dict[str, Any] = {}
_tip_floor_cache_ts: float = 0.0


def get_random_tip_account() -> str:
    """Returns a random canonical Jito tip account to distribute validator tipping."""
    return random.choice(JITO_TIP_ACCOUNTS)


def get_block_engine_url(region: Optional[str] = None) -> str:
    """Returns the URL of the selected Jito Block Engine region."""
    if not region:
        val = settings_manager.get("JITO_BLOCK_ENGINE_REGION")
        region = str(val or "mainnet").lower()
    return BLOCK_ENGINES.get(region, BLOCK_ENGINES["mainnet"])


async def get_tip_floor(percentile: Optional[str] = None) -> int:
    """
    Fetches live tip floor data from Jito.
    Returns tip amount in lamports for the requested percentile (default: p50).
    Cached for 10 seconds to avoid unnecessary network round trips.
    """
    global _tip_floor_cache, _tip_floor_cache_ts

    target_pct = percentile or str(settings_manager.get("JITO_TIP_PERCENTILE") or "p50").lower()
    now = time.time()

    # Serve cached value if fresh (< 10 seconds)
    if _tip_floor_cache and (now - _tip_floor_cache_ts < 10.0):
        val = _tip_floor_cache.get(f"landed_tips_{target_pct}")
        if val is not None:
            return max(MIN_TIP_LAMPORTS, min(int(val), MAX_TIP_LAMPORTS))

    try:
        client = await connection_pool.get_client()
        resp = await client.get(TIP_FLOOR_URL, timeout=3.0)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                _tip_floor_cache = data[0]
                _tip_floor_cache_ts = now
                val = _tip_floor_cache.get(f"landed_tips_{target_pct}")
                if val is not None:
                    lamports = int(float(val) * 1_000_000_000) if float(val) < 1.0 else int(val)
                    return max(MIN_TIP_LAMPORTS, min(lamports, MAX_TIP_LAMPORTS))
    except Exception as e:
        log.debug(f"Could not fetch dynamic tip floor from Jito: {e}")

    # Fallback to configured static tip
    tip_sol = float(settings_manager.get("JITO_TIP_SOL") or 0.0001)
    fallback_lamports = int(tip_sol * 1_000_000_000)
    return max(MIN_TIP_LAMPORTS, min(fallback_lamports, MAX_TIP_LAMPORTS))


async def get_effective_tip_lamports() -> int:
    """
    Computes the effective tip in lamports based on settings_manager.
    If JITO_DYNAMIC_TIP is True, uses live tip floor percentiles.
    Otherwise uses static JITO_TIP_SOL setting.
    """
    if not bool(settings_manager.get("JITO_ENABLED")):
        return 0

    use_dynamic = bool(settings_manager.get("JITO_DYNAMIC_TIP"))
    if use_dynamic:
        return await get_tip_floor()

    tip_sol = float(settings_manager.get("JITO_TIP_SOL") or 0.0001)
    lamports = int(tip_sol * 1_000_000_000)
    return max(MIN_TIP_LAMPORTS, min(lamports, MAX_TIP_LAMPORTS))


def build_tip_instruction(payer_pubkey_str: str, tip_lamports: int, tip_account_str: Optional[str] = None) -> Optional[Any]:
    """
    Builds a native SOL transfer instruction to a Jito tip account.
    Requires solders. Returns None if solders is not installed.
    """
    if not SOLDERS_AVAILABLE:
        log.debug("solders not installed; tip instruction cannot be constructed natively.")
        return None

    tip_account = tip_account_str or get_random_tip_account()
    try:
        from_pubkey = Pubkey.from_string(payer_pubkey_str)
        to_pubkey = Pubkey.from_string(tip_account)

        return transfer(
            TransferParams(
                from_pubkey=from_pubkey,
                to_pubkey=to_pubkey,
                lamports=tip_lamports,
            )
        )
    except Exception as e:
        log.error(f"Failed to build tip instruction: {e}")
        return None


async def send_bundle(signed_tx_base58_list: List[str], region: Optional[str] = None) -> Dict[str, Any]:
    """
    Sends a signed transaction bundle to Jito Block Engine.

    Parameters:
        signed_tx_base58_list: List of 1 to 5 base58-encoded signed transactions.
        region: Optional region override ("mainnet", "amsterdam", "ny", etc.)

    Returns:
        {"success": bool, "bundle_id": str, "error": str | None}
    """
    if not signed_tx_base58_list:
        return {"success": False, "bundle_id": "", "error": "empty_bundle"}

    if len(signed_tx_base58_list) > 5:
        return {"success": False, "bundle_id": "", "error": "bundle_exceeds_max_5_transactions"}

    url = get_block_engine_url(region)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendBundle",
        "params": [signed_tx_base58_list],
    }

    try:
        resp = await connection_pool.jito_post(url, payload, priority="high")
        if not resp:
            return {"success": False, "bundle_id": "", "error": "network_or_rate_limit_failure"}

        if "error" in resp:
            err_msg = resp["error"].get("message", str(resp["error"]))
            log.warning(f"Jito sendBundle returned error: {err_msg}")
            return {"success": False, "bundle_id": "", "error": err_msg}

        bundle_id = resp.get("result", "")
        log.info(f"⚡ Jito Bundle Submitted Successfully! Bundle ID: {bundle_id}")
        return {"success": True, "bundle_id": bundle_id, "error": None}

    except Exception as e:
        log.error(f"Error submitting Jito bundle: {e}")
        return {"success": False, "bundle_id": "", "error": str(e)}


async def get_bundle_statuses(bundle_ids: List[str], region: Optional[str] = None) -> Dict[str, Any]:
    """
    Checks the status of submitted bundles.

    Returns dict mapping bundle_id -> status details (Landed, Pending, Failed, etc.)
    """
    if not bundle_ids:
        return {}

    url = get_block_engine_url(region)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBundleStatuses",
        "params": [bundle_ids],
    }

    try:
        resp = await connection_pool.jito_post(url, payload, priority="normal")
        if not resp or "error" in resp:
            return {}
        return resp.get("result", {})
    except Exception as e:
        log.debug(f"Error checking bundle statuses: {e}")
        return {}


async def simulate_bundle_execution(token: str, side: str, amount_usd: float) -> Dict[str, Any]:
    """
    Simulates Jito atomic bundle execution for paper trading and offline testing.
    Estimates inclusion latency, tip cost, and confirms anti-MEV protection.
    """
    tip_lamports = await get_effective_tip_lamports()
    tip_sol = tip_lamports / 1_000_000_000.0
    tip_account = get_random_tip_account()
    region = str(settings_manager.get("JITO_BLOCK_ENGINE_REGION") or "mainnet")

    # Simulated network latency (30-80ms for block engine ingestion)
    simulated_latency_ms = random.uniform(35.0, 75.0)

    sim_result = {
        "simulation": True,
        "success": True,
        "bundle_id": f"sim_jito_{int(time.time()*1000)}_{random.randint(1000, 9999)}",
        "region": region,
        "token": token,
        "side": side,
        "trade_size_usd": amount_usd,
        "tip_lamports": tip_lamports,
        "tip_sol": tip_sol,
        "tip_account": tip_account,
        "anti_mev_protection": True,
        "atomic_execution": True,
        "simulated_latency_ms": round(simulated_latency_ms, 2),
    }

    log.info(
        f"🛡️ [Jito Simulation] {side} {token[:8]} | Size: ${amount_usd:.2f} | "
        f"Tip: {tip_sol:.5f} SOL ({tip_lamports} lamports to {tip_account[:6]}...) | "
        f"Engine: {region} ({simulated_latency_ms:.1f}ms)"
    )

    return sim_result
