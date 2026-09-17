"""
discovery.py
The Dynamic Whale Discovery Engine.

Key features:
  - Fetches top trending Solana tokens from DexScreener free API.
  - Pulls recent transactions for those tokens via Helius RPC.
  - Extracts the wallet addresses of the top volume traders.
  - Feeds new wallets back to the main scanner seamlessly.

Credentials loaded from .env:
  SOLANA_RPC_URL
"""
import os
import httpx
import logging
import asyncio
from solana.rpc.async_api import AsyncClient

# Initialize logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger("DiscoveryEngine")

GECKO_TRENDING_API = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"

async def get_trending_tokens(limit=3):
    """
    Fetches trending tokens from GeckoTerminal by querying trending pools on Solana.
    """
    log.info("Fetching trending tokens from GeckoTerminal...")
    trending_tokens = []
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(GECKO_TRENDING_API, timeout=10.0)
            if response.status_code == 200:
                data = response.json()
                pools = data.get("data", [])
                
                for pool in pools[:limit]:
                    # Extract the base token ID which looks like solana_EPjFWdd5...
                    token_id = pool.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "")
                    if token_id.startswith("solana_"):
                        token_address = token_id.replace("solana_", "")
                        
                        pool_name = pool.get("attributes", {}).get("name", "Unknown")
                        # e.g., "FOO / SOL" -> symbol "FOO"
                        symbol = pool_name.split(" / ")[0] if " / " in pool_name else "Unknown"
                        
                        if token_address:
                            trending_tokens.append({"address": token_address, "symbol": symbol})
                            log.info(f"Discovered trending token: {symbol} ({token_address})")
            else:
                log.error(f"GeckoTerminal API error: {response.status_code}")
    except Exception as e:
        log.error(f"Failed to fetch trending tokens: {e}")
        
    return trending_tokens

async def extract_whales_from_token(token_address, rpc_client: AsyncClient, max_wallets=2):
    """
    Uses the RPC to fetch recent transactions for a token, 
    and extracts the wallets that were heavily trading it.
    """
    log.info(f"Extracting top traders for token {token_address}...")
    whales = set()
    try:
        from solders.pubkey import Pubkey
        pubkey = Pubkey.from_string(token_address)
        
        # 1. Get recent signatures for the token mint address
        sig_response = await rpc_client.get_signatures_for_address(pubkey, limit=50)
        
        signatures_data = sig_response.value if hasattr(sig_response, 'value') else sig_response
        
        if not signatures_data:
            return set()
            
        signer_counts = {}
        
        for sig_info in signatures_data[:20]:
            try:
                # Fetch parsed transaction to see who signed it (the trader)
                tx_resp = await rpc_client.get_transaction(
                    sig_info.signature, 
                    encoding="jsonParsed", 
                    max_supported_transaction_version=0
                )
                tx_data = tx_resp.value if hasattr(tx_resp, 'value') else tx_resp
                
                if tx_data and tx_data.transaction and tx_data.transaction.transaction and tx_data.transaction.transaction.message:
                    account_keys = tx_data.transaction.transaction.message.account_keys
                    meta = tx_data.transaction.meta
                    
                    signer_wallet = None
                    for account in account_keys:
                        if account.signer:
                            signer_wallet = str(account.pubkey)
                            break # Only look at the primary signer
                            
                    if not signer_wallet or not meta:
                        continue
                        
                    # Map pubkeys to their index so we can check SOL balance changes
                    pubkey_indices = {str(account.pubkey): i for i, account in enumerate(account_keys)}
                        
                    # Calculate token delta for ALL owners to catch buyers/sellers even if they aren't the signer (e.g. Telegram Bots)
                    pre_bals = meta.pre_token_balances or []
                    post_bals = meta.post_token_balances or []
                    
                    owner_deltas = {}
                    for b in pre_bals:
                        if str(b.mint) == token_address:
                            owner = str(b.owner)
                            try:
                                owner_deltas[owner] = owner_deltas.get(owner, 0.0) - float(b.ui_token_amount.ui_amount_string or 0)
                            except: pass
                    for b in post_bals:
                        if str(b.mint) == token_address:
                            owner = str(b.owner)
                            try:
                                owner_deltas[owner] = owner_deltas.get(owner, 0.0) + float(b.ui_token_amount.ui_amount_string or 0)
                            except: pass
                            
                    # Anyone who had a significant balance change is either the Trader or the Liquidity Pool
                    for owner, delta in owner_deltas.items():
                        if abs(delta) > 0.0001:
                            # Now check if this owner swapped a WHALE amount of SOL
                            owner_idx = pubkey_indices.get(owner)
                            if owner_idx is not None and meta.pre_balances and meta.post_balances:
                                pre_sol = meta.pre_balances[owner_idx]
                                post_sol = meta.post_balances[owner_idx]
                                sol_delta = abs(pre_sol - post_sol) / 1_000_000_000 # Convert lamports to SOL
                                
                                # Only count them if they traded >= MIN_WHALE_SOL_VOLUME
                                min_vol = float(os.getenv("MIN_WHALE_SOL_VOLUME", "5.0"))
                                if sol_delta >= min_vol:
                                    signer_counts[owner] = signer_counts.get(owner, 0) + 1
                                else:
                                    log.info(f"Skipping trader {owner} - SOL Volume ({sol_delta:.2f}) < MIN_WHALE_SOL_VOLUME ({min_vol})")
                            
                # Sleep briefly to avoid hitting Helius rate limits on heavy RPC calls
                await asyncio.sleep(0.1)
            except Exception as e:
                log.warning(f"Failed to parse tx: {e}")
                continue
                
        # Filter out bots/routers: real whales don't usually swap the same token > 3 times in 20 recent txs
        for wallet, count in signer_counts.items():
            if count <= 3:
                whales.add(wallet)
                if len(whales) >= max_wallets:
                    break
                
    except Exception as e:
        if "429" in str(e):
            log.warning(f"API Rate Limit Exceeded (HTTP 429) while fetching token {token_address}. Skipping.")
        else:
            import traceback
            log.error(f"Failed to extract whales for token {token_address}:\n{traceback.format_exc()}")
        
    return whales

async def run_discovery_cycle(rpc_url):
    """
    Runs one full cycle of the discovery engine.
    Returns a set of newly discovered whale wallet addresses.
    """
    log.info("Starting Discovery Cycle...")
    new_whales = set()
    
    # 1. Get Trending Tokens
    tokens = await get_trending_tokens(limit=2)
    
    if not tokens:
        log.warning("No trending tokens found in this cycle.")
        return new_whales
        
    # Muted at user request so it doesn't spam every 15 minutes
    # from telegram_notifier import send_discovery_alert
    # send_discovery_alert(tokens)
        
    # 2. Extract whales for each token
    async with AsyncClient(rpc_url) as rpc_client:
        for token in tokens:
            whales = await extract_whales_from_token(token["address"], rpc_client, max_wallets=2)
            new_whales.update(whales)
            
            # Strict pacing: Sleep for 2.0s after fetching data for each token to prevent 429 API bans
            await asyncio.sleep(2.0)
            
    log.info(f"Discovery Cycle Complete. Found {len(new_whales)} active whales.")
    return new_whales



