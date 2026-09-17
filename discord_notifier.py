"""
discord_notifier.py
Sends Whale Tracker alerts and trade notifications to Discord via Webhook.

Key features:
  - Dispatches messages asynchronously to avoid blocking the main trading loop.
  - Rich Discord Embed formatting for buys, exits, errors, and token discoveries.
  
Credentials loaded from .env:
  DISCORD_WEBHOOK_URL
"""

import os
import logging
import asyncio
import httpx
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("DiscordNotifier")

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

# Embed colors (decimal)
COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xE74C3C
COLOR_BLUE = 0x3498DB
COLOR_ORANGE = 0xE67E22
COLOR_PURPLE = 0x9B59B6
COLOR_GREY = 0x95A5A6

_background_tasks = set()

async def send_embed_async(embed: dict) -> bool:
    """Asynchronously POSTs an embed to the Discord webhook."""
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL).strip()
    if not webhook_url:
        return False
        
    payload = {
        "embeds": [embed]
    }
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, json=payload, timeout=5.0)
            if resp.status_code not in (200, 204):
                log.warning(f"Discord notification failed: {resp.status_code} - {resp.text}")
                return False
            return True
    except Exception as e:
        log.warning(f"Failed to dispatch Discord message: {e}")
        return False

def send_embed(embed: dict):
    """Fire-and-forget async wrapper for Discord notifications."""
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL).strip()
    if not webhook_url:
        return
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(send_embed_async(embed))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        try:
            asyncio.run(send_embed_async(embed))
        except Exception:
            pass

def send_trade_alert(trade_data: dict):
    """Sends a rich Discord embed for whale buy/sell entry alerts."""
    token = trade_data.get("token", "Unknown")
    amount = trade_data.get("amount", 0)
    side = trade_data.get("side", "TRADE").upper()
    wallet = trade_data.get("wallet", "Unknown")
    mode = trade_data.get("mode", "PAPER").upper()
    
    amount_str = f"${amount:.2f}" if isinstance(amount, (int, float)) else str(amount)
    color = COLOR_GREEN if side == "BUY" else COLOR_RED
    action_str = "💥 LIVE TRADE via GMGN" if mode == "LIVE" else "📝 Paper Trade Recorded"
    
    embed = {
        "title": f"🐋 WHALE {side} ALERT",
        "color": color,
        "fields": [
            {"name": "Wallet", "value": f"`{wallet[:8]}...{wallet[-6:]}`", "inline": True},
            {"name": "Token", "value": f"`{token[:12]}...`", "inline": True},
            {"name": "Amount", "value": f"**{amount_str}**", "inline": True},
            {"name": "Mode", "value": f"{mode} ({action_str})", "inline": False},
        ],
        "footer": {"text": f"WTB Whale Engine • {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"}
    }
    send_embed(embed)

def send_exit_alert(trade_record: dict):
    """Sends a rich Discord embed when a trade closes with P&L breakdown."""
    token = trade_record.get("token_address", "Unknown")
    profit = trade_record.get("net_profit_percent", 0.0)
    profit_usd = trade_record.get("net_profit_usd", 0.0)
    reason = trade_record.get("exit_reason", "UNKNOWN")
    wallet = trade_record.get("whale_wallet", "Unknown")
    hold_secs = trade_record.get("hold_duration_seconds", 0)
    trade_size = trade_record.get("trade_size_usd", 0.0)
    
    real_profit_usd = trade_record.get("real_net_profit_usd")
    real_profit_pct = trade_record.get("real_net_profit_percent")
    fill_available = trade_record.get("fill_data_available", False)
    
    color = COLOR_GREEN if profit > 0 else COLOR_RED
    
    if hold_secs < 60:
        hold_str = f"{int(hold_secs)}s"
    elif hold_secs < 3600:
        hold_str = f"{int(hold_secs//60)}m {int(hold_secs%60)}s"
    else:
        hold_str = f"{int(hold_secs//3600)}h {int((hold_secs%3600)//60)}m"
        
    pnl_display = (
        f"Real: **{'+' if real_profit_usd >= 0 else ''}${real_profit_usd:.4f}** ({real_profit_pct:+.2f}%)"
        if fill_available and real_profit_usd is not None
        else f"Net: **{'+' if profit_usd >= 0 else ''}${profit_usd:.4f}** ({profit:+.2f}%)"
    )
    
    embed = {
        "title": f"🏁 TRADE CLOSED — {reason}",
        "color": color,
        "fields": [
            {"name": "Token", "value": f"`{token[:10]}...`", "inline": True},
            {"name": "Size", "value": f"${trade_size:.2f}", "inline": True},
            {"name": "Hold Duration", "value": hold_str, "inline": True},
            {"name": "P&L Result", "value": pnl_display, "inline": False},
            {"name": "Copied Whale", "value": f"`{wallet[:8]}...{wallet[-6:]}`", "inline": True},
        ],
        "footer": {"text": f"WTB Exit Engine • {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"}
    }
    send_embed(embed)

def send_error_alert(error_text: str):
    """Sends a Discord embed warning for critical bot errors."""
    embed = {
        "title": "⚠️ SYSTEM WARNING",
        "description": f"```\n{error_text[:1800]}\n```",
        "color": COLOR_ORANGE,
        "footer": {"text": f"WTB Alert • {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"}
    }
    send_embed(embed)

def send_discovery_alert(tokens: list):
    """Sends a Discord embed for newly discovered trending tokens."""
    if not tokens:
        return
    token_lines = [
        f"• **{t.get('symbol', 'Unknown')}** (`{t.get('address', '')[:8]}...`)"
        for t in tokens[:10]
    ]
    embed = {
        "title": "🔍 Trending Tokens Discovered",
        "description": "\n".join(token_lines),
        "color": COLOR_BLUE,
        "footer": {"text": f"WTB Discovery Engine • {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"}
    }
    send_embed(embed)
