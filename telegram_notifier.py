"""
telegram_notifier.py
Sends Whale Tracker alerts and trade notifications to Telegram.

Key features:
  - Dispatches messages asynchronously to avoid blocking the main trading loop.
  - Formats trade data nicely with emojis.
  
Credentials loaded from .env:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""

import os
import logging
import asyncio
import httpx
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("TelegramNotifier")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# We use httpx directly to send simple push messages without needing the full telegram bot framework
# This keeps the main scanner lightweight.
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

async def send_message_async(text: str, reply_markup: dict = None):
    """
    Asynchronously sends a text message to the configured Telegram chat.
    """
    if not BOT_TOKEN or not CHAT_ID:
        return
        
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    if reply_markup:
        payload["reply_markup"] = reply_markup
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(TELEGRAM_API_URL, json=payload, timeout=5.0)
            if resp.status_code != 200:
                log.error(f"Telegram notification failed: {resp.text}")
    except Exception as e:
        log.error(f"Failed to send Telegram message: {e}")

_background_tasks = set()

def send_message(text: str, reply_markup: dict = None):
    """
    Fire-and-forget wrapper to send a message from a synchronous context or 
    dispatch it as a background task in an async context.
    """
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(send_message_async(text, reply_markup))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        # If no loop is running, we just use asyncio.run (though not ideal if called frequently)
        asyncio.run(send_message_async(text, reply_markup))

def send_trade_alert(trade_data: dict):
    """
    Formats a trade dictionary into a nice Telegram alert.
    """
    token = trade_data.get("token", "Unknown")
    amount = trade_data.get("amount", 0)
    side = trade_data.get("side", "TRADE")
    wallet = trade_data.get("wallet", "Unknown")
    
    emoji = "🟢" if side.upper() == "BUY" else "🔴"
    if isinstance(amount, (int, float)):
        amount_str = f"${amount:.2f}"
    else:
        amount_str = str(amount)
        
    mode = trade_data.get("mode", "PAPER")
    if mode == "LIVE":
        action_str = "LIVE TRADE EXECUTED 💥 via GMGN"
    else:
        action_str = "Paper trade recorded 📝"
    
    msg = (
        f"🐋 <b>WHALE {side.upper()} ALERT</b> {emoji}\n\n"
        f"<b>Wallet:</b> <code>{wallet[:8]}...{wallet[-4:]}</code>\n"
        f"<b>Token:</b> {token}\n"
        f"<b>Amount:</b> {amount_str}\n\n"
        f"<i>Action: {action_str}</i>"
    )
    send_message(msg)


def send_discovery_alert(tokens: list):
    """
    Alerts when new trending tokens are found.
    """
    if not tokens:
        return
        
    lines = ["🔍 <b>Trending Tokens Discovered</b>\n"]
    for t in tokens:
        lines.append(f"• {t.get('symbol', 'Unknown')} (<code>{t.get('address')}</code>)")
        
    lines.append("\n<i>Extracting top traders for tracking...</i>")
    send_message("\n".join(lines))

def send_exit_alert(trade_record: dict):
    """
    Sends a Telegram alert when a trade is closed (paper or live GMGN).
    For live GMGN trades, shows both quoted and real P&L side by side.
    """
    token = trade_record.get("token_address", "Unknown")
    profit = trade_record.get("net_profit_percent", 0.0)
    profit_usd = trade_record.get("net_profit_usd", 0.0)
    reason = trade_record.get("exit_reason", "UNKNOWN")
    wallet = trade_record.get("whale_wallet", "Unknown")
    hold_secs = trade_record.get("hold_duration_seconds", 0)
    trade_size = trade_record.get("trade_size_usd", 0.0)
    engine = trade_record.get("execution_engine", "")

    # Real P&L from GMGN order_statistic (only available for live GMGN trades)
    real_profit_usd = trade_record.get("real_net_profit_usd")
    real_profit_pct = trade_record.get("real_net_profit_percent")
    fill_available  = trade_record.get("fill_data_available", False)

    emoji = "🚀" if profit > 0 else "📉"

    # Format hold time
    if hold_secs < 60:
        hold_str = f"{int(hold_secs)}s"
    elif hold_secs < 3600:
        hold_str = f"{int(hold_secs//60)}m {int(hold_secs%60)}s"
    else:
        hold_str = f"{int(hold_secs//3600)}h {int((hold_secs%3600)//60)}m"

    # Reason emoji
    reason_emojis = {
        "TAKE_PROFIT": "✅", "STOP_LOSS": "🛑",
        "TIMEOUT": "⏱️", "PANIC_SELL": "🚨",
        "DEAD_TOKEN_API": "💀", "DEAD_VOLUME": "😴",
        "GMGN_CLOSED": "✅"
    }
    reason_emoji = reason_emojis.get(reason, "📌")

    # Build P&L line
    if fill_available and real_profit_usd is not None and real_profit_pct is not None:
        pnl_line = (
            f"<b>Real P&L:</b> {'+' if real_profit_usd >= 0 else ''}{real_profit_usd:.4f} USD "
            f"({'+' if real_profit_pct >= 0 else ''}{real_profit_pct:.2f}%) ✅\n"
            f"<b>Quoted P&L:</b> {'+' if profit >= 0 else ''}{profit:.2f}%"
        )
    else:
        pnl_line = f"<b>Net Profit:</b> {'+' if profit >= 0 else ''}{profit:.2f}% (${profit_usd:.4f})"

    engine_line = f" | Engine: {engine}" if engine else ""

    msg = (
        f"🏁 <b>TRADE CLOSED</b> {emoji}\n\n"
        f"<b>Token:</b> <code>{token[:8]}...</code>\n"
        f"<b>Wallet:</b> <code>{wallet[:8]}...</code>\n"
        f"<b>Reason:</b> {reason_emoji} {reason}\n"
        f"<b>Size:</b> ${trade_size:.2f} | <b>Held:</b> {hold_str}\n"
        f"{pnl_line}\n\n"
        f"<i>Logged to ML Dataset{engine_line}</i>"
    )
    send_message(msg)

def send_error_alert(error_text: str):
    """
    Sends a high-priority error alert to Telegram.
    """
    msg = f"⚠️ <b>SYSTEM WARNING</b> ⚠️\n\n{error_text}"
    send_message(msg)

def send_gmgn_wallet_approval(stats: dict):
    """
    Sends a GMGN wallet discovery alert with detailed stats and
    inline Approve/Reject buttons for the user to decide.
    """
    addr = stats.get("address", "Unknown")
    sol_bal = stats.get("sol_balance", 0)
    
    wr_1d = stats.get("winrate_1d", 0)
    wr_7d = stats.get("winrate_7d", 0)
    wr_30d = stats.get("winrate_30d", 0)
    wr_all = stats.get("winrate_all", 0)
    
    trades_1d = stats.get("total_trades_1d", 0)
    trades_7d = stats.get("total_trades_7d", 0)
    trades_30d = stats.get("total_trades_30d", 0)
    trades_all = stats.get("total_trades_all", 0)
    
    wins_1d = stats.get("wins_1d", 0)
    wins_7d = stats.get("wins_7d", 0)
    wins_30d = stats.get("wins_30d", 0)
    wins_all = stats.get("wins_all", 0)
    
    realized = stats.get("realized_profit", 0)
    tags = stats.get("tags", [])
    tags_str = ", ".join(tags) if tags else "None"
    
    # Build the win rate display lines, only showing periods with data
    wr_lines = []
    if trades_1d > 0:
        wr_lines.append(f"   • 1D:   <b>{wr_1d:.0f}%</b> ({wins_1d}/{trades_1d} trades)")
    if trades_7d > 0:
        wr_lines.append(f"   • 7D:   <b>{wr_7d:.0f}%</b> ({wins_7d}/{trades_7d} trades)")
    if trades_30d > 0:
        wr_lines.append(f"   • 30D:  <b>{wr_30d:.0f}%</b> ({wins_30d}/{trades_30d} trades)")
    if trades_all > 0:
        wr_lines.append(f"   • All:  <b>{wr_all:.0f}%</b> ({wins_all}/{trades_all} trades)")
    
    if not wr_lines:
        wr_lines.append(f"   • 7D:   <b>{wr_7d:.0f}%</b>")
    
    wr_display = "\n".join(wr_lines)
    
    msg = (
        f"🐋 <b>High Winrate Wallet Found!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 SOL Balance: <b>{sol_bal:.2f} SOL</b>\n"
        f"💵 Realized Profit: <b>${realized:,.2f}</b>\n"
        f"🏷️ Tags: {tags_str}\n\n"
        f"📊 <b>Win Rates:</b>\n"
        f"{wr_display}\n\n"
        f"🔗 Address:\n<code>{addr}</code>\n\n"
        f"<i>Source: GMGN.ai Smart Money</i>"
    )
    
    # Send with inline keyboard buttons
    _send_message_with_buttons(msg, addr)


def _send_message_with_buttons(text: str, wallet_address: str):
    """
    Sends a Telegram message with inline Approve/Reject buttons.
    Uses the Telegram Bot API directly via httpx.
    """
    import json as _json
    
    if not BOT_TOKEN or not CHAT_ID:
        return
    
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ Add to Whitelist", "callback_data": f"gmgn_approve_{wallet_address}"},
                {"text": "❌ Skip", "callback_data": f"gmgn_reject_{wallet_address}"}
            ]
        ]
    }
    
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": _json.dumps(reply_markup)
    }
    
    async def _send():
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(TELEGRAM_API_URL, json=payload, timeout=5.0)
                if resp.status_code != 200:
                    log.error(f"Telegram button notification failed: {resp.text}")
        except Exception as e:
            log.error(f"Failed to send Telegram button message: {e}")
    
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_send())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        asyncio.run(_send())



