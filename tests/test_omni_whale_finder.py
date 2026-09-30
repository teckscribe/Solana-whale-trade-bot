"""
tests/test_omni_whale_finder.py
=============================================================================
Unit and integration tests for the 4-channel Omni-Whale Discovery engine
and web endpoints.
=============================================================================
"""

import os
import sys
import json
import pytest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TESTS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import omni_whale_finder
from omni_whale_finder import (
    DiscoverySource,
    detect_cluster_consensus,
    BANNED_WHALE_TAGS,
    MAX_SAFE_7D_TRADES,
    MIN_SAFE_AI_SCORE
)


def test_anti_toxicity_constants():
    """Verify anti-toxicity constants match institutional requirements."""
    assert "mev" in BANNED_WHALE_TAGS
    assert "sniper" in BANNED_WHALE_TAGS
    assert "arbitrager" in BANNED_WHALE_TAGS
    assert "dev" in BANNED_WHALE_TAGS
    assert MAX_SAFE_7D_TRADES == 200
    assert MIN_SAFE_AI_SCORE == 50


def test_cluster_consensus_detection():
    """Test multi-whale convergence and cross-channel synergy detection."""
    sample_candidates = [
        {
            "wallet": "WalletAlpha111111111111111111111111111111",
            "source": DiscoverySource.GMGN.value,
            "token": "TokenPump1111111111111111111111111111111111",
            "detail": "GMGN Smart Money"
        },
        {
            "wallet": "WalletBeta2222222222222222222222222222222",
            "source": DiscoverySource.DEXSCREENER.value,
            "token": "TokenPump1111111111111111111111111111111111",
            "detail": "DexScreener Breakout Accumulator"
        },
        {
            "wallet": "WalletAlpha111111111111111111111111111111",
            "source": DiscoverySource.DEXSCREENER.value,
            "token": "TokenPump2222222222222222222222222222222222",
            "detail": "DexScreener Breakout Accumulator"
        },
        {
            "wallet": "WalletSolo3333333333333333333333333333333",
            "source": DiscoverySource.DEX_ONCHAIN.value,
            "token": "TokenSolo3333333333333333333333333333333333",
            "detail": "DEX Pool Whale"
        }
    ]

    known_wl = {"WhaleKnown1111111111111111111111111111111"}
    consensus = detect_cluster_consensus(sample_candidates, known_wl)

    # WalletAlpha and WalletBeta converged on TokenPump111...
    wallets_found = [c["wallet"] for c in consensus]
    assert "WalletAlpha111111111111111111111111111111" in wallets_found
    assert "WalletBeta2222222222222222222222222222222" in wallets_found
    # Solo wallet should not trigger cluster consensus
    assert "WalletSolo3333333333333333333333333333333" not in wallets_found


def test_omni_whale_discovery_orchestrator(tmp_path):
    """Test full multi-channel discovery orchestrator with safety filtration."""
    async def _run():
        fake_ranked_file = str(tmp_path / "neutral_whales_ranked.json")

        mock_c1 = [{"wallet": "WalletCleanAlpha111111111111111111111111", "source": "gmgn", "token": "TokenA", "detail": "GMGN Maker"}]
        mock_c2 = [{"wallet": "WalletCleanAlpha111111111111111111111111", "source": "dexscreener", "token": "TokenA", "detail": "Dex Accumulator"}]
        mock_c3 = [{"wallet": "WalletToxicSniper22222222222222222222222", "source": "dex_onchain", "token": "TokenB", "detail": "Pool Buyer"}]

        def fake_profile(wallet, **kwargs):
            if "CleanAlpha" in wallet:
                return {
                    "wallet": wallet,
                    "7d": {"winrate": 78.5, "trades": 25, "realized": 1250.0, "native_balance": 15.0},
                    "tags": ["smart_money", "pump_fun"]
                }
            else:
                return {
                    "wallet": wallet,
                    "7d": {"winrate": 95.0, "trades": 850, "realized": 5000.0, "native_balance": 5.0},
                    "tags": ["sniper", "axiom"]
                }

        with patch("omni_whale_finder.RANKED_FILE", fake_ranked_file), \
             patch("omni_whale_finder.fetch_gmgn_smart_money", new_callable=AsyncMock, return_value=mock_c1), \
             patch("omni_whale_finder.fetch_dexscreener_breakout_whales", new_callable=AsyncMock, return_value=mock_c2), \
             patch("omni_whale_finder.fetch_onchain_dex_whales", new_callable=AsyncMock, return_value=mock_c3), \
             patch("whale_scanner_core.fetch_wallet_profile", side_effect=fake_profile):

            progress_msgs = []
            def on_progress(msg, cur, tot):
                progress_msgs.append((msg, cur, tot))

            res = await omni_whale_finder.run_omni_whale_discovery(
                sources=["gmgn", "dexscreener", "dex_onchain", "cluster"],
                limit_per_source=5,
                progress_callback=on_progress
            )

            assert res["status"] == "completed"
            assert res["total_evaluated"] == 2
            assert res["clean_alphas_found"] == 1
            assert res["blocked_toxic_found"] == 1

            # Check saved JSON
            assert os.path.exists(fake_ranked_file)
            with open(fake_ranked_file, "r", encoding="utf-8") as f:
                saved = json.load(f)

            assert len(saved) == 2
            clean_w = next(x for x in saved if "CleanAlpha" in x["wallet"])
            assert "gmgn" in clean_w["discovery_sources"]
            assert "dexscreener" in clean_w["discovery_sources"]
            assert "cluster" in clean_w["discovery_sources"]
            assert clean_w["ai_score"] >= 50

    asyncio.run(_run())


def test_web_endpoints_omni_channels(tmp_path):
    """Test web_server endpoints with omni-channel discovery and filtering."""
    from fastapi.testclient import TestClient
    import web_server

    client = TestClient(web_server.app)

    # Test GET /api/whales/neutral filtering by channel
    test_ranked = [
        {
            "wallet": "WalletA1111111111111111111111111111111111",
            "7d": {"winrate": 75.0, "trades": 20, "realized": 800.0},
            "tags": ["smart_money"],
            "discovery_sources": ["gmgn"]
        },
        {
            "wallet": "WalletB2222222222222222222222222222222222",
            "7d": {"winrate": 80.0, "trades": 15, "realized": 1200.0},
            "tags": ["runner"],
            "discovery_sources": ["dexscreener", "cluster"]
        }
    ]

    ranked_file = tmp_path / "neutral_whales_ranked.json"
    ranked_file.write_text(json.dumps(test_ranked), encoding="utf-8")

    with patch.object(web_server, "BASE_DIR", str(tmp_path)), \
         patch("whale_manager.get_whitelist_set", return_value=set()), \
         patch("whale_manager.get_discovery_data", return_value={}):

        os.makedirs(tmp_path / "data", exist_ok=True)
        (tmp_path / "data" / "neutral_whales_ranked.json").write_text(json.dumps(test_ranked), encoding="utf-8")

        # 1. Fetch all
        res_all = client.get("/api/whales/neutral?source=all")
        assert res_all.status_code == 200
        data_all = res_all.json()
        assert len(data_all["candidates"]) == 2

        # 2. Filter by dexscreener
        res_dex = client.get("/api/whales/neutral?source=dexscreener")
        assert res_dex.status_code == 200
        data_dex = res_dex.json()
        assert len(data_dex["candidates"]) == 1
        assert "dexscreener" in data_dex["candidates"][0]["discovery_sources"]

        # 3. Filter by cluster
        res_clust = client.get("/api/whales/neutral?source=cluster")
        assert res_clust.status_code == 200
        data_clust = res_clust.json()
        assert len(data_clust["candidates"]) == 1
        assert "cluster" in data_clust["candidates"][0]["discovery_sources"]

        # 4. Filter by gmgn
        res_gmgn = client.get("/api/whales/neutral?source=gmgn")
        assert res_gmgn.status_code == 200
        data_gmgn = res_gmgn.json()
        assert len(data_gmgn["candidates"]) == 1
        assert "gmgn" in data_gmgn["candidates"][0]["discovery_sources"]

        # 5. Scan status check
        res_st = client.get("/api/whales/scan/status")
        assert res_st.status_code == 200
        st = res_st.json()
        assert "is_scanning" in st
        assert "channels" in st
