"""
scanner.py
Manages the Solana WebSocket connection and wallet subscriptions.

Key features:
  - Dynamically subscribes/unsubscribes to wallets on the fly without dropping connection.
  - Listens for `logsNotification` events targeting tracked whales.
  - Filters out failed transactions.
  - Automatically reconnects if the WebSocket drops.

Credentials loaded from .env:
  SOLANA_WSS_URL
  WHALE_WALLETS (initial list)
"""

import asyncio
import json
import logging
import websockets
import os
import time
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger("WhaleScanner")

WSS_URL = os.getenv("SOLANA_WSS_URL", "wss://api.mainnet-beta.solana.com")


def _safe_url(url: str) -> str:
    """
    Host-only form of an endpoint URL, for logging.

    The Helius WSS URL carries its API key as a query parameter, so logging the URL wrote a
    live credential into bot_debug.log on every connect and reconnect — a file the dashboard
    used to serve unauthenticated. Log the host; never the query string.
    """
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}" if parts.netloc else "<endpoint>"
    except Exception:
        return "<endpoint>"


SAFE_WSS_URL = _safe_url(WSS_URL)

class WhaleScanner:
    def __init__(self, callback):
        self.callback = callback
        self.active_wallets = set()
        self.req_id_to_wallet = {}
        self.sub_id_to_wallet = {}
        self.req_id = 1
        self.websocket = None
        self.wallet_tx_times = {}

    async def update_wallets(self, new_wallets: set):
        if self.active_wallets != new_wallets:
            log.info(f"Active wallets updated. Current count: {len(self.active_wallets)}. New count: {len(new_wallets)}.")
        if not self.websocket:
            # If not connected yet, just update the active set
            self.active_wallets = new_wallets
            return
            
        wallets_to_add = new_wallets - self.active_wallets
        wallets_to_remove = self.active_wallets - new_wallets
        
        for wallet in wallets_to_remove:
            # Find the sub_id for this wallet
            sub_id = None
            for sid, w in list(self.sub_id_to_wallet.items()):
                if w == wallet:
                    sub_id = sid
                    del self.sub_id_to_wallet[sid]
                    break
                    
            if sub_id:
                unsub_msg = {
                    "jsonrpc": "2.0",
                    "id": self.req_id,
                    "method": "logsUnsubscribe",
                    "params": [sub_id]
                }
                await self.websocket.send(json.dumps(unsub_msg))
                log.info(f"Unsubscribed from inactive whale: {wallet}")
                self.req_id += 1
                
        for wallet in wallets_to_add:
            sub_msg = {
                "jsonrpc": "2.0",
                "id": self.req_id,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [wallet]},
                    {"commitment": "confirmed"}
                ]
            }
            self.req_id_to_wallet[self.req_id] = wallet
            await self.websocket.send(json.dumps(sub_msg))
            log.info(f"Subscribed to new whale: {wallet}")
            self.req_id += 1
            await asyncio.sleep(0.1) # Throttle to prevent websocket rate limits
            
        self.active_wallets = new_wallets

    async def start(self):
        """
        Connects to the Solana WebSocket and listens for transactions.
        """
        if not self.active_wallets:
            log.warning("No initial WHALE_WALLETS defined.")

        backoff = 2
        while True:
            # Don't bother connecting if we have absolutely 0 whales to track
            if not self.active_wallets:
                log.info("No active wallets to track. Sleeping for 5s...")
                await asyncio.sleep(5)
                continue
                
            log.info(f"Attempting WebSocket connection to {SAFE_WSS_URL}...")
            try:
                # ping_interval keeps the connection alive even if no whales are trading
                async with websockets.connect(WSS_URL, ping_interval=20, ping_timeout=20) as websocket:
                    self.websocket = websocket
                    log.info(f"Connected to {SAFE_WSS_URL}")
                    
                    # Initial subscriptions
                    self.req_id_to_wallet.clear()
                    self.sub_id_to_wallet.clear()
                    for wallet in self.active_wallets:
                        sub_msg = {
                            "jsonrpc": "2.0",
                            "id": self.req_id,
                            "method": "logsSubscribe",
                            "params": [
                                {"mentions": [wallet]},
                                {"commitment": "confirmed"}
                            ]
                        }
                        self.req_id_to_wallet[self.req_id] = wallet
                        await websocket.send(json.dumps(sub_msg))
                        log.info(f"Subscribed to logs for wallet: {wallet}")
                        self.req_id += 1
                        await asyncio.sleep(0.1) # Throttle initial subscriptions
                        
                    # Reset backoff on successful connect
                    backoff = 2

                    # Listen for messages
                    async for message in websocket:
                        data = json.loads(message)
                        
                        # Handle subscription confirmations to store subscription IDs
                        if "result" in data and "id" in data and type(data["result"]) is int:
                            sub_id = data["result"]
                            req_id = data["id"]
                            wallet = self.req_id_to_wallet.get(req_id)
                            if wallet:
                                self.sub_id_to_wallet[sub_id] = wallet
                                
                        # Handle JSON-RPC errors
                        if "error" in data:
                            log.error(f"RPC Provider Error: {data['error'].get('message', data['error'])}")
                                
                        # Filter for log notifications
                        if "method" in data and data["method"] == "logsNotification":
                            sub_id = data["params"]["subscription"]
                            result = data["params"]["result"]
                            signature = result["value"]["signature"]
                            err = result["value"]["err"]
                            
                            wallet = self.sub_id_to_wallet.get(sub_id, "Unknown")
                            
                            if err is None:
                                current_time = time.time()
                                if wallet not in self.wallet_tx_times:
                                    self.wallet_tx_times[wallet] = []
                                    
                                # Keep only timestamps from the last 1 second
                                self.wallet_tx_times[wallet] = [t for t in self.wallet_tx_times[wallet] if current_time - t <= 1.0]
                                
                                if len(self.wallet_tx_times[wallet]) >= 2:
                                    log.debug(f"Throttled spam transaction from {wallet}")
                                else:
                                    self.wallet_tx_times[wallet].append(current_time)
                                    log.info(f"New successful TX detected: {signature}")
                                    asyncio.create_task(self.callback(signature, wallet))
                                
            except websockets.ConnectionClosed:
                log.warning(f"WebSocket connection closed, reconnecting in {backoff}s...")
                self.websocket = None
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)
            except Exception as e:
                log.error(f"WebSocket error: {e}, reconnecting in {backoff}s...")
                self.websocket = None
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)

async def subscribe_to_wallets(callback):
    """Legacy wrapper for compatibility if needed."""
    scanner = WhaleScanner(callback)
    await scanner.start()


