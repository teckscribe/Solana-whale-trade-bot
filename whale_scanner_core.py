"""
whale_scanner_core.py
=========================================================
Core engine for high-performance GMGN whale scanning,
2-tier filtering, persistent disk caching, and alpha ranking.
=========================================================
"""

import sys
import os
import json
import shutil
import subprocess
import time
import re
import logging
from typing import Optional, Dict, List, Tuple
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("WhaleScannerCore")

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

DATA_DIR = os.path.join(_BOT_DIR, "data")
CACHE_FILE = os.path.join(DATA_DIR, "whale_scan_cache.json")
RANKED_FILE = os.path.join(DATA_DIR, "neutral_whales_ranked.json")
CACHE_TTL = 43200  # 12 hours in seconds

GMGN_API_KEY = os.getenv("GMGN_API_KEY", "")
GMGN_BANNED_TAGS = [t.strip().lower() for t in os.getenv("GMGN_BANNED_TAGS", "sniper, mev, padre, banana, trojan, maestro").split(",") if t.strip()]

# ─── Cache Management ─────────────────────────────────────────────────────────

_mem_cache: Optional[Dict[str, dict]] = None


def load_cache() -> Dict[str, dict]:
    """Load persistent scan cache from disk with fallback."""
    global _mem_cache
    if _mem_cache is not None:
        return _mem_cache
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                _mem_cache = json.load(f)
                return _mem_cache
        except Exception as e:
            log.warning(f"Failed to read cache file {CACHE_FILE}: {e}")
    _mem_cache = {}
    return _mem_cache


def save_cache(cache: Dict[str, dict]):
    """Save persistent scan cache to disk atomically."""
    global _mem_cache
    _mem_cache = cache
    os.makedirs(DATA_DIR, exist_ok=True)
    temp_path = f"{CACHE_FILE}.tmp_{os.getpid()}"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
        os.replace(temp_path, CACHE_FILE)
    except Exception as e:
        log.error(f"Failed to save scan cache: {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def clear_cache():
    """Clear memory and on-disk scan cache."""
    global _mem_cache
    _mem_cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            os.remove(CACHE_FILE)
        except OSError:
            pass


# ─── GMGN CLI Execution ───────────────────────────────────────────────────────

_gmgn_ban_until: float = 0.0
_last_gmgn_call_at: float = 0.0
MIN_GMGN_INTERVAL_SEC: float = 1.5


def is_gmgn_rate_limited() -> bool:
    """Return True if GMGN CLI is currently in a 429 cooldown window."""
    return time.time() < _gmgn_ban_until


def run_cmd(cmd: str) -> str:
    """Run shell command with low priority, rate pacing, 429 circuit breaker, and timeout protection."""
    global _gmgn_ban_until, _last_gmgn_call_at
    now = time.time()
    if now < _gmgn_ban_until:
        return ""
    elapsed = now - _last_gmgn_call_at
    if _last_gmgn_call_at > 0 and elapsed < MIN_GMGN_INTERVAL_SEC:
        time.sleep(MIN_GMGN_INTERVAL_SEC - elapsed)

    env = os.environ.copy()
    if GMGN_API_KEY:
        env["GMGN_API_KEY"] = GMGN_API_KEY
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.IDLE_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW

    try:
        _last_gmgn_call_at = time.time()
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=creationflags,
            timeout=25.0
        )
        if result.returncode != 0 or not result.stdout:
            stderr = result.stderr or ""
            if "RATE_LIMIT_BANNED" in stderr or "HTTP 429" in stderr:
                m = re.search(r"~(\d+)s remaining", stderr)
                wait_sec = float(m.group(1)) + 5.0 if m else 180.0
                _gmgn_ban_until = time.time() + wait_sec
                log.warning(f"GMGN CLI rate limit reached; pausing GMGN CLI queries for {wait_sec:.0f}s to prevent ban extension.")
            return ""
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        log.warning(f"Command timed out: {cmd[:60]}...")
        return ""
    except Exception as e:
        log.error(f"Error executing command: {e}")
        return ""


def parse_gmgn_stats_output(raw_output: str) -> dict:
    """Parse raw JSON string from gmgn-cli portfolio stats."""
    if not raw_output:
        return {}
    match = re.search(r'\{.*\}', raw_output.replace('\n', ''))
    if not match:
        return {}
    try:
        s = json.loads(match.group(0))
        pnl_stat = s.get("pnl_stat", {})
        winrate_raw = pnl_stat.get("winrate", 0)
        winrate = float(winrate_raw) * 100.0 if winrate_raw is not None else 0.0

        buy_count = int(s.get("buy", 0) or 0)
        sell_count = int(s.get("sell", 0) or 0)
        total_trades = buy_count + sell_count

        try:
            realized_profit = float(s.get("realized_profit", 0) or 0.0)
        except (ValueError, TypeError):
            realized_profit = 0.0

        try:
            native_balance = float(s.get("native_balance", 0) or 0.0)
        except (ValueError, TypeError):
            native_balance = 0.0

        try:
            roi_pct = float(s.get("realized_profit_pnl", 0) or 0.0) * 100.0
        except (ValueError, TypeError):
            roi_pct = 0.0

        try:
            bought_cost = float(s.get("bought_cost", 0) or 0.0)
        except (ValueError, TypeError):
            bought_cost = 0.0

        try:
            bought_fee = float(s.get("bought_fee", 0) or 0.0)
        except (ValueError, TypeError):
            bought_fee = 0.0

        fee_to_profit_pct = (bought_fee / realized_profit * 100.0) if realized_profit > 0 else 0.0
        token_num = int(pnl_stat.get("token_num", 0) or 0)
        runners_2x_plus = int(pnl_stat.get("pnl_2x_5x_num", 0) or 0) + int(pnl_stat.get("pnl_gt_5x_num", 0) or 0)
        try:
            avg_holding_seconds = float(pnl_stat.get("avg_holding_period", 0) or 0.0)
        except (ValueError, TypeError):
            avg_holding_seconds = 0.0

        tags = s.get("common", {}).get("tags", []) or []

        return {
            "winrate": winrate,
            "trades": total_trades,
            "buy": buy_count,
            "sell": sell_count,
            "realized": realized_profit,
            "native_balance": native_balance,
            "roi_pct": round(roi_pct, 2),
            "bought_cost": round(bought_cost, 2),
            "bought_fee": round(bought_fee, 2),
            "fee_to_profit_pct": round(fee_to_profit_pct, 2),
            "token_num": token_num,
            "runners_2x_plus": runners_2x_plus,
            "avg_holding_seconds": round(avg_holding_seconds, 1),
            "tags": tags
        }
    except Exception as e:
        log.debug(f"JSON parsing error for GMGN output: {e}")
        return {}


def query_gmgn_stats(wallet: str, period: str = "7d") -> dict:
    """Query GMGN CLI for specific wallet and period."""
    gmgn_bin = shutil.which("gmgn-cli.cmd" if os.name == "nt" else "gmgn-cli")
    nice_prefix = "" if os.name == "nt" else "nice -n 19 "
    if gmgn_bin:
        cmd = f'{nice_prefix}"{gmgn_bin}" portfolio stats --chain sol --period {period} --wallet {wallet} --raw'
    else:
        npx_prefix = "npx.cmd" if os.name == "nt" else "/usr/bin/npx"
        cmd = f"{nice_prefix}{npx_prefix} -y gmgn-cli portfolio stats --chain sol --period {period} --wallet {wallet} --raw"
    out = run_cmd(cmd)
    return parse_gmgn_stats_output(out)


# ─── 2-Tier Fast Evaluation Engine ────────────────────────────────────────────

def fetch_wallet_profile(
    wallet: str,
    use_cache: bool = True,
    deep_scan: bool = True,
    rate_sleep: float = 1.0
) -> dict:
    """
    Fetch 1d, 7d, 30d profile with 2-tier fast filtering:
    - Tier 1: Pull 7d stats first.
    - If 7d has 0 trades or negative profit and 0% winrate, skip 1d and 30d!
    - Tier 2: If qualifying, fetch 1d and 30d for comprehensive analysis.
    """
    now = time.time()
    cache = load_cache() if use_cache else {}

    if use_cache and wallet in cache:
        cached_entry = cache[wallet]
        if (now - cached_entry.get("cached_at", 0) < CACHE_TTL) and cached_entry.get("7d"):
            return cached_entry

    profile = {
        "wallet": wallet,
        "cached_at": now,
        "1d": {},
        "7d": {},
        "30d": {},
        "native_balance": 0.0,
        "tags": []
    }

    # --- Tier 1: 7-day Anchor Evaluation ---
    s7d = query_gmgn_stats(wallet, period="7d")
    if not s7d:
        # Do not poison cache with empty CLI timeouts/rate-limit errors;
        # fall back to prior non-empty cached profile if available.
        if use_cache and wallet in cache and cache[wallet].get("7d"):
            return cache[wallet]
        return profile

    profile["7d"] = s7d
    profile["native_balance"] = s7d.get("native_balance", 0.0)
    profile["tags"] = s7d.get("tags", [])

    time.sleep(rate_sleep)

    trades_7d = s7d.get("trades", 0)
    winrate_7d = s7d.get("winrate", 0.0)
    realized_7d = s7d.get("realized", 0.0)

    # Fast-exit condition: Completely inactive or strictly negative PnL with 0 wins
    qualifies_for_tier2 = (trades_7d > 0) and (winrate_7d >= 40.0 or realized_7d > 0.0)

    if deep_scan and qualifies_for_tier2:
        # --- Tier 2: 1d and 30d Detailed Assessment ---
        s1d = query_gmgn_stats(wallet, period="1d")
        profile["1d"] = s1d
        time.sleep(rate_sleep)

        s30d = query_gmgn_stats(wallet, period="30d")
        profile["30d"] = s30d
        time.sleep(rate_sleep)
    else:
        # Use 7d values as fallback to avoid empty fields
        profile["1d"] = {"winrate": 0.0, "trades": 0, "realized": 0.0}
        profile["30d"] = s7d

    if use_cache and s7d:
        cache[wallet] = profile
        save_cache(cache)

    return profile


# ─── Candidate Discovery & Sorting ───────────────────────────────────────────

def get_candidates(
    status_target: str = "NEUTRAL",
    recent_days: Optional[float] = None,
    limit: Optional[int] = None
) -> List[Tuple[str, dict]]:
    """
    Get wallets with status matching status_target, sorted chronologically
    by last_active descending (freshest wallets first).
    """
    import whale_manager
    if status_target == "WHITELIST":
        db = whale_manager._load_db(whale_manager.WHITELIST_FILE)
    elif status_target == "BLACKLIST":
        db = {
            w: info for w, info in whale_manager._load_db(whale_manager.DISCOVERY_FILE).items()
            if info.get("status", "NEUTRAL").upper() == "BLACKLIST"
        }
    else:
        # NEUTRAL
        db = {
            w: info for w, info in whale_manager._load_db(whale_manager.DISCOVERY_FILE).items()
            if info.get("status", "NEUTRAL").upper() == "NEUTRAL"
        }

    now = time.time()
    candidates = []

    for wallet, info in db.items():
        last_active = info.get("last_active", 0.0)
        if recent_days is not None:
            age_days = (now - last_active) / 86400.0
            if age_days > recent_days:
                continue
        candidates.append((wallet, info))

    # Sort descending by last_active (most recently active first)
    candidates.sort(key=lambda item: item[1].get("last_active", 0.0), reverse=True)

    if limit is not None and limit > 0:
        candidates = candidates[:limit]

    return candidates


# ─── Ranking & Filtration ─────────────────────────────────────────────────────

def rank_and_filter_whales(
    profiles: List[dict],
    min_wr: float = 50.0,
    min_trades: int = 5,
    min_profit: float = 0.0,
    max_trades: int = 250,
    filter_banned_tags: bool = True
) -> List[dict]:
    """
    Filter and rank whale profiles based on 7-day metrics, PnL, and tag safety.
    Returns sorted list of qualified alpha candidates.
    """
    ranked = []

    for p in profiles:
        s7d = p.get("7d", {})
        wr = s7d.get("winrate", 0.0)
        trades = s7d.get("trades", 0)
        realized = s7d.get("realized", 0.0)
        tags = [t.lower() for t in p.get("tags", [])]

        # Tag filter (reject sniper/mev bots)
        if filter_banned_tags:
            has_banned_tag = any(banned in tags for banned in GMGN_BANNED_TAGS)
            if has_banned_tag:
                continue

        # Trade volume filter (reject inactive and hyperactive sniper bots)
        if trades < min_trades or trades > max_trades:
            continue

        # Win rate and profit filters
        if wr < min_wr:
            continue

        if realized < min_profit:
            continue

        # Dynamic Alpha Score:
        # Base: Winrate (0-100) + Log-scaled Profit bonus + Activity bonus
        profit_bonus = min(50.0, max(0.0, realized / 500.0))  # +10 score per $5,000 profit
        trade_confidence = min(20.0, trades * 0.5)           # up to +20 score for trade depth
        alpha_score = wr + profit_bonus + trade_confidence

        candidate = dict(p)
        candidate["alpha_score"] = round(alpha_score, 2)
        candidate["winrate_7d"] = wr
        candidate["trades_7d"] = trades
        candidate["realized_7d"] = realized
        ranked.append(candidate)

    # Sort by alpha score descending, then realized profit
    ranked.sort(key=lambda x: (x.get("alpha_score", 0), x.get("realized_7d", 0)), reverse=True)
    return ranked


# ─── Export Utilities ─────────────────────────────────────────────────────────

def export_ranked_json(ranked_whales: List[dict], target_path: str = RANKED_FILE):
    """Save ranked list to formatted JSON file, preserving existing multi-channel discovery metadata."""
    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
    existing_by_wallet: Dict[str, dict] = {}
    if os.path.exists(target_path):
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                existing_list = json.load(f)
            if isinstance(existing_list, list):
                existing_by_wallet = {
                    row.get("wallet"): row for row in existing_list if isinstance(row, dict) and row.get("wallet")
                }
        except Exception:
            existing_by_wallet = {}

    merged_list = []
    for item in ranked_whales:
        w = item.get("wallet")
        prior = existing_by_wallet.get(w, {}) if w else {}
        merged = dict(prior)
        merged.update(item)
        prior_sources = [s for s in (prior.get("discovery_sources", []) or []) if s]
        cur_sources = [s for s in (item.get("discovery_sources", []) or []) if s]
        combined_sources = sorted(set(prior_sources) | set(cur_sources) | {"gmgn"})
        if len(combined_sources) > 1 and "local_history" in combined_sources:
            combined_sources = [s for s in combined_sources if s != "local_history"]
        merged["discovery_sources"] = combined_sources
        merged_list.append(merged)

    temp_path = f"{target_path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(merged_list, f, indent=2)
    os.replace(temp_path, target_path)


def export_ranked_csv(ranked_whales: List[dict], target_path: str):
    """Export ranked whales to CSV for spreadsheet analysis."""
    import csv
    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
    with open(target_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Rank", "Wallet", "Alpha Score", "7d Winrate (%)", "7d Trades",
            "7d Realized Profit ($)", "SOL Balance", "Tags",
            "1d Winrate (%)", "1d Trades", "30d Winrate (%)", "30d Trades"
        ])
        for idx, w in enumerate(ranked_whales, 1):
            s1d = w.get("1d", {})
            s7d = w.get("7d", {})
            s30d = w.get("30d", {})
            tags_str = ", ".join(w.get("tags", []))
            writer.writerow([
                idx,
                w.get("wallet", ""),
                w.get("alpha_score", 0),
                f"{s7d.get('winrate', 0):.1f}",
                s7d.get("trades", 0),
                f"{s7d.get('realized', 0):.2f}",
                f"{w.get('native_balance', 0):.3f}",
                tags_str,
                f"{s1d.get('winrate', 0):.1f}",
                s1d.get("trades", 0),
                f"{s30d.get('winrate', 0):.1f}",
                s30d.get("trades", 0),
            ])
