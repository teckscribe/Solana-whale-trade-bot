import os
import json
import logging
import asyncio
import subprocess
import random
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

import whale_manager
from ml_engine import train_model

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger("MLGenerator")

JSON_FILE = "ml_training_data.json"
GMGN_API_KEY = os.getenv("GMGN_API_KEY")

def _safe_float(val):
    try:
        return float(val) if val is not None else 0.0
    except (ValueError, TypeError):
        return 0.0

def _safe_int(val):
    try:
        return int(val) if val is not None else 0
    except (ValueError, TypeError):
        return 0

async def fetch_wallet_stats(wallet: str) -> dict:
    if not GMGN_API_KEY:
        log.error("GMGN_API_KEY not found!")
        return {}
        
    try:
        npx_prefix = "npx.cmd" if os.name == "nt" else "/usr/bin/npx"
        cmd_str = f"{npx_prefix} -y gmgn-cli portfolio stats --chain sol --wallet {wallet} --period 30d --raw"
        
        env = os.environ.copy()
        env["GMGN_API_KEY"] = GMGN_API_KEY
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
        
        proc = await asyncio.to_thread(
            subprocess.run, cmd_str, capture_output=True, text=True, encoding="utf-8", env=env, check=False, shell=True
        )
        
        if proc.returncode != 0:
            log.warning(f"gmgn-cli failed for {wallet}: {proc.stderr.strip()}")
            return {}
            
        return json.loads(proc.stdout.strip())
    except Exception as e:
        log.error(f"Failed to fetch stats for {wallet}: {e}")
        return {}

async def generate_dataset():
    """
    Synchronizes the legacy ml_training_data.json mirror with the authoritative
    append-only ml_training_data.jsonl ledger and triggers ML model training on real
    recorded follower outcomes (never fabricating synthetic random trades).
    """
    from trade_history import load_trade_history
    log.info("Synchronizing canonical trade history ledger for ML training...")
    records = load_trade_history()

    try:
        with open(JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=4)
        log.info(f"Synchronized {len(records)} canonical trade records to {JSON_FILE}.")
    except Exception as e:
        log.error(f"Failed to sync {JSON_FILE}: {e}")

    if len(records) >= 100:
        log.info("Triggering ML Engine training on canonical trade history...")
        trained = train_model()
        if trained:
            msg = (
                f"✅ <b>ML Training Complete!</b>\n\n"
                f"Trained on <b>{len(records)}</b> verified historical follower trade records."
            )
        else:
            msg = (
                f"⚠️ <b>ML Training Skipped</b>\n\n"
                f"Need at least 100 non-timeout actioned trades (have {len(records)} total records)."
            )
    else:
        log.warning(f"Only {len(records)} canonical trades available. Need 100 to train.")
        msg = (
            f"⚠️ <b>ML Training Deferred</b>\n\n"
            f"Not enough real follower trades to train the model yet ({len(records)}/100)."
        )

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        import urllib.request, urllib.parse
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({'chat_id': chat_id, 'text': msg, 'parse_mode': 'HTML'}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=data))
        except Exception as e:
            log.error(f"Failed to send Telegram completion notice: {e}")

if __name__ == "__main__":
    asyncio.run(generate_dataset())


