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

try:
    import discord_notifier
except ImportError:
    discord_notifier = None

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
    if discord_notifier:
        discord_notifier.send_trade_alert(trade_data)


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
    if discord_notifier:
        discord_notifier.send_discovery_alert(tokens)

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
    if discord_notifier:
        discord_notifier.send_exit_alert(trade_record)

def send_error_alert(error_text: str):
    """
    Sends a high-priority error alert to Telegram and Discord.
    """
    msg = f"⚠️ <b>SYSTEM WARNING</b> ⚠️\n\n{error_text}"
    send_message(msg)
    if discord_notifier:
        discord_notifier.send_error_alert(error_text)

def send_gmgn_wallet_approval(stats: dict):
    """
    Sends a verified GMGN wallet discovery alert with cross-checked copy-tradability
    metrics and inline Approve/Reject buttons for the user to decide.
    """
    addr = stats.get("address", "Unknown")
    sol_bal = float(stats.get("sol_balance", 0.0) or 0.0)
    wr_7d = float(stats.get("winrate_7d", 0.0) or 0.0)
    trades_7d = int(stats.get("total_trades_7d", 0) or 0)
    buy_7d = int(stats.get("buy_7d", 0) or 0)
    sell_7d = int(stats.get("sell_7d", 0) or 0)
    token_num_7d = int(stats.get("token_num_7d", 0) or 0)
    winning_tokens_7d = int(stats.get("winning_tokens_7d", stats.get("wins_7d", 0)) or 0)

    realized = float(stats.get("realized_profit", 0.0) or 0.0)
    roi_pct = float(stats.get("roi_pct", 0.0) or 0.0)
    fee_ratio = float(stats.get("fee_to_profit_pct", 0.0) or 0.0)
    runners_2x = int(stats.get("runners_2x_plus", 0) or 0)
    ai_score = stats.get("ai_score", "N/A")
    ai_status = stats.get("ai_status", "WHITELIST")
    tags = stats.get("tags", [])
    tags_str = ", ".join(tags) if tags else "None"

    if token_num_7d > 0:
        wr_detail = f"<b>{wr_7d:.1f}%</b> ({winning_tokens_7d}/{token_num_7d} tokens)"
    else:
        wr_detail = f"<b>{wr_7d:.1f}%</b> ({winning_tokens_7d}/{trades_7d} trades)"

    if buy_7d > 0 or sell_7d > 0:
        swaps_detail = f"<b>{trades_7d}</b> ({buy_7d} buys / {sell_7d} sells)"
    else:
        swaps_detail = f"<b>{trades_7d}</b>"

    msg = (
        f"🐋 <b>Verified Alpha Wallet Found!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 SOL Balance: <b>{sol_bal:.2f} SOL</b>\n"
        f"💵 7D Realized Profit: <b>${realized:,.2f}</b>\n"
        f"📈 7D Realized ROI: <b>{roi_pct:+.2f}%</b>\n"
        f"🚀 2x+ Runners (7D): <b>{runners_2x}</b>\n"
        f"⛽ Fee / Profit Ratio: <b>{fee_ratio:.1f}%</b>\n"
        f"🤖 AI Safety Score: <b>{ai_score}/100 ({ai_status})</b>\n"
        f"🏷️ Tags: {tags_str}\n\n"
        f"📊 <b>7D Activity:</b>\n"
        f"   • Win Rate: {wr_detail}\n"
        f"   • Swaps:    {swaps_detail}\n"
        f"   • Filter Check: ✅ <b>PASSED ALL PARAMETERS</b>\n\n"
        f"🔗 Address:\n<code>{addr}</code>\n\n"
        f"<i>Source: GMGN.ai Smart Money (Cross-Checked)</i>"
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



