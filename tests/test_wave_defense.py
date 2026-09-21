"""
tests/test_wave_defense.py
=========================================================
Unit tests for Anti-Seeder Wave Attack Defense & Signer Provenance
(Adapted from FOMO Robinhood Radar provenance engine)
=========================================================
"""

import os
import sys
import json
import time
import asyncio
import tempfile
import unittest
from unittest import mock

_DIR = os.path.dirname(os.path.abspath(__file__))
_WTB_DIR = os.path.dirname(_DIR)
if _WTB_DIR not in sys.path:
    sys.path.insert(0, _WTB_DIR)

from trade_brain import STATE, TradingState
import trade_brain
import decoder


def run(coro):
    return asyncio.run(coro)


class TestWaveDefense(unittest.TestCase):

    def setUp(self):
        self.state = TradingState()
        self.state.seeded_blacklist.clear()
        self.state.token_buy_history.clear()
        self.token = "FakeAttackTokenMint1111111111111111111111111"
        self.whale_1 = "WhaleOne111111111111111111111111111111111111"
        self.whale_2 = "WhaleTwo222222222222222222222222222222222222"
        self.whale_3 = "WhaleThree3333333333333333333333333333333333"

    def test_wave_triggers_on_sequential_small_buys(self):
        """Coordinated micro-buys (e.g. 0.05 SOL each, 10s apart) must trigger wave detection."""
        now = time.time()
        # Whale 1 buys 0.05 SOL
        is_wave_1, reason_1 = self.state.detect_seeder_wave(
            token_address=self.token,
            wallet=self.whale_1,
            sol_spent=0.05,
            now_ts=now,
            window_seconds=300,
            min_wallets=2,
            max_buy_sol=0.20
        )
        self.assertFalse(is_wave_1)
        self.assertNotIn(self.token, self.state.seeded_blacklist)

        # Whale 2 buys 0.05 SOL 10 seconds later
        is_wave_2, reason_2 = self.state.detect_seeder_wave(
            token_address=self.token,
            wallet=self.whale_2,
            sol_spent=0.05,
            now_ts=now + 10,
            window_seconds=300,
            min_wallets=2,
            max_buy_sol=0.20
        )
        self.assertTrue(is_wave_2)
        self.assertIn("seeder wave", reason_2.lower())
        self.assertIn(self.token, self.state.seeded_blacklist)

        # A third buy must be rejected immediately from the blacklist
        is_wave_3, reason_3 = self.state.detect_seeder_wave(
            token_address=self.token,
            wallet=self.whale_3,
            sol_spent=0.50,
            now_ts=now + 20,
        )
        self.assertTrue(is_wave_3)
        self.assertIn("already blacklisted", reason_3.lower())

    def test_wave_allows_large_independent_buys(self):
        """Large genuine whale entries (e.g. 1.5 SOL and 2.0 SOL) must NOT trigger wave detection."""
        now = time.time()
        # Whale 1 buys 1.5 SOL
        is_wave_1, _ = self.state.detect_seeder_wave(
            token_address=self.token,
            wallet=self.whale_1,
            sol_spent=1.5,
            now_ts=now,
            window_seconds=300,
            min_wallets=2,
            max_buy_sol=0.20
        )
        self.assertFalse(is_wave_1)

        # Whale 2 buys 2.0 SOL
        is_wave_2, _ = self.state.detect_seeder_wave(
            token_address=self.token,
            wallet=self.whale_2,
            sol_spent=2.0,
            now_ts=now + 30,
            window_seconds=300,
            min_wallets=2,
            max_buy_sol=0.20
        )
        self.assertFalse(is_wave_2)
        self.assertNotIn(self.token, self.state.seeded_blacklist)

    def test_record_trade_skips_blacklisted_seeded_tokens(self):
        """record_trade must immediately drop tokens in STATE.seeded_blacklist."""
        STATE.seeded_blacklist.add(self.token)
        called = {"get_price": False}

        async def fake_price(*a, **k):
            called["get_price"] = True
            return (1.0, "TKN", "Token", 1e6, 1e6)

        with mock.patch.object(trade_brain, "get_token_price_usd", fake_price):
            run(trade_brain.record_trade({
                "wallet": self.whale_1,
                "signature": "sig_test",
                "bought": [{"mint": self.token, "amount": 100.0}],
                "sold": [{"mint": trade_brain.SOL_MINT, "amount": 0.05}],
            }))

        self.assertFalse(called["get_price"], "record_trade should have skipped the blacklisted token before API calls")
        STATE.seeded_blacklist.remove(self.token)

    def test_signer_provenance_check(self):
        """decoder.py must reject transactions where whale is not an authorized signer."""
        # Create a mock Solana transaction message
        mock_tx = mock.MagicMock()
        mock_msg = mock.MagicMock()
        mock_meta = mock.MagicMock()

        mock_msg.account_keys = [
            mock.MagicMock(pubkey="AttackerPubkey11111111111111111111111111"),
            mock.MagicMock(pubkey=self.whale_1),
        ]
        # Only index 0 is a signer
        mock_msg.header.num_required_signatures = 1
        mock_msg.is_signer.side_effect = lambda idx: idx == 0

        mock_tx.transaction.message = mock_msg
        mock_tx.meta = mock_meta
        mock_meta.err = None

        mock_resp = mock.MagicMock()
        mock_resp.value = mock_tx

        with mock.patch("decoder.get_rpc_client") as mock_rpc_getter:
            mock_client = mock.AsyncMock()
            mock_client.get_transaction.return_value = mock_resp
            mock_rpc_getter.return_value = mock_client

            with mock.patch("decoder.asyncio.sleep", mock.AsyncMock()):
                # Whale 1 is at index 1 (not a signer)
                res = run(decoder.decode_transaction("fake_sig_111111111111111111111111111111", self.whale_1))
                self.assertIsNone(res, "Decoder should have rejected non-signer transaction provenance")


if __name__ == "__main__":
    unittest.main()
