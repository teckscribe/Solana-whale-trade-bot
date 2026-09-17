import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

log = logging.getLogger("SettingsManager")

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_PROJECT_DIR, "data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
HISTORY_FILE = os.path.join(DATA_DIR, "settings_history.jsonl")
ENV_FILE = os.path.join(_PROJECT_DIR, ".env")

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")

def _s(key: str, type_: str, default: Any, min_val: float = None, max_val: float = None, hot: bool = True, group: str = "General", help_: str = "", options: list = None) -> dict:
    d = {"key": key, "type": type_, "default": default, "hot": hot, "group": group, "help": help_}
    if min_val is not None:
        d["min"] = min_val
    if max_val is not None:
        d["max"] = max_val
    if options is not None:
        d["options"] = options
    return d

SPEC = [
    _s("TRADE_MODE", "choice", "PAPER", options=["PAPER", "LIVE", "TRUE", "FALSE"], group="General", help_="Trading mode"),
    _s("ALLOCATION_PCT", "float", 10.0, min_val=1.0, max_val=100.0, group="Risk", help_="Percent of balance to allocate per trade"),
    _s("TAKE_PROFIT_PCT", "float", 10.0, min_val=1.0, max_val=500.0, group="Risk", help_="Take profit percentage"),
    _s("STOP_LOSS_PCT", "float", -5.0, min_val=-50.0, max_val=-0.5, group="Risk", help_="Stop loss percentage (negative)"),
    _s("TIMEOUT_ENABLED", "bool", True, group="Risk", help_="Enable time-based exit"),
    _s("TIMEOUT_MINUTES", "int", 30, min_val=1, max_val=1440, group="Risk", help_="Maximum hold time in minutes"),
    _s("MAX_CONCURRENT_TRADES", "int", 10, min_val=1, max_val=50, group="Risk", help_="Maximum simultaneous open trades"),
    _s("MIN_24H_VOLUME", "float", 50000.0, min_val=0.0, max_val=1000000.0, group="Filter", help_="Minimum 24h volume"),
    _s("MIN_MARKET_CAP", "float", 50000.0, min_val=0.0, max_val=10000000.0, group="Filter", help_="Minimum market cap"),
    _s("ML_ENGINE", "bool", False, group="ML", help_="Enable Machine Learning engine"),
    _s("ML_CONFIDENCE", "float", 80.0, min_val=0.0, max_val=100.0, group="ML", help_="Minimum ML confidence score"),
    _s("MOMENTUM_FILTER_ENABLED", "bool", True, group="Filter", help_="Enable momentum filter"),
    _s("MAX_M5_PUMP_PCT", "float", 300.0, min_val=0.0, max_val=1000.0, group="Filter", help_="Maximum 5m pump percentage"),
    _s("PAPER_FEE_PCT_PER_LEG", "float", 1.0, min_val=0.0, max_val=5.0, group="Fees", help_="Paper trading fee percentage per leg"),
    _s("MAX_PRICE_IMPACT_PCT", "float", 3.0, min_val=0.1, max_val=20.0, group="Risk", help_="Maximum acceptable price impact"),
    _s("MAX_POOL_SHARE_PCT", "float", 1.0, min_val=0.1, max_val=10.0, group="Risk", help_="Maximum share of pool liquidity"),
    _s("MAX_PORTFOLIO_EXPOSURE_PCT", "float", 100.0, min_val=10.0, max_val=100.0, group="Risk", help_="Max portfolio exposure percentage"),
    _s("MIN_TRADE_SOL", "float", 0.02, min_val=0.001, max_val=10.0, group="Risk", help_="Minimum trade size in SOL"),
    _s("LIVE_FEE_RESERVE_SOL", "float", 0.01, min_val=0.001, max_val=1.0, group="Risk", help_="SOL reserve left unallocated for fees"),
    _s("GMGN_DISCOVERY_ENABLED", "bool", True, group="General", help_="Enable GMGN discovery"),
    _s("DISCOVERY_INTERVAL_MINUTES", "int", 10, min_val=1, max_val=60, group="General", help_="Interval for discovery in minutes"),
    _s("PAPER_BALANCE_USD", "float", 25.0, min_val=1.0, max_val=100000.0, group="General", help_="Starting paper balance in USD"),
    _s("PAPER_WALLET_BALANCE", "float", 25.0, min_val=0.0, max_val=100000.0, group="General", help_="Current simulated paper wallet balance in USD"),
    _s("POLL_INTERVAL", "float", 2.0, min_val=1.0, max_val=30.0, group="General", help_="Polling interval (dynamically computed in trade_brain)"),
    _s("MAX_RESUME_AGE_HOURS", "float", 2.0, min_val=0.1, max_val=48.0, group="General", help_="Maximum age in hours of saved positions to resume on startup"),
]

SPEC_BY_KEY = {e["key"]: e for e in SPEC}
KEYS = [e["key"] for e in SPEC]

def validate(key: str, raw: Any) -> Tuple[bool, Any, str]:
    """Validate and coerce a setting value according to its SPEC."""
    spec = SPEC_BY_KEY.get(key)
    if spec is None:
        return False, None, f"{key}: not a known setting"
    t = spec["type"]

    if isinstance(raw, bool):
        s = "true" if raw else "false"
    else:
        s = str(raw).strip()
    
    if s == "":
        return False, None, f"{key}: value required"

    if t == "bool":
        if s.lower() in _TRUE:
            return True, True, ""
        if s.lower() in _FALSE:
            return True, False, ""
        return False, None, f"{key}: expected true or false"

    if t == "choice":
        for opt in spec["options"]:
            if s.lower() == opt.lower():
                return True, opt, ""
        return False, None, f"{key}: must be one of {', '.join(spec['options'])}"

    if t in ("int", "float"):
        try:
            v = int(float(s)) if t == "int" else float(s)
        except ValueError:
            return False, None, f"{key}: not a valid {t}"
        if t == "int" and float(s) != v:
            return False, None, f"{key}: must be a whole number"
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and v < lo:
            return False, None, f"{key}: below minimum {lo}"
        if hi is not None and v > hi:
            return False, None, f"{key}: above maximum {hi}"
        return True, (v if t == "int" else round(v, 6)), ""

    return False, None, f"{key}: unknown type {t}"

def to_str(key: str, value: Any) -> str:
    """Canonical string representation for history and errors."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)

def _read_env_file() -> dict:
    """Reads .env file and extracts current variables."""
    out = {}
    try:
        with open(ENV_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out

def _atomic_write_text(path: str, text: str, prefix: str) -> bool:
    tmp = None
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=prefix, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
        return True
    except Exception as exc:
        log.error(f"atomic write to {path} failed: {exc}")
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False

# --- Caching & Loading System ---
_lock = threading.RLock()
_STAT_INTERVAL = 1.0
_cache = {"key": None, "checked": 0.0, "raw": {}, "typed": {}}

def _file_key(path: str) -> Tuple[float, int] | None:
    try:
        st = os.stat(path)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None

def _shell_overrides() -> dict:
    """Keys in os.environ whose value did not come from .env."""
    env_file = _read_env_file()
    out = {}
    for k in KEYS:
        v = os.environ.get(k)
        if v is not None and env_file.get(k) != v:
            out[k] = v
    return out

_overrides = _shell_overrides()
if _overrides:
    log.info(f"Settings overridden from the shell: {sorted(_overrides)}")

def _coerce(key: str, raw: Any, source: str) -> Any:
    ok, v, err = validate(key, raw)
    if ok:
        return v
    log.warning(f"{err} (in {source}) — using default {SPEC_BY_KEY[key]['default']!r}")
    return SPEC_BY_KEY[key]["default"]

def _read_file_raw() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("top level is not an object")
        return data
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.error(f"settings.json unreadable ({exc}) — falling back to .env/defaults")
        return {}

def _build_typed(raw: dict, use_overrides: bool = True) -> dict:
    env_file = None
    typed = {}
    for key in KEYS:
        if use_overrides and key in _overrides:
            typed[key] = _coerce(key, _overrides[key], "shell env")
        elif key in raw:
            typed[key] = _coerce(key, raw[key], "settings.json")
        elif key == "PAPER_WALLET_BALANCE" and "PAPER_BALANCE_USD" in raw:
            typed[key] = _coerce(key, raw["PAPER_BALANCE_USD"], "settings.json")
        else:
            if env_file is None:
                env_file = _read_env_file()
            if key in env_file:
                typed[key] = _coerce(key, env_file[key], ".env")
            elif key == "PAPER_WALLET_BALANCE" and "PAPER_BALANCE_USD" in env_file:
                typed[key] = _coerce(key, env_file["PAPER_BALANCE_USD"], ".env")
            else:
                typed[key] = SPEC_BY_KEY[key]["default"]
    return typed

def _load() -> dict:
    """Returns typed values for all settings, caching re-reads aggressively."""
    now = time.monotonic()
    with _lock:
        if _cache["typed"] and (now - _cache["checked"] < _STAT_INTERVAL):
            return _cache["typed"]
        key = _file_key(SETTINGS_FILE)
        if _cache["typed"] and key is not None and key == _cache["key"]:
            _cache["checked"] = now
            return _cache["typed"]
        raw = _read_file_raw()
        _cache.update(key=key, checked=now, raw=raw, typed=_build_typed(raw))
        return _cache["typed"]

def get(key: str) -> Any:
    """Get the current typed value for a setting. Raises KeyError if unknown."""
    if key not in SPEC_BY_KEY:
        raise KeyError(f"{key} is not a known setting")
    return _load()[key]

def get_all() -> Dict[str, Any]:
    """Get all current typed values."""
    return dict(_load())

def _write_raw(raw: dict) -> bool:
    body = {
        "_comment": "Runtime settings for WTB. Managed by settings_manager.py.",
        "_updated": datetime.now(timezone.utc).isoformat(),
    }
    for k in KEYS:
        if k in raw:
            body[k] = raw[k]
    
    os.makedirs(DATA_DIR, exist_ok=True)
    ok = _atomic_write_text(SETTINGS_FILE, json.dumps(body, indent=2) + "\n", prefix=".settings.")
    if ok:
        with _lock:
            _cache.update(key=_file_key(SETTINGS_FILE), checked=time.monotonic(), raw=body, typed=_build_typed(body))
    return ok

def _append_history(key: str, from_val: Any, to_val: Any, source: str) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": ts,
                "source": source,
                "key": key,
                "from": from_val,
                "to": to_val
            }) + "\n")
    except Exception as exc:
        log.warning(f"settings history append failed: {exc}")

def update(key: str, value: Any, source: str = "unknown") -> Tuple[bool, str]:
    """Validate and persist a single key."""
    if key not in SPEC_BY_KEY:
        return False, f"{key}: not a known setting"

    with _lock:
        current = _load()
        ok, typed, err = validate(key, value)
        if not ok:
            return False, err
        
        current_val = current.get(key)
        if current_val == typed:
            return True, "" # No change needed

        raw = dict(_read_file_raw())
        raw.pop("_comment", None)
        raw.pop("_updated", None)
        
        fallback = _build_typed(raw, use_overrides=False)
        for k in KEYS:
            raw.setdefault(k, fallback[k])
        
        raw[key] = typed
        if key == "PAPER_BALANCE_USD":
            raw["PAPER_WALLET_BALANCE"] = typed

        if not _write_raw(raw):
            return False, "could not write settings.json"
        
        from_str = to_str(key, current_val)
        to_str_val = to_str(key, typed)
        log.info(f"[SETTINGS] {key}: {from_str!r} -> {to_str_val!r} ({source})")
        _append_history(key, from_str, to_str_val, source)
        
        return True, ""

def set_paper_wallet_balance(balance: float) -> bool:
    """Fast-path balance update directly to settings.json without audit log spam."""
    with _lock:
        val = round(float(balance), 6)
        raw = dict(_read_file_raw())
        raw.pop("_comment", None)
        raw.pop("_updated", None)
        fallback = _build_typed(raw, use_overrides=False)
        for k in KEYS:
            raw.setdefault(k, fallback[k])
        if raw.get("PAPER_WALLET_BALANCE") == val:
            return True
        raw["PAPER_WALLET_BALANCE"] = val
        return _write_raw(raw)

def set_value(key: str, value: Any, source: str = "unknown") -> Tuple[bool, str]:
    """Alias for update() for compatibility."""
    return update(key, value, source)

def migrate_from_env() -> bool:
    """One-time migration: reads .env values and seeds settings.json if missing."""
    if os.path.exists(SETTINGS_FILE):
        return False
    
    os.makedirs(DATA_DIR, exist_ok=True)
    env_file = _read_env_file()
    raw = {}
    seeded = []
    
    for key in KEYS:
        if key in env_file:
            ok, typed, err = validate(key, env_file[key])
            if ok:
                raw[key] = typed
                seeded.append(key)
                continue
            log.warning(f"migrate: {err} — using default")
        raw[key] = SPEC_BY_KEY[key]["default"]
        
    if _write_raw(raw):
        log.info(f"Created {SETTINGS_FILE} — {len(seeded)} value(s) seeded from .env: {', '.join(seeded)}")
        return True
    return False
