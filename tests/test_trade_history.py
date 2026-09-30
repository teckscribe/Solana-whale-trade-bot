import json
import os
import sys
import tempfile
import unittest


_DIR = os.path.dirname(os.path.abspath(__file__))
_WTB_DIR = os.path.dirname(_DIR)
if _WTB_DIR not in sys.path:
    sys.path.insert(0, _WTB_DIR)

from trade_history import load_trade_history


class TestTradeHistory(unittest.TestCase):
    def test_jsonl_is_primary_and_legacy_data_is_ignored_when_present(self):
        first = {
            "timestamp_entry": "2026-01-01T00:00:00+00:00",
            "token_address": "TokenA",
            "whale_wallet": "WalletA",
            "net_profit_usd": 1.0,
        }
        duplicate = {**first, "net_profit_usd": 999.0}
        second = {
            "timestamp_entry": "2026-01-02T00:00:00+00:00",
            "token_address": "TokenB",
            "whale_wallet": "WalletB",
            "net_profit_usd": -1.0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = os.path.join(tmp, "history.jsonl")
            json_path = os.path.join(tmp, "history.json")
            with open(jsonl_path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(first) + "\n")
                handle.write("{malformed\n")
            with open(json_path, "w", encoding="utf-8") as handle:
                json.dump([duplicate, second], handle)

            rows = load_trade_history(jsonl_path, json_path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["net_profit_usd"], 1.0)

    def test_legacy_json_is_used_when_jsonl_has_no_valid_records(self):
        row = {
            "timestamp_entry": "2026-01-02T00:00:00+00:00",
            "token_address": "TokenB",
            "whale_wallet": "WalletB",
        }
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = os.path.join(tmp, "missing.jsonl")
            json_path = os.path.join(tmp, "history.json")
            with open(json_path, "w", encoding="utf-8") as handle:
                json.dump([row], handle)
            rows = load_trade_history(jsonl_path, json_path)
        self.assertEqual(rows, [row])


if __name__ == "__main__":
    unittest.main()
