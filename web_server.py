"""
web_server.py

Description:
This is the FastAPI backend for the Solana Whale Trading Bot UI. It replaces the old Streamlit 
dashboard and acts as a lightweight, ultra-fast API server.

Wiring:
- Runs as an independent systemd service (`wtb-ui.service`) on port 8101 (Localhost).
- Tunneled to the outside world via Ngrok (`wtb-ngrok.service`) when triggered by Telegram.
- Reads data directly from the bot's JSON databases (`live_trades.json`, `paper_trades.json`, 
  `ml_training_data.json`, `whitelist.json`, `discovery_db.json`) and the `.env` config file.
- Serves static frontend assets (HTML/CSS/JS) from the `static/` directory.
- The Javascript frontend (`app.js`) polls this server's `/api/*` endpoints every 5 seconds 
  to provide a seamless, lag-free live dashboard experience without requiring page reloads.
"""

import os
import re
import json
import time
import subprocess
import asyncio
import logging
import secrets
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from typing import Dict, Any, Optional

from pydantic import BaseModel
from datetime import datetime, timezone
from jupiter_api import get_sol_price_usd
import settings_manager
import whale_manager
import ai_whale_scorer

load_dotenv()

app = FastAPI(title="Whale Tracker Dashboard")

# ─────────────────────────────────────────────────────────────────────────────
# Authentication
#
# This server is tunneled to the public internet via ngrok. It previously had no auth of
# any kind, with allow_origins=["*"], while /api/download/bot_debug.log served the raw
# DEBUG log — which contains the full Helius WSS URL including its API key. Anyone who
# learned the ngrok URL could read the bot's state and lift the RPC credentials.
#
# Set WEB_AUTH_TOKEN in .env. Pass it as `Authorization: Bearer <token>` or `?token=<token>`
# (the query form exists so the static frontend and a plain browser can both authenticate).
# ─────────────────────────────────────────────────────────────────────────────

TOKEN_FALLBACK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".web_auth_token")


def _resolve_auth_token() -> str:
    """
    Returns the dashboard shared secret.
    If set to NONE, DISABLED, FALSE, or OFF, dashboard auth is disabled.
    """
    token = os.getenv("WEB_AUTH_TOKEN", "").strip()
    if token.upper() in ["NONE", "DISABLED", "FALSE", "OFF"]:
        return "DISABLED"
    if token:
        return token

    try:
        if os.path.exists(TOKEN_FALLBACK_FILE):
            with open(TOKEN_FALLBACK_FILE, "r", encoding="utf-8") as f:
                token = f.read().strip()
        if not token:
            token = secrets.token_urlsafe(32)
            with open(TOKEN_FALLBACK_FILE, "w", encoding="utf-8") as f:
                f.write(token)
            try:
                os.chmod(TOKEN_FALLBACK_FILE, 0o600)
            except Exception:
                pass  # best effort; Windows has no equivalent mode
        logging.warning(
            "WEB_AUTH_TOKEN is not set in .env. Using the generated token in %s instead. "
            "Open the dashboard at /?token=%s — or set WEB_AUTH_TOKEN in .env to pin it.",
            TOKEN_FALLBACK_FILE, token,
        )
        return token
    except Exception as e:
        logging.critical(
            "WEB_AUTH_TOKEN is unset and the fallback token file could not be used (%s). "
            "Refusing to serve an unauthenticated dashboard.", e
        )
        return ""


WEB_AUTH_TOKEN = _resolve_auth_token()


async def require_auth(request: Request, token: Optional[str] = Query(default=None)):
    """Rejects any request without the shared secret. If disabled, allows access."""
    if WEB_AUTH_TOKEN == "DISABLED":
        return True
    if not WEB_AUTH_TOKEN:
        raise HTTPException(status_code=503, detail="Dashboard auth not configured (WEB_AUTH_TOKEN unset).")

    presented = token or ""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        presented = header[7:].strip()

    # Constant-time compare so the token can't be recovered by timing the response.
    if not presented or not secrets.compare_digest(presented, WEB_AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True



# Same-origin only. The frontend is served by this app, so no cross-origin access is needed;
# "*" combined with allow_credentials let any website read the dashboard through the browser
# of anyone logged in to it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.getenv("WEB_ALLOWED_ORIGINS", "").split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["Authorization", "Content-Type"],
)

def trade_pnl(t: Dict[str, Any]) -> float:
    """
    Realised P&L for a trade record, preferring the verified on-chain fill.

    Must NOT be written as `t.get("real_net_profit_usd") or t.get("net_profit_usd")`:
    a genuinely break-even real fill is 0.0, which is falsy, so `or` would silently
    discard the verified number and fall back to the quoted estimate.
    """
    real = t.get("real_net_profit_usd")
    if real is not None:
        return float(real)
    return float(t.get("net_profit_usd", 0.0) or 0.0)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.middleware("http")
async def add_cache_control_header(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response

@app.get("/", response_class=HTMLResponse)
async def read_index():
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
    )

def get_scanner_status() -> bool:
    """
    Checks whether the trading bot / scanner (main.py / wtb.service) is actively running.
    Multi-layer check:
      1. Heartbeat file (data/bot_heartbeat.json) with PID liveness
      2. systemctl is-active wtb (if on Linux systemd)
      3. Process search for main.py
    """
    # 1. Heartbeat check
    hb_path = os.path.join(BASE_DIR, "data", "bot_heartbeat.json")
    if os.path.exists(hb_path):
        try:
            with open(hb_path, "r") as f:
                hb = json.load(f)
            if hb.get("status") == "running":
                age = time.time() - float(hb.get("timestamp", 0))
                if age < 12.0:
                    pid = hb.get("pid")
                    if pid and os.name != "nt":
                        try:
                            os.kill(pid, 0)
                            return True
                        except OSError:
                            return False
                    return True
            elif hb.get("status") == "stopped":
                return False
        except Exception:
            pass

    # 2. Linux systemd check (wtb.service is the bot/scanner service)
    if os.name != "nt":
        try:
            res = subprocess.run(
                ["systemctl", "is-active", "--quiet", "wtb"],
                capture_output=True,
                timeout=1
            )
            if res.returncode == 0:
                return True
            if res.returncode in [3, 4]:
                return False
        except Exception:
            pass

        # 3. pgrep check for main.py
        try:
            res = subprocess.run(
                ["pgrep", "-f", "main.py"],
                capture_output=True,
                timeout=1
            )
            if res.returncode == 0 and res.stdout:
                my_pid = os.getpid()
                pids = [int(p) for p in res.stdout.decode().split() if p.isdigit() and int(p) != my_pid]
                if pids:
                    return True
        except Exception:
            pass

    return False


@app.get("/api/dashboard")
async def get_dashboard(_: bool = Depends(require_auth)):
    raw_mode = str(settings_manager.get("TRADE_MODE")).strip().upper()
    trade_mode = "LIVE" if raw_mode in ["TRUE", "LIVE"] else "PAPER"
    
    trades_file = "live_trades.json" if trade_mode == "LIVE" else "paper_trades.json"
    
    active_trades = []
    if os.path.exists(trades_file):
        try:
            with open(trades_file, "r") as f:
                active_trades = json.load(f)
                if isinstance(active_trades, dict):
                    active_trades = list(active_trades.values())
        except Exception: pass
        
    wallet_data = {"balance": 0.0, "initial": 0.0}
    if trade_mode == "PAPER":
        wallet_data = {
            "balance": float(settings_manager.get("PAPER_WALLET_BALANCE")),
            "initial": float(settings_manager.get("PAPER_BALANCE_USD")),
        }
    elif os.path.exists("live_wallet.json"):
        try:
            with open("live_wallet.json", "r") as f:
                wallet_data = json.load(f)
        except Exception: pass
        
    net_profit = 0.0
    if os.path.exists("ml_training_data.json"):
        try:
            with open("ml_training_data.json", "r") as f:
                hist = json.load(f)
                for t in hist:
                    t_mode = str(t.get("trade_mode", "PAPER")).strip().strip('"\'').upper()
                    is_t_live = t_mode in ["LIVE", "TRUE"]
                    if (trade_mode == "LIVE" and is_t_live) or (trade_mode == "PAPER" and not is_t_live):
                        net_profit += trade_pnl(t)
        except Exception: pass

    max_trades = int(os.getenv("MAX_CONCURRENT_TRADES", "10"))

    if trade_mode == "PAPER":
        # READ-ONLY. This endpoint used to recompute the balance and write it back to
        # paper_wallet.json on every poll (every 5s), while trade_brain.record_trade was
        # independently debiting the same file with no shared lock. The dashboard could
        # restore capital the engine had just committed to an open position, letting the
        # bot allocate the same money twice — and the headline balance shown to the user
        # was written by the dashboard, not by the trade engine.
        #
        # trade_brain is now the single writer. Report what it says.
        initial_bal = wallet_data.get("initial", 100.0)
        available_cash = wallet_data.get("balance", 0.0)
    else:
        # LIVE Mode: Query real on-chain SOL balance via direct JSON-RPC
        available_cash = 0.0
        try:
            wallet_pk = os.getenv("WALLET_PRIVATE_KEY", "").strip()
            pubkey_str = os.getenv("WALLET_ADDRESS", "").strip()
            rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
            
            if not pubkey_str and wallet_pk:
                try:
                    from solders.keypair import Keypair
                    pubkey_str = str(Keypair.from_base58_string(wallet_pk).pubkey())
                except Exception:
                    pass
            
            if pubkey_str:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getBalance",
                    "params": [pubkey_str]
                }
                async with httpx.AsyncClient() as client:
                    resp = await client.post(rpc_url, json=payload, timeout=5.0)
                    if resp.status_code == 200:
                        result = resp.json().get("result", {})
                        lamports = result.get("value", 0)
                        sol_bal = lamports / 1_000_000_000.0
                        
                        sol_price = await get_sol_price_usd()
                        if sol_price <= 0: sol_price = 150.0
                        
                        available_cash = sol_bal * sol_price
        except Exception as e:
            logging.error(f"Live balance fetch error in dashboard API: {e}")
            available_cash = wallet_data.get("balance", 0.0)
                    
    total_value = available_cash
    unrealized_profit = 0.0
    for v in active_trades:
        size = float(v.get("trade_size") or v.get("trade_size_usd") or 0.0)
        pnl = float(v.get("profit_usd") or 0.0)
        entry_price = float(v.get("entry_price") or v.get("entry_usd_price") or 0.0)
        current_price = float(v.get("current_price") or entry_price)
        if pnl == 0.0 and entry_price > 0:
            pnl = ((current_price - entry_price) / entry_price) * size
        unrealized_profit += pnl
        total_value += (size + pnl)
        
    scanner_online = get_scanner_status()
    return {
        "mode": trade_mode,
        "bot_status": "ONLINE" if scanner_online else "OFFLINE",
        "scanner_active": scanner_online,
        "total_value": round(total_value, 2),
        "available_cash": round(available_cash, 2),
        "net_profit": round(net_profit, 2),
        "unrealized_profit": round(unrealized_profit, 2),
        "active_trades_count": len(active_trades),
        "max_trades": max_trades,
        "trades": active_trades
    }


@app.get("/api/history")
async def get_history(mode: str = "ALL", _: bool = Depends(require_auth)):
    trades = []
    if os.path.exists("ml_training_data.json"):
        try:
            with open("ml_training_data.json", "r") as f:
                trades = json.load(f)
        except: pass
        
    filtered = []
    total_pnl = 0.0
    wins = 0
    actioned_count = 0
    for t in trades:
        t_raw = str(t.get("trade_mode", "UNKNOWN")).strip().strip('"\'').upper()
        t_mode = "LIVE" if t_raw in ["LIVE", "TRUE"] else ("PAPER" if t_raw == "PAPER" else t_raw)
        if mode != "ALL" and t_mode != mode:
            continue
        filtered.append(t)
        pnl = trade_pnl(t)
        total_pnl += pnl
        
        if t.get("exit_reason", "") != "TIMEOUT":
            actioned_count += 1
            if pnl > 0:
                wins += 1
            
    filtered.reverse()
    win_rate = (wins / actioned_count * 100) if actioned_count > 0 else 0.0
    
    return {
        "total_trades": len(filtered),
        "win_rate": win_rate,
        "realized_profit": total_pnl,
        "avg_profit": (total_pnl / len(filtered)) if filtered else 0.0,
        "trades": filtered
    }

class WhitelistAction(BaseModel):
    wallet: str
    status: str = "WHITELIST"


@app.get("/api/whales")
async def get_whales(mode: str = "ALL", _: bool = Depends(require_auth)):
    whale_db = {}
    wl_set = whale_manager.get_whitelist_set()
    for w in wl_set:
        whale_db[w] = {"status": "WHITELIST"}
        
    disc_data = whale_manager.get_discovery_data()
    for k, v in disc_data.items():
        if k not in whale_db:
            whale_db[k] = v
        
    trades = []
    if os.path.exists("ml_training_data.json"):
        try:
            with open("ml_training_data.json", "r") as f:
                trades = json.load(f)
        except Exception: pass
        
    stats = {}
    for w in whale_db.keys():
        stats[w] = {"status": whale_db[w].get("status", "NEUTRAL"), "total": 0, "wins": 0, "profit": 0.0, "modes": [], "actioned": 0}
        
    for t in trades:
        t_raw = str(t.get("trade_mode", "UNKNOWN")).strip().strip('"\'').upper()
        t_mode = "LIVE" if t_raw in ["LIVE", "TRUE"] else ("PAPER" if t_raw == "PAPER" else t_raw)
        if mode != "ALL" and t_mode != mode:
            continue
        w = t.get("whale_wallet")
        if w:
            if w not in stats:
                stats[w] = {"status": "UNKNOWN", "total": 0, "wins": 0, "profit": 0.0, "modes": [], "actioned": 0}
            stats[w]["total"] += 1
            if t.get("exit_reason", "") != "TIMEOUT":
                stats[w]["actioned"] += 1
            
            pnl = trade_pnl(t)
            stats[w]["profit"] += pnl
            if t_mode not in stats[w]["modes"]:
                stats[w]["modes"].append(t_mode)
            if pnl > 0:
                stats[w]["wins"] += 1

    result = []
    for w, s in stats.items():
        if s["status"] != "WHITELIST" and s["total"] == 0:
            continue
        result.append({
            "wallet": w,
            "status": s["status"],
            "total": s["total"],
            "win_rate": (s["wins"]/s["actioned"]*100) if s["actioned"] > 0 else 0,
            "profit": s["profit"],
            "modes": ", ".join(sorted(s["modes"])) if s["modes"] else "STANDBY"
        })
        
    result.sort(key=lambda x: (x["status"] == "WHITELIST", x["profit"]), reverse=True)
    
    return {
        "total_db": whale_manager.get_total_whales(),
        "active_whitelists": len(wl_set),
        "whales": result
    }


@app.get("/api/whales/neutral")
async def get_neutral_whales(filter: str = "all", sort_by: str = "pnl", _: bool = Depends(require_auth)):
    """
    Evaluates and returns neutral whales scored by the AI Whale Scorer gauntlet,
    highlighting high-conviction Alpha candidates with an option to whitelist.
    """
    ranked_path = os.path.join(BASE_DIR, "data", "neutral_whales_ranked.json")
    ranked = []
    if os.path.exists(ranked_path):
        try:
            with open(ranked_path, "r", encoding="utf-8") as f:
                ranked = json.load(f)
        except Exception as e:
            logging.warning(f"Error loading {ranked_path}: {e}")

    disc_data = whale_manager.get_discovery_data()
    total_neutral = sum(1 for v in disc_data.values() if v.get("status", "NEUTRAL").upper() == "NEUTRAL")
    wl_set = whale_manager.get_whitelist_set()
    candidates = []

    for r in ranked:
        w = r.get("wallet", "")
        if not w:
            continue
        s7 = r.get("7d", {})
        s1 = r.get("1d", {})
        s30 = r.get("30d", {})
        wr = float(s7.get("winrate", 0.0))
        profit = float(s7.get("realized", 0.0))
        trades = int(s7.get("trades", 0))
        tags = r.get("tags", [])
        native_bal = float(s7.get("native_balance", 0.0))

        data = {
            "winrate_7d": wr,
            "trades_7d": trades,
            "profit_7d": profit,
            "tags": tags,
        }

        score_res = ai_whale_scorer.score_wallet(w, data)
        is_wl = w in wl_set

        # Filter option handling
        if filter == "alpha" and (score_res.score < 70 or score_res.status == "BLACKLIST"):
            continue
        elif filter == "consistent" and (wr < 90.0 or trades < 5):
            continue
        elif filter == "high_pnl" and profit < 500.0:
            continue

        candidates.append({
            "wallet": w,
            "ai_score": score_res.score,
            "ai_status": score_res.status,
            "style": score_res.style,
            "summary": score_res.summary,
            "red_flags": score_res.red_flags,
            "winrate_7d": wr,
            "profit_7d": profit,
            "trades_7d": trades,
            "buy_7d": int(s7.get("buy", 0) or 0),
            "sell_7d": int(s7.get("sell", 0) or 0),
            "profit_1d": float(s1.get("realized", 0.0)),
            "trades_1d": int(s1.get("trades", 0) or 0),
            "profit_30d": float(s30.get("realized", 0.0)),
            "trades_30d": int(s30.get("trades", 0) or 0),
            "native_balance": native_bal,
            "tags": tags,
            "is_whitelisted": is_wl,
        })

    # Sort candidates
    if sort_by == "winrate":
        candidates.sort(key=lambda x: (x["winrate_7d"], x["profit_7d"]), reverse=True)
    elif sort_by == "score":
        candidates.sort(key=lambda x: (x["ai_score"], x["profit_7d"]), reverse=True)
    elif sort_by == "trades":
        candidates.sort(key=lambda x: (x["trades_7d"], x["profit_7d"]), reverse=True)
    else:  # default pnl
        candidates.sort(key=lambda x: (x["profit_7d"], x["ai_score"]), reverse=True)

    return {
        "total_neutral_db": total_neutral,
        "ranked_count": len(candidates),
        "active_whitelists": len(wl_set),
        "candidates": candidates,
    }


@app.post("/api/whales/whitelist")
async def update_whale_whitelist(action: WhitelistAction, _: bool = Depends(require_auth)):
    wallet = action.wallet.strip()
    status = action.status.strip().upper()
    if status not in ["WHITELIST", "NEUTRAL", "BLACKLIST"]:
        raise HTTPException(status_code=400, detail="Status must be WHITELIST, NEUTRAL, or BLACKLIST")

    if len(wallet) < 32 or len(wallet) > 44:
        raise HTTPException(status_code=400, detail="Invalid Solana wallet address format")

    ok = whale_manager.set_whale_status(wallet, status)
    if not ok:
        raise HTTPException(status_code=500, detail=f"Failed to update whale status to {status}")

    return {
        "success": True,
        "wallet": wallet,
        "status": status,
        "active_whitelists": len(whale_manager.get_whitelist())
    }

@app.get("/api/logs")
async def get_logs(_: bool = Depends(require_auth)):
    env_content = []
    if os.path.exists(".env"):
        with open(".env", "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"): continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k_upper = k.upper()
                    
                    # Completely hide/exclude sensitive fields and URLs from the Web UI list
                    sensitive_keywords = ["KEY", "TOKEN", "SECRET", "PASSWORD", "ID", "PRIVATE", "AUTH", "URL", "RPC", "WSS"]
                    if any(s in k_upper for s in sensitive_keywords) or v.startswith("http") or v.startswith("wss") or v.startswith("ws"):
                        continue
                        
                    env_content.append({"key": k, "val": v})
                    
    log_text = ""
    debug_log = "bot_debug.log"
    if os.path.exists(debug_log):
        log_text = await asyncio.to_thread(_tail_redacted, debug_log, 150)

    return {
        "env": env_content,
        "logs": log_text
    }


# Credential patterns that have historically appeared in bot_debug.log. Applied to anything
# this server hands out, so a log written before the scanner.py redaction fix can't leak.
_SECRET_PATTERNS = [
    re.compile(r"(api[-_]?key=)[A-Za-z0-9\-_]+", re.IGNORECASE),
    re.compile(r"(/v2/)[A-Za-z0-9\-_]{16,}"),                     # Alchemy path keys
    re.compile(r"\b(gmgn_)[A-Za-z0-9]{16,}\b", re.IGNORECASE),
    re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{30,}\b"),            # Telegram bot tokens
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
]


def _redact_secrets(text: str) -> str:
    """Strips known credential shapes out of log text before it leaves the process."""
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: m.group(1) + "[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Log reads run OFF the event loop.
#
# main.py starts this uvicorn app with asyncio.create_task() in the SAME loop as the
# scanner, the price-poll monitors and the discovery cycle. A synchronous read+regex over
# the debug log therefore stalls live trading for its full duration:
#
#     0.5 MB ->  43 ms     1.7 MB -> 102 ms     5 MB -> 291 ms     10 MB -> 519 ms
#
# The previous run's log hit 1.7 MB with 5 MB rotations, so a single dashboard log view
# could freeze position monitoring for a third of a second. Both helpers below are pure
# blocking functions invoked via asyncio.to_thread.
# ─────────────────────────────────────────────────────────────────────────────

def _tail_redacted(path: str, lines: int) -> str:
    """Last `lines` lines of a file, redacted. Reads from the end — never loads the whole file."""
    try:
        chunk_size = 8192
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            buf = b""
            while end > 0 and buf.count(b"\n") <= lines:
                step = min(chunk_size, end)
                end -= step
                f.seek(end)
                buf = f.read(step) + buf
        # Normalise newlines: this reads in binary mode (to seek from the end), so unlike a
        # text-mode read it would otherwise hand CRLF through to the UI on a Windows-written log.
        text = buf.decode("utf-8", errors="replace").replace("\r\n", "\n")
        return _redact_secrets("".join(text.splitlines(keepends=True)[-lines:]))
    except Exception as e:
        logging.error(f"Failed to tail {path}: {e}")
        return ""


def _read_redacted(path: str) -> str:
    """Whole file, redacted. Only for the explicit download endpoint."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return _redact_secrets(f.read())


@app.get("/api/settings")
async def get_all_settings(_: bool = Depends(require_auth)):
    """Returns all hot-reloadable settings and their specifications."""
    return {
        "settings": settings_manager.get_all(),
        "spec": settings_manager.SPEC
    }

@app.post("/api/settings")
async def update_setting(request: Request, _: bool = Depends(require_auth)):
    """Updates a hot-reloadable configuration setting."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
        
    key = data.get("key")
    val = data.get("value")
    if not key:
        raise HTTPException(status_code=400, detail="Missing 'key' in payload")
        
    success, msg = settings_manager.update(str(key), val, source="web_dashboard")
    if not success:
        return JSONResponse({"success": False, "message": msg}, status_code=400)
    
    return {
        "success": True,
        "message": msg,
        "key": key,
        "current_value": settings_manager.get(key)
    }

@app.get("/api/telemetry")
async def get_telemetry(_: bool = Depends(require_auth)):
    """Returns rate budget statistics and transaction decode telemetry."""
    try:
        from connection_pool import get_stats as get_pool_stats
        pool_stats = get_pool_stats()
    except Exception:
        pool_stats = {}

    try:
        from decoder import get_decode_stats
        decode_stats = get_decode_stats()
    except Exception:
        decode_stats = {}

    return {
        "budgets": pool_stats,
        "decoder": decode_stats
    }

@app.get("/api/log-info")
async def get_log_info(_: bool = Depends(require_auth)):
    """Returns file metadata for bot_debug.log without streaming the full file content."""
    log_path = os.path.join(BASE_DIR, "bot_debug.log")
    if os.path.exists(log_path):
        stat = os.stat(log_path)
        size_mb = round(stat.st_size / (1024 * 1024), 2)
        mod_time = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return {
            "exists": True,
            "filename": "bot_debug.log",
            "size_mb": size_mb,
            "bytes": stat.st_size,
            "modified": mod_time
        }
    return {
        "exists": False,
        "filename": "bot_debug.log",
        "size_mb": 0.0,
        "bytes": 0,
        "modified": "Never"
    }

@app.get("/api/download/{filename}")
async def download_log(filename: str, _: bool = Depends(require_auth)):
    """
    Serves the debug log with credentials stripped.

    This endpoint was previously unauthenticated and served the raw file, which contained
    the full Helius WSS URL (API key included) 17 times. It is now behind require_auth AND
    redacted on the way out — defence in depth, because rotating a key that has already been
    downloaded doesn't help anyone.
    """
    if filename != "bot_debug.log":
        return JSONResponse({"error": "Invalid file"}, status_code=400)

    path = os.path.join(BASE_DIR, "bot_debug.log")
    if not os.path.exists(path):
        return JSONResponse({"error": "File not found"}, status_code=404)

    redacted = await asyncio.to_thread(_read_redacted, path)

    return PlainTextResponse(
        redacted,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

if __name__ == "__main__":
    web_port = int(os.getenv("WEB_PORT", "8101"))
    web_host = os.getenv("WEB_HOST", "0.0.0.0")
    uvicorn.run("web_server:app", host=web_host, port=web_port, reload=False)



