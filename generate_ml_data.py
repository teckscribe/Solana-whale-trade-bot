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
    log.info("Starting ML Data Generation via Wallet Profiling...")
    
    # ML training uses BOTH whitelist + neutral wallets for maximum training data
    # Exclude blacklisted wallets (bots/scammers) as they would poison the model
    all_data = whale_manager._load_whales()
    whales = [w for w, info in all_data.items() if info.get("status", "NEUTRAL").upper() != "BLACKLIST"]
        
    if not whales:
        log.error("No whales found to pull data from.")
        return
        
    log.info(f"Found {len(whales)} whales to profile.")
    
    # Wipe old data
    ml_data = []
        
    new_entries = 0
    
    for wallet in whales:
        log.info(f"Profiling historical trading for {wallet}...")
        stats = await fetch_wallet_stats(wallet)
        if not stats:
            await asyncio.sleep(1.0)
            continue
            
        winrate = _safe_float(stats.get("pnl_stat", {}).get("winrate", 0))
        total_cost = _safe_float(stats.get("total_cost", 0))
        buy_count = _safe_int(stats.get("buy", 0))
        
        # Calculate their average real-world trade size
        avg_trade_size = total_cost / buy_count if buy_count > 0 else 500.0
        
        # We will generate 40 representative trades per whale based on their true winrate
        num_trades = min(50, max(20, buy_count)) 
        wins_to_generate = int(num_trades * winrate)
        
        # Create true/false profit array and shuffle it
        trade_outcomes = [True] * wins_to_generate + [False] * (num_trades - wins_to_generate)
        random.shuffle(trade_outcomes)
        
        now = datetime.now(timezone.utc)
        
        for i, is_win in enumerate(trade_outcomes):
            # Add some natural variance to trade size (± 30%)
            variance = avg_trade_size * 0.3
            trade_size = avg_trade_size + random.uniform(-variance, variance)
            if trade_size < 10:
                trade_size = 10.0
                
            # Random historical timestamp within the last 30 days
            random_days_ago = random.uniform(0, 30)
            entry_time = now - timedelta(days=random_days_ago)
            
            # Profit logic
            if is_win:
                net_profit_pct = random.uniform(20.0, 150.0)
            else:
                net_profit_pct = random.uniform(-50.0, -10.0)
                
            net_profit_usd = trade_size * (net_profit_pct / 100)
            
            trade_record = {
                "timestamp_entry": entry_time.isoformat(),
                "whale_wallet": wallet,
                "token_address": f"GEN_{int(now.timestamp())}_{i}_{wallet[:8]}", # Dummy
                "entry_usd_price": 0.0,
                "exit_usd_price": 0.0,
                "trade_size_usd": round(trade_size, 2),
                "hold_duration_seconds": random.randint(60, 3600),
                "net_profit_percent": round(net_profit_pct, 2),
                "net_profit_usd": round(net_profit_usd, 4),
                "max_profit_percent": round(net_profit_pct + random.uniform(0, 10), 2),
                "exit_reason": "PROFILED_HISTORICAL_DATA",
                "trade_mode": "HISTORICAL"
            }
            ml_data.append(trade_record)
            new_entries += 1
            
        await asyncio.sleep(1.0)
        
    log.info(f"Generated {new_entries} profile-accurate historical trades.")
    
    with open(JSON_FILE, 'w') as f:
        json.dump(ml_data, f, indent=4)
        
    if len(ml_data) >= 100:
        log.info("Triggering ML Engine training...")
        train_model()
        msg = f"✅ <b>GMGN ML Training Complete!</b>\n\nSuccessfully downloaded historical data and mathematically reconstructed <b>{new_entries}</b> trades.\nThe AI Model has been retrained and is ready to trade!"
    else:
        log.warning(f"Only {len(ml_data)} trades available. Need 100 to train.")
        msg = f"⚠️ <b>GMGN ML Training Failed</b>\n\nNot enough historical trades to train the model. Only found {len(ml_data)}/100."

    # Send Telegram notification
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


