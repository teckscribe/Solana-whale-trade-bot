"""
decoder.py
Parses and decodes raw Solana transactions on-chain.

Key features:
  - Takes a transaction signature and fetches the parsed JSON from Helius.
  - Examines pre and post token balances to determine what was bought/sold.
  - Extracts the main signer (the whale).
  - Normalizes amounts based on token decimals.

Credentials loaded from .env:
  SOLANA_RPC_URL
"""

import os
import logging
import asyncio
import random
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solders.signature import Signature

load_dotenv()
log = logging.getLogger("WhaleDecoder")

RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

from connection_pool import get_rpc_client, create_fallback_rpc_client

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# ---------------------------------------------------------------------------
# Decode Metrics (exposed via web_server /api/stats)
# ---------------------------------------------------------------------------
_decode_stats = {
    "total_attempts": 0,
    "success": 0,
    "failed_after_retries": 0,
    "fallback_success": 0,
    "fallback_attempts": 0,
}

def get_decode_stats() -> dict:
    """Return a copy of decode metrics for dashboard display."""
    return dict(_decode_stats)

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------
MAX_RETRIES = 10
INITIAL_SLEEP = 1.5       # seconds before first RPC attempt (WSS→RPC indexing gap)
BASE_BACKOFF = 1.5        # exponential base
MAX_SINGLE_WAIT = 30.0    # cap per-retry wait
JITTER_RANGE = 0.5        # ±0.5s random jitter

async def decode_transaction(signature_str: str, whale_wallet: str):
    """
    Fetches the transaction and parses token balances to determine
    what the whale bought and sold.
    """
    log.info(f"Decoding TX: {signature_str}")
    _decode_stats["total_attempts"] += 1
    try:
        sig = Signature.from_string(signature_str)
        
        # Initial delay — WSS delivers notifications faster than RPC indexes them
        await asyncio.sleep(INITIAL_SLEEP)
        
        # Retry loop with exponential backoff + jitter
        response = None
        rpc_client = await get_rpc_client()
        for attempt in range(MAX_RETRIES):
            try:
                response = await rpc_client.get_transaction(
                    sig, 
                    commitment="confirmed",
                    max_supported_transaction_version=0
                )
                if response and response.value:
                    break
            except Exception as rpc_e:
                log.debug(f"RPC error on {signature_str[:10]}: {rpc_e}")

            # Exponential backoff: 1.5, 2.25, 3.375, 5.06, 7.59, 11.4, 17.1, 25.6, 30, 30
            delay = min(BASE_BACKOFF ** (attempt + 1), MAX_SINGLE_WAIT)
            delay += random.uniform(-JITTER_RANGE, JITTER_RANGE)
            delay = max(0.5, delay)
            log.warning(f"TX {signature_str[:10]}... not ready. Retry {attempt+1}/{MAX_RETRIES} in {delay:.1f}s")
            await asyncio.sleep(delay)

        # Fallback: try the free public Solana RPC as a last resort
        if not response or not response.value:
            log.info(f"Primary RPC exhausted for {signature_str[:10]}. Trying public fallback...")
            _decode_stats["fallback_attempts"] += 1
            fallback_client = None
            try:
                fallback_client = await create_fallback_rpc_client()
                await asyncio.sleep(2.0)  # brief pause before fallback
                response = await fallback_client.get_transaction(
                    sig,
                    commitment="confirmed",
                    max_supported_transaction_version=0
                )
                if response and response.value:
                    log.info(f"Fallback RPC succeeded for {signature_str[:10]}")
                    _decode_stats["fallback_success"] += 1
            except Exception as fb_e:
                log.debug(f"Fallback RPC error for {signature_str[:10]}: {fb_e}")
            finally:
                if fallback_client:
                    try:
                        await fallback_client.close()
                    except Exception:
                        pass

        if not response or not response.value:
            _decode_stats["failed_after_retries"] += 1
            log.warning(f"TX {signature_str} failed after {MAX_RETRIES} retries + fallback.")
            return None
        
        _decode_stats["success"] += 1
            
        tx = response.value.transaction
        meta = tx.meta
        
        if meta.err is not None:
            log.info(f"Transaction {signature_str} failed. Ignoring.")
            return None
            
        pre_balances = meta.pre_token_balances
        post_balances = meta.post_token_balances
        
        changes = {} # mint -> delta amount
        
        # Helper to process token balances for our whale wallets
        def process_balances(balances, is_post=False):
            for b in balances:
                owner = str(b.owner)
                if owner == whale_wallet:
                    mint = str(b.mint)
                    amount = float(b.ui_token_amount.ui_amount or 0)
                    if mint not in changes:
                        changes[mint] = 0.0
                    changes[mint] += amount if is_post else -amount
                    
        process_balances(pre_balances, is_post=False)
        process_balances(post_balances, is_post=True)
        
        # Extract native SOL changes for the whale
        try:
            account_keys = tx.transaction.message.account_keys
            whale_index = -1
            for i, key in enumerate(account_keys):
                key_str = str(key.pubkey) if hasattr(key, 'pubkey') else str(key)
                if key_str == whale_wallet:
                    whale_index = i
                    break
                    
            if whale_index != -1 and meta.pre_balances and meta.post_balances:
                pre_sol = meta.pre_balances[whale_index] / 1_000_000_000
                post_sol = meta.post_balances[whale_index] / 1_000_000_000
                sol_delta = post_sol - pre_sol
                
                # If the delta is significant (>0.001 SOL), it was likely a swap. 
                # This ignores tiny SOL balance changes from paying network fees (~0.000005).
                if abs(sol_delta) > 0.001:
                    if SOL_MINT not in changes:
                        changes[SOL_MINT] = 0.0
                    changes[SOL_MINT] += sol_delta
        except Exception as e:
            log.warning(f"Failed to parse native SOL balances for {signature_str}: {e}")
        
        # Identify bought and sold tokens
        bought = []
        sold = []
        for mint, delta in changes.items():
            if delta > 0.000001:  # Bought (balance increased)
                bought.append({"mint": mint, "amount": delta})
            elif delta < -0.000001: # Sold (balance decreased)
                sold.append({"mint": mint, "amount": abs(delta)})
                
        # BOTH legs are required. A balance increase on its own is not evidence the whale
        # bought anything — it is equally consistent with an airdrop, a transfer in from
        # another wallet they control, an LP receipt, or a reward claim.
        #
        # This was briefly relaxed to `if bought:` with a synthetic zero-amount SOL sell
        # appended when no sell leg existed. That turns every inbound token into a copy-trade
        # signal, which (a) is the standard way to bait copy-trading bots — dust a tracked
        # wallet with a token you control and the bot buys it — and (b) contaminates the
        # dataset with entries the whale never paid for, which is fatal while the whole point
        # of the current run is to measure whether following these whales has an edge.
        #
        # Genuine swaps always produce a sell leg: paying in SOL registers as a negative
        # SOL_MINT delta above the 0.001 threshold, and paying in any SPL token registers
        # directly. The only buys this misses are ones costing under ~0.001 SOL.
        if bought and sold:
            log.info(f"Whale Swap Detected! Bought: {bought} | Sold: {sold}")
            return {
                "signature": signature_str,
                "bought": bought,
                "sold": sold,
                "wallet": whale_wallet
            }
        elif bought:
            log.debug(
                f"TX {signature_str[:8]} increased the whale's balance of "
                f"{[b['mint'][:8] for b in bought]} with no corresponding payment — "
                f"airdrop/transfer/claim, not a buy. Ignoring."
            )
            return None
        else:
            log.debug(f"TX {signature_str} was not a clear swap for the whale.")
            return None

            
    except Exception as e:
        log.error(f"Error decoding TX {signature_str}: {e}")
        return None



