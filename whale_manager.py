"""
whale_manager.py
RAM-first whale state manager with stat-validated caching.

Manages the persistency and aging of discovered whales.
Stores whales in whitelist.json and discovery_db.json with status support
(NEUTRAL, WHITELIST, BLACKLIST).

TRAM Architecture:
  - Both JSON databases are loaded into RAM on startup.
  - All lookups (get_whale_status, get_whitelist, get_active_whales) operate on
    in-memory dicts/sets — zero disk I/O on the hot path.
  - External modifications (from Telegram bot running in a separate process) are
    detected via (st_mtime, st_size) stat checks, triggered by refresh_if_changed().
  - Write-through: set_whale_status() updates both RAM and disk atomically.
"""

import os
import json
import time
import logging
import tempfile
import threading
from typing import Optional

log = logging.getLogger("WhaleManager")

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
WHITELIST_FILE = os.path.join(_BOT_DIR, "whitelist.json")
DISCOVERY_FILE = os.path.join(_BOT_DIR, "discovery_db.json")
OLD_WHALES_FILE = os.path.join(_BOT_DIR, "whales.json")

# ── In-memory state ──────────────────────────────────────────────────────────
_lock = threading.RLock()

# Master RAM copies of the two databases
_whitelist_data: dict = {}
_discovery_data: dict = {}

# Stat keys for change detection: (st_mtime, st_size)
_whitelist_stat: Optional[tuple] = None
_discovery_stat: Optional[tuple] = None

# Precomputed sets for O(1) lookups
_whitelist_set: frozenset = frozenset()
_blacklist_set: frozenset = frozenset()

# Throttle stat checks to avoid excessive syscalls
_MIN_STAT_INTERVAL = 0.5  # seconds
_last_stat_check = 0.0


# ── Low-level I/O (used sparingly) ──────────────────────────────────────────

def _file_stat(path: str) -> Optional[tuple]:
    """Return (st_mtime, st_size) for a file, or None if it doesn't exist."""
    try:
        st = os.stat(path)
        return (st.st_mtime, st.st_size)
    except FileNotFoundError:
        return None

def _load_db(file_path: str) -> dict:
    """Read and parse a JSON file from disk. Returns {} on any failure."""
    if not os.path.exists(file_path):
        return {}
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"Failed to load {file_path}: {e}")
        return {}

def _save_db(file_path: str, data: dict):
    """Atomic file write via tempfile.mkstemp + os.replace."""
    try:
        dir_path = os.path.dirname(file_path)
        os.makedirs(dir_path, exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(dir=dir_path)
        with os.fdopen(temp_fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_path, file_path)
    except Exception as e:
        log.error(f"Failed to save {file_path}: {e}")


# ── Cache rebuild ────────────────────────────────────────────────────────────

def _rebuild_sets():
    """Rebuild the precomputed O(1) lookup sets from the in-memory dicts."""
    global _whitelist_set, _blacklist_set
    _whitelist_set = frozenset(_whitelist_data.keys())
    _blacklist_set = frozenset(
        w for w, info in _discovery_data.items()
        if info.get("status", "NEUTRAL").upper() == "BLACKLIST"
    )

def _load_into_ram():
    """Load both databases from disk into RAM and rebuild lookup sets."""
    global _whitelist_data, _discovery_data, _whitelist_stat, _discovery_stat
    _whitelist_data = _load_db(WHITELIST_FILE)
    _discovery_data = _load_db(DISCOVERY_FILE)
    _whitelist_stat = _file_stat(WHITELIST_FILE)
    _discovery_stat = _file_stat(DISCOVERY_FILE)
    _rebuild_sets()
    log.info(
        f"Whale data loaded into RAM: {len(_whitelist_data)} whitelisted, "
        f"{len(_discovery_data)} discovery ({len(_blacklist_set)} blacklisted)"
    )


# ── Migration (legacy) ──────────────────────────────────────────────────────

def _migrate():
    """Migrates from old whales.json to new dual-file architecture if needed."""
    if os.path.exists(OLD_WHALES_FILE) and not os.path.exists(OLD_WHALES_FILE + ".bak"):
        log.info("Migrating old whales.json to new dual-file architecture...")
        old_data = _load_db(OLD_WHALES_FILE)
        whitelist_data = {}
        discovery_data = {}

        for w, info in old_data.items():
            status = info.get("status", "NEUTRAL").upper()
            if status == "WHITELIST":
                whitelist_data[w] = info
            else:
                discovery_data[w] = info

        _save_db(WHITELIST_FILE, whitelist_data)
        _save_db(DISCOVERY_FILE, discovery_data)

        os.rename(OLD_WHALES_FILE, OLD_WHALES_FILE + ".bak")
        log.info("Migration successful. Backed up old database to whales.json.bak")


# ── Initialization ───────────────────────────────────────────────────────────
_migrate()
_load_into_ram()


# ── Public API ───────────────────────────────────────────────────────────────

def refresh_if_changed() -> bool:
    """
    Check if either database file has been modified externally (e.g. by the
    Telegram bot process). If so, reload into RAM.

    Uses (st_mtime, st_size) stat checks, throttled to at most once per
    _MIN_STAT_INTERVAL seconds.

    Returns True if data was reloaded, False if still fresh.
    """
    global _last_stat_check
    now = time.monotonic()
    if now - _last_stat_check < _MIN_STAT_INTERVAL:
        return False
    _last_stat_check = now

    wl_stat = _file_stat(WHITELIST_FILE)
    disc_stat = _file_stat(DISCOVERY_FILE)

    if wl_stat == _whitelist_stat and disc_stat == _discovery_stat:
        return False

    with _lock:
        # Double-check after acquiring lock
        wl_stat2 = _file_stat(WHITELIST_FILE)
        disc_stat2 = _file_stat(DISCOVERY_FILE)
        if wl_stat2 == _whitelist_stat and disc_stat2 == _discovery_stat:
            return False
        _load_into_ram()
        log.info("Whale databases reloaded from disk (external modification detected)")
        return True


def get_whitelist_set() -> frozenset:
    """
    Returns a frozenset of all whitelisted wallet addresses for O(1)
    membership tests. Zero disk I/O — reads from RAM.
    """
    return _whitelist_set


def add_whales(new_wallets: set):
    """Adds new whales to the discovery database (only if not already known)."""
    with _lock:
        global _discovery_data, _discovery_stat
        now = time.time()
        added_count = 0

        for wallet in new_wallets:
            if wallet not in _whitelist_data and wallet not in _discovery_data:
                _discovery_data[wallet] = {
                    "discovered_at": now,
                    "last_active": now,
                    "status": "NEUTRAL"
                }
                added_count += 1

        if added_count > 0:
            _save_db(DISCOVERY_FILE, _discovery_data)
            _discovery_stat = _file_stat(DISCOVERY_FILE)
            _rebuild_sets()
            log.info(f"Added {added_count} new whales to discovery database.")


def update_activity(wallet: str):
    """Updates the last_active timestamp when a whale makes a trade."""
    with _lock:
        global _whitelist_stat, _discovery_stat
        if wallet in _whitelist_data:
            _whitelist_data[wallet]["last_active"] = time.time()
            _save_db(WHITELIST_FILE, _whitelist_data)
            _whitelist_stat = _file_stat(WHITELIST_FILE)
            return

        if wallet in _discovery_data:
            _discovery_data[wallet]["last_active"] = time.time()
            _save_db(DISCOVERY_FILE, _discovery_data)
            _discovery_stat = _file_stat(DISCOVERY_FILE)


def set_whale_status(wallet: str, status: str) -> bool:
    """
    Sets a whale's status: WHITELIST, BLACKLIST, or NEUTRAL.
    Write-through: updates both RAM and disk atomically.
    """
    status = status.upper()
    if status not in ["WHITELIST", "BLACKLIST", "NEUTRAL"]:
        return False

    with _lock:
        global _whitelist_data, _discovery_data, _whitelist_stat, _discovery_stat

        # Get existing info from either store, removing from both
        info_from_wl = _whitelist_data.pop(wallet, None)
        info_from_disc = _discovery_data.pop(wallet, None)
        info = info_from_wl or info_from_disc or {
            "discovered_at": time.time(),
            "last_active": time.time(),
        }
        info["status"] = status

        if status == "WHITELIST":
            _whitelist_data[wallet] = info
        else:
            _discovery_data[wallet] = info

        _save_db(WHITELIST_FILE, _whitelist_data)
        _save_db(DISCOVERY_FILE, _discovery_data)
        _whitelist_stat = _file_stat(WHITELIST_FILE)
        _discovery_stat = _file_stat(DISCOVERY_FILE)
        _rebuild_sets()
        log.info(f"Whale {wallet} moved to {status}")
        return True


def get_whale_status(wallet: str) -> str:
    """Returns the status of a specific whale. Zero disk I/O."""
    if wallet in _whitelist_set:
        return "WHITELIST"
    info = _discovery_data.get(wallet)
    if info:
        return info.get("status", "NEUTRAL").upper()
    return "NEUTRAL"


def get_whitelist() -> list:
    """Returns all whitelisted wallet addresses. Zero disk I/O."""
    return list(_whitelist_data.keys())


def get_blacklist() -> list:
    """Returns all blacklisted wallet addresses. Zero disk I/O."""
    return list(_blacklist_set)


def clear_blacklist() -> bool:
    """Clears all blacklisted wallets from the discovery db."""
    try:
        with _lock:
            global _discovery_data, _discovery_stat
            _discovery_data = {
                k: v for k, v in _discovery_data.items()
                if v.get("status") != "BLACKLIST"
            }
            _save_db(DISCOVERY_FILE, _discovery_data)
            _discovery_stat = _file_stat(DISCOVERY_FILE)
            _rebuild_sets()
        return True
    except Exception as e:
        log.error(f"Error clearing blacklist: {e}")
        return False


def remove_whale(wallet: str) -> bool:
    """Removes a whale entirely from the system."""
    with _lock:
        global _whitelist_stat, _discovery_stat
        removed = False

        if wallet in _whitelist_data:
            del _whitelist_data[wallet]
            _save_db(WHITELIST_FILE, _whitelist_data)
            _whitelist_stat = _file_stat(WHITELIST_FILE)
            removed = True

        if wallet in _discovery_data:
            del _discovery_data[wallet]
            _save_db(DISCOVERY_FILE, _discovery_data)
            _discovery_stat = _file_stat(DISCOVERY_FILE)
            removed = True

        if removed:
            _rebuild_sets()
        return removed


def _load_whales() -> dict:
    """Legacy compatibility function for generate_ml_data.py. Zero disk I/O."""
    combined = dict(_discovery_data)
    combined.update(_whitelist_data)
    return combined


def get_active_whales(limit=100, max_age_hours=24) -> set:
    """
    Returns whitelisted wallets only for live scanner monitoring.
    Zero disk I/O — returns from RAM.
    """
    return set(_whitelist_data.keys())


def get_total_whales() -> int:
    """Returns the total number of currently stored whales. Zero disk I/O."""
    return len(_whitelist_data) + len(_discovery_data)


def get_discovery_data() -> dict:
    """Returns a shallow copy of the discovery database for read-only access."""
    return dict(_discovery_data)


def get_whitelist_data() -> dict:
    """Returns a shallow copy of the whitelist database for read-only access."""
    return dict(_whitelist_data)
