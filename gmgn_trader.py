"""
=========================================================
gmgn_trader.py (GMGN Swap & Route Execution Engine)
=========================================================

Short Brief:
This module replaces the legacy Jupiter swap engine. It handles execution for all LIVE 
copy-trades using the GMGN Route API. The defining feature of this script is that it 
executes buys with native Take-Profit and Stop-Loss condition orders seamlessly attached 
at execution time. This shifts the burden of monitoring PnL from the bot's local latency 
to GMGN's servers, allowing for flawless, slippage-free exits even if the bot is restarted.

Key Features:
- Replaces jupiter_api.py for live trading execution.
- Private key NEVER leaves the server (signed locally via Ed25519 in gmgn-cli).
- Supports panic/emergency market sell overriding.

Prerequisites (on the Linux server):
  - gmgn-cli >= 1.5.2 installed: npm install -g gmgn-cli
  - GMGN_API_KEY and GMGN_PRIVATE_KEY set in ~/.config/gmgn/.env
  - gmgn-cli config --check must exit 0
"""

import asyncio
import json
import logging
import os
from typing import Optional

log = logging.getLogger("GMGNTrader")

SOL_MINT = "So11111111111111111111111111111111111111112"

# CLI subprocess timeout
_CLI_TIMEOUT = 45.0

# Fee settings — configurable in settings_manager or .env
import settings_manager

_PRIORITY_FEE = os.getenv("GMGN_PRIORITY_FEE", "0.00001")
_DEFAULT_TIP_FEE = os.getenv("GMGN_TIP_FEE", "0.00001")

def _get_tip_fee_str() -> str:
    """Dynamically resolves tip fee in SOL from settings_manager."""
    tip_sol = settings_manager.get("JITO_TIP_SOL")
    if tip_sol is not None:
        return f"{float(tip_sol):.6f}"
    return _DEFAULT_TIP_FEE

def _is_anti_mev_enabled() -> bool:
    """Checks if Jito / anti-MEV protection is enabled."""
    return bool(settings_manager.get("JITO_ENABLED", True))


async def _run_gmgn_cli(*args) -> dict:
    """
    Run a gmgn-cli command and return parsed JSON output.
    On any error returns {"_error": str, "success": False}.

    NOTE: We explicitly invoke `node <script>` instead of running the gmgn-cli
    file directly. The GMGN NPM package is published with Windows-style line
    endings (\\r\\n) in its shebang line, which causes Linux to look for
    "node\\r" instead of "node". By calling node ourselves we bypass the
    shebang entirely, making this immune to future npm reinstalls.
    """
    import shutil
    cli_bin = shutil.which("gmgn-cli") or "gmgn-cli"
    node_bin = shutil.which("node") or "node"
    
    # Primary: node + cli_bin
    cmd = [node_bin, cli_bin] + [str(a) for a in args]
    log.debug(f"gmgn-cli cmd: {' '.join(cmd)}")

    raw_output = ""
    try:
        env = os.environ.copy()
        if os.getenv("GMGN_API_KEY"):
            env["GMGN_API_KEY"] = os.getenv("GMGN_API_KEY")
        if os.getenv("WALLET_PRIVATE_KEY"):
            env["GMGN_PRIVATE_KEY"] = os.getenv("WALLET_PRIVATE_KEY")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_CLI_TIMEOUT)
            raw_output = stdout.decode().strip()
            err_output = stderr.decode().strip()
        except Exception:
            # Fallback: run gmgn-cli directly
            direct_cmd = [cli_bin] + [str(a) for a in args]
            proc = await asyncio.create_subprocess_exec(
                *direct_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_CLI_TIMEOUT)
            raw_output = stdout.decode().strip()
            err_output = stderr.decode().strip()

        if proc.returncode != 0:
            log.error(f"gmgn-cli exit {proc.returncode}. stderr: {err_output}. stdout: {raw_output[:300]}")
            return {"_error": err_output or raw_output, "success": False}

        if not raw_output:
            log.warning(f"gmgn-cli returned empty output for: {' '.join(args)}")
            return {"_error": "empty_response", "success": False}


        return json.loads(raw_output)

    except asyncio.TimeoutError:
        log.error(f"gmgn-cli timed out after {_CLI_TIMEOUT}s")
        return {"_error": "cli_timeout", "success": False}
    except json.JSONDecodeError as e:
        log.error(f"gmgn-cli JSON parse failed: {e}. Raw: {raw_output[:400]}")
        return {"_error": f"json_parse: {e}", "success": False}
    except FileNotFoundError:
        log.error("gmgn-cli not found. Run: npm install -g gmgn-cli")
        return {"_error": "gmgn_cli_not_installed", "success": False}
    except Exception as e:
        log.error(f"gmgn-cli unexpected exception: {e}")
        return {"_error": str(e), "success": False}


async def execute_gmgn_buy(
    wallet_address: str,
    token_address: str,
    sol_lamports: int,
    take_profit_pct: float,
    stop_loss_pct: float,
) -> dict:
    """
    Execute a buy via gmgn-cli with Take Profit + Stop Loss condition orders attached.

    GMGN servers monitor the position after this call. The strategy fires automatically
    when TP or SL is hit -- the bot does not need to poll price itself.

    Returns:
        {
            "success":           bool,
            "order_id":          str,
            "strategy_order_id": str | None,   # None if best-effort creation failed
            "hash":              str,
            "status":            str,
        }
    """
    # GMGN price_scale convention:
    #   profit_stop: price_scale = gain %  (e.g. "20" means sell at +20%)
    #   loss_stop:   price_scale = drop %  (e.g. "10" means sell when down 10%)
    condition_orders = [
        {"order_type": "profit_stop", "side": "sell",
         "price_scale": str(int(take_profit_pct)), "sell_ratio": "100"},
        {"order_type": "loss_stop",   "side": "sell",
         "price_scale": str(int(abs(stop_loss_pct))), "sell_ratio": "100"},
    ]

    log.info(
        f"GMGN BUY: {token_address[:8]}... | "
        f"{sol_lamports / 1e9:.4f} SOL | "
        f"TP +{int(take_profit_pct)}% | SL -{int(abs(stop_loss_pct))}%"
    )

    buy_args = [
        "swap",
        "--chain", "sol",
        "--from", wallet_address,
        "--input-token", SOL_MINT,
        "--output-token", token_address,
        "--amount", str(sol_lamports),
        "--auto-slippage",
    ]
    if _is_anti_mev_enabled():
        buy_args.append("--anti-mev")
    buy_args.extend([
        "--priority-fee", _PRIORITY_FEE,
        "--tip-fee", _get_tip_fee_str(),
        "--condition-orders", json.dumps(condition_orders),
    ])
    result = await _run_gmgn_cli(*buy_args)

    if result.get("_error"):
        log.error(f"GMGN buy failed: {result['_error']}")
        return {"success": False, "_error": result["_error"]}

    order_id = result.get("order_id")
    if not order_id:
        log.error(f"GMGN buy response missing order_id. Response: {result}")
        return {"success": False, "_error": "missing_order_id"}

    strategy_order_id = result.get("strategy_order_id")
    if not strategy_order_id:
        log.warning(
            f"GMGN buy {order_id}: strategy_order_id missing (best-effort creation failed). "
            f"Will fall back to price-polling monitor for this trade."
        )

    log.info(
        f"GMGN BUY submitted | order_id={order_id} | "
        f"strategy_order_id={strategy_order_id} | status={result.get('status')}"
    )

    return {
        "success": True,
        "order_id": order_id,
        "strategy_order_id": strategy_order_id,
        "hash": result.get("hash", ""),
        "status": result.get("status", "pending"),
    }


async def wait_for_order_confirmed(order_id: str, max_wait_seconds: int = 90) -> dict:
    """
    Poll gmgn-cli order get until the order reaches a terminal state.

    Returns {"confirmed": bool, "status": str, "report": dict | None, ...}
    """
    deadline = asyncio.get_event_loop().time() + max_wait_seconds

    while asyncio.get_event_loop().time() < deadline:
        result = await _run_gmgn_cli("order", "get", "--chain", "sol", "--order-id", order_id)

        if result.get("_error"):
            log.warning(f"order get error ({order_id}): {result['_error']}. Retrying...")
            await asyncio.sleep(5.0)
            continue

        status = result.get("status", "")
        log.debug(f"Order {order_id} status: {status}")

        if status == "confirmed":
            log.info(f"GMGN order {order_id} CONFIRMED.")
            return {**result, "confirmed": True}

        if status in ("failed", "expired"):
            log.error(f"GMGN order {order_id} terminal state: {status}")
            return {**result, "confirmed": False}

        await asyncio.sleep(4.0)

    log.error(f"GMGN order {order_id} not confirmed after {max_wait_seconds}s.")
    return {"confirmed": False, "status": "poll_timeout"}


async def get_strategy_status(strategy_order_id: str, token_address: str) -> Optional[dict]:
    """
    Find a specific strategy (TP/SL) order by its ID.

    Searches open orders first, then history (last 20).
    Returns the full strategy order dict, or None if not found.

    Key fields in returned dict:
      - status:                        "open" | "closed"
      - close_price:                   str (token price at exit, empty when open)
      - order_statistic.usdt_profit:   str (realized P&L USD, only when closed)
      - reason_code:                   str (e.g. "profit_stop", "loss_stop")
    """
    for list_type in [None, "history"]:
        args = [
            "order", "strategy", "list",
            "--chain", "sol",
            "--group-tag", "STMix",
            "--base-token", token_address,
        ]
        if list_type:
            args += ["--type", list_type, "--limit", "20"]

        result = await _run_gmgn_cli(*args)

        if result.get("_error"):
            log.warning(f"strategy list error: {result['_error']}")
            return None

        for order in result.get("list", []):
            if order.get("order_id") == strategy_order_id:
                return order

    return None


async def cancel_strategy_order(wallet_address: str, strategy_order_id: str) -> bool:
    """
    Cancel an open TP/SL strategy order before a manual sell.
    Returns True on success.
    """
    log.info(f"Cancelling GMGN strategy {strategy_order_id}...")
    result = await _run_gmgn_cli(
        "order", "strategy", "cancel",
        "--chain", "sol",
        "--from", wallet_address,
        "--order-id", strategy_order_id,
    )
    if result.get("_error"):
        log.error(f"Failed to cancel strategy {strategy_order_id}: {result['_error']}")
        return False
    log.info(f"Strategy {strategy_order_id} cancelled.")
    return True


async def execute_gmgn_sell_all(wallet_address: str, token_address: str) -> dict:
    """
    Force-sell 100% of a token via gmgn-cli.

    Used for:
      - Timeout exits when TP/SL has not fired yet
      - Trades where strategy_order_id was None (condition order creation failed)

    Returns {"success": bool, "confirmed": bool, "report": dict | None, ...}
    """
    log.info(f"GMGN SELL ALL: {token_address[:8]}... (100% of position)")

    sell_args = [
        "swap",
        "--chain", "sol",
        "--from", wallet_address,
        "--input-token", token_address,
        "--output-token", SOL_MINT,
        "--percent", "100",
        "--auto-slippage",
    ]
    if _is_anti_mev_enabled():
        sell_args.append("--anti-mev")
    sell_args.extend([
        "--priority-fee", _PRIORITY_FEE,
        "--tip-fee", _get_tip_fee_str(),
    ])
    result = await _run_gmgn_cli(*sell_args)

    if result.get("_error"):
        log.error(f"GMGN sell-all failed for {token_address[:8]}: {result['_error']}")
        return {"success": False, "confirmed": False, "_error": result["_error"]}

    order_id = result.get("order_id")
    if not order_id:
        log.error(f"GMGN sell-all missing order_id. Response: {result}")
        return {"success": False, "confirmed": False}

    confirmed = await wait_for_order_confirmed(order_id, max_wait_seconds=90)
    return {**confirmed, "success": confirmed.get("confirmed", False)}


