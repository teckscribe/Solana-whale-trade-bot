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
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solders.signature import Signature

load_dotenv()
log = logging.getLogger("WhaleDecoder")

RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

from connection_pool import get_rpc_client

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Global semaphore to strictly limit concurrent RPC requests across all parallel decoding tasks
_rpc_semaphore = asyncio.Semaphore(2)

async def decode_transaction(signature_str: str, whale_wallet: str):
    """
    Fetches the transaction and parses token balances to determine
    what the whale bought and sold.
    """
    log.info(f"Decoding TX: {signature_str}")
    try:
        sig = Signature.from_string(signature_str)
        
        # Initial delay because WSS is faster than public RPC indexing
        await asyncio.sleep(3.0)
        
        # Retry loop for RPC indexing delays and 429 rate limits
        response = None
        rpc_client = await get_rpc_client()
        for attempt in range(6):
            async with _rpc_semaphore:
                try:
                    response = await rpc_client.get_transaction(
                        sig, 
                        commitment="confirmed",
                        max_supported_transaction_version=0
                    )
                    if response and response.value:
                        break
                except Exception as rpc_e:
                    log.debug(f"RPC error on {signature_str[:8]}: {rpc_e}")

            delay = 3.0 + (attempt * 2.0)
            log.warning(f"Transaction {signature_str[:8]}... not ready or rate limited. Retrying in {delay}s... ({attempt+1}/6)")
            await asyncio.sleep(delay)

        if not response or not response.value:
            log.warning(f"Transaction {signature_str} failed to confirm on RPC after 6 attempts.")
            return None
            
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



