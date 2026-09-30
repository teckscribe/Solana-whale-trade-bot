"""Canonical, read-only loader for completed WTB trade records."""

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple


log = logging.getLogger("TradeHistory")
_ROOT = os.path.dirname(os.path.abspath(__file__))
JSONL_FILE = os.path.join(_ROOT, "ml_training_data.jsonl")
JSON_FILE = os.path.join(_ROOT, "ml_training_data.json")


def _record_key(record: Dict[str, Any]) -> Tuple[str, str, str]:
    """Stable identity for a completed trade across JSONL/JSON mirrors."""
    return (
        str(record.get("timestamp_entry") or record.get("entry_time") or ""),
        str(record.get("token_address") or record.get("token") or ""),
        str(record.get("whale_wallet") or record.get("wallet") or ""),
    )


def load_trade_history(
    jsonl_path: Optional[str] = None,
    json_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Load the append-only JSONL ledger as the source of truth. The legacy JSON
    mirror is used only when JSONL has no valid records. Malformed records are
    skipped and duplicates removed.
    """
    jsonl_path = jsonl_path or JSONL_FILE
    json_path = json_path or JSON_FILE
    records: List[Dict[str, Any]] = []

    if os.path.exists(jsonl_path):
        try:
            with open(jsonl_path, "r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        value = json.loads(line)
                        if isinstance(value, dict):
                            records.append(value)
                    except (TypeError, json.JSONDecodeError) as exc:
                        log.warning("Skipping malformed trade-history line %s: %s", line_number, exc)
        except OSError as exc:
            log.warning("Could not read trade-history JSONL %s: %s", jsonl_path, exc)

    if not records and os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as handle:
                legacy = json.load(handle)
            if isinstance(legacy, list):
                records.extend(value for value in legacy if isinstance(value, dict))
            elif isinstance(legacy, dict):
                records.extend(value for value in legacy.values() if isinstance(value, dict))
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            log.warning("Could not read legacy trade-history JSON %s: %s", json_path, exc)

    deduplicated: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for record in records:
        key = _record_key(record)
        if not all(key):
            log.warning("Skipping trade-history record without timestamp/token/wallet identity")
            continue
        deduplicated.setdefault(key, record)

    return sorted(deduplicated.values(), key=lambda row: _record_key(row)[0])
