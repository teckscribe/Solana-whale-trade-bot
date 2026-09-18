"""
main.py
Central entry point for the Solana Whale Tracker Bot.

Key features:
  - Initializes the WhaleScanner to listen for transactions.
  - Runs the Discovery Engine loop in the background (every 15 minutes).
  - Routes detected signatures to the decoder and paper trader.
  - Completely asynchronous for high performance.

Credentials loaded from .env:
  SOLANA_RPC_URL
  SOLANA_WSS_URL
"""

import asyncio
import logging
from logging.handlers import TimedRotatingFileHandler
import os
import json
import time
from dotenv import load_dotenv
load_dotenv()

from scanner import WhaleScanner
from decoder import decode_transaction
from trade_brain import record_trade
from discovery import run_discovery_cycle
from gmgn_discovery import run_gmgn_discovery
import whale_manager
from ml_engine import train_model
from solders.keypair import Keypair

file_handler = TimedRotatingFileHandler("bot_debug.log", when="D", interval=1, backupCount=3, encoding='utf-8')
file_handler.setLevel(logging.DEBUG)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

logging.basicConfig(
    level=logging.DEBUG, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[file_handler, console_handler],
    force=True
)

# Silence noisy 3rd party debug logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("solana").setLevel(logging.WARNING)
log = logging.getLogger("WhaleBotMain")

import settings_manager

RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

async def handle_new_transaction(signature: str, wallet: str):
    """
    Callback fired by the scanner when a target wallet executes a transaction.
    """
    log.info(f"Main loop received signature: {signature} from wallet: {wallet}")
    
    # Update JSON activity to prevent the whale from being pruned
    whale_manager.update_activity(wallet)
    
    # 1. Decode the transaction to find what was bought/sold
    trade_data = await decode_transaction(signature, wallet)
    
    if trade_data:
        # 2. Record it in our paper trading ledger
        await record_trade(trade_data)

async def discovery_loop(scanner: WhaleScanner):
    """
    Runs in the background, continuously finding new whales and updating the scanner.
    """
    log.info("Starting background Discovery Loop...")
    while True:
        try:
            # Always sync with JSON as the primary source of truth first
            active_whales = whale_manager.get_active_whales(limit=100, max_age_hours=24)
            log.info(f"Loaded {len(active_whales)} active whales from JSON.")
            await scanner.update_wallets(active_whales)
            
            new_whales = await run_discovery_cycle(RPC_URL)
            if new_whales:
                log.info(f"Passing {len(new_whales)} discovered whales to JSON database...")
                # Save new whales to JSON file
                whale_manager.add_whales(new_whales)
                
                # Fetch up to 100 active whales again after adding
                active_whales = whale_manager.get_active_whales(limit=100, max_age_hours=24)
                await scanner.update_wallets(active_whales)
            
            # Run GMGN Smart Money discovery (sends Telegram notifications for approval)
            if bool(settings_manager.get("GMGN_DISCOVERY_ENABLED")):
                try:
                    await run_gmgn_discovery()
                except Exception as gmgn_e:
                    log.warning(f"GMGN Discovery error (non-fatal): {gmgn_e}")

        except Exception as e:
            log.error(f"Error in discovery loop: {e}")

        interval = int(settings_manager.get("DISCOVERY_INTERVAL_MINUTES")) * 60
        log.info(f"Discovery loop sleeping for {interval} seconds...")
        await asyncio.sleep(interval)

async def json_sync_loop(scanner: WhaleScanner):
    """
    Stat-throttled sync loop:
    Checks if whitelist or discovery DB has been updated externally by Telegram bot.
    If changed, reloads RAM cache and updates the scanner in real time.
    """
    while True:
        try:
            if whale_manager.refresh_if_changed():
                active_whales = whale_manager.get_active_whales()
                await scanner.update_wallets(active_whales)
                log.info(f"Scanner wallets updated: {len(active_whales)} active whales.")
        except Exception as e:
            log.error(f"Error in whale sync loop: {e}")
        await asyncio.sleep(3)

async def ml_retrain_loop():
    """
    Periodically retrains the ML model on recent paper trades.
    """
    log.info("Starting ML Retraining Loop (runs every 6 hours)...")
    while True:
        await asyncio.sleep(6 * 3600)
        if not bool(settings_manager.get("ML_ENGINE")):
            log.info("ML Engine disabled in settings. Skipping this retraining cycle.")
            continue
        try:
            log.info("Triggering ML model retraining...")
            await asyncio.to_thread(train_model)
            log.info("ML Retraining complete.")
        except Exception as e:
            log.error(f"Error in ML retraining loop: {e}")

def write_heartbeat(status: str = "running"):
    """Atomically records process status and heartbeat timestamp for web dashboard."""
    try:
        os.makedirs("data", exist_ok=True)
        hb_file = os.path.join("data", "bot_heartbeat.json")
        tmp_file = f"{hb_file}.tmp_{os.getpid()}"
        with open(tmp_file, "w") as f:
            json.dump({
                "pid": os.getpid(),
                "status": status,
                "timestamp": time.time(),
                "service": "wtb"
            }, f)
        os.replace(tmp_file, hb_file)
    except Exception:
        pass

async def heartbeat_loop():
    """Periodically updates heartbeat file so dashboard knows the scanner is alive."""
    while True:
        write_heartbeat("running")
        await asyncio.sleep(4)

async def main():
    log.info("Starting Solana Whale Tracker Bot with Discovery Engine (TRAM Mode)...")
    
    # Restore any orphaned trades from previous crashed runs into RAM
    from trade_brain import restore_active_trades, flush_all_state_now, STATE
    await restore_active_trades()

    # Preload token metadata for active positions to eliminate cold latency
    try:
        from jupiter_api import preload_token_metadata
        open_tokens = set(STATE.active.keys())
        if open_tokens:
            await preload_token_metadata(open_tokens)
    except Exception as pe:
        log.warning(f"Failed to preload token metadata: {pe}")
    
    # Initialize the scanner
    scanner = WhaleScanner(handle_new_transaction)
    
    # Start the scanner in the background
    scanner_task = asyncio.create_task(scanner.start())
    
    # Start the discovery loop in the foreground (or also background)
    discovery_task = asyncio.create_task(discovery_loop(scanner))
    
    # Fast sync for Telegram approvals
    sync_task = asyncio.create_task(json_sync_loop(scanner))
    
    # Start the ML retraining loop
    ml_task = asyncio.create_task(ml_retrain_loop())

    # Start the heartbeat loop for web dashboard status
    heartbeat_task = asyncio.create_task(heartbeat_loop())
    
    # Start the FastAPI Web Dashboard Server on Port 8101 (or WEB_PORT from .env)
    # If running alongside standalone wtb-web.service, avoids port collisions automatically
    web_task = None
    run_embedded = os.getenv("EMBEDDED_WEB_SERVER", "auto").strip().lower()
    if run_embedded not in ["false", "0", "no", "disabled"]:
        web_port = int(os.getenv("WEB_PORT", "8101"))
        import socket
        port_in_use = False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                port_in_use = (s.connect_ex(('127.0.0.1', web_port)) == 0)
        except Exception:
            port_in_use = False

        if port_in_use:
            log.info(f"Port {web_port} is already active (handled by standalone web service). Skipping embedded dashboard.")
        else:
            try:
                from web_server import app as fastapi_app
                import uvicorn
                web_config = uvicorn.Config(app=fastapi_app, host="0.0.0.0", port=web_port, log_level="info")
                web_server = uvicorn.Server(web_config)
                web_task = asyncio.create_task(web_server.serve())
                log.info(f"🚀 Web Dashboard listening on http://0.0.0.0:{web_port}")
            except Exception as web_e:
                log.error(f"Failed to start FastAPI Web Server: {web_e}")

    # Wait for all (they run forever)
    named_tasks = {
        "scanner": scanner_task,
        "discovery": discovery_task,
        "json_sync": sync_task,
        "ml_retrain": ml_task,
        "heartbeat": heartbeat_task,
    }
    if web_task:
        named_tasks["web_dashboard"] = web_task

    try:
        await _supervise(named_tasks)
    finally:
        write_heartbeat("stopped")
        log.info("Executing graceful shutdown: flushing TRAM state and closing connections...")
        try:
            await flush_all_state_now()
        except Exception as fe:
            log.error(f"Error during final state flush: {fe}")
        try:
            from connection_pool import close_all
            await close_all()
        except Exception as ce:
            log.error(f"Error closing connection pools: {ce}")
        log.info("Shutdown complete.")


async def _supervise(named_tasks: dict):
    """
    Wait on all long-running tasks, logging any that die instead of letting the first
    failure tear down the process. Only exits when every task has finished.

    CORE_TASKS are the ones the bot cannot trade without; losing one is fatal and worth
    exiting for so systemd restarts us. Losing the dashboard is not.
    """
    CORE_TASKS = {"scanner", "discovery"}
    pending = set(named_tasks.values())
    name_of = {t: n for n, t in named_tasks.items()}

    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            name = name_of.get(task, "unknown")
            try:
                task.result()
                log.warning(f"Task '{name}' exited cleanly. It was expected to run forever.")
            except asyncio.CancelledError:
                log.warning(f"Task '{name}' was cancelled.")
            except Exception as e:
                log.error(f"Task '{name}' crashed: {e!r}", exc_info=True)
                try:
                    from telegram_notifier import send_error_alert
                    send_error_alert(
                        f"🚨 Bot task '{name}' crashed: {e!r}\n"
                        f"{'Core task — the bot is shutting down for a restart.' if name in CORE_TASKS else 'Non-core — trading continues.'}"
                    )
                except Exception:
                    pass

            if name in CORE_TASKS:
                log.critical(f"Core task '{name}' is gone. Shutting down so the service manager restarts us.")
                for t in pending:
                    t.cancel()
                return


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")






